"""
Engine step 2a: the band, predicted and settled, beside today's cost (EMT-v5_engine_step2_import_bills_
design.md §4, 23 Oct 2026). Read-only: a report of what the step 2 rule — cost = kWh × the band's rate —
gives against what is stored. Billed wins where it contradicts the prediction; counted apart.

Discriminating: every test here fails on the parent (the report is new).
"""
import asyncio
import json
import os
import sys
import tempfile
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


def _fact(bs="2026-09-06T19:00:00", kwh=3.6, cost=None, rate=None, label=None, bill=None, **kw):
    f = {"block_start": bs, "imp_kwh": kwh, "imp_cost": cost, "imp_rate": rate, "bill_label": label,
         "bill_cost": bill, "rate_source": None, "rate_corrected": 0}
    f.update(kw)
    return f


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

    def test_the_row_leaves_it_unsettled(self):
        with mock.patch.object(engine, "split_at", lambda bs: True):
            r = engine.band_parity_row(_fact(bs="2026-08-27T02:30:00", kwh=2.898,
                                             cost=round(2.898 * LO / 100, 6), label="STANDARD_RATE"))
        self.assertEqual((r["status"], r["settled"]), ("unsettled", None))

    def test_no_breakdown_reads_the_label(self):
        """The earlier tariffs' bills carry only the label (fails on the parent only for the new
        `breakdown` argument)."""
        self.assertEqual(engine.settled_band("x", "OFF_PEAK", None), "off_peak")


class TheRow(_Base):

    def test_a_bill_settled_half_hour_is_the_same(self):
        """The evidence of 23 Oct 2026: Octopus's cost is kWh × the band's rate."""
        self.dispatched.add("2026-09-06T19:00:00")
        r = engine.band_parity_row(_fact(cost=round(3.6 * LO / 100, 6), label="OFF_PEAK"))
        self.assertEqual((r["status"], r["settled"], r["predicted"], r["disagrees"]),
                         ("same", "off_peak", "off_peak", False))

    def test_a_stored_cost_off_the_band_changes(self):
        """5 Mar 2025's kind, the other way: the bill's band is off-peak, the stored cost peak."""
        r = engine.band_parity_row(_fact(cost=round(3.6 * HI / 100, 6), label="OFF_PEAK"))
        self.assertEqual(r["status"], "changes")
        self.assertAlmostEqual(r["cost"], 3.6 * LO / 100, places=6)

    def test_billed_wins_where_the_prediction_disagrees(self):
        """Decided (a): Octopus billed peak where a dispatch says off-peak — the cost is the billed
        band's, and the half-hour is flagged."""
        self.dispatched.add("2026-09-06T19:00:00")
        r = engine.band_parity_row(_fact(cost=round(3.6 * HI / 100, 6), label="STANDARD_RATE"))
        self.assertEqual((r["status"], r["settled"], r["predicted"], r["disagrees"]),
                         ("same", "peak", "off_peak", True))

    def test_unsettled_is_priced_by_the_prediction(self):
        r = engine.band_parity_row(_fact(bs="2026-09-07T01:00:00", cost=round(3.6 * LO / 100, 6)))
        self.assertEqual((r["status"], r["predicted"]), ("unsettled", "off_peak"))
        self.assertAlmostEqual(r["cost"], 3.6 * LO / 100, places=6)

    def test_a_published_tariff_is_the_clock(self):
        with mock.patch.object(engine, "tariff_kind_at", lambda bs: "published"):
            r = engine.band_parity_row(_fact(cost=round(3.6 * HI / 100, 6)))
        self.assertEqual((r["status"], r["settled"], r["rate"]), ("same", "clock", round(HI / 100, 6)))

    def test_a_correction_stands(self):
        """GUARD of intent: the user's own figure is not compared."""
        r = engine.band_parity_row(_fact(cost=0.5, rate_corrected=1))
        self.assertEqual(r["status"], "corrected")


class TheReport(_Base):

    def test_it_writes_a_summary_and_the_differences(self):
        st = BlockStore(":memory:")
        st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, "
                         "timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
        for bs, cost in (("2026-09-06T19:00:00", round(3.6 * HI / 100, 6)),   # changes: billed off-peak
                         ("2026-10-02T02:00:00", round(3.6 * LO / 100, 6))):  # same
            st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                             "imp_kwh, imp_cost, imp_rate) VALUES (?,?,'electricity_main',1,3.6,?,?)",
                             (bs, bs, cost, cost / 3.6))
            st.upsert_measured_cost(bs, mpan="M", cost_incl=round(3.6 * LO / 100, 6),
                                    label="OFF_PEAK", kwh=3.6)
        st._conn.commit()
        out = tempfile.mkdtemp()
        with mock.patch.object(engine, "_store", st), mock.patch.object(engine, "_reader", lambda: st):
            summary = asyncio.run(engine.run_band_parity("2026-09-01T00:00:00", "2026-11-01T00:00:00", out))
        # 19:00 has no dispatch recorded, so EMT predicts peak where Octopus billed off-peak:
        # billed wins (the cost changes to the off-peak rate) and the half-hour is flagged — and
        # with no dispatch record at all, it falls before EMT's record began
        self.assertEqual(summary["counts"],
                         {"changes · disagrees before EMT's dispatch record": 1, "same": 1})
        self.assertEqual(sorted(summary["months"]), ["2026-09", "2026-10"])
        with open(os.path.join(out, summary["csv"])) as fh:
            lines = fh.read().strip().splitlines()
        self.assertEqual(len(lines), 2)                       # the header and the one that changes
        self.assertIn("2026-09-06T19:00:00", lines[1])
        self.assertEqual(engine.band_parity_status()["status"], "done")
        self.assertTrue(any(n.endswith(".json") for n in os.listdir(out)))


class TheCsv(_Base):

    def test_a_disagreement_at_the_same_cost_is_listed(self):
        """The first report's CSV left out 37 of 53 July–October disagreements: their cost was the
        billed one, so they read `same`."""
        st = BlockStore(":memory:")
        st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, "
                         "timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
        bs = "2026-09-06T19:00:00"
        st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                         "imp_kwh, imp_cost, imp_rate) VALUES (?,?,'electricity_main',1,3.6,?,?)",
                         (bs, bs, round(3.6 * HI / 100, 6), round(HI / 100, 6)))
        st.upsert_measured_cost(bs, mpan="M", cost_incl=round(3.6 * HI / 100, 6),
                                label="STANDARD_RATE", kwh=3.6)
        st.record_dispatch_history("2026-08-01T01:00:00", "completed", provider="Myenergi",
                                   source="unknown", energy_kwh=-3.0)
        st._conn.commit()
        self.dispatched.add(bs)                          # EMT says off-peak; Octopus billed peak
        out = tempfile.mkdtemp()
        with mock.patch.object(engine, "_store", st), mock.patch.object(engine, "_reader", lambda: st):
            summary = asyncio.run(engine.run_band_parity("2026-09-01T00:00:00", "2026-10-01T00:00:00", out))
        self.assertEqual(summary["counts"], {"same · disagrees": 1})   # after the record began
        with open(os.path.join(out, summary["csv"])) as fh:
            self.assertIn(bs, fh.read())


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
