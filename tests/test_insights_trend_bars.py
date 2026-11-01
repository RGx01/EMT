"""
BL-96 (1 Nov 2026) — Insights Carbon's net carbon per billing period strip.

The user asked what selecting a bar was for. It was meant to go to that period, and did not: the
click set `_periodIdx`, which `loadPeriod` never reads (it loads `_calYear` / `_calMonth`), so it
moved the highlight and reloaded the month in view; and the highlight started on index 0, the oldest
bar, whatever month was shown.

A bar is a billing period; the page shows a calendar month or year. A bar now stands for the month
(Year: the year) holding its middle, for any billing day: that is the bar highlighted, and the one a
click — or Enter on a focused bar — goes to. The strip says so beside its title. Its labels are
thinned to fit, counted back from the newest (three years ran into one another on a phone), and the
newest ends at its bar. The period is saved after it changes: the arrows saved it before, so their
last step was never kept.

Checked in the local preview on 1 Nov 2026, on a scratch copy of staging (billing day 3): October
2026 opens with its own bar highlighted; a click on February 2025's goes to February 2025, Enter on
November 2023's to November 2023; in Year, a 2026 bar to 2026 with its ten bars highlighted.

Discriminating: every test fails on the parent tree.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(_ROOT, "web", "templates", "insights.html"), encoding="utf-8").read()


def _fn(name):
    i = PAGE.index("function " + name + "(")
    return PAGE[i:PAGE.index("\n}\n", i) + 2]


HARNESS = r"""
var _insMode, _calYear, _calMonth, saved = [], loads = 0;
function _insSaveState() { saved.push([_insMode, _calYear, _calMonth]); }
function updatePeriodNav() {}
function loadPeriod() { loads++; }
// billing day 25: a period's start month is not the month it mostly covers
var _periods = [
  {period_start: '2025-01-25', period_end: '2025-02-25'},
  {period_start: '2025-02-25', period_end: '2025-03-25'},
  {period_start: '2025-12-25', period_end: '2026-01-25'}];
__FUNCS__
var out = {};
_insMode = 'month'; _calYear = 2025; _calMonth = 2;
out.inView = _periods.map(_trendInView);
_trendNavTo(1);
out.afterClick = [_calYear, _calMonth, loads, saved.slice(-1)[0]];
_trendNavTo(1);                                   // the one in view: nothing
out.again = loads;
_insMode = 'year'; _calYear = 2025;
out.yearInView = _periods.map(_trendInView);
_trendNavTo(2);
out.yearClick = [_calYear, _calMonth];
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node not available")
class ABarGoesToItsMonth(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        js = HARNESS.replace("__FUNCS__", "\n".join(_fn(n) for n in ("_trendMid", "_trendInView", "_trendNavTo")))
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(path)
        assert res.returncode == 0, res.stderr
        cls.out = json.loads(res.stdout)

    def test_the_bar_in_view_is_the_month_holding_its_middle(self):
        # 25 Jan – 25 Feb is February's; 25 Feb – 25 Mar is March's
        self.assertEqual(self.out["inView"], [True, False, False])

    def test_a_click_goes_to_its_month_and_keeps_it(self):
        self.assertEqual(self.out["afterClick"], [2025, 3, 1, ["month", 2025, 3]])

    def test_the_bar_in_view_does_nothing(self):
        self.assertEqual(self.out["again"], 1)

    def test_in_year_its_year(self):
        self.assertEqual(self.out["yearInView"], [True, True, False])   # 25 Dec – 25 Jan is January's
        self.assertEqual(self.out["yearClick"][0], 2026)


class TheStrip(unittest.TestCase):

    def test_no_index_loadPeriod_never_reads(self):
        self.assertNotIn("_periodIdx", PAGE)

    def test_the_highlight_is_the_period_in_view(self):
        self.assertIn("var isCurrent = _trendInView(_periods[i]);", _fn("renderTrend"))

    def test_it_says_a_bar_can_be_clicked(self):
        self.assertIn('<span id="trend-hint"', PAGE)
        self.assertIn("' \\u00b7 click a bar to view its ' + (_insMode === 'month' ? 'month' : 'year')", _fn("renderTrend"))

    def test_a_bar_to_click_is_a_button(self):
        r = _fn("renderTrend")
        self.assertIn("tabindex=\"0\" role=\"button\" data-i=\"' + i +", r)
        self.assertIn("if (b && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); _trendNavTo(+b.getAttribute('data-i')); }", r)

    def test_the_labels_fit(self):
        r = _fn("renderTrend")
        self.assertIn("var every = Math.max(1, Math.ceil(n * 40 / chartW));", r)
        self.assertIn("if ((n - 1 - i) % every === 0) {", r)
        self.assertIn("'\" text-anchor=\"' + (last ? 'end' : 'middle') + '\"", r)

    def test_the_arrows_save_after_their_step(self):
        f = _fn("navPeriod")
        self.assertLess(f.index("_calYear += dir;"), f.index("_insSaveState();"))


if __name__ == "__main__":
    unittest.main()
