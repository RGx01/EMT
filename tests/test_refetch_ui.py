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
        self.assertIn("state.drag = {p: p, h: h, span: null}", _fn("attachHover"))

    def test_the_day_column_offers_its_day(self):
        body = _fn("dayBarPick")
        self.assertIn("openRefetch(dr[0], dr[1], day)", body)

class TheSelection(unittest.TestCase):
    """PR 5 on the Pi, 14 Oct 2026: a tap on 10 Oct's day bar offered 10 Aug -> 10 Oct — an
    earlier tap had been kept as a range start, silently — and nothing showed what a click,
    a drag or a day covered. A span of days is now a drag only. Discriminating: each fails on
    the tree before #159."""

    def test_a_day_tap_keeps_nothing_for_the_next_tap(self):
        """The 10 Aug -> 10 Oct case: no picked day survives a tap, so no hidden range."""
        self.assertNotIn("pickedDay", PAGE)
        self.assertNotIn("Start a range", PAGE)

    def test_what_is_selected_is_outlined(self):
        self.assertIn("function markSelection(from, to)", PAGE)
        self.assertIn(".hm-sel {", PAGE)
        for ctx in ("row.addEventListener('click'", "row.addEventListener('contextmenu'"):
            i = PAGE.index(ctx)
            self.assertIn("markSelection(", PAGE[i:i + 900], ctx)
        self.assertIn("markSelection(from, to)", _fn("openRefetch"))

    def test_the_outline_goes_when_the_dialog_or_menu_does(self):
        self.assertIn("state.dialogOpen = false; clearSelection();", PAGE)
        i = PAGE.index("function showMenu(")
        self.assertIn("clearSelection()", PAGE[i:i + 900])

    def test_a_drag_is_outlined_while_it_is_held(self):
        """PR 5 on the Pi, 14 Oct 2026 (after #159): nothing showed until the button was released."""
        i = PAGE.index("document.addEventListener('mousemove'")
        self.assertIn("if (span) markSelection(span.from, span.to)", PAGE[i:i + 600])

    def test_a_drag_crosses_months(self):
        """After #160: each month is its own chart, and the drag lived on one month's row. It is
        now followed at document level and finds the month under the pointer."""
        self.assertIn("document.elementFromPoint(ev.clientX, ev.clientY)", _fn("hitAt"))
        self.assertNotIn("row.addEventListener('mouseup'", PAGE)
        i = PAGE.index("document.addEventListener('mouseup'")
        self.assertIn("openRefetch(sp.from, sp.to, sp.label)", PAGE[i:i + 700])

    def test_a_drag_runs_down_the_day_column(self):
        """After #160: a drag that started on a day bar was ignored. From a day bar both ends are
        whole days."""
        i = PAGE.index("row.addEventListener('mousedown'")
        self.assertNotIn("!h.tot", PAGE[i:i + 400])
        self.assertIn("if (d.h.tot || at.h.tot)", _fn("dragEnd"))

    def test_the_release_does_not_close_its_own_menu(self):
        self.assertIn("!state.dragEnded) dismiss();", PAGE)

    def test_the_dialog_names_its_exact_span(self):
        self.assertIn("esc(localTime(from)) + ' \\u2192 ' + esc(localTime(to))", _fn("openRefetch"))

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

    def test_a_replaced_correction_is_said_before_and_after(self):
        """14 Oct 2026: a re-fetch may now replace a correction — the dialog says so first."""
        self.assertIn("(your correction)", _fn("diffTable"))
        body = _fn("openRefetch")
        self.assertIn("your own correction with Octopus", body)
        self.assertIn("replaced your correction", body)
        self.assertNotIn("Your corrections and device", body)

    def test_the_diff_labels_emts_own_split(self):
        """PR 5 on the Pi, 14 Oct 2026: the "stored" EV share was EMT's own, unlabelled."""
        body = _fn("diffTable")
        self.assertIn("r.bill.stored_ev_is_bill === false", body)
        self.assertIn("(EMT\\u2019s split)", body)


class TheFixes(unittest.TestCase):

    def test_a_quiet_refresh_keeps_the_grid(self):
        i = PAGE.index("function refresh() {")
        body = PAGE[i:PAGE.index("\n  }\n", i)]
        self.assertIn("state.panels[k].grid = monthGrid(k);", body)

    def test_the_apply_route_checks_with_its_own_reader(self):
        self.assertIn("refetch_apply_check(token, store_access.store())", SERVER)


class TheDialogFits(unittest.TestCase):
    """PR 5 on the Pi, 14 Oct 2026: the diff scrolled sideways inside the dialog, and a re-fetch
    before the bill floor said nothing about why no bill was asked. Discriminating."""

    def test_the_diff_wraps_instead_of_scrolling(self):
        self.assertNotIn(".hm-diff th, .hm-diff td { font-size:12px; white-space:nowrap; }", PAGE)
        self.assertIn("width:min(640px, 100%); max-height:90vh; overflow-y:auto;", PAGE)

    def test_a_range_before_the_floor_is_said(self):
        self.assertIn("when EMT starts taking Octopus", _fn("floorNote"))
        body = _fn("openRefetch")
        self.assertIn("if (sm.before_floor) state.floorNote = floorNote(sm);", body)


if __name__ == "__main__":
    unittest.main()
