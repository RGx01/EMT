"""
The settlement family's writes — A acceptance 5.

Two operations on the path that records what Octopus actually charged:

    apply_measured_to_main   the bill, onto the main block
    recost_sub_meters        the devices, following the parent rate

THE ASYMMETRY IS THE DESIGN, not an oversight. The main write propagates on
failure — the caller does not guard it — so a failed apply leaves the block
UNSETTLED and the drain revisits it. The device re-cost runs afterwards in its own
transaction inside a `try` that logs and continues, because the bill's figure for
the main row is authoritative and must not be rolled back for the sake of a
derived device cost.

What makes that safe is `repair_device_rate_drift()` (A12), which detects exactly
this divergence on every pass. Before it was reinstated, a failure here left the
main meter settled and its devices on the pre-settlement rate with
`rate_source='settled'` already stamped — so nothing would ever look again.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from block_store import BlockStore          # noqa: E402

S = "2026-09-01T00:00:00"


def _mk_store():
    st = BlockStore(tempfile.mktemp(suffix=".db"))
    st._conn.execute("PRAGMA foreign_keys=OFF")
    return st


def _block(st, meter_id="electricity_main", **cols):
    base = {"block_start": S, "block_end": S, "config_period_id": 1,
            "meter_id": meter_id, "imp_kwh": 5.0, "imp_rate": 0.32,
            "rate_corrected": 0}
    base.update(cols)
    st._conn.execute("INSERT INTO blocks (%s) VALUES (%s)"
                     % (",".join(base), ",".join("?" * len(base))),
                     tuple(base.values()))
    st._conn.commit()


def _row(st, meter_id="electricity_main"):
    return st._conn.execute(
        "SELECT imp_rate, imp_cost, imp_cost_exc, "
        "rate_source, needs_review, review_reason, "
        "imp_kwh_ev, imp_rate_ev, imp_cost_ev, imp_ev_band, imp_home_band, "
        "imp_kwh "
        "FROM blocks WHERE block_start=? AND meter_id=?", (S, meter_id)).fetchone()


_BILL = dict(rate=0.0549, cost=0.2745, cost_exc=0.2614,
             kwh_ev=2.0, rate_ev=0.0549, cost_ev=0.1098,
             ev_band="off_peak", home_band="off_peak")


class ApplyMeasuredToMain(unittest.TestCase):

    def setUp(self):
        self.st = _mk_store()
        _block(self.st, needs_review=1, review_reason="ambiguous ceiling")

    def test_it_writes_the_bill(self):
        self.assertEqual(self.st.apply_measured_to_main(S, **_BILL), 1)
        r = _row(self.st)
        self.assertAlmostEqual(r["imp_rate"], 0.0549)
        self.assertAlmostEqual(r["imp_cost"], 0.2745)
        self.assertEqual(r["rate_source"], "settled")
        # exc_source dropped (step 5b, 10 Oct 2026); the bill's ex-VAT £ is what is stored.
        self.assertAlmostEqual(r["imp_cost_exc"], 0.2614)

    def test_the_bill_leaves_a_stored_review_flag(self):
        """BL-74: the flag is history; settlement no longer clears it."""
        self.st.apply_measured_to_main(S, **_BILL)
        r = _row(self.st)
        self.assertEqual((r["needs_review"], r["review_reason"]), (1, "ambiguous ceiling"))

    def test_the_kwh_is_never_touched(self):
        """The bill sets the PRICE. kWh is the meter's, and imp_cost is stored as
        billed rather than recomputed from it."""
        self.st.apply_measured_to_main(S, **_BILL)
        self.assertAlmostEqual(_row(self.st)["imp_kwh"], 5.0)

    def test_no_split_is_NULL_not_zero(self):
        """NULL means 'no split'; 0.0 would mean 'a split of nothing'. The jobs
        that back-attribute a late-arriving dispatch select on
        `imp_kwh_ev IS NULL`, so a zero here would hide the block from them."""
        bill = dict(_BILL, kwh_ev=None, cost_ev=None)
        self.st.apply_measured_to_main(S, **bill)
        r = _row(self.st)
        self.assertIsNone(r["imp_kwh_ev"])
        self.assertIsNone(r["imp_cost_ev"])

    def test_the_arguments_are_keyword_only(self):
        """Ten values, three of them costs and three of them rates, on the one
        write that records what the supplier charged. `cost` and `cost_exc`
        transposed positionally is a silently wrong bill."""
        with self.assertRaises(TypeError):
            self.st.apply_measured_to_main(S, 0.0549, 0.2745)    # type: ignore

    def test_it_is_meter_scoped(self):
        self.assertEqual(
            self.st.apply_measured_to_main(S, meter_id="sub_meter_ev", **_BILL), 0)


class RecostSubMeters(unittest.TestCase):

    def setUp(self):
        self.st = _mk_store()
        _block(self.st, imp_rate=0.0549)                       # already settled
        _block(self.st, "sub_meter_ev", imp_kwh=4.0, imp_rate=0.32)
        _block(self.st, "sub_meter_battery", imp_kwh=2.0, imp_rate=0.32)

    def test_every_physical_sub_meter_follows_the_parent(self):
        self.assertEqual(self.st.recost_sub_meters(S, 0.0549, 0.0523), 2)
        for m in ("sub_meter_ev", "sub_meter_battery"):
            r = _row(self.st, m)
            self.assertAlmostEqual(r["imp_rate"], 0.0549)
            # exc_source dropped (step 5b, 10 Oct 2026); the ex-VAT £ follows the parent rate.
            self.assertAlmostEqual(r["imp_cost_exc"],
                                   round((4.0 if m == "sub_meter_ev" else 2.0) * 0.0523, 6))

    def test_cost_is_recomputed_from_the_devices_own_kwh(self):
        self.st.recost_sub_meters(S, 0.0549, 0.0523)
        self.assertAlmostEqual(_row(self.st, "sub_meter_ev")["imp_cost"],
                               round(4.0 * 0.0549, 6))
        self.assertAlmostEqual(_row(self.st, "sub_meter_battery")["imp_cost"],
                               round(2.0 * 0.0549, 6))

    def test_the_device_kwh_is_never_touched(self):
        self.st.recost_sub_meters(S, 0.0549, 0.0523)
        self.assertAlmostEqual(_row(self.st, "sub_meter_ev")["imp_kwh"], 4.0)

    def test_the_main_meter_is_excluded(self):
        before = _row(self.st)["imp_rate"]
        self.st.recost_sub_meters(S, 0.99, 0.94)
        self.assertAlmostEqual(_row(self.st)["imp_rate"], before)

    def test_an_export_meter_is_excluded(self):
        """`imp_kwh IS NOT NULL` — an export meter has no import to re-cost."""
        st = _mk_store()
        _block(st, "sub_meter_export", imp_kwh=None, exp_kwh=3.0)
        self.assertEqual(st.recost_sub_meters(S, 0.0549, 0.0523), 0)

    def test_a_synthetic_ev_account_is_a_no_op(self):
        st = _mk_store()
        _block(st)
        self.assertEqual(st.recost_sub_meters(S, 0.0549, 0.0523), 0)


class TheAsymmetryAndItsNet(unittest.TestCase):

    def test_the_two_writes_are_separate_transactions(self):
        """Not one. A device failure must not roll back the bill."""
        st = _mk_store()
        _block(st, needs_review=1)
        _block(st, "sub_meter_ev", imp_kwh=4.0, imp_rate=0.32)
        st.apply_measured_to_main(S, **_BILL)
        self.assertEqual(_row(st)["rate_source"], "settled")
        self.assertAlmostEqual(_row(st, "sub_meter_ev")["imp_rate"], 0.32,
                               msg="the main write must not touch devices")

    def test_a_failed_device_recost_leaves_the_bill_applied(self):
        st = _mk_store()
        _block(st)
        _block(st, "sub_meter_ev", imp_kwh=4.0, imp_rate=0.32)
        st.apply_measured_to_main(S, **_BILL)
        original = BlockStore.recost_sub_meters
        try:
            def _boom(self, *a, **k):
                raise sqlite3.OperationalError("database is locked")
            BlockStore.recost_sub_meters = _boom
            with self.assertRaises(sqlite3.OperationalError):
                st.recost_sub_meters(S, 0.0549, 0.0523)
        finally:
            BlockStore.recost_sub_meters = original
        self.assertEqual(_row(st)["rate_source"], "settled",
                         "the authoritative bill must survive a device failure")
        self.assertAlmostEqual(_row(st, "sub_meter_ev")["imp_rate"], 0.32)

    def test_and_the_drift_repair_converges_it(self):
        """The net under the asymmetry. Without A12 this state would persist:
        rate_source='settled' is already stamped, so the drain never returns."""
        st = _mk_store()
        _block(st)
        _block(st, "sub_meter_ev", imp_kwh=4.0, imp_rate=0.32)
        st.apply_measured_to_main(S, **_BILL)
        self.assertEqual(st.repair_device_rate_drift(), 1)
        self.assertAlmostEqual(_row(st, "sub_meter_ev")["imp_rate"], 0.0549)


class Surfaces(unittest.TestCase):

    def test_both_are_writes(self):
        from store_types import ReadStore, WriteStore
        for name in ("apply_measured_to_main", "recost_sub_meters"):
            self.assertTrue(hasattr(WriteStore, name), name)
            self.assertFalse(hasattr(ReadStore, name),
                             "%s on the read surface has no thread guard" % name)


class NoRawConnectionLeftInTheFamily(unittest.TestCase):

    def test_the_settlement_functions_are_clean(self):
        import ast
        tree = ast.parse(open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read())
        want = {"apply_measured_to_block", "apply_measured_settled",
                "measure_settled_dispatched_blocks", "run_measured_history_drain",
                "_measured_history_backlog", "run_settled_ev_split_heal"}
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in want:
                raw = [x.lineno for x in ast.walk(n)
                       if isinstance(x, ast.Attribute) and x.attr == "_conn"]
                self.assertEqual(raw, [], "%s still uses _conn at %s" % (n.name, raw))


if __name__ == "__main__":
    unittest.main()
