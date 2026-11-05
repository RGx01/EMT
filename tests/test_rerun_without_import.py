"""
The PASS 2 re-run of a main block with no import channel (5 Nov 2026, a fresh install's support file, b12).

`_rerun_pass2_for_settled_block` set `_settled = None` only inside `if imp_ch is not None:` and read it
again after PASS 2, so a main block holding only its export — an import that kept only the export (#88) —
raised UnboundLocalError ("cannot access local variable '_settled'") at every drain of the re-run queue:
the block left flagged, retried and logged as an ERROR each time (40 times each for ten half-hours in the
support file). `_settled` is now set before the branch.

Discriminating: the test not marked GUARD fails on the parent.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine                                    # noqa: E402
from block_store import BlockStore               # noqa: E402

BS = "2026-11-03T19:00:00"


class TheRerun(unittest.TestCase):

    def setUp(self):
        from kraken_rates import RateSchedule
        self.store = BlockStore(":memory:")
        self.store.insert_config_period({"meters": {"electricity_main": {"meta": {
            "timezone": "Europe/London", "billing_day": 1, "block_minutes": 30,
            "currency_symbol": "£", "sub_meter": False}}}})
        cp = self.store._conn.execute("SELECT id FROM config_periods LIMIT 1").fetchone()["id"]
        self.store._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, interpolated, "
            "imp_kwh, exp_kwh, exp_rate, exp_cost, standing_charge, needs_pass2_rerun, source) "
            "VALUES (?, ?, 'electricity_main', ?, 0, NULL, 0.3, 0.12, 0.036, 0.48, 1, 'imported_api')",
            (BS, "2026-11-03T19:30:00", cp))
        self.store._conn.commit()
        self._s, self._r = engine._store, engine._kraken_rate_schedules
        engine._store = self.store
        engine._kraken_rate_schedules = {"export": RateSchedule([("2026-03-01T00:00:00", None, 12.0)])}

    def tearDown(self):
        engine._store, engine._kraken_rate_schedules = self._s, self._r

    def test_a_block_with_no_import_channel_reruns(self):
        block = self.store.get_block_dict_by_start(BS)
        self.assertIsNone((block["meters"]["electricity_main"].get("channels") or {}).get("import"),
                          "precondition: no import channel")
        out = engine._rerun_pass2_for_settled_block(block, rate_resolver=engine._kraken_rate_resolver,
                                                    billing_source="dcc")
        self.assertIs(out, block)

    def test_GUARD_its_export_is_kept(self):
        block = self.store.get_block_dict_by_start(BS)
        try:
            engine._rerun_pass2_for_settled_block(block, rate_resolver=engine._kraken_rate_resolver,
                                                  billing_source="dcc")
        except UnboundLocalError:
            pass
        ex = block["meters"]["electricity_main"]["channels"]["export"]
        self.assertAlmostEqual(ex["kwh"], 0.3)


if __name__ == "__main__":
    unittest.main()
