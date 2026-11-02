"""
`_all_history_utc` — the span behind every `kind=all` request (#46, 2 Oct 2026).

It listed every local day in history (one timezone conversion per block, ~58k on the dev
database) to keep the first and last. On the Pi that was most of the Charts recent-day
check: 2.86 s p50 in the seventh soak, against 0.17 s for the change poll. Now it reads
MIN and MAX and converts two instants. A local date never falls as the UTC instant
rises, so the answer is the same.

The regression test fails on the parent tree, which ran `SELECT DISTINCT block_start`
over all history. The span cases are GUARDS: the parent computed the same answers.
"""
import importlib.util
import inspect
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "web"))

from block_store import BlockStore, local_date_to_utc_bounds   # noqa: E402

TZ = "Europe/London"


def _server():
    spec = importlib.util.spec_from_file_location("server_span", os.path.join(_ROOT, "web", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv


def _store(starts):
    st = BlockStore(os.path.join(tempfile.mkdtemp(), "blocks.db"))
    st._conn.execute("PRAGMA foreign_keys=OFF")
    for bs in starts:
        be = (datetime.fromisoformat(bs) + timedelta(minutes=30)).isoformat()
        st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                         "imp_kwh) VALUES (?, ?, 'electricity_main', 1, 1.0)", (bs, be))
    st._conn.commit()
    return st


def _reference(starts, tz_name):
    """The parent's answer: every block's local day, then the two ends."""
    z = ZoneInfo(tz_name)
    days = sorted({datetime.fromisoformat(s).replace(tzinfo=timezone.utc).astimezone(z).date().isoformat()
                   for s in starts})
    return local_date_to_utc_bounds(days[0], tz_name)[0], local_date_to_utc_bounds(days[-1], tz_name)[1]


class TheSpan(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv = _server()

    def _span(self, st, tz_name=TZ):
        # The parent took a third argument, block_minutes; pass it there so the
        # regression test below fails on what it measures, not on the signature.
        extra = (30,) if len(inspect.signature(self.srv._all_history_utc).parameters) == 3 else ()
        return self.srv._all_history_utc(st, tz_name, *extra)

    def test_it_reads_two_rows_not_every_block(self):
        """REGRESSION (#46): every statement it runs is an aggregate over blocks."""
        st = _store(["2026-03-01T%02d:%02d:00" % (h, m) for h in range(24) for m in (0, 30)])
        self.addCleanup(st.close)
        seen = []
        st._conn.set_trace_callback(seen.append)
        self._span(st)
        st._conn.set_trace_callback(None)
        self.assertTrue(seen)
        for sql in seen:
            self.assertIn("MIN(block_start)", sql)

    def test_a_first_block_late_in_BST_belongs_to_the_next_local_day(self):
        """GUARD. 23:30Z on 30 Jun is 00:30 BST on 1 Jul: the span starts at 1 Jul's local
        midnight, 23:00Z on 30 Jun. The last block, 23:30Z on 1 Nov, is 23:30 GMT on
        1 Nov: the span ends at 2 Nov's local midnight, 00:00Z."""
        st = _store(["2026-06-30T23:30:00", "2026-08-15T12:00:00", "2026-11-01T23:30:00"])
        self.addCleanup(st.close)
        self.assertEqual(self._span(st), ("2026-06-30T23:00:00", "2026-11-02T00:00:00"))

    def test_it_matches_a_per_block_conversion_across_both_clock_changes(self):
        """GUARD: the parent's own method, over blocks either side of 29 Mar and 25 Oct."""
        starts = ["2026-03-28T23:30:00", "2026-03-29T00:30:00", "2026-03-29T01:00:00",
                  "2026-10-24T23:00:00", "2026-10-25T00:30:00", "2026-10-25T01:30:00",
                  "2026-10-25T23:30:00"]
        for tz_name in (TZ, "UTC", "America/New_York"):
            st = _store(starts)
            self.addCleanup(st.close)
            self.assertEqual(self._span(st, tz_name), _reference(starts, tz_name), tz_name)

    def test_no_blocks_is_none(self):
        """GUARD: the route turns this into a 404 (Usage Stats and Spiral draw empty)."""
        st = _store([])
        self.addCleanup(st.close)
        self.assertIsNone(self._span(st))


if __name__ == "__main__":
    unittest.main()
