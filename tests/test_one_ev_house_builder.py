"""
One EV + house builder — bill and split paths design §3.3, consolidation PR 5b (16 Oct 2026).

The carve, the settle's single-band fallback, the reconcile's stale-rated split heal (BL-58b) and
its revert/restore verdict each wrote the EV + house segments at one rate by hand. Now
`pricing_segments.ev_house_at`.

Discriminating: each fails on the parent (the builder does not exist there), except the guard.
"""
import os
import random
import re
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import pricing_segments as ps                   # noqa: E402

R, X = 0.05493, 0.052314


class TheBuilder(unittest.TestCase):

    def test_ev_and_the_house_remainder(self):
        self.assertEqual(ps.ev_house_at(6.0, 3.409, inc_rate=R, exc_rate=X, ev_band="off_peak",
                                        home_band="off_peak"),
                         [ps.Segment(3.409, R, X, "off_peak", "ev"),
                          ps.Segment(2.591, R, X, "off_peak", "house")])

    def test_all_ev_has_no_house(self):
        self.assertEqual(len(ps.ev_house_at(3.0, 3.0, inc_rate=R, exc_rate=X, ev_band="peak",
                                            home_band="day")), 1)

    def test_no_ev_is_one_house_segment(self):
        for ev in (None, 0.0):
            self.assertEqual(ps.ev_house_at(0.809, ev, inc_rate=R, exc_rate=None,
                                            ev_band="off_peak", home_band="day"),
                             [ps.Segment(0.809, R, None, "day", "house")])

    def test_it_matches_the_hand_written_form(self):
        """GUARD (of the refactor, not the parent): the form the three Segment sites wrote."""
        def old(kwh, evk):
            segs = []
            if evk is not None and float(evk) > 1e-9:
                segs.append(ps.Segment(round(float(evk), 6), R, X, "off_peak", "ev"))
                hk = kwh - float(evk)
                if hk > 1e-9:
                    segs.append(ps.Segment(round(hk, 6), R, X, "day", "house"))
            else:
                segs.append(ps.Segment(round(kwh, 6), R, X, "day", "house"))
            return segs
        rnd = random.Random(16)
        for _ in range(2000):
            kwh = round(rnd.uniform(0.001, 8.0), 3)
            evk = rnd.choice([None, 0.0, round(rnd.uniform(0.0, kwh), 4), kwh])
            self.assertEqual(ps.ev_house_at(kwh, evk, inc_rate=R, exc_rate=X, ev_band="off_peak",
                                            home_band="day"), old(kwh, evk), (kwh, evk))


class NoHandWrittenPairs(unittest.TestCase):

    def test_the_engine_builds_none_by_hand(self):
        src = open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read()
        self.assertEqual(re.findall(r'append\(\(?[^\n]*"ev"\)\)', src), [])
        # engine steps 4b–4c: the four writers that built EV + house by hand — the carve, the settle's
        # single-band fallback, the reconcile's stale-rated split heal and its verdict — are settle()
        # now, which builds every one-rate leg pair with the one builder and none by hand
        st = open(os.path.join(_ROOT, "settle.py"), encoding="utf-8").read()
        self.assertEqual(re.findall(r'append\(\(?[^\n]*"ev"\)\)', st), [])
        self.assertGreaterEqual(st.count("ev_house_at("), 1)


if __name__ == "__main__":
    unittest.main()
