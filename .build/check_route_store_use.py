#!/usr/bin/env python3
"""
Which web routes reach the WRITER connection — the adoption guard for workstream A.

WHY THIS EXISTS
    `docs/history/design/EMT-v5_A_storage_design.md` §5 acceptance 3 calls a structural
    test against writer use by route handlers "the single most important item on
    this list", because the read companion already exists in v4 and sits at 4%
    adoption. A safe path that is opt-in stays opt-in. Nothing about the wrong
    choice is punished at the moment it is made: `_get_store()` is shorter, older,
    autocompletes first, and works perfectly in development.

    So this is the thing that turns "please use the read store" into a build
    failure. It runs against v4's code today, which means it measures the size of
    the port before the port starts, and then measures progress through it.

TRANSITIVE BY DESIGN
    Counting only `_get_store()` written inside the handler under-reports: a route
    that calls a module-level helper which reaches for the writer is just as much
    on the writer. The call graph is followed to a fixed point. (The doc's hand
    count found 41 direct; see the report for what transitivity adds.)

WHAT IT CANNOT SEE
    * A handler reaching the writer through a method on an object it was handed.
    * `getattr`-style dispatch to a store accessor.
    Both would UNDER-report, so a clean report here is weaker than a dirty one is
    strong. This is a ratchet against regression, not a proof of adoption.

USAGE
    python3 .build/check_route_store_use.py
    python3 .build/check_route_store_use.py --json
    python3 .build/check_route_store_use.py --baseline .build/route_store_baseline.json
    python3 .build/check_route_store_use.py --update-baseline <path>
"""
import argparse
import ast
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, "web", "server.py")

WRITER_ACCESSORS = {"_get_store"}
# `store_access.store()` is qualified, so it is matched on the pair rather than on
# the bare name — "store" is a local variable at dozens of sites and matching it
# by name alone would report the whole file as adopted.
READER_ACCESSORS = {"_get_read_store", "store_access.store"}

# Methods that mutate, from the classifier. A route calling one of these is
# writing on a Flask worker thread, through the engine's connection.
BASELINE = os.path.join(ROOT, ".build", "store_split_baseline.json")


def called_names(fn):
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
                if isinstance(f.value, ast.Name):
                    out.add("%s.%s" % (f.value.id, f.attr))
        elif isinstance(n, ast.Name):
            out.add(n.id)          # a bare reference passed as a callback
    return out


def analyse():
    tree = ast.parse(open(TARGET, encoding="utf-8").read(), filename="web/server.py")
    funcs = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.setdefault(n.name, n)

    calls = {name: called_names(fn) for name, fn in funcs.items()}

    # The accessors are TERMINAL. `_get_read_store()` ends with
    # `return _get_store()` — a deliberate fallback for an in-memory or
    # not-yet-created database ("correctness over the concurrency win"). Let that
    # propagate and every consumer of the read companion is reported as reaching
    # the writer, which blamed the one route that had actually adopted it.
    #
    # Worth recording rather than just working around: **adopting the companion
    # does not guarantee isolation in v4**, because it can hand back the shared
    # handle. v5's type split must have no such fallback, or `ReadStore` is
    # theatre — see A §3.2.
    for accessor in WRITER_ACCESSORS | READER_ACCESSORS:
        calls[accessor] = set()

    # Fixed point: does this function reach an accessor, directly or through
    # anything it calls?
    def reaches(targets):
        hit = {n for n, c in calls.items() if c & targets}
        changed = True
        while changed:
            changed = False
            for name, c in calls.items():
                if name in hit:
                    continue
                if c & hit:
                    hit.add(name)
                    changed = True
        return hit

    writer_reach = reaches(WRITER_ACCESSORS)
    reader_reach = reaches(READER_ACCESSORS)

    routes = []
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for d in n.decorator_list:
            call = d if isinstance(d, ast.Call) else None
            f = call.func if call else d
            if not (isinstance(f, ast.Attribute) and f.attr == "route"):
                continue
            path = "?"
            if call and call.args and isinstance(call.args[0], ast.Constant):
                path = call.args[0].value
            methods = ["GET"]
            for kw in (call.keywords if call else []):
                if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                    methods = [e.value for e in kw.value.elts
                               if isinstance(e, ast.Constant)]
            direct = calls.get(n.name, set())
            used = _store_methods_reached(n.name, calls, funcs)
            routes.append({
                "handler": n.name,
                "path": path,
                "methods": sorted(methods),
                "get": "GET" in methods,
                "line": n.lineno,
                "writer": n.name in writer_reach,
                "reader": n.name in reader_reach,
                "writer_direct": bool(direct & WRITER_ACCESSORS),
                "write_methods": sorted(used & _write_names()),
            })
    return sorted(routes, key=lambda r: r["line"])


_WRITE_CACHE = []


def _write_names():
    if not _WRITE_CACHE:
        with open(BASELINE, encoding="utf-8") as fh:
            _WRITE_CACHE.append({m for m in json.load(fh)["write"]
                                 if not m.startswith("__")})
    return _WRITE_CACHE[0]


ENGINE_HOPS = {"_engine_write", "_run_on_engine_loop"}


def _attrs(fn):
    """Attribute names this function touches, EXCLUDING anything inside an
    engine-hop call.

    `_engine_write(lambda st: st.prune_stale_deleted_ranges())` still mentions a
    write method, but the write happens on the engine thread — which is the fix,
    not the bug. Counting it would leave the alarm permanently on and therefore
    ignored. What the check is for is a mutation performed through the REQUEST
    thread's own store.
    """
    skip = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                and n.func.id in ENGINE_HOPS:
            for arg in list(n.args) + [k.value for k in n.keywords]:
                for sub in ast.walk(arg):
                    skip.add(id(sub))
    return {x.attr for x in ast.walk(fn)
            if isinstance(x, ast.Attribute) and id(x) not in skip}


def _store_methods_reached(name, calls, funcs, seen=None):
    """Every attribute name this handler touches, following server-local helpers.
    Over-approximating — `.get_meta` on anything counts — which is the right
    direction: a route that turns out NOT to write is cheap to re-check, a route
    that writes unnoticed is the bug."""
    seen = seen if seen is not None else set()
    if name in seen or name not in funcs:
        return set()
    seen.add(name)
    out = _attrs(funcs[name])
    for c in calls.get(name, ()):
        if c in funcs:
            out |= _store_methods_reached(c, calls, funcs, seen)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--baseline")
    ap.add_argument("--update-baseline")
    a = ap.parse_args()

    routes = analyse()
    gets = [r for r in routes if r["get"]]
    on_writer = [r for r in gets if r["writer"]]
    direct = [r for r in on_writer if r["writer_direct"]]
    companion = [r for r in gets if r["reader"] and not r["writer"]]
    no_store = [r for r in gets if not r["writer"] and not r["reader"]]

    writing_gets = [r for r in gets if r["write_methods"]]
    payload = {
        "routes": len(routes),
        "get_routes_that_write": {r["handler"]: {"path": r["path"],
                                                 "methods": r["methods"],
                                                 "writes": r["write_methods"]}
                                  for r in writing_gets},
        "get_capable": len(gets),
        "get_on_writer": sorted(r["handler"] for r in on_writer),
        "get_on_companion": sorted(r["handler"] for r in companion),
        "get_no_store": len(no_store),
        "direct_vs_transitive": {"direct": len(direct),
                                 "only_via_a_helper": len(on_writer) - len(direct)},
    }

    if a.update_baseline:
        with open(a.update_baseline, "w", encoding="utf-8") as fh:
            json.dump({"get_on_writer": payload["get_on_writer"]}, fh, indent=2)
            fh.write("\n")
        print("baseline written: %d GET routes on the writer" % len(payload["get_on_writer"]))
        return 0

    if a.json:
        print(json.dumps(payload, indent=2))
    else:
        print("web/server.py — %d routes, %d GET-capable\n" % (payload["routes"],
                                                               payload["get_capable"]))
        print("  on the WRITER      : %d   (%d written in the handler, %d only via a helper)"
              % (len(on_writer), len(direct), len(on_writer) - len(direct)))
        print("  on the companion   : %d" % len(companion))
        print("  no store access    : %d" % len(no_store))
        n = len(on_writer) + len(companion)
        if n:
            print("\n  adoption: %d/%d = %.0f%%" % (len(companion), n,
                                                    100.0 * len(companion) / n))
        if writing_gets:
            print("\n  !! GET routes that WRITE, on a Flask worker thread:")
            for r in writing_gets:
                mark = "  GET-ONLY" if r["methods"] == ["GET"] else ""
                print("       %-36s %-32s %s%s" % (r["handler"], r["path"],
                                                   ", ".join(r["write_methods"]), mark))
        print("\nGET routes reading on the writer connection:")
        for r in on_writer:
            print("    %-44s %s%s" % (r["handler"], r["path"],
                                      "" if r["writer_direct"] else "   (via a helper)"))

    if a.baseline:
        with open(a.baseline, encoding="utf-8") as fh:
            known = set(json.load(fh)["get_on_writer"])
        now = set(payload["get_on_writer"])
        new = sorted(now - known)
        if new:
            print("\nFAIL: %d GET route(s) newly reading on the writer:" % len(new))
            for h in new:
                print("  " + h)
            print("\nUse the read store. A §1: a safe path that is opt-in stays at 4%.")
            return 1
        gone = sorted(known - now)
        if gone:
            print("\n%d route(s) moved OFF the writer since the baseline (good): %s"
                  % (len(gone), ", ".join(gone[:8]) + ("…" if len(gone) > 8 else "")))
        print("\nOK: writer use by GET routes has not grown.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
