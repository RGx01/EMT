"""
The detail panel says an API-imported half-hour's bill is v4's price (28 Oct 2026, the user).

The v4 import's IMPORTED bill on API history is v4's arithmetic — Octopus's kWh × the tariff's rate
at Octopus's band — and Octopus never sent a cost for it. The panel read "the API import's cost,
imported 28 Oct at 15:42 · stored £0.4362 ✓", which reads as Octopus's cost checked, and the rate
line read "settled". (On staging the method matched Octopus's own cost on all 11,600 half-hours the
asker could fetch, but a half-hour past Octopus's two years cannot be checked.) Now:

- the bill note: "v4's price, <kWh> × <rate> at Octopus's band", then "until Octopus sends its own
  cost" inside the two years, or "past Octopus's two years, so it can't be checked"; no check mark
  (the stored cost is that bill);
- the rate line: "v4's price", not "settled";
- past the two years, no Re-fetch from Octopus (the route's `reachable`).

The CSV import's wording is unchanged.

Discriminating: every test here fails on the parent, but those labelled GUARD.
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import test_settlement_panel as P                # noqa: E402  (the route's store and request)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _panel():
    with open(os.path.join(ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8") as f:
        hm = f.read()
    fn = hm[hm.index("function renderPanel(d, label, more) {"):]
    return fn[:fn.index("\n  }\n  // a missing half-hour")]


class TheRoute(unittest.TestCase):

    def _reachable(self, floor=None):
        server = sys.modules["server"]
        with mock.patch.object(server, "_reach_floor", return_value=floor) if floor \
                else mock.patch.object(server, "_reach_floor", wraps=server._reach_floor):
            r = P.TheRoute._get(self, P.S)
        return json.loads(r.data)["reachable"]

    def test_inside_the_two_years(self):
        self.assertIs(self._reachable(), True)

    def test_past_them(self):
        self.assertIs(self._reachable("2027-01-01T00:00:00"), False)


class ThePanel(unittest.TestCase):

    def test_the_bill_is_v4s_price(self):
        fn = _panel()
        self.assertIn("if (b && b.origin === 'api_import') {", fn)
        self.assertIn("'v4\\u2019s price, ' + esc(kwh(b.kwh)) + ' \\u00d7 ' + pence(c.rate) +", fn)
        self.assertIn("'past Octopus\\u2019s two years, so it can\\u2019t be checked'", fn)
        self.assertIn("'until Octopus sends its own cost'", fn)

    def test_no_check_mark_on_it(self):
        fn = _panel()
        branch = fn[fn.index("if (b && b.origin === 'api_import') {"):fn.index("} else if (b) {")]
        self.assertNotIn("\\u2713", branch)

    def test_the_rate_line(self):
        self.assertIn("var rsrc = b && b.origin === 'api_import' ? 'v4\\u2019s price'", _panel())

    def test_no_refetch_past_the_two_years(self):
        self.assertIn("(d.reachable === false ? '' : '<button class=\"hm-btn\" data-act=\"refetch\">"
                      "Re-fetch from Octopus</button>')", _panel())

    def test_GUARD_the_csv_wording_stays(self):
        self.assertIn("csv_import: 'the CSV import'", _panel())
        self.assertIn("ORIGIN[b.origin] + '\\u2019s cost, imported '", _panel())


if __name__ == "__main__":
    unittest.main()
