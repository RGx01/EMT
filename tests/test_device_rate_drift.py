"""
`repair_device_rate_drift` — the invariant A11 nearly lost.

THE INVARIANT: PASS 2 costs every sub-meter at its parent main block's rate, so a
physical device block (ev_charger / battery) must carry the same `imp_rate` as the
main block for its slot.

WHY IT NEEDS ENFORCING: `apply_measured_to_block` applies the bill in TWO
transactions. The main row (rate, cost, split, `rate_source='settled'`) is
written unguarded — it propagates on failure, so the block stays unsettled and the
drain revisits it. The sub-meter re-cost that follows is in its own `try`, logs and
continues. So a failure there leaves the main meter settled and its devices on the
pre-settlement (often peak) rate, with `rate_source='settled'` already stamped —
which means the drain will never look at that block again.

HOW IT WAS NEARLY LOST: this predicate was the body of v4's
`_smb_device_recost_core`, whose wrapper was marker-gated and said "retires in
v5.0.0". A11 retired the wrapper on the import-floor argument, correctly, and took
the body with it — on the strength of the docstring, without reading the WHERE
clause. The clause has no era filter, no date range and no tariff gate: it is a
live drift detector, and v4 only ever ran it once.

Ungated and recurring is therefore STRICTER than v4, which is what the refactor is
supposed to produce.
"""
import os
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
        "SELECT imp_rate, imp_cost, imp_cost_exc, imp_kwh "
        "FROM blocks WHERE block_start=? AND meter_id=?", (S, meter_id)).fetchone()


class DriftIsRepaired(unittest.TestCase):

    def setUp(self):
        self.st = _mk_store()
        # the settled main row (imp_rate_exc dropped, step 5b: its ex-VAT rate derives from
        # the pair, 0.0549 x 0.523 / 0.549 = 0.0523)
        _block(self.st, "electricity_main", imp_kwh=10.0, imp_rate=0.0549,
               imp_cost=0.549, imp_cost_exc=0.523, rate_source="settled")

    def test_a_stale_device_is_recosted_to_the_parent(self):
        """The exact failure: main settled off-peak, device left at peak."""
        _block(self.st, "sub_meter_ev", imp_kwh=4.0, imp_rate=0.3231,
               imp_cost=round(4.0 * 0.3231, 6), imp_cost_exc=round(4.0 * 0.3077, 6))
        self.assertEqual(self.st.repair_device_rate_drift(), 1)
        r = _row(self.st, "sub_meter_ev")
        self.assertAlmostEqual(r["imp_rate"], 0.0549)
        self.assertAlmostEqual(r["imp_cost"], round(4.0 * 0.0549, 6))
        self.assertAlmostEqual(r["imp_cost_exc"], round(4.0 * 0.0523, 6))

    def test_the_device_kwh_is_never_touched(self):
        """kWh is the grid-clipped device draw and is not this method's business.
        Only the priced rate layer moves."""
        _block(self.st, "sub_meter_ev", imp_kwh=4.0, imp_rate=0.3231)
        self.st.repair_device_rate_drift()
        self.assertAlmostEqual(_row(self.st, "sub_meter_ev")["imp_kwh"], 4.0)

    def test_it_is_idempotent(self):
        _block(self.st, "sub_meter_ev", imp_kwh=4.0, imp_rate=0.3231)
        self.assertEqual(self.st.repair_device_rate_drift(), 1)
        self.assertEqual(self.st.repair_device_rate_drift(), 0,
                         "the steady state must be a no-op")

    def test_agreement_within_tolerance_is_left_alone(self):
        """0.0001 — a sub-hundredth-of-a-penny difference is rounding, not drift."""
        _block(self.st, "sub_meter_ev", imp_kwh=4.0, imp_rate=0.054905)
        self.assertEqual(self.st.repair_device_rate_drift(), 0)

    def test_every_physical_sub_meter_is_repaired_not_just_the_ev(self):
        """`meter_id != 'electricity_main'` — a battery drifts the same way and for
        the same reason."""
        _block(self.st, "sub_meter_ev", imp_kwh=4.0, imp_rate=0.3231)
        _block(self.st, "sub_meter_battery", imp_kwh=2.0, imp_rate=0.3231)
        self.assertEqual(self.st.repair_device_rate_drift(), 2)
        for m in ("sub_meter_ev", "sub_meter_battery"):
            self.assertAlmostEqual(_row(self.st, m)["imp_rate"], 0.0549)

    def test_an_export_meter_is_excluded(self):
        """`imp_kwh IS NOT NULL` is what excludes export — it has no import to
        re-cost, and writing an import rate onto it would be meaningless."""
        _block(self.st, "sub_meter_export", exp_kwh=3.0, exp_rate=0.15)
        self.assertEqual(self.st.repair_device_rate_drift(), 0)

    def test_a_device_with_no_parent_block_is_left_alone(self):
        """The EXISTS clause: with no main row for the slot there is no parent rate
        to copy, and a device must not be zeroed for want of one."""
        st = _mk_store()
        _block(st, "sub_meter_ev", imp_kwh=4.0, imp_rate=0.3231)
        self.assertEqual(st.repair_device_rate_drift(), 0)
        self.assertAlmostEqual(_row(st, "sub_meter_ev")["imp_rate"], 0.3231)

    def test_a_parent_with_no_rate_is_not_copied(self):
        st = _mk_store()
        _block(st, "electricity_main", imp_kwh=10.0)          # imp_rate NULL
        _block(st, "sub_meter_ev", imp_kwh=4.0, imp_rate=0.3231)
        self.assertEqual(st.repair_device_rate_drift(), 0)
        self.assertAlmostEqual(_row(st, "sub_meter_ev")["imp_rate"], 0.3231)

    def test_it_is_scoped_to_the_same_slot(self):
        """The parent is matched on `block_start`, so a device block in a different
        half-hour takes its own parent's rate, not this one's."""
        other = "2026-09-01T00:30:00"
        self.st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, config_period_id, meter_id, "
            "imp_kwh, imp_rate) VALUES (?,?,?,?,?,?)",
            (other, other, 1, "sub_meter_ev", 4.0, 0.3231))
        self.st._conn.commit()
        self.assertEqual(self.st.repair_device_rate_drift(), 0,
                         "a device with no parent in ITS slot must be untouched")


class TheSettlementFailureConverges(unittest.TestCase):
    """The scenario end to end, since it is the reason this exists."""

    def test_a_half_applied_settlement_is_repaired_on_the_next_pass(self):
        st = _mk_store()
        # before settlement: main and device agree at peak
        _block(st, "electricity_main", imp_kwh=10.0, imp_rate=0.3231,
               imp_cost=3.231, imp_cost_exc=3.077)
        _block(st, "sub_meter_ev", imp_kwh=4.0, imp_rate=0.3231,
               imp_cost=round(4.0 * 0.3231, 6), imp_cost_exc=round(4.0 * 0.3077, 6))
        # settlement's FIRST transaction lands: main flips to the settled band and
        # is stamped 'settled', so the drain will not revisit it.
        with st._conn:
            st._conn.execute(
                "UPDATE blocks SET imp_rate=?, imp_cost=?, imp_cost_exc=?, rate_source='settled' "
                "WHERE block_start=? AND meter_id='electricity_main'",
                (0.0549, 0.549, 0.523, S))
        # its SECOND, best-effort transaction does not. Nothing else would notice.
        self.assertAlmostEqual(_row(st, "sub_meter_ev")["imp_rate"], 0.3231)
        self.assertEqual(st.repair_device_rate_drift(), 1)
        self.assertAlmostEqual(_row(st, "sub_meter_ev")["imp_rate"], 0.0549)


class Surfaces(unittest.TestCase):

    def test_it_is_a_write(self):
        from store_types import ReadStore, WriteStore
        self.assertTrue(hasattr(WriteStore, "repair_device_rate_drift"))
        self.assertFalse(hasattr(ReadStore, "repair_device_rate_drift"))


class TheJobIsRecurringAndUngated(unittest.TestCase):

    def test_no_done_marker_and_no_retirement_note(self):
        """What distinguishes it from the four heals A11 removed. If either of
        these appears, it has become a one-off and the invariant is unenforced
        again after the first pass."""
        import ast
        tree = ast.parse(open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "run_device_rate_drift_repair")
        doc = ast.get_docstring(fn) or ""
        self.assertNotIn("retires in v5", doc)
        strings = {n.value for n in ast.walk(fn)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        self.assertFalse([s for s in strings if "_done" in s or "done_v" in s],
                         "a done-marker turns this back into a one-off")

    def test_it_is_in_the_manifest(self):
        sys.path.insert(0, _ROOT)
        import job_coordinator
        self.assertIn("device_rate_drift", job_coordinator.registry())

    def test_it_runs_after_the_settled_apply(self):
        """Ordering matters: the apply is what can break the invariant, so the
        repair must follow it in the same hourly slot."""
        import ast
        tree = ast.parse(open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "_tick_dispatch_capture")
        order = [(n.lineno, n.func.id) for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id in ("apply_measured_settled",
                                   "run_device_rate_drift_repair")]
        order.sort()
        self.assertEqual([nm for _, nm in order],
                         ["apply_measured_settled", "run_device_rate_drift_repair"])


if __name__ == "__main__":
    unittest.main()
