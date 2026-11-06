"""z175: the lab finds an edge that lives in one segment, refuses the segments where it doesn't, controls false
discoveries, and the engine then trades that segment live and everything else in shadow."""
import json
import math
import os
import random
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quant import lab as LAB
from quant.config import Config
from quant.engine import Engine
from quant.segments import keys_for, liq_bucket, matches, tod_bucket
from quant.strategies import Signal

MID = [f"M{i}" for i in range(12)]          # $100M a day: their jumps against the sector come back
MEGA = [f"G{i}" for i in range(12)]         # $2B a day: their jumps keep going
UNI = {"symbols": ["XLK"] + MID + MEGA, "sector_etf": {s: "XLK" for s in MID + MEGA}, "etf_weights": {}, "momentum": [],
       "liquidity": dict({s: 1e8 for s in MID}, **{s: 2e9 for s in MEGA}, XLK=5e9)}


def session(seed, d):
    """One synthetic session of 1-minute bars: a sector factor, noise, and every 20 minutes a one-minute 120 bps jump in
    one stock - coming back over the next 25 minutes for mid names, running further for mega names."""
    rnd = random.Random(seed)
    t_open = datetime(d.year, d.month, d.day, 13, 30, tzinfo=timezone.utc).timestamp()
    px = {s: 100.0 for s in UNI["symbols"]}
    bars = {s: [] for s in px}
    shocks = {}
    for k, start in enumerate(range(60, 360, 20)):
        s = (MID + MEGA)[(seed * 7 + k) % 24]
        shocks[s] = shocks.get(s, []) + [(start, 1 if rnd.random() < 0.5 else -1)]
    for i in range(390):
        f = rnd.gauss(0, 0.0005)
        for s in px:
            r = (1.0 if s == "XLK" else 1.0) * f + rnd.gauss(0, 0.0003 if s != "XLK" else 0.0001)
            for (st, sg) in shocks.get(s, []):
                if i == st:
                    r += sg * 0.012                          # a 120 bps jump in one minute
                elif st < i < st + 26:
                    r += (-sg if s in MID else sg) * 0.0004  # mid: back 100 bps over 25 min; mega: on another 100
            o = px[s]
            px[s] *= math.exp(r)
            bars[s].append((int(t_open + 60 * i), o, max(o, px[s]), min(o, px[s]), px[s], 1000))
    return bars


def run_offline(n=30):
    days, d = [], date(2026, 9, 1)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    cut = round(n * 2 / 3)
    acc = {}
    for k, d in enumerate(days):
        ev, closes, rets = LAB.session_events(UNI, session(k, d), d, 960)
        for st, vid, net, tags in LAB.score_events(ev, closes, rets, UNI):
            for seg in keys_for(tags):
                acc.setdefault((st, vid, seg, "train" if k < cut else "test"), LAB.Acc()).add(net)
    return LAB.build_models(acc, ["sector_reversion"], [d.isoformat() for d in days[:cut]], [d.isoformat() for d in days[cut:]])


class Segments(unittest.TestCase):
    def test_buckets_keys_matching(self):
        self.assertEqual([liq_bucket(x) for x in (3e9, 5e8, 1e8, 1e7, None)], ["mega", "large", "mid", "small", "unknown"])
        self.assertEqual([tod_bucket(m) for m in (600, 700, 900)], ["open", "mid", "late"])
        tags = {"liq": "mid", "tod": "mid", "side": "long", "sector": "XLK"}
        ks = keys_for(tags)
        self.assertEqual(len(ks), 1 + 4 + 6)
        self.assertIn("liq:mid|tod:mid", ks)
        self.assertTrue(matches(tags, "liq:mid|side:long") and matches(tags, "all") and not matches(tags, "liq:mega"))

    def test_benjamini_hochberg(self):
        q = LAB.benjamini_hochberg([0.01, 0.04, 0.03, 0.20])
        self.assertEqual([round(x, 4) for x in q], [0.04, 0.0533, 0.0533, 0.2])


class Lab(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.models = run_offline()

    def test_finds_the_segment_with_the_edge_and_only_that(self):
        m = self.models["strategies"]["sector_reversion"]
        live = m["live"]
        self.assertTrue(live, "an edge planted in mid-size names must be found")
        self.assertTrue(all("liq:mid" in a["segment"] for a in live), [a["segment"] for a in live])
        self.assertFalse(any("mega" in a["segment"] for a in live))
        for a in live:
            self.assertGreaterEqual(a["test"]["n"], LAB.MIN_TEST)
            self.assertGreater(a["test"]["meanBps"], LAB.MIN_EXP_BPS)
            self.assertLessEqual(a["q"], LAB.FDR)
            self.assertEqual(a["params"], m["variants"][a["variant"]])

    def test_the_average_still_loses_and_mega_fails(self):
        rep = {c["segment"]: c for c in self.models["strategies"]["sector_reversion"]["report"]}
        mega = [c for k, c in rep.items() if "liq:mega" in k]
        self.assertTrue(all(not c["approved"] for c in mega))


class EngineUsesModels(unittest.TestCase):
    def test_live_only_in_approved_segments(self):
        d = tempfile.mkdtemp()
        vid = LAB.vid_of("sector_reversion", {"z_in": 2.5, "max_hold": 30, "vol_spike_max": None})
        doc = {"strategies": {"sector_reversion": {"variants": {vid: {"z_in": 2.5, "max_hold": 30, "vol_spike_max": None}},
                                                   "live": [{"segment": "liq:mid|tod:mid", "variant": vid}]}}}
        with open(os.path.join(d, "models.json"), "w") as f:
            json.dump(doc, f)
        e = Engine(replace(Config(), execution="sim"), UNI, clock=lambda: 1793026800, data_dir=d, log=lambda *a: None)
        variants = sorted((s.name, s.variant) for s in e.strategies if s.name == "sector_reversion")
        self.assertEqual(variants, [("sector_reversion", "default"), ("sector_reversion", vid)])
        mk = lambda v, liq, tod: Signal("M1", "long", "sector_reversion", 20, 40, 30, variant=v, tags={"liq": liq, "tod": tod, "side": "long", "sector": "XLK"})
        self.assertTrue(e.live_signal(mk(vid, "mid", "mid")))
        self.assertFalse(e.live_signal(mk(vid, "mid", "open")), "approved variant outside its segment")
        self.assertFalse(e.live_signal(mk("default", "mid", "mid")), "the original rules stay in shadow")
        self.assertTrue(e.live_for("sector_reversion") and not e.live_for("etf_arb"))


if __name__ == "__main__":
    unittest.main()
