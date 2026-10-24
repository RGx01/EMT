"""
The live bill pass and the history drain save their changes to the bill records, not whole copies
(17 Oct 2026).

Each pass reads the 4.5.23 hold-back's wait map (`bill_split_wait`) and the bill-ask record
(`bill_ask`), waits on Octopus, then saves. They run as separate tasks on the loop — the live
pass in the dispatch tick, the drain as its own task — so one's whole-copy save overwrote what
the other had saved while it waited. Here both are held inside their Octopus call until each has
read its records, then released: on the parent the second save drops the first one's half-hour.

Discriminating (fail on the parent): test_both_unanswered_half_hours_are_recorded,
test_both_held_bills_are_kept (on the parent the drain's half-hour is lost). TheMerge tests the new
helper, so it fails on the parent only by name.
"""
import asyncio
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine                                    # noqa: E402
from block_store import BlockStore               # noqa: E402

OFF = 0.05493
MPAN = "2000000000001"
KWH, EV = 6.672, 3.13


def _at(hours_ago):
    t = datetime.now(timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    return (t - timedelta(hours=hours_ago)).isoformat()


LIVE = _at(2)       # inside the drain's six-hour cutoff: the live pass's only
DRAIN = _at(48)     # imported: the drain's only (the live pass leaves imported history alone)


def _house_only():
    return {"home_kwh": KWH, "ev_kwh": 0.0, "home_rate": OFF, "ev_rate": None,
            "home_rate_exc": OFF, "ev_rate_exc": None, "home_cost": round(KWH * OFF, 6),
            "ev_cost": 0.0, "home_band": "off_peak", "ev_band": None}


class _Gate:
    """Octopus, holding every caller until two are waiting — so both passes have read their
    records before either saves."""

    def __init__(self, node=None):
        self.node, self.waiting = node, 0
        self.both = None

    async def recover_device_breakdown(self, mpan, slots, **kw):
        if self.both is None:
            self.both = asyncio.Event()
        self.waiting += 1
        if self.waiting >= 2:
            self.both.set()
        await asyncio.wait_for(self.both.wait(), 5)
        return {s: self.node for s in slots} if self.node else {}


class _Harness(unittest.TestCase):

    def setUp(self):
        self._save = (engine._store, engine._kraken_client, engine._kraken_discovery,
                      engine._kraken_rate_schedules, engine._kraken_current_agreement_from,
                      engine._MEASURED_HISTORY_DRAIN_PACE)
        self.st = BlockStore(":memory:")
        engine._store = self.st
        engine._kraken_current_agreement_from = "2026-01-01T00:00:00"
        engine._MEASURED_HISTORY_DRAIN_PACE = 0
        engine._kraken_discovery = {"import": {"mpan": MPAN, "tariff_code": "IOG-SMB-FIX-12M"}}
        engine._kraken_rate_schedules = {}
        self.st._conn.execute(
            "INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, "
            "block_minutes, timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
        for slot, live in ((LIVE, True), (DRAIN, False)):
            self.st._conn.execute(
                "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
                "imp_kwh_api, imp_rate, imp_cost, rate_source, rate_corrected, source) "
                "VALUES (?,?,?,1,?,?,?,?,'schedule',0,?)",
                (slot, slot, "electricity_main", KWH, KWH if live else None, OFF,
                 round(KWH * OFF, 6), None if live else "imported_api"))
            self.st.set_block_segments(slot, "electricity_main", [
                (EV, OFF, OFF, "off_peak", "ev"), (KWH - EV, OFF, OFF, "off_peak", "house")])
        self.st.upsert_dispatch_slot(LIVE, off_peak=True, source="smart-charge-completed")
        self.st._conn.commit()

    def tearDown(self):
        (engine._store, engine._kraken_client, engine._kraken_discovery,
         engine._kraken_rate_schedules, engine._kraken_current_agreement_from,
         engine._MEASURED_HISTORY_DRAIN_PACE) = self._save

    def _both(self, node=None):
        engine._kraken_client = _Gate(node)

        async def go():
            await asyncio.gather(engine.measure_settled_dispatched_blocks(),
                                 engine.run_measured_history_drain(max_batches=2))
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(go())
        finally:
            loop.close()

    def _record(self, key):
        # the ask record, by the kraken_state key each kind had until engine step 3b
        return self.st.ask_record({"bill_ask": "bill", "split_ask": "split",
                                   "bill_split_wait": "hold"}[key])


class TheRace(_Harness):

    def test_both_unanswered_half_hours_are_recorded(self):
        self._both()
        self.assertEqual(set(self._record("bill_ask")), {LIVE, DRAIN})

    def test_both_held_bills_are_kept(self):
        self._both(_house_only())
        self.assertEqual(set(self._record("bill_split_wait")), {LIVE, DRAIN})


class TheMerge(unittest.TestCase):
    """GUARD (new code): what a save applies."""

    def _merge(self, stored, before, after):
        box = {"m": dict(stored)}
        engine._save_changes(None, before, after, lambda: dict(box["m"]),
                             lambda _st, m: box.__setitem__("m", m))
        return box["m"]

    def test_an_addition_keeps_the_others(self):
        self.assertEqual(self._merge({"x": 1, "y": 2}, {"x": 1}, {"x": 1, "z": 3}),
                         {"x": 1, "y": 2, "z": 3})

    def test_a_removal_removes_only_its_own(self):
        self.assertEqual(self._merge({"x": 1, "y": 2}, {"x": 1}, {}), {"y": 2})

    def test_a_change_is_applied(self):
        self.assertEqual(self._merge({"x": {"first": "a"}, "y": 2}, {"x": {"first": "a"}},
                                     {"x": {"first": "a", "gave_up": "b"}}),
                         {"x": {"first": "a", "gave_up": "b"}, "y": 2})

    def test_no_change_no_write(self):
        writes = []
        engine._save_changes(None, {"x": 1}, {"x": 1}, lambda: {}, lambda *a: writes.append(a))
        self.assertEqual(writes, [])


if __name__ == "__main__":
    unittest.main()
