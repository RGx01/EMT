"""
ADAPTER DIFFERENTIAL — v4's pricing wrapper vs reprice.py's, over the shared core.

WHY THIS IS THE RIGHT TEST
    v5 may adopt `reprice.py` as its pricing core. Both it and v4 already bottom
    out in the SAME function:

        v4:          _apply_iog_split -> compute_iog_split -> iog_cap.price_slot
        reprice.py:  reprice_block                         -> iog_cap.price_slot

    `price_slot` IS the four-rate rule. Adopting reprice.py therefore puts the
    RULE at no risk whatsoever. What it puts at risk is the ADAPTER — whether the
    two wrappers hand `price_slot` the same six arguments.

    That is a question about two pure functions, so it needs no database, no bill
    and no soak. Drive both with the same scenario and compare. Any divergence
    here is a real difference between the wrappers.

    (The bill cannot referee this. It is authoritative for what you were CHARGED;
    it is not a statement of the four-rate rule, and where Octopus's bucket
    attribution is off it is the pre-reconcile prediction that follows the rule.
    So the rule is the referee, and the rule is `price_slot`.)

DELIBERATELY OUT OF SCOPE
    * ex-VAT. v4 derives it in `_apply_import_exc`, outside this seam; reprice.py
      derives it internally from `vat`. Nothing to compare.
    * `settled_cost` anchoring and the carbon substrate — reprice.py has them and
      v4's split path does not, so there is no counterpart. Those need tests
      against the design spec, not a differential.
    * UNITS. `compute_iog_split` takes PENCE from the RateSchedules and converts
      (the 2026-08-18 `imp_rate_ev=6.9` break); `reprice_block` takes £ by
      contract. This file feeds each the units it asks for, which means the
      conversion is EXERCISED here but not PROVEN — v5's adapter must own it, and
      `_sanitise_inc_units` has no equivalent on the reprice side.
"""
import itertools
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import iog_cap                      # noqa: E402
import reprice                      # noqa: E402

# Staging IOG-SMB-TOU, in PENCE as the Octopus API and RateSchedule carry them.
# Confirmed against the bill: the four-bucket breakdown on a real 50-day corpus
# showed exactly {0.05493, 0.323092} £/kWh for house.
P_HOUSE_OFF, P_HOUSE_DAY = 5.493, 32.3092
P_EV_OFF, P_EV_PEAK = 5.493, 32.3092

SEG_RATE_TOLERANCE = 1e-5      # see TestWhereTheyDiffer.test_house_segment_rate_differs
BS, BE = "2026-08-15T01:00:00", "2026-08-15T01:30:00"
DAY_BS, DAY_BE = "2026-08-15T13:00:00", "2026-08-15T13:30:00"


class FakeSchedule:
    """Duck-typed RateSchedule. `compute_iog_split` documents that it takes the
    schedule objects rather than importing RateSchedule, precisely so this is
    possible. Values are PENCE, as the real one holds them."""

    def __init__(self, off, day=None, off_peak_at=None):
        self._off, self._day = off, (day if day is not None else off)
        self._off_peak_at = off_peak_at          # None -> derive from the bounds

    def is_empty(self):
        return False

    def day_rate_bounds(self, ts):
        return (min(self._off, self._day), max(self._off, self._day))

    def resolve(self, ts):
        return self._off

    def is_off_peak(self, ts):
        if self._off_peak_at is not None:
            return self._off_peak_at
        return True


def _v4(bs, be, *, grid, ev, overlay, boost, capped, boundary, off_peak_window):
    """v4's adapter exactly as `_apply_iog_split` calls it."""
    return iog_cap.compute_iog_split(
        bs, be, chosen_kwh=grid, ev_kwh=ev, overlay_rate=overlay,
        is_boost=boost, capped=capped, boundary=boundary,
        import_sched=FakeSchedule(P_HOUSE_OFF, P_HOUSE_DAY, off_peak_at=off_peak_window),
        ev_off_sched=FakeSchedule(P_EV_OFF) if capped else None,
        ev_peak_sched=FakeSchedule(P_EV_PEAK) if capped else None)


def _v5(bs, be, *, grid, ev, overlay, boost, capped, boundary, off_peak_window):
    """The same scenario through reprice.py. The mapping IS the adapter contract
    v5 has to implement, so it is written out rather than hidden in a helper."""
    if capped:
        rates = {"house_off": round(P_HOUSE_OFF / 100.0, 6),
                 "house_day": round(P_HOUSE_DAY / 100.0, 6),
                 "ev_off": round(P_EV_OFF / 100.0, 6),
                 "ev_peak": round(P_EV_PEAK / 100.0, 6)}
        window, elig, bound = off_peak_window, (reprice.BUMP if boost else reprice.SMART), boundary
    else:
        # v4's uncapped branch FORCES these: whole slot on the overlay rate, the
        # window true and the boost flag off. v5 must reproduce that, not just the
        # rates — it is the part of the contract that is easiest to miss.
        rates = {k: overlay for k in ("house_off", "house_day", "ev_off", "ev_peak")}
        window, elig, bound = True, reprice.SMART, None
    return reprice.reprice_block(
        block_start=bs, block_end=be, grid_kwh=grid,
        ev=reprice.resolve_ev_energy(
            reprice.IOG_CAPPED if capped else reprice.IOG_UNCAPPED,
            grid_kwh=grid, dispatch_ev_kwh=ev),
        rates=rates, settled_cost=None, vat=0.05,
        in_off_peak_window=window, ev_eligibility=elig, boundary=bound)


def _segs(v4res, v5res):
    a = [(round(k, 6), round(r, 6), b, at) for (k, r, b, at) in (v4res.get("segments") or [])]
    b = [(round(s.kwh, 6), round(s.inc_rate, 6), s.band, s.attribution)
         for s in (v5res.get("segments") or [])]
    return sorted(a), sorted(b)


class TestAdapterDifferential(unittest.TestCase):
    """The grid. Every axis that changes which branch of price_slot is taken."""

    GRID = list(itertools.product(
        (False, True),                       # capped
        (False, True),                       # boost
        (False, True),                       # in off-peak window
        (0.0, 0.4, 2.0),                     # ev kWh: none, partial, the whole slot.
                                             # ev > grid is NOT here — it is physically
                                             # impossible and the two differ on it; see
                                             # TestWhereTheyDiffer below.
        (2.0,),                              # grid kWh
        # cap boundary is (boundary_slot, boundary_frac) from cap_usage — never an
        # ISO string. None = cap never reached; == this slot with a fraction = the
        # straddle; an earlier slot = wholly over-cap.
        (None, (BS, 0.35), ("2026-08-15T00:00:00", 0.0)),
    ))

    def _compare(self, bs, be, capped, boost, window, ev, grid, boundary):
        ctx = (f"capped={capped} boost={boost} window={window} "
               f"ev={ev} grid={grid} boundary={boundary}")
        kw = dict(grid=grid, ev=ev, overlay=round(P_HOUSE_OFF / 100.0, 6),
                  boost=boost, capped=capped, boundary=boundary,
                  off_peak_window=window)
        a, b = _v4(bs, be, **kw), _v5(bs, be, **kw)
        self.assertIsNotNone(a, "v4 returned None for " + ctx)

        self.assertAlmostEqual(a["imp_cost"], b["cost"], places=6, msg="cost: " + ctx)
        self.assertAlmostEqual(a["imp_rate"], b["rate"], places=6, msg="rate: " + ctx)

        ev_out = b["devices"]["ev"]
        if a["imp_kwh_ev"] is None:
            self.assertLessEqual(ev_out["kwh"], 1e-9, "ev kwh: " + ctx)
        else:
            self.assertAlmostEqual(a["imp_kwh_ev"], ev_out["kwh"], places=6,
                                   msg="ev kwh: " + ctx)
            self.assertAlmostEqual(a["imp_cost_ev"], ev_out["cost"], places=6,
                                   msg="ev cost: " + ctx)
            self.assertAlmostEqual(a["imp_rate_ev"], ev_out["rate"], places=6,
                                   msg="ev rate: " + ctx)

        sa, sb = _segs(a, b)
        if sa:
            self.assertEqual([(k, bd, at) for (k, _r, bd, at) in sa],
                             [(k, bd, at) for (k, _r, bd, at) in sb],
                             "segment shape (kwh/band/attribution): " + ctx)
            # Rates may differ in the last place: reprice.py reconciles the house
            # slice (_reconcile_house) where v4 reads the segment rates straight
            # out of _slot_segments. See TestWhereTheyDiffer for the bound.
            for (_k, ra, _b, _a), (_k2, rb, _b2, _a2) in zip(sa, sb):
                self.assertAlmostEqual(ra, rb, delta=SEG_RATE_TOLERANCE,
                                       msg="segment rate: " + ctx)

    def test_night_slot_grid(self):
        for capped, boost, window, ev, grid, boundary in self.GRID:
            with self.subTest(capped=capped, boost=boost, window=window,
                              ev=ev, boundary=boundary):
                self._compare(BS, BE, capped, boost, window, ev, grid, boundary)

    def test_day_slot_grid(self):
        """Same grid on an out-of-window afternoon slot — where house day rate and
        EV peak rate actually differ from off-peak."""
        for capped, boost, window, ev, grid, boundary in self.GRID:
            if boundary and boundary[0] == BS:
                boundary = (DAY_BS, boundary[1])      # straddle THIS slot
            with self.subTest(capped=capped, boost=boost, window=window,
                              ev=ev, boundary=boundary):
                self._compare(DAY_BS, DAY_BE, capped, boost, window, ev, grid, boundary)


class TestAgileAndOtherTariffs(unittest.TestCase):
    """WHICH TARIFFS REACH THIS SEAM AT ALL.

    `price_slot` is the IOG four-rate rule, and almost nothing else gets there:

      * IOG capped   -> compute_iog_split resolves four rates -> price_slot.
      * IOG uncapped -> the fall-through: all four rates equal to the resolved
        overlay, window forced True, boost forced False. Totals unchanged.
      * Agile / flat / fixed / Eco7 IMPORT -> `_apply_iog_split` returns at its
        `if ev_kwh <= 1e-9: return` gate, because a non-IOG account has no
        dispatch. Priced by the schedule resolver instead, never here.
      * Agile WITH dispatch (an account migrated off IOG, whose history still
        carries dispatch rows) -> capped=False, because there are no ev_device
        schedules. compute_iog_split says so: "Agile never caps". That is the
        uncapped fall-through, which the grid above already covers.
      * EXPORT -> never. See TestExportIsNotInScope.
    """

    def _both(self, pence, *, grid=2.0, ev=1.0):
        ov = round(pence / 100.0, 6)
        a = iog_cap.compute_iog_split(
            DAY_BS, DAY_BE, chosen_kwh=grid, ev_kwh=ev, overlay_rate=ov,
            is_boost=False, capped=False, boundary=None,
            import_sched=FakeSchedule(pence, pence),
            ev_off_sched=None, ev_peak_sched=None)
        b = reprice.reprice_block(
            block_start=DAY_BS, block_end=DAY_BE, grid_kwh=grid,
            ev=reprice.resolve_ev_energy(reprice.IOG_UNCAPPED, grid_kwh=grid,
                                         dispatch_ev_kwh=ev),
            rates={k: ov for k in ("house_off", "house_day", "ev_off", "ev_peak")},
            settled_cost=None, vat=0.05, in_off_peak_window=True,
            ev_eligibility=reprice.SMART, boundary=None)
        return ov, a, b

    def test_agile_plunge_zero_and_spike_agree(self):
        """Agile goes NEGATIVE on a plunge. Both paths multiply through unharmed —
        worth pinning, because a signed rate is the obvious place for a max(0, ...)
        guard to quietly eat the customer's credit."""
        for pence in (-4.5, -0.001, 0.0, 15.0, 98.7):
            with self.subTest(pence=pence):
                ov, a, b = self._both(pence)
                self.assertAlmostEqual(a["imp_cost"], b["cost"], places=9)
                self.assertAlmostEqual(b["cost"], round(2.0 * ov, 6), places=6,
                                       msg="the uncapped total must stay kwh x rate")
                self.assertAlmostEqual(a["imp_rate"], b["rate"], places=9)

    def test_a_negative_rate_is_not_clamped_to_zero(self):
        ov, a, b = self._both(-4.5)
        self.assertLess(b["cost"], 0.0, "a plunge-priced slot must stay a credit")
        self.assertLess(b["devices"]["ev"]["rate"], 0.0)


class TestExportIsNotInScope(unittest.TestCase):
    """Export never reaches this seam, in EITHER implementation — and that is a
    scoping fact v5 has to plan around, not an oversight to test past.

    v4 prices export with a flat multiply (`exp_ch["cost"] = chosen_exp * exp_rate`):
    no split, no segments, no bands. `_dispatch_overlay_rate` refuses it on the
    first line ("Import channel only (export is never dispatch-adjusted)") and
    `_apply_iog_split` only ever touches `imp_ch`.

    reprice.py has no export concept AT ALL. So its docstring claim to be "the
    single, pure per-block pricing model ... the one model every code path
    conforms to" is true only of dispatched IOG import. Adopting it as v5's
    pricing core leaves export, and every non-IOG import tariff, still needing
    the schedule-resolver path.
    """

    def test_reprice_has_no_export_concept(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "reprice.py"), encoding="utf-8") as fh:
            src = fh.read().lower()
        for token in ("export", "exp_kwh", "exp_rate", "exp_cost"):
            self.assertNotIn(token, src,
                             "reprice.py has grown an export concept — this test was "
                             "recording that it had none, so revisit the v5 scoping note")

    def test_the_overlay_refuses_export_on_the_first_line(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "engine.py"), encoding="utf-8") as fh:
            eng = fh.read()
        self.assertIn('if channel_name != "import" or _store is None or not block_start:', eng,
                      "_dispatch_overlay_rate no longer refuses export up front")


class TestWhereTheyDiffer(unittest.TestCase):
    """The grid above agrees on all 144 reachable scenarios. There is exactly one
    input where the two disagree, and it is worth stating precisely."""

    def test_the_single_divergence_is_ev_above_grid(self):
        """More dispatch energy than the slot drew from the grid. `reprice_block`
        clips EV to the grid; `compute_iog_split` does not, and bills the excess.
        On a 2 kWh slot claiming 3.5 kWh of EV at the day rate that is 1.13 vs
        0.65 — v4 charging for energy the meter never saw."""
        kw = dict(grid=2.0, ev=3.5, overlay=round(P_HOUSE_OFF / 100.0, 6),
                  boost=False, capped=True, boundary=None, off_peak_window=False)
        v4, v5 = _v4(DAY_BS, DAY_BE, **kw), _v5(DAY_BS, DAY_BE, **kw)
        ev_off = round(P_EV_OFF / 100.0, 6)
        # boundary=None means the cap was never reached, so the EV energy is
        # within-cap and prices off-peak whatever the house window says.
        self.assertAlmostEqual(v4["imp_cost"], round(3.5 * ev_off, 6), places=6)
        self.assertAlmostEqual(v5["cost"], round(2.0 * ev_off, 6), places=6)
        self.assertGreater(v4["imp_cost"], v5["cost"],
                           "v4 bills EV energy the meter never recorded")

    def test_house_segment_rate_differs_in_the_last_place(self):
        """The other divergence, and the only one inside the reachable space:
        on a cap-boundary straddle the house DAY segment comes back 0.323093 from
        reprice.py against 0.323092 from v4 — 1e-6 on the rate, with the block
        TOTAL identical.

        Cause: reprice.py closes the house slice with `_reconcile_house` so the
        segments sum to the block cost; v4 takes the segment rates straight from
        `_slot_segments`. v4 already documents this scale at this seam — price_slot
        says the decomposition is "sub-penny vs the old independent arithmetic
        (<=3e-6, settlement-jitter scale); the bill (2dp) is unchanged".

        Pinned rather than tolerated silently: if it ever grows past 1e-5 the grid
        above fails, and that would be a real change of behaviour.
        """
        kw = dict(grid=2.0, ev=0.4, overlay=round(P_HOUSE_OFF / 100.0, 6),
                  boost=False, capped=True, boundary=(DAY_BS, 0.35),
                  off_peak_window=False)
        v4, v5 = _v4(DAY_BS, DAY_BE, **kw), _v5(DAY_BS, DAY_BE, **kw)
        self.assertAlmostEqual(v4["imp_cost"], v5["cost"], places=6,
                               msg="the TOTAL must be identical")
        day4 = [r for (_k, r, b, a) in v4["segments"]
                if b == "day" and a == "house"]
        day5 = [s.inc_rate for s in v5["segments"]
                if s.band == "day" and s.attribution == "house"]
        self.assertTrue(day4 and day5, "expected a house/day segment in this scenario")
        delta = abs(day4[0] - day5[0])
        self.assertGreater(delta, 0.0, "if these now agree exactly, delete this test")
        self.assertLess(delta, SEG_RATE_TOLERANCE)

    def test_v4_cannot_reach_it_because_both_call_sites_clip(self):
        """So this is not a live v4 bug — it is a difference in WHERE the clip
        lives. v4 clips in the engine, at each of the two call sites:

            _iog_slot_ev_kwh      ->  return min(ev, float(chosen_kwh))
            apply_measured_to_block -> _disp = min(_disp, kwh)

        reprice.py clips inside the core instead (ev_clip = min(clipped, grid)),
        so a third caller cannot forget. v4 has already been bitten once at this
        seam — block_store.py cites 'the legacy 4.3.0 capped compute_iog_split
        bug, watch #15'. This test pins the two clip sites so that if either is
        ever removed, something says so.
        """
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "engine.py"), encoding="utf-8") as _fh:
            eng = _fh.read()
        self.assertTrue(re.search(r"return min\(ev, float\(chosen_kwh\)\)", eng),
                        "_iog_slot_ev_kwh no longer grid-clips")
        self.assertTrue(re.search(r"_disp = min\(_disp, kwh\)", eng),
                        "apply_measured_to_block no longer grid-clips")
        with open(os.path.join(root, "reprice.py"), encoding="utf-8") as _fh:
            rp = _fh.read()
        self.assertIn("ev_clip = min(max(0.0, float(ev.get(\"clipped\", 0.0))), grid_kwh)", rp,
                      "reprice_block no longer clips in the core")


class TestUncappedIsByteIdentical(unittest.TestCase):
    """compute_iog_split's docstring promises the uncapped path leaves the total
    'byte-identical to today'. If reprice.py is to replace it, it must keep that
    promise — an uncapped account's settled history must not move by a penny."""

    def test_uncapped_total_is_the_overlay_rate_times_kwh(self):
        for ev in (0.0, 0.4, 2.0):
            kw = dict(grid=2.0, ev=ev, overlay=0.2812, boost=False, capped=False,
                      boundary=None, off_peak_window=True)
            a, b = _v4(BS, BE, **kw), _v5(BS, BE, **kw)
            self.assertAlmostEqual(a["imp_cost"], round(2.0 * 0.2812, 6), places=6)
            self.assertAlmostEqual(b["cost"], round(2.0 * 0.2812, 6), places=6)


class TestTheContractThatIsEasyToGetWrong(unittest.TestCase):
    """v4's uncapped branch does not just pass equal rates — it also forces the
    window True and the boost flag False. A v5 adapter that forwards the real
    values instead would diverge on exactly the boosted out-of-window slots."""

    def test_forwarding_boost_on_the_uncapped_path_diverges(self):
        kw = dict(grid=2.0, ev=1.0, overlay=round(P_HOUSE_OFF / 100.0, 6),
                  boost=True, capped=False, boundary=None, off_peak_window=False)
        v4 = _v4(DAY_BS, DAY_BE, **kw)
        naive = reprice.reprice_block(
            block_start=DAY_BS, block_end=DAY_BE, grid_kwh=2.0,
            ev=reprice.resolve_ev_energy(reprice.IOG_UNCAPPED, grid_kwh=2.0,
                                         dispatch_ev_kwh=1.0),
            rates={k: round(P_HOUSE_OFF / 100.0, 6)
                   for k in ("house_off", "house_day", "ev_off", "ev_peak")},
            settled_cost=None, vat=0.05,
            in_off_peak_window=False,               # forwarded, not forced
            ev_eligibility=reprice.BUMP, boundary=None)
        # Same money either way (all four rates are equal), but the BAND differs —
        # which is what the billing summary renders.
        self.assertAlmostEqual(v4["imp_cost"], naive["cost"], places=6)
        self.assertEqual((v4.get("classification") or {}).get("ev"), "off_peak")
        self.assertEqual(naive["bands"]["ev"], "peak",
                         "if this ever matches, the forcing no longer matters and "
                         "this test has outlived its point")


if __name__ == "__main__":
    unittest.main()
