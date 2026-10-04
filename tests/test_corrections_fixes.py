"""
test_corrections_fixes.py — 4.5.7 Historical Corrections panel fixes.

Covers fixes to the corrections surface. (Two more — the review list's timezone and
its band text — went with the review list itself, BL-74, 4 Oct 2026.)
  _cluster_nearby_rates(): fold float/rounding artifacts (e.g. 0.054929 vs the
     canonical 0.054930) into the dominant value, while keeping genuinely-distinct
     Agile rates (which step in 1e-4 increments) separate.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine
import web.server as server


class TestNearbyRateClustering(unittest.TestCase):
    def test_float_artifact_folds_into_dominant(self):
        rows = [{"rate": 0.323092, "count": 204},
                {"rate": 0.054930, "count": 131},
                {"rate": 0.054929, "count": 1}]       # 1e-6 off canonical off-peak
        out = server._cluster_nearby_rates(rows)
        self.assertEqual(len(out), 2)
        peak, off = out[0], out[1]
        self.assertAlmostEqual(peak["rate"], 0.323092)
        self.assertEqual(peak["count"], 204)
        self.assertAlmostEqual(off["rate"], 0.054930)   # dominant value kept
        self.assertEqual(off["count"], 132)             # 131 + 1

    def test_agile_rates_one_step_apart_not_merged(self):
        # Agile unit rates step in 0.01p = 1e-4; two distinct rates exactly 1e-4 apart
        # must survive (strict < epsilon).
        rows = [{"rate": 0.24530, "count": 3},
                {"rate": 0.24540, "count": 2}]
        out = server._cluster_nearby_rates(rows)
        self.assertEqual(len(out), 2)

    def test_empty(self):
        self.assertEqual(server._cluster_nearby_rates([]), [])


class TestMultibandGuard(unittest.TestCase):
    """_corrections_multiband_count flags a genuine cap-transition block (import segments
    spanning a real off-peak<->peak rate gap) so the single-rate corrections tool can refuse it,
    while ignoring same-rate EV/house label pairs and float/rounding noise."""

    def _store(self):
        from block_store import BlockStore
        st = BlockStore(":memory:")
        st._conn.execute(
            "INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, "
            "block_minutes, timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
        return st

    def _seg(self, st, bs, seq, rate, band, attr):
        st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "imp_rate) VALUES (?,?,?,1,1,?) ON CONFLICT DO NOTHING", (bs, bs, "electricity_main", rate))
        st._conn.execute(
            "INSERT INTO block_segments (block_start, meter_id, channel, seq, kwh, inc_rate, "
            "exc_rate, band, attribution) VALUES (?,?,?,?,?,?,?,?,?)",
            (bs, "electricity_main", "import", seq, 1, rate, rate*0.95, band, attr))
        st._conn.commit()

    def test_genuine_transition_flagged(self):
        st = self._store()
        self._seg(st, "2020-01-01T12:00:00", 1, 0.05493,  "off_peak", "ev")
        self._seg(st, "2020-01-01T12:00:00", 2, 0.323092, "peak",     "house")
        self.assertEqual(server._corrections_multiband_count(
            st, "meter_id='electricity_main'", []), 1)

    def test_same_rate_label_pair_not_flagged(self):
        st = self._store()
        self._seg(st, "2020-01-01T12:00:00", 1, 0.323092, "peak", "ev")
        self._seg(st, "2020-01-01T12:00:00", 2, 0.323092, "day",  "house")
        self.assertEqual(server._corrections_multiband_count(
            st, "meter_id='electricity_main'", []), 0)

    def test_float_noise_not_flagged(self):
        st = self._store()
        self._seg(st, "2020-01-01T12:00:00", 1, 0.05493,  "off_peak", "ev")
        self._seg(st, "2020-01-01T12:00:00", 2, 0.054929, "off_peak", "house")
        self.assertEqual(server._corrections_multiband_count(
            st, "meter_id='electricity_main'", []), 0)
