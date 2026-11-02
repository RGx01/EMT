"""
`BlockStore.last_standing_charge` — the first slice of workstream A acceptance 5.

Three call sites in `engine.py` each wrote this SELECT against `_store._conn`,
independently, because `BlockStore` had no standing-charge accessor: the
finalise fallback for an unavailable SC sensor, and the two gap-fill paths
(tick and startup) that seed `build_gap_blocks(last_standing_charge=...)`.

The slice is small on purpose. It exercises the whole acceptance-5 pattern --
a new ReadStore method, several call sites, and a behaviour question to settle
-- on the population where the question is easiest to answer.

THE BEHAVIOUR QUESTION, settled here rather than assumed: the two gap-fill sites
hardcoded 'electricity_main' and the finalise site passed its own meter. The
method takes the meter, and each caller passes what it already passed, so the
convergence changes nothing. These tests pin that -- notably that the method is
meter-scoped, which is the only way the three can share one implementation
without the sub-meter case silently picking up the main meter's charge.

The `> 0` filter is likewise part of the question and not an optimisation: a
stored 0.0 means "not known for this block", so a newer zero must not shadow an
older real value.
"""
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from block_store import BlockStore          # noqa: E402


def _mk_store():
    st = BlockStore(os.path.join(tempfile.mkdtemp(), "blocks.db"))
    st._conn.execute("PRAGMA foreign_keys=OFF")
    return st


def _ins(st, block_start, meter_id, sc):
    st._conn.execute(
        "INSERT INTO blocks (block_start, block_end, config_period_id, meter_id, "
        "standing_charge) VALUES (?,?,?,?,?)",
        (block_start, block_start[:-2] + "30", 1, meter_id, sc))
    st._conn.commit()


class LastStandingCharge(unittest.TestCase):

    def setUp(self):
        self.st = _mk_store()

    def test_nothing_recorded_is_none_not_zero(self):
        """None and 0.0 are different answers. Each caller has its own default
        -- two coalesce to 0.0, one leaves `raw_sc` alone -- and collapsing them
        here would take that choice away."""
        self.assertIsNone(self.st.last_standing_charge("electricity_main"))

    def test_returns_the_most_recent(self):
        _ins(self.st, "2026-09-01T00:00:00", "electricity_main", 0.41)
        _ins(self.st, "2026-09-03T00:00:00", "electricity_main", 0.47)
        _ins(self.st, "2026-09-02T00:00:00", "electricity_main", 0.44)
        self.assertAlmostEqual(
            self.st.last_standing_charge("electricity_main"), 0.47)

    def test_a_newer_zero_does_not_shadow_an_older_value(self):
        """The `> 0` filter is the point. A block with standing_charge 0.0 has
        no charge RECORDED, not a charge OF zero -- every block written before
        settlement carries 0.0."""
        _ins(self.st, "2026-09-01T00:00:00", "electricity_main", 0.41)
        _ins(self.st, "2026-09-09T00:00:00", "electricity_main", 0.0)
        self.assertAlmostEqual(
            self.st.last_standing_charge("electricity_main"), 0.41)

    def test_null_is_not_a_case_the_schema_allows(self):
        """Written as a NULL test, and the schema refused it:
        `standing_charge REAL NOT NULL DEFAULT 0` (block_store.py:157). So
        "unknown" is only ever expressible as 0.0, which is why the `> 0` filter
        above is load-bearing rather than defensive -- there is no NULL for it to
        fall back on. Pinned so a future schema change that relaxes NOT NULL has
        to come back and answer the question."""
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            _ins(self.st, "2026-09-09T00:00:00", "electricity_main", None)

    def test_it_is_meter_scoped(self):
        """The convergence of three call sites onto one method is only safe
        because the meter is a parameter. A sub-meter must not inherit the main
        meter's standing charge by accident -- which is exactly what a method
        that hardcoded 'electricity_main' (as two of the three sites did) would
        have done had finalise_block been pointed at it."""
        _ins(self.st, "2026-09-01T00:00:00", "electricity_main", 0.47)
        self.assertIsNone(self.st.last_standing_charge("sub_meter_ev"))
        _ins(self.st, "2026-09-02T00:00:00", "sub_meter_ev", 0.11)
        self.assertAlmostEqual(self.st.last_standing_charge("sub_meter_ev"), 0.11)
        self.assertAlmostEqual(
            self.st.last_standing_charge("electricity_main"), 0.47)

    def test_it_is_on_the_read_surface_not_the_write_surface(self):
        """A read method that landed on WriteStore would be reachable only from
        the engine thread -- and two of the three call sites are on it, which is
        precisely how such a mistake would survive review."""
        from store_types import ReadStore, WriteStore
        self.assertTrue(hasattr(ReadStore, "last_standing_charge"))
        self.assertTrue(hasattr(WriteStore, "last_standing_charge"))


if __name__ == "__main__":
    unittest.main()
