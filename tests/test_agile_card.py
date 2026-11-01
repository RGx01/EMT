"""
BL-100 (1 Nov 2026) — an Agile card on Overview, the 48-hour history card's style and size.

Where the import or export tariff is dynamic (the AGILE family, `tariff_prices.is_dynamic_code`),
Overview shows the half-hourly prices from 24 h back to as far ahead as Octopus has published (up to
24 h): import a bar a half-hour, green to red by price, blue below 0p; export a line; now dashed; the
past fainter. Above it: the price now, import and export, and the cheapest hour still to come.

The prices come from the schedules the poll's rate refresh already holds (`engine.agile_prices`,
`GET /api/agile-prices`) — no Octopus call of its own. So tomorrow's prices, published about 4pm,
appear at the poll after (it runs every 6 hours, or at Retry settlement).

Checked in the local preview on 1 Nov 2026 with the card inserted and made-up prices (the staging
copy has never been on Agile): the bars, the plunge below 0p, export, now, the summary line.

Discriminating: every test fails on the parent tree.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import engine                                    # noqa: E402

PAGE = open(os.path.join(_ROOT, "web", "templates", "live_power.html"), encoding="utf-8").read()
SERVER = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()

AGILE = "E-1R-AGILE-24-10-01-C"
OUTGOING = "E-1R-AGILE-OUTGOING-19-05-13-C"


class _Sched:
    """Prices to `last` (exclusive): 10p + the half-hour's index, export half that."""

    def __init__(self, last, half=False):
        self.last, self.half = last, half

    def resolve(self, ts):
        if ts >= self.last:
            return None
        t = datetime.fromisoformat(ts)
        v = 10.0 + t.hour + (0.5 if t.minute else 0.0)
        return v / 2 if self.half else v


NOW = datetime(2026, 11, 1, 10, 52)


class ThePrices(unittest.TestCase):

    def _run(self, disc, scheds):
        with mock.patch.object(engine, "_kraken_discovery", disc), \
             mock.patch.object(engine, "_kraken_rate_schedules", scheds):
            return engine.agile_prices(now=NOW)

    def test_import_and_export_back_24h_to_what_is_published(self):
        out = self._run({"import": {"tariff_code": AGILE}, "export": {"tariff_code": OUTGOING}},
                        {"import": _Sched("2026-11-01T23:00:00"), "export": _Sched("2026-11-01T23:00:00", True)})
        imp, exp = out["import"]["prices"], out["export"]["prices"]
        self.assertEqual(imp[0], ["2026-10-31T10:30:00", 20.5])       # the half-hour 24 h back
        self.assertEqual(imp[-1][0], "2026-11-01T22:30:00")           # the last published
        self.assertEqual(len(imp), 73)                                 # 31 Oct 10:30 to 1 Nov 22:30
        self.assertEqual(exp[0][1], 10.25)
        self.assertEqual(out["import"]["tariff"], AGILE)

    def test_not_a_dynamic_tariff(self):
        out = self._run({"import": {"tariff_code": "E-1R-IOG-SMB-FIX-12M-26-03-17-B"}},
                        {"import": _Sched("2026-11-02T00:00:00")})
        self.assertEqual(out, {})

    def test_export_alone(self):
        out = self._run({"import": {"tariff_code": "E-1R-VAR-22-11-01-C"}, "export": {"tariff_code": OUTGOING}},
                        {"import": _Sched("2026-11-02T00:00:00"), "export": _Sched("2026-11-02T00:00:00", True)})
        self.assertEqual(list(out), ["export"])

    def test_no_schedule_no_prices(self):
        self.assertEqual(self._run({"import": {"tariff_code": AGILE}}, {}), {})


class TheRouteAndPage(unittest.TestCase):

    def test_the_route_asks_the_engine(self):
        i = SERVER.index("def api_agile_prices(")
        self.assertIn("_eng.agile_prices()", SERVER[i:i + 800])

    def test_the_card_only_on_a_dynamic_tariff(self):
        self.assertIn("{% if has_agile %}", PAGE)
        self.assertIn('id="agile-card-wrap"', PAGE)
        i = SERVER.index("has_agile = any(_tp_ag.is_dynamic_code(")
        self.assertLess(i, SERVER.index("has_agile=has_agile,"))

    def test_its_size_is_the_history_cards(self):
        i = PAGE.index('id="agile-card-wrap"')
        self.assertIn('class="card power-history-card"', PAGE[i - 60:i])
        self.assertIn('<div class="power-history-wrap">\n      <canvas id="agile-canvas">', PAGE)


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheHelpers(unittest.TestCase):

    def _run(self, expr):
        names = ("_agileMs", "_agileColor", "_agileAt", "_agileCheapest")
        def fn(n):
            i = PAGE.index("function " + n + "(")
            line = PAGE[i:PAGE.index("\n", i)]
            if line.count("{") == line.count("}"):              # a one-line function
                return line + "\n"
            return PAGE[i:PAGE.index("\n}\n", i) + 2]
        js = "\n".join(fn(n) for n in names) + "\nconsole.log(JSON.stringify(" + expr + "));"
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    PRICES = [["2026-11-01T10:00:00", 20], ["2026-11-01T10:30:00", 18], ["2026-11-01T11:00:00", 5],
              ["2026-11-01T11:30:00", 7], ["2026-11-01T12:00:00", 4], ["2026-11-01T13:00:00", 1]]

    def test_the_price_now(self):
        self.assertEqual(self._run("_agileAt(%s, _agileMs('2026-11-01T10:52:00'))" % json.dumps(self.PRICES)), 18)

    def test_the_cheapest_hour_still_to_come_two_half_hours_together(self):
        # 11:00+11:30 is 6p; 11:30+12:00 is 5.5p; 12:00 and 13:00 are not together
        out = self._run("_agileCheapest(%s, _agileMs('2026-11-01T10:52:00'))" % json.dumps(self.PRICES))
        self.assertEqual(out["avg"], 5.5)

    def test_a_plunge_is_blue(self):
        self.assertEqual(self._run("_agileColor(-2, -2, 30)"), "#3b82f6")
        self.assertEqual(self._run("_agileColor(0, 0, 30)"), "hsl(130, 65%, 48%)")


if __name__ == "__main__":
    unittest.main()
