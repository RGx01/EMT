"""
Settings shows what it is doing, and what went wrong (Pi run, 12 Oct 2026).

Since BL-6 (#219) the page's notices go through the notification region (`show()` ->
EMT.notify), and its #alert-success / #alert-error divs stay `hidden`. Ten places still wrote to
#alert-error directly with `classList.add('show')`, which does nothing to a hidden element without
the .alert class: the reset's "deleting all data…" never appeared, and nor did a failed reset,
delete, disconnect or reactivate. The reset's restart notice was a 6-second toast for a restart
that takes longer.

STRUCTURAL (no JS runtime in the suite). Discriminating: every test here fails on the parent.
"""
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(_ROOT, "web", "templates", "meter_config.html"), encoding="utf-8").read()


class TheSettingsNotices(unittest.TestCase):

    def test_nothing_writes_to_the_hidden_alert_divs_directly(self):
        self.assertNotIn("classList.add('show')", PAGE)
        self.assertNotIn("classList.remove('show')", PAGE)

    def test_the_reset_shows_progress_until_the_page_moves_on(self):
        i = PAGE.index("fetch(apiUrl('/api/meter/main/reset')")
        before, after = PAGE[i - 400:i], PAGE[i:i + 1600]
        self.assertIn("progress(", before, "nothing is shown while the reset runs")
        self.assertIn("progress('✓ All data deleted", after, "the restart notice is not persistent")
        j = PAGE.index("function progress(")
        self.assertIn("transient: false", PAGE[j:PAGE.index("\n}\n", j)])


class TheWizardFinish(unittest.TestCase):

    def test_the_page_is_drawn_with_the_chosen_mode(self):
        """Pi run, 12 Oct 2026 (finding 5): finishing the wizard redrew the page before the mode
        was saved, with the page-load mode (`unset`) and the key the credentials step had saved —
        "API is configured but inactive" until the next refresh. STRUCTURAL; fails on the parent."""
        i = PAGE.index("async function wizApplyAndClose()")
        body = PAGE[i:PAGE.index("\n}\n", i)]
        j = body.index("  renderAll();")
        self.assertIn("DATA_SOURCE_MODE = wiz.data.mode", body[:j])
        self.assertLess(j, body.index("/api/data-source-mode"))


if __name__ == "__main__":
    unittest.main()
