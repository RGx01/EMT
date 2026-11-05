"""
An import rebuilds a new device's history from Home Assistant, unasked (4 Nov 2026, the user, a scratch
setup; device history on first import design).

A first setup on Octopus's API saved its devices, offered the history import, and the user took it —
leaving Setup for Data Management. The devices' rebuild offer waited for Setup to reload, so it came
only when the user went back there, and the import's own device step found no `ha_reads` and rebuilt
nothing ("no device reads kept there"). Now an import (Octopus's or a CSV, not a gap fill) rebuilds
each device whose history EMT never had (`devices_without_history`) from HA's recorder, whole, once
the import is done — after the same sensor check *Rebuild history* makes; a sensor that fails it is
held on the device's card, nothing written.

Discriminating: TheDevices' four, TheImport's first two, the callers' first two and the page's two fail
on the parent (no `devices_without_history`, no `fetch_new`). The GUARDS pin what stays as it was: a
gap fill's (both pass on the parent); a retired device, and a device with history rebuilt from its
reads as before (these two call the new query and argument, so cannot run on the parent).
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import engine                                       # noqa: E402
import job_coordinator as C                         # noqa: E402
from tests.test_device_sensors_storage import _store            # noqa: E402
from tests.test_rebuild_from_ha_reads import H, S1, S2, _main, _ev   # noqa: E402

PAGE = open(os.path.join(_ROOT, "web", "templates", "meter_config.html"), encoding="utf-8").read()
END = "2026-10-01T11:00:00"


def _live(st, start, meter="ev_charger", kwh=0.4):
    """A half-hour HA recorded live: its `ha_reads` row and the device's block."""
    with st._conn:
        st._conn.execute("INSERT INTO ha_reads (meter_id, channel, origin, period_start, minutes, "
                         "sensor_id, kwh, recorded_at) VALUES (?, 'import', 'live', ?, 30, NULL, ?, "
                         "'2026-11-04T19:30:00')", (meter, start, kwh))
    _row(st, start, meter, kwh)


def _row(st, start, meter="ev_charger", kwh=0.4, source=None):
    with st._conn:
        st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                         "imp_kwh, source) VALUES (?, ?, ?, ?, ?, ?)",
                         (start, start, meter, st.get_current_config_period_id(), kwh, source))


class FakeHA:
    def __init__(self, kwh):
        self.kwh = kwh

    async def get_statistics(self, ids, start, end, period="hour", timeout=0):
        return {i: [{"start": H + "+00:00", "change": self.kwh}] for i in ids}


class TheDevices(unittest.TestCase):

    def setUp(self):
        self.st = _store()

    def test_a_new_device_has_none(self):
        self.assertEqual(self.st.devices_without_history(), ["ev_charger"])

    def test_nor_once_it_records_live(self):
        _live(self.st, S2)
        self.assertEqual(self.st.devices_without_history(), ["ev_charger"])

    def test_one_asked_of_ha_before_has_some(self):
        self.st.record_recorder_hours("ev_charger", "import", {H: 2.0})
        self.assertEqual(self.st.devices_without_history(), [])

    def test_one_with_rows_before_it_recorded_live_has_some(self):
        # history from v4, or anywhere else: a device row older than its first live half-hour
        _row(self.st, S1)
        _live(self.st, S2)
        self.assertEqual(self.st.devices_without_history(), [])

    def test_GUARD_a_retired_device_is_left(self):
        with self.st._conn:
            self.st._conn.execute("UPDATE meters SET retired_at = '2026-10-01' "
                                  "WHERE meter_id = 'ev_charger'")
        self.assertEqual(self.st.devices_without_history(), [])


class TheImport(unittest.TestCase):

    def setUp(self):
        self.st = _store()
        self.st.append_block(_main(S1, 3.0))            # the import: the house only
        self.st.append_block(_main(S2, 3.0))
        self.p = [mock.patch.object(engine, "get_store", lambda: self.st),
                  mock.patch.object(engine, "_store", self.st)]
        for x in self.p:
            x.start()
        C.reset_for_tests()
        engine._attribution_jobs.pop("ev_charger", None)

    def tearDown(self):
        for x in self.p:
            x.stop()
        C.reset_for_tests()
        engine._attribution_jobs.pop("ev_charger", None)

    def _refill(self, ha, **kw):
        started, real = [], engine.run_attribution_job

        async def _job(mid, sensors, **k):
            started.append(k)
            return await real(mid, sensors, **k)

        async def go():
            with mock.patch.object(engine, "run_attribution_job", side_effect=_job):
                got = await engine.rebuild_devices_after_refill(S1, END, **kw)
                for _ in range(60):
                    if (engine._attribution_jobs.get("ev_charger") or {}).get("status") in (
                            "done", "held", "error"):
                        break
                    await asyncio.sleep(0.05)
            return got
        with mock.patch.object(engine, "_engine_ha", ha):
            got = asyncio.run(go())
        return got, started

    def test_a_new_device_is_rebuilt_from_ha_whole(self):
        got, started = self._refill(FakeHA(2.0), fetch_new=True)
        self.assertEqual(got, ["ev_charger"])
        self.assertEqual(started, [{"pace_s": 0.05}])          # no window, the recorder asked
        a, b = _ev(self.st, S1), _ev(self.st, S2)
        self.assertEqual((a["imp_kwh"], b["imp_kwh"]), (1.0, 1.0))   # by the house's shape
        self.assertEqual(self.st.ha_reads_for("ev_charger")[0], {H: 2.0})

    def test_a_sensor_that_fails_the_check_is_held(self):
        got, started = self._refill(FakeHA(100.0), fetch_new=True)
        self.assertEqual(started, [])
        self.assertIsNone(_ev(self.st, S1))
        j = engine._attribution_jobs["ev_charger"]
        self.assertEqual((j["status"], j["verdict"]), ("held", "device_exceeds_house"))
        self.assertEqual(j["device_kwh"], 100.0)
        self.assertIsNotNone(j["house_kwh"])
        # held, not taken: Rebuild history on the card can claim it
        self.assertTrue(engine.attribution_claim("ev_charger", ["sensor.charger_energy"]))

    def test_GUARD_a_gap_fill_does_not_ask_ha(self):
        got, started = self._refill(FakeHA(2.0))
        self.assertEqual((got, started), ([], []))
        self.assertIsNone(_ev(self.st, S1))

    def test_GUARD_a_device_with_history_is_rebuilt_from_its_reads(self):
        self.st.record_recorder_hours("ev_charger", "import", {H: 2.0})
        got, started = self._refill(None, fetch_new=True)
        self.assertEqual(got, ["ev_charger"])
        self.assertEqual(started, [{"pace_s": 0.05, "window": (S1, END), "fetch": False}])


class TheCallers(unittest.TestCase):

    @staticmethod
    def _body(src, head):
        body = src[src.index(head):]
        return body[:body.index("\nasync def ", 1) if "\nasync def " in body[1:] else None]

    def test_the_api_import_asks_for_new_devices(self):
        src = open(engine.__file__, encoding="utf-8").read()
        body = src[src.index("async def run_api_import_job"):]
        body = body[:body.index("\nasync def ", 1)]
        self.assertIn('_ih["from"], _hi, fetch_new=True)', body)

    def test_a_csv_import_asks_for_new_devices(self):
        src = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
        body = src[src.index("def _rebuild_devices_after_csv"):]
        body = body[:body.index("\ndef ", 1)]
        self.assertIn('span["from"], hi, fetch_new=True)', body)   # hi: widened by the setup gap fill

    def test_GUARD_a_gap_fill_does_not(self):
        src = open(engine.__file__, encoding="utf-8").read()
        self.assertIn('await rebuild_devices_after_refill(_g["from"], _g["to"])\n', src)


class ThePage(unittest.TestCase):

    def test_a_held_rebuild_is_on_the_card(self):
        self.assertIn("} else if (st === 'held') {", PAGE)
        self.assertIn("Not rebuilt after the import: this sensor reads about", PAGE)

    def test_taking_the_import_drops_the_queued_offer(self):
        body = PAGE[PAGE.index("function _historyOfferImport() {"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("sessionStorage.removeItem(EMT.lsKey(_REBUILD_KEY))", body)


if __name__ == "__main__":
    unittest.main()
