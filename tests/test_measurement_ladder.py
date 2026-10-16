"""
test_measurement_ladder.py — 4.5.7.

recover_measurement_costs reads the OFF_PEAK label, which Octopus only attaches once the
query window extends ~2h PAST the slot (forward-anchored; proven by window-sweep on
2026-07-21/-23 & 08-14). The recovery ladder must therefore widen the FORWARD edge, not
the look-back. These simulate that API behaviour and assert the ladder reaches it.
"""
import os
import sys
import asyncio
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kraken_api_client import KrakenAPIClient

SLOT = "2026-07-21T15:00:00"
SLOT_DT = datetime.fromisoformat(SLOT)


class _FwdClient(KrakenAPIClient):
    """Simulates the forward-anchored label: OFF_PEAK only when the window END reaches
    >= slot + `off_at` hours. Records the max look-ahead the ladder actually tried."""
    def __init__(self, off_at_hours):
        self.account_number = "A-TEST"
        self._off_at = off_at_hours
        self.max_lookahead = 0.0

    async def get_measurements(self, mpan, start, end, **kw):
        e = datetime.fromisoformat(end.replace("Z", ""))
        la = (e - SLOT_DT).total_seconds() / 3600.0
        self.max_lookahead = max(self.max_lookahead, la)
        off = la >= self._off_at - 1e-9
        return [{"start": SLOT, "cost_incl": 0.0111, "cost_excl": 0.0105,
                 "kwh": 0.203, "off_peak": off, "standing_incl": None,
                 "standing_excl": None, "buckets": ["OFF_PEAK" if off else "STANDARD"]}]

    async def close(self):
        pass


class TestForwardLadder(unittest.TestCase):
    def _run(self, off_at):
        c = _FwdClient(off_at)
        rec = asyncio.run(
            c.recover_measurement_costs("M", [SLOT], account_number="A-TEST", pace_s=0))
        return c, rec

    def test_flips_at_2h_forward(self):
        # label appears at +2h; the ladder's first rung (2h) must catch it.
        c, rec = self._run(off_at=2.0)
        self.assertIn(SLOT, rec)
        self.assertTrue(rec[SLOT]["off_peak"], "should recover OFF_PEAK via the 2h forward rung")
        self.assertGreaterEqual(c.max_lookahead, 2.0)

    def test_widens_forward_when_first_rung_standard(self):
        # label only at +3h → ladder must widen to the 3h rung and still get OFF_PEAK.
        c, rec = self._run(off_at=3.0)
        self.assertTrue(rec[SLOT]["off_peak"])
        self.assertGreaterEqual(c.max_lookahead, 3.0)

    def test_never_offpeak_keeps_standard_fallback(self):
        # a genuinely-peak slot (never flips) is returned as STANDARD, not dropped.
        c, rec = self._run(off_at=99.0)
        self.assertIn(SLOT, rec)
        self.assertFalse(rec[SLOT]["off_peak"])


class _StripClient(KrakenAPIClient):
    """The Pi, 15 Oct 2026 (.build/probe_measurement_costs.py): tiny half-hours came back with kWh
    and NO cost in the ladder's windows, costed in the day for some, and a whole-month query costed
    all of August. Windows shorter than `day_from_h` strip; `day_ok` slots cost from a day window;
    every slot costs from a window of 20 days or more. Records each window asked."""
    def __init__(self, day_ok, day_from_h=12.0):
        self.account_number = "A-TEST"
        self.day_ok, self.day_from_h, self.windows = set(day_ok), day_from_h, []

    async def get_measurements(self, mpan, start, end, **kw):
        ws = datetime.fromisoformat(start.replace("Z", ""))
        we = datetime.fromisoformat(end.replace("Z", ""))
        span_h = (we - ws).total_seconds() / 3600.0
        self.windows.append(span_h)
        out = []
        for s in self.slots:
            t = datetime.fromisoformat(s)
            if not (ws <= t < we):
                continue
            costed = span_h >= 20 * 24 or (span_h >= self.day_from_h and s in self.day_ok)
            out.append({"start": s, "kwh": 0.001, "off_peak": False, "standing_incl": None,
                        "standing_excl": None, "buckets": ["STANDARD_RATE"],
                        "cost_incl": 0.000323 if costed else None,
                        "cost_excl": 0.000308 if costed else None})
        return out

    async def close(self):
        pass


class TestWideRungs(unittest.TestCase):
    """Bill and split paths design §1.3, consolidation PR 3. Discriminating, except the guard."""

    SLOTS = ["2026-08-19T05:30:00", "2026-08-20T06:00:00", "2026-08-22T05:30:00",
             "2026-08-22T20:00:00", "2026-08-25T05:00:00"]

    def _run(self, day_ok=(), wide=True):
        c = _StripClient(day_ok)
        c.slots = self.SLOTS
        rec = asyncio.run(c.recover_measurement_costs(
            "M", self.SLOTS, account_number="A-TEST", pace_s=0, wide_rungs=wide))
        return c, rec

    def test_the_day_then_the_month_recover_what_the_ladder_cannot(self):
        c, rec = self._run(day_ok={"2026-08-19T05:30:00", "2026-08-25T05:00:00"})
        self.assertEqual(sorted(rec), sorted(self.SLOTS))
        self.assertAlmostEqual(rec["2026-08-22T20:00:00"]["cost_incl"], 0.000323)

    def test_one_query_per_day_then_one_per_month(self):
        c, rec = self._run(day_ok=set(self.SLOTS))           # every day window answers
        day_windows = [w for w in c.windows if 24 <= w < 20 * 24]
        self.assertEqual(len(day_windows), 4)                 # 19, 20, 22, 25 Aug
        self.assertFalse([w for w in c.windows if w >= 20 * 24], "no month query needed")

    def test_a_wide_window_reaches_three_hours_past_its_half_hours(self):
        c, rec = self._run()
        self.assertTrue(all(w >= 24 + 3 for w in c.windows if w >= 12), c.windows)

    def test_without_the_wide_rungs_they_stay_missing(self):
        """GUARD: the ladder alone, as before."""
        c, rec = self._run(wide=False)
        self.assertEqual(rec, {})


if __name__ == "__main__":
    unittest.main()
