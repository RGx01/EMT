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

Then (31 Oct 2026, a third change): HH's two axes share one set of gridlines, each on a round step,
and a label carries the decimals it needs; the 📅 opens a panel of the page's own with Go and Cancel
(Escape and a click outside close it too), and a Today button. Checked in the preview, staging copy,
on 20, 26, 28 and 31 Oct in kWh and cost: every tick of the two axes on the same pixel; the panel's
Cancel, outside click and Escape leave the day, Go loads the new one, Today goes to today and greys.

Then (31 Oct 2026, a fourth change): HH's table Total in cost is the half-hours' unrounded nets,
summed and then rounded; the hover drops the half-hour's Net; the checkbox reads "Running total" on
HH. Checked in the preview, staging copy, on 20, 26 and 28 Oct with and without the standing
charge: the table's Total, the Running total's last point and the Daily view's net agree. And in
cost a day or longer's Net line and hover are the bill's net (per-day rounded, summed), as the table
and the Billing tab: every point, hover and table row agreed in Daily, Monthly and Yearly, with and
without the standing charge.

Checked in the local preview on 31 Oct 2026, on a scratch copy of staging, on 28 Oct: the HH line's
last point equals the Daily view's net for the day in cost (with the standing charge) and CO₂, and to
within the server's 1 Wh rounding in kWh; the two axes' zeros fall on the same pixel; the sidebar's
toggle turns the line dark on light and back.


Checked in the local preview on 31 Oct 2026, on a scratch copy of staging: the Import column equals
the meters' columns on every row in kWh, and so does the Total. The net line equals import − export
(cost: + the standing charge when it is shown; CO₂: the net carbon).

Structural, as the page's other Usage Stats tests are (no JavaScript runtime here). Discriminating:
every test fails on its parent (the first change's on #345's tree, the second's on #347's, the
third's on #348's, the fourth's on #349's).
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
        self.assertIn("return { y: range(best.steps[0]), yNet: range(best.steps[1]) };", align)

    def test_the_two_axes_share_their_gridlines(self):
        """Bounds at the data's ends gave Chart.js odd end labels (7 kWh beside 6) and put the right's
        labels between the left's gridlines. Now: one count of intervals and one split about zero for
        both, each axis on a round step."""
        align = _fn("barAlignZero")
        self.assertIn("for (var T = 4; T <= 7; T++) {", align)
        self.assertIn("for (var d = 0; d <= T; d++) {", align)
        self.assertIn("return { min: -best.d * st, max: (best.T - best.d) * st, grace: 0, step: st };", align)
        self.assertIn("var m = [1, 2, 2.5, 5, 10].find(", _fn("barNiceStep"))
        render = _fn("barRender")
        self.assertIn("axes ? { stepSize: axes.y.step } : {}", render)
        self.assertIn("axes ? { stepSize: axes.yNet.step } : {}", render)

    def test_a_label_has_the_decimals_it_needs(self):
        """A 2.5 kWh step read "3 kWh" (0 dp), £0.25 read "£0.3", and a zero that arrived as -3e-17
        read "-£0.0"."""
        lab = _fn("barYLabel")
        self.assertIn("v = Math.round(v * 1e6) / 1e6;", lab)
        self.assertIn("while (dp < 2 && Math.abs(v * Math.pow(10, dp) - Math.round(v * Math.pow(10, dp))) > 1e-6) dp++;", lab)

    def test_it_is_named_so(self):
        self.assertIn("return barPeriod === 'hh' ? 'Running total' : 'Net';", _fn("barNetLabel"))
        self.assertIn("'\\nRunning total: '", _fn("barRender"))

    def test_the_checkbox_is_named_so(self):
        self.assertIn('<span id="bar-net-text">Net</span>', PAGE)
        self.assertIn("if (_nt) _nt.textContent = barNetLabel();", _fn("barRender"))

    def test_the_hover_has_no_half_hours_net(self):
        """HH's hover: Import, Export and the running total; no Net. And the running total once — not
        in the footer when the line's own row already says it."""
        render = _fn("barRender")
        i = render.index("if (barPeriod === 'hh') {\n              var run = ")
        hh = render[i:render.index("}", i)]
        self.assertNotIn("Net: ", hh)
        self.assertIn("(onLine ? '' : '\\nRunning total: '", hh)
        self.assertIn("var onLine = items.some(function(it) { return it.dataset._net; });", render)


class TheHalfHourTable(unittest.TestCase):
    """HH in cost: the table's Total summed 48 half-hours' net_cost, each rounded to the penny on the
    server, and overstated the day by pennies (staging, 20 Oct: £2.00 for a £1.86 day; the Running
    total and the Daily view said £1.86)."""

    def test_a_row_is_its_own_unrounded_net(self):
        t = _fn("barBuildTable")
        self.assertIn("var rowTotalFmt = barMetric === 'cost' && pd.isHH ? barNetRaw(agg)", t)

    def test_the_total_rounds_the_sum_not_the_rows(self):
        t = _fn("barBuildTable")
        self.assertIn("if (barMetric === 'cost' && pd.isHH) {\n    grandTotalFmt = Math.round(grandTotal * 100) / 100;", t)

    def test_a_value_shown_as_zero_has_no_sign(self):
        self.assertIn("return (v < 0 && Number(shown) !== 0 ? '-' : '') + sym + shown + unit;", _fn("barFmt"))


class TheBillsNet(unittest.TestCase):
    """In cost, a day or longer's Net line and hover are the bill's net: each day rounded to the penny,
    then summed (billing.bill_net_from_day_rollups), as the table, the Billing tab, Insights and the
    spiral say it. Summing the unrounded pounds put the line a penny or so off them (staging, the
    period from 3 Oct: £9.16 on the line, £9.15 everywhere else)."""

    def test_the_line_is_the_bills_net(self):
        raw = _fn("barNetRaw")
        self.assertIn("if (barMetric === 'cost' && barPeriod !== 'hh' && agg.net_cost != null)", raw)
        self.assertIn("return Math.round((agg.net_cost - (barIncStanding() ? 0 : (agg.standing || 0))) * 100) / 100;", raw)

    def test_the_hover_says_the_lines_figure(self):
        self.assertIn("var net = barNetVal(agg);", _fn("barRender"))


class TheDayNavigator(unittest.TestCase):
    """HH's 📅 opens a panel of the page's own; the browser's picker, opened straight from the button,
    had no way out on some browsers but picking a day. And a Today button."""

    def test_the_browsers_picker_is_not_opened_from_the_button(self):
        sub = _fn("barBuildSubBar")
        self.assertNotIn("calInp.showPicker()", PAGE)
        self.assertIn("calBtn.onclick = function() { barHHDatePanel(calWrap, today); };", sub)

    def test_the_panel_can_be_left_without_a_day(self):
        pnl = _fn("barHHDatePanel")
        self.assertIn("cancel.onclick = close;", pnl)
        self.assertIn("function key(e) { if (e.key === 'Escape') close(); }", pnl)
        self.assertIn("function outside(e) { if (!anchor.contains(e.target)) close(); }", pnl)
        self.assertIn("if (open) { open._close(); return; }", pnl)          # the button again
        self.assertIn("go.onclick = pick;", pnl)
        self.assertIn("if (v) barHHSelect(v > today ? today : v);", pnl)

    def test_today_is_a_button_greyed_on_today(self):
        sub = _fn("barBuildSubBar")
        self.assertIn("todayBtn.disabled = (barHHDate === today);", sub)
        self.assertIn("todayBtn.onclick = function() { barHHSelect(barTodayStr()); };", sub)
        self.assertIn("bar.appendChild(todayBtn);", sub)


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
