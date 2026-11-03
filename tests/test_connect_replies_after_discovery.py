"""
Connecting to Octopus answers once discovery has, and builds the rate schedules after (3 Nov 2026).

`/api/kraken-config` — the setup wizard's credentials step, and Settings' reconnect — waits 45 s for
`connect_kraken_now()`. That built the rate schedules before answering, and an Agile tariff's schedule
is its whole price history: a beta tester's Agile Outgoing export was 137,588 half-hours from 2018.
The route timed out, the wizard stayed on its credentials step ("Could not connect"), and it never
reached the main meter — a new install with no site and no meter, while the connect, still running,
switched the API on. Now the schedules are built on the engine loop after the reply, in a task that
engine_startup and a disconnect cancel before they close the client it reads (startup's poll cycle
builds them again).

Every test fails on the parent but test_the_schedules_are_still_built, a GUARD in substance: the
parent built them too (inline), and it fails there only because the task handle does not exist.
"""
import asyncio
import inspect
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine                                     # noqa: E402

DISCOVERY = {"account_number": "A-0000ABC7",
             "import": {"mpan": "2600000000001", "tariff_code": "E-1R-VAR-22-11-01-N"},
             "export": {"mpan": "2600000000002", "tariff_code": "E-1R-AGILE-OUTGOING-19-05-13-N"}}


class TheConnect(unittest.TestCase):

    def setUp(self):
        self.built = None

        async def _discover(force=False):
            engine._kraken_client = object()
            engine._kraken_discovery = dict(DISCOVERY)
        self.p = [mock.patch.object(engine, "_kraken_env", lambda: {"api_key": "k"}),
                  mock.patch.object(engine, "_kraken_startup_discovery", _discover),
                  mock.patch.object(engine, "get_data_source_mode", lambda: "api"),
                  mock.patch.object(engine, "_kraken_client", None),
                  mock.patch.object(engine, "_kraken_discovery", None)]
        for x in self.p:
            x.start()

    def tearDown(self):
        h = getattr(engine, "_kraken_rates_task_handle", None)
        if h is not None and not h.done():
            h.cancel()
        engine._kraken_rates_task_handle = None
        for x in self.p:
            x.stop()

    def _refresh(self, gate):
        async def _slow():
            await gate.wait()                     # an Agile history: as long as the test likes
            self.built = True
        return mock.patch.object(engine, "_refresh_kraken_rate_schedules", _slow)

    def test_the_reply_does_not_wait_for_the_schedules(self):
        async def _go():
            gate = asyncio.Event()
            with self._refresh(gate):
                r = await asyncio.wait_for(engine.connect_kraken_now(), 2)
                self.assertTrue(r["ok"] and r["connected"])
                self.assertIsNone(self.built)                       # not built yet
                h = engine._kraken_rates_task_handle
                self.assertFalse(h.done())
                h.cancel()
        asyncio.run(_go())

    def test_the_schedules_are_still_built(self):
        async def _go():
            gate = asyncio.Event()
            with self._refresh(gate):
                await asyncio.wait_for(engine.connect_kraken_now(), 2)
                gate.set()
                await asyncio.wait_for(engine._kraken_rates_task_handle, 2)
            self.assertTrue(self.built)
        asyncio.run(_go())

    def test_a_cancel_stops_it_and_a_second_connect_replaces_it(self):
        async def _go():
            gate = asyncio.Event()
            with self._refresh(gate):
                await asyncio.wait_for(engine.connect_kraken_now(), 2)   # the parent waits: fail, not hang
                first = engine._kraken_rates_task_handle
                await asyncio.wait_for(engine.connect_kraken_now(), 2)
                await asyncio.sleep(0)
                self.assertTrue(first.cancelled())
                second = engine._kraken_rates_task_handle
                engine._cancel_rate_refresh()
                await asyncio.sleep(0)
                self.assertTrue(second.cancelled())
                self.assertIsNone(engine._kraken_rates_task_handle)
            self.assertIsNone(self.built)
        asyncio.run(_go())


class StartupAndDisconnect(unittest.TestCase):
    """Both close the client the refresh reads: each cancels it, where it cancels the poll task."""

    def test_startup_and_a_disconnect_cancel_it(self):
        for fn in (engine._engine_startup_impl, engine.disconnect_kraken):
            with self.subTest(fn=fn.__name__):
                src = inspect.getsource(fn)
                self.assertIn("_cancel_rate_refresh()", src)
                self.assertLess(src.index("_cancel_kraken_poll_task()"), src.index("_cancel_rate_refresh()"))
        src = inspect.getsource(engine._engine_startup_impl)
        self.assertLess(src.index("_cancel_rate_refresh()"), src.index("_store.close()"))


if __name__ == "__main__":
    unittest.main()
