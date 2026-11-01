"""
BL-104 PR 1 (1 Nov 2026, accumulation design) — the Running total view in Usage Stats, as a line.

The user: move the spirals into Usage Stats as an accumulation chart, line or spiral. Show gains
*Running total*: a card a measure side by side as the spiral had them (the user) — Cost, Usage, and
Carbon where there is any — for one series: Net (import − export), Import, Export, a device, or the
Standing charges (the user: a series), the Cost card alone. *Lifetime* is one line from the first
day; *Year by year* each year from nought on one year's axis. The table is a row a year: each
measure's total, its change on the year before (an amount: a net crossing nought makes a percentage
meaningless), and its total to the same day of the year as the latest day (the user: like for like).
A day's increment is the spiral's rule (EMTSpiral incValue). Hidden in the view: the period, the
metric, Billing / Calendar, Net, the overlays and the sort. The PDF takes the cards.

Checked in the local preview on 1 Nov 2026, on a scratch copy of staging: Net's lifetime totals the
Spiral tab's (£781, 17.4 MWh, 3.8 t) and Standing charges' (£624); Year by year a line a year; the
view and winding restored; the PDF with the three cards and the settings line.

Discriminating: every test fails on the parent tree.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(_ROOT, "web", "templates", "charts.html"), encoding="utf-8").read()


def _kinds():
    i = PAGE.index("var ERA_KINDS = {")
    return PAGE[i:PAGE.index("\n};\n", i) + 3] + "\n"


def _fn(name):
    if name == "erasGroups":                  # the eras' order needs their kinds
        return _kinds() + _fn_plain(name)
    return _fn_plain(name)


def _fn_plain(name):
    i = PAGE.index("function " + name + "(")
    line = PAGE[i:PAGE.index("\n", i)]
    if line.count("{") == line.count("}"):
        return line + "\n"
    return PAGE[i:PAGE.index("\n}\n", i) + 2]


ROWS = [
    {"year": 2024, "month": 12, "day": 30, "imp_kwh": 10, "exp_kwh": 2, "imp_cost": 3.0, "exp_cost": 0.3,
     "net_cost": 3.2, "standing": 0.5, "meters": {"electricity_main": {"carbon_g_imp": 2000, "carbon_g_exp": 500},
                                                   "heat_pump": {"imp_kwh": 4, "imp_cost": 1.0, "carbon_g": 800}}},
    {"year": 2025, "month": 1, "day": 2, "imp_kwh": 8, "exp_kwh": 1, "imp_cost": 2.0, "exp_cost": 0.1,
     "net_cost": 2.4, "standing": 0.5, "meters": {"electricity_main": {"carbon_g_imp": 1000, "carbon_g_exp": 100}}},
    {"year": 2025, "month": 3, "day": 1, "imp_kwh": 5, "exp_kwh": 0, "imp_cost": 1.5, "exp_cost": 0,
     "net_cost": 2.0, "standing": 0.5, "meters": {}},
]


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheTotals(unittest.TestCase):

    def _run(self, expr, series="net", inc=False):
        # PR 3: a day's year is the year its year began in, from 1 January here
        js = "var barAccSeries = %s, barAccIncStanding = %s, barActiveYears = null, barData = %s;\n" % (
            json.dumps(series), "true" if inc else "false", json.dumps(ROWS)) + \
            "var barAccStart = 'jan', barTariffs = [], barEras = [], barBillingDay = 1, BAR_ACC_MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];\n" + \
            "\n".join(_fn(n) for n in ("barAccInc", "barAccMs", "barAccStartChoices", "erasGroups", "barTariffPriceStarts", "barTariffChangeDays", "barTariffVatOnly", "barAccDateLabel", "barAccAnchor", "barAccStartMs", "barAccYearOf", "barAccYearDays", "barAccDoy", "barAccRows", "barAccSeriesOf")) + \
            "\nconsole.log(JSON.stringify(" + expr + "));"
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_net_is_import_less_export_in_each_measure(self):
        self.assertEqual(self._run("barAccInc(barData[0], 'usage')"), 8)
        self.assertAlmostEqual(self._run("barAccInc(barData[0], 'carbon')"), 1.5)
        self.assertAlmostEqual(self._run("barAccInc(barData[0], 'cost')"), 2.7)          # net less standing
        self.assertAlmostEqual(self._run("barAccInc(barData[0], 'cost')", inc=True), 3.2)

    def test_a_device_and_the_standing_charges(self):
        self.assertEqual(self._run("barAccInc(barData[0], 'usage')", series="heat_pump"), 4)
        self.assertEqual(self._run("barAccInc(barData[0], 'standing')", series="standing"), 0.5)

    def test_lifetime_runs_on_years_start_from_nought(self):
        s = self._run("barAccSeriesOf(barAccRows(), 'usage')")
        self.assertEqual([p["y"] for p in s["life"]], [8, 15, 20])
        self.assertEqual([p["y"] for p in s["years"]["2025"]], [7, 12])
        self.assertEqual(s["totals"], {"2024": 8, "2025": 12})

    def test_to_the_same_day_of_the_year_like_for_like(self):
        # the latest day is 1 March (day 59): 2024 holds only 30 Dec, after it; 2025 both its days
        s = self._run("barAccSeriesOf(barAccRows(), 'usage')")
        self.assertEqual(s["lastDoy"], 59)
        self.assertEqual(s["toDate"], {"2025": 12})


class TheView(unittest.TestCase):

    def test_a_third_show(self):
        self.assertIn("""onclick="barSetShow('running',this)">Running total</button>""", PAGE)
        self.assertIn("if (barRunning()) { barAccRender(); return; }", _fn("barRender"))

    def test_the_cards_side_by_side_standing_alone(self):
        f = _fn("barAccKinds")
        self.assertIn("if (barAccSeries === 'standing') return [{kind: 'standing', unit: '\u00a3', title: 'Standing charges'}];", f)
        self.assertIn("k.push({kind: 'carbon', unit: 'kg', title: 'Carbon'});", f)

    def test_standing_charges_a_series(self):
        self.assertIn("out.push({id: 'standing', label: 'Standing charges'});", _fn("barAccSeriesList"))

    def test_what_the_view_hides(self):
        f = _fn("barAccSyncUI")
        for el in ("bar-cal-seg", "bar-order-seg", "bar-net-wrap", "bar-tariff-wrap", "bar-eras-wrap", "bar-sort-btn"):
            self.assertIn("'" + el + "'", f)
        self.assertIn("vis('bar-period-seg', !on, '');", f)
        self.assertIn("vis('bar-metric-seg', !on, '');", f)

    def test_the_change_is_an_amount(self):
        self.assertIn("var ch = p != null ? t - p : null;", _fn("barAccBuildTable"))

    def test_kept_with_the_view(self):
        self.assertIn("accSeries:   barAccSeries,", _fn("barSaveState"))
        self.assertIn("if (st.accMode === 'lifetime' || st.accMode === 'year') barAccMode = st.accMode;", _fn("barLoadState"))

    def test_the_pdf_takes_the_cards(self):
        f = _fn("_doPdfExport")
        self.assertIn("accShots = barAccCharts.map(function(c, i) {", f)
        self.assertIn("var chartSection = accShots", f)


if __name__ == "__main__":
    unittest.main()
