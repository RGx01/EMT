"""
A change inside an agreement that is VAT's alone, labelled so (1 Nov 2026, the user: "label it VAT change").

for_display splits a price period at a VAT change, so a fixed tariff's 1 Oct 2026 (0% VAT) became a tick and a
"price change" in the year's start picker, though Octopus's price had not moved. Now a day where every period
starting on it has the ex-VAT value of the one of its kind, band and window ending there is a **VAT change**: the
picker reads "Intelligent Go (SMB) fixed — VAT change 1 Oct 2026", and the tariff tip from that day adds
"VAT change — the same prices before VAT". A day where any ex-VAT value moves — a variable tariff's price cap on
the same day as the VAT change — is still a price change.

Found from a support file's stored prices on 1 Nov 2026 (the fixed tariff's 1 Oct 2026 the only VAT-only day).

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


def _p(kind, band, window, frm, to, exc):
    return {"kind": kind, "band": band, "window": window, "from": frm, "to": to, "exc": exc, "inc": exc}


# A fixed tariff: one price throughout, split at the VAT change
FIXED = {"direction": "import", "name": "Fixed", "from": "2026-08-26", "to": None, "prices": [
    _p("standing", "", None, "2026-08-26", "2026-10-01", 50.0), _p("standing", "", None, "2026-10-01", None, 50.0),
    _p("unit", "off_peak", "23:30-05:30", "2026-08-26", "2026-10-01", 7.0),
    _p("unit", "off_peak", "23:30-05:30", "2026-10-01", None, 7.0),
    _p("unit", "peak", "05:30-23:30", "2026-08-26", "2026-10-01", 25.0),
    _p("unit", "peak", "05:30-23:30", "2026-10-01", None, 25.0)]}
# A variable tariff: the price cap moves its peak rate on the VAT change's day
VARIABLE = {"direction": "import", "name": "Variable", "from": "2026-07-01", "to": None, "prices": [
    _p("standing", "", None, "2026-07-01", "2026-10-01", 50.0), _p("standing", "", None, "2026-10-01", None, 50.0),
    _p("unit", "peak", "05:30-23:30", "2026-07-01", "2026-10-01", 25.0),
    _p("unit", "peak", "05:30-23:30", "2026-10-01", None, 26.5)]}


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheLabel(unittest.TestCase):

    def _run(self, tariffs, expr):
        js = ("var BAR_ACC_MON = %s, barData = [{year: 2026, month: 6, day: 1}], barBillingDay = 3, barEras = [];\n"
              "var barTariffs = %s;\nfunction erasGroups() { return []; }\n") % (
            json.dumps(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]),
            json.dumps(tariffs)) + \
            "\n".join(_fn(n) for n in ("barAccMs", "barAccDateLabel", "barTariffPriceStarts", "barTariffChangeDays",
                                       "barTariffVatOnly", "barAccStartChoices")) + \
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

    def _labels(self, tariffs):
        return self._run(tariffs, "barAccStartChoices().filter(function (o) { return o.id === 'tariff:2026-10-01'; })"
                                  ".map(function (o) { return o.label; })")

    def test_vat_alone_is_a_vat_change(self):
        self.assertEqual(self._labels([FIXED]), ["   Fixed — VAT change 1 Oct 2026"])

    def test_a_price_moving_the_same_day_is_a_price_change(self):
        self.assertEqual(self._labels([VARIABLE]), ["   Variable — price change 1 Oct 2026"])

    def test_a_day_with_no_period_before_it_is_not_vat_alone(self):
        a = {"direction": "import", "name": "X", "from": "2026-08-26", "to": None,
             "prices": [_p("unit", "flat", None, "2026-09-15", None, 20.0)]}
        self.assertEqual(self._run([a], "barTariffVatOnly(barTariffs[0], '2026-09-15')"), False)


class TheTip(unittest.TestCase):

    def test_the_tip_says_so(self):
        f = _fn("barTariffShow")
        self.assertIn("if (at.from !== a.from && barTariffVatOnly(a, at.from)) {", f)
        self.assertIn("'VAT change \\u2014 the same prices before VAT'", f)


if __name__ == "__main__":
    unittest.main()
