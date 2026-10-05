"""
The re-price sweep commits once per chunk, not once per block (ROADMAP §A.1, 5 Oct 2026).

Each chunk is synchronous, but each block's segment write was its own transaction, and
the chunk's split, exc and cursor writes three more: on the Pi's SD card that is one fsync
per block, and every one held the engine loop (soak 15: 85 of 97 slow commits were
`set_block_segments`, worst 16.8 s). Now the chunk's writes share one `BlockStore.batch()`.

Counted with SQLite's trace callback, which sees every COMMIT the connection issues.
The commit-count test fails on the parent tree; the outcome tests are guards.
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import engine                                     # noqa: E402
import job_coordinator as C                       # noqa: E402
from block_store import BlockStore                # noqa: E402


class _Exc:
    def __init__(self, v): self.v = v
    def resolve(self, ts): return self.v
    def flat_rate(self): return self.v


class _Sched:
    def __init__(self, inc, exc): self._inc = inc; self.exc = _Exc(exc)
    def resolve(self, ts): return self._inc
    def flat_rate(self): return self._inc
    def is_empty(self): return False


_EXC = round(0.30 / 1.05, 6)
_RATE_SEGS = [("2000-01-01T00:00:00", None, _Sched(0.30, round(0.30 / 1.05, 8)))]
N = 60


def _store():
    st = BlockStore(":memory:")
    with st._conn:
        cp = st._conn.execute(
            "INSERT INTO config_periods (effective_from, billing_day, block_minutes, timezone, "
            "currency_symbol, currency_code) VALUES ('2024-01-01T00:00:00',1,30,'UTC','£','GBP')"
        ).lastrowid
        st._conn.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter, "
                         "meter_type) VALUES (?, 'electricity_main', 0, '')", (cp,))
        for i in range(N):
            bs = "2025-03-%02dT%02d:%02d:00" % (1 + i // 48, (i % 48) // 2, 30 * (i % 2))
            st._conn.execute(
                "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                "imp_kwh, imp_rate, imp_cost, source) VALUES (?,?, 'electricity_main', ?, "
                "1.0, 0.30, 0.30, 'imported_api')", (bs, bs, cp))
    return st


class OneSweepPass(unittest.TestCase):

    def setUp(self):
        C.reset_for_tests()
        self.st = _store()
        self.statements = []
        self.st._conn.set_trace_callback(lambda q: self.statements.append(q.split()[0].upper()))

        async def _segs(ch):
            return _RATE_SEGS
        self.p = [mock.patch.object(engine, "_store", self.st),
                  mock.patch.object(engine, "_kraken_rate_schedules", {"import": _Sched(0.30, _EXC)}),
                  mock.patch.object(engine, "_build_channel_rate_segs", _segs),
                  mock.patch.object(engine, "_hist_rate_segs_cache", {}),
                  mock.patch.object(engine, "_write_reprice_report", lambda **k: None)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        C.reset_for_tests()

    def _pass(self, batch):
        self.statements.clear()
        n = asyncio.run(engine._run_historical_reprice_sweep(batch=batch, pace_s=0))
        return n, self.statements.count("COMMIT")

    def test_a_chunk_commits_once(self):
        """One chunk of all N blocks: about one commit, where the parent made one per
        block plus three."""
        with mock.patch.object(engine, "_SWEEP_FIRST_CHUNK", N):
            swept, commits = self._pass(batch=N)
        self.assertEqual(swept, N)
        # the chunk's own commit, plus the done marker the pass sets at the end
        self.assertLessEqual(commits, 3, "%d commits for one chunk of %d blocks" % (commits, N))

    def test_commits_track_chunks_not_blocks(self):
        with mock.patch.object(engine, "_SWEEP_FIRST_CHUNK", 10), \
             mock.patch.object(engine, "_next_sweep_chunk", lambda limit, e, c: limit):
            swept, commits = self._pass(batch=10)
        self.assertEqual(swept, N)
        self.assertLessEqual(commits, N // 10 + 3, "%d commits for %d chunks" % (commits, N // 10))

    def test_every_block_is_still_derived(self):
        """GUARD: the outcome is unchanged — segments and exc on every block, done set."""
        self._pass(batch=N)
        self.assertEqual(self.st.count_blocks_missing_segments(), 0)
        bad = self.st._conn.execute("SELECT COUNT(*) FROM blocks WHERE imp_rate_exc IS NULL "
                                    "OR ABS(imp_rate_exc - ?) > 1e-6", (_EXC,)).fetchone()[0]
        self.assertEqual(bad, 0)
        self.assertTrue((self.st.get_meta(engine._REPRICE_HISTORY_MARKER, {}) or {}).get("done"))

    def test_a_failing_chunk_is_skipped_and_its_cursor_still_saved(self):
        """GUARD: a chunk that raises is counted as skipped, as before, and the cursor
        written in the same transaction survives."""
        with mock.patch.object(engine, "_reprice_history_blocks",
                               side_effect=RuntimeError("bad chunk")):
            self._pass(batch=N)
        state = self.st.get_meta(engine._REPRICE_HISTORY_MARKER, {}) or {}
        self.assertGreater(int(state.get("skipped") or 0), 0)


if __name__ == "__main__":
    unittest.main()
