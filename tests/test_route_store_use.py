"""
A §5 acceptance 3 — "a structural test ... fails if any route handler references
the writer directly ... the single most important item on this list."

v4 built the read companion three times, after three production incidents, and it
sits unused. Not because anyone disagreed with it: `_get_store()` is shorter,
older, autocompletes first, and works perfectly in development, where there is one
request at a time and no writer contending. The cost lands months later on someone
else's box as an intermittent 500.

This is the test that makes the wrong choice fail at build time instead. It runs
against v4's code today, so it also measures the size of the port before the port
starts.
"""
import json
import os
import subprocess
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOL = os.path.join(_ROOT, ".build", "check_route_store_use.py")
_BASE = os.path.join(_ROOT, ".build", "route_store_baseline.json")


def _run(*args):
    return subprocess.run([sys.executable, _TOOL] + list(args),
                          capture_output=True, text=True, cwd=_ROOT)


class TestRouteStoreUse(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.d = json.loads(_run("--json").stdout)

    def test_writer_use_has_not_grown(self):
        r = _run("--baseline", _BASE)
        self.assertEqual(r.returncode, 0,
                         "a GET route has started reading on the writer:\n" + r.stdout)

    def test_the_baseline_is_empty_and_that_is_the_point(self):
        """While the port was in progress the baseline was a ratchet: a list of
        the 44 routes still on the writer, allowed to shrink and never to grow.

        It is empty now, which changes what it means — not a vacuous guard but an
        absolute one. Any route reaching `_get_store()` is a new entry, and the
        ratchet reports it as growth. The list form still matters: a route moving
        off while another moves on cannot net out to "no change".
        """
        with open(_BASE, encoding="utf-8") as fh:
            known = set(json.load(fh)["get_on_writer"])
        self.assertEqual(known, set())
        self.assertLessEqual(set(self.d["get_on_writer"]), known)

    def test_no_get_route_reaches_the_writer(self):
        """Was 44 of 44, adoption 0%. The port moved every one onto `store()`.

        This is the acceptance-3 invariant in its finished form: not "writer use
        has not grown" but "there is none". If it ever fails, a route has been
        written against `_get_store()` again — the 4% drift, starting over.
        """
        self.assertEqual(self.d["get_on_writer"], [])
        # +2: /api/periods, /api/energy (C §3); −3: /api/charts/blocks-summary, blocks-day,
        # data-version, deleted by C step 1 (1 Oct 2026).
        # −8: the Insights/Usage period routes, deleted by C step 2 (1 Oct 2026).
        # −1: /api/billing, deleted (Live Power reads /api/periods?kind=live), 1 Oct 2026.
        # −1: /api/review-blocks, deleted with the review list (BL-74, 4 Oct 2026).
        # −1: /api/blocks/sweep-implausible, deleted with the #307 self-heal (BL-74).
        # +1: /api/blocks/settlement, the settlement detail panel (13 Oct 2026).
        # +1: /api/meter/<id>/delete-preview, a device delete's figures (19 Oct 2026).
        # +1: /api/data-health, Data Management's health strip (21 Oct 2026).
        # −2: /api/unsettled-blocks, /api/blocks/deleted-ranges, with the sub-pages (21 Oct 2026).
        # +1: /data-management, which reads whether there is imported history (21 Oct 2026).
        # +1: /api/battery-sensor/flagged, a battery sensor found counting solar (29 Oct 2026).
        # +1: /api/tariff-agreements, Usage Stats' tariff overlay (31 Oct 2026).
        self.assertEqual(len(self.d["get_on_companion"]), 36)

    def test_adoption_is_total(self):
        """Measured at 0% before the port: v4 credited one route with the read
        companion, but `api_review_blocks` also called `load_config()`, which
        reached for the writer. Now 44/44."""
        n = len(self.d["get_on_companion"]) + len(self.d["get_on_writer"])
        self.assertEqual(len(self.d["get_on_companion"]), n)


class TestGetRoutesThatWrite(unittest.TestCase):
    """Three GET routes mutate the database from a Flask worker thread, using the
    engine's connection. That is not a v5 design concern — it is the live bug
    class this workstream exists to remove, quoted from engine.py itself:

        "a DB write from the executor thread races the loop-thread writers on
         that one connection and corrupts it: 'cannot commit - no transaction is
         active' / 'error return without exception set'"

    None of the three hops to the engine loop first. Under A's model `writer()`
    raises off the engine thread, so all three must move to asking the engine to
    perform the write — which is the boundary workstream B formalises.
    """

    @classmethod
    def setUpClass(cls):
        cls.d = json.loads(_run("--json").stdout)

    def test_no_route_writes_through_the_request_thread(self):
        """All three now hop to the engine via `_engine_write`. The worst was
        `/api/blocks/deleted-ranges` — GET-only, and it DELETEd, so a browser
        prefetch could fire it."""
        self.assertEqual(self.d["get_routes_that_write"], {})


if __name__ == "__main__":
    unittest.main()
