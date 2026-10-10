# EMT v5 — database import specification

*The contract for bringing an existing Energy Meter Tracker database — **at or above the floor
version** (§1.1) — into the new EMT app. This is the artefact the rest of v5 depends on: the new schema, the charting rebuild and the
legacy-column removal all assume history arrives correctly once, from here.*

**Status:** v18 — revised 10 Oct 2026, the importer as built (v17: floor raised to 4.5.22; v16: a held-rate block's segments are refitted to its kWh; v15: `imp_cost_exc` carried; v14: EV segments written before the EV columns drop). §3's gate verified end to end against a real 4.5.14 database (v7). Numbers below
are measured from a real 4.5.12 production database (135,482 blocks, 3 meters, ~2 years) unless
stated; provenance counts in §9 are refreshed from a 4.5.13 prod-dev DB.

*Changes in v2: §1 records the side-by-side container decision and closes Q1/Q4; §3 gains a source
**provenance** gate; §5.1 covers the `label='ZERO'` measured row (BL-70, 4.5.13); §5.2 makes the band
vocabulary an explicit importer mapping; §9 gate 5 widens from a sample to every block.*

*Changes in v3: **§5.2's band rule is reversed.** v2 had v5 unifying to `off_peak` / `peak` and
mapping `day → peak`. That was wrong — it flattens a real distinction between two different
concepts. v5 keeps all four labels and performs one era-scoped relabel instead. §1 and Q5 gain the
importer's retirement rule.*

*Changes in v4: **the source floor moves from 4.4.0 to v4's final heal high-water mark, and the
importer supports one schema era instead of two.** v1–v3 were written before 4.5.14 was decided and
still claimed there would be no final v4 release. There is: 4.5.14 ships BL-72's stamp, and that
marker — not table presence, not a "done" flag — is now the gate. *(v4 described the stamp as a
computed `heal_level`; v6 corrects that to a plain version.)* §3's first three gates
collapse into one that is strictly stronger, §4's era table is deleted, and the 4.5.12/4.5.13
databases on hand become **negative** test cases alongside the 4.3.1 one.*

*A first pass at v4 wrote the floor as the literal `4.5.14`. Corrected same day: the bar is wherever
v4 **stops**, which post-.14 critical fixes can move, and it is a release-day input rather than a
design-time constant (§1.1, Q9).*

*Changes in v5 (19 Sep 2026, after reading 4.5.13): **`not-listed` is a real supplier value and this
spec previously denied it.** v4 has had a supplier registry (`octopus` / `not-listed`) and
`normalize_supplier()` since the setup wizard shipped; BL-73 now stamps the normalised key into
`store_meta`. The retracted claim was "every v4 database that will ever exist is Octopus" — false,
because a local-only install has real CAD kWh history and no Octopus account at any point. §3 gate 3
becomes four cases, and the supplier is read **before** the account check rather than after.*

*Changes in v6 (19 Sep 2026): **`heal_level` is dropped.** BL-72 shipped as a plain version stamp,
`store_meta.written_by`, and the gate is a version comparison against a floor set when v5 is
written. Heals are historical: a later v4 release that adds one just raises the floor. The computed
level bought nothing the markers did not already provide, and its premise did not survive the code
(more than eight heals, mostly in `kraken_state`, one of them a backup with no invariant). §1.1,
§3, §6, §9 and Q9 all change; the *reasoning* about a moving bar survives intact — only the thing
being compared is now a version, not an integer.*

*Changes in v7 (21 Sep 2026, verified against a real 4.5.14 prod-dev database): §9 gains two
acceptance gates that were prose before and are now demonstrated — `store_meta` values must be
**JSON-decoded** (the column holds `'"4.5.14"'`, quotes included), and versions must compare
**numerically** (`2026.9.0` sorts BELOW `4.5.14` as a string, so v5 would reject its own backup).
That database becomes the first real **positive** fixture; everything held until now was a negative.*

*Changes in v8 (28 Sep 2026): **`FLOOR_VERSION` is pinned at `4.5.15`.** v4 is frozen at 4.5.15,
which shipped a heal (#473, device cost from `imp_kwh_grid`), so by §1.1 the floor rises to it.
v5 ports the two 4.5.15 forward fixes (#473, #475) and not the heal. The 4.5.14 prod-dev database
moves from the positive set to the negative set — the first real **below-floor** case — and a
4.5.15 database is needed as the positive fixture (§9).*

*Changes in v9 (29 Sep 2026): **the floor rises to `4.5.16`.** 4.5.16 shipped a heal
(`run_ev_bucket_refetch`, #481 — Octopus's retrospective bucket relabel), so by §1.1 the floor
follows it. v5 ports the forward fix (`bucket_side`) and not the heal. v4 is not necessarily
finished: a 4.5.17 is expected (EV_BOOST band), and if it carries a heal the floor moves again.
The positive fixture is now a 4.5.16 database; 4.5.14 and 4.5.15 are both below-floor negatives.*

*Changes in v10 (1 Oct 2026): **the floor rises to `4.5.17`.** 4.5.17 carries a heal
(`run_vat_backdate_heal` — 1 Oct 2026's 0% VAT, which Octopus applied by editing its open rate
records in place and 4.5.16 backdated to the record's start), so by §1.1 the floor follows it.
v5 ports the forward fix (`RateSchedule.with_vat`, the seeded 1 Oct boundary, the learner's
dating) and not the heal. 4.5.16 joins the below-floor negatives; the positive fixture is a
4.5.17 database, which does not exist until 4.5.17 ships.*

*Changes in v11 (1 Oct 2026, same day): **the floor rises to `4.5.18`.** 4.5.17's learner
re-learned the backdated 0% from IOG's own closed day/night windows, so its heal's work was
undone on the next refresh. 4.5.18 re-arms the heal (`vat_backdate_heal_done_v2`), and by §1.1
the floor follows it. v5 ports the learner fix (`learn_from_records` learns about today and
later only). 4.5.17 joins the below-floor negatives; the positive fixture is a 4.5.18 database.*

*Changes in v12 (1 Oct 2026, same day): **the floor rises to `4.5.19`.** Its heal (re-armed as
`vat_backdate_heal_done_v4`) also re-prices sub-meter rows left at 5% beside a 0% main on
1 Oct 2026. v5 ports the forward fix (the Bill Summary's per-date VAT) and carries no heal.
4.5.18 joins the below-floor negatives; the positive fixture is a 4.5.19 database.*

*Changes in v13 (7 Oct 2026): **the floor rises to `4.5.21`.** It carries three heals: the
EV-priority re-split (`ev_priority_resplit_done` — a rebuilt block lost `meter_type`, so every
re-split since 3.0.0 could give a concurrently charging battery the EV's grid share), the
dispatch-only device re-fit (`dispatch_house_refit_done`), and the corrected-device ex-VAT repair
(`corrected_device_exc_done`). v5 ports the four forward fixes and carries no heal. 4.5.19 and
4.5.20 join the below-floor negatives, and so does a v5 dev build stamped `4.5.20-a21`; v5's own
builds now stamp `4.5.21-a22`. The positive fixture is a 4.5.21 database.*

*Changes in v14 (8 Oct 2026): §5.2 — **a block with an EV split and no segments gets its segments
from the columns before they are dropped.** v5's surfaces now read the EV split from the EV segments
(`ev_split.py`), falling back to the `imp_*_ev` columns only for a block with no segments at all.
On one real install 10 such blocks hold 28.43 kWh of EV that the drop would otherwise lose. The
fallback goes with the columns.*

*Changes in v15 (8 Oct 2026): §5.2 — **`imp_cost_exc` is CARRIED, not dropped** (the drop list
falls from 11 columns to 10). It is the per-block ex-VAT authority, as `imp_cost` is the inc one: on
blocks with segments, Σ kWh × `exc_rate` differs from it by up to ±2.3p a month on real data, and
checked against two Octopus statements neither source matched every band, so the figure EMT has
always shown is kept. `imp_rate_exc`, `standing_charge_exc` and `exc_source` still drop: v5's
surfaces now derive the first two and never read the third.*

*Changes in v16 (9 Oct 2026): §5.2 — **a held-rate block whose segments do not sum to its kWh is
refitted on import** (`pricing_segments.refit_to_kwh`). v4 re-costs a corrected, reconciled or
measured block to a later settled kWh without touching its segments (ROADMAP BL-79 (1), fixed in v5
only), so every v4 database carries such blocks, more each month. Any other block whose segments
miss its kWh is reported, not changed.*

*Changes in v17 (9 Oct 2026): **the floor rises to `4.5.22`.** Its heal is the October VAT resync
(v4 #505): from 1 Oct 2026 Octopus's half-hourly costs still carried 5% VAT though it bills 0%;
4.5.22 detects Octopus's restatement per install and re-settles the period once every half-hour
agrees with the VAT calendar. **It is conditional on Octopus**: a 4.5.22 database has run the
mechanism, not necessarily the heal — if Octopus has not restated, its October measured blocks
still carry 5%. So v5 carries the detection and resync as **forward code** (it re-arms on every VAT
change and on imported data alike), not as a v4 heal; the floor guarantees only that 4.5.21's heals
ran and that 4.5.22's state marker travels with the data. 4.5.21 and the v5 dev build `4.5.21-a22`
join the below-floor negatives; v5's own builds now stamp `4.5.22-a23`.*

*Changes in v18 (10 Oct 2026): **the importer is built** (`v4import/`; [E design](EMT-v5_E_importer_design.md)),
and five decisions from building it change the text:*
1. *§3 — **classification by `store_meta.emt_schema`.** v5 marks a database it creates; a restore
   imports any database without the marker (v4, or a v5 build before it) and swaps a v5 backup in
   as it is, refusing one at another schema revision until the first release.*
2. *§5.2 — **the house `night` relabel is not built.** v5's own writers cannot tell which tariff era
   a past block is in, so the relabel alone would label one band two ways. It waits on ROADMAP
   BL-81; until then house `off_peak` stands in every era, on both sides of the import.*
3. *§6 — **every key has a rule**, in `v4import/rules.py` (E design §7): v4's whole key history,
   the backups' keys and v5's own. Any key ending `_done` / `_done_v<n>` is a completion marker and
   drops; any other unknown key, table or column aborts the import and is named.*
4. *§8 — **one transaction into a fresh target, which REPLACES the live database.** Not chunked and
   resumable: all-or-nothing by construction, and a re-import gives an identical database. Not a
   merge: the `pre_restore` safety backup keeps what was replaced (decided 10 Oct 2026).*
5. *§9 — **the gates run in the import's transaction**, against the attached source, and a failure
   rolls everything back. Gate 5 is `verify.split_reproduced`; gate 11 is `verify.held_fit`. The
   §9 corpus runs against real backups with `tests/test_import_corpus.py` (EMT_IMPORT_CORPUS).*

---

## 1. Purpose and shape

v5 ships as a **new add-on with a new slug**, so users get a *fresh install*, not an in-place
upgrade. There is therefore no schema migration inside the new app — instead there is a
**one-directional importer** that reads a **4.5.14-or-later database at or above the floor
version** (§1.1) and writes the v5 schema. 4.5.14 is the earliest such source, not necessarily an
admissible one.

Consequences that follow from that choice, and which this spec exists to protect:

- The new app **never contains** the pre-4.4.0 one-time heals (BL-33: 7 code sites already tagged
  `# DEPRECATED — remove in 5.0.0`), nor the 15 legacy derived columns, nor the `'measured'`
  naming. Those are known *to the importer only*.
- The old add-on keeps working throughout. A failed import costs the user nothing.
- **The two run side by side, in their own containers, indefinitely.** EMT 2026 is not a
  replacement the user must cut over to on a deadline; it is a second add-on that ingests a copy of
  the first one's history. v4's instance isolation already yields contended resources rather than
  fighting for them (`resolve_backup_dir` hands `/share/..._<site>` to whichever instance owns it),
  so the coexistence mechanism exists and is proven in production.
- There is **no cooperating exporter**. The source is what v4 already writes — the backup zip — and
  the importer validates it itself (§3) rather than trusting a contract.

### 1.1 The floor is a version, and 4.5.14 is where the stamp begins

**Decided 19 Sep 2026.** Earlier drafts set the floor at 4.4.0 and said there would be no final v4
release. Both were wrong: **4.5.14 ships BL-72**, which records the running version in the database.

| marker | where | status |
|---|---|---|
| `written_by` TEXT | `store_meta` | **New — BL-72** (shipped in .14). The app version that last opened the database. **The gate.** |
| account identity | `kraken_state.kraken_account_number` | **Already exists.** Populated in every real database checked (4.5.12, 4.5.13 prod and prod-dev). |
| `supplier` TEXT | `store_meta` | **New — BL-73** (shipped in .14). The normalised supplier registry key. Values: `'octopus'`, `'not-listed'`, or absent. |

EMT cannot retrofit a marker it was not present for — but the account identity was already there,
and an earlier draft of this section proposed adding a duplicate of it. Corrected 19 Sep 2026 after
checking the schema.

#### The account is already stamped, and v4 already guards it

`engine.py` holds `_ACCOUNT_KEY = "kraken_account_number"` in `kraken_state`, written on first
association and on explicit reconnect (`engine.py:5507`, `:5509`). Two functions already exist:

- **`get_db_account()`** — *"The Octopus account this DB is stamped to, or None if never stamped."*
  Exactly the read the importer needs, including the never-stamped case §3 gate 3 branches on.
- **`kraken_account_mismatch()`** — the guard itself, already implemented.

So v5 does **not** need a new `account_ref` field. A draft of BL-73 proposed one on the grounds that
reading a Kraken-specific key would leak v4's layout into v5 — a weak argument, since the importer
reads v4's entire schema by definition, and a bad trade against duplicating a value that exists.

#### The supplier registry already existed; BL-73 records its answer

v4 has had a supplier registry since the setup wizard shipped — `engine.py`:

```python
_API_CAPABLE_SUPPLIERS = frozenset({"octopus"})
```

with `normalize_supplier()` mapping input onto it, and the wizard's `WIZ_SUPPLIERS` dropdown
offering exactly two keys:

| key | label |
|---|---|
| `octopus` | Octopus Energy |
| `not-listed` | My supplier isn't listed / local metering only |

What v4 never did is **write the answer down in one place**. `config_periods.supplier` is the
*display and historical record* and holds either kind of value: a registry key on an install set up
through the wizard, free text on one that predates it (`'Octopus Energy'` — which is what every real
database we hold contains). BL-73 stamps the normalised key into `store_meta.supplier` so the
importer reads v4's own reading of its own field, rather than reimplementing the mapping against a
mixed column.

#### `not-listed` is a real value, and an earlier draft of this section denied it

That draft argued a v4-side token might be unnecessary, because *"v4 is frozen after .14, so every
v4 database that will ever exist is Octopus"*, making the supplier derivable from `written_by`.
**That is false.** `not-listed` means *local metering only, no supplier API* — a supported
configuration with real CAD-read kWh history and no Octopus account at any point. Deriving
`written_by ⇒ octopus` would read such a database as an Octopus account and prompt for credentials
that cannot exist.

So the three states are distinct and all three matter to the first-run wizard (§3a):

| `store_meta.supplier` | means | credential prompt |
|---|---|---|
| `'octopus'` | an API-capable supplier was chosen | **yes** — that supplier's |
| `'not-listed'` | local metering only, chosen deliberately | **no** |
| absent | never asked (a config predating the field) | **no** |

`not-listed` and absent take the same path but are not the same fact, and BL-73 keeps them apart by
writing nothing rather than a placeholder when the question was never put.

**v5's own schema still needs its own token.** v5 *will* be multi-supplier, so its databases and
backups need one from the start — same reasoning, applied to v5's side of the boundary rather than
satisfied by what .14 stamps.

#### Why a version is sufficient — the heals are gated by markers that travel with the data

Every one-off heal in v4 is gated on a marker in `kraken_state` or `store_meta`, and an **ungated
marker runs at startup**. So upgrading any older database to a given release runs every heal that
release carries: a 4.4.0 database opened by 4.5.14 has no markers at all, and therefore runs all of
them. A restored 4.5.12 backup carries 4.5.12's markers, so a heal introduced in .13 is ungated and
runs, while the older ones correctly do not.

**"Last opened by version X" therefore means "healed to X's standard."** That is the whole
inference, and it is why nothing more elaborate is needed.

> **The floor is the last release of v4, decided when v5 is written — not now.** If a post-.14
> critical fix adds a heal, the floor rises to that release. A reader gates on one version
> comparison; it never needs to know how many heals exist or what they are called.
>
> **Pinned 28 Sep 2026 at `4.5.15` (#473's heal); raised 29 Sep 2026 to `4.5.16`** (#481's heal);
> **raised 1 Oct 2026 to `4.5.17`** (the VAT backdate heal), and the same day to **`4.5.18`** (the
> heal re-armed after 4.5.17's learner undid it), and again to **`4.5.19`** (the heal re-armed
> for sub-meter rows); **raised 7 Oct 2026 to `4.5.21`** (three heals: EV-priority re-split,
> dispatch-only device re-fit, corrected-device ex-VAT); **raised 9 Oct 2026 to `4.5.22`** (the
> October VAT resync — conditional on Octopus restating, so v5 also carries it forward; see v17). A
> 4.5.14–4.5.21 database is refused.

*Rejected: a computed `heal_level` integer.* An earlier design proposed one, with each heal
verifying its own invariant at startup so the level could fall when unhealed history arrived. It was
dropped on 19 Sep 2026 as unjustified — it required an invariant per heal, a migration, and a
mechanism, to solve a problem the markers already solve. **Heals are historical.** A later release
that adds one simply raises the floor a reader accepts. The design also did not survive contact with
the code: it assumed eight heals in `store_meta`, when there are more than eight and they live
mostly in `kraken_state`, and one of its eight (`pre_live_snapshot_done`) is a backup with no data
invariant to verify at all.

**One schema era, not two.** Any database at or above the floor is 4.5.14+, so it always has
`block_segments`, `rate_source` and `measured_cost`. The 4.4.0–4.5.4 era — segments present, settled
provenance entirely absent — cannot reach the importer. §4 is deleted rather than defended.

#### v5 stamps its own version on import, and inherits the mechanism

The imported database is stamped with the **v5 release** that wrote it (`2026.9.0` or whatever
ships), by the same means. v5's own heals, as they accumulate, use the same marker gating — so the
same inference holds for whatever reads a v5 database later. The stamp is not a v4 migration
artefact; it is how a database says what it is, permanently.

**Nobody is stranded, at any level.** A user on 4.4.0, on 4.5.13, or on 4.5.14 when the mark has
moved past it, is never refused permanently — they are told to update the old add-on, let it finish,
take a backup, and import that. The heals run where they were always meant to: in v4, against v4's
schema, with v4's tests behind them. That is the whole reason .13 was declared the final *era* of
heals for v5's purposes and v5 starts from an empty heal set of its own.

### 1.2 The importer is a separable unit with a retirement rule

Everything v4-shaped — the gate (§3), the column rules (§5.2), the `store_meta` allow-list (§6) —
lives inside it and nowhere else:

> **E is one module. No domain code may import from it, and no domain concept may be
> importer-only. It is removed in the release after 4.5.14 sources stop being supported.**

This is not housekeeping. v4's three legacy backfills (162 lines) became the cautionary tale
*because they were embedded in `engine.py` with no boundary to cut along* — unwiring was the only
cheap move available, so deletion never happened and the code has sat since reading as
load-bearing. A module with one direction of dependency and a stated end date cannot decay that
way: when the date arrives, the deletion is a directory.

**Non-goals.** This is not a rewrite. The pricing/settlement engine (`engine.py`,
`block_store.py`, `pricing_segments.py`) is carried across substantially as-is; the importer must
not "improve" pricing on the way through. Re-deriving history is explicitly out of scope except
where §5 says so.

---

## 2. The governing invariant

> **Carry what cannot be re-fetched. Re-derive only what is reproducible. Never upgrade
> provenance on the way through.**

This is not a performance preference — some source data is *permanently* unavailable from Octopus:

| Data | Why it cannot be recovered |
|---|---|
| `measured_cost` (settled cost / band / four-bucket split) | Octopus does not always re-serve cost for a past half-hour. Evidenced: a 4.3.1 user database carries **2,091** half-hours for which the Measurements API returns kWh and **no cost**, permanently. |
| Cost-Corrections (`rate_corrected` / `rate_source='corrected'`) | Pure local user input. Nothing upstream has ever held it. |
| `dispatch_slots` / `dispatch_history` | EMT retains the lifecycle permanently *because* Octopus prunes at ~90 days (BL-35, shipped deliberately). |
| `deleted_ranges` | Deliberate-deletion tombstones. Lose them and the new app's backfill silently **re-creates data the user deleted on purpose**. |
| `historical_derivation` + `recorder_attribution_runs` | The device-attribution ledger that makes attribution reversible. |
| `vat_calendar` | Local VAT-rate calendar underpinning every ex-VAT figure. |

The third clause matters as much as the first two, and the 4.5.14 floor does **not** retire it. A
4.5.14 database still contains history written long before settlement existed — every block from the
4.4.0–4.5.4 era carries `rate_source='schedule'` because nothing ever settled it. The importer
carries that word across unchanged. What it must never do is treat "this database is from a version
that *has* settlement" as "this block *was* settled". We spent 4.5.12 fixing exactly that class of
bug — a block claiming to be settled when it wasn't.

The distinction the floor removes is only about the **schema**: every source now has the columns.
The distinction it preserves is about the **rows**: most of them are still unsettled, and say so.

---

## 3. Source eligibility — the gate

The importer's first act is to decide whether a source database is admissible.

**The gate runs in two phases, and the order matters.** Everything that can be checked from the file
alone is checked **before** the user is asked to authenticate; only the account match needs
credentials, and it comes last.

```
PHASE 1 — file only, no credentials                PHASE 2 — after auth
  1  no hot -wal sibling                             6  account stamp matches the
  2  opens read-only; integrity clean                   credentials just supplied (§7)
  3  written_by >= FLOOR_VERSION
  4  supplier present and supported
  5  inc rates in pounds, not pence
        ↓                                                    ↑
   supplier tells the wizard WHICH credential prompt to show ┘
```

**The order within phase 1 is operational, not arbitrary.** The WAL check is a filesystem test and
happens before the file is opened at all. Integrity comes next, because **a marker read out of a
damaged database is not evidence of anything** — `written_by` and `supplier` are only trustworthy
once `quick_check` has passed. Then the two markers, cheapest and most decisive last.

**Why not check the account first?** Because asking someone to sign in and *then* telling them their
backup is unusable is the wrong way round — and because with multiple suppliers it is not even
possible. You cannot prompt for credentials until you know which supplier's credentials to ask for,
and the backup is what says so (§1.1, §3a). Phase 1 is therefore both kinder and structurally
necessary.

**Phase 1 — admissible from the file alone iff all hold, checked in this order:**

*(4 Oct 2026: all four are enforced at restore by `import_gate.py` — `v4import/gate.py` since
10 Oct — which holds the one `FLOOR_VERSION`. Gate 1's `-wal` test applies where there is something to look at — a file
beside the candidate, or a `blocks.db-wal` inside a zip; an uploaded `blocks.db` arrives
alone, so a live copy uploaded without its `-wal` is not detectable from the file.
`source_supplier` reads gate 3's answer the way the wizard must, including the amendment below.)*

1. **The source is a backup, not a live database, and it is intact.** The accepted inputs are a
   backup **zip** as written by `_backup_to_share`, or a `blocks.db` **taken from one**. A file
   copied out of a running container is not admissible: v4 holds the database in WAL mode and
   checkpoints only at startup, so a `blocks.db` lifted mid-run can have committed history sitting
   in a `-wal` sibling that the main file does not yet contain. The importer must not silently drop
   the user's most recent days.

   Enforced as: if a `-wal` sibling is present and non-empty → **refuse**, naming the file and
   directing the user to take a proper backup. If it is absent, open the database **read-only**
   (`file:…?mode=ro`) and verify `PRAGMA quick_check` and `PRAGMA foreign_key_check` are clean.
   The importer never opens a source read-write, and never checkpoints someone else's database —
   recovering a hot WAL would mean writing to the live app's file from another container.

2. **`store_meta.written_by` is present and ≥ `FLOOR_VERSION`** — the last release of v4, pinned
   when v5 is written (§1.1), never a literal in the gate. This one gate replaces the three earlier
   drafts used (does `block_segments` exist · did `reprice_history_state` say `done` · is
   `count_blocks_needing_reprice()` zero), and subsumes all three: every heal is marker-gated and an
   ungated marker runs at startup, so a database last opened by version X has had every heal X
   carries.

   **Absence is decisive.** Only 4.5.14+ writes the stamp, so a database without one is pre-4.5.14
   whatever else it contains — and pre-4.5.14 means no supplier key either, so the wizard has
   nothing to work from.

   > **Compare versions numerically, never as strings.** `"4.5.9" > "4.5.13"` is true lexically and
   > false in every sense that matters here. Parse to a tuple. v4 already has `_parse_version` in
   > `web/server.py` for its own update check; v5 needs the equivalent, and it must also order
   > v4's `4.5.x` below v5's CalVer `2026.x.y` — a v5-stamped database is *above* any v4 floor, and
   > a naive numeric parse would put 4 below 2026 correctly but only by luck of the first field.

3. **`store_meta.supplier` must be one this build understands — which is not the same as one it
   can authenticate.** Four cases, and only the last is a refusal:

   | value | meaning | what happens |
   |---|---|---|
   | `'octopus'` (any API-capable key) | an API-capable supplier was chosen | prompt for that supplier's credentials → phase 2 |
   | `'not-listed'` | local metering only, chosen deliberately | **import**, no prompt, no phase 2 |
   | absent, **account stamped** | an Octopus install whose free-text supplier field was never filled | read as `'octopus'`: prompt → phase 2 |
   | absent, no account | never asked — a config predating the field | **import**, no prompt, no phase 2 |
   | anything else | a supplier this build has not implemented | **refuse** |

   **The two middle rows are the point.** Neither is a refusal, because both describe a database
   that holds **real meter history**: kWh read locally from a CAD is genuine, irreplaceable
   consumption data that simply never got priced. Refusing it would throw away the user's record to
   enforce a check that has nothing to check.

   `not-listed` and absent take the same path for different reasons — *"asked, and the answer was
   local-only"* versus *"never asked"* — and BL-73 keeps them apart deliberately (§1.1). The
   importer does not need to distinguish them; the audit record (§9) should, because a support
   question about missing costs has two different answers.

   *The asymmetry decides it.* Wrongly importing unpriced kWh costs nothing — it is visible, it is
   the user's own data, and connecting an account later fixes it. Wrongly refusing it destroys
   history that no API can re-serve. When a check has no subject, skip the check; do not refuse the
   subject.

   > **The supplier gates the account check, not the other way round.** A user who was on Octopus
   > and has since moved to a local-only setup carries `supplier='not-listed'` *and* a leftover
   > account stamp from the Octopus era. The stamp is stale — it describes history, not the current
   > account — so phase 2 must not run on it. Read the supplier first.

   **Absent is not rare, and an account stamp settles it** *(added 4 Oct 2026)*. BL-73 assumed every
   live database held `config_periods.supplier = 'Octopus Energy'`, so `stamp_supplier()` writes
   nothing when the column is empty. But in v4 that column is an optional free-text box on Billing
   History, and real Octopus API installs on hand have it NULL — no `store_meta.supplier`, and
   `kraken_state.kraken_account_number` set. Octopus is the only API v4 ever had, so a stamped
   account with no supplier can only be Octopus: v5 reads it as `'octopus'` and shows that prompt.
   This is a reader rule, not a v4 change — a v4 stamp would reach only databases a new v4 release
   had opened, so v5 would need the rule for 4.5.19–4.5.20 regardless, unless the floor rose for a
   change that is not a heal. The audit record (§9) notes the supplier as *inferred*, not stamped.

   **Treat the account stamp as the signal, not a marker pair.** `get_db_account()` already answers
   "was this database ever associated with an account?", which is the only question gate 3 needs.

   *(Two earlier drafts got this wrong in opposite directions — first "absence cannot occur, treat
   as corruption", then "absence means there is nothing to bring across, refuse". Both destroy
   importable history.)*

4. **Every priced main-meter import block is in pounds, not pence — judged against its own ex-VAT
   figure.** *(Added 4 Oct 2026, with BL-33.)* The 4.3.0 IOG cap bug wrote some blocks' inc
   columns in pence. v4's re-price sweep repaired them, and v5 deleted that repair — but gate 2 does
   not cover it: the sweep needs the API and can be interrupted, so "stamped at the floor" does not prove it
   ever finished. This gate does, from the file alone.

   For each `electricity_main` import block with a rate and kWh: `ratio = imp_rate ÷ imp_rate_exc`
   (or `imp_cost ÷ imp_cost_exc`), and `expected = 1 + VAT` on the block's date, from the VAT calendar
   (seed plus the source's learned `vat_calendar`). **Refuse** if any block has `ratio ÷ expected > 10`,
   naming the first such block and directing the user to open the database in the last v4 release and
   let the re-price sweep finish. No fixed price: each block is judged against its own canonical £
   figure, so it holds on any tariff, Agile included.

   *Measured, 4 Oct 2026:* on four real databases healthy blocks sit at `ratio ÷ expected` = 1.000 at
   the median, worst deviation 4.8% (the 1 Oct 2026 VAT change); a pence block reads ~100, so the bound
   leaves two orders of magnitude either side. Every priced main block on five databases carried the
   exc figure it needs; a block without one is not judged by this gate (§G of the roadmap: after
   authentication, a local tariff store can judge it against the published rate). 108 databases on
   hand held 0 pence rows.

**Phase 2 — only when *both* sides present an account identity** (the source is stamped per
`get_db_account()`, and the target has credentials configured). Skipped otherwise, including on the
diagnostic path where the recipient has deliberately disconnected — see "Importing a database you do
not own" below:

5. **The source's account stamp matches the account those credentials belong to** (§7).

   **A mismatch does not refuse the import.** v4 already faces this exact situation and answers it
   better than an earlier draft of this spec did — `kraken_account_mismatch()`:

   > *"the loaded DB is stamped to a different Octopus account than the current credentials, so the
   > API was NOT auto-activated. Credentials are kept; an explicit in-app reconnect re-associates
   > this DB."*

   So the history **loads**; what does not happen is the account being associated or the API being
   auto-activated, and the user is told, with reconnect available if they meant it. v5 copies that
   behaviour rather than inventing a refusal. It protects the same thing — no silent mixing of two
   properties' data — without the failure mode of blocking someone from reading their own history.

> **Do not use `schema_version`.** It reads `1` on a 4.3.1, a 4.5.12 and a 4.5.13 database alike —
> it is not a version discriminator, and its uselessness is what BL-72 exists to fix.

**Refusal must be explicit and actionable**, never a partial import:

- *no `written_by`* → "This backup is from a version of Energy Meter Tracker too old to say what
  produced it. Update the old add-on, then take a backup and import that." The user is delayed, not
  stranded — §1.1.
- *`written_by` below `FLOOR_VERSION`* → "This backup was written by Energy Meter Tracker
  `<written_by>`, which is older than EMT can import from. Update the old add-on, then back up
  again."

  **Neither message names a target version.** The floor is whatever v4 ended at, which this document
  does not know and the code must not assert — so the instruction is *"update the old add-on"*, not
  *"update to 4.5.14"*. Both cases have the identical remedy, which is why they are two messages
  only for accuracy about what was found, not because the user does anything different.
- *hot WAL present* → "This looks like a copy taken while Energy Meter Tracker was running, and
  some of your most recent half-hours are not in it yet. In the old add-on take a backup, then
  import the zip it writes."
- *integrity check fails* → "This database is damaged and cannot be imported safely." Never
  attempt repair: the user still has the working original.
- *unsupported supplier* → "This backup is from a `<supplier>` account, which this version of EMT
  cannot import yet." Only for a key this build does not know — **never** for `not-listed`, which is
  a supported state and imports normally.
- *account mismatch* → §7.

**Not a refusal:** `supplier = 'not-listed'`, or no supplier at all. Both are the local-only /
never-asked branch — import proceeds without a credential prompt (gate 3), and the user connects an
account afterwards if they ever have one to connect.

### Credentials are not in the backup, and a missing key is not a refusal

**Verified against merged 4.5.13.** `engine.py:save_kraken_credentials` writes the key to
`KRAKEN_CREDS_PATH` = `{DATA_DIR}/kraken_credentials.json`, mode 0600, and says why in its own
docstring: *"outside the DB so they are never included in backups"*. `_backup_to_share` copies
`blocks.db` plus `meters_config.json` — nothing else. Disconnect is a full teardown, not a file
delete: `/api/disconnect-kraken` → `disconnect_kraken()` stops polling, tears down the client and
removes the file.

Three consequences, and the third is the one earlier drafts got wrong.

1. **v5 never has a key to lift.** The question of whether to trust a secret found in a backup does
   not arise; there is never one there. A key can only ever arrive by the user typing it.
2. **That is what makes the account check meaningful.** Phase 2 compares the source's account stamp
   against the account behind credentials the *user supplied*, which is evidence they hold that
   account. Credentials read out of the same file as the stamp would have been checking the backup
   against itself.
3. **A database can therefore be imported with no credentials at all, on either side.** The
   credential step is **skippable**. Requiring a live account connection in order to load historical
   data would be requiring the wrong thing: the history is already in the file, and costing is a
   forward activity that begins whenever an account is connected. v4 already treats "no credentials"
   as an ordinary state — `has_kraken_credentials()` guards exist throughout — and v5 inherits that.

### Importing a database you do not own

**This is a real, supported workflow and the gate must not break it.** Users send their databases in
for diagnosis; the recipient disconnects their own account first, then imports. Disconnecting
removes the credentials file, so the target instance has no account identity at all.

The rule that follows is symmetric with §3 gate 3:

> **Phase 2 runs only when *both* sides present an account identity** — the source carries
> an API-capable supplier **and** an account stamp, and the target has credentials configured. If
> any of those is absent there is nothing to compare, so the check is skipped rather than failed.

That single rule covers every case without a special diagnostic mode:

| source | target | phase 2 |
|---|---|---|
| `octopus` + stamped | connected | **checked** — on mismatch the history still imports; the account is not associated and the API is not auto-activated (§7) |
| `octopus` + stamped | disconnected | skipped — the diagnostic path |
| `not-listed` | either | skipped — no API account to compare, whatever stamp may linger |
| never stamped | either | skipped — nothing to compare (§3 gate 3) |

No override is needed for the checked row either, because a mismatch was never a refusal — v4's
`kraken_account_mismatch()` imports the data and declines to associate the account (§7). Disconnecting
first remains the cleaner path, since it states the intent up front and avoids the warning entirely.

---

## 3a. First run: import or fresh setup

**Added 19 Sep 2026 — this was missing, and its absence would have shipped an app no v4 user could
migrate into.** Every earlier draft described what the importer *does* and never said how a user
reaches it. The implicit assumption was a fresh install that gets configured and then, somehow,
imports later. For the population this spec exists to serve — every current v4 user — that is the
wrong default path and in the multi-supplier case it is an impossible one.

### The fork

v5's first start offers a choice, before anything else is configured:

```
first start
   │
   ├── "Set up a new account"  ──────────────► supplier → auth → meters → done
   │
   └── "Restore from an Energy Meter Tracker backup"
              │
              ├─ upload the zip
              ├─ PHASE 1 gate: not-live · integrity · written_by · supplier    (§3)
              │     └─ refuse here, before asking the user for anything
              │
              ├─ API-capable supplier? ─ no ─┐   ('not-listed', or never asked:
              │                              │    real kWh history, never priced)
              │   yes                        │
              │    ├─ show THAT supplier's   │
              │    │  credential prompt,     │
              │    │  with SKIP offered      │
              │    │  (keys are never in a   │
              │    │   backup — §3)          │
              │    │                         │
              │    ├─ skipped? ──────────────┤   (import history now,
              │    │   no                    │    connect an account later)
              │    └─ PHASE 2: account stamp │
              │       matches that account   │
              │            │                 │
              ├────────────┴─────────────────┘
              │
              ├─ import (§8) — one job, all-or-nothing, takes the job lock
              │     └─ NO heals run: v5 contains none, which is what the
              │        version floor exists to make safe (§1.1)
              ├─ write the audit record (§9)
              └─ start the engine — history in place
                    └─ if no account was connected (no supplier, or the user
                       skipped), hand off to account setup. Costing begins
                       whenever that happens; the history is already there.
```

**Every path ends with the history imported.** The only outcomes that stop an import are the phase 1
refusals — too old, damaged, unsupported supplier, inconsistent markers — and a genuine account
mismatch. Absent credentials, on either side, never block it.

### Why the backup drives the credential prompt, not the reverse

This is the sequencing consequence of `supplier` existing at all. The wizard cannot ask *"which
supplier?"* on the restore path, because the backup already knows, and asking invites the user to
answer wrongly and then fail the account check for a reason that looks unrelated. Nor can it ask for
credentials before reading the file, because **the shape of the credential prompt depends on the
supplier** — Octopus wants a Kraken API key, another supplier may want OAuth or a password.

So the order is fixed: *read the backup → learn the supplier → ask for that supplier's
credentials → verify they match the account the backup names.* Each step's input is the previous
step's output, which is also why §3 splits in two.

### Both paths must still work

A user who picks "new account", uses EMT for a week, and *then* decides to bring their history
across is not blocked. The import runs against a configured instance, and §3's phase 2 does exactly
what it always did — compares the account stamp against the credentials already present, and on a
mismatch imports anyway without associating the account. The fork is about making the good path reachable, not about closing the other one.

> **Scope note.** The fork is client-layer work, not importer work — the gates and the ingest are
> the same in both orders. It lands with the client (architecture §6 step 6), and the only thing
> that has to be true earlier is that the markers exist: the account stamp already did, and BL-73
> adds the supplier key in .14.

---

## 4. One schema era

*Earlier drafts carried a two-era table here — 4.4.0–4.5.4 (segments present, settled provenance
entirely absent) versus 4.5.5+ — with detection by column presence and a rule that a 4.4.x import
must never produce settled history. **Deleted 19 Sep 2026.** With the floor at 4.5.14 (§1.1), the
first era cannot reach the importer: a database at or above the floor always has
`block_segments`, `rate_source` and `measured_cost`. There is no era detection, no column-presence
probing, and no conditional carry.*

**What remains true, and is not an era question:** a 4.5.14 database still holds history from before
settlement existed, and those rows say `schedule`. The importer carries each row's own provenance
word across; it does not infer provenance from the source's version. That is §2's third clause, and
it applies to every source equally now rather than to one era specially.

Observed provenance distribution on the 4.5.12 reference database (`electricity_main`):
`schedule` 56,932 · `measured` 360 · `corrected` 33 · `reconciled` 20 · null 7. The shape is
representative of what a .14 source looks like — overwhelmingly unsettled history with a thin recent
settled edge — which is why "most rows are unsettled" must stay a normal outcome and never a
warning.

---

## 5. Table-by-table disposition

20 tables in the source. Disposition codes: **CARRY** (verbatim) · **CARRY+REMAP** (shape changes)
· **DERIVE** (recompute in v5) · **DROP** (do not import) · **REGEN** (new value in v5).

### 5.1 Core history

| Table | Rows (ref) | Disposition | Notes |
|---|---|---|---|
| `blocks` | 135,482 | **CARRY+REMAP** | Per-column rules in §5.2. The row identity (`block_start`, `meter_id`) is the join key for everything else. |
| `block_segments` | 37,184 | **CARRY** | The pricing source of truth. Carry verbatim — never recompute from the legacy columns. |
| `measured_cost` | 360 | **CARRY — every row** | Irreplaceable. Carry all columns incl. `home_kwh/ev_kwh/home_rate/ev_rate` and `fetched_at`. 4.5.5+ only. **Including rows that look empty** — see below. |
| `config_periods` | 1 | **CARRY** | Block minutes / timezone / currency / billing day underpin every aggregate. |
| `meters` | 3 | **CARRY** | Device identity, parent/sub relationships, retirement state, sensor bindings. |
| `meter_channels` | 4 | **CARRY** | MPAN / tariff / rate-source bindings per channel. |

**`label='ZERO'` rows are data, not noise (4.5.13, BL-70).** A dispatched half-hour in which
nothing was drawn is answered by the supplier as a real reading with every bucket at zero. 4.5.13
caches that answer as `cost_incl=0, cost_excl=0, kwh=0, label='ZERO'`, with the four breakdown
columns left NULL. Such a row asserts *"the bill says nothing was drawn here"* — which is what stops
the settlement pass re-asking about the slot on every hourly tick, forever.

An importer that filters "empty-looking" rows, or that treats `cost_incl = 0` as *missing*, will
drop them and restart that loop in the new app. The three states are distinct and all three must
survive: **row with figures** (billed), **row with `label='ZERO'`** (billed nothing), **no row**
(never asked). The same distinction governs the split: breakdown columns present-and-zero means the
bill stated no EV; breakdown columns NULL means the bill stated nothing about the split, and a
completed dispatch still decides. v5's schema must keep those separable.

### 5.2 `blocks` — column rules

**CARRY (canonical facts and provenance):** `block_start`, `block_end`, `meter_id`,
`config_period_id`, `interpolated`, `imp_provisional`, `imp_kwh`, `imp_kwh_api`, `exp_kwh`, `exp_kwh_api`,
`imp_read_start/end`, `exp_read_start/end`, `standing_charge`, `carbon_g`, `carbon_intensity_g`,
`source`, `is_provisional`, `finalised_from_cad`, `needs_review`, `review_reason`,
`review_dismissed`, `derivation_id`, `rate_corrected`.

**CARRY — authoritative totals:** `imp_rate`, `imp_cost`, `imp_cost_exc` *(v15)*, `exp_rate`,
`exp_cost`. `imp_cost` on a settled block **is the bill** and must not be recomputed from segments;
segments intentionally differ by a sub-penny sliver which the remainder absorbs.

> **`imp_cost_exc` moved from DROP to CARRY, 8 Oct 2026 (v15).** It is the ex-VAT twin of
> `imp_cost`, and nothing on the ex-VAT side absorbs the segments' sliver. Measured read-only on
> three 4.x databases: where a main block has segments, Σ kWh × `exc_rate` equals it to ≤0.28p per
> block, but over a month the slivers reach ±2.3p, enough to move a rounded monthly figure in 4–5
> months (all CSV-imported history, 2023–24, plus Sep 2026). Two Octopus statements were checked.
> Over Jul and Aug 2026 the two sources agree exactly. Over Jun 2024 the segments matched one band
> and the column another, because there the column is `imp_cost ÷ 1.05` and `imp_cost` and
> `imp_rate` disagree (ROADMAP BL-80). So the column is kept as what EMT has always shown, and v5's
> bill method reads it (`tests/test_exvat_readers.py`).
>
> The other three ex-VAT columns still drop. `standing_charge_exc` is the standing charge ÷
> (1 + the day's VAT) to 4×10⁻⁷ on every row measured, and v5 derives it there. A block's ex-VAT
> rate is `imp_cost_exc ÷ kWh` (no real row has `imp_rate_exc` without `imp_cost_exc`).
> `exc_source` has no v5 reader. On a device row `imp_cost_exc` carries no information v5 uses
> (no surface reads a device's ex-VAT), and a v4 defect leaves some of those rows wrong: 4.5.8–4.5.14
> priced a device's ex-VAT from its raw reading, PASS 2 re-costs a device's inc from its grid share
> but carries its ex-VAT forward, and 4.5.15's clip heal checked only the inc side (114 rows on one
> database, 2 on another). The importer may carry device rows' `imp_cost_exc` as it finds them; it
> must not be read as a device's ex-VAT cost without re-deriving it from `imp_kwh_grid` and the
> main's ex-VAT rate.

**CARRY+REMAP — provenance:** `rate_source` → v5 naming (`'measured'` → `'settled'`, BL-61(5)).
Always present on a .14 source (§4), so there is no absent-column branch — carry each row's own
word. Fold `rate_reconciled` into the provenance model rather than carrying a second flag:
**where `rate_reconciled = 1` and `rate_source` is `'schedule'` or NULL, write `'reconciled'`**; a
`measured` or `corrected` row keeps its word (either outranks a reconciliation). Then drop the
column. *(8 Oct 2026: v5 itself already records a reconciliation in `rate_source` and sets no flag;
it reads the flag only as a fallback for exactly these rows — 11 on prod, 1 on PianSom, 0–2 on the
others — which this rule retires.)*

**DROP — derived, reconstructable from segments** (~~13~~ ~~12~~ ~~11~~ **10** columns; this is the
legacy-column removal): ~~`imp_kwh_grid`,~~ `imp_kwh_remainder`, `imp_cost_remainder`,
~~`imp_provisional`~~, ~~`imp_cost_exc`~~ *(carried, v15)*, `imp_rate_exc`, `standing_charge_exc`, `exc_source`,
`imp_kwh_ev`, `imp_cost_ev`, `imp_rate_ev`, `imp_ev_band`, `imp_home_band`.

> *Dropped so far (10 Oct 2026, importer PR 5a, `EMT_SCHEMA` 3):* `imp_kwh_remainder`,
> `imp_cost_remainder`, and `rate_reconciled` (folded above, read from the source by the importer).
> They are `v4import/rules.py` `DROP_COLUMNS`: named, so a source holding them is not unknown, and
> never carried.
>
> *Then (importer PR 5b, `EMT_SCHEMA` 4):* `imp_rate_exc`, `standing_charge_exc`, `exc_source`. A
> block's ex-VAT rate is derived where read from its own pair (imp_rate × imp_cost_exc / imp_cost),
> else the VAT calendar; the standing charge's from the calendar. The importer reads a source's
> `imp_rate_exc` for transforms 2 and 3 (the EV segments' and the refit's ex-VAT), joined on id.
>
> *Then (importer PR 5c, `EMT_SCHEMA` 5):* `imp_kwh_ev`, `imp_cost_ev`, `imp_rate_ev`, `imp_ev_band`,
> `imp_home_band`. The EV split and both bands are the block's segments; `ev_split` has no LEGACY
> fallback; v5 writes a block's segments in the block's own transaction. The importer reads the
> five from the source for transforms 2 and 3, and **gate 5** (`verify.split_reproduced`) checks,
> for every main block, that the segments reproduce `imp_kwh_ev` and `imp_cost_ev` and that no
> block has EV segments where the columns had none. Measured on five real databases: 0 kWh and 0 £
> mismatches, worst £ divergence 1.5 × 10⁻⁶, 0 phantom. **All 11 columns are now dropped.**

> **`imp_provisional` REMOVED from the drop list, 5 Oct 2026.** `imp_provisional` came OFF the drop list on 5 Oct 2026: it is not a duplicate of `is_provisional`. It marks a SUB-METER block written without its post-boundary read (v4 2.10.0); `is_provisional` marks a MAIN block not yet DCC-settled (3.0.0). `_amend_provisional_sub_meter_blocks` finds and corrects the former. They looked alike because both are 0 on every row measured — data is not proof.
>
> **`imp_kwh_grid` REMOVED from the drop list, 24 Sep 2026 — it is not reconstructable, because
> sub-meters have no segments.** Measured on a 4.5.14 prod database: it is set on **70,497 sub-meter
> blocks** (35,438 ev_charger, 35,059 house_battery), and those meters have **zero** rows in
> `block_segments` — the segment machinery is main-meter-only. It differs from `imp_kwh` on 451 of
> them, covering **86.4 kWh** of device draw that did not come from the grid.
>
> It is also live: `recost_sub_meters` bills on `COALESCE(imp_kwh_grid, imp_kwh)` because a battery
> or V2X device draws from storage as well as the grid, and only the grid portion is chargeable.
> Dropping it would silently re-bill those 451 blocks at their full device draw.
>
> The premise "derived, reconstructable from segments" was read as a property of the column. It is a
> property of the column ON A METER THAT HAS SEGMENTS. See C §3.

> These are exactly the columns whose drift against segments caused the recurring bug class. They
> are dropped *because* `block_segments` is carried verbatim — the information is not lost, only
> de-duplicated. **Dependency:** every v5 surface must read segments first (BL-27) before this is
> safe.

**WRITE SEGMENTS FIRST — a block with an EV split and no segments** (v14, 8 Oct 2026). The EV-split
columns (`imp_kwh_ev`, `imp_cost_ev`, `imp_rate_ev`, `imp_ev_band`, `imp_home_band`) are dropped
because the segments hold the same split. That holds only where a block HAS segments. For a main
block with `imp_kwh_ev > 0` and **no `block_segments` rows at all**, the importer writes them from
the columns before it drops them — `pricing_segments.segments_from_legacy` (an EV segment at the
split's own rate and band, and the house remainder at its derived rate, Σ cost = `imp_cost`), the
backfill's own reconstruction. A block that has segments keeps them as they are, whatever its
columns say: the segments are what the bill priced.

> Measured 8 Oct 2026, read-only, on five 4.x databases: wherever a block has EV segments, their
> kWh and £ equal `imp_kwh_ev` / `imp_cost_ev` exactly (0 mismatches), and no block has EV segments
> with the columns NULL, or house-only segments under a non-NULL `imp_kwh_ev`. The one gap: on one
> install, **10 blocks** (25 and 30 Aug 2026, 28.43 kWh, two of them corrected) carry an EV split and
> no segments at all; its twin install has segments for the same blocks. Those are what this rule
> keeps. v5 reads them through `ev_split.py`'s LEGACY fallback until the drop.
>
> The band columns are written on about twice as many rows as `imp_kwh_ev` (SMB-era rows where the
> supplier stated the four buckets with no EV), and on 6 rows of one install `imp_home_band` says
> `off_peak` where the house segment says `day`. Neither is read once the segments are the record:
> the band survives as `block_segments.band` (below).

**REFIT — a held-rate block whose segments miss its kWh** (v16, 9 Oct 2026). When v4's
settlement re-run holds a block's rate — `rate_source` `'corrected'`, `'reconciled'` or `'measured'` —
it re-costs the block to the settled kWh and leaves its segments at the kWh the block closed with
(ROADMAP BL-79 (1)). v5 fits them at the re-run; v4 does not, and is not fixed (a split
display, not a pricing error, so outside its critical-fix bar). So every v4 database admitted
carries such blocks, and more each month it keeps running. For each MAIN block with segments where

    rate_source IN ('corrected', 'reconciled', 'measured')
    AND ABS(Σ block_segments.kwh − imp_kwh) > 1e-6

the importer writes `pricing_segments.refit_to_kwh(segments, imp_kwh, rate=imp_rate,
exc_rate=imp_rate_exc, band=…)` — the function v5's own re-run uses — so imported history is what
v5 would have settled. No segment is re-priced: the house segments scale to take the difference,
keeping their rate mix; only once the house is gone does the EV shrink; a house-less block gains
one house segment at the held rate (band: `imp_home_band` where it is `off_peak` or `day`, else
`standard`). `imp_kwh`, `imp_cost` and every other column are carried as they are.

This is a transform, not a heal: no marker, no v4 version gate. The floor does not cover it because
no v4 release heals it. It runs once, on the importer's copy, like every rule here.

A main block whose segments miss its kWh but whose rate is NOT held is **reported, not refitted**:
its cause is not this one, and refitting would hide it. The import report counts both — refitted,
and reported with their block starts.

> Measured 9 Oct 2026, read-only: 12 and 13 held-rate blocks on two production-like installs (4 with
> EV on each; 0.13 and 0.10 kWh of segments short or over), none on a third account; on a dev
> database none held and one tariff-priced block (0.009 kWh) — which this rule would report.
> **Acceptance:** §9 gate 11.
>
> *Built 10 Oct 2026 (importer PR 4), re-measured on seven databases:* 12–13 refitted on the
> production-like installs, none on the third account. The dev database's block is **refitted**,
> not reported: it says `schedule` with `rate_reconciled = 1`, so the reconciled fold (transform 1,
> which runs first) makes it `reconciled`, a held rate, as v5's own re-run already treats it.

**CARRY+REMAP — band vocabulary.** `imp_ev_band` / `imp_home_band` are dropped as columns, but the
*words* in them survive in `block_segments.band`. v4 uses four words, and they are **not** four names
for two bands. They are a **2×2 of attribution × band**:

| | low | high |
|---|---|---|
| **house** | `off_peak` ⚠️ *should be `night`* | `day` |
| **EV / device** | `off_peak` | `peak` |

plus `standard` — the `pricing_segments` fallback, meaning **unknown**, not a band at all.

Under IOG-SMB the supplier states this 2×2 explicitly, as four rate buckets:
`CONSUMPTION_CHARGE_ECO7_DAY_x` · `ECO7_NIGHT_x` · `EV_DEVICE_PEAK_x` · `EV_DEVICE_OFF_PEAK_x`
(`x` = region letter). House = Σ `ECO7_*`, EV = Σ `EV_DEVICE_*`; the band is whichever bucket in the
pair carries a non-zero `value`.

**The two axes are different concepts, not synonyms.** House `day`/`night` is **clock-determined** —
Economy-7, the same boundary every day regardless of what the car does. EV `peak`/`off_peak` is
**dispatch-determined** — a smart-charge slot is off-peak, and an EV drawing outside one is charged
at peak. They coincide in being "cheap" and "expensive", and nothing else. Collapsing them would
lose the ability to say *why* a half-hour was cheap, which is the question every billing surface
exists to answer.

### The one remap: house low, SMB era only

> **Not built (v18, 10 Oct 2026): waits on ROADMAP BL-81.** Everything below still describes the
> intended rule. What stops it is v5's side: every writer of a house band decides "SMB" from the
> CURRENT tariff, not the block's date, and the whole-history re-price sweep rewrites segments
> after an import. A relabel only the importer made would leave the same era labelled two ways.
> The era rule for when it is built is decided: the earliest four-bucket `measured_cost` row.

**v5 keeps all four labels.** The importer performs exactly one relabel:

```
block_segments.band  'off_peak' → 'night'   WHERE attribution = house
                                              AND the covering agreement is IOG-SMB
```

`day`, `peak` and EV `off_peak` are carried unchanged. `standard` is carried unchanged as an
explicit unknown.

**Why the era scope is principled, not convenient.** Pre-SMB there is no `ECO7_NIGHT` bucket. In the
legacy Intelligent era a house half-hour is low *because a dispatch discounted the whole slot* —
that is the dispatch sense of off-peak, and `off_peak` is the correct word for it. The vocabulary
differs by era because the **tariff mechanism** differs by era. A blanket rename would be the same
category error as the `day → peak` rule it replaces.

**Why this is a remap and not a heal.** A heal repairs damage — data that was wrong when written, or
that went stale against its source — and therefore needs a marker, a completion check and a level,
because you must be able to tell whether it ran. This is a **schema translation**: it happens exactly
once, in a one-directional importer, and the v5 schema simply does not contain the old value on the
house axis. It sits beside `rate_source 'measured' → 'settled'` above, which is the same operation
and has never been called a heal. It must be expressed **declaratively**, as a row in the importer's
column-rule table, not as a branch in domain code.

**The alternative was more code, not less.** Carrying `off_peak` through would leave the same tariff
era labelled two ways depending on which side of the migration a row landed — imported rows saying
`off_peak`, natively-derived rows saying `night`. Every reader (rate line, settlement labelling,
billing split) would then have to accept both, permanently, in the hot path. The remap moves that
cost into a module with a stated end of life (§1).

> Do not infer the band from the rate to "improve" a `standard` segment on the way through. A rate
> equal to the day's low bound does not prove the supplier billed it off-peak. Provenance is not
> upgraded in transit (§2). The relabel above is permitted precisely because it changes **no** fact:
> the attribution and the band are both already recorded, and only the word for one cell of the 2×2
> changes.

**Verification obligation.** Assert that the v5 segment-derived EV/house split and ex-VAT figures
reproduce the dropped columns' values (within rounding) for **every block**, not a sample — see §9
gate 5. A mismatch means BL-27 is incomplete, not that the data is bad.

### 5.3 Dispatch and attribution

| Table | Rows | Disposition | Notes |
|---|---|---|---|
| `dispatch_slots` | 826 | **CARRY** | Irreplaceable beyond Octopus's ~90-day window. |
| `dispatch_history` | 1,493 | **CARRY** | The planned/started/completed lifecycle — the basis of every smart-vs-bump decision. |
| `historical_derivation` | 4 | **CARRY** | Device-attribution ledger; makes attribution reversible. |
| `deleted_ranges` | 0 | **CARRY** | **Critical even when empty.** Tombstones for deliberate deletes; dropping them makes the new app re-create deleted data. |

### 5.4 Telemetry and recorder series

| Table | Rows | Disposition | Notes |
|---|---|---|---|
| `sub_meter_history` | 33,540 | **CARRY** | Device SoC / inverter series used by attribution. |
| `power_history` | 16,770 | **CARRY** | Live-power series. Large; see §8 on chunking. |
| `mix_history` | 1,719 | **CARRY** | Rolling generation mix. Could be re-fetched, but only for the recent window — carry. |
| `carbon_intensity` | 287 | **CARRY** | Per-postcode intensity; block `carbon_g` already stamped, this is the lookup history. |
| `generation_mix` | 774 | **CARRY or DERIVE** | Block-stamped mix; superseded by `mix_history` for display (BL-38). **Open question §10.** |

### 5.5 Transient — do not import

| Table | Rows | Disposition | Notes |
|---|---|---|---|
| `current_block` | 1 | **DROP** | In-flight block; the new app forms its own on first tick. |
| `current_reads` | 60 | **DROP** | In-flight read buffer. |
| `reads` | 0 | **DROP** | Empty in the reference DB; raw reads are consumed into blocks. Confirm before finalising. |

---

## 6. `store_meta` and `kraken_state`

These hold one-time markers, and **most must not be carried** — a stale "done" flag in the new app
suppresses a pass that should run. Default is **DROP**; carry only by explicit allow-list.

> **As built (v18):** the allow-list and the drop list are `v4import/rules.py`, classified key by key
> in the [E design §7](EMT-v5_E_importer_design.md). Beyond the list below, the API import's
> coverage keys (`api_import_oldest_*`, `api_import_done_*`, `import_cost_coverage`) and the user's
> `billing_source` and `carbon_paused` cross, and `region_probe_clobber_done` crosses because re-run
> it would overwrite the carried regions. `written_by` and `db_uuid` are left for v5's
> `engine_startup` to stamp (REGEN by v5's own mechanism). An unknown key aborts the import.

**CARRY:**

- `vat_calendar` — irreplaceable local calendar (`[["2023-06-02", 0.05]]` in the reference DB).
- `settings` — user preferences (e.g. `{"bill_rounding_summary": true}`).
- `recorder_attribution_runs` — the attribution back-out ledger; pairs with `historical_derivation`.
- `export_settlement_expected` — whether the account has an outgoing agreement.

**REGEN:** `db_uuid` — the v5 database is a new identity. Record the *source* uuid in the import
audit record (§9) for traceability.

**`written_by` — REGEN, and record the source value in the audit trail.** It is the §3 gate's
input, and it must not cross as-is: the new database is written by **v5**, so v5 stamps its own
release version (`2026.9.0` or whatever ships) by the same mechanism. Carrying v4's value forward
would make a v5 database claim to have been produced by v4 — and every later reader of that database
would draw the wrong conclusion about which heals have run against it.

The source's value goes into the §9 audit record, where it answers "what produced this history"
without being mistaken for what produced this *database*.

> **v5 inherits the mechanism, not the markers.** v5's own heals are marker-gated exactly as v4's
> are, so "last opened by version X" keeps meaning "healed to X's standard" on the far side of the
> migration. That is why the stamp is not a migration artefact: it is how a database says what it
> is, and it keeps doing so after v4 is gone.

**DROP (one-time heal / sweep markers — must not suppress v5 passes):** `reprice_history_state`
(no longer read at all — the §3 gate reads `written_by`), `carbon_backfill_state`,
`carbon_backfill_done`,
`exc_backfill_state`, `iog_split_backfill_state`, `stale_exc_repair_state`,
`stale_iog_split_repair_state`, `region_probe_clobber_done`, `export_zero_fill_v1`,
`import_reprice_queue`, `region_reconcile_pending`, `schema_version`.

**`kraken_state` — DROP by default.** All 21 keys are runtime/sweep state
(`smb_rate_repair_done_v2`, `smb_ev_resplit_done`, `smb_device_recost_done`,
`verify_pricing_cursor`, `import_health_summary`, `last_poll_utc`, …). The two worth considering:

- `kraken_account_number` — **CARRY**, it is the account stamp (§7).
- `data_source_mode` — **CARRY** (Q3, closed 19 Sep 2026). The new install starts in the user's
  actual mode (cad / api / api+mini) rather than re-running the survey. From the user's point of
  view moving to EMT is an upgrade, and their hardware did not change on the way through.

  *Confirmed in 4.5.13:* `_MODE_KEY = "data_source_mode"` sits in `kraken_state` beside
  `_ACCOUNT_KEY` (engine.py:227, :235) and is set through `set_data_source_mode()` off an API
  endpoint. It is a **persisted setting**, not a runtime probe result — which is what makes carrying
  it meaningful rather than a guess.

  > **Carried, but not assumed serviceable.** The no-credentials import paths (§3) can land a
  > carried mode of `api` or `api+mini` on an instance with no key — deliberately, on the diagnostic
  > path. v5 must treat that as *mode as recorded, inactive until credentials exist*: keep the value,
  > do not poll, do not silently rewrite it to `cad`. Rewriting loses the user's real setting;
  > polling without a key fails on a loop. v4 already distinguishes these — `mode_uses_api()` and
  > `has_kraken_credentials()` are separate questions — and v5 keeps them separate.

> Rule of thumb: if a key's purpose is "this sweep already ran", it must **not** cross — the v5
> equivalent sweeps have not run. `data_source_mode` is on the other side of that line: it describes
> the user's **setup**, not work already done, so it crosses.

---

## 7. Account identity and multi-instance

Multi-property users run several instances via the repo-URL workaround; **this must keep working
in v5**, so the importer cannot assume a single global database.

- **Account stamp.** Reuse the existing guard (#357): a database is stamped to its account on first
  discovery, and **v4 already implements this whole guard** — `get_db_account()` reads the stamp,
  `kraken_account_mismatch()` compares it against the configured credentials. v5 reuses the
  behaviour rather than reinventing it: on a mismatch the history imports, the account is **not**
  associated, the API is **not** auto-activated, and the user is told — "this database belongs to
  account A…7; these credentials are for A…3" — with an explicit reconnect available if they meant
  it. This is §3's phase 2, the only gate that needs credentials.
- **Instance identity.** Carry `instance_name`; do not carry backup-directory ownership — the new
  instance negotiates its own share directory on first run (`resolve_backup_dir` already handles
  "ideal dir taken by another instance"). **But see §7a: that negotiation is namespaced by the v4
  add-on name, and v5 must not inherit it.**
- **One import per instance.** The importer targets the instance it runs in. Importing property B's
  database into property A's instance is the failure this guard exists to prevent.

---

### 7a. `/share` backup ownership is namespaced by the v4 NAME, and v5 must claim its own

**Found 24 Sep 2026, by running a second instance on the reference Pi.** Not a design review
finding — a log line from a real start-up.

`instance.py` hard-codes two things, both shaped by the v4 add-on:

```python
LEGACY_SHARE_BACKUP_DIR = "/share/energy_meter_tracker_backup"
_SHARE_GLOB             = "energy_meter_tracker_backup*"     # scan for owned dirs
```

`resolve_backup_dir` uses them in three steps: claim a directory whose `.emt_instance` marker is
ours; failing that, **adopt an UNMARKED legacy directory**; failing that, take a fresh one. The
adoption rule is justified for an upgrade — *"EMT only ever supported one instance before BL-5"* —
and a MARKED directory is correctly never touched, which is what protects a sibling.

**What the soak install did.** A second instance on the same Pi took step 2 and adopted
`/share/energy_meter_tracker_backup`, renaming it to its own site name:

```
resolve_backup_dir: migrated legacy backups
  /share/energy_meter_tracker_backup -> /share/energy_meter_tracker_backup_site_20260924T085212
resolve_backup_dir: site renamed; backups moved
  ..._site_20260924T085212 -> /share/energy_meter_tracker_backup_soak
```

Harmless in that instance — the live 4.5.14 install had already marked its own directory, so what
was adopted was a stale unmarked leftover. *(An earlier version of this note claimed the production
backups had been moved. They had not; the marker rule held. Corrected same day — the log showed a
rename and the conclusion outran the evidence.)*

**Why it matters for v5 anyway.** §6 of the architecture is explicit that users *"install alongside,
and retire v4 themselves"*, so v4 and v5 coexist on one `/share` for as long as the user wants. In
that window, a v5 install that inherits these constants:

1. creates directories named `energy_meter_tracker_backup_<site>` — a **v4 name for a v5 install**;
2. globs `energy_meter_tracker_backup*`, so its ownership scan walks v4's directories;
3. and **adopts v4's directory outright if it happens to be unmarked** — which is exactly the case
   for a user arriving from an older v4, or one whose `/share` was restored from a backup predating
   BL-5. v4 is still their live install at that moment.

The marker rule is what makes this safe, and it is safe only while every v4 install has already
marked its directory. The one population where that fails is the one this spec exists for.

**Proposed, and not yet decided:**

- **v5 uses its own namespace** — `/share/emt_backup_<site>`, with `_SHARE_GLOB` scoped to it — so a
  v5 ownership scan cannot see a v4 directory at all. A shared prefix is what makes the sibling
  question arise; removing it dissolves rather than answers it.
- **v5 performs NO legacy adoption.** Its backup history starts empty by definition: a fresh install
  has nothing to carry across, and v4's backups belong to v4 until the user retires it. Nothing in
  §1's one-directional import requires v5 to take ownership of a folder — the user supplies a
  backup file, which is a different act.
- **The importer does not migrate backups**, only the database. Carrying a backup folder across
  would make the import two-directional in effect: v5 would be mutating v4's state.

**Also worth deciding separately:** the adopt-an-unmarked-legacy-directory rule has no way to
distinguish *"I am the upgraded original"* from *"I am a new sibling that started first"*. A cheap
discriminator exists — an install with **no blocks** cannot be the upgraded original — and it would
have made the soak instance take step 3. That is a v4 fix (BL-5's rule), not a v5 one, and belongs
to the divergence rule's critical-fix bar rather than here.

---

## 8. Mechanics

**Source = an uploaded backup/export file, not a live database.** Reasons: it cannot race a running
old instance; it is reproducible (the same file imports identically); it is testable; and the user
keeps a working old add-on throughout. Importing from a live path would require stopping the old
add-on and would make failures hard to reason about.

**Idempotent and re-runnable.** Re-importing the same file over the same target must converge, not
duplicate — key on (`block_start`, `meter_id`) for blocks and the natural keys elsewhere. A user who
retries after a failure must not end up with doubled history.

> **As built (v18):** stronger — the target is built from nothing each time, so a re-import gives an
> identical database (only `db_uuid` differs, by design). The import **replaces** the live database,
> as a restore always has; it does not merge (decided 10 Oct 2026).

**Bounded and observable.** 135k blocks + 37k segments + 33k sub-meter rows is a large but
tractable import. Chunk it, report progress, and make it resumable. It is a bounded mutating job —
it must take the BL-50 job lock, not run alongside an import or delete.

> **As built (v18):** ONE transaction into a fresh file beside the live one, not chunks. An
> interrupted import leaves a target that is deleted, so all-or-nothing needs no resume. Measured on
> a Mac: 2.8 s for 139k blocks, of which the carry 0.9 s and the gates 1.6 s; the Pi on its SD card
> is still to measure, and `store_meta.import_audit` / the restore log now carry each phase's time.
> It runs inside the restore job, which holds the BL-50 lock.

**All-or-nothing per run.** On failure, the target is left empty (or rolled back), never partially
populated. A half-imported database that *looks* complete is the worst outcome here.

**Order of operations.** `config_periods` → `meters` → `meter_channels` → `blocks` →
`block_segments` → `measured_cost` → dispatch → attribution/ledger → telemetry → allow-listed meta.

**After the import: the whole-history sweep, and what the user is told.** §6 drops
`reprice_history_state`, so v5's unified re-price sweep runs over every imported block on first
start. Measured on the Pi (second soak, 29 Sep 2026): **37,103 blocks from 2023-06-05 in about
17 minutes**. While it ran, every surface was slower — pure reads ×1.9 at the median, a static PNG
p95 0.10 s → 5.3 s — because the sweep saturates a Pi's CPU and SD card. That is contention, not
blocking: 0 SQLite faults, and since the engine-loop fixes no read waits behind the loop.

*Decided 29 Sep 2026:* the sweep runs at **full speed**. Throttling it to ~50% duty would halve the
slowdown and double a one-off job to ~35 minutes, which is the worse trade. Instead the first-run
flow **says so**: once the import completes, a notice that history is being processed and pages
may be slow for a few minutes, cleared when `/api/reprice-history-status` reports done. It belongs
to the importer's acceptance (§9), not to the sweep.

---

## 9. Acceptance — how we know an import is correct

The import is not "done" when it completes; it is done when it **reconciles**. Minimum gates:

1. **Row-count parity** for every CARRY table.
2. **Total-cost parity.** Sum of `imp_cost` / `exp_cost` per billing period matches the source
   exactly. This is the headline invariant — a user's bill history must not move by a penny.
3. **Provenance parity.** Counts by provenance match the source exactly. Reference figures,
   `electricity_main`, 4.5.13 prod-dev (17 Sep 2026):
   `schedule` 57,092 · `settled` 457 · `reconciled` 12 · `corrected` 3. Note these move between
   snapshots as settlement proceeds — the gate is *parity with the source presented*, never a match
   against a figure written here.
4. **Corrections preserved.** Every `rate_corrected=1` block (33 in the reference DB) survives with
   its corrected rate intact.
5. **Segment reconstruction — every block, not a sample.** The dropped legacy columns (§5.2)
   reproduce from segments within rounding for **100%** of imported blocks. This is the gate that
   makes the legacy-column removal safe, and a sample tests the premise weakly; the import is a
   one-shot offline job with the whole database to hand, and the reference set is 78 MB, so there is
   no budget argument for sampling. Report the worst absolute divergence, not just a pass/fail —
   a drift that is within tolerance but trending is the early warning.

   *Method already proven:* the same comparison run across every measured block on 4.5.13 prod-dev
   returned 0 mismatches. Compare at **raw stored precision**; do not compare rounded display
   values, which is how several false results were reached during 4.5.13 development.
6. **Tombstones honoured.** After import, a backfill pass must not re-create any range in
   `deleted_ranges`.
7. **`store_meta` values are JSON-DECODED before use.** `BlockStore.set_meta` JSON-encodes, so the
   column holds `'"4.5.14"'` — **with quotes**. The importer opens the source read-only with plain
   SQL rather than through `BlockStore`, so nothing decodes for it:

   ```
   raw == '4.5.14'               False   ← a plain SELECT + compare fails
   json.loads(raw) == '4.5.14'   True
   ```

   Verified against a real 4.5.14 prod-dev database, 21 Sep 2026. This fails **silently and in the
   worst direction**: a perfectly good database reads as having no marker, and the user is told to
   update an add-on that is already current. `db_uuid` and `supplier` have the same shape — it is a
   property of `store_meta`, not of these keys.

8. **Versions compare NUMERICALLY, never as strings.** Two of four real cases disagree:

   | | string | tuple |
   |---|---|---|
   | `4.5.9 >= 4.5.13` | **True** | False |
   | `2026.9.0 >= 4.5.14` | **False** | True |

   The first is the familiar lexical trap. **The second is the one that bites v5 itself:** a
   v5-stamped database (CalVer `2026.x.y`) compares as *older* than the v4 floor under string
   comparison, so v5 would reject its own backup — and any later reader would too. Parse to a tuple
   of integers.

9. **An import audit record** is written: source `db_uuid`, source `written_by` and `supplier`
   (§6 — the source's `written_by` is recorded here precisely because the new database carries v5's
   own), row counts in/out, and the reconciliation results above.
10. **The user is told the history is being processed** (§8, decided 29 Sep 2026): after a
    successful import, a first-run notice that pages may be slow for a few minutes, shown while
    `/api/reprice-history-status` reports `in_progress` and cleared when it reports `done`.
11. **Held-rate segments fit their kWh** (v16, §5.2 REFIT). After import, no main block with
    `rate_source` in (`corrected`, `reconciled`, `measured`) has segments whose kWh differs from its
    `imp_kwh` by more than 1e-6. The audit record (gate 9) carries the refitted count and the
    reported (non-held) block starts, and both equal a pre-import query of the source. Gate 5
    compares against the refitted segments: where a refit shrank the EV (the house was gone), the
    source's `imp_kwh_ev` is expected to differ, by exactly the shrink.

**Test corpus — and note the floor gives us a much better negative set.** The real databases already
on hand:

| database | expected | why |
|---|---|---|
| 4.3.1 | **refused** | no `written_by`, and no `block_segments` either |
| 4.5.12 production | **refused** | no `written_by`. Previously this was a *positive* case. |
| 4.5.13 prod-dev | **refused** | no `written_by`, despite being one release short |
| **4.5.14 prod-dev** (21 Sep 2026) | **refused** | was the first positive case; below the floor since v8. Now the first *real* below-floor negative — stamped, every column present, and inadmissible because it has not run #473's heal. Keep it. |
| 4.5.15 | **refused** | below the 4.5.16 floor since v9 — has not run #481's heal |
| **4.5.16 dev** (on hand) | **refused** | below the 4.5.17 floor since v10 — has not run the VAT backdate heal |
| 4.5.17 | **refused** | below the 4.5.18 floor since v11 — its heal's work was undone by its own learner |
| 4.5.18 | **refused** | below the 4.5.19 floor since v12 — its heal left 1 Oct's sub-meter rows at 5% |
| 4.5.19 | **refused** | below the 4.5.21 floor since v13 — has not run 4.5.21's heals |
| 4.5.20 | **refused** | below the 4.5.21 floor since v13. Its own fix (v4 #493) carried no heal; 4.5.21's three did |
| `4.5.20-a21` (a v5 dev build) | **refused** | v5's build stamp before 7 Oct 2026, running 4.5.20's code |
| 4.5.21 | **refused** | below the 4.5.22 floor since v17 — has not run the October VAT resync |
| `4.5.21-a22` (a v5 dev build) | **refused** | v5's build stamp before 9 Oct 2026, running 4.5.21's code |
| **4.5.22** | **admitted** | the positive fixture at `FLOOR_VERSION` — to be taken from prod-dev once it runs 4.5.22. Real backups never enter the repo: `tests/test_import_corpus.py` judges every `*.db` in `EMT_IMPORT_CORPUS` by its own stamp (v18). |
| **below** `FLOOR_VERSION` | **refused** | stamped, but from a release older than the floor |

Two of these carry most of the value.

**The 4.5.13 near-miss** is the case the gate exists for: every table and column the importer wants,
and still inadmissible because nothing has attested the heals hold. Under the old three-gate design
it would have passed. Keep a .13 database as a permanent negative fixture.

**The below-the-bar case cannot be a fixed file**, because the bar moves (§1.1, Q9). Generate it —
take the positive fixture and rewrite its `written_by` to a version below the floor — so the test
stays honest whatever `FLOOR_VERSION` is pinned to. A test that hard-codes "4.5.14 is admissible"
silently stops testing anything the day v4 reaches .15.

> **The positive fixture is a release-day artefact** *if the floor moves*. It has moved twice
> (4.5.15 in v8, 4.5.16 in v9, 4.5.17 in v10, 4.5.18 in v11, 4.5.19 in v12), so the positive is now a 4.5.19 database. It moves
> again if a later v4 release carries a heal.

---

## 10. Open questions

1. ~~**Versioning/naming.**~~ **Closed (17 Sep 2026).** Public releases use CalVer `YYYY.M.PATCH`
   (`2026.x.x`), matching Home Assistant's own convention; **`v5` is the internal discriminator for
   everything before the first public release** and never appears in a shipped version string. The
   slug change carries the breaking-change signal that semver would otherwise provide, once.
   *Carry-over:* EMT's own upgrade detector compares the stored version to the running one to arm
   the re-price sweep and the upgrade backup — it must order `2026.9.1 < 2026.10.0` correctly. A
   fresh install has no stored version so the first release is safe; the second is where a naive
   comparator would bite.
2. ~~**`generation_mix`**~~ **Closed 19 Sep 2026: CARRY.** Block-stamped, 774 rows. BL-38 moved the
   donut to `mix_history`, so it is superseded *for display* — but "superseded for one surface" is
   not "reconstructable". A mix stamped onto a block at the moment that block formed cannot be
   re-derived from a rolling series if the two ever disagreed, and §2's invariant is carry what
   cannot be re-fetched. Against 774 rows, dropping it would be a space argument, which is precisely
   the reasoning [the schema notes](EMT-v5_schema_notes.md) §2 rejects.

   *If it really is dead, it gets dropped by the same method as everything else:* the AST
   reachability trace that settles the seven deferred columns (schema notes §5), corroborated across
   every database on hand — not by a table-level guess made here.
3. ~~**`data_source_mode`**~~ **Closed 19 Sep 2026: CARRY** — as the original proposal had it. The
   new install starts in the user's actual mode (cad / api / api+mini) instead of re-running the
   survey. **Moving to EMT is an upgrade from the user's point of view; the meter, the CAD and the
   tariff are all the same on the far side of it.**

   *A DROP argument was floated the same day and is wrong, recorded here so it is not re-proposed.*
   It ran: §3's account gate compares against the target instance's configured credentials, so v5 is
   configured before the import runs and already holds its own mode. **That does not follow.**
   Configured with *credentials* is not the same as having completed *mode discovery* — the account
   stamp needs an account number, whereas the mode needs observation of whether a CAD is actually
   feeding readings. The two are established by different means at different times.

   The argument also invented a risk that existing machinery already handles: a carried mode that no
   longer matches the hardware (a CAD removed between installs) is the same situation as a CAD
   failing on a running v4 install, and v4 already promotes the mode on reconnect. Meanwhile the cost
   of dropping it is real — a window after import in which the new app has no mode and block
   derivation has nothing to work from.

   **The general lesson, since this is the second reclassification to get it backwards:** §6's rule
   of thumb is "does this key say *work already ran*?" `data_source_mode` says *what the user's setup
   is*. Setup facts cross; completion claims do not.
4. ~~**Export format.**~~ **Closed (17 Sep 2026), corrected 19 Sep.** The source is what v4 already
   produces: the **backup zip**, or a **`blocks.db` taken from one**. There is still no purpose-built
   exporter, and the importer validates the source itself rather than trusting a contract (§3).

   *What was wrong:* this answer also asserted "there is no final 4.5.x — 4.5.13 is the last v4 the
   importer will ever see", and drew from that the benefit that "every existing 4.5.13 install is
   already able to migrate, with nothing to ship first". **4.5.14 is shipping**, so nothing is
   already able to migrate — a .13 install must update first. That is a deliberate trade: one
   release of v4 in exchange for a gate that can assert the heals *currently hold* rather than
   inferring it from table presence (§1.1, §3).
5. ~~**Migration window.**~~ **Closed (17 Sep 2026), refined 18–19 Sep.** There is no window and no
   deadline: the two add-ons run side by side in separate containers for as long as the user likes
   (§1). So the importer is not deletable on a *date* — it is deletable when we decide 4.5.14
   sources are no longer supported, which is a product call rather than a migration one. What §1
   now adds is that the decision, whenever it comes, must be **cheap to act on**: E is one module,
   nothing depends on it inward, and no importer-only concept leaks into the domain. An open-ended
   lifetime is acceptable; an un-excisable one is not.
6. ~~**HA entity collision.**~~ **Closed 18 Sep 2026: EMT publishes nothing to Home Assistant.** It
   subscribes to sensors and creates no entities or statistics, so the one shared namespace
   `instance.py` does not arbitrate is never entered. Two add-ons run side by side indefinitely with
   no contention beyond the `/share` backup directory, which `instance.py` already resolves.
7. ~~**`reads`**~~ **Closed 19 Sep 2026: DROP — and it is not "transient", it is dead.** Traced in
   4.5.13 rather than inferred from row counts. The table has a full API —
   `insert_read`, `link_reads_to_block`, `get_reads_for_block`, `get_reads_for_range`,
   `purge_reads_older_than` — and **not one of them has a caller outside the test suite.** Nothing
   has ever written a row. Empty in all three real databases because it is unreachable, not because
   it drains.

   The `# ── Reads (Phase 2+) ──` comment above `insert_read` dates it: a planned raw-read store
   that the block model made unnecessary and that was never removed. It belongs in the unwired-paths
   inventory ([architecture §4](EMT-v5_architecture.md)) alongside the three legacy backfills, not
   in an import question — and it means the DROP costs nothing, because there is nothing to lose.
8. **Attribution state (new, 17 Sep 2026).** The device-history attribution ledger is carried
   (§5.3), but v5 is expected to re-found attribution on a job coordinator. Decide whether an
   attribution run that was *in flight* when the backup was taken is importable at all, or whether
   the gate should refuse it. *Reopened 19 Sep:* an earlier note leaned on the computed
   `heal_level` re-verifying invariants to catch this for free. That mechanism is gone, so the
   question is open again on its own terms — does a half-finished attribution leave the database in
   a state v5 would import wrongly, or does the ledger it carries (§5.3) make it recoverable?
9. ~~**Does v5's required version ever rise?**~~ **Closed 19 Sep 2026, and it was never a policy
   question.** The floor is **the last release of v4**, whatever that turns out to be. It rises
   whenever a post-.14 critical fix adds a heal, and previously-admissible backups do stop being
   admissible — correctly, because v5 has no v4-era heals and would otherwise import history that
   release knows is unrepaired. There is nothing to decide, only something to **supply**.

   *Action, not an answer:* `FLOOR_VERSION` is pinned as the last step before v5's first release,
   from whatever v4 has actually reached by then, and §9's positive fixture is regenerated at that
   version. On the release checklist, not in this paragraph. **Currently `4.5.22`** (v17; was
   `4.5.21` in v13, `4.5.19` in v12, `4.5.18` in v11, `4.5.17` in v10, `4.5.16` in v9, `4.5.15` in v8). The release-checklist step stays: confirm the final v4 release, and capture
   the positive fixture at it.

   *(This question originally read "does the required `heal_level` ever rise?" and the answer is
   unchanged in substance — only the thing being compared is now a version rather than an integer.)*