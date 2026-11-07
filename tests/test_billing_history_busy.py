"""
Billing History says when it is taking a backup (6 Nov 2026, the user, after Save Config's #104).

Saving, adding or deleting a period and applying a site change all back up the whole database inside the request.
The dialog said only "Saving…", and Escape or the backdrop could close it mid-save; a delete said "Deleting…"; Apply
said nothing and took a second press.

Discriminating: the tests not marked GUARD fail on the parent. The real functions run against a controlled `fetch`,
so what is on the page during the request is what is tested. The page was also driven by hand in a browser.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(_ROOT, "web", "templates", "billing_history.html"), encoding="utf-8").read()


def _fn(name):
    i = PAGE.index("function %s(" % name)
    depth, j = 0, PAGE.index("{", i)
    while True:
        depth += {"{": 1, "}": -1}.get(PAGE[j], 0)
        if PAGE[j] == "}" and depth == 0:
            return PAGE[i:j + 1]
        j += 1


HARNESS = """
var els = {};
function el(id) {
  return els[id] || (els[id] = {id: id, style: {}, disabled: false, textContent: '', value: '',
                                querySelector: function () { return null; }, appendChild: function (c) { els.badge = c; }});
}
var document = {getElementById: el, createElement: function () { return el('new'); },
                querySelectorAll: function () { return []; }};
el('edit-from').value = '2026-01-01'; el('edit-billing-day').value = '1';
el('edit-save-btn').textContent = 'Save Changes'; el('edit-cancel-btn').textContent = 'Cancel';
el('reconcile-apply').textContent = 'Apply';
var _editId = 7, _reconcilePlan = {source: 'api', sites: []}, _editSaving = false, log = [];
function fromLocalInput(v) { return v; }
function apiUrl(p) { return p; }
function showError(m) { log.push('error: ' + m); }
function showSuccess() { log.push('success'); }
function load() { log.push('load'); }
function _unlockAfterDelete() { log.push('unlocked'); }
var during = null, reply = null;
function fetch() { during = snap(); return reply(); }
var OK = function () { return Promise.resolve({json: function () { return Promise.resolve({ok: true}); }}); };
var NO = function () { return Promise.resolve({json: function () { return Promise.resolve({error: 'no'}); }}); };
var GONE = function () { return Promise.reject(new Error('gone')); };
function settle(f) { var p = Promise.resolve(); for (var i = 0; i < 6; i++) p = p.then(function () {}); p.then(f); }
"""


@unittest.skipUnless(shutil.which("node"), "node not available")
class WhileItWorks(unittest.TestCase):

    def _run(self, body):
        names = ("closeModal", "saveEdit", "applyReconcile", "doDelete")
        src = HARNESS + "\n".join(_fn(n) for n in names) + "\n" + body
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.js")
            with open(p, "w", encoding="utf-8") as f:
                f.write(src)
            r = subprocess.run(["node", p], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    SAVE = """
      function snap() { return {btn: el('edit-save-btn').textContent, off: el('edit-save-btn').disabled,
                                note: el('edit-busy').style.display, saving: _editSaving}; }
      el('edit-modal').style.display = 'flex';
    """

    def test_a_save_names_the_backup_during_the_request(self):
        out = self._run(self.SAVE + """reply = OK; saveEdit();
          settle(function () { console.log(JSON.stringify(during)); });""")
        self.assertEqual(out, {"btn": "Saving…", "off": True, "note": "block", "saving": True})

    def test_the_note_is_about_the_backup(self):
        i = PAGE.index('id="edit-busy"')
        self.assertIn("Taking a backup first", PAGE[i:PAGE.index("</div>", i)])

    def test_escape_cannot_close_the_dialog_during_a_save(self):
        out = self._run(self.SAVE + """
          reply = function () { closeModal(); during = el('edit-modal').style.display; return OK(); };
          saveEdit();
          settle(function () { console.log(JSON.stringify([during, el('edit-modal').style.display])); });""")
        self.assertEqual(out, ["flex", "none"])

    def test_a_refused_save_gives_the_dialog_back(self):
        out = self._run(self.SAVE + """reply = NO; saveEdit();
          settle(function () { console.log(JSON.stringify({now: snap(), modal: el('edit-modal').style.display, log: log})); });""")
        self.assertEqual(out, {"now": {"btn": "Save Changes", "off": False, "note": "none", "saving": False},
                               "modal": "flex", "log": ["error: no"]})

    def test_a_lost_connection_gives_the_dialog_back(self):
        out = self._run(self.SAVE + """reply = GONE; saveEdit();
          settle(function () { console.log(JSON.stringify([snap().saving, snap().note, log])); });""")
        self.assertEqual(out, [False, "none", ["error: gone"]])

    APPLY = """
      function snap() { return {btn: el('reconcile-apply').textContent, off: el('reconcile-apply').disabled,
                                dismiss: el('reconcile-dismiss').disabled, note: el('reconcile-busy').style.display}; }
    """

    def test_apply_shuts_its_buttons_and_names_the_backup(self):
        out = self._run(self.APPLY + """reply = OK; applyReconcile();
          settle(function () { console.log(JSON.stringify(during)); });""")
        self.assertEqual(out, {"btn": "Applying…", "off": True, "dismiss": True, "note": "block"})

    def test_a_refused_apply_gives_its_buttons_back(self):
        for reply, msg in (("NO", "error: no"), ("GONE", "error: gone")):
            out = self._run(self.APPLY + """reply = %s; applyReconcile();
              settle(function () { console.log(JSON.stringify({now: snap(), log: log})); });""" % reply)
            self.assertEqual(out, {"now": {"btn": "Apply", "off": False, "dismiss": False, "note": "none"}, "log": [msg]})

    def test_the_panels_note_is_about_the_backup(self):
        i = PAGE.index('id="reconcile-busy"')
        self.assertIn("Taking a backup first", PAGE[i:PAGE.index("</div>", i)])

    def test_a_delete_names_the_backup_during_the_request(self):
        out = self._run("""
          function snap() { return el('new').textContent; }
          el('period-card-3'); reply = OK; doDelete(3, 0);
          settle(function () { console.log(JSON.stringify(during)); });""")
        self.assertIn("Taking a backup first, then deleting", out)

    def test_GUARD_a_form_the_page_refuses_sends_nothing_and_shows_nothing(self):
        out = self._run(self.SAVE + """el('edit-from').value = ''; reply = OK; saveEdit();
          console.log(JSON.stringify([during, snap().note === undefined, snap().saving]));""")
        self.assertEqual(out, [None, True, False])


if __name__ == "__main__":
    unittest.main()
