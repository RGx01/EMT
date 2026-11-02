"""
The detail panel names an import's provenance (28 Oct 2026, the user).

An imported half-hour's kWh read "DCC settled" — the route counts it settled, and no DCC read is
kept for it — and its IMPORTED bill read "Octopus, fetched 27 Oct 13:22", the time of the import
(staging: 32,685 IMPORTED bills, every one stamped with the import's time). The route now says where
each came from (`origin`), and the panel says:

- kWh: "Octopus's, from the API import" / "from the CSV import"; a DCC read stays "DCC settled";
- bill: "the API import's cost, imported …" / "the CSV import's cost, imported …"; Octopus's stays
  "Octopus, fetched …".

Discriminating: every test here fails on the parent (`_origins` is new; the live case by name only).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import test_server  # noqa: F401,E402  (stubs, imports server)
import server       # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _d(source=None, api=None, label=None):
    return {"source": source, "imp_kwh_api": api, "bill_label": label}


class TheOrigins(unittest.TestCase):

    def test_an_api_imports(self):
        self.assertEqual(server._origins(_d("imported_api", label="IMPORTED")),
                         {"kwh": "api_import", "bill": "api_import"})

    def test_a_csv_imports(self):
        self.assertEqual(server._origins(_d("imported_csv", label="IMPORTED")),
                         {"kwh": "csv_import", "bill": "csv_import"})

    def test_octopuss_bill_on_imported_history_is_octopuss(self):
        """What #315 fetches for API history: the kWh still the import's, the bill Octopus's."""
        self.assertEqual(server._origins(_d("imported_api", label="OFF_PEAK")),
                         {"kwh": "api_import", "bill": "octopus"})

    def test_live_history(self):
        self.assertEqual(server._origins(_d(None, api=1.2, label="STANDARD_RATE")),
                         {"kwh": "dcc", "bill": "octopus"})
        self.assertEqual(server._origins(_d(None))["kwh"], "live")


class TheRoute(unittest.TestCase):

    def _fn(self, path, start, end):
        with open(os.path.join(ROOT, *path), encoding="utf-8") as f:
            src = f.read()
        fn = src[src.index(start):]
        return fn[:fn.index(end)]

    def test_it_sends_both(self):
        fn = self._fn(("web", "server.py"), "def api_block_settlement", "\n\n\n")
        self.assertIn('"origin": _origins(d)["kwh"]}', fn)
        self.assertIn('"origin": _origins(d)["bill"]', fn)

    def test_the_panel_words_them(self):
        fn = self._fn(("web", "templates", "heatmap_module.html"),
                      "function renderPanel(d, label, more) {", "head('Import');")
        self.assertIn("k.origin === 'api_import' ? 'Octopus\\u2019s, from the API import'", fn)
        self.assertIn("k.origin === 'csv_import' ? 'from the CSV import'", fn)
        fn = self._fn(("web", "templates", "heatmap_module.html"),
                      "head('Import');", "var ev = 0")
        self.assertIn("ORIGIN[b.origin] + '\\u2019s cost, imported '", fn)


if __name__ == "__main__":
    unittest.main()
