"""
BL-104 PR 3 (1 Nov 2026, accumulation design §2; home eras design §3.2) — when the year starts.

The Running total view's years run from a chosen day: **1 January**; **the billing anniversary** —
the billing day of the month the first bill started; **a tariff's start** — each stored import
agreement; or **an era's start**. A row's year is the year its year began in; the line's Year by year
axis, the spiral's turn (the start at the top) and its month spokes, the years' chips, the table and
the labels all follow. A year not starting on 1 January is named "2025/26". A start on a day its month
lacks (29 February) falls on the month's last. A choice that is gone (a tariff or era removed) is
1 January again. Changing it shows every year re-cut. The spiral's negative £ reads "−£216".

Checked in the local preview on 1 Nov 2026, on a scratch copy of staging: on the billing anniversary
(3 June) the years 2023/24 to 2026/27, the chips and table following, and the year totals summing to
the same lifetime figure as on 1 January (£781.10); the spiral turned with June at the top.

Discriminating: every test fails on the parent tree.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(_ROOT, "web", "templates", "charts.html"), encoding="utf-8").read()


def _fn(name):
    i = PAGE.index("function " + name + "(")
    line = PAGE[i:PAGE.index("\n", i)]
    if line.count("{") == line.count("}"):
        return line + "\n"
    return PAGE[i:PAGE.index("\n}\n", i) + 2]


def _kinds():
    i = PAGE.index("var ERA_KINDS = {")
    return PAGE[i:PAGE.index("\n};\n", i) + 3] + "\n"


NAMES = ("barAccMs", "barAccStartChoices", "erasGroups", "barTariffPriceStarts", "barTariffChangeDays", "barAccDateLabel", "barAccAnchor", "barAccStartMs", "barAccYearOf",
         "barAccDoy", "barAccYearDays", "barAccYearLabel", "barAccDayLabel", "barAccMonthOffsets", "_spLbl")


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheStart(unittest.TestCase):

    def _run(self, start, expr, data=None, billing_day=3):
        rows = data or [{"year": 2023, "month": 6, "day": 5}, {"year": 2024, "month": 3, "day": 1}]
        js = ("var BAR_ACC_MON = %s, barData = %s, barBillingDay = %d, barAccStart = %s;\n"
              "var barTariffs = [{direction: 'import', name: 'Go', from: '2024-03-14'}, "
              "{direction: 'export', name: 'Outgoing', from: '2024-05-01'}];\n"
              "var barEras = [{id: 'abc12345', label: 'Added battery', start: '2024-02-29'}];\n") % (
            json.dumps(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]),
            json.dumps(rows), billing_day, json.dumps(start)) + _kinds() + \
            "\n".join(_fn(n) for n in NAMES) + "\nconsole.log(JSON.stringify(" + expr + "));"
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_the_choices(self):
        ids = self._run("jan", "barAccStartChoices().map(function (o) { return o.id; })")
        self.assertEqual(ids, ["jan", "billing", "tariff:2024-03-14", "era:abc12345"])   # import tariffs only

    def test_the_billing_anniversary_is_the_first_bills_month(self):
        self.assertEqual(self._run("billing", "barAccAnchor()"), {"m": 6, "d": 3})
        # a first day before the billing day: its bill began the month before
        self.assertEqual(self._run("billing", "barAccAnchor()", billing_day=10), {"m": 5, "d": 10})

    def test_a_rows_year_is_the_year_its_year_began(self):
        self.assertEqual(self._run("tariff:2024-03-14", "[barAccYearOf({year: 2025, month: 3, day: 13}), "
                                   "barAccYearOf({year: 2025, month: 3, day: 14})]"), [2024, 2025])
        self.assertEqual(self._run("tariff:2024-03-14", "barAccDoy({year: 2025, month: 3, day: 16})"), 2)

    def test_29_february_falls_on_the_months_last(self):
        self.assertEqual(self._run("era:abc12345", "[barAccYearOf({year: 2025, month: 2, day: 28}), "
                                   "barAccYearDays(2024)]"), [2025, 365])

    def test_a_year_named_by_its_span(self):
        self.assertEqual(self._run("jan", "barAccYearLabel(2025)"), "2025")
        self.assertEqual(self._run("billing", "barAccYearLabel(2025)"), "2025/26")

    def test_a_choice_that_is_gone_is_1_january(self):
        self.assertEqual(self._run("era:gone0000", "barAccAnchor()"), {"m": 1, "d": 1})

    def test_the_months_from_the_start(self):
        out = self._run("billing", "barAccMonthOffsets().slice(0, 2)")
        self.assertEqual(out, [{"m": 6, "days": 28}, {"m": 7, "days": 59}])    # 1 Jul, 1 Aug from 3 Jun

    def test_a_negative_pound_on_the_spiral(self):
        self.assertEqual(self._run("jan", "_spLbl(-216, '£', {u: '£', d: 1})"), "−£216")


class TheView(unittest.TestCase):

    def test_the_picker_and_its_state(self):
        self.assertIn('<select id="bar-acc-start" onchange="barSetAccStart(this.value)"', PAGE)
        self.assertIn("accStart:    barAccStart,", _fn("barSaveState"))
        self.assertIn("barActiveYears = new Set(barGetYears());", _fn("barSetAccStart"))

    def test_the_view_counts_by_it(self):
        self.assertIn("y = barAccYearOf(r);", _fn("barAccSeriesOf"))
        self.assertIn("barActiveYears.has(barAccYearOf(r))", _fn("barAccRows"))
        self.assertIn("if (barRunning()) return [...new Set(barData.map(function(r) { return barAccYearOf(r); }))].sort();",
                      _fn("barGetYears"))

    def test_the_spiral_turns_with_it(self):
        self.assertIn("return barAccDoy(r) / barAccYearDays(barAccYearOf(r));", _fn("_spFrac"))
        self.assertIn("var m = o.m, a = G.A0 + (o.days / 365) * 2 * Math.PI;", _fn("barAccSpiral"))


if __name__ == "__main__":
    unittest.main()
