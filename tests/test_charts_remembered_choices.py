"""
Charts keeps what you chose while what it depends on is still arriving (20 Nov 2026, the user: back from the
Overview, Year by year's era start had gone to 1 January; asked whether similar bugs lurk).

The Charts page draws before some of its data lands, and three remembered choices were checked against
lists that were not complete yet, so each was replaced and then saved:

- Running total's **year start** (a tariff's or an era's) before the tariffs and eras land — 1 January.
- Rate brackets' **series** (a device, or Export) before the brackets land — Import.
- Running total's **years** on a start other than 1 January: barLoadState checks them against calendar years,
  so a year that begins in the calendar year before the data's first (an October era over data from June
  2024 makes 2023/24) was dropped.

Each reproduced in the local preview on the demo household and is fixed there (four round trips to the
Overview kept 2023/24). Discriminating: every test fails on the parent tree except
test_kept_when_the_device_is_there and test_import_when_the_device_is_not, which are GUARDS (the picker
behaved so once the brackets had landed).
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


def _node(prelude, names, expr):
    js = prelude + "\n" + "\n".join(_fn(n) for n in names) + "\nconsole.log(JSON.stringify(" + expr + "));"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(js)
        path = fh.name
    try:
        res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
    finally:
        os.unlink(path)
    if res.returncode:
        raise AssertionError(res.stderr)
    return json.loads(res.stdout)


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheBracketsSeries(unittest.TestCase):

    PRE = ("var document = {getElementById: function () { return {innerHTML: ''}; }};\n"
           "var barPeriod = 'daily', barBrHH = null, barBrSeries = 'ev_charger';\n"
           "var barMeters = [{id: 'electricity_main', label: 'Direct'}, {id: 'ev_charger', label: 'EV', is_sub: true}];\n")

    def _series(self, days):
        return _node(self.PRE + "var barBrDays = %s;" % json.dumps(days),
                     ("barBrSeriesList", "barBrSyncPicker"), "(barBrSyncPicker(), barBrSeries)")

    def test_kept_until_the_brackets_land(self):
        self.assertEqual(self._series(None), "ev_charger")

    def test_kept_when_the_device_is_there(self):
        """GUARD."""
        self.assertEqual(self._series({"2026-11-01": {"ev_charger": {}, "meter": {}}}), "ev_charger")

    def test_import_when_the_device_is_not(self):
        """GUARD: a device with no brackets still falls back to Import."""
        self.assertEqual(self._series({"2026-11-01": {"meter": {}}}), "meter")


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheRunningYears(unittest.TestCase):

    def _years(self, eras, wanted, on=True):
        pre = ("var barTariffs = [], barEras = %s, barAccYearsWanted = %s, barActiveYears = new Set([2024, 2025]);\n"
               "function barGetYears() { return barEras === null ? [2024, 2025, 2026] : [2023, 2024, 2025, 2026]; }\n"
               % (json.dumps(eras), json.dumps(wanted)))
        return _node(pre, ("barSpansPending", "barAccYearsApply"),
                     "(barAccYearsApply(%s), [Array.from(barActiveYears).sort(), barAccYearsWanted])" % json.dumps(on))

    def test_waits_for_the_eras(self):
        self.assertEqual(self._years(None, [2023, 2024, 2025]), [[2024, 2025], [2023, 2024, 2025]])

    def test_then_the_saved_years_come_back(self):
        self.assertEqual(self._years([{"id": "x"}], [2023, 2024, 2025]), [[2023, 2024, 2025], None])

    def test_forgotten_off_running_total(self):
        self.assertEqual(self._years([{"id": "x"}], [2023], on=False), [[2024, 2025], None])

    def test_wired_in(self):
        self.assertIn("if (st.show === 'running') barAccYearsWanted = st.years.slice();", _fn("barLoadState"))
        self.assertIn("barAccYearsApply(on);", _fn("barAccSyncUI"))
        for loader in ("barLoadEras", "barLoadTariffs"):
            self.assertIn("|| barAccYearsWanted) && barInited && barData) barRender();", _fn(loader), loader)


if __name__ == "__main__":
    unittest.main()
