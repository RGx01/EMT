"""
job_coordinator.py — workstream B.

WHAT THIS IS NOT
    Not a lock. Measurement says uncoordinated jobs do not corrupt the database:
    everything runs on one loop, per-block writes are atomic (no `await` in the
    block-write call tree), and every long-running job is resumable or idempotent.
    A mutual-exclusion coordinator would serialise 23 members that mostly do not
    conflict — and would still not fix the one real bug in the set, because that
    bug is a dependency, not a race:

        "both write the pricing the attribution reads, and attributing over a
         not-yet-verified (imp_rate NULL) block used to crash PASS 2 and could
         cost the device at 0."                        — run_attribution_job, v4

WHAT IT IS
    A declaration of what each member READS, and an admission decision taken
    before it starts. The question a member asks is not "is anyone else writing?"
    but "is what I am about to read settled yet?".

      @job("attribution", depends_on=("import", "verify_pricing"),
                          exclusive_with=("delete",))

    `depends_on`     — correctness. I read what that writes; do not start me while
                       it runs.
    `exclusive_with` — politeness. We would fight over the connection or the API
                       budget. Kept separate on purpose: conflating the two is how
                       a correctness rule ends up enforced by a throttle and gets
                       tuned away.

REGISTRATION IS FREE
    `@job(...)` records a declaration and returns the function UNCHANGED. A member
    can be declared long before admission is wired into it, so the membership
    ratchet (`.build/check_job_membership.py`) can count down without any
    behaviour changing underneath it. That is deliberate: A's adoption went 0% ->
    100% because declaring was cheap and the guard was loud.

THE RULE IT MUST NOT BREAK
    Admission happens at job start and at batch boundaries — NEVER per block. The
    per-block write path is synchronous, and that atomicity is what keeps the
    currently-uncoordinated members safe. A coordinator that awaited inside a
    block write would remove the property and become the cause of the problem it
    was built to solve.  (B §3.4)
"""
import logging
import threading
import time

__all__ = ["job", "registry", "claim", "release", "running", "admission",
           "mark", "state", "validate", "reset_for_tests",
           "ADMIT", "DEFER", "WAIT", "PREEMPT"]

ADMIT, DEFER, WAIT, PREEMPT = "admit", "defer", "wait", "preempt"

_lock = threading.RLock()

# ── THE GRAPH LIVES HERE, NOT ON THE MEMBERS ────────────────────────────────
#
# B §6 q4 asked where the dependency graph should live: declared at each member,
# or centrally. Per-member declarations were tried first and failed with evidence:
# they register when their module is IMPORTED, so `engine` alone knew nothing of
# `delete` — declared on `_delete_worker` in `web/server.py` — and five members'
# exclusion on it silently did not apply.
#
# That was not hypothetical. `test_backfill_defers_while_delete_active` imports
# only `engine`, and the carbon backfill stopped deferring to a delete: a real
# behavioural regression, caused entirely by where the declaration lived.
#
# A manifest cannot be half-loaded. `@job(...)` still marks the function, which is
# what the membership ratchet counts and what makes an undeclared writer fail the
# build — but the graph is data, available the moment this module is imported.
_MANIFEST: dict = {
    # member                depends_on                        exclusive_with   on_conflict
    "live_tick":           ((),                               (),              ADMIT),
    # NOT `exclusive_with=("delete",)`. That was in an earlier draft of this
    # manifest and it was INVENTED: checked against the pre-migration source,
    # `run_api_import_job` never called `delete_in_progress()`, and the import
    # start route gates on `kraken_available`, `api_import_running` and the verify
    # pass — never on a delete. Declaring it would have quietly added a behaviour
    # v4 does not have, under cover of "the graph is measured".
    # Whether it SHOULD defer to a delete is a real question; it is open in §6,
    # not settled here.
    "import":              ((),                               (),              DEFER),
    "device_rate_drift":   ((),                               (),              DEFER),
    # A delete writes its tombstone and then removes blocks, on its own worker
    # connection. A prune in between sees the range still fully populated and
    # deletes the tombstone it just wrote. Measured, not invented: that window
    # existed when the prune ran inside the Data Management GET.
    # Declared for that one edge. NOT a long-running writer (one hourly pass over
    # a handful of tombstone rows), so it is deliberately absent from the
    # long-running lists in .build/check_job_membership.py, check_job_guards.py
    # and write_scope.py, whose figures B's doc asserts.
    "prune_tombstones":    ((),                               ("delete",),     DEFER),
    "verify_pricing":      (("import",),                      (),              DEFER),
    # `restore` (#56, 3 Oct 2026): each of these commits through a restore otherwise —
    # measured for the sweep on the Pi, where it restarted the safety backup for minutes.
    "reprice_sweep":       (("import",),                      ("delete", "restore"), DEFER),
    "reprice_sweep_to_done": ((),                             (),              DEFER),
    "carbon_backfill":     (("import",),                      ("delete", "restore"), DEFER),
    "measured_drain":      (("import",),                      ("delete", "restore"), DEFER),
    "attribution":         (("import", "verify_pricing"),     ("delete",),     WAIT),
    "gap_fill":            ((),                               (),              DEFER),
    "iog_split_carve":     ((),                               (),              DEFER),
    "settled_ev_split_heal": ((),                             (),              DEFER),
    # BL-86 (13 Oct 2026): a user-asked re-fetch of a range from Octopus — refused, not queued,
    # while anything else rewrites the same history. Its diff is a snapshot of the range: an
    # import, delete or restore between preview and apply would make it wrong (and apply refuses a
    # preview whose range has changed since).
    "range_refetch":       (("import",),                      ("delete", "restore"), DEFER),
    # PREEMPT, both of them, and evidenced: each cancels a running import, waits
    # up to 60s for it to stop, and ABORTS ITSELF if it will not
    # (web/server.py:2073 for the delete, 3810 for the restore). Not "wait" —
    # a user has asked for a destructive operation that cannot coexist with an
    # import, and the import is resumable, so it is the import that yields.
    #
    # Neither consults `admission()`; both already implement pre-emption
    # correctly by hand. The manifest is corrected so it DESCRIBES them, which is
    # the point of a manifest. Declaring `depends_on` with the default DEFER
    # would have said they stand aside for an import — the exact opposite.
    "delete":              (("import",),                      (),              PREEMPT),
    "restore":             (("import",),                      (),              PREEMPT),
}

_declared: dict = {
    name: {"depends_on": tuple(dep), "exclusive_with": tuple(ex),
           "on_conflict": oc, "func": None, "module": "manifest",
           # Set here and never overwritten. `@job(...)` fills in `func` and
           # `module` when the member's module imports, so those cannot
           # distinguish a manifest entry from an off-manifest one afterwards.
           "from_manifest": True}
    for name, (dep, ex, oc) in _MANIFEST.items()
}
_runs: dict = {}         # (name, key) -> {"status", "since", ...}

_ACTIVE = ("claimed", "running", "paused")

# ── OBSERVABILITY ───────────────────────────────────────────────────────────
#
# Every decision is logged as one `coord:` line of key=value pairs, so a soak can
# check B's rules from the add-on log alone (.build/soak.py --scan-log): nothing
# runs while a declared dependency runs, and nothing deferred waits forever.
#
# `admission()` is asked on every tick by the sweeps, so a refusal is logged when
# it STARTS and again when it CLEARS (`admit_after`), not on every repeat. The
# live tick admits unconditionally and is never logged.
logger = logging.getLogger("job_coordinator")
_last_refusal: dict = {}     # name -> (decision, blocker) of the refusal in force


def _log(event: str, name: str, **fields) -> None:
    parts = ["coord:", event, "job=%s" % name]
    parts += ["%s=%s" % (k, v) for k, v in fields.items() if v is not None]
    logger.info(" ".join(parts))


def job(name, *, depends_on=None, exclusive_with=None, on_conflict=None):
    """Declare a member. Returns the function unchanged.

    `on_conflict` is how this member reacts to a blocker, and all three of v4's
    behaviours are real and in use:

      DEFER   — "import job active — deferring"; the next tick re-checks. Right
                for a recurring sweep, which loses nothing by waiting.
      WAIT    — attribution pauses and polls rather than vanishing, because a
                user is watching it.
      ADMIT   — the live tick. Member 23 is never deferred and never waits: its
                input is a meter register, which moves whether or not the job
                runs.
      PREEMPT — the blocker yields instead. Restore and delete each CANCEL a
                running import, wait up to 60s, and abort themselves if it will
                not stop. A fourth outcome, missed by the first draft of this
                design, which named three.
    """
    def _decorate(fn):
        with _lock:
            d = _declared.get(name)
            if d is None:
                # A member marked at the function but absent from the manifest.
                # Registered from the arguments so a new job is never silently
                # graph-less, and reported by `validate()` so it gets added.
                d = _declared[name] = {
                    "depends_on": tuple(depends_on or ()),
                    "exclusive_with": tuple(exclusive_with or ()),
                    "on_conflict": on_conflict or DEFER,
                    "off_manifest": True,
                }
            d["func"] = fn.__name__
            d["module"] = getattr(fn, "__module__", "?")
        fn._job_name = name
        return fn
    return _decorate


def registry() -> dict:
    with _lock:
        return {k: dict(v) for k, v in _declared.items()}


def running(name: str, key: str = None) -> bool:
    with _lock:
        for (n, k), r in _runs.items():
            if n != name:
                continue
            if key is not None and k != key:
                continue
            if r.get("status") in _ACTIVE:
                return True
    return False


def admission(name: str):
    """(decision, blocker) — evaluated BEFORE any slow work.

    Dependencies are checked before exclusions so that a member blocked by both
    reports the one that matters. A blocker that is not a declared member is
    ignored rather than treated as absent-and-therefore-clear: an undeclared name
    is a typo, and a typo that silently admits is worse than one that never fires.
    """
    with _lock:
        d = _declared.get(name)
        if d is None:
            return ADMIT, None
        if d["on_conflict"] == ADMIT:
            return ADMIT, None
        verdict = (ADMIT, None)
        for dep in d["depends_on"]:
            if dep in _declared and running(dep):
                verdict = (d["on_conflict"], dep)
                break
        else:
            for ex in d["exclusive_with"]:
                if ex in _declared and running(ex):
                    verdict = (d["on_conflict"], ex)
                    break
        prev = _last_refusal.get(name)
        if verdict[0] != ADMIT:
            if prev != verdict:
                _last_refusal[name] = verdict
                _log(verdict[0], name, blocker=verdict[1],
                     edge="depends_on" if verdict[1] in d["depends_on"] else "exclusive_with")
        elif prev is not None:
            _last_refusal.pop(name, None)
            _log("admit_after", name, was=prev[0], blocker=prev[1])
        return verdict


def claim(name: str, key: str = None, *, force: bool = False):
    """Reserve this member (optionally one keyed run of it), atomically.

    Returns (True, None) or (False, reason). Taken under a lock and BEFORE any
    slow work — attribution's start route previously checked a status that only
    its coroutine set, after a 180s preflight and a database zip, so two starts
    inside that window both passed.

    `key` is the run identity: `meter_id` for attribution, absent for singletons.
    Two attributions for different devices are legitimate; two for the same device
    are not.
    """
    k = key or name
    with _lock:
        cur = _runs.get((name, k)) or {}
        if cur.get("status") in _ACTIVE:
            _log("claim_refused", name, key=k, reason="already_running")
            return False, "already running"
        if not force:
            decision, blocker = admission(name)
            if decision != ADMIT:
                _log("claim_refused", name, key=k, reason=decision, blocker=blocker)
                return False, "%s: %s" % (decision, blocker)
        _runs[(name, k)] = {"status": "claimed", "job": name, "key": k,
                            "since": time.time()}
        _log("claim", name, key=k, force=True if force else None)
    return True, None


def release(name: str, key: str = None) -> None:
    """Drop a claim that never became a run. A claim never released locks the
    member out until restart, so every path that fails to start must call this."""
    k = key or name
    with _lock:
        if (_runs.get((name, k)) or {}).get("status") == "claimed":
            _runs.pop((name, k), None)
            _log("release", name, key=k)


def mark(name: str, key: str = None, **fields) -> None:
    """Move a run on — typically `status="running"` then a terminal status.

    Terminal statuses matter to admission: `running()` only counts claimed,
    running and paused, so a run marked done frees the key for the next claim.
    A member that claims and never marks holds its key until restart.

    (This function was written with the coordinator, deleted the same day because
    nothing called it — API ahead of its callers is how `run_exclusive` became a
    correct primitive with one caller passing `lambda: None` — and restored here
    with attribution, its first caller.)
    """
    k = key or name
    with _lock:
        if (name, k) in _runs:
            before = _runs[(name, k)].get("status")
            _runs[(name, k)].update(fields)
            after = _runs[(name, k)].get("status")
            if after != before:
                _log("mark", name, key=k, status=after, was=before)


def validate(modules=None) -> dict:
    """Blockers that name nothing declared.

    `admission()` ignores an unknown blocker rather than treating it as present —
    otherwise a typo would block a member forever. The cost is that an unknown
    blocker SILENTLY does not apply, and there are two ways to get one: a typo,
    and a declaration that has not been imported yet.

    The second is not hypothetical. Declarations register when their module is
    imported, so `engine` alone knows nothing of `delete`, which is declared on
    `_delete_worker` in `web/server.py` — and attribution's exclusion on it
    quietly did not apply. The application imports both, so production is fine;
    depending on import order for correctness is not. This is the check that says
    so, asserted with both modules loaded.

    (B §6 q4 asks where the dependency graph should live. This is evidence for
    a central manifest over per-member declarations: a manifest cannot be
    half-loaded.)
    """
    with _lock:
        unknown = {}
        for name, d in _declared.items():
            if modules is not None and d.get("module") not in modules:
                continue
            missing = sorted({b for b in d["depends_on"] + d["exclusive_with"]
                              if b not in _declared})
            if missing:
                unknown[name] = missing
            if d.get("off_manifest"):
                unknown.setdefault(name, []).append("<not in _MANIFEST>")
    return unknown


def state() -> dict:
    with _lock:
        return {"members": len(_declared),
                "runs": [dict(v) for v in _runs.values()]}


def reset_for_tests() -> None:
    """Test infrastructure living in production code — named so, and unreachable
    from the application, which is the same exception `store_access` carries."""
    with _lock:
        _runs.clear()
        _last_refusal.clear()
