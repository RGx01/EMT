"""
Engine step 4e, first part (27 Oct 2026): the removals settle() made possible.

- the device rate drift repair: every settle() write takes the main, its segments and its devices
  in one transaction (4b, 4c), so nothing is left for it to catch;
- step 2a's band parity report and its three routes: the settle parity report (4a) compares every
  part;
- finalise's dispatch overlay and IOG split, and the PASS 2 re-run's overlay re-resolve: settle()
  follows every block write (4d). What is left is the fallback for a half-hour settle() cannot
  price — kWh × the stored (sensor or schedule) rate, its segments at that rate.

4e-2 (the same day): the history reprice sweep, and what only it reached — `_reprice_history_block(s)`,
`_apply_iog_split`, `iog_cap.compute_iog_split`, the dispatch-only house refit — go. The settle sweep
covers the history, and what settle() declines keeps its price with segments and ex-VAT from it.

Discriminating: every test here fails on its own parent, but those labelled GUARD.
"""
import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import test_rebuild_meter_type as H              # noqa: E402  (stubs, real engine, flat tariff)
engine = H.engine
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(name):
    with open(os.path.join(_ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def _calls(fn_name, path="engine.py"):
    tree = ast.parse(_src(path))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn_name)
    return {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


class TheRemovals(unittest.TestCase):

    def test_the_drift_repair_is_gone(self):
        from block_store import BlockStore
        self.assertFalse(hasattr(engine, "run_device_rate_drift_repair"))
        self.assertFalse(hasattr(BlockStore, "repair_device_rate_drift"))
        self.assertNotIn("run_device_rate_drift_repair", _src("engine.py"))

    def test_the_band_parity_report_is_gone(self):
        for name in ("band_parity_row", "run_band_parity", "band_parity_status"):
            self.assertFalse(hasattr(engine, name), name)
        self.assertNotIn("/api/settlement/band-parity/", _src("web/server.py"))

    def test_finalise_no_longer_overlays_or_splits(self):
        c = _calls("_reprice_main_import_block")
        self.assertNotIn("_dispatch_overlay_rate", c)
        self.assertNotIn("_apply_iog_split", c)

    def test_the_rerun_no_longer_re_resolves(self):
        c = _calls("_rerun_pass2_for_settled_block")
        self.assertNotIn("_reprice_main_import_block", c)
        self.assertNotIn("_apply_iog_split", c)

    def test_GUARD_settle_still_reads_the_overlay(self):
        """The overlay's decision is the prediction's first stage (predicted_band)."""
        self.assertIn("_dispatch_overlay_rate", _calls("predicted_band"))


class TheReRunsFallback(H._Harness):
    """Where settle() does not price a re-run (here CAD billing), the stored rate stands."""

    def _rerun(self, rate_source, rate, cost):
        self._finalise()
        with engine._store._conn:
            engine._store._conn.execute(
                "UPDATE blocks SET imp_rate=?, imp_cost=?, rate_source=? "
                "WHERE block_start=? AND meter_id='electricity_main'",
                (rate, cost, rate_source, H.START))
        blk = engine._store.get_block_dict_by_start(H.START)
        out = engine._rerun_pass2_for_settled_block(blk, billing_source="cad",
                                                    standing_resolver=None)
        return out["meters"]["electricity_main"]["channels"]["import"]

    def test_GUARD_a_stored_rate_is_re_costed_to_the_kwh(self):
        imp = self._rerun("schedule", 0.0549, 0.1)
        self.assertAlmostEqual(imp["rate"], 0.0549)
        self.assertAlmostEqual(imp["cost"], round(imp["kwh"] * 0.0549, 6))

    def test_GUARD_a_settled_block_keeps_its_bill(self):
        imp = self._rerun("settled", 0.0549, 0.123456)
        self.assertAlmostEqual(imp["cost"], 0.123456)


# ── Engine step 4e-2 (27 Oct 2026): the history reprice sweep goes; the settle sweep covers it ──

class _Store:
    @staticmethod
    def make():
        from block_store import BlockStore
        st = BlockStore(":memory:")
        st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, "
                         "block_minutes, timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
        st._conn.commit()
        return st

    @staticmethod
    def main(st, bs, *, kwh=2.0, cost=0.6, rate=0.3, source="imported_api", exc=None):
        st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "imp_cost, imp_rate, imp_cost_exc, source) VALUES (?,?,'electricity_main',1,?,?,?,?,?)",
            (bs, bs, kwh, cost, rate, exc, source))
        st._conn.commit()


class TheFallback(unittest.TestCase):
    """What settle() declines (no rate, no band) keeps its price; the settle sweep gives it the
    segments and ex-VAT the history reprice sweep used to."""

    BS = "2025-03-01T19:00:00"

    def setUp(self):
        from unittest import mock
        self.st = _Store.make()
        self.p = mock.patch.object(engine, "_settle_write", lambda *a, **k: {
            "status": "no_band", "parts": [], "written": False})
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def _segs(self):
        return [(g["kwh"], g["inc_rate"], g["attribution"])
                for g in self.st.get_block_segments(self.BS, "electricity_main")]

    def test_a_declined_half_hour_gets_its_segments_and_ex_vat(self):
        _Store.main(self.st, self.BS)
        r = engine.settle_range(self.st, "2025-03-01T00:00:00", "2025-03-02T00:00:00")
        self.assertEqual((r["written"], r["parts"]), (1, {"fallback": 1}))
        self.assertEqual(self._segs(), [(2.0, 0.3, "house")])
        exc = self.st._conn.execute("SELECT imp_cost_exc FROM blocks").fetchone()[0]
        self.assertAlmostEqual(exc, round(0.6 / (1 + self.st.vat_rate_at(self.BS)), 6))

    def test_GUARD_one_with_segments_is_left(self):
        _Store.main(self.st, self.BS, exc=0.57)
        self.st.set_block_segments(self.BS, "electricity_main", [(2.0, 0.3, None, "day", "house")])
        r = engine.settle_range(self.st, "2025-03-01T00:00:00", "2025-03-02T00:00:00")
        self.assertEqual(r["written"], 0)

    def test_GUARD_a_gap_stays_unsegmented(self):
        _Store.main(self.st, self.BS, cost=None)
        engine.settle_range(self.st, "2025-03-01T00:00:00", "2025-03-02T00:00:00")
        self.assertEqual(self._segs(), [])


class TheSettleSweepInItsPlace(unittest.TestCase):

    def test_the_reprice_sweep_is_gone(self):
        for name in ("_run_historical_reprice_sweep", "run_reprice_history_sweep_to_done",
                     "reprice_history_in_progress", "_maybe_reprice_history",
                     "_reprice_history_block", "_apply_iog_split", "_refit_devices_to_house"):
            self.assertFalse(hasattr(engine, name), name)
        import iog_cap
        self.assertFalse(hasattr(iog_cap, "compute_iog_split"))
        self.assertNotIn('@app.route("/api/reprice-history-status")', _src("web/server.py"))

    def test_the_settle_sweep_is_the_member(self):
        import job_coordinator as C
        self.assertEqual(getattr(engine._run_settle_history_sweep, "_job_name", None),
                         "settle_sweep")
        self.assertNotIn("reprice_sweep", C.registry())
        self.assertIn('@app.route("/api/settle-history-status")', _src("web/server.py"))

    def test_startup_schedules_it(self):
        self.assertIn("_maybe_settle_history", _calls("_engine_startup_impl"))

    def test_without_a_schedule_it_is_not_in_progress(self):
        """A rate-sensor account: the sweep never runs, so the daily copy must not wait on it."""
        from unittest import mock
        with mock.patch.object(engine, "_store", _Store.make()), \
             mock.patch.object(engine, "_settle_history_running", False), \
             mock.patch.object(engine, "_kraken_rate_schedules", {}):
            self.assertFalse(engine.settle_history_in_progress())


if __name__ == "__main__":
    unittest.main()
