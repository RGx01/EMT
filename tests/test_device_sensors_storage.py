"""
A device's sensors in one place — storage (device sensors design §2, §3, §7; PR 1).

- `device_sensors` holds every meter's sensors keyed by the meter's own id: one PRIMARY per
  channel (the configured `read`), ordered SECONDARIES for history. A config save keeps it; a new
  config period needs no copy. `config_from_db` gives each channel its `history_reads`.
- Changing the primary keeps the old one as the first secondary with its id (the rename prompt's
  default, yes); a save that sends back the list it loaded does not drop it.
- A live block (one with HA's registers) points at the entry that recorded it; an imported one at
  none; a rewrite carries the stored pointer, so a rename never re-points an old block.
- `seed_device_sensors` gives a database from before the table its primaries; a device delete
  removes its entries.

All new code: on the parent every test fails by name. One GUARD, marked.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from block_store import BlockStore      # noqa: E402

PERIOD = "2026-01-01T00:00:00"


def _cfg(ev_read="sensor.charger_energy", history=None, main_read=None):
    ev = {"read": ev_read} if ev_read else {}
    if history is not None:
        ev["history_reads"] = history
    main = {"read": main_read} if main_read else {}
    return {"meters": {
        "electricity_main": {"meta": {"timezone": "UTC"}, "channels": {"import": main, "export": {}}},
        "ev_charger": {"meta": {"sub_meter": True, "parent_meter": "electricity_main",
                                "device": "Charger", "meter_type": "ev"},
                       "channels": {"import": ev}}}}


def _store(cfg=None):
    st = BlockStore(":memory:")
    st.insert_config_period(cfg or _cfg(), effective_from=PERIOD)
    return st


def _save(st, cfg):
    """What save_config does to the active period: delete its meter rows, write them again."""
    pid = st.get_current_config_period_id()
    with st._conn:
        for (mid,) in st._conn.execute("SELECT id FROM meters WHERE config_period_id=?", (pid,)):
            st._conn.execute("DELETE FROM meter_channels WHERE meter_id=?", (mid,))
        st._conn.execute("DELETE FROM meters WHERE config_period_id=?", (pid,))
        st._write_meters(cfg, pid)


def _entries(st, meter="ev_charger"):
    return [(r["entity_id"], r["role"], r["position"]) for r in st._conn.execute(
        "SELECT * FROM device_sensors WHERE meter_id=? ORDER BY channel, role, position, id", (meter,))]


def _history(st):
    return _ev_channels(st)["import"]["history_reads"]


def _ev_channels(st):
    return st.config_from_db(st.get_current_config_period_id())["meters"]["ev_charger"]["channels"]


def _block(st, start, *, read_start=10.0, source=None, replace=False):
    blk = {"start": start, "end": start, "meters": {"ev_charger": {
        "meta": {"sub_meter": True}, "source": source,
        "channels": {"import": {"kwh": 0.5, "read_start": read_start,
                                "read_end": None if read_start is None else read_start + 0.5}}}}}
    (st.append_block_replace if replace else st.append_block)(blk)


def _pointer(st, start):
    return st._conn.execute("SELECT imp_sensor_id FROM blocks WHERE block_start=? AND "
                            "meter_id='ev_charger'", (start,)).fetchone()[0]


def _id(st, entity):
    return st._conn.execute("SELECT id FROM device_sensors WHERE entity_id=?",
                            (entity,)).fetchone()[0]


class TheConfig(unittest.TestCase):

    def test_the_configured_read_is_the_primary(self):
        st = _store()
        self.assertEqual(_entries(st), [("sensor.charger_energy", "primary", None)])

    def test_history_reads_are_the_ordered_secondaries(self):
        st = _store(_cfg(history=["sensor.old_b", "sensor.old_a"]))
        self.assertEqual(_entries(st)[1:], [("sensor.old_b", "secondary", 0),
                                            ("sensor.old_a", "secondary", 1)])
        self.assertEqual(_ev_channels(st)["import"]["history_reads"], ["sensor.old_b", "sensor.old_a"])

    def test_a_save_that_does_not_know_them_keeps_them(self):
        st = _store(_cfg(history=["sensor.old"]))
        _save(st, _cfg())                               # no history_reads key
        self.assertEqual(_history(st), ["sensor.old"])

    def test_a_new_config_period_needs_no_copy(self):
        st = _store(_cfg(history=["sensor.old"]))
        st.insert_config_period(_cfg(), effective_from="2026-06-01T00:00:00")
        self.assertEqual(_ev_channels(st)["import"]["history_reads"], ["sensor.old"])
        self.assertEqual(len(_entries(st)), 2)

    def test_the_main_meter_has_a_primary_and_no_history(self):
        st = _store(_cfg(main_read="sensor.grid_import"))
        self.assertEqual(_entries(st, "electricity_main"), [("sensor.grid_import", "primary", None)])


class ARename(unittest.TestCase):

    def test_the_old_primary_becomes_the_first_secondary_with_its_id(self):
        st = _store(_cfg(history=["sensor.older"]))
        old_id = _id(st, "sensor.charger_energy")
        _save(st, _cfg(ev_read="sensor.charger_energy_2"))
        self.assertEqual(_entries(st), [("sensor.charger_energy_2", "primary", None),
                                        ("sensor.charger_energy", "secondary", 0),
                                        ("sensor.older", "secondary", 1)])
        self.assertEqual(_id(st, "sensor.charger_energy"), old_id)

    def test_a_save_of_the_list_it_loaded_keeps_the_renamed_one(self):
        st = _store(_cfg(history=["sensor.older"]))
        _save(st, _cfg(ev_read="sensor.charger_energy_2", history=["sensor.older"]))
        self.assertEqual(_history(st),
                         ["sensor.charger_energy", "sensor.older"])

    def test_a_secondary_made_primary_again_keeps_its_id(self):
        st = _store()
        old_id = _id(st, "sensor.charger_energy")
        _save(st, _cfg(ev_read="sensor.charger_energy_2"))
        _save(st, _cfg(ev_read="sensor.charger_energy"))
        self.assertEqual(_id(st, "sensor.charger_energy"), old_id)
        self.assertEqual(_entries(st)[0], ("sensor.charger_energy", "primary", None))


class TheBlocks(unittest.TestCase):

    def test_a_live_block_points_at_its_primary(self):
        st = _store()
        _block(st, "2026-03-01T10:00:00")
        self.assertEqual(_pointer(st, "2026-03-01T10:00:00"), _id(st, "sensor.charger_energy"))

    def test_a_block_with_no_ha_registers_points_at_none(self):
        st = _store()
        _block(st, "2026-03-01T10:00:00", read_start=None)
        self.assertIsNone(_pointer(st, "2026-03-01T10:00:00"))

    def test_a_new_row_by_replace_points_at_its_primary(self):
        st = _store()
        _block(st, "2026-03-01T10:00:00", replace=True)
        self.assertEqual(_pointer(st, "2026-03-01T10:00:00"), _id(st, "sensor.charger_energy"))

    def test_a_rewrite_after_a_rename_keeps_the_old_sensor(self):
        st = _store()
        _block(st, "2026-03-01T10:00:00")
        old_id = _id(st, "sensor.charger_energy")
        _save(st, _cfg(ev_read="sensor.charger_energy_2"))
        _block(st, "2026-03-01T10:00:00", replace=True)        # the round-trip rewrite
        self.assertEqual(_pointer(st, "2026-03-01T10:00:00"), old_id)

    def test_a_rewrite_of_an_unpointed_block_stays_unpointed(self):
        """A block from before the column is not given today's primary by a rewrite."""
        st = _store()
        _block(st, "2026-03-01T10:00:00")
        st._conn.execute("UPDATE blocks SET imp_sensor_id = NULL")
        _block(st, "2026-03-01T10:00:00", replace=True)
        self.assertIsNone(_pointer(st, "2026-03-01T10:00:00"))

    def test_a_dropped_sensor_a_block_points_at_stays_off_the_stitch(self):
        st = _store(_cfg(history=["sensor.old"]))
        _block(st, "2026-03-01T10:00:00")
        _save(st, _cfg(ev_read="sensor.new"))                  # renamed: kept for history
        _save(st, _cfg(ev_read="sensor.new", history=[]))      # then dropped from the list
        self.assertEqual(_history(st), [])
        self.assertIn(("sensor.charger_energy", "secondary", None), _entries(st))
        self.assertNotIn("sensor.old", [e for e, _, _ in _entries(st)])


class TheSeed(unittest.TestCase):

    def test_a_database_from_before_gets_its_primaries(self):
        st = _store()
        st._conn.execute("DELETE FROM device_sensors")
        self.assertEqual(st.seed_device_sensors(), 1)
        self.assertEqual(_entries(st), [("sensor.charger_energy", "primary", None)])

    def test_it_is_idempotent(self):
        """GUARD: a seeded database is left alone (fails on the parent only by name)."""
        st = _store()
        st._conn.execute("DELETE FROM device_sensors")
        st.seed_device_sensors()
        self.assertEqual(st.seed_device_sensors(), 0)


class TheDeviceDelete(unittest.TestCase):

    def test_the_route_deletes_its_sensors(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "web", "server.py"), encoding="utf-8").read()
        body = src[src.index("def _do_delete_and_recompute"):]
        self.assertIn('DELETE FROM device_sensors WHERE meter_id = ?', body[:body.index("# 2.")]
                      if "# 2." in body else body[:4000])


if __name__ == "__main__":
    unittest.main()
