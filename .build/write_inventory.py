#!/usr/bin/env python3
"""
Which functions write to the database — workstream B's scope, measured.

WHY THIS, RATHER THAN PORTING engine.py FIRST
    A §6 says: "port modules onto [the new types] one at a time. A module that
    will not compile against `ReadStore` is telling you it writes — which is
    exactly the inventory workstream B needs, produced as a side effect."

    The side effect is the valuable part; the port that produces it is not. The
    engine runs ON the engine thread, so `writer()` is legal at all 372 of its
    store accesses — swapping `_store` for `writer()` there is mechanical, risky
    in bulk, and separates nothing. The inventory can be had directly, and then
    the swap is just the cleanup that retires the last `check_same_thread=False`.

WHAT COUNTS AS A WRITE
    * calling a BlockStore method the classifier calls a writer
      (.build/store_split_baseline.json);
    * raw SQL through `_conn.execute(...)` whose verb mutates — the engine does
      this 63 times, and those bypass the method split entirely, so a tool that
      only looked at method names would under-report;
    * calling anything else in the module that does either, to a fixed point.

    Over-approximating on names, like the reachability tool: `.set_meta` on
    anything counts. For an inventory that is the right direction — B would
    rather coordinate one function that turns out not to write than miss one
    that does.

USAGE
    python3 .build/write_inventory.py [module.py ...]     # default: engine.py
    python3 .build/write_inventory.py --json
"""
import argparse
import ast
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = os.path.join(ROOT, ".build", "store_split_baseline.json")
WRITE_VERBS = {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP",
               "ALTER", "TRUNCATE", "VACUUM", "REINDEX"}


def _sql_text(node):
    out = []

    def walk(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
            return
        for c in ast.iter_child_nodes(n):
            walk(c)

    walk(node)
    return " ".join(out)


def _mutating_sql(node, local_sql):
    """True if this execute() call carries a mutating verb, resolving a local."""
    text = _sql_text(node)
    for name in (x.id for x in ast.walk(node) if isinstance(x, ast.Name)):
        if name in local_sql:
            text += " " + local_sql[name]
    for tok in text.replace("(", " ").replace(")", " ").replace(";", " ").split():
        w = tok.strip().upper()
        if w in WRITE_VERBS:
            return True
        if w in ("SELECT", "PRAGMA", "WITH"):
            return False
    return False


# A write performed through one of these happens on the ENGINE thread, which is
# the fix rather than the bug. Counting it would overstate B's scope with work
# already done — the three GET routes fixed during the surfaces port showed up as
# writers until this exclusion existed.
ENGINE_HOPS = {"_engine_write", "_run_on_engine_loop", "run_coroutine_threadsafe",
               # `_config_mutation_on_loop` is `_engine_write` plus a chart regen —
               # v4 arrived at the same fix for the same race in #361, under a
               # different name. It delegates to the primitive now; it is listed
               # here because the routes that call it are already off the request
               # thread and should not be counted against B's remaining scope.
               "_config_mutation_on_loop"}


def _hopped(fn):
    """ids of nodes that sit inside an engine-hop call.

    Two shapes, and the second is easy to miss:

        _engine_write(lambda st: st.set_meta(...))      # inline — the argument
        _config_mutation_on_loop(_mutate)               # by NAME

    In the second the mutation lives in a nested `def _mutate(store):` and the
    call argument is just a Name, so excluding the argument subtree excludes
    nothing. The handler then still reads as writing on the request thread,
    which is how the three config endpoints stayed in B's scope after they had
    already been fixed. Resolve the name to its nested definition and skip that
    body too.
    """
    skip = set()
    nested = {n.name: n for n in ast.walk(fn)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not fn}
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
        if name not in ENGINE_HOPS:
            continue
        for arg in list(n.args) + [k.value for k in n.keywords]:
            for sub in ast.walk(arg):
                skip.add(id(sub))
            if isinstance(arg, ast.Name) and arg.id in nested:
                for sub in ast.walk(nested[arg.id]):
                    skip.add(id(sub))
    return skip


def _locals(fn):
    out = {}
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign):
            names = [t.id for t in n.targets if isinstance(t, ast.Name)]
        elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name):
            names = [n.target.id]
        else:
            continue
        txt = _sql_text(n.value)
        for nm in names:
            if nm not in out and txt.strip():
                out[nm] = txt
    return out


def analyse(rel):
    with open(os.path.join(ROOT, BASELINE), encoding="utf-8") as fh:
        writers = {m for m in json.load(fh)["write"] if not m.startswith("__")}

    tree = ast.parse(open(os.path.join(ROOT, rel), encoding="utf-8").read(),
                     filename=rel)
    funcs = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.setdefault(n.name, n)

    direct, calls, raw = {}, {}, set()
    for name, fn in funcs.items():
        loc = _locals(fn)
        skip = _hopped(fn)
        hit = False
        for n in ast.walk(fn):
            if id(n) in skip:
                continue
            if isinstance(n, ast.Attribute) and n.attr in writers:
                hit = True
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr in ("execute", "executemany", "executescript") \
                    and n.args and _mutating_sql(n.args[0], loc):
                hit = True
                raw.add(name)
        direct[name] = hit
        # Hops are BARRIERS in the propagation, not just in the direct scan.
        # `_config_mutation_on_loop` genuinely writes — on the engine thread —
        # so every caller inherited "writes" and stayed in B's scope after being
        # fixed. What this inventory is for is writes performed on the REQUEST
        # thread, and a hop is precisely where that stops being true.
        calls[name] = ({x.func.id for x in ast.walk(fn)
                        if isinstance(x, ast.Call) and isinstance(x.func, ast.Name)
                        and id(x) not in skip}
                       | {x.func.attr for x in ast.walk(fn)
                          if isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute)
                          and id(x) not in skip}) - ENGINE_HOPS

    writes = {n for n, v in direct.items() if v}
    changed = True
    while changed:
        changed = False
        for name in funcs:
            if name in writes:
                continue
            if calls[name] & writes:
                writes.add(name)
                changed = True

    return {
        "module": rel,
        "functions": len(funcs),
        "write": sorted(writes),
        "direct": sorted(n for n in writes if direct[n]),
        "only_via_a_callee": sorted(n for n in writes if not direct[n]),
        "raw_sql_writers": sorted(raw),
        "read_only": sorted(set(funcs) - writes),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("modules", nargs="*", default=["engine.py"])
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    reports = [analyse(m) for m in (a.modules or ["engine.py"])]
    if a.json:
        print(json.dumps(reports, indent=2))
        return 0
    for r in reports:
        print("%s — %d functions" % (r["module"], r["functions"]))
        print("  WRITE      : %d   (%d directly, %d only through a callee)"
              % (len(r["write"]), len(r["direct"]), len(r["only_via_a_callee"])))
        print("  read-only  : %d" % len(r["read_only"]))
        print("  of the writers, %d reach raw mutating SQL through `_conn`, "
              "bypassing the method split" % len(r["raw_sql_writers"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
