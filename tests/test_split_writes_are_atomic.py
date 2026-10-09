"""
BL-79 (part 2) — an EV-split writer commits its segments and its columns together (8 Oct 2026).

Each writer of a main block's EV split wrote the imp_*_ev columns and the block's segments in two
transactions, and the segment write was best-effort. A failed segment write left the columns
saying one split and the segments — what every surface reads — another. Now each writer passes
its segments to the store method that writes its columns, inside one transaction: if the segment
write fails, neither changes.

The atomicity cases make the segment write fail (whichever store call does it on either tree) and
assert the columns are unchanged. The reprice sweep's case is a path that wrote the columns and
no segments at all. Discriminating: each first case fails on the parent; the rest are guards.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_ev_back_attribution as BA            # noqa: E402  (store + schedule harness)
import test_measured_apply as MA                 # noqa: E402
import test_reprice_history_sweep as RS          # noqa: E402
engine = BA.engine


def _segments_fail(store):
    """Make every segment write on `store` raise, on this tree and the parent."""
    boom = RuntimeError("segment write failed")
    return [patch.object(store, "set_block_segments", side_effect=boom),
            patch.object(store, "_replace_segments", side_effect=boom, create=True)]


class TheBackAttribution(unittest.TestCase):
    setUp, tearDown = BA.TestEvBackAttribution.setUp, BA.TestEvBackAttribution.tearDown
    _block, _completed, _row = (BA.TestEvBackAttribution._block,
                                BA.TestEvBackAttribution._completed,
                                BA.TestEvBackAttribution._row)
    SLOT = BA.TestEvBackAttribution.SLOT

    def test_a_failed_segment_write_leaves_the_columns(self):
        self._block(imp_kwh=3.0, ev=None)
        self._completed(energy=-2.0)
        ps = _segments_fail(self.st)
        for p in ps:
            p.start()
        try:
            engine._attribute_missing_ev_split()
        finally:
            for p in ps:
                p.stop()
        self.assertIsNone(self._row()["imp_kwh_ev"])

    def test_it_still_writes_both(self):
        """GUARD: with the write working, columns and segments carry the same EV."""
        self._block(imp_kwh=3.0, ev=None)
        self._completed(energy=-2.0)
        self.assertEqual(engine._attribute_missing_ev_split(), 1)
        ev = sum(s["kwh"] for s in self.st.get_block_segments(self.SLOT, "electricity_main")
                 if s["attribution"] == "ev")
        self.assertAlmostEqual(ev, self._row()["imp_kwh_ev"], places=6)


def _failing(store, fn):
    ps = _segments_fail(store)
    for p in ps:
        p.start()
    try:
        return fn()
    finally:
        for p in ps:
            p.stop()


class TheMeasuredApply(unittest.TestCase):
    """A failed write used to leave the block stamped 'measured' — which the drain never
    revisits — under its pre-bill segments. Now nothing is stamped and the block stays a
    candidate."""
    setUp, tearDown = MA.TestMeasuredApply.setUp, MA.TestMeasuredApply.tearDown
    _blk, _row = MA.TestMeasuredApply._blk, MA.TestMeasuredApply._row
    SLOT = MA.TestMeasuredApply.SLOT

    def _apply(self):
        return engine.apply_measured_to_block(self.SLOT, cost_incl=round(3.0 * 0.05493, 6),
                                              label="OFF_PEAK")

    def test_a_failed_segment_write_stamps_nothing(self):
        self._blk(3.0, None)
        self.assertFalse(_failing(self.st, self._apply))
        self.assertEqual(self._row()["rate_source"], "reconciled")      # not 'measured'

    def test_it_still_writes_both(self):
        """GUARD: with the write working, the block is measured and segmented."""
        self._blk(3.0, None)
        self.assertTrue(self._apply())
        self.assertEqual(self._row()["rate_source"], "measured")
        self.assertTrue(self.st.get_block_segments(self.SLOT, "electricity_main"))


class TheRepriceSweep(unittest.TestCase):
    """An imported block that already has house-only segments, admitted to the sweep for its
    missing ex-VAT, with a completed dispatch: the sweep filled its EV columns and left the
    segments house-only, because it rewrote segments only on a block that had none. Every
    surface reads the segments, so the EV never appeared. Now the split's segments go with its
    columns. Discriminating: the first case fails on the parent."""
    setUp, tearDown = RS.TestRepriceHistoryBlockCore.setUp, RS.TestRepriceHistoryBlockCore.tearDown
    _blk = RS.TestRepriceHistoryBlockCore._blk
    _segs = RS.TestRepriceHistoryBlockCore._segs
    B = "2025-01-04T02:00:00"

    def _house_only(self):
        self._blk(self.B, 3.0, 0.30, 0.90, source="imported_api", dispatch=2.0)
        self.st.set_block_segments(self.B, "electricity_main",
                                   [(3.0, 0.30, None, "standard", "house")])
        self.st._conn.commit()
        rows = self.st.get_blocks_needing_reprice()                  # the sweep's own selection
        self.assertEqual([(r["block_start"], r["needs_segments"]) for r in rows], [(self.B, 0)])
        return rows

    def test_the_split_reaches_the_segments(self):
        engine._reprice_history_blocks(self._house_only(), "UTC", RS._RATE_SEGS)
        ev_col = self.st._conn.execute("SELECT imp_kwh_ev FROM blocks WHERE block_start=?",
                                       (self.B,)).fetchone()[0]
        self.assertAlmostEqual(ev_col, 2.0, places=6)
        self.assertAlmostEqual(sum(s["kwh"] for s in self._segs(self.B) if s["attribution"] == "ev"),
                               2.0, places=6)

    def test_a_block_already_split_keeps_its_segments(self):
        """GUARD: the fill-only guard still holds — a split block's segments are not rewritten."""
        rows = self._house_only()
        with self.st._conn:
            self.st._conn.execute("UPDATE blocks SET imp_kwh_ev = 1.0 WHERE block_start = ?", (self.B,))
        engine._reprice_history_blocks(rows, "UTC", RS._RATE_SEGS)
        self.assertEqual([(s["kwh"], s["attribution"]) for s in self._segs(self.B)], [(3.0, "house")])


if __name__ == "__main__":
    unittest.main()
