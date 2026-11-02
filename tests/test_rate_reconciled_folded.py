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
went with `apply_reconcile_verdict`. Engine step 4e (27 Oct 2026): the settlement re-run keeps the
stored rate of every half-hour settle() does not price, whatever its provenance, so the word decides
nothing and its re-run tests went too. The importer's fold still writes it, as v4's provenance.
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


if __name__ == "__main__":
    unittest.main()
