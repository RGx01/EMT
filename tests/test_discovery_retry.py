"""
G1b: the settle sweep waits for discovery, and discovery is asked again until it answers (G1 design
§5, the user, 28 Oct 2026: "make the sweep wait").

After a restore, start-up runs discovery and only then schedules the settle sweep, and the sweep is
scheduled only where discovery has answered (`kraken_available`) — so with G1a a v4 import settles with
its agreements stored. But a start-up discovery that failed — the API down after the restore — was
never asked again (re-discovery and the poll task both need an answer first), and the history stayed
unsettled until the next engine start. Now `_maybe_retry_discovery` asks again hourly while an API mode
has credentials and discovery has not answered; on an answer it schedules the sweep. A refused account
(a mismatch) is not retried.

Discriminating: every test here fails on the parent, but those labelled GUARD.
"""
import inspect
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine                                    # noqa: E402
import test_daily_rediscovery as R               # noqa: E402  (_run, _disc)

T0 = 5000.0


class _Base(unittest.TestCase):

    def setUp(self):
        self._save = (engine._kraken_discovery, engine._kraken_account_mismatch,
                      engine._discovery_retry_at, engine._kraken_client)
        engine._kraken_discovery = None
        engine._kraken_account_mismatch = None
        engine._discovery_retry_at = None
        self.calls, self.swept = [], []
        self.answer = R._disc()

        async def _discover(force=False):
            self.calls.append(1)
            engine._kraken_discovery = self.answer
        self.ps = [mock.patch.object(engine, "_kraken_startup_discovery", _discover),
                   mock.patch.object(engine, "mode_uses_api", lambda: True),
                   mock.patch.object(engine, "_kraken_env", lambda: {"api_key": "k"}),
                   mock.patch.object(engine, "_maybe_settle_history", lambda: self.swept.append(1)),
                   mock.patch.object(engine, "_ensure_kraken_poll_task_running", lambda: None)]
        for p in self.ps:
            p.start()

    def tearDown(self):
        for p in self.ps:
            p.stop()
        (engine._kraken_discovery, engine._kraken_account_mismatch,
         engine._discovery_retry_at, engine._kraken_client) = self._save

    def _retry(self, t):
        return R._run(engine._maybe_retry_discovery(now=t))


class TheRetry(_Base):

    def test_an_hour_after_start_up_it_asks_and_schedules_the_sweep(self):
        self.assertFalse(self._retry(T0))                            # the first tick sets the clock
        self.assertFalse(self._retry(T0 + 1800))
        self.assertTrue(self._retry(T0 + 3600))
        self.assertEqual((len(self.calls), len(self.swept)), (1, 1))

    def test_no_answer_asks_again_an_hour_later(self):
        self.answer = None
        self._retry(T0)
        self.assertFalse(self._retry(T0 + 3600))
        self.assertFalse(self._retry(T0 + 5400))
        self._retry(T0 + 7200)
        self.assertEqual((len(self.calls), len(self.swept)), (2, 0))

    def test_once_answered_it_does_not_ask(self):
        engine._kraken_discovery = R._disc()
        self._retry(T0)
        self._retry(T0 + 3600)
        self.assertEqual(self.calls, [])

    def test_a_refused_account_is_not_retried(self):
        engine._kraken_account_mismatch = ("A-1", "A-2")
        self._retry(T0)
        self._retry(T0 + 3600)
        self.assertEqual(self.calls, [])

    def test_the_tick_starts_it(self):
        src = inspect.getsource(engine._engine_tick)
        self.assertIn("_start_discovery_retry()", src)


class TheSweepWaits(unittest.TestCase):

    def test_GUARD_no_sweep_before_discovery_answers(self):
        """The wait itself was already there: the sweep is scheduled only where discovery answered."""
        with mock.patch.object(engine, "_kraken_discovery", None), \
             mock.patch.object(engine, "_kraken_client", object()), \
             mock.patch.object(engine, "_store", object()), \
             mock.patch.object(engine, "_settle_history_scheduled", False):
            engine._maybe_settle_history()
            self.assertFalse(engine._settle_history_scheduled)

    def test_GUARD_start_up_discovers_before_it_schedules_the_sweep(self):
        src = inspect.getsource(engine._engine_startup_impl)
        self.assertLess(src.index("await _kraken_startup_discovery()"), src.index("_maybe_settle_history()"))


if __name__ == "__main__":
    unittest.main()
