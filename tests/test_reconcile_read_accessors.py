"""
`reconcile_dispatch_overlay`'s reads — A acceptance 5, reads half.

483 lines, 18 raw-connection sites, on the settlement path. Split in two: this is
the reads, which cannot change what gets written. The writes — where the
transaction boundary and the guard convergence live — follow separately, so a Pi
run that turns something up reverts one half rather than all 18.

Three findings the port turned up, each pinned below.

1. THREE SITES WERE THE SAME SELECT. The split re-price, the EV-column re-stamp
   and the post-verdict segment rebuild each selected an overlapping subset of
   one block's split columns. One projection now serves all three.

2. ONE SITE NEEDED NO NEW METHOD. It selected `kind, energy_kwh, first_seen`
   from dispatch_history for a single slot, which `get_dispatch_history_for_slot`
   (added in A2) already answers.

3. THAT SITE INDEXED BY POSITION -- `x[0]`, `x[1]`, `x[2]` over a three-column
   SELECT. The shared accessor returns nine columns in a different order, so the
   reads had to become named. They should have been named anyway: positional
   access to a row is a landmine for whoever next edits the SELECT list.
"""
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from block_store import BlockStore          # noqa: E402

S = "2026-09-01T00:00:00"


def _mk_store():
    st = BlockStore(tempfile.mktemp(suffix=".db"))
    st._conn.execute("PRAGMA foreign_keys=OFF")
    return st


def _block(st, start=S, **cols):
    base = {"block_start": start, "block_end": start, "config_period_id": 1,
            "meter_id": "electricity_main", "imp_kwh": 5.0, "imp_rate": 0.25,
            "rate_corrected": 0}
    base.update(cols)
    st._conn.execute("INSERT INTO blocks (%s) VALUES (%s)"
                     % (",".join(base), ",".join("?" * len(base))),
                     tuple(base.values()))
    st._conn.commit()


def _slot(st, start=S, off_peak=1):
    st.upsert_dispatch_slot(slot_start=start, off_peak=off_peak,
                            provider="octopus", source=None, state=None,
                            energy_planned=None, energy_completed=None,
                            raw_start=None, raw_end=None)


def _hist(st, start=S, kind="planned", kwh=None):
    st.record_dispatch_history(slot_start=start, kind=kind, provider="octopus",
                               source=None, energy_kwh=kwh,
                               raw_start=None, raw_end=None)


LATER = "2099-01-01T00:00:00"


class ReconcileCandidates(unittest.TestCase):

    def setUp(self):
        self.st = _mk_store()

    def _candidate(self, **cols):
        _block(self.st, **cols)
        _slot(self.st)
        _hist(self.st)

    def test_a_plain_candidate_is_selected(self):
        self._candidate()
        self.assertEqual(len(self.st.get_reconcile_candidates(LATER)), 1)

    def test_a_user_correction_is_never_reconciled(self):
        self._candidate(rate_corrected=1)
        self.assertEqual(self.st.get_reconcile_candidates(LATER), [])

    def test_a_measured_rate_outranks_the_heuristic(self):
        """BL-53. Octopus's billed cost is a higher authority than 'we never saw
        it start'."""
        self._candidate(rate_source="settled")
        self.assertEqual(self.st.get_reconcile_candidates(LATER), [])

    def test_imported_history_is_never_reconciled(self):
        """The 'October regressed on its own' bug: imported blocks carry
        Octopus's actual billed dispatch-aware rate, and the local lifecycle
        heuristic is invalid for periods EMT was not running."""
        self._candidate(source="imported_csv")
        self.assertEqual(self.st.get_reconcile_candidates(LATER), [])

    def test_a_slot_that_was_never_an_off_peak_candidate_is_skipped(self):
        _block(self.st)
        _slot(self.st, off_peak=0)
        _hist(self.st)
        self.assertEqual(self.st.get_reconcile_candidates(LATER), [])

    def test_no_dispatch_evidence_means_nothing_to_decide(self):
        _block(self.st)
        _slot(self.st)
        self.assertEqual(self.st.get_reconcile_candidates(LATER), [])

    def test_completed_only_still_counts_as_evidence(self):
        """EMT offline, or a re-import: the completed record is authoritative on
        its own."""
        _block(self.st)
        _slot(self.st)
        _hist(self.st, kind="completed", kwh=3.0)
        self.assertEqual(len(self.st.get_reconcile_candidates(LATER)), 1)

    def test_the_started_gate_is_honoured(self):
        self._candidate()
        self.assertEqual(self.st.get_reconcile_candidates("2026-01-01T00:00:00"), [])


class BlockSplitColumns(unittest.TestCase):

    def setUp(self):
        self.st = _mk_store()

    def test_missing_block_is_none(self):
        self.assertIsNone(self.st.get_block_split_columns(S))

    def test_it_carries_every_column_the_three_sites_wanted(self):
        _block(self.st, imp_kwh_ev=2.0, imp_rate_ev=0.25, imp_rate_exc=0.238,
               imp_ev_band="off_peak", imp_home_band="day")
        r = self.st.get_block_split_columns(S)
        for col in ("imp_kwh", "imp_kwh_ev", "imp_rate", "imp_rate_ev",
                    "imp_rate_exc", "imp_ev_band", "imp_home_band"):
            self.assertIn(col, r)

    def test_an_unsplit_block_is_a_row_with_a_null_column(self):
        """The behaviour change to be aware of. One caller used to filter
        `AND imp_kwh_ev IS NOT NULL` in SQL and test the ROW for None; it now
        tests the COLUMN. A row comes back either way."""
        _block(self.st)
        r = self.st.get_block_split_columns(S)
        self.assertIsNotNone(r, "the block exists, so there is a row")
        self.assertIsNone(r["imp_kwh_ev"], "but it has no split")

    def test_it_is_meter_scoped(self):
        _block(self.st)
        self.assertIsNone(self.st.get_block_split_columns(S, meter_id="sub_meter_ev"))


class DistinctSegmentRates(unittest.TestCase):

    def setUp(self):
        self.st = _mk_store()

    def _segs(self, *rates):
        import pricing_segments as ps
        self.st.set_block_segments(
            S, "electricity_main",
            [ps.Segment(1.0, r, None, "off_peak", "house") for r in rates])

    def test_none_is_empty(self):
        self.assertEqual(self.st.distinct_segment_rates(S), [])

    def test_a_single_rate_block_reports_one(self):
        self._segs(0.25, 0.25)
        self.assertEqual(len(self.st.distinct_segment_rates(S)), 1)

    def test_a_multi_rate_block_reports_more(self):
        """The count is the question: >= 2 means a genuine multi-band capped
        block, which the stale-rated heal must leave alone."""
        self._segs(0.05493, 0.32309)
        self.assertEqual(len(self.st.distinct_segment_rates(S)), 2)

    def test_rates_are_rounded_to_six_places(self):
        self._segs(0.2500001, 0.2500002)
        self.assertEqual(len(self.st.distinct_segment_rates(S)), 1,
                         "rounding noise must not read as two bands")


class StaleBandSegments(unittest.TestCase):

    def setUp(self):
        self.st = _mk_store()

    def _seg(self, band, attribution):
        import pricing_segments as ps
        self.st.set_block_segments(
            S, "electricity_main", [ps.Segment(1.0, 0.25, None, band, attribution)])

    def test_agreement_counts_zero(self):
        self._seg("off_peak", "ev")
        self.assertEqual(self.st.count_stale_band_segments(S, "off_peak", "day"), 0)

    def test_a_stale_ev_band_is_counted(self):
        self._seg("off_peak", "ev")
        self.assertEqual(self.st.count_stale_band_segments(S, "peak", "day"), 1)

    def test_a_stale_house_band_is_counted(self):
        self._seg("off_peak", "house")
        self.assertEqual(self.st.count_stale_band_segments(S, "peak", "day"), 1)


class Surfaces(unittest.TestCase):

    def test_all_four_are_reads(self):
        from store_types import ReadStore
        for name in ("get_reconcile_candidates", "get_block_split_columns",
                     "distinct_segment_rates", "count_stale_band_segments"):
            self.assertTrue(hasattr(ReadStore, name), name)


class NoPositionalRowAccessLeft(unittest.TestCase):
    """AST: the dispatch rows are dicts now, and `x[0]` on a dict is a KeyError
    waiting for a slot with history."""

    def test_the_reconcile_loop_subscripts_by_name(self):
        import ast
        src = open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read()
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "reconcile_dispatch_overlay")
        bad = [n.lineno for n in ast.walk(fn)
               if isinstance(n, ast.Subscript)
               and isinstance(n.value, ast.Name) and n.value.id == "x"
               and isinstance(n.slice, ast.Constant)
               and isinstance(n.slice.value, int)]
        self.assertEqual(bad, [], "positional row access at lines %s" % bad)


if __name__ == "__main__":
    unittest.main()
