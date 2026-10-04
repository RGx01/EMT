"""
"import" is held for as long as anything is importing (4 Oct 2026).

Two ways the coordinator's "import" claim was not held while blocks were being
imported, so every member that depends on "import" — the re-price sweep, carbon
backfill, measured drain, verify and attribution — was admitted beside an import:

1. A GAP FILL never claimed it. `run_gap_fill_job` writes the same imported blocks
   through the same `import_api_history`, and shares `_api_import_job`. In v4 the
   dependants checked `api_import_running()`, which the gap fill's status sets, so
   they waited for it. B moved them to `admission()`, which sees only the
   coordinator, and only `run_api_import_job` claimed "import".

2. The full import RELEASED it for a rate-limit cooldown. `run_api_import_job` marked
   "import" done on `rate_limited`, waited ~60 s, and carried on importing without
   claiming again. `rate_limited` is not an end — v4's predicate counts it as
   running — and B's test counted it as one of "five ways to end".

The two admission tests fail on the parent tree; the release tests are guards there
(the parent never claimed), and pin that the new claim is released on every way out.
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

DEPENDANTS = ("reprice_sweep", "carbon_backfill", "measured_drain", "verify_pricing")


class _Run(unittest.TestCase):

    def setUp(self):
        C.reset_for_tests()
        engine._api_import_job.clear()
        engine._api_import_job["status"] = "idle"
        self.p = [mock.patch.object(engine, "_store", None),
                  mock.patch.object(engine, "_persist_import_health", lambda j: None),
                  mock.patch.object(engine, "_persist_run_status", lambda j: None),
                  mock.patch.object(engine, "_run_wrote_blocks", lambda j: False),
                  mock.patch.object(engine, "_reset_pricing_health", lambda: None)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        engine._api_import_job.clear()
        engine._api_import_job["status"] = "idle"
        C.reset_for_tests()

    def admissions(self):
        return {m: C.admission(m)[0] for m in DEPENDANTS}

    def assert_all_deferred(self, seen):
        self.assertEqual(seen, {m: C.DEFER for m in DEPENDANTS})


class TheGapFill(_Run):

    def _drive(self, results, control=None):
        """Run a gap fill whose fetch returns `results` in turn, sampling the
        dependants' admission inside each fetch."""
        seen = []
        it = iter(results)

        async def _fetch(*a, **k):
            seen.append(self.admissions())
            if control:
                engine._api_import_job["control"] = control
            return next(it)

        async def _go():
            with mock.patch.object(engine, "import_api_history", _fetch):
                await engine.run_gap_fill_job("2026-01-01T00:00:00", "2026-01-02T00:00:00",
                                              channels=("import",))
        asyncio.run(_go())
        return seen

    def test_the_dependants_defer_while_it_imports(self):
        seen = self._drive([{"ok": True, "done": True, "channels": {}}])
        self.assert_all_deferred(seen[0])

    def test_it_releases_import_when_done(self):
        """GUARD for the parent, which never claimed."""
        self._drive([{"ok": True, "done": True, "channels": {}}])
        self.assertFalse(C.running("import"))
        self.assertEqual(self.admissions(), {m: C.ADMIT for m in DEPENDANTS})

    def test_it_releases_import_on_error_and_on_cancel(self):
        """GUARD for the parent (which never claimed); the claim's every way out."""
        self._drive([{"ok": False, "reason": "boom"}])
        self.assertEqual(engine._api_import_job["status"], "error")
        self.assertFalse(C.running("import"))
        self._drive([{"ok": True, "done": False, "channels":
                      {"import": {"oldest": "2026-01-01T12:00:00"}}}], control="cancel")
        self.assertEqual(engine._api_import_job["status"], "cancelled")
        self.assertFalse(C.running("import"))

    def test_it_releases_import_if_it_raises(self):
        async def _boom(*a, **k):
            raise RuntimeError("network")

        async def _go():
            with mock.patch.object(engine, "import_api_history", _boom):
                await engine.run_gap_fill_job("2026-01-01T00:00:00", "2026-01-02T00:00:00")
        asyncio.run(_go())
        self.assertEqual(engine._api_import_job["status"], "error")
        self.assertFalse(C.running("import"))


class TheRateLimitCooldown(_Run):

    def test_the_dependants_defer_through_the_cooldown_and_after(self):
        """The fetch is rate-limited once, then completes. Sampled during the
        cooldown and inside the fetch that follows it."""
        seen = {}
        calls = []

        async def _fetch(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                return {"ok": True, "channels": {"import": {"reason": "rate_limit"}}}
            seen["after"] = self.admissions()
            return {"ok": True, "done": True, "channels": {}}

        real_sleep = asyncio.sleep

        async def _sleep(s):
            if engine._api_import_job.get("status") == "rate_limited" and "during" not in seen:
                seen["during"] = self.admissions()
            await real_sleep(0)

        async def _go():
            with mock.patch.object(engine, "import_api_history", _fetch), \
                 mock.patch.object(engine.asyncio, "sleep", _sleep), \
                 mock.patch.object(engine, "_probe_and_apply_region_periods",
                                   mock.AsyncMock(return_value=None)), \
                 mock.patch.object(engine, "_finalise_after_import", lambda: None), \
                 mock.patch.object(engine, "_drain_reprice_queue_after_import",
                                   mock.AsyncMock(return_value={})):
                await engine.run_api_import_job()
        asyncio.run(_go())
        self.assertEqual(len(calls), 2, "the import did not resume after the cooldown")
        self.assert_all_deferred(seen["during"])
        self.assert_all_deferred(seen["after"])
        self.assertFalse(C.running("import"), "not released at the real end")


if __name__ == "__main__":
    unittest.main()
