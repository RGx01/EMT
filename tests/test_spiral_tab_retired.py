"""
BL-104 PR 5 (1 Nov 2026, accumulation design §3) — the Spiral tab retired (the user).

The Running total view has what the tab had (PRs 1–4): the spiral, its winding, the years, the
standing charge in net, the expand view and the PDF. So the tab goes — its button, its markup and
styles, `EMTSpiral`, its PDF branch and its refresh — and `EMTDayRows`, which Usage Stats shares,
stays. A remembered Spiral tab, or `?tab=spiral`, opens Usage Stats on the Running total's spiral,
and the tab's saved view (grouping, winding, hidden years, the standing charge) is carried over
once and its record removed. `tests/test_spiral_client.py` went with the module it tested.

Checked in the local preview on 1 Nov 2026: a remembered Spiral tab with a saved Import / Year-aligned
view and 2023 hidden opened Usage Stats on Running total, spiral, Import, Year by year, 2024–2026,
the record removed and the remembered tab now Usage Stats; `?tab=spiral` the same; the three tabs
switched with no script error.

Discriminating: every test fails on the parent tree but the one labelled GUARD.
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
    return PAGE[i:PAGE.index("\n}\n", i) + 2]


class TheTabIsGone(unittest.TestCase):

    def test_no_button_markup_or_module(self):
        self.assertNotIn('data-chart="spiral"', PAGE)
        self.assertNotIn('id="chart-spiral"', PAGE)
        self.assertNotIn("window.EMTSpiral", PAGE)
        self.assertNotIn("_activeChart === 'spiral'", PAGE)

    def test_GUARD_the_day_rows_stay(self):
        """GUARD: true before too; Usage Stats shares them."""
        self.assertIn("window.EMTDayRows = (function () {", PAGE)

    def test_its_test_went_with_it(self):
        self.assertFalse(os.path.exists(os.path.join(_ROOT, "tests", "test_spiral_client.py")))


class TheWayIn(unittest.TestCase):

    def test_a_remembered_tab_or_the_link_opens_the_running_totals_spiral(self):
        self.assertIn("if (saved === 'spiral') { _barFromSpiral = true; saved = 'bar'; }", PAGE)
        self.assertIn("else if (saved === 'bar') { showChart('bar', tabs[2]); }", PAGE)

    def test_carried_over_on_both_load_paths(self):
        f = _fn("barLoadState")
        self.assertIn("if (!raw) { barShowFromUrl(); barFromSpiral(); return; }", f)
        self.assertIn("  barShowFromUrl();\n  barFromSpiral();\n}", f)


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheCarryOver(unittest.TestCase):

    def _run(self, saved, flag=True):
        js = """
var store = {'k:emt_spiral_state': %s};
var localStorage = {getItem: function (k) { return k in store ? store[k] : null; },
                    removeItem: function (k) { delete store[k]; }};
var EMT = {lsKey: function (k) { return 'k:' + k; }};
var barShow = 'devices', barAccView = 'line', barAccSeries = 'net', barAccMode = 'lifetime',
    barAccIncStanding = false, barActiveYears = new Set([2023, 2024, 2025]), saved = 0;
function barGetYears() { return [2023, 2024, 2025]; }
function barSaveState() { saved++; }
var _barFromSpiral = %s;
%s
barFromSpiral();
console.log(JSON.stringify({show: barShow, view: barAccView, series: barAccSeries, mode: barAccMode,
  inc: barAccIncStanding, years: Array.from(barActiveYears), left: Object.keys(store), saved: saved,
  again: _barFromSpiral}));
""" % (json.dumps(json.dumps(saved)) if saved is not None else "null", "true" if flag else "false", _fn("barFromSpiral"))
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_the_saved_view_carried_into_the_running_totals_spiral(self):
        out = self._run({"grouping": "import", "mode": "year", "hidden": [2023], "incStanding": True})
        self.assertEqual((out["show"], out["view"], out["series"], out["mode"], out["inc"]),
                         ("running", "spiral", "import", "year", True))
        self.assertEqual(out["years"], [2024, 2025])
        self.assertEqual(out["left"], [])                     # the record removed
        self.assertEqual((out["saved"], out["again"]), (1, False))

    def test_with_no_saved_view_the_spiral_on_its_defaults(self):
        out = self._run(None)
        self.assertEqual((out["show"], out["view"], out["series"], out["mode"]), ("running", "spiral", "net", "lifetime"))

    def test_only_when_the_page_came_from_the_tab(self):
        out = self._run({"grouping": "import"}, flag=False)
        self.assertEqual((out["show"], out["series"], out["saved"]), ("devices", "net", 0))


if __name__ == "__main__":
    unittest.main()
