"""
A half-hour whose cost EMT no longer asks Octopus for reads `abandoned`, red hatched yellow
(19 Oct 2026, the user).

- A live half-hour asked 30 days without a bill (`bill_ask`'s give-up) reads `abandoned` where it
  would otherwise wait (`overdue`); one with a bill, or corrected, does not.
- An API-imported half-hour Octopus gave no cost for at import (EMT's kWh x rate) reads `abandoned`
  at once — nothing asks for it again; except inside an IOG-SMB window, where the bill drain does
  (corrected 20 Oct 2026): there it waits for its bill as a live half-hour does. The import records it (`import_no_cost`, durable: the block
  cannot tell, and the reprice queue empties at the next import); an import or a repair that gets
  Octopus's cost takes it off. API-imported history from before the record reads `applied` as before.
- The heatmap draws it red with yellow hatching, counts it as an issue, and the detail panel says
  since when EMT has not asked.

Discriminating (fail on the parent), except the GUARDs marked.
"""
import asyncio
import os
import sys
import unittest
from unittest import mock
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import engine                                     # noqa: E402
import settlement_state as ss                     # noqa: E402
from block_store import BlockStore                # noqa: E402
from tests.test_consumption_retention_probe import (  # noqa: E402
    TestApiImportApply, _FakeMeasClient, _iv)

NOW = datetime(2026, 10, 19, 12, 0)
S = "2026-05-01T12:00:00"


def _f(**kw):
    f = {"block_start": S, "imp_kwh": 1.0, "imp_cost": 0.3, "imp_rate": 0.3, "imp_kwh_api": 1.0,
         "source": "kraken_api", "rate_source": "schedule"}
    f.update(kw)
    return f


def _bill(f, **ctx):
    return ss.classify(f, smb=False, now=NOW, **ctx)["bill"]


class TheState(unittest.TestCase):

    def test_a_live_half_hour_given_up_is_abandoned(self):
        self.assertEqual(_bill(_f(), given_up={S}), "abandoned")

    def test_still_being_asked_it_is_overdue(self):
        """GUARD: the parent's red."""
        self.assertEqual(_bill(_f()), "overdue")

    def test_a_billed_or_corrected_one_is_not(self):
        """GUARD of what does not move (fails on the parent only by the new argument)."""
        self.assertEqual(_bill(_f(rate_source="settled", bill_cost=0.3), given_up={S}), "applied")
        self.assertEqual(_bill(_f(rate_source="corrected", rate_corrected=1), given_up={S}), "corrected")

    def test_an_imported_half_hour_with_no_octopus_cost_is_abandoned(self):
        self.assertEqual(_bill(_f(source="imported_api", imp_kwh_api=None), import_no_cost={S}),
                         "abandoned")

    def test_imported_history_without_the_record_is_applied(self):
        """GUARD: as before — nothing tells it apart."""
        self.assertEqual(_bill(_f(source="imported_api", imp_kwh_api=None)), "applied")


class InsideAnIogSmbWindow(unittest.TestCase):
    """Staging, 20 Oct 2026: 21 Jul, deleted and refilled from Octopus, came back with 15 half-hours
    Octopus gave no cost for, hatched at once — but inside an IOG-SMB window the bill drain asks
    for imported history's bill, as it had for that day before the delete. Those wait for it."""

    def test_an_asked_import_waits_as_a_live_half_hour_does(self):
        self.assertEqual(_bill(_f(source="imported_api", imp_kwh_api=None),
                               import_asked={S}), "overdue")

    def test_until_it_is_given_up(self):
        self.assertEqual(_bill(_f(source="imported_api", imp_kwh_api=None),
                               import_asked={S}, given_up={S}), "abandoned")

    def test_a_bill_still_outranks_it(self):
        """GUARD: the bill comes first (fails on the parent only by the new argument)."""
        self.assertEqual(_bill(_f(source="imported_api", rate_source="settled", bill_cost=0.3),
                               import_asked={S}), "applied")

    def _split(self, windows, slots):
        st = mock.MagicMock()
        st.import_no_cost_slots.return_value = {k: "2026-10-20T12:00:00" for k in slots}
        with mock.patch.object(engine, "_intelligent_windows", lambda: windows), \
             mock.patch.object(engine, "_kraken_discovery", {}), \
             mock.patch.object(engine, "_ask_floor", lambda vf: vf):
            return engine.bill_no_cost(st)

    def test_the_drain_s_windows_decide(self):
        _, imported, asked = self._split([("2026-07-05T23:00:00", None, True)],
                                         ["2026-07-21T00:00:00", "2026-06-01T00:00:00"])
        self.assertEqual((imported, asked), ({"2026-06-01T00:00:00"}, {"2026-07-21T00:00:00"}))

    def test_an_intelligent_window_that_is_not_smb_does_not_ask(self):
        _, imported, asked = self._split([("2026-07-05T23:00:00", None, False)],
                                         ["2026-07-21T00:00:00"])
        self.assertEqual((imported, asked), ({"2026-07-21T00:00:00"}, set()))

    def test_the_panel_does_not_say_not_asked(self):
        st = mock.MagicMock()
        st.import_no_cost_slots.return_value = {"2026-07-21T00:00:00": "2026-10-20T12:00:00"}
        with mock.patch.object(engine, "_intelligent_windows",
                               lambda: [("2026-07-05T23:00:00", None, True)]), \
             mock.patch.object(engine, "_kraken_discovery", {}), \
             mock.patch.object(engine, "_ask_floor", lambda vf: vf):
            self.assertIsNone(engine.no_cost_since(st, "2026-07-21T00:00:00"))

    def test_the_heatmap_passes_it(self):
        srv = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
        self.assertIn("given_up, import_no_cost, import_asked = _eng.bill_no_cost(store)", srv)
        self.assertIn("import_no_cost=import_no_cost, import_asked=import_asked)", srv)


class TheRecord(unittest.TestCase):

    def test_noted_then_cleared(self):
        st = BlockStore(":memory:")
        st.note_import_cost(no_cost=["a", "b"])
        first = st.import_no_cost_slots()["a"]
        st.note_import_cost(no_cost=["a"], costed=["b"])
        self.assertEqual(st.import_no_cost_slots(), {"a": first})   # first noted kept

    def test_the_import_records_what_octopus_gave_no_cost_for(self):
        t = TestApiImportApply("test_writes_exact_dispatch_aware_blocks")
        t.setUp()
        try:
            ivs = [_iv("2024-07-01T00:00:00", 0.108, 0.007560378),
                   _iv("2024-07-01T00:30:00", 0.127, None),          # kWh, no cost
                   _iv("2024-07-01T01:00:00", 0.0, None)]            # nothing drawn
            engine._kraken_client = _FakeMeasClient(ivs)
            asyncio.run(engine.import_api_history())
            self.assertEqual(set(t.store.import_no_cost_slots()), {"2024-07-01T00:30:00"})
            # a later import that gets the cost takes it off
            ivs[1] = _iv("2024-07-01T00:30:00", 0.127, 0.00889)
            engine._kraken_client = _FakeMeasClient(ivs)
            asyncio.run(engine.fetch_api_window("2024-07-01T00:00:00", "2024-07-01T02:00:00",
                                                pace_s=0))
            self.assertEqual(t.store.import_no_cost_slots(), {})
        finally:
            t.tearDown()


class TheScreens(unittest.TestCase):
    _SRC = open(os.path.join(_ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8").read()

    def test_red_hatched_yellow_and_an_issue(self):
        self.assertIn("if (v === 'abandoned') return hatchAbandoned();", self._SRC)
        self.assertIn("x.fillStyle = '#cc0000'", self._SRC)
        self.assertIn("overdue: 1, abandoned: 1,", self._SRC)            # ISSUE
        self.assertIn("overdue: 3, abandoned: 3,", self._SRC)            # CAT_RANK

    def test_the_panel_says_since_when(self):
        self.assertIn("'not asked since ' + esc(localTime(d.no_cost_since))", self._SRC)
        srv = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
        self.assertIn('"no_cost_since": (_eng_nc.no_cost_since(store, d["block_start"])', srv)
        self.assertIn("given_up=given_up, import_no_cost=import_no_cost", srv)


if __name__ == "__main__":
    unittest.main()
