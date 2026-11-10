# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-QUANT
#  >>> FILE PATH:   quant/lab.py
#  z175 - THE STRATEGY LAB: retraining, nightly. The first backtest said each strategy loses ON AVERAGE across 314
#  names. An average hides where an edge lives - mid-size names, the middle of the day, one side, one sector - so
#  the lab keeps all three strategies and asks, for every segment of every universe name:
#    1. replay the last N sessions of real 1-minute bars once, recording every candidate trade ("event") with
#       permissive rules, its tags (liquidity, time of day, side, sector) and everything needed to judge it;
#    2. score each parameter variant on each event - its exit by the strategy's own rules on the minutes that
#       followed, net of the spread (scaled by the name's liquidity), slippage and fees;
#    3. TRAIN on the first two thirds of the sessions: in each segment pick the variant with the best t-statistic;
#       TEST that one choice on the last third, which it never saw;
#    4. with hundreds of segments tested, some pass by luck - so the out-of-sample p-values go through a
#       Benjamini-Hochberg false-discovery control (10 %); a segment goes live only if it survives that, made
#       money out of sample (>= 0.5 bps a trade after costs) and had enough trades in both halves.
#  -> data/quant/models.json (the engine trades live only in approved segments, shadow everywhere else - the
#     strategies keep running on every name) and data/quant/lab/latest.json (the full report for the Quant tab).
#  Limits, stated: one-minute bars (no queue position), a modelled spread, overlapping trades on one name and the
#  book's position limits are not simulated here (the engine applies them live), today's universe used for past
#  days. Shadow trades from the live engine are the second, independent check.
#
#  z180 - LAB v2: JUDGED BY SESSION, NOT BY TRADE. The first real run (11-07) approved 5 opening-range segments on
#  per-trade t-statistics - but trades on one day share one market and one news flow (a breakdown day sends dozens
#  of shorts the same way), so 2,131 test trades over 13 sessions were not 2,131 independent pieces of evidence.
#  Now every statistic that decides anything is session-clustered: a cluster-robust (CR1) standard error with the
#  sessions as clusters, Student t with sessions - 1 degrees of freedom; plus, to go live:
#    - training evidence of its own (clustered t >= MIN_TRAIN_T), so the test isn't the only filter;
#    - enough distinct sessions in both halves (MIN_TRAIN_SESSIONS / MIN_TEST_SESSIONS), not a few busy days;
#    - no single day carries it: out of sample, the result without its best session must still be positive;
#    - a QUAD segment ("quad:Q3") only where the quad label actually varies inside both windows - otherwise the label
#      is the calendar (all 13 test sessions were Q3, so "short + Q3" meant "short, lately") - and the engine
#      trades it live only while the regime is dialed in and the nowcast agrees with the official quad (STATUS 1b).
#  60 sessions by default (40 train / 20 test): more sessions = more clusters = an honest test with power.
#  models.json carries labVersion 2; the engine trusts no approval from an older lab.
# ============================================================
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from . import market_hours as mh
from .backtest import fetch_day, half_spread_bps, sessions
from .book import Book
from .config import CFG
from .universe import watch_symbols
from .ledger import DATA
from .segments import keys_for, liq_bucket, tod_bucket
from .strategies import EtfArb, OpeningRange, SectorReversion

GRIDS = {
    "sector_reversion": [{"z_in": z, "max_hold": h, "vol_spike_max": v} for z in (2.0, 2.5, 3.0) for h in (15, 30, 60) for v in (None, 3.0)],
    "etf_arb": [{"thr_mult": t, "max_hold": h, "persist": p} for t in (1.5, 2.5, 3.5) for h in (5, 10, 20) for p in (False, True)],
    "opening_range": [{"range_min": r, "target_mult": m, "last_entry": e} for r in (5, 15, 30) for m in (1.5, 2.0, 3.0) for e in (10 * 60 + 30, 11 * 60 + 30)],
}
MIN_TRAIN, MIN_TEST, FDR, MIN_EXP_BPS = 60, 30, 0.10, 0.5
LAB_VERSION = 2                     # z180: session-clustered inference (the engine refuses approvals from an older lab)
MIN_TRAIN_SESSIONS, MIN_TEST_SESSIONS = 12, 8   # z180: distinct sessions with trades in the segment
MIN_TRAIN_T = 1.0                   # z180: the training half must show evidence of its own (clustered t)
MIN_REGIME_SESSIONS = 5             # z180: a quad segment needs >= 5 sessions inside AND outside its quad in each half
DEFAULT_LAB_DAYS = 60               # z180: 40 train / 20 test sessions (was 40: 27 / 13)
FLAT_IDX = 385                      # 1555 ET as a minute index from 0930
SR_LEVELS, ETF_LEVELS = (2.0, 2.5, 3.0), (1.5, 2.5, 3.5)


def vid_of(strategy, params):
    short = {"sector_reversion": "sr", "etf_arb": "ea", "opening_range": "or"}[strategy]
    return short + ":" + "-".join(f"{k}={params[k]}" for k in sorted(params))


def log(*a):
    print("[lab]", *a, flush=True)


# ---------------------------------------------------------------------------------------------------- arrays
def minute_closes(rows, t_open, n=390):
    """Close of every minute 0..n-1 after the open, carried forward over silent minutes (None before the first trade)."""
    by = {int((t - t_open) // 60): c for (t, o, h, l, c, v) in rows}
    out, last = [], None
    for i in range(n):
        if i in by:
            last = by[i]
        out.append(last)
    return out


def minute_vols(rows, t_open, n=390):
    v = [0.0] * n
    for (t, o, h, l, c, vol) in rows:
        i = int((t - t_open) // 60)
        if 0 <= i < n:
            v[i] += vol or 0
    return v


def logret(c):
    return [math.log(c[i] / c[i - 1]) if (i and c[i] and c[i - 1]) else 0.0 for i in range(len(c))]


def z_at(rs, re_, j, W=60, L=10, min_bars=45):
    """Residual z at minute j (returns up to and including j), the same arithmetic as SectorReversion._z."""
    a = max(1, j - W + 1)
    if j - a + 1 < min_bars:
        return None
    x, y = re_[a:j + 1], rs[a:j + 1]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    vx = sum((u - mx) ** 2 for u in x)
    b = min(2.0, max(0.3, (sum((u - mx) * (w - my) for u, w in zip(x, y)) / vx) if vx > 0 else 1.0))
    res = [w - b * u for u, w in zip(x, y)]
    m = sum(res) / len(res)
    sd = math.sqrt(sum((r - m) ** 2 for r in res) / (len(res) - 1)) if len(res) > 1 else 0
    if sd <= 0:
        return None
    cum = sum(res[-L:])
    return cum / (sd * math.sqrt(L)), cum, sd


def gap_at(closes, members, etf, j, K=5, min_cover=0.6):
    ce = closes.get(etf)
    if not ce or j - K < 0 or not ce[j] or not ce[j - K]:
        return None
    tot = cov = 0.0
    for s, w in members:
        c = closes.get(s)
        if c and c[j] and c[j - K]:
            tot += w * math.log(c[j] / c[j - K])
            cov += w
    if cov < min_cover:
        return None
    return (tot / cov - math.log(ce[j] / ce[j - K])) * 1e4


# ---------------------------------------------------------------------------------------------------- events
def session_events(uni, bars, d, close_min, cfg=CFG):
    """Every candidate trade of one session for all three strategies (permissive rules), each with what's needed to
    score any variant later. Returns a list of dicts."""
    et0 = mh.et_now(datetime(d.year, d.month, d.day, 15, tzinfo=timezone.utc))
    t_open = et0.replace(hour=9, minute=30, second=0, microsecond=0).timestamp()
    n_min = max(1, (close_min - 9 * 60 - 30))
    liq = uni.get("liquidity") or {}
    sector_etf = uni.get("sector_etf") or {}
    weights = uni.get("etf_weights") or {}
    closes = {s: minute_closes(r, t_open, n_min) for s, r in bars.items()}
    rets = {s: logret([c if c else 0 for c in v]) for s, v in closes.items()}
    vols = {s: minute_vols(r, t_open, n_min) for s, r in bars.items()}
    cost = {s: 2 * half_spread_bps(liq.get(s), cfg.backtest_half_spread_bps) + 2 * cfg.slippage_bps + cfg.fee_bps_sell for s in bars}
    ev = []
    last_i = min(n_min, FLAT_IDX)
    # --- sector reversion: the first minute |z| crosses each level within an episode (it ends when |z| < 1)
    for s, etf in sector_etf.items():
        if s not in closes or etf not in closes:
            continue
        rs, re_ = rets[s], rets[etf]
        armed = {lv: True for lv in SR_LEVELS}
        for j in range(10, last_i):
            if closes[s][j] is None:
                continue
            zz = z_at(rs, re_, j)
            if not zz:
                continue
            z, cum, sd = zz
            if abs(z) < 1.0:
                armed = {lv: True for lv in SR_LEVELS}
                continue
            for lv in SR_LEVELS:
                if armed[lv] and abs(z) >= lv:
                    armed[lv] = False
                    minute = 9 * 60 + 30 + j
                    if minute < cfg.no_entry_before or minute > cfg.no_entry_after:
                        continue
                    v = vols[s]
                    before = sum(v[max(0, j - 39):j - 9]) / 3 if j >= 40 else None
                    spike = (sum(v[j - 9:j + 1]) / before) if before else None
                    side = "short" if z > 0 else "long"
                    ev.append({"strategy": "sector_reversion", "sym": s, "i": j, "side": side, "level": lv, "z": z, "cum": cum, "sd": sd,
                               "spike": spike, "cost": cost[s], "etf": etf,
                               "tags": {"liq": liq_bucket(liq.get(s)), "tod": tod_bucket(minute), "side": side, "sector": etf, "kind": "stock"}})
    # --- ETF arbitrage: the first minute the gap crosses each level (x the round trip cost), and the second minute
    #     in a row (the "persistent" version)
    for etf, members in weights.items():
        if etf not in closes:
            continue
        armed = {lv: [True, True] for lv in ETF_LEVELS}
        prev = None
        for j in range(5, last_i):
            g = gap_at(closes, members, etf, j)
            if g is None:
                prev = None
                continue
            c = cost[etf]
            if abs(g) < 0.5 * c:
                armed = {lv: [True, True] for lv in ETF_LEVELS}
            minute = 9 * 60 + 30 + j
            for lv in ETF_LEVELS:
                thr = max(6.0, lv * c)
                if abs(g) < thr:
                    continue
                side = "long" if g > 0 else "short"
                tags = {"liq": liq_bucket(liq.get(etf)), "tod": tod_bucket(minute), "side": side, "sector": etf, "kind": "fund"}
                ok_time = cfg.no_entry_before <= minute <= cfg.no_entry_after
                if armed[lv][0]:
                    armed[lv][0] = False
                    if ok_time:
                        ev.append({"strategy": "etf_arb", "sym": etf, "i": j, "side": side, "level": lv, "persist": False, "gap": g, "cost": c, "tags": tags})
                if armed[lv][1] and prev is not None and prev * g > 0 and abs(prev) >= 0.6 * thr:
                    armed[lv][1] = False
                    if ok_time:
                        ev.append({"strategy": "etf_arb", "sym": etf, "i": j, "side": side, "level": lv, "persist": True, "gap": g, "cost": c, "tags": tags})
            prev = g
    # --- opening range: the first clean break of the R-minute range, for R in 5 / 15 / 30
    for s in (uni.get("momentum") or []):
        c = closes.get(s)
        if not c:
            continue
        hi_lo = {}
        for (t, o, h, l, cl, v) in bars[s]:
            k = int((t - t_open) // 60)
            hi_lo[k] = (h, l)
        for R in (5, 15, 30):
            rng = [hi_lo[k] for k in range(R) if k in hi_lo]
            if len(rng) < max(3, R - 2):
                continue
            hi, lo = max(x[0] for x in rng), min(x[1] for x in rng)
            mid = (hi + lo) / 2
            if lo <= 0:
                continue
            width = (hi - lo) / mid * 1e4
            if not (10 <= width <= 500):
                continue
            for j in range(R, min(last_i, 11 * 60 + 30 - 9 * 60 - 30 + 1)):
                last, prev = c[j], c[j - 1]
                if last is None or prev is None:
                    continue
                if last > hi * 1.0002 and prev <= hi:
                    side, stop = "long", (last - mid) / last * 1e4
                elif last < lo * 0.9998 and prev >= lo:
                    side, stop = "short", (mid - last) / last * 1e4
                else:
                    continue
                minute = 9 * 60 + 30 + j
                if minute >= cfg.no_entry_before and stop > 0:
                    ev.append({"strategy": "opening_range", "sym": s, "i": j, "side": side, "R": R, "stop": stop, "cost": cost[s], "minute": minute,
                               "tags": {"liq": liq_bucket(liq.get(s)), "tod": tod_bucket(minute), "side": side, "sector": sector_etf.get(s) or "none",
                                        "kind": (uni.get("kinds") or {}).get(s, "stock")}})
                break
    return ev, closes, rets


# ---------------------------------------------------------------------------------------------------- scoring
def exit_move(c, i, side, stop, target, max_hold, extra_exit=None):
    """Net-of-nothing move (bps, in the trade's favour) from minute i to its exit: the stop, the target, the strategy's own
    exit (extra_exit(j) -> bool), the time stop, or 1555 ET - whichever comes first, judged on minute closes."""
    e0 = c[i]
    sign = 1 if side == "long" else -1
    last = min(len(c) - 1, i + max_hold, FLAT_IDX)
    j = i
    for j in range(i + 1, last + 1):
        if c[j] is None:
            continue
        mv = (c[j] / e0 - 1) * 1e4 * sign
        if mv <= -stop or mv >= target or (extra_exit and extra_exit(j)):
            return mv
    cj = next((c[k] for k in range(last, i, -1) if c[k] is not None), e0)
    return (cj / e0 - 1) * 1e4 * sign


def score_events(events, closes, rets, uni, cfg=CFG):
    """-> list of (strategy, variant_id, net_bps, tags) for every variant each event qualifies for."""
    weights = uni.get("etf_weights") or {}
    out = []
    zcache = {}
    for e in events:
        c = closes[e["sym"]]
        if c[e["i"]] is None:
            continue
        if e["strategy"] == "sector_reversion":
            target = abs(e["cum"]) * 1e4 * 0.6
            if target < 2.0 * e["cost"]:
                continue
            stop = max(12.0, 2.0 * e["sd"] * 1e4 * math.sqrt(10))
            rs, re_ = rets[e["sym"]], rets[e["etf"]]

            def reverted(j, rs=rs, re_=re_, side=e["side"], key=e["sym"]):
                zk = (key, j)
                if zk not in zcache:
                    zz = z_at(rs, re_, j)
                    zcache[zk] = zz[0] if zz else None
                z = zcache[zk]
                return z is not None and ((side == "long" and z >= -0.5) or (side == "short" and z <= 0.5))
            for p in GRIDS["sector_reversion"]:
                if p["z_in"] != e["level"]:
                    continue
                if p["vol_spike_max"] and e["spike"] is not None and e["spike"] > p["vol_spike_max"]:
                    continue
                net = exit_move(c, e["i"], e["side"], stop, target, p["max_hold"], reverted) - e["cost"]
                out.append(("sector_reversion", vid_of("sector_reversion", p), net, e["tags"]))
        elif e["strategy"] == "etf_arb":
            g0 = e["gap"]
            members = weights.get(e["sym"]) or []

            def converged(j, g0=g0, members=members, etf=e["sym"]):
                g = gap_at(closes, members, etf, j)
                return g is not None and (g * g0 <= 0 or abs(g) <= abs(g0) / 3)
            for p in GRIDS["etf_arb"]:
                if p["thr_mult"] != e["level"] or p["persist"] != e["persist"]:
                    continue
                net = exit_move(c, e["i"], e["side"], 1.2 * abs(g0), 0.6 * abs(g0), p["max_hold"], converged) - e["cost"]
                out.append(("etf_arb", vid_of("etf_arb", p), net, e["tags"]))
        else:
            if e["stop"] < 3.0 * e["cost"]:
                continue
            for p in GRIDS["opening_range"]:
                if p["range_min"] != e["R"] or e["minute"] > p["last_entry"]:
                    continue
                net = exit_move(c, e["i"], e["side"], e["stop"], p["target_mult"] * e["stop"], 10 ** 6) - e["cost"]
                out.append(("opening_range", vid_of("opening_range", p), net, e["tags"]))
    return out


# ---------------------------------------------------------------------------------------------------- statistics
class Acc:
    """A running record of one (strategy, variant, segment, half): per trade AND per session (z180).
    add(x, day): trades of one session share a cluster; without a day every trade is its own cluster (= the old,
    per-trade arithmetic). The clustered view is what decides: a CR1 cluster-robust standard error of the mean,
        V = G / (G - 1) * sum_g (S_g - n_g * mean)^2 / N^2      (S_g, n_g = a session's sum and count)
    kept as running sums (sum S_g^2, sum n_g S_g, sum n_g^2), so no session list is held in memory."""
    __slots__ = ("n", "s", "ss", "g", "gs2", "gns", "gn2", "pos_g", "best_s", "best_n", "_day", "_dn", "_ds")

    def __init__(self):
        self.n, self.s, self.ss = 0, 0.0, 0.0
        self.g, self.gs2, self.gns, self.gn2, self.pos_g = 0, 0.0, 0.0, 0.0, 0
        self.best_s, self.best_n = None, 0
        self._day, self._dn, self._ds = None, 0, 0.0

    def _fold(self):
        if self._dn:
            n, s = self._dn, self._ds
            self.g += 1
            self.gs2 += s * s
            self.gns += n * s
            self.gn2 += n * n
            self.pos_g += s > 0
            if self.best_s is None or s > self.best_s:
                self.best_s, self.best_n = s, n
        self._dn, self._ds = 0, 0.0

    def add(self, x, day=None):
        if day is None or day != self._day:
            self._fold()
            self._day = day
        self.n += 1
        self.s += x
        self.ss += x * x
        self._dn += 1
        self._ds += x

    def mean(self):
        return self.s / self.n if self.n else 0.0

    def sd(self):
        if self.n < 2:
            return 0.0
        return math.sqrt(max(0.0, (self.ss - self.s * self.s / self.n) / (self.n - 1)))

    def t(self):
        """Per-trade t (every trade independent) - shown for reference, never decides."""
        sd = self.sd()
        return self.mean() / (sd / math.sqrt(self.n)) if sd > 0 else 0.0

    def _sessions(self):
        """(G, sum S_g^2, sum n_g S_g, sum n_g^2, positive sessions, best S_g, its n) - the open session included."""
        g, gs2, gns, gn2, pos, bs, bn = self.g, self.gs2, self.gns, self.gn2, self.pos_g, self.best_s, self.best_n
        if self._dn:
            n, s = self._dn, self._ds
            g, gs2, gns, gn2, pos = g + 1, gs2 + s * s, gns + n * s, gn2 + n * n, pos + (s > 0)
            if bs is None or s > bs:
                bs, bn = s, n
        return g, gs2, gns, gn2, pos, bs, bn

    def cluster(self):
        """Session-clustered evidence: {sessions, t, df, nEff, posSessions, dropBestBps}."""
        g, gs2, gns, gn2, pos, bs, bn = self._sessions()
        out = {"sessions": g, "t": 0.0, "df": max(0, g - 1), "nEff": None, "posSessions": pos, "dropBestBps": None}
        if bs is not None and self.n > bn:
            out["dropBestBps"] = (self.s - bs) / (self.n - bn)         # the mean without its best session
        if self.n < 2 or g < 2:
            return out
        mu = self.s / self.n
        num = gs2 - 2.0 * mu * gns + mu * mu * gn2                    # sum_g (S_g - n_g mu)^2
        v = (g / (g - 1.0)) * max(num, 0.0) / (self.n * self.n)
        if v <= 0:
            return out
        out["t"] = mu / math.sqrt(v)
        sd = self.sd()
        if sd > 0:                                                    # trades that behave like this many independent ones
            out["nEff"] = int(round(min(self.n, self.n * (sd * sd / self.n) / v)))
        return out

    def view(self):
        c = self.cluster()
        return {"n": self.n, "meanBps": round(self.mean(), 3), "t": round(self.t(), 2),
                "sessions": c["sessions"], "tCl": round(c["t"], 2), "df": c["df"], "nEff": c["nEff"],
                "posSessions": c["posSessions"], "dropBestBps": None if c["dropBestBps"] is None else round(c["dropBestBps"], 3)}


def _betacf(a, b, x, itmax=300, eps=3e-14):
    """Continued fraction for the incomplete beta function (Lentz)."""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
    h = d
    for m in range(1, itmax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1.0 + aa / c if abs(c) > 1e-300 else 1.0 + aa / 1e-300
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1.0 + aa / c if abs(c) > 1e-300 else 1.0 + aa / 1e-300
        de = d * c
        h *= de
        if abs(de - 1.0) < eps:
            break
    return h


def betainc(a, b, x):
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbt = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1.0 - x)
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(lbt) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lbt) * _betacf(b, a, 1.0 - x) / b


def p_one_sided(t, df=None):
    """P(T >= t): Student t with df degrees of freedom (z180: df = sessions - 1); the normal when df is None."""
    if df is None:
        return 0.5 * math.erfc(t / math.sqrt(2))
    if df < 1:
        return 1.0
    tail = 0.5 * betainc(df / 2.0, 0.5, df / (df + t * t))      # P(T >= |t|)
    return tail if t >= 0 else 1.0 - tail


def quad_of(segment):
    """'side:short|quad:Q3' -> 'Q3' (None when the segment isn't split by the quad)."""
    for part in str(segment).split("|"):
        if part.startswith("quad:"):
            return part.split(":", 1)[1]
    return None


def quad_varies(q, quad_days):
    """z180: a quad segment is a REGIME test only if both halves hold sessions inside AND outside that quad."""
    if not quad_days:
        return False, "no record of the quad by session"
    for half in ("train", "test"):
        days = quad_days.get(half) or {}
        tot, inside = sum(days.values()), days.get(q, 0)
        if inside < MIN_REGIME_SESSIONS or tot - inside < MIN_REGIME_SESSIONS:
            return False, f"the quad label is the calendar here: {q} in {inside} of {tot} {half} sessions"
    return True, ""


def benjamini_hochberg(ps):
    """q-values for a list of p-values (same order)."""
    m = len(ps)
    order = sorted(range(m), key=lambda k: ps[k])
    q = [1.0] * m
    prev = 1.0
    for rank in range(m, 0, -1):
        k = order[rank - 1]
        prev = min(prev, ps[k] * m / rank)
        q[k] = prev
    return q


def select(acc, strategies, quad_days=None, n_test=None):
    """Train: best variant per (strategy, segment) by its session-clustered t. Test: that variant only. BH across
    everything tested, on session-clustered p-values (z180). Every candidate says why it is or isn't live."""
    segs = {}
    for (st, vid, seg, split) in acc:
        segs.setdefault(st, set()).add(seg)
    cands = []
    for st in strategies:
        for seg in sorted(segs.get(st, ())):
            best, best_t = None, None
            for p in GRIDS[st]:
                vid = vid_of(st, p)
                tr = acc.get((st, vid, seg, "train"))
                if not tr or tr.n < MIN_TRAIN or tr.mean() <= 0:
                    continue
                cl = tr.cluster()
                if cl["sessions"] < MIN_TRAIN_SESSIONS or cl["t"] < MIN_TRAIN_T:
                    continue
                if best_t is None or cl["t"] > best_t:
                    best, best_t = vid, cl["t"]
            if not best:
                continue
            te = acc.get((st, best, seg, "test"))
            if not te or te.n < MIN_TEST:
                continue
            tv, tc = te.view(), te.cluster()
            cands.append({"strategy": st, "segment": seg, "variant": best, "train": acc[(st, best, seg, "train")].view(),
                          "test": tv, "p": round(p_one_sided(tc["t"], tc["df"]), 5), "pTrade": round(p_one_sided(te.t()), 5)})
    qs = benjamini_hochberg([c["p"] for c in cands]) if cands else []
    for c, q in zip(cands, qs):
        c["q"] = round(q, 4)
        te, why = c["test"], []
        if q > FDR:
            why.append(f"q {c['q']:.2f} > {FDR:.2f} - could be luck among {len(cands)} tested")
        if te["meanBps"] < MIN_EXP_BPS:
            why.append(f"{te['meanBps']:+.1f} bps a trade out of sample (needs >= {MIN_EXP_BPS})")
        if te["sessions"] < MIN_TEST_SESSIONS:
            why.append(f"traded on {te['sessions']}" + (f" of {n_test}" if n_test else "") + f" test sessions (needs >= {MIN_TEST_SESSIONS})")
        if te["dropBestBps"] is None or te["dropBestBps"] <= 0:
            why.append("one session carries it - without its best day it loses")
        qd = quad_of(c["segment"])
        c["regimeDependent"] = bool(qd)
        if qd:
            ok, msg = quad_varies(qd, quad_days)
            if not ok:
                why.append(msg)
        c["approved"] = not why
        c["reasons"] = why
    return cands


def build_models(acc, strategies, days_train, days_test, cfg=CFG, quad_days=None):
    cands = select(acc, strategies, quad_days, n_test=len(days_test) if days_test else None)
    by_vid = {vid_of(st, p): p for st in strategies for p in GRIDS[st]}
    out = {"asOf": datetime.now(timezone.utc).isoformat(timespec="seconds"), "labVersion": LAB_VERSION,
           "inference": "session-clustered (CR1 standard error, Student t with sessions - 1 df); BH false-discovery control on those p-values",
           "sessions": {"train": [days_train[0], days_train[-1], len(days_train)] if days_train else [],
                        "test": [days_test[0], days_test[-1], len(days_test)] if days_test else []},
           "rules": {"minTrainTrades": MIN_TRAIN, "minTestTrades": MIN_TEST, "fdr": FDR, "minTestExpectancyBps": MIN_EXP_BPS,
                     "minTrainSessions": MIN_TRAIN_SESSIONS, "minTestSessions": MIN_TEST_SESSIONS, "minTrainTCl": MIN_TRAIN_T,
                     "minRegimeSessions": MIN_REGIME_SESSIONS, "dropBestSession": "out-of-sample mean without the best session > 0",
                     "quadSegmentsLive": "only while regime certainty is dialed in and the nowcast agrees with the official quad"},
           "quadSessions": quad_days or {},
           "costs": {"halfSpreadBps": "scaled by liquidity (2 bps at $200M a day)", "slippageBps": cfg.slippage_bps, "feeBpsSell": cfg.fee_bps_sell},
           "strategies": {}}
    for st in strategies:
        mine = [c for c in cands if c["strategy"] == st]
        live = [dict(c, params=by_vid[c["variant"]]) for c in mine if c["approved"]]
        overall = {}
        for p in GRIDS[st]:
            vid = vid_of(st, p)
            tr, te = acc.get((st, vid, "all", "train")), acc.get((st, vid, "all", "test"))
            if tr or te:
                overall[vid] = {"train": tr.view() if tr else None, "test": te.view() if te else None}
        out["strategies"][st] = {"variants": {c["variant"]: by_vid[c["variant"]] for c in live},
                                 "live": [{"segment": c["segment"], "variant": c["variant"], "params": c["params"], "train": c["train"], "test": c["test"],
                                           "p": c["p"], "q": c["q"], "regimeDependent": c["regimeDependent"]} for c in live],
                                 "report": [dict(c, params=by_vid[c["variant"]]) for c in sorted(mine, key=lambda c: (-c["approved"], c["q"], -c["train"]["tCl"]))[:25]],
                                 "tested": len(mine), "overall": overall}
    return out


# ---------------------------------------------------------------------------------------------------- the run
def run(n_days=None, strategies=("sector_reversion", "etf_arb", "opening_range")):
    from .universe import load
    key, secret = os.environ.get("ALPACA_KEY_ID", "").strip(), os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if not (key and secret):
        log("ALPACA_KEY_ID / ALPACA_SECRET_KEY not set - cannot fetch history")
        return 1
    uni = load()
    from .macro import history_rows, official_on
    qrows = history_rows()                              # z177: segment by the official quad as known each day
    days = sessions(int(n_days or os.environ.get("QUANT_LAB_DAYS", "") or DEFAULT_LAB_DAYS))
    cut = max(1, round(len(days) * 2 / 3))
    train, test = {d for d, _ in days[:cut]}, {d for d, _ in days[cut:]}
    acc, bad, t0 = {}, {}, time.time()
    quad_days = {"train": {}, "test": {}}               # z180: sessions per official quad label, per half
    for d, close in days:
        bars = fetch_day(watch_symbols(uni, CFG.min_dollar_volume), d, close, key, secret, bad)   # z178: tradable names + references
        if not bars:
            log(f"{d}: no bars - skipped")
            continue
        events, closes, rets = session_events(uni, bars, d, close)
        qd = official_on(d, qrows)
        if qd:
            for e in events:
                e["tags"]["quad"] = f"Q{qd}"
        scored = score_events(events, closes, rets, uni)
        split = "train" if d in train else "test"
        if qd:
            quad_days[split][f"Q{qd}"] = quad_days[split].get(f"Q{qd}", 0) + 1
        day_key = d.isoformat()
        for st, vid, net, tags in scored:
            for seg in keys_for(tags):
                acc.setdefault((st, vid, seg, split), Acc()).add(net, day_key)      # z180: clustered by session
        log(f"{d} ({split}, official quad then: Q{qd}): {len(bars)} symbols · {len(events)} candidate trades · {len(scored)} variant scores · {time.time() - t0:.0f} s")
    models = build_models(acc, list(strategies), [d.isoformat() for d, _ in days[:cut]], [d.isoformat() for d, _ in days[cut:]],
                          quad_days=quad_days)
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "lab").mkdir(exist_ok=True)
    (DATA / "models.json").write_text(json.dumps(models, indent=1))
    (DATA / "lab" / "latest.json").write_text(json.dumps(models, indent=1))
    for st, m in models["strategies"].items():
        log(f"{st}: {m['tested']} segments tested · {len(m['live'])} approved for live")
        for a in m["live"][:8]:
            log(f"      LIVE {a['segment']} · {a['variant']} · test {a['test']['n']} trades on {a['test']['sessions']} sessions "
                f"{a['test']['meanBps']:+.2f} bps · clustered t {a['test']['tCl']} (per-trade {a['test']['t']}) · q {a['q']}"
                + (" · REGIME-GATED (live only while the regime is dialed in)" if a.get("regimeDependent") else ""))
        for c in [c for c in m["report"] if not c.get("approved")][:5]:
            log(f"      not live: {c['segment']} · " + "; ".join(c.get("reasons") or []))
    if bad:
        log(f"Alpaca refused {len(bad)} symbol(s): " + ", ".join(list(bad)[:12]))
    return 0


if __name__ == "__main__":
    sys.exit(run())
