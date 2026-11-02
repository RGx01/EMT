"""
v4 4.5.21 forward fix, ported — a manual rate correction carries the corrected ex-VAT rate onto the
devices.

On an API account (where v4 found it) the corrections route updated only the MAIN's settled row
with its full rewrite (rate, cost, and the ex-VAT rescale of BL-57); devices are carried along afterwards
(#254b), and that step set only their inc rate and cost. Their `imp_rate_exc` / `imp_cost_exc`
kept the pre-correction band, so a battery or EV on a half-hour corrected to off-peak still read
peak in every ex-VAT view (prod-dev, 7 Oct 2026: 21 Jul 05:30 and 16:00).

Drives the real route. Discriminating: test_the_devices_take_the_corrected_exc_rate and
test_reapplying_repairs_a_stale_device fail on the parent. v4's heal for blocks corrected before the
fix (`corrected_device_exc_done`) is not ported — the 4.5.21 import floor covers it. Since BL-75 (e) (29 Oct 2026) the route is CAD-only and no
longer narrows to settled rows; the devices are still carried, so these run on a CAD account.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import test_server as TS                       # noqa: E402  (stubs + make_client)

BS = "2026-07-21T04:30:00"
PEAK, PEAK_EXC = 0.323092, 0.307707
OFF = 0.0549297
OFF_EXC = round(OFF / 1.05, 6)


def _store(device_exc=PEAK_EXC, device_rate=PEAK):
    st = TS.BlockStore(TS._memory_uri())   # shared-cache: v5 routes read on their own connection
    st.insert_config_period({"meters": {
        "electricity_main": {"meta": {"billing_day": 1, "block_minutes": 30, "timezone": "UTC",
                                      "currency_symbol": "£", "currency_code": "GBP"}},
        "house_battery": {"meta": {"sub_meter": True, "parent_meter": "electricity_main",
                                   "meter_type": "battery"}},
    }})
    cp = st.get_current_config_period_id()
    with st._conn:
        st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, interpolated, "
            "imp_kwh, imp_kwh_api, imp_rate, imp_cost, imp_cost_exc, exp_kwh, exp_cost) "
            "VALUES (?, '2026-07-21T05:00:00', 'electricity_main', ?, 0, 0.142, 0.142, ?, ?, ?, 0, 0)",
            (BS, cp, PEAK, round(0.142 * PEAK, 6), round(0.142 * PEAK_EXC, 6)))
        st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, interpolated, "
            "imp_kwh, imp_kwh_grid, imp_rate, imp_cost, imp_cost_exc, exp_kwh, exp_cost) "
            "VALUES (?, '2026-07-21T05:00:00', 'house_battery', ?, 0, 0.002, 0.002, ?, ?, ?, 0, 0)",
            (BS, cp, device_rate, round(0.002 * device_rate, 6),
             round(0.002 * device_exc, 6) if device_exc is not None else None))
    return st


class ACorrection(unittest.TestCase):

    def _correct(self, st):
        client = TS.make_client(store=st)
        # a CAD account: corrections are refused with the supplier API (BL-75 (e))
        r = client.post("/api/corrections/apply", json={
            "type": "rate", "channel": "import", "value": OFF, "recalc_cost": True,
            "from_date": "2026-07-21", "to_date": "2026-07-21",
            "from_time": "04:30", "to_time": "05:00", "meter_id": "all",
            "confirm_multiband": True})
        self.assertEqual(r.status_code, 200, r.data)

    def _row(self, st, meter):
        # imp_rate_exc was dropped (step 5b, 10 Oct 2026): the ex-VAT rate is derived from the
        # row's own pair, imp_rate × imp_cost_exc / imp_cost.
        r = dict(st._conn.execute(
            "SELECT imp_rate, imp_cost, imp_cost_exc FROM blocks "
            "WHERE block_start=? AND meter_id=?", (BS, meter)).fetchone())
        r["rate_exc"] = (r["imp_rate"] * r["imp_cost_exc"] / r["imp_cost"]
                         if r["imp_cost_exc"] is not None and r["imp_cost"] else None)
        return r

    def test_the_main_is_corrected(self):
        """GUARD: the main's own rewrite (BL-57) was already right."""
        st = _store()
        self._correct(st)
        m = self._row(st, "electricity_main")
        self.assertAlmostEqual(m["imp_rate"], OFF, places=6)
        self.assertAlmostEqual(m["imp_cost_exc"], 0.142 * OFF_EXC, places=6)

    def test_the_devices_take_the_corrected_exc_rate(self):
        st = _store()
        self._correct(st)
        d, m = self._row(st, "house_battery"), self._row(st, "electricity_main")
        self.assertAlmostEqual(d["imp_rate"], OFF, places=6)
        self.assertAlmostEqual(d["imp_cost_exc"], round(0.002 * m["rate_exc"], 6), places=6)

    def test_reapplying_repairs_a_stale_device(self):
        """A device an earlier correction left with the old exc rate: its inc rate is already the
        corrected one, so only taking the MAIN's exc (not rescaling its own) repairs it."""
        st = _store(device_rate=OFF, device_exc=PEAK_EXC)
        self._correct(st)
        d, m = self._row(st, "house_battery"), self._row(st, "electricity_main")
        self.assertAlmostEqual(d["imp_cost_exc"], round(0.002 * m["rate_exc"], 6), places=6)


if __name__ == "__main__":
    unittest.main()
