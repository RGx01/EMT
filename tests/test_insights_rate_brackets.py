"""
BL-98 (1 Nov 2026) — Insights Usage's Rate Period Usage card becomes Rate Brackets.

The card listed the period's grid import by exact rate, and folded to one "avg of N rates" row past
five rates (`_collapse_rate_tiers`), so on Agile it said nothing. Its footnote said it read "the
import rate recorded on each half-hour block"; it had read the price slices since the segments
landed. It now reads the rate brackets (BL-93) — Usage Stats' read, `/api/energy?fields=brackets` by
day for the month or year in view, summed — for the whole meter: a row per 2.5p bracket, cheapest
first, with its kWh, £, average p/kWh and share, in Usage Stats' colours. On a fixed-rate tariff a
bracket's average is the rate. Hidden when the import fell in one bracket, as it was for one flat
rate. A link opens Usage Stats on its Rate brackets view (`/charts?tab=bar&show=brackets`).

Checked in the local preview on 1 Nov 2026, on a scratch copy of staging: October 2026's brackets
935.0 and 0.9 kWh, the card's old tiers to the kWh, and their sum the month's import (935.914 kWh);
2025's three brackets summing to its 11,423.2 kWh, two old tiers (27.77p, 28.12p) in one; the link
opening Usage Stats on the whole meter's brackets.

The per-device "Rate breakdown" lines are unchanged: the dispatch EV is no series of the brackets.

Discriminating: every test fails on the parent tree.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INS = open(os.path.join(_ROOT, "web", "templates", "insights.html"), encoding="utf-8").read()
CHARTS = open(os.path.join(_ROOT, "web", "templates", "charts.html"), encoding="utf-8").read()


def _fn(page, name):
    for sig in ("async function " + name + "(", "function " + name + "("):
        if sig in page:
            i = page.index(sig)
            return page[i:page.index("\n}\n", i) + 2]
    raise ValueError(name)


HARNESS = r"""
var els = {'ucard-rates': {style: {}}, 'u-rate-bars': {innerHTML: ''}, 'u-rates-link': {href: ''}};
var document = {getElementById: function (id) { return els[id] || null; },
                documentElement: {getAttribute: function () { return 'dark'; }}};
var BASE_PATH = '/x', _insMode = 'month', _calYear = 2026, _calMonth = 10, asked = [];
function apiUrl(u) { return u; }
var ROWS = __ROWS__;
function fetch(u) { asked.push(u); return Promise.resolve({json: function () {
  return Promise.resolve({brackets: {width_p: 2.5, meter_series: 'meter', rows: ROWS}}); }}); }
__FUNCS__
loadUsageBrackets('£').then(function () {
  console.log(JSON.stringify({asked: asked, shown: els['ucard-rates'].style.display,
                              html: els['u-rate-bars'].innerHTML, link: els['u-rates-link'].href}));
});
"""


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheCard(unittest.TestCase):

    def _run(self, rows):
        funcs = "\n".join(_fn(INS, n) for n in ("_insBrPence", "_insBrLabel", "_insBrColor",
                                                 "loadUsageBrackets", "renderUsageBrackets"))
        js = "var _insBrSeq = 0;\n" + HARNESS.replace("__ROWS__", json.dumps(rows)).replace("__FUNCS__", funcs)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    # two days: off-peak at 7p both, a peak half-hour at 28p on one, a plunge on the other
    ROWS = [{"day": "2026-10-01", "b": {"meter": {"2": [10.0, 0.70], "11": [1.0, 0.28]},
                                        "house": {"2": [3.0, 0.21]}}},
            {"day": "2026-10-02", "b": {"meter": {"2": [5.0, 0.35], "-1": [2.0, -0.04]}}}]

    def test_it_asks_the_brackets_for_the_period_by_day(self):
        out = self._run(self.ROWS)
        self.assertEqual(out["asked"], ["/api/energy?resolution=day&fields=brackets&kind=month&year=2026&month=10"])

    def test_the_days_summed_a_row_a_bracket_cheapest_first(self):
        html = self._run(self.ROWS)["html"]
        rows = html.split('<div style="margin-bottom:14px;">')[1:]
        self.assertEqual(len(rows), 3)
        self.assertIn("−2.5 to 0p", rows[0])
        self.assertIn("5–7.5p", rows[1])
        self.assertIn("15.0 kWh", rows[1])                     # 10 + 5, the whole meter's, not the house's
        self.assertIn("£1.05", rows[1])
        self.assertIn("avg 7.0p", rows[1])
        self.assertIn("27.5–30p", rows[2])
        self.assertIn("(£0.04)", rows[0])                      # a plunge's credit

    def test_shown_and_linked(self):
        out = self._run(self.ROWS)
        self.assertEqual(out["shown"], "block")
        self.assertEqual(out["link"], "/x/charts?tab=bar&show=brackets")

    def test_one_bracket_says_nothing(self):
        self.assertEqual(self._run([{"day": "2026-10-01", "b": {"meter": {"2": [10.0, 0.7]}}}])["shown"], "none")


class ThePage(unittest.TestCase):

    def test_the_old_card_is_gone(self):
        self.assertNotIn("renderUsageTierBars", INS)
        self.assertNotIn("Rate periods are determined by the import rate recorded on each half-hour block", INS)
        self.assertIn("loadUsageBrackets(currency);", _fn(INS, "renderUsage"))

    def test_a_share_over_99_keeps_its_decimal(self):
        self.assertIn("var pctDisp = pct < 1 || (pct > 99 && pct < 100) ? pct.toFixed(1) : Math.round(pct);",
                      _fn(INS, "renderUsageBrackets"))

    def test_usage_stats_opens_on_the_brackets(self):
        f = _fn(CHARTS, "barShowFromUrl")
        self.assertIn("if (new URLSearchParams(location.search).get('show') !== 'brackets') return;", f)
        self.assertIn("barShow = 'brackets'; barBrSeries = 'meter';", f)
        load = _fn(CHARTS, "barLoadState")
        self.assertIn("if (!raw) { barShowFromUrl(); return; }", load)
        self.assertTrue(load.rstrip().endswith("barShowFromUrl();\n}") or "  barShowFromUrl();\n}" in load)


if __name__ == "__main__":
    unittest.main()
