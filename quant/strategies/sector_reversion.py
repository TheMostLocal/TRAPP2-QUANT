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


class SectorReversion(Strategy):
    name = "sector_reversion"
    W, L, Z_IN, Z_OUT, MAX_HOLD, MIN_BARS = 60, 10, 2.5, 0.5, 30, 45

    def _z(self, book, sym, etf):
        s, e = book.s.get(sym), book.s.get(etf)
        if not s or not e:
            return None
        rs, re_ = s.returns(self.W), e.returns(self.W)
        n = min(len(rs), len(re_))
        if n < self.MIN_BARS:
            return None
        rs, re_ = rs[-n:], re_[-n:]
        b = beta(rs, re_)
        res = [a - b * c for a, c in zip(rs, re_)]
        sd = stdev(res)
        if sd <= 0:
            return None
        cum = sum(res[-self.L:])
        return cum / (sd * math.sqrt(self.L)), cum, sd, b

    def on_minute(self, book, ctx):
        out = []
        for sym, etf in ctx.sector_etf.items():
            if sym in ctx.open_symbols:
                continue
            z = self._z(book, sym, etf)
            if not z:
                continue
            zz, cum, sd, b = z
            if abs(zz) < self.Z_IN:
                continue
            st = book.s[sym]
            cost = round_trip_cost_bps(st, ctx.cfg)
            target = abs(cum) * 1e4 * 0.6                     # expect ~60 % of the residual back
            if target < 2 * cost:
                continue
            stop = max(12.0, 2.0 * sd * 1e4 * math.sqrt(self.L))
            out.append(Signal(sym, "short" if zz > 0 else "long", self.name, round(stop, 2), round(target, 2), self.MAX_HOLD,
                              score=abs(zz), reason=f"residual z {zz:+.2f} vs {etf} (beta {b:.2f})", meta={"etf": etf, "z": round(zz, 3)}))
        return out

    def should_exit(self, pos, book, ctx):
        z = self._z(book, pos.symbol, pos.meta.get("etf"))
        if not z:
            return None
        zz = z[0]
        if (pos.side == "long" and zz >= -self.Z_OUT) or (pos.side == "short" and zz <= self.Z_OUT):
            return f"reverted (z {zz:+.2f})"
        return None
