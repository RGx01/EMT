"""
Data Management follows the data (22 Oct 2026, the user's two from staging).

1. **The purge's success banner stayed for good.** A success banner goes after 10 s; an error stays.
2. **After an Octopus import finished, Issues only still folded March 2025's red half-hours away.**
   The heatmap read the months' counts only on a purge or a modal's close. The import ran on for
   minutes after its modal was closed, so its counts came later and nothing told the heatmap. The page
   now asks for the counts every 60 s (30 s while a job runs; not while the tab is hidden), and when
   the months' counts move, or a job's freeze lifts, it refreshes the heatmap.

Discriminating: every test here fails on the parent but the GUARD.
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _dm():
    with open(os.path.join(ROOT, "web", "templates", "data_management.html"), encoding="utf-8") as f:
        return f.read()


def _fn(src, head):
    fn = src[src.index(head):]
    return fn[:fn.index("\n}\n")]


class TheSuccessBanner(unittest.TestCase):

    def test_goes_after_a_while(self):
        fn = _fn(_dm(), "function showSuccess(msg) {")
        self.assertIn("showSuccess._t = setTimeout(function () { el.classList.remove('show'); }, 10000);", fn)

    def test_an_error_stays(self):
        """GUARD: as before."""
        self.assertNotIn("setTimeout", _fn(_dm(), "function showError(msg) {"))


class TheStripFollows(unittest.TestCase):

    def setUp(self):
        self.fn = _fn(_dm(), "function loadHealth() {")

    def test_it_asks_again(self):
        self.assertIn("loadHealth._t = setTimeout(loadHealthSoon, busy ? 30000 : 60000);", self.fn)
        self.assertIn("if (document.hidden) { loadHealth._t = setTimeout(loadHealthSoon, 60000); return; }",
                      _fn(_dm(), "function loadHealthSoon() {"))

    def test_moved_counts_refresh_the_heatmap(self):
        self.assertIn("(sig !== loadHealth._sig || (loadHealth._busy && !busy))", self.fn)
        self.assertIn("EMTHeatmap.refresh();", self.fn)


if __name__ == "__main__":
    unittest.main()
