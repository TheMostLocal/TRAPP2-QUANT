# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-QUANT
#  >>> FILE PATH:   quant/strategies/base.py
# ============================================================
import math
from dataclasses import dataclass, field


@dataclass
class Signal:
    symbol: str
    side: str                 # "long" | "short"
    strategy: str
    stop_bps: float           # adverse move from the entry fill that closes the trade
    target_bps: float         # favourable move that closes it
    max_hold_min: int         # time stop
    score: float = 0.0        # strength (|z|, gap / threshold ...) - ranks signals when slots are short
    reason: str = ""
    meta: dict = field(default_factory=dict)


class Strategy:
    name = "base"
    horizon = "intraday"

    def on_minute(self, book, ctx):
        return []

    def should_exit(self, pos, book, ctx):
        return None


def mean(x):
    return sum(x) / len(x) if x else 0.0


def stdev(x):
    if len(x) < 2:
        return 0.0
    m = mean(x)
    return math.sqrt(sum((v - m) ** 2 for v in x) / (len(x) - 1))


def beta(y, x, lo=0.3, hi=2.0):
    mx, my = mean(x), mean(y)
    vx = sum((a - mx) ** 2 for a in x)
    if vx <= 0:
        return 1.0
    b = sum((a - mx) * (c - my) for a, c in zip(x, y)) / vx
    return min(hi, max(lo, b))


def round_trip_cost_bps(st, cfg):
    """What a round trip costs at today's spread: both half spreads + slippage both sides + the sell fee."""
    sp = st.spread_bps() or 2 * cfg.backtest_half_spread_bps
    return sp + 2 * cfg.slippage_bps + cfg.fee_bps_sell
