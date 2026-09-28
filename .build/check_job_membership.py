#!/usr/bin/env python3
"""
Workstream B, acceptance 1: every writing job is a coordinator member.

WRITTEN BEFORE THE COORDINATOR EXISTS. That is the point, and it is the one
lesson this programme has paid for twice:

  * `run_exclusive()` is a correct mutual-exclusion primitive. It has one caller,
    passing `lambda: None` — used to drain a tick, never to run anything.
  * The read companion was invented three separate times, after three production
    incidents, and reached 0% adoption.

Both were designed well and adopted never, because nothing punished the wrong
choice at the moment it was made. Workstream A only escaped that by writing its
structural test first; route adoption went 0% -> 100% and the baseline now holds
it there. This is the same device for B.

WHAT IT MEASURES
    The 23 members named in docs/design/EMT-v5_B_coordinator_design.md §4, and
    whether each one declares itself to the coordinator. A member declares by
    carrying a `@job(...)` decorator (engine jobs and, once ported, the bulk
    route handlers) — the surface the design specifies. None do yet, so the
    baseline is the full list and the guard is a RATCHET: the unregistered set
    may shrink and must never grow.

    A new job that writes and does not register therefore fails the build on the
    commit that adds it, rather than being noticed a year later by someone
    counting guard flags.

WHAT IT IS NOT
    It does not check that declarations are CORRECT — that a member's
    `depends_on` names the right jobs. That is acceptance 3, and it needs the
    coordinator. This only asks whether the member showed up.

USAGE
    python3 .build/check_job_membership.py [--json]
    python3 .build/check_job_membership.py --baseline .build/job_membership_baseline.json
    python3 .build/check_job_membership.py --update-baseline <path>
"""
import argparse
import ast
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DECORATOR = "job"

# The 23, from the design's §4. Held here rather than derived, because
# "long-running writer" is a judgement no signature exposes — and because a list
# that must be edited by hand is a list someone has to think about when adding a
# job, which is the behaviour this whole file exists to produce.
ENGINE_JOBS = [
    "engine_loop_task",                     # member 23 — the live tick. Never deferred.
    "run_api_import_job", "run_deferred_verify_pricing",
    "_run_historical_reprice_sweep", "_run_historical_carbon_backfill",
    "run_measured_history_drain", "run_gap_fill_job", "run_iog_split_carve",
    "run_attribution_job", "run_device_rate_drift_repair",
    "run_settled_ev_split_heal",
    "run_reprice_history_sweep_to_done",
]
# Background WORKERS: threads spawned by a route, writing from neither the engine
# loop nor a request thread. Missed by the first enumeration, which was built from
# "engine jobs" + "route handlers" and had no category for these — found when
# migrating `delete` and discovering that `api_meter_delete_data` does not set the
# delete flag at all; `_delete_worker` does.
#
# They are the members other members most want to avoid: `delete_in_progress()` is
# the second most consulted guard in the codebase.
WORKER_JOBS = [
    "_delete_worker", "_purge_worker", "_restore_worker",
]
# Route handlers that must be members. NOT "every handler that writes" — six of
# the eight originally listed here stopped needing membership entirely once they
# moved onto `_engine_write`, because a synchronous write on the engine thread is
# not a job: nothing admits it, nothing defers to it, it just runs where every
# other engine write runs.
#
# What is left are the two that STOP THE WORLD. Both call `pause_engine()`, which
# suspends member 23 — the live tick, the one member that is never deferred — so
# they are exactly the things other members should know about.
#
#   api_db_vacuum          rewrites the whole file; minutes on a Pi
#   api_meter_delete_data  pauses, cancels the import, drains the tick, deletes
#
# (`_delete_worker` is a different member, "delete": the BACKGROUND delete. This
# one is the synchronous path.)
ROUTE_JOBS = [
    "api_db_vacuum", "api_meter_delete_data",
]


def _decorated(path, names):
    """Which of `names` in this file carry the registration decorator."""
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    found, registered = set(), set()
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if n.name not in names:
            continue
        found.add(n.name)
        for d in n.decorator_list:
            f = d.func if isinstance(d, ast.Call) else d
            nm = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
            if nm == DECORATOR:
                registered.add(n.name)
    return found, registered


def _consumers(path, names):
    """Which members actually CONSULT `admission()` — in their own body or in a
    scheduler that calls them.

    The membership ratchet counts REGISTRATION, and a member can declare
    dependencies it never consults: the declaration is then decorative and the
    number still looks healthy. That is precisely how the read companion reached
    0% adoption while existing and being correct. So consumption is counted
    separately.
    """
    import ast as _ast
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        src = fh.read()
    tree = _ast.parse(src, filename=path)
    funcs = {n.name: n for n in _ast.walk(tree)
             if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))}
    # member name -> the function decorated with it
    member_of = {}
    for n in funcs.values():
        for d in n.decorator_list:
            f = d.func if isinstance(d, _ast.Call) else d
            nm = f.id if isinstance(f, _ast.Name) else getattr(f, "attr", None)
            if nm == DECORATOR and isinstance(d, _ast.Call) and d.args:
                a = d.args[0]
                if isinstance(a, _ast.Constant):
                    member_of.setdefault(a.value, n.name)

    def _admits(fn, member):
        for c in _ast.walk(fn):
            if isinstance(c, _ast.Call) and \
                    getattr(c.func, "attr", getattr(c.func, "id", None)) == "admission" \
                    and c.args and isinstance(c.args[0], _ast.Constant) \
                    and c.args[0].value == member:
                return True
        return False

    out = set()
    for member, fname in member_of.items():
        if fname in funcs and _admits(funcs[fname], member):
            out.add(member)
            continue
        for n in funcs.values():                       # a scheduler that calls it
            if _admits(n, member):
                out.add(member)
                break
    return out


def analyse():
    e_found, e_reg = _decorated("engine.py", set(ENGINE_JOBS))
    r_found, r_reg = _decorated("web/server.py", set(ROUTE_JOBS) | set(WORKER_JOBS))
    members = ENGINE_JOBS + ROUTE_JOBS + WORKER_JOBS
    missing = sorted(set(members) - (e_found | r_found))
    registered = sorted(e_reg | r_reg)
    consuming = sorted(_consumers("engine.py", None)
                       | _consumers("web/server.py", None))
    return {
        "members": len(members),
        "registered": registered,
        "consuming": consuming,
        "unregistered": sorted(set(members) - set(registered) - set(missing)),
        "not_found": missing,
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
            json.dump({"unregistered": r["unregistered"]}, fh, indent=2)
            fh.write("\n")
        print("baseline written: %d of %d members unregistered"
              % (len(r["unregistered"]), r["members"]))
        return 0

    if a.json:
        print(json.dumps(r, indent=2))
        return 0

    print("coordinator membership — %d members\n" % r["members"])
    print("  registered   : %d" % len(r["registered"]))
    print("  CONSUMING    : %d   (declares AND consults admission)"
          % len(r["consuming"]))
    print("  UNREGISTERED : %d" % len(r["unregistered"]))
    if r["not_found"]:
        print("  NOT FOUND    : %s" % ", ".join(r["not_found"]))
        print("     (renamed or deleted — the list in this file is stale)")
    for m in r["unregistered"]:
        print("      %s" % m)

    if a.baseline:
        with open(a.baseline, encoding="utf-8") as fh:
            known = set(json.load(fh)["unregistered"])
        now = set(r["unregistered"])
        new = sorted(now - known)
        if new:
            print("\nFAIL: %d writing job(s) newly outside the coordinator:" % len(new))
            for n in new:
                print("  " + n)
            print("\nA job that writes is a member. Declare it, or explain here why "
                  "it is not one — B §4.")
            return 1
        gone = sorted(known - now)
        if gone:
            print("\n%d member(s) joined since the baseline: %s"
                  % (len(gone), ", ".join(gone)))
        print("\nOK: the unregistered set has not grown.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
