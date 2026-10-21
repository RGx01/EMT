"""
The support file (data management design §1 (5), PR 5; 21 Oct 2026).

One zip to send to whoever is helping: the log, `support.json`, and the database cut to a span
(default the last 30 days; a month, everything, or a heatmap selection), with the MPANs, meter
serials, the account number and the postcode redacted by default to stable placeholders. Built on
a `VACUUM INTO` copy; the live database is not touched. The size is estimated first — weighted by
every time-indexed table, not the blocks alone (on a staging copy the rolling power and SoC
histories were most of a 30-day file; estimated 2.8, 0.60, 13.1 MB for 30 days, July, everything
against 2.0, 0.57, 11.8 built). On that copy no identifier was left in the database, log or
summary.

Discriminating (fail on the parent: none of it exists), except the GUARD marked.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
import zipfile
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import support_file as sf                 # noqa: E402
from block_store import BlockStore        # noqa: E402

MPAN, MPAN_X, SERIAL, ACCT, PC = "2000099999901", "2000099999902", "22L9999999", "A-1234ABCD", "XY65"


def _db(path):
    st = BlockStore(path)
    c = st._conn
    c.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, timezone) "
              "VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
    c.execute("INSERT INTO meters (id, config_period_id, meter_id, is_sub_meter, postcode_prefix) "
              "VALUES (1, 1, 'electricity_main', 0, ?)", (PC,))
    c.execute("INSERT INTO meter_channels (meter_id, channel, read_sensor, mpan) VALUES "
              "(1, 'import', 'sensor.x', ?), (1, 'export', 'sensor.y', ?)",
              (SERIAL + "-" + MPAN, SERIAL + "-" + MPAN_X))
    for bs in ("2026-08-01T10:00:00", "2026-10-10T10:00:00", "2026-10-10T10:30:00"):
        c.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh) "
                  "VALUES (?, ?, 'electricity_main', 1, 0.5)", (bs, bs))
        c.execute("INSERT INTO measured_cost (slot_start, mpan, direction, cost_incl, cost_excl, label, kwh) "
                  "VALUES (?, ?, 'CONSUMPTION', 0.1, 0.1, 'STANDARD', 0.5)", (bs, MPAN))
    bid = c.execute("SELECT id FROM blocks WHERE block_start = '2026-08-01T10:00:00'").fetchone()[0]
    c.execute("INSERT INTO generation_mix (block_id, fuel, perc) VALUES (?, 'wind', 40)", (bid,))
    c.execute("INSERT INTO carbon_intensity (captured_at, postcode, intensity_actual) VALUES "
              "('2026-10-10T10:00', ?, 100)", (PC,))
    st.set_kraken_state("kraken_account_number", ACCT)
    st.set_meta("note", "account %s on %s" % (ACCT, MPAN))       # a JSON-ish value elsewhere
    c.commit()
    st.close()


class _Dir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.src = os.path.join(self.dir, "blocks.db")
        _db(self.src)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def conn(self, p=None):
        c = sqlite3.connect(p or self.src)
        self.addCleanup(c.close)
        return c


class TheIdentifiers(_Dir):

    def test_found_where_the_database_keeps_them(self):
        ids = sf.identifiers(self.conn())
        self.assertEqual(sorted(ids), sorted([MPAN, MPAN_X, SERIAL, ACCT, PC]))
        self.assertEqual(sorted(set(v.split("-")[0] for v in ids.values())),
                         ["ACCOUNT", "MPAN", "POSTCODE", "SERIAL"])

    def test_stable_placeholders(self):
        ids = sf.identifiers(self.conn())
        t = sf.redact_text("%s and %s-%s" % (MPAN, SERIAL, MPAN), ids)
        self.assertEqual(t, "%s and %s-%s" % (ids[MPAN], ids[SERIAL], ids[MPAN]))


class TheCut(_Dir):

    def test_the_span_and_every_config_table(self):
        out = os.path.join(self.dir, "cut.db")
        kept = sf.cut(self.src, out, lo="2026-10-01T00:00:00")
        c = self.conn(out)
        self.assertEqual(kept["blocks"], 2)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM measured_cost").fetchone()[0], 2)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM meter_channels").fetchone()[0], 2)  # whole
        self.assertEqual(c.execute("SELECT COUNT(*) FROM generation_mix").fetchone()[0], 0)  # its block went

    def test_redacted_in_every_text_column(self):
        out = os.path.join(self.dir, "cut.db")
        ids = sf.identifiers(self.conn())
        sf.cut(self.src, out, mapping=ids)
        c = self.conn(out)
        for v in ids:
            for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                for col in [r[1] for r in c.execute('PRAGMA table_info("%s")' % t)]:
                    n = c.execute('SELECT COUNT(*) FROM "%s" WHERE CAST("%s" AS TEXT) LIKE ?' % (t, col),
                                  ("%" + v + "%",)).fetchone()[0]
                    self.assertEqual(n, 0, (t, col))
        self.assertEqual(c.execute("SELECT DISTINCT mpan FROM measured_cost").fetchall(), [(ids[MPAN],)])

    def test_the_live_database_is_untouched(self):
        """GUARD (fails on the parent only by the missing module)."""
        before = self.conn().execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
        sf.cut(self.src, os.path.join(self.dir, "cut.db"), lo="2026-10-01T00:00:00",
               mapping=sf.identifiers(self.conn()))
        self.assertEqual(self.conn().execute("SELECT COUNT(*) FROM blocks").fetchone()[0], before)
        self.assertEqual(self.conn().execute("SELECT DISTINCT mpan FROM measured_cost").fetchall(), [(MPAN,)])


class TheZip(_Dir):

    def test_its_three_files_redacted(self):
        out = os.path.join(self.dir, "s.zip")
        r = sf.build(self.src, out, log_text="poll mpan=%s account %s\nERROR boom" % (MPAN, ACCT),
                     summary={"config": {"mpan": MPAN}})
        self.assertEqual(r["redacted"], 5)
        z = zipfile.ZipFile(out)
        self.assertEqual(sorted(z.namelist()), ["blocks.db", "emt.log", "support.json"])
        text = z.read("emt.log").decode() + z.read("support.json").decode()
        for v in (MPAN, ACCT):
            self.assertNotIn(v, text)
        self.assertIn("MPAN-", text)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "support_blocks.db")))   # cleaned up

    def test_unredacted_when_asked(self):
        out = os.path.join(self.dir, "s.zip")
        sf.build(self.src, out, redact=False, log_text="mpan=%s" % MPAN)
        self.assertIn(MPAN, zipfile.ZipFile(out).read("emt.log").decode())


class TheEstimate(_Dir):

    def test_a_rolling_table_counts(self):
        """The blocks alone misled: a dense rolling history inside the span is most of a file."""
        c = self.conn()
        for i in range(400):
            c.execute("INSERT INTO power_history (captured_at, net_kw) VALUES (?, 1.0)",
                      ("2026-10-10T10:%02d:%02d" % (i // 60, i % 60),))
        c.commit()
        a = sf.estimate(c, "2026-10-01T00:00:00", None, 10_000_000)
        b = sf.estimate(c, "2026-07-01T00:00:00", "2026-09-01T00:00:00", 10_000_000)
        self.assertGreater(a["bytes"], 10 * b["bytes"])
        self.assertEqual((a["half_hours"], b["half_hours"]), (2, 1))

    def test_last_errors(self):
        self.assertEqual(sf.last_errors("a INFO x\nb WARNING y\nc ERROR z\n"), ["b WARNING y", "c ERROR z"])


class TheRoutes(unittest.TestCase):

    def setUp(self):
        from tests.test_server import make_client, server
        self.server = server
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "blocks.db")
        _db(self.db)
        self.st = BlockStore(self.db)
        self.client = make_client(store=self.st)

        class Inline:
            def __init__(self, target, args=(), kwargs=None, **k):
                self.f, self.a, self.k = target, args, kwargs or {}

            def start(self):
                self.f(*self.a, **self.k)
        self.p = [mock.patch.object(server.store_access, "store", lambda: self.st),
                  mock.patch.object(server, "DATA_DIR", self.dir),
                  mock.patch.object(server, "_addon_log_text", lambda *a, **k: "x ERROR y mpan=" + MPAN),
                  mock.patch.object(server.threading, "Thread", Inline)]
        for x in self.p:
            x.start()
        server._support_job.clear(); server._support_job["status"] = "idle"

    def tearDown(self):
        for x in self.p:
            x.stop()
        self.st.close()
        shutil.rmtree(self.dir, ignore_errors=True)
        self.server._support_job.clear(); self.server._support_job["status"] = "idle"

    def _post(self, url, body):
        r = self.client.post(url, data=json.dumps(body), content_type="application/json")
        return r.status_code, json.loads(r.data)

    def test_the_spans(self):
        lo, hi = self.server._support_span({})
        self.assertIsNone(hi)
        with mock.patch.object(self.server, "_site_tz", lambda: "Europe/London"):
            self.assertEqual(self.server._support_span({"span": "month", "month": "2026-07"}),
                             ("2026-06-30T23:00:00", "2026-07-31T23:00:00"))
        self.assertEqual(self.server._support_span({"span": "all"}), (None, None))
        self.assertEqual(self.server._support_span({"span": "range", "from_utc": "2026-10-10T10:00:00",
                                                    "to_utc": "2026-10-10T11:00:00"}),
                         ("2026-10-10T10:00:00", "2026-10-10T11:00:00"))
        with self.assertRaises(ValueError):
            self.server._support_span({"span": "week"})

    def test_estimate_build_download(self):
        code, e = self._post("/api/support/estimate", {"span": "all"})
        self.assertEqual((code, e["half_hours"]), (200, 3))
        code, b = self._post("/api/support/build", {"span": "all"})
        self.assertEqual(code, 200)
        s = json.loads(self.client.get("/api/support/status").data)
        self.assertEqual((s["status"], s["redacted"]), ("done", 5))
        r = self.client.get("/api/support/download")
        self.assertEqual((r.status_code, r.mimetype), (200, "application/zip"))
        z = zipfile.ZipFile(__import__("io").BytesIO(r.data))
        summary = json.loads(z.read("support.json"))
        for k in ("version", "data_source_mode", "config", "import_audit", "coordinator", "bill_ask",
                  "missing", "blocks_by_meter_source_month", "last_errors", "support_file"):
            self.assertIn(k, summary)
        self.assertNotIn(MPAN, z.read("emt.log").decode())

    def test_one_at_a_time(self):
        self.server._support_job["status"] = "running"
        self.assertEqual(self._post("/api/support/build", {})[0], 409)


class TheDialog(unittest.TestCase):
    _DM = open(os.path.join(_ROOT, "web", "templates", "data_management.html"), encoding="utf-8").read()
    _HM = open(os.path.join(_ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8").read()

    def test_from_the_card_and_from_a_selection(self):
        self.assertIn('onclick="openSupport()"', self._DM)
        self.assertIn("onSupportFile: function (from, to, label) { openSupport({from: from, to: to, label: label}); }", self._DM)
        self.assertEqual(self._HM.count("supportItem("), 4)          # the item + 3 menus

    def test_redacted_and_30_days_by_default(self):
        self.assertIn('id="ss-30d" checked', self._DM)
        self.assertIn('id="ss-redact" checked', self._DM)

    def test_the_size_first_and_only_the_latest(self):
        self.assertIn("over 10 MB, more than many email services take", self._DM)
        self.assertIn("if (seq !== _ssSeq) return;", self._DM)


if __name__ == "__main__":
    unittest.main()
