"""
The tariffs as dots on Running total's Year by year line (1 Nov 2026, the user: "on the year by year running
totals would it be possible to add dot markers with a hover for the tariffs with a selector to hide").

Lifetime has the tariffs as a band above the plot; Year by year had none. Now, on Year by year, each import
agreement's start is a filled dot and each price change inside it (barTariffChangeDays: after its start, before
its end, up to today) a smaller hollow one, on its year's line at that day's running total, in the year's
colour. A year not shown, or a day before the year's first point or after its last, has no dot. The hover is
the bars' tariff tip at the dot's day (barTariffShow with its date, so the prices in force from it). The Tariffs
box shows on both line modes and hides them. The spiral's dots are test_running_total_spiral_tariff_dots.

The preview's scratch copy has no stored prices: checked there on 1 Nov 2026 with the starts, and with price
changes injected.

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


def _fn(name):
    i = PAGE.index("function " + name + "(")
    line = PAGE[i:PAGE.index("\n", i)]
    if line.count("{") == line.count("}"):
        return line + "\n"
    return PAGE[i:PAGE.index("\n}\n", i) + 2]


def _kinds():
    i = PAGE.index("var ERA_KINDS = {")
    return PAGE[i:PAGE.index("\n};\n", i) + 3] + "\n"


def _p(frm):
    return {"kind": "unit", "band": "flat", "from": frm, "inc": 20}


TARIFFS = [
    {"direction": "import", "name": "Go", "from": "2023-03-01", "to": "2024-02-01",
     "prices": [_p("2023-03-01"), _p("2023-10-01")]},
    {"direction": "import", "name": "Intelligent Go", "from": "2024-02-01", "to": None,
     "prices": [_p("2024-02-01"), _p("2024-06-01"), _p("2099-01-01")]},    # a scheduled change: none
    {"direction": "export", "name": "Out", "from": "2023-05-01", "to": None, "prices": []},
]
# A year's points, a day each: x the day of the year, y the running total (here, the day number)
SERIES = {"years": {"2023": [{"x": d, "y": d} for d in range(0, 365)],
                    "2024": [{"x": d, "y": d * 2} for d in range(0, 200)]}}


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheDots(unittest.TestCase):

    def _run(self, mode="year", years=(2023, 2024), view="line", on=True, series=SERIES):
        js = ("var barAccMode = %s, barAccView = %s, barAccStart = 'jan', barData = [], barBillingDay = 1,"
              " BAR_ACC_YC = ['#a', '#b', '#c'], BAR_ACC_MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];\n"
              "var barTariffs = %s, barEras = [];\n"
              "var document = {getElementById: function (id) { return {checked: id === 'bar-tariff-cb' ? %s : false}; }};\n") % (
            json.dumps(mode), json.dumps(view), json.dumps(TARIFFS), "true" if on else "false") + _kinds() + \
            "\n".join(_fn(n) for n in ("barAccMs", "barAccStartChoices", "erasGroups", "barTariffPriceStarts",
                                       "barTariffChangeDays", "barTariffVatOnly", "barAccDateLabel", "barAccAnchor", "barAccStartMs",
                                       "barAccYearOf", "barAccDoy", "barAccShowEras", "barAccShowTariffs", "barAccTariffDays",
                                       "barAccOverlays")) + \
            "\nconsole.log(JSON.stringify(barAccOverlays(%s, %s)));" % (json.dumps(list(years)), json.dumps(series))
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_a_dot_at_each_start_and_change_on_its_years_line(self):
        dots = [(d["iso"], d["x"], d["y"], d["change"], d["color"]) for d in self._run()["dots"]]
        self.assertEqual(dots, [("2023-03-01", 59, 59, False, "#a"),      # 1 Mar 2023: day 59
                                ("2023-10-01", 273, 273, True, "#a"),
                                ("2024-02-01", 31, 62, False, "#b"),
                                ("2024-06-01", 152, 304, True, "#b")])    # 2024 a leap year; no 2099, no export

    def test_none_in_a_year_not_shown_or_past_its_points(self):
        self.assertEqual([d["iso"] for d in self._run(years=(2024,))["dots"]], ["2024-02-01", "2024-06-01"])
        short = {"years": {"2023": [{"x": d, "y": d} for d in range(100, 200)], "2024": []}}
        self.assertEqual(self._run(series=short)["dots"], [])            # 1 Mar before day 100; 1 Oct after 199

    def test_the_box_hides_them_and_lifetime_keeps_its_band(self):
        self.assertEqual(self._run(on=False)["dots"], [])
        life = self._run(mode="lifetime")
        self.assertEqual((life["dots"], [a["name"] for a in life["tariffs"]]), ([], ["Go", "Intelligent Go"]))
        self.assertEqual(self._run(view="spiral")["dots"], [])          # the spiral draws its own


class TheView(unittest.TestCase):

    def _plugin(self):
        i = PAGE.index("function barAccOverlayPlugin(")
        return PAGE[i:PAGE.index("\n}\n", i)]

    def test_drawn_filled_or_hollow_and_hovered_with_the_days_prices(self):
        body = self._plugin()
        self.assertIn("r = d.change ? 3 : 4.5;", body)
        self.assertIn("barTariffShow(dt.d.a, e.native, dt.d.iso); return false;", body)

    def test_each_chart_passes_its_series(self):
        self.assertIn("var ov = barAccOverlays(yearsShown, s);", _fn("barAccRender"))


if __name__ == "__main__":
    unittest.main()
