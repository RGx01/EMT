"""
`billing.py` — deconstruction step 2, and the guard that keeps it a leaf.

EMT-v5_architecture §3: *rescue, then demolish.* `energy_charts.py` is 79%
presentation and dies; these ~900 lines are the arithmetic every surface has to
agree on. This file asserts the three properties that make the extraction worth
anything:

  1. it is a MOVE — the same function objects, reachable from either module;
  2. the dependency runs ONE WAY — presentation imports domain, never the
     reverse;
  3. `billing` is a LEAF — stdlib only, and in particular no `block_store`.

(3) is the one that matters for C. `calculate_billing_summary_for_period` takes a
`store` as an argument and calls methods on it, rather than importing storage and
going to find it. That is what lets the API layer above be the only thing that
knows where data comes from — and it is the property most easily lost by someone
adding "just one" import to save passing an argument.
"""
import ast
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import billing                                   # noqa: E402
import energy_charts                              # noqa: E402

_MOVED = ("calculate_billing_summary_for_period",
          "get_billing_periods_from_config_periods",
          "get_billing_periods_from_config_history",
          "get_all_billing_periods", "get_all_calmonth_periods",
          "get_all_quarter_periods", "get_all_year_periods",
          "bill_slots_from_blocks", "octopus_bill_total", "bankers_round",
          "_parse_block_start", "_utc_to_local")


class ItIsAMove(unittest.TestCase):

    def test_every_name_is_the_same_object_from_either_module(self):
        """A re-export, not a reimplementation. Two copies of billing arithmetic
        is the two-sources-of-truth failure the whole programme is about."""
        for name in _MOVED:
            self.assertTrue(hasattr(billing, name), "billing lacks %s" % name)
            self.assertTrue(hasattr(energy_charts, name),
                            "energy_charts stopped re-exporting %s" % name)
            self.assertIs(getattr(energy_charts, name), getattr(billing, name),
                          "%s is a different object via each module" % name)

    def test_the_arithmetic_emits_no_markup(self):
        """The measurement that corrected §3: these return values, not HTML."""
        import inspect
        for name in ("calculate_billing_summary_for_period",
                     "get_billing_periods_from_config_periods",
                     "get_billing_periods_from_config_history"):
            src = inspect.getsource(getattr(billing, name))
            for tag in ("<div", "<table", "<tr", "<td", "<span", "<script"):
                self.assertNotIn(tag, src, "%s emits markup" % name)


class TheDependencyRunsOneWay(unittest.TestCase):

    def test_billing_does_not_import_energy_charts(self):
        tree = ast.parse(open(os.path.join(_ROOT, "billing.py"), encoding="utf-8").read())
        imported = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imported |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                imported.add(n.module.split(".")[0])
        self.assertNotIn("energy_charts", imported,
                         "the domain must not import the presentation")

    def test_energy_charts_imports_billing(self):
        tree = ast.parse(open(os.path.join(_ROOT, "energy_charts.py"), encoding="utf-8").read())
        froms = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertIn("billing", froms)


class BillingIsALeaf(unittest.TestCase):

    def test_stdlib_only(self):
        """No project module — and NO `block_store`. The store arrives as an
        argument; the domain never goes looking for it.

        Checked against `sys.stdlib_module_names` rather than a hand-written
        allowlist: the first version of this test listed the stdlib modules it
        expected and immediately failed on `calendar`, `logging` and `zoneinfo`,
        which the moved functions import locally. An allowlist of the standard
        library is a list that is always slightly wrong.
        """
        tree = ast.parse(open(os.path.join(_ROOT, "billing.py"), encoding="utf-8").read())
        imported = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imported |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                imported.add(n.module.split(".")[0])
        stray = sorted(imported - set(sys.stdlib_module_names))
        self.assertEqual(stray, [],
                         "billing.py gained a non-stdlib import: %s" % stray)
        self.assertNotIn("block_store", imported)

    def test_the_store_is_a_parameter(self):
        """Stated as a signature check, because the cheap way to lose the leaf
        property is to import storage rather than accept it."""
        import inspect
        sig = inspect.signature(billing.calculate_billing_summary_for_period)
        self.assertIn("store", sig.parameters)


class NothingWasLostInTheMove(unittest.TestCase):

    def test_energy_charts_kept_its_presentation(self):
        """The move took the domain and left the drawing. If a chart generator
        went with it, the split was made in the wrong place."""
        for name in ("generate_daily_import_export_charts", "generate_net_heatmap",
                     "build_day_chart_html", "render_billing_summary"):
            self.assertTrue(hasattr(energy_charts, name),
                            "energy_charts lost %s" % name)
            self.assertFalse(hasattr(billing, name),
                             "billing picked up presentation: %s" % name)

    def test_bankers_round_still_rounds_half_to_even(self):
        """One behavioural spot-check, so 'it imports' is not the only evidence
        the code arrived intact."""
        self.assertEqual(billing.bankers_round(2.5, 0), 2)
        self.assertEqual(billing.bankers_round(3.5, 0), 4)


if __name__ == "__main__":
    unittest.main()
