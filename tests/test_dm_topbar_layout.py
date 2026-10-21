"""
Data Management's jobs move to the topbar (data management design §1 (8), 21 Oct 2026, the user).

Below the heatmap, the Whole history and Backups & storage cards moved as its months loaded, and the
Whole history buttons waited on two requests before they showed. Now the page body is the health
strip and the heatmap. The topbar has three things:
- a *Whole history* menu, its items decided when the page is rendered;
- *Backup & restore*, a modal holding the Backups & storage card;
- *Backup now*.

A backup dropped anywhere on the page opens the restore, and `?open=backups` / `?open=support` open
their modal from a link.

Discriminating: every test here fails on the parent.
"""
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import test_server as TS                       # noqa: E402  (stubs + make_client)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _template():
    with open(os.path.join(ROOT, "web", "templates", "data_management.html"), encoding="utf-8") as f:
        return f.read()


class TheWholeHistoryMenu(unittest.TestCase):

    def _page(self, uses_api, imported):
        client = TS.make_client()
        eng = sys.modules["engine"]
        st = mock.MagicMock()
        st.count_imported_history.return_value = {"blocks": imported}
        with mock.patch.object(eng, "mode_uses_api", lambda *a: uses_api, create=True), \
             mock.patch.object(TS.server.store_access, "store", lambda: st):
            r = client.get("/data-management")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        return html[html.index('id="dm-whole-menu"'):html.index('id="btn-backups"')]

    def test_is_whole_at_first_paint_with_the_api(self):
        menu = self._page(True, 12)
        for item in ("Import history from Octopus", "Import a CSV", "Import PDF bills",
                     "Recover every gap", "Delete all imported history"):
            self.assertIn(item, menu)
        self.assertNotRegex(menu, r'id="dm-purge-btn"[^>]*style="display:none;"')

    def test_without_the_api_its_jobs_are_not_offered(self):
        menu = self._page(False, 0)
        self.assertNotIn("Import history from Octopus", menu)
        self.assertNotIn("Recover every gap", menu)
        self.assertIn("Import a CSV", menu)
        self.assertRegex(menu, r'id="dm-purge-btn"[^>]*style="display:none;"')


class ThePage(unittest.TestCase):

    def setUp(self):
        self.dm = _template()

    def test_nothing_sits_below_the_heatmap(self):
        body = self.dm[self.dm.index("{% block content %}"):self.dm.index("{% block scripts %}")]
        after = body[body.index('id="dm-heatmap-card"'):]
        self.assertNotIn('<div class="card" id="dm-whole"', self.dm)
        # the backups card is in its overlay, which is fixed, not in the flow
        self.assertLess(after.index('id="backups-overlay"'), after.index('id="dm-backups"'))

    def test_the_topbar_has_the_three(self):
        top = self.dm[self.dm.index("{% block topbar %}"):self.dm.index("{% endblock %}")]
        for x in ('id="btn-whole"', 'onclick="openBackups()"', 'onclick="doManualBackup()"'):
            self.assertIn(x, top)

    def test_the_menu_is_not_hidden_after_a_fetch(self):
        self.assertNotIn(".dm-api-only", self.dm)
        fn = self.dm[self.dm.index("function dmWholeButtons() {"):]
        fn = fn[:fn.index("\n}\n")]
        self.assertNotIn("/api/billing-source", fn)

    def test_a_drop_anywhere_opens_the_restore(self):
        self.assertIn("window.addEventListener('drop'", self.dm)
        self.assertIn("if (!files(e) || e.defaultPrevented) return;", self.dm)
        self.assertIn("onFileSelect(e.dataTransfer.files);", self.dm)

    def test_a_link_can_open_the_backups_or_the_support_file(self):
        self.assertIn("if (o === 'backups') openBackups();", self.dm)
        self.assertIn("else if (o === 'support') openSupport();", self.dm)

    def test_the_health_strip_opens_the_backups(self):
        self.assertTrue(re.search(r"<b>Last backup</b>.*\n.*\n.*\"openBackups\(\)\"", self.dm))


if __name__ == "__main__":
    unittest.main()
