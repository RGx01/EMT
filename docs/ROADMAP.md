# Roadmap

*Workstream status. The agreed picture of what v5 is made of is
[design/EMT-v5_architecture.md](design/EMT-v5_architecture.md); the reasoning behind the ordering is
in [design/EMT-v5_programme.md](design/EMT-v5_programme.md) — this file tracks state only.*

**Nothing here has shipped.** EMT is pre-release.

**How this gets built** ([architecture §6](design/EMT-v5_architecture.md)): v4 ships **4.5.14**
(BL-71 + BL-72 + BL-73) and is then **frozen** — users stay on a stable release. This repo is seeded with a
**verbatim copy** of 4.5.14 so the suite (**2,764 at 4.5.14, 21 Sep 2026**) is green from commit one, and the workstreams
below are deconstruction steps against it. After 4.5.14 v4 accepts **critical fixes only**, each
cherry-picked here the same day.

| # | Workstream | Depends on | State |
|---|---|---|---|
| A | Storage and access model | — | 📝 design drafted |
| B | Job coordinator (BL-50) | A | 📋 not started |
| C | One API for every surface | A | 📝 design drafted |
| D | Surfacing performance | A, C | 📋 not started |
| E | Importer | A–D | 📝 spec drafted (v7) |

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

- **The pre-rendered chart files** — C step 4 deletes them.
- **The pre-restore safety zip**, written while the restored engine starts — it overlapped the
  worst commit.
- **`_backup_to_share` after every block finalise** — a whole-database copy each time; harmless
  at one finalise per half-hour, heavy during a catch-up burst.
- **Commits on the engine thread at all.** If the above are not enough, route engine writes
  through a writer thread so the loop never waits on the device. An architecture change, and
  deliberately last.

**Done when:** a soak on the SD card shows no engine-loop stall over 2 s (`scan-log`
`A (loop blocking): PASS`).

## B. Job coordinator

*One coordinator for user-mutating jobs; deny-if-busy with idempotent retry.*

Carried from v4's BL-50 design. The asymmetry is the bug: import and delete are well guarded
(17 and 14 references), attribution is not (3) — and attribution is the one that has crashed against
the engine. With A done this shrinks to coordinating writers only.

**Done when:** every mutating job acquires the coordinator, and a concurrency test proves a second
job is denied rather than interleaved.

## C. One API for every surface

*Break the write → wholesale-recompute coupling. No pre-generated HTML.*
Design: [design/EMT-v5_C_surfaces_design.md](design/EMT-v5_C_surfaces_design.md).

Not a format change. In v4, 17 call sites schedule a regeneration and regeneration is wholesale, so
one half-hour finalising re-renders two years. Five layers of compensation have accumulated around
that; see the programme map.

**Done when:** a write records that data changed and triggers no recompute, and every surface is
served by one API over a requested range.

## D. Surfacing performance

*Measure before optimising.*

Most of the win is delivered by A and C. Target hardware is modest — a Raspberry Pi 4 class box on
SD or USB storage — so ARM CPU and I/O latency, not data volume, set the ceiling.

**Done when:** time-to-first-byte and queries-per-surface are instrumented, with a budget per
surface that CI can assert.

## E. Importer

*Ingest a backup at or above the floor version, currently **4.5.16**. Spec at
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
**Currently 4.5.16** (spec v9, 29 Sep 2026): 4.5.15 carried #473's heal, 4.5.16 #481's. A 4.5.17
(EV_BOOST) that carries a heal raises it again.

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

> ⚠️ **Release-day checklist item, easily missed.** `FLOOR_VERSION` is pinned as the last step
> before v5's first release, from whatever v4 has actually reached by then, and the positive fixture
> is regenerated at that version — not carried forward from development. Until then it is a
> placeholder, and every post-.14 v4 fix that adds a heal bumps it in the same cherry-pick
> ([architecture §6](design/EMT-v5_architecture.md)).

Last deliberately: its contract is "write the EMT schema", so it cannot be finished until A–D have
settled what that schema is.

---

## Picking this up again

*Written 19 Sep 2026, before a pause to ship 4.5.14. Everything below is what a reader needs that
is **not** already stated in the design documents.*

### Where things stand

The **design is settled enough to resume from these documents alone.** Every decision taken has a
home, with its reasoning and its rejected alternatives, and the remaining open items are listed
above with what would answer each. No workstream is blocked on a decision that exists only in
someone's head.

**Not started: any code.** This repository is documentation. Commit one is still a verbatim copy of
4.5.14 ([architecture §6](design/EMT-v5_architecture.md)), which cannot happen until .14 ships.

### The next three actions, in order

1. **Ship 4.5.14 from the v4 repository.** BL-71, BL-72 and BL-73 — all three built and merged.
   BL-72 is the hard dependency: without a version stamp in users' databases the importer has no
   gate, and EMT cannot retrofit one. BL-73 is required too — `not-listed` is a real state, so the
   supplier cannot be inferred from the version.
2. **Seed this repository:** copy 4.5.14 verbatim as commit one, confirm the suite is green, then
   begin deconstruction step 1 (remove the unwired paths, [architecture §4](design/EMT-v5_architecture.md))
   — under the deletion protocol in [§4a](design/EMT-v5_architecture.md), which is what makes a
   subtractive programme against working code safe to run.
3. **Benchmark on the Pi** — this needs no v5 code and can run against a 4.5.13 install at any
   point, including while .14 is in progress. It answers two of the open questions above.

### Two dependencies that live in the v4 repository

Everything else needed to resume is in this repository. These two are not, because they are v4
actions:

| | |
|---|---|
| **BL-73** | A **v4** backlog item. This repository only records the dependency; the specification and the delta land in v4's roadmap and tree. |
| **Tag 4.5.14 at release** | Commit one here is a verbatim copy of exactly the tree that shipped, so it needs to be identifiable later. |

### Two things deliberately absent

- **No design documents for B (job coordinator) or D (performance).** B is carried from v4's BL-50
  design, which lives in the v4 repository; D is mostly delivered by A and C and its own work starts
  with measurement, not design. Both get a document when picked up — the same rule
  [DEVELOPMENT.md](DEVELOPMENT.md) applies to its own architecture sections, and for the same
  reason: a document written before the work is wrong on the day it lands.
- **No record of how any decision changed.** These documents state positions, not their history.
  That is intentional; the reasoning that survived is here, the drafts that did not are kept outside
  the repository.

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
      *(C/D)*
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