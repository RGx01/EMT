"""
Workstream B's starting position, pinned.

The programme map says of B: *"v4 got this right for some jobs and not others —
that asymmetry is the bug."* This measures the asymmetry so it can only narrow.

What the baseline is NOT: a list of races. Every one of these runs on the single
engine loop, so two coroutines interleave at `await` points rather than running in
parallel — workstream A removed the parallel-thread class. The hazard is logical:
a batch reading rows another job is part-way through rewriting. Real, and silent,
but a weaker claim than "these nine race", and the baseline should not be read as
the stronger one.
"""
import json
import os
import subprocess
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOL = os.path.join(_ROOT, ".build", "check_job_guards.py")
_BASE = os.path.join(_ROOT, ".build", "job_guard_baseline.json")


def _run(*a):
    return subprocess.run([sys.executable, _TOOL] + list(a),
                          capture_output=True, text=True, cwd=_ROOT)


class TestJobGuards(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.d = json.loads(_run("--json").stdout)

    def test_the_unguarded_set_has_not_grown(self):
        r = _run("--baseline", _BASE)
        self.assertEqual(r.returncode, 0,
                         "a job has lost its coordination:\n" + r.stdout)

    def test_callers_are_searched_not_just_job_bodies(self):
        """v4 sets the re-entry flags in the scheduler wrapper
        (`_maybe_reprice_history`), never in the job. Measuring the job body alone
        reported five guarded jobs as unguarded on the first pass."""
        self.assertIn("_reprice_history_running",
                      self.d["jobs"]["_run_historical_reprice_sweep"]["guards"])

    def test_old_style_coordination_only_ever_meant_those_two(self):
        """v4's starting position: every job that coordinated at all checked
        `api_import_running`, and mostly `delete_in_progress`. It was never that
        v4 lacked coordination — exactly two jobs were considered worth avoiding,
        and nothing else was.

        This tool measures the OLD mechanism, so migrated members legitimately
        drop out of it: the reprice sweep, carbon backfill and measured drain now
        consult `admission()` instead and no longer name either flag. The
        assertion is therefore about what old-style coordination MEANT, not about
        how many still use it — that count belongs to the membership ratchet.
        """
        # Two kinds of guard, conflated by the original audit and worth keeping
        # apart: a job's OWN re-entry flag ("don't overlap myself") is not
        # coordination with another job at all. `_carbon_backfill_running` is the
        # case that forced the distinction — the carbon backfill now defers via
        # `admission()` and keeps only its own re-entry flag.
        def _cross_job(job, guards):
            own = job.lstrip("_").replace("run_", "").replace("historical_", "")
            return {g for g in guards
                    if not (g.endswith("_running") and g.lstrip("_").split("_")[0]
                            in own.split("_"))}

        coordinating = {j: _cross_job(j, v["guards"])
                        for j, v in self.d["jobs"].items()
                        if v.get("live") and v.get("guards")}
        coordinating = {j: g for j, g in coordinating.items() if g}
        self.assertTrue(coordinating, "all old-style guards gone — retire this tool")
        for j, guards in coordinating.items():
            self.assertTrue(
                {"api_import_running", "delete_in_progress"} & guards,
                "%s coordinates against %s — worth a look" % (j, sorted(guards)))

    def test_the_dead_backfills_are_excluded_from_the_count(self):
        """Three of the seventeen are unreachable. Counting their guards would
        flatter the coverage figure with code that never runs."""
        for j in ("_run_historical_exc_backfill", "_run_historical_segment_backfill",
                  "_run_historical_iog_split_backfill"):
            self.assertFalse(self.d["jobs"][j]["live"])
        self.assertEqual(self.d["live_jobs"], 14)

    def test_run_exclusive_is_still_only_a_barrier(self):
        """`run_exclusive` is v4's one real mutual-exclusion primitive. Its single
        caller passes `lambda: None` — it is used to DRAIN the in-flight tick, not
        to run anything under the lock. The mechanism exists and coordinates
        nothing, which is the same shape as the read companion at 0% adoption.
        """
        hits = []
        for dirpath, dirnames, files in os.walk(_ROOT):
            dirnames[:] = [d for d in dirnames
                           if d not in (".git", "__pycache__", "tests", "docs",
                                        ".build", "scratch")]
            for f in files:
                if f.endswith(".py"):
                    p = os.path.join(dirpath, f)
                    with open(p, encoding="utf-8") as fh:
                        for i, line in enumerate(fh, 1):
                            if "run_exclusive(" in line and "def " not in line:
                                hits.append((os.path.relpath(p, _ROOT), line.strip()))
        self.assertEqual(len(hits), 1, "callers of run_exclusive: %s" % hits)
        self.assertIn("lambda: None", hits[0][1])


if __name__ == "__main__":
    unittest.main()
