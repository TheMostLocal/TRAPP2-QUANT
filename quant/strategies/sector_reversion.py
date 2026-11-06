# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-QUANT
#  >>> FILE PATH:   quant/strategies/sector_reversion.py
#  STATISTICAL ARBITRAGE (the Medallion family): a stock that runs away from its own sector over a few minutes,
#  with no move in the sector to explain it, tends to come part of the way back. Each minute, for every stock
#  with a sector ETF: beta to the ETF from the last 60 one-minute returns, the residual return (stock - beta x
#  ETF), and the z-score of the last 10 minutes' summed residual against the residual noise. |z| >= 2.5 -> fade
#  it; out when the residual has mostly closed (|z| <= 0.5), at the stop, the target or after 30 minutes. A
#  trade is only taken if the expected reversion clears the round-trip cost twice over.
# ============================================================
import math

from .base import Signal, Strategy, beta, round_trip_cost_bps, stdev
from ..segments import tags_for


class SectorReversion(Strategy):
    name = "sector_reversion"
    W, L, Z_IN, Z_OUT, MAX_HOLD, MIN_BARS = 60, 10, 2.5, 0.5, 30, 45       # kept for reference: the defaults
    DEFAULTS = {"W": 60, "L": 10, "z_in": 2.5, "z_out": 0.5, "max_hold": 30, "stop_mult": 2.0, "target_frac": 0.6,
                "cost_mult": 2.0, "vol_spike_max": None, "min_bars": 45}

    def vol_spike(self, book, sym):
        """Volume of the last L minutes against the average L-minute volume before it - a residual move on a volume
        spike is news more often than noise, and news doesn't revert."""
        st = book.s.get(sym)
        if not st:
            return None
        b = list(st.bars)
        L = self.p["L"]
        if len(b) < 4 * L:
            return None
        recent = sum(x[5] for x in b[-L:])
        before = sum(x[5] for x in b[-4 * L:-L]) / 3
        return recent / before if before > 0 else None

    def _z(self, book, sym, etf):
        s, e = book.s.get(sym), book.s.get(etf)
        if not s or not e:
            return None
        rs, re_ = s.returns(self.p["W"]), e.returns(self.p["W"])
        n = min(len(rs), len(re_))
        if n < self.p["min_bars"]:
            return None
        rs, re_ = rs[-n:], re_[-n:]
        b = beta(rs, re_)
        res = [a - b * c for a, c in zip(rs, re_)]
        sd = stdev(res)
        if sd <= 0:
            return None
        cum = sum(res[-self.p["L"]:])
        return cum / (sd * math.sqrt(self.p["L"])), cum, sd, b

    def on_minute(self, book, ctx):
        out = []
        for sym, etf in ctx.sector_etf.items():
            if sym in ctx.open_symbols:
                continue
            z = self._z(book, sym, etf)
            if not z:
                continue
            zz, cum, sd, b = z
            if abs(zz) < self.p["z_in"]:
                continue
            if self.p["vol_spike_max"]:
                vs = self.vol_spike(book, sym)
                if vs is not None and vs > self.p["vol_spike_max"]:
                    continue
            st = book.s[sym]
            cost = round_trip_cost_bps(st, ctx.cfg)
            target = abs(cum) * 1e4 * self.p["target_frac"]   # expect part of the residual back
            if target < self.p["cost_mult"] * cost:
                continue
            stop = max(12.0, self.p["stop_mult"] * sd * 1e4 * math.sqrt(self.p["L"]))
            side = "short" if zz > 0 else "long"
            out.append(Signal(sym, side, self.name, round(stop, 2), round(target, 2), int(self.p["max_hold"]),
                              score=abs(zz), reason=f"residual z {zz:+.2f} vs {etf} (beta {b:.2f})", meta={"etf": etf, "z": round(zz, 3)},
                              variant=self.variant, tags=tags_for(sym, side, ctx)))
        return out

    def should_exit(self, pos, book, ctx):
        z = self._z(book, pos.symbol, pos.meta.get("etf"))
        if not z:
            return None
        zz = z[0]
        if (pos.side == "long" and zz >= -self.p["z_out"]) or (pos.side == "short" and zz <= self.p["z_out"]):
            return f"reverted (z {zz:+.2f})"
        return None
