"""
Engine step 4a: one settle() for a main-meter half-hour, and its parity report
(EMT-v5_engine_step4_settle_design.md §3–§5, decided 25 Oct 2026).

settle() is pure — facts in, the half-hour out: the band (billed wins, else the prediction), each
leg at its band's rate, `imp_rate` the blend (§4 (a)), ex-VAT through the VAT calendar (§4 (c)), the
devices priced at the rate of the legs their kWh sits in (§4 (b)). settle_facts reads the facts and no
stored rate, cost or split; its prediction is judged on the DCC kWh (§4 (d)).

Discriminating: every test here fails on the parent (settle.py and the report are new), except the
ones labelled GUARD.
"""
import asyncio
import dataclasses
import inspect
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_engine  # noqa: F401  — installs stubs, imports engine
import engine
import settle as S
from block_store import BlockStore

LO, HI = 0.05493, 0.323092                   # £: the off-peak and day rates
EV_OFF, EV_PEAK = 0.05493, 0.323092


def _f(**kw):
    base = dict(start="2026-09-06T19:00:00", end="2026-09-06T19:30:00", kwh=4.0,
                kind="intelligent", vat=0.05, lo=LO, hi=HI)
    base.update(kw)
    return S.Facts(**base)


class TheBand(unittest.TestCase):

    def test_billed_wins_over_the_prediction(self):
        s = S.settle(_f(settled="off_peak", predicted="peak"))
        self.assertEqual((s.status, s.band), ("settled", "off_peak"))
        self.assertEqual({g.inc_rate for g in s.legs}, {LO})

    def test_the_prediction_where_nothing_is_billed(self):
        s = S.settle(_f(predicted="peak"))
        self.assertEqual((s.status, s.band, [g.band for g in s.legs]), ("predicted", "peak", ["day"]))

    def test_no_band_prices_nothing(self):
        self.assertEqual(S.settle(_f()).status, "no_band")

    def test_no_rate_prices_nothing(self):
        self.assertEqual(S.settle(_f(lo=None, hi=None, settled="peak")).status, "no_rate")

    def test_a_published_tariff_is_the_clock(self):
        s = S.settle(_f(kind="published", clock_rate=0.2412, lo=None, hi=None))
        self.assertEqual((s.status, s.imp_rate, s.imp_cost), ("clock", 0.2412, round(4.0 * 0.2412, 6)))
        self.assertEqual([(g.band, g.attribution) for g in s.legs], [("standard", "house")])

    def test_nothing_drawn_is_empty(self):
        self.assertEqual(S.settle(_f(kwh=0.0, settled="peak")).status, "empty")


class TheRate(unittest.TestCase):
    """§4 (a): imp_rate is the blend, cost ÷ kWh, so imp_cost = imp_kwh × imp_rate always."""

    def test_one_rate_is_that_rate_exactly(self):
        """The blend from the unrounded sum: 0.004 kWh at the peak rate is the peak rate, not
        round(0.001292 / 0.004) = 0.323 (the smoke run on staging's copy, 25 Oct 2026)."""
        s = S.settle(_f(kwh=0.004, settled="peak"))
        self.assertEqual(s.imp_rate, HI)
        self.assertFalse(s.two_rates)

    def test_a_mixed_bill_off_the_cap_puts_each_leg_on_its_bucket(self):
        s = S.settle(_f(settled="mixed", settled_legs={"ev": "off_peak", "house": "peak"}, ev_kwh=3.0))
        self.assertEqual(sorted((g.attribution, g.band, g.inc_rate) for g in s.legs),
                         [("ev", "off_peak", LO), ("house", "day", HI)])
        self.assertTrue(s.two_rates)
        self.assertAlmostEqual(s.imp_cost, 3.0 * LO + 1.0 * HI, places=6)
        self.assertAlmostEqual(4.0 * s.imp_rate, s.imp_cost, places=6)

    def test_a_mixed_bill_with_no_buckets_falls_to_peak(self):
        """As the bill apply does today; listed, not hidden: the legs carry the peak band."""
        s = S.settle(_f(settled="mixed", ev_kwh=3.0))
        self.assertEqual({g.inc_rate for g in s.legs}, {HI})

    def test_the_cap_prices_over_cap_ev_at_peak(self):
        """Capped, predicted: the cap's legs. Past the cap-day's boundary the EV is peak and the
        house loses the out-of-window freebie."""
        s = S.settle(_f(predicted="off_peak", ev_kwh=3.0, capped=True, ev_off=EV_OFF, ev_peak=EV_PEAK,
                        boundary=("2026-09-06T18:00:00", 0.5)))
        self.assertEqual(sorted((g.attribution, g.band) for g in s.legs),
                         [("ev", "peak"), ("house", "day")])

    def test_the_boundary_half_hour_has_two_rates(self):
        s = S.settle(_f(predicted="off_peak", ev_kwh=3.0, capped=True, ev_off=EV_OFF, ev_peak=EV_PEAK,
                        boundary=("2026-09-06T19:00:00", 0.5)))
        self.assertTrue(s.two_rates)
        self.assertAlmostEqual(s.imp_cost, 1.5 * EV_OFF + 1.5 * EV_PEAK + 0.5 * LO + 0.5 * HI, places=6)
        self.assertAlmostEqual(s.imp_rate * 4.0, s.imp_cost, places=6)

    def test_a_predicted_peak_on_the_cap_is_a_bump(self):
        """A predicted peak is a dispatch that does not count (the lifecycle's bump): the EV on peak."""
        s = S.settle(_f(predicted="peak", ev_kwh=3.0, capped=True, ev_off=EV_OFF, ev_peak=EV_PEAK))
        self.assertEqual([g.band for g in s.legs if g.attribution == "ev"], ["peak"])


class TheExVat(unittest.TestCase):
    """§4 (c): each leg's rate through the VAT calendar, one way."""

    def test_five_percent(self):
        s = S.settle(_f(settled="peak"))
        self.assertEqual(s.legs[0].exc_rate, round(HI / 1.05, 6))
        self.assertAlmostEqual(s.imp_cost_exc, round(4.0 * round(HI / 1.05, 6), 6), places=6)

    def test_zero_percent(self):
        s = S.settle(_f(settled="peak", vat=0.0))
        self.assertEqual(s.legs[0].exc_rate, HI)


class TheDevices(unittest.TestCase):
    """§4 (b): PASS 2's clip, each device at the rate of the legs its kWh sits in."""

    def _two_rate(self, devices, **kw):
        return S.settle(_f(settled="mixed", settled_legs={"ev": "off_peak", "house": "peak"},
                           ev_kwh=3.0, devices=devices, **kw))

    def test_the_ev_meter_pays_the_ev_legs_and_a_battery_the_house(self):
        s = self._two_rate([S.Device("ev_charger", 3.0, True), S.Device("battery", 0.8)])
        d = {x.meter_id: x for x in s.devices}
        self.assertEqual((d["ev_charger"].rate, d["battery"].rate), (LO, HI))
        self.assertLessEqual(sum(x.cost for x in s.devices), s.imp_cost + 1e-9)

    def test_an_ev_meter_past_the_ev_legs_pays_the_house_rate_for_the_rest(self):
        s = self._two_rate([S.Device("ev_charger", 3.5, True)])
        d = s.devices[0]
        self.assertAlmostEqual(d.cost, 3.0 * LO + 0.5 * HI, places=6)

    def test_the_main_rate_is_not_the_device_rate(self):
        """The rejected option: every device at the blend. On a two-rate half-hour they differ."""
        s = self._two_rate([S.Device("ev_charger", 3.0, True)])
        self.assertNotAlmostEqual(s.devices[0].rate, s.imp_rate, places=4)

    def test_the_grid_clips_the_devices(self):
        s = S.settle(_f(settled="peak", kwh=1.0, devices=[S.Device("ev_charger", 0.7, True),
                                                           S.Device("battery", 0.6)]))
        d = {x.meter_id: x for x in s.devices}
        self.assertEqual((d["ev_charger"].kwh_grid, d["battery"].kwh_grid), (0.7, 0.3))
        self.assertAlmostEqual(d["battery"].kwh_other, 0.3, places=6)

    def test_with_no_ev_meter_the_devices_fit_inside_the_house(self):
        """v4 4.5.21: a dispatch-only account reserves the main's EV before the devices."""
        s = S.settle(_f(settled="off_peak", kwh=4.0, ev_kwh=3.0, devices=[S.Device("battery", 2.0)]))
        self.assertEqual(s.devices[0].kwh_grid, 1.0)

    def test_an_unsettled_gap_block_records_the_device_as_read(self):
        s = S.settle(_f(settled="peak", kwh=1.0, clamp=False, devices=[S.Device("battery", 1.5)]))
        self.assertEqual(s.devices[0].kwh_grid, 1.5)

    def test_a_device_that_drew_nothing_follows_its_legs_rate(self):
        s = self._two_rate([S.Device("ev_charger", 0.0, True), S.Device("battery", 0.0)])
        d = {x.meter_id: x for x in s.devices}
        self.assertEqual((d["ev_charger"].rate, d["battery"].rate, d["battery"].cost), (LO, HI, 0.0))


class ItIsPure(unittest.TestCase):

    def test_the_same_facts_give_the_same_answer(self):
        f = _f(settled="mixed", settled_legs={"ev": "off_peak", "house": "peak"}, ev_kwh=3.0,
               devices=[S.Device("ev_charger", 3.2, True)])
        self.assertEqual(S.settle(f), S.settle(dataclasses.replace(f)))

    def test_the_facts_hold_no_stored_rate_cost_or_split(self):
        """§4 (a)'s condition: nothing bands, or matches, on a stored rate — settle() is not given one."""
        names = {x.name for x in dataclasses.fields(S.Facts)}
        self.assertFalse(names & {"imp_rate", "imp_cost", "imp_cost_exc", "segments", "imp_kwh_grid",
                                  "rate_source"})

    def test_settle_facts_reads_no_stored_rate_cost_or_split(self):
        src = inspect.getsource(engine.settle_facts)
        for k in ("imp_rate", "imp_cost", "imp_kwh_grid", "imp_kwh_ev", "get_block_segments"):
            self.assertNotIn(k, src, k)


class _Sched:
    def is_empty(self): return False
    def day_rate_bounds(self, ts): return (LO * 100, HI * 100)
    def off_peak_rate_near(self, ts): return LO * 100
    def is_off_peak(self, ts): return False
    def resolve(self, ts): return HI * 100


class _Base(unittest.TestCase):
    BS = "2026-09-06T19:00:00"

    def setUp(self):
        self.st = BlockStore(":memory:")
        c = self.st._conn
        c.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, timezone) "
                  "VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
        c.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter, parent_meter_id, "
                  "meter_type) VALUES (1,'ev_charger',1,'electricity_main','ev')")
        c.commit()
        self.pred = {}
        self.p = [mock.patch.object(engine, "_store", self.st),
                  mock.patch.object(engine, "_reader", lambda: self.st),
                  mock.patch.object(engine, "_kraken_rate_schedules", {"import": _Sched()}),
                  mock.patch.object(engine, "tariff_kind_at", lambda bs: "intelligent"),
                  mock.patch.object(engine, "split_at", lambda bs: False),
                  mock.patch.object(engine, "capped_at", lambda bs: False),
                  mock.patch.object(engine, "predicted_band",
                                    lambda bs, kwh, live=True: self.pred.get(round(kwh, 3), "peak"))]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def _main(self, kwh=1.0, api=1.0, cost=None, rate=None, cost_exc=None, cad=None, corrected=0):
        c = self.st._conn
        c.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
                  "imp_kwh_api, imp_cost, imp_rate, imp_cost_exc, rate_corrected) "
                  "VALUES (?,?,?,1,?,?,?,?,?,?)",
                  (self.BS, "2026-09-06T19:30:00", "electricity_main", kwh, api, cost, rate, cost_exc,
                   corrected))
        if cad is not None:
            c.execute("INSERT INTO ha_reads (meter_id, channel, origin, period_start, minutes, kwh, "
                      "recorded_at) VALUES ('electricity_main','import','live',?,30,?,?)",
                      (self.BS, cad, self.BS))
        c.commit()

    def _segs(self, *segs):
        import pricing_segments as ps
        self.st.set_block_segments(self.BS, "electricity_main",
                                   [ps.Segment(k, r, round(r / 1.05, 6), b, a) for k, r, b, a in segs])

    def _row(self):
        inp = self.st.get_settle_inputs("2026-09-06T00:00:00", "2026-09-07T00:00:00")
        m = inp["mains"][0]
        return engine.settle_parity_row(m, inp["subs"].get(self.BS, []),
                                        inp["segments"].get(self.BS, []), self.st)


class TheFacts(_Base):

    def test_the_kwh_is_the_dcc_figure(self):
        """The 07:00 shape: a block still on its CAD figure once DCC has landed is settled on DCC."""
        self._main(kwh=0.004, api=0.001)
        m = self.st.get_settle_inputs("2026-09-06T00:00:00", "2026-09-07T00:00:00")["mains"][0]
        self.assertEqual(engine.settle_facts(m, [], self.st).kwh, 0.001)

    def test_a_stored_rate_does_not_move_the_facts(self):
        self._main(rate=0.05493, cost=0.05493)
        m = self.st.get_settle_inputs("2026-09-06T00:00:00", "2026-09-07T00:00:00")["mains"][0]
        a = engine.settle_facts(m, [], self.st)
        b = engine.settle_facts(dict(m, imp_rate=9.9, imp_cost=9.9, rate_source="settled"), [], self.st)
        self.assertEqual(a, b)


class TheRow(_Base):

    def test_same(self):
        self._main(cost=HI, rate=HI, cost_exc=round(round(HI / 1.05, 6), 6))
        self._segs((1.0, HI, "day", "house"))
        r = self._row()
        self.assertEqual((r["status"], r["parts"]), ("same", ""))

    def test_a_peak_bill_on_a_main_holding_the_off_peak_rate_differs(self):
        self._main(cost=LO, rate=LO, cost_exc=round(LO / 1.05, 6))
        self._segs((1.0, LO, "off_peak", "house"))
        r = self._row()
        self.assertEqual(r["status"], "differs")
        self.assertEqual(set(r["parts"].split("+")), {"band", "cost", "rate", "ex-VAT", "segments"})

    def test_a_correction_is_not_settled(self):
        self._main(cost=0.1, rate=0.1, corrected=1)
        self.assertEqual(self._row()["status"], "corrected")

    def test_the_device_is_compared(self):
        self._main(cost=HI, rate=HI, cost_exc=round(HI / 1.05, 6))
        self._segs((1.0, HI, "day", "house"))
        self.st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "imp_kwh_grid, imp_cost, imp_rate) VALUES (?,?,?,1,0.5,0.5,?,?)",
            (self.BS, "2026-09-06T19:30:00", "ev_charger", 0.5 * LO, LO))
        self.st._conn.commit()
        r = self._row()
        self.assertEqual(r["parts"], "devices")
        self.assertIn("ev_charger", r["devices"])

    def test_the_dcc_kwh_moving_a_predicted_band_is_counted(self):
        """§4 (d): the CAD reading predicted off-peak, the DCC kWh predicts peak (a threshold)."""
        self.pred = {0.12: "off_peak", 0.08: "peak"}
        self._main(kwh=0.08, api=0.08, cad=0.12, cost=round(0.08 * HI, 6), rate=HI)
        self.assertEqual(self._row()["dcc_moves"], "off_peak→peak")

    def test_a_billed_half_hour_is_never_counted_as_moved(self):
        self.pred = {0.12: "off_peak", 0.08: "peak"}
        self._main(kwh=0.08, api=0.08, cad=0.12)
        self.st.upsert_measured_cost(self.BS, mpan="M", cost_incl=0.004, label="OFF_PEAK", kwh=0.08)
        self.assertEqual(self._row()["dcc_moves"], "")


class TheReport(_Base):

    def test_it_writes_the_summary_and_the_csv(self):
        self.pred = {0.12: "off_peak", 0.08: "peak"}
        self._main(kwh=0.08, api=0.08, cad=0.12, cost=round(0.08 * HI, 6), rate=HI,
                   cost_exc=round(0.08 * round(HI / 1.05, 6), 6))
        self._segs((0.08, HI, "day", "house"))
        out = tempfile.mkdtemp()
        s = asyncio.run(engine.run_settle_parity("2026-09-01T00:00:00", "2026-10-01T00:00:00", out))
        self.assertEqual(s["counts"], {"same": 1})
        self.assertEqual(s["section4"], {"two_rates": 0, "device_two_rates": 0, "dcc_moves": 1})
        rows = open(os.path.join(out, s["csv"])).read().strip().splitlines()
        self.assertEqual(len(rows), 2)                    # the header, and the half-hour §4 moves
        self.assertTrue(any(n.endswith(".json") for n in os.listdir(out)))
        self.assertEqual(engine.settle_parity_status()["status"], "done")


class TheSettledBandIsUnchanged(unittest.TestCase):
    """settled_band now reads the buckets through _bucket_bands (new); its bands are as before — the
    settled_band assertions are a GUARD, the _bucket_bands one discriminates."""

    def setUp(self):
        self.p = mock.patch.object(engine, "_kraken_rate_schedules", {"import": _Sched()})
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_one_band_and_mixed(self):
        one = {"home_kwh": 0.4, "home_rate": HI, "ev_kwh": 3.2, "ev_rate": HI}
        mix = {"home_kwh": 0.4, "home_rate": HI, "ev_kwh": 3.2, "ev_rate": LO}
        self.assertEqual(engine.settled_band("x", "OFF_PEAK", one), "peak")
        self.assertEqual(engine.settled_band("x", "OFF_PEAK", mix), "mixed")
        self.assertEqual(engine._bucket_bands("x", mix), {"house": "peak", "ev": "off_peak"})


if __name__ == "__main__":
    unittest.main()
