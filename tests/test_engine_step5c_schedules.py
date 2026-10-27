"""
Engine step 5c (27 Oct 2026; EMT-v5_engine_step5_design.md §2): an agreement's rates are fetched once.

`kraken_rates.agreement_schedules` is the one per-agreement fetch: the refresh's import stitch and the
API import's rate and standing-charge schedules read through it — closed agreements cached for the
process, the running one fetched every call (v4 #513). Until now the import fetched every agreement
again for itself, and again after a restart; and it asked for export's standing charges, which an
export tariff does not have.

Discriminating: every test here fails on the parent, but those labelled GUARD.
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import test_engine  # noqa: F401  — installs stubs, imports engine
import engine
import kraken_rates

CLOSED = {"tariff_code": "E-1R-INTELLI-VAR-22-10-14-B",
          "valid_from": "2024-01-01T00:00:00Z", "valid_to": "2025-01-01T00:00:00Z"}
RUNNING = {"tariff_code": "E-1R-INTELLI-FIX-12M-26-03-17-B",
           "valid_from": "2025-01-01T00:00:00Z", "valid_to": None}


class _Base(unittest.TestCase):

    def setUp(self):
        self.called = []

        def _sched():
            return kraken_rates.RateSchedule([("2000-01-01T00:00:00", None, 30.0)])

        async def _fake(client, product, tariff, *, vat=None, period_from=None, period_to=None,
                        **kw):
            self.called.append(tariff)
            return _sched()
        kraken_rates._AGREEMENT_SCHED_CACHE.clear()
        self.p = [mock.patch.object(kraken_rates, "build_rate_schedule", _fake),
                  mock.patch.object(kraken_rates, "build_standing_charge_schedule", _fake),
                  mock.patch.object(engine, "_kraken_client", mock.MagicMock()),
                  mock.patch.object(engine, "_kraken_discovery", {
                      "import": {"agreements": [CLOSED, RUNNING]},
                      "export": {"agreements": [dict(CLOSED, tariff_code="E-1R-OUTGOING-FIX-B")]}})]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        kraken_rates._AGREEMENT_SCHED_CACHE.clear()


class OneFetch(_Base):

    def test_the_import_reads_what_the_stitch_fetched(self):
        client = mock.MagicMock()
        client._tariff_to_product_code = lambda tc: "P"
        asyncio.run(kraken_rates.build_agreement_stitched_schedule(
            client, [CLOSED, RUNNING], vat=[]))
        asyncio.run(engine._build_channel_rate_segs("import"))
        self.assertEqual(self.called.count(CLOSED["tariff_code"]), 1, "the closed one, once")
        self.assertEqual(self.called.count(RUNNING["tariff_code"]), 2, "the running one, each time")

    def test_a_closed_standing_charge_is_fetched_once(self):
        asyncio.run(engine._build_channel_standing_segs("import"))
        asyncio.run(engine._build_channel_standing_segs("import"))
        self.assertEqual(self.called.count(CLOSED["tariff_code"]), 1)

    def test_export_has_no_standing_charge(self):
        self.assertEqual(asyncio.run(engine._build_channel_standing_segs("export")), [])
        self.assertEqual(self.called, [])

    def test_GUARD_each_agreement_keeps_its_window(self):
        segs = asyncio.run(engine._build_channel_rate_segs("import"))
        self.assertEqual([(a, b) for a, b, _s in segs],
                         [("2024-01-01T00:00:00", "2025-01-01T00:00:00"),
                          ("2025-01-01T00:00:00", None)])


if __name__ == "__main__":
    unittest.main()
