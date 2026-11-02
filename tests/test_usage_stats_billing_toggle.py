"""
Usage Stats, Daily: a billing period survives a trip through Calendar (31 Oct 2026).

With Daily and Billing, a past billing period chosen, Calendar showed the month it starts in, but
Billing again showed the latest period: Billing -> Calendar turned the period into its start month,
and nothing turned the month back, so Billing took any month as no period and fell to the latest.

What they pin (`barBillingKeyFor`, used where Billing normalises the period — barRender and the
sub-bar):

- **A month goes back to the period that starts in it**, the reverse of Billing -> Calendar.
- **Failing that, the one running on the month's 1st**, and for a month before the data its first.
- **Only with nothing usable, the latest** — as before for a first visit.

Checked in the local preview on 31 Oct 2026, on a scratch copy of staging: four periods (Jul 2023,
Jun, Sep and Oct 2026) through Billing -> Calendar -> Billing each came back as picked, and Mar 2025
chosen in Calendar came to Billing as 3 Mar – 2 Apr 2025.

Structural, as the page's other Usage Stats tests are (no JavaScript runtime here). Discriminating:
every test fails on the parent.
"""
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(_ROOT, "web", "templates", "charts.html"), encoding="utf-8").read()


def _fn(name):
    i = PAGE.index("function " + name + "(")
    return PAGE[i:PAGE.index("\n}\n", i) + 2]


class TheMapping(unittest.TestCase):

    def setUp(self):
        self.fn = _fn("barBillingKeyFor")

    def test_a_month_goes_back_to_the_period_starting_in_it(self):
        self.assertIn("var ym = dm.year + '-' + String(dm.month).padStart(2, '0');", self.fn)
        self.assertIn("var starts = keys.filter(function(k) { return k.slice(0, 7) === ym; });", self.fn)
        self.assertIn("if (starts.length) return starts[0];", self.fn)

    def test_else_the_one_running_on_its_first(self):
        self.assertIn("var before = keys.filter(function(k) { return k <= ym + '-01'; });", self.fn)
        self.assertIn("return before.length ? before[before.length - 1] : keys[0];", self.fn)

    def test_a_period_already_chosen_is_kept_and_only_nothing_falls_to_the_latest(self):
        self.assertIn("if (typeof dm === 'string' && keys.indexOf(dm) >= 0) return dm;", self.fn)
        self.assertTrue(self.fn.rstrip().endswith("return keys[keys.length - 1];\n}"))


class WhereBillingUsesIt(unittest.TestCase):

    def test_both_places_that_normalise_the_period(self):
        self.assertIn("barDailyMonth = barBillingKeyFor(barDailyMonth, _allBKeys);", _fn("barRender"))
        self.assertIn("barDailyMonth = barBillingKeyFor(barDailyMonth, allBKeys);", _fn("barBuildSubBar"))
        self.assertNotIn("barDailyMonth = _allBKeys.length > 0 ? _allBKeys[_allBKeys.length - 1] : null;", PAGE)
        self.assertNotIn("barDailyMonth = allBKeys.length > 0 ? allBKeys[allBKeys.length - 1] : null;", PAGE)


if __name__ == "__main__":
    unittest.main()
