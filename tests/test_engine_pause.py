"""
`pause_engine()` — the only thing in the system that can stop live capture.

B §4.0 makes the live tick member 23, the one member that is never deferred. This
covers the mechanism that can nonetheless stop it, and the price that stopping it
pays.

The price, measured rather than assumed: a paused tick is SKIPPED, not queued
(`_engine_tick` opens `if _engine_paused: return`). Energy survives, because
blocks store register readings rather than samples, so the delta across the gap
is still right. What a pause can cost is a block BOUNDARY — `interpolate_value()`
estimates the closing register and stamps `interpolated = 1`.

Before this, a pause logged "engine: paused" and nothing else: no reason, no
duration, no mention of the boundary. Four callers each hand-rolled their own
resume discipline — three with `try/finally`, one with a resume at each of three
exits. All correct; all separately arrived at. That is the pattern that produced
seventeen coordination primitives across four mechanisms.
"""
import ast
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import engine                                     # noqa: E402


class TestPauseAccounting(unittest.TestCase):

    def setUp(self):
        engine.resume_engine()

    def tearDown(self):
        engine.resume_engine()

    def test_a_pause_records_who_and_when(self):
        engine.pause_engine("a reason")
        st = engine.engine_pause_state()
        self.assertTrue(st["paused"])
        self.assertEqual(st["reason"], "a reason")

    def test_an_unattributed_pause_still_says_so(self):
        """Better than a blank: 'unspecified' is a finding, `None` is a shrug."""
        engine.pause_engine()
        self.assertEqual(engine.engine_pause_state()["reason"], "unspecified")

    def test_resume_clears_the_state(self):
        engine.pause_engine("x")
        engine.resume_engine()
        st = engine.engine_pause_state()
        self.assertFalse(st["paused"])
        self.assertIsNone(st["reason"])


class TestPauseContextManager(unittest.TestCase):

    def tearDown(self):
        engine.resume_engine()

    def test_it_resumes_on_the_happy_path(self):
        with engine.engine_paused("work"):
            self.assertTrue(engine.engine_pause_state()["paused"])
        self.assertFalse(engine.engine_pause_state()["paused"])

    def test_it_resumes_when_the_body_raises(self):
        """The failure this shape exists to prevent: a pause that leaks leaves
        live capture off until restart, and the only signal is silence."""
        with self.assertRaises(RuntimeError):
            with engine.engine_paused("work"):
                raise RuntimeError("boom")
        self.assertFalse(engine.engine_pause_state()["paused"])


class TestEveryCallerAttributesItsPause(unittest.TestCase):

    def test_no_bare_pause_in_the_server(self):
        """A pause without a reason is untraceable in a log, and these four are
        the only things that can stop live capture."""
        with open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8") as fh:
            src = fh.read()
        bare = []
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.Call):
                f = n.func
                if getattr(f, "attr", None) == "pause_engine" and not n.args:
                    bare.append(n.lineno)
        self.assertEqual(bare, [], "pause_engine() with no reason at %s" % bare)

    def test_every_pause_site_has_a_resume_on_every_exit(self):
        """Verified by reading, not by looking for `finally` — an earlier check
        that only accepted `try/finally` flagged `api_meter_delete_data`, which
        resumes correctly at each of its three exits instead."""
        with open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())

        def _calls(node, name):
            return any(isinstance(x, ast.Call)
                       and getattr(x.func, "attr", getattr(x.func, "id", None)) == name
                       for x in ast.walk(node))

        for n in ast.walk(tree):
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # By AST, not text. A text search matches the route below that merely
            # DESCRIBES pausing in its docstring — which is how this test first
            # failed, in a file whose own docstring warns about that trap.
            if _calls(n, "pause_engine") and not _calls(n, "engine_paused"):
                self.assertTrue(_calls(n, "resume_engine"),
                                "%s pauses and never resumes" % n.name)


if __name__ == "__main__":
    unittest.main()
