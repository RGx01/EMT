"""
#67 (4 Oct 2026) — the share backup after every finalise starved the Pi's SD card.

After each 30-minute finalise, `_backup_to_share` copied the whole database to /share:
~80 s on the Pi, and inside those windows (4% of a night) fell 18 of the night's 25
loop-lag warnings. It was a development aid. Now:

  * the share copy is daily, in the local early hours, in a window clear of a
    finalise, and not while the sweep, an import, a delete or a restore runs;
  * every irreversible operation takes a CHECKED zip backup first and refuses without
    one. The main-meter reset "backed up" through a name server.py never defined: the
    NameError was logged as "backup failed" and the database deleted anyway.

Every test here fails on the parent tree; STRUCTURAL ones read the source.
"""
import ast
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import engine                                     # noqa: E402
import job_coordinator as C                       # noqa: E402
from block_store import BlockStore                # noqa: E402

# 4 Oct 2026 is BST (UTC+1): 02:16 UTC is 03:16 local.
BST_0316 = datetime(2026, 10, 4, 2, 16)


class TheDailyCopy(unittest.TestCase):

    def setUp(self):
        C.reset_for_tests()
        self.dir = tempfile.mkdtemp()
        self.started = []
        self._saved = (engine._backup_to_share, engine._iog_site_tz, engine.SHARE_BACKUP_DIR,
                       engine._share_backup_tried_day, engine._backup_in_progress,
                       engine._settle_history_running, engine._delete_active)
        engine._backup_to_share = lambda: self.started.append(1)
        engine._iog_site_tz = lambda store=None: "Europe/London"
        engine.SHARE_BACKUP_DIR = self.dir
        engine._share_backup_tried_day = None
        engine._backup_in_progress = False
        engine._settle_history_running = False
        engine._delete_active = False

    def tearDown(self):
        (engine._backup_to_share, engine._iog_site_tz, engine.SHARE_BACKUP_DIR,
         engine._share_backup_tried_day, engine._backup_in_progress,
         engine._settle_history_running, engine._delete_active) = self._saved
        C.reset_for_tests()

    def _due(self, when):
        return engine._maybe_daily_share_backup(when)

    def test_it_runs_once_in_the_early_hours(self):
        self.assertTrue(self._due(BST_0316))
        self.assertFalse(self._due(datetime(2026, 10, 4, 2, 17)), "twice in a day")
        self.assertEqual(self.started, [1])

    def test_the_window_is_local_and_clear_of_a_finalise(self):
        for utc in (datetime(2026, 10, 4, 3, 16),     # 04:16 BST — outside the hour
                    datetime(2026, 10, 4, 2, 30),     # 03:30 BST — a finalise
                    datetime(2026, 10, 4, 2, 5),      # 03:05 BST — too near 03:00
                    datetime(2026, 10, 4, 14, 16)):   # mid-afternoon
            self.assertFalse(self._due(utc), utc)
        self.assertTrue(self._due(datetime(2026, 12, 10, 3, 46)), "03:46 GMT")

    def test_a_copy_made_today_survives_a_restart(self):
        open(os.path.join(self.dir, "blocks.db"), "w").close()
        t = datetime(2026, 10, 4, 2, 0).timestamp()        # 03:00 local, today
        os.utime(os.path.join(self.dir, "blocks.db"), (t, t))
        self.assertFalse(self._due(BST_0316))
        self.assertEqual(self.started, [])

    def test_it_waits_for_heavy_jobs(self):
        with mock.patch.object(engine, "settle_history_in_progress", lambda: True):
            self.assertFalse(self._due(BST_0316), "the sweep")
        engine._delete_active = True
        self.assertFalse(self._due(BST_0316), "a delete")
        engine._delete_active = False
        for job in ("restore", "import"):
            C.claim(job, force=True)
            C.mark(job, status="running")
            self.assertFalse(self._due(BST_0316), job)
            C.mark(job, status="done")
        self.assertTrue(self._due(BST_0316), "free again: the next window runs it")


class TheFinalise(unittest.TestCase):

    def test_it_no_longer_copies_to_share(self):
        """STRUCTURAL: finalise_block's body calls no share backup."""
        src = open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "finalise_block")
        called = {c.func.id for c in ast.walk(fn) if isinstance(c, ast.Call)
                  and isinstance(c.func, ast.Name)}
        self.assertNotIn("_backup_to_share", called)

    def test_the_copy_is_one_step(self):
        """STRUCTURAL: a stepped copy restarts on every write (#56)."""
        import inspect
        self.assertIn("pages=-1", inspect.getsource(engine._backup_to_share))


def _server():
    spec = importlib.util.spec_from_file_location("server_sb", os.path.join(_ROOT, "web", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv


class TheSafetyBackup(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv = _server()

    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.share = tempfile.mkdtemp()
        self.db = os.path.join(self.data, "blocks.db")
        st = BlockStore(self.db)
        st.close()
        self.p = [mock.patch.object(self.srv, "DATA_DIR", self.data),
                  mock.patch.object(self.srv, "_share_backup_dir", lambda: self.share)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_a_good_backup_is_named(self):
        name = self.srv._safety_backup("pre-test")
        with zipfile.ZipFile(os.path.join(self.share, "backups", name)) as zf:
            self.assertIn("blocks.db", zf.namelist())

    def test_a_zip_without_the_database_is_a_failure(self):
        """_create_backup_zip writes the zip even when the database copy failed."""
        os.remove(self.db)
        with self.assertRaises(RuntimeError):
            self.srv._safety_backup("pre-test")

    def test_the_reset_refuses_without_a_backup(self):
        """The main-meter reset deleted the database when its backup failed."""
        st = BlockStore(self.db)
        with mock.patch.object(self.srv, "_get_store", lambda: st), \
             mock.patch.object(self.srv, "_create_backup_zip",
                               side_effect=OSError("share is full")):
            r = self.srv.app.test_client().post("/api/meter/main/reset")
        self.assertEqual(r.status_code, 500)
        self.assertIn("nothing was changed", r.get_json()["error"])
        self.assertTrue(os.path.exists(self.db))
        self.assertEqual(st._conn.execute("SELECT 1").fetchone()[0], 1, "connection left open")
        st.close()

    def test_a_range_delete_refuses_without_a_backup(self):
        st = BlockStore(self.db)
        with st._conn:
            st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, "
                             "block_minutes, timezone) VALUES (1,'2026-01-01T00:00:00',1,30,'UTC')")
            st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, "
                             "config_period_id, imp_kwh) VALUES ('2026-03-01T10:00:00',"
                             "'2026-03-01T10:30:00','electricity_main',1,1.0)")
        st.close()
        own = BlockStore(self.db)
        with mock.patch.object(self.srv, "_own_store", lambda: own), \
             mock.patch.object(self.srv, "_release_store", lambda s: None), \
             mock.patch.object(self.srv, "_create_backup_zip",
                               side_effect=OSError("share is full")):
            self.srv._delete_job.clear()
            self.srv._delete_worker("2026-03-01", "00:00", "2026-03-01", "23:59",
                                    None, "UTC", False)
        self.assertEqual(self.srv._delete_job.get("status"), "error")
        n = sqlite3.connect(self.db).execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
        self.assertEqual(n, 1, "the block was deleted without a backup")
        own.close()


class EveryIrreversibleRoute(unittest.TestCase):
    """STRUCTURAL: each route that destroys or overwrites data takes the checked
    backup. The list is the #67 inventory (4 Oct 2026); fails on the parent."""

    ROUTES = {
        "/api/meter/main/reset": "pre-reset",
        "/api/meter/<meter_id>/delete-data": "pre-delete-device",
        "/api/config/history": "pre-config-period",
        "/api/config/history/<int:period_id>": "pre-config-period",
        "/api/region/reconcile/apply": "pre-region-reconcile",
        "/api/corrections/apply": "pre-correction",
        # /api/historical/reprice-from-csv and /reprice-repair went with the pricing-health
        # card (engine step 2d, 23 Oct 2026)
    }

    def test_each_takes_a_checked_backup(self):
        src = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
        lines = src.split("\n")
        seen = {}
        for n in ast.walk(ast.parse(src)):
            if not isinstance(n, ast.FunctionDef):
                continue
            for d in n.decorator_list:
                if (isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "route"
                        and d.args and d.args[0].value in self.ROUTES):
                    meth = [e.value for k in d.keywords if k.arg == "methods"
                            for e in k.value.elts] or ["GET"]
                    if meth == ["GET"]:
                        continue
                    body = "\n".join(lines[n.lineno - 1:n.end_lineno])
                    # A member wrapper (B, 4 Oct 2026) holds its claim and calls the
                    # work; the backup is in the work.
                    for helper in ("_meter_delete_data",):
                        if helper + "(" in body:
                            h = next(x for x in ast.walk(ast.parse(src)) if
                                     isinstance(x, ast.FunctionDef) and x.name == helper)
                            body += "\n".join(lines[h.lineno - 1:h.end_lineno])
                    seen.setdefault(d.args[0].value, []).append(
                        '_safety_backup_or_refuse("%s")' % self.ROUTES[d.args[0].value] in body)
        self.assertEqual(sorted(seen), sorted(self.ROUTES))
        self.assertEqual({r: all(v) for r, v in seen.items()}, {r: True for r in self.ROUTES})

    def test_the_reset_backs_up_before_it_deletes(self):
        src = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
        fn = src[src.index("def api_meter_main_reset"):]
        fn = fn[:fn.index("\n@app.route")]
        self.assertLess(fn.index("_safety_backup_or_refuse"), fn.index("os.remove("))


if __name__ == "__main__":
    unittest.main()
