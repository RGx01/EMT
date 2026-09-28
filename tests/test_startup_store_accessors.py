"""
The `_engine_startup_impl` raw-connection cluster — workstream A acceptance 5.

Four ad-hoc SELECTs in one function, now four `ReadStore` methods. Three are
mechanical. The fourth was not, and is the reason this slice is worth reading.

THE MIX SEED WAS NOT A 4-HOUR WINDOW. It compared `b.block_start` against
`datetime('now', '-4 hours')`, which SQLite renders with a SPACE between date and
time, where `block_start` is stored ISO-style with a 'T'. Ordinary string
comparison then puts every same-date row above the cutoff, because 'T' (84)
sorts after ' ' (32):

    '2026-09-23T00:30:00' >= '2026-09-23 17:44:05'   ->  True

So the seed took everything from the current calendar day: almost nothing just
after midnight, a full day by late evening, and never the four hours it said. The
port computes the cutoff in Python, in the stored format, which makes the window
real. That is a behaviour change, deliberately, and it is pinned below both ways
-- the new behaviour asserted, and the old comparison demonstrated failing -- so
nobody re-derives the "simpler" SQL version later.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from block_store import BlockStore          # noqa: E402


def _mk_store():
    st = BlockStore(tempfile.mktemp(suffix=".db"))
    st._conn.execute("PRAGMA foreign_keys=OFF")
    return st


def _block(st, block_start, meter_id="electricity_main", interpolated=0):
    block_end = (datetime.fromisoformat(block_start)
                 + timedelta(minutes=30)).isoformat()
    st._conn.execute(
        "INSERT INTO blocks (block_start, block_end, config_period_id, meter_id, "
        "interpolated) VALUES (?,?,?,?,?)",
        (block_start, block_end, 1, meter_id, interpolated))
    st._conn.commit()
    return st._conn.execute(
        "SELECT id FROM blocks WHERE block_start=? AND meter_id=?",
        (block_start, meter_id)).fetchone()["id"]


def _iso(delta_hours):
    return (datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(hours=delta_hours)).replace(microsecond=0).isoformat()


class LastBlockEnd(unittest.TestCase):

    def setUp(self):
        self.st = _mk_store()

    def test_none_when_the_meter_has_no_blocks(self):
        self.assertIsNone(self.st.last_block_end("sub_meter_ev"))

    def test_the_latest_end_and_nothing_from_another_meter(self):
        _block(self.st, "2026-09-01T00:00:00", "sub_meter_ev")
        _block(self.st, "2026-09-05T00:00:00", "sub_meter_ev")
        _block(self.st, "2026-09-09T00:00:00", "electricity_main")
        self.assertEqual(self.st.last_block_end("sub_meter_ev"),
                         "2026-09-05T00:30:00")


class HasInterpolatedBlock(unittest.TestCase):

    def setUp(self):
        self.st = _mk_store()

    def test_false_when_absent(self):
        self.assertFalse(self.st.has_interpolated_block("2026-09-01T00:00:00"))

    def test_a_real_block_at_that_start_does_not_count(self):
        """The guard exists to stop a SECOND gap fill overwriting a FIRST one.
        A normally-captured block is not what it is asking about."""
        _block(self.st, "2026-09-01T00:00:00", interpolated=0)
        self.assertFalse(self.st.has_interpolated_block("2026-09-01T00:00:00"))

    def test_true_for_an_interpolated_block(self):
        _block(self.st, "2026-09-01T00:00:00", interpolated=1)
        self.assertTrue(self.st.has_interpolated_block("2026-09-01T00:00:00"))

    def test_it_is_meter_scoped(self):
        _block(self.st, "2026-09-01T00:00:00", "sub_meter_ev", interpolated=1)
        self.assertFalse(self.st.has_interpolated_block("2026-09-01T00:00:00"))
        self.assertTrue(self.st.has_interpolated_block(
            "2026-09-01T00:00:00", meter_id="sub_meter_ev"))


class GenerationMixSince(unittest.TestCase):

    def setUp(self):
        self.st = _mk_store()

    def _seed(self, hours_ago, fuels):
        bid = _block(self.st, _iso(hours_ago))
        self.st.upsert_generation_mix(bid, fuels)

    def test_groups_by_block(self):
        self._seed(1, [{"fuel": "wind", "perc": 40.0},
                       {"fuel": "gas", "perc": 60.0}])
        out = self.st.get_generation_mix_since(hours=4)
        self.assertEqual(len(out), 1)
        (mix,) = out.values()
        self.assertEqual(sorted(f["fuel"] for f in mix), ["gas", "wind"])

    def test_the_window_is_actually_hours_not_a_calendar_day(self):
        """The bug this port fixed. A block 17 hours old must be OUT of a 4-hour
        window -- under the old SQL it was in, whenever it fell on today's
        date."""
        self._seed(1, [{"fuel": "wind", "perc": 10.0}])
        self._seed(17, [{"fuel": "gas", "perc": 90.0}])
        out = self.st.get_generation_mix_since(hours=4)
        self.assertEqual(len(out), 1, "the 17-hour-old block leaked in: %s" % out)

    def test_the_old_comparison_really_did_leak(self):
        """The premise, demonstrated rather than asserted. Not a test of our
        code -- a test of the claim in the docstring, so the fix cannot be
        quietly reverted as unnecessary."""
        c = sqlite3.connect(":memory:")
        leaked = c.execute(
            "SELECT ? >= datetime('now', '-4 hours')",
            (datetime.now(timezone.utc).replace(tzinfo=None)
             .replace(hour=0, minute=30, second=0, microsecond=0).isoformat(),)
        ).fetchone()[0]
        c.close()
        # True whenever 'now' is later than 04:30 UTC, which is the ordinary case.
        if datetime.now(timezone.utc).hour >= 5:
            self.assertEqual(leaked, 1,
                             "expected the space-vs-T comparison to over-match")

    def test_it_is_meter_scoped(self):
        bid = _block(self.st, _iso(1), "sub_meter_ev")
        self.st.upsert_generation_mix(bid, [{"fuel": "wind", "perc": 50.0}])
        self.assertEqual(self.st.get_generation_mix_since(hours=4), {})
        self.assertEqual(
            len(self.st.get_generation_mix_since(hours=4, meter_id="sub_meter_ev")), 1)

    def test_it_is_not_the_weighted_range_method(self):
        """`get_generation_mix_for_range` answers a different question -- ONE
        kWh-weighted average across the range. Keeping both is deliberate."""
        self.assertNotEqual(
            BlockStore.get_generation_mix_since,
            BlockStore.get_generation_mix_for_range)


class CountMixHistory(unittest.TestCase):

    def test_zero_then_counts(self):
        st = _mk_store()
        self.assertEqual(st.count_mix_history(), 0)
        st.upsert_mix_history("2026-09-01T00:00:00",
                              [{"fuel": "wind", "perc": 100.0}])
        self.assertGreater(st.count_mix_history(), 0)


class AllFourAreReads(unittest.TestCase):

    def test_they_are_on_the_read_surface(self):
        from store_types import ReadStore
        for name in ("last_block_end", "has_interpolated_block",
                     "get_generation_mix_since", "count_mix_history"):
            self.assertTrue(hasattr(ReadStore, name), name)


if __name__ == "__main__":
    unittest.main()
