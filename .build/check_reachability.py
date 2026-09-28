#!/usr/bin/env python3
"""
Report production functions that nothing can reach, by AST reachability from the
real entry points.

WHY THIS EXISTS
    EMT (v5) is a SUBTRACTIVE project against a codebase that works. The risk is
    not writing a bug — it is removing something load-bearing and not finding out
    for months. `docs/design/EMT-v5_architecture.md` §4a therefore requires an
    over-approximating reachability trace before anything is deleted. This is that
    trace, committed so the requirement is enforceable rather than aspirational.

DELIBERATELY OVER-APPROXIMATING
    A call by bare name OR attribute matches ANY function of that name, anywhere:
    `foo()`, `x.foo()`, a bare reference `foo` passed as a callback, and the string
    "foo" inside a getattr all mark every `def foo` as reached. That over-reports
    what is live — which is the safe direction. Anything this still calls
    unreachable is confidently unreachable.

    The consequence to keep in mind: a name shared with a live function is reported
    live even if this particular definition is dead. **The intent is that it never
    lies about dead, only about alive** — §4a's default is "when the trace is
    ambiguous, it stays".

    That is an intent, not a guarantee, and the first real use broke it: see
    `_invoked_by_syntax` for a live WSGI entry point this reported dead. Anything
    invoked other than by writing its name is invisible here.

    A worked example of it lying about alive, from this very codebase:
    `carbon.ev_carbon` is reachable ONLY through `carbon.carbon_from_reprice`,
    which nothing calls — so it is dead. But `web/server.py:6338` has a JSON
    payload key `"ev_carbon"`, and a string literal that happens to be a valid
    identifier is indistinguishable here from `getattr(mod, "ev_carbon")`. So it
    is reported live. Its sibling `house_carbon` has no such string and is
    correctly reported dead.

    The fix is NOT to stop reading string literals: that would lose genuine
    dispatch-by-name and could report a live function as dead, which is the
    direction that deletes working code. Read a clean report as "no evidence of
    death", never as "proof of life".

WHAT IT CANNOT SEE — read before trusting a clean report
    * Dynamic dispatch through a computed name (`getattr(o, "do_" + verb)`).
    * CONDITIONALLY LIVE code: a function reached only for a configuration this
      trace does not model is still "reachable" here. That distinction cost a day
      during 4.5.13 — `_hybrid_ev_by_block` was misdiagnosed as dead when it is the
      EV rule for accounts WITH a physical meter. Reachability cannot help there.
    * ANY consumer outside production. `tests/` is in SKIP_DIRS by design, so a
      name with no application caller may still have fixtures, builders and whole
      test modules standing on it. "Unreachable from the app" is not "unreferenced",
      and is certainly not "deletable" — measure the deletion footprint separately.
    * Whether a SURFACE still works. Deleting a chart function makes no test fail,
      because the output was HTML nothing asserts on. §4a requires a before/after
      against a real database for anything in the surfaces.

USAGE
    python3 .build/check_reachability.py              # human-readable report
    python3 .build/check_reachability.py --json       # machine-readable
    python3 .build/check_reachability.py --baseline .build/reachability_baseline.json
                                                      # fail if the dead set GREW
"""
import argparse
import ast
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Directories whose contents are never production code. Every dot-directory is
# skipped too (see production_files) — .git, .build, .pytest_cache, and a local
# .venv, whose site-packages would otherwise be scanned as the application.
SKIP_DIRS = {"__pycache__", "tests", "docs", "screenshots", "web/templates", "venv"}

# Files that reference names BY CONSTRUCTION and so can never be evidence of use.
#
# `store_types.py` is generated: it binds every read method of BlockStore as a
# class attribute (`get_meta = _BS.get_meta`). Scanning it marked 25 genuinely
# dead names as reachable — including `get_reads_for_block` and
# `get_reads_for_range`, two of the five `reads`-table methods §4 documents as
# unreachable. A generated mirror of an API is not a caller of it, any more than
# `tests/` is, and leaving it in would have quietly hollowed out the baseline.
SKIP_FILES = {"store_types.py"}

# The add-on's one true entry point. Everything the application does starts here.
MAIN_ENTRY = "main.py"

# Standalone diagnostic scripts, each run by hand with `python3 <file>`. They are
# shipped and supported, so their own top-level code is an entry point — but they
# are listed separately so a reader can tell "live because the app uses it" from
# "live only because a probe uses it".
PROBE_PREFIXES = ("probe_", "octopus_", "emt_")
PROBE_SUFFIXES = ("_probe.py", "_tracker.py")


def _is_probe(rel):
    b = os.path.basename(rel)
    return (b.startswith(PROBE_PREFIXES) or b.endswith(PROBE_SUFFIXES)) and "/" not in rel


def production_files():
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel_dir = os.path.relpath(dirpath, ROOT)
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".")
                       and d not in SKIP_DIRS
                       and os.path.join(rel_dir, d).replace(os.sep, "/") not in SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            # No prefix strip needed: rel_dir is "" at the root. (This was
            # `.lstrip("./")`, which strips a character SET, not a prefix — it
            # turned `.venv/x.py` into `venv/x.py`, a path that does not exist.)
            rel = os.path.join(rel_dir, fn).replace(os.sep, "/")
            if rel in SKIP_FILES:
                continue
            out.append(rel)
    return sorted(out)


class Defs(ast.NodeVisitor):
    """Every function defined in a module, with the line span so a report can say
    how much code a name represents."""

    def __init__(self, rel):
        self.rel = rel
        self.found = []          # (name, lineno, endline, is_private)

    def _add(self, node):
        end = getattr(node, "end_lineno", node.lineno)
        self.found.append((node.name, node.lineno, end, node.name.startswith("_")))
        self.generic_visit(node)

    visit_FunctionDef = _add
    visit_AsyncFunctionDef = _add


def names_used(node):
    """Every name this subtree could possibly be invoking. Over-approximating by
    design: calls, bare references, attribute access, and string literals (which
    covers getattr and any dispatch table keyed by name)."""
    used = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            used.add(n.id)
        elif isinstance(n, ast.Attribute):
            used.add(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            if n.value.isidentifier():
                used.add(n.value)
    return used


def _invoked_by_syntax(name):
    """True for names Python calls through SYNTAX rather than through a name.

    `__call__` runs when an instance is called, `__aenter__`/`__aexit__` on
    `async with`, `__len__` on `len()`, `__eq__` on `==`. None of those appear
    in the AST as the method name, so a pure name-trace sees no caller and
    reports them dead — which is the direction that deletes working code.

    Found the hard way: this tool's first real use reported
    `web/server.py`'s `IngressMiddleware.__call__` unreachable. It is the WSGI
    entry point — every single request goes through it. Excluding the whole
    dunder namespace over-approximates toward life, which is the safe side.
    """
    return name.startswith("__") and name.endswith("__")


def analyse():
    files = production_files()
    defs = {}                # name -> [(rel, lineno, endline)]
    body_uses = {}           # (rel, name) -> set(names)
    module_uses = {}         # rel -> set(names) used at module level / by decorators
    decorated = set()        # names carrying any decorator (routes, etc.)
    parse_errors = []

    for rel in files:
        path = os.path.join(ROOT, rel)
        try:
            tree = ast.parse(open(path, encoding="utf-8").read(), filename=rel)
        except SyntaxError as e:
            parse_errors.append((rel, str(e)))
            continue

        d = Defs(rel)
        d.visit(tree)
        for name, lineno, end, _priv in d.found:
            defs.setdefault(name, []).append((rel, lineno, end))

        # Per-function bodies.
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body_uses[(rel, n.name)] = names_used(n)
                if n.decorator_list:
                    decorated.add(n.name)
                    for dec in n.decorator_list:
                        module_uses.setdefault(rel, set()).update(names_used(dec))

        # Module level: anything not inside a function runs on import.
        mod = set()
        for stmt in tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if isinstance(stmt, ast.ClassDef):
                    mod |= names_used(stmt)      # class bodies execute on import
                continue
            mod |= names_used(stmt)
        module_uses.setdefault(rel, set()).update(mod)

    # ── seed ────────────────────────────────────────────────────────────────
    # Module-level code of the entry point and everything it can import runs; a
    # decorated function (a Flask route) is called by the framework, not by name.
    reachable = set(decorated)
    frontier = list(decorated)
    for rel, used in module_uses.items():
        for nm in used:
            if nm in defs and nm not in reachable:
                reachable.add(nm)
                frontier.append(nm)

    # ── close over calls ────────────────────────────────────────────────────
    while frontier:
        nm = frontier.pop()
        for (rel, dname), used in body_uses.items():
            if dname != nm:
                continue
            for cand in used:
                if cand in defs and cand not in reachable:
                    reachable.add(cand)
                    frontier.append(cand)

    dead = {n: defs[n] for n in defs
            if n not in reachable and not _invoked_by_syntax(n)}
    return files, defs, dead, parse_errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--baseline")
    ap.add_argument("--update-baseline")
    args = ap.parse_args()

    files, defs, dead, errs = analyse()
    by_file = {}
    for name, sites in dead.items():
        for rel, lineno, end in sites:
            by_file.setdefault(rel, []).append(
                {"name": name, "line": lineno, "lines": end - lineno + 1})

    payload = {
        "files_scanned": len(files),
        "definitions": sum(len(v) for v in defs.values()),
        "unreachable": sum(len(v) for v in dead.values()),
        "by_file": {k: sorted(v, key=lambda x: x["line"]) for k, v in sorted(by_file.items())},
        "parse_errors": errs,
    }

    if args.update_baseline:
        names = sorted({d["name"] for v in payload["by_file"].values() for d in v})
        with open(args.update_baseline, "w", encoding="utf-8") as fh:
            json.dump({"unreachable": names}, fh, indent=2)
            fh.write("\n")
        print(f"baseline written: {len(names)} unreachable names")
        return 0

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"scanned {payload['files_scanned']} production files, "
              f"{payload['definitions']} definitions")
        print(f"UNREACHABLE: {payload['unreachable']}\n")
        for rel, items in payload["by_file"].items():
            total = sum(i["lines"] for i in items)
            print(f"  {rel}  —  {len(items)} function(s), {total} lines")
            for i in items:
                print(f"      {i['line']:>6}  {i['name']}  ({i['lines']} lines)")
        if errs:
            print("\nPARSE ERRORS (not analysed):")
            for rel, e in errs:
                print(f"  {rel}: {e}")

    if args.baseline:
        with open(args.baseline, encoding="utf-8") as fh:
            known = set(json.load(fh)["unreachable"])
        now = {d["name"] for v in payload["by_file"].values() for d in v}
        new = sorted(now - known)
        if new:
            print(f"\nFAIL: {len(new)} newly unreachable name(s) since the baseline:")
            for n in new:
                print(f"  {n}")
            print("\nEither wire them up or delete them under §4a — do not grow the dead set.")
            return 1
        gone = sorted(known - now)
        if gone:
            print(f"\n{len(gone)} name(s) removed since the baseline (good): "
                  + ", ".join(gone[:10]) + ("…" if len(gone) > 10 else ""))
        print("\nOK: the unreachable set has not grown.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
