"""
bill_card — the Billing tab's bill card and side panel as rows, C 30 Sep 2026.

What the card shows is decided in bill_card.py and only printed by the browser
(billing_module.html). #21 had put that arithmetic in a JavaScript port, where the
suite cannot run it; this pins it where it now lives.

THE ROWS ARE EXACT. energy_charts.render_billing_summary and the day side panel now
format these rows with the pre-rendered page's templates, and on a real database the
whole 36 MB page came out byte-identical to the one generated before #20 (md5,
30 Sep 2026) — every card and every panel. The browser prints the same rows:
.build/billing_client_parity.js, 98 cards / 98 labels / 98 compare pairs / 1,212
panels identical. So these tests pin behaviour that was measured, not assumed.

They are new-module tests and fail on the parent tree, where bill_card does not exist.
The older card tests (test_billing_split_rows, test_billing_rate_collapse, …) still run
through energy_charts' wrappers; step 3 moves them here as it deletes the markup.
"""
import json
import os
import sys
import unittest
from datetime import date, datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import bill_card as bc                            # noqa: E402

MAIN = "Electricity Main / Import"
EXP = "Electricity Main / Export"


def _summary(**over):
    """A minimal calculate_billing_summary_for_period dict: one main meter, two rates,
    export, a week of standing charge."""
    s = {
        "start": datetime(2026, 9, 3), "end": datetime(2026, 10, 3),
        "meters": {MAIN: {0.07: {"kwh": 100.0, "cost": 7.0}, 0.30: {"kwh": 50.0, "cost": 15.0}},
                   EXP: {0.15: {"kwh": 20.0, "cost": -3.0}}},
        "totals": {MAIN: {"kwh": 150.0, "cost": 22.0, "is_submeter": False, "read_start": None, "read_end": None},
                   EXP: {"kwh": 20.0, "cost": -3.0, "is_submeter": False, "read_start": 10.0, "read_end": 30.0}},
        "main_import_raw": {0.07: {"kwh": 100.0, "cost": 7.0}, 0.30: {"kwh": 50.0, "cost": 15.0}},
        "ev_by_rate": {}, "home_by_rate": {}, "ev_transition": {}, "home_transition": {},
        "main_import_reads": {"read_start": 1000.0, "read_end": 1150.0},
        "standing": {date(2026, 9, d): 0.6123 for d in range(3, 10)},
        "total_standing": 4.29, "total_cost": 23.29, "meter_meta": {},
    }
    s.update(over)
    return s


def _types(card):
    return [r["type"] for r in card["rows"]]


class RateRows(unittest.TestCase):

    def test_one_row_per_rate_ascending_rounded_and_summed_as_printed(self):
        rows, tk, tc = bc.rate_rows({0.30: {"kwh": 1.0004, "cost": 0.3004},
                                     0.07: {"kwh": 2.0006, "cost": 0.1406}})
        self.assertEqual([r["rate"] for r in rows], ["0.0700", "0.3000"])
        self.assertEqual([r["kwh"] for r in rows], ["2.001", "1.000"])
        # The Total is the sum of the ROUNDED rows (2.001 + 1.000), not of the raw kWh.
        self.assertEqual((tk, tc), (3.001, 0.44))

    def test_more_than_five_rates_collapse_to_one_average_row(self):
        ch = {0.10 + i / 100: {"kwh": 1.0, "cost": round(0.10 + i / 100, 4)} for i in range(6)}
        rows, tk, tc = bc.rate_rows(ch)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["note"], {"avg": 6})
        self.assertEqual(rows[0]["kwh"], "6.000")
        self.assertEqual(rows[0]["rate"], "%.4f" % (tc / tk))

    def test_five_rates_do_not_collapse(self):
        ch = {0.10 + i / 100: {"kwh": 1.0, "cost": 0.1} for i in range(5)}
        self.assertEqual(len(bc.rate_rows(ch)[0]), 5)

    def test_a_credit_is_in_parentheses(self):
        self.assertEqual(bc.paren(-3.0, 2), "(3.00)")
        self.assertEqual(bc.paren(3.0, 2), "3.00")

    def test_formatting_is_pythons(self):
        """GUARD: the rows carry Python's formatting — round-half-even on the exact
        binary value. The browser's toFixed gives 0.125 → "0.13"; nothing formats there now."""
        self.assertEqual(bc.total_row(1.0, 0.125)["cost"], "0.12")
        self.assertEqual(bc.total_row(1.0, 0.375)["cost"], "0.38")


class SplitRows(unittest.TestCase):

    def _s(self, ev, home, ev_tr=None, home_tr=None):
        return _summary(ev_by_rate=ev, home_by_rate=home, ev_transition=ev_tr or {}, home_transition=home_tr or {})

    def test_no_ev_is_none(self):
        self.assertIsNone(bc.split_rows(self._s({}, {0.07: {"kwh": 1.0, "cost": 0.07}})))

    def test_jittered_rates_are_one_band_with_one_shared_rate(self):
        s = self._s({0.0700: {"kwh": 10.0, "cost": 0.700}, 0.0712: {"kwh": 1.0, "cost": 0.0712}},
                    {0.0705: {"kwh": 4.0, "cost": 0.282}})
        rows = bc.split_rows(s)
        self.assertEqual([r["label"] for r in rows], ["EV", "Home"])
        self.assertEqual(rows[0]["rate"], rows[1]["rate"])       # B1: one rate per band
        self.assertEqual(rows[0]["kwh"], "11.000")

    def test_a_real_rate_change_is_its_own_band(self):
        s = self._s({0.07: {"kwh": 1.0, "cost": 0.07}, 0.30: {"kwh": 1.0, "cost": 0.30}}, {})
        self.assertEqual([r["rate"] for r in bc.split_rows(s)], ["0.0700", "0.3000"])

    def test_transition_rows_come_last_and_say_so(self):
        s = self._s({0.07: {"kwh": 1.0, "cost": 0.07}}, {}, ev_tr={"kwh": 2.0, "cost": 0.4},
                    home_tr={"kwh": 1.0, "cost": 0.2})
        rows = bc.split_rows(s)
        self.assertEqual([(r["label"], r["note"]) for r in rows[-2:]],
                         [("EV", "transition"), ("Home", "transition")])
        self.assertEqual(rows[-2]["rate"], "0.2000")

    def test_costs_are_three_decimals_as_the_bill(self):
        s = self._s({0.07: {"kwh": 1.0, "cost": 0.0701}}, {})
        self.assertEqual(bc.split_rows(s)[0]["cost"], "0.070")


class TheCard(unittest.TestCase):

    def test_sections_in_order(self):
        c = bc.card(_summary())
        self.assertEqual(_types(c), [
            "title", "header", "rate", "total",                   # Export
            "title", "header", "rate", "rate", "total",           # Import — total grid
            "line_block", "line_inline",                          # standing, incl. standing
            "breakdown", "title", "header", "rate", "rate", "total",   # Direct import
            "line_block"])                                        # Total Bill
        self.assertEqual(c["rows"][-1]["value"], "£23.29")
        self.assertEqual(c["total"], "£23.29")

    def test_standing_is_grouped_by_rate_and_folded_into_the_import(self):
        c = bc.card(_summary())
        st = [r for r in c["rows"] if r.get("cls") == "standing"]
        self.assertEqual(st[0]["text"], "Standing charge: 7 days @ £0.6123/day")
        incl = next(r for r in c["rows"] if r["type"] == "line_inline")
        self.assertEqual(incl["value"], "%.2f" % (22.0 + round(0.6123 * 7, 2)))

    def test_a_mid_period_standing_change_is_two_lines(self):
        s = _summary(standing={date(2026, 9, 3): 0.60, date(2026, 9, 4): 0.60, date(2026, 9, 5): 0.65})
        st = [r["text"] for r in bc.card(s)["rows"] if r.get("cls") == "standing"]
        self.assertEqual(st, ["Standing charge: 2 days @ £0.6000/day", "Standing charge: 1 days @ £0.6500/day"])

    def test_reads_and_mpan_travel_with_the_title(self):
        s = _summary(meter_meta={EXP: {"mpan": "1234"}})
        t = bc.card(s)["rows"][0]
        self.assertEqual(t["mpan"], "1234")
        self.assertEqual(t["reads"], {"start": "10.000", "end": "30.000", "total": "20.000"})

    def test_the_bill_method_replaces_the_inc_vat_rows(self):
        bm = {"rows": [{"rate_exc": 0.0667, "kwh": 100.0, "cost_exc": 6.667}], "energy_exc": 6.67,
              "standing_days": 7, "standing_rate_exc": 0.5831, "standing_rows": [{"days": 7}],
              "standing_exc": 4.08, "subtotal_exc": 10.75, "vat_rate": 0.05, "vat_amount": 0.54, "inc_total": 11.29}
        c = bc.card(_summary(bill_method=bm))
        header = [r for r in c["rows"] if r["type"] == "header"][1]
        self.assertEqual(header["rate"], "Rate (£/kWh, exc)")
        texts = [r.get("text") for r in c["rows"]]
        self.assertIn("VAT @ 5%", texts)
        self.assertIn("Total incl. VAT", texts)
        self.assertNotIn("line_inline", _types(c))             # no inc-VAT standing fold

    def test_a_submeter_is_named_by_its_device(self):
        s = _summary()
        s["meters"]["EV Charger (Sub-meter) / Import"] = {0.07: {"kwh": 5.0, "cost": 0.35}}
        s["totals"]["EV Charger (Sub-meter) / Import"] = {"kwh": 5.0, "cost": 0.35, "is_submeter": True}
        titles = [r["text"] for r in bc.card(s)["rows"] if r["type"] == "title" and r["indent"]]
        self.assertEqual(titles, ["Direct import", "EV Charger"])

    def test_the_site_is_the_callers_or_the_main_meters(self):
        s = _summary(meter_meta={MAIN: {"site": "Home", "is_submeter": False}})
        self.assertEqual(bc.card(s)["site"], "Home")
        self.assertEqual(bc.card(s, site_name="Other")["site"], "Other")

    def test_it_is_json(self):
        c = bc.card(_summary())
        self.assertEqual(json.loads(json.dumps(c)), c)


class ThePageFormatsTheseRows(unittest.TestCase):
    """GUARD: energy_charts renders from bill_card, not a second copy of the rules."""

    def test_render_billing_summary_is_the_rows(self):
        import energy_charts as ec
        html = ec.render_billing_summary(_summary())
        for r in bc.card(_summary())["rows"]:
            for k in ("text", "rate", "kwh", "cost", "value"):
                if isinstance(r.get(k), str):
                    self.assertIn(r[k], html, "%s %s" % (r["type"], k))

    def test_the_helpers_are_bill_cards(self):
        import energy_charts as ec
        self.assertIs(ec._MAX_RATE_ROWS, bc.MAX_RATE_ROWS)
        self.assertIs(ec._SPLIT_BAND_EPS, bc.SPLIT_BAND_EPS)


class PanelLines(unittest.TestCase):

    def _panel(self, **over):
        p = {"total_import": 12.3456, "import_cost": 2.5, "import_cost_exc": None, "export_kwh": 0.0,
             "export_cost": 0.0, "export_rates": [], "main_color": "#111", "export_color": "#222",
             "breakdown": [{"label": "EV", "kwh": 5.0, "cost": 0.35, "color": "#333", "rate_color": "#444",
                            "rates": [[5.0, 0.07, None]]}]}
        p.update(over)
        return p

    def test_totals_and_columns(self):
        ln = bc.panel_lines(self._panel())
        self.assertEqual([x["text"] for x in ln["totals"]], ["Total import: 12.346 kWh", "Import cost: £2.50"])
        self.assertEqual([x["text"] for x in ln["cols"][0]],
                         ["↳ EV", "5.000 kWh", "£0.35", "5.000 kWh @ £0.0700"])
        self.assertTrue(ln["cols"][0][0]["bold"])

    def test_export_lines_only_with_export(self):
        ln = bc.panel_lines(self._panel(export_kwh=2.0, export_cost=0.3, export_rates=[[2.0, 0.15, None]]))
        self.assertIn("Export credit: £0.30", [x["text"] for x in ln["totals"]])
        self.assertEqual(ln["totals"][-1]["size"], "0.8em")

    def test_a_collapsed_rate_says_how_many(self):
        ln = bc.panel_lines(self._panel(breakdown=[{"label": "EV", "kwh": 1.0, "cost": 0.1, "color": "#3",
                                                     "rate_color": "#4", "rates": [[1.0, 0.1, 7]]}]))
        self.assertEqual(ln["cols"][0][-1]["text"], "1.000 kWh @ £0.1000 (avg of 7)")


if __name__ == "__main__":
    unittest.main()
