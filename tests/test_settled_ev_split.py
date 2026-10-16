"""Regression: the settled bill's EV bucket must not be discarded when no dispatch
data exists for the slot.

`apply_measured_to_block` caps the EV quantity at the car's own completed-dispatch
session — measured is king (4.5.9), because Octopus's EV_DEVICE bucket over-attributes
when another load (a home battery) draws concurrently inside the dispatch window.

But a dispatch ceiling of ZERO is ambiguous: either the car did not charge (a live slot
we were watching), or we hold no dispatch data for that time at all. Imported history is
always the latter — Octopus serves a short rolling dispatch window and no history. The
old code treated both as "no EV", so a fresh install settled its IOG-SMB era from the
bill with the right total cost and a house-only split, throwing away the very bucket the
bill had supplied: 438 slots settled, 111 carrying EV, 0 with imp_kwh_ev.
"""
import os
import sys
import types
import unittest
from unittest.mock import MagicMock

eio = types.ModuleType("energy_engine_io")
eio.ensure_dir = lambda *a, **kw: None
eio.load_json = lambda *a, **kw: a[1] if len(a) > 1 else {}
eio.save_json_atomic = lambda *a, **kw: None
eio.save_file = lambda *a, **kw: None
sys.modules["energy_engine_io"] = eio
ec = types.ModuleType("energy_charts")
sys.modules["energy_charts"] = ec
hc = types.ModuleType("ha_client"); hc.HAClient = MagicMock
sys.modules["ha_client"] = hc

from block_store import BlockStore
from kraken_rates import RateSchedule

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine
import ev_split

SLOT = "2026-09-01T02:00:00"          # inside the IOG off-peak window
CFG = {"meters": {"electricity_main": {"meta": {
    "timezone": "Europe/London", "billing_day": 1, "block_minutes": 30,
    "currency_symbol": "£", "currency_code": "GBP", "sub_meter": False}}}}


class _Base(unittest.TestCase):
    def setUp(self):
        self._s, self._sch, self._d = (engine._store, engine._kraken_rate_schedules,
                                       engine._kraken_discovery)
        st = BlockStore(":memory:"); st.insert_config_period(CFG)
        engine._store = st
        engine._kraken_discovery = {"import": {"mpan": "IM", "tariff_code": "E-1R-IOG-SMB-X-B",
                                               "product_code": "IOG-SMB-X"}}
        _night = ("2026-08-31T22:30:00", "2026-09-01T04:30:00")
        _day = ("2026-09-01T04:30:00", "2026-09-01T22:30:00")
        engine._kraken_rate_schedules = {
            "import": RateSchedule([_night + (5.493,), _day + (32.3092,)]),
            "ev_device_off_peak": RateSchedule([_night + (5.493,)]),
            "ev_device_peak": RateSchedule([_day + (32.3092,)]),
        }

    def tearDown(self):
        (engine._store, engine._kraken_rate_schedules,
         engine._kraken_discovery) = self._s, self._sch, self._d

    def _block(self, *, kwh=6.0, source="imported_api", api=None, corrected=0):
        engine._store._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
            "imp_kwh, imp_rate, imp_cost, source, imp_kwh_api, rate_corrected) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (SLOT, "2026-09-01T02:30:00", "electricity_main", 1, kwh, 0.05493,
             round(kwh * 0.05493, 6), source, api, corrected))
        engine._store._conn.commit()

    def _bill(self, *, ev_kwh, cost=0.329580):
        engine._store.upsert_measured_cost(SLOT, mpan="IM", cost_incl=cost,
                                           cost_excl=round(cost / 1.05, 6),
                                           label="OFF_PEAK", kwh=6.0)
        engine._store.upsert_measured_breakdown(
            SLOT, home_kwh=round(6.0 - ev_kwh, 6), home_rate=0.05493,
            ev_kwh=ev_kwh, ev_rate=0.05493, mpan="IM")

    def _dispatch(self, kwh):
        engine._store.record_dispatch_history(SLOT, "completed", provider="Myenergi",
                                              source="unknown", energy_kwh=-abs(kwh))

    def _ev(self):
        # the EV split is the block's EV segments (step 5c, 10 Oct 2026)
        r = engine._store._conn.execute(
            "SELECT " + ev_split.sql_kwh("b") + " AS ev FROM blocks b "
            "WHERE block_start=? AND meter_id='electricity_main'", (SLOT,)).fetchone()
        return r["ev"]

    def _apply(self):
        return engine.apply_measured_to_block(SLOT, cost_incl=0.329580,
                                              cost_excl=0.313886, label="OFF_PEAK")


class TestAmbiguousCeiling(_Base):
    def test_imported_no_dispatch_takes_the_bill_split(self):
        """THE regression: no dispatch data anywhere -> the bill is the only evidence."""
        self._block(); self._bill(ev_kwh=4.5)
        self.assertTrue(self._apply())
        self.assertAlmostEqual(self._ev() or 0, 4.5, places=6,
                               msg="the bill's EV bucket must not be discarded")

    def test_bill_ev_is_grid_clipped(self):
        self._block(kwh=3.0); self._bill(ev_kwh=4.5)
        self._apply()
        self.assertAlmostEqual(self._ev() or 0, 3.0, places=6,
                               msg="EV can never exceed the slot's grid import")

    def test_imported_with_no_ev_bucket_stays_house(self):
        self._block(); self._bill(ev_kwh=0.0)
        self._apply()
        self.assertIsNone(self._ev(), "no EV in the bill -> house-only")


class TestTheBillsSplitIsFinal(_Base):
    """Bill and split paths design §6 (b), 16 Oct 2026 — the tower's final stage: on the IOG-SMB
    agreement Octopus's split IS the split, clipped to the meter. This replaced v4 4.5.9's
    'measured is king' (the completed dispatch capped the bill, against a battery charging
    concurrently inflating the EV bucket); both tests below asserted the cap until then.
    Discriminating: each fails on the parent."""

    def test_a_zero_dispatch_record_does_not_override_the_bill(self):
        self._block(source="kraken_api", api=6.0)
        self._bill(ev_kwh=4.5)
        self._dispatch(0.0)
        self._apply()
        self.assertAlmostEqual(self._ev() or 0, 4.5, places=6)

    def test_the_dispatch_does_not_cap_the_bill(self):
        self._block(source="kraken_api", api=6.0)
        self._bill(ev_kwh=4.5)
        self._dispatch(2.0)
        self._apply()
        self.assertAlmostEqual(self._ev() or 0, 4.5, places=6)


class TestBeforeTheAgreementTheDispatchStays(_Base):
    """Before the IOG-SMB agreement Octopus's breakdown is not the bill (24 Aug 2026 05:30 BST:
    bill £0.0879 off-peak, breakdown £0.5169 day rate), so the dispatch rule stays there.
    GUARDS: the parent applied the 4.5.9 cap everywhere, which these cases share."""

    def setUp(self):
        super().setUp()
        engine._kraken_discovery = {"import": {"mpan": "IM", "tariff_code": "E-1R-IOG-SMB-X-B",
            "agreements": [{"tariff_code": "E-1R-INTELLI-VAR-B",
                            "valid_from": "2025-01-01T00:00:00Z", "valid_to": "2026-09-02T00:00:00Z"},
                           {"tariff_code": "E-1R-IOG-SMB-X-B",
                            "valid_from": "2026-09-02T00:00:00Z", "valid_to": None}]}}

    def test_the_dispatch_caps_it(self):
        self._block(source="kraken_api", api=6.0)
        self._bill(ev_kwh=4.5)
        self._dispatch(2.0)
        self._apply()
        self.assertAlmostEqual(self._ev() or 0, 2.0, places=6)

    def test_no_dispatch_no_ev(self):
        self._block(source="kraken_api", api=6.0)
        self._bill(ev_kwh=4.5)
        self._dispatch(0.0)
        self._apply()
        self.assertIsNone(self._ev())


class TestSettledEvSplitHeal(_Base):
    def test_heals_a_block_settled_house_only(self):
        self._block(); self._bill(ev_kwh=4.5)
        engine._store._conn.execute(
            "UPDATE blocks SET rate_source='settled' WHERE block_start=?", (SLOT,))
        engine._store._conn.commit()
        self.assertIsNone(self._ev())
        r = engine.run_settled_ev_split_heal()
        self.assertEqual(r.get("healed"), 1, r)
        self.assertAlmostEqual(self._ev() or 0, 4.5, places=6)

    def test_is_idempotent(self):
        self._block(); self._bill(ev_kwh=4.5)
        engine._store._conn.execute(
            "UPDATE blocks SET rate_source='settled' WHERE block_start=?", (SLOT,))
        engine._store._conn.commit()
        engine.run_settled_ev_split_heal()
        self.assertEqual(engine.run_settled_ev_split_heal().get("healed"), 0,
                         "steady state must be a no-op")

    def test_never_touches_a_corrected_block(self):
        self._block(corrected=1); self._bill(ev_kwh=4.5)
        engine._store._conn.execute(
            "UPDATE blocks SET rate_source='corrected' WHERE block_start=?", (SLOT,))
        engine._store._conn.commit()
        self.assertEqual(engine.run_settled_ev_split_heal().get("healed"), 0)
        self.assertIsNone(self._ev())


class TestTheHealDoesNotLoop(_Base):
    """4 Oct 2026: on the 12th Pi soak the heal "restored" the same 20–23 blocks every hour,
    changing nothing. Its query selects "bill has EV, block has none"; but where the dispatch
    ceiling is a KNOWN zero the settle keeps the block house-only (measured is king), so
    re-settling rewrote the same split every pass and counted it as healed. The heal now asks
    the settle's own rule (_measured_ev_kwh) first, and counts only a split actually written."""

    def _settled(self, **kw):
        self._block(**kw)
        engine._store._conn.execute(
            "UPDATE blocks SET rate_source='settled' WHERE block_start=?", (SLOT,))
        engine._store._conn.commit()

    def test_a_known_zero_ceiling_heals_once_then_stops(self):
        """Since §6 (b) the bill's split wins over a zero dispatch record, so this block heals —
        once: the next passes find a split and select nothing. (Until 16 Oct 2026: left alone.)"""
        self._settled(source="kraken_api", api=6.0)
        self._bill(ev_kwh=4.5)
        self._dispatch(0.0)
        self.assertEqual(engine.run_settled_ev_split_heal().get("healed"), 1)
        calls = []
        real = engine.apply_measured_to_block
        engine.apply_measured_to_block = lambda *a, **k: calls.append(a) or real(*a, **k)
        try:
            for _ in range(3):                                  # three hourly passes
                self.assertEqual(engine.run_settled_ev_split_heal().get("healed"), 0)
        finally:
            engine.apply_measured_to_block = real
        self.assertEqual(calls, [], "it re-settled a block that already has its split")
        self.assertAlmostEqual(self._ev() or 0, 4.5, places=6)

    def test_the_heal_and_the_settle_agree(self):
        """The rule the heal asks is the rule the settle applies — one function, so the two
        cannot drift. Each case's settled EV equals what _measured_ev_kwh predicted."""
        cases = [  # (block kwargs, bill ev, dispatch kwh or None, expected ev)
            (dict(), 4.5, None, 4.5),                                  # unknown ceiling: the bill
            (dict(kwh=3.0), 4.5, None, 3.0),                           # grid-clipped
            (dict(source="kraken_api", api=6.0), 4.5, 0.0, 4.5),        # §6 (b): the bill
            (dict(source="kraken_api", api=6.0), 4.5, 2.0, 4.5),        # §6 (b): not capped
        ]
        for blk, bill_ev, disp, want in cases:
            with self.subTest(blk=blk, disp=disp):
                engine._store._conn.execute("DELETE FROM blocks")
                engine._store._conn.execute("DELETE FROM block_segments")
                engine._store._conn.execute("DELETE FROM dispatch_history")
                engine._store._conn.execute("DELETE FROM measured_cost")
                engine._store._conn.commit()
                self._block(**blk); self._bill(ev_kwh=bill_ev)
                if disp is not None:
                    self._dispatch(disp)
                row = engine._store.get_block_measured_inputs(SLOT)
                predicted = engine._measured_ev_kwh(engine._store, SLOT, row, row["imp_kwh"])
                self._apply()
                self.assertAlmostEqual(predicted, want, places=6)
                self.assertAlmostEqual(self._ev() or 0.0, want, places=6)

    def test_a_positive_ceiling_is_still_healed(self):
        """The case the heal exists for still heals — to the bill's split (§6 (b))."""
        self._settled(source="kraken_api", api=6.0)
        self._bill(ev_kwh=4.5)
        self._dispatch(2.0)
        self.assertEqual(engine.run_settled_ev_split_heal().get("healed"), 1)
        self.assertAlmostEqual(self._ev() or 0, 4.5, places=6)


if __name__ == "__main__":
    unittest.main()
