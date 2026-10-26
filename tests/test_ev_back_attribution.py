"""
test_ev_back_attribution.py — BL-9/BL-27 fix: back-attribute the EV/Home split on blocks
whose COMPLETED dispatch arrived AFTER the block was priced.

`imp_kwh_ev` is stamped once, at pricing, from the completed dispatch known then. Octopus
reports a slot's completed energy hours later — often after the block was priced with only a
'planned' dispatch — so the real charge is never attributed: it sits in Home. Two instances
that priced the same slot at different times then disagree permanently. On 4.4.0 the fix
rewrites the columns AND the block's segments (the readers' source of truth).

Step 5c (10 Oct 2026): the imp_*_ev columns are gone; the split is the EV segments, read here
through ev_split.

16 Oct 2026 (bill and split paths design §3.3 2, 6b): `_attribute_missing_ev_split` was merged into
the carve (`run_iog_split_carve`), one population for the stages below the final. These cases now
run the carve; two changed meaning, as labelled: imported history and capped accounts are carved
too (the attribution skipped both; the carve already took imported history).
"""
import asyncio

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine
import ev_split
import pricing_segments as ps
from block_store import BlockStore


# engine step 4c: the carve settles through settle(), which asks the tariff kind (the carve never
# did); these fixtures load no agreement, and the carve only ever acts on an Intelligent dispatch
from unittest import mock as _mock
_TK = _mock.patch.object(engine, "tariff_kind_at", lambda bs: "intelligent")


def setUpModule():
    _TK.start()


def tearDownModule():
    _TK.stop()


OFF = 0.05493


class _Sched:
    def is_empty(self):
        return False

    def off_peak_rate_near(self, ts):
        return 5.493

    def day_rate_bounds(self, ts):              # settle() prices from the day's two rates (4c)
        return (5.493, 32.3092)

    def resolve(self, ts):
        return 5.493



class TestEvBackAttribution(unittest.TestCase):

    SLOT = "2026-08-17T02:00:00"

    def setUp(self):
        self.st = BlockStore(":memory:")
        self.st._conn.execute(
            "INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, "
            "block_minutes, timezone) VALUES (1, '2020-01-01T00:00:00', 1, 30, 'UTC')")
        self.st._conn.execute(
            "INSERT OR IGNORE INTO meters (meter_id, config_period_id, is_sub_meter) "
            "VALUES ('electricity_main', 1, 0)")
        engine._store = self.st
        engine._kraken_rate_schedules = {"import": _Sched()}

    def tearDown(self):
        engine._store = None
        engine._kraken_rate_schedules = {}

    def _carve(self):
        return asyncio.run(engine.run_iog_split_carve()).get("carved", 0)

    def _block(self, slot=None, imp_kwh=3.0, source=None, ev=None):
        slot = slot or self.SLOT
        self.st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
            "imp_kwh, imp_rate, imp_cost, source) VALUES (?,?,?,1,?,?,?,?)",
            (slot, slot, "electricity_main", imp_kwh, OFF, round(imp_kwh * OFF, 6), source))
        self.st._conn.commit()
        if ev is not None:                       # an existing split, as its segments
            self.st.set_block_segments(slot, "electricity_main", [
                (ev, OFF, None, "off_peak", "ev"),
                (imp_kwh - ev, OFF, None, "off_peak", "house")])

    def _completed(self, energy=-2.0, slot=None):
        slot = slot or self.SLOT
        self.st._conn.execute(
            "INSERT INTO dispatch_history (slot_start, kind, energy_kwh, first_seen, "
            "last_seen) VALUES (?, 'completed', ?, ?, ?)", (slot, energy, slot, slot))
        self.st._conn.commit()

    def _row(self, slot=None):
        r = self.st._conn.execute(
            "SELECT " + ev_split.sql_kwh("b") + " AS imp_kwh_ev, " + ev_split.sql_cost("b")
            + " AS imp_cost_ev, " + ev_split.sql_band("b", "ev") + " AS imp_ev_band FROM blocks b "
            "WHERE block_start=? AND meter_id='electricity_main'",
            (slot or self.SLOT,)).fetchone()
        r = dict(r)
        r["imp_rate_ev"] = (r["imp_cost_ev"] / r["imp_kwh_ev"]) if r["imp_kwh_ev"] else None
        return r

    def test_attributes_the_segments(self):
        # No split + completed dispatch → EV attributed on the segments (step 5c: the split).
        self._block(imp_kwh=3.0, ev=None)
        self._completed(energy=-2.0)
        self.assertEqual(self._carve(), 1)
        r = self._row()
        self.assertAlmostEqual(r["imp_kwh_ev"], 2.0, places=5)
        self.assertAlmostEqual(r["imp_rate_ev"], OFF, places=5)
        self.assertEqual(r["imp_ev_band"], "off_peak")
        # segments (BL-27 source of truth) now carry the EV slice too
        segs = [ps.Segment(**x) for x in
                self.st.get_block_segments(self.SLOT, "electricity_main")]
        self.assertAlmostEqual(ps.attribution_kwh(segs, "ev"), 2.0, places=5)
        self.assertAlmostEqual(ps.total_kwh(segs), 3.0, places=5)          # reconciles to grid

    def test_clips_ev_to_grid_import(self):
        self._block(imp_kwh=1.5, ev=None)
        self._completed(energy=-4.0)
        self._carve()
        self.assertAlmostEqual(self._row()["imp_kwh_ev"], 1.5, places=5)

    def test_noop_without_completed_dispatch(self):
        self._block(imp_kwh=3.0, ev=None)
        self.assertEqual(self._carve(), 0)
        self.assertIsNone(self._row()["imp_kwh_ev"])

    def test_an_attribution_off_the_dispatch_follows_it(self):
        """REVERSED by engine step 4c: an attributed block was left as it was. A completed dispatch
        that arrives or changes is a fact, so a split off it is settled to it (staging, 24 Oct 2026
        04:00: carved 2.67 kWh, the dispatch restated to 2.55)."""
        self._block(imp_kwh=3.0, ev=1.0)
        self._completed(energy=-2.0)
        self.assertEqual(self._carve(), 1)
        self.assertAlmostEqual(self._row()["imp_kwh_ev"], 2.0, places=5)

    def test_an_attribution_on_the_dispatch_is_left(self):
        """GUARD: already the dispatch's — nothing to write."""
        self._block(imp_kwh=3.0, ev=2.0)
        self._completed(energy=-2.0)
        self.assertEqual(self._carve(), 0)

    def test_imported_history_is_carved_too(self):
        """GUARD (the carve already took it; passes on the parent). Changed for the merged
        population, 16 Oct 2026 (6b): the attribution skipped imported history."""
        self._block(imp_kwh=3.0, ev=None, source="imported_api")
        self._completed(energy=-2.0)
        self.assertEqual(self._carve(), 1)
        self.assertAlmostEqual(self._row()["imp_kwh_ev"], 2.0, places=5)

    def test_idempotent(self):
        self._block(imp_kwh=3.0, ev=None)
        self._completed(energy=-2.0)
        self.assertEqual(self._carve(), 1)
        self.assertEqual(self._carve(), 0)

    def test_a_capped_account_is_carved_too(self):
        """GUARD (the carve already ran capped; passes on the parent). Changed for the merged
        population, 16 Oct 2026 (6b): the attribution ran on uncapped accounts only."""
        from kraken_rates import RateSchedule
        night = ("2026-08-16T22:30:00", "2026-08-17T04:30:00")
        day = ("2026-08-17T04:30:00", "2026-08-17T22:30:00")
        engine._kraken_rate_schedules = {
            "import": RateSchedule([night + (5.493,), day + (32.3092,)]),
            "ev_device_off_peak": RateSchedule([night + (5.493,)]),
            "ev_device_peak": RateSchedule([day + (32.3092,)])}
        self._block(imp_kwh=3.0, ev=None)
        self._completed(energy=-2.0)
        self.assertEqual(self._carve(), 1)


if __name__ == "__main__":
    unittest.main()
