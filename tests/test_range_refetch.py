"""
ROADMAP BL-86 / settlement surfaces PR 3: re-fetch a range from Octopus — preview, then apply.

The preview fetches the range's kWh, bill and split into memory and reports what differs; it
writes nothing. Apply writes only those half-hours, through the existing paths, and keeps
4.5.23's hold-back. Against faked Octopus answers: a restated split (10 Oct 2026's case), a VAT
restatement (#505's), an unchanged range, a changed kWh, a bill that drops the car, a corrected
block, a stale preview, the range cap.

Discriminating: every test here fails on the parent (none of this exists there).
"""
import asyncio
import os
import sys
import types
import unittest
import unittest.mock
from unittest.mock import MagicMock

eio = types.ModuleType("energy_engine_io")
eio.ensure_dir = lambda *a, **kw: None
eio.load_json = lambda *a, **kw: a[1] if len(a) > 1 else {}
eio.save_json_atomic = lambda *a, **kw: None
eio.save_file = lambda *a, **kw: None
sys.modules["energy_engine_io"] = eio
sys.modules["energy_charts"] = types.ModuleType("energy_charts")
hc = types.ModuleType("ha_client"); hc.HAClient = MagicMock
sys.modules["ha_client"] = hc

from block_store import BlockStore              # noqa: E402
from kraken_rates import RateSchedule           # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine                                   # noqa: E402
import job_coordinator                          # noqa: E402

SLOT = "2026-09-01T02:00:00"                    # inside the IOG off-peak window
FROM, TO = "2026-09-01T00:00:00", "2026-09-02T00:00:00"
OFF = 0.05493
KWH = 6.672
CFG = {"meters": {"electricity_main": {"meta": {
    "timezone": "Europe/London", "billing_day": 1, "block_minutes": 30,
    "currency_symbol": "£", "currency_code": "GBP", "sub_meter": False}}}}


def _node(home, ev, rate=OFF, vat=0.0):
    return {"home_kwh": home, "ev_kwh": ev, "home_rate": rate, "ev_rate": rate if ev else None,
            "home_rate_exc": round(rate / (1 + vat), 6),
            "ev_rate_exc": round(rate / (1 + vat), 6) if ev else None,
            "home_cost": round(home * rate, 6), "ev_cost": round(ev * rate, 6),
            "home_band": "off_peak", "ev_band": "off_peak" if ev else None}


class _Octopus:
    def __init__(self, kwh=KWH, node=None):
        self.kwh, self.node, self.asked = kwh, node, []

    async def get_consumption(self, mpan, serial, period_from=None, period_to=None, **kw):
        return [{"interval_start": SLOT + "Z", "consumption": self.kwh}]

    async def recover_device_breakdown(self, mpan, slots, **kw):
        self.asked.extend(slots)
        return {s: self.node for s in slots if self.node is not None}

    cost_only = None                                   # {slot: node} the Measurements answer
    asked_cost = ()

    async def recover_measurement_costs(self, mpan, slots, **kw):
        self.asked_cost = tuple(self.asked_cost) + tuple(slots)
        return {s: self.cost_only[s] for s in slots if self.cost_only and s in self.cost_only}


class _Harness(unittest.TestCase):

    def setUp(self):
        self._save = (engine._store, engine._kraken_rate_schedules, engine._kraken_discovery,
                      engine._kraken_client, engine._build_kraken_ingester,
                      engine._refetch_preview)
        job_coordinator.reset_for_tests()
        engine._refetch_state.clear(); engine._refetch_state["status"] = "idle"
        st = BlockStore(":memory:"); st.insert_config_period(CFG)
        engine._store = self.st = st
        engine._kraken_discovery = {"import": {"mpan": "IM", "tariff_code": "E-1R-IOG-SMB-X-B",
                                               "product_code": "IOG-SMB-X"}}
        night = ("2026-08-31T22:30:00", "2026-09-01T04:30:00")
        day = ("2026-09-01T04:30:00", "2026-09-01T22:30:00")
        engine._kraken_rate_schedules = {
            "import": RateSchedule([night + (5.493,), day + (32.3092,)]),
            "ev_device_off_peak": RateSchedule([night + (5.493,)]),
            "ev_device_peak": RateSchedule([day + (32.3092,)])}
        engine._build_kraken_ingester = lambda: types.SimpleNamespace(
            import_mpan="IM", import_serial="S", export_mpan=None, export_serial=None)

    def tearDown(self):
        (engine._store, engine._kraken_rate_schedules, engine._kraken_discovery,
         engine._kraken_client, engine._build_kraken_ingester,
         engine._refetch_preview) = self._save
        job_coordinator.reset_for_tests()

    def _block(self, *, rate_source="settled", cost=None, ev=None, bill=True, vat=0.0):
        cost = round(KWH * OFF * (1 + vat), 6) if cost is None else cost
        self.st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "imp_kwh_api, imp_rate, imp_cost, imp_cost_exc, rate_source, rate_corrected) "
            "VALUES (?,?,?,1,?,?,?,?,?,?,?)",
            (SLOT, "2026-09-01T02:30:00", "electricity_main", KWH, KWH, OFF, cost,
             round(KWH * OFF, 6), rate_source, 1 if rate_source == "corrected" else 0))
        segs = [(KWH - (ev or 0), OFF, OFF, "off_peak", "house")]
        if ev:
            segs.insert(0, (ev, OFF, OFF, "off_peak", "ev"))
        self.st.set_block_segments(SLOT, "electricity_main", segs)
        if bill:                                         # the house-only bill of 10 Oct's kind
            self.st.upsert_measured_cost(SLOT, mpan="IM", cost_incl=cost,
                                         cost_excl=round(KWH * OFF, 6), label="OFF_PEAK", kwh=KWH)
            self.st.upsert_measured_breakdown(SLOT, mpan="IM", home_kwh=KWH, home_rate=OFF,
                                              ev_kwh=0.0, ev_rate=None)
        self.st.upsert_dispatch_slot(SLOT, off_peak=True, source="smart-charge")
        self.st.record_dispatch_history(SLOT, "completed", provider="Myenergi", source="unknown",
                                        energy_kwh=-3.23)
        self.st._conn.commit()

    def _dump(self):
        return list(self.st._conn.iterdump())

    def _preview(self, octopus, frm=FROM, to=TO, parts=("kwh", "bill")):
        engine._kraken_client = octopus
        self.assertEqual(engine.refetch_claim(), (True, None))
        asyncio.run(engine.run_refetch_preview(frm, to, parts))
        return engine.refetch_status()

    def _apply(self, token):
        self.assertEqual(engine.refetch_claim(), (True, None))
        asyncio.run(engine.run_refetch_apply(token))
        return engine.refetch_status()


class ThePreview(_Harness):

    def test_it_writes_nothing(self):
        self._block()
        before = self._dump()
        st = self._preview(_Octopus(node=_node(3.5416, 3.1304)))
        self.assertEqual(st["status"], "done", st)
        self.assertEqual(self._dump(), before)

    def test_a_restated_split_is_one_difference(self):
        self._block()
        st = self._preview(_Octopus(node=_node(3.5416, 3.1304)))
        self.assertEqual(st["summary"]["differ"], 1)
        (row,) = st["rows"]
        self.assertEqual((row["bill"]["stored_ev"], row["bill"]["octopus_ev"]), (0.0, 3.1304))
        self.assertFalse(row["bill"]["held"])
        self.assertNotIn("kwh", row)                     # the kWh agrees

    def test_an_unchanged_range_has_nothing_to_apply(self):
        self._block()
        st = self._preview(_Octopus(node=_node(KWH, 0.0)))
        self.assertEqual(st["summary"]["differ"], 0)
        before = self._dump()
        self._apply(st["token"])
        self.assertEqual(self._dump(), before)


    def test_the_range_cap(self):
        self.assertIn("31 days", engine._refetch_check_range("2026-09-01T00:00:00",
                                                             "2026-10-03T00:00:00"))
        self.assertIsNotNone(engine._refetch_check_range(TO, FROM))
        self.assertIsNone(engine._refetch_check_range(FROM, TO))


class TheApply(_Harness):

    def test_a_restated_split_reaches_the_block(self):
        """10 Oct 2026: house-only first, the car two days later."""
        self._block()
        st = self._preview(_Octopus(node=_node(3.5416, 3.1304)))
        done = self._apply(st["token"])
        self.assertEqual(done["applied"]["bill"], 1, done)
        self.assertAlmostEqual(self.st.get_measured_breakdown(SLOT)["ev_kwh"], 3.1304, places=4)
        self.assertAlmostEqual(self.st.get_ev_split_kwh([SLOT])[SLOT], 3.1304, places=3)

    def test_a_vat_restatement_re_settles_the_cost(self):
        """#505: a bill at 5% on a 0% day, restated."""
        self._block(vat=0.05)
        st = self._preview(_Octopus(node=_node(KWH, 0.0)))
        self.assertEqual(st["summary"]["bill"], 1)
        self._apply(st["token"])
        cost = self.st._conn.execute("SELECT imp_cost FROM blocks WHERE block_start=? AND "
                                     "meter_id='electricity_main'", (SLOT,)).fetchone()[0]
        self.assertAlmostEqual(cost, round(KWH * OFF, 6), places=5)

    def test_a_changed_kwh_settles_and_queues_the_re_run(self):
        self._block()
        st = self._preview(_Octopus(kwh=7.0, node=_node(KWH, 0.0)), parts=("kwh",))
        self.assertEqual(st["rows"][0]["kwh"], {"stored": KWH, "octopus": 7.0, "imported": False})
        self._apply(st["token"])
        api, rerun = self.st._conn.execute(
            "SELECT imp_kwh_api, needs_pass2_rerun FROM blocks WHERE block_start=? AND "
            "meter_id='electricity_main'", (SLOT,)).fetchone()
        self.assertEqual((api, rerun), (7.0, 1))

    def test_a_bill_that_drops_the_car_is_held(self):
        """4.5.23's rule: the stored split has the car, Octopus's re-fetch says house only."""
        self._block(ev=3.1, bill=False)
        self.st.upsert_measured_cost(SLOT, mpan="IM", cost_incl=round(KWH * OFF, 6) + 0.01,
                                     cost_excl=round(KWH * OFF, 6), label="OFF_PEAK", kwh=KWH)
        self.st.upsert_measured_breakdown(SLOT, mpan="IM", home_kwh=KWH - 3.1, home_rate=OFF,
                                          ev_kwh=3.1, ev_rate=OFF)
        self.st._conn.commit()
        st = self._preview(_Octopus(node=_node(KWH, 0.0)))
        self.assertTrue(st["rows"][0]["bill"]["held"])
        done = self._apply(st["token"])
        self.assertEqual(done["applied"]["held"], 1)
        self.assertAlmostEqual(self.st.get_measured_breakdown(SLOT)["ev_kwh"], 3.1, places=4)

    def test_a_stale_preview_is_refused(self):
        self._block()
        st = self._preview(_Octopus(node=_node(3.5416, 3.1304)))
        self.st.bump_data_epoch()                         # something wrote since the preview
        self.assertIn("changed since the preview", engine.refetch_apply_check(st["token"]))
        self.assertIn("no longer held", engine.refetch_apply_check("not-the-token"))


class TheApplyReportsHonestly(_Harness):

    def test_a_bill_the_block_would_not_take_is_counted_apart(self):
        """PR 4, 13 Oct 2026: the apply counted every stored bill as applied, even where
        apply_measured_to_block refused (no tariff rate to snap to)."""
        self._block(vat=0.05)
        st = self._preview(_Octopus(node=_node(KWH, 0.0)))
        engine._kraken_rate_schedules = {}                 # nothing to snap the rate to
        done = self._apply(st["token"])["applied"]
        self.assertEqual((done["bill"], done["not_settled"]), (0, 1))

    def test_the_route_checks_with_its_own_store(self):
        """The apply route's check must not touch the engine loop's connection."""
        self._block()
        st = self._preview(_Octopus(node=_node(3.5416, 3.1304)))
        other = unittest.mock.MagicMock()
        other.get_data_epoch.return_value = -1             # a different store, a different answer
        other.day_fingerprints.return_value = {}
        self.assertIn("changed since", engine.refetch_apply_check(st["token"], other))
        self.assertIsNone(engine.refetch_apply_check(st["token"]))


class TheLatestBill(_Harness):

    def test_two_mpans_read_the_latest_fetched_bill(self):
        """A meter change leaves two measured_cost rows for a half-hour; a plain join read
        whichever SQLite met first."""
        self._block(vat=0.05)
        self.st._conn.execute("UPDATE measured_cost SET fetched_at='2026-09-02T00:00:00'")
        self.st.upsert_measured_cost(SLOT, mpan="NEW", cost_incl=round(KWH * OFF, 6),
                                     cost_excl=round(KWH * OFF, 6), label="OFF_PEAK", kwh=KWH,
                                     fetched_at="2026-09-05T00:00:00")
        self.st._conn.commit()
        (f,) = self.st.get_settlement_facts(FROM, TO)
        self.assertAlmostEqual(f["bill_cost"], round(KWH * OFF, 6))
        self.assertAlmostEqual(self.st.get_settlement_detail(SLOT)["bill_cost"], round(KWH * OFF, 6))


class ThePiPreview(_Harness):
    """PR 5 on the Pi, 14 Oct 2026: a two-month preview listed 401 of 1,488 half-hours, nearly
    all no-ops. Each case below was one of them."""

    def _imported(self):
        self.st._conn.execute("UPDATE blocks SET source='imported_api', imp_kwh_api=NULL")
        self.st._conn.commit()

    def test_a_re_fetch_asks_for_bills_before_the_floor(self):
        """14 Oct 2026 (design §1.2, §5.2): a half-hour outside the automatic pass's scope holding
        kWh x rate is red past the window; a re-fetch is its remedy, so it asks for that bill.
        (Until now it asked from the floor only, and 25 Aug 05:00 read as doing nothing.)"""
        self._block(bill=False)
        was, engine._kraken_current_agreement_from = (engine._kraken_current_agreement_from,
                                                      "2026-09-02T00:00:00")
        try:
            oc = _Octopus(node=_node(3.5416, 3.1304))
            self._preview(oc)
        finally:
            engine._kraken_current_agreement_from = was
        self.assertEqual(oc.asked, [SLOT])

    def test_off_smb_an_undispatched_half_hour_is_asked_too(self):
        """GUARD: passes on the parent too — the harness's schedules still read as SMB, so the
        parent's dispatched-only filter never applied here."""
        self._block(bill=False)
        self.st._conn.execute("DELETE FROM dispatch_slots"); self.st._conn.commit()
        engine._kraken_discovery = {"import": {"mpan": "IM", "tariff_code": "E-1R-INTELLI-VAR-B",
                                               "product_code": "INTELLI-VAR"}}
        oc = _Octopus(node=_node(KWH, 0.0))
        self._preview(oc)
        self.assertEqual(oc.asked, [SLOT])

    def test_imported_history_agrees_with_its_own_kwh(self):
        self._block(bill=False)
        self._imported()
        st = self._preview(_Octopus(node=_node(KWH, 0.0)), parts=("kwh",))
        self.assertEqual(st["summary"]["differ"], 0, st["rows"])

    def test_an_imported_kwh_difference_is_reported_not_written(self):
        self._block(bill=False)
        self._imported()
        st = self._preview(_Octopus(kwh=7.0, node=_node(KWH, 0.0)), parts=("kwh",))
        self.assertEqual(st["rows"][0]["kwh"], {"stored": KWH, "octopus": 7.0, "imported": True})
        self._apply(st["token"])
        api, src = self.st._conn.execute(
            "SELECT imp_kwh_api, source FROM blocks WHERE block_start=? AND "
            "meter_id='electricity_main'", (SLOT,)).fetchone()
        self.assertEqual((api, src), (None, "imported_api"))

    def test_an_unbilled_half_hour_at_the_same_cost_gains_its_bill(self):
        """Revised 14 Oct 2026 (design §1.2): Octopus's bill where none is held is a change even at
        the same £ — the cell goes from kWh x rate to Octopus's cost. (Until then: no difference,
        and a re-fetch left the cell red.)"""
        self._block(bill=False)
        st = self._preview(_Octopus(node=_node(KWH, 0.0)))
        (row,) = st["rows"]
        self.assertTrue(row["bill"]["new_bill"])
        self._apply(st["token"])
        self.assertEqual(self.st._conn.execute(
            "SELECT rate_source FROM blocks WHERE block_start=? AND meter_id='electricity_main'",
            (SLOT,)).fetchone()[0], "settled")

    def test_an_unbilled_half_hour_at_another_cost_compares_with_emts(self):
        self._block(bill=False, cost=0.5)
        st = self._preview(_Octopus(node=_node(KWH, 0.0)))
        (row,) = st["rows"]
        self.assertEqual((row["bill"]["stored"], row["bill"]["stored_is_bill"]), (0.5, False))

    def test_a_held_bill_at_the_same_cost_changes_nothing(self):
        self._block(ev=3.1, bill=False)
        st = self._preview(_Octopus(node=_node(KWH, 0.0)))
        self.assertEqual(st["summary"]["differ"], 0, st["rows"])

    def test_no_export_stored_and_none_exported_is_no_difference(self):
        facts = {SLOT: {"imp_kwh": KWH, "imp_kwh_api": KWH, "exp_kwh": 0.0, "exp_kwh_api": None}}
        self.assertEqual(engine._refetch_diff(facts, {SLOT: {"exp_kwh": 0.0}}), [])
        (row,) = engine._refetch_diff(facts, {SLOT: {"exp_kwh": 0.4}})
        self.assertEqual(row["export"]["octopus"], 0.4)


class TheSplitArrives(_Harness):
    """PR 5 on the Pi, 14 Oct 2026: after 8 Sep was applied, two half-hours stayed "EMT's split".
    Their bills were cached cost-only (before v4 4.5.7's bucket fetch); Octopus's split now
    equalled EMT's to the kWh, and the cost agreed, so the diff dropped them and the bill's split
    was never cached."""

    def _cost_only(self, ev):
        self._block(ev=ev, bill=False)
        self.st.upsert_measured_cost(SLOT, mpan="IM", cost_incl=round(KWH * OFF, 6),
                                     cost_excl=round(KWH * OFF, 6), label="STANDARD_RATE", kwh=KWH)
        self.st._conn.commit()

    def test_the_bills_split_at_the_same_figures_is_a_difference(self):
        self._cost_only(ev=KWH)                          # the half-hour wholly on the car
        st = self._preview(_Octopus(node=_node(0.0, KWH)))
        (row,) = st["rows"]
        self.assertFalse(row["bill"]["stored_ev_is_bill"])
        self._apply(st["token"])
        self.assertAlmostEqual(self.st.get_measured_breakdown(SLOT)["ev_kwh"], KWH, places=4)

    def test_no_completed_dispatch_and_no_car_is_still_no_difference(self):
        """GUARD: a house-only half-hour gains nothing from the bill's split."""
        self._cost_only(ev=None)
        self.st._conn.execute("DELETE FROM dispatch_history"); self.st._conn.commit()
        st = self._preview(_Octopus(node=_node(KWH, 0.0)))
        self.assertEqual(st["summary"]["differ"], 0, st["rows"])

    def test_a_held_bill_is_still_not_a_difference(self):
        """GUARD: Octopus's split drops the car; 4.5.23 holds it, so nothing would change."""
        self._cost_only(ev=3.1)
        st = self._preview(_Octopus(node=_node(KWH, 0.0)))
        self.assertEqual(st["summary"]["differ"], 0, st["rows"])


class TheCorrections(_Harness):
    """14 Oct 2026, decided on the Pi: a re-fetch the user asked for may replace their own Cost
    Correction with Octopus's bill (design §5.2). `rate_corrected` was set by Corrections and
    cleared nowhere, so it locked the bill out for good (v4 ROADMAP_archive BL-75 (e)). The
    automatic passes keep their guard."""

    def _corrected(self, cost=0.5):
        self._block(rate_source="corrected", cost=cost, bill=False)

    def _row(self):
        return self.st._conn.execute(
            "SELECT rate_corrected, rate_source, imp_cost FROM blocks WHERE block_start=? AND "
            "meter_id='electricity_main'", (SLOT,)).fetchone()

    def test_a_corrected_half_hour_is_asked_about(self):
        self._corrected()
        oc = _Octopus(node=_node(KWH, 0.0))
        st = self._preview(oc)
        self.assertEqual(oc.asked, [SLOT])
        (row,) = st["rows"]
        self.assertEqual((row["bill"]["stored"], row["bill"]["corrected"]), (0.5, True))
        self.assertEqual(st["summary"]["corrected"], 1)

    def test_apply_replaces_the_correction_with_the_bill(self):
        self._corrected()
        st = self._preview(_Octopus(node=_node(KWH, 0.0)))
        done = self._apply(st["token"])["applied"]
        self.assertEqual((done["bill"], done["corrections"]), (1, 1))
        flag, src, cost = self._row()
        self.assertEqual((flag, src), (0, "settled"))
        self.assertAlmostEqual(cost, round(KWH * OFF, 6), places=6)

    def test_a_bill_the_block_will_not_take_leaves_the_correction(self):
        self._corrected()
        st = self._preview(_Octopus(node=_node(KWH, 0.0)))
        engine._kraken_rate_schedules = {}                 # nothing to snap the rate to
        done = self._apply(st["token"])["applied"]
        self.assertEqual((done["not_settled"], done["corrections"]), (1, 0))
        self.assertEqual(tuple(self._row()), (1, "corrected", 0.5))

    def test_a_correction_that_agrees_with_the_bill_is_no_difference(self):
        """GUARD: nothing would change, so nothing is listed."""
        self._corrected(cost=round(KWH * OFF, 6))
        st = self._preview(_Octopus(node=_node(KWH, 0.0)))
        self.assertEqual(st["summary"]["differ"], 0, st["rows"])

    def test_the_automatic_pass_still_leaves_a_correction(self):
        """GUARD: the settle's flag-clear is opt-in; the default keeps the user's flag."""
        self._corrected()
        self.st.apply_measured_to_main(SLOT, rate=OFF, cost=0.1, cost_exc=0.1)
        self.assertEqual(self._row()[0], 1)


class BeforeTheAgreement(_Harness):
    """PR 5 on the Pi, 14 Oct 2026: 19 Aug 06:30 (the previous tariff) re-fetched, "recovered
    0/1", and the dialog said the range matched Octopus. The device breakdown answers on the
    IOG-SMB agreement only; before it, the single billed cost is asked, as the importer does."""

    COST = {"cost_incl": round(KWH * OFF, 6), "cost_excl": round(KWH * OFF, 6),
            "off_peak": True, "kwh": KWH}

    def test_no_breakdown_falls_back_to_the_billed_cost(self):
        self._block(bill=False)
        oc = _Octopus(node=None); oc.cost_only = {SLOT: self.COST}
        st = self._preview(oc)
        self.assertEqual(oc.asked_cost, (SLOT,))
        (row,) = st["rows"]
        self.assertTrue(row["bill"]["new_bill"])
        self.assertEqual(st["summary"]["no_bill"], 0)
        done = self._apply(st["token"])["applied"]
        self.assertEqual(done["bill"], 1, done)

    def test_a_half_hour_octopus_has_no_bill_for_is_counted(self):
        self._block(bill=False)
        st = self._preview(_Octopus(node=None))
        self.assertEqual((st["summary"]["differ"], st["summary"]["no_bill"]), (0, 1))

    def test_a_breakdown_answer_is_not_asked_twice(self):
        """GUARD: the fallback only covers what the breakdown did not answer."""
        self._block(bill=False)
        oc = _Octopus(node=_node(KWH, 0.0))
        self._preview(oc)
        self.assertEqual(oc.asked_cost, ())


class TheGuards(unittest.TestCase):

    def test_refused_while_a_delete_runs(self):
        job_coordinator.reset_for_tests()
        engine._refetch_state.clear(); engine._refetch_state["status"] = "idle"
        with unittest.mock.patch.object(engine, "delete_in_progress", return_value=True):
            ok, why = engine.refetch_claim()
        self.assertFalse(ok)
        self.assertIn("delete", why)

    def test_the_job_is_declared(self):
        d = job_coordinator.registry()["range_refetch"]
        self.assertEqual(set(d["exclusive_with"]), {"delete", "restore"})


if __name__ == "__main__":
    import unittest.mock
    unittest.main()
