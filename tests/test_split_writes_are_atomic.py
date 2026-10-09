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
import test_reconcile_exc_restamp as RC          # noqa: E402
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
        """GUARD: the fill-only guard still holds — a split block's segments are not rewritten.
        "Split" is the segments' (BL-79 part 3): an EV segment, whatever the column says."""
        rows = self._house_only()
        self.st.set_block_segments(self.B, "electricity_main",
                                   [(1.0, 0.30, None, "off_peak", "ev"),
                                    (2.0, 0.30, None, "standard", "house")])
        engine._reprice_history_blocks(rows, "UTC", RS._RATE_SEGS)
        self.assertEqual([(s["kwh"], s["attribution"]) for s in self._segs(self.B)],
                         [(1.0, "ev"), (2.0, "house")])


class TheReconcileVerdict(unittest.IsolatedAsyncioTestCase):
    """A revert used to commit the main row, devices and EV columns, then write the segments on
    their own: a failure left the block at the new rate over segments at the old one. Now the
    verdict and its segments are one transaction, and a failure leaves the block a candidate."""
    SLOT = RC.TestReconcileRestampsExc.SLOT
    _seed_stale = RC.TestReconcileRestampsExc._seed_stale
    _run = RC.TestReconcileRestampsExc._run

    def _rate(self, st):
        return st._conn.execute("SELECT imp_rate FROM blocks WHERE block_start=?",
                                (self.SLOT,)).fetchone()[0]

    async def test_a_failed_segment_write_moves_nothing(self):
        st = BA.BlockStore(":memory:")
        self._seed_stale(st)
        ps = _segments_fail(st)
        for p in ps:
            p.start()
        try:
            await self._run(st)
        finally:
            for p in ps:
                p.stop()
        self.assertAlmostEqual(self._rate(st), 0.05493, places=6)      # still off-peak

    async def test_it_still_reverts_both(self):
        """GUARD: with the write working, the rate and the segments move together."""
        st = BA.BlockStore(":memory:")
        self._seed_stale(st)
        await self._run(st)
        self.assertAlmostEqual(self._rate(st), 0.323092, places=6)
        self.assertEqual({round(s["inc_rate"], 6) for s in
                          st.get_block_segments(self.SLOT, "electricity_main")}, {0.323092})


class TheStaleRatedSplitHeal(unittest.IsolatedAsyncioTestCase):
    """The reconcile's stale-rated split heal (a block already at peak whose split is still
    priced off-peak) wrote the re-priced segments, then restamped the columns on their own: if
    the restamp failed, the segments had moved and the columns had not. Now restamp_ev_columns
    writes both. Discriminating: the first case fails on the parent (it writes the segments
    before the call that fails)."""
    SLOT = RC.TestReconcileRestampsExc.SLOT

    def _seed(self):
        import pricing_segments as ps
        st = BA.BlockStore(":memory:")
        st._conn.execute(
            "INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, "
            "block_minutes, timezone) VALUES (1, '2020-01-01T00:00:00', 1, 30, 'UTC')")
        st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
            "imp_kwh, imp_rate, imp_cost, imp_rate_exc, imp_cost_exc, exc_source, "
            "rate_corrected, rate_source, imp_kwh_api, imp_kwh_ev, imp_cost_ev, "
            "imp_rate_ev, imp_ev_band, imp_home_band) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.SLOT, self.SLOT, "electricity_main", 1, 3.186, 0.323092,
             round(3.186 * 0.323092, 6), 0.323092 / 1.05, round(3.186 * 0.323092 / 1.05, 6),
             "tariff", 0, "reconciled", 3.186, 2.24, round(2.24 * 0.05493, 6), 0.05493,
             "off_peak", "off_peak"))
        st.set_block_segments(self.SLOT, "electricity_main", [
            ps.Segment(2.24, 0.05493, 0.052314, "off_peak", "ev"),
            ps.Segment(0.946, 0.05493, 0.052314, "off_peak", "house")])
        st.upsert_dispatch_slot(self.SLOT, off_peak=True, provider="Myenergi",
                                source="smart-charge-completed", state="completed")
        st.record_dispatch_history(self.SLOT, "completed", provider="Myenergi")
        st._conn.commit()
        return st

    async def _reconcile(self, st):
        engine._store = st
        engine._RECONCILE_SETTLE_HOURS = 0.0
        engine._kraken_rate_schedules = {"import": RC._Sched(True),
                                         "ev_device_off_peak": RC._Sched(True),
                                         "ev_device_peak": RC._Sched(True)}
        try:
            await engine.reconcile_dispatch_overlay()
        finally:
            engine._store = None

    def _seg_rates(self, st):
        return {round(s["inc_rate"], 6) for s in st.get_block_segments(self.SLOT, "electricity_main")}

    async def test_a_failed_restamp_moves_neither(self):
        st = self._seed()
        with patch.object(st, "restamp_ev_columns", side_effect=RuntimeError("restamp failed")):
            await self._reconcile(st)
        self.assertEqual(self._seg_rates(st), {0.05493})                # segments not moved

    async def test_it_still_heals_both(self):
        """GUARD: with the write working, segments and columns move to peak together."""
        st = self._seed()
        await self._reconcile(st)
        self.assertEqual(self._seg_rates(st), {0.323092})
        self.assertAlmostEqual(st._conn.execute("SELECT imp_rate_ev FROM blocks WHERE block_start=?",
                                                (self.SLOT,)).fetchone()[0], 0.323092, places=5)


class TheCorrectionsRoute(unittest.TestCase):
    """A rate correction committed the main rows, the device rows, the segments' rates and the
    ex-VAT re-derivation each on its own. A failure part-way left the block corrected in some
    places and not others. Now it is one transaction. Discriminating: the first case fails on the
    parent (the main rows were committed before the failing step)."""

    def _correct(self, st):
        import test_correction_device_exc as CD
        client = CD.TS.make_client(store=st)
        with patch.object(CD.TS.server, "_corrections_api_gate_active", return_value=True):
            return client.post("/api/corrections/apply", json={
                "type": "rate", "channel": "import", "value": CD.OFF, "recalc_cost": True,
                "from_date": "2026-07-21", "to_date": "2026-07-21",
                "from_time": "04:30", "to_time": "05:00", "meter_id": "all",
                "confirm_multiband": True})

    def _main_rate(self, st):
        import test_correction_device_exc as CD
        return st._conn.execute("SELECT imp_rate FROM blocks WHERE block_start=? AND "
                                "meter_id='electricity_main'", (CD.BS,)).fetchone()[0]

    def test_a_failure_part_way_corrects_nothing(self):
        import test_correction_device_exc as CD
        st = CD._store()
        with patch.object(st, "set_import_exc_from_inc", side_effect=RuntimeError("failed")):
            try:
                self._correct(st)
            except Exception:
                pass
        self.assertAlmostEqual(self._main_rate(st), CD.PEAK, places=6)   # not corrected

    def test_it_still_corrects(self):
        """GUARD: with nothing failing, the correction lands."""
        import test_correction_device_exc as CD
        st = CD._store()
        self.assertEqual(self._correct(st).status_code, 200)
        self.assertAlmostEqual(self._main_rate(st), CD.OFF, places=6)


if __name__ == "__main__":
    unittest.main()
