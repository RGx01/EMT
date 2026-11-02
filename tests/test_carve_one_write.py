"""
BL-79 (part 2) — the carve writes its segments in its own guarded transaction (8 Oct 2026).

run_iog_split_carve selected un-carved blocks, then wrote each. It wrote the segments first,
unguarded, then the columns behind `imp_kwh_ev IS NULL`. A block carved between the select and
the write kept its columns but lost its segments — and it was counted as carved, and re-fitted.
When COALESCE kept a block's recorded band, the segment took the carve's band instead.

Engine step 4c (26 Oct 2026): the carve settles each half-hour with settle(), which reads the facts
when it writes and writes the main, legs and devices in ONE transaction — so nothing is half
written, and there is no second writer whose split a guard must keep: a split off the completed
dispatch is the dispatch's, and the band is the prediction's. Two tests are REVERSED, as labelled;
the plain carve is a guard.
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_iog_split_carve as C                 # noqa: E402  (schedule stubs, store harness)
engine = C.engine


# engine step 4c: the carve settles through settle(), which asks the tariff kind (the carve never
# did); these fixtures load no agreement, and the carve only ever acts on an Intelligent dispatch
from unittest import mock as _mock
_TK = _mock.patch.object(engine, "tariff_kind_at", lambda bs: "intelligent")


def setUpModule():
    _TK.start()


def tearDownModule():
    _TK.stop()

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
        """GUARD: an un-carved block gets its split (its segments)."""
        self._ready()
        self.assertEqual(asyncio.run(engine.run_iog_split_carve())["carved"], 1)
        r, segs = self._state(SLOT)
        self.assertAlmostEqual(r["imp_kwh_ev"], 3.39, places=5)
        self.assertAlmostEqual(sum(s["kwh"] for s in segs if s["attribution"] == "house"), 0.06, places=5)

    def test_a_split_written_since_the_selection_is_settled_to_the_dispatch(self):
        """REVERSED: the guard kept another writer's split. settle() re-reads the facts at the
        write, so the half-hour is the completed dispatch's, whatever was left between."""
        self._ready()
        rows = self.st.get_blocks_off_dispatch_split(10)      # selected while un-carved...
        theirs = [(1.0, C.OFF, round(C.OFF / 1.05, 6), "off_peak", "ev"),
                  (2.45, C.OFF, round(C.OFF / 1.05, 6), "off_peak", "house")]
        self.st.set_block_segments(SLOT, "electricity_main", theirs)   # ...then split by someone else
        with patch.object(self.st, "get_blocks_off_dispatch_split", return_value=rows):
            res = asyncio.run(engine.run_iog_split_carve())
        self.assertEqual(res["carved"], 1)
        r, segs = self._state(SLOT)
        self.assertAlmostEqual(r["imp_kwh_ev"], 3.39, places=6)

    def test_the_band_is_the_predictions(self):
        """REVERSED: the carve kept the house band the block recorded ('day'). settle() writes the
        prediction's — this half-hour is inside the off-peak window, so off-peak."""
        self._ready()
        with self.st._conn:
            self.st._conn.execute("UPDATE block_segments SET band = 'day' WHERE block_start = ?",
                                  (SLOT,))
        self.assertEqual(asyncio.run(engine.run_iog_split_carve())["carved"], 1)
        r, segs = self._state(SLOT)
        self.assertEqual({s["band"] for s in segs}, {"off_peak"})

if __name__ == "__main__":
    unittest.main()
