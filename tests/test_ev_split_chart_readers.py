"""
Step 5 — the day charts' EV readers take ev_split's split (8 Oct 2026).

They already preferred the EV segments, but fell to the imp_*_ev columns whenever the EV segments
summed to nothing, so a block with house-only segments and a stale column still drew the column's
EV. ev_split falls back to the columns only for a block with no segments at all (LEGACY). Kept
apart from test_ev_split_readers.py, whose test_server import stubs energy_charts.

Discriminating: both fail on the parent.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class TheChartReaders(unittest.TestCase):
    HOUSE_ONLY = {"kwh": 3.0, "kwh_total": 3.0, "cost": 0.63, "rate": 0.21,
                  "kwh_ev": 2.0, "cost_ev": 0.50, "rate_ev": 0.25,
                  "segments": [{"kwh": 3.0, "inc_rate": 0.21, "band": "off_peak",
                                "attribution": "house"}]}

    def test_the_slot_map_carves_the_dispatch_not_the_stale_columns(self):
        import test_ev_split_prefers_stored as P
        from energy_charts import _dispatch_ev_slot_map
        m = _dispatch_ev_slot_map(P._store(), [P._block(dict(self.HOUSE_ONLY))], P.CFG)
        self.assertAlmostEqual(m[P.SLOT]["cost"], 0.42)    # the dispatch carve, 0.63 * 2/3
        self.assertAlmostEqual(m[P.SLOT]["rate"], 0.21)

    def test_the_rate_line_draws_no_ev_rate(self):
        import chart_emit
        blk = {"meters": {"electricity_main": {"channels": {"import": dict(self.HOUSE_ONLY)}}}}
        s = chart_emit.day_rate_series([(24, blk)], slots=48, block_minutes=30, capped=False,
                                       over_cap=None)
        self.assertIsNone(s["ev"][24])


if __name__ == "__main__":
    unittest.main()
