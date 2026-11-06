"""
A half-hour whose main block has no import kWh is a gap (5 Nov 2026, a fresh install's support file, b11).

The install's import kept the export of September and October and lost the import (the import's first
chunk failed; fixed in `test_import_channel_keeps_its_window`). The rows were there, so nothing called them
missing: the heatmap, Data health and the gaps list showed no gap, the gap fill had nothing to fill, and a
whole-history import had nothing below its ceiling — no way to ask Octopus for them again. The gap reads
now count a main block with no `imp_kwh` as missing, a settled figure alone included:
`find_block_gaps`, and with `with_import` the gap fill's `get_block_starts_in_range` and
`get_neighbour_block_starts` (`gap_fill_plan`, `gap_bounds`); the heatmap's Missing marking likewise.

Discriminating: the tests not marked GUARD fail on the parent.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine as real_engine                     # noqa: E402
from tests.test_gap_fill_server import NOW, _hh, _st   # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEATMAP = open(os.path.join(_ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8").read()

DAY = _hh("2026-10-10T00:00:00", 48)
NO_IMPORT = DAY[20:26]                     # 10:00–12:30: the export kept, the import lost


def _fn(name):
    i = HEATMAP.index("function %s(" % name)
    depth, j = 0, HEATMAP.index("{", i)
    while True:
        depth += {"{": 1, "}": -1}.get(HEATMAP[j], 0)
        if HEATMAP[j] == "}" and depth == 0:
            return HEATMAP[i:j + 1]
        j += 1


def _node(body):
    """The page's isMissing, missingText and markMissing's rule for a cell, run in node."""
    m = HEATMAP.index("      c.missing = (c.missing || 0) + 1;")
    rule = HEATMAP[m:HEATMAP.index("      var del = (M.deleted", m)]
    src = ("var MISSING_TEXT = 'no block for this half-hour \\u2014 missing';\n" + _fn("isMissing") + "\n" + _fn("missingText")
           + "\nfunction isMissingAfter(kwh) { var iso = '2026-11-03T21:00:00'; "
             "var c = {kwh: kwh, starts: [iso]};\n" + rule + "\nreturn c; }\n" + body)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "t.js")
        with open(p, "w", encoding="utf-8") as f:
            f.write(src)
        r = subprocess.run(["node", p], capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout)


def _store():
    st = _st(DAY)
    st._conn.execute("UPDATE blocks SET imp_kwh=NULL, exp_kwh=0.3 WHERE block_start IN (%s)"
                     % ",".join("?" * len(NO_IMPORT)), NO_IMPORT)
    st._conn.commit()
    return st


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheGaps(unittest.TestCase):

    def test_the_gap_list_counts_them(self):
        g = _store().find_block_gaps(min_age_hours=0, now=NOW)
        self.assertEqual([(x["start"], x["end"], x["slots"]) for x in g],
                         [(NO_IMPORT[0], NO_IMPORT[-1], 6)])

    def test_the_fill_plans_them(self):
        p = real_engine.gap_fill_plan(_store(), [[DAY[0], "2026-10-11T00:00:00"]], now=NOW)
        self.assertEqual(p["fill_runs"], [[NO_IMPORT[0], DAY[26]]])

    def test_a_selected_one_widens_to_its_gap(self):
        self.assertEqual(real_engine.gap_bounds(_store(), [NO_IMPORT[2]], now=NOW),
                         [[NO_IMPORT[0], DAY[26]]])

    def test_the_heatmap_marks_them(self):
        i = HEATMAP.index("function markMissing(")
        body = HEATMAP[i:i + 1500]
        self.assertTrue(re.search(r"x\.imp_kwh !== null", body), "a block with no import is not 'have'")

    def test_a_settled_figure_alone_is_still_a_gap(self):
        """6 Nov 2026, the same install: the tail a fill left under 48 h took the poll's `imp_kwh_api`.
        The totals read `imp_kwh`, so its kWh was in none of them; the heatmap marked it missing and
        the server, which accepted the settled figure, had nothing to fill."""
        st = _store()
        st._conn.execute("UPDATE blocks SET imp_kwh_api=0.5 WHERE block_start=?", (NO_IMPORT[0],))
        st._conn.commit()
        g = st.find_block_gaps(min_age_hours=0, now=NOW)
        self.assertEqual([x["slots"] for x in g], [6])
        p = real_engine.gap_fill_plan(st, [[DAY[0], "2026-10-11T00:00:00"]], now=NOW)
        self.assertEqual(p["fill_runs"], [[NO_IMPORT[0], DAY[26]]])

    def test_a_click_on_one_offers_the_fill(self):
        """6 Nov 2026, b14: `isMissing` asked for `kwh === null`, and these cells hold their export, so a click
        opened the block's details, with Re-fetch and no Fill. Run in node, as the page's own functions."""
        out = _node("""
          var cell = isMissingAfter(-2.551);
          console.log(JSON.stringify({held: isMissing(cell), starts: cell.starts, gaps: cell.gaps, flag: cell.noImport,
                                      text: missingText(cell)}));""")
        self.assertEqual(out, {"held": True, "starts": ["2026-11-03T21:00:00"], "gaps": ["2026-11-03T21:00:00"],
                               "flag": True, "text": "no usage figure for this half-hour \u2014 missing"})

    def test_GUARD_a_half_hour_with_no_block_is_still_missing(self):
        out = _node("""
          var c = {kwh: null, missing: 1};
          console.log(JSON.stringify([isMissing(c), missingText(c)]));""")
        self.assertEqual(out, [True, "no block for this half-hour \u2014 missing"])

    def test_GUARD_the_clocks_going_back_is_not_missing(self):
        # one of a cell's two half-hours held with usage, the other missing: the cell has a figure
        out = _node("""
          var c = {kwh: 1.2, missing: 1, starts: ['a', 'b'], gaps: ['b']};
          console.log(JSON.stringify(isMissing(c)));""")
        self.assertEqual(out, False)

    def test_GUARD_the_deletes_still_see_every_block(self):
        self.assertEqual(len(_store().get_block_starts_in_range("electricity_main", DAY[0],
                                                                "2026-10-11T00:00:00")), 48)


if __name__ == "__main__":
    unittest.main()
