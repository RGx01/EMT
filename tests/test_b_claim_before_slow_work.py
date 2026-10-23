"""
B §5 acceptance 4 (4 Oct 2026) — a claim is taken before any slow work.

"The attribution window, generalised: a test asserting the claim precedes
preflight/backup in every member that has them." The attribution start route used to
check "nobody is running", then preflight and zip the database, and only then flip to
running — two starts inside that window both passed (attribution_claim fixed it).

Checked here for every member with slow pre-work, and FIXED for the one that still had
the window: the API import's start route checked `api_import_running()`, zipped the
database (a minute or more on the Pi), and only then scheduled the job, which claimed
"import" when it ran. A second start, a delete or a restore inside that minute saw no
import. It now claims first (`api_import_claim`).

The structural tests read each member's source order. Those for members that were
already right are GUARDS (they pass on the parent); the import's, and the behavioural
tests, fail there.
"""
import asyncio
import importlib.util
import os
import sys
import threading
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import engine                                     # noqa: E402
import job_coordinator as C                       # noqa: E402

SERVER = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()


def _fn(name):
    i = SERVER.index("def %s(" % name)
    j = SERVER.find("\n@app.route", i)
    k = SERVER.find("\ndef ", i + 1)
    ends = [x for x in (j, k) if x != -1]
    return SERVER[i:min(ends)] if ends else SERVER[i:]


class TheOrderInEachMember(unittest.TestCase):

    def _before(self, body, *steps):
        at = [body.index(s) for s in steps]
        self.assertEqual(at, sorted(at), "out of order: %s" % (steps,))

    def test_attribution(self):
        """GUARD: the original window, fixed before B."""
        self._before(_fn("api_attribute_start"), "attribution_claim(",
                     "attribution_preflight(", '_create_backup_zip(label="pre-attribution")')

    def test_the_api_import(self):
        self._before(_fn("api_historical_api_start"), "api_import_claim()",
                     '_create_backup_zip(label="pre-api-import")', "run_api_import_job(")

    def test_restore(self):
        """GUARD: #58."""
        self._before(_fn("_restore_worker"), '_jc_r.claim("restore"',
                     '_create_backup_zip(label="pre_restore")')

    def test_the_range_delete(self):
        """GUARD: #69/#70."""
        self._before(_fn("_delete_worker"), "set_delete_active(True)",
                     "_stop_running_import()", '_safety_backup("pre-delete")')

    def test_the_purge(self):
        """GUARD: #70."""
        # its backup required since 21 Oct 2026 (data management design §1 (4))
        self._before(_fn("_purge_worker"), "set_delete_active(True)",
                     "_stop_running_import()", '_safety_backup("pre-purge-import")')

    def test_the_device_delete(self):
        """GUARD: #69 — the wrapper claims, the work (with its backup) follows."""
        self._before(_fn("api_meter_delete_data"), "set_delete_active(True)",
                     "_meter_delete_data(meter_id)")
        self.assertIn('_safety_backup_or_refuse("pre-delete-device")', _fn("_meter_delete_data"))


def _server():
    spec = importlib.util.spec_from_file_location("server_cb", os.path.join(_ROOT, "web", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv


class TheImportStart(unittest.TestCase):
    """Behaviour inside the start route's backup."""

    @classmethod
    def setUpClass(cls):
        cls.srv = _server()

    def setUp(self):
        C.reset_for_tests()
        engine._api_import_job.clear()
        engine._api_import_job["status"] = "idle"
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.imported = []

        async def _import(*a, **k):
            self.imported.append(1)
            return {"ok": True, "done": True, "written": {}}
        self.p = [mock.patch.object(self.srv, "_event_loop", self.loop),
                  mock.patch.object(engine, "kraken_available", lambda: True),
                  mock.patch.object(engine, "import_api_history", _import)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        engine._api_import_job.clear()
        engine._api_import_job["status"] = "idle"
        C.reset_for_tests()

    def _start(self):
        return self.srv.app.test_client().post("/api/historical/api-import/start",
                                               json={"confirmed": True})

    def test_during_the_backup_the_import_is_already_claimed(self):
        seen = {}

        def _backup(label="b"):
            seen["running"] = engine.api_import_running()
            seen["coord"] = C.running("import")
            seen["second"] = self._start().status_code       # a second start, mid-backup
            return "/tmp/x.zip"
        with mock.patch.object(self.srv, "_create_backup_zip", _backup):
            self.assertEqual(self._start().status_code, 200)
        self.assertEqual(seen, {"running": True, "coord": True, "second": 409})

    def test_a_cancel_during_the_backup_is_honoured_when_the_job_starts(self):
        def _backup(label="b"):
            engine.api_import_control("cancel")              # a delete pre-empting it
            return "/tmp/x.zip"
        with mock.patch.object(self.srv, "_create_backup_zip", _backup):
            self._start()
        for _ in range(100):                                 # let the job run on its loop
            if engine._api_import_job.get("status") == "cancelled":
                break
            threading.Event().wait(0.02)
        self.assertEqual(engine._api_import_job.get("status"), "cancelled")
        self.assertEqual(self.imported, [], "it imported after being cancelled")
        self.assertFalse(C.running("import"))

    def test_the_claim_is_released_if_the_job_cannot_be_scheduled(self):
        """GUARD: passes on the parent too, which never claimed."""
        with mock.patch.object(self.srv, "_create_backup_zip", lambda label="b": "/tmp/x.zip"), \
             mock.patch("asyncio.run_coroutine_threadsafe", side_effect=RuntimeError("loop gone")):
            r = self._start()
        self.assertEqual(r.status_code, 500)
        self.assertFalse(engine.api_import_running(), "the claim was left held")
        self.assertFalse(C.running("import"))


if __name__ == "__main__":
    unittest.main()
