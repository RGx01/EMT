"""
BL-79 (part 2) — an EV-split writer commits its segments and its columns together (8 Oct 2026).

Each writer of a main block's EV split wrote the imp_*_ev columns and the block's segments in two
transactions, and the segment write was best-effort. A failed segment write left the columns
saying one split and the segments — what every surface reads — another. Now each writer passes
its segments to the store method that writes its columns, inside one transaction: if the segment
write fails, neither changes.

Each case makes the segment write fail (whichever store call does it on either tree) and asserts
the columns are unchanged. Discriminating: every case fails on the parent.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_ev_back_attribution as BA            # noqa: E402  (store + schedule harness)
import test_measured_apply as MA                 # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
