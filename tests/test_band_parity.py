"""
Engine step 2a: the band, predicted and settled (EMT-v5_engine_step2_import_bills_design.md §4, 23 Oct
2026) — `settled_band`, the buckets' band, the band note, the lifecycle and the overlay's quiet read.
Step 2a's band parity report, which set this rule beside the stored cost, went in engine step 4e (the
settle parity report compares every part); its tests went with it.

Discriminating: every test here failed on step 2a's parent (the rule was new).
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_engine  # noqa: F401  — installs stubs, imports engine
import engine
from block_store import BlockStore

LO, HI = 5.493, 32.3092                     # pence: the off-peak and day rates


class _Sched:
    def is_empty(self): return False
    def day_rate_bounds(self, ts): return (LO, HI)
    def off_peak_rate_near(self, ts): return LO
    def resolve(self, ts):                  # 23:30-05:30 off-peak by the clock
        hh = ts[11:16]
        return LO if (hh >= "23:30" or hh < "05:30") else HI


class _Base(unittest.TestCase):
    def setUp(self):
        self.p = [mock.patch.object(engine, "_kraken_rate_schedules", {"import": _Sched()}),
                  mock.patch.object(engine, "tariff_kind_at", lambda bs: "intelligent"),
                  mock.patch.object(engine, "split_at", lambda bs: False)]
        for x in self.p:
            x.start()
        self.dispatched = set()
        self.p2 = mock.patch.object(engine, "_dispatch_overlay_rate",
                                    lambda ch, bs, base, kwh, quiet=False:
                                    round(LO / 100, 6) if bs in self.dispatched else base)
        self.p2.start()

    def tearDown(self):
        self.p2.stop()
        for x in self.p:
            x.stop()


class TheSettledBand(unittest.TestCase):

    def test_from_the_label(self):
        self.assertEqual(engine.settled_band("x", "OFF_PEAK"), "off_peak")
        self.assertEqual(engine.settled_band("x", "STANDARD_RATE"), "peak")

    def test_none_and_zero_settle_nothing(self):
        self.assertIsNone(engine.settled_band("x", None))
        self.assertIsNone(engine.settled_band("x", "ZERO"))


class TheBucketsBand(_Base):
    """2a's first report (23 Oct 2026): on IOG-SMB the summary label is not the band — the nights of
    27–28 Sep 2026 read STANDARD_RATE on half-hours whose only bucket was at the off-peak rate."""

    def test_the_carrying_bucket_wins_over_the_label(self):
        bd = {"home_kwh": 2.89, "home_rate": round(LO / 100, 6), "ev_kwh": 0.0, "ev_rate": None}
        self.assertEqual(engine.settled_band("2026-09-27T01:30:00", "STANDARD_RATE", bd), "off_peak")

    def test_both_sides_at_one_rate_is_that_band(self):
        bd = {"home_kwh": 0.4, "home_rate": round(HI / 100, 6), "ev_kwh": 3.2, "ev_rate": round(HI / 100, 6)}
        self.assertEqual(engine.settled_band("x", "OFF_PEAK", bd), "peak")

    def test_two_rates_is_mixed(self):
        bd = {"home_kwh": 0.4, "home_rate": round(HI / 100, 6), "ev_kwh": 3.2, "ev_rate": round(LO / 100, 6)}
        self.assertEqual(engine.settled_band("x", "OFF_PEAK", bd), "mixed")

    def test_an_smb_label_without_the_buckets_settles_nothing(self):
        """2a's report on dev (23 Oct 2026): IOG-SMB night half-hours labelled STANDARD_RATE with no
        breakdown cached read peak, and the rule would have written peak over Octopus's off-peak cost."""
        with mock.patch.object(engine, "split_at", lambda bs: True):
            self.assertIsNone(engine.settled_band("2026-08-27T02:30:00", "STANDARD_RATE", None))
            bd = {"home_kwh": 2.9, "home_rate": round(LO / 100, 6), "ev_kwh": 0.0, "ev_rate": None}
            self.assertEqual(engine.settled_band("2026-08-27T02:30:00", "STANDARD_RATE", bd), "off_peak")

    def test_no_breakdown_reads_the_label(self):
        """The earlier tariffs' bills carry only the label (fails on the parent only for the new
        `breakdown` argument)."""
        self.assertEqual(engine.settled_band("x", "OFF_PEAK", None), "off_peak")


class TheBandNote(_Base):
    """The detail panel's note (engine step 2b, the user 23 Oct 2026): the cell stays green,
    the panel says where Octopus's band is not its rules'."""

    def setUp(self):
        super().setUp()
        self.st = BlockStore(":memory:")
        self.st.record_dispatch_history("2026-07-07T22:30:00", "completed", provider="Myenergi",
                                        source="unknown", energy_kwh=-3.0)
        self.p3 = mock.patch.object(engine, "_reader", lambda: self.st)
        self.p3.start()

    def tearDown(self):
        self.p3.stop()
        super().tearDown()

    def test_billed_against_the_rules_is_noted(self):
        self.dispatched.add("2026-09-06T19:00:00")
        n = engine.band_note("2026-09-06T19:00:00", 0.17, "STANDARD_RATE")
        self.assertEqual(n, {"settled": "peak", "predicted": "off_peak", "before_record": False,
                             "disagrees": True})

    def test_agreeing_is_not(self):
        n = engine.band_note("2026-09-06T19:00:00", 0.17, "STANDARD_RATE")
        self.assertEqual(n["disagrees"], False)

    def test_before_the_dispatch_record_nothing_disagrees(self):
        """Imported history's evening charge, billed off-peak: EMT had no dispatch record to
        predict it from, so the panel says nothing (staging, 23 Oct 2026, after the re-import)."""
        n = engine.band_note("2025-03-06T19:30:00", 3.6, "OFF_PEAK", live=False)
        self.assertEqual((n["predicted"], n["before_record"], n["disagrees"]), ("peak", True, False))

    def test_a_published_tariff_has_none(self):
        with mock.patch.object(engine, "tariff_kind_at", lambda bs: "published"):
            self.assertIsNone(engine.band_note("2026-09-06T19:00:00", 0.17, "STANDARD_RATE"))

class TheLifecycle(unittest.TestCase):
    """The prediction's second stage (23 Oct 2026, the user): on a live half-hour the reconcile's
    verdict on the dispatch's lifecycle. 16 Aug 2026 13:00 BST: a completed dispatch of 0.23 kWh,
    no `started` — peak by EMT's rules, as Octopus billed it; the first stage alone read off-peak."""

    BS = "2026-08-16T12:00:00"

    def setUp(self):
        self.st = BlockStore(":memory:")
        self.p = [mock.patch.object(engine, "_kraken_rate_schedules", {"import": _Sched()}),
                  mock.patch.object(engine, "_store", self.st),
                  mock.patch.object(engine, "_reader", lambda: self.st),
                  mock.patch.object(engine, "_dispatch_overlay_rate",
                                    lambda ch, bs, base, kwh, quiet=False: round(LO / 100, 6))]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def _hist(self, kind, kwh=None, slot=None):
        self.st.record_dispatch_history(slot or self.BS, kind, provider="Myenergi", source="unknown",
                                        energy_kwh=kwh)

    def test_a_completed_only_tail_on_a_live_half_hour_is_peak(self):
        self._hist("completed", -0.23)
        self.assertEqual(engine.predicted_band(self.BS, 1.557), "peak")

    def test_imported_history_keeps_the_first_stage(self):
        """The reconcile skips imported history, so the prediction does (fails on the parent only
        for the new `live` argument)."""
        self._hist("completed", -0.23)
        self.assertEqual(engine.predicted_band(self.BS, 1.557, live=False), "off_peak")

    def test_a_started_charge_stays_off_peak(self):
        """GUARD: a genuine smart charge — planned, started, completed."""
        for k, e in (("planned", -3.3), ("started", None), ("completed", -3.2)):
            self._hist(k, e)
        self.assertEqual(engine.predicted_band(self.BS, 3.4), "off_peak")

    def test_inside_the_night_window_nothing_is_decided(self):
        """GUARD: the clock's off-peak needs no dispatch (the reconcile skips it too)."""
        bs = "2026-08-16T01:00:00"
        self._hist("completed", -0.05, slot=bs)
        self.assertEqual(engine.predicted_band(bs, 0.3), "off_peak")


class TheOverlayQuiet(unittest.TestCase):

    def test_the_seam_reads_through_the_reader(self):
        """A request thread may not touch the engine's connection (A §5.5): the seam reads the
        dispatch slot through `_reader()`, for the detail panel's band note."""
        import inspect
        src = inspect.getsource(engine._dispatch_overlay_rate)
        self.assertIn("_reader().get_dispatch_slot(", src)
        self.assertNotIn("_store.get_dispatch_slot(", src)


    def test_the_seam_takes_quiet(self):
        import inspect
        self.assertIn("quiet", inspect.signature(engine._dispatch_overlay_rate).parameters)


if __name__ == "__main__":
    unittest.main()
