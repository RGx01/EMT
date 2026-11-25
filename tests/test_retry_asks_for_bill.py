"""The poll cycle asks for the bills its settled kWh allow (25 Nov 2026, the user).

The pill's Retry settlement runs a poll cycle now (`request_kraken_poll`). On the soak Pi (b34, 25 Nov
2026) it settled 24 Nov's kWh — 61 rows, every half-hour re-priced as a prediction — and the pill said
"Octopus asked"; but the bill is the one asker's (`run_measured_history_drain`), scheduled only from the
hourly reconcile, so the day stayed an estimate until the next pass. A Data Management re-fetch of the
day, two minutes later, found Octopus's bill for it at once.

Now the cycle schedules the asker at its end and waits for it (`_drain_after_poll`), so the poll's
`running` — what the pill waits on — covers the bill too. The asker is backlog-gated and paced as before.

Discriminating: every test fails on the parent but the GUARD.
"""
import asyncio
import inspect
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_engine  # noqa: F401  — installs stubs, imports engine
import engine


class _Open:
    """The asker's gates open: a store, an Intelligent account, the API, admission, a backlog."""

    def __init__(self, backlog=3):
        self.backlog, self.ran = backlog, []

    def __enter__(self):
        async def drain(*a, **k):
            await asyncio.sleep(0.05)
            self.ran.append("drain")
            return {"ok": True}
        self.p = [mock.patch.object(engine, "_store", object()),
                  mock.patch.object(engine, "_any_intelligent", lambda: True),
                  mock.patch.object(engine, "kraken_available", lambda: True),
                  mock.patch.object(engine._coord, "admission", lambda name: (engine.ADMIT, None)),
                  mock.patch.object(engine, "_measured_history_backlog", lambda: self.backlog),
                  mock.patch.object(engine, "run_measured_history_drain", drain),
                  mock.patch.object(engine, "_measured_history_drain_running", False)]
        for x in self.p:
            x.start()
        return self

    def __exit__(self, *a):
        for x in reversed(self.p):
            x.stop()


class TheScheduler(unittest.TestCase):

    def test_it_hands_back_the_task(self):
        with _Open() as g:
            async def go():
                t = engine._maybe_drain_measured_history()
                self.assertIsInstance(t, asyncio.Task)
                await t
            asyncio.run(go())
        self.assertEqual(g.ran, ["drain"])


class AfterThePoll(unittest.TestCase):

    def test_the_cycle_waits_for_the_bills(self):
        with _Open() as g:
            asyncio.run(engine._drain_after_poll())
            self.assertEqual(g.ran, ["drain"])       # done when it returns, not merely scheduled

    def test_GUARD_nothing_to_ask_nothing_asked(self):
        with _Open(backlog=0) as g:
            asyncio.run(engine._drain_after_poll())
        self.assertEqual(g.ran, [])

    def test_inside_the_cycle_before_it_ends(self):
        # after the settlement sweep, inside the try: the poll's `running` (the pill's wait) is still set
        src = inspect.getsource(engine.kraken_poll_task)
        i = src.index("await _maybe_run_settlement_sweep()")
        j = src.index("await _drain_after_poll()")
        k = src.index("finally:", i)
        self.assertLess(i, j)
        self.assertLess(j, k)


if __name__ == "__main__":
    unittest.main()
