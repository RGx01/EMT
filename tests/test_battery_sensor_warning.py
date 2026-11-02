"""
A battery sensor that counts solar, flagged (battery sensor and device split design §3, PR 3; the user,
29 Oct 2026: "fail it loudly" rather than remove it — a wrong verdict on a noisy sensor, or on a v4
import's first start, must not cost history).

When the evidence is conclusive the sensor is flagged and warned of once; nothing in the blocks changes.
The flag shows on every page until the sensor is changed or the user chooses to keep it
anyway. A replacement sensor is judged on the rows that name it: the unnamed rows are the old sensor's.

Discriminating: every test here fails on the parent (the flag, the confirmation and the routes are new).
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine                                    # noqa: E402
import test_battery_sensor_evidence as E         # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAYS = ("2026-06-23", "2026-06-28", "2026-07-01", "2026-07-03", "2026-07-04")


def _t(name):
    with open(os.path.join(ROOT, "web", "templates", name), encoding="utf-8") as f:
        return f.read()


class _Flagged(E._Store):

    def conclusive(self, sensor=None):
        for d in DAYS:
            self.blk(d + "T12:00:00", 0.1, 1.8, sensor=sensor)

    def battery_rows(self):
        return self.st._conn.execute(
            "SELECT block_start, imp_kwh, imp_sensor_id FROM blocks WHERE meter_id = 'house_battery' "
            "ORDER BY block_start").fetchall()

    def new_sensor(self, sid=8):
        c = self.st._conn
        c.execute("UPDATE device_sensors SET role = 'secondary' WHERE id = 7")
        c.execute("INSERT INTO device_sensors (id, meter_id, channel, entity_id, role, added_at) "
                  "VALUES (?, 'house_battery', 'import', 'sensor.battery_from_grid', 'primary', '2026-07-10')",
                  (sid,))
        c.commit()


class TheFlag(_Flagged):

    def test_conclusive_is_flagged_and_nothing_changes(self):
        self.conclusive()
        before = self.battery_rows()
        with self.assertLogs("engine", level="WARNING"):
            engine.check_battery_sensors()
        self.assertEqual(self.battery_rows(), before)
        f = engine.battery_sensor_flags()["house_battery"]
        self.assertEqual((f["conclusive_on"], f["days"], f["confirmed_at"]), ("2026-07-04", 5, None))
        self.assertEqual(f["example"], ["2026-07-04T12:00:00", 1.8, 0.1])

    def test_not_conclusive_is_not_flagged(self):
        for d in DAYS[:4]:
            self.blk(d + "T12:00:00", 0.1, 1.8)
        engine.check_battery_sensors()
        self.assertEqual(engine.battery_sensor_flags(), {})

    def test_confirmed_is_not_warned_and_can_be_taken_back(self):
        self.conclusive()
        self.assertTrue(engine.confirm_battery_sensor("house_battery"))
        with self.assertNoLogs("engine", level="WARNING"):
            engine.check_battery_sensors()
        self.assertIsNotNone(engine.battery_sensor_flags()["house_battery"]["confirmed_at"])
        engine.confirm_battery_sensor("house_battery", False)
        self.assertIsNone(engine.battery_sensor_flags()["house_battery"]["confirmed_at"])


class ANewSensor(_Flagged):

    def test_the_old_sensors_history_does_not_flag_the_new_one(self):
        self.conclusive()                                 # imported history: unnamed rows
        engine.check_battery_sensors()
        self.new_sensor()
        self.assertEqual(engine.battery_sensor_flags(), {})      # the evidence judged the old sensor
        with self.assertNoLogs("engine", level="WARNING"):
            out = engine.check_battery_sensors()
        self.assertEqual((out["house_battery"]["half_hours"], engine.battery_sensor_flags()), (0, {}))

    def test_its_own_rows_can(self):
        self.conclusive()
        engine.check_battery_sensors()
        self.new_sensor()
        for d in ("2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-05"):
            self.blk(d + "T12:00:00", 0.1, 1.8, sensor=8)
        with self.assertLogs("engine", level="WARNING"):
            engine.check_battery_sensors()
        self.assertEqual(engine.battery_sensor_flags()["house_battery"]["conclusive_on"], "2026-08-05")

    def test_a_confirmation_holds_for_its_sensor_only(self):
        self.conclusive(sensor=7)
        engine.confirm_battery_sensor("house_battery")
        self.new_sensor()
        for d in ("2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-05"):
            self.blk(d + "T12:00:00", 0.1, 1.8, sensor=8)
        with self.assertLogs("engine", level="WARNING"):
            engine.check_battery_sensors()
        self.assertIsNone(engine.battery_sensor_flags()["house_battery"]["confirmed_at"])


class TheRoutes(unittest.TestCase):

    FLAGS = {"house_battery": {"conclusive_on": "2026-07-04", "half_hours": 9, "days": 5,
                               "example": ["2026-07-04T11:00:00", 1.8, 0.1], "confirmed_at": None}}

    def test_it_lists_the_flags(self):
        from tests.test_server import make_client
        client = make_client()
        with mock.patch.object(sys.modules["engine"], "battery_sensor_flags", lambda: self.FLAGS, create=True):
            d = json.loads(client.get("/api/battery-sensor/flagged").data)
        self.assertEqual((d["flagged"][0]["meter_id"], d["flagged"][0]["conclusive_on"]),
                         ("house_battery", "2026-07-04"))

    def test_confirm_writes_on_the_engine_thread(self):
        from tests.test_server import make_client
        client = make_client()
        eng, srv = sys.modules["engine"], sys.modules["server"]
        seen = []
        with mock.patch.object(eng, "confirm_battery_sensor", lambda m, yes: seen.append((m, yes)) or True,
                               create=True), \
             mock.patch.object(srv, "_engine_write", lambda op, timeout=30.0: op(None)):
            r = client.post("/api/battery-sensor/confirm", json={"meter_id": "house_battery", "confirmed": False})
        self.assertEqual((r.status_code, seen), (200, [("house_battery", False)]))


class ThePage(unittest.TestCase):

    def test_the_banner_is_loud_and_not_dismissible(self):
        b = _t("base.html")
        js = b[b.index("fetch(apiUrl('/api/battery-sensor/flagged'))"):][:2500]
        self.assertIn("counts solar charging, not only grid charging", js)
        self.assertIn("the device breakdown is wrong", js)
        self.assertIn("Your bill total is not affected", js)
        self.assertNotIn("dismissible", js)

    def test_the_card_says_how_to_fix_it_and_offers_the_confirmation(self):
        m = _t("meter_config.html")
        note = m[m.index("function batteryFlagNote"):][:2500]
        self.assertIn("Keep this sensor anyway", note)
        self.assertIn("you chose to", note)
        self.assertNotIn("grid charge only", note)      # the button claims nothing about the sensor
        self.assertIn("Show the warning again", note)
        self.assertIn("batteryFlagged[meterId] ? batteryFlagNote(", m)

    def test_setup_says_grid_charge_only(self):
        m = _t("meter_config.html")
        self.assertNotIn("use the consumption sensor", m)
        self.assertIn("battery: BATTERY_SENSOR_HINT,", m)
        self.assertIn("type === 'battery' ? BATTERY_SENSOR_HINT", m)

    def test_setup_says_a_clamp_on_the_batterys_cable_counts_solar(self):
        """The Agile-export user's battery is read by a clamp meter on its cable."""
        m = _t("meter_config.html")
        self.assertIn("counts the solar the battery takes as well as the grid", m)
        self.assertEqual(m.count("BATTERY_CLAMP_NOTE"), 4)      # defined; the hint, the card, the requirements


if __name__ == "__main__":
    unittest.main()
