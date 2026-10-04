"""
A verify pass interrupted by a restart resumes from its persisted cursor (4 Oct 2026).

The deferred pricing-verification pass saves its cursor to kraken_state after every
chunk and clears it to "" when it completes, so a non-empty cursor on a freshly
opened database is a pass that did not finish. But its state was in memory only: a
restart cleared `_verify_job`, nothing read the cursor, and the pass — deferred to an
import, paused for API allowance, or simply mid-chunk — was lost. v4 built the cursor
for exactly this ("omit `restart` to RESUME an interrupted pass from its persisted
cursor") and never called the pass with restart=False.

Now the engine tick looks at the cursor once per database (engine_startup re-arms it,
which a restore re-runs), after the API is available and no import runs.

Tests marked GUARD pass on the parent tree too. The rest fail there.
"""
import ast
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
CURSOR = (T0 + timedelta(days=60)).isoformat()    # one chunk already verified


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


class _Base(unittest.TestCase):

    def setUp(self):
        C.reset_for_tests()
        self.st = _store()
        self.chunks = []
        self.api = True

        async def _repair(c_from, c_to, **kw):
            self.chunks.append((c_from, c_to))
            return {"recovered": 0, "checked": 1, "skipped": 0}

        async def _calm(headroom):
            return False

        async def _noop(*a, **k):
            return 0
        self.p = [mock.patch.object(engine, "_store", self.st),
                  mock.patch.object(engine, "kraken_available", lambda: self.api),
                  mock.patch.object(engine, "repair_import_pricing", _repair),
                  mock.patch.object(engine, "_verify_allowance_low", _calm),
                  mock.patch.object(engine, "_run_historical_reprice_sweep", _noop),
                  mock.patch.object(engine, "_maybe_drain_measured_history", lambda: None),
                  mock.patch.object(engine, "_uncostable_ranges", lambda kind: [])]
        for x in self.p:
            x.start()
        engine._verify_job.clear()
        engine._verify_job["status"] = "idle"          # as a fresh process has it
        engine._verify_cursor_checked = False          # as engine_startup leaves it

    def tearDown(self):
        for x in self.p:
            x.stop()
        engine._verify_job.clear()
        engine._verify_job["status"] = "idle"
        C.reset_for_tests()

    def tick(self):
        """One engine tick's resume check, then let anything it scheduled finish."""
        async def _go():
            scheduled = engine._maybe_resume_deferred_verify()
            for _ in range(200):
                await asyncio.sleep(0)
                if not C.running("verify_pricing") and engine._verify_job.get("status") in (
                        "done", "idle", "resuming_interrupted", "deferred", "error"):
                    if self.chunks or not scheduled:
                        break
            return scheduled
        return asyncio.run(_go())


class AnInterruptedPass(_Base):

    def setUp(self):
        super().setUp()
        self.st.set_kraken_state(engine._VERIFY_CURSOR_KEY, CURSOR)

    def test_it_resumes_from_its_cursor(self):
        self.assertTrue(self.tick())
        self.assertEqual(engine._verify_job.get("status"), "done", engine._verify_job)
        self.assertEqual(self.chunks[0][1], CURSOR[:10], "it started over, not from the cursor")
        self.assertTrue(all(c_to <= CURSOR[:10] for _, c_to in self.chunks))
        self.assertEqual(self.st.get_kraken_state(engine._VERIFY_CURSOR_KEY), "")

    def test_it_is_looked_at_once(self):
        """GUARD on the parent, which never resumed at all."""
        self.tick()
        self.chunks.clear()
        self.st.set_kraken_state(engine._VERIFY_CURSOR_KEY, CURSOR)
        engine._verify_job["status"] = "idle"
        self.assertFalse(self.tick(), "a second tick resumed it again")

    def test_it_waits_for_the_api(self):
        self.api = False
        self.assertFalse(self.tick())
        self.api = True
        self.assertTrue(self.tick(), "a late API connection lost it")

    def test_it_waits_for_an_import(self):
        C.claim("import", force=True)
        C.mark("import", status="running")
        self.assertFalse(self.tick())
        self.assertEqual(self.chunks, [])
        C.mark("import", status="done")
        self.assertTrue(self.tick(), "the import's end lost it")

    def test_a_restore_or_startup_re_arms_it(self):
        """STRUCTURAL: engine_startup — which a restore re-runs — resets the check,
        through the body it delegates to."""
        src = open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read()
        fns = {n.name: n for n in ast.walk(ast.parse(src))
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        calls = {c.func.id for c in ast.walk(fns["engine_startup"])
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        self.assertIn("_engine_startup_impl", calls)
        fn = fns["_engine_startup_impl"]
        sets = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "_verify_cursor_checked" for t in n.targets)
                and getattr(n.value, "value", None) is False]
        self.assertTrue(sets)


class NothingToResume(_Base):

    def test_a_completed_pass_leaves_nothing_to_run(self):
        """GUARD: the cursor is "" after a completed pass; nothing runs."""
        self.st.set_kraken_state(engine._VERIFY_CURSOR_KEY, "")
        self.assertFalse(self.tick())
        self.assertEqual(self.chunks, [])

    def test_a_live_pass_is_not_resumed_twice(self):
        """GUARD: only an idle job is a lost one."""
        self.st.set_kraken_state(engine._VERIFY_CURSOR_KEY, CURSOR)
        for status in ("running", "waiting", "paused"):
            with self.subTest(status=status):
                engine._verify_job["status"] = status
                self.assertFalse(self.tick())
        self.assertEqual(self.chunks, [])


if __name__ == "__main__":
    unittest.main()
