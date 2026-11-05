"""
A half-hour whose main block has no import kWh is a gap (5 Nov 2026, a fresh install's support file, b11).

The install's import kept the export of September and October and lost the import (the import's first
chunk failed; fixed in `test_import_channel_keeps_its_window`). The rows were there, so nothing called them
missing: the heatmap, Data health and the gaps list showed no gap, the gap fill had nothing to fill, and a
whole-history import had nothing below its ceiling — no way to ask Octopus for them again. The gap reads
now count a main block with no import kWh (`imp_kwh` and `imp_kwh_api` both NULL) as missing:
`find_block_gaps`, and with `with_import` the gap fill's `get_block_starts_in_range` and
`get_neighbour_block_starts` (`gap_fill_plan`, `gap_bounds`); the heatmap's Missing marking likewise.

Discriminating: the tests not marked GUARD fail on the parent.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine as real_engine                     # noqa: E402
from tests.test_gap_fill_server import NOW, _hh, _st   # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEATMAP = open(os.path.join(_ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8").read()

DAY = _hh("2026-10-10T00:00:00", 48)
NO_IMPORT = DAY[20:26]                     # 10:00–12:30: the export kept, the import lost


def _store():
    st = _st(DAY)
    st._conn.execute("UPDATE blocks SET imp_kwh=NULL, exp_kwh=0.3 WHERE block_start IN (%s)"
                     % ",".join("?" * len(NO_IMPORT)), NO_IMPORT)
    st._conn.commit()
    return st


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

    def test_a_settled_import_counts(self):
        st = _store()
        st._conn.execute("UPDATE blocks SET imp_kwh_api=0.5 WHERE block_start=?", (NO_IMPORT[0],))
        g = st.find_block_gaps(min_age_hours=0, now=NOW)
        self.assertEqual([x["slots"] for x in g], [5])

    def test_GUARD_the_deletes_still_see_every_block(self):
        self.assertEqual(len(_store().get_block_starts_in_range("electricity_main", DAY[0],
                                                                "2026-10-11T00:00:00")), 48)


if __name__ == "__main__":
    unittest.main()
