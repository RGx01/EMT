"""
`reconcile_dispatch_overlay`'s writes — A acceptance 5, writes half.

Five statements, four transactions, three store methods. Two findings.

1. THE EV RESTAMP WAS WRITTEN THREE TIMES, two of the three guarded by
   `AND imp_kwh_ev IS NOT NULL`. Folded into one `_EV_RESTAMP_SQL`, then deleted
   with the EV columns themselves (step 5c, 10 Oct 2026): the split is now only
   the block's segments, which the verdict re-writes.

2. THE VERDICT IS ONE TRANSACTION. Main row, sub-meters, segments. A partial
   verdict leaves sub-meters priced at the old rate while the main row moves,
   and sub-meter cost derives from the main rate — so the block disagrees with
   itself. That atomicity is why `apply_reconcile_verdict` is one method rather
   than three.
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


def _segs(st, meter_id="electricity_main"):
    return [(r["kwh"], r["inc_rate"], r["band"], r["attribution"])
            for r in st.get_block_segments(S, meter_id)]


def _row(st, meter_id="electricity_main"):
    return st._conn.execute(
        "SELECT imp_rate, imp_cost, rate_source, needs_review "
        "FROM blocks WHERE block_start=? AND meter_id=?", (S, meter_id)).fetchone()


# TheEvColumnInvariant (restamp_ev_columns and its `imp_kwh_ev IS NOT NULL` guard) went
# with the EV columns (dropped, step 5c, 10 Oct 2026): the split is the block's segments,
# so there is nothing derived left to restamp.


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

    def _split(self, rate=0.32, exc=0.305):
        import pricing_segments as ps
        return [ps.Segment(2.0, rate, exc, "peak", "ev"),
                ps.Segment(3.0, rate, exc, "day", "house")]

    def test_main_sub_and_ev_all_move_together(self):
        _block(self.st, imp_kwh=5.0, needs_review=1)
        _block(self.st, meter_id="sub_meter_ev", imp_kwh=2.0, imp_kwh_grid=1.5)
        out = self.st.apply_reconcile_verdict(S, 0.32, 0.305, segments=self._split())
        self.assertEqual(out, {"main": 1, "sub_meters": 1})

        m = _row(self.st)
        self.assertAlmostEqual(m["imp_rate"], 0.32)
        self.assertAlmostEqual(m["imp_cost"], round(5.0 * 0.32, 6))
        # Step 5: the verdict is recorded in rate_source (the second flag was dropped, 10 Oct 2026).
        self.assertEqual(m["rate_source"], "reconciled")
        # Step 5c: the EV split moves as the block's segments, in the same verdict.
        self.assertEqual(_segs(self.st), [(2.0, 0.32, "peak", "ev"),
                                          (3.0, 0.32, "day", "house")])

        sub = _row(self.st, "sub_meter_ev")
        self.assertAlmostEqual(sub["imp_rate"], 0.32)
        self.assertAlmostEqual(sub["imp_cost"], round(1.5 * 0.32, 6),
                               msg="sub-meters bill on the GRID portion")

    def test_a_verdict_leaves_a_stored_review_flag(self):
        """BL-74: the flag is history; a verdict no longer clears it."""
        _block(self.st, needs_review=1)
        self.st.apply_reconcile_verdict(S, 0.32, 0.305)
        self.assertEqual(_row(self.st)["needs_review"], 1)

    def test_a_corrected_sub_meter_is_left_alone(self):
        _block(self.st)
        _block(self.st, meter_id="sub_meter_ev", imp_kwh=2.0, imp_rate=0.11,
               rate_corrected=1)
        out = self.st.apply_reconcile_verdict(S, 0.32, 0.305)
        self.assertEqual(out["sub_meters"], 0)
        self.assertAlmostEqual(_row(self.st, "sub_meter_ev")["imp_rate"], 0.11)

    def test_an_unsplit_block_acquires_no_segments(self):
        """No segments passed, none written — an unsplit block takes the rate
        change without acquiring a phantom EV split."""
        _block(self.st)
        out = self.st.apply_reconcile_verdict(S, 0.32, 0.305)
        self.assertEqual(out["main"], 1)
        self.assertEqual(_segs(self.st), [])

    def test_it_is_one_transaction(self):
        """Atomicity, shown rather than asserted: make the segment write fail
        and check the main row did not land."""
        _block(self.st, imp_kwh=5.0)

        def boom(*a, **k):
            raise RuntimeError("segment write failed")
        self.st._replace_segments = boom
        with self.assertRaises(RuntimeError):
            self.st.apply_reconcile_verdict(S, 0.32, 0.305, segments=self._split())
        self.assertAlmostEqual(_row(self.st)["imp_rate"], 0.25,
                               msg="the main row committed despite a failed verdict")
        self.assertIsNone(_row(self.st)["rate_source"],
                          "the verdict's provenance committed despite a failed verdict")


class Surfaces(unittest.TestCase):

    def test_both_are_writes(self):
        from store_types import ReadStore, WriteStore
        for name in ("relabel_segment_bands", "apply_reconcile_verdict"):
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
