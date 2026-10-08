"""
Step 5 — the ex-VAT readers take the stored ex-VAT COST and derive the rest (8 Oct 2026).

`imp_cost_exc` is the per-block ex-VAT authority, as `imp_cost` is the inc one (import spec §5.2,
v14): checked against two Octopus statements it is the figure EMT has always shown, and the
segments' kWh x exc_rate was no closer. The other three ex-VAT columns are derived where read:
the standing charge's ex-VAT is inc ÷ (1 + the day's VAT) (equal to the stored column to 4e-7 on
every real database), a block's ex-VAT ratio is cost_exc ÷ cost, and exc_source has no reader.

Discriminating: both fail on the parent, which read a stale standing_charge_exc and a stale
imp_rate_exc.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import energy_charts as ec                       # noqa: E402


def _blk(start="2026-08-01T00:00:00"):
    # 2 kWh at 21p inc: cost £0.42, ex-VAT £0.40. The stored rate and standing ex-VAT are stale.
    imp = {"kwh": 2.0, "cost": 0.42, "rate": 0.21, "cost_exc": 0.40, "rate_exc": 0.15}
    return {"start": start, "meters": {"electricity_main": {
        "meta": {}, "standing_charge": 0.42, "standing_charge_exc": 0.30,
        "channels": {"import": imp}}}}


class TheBillMethod(unittest.TestCase):

    def test_the_standing_charge_ex_vat_is_derived(self):
        bm = ec._bill_method_breakdown([_blk()])
        self.assertAlmostEqual(bm["standing_exc"], 0.40, places=4)    # 0.42 ÷ 1.05, not 0.30

    def test_the_energy_is_the_stored_cost(self):
        """GUARD: the energy line was already the stored ex-VAT cost."""
        self.assertAlmostEqual(ec._bill_method_breakdown([_blk()])["energy_exc"], 0.40, places=4)


class TheDataTable(unittest.TestCase):

    def test_the_ex_vat_ratio_is_cost_exc_over_cost(self):
        d = ec.day_chart_data("2026-07-26", [(0, _blk())], {"electricity_main": "#1f77b4"},
                              cap_from=None, import_schedule=None, timezone_name="UTC",
                              block_minutes=30, currency="£", bill_rounding=True)
        self.assertAlmostEqual(d["chart"]["exc_ratio"][0], round(0.40 / 0.42, 8), places=8)


if __name__ == "__main__":
    unittest.main()
