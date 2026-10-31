"""
Usage Stats: an Import column, and Net as a line over the bars (31 Oct 2026).

The data table had no whole-import column when there were devices, so a user summed the device
columns by hand. And Net was a view of its own (Totals / Net buttons), a bar per period in place
of the breakdown, with its own three-column table.

What they pin:

- **The Import column** comes after the meters' columns when there are devices. It shows the meter's
  figure (`imp_kwh` / `imp_cost`; gross import carbon for CO₂), not a sum of the columns beside it,
  and is left out of the row's Total, which already counts the meters.
- **Net is a checkbox** that draws a line of import − export over the stacked bars, in its own
  stack and on top, kept out of the month-order year shading. The Totals / Net buttons are gone,
  and the table always shows the breakdown.
- **A saved Net view** comes back with the box ticked.

Then (31 Oct 2026, a second change):

- **HH view: the line is a running total** ("Running total"), the unrounded half-hours' net summed
  from midnight, so its last point is the day's net; on a right-hand axis of its own, its zero level
  with the bars'.
- **The Import column** comes after the standing charge, before Export.
- **A theme flip redraws the chart**, whichever toggle flipped it: the sidebar's tells only iframes, so
  the Net line (the chart's text colour) stayed near-white on a light page.

Checked in the local preview on 31 Oct 2026, on a scratch copy of staging, on 28 Oct: the HH line's
last point equals the Daily view's net for the day in cost (with the standing charge) and CO₂, and to
within the server's 1 Wh rounding in kWh; the two axes' zeros fall on the same pixel; the sidebar's
toggle turns the line dark on light and back.


Checked in the local preview on 31 Oct 2026, on a scratch copy of staging: the Import column equals
the meters' columns on every row in kWh, and so does the Total. The net line equals import − export
(cost: + the standing charge when it is shown; CO₂: the net carbon).

Structural, as the page's other Usage Stats tests are (no JavaScript runtime here). Discriminating:
every test fails on its parent (the first change's on #345's tree, the second's on #347's).
"""
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(_ROOT, "web", "templates", "charts.html"), encoding="utf-8").read()


def _fn(name):
    i = PAGE.index("function " + name + "(")
    return PAGE[i:PAGE.index("\n}\n", i) + 2]


class TheImportColumn(unittest.TestCase):

    def setUp(self):
        self.table = _fn("barBuildTable")

    def test_it_follows_the_meters_when_there_are_devices(self):
        self.assertIn("var hasDevices = barMeters.some(function(m) { return m.is_sub; });", self.table)
        self.assertIn("if (hasDevices) cols.push({key:'imp_total', label:'Import', inTotal: false});", self.table)

    def test_it_comes_after_the_standing_charge(self):
        sc = self.table.index("cols.push({key:'standing'")
        imp = self.table.index("cols.push({key:'imp_total'")
        exp = self.table.index("cols.push({key:'exp'")
        self.assertLess(sc, imp)
        self.assertLess(imp, exp)
        self.assertIn("if (hasDevices) cols.push({key:'net_imp', label:'Import CO₂', inTotal: false});", self.table)

    def test_it_is_the_meters_figure_not_a_sum_of_the_columns(self):
        self.assertIn("if (col.key === 'imp_total') return barMetric === 'kwh' ? (agg.imp_kwh || 0) : "
                      "(agg.imp_cost || 0);", self.table)

    def test_the_row_total_leaves_it_out(self):
        self.assertIn("if (c.inTotal !== false && c.key !== 'avg_intensity') rowTotal += vr;", self.table)


class TheNetLine(unittest.TestCase):

    def test_the_buttons_are_gone(self):
        for gone in ('id="bar-view-seg"', "barSetView(", "barView"):
            self.assertNotIn(gone, PAGE)

    def test_a_checkbox_draws_it_over_the_bars(self):
        self.assertIn('<input type="checkbox" id="bar-net-cb" onchange="barSaveState(); barRender();"', PAGE)
        ds = _fn("barGetDatasets")
        self.assertIn("if (barShowNet()) {", ds)
        for part in ("type: 'line', label: barNetLabel(), _net: true",
                     "data: barNetSeries(aggList, isHH)",
                     "stack: 'net'", "order: -1"):
            self.assertIn(part, ds)

    def test_it_is_import_minus_export(self):
        net = _fn("barNetRaw")
        self.assertIn("var imp = barMetric === 'kwh' ? agg.imp_kwh : (agg.imp_cost + barStandingVal(agg));", net)
        self.assertIn("return imp - exp;", net)
        self.assertIn("if (barMetric === 'co2') return agg.carbon_g_net != null ? agg.carbon_g_net : null;", net)
        self.assertIn("var v = barNetRaw(agg);", _fn("barNetVal"))

    def test_the_year_shading_leaves_it_alone(self):
        self.assertIn("if (ds._net) return;", _fn("barApplyYearGradient"))

    def test_the_legend_names_it(self):
        self.assertIn("var net = barShowNet() ? line(barNetColor(), barNetLabel()) : '';", _fn("barBuildLegend"))


class TheHalfHourView(unittest.TestCase):
    """HH: the line is a running total of the day's net, on an axis of its own whose zero is level with the bars'."""

    def test_it_runs_over_the_day(self):
        ser = _fn("barNetSeries")
        self.assertIn("if (!isHH) return aggList.map(barNetVal);", ser)
        self.assertIn("var v = barNetRaw(agg);", ser)
        self.assertIn("if (v != null) run = (run || 0) + v;", ser)       # unrounded: 48 roundings drift
        self.assertIn("data: barNetSeries(aggList, isHH),", _fn("barGetDatasets"))

    def test_it_has_its_own_axis_with_the_zeros_level(self):
        self.assertIn("yAxisID: isHH ? 'yNet' : 'y',", _fn("barGetDatasets"))
        render = _fn("barRender")
        self.assertIn("var netAxis = !!pd.isHH && barShowNet();", render)
        self.assertIn("var axes = netAxis ? barAlignZero(datasets) : null;", render)
        self.assertIn("yNet: Object.assign({ display: netAxis, position: 'right'", render)
        align = _fn("barAlignZero")
        self.assertIn("var f = Math.max(share(pos, neg), share(lmax, lmin));", align)
        self.assertIn("return { y: range(pos, neg), yNet: range(lmax, lmin) };", align)

    def test_it_is_named_so(self):
        self.assertIn("return barPeriod === 'hh' ? 'Running total' : 'Net';", _fn("barNetLabel"))
        self.assertIn("'\\nRunning total: '", _fn("barRender"))


class TheTheme(unittest.TestCase):

    def test_a_flip_redraws_it_whichever_toggle_flipped_it(self):
        """The sidebar's toggle tells only iframes; Usage Stats is drawn in the page, so its Net line
        kept the old theme's colour (near-white on a light page)."""
        i = PAGE.index("new MutationObserver(function(muts) {")
        obs = PAGE[i:PAGE.index("});", PAGE.index(".observe(", i)) + 3]
        self.assertIn("if (flipped && barInited && barData && window.Chart) barRender();", obs)
        self.assertIn("attributeFilter: ['data-theme']", obs)


class TheSavedState(unittest.TestCase):

    def test_it_is_saved_and_an_old_net_view_turns_it_on(self):
        self.assertIn("net:      barShowNet(),", _fn("barSaveState"))
        self.assertIn("nc.checked = st.net === true || (st.net === undefined && st.view === 'net');",
                      _fn("barLoadState"))


if __name__ == "__main__":
    unittest.main()
