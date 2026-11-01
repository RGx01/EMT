"""
BL-94 (1 Nov 2026) — the PDF button on Bills and Usage Stats.

Three faults, each from the page's source as the other page tests read it:

1. **Bills held only some of the day charts.** A day's chart is built within 600px of the view and
   released past 4000px, and `snapshotCharts` drew only the built ones, so the PDF had the days near
   where the page was scrolled. It now builds, draws and releases each filled day in turn, and
   rebuilds the ones shown.
2. **Usage Stats' settings were a copy of the toolbar's buttons.** They are a line of named values
   from the controls shown (`barPdfSettings`), with the legend, and the brackets' line when shown.
3. **Usage Stats in the dark theme leaked into the PDF.** The canvas was copied as drawn: the Net
   line and the tariff lanes' text white on white. The chart is drawn again in the light theme
   (`barIsDark` false while `barPdfLight`), with no animation, by hand, then back as the page shows it.

Checked in the local preview on 1 Nov 2026, on a scratch copy of staging, in the dark theme: Usage
Stats Monthly with Net and Tariffs, the PDF's chart light (the Net line black, the lanes' text dark),
its bars drawn, and the page's chart dark again after; Bills, one chart built on the page and all 26
of the period's days in the PDF, drawn light.

Discriminating: every test fails on the parent tree.
"""
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHARTS = open(os.path.join(_ROOT, "web", "templates", "charts.html"), encoding="utf-8").read()
BILLING = open(os.path.join(_ROOT, "web", "templates", "billing_module.html"), encoding="utf-8").read()


def _fn(page, name):
    i = page.index("function " + name + "(")
    end = page.index("\n}\n", i) if page[i - 1] == "\n" else page.index("\n  }\n", i)
    return page[i:end + 2]


class BillsEveryDay(unittest.TestCase):

    def test_every_filled_day_not_only_the_built(self):
        f = _fn(BILLING, "snapshotCharts")
        self.assertIn("Object.keys(sec.dayRows).forEach(function (d) {", f)
        self.assertNotIn("var days = Object.keys(state.charts);", f)

    def test_drawn_one_at_a_time_and_the_shown_rebuilt(self):
        f = _fn(BILLING, "snapshotCharts")
        self.assertIn("var shown = Object.keys(state.charts);", f)
        self.assertIn("shown.forEach(buildChart);", f)
        self.assertLess(f.index("out['bchart_' + d]"), f.rindex("releaseChart(d);"))

    def test_a_folded_day_is_not_drawn(self):
        self.assertIn("if (!el || !el.getClientRects().length) return;", _fn(BILLING, "snapshotCharts"))


class UsageStatsLight(unittest.TestCase):

    def test_one_reader_of_the_theme(self):
        self.assertIn("function barIsDark() { return !barPdfLight && ", CHARTS)
        self.assertEqual(CHARTS.count("document.documentElement.getAttribute('data-theme') !== 'light'"), 1)
        self.assertIn("var dark = barIsDark();", _fn(CHARTS, "barGetTheme"))
        self.assertIn("var dark = barIsDark();", _fn(CHARTS, "barBrColor"))
        self.assertIn("dark = barIsDark();", CHARTS[CHARTS.index("var c = chart.ctx, th = barGetTheme()"):][:120])

    def test_drawn_light_by_hand_then_back(self):
        f = _fn(CHARTS, "_doPdfExport")
        self.assertIn("barPdfLight = true; Chart.defaults.animation = false; Chart.defaults.devicePixelRatio = 2;", f)
        self.assertIn("ch.resize(); ch.draw();", f)
        self.assertIn("barPdfLight = false; Chart.defaults.animation = _anim; Chart.defaults.devicePixelRatio = _dpr;", f)
        self.assertLess(f.index("ch.resize(); ch.draw();"), f.index("} finally {"))


class UsageStatsSettings(unittest.TestCase):

    def test_named_values_not_the_toolbar(self):
        f = _fn(CHARTS, "_doPdfExport")
        self.assertIn("var toolbarHtml = barPdfSettings();", f)
        self.assertNotIn("barToolbar.cloneNode(true)", f)

    def test_what_the_line_names(self):
        f = _fn(CHARTS, "barPdfSettings")
        for label in ("'Period'", "'Periods'", "'Metric'", "'Show'", "'Series'", "'Order'", "'With'", "'Sort'"):
            self.assertIn("add(" + label, f)
        self.assertIn("add(lbl ? 'Range' : 'Years'", f)
        self.assertIn("function shown(el) { return !!el && el.getClientRects().length > 0; }", f)

    def test_the_legend_and_the_brackets_line(self):
        f = _fn(CHARTS, "_doPdfExport")
        self.assertIn("header + toolbarHtml + legendHtml + chartSection + noteHtml +", f)


if __name__ == "__main__":
    unittest.main()
