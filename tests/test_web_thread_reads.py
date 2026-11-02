"""
Reads from request threads see the CURRENT database — found by the first Pi soak.

Two failures, both silent, both from workstream A making connections thread-affine
(A §5.5) without every reader following:

1. STALE READERS AFTER A RESTORE. `store_access.store()` keeps one ReadStore per
   thread. A connection holds the file it opened, not the path, so after a restore
   renamed a new blocks.db into place every request thread kept reading the old one.
   Measured 29 Sep 2026: /api/storage-info reported the new file's size
   (35,950,592 bytes) while every read returned the old file's five days.

2. ENGINE ACCESSORS CALLED FROM REQUEST THREADS. `get_data_source_mode()`,
   `load_config()`, `get_db_account()` and others read the engine's `_store`. From
   a request thread that raises ProgrammingError, and each accessor swallowed it and
   returned its default. Measured the same day: the database said
   `data_source_mode='api'`, the settings page said "unset" and offered to
   reactivate a supplier that was connected.

   And its write-side twin: `reset_store()` called on a request thread (retire,
   unretire, import-from-zip) opened the engine's NEW connection on that thread,
   which the engine could then never use.

Every test here runs the read on a real second thread against a file-backed store,
because on one thread, or an in-memory store, none of this can happen.
"""
import ast
import concurrent.futures
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import engine                                   # noqa: E402
import store_access                             # noqa: E402
from block_store import open_block_store        # noqa: E402


def _seed(path, *, days, mode="api", account="A-TEST0001"):
    st = open_block_store(path)
    st._conn.execute("PRAGMA foreign_keys=OFF")
    for d in range(days):
        bs = "2026-09-%02dT00:00:00" % (d + 1)
        st._conn.execute("INSERT INTO blocks (block_start, block_end, config_period_id, "
                         "meter_id, imp_kwh) VALUES (?, ?, 1, 'electricity_main', 1.0)",
                         (bs, bs))
    st._conn.commit()
    st.set_kraken_state("data_source_mode", mode)
    st.set_kraken_state("billing_source", "cad")
    st.set_kraken_state("kraken_account_number", account)
    return st


class _EngineOnThisThread(unittest.TestCase):
    """This thread plays the engine thread; `self.web` is a persistent request
    thread, like one of waitress's."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "blocks.db")
        self._saved = (engine._store, engine.BLOCKS_DB_PATH)
        engine._store = _seed(self.path, days=5)
        engine.BLOCKS_DB_PATH = self.path
        store_access.reset_for_tests()
        store_access.bind(self.path)
        self.web = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    def tearDown(self):
        self.web.submit(lambda: store_access.store().close()).result()
        self.web.shutdown()
        engine._store.close()
        engine._store, engine.BLOCKS_DB_PATH = self._saved
        store_access.reset_for_tests()
        self.tmp.cleanup()

    def on_web(self, fn, *a):
        return self.web.submit(fn, *a).result(timeout=10)


class EngineAccessorsFromARequestThread(_EngineOnThisThread):

    def test_data_source_mode(self):
        self.assertEqual(self.on_web(engine.get_data_source_mode), "api")

    def test_mode_uses_api(self):
        self.assertTrue(self.on_web(engine.mode_uses_api))

    def test_billing_source(self):
        self.assertEqual(self.on_web(engine._get_billing_source), "cad")

    def test_db_account(self):
        self.assertEqual(self.on_web(engine.get_db_account), "A-TEST0001")

    def test_settle_history_in_progress_reads_the_database(self):
        """The settle sweep (the history reprice sweep until engine step 4e) has a schedule to
        price from and has not walked the history; the point is that it answers from the
        database rather than raising."""
        from unittest import mock

        class _S:
            def is_empty(self): return False
        with mock.patch.object(engine, "_kraken_rate_schedules", {"import": _S()}):
            self.assertIs(self.on_web(engine.settle_history_in_progress), True)

    def test_import_gaps_does_not_write_from_a_request_thread(self):
        import json
        engine._store.set_kraken_state("import_gaps_import", json.dumps(
            [{"from": "2026-09-01T00:00:00", "to": "2026-09-01T00:00:00", "count": 1}]))
        out = self.on_web(engine.api_import_gaps)
        self.assertEqual(out["channels"]["import"]["gap_count"], 0, "filled gap is pruned")
        self.assertIn('"count": 1', engine._store.get_kraken_state("import_gaps_import"),
                      "…but the prune is not persisted off the engine thread")
        engine.api_import_gaps()                   # engine thread: now it persists
        self.assertEqual(engine._store.get_kraken_state("import_gaps_import"), "[]")

    def test_the_engine_thread_still_uses_the_engines_store(self):
        self.assertIs(engine._reader(), engine._store)


class ReadersFollowARestore(_EngineOnThisThread):

    def _count(self):
        return store_access.store()._conn.execute(
            "SELECT COUNT(*) FROM blocks").fetchone()[0]

    def test_a_request_thread_sees_the_restored_file(self):
        self.assertEqual(self.on_web(self._count), 5)
        # the restore, in _restore_worker's order: checkpoint and close the engine's
        # store, remove the WAL files, rename the new database in, reset_store.
        # The request thread's reader is still open throughout — that is the bug.
        new = os.path.join(self.tmp.name, "restored.db")
        _seed(new, days=40, mode="api+mini")._conn.close()
        engine._store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        engine._store.close()
        for ext in ("-wal", "-shm"):
            if os.path.exists(self.path + ext):
                os.remove(self.path + ext)
        os.replace(new, self.path)
        engine.reset_store()
        self.assertEqual(self.on_web(self._count), 40)
        self.assertEqual(self.on_web(engine.get_data_source_mode), "api+mini")

    def test_nothing_reopens_without_a_swap(self):
        a = self.on_web(store_access.store)
        b = self.on_web(store_access.store)
        self.assertIs(a, b, "per-request freshness is not a reconnect")


class ResetStoreBelongsToTheEngineThread(_EngineOnThisThread):

    def test_off_the_engine_thread_it_refuses(self):
        with self.assertRaises(store_access.WrongThread):
            self.on_web(engine.reset_store)

    def test_no_route_calls_it_directly(self):
        """Every reset_store() call in server.py is inside a coroutine run on the
        engine loop, or is the no-loop fallback of the one helper that hops."""
        tree = ast.parse(open(os.path.join(_ROOT, "web", "server.py"),
                              encoding="utf-8").read())
        parent = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
        bad = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "reset_store":
                p = n
                while p in parent and not isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    p = parent[p]
                ok = isinstance(p, ast.AsyncFunctionDef) or getattr(p, "name", "") in (
                    "_reset_engine_store", "_restore_worker")
                if not ok:
                    bad.append((n.lineno, getattr(p, "name", "?")))
        self.assertEqual(bad, [])

    def test_the_engine_mutators_are_not_called_from_request_threads(self):
        src = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
        for fn in ("set_data_source_mode",):
            self.assertNotIn("_eng.%s(" % fn, src)
            self.assertIn("_engine_call(_eng.%s," % fn, src)


if __name__ == "__main__":
    unittest.main()
