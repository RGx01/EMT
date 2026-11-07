"""
Any Overview card can be hidden (7 Nov 2026, the user, after the Agile cards' own button: "can all cards have this
on overview?").

The saved list lives in the `overview_hidden` setting, by each card's `data-ov-card` key, and becomes one CSS rule.
That rule is set before the cards exist and uses !important, so a hidden card never flashes, a device card built later
is covered, and `loadChargeSessions`, which sets its card's display every minute, cannot bring its card back.

Keys go into the page's CSS, so the route takes only a narrow alphabet. The Agile cards keep their own flags, as
hiding those also stops fetching prices.

Also driven by hand in a browser on a copy of a real database: mix, Smart Charging, Today and a device hidden, kept
across a reload, then each brought back from the foot of the page; the history chart redraws at full width on return.

Discriminating: the tests not marked GUARD fail on the parent.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(_ROOT, "web", "templates", "live_power.html"), encoding="utf-8").read()


def _server():
    spec = importlib.util.spec_from_file_location("server_ovhide", os.path.join(_ROOT, "web", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv


class _Store:
    def __init__(self, saved=None):
        self.saved = saved or {}

    def get_settings(self):
        return dict(self.saved)

    def save_settings(self, s):
        self.saved = s


class TheSetting(unittest.TestCase):

    def setUp(self):
        self.srv = _server()
        self.store = _Store()
        self.patches = [mock.patch.object(self.srv.store_access, "store", return_value=self.store),
                        mock.patch.object(self.srv, "_engine_write", side_effect=lambda f: f(self.store))]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = self.srv.app.test_client()

    def _post(self, val):
        return self.client.post("/api/settings", data=json.dumps({"overview_hidden": val}),
                                content_type="application/json")

    def test_it_saves_and_reads_back_without_repeats(self):
        self.assertEqual(self._post(["mix", "device-sub_meter_1", "mix"]).status_code, 200)
        self.assertEqual(self.store.saved["overview_hidden"], ["mix", "device-sub_meter_1"])
        self.assertEqual(self.client.get("/api/settings").get_json()["overview_hidden"], ["mix", "device-sub_meter_1"])

    def test_it_starts_empty(self):
        self.assertEqual(self.client.get("/api/settings").get_json()["overview_hidden"], [])

    def test_a_key_that_could_break_the_pages_css_is_refused(self):
        for bad in (['a"]{}body{display:none}'], ["a b"], ["</style>"], [""], [1], "mix", ["x" * 81], ["k"] * 65):
            with self.subTest(bad=str(bad)[:30]):
                self.assertEqual(self._post(bad).status_code, 400)
                self.assertNotIn("overview_hidden", self.store.saved)

    def test_a_bad_saved_value_is_not_read_back(self):
        self.store.saved = {"overview_hidden": ["mix", 'a"]{}', 3]}
        self.assertEqual(self.client.get("/api/settings").get_json()["overview_hidden"], ["mix"])
        self.store.saved = {"overview_hidden": "mix"}
        self.assertEqual(self.client.get("/api/settings").get_json()["overview_hidden"], [])

    def test_GUARD_the_agile_flags_still_save(self):
        self.client.post("/api/settings", data=json.dumps({"agile_hide_export": True}), content_type="application/json")
        self.assertTrue(self.store.saved["agile_hide_export"])


@unittest.skipUnless(shutil.which("node"), "node not available")
class ThePure(unittest.TestCase):

    def _js(self, expr):
        def fn(n):
            i = PAGE.index("function %s(" % n)
            return PAGE[i:PAGE.index("\n}\n", i) + 2]
        src = fn("_ovCss") + "\n" + fn("_ovToggled") + "\n" + fn("_ovCardLabel") + "\nconsole.log(JSON.stringify(%s));" % expr
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(src)
        try:
            r = subprocess.run(["node", fh.name], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(fh.name)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_the_rule_names_each_card_and_beats_a_scripts_own_display(self):
        self.assertEqual(self._js("_ovCss(['mix','device-a'])"),
                         '[data-ov-card="mix"],[data-ov-card="device-a"]{display:none!important;}')

    def test_nothing_hidden_no_rule(self):
        self.assertEqual(self._js("_ovCss([])"), "")

    def test_hiding_and_showing(self):
        self.assertEqual(self._js("_ovToggled(['a'],'b',true)"), ["a", "b"])
        self.assertEqual(self._js("_ovToggled(['a','b'],'a',true)"), ["b", "a"])
        self.assertEqual(self._js("_ovToggled(['a','b'],'a',false)"), ["b"])

    def test_a_cards_link_leaves_out_the_buttons_beside_its_title(self):
        card = "{querySelector:function(){return {querySelector:function(){return {textContent:' 📈 History '};},textContent:'📈 History kW CO2'};}}"
        self.assertEqual(self._js("_ovCardLabel(%s)" % card), "📈 History")


class ThePage(unittest.TestCase):

    def test_every_card_is_marked(self):
        for key, near in (("mix", 'id="mix-card"'), ("power", 'class="power-card"'), ("charge", 'id="charge-card-wrap"'),
                          ("carbon", 'id="carbon-card-wrap"'), ("history", 'id="power-history-card-wrap"')):
            with self.subTest(key):
                self.assertRegex(PAGE, r'%s[^>]*data-ov-card="%s"' % (re.escape(near), key))
        self.assertIn('class="billing-card" data-ov-card="{{ card_id }}"', PAGE)
        self.assertIn("data-ov-card=\"device-' + meterId + '\"", PAGE)

    def test_the_rule_is_set_before_the_cards_exist(self):
        self.assertLess(PAGE.index("document.getElementById('ov-hidden-style').textContent"), PAGE.index("{% block content %}"))

    def test_a_device_card_built_later_gets_its_button(self):
        i = PAGE.index("container.appendChild(tmp.firstChild);")
        self.assertIn("_ovRefresh();", PAGE[i:i + 80])

    def test_a_hidden_gauge_does_not_count_towards_the_layout(self):
        i = PAGE.index("function _applyLayout(")
        self.assertIn("_ovHidden.indexOf('power') < 0", PAGE[i:i + 600])

    def test_GUARD_an_expanded_card_leaves_the_button_out(self):
        # `_expandCard` clones the card's children but skips `.expand-btn`
        self.assertIn("b.className = 'expand-btn ov-hide-btn';", PAGE)
        self.assertIn("child.classList.contains('expand-btn')", PAGE)

    def test_GUARD_the_agile_cards_keep_their_own_button(self):
        self.assertIn("_agileSetHidden('{{ ch }}', true)", PAGE)
        self.assertNotIn('data-ov-card="agile', PAGE)


if __name__ == "__main__":
    unittest.main()
