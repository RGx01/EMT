# EMT v5 — architecture

*The agreed picture of what v5 is made of, what carries across from v4, and what does not.
Workstreams A and C hang off this; so does the porting decision. Where this document and a
workstream design disagree, this one is wrong and should be fixed.*

**Status:** draft, 18 Sep 2026. Every figure measured against merged 4.5.13.

---

## 1. Four layers

v4 has these layers too — they are simply not separated. v5's whole architectural change is to make
the boundaries real.

```
┌──────────────────────────────────────────────────────┐
│  presentation   a client draws from the API           │  NEW — nothing carried
├──────────────────────────────────────────────────────┤
│  API            one data surface, every consumer      │  consolidate 126 routes
├──────────────────────────────────────────────────────┤
│  domain         pricing, settlement, billing, carbon  │  CARRIED substantially as-is
├──────────────────────────────────────────────────────┤
│  storage        read/write split, one writer          │  carried + A's boundary
└──────────────────────────────────────────────────────┘
```

**The HA WebSocket is an inbound edge into the domain layer**, not part of the API or presentation
layers. It carries sensor subscriptions that drive block derivation, HA recorder history for device
attribution, and live state for the overview page. It is carried unchanged, and it is
unrelated to how the browser talks to EMT.

**The rule that makes the layering hold:** each layer may only call downward, and the domain layer
emits **values, never markup**. v4's central architectural fault is that its billing calculation and
its HTML generation live in the same functions — which is why two surfaces could disagree about the
same number.

---

## 2. What each v4 file becomes

| v4 file | lines | v5 |
|---|---|---|
| `block_store.py` | 7,332 | **Carried.** Gains A's read/write type split (104 write methods, 77 read-only, measured). |
| `engine.py` | 14,009 | **Carried.** 182 lines of chart wiring deleted (1.3%), 330 lines unreachable deleted (2.5%). ~96% survives. |
| `energy_charts.py` | 5,436 | **Split and mostly deleted.** 79% presentation dies; ~1,086 lines of billing/period logic move to a new `billing.py`. |
| `web/server.py` | 9,109 | **Consolidated, not rewritten.** 126 `/api/*` routes triaged into one surface; 22 page routes and the `CHART_DIR` pipeline go. |
| `web/templates/` | 19,233 | **Replaced** by a client that fetches from the API. |
| `reprice.py` | 169 | **Deleted.** Zero reachable functions. |
| `carbon.py` | 146 | **Carried** minus `carbon_from_reprice` (unreachable). |
| `pricing_segments.py` | — | **Carried** minus 3 unreachable functions. |
| `kraken_*`, `ha_client`, `iog_cap`, `bill_parser`, `instance`, `vat_calendar`, … | — | **Carried.** Untouched by this restructuring. |

---

## 3. Removing chart generation is a small, contained job

This was assumed to be the hard part. Measured, it isn't:

```
engine.py chart wiring       182 lines of 14,009   (1.3%)   7 functions, 17 call sites
block_store.py                 0 lines             (0%)     serves data either way
energy_charts.py           4,199 lines of 5,436   (79%)     presentation — deletes
```

The engine **pokes** the renderer; it is not woven into it. The three largest chart functions —
`generate_daily_import_export_charts` (2,027 lines), `generate_net_heatmap` (848),
`build_day_chart_html` (615) — are 3,490 lines of pure HTML emission with no domain logic to rescue.

### The real work: rescue the billing logic first

Only **5 of `energy_charts.py`'s 17 public functions** have any external caller. Two of those are the
HTML generators that die. The other three are domain logic that every surface depends on:

| function | lines | fate |
|---|---|---|
| `calculate_billing_summary_for_period` | 377 | **Untangle.** Billing arithmetic and presentation in one function — the arithmetic is what surfaces must agree on. |
| `get_billing_periods_from_config_periods` | 177 | Move to `billing.py` as-is. |
| `build_meter_colors_from_config` | — | Presentation. Moves to the client. |

`calculate_billing_summary_for_period` is the crux of the whole workstream. Until the arithmetic is
separable from the markup, "one API for every surface" cannot be true, because the number and its
rendering are the same 377 lines.

**Sequence:** extract `billing.py` *before* deleting anything. Rescue, then demolish.

---

## 4. The unwired paths

Code nothing calls is not free. It misleads whoever reads next — during 4.5.13 development a bug was
diagnosed at length against `_hybrid_ev_by_block`, confidently and wrongly, because the unreferenced
path read as load-bearing. None of this is ported.

AST reachability from real entry points, deliberately over-approximating (a call by bare name or
attribute matches any function of that name), so anything still reported unreachable is confidently
unreachable:

**Whole codebase: 85 of 1,116 defined names unreachable.**

### `engine.py` — 14 functions, 330 lines of 13,129 (2.5%)

| function | lines | note |
|---|---|---|
| `_maybe_backfill_historical_exc` | 62 | one of the three legacy backfills |
| `_apply_mini_boundary_read` | 54 | Mini-mode path |
| `_maybe_backfill_historical_iog_split` | 50 | legacy backfill |
| `_maybe_backfill_historical_segments` | 50 | legacy backfill |
| `_naive_iso` · `fetch_api_window` | 38 | |
| `_regen_charts_after_import` | 17 | dies with charts anyway |
| `_mini_read_and_apply` · `_mini_boundary_callback` | 27 | Mini-mode |
| `_review_band_reason` | 15 | |
| `register_block_boundary_callback` | 9 | |
| `is_mode_configured` · `floor_to_hh` · `io_save` | 8 | |

**The three legacy backfills (162 lines) were "retired" in 4.5.x by unwiring rather than deleting.**
That is the pattern this section exists to stop: the retirement was real, the removal never happened,
and the code has sat since looking like part of the system.

### Whole modules

- **`reprice.py` — 169 lines, all four functions unreachable.** Does not come across.
- `carbon.py` — `carbon_from_reprice` only.
- `pricing_segments.py` — `attribute_devices`, `import_segments`, `total_cost_exc`.

**Correct the v4 roadmap.** BL-63 describes the shelved layer as including `carbon.ev_carbon` /
`house_carbon` and "9 of 11 public functions in `pricing_segments`". Neither holds: the Δ5 wiring
made the carbon functions live, and `pricing_segments` is 12 live / 3 dead.

---

## 5. The API is already there

```
/api/* routes : 126
page routes   :  22
```

85% of `server.py`'s routes are already data endpoints. C is therefore **triage, not invention** —
consolidate 126 endpoints onto one coherent shape, delete the 22 pages and the pre-render pipeline
behind them. Far less risk than "build an API", and the work is mostly deciding what the shape is
(see [EMT-v5_C_surfaces_design.md](EMT-v5_C_surfaces_design.md)).

---

## 6. How v5 gets built: freeze v4, seed EMT with a verbatim copy

**Decided 18 Sep 2026.**

### The shape

```
4.5.14   BL-71 + BL-72 (+ BL-73, optional). The LAST v4 feature release.
   │     BL-72 is mandatory: `heal_level` and `written_by` must be written
   │     into the user's database BY v4 — EMT cannot retrofit a marker it
   │     was not present for, and a marker added later never covers the
   │     backups already sitting on users' disks.
   │     `heal_level` is what makes .14 the importer's SOURCE FLOOR,
   │     collapsing E from two schema eras to one (import spec §1.1, §3, §4).
   │     BL-73 (a `supplier` token) is optional — v4 is frozen and therefore
   │     Octopus-only, so `written_by` already implies it. The ACCOUNT stamp
   │     needs nothing: `kraken_account_number` and the mismatch guard are
   │     already built (import spec §1.1, §7).
   │
   ├──▶  v4 FROZEN. Users sit on a stable release for as long as they like.
   │
   └──▶  EMT repo, commit one: 4.5.14 copied VERBATIM. Suite green on day one.
         Then deconstruct in the open, at whatever pace:
           1. dead code removed (§4)
           2. billing.py extracted from energy_charts.py (§3)
           3. A — ReadStore / WriteStore
           4. C — API consolidated, HTML pipeline deleted
           5. schema — 15 columns dropped; band moves to block_segments alone
           6. client — including the FIRST-RUN FORK: new setup vs restore
                       from a v4 backup (import spec §3a)
           7. importer (E) — its own module, one-directional, with a retirement rule
         Users arrive when it is ready, install alongside, and retire v4 themselves.
```

**Note what step 5 does *not* include.** An earlier draft had the band vocabulary "unified" here.
It is not: v5 keeps all four labels (house `day`/`night`, EV `peak`/`off_peak`), because the two
axes are different determinations — clock versus dispatch — not two names for cheap and expensive.
The only change is a **house `off_peak → night` relabel, SMB era only**, and it belongs to **step 7,
in the importer**, as a declarative column rule. See [EMT-v5_schema_notes.md](EMT-v5_schema_notes.md)
§3a and the import spec §5.2.

Step 5 is therefore a pure *column* change: bands stop having a block-level home and live only in
`block_segments`, read alongside the segment's attribution.

### Why a verbatim copy

Copy as **commit one**, followed by deconstruction — not copy-and-leave-it:

- **The suite runs from the first commit and stays green through every step.** In a module-by-module
  port it cannot run until the port completes, so the refactoring happens blind and integration is
  where you learn. Here, all 2,711 tests are the safety net continuously.
- **The parity harness becomes optional rather than essential.** The suite *is* the parity check.
  (A harness is still worth having for cross-surface reconciliation against real databases — but it
  stops being a prerequisite.)
- **Every refactor is a diff against known-working code**, reviewable as such.

### Alternatives rejected

**Refactoring in place across 4.5.14–.17** rides users through the whole restructuring. A–C touch the storage boundary, the API and
the entire presentation layer; shipping that as four production releases exposes every user to every
intermediate state. **The premise of the whole plan is that users stay on something stable while the
kinks are ironed out.** The schema-neutrality of A and C makes in-place look attractive; it is not
the constraint that matters.

**Porting module by module** cannot run the test suite until the port completes, so the refactoring
happens blind and integration is where the divergence is discovered.

### The divergence rule

A copy means a fix can land twice. That needs a stated rule, not good intentions:

> **After 4.5.14, v4 accepts critical fixes only — data-loss, crash-on-start, or a pricing error. No
> features, no refactors. Every such fix is cherry-picked into EMT the same day it lands in v4.**

**A critical fix that repairs already-written data raises v4's `heal_level` high-water mark**, and a
pricing-error fix is exactly that kind. So the rule has a second clause:

> **Any v4 fix that adds a heal also bumps EMT's `REQUIRED_HEAL_LEVEL`, in the same cherry-pick.**

Otherwise the importer keeps admitting backups that v4 now knows are unrepaired, and v5 has no heals
to fix them on arrival. This is why .14 is the *earliest* source floor rather than the floor: the
actual bar is wherever v4 stops, and it is pinned before v5's first release (import spec §1.1, Q9).

Without it the two drift until reconciling them is its own project. With it, divergence stays a
handful of small commits that apply cleanly, because EMT's early history is v4's history.

### A side benefit worth exploiting

EMT's schema only diverges from v4's at step 5. For the whole of steps 1–4, an EMT build can read a
v4 database almost directly — so the migration path is testable from very early, long before the
importer is written. Use that: run EMT against a copy of a real v4 database at every step and
compare surfaces. It is the cheapest parity signal available and it exists for free until the schema
moves.

## 7. Scale of the job, honestly

```
carried substantially as-is   ~21,000 lines   block_store + engine + kraken_* + friends
consolidated                   ~9,000 lines   server.py's 126 routes
rescued then demolished         ~5,400 lines   energy_charts.py (1,086 survive as billing.py)
deleted outright                  ~700 lines   reprice.py + 85 unreachable names
replaced entirely              ~19,000 lines   templates → a client
```

The domain — the expensive part, three years of tariff edge cases — is carried. The restructuring is
concentrated in presentation and the seam between domain and presentation.

---

## 8. Open questions

1. **What is the client?** **Defaulted 19 Sep 2026: server-routed pages, no framework.** Revisit
   only against the trigger below.

   *Why this stopped being a big question.* It was listed as "the only genuinely unscoped layer",
   which overstated it. Both options — pages or a single-page app — fetch from the same API over a
   viewport range, and neither pre-renders anything. **The choice has no bearing on the
   write-triggers-wholesale-recompute fix, the fingerprint, or the API shape**; all of that is
   settled in C regardless. It decides one thing only: whether clicking a nav link reloads the
   browser document or swaps it in JavaScript.

   *Why pages is the default.* Closest to v4's 22-page shape, so the client is the layer that
   changes least. No bundler and no build step. And EMT runs inside **Home Assistant's ingress
   iframe**, where path rewriting has already caused trouble once — the 4.5.13 health-panel demo
   picker had to move in-page because a URL parameter could not reach the iframe. SPA routing under
   ingress inherits that whole class of problem for a benefit we have not yet shown we need.

   *The one trigger to revisit:* if carrying **date range and meter selection across navigation**
   turns out to be annoying enough to pay for tooling. That is the SPA's real advantage here — state
   survives a screen change — and it is a judgement to make once the surfaces exist, not before.
2. ~~**Where does `billing.py` live in v4's lifetime?**~~ **Closed by §6.** It is extracted in the
   EMT repo, as deconstruction step 2, with the copied suite green throughout. Extracting it in v4
   would have meant another release for users to absorb, which §6 rejects.
3. ~~**Read snapshot lifetime.**~~ **Closed — and it was a duplicate.** The same question was
   recorded here and in [A §7.1](EMT-v5_A_storage_design.md); the decision now lives there only, so
   it cannot be answered twice differently. **`ReadStore` ends its read transaction between
   requests**, so every request takes a fresh snapshot.
4. ~~**Does the parity harness come first?**~~ **Closed by §6: no.** The copied suite is the primary
   check from commit one, which downgrades the harness from prerequisite to useful. Still worth
   building for cross-surface reconciliation against real databases, and §6's side benefit (EMT
   reads a v4 database directly until step 5) makes it cheap to run continuously — but nothing waits
   on it.