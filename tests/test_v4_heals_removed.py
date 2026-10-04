"""
The four v4 one-off heals are gone, and v5 carries none of its own.

    run_smb_rate_repair    (4.5.7)   smb_rate_repair_done_v2
    run_smb_device_recost  (4.5.7)   smb_device_recost_done
    run_smb_ev_resplit     (4.5.9)   smb_ev_resplit_done
    run_band_rate_snap     (4.5.13)  band_rate_snap_done_v2

THE EVIDENCE IS THE IMPORT FLOOR, NOT A REACHABILITY TRACE. These were reachable
-- called from `_tick_dispatch_capture` on every tick -- so §4a's gate 1 does not
apply and this is a different category of removal: code scheduled to retire at a
version boundary. Each said so itself: *"Gated (...), self-marking, idempotent;
retires in v5.0.0."*

What makes the removal safe is EMT-v5_import_spec §1.1:

    Every one-off heal in v4 is gated on a marker in `kraken_state` or
    `store_meta`, and an ungated marker runs at startup. So upgrading any older
    database to a given release runs every heal that release carries. "Last
    opened by version X" therefore means "healed to X's standard."

and the disposal it draws:

    The heals run where they were always meant to: in v4, against v4's schema,
    with v4's tests behind them. ... v5 starts from an empty heal set of its own.

FOOTPRINT, recorded because "unreachable is not unreferenced" cuts both ways and
the deletion was much larger than its 12 raw-connection sites suggested:

    engine.py        478 lines   4 wrappers, 4 cores, _smb_migration_overlay,
                                 _write_smb_repair_report, 5 constants
                       4 call sites in _tick_dispatch_capture
    job_coordinator    4 manifest entries
    .build/            3 tool lists
    tests/           678 lines / 37 tests across 5 files

ONE THING THIS MUST NOT TAKE WITH IT. `run_settled_ev_split_heal` is named
"heal" and is not one: ungated, no done-marker, runs every pass, no retirement
note. Settlement keeps producing blocks that need it. It is asserted present
below, because a later name-match deletion is the obvious way to lose it.
"""
import ast
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_REMOVED = ("run_smb_rate_repair", "run_smb_device_recost",
            "run_smb_ev_resplit", "run_band_rate_snap",
            "_smb_rate_repair_core", "_smb_device_recost_core",
            "_smb_ev_resplit_core", "_band_rate_snap_core",
            "_smb_migration_overlay", "_write_smb_repair_report")

# The store-side half. The reachability tool found it: deleting the engine jobs
# moved the codebase from 80 unreachable to 81, and the new entry was this. Its
# own docstring already said "removed v5.0.0"; nothing but the heal ever called
# it. A deletion that leaves its store methods behind is half a deletion.
_REMOVED_STORE = ("smb_repair_write_block",
                  # BL-74, 4 Oct 2026: the three startup self-heals' store halves.
                  "sweep_implausible_sub_blocks", "sweep_register_glitches",
                  "flag_grid_invariant_violations")

_REMOVED_MARKERS = ("smb_rate_repair_done", "smb_device_recost_done",
                    "smb_ev_resplit_done", "band_rate_snap_done",
                    # BL-74, 4 Oct 2026: the startup self-heals (#307, register
                    # dip-and-recover, BL-19). Set in every admissible database.
                    "implausible_sub_block_sweep_done", "register_glitch_sweep_done",
                    "bl19_grid_invariant_sweep_done")


def _engine_tree():
    return ast.parse(open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read())


class TheHealsAreGone(unittest.TestCase):

    def test_no_definitions_remain(self):
        names = {n.name for n in ast.walk(_engine_tree())
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        left = sorted(set(_REMOVED) & names)
        self.assertEqual(left, [], "still defined: %s" % left)

    def test_nothing_calls_them(self):
        called = {getattr(n.func, "id", None) or getattr(n.func, "attr", None)
                  for n in ast.walk(_engine_tree()) if isinstance(n, ast.Call)}
        left = sorted(set(_REMOVED) & called)
        self.assertEqual(left, [], "still called: %s" % left)

    def test_the_store_side_half_went_too(self):
        """Found by `.build/check_reachability.py`, not by reading: the engine
        deletion took the codebase from 80 unreachable definitions to 81, and the
        newcomer was `BlockStore.smb_repair_write_block`."""
        import sys
        sys.path.insert(0, _ROOT)
        from block_store import BlockStore
        from store_types import ReadStore, WriteStore
        for name in _REMOVED_STORE:
            self.assertFalse(hasattr(BlockStore, name), name)
            self.assertFalse(hasattr(WriteStore, name), name)
            self.assertFalse(hasattr(ReadStore, name), name)

    def test_the_done_markers_are_gone_too(self):
        """A marker with nothing reading it is a trap: it looks like state the
        app maintains. The keys stay in existing databases, harmlessly — nothing
        v5 runs will ever consult them."""
        src = open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read()
        tree = ast.parse(src)
        strings = {n.value for n in ast.walk(tree)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        for marker in _REMOVED_MARKERS:
            self.assertFalse(
                any(marker in s for s in strings),
                "%s is still referenced as a string constant" % marker)

    def test_the_job_manifest_no_longer_declares_them(self):
        import sys
        sys.path.insert(0, _ROOT)
        import job_coordinator
        declared = set(job_coordinator.registry())
        for short in ("smb_rate_repair", "smb_device_recost",
                      "smb_ev_resplit", "band_rate_snap"):
            self.assertNotIn(short, declared)


class WhatMustSurvive(unittest.TestCase):

    def test_the_recurring_ev_split_heal_is_still_here(self):
        """Named 'heal', is not one. Ungated, every pass, no retirement note."""
        fns = {n.name: n for n in ast.walk(_engine_tree())
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.assertIn("run_settled_ev_split_heal", fns)
        doc = ast.get_docstring(fns["run_settled_ev_split_heal"]) or ""
        self.assertNotIn("retires in v5", doc)
        self.assertIn("NOT a one-off heal", doc,
                      "the note warning against a name-match deletion is gone")

    def test_the_other_recurring_split_jobs_are_still_here(self):
        """Blocks arrive with a NULL split because Octopus reports completed
        dispatch hours after pricing — ordinary lifecycle, not heal damage. The
        jobs that fix it are permanent and unaffected by this removal."""
        fns = {n.name for n in ast.walk(_engine_tree())
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for name in ("_attribute_missing_ev_split", "run_iog_split_carve",
                     "run_settled_ev_split_heal"):
            self.assertIn(name, fns)

    def test_the_smb_helpers_with_other_callers_survived(self):
        """`_measured_floor` and `_import_is_smb_capped` sit in the same region
        and serve the settlement path. Deleting by neighbourhood would have
        taken them."""
        fns = {n.name for n in ast.walk(_engine_tree())
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for name in ("_measured_floor", "_import_is_smb_capped", "_chart_cap_from"):
            self.assertIn(name, fns)


if __name__ == "__main__":
    unittest.main()
