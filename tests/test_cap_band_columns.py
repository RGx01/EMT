"""
test_cap_band_columns.py — BL-9 EV/Home rate bands persist and surface on the read
channel (ev_band / home_band), so the billing summary can group clean bands and
collapse boundary (mixed) blocks into one transition row. Step 5c (10 Oct 2026): the
imp_ev_band / imp_home_band columns are gone; the bands are the block's segments'
bands ('mixed' where an attribution's segments span two).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from block_store import BlockStore, STAGED_SEGMENTS


class TestCapBandColumns(unittest.TestCase):

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

    def test_columns_gone(self):
        # Guard (step 5c): the bands live only on the segments.
        st = self._store()
        cols = {r[1] for r in st._conn.execute("PRAGMA table_info(blocks)").fetchall()}
        self.assertNotIn("imp_ev_band", cols)
        self.assertNotIn("imp_home_band", cols)

    def test_bands_roundtrip(self):
        st = self._store()
        st.append_block({
            "start": "2026-01-01T18:30:00", "end": "2026-01-01T19:00:00",
            "meters": {"electricity_main": {"channels": {"import": {
                "kwh": 3.0, "rate": 0.19, "cost": 0.57,
                # a boundary block: each attribution spans two bands
                STAGED_SEGMENTS: [(1.0, 0.07, 0.0667, "off_peak", "ev"),
                                  (1.0, 0.43, 0.4095, "peak", "ev"),
                                  (0.5, 0.07, 0.0667, "off_peak", "house"),
                                  (0.5, 0.07, 0.0667, "peak", "house")]}}}}}, self._cp)
        imp = st.get_block_dict_by_start(
            "2026-01-01T18:30:00")["meters"]["electricity_main"]["channels"]["import"]
        self.assertEqual(imp["ev_band"], "mixed")
        self.assertEqual(imp["home_band"], "mixed")

    def test_clean_bands_roundtrip(self):
        st = self._store()
        st.append_block({
            "start": "2026-01-01T03:00:00", "end": "2026-01-01T03:30:00",
            "meters": {"electricity_main": {"channels": {"import": {
                "kwh": 3.0, "rate": 0.07, "cost": 0.21,
                STAGED_SEGMENTS: [(2.0, 0.07, 0.0667, "off_peak", "ev"),
                                  (1.0, 0.07, 0.0667, "day", "house")]}}}}}, self._cp)
        imp = st.get_block_dict_by_start(
            "2026-01-01T03:00:00")["meters"]["electricity_main"]["channels"]["import"]
        self.assertEqual(imp["ev_band"], "off_peak")
        self.assertEqual(imp["home_band"], "day")

    def test_absent_when_null(self):
        st = self._store()
        st.append_block({
            "start": "2026-01-01T02:00:00", "end": "2026-01-01T02:30:00",
            "meters": {"electricity_main": {"channels": {"import": {
                "kwh": 1.0, "rate": 0.07, "cost": 0.07}}}}}, self._cp)
        imp = st.get_block_dict_by_start(
            "2026-01-01T02:00:00")["meters"]["electricity_main"]["channels"]["import"]
        self.assertNotIn("ev_band", imp)
        self.assertNotIn("home_band", imp)


if __name__ == "__main__":
    unittest.main()
