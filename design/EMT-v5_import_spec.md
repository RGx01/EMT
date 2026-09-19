# EMT v5 — database import specification

*The contract for bringing an existing Energy Meter Tracker (v4.4.0+) database into the new EMT
app. This is the artefact the rest of v5 depends on: the new schema, the charting rebuild and the
legacy-column removal all assume history arrives correctly once, from here.*

**Status:** v3 — revised 18 Sep 2026 against merged 4.5.13. Numbers below are measured from a real
4.5.12 production database (135,482 blocks, 3 meters, ~2 years) unless stated; provenance counts in
§9 are refreshed from a 4.5.13 prod-dev DB.

*Changes in v2: §1 records the side-by-side container decision and closes Q1/Q4; §3 gains a source
**provenance** gate; §5.1 covers the `label='ZERO'` measured row (BL-70, 4.5.13); §5.2 makes the band
vocabulary an explicit importer mapping; §9 gate 5 widens from a sample to every block.*

*Changes in v3: **§5.2's band rule is reversed.** v2 had v5 unifying to `off_peak` / `peak` and
mapping `day → peak`. That was wrong — it flattens a real distinction between two different
concepts. v5 keeps all four labels and performs one era-scoped relabel instead. §1 and Q5 gain the
importer's retirement rule.*

---

## 1. Purpose and shape

v5 ships as a **new add-on with a new slug**, so users get a *fresh install*, not an in-place
upgrade. There is therefore no schema migration inside the new app — instead there is a
**one-directional importer** that reads a v4.4.0+ database and writes the v5 schema.

Consequences that follow from that choice, and which this spec exists to protect:

- The new app **never contains** the pre-4.4.0 one-time heals (BL-33: 7 code sites already tagged
  `# DEPRECATED — remove in 5.0.0`), nor the 15 legacy derived columns, nor the `'measured'`
  naming. Those are known *to the importer only*.

**The importer is a separable unit with a retirement rule.** Everything v4-shaped — the era
detection (§4), the column rules (§5.2), the allow-list (§6) — lives inside it and nowhere else:

> **E is one module. No domain code may import from it, and no domain concept may be
> importer-only. It is removed in the release after 4.5.13 sources stop being supported.**

This is not housekeeping. v4's three legacy backfills (162 lines) became the cautionary tale
*because they were embedded in `engine.py` with no boundary to cut along* — unwiring was the only
cheap move available, so deletion never happened and the code has sat since reading as
load-bearing. A module with one direction of dependency and a stated end date cannot decay that
way: when the date arrives, the deletion is a directory.
- The old add-on keeps working throughout. A failed import costs the user nothing.
- **The two run side by side, in their own containers, indefinitely.** EMT 2026 is not a
  replacement the user must cut over to on a deadline; it is a second add-on that ingests a copy of
  the first one's history. v4's instance isolation already yields contended resources rather than
  fighting for them (`resolve_backup_dir` hands `/share/..._<site>` to whichever instance owns it),
  so the coexistence mechanism exists and is proven in production.
- **The old app gains nothing to support this.** There is no cooperating exporter and no final 4.5.x
  release; 4.5.13 is the last v4 the importer will ever see. The importer therefore reads artefacts
  that already exist and validates them itself (§3).

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

The third clause matters as much as the first two. A 4.4.x source has **no settled provenance at
all** (§4); the importer must represent that history as *unsettled*, not quietly mark it settled.
We spent 4.5.12 fixing exactly that class of bug — a block claiming to be settled when it wasn't.

---

## 3. Source eligibility — the gate

The importer's first act is to decide whether a source database is admissible. **Advertised but
not enforced today**: the repo README carries the v4.4.0 upgrade notice, but no code refuses an
older database. The importer must enforce it.

**Admissible** iff all hold:

1. `block_segments` table **exists**. This *is* the 4.4.0 line — the priced-segment model arrived
   in 4.4.0. (Verified: a 4.3.1 database has no such table; a 4.5.12 one does.)
2. The 4.4.0 migration **completed**: `store_meta.reprice_history_state` parses and has
   `done == true`. Real value observed: `{"done": true, "scope": 2, "swept": 36355, "healed": 0,
   "skipped": 0, "completed_at": "..."}`.
3. `count_blocks_needing_reprice() == 0` — belt and braces against a marker written optimistically.
4. The source account matches the target instance's account, per §7.
5. **The source is a backup, not a live database.** The accepted inputs are a backup **zip** as
   written by `_backup_to_share`, or a `blocks.db` **taken from one**. A file copied out of a
   running container is not admissible: v4 holds the database in WAL mode and checkpoints only at
   startup, so a `blocks.db` lifted mid-run can have committed history sitting in a `-wal` sibling
   that the main file does not yet contain. The importer must not silently drop the user's most
   recent days.

   Enforced as: if a `-wal` sibling is present and non-empty → **refuse**, naming the file and
   directing the user to take a proper backup. If it is absent, open the database **read-only**
   (`file:…?mode=ro`) and verify `PRAGMA quick_check` and `PRAGMA foreign_key_check` are clean.
   The importer never opens a source read-write, and never checkpoints someone else's database —
   recovering a hot WAL would mean writing to the live app's file from another container.

> **Do not use `schema_version`.** It reads `1` on both a 4.3.1 and a 4.5.12 database — it is not
> a version discriminator.

**Refusal must be explicit and actionable**, never a partial import:

- *no `block_segments`* → "This database predates v4.4.0. Upgrade the old add-on to 4.4.x, let the
  one-time migration finish, then import."
- *migration incomplete / blocks still needing reprice* → "The v4.4.x migration hasn't finished.
  Start the old add-on, let it complete, then export again." (Partially-migrated history is the
  dangerous case: the table exists, so a naive presence check would pass it.)
- *hot WAL present* → "This looks like a copy taken while Energy Meter Tracker was running, and
  some of your most recent half-hours are not in it yet. In the old add-on take a backup, then
  import the zip it writes."
- *integrity check fails* → "This database is damaged and cannot be imported safely." Never
  attempt repair: the user still has the working original.
- *account mismatch* → §7.

---

## 4. Schema eras within the supported range

The supported range spans a discontinuity that changes what can be carried:

| Era | `block_segments` | `rate_source` | `measured_cost` | Meaning |
|---|---|---|---|---|
| **4.4.0 – 4.5.4** | yes | **no** | **no** | Segments exist; **no settled provenance exists at all**. All history is schedule/estimated. |
| **4.5.5 +** | yes | yes | yes | Settled costs, bands, splits and corrections are present and carryable. |

Detect by column/table presence, not by a version string.

**Rule:** a 4.4.x import produces history whose provenance is `schedule` (or the v5 equivalent) —
**never** `settled`. It is legitimate for such history to be re-settled later by the normal
settlement path where Octopus still serves the data; it is not legitimate to assert it was settled
at import time.

Observed provenance distribution on the 4.5.12 reference database (`electricity_main`):
`schedule` 56,932 · `measured` 360 · `corrected` 33 · `reconciled` 20 · null 7.

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
`config_period_id`, `interpolated`, `imp_kwh`, `imp_kwh_api`, `exp_kwh`, `exp_kwh_api`,
`imp_read_start/end`, `exp_read_start/end`, `standing_charge`, `carbon_g`, `carbon_intensity_g`,
`source`, `is_provisional`, `finalised_from_cad`, `needs_review`, `review_reason`,
`review_dismissed`, `derivation_id`, `rate_corrected`.

**CARRY — authoritative totals:** `imp_rate`, `imp_cost`, `exp_rate`, `exp_cost`. `imp_cost` on a
settled block **is the bill** and must not be recomputed from segments; segments intentionally
differ by a sub-penny sliver which the remainder absorbs.

**CARRY+REMAP — provenance:** `rate_source` → v5 naming (`'measured'` → `'settled'`, BL-61(5)).
Absent in 4.4.x ⇒ write the unsettled/schedule value, per §4. Fold `rate_reconciled` into the
provenance model rather than carrying a second flag.

**DROP — derived, reconstructable from segments** (13 columns; this is the legacy-column removal):
`imp_kwh_grid`, `imp_kwh_remainder`, `imp_cost_remainder`, `imp_provisional` (duplicate of
`is_provisional`), `imp_cost_exc`, `imp_rate_exc`, `standing_charge_exc`, `exc_source`,
`imp_kwh_ev`, `imp_cost_ev`, `imp_rate_ev`, `imp_ev_band`, `imp_home_band`.

> These are exactly the columns whose drift against segments caused the recurring bug class. They
> are dropped *because* `block_segments` is carried verbatim — the information is not lost, only
> de-duplicated. **Dependency:** every v5 surface must read segments first (BL-27) before this is
> safe.

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

**CARRY:**

- `vat_calendar` — irreplaceable local calendar (`[["2023-06-02", 0.05]]` in the reference DB).
- `settings` — user preferences (e.g. `{"bill_rounding_summary": true}`).
- `recorder_attribution_runs` — the attribution back-out ledger; pairs with `historical_derivation`.
- `export_settlement_expected` — whether the account has an outgoing agreement.

**REGEN:** `db_uuid` — the v5 database is a new identity. Record the *source* uuid in the import
audit record (§9) for traceability.

**DROP (one-time heal / sweep markers — must not suppress v5 passes):** `reprice_history_state`
(consumed by the §3 gate, not carried), `carbon_backfill_state`, `carbon_backfill_done`,
`exc_backfill_state`, `iog_split_backfill_state`, `stale_exc_repair_state`,
`stale_iog_split_repair_state`, `region_probe_clobber_done`, `export_zero_fill_v1`,
`import_reprice_queue`, `region_reconcile_pending`, `schema_version`.

**`kraken_state` — DROP by default.** All 21 keys are runtime/sweep state
(`smb_rate_repair_done_v2`, `smb_ev_resplit_done`, `smb_device_recost_done`,
`verify_pricing_cursor`, `import_health_summary`, `last_poll_utc`, …). The two worth considering:

- `kraken_account_number` — **CARRY**, it is the account stamp (§7).
- `data_source_mode` — **CARRY**, so the new install starts in the user's actual mode (cad / api /
  api+mini) rather than re-running the survey. *Proposal, confirm.*

> Rule of thumb: if a key's purpose is "this sweep already ran", it must **not** cross — the v5
> equivalent sweeps have not run.

---

## 7. Account identity and multi-instance

Multi-property users run several instances via the repo-URL workaround; **this must keep working
in v5**, so the importer cannot assume a single global database.

- **Account stamp.** Reuse the existing guard (#357): a database is stamped to its Octopus account
  on first discovery. The importer **refuses** a source stamped to a different account than the
  target instance's configured credentials — "this database belongs to account A…7; these
  credentials are for A…3". This is the single most likely user error when juggling properties.
- **Instance identity.** Carry `instance_name`; do not carry backup-directory ownership — the new
  instance negotiates its own share directory on first run (`resolve_backup_dir` already handles
  "ideal dir taken by another instance").
- **One import per instance.** The importer targets the instance it runs in. Importing property B's
  database into property A's instance is the failure this guard exists to prevent.

---

## 8. Mechanics

**Source = an uploaded backup/export file, not a live database.** Reasons: it cannot race a running
old instance; it is reproducible (the same file imports identically); it is testable; and the user
keeps a working old add-on throughout. Importing from a live path would require stopping the old
add-on and would make failures hard to reason about.

**Idempotent and re-runnable.** Re-importing the same file over the same target must converge, not
duplicate — key on (`block_start`, `meter_id`) for blocks and the natural keys elsewhere. A user who
retries after a failure must not end up with doubled history.

**Bounded and observable.** 135k blocks + 37k segments + 33k sub-meter rows is a large but
tractable import. Chunk it, report progress, and make it resumable. It is a bounded mutating job —
it must take the BL-50 job lock, not run alongside an import or delete.

**All-or-nothing per run.** On failure, the target is left empty (or rolled back), never partially
populated. A half-imported database that *looks* complete is the worst outcome here.

**Order of operations.** `config_periods` → `meters` → `meter_channels` → `blocks` →
`block_segments` → `measured_cost` → dispatch → attribution/ledger → telemetry → allow-listed meta.

---

## 9. Acceptance — how we know an import is correct

The import is not "done" when it completes; it is done when it **reconciles**. Minimum gates:

1. **Row-count parity** for every CARRY table.
2. **Total-cost parity.** Sum of `imp_cost` / `exp_cost` per billing period matches the source
   exactly. This is the headline invariant — a user's bill history must not move by a penny.
3. **Provenance parity.** Counts by provenance match the source exactly, with 4.4.x sources showing
   **zero** settled. Reference figures, `electricity_main`, 4.5.13 prod-dev (17 Sep 2026):
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
7. **An import audit record** is written: source uuid, source version era, row counts in/out,
   and the reconciliation results above.

Test corpus: the real databases already on hand — a 4.5.12 production DB, a prod-dev DB, and a
4.3.1 DB as the **negative** case (must be refused at §3).

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
2. **`generation_mix`** (block-stamped, 774 rows) — carry, or drop in favour of `mix_history` now
   that the donut reads the latter (BL-38)?
3. **`data_source_mode`** — carry (skip the survey) or re-run setup on first start?
4. ~~**Export format.**~~ **Closed (17 Sep 2026).** The source is what v4 already produces: the
   **backup zip**, or a **`blocks.db` taken from one**. There is no purpose-built exporter and no
   final 4.5.x — 4.5.13 is the last v4 the importer will ever see. The cost is that the importer
   validates the source itself rather than trusting a contract (§3 gate 5); the benefit is that
   every existing 4.5.13 install is already able to migrate, with nothing to ship first.
5. ~~**Migration window.**~~ **Closed (17 Sep 2026), refined 18 Sep.** There is no window and no
   deadline: the two add-ons run side by side in separate containers for as long as the user likes
   (§1). So the importer is not deletable on a *date* — it is deletable when we decide 4.5.13
   sources are no longer supported, which is a product call rather than a migration one. What §1
   now adds is that the decision, whenever it comes, must be **cheap to act on**: E is one module,
   nothing depends on it inward, and no importer-only concept leaks into the domain. An open-ended
   lifetime is acceptable; an un-excisable one is not.
6. ~~**HA entity collision.**~~ **Closed 18 Sep 2026: EMT publishes nothing to Home Assistant.** It
   subscribes to sensors and creates no entities or statistics, so the one shared namespace
   `instance.py` does not arbitrate is never entered. Two add-ons run side by side indefinitely with
   no contention beyond the `/share` backup directory, which `instance.py` already resolves.
7. **`reads`** is empty in the reference DB — confirm it is always transient before DROP.
8. **Attribution state (new, 17 Sep 2026).** The device-history attribution ledger is carried
   (§5.3), but v5 is expected to re-found attribution on a job coordinator. Decide whether an
   attribution run that was *in flight* when the backup was taken is importable at all, or whether
   the gate should refuse a source with an unfinished attribution job the way §3 refuses an
   unfinished 4.4.0 migration.
