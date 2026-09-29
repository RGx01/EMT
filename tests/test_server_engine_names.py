"""
Every server.py function that uses the name `engine` binds it first.

server.py has no module-level `import engine`; each function imports it locally.
Two routes used the bare name without doing so and raised NameError on every
call — found by the first Pi soak (29 Sep 2026):

    /api/engine/pause-state         500 on every request
    /api/engine/meter-reset-detected   caught its own NameError and answered
                                       {"detected": false}, so the meter-
                                       replacement advisory could never show

The suite did not see either because test_server.py installs `engine` for the
routes it exercises. This reads the source instead, so it cannot be satisfied by
a fixture.
"""
import ast
import os
import unittest

_SERVER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "web", "server.py")


def _unbound_engine_uses():
    tree = ast.parse(open(_SERVER, encoding="utf-8").read())
    module = {a.asname or a.name.split(".")[0] for n in tree.body
              if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    module |= {t.id for n in tree.body if isinstance(n, ast.Assign)
               for t in n.targets if isinstance(t, ast.Name)}
    bad = []
    for f in ast.walk(tree):
        if not isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        bound = {a.asname or a.name.split(".")[0] for n in ast.walk(f)
                 if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        bound |= {n.id for n in ast.walk(f) if isinstance(n, ast.Name)
                  and isinstance(n.ctx, ast.Store)}
        bound |= {n for g in ast.walk(f) if isinstance(g, ast.Global) for n in g.names}
        if "engine" in bound or "engine" in module:
            continue
        uses = [n.lineno for n in ast.walk(f) if isinstance(n, ast.Name)
                and n.id == "engine" and isinstance(n.ctx, ast.Load)]
        if uses:
            bad.append((f.name, uses[0]))
    return bad


class EngineIsBoundBeforeUse(unittest.TestCase):

    def test_no_function_uses_engine_unbound(self):
        self.assertEqual(_unbound_engine_uses(), [])


if __name__ == "__main__":
    unittest.main()
