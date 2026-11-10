"""
The support file hides the site name too (10 Nov 2026, the user: people often name the site after
the house, and a file attached to a public issue can be downloaded by anyone).

Every billing period's site name, each home era's site and the names the caller adds (the live
config's site, the Instance name) become `SITE-1`, … — but only where the name stands whole: a text
column holding exactly it, the name quoted (JSON, the log's `site=%r`), and the backup folder's slug.
Plain replacement would let a site called "Home" rewrite "Home Assistant" and the `home_eras` key.

Discriminating: every test not marked GUARD fails on the parent, which hid no site name.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import support_file as sf                 # noqa: E402
from block_store import BlockStore        # noqa: E402


def _db(path, site, old_site="Old Mill", era_site="Flat 2b"):
    st = BlockStore(path)
    c = st._conn
    c.execute("INSERT INTO config_periods (id, effective_from, effective_to, billing_day, block_minutes, "
              "timezone, site_name) VALUES (1, '2020-01-01T00:00:00', '2024-01-01T00:00:00', 1, 30, 'UTC', ?), "
              "(2, '2024-01-01T00:00:00', NULL, 1, 30, 'UTC', ?)", (old_site, site))
    st.set_meta("home_eras", [{"id": "e1", "kind": "move", "label": "Moved in", "start": "2024-01-01",
                               "site": era_site},
                              {"id": "e2", "kind": "battery", "label": "Home battery", "start": "2025-03-08"}])
    st.set_meta("note", "Home Assistant reached")
    c.commit()
    st.close()


class _Dir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.src = os.path.join(self.dir, "blocks.db")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def build(self, site, log, redact=True, extra=("Barn End",)):
        _db(self.src, site)
        out = os.path.join(self.dir, "s.zip")
        summary = {"config": {"meters": {"electricity_main": {"meta": {"site": site}}}}}
        kw = {"site_extra": extra} if extra is not None else {}
        r = sf.build(self.src, out, redact=redact, log_text=log, summary=summary, work_dir=self.dir, **kw)
        with zipfile.ZipFile(out) as z:
            text = {n: z.read(n) for n in z.namelist()}
        db = os.path.join(self.dir, "out.db")
        with open(db, "wb") as f:
            f.write(text.pop("blocks.db"))
        c = sqlite3.connect(db)
        self.addCleanup(c.close)
        dump = "\n".join(c.iterdump())
        return r, text["support.json"].decode(), text["emt.log"].decode(), dump, c


class TheNames(_Dir):

    def test_found_in_the_periods_the_eras_and_the_extra(self):
        _db(self.src, "Rose Cottage")
        c = sqlite3.connect(self.src)
        self.addCleanup(c.close)
        self.assertEqual(sf.site_names(c, ["Barn End", "", "Rose Cottage"]),
                         {"Old Mill": "SITE-1", "Rose Cottage": "SITE-2", "Flat 2b": "SITE-3",
                          "Barn End": "SITE-4"})


class TheZip(_Dir):
    LOG = ("engine_startup: backup dir = /share/energy_meter_tracker_backup_rose_cottage (site='Rose Cottage')\n"
           "connected to Home Assistant\n")

    def test_no_site_name_is_left_anywhere(self):
        r, summary, log, dump, _ = self.build("Rose Cottage", self.LOG)
        for name in ("Rose Cottage", "rose_cottage", "Old Mill", "Flat 2b"):
            for where, text in (("support.json", summary), ("emt.log", log), ("blocks.db", dump)):
                self.assertNotIn(name, text, "%s left in %s" % (name, where))
        self.assertIn("energy_meter_tracker_backup_site_2 (site='SITE-2')", log)
        self.assertEqual(json.loads(summary)["config"]["meters"]["electricity_main"]["meta"]["site"], "SITE-2")
        self.assertGreaterEqual(r["redacted"], 4)

    def test_the_periods_and_eras_hold_placeholders(self):
        _, _, _, _, c = self.build("Rose Cottage", "")
        self.assertEqual([r[0] for r in c.execute("SELECT site_name FROM config_periods ORDER BY id")],
                         ["SITE-1", "SITE-2"])
        eras = json.loads(c.execute("SELECT value FROM store_meta WHERE key = 'home_eras'").fetchone()[0])
        self.assertEqual(eras[0]["site"], "SITE-3")

    def test_a_site_called_home_leaves_other_words_alone(self):
        _, summary, log, dump, c = self.build("Home", "site='Home'\nconnected to Home Assistant\n")
        self.assertIn("site='SITE-2'", log)
        self.assertIn("connected to Home Assistant", log)
        self.assertIn("Home Assistant reached", dump)
        self.assertIn("Home battery", dump)
        self.assertIsNotNone(c.execute("SELECT 1 FROM store_meta WHERE key = 'home_eras'").fetchone())

    def test_GUARD_unredacted_when_asked(self):
        _, summary, log, dump, _ = self.build("Rose Cottage", self.LOG, redact=False, extra=None)
        self.assertIn("Rose Cottage", log)
        self.assertIn("Rose Cottage", dump)


if __name__ == "__main__":
    unittest.main()
