"""
One ratchet for `engine.py`'s raw-connection use, in one place.

Workstream A acceptance 5 retires `check_same_thread=False`. The method calls are
the easy half; the blocker is the raw `_conn` handle, which routes around the
ReadStore/WriteStore split entirely -- porting method calls alone would leave the
flag necessary anyway.

THIS TOOL WAS WRONG FOR THREE SLICES, and the correction is the reason it now
looks like this. It counted `_store._conn` -- the module-global, by name. But
much of `engine.py` is written injection-style, taking a `store` PARAMETER so the
function can be tested against a temporary database:

    def _smb_ev_resplit_core(store, ...):
        conn = store._conn

Neither `store._conn` nor the `conn` alias contains the string `_store`, so 49
direct accesses and 3 alias uses were invisible. The reported population of 19
was one naming convention, not the raw-connection surface -- and A1 through A5
were measured against it.

The scan now:
  * matches `<anything>._conn`, not `_store._conn`;
  * follows aliases -- `conn = store._conn` binds a name, and every later use of
    that name is a raw-connection touch point;
  * is AST throughout, because a text search over source is a search over
    comments and docstrings (seven false findings on this programme).

The ceiling only goes down. It is now the TRUE figure, so it jumps once here and
descends from there.
"""
import ast
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Lower this as sites are retired. Never raise it.
#
#    19  what the old `_store._conn`-only scan reported at the census
#    ..  A1-A5 retired 12 of those 19, measured against that wrong scan
#    57  the TRUE figure after A5, once the scan was fixed
#    52  after A6 -- pricing range scan, EV-carve candidate scan, carve write
#    49  after A7 -- _attribute_missing_ev_split: candidate scan + replace write
#    42  after A8 -- reconcile_dispatch_overlay READS (the writes follow)
#    32  after A9 -- reconcile_dispatch_overlay WRITES; the function is now clean
_CEILING = 32


def _sites(path):
    """Every raw-connection touch point in `path`, as line numbers.

    A direct `x._conn`, plus every LOAD of a name that was assigned one inside
    the same function. The alias binding itself counts once (it is the direct
    access); its uses count separately, because each one is a place the raw
    handle is actually exercised.
    """
    tree = ast.parse(open(path).read())
    fns = [(n.lineno, n.end_lineno, n) for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    hits = {n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and n.attr == "_conn"}

    for lo, hi, fn in fns:
        aliases = {t.id for n in ast.walk(fn)
                   if isinstance(n, ast.Assign)
                   and isinstance(n.value, ast.Attribute)
                   and n.value.attr == "_conn"
                   for t in n.targets if isinstance(t, ast.Name)}
        if not aliases:
            continue
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and n.id in aliases \
                    and isinstance(n.ctx, ast.Load):
                hits.add(n.lineno)
    return sorted(hits)


class RawConnectionRatchet(unittest.TestCase):

    def test_it_only_shrinks(self):
        sites = _sites(os.path.join(_ROOT, "engine.py"))
        self.assertLessEqual(
            len(sites), _CEILING,
            "raw connection use in engine.py grew to %d (ceiling %d): %s"
            % (len(sites), _CEILING, sites))

    def test_the_ceiling_is_not_slack(self):
        """A ceiling nobody lowers stops being a ratchet."""
        sites = _sites(os.path.join(_ROOT, "engine.py"))
        self.assertEqual(
            len(sites), _CEILING,
            "%d raw sites remain but the ceiling says %d -- lower _CEILING"
            % (len(sites), _CEILING))

    def test_the_scan_sees_an_injected_store(self):
        """The bug this tool had. Asserted directly so it cannot return."""
        import tempfile
        src = "def f(store):\n    return store._conn.execute('SELECT 1')\n"
        p = os.path.join(tempfile.mkdtemp(), "m.py")
        open(p, "w").write(src)
        self.assertEqual(len(_sites(p)), 1,
                         "a store PARAMETER is as raw as the module global")

    def test_the_scan_follows_an_alias(self):
        import tempfile
        src = ("def f(store):\n"
               "    conn = store._conn\n"
               "    conn.execute('SELECT 1')\n"
               "    with conn:\n"
               "        conn.execute('SELECT 2')\n")
        p = os.path.join(tempfile.mkdtemp(), "m.py")
        open(p, "w").write(src)
        # line 2 binds (direct access), lines 3, 4 and 5 use it
        self.assertEqual(len(_sites(p)), 4,
                         "an alias hides every later use behind a local name")


if __name__ == "__main__":
    unittest.main()
