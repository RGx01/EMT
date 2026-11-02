"""
test_iog_split_backfill.py — BL-9 one-shot historical EV/Home split backfill.

The split seam only fires on finalise/settlement, so blocks priced before it shipped
(existing dispatch history + already-settled blocks) carry no EV/Home columns and the
billing summary shows no split. The re-price sweep now carves it (the one-shot backfill,
_run_historical_iog_split_backfill, and its runner tests were deleted — BL-33, 4 Oct
2026). Guarantees checked here, on the store side:
  * the lightweight fetch surfaces the split, and omits it when null.
(The sweep's batch split writer, `set_blocks_iog_split`, went with the history reprice sweep in
engine step 4e: settle() writes the split as the EV segments, with the main.)

Step 5c (10 Oct 2026): the imp_*_ev columns are gone and the split is the EV segments. The
paging pair (get/count_blocks_missing_iog_split) went with them, and their tests here; the
lightweight fetch now surfaces the split as the channel's segments.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine
import ev_split
from block_store import BlockStore
from kraken_rates import RateSchedule


# The carve's 2.0 kWh EV / 1.0 kWh house split of the 3.0 kWh block, at its 0.05 rate.
SPLIT = [(2.0, 0.05, None, "off_peak", "ev"), (1.0, 0.05, None, "off_peak", "house")]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestIogSplitBackfill(unittest.TestCase):

    def setUp(self):
        self.st = BlockStore(":memory:")
        engine._store = self.st
        engine._kraken_rate_schedules = {}
        self.st._conn.execute(
            "INSERT INTO config_periods (id, effective_from, billing_day, "
            "block_minutes, timezone, currency_symbol, currency_code) "
            "VALUES (1, '2026-01-01T00:00:00', 1, 30, 'UTC', '£', 'GBP')")
        self.st._conn.commit()
        # A dispatched main-meter block, priced before the split shipped (no EV split).
        self._insert_block("2026-01-01T13:00:00", "electricity_main",
                           imp_kwh=3.0, imp_rate=0.05, imp_cost=0.15, sub=0)
        self._completed("2026-01-01T13:00:00", 2.0)
        # An import schedule so _apply_iog_split can carve.
        engine._kraken_rate_schedules = {"import": RateSchedule(
            [("2026-01-01T00:00:00", None, 0.05)])}

    def tearDown(self):
        engine._store = None
        engine._kraken_rate_schedules = {}

    def _insert_block(self, start, meter_id, *, imp_kwh, imp_rate, imp_cost, sub):
        c = self.st._conn
        c.execute("INSERT OR IGNORE INTO meters (meter_id, config_period_id, "
                  "is_sub_meter) VALUES (?,1,?)", (meter_id, sub))
        end = start[:11] + "13:30:00" if start.endswith("13:00:00") else start
        c.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
            "imp_kwh, imp_rate, imp_cost) VALUES (?,?,?,?,?,?,?)",
            (start, end, meter_id, 1, imp_kwh, imp_rate, imp_cost))
        c.commit()

    def _completed(self, slot_start, energy):
        self.st._conn.execute(
            "INSERT INTO dispatch_history (slot_start, kind, provider, source, "
            "energy_kwh, first_seen, last_seen) VALUES (?,?,?,?,?,?,?)",
            (slot_start, "completed", "test", None, energy,
             slot_start, slot_start))
        self.st._conn.commit()

    def _row(self, start="2026-01-01T13:00:00", meter="electricity_main"):
        return self.st._conn.execute(
            "SELECT " + ev_split.sql_kwh("b") + " AS imp_kwh_ev, "
            "imp_rate, imp_cost FROM blocks b WHERE block_start=? AND meter_id=?",
            (start, meter)).fetchone()

    # ── runner ───────────────────────────────────────────────────────────────
    # ── the render read-path surfaces the split (regression: the lightweight chart
    #    fetch omitted the columns, so 'Import — total grid' fell back to plain rows) ──
    def test_lightweight_fetch_surfaces_split(self):
        self.st.set_block_segments("2026-01-01T13:00:00", "electricity_main", SPLIT)
        blks = self.st.get_blocks_lightweight(
            "2026-01-01T00:00:00", "2026-01-02T00:00:00")
        imp = blks[0]["meters"]["electricity_main"]["channels"]["import"]
        k, c, r = ev_split.from_channel(imp)
        self.assertAlmostEqual(k, 2.0)
        self.assertAlmostEqual(c, 0.10)
        self.assertAlmostEqual(r, 0.05)
        bands = {s["attribution"]: s["band"] for s in imp["segments"]}
        self.assertEqual(bands, {"ev": "off_peak", "house": "off_peak"})

    def test_lightweight_fetch_omits_split_when_null(self):
        # An undispatched block never got the carve → keys absent, not present-as-None
        # (a present None would zero a real import downstream).
        self._insert_block("2026-01-01T18:00:00", "electricity_main",
                           imp_kwh=1.0, imp_rate=0.30, imp_cost=0.30, sub=0)
        blks = self.st.get_blocks_lightweight(
            "2026-01-01T17:00:00", "2026-01-01T19:00:00")
        imp = blks[0]["meters"]["electricity_main"]["channels"]["import"]
        self.assertNotIn("kwh_ev", imp)
        self.assertNotIn("ev_band", imp)
        self.assertIsNone(ev_split.from_channel(imp))


if __name__ == "__main__":
    unittest.main()
