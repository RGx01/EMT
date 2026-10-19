"""
Before a configuration is in place, the welcome screen is the only way in (19 Oct 2026, staging).

Only "/" sent an unconfigured install to /welcome: a user who opened Charts (or any page) instead of
following it could not get back. Now `_first_run_guard` redirects every page to /welcome until there
is a configuration — except the pages the fork itself uses (Settings: the setup wizard, and where a
restore without a key lands) and Help and Logs. APIs, static files and the Lovelace cards are left
alone; a failed config read never locks the user out. The sidebar dims the links that would bounce.

Discriminating (fail on the parent: no guard), except the GUARDs marked.
"""
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


class TheGuard(unittest.TestCase):

    def setUp(self):
        from tests.test_server import make_client, server
        self.server = server
        self.client = make_client()
        self.cfg = {"meters": {}}
        self.p = mock.patch.object(server, "load_config", lambda: self.cfg)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def _get(self, path):
        return self.client.get(path)

    def test_an_unconfigured_page_goes_to_welcome(self):
        for path in ("/charts", "/insights", "/data-management", "/fill-history"):
            r = self._get(path)
            self.assertEqual(r.status_code, 302, path)
            self.assertTrue(r.headers["Location"].endswith("/welcome"), path)

    def test_the_fork_s_own_pages_and_help_stay_open(self):
        """GUARD (on the parent every page is open)."""
        for path in ("/welcome", "/settings", "/help", "/logs"):
            self.assertNotEqual(self._get(path).status_code, 302, path)

    def test_apis_are_not_pages(self):
        """GUARD: an API is never redirected (the welcome page's restore runs on them)."""
        self.assertNotEqual(self._get("/api/backup/restore/status").status_code, 302)

    def test_a_configured_install_is_left_alone(self):
        """GUARD."""
        self.cfg = {"meters": {"electricity_main": {"meta": {}, "channels": {}}}}
        self.assertNotEqual(self._get("/charts").status_code, 302)

    def test_a_failed_read_never_locks_out(self):
        def boom():
            raise RuntimeError("store not open")
        with mock.patch.object(self.server, "load_config", boom), \
             self.server.app.test_request_context("/charts"):
            from flask import request
            request.url_rule = self.server.app.url_map.bind("localhost").match("/charts", return_rule=True)[0]
            self.assertIsNone(self.server._first_run_guard())

    def test_it_redirects_when_unconfigured_called_directly(self):
        with self.server.app.test_request_context("/charts"):
            from flask import request
            request.url_rule = self.server.app.url_map.bind("localhost").match("/charts", return_rule=True)[0]
            r = self.server._first_run_guard()
        self.assertEqual(r.status_code, 302)


class TheSidebar(unittest.TestCase):

    def test_locked_links_are_dimmed_before_setup(self):
        base = open(os.path.join(_ROOT, "web", "templates", "base.html"), encoding="utf-8").read()
        self.assertEqual(base.count('{% if unconfigured %}data-locked="1"'), 4)
        self.assertIn(".sidebar nav a[data-locked] { opacity:.45; }", base)


if __name__ == "__main__":
    unittest.main()
