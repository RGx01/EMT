"""
Every Intelligent half-hour is asked for its bill, capped (IOG-SMB) or not (18 Oct 2026, the user;
settlement surfaces design §9.3).

On every Intelligent tariff a dispatch can re-band a half-hour to off-peak and EMT knows only the
dispatches it saw, so only Octopus's cost is final there (#203). But EMT asked only where the
agreement was IOG-SMB — the drain walked the IOG-SMB windows, the floor was the earliest IOG-SMB
agreement's start, and off IOG-SMB the live pass asked only for dispatched half-hours — so a
half-hour on an uncapped Intelligent agreement before the move waited for a bill that never came
(208 on the soak, July and August). Now the drain walks every Intelligent agreement's window
(`_intelligent_windows`): the breakdown on IOG-SMB, the billed cost elsewhere (`bill_kind_at`).
Off IOG-SMB it leaves imported history alone — an API import already holds Octopus's cost. The
floor is the earliest Intelligent agreement's start; the pill counts what the drain asks; and a
bill before the current agreement is applied only where the schedule is stitched across the
agreements (v4 4.5.5's reason for its floor).

Discriminating (fail on the parent): test_every_intelligent_agreement_is_a_window,
test_the_floor_is_the_earliest_intelligent_agreement, test_the_drain_asks_an_uncapped_half_hour_for_its_cost,
test_the_backlog_counts_it, test_the_pill_counts_it, test_with_the_stitch_it_is_applied (the parent's
floor never reached it). The rest are guards — test_a_bill_before_the_agreement_waits_for_the_stitch
passes on the parent only because its floor never reached the half-hour either;
test_a_published_agreement_is_not_a_window calls a new helper, so fails there by name.
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine                                    # noqa: E402
from block_store import BlockStore               # noqa: E402

MPAN = "2000000000001"
INTELLI = "E-1R-INTELLI-VAR-22-10-14-H"
SMB = "E-1R-IOG-SMB-FIX-12M-26-03-17-H"
AGILE = "E-1R-AGILE-24-10-01-H"


def _ago(days):
    t = datetime.now(timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    return (t - timedelta(days=days)).isoformat()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# Uncapped Intelligent 60 to 20 days ago, then IOG-SMB.
MOVED = [{"tariff_code": INTELLI, "valid_from": _ago(60) + "Z", "valid_to": _ago(20) + "Z"},
         {"tariff_code": SMB, "valid_from": _ago(20) + "Z", "valid_to": None}]


class _Client:
    def __init__(self):
        self.cost, self.breakdown = [], []

    async def recover_measurement_costs(self, mpan, slots, **kw):
        self.cost.extend(slots); return {}

    async def recover_device_breakdown(self, mpan, slots, **kw):
        self.breakdown.extend(slots); return {}


class _Harness(unittest.TestCase):

    def setUp(self):
        self._save = (engine._store, engine._kraken_client, engine._kraken_discovery,
                      engine._kraken_rate_schedules, engine._kraken_current_agreement_from,
                      engine._MEASURED_FETCH_ENABLED, engine._MEASURED_APPLY,
                      engine._MEASURED_HISTORY_DRAIN_PACE, engine._import_schedule_covers_history)
        self.st = BlockStore(":memory:")
        engine._store = self.st
        engine._MEASURED_FETCH_ENABLED = engine._MEASURED_APPLY = True
        engine._MEASURED_HISTORY_DRAIN_PACE = 0
        engine._kraken_rate_schedules = {}
        engine._import_schedule_covers_history = True
        engine._kraken_discovery = {"import": {"mpan": MPAN, "tariff_code": SMB, "agreements": MOVED}}
        engine._kraken_current_agreement_from = _ago(20)
        self.st._conn.execute(
            "INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, "
            "block_minutes, timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
        self.st._conn.commit()

    def tearDown(self):
        (engine._store, engine._kraken_client, engine._kraken_discovery,
         engine._kraken_rate_schedules, engine._kraken_current_agreement_from,
         engine._MEASURED_FETCH_ENABLED, engine._MEASURED_APPLY,
         engine._MEASURED_HISTORY_DRAIN_PACE, engine._import_schedule_covers_history) = self._save

    def _block(self, slot, source=None):
        live = source is None
        self.st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "imp_kwh_api, imp_rate, imp_cost, rate_source, rate_corrected, source) "
            "VALUES (?,?,?,1,0.4,?,0.25,0.1,'schedule',0,?)",
            (slot, slot, "electricity_main", 0.4 if live else None, source))
        self.st._conn.commit()

    def _drain(self):
        engine._kraken_client = c = _Client()
        _run(engine.run_measured_history_drain(max_batches=10))
        return c


class TheWindows(_Harness):

    def test_every_intelligent_agreement_is_a_window(self):
        self.assertEqual(engine._intelligent_windows(),
                         [(_ago(60), _ago(20), False), (_ago(20), None, True)])

    def test_the_floor_is_the_earliest_intelligent_agreement(self):
        self.assertEqual(engine._measured_floor(), _ago(60))

    def test_a_published_agreement_is_not_a_window(self):
        """GUARD."""
        engine._kraken_discovery = {"import": {"mpan": MPAN, "tariff_code": AGILE, "agreements": [
            {"tariff_code": AGILE, "valid_from": _ago(60) + "Z", "valid_to": None}]}}
        self.assertEqual(engine._intelligent_windows(), [])
        self.assertFalse(engine._any_intelligent())


class TheDrain(_Harness):

    def test_the_drain_asks_an_uncapped_half_hour_for_its_cost(self):
        slot = _ago(40)
        self._block(slot)
        c = self._drain()
        self.assertIn(slot, c.cost)                 # the billed cost, off IOG-SMB
        self.assertNotIn(slot, c.breakdown)

    def test_off_iog_smb_imported_history_is_not_asked(self):
        """GUARD: an API import already holds Octopus's cost."""
        slot = _ago(40)
        self._block(slot, source="imported_api")
        c = self._drain()
        self.assertNotIn(slot, c.cost + c.breakdown)

    def test_on_iog_smb_imported_history_is_still_asked(self):
        """GUARD: its bill carries the House/EV split."""
        slot = _ago(10)
        self._block(slot, source="imported_api")
        self.assertIn(slot, self._drain().breakdown)

    def test_the_backlog_counts_it(self):
        self._block(_ago(40))
        self._block(_ago(41), source="imported_api")
        self.assertEqual(engine._measured_history_backlog(), 1)


class TwoYears(_Harness):
    """Octopus keeps two years of half-hourly data (the API import's `_API_RETENTION_DAYS`), so the
    asking stops there (18 Oct 2026, the user). Discriminating against the commit before the bound:
    all three (the last reads the live pass's wiring)."""

    def setUp(self):
        super().setUp()
        old = [{"tariff_code": INTELLI, "valid_from": _ago(1000) + "Z", "valid_to": _ago(20) + "Z"},
               {"tariff_code": SMB, "valid_from": _ago(20) + "Z", "valid_to": None}]
        engine._kraken_discovery = {"import": {"mpan": MPAN, "tariff_code": SMB, "agreements": old}}

    def test_a_half_hour_older_than_two_years_is_not_asked(self):
        old, recent = _ago(800), _ago(40)
        self._block(old); self._block(recent)
        c = self._drain()
        self.assertNotIn(old, c.cost)
        self.assertIn(recent, c.cost)

    def test_nor_is_it_backlog(self):
        self._block(_ago(800))
        self.assertEqual(engine._measured_history_backlog(), 0)

    def test_the_live_pass_is_bounded_too(self):
        import inspect
        self.assertIn("get_dispatched_blocks_awaiting_measure(_ask_floor(_measured_floor()))",
                      inspect.getsource(engine.measure_settled_dispatched_blocks))


class ThePill(_Harness):

    def test_the_pill_counts_it(self):
        self._block(_ago(5))                       # IOG-SMB, the window it always counted
        self._block(_ago(40))                      # uncapped Intelligent, undispatched
        self.assertEqual(engine.settlement_awaiting(self.st, _ago(50))["bill"], 2)


class TheApply(_Harness):

    def _cached(self, slot):
        self.st.upsert_measured_cost(slot, mpan=MPAN, cost_incl=0.1, cost_excl=0.1,
                                     label="STANDARD_RATE", kwh=0.4)

    def test_a_bill_before_the_agreement_waits_for_the_stitch(self):
        slot = _ago(40)
        self._block(slot); self._cached(slot)
        engine._import_schedule_covers_history = False     # the stitch failed
        applied = []
        save = engine.apply_measured_to_block
        engine.apply_measured_to_block = lambda bs, **k: applied.append(bs) or True
        try:
            engine.apply_measured_settled()
        finally:
            engine.apply_measured_to_block = save
        self.assertNotIn(slot, applied)

    def test_with_the_stitch_it_is_applied(self):
        """GUARD."""
        slot = _ago(40)
        self._block(slot); self._cached(slot)
        applied = []
        save = engine.apply_measured_to_block
        engine.apply_measured_to_block = lambda bs, **k: applied.append(bs) or True
        try:
            engine.apply_measured_settled()
        finally:
            engine.apply_measured_to_block = save
        self.assertIn(slot, applied)


if __name__ == "__main__":
    unittest.main()
