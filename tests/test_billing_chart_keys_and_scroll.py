"""
The Billing tab's day charts: the keys of the legend and the hover, and the reader's place across a refresh
(6 Nov 2026, the user).

The keys: Chart.js drew a rate's key as a box, in the fill a line dataset does not have, and the hover
outlined every key in white, so a rate and a bar looked alike. `keyMark` draws one key for both: a line for a
rate (dashed where its line is), a plain block for a bar.

The place: `refresh` (the data moved, often as the page comes back into view) replaces the whole section. It
read the position before its fetch and put that back after, so scrolling in between jumped back; and the new
section had every data table shut, so the page shortened and the position clamped. On a real database: at
2600 of 5544 with three tables open, scrolled to 3400 during the fetch, it ended at 1424 of 2082 with none
open. `keepView` now reads the position at the swap, and `restoreView` holds each day's height until
`fillDay` fills it, its table open again.

Discriminating: every test here fails on the parent (the functions are new, or the source said otherwise).
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(_ROOT, "web", "templates", "billing_module.html"), encoding="utf-8").read()


def _fn(name):
    """A function's whole text from the module, by matching its braces."""
    i = SRC.index("function %s(" % name)
    depth, j = 0, SRC.index("{", i)
    while True:
        if SRC[j] == "{":
            depth += 1
        elif SRC[j] == "}":
            depth -= 1
            if depth == 0:
                return SRC[i:j + 1]
        j += 1


HARNESS = """
function el(o) { return Object.assign({style: {}}, o); }
function wrap(day, h, table) {
  var t = el({style: {display: table ? 'flex' : 'none'}});
  return el({dataset: {day: day}, offsetHeight: h, querySelector: function () { return t; }});
}
function section(wraps, billOpen) {
  var bill = {open: billOpen};
  return {_bill: bill, querySelector: function () { return bill; },
          querySelectorAll: function () { return wraps; }};
}
var state = {root: {scrollTop: 0}};
var drawn = [];
var document = {createElement: function () {
  return {getContext: function () {
    return {setLineDash: function (d) { drawn.push(['dash', d]); }, beginPath: function () {},
            moveTo: function () {}, lineTo: function () {}, stroke: function () { drawn.push(['line', this.strokeStyle]); },
            fillRect: function () { drawn.push(['block', this.fillStyle]); }};
  }};
}};
var KEY_W = 22, KEY_H = 12, keyMarks = {};
%s
%s
%s
%s
"""


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheFunctions(unittest.TestCase):

    def _run(self, body):
        src = HARNESS % (_fn("keepView"), _fn("restoreView"), _fn("keyMark"), body)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.js")
            with open(p, "w", encoding="utf-8") as f:
                f.write(src)
            res = subprocess.run(["node", p], capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_the_place_is_read_at_the_swap(self):
        out = self._run("""
          var w = [wrap('d1', 351, true), wrap('d2', 350, false)];
          state.cur = {mode: 'month', start: 'a'}; state.section = section(w, false);
          state.root.scrollTop = 3400;
          console.log(JSON.stringify(keepView('month', {start: 'a'})));""")
        self.assertEqual(out, {"y": 3400, "bill": False, "days": {"d1": {"h": 351, "table": True},
                                                                  "d2": {"h": 350, "table": False}}})

    def test_another_period_keeps_nothing(self):
        out = self._run("""
          state.cur = {mode: 'month', start: 'a'}; state.section = section([], true);
          console.log(JSON.stringify([keepView('month', {start: 'b'}), keepView('year', {start: 'a'})]));""")
        self.assertEqual(out, [None, None])

    def test_each_day_holds_its_height_and_table_and_the_place_returns(self):
        out = self._run("""
          var w = [wrap('d1', 0, false), wrap('d2', 0, false), wrap('new', 0, false)];
          var s = section(w, true);
          restoreView(s, {y: 3400, bill: false, days: {d1: {h: 351, table: true}, d2: {h: 0, table: false}}});
          console.log(JSON.stringify({top: state.root.scrollTop, bill: s._bill.open,
            h: w.map(function (x) { return x.style.minHeight || null; }),
            t: w.map(function (x) { return x._keepTable === undefined ? null : x._keepTable; })}));""")
        self.assertEqual(out, {"top": 3400, "bill": False, "h": ["351px", None, None], "t": [True, False, None]})

    def test_nothing_kept_changes_nothing(self):
        out = self._run("""
          state.root.scrollTop = 7; restoreView(section([], true), null);
          console.log(JSON.stringify(state.root.scrollTop));""")
        self.assertEqual(out, 7)

    def test_a_rate_is_a_line_dashed_as_its_line_and_a_bar_a_plain_block(self):
        out = self._run("""
          keyMark({_rate: true, borderColor: '#f00', borderDash: [6, 4]});
          keyMark({backgroundColor: '#0f0'});
          console.log(JSON.stringify(drawn));""")
        self.assertEqual(out, [["dash", [6, 4]], ["line", "#f00"], ["block", "#0f0"]])

    def test_a_key_is_drawn_once(self):
        out = self._run("""
          var a = keyMark({_rate: true, borderColor: '#f00'}), b = keyMark({_rate: true, borderColor: '#f00'});
          var c = keyMark({_rate: true, borderColor: '#f00', borderDash: [6, 4]});
          console.log(JSON.stringify([a === b, a === c]));""")
        self.assertEqual(out, [True, False])


class TheWiring(unittest.TestCase):

    def test_the_legend_and_the_hover_use_the_key(self):
        chart = _fn("buildChart")
        self.assertIn("usePointStyle: true, pointStyleWidth: KEY_W", chart)
        self.assertIn("it.pointStyle = keyMark(ch.data.datasets[it.datasetIndex])", chart)
        self.assertIn("tooltip: {usePointStyle: true, boxWidth: KEY_W, boxHeight: KEY_H, "
                      "multiKeyBackground: 'transparent'", chart)
        self.assertIn("labelPointStyle: function (it) { return {pointStyle: keyMark(it.dataset)", chart)

    def test_the_place_is_read_after_the_fetch_and_put_back_after_the_mount(self):
        show = _fn("show")
        self.assertLess(show.index("Promise.all("), show.index("keep ? keepView(mode, p)"))
        self.assertLess(show.index("keep ? keepView(mode, p)"), show.index("mount(sec)"))
        self.assertLess(show.index("mount(sec)"), show.index("restoreView(sec, kept)"))

    def test_a_refresh_keeps_and_nothing_else_does(self):
        self.assertIn("return show(true);", _fn("refresh"))
        self.assertNotIn("scrollTop", _fn("refresh"))
        self.assertEqual(SRC.count("show(true)"), 1)

    def test_a_filled_day_reopens_its_table_and_lets_go_of_its_height(self):
        fill = _fn("fillDay")
        self.assertIn("state.showData || wrap._keepTable", fill)
        self.assertIn("wrap.style.minHeight = ''", fill)


if __name__ == "__main__":
    unittest.main()
