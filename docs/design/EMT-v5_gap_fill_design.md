# EMT v5 — Gap fill as an action on the heatmap's selection

*Draft, 18 Oct 2026; §7 decided the same day. The user, after the heatmap showed missing half-hours (#199) and folded the
clean days (#201): gap fill next, as a user-initiated action. Decided the same day: (a) it refills
deleted half-hours too, the preview saying how many; (b) the Historical import page's gap list
stays, a deep link to the heatmap later. Every line below was read in the code or queried on
`blocks-38`; nothing is from a docstring.*

This is step 4 of the heatmap issues design (§5) and the first tool of the data tools design's
selection menu (§1, BL-88): *select → preview (nothing written) → apply after a safety backup that
is required → a result that says what happened and what did not.*

---

## 0. What exists

| piece | where | what it does |
|---|---|---|
| **the engine** | `engine.run_gap_fill_job(from_ts, to_ts, channels, chunk_days=60, max_chunks=6, pace_s=1.5)` | fills one window `[from, to)` through `import_api_history` (bounded mode: no full-import checkpoint touched), newest → oldest, pausing below the rate-limit headroom and retrying after a 30 s cool-down; claims `import` in the coordinator for the run; then the full import's finish — the pricing verify pass, the carbon re-arm, the chart rebuild. Pause and cancel between chunks. **Lifts any tombstone over its window first** (`clear_deleted_range`): a targeted fill |
| **the writes** | `upsert_imported_blocks` (`source='imported_api'`) | **first-man-wins**: a block, or a channel of one, that already holds data is left untouched. A fill over present blocks overwrites nothing — but still asks Octopus for them |
| **the job state** | `_api_import_job`, shared with the full import | one fill (or import) at a time; the routes refuse a second (`busy`) and refuse while the previous run's verify pass is active |
| **the routes** | `/api/backfill/fill-gap` (one gap, its `to` the last missing slot's start, extended one block), `/api/blocks/reimport` (a deleted range), `/api/backfill/gaps` (the list), `/api/backfill/reach` (how far back Octopus can serve) | the Historical import page's buttons. **No safety backup** (data tools design §0) |
| **the gap finder** | `BlockStore.find_block_gaps(min_age_hours=48)` | contiguous runs of missing main-meter half-hours, **skipping the last 48 h**: DCC has not settled them, so Octopus returns nothing yet |
| **Octopus's reach** | `_API_RETENTION_DAYS = 730` | two years of half-hourly data |
| **the heatmap** | `markMissing` (#199) | a half-hour is missing from 2 minutes after its end, inside the data's span, with no main-meter block — tombstoned or not |

On `blocks-38`: 1,105 missing half-hours in two runs — 8–30 Jun 2026 (1,104, the user's delete,
tombstoned) and 15 Jul 00:00 BST (1, tombstoned). Both within Octopus's two years.

## 1. The action

**Where.** The heatmap's selection menu — a right click or a drag's release, the menu that offers
the re-fetch — offers **Fill gaps from Octopus…** whenever the selection holds a missing half-hour
(a hatched cell, a hatched day row, a fold is never missing). A click on a missing cell's panel
(`openMissing`) offers it too.

**What it fills: the selection's missing half-hours, as their contiguous runs** — not the whole
selection. First-man-wins would protect the present blocks, but asking Octopus for them spends the
rate limit on data EMT already holds. The runs are the client's (the cells it already marked
missing), checked again by the server against the store at preview.

**Deleted half-hours are filled too** (decided (a)): the fill lifts their tombstones, as the
per-gap fill does. The preview says how many were deleted, so nothing comes back unannounced.

## 2. Preview — nothing written

`POST /api/gaps/fill/preview {runs: [[from, to), …]}` answers, per run and in total:

- **missing**: half-hours with no main-meter block, counted from the store (not the client);
- **deleted**: of those, how many lie inside a tombstone — with its date;
- **too recent**: inside the last 48 h, which Octopus cannot answer yet — left out, and said;
- **too old**: older than Octopus's two years — left out, and said;
- **the size, per channel**: the half-hours to fetch for import and for export (decided (a)), and
  the 60-day chunks they make, for a time estimate.

The dialog shows that, and **Apply** or **Cancel**. A selection with nothing fillable says so and
offers nothing.

## 3. Apply — after a required backup

`POST /api/gaps/fill/apply {token}`, the preview's token (as the re-fetch's: the same runs, a TTL,
a fingerprint of the range so a change since the preview refuses):

1. **a safety backup, required** — refused without one, as delete and the re-fetch are;
2. **one coordinator job**, `gap_fill`, that runs the preview's runs **newest first, one after
   another** through `run_gap_fill_job` — each lifting its own tombstones and writing first-man-wins;
   pause, resume and cancel as the import's;
3. **the import's finish once**, after the last run (not per run): verify, carbon re-arm, chart
   rebuild — and the history drain, which the verify's end kicks, asks the new blocks' bills where
   they are Intelligent half-hours.

The dialog follows the job (`/api/gaps/fill/status`), as the re-fetch's does.

## 4. Result

Per run: half-hours **filled**, **left missing** (Octopus had no data — a meter offline, or before
the account), **tombstones lifted**. The heatmap redraws the months it touched; a filled half-hour
is `imported_api`, so its Import bill reads green (Octopus's own billed cost).

## 5. Not in this step

- **A channel missing from a present block** (an export gap under a present import) is not a
  missing half-hour on the heatmap (§0) and not filled here. It goes with the user's per-channel
  delete idea (data tools design §3, BL-90).
- **The Historical import page's list** stays (decided (b)); a deep link from it to the heatmap,
  selected, is the data tools design's first step (§4).
- **Recover all** (the blanket fill that skips tombstones) stays on Data Management.

## 6. PRs

1. **Server**: the preview (§2) and apply (§3) routes, the required backup, the `gap_fill` runner
   over several runs with one finish, the status route. Tests: the counts (missing, deleted, too
   recent, too old) against a store with holes; a backup failure refuses; runs filled newest first;
   one finish; first-man-wins over present blocks between runs.
2. **Heatmap**: the menu item where the selection holds a missing half-hour, the dialog (preview,
   progress, result), the redraw.

## 7. Decided (18 Oct 2026, the user)

**(a) A long selection. Decided (18 Oct 2026, the user): no maximum of its own.** Octopus keeps two
years of half-hourly data, and that is the bound: anything older is "too old" in the preview and
left out (§2). The preview gives the size in **half-hours per channel** — what the API import
fetches — as well as its chunks. June's 23 days is 1,104 half-hours on each channel, one 60-day
chunk each. The engine is already paced, resumable and rate-limit aware. A CSV or PDF import is
a separate tool on the selection menu (data tools design §1), bounded by its own file, so it
needs no limit here either.

**(b) A run still too recent. Decided (18 Oct 2026, the user): leave it.** A half-hour inside the
last 48 hours is left out of the fill and the preview says so ("too recent: Octopus has not
settled it yet"); there is no option to try anyway. Once it is 48 hours old a later selection
fills it.

**(c) A new install's history. Decided (18 Oct 2026, the user): not a gap.** A new instance with the
supplier API holds nothing before its first block, while Octopus may hold two years. That history
was never recorded, so it is not missing: the heatmap hatches only inside EMT's own span (from the
oldest block, §0). It gets **its own row below the oldest month** instead, "Octopus holds your data
back to <date> — import it", linking to the Historical import page's API import, which fills it.
The row is not hatched and is not an issue. The date is `bounds.reach_from` in `/api/periods`
(`engine.history_reach_from`, #212): the earliest channel floor the import plan uses (a data floor
already found, else the earliest agreement), no older than Octopus's two years, and none once EMT's
oldest block reaches it.

**(d) What "fill the gap" means, and where from. Decided (18 Oct 2026, the user, on the Pi).** The
first heatmap version (#213) filled the missing half-hours of the *selection*, a cell or a day, and
the missing cell's panel offered two buttons side by side that did not read as a choice. Now:

- **Any half-hour selected in a gap means the whole gap.** The server finds it
  (`engine.gap_bounds`, `POST /api/gaps/around`): from the half-hour after the last block before it to
  the first block after it, or to the last finished half-hour where none follows yet. A gap crossing
  into months not loaded is still whole. A selection over several gaps means each of them, whole.
  A half-hour before EMT's oldest block is in no gap (decision (c)).
- **One action, "Fill this gap…"**, on a cell, a day, a drag and the missing cell's panel, with or
  without the supplier API. Its dialog names the gap and offers **three separate sources**:
  - **From Octopus (API)**, the preview, fill and result of §2–§4, shown only with the API;
  - **From a CSV** and **From Octopus PDF bills**: the Fill History page itself, opened as a
    **modal over the heatmap** on that gap (`/historical-import?embed=1&source=csv|bills&gap_from=
    &gap_to=`), so its templates, upload, review and apply are used, not copied. Closing it redraws.
    One gap at a time, as the page's own gap list does.

The heatmap will replace Data Management (data tools design §1); until then the page is both a page
and the modal.

## 8. The fill's pricing check, and a known edge (18 Oct 2026)

**Scoped to the fill (#216).** A fill ends with the deferred pricing verify, which until #216 swept
the whole imported history after every fill (on the Pi soak: 39 chunks, 43–70 s and ~30 Octopus
requests for a gap of a few days). It now checks only the fill's span. What does not narrow with it:
the chunk dates are whole days (the end day inclusive, plus an hour for BST), the cost retry
(`recover_measurement_costs`) builds its own look-back, day and month windows around each half-hour,
and the opportunistic neighbour repricing only touches the windows it fetched.

**Known edge — left (the user, 18 Oct 2026).** `_reprice_suspect` decides "priced at peak" from the
schedule's day bounds where the day is banded; on a FLAT schedule it falls back to an off-peak
floor, the cheapest stored rate among the imported half-hours in the range. Over a 30-day chunk
that always held off-peak nights. Over a gap's own days it may not: a short daytime gap filled
entirely at peak makes the floor the peak rate, nothing reads as suspect, and a dispatch the import
could not label off-peak goes unchecked. It bites only an Intelligent tariff whose off-peak is not in
the published schedule — the case the fallback exists for; the soak account's Intelligent
agreements are all banded. **If an account on such a tariff shows it:** read the floor over the 30
days up to the gap's end (a local read, no Octopus requests), keeping the targets the gap's, with a
test where a peak-only gap on a flat schedule is still checked.

**Also left (the user, 18 Oct 2026):** the full-history pass re-checks every ≥ 1 kWh peak-band
half-hour each time, because nothing stored says Octopus has confirmed it (`exc_source` went in
step 5). Genuine peak use is asked about again on every full pass — about 30 requests on the soak.
With the scoping that pass runs only after a full import. A "confirmed" record the suspect rule
skips would end it, if it ever matters.
