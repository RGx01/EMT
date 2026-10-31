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

Checked in the local preview on 31 Oct 2026, on a scratch copy of staging: the Import column equals
the meters' columns on every row in kWh, and so does the Total. The net line equals import − export
(cost: + the standing charge when it is shown; CO₂: the net carbon).

Structural, as the page's other Usage Stats tests are (no JavaScript runtime here). Discriminating:
every test fails on the parent.
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
        for part in ("type: 'line', label: 'Net', _net: true", "data: aggList.map(barNetVal)",
                     "stack: 'net'", "order: -1"):
            self.assertIn(part, ds)

    def test_it_is_import_minus_export(self):
        net = _fn("barNetVal")
        self.assertIn("var imp = barMetric === 'kwh' ? agg.imp_kwh : (agg.imp_cost + barStandingVal(agg));", net)
        self.assertIn("return Math.round((imp - exp) * 1000) / 1000;", net)
        self.assertIn("var v = agg.carbon_g_net;", net)

    def test_the_year_shading_leaves_it_alone(self):
        self.assertIn("if (ds._net) return;", _fn("barApplyYearGradient"))

    def test_the_legend_names_it(self):
        self.assertIn("var net = barShowNet() ? line(barNetColor(), 'Net') : '';", _fn("barBuildLegend"))


class TheSavedState(unittest.TestCase):

    def test_it_is_saved_and_an_old_net_view_turns_it_on(self):
        self.assertIn("net:      barShowNet(),", _fn("barSaveState"))
        self.assertIn("nc.checked = st.net === true || (st.net === undefined && st.view === 'net');",
                      _fn("barLoadState"))


if __name__ == "__main__":
    unittest.main()
