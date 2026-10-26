# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-QUANT
#  >>> FILE PATH:   quant/feed.py
#  REAL-TIME MARKET DATA, with backups.
#    1. AlpacaStream - websocket quotes + trades, pushed as they happen. Feed "sip" (Algo Trader Plus: every US
#       exchange, unlimited symbols) or "iex" (free: IEX only, 30 symbols) - the most active names stream.
#    2. SnapshotPoller - Alpaca REST snapshots (latest quote + trade), 200 symbols a request, every few seconds:
#       everything not streamed, and EVERYTHING while the stream is down or silent for more than 10 s.
#    3. FinnhubStream - free websocket trades (FINNHUB_KEY): a third source for the streamed names if both
#       Alpaca paths fail.
#  All three write into the same Book; the risk check refuses any symbol whose data is older than stale_seconds.
# ============================================================
import asyncio
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime


def ts_of(s):
    """RFC 3339 with up to nanoseconds -> epoch seconds."""
    if not s:
        return time.time()
    s = str(s)
    if "." in s:
        head, frac = s.split(".", 1)
        tz = ""
        for i, ch in enumerate(frac):
            if not ch.isdigit():
                tz = frac[i:]
                frac = frac[:i]
                break
        s = f"{head}.{frac[:6]}{tz}"
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


class AlpacaStream:
    def __init__(self, book, symbols, feed, key, secret, url=None):
        self.book, self.symbols, self.feed = book, list(symbols), feed
        self.key, self.secret = key, secret
        self.url = url or f"wss://stream.data.alpaca.markets/v2/{feed}"
        self.connected, self.last_msg, self.msgs, self.errors = False, 0.0, 0, []

    def handle(self, raw):
        for m in json.loads(raw):
            T = m.get("T")
            if T == "q":
                self.book.get(m["S"].replace(".", "-")).on_quote(m.get("bp"), m.get("ap"), m.get("bs"), m.get("as"), ts_of(m.get("t")))
            elif T == "t":
                self.book.get(m["S"].replace(".", "-")).on_trade(m.get("p"), m.get("s"), ts_of(m.get("t")))
            elif T == "error":
                self.errors.append(f"{m.get('code')} {m.get('msg')}")
                print(f"[feed] alpaca stream error {m.get('code')}: {m.get('msg')}", flush=True)
            self.msgs += 1
        self.last_msg = time.time()

    async def run(self, stop):
        import websockets
        backoff = 1
        while not stop.is_set():
            try:
                async with websockets.connect(self.url, ping_interval=20, max_size=2 ** 23) as ws:
                    await ws.recv()
                    await ws.send(json.dumps({"action": "auth", "key": self.key, "secret": self.secret}))
                    ok = json.loads(await ws.recv())
                    if not any(x.get("msg") == "authenticated" for x in ok):
                        raise RuntimeError(f"auth failed: {ok}")
                    syms = [s.replace("-", ".") for s in self.symbols]
                    await ws.send(json.dumps({"action": "subscribe", "quotes": syms, "trades": syms}))
                    self.connected, backoff = True, 1
                    print(f"[feed] alpaca {self.feed} stream: {len(syms)} symbols", flush=True)
                    while not stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5)
                        except asyncio.TimeoutError:
                            continue
                        self.handle(raw)
            except Exception as e:
                self.connected = False
                print(f"[feed] alpaca stream down ({type(e).__name__}: {str(e)[:120]}) - retry in {backoff}s", flush=True)
                await asyncio.sleep(backoff)
                backoff = min(60, backoff * 2)
        self.connected = False


class SnapshotPoller:
    URL = "https://data.alpaca.markets/v2/stocks/snapshots"

    def __init__(self, book, symbols, feed, key, secret, every):
        self.book, self.symbols, self.feed, self.every = book, list(symbols), feed, every
        self.key, self.secret = key, secret
        self.extra = []               # streamed names added back while the stream is down
        self.polls, self.errors, self.last_ok = 0, 0, 0.0

    def fetch(self, syms):
        q = urllib.parse.urlencode({"symbols": ",".join(s.replace("-", ".") for s in syms), "feed": self.feed})
        req = urllib.request.Request(f"{self.URL}?{q}", headers={"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())

    def apply(self, snap):
        for s, v in (snap or {}).items():
            if not isinstance(v, dict):
                continue
            st = self.book.get(s.replace(".", "-"))
            q, t = v.get("latestQuote") or {}, v.get("latestTrade") or {}
            if q:
                st.on_quote(q.get("bp"), q.get("ap"), q.get("bs"), q.get("as"), ts_of(q.get("t")))
            if t:
                st.on_trade(t.get("p"), t.get("s"), ts_of(t.get("t")))

    async def run(self, stop):
        while not stop.is_set():
            syms = list(dict.fromkeys(self.symbols + self.extra))
            for i in range(0, len(syms), 200):
                try:
                    self.apply(await asyncio.to_thread(self.fetch, syms[i:i + 200]))
                    self.last_ok = time.time()
                except Exception as e:
                    self.errors += 1
                    if self.errors <= 5 or self.errors % 50 == 0:
                        print(f"[feed] snapshots: {type(e).__name__} {str(e)[:120]}", flush=True)
            self.polls += 1
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.every)
            except asyncio.TimeoutError:
                pass


class FinnhubStream:
    def __init__(self, book, symbols, token):
        self.book, self.symbols, self.token = book, list(symbols)[:50], token
        self.connected, self.last_msg = False, 0.0

    def handle(self, raw):
        m = json.loads(raw)
        if m.get("type") == "trade":
            for d in m.get("data") or []:
                self.book.get(str(d.get("s")).replace(".", "-")).on_trade(d.get("p"), d.get("v"), (d.get("t") or 0) / 1000)
            self.last_msg = time.time()

    async def run(self, stop):
        import websockets
        backoff = 2
        while not stop.is_set():
            try:
                async with websockets.connect(f"wss://ws.finnhub.io?token={self.token}", ping_interval=20) as ws:
                    for s in self.symbols:
                        await ws.send(json.dumps({"type": "subscribe", "symbol": s.replace("-", ".")}))
                    self.connected, backoff = True, 2
                    while not stop.is_set():
                        try:
                            self.handle(await asyncio.wait_for(ws.recv(), timeout=5))
                        except asyncio.TimeoutError:
                            continue
            except Exception as e:
                self.connected = False
                print(f"[feed] finnhub down ({type(e).__name__}) - retry in {backoff}s", flush=True)
                await asyncio.sleep(backoff)
                backoff = min(120, backoff * 2)


class FeedManager:
    """Stream the most important names, poll the rest, and widen the poll to everything if the stream stalls."""

    def __init__(self, book, symbols, stream_symbols, cfg):
        key, secret = os.environ.get("ALPACA_KEY_ID", "").strip(), os.environ.get("ALPACA_SECRET_KEY", "").strip()
        self.cfg = cfg
        self.stream = AlpacaStream(book, stream_symbols, cfg.feed, key, secret) if key and secret else None
        rest = [s for s in symbols if s not in set(stream_symbols)] if self.stream else list(symbols)
        self.poller = SnapshotPoller(book, rest, cfg.feed, key, secret, cfg.poll_seconds) if key and secret else None
        tok = os.environ.get("FINNHUB_KEY", "").strip()
        self.finnhub = FinnhubStream(book, stream_symbols, tok) if tok else None
        self.stream_symbols = list(stream_symbols)

    def status(self):
        now = time.time()
        return {"stream": bool(self.stream and self.stream.connected and now - self.stream.last_msg < 10),
                "streamMsgs": self.stream.msgs if self.stream else 0, "pollerOk": bool(self.poller and now - self.poller.last_ok < 30),
                "finnhub": bool(self.finnhub and self.finnhub.connected), "feed": self.cfg.feed}

    async def watchdog(self, stop):
        while not stop.is_set():
            if self.stream and self.poller:
                down = not self.stream.connected or time.time() - self.stream.last_msg > 10
                self.poller.extra = self.stream_symbols if down else []
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    async def run(self, stop):
        tasks = [x.run(stop) for x in (self.stream, self.poller, self.finnhub) if x] + [self.watchdog(stop)]
        await asyncio.gather(*tasks)
