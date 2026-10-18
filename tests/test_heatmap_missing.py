"""
Missing half-hours on the heatmap (heatmap issues design §2, §6 (b)–(d), 17 Oct 2026, the user).

Rows were only the days that had blocks, so a gap of a day or a month closed up and disappeared,
and a missing half-hour inside a day was transparent — the same as a future half-hour, or the hour
the clocks skip. Now every UTC half-hour inside the data's span (the first block to the last
finished half-hour) with no main-meter block is MISSING: black and diagonally hatched on every
view, its day a row even with no blocks at all, and an issue on the bills (rank 3, as red). A
deleted half-hour is a gap like any other; its tooltip says when. The clocks-forward hour is never
missing: walking UTC half-hours, it does not come up.

The server sends what the grid needs: the first and last blocks' starts (`/api/periods`,
`bounds.earliest_block` / `latest_block` — `earliest` is a day), and the month's tombstones
(`/api/energy … include=gaps`).

Discriminating (fail on the parent): every test but test_gaps_only_at_block_resolution, a guard
(the parent refuses the facet with a 400 too, as unknown). test_the_clocks_forward_hour_is_never_missing
pins the walk's design. The heatmap cannot be run here, so the client tests read its source; the
server tests are real requests.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = open(os.path.join(_ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8").read()


def _fn(name):
    i = _SRC.index("function " + name + "(")
    return _SRC[i:_SRC.index("\n  }\n", i)]


class TheServer(unittest.TestCase):

    def _client(self):
        from tests.test_server import make_client, _make_test_store, MINIMAL_BLOCKS
        st = _make_test_store(MINIMAL_BLOCKS)
        self.st = st
        return make_client(store=st)

    def test_the_bounds_carry_the_first_blocks_start(self):
        c = self._client()
        b = json.loads(c.get("/api/periods?kind=month").data)["bounds"]
        first = self.st.get_block_bounds()["earliest"]
        self.assertEqual(b["earliest_block"], first)
        self.assertEqual(b["earliest"], first[:10])          # unchanged: a day

    def test_the_month_carries_its_tombstones(self):
        c = self._client()
        self.st.add_deleted_range("electricity_main", "2026-01-15T02:00:00", "2026-01-15T03:00:00",
                                  reason="user_delete")
        self.st.add_deleted_range("*", "2026-01-20T00:00:00", "2026-01-21T00:00:00", reason="user_delete")
        self.st.add_deleted_range("zappi", "2026-01-16T00:00:00", "2026-01-17T00:00:00",
                                  reason="user_delete")     # a sub-meter's: not the grid's
        self.st.add_deleted_range("electricity_main", "2025-12-01T00:00:00", "2025-12-02T00:00:00",
                                  reason="user_delete")     # another month
        r = c.get("/api/energy?resolution=block&kind=month&year=2026&month=1&include=gaps")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:200])
        got = sorted(x["start_utc"] for x in json.loads(r.data)["deleted"])
        self.assertEqual(got, ["2026-01-15T02:00:00", "2026-01-20T00:00:00"])

    def test_gaps_only_at_block_resolution(self):
        r = self._client().get("/api/energy?resolution=day&kind=month&year=2026&month=1&include=gaps")
        self.assertEqual(r.status_code, 400)


class TheGridWalk(unittest.TestCase):

    def test_it_walks_utc_half_hours_from_the_first_block_to_the_last_finished(self):
        body = _fn("markMissing")
        self.assertIn("ms(b.earliest_block)", body)
        self.assertIn("var lastDone = Math.floor((Date.now() - 120000) / HH) * HH - HH;", body)
        self.assertIn("new Date(t).toISOString().slice(0, 19)", body)
        self.assertIn("if (have[iso]) continue;", body)

    def test_a_day_with_no_blocks_gets_a_row(self):
        self.assertIn("var d = days[lp.day] || (days[lp.day] =", _fn("markMissing"))
        self.assertIn("markMissing(key, days, n);", _fn("monthGrid"))

    def test_the_clocks_forward_hour_is_never_missing(self):
        """GUARD (the design): the walk is over UTC half-hours, mapped to local slots — a local
        slot that no UTC half-hour maps to (the skipped hour) is never visited."""
        body = _fn("markMissing")
        self.assertIn("var lp = localParts(iso);", body)
        self.assertNotIn("slots()", body)

    def test_a_deleted_half_hour_is_still_missing_and_says_when(self):
        body = _fn("markMissing")
        self.assertIn("c.missing = (c.missing || 0) + 1;", body)
        self.assertIn("c.deleted =", body)
        self.assertIn("'<br>deleted ' + c.deleted", _fn("missingText"))


class TheCells(unittest.TestCase):

    def test_missing_is_an_issue_on_both_bills(self):
        i = _SRC.index("var CAT_RANK = {"); rank = _SRC[i:_SRC.index("};", i)]
        self.assertIn("flagged: 3, missing: 3}", rank)
        self.assertIn("overdue: 3, missing: 3}", rank)

    def test_it_is_hatched_on_every_view(self):
        self.assertIn("if (isMissing(r.c)) return hatch();", _SRC)
        self.assertIn("if (v === 'missing') return hatch();", _fn("settleColour"))
        self.assertIn("createPattern(c, 'repeat')", _fn("hatch"))

    def test_the_month_asks_for_its_tombstones(self):
        self.assertIn("'gaps'", _fn("fetchMonth"))
        self.assertIn("deleted: r.deleted || []", _fn("fetchMonth"))

    def test_a_missing_cell_does_not_fetch_a_detail(self):
        self.assertIn("openMissing(p.grid[h.day].slots[h.ci]", _SRC)
        self.assertIn("function openMissing(c, label, p, day)", _SRC)
        self.assertNotIn("fetchJSON", _fn("openMissing"))


if __name__ == "__main__":
    unittest.main()
