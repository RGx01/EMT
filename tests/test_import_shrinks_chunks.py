"""
An import that Octopus keeps failing asks fewer days at a time (5 Nov 2026, a tester's support file, b8 and
b9).

On one account nearly every page of Octopus's half-hourly measurements timed out at 30 s. A chunk of 60 days
is fetched as up to six pages of 500, and one page failing after its retries threw the whole chunk away, so
the job asked the same six pages again: each chunk took 7–23 minutes, then none got through for two hours.
Another account ran the same query whole in 9 minutes. `run_api_import_job` now halves the days it asks
after a chunk that failed to fetch — 60, 30, 15, then 7 (one page) — and keeps that size for the rest of
the job. Each smaller chunk that lands moves the checkpoint, so it is kept.

Discriminating: the tests not marked GUARD fail on the parent tree, which asked 60 days every time.
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

FAILED = {"ok": True, "channels": {"import": {"reason": "fetch_error", "fetch_error": "GraphQL network error"}}}
MORE = {"ok": True, "channels": {"import": {"reason": "more", "written": 336}}}
DONE = {"ok": True, "done": True, "channels": {}}


class TheJob(unittest.TestCase):

    def setUp(self):
        C.reset_for_tests()
        engine._api_import_job.clear()
        engine._api_import_job["status"] = "idle"
        self.p = [mock.patch.object(engine, "_store", None),
                  mock.patch.object(engine, "_persist_run_status", lambda j: None),
                  mock.patch.object(engine, "_run_wrote_blocks", lambda j: False),
                  mock.patch.object(engine, "check_e7_window", lambda store, sched: None)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        engine._api_import_job.clear()
        engine._api_import_job["status"] = "idle"
        C.reset_for_tests()

    def _run(self, answers):
        asked = []
        answers = list(answers)

        async def _fetch(*a, chunk_days=None, **k):
            asked.append(chunk_days)
            return answers.pop(0)

        async def _sleep(s):
            return None

        async def _go():
            with mock.patch.object(engine, "import_api_history", _fetch), \
                 mock.patch.object(engine.asyncio, "sleep", _sleep), \
                 mock.patch.object(engine, "_probe_and_apply_region_periods", mock.AsyncMock(return_value=None)), \
                 mock.patch.object(engine, "_finalise_after_import", lambda: None):
                await engine.run_api_import_job()
        asyncio.run(_go())
        return asked

    def test_each_failure_halves_the_days_to_one_page(self):
        with self.assertLogs("engine", level="INFO") as cm:
            asked = self._run([FAILED, FAILED, FAILED, FAILED, FAILED, DONE])
        self.assertEqual(asked, [60, 30, 15, 7, 7, 7])
        said = [m for m in cm.output if "did not answer" in m]
        self.assertEqual(len(said), 3, "said once a halving, not at every failure")

    def test_the_smaller_size_holds_for_the_rest_of_the_job(self):
        self.assertEqual(self._run([MORE, FAILED, MORE, MORE, DONE]), [60, 60, 30, 30, 30])

    def test_GUARD_an_answered_import_keeps_its_60_days(self):
        self.assertEqual(self._run([MORE, MORE, DONE]), [60, 60, 60])

    def test_GUARD_a_rate_limit_is_not_a_failure(self):
        rl = {"ok": True, "channels": {"import": {"reason": "rate_limit"}}}
        self.assertEqual(self._run([rl, MORE, DONE]), [60, 60, 60])


class TheRule(unittest.TestCase):

    def test_seven_days_fit_one_page(self):
        self.assertLessEqual(engine._IMPORT_CHUNK_MIN_DAYS * 48, 500)

    def test_an_export_failure_counts_too(self):
        res = {"channels": {"import": {"reason": "more"}, "export": {"reason": "fetch_error"}}}
        self.assertEqual(engine._import_chunk_after(60, res), 30)


if __name__ == "__main__":
    unittest.main()
