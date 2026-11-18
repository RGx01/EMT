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

16 Nov 2026: the day and the month are asked exactly, not padded. From 22 Oct each window ran 30 min
before to 3 h after, and Octopus answers such a window with no cost at all — the probe's --costs on
1 Jan and 1 Apr 2025: the padded day costed 0 of 34 half-hours, the exact day, the month, 60 days
and the ladder all 34. The simulator does the same: a window over 4 h that does not start and end
on a local midnight comes back without costs (TheWindow).

18 Nov 2026: what the month leaves is asked noon to noon. On 24 Apr 2026 every window STARTING at
the day's midnight came back without costs, whatever its end, and every one starting at 03:00 or
later, or at noon the day before, costed all it held (the probe, 16 windows). The simulator's
`poisoned` starts are that; the padded day's fault is now read the same way — a long window that
starts in the night, off the midnight, answers with no costs — so a noon window is costed (TheNoon).

Discriminating: every test here fails on the parent but the GUARDs.
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kraken_api_client import KrakenAPIClient, KrakenAPIError

RUN = ("2025-03-06T17:30:00", "2025-03-06T21:30:00")       # the evening's charge, UTC (GMT)
MID = "2025-03-06T19:30:00"


def _dt(s):
    return datetime.fromisoformat(s.replace("Z", ""))


def _local_midnight(t):
    loc = t.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Europe/London"))
    return (loc.hour, loc.minute, loc.second) == (0, 0, 0)


def _costless_window(ws, we):
    """Octopus, 16 Nov 2026: a long window starting in the night, off its midnight (the padded
    day's 23:30), answers with no costs. A noon start is not in the night (18 Nov)."""
    h = ws.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Europe/London")).hour
    return (we - ws) > timedelta(hours=4) and (h >= 23 or h < 6) and not _local_midnight(ws)


class _Octopus(KrakenAPIClient):
    """Answers each half-hour of the charge OFF_PEAK only when the window holds the charge from its
    start to 2 h past that half-hour; otherwise STANDARD_RATE at the day rate. `strip` half-hours
    come back with no cost in any window shorter than 20 days; `fail` windows raise."""

    def __init__(self, slots, strip=(), fail=0, poisoned=()):
        self.account_number = "A-TEST"
        self.slots, self.strip, self.fail, self.windows = list(slots), set(strip), fail, []
        self.poisoned = {_dt(p) for p in poisoned}    # window starts Octopus answers without costs

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
            stripped = ((s in self.strip and (we - ws) < timedelta(days=20))
                        or _costless_window(ws, we) or ws in self.poisoned)
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

    def test_the_window_is_the_local_day(self):
        """Local midnight to local midnight, in UTC: 6 Mar is GMT."""
        c = _Octopus([MID])
        _run(c, [MID])
        self.assertEqual(c.windows[0], ("2025-03-06T00:00:00Z", "2025-03-07T00:00:00Z"))

    def test_a_summer_day_is_bst(self):
        s = "2026-07-14T10:00:00"
        c = _Octopus([s])
        _run(c, [s])
        self.assertEqual(c.windows[0], ("2026-07-13T23:00:00Z", "2026-07-14T23:00:00Z"))

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


class TheWindow(unittest.TestCase):
    """16 Nov 2026, the support file's 1 Apr 2025: asked the padded day and month, 0 of 22 costed;
    the gap fill asked the exact day two minutes later and costed all 22."""

    def test_a_night_and_an_evening_charge_are_costed(self):
        slots = ["2025-04-01T02:00:00", "2025-04-01T19:00:00", "2025-04-01T22:30:00"]
        c = _Octopus(slots)
        rec = _run(c, slots)
        self.assertEqual(sorted(rec), slots)
        self.assertEqual(len(c.windows), 1)          # the day answered: the month not asked

    def test_a_winter_day_is_costed(self):
        s = "2025-01-01T03:00:00"
        rec = _run(_Octopus([s]), [s])
        self.assertIn(s, rec)


class TheMonth(unittest.TestCase):

    def test_a_half_hour_the_day_strips_is_asked_the_month(self):
        c = _Octopus([MID], strip={MID})
        rec = _run(c, [MID])
        self.assertIn(MID, rec)
        self.assertEqual(len(c.windows), 2)
        # March's local month: 1 Mar 00:00 GMT .. 1 Apr 00:00 BST (31 Mar 23:00 UTC)
        self.assertEqual(c.windows[1], ("2025-03-01T00:00:00Z", "2025-03-31T23:00:00Z"))

    def test_nothing_anywhere_is_omitted(self):
        """GUARD: no cost is invented — the caller keeps its fallback."""
        c = _Octopus([MID])
        async def empty(mpan, start, end, **kw):
            c.windows.append((start, end)); return []
        c.get_measurements = empty
        self.assertEqual(_run(c, [MID]), {})


# 24 Apr 2026 (BST): the day starts 23 Apr 23:00 UTC, April's month 31 Mar 23:00 UTC
APR24_DAY, APR_MONTH = "2026-04-23T23:00:00", "2026-03-31T23:00:00"


class TheNoon(unittest.TestCase):

    def test_a_morning_half_hour_is_asked_from_noon_the_day_before(self):
        s = "2026-04-24T02:00:00"                       # 03:00 BST, the night's charge
        c = _Octopus([s], poisoned={APR24_DAY, APR_MONTH})
        rec = _run(c, [s])
        self.assertIn(s, rec)
        self.assertEqual(c.windows, [("2026-04-23T23:00:00Z", "2026-04-24T23:00:00Z"),
                                     ("2026-03-31T23:00:00Z", "2026-04-30T23:00:00Z"),
                                     ("2026-04-23T11:00:00Z", "2026-04-24T11:00:00Z")])

    def test_an_afternoon_half_hour_is_asked_from_its_noon(self):
        s = "2026-04-24T17:30:00"                       # 18:30 BST
        c = _Octopus([s], poisoned={APR24_DAY, APR_MONTH})
        rec = _run(c, [s])
        self.assertIn(s, rec)
        self.assertEqual(c.windows[-1], ("2026-04-24T11:00:00Z", "2026-04-25T11:00:00Z"))

    def test_a_winter_noon_is_gmt(self):
        s = "2025-03-06T19:30:00"
        c = _Octopus([s], poisoned={"2025-03-06T00:00:00", "2025-03-01T00:00:00"})
        rec = _run(c, [s])
        self.assertIn(s, rec)
        self.assertEqual(c.windows[-1], ("2025-03-06T12:00:00Z", "2025-03-07T12:00:00Z"))

    def test_one_noon_window_for_a_nights_half_hours(self):
        slots = ["2026-04-23T22:30:00", "2026-04-24T02:00:00", "2026-04-24T03:00:00"]
        c = _Octopus(slots, poisoned={APR24_DAY, "2026-04-22T23:00:00", APR_MONTH})
        rec = _run(c, slots)
        self.assertEqual(sorted(rec), slots)
        self.assertEqual(c.windows[-1], ("2026-04-23T11:00:00Z", "2026-04-24T11:00:00Z"))
        self.assertEqual(sum(1 for w in c.windows if w[0].endswith("11:00:00Z")), 1)

    def test_a_day_that_costs_asks_no_noon(self):
        """GUARD: the day answered, so neither the month nor noon is asked."""
        s = "2026-04-24T02:00:00"
        c = _Octopus([s])
        _run(c, [s])
        self.assertEqual(len(c.windows), 1)


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
