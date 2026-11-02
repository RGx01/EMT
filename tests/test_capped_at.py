"""
Capped pricing judged per half-hour by its own agreement (capped pricing by agreement design §4,
PR 1; §5 (b) decided 17 Oct 2026).

Every check that decided capped pricing read the account's tariff TODAY: the EV-device rates being
loaded (`_capped_schedules`). On an account now on IOG-SMB, a re-run of the seam over a half-hour
from its earlier, uncapped Intelligent agreement priced it capped — the current product's EV
rates reach back past the agreement — and the BL-34 band relabel skipped it. `capped_at(start)`
asks the half-hour's own agreement (`split_at`'s question); the seam and the mixed rebuild price
capped only where it says so AND the EV rates are loaded; the relabel skips only where it says so.

Discriminating (fail on the parent): test_a_pre_agreement_half_hour_is_relabelled,
test_a_pre_agreement_mixed_bill_is_not_rebuilt_capped. The seam's own tests (TheSeam, with
test_a_pre_agreement_half_hour_is_priced_uncapped) went with `_apply_iog_split` in engine step 4e;
settle_facts carries the same rule, `capped_at(bs) and _capped_schedules()` (test_one_ev_rule).
The rest are guards (test_capped_at_is_split_at fails on the parent only by name: new code).
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine                                    # noqa: E402
from block_store import BlockStore               # noqa: E402
from kraken_rates import RateSchedule            # noqa: E402

S, E = "2026-01-01T13:00:00", "2026-01-01T13:30:00"     # a day-rate half-hour

# The account moved from an uncapped Intelligent tariff to IOG-SMB; `AGREEMENT` is the move.
def _agreements(move):
    return [{"tariff_code": "E-1R-INTELLI-VAR-22-10-14-H", "valid_from": "2025-01-01T00:00:00Z",
             "valid_to": move},
            {"tariff_code": "E-1R-IOG-SMB-FIX-12M-26-03-17-H", "valid_from": move,
             "valid_to": None}]


BEFORE = "2026-06-01T00:00:00Z"      # S is before the agreement
AFTER = "2025-12-01T00:00:00Z"       # S is on it


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Harness(unittest.TestCase):

    def setUp(self):
        self._save = (engine._store, engine._kraken_rate_schedules, engine._kraken_discovery,
                      engine._import_schedule_covers_history)
        self.st = BlockStore(":memory:")
        engine._store = self.st
        engine._import_schedule_covers_history = True
        engine._kraken_rate_schedules = {
            "import": RateSchedule([
                ("2026-01-01T00:00:00", "2026-01-01T05:30:00", 7.0),    # night 7.0p
                ("2026-01-01T05:30:00", "2026-01-01T23:30:00", 30.0),   # day  30.0p
                ("2026-01-01T23:30:00", "2026-01-02T05:30:00", 7.0)]),
            "ev_device_off_peak": RateSchedule([("2025-01-01T00:00:00", None, 5.0)]),
            "ev_device_peak": RateSchedule([("2025-01-01T00:00:00", None, 25.0)])}
        self.st._conn.execute(
            "INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, "
            "block_minutes, timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
        self.st._conn.commit()

    def tearDown(self):
        (engine._store, engine._kraken_rate_schedules, engine._kraken_discovery,
         engine._import_schedule_covers_history) = self._save

    def _account(self, move):
        engine._kraken_discovery = {"import": {
            "tariff_code": "E-1R-IOG-SMB-FIX-12M-26-03-17-H", "agreements": _agreements(move)}}

    def _history(self, kind, energy=None):
        self.st._conn.execute(
            "INSERT INTO dispatch_history (slot_start, kind, provider, source, energy_kwh, "
            "first_seen, last_seen, raw_start, raw_end) VALUES (?,?,?,?,?,?,?,?,?)",
            (S, kind, "test", None, energy, S, S, S, E))
        self.st._conn.commit()


class TheRelabel(_Harness):
    """BL-34 in `reconcile_dispatch_overlay`: a smart charge priced off-peak whose segments still
    read the day bands. The relabel makes the labels agree with the rate, except on a capped
    half-hour, whose bands legitimately mix."""

    def setUp(self):
        super().setUp()
        self.st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "imp_rate, imp_cost, rate_source, rate_corrected) "
            "VALUES (?,?,?,1,3.0,0.07,0.21,'schedule',0)", (S, E, "electricity_main"))
        self.st.set_block_segments(S, "electricity_main", [
            (2.0, 0.07, 0.07, "peak", "ev"), (1.0, 0.07, 0.07, "day", "house")])
        self.st.upsert_dispatch_slot(S, off_peak=True, source="smart-charge")
        for kind in ("planned", "started"):
            self._history(kind)
        self._history("completed", -2.0)

    def _bands(self):
        _run(engine.reconcile_dispatch_overlay())
        return {(r["attribution"], r["band"]) for r in self.st.get_block_segments(
            S, "electricity_main")}

    def test_a_pre_agreement_half_hour_is_relabelled(self):
        self._account(BEFORE)
        self.assertEqual(self._bands(), {("ev", "off_peak"), ("house", "off_peak")})

    def test_a_half_hour_on_the_agreement_takes_the_caps_legs(self):
        """REVERSED by engine step 4c: the relabel kept a capped half-hour's mixed labels (EV peak,
        house day on segments priced at 7p). The reconcile settles it with settle(): a started
        smart charge within the cap is off-peak, EV and house, at the off-peak rate."""
        self._account(AFTER)
        self.assertEqual(self._bands(), {("ev", "off_peak"), ("house", "off_peak")})


class TheMixedBill(_Harness):
    """`apply_measured_to_block` with a 'mixed' bill rebuilds the block's four capped bands from
    the cap (an over-cap boundary) — through settle()'s `iog_cap.price_slot` since engine step 4b.
    Only on a capped half-hour."""

    def setUp(self):
        super().setUp()
        self.st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "imp_kwh_api, imp_rate, imp_cost, rate_source) "
            "VALUES (?,?,?,1,3.0,3.0,0.07,0.21,'reconciled')", (S, E, "electricity_main"))
        self.st.set_block_segments(S, "electricity_main", [
            (2.0, 0.07, 0.0667, "off_peak", "ev"), (1.0, 0.07, 0.0667, "off_peak", "house")])
        self._history("completed", -2.0)

    def _apply(self):
        import iog_cap
        calls = []
        save = iog_cap.price_slot
        iog_cap.price_slot = lambda *a, **k: calls.append(k) or {
            "segments": [(1.0, 0.05, "off_peak", "ev"), (1.0, 0.25, "peak", "ev"),
                         (1.0, 0.30, "day", "house")],
            "classification": {"ev": "mixed", "house": "day"}}
        # Octopus's buckets at both bands: the mixed band (engine step 2b)
        self.st.upsert_measured_breakdown(S, home_kwh=1.0, home_rate=0.30, ev_kwh=2.0, ev_rate=0.05)
        try:
            engine.apply_measured_to_block(S, cost_incl=0.60, cost_excl=0.57, label="mixed")
        finally:
            iog_cap.price_slot = save
        return calls

    def test_a_pre_agreement_mixed_bill_is_not_rebuilt_capped(self):
        self._account(BEFORE)
        self.assertEqual(self._apply(), [])

    def test_a_mixed_bill_on_the_agreement_is(self):
        """GUARD."""
        self._account(AFTER)
        self.assertEqual(len(self._apply()), 1)


class TheQuestion(unittest.TestCase):
    """GUARD: one question, two names."""

    def test_capped_at_is_split_at(self):
        save = engine._kraken_discovery
        try:
            for move in (BEFORE, AFTER):
                engine._kraken_discovery = {"import": {"agreements": _agreements(move)}}
                self.assertEqual(engine.capped_at(S), engine.split_at(S))
        finally:
            engine._kraken_discovery = save


if __name__ == "__main__":
    unittest.main()
