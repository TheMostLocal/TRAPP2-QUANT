# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-QUANT
#  >>> FILE PATH:   quant/strategies/etf_arb.py
#  ETF ARBITRAGE (the Jane Street family): an ETF is a basket. When its price over the last 5 minutes moves
#  differently from what its holdings did - weighted by the fund's own daily holdings file - the ETF tends to
#  catch up to the basket (that is what market makers' creation / redemption arbitrage enforces). The gap must
#  be larger than 2.5x the round-trip cost and persist for two minutes in a row; we trade the ETF toward its
#  basket and get out when the gap has closed to a third, at the stop, or after 10 minutes. Needs >= 60 % of the
#  fund's weight priced live. (A true arbitrageur also trades the basket the other way; one leg keeps the paper
#  book simple and still measures the effect.)
# ============================================================
import math

from .base import Signal, Strategy, round_trip_cost_bps
from ..segments import tags_for


class EtfArb(Strategy):
    name = "etf_arb"
    K, MAX_HOLD, MIN_COVER = 5, 10, 0.60                                     # kept for reference: the defaults
    DEFAULTS = {"K": 5, "max_hold": 10, "min_cover": 0.60, "thr_mult": 2.5, "min_thr": 6.0, "persist": True,
                "target_frac": 0.6, "stop_mult": 1.2, "converge_frac": 1 / 3}

    def __init__(self, params=None, variant="default"):
        super().__init__(params, variant)
        self.prev = {}          # etf -> gap of the previous minute

    def gap(self, book, etf, members):
        e = book.s.get(etf)
        K = self.p["K"]
        if not e or len(e.bars) < K + 1:
            return None
        ce = e.closes(K + 1)
        if ce[0] <= 0:
            return None
        r_etf = math.log(ce[-1] / ce[0])
        tot = cov = 0.0
        for sym, w in members:
            s = book.s.get(sym)
            if not s or len(s.bars) < K + 1:
                continue
            c = s.closes(K + 1)
            if c[0] <= 0 or c[-1] <= 0 or any(b[6] for b in list(s.bars)[-K:]):     # skip carried (stale) bars
                continue
            tot += w * math.log(c[-1] / c[0])
            cov += w
        if cov < self.p["min_cover"]:
            return None
        return (tot / cov - r_etf) * 1e4, cov

    def on_minute(self, book, ctx):
        out = []
        for etf, members in ctx.etf_weights.items():
            g = self.gap(book, etf, members)
            prev = self.prev.get(etf)
            self.prev[etf] = g[0] if g else None
            if not g or etf in ctx.open_symbols:
                continue
            gp, cov = g
            st = book.s[etf]
            thr = max(self.p["min_thr"], self.p["thr_mult"] * round_trip_cost_bps(st, ctx.cfg))
            if abs(gp) < thr:
                continue
            if self.p["persist"] and (prev is None or prev * gp <= 0 or abs(prev) < 0.6 * thr):
                continue
            side = "long" if gp > 0 else "short"
            out.append(Signal(etf, side, self.name, round(self.p["stop_mult"] * abs(gp), 2), round(self.p["target_frac"] * abs(gp), 2), int(self.p["max_hold"]),
                              score=abs(gp) / thr, reason=f"basket {gp:+.1f} bps vs {etf} over {self.p['K']} min ({cov:.0%} of the fund priced)",
                              meta={"gap": round(gp, 2)}, variant=self.variant, tags=tags_for(etf, side, ctx, sector=etf)))
        return out

    def should_exit(self, pos, book, ctx):
        g = self.gap(book, pos.symbol, ctx.etf_weights.get(pos.symbol, []))
        if not g:
            return None
        g0 = pos.meta.get("gap") or 0
        if g0 and (g[0] * g0 <= 0 or abs(g[0]) <= abs(g0) * self.p["converge_frac"]):
            return f"converged (gap {g[0]:+.1f} bps)"
        return None
