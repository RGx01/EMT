"""
`BlockStore.get_dispatch_history_for_slot` and the `load_config` de-duplication
-- workstream A acceptance 5, second slice.

Two `_store._conn` sites, and they are different KINDS of finding:

  * `load_config` re-implemented `get_current_config_period_id()` against the
    raw connection, character for character. The method already existed. That is
    a deletion, not a new accessor -- and it is the more interesting of the two,
    because nothing about the call site looked wrong.

  * `_iog_slot_ev_kwh` wanted dispatch_history for ONE slot. `dispatch_slots`
    already had its exact-match accessor (`get_dispatch_slot`); dispatch_history
    only had a range one. A genuine gap, so a genuine new method.

The range sibling would in fact select the same rows given [slot, next_slot),
since slot_start is exact-valued. The test below pins that equivalence, so the
new method cannot drift into meaning something the range version doesn't.
"""
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from block_store import BlockStore          # noqa: E402


def _mk_store():
    st = BlockStore(tempfile.mktemp(suffix=".db"))
    st._conn.execute("PRAGMA foreign_keys=OFF")
    return st


def _hist(st, slot, kind, kwh, source=None):
    st.record_dispatch_history(slot_start=slot, kind=kind, provider="octopus",
                               source=source, energy_kwh=kwh,
                               raw_start=None, raw_end=None)


class DispatchHistoryForSlot(unittest.TestCase):

    def setUp(self):
        self.st = _mk_store()
        self.slot = "2026-09-20T01:00:00"
        self.next = "2026-09-20T01:30:00"

    def test_empty_is_an_empty_list(self):
        self.assertEqual(self.st.get_dispatch_history_for_slot(self.slot), [])

    def test_kind_filters(self):
        _hist(self.st, self.slot, "planned", 3.0)
        _hist(self.st, self.slot, "completed", 2.5)
        done = self.st.get_dispatch_history_for_slot(self.slot, kind="completed")
        self.assertEqual(len(done), 1)
        self.assertAlmostEqual(done[0]["energy_kwh"], 2.5)
        self.assertEqual(len(self.st.get_dispatch_history_for_slot(self.slot)), 2)

    def test_it_does_not_bleed_into_the_neighbouring_slot(self):
        _hist(self.st, self.slot, "completed", 2.5)
        _hist(self.st, self.next, "completed", 9.9)
        rows = self.st.get_dispatch_history_for_slot(self.slot, kind="completed")
        self.assertEqual([r["energy_kwh"] for r in rows], [2.5])

    def test_it_agrees_with_the_range_sibling(self):
        """The equivalence that justifies the method existing: same rows, same
        shape, without the caller doing slot arithmetic to find `next_slot`."""
        _hist(self.st, self.slot, "completed", 2.5)
        _hist(self.st, self.next, "completed", 9.9)
        self.assertEqual(
            self.st.get_dispatch_history_for_slot(self.slot, kind="completed"),
            self.st.get_dispatch_history(self.slot, self.next, kind="completed"))

    def test_both_surfaces_carry_it(self):
        from store_types import ReadStore, WriteStore
        self.assertTrue(hasattr(ReadStore, "get_dispatch_history_for_slot"))
        self.assertTrue(hasattr(WriteStore, "get_dispatch_history_for_slot"))


class ConfigPeriodDeDuplication(unittest.TestCase):

    def test_the_method_load_config_now_calls_answers_the_same_question(self):
        """`load_config` ran this SELECT itself. Pinned against the raw query it
        replaced, so the de-duplication is demonstrated rather than asserted."""
        st = _mk_store()
        self.assertIsNone(st.get_current_config_period_id())
        st.insert_config_period({"meters": {}}, effective_from="2026-01-01T00:00:00")
        st.insert_config_period({"meters": {}}, effective_from="2026-06-01T00:00:00")
        raw = st._conn.execute(
            "SELECT id FROM config_periods WHERE effective_to IS NULL "
            "ORDER BY effective_from DESC LIMIT 1").fetchone()
        self.assertIsNotNone(raw)
        self.assertEqual(st.get_current_config_period_id(), raw["id"])


if __name__ == "__main__":
    unittest.main()
