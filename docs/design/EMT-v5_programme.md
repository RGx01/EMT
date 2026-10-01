# EMT v5 — programme map

*What "v5" covers, in dependency order, and why that order. This is the framing document: each
workstream gets its own design doc as it is picked up. The import contract already has one
([EMT-v5_import_spec.md](EMT-v5_import_spec.md)).*

**Status:** draft, 17 Sep 2026. Written immediately after 4.5.13 merged to main.

---

## 0. What v5 is

A new add-on with a new slug, in its own container, running **alongside** Energy Meter Tracker
rather than replacing it. Public versions are CalVer `YYYY.M.PATCH`; `v5` is the internal name for
everything before the first public release and never ships in a version string.

Repository layout follows v4: add-on manifest at the root, `docs/` and `tests/` as siblings. That is
not sentiment — the ~2,700-test suite and its runner depend on that shape, and the suite is the main
instrument for proving v5 behaves like v4.

**What v5 is not.** Not a rewrite of the pricing and settlement engine. That code is correct,
expensively so, and carries three years of tariff edge cases. v5 changes how it is *reached* — the
storage access model, the job boundaries, the surfaces — not what it computes.

---

## 1. The ordering argument

Five areas, and they are not independent. One decision sits underneath three of them.

```
    ┌──────────────────────────────┐
    │  A. Storage + access model   │   ← the root
    └──────────────┬───────────────┘
                   │
      ┌────────────┼────────────┐
      ▼            ▼            ▼
 ┌─────────┐ ┌──────────┐ ┌──────────┐
 │ B. Job  │ │ C. One   │ │ D. Sur-  │
 │ coordi- │ │ API for  │ │ facing   │
 │ nator   │ │ surfaces │ │ perf.    │
 └─────────┘ └────┬─────┘ └────┬─────┘
                  └─────┬──────┘
                        ▼
                 ┌─────────────┐
                 │ E. Importer │   ← last, deliberately
                 └─────────────┘
```

**Why A first.** v4 opens one SQLite connection with `check_same_thread=False` and shares it between
the engine thread and every Flask worker (`block_store.py:996`). The module's own docstring seven
lines above says the web server *should* open its own instance — the intent was recorded and never
enforced. The database is already WAL with `synchronous=NORMAL`, so SQLite is willing to hand every
reader its own consistent snapshot; the code simply doesn't ask for one.

Two production incidents follow directly from that, both recorded in the BL-50 design doc:

- `GET /api/config` 500 on 26 Aug 2026 — a cross-thread read during engine load returned a column as
  `None`, producing a `None` dict key and a jsonify crash. Transient, no corruption.
- `float * NoneType` in device re-attribution (4.5.4) — attribution ran PASS 2 over blocks a
  concurrent verify had not yet repriced.

**The pattern is already in the codebase — applied to exactly one consumer.**
`_generate_charts_offloaded` runs the renderer in a worker thread with *"its OWN read-only
BlockStore (never the engine's writer connection — the 3.1.3 cross-thread SQLite segfault)"*. So
read-only connections are not a proposal here: they are a fix this project already made once, under
duress, after a segfault, and then applied to the chart renderer alone while every other reader
carried on sharing the writer. Workstream A is generalising a solved problem, not adopting a new
one.

The conclusion drawn at the time of the 26 Aug incident was *"BL-50 must serialise reads too, not
just mutating jobs"*. **This document takes the opposite position.** Serialising reads onto the engine loop buys safety by
spending throughput, in the same release where we want surfacing to get faster. A read-only
connection per request buys both: readers stop seeing torn rows *and* stop queueing behind the
engine. Fix the access model and B, C and D all get easier; serialise reads instead and D fights B
for the rest of the project.

**Why E last.** The importer's contract is "write the v5 schema". Until A–D have settled what that
schema is, any importer work is written against a moving target.

---

## 2. Workstreams

### A. Storage and access model

*The root. Everything else is cheaper afterwards.*

- One writer connection owned by the engine; **read-only connections per request** (`mode=ro`), not
  a shared handle with `check_same_thread=False`.
- Decide the boundary explicitly: which operations are writes, and therefore which must go through
  the engine. Today that boundary exists only by convention.
- Retire the point fix that marshals `/api/config` onto the engine loop once the general mechanism
  replaces it.

**Acceptance:** a soak that hammers every read surface while the engine writes, asserting no torn
rows and no read blocked behind a write.

### B. Job coordinator (BL-50)

*Designed: [EMT-v5_B_coordinator_design.md](EMT-v5_B_coordinator_design.md). The headline from that work is that **B is not mutual exclusion** — measurement says uncoordinated jobs do not corrupt each other, and what v4 actually coordinates for is data DEPENDENCIES ("is what I am about to read settled?"), resource contention, and preconditions. A lock would serialise 22 members that mostly do not conflict and still not fix the one real bug in the set.*

*v4 got this right for some jobs and not others — that asymmetry is the bug.*

**Measured 22 Sep 2026** by `.build/check_job_guards.py`, pinned by
`tests/test_job_guards.py`. The asymmetry is wider than the four-job table this section used to
carry: there are **17 long-running write jobs, 14 of them live** (three are the unreachable legacy
backfills), and v4 has **17 coordination primitives** — 9 module flags and 8 predicate functions —
spread across them.

| | |
|---|---|
| live long-running write jobs | **14** |
| …consulting **no** guard at all | **9** |
| …coordinating with something | 5 |

And the five that coordinate all coordinate against the **same two things**: `api_import_running`
and `delete_in_progress`. So it is not that v4 lacks coordination — it is that exactly two jobs were
ever considered worth avoiding, and nothing else was. Backup/restore and historical import were
built not to disturb live processing and they behave; everything added since inherited neither the
habit nor a mechanism that would have made it automatic.

**The one real mutual-exclusion primitive is unused.** `run_exclusive()` takes the engine loop lock
and runs an operation under it. It has a single caller, and that caller passes `lambda: None` — it
is used to *drain* the in-flight tick, never to run anything. A correct mechanism at zero adoption,
which is precisely the shape workstream A found in the read companion (§1 there), and for the same
reason: nothing punishes the wrong choice at the moment it is made.

**What this is not.** Nine unguarded jobs is not nine races. They all run on the one engine loop, so
coroutines interleave at `await` points rather than running in parallel — A removed the
parallel-thread class. The hazard is **logical**: a batch reading rows another job is part-way
through rewriting. Real, and silent, but a weaker claim than a data race, and the number should not
be quoted as the stronger one.

The fix is not more ad-hoc flags — it is the single coordinator BL-50 specifies, with attribution,
attribution-backout and the blocks-delete endpoints as first-class members rather than
afterthoughts.

**What the coordinator cannot be.** The obvious rule — *two jobs may run together when their write
scopes are disjoint* — was measured (`.build/write_scope.py`, pinned by `tests/test_write_scope.py`)
and does not survive at table granularity:

> **13 of the 14 live jobs write `blocks`; 11 also write `block_segments`.**

Everything conflicts with everything, so a table-level rule is a global lock wearing a costume. The
scope that could work is finer — the block RANGE or the meter — and the information already exists:
the import has from/to, the sweeps carry cursors, the SMB repairs have a floor, attribution has a
device and a span.

**Why today's nine uncoordinated jobs nevertheless converge.** The per-block write path is **fully
synchronous** — no `await` anywhere in its call tree. On the single engine loop that makes one
block's read-modify-write atomic: another coroutine cannot interleave inside it, so jobs interleave
*between* blocks and each re-derives from the block's current state.

That is load-bearing and easy to lose. **A coordinator that introduces an `await` inside a block
write removes the property that currently keeps uncoordinated jobs safe** — which would make the
coordinator the thing that created the problem it was built to solve. There is a test asserting the
call tree stays synchronous.

*(Correction, same work: two attribution runs for different devices were described earlier as
touching "disjoint rows". They do not — the device write re-derives the PARENT meter's remainder,
so both rewrite the house row. They are safe for the atomicity reason above, not for disjointness.)*

Scope note: with workstream A done, this is coordinating **writers** — and writers are now
enumerated rather than guessed at (`.build/write_inventory.py`): **92 functions in `engine.py`**,
26 in `web/server.py` of which 19 are route handlers still writing on a request thread, 1 in
`kraken_ingester.py`, and **zero** in `backfill.py` / `csv_import.py` / `api_import.py`.

Note this is a *sharper* scope, not a smaller one. A §7 q3 established that all the long-running
jobs are writers, so the minutes-long work is exactly what B must coordinate. What A bought B is a
measured boundary in place of "serialise everything".

### C. One API for every surface

*The format was never the problem. The **coupling** is: a write triggers a wholesale recompute.*

v4 pre-renders `daily_usage.html` and `net_heatmap.html` into `CHART_DIR` — 28 production call sites
reference that machinery, and **17 call sites schedule a regeneration** (13 in `engine.py`, 4 in
`web/server.py`). Regeneration is wholesale: one `daily_usage.html` covering every day. So a single
half-hour block finalising re-renders two years of history, and a burst of writes queues a burst of
full re-renders behind it. That is the resource consumption, and it is why close-succession writes
followed by reads produced timeouts.

The failure mode is documented in `_schedule_chart_regen`'s own docstring: *"A synchronous render
over 2+ years of blocks takes long enough to stall the HA WebSocket heartbeat, which drops the
connection and re-runs startup: a reconnect→re-finalise→re-render storm."* A heavy read starves the
event loop, HA disconnects, reconnect re-runs startup, startup renders again.

**Five layers of compensation have accumulated around that one coupling:**

| # | Compensation | Provoked by |
|---|---|---|
| 1 | Renderer gets its own read-only `BlockStore` | the 3.1.3 cross-thread SQLite segfault |
| 2 | Render offloaded to a worker thread | render stalling the HA WebSocket heartbeat |
| 3 | In-progress guard + "re-run after it" / "change arrived mid-render" | renders piling up on each other |
| 4 | Deferring reads to a calm period | readers contending with the writer |
| 5 | `_blocks_data_version` change token | telling a client its pre-generated file is stale |

Every one of those is sound engineering *given the coupling*, and every one of them is load the new
app should not inherit. **Break the coupling and all five become unnecessary**: a write records that
data changed, it does not trigger a recompute; a surface asks for the range it is showing, when a
user is actually looking at it; nothing is pre-generated, so nothing can be stale, so no token is
needed to detect staleness.

Note the sequencing consequence: layer 1 is the read-only connection, which is workstream A. Doing A
first means C inherits it as a property of the system rather than as a special case for one
renderer.

A second, independent hazard in the same area — the 4.5.13 `/api/charts/regenerate` episode: the
scheduled renderer passed tariff context (`cap_from`, `import_schedule`) and the
on-demand endpoint silently did not, so the same function produced different charts depending on
who asked. Two days went into chasing it, in the rendering layer, because the divergence was
invisible at the call site.

**The rule that prevents the recurrence:** any function whose output depends on tariff context takes
that context as a **required argument with no default**. `cap_from=None` meaning "render as if
uncapped" is what made the second caller's omission silent. One API surface helps, but only if it is
also impossible to call the renderer without telling it which tariff era it is drawing.

### D. Surfacing performance

*Calibrate before optimising.*

**Know the target hardware.** Development and deployment are Raspberry Pi 4 class — ARM, modest
core count, storage on SD or USB rather than NVMe. That does not change the diagnosis below, but it
does change the tolerances: a wholesale re-render of two years of blocks is expensive on a Pi in a
way it would not be on a laptop, and I/O latency will dominate any query that touches many pages.
Benchmark on the Pi, never on a development machine — a budget that passes on a Mac and fails on the
target is worse than no budget.

The reference database is **78 MB** / ~135k blocks. That is small. If surfacing is slow at that
size, the cause is access shape — the shared connection (A) and the write-triggered wholesale
recompute (C) — not data volume. Both named causes are architectural, so most of D's win is
delivered by A and C rather than by optimisation work of its own. Expect the wins to come from A and from query shape, and treat "drop the
HTML files" (C) as an architectural simplification that happens to help, rather than as the
performance fix itself.

Measure first, and measure the right thing: time to first byte on each surface, and the number of
queries per surface. A surface issuing 200 small queries is the likely shape of the problem.

### E. Importer (see [EMT-v5_import_spec.md](EMT-v5_import_spec.md))

Ingests a backup zip at or above the floor version (currently **4.5.18**), or a `blocks.db`
taken from one. No cooperating exporter — the source is validated by the importer itself. Contract,
gates and table-by-table disposition are in that document.

Two things that were missing from every draft before 19 Sep and are now in the spec: the **first-run
fork** (§3a — new setup or restore, with the backup driving the credential prompt rather than the
reverse), and the rule that **absent credentials never block an import** on either side, which is
what keeps the diagnostic path working where a maintainer disconnects before importing someone
else's database.

**E is the only workstream with a retirement rule, and it needs one.** Everything v4-shaped lives
here — the 4.5.14 gate, the column rules, the `store_meta` allow-list, the band relabel — and all
of it is dead the day 4.5.13 sources stop being supported. That is a long way off and deliberately
undated (§1 of the import spec: the two add-ons coexist indefinitely), which is exactly why the
excision has to be cheap *by construction* rather than by a future decision:

> **E is one module. Dependencies point inward only — no domain code imports from it — and no
> importer-only concept appears in the domain layer. It is removed in the release after v4 sources
> stop being supported.**

§3 below is the argument for this. v4's three legacy backfills (162 lines) are still in `engine.py`
today, two years after being retired, **because they were retired by unwiring** — there was no
boundary to cut along, so unwiring was the cheap move and deletion never followed. The importer is
the one part of v5 guaranteed to become obsolete. It is the one part that must be shaped so that
obsolete means *deletable*, not *unwired*.

*Consequence for the band question:* the SMB-era `house off_peak → night` relabel lives here as a
declarative column rule. It is a schema translation, not a heal — it runs once, in one direction,
and expires with the module. Putting it in the domain instead would have made it permanent, because
imported and natively-derived rows would otherwise label the same tariff era two different ways and
every reader would have to accept both forever.

---

## 3. Dead code: what does not come across

An AST reachability trace over merged 4.5.13, over-approximating (a call by bare name or attribute
matches any function of that name), so anything reported unreachable is confidently unreachable:

```
reprice.py            4 defs   LIVE=0   dead: _band_label, _reconcile_house,
                                              reprice_block, resolve_ev_energy
carbon.py             5 pub    LIVE=2   dead: carbon_from_reprice, and ev_carbon +
                                              house_carbon, reachable only via it
pricing_segments.py  11 pub    LIVE=8   dead: attribute_devices, import_segments,
                                              total_cost_exc
```

`reprice.py` is dead in its entirety — 169 lines, every function. It, `carbon_from_reprice`, and
those three `pricing_segments` functions do not come across. Because there is no final v4 release,
this costs nothing: they simply never arrive, and their ~37 test references stay behind in the v4
repo where the suite still proves v4.

**Correct BL-63 in the v4 roadmap.** It describes the shelved layer as including
`carbon.ev_carbon` / `house_carbon` and *"9 of 11 public functions in `pricing_segments`"*. The
second is wrong — three of eleven are unreachable, and its recommendation to retire "the projection
layer" would delete six live functions. The first is **correct**: those carbon functions are
reachable only through the dead `carbon_from_reprice`. *(An earlier version of this paragraph said
Δ5 made them live. It made `period_ev_saving` and `slot_intensity` live, not these.)*

A backlog item that exists to clean up misleading code had itself become misleading — and so, in the
other direction, had this correction.

Note also that 85 of 1,116 production function names are unreachable overall, so the three modules
above are a floor, not the whole surface. A full pass belongs in whichever workstream touches each
module — not as a separate cleanup project.

**Why this matters more than the byte count.** The cost of the shelved model was never disk space —
it is misdirecting the next person to read the file.

*Corrected 21 Sep 2026.* This paragraph used to cite the 4.5.13 EV-split misdiagnosis as its
example, saying `_hybrid_ev_by_block` was chosen *"because unreferenced code reads as load-bearing"*.
That function has two live callers (`web/server.py:2763`, `:2926`) — it is the EV rule for accounts
**with** a physical EV meter, while `_dispatch_ev_split_by_bucket` serves accounts without one. The
misdiagnosis was picking the right function for the wrong account shape, not mistaking dead code for
live. The argument for removing genuinely unwired paths stands on its own; it did not need that
example, and the example was not true.

---

## 4. Open questions for the programme

1. ~~**Does v5 publish anything to Home Assistant?**~~ **Closed 18 Sep 2026: no.** EMT is
   **read-only toward Home Assistant** — it subscribes to sensors and creates nothing. No entities,
   no statistics, no Energy Dashboard feed. Side-by-side is therefore unconditionally safe on that
   axis: the one namespace `instance.py` cannot arbitrate is one EMT never enters. *Accepted
   consequence:* EMT's figures appear only in EMT's own UI. *Reopens if* that ever changes —
   publishing to HA would make collision with a running v4 a first-class problem again, needing
   distinct entity prefixes or an explicit ownership flag.
2. ~~**Where does the band vocabulary decision land?**~~ **Closed 18 Sep 2026 — and the earlier
   answer was wrong.** This question previously recorded that the import spec committed v5 to one
   vocabulary, `off_peak` / `peak`, with `day → peak` mapped on the way in. That would have
   destroyed information. The four v4 words are a **2×2 of attribution × band** — house
   `day`/`night` (clock-determined, Economy-7) and EV `peak`/`off_peak` (dispatch-determined) — plus
   `standard` for unknown. They are different determinations that happen to share the adjectives
   "cheap" and "expensive".

   **v5 keeps all four.** The importer makes exactly one change: house low `off_peak → night`,
   **IOG-SMB era only**, because only SMB has an `ECO7_NIGHT` bucket; pre-SMB house low genuinely
   means dispatch-discounted and `off_peak` is correct there. Full rule in import spec §5.2;
   schema consequences in [EMT-v5_schema_notes.md](EMT-v5_schema_notes.md) §3a; API shape in
   [EMT-v5_C_surfaces_design.md](EMT-v5_C_surfaces_design.md) §3–§4, which now carries **four** rate
   series keyed by *(attribution, band)* rather than a low/high pair.

   *Reopens if* a workstream wants a different internal representation — that changes the importer's
   contract and is raised there, not decided locally.
3. **Test-suite portage.** Carried wholesale, or rebuilt against the new API surface as C lands?
   Wholesale keeps the v4-equivalence proof; rebuilding suits the new shape. Probably both, in that
   order — but the decision affects how C is sequenced.
4. **What is the first public release's bar?** "Imports my history and shows it correctly" is a much
   earlier date than "replaces v4 for daily use". Naming that bar decides how much of B, C and D is
   pre-release work and how much is `2026.x+1`.