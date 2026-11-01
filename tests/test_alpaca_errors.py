"""z172: Alpaca answered HTTP 400 to the backtest's bar request and the whole run died. A local server speaks
Alpaca's bars API: 400 naming a bad symbol, 400 naming nothing, 403 for SIP, pagination - the fetch must isolate
the culprit, fall back to IEX when SIP is refused, and still load every good symbol."""
import io
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.parse
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quant import backtest as BT
from quant.book import Book
from quant.feed import SnapshotPoller
from quant.universe import us_symbol

STATE = {"sip_403": False, "calls": 0, "all_400": False}


class Alpaca(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        STATE["calls"] += 1
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        syms = q["symbols"][0].split(",")
        if STATE["all_400"]:
            return self._send(400, {"message": "invalid start"})
        if STATE["sip_403"] and q["feed"][0] == "sip":
            return self._send(403, {"message": "subscription does not permit querying recent SIP data"})
        if "ESZ6" in syms:
            return self._send(400, {"message": "invalid symbol: ESZ6"})
        if "QQ9X" in syms:
            return self._send(400, {"message": "bad request"})
        page = q.get("page_token", [None])[0]
        t = "2026-10-30T13:30:00Z" if not page else "2026-10-30T13:31:00Z"
        body = {"bars": {s: [{"t": t, "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 100}] for s in syms}, "next_page_token": None if page else "p2"}
        self._send(200, body)

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


class FetchDay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Alpaca)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        BT.BARS = f"http://127.0.0.1:{cls.srv.server_port}/v2/stocks/bars"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        STATE.update(sip_403=False, calls=0, all_400=False)

    def test_not_a_symbol_problem_stops_after_one_probe(self):
        STATE["all_400"] = True
        bad = {}
        out = BT.fetch_day(["AAPL", "MSFT", "XLK"], date(2026, 10, 30), 960, "k", "s", bad, chunk=100)
        self.assertEqual(out, {})
        self.assertEqual(STATE["calls"], 1, "one SPY probe, then stop - no bisecting a request-level error")
        self.assertEqual(bad, {}, "no symbol blamed")

    def test_named_and_unnamed_bad_symbols_are_isolated(self):
        syms = ["AAPL", "MSFT", "ESZ6", "BRK-B", "QQ9X", "XLK", "NVDA"]
        bad = {}
        out = BT.fetch_day(syms, date(2026, 10, 30), 960, "k", "s", bad, chunk=100)
        self.assertEqual(sorted(out), ["AAPL", "BRK-B", "MSFT", "NVDA", "XLK"])
        self.assertEqual(sorted(bad), ["ESZ6", "QQ9X"])
        self.assertIn("invalid symbol", bad["ESZ6"])
        self.assertEqual(len(out["AAPL"]), 2, "both pages of bars kept")

    def test_sip_refused_falls_back_to_iex(self):
        STATE["sip_403"] = True
        out = BT.fetch_day(["AAPL", "XLK"], date(2026, 10, 30), 960, "k", "s", {}, chunk=100)
        self.assertEqual(sorted(out), ["AAPL", "XLK"])

    def test_known_bad_symbols_are_not_asked_again(self):
        bad = {"ESZ6": "invalid symbol: ESZ6"}
        BT.fetch_day(["AAPL", "ESZ6"], date(2026, 10, 30), 960, "k", "s", bad, chunk=100)
        self.assertEqual(STATE["calls"], 4, "SPY probe (2 pages) + one request (2 pages) - ESZ6 never sent again")


class Poller(unittest.TestCase):
    def test_snapshot_poller_drops_a_refused_symbol(self):
        p = SnapshotPoller(Book(), ["AAPL", "ESZ6", "MSFT"], "iex", "k", "s", 4)

        def fetch(syms):
            if "ESZ6" in syms:
                raise urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(b'{"message":"invalid symbol: ESZ6"}'))
            return {s: {"latestTrade": {"p": 10.0, "s": 1, "t": "2026-10-30T14:00:00Z"}} for s in syms}
        p.fetch = fetch
        got = p.fetch_safe(["AAPL", "ESZ6", "MSFT"])
        self.assertEqual(sorted(got), ["AAPL", "MSFT"])
        self.assertEqual(list(p.refused), ["ESZ6"])
        self.assertEqual(sorted(p.fetch_safe(["AAPL", "ESZ6", "MSFT"])), ["AAPL", "MSFT"])


class Universe(unittest.TestCase):
    def test_only_us_symbols(self):
        self.assertTrue(all(us_symbol(t) for t in ("AAPL", "BRK-B", "XLK", "V")))
        self.assertFalse(any(us_symbol(t) for t in ("ESZ6", "CASH_USD", "-", "", "BRK.B", "^VIX", "EURUSD=X", "BTC-USD", "ABCDEF")))


if __name__ == "__main__":
    unittest.main()
