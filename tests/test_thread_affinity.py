"""
`check_same_thread` stays retired — workstream A, acceptance 5.

THE POINT OF THE WHOLE WORKSTREAM. v4 opened every SQLite connection with
`check_same_thread=False`, which turns off the one guard the driver offers
against a connection being used from two threads. It had to: connections
travelled. `engine.py` held 57 raw `_conn` sites (the true figure, once the scan
learned to see stores passed as parameters and aliases like `conn = store._conn`),
and three background workers in `web/server.py` wrote through the shared web
store on threads they had not opened it on.

With the flag off, the failure mode is the one `engine.py` names:

    "a DB write from the executor thread races the loop-thread writers on that
     one connection and corrupts it: 'cannot commit - no transaction is active' /
     'error return without exception set'"

— silent, intermittent, and discovered later. With it on, a cross-thread use is
an immediate `ProgrammingError` at the line that does it.

Three properties now hold, and each is asserted below because losing any one
would quietly re-create the need for the flag:

  1. `BlockStore` does not pass `check_same_thread`.
  2. `engine.py` holds no raw connection at all.
  3. A connection really is thread-affine — demonstrated, not assumed.
"""
import ast
import os
import sqlite3
import sys
import tempfile
import threading
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from block_store import BlockStore, open_block_store       # noqa: E402


def _connect_kwargs(path, func_name="__init__", cls="BlockStore"):
    """The keywords passed to sqlite3.connect inside a named method. AST, because
    a text search would match the explanatory comment that sits right above it."""
    tree = ast.parse(open(os.path.join(_ROOT, path), encoding="utf-8").read())
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef) and n.name == cls:
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name == func_name:
                    for c in ast.walk(m):
                        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) \
                                and c.func.attr == "connect":
                            return {kw.arg for kw in c.keywords}
    return None


class TheFlagIsGone(unittest.TestCase):

    def test_block_store_does_not_pass_it(self):
        kw = _connect_kwargs("block_store.py")
        self.assertIsNotNone(kw, "BlockStore.__init__ no longer calls sqlite3.connect")
        self.assertNotIn("check_same_thread", kw,
                         "check_same_thread is back — see A acceptance 5")

    def test_read_store_never_passed_it_either(self):
        """`ReadStore` has always opened per-thread, so it never needed the flag.
        Asserted so a future generator change cannot add one 'for consistency'."""
        kw = _connect_kwargs("store_types.py", cls="ReadStore")
        if kw is not None:
            self.assertNotIn("check_same_thread", kw)

    def test_no_module_passes_it_anywhere(self):
        """Including the places that legitimately open their own connection —
        the vacuum, the backup writer, the workers."""
        import glob
        offenders = []
        for f in glob.glob(os.path.join(_ROOT, "*.py")) + \
                 glob.glob(os.path.join(_ROOT, "web", "*.py")):
            try:
                tree = ast.parse(open(f, encoding="utf-8").read())
            except SyntaxError:
                continue
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                        and n.func.attr == "connect":
                    if any(kw.arg == "check_same_thread" for kw in n.keywords):
                        offenders.append("%s:%d" % (os.path.basename(f), n.lineno))
        self.assertEqual(offenders, [], "check_same_thread reappeared at %s" % offenders)


class EngineHoldsNoConnection(unittest.TestCase):
    """The precondition. If engine.py starts holding a raw connection again, the
    flag becomes necessary again — and the failure would appear somewhere else
    entirely, months later."""

    def test_zero_raw_sites(self):
        tree = ast.parse(open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read())
        raw = [n.lineno for n in ast.walk(tree)
               if isinstance(n, ast.Attribute) and n.attr == "_conn"]
        self.assertEqual(raw, [], "engine.py holds a raw connection at %s" % raw)


class AffinityIsRealNow(unittest.TestCase):
    """Demonstrated rather than asserted — the flag's absence should have a
    visible consequence, or it is only a comment."""

    def test_a_cross_thread_use_raises(self):
        db = os.path.join(tempfile.mkdtemp(), "blocks.db")
        st = open_block_store(db)
        caught = []

        def other_thread():
            try:
                st.count_blocks()
            except Exception as e:
                caught.append(type(e).__name__)

        t = threading.Thread(target=other_thread)
        t.start()
        t.join()
        st.close()
        self.assertEqual(caught, ["ProgrammingError"],
                         "a cross-thread use should now fail loudly, not silently")

    def test_a_worker_opening_its_own_is_fine(self):
        """What the three background workers do via `server._own_store()`."""
        db = os.path.join(tempfile.mkdtemp(), "blocks.db")
        open_block_store(db).close()
        ok = []

        def worker():
            own = open_block_store(db)
            try:
                own.count_blocks()
                ok.append(True)
            finally:
                own.close()

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        self.assertEqual(ok, [True])

    def test_a_per_thread_read_store_is_fine(self):
        """What `store_access.store()` gives every request thread."""
        db = os.path.join(tempfile.mkdtemp(), "blocks.db")
        open_block_store(db).close()
        ok = []

        def reader():
            rs = BlockStore(db, read_only=True)
            try:
                rs.count_blocks()
                ok.append(True)
            finally:
                rs.close()

        ts = [threading.Thread(target=reader) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(ok, [True] * 4)


if __name__ == "__main__":
    unittest.main()
