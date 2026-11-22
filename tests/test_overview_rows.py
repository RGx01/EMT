"""
The Overview's rows (docs/history/design/EMT-v5_overview_rows_design.md, 22 Nov 2026, the user): the page is rows of cards —
Power, Grid and carbon, Smart Charging, Agile — the user orders the rows and the Power row's cards, the Power row is
spread evenly, and the cost cards can be totals only. The orders are saved beside the hidden cards
(`overview_rows`, `overview_power_order`), the compact switch as `overview_costs_compact`.

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
                          ("overview_power_order", ["device-sub_meter_2", "power"])):
            with self.subTest(name):
                self.assertEqual(self._post({name: val}).status_code, 200)
                want = list(dict.fromkeys(val))
                self.assertEqual(self.store.saved[name], want)
                self.assertEqual(self.client.get("/api/settings").get_json()[name], want)

    def test_they_start_empty(self):
        got = self.client.get("/api/settings").get_json()
        self.assertEqual((got["overview_rows"], got["overview_power_order"], got["overview_costs_compact"]),
                         ([], [], False))

    def test_a_bad_key_is_refused(self):
        for name in ("overview_rows", "overview_power_order"):
            for bad in (['a"]{}'], "grid", [3]):
                with self.subTest(name=name, bad=str(bad)):
                    self.assertEqual(self._post({name: bad}).status_code, 400)
                    self.assertNotIn(name, self.store.saved)

    def test_compact_costs_saves(self):
        self.assertEqual(self._post({"overview_costs_compact": True}).status_code, 200)
        self.assertIs(self.store.saved["overview_costs_compact"], True)


class ThePage(unittest.TestCase):

    def test_the_rows(self):
        self.assertEqual(re.findall(r'data-ov-row="([a-z]+)"', PAGE), ["power", "charge", "grid", "agile"])

    def test_the_grid_row_holds_mix_carbon_and_history(self):
        i = PAGE.index('data-ov-row="grid"')
        j = PAGE.index("</div><!-- /grid row -->")
        for card in ('id="mix-card"', 'id="carbon-card-wrap"', 'id="power-history-card-wrap"'):
            self.assertIn(card, PAGE[i:j])

    def test_the_power_row_holds_the_gauge_and_the_devices(self):
        i = PAGE.index('data-ov-row="power"')
        j = PAGE.index("</div><!-- /power row -->")
        self.assertIn('class="power-card"', PAGE[i:j])
        self.assertIn('id="soc-cards-container"', PAGE[i:j])
        self.assertNotIn('id="mix-card"', PAGE[i:j])

    def test_the_saved_order_applies_before_the_page_paints(self):
        # straight after the rows, not in the scripts block at the foot of the page
        i = PAGE.index("</div><!-- /.live-power-page -->")
        self.assertLess(PAGE.index("_ovApplyRowOrder();", i), PAGE.index("{% block scripts %}"))

    def test_the_cost_cards_can_be_totals_only(self):
        self.assertIn('{% if overview_costs_compact %} compact{% endif %}" id="ov-costs"', PAGE)
        self.assertIn(".billing-grid.compact .billing-rows { display: none; }", PAGE)

    def test_the_old_two_layouts_are_gone(self):
        for old in ("layout-a", "layout-b", "_applyLayout", "gauge-ci-row", "summary-grid"):
            self.assertNotIn(old, PAGE)


@unittest.skipUnless(shutil.which("node"), "node not available")
class ThePure(unittest.TestCase):

    def _js(self, expr):
        def fn(n):
            i = PAGE.index("function %s(" % n)
            return PAGE[i:PAGE.index("\n}\n", i) + 2]
        src = "\n".join(fn(n) for n in ("_ovOrdered", "_ovPerLine", "_ovMoved"))
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

    def test_a_move_skips_what_is_not_shown(self):
        self.assertEqual(self._js("_ovMoved(['a','b','c'], [true,false,true], 2, -1)"), ["c", "b", "a"])

    def test_a_move_past_the_end_changes_nothing(self):
        self.assertEqual(self._js("_ovMoved(['a','b'], [true,true], 0, -1)"), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
