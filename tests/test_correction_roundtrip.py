"""4.5.11: a user Cost-Correction (rate_corrected=1 / rate_source='corrected') — and the
reconciled provenance — must SURVIVE a read -> modify -> append_block_replace round-trip.
They weren't carried onto the block dict nor written by the append path, so any round-trip
(gap-fill, carbon/remainder recompute, PASS 2 re-run) silently reset them to the column
defaults — un-protecting the correction, which the dispatch reconcile then reverted (the
negative-Home strand on SMB). Exposed when 4.5.7 removed the IOG import gate.
The rate_reconciled flag was dropped in step 5 (10 Oct 2026); a reconciliation is
rate_source='reconciled' alone, so that is what the second test carries."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from block_store import BlockStore

OFF, PK = 0.05493, 0.323092


class TestCorrectionRoundTrip(unittest.TestCase):
    def setUp(self):
        self.st = BlockStore(":memory:")
        self.st.insert_config_period({"meters": {"electricity_main": {"meta": {
            "billing_day": 1, "block_minutes": 30, "timezone": "UTC",
            "currency_symbol": "£", "currency_code": "GBP", "site": "Home"}}}})
        self.cp = self.st._conn.execute("SELECT id FROM config_periods LIMIT 1").fetchone()[0]

    def _ins(self, slot, rate, *, rate_corrected=0, rate_source=None,
             kwh=3.0, ev_kwh=None, ev_rate=None):
        self.st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "imp_rate, imp_cost, rate_corrected, rate_source) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (slot, slot, "electricity_main", self.cp, kwh, rate, round(kwh*rate, 6),
             rate_corrected, rate_source))
        self.st._conn.commit()
        if ev_kwh is not None:
            # the EV split is the block's segments (step 5c: the imp_*_ev columns went)
            self.st.set_block_segments(slot, "electricity_main", [
                (ev_kwh, ev_rate, None, "peak", "ev"),
                (round(kwh - ev_kwh, 6), rate, None, "day", "house")])

    def _flags(self, slot):
        return self.st._conn.execute(
            "SELECT rate_corrected, rate_source, imp_rate "
            "FROM blocks WHERE block_start=? AND meter_id='electricity_main'", (slot,)).fetchone()

    def _ev_rate(self, slot):
        ev = [x for x in self.st.get_block_segments(slot, "electricity_main")
              if x["attribution"] == "ev"]
        return ev[0]["inc_rate"] if ev else None

    def _roundtrip(self, slot):
        # read the block dict, then write it straight back (the exact round-trip shape)
        blocks = self.st.get_blocks_for_utc_range("2000-01-01T00:00:00", "2100-01-01T00:00:00")
        target = next(b for b in blocks if b["start"] == slot)
        self.st.append_block_replace(target, self.cp)

    def test_corrected_block_survives_roundtrip(self):
        slot = "2026-08-30T12:00:00"
        self._ins(slot, PK, rate_corrected=1, rate_source="corrected",
                  kwh=5.276, ev_kwh=3.3, ev_rate=PK)
        self._roundtrip(slot)
        r = self._flags(slot)
        self.assertEqual(r["rate_corrected"], 1)          # correction survives
        self.assertEqual(r["rate_source"], "corrected")   # authority survives
        self.assertAlmostEqual(r["imp_rate"], PK, places=5)
        self.assertAlmostEqual(self._ev_rate(slot), PK, places=5)   # the EV split survives

    def test_reconciled_source_survives_roundtrip(self):
        slot = "2026-08-30T13:00:00"
        self._ins(slot, OFF, rate_source="reconciled")
        self._roundtrip(slot)
        r = self._flags(slot)
        self.assertEqual(r["rate_source"], "reconciled")

    def test_fresh_block_defaults_unchanged(self):
        slot = "2026-08-30T14:00:00"
        self._ins(slot, OFF)                              # uncorrected, schedule
        self._roundtrip(slot)
        r = self._flags(slot)
        self.assertEqual(r["rate_corrected"], 0)
        self.assertIsNone(r["rate_source"])


if __name__ == "__main__":
    unittest.main()
