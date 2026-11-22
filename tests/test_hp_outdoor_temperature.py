"""
A heat pump's outdoor temperature (22 Nov 2026, the user): a sensor set on the heat pump in Meter Config
(`outdoor_temp_sensor`, a `meters` column), sampled by the engine with the device's power (`sub_meter_history.temp_c`,
°C — °F and K converted), shown as a gauge beside the heat pump's power gauge and on its today-against-yesterday chart
on a second axis.

Driven by hand in a browser on a scratch copy with a heat pump given the sensor and two days of temperatures: the
gauge beside the power gauge, the dashed line on its own scale, the legend and the hover tip with today's and
yesterday's temperature, the expanded chart's °C scale on the right.

New column, sample, route field, gauge and axis: every test fails on the parent tree.
"""
import copy
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "web"))
sys.path.insert(0, os.path.dirname(__file__))

from block_store import BlockStore                    # noqa: E402
import overview_days as od                            # noqa: E402
from test_block_store import EXAMPLE_CONFIG, new_store  # noqa: E402

PAGE = open(os.path.join(_ROOT, "web", "templates", "live_power.html"), encoding="utf-8").read()
CONFIG_PAGE = open(os.path.join(_ROOT, "web", "templates", "meter_config.html"), encoding="utf-8").read()
ENGINE = open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read()
TZ = "Europe/London"


def _server():
    spec = importlib.util.spec_from_file_location("server_hptemp", os.path.join(_ROOT, "web", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv


def _iso(t):
    return t.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


class TheSetting(unittest.TestCase):

    def test_it_round_trips_and_clears(self):
        store = new_store()
        try:
            cfg = copy.deepcopy(EXAMPLE_CONFIG)
            cfg["meters"]["electricity_main"]["meta"]["outdoor_temp_sensor"] = "sensor.hp_outdoor"
            store.insert_config_period(cfg)
            meta = store.config_from_db(store.get_current_config_period_id())["meters"]["electricity_main"]["meta"]
            self.assertEqual(meta.get("outdoor_temp_sensor"), "sensor.hp_outdoor")
            cfg["meters"]["electricity_main"]["meta"].pop("outdoor_temp_sensor")
            store.insert_config_period(cfg)
            meta = store.config_from_db(store.get_current_config_period_id())["meters"]["electricity_main"]["meta"]
            self.assertNotIn("outdoor_temp_sensor", meta)
        finally:
            store.close()

    def test_an_older_database_gains_both_columns_on_open(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            BlockStore(path).close()
            raw = sqlite3.connect(path)
            raw.execute("ALTER TABLE meters DROP COLUMN outdoor_temp_sensor")
            raw.execute("ALTER TABLE sub_meter_history DROP COLUMN temp_c")
            raw.commit()
            raw.close()
            st = BlockStore(path)
            self.assertIn("outdoor_temp_sensor", {r[1] for r in st._conn.execute("PRAGMA table_info(meters)")})
            self.assertIn("temp_c", {r[1] for r in st._conn.execute("PRAGMA table_info(sub_meter_history)")})
            st.close()
        finally:
            os.remove(path)

    def test_a_sample_keeps_its_temperature(self):
        store = new_store()
        try:
            ts = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            store.append_sub_meter_history(ts, "hp_1", None, 1.2, 6.5)
            store.append_sub_meter_history(ts, "bat_1", 50.0, 1.0)          # a battery's: no temperature
            self.assertEqual(store.get_sub_meter_history("hp_1")[0]["temp_c"], 6.5)
            self.assertIsNone(store.get_sub_meter_history("bat_1")[0]["temp_c"])
        finally:
            store.close()

    def test_meter_config_offers_it_on_a_heat_pump(self):
        self.assertIn("buildEntityInput(meterId + '__outdoor_temp__outdoor_temp_sensor'", CONFIG_PAGE)
        self.assertIn("outdoor_temp_sensor:     { units: ['°C', '°F', 'C', 'F', 'K']", CONFIG_PAGE)
        self.assertIn("} else if (ch === 'outdoor_temp') {", CONFIG_PAGE)

    def test_the_engine_samples_it_with_the_power(self):
        # a check of the source: the tick that samples is not run here
        self.assertIn('temp_s = meta.get("outdoor_temp_sensor")', ENGINE)
        self.assertIn("_store.append_sub_meter_history(now.isoformat(), mid, soc_val, inv_val, temp_val)", ENGINE)


class Celsius(unittest.TestCase):

    def test_conversions(self):
        self.assertEqual(od.to_celsius("21.5", "°C"), 21.5)
        self.assertEqual(od.to_celsius("50", "°F"), 10.0)
        self.assertEqual(od.to_celsius(283.15, "K"), 10.0)
        self.assertEqual(od.to_celsius("7", ""), 7.0)
        self.assertIsNone(od.to_celsius("unavailable", "°C"))
        self.assertIsNone(od.to_celsius(None, "°C"))

    def test_the_temperature_line_is_the_last_reading_in_each_step(self):
        y0, t0, t1 = od.day_bounds(datetime(2026, 11, 22, 12, 0, tzinfo=ZoneInfo(TZ)))
        s = [{"captured_at": _iso(t0 + timedelta(minutes=m)), "temp_c": c, "soc_pct": None}
             for m, c in ((1, 3.0), (4, 3.5), (6, 4.0))]
        self.assertEqual(od.soc_lines(s, y0, t0, t1, t1, key="temp_c")["today"], [[4.0, 3.5], [6.0, 4.0]])


class TheRoute(unittest.TestCase):

    CFG = {"meters": {
        "electricity_main": {"meta": {"timezone": TZ, "billing_day": 1}},
        "hp_1": {"meta": {"sub_meter": True, "device": "Heat pump", "meter_type": "heat_pump",
                          "device_power_sensor": "sensor.hp", "outdoor_temp_sensor": "sensor.outdoor"}},
        "hp_2": {"meta": {"sub_meter": True, "device": "Other", "meter_type": "heat_pump",
                          "device_power_sensor": "sensor.hp2"}}}}

    def test_a_heat_pump_with_the_sensor_has_its_temperature_line(self):
        srv = _server()
        st = BlockStore(os.path.join(tempfile.mkdtemp(), "blocks.db"))
        now = datetime.now(timezone.utc)
        _, t0, _ = od.day_bounds(now.astimezone(ZoneInfo(TZ)))
        st.append_sub_meter_history(_iso(t0 + timedelta(minutes=2)), "hp_1", None, 1.0, 4.5)
        st.append_sub_meter_history(_iso(t0 + timedelta(minutes=2)), "hp_2", None, 1.0, None)
        with mock.patch.object(srv.store_access, "store", lambda: st), \
             mock.patch.object(srv, "load_config", lambda: self.CFG), \
             mock.patch.object(srv, "_config_timezone", lambda: TZ):
            d = srv.app.test_client().get("/api/overview/days").get_json()
        self.assertEqual(d["devices"]["hp_1"]["temp"]["today"], [[2.0, 4.5]])
        self.assertNotIn("temp", d["devices"]["hp_2"])

    def test_the_card_gets_the_temperature_now_in_celsius(self):
        srv = _server()
        ha = mock.MagicMock()
        ha.get_state.side_effect = lambda e: {"sensor.outdoor": "41", "sensor.hp": "1200"}.get(e)
        ha.get_attributes.side_effect = lambda e: {"unit_of_measurement": "°F" if e == "sensor.outdoor" else "W"}
        out = srv._build_soc_response({"hp_1": {"type": "heat_pump", "temp_entity": "sensor.outdoor",
                                                "power_entity": "sensor.hp"}}, ha)
        self.assertEqual(out["hp_1"]["temp_c"], 5.0)
        self.assertEqual(out["hp_1"]["temp_entity"], "sensor.outdoor")


class ThePage(unittest.TestCase):

    def test_the_gauge_sits_beside_the_power_gauge(self):
        i = PAGE.index("function buildSocCard(")
        body = PAGE[i:PAGE.index("\n}\n", i)]
        self.assertIn("if (isHeatPump && info.temp_entity) {", body)
        self.assertIn("'<div class=\"dev-gauges\">' + battSection + _invHtml + pvGauge + tempGauge + '</div>'", body)
        self.assertIn("setTempGauge(mid, info.temp_c);", PAGE)


@unittest.skipUnless(shutil.which("node"), "node not available")
class ThePure(unittest.TestCase):

    def _js(self, expr):
        def fn(n):
            i = PAGE.index("function %s(" % n)
            return PAGE[i:PAGE.index("\n}\n", i) + 2]
        src = "var TEMP_LO = -10, TEMP_HI = 30;\n" + "\n".join(fn(n) for n in ("_tdFmt", "_tdSeries", "_tdY2Range", "_tempFrac"))
        src += "\nconsole.log(JSON.stringify(%s));" % expr
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(src)
        try:
            r = subprocess.run(["node", fh.name], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(fh.name)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_the_temperature_goes_on_a_second_axis(self):
        d = "{devices:{h:{out:false,energy:{today:{'in':[[0,0]]},yesterday:{}},temp:{today:[[5,3.5]],yesterday:[[5,8]]}}}}"
        self.assertEqual(self._js("_tdSeries(%s,'device','h','energy').y2" % d),
                         {"name": "temp", "unit": "°C", "today": [[5, 3.5]], "yesterday": [[5, 8]]})

    def test_the_second_axis_range(self):
        self.assertEqual(self._js("_tdY2Range({today:[[0,3.4]],yesterday:[[0,8.1]]})"), [2, 10])
        self.assertEqual(self._js("_tdY2Range({today:[[0,5]],yesterday:[]})"), [3, 7])     # at least 4 across
        self.assertIsNone(self._js("_tdY2Range({today:[],yesterday:[]})"))

    def test_the_gauge_and_the_legend(self):
        self.assertEqual(self._js("[_tempFrac(-15), _tempFrac(10), _tempFrac(40), _tdFmt(3.46,'°C')]"),
                         [0, 0.5, 1, "3.5°C"])


if __name__ == "__main__":
    unittest.main()
