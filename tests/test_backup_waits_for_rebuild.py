"""
Backup now is refused while a device's history is being rebuilt (interrupted rebuild design §3,
Phase 3; 21 Nov 2026).

A backup taken mid-rebuild holds the device's history half done — the soak Pi's T3 restore took one
(its safety backup, with the rebuild still writing). Phase 2 made a restore stop a rebuild first and
Phase 1 marks a run cut off; this keeps EMT's own manual backups whole: *Backup now* (`/api/backup`)
and the restore page's pre-restore copy (`/api/backup/create`, non-fatal there) answer 409, naming the
device, and the page shows the message. The pre-change backups EMT takes itself are not refused.

DISCRIMINATING: the two refusals fail on the parent; the GUARD (no rebuild, a backup) passes there.
"""
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from tests.test_server import make_client, server    # noqa: E402

PAGE = open(os.path.join(_ROOT, "web", "templates", "data_management.html"), encoding="utf-8").read()
CFG = {"meters": {"electricity_main": {"meta": {}}, "ev_charger": {"meta": {"device": "Charger",
                                                                           "sub_meter": True}}}}


class BackupNow(unittest.TestCase):

    def setUp(self):
        self.eng = mock.MagicMock()
        self.made = []
        self.p = [mock.patch.dict(sys.modules, {"engine": self.eng}),
                  mock.patch.object(server, "load_config", lambda: CFG),
                  mock.patch.object(server, "_create_backup_zip",
                                    lambda label="b": self.made.append(label) or "/x/%s.zip" % label)]
        for x in self.p:
            x.start()
        self.cli = make_client()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def _rebuilding(self):
        self.eng.attribution_running.return_value = True
        self.eng.api_attribution_status.return_value = {"runs": [
            {"device": "ev_charger", "status": "running"}, {"device": "house_battery", "status": "done"}]}

    def test_it_is_refused_naming_the_device(self):
        self._rebuilding()
        r = self.cli.post("/api/backup")
        self.assertEqual(r.status_code, 409)
        d = r.get_json()
        self.assertEqual(d["error"], "rebuild_running")
        self.assertIn("Charger's history is being rebuilt", d["message"])
        self.assertEqual(self.made, [])

    def test_the_restore_page_s_copy_too(self):
        self._rebuilding()
        r = self.cli.post("/api/backup/create", json={"label": "pre_zip_restore"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.made, [])

    def test_GUARD_no_rebuild_a_backup(self):
        self.eng.attribution_running.return_value = False
        with mock.patch("os.path.getsize", lambda p: 10):
            r = self.cli.post("/api/backup")
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.made, ["manual"])

    def test_the_page_shows_the_message(self):
        i = PAGE.index("async function doManualBackup(")
        body = PAGE[i:PAGE.index("\n}\n", i)]
        self.assertIn("showError(data.message || data.error", body)


if __name__ == "__main__":
    unittest.main()
