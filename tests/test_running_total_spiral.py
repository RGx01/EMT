"""
BL-104 PR 2 (1 Nov 2026, accumulation design) — the spiral in the Running total view.

*Line | Spiral* beside the winding: the same running total, wound a year a turn with 1 January at
the top — the Spiral tab's drawing moved into the view (its geometry, rings, month spokes, year
colours, centre and hover), as the tab goes in PR 5. Lifetime is one coil; Year by year each year
from nought on one scale. Each spiral card has an expand button (the tab's expand view); the PDF
takes the spirals as SVG on the light theme's colours. Negative figures carry a minus sign, kWh as
the £ do, and the table's cells keep to one line.

Checked in the local preview on 1 Nov 2026, on a scratch copy of staging: the three spirals' centres
£781, 17.4 MWh and 3.8 t — the Spiral tab's; the expand view with its hover; the PDF with three
spirals.

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


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheScale(unittest.TestCase):

    def _run(self, expr):
        # PR 3: the year's start, 1 January here
        js = ("var barCurrency = '£', barAccStart = 'jan', barTariffs = [], barEras = [], barData = [], barBillingDay = 1, "
              "BAR_ACC_MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];\n") + "\n".join(
            _fn(n) for n in ("barAccMs", "barAccStartChoices", "erasGroups", "barTariffPriceStarts", "barAccDateLabel", "barAccAnchor", "barAccStartMs",
                             "barAccYearOf", "barAccYearDays", "barAccDoy", "_spFrac", "_spNice", "_spUnit", "_spLbl",
                             "barAccFmt")) + \
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

    def test_a_year_a_turn_from_1_january(self):
        self.assertEqual(self._run("_spFrac({year: 2025, month: 1, day: 1})"), 0)
        self.assertAlmostEqual(self._run("_spFrac({year: 2025, month: 7, day: 2})"), 182 / 365)
        self.assertAlmostEqual(self._run("_spFrac({year: 2024, month: 12, day: 31})"), 365 / 366)   # a leap year

    def test_rings_on_round_numbers(self):
        self.assertEqual(self._run("[_spNice(41), _spNice(180), _spNice(0.42)]"), [50, 200, 0.5])

    def test_the_units_climb_past_a_thousand(self):
        self.assertEqual(self._run("_spUnit('kWh', 17358)"), {"u": "MWh", "d": 1000})
        self.assertEqual(self._run("_spUnit('kg', 3812)"), {"u": "t", "d": 1000})
        self.assertEqual(self._run("_spLbl(17358, 'kWh', {u: 'MWh', d: 1000})"), "17.4")
        self.assertEqual(self._run("_spLbl(781.11, '£', {u: '£', d: 1})"), "£781")

    def test_a_minus_sign_for_kwh_as_for_pounds(self):
        self.assertEqual(self._run("barAccFmt(-1433, 'kWh')"), "−1,433 kWh")
        self.assertEqual(self._run("barAccFmt(-16.09, '£')"), "−£16.09")


class TheView(unittest.TestCase):

    def test_line_or_spiral(self):
        self.assertIn("""onclick="barSetAccView('spiral',this)">Spiral</button>""", PAGE)
        self.assertIn("if (st.accView === 'line' || st.accView === 'spiral') barAccView = st.accView;", _fn("barLoadState"))
        self.assertIn("accView:     barAccView,", _fn("barSaveState"))

    def test_the_spiral_card_draws_and_expands(self):
        r = _fn("barAccRender")
        self.assertIn("barAccSpiral(document.getElementById('bar-acc-s-' + i), rows, k, yearsShown);", r)
        self.assertIn("onclick=\"barAccExpand(' + i + ')\"", r)

    def test_lifetime_one_coil_years_from_nought(self):
        f = _fn("barAccSpiral")
        self.assertIn("var vals = pts.map(function(p) { return barAccMode === 'lifetime' ? p.life : p.yc; });", f)
        self.assertIn("var a = G.A0 + p.frac * 2 * Math.PI;", f)

    def test_the_pdf_takes_the_spirals(self):
        f = _fn("_doPdfExport")
        self.assertIn("if (barRunning() && barAccView === 'spiral') {", f)
        self.assertIn("(a.svg ? '<div class=\"pdf-sp\">' + a.svg + '</div>'", f)


if __name__ == "__main__":
    unittest.main()
