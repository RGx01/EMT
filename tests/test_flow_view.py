"""
Flow, Usage Stats' Sankey (Flow design, PR 2; 4 Nov 2026): grid → rate band → the house and each device,
export out of the house, and in £ the standing charge, for the span on screen.

`barFlowData` turns the buckets' summed rate brackets (`agg.br`, [kWh, £, g] per series and bracket,
PR 1) into nodes and flows, every metric at once. What it must hold:

- a band's inflow is what leaves it, so every node balances and the grid's total is the series' total
  (the Devices view's figures, checked in the local preview to the 0.001 kWh and 1p);
- a bracket under FLOW_OTHER_SHARE (2%) of the span's import in the metric shown is folded into Other;
- export leaves the house; the standing charge, in £ with the box ticked, is the grid's own flow;
- the car of an account with no devices (`ev_dispatch`) is "EV".

And the wiring: the plugin pinned to an exact version on jsdelivr, as the heatmap's; Flow in the view
picker and the remembered views; the HH rows carrying their brackets for Flow too (a first build drew
an empty HH, seen in the preview); and the chart drawn at once when the plugin is in, since the PDF
reads the canvas straight after barRender().

Checked in the local preview on the demo household, a month: the Sankey drawn, each device's and the
house's kWh and £ equal to the Devices view's, the meter's 645.576 kWh and the export's 20.075 kWh; a
hover's kWh × p/kWh its £; the standing charge's £14.50 in the head; HH and Yearly (Other on Yearly);
the PDF's canvas read at once.

Export by rate band, and a negative price drawn in £ (Flow export bands design, 4 Nov 2026, the user:
"can they handle plunge pricing? I think we need to rate band the export too"): the house's export goes
out through its own bands (Other under 2% of the export), and in £ a plunge's credit goes from whoever
used it to Plunge credit, a negative export price's cost to Paid to export — `credit` flows, £ only.
Those tests fail on the tree before them; test_GUARD_no_negative_price_no_credit_nodes passes on both.
Flow's period buttons named by the span they total (Daily, Monthly, Yearly for HH, Daily, Monthly; the
user, 4 Nov 2026), and the active button found by its period, not its words: those two fail on the tree
before them.

Every test fails on the parent tree, where none of it existed. TheEraChapters, TheSpans and the Span
wiring (4 Nov 2026, later; eras on their own and "now" for a future end the same day, from staging's data)
fail on the tree before them: Flow had Yearly, which totalled what Monthly
did, and no view had a span.
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


METERS = [{"id": "electricity_main", "label": "Direct", "color": "#111", "is_sub": False},
          {"id": "heat_pump", "label": "Heat Pump", "color": "#222", "is_sub": True}]


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheData(unittest.TestCase):

    def _run(self, agg_list, metric="kwh", standing=False, meters=METERS):
        js = ("var FLOW_OTHER_SHARE = 0.02, barBrWidth = 2.5, barCurrency = '£', barExportColor = '#ff7f0e';\n"
              "var barMetric = %s, barMeters = %s, barBrSeries = 'meter';\n"
              "function barIsDark() { return true; }\n"
              "function barIncStanding() { return %s; }\n"
              "function barStandingVal(agg) { return agg.standing || 0; }\n" % (
                  json.dumps(metric), json.dumps(meters), "true" if standing else "false") +
              "".join(_fn(n) for n in ("barBrPence", "barBrLabel", "barBrSign", "barBrColor", "barFlowIndex",
                                       "barFlowSeriesLabel", "barFlowSeriesColor", "barFlowPlain", "barFlowData")) +
              "\nconsole.log(JSON.stringify(barFlowData(%s)));" % json.dumps(agg_list))
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    @staticmethod
    def _flows(d):
        return {(f["from"], f["to"]): f["v"] for f in d["flows"]}

    # a month: the house and the heat pump in two bands (3: 7.5–10p, 10: 25–27.5p), and export
    AGG = [{"br": {"meter": {"3": [10.0, 0.8, 2000], "10": [5.0, 1.3, 1000]},
                   "electricity_main": {"3": [6.0, 0.48, 1200], "10": [2.0, 0.52, 400]},
                   "heat_pump": {"3": [4.0, 0.32, 800], "10": [3.0, 0.78, 600]},
                   "export": {"6": [3.0, 0.45, 600]}}, "standing": 0.5},
           {"br": {"heat_pump": {"10": [1.0, 0.26, 200]}}, "standing": 0.5}]

    def test_grid_band_device_and_every_node_balances(self):
        d = self._run(self.AGG, "cost")
        f = self._flows(d)
        self.assertEqual(f[("grid", "b:3")], [10.0, 0.8, 2000])
        self.assertAlmostEqual(f[("grid", "b:10")][0], 6.0)          # 2 + 3 + the second bucket's 1
        self.assertEqual(f[("b:3", "s:heat_pump")], [4.0, 0.32, 800])
        self.assertAlmostEqual(f[("b:10", "s:heat_pump")][0], 4.0)
        grid_out = sum(v[0] for (a, b), v in f.items() if a == "grid")
        into_series = sum(v[0] for (a, b), v in f.items() if b.startswith("s:"))
        self.assertAlmostEqual(grid_out, into_series)
        self.assertEqual([d["nodes"][k]["column"] for k in ("grid", "b:3", "s:heat_pump")], [0, 1, 2])
        self.assertEqual(d["nodes"]["s:electricity_main"]["label"], "Direct (house)")

    def test_export_leaves_the_house_by_its_rate_band(self):
        # export bands design: the house's export through its own bands, in export's colours
        d = self._run(self.AGG, "cost")
        self.assertEqual(self._flows(d)[("s:electricity_main", "x:6")], [3.0, 0.45, 600])
        self.assertEqual(d["nodes"]["x:6"]["column"], 3)
        self.assertEqual(d["nodes"]["x:6"]["label"], "Export 15–17.5p")
        self.assertNotIn("export", d["nodes"])
        self.assertEqual(d["totals"]["exp"], [3.0, 0.45, 600])

    def test_the_standing_charge_in_pounds_with_the_box(self):
        self.assertNotIn(("grid", "standing"), self._flows(self._run(self.AGG, "cost")))
        f = self._flows(self._run(self.AGG, "cost", standing=True))
        self.assertEqual(f[("grid", "standing")], [0, 1.0, 0])

    def test_a_small_band_goes_into_other(self):
        agg = [{"br": {"electricity_main": {"3": [100.0, 8.0, 0], "10": [0.5, 0.135, 0], "-1": [0.5, -0.01, 0]}}}]
        d = self._run(agg, "cost")
        self.assertEqual(d["other"], [10])                              # 13.5p of £8.135: under 2%
        self.assertIn("b:other", d["nodes"])
        self.assertNotIn("b:10", d["nodes"])
        self.assertAlmostEqual(self._flows(d)[("b:other", "s:electricity_main")][0], 0.5)

    def test_bands_and_other_are_pounds_only(self):
        # the user, 5 Nov 2026: a price says nothing about kWh or grams — no bands there, so no Other
        agg = [{"br": {"electricity_main": {"1": [97.0, 2.0, 0], "10": [1.0, 0.27, 0]}}}]
        for metric in ("kwh", "co2"):
            d = self._run(agg, metric)
            self.assertEqual(d["other"], [])
            self.assertFalse([k for k in d["nodes"] if k.startswith(("b:", "x:"))], metric)
        self.assertIn("b:10", self._run(agg, "cost")["nodes"])

    def test_export_bands_and_their_other(self):
        # Agile Outgoing: a month across three export bands, one of them under 2% of the credit
        agg = [{"br": {"electricity_main": {"3": [10.0, 0.8, 0]},
                       "export": {"4": [60.0, 6.6, 0], "6": [39.0, 6.0, 0], "9": [1.0, 0.20, 0]}}}]
        d = self._run(agg, "cost")
        self.assertEqual(d["xother"], [9])
        self.assertEqual([k for k in d["nodes"] if k.startswith("x:")], ["x:4", "x:6", "x:other"])
        self.assertEqual(d["nodes"]["x:other"]["label"], "Other export")
        self.assertAlmostEqual(self._flows(d)[("s:electricity_main", "x:other")][0], 1.0)
        # the export's colours, not the import's: blue to purple, the best-paid the deepest
        self.assertTrue(d["nodes"]["x:4"]["color"].startswith("hsl(205,"))
        self.assertTrue(d["nodes"]["b:3"]["color"].startswith("hsl(130,"))

    def test_a_plunge_s_credit_turned_round(self):
        # an Agile plunge at -2.5 to 0p: the house used 4 kWh and earned 4p, the heat pump 2 kWh and 2p
        agg = [{"br": {"meter": {"-1": [6.0, -0.06, 900], "3": [10.0, 0.8, 1500]},
                       "electricity_main": {"-1": [4.0, -0.04, 600], "3": [10.0, 0.8, 1500]},
                       "heat_pump": {"-1": [2.0, -0.02, 300]}}}]
        d = self._run(agg, "cost")
        f = self._flows(d)
        self.assertEqual(f[("s:electricity_main", "plunge")], [4.0, 0.04, 600])
        self.assertEqual(f[("s:heat_pump", "plunge")], [2.0, 0.02, 300])
        credits = {(x["from"], x["to"]) for x in d["flows"] if x.get("credit")}
        self.assertEqual(credits, {("s:electricity_main", "plunge"), ("s:heat_pump", "plunge")})
        self.assertEqual(d["nodes"]["plunge"]["label"], "Plunge credit")
        self.assertEqual(d["nodes"]["plunge"]["column"], 3)
        self.assertAlmostEqual(d["totals"]["credit"], -0.06)
        # the band's own flows keep their negative £, so its rows in the table say what it earned
        self.assertAlmostEqual(f[("b:-1", "s:heat_pump")][1], -0.02)

    def test_paying_to_export_is_a_cost_out_of_the_house(self):
        agg = [{"br": {"electricity_main": {"3": [10.0, 0.8, 0]},
                       "export": {"-1": [2.0, -0.03, 0], "4": [5.0, 0.55, 0]}}}]
        d = self._run(agg, "cost")
        f = self._flows(d)
        self.assertEqual(f[("s:electricity_main", "paid_export")], [2.0, 0.03, 0])
        self.assertEqual(d["nodes"]["paid_export"]["label"], "Paid to export")
        self.assertTrue(d["nodes"]["paid_export"]["color"].startswith("hsl(0,"))
        self.assertAlmostEqual(d["totals"]["paid"], -0.03)
        self.assertAlmostEqual(d["totals"]["exp"][1], 0.52)

    def test_GUARD_no_negative_price_no_credit_nodes(self):
        d = self._run(self.AGG, "cost")
        self.assertNotIn("plunge", d["nodes"])
        self.assertNotIn("paid_export", d["nodes"])
        self.assertFalse(any(x.get("credit") for x in d["flows"]))

    # ── kWh and CO₂ without the bands (5 Nov 2026, the user) ──

    def test_in_kwh_the_grid_goes_straight_to_each_destination(self):
        d = self._run(self.AGG, "kwh", standing=True)
        f = self._flows(d)
        # every bracket summed: the heat pump's 4 + 3 + the second bucket's 1, its £ and grams with them
        self.assertEqual(f[("grid", "s:heat_pump")], [8.0, 1.36, 1600])
        self.assertEqual(f[("grid", "s:electricity_main")], [8.0, 1.0, 1600])
        self.assertEqual(f[("s:electricity_main", "export")], [3.0, 0.45, 600])
        self.assertEqual(set(f), {("grid", "s:heat_pump"), ("grid", "s:electricity_main"),
                                  ("s:electricity_main", "export")})     # no band, no standing charge
        self.assertEqual([d["nodes"][k]["column"] for k in ("grid", "s:heat_pump", "export")], [0, 1, 2])
        self.assertEqual([round(x, 6) for x in d["totals"]["imp"]], [16.0, 2.36, 3200])

    def test_in_co2_the_export_is_kept_but_not_drawn(self):
        d = self._run(self.AGG, "co2")
        x = [x for x in d["flows"] if x["to"] == "export"]
        self.assertEqual(len(x), 1)
        self.assertTrue(x[0].get("hide"))
        self.assertEqual(d["totals"]["exp"], [3.0, 0.45, 600])
        self.assertFalse(any(x.get("hide") for x in self._run(self.AGG, "kwh")["flows"]))

    def test_no_credit_flows_outside_pounds(self):
        agg = [{"br": {"electricity_main": {"-1": [4.0, -0.04, 600], "3": [10.0, 0.8, 1500]},
                       "export": {"-1": [2.0, -0.03, 0]}}}]
        for metric in ("kwh", "co2"):
            d = self._run(agg, metric)
            self.assertFalse(any(x.get("credit") for x in d["flows"]), metric)
            self.assertNotIn("plunge", d["nodes"])
            self.assertEqual(self._flows(d)[("grid", "s:electricity_main")], [14.0, 0.76, 2100])

    def test_export_with_no_house_import_still_has_its_house(self):
        d = self._run([{"br": {"heat_pump": {"3": [1.0, 0.08, 0]}, "export": {"6": [2.0, 0.3, 0]}}}])
        self.assertIn("s:electricity_main", d["nodes"])
        self.assertEqual(self._flows(d)[("s:electricity_main", "export")], [2.0, 0.3, 0])

    def test_the_car_without_a_device_is_ev(self):
        agg = [{"br": {"electricity_main": {"3": [1.0, 0.08, 0]}, "ev_dispatch": {"2": [5.0, 0.35, 0]}}}]
        d = self._run(agg, meters=METERS[:1])
        self.assertEqual(d["nodes"]["s:ev_dispatch"]["label"], "EV")


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheEraChapters(unittest.TestCase):
    """4 Nov 2026, the user: in Flow Yearly totalled what Monthly did (the years ticked), so Flow has Era
    there — a chapter of the home, from an era's start to the next era's start, of any kind."""

    def _run(self, eras, days):
        rows = [{"year": int(d[:4]), "month": int(d[5:7]), "day": int(d[8:])} for d in days]
        kinds = PAGE[PAGE.index("var ERA_KINDS = {"):PAGE.index("\n};\n", PAGE.index("var ERA_KINDS = {")) + 3]
        js = (kinds + "var barEras = %s, barData = %s;\n" % (json.dumps(eras), json.dumps(rows)) +
              "".join(_fn(n) for n in ("barAddDay", "barFlowYmd", "barFlowChapters")) +
              "\nconsole.log(JSON.stringify(barFlowChapters().map(function (c) {"
              " return [c.id, c.label, c.from, c.to, !!c.latest]; })));")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    DAYS = ["2024-06-01", "2025-01-01", "2026-10-31"]

    def test_chapters_from_start_to_next_start(self):
        eras = [{"kind": "ev", "label": "First EV", "start": "2024-09-14"},
                {"kind": "solar", "label": "Solar", "start": "2025-03-08"}]
        self.assertEqual(self._run(eras, self.DAYS), [
            ["before", "Before the first era", "2024-06-01", "2024-09-14", False],
            ["era:2024-09-14", "\U0001f697 First EV", "2024-09-14", "2025-03-08", False],
            ["era:2025-03-08", "\u2600\ufe0f Solar", "2025-03-08", "2026-11-01", True]])

    def test_eras_on_one_day_are_one_chapter(self):
        eras = [{"kind": "solar", "label": "Solar", "start": "2025-03-08"},
                {"kind": "battery", "label": "Battery", "start": "2025-03-08", "end": "2025-06-01"}]
        out = self._run(eras, self.DAYS)
        self.assertEqual([c[0] for c in out], ["before", "era:2025-03-08"])
        self.assertEqual(out[1][1], "\u2600\ufe0f Solar + \U0001f50b Battery")
        self.assertEqual(out[1][3], "2026-11-01", "to the next era's start, not its own end")

    def test_an_era_before_the_data_starts_with_it(self):
        out = self._run([{"kind": "other", "label": "Moved in", "start": "2020-01-01"}], self.DAYS)
        self.assertEqual(out, [["era:2020-01-01", "\u2022 Moved in", "2024-06-01", "2026-11-01", True]])

    def test_one_after_the_data_is_not_a_chapter(self):
        out = self._run([{"kind": "ev", "label": "EV", "start": "2024-09-14"},
                         {"kind": "other", "label": "Planned", "start": "2027-01-01"}], self.DAYS)
        self.assertEqual([c[0] for c in out], ["before", "era:2024-09-14"])
        self.assertTrue(out[-1][4])

    def test_no_eras_no_chapters(self):
        self.assertEqual(self._run([], self.DAYS), [])


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheSpans(unittest.TestCase):
    """4 Nov 2026, the user: the spans are the eras' chapters and the tariffs, with their price changes;
    in Devices, Rate brackets and Flow. A span's bars: a day up to 62 days, else a month."""

    DAYS = ["2024-06-01", "2024-12-15", "2025-01-10", "2025-02-20", "2025-06-30"]

    def _js(self, tail, eras=(), tariffs=(), flow=False):
        rows = [{"year": int(d[:4]), "month": int(d[5:7]), "day": int(d[8:]), "imp_kwh": 1.0} for d in self.DAYS]
        kinds = PAGE[PAGE.index("var ERA_KINDS = {"):PAGE.index("\n};\n", PAGE.index("var ERA_KINDS = {")) + 3]
        js = (kinds + "var BAR_ACC_MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];\n"
              "var barEras = %s, barTariffs = %s, barData = %s, FLOW_SPAN_DAILY_MAX = 62;\n" % (
                  json.dumps(list(eras)), json.dumps(list(tariffs)), json.dumps(rows)) +
              "function barFlow() { return %s; }\n" % ("true" if flow else "false") +
              "function barAggRows(f) { return {imp_kwh: barData.filter(f).reduce(function (t, r) { return t + r.imp_kwh; }, 0)}; }\n" +
              "".join(_fn(n) for n in ("barAddDay", "barAddMonth", "barDayNo", "barFlowYmd", "barFlowChapters",
                                       "barTariffPriceStarts", "barTariffChangeDays", "barTariffVatOnly", "barAccDateLabel",
                                       "barSpans", "barSpanData")) + tail)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    @staticmethod
    def _p(frm):
        return {"kind": "unit", "band": "flat", "from": frm, "inc": 20, "exc": 19}

    def _tariffs(self):
        return [{"direction": "import", "name": "Flexible", "from": "2024-06-01", "to": "2025-01-01",
                 "prices": [self._p("2024-06-01"), self._p("2024-10-01")]},
                {"direction": "import", "name": "Go", "from": "2025-01-01", "to": None, "prices": [self._p("2025-01-01")]},
                {"direction": "export", "name": "Outgoing", "from": "2024-06-01", "to": None, "prices": []}]

    def test_tariffs_with_their_price_changes(self):
        out = self._js("\nconsole.log(JSON.stringify(barSpans().map(function (o) {"
                       " return [o.group, !!o.sub, o.label, o.from, o.to, !!o.latest]; })));", tariffs=self._tariffs())
        self.assertEqual(out, [
            ["Tariffs", False, "Tariff: Go", "2025-01-01", "2025-07-01", True],
            ["Tariffs", False, "Tariff: Flexible", "2024-06-01", "2025-01-01", False],
            ["Tariffs", True, "Flexible \u2014 price change 1 Oct 2024", "2024-10-01", "2025-01-01", False],
            ["Tariffs", True, "Flexible \u2014 from its start", "2024-06-01", "2024-10-01", False]])

    def test_chapters_then_eras_then_tariffs(self):
        out = self._js("\nconsole.log(JSON.stringify(barSpans().map(function (o) { return o.group; })));",
                       eras=[{"kind": "solar", "label": "Solar", "start": "2025-01-01"}], tariffs=self._tariffs())
        self.assertEqual(out, ["Chapters", "Chapters", "Eras", "Tariffs", "Tariffs", "Tariffs", "Tariffs"])

    def test_an_era_on_its_own_runs_to_its_own_end(self):
        # 4 Nov 2026, the user: the solar era without the chapter's cut at the battery's change; an era's
        # end is exclusive (a replaced battery ends the day the next starts)
        eras = [{"id": "s", "kind": "solar", "label": "Solar", "start": "2024-07-14"},
                {"id": "b1", "kind": "battery", "label": "Battery", "start": "2024-07-14", "end": "2025-02-20"},
                {"id": "b2", "kind": "battery", "label": "Battery", "start": "2025-02-20"}]
        out = self._js("\nconsole.log(JSON.stringify(barSpans().filter(function (o) { return o.group === 'Eras'; })"
                       ".map(function (o) { return [o.id, o.from, o.to, !!o.latest]; })));", eras=eras)
        self.assertEqual(out, [["own:b2", "2025-02-20", "2025-07-01", True],
                               ["own:s", "2024-07-14", "2025-07-01", True],
                               ["own:b1", "2024-07-14", "2025-02-20", False]])

    def test_a_span_reaching_the_last_day_is_now(self):
        # a running fixed-term tariff has its end in the future (staging's IOG-SMB fixed, 4 Nov 2026)
        t = [{"direction": "import", "name": "Fixed", "from": "2025-01-01", "to": "2027-03-18", "prices": []}]
        out = self._js("\nconsole.log(JSON.stringify(barSpans().map(function (o) { return [o.to, !!o.latest]; })));",
                       tariffs=t)
        self.assertEqual(out, [["2025-07-01", True]])

    def test_a_short_span_a_bar_a_day(self):
        out = self._js("\nvar d = barSpanData({from: '2025-01-01', to: '2025-03-01'});"
                       "\nconsole.log(JSON.stringify([d.labels, d.aggList.map(function (a) { return a.imp_kwh; })]));")
        self.assertEqual(out, [["10 Jan", "20 Feb"], [1.0, 1.0]])

    def test_a_long_span_a_bar_a_month_clipped_to_its_dates(self):
        out = self._js("\nvar d = barSpanData({from: '2024-06-15', to: '2025-07-01'});"
                       "\nconsole.log(JSON.stringify([d.labels, d.bucketRanges[0], d.aggList.map(function (a) { return a.imp_kwh; })]));")
        labels, first, kwh = out
        self.assertEqual(labels, ["Dec 2024", "Jan 2025", "Feb 2025", "Jun 2025"])     # 1 Jun 2024 is before it
        self.assertEqual(first, {"from": "2024-12-01", "to": "2025-01-01"})
        self.assertEqual(sum(kwh), 4.0)

    def test_flow_one_bucket(self):
        out = self._js("\nvar d = barSpanData({from: '2024-06-01', to: '2025-07-01', label: 'All'});"
                       "\nconsole.log(JSON.stringify([d.labels, d.aggList[0].imp_kwh]));", flow=True)
        self.assertEqual(out, [["All"], 5.0])


class TheWiring(unittest.TestCase):

    def test_the_plugin_is_pinned(self):
        self.assertIn("var FLOW_PLUGIN = 'https://cdn.jsdelivr.net/npm/chartjs-chart-sankey@0.19.1/dist/"
                      "chartjs-chart-sankey.min.js';", PAGE)

    def test_flow_is_a_view(self):
        self.assertIn("onclick=\"barSetShow('flow',this)\"", PAGE)
        self.assertIn("['devices','brackets','flow','running'].indexOf(st.show)", PAGE)
        self.assertIn("if (barFlow()) { barLoadEras(); barLoadTariffs(); barFlowRender(pd.aggList); return; }", _fn("barRender"))
        self.assertIn("if (barFlow()) { barFlowBuildTable(tbl, aggList); return; }", _fn("barBuildTable"))

    def test_the_hh_rows_carry_their_brackets_for_flow(self):
        self.assertIn("if ((barBrackets() || barFlow()) && barBrHH) {", _fn("barGetDataForPeriod"))
        self.assertIn("if ((barBrackets() || barFlow()) && !barBrReady()) return;", _fn("barRender"))

    def test_drawn_at_once_when_the_plugin_is_in(self):
        f = _fn("barFlowRender")
        self.assertIn("if (window.Chart && Chart.registry.controllers.get('sankey')) { barFlowDraw(aggList); return; }", f)
        self.assertLess(f.index("barFlowDraw(aggList); return;"), f.index("barFlowLib().then("))

    def test_the_bands_are_imports_colours(self):
        self.assertIn("var ex = side ? side === 'export' : barBrSign() < 0;", _fn("barBrColor"))
        self.assertIn("barBrColor(n, ns, 'import')", _fn("barFlowData"))
        self.assertIn("barBrColor(n, xns, 'export')", _fn("barFlowData"))

    def test_a_credit_flow_is_drawn_in_pounds_only(self):
        self.assertIn("f.v[i] > 1e-9 && (!f.credit || i === 1)", _fn("barFlowDraw"))
        self.assertIn("if (f.credit || (Math.abs(f.v[0]) < 1e-9", _fn("barFlowBuildTable"))

    def test_the_head_says_what_the_net_holds(self):
        h = _fn("barFlowHead")
        self.assertIn("'<span>Import is net of ' + barFlowFmt(-t.credit, 1) + ' earned at negative prices: Plunge credit</span>'", h)
        self.assertIn("'<span>Export is net of ' + barFlowFmt(-t.paid, 1) + ' paid to export at negative prices</span>'", h)

    def test_span_in_the_bar_views_yearly_but_in_flow(self):
        self.assertIn("onclick=\"barSetPeriod('span',this)\" id=\"bar-span-btn\"", PAGE)
        f = _fn("barSetShow")
        self.assertIn("if (v === 'flow' && barPeriod === 'yearly') barPeriod = barSpansPending() || barSpans().length ? 'span' : 'monthly';", f)
        self.assertIn("else if (v === 'running' && barPeriod === 'span') barPeriod = 'yearly';", f)
        ui = _fn("barBrSyncUI")
        self.assertIn("vis('bar-yearly-btn', !fl);", ui)
        self.assertIn("vis('bar-span-btn', spanOk);", ui)
        load = _fn("barLoadState")
        self.assertIn("if (st.period === 'era') barPeriod = 'span';", load)
        self.assertIn("if (barPeriod === 'span' && barShow === 'running') barPeriod = 'yearly';", load)

    def test_a_remembered_span_waits_for_the_eras_and_tariffs(self):
        # a reload restored the period before the eras were read, and fell back to Monthly (the preview)
        self.assertIn("if (barPeriod === 'span' && !spanOk && !barSpansPending()) barPeriod = 'monthly';", _fn("barBrSyncUI"))
        self.assertIn("if ((barEras.length || barPeriod === 'span') && barInited && barData) barRender();", _fn("barLoadEras"))
        self.assertIn("if ((barTariffs.length || barPeriod === 'span') && barInited && barData) barRender();", _fn("barLoadTariffs"))

    def test_the_overlays_on_a_span(self):
        self.assertIn("|| barPeriod === 'span';", _fn("barTariffsAvailable"))
        self.assertIn("|| barPeriod === 'span';", _fn("barErasAvailable"))

    def test_flow_names_its_periods_by_what_it_totals(self):
        # 4 Nov 2026, the user: in Flow HH is a day, Daily a month, Monthly the years ticked
        self.assertIn("var FLOW_PERIOD_NAMES = [['hh', 'HH', 'Daily'], ['daily', 'Daily', 'Monthly'], "
                      "['monthly', 'Monthly', 'Yearly']];", PAGE)
        f = _fn("barBrSyncUI")
        self.assertIn("var b = document.getElementById('bar-' + p[0] + '-btn');", f)
        self.assertIn("b.textContent = fl ? p[2] : p[1];", f)
        for p in ("hh", "daily", "monthly"):
            self.assertIn("onclick=\"barSetPeriod('%s',this)\" id=\"bar-%s-btn\"" % (p, p), PAGE)

    def test_the_active_period_by_what_it_sets_not_its_words(self):
        # renamed in Flow, a button's words no longer name its period
        f = _fn("barSyncUI")
        self.assertIn("b.classList.toggle('active', (b.getAttribute('onclick') || '').indexOf(\"'\" + barPeriod + \"'\") !== -1);", f)
        self.assertNotIn("b.textContent.toLowerCase() === v", f)

    def test_export_says_where_emt_cannot_see(self):
        f = _fn("barFlowDraw")
        self.assertIn("if (r.to.indexOf('x:') === 0 || r.to === 'export') return 'Export is energy EMT did not meter coming in: solar or a battery.';", f)
        self.assertIn("if (r.to === 'plunge') return 'Paid to use ' + barFlowFmt(r._v[0], 0) + ' at negative prices.';", f)



class ThePlainDraw(unittest.TestCase):
    """kWh and CO₂ (5 Nov 2026): the hidden export not drawn, the head's CO₂ export what it displaced,
    the hover's g/kWh."""

    def test_a_hidden_flow_is_not_drawn(self):
        self.assertIn("(!f.credit || i === 1) && !f.hide; })", _fn("barFlowDraw"))

    def test_the_head_says_what_export_displaced(self):
        self.assertIn("(i === 2 ? 'Displaced by export' : 'Export')", _fn("barFlowHead"))

    def test_the_hover_gives_grams_a_kwh(self):
        self.assertIn("Math.round(v[2] / v[0]) + ' g/kWh'", _fn("barFlowDraw"))


if __name__ == "__main__":
    unittest.main()
