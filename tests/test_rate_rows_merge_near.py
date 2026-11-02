"""
BL-63's rounding residue (29 Oct 2026): near-equal rates are one row.

v4's history holds one tariff band at two precisions — Octopus's published inc rate (0.323092) and
exc × 1.05 (0.32309235) — and blends worked back from a rounded cost (0.290211, 0.290227, …).
Counted apart on the bill card (keyed at 4 dp) and the day panel, they pushed an IOG month past
MAX_RATE_ROWS: the Agile-export user's Nov 2024 import read one "avg of 7 rates" row. Insights' main
tiers come from the segments (P4.2), clustered already; its device tiers (staging's battery, Mar 2026)
and a period with a half-hour lacking segments are keyed on the stored rate at 6 dp. Rates within bill_card.RATE_MERGE_EPS (a unit of the 4-dp rate shown) are now one row,
labelled with its heaviest rate, its kWh and £ summed. Agile's real rates stay apart: the rule is
not chained. The card's sixth row there was 0 kWh at £0 (half-hours with no rate); it is not shown or
counted now, as the day panel already did.

Discriminating: every test here fails on the parent (merge_near_rates is new, and each surface
counted the near-equal rates apart), except the two labelled GUARD.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "web"))
import bill_card as bc                           # noqa: E402
import energy_charts as ec                       # noqa: E402

# a month like the Agile-export user's Nov 2024 import, in figures of its shape: off-peak and peak
# at two precisions each, a mixed half-hour's blends, and an older peak rate
NOV = {0.0700035: 900.0, 0.070003: 0.001, 0.13401: 0.4, 0.134013: 26.0, 0.134015: 0.2,
       0.2467038: 21.0, 0.246704: 0.001, 0.29: 0.06, 0.2902: 0.01, 0.290227: 9.7, 0.290286: 0.007}


def _chans(rate_kwh):
    return {r: {"kwh": k, "cost": round(r * k, 6)} for r, k in rate_kwh.items()}


class TheRule(unittest.TestCase):

    def test_two_precisions_of_one_rate_are_one(self):
        self.assertEqual(bc.merge_near_rates({0.323092: 10.0, 0.32309235: 1.0}),
                         {0.323092: 0.323092, 0.32309235: 0.323092})

    def test_the_heaviest_names_the_row(self):
        self.assertEqual(set(bc.merge_near_rates({0.323092: 1.0, 0.32309235: 10.0}).values()),
                         {0.32309235})

    def test_it_is_not_chained(self):
        """Agile: 0.20000, 0.20008, 0.20016 — each within the gap of the next, not of the first."""
        to = bc.merge_near_rates({0.2: 3.0, 0.20008: 2.0, 0.20016: 1.0})
        self.assertEqual((to[0.20008], to[0.20016]), (0.2, 0.20016))

    def test_four_dp_neighbours_are_one(self):
        """The bill card's keys: 0.290227 and 0.290286 arrive as 0.2902 and 0.2903."""
        self.assertEqual(bc.merge_near_rates({0.2902: 9.7, 0.2903: 0.007})[0.2903], 0.2902)

    def test_the_real_month_keyed_as_the_card_keys_it(self):
        """NOV at billing.py's 4-dp keys is five rows, not an average."""
        keyed = {}
        for r, k in NOV.items():
            keyed[round(r, 4)] = keyed.get(round(r, 4), 0.0) + k
        rows, _tk, _tc = bc.rate_rows(_chans(keyed))
        self.assertEqual([r["rate"] for r in rows], ["0.0700", "0.1340", "0.2467", "0.2900", "0.2902"])

    def test_real_rates_stay_apart(self):
        """Rates a fiftieth of a penny apart are not merged."""
        to = bc.merge_near_rates({0.29: 1.0, 0.2902: 1.0, 0.0549: 1.0})
        self.assertEqual(len(set(to.values())), 3)


class TheBillCard(unittest.TestCase):

    def test_a_month_of_near_equal_rates_is_not_an_average(self):
        rows, tk, tc = bc.rate_rows(_chans(NOV))
        self.assertEqual([r["rate"] for r in rows], ["0.0700", "0.1340", "0.2467", "0.2900", "0.2902"])
        self.assertTrue(all(r["note"] is None for r in rows))

    def test_an_empty_row_is_neither_shown_nor_counted(self):
        """Nov 2024's 31 half-hours with no rate, keyed 0.0: 0 kWh, £0 — the sixth row."""
        rows, _tk, _tc = bc.rate_rows(_chans({0.0: 0.0, 0.07: 9.0, 0.134: 1.0, 0.2467: 2.0, 0.29: 0.1, 0.2902: 1.0}))
        self.assertEqual([r["rate"] for r in rows], ["0.0700", "0.1340", "0.2467", "0.2900", "0.2902"])

    def test_the_total_does_not_move(self):
        """GUARD: the Total was the rows' sum before, folded or not."""
        _rows, tk, tc = bc.rate_rows(_chans(NOV))
        self.assertAlmostEqual(tk, sum(NOV.values()), places=2)
        self.assertAlmostEqual(tc, sum(r * k for r, k in NOV.items()), places=1)

    def test_agile_still_folds(self):
        """GUARD: forty real Agile rates are still one average row."""
        rows, _tk, _tc = bc.rate_rows(_chans({round(0.10 + 0.003 * i, 6): 1.0 for i in range(40)}))
        self.assertEqual((len(rows), rows[0]["note"]), (1, {"avg": 40}))


class TheDayPanel(unittest.TestCase):

    def test_near_equal_rates_are_one_line(self):
        out = ec._collapse_rate_kwh({0.069: 5.0, 0.0689997: 2.0, 0.285844: 1.0, 0.2858436: 0.5})
        self.assertEqual([(round(k, 3), r, n) for k, r, n in out], [(7.0, 0.069, None), (1.5, 0.285844, None)])


class Insights(unittest.TestCase):

    def test_tiers_merge_before_the_fold(self):
        import server
        tiers = [{"rate": r, "kwh": k, "cost": round(r * k, 4), "blocks": 1} for r, k in sorted(NOV.items())]
        out = server._collapse_rate_tiers(tiers)
        self.assertEqual([t["rate"] for t in out], [0.0700035, 0.134013, 0.2467038, 0.29, 0.290227])
        self.assertEqual(sum(t["blocks"] for t in out), len(NOV))


if __name__ == "__main__":
    unittest.main()
