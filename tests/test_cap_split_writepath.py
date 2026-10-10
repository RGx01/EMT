"""
test_cap_split_writepath.py — the BL-9 IOG house/EV split persists through the
finalise write path (_block_rows → _insert_block_rows). Step 5c (10 Oct 2026): an
import channel carrying its priced segments (STAGED_SEGMENTS) has them committed
with the block; a block without them stores no EV split (no regression).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from block_store import BlockStore, STAGED_SEGMENTS
import ev_split


class TestCapSplitWritePath(unittest.TestCase):

    def _store(self):
        st = BlockStore(":memory:")
        with st._conn:
            cp = st._conn.execute(
                "INSERT INTO config_periods (effective_from, billing_day, "
                "block_minutes, timezone, currency_symbol, currency_code) "
                "VALUES ('2024-01-01T00:00:00',1,30,'UTC','£','GBP')").lastrowid
            st._conn.execute("INSERT INTO meters (config_period_id, meter_id, "
                             "is_sub_meter) VALUES (?, 'electricity_main', 0)", (cp,))
        st._conn.commit()
        self._cp = cp
        return st

    def _block(self, imp_extra):
        imp = {"kwh": 3.0, "rate": 0.056667, "cost": 0.17}
        imp.update(imp_extra)
        return {"start": "2026-01-01T13:00:00", "end": "2026-01-01T13:30:00",
                "meters": {"electricity_main": {"channels": {"import": imp}}}}

    def _read(self, st):
        return st._conn.execute(
            "SELECT b.imp_kwh, b.imp_cost, " + ev_split.sql_kwh("b") + " AS ev_kwh, "
            + ev_split.sql_cost("b") + " AS ev_cost "
            "FROM blocks b WHERE b.meter_id='electricity_main'").fetchone()

    def test_split_persists(self):
        st = self._store()
        st.append_block(self._block(
            {STAGED_SEGMENTS: [(2.0, 0.05, 0.047619, "off_peak", "ev"),
                               (1.0, 0.07, 0.066667, "day", "house")]}), self._cp)
        row = self._read(st)
        self.assertAlmostEqual(row["ev_kwh"], 2.0)
        self.assertAlmostEqual(row["ev_cost"], 0.10)
        self.assertAlmostEqual(row["ev_cost"] / row["ev_kwh"], 0.05)
        # house remainder derivable, and unchanged inc totals
        self.assertAlmostEqual(row["imp_kwh"] - row["ev_kwh"], 1.0)
        self.assertAlmostEqual(row["imp_cost"], 0.17)

    def test_absent_split_stores_null(self):
        st = self._store()
        st.append_block(self._block({}), self._cp)   # no segments staged
        row = self._read(st)
        self.assertIsNone(row["ev_kwh"])
        self.assertIsNone(row["ev_cost"])
        self.assertAlmostEqual(row["imp_cost"], 0.17)   # unaffected


if __name__ == "__main__":
    unittest.main()
