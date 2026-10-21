"""
Data Management's page round the data heatmap (data management design §1 (2), (4); PR 2;
21 Oct 2026).

- The health strip: `/api/data-health` counts each data view's half-hours by state over the whole
  history, keyed `state` or `state|flags` with the bill flags the heatmap folds into a cell, and the
  page counts them with the heatmap's own `isIssue` (EMTHeatmap.issueCount) — on a copy of staging's
  database the strip and the grid's summed "To look at" agreed exactly (79, 90, 39). The missing
  half-hours as the Missing view counts them: interior gaps of any age and the tail.
- The Whole history card: Historical Import's own flows in a modal, on a task (`?task=`).
- The purge's backup is required: it carried on without one (on the copy: refused, nothing deleted).
- The Fill History & Gaps link and the Missing Data nudge go.

Discriminating (fail on the parent: none of it exists), except the GUARD marked.
"""
import json
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from block_store import BlockStore      # noqa: E402

_T = os.path.join(_ROOT, "web", "templates")


def _page(n):
    return open(os.path.join(_T, n), encoding="utf-8").read()


def _st(starts):
    st = BlockStore(":memory:")
    st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, "
                     "timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
    for bs in starts:
        st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                         "imp_kwh) VALUES (?, ?, 'electricity_main', 1, 0.5)", (bs, bs))
    st._conn.commit()
    return st


class TheMissingCount(unittest.TestCase):

    def test_interior_gaps_of_any_age_and_the_tail(self):
        from tests.test_server import server
        st = _st(["2026-10-21T08:00:00", "2026-10-21T09:00:00"])    # 08:30 missing, 2 h ago
        got = server._missing_half_hours(st, "UTC", now=datetime(2026, 10, 21, 10, 10))
        # 08:30 (interior, recent) + 09:30 (the tail; 10:00 is not finished at 10:10)
        self.assertEqual((got["half_hours"], got["gaps"]), (2, 2))
        self.assertEqual(got["newest"], "2026-10-21T09:30:00")

    def test_none(self):
        """GUARD (fails on the parent only by the new function)."""
        from tests.test_server import server
        st = _st(["2026-10-21T09:00:00", "2026-10-21T09:30:00"])
        self.assertEqual(server._missing_half_hours(st, "UTC", now=datetime(2026, 10, 21, 10, 10))["half_hours"], 0)


class TheHealthCall(unittest.TestCase):

    def setUp(self):
        from tests.test_server import make_client, server
        self.server = server
        self.st = _st(["2026-10-21T08:00:00", "2026-10-21T08:30:00", "2026-10-21T09:00:00"])
        self.client = make_client(store=self.st)
        server._health_cache["key"] = None              # each test computes its own
        eng = sys.modules["engine"]
        states = {"2026-10-21T08:00:00": {"bill": "applied", "export": "applied", "flags": []},
                  "2026-10-21T08:30:00": {"bill": "applied", "export": "waiting",
                                          "flags": ["vat_disagrees", "not_folded"]},
                  "2026-10-21T09:00:00": {"bill": "overdue", "export": "waiting", "flags": []}}
        self.p = [mock.patch.object(server.store_access, "store", lambda: self.st),
                  mock.patch.object(server, "_settle_views", lambda e: ["bill", "export"]),
                  mock.patch.object(server, "_settlement_states", lambda s, a, b: states),
                  mock.patch.object(server, "_missing_half_hours",
                                    lambda s, tz: {"half_hours": 0, "gaps": 0, "newest": None})]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_counts_by_state_with_the_folded_flags(self):
        d = json.loads(self.client.get("/api/data-health").data)
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["views"]["bill"], {"applied": 1, "applied|vat_disagrees": 1, "overdue": 1})
        self.assertEqual(d["views"]["export"], {"applied": 1, "waiting": 2})

    def test_only_the_views_the_install_has(self):
        with mock.patch.object(self.server, "_settle_views", lambda e: []):
            d = json.loads(self.client.get("/api/data-health").data)
        self.assertEqual(d["views"], {})


class TheHealthCache(unittest.TestCase):
    """Staging, 21 Oct 2026: every page load classified the whole history again — four at once held
    the web threads and the engine loop. Kept until the data moves or ten minutes pass; one request
    computes, the rest wait. Discriminating: the first three fail on the parent."""

    def setUp(self):
        from tests.test_server import make_client, server
        self.server = server
        self.st = _st(["2026-10-21T08:00:00", "2026-10-21T08:30:00"])
        self.client = make_client(store=self.st)
        server._health_cache["key"] = None
        self.calls = []

        def states(s, a, b):
            self.calls.append(1)
            return {"2026-10-21T08:00:00": {"bill": "applied", "flags": []}}
        self.p = [mock.patch.object(server.store_access, "store", lambda: self.st),
                  mock.patch.object(server, "_settle_views", lambda e: ["bill"]),
                  mock.patch.object(server, "_settlement_states", states),
                  mock.patch.object(server, "_missing_half_hours",
                                    lambda s, tz: {"half_hours": 0, "gaps": 0, "newest": None})]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def _get(self):
        return json.loads(self.client.get("/api/data-health").data)

    def test_a_second_load_does_not_classify_again(self):
        a, b = self._get(), self._get()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(a["views"], b["views"])

    def test_a_write_classifies_again(self):
        self._get()
        with mock.patch.object(self.server, "_blocks_data_version", lambda s: "moved"):
            self._get()
        self.assertEqual(len(self.calls), 2)

    def test_concurrent_loads_classify_once(self):
        import threading
        gate = threading.Event()
        real = self.server._settlement_states

        def slow(s, a, b):
            gate.wait(2)
            return real(s, a, b)
        out = []
        # one SQLite connection cannot cross threads: a stand-in store, the token fixed
        span = mock.MagicMock()
        span.block_span.return_value = ("2026-10-21T08:00:00", "2026-10-21T08:30:00")
        with mock.patch.object(self.server, "_settlement_states", slow), \
             mock.patch.object(self.server.store_access, "store", lambda: span), \
             mock.patch.object(self.server, "_blocks_data_version", lambda s: "k"):
            ts = [threading.Thread(target=lambda: out.append(
                self.server.app.test_client().get("/api/data-health").status_code)) for _ in range(4)]
            for t in ts:
                t.start()
            gate.set()
            for t in ts:
                t.join(10)
        self.assertEqual(out, [200] * 4)
        self.assertEqual(len(self.calls), 1)

    def test_time_alone_turns_it(self):
        """GUARD on the window: an hour on, a state may have turned with age."""
        import time as _t
        self._get()
        with mock.patch.object(_t, "time", lambda: 10 ** 10):
            self._get()
        self.assertEqual(len(self.calls), 2)


class ThePurge(unittest.TestCase):

    def test_its_backup_is_required(self):
        from tests.test_server import server
        st = mock.MagicMock()
        eng = sys.modules["engine"]
        with mock.patch.object(server, "_stop_running_import", lambda *a, **k: True), \
             mock.patch.object(server, "_safety_backup", side_effect=RuntimeError("no room")), \
             mock.patch.object(server, "_own_store", lambda: st), \
             mock.patch.object(server, "_release_store", lambda s: None), \
             mock.patch.object(eng, "set_delete_active", create=True):
            server._delete_job.clear()
            server._purge_worker()
        self.assertEqual(server._delete_job["status"], "error")
        self.assertIn("nothing was deleted", server._delete_job["error"])
        st.purge_imported_history.assert_not_called()
        server._delete_job.clear(); server._delete_job["status"] = "idle"


class TheModule(unittest.TestCase):
    _SRC = _page("heatmap_module.html")

    def _fn(self, name):
        i = self._SRC.index("function " + name + "(")
        return self._SRC[i:self._SRC.index("\n  }\n", i)]

    def test_issue_count_uses_the_heatmaps_own_rule(self):
        body = self._fn("issueCount")
        self.assertIn("settleValue('bill', {bill: parts[0], flags: parts[1] ? parts[1].split(',') : []})", body)
        self.assertIn("if (isIssue(c, view)) n += counts[key];", body)

    def test_the_flags_the_server_carries_are_settle_values(self):
        srv = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
        sv = self._fn("settleValue")
        for f in ("bill_kwh_differs", "vat_disagrees", "split_missing", "recost_bl82"):
            self.assertIn("'" + f + "'", sv)
            self.assertIn('"' + f + '"', srv[srv.index("_HEALTH_BILL_FLAGS ="):][:200])

    def test_exported(self):
        self.assertIn("setMetric: setMetric, issueCount: issueCount, openEmbedded: openEmbedded,", self._SRC)

    def test_a_tool_writing_recounts(self):
        self.assertIn("changed();", self._fn("reloadRange"))
        self.assertIn("refresh(); changed();", self._fn("openEmbedded"))


class ThePage(unittest.TestCase):
    _DM = _page("data_management.html")

    def test_the_strip(self):
        self.assertIn('id="dm-health-row"', self._DM)
        self.assertIn("EMTHeatmap.issueCount(v[0], d.views[v[0]]) + miss", self._DM)
        self.assertIn("onChange: function () { if (window.loadHealth) loadHealth(); }", self._DM)

    def test_the_whole_history_card(self):
        for t in ("api:whole_history", "csv:range", "csv:bills", "api:gaps"):
            self.assertIn("dmWhole('" + t + "'", self._DM)
        self.assertIn("EMTHeatmap.openEmbedded('/historical-import?embed=1&task=' + encodeURIComponent(task)", self._DM)
        self.assertIn("fetch(apiUrl('/api/historical/purge'), {method: 'POST'", self._DM)

    def test_the_fill_history_link_and_the_nudge_go(self):
        self.assertNotIn("fill_history_page", self._DM)
        self.assertNotIn('id="gap-nudge"', self._DM)
        self.assertNotIn("loadGapNudge", self._DM)

    def test_historical_import_opens_on_a_task(self):
        hi = _page("historical_import.html")
        self.assertIn("document.addEventListener('DOMContentLoaded', bfOpenTaskFromUrl);", hi)
        self.assertIn("!/^(whole_history|range|gaps|bills)$/.test(t[1])", hi)


if __name__ == "__main__":
    unittest.main()
