"""
Save Config shows when there is something to save, and removing a device's read sensor asks if it was wrong
(6 Nov 2026, the user).

`_cfgDirty` compares `config` with a snapshot taken once the page has drawn (drawing can fill in defaults, which
is no edit); empty values count as absent. `_emptiedDevices` lists what a save emptied, for `_maybeOfferClear`
after its reload: Asked, not done, since a sensor can go for other reasons.

While it saves: the server backs up the whole database inside the request, and nothing showed while it did. On a
second press a second save and a second backup went. `_saveBusy` shuts the button and `_doSave` names the backup.

Discriminating: every test here fails on the parent (it is new), except those marked GUARD; `WhileItSaves` fails on
the commit before it, for want of `_saveBusy`. The page was also driven by hand in a browser.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(_ROOT, "web", "templates", "meter_config.html"), encoding="utf-8").read()


def _fn(name):
    i = PAGE.index("function %s(" % name)
    depth, j = 0, PAGE.index("{", i)
    while True:
        depth += {"{": 1, "}": -1}.get(PAGE[j], 0)
        if PAGE[j] == "}" and depth == 0:
            return PAGE[i:j + 1]
        j += 1


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheRules(unittest.TestCase):

    def _run(self, body):
        src = ("var config, _serverConfig, _cfgBaseline = null, _cfgTyping = false;\n"
               "function _updateSaveState() {}\n"
               + "\n".join(_fn(n) for n in ("_cfgCanon", "_cfgKey", "_cfgSetBaseline", "_cfgDirty", "_emptiedDevices"))
               + "\n" + body)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.js")
            with open(p, "w", encoding="utf-8") as f:
                f.write(src)
            r = subprocess.run(["node", p], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def _dirty_after(self, edit):
        return self._run("""
          config = {meters: {a: {meta: {sub_meter: true, device: 'A'}, channels: {import: {read: 's', history_reads: []}}}}};
          _cfgSetBaseline();
          var before = _cfgDirty();
          %s;
          console.log(JSON.stringify([before, _cfgDirty()]));""" % edit)

    def test_a_real_edit_is_dirty(self):
        self.assertEqual(self._dirty_after("config.meters.a.channels.import.read = 't'"), [False, True])

    def test_adding_or_removing_a_device_is_dirty(self):
        self.assertEqual(self._dirty_after("config.meters.b = {meta: {device: 'B'}}"), [False, True])
        self.assertEqual(self._dirty_after("delete config.meters.a"), [False, True])

    def test_undoing_it_is_clean_again(self):
        self.assertEqual(self._dirty_after(
            "config.meters.a.channels.import.read = 't'; config.meters.a.channels.import.read = 's'"), [False, False])

    def test_typing_counts_before_the_field_loses_focus(self):
        self.assertEqual(self._dirty_after("_cfgTyping = true"), [False, True])

    def test_empty_values_are_not_edits(self):
        self.assertEqual(self._dirty_after("""
          var m = config.meters.a; m.meta.note = ''; m.meta.x = null; m.meta.invert = false; m.channels.import.y = [];
          m.channels.import.keep_old_read = false"""), [False, False])

    def test_key_order_is_not_an_edit(self):
        self.assertEqual(self._run("""
          console.log(JSON.stringify(_cfgKey({a: 1, b: 2}) === _cfgKey({b: 2, a: 1})));"""), True)

    def test_only_a_device_whose_read_sensor_was_emptied_is_offered(self):
        out = self._run("""
          function dev(read, hist, meta) { return {meta: Object.assign({sub_meter: true, device: 'D'}, meta || {}),
                                                   channels: {import: {read: read, history_reads: hist || []}}}; }
          _serverConfig = {meters: {gone: dev('s1'), kept: dev('s2'), changed: dev('s3'), never: dev(''),
                                    retired: dev('s4'), hist: dev('s5'), main: {meta: {}, channels: {import: {read: 'm'}}}}};
          config = {meters: {gone: dev(''), kept: dev('s2'), changed: dev('s9'), never: dev(''),
                             retired: dev('', [], {retired_at: '2026-01-01'}), hist: dev('', ['old']),
                             main: {meta: {}, channels: {import: {read: ''}}}, fresh: dev('')}};
          console.log(JSON.stringify(_emptiedDevices()));""")
        self.assertEqual(out, [{"id": "gone", "label": "D", "old": "s1", "history": []},
                               {"id": "hist", "label": "D", "old": "s5", "history": ["old"]}])


SAVING_HARNESS = """
var log = [], btn = {classList: {toggle: function () {}}, textContent: '💾 Save Config', disabled: false, title: ''};
var document = {getElementById: function () { return btn; }};
var window = {scrollTo: function () {}, location: {reload: function () { log.push('reload'); }}};
var config = {meters: {}}, _cfgBaseline = 'edited-from', _cfgTyping = false, _cfgSaving = false;
function _cfgKey() { return 'edited-to'; }
function hideAlerts() {}
function validateDeviceNames() { return NAME_ERRORS; }
function showError(m) { log.push('error: ' + m); }
function show(id) { log.push('show ' + id); }
function progress(t) { log.push('progress'); progressText = t; }
function progressDone() { log.push('progressDone'); }
function _queueRebuildOffer() {} function _queueClearOffer() {}
function apiUrl(p) { return p; }
var setTimeout = function (f, ms) { log.push('timer ' + ms); f(); };
var progressText = null, NAME_ERRORS = [], asked = 0, during = null;
function state() { return {text: btn.textContent, disabled: btn.disabled, log: log.slice()}; }
function answer(reply) {
  return function () {
    asked += 1;
    during = state();
    return reply();
  };
}
"""


@unittest.skipUnless(shutil.which("node"), "node not available")
class WhileItSaves(unittest.TestCase):
    """The server backs up the database inside the request: the page says so, and takes no second press."""

    def _save(self, reply, setup=""):
        src = (SAVING_HARNESS + "\n".join(_fn(n) for n in ("_cfgDirty", "_updateSaveState", "_saveBusy", "_doSave"))
               + "\nvar fetch = answer(function () { return %s; });\n" % reply + setup + """
          _updateSaveState();
          _doSave(null);
          // every promise of the save has settled by the time this one's turn comes
          var p = Promise.resolve(); for (var i = 0; i < 6; i++) p = p.then(function () {});
          p.then(function () {
            console.log(JSON.stringify({during: during, after: state(), asked: asked, text: progressText}));
          });""")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.js")
            with open(p, "w", encoding="utf-8") as f:
                f.write(src)
            r = subprocess.run(["node", p], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    OK = "Promise.resolve({json: function () { return Promise.resolve({ok: true}); }})"
    REFUSED = "Promise.resolve({json: function () { return Promise.resolve({error: 'no'}); }})"

    def test_the_request_goes_with_the_button_shut_and_the_backup_named(self):
        out = self._save(self.OK)
        self.assertEqual(out["during"], {"text": "⏳ Saving…", "disabled": True, "log": ["progress"]})
        self.assertIn("taking a backup first", out["text"])

    def test_a_save_that_worked_stays_shut_until_the_reload(self):
        out = self._save(self.OK)
        self.assertEqual(out["after"]["text"], "✓ Saved")
        self.assertTrue(out["after"]["disabled"])
        self.assertEqual(out["after"]["log"], ["progress", "progressDone", "show alert-success", "timer 1200", "reload"])

    def test_a_refused_save_gives_the_button_back_and_keeps_the_edit_unsaved(self):
        out = self._save(self.REFUSED)
        self.assertEqual((out["after"]["text"], out["after"]["disabled"]), ("💾 Save Config", False))
        self.assertEqual(out["after"]["log"], ["progress", "progressDone", "error: no"])

    def test_a_lost_connection_gives_the_button_back(self):
        out = self._save("Promise.reject(new Error('gone'))")
        self.assertEqual((out["after"]["text"], out["after"]["disabled"]), ("💾 Save Config", False))
        self.assertEqual(out["after"]["log"], ["progress", "progressDone", "error: gone"])

    def test_a_click_elsewhere_during_the_save_does_not_reopen_the_button(self):
        # _watchForEdits re-reads the button on every click, and the edit is still unsaved at that moment
        out = self._save("(_updateSaveState(), %s)" % self.OK)
        self.assertTrue(out["during"]["disabled"])

    def test_GUARD_a_name_the_page_refuses_sends_nothing_and_shows_nothing(self):
        out = self._save(self.OK, setup="NAME_ERRORS = ['bad'];")
        self.assertEqual(out["asked"], 0)
        self.assertEqual((out["after"]["text"], out["after"]["disabled"]), ("💾 Save Config", False))


class TheWiring(unittest.TestCase):

    def test_the_baseline_is_taken_once_drawn(self):
        i = PAGE.index("loadEntities().then(function() {\n    renderAll();")
        block = PAGE[i:i + 500]
        self.assertLess(block.index("renderAll()"), block.index("_cfgSetBaseline()"))
        self.assertLess(block.index("_cfgSetBaseline()"), block.index("_maybeOfferClear()"))

    def test_a_save_queues_the_offer_and_the_button_clears_without_asking_twice(self):
        self.assertIn("_queueClearOffer();", _fn("_doSave"))
        self.assertIn("_histClear(d.id, true);", _fn("_maybeOfferClear"))
        self.assertIn("if (!asked && !window.confirm(", _fn("_histClear"))

    def test_a_device_with_history_sensors_is_not_offered_the_button(self):
        self.assertIn("go.style.display = d.history.length ? 'none' : '';", _fn("_maybeOfferClear"))

    def test_the_button_says_what_it_is(self):
        self.assertIn("b.disabled = !dirty;", _fn("_updateSaveState"))
        self.assertIn("#btn-save.btn-save-dirty", PAGE)
        self.assertIn("prefers-reduced-motion", PAGE)


if __name__ == "__main__":
    unittest.main()
