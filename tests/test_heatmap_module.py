"""
The in-browser Heatmaps tab (web/templates/heatmap_module.html) — decisions pinned.

JavaScript cannot run in the suite (no Node on the Mac or the Pi), so what the module
must not lose is pinned from its source. Each line here was learned by measuring it in
a browser against a real 4.5.16 database (30 Sep 2026), not assumed:

  * the y axis is set `reverse: false` EXPLICITLY — chartjs-chart-matrix's defaults
    reverse it, which drew the newest day at the bottom while the hover arithmetic
    counted rows from the top, so every tooltip named the wrong day;
  * hover is arithmetic on the grid, not Chart.js's tooltip — which repaints every
    cell on every pointer move;
  * canvas density is capped at 2x;
  * one chart per MONTH (v4's layout), the newest loaded at once, older ones on scroll;
  * data comes from /api/energy, month by month, never a pre-rendered file.

And three that the first in-browser version got wrong, found comparing it with v4 on
the Pi (30 Sep 2026):

  * a theme switch re-draws every drawn month — the zero colour is the page
    background, baked into the canvas, and it stayed behind until another tab;
  * cells are square — one size for both axes, from the width;
  * the colour scales are v4's own stops (energy_charts.make_colorscale, _hmCo2Cs).
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
        self.assertIn("var dpr = Math.min(2, window.devicePixelRatio || 1);", _SRC)
        self.assertEqual(_SRC.count("devicePixelRatio: dpr"), 2)      # grid and totals

    def test_it_reads_the_api_month_by_month(self):
        self.assertIn("/api/energy?resolution=block&kind=month&year=", _SRC)
        self.assertNotIn("net_heatmap", re.sub(r"<!--.*?-->", "", _SRC, flags=re.S))

    def test_the_newest_month_does_not_wait_for_a_scroll(self):
        self.assertIn("if (i === 0) loadMonth(k);", _SRC)


class V4Parity(unittest.TestCase):
    """REGRESSION (30 Sep 2026): each fails on the tree #17 merged."""

    def test_a_theme_switch_redraws(self):
        self.assertRegex(_SRC, r"new MutationObserver\(function \(\) \{ redrawAll\(true\); \}\)\s*"
                               r"\.observe\(document\.documentElement, \{attributes: true, "
                               r"attributeFilter: \['data-theme'\]\}\)")
        self.assertIn("function bg() { return dark() ? '#1a1d27' : '#ffffff'; }", _SRC)

    def test_cells_are_square(self):
        """One cell size, used for both the width and the height of the grid."""
        self.assertIn("function cellSize(panel) {", _SRC)
        self.assertIn("h = days.length * cs + top + 2", _SRC)

    def test_the_kwh_scale_is_v4s(self):
        """make_colorscale's stops: blue for export, the background at zero, red for import."""
        for c in ("#003366", "#0066cc", "#00aa66", "#ffcc99", "#ff6600", "#cc0000"):
            self.assertIn("'%s'" % c, _SRC[_SRC.index("function kwhStops("):_SRC.index("function co2Stops(")])
        self.assertIn("[wp, W]", _SRC)


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
