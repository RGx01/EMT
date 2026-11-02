"""
C §2 rule 2 — tariff context is a required argument, never a default.

`.build/check_tariff_defaults.py` finds every defaulted parameter, in the pricing and
serving modules, whose name is in its tariff-context vocabulary (cap state, rate
schedules, timezone, VAT). It began as a ratchet: 35 when written, 30 Sep 2026; 0 on
2 Oct 2026, when the baseline was deleted and the ratchet became this assertion — a new
`tz_name=None` fails on the commit that adds it.

Why a default is the defect: 4.5.13's /api/charts/regenerate passed no `cap_from`,
so `capped=False` and the same function drew a different chart depending on its
caller — invisible at the call site, two days to find. And 30 Sep 2026: day
rollups grouped by UTC because nothing made the timezone a question.
"""
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, ".build"))

import check_tariff_defaults as T       # noqa: E402


class TheRule(unittest.TestCase):

    def test_no_tariff_context_has_a_default(self):
        self.assertEqual(T.findings(), [], "tariff context must be a required argument (C §2 rule 2)")


class TheDetector(unittest.TestCase):
    """GUARD: it catches the cases it exists for, and not its near misses."""

    def _scan(self, src):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "billing.py"), "w").write(src)
            return T.findings(root=d)

    def test_it_catches_the_4_5_13_shape(self):
        f = self._scan("def render(blocks, cap_from=None, import_schedule=None): pass\n")
        self.assertEqual(f, ["billing.py:render(cap_from=None)",
                             "billing.py:render(import_schedule=None)"])

    def test_a_timezone_default_is_context(self):
        self.assertEqual(self._scan("def days(a, *, tz_name='UTC'): pass\n"),
                         ["billing.py:days(tz_name='UTC')"])

    def test_a_required_argument_is_fine(self):
        self.assertEqual(self._scan("def days(a, tz_name): pass\n"), [])

    def test_a_row_limit_or_mode_flag_is_not_context(self):
        self.assertEqual(self._scan("def f(cap=20000, hold_rate=False): pass\n"), [])


if __name__ == "__main__":
    unittest.main()
