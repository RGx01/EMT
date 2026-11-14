"""
The second round from staging (21 Oct 2026, the user).

1. Import history from Octopus: the *Reprice from CSV* card goes; Import a CSV fills half-hours.
2. *Preview plan* stayed greyed when the modal opened on a task. Picking the task started a fresh
   flow before the page's first status check, and a fresh flow skipped the verify check the run lock
   waits on, so the lock never lifted. It wasn't the history being complete.
3. Billing: a day's chart fills its row, as tall as the summary beside it and never under 320 px.
4. Billing: the summary's line spacing is 1.3, not 1.6 (a 460 px panel is 367).
5. Data Management: the health counts go a month at a time, each kept until its own data moves
   (since 21 Oct 2026 kept across restarts, test_health_counts_kept.py, and the warm-up after start
   dropped). The heatmap waits for them only 1.5 s.
6. The support file's log was 99 lines. The Supervisor sends its last 100 unless asked for more;
   both reads now pass `lines`.

Discriminating: every test here fails on the parent but the GUARD.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import test_server as TS                       # noqa: E402  (stubs + make_client)
from block_store import BlockStore             # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _t(name):
    with open(os.path.join(ROOT, "web", "templates", name), encoding="utf-8") as f:
        return f.read()


def _st(starts):
    st = BlockStore(":memory:")
    st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, "
                     "timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
    for bs in starts:
        st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                         "imp_kwh) VALUES (?, ?, 'electricity_main', 1, 0.5)", (bs, bs))
    st._conn.commit()
    return st


class TheHealthByMonthCache(unittest.TestCase):
    """A month is counted again only when it needs to be."""

    @staticmethod
    def _fixture_starts():
        """(OLD, NEW) from the clock _health_views reads its age windows by (datetime.now, not the
        time.time the tests patch). Literal dates broke when the calendar moved (14 Nov 2026:
        2026-10 had left the ten-day young window). NEW is yesterday, inside the young window;
        OLD is 19 months before this month — out of it, and short of Octopus's two-year line
        (`_beyond`, 730 days), which counts its month again with age as the young window does.
        OLD to now is then 20 months (21 when now + 30 min is next month): seven passes of three."""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        new = (now - timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
        y, m = divmod(now.year * 12 + now.month - 1 - 19, 12)
        return datetime(y, m + 1, 10, 8).isoformat(), new.isoformat()

    def setUp(self):
        self.OLD, self.NEW = self._fixture_starts()
        server = self.server = TS.server
        self.st = _st([self.OLD, self.NEW])
        server._health_cache.clear()
        server._health_cache.update(key=None, views=None, missing=None, months=None, per={})
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.spans = []

        def states(store, lo, hi):
            self.spans.append((lo, hi))
            rows = store._conn.execute("SELECT block_start FROM blocks WHERE block_start >= ? "
                                       "AND block_start < ?", (lo, hi)).fetchall()
            return {r[0]: {"bill": "applied", "flags": []} for r in rows}
        eng = sys.modules["engine"]
        self.p = [mock.patch.object(server, "_settlement_states", states),
                  mock.patch.object(eng, "bill_split_held", lambda s: (set(), set()), create=True),
                  mock.patch.object(eng, "bill_no_cost", lambda s: set(), create=True),
                  mock.patch.object(eng, "_import_is_smb_capped", lambda: False, create=True),
                  mock.patch.object(server, "_health_cache_path",
                                    lambda: os.path.join(self.tmp, "health_counts.json"))]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def _count(self):
        return self.server._health_views(self.st, ["bill"], self.OLD, "UTC")

    def test_the_first_count_is_one_pass(self):
        """GUARD: the whole history — three months a pass since 22 Oct 2026 (20 or 21 months: 7)."""
        with mock.patch.object(self.server, "_HEALTH_PASS_PAUSE_S", 0):
            totals, months = self._count()
        self.assertEqual(len(self.spans), 7)
        self.assertEqual(totals, {"bill": {"applied": 2}})
        self.assertEqual(sorted(months), [self.OLD[:7], self.NEW[:7]])

    def test_nothing_moved_counts_no_month(self):
        self._count(); self.spans.clear()
        totals, _ = self._count()
        self.assertEqual(self.spans, [])
        self.assertEqual(totals, {"bill": {"applied": 2}})

    def test_a_write_counts_only_its_month(self):
        self._count(); self.spans.clear()
        self.st._conn.execute("UPDATE blocks SET imp_kwh = 0.7 WHERE block_start = ?", (self.OLD,))
        self._count()
        self.assertEqual(self.server._health_cache["counted"], [self.OLD[:7]])

    def test_the_young_month_turns_with_the_window(self):
        """GUARD on the age window: the newest month is counted again when the window moves."""
        import time as _t
        self._count(); self.spans.clear()
        later = _t.time() + self.server._HEALTH_TTL_S
        with mock.patch.object(_t, "time", lambda: later):
            self._count()
        self.assertIn(self.NEW[:7], self.server._health_cache["counted"])
        self.assertNotIn(self.OLD[:7], self.server._health_cache["counted"])


class TheSupervisorLog(unittest.TestCase):

    def _url(self, call):
        seen = []

        class R:
            def read(self): return b"a\nb\n"
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def urlopen(req, timeout=None):
            seen.append(req.full_url); return R()
        with mock.patch.dict(os.environ, {"EMT_MODE": "supervised"}), \
             mock.patch("urllib.request.urlopen", urlopen):
            call()
        return seen[0]

    def test_the_support_file_asks_for_its_lines(self):
        url = self._url(lambda: TS.server._addon_log_text(20000))
        self.assertTrue(url.endswith("/addons/self/logs?lines=20000"), url)

    def test_the_logs_page_asks_for_its_lines(self):
        client = TS.make_client()
        url = self._url(lambda: client.get("/api/logs?lines=500"))
        self.assertTrue(url.endswith("/addons/self/logs?lines=500"), url)


class ThePages(unittest.TestCase):

    def test_reprice_from_csv_is_gone(self):
        hi = _t("historical_import.html")
        self.assertNotIn('id="reprice-csv-file"', hi)
        self.assertNotIn("function repriceFromCsv()", hi)
        # the pricing-health card's slots CSV went with the card (engine step 2d, 23 Oct 2026)
        self.assertNotIn("function applySlotsCsv(", hi)


    def test_the_lock_lifting_restores_preview(self):
        hi = _t("historical_import.html")
        self.assertIn("if(pv){ if(locked) pv.disabled = true; else bfSyncPreviewBtn(); }", hi)

    def test_the_billing_chart_fills_its_row(self):
        b = _t("billing_module.html")
        self.assertIn(".emt-bill .chart-container { flex: 1 1 0; min-width: 0; min-height: 320px; align-self: stretch;", b)
        self.assertIn(".emt-bill .chart-container canvas { position: absolute; inset: 0; }", b)

    def test_the_summary_is_tighter(self):
        b = _t("billing_module.html")
        self.assertIn("';line-height:1.3;white-space:nowrap;'", b)

    def test_the_heatmap_waits_on_the_counts_only_so_long(self):
        hm = _t("heatmap_module.html")
        self.assertIn("Promise.race([loadMonthHealth(), new Promise(function (r) { setTimeout(r, HEALTH_WAIT_MS); })])", hm)


if __name__ == "__main__":
    unittest.main()
