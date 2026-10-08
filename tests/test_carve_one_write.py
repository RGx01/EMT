"""
BL-79 (part 2) — the carve writes its segments in its own guarded transaction (8 Oct 2026).

run_iog_split_carve selects un-carved blocks, then writes each. It wrote the segments first,
unguarded, then the columns behind `imp_kwh_ev IS NULL`. A block carved between the select and
the write kept its columns but lost its segments — and it was counted as carved, and re-fitted.
When COALESCE kept a block's recorded band, the segment took the carve's band instead.

Now carve_ev_split takes the segments and writes them in the same transaction, only when its
guard let it write, each with the band the columns end with.

Discriminating: test_a_block_carved_since_it_was_selected_keeps_its_segments and
test_a_recorded_band_is_the_segments_band fail on the parent; the plain carve is a guard.
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_iog_split_carve as C                 # noqa: E402  (schedule stubs, store harness)
engine = C.engine
SLOT = "2026-09-10T02:00:00"


class TheCarve(unittest.TestCase):
    # The carve harness's fixture, without inheriting (and re-running) its tests.
    setUp, tearDown = C.TestIogSplitCarve.setUp, C.TestIogSplitCarve.tearDown
    _blk, _dispatch, _state = (C.TestIogSplitCarve._blk, C.TestIogSplitCarve._dispatch,
                               C.TestIogSplitCarve._state)

    def _ready(self):
        self._blk(SLOT, 3.45)
        self._dispatch(SLOT, 3.39)
        self.st._conn.commit()

    def test_the_plain_carve_writes_both(self):
        """GUARD: an un-carved block gets its split, columns and segments together."""
        self._ready()
        self.assertEqual(asyncio.run(engine.run_iog_split_carve())["carved"], 1)
        r, segs = self._state(SLOT)
        self.assertAlmostEqual(r["imp_kwh_ev"], 3.39, places=5)
        self.assertAlmostEqual(sum(s["kwh"] for s in segs if s["attribution"] == "ev"), 3.39, places=5)

    def test_a_block_carved_since_it_was_selected_keeps_its_segments(self):
        self._ready()
        rows = self.st.get_blocks_awaiting_ev_carve(10)      # selected while un-carved...
        theirs = [(1.0, C.OFF, round(C.OFF / 1.05, 6), "off_peak", "ev"),
                  (2.45, C.OFF, round(C.OFF / 1.05, 6), "off_peak", "house")]
        with self.st._conn:                                   # ...then split by someone else
            self.st._conn.execute("UPDATE blocks SET imp_kwh_ev = 1.0 WHERE block_start = ?", (SLOT,))
        self.st.set_block_segments(SLOT, "electricity_main", theirs)
        with patch.object(self.st, "get_blocks_awaiting_ev_carve", return_value=rows):
            res = asyncio.run(engine.run_iog_split_carve())
        self.assertEqual(res["carved"], 0)
        r, segs = self._state(SLOT)
        self.assertAlmostEqual(r["imp_kwh_ev"], 1.0, places=6)
        self.assertEqual([(s["kwh"], s["attribution"]) for s in segs], [(1.0, "ev"), (2.45, "house")])

    def test_a_recorded_band_is_the_segments_band(self):
        self._ready()
        with self.st._conn:
            self.st._conn.execute("UPDATE blocks SET imp_ev_band = 'peak' WHERE block_start = ?", (SLOT,))
        asyncio.run(engine.run_iog_split_carve())
        r, segs = self._state(SLOT)
        band = self.st._conn.execute("SELECT imp_ev_band FROM blocks WHERE block_start = ?",
                                     (SLOT,)).fetchone()[0]
        self.assertEqual(band, "peak")
        self.assertEqual({s["band"] for s in segs if s["attribution"] == "ev"}, {"peak"})


if __name__ == "__main__":
    unittest.main()
