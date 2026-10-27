# EMT v5 — Engine step 5: carbon, the rate schedules, the start-up backups

*Decided 27 Oct 2026 (§1.3, §2.2, §3.2); drafted the same day. Step 5 of the engine review (`EMT-v5_engine_review.md` §3.4, §5, §6 (e)). Every
code fact read at `dev` 76c32b8; line numbers are `engine.py` unless named. The staging figures are
from the 27 Oct support file's database.*

**What this note decides:** how carbon is written, how the tariff schedules are built, and which
backups the engine takes on its own. It does not change `settle()`, the carbon intensity fetch
(`_tick_carbon_intensity`, `_fetch_carbon_intensity_range`), the tariff stitch's rules
(`kraken_rates.build_agreement_stitched_schedule`), or the user's own backups (Data Management, the
daily share copy).

---

## 1. Carbon

### 1.1 Today: one formula written three times, intensity found three ways

| writer | when | intensity from | formula |
|---|---|---|---|
| finalise PASS 3b (4423) | the block closes | the 4-day CI table, nearest slot, **no distance bound** | inline |
| `_attribute_block_carbon` (2840) — gap blocks (2398), `_recover_missing_carbon` (2879) | a gap block; after a CI tick, a block with no intensity | the 4-day CI table, nearest slot within 60 min | `_apply_intensity_to_block` (2794) |
| `_run_historical_carbon_backfill` (4826) | startup, after an import | the Carbon Intensity API, per region from the block's config period | `_apply_intensity_to_block` |
| `_recompute_block_carbon` (2935) — the PASS 2 re-run (3934), the remainders recompute (3070), the device rebuild (3277) | the kWh moved | the intensity already on the block | inline, a copy of the same |

The intensity a half-hour was attributed is kept on every meter's row (`carbon_intensity_g`), so the
block already holds the fact; `carbon_g` is derived from it and the kWh. The formula is the same in
all three copies: a device's carbon is its own read × intensity, the main's is (`kwh_total` − export)
× intensity.

### 1.2 Found

1. **Finalise has no distance bound.** `_attribute_block_carbon` refuses a CI row more than 60
   minutes from the half-hour ("a block whose true slot has aged out of the 4-day table would
   otherwise be attributed a wrong, distant CI value"); finalise's own copy takes the nearest row
   whatever its distance. Latent: it needs the CI fetch to have stalled for more than an hour while
   blocks still close — an outage of carbonintensity.org.uk.
2. **A device's carbon is its own read, not its grid share.** On staging, 256 EV-charger half-hours
   and 155 battery half-hours drew more than the grid gave them (the rest was solar or the battery);
   their carbon counts that as grid electricity — 6.2 kg of 2,723 kg on the charger, 4.6 kg of
   1,100 kg on the battery. Devices are priced from their grid share (`imp_kwh_grid`, CLAUDE.md's
   invariant); carbon is not. Every stored device row matches the raw read (6,079 of 6,079, 7,385 of
   7,385), so this is the rule, not drift.
3. **The live paths use the current postcode, the backfill the block's own region.** Right as it
   stands: the live paths only reach the last four days.

### 1.3 Proposal

- **One formula**, `_carbon_of(meter_block, intensity)`, called by every writer; the inline copies
  in finalise and `_recompute_block_carbon` go.
- **One lookup**, `_attribute_block_carbon`, with its 60-minute bound — finalise calls it (fixes 1).
- The four triggers stay: they are when an intensity arrives (finalise, a CI tick, the backfill) or
  when the kWh moves (the re-runs). Carbon needs no `settle()` of its own: its one input besides the
  kWh is already a stored fact.

**Decided (a), 27 Oct 2026 — the device's own read, as v4.** What EMT measures each half-hour is the
main's import and export, each device's own read and the grid's intensity; it does not measure
where a device's energy came from — PASS 2's clip (the EV first, then the largest) is EMT's
allocation. v4 kept carbon to the measured inputs on purpose: a device's stored carbon is its own
read × intensity (the gross figure), the main's is (import − export) × intensity, the export offset
is shown apart (2.4.1), the EV's avoided carbon is the one offset derived — at read time, from the
dispatch draw against the grid-clipped EV, and labelled a credit (`carbon.py`, 4.3.2) — and an
inverter-capable device carries "assumes full grid draw" on Insights. Step 5 keeps that model and
changes only who writes it.
- *Rejected:* the grid share (it builds the clip's allocation into carbon); both figures stored, and
  every device given the EV's credit (the credit would still rest on the allocation).
- *Left open, a display question:* the house's carbon is the main's import carbon − Σ the devices'
  gross carbon (`usage_view._bucket_to_row_values`), unclamped. On staging it is below zero in 340
  half-hours (10.85 kg in all), where the devices drew more than the grid gave; the house's kWh
  subtracts grid shares and does not. Clamp it, or show the difference as behind-meter — not
  step 5's.

## 2. The rate schedules

### 2.1 Today: two builders

| builder | channels | agreements | cache | used by |
|---|---|---|---|---|
| `_refresh_kraken_rate_schedules` (8406), every poll cycle (6 h) and on a reconnect | import, export, EV-device, standing | import: **stitched across all** (`build_agreement_stitched_schedule`, kraken_rates 448); EV-device: stitched across the IOG-SMB ones; **export and standing: the current tariff only** | closed agreements cached for the process (`_AGREEMENT_SCHED_CACHE`); the running one rebuilt every refresh (v4 #513) | `settle()`, the live finalise, the export at finalise, the standing charge |
| `_build_channel_rate_segs` (6626), `_build_channel_standing_segs` (6670), once per API import | import, export, standing | each agreement its own schedule | per process, cleared when an import restarts | the API import: a clean rate by Octopus's label (`_tariff_rate_for`, 6735), the standing charge per day |

So an import fetches every agreement's rates again, whatever the stitch already holds, and the
import's own channel is priced twice: by the import at write, then by `settle()` (4d).

### 2.2 Proposal

**One builder: `_refresh_kraken_rate_schedules` stitches every channel across its agreements** —
import (as now), export and standing — through the one cache (closed agreements once per process,
the running one every refresh). The API import reads `_kraken_rate_schedules`; the two historical
builders and their caches go.
- *API calls:* the import fetches nothing it has not already got; the refresh fetches export's and
  standing's closed agreements once per process, then only the running one. Both instances stitch
  7 import agreements today (their logs, 27 Oct); an import now fetches each channel's 7 again.
- *What it changes:* an old export half-hour, or an old day's standing charge, read at the live
  schedule (finalise never does; the import does) gets its own agreement's rate — what the import
  already gets today, from one place.
- *One edge to keep:* `_tariff_rate_for` falls back to a flat tariff's rate where the agreement's
  published rates start after its `valid_from` (the "transition seam"). The stitch clips each
  agreement to its window, so that sliver is uncovered; the fallback moves with the reader.

**Decided (b), 27 Oct 2026 — one fetch.** The refresh's cache is the one place an agreement's rates
are fetched; the import reads it. Standing charges come from the import agreements only — they apply
to import tariffs, and the import's per-channel builder asked for export's too. Export's rates come
from the export agreements, through the same cache. No stored figure changes; Octopus is asked less.

## 3. The start-up backups

### 3.1 Today: three, besides the user's

| backup | when | what | trims the folder |
|---|---|---|---|
| `_pre_upgrade_backup_if_needed` (11919), BL-53 | a new version, **before the store opens** | the raw `blocks.db` + WAL/SHM + the config | no |
| the upgrade backup (12400) | a new version, after the store opens and migrates | an online `backup()` + the config | **yes, to 20** |
| `_kraken_pre_live_snapshot` (8323), v4 4b-iii | the first live DCC write, **once per database** (`pre_live_snapshot_done`); a cycle that cannot take it runs as a dry run | an online `backup()` | no |

### 3.2 Proposal

- **Decided (§6 (e), 22 Oct 2026):** the pre-migration copy stays and the post-open upgrade backup
  goes. Its one other job — trimming the backups folder to the 20 newest — moves to the
  pre-migration copy (Data Management's own backups trim too, `web/server.py` 7619).
- **Decided (c), 27 Oct 2026 — the pre-live snapshot goes, with its dry-run fallback.** It was v4's rollback point for the day the DCC poll first
  wrote into blocks. An imported v4 database carries its marker (the importer keeps it on purpose:
  re-run after a restore it wrote a third 80 MB copy on the Pi's SD card, 10 Oct 2026 soak), and
  both instances have it; so in v5 it fires only on a fresh install, at its first poll after its
  first blocks — beside the pre-migration copy each version takes and the nightly share copy.
  The pre-migration copy each version takes and the nightly share copy remain.

## 4. Order (each its own PR)

1. **5a, the backups** — the upgrade backup goes and the trim moves to the pre-migration copy; the
   pre-live snapshot goes.
   *As built (27 Oct 2026):* `_pre_upgrade_backup_if_needed` trims the backups folder to
   `_BACKUP_KEEP` (20) after its copy; the post-open upgrade backup, its `.last_startup_backup_version`
   marker, `_kraken_pre_live_snapshot`, `_KRAKEN_SNAPSHOT_DONE_KEY` and the poll's dry-run fallback
   are gone. The importer still carries `pre_live_snapshot_done` (nothing reads it), so the import
   spec stands.
2. **5b, carbon** — one formula, one bounded lookup; the rule unchanged.
3. **5c, the schedules** — one fetch; standing from import only. Checked against the tariff goldens and a
   fresh API import on staging (its stored rates and standing charges beside today's).
