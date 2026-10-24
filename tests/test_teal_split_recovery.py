"""
The recovery asks a teal half-hour's breakdown (18 Oct 2026, the user; reverses bill and split
paths design §7.2, which left these to the user's re-fetch).

Teal on the Import bill is Octopus's cost with EMT's House/EV split: on IOG-SMB, a half-hour the car
drew in (a completed dispatch) whose cached bill is cost-only — fetched before v4 4.5.7 stored the
breakdown, or answered with the cost alone. 69 on the soak's blocks-38. The history drain now asks
their breakdown: an answer is stored and the half-hour re-settled from it, so the split is the
bill's; a breakdown with no EV where EMT had the car is held back (4.5.23); an unanswered one is
asked for up to 30 days, in a record of its own (`split_ask` — the bill-ask record forgets any
half-hour with a cached bill, and these all have one).

Discriminating (fail on the parent): test_the_drain_asks_its_breakdown,
test_the_answer_becomes_its_split, test_it_is_backlog, test_unanswered_is_recorded_and_given_up_after_30_days,
test_a_breakdown_with_no_ev_is_held_back (the parent never asks). The other two are guards.
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
KWH, EMT_EV, BILL_EV = 6.0, 3.0, 3.4


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _ago(days):
    t = datetime.now(timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    return (t - timedelta(days=days)).replace(hour=2).isoformat()


class _Sched:
    def is_empty(self): return False
    def day_rate_bounds(self, ts): return (5.493, 32.3092)
    def resolve(self, ts): return 5.493
    def is_off_peak(self, ts): return True


def _node(ev):
    home = KWH - ev
    return {"home_kwh": home, "ev_kwh": ev, "home_rate": OFF, "ev_rate": OFF if ev else None,
            "home_rate_exc": OFF, "ev_rate_exc": OFF if ev else None,
            "home_cost": round(home * OFF, 6), "ev_cost": round(ev * OFF, 6),
            "home_band": "off_peak", "ev_band": "off_peak" if ev else None}


class _Client:
    def __init__(self, node=None):
        self.node, self.asked = node, []

    async def recover_device_breakdown(self, mpan, slots, **kw):
        self.asked.extend(slots)
        return {s: self.node for s in slots} if self.node else {}

    async def recover_measurement_costs(self, mpan, slots, **kw):
        return {}


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
        engine._kraken_rate_schedules = {"import": _Sched()}
        self.st._conn.execute(
            "INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, "
            "block_minutes, timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
        self.st._conn.commit()
        self.slot = _ago(5)

    def tearDown(self):
        (engine._store, engine._kraken_client, engine._kraken_discovery,
         engine._kraken_rate_schedules, engine._kraken_current_agreement_from,
         engine._MEASURED_HISTORY_DRAIN_PACE) = self._save

    def _teal(self, slot=None, completed=True, corrected=False):
        slot = slot or self.slot
        cost = round(KWH * OFF, 6)
        self.st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "imp_kwh_api, imp_rate, imp_cost, rate_source, rate_corrected) "
            "VALUES (?,?,?,1,?,?,?,?,?,?)",
            (slot, slot, "electricity_main", KWH, KWH, OFF, cost,
             "corrected" if corrected else "settled", 1 if corrected else 0))
        self.st.set_block_segments(slot, "electricity_main", [
            (EMT_EV, OFF, OFF, "off_peak", "ev"), (KWH - EMT_EV, OFF, OFF, "off_peak", "house")])
        self.st.upsert_measured_cost(slot, mpan=MPAN, cost_incl=cost, cost_excl=cost,
                                     label="OFF_PEAK", kwh=KWH)      # cost-only: no breakdown
        if completed:
            self.st._conn.execute(
                "INSERT INTO dispatch_history (slot_start, kind, energy_kwh, first_seen, last_seen) "
                "VALUES (?, 'completed', -3.0, ?, ?)", (slot, slot, slot))
        self.st._conn.commit()

    def _drain(self, node=None):
        engine._kraken_client = c = _Client(node)
        _run(engine.run_measured_history_drain(max_batches=5))
        return c

    def _ev(self, slot=None):
        return self.st.get_ev_split_kwh([slot or self.slot]).get(slot or self.slot)


class TheRecovery(_Harness):

    def test_the_drain_asks_its_breakdown(self):
        self._teal()
        self.assertIn(self.slot, self._drain().asked)

    def test_the_answer_becomes_its_split(self):
        self._teal()
        self._drain(_node(BILL_EV))
        self.assertAlmostEqual(self.st.get_bill_ev_kwh([self.slot])[self.slot], BILL_EV, places=6)
        self.assertAlmostEqual(self._ev(), BILL_EV, places=6)

    def test_it_is_backlog(self):
        self._teal()
        self.assertEqual(engine._measured_history_backlog(), 1)

    def test_unanswered_is_recorded_and_given_up_after_30_days(self):
        self._teal()
        self._drain()
        rec = self.st.ask_record("split")
        self.assertIn(self.slot, rec)
        self.st.ask_record_save("split", {self.slot: {"first": _ago(31)}})
        self.assertNotIn(self.slot, self._drain().asked)
        self.assertEqual(engine._measured_history_backlog(), 0)


class TheGuards(_Harness):

    def test_without_a_completed_dispatch_it_is_not_asked(self):
        """GUARD: no car, no split to correct."""
        self._teal(completed=False)
        self.assertNotIn(self.slot, self._drain().asked)

    def test_a_correction_is_not_asked(self):
        """GUARD: the user's own figure."""
        self._teal(corrected=True)
        self.assertNotIn(self.slot, self._drain().asked)

    def test_a_breakdown_with_no_ev_is_held_back(self):
        """GUARD: the 4.5.23 hold-back judges it as any breakdown."""
        self._teal()
        self._drain(_node(0.0))
        self.assertNotIn(self.slot, self.st.get_bill_ev_kwh([self.slot]))
        self.assertAlmostEqual(self._ev(), EMT_EV, places=6)
        self.assertIn(self.slot, engine._bill_split_wait(self.st))


if __name__ == "__main__":
    unittest.main()
