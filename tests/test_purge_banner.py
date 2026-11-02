"""
Deleting all imported history says so from its start (22 Oct 2026, the user: there was no banner
until it had finished). A working banner names the job's own step — the backup, then the delete —
and the success or error banner replaces it. Opened (or reloaded) while a purge runs, the page picks
it up; an old job's end is not shown again.

Discriminating: every test here fails on the parent.
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _t(name):
    with open(os.path.join(ROOT, "web", "templates", name), encoding="utf-8") as f:
        return f.read()


class ThePurgeBanner(unittest.TestCase):

    def setUp(self):
        self.dm = _t("data_management.html")

    def _fn(self, head):
        fn = self.dm[self.dm.index(head):]
        return fn[:fn.index("\n}\n")]

    def test_it_shows_before_the_request(self):
        fn = self._fn("function dmPurge() {")
        self.assertLess(fn.index("showWorking('Deleting all imported history: starting"),
                        fn.index("fetch(apiUrl('/api/historical/purge'), {method: 'POST'"))

    def test_it_names_the_step(self):
        fn = self._fn("function dmPurgeWatch() {")
        self.assertIn("showWorking('Deleting all imported history: ' + (s.step || 'working')", fn)
        self.assertIn("if (!document.getElementById('alert-working').classList.contains('show')) return;", fn)

    def test_a_page_opened_during_one_picks_it_up(self):
        self.assertIn("document.addEventListener('DOMContentLoaded', dmPurgeWatch);", self.dm)

    def test_the_end_replaces_it(self):
        for fn in ("function showSuccess(msg) {", "function showError(msg) {"):
            self.assertIn("document.getElementById('alert-working').classList.remove('show');", self._fn(fn))
        self.assertIn('<div id="alert-working" class="alert alert-info" role="status"></div>', self.dm)
        self.assertIn(".alert-info ", _t("base.html"))


if __name__ == "__main__":
    unittest.main()
