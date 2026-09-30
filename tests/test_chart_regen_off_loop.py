"""
Chart regeneration never runs on the engine loop, and a slow commit says so.

Fourth Pi soak (30 Sep 2026), 63 watchdog dumps of the stalled engine loop:

  * 17 had the engine thread inside `generate_charts` — reached from
    `server._regen_charts_safely`, which ran the render ON the loop by design (v4's
    fix for one shared connection). A restore's regen held the loop 492 s.
  * Most of the rest were COMMITS on the engine thread (`with self._conn:` in
    set_block_segments, save_current_block, record_dispatch_history, ...) taking
    5-95 s — storage, with SQLite's in-commit auto-checkpoint the prime suspect.
    `_TimedConnection` logs any commit over SLOW_COMMIT_S with the WAL size before
    and after, so the next soak can show whether a checkpoint ran inside it.
"""
import ast
import asyncio
import os
import sys
import tempfile
import threading
import time
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import block_store                               # noqa: E402
import engine                                    # noqa: E402
from block_store import BlockStore, open_block_store   # noqa: E402

_SERVER = os.path.join(_ROOT, "web", "server.py")


def _fn(tree, name):
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _calls(fn):
    return {getattr(c.func, "attr", getattr(c.func, "id", None))
            for c in ast.walk(fn) if isinstance(c, ast.Call)}


class RegenChartsSafelyIsOffTheLoop(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(open(_SERVER, encoding="utf-8").read())

    def test_it_renders_nothing_itself(self):
        """REGRESSION: on dev it called generate_charts inside a coroutine it ran
        on the engine loop."""
        fn = _fn(self.tree, "_regen_charts_safely")
        self.assertNotIn("generate_charts", _calls(fn))
        self.assertIn("_schedule_offloaded_regen", _calls(fn))

    def test_the_path_it_takes_hands_off_to_the_offloader(self):
        """GUARD: _schedule_offloaded_regen schedules the engine's
        _schedule_chart_regen on the loop, and renders inline only with no loop."""
        fn = _fn(self.tree, "_schedule_offloaded_regen")
        src = ast.unparse(fn)
        self.assertIn("_schedule_chart_regen()", src)
        self.assertIn("run_coroutine_threadsafe", src)


class TheOffloadedRenderRunsInAWorkerThread(unittest.TestCase):
    """GUARD (unchanged engine behaviour, and what the server now relies on):
    scheduling a regen from the loop renders on a different thread, against a
    read-only store of its own."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "blocks.db")
        self._saved = (engine._store, engine.BLOCKS_DB_PATH, engine.generate_charts,
                       engine.load_config)
        engine._store = open_block_store(self.path)
        engine.BLOCKS_DB_PATH = self.path
        engine.load_config = lambda: {"meters": {}}
        self.seen = []

        def _record(store, config=None):
            self.seen.append((threading.get_ident(), getattr(store, "_conn", None)))
        engine.generate_charts = _record

    def tearDown(self):
        engine._store.close()
        (engine._store, engine.BLOCKS_DB_PATH, engine.generate_charts,
         engine.load_config) = self._saved
        self.tmp.cleanup()

    def test_the_render_is_not_on_the_loop_thread(self):
        async def main():
            loop_thread = threading.get_ident()
            engine._schedule_chart_regen()
            for _ in range(200):
                if self.seen:
                    break
                await asyncio.sleep(0.01)
            return loop_thread
        loop_thread = asyncio.run(main())
        self.assertTrue(self.seen, "the render ran")
        self.assertNotEqual(self.seen[0][0], loop_thread)


class SlowCommitsAreReported(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = BlockStore(os.path.join(self.tmp.name, "blocks.db"))
        self._saved = block_store.SLOW_COMMIT_S

    def tearDown(self):
        block_store.SLOW_COMMIT_S = self._saved
        self.st._conn.close()
        self.tmp.cleanup()

    def test_the_engine_store_uses_the_timed_connection(self):
        self.assertIsInstance(self.st._conn, block_store._TimedConnection)

    def test_a_slow_commit_names_the_method_that_made_it(self):
        block_store.SLOW_COMMIT_S = 0.0          # every commit counts as slow
        with self.assertLogs("block_store", level="WARNING") as cm:
            self.st.set_meta("soak_probe", 1)
        self.assertTrue(any("slow commit:" in m and "in set_meta" in m for m in cm.output),
                        cm.output)

    def test_a_with_block_commit_is_timed_too(self):
        """`with self._conn:` commits through __exit__, not commit()."""
        block_store.SLOW_COMMIT_S = 0.0

        def _write():
            with self.st._conn:
                self.st._conn.execute("INSERT INTO store_meta (key, value) VALUES ('k', '1')")
        with self.assertLogs("block_store", level="WARNING") as cm:
            _write()
        self.assertTrue(any("in _write" in m for m in cm.output), cm.output)

    def test_a_fast_commit_is_silent(self):
        block_store.SLOW_COMMIT_S = 60.0
        with self.assertNoLogs("block_store", level="WARNING"):
            self.st.set_meta("soak_probe", 2)

    def test_a_wal_that_shrank_is_reported_as_a_checkpoint(self):
        with self.assertLogs("block_store", level="WARNING") as cm:
            self.st._conn._report(time.monotonic() - 5.0, 50 * 1048576, "save_current_block")
        self.assertIn("a checkpoint ran in this commit", cm.output[0])
        self.assertIn("in save_current_block", cm.output[0])

    def test_an_in_memory_store_is_unaffected(self):
        st = BlockStore(":memory:")
        block_store.SLOW_COMMIT_S = 0.0
        with self.assertLogs("block_store", level="WARNING") as cm:
            st.set_meta("x", 1)
        self.assertIn("WAL ? -> ?", cm.output[0])
        st._conn.close()


if __name__ == "__main__":
    unittest.main()
