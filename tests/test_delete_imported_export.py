"""
Deleting an imported export (soft delete design §4, PR 1, 19 Oct 2026).

The export of IMPORTED main-meter history over whole LOCAL days is blanked, after a required backup
(the delete job), with its carbon recomputed from import alone; live and settled blocks are not
touched, and nothing refills it but the CSV "Export only" overlay:
- the settlement poll never writes onto an imported block (`_UNSETTLED_WHERE`);
- whole days keep `zero_fill_imported_export_blanks` away (it fills only a day that still holds
  some imported export);
- the overlay is first-man-wins per channel, so it fills exactly the emptied channel.
The heatmap offers it on every view but the Import bill.

Discriminating (fail on the parent: none of it exists), except the GUARDs marked.
"""
import json
import os
import sys
import time
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from block_store import BlockStore      # noqa: E402

TZ = "Europe/London"


def _st():
    st = BlockStore(":memory:")
    st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, "
                     "timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'Europe/London')")
    rows = [  # 1 Jun 2026 is BST: its local day is 31 May 23:00 → 1 Jun 23:00 UTC
        ("2026-05-31T22:30:00", "imported_csv", 1.0, 0.5),    # 31 May local: kept
        ("2026-05-31T23:00:00", "imported_csv", 1.0, 0.5),    # 1 Jun local 00:00
        ("2026-06-01T12:00:00", "imported_api", 0.2, 1.4),    # 1 Jun local 13:00
        ("2026-06-01T12:30:00", None, 0.3, 1.1),              # live, 1 Jun: kept
        ("2026-06-01T23:00:00", "imported_csv", 1.0, 0.5),    # 2 Jun local: kept
    ]
    for bs, src, imp, exp in rows:
        st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "exp_kwh, exp_rate, exp_cost, carbon_intensity_g, carbon_g, source) VALUES "
            "(?, ?, 'electricity_main', 1, ?, ?, 0.15, ?, 100, ?, ?)",
            (bs, bs, imp, exp, round(exp * 0.15, 6), round((imp - exp) * 100, 4), src))
    st._conn.commit()
    return st


def _row(st, bs):
    return st._conn.execute("SELECT * FROM blocks WHERE block_start = ?", (bs,)).fetchone()


class TheStore(unittest.TestCase):

    def test_the_preview_counts_the_imported_and_the_live_left_alone(self):
        p = _st().count_imported_export("2026-06-01", "2026-06-01", TZ)
        self.assertEqual((p["from_utc"], p["to_utc"]), ("2026-05-31T23:00:00", "2026-06-01T23:00:00"))
        self.assertEqual((p["half_hours"], p["live_kept"], p["kwh"]), (2, 1, 1.9))

    def test_it_blanks_the_imported_export_of_whole_local_days(self):
        st = _st()
        self.assertEqual(st.delete_imported_export("2026-06-01", "2026-06-01", TZ)["half_hours"], 2)
        for bs in ("2026-05-31T23:00:00", "2026-06-01T12:00:00"):
            r = _row(st, bs)
            self.assertEqual((r["exp_kwh"], r["exp_rate"], r["exp_cost"], r["exp_kwh_api"]),
                             (None, None, None, None))
            self.assertIsNotNone(r["imp_kwh"])                          # import kept

    def test_carbon_is_from_import_alone(self):
        st = _st()
        st.delete_imported_export("2026-06-01", "2026-06-01", TZ)
        self.assertAlmostEqual(_row(st, "2026-06-01T12:00:00")["carbon_g"], 20.0)   # 0.2 × 100

    def test_live_blocks_and_other_days_are_untouched(self):
        st = _st()
        st.delete_imported_export("2026-06-01", "2026-06-01", TZ)
        self.assertEqual(_row(st, "2026-06-01T12:30:00")["exp_kwh"], 1.1)          # live
        self.assertEqual(_row(st, "2026-05-31T22:30:00")["exp_kwh"], 0.5)          # 31 May
        self.assertEqual(_row(st, "2026-06-01T23:00:00")["exp_kwh"], 0.5)          # 2 Jun

    def test_the_zero_fill_leaves_a_wholly_deleted_day_empty(self):
        """GUARD of why whole days: the zero-fill fills only a day still holding imported export
        (fails on the parent only by the missing delete)."""
        st = _st()
        st.delete_imported_export("2026-06-01", "2026-06-01", TZ)
        st.zero_fill_imported_export_blanks(TZ)
        self.assertIsNone(_row(st, "2026-06-01T12:00:00")["exp_kwh"])

    def test_the_poll_never_chases_an_imported_block(self):
        """GUARD: the settlement poll's selector excludes imported blocks, so nothing refills it."""
        self.assertIn("source NOT LIKE 'imported%'", BlockStore._UNSETTLED_WHERE)

    def test_the_csv_overlay_refills_it(self):
        st = _st()
        st.delete_imported_export("2026-06-01", "2026-06-01", TZ)
        _, wrote = st.upsert_imported_block("2026-06-01T12:00:00", "electricity_main", "export",
                                            kwh=0.9, rate=0.15, cost=0.135)
        self.assertTrue(wrote)
        self.assertEqual(_row(st, "2026-06-01T12:00:00")["exp_kwh"], 0.9)


class TheRoutes(unittest.TestCase):

    def setUp(self):
        from tests.test_server import make_client, server
        self.server = server
        self.st = _st()
        self.client = make_client(store=self.st)
        eng = sys.modules["engine"]
        self.p = [mock.patch.object(server.store_access, "store", lambda: self.st),
                  mock.patch.object(server, "_own_store", lambda: self.st),
                  mock.patch.object(server, "_release_store", lambda st: None),
                  mock.patch.object(server, "_site_tz", lambda: TZ),
                  mock.patch.object(server, "_stop_running_import", lambda *a, **k: True),
                  mock.patch.object(server, "_safety_backup", lambda label: "%s.zip" % label),
                  mock.patch.object(eng, "delete_in_progress", create=True, return_value=False),
                  mock.patch.object(eng, "poll_in_progress", create=True, return_value=False),
                  mock.patch.object(eng, "set_delete_active", create=True)]
        for x in self.p:
            x.start()
        server._delete_job.clear()
        server._delete_job["status"] = "idle"

    def tearDown(self):
        for x in self.p:
            x.stop()
        self.server._delete_job.clear()
        self.server._delete_job["status"] = "idle"

    def _post(self, url, body):
        r = self.client.post(url, data=json.dumps(body), content_type="application/json")
        return r.status_code, json.loads(r.data)

    def test_the_preview(self):
        code, d = self._post("/api/blocks/delete-export/preview", {"from_date": "2026-06-01"})
        self.assertEqual((code, d["half_hours"], d["live_kept"]), (200, 2, 1))

    def test_unconfirmed_is_refused(self):
        self.assertEqual(self._post("/api/blocks/delete-export", {"from_date": "2026-06-01"})[0], 400)

    def test_it_runs_as_the_delete_job_after_its_backup(self):
        class Inline:      # the worker inline: an in-memory store is one thread's
            def __init__(self, target, args=(), kwargs=None, **k):
                self.f, self.a, self.k = target, args, kwargs or {}
            def start(self):
                self.f(*self.a, **self.k)
        with mock.patch.object(self.server.threading, "Thread", Inline):
            code, _ = self._post("/api/blocks/delete-export",
                                 {"from_date": "2026-06-01", "to_date": "2026-06-01", "confirmed": True})
        self.assertEqual(code, 200)
        for _ in range(50):
            if self.server._delete_job.get("status") != "running":
                break
            time.sleep(0.05)
        j = self.server._delete_job
        self.assertEqual((j["status"], j["kind"], j["backup"]), ("done", "delete_export",
                                                                 "pre-delete-export.zip"))
        self.assertEqual(j["result"]["half_hours"], 2)
        self.assertIsNone(_row(self.st, "2026-06-01T12:00:00")["exp_kwh"])

    def test_refused_while_a_delete_runs(self):
        self.server._delete_job["status"] = "running"
        self.assertEqual(self._post("/api/blocks/delete-export",
                                    {"from_date": "2026-06-01", "confirmed": True})[0], 409)


class TheHeatmap(unittest.TestCase):
    _SRC = open(os.path.join(_ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8").read()

    def _fn(self, name):
        i = self._SRC.index("function " + name + "(")
        return self._SRC[i:self._SRC.index("\n  }\n", i)]

    def test_offered_on_every_view_but_the_import_bill(self):
        self.assertIn("if (state.metric === 'bill') return null;", self._fn("exportDeleteItem"))
        self.assertEqual(self._SRC.count("exportDeleteItem("), 4)          # the item + 3 menus

    def test_preview_then_delete_then_redraw(self):
        body = self._fn("openExportDelete")
        self.assertIn("postJSON('/api/blocks/delete-export/preview'", body)
        self.assertIn("postJSON('/api/blocks/delete-export', {from_date: a, to_date: b, confirmed: true})", body)
        self.assertIn("reloadRange(p.from_utc, p.to_utc);", body)
        self.assertIn("nothing to delete", body)


if __name__ == "__main__":
    unittest.main()
