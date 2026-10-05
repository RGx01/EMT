"""
Workstream D — queries per surface, asserted in CI (5 Oct 2026).

Every surface `.build/soak.py` names is requested against a synthetic database
(`tests/surface_fixture.py`) at two lengths of history, and the SQL statements it issues are
counted (`.build/query_budgets.py`). Asserted:

  * every soak surface has a budget in `.build/query_budgets.json`, and none exceeds it;
  * the count is the SAME at 40 and 200 days — no query per day, per block or per period.

Time to first byte stays with the Pi soak (`.build/soak_budgets.json`): a CI runner's timings
say nothing about a Pi on SD storage.

GUARDS: the property already holds (9–31 statements, identical across 5× history), so these
pass on the parent tree. `TheCheckCatchesItsTarget` shows they are not vacuous: an N+1 injected
into the all-days load — one query per block — is caught by the invariance test.
"""
import json
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, ".build"))
sys.path.insert(0, _ROOT)

import query_budgets as Q                         # noqa: E402
import soak                                       # noqa: E402


class Budgets(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.budgets = json.load(open(Q.BUDGETS))
        cls.small = Q.measure(Q.SMALL_DAYS)
        cls.large = Q.measure(Q.LARGE_DAYS)

    def test_every_soak_surface_has_a_budget(self):
        names = {n for n, _, _ in soak.SURFACES}
        self.assertEqual(sorted(names - set(self.budgets)), [], "a soak surface with no budget")
        self.assertEqual(sorted(set(self.budgets) - names), [], "a budget with no soak surface")

    def test_every_surface_answers(self):
        """Counting a 400 would measure the refusal, not the surface."""
        for name, (status, _) in self.large.items():
            with self.subTest(surface=name):
                self.assertEqual(status, 200)

    def test_no_surface_exceeds_its_budget(self):
        for name, (_, n) in self.large.items():
            with self.subTest(surface=name):
                self.assertLessEqual(n, self.budgets[name],
                                     "%s issues %d statements (budget %d) — if that is right, "
                                     "run .build/query_budgets.py --update"
                                     % (name, n, self.budgets[name]))

    def test_no_surface_issues_more_queries_for_more_history(self):
        for name in self.small:
            with self.subTest(surface=name):
                self.assertEqual(self.small[name][1], self.large[name][1],
                                 "%s: %d statements over %d days, %d over %d — a query per "
                                 "day, block or period" % (name, self.small[name][1], Q.SMALL_DAYS,
                                                           self.large[name][1], Q.LARGE_DAYS))


class TheCheckCatchesItsTarget(unittest.TestCase):

    def test_an_injected_query_per_block_is_caught(self):
        import store_access
        import usage_view
        real = usage_view._local_day_of

        def leaky(utc_start, utc_end, tz):
            day = real(utc_start, utc_end, tz)

            def _day(bs):
                store_access.store()._conn.execute("SELECT 1").fetchone()   # the N+1
                return day(bs)
            return _day
        with mock.patch.object(usage_view, "_local_day_of", leaky):
            small, large = Q.measure(Q.SMALL_DAYS), Q.measure(Q.LARGE_DAYS)
        name = "Spiral / Usage Stats: all days"
        self.assertGreater(large[name][1], small[name][1], "the injected N+1 was not seen")


if __name__ == "__main__":
    unittest.main()
