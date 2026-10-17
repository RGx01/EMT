"""
The uncapped seam labels its legs from its rate (17 Oct 2026).

On an uncapped Intelligent tariff, `iog_cap.compute_iog_split` prices the whole dispatched slot at
the overlay rate and carves the EV slice for the display. It labelled every leg off-peak, whatever
the rate, so a dispatch billed at the peak rate (a bump outside the window) was stored as EV
`off_peak` at the peak price. On the soak, 18 imported July half-hours read that way.

Now the legs take their band from the rate: off-peak where it is nearer the day's off-peak rate,
else EV peak and house day (BL-58b's rule). The cost is unchanged — every leg is at the one rate.

Discriminating (fail on the parent): test_a_peak_rated_dispatch_is_labelled_peak,
test_the_classification_says_peak_too. The rest are guards.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import iog_cap                                   # noqa: E402

OFF, PEAK = 0.05493, 0.32309235
S, E = "2026-07-07T05:00:00", "2026-07-07T05:30:00"


class _Sched:
    def __init__(self, bounds=(5.493, 32.309235)):
        self.bounds = bounds

    def is_empty(self): return False
    def day_rate_bounds(self, ts): return self.bounds
    def is_off_peak(self, ts): return False


def _split(rate, kwh=2.0, ev=1.5, sched=None):
    return iog_cap.compute_iog_split(S, E, chosen_kwh=kwh, ev_kwh=ev, overlay_rate=rate,
                                     is_boost=False, capped=False,
                                     import_sched=sched or _Sched())


def _bands(res):
    return {(a, b) for (_k, _r, b, a) in res["segments"]}


class TheLabels(unittest.TestCase):

    def test_a_peak_rated_dispatch_is_labelled_peak(self):
        self.assertEqual(_bands(_split(PEAK)), {("ev", "peak"), ("house", "day")})

    def test_the_classification_says_peak_too(self):
        """`_apply_iog_split` stores the EV and house bands from the classification."""
        cls = _split(PEAK)["classification"]
        self.assertEqual((cls["ev"], cls["house"]), ("peak", "day"))

    def test_an_off_peak_rated_dispatch_stays_off_peak(self):
        """GUARD: the ordinary smart charge."""
        self.assertEqual(_bands(_split(OFF)), {("ev", "off_peak"), ("house", "off_peak")})

    def test_a_flat_day_keeps_the_off_peak_labels(self):
        """GUARD: no two rates to tell apart."""
        res = _split(0.25, sched=_Sched((25.0, 25.0)))
        self.assertEqual(_bands(res), {("ev", "off_peak"), ("house", "off_peak")})

    def test_an_uncovered_day_keeps_the_off_peak_labels(self):
        """GUARD."""
        res = _split(PEAK, sched=_Sched((None, None)))
        self.assertEqual(_bands(res), {("ev", "off_peak"), ("house", "off_peak")})


class TheCost(unittest.TestCase):
    """GUARD: only the labels move — the uncapped seam's totals stay the overlay's."""

    def test_the_cost_is_the_overlay_rate(self):
        for rate in (OFF, PEAK):
            res = _split(rate)
            self.assertAlmostEqual(res["imp_cost"], round(2.0 * rate, 6), places=6)
            self.assertAlmostEqual(res["imp_cost_ev"], round(1.5 * rate, 6), places=6)
            self.assertTrue(all(abs(r - rate) < 1e-12 for (_k, r, _b, _a) in res["segments"]))


if __name__ == "__main__":
    unittest.main()
