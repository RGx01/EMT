"""
The setup gap, from Home Assistant's recorder (5 Nov 2026; docs/design/EMT-v5_setup_gap_from_ha_design.md).

A new install on `cad+api` imports Octopus's history to its last settled half-hour and records live from
its first tick. The half-hours between appeared only when the poll backfilled them from Octopus's settled
figures, a day or two later — kWh-settled from the start, never predicted. Now, after an import (Octopus's
or a CSV), the empty ones are filled from HA's 5-minute statistics for the CAD: unsettled, marked
`interpolated = 3`, `source = 'ha_recorder'`, flagged for PASS 2, then the devices are split over them.
What HA cannot vouch for — a half-hour missing a row, the one after a hole, a negative change, one over
the ceiling — is left to the poll, as before.

The store reads `interpolated` back as its integer: written `1 if … else 0`, a round trip (the PASS 2
drain's) turned a backfilled half-hour's 2 into 1, and would have turned 3 into 1.

Discriminating: every test here fails on the parent (no `fill_setup_gap_from_ha`, no
`create_recorder_blocks`, the marker read as a bool, the clamp off for any marker).
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import engine                                       # noqa: E402
from tests.test_device_sensors_storage import _cfg, _store      # noqa: E402

ENGINE = open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read()
SERVER = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()

LAST_IMPORTED = "2026-11-02T23:30:00"
GAP = ["2026-11-03T00:00:00", "2026-11-03T00:30:00", "2026-11-03T01:00:00", "2026-11-03T01:30:00"]
FIRST_LIVE = "2026-11-03T02:00:00"


def _ms(iso):
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000.0


class FakeHA:
    """HA's 5-minute statistics: 0.1 kWh a row, every row present unless dropped or overridden."""

    def __init__(self, drop=(), change=None, rows_for=None):
        self.drop, self.change, self.calls = set(drop), dict(change or {}), []
        self.rows_for = rows_for

    async def get_statistics(self, ids, start, end, period="hour", types=None, timeout=0):
        self.calls.append((tuple(ids), start, end, period))
        t = datetime.fromisoformat(start).replace(tzinfo=None)
        e = datetime.fromisoformat(end).replace(tzinfo=None)
        rows = []
        while t < e:
            k = t.isoformat()
            if k not in self.drop:
                rows.append({"start": _ms(k), "change": self.change.get(k, 0.1)})
            t += timedelta(minutes=5)
        return {i: rows for i in ids}


def _main_row(st, start, source=None, kwh=1.0):
    with st._conn:
        st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                         "imp_kwh, source) VALUES (?, ?, 'electricity_main', ?, ?, ?)",
                         (start, start, st.get_current_config_period_id(), kwh, source))


def _row(st, start):
    r = st._conn.execute("SELECT * FROM blocks WHERE meter_id = 'electricity_main' AND "
                         "block_start = ?", (start,)).fetchone()
    return None if r is None else dict(r)


class _Base(unittest.TestCase):
    MODE = "cad+api"
    EXPORT = None

    def setUp(self):
        cfg = _cfg(main_read="sensor.cad_import")
        if self.EXPORT:
            cfg["meters"]["electricity_main"]["channels"]["export"] = {"read": self.EXPORT}
        self.cfg = cfg
        self.st = _store(cfg)
        _main_row(self.st, LAST_IMPORTED, "imported_api")
        with self.st._conn:
            self.st._conn.execute("INSERT INTO ha_reads (meter_id, channel, origin, period_start, "
                                  "minutes, kwh, recorded_at) VALUES ('electricity_main', 'import', "
                                  "'live', ?, 30, 0.5, ?)", (FIRST_LIVE, FIRST_LIVE))
        _main_row(self.st, FIRST_LIVE, None)
        self.p = [mock.patch.object(engine, "get_store", lambda: self.st),
                  mock.patch.object(engine, "_store", self.st),
                  mock.patch.object(engine, "load_config", lambda: self.cfg),
                  mock.patch.object(engine, "get_data_source_mode", lambda: self.MODE)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def _fill(self, ha):
        with mock.patch.object(engine, "_engine_ha", ha):
            return asyncio.run(engine.fill_setup_gap_from_ha())


class TheFill(_Base):

    def test_the_gap_is_filled_unsettled_and_marked(self):
        ha = FakeHA()
        res = self._fill(ha)
        self.assertEqual((res["filled"], res["span"]), (4, [GAP[0], GAP[-1]]))
        for bs in GAP:
            r = _row(self.st, bs)
            self.assertAlmostEqual(r["imp_kwh"], 0.6)
            self.assertEqual((r["imp_kwh_api"], r["needs_pass2_rerun"], r["source"], r["interpolated"]),
                             (None, 1, "ha_recorder", 3))
        # one call, 5-minute rows, from the row before the gap's first half-hour
        self.assertEqual(ha.calls, [(("sensor.cad_import",), "2026-11-02T23:55:00+00:00",
                                     "2026-11-03T02:00:00+00:00", "5minute")])
        self.assertEqual(_row(self.st, FIRST_LIVE)["imp_kwh"], 1.0)        # the live one untouched

    def test_a_half_hour_missing_a_row_and_the_one_after_the_hole_wait(self):
        res = self._fill(FakeHA(drop={"2026-11-03T00:55:00"}))
        self.assertEqual(res["filled"], 2)
        self.assertEqual(res["refused"], {"incomplete": 1, "after_hole": 1})
        self.assertIsNone(_row(self.st, GAP[1]))
        self.assertIsNone(_row(self.st, GAP[2]))
        self.assertIsNotNone(_row(self.st, GAP[3]))

    def test_without_the_row_before_the_first_waits_too(self):
        res = self._fill(FakeHA(drop={"2026-11-02T23:55:00"}))
        self.assertIsNone(_row(self.st, GAP[0]))
        self.assertEqual(res["refused"], {"after_hole": 1})

    def test_a_negative_change_and_one_over_the_ceiling_wait(self):
        res = self._fill(FakeHA(change={"2026-11-03T00:05:00": -3.0, "2026-11-03T01:05:00": 9999.0}))
        self.assertEqual(res["refused"], {"negative": 1, "ceiling": 1})
        self.assertIsNone(_row(self.st, GAP[0]))
        self.assertIsNone(_row(self.st, GAP[2]))

    def test_a_half_hour_with_a_block_is_left(self):
        _main_row(self.st, GAP[1], "kraken_api", kwh=2.0)
        res = self._fill(FakeHA())
        self.assertEqual(res["filled"], 3)
        self.assertEqual((_row(self.st, GAP[1])["imp_kwh"], _row(self.st, GAP[1])["source"]),
                         (2.0, "kraken_api"))

    def test_a_deleted_half_hour_stays_deleted(self):
        with self.st._conn:
            self.st._conn.execute("INSERT INTO deleted_ranges (meter_id, start_utc, end_utc, "
                                  "created_at, reason) VALUES ('electricity_main', ?, ?, ?, "
                                  "'user_delete')", (GAP[0], GAP[1], GAP[0]))
        res = self._fill(FakeHA())
        self.assertEqual(res["filled"], 3)
        self.assertIsNone(_row(self.st, GAP[0]))

    def test_no_ha_nothing(self):
        self.assertEqual(self._fill(None)["reason"], "no_ha")
        self.assertIsNone(_row(self.st, GAP[0]))


class WithExport(_Base):
    EXPORT = "sensor.cad_export"

    def test_the_export_is_filled_beside_it(self):
        self._fill(FakeHA())
        self.assertAlmostEqual(_row(self.st, GAP[0])["exp_kwh"], 0.6)


class OnApi(_Base):
    MODE = "api"

    def test_api_mode_has_no_cad_so_nothing(self):
        ha = FakeHA()
        self.assertEqual(self._fill(ha)["reason"], "mode")
        self.assertEqual(ha.calls, [])


class WhatFollows(_Base):

    def test_octopus_s_figure_is_stored_on_a_filled_half_hour(self):
        self._fill(FakeHA())
        res = self.st.upsert_kraken_block(GAP[0], "electricity_main", 0.62, channel="import",
                                          source="kraken_api", billing_source="api")
        self.assertEqual(res["status"], "stored")
        self.assertAlmostEqual(_row(self.st, GAP[0])["imp_kwh_api"], 0.62)

    def test_a_round_trip_keeps_the_marker(self):
        self._fill(FakeHA())
        b = self.st.get_block_dict_by_start(GAP[0])
        self.st.append_block_replace(b)
        self.assertEqual(_row(self.st, GAP[0])["interpolated"], 3)

    def test_a_round_trip_keeps_a_backfilled_marker(self):
        self.st.create_backfill_block(GAP[0], "electricity_main", 0.5)
        self.st.append_block_replace(self.st.get_block_dict_by_start(GAP[0]))
        self.assertEqual(_row(self.st, GAP[0])["interpolated"], self.st.BACKFILL_INTERPOLATED)
        self.assertEqual(self.st.count_backfilled_blocks(), 1)

    def test_the_device_rebuild_takes_in_the_fill(self):
        self.assertEqual(engine._setup_gap_hi({"span": [GAP[0], GAP[-1]]}, GAP[0]), FIRST_LIVE)
        self.assertEqual(engine._setup_gap_hi({"span": None}, GAP[0]), GAP[0])


class TheClamp(unittest.TestCase):
    """A filled half-hour is a reading: its devices are clipped to the grid as on a live one."""

    def _clamp(self, interpolated, api=None):
        m = {"block_start": GAP[0], "imp_kwh": 1.0, "imp_kwh_api": api,
             "interpolated": interpolated, "source": "ha_recorder"}
        with mock.patch.object(engine, "tariff_kind_at", lambda bs: "none"), \
             mock.patch.object(engine, "_agreement_priced_ok", lambda bs: False):
            return engine.settle_facts(m, [], None).clamp

    def test_a_filled_half_hour_is_clamped(self):
        self.assertTrue(self._clamp(3))

    def test_GUARD_a_locally_interpolated_one_is_not(self):
        self.assertFalse(self._clamp(1))


class TheImports(unittest.TestCase):

    def test_the_api_import_fills_before_its_device_rebuild(self):
        i = ENGINE.index("_hi = _setup_gap_hi(await fill_setup_gap_from_ha(), _hi)")
        self.assertLess(i, ENGINE.index("_ih[\"from\"], _hi, fetch_new=True)", i))

    def test_the_csv_import_too(self):
        i = SERVER.index("def _rebuild_devices_after_csv(")
        body = SERVER[i:SERVER.index("\ndef ", i + 1)]
        self.assertLess(body.index("_eng.fill_setup_gap_from_ha()"),
                        body.index("rebuild_devices_after_refill(span[\"from\"], hi, fetch_new=True)"))


if __name__ == "__main__":
    unittest.main()
