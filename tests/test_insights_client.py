"""
Insights (insights.html) reads /api/periods and /api/energy — C step 2, part 2, 1 Oct 2026.

  * Bounds and the trend strip from ONE /api/periods?kind=billing&include=carbon (were
    /api/insights/data-bounds and /api/insights/periods). /api/periods' `earliest`/`latest`
    are ALL blocks (the Usage tab's range), `carbon_earliest`/`carbon_latest` the carbon range
    — the reverse of data-bounds' names, so the mapping is pinned.
  * Each view from /api/energy?resolution=period&include=carbon|usage (were
    /api/{insights,usage}/calendar-{month,year}, URLs built by concatenation): the same
    aggregators; the facet's body is flattened with period start/end/label, the shape the old
    routes returned. In a browser on a production-like database the flattened payloads were
    key-for-key identical to the old routes' for months and years on both tabs, except the
    Usage `net_cost`, now the bill's per-day penny rounding (the Billing tab's).
  * The period and its comparison are fetched together, and a superseded load cannot render
    (they were sequential, with no guard against a slow, older response).
  * A restored Year view syncs its buttons and compare row; the unreachable 'bill' mode is gone.

These fail on the parent tree.
"""
import os
import re
import unittest

_SRC = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "web", "templates", "insights.html"), encoding="utf-8").read()


def _fn(name):
    m = re.search(r"(?:async )?function " + name + r"\([^)]*\)\s*\{(.*?)\n\}", _SRC, re.S)
    assert m, name
    return m.group(1)


class InsightsReadsTheOneEndpoint(unittest.TestCase):

    def test_no_old_route_is_called(self):
        code = re.sub(r"//[^\n]*", "", _SRC)      # comments may still name them
        for old in ("/api/insights/", "/api/usage"):
            self.assertNotIn(old, code)
        self.assertNotIn("apiBase", code)

    def test_bounds_and_trend_in_one_request_with_the_names_mapped(self):
        init = _fn("initInsights")
        self.assertIn("/api/periods?kind=billing&include=carbon", init)
        self.assertIn("earliest: bd.carbon_earliest", init)      # Carbon tab: carbon range
        self.assertIn("{ earliest: bd.earliest,", init)           # Usage tab: all blocks
        self.assertIn("period_start: p.start", init)

    def test_a_view_is_the_facet_flattened_with_the_period(self):
        self.assertIn("'/api/energy?resolution=period&include=' + facet", _fn("_insEnergyUrl"))
        f = _fn("_insFetchPeriod")
        self.assertIn("Object.assign({}, j[facet] || {}", f)
        for k in ("period_start", "period_end", "period_label"):
            self.assertIn(k + ": p." + k, f)


class TheLoadIsOrdered(unittest.TestCase):

    def test_main_and_compare_together(self):
        self.assertIn("Promise.all([", _fn("loadPeriod"))

    def test_a_superseded_load_does_not_render(self):
        lp = _fn("loadPeriod")
        self.assertIn("var seq = ++_insLoadSeq;", lp)
        self.assertLess(lp.index("if (seq !== _insLoadSeq) return;"), lp.index("renderUsage(d, cmpData)"))


class TheRestoredViewIsWhole(unittest.TestCase):

    def test_only_the_modes_the_page_has(self):
        self.assertIn("['month','year'].indexOf(_saved.mode)", _SRC)
        self.assertNotIn("'bill'", re.sub(r"//[^\n]*", "", _SRC))

    def test_the_mode_ui_is_synced_on_restore(self):
        self.assertIn("_insSyncModeUI();", _fn("initInsights"))


if __name__ == "__main__":
    unittest.main()
