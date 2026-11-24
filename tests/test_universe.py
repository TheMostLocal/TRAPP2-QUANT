"""z174: the quant universe takes every US stock / ETF in the books - no top-120 cap - and says what it can't trade."""
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import quant.universe as U
from quant.config import Config
from quant.engine import Engine
from quant.strategies import Signal


def master(rows):
    return json.dumps(rows).encode()


class Build(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp()) / "universe.json"
        U.OUT = self.out
        stocks = [{"ticker": f"S{chr(65 + i // 26)}{chr(65 + i % 26)}", "price": "50", "volume": str(10000 * (i + 1)), "sector": "Technology"} for i in range(300)]
        extra = [{"ticker": t, "price": "10", "volume": "1000"} for t in ("^VIX", "EURUSD=X", "CL=F", "BTC-USD", "SAP.DE", "SPY261204C00800000")]
        thin = [{"ticker": "THIN", "price": "3", "volume": "100", "sector": "Energy"}]
        books = {"TRAPP2": stocks[:150] + extra, "TRAPP2-1": stocks[150:] + thin, "TRAPP2-2": stocks[:10], "TRAPP2-3": []}

        def get(url, timeout=60):
            if "ssga" in url:
                raise RuntimeError("offline")
            b = url.split("/TheMostLocal/")[1].split("/")[0]
            return master(books[b])
        U.get = get

    def test_every_us_symbol_kept_and_the_rest_counted(self):
        d = U.build(n_stream=30, momentum_min_dv=2e7)
        c = d["counts"]
        self.assertEqual(c["bookTickers"], 307)                       # 300 stocks + THIN + 6 others, duplicates counted once
        self.assertEqual(c["usTradable"], 301)                        # no 120 cap any more
        self.assertEqual(c["notOnAlpacaStocks"], {"index": 1, "fx": 1, "future": 1, "crypto": 1, "foreign listing": 1, "other": 1})
        self.assertIn("THIN", d["symbols"])                           # watched ...
        self.assertEqual(d["liquidity"]["THIN"], 300)                 # ... with its $300 a day on record
        self.assertEqual(len(d["sector_etf"]), 301)
        self.assertTrue(all(d["liquidity"][t] >= 2e7 for t in d["momentum"]))
        self.assertEqual(len(d["stream"]), 30)
        self.assertEqual(d["stream"][:4], ["SPY", "QQQ", "IWM", "DIA"])


class Liquidity(unittest.TestCase):
    def test_engine_never_enters_below_the_dollar_volume_floor(self):
        d = tempfile.mkdtemp()
        uni = {"symbols": ["THIN", "AAPL"], "liquidity": {"THIN": 300, "AAPL": 9e9}, "sector_etf": {}, "etf_weights": {}, "momentum": []}
        e = Engine(replace(Config(), execution="sim"), uni, clock=lambda: 1793026800, data_dir=d, log=lambda *a: None)
        sig = Signal("THIN", "long", "sector_reversion", 20.0, 40.0, 30)
        self.assertIsNone(e.enter(sig, 1793026800))
        self.assertEqual(e.counts["skip:illiquid"], 1)




class Costs(unittest.TestCase):
    def test_half_spread_scales_with_liquidity(self):
        from quant.backtest import half_spread_bps as h
        self.assertAlmostEqual(h(2e8), 2.0)
        self.assertAlmostEqual(h(5e7), 4.0)
        self.assertEqual(h(2e9), 1.0)            # floor
        self.assertEqual(h(1e5), 15.0)           # cap
        self.assertEqual(h(None), 2.0)           # unknown: the base




class Kinds(unittest.TestCase):
    def test_funds_by_expense_ratio_and_tags(self):
        from types import SimpleNamespace
        from quant.segments import tags_for, keys_for
        out = Path(tempfile.mkdtemp()) / "universe.json"
        U.OUT = out
        rows = [{"ticker": "AAPL", "price": "200", "volume": "5e7", "sector": "Technology", "expenseRatio": ""},
                {"ticker": "EWZ", "price": "26", "volume": "4e7", "expenseRatio": "0.59"},
                {"ticker": "GLD", "price": "300", "volume": "1e7", "expenseRatio": "0.4"}]
        U.get = lambda url, timeout=60: (_ for _ in ()).throw(RuntimeError("offline")) if "ssga" in url else (master(rows) if "/TRAPP2/" in url else master([]))
        d = U.build(n_stream=30, momentum_min_dv=0)
        self.assertEqual((d["kinds"]["AAPL"], d["kinds"]["EWZ"], d["kinds"]["GLD"], d["kinds"]["XLK"]), ("stock", "fund", "fund", "fund"))
        self.assertEqual(d["counts"]["stocksInUniverse"], 1)
        ctx = SimpleNamespace(minute=700, liquidity=d["liquidity"], sector_etf=d["sector_etf"], kinds=d["kinds"])
        self.assertEqual(tags_for("AAPL", "long", ctx)["kind"], "stock")
        self.assertEqual(tags_for("XLK", "short", ctx, sector="XLK")["kind"], "fund")
        self.assertFalse(any("kind" in k for k in keys_for(tags_for("AAPL", "long", ctx))), "kind is shown, not a segment dimension")


class Watch(unittest.TestCase):
    def test_watch_list_keeps_tradable_names_and_references(self):
        uni = {"symbols": ["XLK", "SPY", "AAPL", "THIN", "MIDCAP", "HOLDX"], "liquidity": {"AAPL": 9e9, "THIN": 3e5, "MIDCAP": 6e6}}
        self.assertEqual(U.watch_symbols(uni, 5e6), ["XLK", "SPY", "AAPL", "MIDCAP", "HOLDX"])     # THIN can never be entered


if __name__ == "__main__":
    unittest.main()


class AverageDollarVolume(unittest.TestCase):
    """z205: the 20-session average dollar volume (data/derived/market.json) is a name's liquidity; one session's
    price x volume only where the file has nothing (or is over a week old)."""

    def setUp(self):
        self.out = Path(tempfile.mkdtemp()) / "universe.json"
        U.OUT = self.out
        self.books = {"TRAPP2": [{"ticker": "AAA", "price": "50", "volume": "100000", "sector": "Technology"},     # $5.0M today
                                 {"ticker": "BBB", "price": "20", "volume": "50000", "sector": "Energy"},          # $1.0M today
                                 {"ticker": "CCC", "price": "10", "volume": "1000", "sector": "Energy"},           # not in the file
                                 {"ticker": "EEE", "price": "30", "volume": "10000", "sector": "Energy"}],         # flagged px:
                      "TRAPP2-1": [], "TRAPP2-2": [], "TRAPP2-3": [], "TRAPP2-4": []}
        self.at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")      # a fresh file, whenever the test runs

        def get(url, timeout=60):
            if "ssga" in url:
                raise RuntimeError("offline")
            if "derived/market.json" in url:
                return json.dumps({"at": self.at, "date": "2026-11-20", "tickers": {
                    "AAA": {"dv20": 3.2e6}, "BBB": {"dv20": 7.5e6}, "DDD": {"dv20": 1e9, "q": ["stale"]},
                    "EEE": {"dv20": 9e6, "q": ["short", "px:x0.415"]}}}).encode()
            b = url.split("/TheMostLocal/")[1].split("/")[0]
            return master(self.books[b])
        U.get = get

    def test_average_wins_and_the_floor_moves_with_it(self):
        d = U.build(n_stream=10, momentum_min_dv=2e7)
        self.assertEqual(d["liquidity"]["AAA"], 3200000)                  # one session said $5.0M - above the $5M floor
        self.assertEqual(d["liquidity"]["BBB"], 7500000)                  # one session said $1.0M - below it
        self.assertEqual(d["liquidity"]["CCC"], 10000)                    # not in the file: one session's figure
        self.assertEqual(d["liquidity"]["EEE"], 300000)                   # its close disagreed with the book's: one session's
        self.assertEqual(d["counts"]["liquidity"], {"avg20": 2, "oneSession": 2, "asOf": "2026-11-20"})
        w = U.watch_symbols(d, 5e6)
        self.assertIn("BBB", w)
        self.assertNotIn("AAA", w)

    def test_old_or_missing_file_falls_back(self):
        self.at = "2026-11-01T03:00:00Z"
        adv, date = U.average_dollar_volume()
        self.assertEqual((adv, date), ({}, None))
        d = U.build(n_stream=10, momentum_min_dv=2e7)
        self.assertEqual(d["liquidity"]["AAA"], 5000000)
        self.assertEqual(d["counts"]["liquidity"]["oneSession"], 4)
        U.get = lambda url, timeout=60: (_ for _ in ()).throw(RuntimeError("offline"))
        self.assertEqual(U.average_dollar_volume(), ({}, None))
