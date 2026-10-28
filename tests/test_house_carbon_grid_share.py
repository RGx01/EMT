"""
The house's carbon on its devices' grid shares (28 Oct 2026, the user).

The house was the main's import carbon less the devices' GROSS carbon (their own reads × intensity).
Where a device drew from the battery or solar, its own read exceeds its grid share, so the house went
below zero — on staging in 340 half-hours, 10.85 kg; on 14 Oct 2026 05:30 BST (an inverter firmware
update left the battery's sensor counting), −814 g against 0 kWh. Now:

- Usage Stats / Charts: a device's carbon bar is its stored carbon scaled to its grid share — the kWh
  its bar shows and is priced on — and the house subtracts that, so house + devices is the grid's
  import carbon;
- Insights: the house's kWh and carbon are each half-hour's main less its devices' grid shares,
  floored at zero (direct_import's rule, as the bill's Direct import) — they were the period's main
  less the devices' own reads, clamped. Each device's card keeps its gross carbon.

Stored carbon is unchanged (gross, as decided in the engine step 5 design §1.3).

Discriminating: every test here fails on the parent, but those labelled GUARD.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import test_direct_import as DI                 # noqa: E402  (fixtures, the bill card's reader)
import usage_view as UV                         # noqa: E402

BS = "2026-10-14T04:30:00"


def _row(meter, kwh, grid, carbon, sub=True, exp=0.0):
    # (block_start, meter_id, is_sub_meter, billing_day, imp_kwh, imp_kwh_grid, imp_rate, imp_cost,
    #  exp_kwh, exp_cost, standing_charge, carbon_g)
    return (BS, meter, sub, 1, kwh, grid, 0.05, round(kwh * 0.05, 6), exp, 0.0, 0.0, carbon)


def _usage(rows):
    agg = UV._aggregate_tuples(rows, lambda bs: "b")
    return UV._bucket_to_row_values(agg["b"])["meters"]


class UsageStats(unittest.TestCase):

    def test_14_oct_the_house_does_not_go_below_zero(self):
        """0.809 kWh from the grid at 162 g/kWh; the car read 3.03 (grid 0.809), the battery 2.807
        (grid 0). The house was 131.1 − 490.9 − 454.7 = −814.5 g."""
        m = _usage([_row("electricity_main", 0.809, None, round(0.807 * 162, 4), sub=False, exp=0.002),
                    _row("ev_charger", 3.03, 0.809, round(3.03 * 162, 4)),
                    _row("house_battery", 2.807, 0.0, round(2.807 * 162, 4))])
        self.assertAlmostEqual(m["electricity_main"]["carbon_g"], 0.0, places=3)
        self.assertAlmostEqual(m["ev_charger"]["carbon_g"], round(0.809 * 162, 4), places=3)
        self.assertIsNone(m["house_battery"]["carbon_g"])            # no grid share, no grid carbon

    def test_house_and_devices_are_the_grids_import_carbon(self):
        """A battery charged 3 kWh, 1 from the grid, while the house drew 1."""
        m = _usage([_row("electricity_main", 2.0, None, 200.0, sub=False),
                    _row("house_battery", 3.0, 1.0, 300.0)])
        self.assertAlmostEqual(m["electricity_main"]["carbon_g"], 100.0, places=3)
        self.assertAlmostEqual(m["house_battery"]["carbon_g"], 100.0, places=3)
        self.assertAlmostEqual(m["electricity_main"]["carbon_g"] + m["house_battery"]["carbon_g"],
                               m["electricity_main"]["carbon_g_imp"], places=3)

    def test_GUARD_a_device_wholly_on_the_grid_is_as_before(self):
        m = _usage([_row("electricity_main", 2.0, None, 200.0, sub=False),
                    _row("heat_pump", 1.5, 1.5, 150.0)])
        self.assertAlmostEqual(m["heat_pump"]["carbon_g"], 150.0, places=3)
        self.assertAlmostEqual(m["electricity_main"]["carbon_g"], 50.0, places=3)


class Insights(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import tariff_goldens as G
        cls.answers = {}
        for tariff, devices in G.fixtures():
            fx = DI._Fixture(tariff, devices)
            try:
                for label, query, days in DI.WINDOWS:
                    lo, hi = fx._utc(days)
                    cls.answers[(G.name(tariff, devices), label)] = (
                        fx.bill(query), fx.srv._aggregate_insights(fx.store, fx.cfg, lo, hi))
            finally:
                fx.close()

    def test_the_house_is_the_bills_direct_import(self):
        """On e7_over and e7_gap, where devices out-read the main, it read 1.82 kWh against 119.84."""
        off = [(key, direct[0], ins["house_imp_kwh"])
               for key, ((direct, _devs), ins) in self.answers.items()
               if abs(ins["house_imp_kwh"] - direct[0]) > 0.005]
        self.assertEqual(off, [])

    def test_GUARD_a_devices_card_keeps_its_gross_figures(self):
        leaked = [(key, mid) for key, (_bill, ins) in self.answers.items()
                  for mid, st in (ins.get("sub_meters") or {}).items() if "grid_kwh" in st]
        self.assertEqual(leaked, [])


if __name__ == "__main__":
    unittest.main()
