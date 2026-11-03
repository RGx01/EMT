"""
The settle sweep is not "in progress" on an install with no history (3 Nov 2026).

A beta tester's new install connected to Octopus in the setup wizard before its meter was set up: the
import schedule was built, the sweep's marker was not yet done, and `settle_history_in_progress()`
said so — the banner read "Finishing your upgrade … (null days left)" on an install that had never
run a previous version and had nothing to walk. The sweep that would mark it done runs only with a
meter. With no main-meter history it is not in progress.

test_no_history_is_not_in_progress fails on the parent; the other two are GUARDS (the parent's
behaviour, kept).
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine                                     # noqa: E402
from block_store import BlockStore                # noqa: E402


class _Sched:
    def is_empty(self):
        return False


class TheSweepState(unittest.TestCase):

    def setUp(self):
        self.st = BlockStore(":memory:")
        self.p = [mock.patch.object(engine, "_store", self.st),
                  mock.patch.object(engine, "_reader", lambda: self.st),
                  mock.patch.object(engine, "_settle_history_running", False),
                  mock.patch.object(engine, "_kraken_rate_schedules", {"import": _Sched()})]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        self.st.close()

    def test_no_history_is_not_in_progress(self):
        self.assertFalse(engine.settle_history_in_progress())

    def test_GUARD_history_not_yet_walked_is(self):
        self.st.block_span = lambda meter_id="electricity_main": ("2026-01-01T00:00:00",
                                                                 "2026-11-01T00:00:00")
        self.assertTrue(engine.settle_history_in_progress())

    def test_GUARD_a_walked_history_is_not(self):
        self.st.set_meta(engine._SETTLE_HISTORY_MARKER,
                         {"done": True, "version": engine._SETTLE_HISTORY_VERSION})
        self.st.block_span = lambda meter_id="electricity_main": ("2026-01-01T00:00:00",
                                                                 "2026-11-01T00:00:00")
        self.assertFalse(engine.settle_history_in_progress())


if __name__ == "__main__":
    unittest.main()
