"""
Spiral (charts.html, window.EMTSpiral) — C step 1, part 3, 1 Oct 2026.

  * It reads every local day's row from /api/energy?resolution=day&kind=all&include=stats
    (usage_view.day_stats — the rows /api/charts/blocks-summary returned).
  * It refreshes. The data poll called EMTSpiral.init(), which returns at once after the
    first load, so the spirals never moved until the page was reloaded; the poll now calls
    refresh(), which re-fetches and redraws keeping the view.
  * It remembers its view between visits — grouping, winding, hidden years, the standing
    toggle — in localStorage (EMT.lsKey, as Usage Stats does), restored after the data loads
    and validated against it. They lived only in memory, so every visit opened on the
    defaults (reported 1 Oct 2026).

Checked in a browser on a production-like database: EV grouping, year-aligned winding and a
hidden year survive a reload and a refresh; a saved removed device, unknown winding, absent
year and non-boolean toggle fall back to Net / Lifetime / dropped / unchanged.

These fail on the parent tree.
"""
import os
import re
import unittest

_SRC = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "web", "templates", "charts.html"), encoding="utf-8").read()
_MOD = _SRC[_SRC.index("window.EMTSpiral=(function(){"):]
_MOD = _MOD[:_MOD.index("})();") + 5]


class SpiralReadsTheOneEndpoint(unittest.TestCase):

    def test_endpoint(self):
        """Through the shared day-row cache (2 Oct 2026), which reads /api/energy."""
        self.assertIn("var j=await EMTDayRows.get();", _MOD)
        self.assertIn("/api/energy?resolution=day&kind=all&fields=stats", _SRC)
        self.assertNotIn("blocks-summary\"", _MOD)


class SpiralRefreshes(unittest.TestCase):

    def test_the_module_exports_refresh(self):
        self.assertIn("return { init: init, refresh: refresh };", _MOD)

    def test_the_poll_calls_refresh_not_init(self):
        self.assertIn("if (window.EMTSpiral) EMTSpiral.refresh();", _SRC)
        self.assertNotRegex(_SRC, r"_activeChart === 'spiral'\) \{ if \(window\.EMTSpiral\) EMTSpiral\.init\(\)")


class SpiralRemembersItsView(unittest.TestCase):

    def test_every_control_saves(self):
        # grouping, years, winding, standing toggle
        for pat in (r"state\.grouping=it\.key;saveState\(\)",
                    r"state\.hidden\.add\(y\);saveState\(\)",
                    r"state\.mode=b\.dataset\.mode;saveState\(\)",
                    r"state\.incStanding=cb\.checked;saveState\(\)"):
            self.assertRegex(_MOD, pat)

    def test_restored_after_the_data_and_validated(self):
        load = _MOD[_MOD.index("async function load("):]
        self.assertLess(load.index("buildMetaFrom("), load.index("loadState();"))
        self.assertIn("groupingOptions().some(", _MOD)
        self.assertIn("YEARS.indexOf(y)>=0", _MOD)

    def test_the_key_is_per_instance(self):
        self.assertIn('EMT.lsKey("emt_spiral_state")', _MOD)


if __name__ == "__main__":
    unittest.main()
