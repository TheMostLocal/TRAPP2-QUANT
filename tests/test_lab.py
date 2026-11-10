"""z175: the lab finds an edge that lives in one segment, refuses the segments where it doesn't, controls false
discoveries, and the engine then trades that segment live and everything else in shadow.
z180 (lab v2): judged by session - clustered statistics, enough sessions, no single day carrying it, quad segments
only where the quad varies, quad-split approvals live only while the regime is dialed in; older verdicts untrusted."""
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
                acc.setdefault((st, vid, seg, "train" if k < cut else "test"), LAB.Acc()).add(net, d.isoformat())
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
            self.assertGreaterEqual(a["test"]["sessions"], LAB.MIN_TEST_SESSIONS)        # z180
            self.assertGreaterEqual(a["train"]["tCl"], LAB.MIN_TRAIN_T)
            self.assertGreater(a["test"]["dropBestBps"], 0)
        self.assertEqual(self.models["labVersion"], LAB.LAB_VERSION)

    def test_the_average_still_loses_and_mega_fails(self):
        rep = {c["segment"]: c for c in self.models["strategies"]["sector_reversion"]["report"]}
        mega = [c for k, c in rep.items() if "liq:mega" in k]
        self.assertTrue(all(not c["approved"] for c in mega))


class EngineUsesModels(unittest.TestCase):
    def test_live_only_in_approved_segments(self):
        d = tempfile.mkdtemp()
        vid = LAB.vid_of("sector_reversion", {"z_in": 2.5, "max_hold": 30, "vol_spike_max": None})
        doc = {"labVersion": 2, "strategies": {"sector_reversion": {"variants": {vid: {"z_in": 2.5, "max_hold": 30, "vol_spike_max": None}},
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




def _acc(days, per_day, seed=1, shocks=None, base=0.0, sd=10.0):
    """An Acc over `days` sessions, `per_day` trades each, plus an optional per-session common shock {day index: bps}."""
    rnd, a = random.Random(seed), LAB.Acc()
    for k in range(days):
        common = (shocks or {}).get(k, 0.0)
        for _ in range(per_day):
            a.add(base + common + rnd.gauss(0, sd), f"d{k}")
    return a


class Stats(unittest.TestCase):
    """z180: the arithmetic the verdict now rests on."""

    def test_student_t_tail(self):
        for t, df, p in ((2.0, 10, 0.036694), (1.812461, 10, 0.05), (3.0, 5, 0.015050), (1.0, 1, 0.25), (2.262157, 9, 0.025),
                         (0.0, 7, 0.5), (-2.0, 10, 0.963306), (5.0, 3, 0.0076963), (1.96, 10 ** 6, 0.0249979)):
            self.assertAlmostEqual(LAB.p_one_sided(t, df), p, delta=2e-5, msg=(t, df))
        self.assertAlmostEqual(LAB.p_one_sided(1.96), 0.024998, delta=1e-5)          # no df = the normal (the old arithmetic)

    def test_cluster_robust_matches_brute_force(self):
        rnd, a, clusters = random.Random(3), LAB.Acc(), []
        for d in range(15):
            k, shock = rnd.randint(1, 40), rnd.gauss(0, 8)
            xs = [shock + rnd.gauss(1.0, 20) for _ in range(k)]
            clusters.append(xs)
            for x in xs:
                a.add(x, f"d{d}")
        N = sum(len(c) for c in clusters)
        mu = sum(map(sum, clusters)) / N
        V = len(clusters) / (len(clusters) - 1) * sum((sum(c) - len(c) * mu) ** 2 for c in clusters) / N ** 2
        cl = a.cluster()
        self.assertAlmostEqual(cl["t"], mu / math.sqrt(V), places=9)
        self.assertEqual((cl["sessions"], cl["df"]), (15, 14))
        best = max(clusters, key=sum)
        self.assertAlmostEqual(cl["dropBestBps"], (sum(map(sum, clusters)) - sum(best)) / (N - len(best)), places=9)
        self.assertLess(cl["t"], a.t(), "shared session shocks: the clustered t is smaller than the per-trade t")
        self.assertLess(cl["nEff"], N)

    def test_without_days_every_trade_is_its_own_cluster(self):
        a = LAB.Acc()
        for x in (3.0, -1.0, 4.0, 1.0, 5.0, -2.0, 6.0):
            a.add(x)
        self.assertAlmostEqual(a.cluster()["t"], a.t(), places=9)
        b = LAB.Acc()
        b.add(1, "x"); b.add(2, "x")
        b.view()                                     # a read in the middle of a session must not split it
        b.add(3, "x"); b.add(5, "y")
        self.assertEqual(b.cluster()["sessions"], 2)


def _one(train, test, seg="liq:mid", st="sector_reversion"):
    vid = LAB.vid_of(st, LAB.GRIDS[st][0])
    return {(st, vid, seg, "train"): train, (st, vid, seg, "test"): test}


class Verdict(unittest.TestCase):
    """z180: the cases the first real run got wrong."""

    def test_one_market_day_is_not_two_thousand_trades(self):
        # 13 test sessions x 160 trades; the mean comes from two big days that moved every trade together
        train = _acc(40, 20, seed=2, base=4.0)
        test = _acc(13, 160, seed=3, base=-6.0, shocks={4: 60.0, 9: 55.0})
        c = LAB.select(_one(train, test), ["sector_reversion"], n_test=13)[0]
        self.assertGreater(c["test"]["t"], 3.0, "per trade it looks decisive")
        self.assertLess(c["test"]["tCl"], 2.0, "per session it isn't")
        self.assertFalse(c["approved"])
        self.assertTrue(any("without its best day" in r for r in c["reasons"]), c["reasons"])

    def test_few_sessions_or_no_training_evidence(self):
        few = LAB.select(_one(_acc(40, 20, seed=4, base=4.0), _acc(5, 40, seed=5, base=8.0)), ["sector_reversion"], n_test=13)[0]
        self.assertFalse(few["approved"])
        self.assertTrue(any("test sessions" in r for r in few["reasons"]), few["reasons"])
        # training: +0.5 bps a trade on average, but sessions swing +6 / -5 - per session that is no evidence (t ~ 0.6)
        swing = _acc(40, 20, seed=6, sd=2.0, shocks={k: (6.0 if k % 2 == 0 else -5.0) for k in range(40)})
        self.assertLess(swing.cluster()["t"], LAB.MIN_TRAIN_T)
        self.assertGreater(swing.mean(), 0)
        weak = LAB.select(_one(swing, _acc(13, 40, seed=7, base=9.0)), ["sector_reversion"], n_test=13)
        self.assertEqual(weak, [], "no training evidence of its own: never forwarded to the test")

    def test_a_steady_edge_passes(self):
        c = LAB.select(_one(_acc(40, 20, seed=8, base=5.0), _acc(20, 20, seed=9, base=5.0)), ["sector_reversion"], n_test=20)[0]
        self.assertTrue(c["approved"], c["reasons"])
        self.assertEqual(c["reasons"], [])

    def test_quad_segment_needs_the_quad_to_vary(self):
        acc = _one(_acc(40, 20, seed=10, base=5.0), _acc(20, 20, seed=11, base=5.0), seg="side:short|quad:Q3")
        calendar = {"train": {"Q4": 30, "Q3": 10}, "test": {"Q3": 20}}           # every test session Q3 (the 11-07 case)
        c = LAB.select(acc, ["sector_reversion"], quad_days=calendar, n_test=20)[0]
        self.assertTrue(c["regimeDependent"])
        self.assertFalse(c["approved"])
        self.assertTrue(any("calendar" in r for r in c["reasons"]), c["reasons"])
        varied = {"train": {"Q4": 25, "Q3": 15}, "test": {"Q3": 12, "Q4": 8}}
        self.assertTrue(LAB.select(acc, ["sector_reversion"], quad_days=varied, n_test=20)[0]["approved"])
        self.assertFalse(LAB.select(acc, ["sector_reversion"], quad_days=None, n_test=20)[0]["approved"], "no quad record: not approved")

    def test_models_carry_the_version_and_rules(self):
        m = LAB.build_models(_one(_acc(40, 20, seed=8, base=5.0), _acc(20, 20, seed=9, base=5.0)), ["sector_reversion"],
                             ["2026-08-01", "2026-09-30"], ["2026-10-01", "2026-10-30"])
        self.assertEqual(m["labVersion"], 2)
        self.assertEqual(m["rules"]["minTestSessions"], LAB.MIN_TEST_SESSIONS)
        live = m["strategies"]["sector_reversion"]["live"]
        self.assertTrue(live and live[0]["regimeDependent"] is False and live[0]["test"]["sessions"] == 20)


class EngineTrust(unittest.TestCase):
    """z180: the engine trusts only a v2 verdict, and a quad-split approval only through the regime gate."""

    def _engine(self, doc, regime=None):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "models.json"), "w") as f:
            json.dump(doc, f)
        uni = dict(UNI, regime=regime) if regime is not None else dict(UNI)
        logs = []
        e = Engine(replace(Config(), execution="sim"), uni, clock=lambda: 1793026800, data_dir=d, log=lambda *a: logs.append(" ".join(map(str, a))))
        return e, logs

    def test_lab_v1_verdict_is_not_trusted(self):
        vid = LAB.vid_of("sector_reversion", {"z_in": 2.5, "max_hold": 30, "vol_spike_max": None})
        doc = {"strategies": {"sector_reversion": {"variants": {vid: {"z_in": 2.5, "max_hold": 30, "vol_spike_max": None}},
                                                   "live": [{"segment": "liq:mid", "variant": vid}]}}}       # no labVersion = v1
        e, logs = self._engine(doc)
        self.assertEqual(e.models, {})
        self.assertFalse(e.live_for("sector_reversion"))
        sig = Signal("M1", "long", "sector_reversion", 20, 40, 30, variant=vid, tags={"liq": "mid", "tod": "mid", "side": "long", "sector": "XLK"})
        self.assertFalse(e.live_signal(sig))
        self.assertTrue(any("lab v1" in l and "not trusted" in l for l in logs), logs)
        self.assertEqual(sorted(s.variant for s in e.strategies if s.name == "sector_reversion"), ["default"])

    def test_quad_split_approval_waits_on_the_regime_gate(self):
        vid = LAB.vid_of("opening_range", {"range_min": 30, "target_mult": 3.0, "last_entry": 690})
        doc = {"labVersion": 2, "strategies": {"opening_range": {"variants": {vid: {"range_min": 30, "target_mult": 3.0, "last_entry": 690}},
                                                                 "live": [{"segment": "side:short|quad:Q3", "variant": vid, "regimeDependent": True},
                                                                          {"segment": "sector:XLE", "variant": vid}]}}}
        tag = lambda sector: {"liq": "mid", "tod": "open", "side": "short", "sector": sector, "quad": "Q3"}
        sig_q = Signal("M1", "short", "opening_range", 20, 60, 10 ** 6, variant=vid, tags=tag("XLK"))      # only the quad approval
        sig_e = Signal("M2", "short", "opening_range", 20, 60, 10 ** 6, variant=vid, tags=tag("XLE"))      # also a plain approval
        shut, _ = self._engine(doc, regime={"ok": False, "why": "certainty 34% < 65%"})
        self.assertFalse(shut.live_signal(sig_q))
        self.assertTrue(shut.regime_blocked(sig_q), "blocked by the gate, so it still runs in shadow")
        self.assertTrue(shut.live_signal(sig_e), "a segment that isn't split by the quad doesn't wait on it")
        missing, _ = self._engine(doc)                                   # no gate on record = not dialed in
        self.assertFalse(missing.live_signal(sig_q))
        open_, _ = self._engine(doc, regime={"ok": True})
        self.assertTrue(open_.live_signal(sig_q) and not open_.regime_blocked(sig_q))


class RegimeGate(unittest.TestCase):
    def test_gate(self):
        from quant.macro import regime_gate
        d = date(2026, 11, 9)
        fresh = {"certainty": 0.71, "threshold": 0.65, "dialedIn": True, "asOf": "2026-11-06"}
        self.assertTrue(regime_gate({"agree": True}, fresh, today=d)["ok"])
        self.assertFalse(regime_gate({"agree": False}, fresh, today=d)["ok"], "the nowcast and the official quad must agree")
        self.assertFalse(regime_gate({"agree": True}, dict(fresh, dialedIn=False, certainty=0.335), today=d)["ok"])
        self.assertFalse(regime_gate({"agree": True}, dict(fresh, asOf="2026-10-20"), today=d)["ok"], "stale certainty")
        g = regime_gate({"agree": None}, {}, today=d)
        self.assertFalse(g["ok"])
        self.assertIn("no regime certainty file", g["why"])


class Quad(unittest.TestCase):
    """z177: the quad on every trade, the same definition live and in the lab."""

    def test_official_as_known_and_current(self):
        from quant.macro import official_on, current_quads
        rows = [{"date": "2026-07-01", "quad": 4}, {"date": "2026-08-01", "quad": 4}, {"date": "2026-09-01", "quad": 3}]
        self.assertEqual(official_on("2026-10-10", rows), 4, "September's CPI isn't out until mid-October")
        self.assertEqual(official_on("2026-11-06", rows), 3)
        doc = {"current": {"quad": 4, "method": "market-nowcast", "asOf": "2026-11-06"}, "official": {"quad": 3, "asOf": "2026-09-01"},
               "nowcast": {"quad": 4, "asOf": "2026-11-06"}, "agree": {"sameQuad": False, "note": "disagree on inflation"}}
        q = current_quads(doc)
        self.assertEqual((q["official"], q["nowcast"], q["agree"]), (3, 4, False))
        old = {"current": {"quad": 3, "asOf": "2026-09-01"}}            # an old-shape file (official only)
        self.assertEqual(current_quads(old)["official"], 3)

    def test_tags_and_segments_carry_the_quad(self):
        from types import SimpleNamespace
        from quant.segments import tags_for
        ctx = SimpleNamespace(minute=700, liquidity={"AAPL": 5e9}, sector_etf={"AAPL": "XLK"}, kinds={"AAPL": "stock"}, quad={"official": 3, "nowcast": 4})
        t = tags_for("AAPL", "long", ctx)
        self.assertEqual((t["quad"], t["quadNow"]), ("Q3", "Q4"))
        ks = keys_for(t)
        self.assertIn("quad:Q3", ks)
        self.assertIn("liq:mega|quad:Q3", ks)
        self.assertFalse(any("quadNow" in k for k in ks), "the nowcast is recorded, not segmented")


if __name__ == "__main__":
    unittest.main()
