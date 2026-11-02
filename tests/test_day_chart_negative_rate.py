"""Agile plunge-price: a NEGATIVE import rate must render below the axis on the
day chart, not clip flat at zero.

Two halves:
  1. day_chart_data carries the negative rate into the rate series (server side —
     the rate is never floored to 0).
  2. generate_daily_import_export_charts' client-side _alignY2 gives the rate axis
     room below zero on an import-only day (previously hard-coded y2min = 0, which
     clipped the plunge-price rate line flat at the baseline even though the cost
     already showed the credit).
"""
import unittest
import energy_charts as ec


class TestDayChartNegativeRateLine(unittest.TestCase):

    def _blk(self, kwh, cost, rate):
        return {"meters": {"electricity_main": {"meta": {}, "standing_charge": 0.5,
                "channels": {"import": {"kwh": kwh, "cost": cost, "rate": rate}}}}}

    def _html(self, day_blocks):
        # The day's chart JSON as text (C, 30 Sep 2026: this read build_day_chart_html's markup; the page it made is gone, and day_chart_data is what the Billing tab is served.)
        import json
        return json.dumps(ec.day_chart_data("2026-07-26", day_blocks, {"electricity_main": "#1f77b4"},
                                            cap_from=None, import_schedule=None, timezone_name="UTC",
                                            block_minutes=30, currency="£")["chart"])

    def test_negative_rate_reaches_the_rate_trace(self):
        # A plunge slot (rate -0.05, a credit) alongside a normal slot: the rate trace
        # must carry -0.05, not a clamped 0.
        db = [(12, self._blk(1.0, -0.05, -0.05)),
              (20, self._blk(2.0, 0.42, 0.21))]
        html = self._html(db)
        self.assertIn("-0.05", html)

    def test_alignY2_drops_rate_axis_below_zero_for_negative_rate(self):
        # The axis alignment must handle a negative rate axis on an import-only day,
        # not the old unconditional y2min = 0 clamp. It was the pre-rendered page's
        # _alignY2; the Billing tab (billing_module.html) carries it since C deleted
        # that page (30 Sep 2026).
        import os
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "web", "templates", "billing_module.html"), encoding="utf-8").read()
        self.assertIn("if (rmin < 0) {", src)                        # the negative branch
        self.assertIn("y2min = rmin - (-rmin) * 0.08;", src)         # headroom below the trough
        self.assertNotIn("y1top = y1range[1]; y2min = 0;", src)      # old one-line clamp gone

if __name__ == "__main__":
    unittest.main()
