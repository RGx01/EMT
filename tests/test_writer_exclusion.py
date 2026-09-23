"""
Backups, restores and attribution against the engine's writer — workstream B.

Three findings, each pinned so it cannot come back:

1. `_create_backup_zip` ran `PRAGMA wal_checkpoint(TRUNCATE)` and `.backup()` on
   the ENGINE's connection, from a Flask request thread. All eight callers are
   request threads or the restore worker — never the loop.
2. The restore path, three steps later in the same file, documents exactly why
   that is wrong. The correct pattern was written next to the wrong one.
3. `api_attribute_start` guards against a second run with `attribution_running()`,
   but the status it reads is set inside the coroutine — after a preflight worth
   up to 180s and a full database zip. The guard is real; the window is minutes.
"""
import ast
import os
import re
import sqlite3
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVER = os.path.join(_ROOT, "web", "server.py")


QUOTES = (chr(34) * 3, chr(39) * 3)


def _code_only(node, lines):
    """The function's source with comments and docstrings removed.

    Needed because these checks kept matching their own explanations: the fixed
    `_create_backup_zip` says "this used to run PRAGMA wal_checkpoint", and the
    attribution route's docstring names `_attribution_job`. A text search over
    source is a search over prose too — the same trap the `check_same_thread`
    census fell into before it moved to AST.
    """
    import io
    import tokenize
    seg = "\n".join(lines[node.lineno - 1:node.end_lineno])
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(seg).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and tok.line.lstrip()[:3] in QUOTES:
                continue
            out.append(tok.string)
    except (tokenize.TokenError, IndentationError):
        return seg
    return " ".join(out)


def _src():
    with open(_SERVER, encoding="utf-8") as fh:
        return fh.read()


class TestBackupUsesItsOwnConnection(unittest.TestCase):

    def test_backup_does_not_touch_the_engine_store(self):
        src = _src()
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_create_backup_zip")
        body = _code_only(fn, src.split("\n"))
        self.assertNotIn("get_store", body,
                         "_create_backup_zip is reaching the engine's connection "
                         "again — every caller is an off-loop thread")
        self.assertNotIn("wal_checkpoint", body,
                         "a checkpoint on someone else's connection is the "
                         "dangerous half, and it was never needed")
        self.assertIn("sqlite3", body, "it should open its own connection")
        self.assertIn("backup", body)

    def test_a_private_connection_captures_uncheckpointed_wal(self):
        """Why the checkpoint was never needed — the claim the fix rests on, so
        it is measured rather than asserted. SQLite's online backup reads through
        a normal connection, so committed transactions still in the WAL are in
        the copy, and an OPEN transaction correctly is not."""
        d = tempfile.mkdtemp()
        src, dst = os.path.join(d, "s.db"), os.path.join(d, "b.db")
        w = sqlite3.connect(src)
        w.execute("PRAGMA journal_mode=WAL")
        w.execute("CREATE TABLE t(x)")
        for i in (1, 2):
            w.execute("INSERT INTO t VALUES (?)", (i,))
            w.commit()
        self.assertTrue(os.path.exists(src + "-wal"))
        self.assertGreater(os.path.getsize(src + "-wal"), 0, "nothing in the WAL to prove")

        w.execute("INSERT INTO t VALUES (3)")          # open, uncommitted
        r = sqlite3.connect(src)
        b = sqlite3.connect(dst)
        r.backup(b, pages=100)
        b.close()
        r.close()
        c = sqlite3.connect(dst)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM t").fetchone()[0], 2,
                         "committed WAL rows must be in the backup, the open one not")
        c.close()
        w.close()

    def test_every_engine_store_reach_in_server_is_on_the_loop(self):
        """The general rule the backup broke. A reach is acceptable inside a
        coroutine scheduled onto the loop, or in a documented `else:` fallback for
        when no loop is running (tests / pre-init), where there is nothing to
        race."""
        lines = _src().split("\n")
        pat = re.compile(r"_eng[a-z_]*\.get_store\(\)|engine\.get_store\(\)")
        offenders = []
        for i, line in enumerate(lines, 1):
            if not pat.search(line) or line.strip().startswith("#"):
                continue
            window = "\n".join(lines[max(0, i - 16):i])
            if ("async def" in window or "run_coroutine_threadsafe" in window
                    or "_run_on_engine_loop" in window or "_engine_write" in window
                    or re.search(r"^\s+else:\s*$", window, re.M)):
                continue
            offenders.append((i, line.strip()[:70]))
        self.assertEqual(offenders, [],
                         "off-loop use of the engine's connection: %s" % offenders)


class TestRouteWritesShareOneConnection(unittest.TestCase):
    """The open half of BL-18c, in v4's own words.

    BL-18c gave every request thread its own READ connection after concurrent
    page-load reads on the shared handle produced intermittent SQLITE_MISUSE.
    The comment that records it ends: *"Reads only — writes still go through
    `_get_store()`."*

    `_store` is a module global and waitress runs `threads=4`, so every
    POST/PUT/DELETE handler writes through ONE connection shared by all four
    worker threads — the same condition, acknowledged and left open.

    Note what this is NOT: it is not the engine race. `server._store` and
    `engine._store` are different connections to the same file, so the routes
    contend with the engine through SQLite's own WAL locking, which is what that
    is for. The hazard here is threads sharing ONE connection object.
    """

    def test_the_server_write_store_is_a_module_global(self):
        src = _src()
        self.assertIn("_store: BlockStore | None = None", src,
                      "if this became thread-local, the finding is closed")
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "_get_store")
        body = _code_only(fn, src.split("\n"))
        self.assertIn("global", body)
        self.assertNotIn("_local", body, "still not per-thread")

    def test_waitress_serves_on_several_threads(self):
        """One thread would make the shared connection harmless."""
        m = re.search(r"threads\s*=\s*(\d+)", _src())
        self.assertIsNotNone(m)
        self.assertGreater(int(m.group(1)), 1)

    def test_the_read_side_is_per_thread(self):
        """The half that was fixed, kept alongside so the asymmetry is visible."""
        self.assertIn("threading.local()", _src())


class TestASnapshotMidJobIsRecoverable(unittest.TestCase):
    """Why a backup does not need to EXCLUDE writers, only be consistent.

    The online backup gives a transactionally valid snapshot, but a valid snapshot
    can still catch a long job half-done — the reprice sweep with some blocks
    swept and some not. That only matters if such a state cannot be recovered
    from, so: every live long-running write job is either cursor/marker resumable
    or idempotent by query.

    Pinned because it is the assumption the backup design rests on. If a job is
    ever added that is neither, a backup taken while it runs becomes a restore
    that cannot finish what it interrupted — and nothing else would say so.
    """

    JOBS_WITH_MARKERS = (
        "_run_historical_carbon_backfill", "_run_historical_reprice_sweep",
        "run_api_import_job", "run_band_rate_snap", "run_deferred_verify_pricing",
        "run_measured_history_drain", "run_smb_device_recost", "run_smb_ev_resplit",
        "run_smb_rate_repair",
    )

    @classmethod
    def setUpClass(cls):
        path = os.path.join(_ROOT, "engine.py")
        with open(path, encoding="utf-8") as fh:
            cls.src = fh.read()
        cls.lines = cls.src.split("\n")
        cls.funcs = {n.name: n for n in ast.walk(ast.parse(cls.src))
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def _body(self, name):
        f = self.funcs[name]
        return "\n".join(self.lines[f.lineno - 1:f.end_lineno])

    def test_the_marker_jobs_still_carry_a_marker(self):
        pat = re.compile(r"_MARKER|_DONE_KEY|set_kraken_state|set_meta|cursor|after_start")
        for job in self.JOBS_WITH_MARKERS:
            self.assertTrue(pat.search(self._body(job)),
                            "%s has lost its resume/done marker — a backup taken "
                            "while it runs is no longer recoverable" % job)

    def test_the_carve_is_idempotent_by_query_instead(self):
        """`run_iog_split_carve` is the one job with no marker, deliberately:
        *"No done-marker — it runs each tick and is cheap (only late-dispatch
        stragglers ever match)."* It self-selects on `imp_kwh_ev IS NULL`, so a
        block it has carved stops matching. That is idempotence by query, and it
        is only true while that predicate is in the SELECT."""
        body = self._body("run_iog_split_carve")
        self.assertIn("imp_kwh_ev IS NULL", body)


class TestAttributionStartWindow(unittest.TestCase):
    """The check-then-act window, shown rather than argued."""

    def setUp(self):
        src = _src()
        tree = ast.parse(src)
        self.fn = next(n for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef) and n.name == "api_attribute_start")
        self.lines = src.split("\n")

    def _line_of(self, needle):
        for i in range(self.fn.lineno - 1, self.fn.end_lineno):
            if needle in self.lines[i]:
                return i + 1
        return None

    def test_the_run_is_claimed_before_any_slow_work(self):
        """The fix, asserted as an ordering.

        The route used to check `attribution_running()` and then — still on the
        Flask thread — run a preflight (`timeout=180`) and zip the whole database
        before scheduling the coroutine that finally set the status. Two starts
        inside that gap both passed.

        Now `attribution_claim()` reserves the device under a lock, and it comes
        FIRST. If this ordering ever inverts, the window is back.
        """
        claim = self._line_of("attribution_claim")
        preflight = self._line_of("attribution_preflight")
        backup = self._line_of("_create_backup_zip")
        schedule = self._line_of("run_attribution_job")
        for name, val in (("claim", claim), ("preflight", preflight),
                          ("backup", backup), ("schedule", schedule)):
            self.assertIsNotNone(val, "could not locate the %s" % name)
        self.assertLess(claim, preflight, "the claim must precede the preflight")
        self.assertLess(claim, backup, "the claim must precede the backup")
        self.assertLess(backup, schedule)

    def test_a_refused_claim_is_a_409(self):
        body = _code_only(self.fn, self.lines)
        self.assertIn("409", body)
        self.assertIn("attribution_claim", body)

    def test_every_path_after_the_claim_releases_it(self):
        """A claim that is never released locks the device out until restart. The
        two ways to leave the route after claiming are the preflight refusal and
        an exception, and both must release."""
        body = _code_only(self.fn, self.lines)
        self.assertGreaterEqual(body.count("attribution_release"), 2,
                                "a path after the claim can leave the device "
                                "permanently reserved")

    def test_the_claim_is_taken_under_a_lock(self):
        """Two Flask threads can call this at the same moment; a check-then-set
        without a lock is the same bug one layer down."""
        with open(os.path.join(_ROOT, "engine.py"), encoding="utf-8") as fh:
            src = fh.read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "attribution_claim")
        body = _code_only(fn, src.split("\n"))
        self.assertIn("_attribution_lock", body)

    def test_runs_are_keyed_by_device(self):
        """The other half: one dict meant a second start wiped the first run's
        counters, and one `control` key meant pausing either paused both."""
        with open(os.path.join(_ROOT, "engine.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("_attribution_jobs: dict = {}", src)
        # word-bounded: `_attribution_job` is a substring of `run_attribution_job`,
        # which appears in this module's own log strings
        self.assertIsNone(re.search(r"\b_attribution_job\b(?!s)", src),
                          "the single-dict registry is back")
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_attribution_job")
        body = _code_only(fn, src.split("\n"))
        self.assertNotIn("clear", body,
                         "clearing the registry is what let one run wipe another")

    def test_the_status_shape_stays_backward_compatible(self):
        """`historical_probe.html` polls a FLAT object and has one pause/cancel
        pair. Workstream C rewrites that surface; until then the page must keep
        working, so the active run is still returned flat with `runs` alongside.
        """
        with open(os.path.join(_ROOT, "engine.py"), encoding="utf-8") as fh:
            src = fh.read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "api_attribution_status")
        body = _code_only(fn, src.split("\n"))
        self.assertIn("runs", body)
        page = os.path.join(_ROOT, "web", "templates", "historical_probe.html")
        with open(page, encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("d.done_hours", html)
        self.assertIn("d.status", html)


if __name__ == "__main__":
    unittest.main()
