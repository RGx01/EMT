"""
BL-101 PR 2 (1 Nov 2026, home eras design §3.1) — the home's eras marked on Usage Stats.

Markers, not a lane (the user): eras overlap. A dashed line at each era's start across the bars, a
chip with its icon at the top — under the import tariff band when that is shown — and a fainter line
at its end. Chips that would touch step down a row. Where a bar is a stretch of days (Monthly by
Date, Daily); not on HH, where the bars are one day. An *Eras* checkbox beside *Tariffs*, on until
turned off, saved with the view. A chip's hover shows the era (the tariffs' tip, shared); the era
plugin goes first, so a chip over the tariff band is the era's. Saving in the eras modal redraws.

Checked in the local preview on 1 Nov 2026, on a scratch copy of staging, three eras posted and
cleared after: Monthly, the battery and EV chips a row apart, the insulation's start and end; a
chip's hover; the tariff band's hover still its own; Daily with the checkbox, HH without.

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


def _fn(name):
    i = PAGE.index("function " + name + "(")
    line = PAGE[i:PAGE.index("\n", i)]
    if line.count("{") == line.count("}"):
        return line + "\n"
    return PAGE[i:PAGE.index("\n}\n", i) + 2]


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheMarks(unittest.TestCase):

    def _marks(self, eras, lanes):
        # twelve monthly bars, 30 px apart; the chart area from 40 px, 400 px down
        ranges = [{"from": "2025-%02d-01" % m, "to": ("2025-%02d-01" % (m + 1)) if m < 12 else "2026-01-01"}
                  for m in range(1, 13)]
        js = "\n".join(_fn(n) for n in ("barDayNo", "barTariffX", "barEraMarks")) + """
var BAR_TARIFF_LANE = 14, BAR_ERA_CHIP = 16, barEras = %s;
var chart = {chartArea: {top: 40, bottom: 440, left: 0, right: 360},
             scales: {x: {getPixelForValue: function (i) { return 15 + 30 * i; }, left: 0, right: 360}}};
console.log(JSON.stringify(barEraMarks(chart, %s, %s).map(function (m) {
  return [m.e.label, m.x == null ? null : Math.round(m.x), m.end == null ? null : Math.round(m.end), m.row, m.cy]; })));
""" % (json.dumps(eras), json.dumps(ranges), json.dumps(lanes))
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_a_start_within_its_bar_by_its_day(self):
        out = self._marks([{"label": "Solar", "kind": "solar", "start": "2025-03-16"}], None)
        self.assertEqual(out[0][:3], ["Solar", 75, None])           # March's slot 60–90, half way

    def test_an_end_in_view_and_a_start_before_it(self):
        out = self._marks([{"label": "Old", "kind": "other", "start": "2020-01-01", "end": "2025-07-01"}], None)
        self.assertEqual(out[0][1:3], [None, 180])

    def test_chips_that_would_touch_step_down(self):
        out = self._marks([{"label": "A", "kind": "battery", "start": "2025-06-10"},
                           {"label": "B", "kind": "ev", "start": "2025-06-15"}], None)
        self.assertEqual([o[3] for o in out], [0, 1])
        self.assertEqual(out[1][4] - out[0][4], 18)

    def test_under_the_import_band(self):
        plain = self._marks([{"label": "A", "kind": "solar", "start": "2025-06-10"}], None)[0][4]
        banded = self._marks([{"label": "A", "kind": "solar", "start": "2025-06-10"}], {"imp": True})[0][4]
        self.assertEqual(banded - plain, 8 + 14 + 4)

    def test_out_of_view_none(self):
        self.assertEqual(self._marks([{"label": "A", "kind": "solar", "start": "2023-01-01"}], None), [])


class TheView(unittest.TestCase):

    def test_where_a_bar_is_days_not_hh(self):
        f = _fn("barErasAvailable")
        self.assertIn("(barPeriod === 'monthly' && barMonthOrder === 'date') || barPeriod === 'daily'", f)
        self.assertNotIn("'hh'", f)

    def test_the_checkbox_on_until_turned_off_and_saved(self):
        self.assertIn('<input type="checkbox" id="bar-eras-cb"', PAGE)
        self.assertIn("if (ec) ec.checked = st.eras !== false;", _fn("barLoadState"))
        self.assertIn("eras:     !!(document.getElementById('bar-eras-cb') || {}).checked,", _fn("barSaveState"))

    def test_the_eras_plugin_first(self):
        self.assertIn("plugins: (showEras ? [barErasPlugin(pd.bucketRanges, tariffLanes)] : [])\n"
                      "               .concat(tariffLanes ? [barTariffPlugin(pd.bucketRanges, tariffLanes)] : []),", PAGE)

    def test_a_chip_s_hover_is_the_era(self):
        i = PAGE.index("function barErasPlugin(")
        self.assertIn("barEraShow(hit.e, e.native);", PAGE[i:PAGE.index("\nfunction ", i + 10)])

    def test_a_save_in_the_modal_redraws(self):
        self.assertIn("barEras = _eras; if (barInited && barData) barRender();", _fn("_erasPost"))

    def test_in_the_pdf_settings(self):
        self.assertIn("['bar-eras-wrap', 'bar-eras-cb', 'Eras']", _fn("barPdfSettings"))


if __name__ == "__main__":
    unittest.main()
