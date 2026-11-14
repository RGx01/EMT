"""
The Billing day chart's EV rate line follows the EV curve for the configured EV meter, whatever
its id (14 Nov 2026, the user: prod and staging, both b19, drew the same reconciled day
differently). day_chart_data chose the curve by the meter's id — `ev_dispatch`, `ev_…`, or one
holding "charger" — so v4's imported `ev_charger` got the EV curve, and a meter the setup wizard
made (`sub_meter_<n>`, meter_type "ev") got the HOUSE curve: on a capped day its line sat on
the house's peak over every idle half-hour instead of the off-peak baseline.

The caller names the EV meter (`ev_fold_meter`, billing_view's ev_phys_id, found by meter_type);
the line now follows the EV curve for it.

Discriminating: test_a_wizard_ev_meter_idles_off_peak fails on the parent. The `ev_charger` test
is a GUARD (the id the old test already matched).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import energy_charts as ec

MAIN = "electricity_main"
OFF, DAY = 0.0523, 0.3077
DAYSTR = "2026-11-10"
IDLE, CHARGE = 20, 46                      # 10:00 (car idle, house on the day rate), 23:00


def _block(hh, ev_id, ev_kwh, house_kwh, house_rate):
    segs = [{"kwh": house_kwh, "inc_rate": house_rate, "exc_rate": None,
             "band": "off_peak" if house_rate == OFF else "day", "attribution": "house"}]
    if ev_kwh:
        segs.append({"kwh": ev_kwh, "inc_rate": OFF, "exc_rate": None,
                     "band": "off_peak", "attribution": "ev"})
    kwh = house_kwh + ev_kwh
    cost = house_kwh * house_rate + ev_kwh * OFF
    start = "%sT%02d:%02d:00" % (DAYSTR, *divmod(hh * 30, 60))
    return (hh, {"start": start, "meters": {
        MAIN: {"meta": {}, "standing_charge": 0.0,
               "channels": {"import": {"kwh": kwh, "cost": round(cost, 6),
                                       "rate": round(cost / kwh, 6), "segments": segs}}},
        ev_id: {"meta": {"sub_meter": True, "meter_type": "ev"},
                "channels": {"import": {"kwh": ev_kwh, "kwh_grid": ev_kwh,
                                        "cost": round(ev_kwh * OFF, 6), "rate": OFF}}}}})


class TheEvLineFollowsTheConfiguredEv(unittest.TestCase):

    def _ev_line(self, ev_id):
        blocks = [_block(IDLE, ev_id, 0.0, 0.4, DAY), _block(CHARGE, ev_id, 3.0, 0.2, OFF)]
        d = ec.day_chart_data(DAYSTR, blocks, {MAIN: "#1f77b4", ev_id: "#bcbd22"},
                              cap_from=[[DAYSTR, None]], import_schedule=None,
                              timezone_name="UTC", block_minutes=30, currency="£",
                              ev_fold_meter=ev_id, ev_label="EV")
        m = d["chart"]["meters"]
        return m[ev_id]["rate"], m[MAIN]["rate"]

    def test_a_wizard_ev_meter_idles_off_peak(self):
        ev, house = self._ev_line("sub_meter_1700000000000")
        self.assertAlmostEqual(house[IDLE], DAY, places=4)      # the house is on the day rate
        self.assertAlmostEqual(ev[IDLE], OFF, places=4)         # the idle car is not
        self.assertAlmostEqual(ev[CHARGE], OFF, places=4)

    def test_an_ev_charger_meter_idles_off_peak(self):
        """GUARD: the id the name test already knew."""
        ev, house = self._ev_line("ev_charger")
        self.assertAlmostEqual(house[IDLE], DAY, places=4)
        self.assertAlmostEqual(ev[IDLE], OFF, places=4)


if __name__ == "__main__":
    unittest.main()
