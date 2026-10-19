"""
Device History on the device's own sensors (device sensors design §2, PR 3).

- `POST /api/meter/<id>/history-sensors` sets a device's history sensors — its import channel's
  secondaries in `device_sensors`, in order, its primary untouched (`set_device_history_sensors`).
  Refused for an unknown meter, the main meter and a bad body.
- The Device History page shows the device's live sensor (changed in Settings) and edits its
  history sensors, saved on the device; "Attribute history" saves them, then starts the run, which
  the server runs on the device's sensors.

All new code: on the parent every test fails by name.
"""
import json
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from block_store import BlockStore      # noqa: E402

_PAGE = open(os.path.join(_ROOT, "web", "templates", "historical_probe.html"), encoding="utf-8").read()
CFG = {"meters": {
    "electricity_main": {"meta": {"timezone": "UTC"}, "channels": {"import": {}}},
    "ev_charger": {"meta": {"sub_meter": True, "parent_meter": "electricity_main"},
                   "channels": {"import": {"read": "sensor.ev_now"}}}}}


def _fn(name):
    i = _PAGE.index("function " + name + "(")
    return _PAGE[i:_PAGE.index("\n}\n", i)]


class TheStoreWriter(unittest.TestCase):

    def setUp(self):
        self.st = BlockStore(":memory:")
        self.st.insert_config_period(CFG, effective_from="2026-01-01T00:00:00")

    def test_the_secondaries_become_the_list(self):
        self.assertEqual(self.st.set_device_history_sensors("ev_charger", "import",
                                                            ["sensor.ev_old", "sensor.ev_older"]),
                         ["sensor.ev_old", "sensor.ev_older"])
        self.assertEqual(self.st.set_device_history_sensors("ev_charger", "import", ["sensor.ev_older"]),
                         ["sensor.ev_older"])

    def test_the_primary_is_untouched(self):
        self.st.set_device_history_sensors("ev_charger", "import", ["sensor.ev_old"])
        prim = self.st._conn.execute("SELECT entity_id FROM device_sensors WHERE meter_id='ev_charger' "
                                     "AND role='primary'").fetchall()
        self.assertEqual([r[0] for r in prim], ["sensor.ev_now"])

    def test_the_primary_is_not_a_history_sensor(self):
        self.assertEqual(self.st.set_device_history_sensors("ev_charger", "import",
                                                            ["sensor.ev_now", "sensor.ev_old"]),
                         ["sensor.ev_old"])


class TheRoute(unittest.TestCase):

    def setUp(self):
        from tests.test_server import make_client, server
        self.server = server
        self.st = BlockStore(":memory:")
        self.st.insert_config_period(CFG, effective_from="2026-01-01T00:00:00")
        self.client = make_client(store=self.st)
        self.p = [mock.patch.object(server, "load_config",
                                    lambda: self.st.config_from_db(self.st.get_current_config_period_id())),
                  mock.patch.object(server, "_engine_write", lambda op, timeout=30.0: op(self.st))]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def _post(self, meter, body):
        r = self.client.post("/api/meter/%s/history-sensors" % meter, data=json.dumps(body),
                             content_type="application/json")
        return r.status_code, json.loads(r.data)

    def test_it_saves_on_the_device(self):
        code, d = self._post("ev_charger", {"history_sensors": ["sensor.ev_old"]})
        self.assertEqual((code, d["history_sensors"]), (200, ["sensor.ev_old"]))
        cfg = self.st.config_from_db(self.st.get_current_config_period_id())
        self.assertEqual(cfg["meters"]["ev_charger"]["channels"]["import"]["history_reads"],
                         ["sensor.ev_old"])

    def test_an_unknown_meter_is_404(self):
        self.assertEqual(self._post("nope", {"history_sensors": []})[0], 404)

    def test_the_main_meter_is_refused(self):
        self.assertEqual(self._post("electricity_main", {"history_sensors": ["sensor.x"]})[0], 400)

    def test_a_bad_body_is_refused(self):
        self.assertEqual(self._post("ev_charger", {"history_sensors": "sensor.x"})[0], 400)


class ThePage(unittest.TestCase):

    def test_it_shows_the_live_sensor_read_only(self):
        body = _fn("_deviceSection")
        self.assertIn("Live sensor: <code>", body)
        self.assertIn("changed in Settings", body)
        self.assertNotIn("_ledger", body)
        self.assertNotIn("Sensors from your last reconstruction", _PAGE)

    def test_history_sensors_are_saved_on_the_device(self):
        self.assertIn("'/history-sensors'", _fn("_saveHistory"))

    def test_attribute_saves_then_starts_without_sensor_ids(self):
        self.assertIn("_saveHistory(btn).then(function(){ _startAttribute(meter, false); }",
                      _fn("_attributeDevice"))
        self.assertIn("JSON.stringify({ meter_id: meter, confirm_override: !!override })",
                      _fn("_startAttribute"))

    def test_the_check_probes_primary_then_history(self):
        self.assertIn("var p = dv.getAttribute('data-primary') || '';", _fn("_sensorsOf"))
        self.assertIn("entities: _sensorsOf(dv)", _fn("_runProbe"))


if __name__ == "__main__":
    unittest.main()
