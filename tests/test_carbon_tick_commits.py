"""
#65 (3 Oct 2026) — the carbon tick and carbon recovery held the engine loop.

Every 15 minutes `_tick_carbon_intensity` fetched from the Carbon Intensity API with a
blocking `urlopen` (8 s timeout) ON the engine loop, then wrote ~96 slots and ~96
generation mixes with a commit each. Over a 137-hour Pi log, 54% of ticks were followed
within 20 s by a loop-lag warning (median 2.6 s), against 5% by chance. After a restore,
`_recover_missing_carbon` then committed once per block and meter and rebuilt the whole
configuration per block for the postcode: 95 blocks held the loop 7.6 s.

Now the fetch runs in an executor (it touches no database; the writes stay on the loop
thread, which owns the connection), the slots and mixes are one transaction, and
recovery reads the postcode once and writes in one transaction.

Tests marked GUARD pass on the parent tree too. The rest fail there.
"""
import asyncio
import datetime as dt
import os
import sys
import threading
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import engine                                     # noqa: E402
from block_store import BlockStore                # noqa: E402

PC = "DE1"
STARTS = ["2026-06-05T%02d:%s:00" % (h, m) for h in range(18, 23) for m in ("00", "30")]


class _Base(unittest.TestCase):

    def setUp(self):
        self.st = BlockStore(":memory:")
        self.st.insert_config_period({"meters": {
            "electricity_main": {"meta": {"timezone": "Europe/London", "billing_day": 1,
                                          "block_minutes": 30, "currency_symbol": "£",
                                          "sub_meter": False, "postcode_prefix": PC}},
            "heat_pump": {"meta": {"sub_meter": True, "device": "Heat Pump"}}}})
        self.cp = self.st._conn.execute("SELECT id FROM config_periods LIMIT 1").fetchone()["id"]
        self._saved = (engine._store, engine._fetch_carbon_intensity,
                       engine._maybe_backfill_historical_carbon, engine._last_ci_fetch,
                       engine._get_postcode)
        engine._store = self.st
        engine._maybe_backfill_historical_carbon = lambda: None
        self._mix = dict(engine._current_slot_mix)
        engine._current_slot_mix.clear()
        self.commits = []
        self.st._conn.set_trace_callback(
            lambda q: self.commits.append(q) if q.strip().upper().startswith("COMMIT") else None)

    def tearDown(self):
        self.st._conn.set_trace_callback(None)
        engine._current_slot_mix.clear()
        engine._current_slot_mix.update(self._mix)
        (engine._store, engine._fetch_carbon_intensity,
         engine._maybe_backfill_historical_carbon, engine._last_ci_fetch,
         engine._get_postcode) = self._saved

    def _gap_blocks(self):
        """Ten gap-fill half-hours with energy and no carbon, on two meters, and a CI
        slot for each."""
        with self.st._conn:
            for i, bs in enumerate(STARTS):
                for mid, k in (("electricity_main", 1.0 + i / 10), ("heat_pump", 0.4)):
                    self.st._conn.execute(
                        "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                        "interpolated, imp_kwh, standing_charge, carbon_intensity_g, source) "
                        "VALUES (?,?,?,?,1,?,0.5,NULL,'gap_fill')", (bs, bs, mid, self.cp, k))
        for i, bs in enumerate(STARTS):
            self.st.upsert_carbon_intensity(bs, PC, 200.0 + i, "high", None)
        self.commits.clear()


class TheRecovery(_Base):

    def test_one_commit_per_pass(self):
        self._gap_blocks()
        self.assertEqual(engine._recover_missing_carbon(), len(STARTS))
        self.assertEqual(len(self.commits), 1, "%d commits" % len(self.commits))

    def test_one_postcode_lookup_per_pass(self):
        self._gap_blocks()
        calls = []
        real = self._saved[4]
        engine._get_postcode = lambda: calls.append(1) or real()
        engine._recover_missing_carbon()
        self.assertEqual(len(calls), 1)

    def test_what_it_writes(self):
        """GUARD: each meter's carbon is its kWh × the slot's intensity, written in
        place — the source tag survives."""
        self._gap_blocks()
        engine._recover_missing_carbon()
        for i, bs in enumerate(STARTS):
            for mid, k in (("electricity_main", 1.0 + i / 10), ("heat_pump", 0.4)):
                r = self.st._conn.execute(
                    "SELECT carbon_intensity_g, carbon_g, source FROM blocks "
                    "WHERE block_start=? AND meter_id=?", (bs, mid)).fetchone()
                self.assertAlmostEqual(r["carbon_intensity_g"], 200.0 + i, places=4)
                self.assertAlmostEqual(r["carbon_g"], k * (200.0 + i), places=2)
                self.assertEqual(r["source"], "gap_fill")

    def test_no_postcode_no_work(self):
        """GUARD: nothing is attributed without a postcode."""
        self._gap_blocks()
        engine._get_postcode = lambda: None
        self.assertEqual(engine._recover_missing_carbon(), 0)
        self.assertEqual(self.commits, [])


# Recent slots: the tick prunes intensity older than 4 days and mixes older than 48 h.
_T0 = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)
       - dt.timedelta(hours=6))


def _at(i):
    return (_T0 + dt.timedelta(minutes=30 * i)).isoformat()


def _slots(n=6, bad_at=None):
    out = []
    for i in range(n):
        mix = [{"fuel": "gas", "perc": 40.0}, {"fuel": "wind", "perc": 60.0}]
        if i == bad_at:                       # BL-38: a regional-model glitch slot
            mix = [{"fuel": "wind", "perc": 100.0}]
        out.append({"captured_at": _at(i), "intensity_forecast":
                    0.0 if i == bad_at else 150.0 + i, "ci_index": "moderate",
                    "intensity_actual": None, "generationmix": mix})
    return out


class TheTick(_Base):

    def _tick(self, slots):
        seen = {}

        def _fetch(pc):
            seen["thread"] = threading.current_thread()
            return slots
        engine._fetch_carbon_intensity = _fetch
        engine._last_ci_fetch = None
        main = threading.current_thread()
        asyncio.run(engine._tick_carbon_intensity())
        return seen["thread"], main

    def test_the_fetch_runs_off_the_loop(self):
        t, loop_thread = self._tick(_slots())
        self.assertIsNot(t, loop_thread, "the API fetch ran on the engine loop's thread")

    def test_the_slots_are_one_transaction(self):
        """Six slots with mixes: one write, plus the two prunes. 14 on the parent."""
        self._tick(_slots())
        self.assertLessEqual(len(self.commits), 3, "%d commits" % len(self.commits))

    def test_what_the_tick_stores(self):
        """GUARD: every plausible slot and its mix lands; the glitch slot does not."""
        self._tick(_slots(bad_at=2))
        got = [r[0] for r in self.st._conn.execute(
            "SELECT captured_at FROM carbon_intensity WHERE postcode=? ORDER BY 1", (PC,))]
        self.assertEqual(got, [s["captured_at"] for i, s in enumerate(_slots()) if i != 2])
        n_mix = self.st._conn.execute("SELECT COUNT(*) FROM mix_history").fetchone()[0]
        self.assertEqual(n_mix, 5 * 2)
        self.assertNotIn(_at(2), engine._current_slot_mix)


if __name__ == "__main__":
    unittest.main()
