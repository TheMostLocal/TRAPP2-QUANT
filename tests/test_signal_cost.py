# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-QUANT
#  >>> FILE PATH:   tests/test_signal_cost.py
#  z186: signal vs cost - the record says whether a losing strategy has no signal or a signal its costs eat.
# ============================================================
import random
import unittest

from quant.stats import signal_vs_cost, summarize


def trade(side, entry_mid, exit_mid, cost_bps):
    m = (1 if side == "long" else -1) * (exit_mid / entry_mid - 1) * 1e4
    return {"side": side, "entry_mid": entry_mid, "exit_mid": exit_mid, "net_bps": m - cost_bps}


class SignalVsCost(unittest.TestCase):
    def test_decomposition(self):
        t = [trade("long", 100.0, 100.10, 4.0), trade("short", 50.0, 49.95, 4.0)]
        out = signal_vs_cost(t)
        self.assertAlmostEqual(out["signalBps"], 10.0, places=6)          # +10 bps each, in the trade's direction
        self.assertAlmostEqual(out["costBps"], 4.0, places=6)

    def test_diagnoses(self):
        rnd = random.Random(1)
        noise = [trade("long", 100.0, 100.0 * (1 + rnd.gauss(0, 0.002)), 4.0) for _ in range(2000)]
        self.assertEqual(signal_vs_cost(noise)["diagnosis"], "no signal")
        small = [trade("long", 100.0, 100.0 * (1 + 0.0003 + rnd.gauss(0, 0.001)), 6.0) for _ in range(2000)]
        self.assertEqual(signal_vs_cost(small)["diagnosis"], "signal smaller than cost")
        edge = [trade("long", 100.0, 100.0 * (1 + 0.0010 + rnd.gauss(0, 0.001)), 4.0) for _ in range(2000)]
        self.assertEqual(signal_vs_cost(edge)["diagnosis"], "edge")

    def test_summary_carries_it_and_old_rows_dont_break(self):
        s = summarize([trade("long", 100.0, 100.2, 5.0), trade("long", 100.0, 100.1, 5.0)])
        self.assertIn("signalBps", s)
        self.assertEqual(signal_vs_cost([{"net_bps": 1.0}, {"net_bps": 2.0}]), {})
        self.assertNotIn("signalBps", summarize([{"net_bps": 1.0}, {"net_bps": 2.0}]))


if __name__ == "__main__":
    unittest.main()
