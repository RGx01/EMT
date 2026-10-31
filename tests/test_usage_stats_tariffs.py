"""
Usage Stats' tariff overlay (31 Oct 2026): the account's tariffs along the Monthly bars, by date.

Import agreements in a band at the top of the chart, above the axis; export at the bottom, below it;
a short tick at each change of tariff, placed by its date within its month or billing period; hover
a band for the tariff's name, code, dates and length. Offered where a bar is one stretch of time —
Monthly by Date, and since a second change the same day Daily and HH — and only where agreements are
stored. On HH a day is inside one agreement (they change at local midnight), so one band spans it.

What they pin:

- **The names** (`tariff_names.tariff_name`), made offline from the code's product part: the family,
  the more specific first, then " fixed" for a fixed-term product; an unknown product as its product
  part, never a guess.
- **The route** (`GET /api/tariff-agreements`): the stored agreements of the stamped account, local
  dates, a blip that ends where it starts left out, and no MPAN or account in the answer.
- **The page**: the checkbox only in Monthly by Date with agreements; each bar's dates
  (`bucketRanges`) so a date lands across its own bar; the bands' headroom; the hover over the band
  in place of the bars' tooltip.

Checked in the local preview on 31 Oct 2026, on a scratch copy of staging (seven import agreements,
two export, one blip): the bands in the right months, the ticks at the changes, the hover's dates
the agreements' local days (21 Sep 2023 – 11 Jun 2024 · 8.7 months), Billing and Calendar both, dark
and light; no box in Yearly or Monthly by Month. Daily and HH: the band split at 26 Aug's left edge
in the period from 3 Aug; HH on 26 Aug one band each way across the day, the running total's axes
still level (every tick on the same pixel).

Discriminating: every test fails on its parent (the module, route and overlay on #352's tree; Daily
and HH on #353's).
"""
import json
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(__file__))
import tariff_names as TN                                          # noqa: E402

PAGE = open(os.path.join(_ROOT, "web", "templates", "charts.html"), encoding="utf-8").read()


def _fn(name):
    i = PAGE.index("function " + name + "(")
    return PAGE[i:PAGE.index("\n}\n", i) + 2]


class TheNames(unittest.TestCase):

    def test_the_families(self):
        for code, name in (("E-1R-VAR-22-11-01-A", "Flexible"),
                           ("E-1R-INTELLI-VAR-22-10-14-A", "Intelligent Go"),
                           ("E-1R-GO-VAR-22-10-14-A", "Go"),
                           ("E-1R-AGILE-24-10-01-A", "Agile"),
                           ("E-1R-COSY-22-12-08-A", "Cosy"),
                           ("E-1R-SILVER-FLEX-22-11-25-A", "Tracker"),
                           ("E-1R-OUTGOING-VAR-24-10-26-A", "Outgoing")):
            self.assertEqual(TN.tariff_name(code), name, code)

    def test_the_more_specific_first(self):
        self.assertEqual(TN.tariff_name("E-1R-AGILE-OUTGOING-19-05-13-A"), "Agile Outgoing")
        self.assertEqual(TN.tariff_name("E-1R-INTELLI-FLUX-IMPORT-23-07-14-A"), "Intelligent Flux")
        self.assertEqual(TN.tariff_name("E-1R-IOG-SMB-FIX-12M-26-03-17-A"), "Intelligent Go (SMB) fixed")

    def test_fixed_terms(self):
        self.assertEqual(TN.tariff_name("E-1R-INTELLI-FIX-12M-26-03-17-A"), "Intelligent Go fixed")
        self.assertEqual(TN.tariff_name("E-1R-OUTGOING-FIX-12M-19-05-13-A"), "Outgoing fixed")
        self.assertEqual(TN.tariff_name("E-1R-OE-FIX-12M-24-09-20-A"), "Fixed")

    def test_an_unknown_product_is_shown_not_guessed(self):
        self.assertEqual(TN.tariff_name("E-1R-WEIRD-1-A"), "WEIRD-1")
        self.assertEqual(TN.tariff_name("not-a-code"), "not-a-code")


class TheRoute(unittest.TestCase):

    ROWS = [
        {"mpan": "2600000000001", "direction": "import", "tariff_code": "E-1R-GO-VAR-22-10-14-A",
         "product_code": None, "valid_from": "2023-09-20T23:00:00", "valid_to": "2024-06-11T23:00:00",
         "fetched_at": "x"},
        {"mpan": "2600000000002", "direction": "export", "tariff_code": "E-1R-VAR-22-11-01-A",
         "product_code": None, "valid_from": "2024-07-13T23:00:00", "valid_to": "2024-07-13T23:00:00",
         "fetched_at": "x"},
        {"mpan": "2600000000002", "direction": "export", "tariff_code": "E-1R-OUTGOING-VAR-24-10-26-A",
         "product_code": None, "valid_from": "2025-07-13T23:00:00", "valid_to": None, "fetched_at": "x"},
    ]

    def _get(self):
        from tests.test_server import make_client
        client = make_client()
        eng, srv = sys.modules["engine"], sys.modules["server"]
        asked = []

        class _Store:
            def get_tariff_agreements(_s, account):
                asked.append(account)
                return self.ROWS
        with mock.patch.object(eng, "get_db_account", lambda: "A-0000ABC7", create=True), \
             mock.patch.object(srv.store_access, "store", lambda: _Store()), \
             mock.patch.object(srv, "_period_block_minutes_tz", lambda st, f: (30, "Europe/London")):
            body = client.get("/api/tariff-agreements").data
        return json.loads(body), body.decode(), asked

    def test_the_stamped_accounts_agreements_in_local_dates(self):
        d, _, asked = self._get()
        self.assertEqual(asked, ["A-0000ABC7"])
        self.assertEqual(d["agreements"][0], {"direction": "import", "tariff_code": "E-1R-GO-VAR-22-10-14-A",
                                              "name": "Go", "from": "2023-09-21", "to": "2024-06-12"})
        self.assertEqual(d["agreements"][-1]["to"], None)

    def test_a_blip_is_left_out(self):
        d, _, _ = self._get()
        self.assertEqual([a["tariff_code"] for a in d["agreements"] if a["direction"] == "export"],
                         ["E-1R-OUTGOING-VAR-24-10-26-A"])

    def test_no_mpan_or_account_in_the_answer(self):
        _, raw, _ = self._get()
        for ident in ("2600000000001", "2600000000002", "A-0000ABC7", "mpan"):
            self.assertNotIn(ident, raw)


class ThePage(unittest.TestCase):

    def test_where_a_bar_is_one_stretch_of_time_with_agreements(self):
        avail = _fn("barTariffsAvailable")
        self.assertIn("var view = (barPeriod === 'monthly' && barMonthOrder === 'date') || barPeriod === 'daily' || barPeriod === 'hh';",
                      avail)
        self.assertIn("return view && !!barTariffs && barTariffs.length > 0;", avail)
        self.assertIn("if (_tw) _tw.style.display = barTariffsAvailable() ? 'flex' : 'none';", _fn("barRender"))

    def test_each_bar_carries_its_dates(self):
        data = _fn("barGetDataForPeriod")
        self.assertIn("bucketRanges: keys.map(function(bk){ return { from: bk, to: barAddMonth(bk) }; }),", data)
        self.assertIn("return { from: from, to: barAddMonth(from) };", data)
        self.assertIn("return { from: from, to: barAddDay(from) };", data)                        # Daily
        self.assertIn("bucketRanges: rows.map(function(){ return { from: barHHDate, to: _hhTo }; })", data)   # HH

    def test_hh_keeps_its_two_zeros_level_with_the_bands(self):
        """HH's bars and running total share gridlines (#349); the bands' room goes into that choice,
        not after it, or the two zeros part."""
        align = _fn("barAlignZero")
        self.assertIn("var rb = barTariffRoom(pos, neg, lanes), rl = barTariffRoom(lmax, lmin, lanes);", align)
        self.assertLess(align.index("barTariffRoom("), align.index("var axes = [[pos * head"))
        render = _fn("barRender")
        self.assertIn("var axes = netAxis ? barAlignZero(datasets, tariffLanes) : null;", render)
        self.assertIn("afterDataLimits: tariffLanes && !axes ? function(sc)", render)

    def test_a_date_lands_across_its_own_bar(self):
        x = _fn("barTariffX")
        self.assertIn("if (iso < ranges[i].from) return left(i);", x)
        self.assertIn("return left(i) + f * w;", x)

    def test_import_above_export_below(self):
        segs = _fn("barTariffSegments")
        self.assertIn("var ys = { imp: ca.top + 2, exp: ca.bottom - BAR_TARIFF_LANE - 2 };", segs)
        room = _fn("barTariffRoom")
        self.assertIn("return [max + (lanes && lanes.imp ? span * 0.12 : 0), min - (lanes && lanes.exp ? span * 0.12 : 0)];", room)
        self.assertIn("var r = barTariffRoom(sc.max, sc.min, lanes);", _fn("barTariffHeadroom"))

    def test_the_bands_hover_in_place_of_the_bars(self):
        plug = _fn("barTariffPlugin")
        self.assertIn("barTariffShow(hit.a, e.native);", plug)
        self.assertIn("return false;", plug)

    def test_the_hover_names_its_dates(self):
        self.assertIn("return barFmtDate(a.from) + ' \\u2013 ' + barFmtDate(end) + ' \\u00b7 ' + len;", _fn("barTariffWhen"))

    def test_the_choice_is_kept(self):
        self.assertIn("tariffs:  !!(document.getElementById('bar-tariff-cb') || {}).checked,", _fn("barSaveState"))
        self.assertIn("if (tc) tc.checked = st.tariffs === true;", _fn("barLoadState"))


if __name__ == "__main__":
    unittest.main()
