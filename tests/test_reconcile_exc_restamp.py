"""
test_reconcile_exc_restamp.py — BL-23/BL-9, BL-58, BL-58b: a reconcile revert's ex-VAT and split.

The settlement reconciliation pass rewrote imp_rate/imp_cost on a revert or restore and once LEFT the
ex-VAT figure and the EV split at the OLD band (BL-23/BL-9: a stale off-peak ex-VAT on a peak block;
BL-58: the split frozen off-peak on a capped revert; BL-58b: a split left at the old rate by an
earlier revert). Each was fixed by a re-stamp of its own.

Engine step 4c (26 Oct 2026): the pass settles each candidate with settle(), which writes the main,
its legs and its devices together from the facts — so none of those can be left behind, and the
re-stamps went. ex-VAT is each leg's rate through the VAT calendar (design §4 (c)), so with no
ex-VAT schedule it is the calendar's, not NULL (REVERSED from BL-23). The EV is the completed
dispatch's: a planned-only slot never charged and has none. A genuine two-rate capped half-hour is
settle()'s cap legs (tests/test_settle.py, TheRate).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine
from block_store import BlockStore

PEAK_INC = 32.3092          # pence (schedule.resolve)
PEAK_EXC = 32.3092 / 1.05   # pence (schedule.exc.resolve, 5% VAT)


class _ExcSched:
    def resolve(self, ts):
        return PEAK_EXC


class _Sched:
    """Import schedule stub in pence, optionally with an .exc sub-schedule."""
    def __init__(self, with_exc=True):
        if with_exc:
            self.exc = _ExcSched()

    def is_empty(self):
        return False

    def off_peak_rate_near(self, ts):
        return 5.493

    def day_rate_bounds(self, ts):
        return (5.493, PEAK_INC)

    def resolve(self, ts):
        return PEAK_INC


class TestReconcileRestampsExc(unittest.IsolatedAsyncioTestCase):

    SLOT = "2020-01-01T20:00:00"

    def setUp(self):
        from unittest import mock
        # engine step 4c: the reconcile settles through settle(), which asks the tariff kind
        _tk = mock.patch.object(engine, "tariff_kind_at", lambda bs: "intelligent")
        _tk.start()
        self.addCleanup(_tk.stop)

    def _seed_stale(self, st, *, completed_kwh=None, ev_seg=None):
        # Block currently off-peak with a STALE off-peak exc and a planned dispatch (completed only
        # if `completed_kwh`, no started) → the prediction reverts it to peak.
        st._conn.execute(
            "INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, "
            "block_minutes, timezone) VALUES (1, '2020-01-01T00:00:00', 1, 30, 'UTC')")
        st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
            "imp_kwh, imp_rate, imp_cost, imp_cost_exc, "
            "rate_corrected, imp_kwh_api) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self.SLOT, self.SLOT, "electricity_main", 1, 1.0, 0.05493, 0.05493,
             0.052314, 0, 1.0))
        st.upsert_dispatch_slot(self.SLOT, off_peak=True, provider="Myenergi",
                                source="smart-charge", state="planned")
        st.record_dispatch_history(self.SLOT, "planned", provider="Myenergi")
        if completed_kwh is not None:
            st.record_dispatch_history(self.SLOT, "completed", provider="Myenergi",
                                       energy_kwh=-abs(completed_kwh))
        if ev_seg is not None:                      # a stale off-peak split, as segments
            import pricing_segments as ps
            st.set_block_segments(self.SLOT, "electricity_main", [
                ps.Segment(ev_seg, 0.05493, 0.052314, "off_peak", "ev"),
                ps.Segment(round(1.0 - ev_seg, 6), 0.05493, 0.052314, "off_peak", "house")])
        st._conn.commit()

    async def _run(self, st, with_exc=True, capped=False):
        from unittest import mock
        engine._store = st
        engine._RECONCILE_SETTLE_HOURS = 0.0
        engine._kraken_rate_schedules = {"import": _Sched(with_exc)}
        if capped:
            engine._kraken_rate_schedules.update(ev_device_off_peak=_Sched(with_exc),
                                                 ev_device_peak=_Sched(with_exc))
        try:
            with mock.patch.object(engine, "capped_at", lambda bs: capped):
                return await engine.reconcile_dispatch_overlay()
        finally:
            engine._store = None

    def _row(self, st):
        # imp_rate_exc dropped (step 5b, 10 Oct 2026): the ex-VAT rate is derived from the
        # pair, imp_rate × imp_cost_exc / imp_cost.
        return st._conn.execute(
            "SELECT imp_rate, imp_cost, imp_cost_exc FROM blocks WHERE block_start=?",
            (self.SLOT,)).fetchone()

    def _segs(self, st):
        import pricing_segments as ps
        return [ps.Segment(**x) for x in st.get_block_segments(self.SLOT, "electricity_main")]

    async def test_revert_restamps_exc_to_new_band(self):
        st = BlockStore(":memory:")
        self._seed_stale(st)
        res = await self._run(st, with_exc=True)
        self.assertEqual(res["settled"], 1)
        r = self._row(st)
        self.assertAlmostEqual(r["imp_rate"], 0.323092, places=5)          # peak inc
        # exc follows the PEAK rate — NOT left at the stale off-peak 0.052314
        self.assertAlmostEqual(r["imp_cost_exc"], 1.0 * 0.323092 / 1.05, places=5)
        self.assertAlmostEqual(r["imp_rate"] * r["imp_cost_exc"] / r["imp_cost"],
                               0.323092 / 1.05, places=5)

    async def test_with_no_exc_schedule_the_calendar_gives_the_exc(self):
        """REVERSED (design §4 (c)): BL-23 NULLed ex-VAT where no exc schedule covered the slot;
        settle() takes it through the VAT calendar, one way, whatever the schedule carries."""
        st = BlockStore(":memory:")
        self._seed_stale(st)
        await self._run(st, with_exc=False)
        r = self._row(st)
        self.assertAlmostEqual(r["imp_rate"], 0.323092, places=5)
        self.assertAlmostEqual(r["imp_cost_exc"], round(0.323092 / 1.05, 6), places=6)

    async def test_a_planned_only_revert_has_no_ev(self):
        """The revert's legs carry the new rate and sum to the cost. A planned dispatch that never
        completed never charged, so a stale EV split goes (engine step 4c: settle() reads no stored
        split)."""
        st = BlockStore(":memory:")
        self._seed_stale(st, ev_seg=0.23)
        await self._run(st, with_exc=True)
        segs = self._segs(st)
        self.assertEqual([(x.attribution, x.band) for x in segs], [("house", "day")])
        self.assertAlmostEqual(sum(x.kwh * x.inc_rate for x in segs), self._row(st)["imp_cost"],
                               places=6)

    async def test_revert_restamps_split_on_capped(self):
        """BL-58: a capped revert left the EV frozen off-peak on a peak block (the 23/08 bump).
        settle() prices a capped half-hour's legs by the cap: a completed charge under 0.4 kWh with
        no `started` is a bump, so the EV is peak and the house day, at the peak rate."""
        st = BlockStore(":memory:")
        self._seed_stale(st, completed_kwh=0.23, ev_seg=0.23)
        await self._run(st, with_exc=True, capped=True)
        segs = self._segs(st)
        ev = [x for x in segs if x.attribution == "ev"]
        ho = [x for x in segs if x.attribution == "house"]
        self.assertTrue(ev and abs(ev[0].inc_rate - 0.323092) < 1e-5 and ev[0].band == "peak")
        self.assertAlmostEqual(sum(x.kwh for x in ev), 0.23, places=6)
        self.assertTrue(ho and ho[0].band == "day")
        self.assertAlmostEqual(sum(x.kwh * x.inc_rate for x in segs), self._row(st)["imp_cost"],
                               places=6)

    async def test_a_stale_rated_split_does_not_survive(self):
        """BL-58b: a block already at peak whose split was left at the old off-peak rate. settle()
        rebuilds the legs from the facts, so they carry the block's band and rate."""
        st = BlockStore(":memory:")
        self._seed_stale(st, completed_kwh=0.23, ev_seg=0.23)
        st._conn.execute("UPDATE blocks SET imp_rate=0.323092, imp_cost=0.323092 "
                         "WHERE block_start=?", (self.SLOT,))
        st._conn.commit()
        await self._run(st, with_exc=True)
        self.assertEqual({round(x.inc_rate, 6) for x in self._segs(st)}, {0.323092})

    async def test_same_band_rounding_is_not_rewritten(self):
        """GUARD: segments a rounding unit off the rate, in the same band, are within the write's
        tolerance — nothing is rewritten (BL-58b's refinement)."""
        import pricing_segments as ps
        st = BlockStore(":memory:")
        self._seed_stale(st)
        st._conn.execute("UPDATE blocks SET imp_rate=0.323092, imp_cost=0.323092, "
                         "imp_cost_exc=? WHERE block_start=?",
                         (round(0.323092 / 1.05, 6), self.SLOT))
        st.set_block_segments(self.SLOT, "electricity_main", [
            ps.Segment(1.0, 0.32300, 0.30762, "day", "house")])
        st._conn.commit()
        res = await self._run(st, with_exc=True)
        self.assertEqual(res["settled"], 0)
        self.assertTrue(all(abs(x.inc_rate - 0.32300) < 1e-9 for x in self._segs(st)))

if __name__ == "__main__":
    unittest.main()
