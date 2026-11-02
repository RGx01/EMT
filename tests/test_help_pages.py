"""
BL-102 (help pages design, PR 1): the help is a page per section at /help/<section>, with a shared
side list, and /help a contents page of them. PR 1: Getting started, Overview, Charts, Insights;
PR 2: Data Management, Settings, How EMT counts, Running EMT, and the old page's cards gone.

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
                  "charts": "Usage Stats", "insights": "Peak Demand Window",
                  "data-management": "The tools on a selection", "settings": "Meter Config",
                  "how-emt-counts": "The house and the devices", "running-emt": "Where your data is"}
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

    def test_the_contents_page_lists_the_pages_and_none_of_the_old_cards(self):
        html = self._text("/help")
        body = html.split('class="help-contents"')[1]
        for slug, _ in self.server.HELP_PAGES:
            self.assertIn('href="/help/%s"' % slug, body)
        # PR 2: the v4 page's cards are all rewritten, so none is left on it.
        for old in ("What is Energy Meter Tracker?", "Meter Reconciliation Period", "Gap Filling",
                    "Home Assistant Sensors", "Further Reading"):
            self.assertNotIn(old, html)

    def test_no_page_offers_what_v5_does_not_have(self):
        """The v4 page's claims gone with it: the dispatch overlay option, the billing source toggle,
        and the four HA sensors (removed in v4 3.2.0)."""
        for path in ["/help"] + ["/help/" + s for s, _ in self.server.HELP_PAGES]:
            html = self._text(path)
            for gone in ("dispatch overlay", "billing source", "sensor.energy_meter_import_kwh"):
                self.assertNotIn(gone, html, path)

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


class TheQuestionMark(unittest.TestCase):
    """PR 3: a ? in each page's header opens its help page."""

    PAGES = {"/charts": "charts", "/live-power": "overview", "/insights": "insights",
             "/data-management": "data-management", "/settings": "settings",
             "/settings?tab=carbon": "settings", "/billing-history": "settings",
             "/logs": "running-emt", "/welcome": "getting-started", "/historical-import": "data-management"}

    def setUp(self):
        from tests.test_server import make_client, server
        self.server = server
        self.client = make_client()
        cfg = {"meters": {"electricity_main": {"meta": {}, "channels": {}}}}
        self.p = mock.patch.object(server, "load_config", lambda: cfg)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_each_page_links_its_help(self):
        for path, section in self.PAGES.items():
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            qs = re.findall(r'<a class="help-q" href="([^"]+)"', r.get_data(as_text=True))
            self.assertEqual(qs, ["/help/" + section], path)

    def test_every_template_with_a_title_has_its_question_mark(self):
        """Every page with a title has its ?, and a page added later without one fails here (the help pages
        themselves aside)."""
        tdir = os.path.join(_ROOT, "web", "templates")
        for name in sorted(os.listdir(tdir)):
            if not name.endswith(".html") or name in ("base.html", "help_base.html"):
                continue
            src = open(os.path.join(tdir, name), encoding="utf-8").read()
            if "{% block topbar %}" in src:
                self.assertIn("help_q(", src, name)


if __name__ == "__main__":
    unittest.main()
