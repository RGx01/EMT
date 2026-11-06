"""
A device with no read sensor (6 Nov 2026, the user: a battery whose inverter reports no grid charge).

It could always be saved with the sensor empty, and the Overview's cards read only its power and SoC sensors.
But emptying the sensor on an existing device left two faults. The "counts solar" flag stayed for good, as old
rows name no sensor and so still counted, and *Keep this sensor anyway* does nothing with no sensor. And what
the wrong sensor recorded could not be removed: Replace history refuses with no sensor, and Remove takes the
device. So:

- neither sensor check judges a device with no primary sensor;
- **Clear history** (`/api/historical/attribute/clear`, `_backout_impl(whole=True)`) drops everything the device
  holds, its kept readings included, or a later refill would rebuild it;
- emptying the sensor skips the change dialog, whose answers are all about a new sensor.

Discriminating: the tests not marked GUARD fail on the parent. The GUARDs pin what was already so, among them a
half-hour for such a device, and its Overview card.
"""
import asyncio
import os
import sys
import unittest
from unittest import mock
from unittest.mock import MagicMock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import engine                                        # noqa: E402
import job_coordinator as C                          # noqa: E402
from block_store import BlockStore                   # noqa: E402
from tests.test_battery_sensor_evidence import _Store   # noqa: E402
from tests.test_device_sensors_storage import _cfg, _save   # noqa: E402
from tests.test_replace_device_history import HALF, WRONG, _household   # noqa: E402

PAGE = open(os.path.join(_ROOT, "web", "templates", "meter_config.html"), encoding="utf-8").read()
BASE = open(os.path.join(_ROOT, "web", "templates", "base.html"), encoding="utf-8").read()
HELP = open(os.path.join(_ROOT, "web", "templates", "help", "settings.html"), encoding="utf-8").read()
DAYS = ("2026-06-23", "2026-06-28", "2026-07-01", "2026-07-03", "2026-07-04")


class TheChecks(_Store):

    def _flag_then_remove(self):
        for d in DAYS:
            self.blk(d + "T12:00:00", 0.1, 1.8)
        with self.assertLogs("engine", level="WARNING"):
            engine.check_battery_sensors()
        self.assertEqual(list(engine.battery_sensor_flags()), ["house_battery"])
        with self.st._conn:
            self.st._sync_device_sensors("house_battery", "import", "", keep_old=False)

    def test_GUARD_the_banner_clears_as_soon_as_the_sensor_goes(self):
        # (it did on the parent too, only until the next hourly check brought it back: the test below)
        self._flag_then_remove()
        self.assertEqual(engine.battery_sensor_flags(), {})

    def test_the_next_check_finds_nothing_to_judge(self):
        self._flag_then_remove()
        with self.assertNoLogs("engine", level="WARNING"):
            out = engine.check_battery_sensors()
        self.assertEqual((out["house_battery"]["sensor_id"], out["house_battery"]["half_hours"],
                          out["house_battery"]["conclusive_on"]), (None, 0, None))
        self.assertEqual(engine.battery_sensor_flags(), {})

    def test_nor_is_it_judged_as_reading_the_whole_house(self):
        self._flag_then_remove()
        c = self.st._conn
        for i in range(250):
            s = "2026-07-0%dT%02d:%02d:00" % (1 + i // 48, (i % 48) // 2, 30 * (i % 2))
            c.execute("INSERT OR REPLACE INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh) "
                      "VALUES (?, ?, 'electricity_main', 1, 2.0)", (s, s))
            c.execute("INSERT OR REPLACE INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh) "
                      "VALUES (?, ?, 'house_battery', 1, 2.0)", (s, s))
        c.commit()
        with self.assertNoLogs("engine", level="WARNING"):
            out = engine.check_whole_house_sensors()
        self.assertEqual((out["house_battery"]["sensor_id"], out["house_battery"]["whole_house"]), (None, False))
        self.assertEqual(engine.whole_house_sensor_flags(), {})

    def test_GUARD_a_device_with_a_sensor_is_still_judged(self):
        for d in DAYS:
            self.blk(d + "T12:00:00", 0.1, 1.8)
        with self.assertLogs("engine", level="WARNING"):
            engine.check_battery_sensors()
        self.assertEqual(list(engine.battery_sensor_flags()), ["house_battery"])


class TheClear(unittest.TestCase):

    def setUp(self):
        self.st = _household()
        with self.st._conn:      # an imported row, which a replace would leave
            self.st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
                                  "source) VALUES ('2026-10-01T09:30:00', '2026-10-01T10:00:00', 'ev_charger', 1, 0.5, "
                                  "'imported_csv')")
        self.p = [mock.patch.object(engine, "get_store", lambda: self.st), mock.patch.object(engine, "_store", self.st)]
        for x in self.p:
            x.start()
        C.reset_for_tests()
        engine._backout_job.clear()
        engine._backout_job["status"] = "idle"

    def tearDown(self):
        for x in self.p:
            x.stop()
        C.reset_for_tests()

    def _empty_the_sensor(self):
        cfg = _cfg(ev_read="")
        cfg["meters"]["ev_charger"]["channels"]["import"]["keep_old_read"] = False
        _save(self.st, cfg)

    def _clear(self):
        return asyncio.run(engine.backout_recorder_attribution_async(meter_id="ev_charger", whole=True))

    def _count(self, table="blocks"):
        return self.st._conn.execute("SELECT COUNT(*) FROM %s WHERE meter_id='ev_charger'" % table).fetchone()[0]

    def test_everything_the_device_holds_goes(self):
        self._empty_the_sensor()
        self.assertGreater(self._count(), 0)
        self.assertGreater(self._count("ha_reads"), 0)
        res = self._clear()
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["deleted"], self._count(), self._count("ha_reads")), (5, 0, 0))

    def test_the_house_gets_it_back_and_its_meter_is_untouched(self):
        self._empty_the_sensor()
        before = self.st._conn.execute("SELECT COUNT(*), SUM(imp_kwh) FROM blocks WHERE meter_id='electricity_main'"
                                       ).fetchone()[:]
        self.assertGreater(self._clear()["recomputed"], 0)
        self.assertEqual(self.st._conn.execute("SELECT COUNT(*), SUM(imp_kwh) FROM blocks "
                                               "WHERE meter_id='electricity_main'").fetchone()[:], before)
        for s in HALF:
            b = self.st.get_block_dict_by_start(s)
            self.assertNotIn("ev_charger", b["meters"])

    def test_the_device_stays(self):
        self._empty_the_sensor()
        self._clear()
        self.assertIn("ev_charger", [m for m, _s in self.st.device_primary_sensors()])

    def test_a_refill_cannot_bring_it_back(self):
        self._empty_the_sensor()
        self._clear()
        self.assertEqual(self.st.ha_read_devices("2026-10-01T00:00:00", "2026-10-02T00:00:00"), [])
        self.assertEqual(self.st.device_stitch_sensors("ev_charger"), [])
        self.assertEqual(self.st.get_attribution_runs(), [])
        # the old sensor's entry goes too: nothing names it now
        self.assertEqual(self.st._conn.execute("SELECT COUNT(*) FROM device_sensors WHERE entity_id=?",
                                               (WRONG,)).fetchone()[0], 0)

    def test_refused_while_it_has_a_sensor(self):
        res = self._clear()
        self.assertEqual(res, {"ok": False, "error": "has_sensor"})
        self.assertEqual(self._count(), 5)

    def test_refused_with_no_device_named(self):
        res = asyncio.run(engine.backout_recorder_attribution_async(whole=True))
        self.assertEqual(res["error"], "has_sensor")

    def test_GUARD_a_replace_still_leaves_imported_rows(self):
        self.st.drop_device_history("ev_charger", [])
        self.assertEqual(self.st._conn.execute("SELECT COUNT(*) FROM blocks WHERE meter_id='ev_charger' AND "
                                               "source='imported_csv'").fetchone()[0], 1)


class TheRoute(unittest.TestCase):

    def setUp(self):
        from tests.test_server import make_client, server
        self.server, self.client = server, make_client()

    def _post(self, read="", history=(), sub=True, backup=None, **eng_over):
        started = []
        eng = sys.modules["engine"]
        cfg = {"meters": {"bat": {"meta": {"sub_meter": sub},
                                  "channels": {"import": {"read": read, "history_reads": list(history)}}}}}

        def _start(coro, loop):
            started.append(coro)
            return MagicMock()
        over = dict({"attribution_running": lambda: False, "api_import_running": lambda: False,
                     "backout_running": lambda: False, "api_backout_status": lambda: {},
                     "backout_forget_last": lambda: None,
                     "backout_recorder_attribution_async": lambda **k: ("clear", k)}, **eng_over)
        ps = [mock.patch.object(eng, k, v, create=True) for k, v in over.items()]
        for p in ps:
            p.start()
        try:
            with mock.patch.object(self.server, "load_config", lambda: cfg), \
                 mock.patch.object(self.server, "_event_loop", MagicMock(is_running=lambda: True)), \
                 mock.patch("asyncio.run_coroutine_threadsafe", _start), \
                 mock.patch.object(self.server, "_create_backup_zip", backup or (lambda label: "/tmp/b-" + label + ".zip")):
                r = self.client.post("/api/historical/attribute/clear", json={"meter_id": "bat"})
        finally:
            for p in ps:
                p.stop()
        return r, started

    def test_it_backs_up_then_starts_the_whole_clear(self):
        r, started = self._post()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["backup"], "b-pre-clear-history.zip")
        self.assertEqual(started, [("clear", {"meter_id": "bat", "whole": True})])

    def test_refused_with_a_read_sensor(self):
        r, started = self._post(read="sensor.x")
        self.assertEqual((r.status_code, started), (409, []))

    def test_refused_with_a_history_sensor(self):
        r, started = self._post(history=["sensor.old"])
        self.assertEqual((r.status_code, started), (409, []))

    def test_refused_for_the_main_meter(self):
        r, started = self._post(sub=False)
        self.assertEqual((r.status_code, started), (400, []))

    def test_refused_during_an_import_a_rebuild_or_a_removal(self):
        for k in ("api_import_running", "attribution_running", "backout_running"):
            r, started = self._post(**{k: lambda: True})
            self.assertEqual((r.status_code, started), (409, []), k)

    def test_no_backup_no_clear(self):
        def boom(label):
            raise OSError("disk full")
        r, started = self._post(backup=boom)
        self.assertEqual((r.status_code, started), (500, []))


META = {"sub_meter": False, "block_minutes": 30, "timezone": "UTC", "billing_day": 1,
        "currency_symbol": "£", "currency_code": "GBP"}
BAT = {"sub_meter": True, "parent_meter": "electricity_main", "meter_type": "battery", "device": "Batt",
       "soc_sensor": "sensor.soc", "inverter_power_sensor": "sensor.inv"}
CFG = {"meters": {"electricity_main": {"meta": META, "channels": {
           "import": {"read": "sensor.imp", "rate": "sensor.rate"}}},
       "bat": {"meta": BAT, "channels": {"import": {"read": "", "rate": ""}}}}}
CB = {"start": "2026-10-20T13:00:00", "end": "2026-10-20T13:30:00", "meters": {
    "electricity_main": {"meta": META, "channels": {"import": {
        "reads": [{"ts": "2026-10-20T13:00:00", "value": 1000.0}, {"ts": "2026-10-20T13:30:00", "value": 1002.0}],
        "rates": [{"ts": "2026-10-20T12:00:00", "value": 0.25}]}}},
    "bat": {"meta": BAT, "channels": {"import": {"reads": [], "rates": []}}}}}


class AHalfHour(unittest.TestCase):
    """Already so, and not confirmed until this ran."""

    def _finalise(self):
        s = BlockStore(":memory:")
        s.insert_config_period(CFG)
        with mock.patch.object(engine, "_store", s), \
             mock.patch.object(engine, "get_store", lambda: s), \
             mock.patch.object(engine, "load_config", lambda: CFG), \
             mock.patch.object(engine, "load_json", lambda path, default=None: CFG if "config" in str(path) else (default or {})), \
             mock.patch.object(engine, "_backup_to_share", lambda: None), \
             mock.patch.object(engine, "get_data_source_mode", lambda: "cad"), \
             mock.patch.object(engine, "read_sensor", lambda ha, eid: None), \
             mock.patch.object(engine, "_kraken_standing_resolver", lambda ts: None), \
             mock.patch.object(engine, "_kraken_rate_resolver", lambda ch, ts: None):
            engine.finalise_block(MagicMock(), block_data=CB)
        return s

    def test_GUARD_all_of_it_counts_in_the_house(self):
        s = self._finalise()
        main = s._conn.execute("SELECT imp_kwh FROM blocks WHERE meter_id='electricity_main'").fetchone()[0]
        self.assertAlmostEqual(main, 2.0)
        dev = s._conn.execute("SELECT COALESCE(imp_kwh, 0), COALESCE(imp_kwh_grid, 0) FROM blocks WHERE meter_id='bat'"
                              ).fetchone()
        self.assertEqual(tuple(dev or (0, 0)), (0, 0))


class TheOverview(unittest.TestCase):

    def test_GUARD_its_card_needs_no_read_sensor(self):
        from tests.test_server import make_client, server
        client = make_client()
        with mock.patch.object(server, "load_config", lambda: CFG):
            server._ha_client.get_state.side_effect = lambda e: {"sensor.soc": "55", "sensor.inv": "1200"}.get(e)
            server._ha_client.get_attributes.return_value = {"unit_of_measurement": "W"}
            d = client.get("/api/power").get_json()
        self.assertEqual(d["soc_sensors"]["bat"]["soc"], 55.0)
        self.assertAlmostEqual(d["soc_sensors"]["bat"]["power_kw"], 1.2)


class ThePage(unittest.TestCase):

    def test_emptying_the_sensor_asks_nothing(self):
        i = PAGE.index("function _renamedDevices(")
        body = PAGE[i:PAGE.index("var _replaceAfterSave")]
        self.assertIn("if (!(imp.read || '').trim()) { imp.keep_old_read = false; return; }", body)

    def test_no_sensor_shows_clear_instead_of_rebuild(self):
        self.assertIn("var noSensor = !_histSensors(m).length && !_histUnsaved(m);", PAGE)
        self.assertIn('js-hist-clear" data-meter="', PAGE)
        self.assertIn("if (el.classList.contains('js-hist-clear'))", PAGE)
        self.assertIn("apiUrl('/api/historical/attribute/clear')", PAGE)

    def test_the_wording_says_it_keeps_its_card(self):
        self.assertNotIn("leave the battery out", PAGE)
        self.assertNotIn("Leave the battery out", PAGE)
        self.assertIn("leave this blank: the battery keeps its Overview card", PAGE)
        self.assertIn("empty the Read Sensor and save", PAGE)
        self.assertIn("or empty its read sensor if your inverter has none", BASE)
        self.assertIn("No right sensor to move to?", HELP)


if __name__ == "__main__":
    unittest.main()
