"""
Step 5b — a block's ex-VAT rate is derived, not stored (10 Oct 2026).

`imp_rate_exc`, `standing_charge_exc` and `exc_source` left the schema. `imp_cost_exc` stays: it
is the per-block ex-VAT authority (import spec §5.2, v15). Where a reader needed the block's
ex-VAT RATE it now takes it from the block's own pair, imp_rate × imp_cost_exc / imp_cost, and
only without one from the VAT calendar (engine._exc_ratio).

Two behaviours moved with it, and these tests pin them:

1. The settlement re-run re-costs a held block to its settled kWh. With no ex-VAT tariff to
   re-derive from, the loaded imp_cost_exc was written back unchanged — stale against the new
   imp_cost, which the stored imp_rate_exc hid. Now the block's own ratio is carried to the new
   cost (engine._carry_exc_to_cost).
2. The predicted-split carve priced a block with no ex-VAT £ at rate / 1.05 — 5% VAT on a day
   the calendar says 0%. Now it takes the calendar.

Discriminating: test_a_held_block_keeps_its_ratio_to_the_new_cost, test_a_zero_vat_day_is_priced_at_zero
and test_the_pair_prices_the_carve fail on the parent (the last because, with no imp_rate_exc
stored, the parent fell to / 1.05 whatever the block's own pair said). The _exc_ratio cases are
guards of new code.
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import test_rebuild_meter_type as H              # noqa: E402  (stubs, real engine, flat tariff)
from block_store import BlockStore               # noqa: E402
engine = H.engine


class TheRatio(unittest.TestCase):
    """GUARD: engine._exc_ratio's order (new code)."""

    def setUp(self):
        self._st = engine._store
        engine._store = BlockStore(":memory:")

    def tearDown(self):
        engine._store = self._st

    def test_a_rate_this_run_derived_comes_first(self):
        self.assertAlmostEqual(engine._exc_ratio(
            {"rate": 0.30, "rate_exc": 0.25, "cost": 1.0, "cost_exc": 0.5}, "2026-09-01T00:00:00"),
            0.25 / 0.30)

    def test_then_the_blocks_own_pair(self):
        self.assertAlmostEqual(engine._exc_ratio(
            {"rate": 0.30, "cost": 1.0, "cost_exc": 0.8}, "2026-09-01T00:00:00"), 0.8)

    def test_then_the_vat_calendar(self):
        self.assertAlmostEqual(engine._exc_ratio({"rate": 0.30}, "2026-09-01T00:00:00"),
                               1 / 1.05)
        self.assertAlmostEqual(engine._exc_ratio({"rate": 0.30}, "2026-10-05T00:00:00"), 1.0)

    def test_or_nothing_when_asked_for_the_pair_only(self):
        self.assertIsNone(engine._exc_ratio({"rate": 0.30}, "2026-09-01T00:00:00",
                                            vat_fallback=False))


class AHeldBlock(H._Harness):
    RATIO = 0.8      # deliberately not the calendar's: only the carried pair gives it

    def _ce(self):
        return engine._store._conn.execute(
            "SELECT imp_cost, imp_cost_exc FROM blocks WHERE block_start=? "
            "AND meter_id='electricity_main'", (H.START,)).fetchone()

    def test_a_held_block_keeps_its_ratio_to_the_new_cost(self):
        self._finalise()
        with engine._store._conn as c:
            c.execute("UPDATE blocks SET rate_source='corrected', imp_cost_exc=ROUND(imp_cost*?, 6) "
                      "WHERE block_start=? AND meter_id='electricity_main'", (self.RATIO, H.START))
        self._settle(dcc=7.5)
        r = self._ce()
        self.assertAlmostEqual(r["imp_cost"], round(7.5 * H.FLAT, 6), places=6)
        self.assertAlmostEqual(r["imp_cost_exc"], round(7.5 * H.FLAT * self.RATIO, 6), places=6)


OFF = 0.05493


class _Sched:
    def is_empty(self): return False
    def day_rate_bounds(self, ts): return (5.493, 32.3092)   # pence
    def is_off_peak(self, ts): return True
    def resolve(self, ts): return 5.493


class _Ev:
    def __init__(self, p): self.p = p
    def resolve(self, ts): return self.p


class TheCarve(unittest.TestCase):
    """The predicted-split carve's EV segment ex-VAT rate (test_iog_split_carve's fixture)."""

    def setUp(self):
        self._save = (engine._store, engine._kraken_rate_schedules)
        self.st = BlockStore(":memory:")
        engine._store = self.st
        engine._kraken_rate_schedules = {"import": _Sched(), "ev_device_off_peak": _Ev(5.493),
                                         "ev_device_peak": _Ev(32.3092)}
        self.st._conn.execute(
            "INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, "
            "block_minutes, timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")

    def tearDown(self):
        (engine._store, engine._kraken_rate_schedules) = self._save

    def _carved_ev_exc(self, slot, cost_exc=None):
        kwh = 3.45
        self.st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "imp_rate, imp_cost, imp_cost_exc, rate_source) VALUES (?,?,?,1,?,?,?,?,'schedule')",
            (slot, slot, "electricity_main", kwh, OFF, round(kwh * OFF, 6), cost_exc))
        self.st.set_block_segments(slot, "electricity_main",
                                   [(kwh, OFF, OFF, "off_peak", "house")])
        self.st._conn.execute(
            "INSERT INTO dispatch_history (slot_start, kind, energy_kwh, first_seen, last_seen) "
            "VALUES (?, 'completed', -3.39, ?, ?)", (slot, slot, slot))
        self.st._conn.commit()
        self.assertEqual(asyncio.run(engine.run_iog_split_carve())["carved"], 1)
        segs = self.st.get_block_segments(slot, "electricity_main")
        return next(s for s in segs if s["attribution"] == "ev")["exc_rate"]

    def test_a_zero_vat_day_is_priced_at_zero(self):
        """No ex-VAT £ on a 0% day (from 1 Oct 2026): the calendar's rate, not / 1.05."""
        self.assertAlmostEqual(self._carved_ev_exc("2026-10-05T02:00:00"), OFF, places=6)

    def test_the_pair_prices_the_carve(self):
        """With an ex-VAT £ the block's own ratio speaks."""
        exc = self._carved_ev_exc("2026-09-10T02:00:00", cost_exc=round(3.45 * OFF * 0.8, 6))
        self.assertAlmostEqual(exc, round(OFF * 0.8, 6), places=6)


if __name__ == "__main__":
    unittest.main()
