"""
A gap fill's pricing check covers what it filled, not the whole history (18 Oct 2026, the soak).

Every heatmap fill ended with `run_deferred_verify_pricing()` over the WHOLE imported history,
2023 → today: 39 chunks, 43–70 s and ~30 Octopus requests on the Pi, for a gap of a few days.
Now `_gap_fill_finish` passes the fill's span, and the pass checks only that.

- `span` clamps the pass to (from, to) inside the imported history;
- it is persisted with the cursor, so a pass paused for the allowance, or interrupted by a
  restart, resumes over the same span; one deferred to an import widens to the whole history;
- the full import's pass is unchanged (no span).

Discriminating (fail on the parent: `span` does not exist there) but those marked GUARD.
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
DAYS = 180


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


class _Pass(unittest.TestCase):

    def setUp(self):
        C.reset_for_tests()
        self.st = _store()
        self.chunks = []
        self.low = False

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

    def _run(self, **kw):
        return asyncio.run(engine.run_deferred_verify_pricing(wait_s=0, max_wait_cycles=2, **kw))

    def _days(self):
        return sorted({d for c in self.chunks for d in c})


class AScopedPass(_Pass):

    def test_checks_only_the_span(self):
        self._run(span=("2026-03-01T00:00:00", "2026-03-11T00:00:00"))
        self.assertEqual(self.chunks, [("2026-03-01", "2026-03-11")])

    def test_a_span_beyond_the_history_is_clamped(self):
        self._run(span=("2025-06-01T00:00:00", "2026-01-11T00:00:00"))
        self.assertEqual(self.chunks, [("2026-01-01", "2026-01-11")])

    def test_a_span_outside_the_history_checks_nothing(self):
        r = self._run(span=("2024-01-01T00:00:00", "2024-02-01T00:00:00"))
        self.assertEqual((r["ran"], self.chunks), (False, []))

    def test_the_whole_history_without_one(self):
        """GUARD: the full import's pass, unchanged — every chunk to the oldest block."""
        self._run()
        self.assertEqual(self._days()[0], "2026-01-01")
        self.assertEqual(self._days()[-1], "2026-06-30")

    def test_done_clears_the_span(self):
        self._run(span=("2026-03-01T00:00:00", "2026-03-11T00:00:00"))
        self.assertEqual(self.st.get_kraken_state(engine._VERIFY_SPAN_KEY), "")


class AResumedScopedPass(_Pass):

    def _pause(self):
        self.low = True
        self._run(span=("2026-03-01T00:00:00", "2026-03-11T00:00:00"))
        self.assertEqual(engine._verify_job["status"], "paused")
        self.low = False

    def test_paused_it_resumes_over_its_span(self):
        self._pause()
        engine._verify_job["status"] = "resuming_paused"
        self._run(restart=False)
        self.assertEqual(self.chunks, [("2026-03-01", "2026-03-11")])

    def test_after_a_restart_it_resumes_over_its_span(self):
        self._pause()
        engine._verify_job.clear()
        engine._verify_job["status"] = "resuming_interrupted"
        self._run(restart=False)
        self.assertEqual(self.chunks, [("2026-03-01", "2026-03-11")])

    def test_deferred_to_an_import_it_checks_the_whole_history(self):
        self._pause()
        engine._verify_job["status"] = "resuming"
        self._run(restart=False)
        self.assertEqual(self._days()[0], "2026-01-01")


class TheFillFinish(unittest.TestCase):

    def test_the_finish_passes_the_fill_s_span(self):
        launched = []

        async def _verify(**kw):
            launched.append(kw)

        async def _repair(*a, **k):
            return {}
        j = {"gap": {"from": "2026-06-24T21:00:00", "to": "2026-06-30T23:00:00"},
             "written": {"import": 292, "export": 292}}
        with mock.patch.object(engine, "_store", None), \
             mock.patch.object(engine, "repair_import_pricing", _repair), \
             mock.patch.object(engine, "_persist_import_health", lambda j: None), \
             mock.patch.object(engine, "_persist_run_status", lambda j: None), \
             mock.patch.object(engine, "run_deferred_verify_pricing", _verify):
            async def go():
                await engine._gap_fill_finish(j, ("import", "export"))
                await asyncio.sleep(0)
            asyncio.run(go())
        self.assertEqual(launched, [{"span": ("2026-06-24T21:00:00", "2026-06-30T23:00:00")}])


if __name__ == "__main__":
    unittest.main()
