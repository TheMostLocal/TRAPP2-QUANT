import json
import math
import os
import random
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from quant import backtest as BT
from quant.config import Config
from quant.engine import Engine
from sim_harness import run_session, universe

ALL_PASS = {"sector_reversion": {"pass": True}, "etf_arb": {"pass": True}, "opening_range": {"pass": True}}


def cfg():
    return replace(Config(), execution="sim")


def rd(p, raw=False):
    with open(p) as f:
        return f.read() if raw else json.load(f)


class EngineSession(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_full_session_all_live(self):
        eng, s = run_session(Engine, cfg(), self.d, gate=ALL_PASS)
        strategies = {t["strategy"] for t in eng.ledger.closed}
        self.assertEqual(strategies, {"sector_reversion", "etf_arb", "opening_range"})
        self.assertEqual(eng.positions, [])                                         # flat after 1555
        self.assertTrue(all(t["mode"] == "live" for t in eng.ledger.closed))
        late = [t for t in eng.ledger.closed if t["reason_out"].startswith("flatten")]
        for t in late:
            self.assertGreaterEqual(t["exit_ts"], "2026-10-26T19:55:00+00:00")
        ents = [t["entry_ts"] for t in eng.ledger.closed]
        self.assertTrue(min(ents) >= "2026-10-26T13:40:00+00:00" and max(ents) <= "2026-10-26T19:41:00+00:00")   # 0940-1540 ET
        sr = [t for t in eng.ledger.closed if t["strategy"] == "sector_reversion" and t["symbol"] == "AAPL" and "14:3" in t["entry_ts"]]
        self.assertTrue(sr and sr[0]["side"] == "short", "the 1030 AAPL jump is faded")
        for t in eng.ledger.closed:                                                   # ledger arithmetic
            sign = 1 if t["side"] == "long" else -1
            gross = t["qty"] * (t["exit_px"] - t["entry_px"]) * sign
            self.assertAlmostEqual(t["pnl"], round(gross - t["fee"], 2), delta=0.011)
        files = os.listdir(os.path.join(self.d, "trades"))
        self.assertEqual(files, ["2026-10-26.jsonl"])
        n_file = len(rd(os.path.join(self.d, "trades", "2026-10-26.jsonl"), raw=True).splitlines())
        self.assertEqual(n_file, len(eng.ledger.closed))
        summ = rd(os.path.join(self.d, "summary.json"))
        self.assertEqual(summ["live"]["n"], len(eng.ledger.closed))
        self.assertEqual(rd(os.path.join(self.d, "open.json"))["positions"], [])

    def test_no_gate_means_shadow(self):
        eng, s = run_session(Engine, cfg(), self.d, gate=None)
        self.assertTrue(eng.ledger.closed)
        self.assertTrue(all(t["mode"] == "shadow" for t in eng.ledger.closed))
        self.assertEqual(eng.ledger.pnl, 0.0)                                         # shadow P&L never counts toward the book

    def test_job_handoff_keeps_positions(self):
        stop = datetime(2026, 10, 26, 14, 31, 30, tzinfo=timezone.utc)                # job 1 ends 1031:30 ET, mid-trade
        eng1, _ = run_session(Engine, cfg(), self.d, gate=ALL_PASS, until_utc=stop)
        held = [p.symbol for p in eng1.positions]
        saved = rd(os.path.join(self.d, "open.json"))
        self.assertEqual([p["symbol"] for p in saved["positions"]], held)
        eng2, _ = run_session(Engine, cfg(), self.d, gate=ALL_PASS, start_utc=stop)
        self.assertEqual(eng2.ledger.pnl != 0 or not held, True)
        closed_ids = {t["id"] for t in eng2.ledger.closed}
        self.assertTrue({p["id"] for p in saved["positions"]} <= closed_ids, "positions from job 1 are closed by job 2")
        lines = rd(os.path.join(self.d, "trades", "2026-10-26.jsonl"), raw=True).splitlines()
        self.assertEqual(len(lines), len({json.loads(l)["id"] for l in lines}), "no trade written twice")


class Backtest(unittest.TestCase):
    def test_replay_day_and_gate(self):
        rnd = random.Random(11)
        d = date(2026, 10, 23)
        t_open = datetime(2026, 10, 23, 13, 30, tzinfo=timezone.utc).timestamp()
        px = {s: p for s, p in (("XLK", 250.0), ("AAPL", 230.0), ("NVDA", 180.0), ("MSFT", 500.0), ("SPY", 600.0))}
        bars = {s: [] for s in px}
        for i in range(390):
            f = rnd.gauss(0, 0.0006)
            for s in px:
                b = {"XLK": 1.0, "AAPL": 1.1, "NVDA": 1.4, "MSFT": 0.9, "SPY": 0.7}[s]
                r = b * f + rnd.gauss(0, 0.0004)
                if s == "AAPL" and 60 <= i < 66:
                    r += 0.0012
                if s == "AAPL" and 66 <= i < 86:
                    r -= 0.0003
                o = px[s]
                px[s] *= math.exp(r)
                bars[s].append((int(t_open + 60 * i), o, max(o, px[s]), min(o, px[s]), px[s], 1000))
        tr = BT.replay_day(cfg(), universe(), bars, d, 16 * 60)
        self.assertTrue(tr)
        self.assertTrue(all(t["mode"] == "backtest" for t in tr))
        self.assertTrue(all(t["exit_ts"] <= "2026-10-23T19:55:59+00:00" for t in tr))
        c = replace(cfg(), gate_min_trades=3, gate_min_tstat=-99, gate_min_expectancy_bps=-999)
        g = BT.gate(tr, c)
        self.assertEqual(set(g), set(c.strategies))
        strict = BT.gate(tr, cfg())
        self.assertTrue(all(not v["pass"] for v in strict.values()), "one synthetic day can't clear 200 trades / t >= 2")

    def test_sessions_skip_weekends_and_holidays(self):
        days = BT.sessions(3, today=date(2026, 11, 30))                              # Monday after Thanksgiving week
        self.assertEqual([(d.isoformat(), c) for d, c in days], [("2026-11-24", 960), ("2026-11-25", 960), ("2026-11-27", 780)])   # no Thanksgiving; Friday closes 1300


if __name__ == "__main__":
    unittest.main()
