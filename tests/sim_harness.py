"""A deterministic simulated session for tests: a fake clock, a fake feed writing quotes into the Book every
second, and planted opportunities for each strategy. No network."""
import asyncio
import math
import random
from datetime import datetime, timezone


class FakeClock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t


class FakeFeed:
    """Random-walk prices with a common sector factor; planted events:
       - AAPL jumps +40 bps vs XLK at 10:30 and drifts back over 15 min (stat-arb reversion)
       - NVDA and AAPL rally 25 bps at 11:00 while XLK lags 6 minutes (ETF arbitrage)
       - MSFT breaks its opening range at 09:52 and trends up (opening-range momentum)"""

    def __init__(self, engine, clock, seed=7):
        self.e, self.c, self.r = engine, clock, random.Random(seed)
        self.px = {"XLK": 250.0, "AAPL": 230.0, "NVDA": 180.0, "MSFT": 500.0, "SPY": 600.0}
        self.f = 0.0

    def status(self):
        return {"stream": True, "feed": "fake"}

    def step(self):
        now = self.c()
        et = self.e.et(now)
        m = et.hour * 60 + et.minute + et.second / 60
        df = self.r.gauss(0, 0.00012)                    # sector factor per second
        for s in self.px:
            idio = self.r.gauss(0, 0.00006)
            beta = {"XLK": 1.0, "AAPL": 1.1, "NVDA": 1.4, "MSFT": 0.9, "SPY": 0.7}[s]
            r = beta * df + idio
            if s == "AAPL" and 630 <= m < 631:          # 10:30: +40 bps idiosyncratic jump within a minute
                r += 0.0040 / 60
            if s == "AAPL" and 631 <= m < 646:          # drifts back over 15 minutes
                r -= 0.0030 / (15 * 60)
            if s in ("AAPL", "NVDA") and 660 <= m < 661:
                r += 0.0025 / 60
            if s == "XLK" and 666 <= m < 667:           # XLK catches up 6 minutes later
                r += 0.0025 * 0.55 / 60
            if s == "MSFT" and 592 <= m < 640:           # 09:52 on: a steady trend up
                r += 0.0040 / (48 * 60)
            self.px[s] *= math.exp(r)
            p = self.px[s]
            hs = p * 0.00008                            # 1.6 bps spread
            st = self.e.book.get(s)
            st.on_quote(p - hs, p + hs, 100, 100, now)

    async def run(self, stop):
        while not stop.is_set():
            await asyncio.sleep(0)


def universe():
    return {"symbols": ["XLK", "AAPL", "NVDA", "MSFT", "SPY"], "stream": ["XLK", "AAPL"], "sector_etf": {"AAPL": "XLK", "NVDA": "XLK", "MSFT": "XLK"},
            "etf_weights": {"XLK": [["NVDA", 0.40], ["AAPL", 0.35]]}, "momentum": ["MSFT", "AAPL", "NVDA"]}


def run_session(engine_cls, cfg, data_dir, gate=None, start_utc=datetime(2026, 10, 26, 13, 30, tzinfo=timezone.utc), until_utc=None, log=lambda *a: None, broker=None):
    import json
    from pathlib import Path
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    if gate is not None:
        (Path(data_dir) / "gate.json").write_text(json.dumps({"strategies": gate}))
    clock = FakeClock(start_utc.timestamp())
    eng = engine_cls(cfg, universe(), clock=clock, data_dir=data_dir, log=log, broker=broker)
    feed = FakeFeed(eng, clock)

    async def fake_sleep(dt):
        clock.t += dt
        feed.step()
        await asyncio.sleep(0)

    end = (until_utc or datetime(2026, 10, 26, 20, 3, tzinfo=timezone.utc)).timestamp()
    summary = asyncio.run(eng.run(until_epoch=end, feeds=feed, sleep=fake_sleep))
    return eng, summary
