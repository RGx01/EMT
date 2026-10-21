"""
Gap fill on the heatmap (gap fill design §1–§4, PR 2) and Octopus's history row (decided 18 Oct
2026, the user).

- `markMissing` records which of a cell's half-hours are missing (`gaps`), and `missingRuns` turns
  the selection's into contiguous runs: the fill asks Octopus for those, not the whole selection.
- The menu offers "Fill this gap…" where the selection holds a missing half-hour — on a cell, a day,
  a drag, and the missing cell's panel — with or without the supplier API (18 Oct 2026, the user:
  a gap fills from a CSV or the PDF bills too). Any half-hour selected in a gap means the WHOLE gap:
  the server finds its bounds (`/api/gaps/around`).
- The dialog names the gap and offers three separate sources: Octopus (the API, only with it), a
  CSV, the PDF bills. The API route previews (nothing written), fills after the apply's backup,
  follows the job, and says what is still missing — counted again from the store — then redraws.
  CSV and PDF open the Fill History page as a modal over the heatmap, on that gap; closing it
  redraws. One gap at a time for those.
- History before EMT's oldest block, within Octopus's reach (`bounds.reach_from`), is its own row
  below the oldest month linking to the API import: not hatched, not an issue.

All discriminating (fail on the parent: none of it exists there) but `test_not_a_gap`, a guard. The heatmap cannot be run here,
so these read its source.
"""
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = open(os.path.join(_ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8").read()


def _fn(name):
    i = _SRC.index("function " + name + "(")
    return _SRC[i:_SRC.index("\n  }\n", i)]


class TheRuns(unittest.TestCase):

    def test_a_missing_cell_records_its_half_hours(self):
        self.assertIn("(c.gaps || (c.gaps = [])).push(iso);", _fn("markMissing"))

    def test_only_the_missing_half_hours_in_the_selection(self):
        body = _fn("missingRuns")
        self.assertIn("if (x >= from && x < to) all[x] = 1;", body)
        self.assertIn("c.gaps", body)
        self.assertNotIn("c.starts", body)            # a present half-hour is never asked for

    def test_contiguous_half_hours_make_one_run(self):
        self.assertIn("if (r && r[1] === x) r[1] = plus30(x); else runs.push([x, plus30(x)]);",
                      _fn("missingRuns"))


class TheMenu(unittest.TestCase):

    def test_offered_with_a_missing_half_hour_with_or_without_the_api(self):
        body = _fn("fillItem")
        self.assertNotIn("canRefetch", body)
        self.assertIn("if (!runs.length) return null;", body)

    def test_it_names_the_gap_not_the_half_hours(self):
        body = _fn("fillItem")
        self.assertIn("'Fill this gap…'", body)
        self.assertIn("openGapFill(runs.map(function (r) { return r[0]; }), label)", body)

    def test_on_a_cell_a_day_a_drag_and_the_missing_panel(self):
        self.assertIn("var fc = fillItem(h.starts[0]", _SRC)          # the cell's menu
        self.assertIn("var fd = fillItem(dr[0], dr[1], day);", _fn("dayBarPick"))
        self.assertIn("var fs = fillItem(sp.from, sp.to, sp.label);", _SRC)   # the drag
        self.assertIn("fillItem(g[0], plus30(g[g.length - 1]), label)", _fn("openMissing"))

    def test_the_missing_panel_has_one_action(self):
        body = _fn("openMissing")
        self.assertIn("Fill this gap\\u2026</button>", body)
        self.assertNotIn("fillday", body)

    def test_a_drag_and_the_day_column_work_without_the_api(self):
        self.assertIn("if (ev.button !== 0 || !isData()) return;", _SRC)
        self.assertIn("var items = state.canRefetch ? [{text: 'Re-fetch ' + day", _fn("dayBarPick"))


class TheDialog(unittest.TestCase):

    def test_the_server_finds_the_whole_gap_first(self):
        self.assertIn("postJSON('/api/gaps/around', {at: ats})", _fn("openGapFill"))

    def test_three_separate_sources(self):
        body = _fn("openGapFill")
        self.assertIn("(state.canRefetch ? card('api', 'From Octopus (API)'", body)
        self.assertIn("card('csv', 'From a CSV'", body)
        self.assertIn("card('bills', 'From Octopus PDF bills'", body)

    def test_csv_and_pdf_one_gap_at_a_time(self):
        body = _fn("openGapFill")
        self.assertIn("var oneOnly = one ? '' : 'One gap at a time", body)
        self.assertIn("openFillHistory(gaps[0], b.dataset.src)", body)

    def test_preview_the_whole_gaps_then_apply_with_the_token(self):
        body = _fn("openGapFill")
        self.assertIn("postJSON('/api/gaps/fill/preview', {runs: gaps.map(function (g) { return [g.from, g.to]; })})", body)
        self.assertIn("postJSON('/api/gaps/fill/apply', {token: token})", body)

    def test_the_preview_says_what_is_left_out_and_what_was_deleted(self):
        body = _fn("openGapFill")
        for s in ("t.too_recent", "t.too_old", "t.deleted", "Nothing here Octopus can fill"):
            self.assertIn(s, body)

    def test_a_status_from_before_the_fill_is_not_its_end(self):
        body = _fn("openGapFill")
        self.assertIn("if (FILL_GOING[s.status] && s.runs)", body)
        self.assertIn("else if (!seen && polls < 10) setTimeout(tick, 1000);", body)

    def test_stop_cancels_the_job(self):
        self.assertIn("postJSON('/api/historical/api-import/control', {action: 'cancel'})", _fn("openGapFill"))

    def test_the_result_counts_again_and_redraws(self):
        body = _fn("openGapFill")
        self.assertIn("postJSON('/api/gaps/fill/preview', {runs: before.fill_runs})", body)
        self.assertIn("left missing: Octopus had no data", body)
        self.assertIn("reloadRange(from, to);", body)


class TheFillHistoryModal(unittest.TestCase):

    def test_the_page_itself_on_the_gap(self):
        body = _fn("openFillHistory")
        self.assertIn("var f = document.createElement('iframe');", body)
        self.assertIn("apiUrl('/historical-import?embed=1&source='", body)
        self.assertIn("'&gap_from=' + encodeURIComponent(g.from) + '&gap_to=' + encodeURIComponent(g.to)", body)

    def test_closing_it_redraws(self):
        self.assertIn("refresh();", _fn("openFillHistory"))

    def test_embedded_the_page_has_no_navigation(self):
        base = open(os.path.join(_ROOT, "web", "templates", "base.html"), encoding="utf-8").read()
        self.assertIn("{% if request.args.get('embed') %}", base)
        self.assertIn(".sidebar, .sidebar-overlay, #mobile-topbar, .topbar { display:none !important; }", base)

    def test_the_page_opens_on_the_gap_from_its_url(self):
        hi = open(os.path.join(_ROOT, "web", "templates", "historical_import.html"), encoding="utf-8").read()
        i = hi.index("function bfOpenGapFromUrl("); body = hi[i:hi.index("\n}\n", i)]
        self.assertIn("csvOpen({from: from, to: to, channel: null, inclusive: false});", body)
        self.assertIn("if (scope) bfPickScope(scope);", body)
        self.assertIn("if (!from || !to || (src !== 'csv' && src !== 'bills')) return;", body)
        self.assertIn("document.addEventListener('DOMContentLoaded', bfOpenGapFromUrl);", hi)


class TheHistoryRow(unittest.TestCase):

    def test_its_own_row_after_the_oldest_month(self):
        body = _fn("reachRow")
        self.assertIn("(state.bounds || {}).reach_from", body)
        self.assertIn("wrap.appendChild(el);", body)
        self.assertIn("/historical-import#api-import-panel", body)
        self.assertIn("reachRow();\n    redrawAll(false);", _fn("buildMonths"))

    def test_only_with_the_api(self):
        self.assertIn("if (!(state.canRefetch && r)) { if (el) el.remove(); return; }", _fn("reachRow"))

    def test_not_a_gap(self):
        """GUARD (passes on the parent): the row does not change what is hatched."""
        # markMissing starts at EMT's oldest block, so nothing before it is hatched
        self.assertIn("var t = Math.max(ms(M.range.start_utc), ms(b.earliest_block));", _fn("markMissing"))
        self.assertNotIn("reach_from", _fn("markMissing"))

    def test_an_import_moves_it(self):
        self.assertIn("state.bounds = (pp && pp.bounds) || state.bounds; reachRow();", _fn("refresh"))


if __name__ == "__main__":
    unittest.main()
