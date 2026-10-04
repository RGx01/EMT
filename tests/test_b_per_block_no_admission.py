"""
B §5 acceptance 5 (4 Oct 2026) — admission never happens per block.

"The call tree of the block write path stays free of `await`." §1's per-block
atomicity is what keeps the uncoordinated jobs safe: a block's read-modify-write runs
with no `await` in it, so the loop cannot interleave inside one block, and jobs
interleave only BETWEEN blocks. §3.4: a coordinator that put an admission check inside
a block write would remove that — admission belongs at job start and batch boundaries.

`test_write_scope.py::test_the_block_write_path_never_awaits` held this for one
function, `_write_device_into_block`, following plain calls for `await`. This holds it
for every per-block writer the jobs use, and for coordinator calls as well as `await`
(a synchronous admission() per block does not break atomicity, but it is the per-block
admission §3.4 forbids, and it would be the first step to an async one).

GUARDS: the property already holds, so these pass on the parent tree. Checked by
mutation (4 Oct 2026): an `_coord.admission(...)` call, or an `await`, injected into
`_apply_pass2` — reached from the device write — fails them.
"""
import ast
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The per-block write path: each takes one block and writes it.
BLOCK_WRITERS = [
    "_write_device_into_block",      # attribution
    "_apply_pass2",                  # every remainder re-derivation
    "apply_measured_to_block",       # measured drain / settlement
    "_reprice_history_blocks",       # the re-price sweep's per-block body
    "_persist_block_segments",
    "_persist_block_carbon",
    "_attribute_block_carbon",       # carbon recovery / gap fill
    "append_block",
    "append_block_replace",
]
COORDINATOR = ("_coord",)            # engine's `import job_coordinator as _coord`


class _Tree:
    def __init__(self, src):
        self.funcs = {n.name: n for n in ast.walk(ast.parse(src))
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def walk(self, name, seen=None, depth=0):
        """Every function reached from `name` by plain calls (module functions)."""
        seen = seen if seen is not None else set()
        if name in seen or name not in self.funcs or depth > 6:
            return seen
        seen.add(name)
        for n in ast.walk(self.funcs[name]):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                self.walk(n.func.id, seen, depth + 1)
        return seen

    def awaits(self, name):
        node = self.funcs[name]
        return isinstance(node, ast.AsyncFunctionDef) or any(
            isinstance(n, (ast.Await, ast.AsyncFor, ast.AsyncWith)) for n in ast.walk(node))

    def coordinator_calls(self, name):
        return sorted({"%s.%s" % (n.func.value.id, n.func.attr)
                       for n in ast.walk(self.funcs[name])
                       if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                       and isinstance(n.func.value, ast.Name) and n.func.value.id in COORDINATOR})


class TheBlockWritePath(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.t = _Tree(open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read())

    def test_every_writer_listed_still_exists(self):
        """A rename must not quietly shrink what is checked."""
        self.assertEqual([w for w in BLOCK_WRITERS if w not in self.t.funcs], [])

    def test_the_walk_follows_calls(self):
        """The device write re-derives the parent's remainder through _apply_pass2;
        a walk that did not reach it would check nothing."""
        self.assertIn("_apply_pass2", self.t.walk("_write_device_into_block"))

    def test_no_await_anywhere_in_the_tree(self):
        for w in BLOCK_WRITERS:
            with self.subTest(writer=w):
                bad = sorted(f for f in self.t.walk(w) if self.t.awaits(f))
                self.assertEqual(bad, [], "%s reaches an await through %s" % (w, bad))

    def test_no_coordinator_call_anywhere_in_the_tree(self):
        for w in BLOCK_WRITERS:
            with self.subTest(writer=w):
                bad = {f: self.t.coordinator_calls(f) for f in self.t.walk(w)
                       if self.t.coordinator_calls(f)}
                self.assertEqual(bad, {}, "%s asks the coordinator per block" % w)


class TheCheckCatchesItsTarget(unittest.TestCase):
    """The checks above, run against a mutated copy, must fail."""

    def _mutated(self, line):
        src = open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read()
        anchor = "def _apply_pass2("
        i = src.index(anchor)
        j = src.index(":\n", src.index(")", i)) + 2       # end of the signature
        return _Tree(src[:j] + "    " + line + "\n" + src[j:])

    def test_an_admission_call_in_a_block_write_is_caught(self):
        t = self._mutated('_coord.admission("reprice_sweep")')
        self.assertIn("_coord.admission", t.coordinator_calls("_apply_pass2"))
        self.assertIn("_apply_pass2", t.walk("_write_device_into_block"))

    def test_an_await_in_a_block_write_is_caught(self):
        src = open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read()
        src = src.replace("def _apply_pass2(", "async def _apply_pass2(", 1)
        t = _Tree(src)
        self.assertTrue(t.awaits("_apply_pass2"))


if __name__ == "__main__":
    unittest.main()
