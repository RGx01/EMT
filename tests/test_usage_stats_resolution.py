"""Usage Stats: Period or Span, then what each bar holds (24 Nov 2026, the user).

The period row was HH / Daily / Monthly / Yearly / Span, with a Day / Month / Year row under
Span only, and Flow renamed the period buttons by what its one bar totals. It is now Period |
Span, and one resolution row, HH / Day / Month / Year, under both: under Period the resolution
picks the period shown (HH a day's half-hours, Day a month's days, Month the years' months, Year
the years); under Span it is the span's bars, with no HH. The stored period keeps its values
(hh, daily, monthly, yearly, span), so a remembered view comes back as it was.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
with open(os.path.join(ROOT, "web", "templates", "charts.html"), encoding="utf-8") as fh:
    PAGE = fh.read()


def _fn(name):
    i = PAGE.index("function " + name + "(")
    line = PAGE[i:PAGE.index("\n", i)]
    if line.count("{") == line.count("}"):
        return line + "\n"
    return PAGE[i:PAGE.index("\n}\n", i) + 2]


def _var(name):
    i = PAGE.index("var " + name + " =")
    return PAGE[i:PAGE.index("\n", i)] + "\n"


_STUBS = ("var barPeriod = 'daily', barLastPeriod = 'daily', barSpanRes = null, FLOW = false;\n"
          "function barFlow() { return FLOW; }\n"
          "function barSaveState() {} function barSyncUI() {} function barRender() {}\n")


def _node(body):
    src = (_STUBS + _var("BAR_RES_PERIOD") + _var("BAR_RES_PERIOD_FLOW") +
           _fn("barResOfPeriod") + _fn("barPeriodOfRes") + _fn("barResOffered") +
           _fn("barSetKind") + _fn("barSetRes") + "\n" + body)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(src)
        path = fh.name
    try:
        res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
    finally:
        os.unlink(path)
    if res.returncode != 0:
        raise AssertionError(res.stderr)
    return json.loads(res.stdout)


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheResolutionPicksThePeriod(unittest.TestCase):

    def test_under_period(self):
        got = _node("var seen = [];\n['hh', 'day', 'month', 'year'].forEach(function (r) {"
                    " barSetRes(r); seen.push(barPeriod); });\nconsole.log(JSON.stringify(seen));")
        self.assertEqual(got, ["hh", "daily", "monthly", "yearly"])

    def test_span_and_back_to_the_period_left(self):
        got = _node("barSetRes('month'); barSetKind('span'); var a = barPeriod;"
                    " barSetKind('period');\nconsole.log(JSON.stringify([a, barPeriod]));")
        self.assertEqual(got, ["span", "monthly"])

    def test_under_span_the_resolution_is_the_spans_bars_and_no_hh(self):
        got = _node("barSetRes('year'); barSetKind('span'); barSetRes('month'); var a = barSpanRes;"
                    " barSetRes('hh');\nconsole.log(JSON.stringify([barPeriod, a, barSpanRes]));")
        self.assertEqual(got, ["span", "month", "month"])

    def test_in_flow_a_bar_is_the_day_month_or_years(self):
        got = _node("FLOW = true; var seen = [];\n['day', 'month', 'year', 'hh'].forEach(function (r) {"
                    " barSetRes(r); seen.push(barPeriod); });\nconsole.log(JSON.stringify(seen));")
        # HH is not offered in Flow: the period stays the years
        self.assertEqual(got, ["hh", "daily", "monthly", "monthly"])

    def test_flow_back_from_span_never_lands_on_yearly(self):
        got = _node("barSetRes('year'); FLOW = true; barSetKind('span'); barSetKind('period');"
                    "\nconsole.log(JSON.stringify(barPeriod));")
        self.assertEqual(got, "monthly")

    def test_the_buttons_offered(self):
        got = _node("console.log(JSON.stringify([barResOffered('daily', false), barResOffered('daily', true),"
                    " barResOffered('span', false), barResOffered('span', true)]));")
        self.assertEqual(got, [["hh", "day", "month", "year"], ["day", "month", "year"],
                               ["day", "month", "year"], []])

    def test_the_active_button_of_a_stored_period(self):
        got = _node("console.log(JSON.stringify([barResOfPeriod('hh', false), barResOfPeriod('yearly', false),"
                    " barResOfPeriod('hh', true), barResOfPeriod('monthly', true), barResOfPeriod('yearly', true)]));")
        self.assertEqual(got, ["hh", "year", "day", "year", None])


class TheToolbar(unittest.TestCase):

    def test_period_or_span(self):
        seg = PAGE[PAGE.index('<div class="bar-seg" id="bar-period-seg">'):]
        seg = seg[:seg.index("</div>")]
        self.assertIn(">Period</button>", seg)
        self.assertIn(">Span</button>", seg)
        for gone in ("Daily", "Monthly", "Yearly", ">HH<"):
            self.assertNotIn(gone, seg)

    def test_one_resolution_row(self):
        seg = PAGE[PAGE.index('<div class="bar-seg" id="bar-res-seg"'):]
        seg = seg[:seg.index("</div>")]
        for r, label in (("hh", "HH"), ("day", "Day"), ("month", "Month"), ("year", "Year")):
            self.assertIn("onclick=\"barSetRes('%s',this)\" data-res=\"%s\"" % (r, r), seg)
            self.assertIn(">%s</button>" % label, seg)

    def test_the_period_left_is_remembered(self):
        self.assertIn("lastPeriod: barLastPeriod,", _fn("barSaveState"))
        load = _fn("barLoadState")
        self.assertIn("if (['daily','monthly','yearly','hh'].indexOf(st.lastPeriod) !== -1) barLastPeriod = st.lastPeriod;", load)
        self.assertIn("if (barPeriod !== 'span') barLastPeriod = barPeriod;", load)

    def test_the_pdf_names_the_bars(self):
        self.assertIn("add('Bars', seg('bar-res-seg'));", _fn("barPdfSettings"))


if __name__ == "__main__":
    unittest.main()
