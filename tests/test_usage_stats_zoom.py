"""
Usage Stats' zoom (22 Nov 2026, the user: "HA has a nice way to zoom the X axis from the bottom and top ends, could we do
the same in the manner I've sketched?"): a window on the bars in the Data table's header row, between its title and
the copy button; drag an end to zoom from that side, the middle to move along; the reset button, or a double-click,
shows every bar.

The bars are cut to the window where they are made (`barGetDataForPeriod` → `barZoomSlice`), so everything reading
them follows: the chart and its overlays, the separators, the tooltips, the table. Not on HH (its Net line is a running
total from midnight), Flow (one bar) or Running total. It goes back to whole when the bars change (another period,
span, resolution or order — not the metric). Mid-drag the full bars are worked out once (78 ms for three years of days
on a Mac, each frame): a frame then draws in 18 ms, the table once, when let go.

Driven in a browser on a copy of a real database: a three-year span by the month, its left end dragged (Dec 2024 to
Sep 2026, the table and its totals following), its right end, then moved along; reset by a resolution change; hidden on
Running total.

New: every test fails on the parent tree.
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


@unittest.skipUnless(shutil.which("node"), "node not available")
class ThePure(unittest.TestCase):

    def _js(self, expr):
        src = (PAGE[PAGE.index("var BAR_ZOOM_LISTS"):PAGE.index(";\n", PAGE.index("var BAR_ZOOM_LISTS")) + 2]
               + _fn("barZoomRange") + _fn("barZoomSlice") + _fn("barZoomMoved")
               + "\nconsole.log(JSON.stringify(%s));" % expr)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(src)
        try:
            r = subprocess.run(["node", fh.name], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(fh.name)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_whole_shows_every_bar(self):
        self.assertEqual(self._js("[barZoomRange(10,[0,1]), barZoomRange(2,[0.4,0.6])]"), [[0, 10], [0, 2]])

    def test_a_window_and_never_fewer_than_two_bars(self):
        self.assertEqual(self._js("[barZoomRange(10,[0.3,0.7]), barZoomRange(10,[0.5,0.52]), barZoomRange(10,[0.95,1])]"),
                         [[3, 7], [5, 7], [8, 10]])

    def test_every_per_bar_list_is_cut_and_the_rest_kept(self):
        pd = ("{labels:['a','b','c','d'], aggList:[1,2,3,4], bucketRanges:[1,2,3,4], groups:[1,1,2,2],"
              " majors:[1,1,2,2], bucketYears:[1,2,3,4], monthOrder:true}")
        got = self._js("barZoomSlice(%s, [0.5, 1])" % pd)
        self.assertEqual((got["labels"], got["aggList"], got["groups"], got["bucketYears"], got["monthOrder"]),
                         (["c", "d"], [3, 4], [2, 2], [3, 4], True))

    def test_hh_is_not_cut(self):
        self.assertEqual(self._js("barZoomSlice({labels:['a','b','c'], aggList:[1,2,3], isHH:true}, [0.5,1]).labels"),
                         ["a", "b", "c"])

    def test_an_end_dragged_keeps_the_least_width(self):
        self.assertEqual(self._js("[barZoomMoved([0,1],'a',0.3,0.1), barZoomMoved([0.3,1],'a',0.95,0.1),"
                                  " barZoomMoved([0,1],'b',-0.2,0.1)]"),
                         [[0.3, 1], [0.9, 1], [0, 0.1]])

    def test_the_middle_moves_the_window_inside_the_track(self):
        got = self._js("[barZoomMoved([0.2,0.5],'move',0.1,0.1), barZoomMoved([0.2,0.5],'move',0.9,0.1),"
                       " barZoomMoved([0.2,0.5],'move',-0.9,0.1)]")
        self.assertEqual([[round(a, 6), round(b, 6)] for a, b in got], [[0.3, 0.6], [0.7, 1.0], [0.0, 0.3]])


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheLegendInMonthOrder(unittest.TestCase):
    """22 Nov 2026, the user: "sometimes the legend colours are missing" — Rate brackets in month order: the year
    shading made each bracket's colour a list (a shade a bar), and the legend drew the list as a colour, so nothing;
    the hover highlight's fade expected one hsl() and broke on it. Fails on the parent tree."""

    def _js(self, expr):
        src = (_fn("barParseColor") + _fn("barBrFaded") + _fn("barDsColor")
               + "\nconsole.log(JSON.stringify(%s));" % expr)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(src)
        try:
            r = subprocess.run(["node", fh.name], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(fh.name)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_a_shaded_dataset_keeps_its_own_colour_for_the_legend(self):
        self.assertEqual(self._js("[barDsColor({_baseColor:'hsl(120,60%,45%)', backgroundColor:['rgba(1,2,3,1)']}),"
                                  " barDsColor({backgroundColor:'hsl(1,2%,3%)'}),"
                                  " barDsColor({backgroundColor:['rgba(9,9,9,1)','rgba(1,1,1,1)']})]"),
                         ["hsl(120,60%,45%)", "hsl(1,2%,3%)", "rgba(1,1,1,1)"])

    def test_the_fade_takes_a_shaded_list(self):
        self.assertEqual(self._js("[barBrFaded('hsl(120, 60%, 45%)'), barBrFaded(['rgba(10,20,30,1)', '#ffffff'])]"),
                         ["hsla(120, 60%, 45%, 0.18)", ["rgba(10,20,30,0.18)", "rgba(255,255,255,0.18)"]])

    def test_the_legends_read_it(self):
        self.assertIn("_ds.forEach(function(d) { el.innerHTML += dot(barDsColor(d), d.label); });", _fn("barBuildLegend"))
        self.assertIn("(byN[n] ? barDsColor(byN[n]) : barBrColor(n, ns, side || undefined, !!side) + ';opacity:.25')", _fn("barBrScale"))
        self.assertIn("if (typeof base === 'string') ds._baseColor = base;", _fn("barApplyYearGradient"))


class TheWiring(unittest.TestCase):

    def test_it_sits_in_the_table_header_between_title_and_copy(self):
        i, j = PAGE.index('>Data table</span>'), PAGE.index('id="bar-copy-btn"')
        self.assertIn('<div id="bar-zoom" class="bar-zoom" style="display:none;">', PAGE[i:j])

    def test_everything_reads_the_cut_bars(self):
        f = _fn("barGetDataForPeriod")
        self.assertIn("var pd = barZoomFull || barGetDataForPeriodAll();", f)
        self.assertIn("return barZoomSlice(pd, barZoom);", f)

    def test_shown_on_the_bar_views_only(self):
        self.assertIn("var on = !barRunning() && !barFlow() && barPeriod !== 'hh' && barZoomN >= 3;", _fn("barZoomSync"))
        self.assertIn("barZoomSync();                                      // hidden on Running total, Flow and HH",
                      _fn("barRender"))

    def test_whole_again_when_the_bars_change(self):
        f = _fn("barZoomSeen")
        self.assertIn("var key = barPeriod + '|' + l.length + '|' + l[0] + '|' + l[l.length - 1];", f)
        self.assertIn("if (key !== barZoomKey) { barZoomKey = key; barZoom = [0, 1]; }", f)

    def test_a_drag_draws_without_the_table_or_animation(self):
        r = _fn("barRender")
        self.assertIn("if (!barZoomDragging) barBuildTable();", r)
        self.assertIn("animation: barZoomDragging ? false : undefined,", r)


if __name__ == "__main__":
    unittest.main()
