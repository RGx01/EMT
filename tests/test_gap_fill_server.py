"""
Gap fill as an action on the heatmap's selection — the server (gap fill design §2–§4, PR 1).

- `engine.gap_fill_plan` counts, from the store, what filling the selection's runs would do:
  missing half-hours, the deleted among them, those too recent (under 48 h: left out, decided (b))
  and too old (beyond Octopus's two years: left out, decided (a)), and the fillable ones as runs.
- `/api/gaps/fill/preview` answers with that and a token; `/api/gaps/fill/apply` re-counts, takes a
  REQUIRED safety backup and runs `engine.run_gap_fill_runs` over the fillable runs.
- `run_gap_fill_runs` fills the runs newest first, each lifting its own tombstones, and finishes
  once; `run_gap_fill_job` (the Historical import page's) is unchanged — its window and finish are
  the same code, now apart.

- `engine.history_reach_from` gives the heatmap's "import your history" row its start: the
  earliest channel floor, no older than two years, and None once EMT's oldest block reaches it.
  `/api/periods` answers it as `bounds.reach_from`.

All new code, so on the parent every test fails by name. `test_the_historical_import_fill_is_unchanged`
is a guard of the split (it patches the split's helpers, so it cannot run on the parent either).
"""
import asyncio
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine as real_engine                     # noqa: E402  (before test_server stubs it)
from block_store import BlockStore               # noqa: E402

NOW = datetime(2026, 10, 18, 12, 0)


def _st(present, tombs=()):
    st = BlockStore(":memory:")
    st._conn.execute("INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, "
                     "block_minutes, timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
    for s in present:
        st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                         "imp_kwh) VALUES (?,?, 'electricity_main', 1, 0.4)", (s, s))
    for a, b in tombs:
        st.add_deleted_range("*", a, b, reason="user_delete")
    st._conn.commit()
    return st


def _hh(start, n):
    t = datetime.fromisoformat(start)
    return [(t + timedelta(minutes=30 * i)).isoformat() for i in range(n)]


class ThePlan(unittest.TestCase):

    def _plan(self, st, runs):
        return real_engine.gap_fill_plan(st, runs, now=NOW)

    def test_missing_half_hours_become_fillable_runs(self):
        # 10:00..12:00 with 10:30 present: two runs, 10:00 and 11:00-12:00
        st = _st(["2026-10-10T10:30:00"])
        p = self._plan(st, [["2026-10-10T10:00:00", "2026-10-10T12:00:00"]])
        self.assertEqual(p["total"]["missing"], 3)
        self.assertEqual(p["fill_runs"], [["2026-10-10T10:00:00", "2026-10-10T10:30:00"],
                                          ["2026-10-10T11:00:00", "2026-10-10T12:00:00"]])

    def test_deleted_are_counted_and_still_filled(self):
        st = _st([], tombs=[("2026-10-10T10:00:00", "2026-10-10T11:00:00")])
        p = self._plan(st, [["2026-10-10T10:00:00", "2026-10-10T12:00:00"]])
        self.assertEqual((p["total"]["deleted"], p["total"]["fillable"]), (2, 4))

    def test_the_last_48_hours_are_left_out(self):
        st = _st([])
        p = self._plan(st, [["2026-10-16T10:00:00", "2026-10-16T14:00:00"]])   # 12:00 is 48 h ago
        self.assertEqual((p["total"]["too_recent"], p["total"]["fillable"]), (4, 4))
        self.assertEqual(p["fill_runs"], [["2026-10-16T10:00:00", "2026-10-16T12:00:00"]])

    def test_beyond_two_years_is_left_out(self):
        st = _st([])
        p = self._plan(st, [["2024-10-01T00:00:00", "2024-10-01T01:00:00"]])
        self.assertEqual((p["total"]["too_old"], p["fill_runs"]), (2, []))

    def test_the_size_is_per_channel(self):
        st = _st([])
        p = real_engine.gap_fill_plan(st, [["2026-06-07T23:00:00", "2026-06-30T23:00:00"]], now=NOW,
                                      channels=("import", "export"))
        self.assertEqual(p["size"]["import"], {"half_hours": 1104, "chunks": 1})
        self.assertEqual(p["size"]["export"], {"half_hours": 1104, "chunks": 1})


class TheRoutes(unittest.TestCase):

    def setUp(self):
        from tests.test_server import make_client, server
        self.server = server
        self.st = _st(["2026-10-10T10:30:00"])
        self.client = make_client(store=self.st)
        eng = sys.modules["engine"]
        self.p = [mock.patch.object(eng, "kraken_available", create=True, return_value=True),
                  mock.patch.object(eng, "gap_fill_plan", create=True,
                                    side_effect=lambda st, runs, **k: real_engine.gap_fill_plan(
                                        st, runs, now=NOW, **k)),
                  mock.patch.object(eng, "api_import_running", create=True, return_value=False),
                  mock.patch.object(eng, "run_gap_fill_runs", create=True,
                                    side_effect=lambda runs, **k: ("runner", runs, k)),
                  mock.patch.object(server, "_backfill_verify_active", return_value=False),
                  mock.patch.object(server, "_main_has_export", return_value=False),
                  # the request thread's reader: the test store (an in-memory DB is per connection)
                  mock.patch.object(server.store_access, "store", lambda: self.st)]
        for x in self.p:
            x.start()
        self.runs = [["2026-10-10T10:00:00", "2026-10-10T12:00:00"]]

    def tearDown(self):
        for x in self.p:
            x.stop()
        self.server._gap_fill_preview.clear()

    def _post(self, url, body):
        r = self.client.post(url, data=json.dumps(body), content_type="application/json")
        return r.status_code, json.loads(r.data)

    def test_the_preview_counts_and_gives_a_token(self):
        code, d = self._post("/api/gaps/fill/preview", {"runs": self.runs})
        self.assertEqual(code, 200)
        self.assertEqual(d["total"]["fillable"], 3)
        self.assertTrue(d["token"])
        self.assertEqual(list(d["size"]), ["import"])

    def test_bad_runs_are_refused(self):
        self.assertEqual(self._post("/api/gaps/fill/preview", {"runs": [["b", "a"]]})[0], 400)

    def test_apply_without_a_backup_fills_nothing(self):
        _, d = self._post("/api/gaps/fill/preview", {"runs": self.runs})
        scheduled = []
        with mock.patch.object(self.server, "_safety_backup", side_effect=RuntimeError("disk")), \
             mock.patch.object(self.server, "_event_loop", mock.MagicMock()), \
             mock.patch("asyncio.run_coroutine_threadsafe", side_effect=lambda c, l: scheduled.append(c)):
            code, r = self._post("/api/gaps/fill/apply", {"token": d["token"]})
        self.assertEqual(code, 500)
        self.assertIn("no safety backup", r["error"])
        self.assertEqual(scheduled, [])

    def test_apply_runs_the_preview_s_fillable_runs(self):
        _, d = self._post("/api/gaps/fill/preview", {"runs": self.runs})
        scheduled = []
        with mock.patch.object(self.server, "_safety_backup", return_value="pre.zip"), \
             mock.patch.object(self.server, "_event_loop", mock.MagicMock()), \
             mock.patch("asyncio.run_coroutine_threadsafe", side_effect=lambda c, l: scheduled.append(c)):
            code, r = self._post("/api/gaps/fill/apply", {"token": d["token"]})
        self.assertEqual((code, r["backup"], r["runs"]), (200, "pre.zip", 2))
        self.assertEqual(scheduled[0][1], d["fill_runs"])

    def test_a_changed_selection_is_refused(self):
        _, d = self._post("/api/gaps/fill/preview", {"runs": self.runs})
        self.st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                              "imp_kwh) VALUES ('2026-10-10T11:00:00','x','electricity_main',1,0.4)")
        self.st._conn.commit()
        with mock.patch.object(self.server, "_event_loop", mock.MagicMock()):
            code, r = self._post("/api/gaps/fill/apply", {"token": d["token"]})
        self.assertEqual(code, 409)
        self.assertIn("changed since the preview", r["error"])

    def test_an_unknown_token_is_refused(self):
        self.assertEqual(self._post("/api/gaps/fill/apply", {"token": "nope"})[0], 409)


class TheHistoryReach(unittest.TestCase):

    def _reach(self, disc, earliest, floors=None):
        with mock.patch.object(real_engine, "kraken_available", return_value=True), \
             mock.patch.object(real_engine, "_kraken_discovery", disc), \
             mock.patch.dict(real_engine._hist_floor_cache, floors or {}, clear=True), \
             mock.patch.object(real_engine, "_retention_floor_iso", return_value="2024-10-18T12:00:00"):
            return real_engine.history_reach_from(earliest)

    @staticmethod
    def _ch(*froms):
        return {"mpan": "x", "agreements": [{"valid_from": f} for f in froms]}

    def test_the_earliest_agreement_of_either_channel(self):
        disc = {"import": self._ch("2025-03-01T00:00:00Z"), "export": self._ch("2025-01-10T00:00:00Z")}
        self.assertEqual(self._reach(disc, "2026-09-01T00:00:00"), "2025-01-10T00:00:00")

    def test_a_resolved_data_floor_wins_over_the_agreement(self):
        disc = {"import": self._ch("2025-03-01T00:00:00Z")}
        self.assertEqual(self._reach(disc, "2026-09-01T00:00:00", {"import": "2025-02-01T00:00:00"}),
                         "2025-02-01T00:00:00")

    def test_no_older_than_two_years_on_a_half_hour(self):
        disc = {"import": self._ch("2019-01-01T00:00:00Z")}
        self.assertEqual(self._reach(disc, "2026-09-01T00:00:00"), "2024-10-18T12:00:00")
        with mock.patch.object(real_engine, "kraken_available", return_value=True), \
             mock.patch.object(real_engine, "_kraken_discovery", disc), \
             mock.patch.object(real_engine, "_retention_floor_iso", return_value="2024-10-18T12:07:41"):
            self.assertEqual(real_engine.history_reach_from("2026-09-01T00:00:00"), "2024-10-18T12:30:00")

    def test_none_once_the_oldest_block_reaches_it(self):
        disc = {"import": self._ch("2025-03-01T00:00:00Z")}
        self.assertIsNone(self._reach(disc, "2025-03-01T00:00:00"))

    def test_none_without_the_api(self):
        with mock.patch.object(real_engine, "kraken_available", return_value=False):
            self.assertIsNone(real_engine.history_reach_from("2026-09-01T00:00:00"))

    def test_the_periods_bounds_carry_it(self):
        from tests.test_server import make_client, server
        st = _st(["2026-10-10T10:30:00"])
        client = make_client(store=st)
        eng = sys.modules["engine"]
        with mock.patch.object(eng, "history_reach_from", create=True,
                               side_effect=lambda e: "2025-01-10T00:00:00" if e else None), \
             mock.patch.object(server.store_access, "store", lambda: st):
            d = json.loads(client.get("/api/periods?kind=month").data)
        self.assertEqual(d["bounds"]["reach_from"], "2025-01-10T00:00:00")


class TheRunner(unittest.TestCase):

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def setUp(self):
        self.calls = []
        self.st = _st([])
        self.p = [mock.patch.object(real_engine, "_store", self.st),
                  mock.patch.object(real_engine, "_reset_pricing_health", lambda: None),
                  mock.patch.object(real_engine, "_gap_fill_window", self._window),
                  mock.patch.object(real_engine, "_gap_fill_finish", self._finish)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    async def _window(self, j, a, b, **k):
        self.calls.append(("window", a, b))
        return not getattr(self, "fail_at", None) == a

    async def _finish(self, j, channels):
        self.calls.append(("finish",))

    def test_runs_are_filled_newest_first_and_finished_once(self):
        self._run(real_engine.run_gap_fill_runs(
            [["2026-06-08T00:00:00", "2026-06-09T00:00:00"], ["2026-07-14T23:00:00", "2026-07-14T23:30:00"]]))
        self.assertEqual(self.calls, [("window", "2026-07-14T23:00:00", "2026-07-14T23:30:00"),
                                      ("window", "2026-06-08T00:00:00", "2026-06-09T00:00:00"),
                                      ("finish",)])

    def test_each_run_lifts_its_tombstones(self):
        self.st.add_deleted_range("*", "2026-06-08T00:00:00", "2026-06-09T00:00:00")
        self._run(real_engine.run_gap_fill_runs([["2026-06-08T00:00:00", "2026-06-09T00:00:00"]]))
        self.assertEqual(self.st.get_deleted_ranges(), [])
        self.assertEqual(real_engine._api_import_job["runs"][0]["lifted"], 1)

    def test_a_stopped_run_does_not_finish(self):
        self.fail_at = "2026-07-14T23:00:00"
        self._run(real_engine.run_gap_fill_runs(
            [["2026-06-08T00:00:00", "2026-06-09T00:00:00"], ["2026-07-14T23:00:00", "2026-07-14T23:30:00"]]))
        self.assertNotIn(("finish",), self.calls)

    def test_the_historical_import_fill_is_unchanged(self):
        """GUARD: one window, then the finish."""
        self._run(real_engine.run_gap_fill_job("2026-06-08T00:00:00", "2026-06-09T00:00:00"))
        self.assertEqual(self.calls, [("window", "2026-06-08T00:00:00", "2026-06-09T00:00:00"),
                                      ("finish",)])


if __name__ == "__main__":
    unittest.main()
