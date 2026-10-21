"""
Data Management after the staging rebuild (21 Oct 2026, the user's four).

1. A drag across rows selects whole days, its first row included. Rows run newest first, so a span
   of time took the origin row from midnight up to the cell, the wrong way from where the drag began.
   Within one row it stays cell to cell.
2. *Issues only* folds a month with nothing to look at, a run of them into one line. The counts come
   by local month from the health strip's cached pass, so a clean month is never fetched to find out.
3. Opened on a task from the topbar's *Whole history*, Historical Import hides its source and task
   choosers. Picking API again had reset the task and left the modal half on one flow. Each task says
   what it does.
4. *Missing* is offered only where there is no bill view (CAD-only). With the API, a missing half-hour
   is an issue in the Import and Export bill views already.

Discriminating: every test here fails on the parent.
"""
import json
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import test_server as TS                       # noqa: E402  (stubs + make_client)
from block_store import BlockStore             # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _t(name):
    with open(os.path.join(ROOT, "web", "templates", name), encoding="utf-8") as f:
        return f.read()


def _st(starts, tz="Europe/London"):
    st = BlockStore(":memory:")
    st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, "
                     "timezone) VALUES (1,'2020-01-01T00:00:00',1,30,?)", (tz,))
    for bs in starts:
        st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                         "imp_kwh) VALUES (?, ?, 'electricity_main', 1, 0.5)", (bs, bs))
    st._conn.commit()
    return st


class MissingByMonth(unittest.TestCase):

    def test_a_gap_across_local_midnight_counts_in_each_month(self):
        """30 Sep 2026 23:00 UTC is 1 Oct 00:00 BST: the gap's 22:30 and 23:00 UTC half-hours fall in
        September and October locally."""
        st = _st(["2026-09-30T22:00:00", "2026-09-30T23:30:00"])
        got = TS.server._missing_half_hours(st, "Europe/London", now=datetime(2026, 9, 30, 23, 45))
        self.assertEqual(got["by_month"], {"2026-09": 1, "2026-10": 1})
        self.assertEqual(got["half_hours"], 2)


class TheHealthByMonth(unittest.TestCase):

    def test_each_month_carries_its_states_and_missing(self):
        server = TS.server
        st = _st(["2026-09-10T08:00:00", "2026-10-02T08:00:00"], tz="UTC")
        client = TS.make_client(store=st)
        server._health_cache["key"] = None
        states = {"2026-09-10T08:00:00": {"bill": "applied", "flags": []},
                  "2026-10-02T08:00:00": {"bill": "overdue", "flags": []}}
        with mock.patch.object(server.store_access, "store", lambda: st), \
             mock.patch.object(server, "_settle_views", lambda e: ["bill"]), \
             mock.patch.object(server, "_settlement_states", lambda s, a, b: states), \
             mock.patch.object(server, "_config_timezone", lambda: "UTC"), \
             mock.patch.object(server, "_missing_half_hours",
                               lambda s, tz: {"half_hours": 3, "gaps": 1, "newest": None,
                                          "by_month": {"2026-10": 3}}):
            d = json.loads(client.get("/api/data-health").data)
        self.assertEqual(d["months"]["2026-09"], {"bill": {"applied": 1}})
        self.assertEqual(d["months"]["2026-10"], {"bill": {"overdue": 1}, "missing": 3})
        self.assertNotIn("by_month", d["missing"])
        self.assertEqual(d["views"]["bill"], {"applied": 1, "overdue": 1})


class TheHeatmap(unittest.TestCase):

    def setUp(self):
        self.hm = _t("heatmap_module.html")

    def test_a_drag_off_its_row_takes_whole_days(self):
        fn = self.hm[self.hm.index("function dragSpan(d, at) {"):]
        fn = fn[:fn.index("\n  }\n")]
        self.assertIn("d.h.day !== at.h.day", fn)
        self.assertIn("dragEnd(d, whole), b = dragEnd(at, whole)", fn)

    def test_issues_only_folds_clean_months(self):
        self.assertIn("function foldMonths()", self.hm)
        self.assertIn("p.el.style.display = clean ? 'none' : '';", self.hm)
        self.assertIn("issueCount(state.metric, h[state.metric] || {}) === 0", self.hm)
        self.assertIn("if (k === state.keys[0] || (state.dayWanted", self.hm)

    def test_missing_only_without_a_bill_view(self):
        self.assertIn("if (!METRICS.bill && !METRICS.export) METRICS.presence = PRESENCE_METRIC;", self.hm)


class ThePages(unittest.TestCase):

    def test_a_task_hides_the_choosers(self):
        hi = _t("historical_import.html")
        fn = hi[hi.index("function bfOpenTaskFromUrl() {"):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn("['bf-source-label', 'bf-source', 'bf-task-label', 'bf-task', 'bf-head']", fn)
        self.assertIn("_BF_TASK_INTRO[t[0] + ':' + t[1]]", fn)

    def test_the_health_strips_missing_opens_a_bill_view_where_there_is_one(self):
        dm = _t("data_management.html")
        self.assertIn("(d.views && d.views.bill ? 'bill' : d.views && d.views.export ? 'export' : 'presence')", dm)


if __name__ == "__main__":
    unittest.main()
