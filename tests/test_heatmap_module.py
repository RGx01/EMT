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

And two v4 features it lacked:

  * the metric bar floats — sticky at the top of the scroll container (v4's #hm-nav),
    with v4's drop shadow;
  * weekends are shaded across every slot of the grid, not only behind the totals.
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


class V4Features(unittest.TestCase):
    """REGRESSION (30 Sep 2026): the bar and weekend tests fail on the tree before this change."""

    def _css(self, sel):
        m = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", _SRC)
        self.assertIsNotNone(m, sel)
        return m.group(1)

    def test_the_metric_bar_floats_with_a_shadow(self):
        css = self._css(".hm-bar")
        self.assertIn("position:sticky", css)
        self.assertIn("top:0", css)
        self.assertIn("box-shadow:0 2px 8px rgba(0,0,0,0.4)", css)     # v4's #hm-nav
        self.assertIn("background:", css)                             # opaque over the cells

    def test_the_bar_is_a_direct_child_of_the_scroller(self):
        """GUARD: sticky pins to the nearest scrolling ancestor; on the Charts page that is
        #chart-heatmap-content itself (measured), so the bar must not be wrapped."""
        self.assertIn("root.innerHTML = '<div class=\"hm-bar\">", _SRC)

    def test_weekends_are_shaded_across_the_grid(self):
        self.assertIn("type: 'matrix', plugins: [weekendRows(days, 'afterDatasetsDraw')],", _SRC)
        self.assertIn("plugins: [weekendRows(days, 'beforeDatasetsDraw')],", _SRC)


class ApiViewsRetired(unittest.TestCase):
    """14 Oct 2026, settlement surfaces design §9.2: the API import / API export views are retired;
    Import bill and Export bill carry what they showed. Discriminating."""

    def test_the_api_views_are_gone(self):
        for gone in ("API_METRICS", "apiColour", "API_PENDING_H", "isApi("):
            self.assertNotIn(gone, _SRC)

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
