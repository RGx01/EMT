"""
Usage Stats: the split between the chart and the data table, flexible (31 Oct 2026).

The chart's height was fixed (300 px, 240 on a phone), the table taking the rest. A handle between
them now sets it.

What they pin:

- **The handle:** a separator, focusable, dragged with the pointer (mouse or touch); the chart's
  height follows it, between 160 px and what leaves the table 240 px (on a phone, where the page
  scrolls, 80% of the screen); the chart is redrawn to fit.
- **Kept in this browser**, per instance; **double-click resets** to the default; **arrow keys**
  move it 20 px.
- **While the tab is hidden** the saved height is restored unclamped (there is nothing to measure),
  and the phone's default is no longer `!important`, so a dragged height applies there too.

Checked in the local preview on 31 Oct 2026, at 1400×900 and 375×812: dragged down 150 px to 450,
to the maximum (the table kept 178 px) and to the minimum (160); an arrow key +20; double-click back
to 300 (240 on the phone); the height restored after a reload; no sideways scroll.

Structural (no browser here). Discriminating: every test fails on the parent.
"""
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(_ROOT, "web", "templates", "charts.html"), encoding="utf-8").read()


def _fn(name):
    i = PAGE.index("function " + name + "(")
    return PAGE[i:PAGE.index("\n}\n", i) + 2]


class TheHandle(unittest.TestCase):

    def test_it_sits_between_the_chart_and_the_table(self):
        a, b, c = (PAGE.index('id="bar-chart-wrap"'), PAGE.index('id="bar-split"'),
                   PAGE.index('id="bar-table-wrap"'))
        self.assertLess(a, b); self.assertLess(b, c)
        self.assertIn('role="separator" aria-orientation="horizontal" tabindex="0"', PAGE)

    def test_dragging_sets_the_charts_height_within_bounds(self):
        init = _fn("barInitSplit")
        self.assertIn("barSetChartHeight(h0 + e.clientY - y0, false);", init)
        self.assertIn("bar.setPointerCapture(e.pointerId);", init)
        setter = _fn("barSetChartHeight")
        self.assertIn("h = Math.round(Math.max(BAR_SPLIT_MIN, Math.min(barSplitMax(), h)));", setter)
        self.assertIn("if (barChartInstance) barChartInstance.resize();", setter)
        self.assertIn("var BAR_SPLIT_MIN = 160;", PAGE)
        self.assertIn("var BAR_SPLIT_TABLE = 240;", PAGE)

    def test_the_bounds_on_a_phone_and_while_hidden(self):
        mx = _fn("barSplitMax")
        self.assertIn("return Math.round(window.innerHeight * 0.8);", mx)
        self.assertIn("if (!box.getBoundingClientRect().height) return Infinity;", mx)

    def test_kept_reset_and_keys(self):
        init = _fn("barInitSplit")
        self.assertIn("if (saved > 0) barSetChartHeight(saved, false);", init)
        self.assertIn("bar.addEventListener('dblclick', barResetChartHeight);", init)
        self.assertIn("(e.key === 'ArrowDown' ? 20 : -20)", init)
        self.assertIn("localStorage.removeItem(barSplitKey())", _fn("barResetChartHeight"))
        self.assertIn("EMT.lsKey('emt_bar_chart_h')", _fn("barSplitKey"))

    def test_the_default_height_is_css_so_a_dragged_one_wins(self):
        self.assertIn("#bar-chart-wrap { height: 300px; }", PAGE)
        self.assertIn("#bar-chart-wrap { height: 240px; }", PAGE)
        self.assertNotIn("#bar-chart-wrap { height: 240px !important; }", PAGE)
        self.assertIn("barInitSplit();", _fn("barInit"))


if __name__ == "__main__":
    unittest.main()
