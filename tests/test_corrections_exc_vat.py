"""
test_corrections_exc_vat.py — BL-57b.

The Cost-Corrections tool takes an inc-VAT rate. set_import_exc_from_inc re-derives
block + segment ex-VAT from the corrected inc via the VAT calendar — authoritatively,
including a block whose exc was NULL (the escape that used to fall back to inc÷1.05).
"""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from block_store import BlockStore
import pricing_segments as ps


class TestCorrectionsExcVat(unittest.TestCase):
    def setUp(self):
        self.st = BlockStore(":memory:")
        self.st._conn.execute(
            "INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, "
            "block_minutes, timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
        self.vat = self.st.vat_rate_at("2026-08-14T19:00:00")   # flat domestic (0.05)

    def _blk(self, slot, inc_rate, kwh=2.0, exc=None):
        self.st._conn.execute(
            "INSERT INTO blocks (block_start,block_end,meter_id,config_period_id,"
            "imp_kwh,imp_rate,imp_cost,imp_cost_exc) VALUES (?,?,?,?,?,?,?,?)",
            (slot, slot, "electricity_main", 1, kwh, inc_rate, round(kwh*inc_rate,6),
             (round(kwh*exc,6) if exc is not None else None)))
        self.st._conn.commit()

    def test_derives_exc_from_inc_incl_null(self):
        f = 1.0/(1.0+self.vat)
        # A: had a (stale) exc; B: had NULL exc (the escape) — both corrected to inc=0.30
        self._blk("2026-08-14T19:00:00", 0.30, exc=0.2857)   # stale ratio
        self._blk("2026-08-15T20:00:00", 0.30, exc=None)     # NULL exc
        n = self.st.set_import_exc_from_inc(
            ["2026-08-14T19:00:00", "2026-08-15T20:00:00"])
        self.assertEqual(n, 2)
        for slot in ("2026-08-14T19:00:00", "2026-08-15T20:00:00"):
            r = self.st._conn.execute(
                "SELECT imp_rate, imp_cost, imp_cost_exc "
                "FROM blocks WHERE block_start=?", (slot,)).fetchone()
            # imp_rate_exc and exc_source dropped (step 5b, 10 Oct 2026): the ex-VAT rate is
            # derived from the pair, so the £ carries it.
            self.assertAlmostEqual(r["imp_cost_exc"], round(r["imp_cost"]*f, 6), places=6)
            # exc/inc ratio is a valid VAT ratio (0.95..), never > 1 or stale
            self.assertAlmostEqual(r["imp_cost_exc"]/r["imp_cost"], f, places=4)  # valid VAT ratio, not stale

    def test_segments_exc_follow(self):
        f = 1.0/(1.0+self.vat)
        self._blk("2026-08-14T19:00:00", 0.30)
        self.st.set_block_segments("2026-08-14T19:00:00", "electricity_main", [
            ps.Segment(1.0, 0.30, 0.99, "peak", "ev"),      # deliberately wrong exc
            ps.Segment(1.0, 0.30, 0.99, "day", "house")])
        self.st.set_import_exc_from_inc(["2026-08-14T19:00:00"])
        segs = [ps.Segment(**x) for x in
                self.st.get_block_segments("2026-08-14T19:00:00", "electricity_main")]
        for sg in segs:
            self.assertAlmostEqual(sg.exc_rate, round(0.30*f, 6), places=6)


if __name__ == "__main__":
    unittest.main()
