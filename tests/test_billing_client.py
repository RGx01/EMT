"""
The Billing tab drawn in the browser (web/templates/billing_module.html) — C,
30 Sep 2026 — and the one server addition it needed, `/api/periods?include=net_cost`.

THE PORT IS CHECKED IN THE BROWSER, NOT HERE. The suite cannot run JavaScript (no
Node on the Mac or the Pi). `.build/billing_client_parity.js` checks the JS bill card,
picker labels, vs Prev / vs Last Year picks and day side panels against the
pre-rendered page on a real database: 30 Sep 2026, 98 cards across the four modes,
98 labels, 98 compare pairs and 1,212 day panels, all identical; with a 1p nudge to
every card's total rejected (the check discriminates). What must not be lost is
pinned from the source here — each line learned by measuring it in a browser:

  * the card and side panel are PRINTED from bill_card's rows and decide nothing — no
    number is formatted in the browser (it was, for a day, in #21's JS port: its
    toFixed rounds a tie away from zero, 0.125 → "0.13" where Python gives "0.12");
  * the charged-rate line is Plotly's `hv`: a LINEAR x axis from -0.5 and
    `stepped: 'before'`, so each rate point at i − 0.5 covers its slot's bar;
  * day charts are released when far from view — a year is 365 canvases;
  * the energy axis's explicit range is padding, not a value (`includeBounds: false`,
    or a stray "6.6" labels the top);
  * the PDF export fills every day's panel before it writes (only those scrolled to
    are loaded).

These are new-surface tests: they fail on the parent tree, where the module and the
facet do not exist.
"""
import json
import os
import re
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_SRC = open(os.path.join(_ROOT, "web", "templates", "billing_module.html"), encoding="utf-8").read()
_PAGE = open(os.path.join(_ROOT, "web", "templates", "charts.html"), encoding="utf-8").read()


def _fn(src, name):
    """The body of `function name(` up to the next top-level `function`."""
    i = src.index("function %s(" % name)
    j = src.find("\n  function ", i + 1)
    return src[i:j if j > 0 else None]


class ModuleDecisions(unittest.TestCase):

    def test_the_card_and_panel_are_printed_not_computed(self):
        for f in ("rowHtml", "renderCard", "spans", "renderPanel", "optionLabel"):
            body = _fn(_SRC, f)
            self.assertNotIn(".toFixed(", body, "%s formats a number" % f)
            self.assertNotIn("Math.", body, "%s computes" % f)
        for gone in ("pyFixed", "pyRound", "function rateRows(", "function splitRows(", "SPLIT_BAND_EPS"):
            self.assertNotIn(gone, _SRC)
        self.assertIn("renderCard(bill.card)", _SRC)
        self.assertIn("renderPanel(day, row.panel_lines)", _SRC)
        self.assertIn("p.net_cost_text", _SRC)

    def test_the_rate_line_is_hv_on_a_linear_axis(self):
        self.assertIn("stepped: 'before'", _SRC)
        self.assertIn("x: {type: 'linear', min: -0.5, max: n - 0.5", _SRC)
        self.assertIn("m.rate_x.map(function (x, j) { return {x: x, y: m.rate[j]}; })", _SRC)

    def test_far_charts_are_released(self):
        self.assertIn("rootMargin: '4000px 0px'", _SRC)
        self.assertIn("if (!e.isIntersecting) releaseChart(e.target.dataset.day);", _SRC)

    def test_the_energy_axis_bounds_are_not_labelled(self):
        self.assertIn("y: {stacked: true, min: rg.y1[0], max: rg.y1[1]", _SRC)
        self.assertIn("ticks: {color: tc.axisC, includeBounds: false}", _SRC)

    def test_it_reads_the_api(self):
        self.assertIn("/api/periods?kind=' + MODES[mode].kind + '&include=net_cost", _SRC)
        self.assertIn("'/api/energy?resolution=period&' + query(mode, p) + '&include=billing'", _SRC)
        self.assertIn("'/api/energy?resolution=day&kind=month&year='", _SRC)
        self.assertNotIn("daily_usage.html", re.sub(r"<!--.*?-->", "", _SRC, flags=re.S))

    def test_the_theme_redraws(self):
        self.assertIn("attributeFilter: ['data-theme']", _SRC)


class ThePageUsesIt(unittest.TestCase):

    def test_it_is_included_and_the_billing_tab_routes_to_it(self):
        self.assertIn('{% include "billing_module.html" %}', _PAGE)
        fn = _PAGE[_PAGE.index("async function loadChartContent(name, force) {"):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn("if (name === 'daily') {", fn)
        self.assertIn("EMTBilling.init(document.getElementById('chart-daily-content'))", fn)
        self.assertIn("EMTBilling.rerender()", fn)
        self.assertIn("EMTBilling.refresh()", fn)

    def test_the_tab_no_longer_waits_for_a_pre_rendered_file(self):
        block = _PAGE[_PAGE.index('<div id="chart-daily" class="chart-frame-wrap">'):]
        block = block[:block.index("<!-- Heatmap chart -->")]
        self.assertNotIn("daily_exists", block)

    def test_the_pdf_reads_the_module(self):
        self.assertIn("EMTBilling.fillAll().then(function () {", _PAGE)
        self.assertIn("Object.assign(chartImages, EMTBilling.snapshotCharts());", _PAGE)
        self.assertNotIn("_getIframe(", _PAGE)


class PeriodTotals(unittest.TestCase):
    """`/api/periods?include=net_cost`: each period's bill total — the rule the
    Billing card's Total Bill uses — and whether it holds data, from ONE rollup."""

    @classmethod
    def setUpClass(cls):
        from tests.test_billing_view import _store
        cls.st = _store()

    @classmethod
    def tearDownClass(cls):
        cls.st.close()

    def _get(self, qs):
        from tests.test_server import make_client
        r = make_client(store=self.st).get("/api/periods" + qs)
        return r.status_code, json.loads(r.data)

    def test_each_total_is_the_cards_total(self):
        code, d = self._get("?kind=billing&include=net_cost")
        self.assertEqual(code, 200, d)
        with_data = [p for p in d["periods"] if p["has_data"]]
        self.assertEqual(len(with_data), 2)                       # Sep and Oct 2026
        for p in with_data:
            card = round(self.st.compute_period_net(p["start_utc"], p["end_utc"], "Europe/London"), 2)
            self.assertEqual(p["net_cost"], card, p["start"])

    def test_an_empty_period_says_so(self):
        """A month with no blocks between two that have them: has_data false, total 0."""
        from tests.test_billing_view import _store
        from tests.test_server import make_client
        st = _store()
        self.addCleanup(st.close)
        st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, imp_cost) "
            "VALUES ('2026-12-15T12:00:00', '2026-12-15T12:30:00', 'electricity_main', 1, 1.0, 0.3)")
        st._conn.commit()
        d = json.loads(make_client(store=st).get("/api/periods?kind=month&include=net_cost").data)
        by = {p["start"]: p for p in d["periods"]}
        self.assertEqual(sorted(by), ["2026-09-01", "2026-10-01", "2026-11-01", "2026-12-01"])
        self.assertFalse(by["2026-11-01"]["has_data"])
        self.assertEqual(by["2026-11-01"]["net_cost"], 0.0)
        self.assertTrue(by["2026-12-01"]["has_data"])

    def test_each_total_arrives_as_the_picker_prints_it(self):
        code, d = self._get("?kind=billing&include=net_cost")
        for p in d["periods"]:
            self.assertEqual(p["net_cost_text"], "£%.2f" % p["net_cost"])

    def test_the_default_still_carries_no_totals(self):
        code, d = self._get("?kind=billing")
        self.assertTrue(all("net_cost" not in p for p in d["periods"]))

    def test_an_unknown_facet_is_refused(self):
        self.assertEqual(self._get("?kind=billing&include=carbon")[0], 400)


if __name__ == "__main__":
    unittest.main()
