"""
The heatmap's rows at their size (1 Nov 2026, the user's screenshot: Data Management with Issues
only, November's one row drawn as a thin strip with an empty band under it).

Chart.js takes room above and below a month's plot for its day labels — 16 px, measured in the
preview — out of the box the grid is drawn in, which was sized at rows × cell. A month of 31 rows
lost half a pixel a row; a month of one row (the 1st of a month, or Issues only leaving one) kept
4 px of its 20. `drawMonth` now measures what was taken and grows the box, the totals' with it. And
the month's name on its side, about 50 px of text, stretched a one-row month's panel past its row:
a month too short for it is named by its month, in full on hover.

Checked in the local preview on 1 Nov 2026: plot heights 18, 90 and 558 px for 1, 5 and 31 rows of
18 px cells; the one-row panel 34 px, labelled "Oct", its totals aligned.

Discriminating: every test fails on the parent tree.
"""
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(_ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8").read()


def _draw_month():
    i = PAGE.index("  function drawMonth(key) {")
    return PAGE[i:PAGE.index("\n  }\n", i) + 4]


class TheRows(unittest.TestCase):

    def test_the_box_grows_by_what_the_labels_took(self):
        f = _draw_month()
        self.assertIn("var lost = days.length * cs - p.chart.chartArea.height;", f)
        self.assertIn("row.querySelectorAll('.hm-grid, .hm-tot').forEach(function (el) { el.style.height = h + 'px'; });", f)
        self.assertIn("p.chart.resize();", f)

    def test_before_the_totals_are_drawn(self):
        f = _draw_month()
        self.assertLess(f.index("var lost = days.length * cs"), f.index("p.tot = new Chart("))


class TheMonthsName(unittest.TestCase):

    def test_a_short_month_by_its_month(self):
        f = _draw_month()
        self.assertIn("ml.textContent = h < 56 ? monthLabel(key).split(' ')[0] : monthLabel(key);", f)
        self.assertIn("ml.title = h < 56 ? monthLabel(key) : '';", f)


if __name__ == "__main__":
    unittest.main()
