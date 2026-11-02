"""
Workstream B's admission rule, constrained by measurement.

The finding: a table-granularity "run together if write scopes are disjoint"
rule degenerates into a global lock, because nearly every job writes `blocks`.
The rule has to be range- or meter-scoped instead.

The second finding is the one to guard hardest. Per-block writes are fully
synchronous, so on the single engine loop one block's read-modify-write is
atomic and concurrent jobs interleave BETWEEN blocks. That is what makes today's
uncoordinated jobs converge rather than corrupt — and it is a property that an
`await` added inside a block write would silently remove.
"""
import ast
import json
import os
import subprocess
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOL = os.path.join(_ROOT, ".build", "write_scope.py")


class TestScopeOverlap(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        r = subprocess.run([sys.executable, _TOOL, "--json"],
                           capture_output=True, text=True, cwd=_ROOT)
        cls.d = json.loads(r.stdout)

    def test_nearly_every_job_writes_blocks(self):
        """If this ever stops being true the admission rule can get simpler —
        which would be worth noticing."""
        jobs = self.d["jobs"]
        writing_blocks = [j for j, t in jobs.items() if "blocks" in t]
        self.assertGreaterEqual(len(writing_blocks), len(jobs) - 1,
                                "table-granularity coordination may now be viable")

    def test_the_scope_map_actually_resolved_something(self):
        """A regex that silently matches nothing would make every job look
        conflict-free, which is the dangerous direction for an admission rule."""
        self.assertGreater(self.d["methods_mapped"], 50)


class TestPerBlockWritesAreAtomic(unittest.TestCase):
    """The property the current safety rests on."""

    @classmethod
    def setUpClass(cls):
        src = open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read()
        cls.funcs = {n.name: n for n in ast.walk(ast.parse(src))
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def _has_await(self, name, seen=None, depth=0):
        seen = seen if seen is not None else set()
        if name in seen or name not in self.funcs or depth > 5:
            return False
        seen.add(name)
        node = self.funcs[name]
        if isinstance(node, ast.AsyncFunctionDef):
            return True
        for n in ast.walk(node):
            if isinstance(n, (ast.Await, ast.AsyncFor, ast.AsyncWith)):
                return True
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                if self._has_await(n.func.id, seen, depth + 1):
                    return True
        return False

    def test_the_block_write_path_never_awaits(self):
        """No `await` anywhere in its call tree, so the loop cannot interleave
        inside one block's read-modify-write. Two attributions for different
        devices both rewrite the parent meter's remainder — they converge only
        because each re-derives from the block's current state, uninterrupted.

        If this fails, that reasoning is gone and the uncoordinated jobs stop
        being merely uncoordinated.
        """
        self.assertFalse(self._has_await("_write_device_into_block"))

    def test_attribution_does_touch_the_parent_row(self):
        """Recorded because the opposite was asserted earlier in this work:
        "different meter_ids, disjoint rows, no overlap". Not so — the device
        write re-derives the parent's remainder, so two device runs DO share a
        row. They are safe for the atomicity reason above, not for disjointness.
        """
        node = self.funcs["_write_device_into_block"]
        src = open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read()
        body = "\n".join(src.split("\n")[node.lineno - 1:node.end_lineno])
        self.assertIn("parent_meter_id", body)
        self.assertIn("_apply_pass2", body)


if __name__ == "__main__":
    unittest.main()
