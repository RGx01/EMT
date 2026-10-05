"""
The re-price sweep waits for startup's own writes before its first pass (5 Oct 2026).

Startup schedules the sweep from inside itself, so it began while startup was still
writing — and alongside the first Kraken poll (with its BL-8 outage backfill) and the
carbon backfill. Soak 17's worst lag, 11.2 s, was that overlap on the SD card. The sweep
now holds until startup has finished, no poll is running, and _SWEEP_STARTUP_HOLD_S has
passed since startup — never longer than _SWEEP_STARTUP_HOLD_MAX_S.

The timings are shrunk for the test. All but the labelled guard fail on the parent tree.
"""
import asyncio
import os
import sys
import time
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import engine                                     # noqa: E402

HOLD, MAX, TICK = 0.2, 1.0, 0.01


class TheHold(unittest.TestCase):

    def setUp(self):
        self.poll = False
        self._saved_ts = engine._last_startup_complete_ts
        self.p = [mock.patch.object(engine, "_SWEEP_STARTUP_HOLD_S", HOLD),
                  mock.patch.object(engine, "_SWEEP_STARTUP_HOLD_MAX_S", MAX),
                  mock.patch.object(engine, "_SWEEP_STARTUP_HOLD_POLL_S", TICK),
                  mock.patch.object(engine, "poll_in_progress", lambda: self.poll)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        engine._last_startup_complete_ts = self._saved_ts

    def _held_for(self, setup):
        async def _go():
            await setup()
            t0 = time.monotonic()
            await engine._hold_sweep_after_startup()
            return time.monotonic() - t0
        return asyncio.run(_go())

    def test_it_waits_until_the_hold_has_passed_since_startup(self):
        async def setup():
            engine._last_startup_complete_ts = time.monotonic()      # startup just finished
        self.assertGreaterEqual(self._held_for(setup), HOLD * 0.9)

    def test_it_waits_for_a_running_startup(self):
        async def setup():
            engine._last_startup_complete_ts = time.monotonic() - 3600
            await engine._startup_lock.acquire()

            async def _finish():
                await asyncio.sleep(0.3)
                engine._last_startup_complete_ts = time.monotonic() - 3600
                engine._startup_lock.release()
            asyncio.ensure_future(_finish())
        self.assertGreaterEqual(self._held_for(setup), 0.3 * 0.9)

    def test_it_waits_for_a_running_poll(self):
        async def setup():
            engine._last_startup_complete_ts = time.monotonic() - 3600
            self.poll = True

            async def _finish():
                await asyncio.sleep(0.3)
                self.poll = False
            asyncio.ensure_future(_finish())
        self.assertGreaterEqual(self._held_for(setup), 0.3 * 0.9)

    def test_it_never_holds_longer_than_the_cap(self):
        async def setup():
            engine._last_startup_complete_ts = time.monotonic() - 3600
            self.poll = True                                         # a poll that never ends
        held = self._held_for(setup)
        self.assertGreaterEqual(held, MAX * 0.9)
        self.assertLess(held, MAX + 0.5)

    def test_no_hold_long_after_startup(self):
        """GUARD in behaviour (the parent never held); it errors there only because the
        hold is new."""
        async def setup():
            engine._last_startup_complete_ts = time.monotonic() - 3600
        self.assertLess(self._held_for(setup), 0.1)


class TheSweepTask(unittest.TestCase):
    """The scheduled sweep task holds before its first pass."""

    def test_the_hold_comes_before_the_first_pass(self):
        order = []

        async def _hold():
            order.append("hold")

        async def _sweep(*a, **k):
            order.append("pass")
            return 0

        st = mock.MagicMock()
        st.get_meta.side_effect = lambda key, default=None: (
            {"done": True} if order.count("pass") else {})

        async def _go():
            with mock.patch.object(engine, "_store", st), \
                 mock.patch.object(engine, "kraken_available", lambda: True), \
                 mock.patch.object(engine, "_reprice_history_running", False), \
                 mock.patch.object(engine, "_hold_sweep_after_startup", _hold), \
                 mock.patch.object(engine, "_run_historical_reprice_sweep", _sweep), \
                 mock.patch.object(engine._coord, "admission", lambda name: (engine.ADMIT, None)):
                engine._maybe_reprice_history()
                for _ in range(50):
                    await asyncio.sleep(0)
                    if "pass" in order:
                        break
        asyncio.run(_go())
        self.assertEqual(order[:2], ["hold", "pass"])


if __name__ == "__main__":
    unittest.main()
