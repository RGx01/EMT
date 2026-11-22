"""
EMT's JSON answers are never reused from a cache (22 Nov 2026, the user: "I pressed the link at the bottom of the page to
show the card again but the card never appears" — both Agile cards, until a hard refresh).

Shown again, an Agile card re-reads `/api/agile-prices`; the answer carried no caching header, so a browser or Home
Assistant's ingress could hand back the one from while the card was hidden, that direction left out, and the card had
no prices to show. Every JSON answer is now `Cache-Control: no-store` unless it says otherwise; pages keep `no-cache`.
The Agile fetch asks for a fresh copy too.

Fails on the parent tree.
"""
import importlib.util
import os
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(_ROOT, "web", "templates", "live_power.html"), encoding="utf-8").read()


class _Store:
    def get_settings(self):
        return {"agile_hide_import": True}


class TheHeader(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("server_nocache", os.path.join(_ROOT, "web", "server.py"))
        cls.srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.srv)

    def test_a_json_answer_is_not_stored(self):
        with mock.patch.object(self.srv.store_access, "store", return_value=_Store()):
            r = self.srv.app.test_client().get("/api/settings")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")

    def test_GUARD_an_answer_that_says_otherwise_keeps_it(self):
        app = self.srv.app
        with app.test_request_context():
            resp = app.response_class('{"a":1}', mimetype="application/json")
            resp.headers["Cache-Control"] = "max-age=60"
            out = self.srv._pages_revalidate(resp)
        self.assertEqual(out.headers["Cache-Control"], "max-age=60")

    def test_the_agile_fetch_asks_for_a_fresh_copy(self):
        self.assertIn("fetch(apiUrl('/api/agile-prices'), {cache: 'no-store'})", PAGE)


if __name__ == "__main__":
    unittest.main()
