"""
v4 4.5.21 forward fix, ported: a REBUILT block keeps meter_type, so a re-split keeps the EV's grid
priority.

`BlockStore._row_to_block` read `meter_type` inside a try that first read `power_source`, a column
`_select_blocks` never selects; the IndexError skipped it, so every rebuilt block lost it. PASS 2
(`_apply_pass2`) gives an EV-typed device grid first and keys on that field, so every re-split —
settlement, device history written into imported blocks, a device delete — fell back to biggest
draw first: a battery charging alongside the car took the EV's grid share. v4 since 3.0.0; v5
inherited it. v4 healed the history (`ev_priority_resplit_done`); v5 carries no heals — the import
floor (4.5.21) means a database arrives already healed.

The fixture is a FLAT tariff with no dispatches: the priority keys on the meter type, not the tariff.

Discriminating: test_the_rebuild_carries_meter_type and test_settlement_keeps_the_ev_first_split
fail on the parent.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

# ── Minimal stubs so engine.py imports without HA/filesystem ─────────────────
eio = types.ModuleType("energy_engine_io")
eio.ensure_dir = lambda *a, **kw: None
eio.load_json = lambda *a, **kw: a[1] if len(a) > 1 else {}
eio.save_json_atomic = lambda *a, **kw: None
eio.save_file = lambda *a, **kw: None
sys.modules["energy_engine_io"] = eio

ec = types.ModuleType("energy_charts")
ec.generate_net_heatmap = lambda *a, **kw: ""
ec.generate_daily_import_export_charts = lambda *a, **kw: ""
sys.modules["energy_charts"] = ec

hc = types.ModuleType("ha_client")
hc.HAClient = MagicMock
sys.modules["ha_client"] = hc

from block_store import BlockStore              # noqa: E402
from kraken_rates import RateSchedule           # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
import engine                                   # noqa: E402

FLAT = 0.245
START, END = "2026-07-12T15:30:00", "2026-07-12T16:00:00"
# The real shape (a 4.5.20 database): main 7.058; battery 7.0 and EV 2.86 drew at once, solar
# covering the rest. EV first: EV 2.86, battery 4.198. Biggest first: battery 7.0, EV 0.058.
MAIN, BATT, EV = 7.058, 7.0, 2.86


class _HA:
    def get_state(self, e):
        return None


def _cfg(ev_type="ev"):
    return {"meters": {
        "electricity_main": {
            "meta": {"timezone": "Europe/London", "billing_day": 1, "block_minutes": 30,
                     "currency_symbol": "£", "sub_meter": False},
            "channels": {"import": {"read": "sensor.main_imp", "rate": "", "rate_source": "api"},
                         "export": {"read": "", "rate": ""}}},
        "house_battery": {
            "meta": {"sub_meter": True, "parent_meter": "electricity_main", "meter_type": "battery"},
            "channels": {"import": {"read": "sensor.batt_imp", "rate_source": "main"}}},
        "ev_charger": {
            "meta": {"sub_meter": True, "parent_meter": "electricity_main", "meter_type": ev_type},
            "channels": {"import": {"read": "sensor.ev_imp", "rate_source": "main"}}},
    }}


class _Harness(unittest.TestCase):
    ev_type = "ev"

    def _make_cfg(self):
        return _cfg(self.ev_type)

    def setUp(self):
        self.cfg = self._make_cfg()
        self._orig_cfg_path = engine.CONFIG_PATH
        self._cfg_dir = tempfile.mkdtemp(prefix="emt-test-cfg-")
        engine.CONFIG_PATH = os.path.join(self._cfg_dir, "meters_config.json")
        with open(engine.CONFIG_PATH, "w") as f:
            json.dump(self.cfg, f)
        self._lj = patch.object(engine, "load_json", side_effect=lambda *a, **k: self.cfg)
        self._lj.start()
        self._orig_store = engine._store
        self._orig_sched = engine._kraken_rate_schedules
        engine._store = BlockStore(":memory:")
        engine._store.insert_config_period(self.cfg)
        engine.set_data_source_mode("api")
        engine._kraken_rate_schedules = {"import": RateSchedule([
            ("2026-07-01T00:00:00", "2026-08-01T00:00:00", FLAT * 100)])}

    def tearDown(self):
        self._lj.stop()
        engine._store = self._orig_store
        engine._kraken_rate_schedules = self._orig_sched
        shutil.rmtree(self._cfg_dir, ignore_errors=True)
        engine.CONFIG_PATH = self._orig_cfg_path

    def _finalise(self, main=MAIN, batt=BATT, ev=EV, start=START, end=END):
        blk = engine.create_block(datetime.fromisoformat(start), datetime.fromisoformat(end), 30,
                                  seed_meters=True)

        def _reads(meter, lo, delta):
            blk["meters"][meter]["channels"]["import"]["reads"] = [
                {"ts": start, "value": lo}, {"ts": end, "value": round(lo + delta, 6)}]
        _reads("electricity_main", 100.0, main)
        _reads("house_battery", 10.0, batt)
        _reads("ev_charger", 20.0, ev)
        engine.finalise_block(_HA(), block_data=blk)

    def _settle(self, start=START, dcc=MAIN):
        blk = engine._store.get_block_dict_by_start(start)
        blk["meters"]["electricity_main"]["imp_kwh_api"] = dcc
        engine._rerun_pass2_for_settled_block(blk, billing_source="api", standing_resolver=None)
        engine.append_block_replace(blk)

    def _row(self, meter, start=START):
        return engine._store._conn.execute(
            "SELECT imp_kwh, imp_kwh_grid, imp_cost, imp_rate, rate_source FROM blocks "
            "WHERE block_start=? AND meter_id=?", (start, meter)).fetchone()


class TheRebuiltBlock(_Harness):

    def test_the_rebuild_carries_meter_type(self):
        self._finalise()
        blk = engine._store.get_block_dict_by_start(START)
        self.assertEqual(blk["meters"]["ev_charger"]["meta"].get("meter_type"), "ev")
        self.assertEqual(blk["meters"]["house_battery"]["meta"].get("meter_type"), "battery")

    def test_the_close_splits_ev_first(self):
        """GUARD: the block's own close always had the priority (it splits a config-built block)."""
        self._finalise()
        self.assertAlmostEqual(self._row("ev_charger")["imp_kwh_grid"], EV, places=4)
        self.assertAlmostEqual(self._row("house_battery")["imp_kwh_grid"], MAIN - EV, places=4)

    def test_settlement_keeps_the_ev_first_split(self):
        self._finalise()
        self._settle()
        self.assertAlmostEqual(self._row("ev_charger")["imp_kwh_grid"], EV, places=4)
        self.assertAlmostEqual(self._row("house_battery")["imp_kwh_grid"], MAIN - EV, places=4)

    def test_settlement_keeps_the_ha_readings(self):
        """GUARD: only the grid shares are split; each device's own reading is kept."""
        self._finalise()
        self._settle()
        self.assertAlmostEqual(self._row("ev_charger")["imp_kwh"], EV, places=4)
        self.assertAlmostEqual(self._row("house_battery")["imp_kwh"], BATT, places=4)


if __name__ == "__main__":
    unittest.main()
