"""
Device History on the device's own sensors (device sensors design §2, PR 3).

- `POST /api/meter/<id>/history-sensors` sets a device's history sensors — its import channel's
  secondaries in `device_sensors`, in order, its primary untouched (`set_device_history_sensors`).
  Refused for an unknown meter, the main meter and a bad body.
- The Device History page that edited them retired in PR 5 (19 Oct 2026): checking, rebuilding and
  removing a device's history are on its card in Settings (`TheCard`).

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

CFG = {"meters": {
    "electricity_main": {"meta": {"timezone": "UTC"}, "channels": {"import": {}}},
    "ev_charger": {"meta": {"sub_meter": True, "parent_meter": "electricity_main"},
                   "channels": {"import": {"read": "sensor.ev_now"}}}}}


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


class TheCard(unittest.TestCase):
    """PR 5 (19 Oct 2026): the Device History page retired; checking a device's sensors,
    rebuilding its history and removing it are on the device's card in Settings."""

    @staticmethod
    def _src():
        return open(os.path.join(_ROOT, "web", "templates", "meter_config.html"), encoding="utf-8").read()

    def _fn(self, name):
        s = self._src(); i = s.index("function " + name + "(")
        return s[i:s.index("\n}\n", i)]

    def test_every_device_card_has_its_history(self):
        self.assertIn("buildHistorySection(meterId, !!meta.retired_at);", self._fn("buildSubCard"))
        body = self._fn("buildHistorySection")
        for c in ("js-hist-check", "js-hist-rebuild", "js-hist-layer", "js-hist-progress"):
            self.assertIn(c, body)

    def test_check_and_rebuild_use_the_saved_sensors(self):
        self.assertIn("if (_histUnsaved(m))", self._fn("_histCheck"))
        self.assertIn("if (_histUnsaved(m))", self._fn("_histRebuild"))
        self.assertIn("_serverConfig.meters", self._fn("_histSensors"))
        self.assertIn("'/api/historical/probe'", self._fn("_histCheck"))

    def test_a_sanity_refusal_offers_rebuild_anyway(self):
        body = self._fn("_histRebuild")
        self.assertIn("if (d.sanity)", body)
        self.assertIn("js-hist-override", body)
        self.assertIn("confirm_override: !!override", body)

    def test_remove_backs_out_the_device(self):
        self.assertIn("'/api/historical/attribute/backout'", self._fn("_histRemoveLayer"))
        self.assertIn("JSON.stringify({meter_id: m})", self._fn("_histRemoveLayer"))

    def test_progress_and_control_are_per_device(self):
        self.assertIn("body: JSON.stringify({action: el.dataset.act, meter_id: el.dataset.meter})",
                      self._src())
        self.assertIn("_hist.timer = setTimeout(_loadHistoryState, 2000)", self._fn("_loadHistoryState"))

    def test_the_page_is_gone_and_its_address_goes_to_settings(self):
        self.assertFalse(os.path.exists(os.path.join(_ROOT, "web", "templates", "historical_probe.html")))
        from tests.test_server import make_client
        r = make_client().get("/historical-probe")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/settings"))


if __name__ == "__main__":
    unittest.main()
