"""
A tariff's rate history is fetched from the account's own first agreement, not the product's launch
(3 Nov 2026).

`_refresh_kraken_rate_schedules` fetched each channel's current tariff, its standing charge and the
IOG EV-device buckets with no `period_from`: the product's whole price history. A beta tester's
Agile Outgoing export was 137,588 half-hours from 2018 — about 92 requests, at every refresh, and
long enough to time out the setup wizard (#33). The agreement stitches already asked each agreement's
own window. Now these fetches start a day before the earlier of the channel's first agreement and the
first half-hour stored, so every half-hour EMT holds is covered as before (`_rate_history_from`).

Every test fails on the parent (no `_rate_history_from`; the fetches had no `period_from`).
test_GUARD_neither_known_asks_the_whole_history is a GUARD in substance: its None is the parent's
behaviour, and it fails there only because the function does not exist.
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine                                     # noqa: E402
import kraken_rates                               # noqa: E402
from block_store import BlockStore                # noqa: E402

IMP = {"mpan": "2600000000001", "tariff_code": "E-1R-VAR-22-11-01-N", "product_code": "VAR-22-11-01",
       "agreements": [{"tariff_code": "E-1R-VAR-22-11-01-N", "valid_from": "2023-12-12T00:00:00Z",
                       "valid_to": None}]}
EXP = {"mpan": "2600000000002", "tariff_code": "E-1R-AGILE-OUTGOING-19-05-13-N",
       "product_code": "AGILE-OUTGOING-19-05-13",
       "agreements": [{"tariff_code": "E-1R-AGILE-OUTGOING-19-05-13-N",
                       "valid_from": "2024-03-01T00:00:00Z", "valid_to": None}]}


class _Base(unittest.TestCase):

    def setUp(self):
        self.st = BlockStore(":memory:")
        self.p = [mock.patch.object(engine, "_store", self.st),
                  mock.patch.object(engine, "_stored_agreements", None),
                  mock.patch.object(engine, "_kraken_discovery", {"import": IMP, "export": EXP}),
                  mock.patch.object(engine, "_kraken_client", object())]
        for x in self.p:
            x.start()
        engine._AGREEMENT_WINDOWS.clear()

    def tearDown(self):
        engine._AGREEMENT_WINDOWS.clear()
        for x in self.p:
            x.stop()
        self.st.close()

    def _block(self, start):
        """The first half-hour stored: all `_rate_history_from` reads of the blocks."""
        self.st.block_span = lambda meter_id="electricity_main": (start, start)


class WhereItStarts(_Base):

    def test_a_new_install_starts_at_its_first_agreement(self):
        self.assertEqual(engine._rate_history_from("export"), "2024-02-29T00:00:00Z")
        self.assertEqual(engine._rate_history_from("import"), "2023-12-11T00:00:00Z")

    def test_history_older_than_the_agreement_is_still_covered(self):
        self._block("2022-06-01T00:00:00")          # a CAD history from before Octopus
        self.assertEqual(engine._rate_history_from("export"), "2022-05-31T00:00:00Z")

    def test_the_earliest_of_several_agreements(self):
        older = dict(EXP, agreements=[{"tariff_code": "E-1R-OUTGOING-FIX-12M-19-05-13-N",
                                       "valid_from": "2021-01-15T00:00:00Z",
                                       "valid_to": "2024-03-01T00:00:00Z"}] + EXP["agreements"])
        with mock.patch.object(engine, "_kraken_discovery", {"import": IMP, "export": older}):
            engine._AGREEMENT_WINDOWS.clear()
            self.assertEqual(engine._rate_history_from("export"), "2021-01-14T00:00:00Z")

    def test_GUARD_neither_known_asks_the_whole_history(self):
        with mock.patch.object(engine, "_kraken_discovery", {}):
            engine._AGREEMENT_WINDOWS.clear()
            self.assertIsNone(engine._rate_history_from("export"))


class TheRefreshAsksFromThere(_Base):

    def test_each_single_tariff_fetch_takes_it(self):
        asked = []

        async def _rates(client, product, tariff, *, vat, period_from=None, period_to=None,
                         raise_on_error=False):
            asked.append(("rate", tariff, period_from))
            return kraken_rates.RateSchedule([])

        async def _standing(client, product, tariff, *, vat, period_from=None, period_to=None):
            asked.append(("standing", tariff, period_from))
            return kraken_rates.RateSchedule([])

        with mock.patch.object(kraken_rates, "build_rate_schedule", _rates), \
             mock.patch.object(kraken_rates, "build_standing_charge_schedule", _standing):
            asyncio.run(engine._refresh_kraken_rate_schedules())
        self.assertIn(("rate", IMP["tariff_code"], "2023-12-11T00:00:00Z"), asked)
        self.assertIn(("rate", EXP["tariff_code"], "2024-02-29T00:00:00Z"), asked)
        self.assertIn(("standing", IMP["tariff_code"], "2023-12-11T00:00:00Z"), asked)
        self.assertFalse([a for a in asked if a[2] is None], asked)


if __name__ == "__main__":
    unittest.main()
