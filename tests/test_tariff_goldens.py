"""
Golden outputs per tariff shape — step 5 must not move them (5 Oct 2026).

Every real database on hand is Intelligent Octopus Go, so parity on real data proves only that IOG
accounts are unchanged. `tests/tariff_fixtures.py` builds the other shapes — flat, Economy 7, Go,
Agile (zero and negative slots included), each with and without an EV charger and a battery, across
the 1 Oct 2026 VAT change — and `.build/tariff_goldens.py` records what every data surface answers
on each: bills, Usage Stats, the heatmap, Insights, the period picker. This test asserts the answers
are unchanged.

When a change is MEANT to move an answer, regenerate with `.build/tariff_goldens.py --update` and
say why in the commit; `--dump DIR` on the parent and on the change gives two trees to diff.

GUARD: today's answers are the goldens, so this passes on the parent. `TheGoldensSeeABreak` shows
they are not vacuous: a bill reader mis-reading the stored cost by 1% moves the bills.
"""
import json
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, ".build"))
sys.path.insert(0, _ROOT)

import tariff_goldens as G                        # noqa: E402


class EveryTariff(unittest.TestCase):

    def test_every_fixture_has_goldens(self):
        for tariff, devices in G.fixtures():
            with self.subTest(fixture=G.name(tariff, devices)):
                self.assertTrue(os.path.exists(G.golden_path(tariff, devices)))

    def test_no_answer_moved(self):
        for tariff, devices in G.fixtures():
            want = json.load(open(G.golden_path(tariff, devices)))
            got = G.digest(G.capture(tariff, devices))
            self.assertEqual(sorted(got), sorted(want), "the set of requests changed")
            for label in got:
                with self.subTest(fixture=G.name(tariff, devices), request=label):
                    self.assertEqual(got[label]["status"], 200)
                    self.assertEqual(got[label], want[label],
                                     "moved — if meant, run .build/tariff_goldens.py --update "
                                     "and say why; --dump DIR to diff")


class TheGoldensSeeABreak(unittest.TestCase):

    def test_a_reader_regression_moves_the_bills(self):
        """A bill reader that mis-reads the stored cost by 1% moves the bills. (A VAT-calendar
        break would not: the fixtures store ex-VAT, so the bill never consults the calendar —
        which is why the break is made at the reader, where step 5 works.)"""
        import block_store
        import store_types
        want = json.load(open(G.golden_path("flat", False)))
        # The routes read through ReadStore, whose methods are BlockStore's copied at import:
        # patching BlockStore would not reach them, and the check would pass vacuously.
        real = store_types.ReadStore.get_bill_rows
        i = block_store.BlockStore.BILL_ROW_COLUMNS.index("imp_cost")

        def misread(self, a, b):
            rows, segs = real(self, a, b)
            return [tuple(v * 1.01 if j == i and v else v for j, v in enumerate(r))
                    for r in rows], segs
        with mock.patch.object(store_types.ReadStore, "get_bill_rows", misread):
            got = G.digest(G.capture("flat", False))
        self.assertNotEqual(got["Bill: October"], want["Bill: October"])
        self.assertNotEqual(got["Bill: 2026"], want["Bill: 2026"])


if __name__ == "__main__":
    unittest.main()
