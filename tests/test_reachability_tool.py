"""The reachability trace §4a depends on.

EMT is a subtractive project against a codebase that works, so `architecture §4a`
requires an over-approximating reachability trace before anything is deleted.
These tests exist so that requirement is enforceable rather than aspirational:
the trace is committed, its dead set cannot silently grow, and — most
importantly — it is checked against cases established by hand, because a trace
nobody has validated is worse than no trace at all.
"""
import json
import os
import subprocess
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOL = os.path.join(_ROOT, ".build", "check_reachability.py")
_BASE = os.path.join(_ROOT, ".build", "reachability_baseline.json")


def _run(*args):
    return subprocess.run([sys.executable, _TOOL, *args],
                          capture_output=True, text=True, cwd=_ROOT)


class TestToolRuns(unittest.TestCase):
    def test_reports_json(self):
        r = _run("--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertGreater(d["files_scanned"], 20)
        self.assertGreater(d["definitions"], 1000)
        self.assertEqual(d["parse_errors"], [], "every production file must parse")


class TestWalkScope(unittest.TestCase):
    """Regression: `production_files` used `.lstrip("./")` to trim a prefix. lstrip
    takes a character SET, so a local `.venv/lib/x.py` came back as `venv/lib/x.py`
    -- a path that does not exist -- and the tool crashed on it. A virtualenv is
    also not production code, dotted or not, and must not be scanned."""

    def test_virtualenvs_and_dot_dirs_are_not_scanned(self):
        import importlib.util
        import tempfile
        spec = importlib.util.spec_from_file_location("_reach", _TOOL)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as root:
            for rel in ("main.py", "pkg/mod.py", ".venv/lib/a.py",
                        "venv/lib/b.py", ".hidden/c.py"):
                path = os.path.join(root, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                open(path, "w").close()
            mod.ROOT = root
            self.assertEqual(mod.production_files(), ["main.py", "pkg/mod.py"])


class TestAgreesWithHandEstablishedCases(unittest.TestCase):
    """The trace was checked against cases worked out by reading the code. If a
    future change to the tool breaks one of these, the tool is wrong — not the
    case."""

    @classmethod
    def setUpClass(cls):
        cls.dead = {n["name"]
                    for v in json.loads(_run("--json").stdout)["by_file"].values()
                    for n in v}

    def _dead(self, *names):
        for n in names:
            self.assertIn(n, self.dead, "%s should trace as unreachable" % n)

    def _live(self, *names):
        for n in names:
            self.assertNotIn(n, self.dead, "%s is LIVE — tracing it dead would "
                                           "licence deleting working code" % n)

    def test_reprice_is_wholly_unreachable(self):
        self._dead("_band_label", "_reconcile_house", "reprice_block", "resolve_ev_energy")

    def test_pricing_segments_three_dead_eight_live(self):
        self._dead("attribute_devices", "import_segments", "total_cost_exc")
        # These six have no EXTERNAL caller but are reached transitively; an
        # earlier v4 roadmap entry called all nine shelved, and acting on that
        # would have deleted working code.
        self._live("segments_from_legacy", "price_devices_hybrid", "total_kwh",
                   "total_cost", "blended_rate", "attribution_kwh",
                   "attribution_cost", "attribution_rate")

    def test_reads_table_api_is_unreachable(self):
        self._dead("insert_read", "link_reads_to_block", "get_reads_for_block",
                   "get_reads_for_range", "purge_reads_older_than")

    def test_superseded_card_formatter_is_unreachable(self):
        self._dead("_format_billing")

    def test_conditionally_live_code_is_not_called_dead(self):
        """`_hybrid_ev_by_block` is the EV rule for accounts WITH a physical
        meter; `_dispatch_ev_split_by_bucket` serves accounts without one. A
        4.5.13 bug was misdiagnosed against the first on the belief it was
        unreferenced. Both are live."""
        self._live("_hybrid_ev_by_block", "_dispatch_ev_split_by_bucket")

    def test_dunders_are_never_reported_dead(self):
        """Found by the first real use of this tool. `IngressMiddleware.__call__`
        is the WSGI entry point — EVERY request goes through it — but nothing
        writes the name, so a name-trace saw no caller and reported it dead.
        `__aenter__`/`__aexit__` (async with) and `__len__` (len()) the same.
        This is the tool lying about DEAD, the direction that deletes working
        code, so the whole dunder namespace is now excluded."""
        self._live("__call__", "__aenter__", "__aexit__", "__len__")

    def test_unreachable_does_not_mean_unreferenced(self):
        """The scan skips tests/ by design, so a dead name can still carry
        fixtures and whole test modules. reprice.py is the case in point: four
        functions with no application caller, three test consumers, 480 lines in
        the deletion footprint rather than 169."""
        self._dead("reprice_block")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        consumers = [f for f in os.listdir(os.path.join(root, "tests"))
                     if f.endswith(".py")
                     and "import reprice" in open(os.path.join(root, "tests", f)).read()]
        self.assertTrue(consumers,
                        "if reprice.py's test consumers have gone, this test has "
                        "outlived the point it was making — delete it")

    def test_the_first_five_deletions_are_gone_and_their_siblings_live(self):
        """§4a's first exercise, 22 Sep 2026. Each of these had a LIVE near-twin,
        which is exactly how a careless deletion takes the wrong one."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for fn, gone in (("engine.py", "def io_save("),
                         ("iog_cap.py", "def _hours("),
                         ("energy_engine_io.py", "def save_json("),
                         ("block_store.py", "def get_setting("),
                         ("energy_charts.py", "def rate_breakdown_html(")):
            with open(os.path.join(root, fn), encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn(gone, src, "%s came back in %s" % (gone, fn))
        self._live("save_json_atomic", "get_settings", "get_meta", "cap_usage")

    def test_generated_mirrors_are_not_evidence_of_use(self):
        """`store_types.py` binds every read method of BlockStore as a class
        attribute. Scanning it marked 25 genuinely dead names reachable —
        including `get_reads_for_block` and `get_reads_for_range`, two of the five
        `reads`-table methods §4 documents as dead. A generated mirror of an API
        is not a caller of it.

        Caught by the ratchet when the file was added, which is the ratchet
        earning its place: the failure mode was a baseline that silently got 25
        names smaller and stopped protecting them.
        """
        self._dead("get_reads_for_block", "get_reads_for_range", "insert_read",
                   "link_reads_to_block", "purge_reads_older_than")
        self._dead("get_generation_mix", "tombstoned_slots_in", "get_drift_alerts")

    def test_workstream_a_scaffolding_is_now_wired(self):
        """`store_access.bind` was dead when the module landed — scaffolding ahead
        of its callers. `server.init()` calls it now, so the exception is spent.
        It is asserted LIVE rather than deleted, so the wiring cannot quietly go
        away again."""
        self._live("bind")

    def test_the_test_infrastructure_exceptions(self):
        """Test-only helpers living in production code, named rather than
        exempted by a rule — a rule like "anything ending _for_tests is fine" is
        a loophole anyone can walk through.

        `bind_object_for_tests` is the one hole in A's read/write boundary: parts
        of the suite hand the server a MagicMock with no openable path, and a
        second connection to such a thing does not exist. Production calls
        `bind()`, which takes a path.

        `reset_for_tests` is NO LONGER in the dead set, and the reason is the
        tool's documented over-approximation rather than a change in the code:
        `job_coordinator.__all__` lists it as a string, and an identifier-shaped
        string literal is indistinguishable here from a `getattr`. Same mechanism
        as `carbon.ev_carbon`, which is reachable only through dead code but has
        its name in a JSON payload. The tool lying about ALIVE, which is the safe
        direction — it means less gets deleted, not more.
        """
        exceptions = sorted(n for n in self.dead if n.endswith("_for_tests"))
        self.assertEqual(exceptions, ["bind_object_for_tests"])
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        owners = []
        for f in sorted(os.listdir(root)):
            if not f.endswith(".py"):
                continue
            with open(os.path.join(root, f), encoding="utf-8") as fh:
                if "def reset_for_tests" in fh.read():
                    owners.append(f)
        # Two modules is a pattern; a third is a convention that should be
        # extracted rather than listed, and this test should change shape then.
        self.assertEqual(owners, ["job_coordinator.py", "store_access.py"])

    def test_the_414_markers_are_live(self):
        self._live("stamp_version", "stamp_supplier", "_reconcile_decision")

    def test_carbon_functions_the_trace_cannot_settle(self):
        """A documented limitation, pinned so it is not mistaken for a finding.
        `ev_carbon` is reachable only via the dead `carbon_from_reprice`, but a
        JSON payload key "ev_carbon" in server.py is indistinguishable from a
        getattr, so the trace reports it live. Its sibling has no such string."""
        self._dead("carbon_from_reprice", "house_carbon")
        self._live("ev_carbon")           # over-approximated — see the tool's docstring
        self._live("period_ev_saving", "slot_intensity")


class TestBaselineGuard(unittest.TestCase):
    def test_baseline_exists_and_matches(self):
        self.assertTrue(os.path.exists(_BASE), "the baseline is the regression guard")
        r = _run("--baseline", _BASE)
        self.assertEqual(r.returncode, 0,
                         "the unreachable set has grown — wire it up or delete it "
                         "under §4a, but do not let dead code accumulate:\n" + r.stdout)


if __name__ == "__main__":
    unittest.main()
