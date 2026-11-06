"""
Rate brackets' legend becomes a scale past a dozen brackets (6 Nov 2026, the user: an Agile year's 38 keys took four
rows above the chart).

`barBrScale` draws one segment per 2.5p from the cheapest bracket to the dearest, in the bars' own colours, with a
bracket nothing fell in faded so the scale stays even. Hovering a segment names it and picks it out in the bars.
A view with a dozen brackets or fewer keeps its named keys.

Discriminating: every test here fails on the parent (the functions are new, or the legend listed every bracket),
except the one marked GUARD. The page was also driven by hand in a browser.
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
    i = PAGE.index("function %s(" % name)
    depth, j = 0, PAGE.index("{", i)
    while True:
        depth += {"{": 1, "}": -1}.get(PAGE[j], 0)
        if PAGE[j] == "}" and depth == 0:
            return PAGE[i:j + 1]
        j += 1


HARNESS = """
var barBrWidth = 2.5, barChartInstance = null, updates = 0;
function barBrColor(n) { return 'hsl(' + n + ', 50%, 50%)'; }
function barShowNet() { return false; }
function barBrackets() { return true; }
function ds(ns) { return ns.map(function (n) { return {_bracket: n, label: 'L' + n, backgroundColor: 'hsl(' + n + ', 62%, 48%)'}; }); }
function range(a, b) { var o = []; for (var i = a; i <= b; i++) o.push(i); return o; }
var listeners = {}, read = {textContent: ''}, strip = {addEventListener: function (k, f) { listeners[k] = f; }};
var ticks = {children: []};
var legend = {innerHTML: '', querySelector: function (q) { return q === '.bar-br-strip' ? strip : q === '.bar-br-ticks' ? ticks : read; }};
var document = {getElementById: function () { return legend; }}, window = {};
function chart(sets) { barChartInstance = {data: {datasets: sets}, update: function () { updates += 1; }}; }
"""


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheScale(unittest.TestCase):

    def _run(self, body):
        names = ("barBrPence", "barBrLabel", "barBrScaleTicks", "barBrTicksHidden", "barBrFaded", "barBrHighlight", "barBrScale", "barBuildLegend")
        src = (HARNESS + "var BAR_BR_KEYS_MAX = 12;\n" + "\n".join(_fn(n) for n in names) + "\n" + body)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.js")
            with open(p, "w", encoding="utf-8") as f:
                f.write(src)
            r = subprocess.run(["node", p], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_the_labels_are_both_ends_and_round_prices(self):
        self.assertEqual(self._run("console.log(JSON.stringify(barBrScaleTicks(-12.5, 92.5)));"),
                         [-12.5, 0, 25, 50, 75, 92.5])

    def test_a_round_price_too_close_to_an_end_is_left_out(self):
        self.assertEqual(self._run("console.log(JSON.stringify(barBrScaleTicks(-2.5, 92.5)));"),
                         [-2.5, 25, 50, 75, 92.5])

    def _hidden(self, boxes):
        return self._run("console.log(JSON.stringify(barBrTicksHidden(%s, 4)));" % json.dumps(boxes))

    def test_a_label_that_would_touch_the_first_is_hidden(self):
        # a phone's strip: "−12.5p" ran into "0p"
        self.assertEqual(self._hidden([{"left": 0, "right": 35}, {"left": 31, "right": 45}, {"left": 90, "right": 110},
                                       {"left": 300, "right": 330}]), [False, True, False, False])

    def test_a_label_that_would_touch_the_last_is_hidden_and_the_ends_stay(self):
        self.assertEqual(self._hidden([{"left": 0, "right": 35}, {"left": 90, "right": 110}, {"left": 199, "right": 219},
                                       {"left": 216, "right": 246}]), [False, False, True, False])

    def test_GUARD_labels_with_room_all_show(self):
        self.assertEqual(self._hidden([{"left": 0, "right": 35}, {"left": 90, "right": 110}, {"left": 200, "right": 220},
                                       {"left": 300, "right": 330}]), [False, False, False, False])

    def test_a_dozen_brackets_keep_their_keys(self):
        out = self._run("""chart(ds(range(0, 11))); barBuildLegend();
          console.log(JSON.stringify([legend.innerHTML.indexOf('bar-br-strip'), legend.innerHTML.indexOf('L11') > -1]));""")
        self.assertEqual(out, [-1, True])

    def test_past_a_dozen_it_is_one_strip_with_a_segment_each(self):
        out = self._run("""chart(ds(range(0, 12))); barBuildLegend();
          console.log(JSON.stringify([legend.innerHTML.indexOf('bar-br-strip') > -1,
                                      legend.innerHTML.indexOf('L0') > -1,
                                      legend.innerHTML.split('data-n=').length - 1]));""")
        self.assertEqual(out, [True, False, 13])

    def test_a_used_segment_wears_the_bars_colour_and_a_gap_is_faded(self):
        out = self._run("""chart(ds(range(0, 12).concat([14]))); barBuildLegend();
          var h = legend.innerHTML;
          function seg(n) { var i = h.indexOf('data-n="' + n + '"'); return h.slice(i, h.indexOf('</span>', i)); }
          console.log(JSON.stringify([seg(3).indexOf('background:hsl(3, 62%, 48%)') > -1, seg(3).indexOf('opacity') > -1,
                                      seg(13).indexOf('opacity:.25') > -1, h.split('data-n=').length - 1]));""")
        self.assertEqual(out, [True, False, True, 15])

    def test_hovering_names_the_bracket_and_fades_the_others_and_leaving_restores(self):
        out = self._run("""var sets = ds(range(0, 12)); chart(sets); barBuildLegend();
          listeners.mouseover({target: {dataset: {n: '2'}}});
          var on = [read.textContent, sets[2].backgroundColor, sets[3].backgroundColor];
          listeners.mouseleave();
          console.log(JSON.stringify({on: on, rest: read.textContent, back: sets[3].backgroundColor, updates: updates}));""")
        self.assertEqual(out["on"], ["5–7.5p", "hsl(2, 62%, 48%)", "hsla(3, 62%, 48%, 0.18)"])
        self.assertEqual((out["rest"], out["back"], out["updates"]), ("Rate, p/kWh", "hsl(3, 62%, 48%)", 2))

    def test_hovering_a_gap_says_so_and_fades_nothing(self):
        out = self._run("""var sets = ds(range(0, 12).concat([14])); chart(sets); barBuildLegend();
          listeners.mouseover({target: {dataset: {n: '13'}}});
          console.log(JSON.stringify([read.textContent, sets[3].backgroundColor]));""")
        self.assertEqual(out, ["32.5–35p: none", "hsl(3, 62%, 48%)"])

    def test_GUARD_hovering_the_strip_itself_changes_nothing(self):
        out = self._run("""var sets = ds(range(0, 12)); chart(sets); barBuildLegend();
          read.textContent = 'as it was';
          listeners.mouseover({target: {dataset: {}}});
          console.log(JSON.stringify([read.textContent, updates]));""")
        self.assertEqual(out, ["as it was", 0])


class TheBars(unittest.TestCase):

    def test_the_faded_colour_is_drawn(self):
        # Chart.js 4.4.1: update('none') keeps a bar's shared options, so the dataset changed and the bars did not.
        # Seen on the drawn bars in a browser; a stub cannot tell the modes apart, so this pins the mode.
        self.assertIn("ch.update('active');", _fn("barBrHighlight"))


class ThePdf(unittest.TestCase):

    def test_it_prints_its_colours(self):
        # the PDF copies the legend's markup into its own page, where the browser leaves backgrounds out by default
        self.assertIn("print-color-adjust:exact", _fn("barBrScale"))


if __name__ == "__main__":
    unittest.main()
