"""
BL-85 (13 Oct 2026): the Charts pill names the stage of settlement its half-hours wait at.

It read "N blocks awaiting cost settlement" and counted only half-hours without their DCC kWh —
the first of three stages (ROADMAP BL-82's model: kWh, then the bill, then on IOG-SMB the split).
`/api/billing-source` now sends `awaiting: {kwh, bill, split}` and the pill names each one.

Discriminating: every test here fails on the parent.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
import test_rebuild_meter_type as H              # noqa: E402  (stubs, real engine)
from block_store import BlockStore               # noqa: E402
engine = H.engine
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

OFF = 0.05493


def _ago(h):
    return (datetime.now(timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)
            - timedelta(hours=h)).isoformat()


class _Stages(unittest.TestCase):
    NO_KWH, BILL_DUE, BILL_DUE_DISPATCHED, BILLED = _ago(6), _ago(40), _ago(41), _ago(60)
    SINCE = _ago(14 * 24)

    def setUp(self):
        self.st = BlockStore(":memory:")
        self.st._conn.execute(
            "INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, "
            "block_minutes, timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
        for slot, api in ((self.NO_KWH, None), (self.BILL_DUE, 2.0),
                          (self.BILL_DUE_DISPATCHED, 2.0), (self.BILLED, 2.0)):
            self.st._conn.execute(
                "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
                "imp_kwh_api, imp_rate, imp_cost, rate_source) VALUES (?,?,?,1,2.0,?,?,?,'schedule')",
                (slot, slot, "electricity_main", api, OFF, round(2 * OFF, 6)))
        self.st.upsert_dispatch_slot(self.BILL_DUE_DISPATCHED, off_peak=True, source="smart-charge")
        self.st.upsert_measured_cost(self.BILLED, mpan="m", cost_incl=0.11, cost_excl=0.11,
                                     label="OFF_PEAK", kwh=2.0)
        self.st._conn.commit()


class TheBillStage(_Stages):

    def test_on_iog_smb_every_half_hour_with_settled_kwh_waits_for_its_bill(self):
        self.assertEqual(self.st.count_awaiting_bill(self.SINCE, every_half_hour=True), 2)

    def test_elsewhere_only_a_dispatched_one(self):
        self.assertEqual(self.st.count_awaiting_bill(self.SINCE, every_half_hour=False), 1)

    def test_a_bill_held_for_its_split_waits_at_the_next_stage(self):
        self.assertEqual(self.st.count_awaiting_bill(self.SINCE, every_half_hour=True,
                                                     exclude=[self.BILL_DUE]), 1)


class TheEngine(_Stages):

    def setUp(self):
        super().setUp()
        self._save = (engine._kraken_discovery, engine._import_is_smb_capped,
                      engine._kraken_current_agreement_from)
        engine._kraken_current_agreement_from = "2026-01-01T00:00:00"

    def tearDown(self):
        (engine._kraken_discovery, engine._import_is_smb_capped,
         engine._kraken_current_agreement_from) = self._save

    def test_three_stages_on_iog_smb(self):
        engine._kraken_discovery = {"import": {"tariff_code": "E-1R-IOG-SMB-X-B"}}
        engine._import_is_smb_capped = lambda: True
        engine._bill_split_wait_save(self.st, {self.BILL_DUE: {"first": _ago(30)}})
        a = engine.settlement_awaiting(self.st, self.SINCE)
        self.assertEqual(a, {"kwh": 1, "bill": 1, "split": 1})

    def test_no_bill_stage_where_emt_fetches_no_bill(self):
        """A flat or Agile tariff: the cost is kWh × rate, final with the kWh."""
        engine._kraken_discovery = {"import": {"tariff_code": "E-1R-AGILE-24-10-01-B"}}
        engine._import_is_smb_capped = lambda: False
        self.assertEqual(engine.settlement_awaiting(self.st, self.SINCE)["bill"], 0)


class ThePill(unittest.TestCase):
    PAGE = open(os.path.join(_ROOT, "web", "templates", "charts.html"), encoding="utf-8").read()

    def test_it_names_the_stages_from_awaiting(self):
        i = self.PAGE.index("function _settleStages(d)")
        body = self.PAGE[i:self.PAGE.index("\n  }\n", i)]
        for what in ("'kWh'", "'bill'", "'split'"):
            self.assertIn(what, body)
        self.assertIn("d.awaiting", body)

    def test_it_no_longer_calls_the_kwh_count_cost_settlement(self):
        j = self.PAGE.index("(function loadSettleNote(){")
        body = self.PAGE[j:self.PAGE.index("})();", j)]
        self.assertNotIn("awaiting cost settlement'", body)
        self.assertIn("_settleStages(d)", body)


class TheRoute(unittest.TestCase):

    def test_it_sends_the_stages(self):
        import ast
        src = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "api_billing_source_get")
        body = ast.unparse(fn)
        self.assertIn("settlement_awaiting", body)
        self.assertIn("'awaiting': awaiting", body)


if __name__ == "__main__":
    unittest.main()
