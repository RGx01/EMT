"""
BL-104 PR 4 (1 Nov 2026, accumulation design §2) — eras and tariffs on the running total.

Usage Stats' own *Eras* and *Tariffs* checkboxes, in the Running total view where they apply. On the
*Lifetime* line the import tariffs are a band above the plot, as on the bars, and each era a dashed
line with its icon chip (a fainter line at its end); on *Year by year* the eras only, each in its
year's colour at its day of the year — a band a year would be clutter; on the spiral each era is a
chip on the coil at its start. The hovers are the bars' own. The Lifetime axis now spans the data's
own days: Chart.js rounded it out (April 2022 to January 2027 for June 2023 to October 2026).

Checked in the local preview on 1 Nov 2026, on a scratch copy of staging, two eras posted and
cleared after: the tariff band and the eras on the Lifetime line, their hovers; Year by year with
the eras and no tariffs; the spiral's two chips.

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
class TheOverlays(unittest.TestCase):

    def _run(self, mode, years, tariffs_on=True, eras_on=True):
        js = ("var barAccMode = %s, barAccStart = 'jan', barData = [], barBillingDay = 1,"
              " BAR_ACC_YC = ['#a', '#b', '#c'], BAR_ACC_MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];\n"
              "var barTariffs = [{direction: 'import', name: 'Go', from: '2023-06-01'}, {direction: 'export', name: 'Out', from: '2023-06-01'}];\n"
              "var barEras = [{id: 'a1b2c3d4', label: 'Battery', kind: 'battery', start: '2024-06-12', end: '2025-01-01'}];\n"
              "var document = {getElementById: function (id) { return {checked: id === 'bar-tariff-cb' ? %s : %s}; }};\n") % (
            json.dumps(mode), "true" if tariffs_on else "false", "true" if eras_on else "false") + \
            "\n".join(_fn(n) for n in ("barAccMs", "barAccStartChoices", "erasGroups", "barAccDateLabel", "barAccAnchor", "barAccStartMs",
                                        "barAccYearOf", "barAccDoy", "barAccShowEras", "barAccShowTariffs",
                                        "barAccOverlays")) + \
            "\nconsole.log(JSON.stringify(barAccOverlays(%s)));" % json.dumps(years)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_lifetime_the_import_tariffs_and_the_era_by_its_date(self):
        out = self._run("lifetime", [2023, 2024])
        self.assertEqual([a["name"] for a in out["tariffs"]], ["Go"])          # import only
        self.assertEqual(out["eras"][0]["x"], 1718150400000)                    # 12 Jun 2024, ms
        self.assertEqual(out["eras"][0]["end"], 1735689600000)

    def test_year_by_year_the_era_at_its_day_in_its_years_colour_no_tariffs(self):
        out = self._run("year", [2023, 2024])
        self.assertIsNone(out["tariffs"])
        self.assertEqual((out["eras"][0]["x"], out["eras"][0]["color"]), (163, "#b"))   # 12 Jun 2024: day 163

    def test_an_era_in_a_year_not_shown_is_left_out(self):
        self.assertEqual(self._run("year", [2023])["eras"], [])

    def test_off_when_their_boxes_are(self):
        out = self._run("lifetime", [2023, 2024], tariffs_on=False, eras_on=False)
        self.assertEqual(out, {"tariffs": None, "eras": []})


class TheView(unittest.TestCase):

    def test_the_boxes_shown_where_they_apply(self):
        f = _fn("barAccSyncUI")
        self.assertIn("vis('bar-eras-wrap', !!(barEras && barEras.length), 'flex');", f)
        self.assertIn("&& barAccView === 'line' && barAccMode === 'lifetime', 'flex');", f)

    def test_the_line_charts_take_the_overlays(self):
        f = _fn("barAccRender")
        self.assertIn("plugins: ov.tariffs || ov.eras.length ? [barAccOverlayPlugin(ov)] : [],", f)

    def test_the_hovers_are_the_bars(self):
        i = PAGE.index("function barAccOverlayPlugin(")
        body = PAGE[i:PAGE.index("\n}\n", i)]
        self.assertIn("barEraShow(m.e, e.native);", body)
        self.assertIn("barTariffShow(b.a, e.native, null);", body)

    def test_the_spiral_s_era_chips(self):
        self.assertIn("if (barAccShowEras()) {", _fn("barAccSpiral"))

    def test_the_lifetime_axis_is_the_datas_span(self):
        self.assertIn("min: s.life.length ? s.life[0].x : undefined, max: s.life.length ? s.life[s.life.length - 1].x : undefined,",
                      _fn("barAccRender"))


if __name__ == "__main__":
    unittest.main()
