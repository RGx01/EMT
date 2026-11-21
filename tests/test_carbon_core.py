"""
Phase-2 Δ5: the pure carbon axis (carbon.py) — kWh × grid CO2 intensity, price never enters.
Locks the 4.3.2 draw-gap credit methodology re-sourced from synthetic dispatch:
  - unclipped > clipped  → a saving (behind-meter energy avoids grid carbon)
  - unclipped == clipped → zero saving, gross == grid (byte-identical to a no-credit block)
  - clamps guard against a stale/over-clipped billing figure (never a negative saving)
  - the identity gross_g == grid_g + saving_g holds
  - the pass over a reprice result reads ONLY emitted energies (no rate/cost).
"""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import carbon

I = 140.0  # gCO2/kWh, a plausible grid intensity


class TestEvCarbon(unittest.TestCase):
    def test_credit_on_draw_gap(self):
        # car drew 2.31, grid supplied 2.18 → 0.13 kWh behind the meter (the 08-19 04:30 case)
        r = carbon.ev_carbon(unclipped_kwh=2.31, clipped_kwh=2.18, intensity_g=I)
        self.assertAlmostEqual(r["behind_meter_kwh"], 0.13, places=6)
        self.assertAlmostEqual(r["saving_g"], round(0.13 * I, 4), places=4)
        self.assertAlmostEqual(r["grid_g"], round(2.18 * I, 4), places=4)
        # gross == grid + saving (the 4.3.2 sub-meter gross figure)
        self.assertAlmostEqual(r["gross_g"], round(r["grid_g"] + r["saving_g"], 4), places=4)

    def test_no_gap_zero_saving(self):
        # all grid-supplied → no credit, gross == grid (indistinguishable from a no-EV-credit block)
        r = carbon.ev_carbon(unclipped_kwh=3.0, clipped_kwh=3.0, intensity_g=I)
        self.assertEqual(r["saving_g"], 0.0)
        self.assertEqual(r["gross_g"], r["grid_g"])
        self.assertEqual(r["behind_meter_kwh"], 0.0)

    def test_clipped_clamped_never_negative_saving(self):
        # a stale billing figure that exceeds the draw must not manufacture a negative saving
        r = carbon.ev_carbon(unclipped_kwh=1.0, clipped_kwh=1.4, intensity_g=I)
        self.assertEqual(r["clipped_kwh"], 1.0)
        self.assertEqual(r["saving_g"], 0.0)

    def test_no_ev(self):
        r = carbon.ev_carbon(unclipped_kwh=0.0, clipped_kwh=0.0, intensity_g=I)
        self.assertEqual((r["grid_g"], r["saving_g"], r["gross_g"]), (0.0, 0.0, 0.0))

    def test_zero_intensity(self):
        r = carbon.ev_carbon(unclipped_kwh=5.0, clipped_kwh=2.0, intensity_g=0.0)
        self.assertEqual((r["grid_g"], r["saving_g"], r["gross_g"]), (0.0, 0.0, 0.0))


class TestSlotIntensity(unittest.TestCase):
    def test_prefers_stored(self):
        # stored value wins, even for a zero-net block; rounded to 1 dp, abs
        self.assertEqual(carbon.slot_intensity(280.0, -140.04, 0.0), 140.0)

    def test_fallback_from_carbon_over_net(self):
        self.assertEqual(carbon.slot_intensity(300.0, None, 3.0), 100.0)
        # export (net negative) → abs
        self.assertEqual(carbon.slot_intensity(-450.0, None, -3.0), 150.0)

    def test_zero_net_no_stored_is_blank(self):
        self.assertIsNone(carbon.slot_intensity(0.0, None, 0.0))

    def test_guards(self):
        self.assertIsNone(carbon.slot_intensity(None, None, 2.0))


if __name__ == "__main__":
    unittest.main()


class TestPeriodEvSaving(unittest.TestCase):
    def test_aggregates_and_single_sources(self):
        rows = [
            ("s1", 2.18, I),   # dispatch present, unclipped 2.31 → 0.13 saving
            ("s2", 3.0,  I),   # dispatch present, unclipped 3.0  → no saving
            ("s3", 1.0,  I),   # NO dispatch: Octopus's split alone → the car's grid kWh, no saving
            ("s4", 0.0,  I),   # zero clipped → skipped
        ]
        unclipped = {"s1": 2.31, "s2": 3.0}
        out = carbon.period_ev_saving(rows, unclipped)
        self.assertEqual(out["credit_blocks"], 1)
        self.assertAlmostEqual(out["saving_g"], round(0.13 * I, 4), places=4)
        # s3 counted (21 Nov 2026; it was skipped and stayed in Insights' house): grid only
        self.assertAlmostEqual(out["clipped_kwh"], round(2.18 + 3.0 + 1.0, 6), places=6)
        self.assertAlmostEqual(out["grid_g"], round((2.18 + 3.0 + 1.0) * I, 4), places=3)

    def test_non_iog_zero(self):
        # non-IOG: no EV split rows at all → zero everything → caller renders nothing
        out = carbon.period_ev_saving([], {})
        self.assertEqual(out["clipped_kwh"], 0.0)
        self.assertEqual(out["credit_blocks"], 0)

    def test_a_split_without_a_dispatch_saves_nothing(self):
        out = carbon.period_ev_saving([("s1", 2.0, I)], {})
        self.assertEqual(out["saving_g"], 0.0)
        self.assertEqual(out["credit_blocks"], 0)
        self.assertAlmostEqual(out["clipped_kwh"], 2.0, places=6)
