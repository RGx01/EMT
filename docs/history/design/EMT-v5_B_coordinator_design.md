# Workstream B — the job coordinator

> **History — a design record.** What was decided, when and why, as it stood then; parts describe code that has since changed. How EMT works today: [reference/architecture.md](../../reference/architecture.md). PR numbers (`#NNN`) are in the private archive, `RGx01/EMT-archive`.

*BL-50. Sits on top of [A](EMT-v5_A_storage_design.md): with reads structurally unable to mutate
and the writer thread-affine, what is left to coordinate is writers.*

**Status:** the coordinator exists, 23 Sep 2026. `job_coordinator.py`
holds the decision logic and the dependency manifest; `tests/test_job_coordinator.py` covers it.
Membership stands at **12/13** registered (`.build/check_job_membership.py`): every engine job
including the live tick, the three background workers, and (4 Oct 2026) `api_meter_delete_data`,
which joined `delete` and now holds the delete flag while it works, so the members
`exclusive_with` "delete" see it. The 1 outstanding is `api_db_vacuum` (which never joins — see
§4). **5 of the 7** members that declare an edge consult `admission()` (§5, acceptance 1);
`verify_pricing` joined on 4 Oct 2026. The other two, `restore` and `delete`, pre-empt the import
instead — each cancels a running import and waits, and aborts if it will not stop. (The range delete and the purge, entries
of `delete`, did neither until 4 Oct 2026; acceptance 3.)

*Registration changes no behaviour: `@job(...)` records a declaration and returns the function
unchanged. That is why the whole engine membership could be declared in one commit — the ratchet
counts down without anything moving underneath it. Wiring a member to actually consult `admission()`
is separate, and attribution is first.*

**Original status:** draft, 23 Sep 2026. Every figure below is produced by a committed tool and guarded by a
test; none is a hand count. The tools are named where the numbers appear.

---

## 1. What B is actually for

The framing inherited from BL-50 is mutual exclusion — jobs must not run at the same time because
they would corrupt each other. **Measurement does not support that framing**, and following it
would build the wrong thing.

Three findings, each from A's work:

- **Everything runs on one loop.** There is no parallel-thread class left; A removed it. Two jobs
  interleave at `await` points.
- **Per-block writes are atomic.** `_write_device_into_block` and the block write path have no
  `await` anywhere in their call tree, so one block's read-modify-write cannot be interleaved.
  Jobs interleave *between* blocks, each re-deriving from the block's current state.
  (`tests/test_write_scope.py`)
- **Every long-running job is resumable or idempotent.** 10 of 11 carry a cursor, resume marker or
  done-key; `run_iog_split_carve` self-selects on `imp_kwh_ev IS NULL` so carved blocks stop
  matching. (`tests/test_writer_exclusion.py`)

So uncoordinated jobs do not corrupt the database. **Then why does v4 coordinate at all?** Because
its own comments give three quite different reasons, and only by reading them does the real
requirement appear:

> **Data dependency** — *"Wait out a running import OR verify too: both write the pricing the
> attribution reads, and attributing over a not-yet-verified (`imp_rate` NULL) block used to crash
> PASS 2 and could cost the device at 0."* (`run_attribution_job`)

> **Resource contention** — *"Don't compete with an active historical import — both do heavy work
> on the engine's single DB connection."* (`_maybe_backfill_historical_carbon`)

> **Precondition** — *"deferred — supplier API not ready yet (no kraken discovery); will retry"*
> (`_maybe_backfill_historical_exc`) — not coordination at all.

**B is therefore a dependency and admission mechanism, not a lock.** The question a member asks is
not "is anyone else writing?" but **"is what I am about to read settled yet?"** — and separately,
"is now a polite moment?".

That distinction is the whole design. A mutual-exclusion coordinator would serialise 17 members
that mostly do not conflict, and would still not fix the attribution-over-unverified-blocks bug,
because that is a *dependency*, not a race.

---

## 2. What the measurements rule out

### 2.1 Scope-disjointness, at table granularity

`.build/write_scope.py`:

> **7 of the 7 live jobs write `blocks`; 4 also write `block_segments`.**

*(7 of 7, 4 with segments, since 27 Oct 2026, when engine step 4e-2 replaced the history reprice sweep and its run-to-done with the settle sweep; 8 of 8 earlier that day, when 4e removed the device rate drift repair; 9 of 9 from 23 Oct 2026, when engine step 2d removed the deferred pricing verify pass; 10 of 10 from 20 Oct 2026. The attribution job always wrote `blocks`, through
`append_block_replace`, five calls down — past the tool's depth of 4. Putting a device half-hour
back from `ha_reads` points it at its sensor with a direct UPDATE three calls down, which the
tool sees.)*

Everything conflicts with everything. A table-level admission rule is a global lock wearing a
costume.

### 2.2 Scope-disjointness, at range granularity

The obvious repair is "disjoint block ranges". An earlier draft of this section asserted the
information already existed — *"the import has from/to, the sweeps carry cursors, attribution has a
device and a span"*. **Measured against the signatures, that is false.** Only two of eleven
declare a scope:

| job | parameters | is that a scope? |
|---|---|---|
| `run_gap_fill_job` | `from_ts, to_ts, channels, meter_id` | **yes** |
| `run_attribution_job` | `device_meter_id, sensor_ids` (and, since 20 Oct 2026, `window`) | **yes — a meter** |
| `run_api_import_job` | `requested_from, chunk_days, max_chunks` | a start, no end |
| `_run_settle_history_sweep` | `force, max_days` | no — a budget |
| `run_measured_history_drain` | `max_batches` | no — a budget |
| `_run_historical_carbon_backfill` | `window_days, max_windows` | no — a budget |
| `run_iog_split_carve` | `max_blocks` | no — a budget |

`max_blocks`, `limit`, `batch`, `force` say **how much**, not **which**. The sweeps operate over
"whatever currently matches a predicate", and the predicate lives inside the SQL, not the signature.
Nine of eleven have no range to declare, so a range-scoped rule would require rewriting every
job's contract before the coordinator could admit anything.

**A dependency is declarable where a range is not.** Each member can name the handful of jobs whose
output it reads. That is a property its author already knows — v4's authors wrote it into comments.

---

## 3. The design

### 3.1 A member declares what it depends on, not what it writes

```python
@job("attribution", depends_on=("import", "verify_pricing"), exclusive_with=("delete",))
async def run_attribution_job(...):
```

- **`depends_on`** — *"I read what this writes; do not start me until it has settled."* This is the
  correctness relationship, and it is the one v4 gets wrong by omission.
- **`exclusive_with`** — *"we would fight over the connection or the API budget."* Politeness, not
  correctness. Separate, because conflating them is how a data dependency ends up enforced by a
  throttle and quietly dropped when the throttle is tuned.

Today's graph, measured from the guard checks (`.build/check_job_guards.py`):

| member | depends on / defers to | evidenced by |
|---|---|---|
| attribution | import, verify_pricing, delete | all three checked in `run_attribution_job` |
| carbon backfill | import, delete | both checked in `_maybe_backfill_historical_carbon` |
| measured drain | import, delete | both checked in `_maybe_drain_measured_history` |
| settle sweep (the reprice sweep until engine step 4e) | import, delete | both checked in `_maybe_settle_history` |
| verify_pricing | import | `api_import_running()` in the deferred pass |
| restore | import | `_restore_worker` drains a running import |
| **the other ten** | **nothing** | — and §6 q2 asks whether that is examined or merely unexamined |

*Every row is read off the PRE-MIGRATION source. The guard audit
(`.build/check_job_guards.py`) measured the starting position and is eroded by migration itself —
a member that moves to `admission()` stops naming the old flags — so it should be read as history,
not re-baselined into agreement.*

### 3.2 Four admission outcomes, not two

v4 already uses all four; only two are ever named. *(This section said three until
23 Sep 2026 — see PREEMPT.)*

- **admit** — nothing it depends on is running.
- **defer** — *"import job active — deferring"*; the next tick re-checks. Right for recurring
  sweeps, which lose nothing by waiting.
- **wait** — attribution *pauses and polls*: `j["status"] = "paused"; await sleep(1.0)`. Right for
  a user-initiated run that must not simply vanish.

- **preempt** — the blocker yields instead. Restore and delete each **cancel** a running import,
  wait up to 60 s, and abort *themselves* if it will not stop
  (`web/server.py:2073` and `:3810`). Not waiting: a user has asked for a destructive operation
  that cannot coexist with an import, and the import is resumable, so it is the import that gives
  way.

A coordinator offering only admit/refuse would force every deferrer to become a refuser, and a
user-visible job to fail where today it waits.

> **PREEMPT was missed by this document's first draft**, which named three outcomes after reading
> the deferrers and attribution. It surfaced only when `restore` was examined for migration and
> turned out not to wait at all. The manifest had `restore` on the default DEFER and `delete`
> declaring nothing — the first said it stands aside for an import, the second said nothing
> matters. Both are now PREEMPT, evidenced by the cancel calls, and a test asserts those calls
> still exist: a manifest that describes behaviour the code has stopped doing is worse than no
> manifest.
>
> Neither member consults `admission()`; both already implement pre-emption correctly by hand. The
> manifest was corrected to DESCRIBE them rather than the code changed to match it.

### 3.3 The registry already exists — generalise it, do not invent it

`attribution` was made multi-tenant on 22 Sep and now has every part a member needs:

- **a claim taken under a lock, before any slow work** (`attribution_claim`) — this is what closed a
  check-then-act window that was as wide as a 180-second preflight plus a database zip;
- **one entry per run, keyed by identity** (`_attribution_jobs[meter_id]`) — a single shared dict
  previously meant a second start wiped the first run's counters and one `control` key paused both;
- **status and controls addressing a run**, with a flat compatibility shape for the current page.

The coordinator is that, with the key generalised from `meter_id` to a member-supplied identity, and
`depends_on` / `exclusive_with` consulted at claim time. **Attribution is the prototype and should
be the first member migrated**, so the generalisation is tested against the case it came from.

### 3.4 What the coordinator must not do

**It must not introduce an `await` inside a block write.** §1's atomicity is what keeps the nine
currently-uncoordinated jobs safe. A coordinator that wraps each block write in an async admission
check would remove that property and become the cause of the problem it was built to solve. Admission
happens at job start and at batch boundaries — never per block. Guarded by
`tests/test_write_scope.py::test_the_block_write_path_never_awaits`.

---

## 4. Membership

### 4.0 The live tick is a member, and it is the one that can never be deferred

*Added 23 Sep 2026 — it was missing from the first draft of this document, which omitted the only
member with a real-time obligation.*

`engine_loop_task` ticks every 10s under `_engine_loop_lock` and writes the current block. It is a
writer like the others, but unlike any of them **deferring it has a cost outside the database**: it
is the only member whose input (a meter register) moves on whether or not the job runs.

What is true today, measured:

- **A historical import does not block it.** `run_api_import_job` never takes `_engine_loop_lock`
  and awaits between chunks, so the tick takes the lock uncontested on its next pass. The two write
  different rows — historic dates against the current block — and per-block writes are atomic (§1).
  So the answer to "does historic import block a live block, or is the live block queued?" is
  **neither: they interleave, and nothing needs queueing.**
- **`pause_engine()` does block it, and nothing is queued.** `_engine_tick` opens with
  `if _engine_paused: return`. Four callers pause: device-data delete, restore, zip upload and
  VACUUM. While paused there is no capture at all.
- **Energy is not lost by a pause, but boundary precision is.** Blocks store register readings
  (`imp_read_start` / `imp_read_end`), not samples, so the delta across a gap is still correct. What
  a pause can cost is a block *boundary*: `interpolate_value()` estimates the register at the
  boundary from the reads either side and stamps the block `interpolated = 1`.

**Consequences for the design:**

1. The tick is **a member**, with a declared property no other member has: it is never deferred and
   never waits. Every `exclusive_with` is read as "not against the tick".
2. **`pause_engine()` belongs to the coordinator.** It is currently a bare module flag set by four
   callers, and it is the only thing in the system that can stop live capture. A coordinator that
   admits and defers the rest while a separate global flag silently suspends this one has not
   actually taken charge of anything.
3. A member that needs the tick stopped — restore genuinely does — asks for it explicitly, and the
   interpolated boundary is the declared, visible price. Today that price is paid without being
   named anywhere.

### 4.1 The other 19

**12 further members**, enumerated rather than estimated — **13 members** in all (14 until engine step 4e-2 put the
settle sweep in place of the history reprice sweep and its run-to-done, and 15 until 4e removed the device rate
drift repair, both 27 Oct 2026; 16 until engine step 2d removed the deferred pricing verify pass,
23 Oct 2026).

*Corrected three times, in both directions. First 23, which missed the three background workers.
Then 26. Then 20, because **six of the eight route handlers stopped needing membership at all**: once a
handler moves onto `_engine_write`, a synchronous write on the engine thread is not a job — nothing
admits it, nothing defers to it, it runs where every other engine write runs. Listing them as
members-to-be was a category error, corrected by doing the port and watching the requirement
evaporate.*

*Corrected 23 Sep 2026: the first enumeration said 23 and missed three. It was built from "engine
jobs" + "route handlers" and had no category for a **background worker** — a thread spawned by a
route, writing from neither the engine loop nor a request thread. Found while migrating `delete`
and discovering that `api_meter_delete_data` does not set the delete flag at all: `_delete_worker`
does.*

**3 background workers** — `_delete_worker`, `_purge_worker`, `_restore_worker`. They are the
members others most want to avoid: `delete_in_progress()` is the second most consulted guard in the
codebase. `_delete_worker` and `_purge_worker` are ONE member with two entry points, because that
is what `delete_in_progress()` already means.

**7 live engine jobs** (`.build/check_job_guards.py`; three further jobs — the legacy exc, segment
and iog-split backfills — are unreachable and do not come across):
`run_api_import_job`, `_run_settle_history_sweep`,
`_run_historical_carbon_backfill`, `run_measured_history_drain`, `run_gap_fill_job`,
`run_iog_split_carve`, `run_attribution_job`.

*`_run_historical_reprice_sweep` and `run_reprice_history_sweep_to_done` went on 27 Oct 2026 (engine step 4e-2):
the settle sweep walks the whole history and gives what settle() declines its segments, so they had
nothing left. `run_device_rate_drift_repair` went the same day (engine step 4e): every settle() write takes the
devices with the main, so nothing is left to drift. Was 14, then 10, then 11, now 10 again: `run_settled_ev_split_heal` was removed on 16 Oct 2026
(bill and split paths design §7.3) — once v5's settle took the bill's split (#180) and the importer
gave imported history it (#184), the heal had nothing left but rewriting history at runtime. The four v4 one-off heals — `run_smb_rate_repair`, `run_smb_device_recost`,
`run_band_rate_snap`, `run_smb_ev_resplit` — retired with v4: each was marker-gated and
self-marking, and the import floor guarantees they have already run against any database v5 will
open (EMT-v5_import_spec §1.1). Workstream A removed them; `tests/test_v4_heals_removed.py` pins
it. `run_settled_ev_split_heal` was named like them and was NOT one — ungated, recurring; it stayed
until its purpose went (above).*

**1 bulk route handler** still writing on the request thread (`.build/write_inventory.py`):
`api_db_vacuum`. It never moves — see below.

*`api_corrections_apply` moved on 23 Sep 2026, as ONE unit rather than write by write. It is not a
single statement: three alternative UPDATEs, a device-row propagation, a `block_segments` rate
rewrite and an exc recompute, with explicit commits between them. Hopping each separately would let
another writer interleave between the main-meter update and the device rows that are supposed to
follow it — the "partial heal" its own comments warn about, which left the 2026-08-18 EV columns
100× out. The whole block became a nested function on the engine thread; `updated` is the only
value the rest of the handler needs back.*

**`api_db_vacuum` is a permanent exception, not an outstanding item.** VACUUM rewrites the entire
database file — minutes on a Pi — and the engine thread is where member 23 lives, so hopping it onto
the loop would stall live capture for the duration: the one thing §4.0 says must not happen. It
pauses the engine and works on its OWN connection now, which is the right shape; it was only ever
using the shared web store that four waitress threads also write through. So **acceptance 2's target
is 1, not 0**, and the 1 is named here.

*`api_region_reconcile_apply` and `api_historical_site_plan_apply` moved too: each performs three
writes that belong together (apply, stamp the marker, re-arm carbon), so they go in ONE hop — split
across three, a half-applied reconciliation could sit between two engine turns. Both loop over
SITES, a handful of regions rather than blocks, so a synchronous hop costs nothing. Each also called
`_rebuild_config_period_chain(store)` outside the hop, which is what kept them flagged after the
obvious writes moved.*

*`api_backfill_run` and `api_historical_reprice_from_csv` moved onto `_engine_write` on
23 Sep 2026 — neither needed the job treatment, because both already block the request for up to
1800 s on an engine-loop hop further down. The synchronous write adds no blocking they did not
already have; it only stops the write going through the connection four waitress threads share.*

*`api_meter_delete_data` moved on 23 Sep 2026, and did NOT need to become a paced job — which is
a correction to §6 q6, made an hour after writing it. The concern was that running the delete on the
loop would stall it. Measured: the deletes are four bulk `DELETE FROM ... WHERE meter_id = ?`
statements, not a Python loop over blocks, and the single loop inside iterates METERS — a handful.
The loop stalls for as long as SQLite takes on four indexed deletes, which is not the problem a
chunked sweep exists to solve. So `_do_delete_and_recompute` simply runs on the engine thread, with
the tick already paused and the import already cancelled: the exclusivity `run_exclusive` was
reaching for, without its executor thread.*

*(19 route handlers wrote on the request thread before 22 Sep. Six were ported to `_engine_write`,
three were never in scope — already on the loop via `_config_mutation_on_loop` — and two more fell
out when `save_config()` moved.)*

---

## 5. Acceptance

**Acceptance 1 is written first, before the coordinator has a single member.** This is the lesson A
paid for twice over: `run_exclusive` is a correct mutual-exclusion primitive whose only caller
passes `lambda: None`, and the read companion was invented three times and reached 0% adoption. Both
were designed well and adopted never. A §5.3's structural test is the only reason A did not go the
same way.

1. ~~**Every writing job is a member.**~~ **Written 23 Sep 2026, before the coordinator has a
   member** — `.build/check_job_membership.py`, `.build/job_membership_baseline.json`,
   `tests/test_job_membership.py`. Standing at **0 registered / 23** when written.

   Green from the start, because the guard is a **ratchet** rather than an assertion of the
   finished state: the unregistered set may shrink and must never grow. Verified in both
   directions — adding a writing job without a declaration fails the build on that commit
   (*"1 writing job(s) newly outside the coordinator"*), and decorating a member moves it out of
   the set.

   The member list is maintained **by hand**, which is deliberate. "Long-running writer" is a
   judgement no signature exposes, and a list someone must edit is a list someone must think about
   when adding a job — which is the behaviour this file exists to produce. The cost is that a
   rename leaves a ghost and quietly shrinks the denominator, so `not_found` is asserted empty.

   When the count reaches zero the ratchet is spent: the test then becomes a plain assertion that
   nothing is unregistered, and the baseline file is deleted. That is written into the test.

   **And registration is not adoption.** A member can declare `depends_on` and never consult
   `admission()`; the declaration is then decorative while the number still looks healthy — exactly
   how the read companion existed, was correct, and reached 0%. So the tool counts **consuming**
   separately, as a second ratchet that may only grow. Standing at **5 of 7** members that declare
   a dependency: attribution, reprice_sweep, carbon_backfill, measured_drain, and (29 Sep 2026)
   prune_tombstones — the stale-tombstone prune moved out of a GET, declared for its one edge,
   `exclusive_with=("delete",)`. **Re-counted 4 Oct 2026: 5 of 8.** #58 gave `restore` its edge, and
`import` declares none (it was miscounted here). The three that do not consult are `verify_pricing`,
which should; and `restore` and `delete`, which pre-empt the import (cancel it and wait). The
range-delete and purge entries of `delete` honoured nothing until 4 Oct 2026 — acceptance 3.
   `verify_pricing` consults since 4 Oct 2026: **6 of 8**, the other two pre-empting.
2. **No unregistered write from a request thread.** `.build/write_inventory.py` already measures
   this. The count was 8 and must reach **1** — `api_db_vacuum`, which cannot move without stalling
   member 23 (see §4). Anything else in that list is outstanding work.

   **Met, 4 Oct 2026** — the only request handler that writes is `api_db_vacuum`.
   `tests/test_b_acceptance_2.py` pins that SET (the existing figure test asserts only the count,
   against the doc), and accounts for every other writer in `web/server.py`: nested bodies handed
   to an engine hop, registered background workers, and `_rebuild_config_period_chain`, called
   only from `_mutate` bodies on the engine thread. A guard — checked by mutation: a route that
   writes on the request thread, or a module-level writer with no registered home, fails it.
3. **Declared dependencies are honoured.** For each `depends_on` edge, a test that starting the
   dependant while the dependency runs defers or waits — never proceeds.

   **`delete` → `import`, 4 Oct 2026.** A PREEMPT edge, so "never proceeds" means "never proceeds
   with the import still running": every entry of `delete` cancels a running import and waits, and
   aborts without deleting if it will not stop (`_stop_running_import`;
   `tests/test_delete_honours_import.py` for the range delete and the purge, which honoured nothing
   before). `restore` → `import` was already so (#58).

   **`verify_pricing` → `import`, 4 Oct 2026.** DEFER: the pass consults `admission()` at its start
   and at every chunk boundary, and while an import runs it stands aside with its cursor saved and
   its claim kept active (so attribution, which depends on it, keeps waiting). The engine tick
   resumes it when the import ends — from the cursor, or from the newest edge if the import added
   newer history. A launch while a pass runs does not start a second one
   (`tests/test_verify_defers_to_import.py`).

   **Every other edge, 4 Oct 2026: met.** `tests/test_b_edges_honoured.py` takes the real coordinator,
   makes each blocker running, calls the member's own entry point and observes it stop at its gate —
   13 edges across attribution (WAIT: it pauses in place, "waiting for <blocker>", writing nothing),
   the re-price sweep, carbon backfill, measured drain and the tombstone prune (DEFER). A table in
   the test is asserted equal to the manifest, so a new edge without a test fails. They are guards —
   the edges were already honoured — and each was checked to fail with its member's `admission()`
   call removed.
4. **A claim is taken before any slow work.** The attribution window, generalised: a test asserting
   the claim precedes preflight/backup in every member that has them.

   **Met, 4 Oct 2026** — `tests/test_b_claim_before_slow_work.py` checks the order in attribution,
   the API import, the restore, the range delete, the purge and the device delete. One still had the
   window and is fixed: the **API import's start route** checked `api_import_running()`, zipped the
   database (a minute or more on the Pi) and only then scheduled the job, which claimed `import` when
   it ran — a second start, a delete or a restore inside that minute saw no import. It now claims
   first (`api_import_claim`, released on failure); `api_import_running()` counts `"claimed"`; and a
   cancel that arrives during the backup is honoured when the job starts.

   *Not a member, deleted 4 Oct 2026:* `/api/historical/api-import/apply` ran `import_api_history`
   synchronously on the engine loop (up to 300 s) without claiming `import`, so members gated on
   `import` could not see it. It was v4's first synchronous wizard step, superseded by `/start` in
   the commit that added both; no UI in v4 or v5 ever called it.

   *Two more windows where nothing held `import`, closed 4 Oct 2026* (`tests/test_import_claim_held.py`):
   a **gap fill** never claimed it — it writes the same imported blocks through `import_api_history`
   and shares `_api_import_job`, so v4's dependants waited for it through `api_import_running()`;
   it now claims `import` for its whole run. And the full import **released it for a rate-limit
   cooldown** and resumed without re-claiming: `rate_limited` is not an end, and is no longer
   treated as one.
5. **Admission never happens per block.** The call tree of the block write path stays free of
   `await`.

   **Met, 4 Oct 2026** — `tests/test_b_per_block_no_admission.py` holds it for the nine per-block
   writers the jobs use (`_write_device_into_block`, `_apply_pass2`, `apply_measured_to_block`,
   `_reprice_history_blocks`, `_persist_block_segments`, `_persist_block_carbon`,
   `_attribute_block_carbon`, `append_block`, `append_block_replace`): no `await` and no coordinator
   call anywhere in their call trees. `test_write_scope.py` held the `await` half for the first one
   only. A guard — the property already held — checked by mutation: an `_coord.admission(...)` in
   `_apply_pass2` or `apply_measured_to_block` fails it.
6. **Soak on the Pi:** every member started concurrently against a real database, asserting no
   member starves and none runs while a declared dependency runs.

   *Harness written 29 Sep 2026.* The coordinator now logs every decision as a `coord:` line
   (claim, refusal, release, status change, and admission refusals with the blocking member and
   edge), and `.build/soak.py scan-log` checks the add-on log for a dependant becoming active
   while its dependency is, and for a refusal still standing after `--starve-min`. **It can only
   see what members report:** until `verify_pricing` and `import` consult `admission()` and
   every member claims/marks, a clean scan says less than it appears to.

   *Run 29–30 Sep 2026 (five Pi soaks, A design §5e): **NOT EXERCISED.** No member claimed, marked
   or was refused in any of them — the only job running was the re-price sweep, admitted every
   time with nothing in its way. Testing this needs members started deliberately so they
   overlap, and most writing members call Octopus or HA, which the soak must not.*

   ***Met 5 Oct 2026 (soak 15, a21).*** The soak database restored through the import gate, its
   sweep rebuilding 37,220 blocks; mid-sweep, a bounded gap fill (which claims `import` since
   #85) and a one-day range delete were started through the API. From the add-on log:

   | time | |
   |---|---|
   | 07:19:07 | `claim import`; `defer reprice_sweep blocker=import edge=depends_on` — the sweep stood aside at its cursor in the same second |
   | 07:19:11 | `import done`; `claim verify_pricing`; `admit_after reprice_sweep` |
   | 07:19:51 | `claim delete`; the delete checked for a running import and took its safety backup; `defer reprice_sweep blocker=delete edge=exclusive_with` |
   | 07:20:17 | 48 blocks deleted; `delete done`; `admit_after reprice_sweep` |
   | 07:24:45 | `verify_pricing done` (41 slots corrected) |

   `scan-log`: coordinator PASS (members seen: delete, import, reprice_sweep, restore,
   verify_pricing), database faults PASS, no refusal left standing. The harness change that made
   it observable is #93 (the wait loop reports every poll). Loop blocking failed inside the sweep
   only — SD-card commit stalls, ROADMAP §A.1, not a B criterion; the quiet phase had one
   over-budget lag (a 4.8 s log-pipe flush) and no slow commit.

---

## 6. Open questions

*Left open. Reasoning them shut is how the last three design claims in this programme turned out
wrong — a snapshot bug that did not exist, "disjoint rows" that were shared, and the range-scope
assumption §2.2 corrects.*

1. **Is `exclusive_with` a real requirement or an artefact of one connection?** Every contention
   comment cites *"the engine's single DB connection"*. A §3 gives readers their own connections but
   leaves one writer. If contention is only ever writer-vs-writer on one connection, politeness
   exclusion may be a throughput tuning knob rather than part of the coordinator.
2. **What happens to a deferred job that never gets a turn?** v4's sweeps retry on the next tick
   forever. With 22 members and a busy import, starvation is expressible; no measurement says
   whether it happens today.
3. ~~**Do the route-handler members need user-visible status?**~~ **Moot: there are none left
   that need it.** Six became engine-thread writers, `api_meter_delete_data` stayed synchronous
   because it is fast enough to, and `api_db_vacuum` never joins. The question was premised on
   claim-schedule-return-poll being necessary, and measuring each case dissolved it.
4. **Where does the dependency graph live?** Declared at the member (readable at the job, drifts
   independently) or centrally (one place to read, easy to forget when adding a job). A §1's finding
   — that the safe path stays unused unless the unsafe one is hard to reach — argues for whichever
   makes an *undeclared* member fail loudly.

   ~~**Evidence for "centrally"**~~ **Closed 23 Sep 2026: centrally, and it cost a real
   regression to learn.** Per-member declarations register when their module is imported, so
   `engine` alone knew nothing of `delete` — declared on `_delete_worker` in `web/server.py` — and
   five members' exclusion on it silently did not apply.

   Not hypothetical: `test_backfill_defers_while_delete_active` imports only `engine`, and the
   carbon backfill **stopped deferring to a delete** the moment it started consulting `admission()`.
   A behavioural regression caused entirely by where the declaration lived.

   `_MANIFEST` in `job_coordinator.py` now holds the graph, available the moment that module is
   imported. `@job(...)` still marks the function — that is what the ratchet counts and what makes
   an undeclared writer fail the build — but a member marked and absent from the manifest is
   reported by `validate()` rather than silently graph-less. **A manifest cannot be half-loaded.**
5. **Should an import defer to a delete?** It does not today: `run_api_import_job` never calls
   `delete_in_progress()`, and the start route gates on `kraken_available()`,
   `api_import_running()` and the verify pass only. Both mutate blocks heavily on one connection,
   so there is a case — but it is a behaviour change, and it needs its own evidence rather than
   arriving as a manifest entry.

   *Recorded because it nearly did arrive that way: an earlier draft of `_MANIFEST` declared
   `import exclusive_with=("delete",)`, invented rather than measured, in a document that says
   dependencies are read off the guard audit. Caught by re-checking every entry against the
   pre-migration source.*

6. ~~**Does `run_exclusive` come across?**~~ **Closed 23 Sep 2026: no. It is deleted under §4a,
   and its one real use becomes a paced job.**

   It looked like the one correct primitive v4 has. Measured, it is three things at once and
   none of them survives workstream A:

   - **It runs the work on an EXECUTOR THREAD.** Its own comment explains why — *"a heavy fn run
     directly on the loop thread would freeze the loop (HA websocket drops, request queue backs
     up)"* — but A §3.3 makes exactly that illegal: `writer()` raises off the engine thread. The
     primitive and the workstream solve the same problem in opposite directions.
   - **Its exclusion is partial.** `_engine_loop_lock` is acquired in exactly two places in the
     codebase: `run_exclusive` and `engine_loop_task`. **None of the fourteen long-running jobs
     takes it.** So it excludes the TICK, not the other writers — an import or a sweep would
     interleave with the executor thread on the one connection regardless.
   - **What closes that gap is prose.** *"Caller must `pause_engine()` and drain any running
     import first."* Safety by convention, in a docstring, for a primitive with one caller.

   **The pattern that actually works is already in the codebase fourteen times**: a paced batch job
   on the loop that `await`s between batches, keeping the loop responsive without a second thread.
   That is how every sweep, backfill and drain already behaves, and it needs no lock because
   per-block writes are atomic (§1).

   **Done 23 Sep 2026.** `run_exclusive` is deleted; its barrier is now `drain_tick()`, named for
   what it actually did. `pause_engine()` stops the NEXT tick but not one already running, and
   waiting for the lock is all the caller ever needed. The three findings above are kept rather
   than deleted with the code: if anyone reintroduces a mutual-exclusion primitive here, the
   argument against this one should be readable without archaeology.

   *Corrected an hour after writing: this said the delete "becomes a paced job". It did not need to
   — see §4. The paced-job reflex came from assuming the delete loops over blocks; it issues four
   bulk SQL statements. The pattern was right in general and wrong for this member, which is what
   measuring each case rather than applying the conclusion is for.*
