"""
Device cost prices from the GRID-ATTRIBUTED kWh, never from the raw device draw.

Ported from v4 4.5.15 (#473). The fix only — not the heal. v4's
`run_smb_device_cost_clip` repaired history written by the bug; v5 imports only at or
above the 4.5.15 floor (EMT-v5_import_spec §1.1), so every admissible database has
already been healed by v4 and v5 carries no heal of its own.

PASS 2 costs a sub-meter at `imp_kwh_grid x parent_rate` — the portion of the device's
draw the GRID actually supplied. Two v5 re-cost sites priced from raw `imp_kwh`:

    WriteStore.recost_sub_meters         settlement's device re-cost
    WriteStore.repair_device_rate_drift  the recurring drift sweep (was v4's
                                         _smb_device_recost_core body)

`apply_reconcile_verdict` already priced its sub-meters from `imp_kwh_grid`.

WHY IT REACHES THE BILL. The day total is `max(0, main - SUM(devices)) + SUM(devices)`,
algebraically `main` until the clamp fires — which it does once devices exceed the main.
So an over-costed device inflates the bill, not just a device line.

WHY IT IS WORSE IN v5 THAN v4. v4 ran `_smb_device_recost_core` once. v5 runs
`repair_device_rate_drift` on every pass, so without this fix it would re-apply the
raw-kWh cost on every drift it found, indefinitely.

The fixture (battery drew 4 kWh, 0.01 from the grid) makes the two forms differ by
~400x.

REGRESSION TESTS (fail on the unpatched store, checked):
    *.test_cost_derives_from_grid_attributed_kwh
GUARDS (pass before and after — they pin behaviour the fix must not break):
    *.test_unclipped_device_keeps_costing_from_raw_kwh, *.test_kwh_columns_are_never_touched,
    Teeth.*
"""
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from block_store import BlockStore          # noqa: E402

S = "2026-09-06T02:00:00"
OLD, OLD_EXC = 0.3231, 0.3077               # pre-settlement (peak) device rate
NEW, NEW_EXC = 0.0549, 0.0523               # settled parent rate


def _mk_store():
    st = BlockStore(tempfile.mktemp(suffix=".db"))
    st._conn.execute("PRAGMA foreign_keys=OFF")
    return st


def _block(st, meter_id, **cols):
    base = {"block_start": S, "block_end": S, "config_period_id": 1,
            "meter_id": meter_id}
    base.update(cols)
    st._conn.execute("INSERT INTO blocks (%s) VALUES (%s)"
                     % (",".join(base), ",".join("?" * len(base))),
                     tuple(base.values()))
    st._conn.commit()


def _row(st, meter_id):
    return st._conn.execute(
        "SELECT imp_kwh, imp_kwh_grid, imp_rate, imp_cost, imp_cost_exc "
        "FROM blocks WHERE block_start=? AND meter_id=?", (S, meter_id)).fetchone()


class _Base(unittest.TestCase):

    def setUp(self):
        self.st = _mk_store()
        # imp_rate_exc was dropped (step 5b, 10 Oct 2026): a row's ex-VAT rate is derived from
        # its pair, imp_rate x imp_cost_exc / imp_cost, so each row carries both costs.
        _block(self.st, "electricity_main", imp_kwh=1.0, imp_rate=NEW,
               imp_cost=NEW, imp_cost_exc=NEW_EXC, rate_source="settled")
        # battery: drew 4 kWh, only 0.01 of it from the grid (the rest was solar)
        _block(self.st, "sub_meter_battery", imp_kwh=4.0, imp_kwh_grid=0.01,
               imp_rate=OLD, imp_cost=round(0.01 * OLD, 6), imp_cost_exc=round(0.01 * OLD_EXC, 6))
        # a device PASS 2 never clipped: imp_kwh_grid NULL -> keeps costing from raw
        _block(self.st, "sub_meter_nogrid", imp_kwh=0.5, imp_kwh_grid=None,
               imp_rate=OLD, imp_cost=round(0.5 * OLD, 6), imp_cost_exc=round(0.5 * OLD_EXC, 6))

    def _assert_clipped(self):
        b = _row(self.st, "sub_meter_battery")
        self.assertAlmostEqual(b["imp_cost"], round(0.01 * NEW, 6), places=9)
        self.assertAlmostEqual(b["imp_cost_exc"], round(0.01 * NEW_EXC, 6), places=9)

    def _assert_unclipped_falls_back(self):
        n = _row(self.st, "sub_meter_nogrid")
        self.assertAlmostEqual(n["imp_cost"], round(0.5 * NEW, 6), places=9)
        self.assertAlmostEqual(n["imp_cost_exc"], round(0.5 * NEW_EXC, 6), places=9)

    def _assert_kwh_untouched(self):
        b = _row(self.st, "sub_meter_battery")
        self.assertEqual((b["imp_kwh"], b["imp_kwh_grid"]), (4.0, 0.01))


class RecostSubMetersClips(_Base):

    def setUp(self):
        super().setUp()
        self.st.recost_sub_meters(S, NEW, NEW_EXC)

    def test_cost_derives_from_grid_attributed_kwh(self):
        self._assert_clipped()

    def test_unclipped_device_keeps_costing_from_raw_kwh(self):
        self._assert_unclipped_falls_back()

    def test_kwh_columns_are_never_touched(self):
        self._assert_kwh_untouched()


class DriftRepairClips(_Base):

    def setUp(self):
        super().setUp()
        self.assertEqual(self.st.repair_device_rate_drift(), 2)

    def test_cost_derives_from_grid_attributed_kwh(self):
        self._assert_clipped()

    def test_unclipped_device_keeps_costing_from_raw_kwh(self):
        self._assert_unclipped_falls_back()

    def test_kwh_columns_are_never_touched(self):
        self._assert_kwh_untouched()


class Teeth(_Base):
    """Guards on the fixture itself: if these stop holding, the tests above stop
    discriminating between the raw and clipped forms."""

    def test_raw_and_clipped_forms_differ_on_this_fixture(self):
        self.assertGreater(abs(4.0 * NEW - 0.01 * NEW), 0.1)

    def test_raw_costed_device_would_exceed_the_main(self):
        """The condition under which the bill clamp fires."""
        self.assertGreater(4.0 * NEW, 1.0 * NEW)


if __name__ == "__main__":
    unittest.main()
