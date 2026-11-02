"""
Usage Stats' toolbar (1 Nov 2026, the user: "Devices, Rate Brackets and Running Totals are the primary
options … Running totals is almost a good layout but still a bit clumsy with the placement of eras
check box. checking the eras box is a bit laggy too").

- **The view first.** The toolbar opens with Devices | Rate brackets | Running total, then a rule, then
  only that view's own settings; its selects are as wide as their text (the page's selects are full
  width, which stacked Running total's settings four rows deep).
- **The toggles at the Years row's end** — Net, the standing charge (both views'), Tariffs, Eras and the
  sort — where the row was empty. Running total's own standing charge box is hidden with its view.
- **The lag.** Every row's year and day asked for the year's start, and each ask made the choices again,
  sorting the whole history: a Running total redraw of ~1,240 days took 1.0 s as a line and 2.0 s as a
  spiral in the preview. The start is kept until what it is made from changes: 12–47 ms.

Checked in the local preview on 1 Nov 2026 (a scratch copy of staging): each view one 42 px toolbar row
and a 28 px Years row at 1,600 px; on a phone the toolbar wraps, with no sideways scroll.

Discriminating: every test fails on the parent tree.
"""
import json
import os
import re
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


def _block(start, end):
    i = PAGE.index(start)
    return PAGE[i:PAGE.index(end, i)]


class TheLayout(unittest.TestCase):

    def setUp(self):
        self.toolbar = _block('<div class="bar-toolbar"', '<div id="bar-sub-row"')
        self.toggles = _block('<div id="bar-toggles"', "<!-- Legend -->")

    def test_the_view_comes_first(self):
        ids = re.findall(r'id="(bar-[a-z-]+-seg|bar-acc-wrap|bar-br-wrap)"', self.toolbar)
        self.assertEqual(ids[0], "bar-show-seg")
        self.assertLess(self.toolbar.index('id="bar-show-seg"'), self.toolbar.index('class="bar-tb-sep"'))
        self.assertLess(self.toolbar.index('class="bar-tb-sep"'), self.toolbar.index('id="bar-period-seg"'))

    def test_the_toggles_are_at_the_years_rows_end(self):
        for el in ("bar-standing-wrap", "bar-acc-standing-wrap", "bar-net-wrap", "bar-tariff-wrap",
                   "bar-eras-wrap", "bar-sort-btn"):
            self.assertIn('id="%s"' % el, self.toggles, el)
            self.assertNotIn('id="%s"' % el, self.toolbar, el)
        self.assertIn('<div id="bar-sub-bar"', _block('<div id="bar-sub-row"', '<div id="bar-toggles"'))

    def test_the_selects_are_as_wide_as_their_text(self):
        self.assertIn(".bar-toolbar select { width: auto;", PAGE)

    def test_the_running_totals_standing_box_goes_with_its_view(self):
        self.assertIn("if (!on) { vis('bar-acc-standing-wrap', false); return; }", _fn("barAccSyncUI"))


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheStartIsKept(unittest.TestCase):

    def _run(self, body):
        js = ("var made = 0, barAccStart = 'jan', barData = [{}], barTariffs = [], barEras = [], barBillingDay = 1;\n"
              "function barAccStartChoices() { made++; return [{id: 'jan', m: 1, d: 1}, "
              "{id: 'era:x', m: 7, d: 14}]; }\n" + _fn("barAccAnchor") + body)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_made_once_for_a_redraw(self):
        self.assertEqual(self._run("for (var i = 0; i < 1000; i++) barAccAnchor();\n"
                                   "console.log(JSON.stringify(made));"), 1)

    def test_made_again_when_what_it_is_made_from_changes(self):
        out = self._run("var a = [barAccAnchor()];\n"
                        "barAccStart = 'era:x'; a.push(barAccAnchor());\n"
                        "barEras = []; barAccAnchor();\n"
                        "barData = [{}, {}]; barAccAnchor();\n"
                        "barAccAnchor();\n"
                        "console.log(JSON.stringify({a: a, made: made}));")
        self.assertEqual(out["a"], [{"m": 1, "d": 1}, {"m": 7, "d": 14}])
        self.assertEqual(out["made"], 4)


if __name__ == "__main__":
    unittest.main()
