"""
Charts' topbar on a phone (31 Oct 2026): no sideways scroll.

At 410 px the topbar, one nowrap row of the title, the four tab icons, the settle note ("⏳ 118
awaiting kWh") and PDF, was 506 px wide; `.main` scrolled sideways and the whole page with it.

What they pin:

- **On a narrow screen the topbar wraps** (the ≤768 px / landscape rule that set it nowrap).
- **Below 560 px the note goes last**, onto a row of its own under the title, tabs and PDF, and may
  wrap there. Wider, it all fits one row in its own order (note, then PDF); desktop is unchanged.

Checked in the local preview on 31 Oct 2026 at 375, 410, 600, 768 and 1280 px, with the note's
one-stage, three-stage and API-mode texts: `.main` never wider than the screen; at 768 and 1280 one
row, the note before PDF.

Structural (no browser here). Discriminating: every test fails on the parent.
"""
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(_ROOT, "web", "templates", "charts.html"), encoding="utf-8").read()


class TheNarrowTopbar(unittest.TestCase):

    def test_it_wraps(self):
        self.assertIn(".topbar { padding: 8px 12px; gap: 6px; row-gap: 6px; flex-wrap: wrap; align-items: center; }", PAGE)
        self.assertNotIn(".topbar { padding: 8px 12px; gap: 6px; flex-wrap: nowrap;", PAGE)

    def test_on_a_phone_the_note_goes_last_and_may_wrap(self):
        m = re.search(r"@media \(max-width: 560px\) \{\s*(\.topbar \.dcc-settle-note \{[^}]*\})", PAGE)
        self.assertIsNotNone(m)
        rule = m.group(1)
        for part in ("order: 3;", "max-width: 100%;", "white-space: normal;"):
            self.assertIn(part, rule)


if __name__ == "__main__":
    unittest.main()
