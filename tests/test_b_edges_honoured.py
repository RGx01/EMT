"""
B §5 acceptance 3 (4 Oct 2026) — every declared edge is honoured, one test per edge.

"For each `depends_on` edge, a test that starting the dependant while the dependency
runs defers or waits — never proceeds." Each test below takes the REAL coordinator,
makes the blocker running, calls the member's own entry point, and observes that it
did not get past its admission gate. The blocker's work is not simulated; the
manifest's edge is what is under test, through the member's own code.

    attribution       depends import · exclusive delete                   (WAIT)
    settle_sweep      depends import · exclusive delete, restore          (DEFER)
    carbon_backfill   depends import · exclusive delete, restore          (DEFER)
    measured_drain    depends import · exclusive delete, restore          (DEFER)
    prune_tombstones  exclusive delete                                    (DEFER)

delete → import (PREEMPT) is test_delete_honours_import; restore → import is
test_restore_window. (verify_pricing → import went with the verify pass, engine step 2d.)

GUARDS, all of them: the edges were already honoured, so these pass on the parent
tree. They pin it. Each was checked to FAIL with its member's admission() call
removed (4 Oct 2026), so a member that stops consulting is caught.
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import engine                                     # noqa: E402
import job_coordinator as C                       # noqa: E402

EDGES = {
    "settle_sweep":     ("import", "delete", "restore"),
    "carbon_backfill":  ("import", "delete", "restore"),
    "measured_drain":   ("import", "delete", "restore"),
    "prune_tombstones": ("delete",),
    "attribution":      ("import", "delete"),
}


def _running(name):
    C.claim(name, force=True)
    C.mark(name, status="running")


class _Recorder:
    """A store that records which meta keys and methods were reached."""

    def __init__(self):
        self.reached = []

    def get_meta(self, key, default=None):
        self.reached.append(("get_meta", key))
        return default

    def __getattr__(self, name):
        def _call(*a, **k):
            self.reached.append((name,))
            return None
        return _call


class TheManifestHasTheseEdges(unittest.TestCase):

    def test_the_table_above_is_the_manifest(self):
        """The edges tested here are the manifest's, so a new edge without a test
        fails this rather than going quietly untested."""
        for member, blockers in EDGES.items():
            deps, excl, _ = C._MANIFEST[member]
            self.assertEqual(sorted(deps + excl), sorted(blockers), member)


class _Gate(unittest.TestCase):
    MEMBER = None

    def setUp(self):
        C.reset_for_tests()
        self.st = _Recorder()
        self.p = [mock.patch.object(engine, "_store", self.st),
                  mock.patch.object(engine, "kraken_available", lambda: True)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        C.reset_for_tests()

    def passed_the_gate(self):
        raise NotImplementedError

    def run_entry(self):
        raise NotImplementedError

    def _check_edges(self):
        if self.MEMBER is None:
            return
        for blocker in EDGES[self.MEMBER]:
            with self.subTest(blocker=blocker):
                C.reset_for_tests()
                self.st.reached.clear()
                _running(blocker)
                self.run_entry()
                self.assertFalse(self.passed_the_gate(),
                                 "%s proceeded while %s ran" % (self.MEMBER, blocker))
        C.reset_for_tests()
        self.st.reached.clear()
        self.run_entry()
        self.assertTrue(self.passed_the_gate(), "with nothing running it never got past the gate"
                        " — the test is not observing the gate")


class TheSettleSweep(_Gate):
    """The settle sweep's scheduler (the history reprice sweep's, until engine step 4e)."""
    MEMBER = "settle_sweep"

    def run_entry(self):
        with mock.patch.object(engine, "_settle_history_scheduled", False):
            engine._maybe_settle_history()

    def passed_the_gate(self):
        return ("get_meta", engine._SETTLE_HISTORY_MARKER) in self.st.reached

    def test_each_edge(self):
        self._check_edges()


class TheCarbonBackfill(_Gate):
    MEMBER = "carbon_backfill"

    def run_entry(self):
        with mock.patch.object(engine, "_get_postcode", lambda: "DE1"), \
             mock.patch.object(engine, "_carbon_backfill_running", False):
            engine._maybe_backfill_historical_carbon()

    def passed_the_gate(self):
        return ("get_meta", engine._CARBON_BACKFILL_MARKER) in self.st.reached

    def test_each_edge(self):
        self._check_edges()


class TheMeasuredDrain(_Gate):
    MEMBER = "measured_drain"

    def setUp(self):
        super().setUp()
        self.backlog = []
        # the drain's own "applicable" gate: any Intelligent agreement (18 Oct 2026; it was
        # "any IOG-SMB agreement", reached through _import_is_smb_capped)
        self.p += [mock.patch.object(engine, "_any_intelligent", lambda: True),
                   mock.patch.object(engine, "_measured_history_backlog",
                                     lambda: self.backlog.append(1) or 0)]
        for x in self.p[-2:]:
            x.start()

    def run_entry(self):
        self.backlog.clear()
        with mock.patch.object(engine, "_measured_history_drain_running", False):
            engine._maybe_drain_measured_history()

    def passed_the_gate(self):
        return bool(self.backlog)

    def test_each_edge(self):
        self._check_edges()


class ThePrune(_Gate):
    MEMBER = "prune_tombstones"

    def run_entry(self):
        self.result = engine.run_prune_stale_tombstones()

    def passed_the_gate(self):
        return ("prune_stale_deleted_ranges",) in self.st.reached

    def test_each_edge(self):
        self._check_edges()


class _FakeHA:
    async def get_statistics(self, ids, start, end, period="hour", timeout=0):
        return {i: [{"start": "2026-03-01T10:00:00+00:00", "sum": 1.0}] for i in ids}


class TheAttribution(unittest.TestCase):
    """WAIT: it pauses in place, phase "waiting for <blocker>", and writes nothing."""

    def setUp(self):
        C.reset_for_tests()
        engine._attribution_jobs.pop("heat_pump", None)

    def tearDown(self):
        engine._attribution_jobs.pop("heat_pump", None)
        C.reset_for_tests()

    def _job_under(self, blocker):
        store = mock.MagicMock()
        store.get_parent_meter_id.return_value = "electricity_main"
        store.get_device_live_coverage_start.return_value = None
        store.ha_reads_for.return_value = ({}, {})
        seen = {}

        async def _drive():
            with mock.patch.object(engine, "_engine_ha", _FakeHA()), \
                 mock.patch.object(engine, "get_store", lambda: store), \
                 mock.patch.object(engine, "_stitch_hourly_energy",
                                   lambda series, ids, **kw: {"2026-03-01T10:00:00": 1.0}):
                if blocker:
                    _running(blocker)
                C.claim("attribution", "heat_pump", force=True)
                t = asyncio.ensure_future(engine.run_attribution_job("heat_pump", ["sensor.hp"]))
                await asyncio.sleep(1.3)                   # one turn of its 1 s wait loop
                j = engine._attribution_jobs["heat_pump"]
                seen.update(status=j.get("status"), phase=j.get("phase"),
                            written=j.get("written"))
                j["control"] = "cancel"
                await asyncio.wait_for(t, 5)
        asyncio.run(_drive())
        return seen

    def test_each_edge(self):
        for blocker in EDGES["attribution"]:
            with self.subTest(blocker=blocker):
                C.reset_for_tests()
                engine._attribution_jobs.pop("heat_pump", None)
                seen = self._job_under(blocker)
                self.assertEqual(seen["status"], "paused", seen)
                self.assertEqual(seen["phase"], "waiting for %s" % blocker)
                self.assertEqual(seen["written"], 0)

    def test_with_nothing_running_it_does_not_wait(self):
        seen = self._job_under(None)
        self.assertFalse(str(seen.get("phase") or "").startswith("waiting for"), seen)


if __name__ == "__main__":
    unittest.main()
