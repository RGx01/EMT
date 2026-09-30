"""
A slow commit says so — and the chart render that stalled the loop is gone.

Fourth Pi soak (30 Sep 2026), 63 watchdog dumps of the stalled engine loop:

  * 17 had the engine thread inside `generate_charts` — reached from
    `server._regen_charts_safely`, which ran the render ON the loop by design (v4's
    fix for one shared connection). A restore's regen held the loop 492 s. That was
    moved off the loop (#12) and then deleted with the pre-rendered charts (C,
    30 Sep 2026) — tests/test_no_prerender.py holds that nothing renders now.
  * Most of the rest were COMMITS on the engine thread (`with self._conn:` in
    set_block_segments, save_current_block, record_dispatch_history, ...) taking
    5-95 s — storage, with SQLite's in-commit auto-checkpoint the prime suspect.
    `_TimedConnection` logs any commit over SLOW_COMMIT_S with the WAL size before
    and after, so the next soak can show whether a checkpoint ran inside it.
"""
import os
import sys
import tempfile
import time
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import block_store                               # noqa: E402
from block_store import BlockStore, open_block_store   # noqa: E402

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
