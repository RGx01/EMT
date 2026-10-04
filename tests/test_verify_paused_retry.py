"""
A verify pass paused for API allowance is retried (4 Oct 2026).

After 120 waits (~2 h) on a low allowance, the pass returns "paused — API allowance
still low, will resume", its cursor saved and its `verify_pricing` claim held, so
attribution (which depends on it) keeps waiting. The phase has promised a resume since
v4 4.0.0; nothing resumed it while the process ran, so attribution waited until a
restart. Now the engine tick retries it from its cursor after _VERIFY_PAUSE_RETRY_S.

Tests marked GUARD hold on the parent tree too. The rest fail there.
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import engine                                     # noqa: E402
import job_coordinator as C                       # noqa: E402
from block_store import BlockStore                # noqa: E402

T0 = datetime(2026, 1, 1)
DAYS = 90
_real = engine.run_deferred_verify_pricing


def _store():
    st = BlockStore(":memory:")
    with st._conn:
        st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, "
                         "block_minutes, timezone) VALUES (1,'2025-01-01T00:00:00',1,30,'UTC')")
        for d in (0, DAYS):
            bs = (T0 + timedelta(days=d)).isoformat()
            st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, "
                             "config_period_id, imp_kwh, source) VALUES (?,?,'electricity_main',"
                             "1,1.0,'imported_api')", (bs, bs))
    st.set_meta(engine._REPRICE_HISTORY_MARKER, {"done": True})
    return st


class APausedPass(unittest.TestCase):

    def setUp(self):
        C.reset_for_tests()
        self.st = _store()
        self.chunks = []
        self.low = True                     # the allowance
        self.now = 1000.0                   # time.monotonic()

        async def _repair(c_from, c_to, **kw):
            self.chunks.append((c_from, c_to))
            return {"recovered": 0, "checked": 1, "skipped": 0}

        async def _allowance_low(headroom):
            return self.low

        async def _noop(*a, **k):
            return 0
        self.p = [mock.patch.object(engine, "_store", self.st),
                  mock.patch.object(engine, "kraken_available", lambda: True),
                  mock.patch.object(engine, "repair_import_pricing", _repair),
                  mock.patch.object(engine, "_verify_allowance_low", _allowance_low),
                  mock.patch.object(engine, "_run_historical_reprice_sweep", _noop),
                  mock.patch.object(engine, "_maybe_drain_measured_history", lambda: None),
                  mock.patch.object(engine, "_uncostable_ranges", lambda kind: []),
                  mock.patch.object(engine.time, "monotonic", lambda: self.now)]
        for x in self.p:
            x.start()
        engine._verify_job.clear()
        engine._verify_job["status"] = "idle"
        engine._verify_cursor_checked = True      # the restart path is not under test
        self._pause()

    def tearDown(self):
        for x in self.p:
            x.stop()
        engine._verify_job.clear()
        engine._verify_job["status"] = "idle"
        C.reset_for_tests()

    def _pause(self):
        """Run a real pass into "paused": the allowance never recovers."""
        asyncio.run(engine.run_deferred_verify_pricing(wait_s=0, max_wait_cycles=2))
        self.assertEqual(engine._verify_job["status"], "paused")
        self.assertEqual(self.chunks, [])

    def tick(self):
        async def _go():
            scheduled = engine._maybe_resume_deferred_verify()
            for _ in range(200):
                await asyncio.sleep(0)
                if engine._verify_job.get("status") in ("done", "paused", "error"):
                    break
            return scheduled
        return asyncio.run(_go())

    def test_attribution_waits_while_it_is_paused(self):
        """GUARD: a paused pass has not finished writing the imp_rate attribution reads."""
        self.assertEqual(C.admission("attribution")[0], C.WAIT)

    def test_it_is_retried_after_the_delay_and_finishes(self):
        self.low = False
        self.now += engine._VERIFY_PAUSE_RETRY_S + 1
        self.assertTrue(self.tick())
        self.assertEqual(engine._verify_job["status"], "done", engine._verify_job)
        self.assertEqual(len(self.chunks), 3)
        self.assertEqual(C.admission("attribution")[0], C.ADMIT, "attribution still blocked")

    def test_not_before_the_delay(self):
        """GUARD on the new retry's limits. The parent never retried, so this held
        there too (it errors on the parent only because the delay constant is new)."""
        self.low = False
        self.now += engine._VERIFY_PAUSE_RETRY_S - 1
        self.assertFalse(self.tick())
        self.assertEqual(engine._verify_job["status"], "paused")

    def test_not_while_an_import_runs(self):
        """GUARD on the new retry's limits. The parent never retried, so this held
        there too (it errors on the parent only because the delay constant is new)."""
        self.now += engine._VERIFY_PAUSE_RETRY_S + 1
        C.claim("import", force=True)
        C.mark("import", status="running")
        self.assertFalse(self.tick())
        self.assertEqual(engine._verify_job["status"], "paused")

    def test_still_low_it_pauses_again_and_retries_later(self):
        self.now += engine._VERIFY_PAUSE_RETRY_S + 1
        # The retried pass waits as briefly as the first did.
        with mock.patch.object(engine, "run_deferred_verify_pricing",
                               lambda **k: _real(wait_s=0, max_wait_cycles=2, **k)):
            self.assertTrue(self.tick())
        self.assertEqual(engine._verify_job["status"], "paused")
        self.assertEqual(self.chunks, [])
        self.low = False
        self.now += engine._VERIFY_PAUSE_RETRY_S + 1
        self.assertTrue(self.tick())
        self.assertEqual(engine._verify_job["status"], "done")


if __name__ == "__main__":
    unittest.main()
