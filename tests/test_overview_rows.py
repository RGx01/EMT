"""
The Overview's rows (docs/history/design/EMT-v5_overview_rows_design.md, 22 Nov 2026, the user): the page is rows of cards —
Power, Grid and carbon, Smart Charging, Agile — the user orders the rows and the Power row's cards, the Power row is
spread evenly, and the cost cards can be totals only. The orders are saved beside the hidden cards
(`overview_rows`, `overview_card_order`), the compact switch as `overview_costs_compact`.

Driven by hand in a browser on a copy of a real database: five device cards at 1,132 px wide sat 3 + 2; a row and a
card moved, the compact switch set, all kept across a reload; at 375 px every card one column, no sideways scroll.

Discriminating: every test fails on the parent tree (no rows, no orders, no switch).
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(_ROOT, "web", "templates", "live_power.html"), encoding="utf-8").read()


def _server():
    spec = importlib.util.spec_from_file_location("server_ovrows", os.path.join(_ROOT, "web", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv


class _Store:
    def __init__(self):
        self.saved = {}

    def get_settings(self):
        return dict(self.saved)

    def save_settings(self, s):
        self.saved = s


class TheSettings(unittest.TestCase):

    def setUp(self):
        self.srv = _server()
        self.store = _Store()
        for p in (mock.patch.object(self.srv.store_access, "store", return_value=self.store),
                  mock.patch.object(self.srv, "_engine_write", side_effect=lambda f: f(self.store))):
            p.start()
            self.addCleanup(p.stop)
        self.client = self.srv.app.test_client()

    def _post(self, body):
        return self.client.post("/api/settings", data=json.dumps(body), content_type="application/json")

    def test_the_orders_save_and_read_back(self):
        for name, val in (("overview_rows", ["grid", "power", "grid"]),
                          ("overview_card_order", ["device-sub_meter_2", "power"])):
            with self.subTest(name):
                self.assertEqual(self._post({name: val}).status_code, 200)
                want = list(dict.fromkeys(val))
                self.assertEqual(self.store.saved[name], want)
                self.assertEqual(self.client.get("/api/settings").get_json()[name], want)

    def test_they_start_empty(self):
        got = self.client.get("/api/settings").get_json()
        self.assertEqual((got["overview_rows"], got["overview_card_order"], got["overview_costs_compact"]),
                         ([], [], False))

    def test_a_bad_key_is_refused(self):
        for name in ("overview_rows", "overview_card_order"):
            for bad in (['a"]{}'], "grid", [3]):
                with self.subTest(name=name, bad=str(bad)):
                    self.assertEqual(self._post({name: bad}).status_code, 400)
                    self.assertNotIn(name, self.store.saved)

    def test_compact_power_saves(self):
        self.assertEqual(self._post({"overview_power_compact": True}).status_code, 200)
        self.assertIs(self.store.saved["overview_power_compact"], True)

    def test_compact_costs_saves(self):
        self.assertEqual(self._post({"overview_costs_compact": True}).status_code, 200)
        self.assertIs(self.store.saved["overview_costs_compact"], True)


class ThePage(unittest.TestCase):

    def test_the_rows(self):
        # each Agile card its own row, and the cost cards a row like the others (22 Nov 2026, the user)
        self.assertEqual(re.findall(r'data-ov-row="([^"]+)"', PAGE),
                         ["power", "charge", "grid", "agile-{{ ch }}", "costs"])

    def test_the_costs_row_is_inside_the_rows(self):
        i, j = PAGE.index('data-ov-row="costs"'), PAGE.index("</div><!-- /.live-power-page -->")
        self.assertLess(i, j)
        self.assertIn('id="ov-costs"', PAGE[i:j])

    def test_arrange_is_in_the_header_bar(self):
        i, j = PAGE.index("{% block topbar %}"), PAGE.index("{% endblock %}", PAGE.index("{% block topbar %}"))
        self.assertIn('id="ov-arrange-btn"', PAGE[i:j])

    def test_every_card_of_a_compact_row_has_the_rows_button(self):
        # one button on every card of the row, the same icon on both rows (22 Nov 2026, the user: it was on the first
        # card only, a different icon on each)
        self.assertIn('data-ov-row="power" data-ov-row-label="Power" data-ov-compact="power"', PAGE)
        self.assertIn('data-ov-row="costs" data-ov-row-label="Costs" data-ov-compact="costs"', PAGE)
        i = PAGE.index("function _ovDecorate(")
        body = PAGE[i:PAGE.index("\n}\n", i)]
        self.assertIn("var row = el.closest('.ov-row[data-ov-compact]');", body)
        self.assertIn("c.className = 'expand-btn ov-compact-btn';", body)
        self.assertNotIn('id="ov-compact-btn"', PAGE)
        self.assertNotIn('id="ov-power-compact-btn"', PAGE)
        i = PAGE.index("function _ovToolsText(")
        self.assertIn("b.innerHTML = on ? _OV_COMPACT_OUT : _OV_COMPACT_IN;", PAGE[i:PAGE.index("\n}\n", i)])

    def test_every_rows_cards_are_ordered_and_moved(self):
        i = PAGE.index("function _ovLayoutNow(")
        self.assertIn("_ovRowEls().forEach(function(r){\n    var rc = _ovRowCards(r)", PAGE[i:i + 600])
        i = PAGE.index("function _ovArrangeTools(")
        self.assertIn("perRow.forEach(function(cards){", PAGE[i:PAGE.index("\n}\n", i)])

    def test_a_cards_title_leaves_room_for_its_buttons(self):
        self.assertIn(".soc-card > .card-title, .power-card > .card-title { display: block; align-self: stretch !important;",
                      PAGE)
        self.assertIn(".power-card > .card-title, .ov-power-row .soc-card > .card-title { padding-right: 84px; }", PAGE)

    def test_the_grid_rows_cards_fill_a_dropped_ones_place(self):
        for rule in (".ov-grid-row #mix-card { flex: 1 1 280px;", ".ov-grid-row #carbon-card-wrap { flex: 4 1 340px;",
                     ".ov-grid-row #power-history-card-wrap { flex: 12 1 420px;"):
            self.assertIn(rule, PAGE)

    def test_the_battery_stands_upright_beside_its_gauges(self):
        i = PAGE.index("function buildSocCard(")
        body = PAGE[i:PAGE.index("\n}\n", i)]
        self.assertIn("'<div class=\"battery-icon v\"", body)
        self.assertIn("gaugeSection = '<div class=\"dev-gauges\">' + battSection + _invHtml + pvGauge + tempGauge + capSection + '</div>';", body)
        self.assertIn("fill.style.height = (pct !== null ? pctVal + '%' : '0%');", PAGE)

    def test_the_meter_card_is_titled_and_its_gauge_labelled_as_a_devices(self):
        i = PAGE.index('data-ov-card="power"')
        card = PAGE[i:i + 2500]
        self.assertIn('<div class="card-title">🏠 Meter</div>', card)
        self.assertIn('<div class="dev-gauge-label">⚡ Grid</div>', card)

    def test_every_power_card_expands(self):
        i = PAGE.index('data-ov-card="power"')
        self.assertIn('onclick="_expandPower(this)"', PAGE[i:i + 600])
        i = PAGE.index("function buildSocCard(")
        self.assertIn("onclick=\"_expandPower(this)\"", PAGE[i:PAGE.index("\n}\n", i)])

    def test_the_power_cards_can_be_gauges_only(self):
        self.assertIn('ov-power-row{% if overview_power_compact %} compact{% endif %}"', PAGE)
        self.assertIn(".ov-power-row.compact .td { display: none; }", PAGE)

    def test_unlocked_rows_and_cards_are_dragged(self):
        i = PAGE.index("function _ovArrangeTools(")
        body = PAGE[i:PAGE.index("\n}\n", i)]
        self.assertIn("_ovDragStart(e, 'row', r)", body)
        self.assertIn("_ovDragStart(e, 'card', c)", body)
        self.assertIn("document.addEventListener('pointermove', _ovDragMove);", PAGE)

    def test_a_card_drag_changes_the_order_the_layout_applies(self):
        # the layout rewrites every card's CSS order from _ovCardOrder: a drag that set styles alone was undone
        i = PAGE.index("function _ovDragMove(")
        self.assertIn("_ovCardOrder = all;", PAGE[i:PAGE.index("\n}\n", i)])

    def test_the_padlock(self):
        i = PAGE.index("{% block topbar %}")
        self.assertIn('class="ov-head-btn ov-lock"', PAGE[i:PAGE.index("{% endblock %}", i)])

    def test_the_history_fills_its_card(self):
        self.assertIn(".ov-row .power-history-card .power-history-wrap { flex: 1 1 120px; height: auto;", PAGE)
        i = PAGE.index("var _phCanvas   = document.getElementById('power-history-canvas');")
        self.assertIn("}).observe(_phCanvas.parentElement);", PAGE[i:i + 600])

    def test_the_live_power_pill_sits_beside_its_gauge(self):
        i = PAGE.index('<div class="dev-gauges lp-head">')
        self.assertLess(PAGE.index('<div class="gauge-wrap">', i), PAGE.index('id="power-status" class="lp-pill"', i))

    def test_the_grid_row_holds_mix_carbon_and_history(self):
        i = PAGE.index('data-ov-row="grid"')
        j = PAGE.index("</div><!-- /grid row -->")
        for card in ('id="mix-card"', 'id="carbon-card-wrap"', 'id="power-history-card-wrap"'):
            self.assertIn(card, PAGE[i:j])

    def test_the_power_row_holds_the_gauge_and_the_devices(self):
        i = PAGE.index('data-ov-row="power"')
        j = PAGE.index("</div><!-- /power row -->")
        self.assertIn('class="power-card expandable-card"', PAGE[i:j])
        self.assertIn('id="soc-cards-container"', PAGE[i:j])
        self.assertNotIn('id="mix-card"', PAGE[i:j])

    def test_the_saved_order_applies_before_the_page_paints(self):
        # straight after the rows, not in the scripts block at the foot of the page
        i = PAGE.index("</div><!-- /.live-power-page -->")
        self.assertLess(PAGE.index("_ovApplyRowOrder();", i), PAGE.index("{% block scripts %}"))

    def test_the_cost_cards_can_be_totals_only_on_one_line(self):
        self.assertIn('ov-costs-row{% if overview_costs_compact %} compact{% endif %}"', PAGE)
        self.assertIn(".ov-costs-row.compact .billing-rows { display: none; }", PAGE)
        self.assertIn(".ov-costs-row.compact .billing-card { display: flex; flex-wrap: wrap; align-items: baseline;", PAGE)

    def test_the_old_two_layouts_are_gone(self):
        for old in ("layout-a", "layout-b", "_applyLayout", "gauge-ci-row", "summary-grid"):
            self.assertNotIn(old, PAGE)


@unittest.skipUnless(shutil.which("node"), "node not available")
class ThePure(unittest.TestCase):

    def _js(self, expr):
        def fn(n):
            i = PAGE.index("function %s(" % n)
            return PAGE[i:PAGE.index("\n}\n", i) + 2]
        src = "\n".join(fn(n) for n in ("_ovOrdered", "_ovPerLine", "_ovMovedTo"))
        src += "\nconsole.log(JSON.stringify(%s));" % expr
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(src)
        try:
            r = subprocess.run(["node", fh.name], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(fh.name)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_the_power_row_is_spread_evenly(self):
        # room for 4 at 220 px with 12 px gaps in 1,132 px
        got = self._js("[1,2,3,4,5,6,7,8,9].map(function(n){ return _ovPerLine(n, 1132, 220, 12); })")
        self.assertEqual(got, [1, 2, 3, 4, 3, 3, 4, 4, 3])     # 5 → 3+2, 7 → 4+3, 9 → 3+3+3

    def test_a_narrow_row_is_one_a_line(self):
        self.assertEqual(self._js("_ovPerLine(3, 200, 220, 12)"), 1)

    def test_saved_keys_first_then_the_rest_in_their_own_order(self):
        self.assertEqual(self._js("_ovOrdered(['grid','gone','power','grid'], ['power','charge','grid','agile'])"),
                         ["grid", "power", "charge", "agile"])

    def test_a_dragged_card_takes_the_place_it_is_dropped_on(self):
        self.assertEqual(self._js("_ovMovedTo(['a','b','c','d'], 3, 1)"), ["a", "d", "b", "c"])
        self.assertEqual(self._js("_ovMovedTo(['a','b','c','d'], 0, 2)"), ["b", "c", "a", "d"])

    def test_a_drop_past_the_end_lands_at_the_end(self):
        self.assertEqual(self._js("_ovMovedTo(['a','b'], 0, 9)"), ["b", "a"])


if __name__ == "__main__":
    unittest.main()
