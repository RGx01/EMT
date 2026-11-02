"""
An Octopus error is told to a person in plain words, never by its URL (2 Nov 2026).

A REST error's text names the URL it asked — `/v1/accounts/<account>/`, and for readings
`/v1/electricity-meter-points/<MPAN>/meters/<serial>/…` — and quotes Octopus's reply. It reached
the import and gap-fill jobs' status, the re-fetch's, attribution's, and the Settings page when
connecting failed, as it was: a screenshot of a failed import in a public issue showed the
account number. `kraken_api_client.person_message` says what happened from the error's type and
status; `engine._error_text` and the web routes' `_error_text` use it, and anything else is
"where and when". The log keeps the full text (the support file replaces the identifiers there).

Each test fails on the parent tree.
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import engine                                     # noqa: E402
import job_coordinator as C                       # noqa: E402
import kraken_api_client as K                     # noqa: E402

ACCOUNT, MPAN, SERIAL = "A-0000ABC7", "2600000000001", "99Z0000001"
URL = ("https://api.octopus.energy/v1/electricity-meter-points/%s/meters/%s/consumption/"
       % (MPAN, SERIAL))


def _rest_404():
    return K.KrakenAPIError("HTTP 404 for %s: {\"detail\": \"account %s\"}" % (URL, ACCOUNT),
                            status=404)


class PersonMessage(unittest.TestCase):

    def assert_no_identifiers(self, text):
        for s in (ACCOUNT, MPAN, SERIAL, "http", "octopus.energy/"):
            self.assertNotIn(s, text)

    def test_a_rest_error_says_its_status_not_its_url(self):
        m = K.person_message(_rest_404())
        self.assertEqual(m, "Octopus answered with an error (HTTP 404).")
        self.assert_no_identifiers(m)

    def test_a_refused_key_says_what_to_do(self):
        m = K.person_message(K.KrakenAuthError("authentication failed (401) — check API key",
                                               status=401))
        self.assertIn("check it in Settings", m)

    def test_an_edge_block_is_not_the_key(self):
        m = K.person_message(K.KrakenEdgeBlockError("REST blocked …", status=403))
        self.assertIn("not your API key", m)

    def test_a_rate_limit_and_a_cooldown_say_later(self):
        self.assertIn("tries again later",
                      K.person_message(K.KrakenRateLimitError("REST rate limit hit (429)", status=429)))
        self.assertIn("cooldown", K.person_message(K.KrakenCooldownError("cooling", status=403)))

    def test_a_network_error_names_no_url(self):
        m = K.person_message(K.KrakenAPIError("network error for %s: Cannot connect" % URL))
        self.assertEqual(m, "Octopus could not be reached.")

    def test_anything_else_is_not_octopuss(self):
        self.assertIsNone(K.person_message(RuntimeError(URL)))


class TheImportJob(unittest.TestCase):
    """The gap fill shares the import's status, which its page polls."""

    def setUp(self):
        C.reset_for_tests()
        engine._api_import_job.clear()
        engine._api_import_job["status"] = "idle"
        self.p = [mock.patch.object(engine, "_store", None),
                  mock.patch.object(engine, "_persist_run_status", lambda j: None),
                  mock.patch.object(engine, "_run_wrote_blocks", lambda j: False)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        engine._api_import_job.clear()
        engine._api_import_job["status"] = "idle"
        C.reset_for_tests()

    def _run(self, exc):
        async def _boom(*a, **kw):
            raise exc
        with mock.patch.object(engine, "import_api_history", _boom):
            asyncio.run(engine.run_gap_fill_job("2026-01-01T00:00:00", "2026-01-02T00:00:00",
                                                channels=("import",)))
        return engine._api_import_job

    def test_an_octopus_error_leaves_no_identifier_in_the_status(self):
        j = self._run(_rest_404())
        self.assertEqual(j["status"], "error")
        self.assertEqual(j["error"], "Octopus answered with an error (HTTP 404).")

    def test_any_other_error_is_where_and_when(self):
        j = self._run(RuntimeError("/data/" + MPAN))
        self.assertNotIn(MPAN, j["error"])
        self.assertIn("gap fill", j["error"])
        self.assertIn("the log has the detail", j["error"])


class ConnectingFromSettings(unittest.TestCase):

    def test_the_failure_detail_names_no_url(self):
        async def _boom(**kw):
            raise _rest_404()
        with mock.patch.object(engine, "_kraken_env", lambda: {"api_key": "k"}), \
             mock.patch.object(engine, "_kraken_startup_discovery", _boom):
            r = asyncio.run(engine.connect_kraken_now())
        self.assertFalse(r["connected"])
        self.assertEqual(r["detail"], "Octopus answered with an error (HTTP 404).")


if __name__ == "__main__":
    unittest.main()
