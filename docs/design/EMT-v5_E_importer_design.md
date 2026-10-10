# EMT v5 — Workstream E: the importer

*How the [import spec](EMT-v5_import_spec.md) is built. The spec says **what** a v4 database
becomes; this says **where the code lives, how it runs, in what order the rules apply, and in what
order the work lands**. Where the two disagree, the spec wins until it is amended, and every
amendment this design needs is listed in §9.*

**Status:** agreed, 10 Oct 2026 (§11). Architecture step 7. Decided by the user on 10 Oct: the house
`off_peak → night` remap is **kept** (spec §5.2), `rate_source 'measured' → 'settled'` is **done**
(spec §5.2, BL-61(5)), and this document comes first. Figures are measured read-only on the
October backups on hand (blocks-20 to blocks-24, the PianSom database, dev v4 011026) unless stated.

---

## 0. What exists today, and what is missing

A v4 backup can already be restored into v5: `web/server.py:_restore_worker` takes the job lock,
pauses the engine, runs `import_gate.check` (spec §3 phase 1, gates 1–4) on the candidate, takes a
`pre_restore` safety backup and swaps the file in. Until step 5, v5's schema is v4's, so the swap
is enough and the soak flow (`.build/soak_prepare_db.py` → restore → soak) depends on it.

What does not exist is everything in spec §5, §6 and §9 that changes or checks the data on the way
through: the provenance rename and the fold of `rate_reconciled`, the EV segments for blocks that
have none (v14), the held-rate refit (v16), the band relabel, the `store_meta` / `kraken_state`
allow-lists, the regenerated `written_by` and `db_uuid`, the audit record and the reconciliation
gates. Today a restored v4 database brings across every heal marker and sweep cursor it holds.

---

## 1. One package, one direction

```
v4import/
  __init__.py     import_v4(candidate, target) -> Report      the only entry point
  gate.py         import_gate.py, moved                      spec §3 phase 1
  rules.py        the declarative tables                     spec §5, §6
  transforms.py   the ordered rewrites                       spec §5.2
  verify.py       the reconciliation gates                   spec §9
```

**The rule is the spec's §1.2, made checkable:** no module outside `v4import/` imports from it
except the restore worker, through `import_v4` and `gate.check`. A test walks the AST of every
other module and fails on any other import. When v4 sources stop being supported, the package is
deleted and the restore worker loses one branch.

`import_gate.py` moves into the package because everything in it is v4-shaped: the floor, the
`written_by` stamp, the pence check. Gate 1 (no hot `-wal`, `quick_check` clean) applies to v5's
own backups too, so the restore worker keeps calling it directly for those. It is generic SQLite,
not a v4 concept.

Domain code may not hold importer-only concepts. The one place that matters is the band
vocabulary (§5): v5's native writers have to use `night` themselves, or the relabel would leave the
same tariff era labelled two ways.

---

## 2. Where it runs: inside the restore job

The importer is a step in the existing restore, not a second job. The restore already has
everything spec §8 asks for: the job lock (`@job("restore", depends_on=("import",))`), the engine
pause, the safety backup, the atomic swap and progress through `_restore_job`.

```
candidate (zip's blocks.db, flat file, or upload)
   │
   ├─ classify:  store_meta.emt_schema present?  ── yes ──► v5 backup: gate 1 only, swap as today
   │                     │ no
   │                     ▼
   ├─ v4 gate:   v4import.gate.check  (gates 1–4)  ── refusal ──► restore ends, nothing touched
   │
   ├─ import:    v4import.import_v4(candidate, DATA_DIR/import_target.db)
   │               carry → transform → verify            (one transaction, §3)
   │               └─ verify fails ──► target deleted, restore ends with the report's reason
   │
   ├─ safety backup, stop the stores        (unchanged)
   └─ swap import_target.db into place      (the existing swap, given a different file)
```

**The schema marker is new.** v4 has no reliable discriminator: spec §3 rules out
`schema_version` (it reads `1` everywhere), and `written_by` cannot tell a v5 dev build from v4,
because `4.5.22-a23` is a v5 build running 4.5.22's schema. So v5 writes `store_meta.emt_schema`
when it **creates** a database (`BlockStore`, only when the file has no tables yet), and the
importer's target gets it that way. A database without it is v4-shaped, whoever wrote it. **v5
dev databases made before the marker are v4-shaped and go through the importer**, which is
correct: they hold `measured` rows and the legacy columns.

*Built (PR 2):* the value is a **revision** (`block_store.EMT_SCHEMA`, 1), not a constant `5`. Each
change to what v5 stores raises it (the vocabulary in PR 3, the drop in PR 5). Until the first
release, a v5 backup at any other revision is refused: an older one with "restore a backup from
the v4 add-on instead", a newer one with "update EMT". A dev database marked at an older revision
holds what that build stored, and the importer only reads v4 shapes. At the first release the
revision becomes the baseline its migrations start from.

---

## 3. Mechanism: a fresh target filled from an attached source

```python
target = BlockStore(DATA_DIR + "/import_target.db")   # v5's own DDL and migrations, empty
conn.execute("ATTACH DATABASE 'file:<candidate>?immutable=1' AS src")
with conn:                                             # ONE transaction
    for table, rule in rules.TABLES:                   # spec §8's order
        conn.execute(rule.insert_select())             # INSERT INTO main.t (...) SELECT ... FROM src.t
    transforms.run(conn, report)                       # §4, in order
    verify.run(conn, report)                           # §6; raises on any failure
```

Each choice:

- **Fresh target, not a rewrite in place.** The target's schema is exactly v5's, because v5
  created it, and there is no `ALTER TABLE … DROP COLUMN` path to maintain beside `BlockStore`'s
  DDL. The source is never opened read-write (spec §3 gate 1); it is attached `immutable`, as
  `import_gate` already opens it.
- **Every source column must have a rule.** `rules.py` names every column of every source table
  as CARRY, REMAP (an SQL expression) or DROP. An unexpected column, or an unexpected table,
  **aborts** the import and names it. It is never carried or dropped silently. That is how a column
  v4 added after this document reaches a human instead of the bin.
- **Row ids are carried verbatim.** `generation_mix.block_id` and `reads.block_id` reference
  `blocks.id`, `meter_channels.meter_id` references `meters.id`, and
  `historical_derivation.superseded_by` references itself (`PRAGMA foreign_key_list` on blocks-20).
  `sqlite_sequence` follows from the carried ids. A target that renumbers would break every one of
  those links.
- **One transaction, not chunks.** Spec §8 asks for chunked, resumable work. A single transaction
  into a file that is not yet live gives all-or-nothing more cheaply: an interrupted import leaves
  a target that is simply deleted, and a retry starts again. The plain carry of blocks-20 (139,073
  blocks, 37,957 segments, 80 MB) into a fresh file takes **0.3 s** on a Mac with no indexes. The
  Pi on its SD card is the number that matters. The acceptance (§8) times it there, and the design
  changes only if that time is long enough to matter. Progress is reported per table through
  `_restore_job["step"]`.
- **Re-importing the same file gives the same database.** Spec §8 asks for convergence. Here the
  target is rebuilt from nothing every time, so the second import is identical to the first,
  which is stronger. The one thing that differs is `db_uuid`, which is new by design (spec §6).

### Replace, not merge — the decision this needs

Spec §3a says a user who set up v5 fresh, ran it for a week, and then imports must not be blocked.
The import **replaces** the live database, as a restore always has (decided 10 Oct 2026), and the
`pre_restore` safety backup holds the replaced week. It does not merge.

The recommendation rests on what the week holds. v5 and v4 run side by side and meter the same
house, so a v4 backup taken after the week covers it, and with v4's provenance. A merge would have
to decide, block by block, which of two independently priced records of the same half-hour wins.
It would also need its own reconciliation rules, for a case that taking the v4 backup later avoids
altogether. **If the user wants a merge, it is a separate piece of work after this one.** §9 lists
the spec change.

---

## 4. The transforms, and why their order is fixed

The carry (§3) writes every row as the source holds it, apart from the REMAP expressions. Then,
in this order, on the target:

| # | transform | spec | why here |
|---|---|---|---|
| 1 | **Provenance.** `rate_source` `'measured'` → `'settled'`. Where `rate_reconciled = 1` and the word is `'schedule'` or NULL, write `'reconciled'`. | §5.2 | First: 3 and 4 select on the v5 words. |
| 2 | **EV segments from the columns**, for a main block with `imp_kwh_ev > 0` and no segments (`pricing_segments.segments_from_legacy`). | §5.2 v14 | Before 3: a refit needs segments to fit. Before the columns go. |
| 3 | **Held-rate refit**, for a main block with `rate_source IN ('corrected','reconciled','settled')` whose segments miss `imp_kwh` by more than 1e-6 (`pricing_segments.refit_to_kwh`). Any other block that misses is **reported**. | §5.2 v16 | After 2, so a block given segments in 2 is checked too. Before 4: a house-less block gains a house segment whose band comes from `imp_home_band`, and that word may need relabelling. |
| 4 | **Band relabel**: house `off_peak` → `night` inside the SMB era (§5). | §5.2 | Last of the block rewrites, so it sees every house segment 2 and 3 wrote. |
| 5 | **Meta.** Carry the allow-listed keys (§7). Leave `written_by` and `db_uuid` out: the restore re-runs `engine_startup`, which stamps v5's version and mints a native `db_uuid` (spec §6 REGEN by v5's own mechanism). `emt_schema` comes with the target (§2). The audit record (`store_meta.import_audit`) is written after verify. | §6, §9.9 | After verify has passed, so the audit record holds its results. |

Each transform is one function, reads only the target, and adds a count to the report, so the audit
record says how many rows each rule touched. **None is a heal**: there is no marker, no version
gate and no second run. They are a translation applied once, to the importer's copy, which is how
the spec's §5.2 describes the relabel.

---

## 5. The `night` relabel needs two things the spec does not say

**(a) The SMB era is not recorded in the file.** The spec scopes the relabel to "the covering
agreement is IOG-SMB". A v4 database does not hold the agreement history. `meter_channels` keeps
the tariff code from setup (`INTELLI-VAR` on blocks-20, which has been on IOG-SMB since late
August), and v4 reads the agreements from the API at run time. The import runs with no credentials
on some paths (spec §3), and the same file must import the same way every time (spec §8). So the
era has to come from the file.

The file-only signal is the **four-bucket settlement**: a `measured_cost` row with `home_rate` or
`ev_rate` set exists only where Octopus answered with the IOG-SMB buckets. The era is taken to
start at the earliest such row, which falls on 26 Aug 2026 on blocks-20 and 25 Aug 2026 on PianSom.
Its limit: an account that later *left* IOG-SMB would be relabelled to the end of its history. No
account on hand has done that, and the report records the era it used, so the case is visible if it
arises.

**(b) v5's own writers still say `off_peak`.** If the importer relabels and v5 does not, the
problem the spec warns about appears at once: an imported half-hour says `night` and a natively
priced one says `off_peak`. It does not wait for new data either. The whole-history re-price sweep
runs over every imported block on first start (spec §8), so wherever it rewrites segments, it would
write `off_peak` back. The house low band is labelled in at least `iog_cap.py:317` (the SMB cap
split), `engine.py:12157` (the measured apply), `:12570`, `:12634` and `:12710` (reconcile), and
`:772` (the refit's house band). The one reader that tests a house band by name,
`chart_emit.py:86`, accepts only `off_peak` and `peak`. So **the relabel ships with the vocabulary
change (§10, PR 3)**: v5's writers emit `night` for the house low band inside the SMB era, and its
readers accept it. The era test natively is the one v5 already has (`_import_is_smb_capped`, and the
agreement history for past blocks). Measured on blocks-20, 15,225 house `off_peak` segments exist,
about 420 a month both before the SMB era and inside it.

> **Superseded 10 Oct 2026: the relabel moves to ROADMAP BL-81.** The sentence above assumed v5
> has an era test for past blocks. It does not. A trace of every writer of a house band found about
> ten paths, and each decides "SMB" from the **current** tariff (`ev_device_*` schedules,
> `_import_is_smb_capped`), not the block's date. The whole-history sweep also rebuilds segments and
> discards the stored band. So the native half needs a per-block era first. Until BL-81, both
> v5 and the importer keep house `off_peak` in every era, and transform 4 is not built.

*Also to trace before PR 3, not to assume:* `chart_emit.py:86` does not count house `day` as the
bill having spoken. That may be deliberate (the chart's house line may only want the low band), or
a gap. Settle it by reading the introducing commit (memory: trace v4 intent before deleting).

---

## 6. Verification: the spec's gates, run before the swap

`verify.run` runs inside the transaction, with the source still attached, so every gate compares
target to source directly. Any failure raises, the transaction rolls back, the target is deleted and
the restore ends with the gate's message. The live database has not been touched.

| spec §9 | how |
|---|---|
| 1 row counts | per CARRY table, `COUNT(*)` main vs src. `measured_cost` includes its `label='ZERO'` rows (spec §5.1). |
| *carried exactly* | the broad gate: for every carried table, the carried columns `EXCEPT` in both directions is empty. Columns a transform rewrites leave it and are checked by their own gate (`verify.REWRITTEN`). The named gates restate the spec's invariants one by one, so each keeps its message once the transforms arrive. |
| 2 cost parity | every block's `imp_cost`, `exp_cost` and `imp_cost_exc` equal the source's, joined on the carried id, exact at stored precision. Per block is finer than any period, so it implies period parity. *(Drafted as per day; built per block, 10 Oct 2026, because it is stronger and no slower.)* |
| 3 provenance | counts by word, after mapping the source through transform 1's rule in SQL. |
| 4 corrections | every `rate_corrected = 1` source block exists with the same `imp_rate`, `imp_cost` and `rate_source`. |
| 5 segments reproduce the dropped columns | every main block: EV kWh and £ from segments vs `src.imp_kwh_ev` / `imp_cost_ev`; the derived ex-VAT rate vs `src.imp_rate_exc`. Report the worst divergence. Blocks refitted in 3 are compared against the refit (spec gate 11's note). **Lands with the drop (PR 5)**; before it, the columns are still there and the comparison is with themselves. |
| 6 tombstones | `deleted_ranges` carried row for row. *The backfill-does-not-recreate half is the engine's behaviour, tested there, not at import.* |
| 7, 8 | already in the gate (`_meta` decodes, `parse_version` is numeric). Kept as tests. |
| 9 audit record | written in transform 5. |
| 10 first-run notice | client work (step 6). Not the importer's. |
| 11 held-rate fit | no held main block's segments miss `imp_kwh` by more than 1e-6, and the refitted and reported counts equal a pre-import query of the source. |

---

## 7. `store_meta` and `kraken_state`: every key on hand

Spec §6 says DROP by default and CARRY by allow-list, and lists 21 `kraken_state` keys. The
backups on hand hold 29 `kraken_state` and 25 `store_meta` keys between them. Each is below, with
whether v5 still reads it. The rule is the spec's: **setup facts cross; claims that work already
ran do not.** One refinement: a marker that stops v5 re-running a pass that would **overwrite
carried user data** is a setup fact in effect, and crosses.

**CARRY**

| key | table | why |
|---|---|---|
| `vat_calendar`, `settings`, `recorder_attribution_runs`, `export_settlement_expected` | meta | spec §6 |
| `supplier` | meta | the source's answer. The audit record also keeps it. |
| `kraken_account_number`, `data_source_mode` | state | spec §6, §7 |
| `api_import_oldest_import/export`, `api_import_done_import/export` | state | *How far back the history reaches* is a fact about the carried blocks, not a sweep. Dropped, they would make v5 offer the API history import again over history it holds. **To verify in PR 2:** what `api_import` does when they are absent and the blocks are present. |
| `region_probe_clobber_done` | meta | v5 still runs the one-time region overwrite (`engine.py:9622`). Re-run on an import, it would overwrite the carried `config_periods` regions from the API. |
| `billing_source` | state | the user's choice of what settles a block (*added in PR 2, from v5's own keys*) |
| `carbon_paused` | meta | the user's carbon kill switch (*added in PR 2*) |
| `import_cost_coverage` | meta | what the API import found about this history, per tariff era (*added in PR 2*) |

**REGEN**: `written_by` (v5's own version), `db_uuid` (new; the source's goes in the audit
record), `emt_schema` (new, §2).

**DROP, v5 still reads them, and re-running is safe**

| key | what v5 does without it |
|---|---|
| `last_poll_utc` | the DCC poll's first-run window (`backfill_days`), with its re-check overlap |
| `last_settlement_sweep_utc`, `verify_pricing_cursor`, `verify_pricing_summary`, `import_health_summary`, `import_run_status`, `import_pricing_diag` | runtime state, rebuilt |
| `import_gaps_import/export` | recomputed (to verify alongside the `api_import_*` keys) |
| `pre_live_snapshot_done` | takes its one snapshot |
| `export_zero_fill_v1` | fills blank export slots on imported days again; idempotent |
| `carbon_backfill_state`, `import_reprice_queue`, `region_reconcile_pending`, `reprice_history_state` | spec §6. The whole-history sweep then runs (spec §8). |
| `measured_vat_resync` (4.5.22, not yet on hand) | v5's forward resync starts from the data. If v4 finished, v5 finds no disagreeing block and marks itself done. If not, it waits for a new hint, as v4 would have. |
| `schema_version` | spec §3 and §6. The target keeps the one v5 wrote. |
| `data_epoch` | the restore bumps the epoch above the live one anyway (C §2a) (*added in PR 2*) |
| `preimport_sites_applied` | a marker the API import clears when it finishes (*added in PR 2*) |

**DROP, no v5 reader** (v4 heal and sweep markers, which is what v5 carrying no heals means):
`band_rate_snap_done_v2`, `bl19_grid_invariant_sweep_done`, `dispatch_house_refit_done`,
`ev_bucket_refetch_done`, `ev_priority_resplit_done`, `implausible_sub_block_sweep_done`,
`register_glitch_sweep_done`, `smb_device_cost_clip_done`, `smb_device_recost_done`,
`smb_ev_resplit_done`, `smb_rate_repair_done`, `smb_rate_repair_done_v2`,
`vat_backdate_heal_done_v3`, `vat_backdate_heal_done_v4`, `bill_resettle_v1_done` …`_v4_done`,
`carbon_backfill_done`, `exc_backfill_state`, `iog_split_backfill_state`,
`stale_exc_repair_state`, `stale_iog_split_repair_state`, `segment_backfill_state`.

**A key ending `_done` or `_done_v<n>` is dropped** as a v4 completion marker. That is the
spec's own test for a key that must not cross, written as a rule, and it covers markers no backup on
hand still holds (`corrected_device_exc_done`, `vat_backdate_heal_done`, `…_v2`). An explicit
entry wins, which is how `region_probe_clobber_done` crosses. *Checked 10 Oct 2026* against every
key v4 has written in its whole git history, every key in the backups, and every key v5 reads:
each has a rule, and none ending `_done` is a setup fact.

**Any other unknown key aborts the import**, as an unknown column does (§3). A key v4 adds after this
document is classified by a person, not by default.

---

## 8. Acceptance for the importer itself

Beyond spec §9's gates, which run on every import:

- **The corpus.** The spec's §9 table, as tests. Every below-floor database is refused with its
  message. A generated below-floor copy is refused. The 4.5.22 positive fixture imports, reconciles,
  and its audit record matches a pre-import query. *(That fixture is still to be taken from
  prod-dev.)*
- **Discriminating tests per transform.** Each transform has a test that fails with the transform
  removed: a `measured` row arrives as `measured`, a no-segment EV block arrives with no EV, a
  held block's segments still miss, a house SMB-era segment says `off_peak`. The skeleton's
  identity carry passes the gates and fails these, which is the check that they discriminate.
- **The boundary test** (§1): no module outside `v4import/` imports from it.
- **Unknown column, table and key** each abort with the name.
- **A Pi soak**: restore a real 4.5.22 backup through the importer and time it on the SD card
  (§3). Then run the usual soak, and `scan-log` from the restore time.

---

## 9. Spec amendments this design needs

To be written into the import spec as **v18** when the PRs that need them land, not before:

1. **§5.2 the relabel's era:** the earliest four-bucket `measured_cost` row, from the file (§5a).
2. **§5.2 the relabel's partner:** v5's native writers emit `night` too (§5b). Without it the
   relabel does what the spec says it prevents.
3. **§6 the key table:** §7's classification replaces the 21-key paragraph, including the four
   `api_import_*` keys and `region_probe_clobber_done` crossing.
4. **§8 mechanics:** one transaction into a fresh target, not chunked and resumable. A re-import
   gives an identical database. The import **replaces** the live database (§3).
5. **§3 classification:** `store_meta.emt_schema` distinguishes a v5 backup from a v4-shaped one.

---

## 10. Order of work

The order agreed on 10 Oct was: this document, the vocabulary, the importer module, the
acceptance, the schema drop, the restore wiring. **This design changes one thing: the importer
reaches the restore path before the vocabulary changes.** The reason: once v5 reads only
`settled`, a v4 database restored the way it is today keeps its `measured` rows, and v5 would no
longer read them. The soak flow, and any dev install restored that way, would lose every settled
block. Wiring the importer first, with the rename as one of its rules from the start, keeps every
step usable on the Pi.

| PR | contents | spec gates live |
|---|---|---|
| 1 | this document | — |
| 2 | **skeleton, wired into the restore**: `v4import/` with the gate moved in, the classification and `emt_schema`, the fresh-target carry with every column and table classified (identity rules, since the schema has not moved yet), the §7 key allow-lists, REGEN, the audit record and the boundary test | 1, 2, 3 (pre-rename), 4, 6, 9 |
| 3 | **vocabulary**: `'measured'` → `'settled'` (28 sites in `engine.py` and `block_store.py`, `.build/bill_conformance.py`, 24 lines in tests) with transform 1 and the reconciled fold; `EMT_SCHEMA` 2. *House `night` and transform 4 split out to ROADMAP BL-81 (10 Oct 2026): v5 has no per-block tariff era to label against.* | 3 |
| 4 | **segments**: transforms 2 and 3. *Built 10 Oct 2026: on seven real databases, 10 blocks / 28.43 kWh of EV given segments on one install, 12–13 held blocks refitted on each production-like one, none reported.* | 11 |
| 5 | **the schema drop**: the 10 columns and `rate_reconciled` leave `BlockStore`'s DDL and v5's code (about 330 production references), `ev_split`'s LEGACY fallback goes, and the rules become DROP. *Split 10 Oct 2026, after an inventory of every reference, into four, each removing its columns completely (schema, code, importer rule, tests) and raising `EMT_SCHEMA`:* | 5 |
| 5a | the remainder pair and `rate_reconciled`: plumbing only, since no surface reads the stored values (`EMT_SCHEMA` 3) | — |
| 5b | `imp_rate_exc`, `standing_charge_exc`, `exc_source`: six readers that load `imp_rate_exc` from the database take a derived rate instead (`imp_cost_exc ÷ imp_cost` as the exc/inc ratio, else the VAT calendar), and the held-rate re-run stops writing back an `imp_cost_exc` stale against its new cost | — |
| 5c | the five EV-split columns: finalise and the settlement re-run write the segments in the block's own transaction (a failed segment write used to leave the split only in the columns), bands are read from the segments, the split writers become segment-only, and `ev_split`'s LEGACY fallback goes. *Built 10 Oct 2026, with gate 5 (`verify.split_reproduced`).* | 5 |
| 5d | *folded into 5c (10 Oct 2026):* gate 5 landed with the EV columns it checks, and the documented counts were already right | — |
| 6 | positive fixture at 4.5.22, Pi timing, spec v18. *Corpus test, timings and v18 in #129. The 4.5.22 fixture (prod-dev, 10 Oct 2026) imports with every gate holding in 2.8 s on a Mac; a key no earlier backup held, `corrected_device_exc_done`, dropped under the `_done` rule as intended. The Pi timing is still to take.* | all |

Each PR is a normal `feat/…` or `chore/…` from `dev`. PR 3 is the first to change what v5 writes
natively, and PR 5 is by far the largest. Dev databases made before PR 3 are re-restored from a v4
backup rather than migrated in place. v5 is unreleased, and the importer is the migration (§2).

---

## 11. Decided

Decided by the user, 10 Oct 2026:

1. **Replace, not merge** (§3). The import replaces the live database, and the `pre_restore`
   safety backup holds what it replaced.
2. **The SMB era runs from the earliest four-bucket `measured_cost` row** (§5a), with the stated
   limit recorded in the report.
3. **The importer is wired into the restore before the vocabulary changes** (§10).
