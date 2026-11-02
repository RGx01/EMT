"""
Workstream A acceptance 1, 2 and 5 — the read/write split itself.

A §1's finding is that v4 already HAD a safe read path and nobody used it: the
companion was built three times after three incidents and reached 0% adoption.
So the tests that matter here are not "does ReadStore work" but "is the unsafe
thing genuinely unreachable" — the two properties that make the split structural
rather than a naming convention:

  1. a read path cannot write, because the attribute is absent;
  2. the writer cannot be reached from the wrong thread.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import store_access                                    # noqa: E402
from block_store import BlockStore                     # noqa: E402
from store_types import ReadStore, WriteStore          # noqa: E402

_BASE = os.path.join(_ROOT, ".build", "store_split_baseline.json")
_GEN = os.path.join(_ROOT, ".build", "gen_store_types.py")


def _tmpdb():
    return os.path.join(tempfile.mkdtemp(), "t.db")


class TestAcceptance1_NoReadPathCanWrite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(_BASE, encoding="utf-8") as fh:
            cls.base = json.load(fh)

    def test_no_write_method_appears_on_readstore(self):
        """The invariant is ONE-WAY. `WriteStore` must be able to read — 40 of
        the 93 writers (43%) run their own SELECT or call a read method — so the
        two types are not disjoint. What must never happen is a writer showing up
        on the read type."""
        # Dunders are excluded: `__init__` is classified a writer because
        # BlockStore's builds the schema, but ReadStore defines its OWN, which is
        # the read-only connection. Comparing them would be comparing names, not
        # behaviour — the leak that matters is a callable BlockStore writer.
        surface = {m for m in dir(ReadStore) if not m.startswith("__")}
        leaked = sorted({w for w in self.base["write"] if not w.startswith("__")}
                        & surface)
        self.assertEqual(leaked, [],
                         "write methods reachable from a read path: %s" % leaked)

    def test_every_read_method_is_present(self):
        missing = sorted(set(self.base["read"]) - set(dir(ReadStore)))
        self.assertEqual(missing, [],
                         "a read path would have to reach for the writer to get: "
                         "%s" % missing)

    def test_readstore_is_a_subset_of_the_writer(self):
        pub = lambda t: {m for m in dir(t) if not m.startswith("_")}
        self.assertTrue(pub(ReadStore) <= pub(WriteStore),
                        "the writer must be able to do anything a reader can")

    def test_there_is_no_getattr_escape_hatch(self):
        """A catch-all forwarder would restore every attribute the type exists to
        remove, and would do it invisibly."""
        self.assertFalse(hasattr(ReadStore, "__getattr__"))
        r = ReadStore(_tmpdb_with_schema())
        with self.assertRaises(AttributeError):
            r.set_meta("x", 1)

    def test_the_connection_refuses_writes_too(self):
        """Defence in depth: the type stops it at the call site, `query_only`
        stops it at SQLite. v4 had only the second, which is why the wrong path
        was reachable at all."""
        import sqlite3
        r = ReadStore(_tmpdb_with_schema())
        with self.assertRaises(sqlite3.OperationalError):
            r._conn.execute("INSERT INTO store_meta (key, value) VALUES ('x','1')")

    def test_readstore_has_no_fallback_to_a_writable_store(self):
        """v4's `_get_read_store()` ends `return _get_store()` when it cannot open
        its own connection. A safe call that can hand back the unsafe object is
        not a boundary."""
        src = open(os.path.join(_ROOT, "store_access.py"), encoding="utf-8").read()
        body = src.split("def store()")[1].split("def writer()")[0]
        self.assertNotIn("WriteStore(", body)
        self.assertNotIn("writer()", body)


class TestAcceptance2_WriterIsThreadAffine(unittest.TestCase):

    def setUp(self):
        store_access.reset_for_tests()
        store_access.bind(_tmpdb_with_schema())

    def tearDown(self):
        store_access.reset_for_tests()

    def test_writer_on_the_engine_thread_works(self):
        store_access.writer().set_meta("k", "v")
        self.assertEqual(store_access.store().get_meta("k"), "v")

    def test_writer_from_another_thread_raises(self):
        caught = []

        def worker():
            try:
                store_access.writer()
            except Exception as e:
                caught.append(e)

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        self.assertEqual(len(caught), 1)
        self.assertIsInstance(caught[0], store_access.WrongThread)

    def test_reading_from_another_thread_is_fine(self):
        store_access.writer().set_meta("k", "cross-thread")
        got = []
        t = threading.Thread(target=lambda: got.append(store_access.store().get_meta("k")))
        t.start()
        t.join()
        self.assertEqual(got, ["cross-thread"])

    def test_each_thread_gets_its_own_read_store(self):
        seen = []
        main = store_access.store()
        t = threading.Thread(target=lambda: seen.append(store_access.store()))
        t.start()
        t.join()
        self.assertIsNot(main, seen[0])


class TestAcceptance6_NoStaleSnapshot(unittest.TestCase):
    """§7.1: a reader must never serve a stale snapshot.

    FILE-BACKED ON PURPOSE. An in-memory database has no WAL, therefore no
    snapshots, therefore nothing for this test to catch — it would pass whether
    or not the implementation were right. §7 question 2 spells that out; this is
    the test it was spelled out for.

    The failure guarded against has no symptom: a long-lived connection that
    never ends its read transaction pins the snapshot it first saw, so the page
    renders coherent, internally consistent, STALE numbers and looks healthy
    doing it. No error, no torn row, just yesterday's figures served with
    confidence.

    Honest about its own strength: the two freshness assertions below pass with
    `isolation_level=None` REMOVED, because Python's sqlite3 does not begin a
    transaction for a SELECT. They guard the property, not the setting. The third
    test asserts the mechanism directly, and that one can fail.

    (An earlier version of this docstring said the port had shipped this bug. It
    had not — that failure was a fixture binding the wrong store.)
    """

    def setUp(self):
        store_access.reset_for_tests()
        self.path = _tmpdb()
        BlockStore(self.path).close()          # real file, WAL
        store_access.bind(self.path)

    def tearDown(self):
        store_access.reset_for_tests()

    def test_the_fixture_really_is_wal(self):
        """If this ever reports anything but 'wal', the test below has quietly
        stopped testing what it says it does."""
        mode = store_access.store()._conn.execute(
            "PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(str(mode).lower(), "wal")

    def test_a_reader_sees_a_write_committed_after_it_first_read(self):
        r = store_access.store()
        self.assertIsNone(r.get_meta("later", None))     # opens the read
        store_access.writer().set_meta("later", "committed")
        self.assertEqual(store_access.store().get_meta("later", None), "committed",
                         "the reader is pinned to the snapshot it opened with")

    def test_a_read_leaves_no_open_transaction(self):
        """The mechanism, asserted directly — this is the falsifiable one.

        A pinned snapshot needs an open read transaction. If the store ever
        starts holding one (an explicit BEGIN, a pooling wrapper, a driver
        change), this fails here rather than as a page of stale numbers that
        looks perfectly healthy.
        """
        r = store_access.store()
        r.get_meta("anything", None)
        self.assertFalse(r._conn.in_transaction,
                         "the read store is holding a transaction open — it will "
                         "pin the WAL snapshot and serve stale reads silently")

    def test_it_holds_across_several_writes(self):
        r = store_access.store()
        for i in range(3):
            store_access.writer().set_meta("n", i)
            self.assertEqual(r.get_meta("n", None), i)


class TestAcceptance5_CheckSameThread(unittest.TestCase):
    def test_there_is_exactly_one_left_in_the_codebase(self):
        """§5.5: `check_same_thread=False` is what made the whole class of bug
        expressible, so the last one must not quietly become two.

        Found by AST, as an actual keyword ARGUMENT — a text search matches the
        five places that merely discuss it in a comment or docstring (including
        this module's own), which would make the census permanently red and
        therefore ignored.
        """
        import ast
        hits = []
        for dirpath, dirnames, files in os.walk(_ROOT):
            dirnames[:] = [d for d in dirnames
                           if d not in (".git", "__pycache__", "tests", "docs",
                                        ".build", "scratch", "screenshots")]
            for f in sorted(files):
                if not f.endswith(".py"):
                    continue
                path = os.path.join(dirpath, f)
                with open(path, encoding="utf-8") as fh:
                    try:
                        tree = ast.parse(fh.read())
                    except SyntaxError:
                        continue
                for n in ast.walk(tree):
                    if isinstance(n, ast.Call):
                        for kw in n.keywords:
                            if kw.arg == "check_same_thread":
                                hits.append("%s:%d" % (os.path.relpath(path, _ROOT),
                                                       n.lineno))
        # Was `["block_store.py:1001"]` — the single remaining occurrence, which
        # acceptance 5 existed to retire. Retired 24 Sep 2026; the flag is now
        # absent everywhere, and tests/test_thread_affinity.py owns the guard
        # (including the demonstration that a cross-thread use actually raises).
        # Kept here as well because this census is where the count lived while it
        # was going down, and an empty list is the honest end state of it.
        self.assertEqual(sorted(hits), [],
                         "check_same_thread reappeared: %s" % sorted(hits))


class TestGeneratorIsCurrent(unittest.TestCase):
    def test_store_types_matches_the_classifier(self):
        """`store_types.py` is generated. If BlockStore gains a method and this
        is not regenerated, the method is silently unavailable to every read path
        — and the obvious workaround is to reach for the writer."""
        r = subprocess.run([sys.executable, _GEN, "--check"],
                           capture_output=True, text=True, cwd=_ROOT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


def _tmpdb_with_schema():
    p = _tmpdb()
    BlockStore(p).close()
    return p


if __name__ == "__main__":
    unittest.main()
