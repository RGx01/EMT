"""
`BlockStore.checkpoint()`, `journal_size_limit`, and the classifier rule that
puts a mutating PRAGMA on the write surface -- workstream A acceptance 5.

THE WAL, and why any of this matters. SQLite does not edit blocks.db in place;
it appends changes to blocks.db-wal and periodically copies them back. That
copy-back is the checkpoint. Measured here rather than assumed:

  * with no concurrent reader, SQLite's own wal_autocheckpoint holds the WAL at
    ~4 MB indefinitely -- growth is already bounded, and the startup TRUNCATE is
    tidiness rather than the thing keeping the lid on;
  * a reader holding an open transaction is the only thing that breaks that, and
    it breaks the checkpoint at the same time and for the same reason: a
    checkpoint cannot reclaim pages a reader still needs;
  * before journal_size_limit, that inflation was permanent -- the WAL is reused
    in place, never shrunk, so one unlucky coincidence left it large until a
    restart, and this add-on runs for months.

So the fix is not a retry on the checkpoint (which would wait on the very reader
that blocked it). It is to let SQLite recover the space by itself.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from block_store import BlockStore          # noqa: E402


def _mk_store():
    st = BlockStore(tempfile.mktemp(suffix=".db"))
    st._conn.execute("PRAGMA foreign_keys=OFF")
    return st


class CheckpointContract(unittest.TestCase):

    def setUp(self):
        self.st = _mk_store()

    def test_it_returns_sqlites_own_row(self):
        busy, log_pages, done = self.st.checkpoint("TRUNCATE")
        self.assertEqual(busy, 0)
        self.assertIsInstance(log_pages, int)
        self.assertIsInstance(done, int)

    def test_busy_is_reported_not_raised(self):
        """A blocked checkpoint is a normal outcome under contention, not an
        error. It must reach the caller as data -- every site before this method
        existed discarded it, and startup logged 'complete' regardless."""
        for i in range(400):
            self.st.set_kraken_state("k%d" % i, "v" * 500)
        other = sqlite3.connect(self.st._path)
        other.execute("BEGIN")
        other.execute("SELECT COUNT(*) FROM blocks").fetchone()
        try:
            self.st.set_kraken_state("more", "v")
            busy, log_pages, done = self.st.checkpoint("TRUNCATE")
            self.assertEqual(busy, 1, "expected a blocked checkpoint")
            self.assertGreater(log_pages, 0)
        finally:
            other.rollback()
            other.close()

    def test_busy_does_not_mean_nothing_happened(self):
        """Written expecting busy=1 to imply checkpointed=0. It does not: a
        blocked TRUNCATE copied 207 pages and then failed to RESET the log,
        because resetting needs a lock the reader holds.

        The distinction matters for the log line. "Not run" would have been
        wrong; what busy actually reports is that the WAL was not left in the
        state the mode asked for."""
        for i in range(400):
            self.st.set_kraken_state("k%d" % i, "v" * 500)
        other = sqlite3.connect(self.st._path)
        other.execute("BEGIN")
        other.execute("SELECT COUNT(*) FROM blocks").fetchone()
        try:
            busy, log_pages, done = self.st.checkpoint("TRUNCATE")
            self.assertEqual(busy, 1)
            self.assertGreater(done, 0,
                               "a blocked checkpoint still copies pages across")
            wal = self.st._path + "-wal"
            self.assertGreater(os.path.getsize(wal), 0,
                               "but the log is NOT truncated — that is what busy means")
        finally:
            other.rollback()
            other.close()

    def test_an_unknown_mode_is_rejected(self):
        """SQLite does NOT error on PRAGMA wal_checkpoint(NONSENSE) -- it
        silently does a PASSIVE one. A typo would degrade a TRUNCATE to a no-op
        with nothing said anywhere."""
        with self.assertRaises(ValueError):
            self.st.checkpoint("TRUNCATEE")
        with self.assertRaises(ValueError):
            self.st.checkpoint("")

    def test_sqlite_really_does_swallow_a_bad_mode(self):
        """The premise of the check above, pinned. If a future SQLite starts
        erroring on this, the validation becomes belt-and-braces rather than
        load-bearing, and this test says so."""
        row = self.st._conn.execute("PRAGMA wal_checkpoint(NONSENSE)").fetchone()
        self.assertIsNotNone(row, "expected a silent PASSIVE, not an error")

    def test_mode_is_case_insensitive(self):
        self.assertEqual(len(self.st.checkpoint("truncate")), 3)


class JournalSizeLimit(unittest.TestCase):

    def test_the_limit_is_set_on_the_connection(self):
        st = _mk_store()
        self.assertEqual(
            st._conn.execute("PRAGMA journal_size_limit").fetchone()[0],
            4194304)

    def test_it_lets_an_inflated_wal_recover_unattended(self):
        """The behaviour the limit exists for, end to end: inflate the WAL
        behind an open reader, release it, keep writing, and watch the file come
        back down without anyone intervening.

        Run against a bare sqlite connection rather than a BlockStore so the
        comparison against an unlimited connection is exact.
        """
        def run(limit):
            path = tempfile.mktemp(suffix=".db")
            w = sqlite3.connect(path)
            w.executescript("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")
            if limit is not None:
                w.execute("PRAGMA journal_size_limit=%d" % limit)
            w.execute("CREATE TABLE t (x INTEGER, pad TEXT)")
            w.commit()
            r = sqlite3.connect(path)
            r.execute("BEGIN")
            r.execute("SELECT COUNT(*) FROM t").fetchone()
            for i in range(3000):
                w.execute("INSERT INTO t VALUES (?,?)", (i, "x" * 2000))
                w.commit()
            inflated = os.path.getsize(path + "-wal")
            r.rollback()
            r.close()
            for i in range(1500):
                w.execute("INSERT INTO t VALUES (?,?)", (i, "x" * 2000))
                w.commit()
            after = os.path.getsize(path + "-wal")
            w.close()
            return inflated, after

        inflated_a, after_a = run(None)
        inflated_b, after_b = run(4 * 1024 * 1024)

        self.assertGreater(inflated_a, 8 * 1024 * 1024,
                           "the reader should have inflated the WAL")
        self.assertGreater(inflated_b, 8 * 1024 * 1024)
        # Without the limit the space is never returned.
        self.assertGreater(after_a, 8 * 1024 * 1024)
        # With it, the next completed checkpoint truncates the file back.
        self.assertLess(after_b, 8 * 1024 * 1024,
                        "journal_size_limit did not reclaim the WAL")

    def test_truncation_happens_after_the_data_is_safe(self):
        """The reassurance that matters: a checkpoint copies the WAL into the
        main database BEFORE the file is truncated, so shrinking it cannot lose
        anything. Written as a round trip through a TRUNCATE (WAL goes to zero)
        with the rows still readable from a fresh connection afterwards."""
        st = _mk_store()
        for i in range(50):
            st.set_kraken_state("k%d" % i, "v%d" % i)
        busy, _, _ = st.checkpoint("TRUNCATE")
        self.assertEqual(busy, 0)
        wal = st._path + "-wal"
        self.assertEqual(os.path.getsize(wal) if os.path.exists(wal) else 0, 0,
                         "TRUNCATE should have emptied the WAL")
        fresh = sqlite3.connect(st._path)
        n = fresh.execute("SELECT COUNT(*) FROM kraken_state").fetchone()[0]
        fresh.close()
        self.assertGreaterEqual(n, 50, "rows must survive the truncation")


class ClassifierPutsItOnTheWriteSurface(unittest.TestCase):
    """`PRAGMA` is in READ_VERBS, and rightly -- most pragmas interrogate the
    connection. `wal_checkpoint` rewrites the main database file, and filing it
    as a read would put it on ReadStore: callable from any thread, no affinity
    check, which is the documented segfault."""

    def test_checkpoint_is_on_the_writer_only(self):
        from store_types import ReadStore, WriteStore
        self.assertTrue(hasattr(WriteStore, "checkpoint"))
        self.assertFalse(hasattr(ReadStore, "checkpoint"),
                         "a checkpoint on the read surface has no thread guard")

    def test_the_rule_is_what_puts_it_there(self):
        """Counterfactual: without MUTATING_PRAGMAS the classifier calls it a
        read. Asserted so the rule cannot be removed as dead weight."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_classify", os.path.join(_ROOT, ".build", "classify_store_methods.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sql = "PRAGMA wal_checkpoint(TRUNCATE)"
        self.assertEqual(mod.first_verb(sql), "PRAGMA",
                         "the bare verb is a read -- that is the trap")
        self.assertTrue(mod._mutating_pragma(sql))
        self.assertFalse(mod._mutating_pragma("PRAGMA table_info(blocks)"))
        self.assertFalse(mod._mutating_pragma("PRAGMA query_only = ON"))


if __name__ == "__main__":
    unittest.main()
