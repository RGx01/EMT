# EMT v5 — Delete by channel, softly

*Draft, 18 Oct 2026. ROADMAP BL-90 (soft delete, data tools design §3) with the user's idea of
17 Oct 2026: delete a channel — import or export — not only whole half-hours. Every claim in §0 was
read in the code on `dev` (59310f6); none is from a docstring. Nothing here is decided: §6 lists the
questions.*

This is the delete tool of the data tools design's selection menu (§1, BL-88): *select → preview
(nothing written) → apply after a required safety backup → a result that says what happened.*

---

## 0. What exists

**The delete** (`POST /api/blocks/delete` → `_delete_worker`, `@job("delete")`, server.py ~7889):
preempts a running import, takes a required `pre-delete` backup, then
`BlockStore.delete_blocks_for_date_range` (block_store.py ~2740) deletes whole **rows** in one
transaction — `reads`, `generation_mix`, `block_segments`, `blocks` — and writes a tombstone. It
never blanks a column, and never touches `measured_cost` (the cached bills, keyed by slot, MPAN and
direction, with no meter). The body has no channel. It bumps the data epoch.

**The tombstone** (`deleted_ranges`, block_store.py ~429): `meter_id` (a meter, or `'*'` = all),
`[start_utc, end_utc)`, `created_at`, `reason`. **No channel.** What it does, and what it does not:

| | honours a tombstone? |
|---|---|
| the settlement poll **creating** a missing block (`create_backfill_block`, ~4536) | yes |
| "recover all" (`resolve_history_gaps`) | yes, skips a tombstoned run |
| the settlement poll **writing onto an existing block** (`upsert_kraken_block`, ~4437) | **no** — it chases any non-imported block whose `imp_kwh_api` or `exp_kwh_api` is NULL (`_UNSETTLED_WHERE`, ~4758) |
| the API import / gap fill (`upsert_imported_blocks`, ~3292) | no — and it **overwrites** on conflict, `source` included |
| CSV import (`upsert_imported_block`) | no — first-man-wins per channel: an empty channel of a present row is filled |
| a targeted fill (gap fill, CSV, re-import) | lifts it (`clear_deleted_range`) |

**The prune** (`run_prune_stale_tombstones`, hourly; `stale_deleted_range_ids`, ~5038): a tombstone
is stale — and deleted — once **every slot in it has a block** for its meter (`'*'` checked against
the main meter). By that rule a channel tombstone over present blocks would be pruned within the
hour.

**A channel, in storage** (`blocks`, one row per half-hour and meter):

- export: `exp_kwh`, `exp_rate`, `exp_cost`, `exp_read_start`, `exp_read_end`, `exp_kwh_api`;
- import: `imp_kwh`, `imp_kwh_grid`, `imp_rate`, `imp_cost`, `imp_cost_exc`, `imp_read_start`,
  `imp_read_end`, `imp_kwh_api`, `imp_provisional`, and the EV/house `block_segments`;
- the half-hour's, not a channel's: `standing_charge` (carried on the import side), `carbon_g` (net
  of both), `source`, `rate_source`, `rate_corrected`, `is_provisional`, `derivation_id`.

`source` is the block's, not a channel's: an API import over a live block re-labels the whole block
`imported_api`, and the Import bill reads that as Octopus's cost.

**NULL and zero differ** for export since #214: an unsettled export with no live sensor waits
(amber); a 0.0 with a sensor is "nothing exported". The bill and day rollups add NULL as 0.

**The devices** (sub-meters) carry import only, clipped to the main meter's grid import in PASS 2
(`_apply_pass2`), and the day's bill is `max(0, main − Σdevices) + Σdevices`.

## 1. What is asked

1. **Delete one channel** of the main meter over a selection — its export (a wrong export register,
   a FIT meter's deemed export) or its import — and keep the other channel's data as it is.
2. **Soft** (BL-90): *Delete* hides, *Restore* brings back exactly what was there, *Delete
   permanently* removes. HA register reads, which are not re-readable after HA's ~10-day recorder
   detail, are kept by a soft delete.

A device's data is a meter, not a channel: deleting it is the delete over that meter, as now.

## 2. Proposal: a shelf, not a flag

**The shelf.** A soft delete **moves** what it deletes out of the live tables into shelf tables,
and *Restore* moves it back:

- a whole half-hour: its `blocks`, `reads`, `generation_mix` and `block_segments` rows go to
  `shelf_blocks`, `shelf_reads`, `shelf_generation_mix`, `shelf_segments` — the same columns, plus
  `shelf_id` (one per delete);
- one channel: that channel's columns of each `blocks` row (§0's lists) are copied to
  `shelf_channels` (`shelf_id, block_start, meter_id, channel, values` as JSON of the columns) and
  set NULL in `blocks`; on import, its `block_segments` rows go to `shelf_segments` and its `reads`
  rows for that channel to `shelf_reads`;
- `shelves` (`shelf_id, meter_id, channel` ('*' | 'import' | 'export'), `[start_utc, end_utc)`,
  `created_at, reason, restored_at, purged_at`) — what the user sees and restores.

**Why not a `deleted_at` flag** (§3's first idea): every reader would filter it — the reader audit
step 5 needed for 11 columns, across billing, charts, settlement, carbon, the heatmap, the
importer, HA's cumulative totals. With the shelf the live tables hold only live data, and **no
reader changes**: a deleted half-hour is absent, as a deleted one is today; a deleted channel is
NULL, as a never-recorded one is. The writers are the only audit.

**Delete permanently** empties a shelf (`purged_at`, its shelf rows deleted). Today's delete is a
soft delete followed by a purge.

## 3. The tombstone learns a channel

`deleted_ranges` gains `channel` ('*' default — every row now is whole-block), and the rules:

- **the settlement poll** (`upsert_kraken_block`) skips a channel a tombstone covers — today it
  would refill a NULL export within a day, so a channel delete without this does not hold;
- **the prune** (`stale_deleted_range_ids`): a channel tombstone is stale when that channel is
  present (not NULL) in every slot, not when the blocks are;
- **the bill apply** (`apply_measured_settled`) already refuses a block with no settled import kWh,
  so a deleted import channel is not re-costed from a cached bill — kept, and tested;
- **the CSV import** fills an empty channel first-man-wins: over a channel tombstone, only an
  explicit fill (§4) should, so it checks the channel tombstone as it already lifts whole ones.

## 4. Filling a deleted channel again

A deleted export over present import blocks is not a missing half-hour (the heatmap hatches only
absent blocks), and gap fill does not fill it (gap fill note §5). Two ways back:

- **Restore** (soft): the shelf moves back.
- **Re-fetch from Octopus** (BL-86, the range re-fetch): it already applies export and kWh
  differences on present blocks through `upsert_kraken_block` — the settlement's own write, which
  keeps an importer's `source` and sets a live block's to `kraken_api`, as every settlement does
  (`run_refetch_apply`, engine.py ~12728). The right writer for a channel. It would lift the
  channel tombstone over what it applies. **Not the API import**: it overwrites `source` to
  `imported_api` for the whole block (§0), which would re-label a live import.

The heatmap shows a deleted channel on its own view: on the Export bill a deleted export is hatched
as missing (with "deleted on …"), and on the Import bill a deleted import likewise; the energy
views show the remaining channel's kWh.

## 5. The surfaces

- **The menu:** *Delete…* on a selection, with the channel to choose: *both* (the half-hour),
  *import*, *export* — export offered only where the main meter has one.
- **The preview:** half-hours affected per channel, what goes with them (HA reads, EV/house split,
  device rows if §6 (b) says so), what is already deleted, that it can be restored.
- **Deleted items:** the heatmap's selection menu offers *Restore* and *Delete permanently* over a
  deleted span; a list (today's Data Management tombstone list, read from `shelves`) the rest.

## 6. Questions for the user

**(a) The shelf** (§2) rather than a `deleted_at` flag on `blocks`. **Decided (18 Oct 2026, the user):
the shelf.**

**(b) Deleting the main meter's import. Decided (18 Oct 2026, the user): the devices go with it** —
their rows for the same half-hours onto the same shelf, restored together. Their rows are clipped to
the main import, so kept alone they would carry the day's bill (`Σdevices`) over an absent meter.
What is shelved keeps its provenance: a block its `source`, `derivation_id` and config period; an HA
reading its sensor — recorded from now on, inferred from the device's primary for older readings
(`EMT-v5_device_sensors_design.md` §7). Why it matters (the user): an import can be wrong or coarse,
and the HA data at its original resolution is what a later rebuild works from. A **device** delete
is not a shelf: it stays permanent (device sensors design §5).

**(c) What goes with a channel:** `standing_charge` is the day's, on the import side — kept when
import is deleted? `carbon_g` is net of both channels — recomputed from what remains, or NULL?

**(d) Refill** (§4): only Restore and the re-fetch, never the API import, for a channel?

**(e) The supersede rule** (BL-90 (g)): a fill over a soft-deleted span writes new data — is the
shelved data then kept until *Delete permanently*, or dropped at once?

**(f)** BL-90's (h)–(j): a v4 restore has no shelf (nothing to carry — its tombstones read as
`channel='*'`); is the purge of imported history soft too; is there a retention limit on shelves?

**(g) The old tombstones:** a delete made before this has no shelf. It stays a tombstone that
cannot be restored, listed as "deleted permanently".

## 7. PRs, if agreed

1. **Schema:** `deleted_ranges.channel`, the shelf tables; the importer carries tombstones as
   `channel='*'` (import spec).
2. **The writers:** the settlement poll, the prune, the CSV import honour a channel tombstone; the
   re-fetch lifts one. Tests: a deleted export is not refilled by the poll; the prune keeps it.
3. **Soft delete and restore, whole half-hours** — today's delete through the shelf; *Restore*;
   *Delete permanently*.
4. **By channel** — the channel's columns, segments and reads to the shelf; the heatmap hatching
   per channel view.
5. **The heatmap menu** — *Delete…* with the channel; *Restore* / *Delete permanently* on a deleted
   span; the preview.
