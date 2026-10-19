# EMT v5 — Soft delete, and deleting an imported export

*Draft, 18–19 Oct 2026. ROADMAP BL-90 (soft delete, data tools design §3) and the user's idea of
17 Oct 2026, delete by channel. Every claim in §0 was read in the code on `dev`; none is from a
docstring.*

**Decided so far (the user):**
- **A shelf, not a flag** (18 Oct, §2).
- **No general per-channel delete** (19 Oct). Tracing it showed its cost: a channel on the tombstone,
  three writers taught to honour it, a half-hour's `source` being the block's and not a channel's,
  and the devices, standing charge and carbon to settle. Its use cases mostly have other answers
  (§1). What is left is narrow: **deleting the export of imported CSV or PDF history** (§4),
  with a required backup and no shelf.
- **Deleting the main import shelves the devices with it** (18 Oct) — now simply the whole
  half-hour, all meters, as today's delete.
- **A deleted imported export reads grey on the Export bill, as "no export"** (19 Oct): on CSV history
  EMT cannot tell an export that was never recorded from one that was nothing.
- **Only data recorded live from HA is shelved** (19 Oct, §2). An import can be wrong or coarse, and
  HA's data at its half-hour resolution — the block, with HA's registers at its edges — is what a
  later rebuild works from and cannot be got back; imported data and Device History's rebuild can.
  Shelved blocks keep their provenance: `source`, `derivation_id`, config period, and the sensor
  (`EMT-v5_device_sensors_design.md` §7).
- **(a)–(c) of §5** (19 Oct): shelved HA data is kept until *Delete permanently*, a refill
  notwithstanding; v4 stays as it is; an old delete is listed as deleted permanently.

This is the delete tool of the data tools design's selection menu (§1, BL-88): *select → preview
(nothing written) → apply after a required safety backup → a result that says what happened.*

---

## 0. What exists

**The delete** (`POST /api/blocks/delete` → `_delete_worker`, `@job("delete")`, server.py ~7889):
preempts a running import, takes a required `pre-delete` backup, then
`BlockStore.delete_blocks_for_date_range` (block_store.py ~2740) deletes whole **rows** in one
transaction — `reads` (always empty: never written, ROADMAP 19 Sep 2026), `generation_mix`,
`block_segments`, `blocks` — and writes a tombstone. It
never blanks a column, and never touches `measured_cost` (the cached bills, keyed by slot, MPAN and
direction). It bumps the data epoch.

**The tombstone** (`deleted_ranges`, ~429): `meter_id` (a meter, or `'*'`), `[start_utc, end_utc)`,
`created_at`, `reason`. It stops a deleted block being **created** again — by the settlement poll's
backfill (`create_backfill_block`) and "recover all" — and a targeted fill (gap fill, CSV,
re-import) lifts it. The hourly prune drops one once every slot in it has a block again.

**The writers that matter here:**
- the settlement poll writes onto any existing block whose settled kWh is missing — **except imported
  blocks** (`_UNSETTLED_WHERE`, ~4758: `source NOT LIKE 'imported%'`);
- the API import and gap fill (`upsert_imported_blocks`) **overwrite** on conflict, `source` included
  — a gap fill asks only for missing half-hours, so it writes over nothing;
- the CSV import (`upsert_imported_block`) is **first-man-wins per channel**: it fills a channel only
  where it is empty. Its *"Export only (overlay an existing range)"* mode exists to add one channel —
  a FIT meter's export from a third-party CSV — over half-hours that already hold the other;
- `zero_fill_imported_export_blanks` (~4597; after a CSV import, after an import or gap fill, and once
  at startup) sets a NULL export to 0.0 on imported blocks, **on any local day that still has some
  imported export** — so a solar meter's nights are not read as gaps. A wholly empty day is left alone.

**Carbon:** on the main meter `carbon_g = (imp_kwh − exp_kwh) × carbon_intensity_g` (engine ~3034).
The carbon backfill selects on `carbon_intensity_g IS NULL`, not on `carbon_g`.

## 1. The use cases for a per-channel delete, and their answers

| use case | answer |
|---|---|
| an export (or import) wrong, and Octopus has the right figures | **Re-fetch from Octopus** (BL-86) corrects a channel on present half-hours, applying the differences only — no delete |
| a CSV or PDF import wrong or coarse, both channels from it | delete the whole half-hours (§3), re-import both files |
| **an imported export wrong — a third-party or FIT export CSV, a bill's reconstructed export — over import that is fine, and outside what Octopus can re-fetch** (no export feed, or older than its two years) | **delete the imported export** (§4), overlay the corrected file |
| live or settled data wrong beyond Octopus's two years | delete the whole half-hours, re-import from CSV or PDF |

## 2. The shelf

**What it keeps: what only HA had.** HA's data is kept at the half-hour — a live block's kWh and
HA's register at its start and end; the samples within the half-hour were never kept (§0). So the
shelf holds a deleted half-hour's **live-recorded rows** — the main meter's and the devices' whose
`source` is NULL (recorded live) or `kraken_api` (recorded live, then settled by Octopus: the kWh is
Octopus's, the registers HA's) — with their `block_segments` and `generation_mix`. Rows from an
import (`imported%`) or rebuilt by Device History (`recorder_attributed`) are deleted permanently,
after the required backup, as today: their source can give them again. On the user's prod-dev
database (28 Aug 2026) the live-recorded rows are ~16,300 of ~133,000.

**The tables:** `shelf_blocks`, `shelf_segments`, `shelf_generation_mix` (the same columns, plus
`shelf_id`), and `shelves` (`shelf_id, meter_id, [start_utc, end_utc), created_at, reason,
restored_at, purged_at`) — what the user sees and restores.

**Why not a `deleted_at` flag:** every reader would have to filter it — the reader audit step 5
needed for 11 columns. With the shelf the live tables hold only live data and **no reader changes**:
a deleted half-hour is absent, as it is today. The tombstone stays, so the poll and "recover all"
keep away.

**A refill over shelved data** (decided (a)): the shelf is kept — the refill is Octopus's or a CSV's,
and the HA rows cannot be got back. *Restore* over a refilled span puts the HA rows back in place of
the refill, after a backup. *Delete permanently* empties a shelf (its rows deleted, `purged_at`
set). Today's delete is a soft delete and a purge.

## 3. Deleting half-hours

From a heatmap selection, *Delete…* deletes **whole half-hours** — every meter, both channels — onto
the shelf. The preview says so plainly, whichever view the selection was made on:

> **Deletes everything in these half-hours, not only the export.** Import, export and the devices'
> data for N half-hours (from → to) go onto the shelf. Restore puts them back as they were; to
> replace them, fill from Octopus or a CSV. *If only the export is wrong, Re-fetch from Octopus
> corrects it without deleting anything.*

(On the Import bill and the energy views it names import first.) The last line is offered only where
Re-fetch can help: the supplier API connected and the span inside its two years.

*Restore* and *Delete permanently* are on the selection menu over a deleted span, and on a list
(today's Data Management tombstone list, read from `shelves`).

## 4. Deleting an imported export

**Scope:** the export channel of **imported** blocks (`source LIKE 'imported%'` — CSV, PDF, the API
import) of the main meter, over **whole local days**. Not offered on live or settled data (§1 has
their answers), nor for import (a wrong imported import is both channels from a CSV: §3).

**What it writes,** in one transaction after a required `pre-delete-export` backup (no shelf — the
backup is enough, the user decided):

- `exp_kwh`, `exp_rate`, `exp_cost`, `exp_read_start`, `exp_read_end`, `exp_kwh_api` set NULL;
- `carbon_g` recomputed from import alone, `imp_kwh × carbon_intensity_g`, where the intensity is
  held (the carbon backfill would not do it: it selects on the intensity, §0);
- the data epoch bumped.

**Why it needs nothing else:**
- **The poll** never writes onto imported blocks (§0), so the export stays deleted — no tombstone.
- **The refill** is the CSV overlay, *"Export only"*: first-man-wins fills exactly the emptied channel.
  A PDF-bill rebuild goes the same way.
- **Whole days** because of the zero-fill: on a day still holding some imported export it would set
  the deleted half-hours to 0.0 at the next import. Over whole days it leaves them empty.
- **Gap fill and the API import** write only missing half-hours or below the oldest block, so they do
  not reach a present block. Inside Octopus's two years a range re-fetch could write Octopus's export
  — then that is the right figure.
- **The devices** carry import only; the standing charge is on the import side.

**How it reads:** the Export bill shows an imported half-hour with no export **grey, "no export"**, as
today; the bill's export credit goes; the energy views show import only.

**The preview:** *"Deletes the imported export for N half-hours over D days (from → to). Import is
kept. A backup is taken first; to put the export back, overlay a CSV for these days."* — with the
days rounded out to whole local days and shown.

## 5. Decided, and left

**(a) Decided (19 Oct 2026, the user):** shelved HA data is kept until *Delete permanently*; a refill
does not drop it (§2).

**(b) Decided:** v4 stays as v4 — a restore carries its tombstones over as they are, with no shelf.
The purge of imported history stays permanent (imported data is never shelved, §2).

**(c) Decided:** a delete made before this has no shelf; it is listed as "deleted permanently".

**Proposed, not asked:** no retention limit — a shelf stays until *Delete permanently*. Live-recorded
rows are a small share of a database (§2).

## 6. PRs, if agreed

1. **Delete an imported export** (§4) — *built 19 Oct 2026*: `BlockStore.count_imported_export` /
   `delete_imported_export`; `POST /api/blocks/delete-export/preview` and `/api/blocks/delete-export`
   (the delete job, its guards, a required `pre-delete-export` backup); on the heatmap's selection
   menu on every view but the Import bill (an install without the API has no Export bill view, and a
   FIT export CSV is the case it is for). Tests: export NULL and import kept; carbon from import alone; live blocks refused; partial
   days rounded out; the poll does not refill it; the overlay does.
2. **Schema:** the shelf tables (blocks, segments, generation mix); the importer carries tombstones as
   they are (import spec).
3. **Soft delete and restore** — today's delete through the shelf; *Restore*; *Delete permanently*.
4. **The heatmap menu** — *Delete…* with §3's preview; *Restore* / *Delete permanently* on a deleted
   span.
