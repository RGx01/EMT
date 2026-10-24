"""
A bill Octopus has not answered is asked hourly for 2 days, then daily (20 Oct 2026, the user: go
easier on the API).

Every pass that asks — the live settled-cost pass, the history drain, and the drain's breakdown
asks for the teal half-hours — asked each unanswered half-hour again every hourly run, for up to 30
days: on staging (20 Oct) 12 half-hours were asked every hour, `recovered 0/12` each time. Now the
record keeps the last ask; past its first 2 days a half-hour is asked at most once a day, until the
30 days run out as before. A record from before (no `last`) is asked, then daily. The backlog does
not count a resting half-hour, so it does not reschedule the drain for it.

Discriminating (fail on the parent), except the GUARDs marked.
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine                                    # noqa: E402
from tests.test_bill_ask_give_up import _Harness, _ago   # noqa: E402

NOW = datetime(2026, 10, 20, 12, 0)


def _hours_ago(h):
    return (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=h)).isoformat()


class TheRule(unittest.TestCase):

    def _resting(self, first_days, last_hours):
        ent = {"first": (NOW - timedelta(days=first_days)).isoformat()}
        if last_hours is not None:
            ent["last"] = (NOW - timedelta(hours=last_hours)).isoformat()
        return "s" in engine._bill_ask_resting({"s": ent}, now=NOW)

    def test_hourly_for_the_first_two_days(self):
        """GUARD of the parent's rule, asked every pass (fails there only by the new function)."""
        self.assertFalse(self._resting(1, 0.5))

    def test_then_daily(self):
        self.assertTrue(self._resting(3, 1))
        self.assertTrue(self._resting(3, 23))

    def test_its_day_comes_round(self):
        self.assertFalse(self._resting(3, 25))

    def test_a_record_from_before_is_asked(self):
        self.assertFalse(self._resting(10, None))

    def test_an_ask_records_when(self):
        a = {}
        engine._bill_ask_unanswered(a, "s")
        first = a["s"]["first"]
        a["s"]["last"] = "2000-01-01T00:00:00"
        engine._bill_ask_unanswered(a, "s")
        self.assertEqual(a["s"]["first"], first)              # first kept
        self.assertGreater(a["s"]["last"], "2026-01-01")


class ThePasses(_Harness):

    def _seed(self, first_days, last_hours):
        self.st.ask_record_save("bill",
            {self.slot: {"first": _ago(first_days), "last": _hours_ago(last_hours)}})

    def test_the_live_pass_rests_it(self):
        self._seed(3, 2)
        self.assertNotIn(self.slot, self._live().asked)

    def test_the_drain_rests_it(self):
        self._seed(3, 2)
        self.assertNotIn(self.slot, self._drain().asked)

    def test_and_asks_it_when_its_day_comes(self):
        """GUARD (passes on the parent, which asks every pass)."""
        self._seed(3, 25)
        self.assertIn(self.slot, self._drain().asked)

    def test_a_resting_half_hour_is_not_backlog(self):
        self._seed(3, 2)
        self.assertEqual(engine._measured_history_backlog(), 0)

    def test_the_drain_records_the_ask(self):
        self._drain()
        self.assertIn("last", self._record()[self.slot])


class TheTealAsk(unittest.TestCase):

    def test_the_breakdown_ask_rests_too(self):
        src = open(engine.__file__, encoding="utf-8").read()
        self.assertEqual(src.count("_gone = _bill_ask_given_up(_ask) | _bill_ask_resting(_ask)"), 3)


if __name__ == "__main__":
    unittest.main()
