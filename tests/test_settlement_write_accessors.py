"""
The settlement family's write — A acceptance 5.

    apply_measured_to_main   the bill, onto the main block — and, since engine step 4b, its devices

Until engine step 4b (25 Oct 2026) the devices were re-costed afterwards by `recost_sub_meters`, in a
transaction of their own inside a `try` that logged and continued, so a failed device write left
the main settled and its devices on the old rate — what a recurring drift repair (A12) was
reinstated to catch. settle() now prices the devices with the main (at the rate of the legs their
kWh sits in, design §4 (b)), and the one write takes both: if it fails, nothing is stamped and the
block stays a candidate. With every writer through settle() (step 4c, 4d) the drift repair went in
engine step 4e.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from block_store import BlockStore          # noqa: E402
import ev_split                             # noqa: E402

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
        + ev_split.sql_kwh("blocks") + " AS ev_kwh, "
        + ev_split.sql_cost("blocks") + " AS ev_cost, "
        "imp_kwh "
        "FROM blocks WHERE block_start=? AND meter_id=?", (S, meter_id)).fetchone()


# The bill's EV/house split is its segments (step 5c, 10 Oct 2026): 2 kWh EV + 3 kWh house.
_SEGS = [(2.0, 0.0549, 0.0523, "off_peak", "ev"), (3.0, 0.0549, 0.0523, "off_peak", "house")]
_BILL = dict(rate=0.0549, cost=0.2745, cost_exc=0.2614, segments=_SEGS)


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
        # ...and its split, as segments, in the same write (BL-79)
        self.assertAlmostEqual(r["ev_kwh"], 2.0)
        self.assertAlmostEqual(r["ev_cost"], round(2.0 * 0.0549, 6))

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
        "no EV segment" (ev_split.sql_kwh IS NULL; the imp_kwh_ev column until step 5c), so
        a zero here would hide the block from them. A house-only bill writes no EV segment."""
        bill = dict(_BILL, segments=[(5.0, 0.0549, 0.0523, "off_peak", "house")])
        self.st.apply_measured_to_main(S, **bill)
        r = _row(self.st)
        self.assertIsNone(r["ev_kwh"])
        self.assertIsNone(r["ev_cost"])

    def test_the_arguments_are_keyword_only(self):
        """Three values, two of them costs, on the one
        write that records what the supplier charged. `cost` and `cost_exc`
        transposed positionally is a silently wrong bill."""
        with self.assertRaises(TypeError):
            self.st.apply_measured_to_main(S, 0.0549, 0.2745)    # type: ignore

    def test_it_is_meter_scoped(self):
        self.assertEqual(
            self.st.apply_measured_to_main(S, meter_id="sub_meter_ev", **_BILL), 0)


class TheDevicesGoWithTheMain(unittest.TestCase):
    """Engine step 4b: one transaction. Discriminating — the parent had no `devices`."""

    def test_the_devices_are_written_with_the_main(self):
        st = _mk_store()
        _block(st)
        _block(st, "sub_meter_ev", imp_kwh=4.0, imp_rate=0.32)
        st.apply_measured_to_main(S, **_BILL, devices=[("sub_meter_ev", 3.5, 0.0549, 0.19215, 0.183)])
        r = _row(st, "sub_meter_ev")
        self.assertAlmostEqual(r["imp_rate"], 0.0549)
        self.assertAlmostEqual(r["imp_cost"], 0.19215)
        self.assertAlmostEqual(r["imp_cost_exc"], 0.183)
        self.assertAlmostEqual(r["imp_kwh"], 4.0)              # the device's own read is kept

    def test_a_failed_device_write_stamps_nothing(self):
        st = _mk_store()
        _block(st)
        _block(st, "sub_meter_ev", imp_kwh=4.0, imp_rate=0.32)
        with self.assertRaises(sqlite3.Error):
            # a value sqlite cannot bind fails the device UPDATE inside the one transaction
            st.apply_measured_to_main(S, **_BILL, devices=[("sub_meter_ev", object(), 0.05, 0.1, 0.1)])
        self.assertNotEqual(_row(st)["rate_source"], "settled")
        self.assertAlmostEqual(_row(st, "sub_meter_ev")["imp_rate"], 0.32)

    def test_without_devices_the_main_alone(self):
        """GUARD: a synthetic-EV account (no sub-meter blocks) writes the main as before."""
        st = _mk_store()
        _block(st)
        st.apply_measured_to_main(S, **_BILL)
        self.assertEqual(_row(st)["rate_source"], "settled")


class Surfaces(unittest.TestCase):

    def test_both_are_writes(self):
        from store_types import ReadStore, WriteStore
        for name in ("apply_measured_to_main",):
            self.assertTrue(hasattr(WriteStore, name), name)
            self.assertFalse(hasattr(ReadStore, name),
                             "%s on the read surface has no thread guard" % name)


class NoRawConnectionLeftInTheFamily(unittest.TestCase):

    def test_the_settlement_functions_are_clean(self):
        import ast
        tree = ast.parse(open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read())
        want = {"apply_measured_to_block", "apply_measured_settled",
                "measure_settled_dispatched_blocks", "run_measured_history_drain",
                "_measured_history_backlog"}            # the split heal: removed 16 Oct 2026
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in want:
                raw = [x.lineno for x in ast.walk(n)
                       if isinstance(x, ast.Attribute) and x.attr == "_conn"]
                self.assertEqual(raw, [], "%s still uses _conn at %s" % (n.name, raw))


if __name__ == "__main__":
    unittest.main()
