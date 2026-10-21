"""
The sub-pages retired (data management design §1 (7), PR 7; 21 Oct 2026).

Fill History & Gaps, Delete Blocks and Cost Corrections go, their old addresses redirected to where
their jobs went: Data Management (the heatmap's *Delete…* and *Fill this gap…*, the Whole history
card's purge and imports, the device cards) and Charts' Billing tab (the correction modal, CAD-only).
The routes only they called go with them — the deleted-ranges list and its *Re-import* (a deleted
half-hour is a missing one on the heatmap, and filling it lifts the deletion), *Review unsettled
blocks* and *Retry settlement* (the Import bill's amber and red, and *Re-fetch*), and the zip
extracts no page calls since the Backups & storage card. The billing settlement source is retired,
not moved: its card, the wizard's step and its POST go; the importer no longer carries it.

Discriminating: every test here fails on the parent.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import test_server as TS                       # noqa: E402  (stubs + make_client)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


class TheOldAddresses(unittest.TestCase):

    def setUp(self):
        self.client = TS.make_client()

    def _to(self, path):
        r = self.client.get(path)
        self.assertEqual(r.status_code, 302, path)
        return r.headers["Location"]

    def test_delete_blocks_and_fill_history_go_to_data_management(self):
        for path in ("/delete-blocks", "/fill-history"):
            with self.subTest(path=path):
                self.assertTrue(self._to(path).endswith("/data-management"))

    def test_corrections_go_to_the_billing_tab(self):
        self.assertTrue(self._to("/corrections").endswith("/charts?tab=daily"))

    def test_the_templates_are_gone(self):
        for t in ("corrections.html", "delete_blocks.html", "fill_history.html"):
            with self.subTest(t=t):
                self.assertFalse(os.path.exists(os.path.join(ROOT, "web", "templates", t)))


class TheRoutesOnlyTheyCalled(unittest.TestCase):

    def test_are_gone(self):
        rules = {(r.rule, m) for r in TS.server.app.url_map.iter_rules() for m in r.methods}
        for rule, method in (("/api/blocks/deleted-ranges", "GET"), ("/api/blocks/reimport", "POST"),
                             ("/api/unsettled-blocks", "GET"), ("/api/retry-settlement", "POST"),
                             ("/api/import/extract-zip", "POST"),
                             ("/api/import/extract-zip-by-name", "POST"),
                             ("/api/billing-source", "POST")):
            with self.subTest(rule=rule):
                self.assertNotIn((rule, method), rules)

    def test_the_billing_source_is_still_read(self):
        """GUARD: the GET stays — the pages read `uses_api`, the stages and the views from it."""
        rules = {(r.rule, m) for r in TS.server.app.url_map.iter_rules() for m in r.methods}
        self.assertIn(("/api/billing-source", "GET"), rules)


class ThePages(unittest.TestCase):

    def test_data_management_keeps_only_backup_now_in_its_topbar(self):
        dm = _read("web", "templates", "data_management.html")
        top = dm[dm.index("{% block topbar %}"):dm.index("{% endblock %}")]
        self.assertIn("doManualBackup()", top)
        self.assertNotIn("corrections_page", top)
        self.assertNotIn("delete_blocks_page", top)

    def test_charts_opens_the_tab_it_is_asked_for(self):
        self.assertIn("new URLSearchParams(location.search).get('tab')", _read("web", "templates", "charts.html"))

    def test_the_wizard_has_no_settlement_step(self):
        mc = _read("web", "templates", "meter_config.html")
        self.assertNotIn("steps.push('settlement')", mc)
        self.assertNotIn("wizStepSettlement", mc)
        self.assertNotIn("billing_source", mc)


if __name__ == "__main__":
    unittest.main()
