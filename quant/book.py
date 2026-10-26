# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-QUANT
#  >>> FILE PATH:   quant/book.py
#  The live picture of every symbol: last trade, best bid / ask (mid, spread in bps), and 1-minute bars of the
#  MID price (quotes are far denser than trades on IEX, and a mid is not bounced by the spread). A bar closes when
#  the clock passes the minute; a symbol with no update inside a minute carries its last mid forward (flagged).
# ============================================================
import math
from collections import deque


class SymbolState:
    __slots__ = ("sym", "last", "last_ts", "bid", "ask", "bsz", "asz", "q_ts", "bars", "_cur", "vol_min", "upd")

    def __init__(self, sym, keep=240):
        self.sym = sym
        self.last = self.bid = self.ask = None
        self.bsz = self.asz = 0
        self.last_ts = self.q_ts = 0.0
        self.bars = deque(maxlen=keep)          # completed minutes: (minute_epoch, o, h, l, c, vol, carried)
        self._cur = None                        # [minute_epoch, o, h, l, c, vol]
        self.vol_min = 0
        self.upd = 0.0                          # last time anything arrived (epoch seconds)

    # ---- events --------------------------------------------------------------------------------
    def on_trade(self, px, size, ts):
        if not px or px <= 0:
            return
        self.last, self.last_ts, self.upd = float(px), ts, max(self.upd, ts)
        self._touch(self.mid() or px, ts, size)

    def on_quote(self, bp, ap, bs, as_, ts):
        if not bp or not ap or bp <= 0 or ap <= 0 or ap < bp:
            return                              # one-sided or crossed quote: ignore
        self.bid, self.ask, self.bsz, self.asz, self.q_ts = float(bp), float(ap), bs or 0, as_ or 0, ts
        self.upd = max(self.upd, ts)
        self._touch(self.mid(), ts, 0)

    def _touch(self, px, ts, size):
        m = int(ts // 60) * 60
        c = self._cur
        if c is None or m > c[0]:
            if c is not None:
                self._close(m)
            self._cur = [m, px, px, px, px, size or 0]
        elif m == c[0]:
            c[2], c[3], c[4] = max(c[2], px), min(c[3], px), px
            c[5] += size or 0

    def _close(self, next_minute):
        c = self._cur
        self.bars.append((c[0], c[1], c[2], c[3], c[4], c[5], False))
        # minutes with no update at all between the closed bar and the next one: carry the close forward
        gap = int((next_minute - c[0]) // 60) - 1
        for k in range(1, min(gap, 30) + 1):
            self.bars.append((c[0] + 60 * k, c[4], c[4], c[4], c[4], 0, True))
        self._cur = None

    def roll(self, now_epoch):
        """Close the current bar if the clock has passed its minute."""
        if self._cur is not None and int(now_epoch // 60) * 60 > self._cur[0]:
            self._close(int(now_epoch // 60) * 60)

    # ---- views ---------------------------------------------------------------------------------
    def mid(self):
        return (self.bid + self.ask) / 2 if (self.bid and self.ask) else self.last

    def spread_bps(self):
        if not (self.bid and self.ask):
            return None
        m = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / m * 1e4

    def closes(self, n):
        return [b[4] for b in list(self.bars)[-n:]]

    def returns(self, n):
        c = self.closes(n + 1)
        return [math.log(c[i] / c[i - 1]) for i in range(1, len(c)) if c[i - 1] > 0 and c[i] > 0]

    def fresh(self, now_epoch, stale_seconds):
        return self.upd > 0 and now_epoch - self.upd <= stale_seconds


class Book:
    def __init__(self, symbols=(), keep=240):
        self.s = {}
        self.keep = keep
        for x in symbols:
            self.get(x)

    def get(self, sym):
        st = self.s.get(sym)
        if st is None:
            st = self.s[sym] = SymbolState(sym, self.keep)
        return st

    def roll(self, now_epoch):
        for st in self.s.values():
            st.roll(now_epoch)

    # backtests: a whole minute bar at once (the bar's close is the mid; spread from the cost model)
    def on_bar(self, sym, minute_epoch, o, h, l, c, v, half_spread_bps):
        st = self.get(sym)
        st.bars.append((minute_epoch, o, h, l, c, v, False))
        hs = c * half_spread_bps / 1e4
        st.bid, st.ask, st.last, st.q_ts = c - hs, c + hs, c, minute_epoch + 59
        st.upd = minute_epoch + 59
