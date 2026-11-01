"""
The year's start picker in the eras modal's order (1 Nov 2026, the user: "the era selecting needs to have
the same ordering applied as per the modal").

The picker listed the tariffs oldest first and the eras as stored, kinds mixed. Now each choice names its
group and the picker draws an <optgroup> a group: Year (1 January, the billing anniversary), Tariffs newest
first, then the eras by kind in ERA_KINDS' order — Solar and Battery together first — newest first in each
(erasGroups, the modal's own). A choice's id is unchanged, so a saved start still holds.

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


TARIFFS = [{"direction": "import", "name": "Go", "from": "2023-09-21"},
           {"direction": "import", "name": "Intelligent Go", "from": "2025-07-01"},
           {"direction": "export", "name": "Outgoing", "from": "2024-05-01"},
           {"direction": "import", "name": "Flexible", "from": "2023-06-03"}]
ERAS = [{"id": "car1", "kind": "ev", "label": "Corsa", "start": "2023-09-21"},
        {"id": "car2", "kind": "ev", "label": "Ariya", "start": "2024-06-04"},
        {"id": "bat1", "kind": "battery", "label": "Battery", "start": "2024-07-14"},
        {"id": "pv", "kind": "solar", "label": "Solar", "start": "2024-07-14"},
        {"id": "bat2", "kind": "battery", "label": "Bigger battery", "start": "2025-08-20"}]


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheOrder(unittest.TestCase):

    def _run(self, expr):
        js = ("var BAR_ACC_MON = %s, barData = [{year: 2023, month: 6, day: 5}], barBillingDay = 3;\n"
              "var barTariffs = %s, barEras = %s;\n") % (
            json.dumps(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]),
            json.dumps(TARIFFS), json.dumps(ERAS)) + _kinds() + \
            "\n".join(_fn(n) for n in ("barAccMs", "barAccDateLabel", "erasGroups", "barTariffPriceStarts", "barTariffChangeDays", "barAccStartChoices",
                                       "barAccStartOptions")) + \
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

    def test_tariffs_newest_first_eras_by_kind_newest_first(self):
        out = self._run("barAccStartChoices().map(function (o) { return [o.group, o.id]; })")
        self.assertEqual(out, [["Year", "jan"], ["Year", "billing"],
                               ["Tariffs", "tariff:2025-07-01"], ["Tariffs", "tariff:2023-09-21"],
                               ["Tariffs", "tariff:2023-06-03"],
                               ["Eras: Solar", "era:pv"],
                               ["Eras: Battery", "era:bat2"], ["Eras: Battery", "era:bat1"],
                               ["Eras: EV", "era:car2"], ["Eras: EV", "era:car1"]])

    def test_an_optgroup_a_group_the_current_selected(self):
        html = self._run("barAccStartOptions(barAccStartChoices(), 'era:bat1')")
        self.assertEqual(html.count("<optgroup"), 5)
        self.assertEqual(html.count("</optgroup>"), 5)
        self.assertIn('<optgroup label="Eras: Battery"><option value="era:bat2">Era: Bigger battery (20 Aug 2025)'
                      '</option><option value="era:bat1" selected>', html)

    def test_a_label_is_escaped(self):
        html = self._run("barAccStartOptions([{id: 'era:x', label: 'A <b> & \"c\"', group: 'Eras: Other'}], '')")
        self.assertIn('>A &lt;b> &amp; &quot;c&quot;</option>', html)


class TheView(unittest.TestCase):

    def test_the_picker_draws_them(self):
        i = PAGE.index("function barAccSyncUI(")
        body = PAGE[i:PAGE.index("\n}\n", i)]
        self.assertIn("ss.innerHTML = barAccStartOptions(starts, barAccStart);", body)


if __name__ == "__main__":
    unittest.main()
