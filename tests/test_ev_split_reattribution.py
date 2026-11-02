"""
The dispatch-split population's store access — A acceptance 5; since 16 Oct 2026 (bill and split
paths design §3.3 2, 6b) one query, `get_blocks_off_dispatch_split`, for what the carve and
`_attribute_missing_ev_split` selected from opposite ends. Their cases are kept below, the two that
changed meaning labelled.

The job re-derives the EV/Home split on blocks that should have one and don't.
Octopus reports a slot's COMPLETED dispatch energy hours after the fact, usually
after the block was priced from a merely 'planned' dispatch, so the charge sits
silently in Home and nothing else revisits it — the reconcile's segment resync
only rewrites blocks that already have a split.

Two methods, and the interesting design point is that there are TWO write methods
for what looks like one operation:

    (carve_ev_split and replace_ev_split, the carve's two writes, went with engine step 4c:
    the carve settles each half-hour with settle())

A single method with an `overwrite=` flag would put "never stomp a manual
correction" one boolean away from being got wrong at a call site. Separate names
make the wrong one look wrong. The contrast is asserted below.
"""
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from block_store import BlockStore          # noqa: E402
import ev_split                             # noqa: E402


def _mk_store():
    st = BlockStore(tempfile.mktemp(suffix=".db"))
    st._conn.execute("PRAGMA foreign_keys=OFF")
    return st


def _segs(ev, house, ev_band="off_peak", home_band="off_peak", rate=0.25):
    """An EV/house split as segments (step 5c: the split IS the segments)."""
    return ([(ev, rate, None, ev_band, "ev")]
            + ([(house, rate, None, home_band, "house")] if house > 1e-9 else []))


def _block(st, start, ev=None, **cols):
    base = {"block_start": start, "block_end": start, "config_period_id": 1,
            "meter_id": "electricity_main", "imp_kwh": 5.0, "imp_cost": 1.25,
            "imp_rate": 0.25}
    base.update(cols)
    st._conn.execute("INSERT INTO blocks (%s) VALUES (%s)"
                     % (",".join(base), ",".join("?" * len(base))),
                     tuple(base.values()))
    st._conn.commit()
    if ev is not None:                         # the block's stored EV split, as segments
        st.set_block_segments(start, base["meter_id"], _segs(ev, base["imp_kwh"] - ev))


def _completed(st, slot, kwh=3.0):
    st.record_dispatch_history(slot_start=slot, kind="completed", provider="octopus",
                               source=None, energy_kwh=kwh, raw_start=None, raw_end=None)


S = "2026-09-01T00:00:00"


class OffDispatchSplitSelection(unittest.TestCase):
    """The no-split arm — the carve's and the attribution's cases together."""

    def setUp(self):
        self.st = _mk_store()

    def _got(self):
        return self.st.get_blocks_off_dispatch_split(10)

    def test_a_null_split_with_a_completed_dispatch_is_selected(self):
        _block(self.st, S)
        _completed(self.st, S)
        self.assertEqual(len(self._got()), 1)

    def test_a_null_rate_source_is_a_candidate(self):
        """A `NOT IN` against NULL is UNKNOWN; the query must not drop never-stamped blocks."""
        _block(self.st, S)
        _completed(self.st, S)
        self.assertIsNone(self.st._conn.execute("SELECT rate_source FROM blocks").fetchone()[0])
        self.assertEqual(len(self._got()), 1)

    def test_no_completed_dispatch_means_nothing_to_derive_from(self):
        _block(self.st, S)
        self.st.record_dispatch_history(slot_start=S, kind="planned", provider="octopus",
                                        source=None, energy_kwh=3.0, raw_start=None, raw_end=None)
        self.assertEqual(self._got(), [])

    def test_a_block_that_already_has_a_split_is_left_alone(self):
        _block(self.st, S, ev=3.0)
        _completed(self.st, S)
        self.assertEqual(self._got(), [])

    def test_imported_history_is_selected_too(self):
        """Changed 16 Oct 2026 (6b): the attribution left imported history out, the carve took
        it; the one population takes it — a completed dispatch is evidence either way."""
        _block(self.st, S, source="imported_csv")
        _completed(self.st, S)
        self.assertEqual(len(self._got()), 1)

    def test_a_settled_block_is_selected_and_the_carve_decides(self):
        """Changed 16 Oct 2026 (6b): the carve left settled blocks out, the attribution took
        them. The query takes them; the carve skips the final stage (settled, on an IOG-SMB
        agreement, a bill split cached) — a settled block below it (split pending, or before the
        agreement) takes the dispatch's split."""
        _block(self.st, S, rate_source="settled")
        _completed(self.st, S)
        self.assertEqual(len(self._got()), 1)

    def test_a_manual_correction_is_never_stomped(self):
        _block(self.st, S, rate_corrected=1)
        _block(self.st, "2026-09-02T00:00:00", rate_source="corrected")
        _completed(self.st, S)
        _completed(self.st, "2026-09-02T00:00:00")
        self.assertEqual(self._got(), [])

    def test_zero_kwh_has_nothing_to_split(self):
        _block(self.st, S, imp_kwh=0.0)
        _completed(self.st, S)
        self.assertEqual(self._got(), [])

    def test_starts_limits_it(self):
        _block(self.st, S)
        _block(self.st, "2026-09-02T00:00:00")
        _completed(self.st, S)
        _completed(self.st, "2026-09-02T00:00:00")
        self.assertEqual([r["block_start"] for r in
                          self.st.get_blocks_off_dispatch_split(10, starts=[S])], [S])


class GapFilledDisagreement(unittest.TestCase):
    """BL-39. A gap fill can stamp a WRONG non-NULL split from a stale or planned
    fragment, and the NULL-only rule above would never revisit it."""

    def setUp(self):
        self.st = _mk_store()

    def _got(self):
        return self.st.get_blocks_off_dispatch_split(10)

    def test_a_materially_wrong_gap_fill_is_reselected(self):
        _block(self.st, S, interpolated=1, ev=0.5)
        _completed(self.st, S, kwh=3.0)          # stored 0.5 vs actual 3.0
        self.assertEqual(len(self._got()), 1)

    def test_agreement_within_tolerance_is_left_alone(self):
        """The tolerance is 0.0005 kWh since engine step 4c (0.1 until then)."""
        _block(self.st, S, interpolated=1, ev=3.0)
        _completed(self.st, S, kwh=3.0004)
        self.assertEqual(self._got(), [])

    def test_a_restated_dispatch_is_reselected(self):
        """REVERSED by engine step 4c: 3.02 against 3.0 was within the old 0.1 kWh, and stayed."""
        _block(self.st, S, interpolated=1, ev=3.0)
        _completed(self.st, S, kwh=3.02)
        self.assertEqual(len(self._got()), 1)

    def test_a_normally_captured_block_is_reselected_on_disagreement(self):
        """REVERSED by engine step 4c: the BL-39 arm was scoped to gap-filled blocks, so a dispatch
        Octopus restated after the carve left a normally captured block on its first figure
        (staging, 24 Oct 2026 04:00). A completed dispatch that changes is a fact, for any block."""
        _block(self.st, S, interpolated=0, ev=0.5)
        _completed(self.st, S, kwh=3.0)
        self.assertEqual(len(self._got()), 1)

    def test_the_dispatch_is_grid_clipped_before_comparing(self):
        """A dispatch larger than the block's own import is clipped to it, so it cannot
        manufacture a disagreement out of a figure the grid never delivered."""
        _block(self.st, S, imp_kwh=2.0, interpolated=1, ev=2.0)
        _completed(self.st, S, kwh=9.0)          # clipped to 2.0 == stored
        self.assertEqual(self._got(), [])



class Surfaces(unittest.TestCase):

    def test_the_read_reads_and_the_write_writes(self):
        from store_types import ReadStore, WriteStore
        self.assertTrue(hasattr(ReadStore, "get_blocks_off_dispatch_split"))
        # replace_ev_split and carve_ev_split went with engine step 4c (the carve settles)
        self.assertFalse(hasattr(WriteStore, "replace_ev_split"))


if __name__ == "__main__":
    unittest.main()
