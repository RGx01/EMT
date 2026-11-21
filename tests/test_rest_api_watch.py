"""
Octopus's REST API, watched (21 Nov 2026).

GraphQL changes are caught weekly by introspection; the REST products API, where tariff rates come
from, has no schema to introspect. Two signals now say when it changes:

- **Shape.** Records came back and none built a period (`kraken_rates._flag_unreadable`): an ERROR
  `kraken_rest_drift` naming the fields the first record carries.
- **Emptiness.** A refresh answered with no rates for a tariff whose last schedule had periods
  (`engine._rest_went_empty`): an ERROR `kraken_rest_drift`, once until it answers again, and the
  last-known schedule is kept. Before, the import's schedule was dropped and the "tariff
  unsupported" banner raised (its message blaming the IOG 6-hour-cap tariff), and an export's or
  standing charge's emptiness said nothing.

Discriminating on the parent: the shape tests (no log), the emptiness tests that look for the log,
the kept import and export schedules, and the banner left alone. GUARDs: a new tariff code that
answers empty still raises the banner as before; a refresh with rates says nothing; readable records
say nothing. The two emptiness GUARDs fail on the parent only because the names they patch do not
exist there.
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
EXP = {"mpan": "2600000000002", "tariff_code": "E-1R-OUTGOING-FIX-12M-19-05-13-N",
       "product_code": "OUTGOING-FIX-12M-19-05-13",
       "agreements": [{"tariff_code": "E-1R-OUTGOING-FIX-12M-19-05-13-N",
                       "valid_from": "2024-03-01T00:00:00Z", "valid_to": None}]}


def _sched(rate):
    return kraken_rates.RateSchedule([("2023-12-12T00:00:00", None, rate)])


class _FakeClient:
    def __init__(self, records):
        self.records = records

    async def get_unit_rates(self, *a, **k):
        return self.records

    async def get_standing_charges(self, *a, **k):
        return self.records


class TheShape(unittest.TestCase):

    def test_records_none_readable_are_flagged_with_their_fields(self):
        renamed = [{"price_inc_vat": 24.5, "price_exc_vat": 23.3, "from": "2026-11-01T00:00:00Z"}]
        with self.assertLogs("kraken_rates", level="ERROR") as logs:
            s = asyncio.run(kraken_rates.build_rate_schedule(
                _FakeClient(renamed), "VAR-22-11-01", "E-1R-VAR-22-11-01-N", vat=kraken_rates.NO_VAT))
        self.assertTrue(s.is_empty())
        line = next(m for m in logs.output if "kraken_rest_drift" in m)
        self.assertIn("price_inc_vat", line)
        self.assertNotIn("24.5", line, "names only, no values")

    def test_standing_charges_the_same(self):
        with self.assertLogs("kraken_rates", level="ERROR") as logs:
            asyncio.run(kraken_rates.build_standing_charge_schedule(
                _FakeClient([{"amount": 60.0}]), "VAR-22-11-01", "E-1R-VAR-22-11-01-N",
                vat=kraken_rates.NO_VAT))
        self.assertTrue(any("kraken_rest_drift: standing charges" in m for m in logs.output))

    def test_GUARD_readable_records_say_nothing(self):
        good = [{"value_inc_vat": 24.5, "value_exc_vat": 23.3, "valid_from": "2026-11-01T00:00:00Z",
                 "valid_to": None}]
        with self.assertNoLogs("kraken_rates", level="ERROR"):
            s = asyncio.run(kraken_rates.build_rate_schedule(
                _FakeClient(good), "VAR-22-11-01", "E-1R-VAR-22-11-01-N", vat=kraken_rates.NO_VAT))
        self.assertFalse(s.is_empty())


class TheEmptiness(unittest.TestCase):

    def setUp(self):
        self.st = BlockStore(":memory:")
        self.answer = {}          # tariff -> RateSchedule the next refresh returns
        self.p = [mock.patch.object(engine, "_store", self.st),
                  mock.patch.object(engine, "_stored_agreements", None),
                  mock.patch.object(engine, "_kraken_discovery", {"import": IMP, "export": EXP}),
                  mock.patch.object(engine, "_kraken_client", object()),
                  mock.patch.object(engine, "_kraken_rate_schedules", {}),
                  mock.patch.object(engine, "_kraken_standing_schedule", None),
                  mock.patch.object(engine, "_rate_schedule_unsupported", None),
                  mock.patch.object(engine, "_rate_schedule_tariff", {}),
                  mock.patch.object(engine, "_rest_drift_flagged", set()),
                  mock.patch.object(kraken_rates, "build_rate_schedule", self._rates),
                  mock.patch.object(kraken_rates, "build_standing_charge_schedule", self._standing)]
        for x in self.p:
            x.start()
        engine._AGREEMENT_WINDOWS.clear()

    def tearDown(self):
        engine._AGREEMENT_WINDOWS.clear()
        for x in self.p:
            x.stop()
        self.st.close()

    async def _rates(self, client, product, tariff, *, vat, period_from=None, period_to=None,
                     raise_on_error=False):
        return self.answer.get(tariff, kraken_rates.RateSchedule([]))

    async def _standing(self, client, product, tariff, *, vat, period_from=None, period_to=None):
        return self.answer.get(("standing", tariff), kraken_rates.RateSchedule([]))

    def _refresh(self):
        asyncio.run(engine._refresh_kraken_rate_schedules())

    def _had_rates(self):
        self.answer = {IMP["tariff_code"]: _sched(24.5), EXP["tariff_code"]: _sched(15.0),
                       ("standing", IMP["tariff_code"]): _sched(60.0)}
        self._refresh()
        self.assertFalse(engine._kraken_rate_schedules["import"].is_empty())

    def test_a_tariff_that_had_rates_and_now_has_none_is_flagged_once(self):
        self._had_rates()
        self.answer = {}
        with self.assertLogs("engine", level="ERROR") as logs:
            self._refresh()
            self._refresh()
        drift = [m for m in logs.output if "kraken_rest_drift" in m]
        self.assertEqual(sum("(import)" in m for m in drift), 1, drift)
        self.assertEqual(sum("(export)" in m for m in drift), 1, drift)
        self.assertEqual(sum("(standing)" in m for m in drift), 1, drift)

    def test_the_last_known_schedules_are_kept(self):
        self._had_rates()
        self.answer = {}
        with self.assertLogs("engine", level="ERROR"):
            self._refresh()
        self.assertEqual(engine._kraken_rate_schedules["import"].resolve("2026-11-21T12:00:00"), 24.5)
        self.assertEqual(engine._kraken_rate_schedules["export"].resolve("2026-11-21T12:00:00"), 15.0)
        self.assertFalse(engine._kraken_standing_schedule.is_empty())

    def test_the_unsupported_banner_is_left_alone(self):
        self._had_rates()
        self.answer = {}
        with self.assertLogs("engine", level="ERROR"):
            self._refresh()
        self.assertIsNone(engine._rate_schedule_unsupported)

    def test_answering_again_is_said_and_rearms(self):
        self._had_rates()
        self.answer = {}
        with self.assertLogs("engine", level="ERROR"):
            self._refresh()
        with self.assertLogs("engine", level="INFO") as logs:
            self._had_rates()
        self.assertTrue(any("has rates from Octopus again" in m for m in logs.output))
        self.assertEqual(engine._rest_drift_flagged, set())

    def test_GUARD_a_new_tariff_that_answers_empty_raises_the_banner_as_before(self):
        self._had_rates()
        new = dict(IMP, tariff_code="E-1R-INTELLI-VAR-24-10-29-N", product_code="INTELLI-VAR-24-10-29")
        self.answer = {}
        with mock.patch.object(engine, "_kraken_discovery", {"import": new, "export": EXP}), \
             self.assertLogs("engine", level="ERROR"):
            self._refresh()
        self.assertEqual((engine._rate_schedule_unsupported or {}).get("tariff"),
                         new["tariff_code"])

    def test_GUARD_a_refresh_with_rates_says_nothing(self):
        self._had_rates()
        with self.assertNoLogs("engine", level="ERROR"):
            self._had_rates()


if __name__ == "__main__":
    unittest.main()
