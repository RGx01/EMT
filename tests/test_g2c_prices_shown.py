"""
G2c (tariff prices design §5, 31 Oct 2026): the tariff overlay shows each agreement's prices.

What they pin:

- **The display periods** (`tariff_prices.for_display`): local dates, `to` exclusive (a period
  ending at 23:30 local is in force that day, one ending at midnight is not); inc VAT from EMT's
  calendar; a period across a VAT change shown as two, so the change has its tick; `dynamic` no
  value.
- **The route** (`GET /api/tariff-agreements`): each agreement carries its own price periods, by
  tariff and agreement start, and still no MPAN or account.
- **The page:** a minor tick at each price change inside an agreement, one on an agreement tick or
  another minor tick drawn once; the hover shows the prices of the tick period under the pointer,
  from the tick before it to the one after.

Checked in the local preview on 31 Oct 2026, a scratch copy of staging with invented prices (no
credentials there): the hover in Nov 2024 read that tick period (1 Oct 2024 – 31 Mar 2025) and its
off-peak, peak and standing charge inc VAT; a fixed agreement's standing charge split at 1 Oct 2026
by VAT alone; the minor ticks drawn at the changes.

Discriminating: every test fails on the parent (for_display, the route's prices and the page's
ticks and hover are new).
"""
import json
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(__file__))

PAGE = open(os.path.join(_ROOT, "web", "templates", "charts.html"), encoding="utf-8").read()
VAT = [("1997-09-01", 0.05), ("2026-10-01", 0.0)]
GO = "E-1R-INTELLI-VAR-22-10-14-A"


def _fn(name):
    i = PAGE.index("function " + name + "(")
    return PAGE[i:PAGE.index("\n}\n", i) + 2]


def _row(kind, band, window, v, vf, vt):
    return {"kind": kind, "band": band, "window": window, "value_exc": v, "valid_from": vf, "valid_to": vt}


class TheDisplayPeriods(unittest.TestCase):

    def setUp(self):
        import tariff_prices
        self.fd = lambda rows: tariff_prices.for_display(rows, vat_dates=VAT, tz_name="Europe/London")

    def test_local_dates_to_exclusive(self):
        got = self.fd([_row("standing", "", None, 40.0, "2024-06-11T23:00:00", "2024-09-30T23:00:00"),
                       _row("unit", "peak", "05:30-23:30", 25.0, "2024-06-12T04:30:00", "2024-09-30T22:30:00")])
        self.assertEqual([(p["kind"], p["from"], p["to"]) for p in got],
                         [("standing", "2024-06-12", "2024-10-01"), ("unit", "2024-06-12", "2024-10-01")])

    def test_inc_vat_from_the_calendar(self):
        (p,) = self.fd([_row("standing", "", None, 40.0, "2024-06-11T23:00:00", "2024-09-30T23:00:00")])
        self.assertEqual((p["exc"], p["inc"]), (40.0, 42.0))

    def test_a_vat_change_splits_a_period(self):
        got = self.fd([_row("standing", "", None, 48.0, "2026-08-25T23:00:00", None)])
        self.assertEqual([(p["from"], p["to"], p["inc"]) for p in got],
                         [("2026-08-26", "2026-10-01", 50.4), ("2026-10-01", None, 48.0)])

    def test_export_has_no_vat_and_no_vat_split(self):
        """No VAT on what Octopus pays for export (the user, 31 Oct 2026): inc is the published price,
        and a VAT change neither moves it nor ticks."""
        import tariff_prices
        got = tariff_prices.for_display([_row("unit", "", None, 15.0, "2025-07-13T23:00:00", None)],
                                        vat_dates=VAT, tz_name="Europe/London", vat=False)
        self.assertEqual([(p["from"], p["to"], p["inc"]) for p in got], [("2025-07-14", None, 15.0)])

    def test_dynamic_has_no_value(self):
        (p,) = self.fd([_row("unit", "dynamic", None, None, "2024-01-01T00:00:00", "2024-06-01T00:00:00")])
        self.assertEqual((p["band"], p["exc"], p["inc"]), ("dynamic", None, None))


class TheRoute(unittest.TestCase):

    def test_each_agreement_carries_its_own_prices_and_no_identifiers(self):
        from tests.test_server import make_client
        client = make_client()
        eng, srv = sys.modules["engine"], sys.modules["server"]

        class _Store:
            def get_tariff_agreements(_s, account):
                return [{"mpan": "2600000000001", "direction": "import", "tariff_code": GO,
                         "product_code": None, "valid_from": "2024-06-11T23:00:00",
                         "valid_to": "2025-06-30T23:00:00", "fetched_at": "x"},
                        {"mpan": "2600000000001", "direction": "import", "tariff_code": GO,
                         "product_code": None, "valid_from": "2025-06-30T23:00:00", "valid_to": None,
                         "fetched_at": "x"}]

            def get_tariff_prices(_s):
                return [dict(_row("standing", "", None, 40.0, "2024-06-11T23:00:00", "2025-06-30T23:00:00"),
                             tariff_code=GO, agreement_from="2024-06-11T23:00:00")]

            def get_vat_calendar(_s):
                return []
        with mock.patch.object(eng, "get_db_account", lambda: "A-0000ABC7", create=True), \
             mock.patch.object(srv.store_access, "store", lambda: _Store()), \
             mock.patch.object(srv, "_period_block_minutes_tz", lambda st, f: (30, "Europe/London")):
            raw = client.get("/api/tariff-agreements").data
        d = json.loads(raw)
        self.assertEqual([len(a["prices"]) for a in d["agreements"]], [1, 0])
        self.assertEqual(d["agreements"][0]["prices"][0]["inc"], 42.0)
        self.assertIn('vat=r["direction"] != "export")', open(os.path.join(_ROOT, "web", "server.py"),
                                                              encoding="utf-8").read())
        for ident in ("2600000000001", "A-0000ABC7", "mpan"):
            self.assertNotIn(ident, raw.decode())


class ThePage(unittest.TestCase):

    def test_a_minor_tick_at_each_price_change_inside_an_agreement(self):
        starts = _fn("barTariffPriceStarts")
        self.assertIn("if (p.from > a.from) seen[p.from] = true;", starts)
        plug = _fn("barTariffPlugin")
        self.assertIn("barTariffPriceStarts(sg.a).forEach(function(iso) {", plug)
        self.assertIn("c.globalAlpha = 0.55; c.lineWidth = 1;", plug)
        # just outside the band: above import's, below export's (the user, 31 Oct 2026)
        self.assertIn("c.moveTo(x, exp ? sg.y1 + 1 : sg.y0 - 1);", plug)
        self.assertIn("c.lineTo(x, exp ? sg.y1 + 6 : sg.y0 - 6);", plug)
        self.assertIn("var ys = { imp: ca.top + 8, exp: ca.bottom - BAR_TARIFF_LANE - 8 };",
                      _fn("barTariffSegments"))
        self.assertIn("if (drawn.some(function(d) { return d.y0 === sg.y0 && Math.abs(d.x - x) < 2; })) return;", plug)

    def test_the_hover_shows_the_tick_period_under_the_pointer(self):
        self.assertIn("barTariffShow(hit.a, e.native, barTariffDateAt(chart, ranges, e.x));", _fn("barTariffPlugin"))
        at = _fn("barTariffPricesAt")
        self.assertIn("var from = starts.filter(function(d) { return d <= iso; }).pop() || a.from;", at)
        self.assertIn("var next = starts.filter(function(d) { return d > iso; })[0] || a.to;", at)
        self.assertIn("if (!latest[k] || p.from > latest[k].from) latest[k] = p;", at)   # one per band
        lines = _fn("barTariffPriceLines")
        for part in ("'Unit rates: half-hourly'", "'Standing charge ' + p2(p.inc) + '/day'"):
            self.assertIn(part, lines)

    def test_one_row_per_time_window_in_time_order(self):
        """The user, 31 Oct 2026: one row per time period, for Cosy's seven windows and Intelligent's
        two alike — no off-peak / peak names, no windows joined on a line."""
        lines = _fn("barTariffPriceLines")
        self.assertIn("return String(x.window || '').localeCompare(String(y.window || ''));", lines)
        self.assertIn("if (p.window) return p.window.replace('-', '\\u2013') + ' ' + p2(p.inc) + '/kWh';", lines)
        self.assertNotIn("'Off-peak '", lines)
        self.assertNotIn("parts.join(", lines)
        self.assertIn("(a.direction === 'export' ? ' (no VAT on export)' : ', inc VAT')", _fn("barTariffShow"))


if __name__ == "__main__":
    unittest.main()
