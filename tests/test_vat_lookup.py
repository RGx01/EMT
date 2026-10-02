"""
#48 (2 Oct 2026) — a year's bill read the VAT calendar 5,440 times.

`calculate_billing_summary_for_period` asked `store.vat_rate_at` for every unsettled
block's fallback VAT rate. Memoising by UTC hour (C, 30 Sep) still left one call per hour,
and each call read the learned calendar from `store_meta`, parsed it, merged it with the
seed and walked it. `_bill_method_breakdown` converted all 17,520 half-hours of a year to a
UK-local date one at a time. Now the calendar is merged once (`vat_calendar.rate_lookup`,
exposed as `BlockStore.vat_rate_lookup`), and local days are remembered per UTC hour
(`local_day_fn`).

On the dev and production-like databases the bill responses (year, quarter, month, day
charts, across the 1 Oct 2026 change) were byte-identical before and after. On the Mac a
2025 year's bill went from 0.477 s to 0.430 s on dev. On the Pi it saves 5,440 database
reads.

`test_one_read_per_summary` is the regression test: it fails on the parent tree. The
equivalence tests are new-function tests (they fail there for want of the functions), and
are guards in spirit: they pin the rule as `resolve_vat` states it.
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import vat_calendar as vc                       # noqa: E402

LEARNED = ([], [("2026-10-01", 0.0)], [("2026-03-29", 0.2), ("2026-10-25", 0.05)])


def _probes():
    """Every half-hour across the seed's first entry, both 2026 clock changes and the
    1 Oct 2026 change, as naive UTC, as a bare date, with Z and with an offset."""
    out = []
    for a, b in ((datetime(1997, 8, 30), datetime(1997, 9, 3)),
                 (datetime(2026, 3, 27), datetime(2026, 4, 1)),
                 (datetime(2026, 9, 29), datetime(2026, 10, 3)),
                 (datetime(2026, 10, 23), datetime(2026, 10, 28))):
        t = a
        while t < b:
            out += [t.isoformat(), t.date().isoformat(), t.isoformat() + "Z", t.isoformat() + "+01:00"]
            t += timedelta(minutes=30)
    return out + ["", None]


class TheRule(unittest.TestCase):

    def test_rate_lookup_is_resolve_vat(self):
        for learned in LEARNED:
            f = vc.rate_lookup(learned)
            for w in _probes():
                self.assertEqual(f(w), vc.resolve_vat(w, learned), (learned, w))

    def test_local_day_fn_is_local_day(self):
        f = vc.local_day_fn()
        for w in _probes()[:-2]:
            self.assertEqual(f(w), vc.local_day(w), w)

    def test_the_first_half_hour_of_october_in_BST(self):
        """23:00Z on 30 Sep is 00:00 BST on 1 Oct: the 0% half-hour."""
        f = vc.rate_lookup([])
        self.assertEqual((f("2026-09-30T22:30:00"), f("2026-09-30T23:00:00")), (0.05, 0.0))


class TheSummary(unittest.TestCase):

    def test_one_read_per_summary(self):
        """REGRESSION (#48): three days of unsettled blocks read the calendar once, not
        once per UTC hour (72 times on the parent tree)."""
        from block_store import BlockStore
        import billing
        st = BlockStore(tempfile.mktemp(suffix=".db"))
        self.addCleanup(st.close)
        st._conn.execute("PRAGMA foreign_keys=OFF")
        st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, "
                         "timezone) VALUES (1, '2026-01-01T00:00:00', 3, 30, 'Europe/London')")
        st._conn.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter) "
                         "VALUES (1, 'electricity_main', 0)")
        t = datetime(2026, 9, 29, 23, 0)
        for _ in range(144):
            be = (t + timedelta(minutes=30)).isoformat()
            st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                             "imp_kwh, imp_rate, imp_cost, standing_charge) "
                             "VALUES (?, ?, 'electricity_main', 1, 1.0, 0.25, 0.25, 0.5)", (t.isoformat(), be))
            t += timedelta(minutes=30)
        st._conn.commit()
        blocks = st.get_blocks_lightweight("2026-09-29T23:00:00", "2026-10-02T23:00:00")
        seen = []
        st._conn.set_trace_callback(seen.append)
        s = billing.calculate_billing_summary_for_period(
            blocks, datetime(2026, 9, 30), datetime(2026, 10, 3), store=st, tz_name="Europe/London")
        st._conn.set_trace_callback(None)
        self.assertTrue(s)
        self.assertEqual(sum("vat_calendar" in q for q in seen), 1)


if __name__ == "__main__":
    unittest.main()
