"""
BL-95 (1 Nov 2026) — the settlement pill as a way in.

While half-hours wait at a stage of settlement, the Charts pill is a way in: a click opens Data
Management with Issues only on (`?issues=1`, for that visit, the browser's own choice left as it
was); a right-click (a long press on touch) offers *Retry settlement* and *Data Management*.

*Retry settlement* is the 6-hourly poll's cycle run now (the user: "whatever the 6hrly poll normally
does") — the agreements, the rates, Octopus's kWh for the poll's window — and the 6 hours start again
from it. The poll waits on an event between cycles, which `POST /api/settlement/poll-now` sets. It is
refused with no API, while a cycle runs, and within 15 minutes of the last cycle's start, so clicks
cannot spend the account's allowance.

Checked in the local preview on 1 Nov 2026 (no API there, so the menu and the refusal; the cycle
itself by the loop test below).

Discriminating: every test fails on the parent tree, where none of it existed.
"""
import asyncio
import importlib.util
import os
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import engine                                     # noqa: E402

CHARTS = open(os.path.join(_ROOT, "web", "templates", "charts.html"), encoding="utf-8").read()
HEATMAP = open(os.path.join(_ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8").read()
DM = open(os.path.join(_ROOT, "web", "templates", "data_management.html"), encoding="utf-8").read()


def _fn(name):
    i = CHARTS.index("function " + name + "(")
    return CHARTS[i:CHARTS.index("\n}\n", i) + 2]


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class _Ingester:
    backfill_days = 1

    def __init__(self):
        self.polls = 0

    async def poll(self, **kw):
        self.polls += 1
        return {"window": ("a", "b"), "import_rows": 0, "stored": 0, "export_rows": 0,
                "export_stored": 0, "skipped_no_block": 0, "drifted": 0, "errors": []}


async def _nothing(*a, **k):
    return None


class ThePollWakes(unittest.TestCase):
    """The real poll loop, its Octopus side stubbed: a request runs a cycle at once."""

    def setUp(self):
        self.ing = _Ingester()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.p = [mock.patch.object(engine, "_kraken_client", object()),
                  mock.patch.object(engine, "_kraken_discovery", {"import": {"mpan": "2600000000001"}}),
                  mock.patch.object(engine, "_build_kraken_ingester", lambda: self.ing),
                  mock.patch.object(engine, "_maybe_rediscover", _nothing),
                  mock.patch.object(engine, "_refresh_kraken_rate_schedules", _nothing),
                  mock.patch.object(engine, "_maybe_settle_history", lambda: None),
                  mock.patch.object(engine, "_fetch_closed_agreement_prices", _nothing),
                  mock.patch.object(engine, "_maybe_run_settlement_sweep", _nothing),
                  mock.patch.object(engine, "delete_in_progress", lambda: False),
                  mock.patch.object(engine, "_KRAKEN_POLL_NOW_MIN_S", 0)]
        for x in self.p:
            x.start()
        engine._kraken_poll_state.update(running=False, started=None, finished=None)
        self.task = asyncio.run_coroutine_threadsafe(self._start(), self.loop).result(5)

    async def _start(self):
        t = asyncio.ensure_future(engine.kraken_poll_task(None))
        engine._kraken_poll_task_handle = t
        return t

    def tearDown(self):
        self.loop.call_soon_threadsafe(self.task.cancel)
        time.sleep(0.05)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        for x in self.p:
            x.stop()
        engine._kraken_poll_task_handle = None
        engine._kraken_poll_wake = None
        engine._kraken_poll_state.update(running=False, started=None, finished=None)

    def _until(self, cond, s=3.0):
        t0 = time.time()
        while time.time() - t0 < s:
            if cond():
                return True
            time.sleep(0.02)
        return False

    def test_a_request_runs_a_cycle_now(self):
        self.assertTrue(self._until(lambda: self.ing.polls == 1 and engine._kraken_poll_state["finished"]))
        ok, why = engine.request_kraken_poll(self.loop)
        self.assertTrue(ok, why)
        self.assertTrue(self._until(lambda: self.ing.polls == 2), "the 6-hour sleep was not woken")

    def test_without_a_request_it_sleeps(self):
        self.assertTrue(self._until(lambda: self.ing.polls == 1))
        time.sleep(0.3)
        self.assertEqual(self.ing.polls, 1)


class TheRefusals(unittest.TestCase):

    def setUp(self):
        self.loop = mock.Mock()
        self.handle = mock.Mock(done=lambda: False)
        self.p = [mock.patch.object(engine, "_kraken_client", object()),
                  mock.patch.object(engine, "_kraken_discovery", {"import": {}}),
                  mock.patch.object(engine, "_kraken_poll_task_handle", self.handle),
                  mock.patch.object(engine, "_kraken_poll_wake", mock.Mock())]
        for x in self.p:
            x.start()
        engine._kraken_poll_state.update(running=False, started=None, finished=None)

    def tearDown(self):
        for x in self.p:
            x.stop()
        engine._kraken_poll_state.update(running=False, started=None, finished=None)

    def test_with_no_api(self):
        with mock.patch.object(engine, "_kraken_client", None):
            self.assertEqual(engine.request_kraken_poll(self.loop), (False, "no supplier API connected"))
        self.loop.call_soon_threadsafe.assert_not_called()

    def test_while_a_cycle_runs(self):
        engine._kraken_poll_state["running"] = True
        self.assertEqual(engine.request_kraken_poll(self.loop), (False, "a poll is running"))

    def test_within_fifteen_minutes(self):
        engine._kraken_poll_state["started"] = (_now() - timedelta(minutes=10)).isoformat()
        ok, why = engine.request_kraken_poll(self.loop)
        self.assertFalse(ok)
        self.assertIn("under 15 minutes ago", why)
        engine._kraken_poll_state["started"] = (_now() - timedelta(minutes=16)).isoformat()
        self.assertEqual(engine.request_kraken_poll(self.loop), (True, ""))
        self.loop.call_soon_threadsafe.assert_called_once()

    def test_the_pace_is_fifteen_minutes(self):
        self.assertEqual(engine._KRAKEN_POLL_NOW_MIN_S, 15 * 60)


class TheRoute(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("server_bl95", os.path.join(_ROOT, "web", "server.py"))
        cls.srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.srv)

    def test_post_asks_the_engine_and_says_since_when(self):
        loop = mock.Mock(is_running=lambda: True)
        engine._kraken_poll_state.update(running=False, started="2026-11-01T06:00:00", finished=None)
        try:
            with mock.patch.object(self.srv, "_event_loop", loop), \
                 mock.patch.object(engine, "request_kraken_poll", lambda lp: (True, "")) as _r:
                r = self.srv.app.test_client().post("/api/settlement/poll-now")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json(), {"ok": True, "status": "running", "since": "2026-11-01T06:00:00"})
            with mock.patch.object(self.srv, "_event_loop", loop), \
                 mock.patch.object(engine, "request_kraken_poll", lambda lp: (False, "a poll is running")):
                r = self.srv.app.test_client().post("/api/settlement/poll-now")
            self.assertEqual(r.status_code, 409)
            self.assertEqual(self.srv.app.test_client().get("/api/settlement/poll-now").get_json()["started"],
                             "2026-11-01T06:00:00")
        finally:
            engine._kraken_poll_state.update(running=False, started=None, finished=None)


class ThePage(unittest.TestCase):

    def test_only_while_half_hours_wait(self):
        self.assertIn("amber = true; actionable = true;", CHARTS)
        self.assertIn("el.classList.toggle('actionable', actionable);", CHARTS)
        self.assertIn("settleNoteWire(document.getElementById('dcc-settle-note'));", CHARTS)

    def test_a_click_opens_the_issues(self):
        self.assertIn("function settleOpenDM() { window.location.href = BASE_PATH + '/data-management?issues=1'; }",
                      CHARTS)
        w = _fn("settleNoteWire")
        self.assertIn("if (el.classList.contains('actionable') && !settleNoteBusy) settleOpenDM();", w)
        self.assertIn("if (pressed) { pressed = false; return; }", w)    # a long press's lift

    def test_a_right_click_or_long_press_the_menu(self):
        w = _fn("settleNoteWire")
        self.assertIn("e.preventDefault(); settleMenu(el, e.clientX, e.clientY);", w)
        self.assertIn("pressed = true; settleMenu(el, t0.x, t0.y); }, 550);", w)
        self.assertIn("[['Retry settlement', function() { settleRetry(el); }], ['Data Management', settleOpenDM]]",
                      _fn("settleMenu"))

    def test_the_retry_asks_and_waits_for_its_cycle(self):
        r = _fn("settleRetry")
        self.assertIn("fetch(apiUrl('/api/settlement/poll-now'), {method: 'POST'})", r)
        self.assertIn("if ((s.started === since || s.running) && Date.now() - t0 < 10 * 60000) { setTimeout(wait, 3000); return; }", r)

    def test_issues_only_for_the_visit(self):
        self.assertIn("issues: q.get('issues') === '1',", DM)
        i = HEATMAP.index("if (opts.issues) state.collapse = true;")
        self.assertGreater(i, HEATMAP.index("state.collapse = localStorage.getItem('emt_hm_collapse') !== '0';"))


if __name__ == "__main__":
    unittest.main()
