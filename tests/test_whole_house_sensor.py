"""
BL-108: a device sensor that reads the whole house (5 Nov 2026, a tester's support file; the user: "add the
BL item then build it").

A GivEnergy battery was set up with GivTCP's `import_energy_today_kwh` — the house's grid import at the
inverter's meter. It matched the main meter at every hour of the day; in the 44 half-hours the Zappi charged
the car, the "battery" read 261.5 kWh of the meter's 262.0. The battery took everything but the car and the
house read near zero. The battery check looks for more than the grid, and this read the same.

Any device's sensor is now judged: over the meter's last 30 days, within 5% of the meter's import in at least
75% of the half-hours the house imported over 0.1 kWh, on 200 or more. On real data: that battery 331 of 385
(86%); every other device in the databases to hand 9% at most. Flag only, as the battery check (the user,
29 Oct 2026): logged once, a banner, a card note, *Keep this sensor anyway*; nothing in the data changes.

Discriminating: every test fails on the parent tree, where none of it existed.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine                                    # noqa: E402
import test_daily_rediscovery as R               # noqa: E402  (a real in-memory store as engine._store)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*p):
    return open(os.path.join(ROOT, *p), encoding="utf-8").read()


class TheVerdict(unittest.TestCase):

    def test_three_quarters_of_two_hundred(self):
        self.assertTrue(engine.whole_house_verdict(200, 150))
        self.assertFalse(engine.whole_house_verdict(200, 149))
        self.assertFalse(engine.whole_house_verdict(199, 199), "too few half-hours to say")


class _Store(R._Harness):

    def setUp(self):
        super().setUp()
        c = self.st._conn
        c.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, timezone) "
                  "VALUES (1, '2020-01-01T00:00:00', 1, 30, 'UTC')")
        c.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter) VALUES (1, 'electricity_main', 0)")
        for mid, mtype in (("aio", "battery"), ("zappi", "ev")):
            c.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter, parent_meter_id, meter_type) "
                      "VALUES (1, ?, 1, 'electricity_main', ?)", (mid, mtype))
        c.execute("INSERT INTO device_sensors (id, meter_id, channel, entity_id, role, added_at) "
                  "VALUES (7, 'aio', 'import', 'sensor.givtcp_import_energy_today_kwh', 'primary', '2026-01-01')")
        c.execute("INSERT INTO device_sensors (id, meter_id, channel, entity_id, role, added_at) "
                  "VALUES (8, 'zappi', 'import', 'sensor.zappi_energy_used_today', 'primary', '2026-01-01')")
        c.commit()

    def day(self, date, aio, zappi=None, sensor=None):
        """A day of half-hours: the meter 1 kWh each; the battery `aio(i)`, the car `zappi(i)`."""
        c = self.st._conn
        for i in range(48):
            bs = "%sT%02d:%02d:00" % (date, i // 2, 30 * (i % 2))
            c.execute("INSERT OR REPLACE INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh) "
                      "VALUES (?, ?, 'electricity_main', 1, 1.0)", (bs, bs))
            c.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, imp_sensor_id) "
                      "VALUES (?, ?, 'aio', 1, ?, ?)", (bs, bs, aio(i), sensor))
            if zappi:
                c.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh) "
                          "VALUES (?, ?, 'zappi', 1, ?)", (bs, bs, zappi(i)))
        c.commit()

    def days(self, n, aio, **kw):
        for d in range(n):
            self.day("2026-10-%02d" % (d + 1), aio, **kw)


class TheEvidence(_Store):

    def test_every_device_is_judged(self):
        self.assertEqual([m for m, _s in self.st.device_primary_sensors()], ["aio", "zappi"])

    def test_the_whole_house_matches_a_real_battery_does_not(self):
        self.days(5, lambda i: 0.98)                       # within 5% of 1 kWh, every half-hour
        ev = self.st.whole_house_evidence("aio", 7, days=30, tol=0.05, floor_kwh=0.1)
        self.assertEqual((ev["n"], ev["matched"]), (240, 240))
        self.st._conn.execute("DELETE FROM blocks")
        self.days(5, lambda i: 1.0 if i < 8 else 0.0)      # a battery filling at night only
        ev = self.st.whole_house_evidence("aio", 7, days=30, tol=0.05, floor_kwh=0.1)
        self.assertEqual((ev["n"], ev["matched"]), (240, 40))

    def test_only_the_last_30_days_of_the_meter(self):
        self.day("2026-08-01", lambda i: 1.0)
        self.days(5, lambda i: 0.0)
        ev = self.st.whole_house_evidence("aio", 7, days=30, tol=0.05, floor_kwh=0.1)
        self.assertEqual((ev["n"], ev["matched"]), (240, 0))


class TheCheck(_Store):

    def test_found_flagged_and_logged_once(self):
        self.days(5, lambda i: 1.0, zappi=lambda i: 0.0)
        with self.assertLogs("engine", level="WARNING") as log:
            out = engine.check_whole_house_sensors()
        self.assertTrue(out["aio"]["whole_house"])
        self.assertFalse(out["zappi"]["whole_house"])
        self.assertIn("reads what the whole house imports", log.output[0])
        with self.assertNoLogs("engine", level="WARNING"):
            engine.check_whole_house_sensors()
        self.assertEqual(list(engine.whole_house_sensor_flags()), ["aio"])

    def test_a_real_device_is_not(self):
        self.days(5, lambda i: 1.0 if i < 8 else 0.0)
        with self.assertNoLogs("engine", level="WARNING"):
            out = engine.check_whole_house_sensors()
        self.assertFalse(out["aio"]["whole_house"])
        self.assertEqual(engine.whole_house_sensor_flags(), {})

    def test_kept_anyway_then_shown_again(self):
        self.days(5, lambda i: 1.0)
        engine.check_whole_house_sensors()
        self.assertTrue(engine.confirm_whole_house_sensor("aio"))
        self.assertTrue(engine.whole_house_sensor_flags()["aio"]["confirmed_at"])
        engine.confirm_whole_house_sensor("aio", False)
        self.assertIsNone(engine.whole_house_sensor_flags()["aio"]["confirmed_at"])

    def test_a_new_sensor_is_judged_on_its_own_rows(self):
        self.days(5, lambda i: 1.0)                          # unnamed rows: the old sensor's
        engine.check_whole_house_sensors()
        c = self.st._conn
        c.execute("UPDATE device_sensors SET role = 'secondary' WHERE id = 7")
        c.execute("INSERT INTO device_sensors (id, meter_id, channel, entity_id, role, added_at) "
                  "VALUES (9, 'aio', 'import', 'sensor.givtcp_ac_charge_energy_today_kwh', 'primary', '2026-11-01')")
        c.commit()
        out = engine.check_whole_house_sensors()
        self.assertEqual((out["aio"]["n"], out["aio"]["whole_house"]), (0, False))
        self.assertEqual(engine.whole_house_sensor_flags(), {})

    def test_it_runs_with_the_battery_check(self):
        calls = []
        saved = (engine.check_battery_sensors, engine.check_whole_house_sensors, engine._battery_check_at)
        engine.check_battery_sensors = lambda: calls.append("battery")
        engine.check_whole_house_sensors = lambda: calls.append("whole")
        try:
            engine._battery_check_at = None
            engine._maybe_check_battery_sensors(now=100.0)
        finally:
            engine.check_battery_sensors, engine.check_whole_house_sensors, engine._battery_check_at = saved
        self.assertEqual(calls, ["battery", "whole"])


class ThePage(unittest.TestCase):

    def test_one_reply_for_both_checks(self):
        src = _read("web", "server.py")
        self.assertIn('whole = _named(_eng.whole_house_sensor_flags())', src)
        self.assertIn('return jsonify({"flagged": flagged, "whole_house": whole})', src)
        self.assertIn('fn = _eng.confirm_whole_house_sensor if whole else _eng.confirm_battery_sensor', src)
        self.assertIn('("whole_house_sensor_evidence", lambda: store.get_meta("whole_house_sensor_evidence"))', src)

    def test_the_banner_and_the_card(self):
        self.assertIn("((d && d.whole_house) || []).forEach(function (b) {", _read("web", "templates", "base.html"))
        cfg = _read("web", "templates", "meter_config.html")
        self.assertIn("(wholeHouseFlagged[meterId] ? wholeHouseFlagNote(meterId, wholeHouseFlagged[meterId]) : '')", cfg)
        self.assertIn("kind: el.dataset.kind || 'battery'", cfg)

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_the_banner_script_parses(self):
        # a first build dropped the fetch's .catch, check() and its timer: every page's script after it
        # stopped, EMT.notify with it, and no banner showed (seen in the local preview)
        base = _read("web", "templates", "base.html")
        i = base.index("/* ── A battery sensor that counts solar")
        j = base.index("})();", base.index("setInterval(check, 600000);", i)) + 5
        js = base[base.index("(function", i):j].replace("{{ url_for('settings_page') }}", "/settings")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
        try:
            r = subprocess.run(["node", "--check", fh.name], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(fh.name)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(base.index("((d && d.whole_house) || [])", i), j)


if __name__ == "__main__":
    unittest.main()
