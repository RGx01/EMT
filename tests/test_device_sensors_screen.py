"""
A device's sensors on the device screen (device sensors design §2–§5, PR 4).

- The device card lists the device's history sensors (`channels.import.history_reads`), editable.
- Changing a device's live sensor asks whether to keep the old one for history, yes by default;
  the answer travels as `keep_old_read`, and "no" drops it (kept only while a block points at it).
  The main meter has no history sensors: its old sensor is never one.
- Saving a NEW device with a sensor offers to rebuild its history, after the reload (the save
  restarts the engine); the wizard's save reloads for it too.
- A device delete's warning gives what goes: half-hours recorded live, from which sensor, since
  when, and those rebuilt (`/api/meter/<id>/delete-preview`).

All new code; on the parent every test fails but the two GUARDs, marked.
"""
import json
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from block_store import BlockStore      # noqa: E402

_PAGE = open(os.path.join(_ROOT, "web", "templates", "meter_config.html"), encoding="utf-8").read()


def _cfg(ev_read="sensor.ev_now", main_read="sensor.grid", keep=None, history=None):
    imp = {"read": ev_read}
    if keep is not None:
        imp["keep_old_read"] = keep
    if history is not None:
        imp["history_reads"] = history
    return {"meters": {
        "electricity_main": {"meta": {"timezone": "UTC"}, "channels": {"import": {"read": main_read}}},
        "ev_charger": {"meta": {"sub_meter": True, "parent_meter": "electricity_main", "device": "EV"},
                       "channels": {"import": imp}}}}


def _save(st, cfg):
    pid = st.get_current_config_period_id()
    with st._conn:
        for (mid,) in st._conn.execute("SELECT id FROM meters WHERE config_period_id=?", (pid,)):
            st._conn.execute("DELETE FROM meter_channels WHERE meter_id=?", (mid,))
        st._conn.execute("DELETE FROM meters WHERE config_period_id=?", (pid,))
        st._write_meters(cfg, pid)


def _hist(st, meter="ev_charger"):
    return st.config_from_db(st.get_current_config_period_id())["meters"][meter]["channels"]["import"]["history_reads"]


def _fn(name):
    i = _PAGE.index("function " + name + "(")
    return _PAGE[i:_PAGE.index("\n}\n", i)]


class TheRenameAnswer(unittest.TestCase):

    def setUp(self):
        self.st = BlockStore(":memory:")
        self.st.insert_config_period(_cfg(), effective_from="2026-01-01T00:00:00")

    def test_yes_keeps_the_old_sensor(self):
        """GUARD (passes on the parent, whose default already keeps it)."""
        _save(self.st, _cfg(ev_read="sensor.ev_new", keep=True))
        self.assertEqual(_hist(self.st), ["sensor.ev_now"])

    def test_no_drops_it(self):
        _save(self.st, _cfg(ev_read="sensor.ev_new", keep=False))
        self.assertEqual(_hist(self.st), [])
        self.assertEqual(self.st._conn.execute("SELECT COUNT(*) FROM device_sensors WHERE "
                                               "entity_id='sensor.ev_now'").fetchone()[0], 0)

    def test_the_main_meter_s_old_sensor_is_never_a_history_sensor(self):
        _save(self.st, _cfg(main_read="sensor.grid_new"))
        self.assertEqual(_hist(self.st, "electricity_main"), [])


class TheDeletePreview(unittest.TestCase):

    def setUp(self):
        from tests.test_server import make_client, server
        self.server = server
        self.st = BlockStore(":memory:")
        self.st.insert_config_period(_cfg(history=["sensor.ev_old"]), effective_from="2026-01-01T00:00:00")
        for i, src in enumerate((None, None, "recorder_attributed")):
            self.st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                                  "imp_kwh, source) VALUES (?, 'x', 'ev_charger', 1, 0.5, ?)",
                                  ("2026-03-0%dT10:00:00" % (i + 1), src))
        self.client = make_client(store=self.st)
        self.p = [mock.patch.object(server, "load_config",
                                    lambda: self.st.config_from_db(self.st.get_current_config_period_id())),
                  mock.patch.object(server.store_access, "store", lambda: self.st)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_it_counts_what_goes(self):
        d = json.loads(self.client.get("/api/meter/ev_charger/delete-preview").data)
        self.assertEqual((d["half_hours"], d["recorded_live"], d["rebuilt"], d["first"]),
                         (3, 2, 1, "2026-03-01"))
        self.assertEqual(d["sensors"], ["sensor.ev_now", "sensor.ev_old"])

    def test_an_unknown_device_is_404(self):
        """GUARD (on the parent the route itself is missing, so 404 too)."""
        self.assertEqual(self.client.get("/api/meter/nope/delete-preview").status_code, 404)


class TheScreen(unittest.TestCase):

    def test_the_card_lists_the_history_sensors(self):
        self.assertIn("buildHistoryList(meterId, imp)", _fn("buildSubCard"))
        body = _fn("buildHistoryList")
        self.assertIn("class=\"js-hist\"", body)
        self.assertIn("js-hist-add", body)
        self.assertIn('list="hist-entities"', body)

    def test_edits_land_in_history_reads(self):
        self.assertIn("imp.history_reads = []", _fn("_histOf"))
        self.assertIn("el.classList.contains('js-hist')", _fn("onContainerChange"))

    def test_a_changed_live_sensor_asks_before_the_save(self):
        self.assertIn("_askRenames(_renamedDevices(), _saveConfigAsked);", _fn("saveConfig"))
        self.assertIn("config.meters[r.id].channels.import.keep_old_read = keep;", _fn("_askRenames"))
        self.assertIn("rename-yes').focus()", _fn("_askRenames"))     # yes is the default

    def test_a_new_device_is_offered_a_rebuild_after_the_reload(self):
        self.assertIn("_queueRebuildOffer();", _fn("_doSave"))
        self.assertIn("!(_serverConfig.meters || {})[id]", _fn("_queueRebuildOffer"))
        self.assertIn("_maybeOfferRebuild();", _PAGE)
        self.assertIn("'/api/historical/attribute/start'", _fn("_maybeOfferRebuild"))

    def test_the_wizard_reloads_for_the_offer(self):
        # since 4 Nov 2026 the history offer (tests/test_history_offer.py) goes first, and its Not now
        # reloads for this one; without it, the reload is as before
        self.assertIn("var rebuild = _queueRebuildOffer();", _PAGE)
        self.assertIn("else if (rebuild) setTimeout(function() { window.location.reload(); }, 1200);", _PAGE)

    def test_the_delete_warning_says_what_goes(self):
        body = _fn("openDeleteModal")
        self.assertIn("/delete-preview'", body)
        self.assertIn("which are <strong>hourly</strong>", body)


if __name__ == "__main__":
    unittest.main()
