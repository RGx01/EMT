"""
G2b (tariff prices design §4, 31 Oct 2026): the closed agreements' prices the refresh does not fetch
— import's standing charges and export's rates — fetched from Octopus's published tariff pages, a few
a cycle, once ever.

What they pin (`engine._fetch_closed_agreement_prices`):

- **What is fetched:** a closed agreement's import standing charge and rates and export rates, where
  none is stored; never a running agreement (the refresh keeps those) nor a degenerate window.
- **Once ever:** once its prices are stored, a closed agreement is not fetched again.
- **Capped and paced:** at most 4 agreement-kinds a cycle.
- **A failure** (nothing comes back) is tried again after a day, three times at most, the count in
  kraken_state so a restart does not reset it.
- **Agile:** no call for its unit rates; its `dynamic` row is written from the agreement's window.
- It runs in the poll cycle, after the rate refresh.

Discriminating: every test fails on the parent (G2b is new).
"""
import asyncio
import json
import os
import sys
import time
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

GO = "E-1R-GO-VAR-22-10-14-A"
OUT = "E-1R-OUTGOING-FIX-12M-19-05-13-A"
AGILE = "E-1R-AGILE-24-10-01-A"


def _ag(tc, vf, vt):
    return {"tariff_code": tc, "valid_from": vf, "valid_to": vt}


class _Base(unittest.TestCase):

    def setUp(self):
        import engine
        import kraken_rates
        from block_store import BlockStore
        self.eng, self.kr = engine, kraken_rates
        engine._store = self.st = BlockStore(":memory:")
        self._saved = (engine._kraken_client, engine._agreement_info, engine._CLOSED_PRICES_PACE_S,
                       kraken_rates.agreement_schedules)
        engine._kraken_client = object()
        engine._CLOSED_PRICES_PACE_S = 0
        self.calls = []
        self.answer = lambda ag, kind: [(ag["valid_from"], ag["valid_to"], kraken_rates.RateSchedule(
            [(ag["valid_from"], ag["valid_to"], 21.0)], [(ag["valid_from"], ag["valid_to"], 20.0)]))]

        async def fake(client, agreements, *, kind="rate", product_of=None, on_built=None):
            self.calls.append((agreements[0]["tariff_code"], kind))
            return self.answer(agreements[0], kind)
        kraken_rates.agreement_schedules = fake
        self.agreements = {"import": [_ag(GO, "2023-09-20T23:00:00", "2024-06-11T23:00:00"),
                                      _ag(GO, "2026-08-25T23:00:00", None)],
                           "export": [_ag(OUT, "2024-07-13T23:00:00", "2025-07-13T23:00:00"),
                                      _ag(OUT, "2024-07-13T23:00:00", "2024-07-13T23:00:00")]}
        engine._agreement_info = lambda ch: {"agreements": self.agreements.get(ch, [])}

    def tearDown(self):
        (self.eng._kraken_client, self.eng._agreement_info, self.eng._CLOSED_PRICES_PACE_S,
         self.kr.agreement_schedules) = self._saved

    def run_cycle(self):
        return asyncio.run(self.eng._fetch_closed_agreement_prices())

    def stored(self):
        return sorted((r[0], r[1]) for r in self.st._conn.execute(
            "SELECT tariff_code, kind FROM tariff_prices"))


class WhatIsFetched(_Base):

    def test_closed_import_standing_and_rates_and_export_rates(self):
        self.assertEqual(self.run_cycle(), 3)
        self.assertEqual(sorted(self.calls), [(GO, "rate"), (GO, "standing"), (OUT, "rate")])
        self.assertEqual(self.stored(), [(GO, "standing"), (GO, "unit"), (OUT, "unit")])

    def test_once_ever(self):
        self.run_cycle()
        self.calls.clear()
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.calls, [])

    def test_capped_a_cycle(self):
        self.agreements["import"] = [_ag(GO, "2023-0%d-01T00:00:00" % m, "2023-0%d-01T00:00:00" % (m + 1))
                                     for m in range(1, 5)]
        self.assertEqual(self.run_cycle(), 4)
        self.assertEqual(self.run_cycle(), 4)
        self.assertEqual(self.run_cycle(), 1)          # 4 agreements × 2 kinds, then export's


class AFailure(_Base):

    def test_is_tried_again_after_a_day_three_times_at_most(self):
        self.answer = lambda ag, kind: []
        self.agreements = {"export": [self.agreements["export"][0]]}
        self.run_cycle()
        self.assertEqual(len(self.calls), 1)
        self.run_cycle()
        self.assertEqual(len(self.calls), 1)          # not within the day
        key = next(iter(json.loads(self.st.get_kraken_state("tariff_prices_tried"))))
        for n in (1, 2):
            self.st.set_kraken_state("tariff_prices_tried",
                                     json.dumps({key: {"n": n, "at": time.time() - 86401}}))
            self.run_cycle()
        self.assertEqual(len(self.calls), 3)
        self.st.set_kraken_state("tariff_prices_tried", json.dumps({key: {"n": 3, "at": 0}}))
        self.run_cycle()
        self.assertEqual(len(self.calls), 3)          # given up


class Agile(_Base):

    def test_no_call_for_its_rates_a_dynamic_row(self):
        self.agreements = {"import": [_ag(AGILE, "2024-01-01T00:00:00", "2024-12-01T00:00:00")]}
        self.run_cycle()
        self.assertEqual(self.calls, [(AGILE, "standing")])
        row = self.st._conn.execute("SELECT band, value_exc, valid_from, valid_to FROM tariff_prices "
                                    "WHERE kind = 'unit'").fetchone()
        self.assertEqual(tuple(row), ("dynamic", None, "2024-01-01T00:00:00", "2024-12-01T00:00:00"))


class ThePollCycle(unittest.TestCase):

    def test_it_runs_after_the_rate_refresh(self):
        src = open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read()
        i = src.index("            await _refresh_kraken_rate_schedules()\n            # a change of agreements")
        self.assertIn("await _fetch_closed_agreement_prices()", src[i:i + 400])


if __name__ == "__main__":
    unittest.main()
