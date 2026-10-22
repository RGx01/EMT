"""
.build/probe_window_shapes.py, offline: the parts that decide what is asked and how an answer is
read (22 Oct 2026). Nothing here calls Octopus.

- EMT's ladder rule: OFF_PEAK at the first rung that gives it, else the last cost kept.
- A node's cost and the label of the bucket that carries the charge; no energy statistic is "no
  cost" (stripped), not zero.
- The local day across BST, the ladder's picks spread across a charging run, and the query count a
  dry run states.

Discriminating: every test here fails on the parent (the probe is new).
"""
import importlib.util
import os
import unittest
from datetime import date

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "probe_window_shapes", os.path.join(_ROOT, ".build", "probe_window_shapes.py"))
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)


def _node(kwh, *stats):
    return {"value": kwh, "startAt": "2025-03-06T19:00:00+00:00",
            "metaData": {"statistics": [
                {"type": t, "label": lab, "value": v, "costInclTax": {"estimatedAmount": c}}
                for t, lab, v, c in stats]}}


class TheLadderRule(unittest.TestCase):

    def test_off_peak_at_the_first_rung_wins(self):
        a = (3.6, 0.25, "OFF_PEAK"); b = (3.6, 0.92, "STANDARD_RATE")
        self.assertEqual(P.ladder_pick([a, b]), a)

    def test_standard_then_off_peak_takes_the_second(self):
        a = (3.6, 0.92, "STANDARD_RATE"); b = (3.6, 0.25, "OFF_PEAK")
        self.assertEqual(P.ladder_pick([a, b]), b)

    def test_standard_both_keeps_the_last_cost(self):
        a = (3.6, 0.92, "STANDARD_RATE"); b = (3.6, 0.93, "STANDARD_RATE")
        self.assertEqual(P.ladder_pick([a, b]), b)

    def test_a_stripped_rung_is_passed_over(self):
        a = (3.6, 0.92, "STANDARD_RATE")
        self.assertEqual(P.ladder_pick([a, (3.6, None, None), None]), a)


class ANode(unittest.TestCase):

    def test_the_carrying_bucket_names_it(self):
        n = _node(3.6, ("TOU_BUCKET_COST", "OFF_PEAK", 3.6, 25.2),
                  ("TOU_BUCKET_COST", "STANDARD_RATE", 0.0, 0.0),
                  ("STANDING_CHARGE_COST", None, None, 1.1))
        self.assertEqual(P.read_node(n), (3.6, 0.252, "OFF_PEAK"))

    def test_no_energy_statistic_is_no_cost(self):
        n = _node(3.6, ("STANDING_CHARGE_COST", None, None, 1.1))
        self.assertEqual(P.read_node(n), (3.6, None, None))


class TheWindows(unittest.TestCase):

    def test_a_summer_day_is_bst(self):
        a, b = P.local_day(date(2025, 7, 1))
        self.assertEqual((a.isoformat(), b.isoformat()), ("2025-06-30T23:00:00", "2025-07-01T23:00:00"))

    def test_the_ladder_spreads_across_the_run(self):
        stmt = {"2025-03-06T%02d:%02d:00" % (h, m): {"offpeak": True, "local": "%02d:%02d" % (h, m),
                                                    "kwh": 3.6, "day": date(2025, 3, 6)}
                for h in range(17, 22) for m in (0, 30)}
        self.assertEqual(P.ladder_slots(stmt, 3),
                         ["2025-03-06T17:00:00", "2025-03-06T19:00:00", "2025-03-06T21:30:00"])

    def test_the_night_window_is_not_asked(self):
        stmt = {"2025-03-06T02:00:00": {"offpeak": True, "local": "02:00", "kwh": 3.0,
                                        "day": date(2025, 3, 6)}}
        self.assertEqual(P.ladder_slots(stmt, 3), [])

    def test_one_day_is_about_nineteen_queries(self):
        days = [date(2025, 3, 6)]
        ladder = ["2025-03-06T17:30:00", "2025-03-06T19:30:00", "2025-03-06T21:30:00"]
        # 6 rungs, day/500 1, day/48 1, day/10 5, the 61-day window 6 pages of 500
        self.assertEqual(P.queries_for(P.plan(days, ladder)), 19)


if __name__ == "__main__":
    unittest.main()
