"""
BL-79 (part 3) — the engine's "is this block split?" checks read ev_split's split (9 Oct 2026).

Every surface reads a block's EV split from its segments, and since part 2 every writer commits
segments and columns together. The engine's own checks still read the columns: `imp_kwh_ev IS
NULL` meant "not split yet" to the carve, the back-attribution, the settled-split heal and the
reprice sweep's fill guard, and `imp_kwh_ev > 0` picked the dispatch-only re-fit's blocks. Where
the two disagreed — a gap-fill rewrite blanked the columns over EV segments; an older writer left a
column over house-only segments — those checks decided against the record. They now read
ev_split: the EV segments.

Step 5c (10 Oct 2026): the imp_*_ev columns are gone, so the fixtures carry the split as segments
only, and the LEGACY cases (a block with no segments split by its columns) went with them.

Discriminating: every case failed on the parent of part 3.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from block_store import BlockStore                # noqa: E402

BS = "2026-09-10T02:00:00"
RATE = 0.05493
EV_SEGS = [(2.0, RATE, None, "off_peak", "ev"), (1.0, RATE, None, "off_peak", "house")]
HOUSE_SEGS = [(3.0, RATE, None, "off_peak", "house")]


def _store(*, segs, rate_source="schedule", interpolated=0, device=False):
    st = BlockStore(":memory:")
    with st._conn:
        st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes,"
                         " timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
        st._conn.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter, meter_type) "
                         "VALUES (1,'electricity_main',0,'')")
        st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, interpolated, "
            "imp_kwh, imp_rate, imp_cost, rate_source) VALUES (?,?,?,1,?,?,?,?,?)",
            (BS, BS, "electricity_main", interpolated, 3.0, RATE, round(3 * RATE, 6),
             rate_source))
        st._conn.execute("INSERT INTO dispatch_history (slot_start, kind, energy_kwh, first_seen, "
                         "last_seen) VALUES (?, 'completed', -2.0, ?, ?)", (BS, BS, BS))
        if device:
            st._conn.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter, "
                             "meter_type) VALUES (1,'house_battery',1,'battery')")
            st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id,"
                             " imp_kwh, imp_kwh_grid, imp_rate, imp_cost) VALUES (?,?,"
                             "'house_battery',1,0.5,0.5,?,?)", (BS, BS, RATE, round(0.5 * RATE, 6)))
    if segs is not None:
        st.set_block_segments(BS, "electricity_main", segs)
    return st


def _starts(rows):
    return [r["block_start"] for r in rows]


class EvSegments(unittest.TestCase):
    """The gap-fill shape: the segments carry the EV (the imp_kwh_ev column was blank)."""

    def test_the_carve_leaves_it(self):
        self.assertEqual(_starts(_store(segs=EV_SEGS).get_blocks_awaiting_ev_carve(10)), [])

    def test_back_attribution_leaves_it(self):
        self.assertEqual(_starts(_store(segs=EV_SEGS).get_blocks_missing_ev_split()), [])

    def test_the_carve_guard_refuses_it(self):
        st = _store(segs=EV_SEGS)
        self.assertEqual(st.carve_ev_split(BS, segments=HOUSE_SEGS), 0)
        self.assertEqual([s["attribution"] for s in st.get_block_segments(BS, "electricity_main")],
                         ["ev", "house"])

    def test_the_sweep_guard_refuses_it(self):
        st = _store(segs=EV_SEGS)
        self.assertEqual(st.set_blocks_iog_split([(BS, "electricity_main", HOUSE_SEGS, 0)]), 0)

    def test_the_dispatch_only_refit_takes_it(self):
        st = _store(segs=EV_SEGS, device=True)
        self.assertEqual(st.dispatch_house_candidates(), [BS])


class HouseOnlySegments(unittest.TestCase):
    """An older writer left imp_kwh_ev over these; the segments say no split."""

    def test_the_carve_takes_it(self):
        self.assertEqual(_starts(_store(segs=HOUSE_SEGS).get_blocks_awaiting_ev_carve(10)),
                         [BS])


class BlocksWithNoSegments(unittest.TestCase):
    """With no segments at all, the block has no EV split."""

    def test_an_unsplit_one_is_carved(self):
        self.assertEqual(_starts(_store(segs=None).get_blocks_awaiting_ev_carve(10)), [BS])


class TheLoadedBlock(unittest.TestCase):
    """A block dict the engine loads (get_block_dict_by_start, _select_blocks, …) carries
    ev_split's split, so PASS 2's EV reservation and the re-fit follow the segments."""

    def _main_imp(self, st):
        return st.get_block_dict_by_start(BS)["meters"]["electricity_main"]["channels"]["import"]

    def test_ev_segments_load_the_segments_split(self):
        imp = self._main_imp(_store(segs=EV_SEGS))
        self.assertAlmostEqual(imp.get("kwh_ev") or 0.0, 2.0, places=6)
        self.assertAlmostEqual(imp.get("cost_ev") or 0.0, round(2.0 * RATE, 6), places=6)

    def test_house_only_segments_load_no_split(self):
        imp = self._main_imp(_store(segs=HOUSE_SEGS))
        self.assertNotIn("kwh_ev", imp)


if __name__ == "__main__":
    unittest.main()
