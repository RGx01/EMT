"""
A new account on Octopus's API is offered its history, once (4 Nov 2026, the user).

Octopus keeps about two years of half-hours, and the import that brings them in (Data Management →
History → Import history from Octopus) runs on an empty install — but a new user did not find it. Now
the setup wizard's first Finish, in an API mode, asks: *Bring in your history from Octopus?* **Import
my history** opens Data Management on the import (`?open=import`), whose plan is shown before anything
is asked of Octopus; **Not now** leaves it in the History menu. Once: the first setup is a Finish with
no main meter saved before it (the server's copy as the page loaded — `config` holds a draft main
meter while the wizard runs, which the first version read, and offered nothing); a second Finish on the
same page is not asked again either.

Checked in the local preview on a fresh install (the real startup, `tests/test_fresh_install.py`'s
setting): the offer after Finish; Import my history opened Data Management on the import, at its
Preview plan.

Every test fails on the parent tree, where none of it existed.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = open(os.path.join(_ROOT, "web", "templates", "meter_config.html"), encoding="utf-8").read()
DM = open(os.path.join(_ROOT, "web", "templates", "data_management.html"), encoding="utf-8").read()


def _fn(name):
    i = CFG.index("function " + name + "(")
    return CFG[i:CFG.index("\n}\n", i) + 2]


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheOffer(unittest.TestCase):

    def _run(self, calls):
        js = ("var overlay = {style: {display: 'none'}};\n"
              "var document = {getElementById: function () { return overlay; }};\n"
              "var _historyOffer = {reload: false, shown: false};\n" + _fn("_offerHistory") +
              "\nvar out = [%s];\nconsole.log(JSON.stringify({out: out, shown: overlay.style.display}));" % calls)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_a_first_setup_on_the_api_is_asked(self):
        for mode in ("api", "api+mini", "cad+api"):
            with self.subTest(mode=mode):
                self.assertEqual(self._run("_offerHistory(true, %s)" % json.dumps(mode)),
                                 {"out": [True], "shown": "flex"})

    def test_not_without_the_api(self):
        self.assertEqual(self._run("_offerHistory(true, 'cad')"), {"out": [False], "shown": "none"})

    def test_not_after_the_first_setup(self):
        self.assertEqual(self._run("_offerHistory(false, 'api')"), {"out": [False], "shown": "none"})

    def test_once_on_the_page(self):
        self.assertEqual(self._run("_offerHistory(true, 'api'), _offerHistory(true, 'api')")["out"], [True, False])


class TheWiring(unittest.TestCase):

    def test_the_first_setup_is_the_servers_copy_not_the_draft(self):
        f = _fn("wizApplyAndClose")
        self.assertIn("var firstSetup = !(_serverConfig.meters || {})[mainId];", f)
        self.assertNotIn("var firstSetup = !config.meters[mainId];", f)
        # read before the wizard writes its meter into `config`
        self.assertLess(f.index("var firstSetup"), f.index("config.meters[mainId] = mainMeter;"))

    def test_asked_once_the_save_has_succeeded(self):
        f = _fn("wizApplyAndClose")
        i = f.index("if (data.ok) {")
        self.assertIn("if (_offerHistory(firstSetup, wiz.data.mode)) _historyOffer.reload = rebuild;",
                      f[i:i + 600])

    def test_import_opens_data_management_on_the_import(self):
        self.assertIn("window.location.href = apiUrl('/data-management?open=import');", _fn("_historyOfferImport"))
        self.assertIn('id="history-offer-overlay"', CFG)
        self.assertIn("else if (o === 'import' && {{ uses_api|tojson }}) "
                      "dmWhole('api:whole_history', 'Import history from Octopus');", DM)

    def test_not_now_keeps_the_devices_rebuild_offer(self):
        self.assertIn("if (_historyOffer.reload) window.location.reload();", _fn("_historyOfferNotNow"))


if __name__ == "__main__":
    unittest.main()
