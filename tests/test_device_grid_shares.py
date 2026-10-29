"""
BL-76: the device grid split proportional after the EV (battery sensor and device split design §2;
the user, 29 Oct 2026).

After the EV's grid was reserved, the other devices took what was left largest first, so the smaller
of two devices short of grid absorbed the whole shortfall (on a user's history 3,475 of 37,283
half-hours contested, 579 giving the heat pump no grid at all). Now every other device shares what is
left in proportion to its reading, in whole watt-hours, by one rule (`settle.device_grid_shares`)
that PASS 2 and settle() both call. The history is re-split by the settle sweep (version 2).

Discriminating: every test here fails on the parent (the rule is new; PASS 2 and settle() split greedily there).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_engine  # noqa: F401,E402  — installs stubs, imports engine
import engine                                    # noqa: E402
import settle                                    # noqa: E402

shares = settle.device_grid_shares


class TheRule(unittest.TestCase):

    def test_two_devices_short_of_grid_share_in_proportion(self):
        """A battery reading 3 kWh and a heat pump 1 kWh on 2 kWh of grid: 1.5 and 0.5, not 2 and 0."""
        self.assertEqual(shares(2.0, [(3.0, False), (1.0, False)]), [1.5, 0.5])

    def test_the_order_does_not_matter(self):
        self.assertEqual(shares(2.0, [(1.0, False), (3.0, False)]), [0.5, 1.5])

    def test_the_ev_claims_first_and_the_rest_share(self):
        self.assertEqual(shares(3.0, [(1.0, False), (2.0, True), (3.0, False)], has_ev_meter=True),
                         [0.25, 2.0, 0.75])

    def test_with_no_ev_meter_the_dispatch_ev_is_reserved(self):
        self.assertEqual(shares(3.0, [(3.0, False), (1.0, False)], ev_kwh=2.0), [0.75, 0.25])

    def test_whole_watt_hours_adding_up_to_the_grid(self):
        got = shares(1.0, [(1.0, False), (1.0, False), (1.0, False)])
        self.assertEqual(sorted(got), [0.333, 0.333, 0.334])
        self.assertAlmostEqual(sum(got), 1.0, places=9)

    def test_enough_grid_each_takes_its_reading(self):
        self.assertEqual(shares(5.0, [(3.0, False), (1.0, False)]), [3.0, 1.0])

    def test_an_unsettled_gap_block_records_a_device_as_read(self):
        self.assertEqual(shares(0.157, [(2.053, False)], clamp=False), [2.053])

    def test_never_past_a_reading(self):
        for got, kwh in zip(shares(0.5, [(0.001, False), (9.0, False)]), (0.001, 9.0)):
            self.assertLessEqual(got, kwh)


def _block(grid, devices, settled=True):
    meters = {"electricity_main": {"meta": {}, "imp_kwh_api": grid if settled else None,
                                   "channels": {"import": {"kwh": grid, "rate": 0.25, "cost": grid * 0.25}}}}
    for mid, kwh, mtype in devices:
        meters[mid] = {"meta": {"sub_meter": True, "parent_meter": "electricity_main", "meter_type": mtype},
                       "channels": {"import": {"kwh": kwh}}}
    return {"start": "2026-07-01T12:00:00", "meters": meters, "interpolated": False}


class BothCallers(unittest.TestCase):

    def test_pass2_shares_in_proportion(self):
        b = _block(2.0, [("house_battery", 3.0, "battery"), ("heat_pump", 1.0, "heat_pump")])
        engine._apply_pass2(b)
        got = {m: b["meters"][m]["channels"]["import"]["kwh_grid"] for m in ("house_battery", "heat_pump")}
        self.assertEqual(got, {"house_battery": 1.5, "heat_pump": 0.5})

    def test_settle_gives_the_same_shares(self):
        f = settle.Facts(start="2026-07-01T12:00:00", end="2026-07-01T12:30:00", kwh=2.0, kind="published",
                         vat=0.05, devices=[settle.Device("house_battery", 3.0, False),
                                            settle.Device("heat_pump", 1.0, False)], clamp=True)
        f.clock_rate = 0.25
        got = {d.meter_id: d.kwh_grid for d in settle.settle(f).devices}
        self.assertEqual(got, {"house_battery": 1.5, "heat_pump": 0.5})

    def test_pass2_stores_no_kwh_battery(self):
        b = _block(2.0, [("house_battery", 3.0, "battery")])
        engine._apply_pass2(b)
        self.assertNotIn("kwh_battery", b["meters"]["house_battery"]["channels"]["import"])

    def test_the_history_is_re_split(self):
        self.assertGreaterEqual(engine._SETTLE_HISTORY_VERSION, 2)


if __name__ == "__main__":
    unittest.main()
