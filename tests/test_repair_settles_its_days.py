"""
A gap fill settles what it filled, not the whole history (6 Nov 2026, the user, after repairing 13 half-hours).

The repair walked 732 days under "Finishing your import … avoid restarting or rebuilding", and changed 5 half-hours,
all its own. Now the fill queues its own spans; an import still walks from the start. Moving the fill off the walk
showed what the walk had been doing by accident: a device rebuild writes rows at the main's single rate, and the walk
arrived after it. So a rebuild that writes queues its span again. A done marker has no cursor, so the banner, which
reads `in_progress`, would have said "732 days left" through a pass of a few days: it now asks about a walk only.

Discriminating: the tests not marked GUARD fail on the parent.
"""
import asyncio
import inspect
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine                                    # noqa: E402
import test_daily_rediscovery as R               # noqa: E402  (_run, _Harness)

LO, HI = "2025-06-02T17:30:00", "2025-06-03T00:00:00"
DAYS = ("2025-05-01T12:00:00", "2025-06-01T12:00:00", "2025-06-02T18:00:00", "2025-06-02T23:30:00", "2025-07-01T12:00:00")


class _Base(R._Harness):

    def setUp(self):
        super().setUp()
        self.st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, "
                              "block_minutes, timezone) VALUES (1, '2020-01-01T00:00:00', 1, 30, 'UTC')")
        for bs in DAYS:
            self.st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                                  "imp_kwh) VALUES (?, ?, 'electricity_main', 1, 1.0)", (bs, bs))
        self.st._conn.commit()
        self.done = {"version": engine._SETTLE_HISTORY_VERSION, "done": True, "read": 9, "written": 1}
        self.st.set_meta(engine._SETTLE_HISTORY_MARKER, dict(self.done))
        self.ranges = []
        self._patches = [
            mock.patch.object(engine, "settle_range", lambda st, lo, hi: self.ranges.append((lo, hi)) or {
                "read": 1, "written": 0, "parts": {}}),
            mock.patch.object(engine._coord, "admission", lambda name: (engine.ADMIT, None)),
            mock.patch.object(engine, "_SETTLE_HISTORY_PACE_S", 0),
            mock.patch.object(engine, "_SETTLE_HISTORY_PASS_PACE", 0),
            mock.patch.object(engine, "_kraken_rate_schedules", {"import": mock.MagicMock(is_empty=lambda: False)}),
            mock.patch.object(engine, "_settle_history_running", False),
            mock.patch.object(engine, "_settle_history_rearm", False),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        super().tearDown()


class TheFill(_Base):

    def _finish(self, j):
        """`_gap_fill_finish`, then what it started, to the end."""
        async def go():
            with mock.patch.object(engine, "rebuild_devices_after_refill", mock.AsyncMock(return_value=[])), \
                 mock.patch.object(engine, "_apply_bills_paced", mock.AsyncMock(return_value=0)), \
                 mock.patch.object(engine, "_maybe_drain_measured_history", lambda: None), \
                 mock.patch.object(engine, "_persist_run_status", lambda j: None), \
                 mock.patch.object(engine._coord, "running", lambda name, key=None: False):
                await engine._gap_fill_finish(j, ("import",))
                for t in [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]:
                    await t
        R._run(go())

    def test_it_settles_its_own_half_hours_and_no_others(self):
        self._finish({"written": {"import": 13}, "gap": {"from": LO, "to": HI}})
        self.assertEqual(self.ranges, [(LO, "2025-06-03T00:00:00")])

    def test_the_walk_is_left_alone(self):
        self._finish({"written": {"import": 13}, "gap": {"from": LO, "to": HI}})
        self.assertEqual(self.st.get_meta(engine._SETTLE_HISTORY_MARKER), self.done)
        self.assertEqual(self.st.get_meta(engine._SETTLE_SPANS_KEY), [])

    def test_it_crosses_midnight_a_day_at_a_time(self):
        self._finish({"written": {"import": 3}, "gap": {"from": "2025-06-01T23:30:00", "to": "2025-06-02T00:30:00"}})
        self.assertEqual(self.ranges, [("2025-06-01T23:30:00", "2025-06-02T00:00:00"),
                                       ("2025-06-02T00:00:00", "2025-06-02T00:30:00")])

    def test_several_runs_settle_each_run_not_the_days_between(self):
        a, b = ["2025-06-02T18:00:00", "2025-06-02T18:30:00"], ["2025-05-01T12:00:00", "2025-05-01T12:30:00"]
        self._finish({"written": {"import": 2}, "gap": {"from": b[0], "to": a[1]},
                      "runs": [{"from": a[0], "to": a[1]}, {"from": b[0], "to": b[1]}]})
        self.assertEqual(self.ranges, [tuple(b), tuple(a)])

    def test_GUARD_with_no_span_known_it_walks(self):
        self._finish({"written": {"import": 1}})
        self.assertEqual(self.ranges[0][0], "2025-05-01T00:00:00")
        self.assertEqual(self.st.get_meta(engine._SETTLE_HISTORY_MARKER)["read"], len(self.ranges))

    def test_GUARD_a_fill_that_wrote_nothing_settles_nothing(self):
        self._finish({"written": {"import": 0}, "gap": {"from": LO, "to": HI}})
        self.assertEqual(self.ranges, [])

    def test_a_restart_before_the_pass_still_settles_them(self):
        async def _noop(**k):
            pass
        async def go():
            with mock.patch.object(engine, "_after_import_settle", _noop), \
                 mock.patch.object(engine, "rebuild_devices_after_refill", mock.AsyncMock(return_value=[])), \
                 mock.patch.object(engine, "_persist_run_status", lambda j: None):
                await engine._gap_fill_finish({"written": {"import": 13}, "gap": {"from": LO, "to": HI}}, ("import",))
        R._run(go())
        self.assertEqual(self.st.get_meta(engine._SETTLE_SPANS_KEY), [[LO, HI]])


class TheImport(_Base):

    def test_GUARD_an_import_still_walks_from_the_start(self):
        async def go():
            with mock.patch.object(engine, "_apply_bills_paced", mock.AsyncMock(return_value=0)), \
                 mock.patch.object(engine, "_maybe_drain_measured_history", lambda: None), \
                 mock.patch.object(engine._coord, "running", lambda name, key=None: False):
                await engine._after_import_settle()
        R._run(go())
        self.assertEqual(self.ranges[0][0], "2025-05-01T00:00:00")
        self.assertGreater(len(self.ranges), 60)
        self.assertTrue(self.st.get_meta(engine._SETTLE_HISTORY_MARKER)["done"])

    def test_GUARD_an_import_and_a_csv_call_it_with_no_argument(self):
        src = inspect.getsource(engine.run_api_import_job)
        self.assertIn("asyncio.create_task(_after_import_settle())", src)
        server = open(os.path.join(os.path.dirname(engine.__file__), "web", "server.py"), encoding="utf-8").read()
        self.assertIn("_asyncio.create_task(_eng._after_import_settle())", server)


class TheDevices(_Base):

    def _rebuild(self, written, **k):
        async def go():
            with mock.patch.object(engine, "run_attribution_job", mock.AsyncMock(return_value={"written": written})), \
                 mock.patch.object(engine, "attribution_claim", lambda *a, **kw: (True, None), create=True), \
                 mock.patch.object(engine._coord, "claim", lambda *a, **kw: True), \
                 mock.patch.object(engine._coord, "running", lambda *a, **kw: False):
                await engine._rebuild_when_admitted("sub_meter_1", ["sensor.x"], LO, HI, **k)
                for t in [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]:
                    await t
        R._run(go())

    def test_a_rebuild_that_wrote_settles_its_span_after_it(self):
        self._rebuild(3, settle_after=True)
        self.assertEqual(self.ranges, [(LO, "2025-06-03T00:00:00")])

    def test_GUARD_a_rebuild_that_wrote_nothing_settles_nothing(self):
        self._rebuild(0, settle_after=True)
        self.assertEqual(self.ranges, [])

    def test_GUARD_an_imports_rebuild_adds_no_second_pass(self):
        self._rebuild(3)
        self.assertEqual(self.ranges, [])

    def test_a_gap_fill_asks_for_it(self):
        src = inspect.getsource(engine._gap_fill_finish)
        self.assertIn("settle_after=True", src)


class ASpanQueuedDuringAWalk(_Base):

    def test_it_is_settled_when_the_walk_ends(self):
        """A fill or a rebuild that finds a walk running only queues: the walk's own loop must take it."""
        self.st.set_meta(engine._SETTLE_HISTORY_MARKER, {"version": engine._SETTLE_HISTORY_VERSION,
                                                        "cursor": "2025-07-01T00:00:00"})
        self.st.set_meta(engine._SETTLE_SPANS_KEY, [[LO, HI]])
        R._run(engine._settle_history_to_done())
        self.assertEqual(self.ranges, [("2025-07-01T00:00:00", "2025-07-02T00:00:00"), (LO, "2025-06-03T00:00:00")])
        self.assertEqual(self.st.get_meta(engine._SETTLE_SPANS_KEY), [])


class TheBanner(_Base):

    def test_a_queued_pass_is_not_a_walk(self):
        engine._settle_history_running = True
        self.assertTrue(engine.settle_history_in_progress())       # the share copy still waits
        self.assertFalse(engine.settle_history_walking())

    def test_GUARD_a_walk_is(self):
        engine._settle_history_running = True
        self.st.set_meta(engine._SETTLE_HISTORY_MARKER, {"version": engine._SETTLE_HISTORY_VERSION,
                                                        "cursor": "2025-06-01T00:00:00"})
        self.assertTrue(engine.settle_history_walking())

    def test_a_walk_asked_for_but_not_begun_is(self):
        engine._settle_history_running = True
        engine._settle_history_rearm = True
        self.assertTrue(engine.settle_history_walking())

    def test_GUARD_nothing_running_is_not(self):
        self.assertFalse(engine.settle_history_walking())

    def test_the_route_asks_about_the_walk(self):
        from tests.test_server import server
        engine._settle_history_running = True
        with mock.patch.dict(sys.modules, {"engine": engine}), \
             mock.patch.object(server.store_access, "store", lambda: self.st), \
             server.app.test_client() as c:
            d = c.get("/api/settle-history-status").get_json()
        self.assertEqual((d["in_progress"], d["days_left"]), (False, None))

    def _words(self):
        src = open(os.path.join(os.path.dirname(engine.__file__), "web", "templates", "base.html"), encoding="utf-8").read()
        i = src.index("var ID = 'reprice-sweep'")
        return src[i:src.index("setInterval(check, 30000)", i)]

    def test_it_does_not_warn_against_a_restart(self):
        self.assertNotIn("avoid restarting", self._words())
        self.assertIn("carries on from where it stopped", self._words())

    def test_the_count_is_of_history_not_time(self):
        self.assertIn("' of history left)", self._words())


class TheCause(_Base):

    def _cause(self, snap):
        from tests.test_server import server
        self.st.set_kraken_state(engine._IMPORT_RUN_KEY, json.dumps(snap))
        with mock.patch.dict(sys.modules, {"engine": engine}):
            return server._settle_sweep_cause(self.st)

    def _now(self):
        from datetime import datetime
        return datetime.utcnow().isoformat()

    def test_a_repair_that_just_finished_is_not_the_cause(self):
        self.assertEqual(self._cause({"finished_at": self._now(), "gap": {"from": LO, "to": HI}}), "upgrade")

    def test_GUARD_an_import_that_just_finished_is(self):
        self.assertEqual(self._cause({"finished_at": self._now(), "gap": None}), "import")


class ARestart(_Base):

    def test_GUARD_the_walk_resumes_at_its_cursor(self):
        """What the banner now promises."""
        self.st.set_meta(engine._SETTLE_HISTORY_MARKER, {"version": engine._SETTLE_HISTORY_VERSION,
                                                        "cursor": "2025-07-01T00:00:00", "read": 4, "written": 0, "parts": {}})
        self.assertIn("if _settle_history_done() and not _settle_spans_pending():",
                      inspect.getsource(engine._maybe_settle_history))
        R._run(engine._settle_history_to_done(wait_for_schedule=True))
        self.assertEqual(self.ranges, [("2025-07-01T00:00:00", "2025-07-02T00:00:00")])
        self.assertEqual(self.st.get_meta(engine._SETTLE_HISTORY_MARKER)["read"], 5)


if __name__ == "__main__":
    unittest.main()
