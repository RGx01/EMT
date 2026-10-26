"""
A completed dispatch is split as soon as it is captured (13 Oct 2026).

The dispatch capture runs every 5 minutes; the predicted EV/House carve ran only in the hourly
pass. So a half-hour whose completed record had just arrived stayed House, and its rate line peak,
for up to an hour: 05:30 BST's charge was recorded at 06:42 and still drawn as House at 06:47.
Now the capture hands the slots it saw completed to the carve, which runs for those alone.

Discriminating: both tests fail on the parent (no `starts`; no carve after the capture).
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
import test_rebuild_meter_type as H              # noqa: E402  (stubs, real engine)
from block_store import BlockStore               # noqa: E402
engine = H.engine


# engine step 4c: the carve settles through settle(), which asks the tariff kind (the carve never
# did); these fixtures load no agreement, and the carve only ever acts on an Intelligent dispatch
from unittest import mock as _mock
_TK = _mock.patch.object(engine, "tariff_kind_at", lambda bs: "intelligent")


def setUpModule():
    _TK.start()


def tearDownModule():
    _TK.stop()


OFF = 0.05493


class _Sched:
    def is_empty(self): return False
    def day_rate_bounds(self, ts): return (5.493, 32.3092)
    def is_off_peak(self, ts): return True
    def resolve(self, ts): return 5.493


class _Ev:
    def __init__(self, p): self.p = p
    def resolve(self, ts): return self.p


class _Harness(unittest.TestCase):
    A, B = "2026-10-13T03:00:00", "2026-10-13T04:30:00"

    def setUp(self):
        self._save = (engine._store, engine._kraken_rate_schedules, engine._kraken_client,
                      engine._kraken_discovery, engine._last_dispatch_capture,
                      engine._last_reconcile, engine._capture_dispatch_slots)
        self.st = BlockStore(":memory:")
        engine._store = self.st
        engine._kraken_rate_schedules = {"import": _Sched(), "ev_device_off_peak": _Ev(5.493),
                                         "ev_device_peak": _Ev(32.3092)}
        self.st._conn.execute(
            "INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, "
            "block_minutes, timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
        for slot in (self.A, self.B):
            self.st._conn.execute(
                "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
                "imp_rate, imp_cost, rate_source) VALUES (?,?,?,1,3.1,?,?,'schedule')",
                (slot, slot, "electricity_main", OFF, round(3.1 * OFF, 6)))
            self.st.set_block_segments(slot, "electricity_main",
                                       [(3.1, OFF, OFF, "off_peak", "house")])
        self.st._conn.commit()

    def tearDown(self):
        (engine._store, engine._kraken_rate_schedules, engine._kraken_client,
         engine._kraken_discovery, engine._last_dispatch_capture,
         engine._last_reconcile, engine._capture_dispatch_slots) = self._save

    def _complete(self, slot):
        self.st._conn.execute(
            "INSERT INTO dispatch_history (slot_start, kind, energy_kwh, first_seen, last_seen) "
            "VALUES (?, 'completed', -2.88, ?, ?)", (slot, slot, slot))
        self.st._conn.commit()

    def _ev(self, slot):
        return self.st.get_ev_split_kwh([slot]).get(slot)


class TheCarve(_Harness):

    def test_it_can_be_aimed_at_the_slots_just_completed(self):
        self._complete(self.A)
        self._complete(self.B)
        res = asyncio.run(engine.run_iog_split_carve(starts=[self.B]))
        self.assertEqual(res["carved"], 1)
        self.assertIsNotNone(self._ev(self.B))
        self.assertIsNone(self._ev(self.A), "a slot outside `starts` was carved")

    def test_with_no_slots_it_does_nothing(self):
        """GUARD (new code): an empty `starts` is not "every slot"."""
        self._complete(self.A)
        self.assertEqual(asyncio.run(engine.run_iog_split_carve(starts=[]))["carved"], 0)


class TheCapture(_Harness):

    def test_a_captured_completion_is_split_before_the_hourly_pass(self):
        async def _capture():
            self._complete(self.B)                         # Octopus has just reported it
            engine._capture_completed_slots.add(self.B)
            return 0
        engine._capture_dispatch_slots = _capture
        engine._kraken_client = object()
        engine._kraken_discovery = {"import": {"mpan": "m"}}
        engine._last_dispatch_capture = None
        engine._last_reconcile = datetime.now(timezone.utc).replace(tzinfo=None)   # hourly not due
        asyncio.run(engine._tick_dispatch_capture())
        self.assertIsNotNone(self._ev(self.B), "the completed slot is still House")


if __name__ == "__main__":
    unittest.main()
