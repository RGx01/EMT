#!/usr/bin/env python3
"""
Which TABLES each long-running job writes — the input to B's admission rule.

WHY
    The obvious coordinator design is "two jobs may run together if their write
    scopes are disjoint". This measures the scopes, and the answer rules that
    design out at table granularity: **13 of the 14 live jobs write `blocks`**,
    and 11 also write `block_segments`. Everything conflicts with everything, so
    a table-level rule degenerates into a global lock.

    The scope that could work is finer — the block RANGE, or the meter. Every one
    of these jobs already operates over a range or a selection predicate (the
    import has from/to, the sweeps carry cursors, the SMB repairs have a floor,
    attribution has a device and a span), so the information exists.

THE ATOMICITY THAT MAKES THIS SURVIVABLE TODAY
    The per-block write path is FULLY SYNCHRONOUS — `_write_device_into_block`
    has no `await` anywhere in its call tree. On the single engine loop that
    makes one block's read-modify-write atomic: another coroutine cannot
    interleave inside it. Concurrent jobs interleave BETWEEN blocks, and each
    re-derives from the block's current state.

    That is why two attributions for different devices converge even though both
    rewrite the parent meter's remainder. It is also a property a coordinator
    must not quietly break: put an `await` inside a block write and the reasoning
    above stops holding.

HOW IT MEASURES
    Each BlockStore write method is mapped to the tables its SQL names, then each
    job inherits the union of everything it reaches. Comments and docstrings are
    stripped first — without that the verb regex harvests prose, and an earlier
    run reported `an`, `for`, `of` and `the` as tables.

USAGE
    python3 .build/write_scope.py [--json]
"""
import ast
import io
import json
import os
import re
import sys
import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, ".build"))
import write_inventory as W                      # noqa: E402

QUOTES = (chr(34) * 3, chr(39) * 3)
VERB = re.compile(
    r"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+([A-Za-z_]\w*)",
    re.I)
NOISE = {"set", "values", "where", "from"}

JOBS = ["run_api_import_job",
        "_run_historical_reprice_sweep", "_run_historical_carbon_backfill",
        "run_measured_history_drain", "run_gap_fill_job", "run_iog_split_carve",
        "run_attribution_job", "run_device_rate_drift_repair",
        "run_reprice_history_sweep_to_done"]


def code_only(node, lines):
    seg = "\n".join(lines[node.lineno - 1:node.end_lineno])
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(seg).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and tok.line.lstrip()[:3] in QUOTES:
                continue
            out.append(tok.string)
    except Exception:
        return seg
    return " ".join(out)


def _tables(text):
    return {t.lower() for t in VERB.findall(text)} - NOISE


def method_tables():
    with open(os.path.join(ROOT, ".build", "store_split_baseline.json"),
              encoding="utf-8") as fh:
        writers = {m for m in json.load(fh)["write"] if not m.startswith("__")}
    src = open(os.path.join(ROOT, "block_store.py"), encoding="utf-8").read()
    lines = src.split("\n")
    cls = next(n for n in ast.parse(src).body
               if isinstance(n, ast.ClassDef) and n.name == "BlockStore")
    out, calls = {}, {}
    methods = {m.name: m for m in cls.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name, m in methods.items():
        text = code_only(m, lines) + " " + " ".join(W._locals(m).values())
        out[name] = _tables(text)
        # the store's own helpers it calls (self.<method>): a writer whose SQL lives in a shared
        # private helper writes what the helper writes (engine step 4c: apply_measured_to_main and
        # write_predicted both write through _write_half_hour)
        calls[name] = {x.attr for x in ast.walk(m) if isinstance(x, ast.Attribute)
                       and isinstance(x.value, ast.Name) and x.value.id == "self"
                       and x.attr in methods}
    changed = True
    while changed:                                  # to a fixed point
        changed = False
        for name in methods:
            for c in calls[name]:
                if not out[c] <= out[name]:
                    out[name] |= out[c]
                    changed = True
    return {n: sorted(t) for n, t in out.items() if n in writers and t}


def analyse():
    m2t = method_tables()
    src = open(os.path.join(ROOT, "engine.py"), encoding="utf-8").read()
    lines = src.split("\n")
    funcs = {n.name: n for n in ast.walk(ast.parse(src))
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def scope(name, seen=None, depth=0):
        seen = seen if seen is not None else set()
        if name in seen or name not in funcs or depth > 4:
            return set()
        seen.add(name)
        node = funcs[name]
        out = set()
        for a in {x.attr for x in ast.walk(node) if isinstance(x, ast.Attribute)}:
            out |= set(m2t.get(a, []))
        out |= _tables(code_only(node, lines) + " "
                       + " ".join(W._locals(node).values()))
        for c in {x.func.id for x in ast.walk(node)
                  if isinstance(x, ast.Call) and isinstance(x.func, ast.Name)}:
            if c in funcs:
                out |= scope(c, seen, depth + 1)
        return out

    return {"methods_mapped": len(m2t),
            "jobs": {j: sorted(scope(j)) for j in JOBS if j in funcs}}


def main():
    r = analyse()
    if "--json" in sys.argv:
        print(json.dumps(r, indent=2))
        return 0
    jobs = r["jobs"]
    print("%-36s %s" % ("live job", "tables written"))
    print("-" * 100)
    for j in sorted(jobs):
        print("  %-34s %s" % (j, ", ".join(jobs[j]) or "(none resolved)"))
    n = len(jobs)
    blocks = sum(1 for t in jobs.values() if "blocks" in t)
    segs = sum(1 for t in jobs.values() if "block_segments" in t)
    print("\n  %d of %d jobs write `blocks`; %d write `block_segments`."
          % (blocks, n, segs))
    print("  A table-granularity admission rule is therefore a global lock.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
