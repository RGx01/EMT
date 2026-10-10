"""
`reconcile_dispatch_overlay`'s writes — A acceptance 5, writes half.

Five statements, four transactions, three store methods. Two findings.

1. THE EV RESTAMP WAS WRITTEN THREE TIMES, and two of the three carried
   `AND imp_kwh_ev IS NOT NULL` while the middle one did not. One
   `_EV_RESTAMP_SQL` constant now serves all three, guarded.

   I first argued the guard was redundant — the unguarded site is reached only
   where the caller has already established a split exists. That was the wrong
   way round. The guard is THE INVARIANT: these columns are DERIVED from the
   split, and stamping them where none exists leaves the row asserting two
   contradictory things. `web/server.py`'s correction tool states the same rule a
   third way (`CASE WHEN imp_kwh_ev IS NOT NULL ... ELSE <unchanged>`), with the
   comment *"so imp_cost_ev + imp_cost_remainder == imp_cost"*. Three
   independent implementations, no assertion anywhere — until here.

2. THE VERDICT IS ONE TRANSACTION OF THREE STATEMENTS. Main row, sub-meters, EV
   columns. A partial verdict leaves sub-meters priced at the old rate while the
   main row moves, and sub-meter cost derives from the main rate — so the block
   disagrees with itself. That atomicity is why `apply_reconcile_verdict` is one
   method rather than three.
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
    st._conn.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter) "
                     "VALUES (1,'sub_meter_ev',1)")
    st._conn.commit()
    return st


def _block(st, meter_id="electricity_main", **cols):
    base = {"block_start": S, "block_end": S, "config_period_id": 1,
            "meter_id": meter_id, "imp_kwh": 5.0, "imp_rate": 0.25,
            "imp_cost": 1.25, "rate_corrected": 0}
    base.update(cols)
    st._conn.execute("INSERT INTO blocks (%s) VALUES (%s)"
                     % (",".join(base), ",".join("?" * len(base))),
                     tuple(base.values()))
    st._conn.commit()


def _row(st, meter_id="electricity_main"):
    return st._conn.execute(
        "SELECT imp_rate, imp_cost, imp_rate_ev, imp_cost_ev, imp_ev_band, "
        "imp_home_band, rate_source, needs_review "
        "FROM blocks WHERE block_start=? AND meter_id=?", (S, meter_id)).fetchone()


class TheEvColumnInvariant(unittest.TestCase):

    def setUp(self):
        self.st = _mk_store()

    def test_an_unsplit_block_is_left_entirely_alone(self):
        """The invariant. Without the guard this row would come back claiming
        imp_cost_ev = 0 and the whole block as remainder, while imp_kwh_ev still
        says the split is unknown."""
        _block(self.st)
        n = self.st.restamp_ev_columns(S, 0.32, "peak", "day")
        self.assertEqual(n, 0)
        r = _row(self.st)
        for col in ("imp_rate_ev", "imp_cost_ev", "imp_ev_band",
                    "imp_home_band"):
            self.assertIsNone(r[col], "%s was stamped on an unsplit block" % col)

    def test_a_split_block_is_restamped(self):
        _block(self.st, imp_kwh_ev=2.0)
        self.assertEqual(self.st.restamp_ev_columns(S, 0.32, "peak", "day"), 1)
        r = _row(self.st)
        self.assertAlmostEqual(r["imp_rate_ev"], 0.32)
        self.assertAlmostEqual(r["imp_cost_ev"], round(2.0 * 0.32, 6))
        self.assertEqual(r["imp_ev_band"], "peak")

    # test_the_cost_columns_add_up (imp_cost_ev + imp_cost_remainder == imp_kwh x rate) went
    # with imp_cost_remainder (dropped, step 5, 10 Oct 2026); the restamp no longer writes it.

    def test_it_is_meter_scoped(self):
        _block(self.st, imp_kwh_ev=2.0)
        self.assertEqual(
            self.st.restamp_ev_columns(S, 0.32, "peak", "day",
                                       meter_id="sub_meter_ev"), 0)


class BandRelabel(unittest.TestCase):

    def setUp(self):
        self.st = _mk_store()

    def _seg(self, band, attribution):
        import pricing_segments as ps
        self.st.set_block_segments(
            S, "electricity_main", [ps.Segment(1.0, 0.25, None, band, attribution)])

    def _bands(self):
        return [(r["attribution"], r["band"]) for r in self.st._conn.execute(
            "SELECT attribution, band FROM block_segments WHERE block_start=?", (S,))]

    def test_a_stale_label_is_corrected(self):
        self._seg("off_peak", "ev")
        self.assertEqual(self.st.relabel_segment_bands(S, "peak", "day"), 1)
        self.assertEqual(self._bands(), [("ev", "peak")])

    def test_it_is_self_limiting(self):
        """Rewrites once, then matches — which is what lets it run every pass."""
        self._seg("off_peak", "ev")
        self.st.relabel_segment_bands(S, "peak", "day")
        self.assertEqual(self.st.relabel_segment_bands(S, "peak", "day"), 0)

    def test_it_touches_labels_only(self):
        """kwh, rate, exc and attribution are untouched, so the reconciliation
        invariant (segments sum to the block) holds across a relabel."""
        self._seg("off_peak", "ev")
        before = self.st._conn.execute(
            "SELECT kwh, inc_rate, attribution FROM block_segments WHERE block_start=?",
            (S,)).fetchall()
        self.st.relabel_segment_bands(S, "peak", "day")
        after = self.st._conn.execute(
            "SELECT kwh, inc_rate, attribution FROM block_segments WHERE block_start=?",
            (S,)).fetchall()
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])


class ReconcileVerdict(unittest.TestCase):

    def setUp(self):
        self.st = _mk_store()

    def test_main_sub_and_ev_all_move_together(self):
        _block(self.st, imp_kwh=5.0, imp_kwh_ev=2.0, needs_review=1)
        _block(self.st, meter_id="sub_meter_ev", imp_kwh=2.0, imp_kwh_grid=1.5)
        out = self.st.apply_reconcile_verdict(S, 0.32, 0.305, "schedule",
                                              "peak", "day")
        self.assertEqual(out, {"main": 1, "sub_meters": 1, "ev_columns": 1})

        m = _row(self.st)
        self.assertAlmostEqual(m["imp_rate"], 0.32)
        self.assertAlmostEqual(m["imp_cost"], round(5.0 * 0.32, 6))
        # Step 5: the verdict is recorded in rate_source (the second flag was dropped, 10 Oct 2026).
        self.assertEqual(m["rate_source"], "reconciled")

        sub = _row(self.st, "sub_meter_ev")
        self.assertAlmostEqual(sub["imp_rate"], 0.32)
        self.assertAlmostEqual(sub["imp_cost"], round(1.5 * 0.32, 6),
                               msg="sub-meters bill on the GRID portion")

    def test_a_verdict_leaves_a_stored_review_flag(self):
        """BL-74: the flag is history; a verdict no longer clears it."""
        _block(self.st, needs_review=1)
        self.st.apply_reconcile_verdict(S, 0.32, 0.305, "schedule", "peak", "day")
        self.assertEqual(_row(self.st)["needs_review"], 1)

    def test_a_corrected_sub_meter_is_left_alone(self):
        _block(self.st)
        _block(self.st, meter_id="sub_meter_ev", imp_kwh=2.0, imp_rate=0.11,
               rate_corrected=1)
        out = self.st.apply_reconcile_verdict(S, 0.32, 0.305, "schedule",
                                              "peak", "day")
        self.assertEqual(out["sub_meters"], 0)
        self.assertAlmostEqual(_row(self.st, "sub_meter_ev")["imp_rate"], 0.11)

    def test_the_ev_guard_holds_inside_the_verdict_too(self):
        """The third statement is the same guarded SQL — an unsplit block takes
        the rate change without acquiring phantom EV columns."""
        _block(self.st)
        out = self.st.apply_reconcile_verdict(S, 0.32, 0.305, "schedule",
                                              "peak", "day")
        self.assertEqual(out["main"], 1)
        self.assertEqual(out["ev_columns"], 0)
        self.assertIsNone(_row(self.st)["imp_cost_ev"])

    def test_it_is_one_transaction(self):
        """Atomicity, shown rather than asserted: make the third statement fail
        and check the first two did not land."""
        _block(self.st, imp_kwh=5.0, imp_kwh_ev=2.0)
        original = BlockStore._EV_RESTAMP_SQL
        try:
            BlockStore._EV_RESTAMP_SQL = "UPDATE blocks SET no_such_column = 1"
            with self.assertRaises(Exception):
                self.st.apply_reconcile_verdict(S, 0.32, 0.305, "schedule",
                                                "peak", "day")
        finally:
            BlockStore._EV_RESTAMP_SQL = original
        self.assertAlmostEqual(_row(self.st)["imp_rate"], 0.25,
                               msg="the main row committed despite a failed verdict")
        self.assertIsNone(_row(self.st)["rate_source"],
                          "the verdict's provenance committed despite a failed verdict")


class Surfaces(unittest.TestCase):

    def test_all_three_are_writes(self):
        from store_types import ReadStore, WriteStore
        for name in ("restamp_ev_columns", "relabel_segment_bands",
                     "apply_reconcile_verdict"):
            self.assertTrue(hasattr(WriteStore, name), name)
            self.assertFalse(hasattr(ReadStore, name),
                             "%s on the read surface has no thread guard" % name)


class ReconcileHasNoRawConnectionLeft(unittest.TestCase):

    def test_the_function_is_clean(self):
        import ast
        tree = ast.parse(open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "reconcile_dispatch_overlay")
        raw = [n.lineno for n in ast.walk(fn)
               if isinstance(n, ast.Attribute) and n.attr == "_conn"]
        self.assertEqual(raw, [], "raw connection use remains at %s" % raw)


if __name__ == "__main__":
    unittest.main()
