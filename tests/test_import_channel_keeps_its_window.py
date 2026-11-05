"""
A channel whose first chunk failed asks for it again, even when the other channel's landed (5 Nov 2026,
a fresh install's support file, b11).

The API import asks each channel newest → oldest from its checkpoint; until a chunk lands a channel has
none, and its ceiling was the oldest block of ANY source. On a fresh install the import's first 60 days
failed (Octopus did not answer) and the export's landed — so on the next pass the export's rows were the
import's ceiling, and the import went on below them: September and October imported with export kWh and
no import kWh, the devices clipped to nothing, Data health seeing no gap (the rows were there).
`import_api_history` now keeps each channel's top from its first call (`api_import_top_<channel>`).

Discriminating: the test not marked GUARD fails on the parent, where the second pass wrote no import kWh.
"""
import asyncio
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import engine                                     # noqa: E402
from tests import test_consumption_retention_probe as _crp   # noqa: E402

FLOOR = "2024-06-30T22:00:00"
HH = ["2024-07-01T00:00:00", "2024-07-01T00:30:00", "2024-07-01T01:00:00", "2024-07-01T01:30:00"]


class _FailsImportOnce(_crp._FakeMeasClient):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.failed = False

    async def get_measurements(self, mpan, start, end, *, direction="CONSUMPTION", **kw):
        if direction == "CONSUMPTION" and not self.failed:
            self.failed = True
            raise RuntimeError("GraphQL network error")
        return await super().get_measurements(mpan, start, end, direction=direction, **kw)


class TheRetry(unittest.TestCase):

    def setUp(self):
        self.t = _crp.TestApiImportApply("test_writes_exact_dispatch_aware_blocks")
        self.t.setUp()
        imp = engine._kraken_discovery["import"]
        engine._kraken_discovery = {"import": imp, "export": dict(imp, mpan="EX", serial="ES")}
        self.st = self.t.store

    def tearDown(self):
        self.t.tearDown()

    def _imp(self):
        return {r[0]: r[1] for r in self.st._conn.execute(
            "SELECT block_start, imp_kwh FROM blocks WHERE meter_id='electricity_main' "
            "AND source='imported_api' ORDER BY block_start")}

    def test_the_failed_import_window_is_asked_again(self):
        engine._kraken_client = _FailsImportOnce([_crp._iv(s, round(0.1 + i / 100, 3), 0.007) for i, s in enumerate(HH)])
        with self.assertLogs("engine", level="WARNING") as cm:
            first = asyncio.run(engine.import_api_history(FLOOR, restart=True))
        self.assertEqual(first["channels"]["import"]["reason"], "fetch_error")
        self.assertTrue(any("did not answer" in m for m in cm.output), "the failure is logged")
        # the export landed: its rows are now the oldest blocks in the store
        self.assertEqual(sorted(self._imp()), HH)
        self.assertTrue(all(v is None for v in self._imp().values()))
        asyncio.run(engine.import_api_history(FLOOR))
        got = self._imp()
        self.assertEqual([got[s] for s in HH], [0.1, 0.11, 0.12, 0.13])

    def test_GUARD_a_restart_asks_from_the_new_top(self):
        # a pinned top from an earlier job never outlives a restart
        self.st.set_kraken_state("api_import_top_import", "2024-07-01T01:00:00")
        engine._kraken_client = _crp._FakeMeasClient([_crp._iv(s, 0.2, 0.014) for s in HH])
        asyncio.run(engine.import_api_history(FLOOR, restart=True))
        self.assertEqual([self._imp()[s] for s in HH], [0.2] * 4)


if __name__ == "__main__":
    unittest.main()
