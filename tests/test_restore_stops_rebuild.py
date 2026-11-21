"""
A restore stops a device history rebuild first (interrupted rebuild design §3, Phase 2; 21 Nov 2026).

T3 on the owner's soak Pi: *Replace history* was writing when a backup was restored. The rebuild waits
only for an import or a delete, so it kept writing the old database through the restore's checks and
safety backup, then failed — "BlockStore not initialised" — because it asked for the store between the
restore closing it and reopening it. That was timing: with a file copy quicker than the rebuild's pause
between half-hours, it would have woken after the reopen and written the old run into the restored
database. The card then showed the dead run's error as if the restored data were broken.

Now: the restore cancels a rebuild and waits for it, as for an import (one that will not stop aborts
the restore); the engine counts database replacements, and a rebuild, a replace or a queued refill
rebuild started on the old database stops when it moves; a replace checks for cancel between its
re-split chunks; finished runs' state is forgotten on a restore.

The figures are made up. DISCRIMINATING: every test fails on the parent (no `database_replaced`, no
step 1c) but the GUARD, which pins that an ordinary rebuild still runs to the end.
"""
import asyncio
import inspect
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import engine                                       # noqa: E402
import job_coordinator as C                         # noqa: E402
from block_store import BlockStore                  # noqa: E402
from tests.test_device_sensors_storage import _cfg, PERIOD   # noqa: E402
from tests.test_replace_device_history import FakeHA, RIGHT, HALF, H1, H2, _house, _rows  # noqa: E402

SERVER = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()


def _no_history():
    """The house, the device on the right sensor, its two recorder hours kept, no device rows yet —
    what a replace leaves after its drop."""
    st = BlockStore(":memory:")
    st.insert_config_period(_cfg(ev_read=RIGHT), effective_from=PERIOD)
    for s in HALF:
        _house(st, s)
    st.record_recorder_hours("ev_charger", "import", {H1: 1.0, H2: 1.0}, {H1: RIGHT, H2: RIGHT})
    return st


class _Base(unittest.TestCase):

    def setUp(self):
        self.st = _no_history()
        self.p = [mock.patch.object(engine, "get_store", lambda: self.st),
                  mock.patch.object(engine, "_store", self.st),
                  mock.patch.object(engine, "_engine_ha", FakeHA())]
        for x in self.p:
            x.start()
        C.reset_for_tests()
        engine._attribution_jobs.clear()

    def tearDown(self):
        for x in self.p:
            x.stop()
        C.reset_for_tests()
        engine._attribution_jobs.clear()


class TheGeneration(_Base):

    def test_it_cancels_a_run_and_forgets_finished_ones(self):
        engine._attribution_jobs.update({
            "ev_charger": {"device": "ev_charger", "status": "running", "control": "run"},
            "heat_pump": {"device": "heat_pump", "status": "error", "error": "Something went wrong"}})
        before = engine._db_generation
        engine.database_replaced()
        self.assertEqual(engine._db_generation, before + 1)
        self.assertEqual(engine._attribution_jobs["ev_charger"]["control"], "cancel")
        self.assertNotIn("heat_pump", engine._attribution_jobs, "a dead run's error outlived the restore")


class ARebuild(_Base):

    def _run(self, on_first=None):
        real, calls = engine._attribute_hour, []

        def hour(*a, **kw):
            out = real(*a, **kw)
            calls.append(a[0])
            if len(calls) == 1 and on_first:
                on_first()
            return out
        self.assertTrue(engine.attribution_claim("ev_charger", [RIGHT]))
        with mock.patch.object(engine, "_attribute_hour", hour):
            return asyncio.run(engine.run_attribution_job("ev_charger", [RIGHT], fetch=False))

    def test_it_stops_when_the_database_is_replaced(self):
        res = self._run(on_first=engine.database_replaced)
        self.assertEqual(sorted(_rows(self.st)), ["2026-10-01T10:00:00", "2026-10-01T10:30:00"],
                         "it went on writing after the database was replaced")
        self.assertEqual(res["status"], "cancelled")
        self.assertEqual(self.st.get_attribution_runs(), [], "the old run entered in the new ledger")

    def test_GUARD_an_ordinary_rebuild_runs_to_the_end(self):
        res = self._run()
        self.assertEqual(res["status"], "done")
        self.assertEqual(len(_rows(self.st)), 4)


class AReplace(_Base):

    def test_a_cancel_during_the_resplit_stops_it_before_the_rebuild(self):
        """What a restore's cancel does to a replace in "Giving the house back its share": it stops
        between chunks, rebuilds nothing, and frees the device. Rebuild history finishes it."""
        with self.st._conn:
            for s in HALF:
                self.st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, "
                                      "config_period_id, imp_kwh, source) VALUES (?, ?, 'ev_charger', "
                                      "1, 0.5, 'recorder_attributed')", (s, HALF[s]))
        real = self.st.drop_device_history

        def drop(*a, **kw):
            out = real(*a, **kw)
            engine.attribution_control("cancel", "ev_charger")
            return out
        self.assertTrue(engine.attribution_claim("ev_charger", [RIGHT]))
        with mock.patch.object(self.st, "drop_device_history", drop):
            res = asyncio.run(engine.replace_device_history("ev_charger", [RIGHT]))
        self.assertEqual(res.get("error"), "cancelled", res)
        self.assertEqual(_rows(self.st), {}, "the rebuild ran after the cancel")
        self.assertEqual(engine._attribution_jobs["ev_charger"]["status"], "cancelled")
        self.assertTrue(engine.attribution_claim("ev_charger", [RIGHT]), "the device is still held")


class AQueuedRefillRebuild(_Base):

    def test_it_is_dropped_when_the_database_is_replaced_while_it_waits(self):
        ran, tries = [], []

        def claim(mid, sensors):
            tries.append(mid)
            engine.database_replaced()           # a restore while it waited for admission
            return False

        async def run(*a, **kw):
            ran.append(a)
        with mock.patch.object(engine, "attribution_claim", claim), \
             mock.patch.object(engine, "run_attribution_job", run), \
             mock.patch.object(engine, "_REFILL_REBUILD_POLL_S", 0):
            asyncio.run(engine._rebuild_when_admitted("ev_charger", [RIGHT], H1, H2))
        self.assertEqual(tries, ["ev_charger"])
        self.assertEqual(ran, [], "the old database's refill rebuilt into the new one")


class TheRestore(unittest.TestCase):
    """The restore worker, read in order: the rebuild stops before the safety backup, and the
    engine is told once the file is swapped."""

    def setUp(self):
        i = SERVER.index("def _restore_worker(")
        self.body = SERVER[i:SERVER.index("\ndef ", i + 1)]

    def test_it_cancels_a_rebuild_before_the_safety_backup(self):
        stop = self.body.index('attribution_control("cancel")')
        self.assertLess(self.body.index("api_import_control(\"cancel\")"), stop)
        self.assertLess(stop, self.body.index('_create_backup_zip(label="pre_restore")'))
        self.assertIn("A device's history rebuild was still stopping — restore aborted.", self.body)

    def test_the_engine_is_told_after_the_swap(self):
        told = self.body.index("database_replaced()")
        self.assertLess(self.body.index("restored %s from %s"), told)
        self.assertLess(told, self.body.index("reset_store()"))


if __name__ == "__main__":
    unittest.main()
