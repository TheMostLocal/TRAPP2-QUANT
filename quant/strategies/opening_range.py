# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-QUANT
#  >>> FILE PATH:   quant/strategies/opening_range.py
#  INTRADAY MOMENTUM (breadth across the day): the first 15 minutes' range is the market's opening argument; a
#  clean break of it, before 1130 ET, tends to carry. One trade per symbol per day; stop at the middle of the range,
#  target twice the risk, flat by the close. Wins less often than it loses - it pays by size of wins - which is why
#  the book is judged on expectancy, not hit rate alone.
# ============================================================
from .base import Signal, Strategy, round_trip_cost_bps


class OpeningRange(Strategy):
    name = "opening_range"
    RANGE_MIN, LAST_ENTRY, MIN_RANGE_BPS, MAX_RANGE_BPS = 15, 11 * 60 + 30, 25, 300

    def on_minute(self, book, ctx):
        if not (9 * 60 + 30 + self.RANGE_MIN <= ctx.minute <= self.LAST_ENTRY):
            return []
        out = []
        r0, r1 = ctx.day_open_epoch, ctx.day_open_epoch + self.RANGE_MIN * 60
        for sym in ctx.momentum_universe:
            if sym in ctx.open_symbols or sym in ctx.traded_today.get(self.name, set()):
                continue
            st = book.s.get(sym)
            if not st or len(st.bars) < self.RANGE_MIN + 2:
                continue
            rng = [b for b in st.bars if r0 <= b[0] < r1 and not b[6]]
            if len(rng) < self.RANGE_MIN - 2:
                continue
            hi, lo = max(b[2] for b in rng), min(b[3] for b in rng)
            mid = (hi + lo) / 2
            if lo <= 0:
                continue
            width = (hi - lo) / mid * 1e4
            if not (self.MIN_RANGE_BPS <= width <= self.MAX_RANGE_BPS):
                continue
            last, prev = st.bars[-1][4], st.bars[-2][4]
            cost = round_trip_cost_bps(st, ctx.cfg)
            if last > hi * 1.0002 and prev <= hi:
                stop = (last - mid) / last * 1e4
                side = "long"
            elif last < lo * 0.9998 and prev >= lo:
                stop = (mid - last) / last * 1e4
                side = "short"
            else:
                continue
            if stop < 3 * cost:
                continue
            out.append(Signal(sym, side, self.name, round(stop, 2), round(2 * stop, 2), ctx.cfg.flatten_at - ctx.minute,
                              score=width / 100, reason=f"broke the opening range {lo:.2f}-{hi:.2f} ({width:.0f} bps wide)"))
        return out
