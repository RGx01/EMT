"""
B §5 acceptance 3 (4 Oct 2026) — `delete` honours its dependency on `import`.

The manifest gives `delete` `depends_on=("import",)`. The device delete and the
restore pre-empted a running API import inline (cancel it, wait, abort if it will not
stop). The range delete and the purge, entries of the same member, did neither: one
could start under a running import, which writes on the engine loop and refills the
span being deleted. Both now stop the import first (`_stop_running_import`), and abort
without deleting if it will not stop.

Tests marked GUARD pass on the parent tree too. The rest fail there.
"""
import functools
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import engine                                     # noqa: E402
from block_store import BlockStore                # noqa: E402


def _server():
    spec = importlib.util.spec_from_file_location("server_di", os.path.join(_ROOT, "web", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv


class _FakeImport:
    """engine's API import, reduced to the two calls a delete makes."""

    def __init__(self, running, stops):
        self.running, self.stops, self.cancels = running, stops, 0

    def is_running(self):
        return self.running

    def control(self, action):
        if action == "cancel":
            self.cancels += 1
            if self.stops:
                self.running = False
        return {"ok": True}


class _Base(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv = _server()

    def setUp(self):
        engine.set_delete_active(False)
        self.db = os.path.join(tempfile.mkdtemp(), "blocks.db")
        st = BlockStore(self.db)
        with st._conn:
            st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, "
                             "block_minutes, timezone) VALUES (1,'2026-01-01T00:00:00',1,30,'UTC')")
            st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, "
                             "config_period_id, imp_kwh, source) VALUES ('2026-03-01T10:00:00',"
                             "'2026-03-01T10:30:00','electricity_main',1,1.0,'imported_api')")
        st.close()
        self.own = BlockStore(self.db)
        self.p = [mock.patch.object(self.srv, "_own_store", lambda: self.own),
                  mock.patch.object(self.srv, "_release_store", lambda s: None),
                  mock.patch.object(self.srv, "_create_backup_zip", lambda label="b": "/tmp/x.zip"),
                  mock.patch.object(self.srv, "_safety_backup", lambda label: label + ".zip")]
        for x in self.p:
            x.start()
        self.srv._delete_job.clear()

    def tearDown(self):
        for x in self.p:
            x.stop()
        engine.set_delete_active(False)
        self.own.close()

    def _with_import(self, fake):
        return [mock.patch.object(engine, "api_import_running", fake.is_running),
                mock.patch.object(engine, "api_import_control", fake.control)]

    def _blocks(self):
        return sqlite3.connect(self.db).execute("SELECT COUNT(*) FROM blocks").fetchone()[0]

    def _run(self, fake, quick=False):
        ps = self._with_import(fake)
        if quick:                     # don't wait the full minute for an import that won't stop
            ps.append(mock.patch.object(self.srv, "_stop_running_import", functools.partial(
                self.srv._stop_running_import, wait_s=1.0)))
        for x in ps:
            x.start()
        try:
            self.work()
        finally:
            for x in ps:
                x.stop()


class TheRangeDelete(_Base):

    def work(self):
        self.srv._delete_worker("2026-03-01", "00:00", "2026-03-01", "23:59", None, "UTC", False)

    def test_it_stops_a_running_import_first(self):
        fake = _FakeImport(running=True, stops=True)
        self._run(fake)
        self.assertEqual(fake.cancels, 1)
        self.assertEqual(self.srv._delete_job.get("status"), "done", self.srv._delete_job)
        self.assertEqual(self._blocks(), 0)

    def test_it_deletes_nothing_if_the_import_will_not_stop(self):
        fake = _FakeImport(running=True, stops=False)
        self._run(fake, quick=True)
        self.assertEqual(self.srv._delete_job.get("status"), "error")
        self.assertIn("still finishing", self.srv._delete_job.get("error") or "")
        self.assertEqual(self._blocks(), 1)
        self.assertFalse(engine.delete_in_progress(), "the flag was left set")

    def test_with_no_import_it_just_deletes(self):
        """GUARD: nothing to stop, nothing cancelled."""
        fake = _FakeImport(running=False, stops=True)
        self._run(fake)
        self.assertEqual(fake.cancels, 0)
        self.assertEqual(self._blocks(), 0)


class ThePurge(_Base):

    def work(self):
        self.srv._purge_worker()

    def test_it_stops_a_running_import_first(self):
        fake = _FakeImport(running=True, stops=True)
        self._run(fake)
        self.assertEqual(fake.cancels, 1)
        self.assertEqual(self.srv._delete_job.get("status"), "done", self.srv._delete_job)
        self.assertEqual(self._blocks(), 0)

    def test_it_purges_nothing_if_the_import_will_not_stop(self):
        fake = _FakeImport(running=True, stops=False)
        self._run(fake, quick=True)
        self.assertEqual(self.srv._delete_job.get("status"), "error")
        self.assertEqual(self._blocks(), 1)

    def test_with_no_import_it_just_purges(self):
        """GUARD: nothing to stop, nothing cancelled."""
        fake = _FakeImport(running=False, stops=True)
        self._run(fake)
        self.assertEqual(fake.cancels, 0)
        self.assertEqual(self._blocks(), 0)


if __name__ == "__main__":
    unittest.main()
