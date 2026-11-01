"""
BL-102 (help pages design, PR 1): the help is a page per section at /help/<section>, with a shared
side list, and /help a contents page of them. Getting started, Overview, Charts and Insights are
rewritten for v5; the old page's other cards stay on the contents page until PR 2.

Discriminating (fail on the parent: one /help page, no /help/<section>), except the GUARDs marked.
"""
import os
import re
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_HELP_DIR = os.path.join(_ROOT, "web", "templates", "help")


class HelpPages(unittest.TestCase):

    def setUp(self):
        from tests.test_server import make_client, server
        self.server = server
        self.client = make_client()

    def _text(self, path):
        r = self.client.get(path)
        self.assertEqual(r.status_code, 200, path)
        return r.get_data(as_text=True)

    def test_each_section_has_its_page(self):
        titles = {"getting-started": "What Energy Meter Tracker does", "overview": "Smart Charging",
                  "charts": "Usage Stats", "insights": "Peak Demand Window"}
        self.assertEqual([s for s, _ in self.server.HELP_PAGES], list(titles))
        for slug, marker in titles.items():
            html = self._text("/help/" + slug)
            self.assertIn(marker, html, slug)

    def test_a_section_with_no_page_is_a_404(self):
        """GUARD (the parent has no /help/<section> at all)."""
        self.assertEqual(self.client.get("/help/no-such-page").status_code, 404)

    def test_the_side_list_names_every_page_and_marks_this_one(self):
        html = self._text("/help/charts")
        for slug, label in self.server.HELP_PAGES:
            self.assertIn('href="/help/%s"' % slug, html)
        self.assertRegex(html, r'href="/help/charts" class="on"')
        self.assertNotRegex(html, r'href="/help/overview" class="on"')

    def test_prev_and_next_follow_the_order(self):
        html = self._text("/help/overview")
        self.assertIn("← Getting started", html)
        self.assertIn("Charts →", html)
        first = self._text("/help/getting-started")
        nxt = re.search(r'<div class="help-next">(.*?)</div>', first, re.S).group(1)
        self.assertNotIn("←", nxt)
        self.assertIn("Overview →", nxt)

    def test_the_contents_page_lists_the_pages_and_drops_the_rewritten_cards(self):
        html = self._text("/help")
        for slug, _ in self.server.HELP_PAGES:
            self.assertIn('href="/help/%s"' % slug, html)
        # The cards now pages of their own are gone from it; the ones still to be rewritten stay.
        self.assertNotIn("What is Energy Meter Tracker?", html)
        self.assertNotIn("⚡ Overview Page", html)
        self.assertIn("Gap Filling", html)

    def test_no_link_points_at_v4_s_repository(self):
        """v4's repository goes private when v5 launches (design §2)."""
        for path in ["/help"] + ["/help/" + s for s, _ in self.server.HELP_PAGES]:
            self.assertNotIn("energy-meter-tracker-addon", self._text(path), path)

    def test_every_page_has_its_template(self):
        """GUARD: a section in HELP_PAGES with no template would 500."""
        for slug, _ in self.server.HELP_PAGES:
            self.assertTrue(os.path.exists(os.path.join(_HELP_DIR, slug.replace("-", "_") + ".html")), slug)

    def test_the_help_pages_stay_open_before_setup(self):
        with mock.patch.object(self.server, "load_config", lambda: {"meters": {}}):
            self.assertEqual(self.client.get("/help/getting-started").status_code, 200)


if __name__ == "__main__":
    unittest.main()
