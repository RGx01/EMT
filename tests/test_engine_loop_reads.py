"""
Reads do not wait behind the engine loop, and the sweep does not hold it for long.

Measured on the second Pi soak (29 Sep 2026), 37,103-block re-price sweep:

    route                        quiet p50   during sweep p50
    GET /api/config                 0.02 s        15.0 s
    GET /api/blocks/deleted-ranges  0.02 s        16.9 s
    pure database reads (median)                  x1.9

The two outliers were the only GET routes that reached the DATABASE through the
engine loop, and the sweep held that loop ~14 s per 500-block chunk (36 blocks/s
on the Pi). Three fixes, each pinned here:

  1. GET /api/config reads on the request thread (load_config reads through
     engine._reader() since the stale-readers fix).
  2. GET /api/blocks/deleted-ranges no longer prunes: it hides stale tombstones
     with the read-only stale_deleted_range_ids(); the engine prunes hourly
     (run_prune_stale_tombstones), deferring to a running delete. (The route went with
     the Delete Blocks page on 21 Oct 2026; its tests with it.)
  3. The sweep sized chunks by time (_SWEEP_SLICE_S), not by count — gone with the history
     reprice sweep in engine step 4e; the settle sweep writes a day per transaction.
"""
import ast
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import engine                                   # noqa: E402
import job_coordinator as C                     # noqa: E402
from block_store import BlockStore              # noqa: E402

_SERVER = os.path.join(_ROOT, "web", "server.py")


def _route(path, method="GET"):
    tree = ast.parse(open(_SERVER, encoding="utf-8").read())
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in n.decorator_list:
                if isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "route" \
                        and d.args and d.args[0].value == path:
                    ms = ["GET"]
                    for kw in d.keywords:
                        if kw.arg == "methods":
                            ms = [e.value for e in kw.value.elts]
                    if method in ms:
                        return n
    raise AssertionError("no %s %s" % (method, path))


_HOPS = ("_run_on_engine_loop", "_engine_write", "_engine_call",
         "_config_mutation_on_loop", "run_coroutine_threadsafe")


def _hops(fn):
    return sorted({getattr(c.func, "id", getattr(c.func, "attr", "")) for c in ast.walk(fn)
                   if isinstance(c, ast.Call)
                   and getattr(c.func, "id", getattr(c.func, "attr", "")) in _HOPS})


class TheseGetsDoNotWaitOnTheEngineLoop(unittest.TestCase):

    def test_config(self):
        self.assertEqual(_hops(_route("/api/config")), [])


def _tomb_store():
    st = BlockStore(":memory:")
    st._conn.execute("INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, "
                     "block_minutes, timezone) VALUES (1, '2020-01-01T00:00:00', 1, 30, 'UTC')")
    for b in ("2026-08-10T10:00:00", "2026-08-10T10:30:00"):
        st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id,"
                         " imp_kwh) VALUES (?, ?, 'electricity_main', 1, 0.1)", (b, b))
    st._conn.execute("INSERT INTO deleted_ranges (meter_id, start_utc, end_utc, created_at) VALUES "
                     "('electricity_main', '2026-08-10T10:00:00', '2026-08-10T11:00:00', '2026-08-10T12:00:00')")
    st._conn.execute("INSERT INTO deleted_ranges (meter_id, start_utc, end_utc, created_at) VALUES "
                     "('electricity_main', '2026-08-11T10:00:00', '2026-08-11T11:00:00', '2026-08-11T12:00:00')")
    st._conn.commit()
    return st


class StaleTombstones(unittest.TestCase):

    def setUp(self):
        C.reset_for_tests()
        self._saved = engine._store
        engine._store = self.st = _tomb_store()

    def tearDown(self):
        engine._store = self._saved
        self.st._conn.close()
        C.reset_for_tests()

    def _count(self):
        return self.st._conn.execute("SELECT COUNT(*) FROM deleted_ranges").fetchone()[0]

    def test_the_stale_check_reads_and_does_not_write(self):
        self.assertEqual(len(self.st.stale_deleted_range_ids()), 1)
        self.assertEqual(self._count(), 2)

    def test_it_is_on_the_read_type(self):
        from store_types import ReadStore
        self.assertTrue(hasattr(ReadStore, "stale_deleted_range_ids"))
        self.assertFalse(hasattr(ReadStore, "prune_stale_deleted_ranges"))

    def test_the_engine_prunes_only_the_stale_one(self):
        self.assertEqual(engine.run_prune_stale_tombstones(), {"ok": True, "pruned": 1})
        self.assertEqual(self._count(), 1)

    def test_it_defers_while_a_delete_runs(self):
        """A delete writes its tombstone before removing the blocks; a prune in
        between would delete the tombstone it just wrote."""
        C.claim("delete")
        self.assertEqual(engine.run_prune_stale_tombstones()["ok"], False)
        self.assertEqual(self._count(), 2)

    def test_the_hourly_tick_runs_it(self):
        tree = ast.parse(open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read())
        tick = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "_tick_dispatch_capture")
        called = {c.func.id for c in ast.walk(tick) if isinstance(c, ast.Call)
                  and isinstance(c.func, ast.Name)}
        self.assertIn("run_prune_stale_tombstones", called)


if __name__ == "__main__":
    unittest.main()
