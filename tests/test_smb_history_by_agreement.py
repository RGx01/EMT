"""
Capped history judged by its agreements, whatever the current tariff (capped pricing by agreement
design §4, PR 2; §5 (a) and (c) decided 17 Oct 2026).

The account-level gates read the CURRENT tariff: the history drain, its backlog and the heatmap's
Split view ran only while the account was on IOG-SMB; the floor was the current agreement's start;
the Charts pill counted every half-hour only while on IOG-SMB; the rate lines' cap date was the
current agreement's; and the EV-device rates were built for the current product only. After a
renewal onto a new IOG-SMB agreement the earlier capped history left all of them, and after
leaving IOG-SMB all of it did. Now they read the IOG-SMB agreements discovery holds
(`_smb_windows`), and the EV-device rates are stitched across them.

Discriminating (fail on the parent): test_the_floor_is_the_earliest_agreement,
test_after_leaving_the_drain_finishes_the_capped_history,
test_after_a_renewal_the_drain_reaches_the_first_agreement, test_the_backlog_is_the_windows,
test_the_pill_counts_the_window_every_half_hour, test_the_cap_days_are_the_windows,
test_after_leaving_the_ev_rates_are_still_loaded. Two test new code and fail on the parent only
by name: test_a_day_between_two_windows_is_not_capped, test_each_agreement_takes_its_own_products_ev_rates.
The rest are guards; test_never_on_iog_smb_is_no_window and test_a_single_date_still_gates call
new helpers, so they fail on the parent by name too.
"""
import asyncio
import os
import sys
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine                                    # noqa: E402
import kraken_rates                              # noqa: E402
from block_store import BlockStore               # noqa: E402

MPAN = "2000000000001"
SMB1 = "E-1R-IOG-SMB-FIX-12M-26-03-17-H"
SMB2 = "E-1R-IOG-SMB-FIX-12M-27-03-17-H"
AGILE = "E-1R-AGILE-24-10-01-H"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _ago(days):
    t = datetime.now(timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    return (t - timedelta(days=days)).isoformat()


# Leaving: on IOG-SMB from 40 days ago until 10 days ago, on Agile since.
LEFT = [{"tariff_code": SMB1, "valid_from": _ago(40) + "Z", "valid_to": _ago(10) + "Z"},
        {"tariff_code": AGILE, "valid_from": _ago(10) + "Z", "valid_to": None}]
# A renewal: one IOG-SMB agreement 40 to 20 days ago, a new one since.
RENEWED = [{"tariff_code": SMB1, "valid_from": _ago(40) + "Z", "valid_to": _ago(20) + "Z"},
           {"tariff_code": SMB2, "valid_from": _ago(20) + "Z", "valid_to": None}]


class _Harness(unittest.TestCase):

    def setUp(self):
        self._save = (engine._store, engine._kraken_client, engine._kraken_discovery,
                      engine._kraken_rate_schedules, engine._kraken_current_agreement_from,
                      engine._MEASURED_FETCH_ENABLED, engine._MEASURED_APPLY,
                      engine._MEASURED_HISTORY_DRAIN_PACE)
        self.st = BlockStore(":memory:")
        engine._store = self.st
        engine._MEASURED_FETCH_ENABLED = engine._MEASURED_APPLY = True
        engine._MEASURED_HISTORY_DRAIN_PACE = 0
        engine._kraken_rate_schedules = {}
        self.st._conn.execute(
            "INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, "
            "block_minutes, timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
        self.st._conn.commit()

    def tearDown(self):
        (engine._store, engine._kraken_client, engine._kraken_discovery,
         engine._kraken_rate_schedules, engine._kraken_current_agreement_from,
         engine._MEASURED_FETCH_ENABLED, engine._MEASURED_APPLY,
         engine._MEASURED_HISTORY_DRAIN_PACE) = self._save

    def _account(self, agreements, current):
        engine._kraken_discovery = {"import": {"mpan": MPAN, "tariff_code": current,
                                               "agreements": agreements}}
        cur = [a for a in agreements if a["valid_to"] is None][0]
        engine._kraken_current_agreement_from = cur["valid_from"][:-1]

    def _block(self, slot):
        self.st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "imp_kwh_api, imp_rate, imp_cost, rate_source, rate_corrected) "
            "VALUES (?,?,?,1,0.4,0.4,0.25,0.1,'schedule',0)", (slot, slot, "electricity_main"))
        self.st._conn.commit()


class _Client:
    def __init__(self):
        self.asked = []

    async def recover_device_breakdown(self, mpan, slots, **kw):
        self.asked.extend(slots)
        return {}

    async def recover_measurement_costs(self, mpan, slots, **kw):
        self.asked.extend(slots)
        return {}


class TheWindows(_Harness):

    def test_the_floor_is_the_earliest_agreement(self):
        self._account(RENEWED, SMB2)
        self.assertEqual(engine._measured_floor(), _ago(40))

    def test_one_agreement_keeps_its_floor(self):
        """GUARD: the account of today — one IOG-SMB agreement."""
        engine._kraken_discovery = {"import": {"tariff_code": SMB1, "agreements": [
            {"tariff_code": SMB1, "valid_from": _ago(40) + "Z", "valid_to": None}]}}
        engine._kraken_current_agreement_from = _ago(40)
        self.assertEqual(engine._measured_floor(), _ago(40))

    def test_never_on_iog_smb_is_no_window(self):
        """GUARD: the floor stays the current agreement's."""
        engine._kraken_discovery = {"import": {"tariff_code": AGILE, "agreements": [
            {"tariff_code": AGILE, "valid_from": _ago(40) + "Z", "valid_to": None}]}}
        engine._kraken_current_agreement_from = _ago(40)
        self.assertEqual(engine._smb_windows(), [])
        self.assertFalse(engine._any_intelligent())      # an Agile account: no bill windows
        self.assertEqual(engine._measured_floor(), _ago(40))


class TheDrain(_Harness):

    def test_after_leaving_the_drain_finishes_the_capped_history(self):
        self._account(LEFT, AGILE)
        inside, after = _ago(30), _ago(5)
        self._block(inside)
        self._block(after)
        engine._kraken_client = client = _Client()
        _run(engine.run_measured_history_drain(max_batches=5))
        self.assertIn(inside, client.asked)
        self.assertNotIn(after, client.asked, "the drain asked an Agile half-hour")

    def test_after_a_renewal_the_drain_reaches_the_first_agreement(self):
        self._account(RENEWED, SMB2)
        first = _ago(30)
        self._block(first)
        engine._kraken_client = client = _Client()
        _run(engine.run_measured_history_drain(max_batches=5))
        self.assertIn(first, client.asked)

    def test_the_backlog_is_the_windows(self):
        self._account(LEFT, AGILE)
        self._block(_ago(30))
        self._block(_ago(5))
        self.assertEqual(engine._measured_history_backlog(), 1)


class ThePill(_Harness):

    def test_the_pill_counts_the_window_every_half_hour(self):
        """After leaving, a capped half-hour still waits for its bill (the drain asks it)."""
        self._account(LEFT, AGILE)
        self._block(_ago(12))
        self.assertEqual(engine.settlement_awaiting(self.st, _ago(14))["bill"], 1)

    def test_an_undispatched_half_hour_outside_the_windows_is_not_waiting(self):
        """GUARD: on Agile, nothing outside the window waits for a bill."""
        self._account(LEFT, AGILE)
        self._block(_ago(5))
        self.assertEqual(engine.settlement_awaiting(self.st, _ago(14))["bill"], 0)


class TheRateLines(_Harness):

    def test_the_cap_days_are_the_windows(self):
        self._account(LEFT, AGILE)
        with mock.patch.object(engine, "_iog_site_tz", lambda *a, **k: "UTC"):
            self.assertEqual(engine._chart_cap_from(), [[_ago(40)[:10], _ago(10)[:10]]])

    def test_a_day_between_two_windows_is_not_capped(self):
        """`energy_charts.day_chart_data` gates each day on the windows."""
        import energy_charts
        cap = [["2026-08-26", "2026-09-10"], ["2026-09-20", None]]
        self.assertEqual([energy_charts._is_capped_day(d, cap) for d in (
            "2026-08-25", "2026-08-26", "2026-09-15", "2026-09-20")], [False, True, False, True])

    def test_a_single_date_still_gates(self):
        """GUARD: the callers that pass one start date (the chart tests)."""
        import energy_charts
        self.assertEqual([energy_charts._is_capped_day(d, "2026-08-26") for d in (
            "2026-08-25", "2026-08-26")], [False, True])
        self.assertFalse(energy_charts._is_capped_day("2026-08-26", None))


class _RatesClient:
    """Agile's standard rates now; each IOG-SMB product's EV buckets at its own price."""

    @staticmethod
    def _tariff_to_product_code(tc):
        return "-".join(tc.split("-")[2:-1])

    async def get_unit_rates(self, product, tariff, *, rate_type="standard-unit-rates",
                             period_from=None, period_to=None):
        if "AGILE" in tariff and rate_type == "standard-unit-rates":
            return [{"value_inc_vat": 20.0, "value_exc_vat": 20.0,
                     "valid_from": "2020-01-01T00:00:00Z", "valid_to": None}]
        if "IOG-SMB" in tariff and rate_type.startswith("ev-device"):
            v = (7.0 if "26-03" in tariff else 6.0) * (1 if "off-peak" in rate_type else 4)
            return [{"value_inc_vat": v, "value_exc_vat": v,
                     "valid_from": "2020-01-01T00:00:00Z", "valid_to": None}]
        return []

    async def get_standing_charges(self, *a, **k):
        return []


class TheEvRates(_Harness):

    def test_each_agreement_takes_its_own_products_ev_rates(self):
        kraken_rates._EV_DEVICE_SCHED_CACHE.clear()
        off, peak = _run(kraken_rates.build_ev_device_stitched(
            _RatesClient(), [a for a in RENEWED], vat=kraken_rates.NO_VAT))
        self.assertEqual(off.resolve(_ago(30)), 7.0)        # the first product's
        self.assertEqual(off.resolve(_ago(5)), 6.0)         # the renewal's
        self.assertEqual(peak.resolve(_ago(30)), 28.0)
        self.assertIsNone(off.resolve(_ago(50)), "an EV rate before the first agreement")

    def test_after_leaving_the_ev_rates_are_still_loaded(self):
        self._account(LEFT, AGILE)
        engine._kraken_discovery["import"]["product_code"] = "AGILE-24-10-01"
        engine._kraken_client = _RatesClient()
        kraken_rates._EV_DEVICE_SCHED_CACHE.clear()
        _run(engine._refresh_kraken_rate_schedules())
        sched = engine._kraken_rate_schedules.get("ev_device_off_peak")
        self.assertIsNotNone(sched, "no EV-device rates after leaving IOG-SMB")
        self.assertIsNotNone(sched.resolve(_ago(30)))
        self.assertIsNone(sched.resolve(_ago(5)), "an EV rate on the Agile agreement")


if __name__ == "__main__":
    unittest.main()
