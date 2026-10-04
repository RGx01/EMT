"""
B §5 acceptance 2 (4 Oct 2026) — no unregistered write from a request thread.

"The count was 8 and must reach 1 — `api_db_vacuum`, which cannot move without
stalling member 23." It has reached 1. Every other writer in web/server.py runs
somewhere a request thread is not:

  * nested bodies handed to an engine hop (`_engine_write`, `_config_mutation_on_loop`)
    — the inventory lists them as writers in their own right, but they run on the
    engine thread;
  * background workers on their own thread and connection, each a registered member
    (`@job`);
  * `_rebuild_config_period_chain`, a module-level helper called only from inside
    `_mutate` bodies, which run on the engine thread.

`test_b_design_figures.test_bulk_handler_count` asserts the handler COUNT against the
doc's figure, so a new writing handler plus a doc edit would pass it. This pins the
SET, and accounts for every other writer.

A GUARD: the property already holds, so it passes on the parent tree.
"""
import ast
import json
import os
import subprocess
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
TREE = ast.parse(SRC)

# The one request-thread writer allowed, and why (B §4).
ALLOWED_HANDLERS = {"api_db_vacuum"}       # rewrites the whole file; cannot move without stalling the tick
# Module-level helpers that write, called only from engine-thread bodies.
ENGINE_ONLY_HELPERS = {"_rebuild_config_period_chain": "_mutate"}


def _inventory():
    out = subprocess.run([sys.executable, os.path.join(_ROOT, ".build", "write_inventory.py"),
                          "web/server.py", "--json"], capture_output=True, text=True, cwd=_ROOT)
    return json.loads(out.stdout)[0]


def _decorated(attr):
    names = set()
    for n in ast.walk(TREE):
        for d in getattr(n, "decorator_list", []):
            f = d.func if isinstance(d, ast.Call) else d
            if (isinstance(f, ast.Attribute) and f.attr == attr) or \
               (isinstance(f, ast.Name) and f.id == attr):
                names.add(n.name)
    return names


class NoRequestThreadWrites(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.writers = set(_inventory()["write"])
        cls.routes = _decorated("route")
        cls.members = _decorated("job")
        cls.module_level = {n.name for n in TREE.body if isinstance(n, ast.FunctionDef)}

    def test_the_inventory_sees_writers_at_all(self):
        """A tool that silently found nothing would make this pass vacuously."""
        self.assertGreater(len(self.writers), 5)

    def test_the_only_writing_handler_is_the_vacuum(self):
        self.assertEqual(self.writers & self.routes, ALLOWED_HANDLERS)

    def test_every_other_writer_is_accounted_for(self):
        unaccounted = []
        for w in sorted(self.writers - self.routes):
            if w not in self.module_level:
                continue                      # a nested body: handed to an engine hop
            if w in self.members:
                continue                      # a registered background worker
            if w in ENGINE_ONLY_HELPERS:
                continue                      # checked below
            unaccounted.append(w)
        self.assertEqual(unaccounted, [], "module-level writers with no registered home")

    def test_the_engine_only_helpers_are_called_only_from_engine_bodies(self):
        for helper, caller in ENGINE_ONLY_HELPERS.items():
            with self.subTest(helper=helper):
                sites = []
                for fn in ast.walk(TREE):
                    if not isinstance(fn, ast.FunctionDef):
                        continue
                    for c in ast.walk(fn):
                        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) \
                                and c.func.id == helper:
                            sites.append(fn.name)
                # ast.walk visits enclosing functions too; the innermost name is what
                # runs it, so every site must include the engine-thread body.
                self.assertTrue(sites, "%s has no callers — retire it" % helper)
                inner = {s for s in sites if s == caller}
                outer = set(sites) - inner
                for o in outer:
                    body = SRC[SRC.index("def %s(" % o):]
                    self.assertIn("def %s(" % caller, body.split("\n@app.route")[0],
                                  "%s calls %s outside a %s body" % (o, helper, caller))
                self.assertTrue(inner)

    def test_the_hop_bodies_are_really_handed_to_a_hop(self):
        """A nested writer is only safe if it is passed to an engine hop."""
        hops = ("_engine_write(", "_config_mutation_on_loop(")
        for w in sorted(self.writers - self.routes - self.module_level):
            with self.subTest(body=w):
                self.assertTrue(any("%s%s" % (h, w) in SRC.replace(" ", "") or
                                    "%s%s," % (h, w) in SRC.replace(" ", "") for h in hops),
                                "%s writes and is not passed to an engine hop" % w)


if __name__ == "__main__":
    unittest.main()
