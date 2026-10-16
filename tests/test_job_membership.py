"""
B acceptance 1 — the membership ratchet, written before the coordinator.

This is the test that decides whether B ends like `run_exclusive`: a correct
mechanism with one caller passing `lambda: None`. A §5.3 is the precedent —
route adoption went 0% -> 100% and stayed there because the structural test was
written first and failed the build on the wrong choice.

It starts at 23/23 unregistered and green. Green because the guard is a RATCHET,
not an assertion of the finished state: the unregistered set may shrink and must
never grow. A new writing job that skips the coordinator fails on the commit that
adds it.
"""
import json
import os
import subprocess
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOL = os.path.join(_ROOT, ".build", "check_job_membership.py")
_BASE = os.path.join(_ROOT, ".build", "job_membership_baseline.json")


def _run(*a):
    return subprocess.run([sys.executable, _TOOL] + list(a),
                          capture_output=True, text=True, cwd=_ROOT)


class TestMembershipRatchet(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.d = json.loads(_run("--json").stdout)

    def test_the_unregistered_set_has_not_grown(self):
        r = _run("--baseline", _BASE)
        self.assertEqual(r.returncode, 0,
                         "a writing job is outside the coordinator:\n" + r.stdout)

    def test_every_member_still_exists(self):
        """The member list is maintained by hand — deliberately, since
        "long-running writer" is a judgement no signature exposes. The cost is
        that a rename leaves a ghost, and a ghost quietly shrinks the
        denominator."""
        self.assertEqual(self.d["not_found"], [],
                         "members named in the list no longer exist: %s"
                         % self.d["not_found"])

    def test_the_live_tick_is_a_member(self):
        """It was missing from the design's first draft. It is the only member
        whose input moves whether or not the job runs, so it is the one that can
        never be deferred — which makes leaving it out worse than leaving out any
        of the other 22."""
        all_members = set(self.d["registered"]) | set(self.d["unregistered"])
        self.assertIn("engine_loop_task", all_members)

    def test_the_count_matches_the_design(self):
        """16 — and the number has moved five times, in both directions, for good
        reasons rather than because it was guessed at.

        23 first, which missed the three background WORKERS —
        `_delete_worker`, `_purge_worker`, `_restore_worker` — were missed because
        the first list was built from "engine jobs" + "route handlers" and had no
        category for a thread spawned by a route. They are the members others most
        want to avoid: `delete_in_progress()` is the second most consulted guard in
        the codebase. 17 to 16 on 16 Oct 2026: the settled EV split heal was removed (bill and
        split paths design §7.3)."""
        self.assertEqual(self.d["members"], 16)

    def test_the_baseline_is_a_countdown_not_a_record(self):
        """If this ever reaches zero the ratchet is finished and acceptance 1 is
        met — at which point this test should become an assertion that the
        unregistered set is EMPTY, and the baseline file deleted."""
        with open(_BASE, encoding="utf-8") as fh:
            known = json.load(fh)["unregistered"]
        self.assertLessEqual(len(self.d["unregistered"]), len(known))
        if not known:
            self.fail("acceptance 1 is met — replace the ratchet with an "
                      "assertion that nothing is unregistered")


class TestConsumptionNotJustRegistration(unittest.TestCase):
    """The gap acceptance 1 leaves open.

    The membership ratchet counts REGISTRATION. A member can declare
    `depends_on` and never consult `admission()` — the declaration is then
    decorative, the number looks healthy, and nothing says otherwise. That is
    exactly how v4's read companion existed, was correct, and reached 0%
    adoption.

    So consumption is counted separately, and this is its ratchet: it may only
    grow.
    """

    @classmethod
    def setUpClass(cls):
        cls.d = json.loads(_run("--json").stdout)

    def test_the_one_outstanding_member_is_the_vacuum(self):
        """Not "the handlers that write" — the handlers that STOP the live tick. Of
        the two, api_meter_delete_data joined `delete` on 4 Oct 2026; api_db_vacuum
        never joins (B §4)."""
        self.assertEqual(sorted(self.d["unregistered"]), ["api_db_vacuum"])

    def test_the_consumers_so_far(self):
        self.assertEqual(sorted(self.d["consuming"]),
                         ["attribution", "carbon_backfill", "measured_drain",
                          "prune_tombstones", "reprice_sweep", "verify_pricing"])

    def test_a_declared_dependency_that_is_never_consulted_is_visible(self):
        """Not a failure — `delete` and `restore` declare `import` and do not consult
        it: they PRE-EMPT it (cancel and wait) instead, which is honoured but not
        through admission(). (`verify_pricing` was the one that declared and ignored
        it, until 4 Oct 2026.) What matters is that it is COUNTABLE, so "18
        registered" can never be mistaken for "18 coordinating".
        """
        import sys as _sys
        _sys.path.insert(0, _ROOT)
        import job_coordinator as C
        declared_with_deps = {n for n, d in C.registry().items()
                              if d["depends_on"] or d["exclusive_with"]}
        consuming = set(self.d["consuming"])
        self.assertTrue(declared_with_deps - consuming,
                        "every declared dependency is now consulted — replace this "
                        "with an assertion that the two sets are equal")


if __name__ == "__main__":
    unittest.main()
