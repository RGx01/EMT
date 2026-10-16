"""
The B design doc claims to carry no hand counts. This checks that.

Every figure in `docs/design/EMT-v5_B_coordinator_design.md` is supposed to come
from a committed tool. Figures in prose drift — this programme's docs have drifted
four times already (85 unreachable that was 88 that was 84; 104/77 write/read that
was 93/102; 88 GET routes that was 87; "4% adoption" that was 0%). Each time the
doc was right when written and wrong a week later.

So the numbers that matter are asserted against the tools that produce them. A
figure that changes legitimately fails here, and the doc gets updated in the same
commit as the change — which is the only way prose stays true.
"""
import ast
import json
import os
import re
import subprocess
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DOC = os.path.join(_ROOT, "docs", "design", "EMT-v5_B_coordinator_design.md")


def _tool(name, *args):
    r = subprocess.run([sys.executable, os.path.join(_ROOT, ".build", name)]
                       + list(args), capture_output=True, text=True, cwd=_ROOT)
    return r.stdout


class TestArchitectureDocHeadline(unittest.TestCase):
    """The §4 unreachable figure, checked against the tool.

    It has drifted five times: 85 that was 88 that was 84 that was 79 that is now
    what the tool says. Every version was right when written. Prose does not
    notice a deletion, so the number is asserted rather than maintained.
    """

    def test_the_unreachable_headline_is_current(self):
        d = json.loads(_tool("check_reachability.py", "--json"))
        doc = os.path.join(_ROOT, "docs", "design", "EMT-v5_architecture.md")
        with open(doc, encoding="utf-8") as fh:
            text = fh.read()
        claim = "%d of %s definitions unreachable" % (
            d["unreachable"], format(d["definitions"], ","))
        self.assertIn(claim, text,
                      "architecture §4 says something other than %r" % claim)


class TestDocFiguresMatchTheTools(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(_DOC, encoding="utf-8") as fh:
            cls.doc = fh.read()

    def test_the_doc_exists_and_names_its_sources(self):
        for tool in ("check_job_guards.py", "write_inventory.py", "write_scope.py"):
            self.assertIn(tool, self.doc,
                          "a figure without a named source is a hand count")

    def test_live_job_count(self):
        d = json.loads(_tool("check_job_guards.py", "--json"))
        self.assertEqual(d["live_jobs"], 10)
        self.assertIn("**10 live engine jobs**", self.doc)

    def test_bulk_handler_count(self):
        inv = json.loads(_tool("write_inventory.py", "web/server.py", "--json"))[0]
        with open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8") as fh:
            src = fh.read()
        routes = set()
        for n in ast.walk(ast.parse(src)):
            for d in getattr(n, "decorator_list", []):
                f = d.func if isinstance(d, ast.Call) else d
                if isinstance(f, ast.Attribute) and f.attr == "route":
                    routes.add(n.name)
        handlers = sorted(set(inv["write"]) & routes)
        # The count is falling as handlers move onto `_engine_write` or become
        # jobs. Asserted against the doc rather than pinned to a number, so the
        # doc has to be updated in the same commit as the port.
        # singular/plural, and don't dump the whole document on failure
        import re as _re
        m = _re.search(r"\*\*(\d+) bulk route handlers?\*\*", self.doc)
        self.assertIsNotNone(m, "the doc no longer states a handler count")
        self.assertEqual(int(m.group(1)), len(handlers),
                         "doc says %s, tool says %d" % (m.group(1), len(handlers)))
        for h in handlers:
            self.assertIn(h, self.doc, "%s is a member and is not listed" % h)

    def test_membership_total(self):
        self.assertIn("member 23", self.doc, "the live tick was omitted once already")
        d = json.loads(_tool("check_job_membership.py", "--json"))
        self.assertEqual(d["members"], 16)
        self.assertIn("16 members", self.doc)
        d = json.loads(_tool("check_job_guards.py", "--json"))
        inv = json.loads(_tool("write_inventory.py", "web/server.py", "--json"))[0]
        with open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8") as fh:
            src = fh.read()
        routes = set()
        for n in ast.walk(ast.parse(src)):
            for dec in getattr(n, "decorator_list", []):
                f = dec.func if isinstance(dec, ast.Call) else dec
                if isinstance(f, ast.Attribute) and f.attr == "route":
                    routes.add(n.name)

    def test_the_import_does_not_take_the_loop_lock(self):
        """§4.0 rests on this: the import and the live tick interleave rather
        than one blocking the other. If the import ever takes
        `_engine_loop_lock`, live capture stalls for the duration of an import
        and the section is wrong."""
        with open(os.path.join(_ROOT, "engine.py"), encoding="utf-8") as fh:
            src = fh.read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_api_import_job")
        body = "\n".join(src.split("\n")[fn.lineno - 1:fn.end_lineno])
        self.assertNotIn("_engine_loop_lock", body)

    def test_the_tick_returns_immediately_when_paused(self):
        """And this: a pause SKIPS capture, it does not queue it. Four callers
        pause — delete, restore, zip upload, VACUUM."""
        with open(os.path.join(_ROOT, "engine.py"), encoding="utf-8") as fh:
            src = fh.read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "_engine_tick")
        first = src.split("\n")[fn.lineno:fn.lineno + 3]
        self.assertTrue(any("_engine_paused" in l for l in first),
                        "the pause check is no longer the first thing the tick does")
        self.assertTrue(any("return" in l for l in first))

    def test_blocks_store_registers_not_samples(self):
        """Why a pause costs boundary precision but not energy."""
        with open(os.path.join(_ROOT, "block_store.py"), encoding="utf-8") as fh:
            schema = fh.read()
        for col in ("imp_read_start", "imp_read_end", "interpolated"):
            self.assertIn(col, schema)

    def test_acceptance_1_exists_and_the_doc_says_so(self):
        """The design's whole bet is that this was written FIRST. If the doc
        claims it and the file is absent, the bet is not being placed."""
        self.assertTrue(os.path.exists(
            os.path.join(_ROOT, ".build", "check_job_membership.py")))
        self.assertIn("check_job_membership.py", self.doc)
        d = json.loads(_tool("check_job_membership.py", "--json"))
        self.assertEqual(d["members"], 16)
        # the doc records the standing; the tool is the source of truth for it
        reg, total = len(d["registered"]), d["members"]
        self.assertIn("%d/%d" % (reg, total), self.doc.replace("**", ""),
                      "the doc's membership standing is stale: tool says %d/%d"
                      % (reg, total))

    def test_the_scope_claim(self):
        """§2.1 — a table-granularity rule is a global lock."""
        out = _tool("write_scope.py")
        m = re.search(r"(\d+) of (\d+) jobs write `blocks`", out)
        self.assertIsNotNone(m)
        self.assertIn("**9 of the 10 live jobs write `blocks`", self.doc)
        self.assertEqual((m.group(1), m.group(2)), ("9", "10"))

    def test_only_two_jobs_declare_a_scope(self):
        """§2.2 — the correction. `max_blocks`/`limit`/`force` are budgets, not
        scopes, and an earlier draft of that section got this backwards."""
        with open(os.path.join(_ROOT, "engine.py"), encoding="utf-8") as fh:
            src = fh.read()
        funcs = {n.name: n for n in ast.walk(ast.parse(src))
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        scoped = []
        for job in json.loads(_tool("check_job_guards.py", "--json"))["jobs"]:
            n = funcs.get(job)
            if n is None:
                continue
            names = {a.arg for a in n.args.posonlyargs + n.args.args + n.args.kwonlyargs}
            if names & {"from_ts", "to_ts", "device_meter_id", "meter_id"}:
                scoped.append(job)
        self.assertEqual(sorted(scoped),
                         ["run_attribution_job", "run_gap_fill_job"])
        self.assertIn("Only two of eleven", self.doc)

    def test_run_exclusive_is_still_the_unused_primitive(self):
        """§6 q5 rests on this."""
        hits = []
        for dirpath, dirnames, files in os.walk(_ROOT):
            dirnames[:] = [d for d in dirnames if d not in
                           (".git", "__pycache__", "tests", "docs", ".build", "scratch")]
            for f in files:
                if f.endswith(".py"):
                    p = os.path.join(dirpath, f)
                    with open(p, encoding="utf-8") as fh:
                        for line in fh.read().split("\n"):
                            if "run_exclusive(" in line and "def " not in line:
                                hits.append(line.strip())
        self.assertEqual(len(hits), 1)
        self.assertIn("lambda: None", hits[0])


if __name__ == "__main__":
    unittest.main()
