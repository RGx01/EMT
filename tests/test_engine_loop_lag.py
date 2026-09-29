"""
The engine-loop lag monitor — how the soak judges "a read blocked behind a write".

Since workstream A a read cannot wait in SQLite (WAL readers never wait on the
writer), so the one place a request can wait behind a write is the engine loop.
Second Pi soak (29 Sep 2026): the re-price sweep held it ~14 s per chunk, and
latency at the web surface could not separate that from heavy reads slowed by a
saturated Pi — a static PNG slowed x54. The monitor measures it at the source.
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


class _Reset(unittest.TestCase):
    def setUp(self):
        self._saved = dict(engine._loop_lag)
        engine._loop_lag.update(max_s=0.0, max_at=None, last_s=0.0, warnings=0)

    def tearDown(self):
        engine._loop_lag.clear()
        engine._loop_lag.update(self._saved)


class Recording(_Reset):

    def test_a_lag_over_the_line_warns(self):
        with self.assertLogs("engine", level="WARNING") as cm:
            engine._record_loop_lag(3.5)
        self.assertIn("engine loop lag: 3.50 s", cm.output[0])
        self.assertEqual(engine.engine_loop_lag()["warnings"], 1)

    def test_a_small_lag_is_kept_but_quiet(self):
        with self.assertNoLogs("engine", level="WARNING"):
            engine._record_loop_lag(0.2)
        self.assertEqual(engine.engine_loop_lag()["last_s"], 0.2)

    def test_the_worst_is_kept(self):
        for lag in (0.1, 2.0, 0.3):
            engine._record_loop_lag(lag)
        d = engine.engine_loop_lag()
        self.assertEqual((d["max_s"], d["last_s"]), (2.0, 0.3))
        self.assertIsNotNone(d["max_at"])


class OnARealLoop(_Reset):
    """Hold the loop for 1.2 s with synchronous work, as the sweep did, and the
    monitor must see it."""

    def test_it_measures_a_held_loop(self):
        saved = engine._LOOP_LAG_INTERVAL_S
        engine._LOOP_LAG_INTERVAL_S = 0.05

        async def main():
            mon = asyncio.ensure_future(engine.engine_loop_lag_monitor())
            await asyncio.sleep(0.12)
            time.sleep(1.2)                    # the synchronous chunk
            await asyncio.sleep(0.12)
            mon.cancel()
            try:
                await mon
            except asyncio.CancelledError:
                pass
        try:
            with self.assertLogs("engine", level="WARNING"):
                asyncio.run(main())
        finally:
            engine._LOOP_LAG_INTERVAL_S = saved
        self.assertGreater(engine.engine_loop_lag()["max_s"], 1.0)


class Wiring(unittest.TestCase):

    def test_started_once_from_main_not_from_engine_startup(self):
        """engine_startup re-runs on every restore and config save; a monitor
        started there would multiply."""
        main_src = open(os.path.join(_ROOT, "main.py"), encoding="utf-8").read()
        self.assertIn("engine_loop_lag_monitor()", main_src)
        tree = ast.parse(open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read())
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                    n.name in ("engine_startup", "_engine_startup_impl"):
                self.assertNotIn("engine_loop_lag_monitor", ast.unparse(n))

    def test_the_pause_state_surface_reports_it(self):
        src = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
        self.assertIn('"loop_lag": _eng.engine_loop_lag()', src)

    def test_the_soak_logs_line_matches_what_the_scanner_parses(self):
        """The two sides of one contract: engine logs it, scan-log reads it."""
        sys.path.insert(0, os.path.join(_ROOT, ".build"))
        import soak
        with self.assertLogs("engine", level="INFO") as cm:
            engine._record_loop_lag(2.5)
        line = "2026-09-29T21:00:00  WARNING  engine  " + cm.records[0].getMessage()
        self.assertTrue(soak._LAG.search(line))
        started = "engine loop lag monitor started (interval %.1f s, warn above %.1f s)" % (
            engine._LOOP_LAG_INTERVAL_S, engine._LOOP_LAG_WARN_S)
        self.assertTrue(soak._LAG_MONITOR.search(started))


if __name__ == "__main__":
    unittest.main()
