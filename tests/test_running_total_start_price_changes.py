"""
A price change inside an agreement as a year's start (1 Nov 2026, the user: "the tariff portion of the eras
needs a slight enhancement to include that variations in agreements when a price change occurred within the
agreement. e.g. agreement - date of change").

Under each tariff in the picker, newest first, the days its prices changed after its start — the bars' minor
ticks, barTariffPriceStarts — indented: "Intelligent Go — price change 1 Oct 2025". Only up to today (a
change Octopus has scheduled is not a year that has begun) and before the agreement's end. A day already a
choice is not listed twice. The id is tariff:<date>, as an agreement's start, so the anchor reads it the same.

The preview's scratch copy holds no stored prices; checked there with prices injected on 1 Nov 2026.

Discriminating: every test fails on the parent tree but test_a_day_already_a_choice_is_not_twice, a GUARD
(the parent lists no change at all, so no day twice).
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


def _p(frm, kind="unit"):
    return {"kind": kind, "band": "flat", "from": frm, "inc": 20}


TARIFFS = [
    {"direction": "import", "name": "Go", "from": "2023-09-21", "to": "2024-06-12",
     "prices": [_p("2023-09-21"), _p("2023-10-01"), _p("2024-01-01", "standing"), _p("2024-04-01"),
                _p("2024-07-01")]},                                     # after its end: not listed
    {"direction": "import", "name": "Intelligent Go", "from": "2024-06-12", "to": None,
     "prices": [_p("2024-06-12"), _p("2025-04-01"), _p("2099-04-01")]},  # a scheduled change: not listed
    {"direction": "export", "name": "Outgoing", "from": "2024-05-01", "to": None,
     "prices": [_p("2025-01-01")]},                                     # export: never a start
]


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheChanges(unittest.TestCase):

    def _run(self, expr, tariffs=TARIFFS, start="jan"):
        js = ("var BAR_ACC_MON = %s, barData = [{year: 2023, month: 6, day: 5}], barBillingDay = 3;\n"
              "var barTariffs = %s, barEras = [], barAccStart = %s;\n") % (
            json.dumps(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]),
            json.dumps(tariffs), json.dumps(start)) + _kinds() + \
            "\n".join(_fn(n) for n in ("barAccMs", "barAccDateLabel", "erasGroups", "barTariffPriceStarts", "barTariffChangeDays",
                                       "barAccStartChoices", "barAccAnchor")) + \
            "\nconsole.log(JSON.stringify(" + expr + "));"
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_each_tariffs_changes_under_it_newest_first(self):
        ids = self._run("barAccStartChoices().filter(function (o) { return o.group === 'Tariffs'; })"
                        ".map(function (o) { return o.id; })")
        self.assertEqual(ids, ["tariff:2024-06-12", "tariff:2025-04-01",
                               "tariff:2023-09-21", "tariff:2024-04-01", "tariff:2024-01-01", "tariff:2023-10-01"])

    def test_the_label_names_the_agreement_and_the_day(self):
        labels = self._run("barAccStartChoices().filter(function (o) { return o.id === 'tariff:2025-04-01'; })"
                           ".map(function (o) { return o.label; })")
        self.assertEqual(labels, ["   Intelligent Go — price change 1 Apr 2025"])

    def test_a_change_is_a_start_like_any(self):
        self.assertEqual(self._run("barAccAnchor()", start="tariff:2023-10-01"), {"m": 10, "d": 1})

    def test_a_day_already_a_choice_is_not_twice(self):
        # GUARD: passes on the parent tree too
        tariffs = [{"direction": "import", "name": "A", "from": "2023-01-01", "to": "2023-06-01",
                    "prices": [_p("2023-01-01"), _p("2023-03-01")]},
                   {"direction": "import", "name": "B", "from": "2023-03-01", "to": None, "prices": []}]
        ids = self._run("barAccStartChoices().map(function (o) { return o.id; })", tariffs=tariffs)
        self.assertEqual(ids.count("tariff:2023-03-01"), 1)


if __name__ == "__main__":
    unittest.main()
