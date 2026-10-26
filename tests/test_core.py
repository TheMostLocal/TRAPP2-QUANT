import json
import math
import os
import sys
import unittest
from dataclasses import replace
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quant.book import Book
from quant.broker import AlpacaBroker, SimBroker
from quant.config import Config
from quant.feed import AlpacaStream, FeedManager, SnapshotPoller, ts_of
from quant.risk import Position, Risk
from quant.stats import binom_sf, summarize, trades_needed, wilson
from quant.strategies import EtfArb, OpeningRange, SectorReversion, Signal

T0 = 1792675800 - (1792675800 % 60)


def cfg(**k):
    k.setdefault("execution", "sim")
    return replace(Config(), **k)


class Stats(unittest.TestCase):
    def test_wilson_and_binomial(self):
        lo, hi = wilson(5100, 10000)
        self.assertAlmostEqual(lo, 0.5002, places=3)
        self.assertAlmostEqual(hi, 0.5198, places=3)
        self.assertAlmostEqual(binom_sf(6, 10, 0.5), 0.376953125, places=9)          # exact
        self.assertLess(binom_sf(5300, 10000, 0.5075), 0.001)
        self.assertEqual(trades_needed(0.55), 375)

    def test_summary_flats_and_pf(self):
        s = summarize([{"net_bps": x} for x in (10, -5, 0.0, 20, -10)])
        self.assertEqual((s["n"], s["wins"], s["losses"]), (5, 2, 2))
        self.assertEqual(s["winRate"], 0.5)
        self.assertAlmostEqual(s["profitFactor"], 2.0)
        self.assertAlmostEqual(s["expectancyBps"], 3.0)


class BookTests(unittest.TestCase):
    def test_mid_bars_and_carry(self):
        b = Book()
        s = b.get("AAPL")
        s.on_quote(100.0, 100.02, 1, 1, T0 + 5)
        s.on_quote(100.10, 100.12, 1, 1, T0 + 50)
        s.on_quote(100.0, 99.0, 1, 1, T0 + 55)                     # crossed: ignored
        s.on_quote(100.20, 100.22, 1, 1, T0 + 180 + 2)
        b.roll(T0 + 240 + 1)
        self.assertEqual([round(x[4], 2) for x in s.bars], [100.11, 100.11, 100.11, 100.21])
        self.assertEqual([x[6] for x in s.bars], [False, True, True, False])
        self.assertAlmostEqual(s.spread_bps(), 0.02 / 100.21 * 1e4, places=6)
        self.assertTrue(s.fresh(T0 + 200, 30))
        self.assertFalse(s.fresh(T0 + 400, 30))


def feed_series(book, sym, closes, t0=T0, spread=0.0001):
    s = book.get(sym)
    for i, c in enumerate(closes):
        s.on_quote(c * (1 - spread / 2), c * (1 + spread / 2), 1, 1, t0 + 60 * i + 30)
    book.roll(t0 + 60 * len(closes) + 1)


class StrategyTests(unittest.TestCase):
    def setUp(self):
        import random
        self.r = random.Random(3)

    def ctx(self, **k):
        base = dict(cfg=cfg(), minute=11 * 60, now=T0, day_open_epoch=T0, open_symbols=set(), traded_today={},
                    sector_etf={"AAPL": "XLK"}, etf_weights={}, momentum_universe=[])
        base.update(k)
        return SimpleNamespace(**base)

    def test_sector_reversion_fades_a_residual(self):
        b = Book()
        etf, stk = [250.0], [230.0]
        for i in range(80):
            f = self.r.gauss(0, 0.0006)
            etf.append(etf[-1] * math.exp(f))
            stk.append(stk[-1] * math.exp(1.1 * f + self.r.gauss(0, 0.0003) + (0.0012 if i >= 70 else 0)))   # last 10 min: +12 bps a minute vs the sector
        feed_series(b, "XLK", etf)
        feed_series(b, "AAPL", stk)
        sigs = SectorReversion().on_minute(b, self.ctx())
        self.assertEqual(len(sigs), 1)
        self.assertEqual((sigs[0].symbol, sigs[0].side), ("AAPL", "short"))
        self.assertGreater(sigs[0].meta["z"], 2.5)
        self.assertGreater(sigs[0].target_bps, 2 * (1 + 2 * 1.0 + 0.3))          # clears twice the round-trip cost

    def test_etf_arb_needs_persistence(self):
        b = Book()
        n = 30
        aapl = [230 * (1 + (0.0025 if i >= n - 2 else 0)) for i in range(n)]
        nvda = [180 * (1 + (0.0025 if i >= n - 2 else 0)) for i in range(n)]
        xlk = [250.0] * n
        feed_series(b, "AAPL", aapl)
        feed_series(b, "NVDA", nvda)
        feed_series(b, "XLK", xlk)
        st = EtfArb()
        ctx = self.ctx(etf_weights={"XLK": [["AAPL", 0.4], ["NVDA", 0.35]]})
        b2 = Book()
        feed_series(b2, "AAPL", aapl[:-1]); feed_series(b2, "NVDA", nvda[:-1]); feed_series(b2, "XLK", xlk[:-1])
        self.assertEqual(st.on_minute(b2, ctx), [])                                # first minute of the gap: remembered, not traded
        sigs = st.on_minute(b, ctx)
        self.assertEqual(len(sigs), 1)
        self.assertEqual((sigs[0].symbol, sigs[0].side), ("XLK", "long"))
        self.assertAlmostEqual(sigs[0].meta["gap"], 24.97, delta=0.2)

    def test_opening_range_breakout(self):
        b = Book()
        rng = [100 + (0.3 if i % 2 else -0.3) for i in range(15)] + [100.1, 100.2, 100.45]
        feed_series(b, "MSFT", rng, t0=T0)
        ctx = self.ctx(minute=9 * 60 + 48, day_open_epoch=T0, momentum_universe=["MSFT"], traded_today={})
        sigs = OpeningRange().on_minute(b, ctx)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].side, "long")
        self.assertAlmostEqual(sigs[0].target_bps, 2 * sigs[0].stop_bps, places=1)


class RiskBroker(unittest.TestCase):
    def test_sizing_and_limits(self):
        c = cfg()
        r = Risk(c)
        sig = Signal("AAPL", "long", "x", 20.0, 40.0, 30)
        self.assertEqual(r.size(sig, 200.0), 25)                                  # min(150 / (200 x 0.002) = 375, 5000 / 200 = 25)
        b = Book(); s = b.get("AAPL"); s.on_quote(199.99, 200.01, 1, 1, T0)
        ok, q = r.check(sig, s, 10 * 60, T0 + 1, [], 0.0)
        self.assertTrue(ok); self.assertEqual(q, 25)
        self.assertEqual(r.check(sig, s, 10 * 60, T0 + 1, [], -1600.0)[1], "daily loss halt")
        self.assertEqual(r.check(sig, s, 9 * 60 + 35, T0 + 1, [], 0)[1], "outside the entry window")
        self.assertEqual(r.check(sig, s, 10 * 60, T0 + 100, [], 0)[1], "stale quote")
        s.on_quote(199.0, 201.0, 1, 1, T0 + 2)
        self.assertTrue(r.check(sig, s, 10 * 60, T0 + 3, [], 0)[1].startswith("spread"))

    def test_sim_fill_pays_spread_and_slippage(self):
        c = cfg()
        b = Book(); s = b.get("X"); s.on_quote(99.99, 100.01, 1, 1, T0)
        q, px = SimBroker(c).fill("X", "buy", 10, s)
        self.assertEqual(q, 10); self.assertAlmostEqual(px, 100.01 * 1.0001, places=6)
        q, px = SimBroker(c).fill("X", "sell", 10, s)
        self.assertAlmostEqual(px, 99.99 * 0.9999, places=6)

    def test_alpaca_order_flow(self):
        c = cfg(execution="alpaca")
        br = AlpacaBroker(c, key="k", secret="s")
        calls = []
        seq = [{"id": "o1", "status": "new"}, {"id": "o1", "status": "partially_filled", "filled_qty": "5"}, {"id": "o1", "status": "filled", "filled_qty": "10", "filled_avg_price": "100.02"}]

        def req(method, path, body=None, timeout=15):
            calls.append((method, path, body))
            return seq.pop(0)
        br._req = req
        q, px = br.fill("BRK-B", "buy", 10, None, sleep=lambda s: None)
        self.assertEqual((q, px), (10, 100.02))
        self.assertEqual(calls[0][0], "POST")
        self.assertEqual(calls[0][2]["symbol"], "BRK.B")
        self.assertEqual(calls[0][2]["type"], "market")
        self.assertTrue(calls[0][2]["client_order_id"].startswith("q-"))

        def rejected(method, path, body=None, timeout=15):
            import urllib.error, io
            raise urllib.error.HTTPError("u", 403, "forbidden", {}, io.BytesIO(b'{"message":"not shortable"}'))
        br._req = rejected
        self.assertEqual(br.fill("GME", "sell", 10, None, sleep=lambda s: None), (0, None))


class FeedTests(unittest.TestCase):
    def test_ts_and_stream_messages(self):
        self.assertAlmostEqual(ts_of("2026-10-26T14:30:05.123456789Z") % 60, 5.123456, places=5)
        b = Book()
        a = AlpacaStream(b, ["BRK-B"], "iex", "k", "s")
        a.handle(json.dumps([{"T": "q", "S": "BRK.B", "bp": 480.1, "ap": 480.2, "bs": 1, "as": 2, "t": "2026-10-26T14:30:05Z"},
                             {"T": "t", "S": "AAPL", "p": 230.5, "s": 100, "t": "2026-10-26T14:30:06Z"},
                             {"T": "error", "code": 405, "msg": "symbol limit exceeded"}]))
        self.assertAlmostEqual(b.get("BRK-B").mid(), 480.15)
        self.assertEqual(b.get("AAPL").last, 230.5)
        self.assertEqual(a.errors, ["405 symbol limit exceeded"])

    def test_snapshot_apply_and_watchdog_widening(self):
        b = Book()
        p = SnapshotPoller(b, ["MSFT"], "iex", "k", "s", 4)
        p.apply({"MSFT": {"latestQuote": {"bp": 500, "ap": 500.1, "bs": 1, "as": 1, "t": "2026-10-26T14:30:05Z"}, "latestTrade": {"p": 500.05, "s": 10, "t": "2026-10-26T14:30:05Z"}}})
        self.assertAlmostEqual(b.get("MSFT").mid(), 500.05)
        os.environ.update({"ALPACA_KEY_ID": "k", "ALPACA_SECRET_KEY": "s"})
        fm = FeedManager(b, ["SPY", "AAPL", "MSFT"], ["SPY"], cfg())
        self.assertEqual(fm.poller.symbols, ["AAPL", "MSFT"])
        import asyncio

        async def once():
            stop = asyncio.Event()
            t = asyncio.create_task(fm.watchdog(stop))
            await asyncio.sleep(0.01)
            stop.set()
            await t
        asyncio.run(once())
        self.assertEqual(fm.poller.extra, ["SPY"])                                 # stream not connected -> poll the streamed names too


if __name__ == "__main__":
    unittest.main()
