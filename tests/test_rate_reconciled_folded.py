"""
Step 5 — `rate_reconciled` folded into `rate_source`, the provenance model (8 Oct 2026).

`rate_reconciled` is on the drop list: "a second provenance flag beside rate_source". It meant
"an authority set this rate, so a later re-split keeps it". Its writers were the overlay reconcile
verdict (which set it and left rate_source alone, so a reconciled block could still say
'schedule') and measured settlement (which also stamps rate_source='settled'). Its one deciding
reader is the settlement re-run, which keeps a held rate instead of re-resolving it.

Now the verdict writes rate_source='reconciled' (a measured or corrected row keeps its word) and
the re-run reads rate_source. Rows a v4 reconcile flagged while leaving 'schedule' (11 on a real
database; 0–2 on others) are folded into 'reconciled' by the importer (import spec §5.2).

The column was dropped on 10 Oct 2026 (step 5), and with it the LEGACY fallback and its guard;
the tests that checked the flag was left 0 now check rate_source alone.

The re-run cases use CAD billing (`billing_source='cad'`), where only a correction, a
reconciliation or a bill holds the rate — on the DCC path every predicted band is held anyway
(4.5.15), so the flag never decided anything there.

Discriminating (fail on the parent of the fold): a CAD re-run holds a block whose rate_source
alone says reconciled.

Engine step 4c (26 Oct 2026): the reconcile settles its candidates with settle(), which does not
stamp who set a prediction — so 'reconciled' is no longer written, and the verdict's two tests
went with `apply_reconcile_verdict`. The word is still read (an imported v4 row's, and the CAD
re-run's hold below), and still written by the importer's fold.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import test_rebuild_meter_type as H              # noqa: E402  (stubs, real engine, flat tariff)
engine = H.engine

OFF = 0.0549297


class TheVerdict(H._Harness):

    def _row(self, meter="electricity_main"):
        return engine._store._conn.execute(
            "SELECT imp_rate, rate_source FROM blocks WHERE block_start=? "
            "AND meter_id=?", (H.START, meter)).fetchone()


    def test_measured_settlement_is_recorded_in_rate_source(self):
        self._finalise()
        engine._store.apply_measured_to_main(
            H.START, rate=OFF, cost=round(H.MAIN * OFF, 6), cost_exc=None)
        self.assertEqual(self._row()["rate_source"], "settled")


class TheReRun(H._Harness):
    """Whether the settlement re-run HOLDS a block's rate (its "rate preserved" decision, logged)
    rather than re-deriving it. In CAD billing only a correction, a reconciliation or a bill holds
    it. (The rate itself can't show it here: a CAD re-run keeps any sane stored rate either way —
    it only repairs a missing one — so the decision is what is asserted.)"""

    def _holds(self, rate_source):
        self._finalise()
        with engine._store._conn:
            engine._store._conn.execute(
                "UPDATE blocks SET imp_rate=?, imp_cost=ROUND(imp_kwh*?, 6), rate_source=? "
                "WHERE block_start=? AND meter_id='electricity_main'",
                (OFF, OFF, rate_source, H.START))
        blk = engine._store.get_block_dict_by_start(H.START)
        with self.assertLogs("engine", level="INFO") as cm:
            engine.logger.info("probe")          # assertLogs needs at least one record
            engine._rerun_pass2_for_settled_block(blk, billing_source="cad",
                                                  standing_resolver=None)
        return any("rate preserved" in m for m in cm.output)

    def test_rate_source_alone_holds_a_reconciled_rate(self):
        self.assertTrue(self._holds("reconciled"))

    def test_an_unheld_schedule_row_is_not_held(self):
        """GUARD: the case is real — a plain schedule row is re-derived, not held, in CAD."""
        self.assertFalse(self._holds("schedule"))


if __name__ == "__main__":
    unittest.main()
