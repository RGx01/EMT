"""
Device History rebuilds from HA's reads, and a refill starts it (soft delete design §2–§3, PR 3;
20 Oct 2026).

- A half-hour HA recorded live goes back as it was — its kWh, its registers, no source tag, the
  sensor that recorded it — not re-split from the recorder's hour.
- The rest of the hour, the recorder's hour less the kept half-hours, is split by the house's
  shape over the others, as before. With no recorder hour, the kept half-hours alone.
- The job walks the hours it kept before as well as those it fetches (a fetch wins an hour), and
  `fetch=False` asks HA's recorder nothing.
- A refill — a gap fill or a CSV import, the main meter only — starts the rebuild of each device
  holding reads in its span, from those alone.

Discriminating (fail on the parent: none of it exists), except the GUARDs marked.
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
from tests.test_device_sensors_storage import _store, _id      # noqa: E402

H = "2026-10-01T10:00:00"
S1, S2 = "2026-10-01T10:00:00", "2026-10-01T10:30:00"
END = {S1: S2, S2: "2026-10-01T11:00:00"}


def _main(start, kwh):
    return {"start": start, "end": END[start], "meters": {"electricity_main": {
        "meta": {}, "channels": {"import": {"kwh": kwh, "rate": 0.25, "cost": kwh * 0.25,
                                            "read_start": 100.0, "read_end": 100.0 + kwh}}}}}


def _ev(st, start):
    r = st._conn.execute("SELECT * FROM blocks WHERE block_start = ? AND meter_id = 'ev_charger'",
                         (start,)).fetchone()
    return None if r is None else dict(r)


class _Base(unittest.TestCase):
    def setUp(self):
        self.st = _store()
        self.st.append_block(_main(S1, 1.0))          # the house's shape: 1 to 3
        self.st.append_block(_main(S2, 3.0))
        self.p = [mock.patch.object(engine, "get_store", lambda: self.st),
                  mock.patch.object(engine, "_store", self.st)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def live(self, start, kwh, rs, sensor=None):
        blk = {"start": start, "end": END[start], "meters": {"ev_charger": {
            "meta": {"sub_meter": True}, "channels": {"import": {
                "kwh": kwh, "read_start": rs, "read_end": rs + kwh}}}}}
        self.st._conn.executemany(
            "INSERT INTO ha_reads (meter_id, channel, origin, period_start, minutes, sensor_id, "
            "kwh, read_start, read_end, interpolated, recorded_at) VALUES (:meter_id, :channel, "
            ":origin, :period_start, :minutes, %s, :kwh, :read_start, :read_end, :interpolated, "
            ":recorded_at)" % ("NULL" if sensor is None else int(sensor)),
            __import__("block_store")._live_reads(blk, ["ev_charger"]))
        self.st._conn.commit()

    def kept(self, start):
        return self.st.ha_reads_for("ev_charger")[1][start]


class TheHour(_Base):

    def test_a_live_half_hour_goes_back_as_it_was_and_the_rest_of_the_hour_is_split(self):
        self.live(S1, 0.4, 5.0)
        w, s, nh = engine._attribute_hour(H, "ev_charger", "electricity_main", 2.0,
                                          kept={S1: self.kept(S1)})
        self.assertEqual((w, s, nh), (2, 0, 0))
        a, b = _ev(self.st, S1), _ev(self.st, S2)
        self.assertEqual((a["imp_kwh"], a["imp_read_start"], a["imp_read_end"], a["source"]),
                         (0.4, 5.0, 5.4, None))
        self.assertEqual((b["imp_kwh"], b["source"]), (1.6, "recorder_attributed"))

    def test_with_none_kept_the_hour_splits_by_the_house_shape(self):
        """GUARD: as before (passes on the parent)."""
        engine._attribute_hour(H, "ev_charger", "electricity_main", 2.0)
        self.assertEqual((_ev(self.st, S1)["imp_kwh"], _ev(self.st, S2)["imp_kwh"]), (0.5, 1.5))

    def test_with_no_recorder_hour_the_kept_half_hours_alone(self):
        self.live(S1, 0.4, 5.0)
        engine._attribute_hour(H, "ev_charger", "electricity_main", None,
                               kept={S1: self.kept(S1)})
        self.assertEqual(_ev(self.st, S1)["imp_kwh"], 0.4)
        self.assertIsNone(_ev(self.st, S2))

    def test_it_names_the_sensor_that_recorded_it(self):
        self.st.set_device_history_sensors("ev_charger", "import", ["sensor.old_charger"])
        old = _id(self.st, "sensor.old_charger")
        self.live(S1, 0.4, 5.0, sensor=old)
        engine._attribute_hour(H, "ev_charger", "electricity_main", None,
                               kept={S1: self.kept(S1)})
        self.assertEqual(_ev(self.st, S1)["imp_sensor_id"], old)

    def test_a_present_device_row_is_never_overwritten(self):
        """GUARD: the per-block guard stands for a kept read too (fails on the parent only by
        the `kept` argument)."""
        self.st.append_block_replace({"start": S1, "end": S2, "meters": {"ev_charger": {
            "meta": {"sub_meter": True}, "channels": {"import": {"kwh": 0.9}}}}})
        self.live(S1, 0.4, 5.0)
        engine._attribute_hour(H, "ev_charger", "electricity_main", None,
                               kept={S1: self.kept(S1)})
        self.assertEqual(_ev(self.st, S1)["imp_kwh"], 0.9)


class TheJob(_Base):

    def _run(self, **kw):
        C.reset_for_tests()
        engine._attribution_jobs.pop("ev_charger", None)
        C.claim("attribution", "ev_charger", force=True)
        try:
            return asyncio.run(engine.run_attribution_job("ev_charger", ["sensor.charger_energy"],
                                                          **kw))
        finally:
            C.reset_for_tests()

    def test_without_a_fetch_it_rebuilds_from_the_table_alone(self):
        self.live(S1, 0.4, 5.0)
        self.st.record_recorder_hours("ev_charger", "import", {H: 2.0})
        with mock.patch.object(engine, "_engine_ha", None):
            res = self._run(fetch=False, window=(S1, "2026-10-01T11:00:00"))
        self.assertTrue(res["ok"], res)
        self.assertEqual((_ev(self.st, S1)["imp_kwh"], _ev(self.st, S2)["imp_kwh"]), (0.4, 1.6))

    def test_the_window_bounds_it(self):
        self.st.record_recorder_hours("ev_charger", "import", {H: 2.0, "2026-10-02T10:00:00": 1.0})
        with mock.patch.object(engine, "_engine_ha", None):
            self._run(fetch=False, window=("2026-10-02T00:00:00", "2026-10-03T00:00:00"))
        self.assertIsNone(_ev(self.st, S1))

    def test_a_fetched_hour_wins_over_a_kept_one(self):
        """GUARD (passes on the parent, which reads only the fetch)."""
        class FakeHA:
            async def get_statistics(self, ids, start, end, period="hour", timeout=0):
                return {i: [{"start": H + "+00:00", "change": 4.0}] for i in ids}
        self.st.record_recorder_hours("ev_charger", "import", {H: 2.0})
        with mock.patch.object(engine, "_engine_ha", FakeHA()):
            self._run()
        self.assertEqual((_ev(self.st, S1)["imp_kwh"], _ev(self.st, S2)["imp_kwh"]), (1.0, 3.0))


class TheRefill(_Base):

    def test_deleted_then_refilled_gets_its_devices_back_with_no_recorder_call(self):
        st = self.st
        st.append_block({"start": S1, "end": S2, "meters": {"ev_charger": {
            "meta": {"sub_meter": True}, "channels": {"import": {
                "kwh": 0.4, "read_start": 5.0, "read_end": 5.4}}}}}, ha_meters=["ev_charger"])
        st.delete_blocks_for_date_range("2026-10-01", "2026-10-01", tz_name="UTC")
        st.append_block(_main(S1, 1.0))                         # Octopus's refill: the house only
        st.append_block(_main(S2, 3.0))
        started, real = [], engine.run_attribution_job

        async def _job(mid, sensors, **kw):
            started.append((mid, kw))
            return await real(mid, sensors, **kw)

        async def go():
            with mock.patch.object(engine, "run_attribution_job", side_effect=_job):
                got = await engine.rebuild_devices_after_refill(S1, "2026-10-01T11:00:00")
                for _ in range(50):
                    if (engine._attribution_jobs.get("ev_charger") or {}).get("status") == "done":
                        break
                    await asyncio.sleep(0.05)
            return got
        C.reset_for_tests()
        engine._attribution_jobs.pop("ev_charger", None)
        with mock.patch.object(engine, "_engine_ha", None):
            got = asyncio.run(go())
        C.reset_for_tests()
        self.assertEqual(got, ["ev_charger"])
        self.assertEqual(started[0][1]["fetch"], False)
        a = _ev(st, S1)
        self.assertEqual((a["imp_kwh"], a["imp_read_start"], a["source"]), (0.4, 5.0, None))

    def test_a_device_already_rebuilding_is_left_to_its_run(self):
        self.live(S1, 0.4, 5.0)
        with mock.patch.object(engine, "attribution_claim", lambda m, s: False), \
             mock.patch.object(engine, "run_attribution_job") as job:
            got = asyncio.run(engine.rebuild_devices_after_refill(S1, S2))
        self.assertEqual(got, [])
        job.assert_not_called()

    def test_no_reads_in_the_span_starts_nothing(self):
        """GUARD (fails on the parent only by the missing function)."""
        with mock.patch.object(engine, "run_attribution_job") as job:
            self.assertEqual(asyncio.run(engine.rebuild_devices_after_refill(S1, S2)), [])
        job.assert_not_called()


class TheCallers(unittest.TestCase):

    def test_the_gap_fills_finish_starts_it(self):
        src = open(engine.__file__, encoding="utf-8").read()
        body = src[src.index("async def _gap_fill_finish"):]
        body = body[:body.index("\ndef ")]
        self.assertIn('await rebuild_devices_after_refill(_g["from"], _g["to"])', body)

    def test_both_csv_applies_start_it(self):
        src = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
        self.assertIn("    _rebuild_devices_after_csv(res)\n", src)            # the dispatch
        self.assertIn("    _rebuild_devices_after_csv(result)\n", src)         # the import route

    def test_a_csv_that_wrote_nothing_starts_nothing(self):
        from tests.test_server import server
        with mock.patch.object(server, "_run_on_engine_loop") as run:
            server._rebuild_devices_after_csv({"blocks_written": 0,
                                               "span": {"from": S1, "to": S2}})
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
