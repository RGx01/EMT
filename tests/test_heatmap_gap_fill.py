"""
Gap fill on the heatmap (gap fill design §1–§4, PR 2) and Octopus's history row (decided 18 Oct
2026, the user).

- `markMissing` records which of a cell's half-hours are missing (`gaps`), and `missingRuns` turns
  the selection's into contiguous runs: the fill asks Octopus for those, not the whole selection.
- The menu offers "Fill … from Octopus…" only where the selection holds a missing half-hour and
  the supplier API is connected: on a cell, a day, a drag, and the missing cell's panel.
- The dialog previews (nothing written), fills after the apply's backup, follows the job, and says
  what is still missing — counted again from the store — then redraws the months it touched.
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

    def test_offered_only_with_a_missing_half_hour_and_the_api(self):
        body = _fn("fillItem")
        self.assertIn("if (!state.canRefetch) return null;", body)
        self.assertIn("if (!n) return null;", body)

    def test_on_a_cell_a_day_a_drag_and_the_missing_panel(self):
        self.assertIn("var fc = fillItem(h.starts[0]", _SRC)          # the cell's menu
        self.assertIn("var fd = fillItem(dr[0], dr[1], day);", _fn("dayBarPick"))
        self.assertIn("var fs = fillItem(sp.from, sp.to, sp.label);", _SRC)   # the drag
        self.assertIn("fillItem(g[0], plus30(g[g.length - 1]), label)", _fn("openMissing"))


class TheDialog(unittest.TestCase):

    def test_preview_then_apply_with_the_token(self):
        body = _fn("openGapFill")
        self.assertIn("postJSON('/api/gaps/fill/preview', {runs: runs})", body)
        self.assertIn("postJSON('/api/gaps/fill/apply', {token: token})", body)

    def test_the_preview_says_what_is_left_out_and_what_was_deleted(self):
        body = _fn("openGapFill")
        for s in ("t.too_recent", "t.too_old", "t.deleted", "Nothing here can be filled"):
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


class TheHistoryRow(unittest.TestCase):

    def test_its_own_row_after_the_oldest_month(self):
        body = _fn("reachRow")
        self.assertIn("(state.bounds || {}).reach_from", body)
        self.assertIn("wrap.appendChild(el);", body)
        self.assertIn("/historical-import#api-import-panel", body)
        self.assertIn("reachRow();\n    redrawAll(false);", _fn("buildUI"))

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
