# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-QUANT
#  >>> FILE PATH:   quant/config.py
#  Every knob of the quant book in one place; environment variables (repo Variables / secrets) override.
# ============================================================
import os
from dataclasses import dataclass, field


def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


@dataclass
class Config:
    # ---- market data -----------------------------------------------------------------------------
    feed: str = os.environ.get("QUANT_FEED", "iex")                 # "sip" = Alpaca Algo Trader Plus (all exchanges, real time)
    stream_limit: int = int(_f("QUANT_STREAM_LIMIT", 30))           # free plan: 30 streamed symbols; sip: raise to the universe
    poll_seconds: float = _f("QUANT_POLL_SECONDS", 4)               # REST snapshots for everything not streamed
    stale_seconds: float = _f("QUANT_STALE_SECONDS", 30)            # a symbol older than this is not traded
    # ---- the book --------------------------------------------------------------------------------
    capital: float = _f("QUANT_CAPITAL", 100000)
    risk_per_trade: float = _f("QUANT_RISK_PER_TRADE", 0.0015)      # loss at the stop, share of capital (0.15 %)
    max_position_pct: float = _f("QUANT_MAX_POSITION_PCT", 0.05)    # one position <= 5 % of capital
    max_gross_pct: float = _f("QUANT_MAX_GROSS_PCT", 1.5)           # gross exposure <= 150 % of capital
    max_positions: int = int(_f("QUANT_MAX_POSITIONS", 25))
    daily_loss_halt_pct: float = _f("QUANT_DAILY_HALT_PCT", 0.015)  # no new entries after -1.5 % on the day
    max_spread_bps: float = _f("QUANT_MAX_SPREAD_BPS", 8)
    min_price: float = _f("QUANT_MIN_PRICE", 5)
    # ---- session, ET minutes from midnight ---------------------------------------------------------
    no_entry_before: int = 9 * 60 + 40          # let the opening auction settle
    no_entry_after: int = 15 * 60 + 40
    flatten_at: int = 15 * 60 + 55              # the intraday book is flat into the close
    # ---- costs (paper realism: every fill pays the half spread plus this) --------------------------
    slippage_bps: float = _f("QUANT_SLIPPAGE_BPS", 1.0)             # per side, beyond the half spread
    fee_bps_sell: float = _f("QUANT_FEE_BPS_SELL", 0.3)             # SEC + FINRA TAF on sales, rounded up
    backtest_half_spread_bps: float = _f("QUANT_BT_HALF_SPREAD_BPS", 2.0)
    # ---- execution -------------------------------------------------------------------------------
    execution: str = os.environ.get("QUANT_EXECUTION", "alpaca")    # "alpaca" (paper API, real quotes) | "sim"
    alpaca_trading: str = os.environ.get("QUANT_ALPACA_TRADING", "https://paper-api.alpaca.markets")
    # ---- quality gate: a strategy trades live only if its last backtest earned it -----------------
    gate_min_trades: int = int(_f("QUANT_GATE_MIN_TRADES", 200))
    gate_min_tstat: float = _f("QUANT_GATE_MIN_TSTAT", 2.0)
    gate_min_expectancy_bps: float = _f("QUANT_GATE_MIN_EXP_BPS", 0.5)
    strategies: tuple = tuple(s.strip() for s in os.environ.get("QUANT_STRATEGIES", "sector_reversion,etf_arb,opening_range").split(",") if s.strip())
    benchmark_win_rate: float = 0.5075          # Medallion's often-quoted hit rate - the bar to clear, with statistics


CFG = Config()
