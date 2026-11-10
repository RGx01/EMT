"""
The eras modal (Charts → Billing) reopens with "+ Add an era" (10 Nov 2026, the user).

Adding an era hides the add button while its form is open, and Cancel shows it again. Closing the
modal any other way — the ✕, a click on the backdrop, Escape — left it hidden, so the next opening
offered Edit only, until an edit was cancelled. Every opening now starts as Cancel leaves it.

Discriminating: test_reopening_after_closing_mid_form_offers_add fails on the parent.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _page():
    return open(os.path.join(_ROOT, "web", "templates", "charts.html"), encoding="utf-8").read()


def _fn(src, name):
    i = src.index("function %s(" % name)
    depth, j = 0, src.index("{", i)
    while True:
        depth += {"{": 1, "}": -1}.get(src[j], 0)
        if src[j] == "}" and depth == 0:
            return src[i:j + 1]
        j += 1


HARNESS = """
var els = {};
function el(id) {
  return els[id] || (els[id] = {style: {}, innerHTML: '', classList: {
    on: false, add: function () { this.on = true; }, remove: function () { this.on = false; },
    contains: function () { return this.on; }}});
}
var document = {getElementById: el};
function apiUrl(p) { return p; }
function _eraEsc(v) { return String(v); }
var _eras = [], _erasSite = '';
function erasRenderList() {}
function fetch() { return new Promise(function () {}); }   // the list never arrives: not this test's concern
"""


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheModal(unittest.TestCase):

    def _run(self, steps):
        src = HARNESS + "\n".join(_fn(_page(), n) for n in ("erasOpen", "erasClose", "erasCancel")) + """
          // the state "+ Add an era" leaves (erasEdit): the form showing, the add button hidden
          function addingAnEra() { el('eras-form-wrap').innerHTML = '<form></form>'; el('eras-add-btn').style.display = 'none'; }
          %s
          console.log(JSON.stringify({open: el('eras-backdrop').classList.on,
                                      add: el('eras-add-btn').style.display, form: el('eras-form-wrap').innerHTML}));
        """ % steps
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.js")
            with open(p, "w", encoding="utf-8") as f:
                f.write(src)
            r = subprocess.run(["node", p], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_reopening_after_closing_mid_form_offers_add(self):
        out = self._run("erasOpen(); addingAnEra(); erasClose(); erasOpen();")
        self.assertEqual(out, {"open": True, "add": "", "form": ""})

    def test_GUARD_cancel_then_reopen_still_offers_add(self):
        out = self._run("erasOpen(); addingAnEra(); erasCancel(); erasClose(); erasOpen();")
        self.assertEqual(out, {"open": True, "add": "", "form": ""})

    def test_GUARD_adding_hides_the_button_this_resets(self):
        # The harness's addingAnEra() stands in for erasEdit's first step; if erasEdit stops hiding
        # the button, this says the test above no longer tests the real state.
        self.assertIn("document.getElementById('eras-add-btn').style.display = 'none';", _fn(_page(), "erasEdit"))


class TheWaysOut(unittest.TestCase):

    def test_GUARD_the_cross_backdrop_and_escape_all_go_through_erasClose(self):
        page = _page()
        self.assertIn('onclick="erasClose()" aria-label="Close">✕</button>', page)
        self.assertIn('id="eras-backdrop" onclick="if(event.target===this)erasClose()"', page)
        self.assertIn("classList.contains('open')) erasClose();", page)


if __name__ == "__main__":
    unittest.main()
