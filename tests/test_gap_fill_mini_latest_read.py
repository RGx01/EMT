"""
A gap waiting for its post-outage read on a Mini-only install asks the Mini for its latest read
(Pi soak, 19 Oct 2026).

After a restart 1m53s into a half-hour the gap fill logged "waiting for first post-outage read"
for the rest of the half-hour: with no local import sensor the only reads come from the Mini, and
its collection opens 20 s before a boundary. Now `_mini_post_outage_read` asks the Mini once for
its latest register and adds it to the current block — sparingly, the telemetry API being
rate-limited: at most 3 tries per gap, 5 minutes apart, recorded on the marker (so a restart does
not reset them). No call with a local sensor or a read already present; a point not newer than the
gap's last known read is not used.

All new code: on the parent every test fails by name.
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine  # noqa: E402

NOW = datetime(2026, 10, 19, 14, 31, 59)
PRE = "2026-10-19T14:00:00"


class FakeReader:
    def __init__(self, point=None):
        self.calls = 0
        self.point = point

    async def read_latest(self, now_dt, lookback_s=300):
        self.calls += 1
        return self.point


def _block(**marker):
    m = {"pre_reads": {"electricity_main": {"import": {"ts": PRE, "value": 34500.231}}},
         "last_known_rates": {}}
    m.update(marker)
    return {"start": "2026-10-19T14:30:00", "_gap_marker": m,
            "meters": {"electricity_main": {"meta": {}, "channels": {}}}}


class TheLatestRead(unittest.TestCase):

    def setUp(self):
        self.reader = FakeReader({"value": 34500.4, "ts": "2026-10-19T14:31:40"})
        self.store = mock.MagicMock()
        self.local = False
        self.p = [mock.patch.object(engine, "_kraken_mini_reader", self.reader),
                  mock.patch.object(engine, "_store", self.store),
                  mock.patch.object(engine, "_main_meter_id", lambda: "electricity_main"),
                  mock.patch.object(engine, "_has_local_import_sensor", lambda: self.local)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def _ask(self, blk, now=NOW):
        return asyncio.run(engine._mini_post_outage_read(blk, now))

    def _reads(self, blk):
        return blk["meters"]["electricity_main"]["channels"].get("import", {}).get("reads", [])

    def test_one_call_gives_the_gap_its_read(self):
        blk = _block()
        self.assertTrue(self._ask(blk))
        self.assertEqual(self.reader.calls, 1)
        self.assertEqual(self._reads(blk), [{"value": 34500.4, "ts": "2026-10-19T14:31:40"}])
        self.store.save_current_block.assert_called_once()

    def test_again_within_five_minutes_makes_no_call(self):
        self.reader.point = None
        blk = _block()
        self._ask(blk)
        self._ask(blk, NOW + timedelta(seconds=299))
        self.assertEqual(self.reader.calls, 1)
        self._ask(blk, NOW + timedelta(seconds=301))
        self.assertEqual(self.reader.calls, 2)

    def test_three_tries_then_none(self):
        self.reader.point = None
        blk = _block()
        for i in range(5):
            self._ask(blk, NOW + timedelta(minutes=6 * i))
        self.assertEqual(self.reader.calls, 3)

    def test_the_tries_live_on_the_marker(self):
        """A restart reloads the current block with its marker: the count carries on."""
        self.reader.point = None
        blk = _block(mini_tries=3)
        self._ask(blk)
        self.assertEqual(self.reader.calls, 0)

    def test_no_call_with_a_local_sensor(self):
        self.local = True
        self.assertFalse(self._ask(_block()))
        self.assertEqual(self.reader.calls, 0)

    def test_no_call_when_a_read_is_already_there(self):
        blk = _block()
        blk["meters"]["electricity_main"]["channels"]["import"] = {
            "reads": [{"value": 1.0, "ts": "2026-10-19T14:31:00"}], "rates": []}
        self.assertFalse(self._ask(blk))
        self.assertEqual(self.reader.calls, 0)

    def test_a_point_not_newer_than_the_gap_is_not_used(self):
        self.reader.point = {"value": 34500.231, "ts": PRE}
        blk = _block()
        self.assertFalse(self._ask(blk))
        self.assertEqual(self._reads(blk), [])

    def test_the_tick_asks_before_it_looks_for_a_read(self):
        src = open(engine.__file__, encoding="utf-8").read()
        i = src.index("    if has_gap_marker(current_block):\n        try:\n            await _mini_post_outage_read(")
        self.assertLess(i, src.index("        if not has_real_read:\n            logger.info(\"gap fill: waiting"))


class TheReader(unittest.TestCase):

    def test_read_latest_is_one_fetch_of_the_last_five_minutes(self):
        import kraken_mini
        r = kraken_mini.MiniBoundaryReader.__new__(kraken_mini.MiniBoundaryReader)
        seen = []

        async def fetch(a, b):
            seen.append((a, b))
            return [{"value": 1.0, "ts": "x"}, {"value": 2.0, "ts": "y"}]
        r._fetch = fetch
        got = asyncio.run(r.read_latest(NOW))
        self.assertEqual(got, {"value": 2.0, "ts": "y"})
        self.assertEqual(seen, [(NOW - timedelta(seconds=300), NOW + timedelta(seconds=1))])


if __name__ == "__main__":
    unittest.main()
