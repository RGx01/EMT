"""
The Overview's today-against-yesterday lines (overview rows design, part 2, 22 Nov 2026, the user): the route
`/api/overview/days`, the battery cards' SoC / energy choice (`overview_battery_view`), and the page's charts.

The route runs on a real BlockStore at the real time: the device samples are read back by age
(`get_sub_meter_history`), so the data is laid out from now.

Driven by hand in a browser on a copy of a real database with a battery, a V2X car and a heat pump added and two
days of synthetic power samples: the four cards drew today over yesterday, the battery switched to kWh and back,
and the Power row kept its height (244.8 px before and after at 1,400 px).

New route, setting and charts: every test fails on the parent tree.
"""
import importlib.util
import json
import os
import shutil
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

from block_store import BlockStore               # noqa: E402
import overview_days as od                       # noqa: E402

PAGE = open(os.path.join(_ROOT, "web", "templates", "live_power.html"), encoding="utf-8").read()
TZ = "Europe/London"
CFG = {"meters": {
    "electricity_main": {"meta": {"timezone": TZ, "currency_symbol": "£", "billing_day": 1,
                                  "power_sensor": "sensor.grid"}},
    "battery_1": {"meta": {"sub_meter": True, "device": "Battery", "meter_type": "battery",
                           "soc_sensor": "sensor.soc", "inverter_power_sensor": "sensor.inv"}},
    "car_1": {"meta": {"sub_meter": True, "device": "Car", "meter_type": "ev",
                       "device_power_sensor": "sensor.car", "v2x_capable": True}},
    "hp_1": {"meta": {"sub_meter": True, "device": "Heat pump", "meter_type": "heat_pump",
                      "device_power_sensor": "sensor.hp"}},
    "plug_1": {"meta": {"sub_meter": True, "device": "Plug"}}}}       # no sensor: no card, no line


def _server():
    spec = importlib.util.spec_from_file_location("server_tdays", os.path.join(_ROOT, "web", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv


def _iso(t):
    return t.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


class TheRoute(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv = _server()
        cls.st = BlockStore(os.path.join(tempfile.mkdtemp(), "blocks.db"))
        now = datetime.now(timezone.utc)
        cls.y0, cls.t0, _ = od.day_bounds(now.astimezone(ZoneInfo(TZ)))
        c = cls.st._conn
        c.execute("PRAGMA foreign_keys=OFF")
        c.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, timezone) "
                  "VALUES (1, '2026-01-01T00:00:00', 1, 30, ?)", (TZ,))
        c.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter) VALUES (1, 'electricity_main', 0)")
        ins = ("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, exp_kwh, "
               "is_provisional, interpolated) VALUES (?, ?, 'electricity_main', 1, ?, ?, 0, 0)")
        for day0, imp in ((cls.y0, 1.0), (cls.t0, 0.5)):               # each day's first half-hour
            c.execute(ins, (_iso(day0), _iso(day0 + timedelta(minutes=30)), imp, 0.2))
        # a battery charging at 2 kW for 30 minutes from yesterday's 01:00, the car out at 3 kW for 10 minutes
        smp = "INSERT INTO sub_meter_history (captured_at, meter_id, soc_pct, inverter_kw) VALUES (?, ?, ?, ?)"
        for i in range(61):
            t = cls.y0 + timedelta(hours=1, seconds=30 * i)
            c.execute(smp, (_iso(t), "battery_1", 50.0 + i * 0.1, 2.0))
        for i in range(21):
            c.execute(smp, (_iso(cls.y0 + timedelta(hours=2, seconds=30 * i)), "car_1", None, -3.0))
        c.commit()

    def _get(self):
        with mock.patch.object(self.srv.store_access, "store", lambda: self.st), \
             mock.patch.object(self.srv, "load_config", lambda: CFG), \
             mock.patch.object(self.srv, "_config_timezone", lambda: TZ):
            r = self.srv.app.test_client().get("/api/overview/days")
        self.assertEqual(r.status_code, 200, r.data[:300])
        return r.get_json()

    def test_the_main_meter_from_its_half_hours(self):
        m = self._get()["main"]
        self.assertEqual(m["yesterday"]["imp"], [[0, 0.0], [30.0, 1.0]])
        self.assertEqual(m["today"]["exp"][:2], [[0, 0.0], [30.0, 0.2]])

    def test_a_battery_in_and_out_from_its_power_sensor_and_its_soc(self):
        b = self._get()["devices"]["battery_1"]
        self.assertEqual((b["source"], b["out"]), ("power", True))
        self.assertAlmostEqual(b["energy"]["yesterday"]["in"][-1][1], 1.0, places=3)
        self.assertEqual(b["soc"]["yesterday"][-1][1], 56.0)

    def test_a_v2x_car_has_out(self):
        car = self._get()["devices"]["car_1"]
        self.assertTrue(car["out"])
        self.assertAlmostEqual(car["energy"]["yesterday"]["out"][-1][1], 0.5, places=3)

    def test_a_heat_pump_is_in_only(self):
        hp = self._get()["devices"]["hp_1"]
        self.assertEqual((hp["out"], sorted(hp["energy"]["today"])), (False, ["in"]))

    def test_a_device_without_a_card_has_no_line(self):
        self.assertNotIn("plug_1", self._get()["devices"])

    def test_the_days_lengths_and_now(self):
        d = self._get()
        self.assertIn(d["day_min"]["yesterday"], (1380.0, 1440.0, 1500.0))
        self.assertTrue(0 <= d["now_min"] <= d["day_min"]["today"])


class _Store:
    def __init__(self):
        self.saved = {}

    def get_settings(self):
        return dict(self.saved)

    def save_settings(self, s):
        self.saved = s


class TheBatteryView(unittest.TestCase):

    def setUp(self):
        self.srv = _server()
        self.store = _Store()
        for p in (mock.patch.object(self.srv.store_access, "store", return_value=self.store),
                  mock.patch.object(self.srv, "_engine_write", side_effect=lambda f: f(self.store))):
            p.start()
            self.addCleanup(p.stop)
        self.client = self.srv.app.test_client()

    def _post(self, val):
        return self.client.post("/api/settings", data=json.dumps({"overview_battery_view": val}),
                                content_type="application/json")

    def test_it_saves_and_reads_back(self):
        self.assertEqual(self._post({"device-battery_1": "energy"}).status_code, 200)
        self.assertEqual(self.client.get("/api/settings").get_json()["overview_battery_view"],
                         {"device-battery_1": "energy"})

    def test_a_bad_view_or_key_is_refused(self):
        for bad in ({"device-battery_1": "pie"}, {'a"]{}': "soc"}, ["soc"], "soc"):
            with self.subTest(bad=str(bad)):
                self.assertEqual(self._post(bad).status_code, 400)
                self.assertNotIn("overview_battery_view", self.store.saved)


class ThePage(unittest.TestCase):

    def test_the_live_power_card_has_its_chart(self):
        i = PAGE.index('<div class="power-card expandable-card" data-ov-card="power">')
        self.assertIn('<div class="td" id="td-power" data-td-kind="main"></div>', PAGE[i:i + 3000])

    def test_a_device_card_has_its_chart_and_a_battery_with_soc_its_switch(self):
        i = PAGE.index("function buildSocCard(")
        body = PAGE[i:PAGE.index("\n}\n", i)]
        self.assertIn("data-td-kind=\"device\" data-td-soc=\"' + (isBatt && info.soc_entity ? '1' : '')", body)

    def test_a_chart_answers_the_pointer(self):
        i = PAGE.index("function _tdRender(")
        body = PAGE[i:PAGE.index("\n}\n", i)]
        self.assertIn("cv0.addEventListener('pointermove', function(e){ _tdHover(el, e); });", body)
        self.assertIn('<div class="td-tip"></div>', body)

    def test_an_expanded_chart_names_its_card(self):
        i = PAGE.index("function _expandPower(")
        self.assertIn("td.setAttribute('data-td-mid', src.id.replace(/^td-/, ''));", PAGE[i:PAGE.index("\n}\n", i)])

    def test_expanded_the_gauges_are_one_size(self):
        # 22 Nov 2026, the user: "some of the expanded views don't present the gauges and info in a tidy aligned
        # manner" — the card's sizes applied in the Power row only, so the overlay mixed the old and the new
        self.assertIn(".expand-modal .dev-gauge .inv-gauge-wrap, .expand-modal .dev-gauge .pv-gauge-wrap, "
                      ".expand-modal .dev-gauge .gauge-wrap {\n  width: 150px; max-width: 150px; height: 84px;", PAGE)

    def test_expanded_the_scale_is_not_cut(self):
        i = PAGE.index("function _tdDraw(")
        body = PAGE[i:PAGE.index("\n}\n", i)]
        self.assertIn("padT = big ? 12 : 2;", body)                    # room above for the top label
        self.assertIn("padL = Math.max(40, Math.ceil(Math.max.apply(null, scale.map(function(x){ return g.measureText(x[1]).width; }))) + 12);", body)
        self.assertIn("return padL;", body)
        i = PAGE.index("function _tdHover(")
        self.assertIn("padL = st.padL || 2;", PAGE[i:PAGE.index("\n}\n", i)])   # the hover maps through the same margin

    def test_a_new_device_card_draws_its_chart(self):
        i = PAGE.index("container.appendChild(tmp.firstChild);")
        self.assertIn("_tdRender(document.getElementById('td-' + mid));", PAGE[i:i + 200])


@unittest.skipUnless(shutil.which("node"), "node not available")
class ThePure(unittest.TestCase):

    def _js(self, expr):
        def fn(n):
            i = PAGE.index("function %s(" % n)
            return PAGE[i:PAGE.index("\n}\n", i) + 2]
        src = "\n".join(fn(n) for n in ("_tdAt", "_tdFmt", "_tdSeries", "_tdClock"))
        src += "\nconsole.log(JSON.stringify(%s));" % expr
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(src)
        try:
            r = subprocess.run(["node", fh.name], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(fh.name)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_yesterday_by_this_time(self):
        self.assertEqual(self._js("[_tdAt([[0,0],[30,1],[60,3]],45), _tdAt([[0,0],[30,1]],500), _tdAt([],5)]"),
                         [2, 1, None])

    def test_a_battery_shows_its_soc_or_its_energy(self):
        d = ("{devices:{b:{out:true,energy:{today:{'in':[[0,0]],out:[[0,0]]},yesterday:{}},"
             "soc:{today:[[1,50]],yesterday:[]}}}}")
        self.assertEqual(self._js("_tdSeries(%s,'device','b','soc')" % d),
                         {"unit": "%", "top": 100,
                          "lines": [{"name": "soc", "today": [[1, 50]], "yesterday": []}]})
        self.assertEqual([x["name"] for x in self._js("_tdSeries(%s,'device','b','energy')" % d)["lines"]],
                         ["in", "out"])

    def test_what_leaves_is_written_negative(self):
        # export and out are drawn below zero and written so (22 Nov 2026, the user)
        self.assertEqual(self._js("[_tdFmt(1.5,'kWh',true), _tdFmt(0,'kWh',true), _tdFmt(1.5,'kWh',false), _tdFmt(40,'%',true)]"),
                         ["−1.50", "0.00", "1.50", "40%"])

    def test_the_pointers_time(self):
        self.assertEqual(self._js("[_tdClock(0), _tdClock(605.5), _tdClock(1439), _tdClock(1440)]"),
                         ["00:00", "10:05", "23:59", "00:00"])

    def test_a_card_with_no_data_has_no_chart(self):
        self.assertIsNone(self._js("_tdSeries({devices:{}},'device','gone','energy')"))


if __name__ == "__main__":
    unittest.main()
