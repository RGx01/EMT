# EMT v5 — HA's reads kept apart, deleting half-hours, and deleting an imported export

*Draft, 18–20 Oct 2026. ROADMAP BL-90 (soft delete, data tools design §3) and the user's idea of
17 Oct 2026, delete by channel. Every claim in §0 was read in the code on `dev`; none is from a
docstring. Rewritten 20 Oct 2026: the shelf of 18–19 Oct is dropped (§2's last paragraph says why).*

**Decided so far (the user):**
- **HA's reads are kept in a table of their own** (20 Oct, §2). What HA reported for a meter or a
  device — a half-hour recorded live, or an hour from HA's recorder — is written once and kept; a
  block is built from it, and a delete of blocks leaves it alone. Only a device delete removes a
  device's reads. This replaces the shelf (18–19 Oct) and *Restore* / *Delete permanently*: the
  refill is the restore.
- **An hour from the recorder is kept as the hour** (20 Oct, §2): the split into half-hours by the
  house's import shape (`_split_hour_to_blocks`) runs whenever the blocks are written, against the
  house import they hold then.
- **No general per-channel delete** (19 Oct). Tracing it showed its cost: a channel on the tombstone,
  three writers taught to honour it, a half-hour's `source` being the block's and not a channel's,
  and the devices, standing charge and carbon to settle. Its use cases mostly have other answers
  (§1). What is left is narrow: **deleting the export of imported CSV or PDF history** (§4),
  with a required backup.
- **Deleting half-hours deletes them whole** — every meter, both channels — as today's delete (18 Oct).
- **A deleted imported export reads grey on the Export bill, as "no export"** (19 Oct): on CSV history
  EMT cannot tell an export that was never recorded from one that was nothing.
- **v4 stays as v4** (19 Oct): a restore carries its tombstones over as they are.

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

## 2. HA's reads, kept apart

**What only HA had.** HA's data is kept at the half-hour — a live block's kWh and HA's register at
its start and end; the samples within the half-hour were never kept (§0). Device History's rebuild
gets less: HA's recorder keeps its five-minute statistics about ten days, so the rebuild asks for
**hours** (`get_statistics(..., period="hour")`, engine ~3726 and ~3789) and splits each hour
between its half-hours by the house's import (`_attribute_hour` → `_split_hour_to_blocks`, engine
~3471–3590): a charging burst lands in the half-hour it happened, and an hour the house imported
nothing is split evenly. Today both live only on the blocks, so deleting a block loses them — and
1 Apr on staging (19 Oct) showed it: a gap fill writes the main meter only, and the devices' rows
were gone.

**The table** (`ha_reads`; the name settled in PR 2), one row per period HA reported:

| column | |
|---|---|
| `meter_id` | the main meter or a device — its id |
| `channel` | `import` / `export` |
| `period_start`, `minutes` | 30 for a half-hour recorded live; 60 for an hour from the recorder |
| `sensor_id` | the `device_sensors` entry the figure came from (device sensors design §7) |
| `sensor_inferred` | 1 where the v4 import named the device's primary, the block having no pointer |
| `kwh` | as HA reported it |
| `read_start`, `read_end` | HA's register at the edges, where EMT had them (live); NULL from the recorder |
| `origin` | `live` or `recorder` |
| `recorded_at` | when EMT wrote it |

**Written** where the figure arrives: at finalise, in the same transaction as the block (live); by
Device History's rebuild, one row per hour fetched (recorder). **Precedence:** a live half-hour is
never replaced by a recorder hour over it, and a block built where both exist takes the live one.

**Read** by whatever builds a device's block: the rebuild looks in the table before asking HA's
recorder, and splits a recorder hour by the house import the blocks hold *then* — so after a refill
from Octopus or a CSV, whose shape may differ from what was deleted, the split follows the new house
figures. Kept as the hour, the split is never stale.

**Removed** only by a device delete (device sensors design §5): the device's reads go with its rows,
and the warning says so. A delete of half-hours (§3) and the purge of imported history leave it.

**From v4** (an import transform, not a heal — v5 reads the table from then on): a live block
(`source` NULL or `kraken_api`) gives a half-hour row with its registers and sensor pointer; a pair
of `recorder_attributed` half-hours gives back its hour as their sum (the split sums to the hour,
within its 6-dp rounding), and a lone half-hour — its partner had no house block, or was recorded live (`_attribute_hour` writes
only where the device has no row) — is kept as a 30-minute `recorder` row. Imported blocks give nothing: they are not HA's. The staging and Pi
databases take it by importing their backups again.

**The `reads` table** (`block_store.py` ~256) is not this: keyed on `block_id` and deleted with its
block (~2872), and never written (ROADMAP, 19 Sep 2026). It is retired in a PR of its own (20 Oct
2026): its five methods are the reachability tool's fixtures for dead code
(`tests/test_reachability_tool.py`), so that tool's tests change with it.

**Why not the shelf** (18–19 Oct, dropped 20 Oct): a shelf copied deleted rows — cost, bands, carbon
and mix with them, all of which EMT derives again — and needed *Restore*, *Delete permanently* and a
rule for when a refill consumes it. Kept apart from the start, HA's reads need none of that: the delete
is unchanged, and refilling the main meter and rebuilding the devices is the restore.

## 3. Deleting half-hours

From a heatmap selection, *Delete…* deletes **whole half-hours** — every meter, both channels — as
today, after the required backup. The preview says so plainly, whichever view the selection was made on:

> **Deletes everything in these half-hours, not only the export.** Import, export and the devices'
> data for N half-hours (from → to) are deleted. HA's own readings are kept: when the half-hours are
> filled again from Octopus or a CSV, the devices are rebuilt from them. *If only the export is wrong,
> Re-fetch from Octopus corrects it without deleting anything.*

(On the Import bill and the energy views it names import first.) The last line is offered only where
Re-fetch can help: the supplier API connected and the span inside its two years.

**A refill rebuilds the devices.** Today none does — the gap fill, the API import and the CSV import
write the main meter only, and the devices wait for *Rebuild history* on each card. A refill over
half-hours where `ha_reads` holds a device's figures starts that device's rebuild for them, from the
table, with no call to HA's recorder.

## 4. Deleting an imported export

**Scope:** the export channel of **imported** blocks (`source LIKE 'imported%'` — CSV, PDF, the API
import) of the main meter, over **whole local days**. Not offered on live or settled data (§1 has
their answers), nor for import (a wrong imported import is both channels from a CSV: §3).

**What it writes,** in one transaction after a required `pre-delete-export` backup (the backup is
enough, the user decided):

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

**Decided (19 Oct 2026, the user):** v4 stays as v4 — a restore carries its tombstones over as they
are. The purge of imported history stays permanent.

**Decided (20 Oct 2026, the user):** HA's reads in a table of their own, removed only with the device;
a recorder hour kept as the hour (§2).

**Settled in PR 2 (20 Oct 2026):**
- A recorder hour names the sensor whose statistics gave it: the stitch now says, per hour
  (`_stitch_hourly_energy(..., sources=)`). From v4 it names none — v4 did not record it.
- A live row's kWh is the block's at finalise. A settled main block's kWh is Octopus's, so from v4 it
  is the registers' difference (on prod's backup, HA's 7,429 kWh against Octopus's 7,453 over 10,989
  settled half-hours); a device's is its kWh, since an EV charger's session sensor resets (199 rows on
  prod where the registers' difference is negative).
- compute_channel's no-reads 0.0 / 0.0 is left out on the main meter — a grid register is never 0 —
  and kept on a device, whose session sensor sits at 0 between charges (2,282 EV half-hours on prod).
  A device with no reads at all reads the same, and is kept with them.
- Written live at finalise (every meter) and at a provisional device's amendment (that device only).
  The gap fill's interpolated blocks are not written: they are EMT's interpolation between two HA
  registers, not a figure HA reported.

## 6. PRs, if agreed

1. **Delete an imported export** (§4) — *built 19 Oct 2026*: `BlockStore.count_imported_export` /
   `delete_imported_export`; `POST /api/blocks/delete-export/preview` and `/api/blocks/delete-export`
   (the delete job, its guards, a required `pre-delete-export` backup); on the heatmap's selection
   menu on every view but the Import bill (an install without the API has no Export bill view, and a
   FIT export CSV is the case it is for). Tests: export NULL and import kept; carbon from import alone; live blocks refused; partial
   days rounded out; the poll does not refill it; the overlay does.
2. **`ha_reads`** (§2) — *built 20 Oct 2026*: the table; written at finalise, at a provisional
   device's amendment and by the rebuild; a device delete removes it; import transform 7 (spec v24,
   gate `ha_reads`). `reads` is retired separately (§2). Tests: a live half-hour and a recorder hour land; a block
   delete leaves them; a device delete removes them; the transform's pair sums back to the hour.
3. **The rebuild reads it** — Device History's rebuild takes `ha_reads` first, the recorder only for
   hours it lacks; a refill over half-hours with a device's reads starts that rebuild (§3). Tests: a
   deleted and refilled half-hour gets its devices back with no recorder call; a recorder hour re-split
   by a changed house shape.
4. **The heatmap menu** — *Delete…* with §3's preview.
