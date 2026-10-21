"""
The heatmap's two modes, the Missing view and `?day=` (data management design §1 (1), PR 1;
21 Oct 2026).

- **chart** (Charts, the Lovelace card; the default): the energy views — kWh, gCO₂, gCO₂/kWh —
  read-only. No Issues only, no drag, no tools; a cell's detail, a missing half-hour and the day
  column offer *Open in Data Management* for the day.
- **data** (Data Management): the Import bill and Export bill where they show today, and the
  Missing view (recorded / missing — the one data view without the supplier API); Issues only on
  unless turned off; every tool.
- `/data-management?day=YYYY-MM-DD` opens that day's month, unfolds the day, and marks it.

The heatmap cannot be run here, so these read its source, as the other heatmap tests do. All
discriminating (fail on the parent: none of it exists).
"""
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_T = os.path.join(_ROOT, "web", "templates")
_SRC = open(os.path.join(_T, "heatmap_module.html"), encoding="utf-8").read()


def _fn(name):
    i = _SRC.index("function " + name + "(")
    return _SRC[i:_SRC.index("\n  }\n", i)]


def _page(name):
    return open(os.path.join(_T, name), encoding="utf-8").read()


class TheModes(unittest.TestCase):

    def test_chart_is_the_default(self):
        body = _fn("init")
        self.assertIn("state.mode = opts.mode === 'data' ? 'data' : 'chart';", body)

    def test_data_mode_swaps_the_energy_views_for_the_data_views(self):
        body = _fn("init")
        self.assertIn("Object.keys(METRICS).forEach(function (k) { delete METRICS[k]; });", body)
        self.assertIn("METRICS.presence = PRESENCE_METRIC;", body)
        self.assertIn("state.metric = METRICS.bill ? 'bill' : 'presence';", body)

    def test_chart_mode_never_adds_the_bill_views(self):
        body = _fn("init")
        self.assertLess(body.index("if (isData()) {"), body.index("settle_views"))

    def test_issues_only_is_data_mode_and_on_by_default(self):
        self.assertIn("if (!isData()) return buildMonths(root);", _fn("buildUI"))
        self.assertIn("localStorage.getItem('emt_hm_collapse') !== '0'", _fn("init"))

    def test_no_drag_in_chart_mode(self):
        self.assertIn("if (ev.button !== 0 || !isData()) return;", _SRC)


class ChartModeLinksToDataManagement(unittest.TestCase):

    def test_the_url(self):
        self.assertIn("apiUrl('/data-management?day=' + encodeURIComponent(day))", _fn("dmUrl"))

    def test_a_cells_menu(self):
        self.assertIn("items.push({text: 'Open ' + h.day + ' in Data Management'", _SRC)

    def test_the_day_column(self):
        self.assertIn("if (!isData()) {\n      showMenu(ev, [{text: 'Open ' + day + ' in Data Management'",
                      _fn("dayBarPick"))

    def test_the_detail_panel(self):
        self.assertIn("'<a class=\"hm-btn hm-primary\" href=\"' + dmUrl(localParts(d.start).day) + '\">Open in Data Management</a>",
                      _fn("renderPanel"))

    def test_a_missing_half_hour_offers_no_fill(self):
        body = _fn("openMissing")
        self.assertIn("fc = g.length && isData() ? fillItem(", body)
        self.assertIn("Open in Data Management", body)


class TheMissingView(unittest.TestCase):

    def test_a_categorical_view_of_presence(self):
        self.assertIn("return isSettle(m) || m === 'presence';", _fn("isCat"))
        self.assertIn("if (m === 'presence') return c.missing ? 'missing' : 'present';", _fn("metricOf"))
        self.assertIn("if (m === 'presence') return v === 'present' ? C.green : 'rgba(0,0,0,0)';",
                      _fn("settleColour"))

    def test_its_totals_count_the_missing(self):
        self.assertIn("Object.keys(SETTLE_METRICS).concat(['presence']).forEach", _fn("monthGrid"))
        self.assertIn("presence: {missing: 1}", _SRC)

    def test_the_cells_say_what_they_are(self):
        self.assertIn("presence: {present: 'recorded', missing: MISSING_TEXT},", _SRC)
        self.assertIn(": isCat(state.metric) ? (hitTot ?", _SRC)


class TheDay(unittest.TestCase):

    def test_a_day_is_checked_and_opened(self):
        self.assertIn("/^\\d{4}-\\d{2}-\\d{2}$/.test(opts.day || '')", _fn("init"))
        self.assertIn("if (state.dayWanted) openDay(state.dayWanted);", _SRC)

    def test_it_draws_the_months_above_first(self):
        """Clicked through on a copy of staging's database (21 Oct 2026): scrolled at once, the
        day ended above the view — the months over it drew and folded far shorter than their
        placeholders. Only the months above move it, so they are drawn first."""
        self.assertIn("Promise.all(state.keys.slice(0, i + 1).map(function (k) { return loadMonth(k); }))",
                      _fn("openDay"))

    def test_it_unfolds_marks_and_scrolls(self):
        body = _fn("openDay")
        self.assertIn("if (f) expandFold(f);", body)
        self.assertIn("if (dr) markSelection(dr[0], dr[1]);", body)
        self.assertIn("scrollToRow(p, day);", body)

    def test_below_both_sticky_headers(self):
        """The view bar and the time axis under it are both sticky: the day went under the axis."""
        self.assertIn("['.hm-bar', '.hm-axis'].forEach(", _fn("scrollToRow"))


class TheHosts(unittest.TestCase):

    def test_charts_and_lovelace_are_chart_mode(self):
        self.assertIn("EMTHeatmap.init(document.getElementById('chart-heatmap-content'), {mode: 'chart'})",
                      _page("charts.html"))
        self.assertIn("EMTHeatmap.init(document.getElementById('chart-heatmap-content'), {mode: 'chart'})",
                      _page("lovelace_heatmap.html"))

    def test_data_management_is_data_mode_with_the_day(self):
        dm = _page("data_management.html")
        self.assertIn('{% include "heatmap_module.html" %}', dm)
        self.assertIn("{mode: 'data', day: new URLSearchParams(window.location.search).get('day')}", dm)


if __name__ == "__main__":
    unittest.main()
