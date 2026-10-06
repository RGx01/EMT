# Roadmap

*Workstream status and the open backlog. The agreed picture of what v5 is made of is
[design/EMT-v5_architecture.md](design/EMT-v5_architecture.md); the reasoning behind the ordering is
in [design/EMT-v5_programme.md](design/EMT-v5_programme.md) — this file tracks state only.*

**Nothing here has shipped.** EMT is pre-release.

**How this gets built** ([architecture §6](design/EMT-v5_architecture.md)): v4 shipped **4.5.14** and
is frozen to **critical fixes only** (data loss, crash-on-start, a pricing error), each ported here as
its forward fix. This repository was seeded with a verbatim copy of 4.5.14 (suite **2,764** green at
the seed, 21 Sep 2026; **3,605** on 4 Oct 2026) and the workstreams below deconstruct it. v4 is at
**4.5.20**; the import floor is **4.5.19** (E).

*Refreshed 4 Oct 2026.*

| # | Workstream | Depends on | State |
|---|---|---|---|
| A | Storage and access model | — | ✅ closed 30 Sep 2026; A.1 closed with a caveat 5 Oct 2026 — a ~1-min burst after a restore |
| B | Job coordinator (v4 BL-50) | A | ✅ all six acceptances met — 6 on the Pi, 5 Oct 2026 |
| C | One API for every surface | A | ✅ acceptance rows 1–8 met (C design §0) |
| D | Surfacing performance | A, C | 🟢 17 of 17 surfaces: time bounded on the Pi (#64), queries asserted in CI |
| E | Importer | A–D | 📝 spec v12, floor 4.5.19 |
| F | Carried from v4's backlog | — | 📋 9 items open, 1 done (BL-50 = B) |
| G | Local tariff store | A, B | 📝 proposed 4 Oct 2026 |
| H | Before going public | — | 📋 1 item (BL-77), deferred until EMT goes to `main` |

---

## A. Storage and access model

*The root. One writer owned by the engine; read-only connections per request.*
Design: [design/EMT-v5_A_storage_design.md](design/EMT-v5_A_storage_design.md).

Not "build the mechanism" — v4 built it three times. **4% of call sites use it**, and 41 GET routes
still read on the engine's writer handle. A is about making the unsafe path hard to reach.

Generalises a fix v4 already made once under duress — `_generate_charts_offloaded` has its own
read-only `BlockStore` because of a cross-thread SQLite segfault — and applies it to every reader
rather than to the chart renderer alone.

**Done when:** a soak hammering every read surface while the engine writes shows no torn rows and
no read blocked behind a write.

**Done 30 Sep 2026.** Five Pi soaks (A design §5e): no torn rows, 0 SQLite faults, no read
blocked in SQLite, no GET waiting on the engine loop for a database read. What they left is §A.1.

### A.1 Engine writes stall on the SD card

*Not a read-path problem, so not A's — but found by A's soak, and it has a user-visible cost.*

On the Pi's SD card, the engine loop still stalls 10–53 s when several things write at once, and
every stall has the engine thread inside a **commit** (fifth soak: 80 over 1 s, worst 52.7 s, 69 in
`set_block_segments` during the re-price sweep). One stall dropped the HA WebSocket. Candidates,
largest first:

- ~~**The pre-rendered chart files**~~ — deleted by C (30 Sep 2026, #23). The sixth soak
  (4.5.16-a20, same restore and sweep) had no render at all and a worst stall of **22.3 s**, down
  from 52.7 s: 83 slow commits, 74 in `set_block_segments` during the sweep, none with a WAL
  checkpoint inside; one was followed by an HA WebSocket reconnect (C design §5).
- **The pre-restore safety zip**, written while the restored engine starts — it overlapped the
  worst commit. Now one step (#56, #58): 52 s in the twelfth soak, against 3 min 50 s restarting.
- ~~**`_backup_to_share` after every block finalise**~~ — daily since #68 (#67, 4 Oct 2026). Its
  ~80 s windows held 18 of a night's 25 loop-lag warnings.
- ~~**The carbon tick**~~ — #66 (#65): a blocking API fetch on the loop and ~190 single-row
  commits every 15 minutes; lag followed 100% of idle ticks before, 13% after (those overlapped
  the share copy above).
- **Commits on the engine thread at all.** If the above are not enough, route engine writes
  through a writer thread so the loop never waits on the device. An architecture change, and
  deliberately last.

**WAL checkpoint starvation — measured in the eighth soak (4.5.20-a21, 3 Oct 2026).** SQLite can
only reset the WAL when no reader is using it. The soak's two workers kept a read open almost
continuously, and its slowest requests ran 13–29 s (the all-days load, a full year's bill), so the
checkpoint never got its gap:

- the WAL grew from ~25 MB at 14:08 to **343 MB** by the start of the quiet phase (14:13). A
  checkpoint at 14:11 copied 51,369 of 57,961 pages and could not truncate ("a reader holds an
  open transaction");
- it stayed at **~346 MB** for the whole 30-minute quiet phase, and dropped to **4 MB** at 14:50,
  once the soak stopped;
- every quiet-phase read went through it. Requests whose code had not changed since the seventh
  soak ran **1.3–1.9× slower** (Live Power cards 0.56 → 1.08 s p50, picker totals 0.84 → 1.56 s);
- commits under it were slow too: `save_current_block` 20.5 s at 14:32, WAL 346 MB.

The seventh soak's WAL peaked at 115 MB and kept draining: its slowest surface then took ~6 s at the median (a partial year's bill).
A household is unlikely to cause this, but overlapping long reads would, for example a dashboard
card refreshing while someone opens year views. Candidates: make the long reads short (#47, #48),
and/or have the engine force a checkpoint (`wal_checkpoint(RESTART)`) after a sweep, before readers
pile on. **The soak now waits for the WAL to drain** after the sweep (a settle phase), samples it
through the quiet phase, and warns when it starves, so its budgets measure normal use.

**Twelfth soak (4 Oct 2026, after #66 and #68):** the quiet phase **passes** loop blocking for the
first time (worst lag 1.46 s). The whole run still fails, on the re-price sweep alone: worst lag
12.9 s and worst commit 12.3 s, in `set_block_segments` (72.6 s and 48.4 s that morning, before the
share copy went). What is left is the sweep's own commits — candidates: one transaction per chunk,
as #66 did for carbon; then the writer thread, last.

**Fifteenth soak (5 Oct 2026):** the quiet phase passes again — one over-budget lag, a 4.8 s
log-pipe flush, and no slow commit (the dispatch poll's commits went with #91). The sweep still
fails: 85 of the run's 97 slow commits are `set_block_segments`, worst 16.8 s. *Then, the same day:*
the sweep commits **once per chunk** — each block's segment write had been its own transaction,
and the chunk's split, exc and cursor three more (63 commits for a 60-block chunk; now at most 3).
The writer thread stays last, pending a soak.

**Sixteenth soak (5 Oct 2026, after #95) — the cheap half is done, and was not enough.** The
sweep took **1 min 16 s** (about 7 min in soak 15); over-budget loop lags fell from 59 to **10**,
slow commits from 97 to **11**, and the quiet phase passed every surface. But the run still fails:

- inside the sweep, each chunk's single `batch` commit took 2–5.5 s, and a lag of the same length
  followed each (7 of them);
- the worst lag, 18.3 s at 10:30:37, is not the sweep at all: the half-hour finalise's
  `save_current_block` commit took 10.4 s and `_insert_block_rows` 8.2 s, both single small
  commits. Soak 13 had the same (18.8 s at 11:00).

The database runs WAL with `synchronous = NORMAL`, so an ordinary commit does not fsync; only a
checkpoint does, and 10 of the 11 slow commits had none inside. The time is therefore most likely
the commit's own `write()` blocking while the card drains dirty pages — Home Assistant's recorder
writes to the same card. Fewer commits cannot fix a single commit that takes 10 s; keeping the loop
from waiting on one can. **Next: the writer thread** —
[design/EMT-v5_A1_writer_thread_design.md](design/EMT-v5_A1_writer_thread_design.md).

**Seventeenth soak (5 Oct 2026) — the design's step 0, with the Pi's second EMT instance (v4
4.5.20 dev) stopped.** The quiet phase had **no** over-budget lag, finalise included (the 12:00
finalise made no slow commit); soak 16's worst, 18.3 s at 10:30:37, fell on a half-hour boundary,
when the other instance finalises too. Over the whole run, 4 over-budget lags (10 in soak 16) and
8 slow commits (11), worst 7.7 s — **all within 47 s after the restore**, where the sweep's chunk
commits met startup's own writes: the first Kraken poll's BL-8 outage backfill
(`create_backfill_block`, 7.7 s), its settlement (`upsert_kraken_block`, 3.2 s) and the carbon
backfill. *Then:* the sweep holds its first pass until startup has finished, no poll runs and 60 s
have passed (capped at 5 min). Two of the sweep's own chunk commits (3.0 s, 5.1 s) ran with nothing
beside them, so the hold is not expected to pass the run on its own; it removes the overlap. **Two
EMT instances on one SD card** — a v4 and a v5 side by side — is a known cost: their half-hour
finalises commit together.

**Eighteenth soak (5 Oct 2026, after #97, v4 stopped) — closed with a caveat.** The sweep held
61 s for startup's own writes, then ran in 49 s. Over-budget lags 5 (4 in soak 17), worst **5.8 s**
(11.2 s), slow commits 6; the quiet phase, the 13:30 finalise included, had none. Each that remained
was **one commit with nothing beside it**: the first poll's outage backfill
(`_insert_block_rows_replace`, 5.75 s, during the hold) and four of the sweep's chunk commits
(1.4–4.4 s). There is nothing left to batch or to separate.

**Closed 5 Oct 2026, by decision, with this caveat recorded:** on SD storage, for about a minute
after a restore or an upgrade, single commits take 2–6 s and the engine loop waits on each; normal
running is clean. The original criterion — no stall over 2 s across the whole run — is not met.
The fix for the remainder is the writer thread, designed and not built
([design/EMT-v5_A1_writer_thread_design.md](design/EMT-v5_A1_writer_thread_design.md)); reopen if
real use shows the burst matters, or when HA's recorder sharing a card is shown to bring stalls into
normal running. Known cost, also recorded: **two EMT instances on one SD card** commit their
half-hour finalises together (soak 16's 18.3 s stall).

## B. Job coordinator

*One coordinator for user-mutating jobs; deny-if-busy with idempotent retry.* Carried from v4's
BL-50. Design: [design/EMT-v5_B_coordinator_design.md](design/EMT-v5_B_coordinator_design.md).

**State, 4 Oct 2026.** Membership **16 of 17** (only `api_db_vacuum`, which never joins); **6 of the 8**
members that declare an edge consult `admission()`, and the other two (`delete`, `restore`)
pre-empt the import. Acceptances (B §5):

| # | acceptance | state |
|---|---|---|
| 1 | every writing job is a member | ✅ ratchet at 1 (`api_db_vacuum`) — #69 |
| 2 | no unregistered write from a request thread | ✅ pinned to `api_db_vacuum` — #75 |
| 3 | declared dependencies honoured | ✅ a test per edge — #70, #71, #72 |
| 4 | a claim before any slow work | ✅ the API import claimed before its backup — #73 |
| 5 | admission never per block | ✅ nine per-block writers, no await or admission — #74 |
| 6 | Pi soak, members concurrent | ✅ soak 15, 5 Oct 2026 — every edge held (B §5) |

**Acceptance 6, met 5 Oct 2026:** the soak database restored (starting the sweep), then a bounded
gap fill and a one-day range delete started mid-sweep. The sweep deferred to each in the same
second and was re-admitted when each finished; `scan-log` coordinator PASS, no refusal left
standing. Two cosmetic findings: the sweep status labels a restore-armed sweep `upgrade`, and the
dispatch poll logs "completed energy on N slot(s)" for slots it no longer writes.

*Fixed 4 Oct 2026:* a restart no longer loses a deferred, paused or mid-chunk verify pass — the
tick resumes it from its persisted cursor, once per database; and a pass **paused for API
allowance** (120 waits, ~2 h), which kept its `verify_pricing` claim and so held attribution until
a restart, is retried from its cursor after `_VERIFY_PAUSE_RETRY_S` (1 h). *(`/api/historical/api-import/apply`, which
ran the import without claiming it, was deleted 4 Oct 2026: no UI had ever called it.)*

**Done 5 Oct 2026.** What remains of B is the open questions in B §6, none blocking C.

## C. One API for every surface

*Break the write → wholesale-recompute coupling. No pre-generated HTML.*
Design: [design/EMT-v5_C_surfaces_design.md](design/EMT-v5_C_surfaces_design.md).

**State:** acceptance rows 1–8 met (C design §0) — no pre-generated artefacts and `CHART_DIR` gone,
every surface on `/api/energy`, no defaulted tariff context, billing reconciled with v4 to the
penny (with v4's sub-meter rounding explained, #63), and every surface bounded on the Pi.

**#31 — fixed 5 Oct 2026.** The hybrid EV fold capped each bucket's move at its own Direct, so a
day let one slot's surplus cover another's shortfall and moved more EV than its half-hours summed
(36 of 60 days on a production-like database). Coarser buckets now fold per slot, as the HH view
does, and sum: over 1,216 days a day equals its slots, and no day total moved.

**C §7.3 — closed 5 Oct 2026.** `/api/energy` caps a request at 31 days of blocks and 366 days
for a period total; by-day is uncapped. Only `kind=range` (and `kind=all` for a period) could
exceed either, and no page does.

**Direct import — one rule, 6 Oct 2026.** Three surfaces showed the house's share of the main
meter, and each worked it out its own way: the bill floored it each half-hour, the Overview cards
over the whole window, Usage Stats in kWh per half-hour and in £ per day. Where devices out-read the
main (fixture `e7_over`) the same period read 87.64 kWh / £22.63, 8.96 kWh / £13.57 and
87.64 kWh / £17.43. `direct_import.py` is now the rule, in Python and in SQL. All three use it and
show a "Devices beyond the metered supply" line when it is needed, so the breakdown adds up to the
meter. `tests/test_direct_import.py` asks every surface for the same periods on every tariff fixture
and fails if they disagree. On two real databases this also corrected days where a stored remainder
had outlived its device's rows.

**Open — two Direct readers not yet on the rule.** (1) Insights usage (`_aggregate_usage`) floors
house = main − devices − EV once per period, through its own segment re-pricing. (2) The IOG hybrid
EV: the bill carves EV out of Direct per rate while the Overview counts it as a device. They agree on
every fixture, but no fixture is IOG, so that agreement is unasserted. Both belong in
`test_direct_import.py` when they move.

## D. Surfacing performance

*Measure before optimising.* Target hardware is a Raspberry Pi 4 class box on SD storage.

**State:** `.build/soak.py` names 17 surfaces and judges each against `.build/soak_budgets.json`,
all 17 set from the tenth soak (#64). #46, #47 and #48 took the slowest: a year's bill **18.2 →
4.8 s**, the all-days load **11.6 → 2.8 s** (p50, Pi, under the soak's load).

**Queries per surface, asserted in CI (5 Oct 2026).** `.build/query_budgets.py` requests every soak
surface against a synthetic database (`tests/surface_fixture.py`: a main meter, an EV charger and a
battery) at 40 and 200 days of history, and counts the SQL statements each issues;
`tests/test_surface_query_budgets.py` asserts each stays within `.build/query_budgets.json` (9–31
statements) and is the **same at both lengths** — no query per day, block or period. Measured on a
production-like database too: the same shape (a month's, a quarter's and a year's bill all issue 27).
An N+1 injected into the all-days load is caught. A statement count is deterministic, so CI asserts it
exactly; time is not, so **time to first byte stays Pi-asserted** by the soak (17 budgets).

**Done when:** time-to-first-byte and queries-per-surface are instrumented, with a budget per
surface that CI can assert. *(Queries: CI-asserted. Time: Pi-asserted by the soak — a CI runner's
timings say nothing about a Pi on SD storage.)*

## E. Importer

*Ingest a backup at or above the floor version, currently **4.5.19**. Spec at
[design/EMT-v5_import_spec.md](design/EMT-v5_import_spec.md).*

Three things the importer needs from a source. **Two are new in 4.5.14; one already exists** — an
earlier draft of BL-73 proposed adding a duplicate of that one, before the 4.5.13 schema was
checked. What genuinely cannot be added later is a *new* marker, because a backup already written
can never be annotated:

| marker | from | why v5 needs it |
|---|---|---|
| `written_by` | BL-72 | the app version that last opened the database. **The §3 gate.** |
| `supplier` | **BL-73** | the normalised supplier registry key (`octopus` / `not-listed` / absent). **Not optional after all** — v4 is *not* Octopus-only: `not-listed` is a supported local-metering state, so it cannot be derived from `written_by`. |
| *account stamp* | **already exists** | `kraken_state.kraken_account_number`, with `get_db_account()` and `kraken_account_mismatch()` already implemented. No new field. |

EMT keeps the marker-gated heal mechanism and starts its own heal set from an empty one, and stamps
its **own** release version on the imported database — so "last opened by X means healed to X's
standard" keeps holding on the far side of the migration.

**The floor is the last release of v4 — not necessarily 4.5.14** (spec v6, 19 Sep 2026). 4.5.14 is
only the *earliest* a source can be, because it is the first release that stamps its version. v4 is
frozen to critical fixes after .14, and a fix that repairs already-written data adds a heal, so the
floor may end at .15, .17, wherever v4 actually stops. It is pinned when v5 is written.
**Currently 4.5.19** (spec v12, 1 Oct 2026): 4.5.15 carried #473's heal, 4.5.16 #481's, 4.5.17 the
VAT backdate heal (1 Oct 2026's 0% VAT), 4.5.18 that heal re-armed after 4.5.17's learner undid it, 4.5.19 re-armed again for sub-meter rows.
4.5.20 (2 Oct 2026, the gap-fill zero-opener fix, v4 #494) carries no heal, so the floor does not move.

Why a version suffices: every heal is marker-gated and an ungated marker runs at startup, so
upgrading any older database runs every heal that release carries. **"Last opened by X" means
"healed to X's standard."** *An earlier design proposed a computed `heal_level` integer; dropped
19 Sep 2026 as unjustified — heals are historical, so a later release that adds one simply raises
the floor.*

Because only 4.5.14+ stamps a version at all, the importer supports **one schema era**: the
4.4.0–4.5.4 era (segments present, settled provenance entirely absent) cannot reach it. Three gates
collapse into one, the era-detection code is never written, and 4.5.12/4.5.13 databases become
**negative** fixtures — near-misses with every table the importer wants and no attestation.

**Gate verified against a real 4.5.14 database** (prod-dev, 21 Sep 2026): `written_by='4.5.14'`,
`supplier='octopus'`, account stamped — admitted, restore path prompts for an Octopus key. That is
the first genuine *positive* fixture; keep it. Two failure modes it exposed are now §9 gates: values
in `store_meta` are **JSON-encoded** (the column holds `'"4.5.14"'`, so a plain SELECT-and-compare
finds no marker on a perfectly good database), and versions must compare **numerically** — under
string comparison v5's own CalVer `2026.9.0` sorts *below* the v4 floor.

**Done when** (in addition to the spec's §9 gates):

- `FLOOR_VERSION` exists as a **single named constant**, no version literal anywhere in the gate or
  its messages.
- Versions compare **numerically**, never as strings — `"4.5.9" > "4.5.13"` lexically — and v4's
  `4.5.x` orders below v5's CalVer `2026.x.y`.
- The below-the-floor test **generates** its fixture by rewriting the positive one's `written_by`,
  so it keeps testing something after the floor moves.

**Enforced at restore since 4 Oct 2026** (`import_gate.py`): gates 2 (the floor) and 4 (units) run
on the candidate file before anything is stopped, backed up or swapped, on every path that replaces
the live database — a backup zip, the flat share copy, an upload, and `/api/import/from-zip`. Until
then `written_by` was stamped and never read, so a restore took any v4 backup. Gates 1 (live copy +
integrity) and 3 (supplier) followed the same day; all seven admissible databases on hand pass all
four, and `quick_check` + `foreign_key_check` take under 0.5 s on the largest (79 MB).

> ⚠️ **Release-day checklist item, easily missed.** `FLOOR_VERSION` is pinned as the last step
> before v5's first release, from whatever v4 has actually reached by then, and the positive fixture
> is regenerated at that version — not carried forward from development. Until then it is a
> placeholder, and every post-.14 v4 fix that adds a heal bumps it in the same cherry-pick
> ([architecture §6](design/EMT-v5_architecture.md)).

Last deliberately: its contract is "write the EMT schema", so it cannot be finished until A–D have
settled what that schema is.

---

## F. Carried from v4's backlog

*v4's open backlog (its `docs/ROADMAP.md`, 4 Oct 2026), assessed against v5's code. v4 takes
critical fixes only, so everything below is v5 work; the v4 entries point here. Each keeps its v4
number. Order is the agreed one — deletions first, while they are cheapest.*

| # | Item | Kind | State in v5 (checked 4 Oct 2026) |
|---|---|---|---|
| — | **BL-50** — unify user-job mutual exclusion | architecture | ✅ **is workstream B** |
| 1 | **BL-33** — remove the one-time legacy migrations | deletion | ✅ **done 4 Oct 2026** — #78 (below) |
| 2 | **BL-61 (1)** — remove the SMB rate-repair migration | deletion | ✅ **already done, 28 Sep 2026** (1156508; guarded by `tests/test_v4_heals_removed.py`) — §F's first draft said otherwise without checking |
| 3 | **BL-63** — two pricing models: adopt the live one | deletion | ✅ **deletion done 4 Oct 2026** — the rounding residue is still open (below) |
| 4 | **BL-74** — retire the `needs_review` writes nothing displays | deletion + fix | ✅ **done 4 Oct 2026** — all of it, and the review list with it (below) |
| 5 | **BL-61 (5)** — rename `rate_source` `'measured'` → `'settled'` | schema | with step 5; the importer maps the value |
| 6 | **BL-76** — proportional device grid split | correctness | forward rule; history re-derived by the importer |
| 7 | **BL-75** — settlement visibility | correctness | all six parts apply; split it |
| 8 | **BL-47** — auto-attribution after a gap fill | feature | attribution is a coordinator member now |
| 9 | **BL-60** — block inspector | feature | a per-block endpoint on C's API |
| 10 | **BL-64** — bill-parser fixtures + a pypdf bump gate | tooling | ports as is |

**Deletions (1–4).** The floor (E) is what makes them safe: every database v5 admits was opened by
4.5.19, so every one-time migration and heal in that range has already run. They go under the
deletion protocol ([architecture §4a](design/EMT-v5_architecture.md)), and two checks come first:

- **Done-markers on real databases.** Confirm each migration's marker reads done on the
  production-like and dev databases before deleting its path; the segment backfill in particular
  may share code with the live sweep.
- **Reachability, re-run.** BL-63's first count ("9 of 11 `pricing_segments` functions dead") was
  wrong in the direction that deletes working code — 8 are live. Trace from real entry points,
  over-approximating, not by grep. Part of BL-63's rounding residue is already fixed: the
  reconcile now compares bands by nearness, not price equality (#60); other exact-equality
  comparisons against a canonical rate may remain.

**BL-33 — done, 4 Oct 2026 (#78).** Three removals, one commit each, each with its evidence:

1. **The three legacy historical backfills** (ex-VAT, IOG-split, segment), their schedulers,
   `_net_alarm` and `_reprice_sweep_done` — unreachable, and `engine_startup` called them a "dormant
   net, to be deleted a release later". 524 lines of `engine.py`. The house/EV split for a late
   completed dispatch is unaffected: that is `run_iog_split_carve` and `_attribute_missing_ev_split`.
2. **The one-off stale ex-VAT repair** — reachable but provably done: v4 4.5.14 runs it at startup at
   the same scope, and all 108 databases on hand stamped 4.5.14+ read done.
3. **The sweep's pence repair**, paired with a new **import gate** (import spec §3 phase 1, gate 4):
   refuse a source when any priced main block's (inc ÷ exc) ÷ (1 + VAT on its date) exceeds 10.
   Healthy blocks measure 1.000 (worst 4.8%); a pence block ~100. The write-point unit guard
   (`_sanitise_inc_units`) stays.

**`run_settled_ev_split_heal` — traced, kept.** Not a one-off: no marker, runs every dispatch-capture
tick, and its candidate set is empty on every real database checked. The trace found a defect instead:
on the 12th Pi soak it "restored" the same 20–23 blocks every hour, changing nothing — where the
dispatch ceiling is a known zero the settle keeps a block house-only (measured is king), and the heal
re-settled it each pass. Fixed in the same PR as this note: the heal asks the settle's own rule
(`_measured_ev_kwh`) first, and counts only a split actually written.

**BL-63 — the deletion done, 4 Oct 2026.** Option 2, adopt the live model, in three commits:
`reprice.py` and its test consumers (876 lines, including the adapter differential that existed to
judge option 1 and a fixture generated through `reprice_block`); `carbon_from_reprice` and
`house_carbon`; and `pricing_segments`' three unreachable builders, **moved** to
`tests/segment_helpers.py` rather than deleted, because tests of the eight live projections build
their segments with them. **`ev_carbon` is live** (`period_ev_saving` calls it) — the architecture's
module table had listed it for deletion; corrected. **Still open — the rounding residue:** derived
rates are back-computed from a 6-dp rounded cost rather than carrying the canonical band rate, so an
exact-equality comparison against a canonical rate can be defeated. #60 fixed the one that caused
BL-62 (the reconcile's band test); a sweep for the others remains.

**BL-74 — done 4 Oct 2026, wider than v4 scoped it.** Nothing in v5 sets or clears `needs_review`
now: the rogue-block clamps stop flagging (the clamps and warnings stay); settlement reports drift
for the poll log and no longer flags it (the old defect: a cost-settled block re-flagged on every
poll); and the dispatch reconcile's **review list goes too** — an ambiguous band is decided by cost
settlement when Octopus bills the block, so the Corrections page's "Flagged for review" card, its
banner, `/api/review-blocks` and the store's review methods are deleted. The three startup
self-heals (#307 spike, register dip-and-recover, BL-19 grid invariant) are deleted on the floor
argument — their markers are set in every admissible database — with their write-point guards kept.
**No stored data changed:** the review columns, their index and every row are left as they were.
*Fixed after, 4 Oct 2026:* `append_block_replace` now carries the review columns, and
`finalised_from_cad` and `derivation_id`, from the stored row — a round-trip rewrite reset all five.

**BL-61 (1)–(4) — re-checked 4 Oct 2026.** (1) went on 28 Sep with the other v4 one-off heals
(`run_smb_rate_repair`, `run_smb_device_recost`, `run_smb_ev_resplit`, `run_band_rate_snap`), on the
import-floor argument `test_v4_heals_removed.py` records. The rest are not floor-safe deletions:

- **(2) the `chart_emit` house-TOU override is live** — the Billing tab's day charts reach
  `chart_emit.day_rate_series(…, house_tou=…)` through `energy_charts.day_chart_data`. BL-61's case
  for retiring it is that the stored idle-slot rate is already the clean rate, so the override only
  re-affirms it. That is a surface change no test covers, so it needs a before/after of day charts on
  a real database (architecture §4a) before the override goes.
- **(3) the single charting API** is partly C's: every surface reads `/api/energy`, but day charts
  still draw their rate lines through `chart_emit`.
- **(4) the import-recovery ladder** is still there (`lookahead_ladder` in
  `kraken_api_client.recover_measurement_costs`). Collapsing it to a plain small-window cost fetch
  changes what is fetched from Octopus, so it can only be proved against the live API.

**BL-61 (5) — the rename.** Deferred in v4 because it forces a data migration of every `'measured'`
row. In v5 it belongs to schema step 5, and the importer writes `'settled'` from a v4 `'measured'` —
nothing in v4 migrates. BL-61's (2)–(4) (the `chart_emit` house-TOU override, the single charting
API, the import-recovery `lookahead_ladder`) are re-checked against what C left: `chart_emit.py` still
has the `house_tou` override path — whether anything still reaches it is unchecked.

**BL-76 — proportional split.** `_apply_pass2` gives each device after the EV
`grid_remaining × (its kWh / Σ kWh)` instead of the greedy ranked draw — bounded, order-independent,
and bill-neutral (Σ device cost is unchanged). The EV pre-allocation stays. What v4 would have needed
as a heal becomes the importer re-deriving `imp_kwh_grid` from stored `imp_kwh` — no v4 heal, no floor
change. Delete the dead `unprotected` branch and decide on `kwh_battery` (computed, discarded) in the
same change.

**BL-75 — settlement visibility.** Split into: (b) widen the cost fetch beyond IOG/INTELLI (still
gated at `measure_settled_dispatched_blocks`) and keep the agreement floor; (c) a per-slot attempt
count with backoff and a terminal state; (a) the pill reporting both frontiers; (e)/(f) the
corrections tool as last resort and retry from the pill. **(d) needs a decision:** stop stamping
`finalised_from_cad` automatically past the horizon (`finalise_past_horizon_blocks`), and stamp it only
on the user's acceptance that the usage is unrecoverable.

**BL-47, BL-60, BL-64** — as time allows. BL-60's design is in the v4 repository
(`docs/design/EMT-BL60_block_inspector_pre_post_settlement.md`). BL-64 needs real bills to build the
redacted fixtures, and a redaction check that fails loudly; v5 already pins pypdf 6.18.0.

---

## G. Local tariff store

*Proposed 4 Oct 2026. Not designed yet — this records the problem, the shape agreed in
conversation, and what it must handle.*

**The problem.** EMT holds no copy of the tariff. Unit rates and standing charges are fetched from
the Octopus REST products API and kept **in memory only** (`_kraken_rate_schedules`, refreshed each
6-hour poll cycle and on connect), and so is the agreement history (`_kraken_discovery`; only the
account number is persisted). So:

- **an add-on restart while the API is unavailable leaves EMT with no prices** — the resolver returns
  nothing, and new blocks are written at a zero rate and flagged for repair;
- **there is no record of what a block was priced against.** When Octopus edited the 1 Oct 2026 rate
  records in place (0% VAT), nothing could see that a rate EMT had already used had changed — which
  is why v4 needed heals (4.5.17–4.5.19) rather than a re-price.

**The shape.** Two tables — tariff **agreements** (account, MPAN, direction, tariff and product
code, valid from/to) and tariff **rates** (tariff code, unit or standing, band, valid from/to, inc
and ex VAT) — each row carrying when it was fetched and what replaced it.

- **A cache of the supplier's truth, never a second truth.** Each refresh *replaces* the account's
  agreements and rates wholesale with what the API now says; the previous version is kept as history
  (fetched-at, superseded-at), not used for pricing.
- **Re-price when a rate changes.** Each refresh diffs the new version against the old. A changed
  rate record, a changed agreement window, or an agreement that disappeared gives an exact span; the
  blocks in it go on the existing re-price queue. This is what "Octopus edited it in place" needed,
  and nothing does it today.
- **A customer who backs out of an agreement** (cooling-off, a reversed switch) is the same case: the
  next refresh no longer contains it, the diff marks its span, and the span re-prices on whatever
  agreement Octopus now reports. Settled blocks keep the bill's figures (`rate_source`), as now.
- **Cadence.** Daily is enough — tariffs change rarely, agreements more rarely. The 6-hourly poll
  already fetches the rates, so persisting them costs no extra API calls. The refresh is a
  coordinator member (B), so it does not overlap an import or a delete.
- **Offline first.** Pricing reads the local copy; the API refreshes it. With the API down, EMT keeps
  pricing from the last good copy instead of writing zero rates.

**The importer (E).** A v4 database has no tariff tables. The importer fills them on the first
refresh after the user authenticates; until then the in-file checks apply (each block's own ex-VAT
figure — see BL-33 in §F). With them, a post-authentication gate can check stored rates against the
canonical published rate per slot.

**Also uses it.** The live write guard (`_sanitise_inc_units`, today a fixed £3/kWh ceiling) can compare
against the slot's canonical rate from the local copy, with the API down too.

**Watching for Kraken API changes.** GraphQL deprecations are covered: the client introspects the live
schema and warns (`check_field_deprecations`), with `.build/check_graphql_deprecations.py`, the field
watch baseline and their tests. **The REST products API — where the rates come from — has no schema to
introspect, so a REST deprecation would not be caught.** The store gives that a natural check: a
refresh that suddenly returns nothing, or a different shape, for a tariff that had rates yesterday is a
signal worth surfacing rather than a reason to clear the copy.

### G.1 Eras — future idea

*Recorded 4 Oct 2026, not planned.* Historical tariff agreements, stored locally, are eras: a span with
a tariff, a start and an end. Combined with config periods (which already split history) and
**user-defined periods** (a new heat pump, a house move, a change of habit), they give a set of eras to
compare — usage, cost and carbon per era, and like-for-like comparisons across them — as new Insights
and Usage Stats views, with no API call. The tariff store's schema should allow for this from the
start: agreements addressable as periods, and a user-periods table alongside.

---

## H. Before going public

*Items deliberately held until EMT is pushed to `main` and made public — nothing here is urgent.*

### BL-77 — Kraken schema watch: review the 5 Oct 2026 additions, and make the watch run for v5

**What happened.** v4's weekly GraphQL check failed on 5 Oct 2026 (run 37318049738, schedule on
`main`). The *deprecation* check passed; the **field watch** (`check_graphql_field_watch.py
--fail-on-change`) failed, as designed, on three additions — nothing removed:

| change | what it is | relevant? |
|---|---|---|
| `ContractedVolumeMeasurementPeriod` — new enum `WHOLE_PERIOD`, `DAILY`, `HOURLY` | cadence of a *contracted volume*; used only by `ContractedVolumePeriodType` and `QuoteContractedVolumePeriodType` | no — quotes/contracts (business supply) |
| `ApplicableRateType.params` — new `JSONString` | "raw parameters associated with the rate, e.g. step information for stepped rates" | unlikely — stepped tariffs |
| `TimeOfUseBandUsageType` — new `band`, `value`, `units`, `qualities` | under `TimeOfUseDevice` / `TimeOfUseDeviceRegister.timeOfUseUsage`: usage "organised into calendar periods and split by time-of-use band", with a reading-quality breakdown | **possibly** — Octopus's own per-band consumption would be an independent check on EMT's peak/off-peak split |

Introspection sees that the schema exists, not whether a domestic account may query it or whether
it is populated (the watch's own stated boundary).

**To do, when going public:**

1. **Refresh the baselines** — `check_graphql_field_watch.py --update-baseline` in v4 (a CI-tooling
   change: branch, PR, signed commits for `main`; not an app release) and in EMT (whose baseline
   is identical to v4's), so the check is green and watches from the new surface.
2. **Make the watch run for v5.** GitHub runs scheduled workflows from the default branch only;
   EMT's is `main`, and `.github/workflows/graphql-deprecations.yml` is not on it — so v5 has had
   no weekly deprecation or field watch at all. It runs once the workflow reaches `main`.
3. **Probe `timeOfUseUsage`** with an authenticated query on a real account: is it open to a
   domestic IOG account, is it populated, and do its band totals match the bill's? If so, it is a
   candidate settlement cross-check (or more) — a separate item then.

*Recorded 5 Oct 2026. Nothing in the additions is a removal or a deprecation, so there is time.*

---

## Open questions

Carried from the programme map §4 and the import spec §10. These need answers before the
workstreams that depend on them start.

- [x] **Does EMT publish anything to Home Assistant?** **No** (18 Sep 2026). EMT is read-only
      toward HA: it subscribes to sensors and creates no entities or statistics. The one namespace
      `instance.py` cannot arbitrate is one EMT never enters, so side-by-side is unconditionally safe
      on that axis. Accepted consequence: EMT's figures appear only in EMT's own UI. Reopens only if
      EMT ever starts publishing.
- [x] **Is on-demand computation cheaper than pre-rendering once?** **Withdrawn — false dichotomy**
      (18 Sep 2026). v4 does not pre-render once: the chart is a complete document inside an iframe,
      so the document is the unit of update and *any* change re-renders every year held. Scoping to
      the viewport is strictly cheaper at every size; there is nothing to trade off. See
      [C §5](design/EMT-v5_C_surfaces_design.md).
- [x] **What is the client?** **Defaulted: server-routed pages, no framework** (19 Sep 2026). The
      choice has no bearing on the API, the fingerprint, or the write-triggers-recompute fix — both
      options fetch from the same API over a viewport range and neither pre-renders anything. It
      decides only whether a nav link reloads the document. Pages keeps the client closest to v4,
      needs no bundler, and avoids SPA routing inside HA's ingress iframe. *Revisit only if* carrying
      date range and meter selection across navigation proves annoying enough to pay for tooling.
      See [architecture §8.1](design/EMT-v5_architecture.md).
- [x] **Read snapshot lifetime** — **per-request freshness**; `ReadStore` ends its read transaction
      between requests (19 Sep 2026). Was recorded in two places; now lives in
      [A §7.1](design/EMT-v5_A_storage_design.md) alone, with an acceptance test, because the
      failure is a coherent page showing stale numbers and no error anywhere. *(A)*
- [x] **How does a v4 user actually reach the importer?** **A first-run fork** (19 Sep 2026). Every
      earlier draft described what the importer does and never how it is reached, implicitly assuming
      a fresh install — the wrong default for the entire population E exists to serve. v5's first
      start now offers *new setup* or *restore from a v4 backup*, and on the restore path the backup
      drives the credential prompt rather than the reverse. Forces §3's gates to split: everything
      checkable from the file runs **before** the user is asked to authenticate.
      See [import spec §3a](design/EMT-v5_import_spec.md). *(client + E)*
- [ ] **Viewport range vs materialised period summary** — once scoped to what the user can see, is
      per-request computation cheap enough, or does the writer maintain an incremental summary?
      Benchmark on the Pi: TTFB for a month and a year, cold and warm, plus queries-per-surface.
      *(C/D)* — *Evidence so far (4 Oct 2026): per-request, a month's bill is 0.42 s and a full
      year's 4.8 s at p50 on the Pi under the soak's load (#62); no summary built yet.*
- [ ] **Test-suite portage** — wholesale (keeps the v4-equivalence proof) or rebuilt against the new
      API (suits the new shape)? Probably both, in that order. *(C)*
- [ ] **What is the first public release's bar?** "Imports my history and shows it correctly" is a
      far earlier date than "replaces v4 for daily use", and decides how much of B–D is pre-release.
- [x] **Is the `reads` table safe to drop?** **Yes — it is dead, not transient** (19 Sep 2026).
      Its five store methods have no caller outside tests; nothing has ever written a row. Goes in
      the unwired-paths inventory, not the import question list. *(E)*
- [x] **Does anything read the DB off the engine thread?** **No** (19 Sep 2026). v4's rule is
      offload network/CPU, keep DB access on the loop thread; the two workers that do touch SQLite
      each own their connection. v5's `store()` makes that structural instead of comment-enforced.
      *(A)*
- [ ] **Attribution state in a backup** — is a source with an unfinished attribution job importable,
      or refused? An earlier note leaned on the computed `heal_level` catching this for free; that
      mechanism was dropped, so the question stands on its own terms again — does a half-finished
      attribution leave the database in a state v5 would import wrongly, or does the ledger it
      carries make it recoverable? *(B/E)*
- [x] **Does v5's required version ever rise?** **Yes, and it is not a policy question**
      (19 Sep 2026). The floor is the last release of v4; it moves whenever a post-.14 critical fix
      adds a heal, and previously-admissible backups correctly stop being admissible. Nothing to
      decide — only a version to supply on release day. See E above.