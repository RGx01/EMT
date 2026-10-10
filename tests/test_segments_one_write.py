"""
Step 5c — a block's segments are written in the block's own transaction (10 Oct 2026).

Finalise and the settlement re-run wrote a main block's segments first, in their own
best-effort transaction (a failure logged SEGMENT-WRITE-FAIL and was swallowed), and the block
row afterwards. Once the EV-split columns went, a block whose segment write failed while its
row landed would have kept its EV split nowhere. Now pricing builds the segments and leaves
them on the channel (STAGED_SEGMENTS), and the block write commits them with the row: both
land, or neither does.

Discriminating: test_finalise_never_lands_a_block_without_its_segments and
test_a_settlement_never_lands_without_its_segments fail on the parent. The others are guards.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import test_rebuild_meter_type as H              # noqa: E402  (stubs, real engine, flat tariff)
import block_store                               # noqa: E402
engine = H.engine


class _Fail(Exception):
    pass


class OneWrite(H._Harness):

    def _state(self):
        c = engine._store._conn
        row = c.execute("SELECT imp_kwh FROM blocks WHERE block_start=? "
                        "AND meter_id='electricity_main'", (H.START,)).fetchone()
        segs = engine._store.get_block_segments(H.START, "electricity_main")
        return row, segs

    def _failing_segments(self):
        return mock.patch.object(block_store.BlockStore, "_replace_segments",
                                 side_effect=_Fail("disk full"))

    def test_finalise_never_lands_a_block_without_its_segments(self):
        with self._failing_segments():
            try:
                self._finalise()
            except _Fail:
                pass
        row, segs = self._state()
        self.assertFalse(row is not None and not segs,
                         "the block row landed without its segments")

    def test_a_settlement_never_lands_without_its_segments(self):
        self._finalise()
        before = self._state()
        with self._failing_segments():
            try:
                self._settle(dcc=7.5)
            except _Fail:
                pass
        row, segs = self._state()
        self.assertAlmostEqual(row["imp_kwh"], before[0]["imp_kwh"], places=6,
                               msg="the settled row landed while its segments did not")
        self.assertEqual(segs, before[1])

    def test_finalise_writes_both(self):
        """GUARD: the ordinary case."""
        self._finalise()
        row, segs = self._state()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(sum(s["kwh"] for s in segs), H.MAIN, places=6)

    def test_nothing_is_left_staged_on_the_dict(self):
        """GUARD (new code): the block write takes the staged segments off, so a re-used dict cannot
        write them twice."""
        self._finalise()
        blk = engine._store.get_block_dict_by_start(H.START)
        blk["meters"]["electricity_main"]["imp_kwh_api"] = 7.5
        engine._rerun_pass2_for_settled_block(blk, billing_source="api", standing_resolver=None)
        imp = blk["meters"]["electricity_main"]["channels"]["import"]
        self.assertIn(engine.STAGED_SEGMENTS, imp)
        engine.append_block_replace(blk)
        self.assertNotIn(engine.STAGED_SEGMENTS, imp)

    def test_the_two_names_for_the_key_agree(self):
        """GUARD (new code): engine defines the key as a literal (tests stub block_store)."""
        self.assertEqual(engine.STAGED_SEGMENTS, block_store.STAGED_SEGMENTS)


if __name__ == "__main__":
    unittest.main()
