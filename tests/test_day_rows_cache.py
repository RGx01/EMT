"""
The Spiral / Usage Stats day rows, fetched whole once and then only where they moved
(C, 2 Oct 2026). Server: /api/energy `kind=range` (local days start..end) and
`fields=days&since=` (recent per-day fingerprints + ONE `history` token before `since`,
BlockStore.history_token). Client: charts.html's EMTDayRows.

On a production-like database (138k blocks): the whole fetch 1.7 s / 1.7 MB; the change
check 0.08 s / 5 KB (history_token 0.067 s with NOT INDEXED — 0.28 s walking the index);
a 3-day range 0.03 s. A range's rows equal the same days' rows from all history (five
ranges incl. both clock changes, two databases). In a browser: an unchanged check makes
one request; a recent edit re-fetches a 3-day range and the merged rows equal a fresh whole
fetch; an edit older than `since` re-fetches whole.

New-facet tests: they fail on the parent tree.
"""
import importlib.util
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "web"))

from block_store import BlockStore              # noqa: E402

TZ = "Europe/London"
CFG = {"meters": {"electricity_main": {"meta": {"timezone": TZ, "currency_symbol": "£",
                                                "billing_day": 1, "block_minutes": 30}}}}


def _store():
    st = BlockStore(tempfile.mktemp(suffix=".db"))
    st._conn.execute("PRAGMA foreign_keys=OFF")
    st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, "
                     "timezone) VALUES (1, '2026-01-01T00:00:00', 1, 30, ?)", (TZ,))
    st._conn.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter) VALUES (1, 'electricity_main', 0)")
    t = datetime(2026, 8, 31, 23, 0)
    while t < datetime(2026, 9, 10, 23, 0):
        st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
                         "imp_cost, standing_charge, is_provisional, interpolated) VALUES (?,?,?,1,1.0,0.25,0.5,0,0)",
                         (t.isoformat(), (t + timedelta(minutes=30)).isoformat(), "electricity_main"))
        t += timedelta(minutes=30)
    st._conn.commit()
    return st


class TheServerPieces(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("server_dr", os.path.join(_ROOT, "web", "server.py"))
        cls.srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.srv)
        cls.st = _store()

    def _get(self, q):
        with mock.patch.object(self.srv.store_access, "store", lambda: self.st), \
             mock.patch.object(self.srv, "load_config", lambda: CFG), \
             mock.patch.object(self.srv, "_config_timezone", lambda: TZ):
            r = self.srv.app.test_client().get("/api/energy?" + q)
        return r.status_code, r.get_json()

    def test_a_range_is_those_local_days_and_they_equal_all_historys(self):
        key = lambda r: (r["year"], r["month"], r["day"])
        _, a = self._get("resolution=day&kind=all&include=stats")
        _, r = self._get("resolution=day&kind=range&start=2026-09-03&end=2026-09-05&include=stats")
        self.assertEqual([key(x) for x in r["stats"]["rows"]], [(2026, 9, 3), (2026, 9, 4), (2026, 9, 5)])
        whole = {key(x): x for x in a["stats"]["rows"]}
        for x in r["stats"]["rows"]:
            self.assertEqual(x, whole[key(x)])

    def test_a_bad_range_is_refused(self):
        self.assertEqual(self._get("resolution=day&kind=range&start=2026-09-05&end=2026-09-03")[0], 400)
        self.assertEqual(self._get("resolution=day&kind=range&start=x&end=2026-09-03")[0], 400)

    def test_days_since_is_recent_fingerprints_and_one_history_token(self):
        _, f = self._get("resolution=day&kind=all&fields=days&since=2026-09-08")
        days = sorted(f["fingerprint"]["days"])
        self.assertEqual(days[0], "2026-09-07")          # 00:00 BST 8 Sep is 23:00Z on the 7th
        self.assertIsNotNone(f["fingerprint"]["history"])
        self.assertNotIn("stats", f)

    def test_the_history_token_moves_only_for_old_blocks(self):
        q = "resolution=day&kind=all&fields=days&since=2026-09-08"
        h0 = self._get(q)[1]["fingerprint"]["history"]
        self.st._conn.execute("UPDATE blocks SET imp_cost = 0.3 WHERE block_start = '2026-09-09T12:00:00'")
        h1 = self._get(q)[1]["fingerprint"]["history"]
        self.st._conn.execute("UPDATE blocks SET imp_cost = 0.3 WHERE block_start = '2026-09-02T12:00:00'")
        h2 = self._get(q)[1]["fingerprint"]["history"]
        self.assertEqual(h0, h1, "a change after `since` is the recent days' business")
        self.assertNotEqual(h1, h2, "a change before `since` moves the history token")


_SRC = open(os.path.join(_ROOT, "web", "templates", "charts.html"), encoding="utf-8").read()
_MOD = _SRC[_SRC.index("window.EMTDayRows = (function () {"):]
_MOD = _MOD[:_MOD.index("})();") + 5]


class TheClientCache(unittest.TestCase):

    def test_whole_once_then_the_check(self):
        self.assertIn("'/api/energy?resolution=day&kind=all&fields=stats'", _MOD)
        self.assertIn("'/api/energy?resolution=day&kind=all&fields=days&since=' + since", _MOD)

    def test_anything_older_or_bulk_or_config_is_a_whole_refetch(self):
        self.assertRegex(_MOD, r"f\.history !== c\.history \|\| f\.epoch !== c\.epoch \|\| f\.config !== c\.config")

    def test_a_moved_utc_date_refetches_the_local_days_either_side(self):
        self.assertIn("var lo = addDays(moved[0], -1), hi = addDays(moved[moved.length - 1], 1);", _MOD)
        self.assertIn("kind=range&fields=stats&start=' + lo + '&end=' + hi", _MOD)

    def test_one_request_at_a_time(self):
        self.assertIn("if (!inflight) inflight = update(token)", _MOD)


if __name__ == "__main__":
    unittest.main()
