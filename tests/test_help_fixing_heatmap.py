"""
BL-105 PR 2 (1–2 Nov 2026, the user): Data Management's help says how to fix each state of the data heatmap,
what a red day Octopus has not costed means, and that a costed half-hour turning red after a re-fetch is
outside EMT's control — with the key's swatches.

A *Fixing the heatmap* card (`#fixing`) lists each Import bill and Export bill state with its swatch, what it
means and what to do; the red one names its four reasons; red hatched says only Octopus can supply the cost; a
section explains the re-fetch reds, with the one EMT-side case (the log line it writes). The swatches are the
heatmap's own colours: the test reads them from `settleColour` and the hatches from `swatchCss`, so a colour
changed there fails here. The old colour sentence in *The data heatmap* card goes, pointing to the new card.

Checked in the local preview on 2 Nov 2026, light and dark.

Discriminating: every test fails on the parent tree.
"""
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELP = open(os.path.join(_ROOT, "web", "templates", "help", "data_management.html"), encoding="utf-8").read()
HM = open(os.path.join(_ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8").read()
ENGINE = open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read()


def _section():
    i = HELP.index('id="fixing"')
    return HELP[i:HELP.index('<div class="card help-section">', i)]


class TheSwatches(unittest.TestCase):

    def test_the_colours_are_the_heatmaps(self):
        c = dict(re.findall(r"(\w+): '(#[0-9a-f]{6})'", HM[HM.index("function settleColour("):
                                                          HM.index("return by[v]", HM.index("function settleColour("))]))
        for kind in ("green", "teal", "lamber", "amber", "red", "blue"):
            self.assertIn(".hsw-%s { background:%s; }" % (kind, c[kind]), HELP)
        self.assertIn(".hsw-grey { background:#d4d7de; } [data-theme=\"dark\"] .hsw-grey { background:#3a3e4a; }", HELP)

    def test_the_hatches_are_the_keys(self):
        f = HM[HM.index("  function swatchCss("):HM.index("\n  }\n", HM.index("  function swatchCss("))]
        self.assertIn("diag('#000', '#6b7080')", f)
        self.assertIn("diag('#cc0000', '#f2c94c')", f)
        self.assertIn(".hsw-missing { background:#000 repeating-linear-gradient(135deg,#6b7080 0 1.2px,transparent 1.2px 4px); }", HELP)
        self.assertIn(".hsw-abandoned { background:#cc0000 repeating-linear-gradient(135deg,#f2c94c 0 1.2px,transparent 1.2px 4px); }", HELP)


class TheCard(unittest.TestCase):

    def test_a_state_a_swatch_and_what_to_do(self):
        s = _section()
        self.assertEqual(s.count("{{ sw('"), 15)            # nine Import bill states, six Export bill
        for words in ("Re-fetch from Octopus</strong> asks now", "Fill this\n      gap</strong>",
                      "Fill the missing export</strong> from your PDF bills or a CSV"):
            self.assertIn(words, s)

    def test_red_names_its_four_reasons(self):
        s = _section()
        for words in ("Not final after 7 days", "Octopus's figures disagree",
                      "A completed dispatch with no House/EV split", "The devices cost more than the meter"):
            self.assertIn(words, s)

    def test_the_uncosted_days_and_the_refetch_reds(self):
        s = _section()
        self.assertIn("only Octopus can supply it", s)
        self.assertIn("a re-fetch never takes away a cost", s)
        self.assertIn("outside EMT's\n    control", s)

    def test_the_emt_side_case_quotes_the_log_line(self):
        line = "the bill is stored, the block did not\n    re-settle from it"
        self.assertIn(line, _section())
        self.assertIn("the bill is stored, the block did not \"\n                               \"re-settle from it", ENGINE)

    def test_the_ask_schedule_is_the_engines(self):
        self.assertIn("hourly\n          for two days, then daily, for 30 days", _section())
        self.assertIn("_BILL_ASK_DAYS = 30", ENGINE)
        self.assertIn("_BILL_ASK_HOURLY_DAYS = 2", ENGINE)

    def test_the_old_colour_sentence_points_here(self):
        self.assertNotIn("<em>Light amber</em>: the kWh is", HELP)
        self.assertIn('<a href="#fixing">Fixing the heatmap</a>', HELP)


if __name__ == "__main__":
    unittest.main()
