"""A reconnect to Home Assistant re-runs engine_startup only when HA itself was down.

Soak Pi, 23 Nov 2026 (2026.11.0b33): a 20 s commit stall on the SD card held the
engine loop; HA dropped the WebSocket; the first reconnect attempt succeeded (HA had
stayed up), yet the reconnect handler re-ran the whole startup, which held the loop
another ~25 s. In the same log, the one genuine HA restart (20 Nov) answered 502 seven
times before the reconnect succeeded. The listen loop re-subscribes state_changed on
every reconnect, so a drop where HA stayed up needs no startup.
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_client  # noqa: E402


class _EndOfTest(BaseException):  # listen() swallows Exception
    pass


def _run_listen(refusals: int) -> list:
    """Drive HAClient.listen() through one dropped socket and a reconnect that is
    refused `refusals` times first. Returns the kwargs each on_reconnect got."""
    client = ha_client.HAClient()
    calls = []
    attempts = {"n": 0}

    async def subscribe():
        if calls:                       # second time round: the test is over
            raise _EndOfTest()
        raise ConnectionResetError("socket dropped")

    async def connect():
        attempts["n"] += 1
        if attempts["n"] <= refusals:
            raise RuntimeError("502, message='Invalid response status'")
        client._connected.set()

    async def on_reconnect(**kwargs):
        calls.append(kwargs)

    async def no_sleep(_s):
        return None

    client._subscribe_state_changes = subscribe
    client.connect = connect
    client._on_reconnect = on_reconnect
    client._connected.set()

    real_sleep = ha_client.asyncio.sleep
    ha_client.asyncio.sleep = no_sleep
    try:
        async def drive():
            try:
                await client.listen()
            except _EndOfTest:
                pass
        asyncio.run(drive())
    finally:
        ha_client.asyncio.sleep = real_sleep
    return calls


class TestReconnectTellsWhetherHAWasDown(unittest.TestCase):

    def test_a_drop_where_ha_stayed_up(self):
        # REGRESSION: on the parent the callback got no argument at all.
        self.assertEqual(_run_listen(refusals=0), [{"ha_was_down": False}])

    def test_a_reconnect_refused_first_means_ha_was_down(self):
        # REGRESSION: on the parent the callback got no argument at all.
        self.assertEqual(_run_listen(refusals=3), [{"ha_was_down": True}])


class TestStartupRerunWanted(unittest.TestCase):

    def setUp(self):
        import main
        self.wanted = main.startup_rerun_wanted
        self.cooldown = main.RECONNECT_STARTUP_COOLDOWN_S

    def test_ha_stayed_up_never_reruns(self):
        # The 23 Nov case: hours since startup, HA up — the parent re-ran startup.
        self.assertFalse(self.wanted(False, 6 * 3600.0))
        self.assertFalse(self.wanted(False, float("inf")))

    def test_ha_was_down_reruns_after_the_cooldown(self):
        self.assertTrue(self.wanted(True, self.cooldown))
        self.assertTrue(self.wanted(True, 6 * 3600.0))

    def test_ha_was_down_inside_the_cooldown_does_not(self):
        # GUARD: the parent's cooldown, kept.
        self.assertFalse(self.wanted(True, self.cooldown - 1))


if __name__ == "__main__":
    unittest.main()
