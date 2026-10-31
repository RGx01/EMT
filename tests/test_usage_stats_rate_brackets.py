"""
BL-93 PR 2 (rate brackets design §5, 31 Oct 2026) — Usage Stats' Rate brackets view.

A Show switch beside the metric: Devices (the chart as it was) or Rate brackets, where the bars are
grid import by the 2.5p bracket of the rate it was charged (`/api/energy?fields=brackets`, PR 1) for
the whole meter, the house or one device, chosen in a picker. kWh or £ follows the metric switch; %
shows each bracket's share of the bar. The table goes down the brackets for the range in view, with
kWh, £, average p/kWh and share, and a line above it gives the series' average against the meter's.
The Net line, the standing charge, CO₂ and the period sort belong to the Devices view.

What they pin, as the other Usage Stats tests do, from the page's source:

- the switch, the picker and % in the toolbar, and the state saved and restored;
- the brackets read once per data version (per day for HH) and summed into each bucket as the
  meters are, so Monthly, Daily, Yearly and Billing / Calendar need nothing of their own;
- the datasets and the table built from the same brackets, so a bracket's colour agrees;
- what is hidden in the view.

Checked in the local preview on 31 Oct 2026, on a scratch copy of staging: Monthly by date, eight
brackets for the whole meter, the table's totals the meter's stored kWh and £ for all history; EV,
house and battery series; % summing to 100 per bar; Cost; Daily (31 bars); HH (48 slots, the day's
brackets); back to Devices with the Net box and the period table restored; dark and light; 375 px
with no sideways scroll; the tariff overlay drawn over the brackets.

Discriminating: every test fails on the parent tree, where none of it existed.
"""
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(_ROOT, "web", "templates", "charts.html"), encoding="utf-8").read()


def _fn(name):
    i = PAGE.index("function " + name + "(")
    return PAGE[i:PAGE.index("\n}\n", i) + 2]


class TheToolbar(unittest.TestCase):

    def test_the_show_switch(self):
        self.assertIn("""onclick="barSetShow('devices',this)">Devices</button>""", PAGE)
        self.assertIn("""onclick="barSetShow('brackets',this)">Rate brackets</button>""", PAGE)

    def test_the_series_picker_and_share(self):
        self.assertIn('<select id="bar-br-series" onchange="barSetBrSeries(this.value)"', PAGE)
        self.assertIn('<input type="checkbox" id="bar-br-pct" onchange="barSetBrPct(this.checked)"', PAGE)

    def test_the_choices_are_the_meter_the_house_and_each_device(self):
        f = _fn("barBrSeriesList")
        self.assertIn("var out = [{ id: 'meter', label: 'Whole meter' }];", f)
        self.assertIn("out.push({ id: 'electricity_main', label: (main ? main.label : 'Direct') + ' (house)' });", f)
        self.assertIn("var devs = barMeters.filter(function(m) { return m.is_sub && have[m.id]; });", f)

    def test_the_state_is_kept(self):
        save, load = _fn("barSaveState"), _fn("barLoadState")
        for k in ("show:     barShow,", "brSeries: barBrSeries,", "brPct:    barBrPct,"):
            self.assertIn(k, save)
        self.assertIn("if (['devices','brackets'].indexOf(st.show) !== -1) barShow = st.show;", load)


class TheData(unittest.TestCase):

    def test_read_once_per_data_version(self):
        f = _fn("barBrReady")
        self.assertIn("if (barBrDays && barBrToken === tok) return true;", f)
        self.assertIn("barBrFetch('resolution=day&kind=all')", f)
        self.assertIn("barBrFetch('resolution=block&kind=day&date=' + encodeURIComponent(barHHDate))", f)
        self.assertIn("var r = await fetch(apiUrl('/api/energy?fields=brackets&' + q));", _fn("barBrFetch"))

    def test_render_waits_for_them(self):
        r = _fn("barRender")
        self.assertIn("if (barBrackets() && !barBrReady()) return;", r)
        self.assertLess(r.index("if (barBrackets() && !barBrReady()) return;"), r.index("var pd = barGetDataForPeriod();"))

    def test_summed_into_each_bucket_as_the_meters_are(self):
        agg = _fn("barAggRows")
        self.assertIn("var _b = barBrDays && barBrDays[barBrDayKey(row)];", agg)
        self.assertIn("_a[0] += _b[sr][n][0]; _a[1] += _b[sr][n][1];", agg)
        hh = _fn("barGetDataForPeriod")
        self.assertIn("rows = rows.map(function(r) { return Object.assign({}, r, { br: barBrHH[r.slot] || {} }); });", hh)


class TheChart(unittest.TestCase):

    def test_the_bars_are_the_brackets(self):
        self.assertIn("if (barBrackets()) return barBrDatasets(aggList);", _fn("barGetDatasets"))
        ds = _fn("barBrDatasets")
        self.assertIn("var ns = barBrNs(aggList);", ds)
        self.assertIn("if (barBrPct) return totals[i] > 0 ? Math.round(x / totals[i] * 10000) / 100 : 0;", ds)
        self.assertIn("backgroundColor: barBrColor(n, ns)", ds)

    def test_kwh_or_cost_follows_the_metric(self):
        self.assertIn("function barBrVal(v) { return barMetric === 'cost' ? v[1] : v[0]; }", PAGE)

    def test_a_bracket_is_named_by_its_pence(self):
        self.assertIn("return barBrPence(n * barBrWidth) + '\u2013' + barBrPence((n + 1) * barBrWidth) + 'p';",
                      _fn("barBrLabel"))

    def test_cheap_to_dear_by_price_neighbours_apart(self):
        c = _fn("barBrColor")
        self.assertIn("var t = hi > lo ? (n - lo) / (hi - lo) : 0;", c)
        self.assertIn("var i = ns.indexOf(n), alt = i % 2 ? (dark ? 14 : 13) : 0;", c)
        self.assertIn("if (n < 0) return 'hsl(215, 70%, '", c)

    def test_share_is_out_of_a_hundred(self):
        self.assertIn("barBrackets() && barBrPct ? { min: 0, max: 100, grace: 0 } : {}", _fn("barRender"))


class TheTable(unittest.TestCase):

    def test_down_the_brackets(self):
        self.assertIn("if (barBrackets()) { barBrBuildTable(tbl, aggList); return; }", _fn("barBuildTable"))
        t = _fn("barBrBuildTable")
        self.assertIn("<th>Rate bracket</th><th>kWh</th><th>Cost</th><th>Avg p/kWh</th><th>Share of kWh</th>", t)
        self.assertIn("var ns = barBrNs(aggList);", t)          # the chart's brackets: the colours agree

    def test_the_average_is_cost_over_kwh(self):
        self.assertIn("return k > 0 ? c / k * 100 : null;", _fn("barBrAvg"))

    def test_the_line_above_it(self):
        n = _fn("barBrNote")
        self.assertIn("var mine = barBrAvg(barBrSum(aggList, barBrSeries));", n)
        self.assertIn("txt += ', the whole meter ' + all.toFixed(1) + 'p/kWh';", n)
        self.assertIn('<div id="bar-br-note"', PAGE)


class WhatTheViewHides(unittest.TestCase):

    def test_no_net_line(self):
        self.assertIn("if (barBrackets()) return false;", _fn("barShowNet"))

    def test_no_standing_charge_co2_or_period_sort(self):
        self.assertIn("if (sw) sw.style.display = barMetric === 'cost' && !barBrackets() ? 'flex' : 'none';",
                      _fn("barRender"))
        ui = _fn("barBrSyncUI")
        self.assertIn("vis('bar-net-wrap', !on, 'flex');", ui)
        self.assertIn("vis('bar-sort-btn', !on);", ui)
        self.assertIn("if (co2 && on) co2.style.display = 'none';", ui)
        self.assertIn("if (v === 'brackets' && barMetric === 'co2') barMetric = 'kwh';", _fn("barSetShow"))


if __name__ == "__main__":
    unittest.main()
