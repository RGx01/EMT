#!/usr/bin/env python3
"""
Which long-running write jobs coordinate with anything — workstream B, measured.

WHY
    The programme map says of B: *"v4 got this right for some jobs and not others
    — that asymmetry is the bug."* It lists four jobs and their guard-reference
    counts. There are fourteen live ones, and the asymmetry is wider than the
    table suggests.

WHAT IT MEASURES
    Guard primitives are collected by shape, not from a list: module-level state
    and predicate functions whose names end `_running`, `_in_progress`, `_lock`,
    `_busy`, `_paused`. Then, for each long-running write job, whether the job or
    any DIRECT CALLER consults one. Callers matter because v4 sets the re-entry
    flags in the scheduler wrappers (`_maybe_reprice_history`), not in the job —
    measuring the job body alone reports five guarded jobs as unguarded, which it
    did on the first pass here.

WHAT IT DOES NOT MEAN
    An unguarded job is NOT automatically a race. Everything here runs on the one
    engine loop, so two coroutines interleave only at `await` points; there is no
    parallel-thread corruption to find (workstream A removed that class). The
    hazard is LOGICAL: a batch reading rows another job is halfway through
    rewriting. That is a real bug and an invisible one, but it is a different
    claim from "these nine race", and this tool does not make the stronger one.

USAGE
    python3 .build/check_job_guards.py [--json]
    python3 .build/check_job_guards.py --baseline .build/job_guard_baseline.json
    python3 .build/check_job_guards.py --update-baseline <path>
"""
import argparse
import ast
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, "engine.py")
REACH = os.path.join(ROOT, ".build", "reachability_baseline.json")
GUARD_SHAPE = re.compile(r"_running$|_in_progress$|_lock$|_busy$|_paused$")

# The long-running write jobs. Identified by hand rather than by pattern because
# "long-running" is a judgement: `run_*` catches helpers, and the property that
# matters is minutes of batch mutation, which no signature exposes.
JOBS = [
    "run_api_import_job", "run_deferred_verify_pricing",
    "_run_historical_reprice_sweep",
    "_run_historical_carbon_backfill", "run_measured_history_drain",
    "run_gap_fill_job", "run_iog_split_carve", "run_attribution_job", "run_device_rate_drift_repair",
    "run_reprice_history_sweep_to_done",
]


def analyse():
    src = open(TARGET, encoding="utf-8").read()
    tree = ast.parse(src, filename="engine.py")
    lines = src.split("\n")
    funcs = {n.name: n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    flags = [t.id for n in tree.body if isinstance(n, ast.Assign)
             for t in n.targets
             if isinstance(t, ast.Name) and GUARD_SHAPE.search(t.id)]
    preds = [n for n in funcs if GUARD_SHAPE.search(n)]
    guards = sorted(set(flags) | set(preds))

    def body(name):
        f = funcs[name]
        return "\n".join(lines[f.lineno - 1:f.end_lineno])

    callers = {}
    for name, f in funcs.items():
        for ref in {x.id for x in ast.walk(f) if isinstance(x, ast.Name)}:
            callers.setdefault(ref, set()).add(name)

    try:
        dead = set(json.load(open(REACH, encoding="utf-8"))["unreachable"])
    except Exception:
        dead = set()

    out = {}
    for job in JOBS:
        if job not in funcs:
            out[job] = {"missing": True}
            continue
        text = body(job) + "\n" + "\n".join(
            body(c) for c in sorted(callers.get(job, ())) if c in funcs)
        seen = sorted(g for g in guards
                      if re.search(r"\b" + re.escape(g) + r"\b", text))
        out[job] = {"guards": seen, "live": job not in dead}
    live = {k: v for k, v in out.items() if v.get("live")}
    return {
        "guard_primitives": {"module_flags": sorted(set(flags)),
                             "predicates": sorted(preds)},
        "jobs": out,
        "live_jobs": len(live),
        "live_unguarded": sorted(k for k, v in live.items() if not v["guards"]),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--baseline")
    ap.add_argument("--update-baseline")
    a = ap.parse_args()
    r = analyse()

    if a.update_baseline:
        with open(a.update_baseline, "w", encoding="utf-8") as fh:
            json.dump({"live_unguarded": r["live_unguarded"]}, fh, indent=2)
            fh.write("\n")
        print("baseline written: %d live jobs consult no guard"
              % len(r["live_unguarded"]))
        return 0

    if a.json:
        print(json.dumps(r, indent=2))
        return 0

    g = r["guard_primitives"]
    print("guard primitives: %d module flags, %d predicate functions\n"
          % (len(g["module_flags"]), len(g["predicates"])))
    for job in JOBS:
        d = r["jobs"][job]
        if d.get("missing"):
            print("  %-36s (not found)" % job)
            continue
        tag = "" if d["live"] else "   [unreachable]"
        print("  %-36s %s%s" % (job, ", ".join(d["guards"]) or "** NONE **", tag))
    print("\n  live jobs: %d    consulting no guard: %d"
          % (r["live_jobs"], len(r["live_unguarded"])))

    if a.baseline:
        known = set(json.load(open(a.baseline, encoding="utf-8"))["live_unguarded"])
        now = set(r["live_unguarded"])
        new = sorted(now - known)
        if new:
            print("\nFAIL: %d job(s) newly consult no guard:" % len(new))
            for n in new:
                print("  " + n)
            return 1
        gone = sorted(known - now)
        if gone:
            print("\n%d job(s) gained coordination since the baseline (good): %s"
                  % (len(gone), ", ".join(gone)))
        print("\nOK: the unguarded set has not grown.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
