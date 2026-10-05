"""
The dispatch poll writes only what changed, in one transaction (5 Oct 2026).

Octopus re-serves recent COMPLETED dispatches on every 5-minute poll. Each slot's
`dispatch_slots` upsert and `dispatch_history` row was its own transaction — about ten
commits a poll, each an fsync on the Pi's SD card — so the engine loop stalled for
seconds at :04/:34 (soak 13: up to 16 s). Every one of them rewrote identical data; only
`captured_at` / `last_seen` moved, and nothing reads those for completed slots.

Now an unchanged row is not written (its timestamp is when its data last changed),
PLANNED history still refreshes last_seen every poll (the smart-charging card uses it to
tell the current plan from a superseded one), and a poll's writes share one transaction.
The same for the OHME path.

Tests marked GUARD hold on the parent tree too. The rest fail there.
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import engine                                     # noqa: E402
from block_store import BlockStore                # noqa: E402

S = "2026-10-04T10:00:00"


class _Store(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.st = BlockStore(os.path.join(self.dir, "b.db"))

    def tearDown(self):
        self.st.close()

    def changes(self, fn):
        before = self.st._conn.total_changes
        fn()
        return self.st._conn.total_changes - before

    def slot(self):
        return self.st._conn.execute(
            "SELECT captured_at, energy_completed FROM dispatch_slots WHERE slot_start=?",
            (S,)).fetchone()

    def hist(self, kind):
        return self.st._conn.execute(
            "SELECT first_seen, last_seen, energy_kwh FROM dispatch_history "
            "WHERE slot_start=? AND kind=?", (S, kind)).fetchone()


class TheSlotUpsert(_Store):

    def _up(self, e, at):
        self.st.upsert_dispatch_slot(S, provider="MYENERGI", source="smart-charge",
                                     state="completed", energy_completed=e, captured_at=at)

    def test_an_unchanged_slot_is_not_written(self):
        self._up(-3.1, "2026-10-04T10:05:00")
        self.assertEqual(self.changes(lambda: self._up(-3.1, "2026-10-04T10:10:00")), 0)
        self.assertEqual(self.slot()["captured_at"], "2026-10-04T10:05:00",
                         "captured_at is when the data last changed")

    def test_a_changed_slot_is_written(self):
        """GUARD: the parent wrote every time."""
        self._up(-3.1, "2026-10-04T10:05:00")
        self._up(-3.4, "2026-10-04T10:10:00")
        self.assertEqual(tuple(self.slot()), ("2026-10-04T10:10:00", -3.4))

    def test_a_planned_recapture_still_keeps_the_completed_figure(self):
        """GUARD: the COALESCE rule — a re-seen plan does not wipe a settled figure."""
        self._up(-3.1, "2026-10-04T10:05:00")
        self.st.upsert_dispatch_slot(S, provider="MYENERGI", source="smart-charge",
                                     state="completed")
        self.assertEqual(self.slot()["energy_completed"], -3.1)


class TheHistoryRow(_Store):

    def _rec(self, kind, e, at, **kw):
        self.st.record_dispatch_history(S, kind, provider="MYENERGI", source="unknown",
                                        energy_kwh=e, seen_at=at, **kw)

    def test_an_unchanged_completed_row_is_not_written(self):
        self._rec("completed", -3.1, "2026-10-04T10:05:00", refresh_seen=False)
        n = self.changes(lambda: self._rec("completed", -3.1, "2026-10-04T10:10:00",
                                           refresh_seen=False))
        self.assertEqual(n, 0)
        self.assertEqual(self.hist("completed")["last_seen"], "2026-10-04T10:05:00")

    def test_a_changed_completed_row_is_written(self):
        """GUARD in behaviour (the parent always wrote); it errors there only
        because refresh_seen is new."""
        self._rec("completed", -3.1, "2026-10-04T10:05:00", refresh_seen=False)
        self._rec("completed", -3.4, "2026-10-04T10:10:00", refresh_seen=False)
        r = self.hist("completed")
        self.assertEqual((r["first_seen"], r["last_seen"], r["energy_kwh"]),
                         ("2026-10-04T10:05:00", "2026-10-04T10:10:00", -3.4))

    def test_a_planned_row_still_refreshes_last_seen_every_poll(self):
        """GUARD: the default. The upcoming-plan filter depends on it."""
        self._rec("planned", -2.0, "2026-10-04T10:05:00")
        self._rec("planned", -2.0, "2026-10-04T10:10:00")
        self.assertEqual(self.hist("planned")["last_seen"], "2026-10-04T10:10:00")


class TheBatch(_Store):

    def test_its_writes_are_one_transaction(self):
        other = sqlite3.connect(os.path.join(self.dir, "b.db"))
        with self.st.dispatch_batch():
            self.st.upsert_dispatch_slot(S, state="planned", energy_planned=-1.0)
            self.st.record_dispatch_history(S, "planned", energy_kwh=-1.0)
            self.assertTrue(self.st._conn.in_transaction, "a write committed mid-batch")
            seen = other.execute("SELECT COUNT(*) FROM dispatch_slots").fetchone()[0]
            self.assertEqual(seen, 0, "another connection saw an uncommitted batch")
        self.assertFalse(self.st._conn.in_transaction)
        self.assertEqual(other.execute("SELECT COUNT(*) FROM dispatch_slots").fetchone()[0], 1)
        other.close()

    def test_a_nested_batch_joins_the_outer_one(self):
        with self.st.dispatch_batch():
            with self.st.dispatch_batch():
                self.st.upsert_dispatch_slot(S, state="planned")
            self.assertTrue(self.st._conn.in_transaction)


class _FakeKraken:
    def __init__(self, disp):
        self.disp = disp

    async def get_dispatches(self, account):
        return self.disp

    async def get_intelligent_state(self, account):
        return None


COMPLETED = [{"start": "2026-10-04T08:00:00Z", "end": "2026-10-04T10:30:00Z",
              "delta": -5.0, "source": "unknown"}]


class ARealPoll(_Store):
    """_capture_dispatch_slots, twice, with Octopus returning the same completed
    dispatches — as it does on every 5-minute poll."""

    def _poll(self, provider):
        disp = {"provider": provider, "planned": [], "completed": COMPLETED}
        with mock.patch.object(engine, "_store", self.st), \
             mock.patch.object(engine, "_kraken_client", _FakeKraken(disp)), \
             mock.patch.object(engine, "_kraken_discovery", {"ok": True}), \
             mock.patch.object(engine, "_kraken_account_number", "A-1"), \
             mock.patch.object(engine, "_detected_integrations", {}):
            asyncio.run(engine._capture_dispatch_slots())

    def test_a_repeat_poll_writes_nothing(self):
        self._poll("MYENERGI")
        first = self.st._conn.execute("SELECT COUNT(*) FROM dispatch_history "
                                      "WHERE kind='completed'").fetchone()[0]
        self.assertGreater(first, 0, "the fixture recorded nothing — the test would be vacuous")
        self.assertEqual(self.changes(lambda: self._poll("MYENERGI")), 0)

    def test_a_repeat_ohme_poll_writes_nothing(self):
        self._poll("OHME")
        first = self.st._conn.execute("SELECT COUNT(*) FROM dispatch_history "
                                      "WHERE kind='completed'").fetchone()[0]
        self.assertGreater(first, 0)
        self.assertEqual(self.changes(lambda: self._poll("OHME")), 0)


if __name__ == "__main__":
    unittest.main()
