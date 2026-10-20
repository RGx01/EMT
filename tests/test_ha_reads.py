"""
HA's reads kept apart from the blocks (soft delete design §2, PR 2; 20 Oct 2026).

`ha_reads` holds what HA reported: a half-hour recorded live, written with its block at finalise
(before settlement can put Octopus's kWh on it) and again when a provisional device half-hour is
amended; and an hour from HA's recorder, kept AS the hour when Device History fetches it, so its
split by the house's import shape can be redone against the blocks as they are. A delete of
blocks leaves it; a device delete removes the device's rows. Nothing reads it yet (PR 3).

From v4, transform 7: a live block gives a half-hour row per channel holding both registers —
on a settled main block the registers' difference, since v4 put Octopus's kWh on the block; a
device keeps its kWh, its session sensor resetting — and a pair of Device History half-hours
gives back its hour.

Discriminating (fail on the parent: none of it exists), except the GUARDs marked.
"""
import asyncio
import os
import sqlite3
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import engine                                       # noqa: E402
import job_coordinator as C                         # noqa: E402
import v4import                                     # noqa: E402
from tests.test_device_sensors_storage import _store, _id      # noqa: E402
from tests.test_v4import import _Dir                # noqa: E402

S1, S2 = "2026-10-01T10:00:00", "2026-10-01T10:30:00"
END = {S1: S2, S2: "2026-10-01T11:00:00"}


def _blk(start, *, main=(100.0, 101.0, 1.0), ev=(5.0, 5.4, 0.4), interpolated=False):
    m = {}
    if main:
        rs, re_, k = main
        m["electricity_main"] = {"meta": {}, "channels": {
            "import": {"kwh": k, "read_start": rs, "read_end": re_},
            "export": {"kwh": 0.0, "read_start": 0.0, "read_end": 0.0}}}
    if ev:
        rs, re_, k = ev
        m["ev_charger"] = {"meta": {"sub_meter": True, "parent_meter": "electricity_main"},
                           "channels": {"import": {"kwh": k, "read_start": rs, "read_end": re_}}}
    return {"start": start, "end": END[start], "interpolated": interpolated, "meters": m}


def _reads(st, **where):
    sql = "SELECT * FROM ha_reads"
    if where:
        sql += " WHERE " + " AND ".join("%s = ?" % k for k in where)
    return [dict(r) for r in st._conn.execute(sql + " ORDER BY meter_id, channel, origin, "
                                              "period_start", tuple(where.values()))]


class TheLiveWrite(unittest.TestCase):

    def test_a_finalised_block_writes_each_channel_with_its_registers(self):
        st = _store()
        st.append_block(_blk(S1), ha_meters=["electricity_main", "ev_charger"])
        got = [(r["meter_id"], r["channel"], r["origin"], r["minutes"], r["kwh"],
                r["read_start"], r["read_end"]) for r in _reads(st)]
        self.assertEqual(got, [("electricity_main", "import", "live", 30, 1.0, 100.0, 101.0),
                               ("ev_charger", "import", "live", 30, 0.4, 5.0, 5.4)])
        self.assertEqual(_reads(st, meter_id="ev_charger")[0]["sensor_id"],
                         _id(st, "sensor.charger_energy"))

    def test_the_main_meters_no_reads_zero_is_left_out_a_devices_is_kept(self):
        """compute_channel gives 0.0 / 0.0 for a channel with no reads; a grid register is never
        0, but a session sensor sits there between charges (2,282 such EV half-hours on prod)."""
        st = _store()
        st.append_block(_blk(S1, main=(0.0, 0.0, 0.0), ev=(0.0, 0.0, 0.0)),
                        ha_meters=["electricity_main", "ev_charger"])
        self.assertEqual([(r["meter_id"], r["kwh"]) for r in _reads(st)], [("ev_charger", 0.0)])

    def test_the_kwh_not_the_registers_difference(self):
        """An EV charger's sensor resets each session: the block's kWh is HA's figure."""
        st = _store()
        st.append_block(_blk(S1, main=None, ev=(24.9, 0.3, 0.3)), ha_meters=["ev_charger"])
        self.assertEqual(_reads(st)[0]["kwh"], 0.3)

    def test_interpolated_is_kept(self):
        st = _store()
        st.append_block(_blk(S1, main=None, interpolated=True), ha_meters=["ev_charger"])
        self.assertEqual(_reads(st)[0]["interpolated"], 1)

    def test_a_write_naming_no_meters_writes_none(self):
        """GUARD of the default (a round-trip rewrite, an import): nothing to ha_reads. Fails on
        the parent only by the missing table."""
        st = _store()
        st.append_block(_blk(S1))
        st.append_block_replace(_blk(S1))
        self.assertEqual(_reads(st), [])

    def test_a_rewrite_after_settlement_leaves_hass_figure(self):
        """Settlement puts Octopus's kWh on the block; the round-trip rewrite names no meters."""
        st = _store()
        st.append_block(_blk(S1), ha_meters=["electricity_main", "ev_charger"])
        settled = _blk(S1, main=(100.0, 101.0, 1.25))
        st.append_block_replace(settled)
        self.assertEqual(_reads(st, meter_id="electricity_main")[0]["kwh"], 1.0)

    def test_an_amendment_replaces_only_its_device(self):
        st = _store()
        st.append_block(_blk(S1), ha_meters=["electricity_main", "ev_charger"])
        before = _reads(st, meter_id="ev_charger")[0]["sensor_id"]
        st.append_block_replace(_blk(S1, main=(100.0, 101.0, 1.25), ev=(5.0, 5.55, 0.55)),
                                ha_meters=["ev_charger"])
        self.assertEqual(_reads(st, meter_id="ev_charger")[0]["kwh"], 0.55)
        self.assertEqual(_reads(st, meter_id="ev_charger")[0]["sensor_id"], before)
        self.assertEqual(_reads(st, meter_id="electricity_main")[0]["kwh"], 1.0)


class TheRecorderHours(unittest.TestCase):

    def test_kept_as_the_hour_with_the_sensor_that_gave_it(self):
        st = _store()
        n = st.record_recorder_hours("ev_charger", "import", {S1: 1.5},
                                     {S1: "sensor.charger_energy"})
        self.assertEqual(n, 1)
        r = _reads(st)[0]
        self.assertEqual((r["origin"], r["minutes"], r["kwh"], r["sensor_id"], r["read_start"]),
                         ("recorder", 60, 1.5, _id(st, "sensor.charger_energy"), None))

    def test_a_later_fetch_replaces_the_hour_and_never_a_live_half_hour(self):
        st = _store()
        st.append_block(_blk(S1, main=None), ha_meters=["ev_charger"])
        st.record_recorder_hours("ev_charger", "import", {S1: 1.5})
        st.record_recorder_hours("ev_charger", "import", {S1: 1.6})
        got = [(r["origin"], r["kwh"]) for r in _reads(st)]
        self.assertEqual(got, [("live", 0.4), ("recorder", 1.6)])

    def test_the_stitch_names_each_hours_sensor(self):
        series = {"sensor.new": [{"start": "2026-03-01T11:00:00+00:00", "change": 0.5}],
                  "sensor.old": [{"start": "2026-03-01T10:00:00+00:00", "change": 0.7},
                                 {"start": "2026-03-01T11:00:00+00:00", "change": 9.0}]}
        src = {}
        out = engine._stitch_hourly_energy(series, ["sensor.new", "sensor.old"], sources=src)
        self.assertEqual(out, {"2026-03-01T10:00:00": 0.7, "2026-03-01T11:00:00": 0.5})
        self.assertEqual(src, {"2026-03-01T10:00:00": "sensor.old",
                               "2026-03-01T11:00:00": "sensor.new"})

    def test_the_rebuild_keeps_what_it_fetched(self):
        class FakeHA:
            async def get_statistics(self, ids, start, end, period="hour", timeout=0):
                return {i: [{"start": "2026-03-01T10:00:00+00:00", "change": 1.0}] for i in ids}
        store = mock.MagicMock()
        store.get_parent_meter_id.return_value = "electricity_main"
        store.get_device_live_coverage_start.return_value = None
        store.get_block_dict_by_start.return_value = None
        C.reset_for_tests()
        engine._attribution_jobs.pop("heat_pump", None)
        C.claim("attribution", "heat_pump", force=True)
        with mock.patch.object(engine, "_engine_ha", FakeHA()), \
             mock.patch.object(engine, "get_store", lambda: store):
            asyncio.run(engine.run_attribution_job("heat_pump", ["sensor.hp"]))
        store.record_recorder_hours.assert_called_once_with(
            "heat_pump", "import", {"2026-03-01T10:00:00": 1.0},
            {"2026-03-01T10:00:00": "sensor.hp"})
        C.reset_for_tests()


class WhatRemovesThem(unittest.TestCase):

    def test_a_delete_of_half_hours_leaves_them(self):
        st = _store()
        st.append_block(_blk(S1), ha_meters=["electricity_main", "ev_charger"])
        st.record_recorder_hours("ev_charger", "import", {S1: 1.5})
        st.delete_blocks_for_date_range("2026-10-01", "2026-10-01", tz_name="UTC")
        self.assertEqual(st._conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0], 0)
        self.assertEqual(len(_reads(st)), 3)

    def test_a_device_delete_removes_its_rows(self):
        src = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
        body = src[src.index("def _do_delete_and_recompute"):]
        self.assertIn('DELETE FROM ha_reads WHERE meter_id = ?', body[:body.index("# 2.")])

    def test_a_secondary_a_read_names_is_kept(self):
        st = _store()
        st.set_device_history_sensors("ev_charger", "import", ["sensor.old"])
        st.record_recorder_hours("ev_charger", "import", {S1: 1.5}, {S1: "sensor.old"})
        st.set_device_history_sensors("ev_charger", "import", [])
        self.assertEqual(st._conn.execute("SELECT COUNT(*) FROM device_sensors WHERE "
                                          "entity_id = 'sensor.old'").fetchone()[0], 1)


class TheEngineWrites(unittest.TestCase):

    def test_finalise_names_every_meter(self):
        store = mock.MagicMock()
        with mock.patch.object(engine, "get_store", lambda: store):
            engine.append_block(_blk(S1))
        self.assertEqual(store.append_block.call_args.kwargs["ha_meters"],
                         ["electricity_main", "ev_charger"])

    def test_the_amendment_names_only_its_device(self):
        src = open(engine.__file__, encoding="utf-8").read()
        body = src[src.index("def _amend_provisional"):]
        self.assertIn("append_block_replace(full_block, ha_meters=[meter_name])",
                      body[:body.index("\ndef ", 10)])


# ── transform 7 ───────────────────────────────────────────────────────────────

def _b(i, bs, meter, source, kwh, rs=None, re_=None, *, api=None, minutes=30):
    from datetime import datetime, timedelta
    be = (datetime.fromisoformat(bs) + timedelta(minutes=minutes)).isoformat()
    return ("INSERT INTO blocks (id, block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "imp_read_start, imp_read_end, imp_kwh_api, source) VALUES "
            "(%d, '%s', '%s', '%s', 1, %r, %s, %s, %s, %s)" % (
                i, bs, be, meter, kwh, "NULL" if rs is None else repr(rs),
                "NULL" if re_ is None else repr(re_), "NULL" if api is None else repr(api),
                "NULL" if source is None else "'%s'" % source))


METERS = ("INSERT INTO meters (id, config_period_id, meter_id, is_sub_meter) VALUES "
          "(1, 1, 'electricity_main', 0), (2, 1, 'ev_charger', 1)",
          "INSERT INTO meter_channels (meter_id, channel, read_sensor) VALUES "
          "(1, 'import', NULL), (1, 'export', NULL), (2, 'import', 'sensor.charger_now')")
FROM_V4 = METERS + (
    _b(70, "2026-09-01T10:00:00", "electricity_main", "kraken_api", 1.2, 100.0, 101.0, api=1.2),
    _b(71, "2026-09-01T10:30:00", "electricity_main", None, 0.8, 101.0, 101.8),
    _b(72, "2026-09-01T11:00:00", "electricity_main", None, 0.0, 0.0, 0.0),
    _b(73, "2026-09-01T10:00:00", "ev_charger", None, 0.3, 24.9, 0.3),
    _b(74, "2026-09-01T10:30:00", "ev_charger", None, 0.0, 0.0, 0.0),
    _b(75, "2026-08-01T10:00:00", "ev_charger", "recorder_attributed", 0.4),
    _b(76, "2026-08-01T10:30:00", "ev_charger", "recorder_attributed", 0.6),
    _b(77, "2026-08-01T12:30:00", "ev_charger", "recorder_attributed", 0.2),
    _b(78, "2026-08-01T10:00:00", "electricity_main", "imported_api", 1.0),
)


class FromV4(_Dir):

    def _rows(self, conn):
        return [(r["meter_id"], r["origin"], r["period_start"][11:16], r["minutes"], r["kwh"],
                 r["sensor_inferred"]) for r in conn.execute(
            "SELECT * FROM ha_reads ORDER BY meter_id, origin, period_start")]

    def test_what_each_kind_of_block_gives(self):
        conn, report = self.imported(extra_sql=FROM_V4)
        self.assertEqual(self._rows(conn), [
            ("electricity_main", "live", "10:00", 30, 1.0, 0),    # settled: the registers' 1.0
            ("electricity_main", "live", "10:30", 30, 0.8, 0),    # 11:00's 0.0 / 0.0 left out
            ("ev_charger", "live", "10:00", 30, 0.3, 1),          # reset: its kWh, inferred
            ("ev_charger", "live", "10:30", 30, 0.0, 1),          # idle: HA's zero kept
            ("ev_charger", "recorder", "10:00", 60, 1.0, 0),      # the pair gives its hour back
            ("ev_charger", "recorder", "12:30", 30, 0.2, 0)])     # a lone half as it is
        self.assertEqual(report["transforms"]["ha_reads"],
                         {"live": 4, "recorder_hours": 1, "recorder_half_hours": 1})
        self.assertTrue(report["gates"]["ha_reads"]["ok"])

    def test_the_live_rows_name_the_primary(self):
        conn, _ = self.imported(extra_sql=FROM_V4)
        got = conn.execute("SELECT DISTINCT s.entity_id FROM ha_reads h JOIN device_sensors s "
                           "ON s.id = h.sensor_id").fetchall()
        self.assertEqual([r[0] for r in got], ["sensor.charger_now"])

    def test_the_gate_checks_against_the_source(self):
        from v4import import transforms
        real = transforms.ha_reads

        def lossy(conn, report):
            real(conn, report)
            conn.execute("DELETE FROM main.ha_reads WHERE minutes = 60")
        with mock.patch.object(transforms, "ha_reads", lossy):
            with self.assertRaises(v4import.ImportFailed) as e:
                self.imported(extra_sql=FROM_V4)
        self.assertIn("recorder kWh not the source's for ev_charger", str(e.exception))


if __name__ == "__main__":
    unittest.main()
