"""
BL-79 (part 2) — an EV-split writer commits its segments and its columns together (8 Oct 2026).

Each writer of a main block's EV split wrote the imp_*_ev columns and the block's segments in two
transactions, and the segment write was best-effort. A failed segment write left the columns
saying one split and the segments — what every surface reads — another. Now each writer passes
its segments to the store method that writes its columns, inside one transaction: if the segment
write fails, neither changes.

The atomicity cases make the segment write fail (whichever store call does it on either tree) and
assert the columns are unchanged. The reprice sweep's case (a path that wrote the columns and no
segments at all) went with the sweep in engine step 4e. Discriminating: each first case fails on the parent; the rest are guards.

Step 5c (10 Oct 2026) dropped the imp_*_ev columns: the split IS the segments, so there is no
second write for them to disagree with. The cases that asserted "the columns are unchanged" now
assert "no split was written" (guards on this tree), and the stale-rated split heal's
restamp_ev_columns case went with that method.
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_ev_back_attribution as BA            # noqa: E402  (store + schedule harness)
import test_measured_apply as MA                 # noqa: E402
import test_reconcile_exc_restamp as RC          # noqa: E402
import ev_split                                  # noqa: E402
engine = BA.engine

# engine step 4c: the carve and the reconcile settle through settle(), which asks the tariff kind;
# the harnesses borrowed here load no agreement (their own modules stub it only when they run)
from unittest import mock as _mock                # noqa: E402
_TK = _mock.patch.object(engine, "tariff_kind_at", lambda bs: "intelligent")


def setUpModule():
    _TK.start()


def tearDownModule():
    _TK.stop()


def _segments_fail(store):
    """Make every segment write on `store` raise, on this tree and the parent."""
    boom = RuntimeError("segment write failed")
    return [patch.object(store, "set_block_segments", side_effect=boom),
            patch.object(store, "_replace_segments", side_effect=boom, create=True)]


class TheBackAttribution(unittest.TestCase):
    """The back-attribution is the carve since 16 Oct 2026 (bill and split paths design §3.3 2,
    6b); these run it."""
    setUp, tearDown = BA.TestEvBackAttribution.setUp, BA.TestEvBackAttribution.tearDown
    _completed = BA.TestEvBackAttribution._completed
    SLOT = BA.TestEvBackAttribution.SLOT

    def _block(self, imp_kwh=3.0, ev=None):
        """A main block with no EV split (step 5c: no EV segment; `ev` must be None)."""
        assert ev is None
        self.st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
            "imp_kwh, imp_rate, imp_cost) VALUES (?,?,?,1,?,?,?)",
            (self.SLOT, self.SLOT, "electricity_main", imp_kwh, BA.OFF,
             round(imp_kwh * BA.OFF, 6)))

    def _row(self):
        return self.st._conn.execute(
            "SELECT " + ev_split.sql_kwh("b") + " AS imp_kwh_ev FROM blocks b "
            "WHERE block_start=? AND meter_id='electricity_main'", (self.SLOT,)).fetchone()

    def test_a_failed_segment_write_leaves_the_columns(self):
        """GUARD since step 5c: a failed segment write leaves no split (and does not raise)."""
        self._block(imp_kwh=3.0, ev=None)
        self._completed(energy=-2.0)
        ps = _segments_fail(self.st)
        for p in ps:
            p.start()
        try:
            asyncio.run(engine.run_iog_split_carve())
        finally:
            for p in ps:
                p.stop()
        self.assertIsNone(self._row()["imp_kwh_ev"])

    def test_it_still_writes_both(self):
        """GUARD: with the write working, the segments carry the EV."""
        self._block(imp_kwh=3.0, ev=None)
        self._completed(energy=-2.0)
        self.assertEqual(asyncio.run(engine.run_iog_split_carve())["carved"], 1)
        ev = sum(s["kwh"] for s in self.st.get_block_segments(self.SLOT, "electricity_main")
                 if s["attribution"] == "ev")
        self.assertAlmostEqual(ev, 2.0, places=6)
        self.assertAlmostEqual(self._row()["imp_kwh_ev"], 2.0, places=6)


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
    """A failed write used to leave the block stamped 'settled' — which the drain never
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
        self.assertEqual(self._row()["rate_source"], "reconciled")      # not 'settled'

    def test_it_still_writes_both(self):
        """GUARD: with the write working, the block is measured and segmented."""
        self._blk(3.0, None)
        self.assertTrue(self._apply())
        self.assertEqual(self._row()["rate_source"], "settled")
        self.assertTrue(self.st.get_block_segments(self.SLOT, "electricity_main"))


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


# TheStaleRatedSplitHeal went with the heal (engine step 4c, 26 Oct 2026): the reconcile settles its
# candidates with settle(), which rebuilds the legs from the facts in one write, so a stale-rated
# split cannot survive — test_reconcile_exc_restamp.test_a_stale_rated_split_does_not_survive.


class TheCorrectionsRoute(unittest.TestCase):
    """A rate correction committed the main rows, the device rows, the segments' rates and the
    ex-VAT re-derivation each on its own. A failure part-way left the block corrected in some
    places and not others. Now it is one transaction. Discriminating: the first case fails on the
    parent (the main rows were committed before the failing step)."""

    def _correct(self, st):
        import test_correction_device_exc as CD
        client = CD.TS.make_client(store=st)
        # a CAD account: corrections are refused with the supplier API (BL-75 (e))
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


class TheSplitTheyRebuildFrom(unittest.IsolatedAsyncioTestCase):
    """BL-79 part 3: the reconcile verdict rebuilds a block's segments from its split, and
    measured settlement prefers the block's stored EV; both read the imp_kwh_ev COLUMN (since gone, step 5c). With the
    EV in the segments and the columns blank (the gap-fill shape), the verdict rebuilt the block
    house-only and settlement took the dispatch instead. Both now read ev_split's split.
    Discriminating: both fail on the parent."""
    SLOT = RC.TestReconcileRestampsExc.SLOT
    _seed_stale = RC.TestReconcileRestampsExc._seed_stale
    _run = RC.TestReconcileRestampsExc._run

    async def test_a_revert_takes_the_ev_from_the_dispatch(self):
        """REVERSED by engine step 4c: the revert kept the segments' EV. settle() reads no stored
        split, and a planned dispatch that never completed never charged — so no EV, on the new
        rate (test_reconcile_exc_restamp.test_a_planned_only_revert_has_no_ev)."""
        st = BA.BlockStore(":memory:")
        self._seed_stale(st)                                   # 1.0 kWh, off-peak, reverts to peak
        st.set_block_segments(self.SLOT, "electricity_main", [
            (0.6, 0.05493, 0.052314, "off_peak", "ev"), (0.4, 0.05493, 0.052314, "off_peak", "house")])
        await self._run(st)
        segs = st.get_block_segments(self.SLOT, "electricity_main")
        self.assertEqual(sum(x["kwh"] for x in segs if x["attribution"] == "ev"), 0)
        self.assertEqual({round(x["inc_rate"], 6) for x in segs}, {0.323092})

    def test_measured_settlement_takes_the_dispatch_not_the_stored_split(self):
        h = MA.TestMeasuredApply("test_no_dispatch_stays_house_only")
        h.setUp()
        try:
            h._blk(3.0, None)
            h.st.set_block_segments(h.SLOT, "electricity_main", [
                (1.0, 0.05493, None, "off_peak", "ev"), (2.0, 0.05493, None, "off_peak", "house")])
            h.st.record_dispatch_history(h.SLOT, "completed", provider="Myenergi",
                                         source="unknown", energy_kwh=-2.0)
            engine.apply_measured_to_block(h.SLOT, cost_incl=round(3.0 * 0.05493, 6), label="OFF_PEAK")
            ev = sum(x["kwh"] for x in h.st.get_block_segments(h.SLOT, "electricity_main")
                     if x["attribution"] == "ev")
            # REVERSED by engine step 4b (design §3): settle() reads no stored split — the EV off
            # IOG-SMB is the completed dispatch, a fact. A stored EV that differs from it was
            # written by a writer that did not follow a restated dispatch (staging, 24 Oct 2026
            # 04:00: the carve kept 2.67 kWh, the dispatch now 2.55). Until then the stored 1.0 won.
            self.assertAlmostEqual(ev, 2.0, places=6)
        finally:
            h.tearDown()


if __name__ == "__main__":
    unittest.main()
