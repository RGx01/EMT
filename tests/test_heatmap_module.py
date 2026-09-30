"""
The in-browser Heatmaps tab (web/templates/heatmap_module.html) — decisions pinned.

JavaScript cannot run in the suite (no Node on the Mac or the Pi), so what the module
must not lose is pinned from its source. Each line here was learned by measuring it in
a browser against a real 4.5.16 database (30 Sep 2026), not assumed:

  * the y axis is set `reverse: false` EXPLICITLY — chartjs-chart-matrix's defaults
    reverse it, which drew the newest day at the bottom while the hover arithmetic
    counted rows from the top, so every tooltip named the wrong day;
  * hover is arithmetic on the grid, not Chart.js's tooltip — which repaints a whole
    year (17,934 cells) on every pointer move;
  * canvas density is capped at 2x — a year is a tall canvas, ~32 MB at 2x;
  * one chart per year, the newest loaded at once, older ones on scroll;
  * data comes from /api/energy, month by month, never a pre-rendered file.
"""
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = open(os.path.join(_ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8").read()
_PAGE = open(os.path.join(_ROOT, "web", "templates", "charts.html"), encoding="utf-8").read()


class ModuleDecisions(unittest.TestCase):

    def test_the_y_axis_direction_is_explicit(self):
        m = re.search(r"y:\s*\{type: 'category'[^}]*", _SRC)
        self.assertIsNotNone(m)
        self.assertIn("reverse: false", m.group(0))

    def test_hover_does_not_repaint_the_canvas(self):
        self.assertIn("events: []", _SRC)
        self.assertIn("tooltip: {enabled: false}", _SRC)
        self.assertIn("function attachHover(", _SRC)

    def test_canvas_density_is_capped(self):
        self.assertIn("devicePixelRatio: Math.min(2,", _SRC)

    def test_it_reads_the_api_month_by_month(self):
        self.assertIn("/api/energy?resolution=block&kind=month&year=", _SRC)
        self.assertNotIn("net_heatmap", re.sub(r"<!--.*?-->", "", _SRC, flags=re.S))

    def test_the_newest_year_does_not_wait_for_a_scroll(self):
        self.assertIn("if (y === state.years[0]) loadYear(y); else io.observe(el);", _SRC)


class ThePageUsesIt(unittest.TestCase):

    def test_it_is_included_and_the_iframe_path_is_gone(self):
        self.assertIn('{% include "heatmap_module.html" %}', _PAGE)
        self.assertNotIn("/api/charts/heatmap", _PAGE)
        self.assertNotIn("heatmap_exists", _PAGE)

    def test_every_loader_path_routes_the_heatmap_to_the_module(self):
        fn = _PAGE[_PAGE.index("async function loadChartContent(name, force) {"):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn("if (name === 'heatmap') {", fn)
        self.assertIn("EMTHeatmap.rerender()", fn)
        self.assertIn("EMTHeatmap.refresh()", fn)


if __name__ == "__main__":
    unittest.main()
