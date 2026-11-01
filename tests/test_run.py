import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import quant.run as R

U = lambda *a: datetime(*a, tzinfo=timezone.utc)


class StartTiming(unittest.TestCase):
    """z169: when a job should trade, wait, bridge the night, or stop for the weekend."""

    def test_in_session_and_before_the_open(self):
        self.assertEqual(R.seconds_to_start(U(2026, 10, 30, 14, 0)), 0)                 # Fri 1000 EDT
        self.assertEqual(R.seconds_to_start(U(2026, 11, 2, 13, 0)), 80 * 60)            # Mon 0800 EST -> 0920

    def test_weeknight_is_bridged_weekend_and_holiday_are_not(self):
        w = R.seconds_to_start(U(2026, 11, 3, 23, 0))                                   # Tue 1800 EST -> Wed 0920
        self.assertEqual(w, (15 * 60 + 20) * 60)
        self.assertLessEqual(w, R.BRIDGE_MAX_H * 3600)
        self.assertGreater(R.seconds_to_start(U(2026, 10, 31, 14, 0)), R.BRIDGE_MAX_H * 3600)    # Saturday
        self.assertGreater(R.seconds_to_start(U(2026, 11, 25, 21, 30)), R.BRIDGE_MAX_H * 3600)   # Thanksgiving eve

    def test_missing_keys_stop_at_once(self):
        saved = {k: os.environ.pop(k, None) for k in ("ALPACA_KEY_ID", "ALPACA_SECRET_KEY")}
        try:
            self.assertEqual(R.main(), 2)
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
