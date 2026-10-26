# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-QUANT
#  >>> FILE PATH:   quant/risk.py
#  Sizing and the limits every entry passes: the loss at the stop is a fixed share of capital (0.15 %), capped
#  at 5 % of capital per position; at most 25 positions and 150 % gross; nothing new after -1.5 % on the day,
#  outside 0940-1540 ET, on a stale symbol, a spread wider than 8 bps or a price under $5.
# ============================================================
import math
from dataclasses import dataclass, field


@dataclass
class Position:
    id: str
    strategy: str
    symbol: str
    side: str
    qty: int
    entry_px: float
    entry_mid: float
    entry_spread_bps: float
    entry_ts: float
    stop_bps: float
    target_bps: float
    max_hold_min: int
    mode: str = "live"                      # "live" (sent to the broker) | "shadow" (simulated, strategy not yet gated)
    reason: str = ""
    meta: dict = field(default_factory=dict)

    def notional(self):
        return abs(self.qty) * self.entry_px

    def move_bps(self, px):
        if not px or not self.entry_px:
            return 0.0
        r = (px / self.entry_px - 1) * 1e4
        return r if self.side == "long" else -r


class Risk:
    def __init__(self, cfg):
        self.cfg = cfg

    def size(self, sig, price):
        c = self.cfg
        if not price or price <= 0 or sig.stop_bps <= 0:
            return 0
        by_risk = c.capital * c.risk_per_trade / (price * sig.stop_bps / 1e4)
        by_cap = c.capital * c.max_position_pct / price
        return int(math.floor(min(by_risk, by_cap)))

    def check(self, sig, st, minute, now, positions, day_pnl):
        """(ok, reason) for a new entry."""
        c = self.cfg
        if day_pnl <= -c.daily_loss_halt_pct * c.capital:
            return False, "daily loss halt"
        if not (c.no_entry_before <= minute <= c.no_entry_after):
            return False, "outside the entry window"
        if any(p.symbol == sig.symbol for p in positions):
            return False, "already holding it"
        live = [p for p in positions if p.mode == "live"]
        if len(live) >= c.max_positions:
            return False, "position limit"
        if not st or not st.fresh(now, c.stale_seconds):
            return False, "stale quote"
        px = st.mid()
        if not px or px < c.min_price:
            return False, "price under the minimum"
        sp = st.spread_bps()
        if sp is not None and sp > c.max_spread_bps:
            return False, f"spread {sp:.1f} bps"
        q = self.size(sig, px)
        if q < 1:
            return False, "size rounds to zero"
        gross = sum(p.notional() for p in live) + q * px
        if gross > c.max_gross_pct * c.capital:
            return False, "gross exposure limit"
        return True, q
