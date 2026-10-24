"""
The settlement family's reads — A acceptance 5.

Nine sites across six functions, all on the path that applies Octopus's bill to a
block. Split reads from writes, as with reconcile: this half cannot change what is
persisted, so a Pi run that turns something up reverts one patch rather than the
whole family.

Three of the nine are worth more than a mechanical port, and each has its own
class below.

  * (`dispatch_coverage` was the third — "no dispatch" vs "no dispatch records reaching back
    this far" — and was deleted 16 Oct 2026, bill and split paths design §6 (b): on the
    IOG-SMB agreement the bill's split is the split, so the applier no longer asks.)
  * `get_settleable_blocks` has THREE bounds that look redundant and are not:
    floor, cursor, cutoff.
  * `_SETTLEABLE_SQL` was spliced into three queries in engine.py — twice by
    string concatenation, once by f-string. It is now one class constant on
    BlockStore, shared by the three methods that replaced them.
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


def _block(st, start=S, meter_id="electricity_main", **cols):
    base = {"block_start": start, "block_end": start, "config_period_id": 1,
            "meter_id": meter_id, "imp_kwh": 5.0, "rate_corrected": 0}
    base.update(cols)
    st._conn.execute("INSERT INTO blocks (%s) VALUES (%s)"
                     % (",".join(base), ",".join("?" * len(base))),
                     tuple(base.values()))
    st._conn.commit()


def _hist(st, slot, kind="completed", kwh=-3.0):
    st.record_dispatch_history(slot_start=slot, kind=kind, provider="octopus",
                               source=None, energy_kwh=kwh,
                               raw_start=None, raw_end=None)


def _measured(st, slot, *, ev_kwh=None, **kw):
    kw.setdefault("mpan", "123")
    kw.setdefault("cost_incl", 1.25)
    st.upsert_measured_cost(slot, **kw)
    if ev_kwh is not None:
        st.upsert_measured_breakdown(slot, mpan=kw["mpan"], ev_kwh=ev_kwh)


class BlockMeasuredInputs(unittest.TestCase):

    def test_missing_block_is_none(self):
        self.assertIsNone(_mk_store().get_block_measured_inputs(S))

    def test_it_carries_the_settleable_evidence_too(self):
        """`source` and `imp_kwh_api` are in the projection because the applier
        repeats the settleable check as defence in depth — it is the single
        stamper of rate_source='settled', whoever calls it."""
        st = _mk_store()
        _block(st, imp_kwh_api=4.9, source="imported_csv")
        # the stored EV split is the block's EV segments (step 5c, 10 Oct 2026)
        st.set_block_segments(S, "electricity_main", [(2.0, 0.3, 0.29, "peak", "ev"),
                                                      (3.0, 0.3, 0.29, "peak", "house")])
        r = st.get_block_measured_inputs(S)
        self.assertAlmostEqual(r["imp_kwh_ev"], 2.0)
        for col in ("imp_kwh", "imp_kwh_ev", "imp_rate", "source", "imp_kwh_api"):
            self.assertIn(col, r)


class CompletedDispatchKwh(unittest.TestCase):

    def setUp(self):
        self.st = _mk_store()

    def test_none_is_zero_not_none(self):
        """The caller compares against a float threshold; None would raise."""
        self.assertEqual(self.st.completed_dispatch_kwh(S), 0.0)

    def test_it_returns_a_positive_figure(self):
        """Dispatch energy is recorded NEGATIVE (energy drawn). ABS is what makes
        this the EV ceiling rather than its negation."""
        _hist(self.st, S, kwh=-3.0)
        self.assertAlmostEqual(self.st.completed_dispatch_kwh(S), 3.0)

    def test_the_sum_can_only_ever_see_one_row(self):
        """Written expecting two completed rows to add up. They cannot:
        dispatch_history is PRIMARY KEY (slot_start, kind), so a second
        'completed' for the same slot REPLACES the first. The production SQL's
        SUM() is therefore ABS() with extra steps — harmless, and worth knowing
        before anyone reads it as evidence that multiple dispatches per slot
        accumulate."""
        _hist(self.st, S, kwh=-3.0)
        _hist(self.st, S, kwh=-1.5)
        self.assertAlmostEqual(self.st.completed_dispatch_kwh(S), 1.5,
                               msg="the second row replaced the first")
        n = self.st._conn.execute(
            "SELECT COUNT(*) FROM dispatch_history WHERE slot_start=? AND kind='completed'",
            (S,)).fetchone()[0]
        self.assertEqual(n, 1)

    def test_only_completed_counts(self):
        _hist(self.st, S, kind="planned", kwh=-9.0)
        self.assertEqual(self.st.completed_dispatch_kwh(S), 0.0)

    def test_it_does_not_bleed_across_slots(self):
        _hist(self.st, S, kwh=-3.0)
        _hist(self.st, "2026-09-01T00:30:00", kwh=-9.0)
        self.assertAlmostEqual(self.st.completed_dispatch_kwh(S), 3.0)


class SettleableBlocks(unittest.TestCase):
    """floor / cursor / cutoff — three bounds, none redundant."""

    def setUp(self):
        self.st = _mk_store()
        for d in ("01", "02", "03", "04"):
            _block(self.st, "2026-09-%sT00:00:00" % d, imp_kwh=5.0, imp_kwh_api=4.9)

    def test_a_settleable_block_is_selected(self):
        out = self.st.get_settleable_blocks("2026-09-01T00:00:00", "",
                                            "2026-09-09T00:00:00", 10)
        self.assertEqual(len(out), 4)

    def test_a_live_cad_block_is_never_settleable(self):
        """No imp_kwh_api and not imported: there is no authoritative kWh, so it
        must never be stamped 'settled'."""
        st = _mk_store()
        _block(st, imp_kwh=5.0)
        self.assertEqual(st.get_settleable_blocks("2026-01-01T00:00:00", "",
                                                  "2026-12-01T00:00:00", 10), [])

    def test_imported_history_is_settleable_without_api_kwh(self):
        st = _mk_store()
        _block(st, imp_kwh=5.0, source="imported_csv")
        self.assertEqual(len(st.get_settleable_blocks("2026-01-01T00:00:00", "",
                                                      "2026-12-01T00:00:00", 10)), 1)

    def test_the_cursor_pages_within_a_run(self):
        """`after` advances past slots Octopus had no answer for. Collapsing it
        into `floor` makes the drain re-read the same page forever."""
        first = self.st.get_settleable_blocks("2026-09-01T00:00:00", "",
                                              "2026-09-09T00:00:00", 2)
        self.assertEqual(len(first), 2)
        nxt = self.st.get_settleable_blocks("2026-09-01T00:00:00", first[-1],
                                            "2026-09-09T00:00:00", 2)
        self.assertEqual(len(nxt), 2)
        self.assertTrue(set(first).isdisjoint(nxt), "the cursor did not advance")

    def test_the_cutoff_excludes_the_not_yet_billed_window(self):
        out = self.st.get_settleable_blocks("2026-09-01T00:00:00", "",
                                            "2026-09-03T00:00:00", 10)
        self.assertEqual(out, ["2026-09-01T00:00:00", "2026-09-02T00:00:00"])

    def test_a_block_with_a_cached_bill_is_done(self):
        _measured(self.st, "2026-09-01T00:00:00")
        out = self.st.get_settleable_blocks("2026-09-01T00:00:00", "",
                                            "2026-09-09T00:00:00", 10)
        self.assertNotIn("2026-09-01T00:00:00", out)

    def test_already_settled_is_excluded(self):
        st = _mk_store()
        _block(st, imp_kwh=5.0, imp_kwh_api=4.9, rate_source="settled")
        self.assertEqual(st.get_settleable_blocks("2026-01-01T00:00:00", "",
                                                  "2026-12-01T00:00:00", 10), [])


class SharedPredicate(unittest.TestCase):

    def test_the_methods_share_one_constant(self):
        """Spliced into three queries in engine.py before this — twice by
        concatenation, once by f-string. The classifier learned to resolve class
        SQL constants in A9, which is what makes sharing possible at all."""
        sql = BlockStore._SETTLEABLE_SQL
        self.assertIn("imp_kwh_api", sql)
        self.assertIn("imported", sql)
        import inspect
        # (count_settleable_blocks was the third, until engine step 3c's one selector)
        for name in ("get_settleable_blocks", "get_measured_costs_to_apply"):
            src = inspect.getsource(getattr(BlockStore, name))
            self.assertIn("_SETTLEABLE_SQL", src, name)

    def test_engine_no_longer_carries_its_own_copy(self):
        import ast
        tree = ast.parse(open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read())
        assigned = [t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
                    for t in n.targets if isinstance(t, ast.Name)]
        self.assertNotIn("_SETTLEABLE_SQL", assigned,
                         "two copies of a predicate is how they drift")


class OrderingIsPartOfTheContract(unittest.TestCase):

    def test_the_bulk_drain_is_oldest_first(self):
        st = _mk_store()
        for d in ("03", "01", "02"):
            _block(st, "2026-09-%sT00:00:00" % d, imp_kwh=5.0, imp_kwh_api=4.9)
        out = st.get_settleable_blocks("2026-09-01T00:00:00", "",
                                       "2026-09-09T00:00:00", 10)
        self.assertEqual(out, sorted(out))

    def test_the_live_pass_is_newest_first(self):
        """Opposite on purpose: the live pass is bounded per tick and the most
        recent settlement is the one a user is looking at."""
        st = _mk_store()
        for d in ("01", "02", "03"):
            s = "2026-09-%sT00:00:00" % d
            _block(st, s, imp_kwh=5.0, imp_kwh_api=4.9)
            st.upsert_dispatch_slot(slot_start=s, off_peak=1, provider="octopus",
                                    source=None, state=None, energy_planned=None,
                                    energy_completed=None, raw_start=None, raw_end=None)
        out = [r["block_start"] for r in
               st.get_dispatched_blocks_awaiting_measure("2026-09-01T00:00:00")]
        self.assertEqual(out, sorted(out, reverse=True))

    def test_the_live_pass_excludes_imported_history(self):
        """Imported blocks are drained separately and deliberately, oldest-first."""
        st = _mk_store()
        s = "2026-09-01T00:00:00"
        _block(st, s, imp_kwh=5.0, imp_kwh_api=4.9, source="imported_csv")
        st.upsert_dispatch_slot(slot_start=s, off_peak=1, provider="octopus",
                                source=None, state=None, energy_planned=None,
                                energy_completed=None, raw_start=None, raw_end=None)
        self.assertEqual(st.get_dispatched_blocks_awaiting_measure("2026-01-01T00:00:00"), [])


class MeasuredCostsToApply(unittest.TestCase):

    def setUp(self):
        self.st = _mk_store()
        _block(self.st, imp_kwh=5.0, imp_kwh_api=4.9, imp_cost=1.60)

    def test_a_cached_bill_is_offered(self):
        _measured(self.st, S, mpan="123", cost_incl=1.25)
        self.assertEqual(len(self.st.get_measured_costs_to_apply("123", "2026-01-01T00:00:00")), 1)

    def test_another_meters_bill_is_not(self):
        """MPAN-scoped so a move to a new meter cannot apply the old one's bill."""
        _measured(self.st, S, mpan="999", cost_incl=1.25)
        self.assertEqual(self.st.get_measured_costs_to_apply("123", "2026-01-01T00:00:00"), [])

    def test_a_zero_bill_is_not_applied(self):
        _measured(self.st, S, mpan="123", cost_incl=0.0)
        self.assertEqual(self.st.get_measured_costs_to_apply("123", "2026-01-01T00:00:00"), [])

    def test_a_user_correction_is_never_overwritten(self):
        st = _mk_store()
        _block(st, imp_kwh=5.0, imp_kwh_api=4.9, rate_corrected=1)
        _measured(st, S, mpan="123", cost_incl=1.25)
        self.assertEqual(st.get_measured_costs_to_apply("123", "2026-01-01T00:00:00"), [])


class Surfaces(unittest.TestCase):

    def test_all_five_are_reads(self):
        from store_types import ReadStore
        for name in ("get_block_measured_inputs", "completed_dispatch_kwh",
                     "get_settleable_blocks",
                     "get_measured_costs_to_apply",
                     "get_dispatched_blocks_awaiting_measure"):
            self.assertTrue(hasattr(ReadStore, name), name)


if __name__ == "__main__":
    unittest.main()
