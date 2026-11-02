"""
BL-86's routes (settlement surfaces PR 3): /api/settlement/refetch/{preview,status,apply}.

The engine's half is tests/test_range_refetch.py; this is what the routes refuse before any of it
runs. Discriminating: every test here fails on the parent (the routes are new).
"""
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(__file__))
from tests.test_server import make_client       # noqa: E402
import server                                    # noqa: E402


def _eng(**over):
    eng = sys.modules["engine"]
    base = {"_refetch_check_range": lambda a, b: None, "_REFETCH_PARTS": ("kwh", "bill"),
            "kraken_available": lambda: True, "refetch_claim": lambda: (True, None),
            "refetch_apply_check": lambda t, store=None: None, "refetch_status": lambda: {"status": "idle"},
            "run_refetch_preview": unittest.mock.MagicMock(), "run_refetch_apply": unittest.mock.MagicMock()}
    base.update(over)
    return [unittest.mock.patch.object(eng, k, v, create=True) for k, v in base.items()]


class TheRoutes(unittest.TestCase):

    def setUp(self):
        self.client = make_client()
        server._restore_job = {"status": "idle"}

    def tearDown(self):
        server._restore_job = {"status": "idle"}

    def _post(self, url, body, **over):
        ps = _eng(**over)
        for p in ps:
            p.start()
        try:
            return self.client.post(url, json=body)
        finally:
            for p in ps:
                p.stop()

    def test_a_bad_range_is_400(self):
        r = self._post("/api/settlement/refetch/preview", {"from": "a", "to": "b"},
                       _refetch_check_range=lambda a, b: "at most 31 days at a time")
        self.assertEqual(r.status_code, 400)
        self.assertIn("31 days", r.get_json()["error"])

    def test_unknown_parts_are_400(self):
        r = self._post("/api/settlement/refetch/preview",
                       {"from": "2026-09-01T00:00:00", "to": "2026-09-02T00:00:00", "parts": ["cost"]})
        self.assertEqual(r.status_code, 400)

    def test_no_api_is_400_and_a_restore_is_409(self):
        body = {"from": "2026-09-01T00:00:00", "to": "2026-09-02T00:00:00"}
        self.assertEqual(self._post("/api/settlement/refetch/preview", body,
                                    kraken_available=lambda: False).status_code, 400)
        server._restore_job = {"status": "running"}
        self.assertEqual(self._post("/api/settlement/refetch/preview", body).status_code, 409)

    def test_a_stale_token_is_409_before_any_backup(self):
        with unittest.mock.patch.object(server, "_safety_backup") as bk:
            r = self._post("/api/settlement/refetch/apply", {"token": "x"},
                           refetch_apply_check=lambda t, store=None: "that preview is no longer held")
        self.assertEqual(r.status_code, 409)
        bk.assert_not_called()

    def test_no_safety_backup_no_apply(self):
        ran = unittest.mock.MagicMock()
        with unittest.mock.patch.object(server, "_safety_backup", side_effect=RuntimeError("disk")), \
             unittest.mock.patch.object(server, "_event_loop", unittest.mock.MagicMock()):
            r = self._post("/api/settlement/refetch/apply", {"token": "t"}, run_refetch_apply=ran)
        self.assertEqual(r.status_code, 500)
        err = r.get_json()["error"]
        self.assertIn("no safety backup", err.lower())
        self.assertNotIn("disk", err)               # the exception's text stays in the log
        ran.assert_not_called()

    def test_status_reports_the_job(self):
        ps = _eng(refetch_status=lambda: {"status": "done", "summary": {"differ": 1}})
        for p in ps:
            p.start()
        try:
            self.assertEqual(self.client.get("/api/settlement/refetch/status").get_json()["summary"],
                             {"differ": 1})
        finally:
            for p in ps:
                p.stop()


if __name__ == "__main__":
    unittest.main()
