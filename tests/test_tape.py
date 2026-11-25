"""z202: the market tape in the quant book (quant/tape.py) - the sell-off read as known at the open ("mkt", six of
Today's Trends' seven criteria from daily histories, no look-ahead) and the S&P 500 at the minute ("spx", SPY vs its
prior close); both tag every signal and are lab segments; a "mkt:" segment needs the state to vary in both halves;
the live Today's Trends read is recorded, never segmented; the ledger reports by market state."""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from quant import lab as LAB
from quant import tape
from quant.config import Config
from quant.engine import Engine
from quant.segments import keys_for, tags_for
from sim_harness import FakeClock, FakeFeed, universe


def weekdays(n, end=date(2026, 11, 20)):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return list(reversed(out))


def hist_calm_then_selloff():
    """300 sessions: a steady climb with small wiggles to a high, then 7 straight lower closes (-11 %) on rising volume;
    the VIX 15 -> 36, HYG down 2 % in the last week."""
    ds = weekdays(300)
    cs, vs = [], []
    px = 5000.0
    for i in range(293):
        px *= 1.0015 if i % 3 else 0.9995
        cs.append(round(px, 2))
        vs.append(4e9 + (i % 5) * 1e7)
    for k in range(7):
        px *= 0.983
        cs.append(round(px, 2))
        vs.append(5e9 + k * 2e8)
    vix = [15.0] * 293 + [17, 19, 22, 25, 28, 32, 36]
    hyg = [80.0] * 294 + [79.8, 79.5, 79.2, 78.9, 78.6, 78.3]
    spy = [round(c / 10, 2) for c in cs]
    rows = lambda vals, vols=None: [{"date": d, "close": v, "volume": (vols[i] if vols else 0)} for i, (d, v) in enumerate(zip(ds, vals))]
    return {"spx": tape.series(rows(cs, vs)), "vix": tape.series(rows(vix)), "hyg": tape.series(rows(hyg)), "spy": tape.series(rows(spy))}, ds


class TapeState(unittest.TestCase):
    def setUp(self):
        self.H, self.ds = hist_calm_then_selloff()

    def test_calm_before_the_selloff_selloff_after(self):
        calm = tape.state_on(self.ds[-8], self.H)          # the open of the first down day: completed closes only
        self.assertEqual(calm["state"], "calm", calm)
        self.assertGreater(calm["run"], -4, "no run of 4+ lower closes yet")
        late = tape.state_on("2026-11-23", self.H)         # Monday's open: through Friday's close
        self.assertEqual(late["asOf"], "2026-11-20")
        self.assertEqual(late["state"], "selloff", late)
        self.assertEqual(late["run"], -7)
        c = late["criteria"]
        self.assertTrue(c["run"] and c["drawdown"] and c["trend"] and c["fear"] and c["credit"], c)
        self.assertLess(late["dd"], -10)
        self.assertEqual(late["of"], 6, "six criteria - breadth is recorded live, not computed here")

    def test_no_look_ahead(self):
        d = self.ds[-3]                                     # the read at this session's open...
        before = tape.state_on(d, self.H)
        self.assertEqual(before["asOf"], self.ds[-4])
        trimmed = {k: tuple(list(x)[: list(v[0]).index(d)] for x in v) if d in v[0] else v for k, v in self.H.items()}
        self.assertEqual(tape.state_on(d, trimmed), before, "...is the same with or without that day's own close on file")

    def test_too_little_or_stale_history(self):
        self.assertIsNone(tape.state_on("2026-12-15", self.H), "the history stops 3 weeks earlier: no state")
        short = {k: tuple(list(x)[:40] for x in v) for k, v in self.H.items()}
        self.assertIsNone(tape.state_on(self.ds[41], short))

    def test_prior_close_and_day_bucket(self):
        self.assertEqual(tape.prior_close(self.H["spy"], "2026-11-23"), self.H["spy"][1][-1])
        self.assertIsNone(tape.prior_close(self.H["spy"], "2026-12-15"))
        self.assertEqual([tape.day_bucket(x) for x in (-0.012, -0.0075, -0.002, 0.0, 0.0074, 0.0075, None)],
                         ["dn", "dn", "flat", "flat", "flat", "up", None])

    def test_trends_now_and_describe(self):
        doc = {"date": "2026-11-20", "etTime": "19:30", "state": "final", "read": {"label": "heavy sell-off · correction", "state": "heavy sell-off",
               "overlay": "correction", "on": 6, "of": 7}, "indices": {"^GSPC": {"chg": -3.7, "streak": {"dir": "down", "n": 7, "through": "2026-11-20"}}},
               "internals": {"pctUp": 24.5}}
        now = tape.trends_now(doc)
        self.assertEqual((now["label"], now["run"], now["on"], now["pctUp"], now["session"]), ("heavy sell-off · correction", -7, 6, 24.5, "final"))
        self.assertIsNone(tape.trends_now({"nothing": 1}))
        m = dict(tape.state_on("2026-11-23", self.H), spyPrev=self.H["spy"][1][-1], now=now)
        line = tape.describe(m)
        self.assertIn("SELLOFF", line)
        self.assertIn("today's trends: heavy sell-off · correction", line)
        self.assertIn("no state", tape.describe({}))

    def test_current_offline(self):
        def fetch(url):
            key = {"%5EGSPC": "spx", "%5EVIX": "vix", "HYG": "hyg", "SPY": "spy"}
            for k, v in key.items():
                if url.endswith(f"/{k}.json"):
                    s = self.H[v]
                    return [{"date": d, "close": c, "volume": vv} for d, c, vv in zip(*s)]
            return None
        m = tape.current(date(2026, 11, 23), fetch=fetch)
        self.assertEqual((m["state"], m["run"]), ("selloff", -7))
        self.assertAlmostEqual(m["spyPrev"], self.H["spy"][1][-1])
        self.assertIsNone(m["now"])                         # the trends doc wasn't served


class MacroBookOut(unittest.TestCase):
    """z206 - 11-24: every TRAPP2-1 file 404. The tape reads SPY (as the S&P 500, with its volume) and HYG from TRAPP2;
    no VIX - that criterion isn't counted; 'src' says what stood in."""

    def setUp(self):
        self.H, self.ds = hist_calm_then_selloff()

    def test_fallback_to_trapp2(self):
        def fetch(url):
            if "/TRAPP2-1/" in url:
                return None
            for k, v in (("SPY", "spy"), ("HYG", "hyg")):
                if url.endswith(f"/TRAPP2/main/data/history/{k}.json"):
                    s = self.H["spx"] if v == "spy" else self.H[v]        # SPY moves as the index does here
                    return [{"date": d, "close": c / (10 if v == "spy" else 1), "volume": vv} for d, c, vv in zip(*s)]
            return None
        H = tape.load_histories(fetch)
        self.assertEqual(H["src"], {"spy": "TRAPP2 SPY", "hyg": "TRAPP2 HYG", "spx": "SPY (TRAPP2)"})
        self.assertEqual(H["vix"], ([], [], []))
        st = tape.state_on("2026-11-23", H)
        self.assertEqual((st["state"], st["run"], st["of"]), ("selloff", -7, 5))       # no VIX: five criteria known
        self.assertNotIn("fear", st["criteria"])
        m = tape.current(date(2026, 11, 23), H=H, fetch=lambda url: None)
        self.assertEqual(m["src"]["spx"], "SPY (TRAPP2)")
        self.assertIn("read on SPY (TRAPP2) - the macro book (TRAPP2-1) is out", tape.describe(m))

    def test_the_macro_book_present_no_src(self):
        def fetch(url):
            key = {"%5EGSPC": "spx", "%5EVIX": "vix", "HYG": "hyg", "SPY": "spy"}
            for k, v in key.items():
                if url.endswith(f"/TRAPP2-1/main/data/history/{k}.json"):
                    return [{"date": d, "close": c, "volume": vv} for d, c, vv in zip(*self.H[v])]
            return None
        self.assertNotIn("src", tape.load_histories(fetch))

    def test_trends_now_carries_the_positioning(self):
        doc = {"date": "2026-11-24", "etTime": "11:15", "state": "intraday", "read": {"label": "sell-off", "state": "sell-off", "on": 3, "of": 6},
               "indices": {"^GSPC": {"chg": -1.23}}, "proxy": {"^GSPC": "SPY"},
               "flows": {"date": "2026-11-24", "label": "risk-off rotation", "z": {"si": 0.0, "mom": -1.2, "rev": 0.2, "beta": -2.5},
                         "flags": {"riskOff": True}, "after": {"prior": "2026-11-23"}}}
        now = tape.trends_now(doc)
        self.assertEqual((now["flow"], now["flowFlags"], now["flowAfter"], now["proxy"]), ("risk-off rotation", ["riskOff"], "2026-11-23", "SPY"))
        self.assertEqual(now["flowZ"]["beta"], -2.5)
        doc["flows"]["date"] = "2026-11-23"                  # a flows block from another session: not today's
        self.assertIsNone(tape.trends_now(doc)["flow"])
        ctx = SimpleNamespace(minute=700, liquidity={"AAPL": 5e9}, sector_etf={"AAPL": "XLK"}, kinds={"AAPL": "stock"}, quad={},
                              mkt={"state": "selloff", "run": 1, "now": tape.trends_now(dict(doc, flows=dict(doc["flows"], date="2026-11-24")))}, spx_move=-0.012)
        t = tags_for("AAPL", "long", ctx)
        self.assertEqual(t["flowNow"], "risk-off rotation")
        self.assertFalse(any("flow" in k for k in keys_for(t)), "recorded, not segmented")


class Tags(unittest.TestCase):
    def test_tags_and_segment_keys(self):
        ctx = SimpleNamespace(minute=700, liquidity={"AAPL": 5e9}, sector_etf={"AAPL": "XLK"}, kinds={"AAPL": "stock"}, quad={"official": 3},
                              mkt={"state": "selloff", "run": -7, "now": {"label": "heavy sell-off · correction"}}, spx_move=-0.0123)
        t = tags_for("AAPL", "long", ctx)
        self.assertEqual((t["mkt"], t["spx"], t["mktRun"], t["spxBps"], t["mktNow"]), ("selloff", "dn", -7, -123.0, "heavy sell-off · correction"))
        ks = keys_for(t)
        for k in ("mkt:selloff", "spx:dn", "side:long|mkt:selloff", "mkt:selloff|spx:dn", "liq:mega|spx:dn"):
            self.assertIn(k, ks)
        self.assertFalse(any(x in k for k in ks for x in ("mktNow", "mktRun", "spxBps")), "recorded, not segmented")

    def test_no_tape_no_tags(self):
        ctx = SimpleNamespace(minute=700, liquidity={}, sector_etf={}, kinds={}, quad={})
        t = tags_for("AAPL", "short", ctx)
        self.assertFalse({"mkt", "spx", "mktNow"} & set(t))


class Lab(unittest.TestCase):
    def test_mkt_segment_needs_the_state_to_vary(self):
        acc = {}
        seg = "side:long|mkt:selloff"
        import random
        r = random.Random(3)
        for half, n in (("train", 40), ("test", 20)):
            for k in range(n):
                a = acc.setdefault(("sector_reversion", LAB.vid_of("sector_reversion", LAB.GRIDS["sector_reversion"][0]), seg, half), LAB.Acc())
                for j in range(8):
                    a.add(5.0 + r.gauss(0, 3), f"{half}-{k}")
        calendar = {"train": {"calm": 34, "pressure": 6}, "test": {"selloff": 14, "pressure": 6}}   # no sell-off in training: the calendar
        c = LAB.select(acc, ["sector_reversion"], n_test=20, mkt_days=calendar)[0]
        self.assertEqual(c["marketDependent"], "selloff")
        self.assertFalse(c["approved"])
        self.assertTrue(any("market read is the calendar" in x for x in c["reasons"]), c["reasons"])
        varied = {"train": {"calm": 25, "selloff": 15}, "test": {"selloff": 9, "calm": 11}}
        self.assertTrue(LAB.select(acc, ["sector_reversion"], n_test=20, mkt_days=varied)[0]["approved"])
        self.assertFalse(LAB.select(acc, ["sector_reversion"], n_test=20, mkt_days=None)[0]["approved"], "no record of the read: not approved")
        m = LAB.build_models(acc, ["sector_reversion"], ["2026-08-01", "2026-09-30"], ["2026-10-01", "2026-10-30"], mkt_days=varied)
        self.assertEqual(m["mktSessions"], varied)
        self.assertEqual(m["strategies"]["sector_reversion"]["live"][0]["marketDependent"], "selloff")
        self.assertIn("marketSegments", m["rules"])

    def test_spx_segment_has_no_calendar_rule(self):
        self.assertIsNone(LAB.mkt_of("side:long|spx:dn"))
        self.assertEqual(LAB.mkt_of("mkt:calm"), "calm")

    def test_tag_market_on_events(self):
        H, ds = hist_calm_then_selloff()
        prev = tape.prior_close(H["spy"], "2026-11-23")
        spy = [prev * 0.995] * 100 + [prev * 0.985] * 100            # flat-ish at first, then -1.5 %
        ev = [{"i": 50, "tags": {"side": "long"}}, {"i": 150, "tags": {"side": "short"}}]
        ms = LAB.tag_market(ev, {"SPY": spy}, "2026-11-23", H)
        self.assertEqual(ms["state"], "selloff")
        self.assertEqual([(e["tags"]["mkt"], e["tags"]["spx"]) for e in ev], [("selloff", "flat"), ("selloff", "dn")])
        ev2 = [{"i": 5, "tags": {}}]
        LAB.tag_market(ev2, {}, "2026-11-23", H)                     # no SPY bars that day: mkt only
        self.assertEqual(ev2[0]["tags"], {"mkt": "selloff"})


class EngineTape(unittest.TestCase):
    """A simulated session with the tape on: every trade carries it, the summary reports by it, the state shows it."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_session_trades_carry_the_tape(self):
        with open(os.path.join(self.d, "gate.json"), "w") as f:
            json.dump({"strategies": {"sector_reversion": {"pass": True}, "etf_arb": {"pass": True}, "opening_range": {"pass": True}}}, f)
        uni = dict(universe(), mkt={"state": "selloff", "on": 5, "of": 6, "run": -7, "asOf": "2026-10-23", "spyPrev": 610.0,
                                    "now": {"label": "heavy sell-off · correction"}})
        clock = FakeClock(datetime(2026, 10, 26, 13, 30, tzinfo=timezone.utc).timestamp())
        eng = Engine(replace(Config(), execution="sim"), uni, clock=clock, data_dir=self.d, log=lambda *a: None)
        calls = []
        eng.refresh_now = lambda: calls.append(1) or {"label": "sell-off"}
        feed = FakeFeed(eng, clock)

        async def fake_sleep(dt):
            clock.t += dt
            feed.step()
            await asyncio.sleep(0)
        asyncio.run(eng.run(until_epoch=datetime(2026, 10, 26, 20, 3, tzinfo=timezone.utc).timestamp(), feeds=feed, sleep=fake_sleep))
        self.assertTrue(eng.ledger.closed)
        for t in eng.ledger.closed:
            tg = t["tags"]
            self.assertEqual(tg["mkt"], "selloff")
            self.assertEqual(tg["spx"], "dn", "SPY ~600 against a 610 prior close")
            self.assertIn(tg["mktNow"], ("heavy sell-off · correction", "sell-off"))
            self.assertEqual(tg["mktRun"], -7)
        self.assertGreaterEqual(len(calls), 10, "the trends read refreshed every 30 minutes")
        with open(os.path.join(self.d, "summary.json")) as f:
            summ = json.load(f)
        self.assertEqual(summ["byMarket"]["mkt"]["selloff"]["live"]["n"], len(eng.ledger.closed))
        self.assertEqual(summ["byMarket"]["spx"]["dn"]["live"]["n"], len(eng.ledger.closed))
        st = eng.state(clock.t)
        self.assertEqual((st["mkt"]["state"], st["mkt"]["run"]), ("selloff", -7))
        self.assertLess(st["spxBps"], -50, "SPY below its 610 prior close")

    def test_no_tape_still_trades(self):
        uni = universe()
        clock = FakeClock(datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc).timestamp())
        eng = Engine(replace(Config(), execution="sim"), uni, clock=clock, data_dir=self.d, log=lambda *a: None)
        self.assertIsNone(eng.spx_move())
        ctx = eng.ctx(clock.t)
        self.assertEqual(ctx.mkt, {})


if __name__ == "__main__":
    unittest.main()
