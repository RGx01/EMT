"""
v4 4.5.23 (#510), ported as forward code — a bill that drops the car's half-hour is not settled yet.

On 2026-10-10 Octopus's device breakdown, fetched ~29 h after the night, put every dispatched
half-hour wholly on the house (EV 0 kWh) and billed 05:30-06:00 BST at the standard rate. Days
later it served the true split. The breakdown was cached and applied as the authority, so EMT's own
EV split for the night was replaced for good.

Now, where EMT's split (the block's EV segments) has the car drawing and the bill says the EV drew
nothing, the bill is held back — not cached, not applied, the segments kept — and asked for again
on later passes; after 28 days EMT stops asking and keeps its own split. v4's one-off re-fetch is
not ported: the 4.5.23 floor guarantees it ran.

Discriminating (fail on the parent): test_a_bill_with_no_ev_is_held_back,
test_after_28_days_emt_keeps_its_split_and_stops_asking, test_the_drain_holds_it_back_too,
test_a_held_slot_is_not_drain_backlog, test_the_wait_crosses_an_import. The rest are guards.
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine                                    # noqa: E402
from block_store import BlockStore               # noqa: E402

OFF = 0.05493
MPAN = "2000000000001"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _slot(days_ago):
    t = datetime.now(timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    return (t - timedelta(days=days_ago)).replace(hour=2).isoformat()


class _Sched:
    def is_empty(self): return False
    def day_rate_bounds(self, ts): return (5.493, 32.3092)


def _node(home, ev, rate=OFF, band="off_peak"):
    return {"home_kwh": home, "ev_kwh": ev, "home_rate": rate, "ev_rate": rate if ev else None,
            "home_rate_exc": rate, "ev_rate_exc": rate if ev else None,
            "home_cost": round(home * rate, 6), "ev_cost": round(ev * rate, 6),
            "home_band": band, "ev_band": band if ev else None}


class _Client:
    def __init__(self, node):
        self.node, self.asked = node, []

    async def recover_device_breakdown(self, mpan, slots, **kw):
        self.asked.extend(slots)
        return {s: self.node for s in slots}


class _Harness(unittest.TestCase):
    KWH, EV = 6.672, 3.13

    def setUp(self):
        self._save = (engine._store, engine._kraken_client, engine._kraken_discovery,
                      engine._kraken_rate_schedules, engine._kraken_current_agreement_from,
                      engine._MEASURED_HISTORY_DRAIN_PACE)
        self.st = BlockStore(":memory:")
        engine._store = self.st
        engine._kraken_current_agreement_from = "2026-01-01T00:00:00"
        engine._MEASURED_HISTORY_DRAIN_PACE = 0
        engine._kraken_discovery = {"import": {"mpan": MPAN, "tariff_code": "IOG-SMB-FIX-12M"}}
        engine._kraken_rate_schedules = {"import": _Sched(), "ev_device_off_peak": _Sched()}
        self.st._conn.execute(
            "INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, "
            "block_minutes, timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
        self.st._conn.commit()

    def tearDown(self):
        (engine._store, engine._kraken_client, engine._kraken_discovery,
         engine._kraken_rate_schedules, engine._kraken_current_agreement_from,
         engine._MEASURED_HISTORY_DRAIN_PACE) = self._save

    def _block(self, slot, ev=EV):
        self.st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "imp_kwh_api, imp_rate, imp_cost, rate_source, rate_corrected) "
            "VALUES (?,?,?,1,?,?,?,?,'schedule',0)",
            (slot, slot, "electricity_main", self.KWH, self.KWH, OFF, round(self.KWH * OFF, 6)))
        segs = [(self.KWH - (ev or 0.0), OFF, OFF, "off_peak", "house")]
        if ev:
            segs.insert(0, (ev, OFF, OFF, "off_peak", "ev"))
        self.st.set_block_segments(slot, "electricity_main", segs)
        self.st.upsert_dispatch_slot(slot, off_peak=True, source="smart-charge-completed")
        self.st._conn.commit()

    def _cached(self, slot):
        return self.st._conn.execute("SELECT * FROM measured_cost WHERE slot_start = ?",
                                     (slot,)).fetchone()

    def _emt_ev(self, slot):
        return self.st.get_ev_split_kwh([slot]).get(slot)


class TheLivePass(_Harness):

    def _pass(self, node):
        engine._kraken_client = client = _Client(node)
        return _run(engine.run_measured_history_drain()), client

    def test_a_bill_with_no_ev_is_held_back(self):
        slot = _slot(1)
        self._block(slot)
        res, _ = self._pass(_node(self.KWH, 0.0))
        self.assertEqual(res["held"], 1)
        self.assertIsNone(self._cached(slot), "the house-only bill was cached")
        self.assertAlmostEqual(self._emt_ev(slot), self.EV, places=6)
        self.assertIn(slot, engine._bill_split_wait(self.st))

    def test_it_is_asked_again_and_settles_once_octopus_has_the_car(self):
        """GUARD of the new path's second half."""
        slot = _slot(1)
        self._block(slot)
        self._pass(_node(self.KWH, 0.0))
        res, _ = self._pass(_node(self.KWH - 3.1304, 3.1304))
        self.assertEqual(res["stored"], 1)
        self.assertAlmostEqual(self._cached(slot)["ev_kwh"], 3.1304, places=6)
        self.assertNotIn(slot, engine._bill_split_wait(self.st))

    def test_after_28_days_emt_keeps_its_split_and_stops_asking(self):
        slot = _slot(30)
        self._block(slot)
        engine._bill_split_wait_save(self.st, {slot: {"first": _slot(29)}})
        _, client = self._pass(_node(self.KWH, 0.0))
        self.assertNotIn(slot, client.asked, "still asking after 28 days")
        self.assertIsNone(self._cached(slot))
        self.assertTrue(engine._bill_split_wait(self.st)[slot].get("kept"))

    def test_a_bill_with_no_ev_where_emt_had_none_is_cached_as_before(self):
        """GUARD: no disagreement, nothing held — the bill is the authority."""
        slot = _slot(1)
        self._block(slot, ev=None)
        res, _ = self._pass(_node(self.KWH, 0.0))
        self.assertEqual((res["stored"], res.get("held", 0)), (1, 0))

    def test_a_bill_with_the_car_is_cached_as_before(self):
        """GUARD: the ordinary case."""
        slot = _slot(1)
        self._block(slot)
        res, _ = self._pass(_node(3.54, 3.13))
        self.assertEqual((res["stored"], res.get("held", 0)), (1, 0))


class TheDrain(_Harness):

    def test_the_drain_holds_it_back_too(self):
        slot = _slot(2)
        self._block(slot)
        engine._kraken_client = _Client(_node(self.KWH, 0.0))
        _run(engine.run_measured_history_drain(max_batches=3))
        self.assertIsNone(self._cached(slot), "the drain cached the house-only bill")
        self.assertIn(slot, engine._bill_split_wait(self.st))

    def test_a_held_slot_follows_the_backoff(self):
        """Engine step 3c (24 Oct 2026): a held half-hour is asked on the same backoff as an
        unanswered one — hourly for two days, then daily — not every pass for 28 days, and not left
        out of the backlog altogether (which asked it only when other backlog ran the drain)."""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        ago = lambda **kw: (now - timedelta(**kw)).isoformat()
        slot = _slot(2)
        self._block(slot)
        self.assertEqual(engine._measured_history_backlog(), 1)
        engine._bill_split_wait_save(self.st, {slot: {"first": ago(days=3), "last": ago(hours=1)}})
        self.assertEqual(engine._measured_history_backlog(), 0)            # resting today
        engine._bill_split_wait_save(self.st, {slot: {"first": ago(days=3), "last": ago(hours=25)}})
        self.assertEqual(engine._measured_history_backlog(), 1)            # its day has come


class TheImport(unittest.TestCase):

    def test_the_wait_crosses_an_import(self):
        """A 4.5.23 backup carries it; an unknown key would abort the import. v4's one-off marker
        is a completion marker and drops."""
        from v4import import rules
        self.assertEqual(rules.key_rule("kraken_state", "bill_split_wait"), "carry")
        self.assertEqual(rules.key_rule("kraken_state", "bill_split_refetch_done"), "drop")


class TheRule(unittest.TestCase):
    """GUARD (new code): what counts as the bill dropping the car."""

    def test_no_ev_on_the_bill_against_emts_split(self):
        self.assertTrue(engine._bill_drops_ev(3.13, 0.0))

    def test_any_ev_on_the_bill_is_a_split(self):
        self.assertFalse(engine._bill_drops_ev(3.13, 0.2))

    def test_no_split_of_emts_own_is_no_disagreement(self):
        self.assertFalse(engine._bill_drops_ev(None, 0.0))
        self.assertFalse(engine._bill_drops_ev(0.01, 0.0))


if __name__ == "__main__":
    unittest.main()
