"""
BL-82 (13 Oct 2026): a settled block keeps Octopus's bill when its kWh is re-confirmed.

The settlement re-run held a settled block's rate and re-costed it to kWh x rate, replacing the bill
on every DCC re-confirmation — on 12 Oct's backups 262 and 247 settled blocks no longer carried
their bill, every one re-costed, none with a different kWh. kWh x rate is the right cost BEFORE the
bill (and for corrected / reconciled rates); after it, the bill is the cost.

Discriminating: the first three fail on the parent; the last two are guards.
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

    def test_a_re_confirmed_kwh_keeps_the_bill(self):
        self._settled()
        self._settle(dcc=H.MAIN)
        self.assertAlmostEqual(self._main()["imp_cost"], BILL, places=6,
                               msg="the bill was re-costed to kWh x rate")

    def test_and_its_ex_vat(self):
        self._settled()
        self._settle(dcc=H.MAIN)
        self.assertAlmostEqual(self._main()["imp_cost_exc"], BILL_EXC, places=6)

    def test_a_settled_kwh_that_differs_keeps_the_bill_and_says_so(self):
        self._settled()
        with self.assertLogs("engine", level="WARNING") as logs:
            self._settle(dcc=H.MAIN + 0.5)
        r = self._main()
        self.assertAlmostEqual(r["imp_cost"], BILL, places=6)
        self.assertAlmostEqual(r["imp_kwh"], H.MAIN + 0.5, places=6)    # the kWh does settle
        self.assertTrue(any("bill kept" in m for m in logs.output), logs.output)

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
