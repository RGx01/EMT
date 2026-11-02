"""
recover_measurement_costs asks a LOCAL DAY at a time, then a month (22 Oct 2026; until then
test_measurement_ladder.py, 4.5.7).

The reader asked each half-hour a narrow window — 30 min before to 2 h, then 3 h, after — on the
belief that Octopus labels a smart charge outside the night window off-peak only once the window
reaches ~2 h past it. .build/probe_window_shapes.py, against the user's March 2025 statement:
6 Mar 2025 19:30, the middle of a 4-hour evening charge, came back STANDARD_RATE (£0.9223) both
rungs; asked as its local day (page sizes 500, 48, 10) or 60 days, all 48 half-hours matched the
statement (19:30 off-peak, £0.2542), none without a cost. The simulator below is that: OFF_PEAK
only when the window holds the whole charge, from its start to 2 h past the half-hour.

The month step is kept for a half-hour the day leaves with kWh and no cost (the Pi, 15 Oct 2026:
half-hours of a few Wh stripped in narrow windows, costed by the day or the month).

Discriminating: every test here fails on the parent but the GUARDs.
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kraken_api_client import KrakenAPIClient, KrakenAPIError

RUN = ("2025-03-06T17:30:00", "2025-03-06T21:30:00")       # the evening's charge, UTC (GMT)
MID = "2025-03-06T19:30:00"


def _dt(s):
    return datetime.fromisoformat(s.replace("Z", ""))


class _Octopus(KrakenAPIClient):
    """Answers each half-hour of the charge OFF_PEAK only when the window holds the charge from its
    start to 2 h past that half-hour; otherwise STANDARD_RATE at the day rate. `strip` half-hours
    come back with no cost in any window shorter than 20 days; `fail` windows raise."""

    def __init__(self, slots, strip=(), fail=0):
        self.account_number = "A-TEST"
        self.slots, self.strip, self.fail, self.windows = list(slots), set(strip), fail, []

    async def get_measurements(self, mpan, start, end, **kw):
        ws, we = _dt(start), _dt(end)
        self.windows.append((start, end))
        if self.fail:
            self.fail -= 1
            raise KrakenAPIError("boom")
        out = []
        for s in self.slots:
            t = _dt(s)
            if not (ws <= t < we):
                continue
            in_run = _dt(RUN[0]) <= t < _dt(RUN[1])
            off = (not in_run) or (ws <= _dt(RUN[0]) and we >= t + timedelta(hours=2))
            stripped = s in self.strip and (we - ws) < timedelta(days=20)
            out.append({"start": s, "kwh": 3.6, "off_peak": off, "standing_incl": None,
                        "standing_excl": None, "buckets": ["OFF_PEAK" if off else "STANDARD_RATE"],
                        "cost_incl": None if stripped else (0.2542 if off else 0.9223),
                        "cost_excl": None})
        return out

    async def close(self):
        pass


def _run(c, slots, **kw):
    return asyncio.run(c.recover_measurement_costs("M", slots, account_number="A-TEST",
                                                   pace_s=0, **kw))


class TheDay(unittest.TestCase):

    def test_the_middle_of_a_long_charge_is_off_peak(self):
        c = _Octopus([MID])
        rec = _run(c, [MID])
        self.assertTrue(rec[MID]["off_peak"])
        self.assertAlmostEqual(rec[MID]["cost_incl"], 0.2542)

    def test_one_query_for_the_day(self):
        slots = ["2025-03-06T%02d:%02d:00" % (h, m) for h in range(17, 22) for m in (0, 30)]
        c = _Octopus(slots)
        rec = _run(c, slots)
        self.assertEqual(sorted(rec), sorted(slots))
        self.assertEqual(len(c.windows), 1)

    def test_the_window_is_the_local_day_padded(self):
        """[local midnight − 30 min, next local midnight + 3 h), in UTC: 6 Mar is GMT."""
        c = _Octopus([MID])
        _run(c, [MID])
        self.assertEqual(c.windows[0], ("2025-03-05T23:30:00Z", "2025-03-07T03:00:00Z"))

    def test_a_summer_day_is_bst(self):
        s = "2026-07-14T10:00:00"
        c = _Octopus([s])
        _run(c, [s])
        self.assertEqual(c.windows[0], ("2026-07-13T22:30:00Z", "2026-07-15T02:00:00Z"))

    def test_a_standard_answer_from_the_day_stands(self):
        """GUARD: a genuine day-rate half-hour, outside any charge, keeps its STANDARD cost."""
        s = "2025-03-06T12:00:00"
        c = _Octopus([s])
        c.get_measurements = _standard(c.get_measurements)
        rec = _run(c, [s])
        self.assertFalse(rec[s]["off_peak"])


def _standard(inner):
    async def f(mpan, start, end, **kw):
        rows = await inner(mpan, start, end, **kw)
        for r in rows:
            r.update(off_peak=False, cost_incl=0.9223, buckets=["STANDARD_RATE"])
        return rows
    return f


class TheMonth(unittest.TestCase):

    def test_a_half_hour_the_day_strips_is_asked_the_month(self):
        c = _Octopus([MID], strip={MID})
        rec = _run(c, [MID])
        self.assertIn(MID, rec)
        self.assertEqual(len(c.windows), 2)
        # March's local month: 1 Mar 00:00 GMT − 30 min .. 1 Apr 00:00 BST (31 Mar 23:00 UTC) + 3 h
        self.assertEqual(c.windows[1], ("2025-02-28T23:30:00Z", "2025-04-01T02:00:00Z"))

    def test_nothing_anywhere_is_omitted(self):
        """GUARD: no cost is invented — the caller keeps its fallback."""
        c = _Octopus([MID])
        async def empty(mpan, start, end, **kw):
            c.windows.append((start, end)); return []
        c.get_measurements = empty
        self.assertEqual(_run(c, [MID]), {})


class TheRetry(unittest.TestCase):

    def test_a_failed_day_is_asked_again(self):
        """GUARD: the ladder retried too."""
        c = _Octopus([MID], fail=1)
        rec = _run(c, [MID])
        self.assertIn(MID, rec)
        self.assertEqual(len(c.windows), 2)              # the day, and the day again

    def test_a_day_that_answered_is_not(self):
        """A stripped half-hour on a day that answered goes to the month, not the day again."""
        c = _Octopus([MID], strip={MID})
        _run(c, [MID], max_attempts=3)
        self.assertEqual(len(c.windows), 2)


if __name__ == "__main__":
    unittest.main()
