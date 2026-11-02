"""
The period total is the MAIN METER's cost. Sub-meters attribute it, never add to it.

PROVENANCE. A 4.5.14 user reported a bill summary header of £47.54 on a period
whose own breakdown table read £25.11. Reconstructed from their database:

    compute_period_net          £47.28
    Usage Stats (SUM net_cost)  £47.29
    main + standing - export    £24.86   <- the metered truth
                                +£22.43  overstated by 90%

Both wrong surfaces built the day's import as `max(0, main - subs) + subs`.
That is algebraically `main` -- EXCEPT when the sub-meters out-read the main, and
then the clamp floors the remainder at 0 and the day's import silently becomes
SUM(subs). Their site has a battery: it charges off solar, registering kWh on its
own sub-meter that the grid never supplied. 11 of their 29 days tripped it.

WHY NO TEST CAUGHT IT. The clamp existed in two places -- `compute_period_net`
and a hand-written copy in `_aggregate_block_rows` -- and `test_usage_billing_
multiband` pinned the two against EACH OTHER. An earlier fix had reshaped the
Usage Stats clamp specifically to "match compute_period_net exactly", so the two
implementations agreed on the same wrong number and the comparison passed. Two
implementations are not a cross-check when a maintenance fix is free to make them
agree; the anchor has to be the metered figure, which is what this file asserts.

The clamp is NOT deleted. A displayed "Direct/House" line must not render
negative, so it survives on `meters[...]["imp_cost"]`. Only the totals changed.
"""
import os
import tempfile
import unittest

from block_store import BlockStore

TZ = "Europe/London"
# One local day, 2026-07-01 (BST, UTC+1) -> UTC 2026-06-30T23:00 .. 2026-07-01T23:00
UTC_S, UTC_E = "2026-06-30T23:00:00", "2026-07-01T23:00:00"
BLOCK = "2026-06-30T23:00:00"

MAIN_COST, STANDING, EXPORT = 1.00, 0.50, 0.20
SUB_A, SUB_B = 2.00, 1.00          # subs total 3.00 -- far more than the main


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.join(tempfile.mkdtemp(), "blocks.db")
        self.store = BlockStore(self.tmp)
        with self.store._conn:
            self.store._conn.execute(
                "INSERT INTO config_periods (billing_day, block_minutes, timezone, "
                "currency_symbol, currency_code, effective_from) "
                "VALUES (1, 30, ?, '£', 'GBP', '2026-01-01')", (TZ,))
            self.cp = self.store._conn.execute(
                "SELECT id FROM config_periods LIMIT 1").fetchone()[0]
            for mid, sub in (("electricity_main", 0), ("sub_a", 1), ("sub_b", 1)):
                self.store._conn.execute(
                    "INSERT OR IGNORE INTO meters (meter_id, config_period_id, is_sub_meter, "
                    "parent_meter_id) VALUES (?, ?, ?, ?)",
                    (mid, self.cp, sub, "electricity_main" if sub else None))
            self.store._conn.execute(
                "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                "imp_kwh, imp_rate, imp_cost, exp_kwh, exp_cost, standing_charge) "
                "VALUES (?, '2026-06-30T23:30:00', 'electricity_main', ?, 10.0, 0.10, ?, "
                "2.0, ?, ?)", (BLOCK, self.cp, MAIN_COST, EXPORT, STANDING))
            for mid, cost in (("sub_a", SUB_A), ("sub_b", SUB_B)):
                self.store._conn.execute(
                    "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                    "imp_kwh, imp_kwh_grid, imp_rate, imp_cost, exp_kwh, exp_cost) "
                    "VALUES (?, '2026-06-30T23:30:00', ?, ?, ?, ?, 0.10, ?, 0.0, 0.0)",
                    (BLOCK, mid, self.cp, cost / 0.10, cost / 0.10, cost))

    def tearDown(self):
        self.store._conn.close()
        if os.path.exists(self.tmp):
            os.remove(self.tmp)


class TestPeriodTotalIsMetered(_Base):

    def test_subs_exceeding_main_do_not_inflate_the_total(self):
        """The fixture that reproduces the report: subs £3.00 vs main £1.00."""
        self.assertAlmostEqual(
            self.store.compute_period_net(UTC_S, UTC_E, TZ),
            round(MAIN_COST + STANDING - EXPORT, 2), places=2)

    def test_old_formula_would_have_failed_this_fixture(self):
        """Teeth. The superseded construction on these numbers, spelled out.

        max(0, 1.00 - 3.00) + 3.00 = 3.00, vs a metered 1.00. If this ever equals
        the metered total the fixture has stopped discriminating and the test
        above is no longer evidence -- exactly how the original defect survived.
        """
        old = max(0.0, MAIN_COST - (SUB_A + SUB_B)) + (SUB_A + SUB_B)
        self.assertNotAlmostEqual(old, MAIN_COST, places=2)
        self.assertAlmostEqual(round(old + STANDING - EXPORT, 2), 3.30, places=2)

    def test_agile_negative_main_still_reaches_the_total(self):
        """A genuinely negative main cost is a plunge-price CREDIT, not an error."""
        with self.store._conn:
            self.store._conn.execute(
                "UPDATE blocks SET imp_cost = -0.40 WHERE meter_id='electricity_main'")
        self.assertAlmostEqual(
            self.store.compute_period_net(UTC_S, UTC_E, TZ),
            round(-0.40 + STANDING - EXPORT, 2), places=2)

    def test_no_sub_meters_is_unchanged(self):
        with self.store._conn:
            self.store._conn.execute("DELETE FROM blocks WHERE meter_id LIKE 'sub_%'")
        self.assertAlmostEqual(
            self.store.compute_period_net(UTC_S, UTC_E, TZ),
            round(MAIN_COST + STANDING - EXPORT, 2), places=2)

    def test_ordinary_day_subs_below_main_is_unchanged(self):
        """The clamp never fired here, so this must be byte-identical to before."""
        with self.store._conn:
            self.store._conn.execute(
                "UPDATE blocks SET imp_cost = 5.00 WHERE meter_id='electricity_main'")
        self.assertAlmostEqual(
            self.store.compute_period_net(UTC_S, UTC_E, TZ),
            round(5.00 + STANDING - EXPORT, 2), places=2)


if __name__ == "__main__":
    unittest.main()
