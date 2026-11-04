"""
Replacing a device's history after a wrong sensor (4 Nov 2026, the user, a scratch setup; replace device
history design).

The user's battery was set up on a sensor that counts its solar charging too; its history was rebuilt
from it (34,924 half-hours) and its recorder hours kept (20,603). Changing the sensor kept all of it:
*Rebuild history* fills only half-hours a device has no figure for, and *Remove* then a rebuild mixes
the sensors — the new sensor's hours where HA has them, the old sensor's kept hours everywhere else,
its live half-hours put back as they were. Now:

- a sensor change asks why: **renamed** (keep it for history), **a new device** (its history stays),
  or **the wrong sensor** — the device's history is then offered for replacing, after the reload;
- **Replace history** (the card, or that offer): a backup, the sensor check, then the device's rebuilt
  half-hours, and everything a sensor it no longer uses recorded (`ha_reads` and live half-hours), are
  dropped, and its history is rebuilt whole from the sensors it uses now;
- the card says which sensors the history came from, and warns where one is no longer used.

The figures are made up.

b7 (4 Nov 2026) dropped first and fetched after: the drop's house recompute held the loop for 104 s,
HA's WebSocket dropped, the fetch never came back, and the run said "done" with nothing written — the
device left with no history; and the card showed nothing while the replace was still its claim.
FetchFirst, ARebuildWhoseFetchFails and ThePage's first two fail on b7.

Discriminating (as first built, #64): every test here fails on the parent but one. test_GUARD_rebuild_alone_would_have_kept_
the_wrong_sensor passes there: it pins the defect, what *Rebuild history* alone does. The store's GUARD
(the sensors in use keep their rows) calls the new drop, so cannot run on the parent.
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
from block_store import BlockStore                  # noqa: E402
from tests.test_device_sensors_storage import _cfg, _save, PERIOD   # noqa: E402

PAGE = open(os.path.join(_ROOT, "web", "templates", "meter_config.html"), encoding="utf-8").read()
WRONG, RIGHT = "sensor.battery_in", "sensor.inverter_in"
H1, H2 = "2026-10-01T10:00:00", "2026-10-01T11:00:00"
HALF = {"2026-10-01T10:00:00": "2026-10-01T10:30:00", "2026-10-01T10:30:00": "2026-10-01T11:00:00",
        "2026-10-01T11:00:00": "2026-10-01T11:30:00", "2026-10-01T11:30:00": "2026-10-01T12:00:00"}


def _fn(name):
    i = PAGE.index("function " + name + "(")
    return PAGE[i:PAGE.index("\n}\n", i)]


def _house(st, start, kwh=3.0):
    st.append_block({"start": start, "end": HALF[start], "meters": {"electricity_main": {
        "meta": {}, "channels": {"import": {"kwh": kwh, "rate": 0.25, "cost": kwh * 0.25,
                                            "read_start": 100.0, "read_end": 100.0 + kwh}}}}})


def _sid(st, entity):
    r = st._conn.execute("SELECT id FROM device_sensors WHERE entity_id = ?", (entity,)).fetchone()
    return r and r[0]


def _rows(st):
    return {r[0]: (r[1], r[2], r[3]) for r in st._conn.execute(
        "SELECT block_start, source, imp_sensor_id, imp_kwh FROM blocks WHERE meter_id = 'ev_charger'")}


def _household():
    """A device rebuilt from the wrong sensor, one live half-hour from it, then the sensor changed
    and the old one not kept (the user's answer)."""
    st = BlockStore(":memory:")
    st.insert_config_period(_cfg(ev_read=WRONG), effective_from=PERIOD)
    for s in HALF:
        _house(st, s)
    st.record_recorder_hours("ev_charger", "import", {H1: 2.4, H2: 2.4}, {H1: WRONG, H2: WRONG})
    wid = _sid(st, WRONG)
    with st._conn:
        for s in ("2026-10-01T10:00:00", "2026-10-01T10:30:00", "2026-10-01T11:00:00"):
            st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                             "imp_kwh, source) VALUES (?, ?, 'ev_charger', 1, 1.2, 'recorder_attributed')",
                             (s, HALF[s]))
        st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
                         "imp_sensor_id) VALUES ('2026-10-01T11:30:00', '2026-10-01T12:00:00', "
                         "'ev_charger', 1, 1.2, ?)", (wid,))
        st._conn.execute("INSERT INTO ha_reads (meter_id, channel, origin, period_start, minutes, sensor_id, "
                         "kwh, recorded_at) VALUES ('ev_charger', 'import', 'live', '2026-10-01T11:30:00', "
                         "30, ?, 1.2, '2026-10-01T12:00:00')", (wid,))
    cfg = _cfg(ev_read=RIGHT)
    cfg["meters"]["ev_charger"]["channels"]["import"]["keep_old_read"] = False
    _save(st, cfg)
    st.record_attribution_run({"run_id": "r1", "meter_id": "ev_charger", "from": H1, "to": H2})
    return st


class TheStore(unittest.TestCase):

    def setUp(self):
        self.st = _household()

    def test_the_old_sensor_is_off_the_stitch(self):
        self.assertEqual([o["entity_id"] for o in self.st.device_off_stitch_sensors("ev_charger")], [WRONG])

    def test_the_card_is_told_where_the_history_came_from(self):
        src = self.st.device_history_sources()["ev_charger"]
        self.assertEqual([(x["entity_id"], x["on_stitch"], x["hours"], x["half_hours"]) for x in src],
                         [(WRONG, False, 2, 1)])

    def test_the_drop_takes_the_rebuilt_rows_and_the_old_sensor_s(self):
        off = [o["id"] for o in self.st.device_off_stitch_sensors("ev_charger")]
        res = self.st.drop_device_history("ev_charger", off)
        self.assertEqual((res["deleted_blocks"], res["deleted_reads"]), (4, 3))
        self.assertEqual(res["parents"], ["electricity_main"])
        self.assertEqual(_rows(self.st), {})
        self.assertIsNone(_sid(self.st, WRONG))                 # nothing names it now

    def test_GUARD_the_drop_keeps_what_the_sensors_in_use_recorded(self):
        rid = _sid(self.st, RIGHT)
        with self.st._conn:
            self.st._conn.execute("UPDATE blocks SET imp_sensor_id = ? WHERE meter_id = 'ev_charger' AND "
                                  "block_start = '2026-10-01T11:30:00'", (rid,))
        self.st.drop_device_history("ev_charger", [])
        self.assertEqual(list(_rows(self.st)), ["2026-10-01T11:30:00"])


class FakeHA:
    """HA's recorder: the right sensor has the first hour only."""
    async def get_statistics(self, ids, start, end, period="hour", timeout=0):
        return {i: ([{"start": H1 + "+00:00", "change": 1.0}] if i == RIGHT else []) for i in ids}


class _ReplaceBase(unittest.TestCase):

    def setUp(self):
        self.st = _household()
        self.p = [mock.patch.object(engine, "get_store", lambda: self.st),
                  mock.patch.object(engine, "_store", self.st),
                  mock.patch.object(engine, "_engine_ha", FakeHA())]
        for x in self.p:
            x.start()
        C.reset_for_tests()
        engine._attribution_jobs.pop("ev_charger", None)

    def tearDown(self):
        for x in self.p:
            x.stop()
        C.reset_for_tests()
        engine._attribution_jobs.pop("ev_charger", None)

    def _replace(self):
        self.assertTrue(engine.attribution_claim("ev_charger", [RIGHT]))
        return asyncio.run(engine.replace_device_history("ev_charger", [RIGHT]))



class TheReplace(_ReplaceBase):

    def test_the_history_is_the_new_sensor_s_alone(self):
        res = self._replace()
        self.assertTrue(res["ok"], res)
        rows = _rows(self.st)
        # the first hour from the right sensor, by the house's shape; the second hour, which only the
        # wrong sensor had, and its live half-hour, gone
        self.assertEqual({s: r[2] for s, r in rows.items()},
                         {"2026-10-01T10:00:00": 0.5, "2026-10-01T10:30:00": 0.5})
        self.assertEqual(self.st.ha_reads_for("ev_charger")[0], {H1: 1.0})
        self.assertEqual([x["entity_id"] for x in self.st.device_history_sources()["ev_charger"]], [RIGHT])

    def test_without_ha_nothing_is_dropped(self):
        with mock.patch.object(engine, "_engine_ha", None):
            res = self._replace()
        self.assertFalse(res["ok"])
        self.assertEqual(len(_rows(self.st)), 4)
        self.assertEqual(engine._attribution_jobs["ev_charger"]["status"], "error")

    def test_the_old_runs_leave_the_ledger(self):
        self._replace()
        self.assertNotIn("r1", [r.get("run_id") for r in self.st.get_attribution_runs()])

    def test_GUARD_rebuild_alone_would_have_kept_the_wrong_sensor(self):
        """What *Rebuild history* does with the same household: every half-hour has a figure, and
        the wrong sensor's kept hour fills the hour the right one lacks."""
        self.assertTrue(engine.attribution_claim("ev_charger", [RIGHT]))
        asyncio.run(engine.run_attribution_job("ev_charger", [RIGHT], pace_s=0.0))
        self.assertEqual(self.st.ha_reads_for("ev_charger")[0], {H1: 1.0, H2: 2.4})
        self.assertEqual(_rows(self.st)["2026-10-01T11:30:00"][2], 1.2)


class SilentHA:
    """HA's recorder not answering: the WebSocket dropped under the request (b7, 4 Nov 2026)."""
    def __init__(self):
        self.calls = 0

    async def get_statistics(self, ids, start, end, period="hour", timeout=0):
        self.calls += 1
        raise asyncio.TimeoutError()


class CountingHA(FakeHA):
    def __init__(self, rows=True):
        self.calls, self.rows = 0, rows

    async def get_statistics(self, ids, start, end, period="hour", timeout=0):
        self.calls += 1
        return await super().get_statistics(ids, start, end) if self.rows else {i: [] for i in ids}


class FetchFirst(_ReplaceBase):
    """b7's replace dropped first and fetched after; the fetch failed, and the device was left with
    no history. Now the fetch comes first: nothing is dropped unless HA answered with hours."""

    def _with(self, ha):
        with mock.patch.object(engine, "_engine_ha", ha):
            return self._replace()

    def test_a_fetch_that_fails_drops_nothing(self):
        res = self._with(SilentHA())
        self.assertEqual(res["error"], "fetch_failed")
        self.assertEqual(len(_rows(self.st)), 4)
        self.assertEqual(self.st.ha_reads_for("ev_charger")[0], {H1: 2.4, H2: 2.4})
        j = engine._attribution_jobs["ev_charger"]
        self.assertEqual(j["status"], "error")
        self.assertIn("nothing was replaced", j["error"])
        self.assertTrue(engine.attribution_claim("ev_charger", [RIGHT]))      # the claim is free

    def test_no_statistics_drops_nothing(self):
        res = self._with(CountingHA(rows=False))
        self.assertEqual(res["error"], "no_data")
        self.assertEqual(len(_rows(self.st)), 4)

    def test_GUARD_ha_is_asked_once(self):
        ha = CountingHA()
        res = self._with(ha)
        self.assertTrue(res["ok"], res)
        self.assertEqual(ha.calls, 1)                    # the rebuild uses the hours just kept

    def test_the_house_is_given_back_in_steps(self):
        seen, real = [], engine.recompute_remainders_for_window

        def spy(parent, lo, hi, **kw):
            seen.append((lo, hi))
            return real(parent, lo, hi, **kw)
        with mock.patch.object(engine, "recompute_remainders_for_window", spy):
            n = asyncio.run(engine.recompute_remainders_for_window_async(
                "electricity_main", "2026-10-01T10:00:00", "2026-10-01T12:00:00", every=2))
        self.assertEqual(n, 4)
        self.assertEqual([lo for lo, _ in seen], ["2026-10-01T10:00:00", "2026-10-01T11:00:00"])


class ARebuildWhoseFetchFails(_ReplaceBase):

    def test_is_an_error_not_done(self):
        with mock.patch.object(engine, "_engine_ha", SilentHA()):
            self.assertTrue(engine.attribution_claim("ev_charger", [RIGHT]))
            res = asyncio.run(engine.run_attribution_job("ev_charger", [RIGHT]))
        self.assertFalse(res["ok"])
        self.assertEqual(engine._attribution_jobs["ev_charger"]["status"], "error")


def _scheduler(started):
    """asyncio.run_coroutine_threadsafe: the sensor check is run (its verdict is the point); the
    job the route schedules is recorded, not run."""
    def run(job, loop):
        if asyncio.iscoroutine(job):
            v = asyncio.new_event_loop().run_until_complete(job)
            return mock.MagicMock(result=lambda timeout=None: v)
        started.append(job)
    return run


class TheRoute(unittest.TestCase):

    def setUp(self):
        sys.path.insert(0, os.path.join(_ROOT, "tests"))
        from tests.test_server import make_client, server
        self.server, self.client = server, make_client()
        self.cfg = {"meters": {"ev": {"meta": {"sub_meter": True},
                                      "channels": {"import": {"read": RIGHT}}}}}

    def _post(self, **patches):
        started = []
        eng = sys.modules["engine"]        # the module the route imports (test_server's, here)
        with mock.patch.object(self.server, "load_config", lambda: self.cfg), \
             mock.patch.object(self.server, "_event_loop", mock.MagicMock(is_running=lambda: True)), \
             mock.patch.object(eng, "attribution_claim", lambda m, s: True, create=True), \
             mock.patch.object(eng, "attribution_release", lambda m: started.append("released"),
                               create=True), \
             mock.patch.object(eng, "replace_device_history", lambda m, s: ("replace", m, s),
                               create=True), \
             mock.patch.object(self.server, "_run_on_engine_loop",
                               lambda coro, timeout=30.0: (coro.close(), [{"entity_id": WRONG}])[1]), \
             mock.patch("asyncio.run_coroutine_threadsafe", _scheduler(started)), \
             mock.patch.object(self.server, "_create_backup_zip",
                               patches.get("backup", lambda label: "/tmp/b-" + label + ".zip")):
            r = self.client.post("/api/historical/attribute/replace",
                                 json={"meter_id": "ev", "confirm_override": patches.get("override", True)})
        return r, started

    def test_it_backs_up_then_replaces(self):
        r, started = self._post()
        d = r.get_json()
        self.assertTrue(d["ok"], d)
        self.assertEqual((d["backup"], d["replaced"]), ("b-pre-replace-history.zip", [WRONG]))
        self.assertEqual(started, [("replace", "ev", [RIGHT])])

    def test_nothing_to_rebuild_from_no_replace(self):
        eng = sys.modules["engine"]
        for verdict in ("no_ha", "no_data"):
            async def pf(m, s, v=verdict):
                return {"verdict": v}
            with mock.patch.object(eng, "attribution_preflight", pf, create=True):
                r, started = self._post(override=False)
            self.assertEqual(r.status_code, 409, verdict)
            self.assertIn("nothing was replaced", r.get_json()["error"])
            self.assertEqual(started, ["released"])

    def test_no_backup_no_replace(self):
        def boom(label):
            raise OSError("disk full")
        r, started = self._post(backup=boom)
        self.assertEqual(r.status_code, 500)
        self.assertEqual(started, ["released"])


class ThePage(unittest.TestCase):

    def test_a_sensor_change_asks_why(self):
        for b in ('id="rename-yes"', 'id="rename-no"', 'id="rename-wrong"'):
            self.assertIn(b, PAGE)
        body = _fn("_askRenames")
        self.assertIn("document.getElementById('rename-wrong').onclick = function() { answer(false, true); };", body)
        self.assertIn("_replaceAfterSave.push(", body)

    def test_the_wrong_sensor_s_replace_is_offered_after_the_reload(self):
        self.assertIn("fresh = fresh.concat(_replaceAfterSave);", _fn("_queueRebuildOffer"))
        self.assertIn("'/api/historical/attribute/replace'", _fn("_maybeOfferRebuild"))

    def test_a_run_shows_from_its_claim(self):
        body = _fn("_renderHistoryState")
        self.assertIn("var busy = st === 'claimed' || st === 'running' || st === 'paused';", body)
        self.assertIn("b.disabled = busy;", body)
        self.assertIn("if (st === 'claimed' || (st === 'running' && run.phase === 'fetching')) {", body)
        self.assertIn("return t === 'claimed' || t === 'running' || t === 'paused';", _fn("_loadHistoryState"))

    def test_a_second_click_shows_the_run(self):
        for fn in ("_histRebuild", "_histReplace"):
            self.assertIn("if (d.status && d.status.status) { _loadHistoryState(); return; }", _fn(fn))

    def test_the_card_replaces_and_says_where_from(self):
        self.assertIn("js-hist-replace", _fn("buildHistorySection"))
        self.assertIn("'/api/historical/attribute/replace'", _fn("_histReplace"))
        self.assertIn("_histSourcesHtml(m)", _fn("_renderHistoryState"))
        self.assertIn("no longer used", _fn("_histSourcesHtml"))


if __name__ == "__main__":
    unittest.main()
