"""
The write inventory (.build/write_inventory.py) is workstream B's scope. B is the
job coordinator, and what it coordinates is writers — so a wrong inventory means
either an uncoordinated write (a race) or a coordinated read (a pointless lock).

Checked against cases established by hand, the same discipline as the reachability
and classifier tools. Two of the assertions below are worth more than the rest:

  * `reprice_history_conformance` must come back READ-ONLY. Its docstring says it
    "persists NOTHING", and the tool agreeing with that is an independent check of
    both the tool and the docstring.
  * the three GET routes fixed during the surfaces port must NOT be writers. They
    still name their write method — inside an `_engine_write(...)` lambda — so a
    tool that missed the hop would overstate B's scope with work already done.
"""
import json
import os
import subprocess
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOL = os.path.join(_ROOT, ".build", "write_inventory.py")


def _inv(*modules):
    r = subprocess.run([sys.executable, _TOOL, "--json"] + list(modules),
                       capture_output=True, text=True, cwd=_ROOT)
    return {x["module"]: x for x in json.loads(r.stdout)}


class TestEngineInventory(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.e = _inv("engine.py")["engine.py"]
        cls.write = set(cls.e["write"])

    def test_every_long_running_job_is_a_writer(self):
        """All 17 of them. A §7 q3 closed on this: there is no such thing as a
        long-running READ job in EMT, so B's scope is not reduced by them — it is
        defined by them."""
        for job in ("run_api_import_job", "run_deferred_verify_pricing",
                    "_run_historical_reprice_sweep",
                    # The three legacy backfills (_run_historical_{exc,iog_split,segment}
                    # _backfill) were deleted — BL-33, 4 Oct 2026.
                    "_run_historical_carbon_backfill", "run_measured_history_drain",
                    "run_gap_fill_job", "run_iog_split_carve", "run_attribution_job",
                    # The four v4 one-off heals that used to sit here
                    # (run_smb_rate_repair, run_smb_device_recost,
                    # run_band_rate_snap, run_smb_ev_resplit) were removed for
                    # v5 — see tests/test_v4_heals_removed.py.
                    "run_reprice_history_sweep_to_done"):
            self.assertIn(job, self.write, "%s must be coordinated" % job)

    def test_the_conformance_pass_is_read_only(self):
        """It says so itself — "recompute ... IN MEMORY — persists NOTHING". The
        tool reaching the same verdict independently is worth more than either
        claim alone."""
        self.assertNotIn("reprice_history_conformance", self.write)

    def test_pricing_computation_is_separated_from_persistence(self):
        """`_apply_iog_split` mutates the channel dict; `_persist_block_segments`
        is what touches the database. If the first ever becomes a writer, that
        separation has been lost."""
        self.assertNotIn("_apply_iog_split", self.write)
        self.assertIn("_persist_block_segments", self.write)
        self.assertIn("_reprice_main_import_block", self.write)

    def test_transitive_writers_are_found(self):
        self.assertTrue(self.e["only_via_a_callee"],
                        "a caller-only writer must still be in scope")

    def test_raw_sql_writers_are_gone(self):
        """Seven engine functions used to mutate through `_conn` directly,
        bypassing the method split — a tool reading only method names would miss
        them, which is why this inventory looks for raw SQL at all.

        There are now none. `apply_measured_to_block` was the last (A16); every
        write in `engine.py` goes through a `WriteStore` method, so thread
        affinity is enforceable at the type boundary rather than trusted.

        This asserts EMPTY rather than a shrinking list, because the number it
        replaced was a count of the problem: the remaining `_conn` uses in
        engine.py are all reads, tracked separately by
        tests/test_raw_connection_ratchet.py.
        """
        self.assertEqual(self.e["raw_sql_writers"], [],
                         "a raw SQL write reappeared in engine.py")


class TestServerInventory(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.s = _inv("web/server.py")["web/server.py"]
        cls.write = set(cls.s["write"])

    def test_hops_are_barriers_in_the_transitive_step_too(self):
        """`_config_mutation_on_loop` genuinely writes — on the engine thread —
        so every caller inherited "writes" and stayed in B's scope after already
        being fixed. Three config endpoints sat there for that reason. What this
        inventory measures is writes performed on the REQUEST thread, and a hop is
        exactly where that stops being true."""
        for h in ("api_config_history_create", "api_config_history_update",
                  "api_config_history_delete"):
            self.assertNotIn(h, self.write,
                             "%s writes via _config_mutation_on_loop, on the loop" % h)

    def test_the_ported_small_handlers_are_gone(self):
        """Ported this pass onto `_engine_write`: single-statement or
        handful-of-meters writes, where a synchronous hop costs nothing."""
        for h in ("api_retire_meter", "api_unretire_meter", "api_settings_post",
                  "api_region_reconcile_dismiss", "api_review_blocks_dismiss",
                  "api_historical_retag_imports"):
            self.assertNotIn(h, self.write)

    def test_the_whole_config_write_path_is_on_the_engine(self):
        """`save_config()` is the path that saves the entire configuration, and it
        wrote through the shared web store from a request thread. #361 had already
        found and fixed that race for the config-HISTORY endpoints; the plain save
        was left on the old path.

        `api_power_source_mini` clears with it — it had no writes of its own.
        """
        for h in ("save_config", "api_power_source_mini", "api_save_config"):
            self.assertNotIn(h, self.write, "%s still writes on the request thread" % h)

    def test_the_engine_hop_is_not_counted_as_a_write(self):
        """These three were GET routes writing on a Flask worker; they now go
        through `_engine_write`. They still NAME a write method, inside the
        lambda. Counting that would charge B for work already done."""
        for h in ("api_blocks_deleted_ranges", "api_blocks_sweep_implausible",
                  "api_carbon_pause"):
            self.assertNotIn(h, self.write, "%s hops to the engine now" % h)


class TestPureModules(unittest.TestCase):
    def test_the_transform_modules_write_nothing(self):
        """`backfill`, `csv_import` and `api_import` compute; the caller persists.
        Worth pinning: they are the modules most likely to grow a convenience
        write, and that would put a writer outside the engine's thread."""
        inv = _inv("backfill.py", "csv_import.py", "api_import.py")
        for mod in ("backfill.py", "csv_import.py", "api_import.py"):
            self.assertEqual(inv[mod]["write"], [], "%s has grown a write" % mod)


if __name__ == "__main__":
    unittest.main()
