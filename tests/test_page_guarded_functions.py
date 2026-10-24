"""
A page's `typeof f === 'function'` guards name functions the page defines (24 Oct 2026).

Engine step 2d-1 (#284) removed the pricing-health card from Historical Import, and the cut took
`goStep` with it. Every caller guarded it — `if (typeof goStep === 'function') goStep('intro')` —
so nothing failed: the gap list's "🩹 API →" and Data Management's "Recover every gap" simply did
nothing. A guard is for a function that may not be there yet, not for one that is gone.

Discriminating: fails on the parent (goStep is missing there).
"""
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PAGES = ("historical_import.html", "data_management.html", "heatmap_module.html", "billing_module.html")


class TheGuards(unittest.TestCase):

    def test_every_guarded_function_is_defined(self):
        for page in _PAGES:
            path = os.path.join(_ROOT, "web", "templates", page)
            if not os.path.exists(path):
                continue
            src = open(path, encoding="utf-8").read()
            for name in set(re.findall(r"typeof (\w+) === 'function'", src)):
                with self.subTest(page=page, name=name):
                    defined = re.search(r"function %s\(|\b%s\s*=\s*function|\b%s\s*:\s*function"
                                        % (name, name, name), src)
                    known = name in ("Chart", "EMTHeatmap", "apiUrl")   # libraries and the shell's
                    self.assertTrue(defined or known, "%s guards %s, which it does not define"
                                    % (page, name))

    def test_historical_import_shows_its_steps(self):
        src = open(os.path.join(_ROOT, "web", "templates", "historical_import.html"),
                   encoding="utf-8").read()
        self.assertIn("function goStep(s){", src)


if __name__ == "__main__":
    unittest.main()


class TheSupportButton(unittest.TestCase):
    """24 Oct 2026, staging: after a support file was made the button became "Download", and
    reopening the dialog reset its label but not its click, so it downloaded the last file again
    instead of making one. Fails on the parent."""

    SRC = open(os.path.join(_ROOT, "web", "templates", "data_management.html"), encoding="utf-8").read()

    def _fn(self, name):
        i = self.SRC.index("function " + name + "(")
        return self.SRC[i:self.SRC.index("\n}\n", i)]

    def test_making_resets_label_and_click_together(self):
        self.assertIn("b.textContent = 'Make the file'; b.onclick = buildSupport;", self._fn("_ssMake"))

    def test_opening_and_a_new_span_make_a_new_file(self):
        self.assertIn("_ssMake();", self._fn("openSupport"))
        i = self.SRC.index("document.addEventListener('change', function (e) {")
        self.assertIn("_ssMake(); supportEstimate();", self.SRC[i:i + 400])
