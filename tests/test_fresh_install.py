"""
A new install, set up from scratch, as main.py runs it (3 Nov 2026).

A beta tester's from-scratch setup ended with no site and no main meter while the engine ticked
(#33–#35): the wizard's connect timed out on an Agile tariff's price history, a fresh install's save
dropped the supplier, and the page said "Finishing your upgrade (null days left)". Everything before
had been checked on a v4 import, and `.build/serve_local.py` — the local preview — runs no
`engine_startup`, so it could not show any of it: startup is what makes the new install's empty
config period, which sends the wizard's save down the in-place path.

Here: an empty data folder; `engine.setup()`, `store_access.bind` and the real `engine_startup` on an
engine loop of its own thread, with a Home Assistant of no sensors; the real web server's routes,
whose writes hop onto that loop. Then the wizard's requests, in its order: the connect, the mode,
the save — and the save's own engine restart. No network: the connect's Octopus side is stubbed.

A regression net for the from-scratch path rather than one fix's test: on the tree before #33
(389a984) test_the_connect_answers_before_the_rate_schedules, test_the_wizards_save_sets_up_the_meter
and test_no_upgrade_banner fail; test_startup_leaves_the_install_to_the_wizard is a GUARD (it held
then too).
"""
import asyncio
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "web")]

import engine                                     # noqa: E402
import instance                                   # noqa: E402
import store_access                               # noqa: E402

# What the wizard posts for an Octopus account on the API, no sensors (wizApplyAndClose).
WIZARD_CONFIG = {"meters": {"electricity_main": {
    "meta": {"type": "electricity", "site": "Test House", "supplier": "octopus", "billing_day": 1,
             "block_minutes": 30, "timezone": "Europe/London"},
    "channels": {"import": {"read": "", "rate": "", "rate_source": "api", "standing_charge_source": "api"},
                 "export": {"read": "", "rate": "", "rate_source": "api"}}}}}


class _NoSensorsHA:
    """Home Assistant with nothing in it: every state unknown, every call a no-op."""

    def subscribe_state(self, *a, **k):
        pass

    def get_state(self, entity_id):
        return None

    def __getattr__(self, name):
        async def _none(*a, **k):
            return None
        return _none


def _load_server():
    spec = importlib.util.spec_from_file_location("server_fresh", os.path.join(_ROOT, "web", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv


class AFreshInstall(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        share = os.path.join(self.dir, "share")
        os.makedirs(share)
        self.p = [mock.patch.object(engine, "DATA_DIR", self.dir),
                  mock.patch.object(engine, "CONFIG_PATH", os.path.join(self.dir, "meters_config.json")),
                  mock.patch.object(engine, "KRAKEN_CREDS_PATH", os.path.join(self.dir, "kraken_credentials.json")),
                  mock.patch.object(engine, "BLOCKS_PATH", os.path.join(self.dir, "blocks.json")),
                  mock.patch.object(engine, "BLOCKS_DB_PATH", os.path.join(self.dir, "blocks.db")),
                  mock.patch.object(engine, "SHARE_BACKUP_DIR", os.path.join(share, "energy_meter_tracker_backup")),
                  mock.patch.object(engine, "_store", None),
                  mock.patch.object(engine, "_kraken_client", None),
                  mock.patch.object(engine, "_kraken_discovery", None),
                  mock.patch.object(engine, "_kraken_rate_schedules", {}),
                  mock.patch.object(engine, "_stored_agreements", None),
                  mock.patch.object(instance, "DATA_DIR", self.dir),
                  mock.patch.object(instance, "_SHARE_ROOT", share),
                  mock.patch.object(instance, "LEGACY_SHARE_BACKUP_DIR",
                                    os.path.join(share, "energy_meter_tracker_backup")),
                  # its default `data_dir` is bound at definition, to /data
                  mock.patch.object(instance, "get_instance_id", lambda data_dir=None: "fresh-install-test")]
        for x in self.p:
            x.start()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.ha = _NoSensorsHA()
        engine.setup()
        store_access.reset_for_tests()
        self._on_loop(self._bind())
        self._on_loop(engine.engine_startup(self.ha), timeout=30)
        self.srv = _load_server()
        self.srv.DATA_DIR, self.srv._ha_client, self.srv._event_loop = self.dir, self.ha, self.loop
        self.client = self.srv.app.test_client()

    async def _bind(self):
        store_access.bind(engine.BLOCKS_DB_PATH)        # on the engine thread, as main.py does

    def tearDown(self):
        self._settle()
        getattr(engine, "_cancel_rate_refresh", lambda: None)()   # (absent before #33)
        self._on_loop(self._close())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()
        store_access.reset_for_tests()
        for x in self.p:
            x.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def _close(self):
        if engine._store is not None:
            engine._store.close()

    def _on_loop(self, coro, timeout=10):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def _settle(self, s=15.0):
        """Until no engine_startup runs (a save schedules one)."""
        t0 = time.time()
        while time.time() - t0 < s:
            if not engine._startup_lock.locked():
                time.sleep(0.05)
                if not engine._startup_lock.locked():
                    return
            time.sleep(0.05)
        self.fail("engine_startup still running after %.0f s" % s)

    def _db(self, sql):
        return self._on_loop(self._query(sql))

    async def _query(self, sql):
        return [tuple(r) for r in engine._store._conn.execute(sql).fetchall()]

    def _post(self, path, body):
        return self.client.post(path, data=json.dumps(body), content_type="application/json")

    # ── the install before the wizard ─────────────────────────────────────────────────────

    def test_startup_leaves_the_install_to_the_wizard(self):
        self.assertEqual(self._db("SELECT COUNT(*) FROM config_periods"), [(1,)])   # startup's own
        self.assertEqual(self._db("SELECT COUNT(*) FROM meters"), [(0,)])
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/welcome", r.headers["Location"])

    # ── the wizard: connect, mode, save ───────────────────────────────────────────────────

    def test_the_connect_answers_before_the_rate_schedules(self):
        gate = threading.Event()                    # an Agile price history: as long as we like

        async def _discover(force=False):
            engine._kraken_client = object()
            engine._kraken_discovery = {"account_number": "A-0000ABC7",
                                        "import": {"mpan": "2600000000001"},
                                        "export": {"mpan": "2600000000002"}}

        async def _slow_rates():
            while not gate.is_set():
                await asyncio.sleep(0.05)
        with mock.patch.object(engine, "save_kraken_credentials", lambda *a, **k: None), \
             mock.patch.object(engine, "_kraken_env", lambda: {"api_key": "k", "account_number": None,
                                                               "base_url": None}), \
             mock.patch.object(engine, "_kraken_startup_discovery", _discover), \
             mock.patch.object(engine, "_refresh_kraken_rate_schedules", _slow_rates):
            t0 = time.time()
            r = self._post("/api/kraken-config", {"api_key": "k", "account_number": None})
            took = time.time() - t0
            gate.set()
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(r.get_json()["connected"])
        self.assertLess(took, 5.0, "the wizard's credentials step waits 45 s and gives up")

    def test_the_wizards_save_sets_up_the_meter(self):
        # the credentials step has saved the key before the mode is posted
        with mock.patch.object(engine, "has_kraken_credentials", lambda: True):
            self.assertEqual(self._post("/api/data-source-mode",
                                        {"mode": "api", "supplier": "octopus"}).status_code, 200)
        before = engine._last_startup_complete_ts
        r = self._post("/api/config", WIZARD_CONFIG)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(r.get_json().get("ok"))
        t0 = time.time()                            # the save's own engine restart
        while engine._last_startup_complete_ts == before and time.time() - t0 < 15:
            time.sleep(0.05)
        self.assertNotEqual(engine._last_startup_complete_ts, before, "the save restarted no engine")
        self._settle()
        self.assertEqual(self._db("SELECT site_name, supplier FROM config_periods"),
                         [("Test House", "octopus")])
        self.assertEqual(self._db("SELECT meter_id FROM meters"), [("electricity_main",)])
        cfg = self.client.get("/api/config").get_json()
        self.assertEqual(list(cfg["meters"]), ["electricity_main"])
        self.assertEqual(cfg["meters"]["electricity_main"]["meta"]["site"], "Test House")
        self.assertNotIn("/welcome", self.client.get("/").headers.get("Location", ""))
        self.assertIn("test_house", engine.SHARE_BACKUP_DIR)     # the restart read the site

    def test_no_upgrade_banner(self):
        """Connected before the meter was set up: an import schedule, no history."""
        class _Sched:
            def is_empty(self):
                return False
        with mock.patch.object(engine, "_kraken_rate_schedules", {"import": _Sched()}):
            d = self.client.get("/api/settle-history-status").get_json()
        self.assertFalse(d["in_progress"], d)


if __name__ == "__main__":
    unittest.main()
