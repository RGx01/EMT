"""
The ask records are saved as changes, not whole copies (`_save_changes`, 17 Oct 2026).

Engine step 3c (24 Oct 2026): the race these tests reproduced — the live pass and the drain, two
tasks, each saving a whole copy over the other's — is gone with the live pass; the one asker runs
behind a running flag. TheMerge keeps the helper's own tests: the asker still saves through it, so
a re-fetch or a hold-back marked in between is not overwritten. The original account follows.

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
