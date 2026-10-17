"""
Issues first: the heatmap folds the days with nothing to look at (heatmap issues design §3, §6 (a),
(e), decided 17 Oct 2026, the user).

"Issues only" folds each run of two or more clean days into one row, which a click (or a tap, or
the menu) expands. An ISSUE is anything not green except CSV history and `none` — on the Import
bill teal, blue, the ambers, red and missing; on the Export bill `no_agreement`, `waiting`,
`overdue` and missing; on the energy views only missing. One rule (`isIssue`) for the fold and the
"To look at" totals, which counted amber and red only (rank >= 2) and now count the same issues.
A fold carries its days' half-hours, so a drag that starts or ends on one covers them, and a
selection across one outlines its row.

All discriminating (fail on the parent: none of it exists there). The heatmap cannot be run here,
so these read its source.
"""
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = open(os.path.join(_ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8").read()


def _fn(name):
    i = _SRC.index("function " + name + "(")
    return _SRC[i:_SRC.index("\n  }\n", i)]


class TheIssueRule(unittest.TestCase):

    def test_anything_not_green_but_csv_history_and_none(self):
        i = _SRC.index("var ISSUE = {"); rule = _SRC[i:_SRC.index("};", i)]
        for s in ("applied_emt_split: 1", "corrected: 1", "estimate_dcc: 1", "estimate_cad: 1",
                  "known: 1", "overdue: 1", "differs: 1", "flagged: 1", "missing: 1",
                  "no_agreement: 1", "waiting: 1"):
            self.assertIn(s, rule)
        for s in ("applied:", "imported:", "none:"):
            self.assertNotIn(s, rule)

    def test_missing_is_an_issue_on_every_view(self):
        self.assertIn("if (c.missing) return true;", _fn("isIssue"))

    def test_the_totals_count_the_same_issues(self):
        self.assertIn("filter(function (c) { return isIssue(c, m); }).length", _fn("monthGrid"))
        self.assertNotIn("CAT_RANK[m][c[m]] >= 2", _SRC)


class TheFolds(unittest.TestCase):

    def test_runs_of_two_or_more_clean_days_fold(self):
        body = _fn("foldRows")
        self.assertIn("if (!state.collapse) return days;", body)
        self.assertIn("run.length >= 2", body)
        self.assertIn("isIssue(c, m)", body)

    def test_a_fold_carries_its_days_half_hours(self):
        self.assertIn("starts: starts", _fn("foldRows"))
        self.assertIn("fold: f, starts: f.starts", _fn("hitCell"))

    def test_a_click_or_the_menu_expands_it(self):
        self.assertEqual(_SRC.count("if (h.fold) { expandFold(h.fold); return; }"), 2)
        self.assertIn("state.expanded[f.key + '|' + f.from + '|' + f.to] = true;", _fn("expandFold"))

    def test_every_reader_of_a_row_goes_through_rowOf(self):
        draw = _fn("drawMonth")
        self.assertIn("rowOf(p, d).slots", draw)
        self.assertIn("rowOf(p, d).total[m]", draw)
        self.assertIn("var g = rowOf(p, day);", _fn("dayRange"))
        self.assertIn("rowOf(p, day)", _fn("markSelection"))

    def test_a_selection_outlines_a_fold_it_covers(self):
        self.assertIn("run = [0, p.xs.length - 1]", _fn("markSelection"))


class TheToggle(unittest.TestCase):

    def test_it_is_in_the_bar_and_remembered(self):
        self.assertIn("fb.textContent = 'Issues only';", _SRC)
        self.assertIn("localStorage.setItem('emt_hm_collapse'", _SRC)
        self.assertIn("localStorage.getItem('emt_hm_collapse') === '1'", _SRC)

    def test_switching_metric_keeps_it_lit(self):
        self.assertIn("mb.querySelectorAll('button:not(.hm-collapse)')", _SRC)


if __name__ == "__main__":
    unittest.main()
