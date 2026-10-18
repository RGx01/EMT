"""
An open page learns it is stale, and a restore redraws every drawn month (18 Oct 2026, the Pi).

1. The heatmap's #205 fix did nothing on the Pi until the Charts page was reloaded: a page keeps
   running the code it was served with across an add-on update. The server now works out a build
   id at start (`BUILD_ID`: the version and every page template, hashed — dev builds share a
   version string), serves it to the page (`EMT.build`) and in the change poll's answer; the
   Charts page's poll compares the two and says "reload" once.
2. A bulk change (a restore, an import, a delete) moves the data epoch. The heatmap's refresh
   emptied its month cache and called redrawAll, which redraws only cached months — so the older
   drawn months kept showing what they showed before the restore. It now re-fetches every drawn
   month.

All discriminating (fail on the parent: none of it exists there). The pages cannot be run here, so
the client tests read their source; the server tests are real requests.
"""
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tpl(name):
    return open(os.path.join(_ROOT, "web", "templates", name), encoding="utf-8").read()


class TheBuild(unittest.TestCase):

    def test_the_change_poll_says_which_build_is_running(self):
        from tests.test_server import make_client, server
        r = make_client().get("/api/energy?fields=fingerprint")
        self.assertEqual(json.loads(r.data)["build"], server.BUILD_ID)

    def test_it_is_a_short_hash_of_the_version_and_the_templates(self):
        from tests.test_server import server
        self.assertTrue(re.fullmatch(r"[0-9a-f]{12}", server.BUILD_ID))
        src = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
        body = src[src.index("def _read_build_id"):src.index("BUILD_ID = _read_build_id()")]
        self.assertIn("APP_VERSION.encode()", body)
        self.assertIn('name.endswith(".html")', body)

    def test_every_page_knows_its_own(self):
        self.assertIn("EMT.build = {{ build_id|default('')|tojson }};", _tpl("base.html"))
        src = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
        self.assertIn('"build_id": BUILD_ID', src)

    def test_the_charts_poll_says_reload_when_they_differ(self):
        page = _tpl("charts.html")
        i = page.index("async function _fetchDataVersion()")
        self.assertIn("_checkBuild(j && j.build);", page[i:page.index("\n}\n", i)])
        i = page.index("function _checkBuild(running)")
        body = page[i:page.index("\n}\n", i)]
        self.assertIn("running === EMT.build", body)
        self.assertIn("id: 'emt-updated'", body)
        self.assertIn("location.reload()", body)


class TheEpoch(unittest.TestCase):

    def test_a_new_epoch_refetches_every_drawn_month(self):
        src = _tpl("heatmap_module.html")
        i = src.index("  function refresh() {")
        body = src[i:src.index("\n  }\n", i)]
        self.assertIn("if (state.epoch !== ep) {", body)
        self.assertIn("return Promise.all(state.keys.map(function (o) {", body)
        self.assertIn("if (o !== k && !p.drawn) return null;", body)
        self.assertNotIn("if (state.epoch !== ep) { redrawAll(true); return; }", body)


if __name__ == "__main__":
    unittest.main()
