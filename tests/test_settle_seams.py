"""
Engine step 4d: the finalise seam, the gap blocks, the imports and the sweep through settle()
(EMT-v5_engine_step4_settle_design.md §5, 4d).

- every block write (finalise, a gap block, a provisional amendment, a device rebuild) is followed by
  settle() on its main half-hour, written only where it differs — finalise's live `standard` label
  was staging's last band difference;
- the history reprice sweep settles a main half-hour through settle() before its own derivation;
- a settle sweep walks the whole history once per version, and again after an import;
- v5's CSV import writes the IMPORTED bill the v4 importer's transform 3c writes.

Discriminating: every test fails on the parent (no write settles, there is no settle sweep, the CSV
import writes no bill), except those labelled GUARD.
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_engine  # noqa: F401  — installs stubs, imports engine
import engine
from block_store import BlockStore

LO, HI = 0.05493, 0.323092
BS = "2026-09-06T19:00:00"


class _Sched:
    def is_empty(self): return False
    def day_rate_bounds(self, ts): return (LO * 100, HI * 100)
    def off_peak_rate_near(self, ts): return LO * 100
    def is_off_peak(self, ts): return False
    def resolve(self, ts): return HI * 100


class _Base(unittest.TestCase):

    def setUp(self):
        self.st = BlockStore(":memory:")
        c = self.st._conn
        c.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, timezone) "
                  "VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
        c.commit()
        self.p = [mock.patch.object(engine, "_store", self.st),
                  mock.patch.object(engine, "_reader", lambda: self.st),
                  mock.patch.object(engine, "get_store", lambda: self.st),
                  mock.patch.object(engine, "_kraken_rate_schedules", {"import": _Sched()}),
                  mock.patch.object(engine, "tariff_kind_at", lambda bs: "intelligent"),
                  mock.patch.object(engine, "split_at", lambda bs: False),
                  mock.patch.object(engine, "capped_at", lambda bs: False),
                  mock.patch.object(engine, "predicted_band",
                                    lambda bs, kwh, live=True, was_online=True: "peak")]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def _main(self, bs=BS, kwh=1.0, rate=HI, corrected=0, band="standard"):
        """A main half-hour as finalise left it: the right price, its house leg labelled `band`."""
        import pricing_segments as ps
        end = (engine.datetime.fromisoformat(bs) + engine.timedelta(minutes=30)).isoformat()
        self.st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "imp_cost, imp_rate, imp_cost_exc, rate_corrected) VALUES (?,?,?,1,?,?,?,?,?)",
            (bs, end, "electricity_main", kwh, round(kwh * rate, 6), rate,
             round(kwh * round(rate / 1.05, 6), 6), corrected))
        self.st._conn.commit()
        self.st.set_block_segments(bs, "electricity_main",
                                   [ps.Segment(kwh, rate, round(rate / 1.05, 6), band, "house")])

    def _bands(self, bs=BS):
        return [(g["attribution"], g["band"]) for g in self.st.get_block_segments(bs, "electricity_main")]


class EveryWriteIsSettled(_Base):

    def _write(self, fn):
        with mock.patch.object(self.st, "append_block", lambda b, **k: None), \
             mock.patch.object(self.st, "append_block_replace", lambda b, **k: None):
            fn({"start": BS, "meters": {"electricity_main": {}}})

    def test_finalise_takes_settles_label(self):
        """Staging's 17 band rows: finalise labelled the live house leg `standard`."""
        self._main()
        self._write(engine.append_block)
        self.assertEqual(self._bands(), [("house", "day")])

    def test_a_gap_block_or_an_amendment_too(self):
        self._main()
        self._write(engine.append_block_replace)
        self.assertEqual(self._bands(), [("house", "day")])

    def test_GUARD_a_correction_is_left(self):
        self._main(corrected=1)
        self._write(engine.append_block_replace)
        self.assertEqual(self._bands(), [("house", "standard")])

    def test_GUARD_a_block_with_no_main_is_left(self):
        self._main()
        with mock.patch.object(self.st, "append_block_replace", lambda b, **k: None):
            engine.append_block_replace({"start": BS, "meters": {"ev_charger": {}}})
        self.assertEqual(self._bands(), [("house", "standard")])

    def test_GUARD_a_settle_failure_leaves_the_writers_figures(self):
        self._main()
        with mock.patch.object(engine, "settle_half_hour", side_effect=RuntimeError("x")):
            self._write(engine.append_block)        # never raises
        self.assertEqual(self._bands(), [("house", "standard")])


class TheRange(_Base):

    def test_only_what_differs_is_written(self):
        self._main(bs="2026-09-06T19:00:00")
        self._main(bs="2026-09-06T19:30:00", band="day")
        r = engine.settle_range(self.st, "2026-09-06T00:00:00", "2026-09-07T00:00:00")
        self.assertEqual((r["read"], r["written"]), (2, 1))
        self.assertEqual(r["parts"], {"band": 1})


class TheSweep(_Base):

    def setUp(self):
        super().setUp()
        for x in (mock.patch.object(engine._coord, "admission", lambda k: (engine.ADMIT, None)),
                  mock.patch.object(engine, "delete_in_progress", lambda: False),
                  mock.patch.object(engine, "_SETTLE_HISTORY_PACE_S", 0)):
            x.start()
            self.p.append(x)
        self._main(bs="2026-09-06T19:00:00")
        self._main(bs="2026-09-08T19:00:00")

    def _state(self):
        return self.st.get_meta(engine._SETTLE_HISTORY_MARKER, {}) or {}

    def test_it_walks_the_history_once(self):
        self.assertEqual(asyncio.run(engine._run_settle_history_sweep()), 2)
        self.assertEqual(self._bands("2026-09-08T19:00:00"), [("house", "day")])
        s = self._state()
        self.assertTrue(s["done"])
        self.assertEqual((s["read"], s["written"], s["parts"]), (2, 2, {"band": 2}))
        self.assertEqual(asyncio.run(engine._run_settle_history_sweep()), 0)      # done

    def test_an_import_walks_it_again(self):
        asyncio.run(engine._run_settle_history_sweep())
        self._main(bs="2026-09-07T19:00:00")                                   # imported
        asyncio.run(engine._settle_history_to_done(force=True))
        self.assertEqual(self._bands("2026-09-07T19:00:00"), [("house", "day")])
        self.assertEqual(self._state()["read"], 3)

    def test_it_stands_aside_and_resumes(self):
        with mock.patch.object(engine._coord, "admission", lambda k: ("defer", "import")):
            self.assertEqual(asyncio.run(engine._run_settle_history_sweep()), 0)
        self.assertFalse(self._state().get("done"))
        self.assertEqual(asyncio.run(engine._run_settle_history_sweep()), 2)

    def test_no_schedule_defers_without_marking_done(self):
        with mock.patch.object(engine, "_kraken_rate_schedules", {}):
            self.assertEqual(asyncio.run(engine._run_settle_history_sweep()), -1)
            asyncio.run(engine._settle_history_to_done())                      # returns at once
        self.assertFalse(self._state().get("done"))


class TheRepriceChunk(_Base):

    def test_a_main_half_hour_is_settled_not_derived(self):
        self._main()
        with mock.patch.object(engine, "_reprice_history_block",
                               side_effect=AssertionError("derived")):
            engine._reprice_history_blocks([{"block_start": BS, "meter_id": "electricity_main"}],
                                           "UTC", None)
        self.assertEqual(self._bands(), [("house", "day")])

    def test_GUARD_what_settle_cannot_price_is_derived(self):
        self._main()
        seen = []
        with mock.patch.object(engine, "_kraken_rate_schedules", {}), \
             mock.patch.object(engine, "_band_rates", lambda bs: (None, None)), \
             mock.patch.object(engine, "_reprice_history_block",
                               lambda row, tz, segs: seen.append(row) or {"_start": BS}):
            engine._reprice_history_blocks([{"block_start": BS, "meter_id": "electricity_main"}],
                                           "UTC", None)
        self.assertEqual(len(seen), 1)


class TheCsvImportWritesABill(unittest.TestCase):
    """What the import heard is a bill (design §6 (e)): v5's own CSV import, as transform 3c."""

    CSV = ("Consumption (kwh),Estimated Cost Inc. Tax (p),Standing Charge Inc. Tax (p),Start,End\n"
           "1.0,7.0,1.12,2024-07-01T00:00:00+00:00,2024-07-01T00:30:00+00:00\n"
           "2.0,50.0,1.12,2024-07-01T18:00:00+00:00,2024-07-01T18:30:00+00:00\n"
           "0.0,0.0,1.12,2024-07-01T19:00:00+00:00,2024-07-01T19:30:00+00:00\n")

    def setUp(self):
        self.st = BlockStore(":memory:")
        self.st.insert_config_period({"meters": {"electricity_main": {"meta": {
            "billing_day": 1, "block_minutes": 30, "timezone": "UTC",
            "currency_symbol": "£", "currency_code": "GBP", "site": "Home"}}}})

    def _bills(self):
        return [tuple(r) for r in self.st._conn.execute(
            "SELECT slot_start, mpan, label, kwh, ROUND(cost_incl, 6) FROM measured_cost "
            "ORDER BY slot_start")]

    def test_each_half_hour_with_a_cost(self):
        r = self.st.apply_csv_import({"import": self.CSV}, bill_mpan="M")
        self.assertEqual(r["bills_written"], 2)                       # not the 0 kWh one
        self.assertEqual(self._bills(), [("2024-07-01T00:00:00", "M", "IMPORTED", 1.0, 0.07),
                                         ("2024-07-01T18:00:00", "M", "IMPORTED", 2.0, 0.5)])

    def test_a_bill_already_held_is_kept(self):
        self.st.upsert_measured_cost("2024-07-01T18:00:00", mpan="M", cost_incl=0.11,
                                     label="OFF_PEAK", kwh=2.0)
        r = self.st.apply_csv_import({"import": self.CSV}, bill_mpan="M")
        self.assertEqual(r["bills_written"], 1)
        self.assertIn(("2024-07-01T18:00:00", "M", "OFF_PEAK", 2.0, 0.11), self._bills())


if __name__ == "__main__":
    unittest.main()
