"""
Flow, Usage Stats' Sankey (Flow design, PR 2; 4 Nov 2026): grid → rate band → the house and each device,
export out of the house, and in £ the standing charge, for the span on screen.

19 Nov 2026, the user: the house drawn at its own import, the export starting from it at that size and widening
or narrowing to the export's (barFlowTaper, from a stand-in node out of the layout); the export's bands coming
back together in one Export bar named with its total; the Grid named with its import. The tests rewritten for
the stand-in, and the taper's, house-size and Grid tests, fail on the tree before them.

The standing charge through its daily rates, as the import through its price (19 Nov 2026, the user): its two
tests fail on the tree before them; test_the_standing_charge_in_pounds_with_the_box, a bucket without its days'
rates going straight to the charge, passes on both.

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

Every test fails on the parent tree, where none of it existed. TheSpans and the Span
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
              "var FLOW_DISPLACED_COLOR = '#16a34a';\n"
              "var barMetric = %s, barMeters = %s, barBrSeries = 'meter';\n"
              "function barIsDark() { return true; }\n"
              "function barIncStanding() { return %s; }\n"
              "function barStandingVal(agg) { return agg.standing || 0; }\n" % (
                  json.dumps(metric), json.dumps(meters), "true" if standing else "false") +
              "".join(_fn(n) for n in ("barBrPence", "barBrLabel", "barBrSign", "barBrColor", "barFlowIndex",
                                       "barFlowFmt", "barFlowSeriesLabel", "barFlowSeriesColor", "barFlowExportEnds", "barFlowGridLabel", "barFlowPlain", "barFlowData")) +
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
        self.assertEqual(d["nodes"]["s:electricity_main"]["name"], "Direct (house)")

    def test_export_leaves_the_house_by_its_rate_band(self):
        # export bands design: the house's export through its own bands, in export's colours. Since 19 Nov
        # 2026 its flows leave `xsrc`, the house's stand-in that barFlowTaper draws from the house's box,
        # and the bands come back together in one Export bar named with its total (the user)
        d = self._run(self.AGG, "cost")
        f = self._flows(d)
        self.assertEqual(f[("xsrc", "x:6")], [3.0, 0.45, 600])
        self.assertEqual(f[("x:6", "export")], [3.0, 0.45, 600])
        self.assertEqual(d["nodes"]["x:6"]["column"], 3)
        self.assertEqual(d["nodes"]["x:6"]["label"], "Export 15–17.5p")
        self.assertEqual((d["nodes"]["export"]["label"], d["nodes"]["export"]["column"]), ("Export \u00b7 \u00a30.45", 4))
        self.assertEqual(d["totals"]["exp"], [3.0, 0.45, 600])

    def test_the_house_keeps_its_own_size_and_the_export_starts_from_it(self):
        # 19 Nov 2026, the user: "the house bar shows the export size rather than house size, it should start
        # as the house size and expand or contract to the export size". Nothing leaves the house in the data,
        # so the plugin draws it at its import; the stand-in is between columns, out of the layout, and drawn
        # by no one
        agg = [{"br": {"electricity_main": {"3": [8.0, 0.6, 1000]}, "export": {"6": [30.0, 4.5, 6000]}}}]
        for metric, col in (("kwh", 1.5), ("cost", 2.5), ("co2", 1.5)):
            d = self._run(agg, metric)
            self.assertFalse([k for k in self._flows(d) if k[0] == "s:electricity_main"], metric)
            x = d["nodes"]["xsrc"]
            self.assertEqual((x["proxy"], x["label"], x["column"], x["name"]), (True, "", col, "Direct (house)"), metric)

    def test_the_grid_is_named_with_its_import(self):
        self.assertEqual(self._run(self.AGG, "kwh")["nodes"]["grid"]["label"], "Grid \u00b7 16.0 kWh")
        self.assertEqual(self._run(self.AGG, "cost", standing=True)["nodes"]["grid"]["label"], "Grid \u00b7 \u00a33.36")
        self.assertEqual(self._run(self.AGG, "co2")["nodes"]["grid"]["label"], "Grid \u00b7 3.2 kg")

    def test_the_standing_charge_in_pounds_with_the_box(self):
        self.assertNotIn(("grid", "standing"), self._flows(self._run(self.AGG, "cost")))
        f = self._flows(self._run(self.AGG, "cost", standing=True))
        self.assertEqual(f[("grid", "standing")], [0, 1.0, 0])

    def test_the_standing_charge_through_its_daily_rates(self):
        # 19 Nov 2026, the user: "standing charges … are not banded as they change". Two buckets across a
        # change, 16 days at 47.63p then 14 at 50.46p: grid → each rate in the bands' column → the charge
        agg = [{"br": {"electricity_main": {"3": [10.0, 0.8, 0]}}, "standing": 7.6208, "standingBy": {"47.63": [7.6208, 16]}},
               {"br": {"electricity_main": {"3": [10.0, 0.8, 0]}}, "standing": 7.0644,
                "standingBy": {"50.46": [7.0644, 14]}}]
        d = self._run(agg, "cost", standing=True)
        f = self._flows(d)
        self.assertEqual(f[("grid", "sb:47.63")], [0, 7.6208, 0])
        self.assertEqual(f[("sb:50.46", "standing")], [0, 7.0644, 0])
        self.assertNotIn(("grid", "standing"), f)
        n = d["nodes"]
        self.assertEqual((n["sb:47.63"]["label"], n["sb:47.63"]["column"], n["sb:47.63"]["days"]), ("Standing 47.63p/day", 1, 16))
        self.assertEqual(n["standing"]["label"], "Standing charge \u00b7 \u00a314.69")
        self.assertAlmostEqual(d["totals"]["standing"], 14.6852)
        # not in kWh or CO₂, nor without the box
        for metric, on in (("kwh", True), ("cost", False)):
            self.assertFalse([k for k in self._run(agg, metric, standing=on)["nodes"] if k.startswith("sb:")])

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
        self.assertAlmostEqual(self._flows(d)[("xsrc", "x:other")][0], 1.0)
        self.assertAlmostEqual(self._flows(d)[("x:other", "export")][0], 1.0)
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

    def test_in_pounds_each_destination_is_named_with_its_net(self):
        # 5 Nov 2026, the user: the plunge's credit leaves the device, so the block is its cost before the
        # credit; its name says the net. The house 80p less 4p, the heat pump nothing less 2p
        agg = [{"br": {"electricity_main": {"-1": [4.0, -0.04, 600], "3": [10.0, 0.8, 1500]},
                       "heat_pump": {"-1": [2.0, -0.02, 300]}}}]
        n = self._run(agg, "cost")["nodes"]
        self.assertEqual(n["s:electricity_main"]["label"], "Direct (house) \u00b7 \u00a30.76")
        self.assertEqual(n["s:heat_pump"]["label"], "Heat Pump \u00b7 -\u00a30.02")
        self.assertEqual(n["s:electricity_main"]["name"], "Direct (house)")
        self.assertEqual([round(n["s:electricity_main"][k], 6) for k in ("gross", "credit", "net")], [0.8, 0.04, 0.76])

    def test_the_net_is_the_pence_shown_less_the_pence_shown(self):
        # the demo's EV, a month: £13.115 at 7.5-10p, £0.443 earned in plunges. Rounded on its own the net is
        # £12.67, and the hover read £13.12 − £0.44 = £12.67
        agg = [{"br": {"electricity_main": {"-2": [14.77, -0.443, 0], "3": [163.958, 13.115, 0]}}}]
        n = self._run(agg, "cost")["nodes"]["s:electricity_main"]
        self.assertEqual((n["gross"], n["credit"], n["net"]), (13.12, 0.44, 12.68))
        self.assertTrue(n["label"].endswith("\u00a312.68"))

    def test_GUARD_in_kwh_the_names_are_plain(self):
        agg = [{"br": {"electricity_main": {"-1": [4.0, -0.04, 600], "3": [10.0, 0.8, 1500]}}}]
        self.assertEqual(self._run(agg, "kwh")["nodes"]["s:electricity_main"]["label"], "Direct (house)")

    def test_the_credit_s_hover_does_the_sum_and_the_table_keeps_the_plain_name(self):
        draw = _fn("barFlowDraw")
        self.assertIn("' used ' + barFlowFmt(r._v[0], 0) + ' at negative prices and was paid '", draw)
        self.assertIn("' = ' + barFlowFmt(dn.net, 1) + ', its net cost.'", draw)
        self.assertIn("return n.name || n.label || k;", _fn("barFlowBuildTable"))
        self.assertIn("return r ? nm(r.from) + ' → ' + nm(r.to) : '';", draw)

    def test_paying_to_export_is_a_cost_out_of_the_house(self):          # drawn from the house's box
        agg = [{"br": {"electricity_main": {"3": [10.0, 0.8, 0]},
                       "export": {"-1": [2.0, -0.03, 0], "4": [5.0, 0.55, 0]}}}]
        d = self._run(agg, "cost")
        f = self._flows(d)
        self.assertEqual(f[("xsrc", "paid_export")], [2.0, 0.03, 0])
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
        self.assertEqual(f[("xsrc", "export")], [3.0, 0.45, 600])
        self.assertEqual(set(f), {("grid", "s:heat_pump"), ("grid", "s:electricity_main"),
                                  ("xsrc", "export")})                   # no band, no standing charge
        self.assertEqual([d["nodes"][k]["column"] for k in ("grid", "s:heat_pump", "export")], [0, 1, 2])
        self.assertEqual([round(x, 6) for x in d["totals"]["imp"]], [16.0, 2.36, 3200])

    def test_in_co2_the_export_is_drawn_as_what_it_displaced(self):
        # 5 Nov 2026, the user: "shouldn't it show export displacement?" — until then kept but not drawn
        d = self._run(self.AGG, "co2")
        self.assertEqual(self._flows(d)[("xsrc", "export")], [3.0, 0.45, 600])
        self.assertFalse(any(x.get("hide") for x in d["flows"]))
        self.assertEqual((d["nodes"]["export"]["label"], d["nodes"]["export"]["color"]),
                         ("Displaced by export \u00b7 0.6 kg", "#16a34a"))
        self.assertEqual(d["totals"]["exp"], [3.0, 0.45, 600])

    def test_GUARD_in_kwh_the_export_is_export(self):
        d = self._run(self.AGG, "kwh")
        self.assertEqual((d["nodes"]["export"]["label"], d["nodes"]["export"]["color"]), ("Export \u00b7 3.0 kWh", "#ff7f0e"))

    def test_no_credit_flows_outside_pounds(self):
        agg = [{"br": {"electricity_main": {"-1": [4.0, -0.04, 600], "3": [10.0, 0.8, 1500]},
                       "export": {"-1": [2.0, -0.03, 0]}}}]
        for metric in ("kwh", "co2"):
            d = self._run(agg, metric)
            self.assertFalse(any(x.get("credit") for x in d["flows"]), metric)
            self.assertNotIn("plunge", d["nodes"])
            self.assertEqual(self._flows(d)[("grid", "s:electricity_main")], [14.0, 0.76, 2100])

    def test_export_with_no_house_import_still_has_its_house(self):
        # no box to start from: the stand-in is drawn, as the house, in the house's column
        d = self._run([{"br": {"heat_pump": {"3": [1.0, 0.08, 0]}, "export": {"6": [2.0, 0.3, 0]}}}])
        x = d["nodes"]["xsrc"]
        self.assertEqual((x.get("proxy"), x["label"], x["column"]), (None, "Direct (house)", 1))
        self.assertEqual(self._flows(d)[("xsrc", "export")], [2.0, 0.3, 0])

    def test_the_car_without_a_device_is_ev(self):
        agg = [{"br": {"electricity_main": {"3": [1.0, 0.08, 0]}, "ev_dispatch": {"2": [5.0, 0.35, 0]}}}]
        d = self._run(agg, meters=METERS[:1])
        self.assertEqual(d["nodes"]["s:ev_dispatch"]["label"], "EV")


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheSpans(unittest.TestCase):
    """4 Nov 2026, the user: the spans are the tariffs, with their price changes, and the eras; in Devices,
    Rate brackets and Flow. A span's bars: a day up to 62 days, else a month. 5 Nov 2026, the user: the
    chapters (an era's start to the next era's) dropped, and the picker in Running total's year-start
    order — tariffs, then the eras by kind."""

    DAYS = ["2024-06-01", "2024-12-15", "2025-01-10", "2025-02-20", "2025-06-30"]

    def _js(self, tail, eras=(), tariffs=(), flow=False, res=None, order="date"):
        rows = [{"year": int(d[:4]), "month": int(d[5:7]), "day": int(d[8:]), "imp_kwh": 1.0} for d in self.DAYS]
        kinds = PAGE[PAGE.index("var ERA_KINDS = {"):PAGE.index("\n};\n", PAGE.index("var ERA_KINDS = {")) + 3]
        js = (kinds + "var BAR_ACC_MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];\n"
              "var barEras = %s, barTariffs = %s, barData = %s, FLOW_SPAN_DAILY_MAX = 62;\n" % (
                  json.dumps(list(eras)), json.dumps(list(tariffs)), json.dumps(rows)) +
              "var barSpanRes = %s, barMonthOrder = %s, barPeriod = 'span', barSpanId = null;\n" % (
                  json.dumps(res), json.dumps(order)) +
              "function barFlow() { return %s; }\n" % ("true" if flow else "false") +
              "function barAggRows(f) { return {imp_kwh: barData.filter(f).reduce(function (t, r) { return t + r.imp_kwh; }, 0)}; }\n" +
              "".join(_fn(n) for n in ("barAddDay", "barAddMonth", "barDayNo", "barFlowYmd", "erasGroups", "barEraLabel",
                                       "barTariffPriceStarts", "barTariffChangeDays", "barTariffVatOnly", "barAccDateLabel",
                                       "barSpans", "barSpanCurrent", "barSpanResolution", "barSpanMonthFirst",
                                       "barSpanOrder", "barGroupBreaks", "barSpanData")) + tail)
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
            ["Tariffs", True, "Flexible \u2014 from its start", "2024-06-01", "2024-10-01", False],
            # 16 Nov 2026, the user: the export tariffs too, in their own group (until then import only)
            ["Export tariffs", False, "Export tariff: Outgoing", "2024-06-01", "2025-07-01", True]])

    def test_tariffs_then_eras_by_kind_no_chapters(self):
        eras = [{"id": "c", "kind": "ev", "label": "Car", "start": "2024-09-01"},
                {"id": "s", "kind": "solar", "label": "Solar", "start": "2024-07-14"},
                {"id": "b", "kind": "battery", "label": "Battery", "start": "2025-01-01"}]
        out = self._js("\nconsole.log(JSON.stringify(barSpans().map(function (o) { return [o.group, o.label]; })));",
                       eras=eras, tariffs=self._tariffs())
        self.assertEqual([g for g, _ in out], ["Tariffs"] * 4 + ["Export tariffs", "Eras: Solar", "Eras: Battery", "Eras: EV"])
        self.assertEqual([l for _, l in out[5:]], ["\u2600\ufe0f Solar", "\U0001f50b Battery", "\U0001f697 Car"])

    def test_an_era_on_its_own_runs_to_its_own_end(self):
        # 4 Nov 2026, the user: the solar era on its own, through the battery's change; an era's end is
        # exclusive (a replaced battery ends the day the next starts). By kind, newest first in each
        eras = [{"id": "s", "kind": "solar", "label": "Solar", "start": "2024-07-14"},
                {"id": "b1", "kind": "battery", "label": "Battery", "start": "2024-07-14", "end": "2025-02-20"},
                {"id": "b2", "kind": "battery", "label": "Battery", "start": "2025-02-20"}]
        out = self._js("\nconsole.log(JSON.stringify(barSpans().filter(function (o) { return o.group.indexOf('Eras') === 0; })"
                       ".map(function (o) { return [o.id, o.from, o.to, !!o.latest]; })));", eras=eras)
        self.assertEqual(out, [["own:s", "2024-07-14", "2025-07-01", True],
                               ["own:b2", "2025-02-20", "2025-07-01", True],
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

    # 22 Nov 2026, the user: a span's bars a day, a month or a year, by Date or by Month as Monthly's
    def test_a_long_span_by_the_day_when_chosen(self):
        out = self._js("\nvar d = barSpanData({from: '2024-06-15', to: '2025-07-01'});"
                       "\nconsole.log(JSON.stringify(d.labels));", res="day")
        self.assertEqual(out, ["15 Dec", "10 Jan", "20 Feb", "30 Jun"])

    def test_by_the_year_clipped_to_its_dates(self):
        out = self._js("\nvar d = barSpanData({from: '2024-06-15', to: '2025-07-01'});"
                       "\nconsole.log(JSON.stringify([d.labels, d.bucketRanges, d.aggList.map(function (a) { return a.imp_kwh; })]));",
                       res="year")
        self.assertEqual(out, [["2024", "2025"],
                               [{"from": "2024-06-15", "to": "2025-01-01"}, {"from": "2025-01-01", "to": "2025-07-01"}],
                               [1.0, 3.0]])

    def test_by_the_year_there_is_no_month_order(self):
        out = self._js("\nvar d = barSpanData({from: '2024-01-01', to: '2025-07-01'});"
                       "\nconsole.log(JSON.stringify([d.labels, !!d.groups]));", res="year", order="month")
        self.assertEqual(out, [["2024", "2025"], False])

    def test_months_in_month_order_grouped_by_the_month(self):
        self.DAYS = ["2024-06-01", "2024-12-15", "2025-06-10", "2025-12-20", "2025-01-30"]
        out = self._js("\nvar d = barSpanData({from: '2024-01-01', to: '2026-01-01'});"
                       "\nconsole.log(JSON.stringify([d.labels, d.groups, d.bucketYears, barGroupBreaks(d.groups)]));",
                       res="month", order="month")
        self.assertEqual(out, [["Jan 2025", "Jun 2024", "Jun 2025", "Dec 2024", "Dec 2025"],
                               [1, 6, 6, 12, 12], [2025, 2024, 2025, 2024, 2025], [1, 3]])

    def test_days_in_month_order_each_month_of_days_its_own_group(self):
        # the Net line does not run on from 31 Jan '24 to 1 Jan '25
        self.DAYS = ["2024-01-31", "2025-01-01", "2024-02-02"]
        out = self._js("\nvar d = barSpanData({from: '2024-01-01', to: '2026-01-01'});"
                       "\nconsole.log(JSON.stringify([d.labels, d.groups]));", res="day", order="month")
        self.assertEqual(out, [["31 Jan 24", "1 Jan 25", "2 Feb 24"], ["2024-01", "2025-01", "2024-02"]])

    def test_flow_one_bucket(self):
        out = self._js("\nvar d = barSpanData({from: '2024-06-01', to: '2025-07-01', label: 'All'});"
                       "\nconsole.log(JSON.stringify([d.labels, d.aggList[0].imp_kwh]));", flow=True)
        self.assertEqual(out, [["All"], 5.0])


@unittest.skipUnless(shutil.which("node"), "node not available")
class ThePureControls(unittest.TestCase):

    def _js(self, expr):
        src = _fn("barOrderResShown") + _fn("barGroupBreaks") + _fn("barGroupLines") + \
              "\nconsole.log(JSON.stringify(%s));" % expr
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(src)
            path = fh.name
        try:
            res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_which_controls_show(self):
        cases = [("monthly", "devices", None), ("span", "devices", "day"), ("span", "devices", "year"),
                 ("span", "flow", "month"), ("span", "running", "day"), ("monthly", "running", None),
                 ("daily", "devices", None)]
        got = self._js("[%s]" % ",".join("barOrderResShown(%s,%s,%s)" % tuple(json.dumps(x) for x in c) for c in cases))
        # 24 Nov 2026: the resolution row is on every period, not only a span; not Flow's span, not Running total
        self.assertEqual([(g["order"], g["res"]) for g in got],
                         [(True, True), (True, True), (False, True), (False, False), (False, False),
                          (False, False), (False, True)])

    def test_bold_where_the_calendar_month_changes(self):
        # by the day in month order: Jan '24's days, Jan '25's, then Feb '24's — a thin line, then a bold one
        got = self._js("barGroupLines(['2024-01','2024-01','2025-01','2024-02'], [1,1,1,2])")
        self.assertEqual(got, [[2, False], [3, True]])

    def test_monthly_every_separator_bold(self):
        self.assertEqual(self._js("barGroupLines([1,1,2,2,3], [1,1,2,2,3])"), [[2, True], [4, True]])


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
        self.assertIn("if ((barBrackets() || barFlow()) && barBrHH) {", _fn("barGetDataForPeriodAll"))
        self.assertIn("if ((barBrackets() || barFlow()) && !barBrReady()) return;", _fn("barRender"))

    def test_drawn_at_once_when_the_plugin_is_in(self):
        f = _fn("barFlowRender")
        self.assertIn("if (window.Chart && Chart.registry.controllers.get('sankey')) { barFlowDraw(aggList); return; }", f)
        self.assertLess(f.index("barFlowDraw(aggList); return;"), f.index("barFlowLib().then("))

    def test_the_bands_are_imports_colours(self):
        self.assertIn("var ex = side ? side === 'export' : barBrSign() < 0;", _fn("barBrColor"))
        self.assertIn("barBrColor(n, ns, 'import')", _fn("barFlowData"))
        self.assertIn("barBrColor(n, xns, 'export')", _fn("barFlowData"))

    def test_the_buckets_carry_each_days_standing_rate(self):
        f = _fn("barAggRows")
        self.assertIn("var _sk = (Math.round(row.standing * 10000) / 100).toFixed(2)", f)
        self.assertIn("_sa[0] += row.standing; _sa[1] += 1;", f)
        self.assertIn("(f.to === 'standing' && f.from.indexOf('sb:') === 0)", _fn("barFlowBuildTable"))
        self.assertIn("' at ' + sbn.rate + 'p a day, inc VAT.'", _fn("barFlowDraw"))

    def test_a_credit_flow_is_drawn_in_pounds_only(self):
        self.assertIn("f.v[i] > 1e-9 && (!f.credit || i === 1)", _fn("barFlowDraw"))
        self.assertIn("if (f.credit || (f.to === 'export' && f.from.indexOf('x:') === 0) ||", _fn("barFlowBuildTable"))

    def test_the_taper_draws_from_the_house_box(self):
        t = _fn("barFlowTaper")
        self.assertIn("var dh = h * el.height / sum, poly = barFlowTaperPath(x, at, at + dh, el.x2, el.y2, el.y2 + el.height);", t)
        self.assertIn("el.draw = function() {};", t)
        self.assertIn("el.inRange = function(mx, my) { return barFlowInPoly(poly, mx, my); };", t)
        self.assertIn("beforeDatasetsDraw", t)                  # under the plugin's labels
        draw = _fn("barFlowDraw")
        self.assertIn("plugins: d.nodes.xsrc && d.nodes.xsrc.proxy ? [barFlowTaper(", draw)
        self.assertIn("(d.nodes[c.raw.from] || {}).proxy ? 'rgba(0,0,0,0)'", draw)

    def test_the_head_says_what_the_net_holds(self):
        h = _fn("barFlowHead")
        self.assertIn("'<span>Import is net of ' + barFlowFmt(-t.credit, 1) + ' earned at negative prices: Plunge credit</span>'", h)
        self.assertIn("'<span>Export is net of ' + barFlowFmt(-t.paid, 1) + ' paid to export at negative prices</span>'", h)

    def test_span_in_the_bar_views_yearly_but_in_flow(self):
        self.assertIn("onclick=\"barSetKind('span',this)\" id=\"bar-span-btn\"", PAGE)
        f = _fn("barSetShow")
        self.assertIn("if (v === 'flow' && barPeriod === 'yearly') barPeriod = barSpansPending() || barSpans().length ? 'span' : 'monthly';", f)
        ui = _fn("barBrSyncUI")
        self.assertIn("vis('bar-span-btn', spanOk);", ui)
        load = _fn("barLoadState")
        self.assertIn("if (st.period === 'era') barPeriod = 'span';", load)
        # Running total keeps the Span (16 Nov 2026): a reload in it no longer turns the period to Yearly
        self.assertNotIn("barShow === 'running') barPeriod", load)

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_running_total_keeps_the_span_of_the_view_left(self):
        # 16 Nov 2026, the user: Devices or Rate brackets on a Span, to Running total and back, came back Yearly
        js = ("var barShow = 'devices', barPeriod = 'span', barMetric = 'kwh', barBrError = null;\n"
              "function barSetActive() {} function barSaveState() {} function barSyncUI() {} function barRender() {}\n"
              "function barSpansPending() { return false; } function barSpans() { return [{id: 't'}]; }\n" +
              _fn("barSetShow") +
              "\nvar seen = [];\n['running', 'devices', 'running', 'brackets', 'running', 'flow', 'running', 'flow']"
              ".forEach(function (v) { barSetShow(v, null); seen.push(v + ':' + barPeriod); });\n"
              "console.log(JSON.stringify(seen));")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout),
                         ["running:span", "devices:span", "running:span", "brackets:span", "running:span",
                          "flow:span", "running:span", "flow:span"])

    def test_a_remembered_span_waits_for_the_eras_and_tariffs(self):
        # a reload restored the period before the eras were read, and fell back to Monthly (the preview)
        self.assertIn("if (barPeriod === 'span' && !spanOk && !barSpansPending()) barPeriod = 'monthly';", _fn("barBrSyncUI"))
        self.assertIn("if ((barEras.length || barPeriod === 'span' || barAccYearsWanted) && barInited && barData) barRender();", _fn("barLoadEras"))
        self.assertIn("if ((barTariffs.length || barPeriod === 'span' || barAccYearsWanted) && barInited && barData) barRender();", _fn("barLoadTariffs"))

    def test_the_overlays_on_a_span(self):
        # on a span in date order; in month order (22 Nov 2026) the bars are not a time axis
        self.assertIn("(barPeriod === 'span' && !barSpanMonthFirst());", _fn("barTariffsAvailable"))
        self.assertIn("(barPeriod === 'span' && !barSpanMonthFirst());", _fn("barErasAvailable"))

    def test_month_order_draws_separators_and_breaks_the_net_line(self):
        f = _fn("barRender")
        self.assertIn("if (pd.groups) barBreakNetAtGroups(datasets, pd.groups);", f)
        self.assertIn(".concat(pd.groups ? [barGroupSepPlugin(pd.groups, _majors)] : [])", f)
        self.assertIn("groups[c.p0DataIndex] !== groups[c.p1DataIndex] ? 'rgba(0,0,0,0)' : undefined", _fn("barBreakNetAtGroups"))
        # Monthly in month order carries its groups, by calendar month
        self.assertIn("groups: monthFirst ? buckets.map(function(b){ return b.m; }) : null", PAGE)

    def test_span_has_its_resolution_and_the_order(self):
        self.assertIn('<div class="bar-seg" id="bar-res-seg"', PAGE)
        self.assertNotIn('bar-spanres-seg', PAGE)
        self.assertIn("if (['day','month','year'].indexOf(st.spanRes) !== -1)     barSpanRes = st.spanRes;", PAGE)

    def test_one_place_decides_both_controls(self):
        # 22 Nov 2026, the user: "sometimes they disappear, page refresh only half brings them back" — barRender
        # had its own line showing the order on Monthly only, and hid it on a Span at every redraw
        self.assertIn("barOrderResSync();", _fn("barSyncUI"))
        r = _fn("barRender")
        self.assertIn("barOrderResSync();", r)
        self.assertNotIn("_orderSeg.style.display = (barPeriod === 'monthly')", r)
        self.assertIn("['bar-cal-seg', 'bar-order-seg', 'bar-res-seg',", PAGE)   # Running total hides both

    def test_flow_renames_no_buttons(self):
        # 24 Nov 2026: the resolution row names what each bar holds, so Flow's one bar needs no renamed buttons
        self.assertNotIn("FLOW_PERIOD_NAMES", PAGE)
        self.assertNotIn("b.textContent = fl ?", _fn("barBrSyncUI"))

    def test_the_active_period_by_period_or_span(self):
        f = _fn("barSyncUI")
        self.assertIn("var span = b.id === 'bar-span-btn';", f)
        self.assertIn("b.classList.toggle('active', span === (barPeriod === 'span'));", f)

    def test_export_says_where_emt_cannot_see(self):
        f = _fn("barFlowDraw")
        self.assertIn("if (r.to.indexOf('x:') === 0 || r.to === 'export') return 'Export is energy EMT did not meter coming in: solar or a battery.';", f)
        self.assertIn("if (r.to === 'plunge') {", f)



class ThePlainDraw(unittest.TestCase):
    """kWh and CO₂ (5 Nov 2026): the head's CO₂ export what it displaced, the hover's g/kWh; later the
    same day the CO₂ export drawn, as Displaced by export, where it had been hidden."""

    def test_the_head_says_what_export_displaced(self):
        self.assertIn("(i === 2 ? 'Displaced by export' : 'Export')", _fn("barFlowHead"))

    def test_the_hover_gives_grams_a_kwh(self):
        self.assertIn("Math.round(v[2] / v[0]) + ' g/kWh'", _fn("barFlowDraw"))

    def test_the_draw_hides_nothing_and_its_hover_says_displaced(self):
        draw = _fn("barFlowDraw")
        self.assertNotIn("f.hide", draw)
        self.assertIn("Grid carbon your export displaced", draw)


if __name__ == "__main__":
    unittest.main()
