"""
A backup does not need a checkpoint first — the measurement behind a §4a removal.

Two sites in `engine.py` ran `PRAGMA wal_checkpoint(PASSIVE)` immediately before
`_store.backup(...)`: the pre-Kraken-live snapshot and the pre-upgrade backup. (Both went in engine
step 5a, 27 Oct 2026, with their site tests; the measurement stands for every online backup.)
Neither did anything. SQLite's online backup reads the source through a normal
connection, so committed transactions still sitting in the write-ahead log are
already part of what it copies.

This is not a new finding. `_create_backup_zip` in `web/server.py` was fixed on
exactly this reasoning and `tests/test_writer_exclusion.py` pins it there. The
engine's own two copies were never revisited — which is the usual shape of this
sort of thing, a correct conclusion reached once and not carried across.

The upgrade site's comment made the same mistake in words: *"Commit any pending
transactions before backup to avoid WAL lock"*. A checkpoint is not a commit,
and the backup takes no lock that a checkpoint would release.

§4a gate 5 — read the comment at the site before deleting. Read; it asserted a
purpose the code does not have, and this file is the evidence for gate 4.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from block_store import BlockStore          # noqa: E402


class BackupIsCompleteWithoutACheckpoint(unittest.TestCase):

    def setUp(self):
        self.st = BlockStore(os.path.join(tempfile.mkdtemp(), "blocks.db"))

    def test_an_uncheckpointed_wal_is_still_backed_up(self):
        """The whole argument, in one assertion: write enough to leave megabytes
        in the WAL, never checkpoint, back up, and find every row present."""
        for i in range(800):
            self.st.set_kraken_state("k%d" % i, "v" * 500)

        wal = self.st._path + "-wal"
        self.assertGreater(os.path.getsize(wal), 1024 * 1024,
                           "test is only meaningful with a substantial WAL")

        dst = self.st._path + ".bak"
        self.st.backup(dst)

        c = sqlite3.connect(dst)
        try:
            self.assertEqual(
                c.execute("SELECT COUNT(*) FROM kraken_state").fetchone()[0], 800)
            self.assertIsNotNone(
                c.execute("SELECT value FROM kraken_state WHERE key='k799'").fetchone(),
                "the most recent write, still only in the WAL, must be present")
        finally:
            c.close()

    def test_a_checkpoint_first_makes_no_difference(self):
        """Same data, both ways, compared directly — so the removal is shown to
        be a no-op rather than argued to be one."""
        for i in range(400):
            self.st.set_kraken_state("k%d" % i, "v" * 500)

        without = self.st._path + ".without.bak"
        self.st.backup(without)

        self.st.checkpoint("TRUNCATE")
        with_cp = self.st._path + ".with.bak"
        self.st.backup(with_cp)

        def rows(p):
            c = sqlite3.connect(p)
            try:
                return c.execute(
                    "SELECT key, value FROM kraken_state ORDER BY key").fetchall()
            finally:
                c.close()

        self.assertEqual(rows(without), rows(with_cp))


class TheEngineNoLongerCheckpointsBeforeABackup(unittest.TestCase):
    """The two sites went in engine step 5a; what is left is the startup checkpoint, which is not
    a backup's."""

    def test_startup_keeps_its_own_checkpoint(self):
        """The startup TRUNCATE is NOT part of this removal. It bounds the WAL
        file at boot, which is a different job from making a backup complete, and
        it now runs through `WriteStore.checkpoint()` rather than a raw pragma."""
        import ast
        tree = ast.parse(open(os.path.join(_ROOT, "engine.py")).read())
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and n.name == "_engine_startup_impl":
                names = [c.func.attr for c in ast.walk(n)
                         if isinstance(c, ast.Call)
                         and isinstance(c.func, ast.Attribute)]
                self.assertIn("checkpoint", names)
                return
        self.fail("_engine_startup_impl not found")


if __name__ == "__main__":
    unittest.main()
