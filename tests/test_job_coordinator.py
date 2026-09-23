"""
The coordinator itself (B §3).

Registration is free by design — `@job(...)` records a declaration and returns
the function unchanged — so the whole membership can be declared before any
admission is wired in, and the ratchet counts down without behaviour moving
underneath it. These tests cover the decision logic; migrating a member to
actually consult it is separate.
"""
import os
import sys
import threading
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import job_coordinator as C                       # noqa: E402


class TestDeclaration(unittest.TestCase):

    def tearDown(self):
        C.reset_for_tests()

    def test_the_decorator_returns_the_function_unchanged(self):
        """The property that makes declaring 23 members a zero-risk commit."""
        def f(a, b=2):
            return a + b
        g = C.job("t_unchanged")(f)
        self.assertIs(g, f)
        self.assertEqual(g(1), 3)

    def test_the_real_membership_is_declared(self):
        import engine                              # noqa: F401
        reg = C.registry()
        for m in ("live_tick", "import", "attribution", "verify_pricing",
                  "reprice_sweep", "carbon_backfill", "measured_drain"):
            self.assertIn(m, reg)

    def test_dependencies_match_the_guards_v4_already_checks(self):
        """Not invented — read off `.build/check_job_guards.py`. Attribution waits
        on the import AND the verify pass because it reads the pricing both
        write: "attributing over a not-yet-verified (imp_rate NULL) block used to
        crash PASS 2 and could cost the device at 0"."""
        import engine                              # noqa: F401
        att = C.registry()["attribution"]
        self.assertEqual(set(att["depends_on"]), {"import", "verify_pricing"})
        self.assertEqual(att["on_conflict"], C.WAIT)


class TestAdmission(unittest.TestCase):

    def setUp(self):
        C.reset_for_tests()

    def tearDown(self):
        C.reset_for_tests()

    def test_a_member_waits_for_what_it_reads(self):
        import engine                              # noqa: F401
        self.assertEqual(C.admission("attribution"), (C.ADMIT, None))
        C.claim("import")
        self.assertEqual(C.admission("attribution"), (C.WAIT, "import"))

    def test_a_sweep_defers_rather_than_waiting(self):
        """v4's own behaviour: "import job active — deferring"; the next tick
        re-checks. A recurring sweep loses nothing by waiting, and a coordinator
        offering only admit/refuse would force it to fail instead."""
        import engine                              # noqa: F401
        C.claim("import")
        self.assertEqual(C.admission("reprice_sweep"), (C.DEFER, "import"))

    def test_the_live_tick_is_never_blocked(self):
        """Member 23. Its input is a meter register, which moves whether or not
        the job runs — so deferring it has a cost outside the database that no
        other member has."""
        import engine                              # noqa: F401
        C.claim("import")
        C.claim("attribution", "ev")
        self.assertEqual(C.admission("live_tick"), (C.ADMIT, None))

    def test_an_undeclared_blocker_never_silently_admits(self):
        """A dependency naming a member that does not exist is a typo, and a typo
        that admits is worse than one that never fires."""
        C.job("t_typo", depends_on=("no_such_member",))(lambda: None)
        self.assertEqual(C.admission("t_typo"), (C.ADMIT, None))
        self.assertIn("no_such_member", C.registry()["t_typo"]["depends_on"])


class TestClaim(unittest.TestCase):

    def setUp(self):
        C.reset_for_tests()

    def tearDown(self):
        C.reset_for_tests()

    def test_two_runs_of_the_same_key_cannot_both_claim(self):
        self.assertEqual(C.claim("attribution", "ev_charger"), (True, None))
        ok, why = C.claim("attribution", "ev_charger")
        self.assertFalse(ok)
        self.assertEqual(why, "already running")

    def test_two_runs_of_different_keys_can(self):
        """Attributing two devices is legitimate — different meter_ids. It was
        only the bookkeeping that was single-tenant."""
        self.assertTrue(C.claim("attribution", "ev_charger")[0])
        self.assertTrue(C.claim("attribution", "house_battery")[0])

    def test_release_frees_a_claim_that_never_ran(self):
        C.claim("import")
        C.release("import")
        self.assertFalse(C.running("import"))

    def test_release_does_not_touch_a_run_in_progress(self):
        """A claim that became a run is not a claim any more; releasing it would
        let a second start in underneath the first."""
        C.claim("import")
        C._runs[("import", "import")]["status"] = "running"
        C.release("import")
        self.assertTrue(C.running("import"))

    def test_the_claim_is_taken_under_a_lock(self):
        """Two Flask threads can reach this at the same moment. Check-then-set
        without a lock is the attribution window one layer down."""
        C.reset_for_tests()
        results = []
        barrier = threading.Barrier(8)

        def go():
            barrier.wait()
            results.append(C.claim("import")[0])

        ts = [threading.Thread(target=go) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sum(1 for r in results if r), 1,
                         "exactly one of eight concurrent claims may win")


if __name__ == "__main__":
    unittest.main()


class TestAttributionIsTheFirstMember(unittest.TestCase):
    """B §3.3 — the coordinator generalises attribution's registry, so attribution
    is the first member migrated and the generalisation gets tested against the
    case it came from.

    The split is deliberate and worth stating, because two records of one thing
    is the §3.4 failure: the COORDINATOR owns identity and admission (is this
    device taken), `_attribution_jobs` owns progress (counters, control, phase)
    because that is the shape the page polls and workstream C owns that surface.
    """

    def setUp(self):
        import engine
        C.reset_for_tests()
        engine._attribution_jobs.clear()

    def tearDown(self):
        import engine
        C.reset_for_tests()
        engine._attribution_jobs.clear()

    def test_a_claim_registers_with_the_coordinator(self):
        import engine
        self.assertTrue(engine.attribution_claim("ev_charger", ["s"]))
        self.assertTrue(C.running("attribution", "ev_charger"))

    def test_the_coordinator_is_what_refuses_a_second_run(self):
        import engine
        engine.attribution_claim("ev_charger", ["s"])
        self.assertFalse(engine.attribution_claim("ev_charger", ["s"]))

    def test_different_devices_still_run_together(self):
        import engine
        self.assertTrue(engine.attribution_claim("ev_charger", ["s"]))
        self.assertTrue(engine.attribution_claim("house_battery", ["s"]))

    def test_a_finished_run_frees_the_device(self):
        """Both records move at the end of a run. A member that claims and never
        marks holds its key until restart."""
        import engine
        engine.attribution_claim("ev_charger", ["s"])
        engine._attribution_jobs["ev_charger"]["status"] = "done"
        C.mark("attribution", "ev_charger", status="done")
        self.assertTrue(engine.attribution_claim("ev_charger", ["s"]))

    def test_release_clears_both_records(self):
        import engine
        engine.attribution_claim("ev_charger", ["s"])
        engine.attribution_release("ev_charger")
        self.assertFalse(C.running("attribution", "ev_charger"))
        self.assertNotIn("ev_charger", engine._attribution_jobs)

    def test_every_coordinator_call_is_in_the_right_function(self):
        """A blanket text replace put `_coord.mark("attribution", device_meter_id,
        ...)` into `run_api_import_job` and `run_gap_fill_job`, where that name
        does not exist — caught by the suite as
        `name 'device_meter_id' is not defined`, but only because those paths are
        exercised. A member marking another member's run would be worse and
        quieter."""
        import ast
        import os as _os
        root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        with open(_os.path.join(root, "engine.py"), encoding="utf-8") as fh:
            src = fh.read()
        lines = src.split("\n")
        tree = ast.parse(src)

        def owner(ln):
            best = None
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and n.lineno <= ln <= n.end_lineno:
                    if best is None or n.lineno > best.lineno:
                        best = n
            return best.name if best else "?"

        stray = [(i, owner(i)) for i, l in enumerate(lines, 1)
                 if '_coord.mark("attribution"' in l
                 and owner(i) != "run_attribution_job"]
        self.assertEqual(stray, [], "attribution marks outside its job: %s" % stray)

    def test_the_wait_loop_now_asks_the_coordinator(self):
        """The payoff. Attribution is the first member to CONSUME admission
        rather than only feed it.

        The loop was three hand-rolled predicate calls — `delete_in_progress() or
        api_import_running() or _verify_running()` — with the reason for each in
        a comment above them. That reason is the declaration now, so the loop asks
        one question and a fourth blocker becomes a declaration rather than an
        edit here.
        """
        import ast
        import os as _os
        root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        with open(_os.path.join(root, "engine.py"), encoding="utf-8") as fh:
            src = fh.read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == "run_attribution_job")
        calls = {getattr(x.func, "id", getattr(x.func, "attr", None))
                 for x in ast.walk(fn) if isinstance(x, ast.Call)}
        self.assertIn("admission", calls)
        for gone in ("delete_in_progress", "api_import_running", "_verify_running"):
            self.assertNotIn(gone, calls, "%s is still hand-rolled here" % gone)

    def test_the_users_pause_is_not_a_dependency(self):
        """The member's own control stays separate from admission. Conflating them
        would make `admission()` answerable by the UI."""
        import ast
        import os as _os
        root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        with open(_os.path.join(root, "engine.py"), encoding="utf-8") as fh:
            src = fh.read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == "run_attribution_job")
        body = "\n".join(src.split("\n")[fn.lineno - 1:fn.end_lineno])
        self.assertIn('j.get("control") == "pause"', body)
        self.assertIn("paused by request", body)

    def test_migrating_deleted_a_predicate(self):
        """The coordinator's first removal rather than addition. `_verify_running()`
        existed for exactly one caller; its docstring WAS the dependency, and that
        sentence is now the declaration. The reachability ratchet surfaced it on
        the same commit that moved the loop."""
        import os as _os
        root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        with open(_os.path.join(root, "engine.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("def _verify_running(", src)


class TestTheManifestIsMeasuredNotInvented(unittest.TestCase):
    """Every declared edge must be traceable to something v4 actually did.

    The design says dependencies are read off the guard audit, not invented. One
    slipped through anyway: `import exclusive_with=("delete",)`. Checked against
    the pre-migration source, `run_api_import_job` never called
    `delete_in_progress()` and the start route gates on `kraken_available()`,
    `api_import_running()` and the verify pass only — so declaring it would have
    added a behaviour v4 does not have, under cover of "the graph is measured".
    """

    def test_the_import_declares_nothing_it_never_checked(self):
        d = C.registry()["import"]
        self.assertEqual(d["depends_on"], ())
        self.assertEqual(d["exclusive_with"], (),
                         "an import deferring to a delete is a behaviour CHANGE — "
                         "B §6 q5, not a manifest entry")

    def test_the_edges_that_are_declared_have_a_named_source(self):
        """Each of these was checked by the pre-migration code. Recorded as the
        list so a future addition has to say where it came from."""
        expected = {
            "attribution": {"import", "verify_pricing", "delete"},
            "carbon_backfill": {"import", "delete"},
            "measured_drain": {"import", "delete"},
            "reprice_sweep": {"import", "delete"},
            "verify_pricing": {"import"},
            "restore": {"import"},
            "delete": {"import"},
        }
        reg = C.registry()
        # manifest entries only — this file declares fixture members with
        # deliberate typos, and declarations are module-global
        actual = {n: set(d["depends_on"]) | set(d["exclusive_with"])
                  for n, d in reg.items()
                  if (d["depends_on"] or d["exclusive_with"])
                  and d.get("from_manifest")}
        self.assertEqual(actual, expected,
                         "a declared edge changed — name its evidence in the "
                         "manifest comment before adding it here")


class TestRunExclusiveDoesNotComeAcross(unittest.TestCase):
    """B §6 q6, closed. The evidence, pinned — because the conclusion rests on it
    and all three parts are quiet properties of the code rather than anything a
    reader would notice."""

    @classmethod
    def setUpClass(cls):
        import os as _os
        root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        with open(_os.path.join(root, "engine.py"), encoding="utf-8") as fh:
            cls.src = fh.read()
        cls.lines = cls.src.split("\n")

    def test_the_tick_lock_is_taken_in_exactly_two_places(self):
        """`run_exclusive` and the tick. If a long-running job ever starts taking
        it, the "exclusion is partial" argument changes and q6 should be re-opened.
        """
        import ast
        uses = [n.lineno for n in ast.walk(ast.parse(self.src))
                if isinstance(n, ast.AsyncWith)
                for item in n.items
                if isinstance(item.context_expr, ast.Name)
                and item.context_expr.id == "_engine_loop_lock"]
        self.assertEqual(len(uses), 2, "acquired at lines %s" % uses)

    def test_no_long_running_job_takes_it(self):
        """So it excludes the TICK, not the other writers."""
        import ast
        funcs = {n.name: n for n in ast.walk(ast.parse(self.src))
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for job in ("run_api_import_job", "_run_historical_reprice_sweep",
                    "run_measured_history_drain", "run_attribution_job",
                    "run_gap_fill_job"):
            fn = funcs[job]
            body = "\n".join(self.lines[fn.lineno - 1:fn.end_lineno])
            self.assertNotIn("_engine_loop_lock", body, job)

    def test_what_replaced_it_does_not_use_an_executor(self):
        """The part A §3.3 forbids. `drain_tick` waits on the lock and returns —
        no thread, no work run anywhere but the loop."""
        import ast
        fn = next(n for n in ast.walk(ast.parse(self.src))
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "drain_tick")
        body = "\n".join(self.lines[fn.lineno - 1:fn.end_lineno])
        self.assertNotIn("run_in_executor", body)

    def test_it_is_gone(self):
        """Deleted 23 Sep 2026 under §4a, after its single caller — which passed
        `lambda: None` and never ran anything under the lock — was replaced by
        `drain_tick()`, named for what it actually did.

        The three findings above are why, and they are kept rather than deleted
        with the code: if anyone reintroduces a mutual-exclusion primitive here,
        the argument against this one should be readable without archaeology.
        """
        # By AST. A text search matches `drain_tick`'s own docstring, which
        # explains what it replaced — the seventh time in this work that a
        # source-text check has matched prose rather than code.
        import ast
        import os as _os
        root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        for dirpath, dirnames, files in _os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in
                           (".git", "__pycache__", "tests", "docs", ".build", "scratch")]
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = _os.path.join(dirpath, f)
                with open(path, encoding="utf-8") as fh:
                    try:
                        tree = ast.parse(fh.read())
                    except SyntaxError:
                        continue
                for n in ast.walk(tree):
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                            and n.name == "run_exclusive":
                        self.fail("run_exclusive defined again in %s" % f)
                    if isinstance(n, ast.Call) and \
                            getattr(n.func, "attr", getattr(n.func, "id", None)) \
                            == "run_exclusive":
                        self.fail("run_exclusive called again in %s:%d" % (f, n.lineno))


class TestPreemptIsTheFourthOutcome(unittest.TestCase):
    """The design named three admission outcomes. v4 uses four.

    Restore and delete each CANCEL a running import, wait up to 60s, and abort
    themselves if it will not stop — `web/server.py:2073` and `:3810`. Not
    waiting: a user has asked for a destructive operation that cannot coexist
    with an import, and the import is resumable, so the import yields.

    Declaring those two with the default DEFER would have said they stand aside
    for an import — the exact opposite of what they do. `delete` previously
    declared nothing at all, which was equally wrong in a quieter way.
    """

    def setUp(self):
        C.reset_for_tests()

    def tearDown(self):
        C.reset_for_tests()

    def test_both_destructive_members_preempt(self):
        C.claim("import", force=True)
        self.assertEqual(C.admission("delete"), (C.PREEMPT, "import"))
        self.assertEqual(C.admission("restore"), (C.PREEMPT, "import"))

    def test_nothing_else_preempts(self):
        """Pre-emption destroys another member's work. Two members do it, both
        user-initiated and both destructive by nature; a third would need saying
        out loud."""
        preemptors = sorted(n for n, d in C.registry().items()
                            if d.get("from_manifest")
                            and d["on_conflict"] == C.PREEMPT)
        self.assertEqual(preemptors, ["delete", "restore"])

    def test_the_code_really_does_cancel_the_import(self):
        """The evidence, asserted rather than cited — if either stops cancelling,
        the manifest is describing something that no longer happens."""
        import os as _os
        root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        with open(_os.path.join(root, "web", "server.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertEqual(src.count('api_import_control("cancel")'), 2)


class TestDeclarationsMustAllResolve(unittest.TestCase):
    """The import-order trap, and the check that closes it."""

    def test_every_declared_blocker_is_itself_declared(self):
        """With BOTH modules imported — which is what the application does.

        `admission()` ignores an unknown blocker rather than treating it as
        present, because a typo should not block a member forever. The cost is
        that an unknown blocker silently does not apply, and there are two ways to
        get one: a typo, and a module not yet imported.

        The second bit: `engine` alone knows nothing of `delete`, declared on
        `_delete_worker` in `web/server.py`, and FIVE members' exclusion on it
        quietly did not apply.
        """
        import engine                              # noqa: F401
        import sys as _sys
        import os as _os
        _sys.path.insert(0, _os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "web"))
        import server                              # noqa: F401
        # Scoped to the real declaring modules: this file declares fixture
        # members with deliberate typos, and they are module-global like any
        # other declaration.
        self.assertEqual(C.validate(modules=("engine", "server")), {},
                         "a declared blocker names nothing declared — either a "
                         "typo, or a module that has not been imported")


class TestDeleteIsAMember(unittest.TestCase):
    """`_delete_worker` and `_purge_worker` share one flag and are one member."""

    def setUp(self):
        C.reset_for_tests()

    def tearDown(self):
        C.reset_for_tests()

    def test_the_flag_is_the_claim(self):
        """One chokepoint: `set_delete_active()` already existed and both workers
        call it in a `finally`. Making it claim means both are covered without
        touching either worker."""
        import engine
        engine.set_delete_active(True)
        self.assertTrue(C.running("delete"))
        engine.set_delete_active(False)
        self.assertFalse(C.running("delete"))

    def test_an_active_delete_blocks_the_members_that_exclude_it(self):
        import engine
        import sys as _sys
        import os as _os
        _sys.path.insert(0, _os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "web"))
        import server                              # noqa: F401
        engine.set_delete_active(True)
        self.assertEqual(C.admission("attribution"), (C.WAIT, "delete"))
        self.assertEqual(C.admission("reprice_sweep"), (C.DEFER, "delete"))
        self.assertEqual(C.admission("live_tick"), (C.ADMIT, None))


class TestVerifyPricingIsTheThirdMember(unittest.TestCase):
    """Attribution's second blocker.

    (Restored: an earlier edit here replaced a span of this file by slice and
    removed this class silently. The suite stayed green, because deleting tests
    never turns anything red — which is the whole argument for listing what a
    file covers rather than trusting a diff to be legible.)
    """

    def setUp(self):
        C.reset_for_tests()

    def tearDown(self):
        C.reset_for_tests()

    def test_an_active_verify_blocks_attribution(self):
        import engine                              # noqa: F401
        C.claim("verify_pricing", force=True)
        self.assertEqual(C.admission("attribution"), (C.WAIT, "verify_pricing"))

    def test_it_blocks_nothing_that_did_not_declare_it(self):
        """The reprice sweep reads what the IMPORT writes, not what the verify
        pass writes — it declares `import` only. A coordinator blocking everything
        on everything would be the lock this design rejects."""
        import engine                              # noqa: F401
        C.claim("verify_pricing", force=True)
        self.assertEqual(C.admission("reprice_sweep"), (C.ADMIT, None))
        self.assertEqual(C.admission("live_tick"), (C.ADMIT, None))

    def test_it_claims_for_the_states_the_old_predicate_covered(self):
        """`_verify_running()` counted running / waiting / paused, and is now
        deleted — attribution was its only caller. The claim spans the whole pass,
        so those states stay covered: a pass stopped for API allowance has not
        finished writing. Unlike the import, where `api_import_running()` genuinely
        missed "finalising", there was nothing to correct here."""
        import ast
        import os as _os
        root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        with open(_os.path.join(root, "engine.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("def _verify_running(", src)
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == "run_deferred_verify_pricing")
        body = "\n".join(src.split("\n")[fn.lineno - 1:fn.end_lineno])
        self.assertEqual(body.count('_coord.mark("verify_pricing", status="done")'), 2)
        self.assertEqual(body.count('_coord.claim("verify_pricing"'), 1)


class TestImportIsTheSecondMember(unittest.TestCase):
    """The import is the blocker four other members declare, so migrating it is
    what makes `admission()` mean anything for the first time."""

    def setUp(self):
        C.reset_for_tests()

    def tearDown(self):
        C.reset_for_tests()

    def test_an_active_import_blocks_the_members_that_read_its_output(self):
        import engine                              # noqa: F401
        C.claim("import", force=True)
        self.assertEqual(C.admission("attribution"), (C.WAIT, "import"))
        self.assertEqual(C.admission("reprice_sweep"), (C.DEFER, "import"))
        self.assertEqual(C.admission("carbon_backfill"), (C.DEFER, "import"))
        self.assertEqual(C.admission("measured_drain"), (C.DEFER, "import"))
        self.assertEqual(C.admission("verify_pricing"), (C.DEFER, "import"))

    def test_it_never_blocks_the_live_tick(self):
        import engine                              # noqa: F401
        C.claim("import", force=True)
        self.assertEqual(C.admission("live_tick"), (C.ADMIT, None))

    def test_the_coordinator_is_stricter_than_api_import_running(self):
        """A deliberate, named difference rather than a silent one.

        `api_import_running()` counts only running/paused/rate_limited — it
        returns False during "finalising". But finalising runs
        `_probe_and_apply_region_periods`, `_drain_reprice_queue_after_import` and
        a chart regen: all writes, including repricing. Admitting a job then
        admits it while the import is still rewriting the pricing it reads.

        The coordinator claims for the whole run and marks done only at a terminal
        status, so it covers finalising. v4's predicate is left exactly as it was,
        so nothing that calls it changes behaviour; members get the stricter view
        as they migrate.
        """
        import ast
        import os as _os
        root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        with open(_os.path.join(root, "engine.py"), encoding="utf-8") as fh:
            src = fh.read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "api_import_running")
        body = "\n".join(src.split("\n")[fn.lineno - 1:fn.end_lineno])
        self.assertIn("rate_limited", body)
        self.assertNotIn("finalising", body)
        self.assertNotIn("_coord", body, "v4's predicate must stay independent")

    def test_the_import_marks_every_terminal_status(self):
        """A member that claims and never marks holds its key until restart, and
        the import has five ways to end: done, error, cancelled, rate_limited and
        the outer handler."""
        import ast
        import os as _os
        root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        with open(_os.path.join(root, "engine.py"), encoding="utf-8") as fh:
            src = fh.read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_api_import_job")
        body = "\n".join(src.split("\n")[fn.lineno - 1:fn.end_lineno])
        self.assertEqual(body.count('_coord.mark("import", status="done")'), 5)
        self.assertEqual(body.count('_coord.claim("import"'), 1)


if __name__ == "__main__":
    unittest.main()
