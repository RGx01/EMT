"""
test_billing_split_render.py — bill_card.split_rows gives the IOG EV/Home breakdown
for 'Import — total grid' (clean bands interleaved per rate, boundary blocks as one
transition row each). None when there's no EV → caller shows the plain rate rows.

(C, 30 Sep 2026: it was energy_charts._bill_split_rows, which returned the rows as
HTML; `_flat` joins a row's printed fields so the figures are asserted as before.)
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bill_card import split_rows


def _bill_split_rows(summary, currency, exc=False):
    rows = split_rows(summary, exc=exc)
    return None if rows is None else rows


def _flat(rows):
    return " ".join(str(v) for r in rows for k, v in r.items() if k != "type")


class TestBillSplitRows(unittest.TestCase):

    def test_none_when_no_ev(self):
        self.assertIsNone(_bill_split_rows({}, "£"))
        self.assertIsNone(_bill_split_rows(
            {"ev_by_rate": {}, "home_by_rate": {0.30: {"kwh": 1.0, "cost": 0.30}}}, "£"))

    def test_ev_home_rows_like_the_bill(self):
        # mirrors the real capped bill: EV/Home both @ 5.23p off-peak, Home @ 32.11 peak
        html = _bill_split_rows({
            "ev_by_rate":   {0.0523: {"kwh": 152.9, "cost": 8.001, "cost_exc": 7.620}},
            "home_by_rate": {0.0523: {"kwh": 371.5, "cost": 19.434, "cost_exc": 18.500},
                             0.3211: {"kwh": 5.4,  "cost": 1.744,  "cost_exc": 1.660}},
        }, "£")
        self.assertIsNotNone(html)
        html = _flat(html)
        self.assertIn("EV", html)
        self.assertIn("Home", html)
        self.assertIn("152.900", html)       # EV kWh
        self.assertIn("8.001", html)          # EV cost — 3dp, as the bill
        self.assertIn("1.744", html)          # Home peak cost — 3dp
        self.assertIn("0.0523", html)         # rate = 8.001/152.9
        self.assertIn("0.3230", html)         # Home peak rate = 1.744/5.4

    def test_transition_rows(self):
        html = _bill_split_rows({
            "ev_by_rate":   {0.05: {"kwh": 100.0, "cost": 5.0, "cost_exc": 4.76}},
            "home_by_rate": {},
            "ev_transition":   {"kwh": 2.0, "cost": 0.30, "cost_exc": 0.29},
            "home_transition": {"kwh": 2.0, "cost": 0.37, "cost_exc": 0.35},
        }, "£")
        self.assertEqual([r["note"] for r in html].count("transition"), 2)
        self.assertIn("0.1500", _flat(html))  # EV transition rate 0.30/2.0

    def test_exc_uses_derived_cost(self):
        base = {"ev_by_rate": {0.05: {"kwh": 100.0, "cost": 5.0, "cost_exc": 4.762}},
                "home_by_rate": {}}
        self.assertIn("5.000", _flat(_bill_split_rows(base, "£", exc=False)))
        self.assertIn("4.762", _flat(_bill_split_rows(base, "£", exc=True)))


if __name__ == "__main__":
    unittest.main()
