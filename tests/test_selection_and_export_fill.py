"""
The heatmap's selection, and filling a missing export (22 Oct 2026, the user's three).

1. A selection over red-hatched export (`no_data`) offered no way to fill it from a CSV or the PDF
   bills. Any selection holding it now offers *Fill the missing export… from PDF bills* and *…from
   a CSV*, each opening Historical Import on the span, export only (`channel=export`). A CSV fills
   only an empty channel, so the import EMT holds stays.
2. A drag opened its menu on release, couldn't reach past the window, and nothing else selected.
   - The release leaves the selection outlined; a right click (a long press) inside it opens the
     selection's menu.
   - A shift-click extends it from the last click.
   - Near the window's edge a drag scrolls the page.
   - Escape lets it go; dismissing the menu keeps it.
3. `no_agreement` is not an issue: no export agreement, nothing paid, nothing to fix. CSV history
   there reads as CSV history again, and a missing export there is `no_agreement`, not a gap. The
   health counts file goes to version 4.

Discriminating: every test here fails on the parent but the GUARDs.
"""
import os
import sys
import unittest
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import settlement_state as ss                  # noqa: E402

NOW = datetime(2026, 10, 22, 12, 0)
BS = "2024-08-05T12:00:00"


def _export(covered, **kw):
    f = {"block_start": BS, "imp_kwh": 0.4, "imp_cost": 0.1, "source": "imported_api",
         "exp_kwh": None, "exp_kwh_api": None}
    f.update(kw)
    return ss.classify(f, smb=False, now=NOW, export_channel=True,
                       export_agreement_at=lambda s: covered)["export"]


def _t(name):
    with open(os.path.join(_ROOT, "web", "templates", name), encoding="utf-8") as f:
        return f.read()


class NoAgreement(unittest.TestCase):

    def test_csv_history_stays_csv_history(self):
        self.assertEqual(_export(False, source="imported_csv", exp_kwh=0.8), "imported")

    def test_a_missing_export_is_not_a_gap(self):
        """An API import with no export figure, outside any agreement: nothing to miss."""
        self.assertEqual(_export(False), "no_agreement")

    def test_inside_an_agreement_it_is_a_gap(self):
        """GUARD: as 21 Oct 2026 (test_imported_export_gaps.py)."""
        self.assertEqual(_export(True), "no_data")
        self.assertEqual(_export(True, source="imported_csv", exp_kwh=0.8), "imported")

    def test_the_health_counts_are_counted_again(self):
        with open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8") as f:
            self.assertIn("\n_HEALTH_FILE_V = 4\n", f.read())

    def test_not_an_issue(self):
        hm = _t("heatmap_module.html")
        i = hm.index("    export: {", hm.index("var ISSUE = {")); rule = hm[i:hm.index("},", i)]
        self.assertNotIn("no_agreement", rule)


class FillTheMissingExport(unittest.TestCase):

    def setUp(self):
        self.hm = _t("heatmap_module.html")

    def test_the_span_is_its_no_data_half_hours(self):
        fn = self.hm[self.hm.index("function exportGapSpan(from, to) {"):]
        fn = fn[:fn.index("\n  }\n")]
        self.assertIn("settleValue('export', s[x]) !== 'no_data'", fn)
        self.assertIn("return n ? {from: lo, to: plus30(hi), n: n} : null;", fn)

    def test_both_sources_open_export_only(self):
        fn = self.hm[self.hm.index("function exportFillItems(from, to) {"):]
        fn = fn[:fn.index("\n  }\n")]
        self.assertIn("'&channel=export&gap_from='", fn)
        self.assertIn("open('bills')", fn)
        self.assertIn("open('csv')", fn)

    def test_every_menu_offers_it(self):
        fn = self.hm[self.hm.index("function toolItems(from, to, label) {"):]
        fn = fn[:fn.index("\n  }\n")]
        self.assertIn("exportFillItems(from, to)", fn)
        for caller in ("function selectionMenu(ev) {", "function dayBarPick(p, day, ev) {",
                       "row.addEventListener('contextmenu'"):
            body = self.hm[self.hm.index(caller):]
            self.assertIn("toolItems(", body[:body.index("\n  }\n")], caller)

    def test_the_import_page_reads_the_channel(self):
        hi = _t("historical_import.html")
        fn = hi[hi.index("function bfOpenGapFromUrl() {"):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn("var ch = q.get('channel') === 'export' ? 'export' : null;", fn)
        self.assertIn("csvOpen({from: from, to: to, channel: ch, inclusive: false});", fn)


class TheSelection(unittest.TestCase):

    def setUp(self):
        self.hm = _t("heatmap_module.html")

    def _fn(self, head):
        fn = self.hm[self.hm.index(head):]
        return fn[:fn.index("\n  });\n")]

    def test_the_release_opens_no_menu(self):
        up = self._fn("document.addEventListener('mouseup', function () {")
        self.assertNotIn("showMenu(", up)
        self.assertIn("state.sel = d.span;", up)

    def test_a_right_click_inside_opens_the_selections_menu(self):
        cm = self._fn("row.addEventListener('contextmenu', function (ev) {")
        self.assertIn("if (isData() && inSelection(p, h)) { selectionMenu(ev); return; }", cm)

    def test_shift_click_extends_from_the_anchor(self):
        click = self._fn("row.addEventListener('click', function (ev) {")
        self.assertIn("var sp = dragSpan(state.anchor, {p: p, h: h});", click)
        down = self._fn("row.addEventListener('mousedown', function (ev) {")
        self.assertIn("if (ev.shiftKey && state.anchor) return;", down)

    def test_the_drag_scrolls_at_the_edge(self):
        move = self._fn("document.addEventListener('mousemove', function (ev) {")
        self.assertIn("if (!autoS.raf) autoS.raf = requestAnimationFrame(autoScroll);", move)
        fn = self.hm[self.hm.index("function autoScroll() {"):]
        self.assertIn("dragTo(autoS.x,", fn[:fn.index("\n  }\n")])

    def test_escape_lets_it_go_and_the_menu_keeps_it(self):
        self.assertIn("if (state.sel) { state.sel = null; clearSelection(); }", self.hm)
        self.assertIn("if (state.sel) markSelection(state.sel.from, state.sel.to);   // the selection stays", self.hm)

    def test_a_month_drawn_inside_it_is_outlined(self):
        fn = self.hm[self.hm.index("function drawMonth(key) {"):]
        self.assertIn("if (state.sel) markSelection(state.sel.from, state.sel.to);", fn[:fn.index("\n  }\n")])


if __name__ == "__main__":
    unittest.main()
