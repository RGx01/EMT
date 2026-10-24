"""
One bill reader — bill and split paths design §1.3 and §6 (a), consolidation PR 2 (16 Oct 2026).

The live settled-cost pass, the history drain, the VAT resync (#505) and the range re-fetch each
parsed Octopus's answer themselves — the breakdown in three copies that disagreed on a zero answer —
and chose breakdown or cost-only three ways. Now: `bill_kind_at` chooses per half-hour,
`_fetch_bill_nodes` fetches, `parse_bill` reads, one zero rule.

Discriminating (fail on the parent: none of these functions exists there), except the guards named.
"""
import ast
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import engine                                   # noqa: E402

OFF = 0.05493


def _bd(home, ev, cost_home, cost_ev, bands=("off_peak", None)):
    return {"home_kwh": home, "ev_kwh": ev, "home_cost": cost_home, "ev_cost": cost_ev,
            "home_rate": OFF, "ev_rate": OFF if ev else None,
            "home_rate_exc": OFF, "ev_rate_exc": OFF if ev else None,
            "home_band": bands[0], "ev_band": bands[1]}


class TheParser(unittest.TestCase):

    def test_a_breakdown(self):
        row = engine.parse_bill(_bd(1.0, 2.0, 0.05493, 0.10986, ("off_peak", "off_peak")),
                                "breakdown", 3.0)
        self.assertEqual((row["cost_incl"], row["label"], row["kwh"]), (0.164790, "OFF_PEAK", 3.0))
        self.assertEqual(row["breakdown"], (1.0, OFF, 2.0, OFF))

    def test_mixed_bands_are_mixed(self):
        row = engine.parse_bill(_bd(1.0, 2.0, 0.32309, 0.10986, ("peak", "off_peak")),
                                "breakdown", 3.0)
        self.assertEqual(row["label"], "mixed")

    def test_a_cost_answer_has_no_breakdown(self):
        row = engine.parse_bill({"cost_incl": 0.2, "cost_excl": 0.19, "off_peak": False,
                                 "kwh": 0.62}, "cost", 0.62)
        self.assertEqual((row["label"], row.get("breakdown")), ("STANDARD_RATE", None))

    def test_no_answer(self):
        self.assertIsNone(engine.parse_bill(None, "breakdown", 1.0))
        self.assertIsNone(engine.parse_bill({"cost_incl": None, "kwh": 1.0}, "cost", 1.0))


class TheZeroRule(unittest.TestCase):
    """§6 (a): zero is the bill only where EMT holds zero too."""

    def test_zero_against_zero_is_cached(self):
        self.assertEqual(engine.parse_bill(_bd(0, 0, 0, 0), "breakdown", 0.0), {"zero": True})
        self.assertEqual(engine.parse_bill({"cost_incl": 0.0, "kwh": 0.0}, "cost", 0.0),
                         {"zero": True})

    def test_zero_against_kwh_is_not_an_answer(self):
        self.assertIsNone(engine.parse_bill(_bd(0, 0, 0, 0), "breakdown", 1.2))
        self.assertIsNone(engine.parse_bill({"cost_incl": 0.0, "kwh": 0.0, "start": "x"},
                                            "cost", 1.2))


class TheSource(unittest.TestCase):

    def test_per_half_hour_by_its_agreement(self):
        save = engine._kraken_discovery
        try:
            engine._kraken_discovery = {"import": {"tariff_code": "E-1R-IOG-SMB-X-B", "agreements": [
                {"tariff_code": "E-1R-INTELLI-VAR-B", "valid_from": "2025-01-01T00:00:00Z",
                 "valid_to": "2026-08-25T23:00:00Z"},
                {"tariff_code": "E-1R-IOG-SMB-X-B", "valid_from": "2026-08-25T23:00:00Z",
                 "valid_to": None}]}}
            self.assertEqual(engine.bill_kind_at("2026-08-24T04:30:00"), "cost")
            self.assertEqual(engine.bill_kind_at("2026-09-01T02:00:00"), "breakdown")
        finally:
            engine._kraken_discovery = save


class EveryPathUsesIt(unittest.TestCase):
    """No path parses or fetches a bill on its own any more."""

    SRC = open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read()

    def _fn(self, name):
        tree = ast.parse(self.SRC)
        return next(ast.get_source_segment(self.SRC, n) for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)

    def test_one_parser(self):
        self.assertEqual(self.SRC.count('_n(node.get("home_cost")) + _n(node.get("ev_cost"))'), 1)
        self.assertNotIn("def _vat_resync_row", self.SRC)

    def test_each_path_fetches_and_parses_through_it(self):
        # (the live pass was a third, until the one asker took it in engine step 3c)
        for name in ("run_measured_history_drain", "_refetch_octopus"):
            body = self._fn(name)
            if name == "run_measured_history_drain":       # it parses in its page writer
                body += self._fn("_ask_write_page")
            self.assertIn("_fetch_bill_nodes(", body, name)
            self.assertIn("parse_bill(", body, name)
            self.assertNotIn("recover_device_breakdown(", body, name)


if __name__ == "__main__":
    unittest.main()
