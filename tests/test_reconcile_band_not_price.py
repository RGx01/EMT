"""
#28 (3 Oct 2026) — reconcile_dispatch_overlay decided a slot's band by price EQUALITY.

`currently_off_peak = abs(cur_rate - off_peak) < 1e-6`. When the tariff's off-peak price
changed (Octopus edited its records for 1 Oct 2026's 0% VAT, so the schedule read 5.2314p
back to July), a correctly stored off-peak dispatch slot at 5.493p stopped "being
off-peak". The reconcile read that as a band disagreement and re-priced the slot to the
new figure. The band now comes from the nearer of the date's off-peak and base prices.

The first test fails on the parent tree. The others are GUARDS: genuine disagreements
still re-price, as before.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine                                   # noqa: E402
from block_store import BlockStore              # noqa: E402

PEAK = 32.3092        # pence
OFF_STORED = 5.493    # what the slot was priced at
OFF_NOW = 5.2314      # what the schedule says off-peak is now


class _Sched:
    def __init__(self, off):
        self.off = off

    def is_empty(self):
        return False

    def off_peak_rate_near(self, ts):
        return self.off

    def day_rate_bounds(self, ts):
        return (self.off, PEAK)

    def resolve(self, ts):
        return PEAK


class TheBandNotThePrice(unittest.IsolatedAsyncioTestCase):
    SLOT = "2026-09-24T12:00:00"          # 13:00 BST: a daytime smart charge

    def setUp(self):
        from unittest import mock
        # engine step 4c: the reconcile settles through settle(), which asks the tariff kind
        _tk = mock.patch.object(engine, "tariff_kind_at", lambda bs: "intelligent")
        _tk.start()
        self.addCleanup(_tk.stop)

    def _store(self, rate):
        st = BlockStore(":memory:")
        st._conn.execute(
            "INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, "
            "block_minutes, timezone) VALUES (1, '2020-01-01T00:00:00', 1, 30, 'UTC')")
        st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
            "imp_kwh, imp_rate, imp_cost, rate_corrected, imp_kwh_api) "
            "VALUES (?,?,?,1,?,?,?,0,NULL)",
            (self.SLOT, self.SLOT, "electricity_main", 2.0,
             (rate / 100.0) if rate is not None else None,
             round(2.0 * rate / 100.0, 6) if rate is not None else None))
        st.upsert_dispatch_slot(self.SLOT, off_peak=True, provider="Myenergi",
                                source="smart-charge", state="completed")
        for kind, e in (("planned", -1.8), ("started", None), ("completed", -1.8)):
            st.record_dispatch_history(self.SLOT, kind, provider="Myenergi", energy_kwh=e)
        st._conn.commit()
        return st

    async def _run(self, st, off):
        engine._store = st
        engine._RECONCILE_SETTLE_HOURS = 0.0
        engine._kraken_rate_schedules = {"import": _Sched(off)}
        try:
            return await engine.reconcile_dispatch_overlay()
        finally:
            engine._store = None

    def _rate(self, st):
        return st._conn.execute("SELECT imp_rate FROM blocks WHERE block_start=?",
                                (self.SLOT,)).fetchone()[0]

    async def test_the_band_is_the_dispatchs_and_the_price_the_schedules(self):
        """Engine step 4c: settle() never reads a band from the stored price, so #28 cannot recur;
        the started smart charge is off-peak and priced at the schedule's off-peak for its date.
        (The schedule is restated per date since 4.5.17 — `RateSchedule.with_vat` — so a real one
        reads September's 5% figure; this stub's 0% price stands in for whatever it says.) REWRITTEN:
        it asserted the stored 5.493p was kept."""
        st = self._store(OFF_STORED)
        await self._run(st, OFF_NOW)
        self.assertAlmostEqual(self._rate(st), OFF_NOW / 100.0, places=6)
        self.assertEqual({x["band"] for x in st.get_block_segments(self.SLOT, "electricity_main")},
                         {"off_peak"})

    async def test_a_peak_slot_that_charged_is_still_restored(self):
        """GUARD: a started smart charge priced at peak is a real band disagreement."""
        st = self._store(PEAK)
        res = await self._run(st, OFF_NOW)
        self.assertEqual(res["settled"], 1)
        self.assertAlmostEqual(self._rate(st), OFF_NOW / 100.0, places=6)

    async def test_an_unpriced_slot_is_not_already_off_peak(self):
        """GUARD: rate 0 is nearer off-peak than peak, but it is not priced."""
        st = self._store(None)
        res = await self._run(st, OFF_NOW)
        self.assertEqual(res["settled"], 1)
        self.assertAlmostEqual(self._rate(st), OFF_NOW / 100.0, places=6)


if __name__ == "__main__":
    unittest.main()
