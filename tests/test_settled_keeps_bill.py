"""
A settled block re-confirmed by its kWh is kWh × its band's rate (engine step 4b, 25 Oct 2026).

BL-82 (13 Oct 2026) kept a settled block's cost as Octopus's bill when the settlement re-run fired on a
DCC re-confirmation: on 12 Oct's backups the re-run had replaced 262 and 247 bills with kWh × rate.
Engine step 2b (23 Oct 2026) then made the cost kWh × the band's rate on every tariff, never Octopus's
cost copied (on staging, all 5,119 bill-settled half-hours had Octopus's cost within a thousandth of a
penny of it), and the bill apply has written that since. The re-run now settles the half-hour with
settle(): the same rule, so a bill's own rounding (BILL below is kWh × rate + £0.0004) no longer
survives. REVERSED: the first three asserted the bill was kept; they fail on the parent now, and
the last two are guards.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import test_rebuild_meter_type as H              # noqa: E402  (stubs, real engine, flat tariff)
engine = H.engine

BILL = round(H.MAIN * H.FLAT, 6) + 0.0004       # Octopus's per-bucket rounding: not kWh x rate
BILL_EXC = round(BILL / 1.05, 6)


class ASettledBlock(H._Harness):

    def _settled(self, rate_source="settled"):
        self._finalise()
        with engine._store._conn as c:
            c.execute("UPDATE blocks SET rate_source=?, imp_cost=?, imp_cost_exc=?, "
                      "imp_kwh_api=? WHERE block_start=? AND meter_id='electricity_main'",
                      (rate_source, BILL, BILL_EXC, H.MAIN, H.START))

    def _main(self):
        return engine._store._conn.execute(
            "SELECT imp_kwh, imp_cost, imp_cost_exc, rate_source FROM blocks WHERE block_start=? "
            "AND meter_id='electricity_main'", (H.START,)).fetchone()

    def test_a_re_confirmed_kwh_is_kwh_times_the_rate(self):
        self._settled()
        self._settle(dcc=H.MAIN)
        self.assertAlmostEqual(self._main()["imp_cost"], round(H.MAIN * H.FLAT, 6), places=6)

    def test_and_its_ex_vat_through_the_calendar(self):
        self._settled()
        self._settle(dcc=H.MAIN)
        self.assertAlmostEqual(self._main()["imp_cost_exc"],
                               round(H.MAIN * round(H.FLAT / 1.05, 6), 6), places=6)

    def test_a_settled_kwh_that_differs_is_re_costed(self):
        self._settled()
        self._settle(dcc=H.MAIN + 0.5)
        r = self._main()
        self.assertAlmostEqual(r["imp_kwh"], H.MAIN + 0.5, places=6)
        self.assertAlmostEqual(r["imp_cost"], round((H.MAIN + 0.5) * H.FLAT, 6), places=6)

    def test_a_reconciled_rate_is_still_re_costed(self):
        """GUARD: no bill — kWh x the held rate is its best cost."""
        self._settled(rate_source="reconciled")
        self._settle(dcc=H.MAIN + 0.5)
        self.assertAlmostEqual(self._main()["imp_cost"], round((H.MAIN + 0.5) * H.FLAT, 6),
                               places=6)

    def test_a_corrected_rate_is_still_re_costed(self):
        """GUARD: the user's rate, re-costed to the settled kWh."""
        self._settled(rate_source="corrected")
        self._settle(dcc=H.MAIN + 0.5)
        self.assertAlmostEqual(self._main()["imp_cost"], round((H.MAIN + 0.5) * H.FLAT, 6),
                               places=6)


if __name__ == "__main__":
    unittest.main()
