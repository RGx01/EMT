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
     (run_prune_stale_tombstones), deferring to a running delete.
  3. The sweep sizes chunks by time (_SWEEP_SLICE_S), not by count.
"""
import ast
import asyncio
import os
import sys
import time
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

    def test_deleted_ranges(self):
        self.assertEqual(_hops(_route("/api/blocks/deleted-ranges")), [])

    def test_deleted_ranges_writes_nothing(self):
        src = ast.unparse(_route("/api/blocks/deleted-ranges"))
        self.assertNotIn("prune_stale_deleted_ranges", src)
        self.assertIn("stale_deleted_range_ids", src)


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


class ChunkSizing(unittest.TestCase):

    def test_an_overrun_shrinks_in_proportion(self):
        self.assertEqual(engine._next_sweep_chunk(500, 14.0, 500), 8)

    def test_a_fast_chunk_doubles_up_to_the_ceiling(self):
        self.assertEqual(engine._next_sweep_chunk(25, 0.01, 500), 50)
        self.assertEqual(engine._next_sweep_chunk(400, 0.01, 500), 500)

    def test_within_budget_it_holds(self):
        self.assertEqual(engine._next_sweep_chunk(40, 0.2, 500), 40)

    def test_never_below_one(self):
        self.assertEqual(engine._next_sweep_chunk(1, 60.0, 500), 1)


class _Exc:
    def __init__(self, v): self.v = v
    def resolve(self, ts): return self.v
    def flat_rate(self): return self.v


class _Sched:
    def __init__(self, inc, exc): self._inc = inc; self.exc = _Exc(exc)
    def resolve(self, ts): return self._inc
    def flat_rate(self): return self._inc
    def is_empty(self): return False


class TheSweepYieldsTheLoop(unittest.TestCase):
    """Pi speed simulated: each block costs 5 ms, so a 200-row chunk would hold the
    loop a full second. A ticker on the same loop records its longest wait."""

    PER_BLOCK_S = 0.005
    N = 200

    def setUp(self):
        C.reset_for_tests()
        self._saved = (engine._store, engine._kraken_rate_schedules,
                       engine._build_channel_rate_segs, engine._reprice_history_blocks)
        exc = round(0.30 / 1.05, 8)
        engine._kraken_rate_schedules = {"import": _Sched(0.30, exc)}
        segs = [("2000-01-01T00:00:00", None, _Sched(0.30, exc))]

        async def _fake_segs(ch):
            return segs
        engine._build_channel_rate_segs = _fake_segs
        real = self._saved[3]

        def _slow(rows, *a, **kw):
            time.sleep(self.PER_BLOCK_S * len(rows))
            return real(rows, *a, **kw)
        engine._reprice_history_blocks = _slow
        st = BlockStore(":memory:")
        with st._conn:
            cp = st._conn.execute(
                "INSERT INTO config_periods (effective_from, billing_day, block_minutes, "
                "timezone, currency_symbol, currency_code) "
                "VALUES ('2024-01-01T00:00:00',1,30,'UTC','£','GBP')").lastrowid
            st._conn.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter, "
                             "meter_type) VALUES (?, 'electricity_main', 0, '')", (cp,))
            for i in range(self.N):
                bs = "2025-03-%02dT%02d:%02d:00" % (1 + i // 48, (i % 48) // 2, 30 * (i % 2))
                st._conn.execute(
                    "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                    "imp_kwh, imp_rate, imp_cost, source) VALUES (?, ?, 'electricity_main', ?, "
                    "1.0, 0.30, 0.30, 'imported_api')", (bs, bs, cp))
        engine._store = self.st = st

    def tearDown(self):
        (engine._store, engine._kraken_rate_schedules,
         engine._build_channel_rate_segs, engine._reprice_history_blocks) = self._saved
        self.st._conn.close()
        C.reset_for_tests()

    def test_longest_hold_stays_near_the_budget(self):
        gaps = []

        async def ticker(stop):
            last = time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(0.005)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        async def main():
            stop = asyncio.Event()
            t = asyncio.ensure_future(ticker(stop))
            await asyncio.sleep(0)
            await engine._run_historical_reprice_sweep()
            stop.set()
            await t

        asyncio.run(main())
        self.assertEqual(self.st.count_blocks_missing_segments(), 0, "it still finishes")
        # 0.6 s: twice the 0.25 s slice (one chunk may overrun before it is resized),
        # plus scheduling slack. A literal, not engine._SWEEP_SLICE_S, so this measures
        # the same thing on a tree that predates the budget. There, 200 blocks at 5 ms
        # go in one chunk: ~1 s.
        self.assertLess(max(gaps), 0.6, "the sweep held the loop %.2f s" % max(gaps))


if __name__ == "__main__":
    unittest.main()
