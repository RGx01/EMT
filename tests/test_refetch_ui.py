"""
Settlement surfaces PR 4 (design §5.1): acting on the heatmap — selection, the context menu, the
re-fetch dialog, the panel's Re-fetch button. STRUCTURAL (no JS runtime in the suite); the flow was
run in the browser pane against a fake restated Octopus on the 13 Oct dev backup: right click a
10 Oct cell, re-fetch the day, 25 bills 5% -> 0% shown, applied, the day reads billed.

Also: a quiet refresh() left a drawn month with a null grid, which broke the hover and the click
after the first poll — fixed here.

Discriminating: every test here fails on the parent.
"""
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(_ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8").read()
SERVER = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()


def _fn(name):
    i = PAGE.index("function %s(" % name)
    return PAGE[i:PAGE.index("\n  }\n", i)]


class TheActions(unittest.TestCase):

    def test_a_right_click_offers_details_and_re_fetch(self):
        i = PAGE.index("row.addEventListener('contextmenu'")
        body = PAGE[i:i + 1500]
        for needle in ("Show details", "Re-fetch this half-hour from Octopus", "openRefetch(dr[0], dr[1]"):
            self.assertIn(needle, body)
        self.assertIn("state.canRefetch", body)            # only on a supplier-API account

    def test_a_drag_selects_a_span(self):
        self.assertIn("row.addEventListener('mousedown'", PAGE)
        self.assertIn("state.dragFrom", _fn("attachHover"))

    def test_the_day_column_makes_a_range_on_a_phone(self):
        body = _fn("dayBarPick")
        self.assertIn("state.pickedDay", body)
        self.assertIn("tap another day", body)

    def test_the_panel_has_the_re_fetch_button(self):
        self.assertIn('data-act="refetch"', _fn("renderPanel"))


class TheDialog(unittest.TestCase):

    def test_a_long_range_runs_a_month_at_a_time(self):
        self.assertIn("var REFETCH_DAYS = 31;", PAGE)
        self.assertIn("step = REFETCH_DAYS * 86400e3", _fn("refetchChunks"))

    def test_nothing_is_written_without_a_confirm(self):
        body = _fn("openRefetch")
        self.assertIn("/api/settlement/refetch/preview", body)
        self.assertIn('data-act="apply"', body)
        self.assertIn("if (!sm.differ) { i++; next(); return; }", body)   # nothing different: on
        i = body.index("function apply(token)")
        self.assertIn("/api/settlement/refetch/apply", body[i:])

    def test_a_bill_not_re_settled_is_said(self):
        self.assertIn("stored but not re-settled", _fn("openRefetch"))

    def test_the_diff_shows_the_ev_share_only_where_it_moves(self):
        self.assertIn("var evMoved", _fn("diffTable"))


class TheFixes(unittest.TestCase):

    def test_a_quiet_refresh_keeps_the_grid(self):
        i = PAGE.index("function refresh() {")
        body = PAGE[i:PAGE.index("\n  }\n", i)]
        self.assertIn("state.panels[k].grid = monthGrid(k);", body)

    def test_the_apply_route_checks_with_its_own_reader(self):
        self.assertIn("refetch_apply_check(token, store_access.store())", SERVER)


if __name__ == "__main__":
    unittest.main()
