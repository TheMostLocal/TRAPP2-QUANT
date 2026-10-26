# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-QUANT
#  >>> FILE PATH:   quant/backtest.py
#  THE QUALITY GATE. Replays the last N sessions of real 1-minute bars (Alpaca, consolidated SIP - free once
#  older than 15 minutes) through the SAME engine, strategies and risk rules, with fills at the bar's close plus
#  the half spread and slippage, and writes data/quant/gate.json: a strategy goes live only with >= 200 trades,
#  expectancy >= 0.5 bps net of costs and a t-statistic >= 2. Everything else stays in shadow.
#  Limits, stated: minute bars (no queue position, no intrabar path - stops and targets are judged at the close),
#  a modelled spread, and today's universe applied to past days (survivorship). Live shadow results are the
#  second, independent check.
# ============================================================
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import market_hours as mh
from .broker import SimBroker
from .config import CFG
from .engine import Engine
from .ledger import DATA
from .stats import summarize

BARS = "https://data.alpaca.markets/v2/stocks/bars"


def log(*a):
    print("[backtest]", *a, flush=True)


def sessions(n, today=None):
    d, out = (today or mh.et_now().date()) - timedelta(days=1), []
    while len(out) < n:
        ok, close, _ = mh.day_status(d)
        if ok:
            out.append((d, close))
        d -= timedelta(days=1)
    return list(reversed(out))


def fetch_day(symbols, d, close_min, key, secret):
    """{symbol: [(epoch, o, h, l, c, v)]} for the regular session of day d."""
    et0 = mh.et_now(datetime(d.year, d.month, d.day, 15, tzinfo=timezone.utc))
    start = et0.replace(hour=9, minute=30, second=0, microsecond=0)
    end = et0.replace(hour=close_min // 60, minute=close_min % 60, second=0, microsecond=0)
    out = {}
    for i in range(0, len(symbols), 200):
        token = None
        while True:
            q = {"symbols": ",".join(s.replace("-", ".") for s in symbols[i:i + 200]), "timeframe": "1Min", "feed": "sip", "adjustment": "raw",
                 "start": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "end": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "limit": 10000, "sort": "asc"}
            if token:
                q["page_token"] = token
            req = urllib.request.Request(f"{BARS}?{urllib.parse.urlencode(q)}", headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
            for k in range(4):
                try:
                    with urllib.request.urlopen(req, timeout=60) as r:
                        j = json.loads(r.read().decode())
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 429 and k < 3:
                        time.sleep(3 + 4 * k)
                        continue
                    raise
            for s, rows in (j.get("bars") or {}).items():
                out.setdefault(s.replace(".", "-"), []).extend(
                    (int(datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp()), b["o"], b["h"], b["l"], b["c"], b.get("v", 0)) for b in rows)
            token = j.get("next_page_token")
            if not token:
                break
    return out


def replay_day(cfg, uni, bars, d, close_min, log_fn=lambda *a: None):
    """Run one session through the engine minute by minute; returns its closed trades."""
    tmp = tempfile.mkdtemp()
    (Path(tmp) / "gate.json").write_text(json.dumps({"strategies": {s: {"pass": True} for s in cfg.strategies}}))
    et0 = mh.et_now(datetime(d.year, d.month, d.day, 15, tzinfo=timezone.utc))
    t_open = et0.replace(hour=9, minute=30, second=0, microsecond=0).timestamp()
    clock = {"t": t_open}
    eng = Engine(replace(cfg, execution="sim"), uni, clock=lambda: clock["t"], broker=SimBroker(cfg), data_dir=tmp, log=log_fn)
    eng.positions, eng.ledger.pnl = [], 0.0
    by_min = {}
    for s, rows in bars.items():
        for (t, o, h, l, c, v) in rows:
            by_min.setdefault(t, []).append((s, o, h, l, c, v))
    flatten_at = min(cfg.flatten_at, close_min - 5)
    for t in sorted(by_min):
        for (s, o, h, l, c, v) in by_min[t]:
            eng.book.on_bar(s, t, o, h, l, c, v, cfg.backtest_half_spread_bps)
        clock["t"] = t + 59
        m = eng.minute(clock["t"])
        if m >= flatten_at:
            eng.flatten(clock["t"], "flatten (backtest)")
            break
        eng.on_minute(clock["t"])
    if eng.positions:
        eng.flatten(clock["t"], "end of data")
    for tr in eng.ledger.closed:
        tr["mode"] = "backtest"
    return eng.ledger.closed


def gate(trades, cfg):
    res = {}
    for s in cfg.strategies:
        st = summarize([t for t in trades if t["strategy"] == s], cfg.benchmark_win_rate)
        ok = (st.get("n", 0) >= cfg.gate_min_trades and (st.get("expectancyBps") or -1) >= cfg.gate_min_expectancy_bps
              and (st.get("tStat") or 0) >= cfg.gate_min_tstat)
        res[s] = {"pass": bool(ok), **{k: st.get(k) for k in ("n", "winRate", "winRateCI95", "expectancyBps", "tStat", "profitFactor", "pBeatsBenchmark")}}
    return res


def main(n_days=None):
    from .universe import load
    cfg = CFG
    key, secret = os.environ.get("ALPACA_KEY_ID", "").strip(), os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if not (key and secret):
        log("ALPACA_KEY_ID / ALPACA_SECRET_KEY not set - cannot fetch history")
        return 1
    uni = load()
    days = sessions(int(n_days or os.environ.get("QUANT_BT_DAYS", "10")))
    trades = []
    for d, close in days:
        bars = fetch_day(uni["symbols"], d, close, key, secret)
        tr = replay_day(cfg, uni, bars, d, close)
        trades += tr
        log(f"{d}: {sum(len(v) for v in bars.values())} bars, {len(bars)} symbols · {len(tr)} trades")
    g = gate(trades, cfg)
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "backtest").mkdir(exist_ok=True)
    doc = {"asOf": datetime.now(timezone.utc).isoformat(timespec="seconds"), "days": [d.isoformat() for d, _ in days], "trades": len(trades),
           "strategies": g, "costs": {"halfSpreadBps": cfg.backtest_half_spread_bps, "slippageBps": cfg.slippage_bps, "feeBpsSell": cfg.fee_bps_sell},
           "rules": {"minTrades": cfg.gate_min_trades, "minExpectancyBps": cfg.gate_min_expectancy_bps, "minTStat": cfg.gate_min_tstat}}
    (DATA / "backtest" / "latest.json").write_text(json.dumps(doc, indent=1))
    (DATA / "gate.json").write_text(json.dumps({"asOf": doc["asOf"], "strategies": g, "rules": doc["rules"]}, indent=1))
    for s, v in g.items():
        log(f"{s}: {'LIVE' if v['pass'] else 'shadow'} · n {v['n']} · win {v['winRate']} · expectancy {v['expectancyBps']} bps · t {v['tStat']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
