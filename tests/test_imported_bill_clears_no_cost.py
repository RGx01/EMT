"""
An imported half-hour with Octopus's bill cached is costed, whatever the import's no-cost record says
(21 Oct 2026, staging).

21 Jul 2026 was deleted, then refilled from Octopus on 20 Oct. Octopus's measurements returned 15
of its half-hours with no cost, so the import put them on its no-cost record ("no cost from
Octopus, and EMT no longer asks"). The bill drain fetched their bills two hours later and applied
them, but nothing took them off the record: they stayed red-hatched with the right cost on them.
- **classify:** an `imported_api` half-hour with a cached bill is `applied`, or `differs` if its
  cost isn't the bill, never `abandoned`.
- **`engine._store_bill`:** caching a bill takes the slot off the record.
- **The health strip's counts file** goes to version 3, since the rule changed.

And the pricing check's log line said "corrected 28 of 26 re-checked": the corrected count included
the neighbours the same fetch re-priced (repair_import_pricing's `opportunistic`), which were not
re-checked. They are now said apart.

Discriminating: every test here fails on the parent but the GUARD.
"""
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import settlement_state as ss                  # noqa: E402

NOW = datetime(2026, 10, 21, 12, 0)
BS = "2026-07-21T05:00:00"


def _bill(**kw):
    f = {"block_start": BS, "imp_kwh": 0.022, "imp_cost": 0.007108, "source": "imported_api",
         "exp_kwh": 0.0, "exp_kwh_api": None, "bill_cost": 0.007108}
    f.update(kw)
    return ss.classify(f, smb=True, now=NOW, import_no_cost={BS}, export_channel=True)["bill"]


class TheState(unittest.TestCase):

    def test_a_cached_bill_is_applied_off_the_stale_record(self):
        self.assertEqual(_bill(), "applied")

    def test_a_cached_bill_that_isnt_the_cost_differs(self):
        self.assertEqual(_bill(imp_cost=0.02), "differs")

    def test_no_bill_on_the_record_is_still_abandoned(self):
        """GUARD: the record still means what it said where no bill has come."""
        self.assertEqual(_bill(bill_cost=None), "abandoned")


class TheDrain(unittest.TestCase):

    def test_caching_a_bill_clears_the_record(self):
        import engine
        from block_store import BlockStore
        st = BlockStore(":memory:")
        st.note_import_cost(no_cost=[BS, "2026-07-21T05:30:00"])
        engine._store_bill(st, BS, "M1", {"cost_incl": 0.007108, "cost_excl": 0.007108,
                                           "label": "STANDARD_RATE", "kwh": 0.022})
        self.assertEqual(sorted(st.import_no_cost_slots()), ["2026-07-21T05:30:00"])


class TheLogLine(unittest.TestCase):

    def test_says_the_neighbours_apart(self):
        with open(os.path.join(_ROOT, "engine.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('half-hour(s) from the same fetch (%d already-correct skipped)', src)
        self.assertIn("_cr - _op, _ck, _op, _sk, _elapsed", src)


class TheKeptCounts(unittest.TestCase):

    def test_are_dropped_for_the_new_rule(self):
        with open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8") as f:
            self.assertIn("_HEALTH_FILE_V = 3", f.read())


if __name__ == "__main__":
    unittest.main()
