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


def _get(url, key, secret, tries=4):
    """GET JSON; retries 429 / 5xx; returns (json, None) or (None, (code, message))."""
    req = urllib.request.Request(url, headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    for k in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode()), None
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if (e.code == 429 or e.code >= 500) and k < tries - 1:
                time.sleep(3 + 4 * k)
                continue
            try:
                msg = json.loads(body).get("message") or body
            except Exception:
                msg = body
            return None, (e.code, msg)
        except Exception as e:
            if k < tries - 1:
                time.sleep(2 + 2 * k)
                continue
            return None, (0, str(e))
    return None, (0, "no answer")


def _bars_chunk(syms, start, end, key, secret, feed, out, bad):
    """Fetch one chunk of symbols (all pages) into out. A 400 means Alpaca refused something in the request - usually
    one symbol it doesn't recognise as one (z172): drop the symbol it names, or split the chunk in half until the
    culprit is alone, and carry on without it. Returns the feed used, or None if Alpaca refused the feed itself."""
    token = None
    while True:
        q = {"symbols": ",".join(s.replace("-", ".") for s in syms), "timeframe": "1Min", "feed": feed, "adjustment": "raw",
             "start": start, "end": end, "limit": 10000, "sort": "asc"}
        if token:
            q["page_token"] = token
        j, err = _get(f"{BARS}?{urllib.parse.urlencode(q)}", key, secret)
        if err:
            code, msg = err
            if code == 403 and feed == "sip":
                log(f"SIP refused ({msg}) - using IEX bars for this request")
                return _bars_chunk(syms, start, end, key, secret, "iex", out, bad)
            if code in (400, 422):
                named = [s for s in syms if s.replace("-", ".").upper() in str(msg).upper().replace(",", " ").split()]
                if len(syms) == 1 or named:
                    for s in (named or syms):
                        bad[s] = str(msg)[:120]
                    rest = [s for s in syms if s not in bad]
                    return _bars_chunk(rest, start, end, key, secret, feed, out, bad) if rest else feed
                h = len(syms) // 2
                _bars_chunk(syms[:h], start, end, key, secret, feed, out, bad)
                _bars_chunk(syms[h:], start, end, key, secret, feed, out, bad)
                return feed
            log(f"bars request failed: HTTP {code} {str(msg)[:160]} - {len(syms)} symbols skipped for this day")
            return feed
        for s, rows in (j.get("bars") or {}).items():
            out.setdefault(s.replace(".", "-"), []).extend(
                (int(datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp()), b["o"], b["h"], b["l"], b["c"], b.get("v", 0)) for b in rows)
        token = j.get("next_page_token")
        if not token:
            return feed


def fetch_day(symbols, d, close_min, key, secret, bad=None, chunk=100):
    """{symbol: [(epoch, o, h, l, c, v)]} for the regular session of day d. Symbols Alpaca refuses are collected in
    `bad` (symbol -> its message) and left out; the rest of the day still loads."""
    et0 = mh.et_now(datetime(d.year, d.month, d.day, 15, tzinfo=timezone.utc))
    start = et0.replace(hour=9, minute=30, second=0, microsecond=0).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = et0.replace(hour=close_min // 60, minute=close_min % 60, second=0, microsecond=0).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    bad = {} if bad is None else bad
    syms = [s for s in symbols if s not in bad]
    out = {}
    # one request for SPY first: if Alpaca refuses even that, the problem isn't a symbol - say so once and stop
    probe = {}
    _bars_chunk(["SPY"], start, end, key, secret, "sip", probe, bad)
    if "SPY" in bad:
        log(f"Alpaca refuses even SPY for {d}: {bad.pop('SPY')} - not a symbol problem; day skipped")
        return {}
    for i in range(0, len(syms), chunk):
        _bars_chunk(syms[i:i + chunk], start, end, key, secret, "sip", out, bad)
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
    trades, bad, loaded = [], {}, 0
    for d, close in days:
        bars = fetch_day(uni["symbols"], d, close, key, secret, bad)
        if not bars:
            log(f"{d}: no bars came back - day skipped")
            continue
        loaded += 1
        tr = replay_day(cfg, uni, bars, d, close)
        trades += tr
        log(f"{d}: {sum(len(v) for v in bars.values())} bars, {len(bars)} symbols · {len(tr)} trades")
    if bad:
        log(f"Alpaca refused {len(bad)} symbol(s), left out: " + ", ".join(f"{k} ({v})" for k, v in list(bad.items())[:12]))
    if not loaded:
        log("no session's bars could be loaded - check the Alpaca keys and the messages above")
        return 1
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
