"""
The battery sensor check, recorded only (battery sensor and device split design §3, §4 PR 2; the user,
29 Oct 2026).

A battery must report its charge from the grid. One that reports its total charge, solar included,
reads more than the house imported — which a grid-only charge cannot. Evidence counts only where it is
conclusive: more than 1.5 × the half-hour's whole grid import AND more than 0.25 kWh above it (staging's
trickles and its 14 Oct 2026 firmware over-reads stay below), on five days within 30. It is recorded in
store_meta, shown in the support file and logged once; since PR 3 a conclusive sensor is flagged on the
page (tests/test_battery_sensor_warning.py). Nothing in the data changes.

On real data: staging's battery (grid import) has 5 such half-hours, all on 14 Oct 2026 — kept; the
Agile-export user's (total charge) 634 on 96 days, conclusive on 4 Jul 2026.

Discriminating: every test here fails on the parent (the check is new).
"""
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine                                    # noqa: E402
import test_daily_rediscovery as R               # noqa: E402  (a real in-memory store as engine._store)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TheVerdict(unittest.TestCase):

    def test_five_days_within_thirty(self):
        days = ["2026-06-23", "2026-06-28", "2026-07-01", "2026-07-03", "2026-07-04"]
        self.assertEqual(engine.battery_sensor_verdict(days), "2026-07-04")

    def test_four_days_are_not_enough(self):
        self.assertIsNone(engine.battery_sensor_verdict(["2026-06-23", "2026-06-28", "2026-07-01", "2026-07-03"]))

    def test_five_days_spread_over_more_than_thirty_are_not(self):
        self.assertIsNone(engine.battery_sensor_verdict(
            ["2026-06-01", "2026-06-11", "2026-06-21", "2026-07-01", "2026-07-11"]))


class _Store(R._Harness):

    def setUp(self):
        super().setUp()
        c = self.st._conn
        c.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, timezone) "
                  "VALUES (1, '2020-01-01T00:00:00', 1, 30, 'UTC')")
        c.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter) VALUES (1, 'electricity_main', 0)")
        for mid, mtype in (("house_battery", "battery"), ("heat_pump", "heat_pump")):
            c.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter, parent_meter_id, meter_type) "
                      "VALUES (1, ?, 1, 'electricity_main', ?)", (mid, mtype))
        c.execute("INSERT INTO device_sensors (id, meter_id, channel, entity_id, role, added_at) "
                  "VALUES (7, 'house_battery', 'import', 'sensor.battery_grid_charge', 'primary', '2026-01-01')")
        c.commit()

    def blk(self, bs, grid, battery, sensor=None, meter="house_battery"):
        c = self.st._conn
        c.execute("INSERT OR REPLACE INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh) "
                  "VALUES (?, ?, 'electricity_main', 1, ?)", (bs, bs, grid))
        c.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, imp_sensor_id) "
                  "VALUES (?, ?, ?, 1, ?, ?)", (bs, bs, meter, battery, sensor))
        c.commit()

    def rows(self, sensor=7):
        return self.st.battery_evidence_rows("house_battery", sensor, ratio=1.5, floor_kwh=0.25)


class TheRows(_Store):

    def test_solar_charging_counts(self):
        self.blk("2026-07-01T12:00:00", 0.1, 1.8)
        self.assertEqual([r[0] for r in self.rows()], ["2026-07-01T12:00:00"])

    def test_a_trickle_does_not(self):
        """Staging: 0.035-0.18 kWh against almost no import."""
        self.blk("2025-09-15T14:00:00", 0.0, 0.18)
        self.assertEqual(self.rows(), [])

    def test_a_reading_near_the_import_does_not(self):
        self.blk("2026-07-01T02:00:00", 2.0, 2.9)             # 1.45× — rounding and timing
        self.assertEqual(self.rows(), [])

    def test_another_sensors_rows_do_not(self):
        self.blk("2026-07-01T12:00:00", 0.1, 1.8, sensor=3)
        self.assertEqual(self.rows(), [])

    def test_its_own_and_imported_rows_do(self):
        self.blk("2026-07-01T12:00:00", 0.1, 1.8, sensor=7)
        self.blk("2026-07-02T12:00:00", 0.1, 1.8, sensor=None)
        self.assertEqual(len(self.rows()), 2)

    def test_only_batteries_are_judged(self):
        self.assertEqual([m for m, _s in self.st.battery_devices()], ["house_battery"])


class TheCheck(_Store):

    def test_a_firmware_incident_is_one_day_and_kept(self):
        """Staging, 14 Oct 2026: five over-reads in one morning."""
        for h in ("03:30", "04:00", "04:30", "05:00", "05:30"):
            self.blk("2026-10-14T%s:00" % h, 0.8, 2.8)
        with self.assertNoLogs("engine", level="WARNING"):
            out = engine.check_battery_sensors()
        self.assertEqual((out["house_battery"]["half_hours"], out["house_battery"]["days"],
                          out["house_battery"]["conclusive_on"]), (5, 1, None))

    def test_five_days_are_conclusive_and_logged_once(self):
        for d in ("2026-06-23", "2026-06-28", "2026-07-01", "2026-07-03", "2026-07-04"):
            self.blk(d + "T12:00:00", 0.1, 1.8)
        with self.assertLogs("engine", level="WARNING") as log:
            out = engine.check_battery_sensors()
        self.assertEqual(out["house_battery"]["conclusive_on"], "2026-07-04")
        self.assertIn("the device split is wrong", log.output[0])
        with self.assertNoLogs("engine", level="WARNING"):
            engine.check_battery_sensors()
        self.assertEqual(self.st.get_meta(engine._BATTERY_EVIDENCE_KEY)["house_battery"]["days"], 5)

    def test_it_runs_hourly_from_the_tick(self):
        self.assertIn("_maybe_check_battery_sensors()", inspect.getsource(engine._engine_tick))
        calls = []
        orig = engine.check_battery_sensors
        engine.check_battery_sensors = lambda: calls.append(1)
        saved = engine._battery_check_at
        try:
            engine._battery_check_at = None
            for t in (100.0, 200.0, 100.0 + 3600):
                engine._maybe_check_battery_sensors(now=t)
        finally:
            engine.check_battery_sensors = orig
            engine._battery_check_at = saved
        self.assertEqual(len(calls), 2)


class TheSupportFile(unittest.TestCase):

    def test_it_carries_the_evidence(self):
        with open(os.path.join(ROOT, "web", "server.py"), encoding="utf-8") as f:
            src = f.read()
        fn = src[src.index("def _support_summary"):src.index("def _supervisor_info")]
        self.assertIn('("battery_sensor_evidence", lambda: store.get_meta("battery_sensor_evidence"))', fn)


if __name__ == "__main__":
    unittest.main()
