"""
B §5 acceptance 3 (4 Oct 2026) — verify_pricing honours `depends_on=("import",)`.

The deferred pricing-verification pass re-checks imported history chunk by chunk. It
declared a dependency on the API import and never consulted it, so an import starting
mid-pass ran alongside a pass checking the history it was writing. Nothing stopped a
second pass starting while one ran, either: two imports finishing close together each
launched one, both writing `_verify_job`.

Now, DEFER: the pass stands aside at the start or at the next chunk boundary while an
import runs, cursor saved, its claim kept active (attribution waits for it); the engine
tick resumes it when the import ends — from the cursor, or from the top if the import
added newer history. A launch while a pass runs does not start another.

Tests marked GUARD pass on the parent tree too. The rest fail there.
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
DAYS = 90                                         # three 30-day chunks


def _store(days=DAYS):
    st = BlockStore(":memory:")
    with st._conn:
        st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, "
                         "block_minutes, timezone) VALUES (1,'2025-01-01T00:00:00',1,30,'UTC')")
        for d in (0, days):
            bs = (T0 + timedelta(days=d)).isoformat()
            st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, "
                             "config_period_id, imp_kwh, source) VALUES (?,?,'electricity_main',"
                             "1,1.0,'imported_api')", (bs, bs))
    st.set_meta(engine._REPRICE_HISTORY_MARKER, {"done": True})
    return st


class _Base(unittest.TestCase):

    def setUp(self):
        C.reset_for_tests()
        self.st = _store()
        self.chunks = []
        self.on_chunk = None

        async def _repair(c_from, c_to, **kw):
            self.chunks.append((c_from, c_to))
            if self.on_chunk:
                self.on_chunk(len(self.chunks))
            return {"recovered": 0, "checked": 1, "skipped": 0}

        async def _calm(headroom):
            return False

        async def _noop(*a, **k):
            return 0
        self.p = [mock.patch.object(engine, "_store", self.st),
                  mock.patch.object(engine, "kraken_available", lambda: True),
                  mock.patch.object(engine, "repair_import_pricing", _repair),
                  mock.patch.object(engine, "_verify_allowance_low", _calm),
                  mock.patch.object(engine, "_run_historical_reprice_sweep", _noop),
                  mock.patch.object(engine, "_maybe_drain_measured_history", lambda: None),
                  mock.patch.object(engine, "_uncostable_ranges", lambda kind: [])]
        for x in self.p:
            x.start()
        engine._verify_job.clear()
        engine._verify_job["status"] = "idle"

    def tearDown(self):
        for x in self.p:
            x.stop()
        engine._verify_job.clear()
        engine._verify_job["status"] = "idle"
        C.reset_for_tests()

    def _pass(self, **kw):
        return asyncio.run(engine.run_deferred_verify_pricing(wait_s=0, **kw))

    def _import_running(self):
        C.claim("import", force=True)
        C.mark("import", status="running")

    def _import_done(self):
        C.mark("import", status="done")


class TheDependency(_Base):

    def test_it_does_not_start_while_an_import_runs(self):
        self._import_running()
        r = self._pass()
        self.assertEqual(self.chunks, [], "it checked history the import was writing")
        self.assertEqual(engine._verify_job.get("status"), "deferred")
        self.assertTrue(r.get("deferred"))

    def test_an_import_starting_mid_pass_stops_it_at_the_next_chunk(self):
        self.on_chunk = lambda n: self._import_running() if n == 1 else None
        self._pass()
        self.assertEqual(len(self.chunks), 1)
        self.assertEqual(engine._verify_job.get("status"), "deferred")

    def test_attribution_keeps_waiting_while_it_is_deferred(self):
        self._import_running()
        self._pass()
        self._import_done()
        self.assertNotEqual(C.admission("attribution")[0], C.ADMIT,
                            "attribution admitted before the verify pass finished")

    def test_the_tick_resumes_it_from_its_cursor(self):
        self.on_chunk = lambda n: self._import_running() if n == 1 else None
        self._pass()
        self.on_chunk = None
        first = list(self.chunks)
        self._import_done()

        async def _tick_then_run():
            self.assertTrue(engine._maybe_resume_deferred_verify())
            for _ in range(50):
                await asyncio.sleep(0)
                if engine._verify_job.get("status") == "done":
                    break
        asyncio.run(_tick_then_run())
        self.assertEqual(engine._verify_job.get("status"), "done")
        self.assertNotIn(first[0], self.chunks[1:], "it re-checked the chunk already done")
        self.assertFalse(C.running("verify_pricing"))

    def test_newer_history_while_deferred_restarts_from_the_top(self):
        self.on_chunk = lambda n: self._import_running() if n == 1 else None
        self._pass()
        self.assertEqual(len(self.chunks), 1, "precondition: the pass deferred after a chunk")
        self.on_chunk = None
        newest = (T0 + timedelta(days=DAYS + 20)).isoformat()
        with self.st._conn:
            self.st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, "
                                  "config_period_id, imp_kwh, source) VALUES (?,?,"
                                  "'electricity_main',1,1.0,'imported_api')", (newest, newest))
        self._import_done()
        n_before = len(self.chunks)
        engine._verify_job["status"] = "resuming"          # as the tick leaves it
        self._pass(restart=False)
        self.assertEqual(self.chunks[n_before][1][:10], newest[:10],
                         "the resumed pass did not start at the newest edge")

    def test_one_pass_at_a_time(self):
        engine._verify_job["status"] = "running"
        r = self._pass()
        self.assertTrue(r.get("already_running"))
        self.assertEqual(self.chunks, [])

    def test_with_no_import_it_runs_to_the_end(self):
        """GUARD: nothing to wait for."""
        self._pass()
        self.assertEqual(len(self.chunks), 3)
        self.assertEqual(engine._verify_job.get("status"), "done")
        self.assertFalse(C.running("verify_pricing"))


if __name__ == "__main__":
    unittest.main()
