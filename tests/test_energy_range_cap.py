"""
C §7.3 — how much history one /api/energy request may cover (5 Oct 2026).

An unbounded range re-creates the wholesale problem at request time instead of write time.
`resolution=block` refused only `kind=all`; a year, a quarter or any `kind=range` passed, so one
request could ask for every block in history (~15 MB raw a year, C §7.1). Now:

  block   at most 31 days — a month, the most any page asks for
  period  at most 366 days — a calendar year, the longest bill any page asks for
  day     uncapped — one row per day; all history is what Spiral / Usage Stats load

Counted from the resolved UTC range, so a billing period (whose period_end is the next one's
start) and a clock-change month count as the days they are.

Tests marked GUARD hold on the parent too (the pages' own requests). The rest fail there.
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import store_access                               # noqa: E402
from block_store import BlockStore                # noqa: E402

CONFIG = {"meters": {"electricity_main": {
    "meta": {"billing_day": 3, "block_minutes": 30, "timezone": "Europe/London",
             "currency_symbol": "£", "currency_code": "GBP"},
    "channels": {"import": {}, "export": {}}}}}


def _server():
    spec = importlib.util.spec_from_file_location("server_rc", os.path.join(_ROOT, "web", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv


class TheCap(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        path = os.path.join(cls.dir, "blocks.db")
        st = BlockStore(path)
        st.insert_config_period(CONFIG)
        cp = st.get_current_config_period_id()
        with st._conn:
            for bs in ("2023-06-06T10:00:00", "2024-02-29T10:00:00", "2025-03-30T10:00:00",
                       "2025-10-26T10:00:00", "2026-10-03T10:00:00"):
                st._conn.execute(
                    "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                    "imp_kwh, imp_rate, imp_cost, exp_kwh, exp_cost, standing_charge) "
                    "VALUES (?, ?, 'electricity_main', ?, 1.0, 0.3, 0.3, 0, 0, 0.5)", (bs, bs, cp))
        st.close()
        cls.srv = _server()
        cls.srv.DATA_DIR = cls.dir
        store_access.reset_for_tests()
        store_access.bind(path)
        cls.client = cls.srv.app.test_client()

    @classmethod
    def tearDownClass(cls):
        store_access.reset_for_tests()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def get(self, q):
        r = self.client.get("/api/energy?" + q)
        return r.status_code, (r.get_json() or {})

    def assertServed(self, q):
        code, j = self.get(q)
        self.assertEqual(code, 200, (q, j.get("error")))

    def assertRefused(self, q, days):
        code, j = self.get(q)
        self.assertEqual(code, 400, q)
        self.assertIn("covers %d" % days, j.get("error", ""))

    # ── block ───────────────────────────────────────────────────────────────
    def test_a_month_of_blocks_is_served(self):
        """GUARD: the heatmap's request."""
        self.assertServed("resolution=block&kind=month&year=2024&month=1")

    def test_a_day_of_blocks_is_served(self):
        """GUARD: the HH view's request."""
        self.assertServed("resolution=block&kind=day&date=2025-03-30&fields=stats")

    def test_thirty_one_days_of_blocks_is_served(self):
        """GUARD: the boundary itself."""
        self.assertServed("resolution=block&kind=range&start=2025-01-01&end=2025-01-31")

    def test_clock_change_months_count_as_their_days(self):
        """GUARD: 23- and 25-hour days round to whole days."""
        self.assertServed("resolution=block&kind=range&start=2025-03-01&end=2025-03-31")
        self.assertServed("resolution=block&kind=range&start=2025-10-01&end=2025-10-31")

    def test_thirty_two_days_of_blocks_is_refused(self):
        self.assertRefused("resolution=block&kind=range&start=2025-01-01&end=2025-02-01", 32)

    def test_a_year_of_blocks_is_refused(self):
        self.assertRefused("resolution=block&kind=year&year=2025", 365)

    # ── period ──────────────────────────────────────────────────────────────
    def test_a_leap_year_bill_is_served(self):
        """GUARD: the Billing tab's longest request, 366 days."""
        self.assertServed("resolution=period&kind=year&year=2024&include=billing")

    def test_a_billing_period_is_served(self):
        """GUARD: period_end is the next period's start; the count is not fooled by it."""
        self.assertServed("resolution=period&kind=billing&include=billing")

    def test_a_bill_over_all_history_is_refused(self):
        code, j = self.get("resolution=period&kind=all")
        self.assertEqual(code, 400)
        self.assertIn("at most 366 days", j.get("error", ""))

    def test_a_period_over_a_long_range_is_refused(self):
        self.assertRefused("resolution=period&kind=range&start=2024-01-01&end=2025-12-31", 731)

    # ── day ─────────────────────────────────────────────────────────────────
    def test_all_history_by_day_is_served(self):
        """GUARD: Spiral and Usage Stats load every day."""
        self.assertServed("resolution=day&kind=all&fields=stats")

    def test_the_refusal_names_the_way_round(self):
        _, j = self.get("resolution=block&kind=year&year=2025")
        self.assertIn("kind=range", j.get("error", ""))


if __name__ == "__main__":
    unittest.main()
