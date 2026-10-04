"""
B (4 Oct 2026) — the device delete is a coordinator member.

`api_meter_delete_data` removes a device and all its data. It paused the engine and
cancelled a running import, but never set the delete flag. So the members that are
`exclusive_with=("delete",)` — the re-price sweep, the carbon backfill, the measured
drain — and the daily share copy (#67) could not see it. It now joins `delete`, with
the range delete and the purge, and holds the flag while it works. The flag the three
share holds one delete at a time, so each refuses while another holds it.

Tests marked GUARD pass on the parent tree too. The rest fail there.
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import engine                                     # noqa: E402
import job_coordinator as C                       # noqa: E402
from block_store import BlockStore                # noqa: E402


def _server():
    spec = importlib.util.spec_from_file_location("server_dd", os.path.join(_ROOT, "web", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv


class TheDeviceDelete(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv = _server()

    def setUp(self):
        C.reset_for_tests()
        engine.set_delete_active(False)
        self.st = BlockStore(tempfile.mktemp(suffix=".db"))
        self.seen = {}

        def _write(op, timeout=30.0):
            # What the rest of the system sees while the device delete does its work.
            self.seen["flag"] = engine.delete_in_progress()
            self.seen["delete_running"] = C.running("delete")
            self.seen["sweep"] = C.admission("reprice_sweep")[0]
            return {"blocks": 0}
        self.p = [mock.patch.object(self.srv, "_get_store", lambda: self.st),
                  mock.patch.object(self.srv, "_safety_backup", lambda label: label + ".zip"),
                  mock.patch.object(self.srv, "_engine_write", _write)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        engine.set_delete_active(False)
        engine.resume_engine()
        C.reset_for_tests()
        self.st.close()

    def _post(self):
        return self.srv.app.test_client().post("/api/meter/heat_pump/delete-data", json={})

    def test_it_holds_the_delete_flag_while_it_works(self):
        r = self._post()
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(self.seen["flag"], "delete_in_progress() was False mid-delete")
        self.assertTrue(self.seen["delete_running"])
        self.assertNotEqual(self.seen["sweep"], C.ADMIT, "the sweep would have been admitted")

    def test_it_lets_go_afterwards(self):
        """GUARD: passes on the parent too, which never took the flag."""
        self._post()
        self.assertFalse(engine.delete_in_progress())
        self.assertFalse(C.running("delete"))

    def test_it_lets_go_when_the_work_fails(self):
        """GUARD: passes on the parent too, which never took the flag."""
        def _boom(op, timeout=30.0):
            raise RuntimeError("disk")
        with mock.patch.object(self.srv, "_engine_write", _boom):
            r = self._post()
        self.assertEqual(r.status_code, 500)
        self.assertFalse(engine.delete_in_progress())

    def test_it_refuses_while_another_delete_runs(self):
        engine.set_delete_active(True)
        r = self._post()
        self.assertEqual(r.status_code, 409)
        self.assertNotIn("flag", self.seen, "it ran anyway")

    def test_the_range_delete_refuses_while_a_device_delete_holds_the_flag(self):
        engine.set_delete_active(True)
        self.srv._delete_job.clear()
        with mock.patch.object(engine, "poll_in_progress", lambda: False):
            r = self.srv.app.test_client().post("/api/blocks/delete", json={
                "from_date": "2026-03-01", "to_date": "2026-03-01", "confirmed": True})
        self.assertEqual(r.status_code, 409)
        self.assertNotEqual(self.srv._delete_job.get("status"), "running")

    def test_a_free_store_still_deletes(self):
        """GUARD: with nothing else running, the device delete goes through."""
        self.assertEqual(self._post().status_code, 200)


if __name__ == "__main__":
    unittest.main()
