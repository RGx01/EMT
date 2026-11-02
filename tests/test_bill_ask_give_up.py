"""
A half-hour Octopus never bills is not asked for ever (capped pricing by agreement design §5 (a),
17 Oct 2026, the user).

The live settled-cost pass and the history drain asked again, every pass, for any half-hour with
no cached bill. One Octopus never answers stayed in the drain's backlog, so the hourly tick
rescheduled the drain for it for good: on the soak of 26-30 Sep 2026, 79 of 98 hourly runs were
for the same three half-hours. Now each half-hour is asked for up to 30 days from the first time
Octopus did not answer it, then dropped from both passes, the backlog and the Charts pill.

Discriminating (fail on the parent): test_an_unanswered_half_hour_is_recorded,
test_after_30_days_the_live_pass_stops_asking, test_after_30_days_the_drain_stops_asking,
test_a_given_up_half_hour_is_not_drain_backlog, test_a_given_up_half_hour_is_not_awaiting_a_bill,
test_an_answer_clears_the_record. test_a_cached_bill_prunes_the_record tests the record's own
reader, new code (on the parent it fails only by name). The rest are guards.
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
KWH = 0.4


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


_NODE = {"home_kwh": KWH, "ev_kwh": 0.0, "home_rate": OFF, "ev_rate": None,
         "home_rate_exc": OFF, "ev_rate_exc": None, "home_cost": round(KWH * OFF, 6),
         "ev_cost": 0.0, "home_band": "off_peak", "ev_band": None}


class _Client:
    """Octopus: answers nothing, or `node` for every half-hour asked."""

    def __init__(self, node=None):
        self.node, self.asked = node, []

    async def recover_device_breakdown(self, mpan, slots, **kw):
        self.asked.extend(slots)
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
        engine._kraken_rate_schedules = {"import": _Sched(), "ev_device_off_peak": _Sched()}
        self.st._conn.execute(
            "INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, "
            "block_minutes, timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
        self.st._conn.commit()
        self.slot = _ago(2)
        self._block(self.slot)

    def tearDown(self):
        (engine._store, engine._kraken_client, engine._kraken_discovery,
         engine._kraken_rate_schedules, engine._kraken_current_agreement_from,
         engine._MEASURED_HISTORY_DRAIN_PACE) = self._save

    def _block(self, slot):
        self.st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "imp_kwh_api, imp_rate, imp_cost, rate_source, rate_corrected) "
            "VALUES (?,?,?,1,?,?,?,?,'schedule',0)",
            (slot, slot, "electricity_main", KWH, KWH, OFF, round(KWH * OFF, 6)))
        self.st.set_block_segments(slot, "electricity_main",
                                   [(KWH, OFF, OFF, "off_peak", "house")])
        self.st.upsert_dispatch_slot(slot, off_peak=True, source="smart-charge-completed")
        self.st._conn.commit()

    # The record is read and seeded as stored (kraken_state `bill_ask`), so the tests judge the
    # passes' behaviour on the parent too, not the helpers' names.
    def _first_unanswered(self, days_ago):
        self.st.ask_record_save("bill", {self.slot: {"first": _ago(days_ago)}})

    def _record(self):
        return self.st.ask_record("bill")

    def _live(self, node=None):
        engine._kraken_client = client = _Client(node)
        _run(engine.run_measured_history_drain())
        return client

    def _drain(self, node=None):
        engine._kraken_client = client = _Client(node)
        _run(engine.run_measured_history_drain(max_batches=3))
        return client


class TheLivePass(_Harness):

    def test_an_unanswered_half_hour_is_recorded(self):
        self._live()
        self.assertIn(self.slot, self._record())

    def test_within_30_days_it_is_asked_again(self):
        """GUARD: the ordinary wait for a bill that has not run yet."""
        self._first_unanswered(29)
        self.assertIn(self.slot, self._live().asked)

    def test_after_30_days_it_stops_asking_and_says_when(self):
        """The live pass's, until the one asker took it (engine step 3c): `gave_up` is what
        "abandoned" and the panel's "not asked since" read."""
        self._first_unanswered(31)
        self.assertNotIn(self.slot, self._live().asked)
        self.assertTrue(self._record()[self.slot].get("gave_up"))

    def test_an_answer_clears_the_record(self):
        self._first_unanswered(3)
        self._live(_NODE)
        self.assertNotIn(self.slot, self._record())


class TheDrain(_Harness):

    def test_an_unanswered_half_hour_is_recorded(self):
        self._drain()
        self.assertIn(self.slot, self._record())

    def test_within_30_days_it_is_asked_again(self):
        """GUARD."""
        self._first_unanswered(29)
        self.assertIn(self.slot, self._drain().asked)

    def test_after_30_days_the_drain_stops_asking(self):
        self._first_unanswered(31)
        self.assertNotIn(self.slot, self._drain().asked)

    def test_a_given_up_half_hour_is_not_drain_backlog(self):
        """The drain was rescheduled every hourly tick for it (the soak, 26-30 Sep 2026)."""
        self.assertEqual(engine._measured_history_backlog(), 1)
        self._first_unanswered(31)
        self.assertEqual(engine._measured_history_backlog(), 0)

    def test_the_backlog_count_does_not_mark_it(self):
        """GUARD: the counts only read; the passes that ask mark (and log) it once."""
        self._first_unanswered(31)
        engine._measured_history_backlog()
        self.assertFalse(self._record()[self.slot].get("gave_up"))


class ThePill(_Harness):

    def test_a_given_up_half_hour_is_not_awaiting_a_bill(self):
        since = _ago(10)
        self.assertEqual(engine.settlement_awaiting(self.st, since)["bill"], 1)
        self._first_unanswered(31)
        self.assertEqual(engine.settlement_awaiting(self.st, since)["bill"], 0)


class TheRecord(_Harness):

    def test_a_cached_bill_prunes_the_record(self):
        """A range re-fetch that gets the bill caches it outside the passes."""
        self._first_unanswered(31)
        self.st.upsert_measured_cost(self.slot, mpan=MPAN, cost_incl=0.02, cost_excl=0.02,
                                     label="OFF_PEAK", kwh=KWH)
        self.assertNotIn(self.slot, engine._bill_ask(self.st, MPAN))

    def test_an_unreadable_first_is_not_given_up(self):
        """GUARD: a record with no date never drops a half-hour."""
        self.st.ask_record_save("bill", {self.slot: {"first": ""}})
        self.assertEqual(engine._bill_ask_given_up(engine._bill_ask(self.st, MPAN)), set())


if __name__ == "__main__":
    unittest.main()
