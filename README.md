# TRAPP2-QUANT — the intraday / short-horizon quant book (z163)

A second, separate paper book next to the swing bot (TRAPP2-BOT): many small, short-horizon trades judged by
statistics, not anecdotes. It borrows the two families the request named — **statistical arbitrage** (Medallion:
many small mean-reversion bets) and **ETF arbitrage** (Jane Street: an ETF converges to its basket) — plus
intraday momentum for breadth across the day.

| Strategy | Family | Holds | Edge it looks for |
|---|---|---|---|
| `sector_reversion` | stat-arb | ≤ 30 min | a stock's 10-minute move against its sector ETF, net of beta, at z ≥ 2.5 — faded |
| `etf_arb` | ETF arbitrage | ≤ 10 min | a sector ETF lagging its own holdings (State Street daily weights) by > 2.5× the round-trip cost, two minutes running |
| `opening_range` | momentum | to the close | a clean break of the first 15 minutes' range before 1130 ET |

**Data, best first:** Alpaca websocket (feed `sip` with Algo Trader Plus — every exchange, real time, unlimited
symbols; `iex` free — IEX only, 30 symbols) → Alpaca REST snapshots every 4 s for everything else and for all names
whenever the stream is silent > 10 s → Finnhub websocket trades (optional `FINNHUB_KEY`). Stale data (> 30 s) is never traded.

**Execution:** market orders on the Alpaca **paper** account (same keys) — real fills against real quotes. Strategies
not yet proven run in **shadow** (same signals, simulated fills at the far quote + slippage, separate statistics).

**The quality gate** (`quant/backtest.py`, nightly): the last 10 sessions of real 1-minute bars through the same engine;
a strategy trades live only with ≥ 200 trades, expectancy ≥ 0.5 bps net of costs and t ≥ 2.

**Risk:** 0.15 % of capital lost at a stop; ≤ 5 % per position, ≤ 25 positions, ≤ 150 % gross; entries 0940–1540 ET,
flat at 1555 ET; no new entries after −1.5 % on the day; spread ≤ 8 bps, price ≥ $5.

**Hosting:** GitHub Actions (free): `engine.yml` 0920 ET → close in chained 345-minute jobs, open positions handed over in
`data/quant/open.json`. For no limits: `deploy/Dockerfile` or `deploy/quant.service` on a small VPS in us-east.

**Statistics** (`quant/stats.py`): win rate with its 95 % interval, one-sided test against Medallion's 50.75 %, expectancy
in bps net of costs, profit factor, t-stat. Rule of thumb: ~4,300 trades to show 52 % beats 50.75 %; ~375 for 55 %.

Tests: `python -m unittest discover -s tests`.
