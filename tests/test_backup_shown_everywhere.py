"""
Every page that makes the user wait for a backup says so (6 Nov 2026, the user, after Save Config's #104 and Billing
History): the setup wizard, a postcode saved on Settings or Insights, the rebuild offer after a save, deleting a device,
applying a re-fetch, saving sites before an import.

And one that said so and should not: a gap fill read "Backing up & starting…", but `api_backfill_fill_gap` takes no
backup. It now reads "Starting…"; the whole-history import, which does, keeps the words.

Discriminating: the tests not marked GUARD fail on the parent. The postcode saves run for real against a controlled
`fetch`; the others are nested in larger flows, so their order and words are read from the source.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _page(name):
    return open(os.path.join(_ROOT, "web", "templates", name), encoding="utf-8").read()


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
function el(id) { return els[id] || (els[id] = {style: {}, disabled: false, textContent: '', value: ''}); }
var document = {getElementById: el};
el('postcode-input').value = 'sw1a';
var LABEL = 'Save it'; el('postcode-save').textContent = LABEL;
function apiUrl(p) { return p; }
function showSuccess() {} function showError(m) { el('alert').textContent = m; }
var posts = 0, during = null, reply = null;
async function fetch(u, o) {
  if (!o) return {json: async function () { return {meters: {m: {meta: {}}}}; }};
  posts += 1;
  during = {text: el('postcode-save').textContent, off: el('postcode-save').disabled};
  return reply();
}
var OK = function () { return {json: async function () { return {ok: true}; }}; };
var NO = function () { return {json: async function () { return {error: 'no'}; }}; };
var GONE = function () { throw new Error('gone'); };
"""


@unittest.skipUnless(shutil.which("node"), "node not available")
class ThePostcode(unittest.TestCase):

    def _run(self, page, reply, setup=""):
        src = (HARNESS + "async " + _fn(_page(page), "savePostcode") + "\nreply = %s;\n%s\n" % (reply, setup) + """
          var first = savePostcode(), second = savePostcode();
          Promise.all([first, second]).then(function () {
            console.log(JSON.stringify({during: during, posts: posts,
              after: {text: el('postcode-save').textContent, off: el('postcode-save').disabled}}));
          });""")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.js")
            with open(p, "w", encoding="utf-8") as f:
                f.write(src)
            r = subprocess.run(["node", p], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_it_names_the_backup_and_takes_one_press(self):
        for page in ("settings.html", "insights.html"):
            with self.subTest(page):
                out = self._run(page, "OK")
                self.assertEqual(out["during"], {"text": "⏳ Backing up and saving…", "off": True})
                self.assertEqual(out["posts"], 1)

    def test_GUARD_the_button_comes_back_whatever_happened(self):
        for page in ("settings.html", "insights.html"):
            for reply in ("OK", "NO", "GONE"):
                with self.subTest(page=page, reply=reply):
                    self.assertEqual(self._run(page, reply)["after"], {"text": "Save it", "off": False})

    def test_GUARD_an_empty_postcode_sends_nothing(self):
        for page in ("settings.html", "insights.html"):
            with self.subTest(page):
                out = self._run(page, "OK", "el('postcode-input').value = '  ';")
                self.assertEqual((out["posts"], out["after"]["off"]), (0, False))

    def test_the_buttons_carry_the_id(self):
        for page in ("settings.html", "insights.html"):
            self.assertIn('id="postcode-save"', _page(page))


class TheWords(unittest.TestCase):

    def test_the_wizard_says_so_before_it_saves_and_clears_it_on_every_ending(self):
        wiz = _fn(_page("meter_config.html"), "wizApplyAndClose")
        i = wiz.index("taking a backup first")
        j = wiz.index("fetch(apiUrl('/api/config')", i)
        self.assertLess(i, j)
        rest = wiz[j:]
        self.assertLess(rest.index("progressDone();"), rest.index("if (data.ok)"))
        self.assertLess(rest.index("catch(e) {"), rest.rindex("progressDone();"))

    def test_the_rebuild_offer_locks_and_says_so_before_it_starts_and_gives_back_on_failure(self):
        page = _page("meter_config.html")
        i = page.index("document.getElementById('rebuild-now').onclick")
        body = page[i:page.index("modal.style.display = 'flex';", i)]
        self.assertLess(body.index("now.disabled = later.disabled = true;"), body.index("fetch(apiUrl("))
        self.assertIn("Checking, backing up and starting", body)
        self.assertIn("later.disabled = false;", body)
        self.assertIn("catch(function(e) { now.disabled = later.disabled = false;", body)

    def test_deleting_a_device_says_so(self):
        # (the whole-meter reset's own words are "Backing up, then deleting all data")
        self.assertIn('Backing up, then deleting "\' + devName', _page("meter_config.html"))

    def test_a_refetch_apply_says_so(self):
        self.assertIn("Backing up, then applying", _page("heatmap_module.html"))

    def test_saving_sites_says_so(self):
        self.assertIn("'Backing up & saving sites…'", _page("historical_import.html"))


class TheGapFill(unittest.TestCase):
    """A claim about the server, so the server is read too."""

    def test_it_does_not_claim_a_backup(self):
        start = _fn(_page("historical_import.html"), "_doStart")
        self.assertIn("PENDING_GAP ? 'Starting…' : 'Backing up & starting…'", start)

    def test_GUARD_the_route_takes_no_backup_and_the_import_does(self):
        # If a gap fill gains a backup, this says the page's words should change back.
        src = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
        def body(name):
            m = re.search(r"^def %s\(.*?(?=^@app\.route|^def )" % name, src, re.S | re.M)
            return m.group(0)
        self.assertNotIn("backup", body("api_backfill_fill_gap"))
        self.assertIn('_create_backup_zip(label="pre-api-import")', body("api_historical_api_start"))


if __name__ == "__main__":
    unittest.main()
