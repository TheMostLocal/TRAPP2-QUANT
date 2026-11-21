# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-QUANT
#  >>> FILE PATH:   quant/tape.py
#  z202 - THE MARKET TAPE for the quant book. Owner 11-21: "make sure this new trends strategy is relevant and being
#  considered by the BOT and QUANT. This is how we can improve these trading schemas without faster data for now."
#  TRAPP2-ANALYTICS' Today's Trends (pipeline/trends.py, z201) reads the S&P 500's run, drawdown, trend, the VIX,
#  distribution days, breadth and credit into one sell-off read. The quant book takes the same rules in two forms,
#  each computed the same way live and in the lab's past sessions (one definition, no look-ahead):
#    1. "mkt" - the read AS KNOWN AT THE OPEN: completed sessions only (through the prior close), from TRAPP2-1's
#       daily histories (^GSPC with volume, ^VIX, HYG). Six of trends.py's seven criteria - breadth needs every
#       member's history, so it is recorded live (below), not used here:
#         run           4+ straight lower S&P 500 closes through the prior close
#         drawdown      the prior close 5 %+ below its 52-week (252-session) high
#         trend         the prior close below its 50-day average
#         fear          VIX 25+ at the prior close, or +30 % over 5 sessions
#         distribution  5+ distribution days (a 0.2 %+ decline on higher volume) in the last 25 sessions
#         credit        HYG -1 % or worse over 5 sessions
#       0-1 on: calm · 2: pressure · 3+: selloff (three states, so each holds enough sessions for the lab to test).
#    2. "spx" - the S&P 500 TODAY, at the signal's minute: SPY against its prior close - dn (-0.75 % or worse), flat,
#       up (+0.75 % or better). Real time from the engine's own SPY quotes; in the lab from SPY's 1-minute bars.
#  Evidence first, as with the quad (z177): both TAG every trade and are lab segment dimensions; a strategy trades
#  live in a market state only where the lab approved it there, and a "mkt:" segment counts only where the state
#  varied inside both halves (else "selloff" just means "the last two weeks"). Nothing is tilted by assumption.
#  The live Today's Trends doc (TRAPP2-ANALYTICS data/trends/latest.json - the full 7-criterion read, today's
#  provisional close, breadth) is RECORDED on every trade ("mktNow"), never segmented: the lab can't know it for
#  past sessions.
# ============================================================
import json
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

RAW = "https://raw.githubusercontent.com/TheMostLocal"
HIST = RAW + "/TRAPP2-1/main/data/history/{}.json"
TRENDS_URL = RAW + "/TRAPP2-ANALYTICS/main/data/trends/latest.json"
SPX, VIX, HYG, SPY = "^GSPC", "^VIX", "HYG", "SPY"
STATES = ((0, "calm"), (2, "pressure"), (3, "selloff"))
DAY_BAND = 0.0075                   # +-0.75 % on the day = "flat"
MAX_GAP_DAYS = 6                    # the history must reach within 6 calendar days of the session, or no state


def _get(url, timeout=30):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "valuatio-quant"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _num(v):
    try:
        x = float(v)
        return x if x == x and x not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def series(rows):
    """History rows [{date, close, volume}] -> (dates, closes, volumes): sorted, one per date, valid closes only."""
    by = {}
    for r in (rows if isinstance(rows, list) else []):            # anything but a list of rows (an error page) = nothing
        if not isinstance(r, dict):
            continue
        d, c = str(r.get("date") or "")[:10], _num(r.get("close"))
        if len(d) == 10 and c and c > 0:
            by[d] = (c, _num(r.get("volume")))
    ds = sorted(by)
    return ds, [by[d][0] for d in ds], [by[d][1] for d in ds]


def load_histories(fetch=None):
    """{'spx', 'vix', 'hyg', 'spy'} -> (dates, closes, volumes) from TRAPP2-1 (one request each, ~300 KB)."""
    fetch = fetch or _get
    out = {}
    for k, sym in (("spx", SPX), ("vix", VIX), ("hyg", HYG), ("spy", SPY)):
        out[k] = series(fetch(HIST.format(urllib.parse.quote(sym, safe=""))))
    return out


def _upto(ser, day):
    """The part of a series strictly before `day` (completed sessions as known at that session's open)."""
    ds, cs, vs = ser or ([], [], [])
    k = 0
    for i, d in enumerate(ds):
        if d < day:
            k = i + 1
        else:
            break
    return ds[:k], cs[:k], vs[:k]


def state_on(day, H):
    """The market read as known at the open of `day` (ISO date or date). -> dict or None (too little history /
    the history doesn't reach the session)."""
    day = day.isoformat() if isinstance(day, date) else str(day)[:10]
    ds, cs, vs = _upto((H or {}).get("spx"), day)
    if len(cs) < 60:
        return None
    try:
        if (date.fromisoformat(day) - date.fromisoformat(ds[-1])).days > MAX_GAP_DAYS:
            return None
    except ValueError:
        return None
    crit = {}
    run = 0
    for i in range(len(cs) - 1, 0, -1):                     # straight lower closes through the prior close
        if cs[i] < cs[i - 1]:
            run += 1
        else:
            break
    up = 0
    if not run:
        for i in range(len(cs) - 1, 0, -1):
            if cs[i] > cs[i - 1]:
                up += 1
            else:
                break
    crit["run"] = run >= 4
    hi = max(cs[-252:])
    dd = cs[-1] / hi - 1
    crit["drawdown"] = dd <= -0.05
    m50 = sum(cs[-50:]) / 50
    crit["trend"] = cs[-1] < m50
    vd, vc, _ = _upto(H.get("vix"), day)
    vix = vix5 = None
    if vc and vd[-1] == ds[-1]:
        vix = vc[-1]
        vix5 = (vc[-1] / vc[-6] - 1) if len(vc) > 6 and vc[-6] else None
        crit["fear"] = vix >= 25 or (vix5 is not None and vix5 >= 0.30)
    dist = None
    if len(vs) >= 26 and any(vs[-25:]):                     # as trends.py: sessions without a volume are skipped
        dist = sum(1 for k in range(len(cs) - 25, len(cs)) if vs[k] and vs[k - 1] and cs[k] / cs[k - 1] - 1 <= -0.002 and vs[k] > vs[k - 1])
        crit["distribution"] = dist >= 5
    hd, hc, _ = _upto(H.get("hyg"), day)
    hyg5 = None
    if len(hc) > 6 and hd[-1] == ds[-1]:
        hyg5 = hc[-1] / hc[-6] - 1
        crit["credit"] = hyg5 <= -0.01
    on = sum(1 for v in crit.values() if v)
    state = [name for lo, name in STATES if on >= lo][-1]
    return {"state": state, "on": on, "of": len(crit), "asOf": ds[-1], "run": -run if run else up,
            "dd": round(dd * 100, 2), "vs50": round((cs[-1] / m50 - 1) * 100, 2), "vix": round(vix, 2) if vix else None,
            "vix5": round(vix5 * 100, 1) if vix5 is not None else None, "dist": dist,
            "hyg5": round(hyg5 * 100, 2) if hyg5 is not None else None,
            "criteria": {k: bool(v) for k, v in crit.items()}}


def prior_close(ser, day):
    """SPY's close on the last session before `day` (None when the history doesn't reach it)."""
    day = day.isoformat() if isinstance(day, date) else str(day)[:10]
    ds, cs, _ = _upto(ser, day)
    if not cs:
        return None
    try:
        if (date.fromisoformat(day) - date.fromisoformat(ds[-1])).days > MAX_GAP_DAYS:
            return None
    except ValueError:
        return None
    return cs[-1]


def day_bucket(move):
    """SPY's move from its prior close (a fraction) -> 'dn' / 'flat' / 'up' (None when unknown)."""
    if move is None:
        return None
    return "dn" if move <= -DAY_BAND else "up" if move >= DAY_BAND else "flat"


def trends_now(doc=None, fetch=None):
    """The live Today's Trends read (recorded on trades, not segmented) - compact, or None."""
    d = doc if doc is not None else (fetch or _get)(TRENDS_URL)
    if not isinstance(d, dict) or not isinstance(d.get("read"), dict):
        return None
    r, spx = d["read"], ((d.get("indices") or {}).get(SPX) or {})
    s = spx.get("streak") or {}
    run = (-1 if s.get("dir") == "down" else 1) * int(s.get("n") or 0) if s.get("dir") else 0
    return {"label": r.get("label"), "state": r.get("state"), "overlay": r.get("overlay"), "on": r.get("on"), "of": r.get("of"),
            "date": d.get("date"), "etTime": d.get("etTime"), "session": d.get("state"), "spxChg": spx.get("chg"),
            "run": run, "runThrough": s.get("through"), "pctUp": (d.get("internals") or {}).get("pctUp")}


def current(today=None, H=None, fetch=None):
    """Everything the engine needs at the open: {'state', ..., 'spyPrev', 'now'} (state None when unknown)."""
    today = today or date.today()
    H = H if H is not None else load_histories(fetch)
    st = state_on(today, H) or {}
    out = dict(st, spyPrev=prior_close(H.get("spy"), today))
    out["now"] = trends_now(fetch=fetch)
    return out


def describe(m):
    """One log line."""
    if not m or not m.get("state"):
        return "market tape: no state (the history doesn't reach this session)"
    c = m.get("criteria") or {}
    on = ", ".join(k for k, v in c.items() if v) or "none"
    now = m.get("now") or {}
    return (f"market tape (as of the {m.get('asOf')} close): {m['state'].upper()} - {m.get('on')} of {m.get('of')} on ({on}) · "
            f"S&P run {m.get('run'):+d} · {m.get('dd'):+.1f}% from the 52-week high · VIX {m.get('vix')}"
            + (f" · today's trends: {now.get('label')} ({now.get('on')} of {now.get('of')}, {now.get('date')} {now.get('etTime') or ''})" if now else "")
            + (f" · SPY prior close {m['spyPrev']:.2f}" if m.get("spyPrev") else " · no SPY prior close (no 'spx' tag today)"))
