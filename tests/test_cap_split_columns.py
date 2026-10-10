"""
test_cap_split_columns.py — BL-9 IOG house/EV split storage.
The dispatch-derived EV portion of a block's import is stored as the main block's
segments with attribution 'ev'; house is the remainder. Step 5c (10 Oct 2026): the
imp_kwh_ev / imp_cost_ev / imp_rate_ev columns are gone — ev_split reads the segments.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from block_store import BlockStore
import ev_split

_COLS = ("imp_kwh_ev", "imp_cost_ev", "imp_rate_ev")


class TestCapSplitColumns(unittest.TestCase):

    def _store(self):
        st = BlockStore(":memory:")
        with st._conn:
            cp = st._conn.execute(
                "INSERT INTO config_periods (effective_from, billing_day, "
                "block_minutes, timezone, currency_symbol, currency_code) "
                "VALUES ('2024-01-01T00:00:00',1,30,'UTC','£','GBP')").lastrowid
            st._conn.execute("INSERT INTO meters (config_period_id, meter_id, "
                             "is_sub_meter) VALUES (?, 'electricity_main', 0)", (cp,))
        self._cp = cp
        st._conn.commit()
        return st

    def _cols(self, st):
        return {r[1] for r in st._conn.execute(
            "PRAGMA table_info(blocks)").fetchall()}

    def _insert(self, st):
        st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
            "imp_kwh, imp_cost, imp_rate, source) "
            "VALUES ('2026-01-01T13:00:00','2026-01-01T13:30:00','electricity_main',?,"
            "3.0, 0.17, 0.056667, 'kraken_api')",
            (self._cp,))

    def _split(self, st):
        return st._conn.execute(
            "SELECT b.imp_kwh, b.imp_cost, " + ev_split.sql_kwh("b") + " AS ev_kwh, "
            + ev_split.sql_cost("b") + " AS ev_cost FROM blocks b").fetchone()

    def test_columns_gone_on_fresh_db(self):
        # Guard (step 5c): the split is not held in columns.
        st = self._store()
        cols = self._cols(st)
        for c in _COLS:
            self.assertNotIn(c, cols)

    def test_null_when_no_ev_segment(self):
        st = self._store()
        self._insert(st)
        row = self._split(st)
        self.assertIsNone(row["ev_kwh"])
        self.assertIsNone(row["ev_cost"])

    def test_write_read_roundtrip(self):
        st = self._store()
        # 3 kWh import, 2 kWh EV @ 0.05 = 0.10, house 1 kWh @ 0.07 = 0.07 → 0.17
        self._insert(st)
        st.set_block_segments("2026-01-01T13:00:00", "electricity_main",
                              [(2.0, 0.05, 0.047619, "off_peak", "ev"),
                               (1.0, 0.07, 0.066667, "day", "house")])
        row = self._split(st)
        self.assertAlmostEqual(row["ev_kwh"], 2.0)
        self.assertAlmostEqual(row["ev_cost"], 0.10)
        self.assertAlmostEqual(row["ev_cost"] / row["ev_kwh"], 0.05)
        # house remainder is derivable, not stored
        self.assertAlmostEqual(row["imp_kwh"] - row["ev_kwh"], 1.0)
        self.assertAlmostEqual(row["imp_cost"] - row["ev_cost"], 0.07)

if __name__ == "__main__":
    unittest.main()
