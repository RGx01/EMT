"""
The settlement pill on the Overview (21 Nov 2026, the user: "add the missing data pill to the overview page").

Charts' pill (half-hours waiting for their kWh, bill or split; a click opens Data Management's issues, a
right-click retries settlement) moved into `_settle_pill.html`, one copy that Charts and the Overview both
include. The Overview redraws it with its cost cards and when its tab comes back, as Charts does with its data.

Discriminating: the Overview tests fail on the parent tree (the include and the Overview's pill are new). The
Charts test is a GUARD: Charts showed the pill before and must still.
"""
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    return open(os.path.join(_ROOT, "web", "templates", name), encoding="utf-8").read()


SPAN = '<span id="dcc-settle-note" class="dcc-settle-note" style="display:none;"></span>'


class TheOverview(unittest.TestCase):
    PAGE = _read("live_power.html")

    def test_it_shows_the_pill_in_its_topbar(self):
        top = self.PAGE[self.PAGE.index("{% block topbar %}"):]
        top = top[:top.index("{% endblock %}")]
        self.assertIn(SPAN, top)
        self.assertIn('{% include "_settle_pill.html" %}', self.PAGE)

    def test_it_redraws_with_the_cards_and_when_the_tab_comes_back(self):
        i = self.PAGE.index("function refreshBilling()")
        self.assertIn("_billRedrawPill();", self.PAGE[i:self.PAGE.index("\n}\n", i)])
        self.assertIn("document.addEventListener('visibilitychange', function() { if (!document.hidden) "
                      "_billRedrawPill(); });", self.PAGE)


class ThePillIsOneCopy(unittest.TestCase):
    PILL = _read("_settle_pill.html")

    def test_charts_includes_it_and_keeps_none_of_its_own(self):      # GUARD for Charts' pill
        charts = _read("charts.html")
        self.assertIn(SPAN, charts)
        self.assertIn('{% include "_settle_pill.html" %}', charts)
        for name in ("function loadSettleNote(", "function settleNoteWire(", "function settleRetry(",
                     ".dcc-settle-note.amber {"):
            self.assertNotIn(name, charts)
            self.assertIn(name, self.PILL)

    def test_it_needs_nothing_from_charts(self):
        # Charts' own refresh is asked for only where it exists; the rest is base.html's (apiUrl, BASE_PATH).
        self.assertIn("if (typeof _refreshIfChanged === 'function') _refreshIfChanged(false);", self.PILL)
        bare = re.findall(r"(?<![.\w])_refreshIfChanged\(", self.PILL)
        self.assertEqual(len(bare), 1)

    def test_it_draws_whether_or_not_the_page_has_loaded(self):
        self.assertIn("if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);",
                      self.PILL)


if __name__ == "__main__":
    unittest.main()
