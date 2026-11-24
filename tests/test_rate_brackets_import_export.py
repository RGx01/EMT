"""Rate brackets: Import & export together (24 Nov 2026, the user).

Where the meter exports, the series picker offers 'both': the whole meter's import brackets above the
axis and the export's below, each in its own colours. Beside import, export takes hues of its own
(earned indigo to violet, paid to export magenta), as its blue and red alone would be the import
plunge's blue and the dearest import's red. In Cost the bars are money: a plunge's negative £ sits
below the axis with the export earned, a negative export price above with the import paid.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
with open(os.path.join(ROOT, "web", "templates", "charts.html"), encoding="utf-8") as fh:
    PAGE = fh.read()


def _fn(name):
    i = PAGE.index("function " + name + "(")
    line = PAGE[i:PAGE.index("\n", i)]
    if line.count("{") == line.count("}"):
        return line + "\n"
    return PAGE[i:PAGE.index("\n}\n", i) + 2]


def _node(src):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(src)
        path = fh.name
    try:
        res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
    finally:
        os.unlink(path)
    if res.returncode != 0:
        raise AssertionError(res.stderr)
    return json.loads(res.stdout)


# Two days: import at -5p (a plunge) and 8p; export at -2p (paid to export) and 15p. [kWh, £, g]
AGGS = [{"br": {"meter": {"-2": [1.0, -0.05, 0], "3": [4.0, 0.32, 0]},
                "export": {"-1": [0.5, -0.01, 0], "6": [2.0, 0.30, 0]}}},
        {"br": {"meter": {"3": [2.0, 0.16, 0]}, "export": {"6": [1.0, 0.15, 0]}}}]

_HEAD = ("var barBrSeries = 'both', barBrPct = false, barMetric = %s, barBrWidth = 2.5;\n"
         "function barIsDark() { return true; }\n")


def _datasets(metric, pct=False):
    src = (_HEAD % json.dumps(metric) + ("barBrPct = true;\n" if pct else "") +
           _fn("barBrOf") + _fn("barBrNs") + _fn("barBrBoth") + _fn("barBrPence") + _fn("barBrLabel") +
           _fn("barBrColor") + _fn("barBrVal") + _fn("barBrSign") + _fn("barBrDatasets") + _fn("barBrSideDatasets") +
           "\nconsole.log(JSON.stringify(barBrDatasets(%s).map(function (d) {"
           " return [d.label, d._side, d._sign, d.data, d.backgroundColor]; })));" % json.dumps(AGGS))
    return _node(src)


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheBars(unittest.TestCase):

    def test_kwh_import_above_export_below(self):
        got = _datasets("kwh")
        self.assertEqual([(g[0], g[1], g[2], g[3]) for g in got], [
            ("Import −5 to −2.5p", "import", 1, [1.0, 0]),
            ("Import 7.5–10p", "import", 1, [4.0, 2.0]),
            ("Export −2.5 to 0p", "export", -1, [-0.5, 0]),
            ("Export 15–17.5p", "export", -1, [-2.0, -1.0]),
        ])

    def test_cost_is_money_paid_above_paid_to_you_below(self):
        got = {g[0]: g[3] for g in _datasets("cost")}
        self.assertEqual(got["Import −5 to −2.5p"], [-0.05, 0])     # a plunge's credit: below
        self.assertEqual(got["Import 7.5–10p"], [0.32, 0.16])        # import paid: above
        self.assertEqual(got["Export −2.5 to 0p"], [0.01, 0])        # paid to export: above
        self.assertEqual(got["Export 15–17.5p"], [-0.3, -0.15])      # export earned: below

    def test_pct_is_each_sides_own_share(self):
        got = {g[0]: g[3] for g in _datasets("kwh", pct=True)}
        self.assertEqual(got["Import −5 to −2.5p"], [20, 0])
        self.assertEqual(got["Import 7.5–10p"], [80, 100])
        self.assertEqual(got["Export −2.5 to 0p"], [-20, 0])
        self.assertEqual(got["Export 15–17.5p"], [-80, -100])

    def test_no_colour_shared_across_the_sides(self):
        got = _datasets("kwh")
        imp = {g[4] for g in got if g[1] == "import"}
        exp = {g[4] for g in got if g[1] == "export"}
        self.assertFalse(imp & exp)
        # paid to export is magenta beside import (alone it is red, the dearest import's colour)
        self.assertTrue([g[4] for g in got if g[0] == "Export −2.5 to 0p"][0].startswith("hsl(320,"))


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheSingleSeriesAsBefore(unittest.TestCase):

    def test_export_alone_keeps_its_colours(self):
        # GUARD: export alone is drawn as before (red paid, blue to purple earned; the second bracket the lighter)
        src = ("var barBrSeries = 'export';\nfunction barIsDark() { return true; }\n" + _fn("barBrSign") +
               _fn("barBrColor") + "\nconsole.log(JSON.stringify([barBrColor(-1, [-1, 6]), barBrColor(6, [-1, 6])]));")
        self.assertEqual(_node(src), ["hsl(0, 62%, 48%)", "hsl(205, 62%, 68%)"])


class TheWiring(unittest.TestCase):

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_first_in_the_list_and_only_with_export(self):
        # 24 Nov 2026, the user: Import & export at the top of the list
        src = ("var barPeriod = 'daily', barBrHH = null, barBrDays = null, barMeters = [];\n" + _fn("barBrSeriesList") +
               "\nbarBrDays = {d1: {meter: {}, export: {}}};\nvar a = barBrSeriesList().map(function (o) { return o.id; });"
               "\nbarBrDays = {d1: {meter: {}}};\nvar b = barBrSeriesList().map(function (o) { return o.id; });"
               "\nconsole.log(JSON.stringify([a, b]));")
        self.assertEqual(_node(src), [["both", "meter", "export"], ["meter"]])

    def test_tooltip_note_table_and_axis(self):
        self.assertIn("v *= c.dataset._sign || barBrSign();", PAGE)
        self.assertIn("barBrFootLine(agg, 'meter', 'Import') + '\\n' + barBrFootLine(agg, 'export', 'Export')", PAGE)
        self.assertIn("'Import paid ' + bi.toFixed(1) + 'p/kWh on average'", _fn("barBrNote"))
        self.assertIn("imp.totals.replace('>Totals<', '>Import<')", _fn("barBrBuildTable"))
        self.assertIn("barBrBoth() ? { min: -100, max: 100, grace: 0 }", PAGE)

    def test_many_brackets_a_strip_a_side(self):
        f = _fn("barBuildLegend")
        self.assertIn("if (_ds.length > BAR_BR_KEYS_MAX && barBrBoth()) {", f)
        self.assertIn("barBrScale(box, part, sd[1]);", f)
        self.assertIn("barBrHighlight(byN[v] ? v : null, side);", _fn("barBrScale"))
        self.assertIn("(d._bracket === n && (!side || d._side === side))", _fn("barBrHighlight"))


if __name__ == "__main__":
    unittest.main()
