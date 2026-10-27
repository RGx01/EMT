"""
Engine step 4e, first part (27 Oct 2026): the removals settle() made possible.

- the device rate drift repair: every settle() write takes the main, its segments and its devices
  in one transaction (4b, 4c), so nothing is left for it to catch;
- step 2a's band parity report and its three routes: the settle parity report (4a) compares every
  part;
- finalise's dispatch overlay and IOG split, and the PASS 2 re-run's overlay re-resolve: settle()
  follows every block write (4d). What is left is the fallback for a half-hour settle() cannot
  price — kWh × the stored (sensor or schedule) rate, its segments at that rate.

Discriminating: every test here fails on the parent, but those labelled GUARD.
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


if __name__ == "__main__":
    unittest.main()
