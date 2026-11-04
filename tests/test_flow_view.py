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

Every test fails on the parent tree, where none of it existed.
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
                                       "barFlowSeriesLabel", "barFlowSeriesColor", "barFlowData")) +
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
        d = self._run(self.AGG)
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

    def test_export_leaves_the_house(self):
        d = self._run(self.AGG)
        self.assertEqual(self._flows(d)[("s:electricity_main", "export")], [3.0, 0.45, 600])
        self.assertEqual(d["nodes"]["export"]["column"], 3)

    def test_the_standing_charge_in_pounds_with_the_box(self):
        self.assertNotIn(("grid", "standing"), self._flows(self._run(self.AGG, "cost")))
        f = self._flows(self._run(self.AGG, "cost", standing=True))
        self.assertEqual(f[("grid", "standing")], [0, 1.0, 0])

    def test_a_small_band_goes_into_other(self):
        agg = [{"br": {"electricity_main": {"3": [100.0, 8.0, 0], "10": [1.0, 0.27, 0], "-1": [0.5, -0.01, 0]}}}]
        d = self._run(agg)
        self.assertEqual(d["other"], [-1, 10])                          # 1.5 of 101.5 kWh: under 2% each
        self.assertIn("b:other", d["nodes"])
        self.assertNotIn("b:10", d["nodes"])
        self.assertAlmostEqual(self._flows(d)[("b:other", "s:electricity_main")][0], 1.5)

    def test_other_is_by_the_metric_shown(self):
        # 10p bracket: 3% of the kWh, but 12% of the £
        agg = [{"br": {"electricity_main": {"1": [97.0, 2.0, 0], "10": [3.0, 0.27, 0]}}}]
        self.assertEqual(self._run(agg, "kwh")["other"], [])
        self.assertEqual(self._run(agg, "cost")["other"], [])
        agg[0]["br"]["electricity_main"]["10"] = [1.0, 0.27, 0]          # 1% of the kWh, 12% of the £
        self.assertEqual(self._run(agg, "kwh")["other"], [10])
        self.assertEqual(self._run(agg, "cost")["other"], [])

    def test_the_car_without_a_device_is_ev(self):
        agg = [{"br": {"electricity_main": {"3": [1.0, 0.08, 0]}, "ev_dispatch": {"2": [5.0, 0.35, 0]}}}]
        d = self._run(agg, meters=METERS[:1])
        self.assertEqual(d["nodes"]["s:ev_dispatch"]["label"], "EV")


class TheWiring(unittest.TestCase):

    def test_the_plugin_is_pinned(self):
        self.assertIn("var FLOW_PLUGIN = 'https://cdn.jsdelivr.net/npm/chartjs-chart-sankey@0.19.1/dist/"
                      "chartjs-chart-sankey.min.js';", PAGE)

    def test_flow_is_a_view(self):
        self.assertIn("onclick=\"barSetShow('flow',this)\"", PAGE)
        self.assertIn("['devices','brackets','flow','running'].indexOf(st.show)", PAGE)
        self.assertIn("if (barFlow()) { barFlowRender(pd.aggList); return; }", _fn("barRender"))
        self.assertIn("if (barFlow()) { barFlowBuildTable(tbl, aggList); return; }", _fn("barBuildTable"))

    def test_the_hh_rows_carry_their_brackets_for_flow(self):
        self.assertIn("if ((barBrackets() || barFlow()) && barBrHH) {", _fn("barGetDataForPeriod"))
        self.assertIn("if ((barBrackets() || barFlow()) && !barBrReady()) return;", _fn("barRender"))

    def test_drawn_at_once_when_the_plugin_is_in(self):
        f = _fn("barFlowRender")
        self.assertIn("if (window.Chart && Chart.registry.controllers.get('sankey')) { barFlowDraw(aggList); return; }", f)
        self.assertLess(f.index("barFlowDraw(aggList); return;"), f.index("barFlowLib().then("))

    def test_the_bands_are_imports_colours(self):
        self.assertIn("var ex = !asImport && barBrSign() < 0;", _fn("barBrColor"))
        self.assertIn("barBrColor(n, ns, true)", _fn("barFlowData"))

    def test_export_says_where_emt_cannot_see(self):
        self.assertIn("Export is energy EMT did not meter coming in: solar or a battery.", _fn("barFlowDraw"))


if __name__ == "__main__":
    unittest.main()
