"""4.5.6 — reconcile must not REVERT a SETTLED, completed-dispatch block to peak (until engine step 4c).

Models the 2026-07-21 16:00 BST case: a genuine off-peak dispatched bump (planned +
completed, small energy, EMT never captured 'started') that the pre-4.5.6 overlay
wrongly reverted to peak. Octopus billed it off-peak; the guard left a settled block to the bill.
Engine step 4c (26 Oct 2026) settles the reconcile's candidates with settle(), whose prediction is
the 2b–2c rule with no guard: billed wins, so the bill moves it back when it comes. The first test
is reversed; a planned-only slot and an unsettled tiny charge revert as before.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine
from block_store import BlockStore

PEAK_INC = 32.3092   # pence
OFF_INC = 5.493      # pence


class _Sched:
    def is_empty(self):
        return False

    def off_peak_rate_near(self, ts):
        return OFF_INC

    def day_rate_bounds(self, ts):
        return (OFF_INC, PEAK_INC)

    def resolve(self, ts):
        return PEAK_INC


class TestSettledGuard(unittest.IsolatedAsyncioTestCase):
    SLOT = "2026-07-21T15:00:00"   # 16:00 BST — daytime, off-peak dispatched bump

    def setUp(self):
        from unittest import mock
        # engine step 4c: the reconcile settles through settle(), which asks the tariff kind
        _tk = mock.patch.object(engine, "tariff_kind_at", lambda bs: "intelligent")
        _tk.start()
        self.addCleanup(_tk.stop)

    def _seed(self, st, *, settled, completed=True):
        st._conn.execute(
            "INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, "
            "block_minutes, timezone) VALUES (1, '2020-01-01T00:00:00', 1, 30, 'UTC')")
        st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
            "imp_kwh, imp_rate, imp_cost, rate_corrected, imp_kwh_api) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (self.SLOT, self.SLOT, "electricity_main", 1, 0.203, OFF_INC / 100.0,
             round(0.203 * OFF_INC / 100.0, 6), 0, (1.0 if settled else None)))
        st.upsert_dispatch_slot(self.SLOT, off_peak=True, provider="Myenergi",
                                source="smart-charge",
                                state=("completed" if completed else "planned"))
        st.record_dispatch_history(self.SLOT, "planned", provider="Myenergi",
                                   energy_kwh=-0.34)
        if completed:
            st.record_dispatch_history(self.SLOT, "completed", provider="Myenergi",
                                       energy_kwh=-0.09)   # small → negligible → revert
        st._conn.commit()

    async def _run(self, st):
        engine._store = st
        engine._RECONCILE_SETTLE_HOURS = 0.0
        engine._kraken_rate_schedules = {"import": _Sched()}
        try:
            return await engine.reconcile_dispatch_overlay()
        finally:
            engine._store = None

    def _rate(self, st):
        return st._conn.execute(
            "SELECT imp_rate FROM blocks WHERE block_start=?", (self.SLOT,)).fetchone()[0]

    async def test_a_settled_completed_dispatch_is_predicted_peak(self):
        """REVERSED by engine step 4c: the guard held a DCC-settled completed-dispatch block at its
        price for the bill to decide. The prediction is the 2b–2c rule — a completed charge with no
        `started`, under 0.4 kWh, is peak by EMT's rules (16 Aug 2026 13:00 BST) — and billed wins
        when the bill comes (21 Jul 2026 16:00 BST was billed off-peak: its bill settles it)."""
        st = BlockStore(":memory:")
        self._seed(st, settled=True, completed=True)
        res = await self._run(st)
        self.assertEqual(res["settled"], 1)
        self.assertAlmostEqual(self._rate(st), PEAK_INC / 100.0, places=5)

    async def test_unsettled_completed_dispatch_still_reverts(self):
        st = BlockStore(":memory:")
        self._seed(st, settled=False, completed=True)
        res = await self._run(st)
        self.assertEqual(res["settled"], 1)                         # pre-settlement: allowed
        self.assertAlmostEqual(self._rate(st), PEAK_INC / 100.0, places=5)

    async def test_settled_planned_only_is_still_reverted(self):
        # Never completed = never charged → peak revert is correct, NOT guarded.
        st = BlockStore(":memory:")
        self._seed(st, settled=True, completed=False)
        res = await self._run(st)
        self.assertEqual(res["settled"], 1)
        self.assertAlmostEqual(self._rate(st), PEAK_INC / 100.0, places=5)


if __name__ == "__main__":
    unittest.main()
