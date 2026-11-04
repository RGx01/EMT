"""
Remove and the deletes, on the engine's thread and yielding to its loop (4 Nov 2026).

A b7 replace recomputed the house over a device's whole history straight through on the engine loop:
a 104 s stall, and HA's WebSocket dropped. Looking for the other callers of that recompute found:

- **Remove** (the device card's undo of a rebuild, `/api/historical/attribute/backout`) ran on the
  request thread, against the engine's store — which may be used only on the engine's thread. It failed
  every time on an install: "SQLite objects created in a thread can only be used in that same thread",
  answered as "The undo failed". (Reproduced on a copy of a support file's database.)
- **A range delete's** recompute ran on its worker thread against the engine's store: the same error,
  caught and logged as "remainder recompute failed", so the surviving devices were never re-derived.
- **Removing a device** ran its whole-history recompute on the loop in one piece (`_engine_write`),
  though its comment said it deliberately did not.

Now Remove runs on the engine loop, started and polled (`backout_recorder_attribution_async`, its result
on `/backout/status` as `last`); both deletes hand their recompute to the loop
(`_recompute_on_engine`); and every one of them is `recompute_remainders_for_window_async`, which
yields every 50 blocks.

Discriminating: every test here fails on the parent but the GUARD, marked.
"""
import asyncio
import os
import sys
import threading
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import engine                                       # noqa: E402
from tests.test_replace_device_history import _household, _rows   # noqa: E402

SERVER = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
PAGE = open(os.path.join(_ROOT, "web", "templates", "meter_config.html"), encoding="utf-8").read()


def _body(src, head):
    i = src.index(head)
    j = src.find("\ndef ", i + 1)
    k = src.find("\n@app.route", i + 1)
    ends = [x for x in (j, k) if x != -1]
    return src[i:min(ends)] if ends else src[i:]


class OnTheEngineThread(unittest.TestCase):
    """The engine's store opened on its loop's thread, as on an install; the caller elsewhere."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.t = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.t.start()

        async def make():
            return _household()
        self.st = asyncio.run_coroutine_threadsafe(make(), self.loop).result(10)
        self.p = [mock.patch.object(engine, "_store", self.st),
                  mock.patch.object(engine, "get_store", lambda: self.st)]
        for x in self.p:
            x.start()
        engine._backout_job.clear()
        engine._backout_job.update({"status": "idle"})

    def tearDown(self):
        for x in self.p:
            x.stop()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.t.join(5)

    def test_GUARD_the_store_refuses_another_thread(self):
        """What Remove ran into: the sync entry point, from this (a request's) thread."""
        res = engine.backout_recorder_attribution(meter_id="ev_charger")
        self.assertEqual(res, {"ok": False, "error": "backout_failed"})

    def test_remove_on_the_loop_takes_the_rebuilt_rows(self):
        res = asyncio.run_coroutine_threadsafe(
            engine.backout_recorder_attribution_async(meter_id="ev_charger"), self.loop).result(30)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["deleted"], 3)
        async def rows():
            return _rows(self.st)
        left = asyncio.run_coroutine_threadsafe(rows(), self.loop).result(10)
        self.assertEqual([r[0] for r in left.values()], [None])        # the live half-hour stays
        st = engine.api_backout_status()
        self.assertEqual((st["status"], st["last"]["deleted"], st["last"]["meter_id"]),
                         ("idle", 3, "ev_charger"))

    def test_a_new_remove_forgets_the_last_result(self):
        engine._backout_job["last"] = {"ok": True, "meter_id": "ev_charger", "deleted": 9}
        engine.backout_forget_last()
        self.assertNotIn("last", engine.api_backout_status())


class TheRoute(unittest.TestCase):

    def test_remove_is_started_on_the_engine_and_answered_at_once(self):
        from tests.test_server import make_client, server
        eng = sys.modules["engine"]
        started, forgot = [], []

        async def job(**kw):
            return kw
        with mock.patch.object(server, "_event_loop", mock.MagicMock(is_running=lambda: True)), \
             mock.patch.object(eng, "attribution_running", lambda meter_id=None: False, create=True), \
             mock.patch.object(eng, "backout_running", lambda: False, create=True), \
             mock.patch.object(eng, "backout_forget_last", lambda: forgot.append(1), create=True), \
             mock.patch.object(eng, "backout_recorder_attribution_async", job, create=True), \
             mock.patch.object(eng, "backout_recorder_attribution",
                               lambda **kw: self.fail("run on the request thread"), create=True), \
             mock.patch("asyncio.run_coroutine_threadsafe",
                        lambda coro, loop: (started.append(coro.cr_code.co_name), coro.close())):
            r = make_client().post("/api/historical/attribute/backout", json={"meter_id": "ev"})
        self.assertEqual(r.get_json(), {"ok": True, "status": "running"})
        self.assertEqual((started, forgot), (["job"], [1]))


class TheDeletes(unittest.TestCase):

    def test_a_range_delete_hands_its_recompute_to_the_engine(self):
        body = _body(SERVER, "def _delete_worker(")
        self.assertIn('result["remainders_recomputed"] = _recompute_on_engine(', body)
        self.assertNotIn("_eng.recompute_remainders_for_window(", body)

    def test_removing_a_device_recomputes_after_its_delete_yielding(self):
        body = _body(SERVER, "def _meter_delete_data(")
        self.assertIn('_n46 = _recompute_on_engine(_rc["parent"], _rc["lo"], _rc["hi"])', body)
        self.assertNotIn("_eng_d.recompute_remainders_for_window(", body)
        self.assertLess(body.index("deleted = _engine_write(_do_delete_and_recompute"),
                        body.index("_recompute_on_engine(_rc"))

    def test_the_recompute_on_the_engine_yields(self):
        body = _body(SERVER, "def _recompute_on_engine(")
        self.assertIn("_run_on_engine_loop(_eng.recompute_remainders_for_window_async(parent, lo, hi)", body)


class ThePage(unittest.TestCase):

    def test_remove_polls_for_its_result(self):
        i = PAGE.index("function _histRemoveLayer(")
        body = PAGE[i:PAGE.index("\n}\n", i)]
        self.assertIn("return (d.ok && d.status === 'running') ? _pollBackout(m) : d;", body)
        self.assertIn("'/api/historical/attribute/backout/status'", PAGE[PAGE.index("function _pollBackout("):i])


if __name__ == "__main__":
    unittest.main()
