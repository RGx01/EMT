"""
Engine step 5a (27 Oct 2026; EMT-v5_engine_step5_design.md §3): one start-up backup per version.

- the upgrade backup taken after the store opened and migrated goes (decided 22 Oct 2026, the engine
  review §6 (e)); the pre-migration copy is the one per version, and it now trims the share's
  backups folder to the 20 newest, the trim the upgrade backup did;
- v4's pre-live snapshot (once per database, before the first live DCC write) goes with its dry-run
  fallback (decided 27 Oct 2026): every poll writes.

Discriminating: every test here fails on the parent, but those labelled GUARD.
"""
import ast
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import test_engine  # noqa: F401  — installs stubs, imports engine
import engine

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read()


class TheOneBackup(unittest.TestCase):

    def setUp(self):
        self.data, self.share = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.bk = os.path.join(self.share, "backups")
        os.makedirs(self.bk)
        for i in range(25):
            open(os.path.join(self.bk, "2026010%dT0000%02d_old.zip" % (1 + i // 10, i)), "w").close()
        self.db = os.path.join(self.data, "blocks.db")
        open(self.db, "wb").write(b"x")
        self.p = [mock.patch.object(engine, "DATA_DIR", self.data),
                  mock.patch.object(engine, "SHARE_BACKUP_DIR", self.share),
                  mock.patch.object(engine, "BLOCKS_DB_PATH", self.db),
                  mock.patch.object(engine, "_read_config_version", lambda: "5.0.0-test")]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def _zips(self):
        return sorted(os.listdir(self.bk))

    def test_the_pre_migration_copy_trims_the_folder(self):
        engine._pre_upgrade_backup_if_needed()
        z = self._zips()
        self.assertEqual(len(z), 20)
        self.assertTrue(any("_pre_upgrade_5.0.0-test" in n for n in z), "the new copy is kept")

    def test_GUARD_once_per_version(self):
        engine._pre_upgrade_backup_if_needed()
        n = len(self._zips())
        engine._pre_upgrade_backup_if_needed()
        self.assertEqual(len(self._zips()), n)


class TheRemovals(unittest.TestCase):

    def test_the_upgrade_backup_after_the_store_opens_is_gone(self):
        self.assertNotIn(".last_startup_backup_version", SRC)
        self.assertNotIn('_upgrade_{_cur_ver}.zip"', SRC.replace("_pre_upgrade_", ""))

    def test_the_pre_live_snapshot_is_gone(self):
        self.assertFalse(hasattr(engine, "_kraken_pre_live_snapshot"))
        self.assertFalse(hasattr(engine, "_KRAKEN_SNAPSHOT_DONE_KEY"))

    def test_every_poll_writes(self):
        fn = next(n for n in ast.walk(ast.parse(SRC))
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "kraken_poll_task")
        polls = [c for c in ast.walk(fn) if isinstance(c, ast.Call)
                 and isinstance(c.func, ast.Attribute) and c.func.attr == "poll"]
        self.assertTrue(polls)
        self.assertFalse(any(k.arg == "dry_run" for c in polls for k in c.keywords))


if __name__ == "__main__":
    unittest.main()
