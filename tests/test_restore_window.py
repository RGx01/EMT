"""
#56 and #57 (3 Oct 2026) — a restore while the re-price sweep runs, on the Pi soak. (The history
re-price sweep went in engine step 4e; the settle sweep that replaced it is tested here.)

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


class _Sched:
    """A published tariff at one rate, in pence/kWh as kraken_rates.RateSchedule answers."""
    def is_empty(self): return False
    def resolve(self, ts): return 30.0
    def day_rate_bounds(self, ts): return (30.0, 30.0)
    def off_peak_rate_near(self, ts): return 30.0
    def is_off_peak(self, ts): return False


class TheSweepStandsAside(unittest.TestCase):
    """The settle sweep (engine step 4d; the history reprice sweep until 4e) over five days of
    half-hours with no segments, with a restore. It writes a day per transaction and checks
    admission before each."""

    DAYS = 5

    def setUp(self):
        C.reset_for_tests()
        self._saved = (engine._store, engine._kraken_rate_schedules, engine._kraken_discovery,
                       engine._SETTLE_HISTORY_PACE_S, engine.settle_range)
        engine._kraken_rate_schedules = {"import": _Sched()}
        engine._kraken_discovery = {}
        engine._SETTLE_HISTORY_PACE_S = 0
        st = BlockStore(":memory:")
        with st._conn:
            cp = st._conn.execute(
                "INSERT INTO config_periods (effective_from, billing_day, block_minutes, "
                "timezone, currency_symbol, currency_code) "
                "VALUES ('2024-01-01T00:00:00',1,30,'UTC','£','GBP')").lastrowid
            st._conn.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter, "
                             "meter_type) VALUES (?, 'electricity_main', 0, '')", (cp,))
            for d in range(self.DAYS):
                for h in range(0, 48, 12):
                    bs = "2025-03-%02dT%02d:%02d:00" % (1 + d, h // 2, 30 * (h % 2))
                    st._conn.execute(
                        "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                        "imp_kwh, imp_rate, imp_cost, source) VALUES (?, ?, 'electricity_main', "
                        "?, 1.0, 0.30, 0.30, 'imported_api')", (bs, bs, cp))
        engine._store = self.st = st
        self.N = self.DAYS * 4

    def tearDown(self):
        (engine._store, engine._kraken_rate_schedules, engine._kraken_discovery,
         engine._SETTLE_HISTORY_PACE_S, engine.settle_range) = self._saved
        C.reset_for_tests()

    def _missing(self):
        return self.st._conn.execute(
            "SELECT COUNT(*) FROM blocks b WHERE NOT EXISTS (SELECT 1 FROM block_segments s "
            "WHERE s.block_start = b.block_start AND s.meter_id = b.meter_id)").fetchone()[0]

    def _sweep(self):
        return asyncio.run(engine._run_settle_history_sweep())

    def test_a_restore_already_running_stops_it_before_a_day(self):
        _restore_running()
        self._sweep()
        self.assertEqual(self._missing(), self.N)

    def test_a_restore_mid_sweep_stops_it_at_the_next_day(self):
        real = engine.settle_range
        calls = []

        def _then_restore(store, a, b):
            calls.append(a)
            out = real(store, a, b)
            if len(calls) == 1:
                _restore_running()                 # a restore starts during day one
            return out
        engine.settle_range = _then_restore
        self._sweep()
        self.assertEqual(len(calls), 1, "it settled a day after the restore started")
        self.assertGreater(self._missing(), 0)

    def test_it_resumes_once_the_restore_is_done(self):
        """GUARD: from its cursor."""
        _restore_running()
        self._sweep()
        C.mark("restore", status="done")
        self._sweep()
        self.assertEqual(self._missing(), 0)

    def test_the_other_long_writers_defer_to_a_restore(self):
        _restore_running()
        for member in ("settle_sweep", "carbon_backfill", "measured_drain"):
            self.assertNotEqual(C.admission(member)[0], C.ADMIT, member)

    def test_with_no_store_it_returns(self):
        """GUARD (#57): a pass that starts while the restore has the engine store closed stops."""
        engine._store = None
        try:
            self.assertEqual(self._sweep(), 0)
        finally:
            engine._store = self.st


class AConnectionInUse(unittest.TestCase):
    """#57: a swap retires a thread's connection instead of closing it mid-request."""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "blocks.db")
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
