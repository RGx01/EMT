"""
Engine step 2c (23 Oct 2026): the API import keeps Octopus's answer as band evidence.

The import gets a label (OFF_PEAK / STANDARD_RATE) and a cost for every half-hour it asks Octopus
for, and kept only the cost. It now writes each costed Intelligent half-hour's label, cost and
kWh to the bill cache (`measured_cost`) in one transaction for the chunk, so imported history
settles through the same rule as live (kWh × the band's rate, `apply_measured_to_block`). Never
over an IOG-SMB breakdown already cached; a newer answer replaces an older one.

Discriminating: every test here fails on the parent (the write is new), but the GUARD.
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import engine                                     # noqa: E402
from block_store import BlockStore                # noqa: E402
from tests import test_consumption_retention_probe as _crp   # noqa: E402
_FakeMeasClient, _iv = _crp._FakeMeasClient, _crp._iv

S = "2024-07-01T00:00:00"


class TheStore(unittest.TestCase):

    def setUp(self):
        self.st = BlockStore(":memory:")

    def _row(self, s=S):
        return self.st._conn.execute(
            "SELECT cost_incl, label, kwh, home_kwh FROM measured_cost WHERE slot_start=?",
            (s,)).fetchone()

    def test_written_in_one_go(self):
        n = self.st.upsert_imported_bills([
            {"start": S, "cost_incl": 0.0756, "cost_excl": 0.072, "label": "OFF_PEAK", "kwh": 1.08},
            {"start": "2024-07-01T00:30:00", "cost_incl": 0.35, "label": "STANDARD_RATE", "kwh": 1.1}],
            mpan="IM")
        self.assertEqual(n, 2)
        self.assertEqual(tuple(self._row())[:3], (0.0756, "OFF_PEAK", 1.08))

    def test_a_newer_answer_replaces_an_older(self):
        self.st.upsert_measured_cost(S, mpan="IM", cost_incl=0.35, label="STANDARD_RATE", kwh=1.08)
        self.st.upsert_imported_bills([{"start": S, "cost_incl": 0.0756, "label": "OFF_PEAK",
                                        "kwh": 1.08}], mpan="IM")
        self.assertEqual(self._row()["label"], "OFF_PEAK")

    def test_never_over_an_smb_breakdown(self):
        self.st.upsert_measured_cost(S, mpan="IM", cost_incl=0.0756, label="OFF_PEAK", kwh=1.08)
        self.st.upsert_measured_breakdown(S, mpan="IM", home_kwh=1.08, home_rate=0.07,
                                          ev_kwh=0.0, ev_rate=None)
        self.st.upsert_imported_bills([{"start": S, "cost_incl": 0.35, "label": "STANDARD_RATE",
                                        "kwh": 1.08}], mpan="IM")
        r = self._row()
        self.assertEqual((r["cost_incl"], r["label"], r["home_kwh"]), (0.0756, "OFF_PEAK", 1.08))


class TheLabel(unittest.TestCase):

    def test_parse_bills_labels(self):
        self.assertEqual([engine._import_label(x) for x in (True, False, None)],
                         ["OFF_PEAK", "STANDARD_RATE", "mixed"])


class TheImport(unittest.TestCase):

    def _run(self, kind, ivs):
        t = _crp.TestApiImportApply("test_writes_exact_dispatch_aware_blocks")
        t.setUp()
        try:
            engine._kraken_client = _FakeMeasClient(ivs)
            with mock.patch.object(engine, "tariff_kind_at", lambda bs: kind):
                asyncio.run(engine.import_api_history())
            return {r["slot_start"]: dict(r) for r in t.store._conn.execute(
                "SELECT slot_start, mpan, cost_incl, label, kwh FROM measured_cost")}
        finally:
            t.tearDown()

    def test_an_intelligent_half_hour_keeps_its_band(self):
        got = self._run("intelligent", [
            _iv("2024-07-01T00:00:00", 0.108, 0.007560378),
            _iv("2024-07-01T00:30:00", 0.127, 0.03, off_peak=False),
            _iv("2024-07-01T01:00:00", 0.127, None),                 # no cost: nothing to keep
            _iv("2024-07-01T01:30:00", 0.0, 0.0)])                   # nothing drawn
        self.assertEqual(sorted(got), ["2024-07-01T00:00:00", "2024-07-01T00:30:00"])
        self.assertEqual((got["2024-07-01T00:00:00"]["label"], got["2024-07-01T00:00:00"]["mpan"]),
                         ("OFF_PEAK", "IM"))
        self.assertEqual(got["2024-07-01T00:30:00"]["label"], "STANDARD_RATE")

    def test_a_published_tariff_keeps_none(self):
        """GUARD (nothing was kept on the parent either): on a published tariff the settled kWh ×
        the published rate is the bill, so there is no band to keep."""
        got = self._run("published", [_iv("2024-07-01T00:00:00", 0.108, 0.007560378)])
        self.assertEqual(got, {})


if __name__ == "__main__":
    unittest.main()
