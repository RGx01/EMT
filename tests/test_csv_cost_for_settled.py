"""
A CSV or PDF bill gives a settled half-hour with no bill its cost, and nothing else (2 Nov 2026, the user: "As
long as we aren't replacing the settled kWh and were using the csv or pdf to understand the cost/rate I think it's
fair the csv can be used").

A CSV import is first-man-wins: a half-hour EMT already holds is left as it is. So a settled half-hour Octopus never
costed — the user's 87 on 31 Mar and 1, 5, 9, 24 Apr 2026, live blocks with DCC kWh — could not take a cost from the
day's CSV or PDF bill. The route's one exception, the spans an API import proved Octopus does not cost, read
`engine._uncostable_ranges`, which engine step 2d-1 deleted on 23 Oct 2026: the call failed and logged on every
import, and nothing was replaced.

Now `apply_csv_import(..., cost_band=)` writes the CSV row's cost as the half-hour's IMPORTED bill — the block's
kWh, rate, cost, source and every other column untouched — where its kWh has settled (DCC's, or an import's own), it
has no bill, it is not a correction, the CSV's kWh is the settled kWh (to 0.001 kWh), and `cost_band` bands the cost
(the route's: an Intelligent tariff, `engine._imported_cost_band`). The bill carries the settled kWh, so settle()
bands it as an import's; the half-hour reads `imported`. Each refusal is counted.

Checked on a copy of the user's support file (2 Nov 2026) with a made-up CSV for 24 Apr 2026: 17 taken, the
blocks' columns identical before and after.

Discriminating: every test fails on the parent tree but test_no_band_rule_no_cost, a GUARD.
"""
import os
import sys
import types
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from block_store import BlockStore
import settlement_state as ss

_HEADER = "Consumption (kwh),Estimated Cost Inc. Tax (p),Standing Charge Inc. Tax (p),Start,End\n"
S1, S2 = "2026-04-24T08:00:00", "2026-04-24T08:30:00"


def _csv(rows):
    return _HEADER + "".join(f"{k},{c},{sc},{s}+00:00,{s}+00:00\n" for k, c, sc, s in rows)


def _band(bs, cost, kwh):                        # the day's rates 7p / 30p, as the route's would band
    for band, rate in (("off_peak", 0.07), ("peak", 0.30)):
        if abs(cost - kwh * rate) <= 0.0001:
            return band
    return None


class TheCost(unittest.TestCase):

    def setUp(self):
        eio = types.ModuleType("energy_engine_io"); eio.load_json = lambda *a, **kw: {}
        sys.modules.setdefault("energy_engine_io", eio)
        self.store = BlockStore(":memory:")
        self.store.insert_config_period({"meters": {"electricity_main": {"meta": {
            "billing_day": 1, "block_minutes": 30, "timezone": "UTC",
            "currency_symbol": "£", "currency_code": "GBP", "site": "Home"}}}})
        self.cp = self.store._conn.execute("SELECT id FROM config_periods").fetchone()[0]

    def _live(self, start, kwh, api=True, corrected=False):
        end = start[:11] + ("08:30:00" if start.endswith("08:00:00") else "09:00:00")
        with self.store._conn:
            self.store._conn.execute(
                "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, imp_kwh_api, "
                "imp_rate, imp_cost, standing_charge, source, rate_corrected, rate_source) "
                "VALUES (?,?,'electricity_main',?,?,?,0.07,?,0.5,NULL,?,?)",
                (start, end, self.cp, kwh, kwh if api else None, round(kwh * 0.07, 6), 1 if corrected else 0,
                 "corrected" if corrected else "schedule"))

    def _block(self, start):
        return dict(self.store._conn.execute("SELECT * FROM blocks WHERE block_start = ?", (start,)).fetchone())

    def _bill(self, start):
        r = self.store._conn.execute("SELECT cost_incl, label, kwh FROM measured_cost WHERE slot_start = ?",
                                     (start,)).fetchone()
        return None if r is None else tuple(r)

    def test_a_settled_half_hour_takes_the_cost_and_nothing_else(self):
        self._live(S1, 1.2)
        before = self._block(S1)
        r = self.store.apply_csv_import({"import": _csv([(1.2, 36.0, 0, S1)])}, cost_band=_band)
        self.assertEqual(self._block(S1), before)                       # every column as it was
        self.assertEqual(self._bill(S1), (0.36, "IMPORTED", 1.2))
        self.assertEqual((r["costs_taken"], r["blocks_written"], r["blocks_skipped"]), (1, 0, 1))

    def test_it_then_reads_imported(self):
        self._live(S1, 1.2)
        self.store.apply_csv_import({"import": _csv([(1.2, 36.0, 0, S1)])}, cost_band=_band)
        f = self.store.get_settlement_facts(S1, S2)[0]
        self.assertEqual(ss.classify(f, smb=False, now=datetime(2026, 11, 2))["bill"], "imported")

    def test_what_is_not_taken_is_counted(self):
        self._live(S1, 1.2)
        self._live(S2, 1.0, corrected=True)
        r = self.store.apply_csv_import({"import": _csv([(1.25, 36.0, 0, S1), (1.0, 30.0, 0, S2)])}, cost_band=_band)
        self.assertEqual((r["costs_kwh_differs"], r["costs_corrected"], r["costs_taken"]), (1, 1, 0))
        self.assertIsNone(self._bill(S1))
        self.assertIsNone(self._bill(S2))

    def test_a_cost_of_neither_rate_is_not_taken(self):
        self._live(S1, 1.0)
        r = self.store.apply_csv_import({"import": _csv([(1.0, 18.5, 0, S1)])}, cost_band=_band)   # a blend
        self.assertEqual((r["costs_no_band"], r["costs_taken"]), (1, 0))
        self.assertIsNone(self._bill(S1))

    def test_unsettled_and_billed_are_left(self):
        self._live(S1, 1.0, api=False)                                   # CAD only: kWh not settled
        self._live(S2, 1.0)
        self.store.upsert_measured_cost(S2, cost_incl=0.07, label="OFF_PEAK", kwh=1.0)   # Octopus's own
        r = self.store.apply_csv_import({"import": _csv([(1.0, 30.0, 0, S1), (1.0, 30.0, 0, S2)])},
                                        cost_band=_band)
        self.assertEqual(r["costs_taken"], 0)
        self.assertIsNone(self._bill(S1))
        self.assertEqual(self._bill(S2), (0.07, "OFF_PEAK", 1.0))

    def test_no_band_rule_no_cost(self):
        # GUARD: passes on the parent tree too — without the route's rule nothing is taken
        self._live(S1, 1.2)
        self.store.apply_csv_import({"import": _csv([(1.2, 36.0, 0, S1)])})
        self.assertIsNone(self._bill(S1))


class TheRoute(unittest.TestCase):

    def test_it_passes_the_rule_and_the_dead_call_is_gone(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "server.py"),
                   encoding="utf-8").read()
        i = src.index("def api_historical_csv_apply(")
        body = src[i:src.index("\n@app.route", i)]
        self.assertNotIn("_uncostable_ranges(", body)
        self.assertIn("bill_mpan=bill_mpan, cost_band=_cost_band),", body)
        self.assertIn('if _eng.tariff_kind_at(bs) != "intelligent":', body)
        self.assertIn("return _eng._imported_cost_band(bs, cost, kwh, kwh)", body)


if __name__ == "__main__":
    unittest.main()
