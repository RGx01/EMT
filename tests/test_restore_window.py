"""
#56 and #57 (3 Oct 2026) — a restore while the re-price sweep runs, on the Pi soak.

#56. The restore paused the engine, but the sweep never read the pause: it committed
through the whole restore, and every commit restarted the safety backup's stepped copy
(3 min 50 s, a 262 MB WAL, a 118 s commit). Now the restore registers with the coordinator,
the long-running writers declare `exclusive_with=("restore",)`, the sweep re-checks
admission between chunks, and the backup copies in one step.

#57. A route takes `store_access.store()` and then calls helpers that call it again. After
a file swap, that second call closed the connection the route was still using, which gave a
500 ("Cannot operate on a closed database"). Now a swapped-out connection is retired and
closed at the end of the request. Also, a sweep pass that started while the restore had the
engine store closed raised `'NoneType' object has no attribute ...`. It now stops.

Tests marked GUARD pass on the parent tree too. The rest fail there.
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import tests.test_engine_loop_reads as _base                              # noqa: E402
engine, C = _base.engine, _base.C
import store_access                                                       # noqa: E402
from block_store import BlockStore                                        # noqa: E402


def _restore_running():
    C.claim("restore", force=True)
    C.mark("restore", status="running")


class TheSweepStandsAside(_base.TheSweepYieldsTheLoop):
    """The 200-block sweep fixture of test_engine_loop_reads, with a restore."""

    test_longest_hold_stays_near_the_budget = None      # not re-run here

    def _sweep(self):
        asyncio.run(engine._run_historical_reprice_sweep())

    def test_a_restore_already_running_stops_it_before_a_chunk(self):
        _restore_running()
        self._sweep()
        self.assertEqual(self.st.count_blocks_missing_segments(), self.N)

    def test_a_restore_mid_sweep_stops_it_at_the_next_chunk(self):
        real = engine._reprice_history_blocks
        calls = []

        def _then_restore(rows, *a, **kw):
            calls.append(len(rows))
            out = real(rows, *a, **kw)
            if len(calls) == 1:
                _restore_running()                 # a restore starts during chunk one
            return out
        engine._reprice_history_blocks = _then_restore
        self._sweep()
        self.assertEqual(len(calls), 1, "it ran a chunk after the restore started")
        self.assertGreater(self.st.count_blocks_missing_segments(), 0)

    def test_it_resumes_once_the_restore_is_done(self):
        """GUARD: the parent finishes too (it never stopped)."""
        _restore_running()
        self._sweep()
        C.mark("restore", status="done")
        self._sweep()
        self.assertEqual(self.st.count_blocks_missing_segments(), 0)

    def test_the_other_long_writers_defer_to_a_restore(self):
        _restore_running()
        for member in ("reprice_sweep", "carbon_backfill", "measured_drain"):
            self.assertNotEqual(C.admission(member)[0], C.ADMIT, member)

    def test_with_no_store_it_returns(self):
        """GUARD: `_run_historical_reprice_sweep` already returned on `_store is None`;
        the fix is in its scheduler's loop, which re-read `_store` after each pass."""
        engine._store = None
        try:
            self.assertEqual(asyncio.run(engine._run_historical_reprice_sweep()), 0)
        finally:
            engine._store = self.st


class AConnectionInUse(unittest.TestCase):
    """#57: a swap retires a thread's connection instead of closing it mid-request."""

    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        BlockStore(self.path).close()
        store_access.reset_for_tests()
        store_access.bind(self.path)

    def tearDown(self):
        getattr(store_access, "close_retired", lambda: None)()   # absent on the parent
        store_access.reset_for_tests()

    def test_a_request_keeps_its_connection_across_a_swap(self):
        held = store_access.store()               # the route's handle
        store_access.invalidate()                 # a restore swaps the file
        again = store_access.store()              # load_config() inside the same request
        self.assertIsNot(again, held)
        self.assertEqual(held._conn.execute("SELECT 1").fetchone()[0], 1)

    def test_the_end_of_the_request_closes_it(self):
        held = store_access.store()
        store_access.invalidate()
        store_access.store()
        store_access.close_retired()
        with self.assertRaises(sqlite3.ProgrammingError):
            held._conn.execute("SELECT 1")

    def test_the_next_request_reads_the_new_file(self):
        """GUARD: the parent reopened on a generation change too."""
        held = store_access.store()
        store_access.invalidate()
        self.assertIsNot(store_access.store(), held)


class TheBackupCopiesInOneStep(unittest.TestCase):
    """Structural: a stepped backup restarts on every concurrent write, so the safety
    backup must copy in one step. Checked in the source, because the restart is a timing
    effect a unit test cannot reproduce reliably. Fails on the parent tree (pages=100)."""

    def test_pages_minus_one(self):
        src = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
        self.assertIn("_src_conn.backup(_dst_conn, pages=-1)", src)
        self.assertNotIn("_src_conn.backup(_dst_conn, pages=100", src)


if __name__ == "__main__":
    unittest.main()
