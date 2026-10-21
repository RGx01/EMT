"""
Backups & storage — one card (data management design §1 (5), PR 3; 21 Oct 2026).

- `/api/backup/list` gives each zip's size and time (`zip_info`) and the daily copy's size; the
  page shows a What · When · Size table — what made each read from the label in its name, when from
  the time in its name (a copy keeps it; the file's modified time moves), the daily copy a row of
  its own, restorable; the legacy v1–v2.0 row gone.
- `/api/storage-info` gives what a compact reclaims at least (SQLite's free pages); storage is one
  line; Create backup now and Compact now in the card's head.
- A dropped or chosen blocks.db opens the restore at once, as a zip did — the file list and its
  Import Selected Files button, below the fold, go (the user, 21 Oct 2026).
- The Import DB, Storage and Compact cards are folded in.

Discriminating (fail on the parent), except the GUARD marked.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
_DM = open(os.path.join(_ROOT, "web", "templates", "data_management.html"), encoding="utf-8").read()


def _fn(name):
    i = _DM.index("function " + name + "(")
    return _DM[i:_DM.index("\n}\n", i)]


class TheList(unittest.TestCase):

    def setUp(self):
        from tests.test_server import make_client, server
        self.server = server
        self.client = make_client()
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "backups"))
        for n, size in (("20261020T105607_manual.zip", 1234), ("20261021T091500_pre-gap-fill.zip", 99)):
            with open(os.path.join(self.dir, "backups", n), "wb") as f:
                f.write(b"x" * size)
        with open(os.path.join(self.dir, "blocks.db"), "wb") as f:
            f.write(b"y" * 4321)
        self.p = mock.patch.object(server, "_share_backup_dir", lambda: self.dir)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_each_zip_has_its_size_and_time(self):
        d = json.loads(self.client.get("/api/backup/list").data)
        self.assertEqual([(z["name"], z["size"]) for z in d["zip_info"]],
                         [("20261021T091500_pre-gap-fill.zip", 99), ("20261020T105607_manual.zip", 1234)])
        self.assertTrue(all(isinstance(z["mtime"], int) for z in d["zip_info"]))

    def test_the_daily_copy_has_its_size(self):
        d = json.loads(self.client.get("/api/backup/list").data)
        self.assertEqual([(f["name"], f["size"]) for f in d["flat"]], [("blocks.db", 4321)])

    def test_the_names_are_still_listed(self):
        """GUARD: `zips` stays a list of names for what reads it."""
        d = json.loads(self.client.get("/api/backup/list").data)
        self.assertEqual(d["zips"], ["20261021T091500_pre-gap-fill.zip", "20261020T105607_manual.zip"])


class TheStorage(unittest.TestCase):

    def test_what_a_compact_reclaims(self):
        from tests.test_server import make_client
        d = json.loads(make_client().get("/api/storage-info").data)
        self.assertIn("reclaimable_bytes", d)
        self.assertIsInstance(d["reclaimable_bytes"], int)


class TheCard(unittest.TestCase):

    def test_one_card_its_actions_in_its_head(self):
        i = _DM.index('<div class="card" id="dm-backups">')
        head = _DM[i:_DM.index('id="dm-storage-line"', i)]
        self.assertIn('onclick="doManualBackup()"', head)
        self.assertIn('id="btn-vacuum" onclick="doVacuum()"', head)

    def test_the_cards_folded_in_are_gone(self):
        for gone in ("Compact Database", 'id="storage-card"', "📥 Import DB", 'id="import-btn"',
                     "Import Selected Files", "Restore legacy files", "extractZipToSelected"):
            self.assertNotIn(gone, _DM)

    def test_storage_is_one_line(self):
        self.assertIn("document.getElementById('dm-storage-line')", _fn("loadStorageInfo"))
        self.assertIn("free inside the file", _fn("loadStorageInfo"))

    def test_a_compact_recounts_the_storage(self):
        self.assertIn("loadStorageInfo();                     // what is left to reclaim", _DM)


class TheTable(unittest.TestCase):

    def test_what_when_size(self):
        body = _fn("loadBackupList")
        self.assertIn("<th style=\"' + th + '\">What</th><th style=\"' + th + '\">When</th>", body)
        self.assertIn("text-align:right;\">Size</th>", body)
        self.assertIn("_backupWhat(z.name)", body)
        self.assertIn("_fmtBytes(z.size)", body)

    def test_when_is_the_time_in_the_name(self):
        self.assertIn("/^(\\d{4})(\\d{2})(\\d{2})T(\\d{2})(\\d{2})(\\d{2})_/.exec(z.name || '')", _fn("_madeAt"))
        self.assertIn("_localWhen(_madeAt(z))", _fn("loadBackupList"))
        self.assertIn("_ago(_madeAt(b))", _fn("loadHealth"))

    def test_the_daily_copy_is_a_restorable_row(self):
        body = _fn("loadBackupList")
        self.assertIn("Daily copy of the database", body)
        self.assertIn("onclick=\"restoreFromFlat()\"", body)


class TheDrop(unittest.TestCase):

    def test_a_blocks_db_opens_the_restore_at_once(self):
        body = _fn("onFileSelect")
        self.assertIn("/\\.(zip|db)$/i.test(x.name)", body)
        self.assertIn("showZipConfirm(f);", body)
        self.assertNotIn("selectedFiles", _DM)

    def test_one_restore_for_either(self):
        self.assertIn("Restore from a file", _DM)
        self.assertIn("await restoreUpload(zipFile,", _fn("doZipRestore"))


if __name__ == "__main__":
    unittest.main()
