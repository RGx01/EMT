# EMT v5 — Data tools: one surface, a quality strip, soft delete (ROADMAP BL-88–BL-90)

*Draft, 15 Oct 2026 — the user's idea from the PR 5 Pi check, not fully formed; this note sets out
what exists and a direction, for the user to shape. Nothing here is decided.*

**The problem.** Fixing a data issue today means: see it on a chart, remember the date and time,
switch to another page (Delete Blocks, Historical import / Gap fill, Corrections), and type the
range again in that page's own date and time inputs. Each tool has its own range picker, its own
rules for what an end time means, its own safety story. The heatmap's re-fetch (settlement surfaces
design §5) showed the other way: select on the grid, act from a menu, preview, apply.

---

## 0. What exists (traced 15 Oct 2026)

| tool | where | how the range is chosen | runs as | safety backup | writes |
|---|---|---|---|---|---|
| **Delete blocks** | `/delete-blocks` | date + time inputs, local, 00:00–23:59 default; an end of 23:59 means the next midnight, any other end is inclusive (`block_store.py` ~2640) | `@job("delete")` | yes, refuses without (`_safety_backup("pre-delete")`) | deletes `reads`, `generation_mix`, `block_segments`, `blocks` in one transaction; a `deleted_ranges` tombstone per meter |
| **Purge imported history** | same page | none — all `source LIKE 'imported%'` | `@job("delete")` | taken, but **the purge continues if it fails** (`server.py` ~7740) | deletes the imported rows and six import checkpoint keys; **no tombstone** |
| **Gap fill** (per gap) | Historical import | pick a gap from the list (`/api/backfill/gaps`: block gaps + the import's own) | `@job("gap_fill")`, claims `import` | **none** | lifts the tombstone, then `import_api_history` newest→oldest, 60-day chunks, paced, rate-limit cooldown; no maximum span |
| **Recover all** | Historical import | none | inline, 300 s | none | up to 60 runs; **skips** tombstoned gaps |
| **Historical import** (API) | Historical import | one "go back to" date; the end is the earliest data held | `@job("import")` | taken, continues if it fails | `imported_api` blocks; per-channel checkpoints; pauses below 25 % rate-limit headroom |
| **CSV import** | Historical import | the file's span (+ an overlay "to" date) | an engine write, not a job | taken, continues if it fails | `imported_csv` blocks; lifts tombstones over the span |
| **Corrections** | `/corrections` | date inputs + optional times, local; meter fixed to *all* | an engine write, not a job | yes, refuses without | UPDATEs `blocks`, device rows, `block_segments`; sets `rate_corrected` |
| **Re-fetch** | Heatmaps | select on the grid (cell, drag, day, range), or the detail panel | `@job("range_refetch")` | yes, refuses without | the differing half-hours only; 31-day chunks |
| **Recorder attribution** | Historical import | per device, a range | `@job("attribution")` | — | device rows from HA's long-term statistics (hourly); BL-87 would run it with the API import |

Nothing opens a page at a given day: the Charts tab comes from `localStorage`, the heatmap has
`loadMonth(key)` but no URL calls it, and no tool reads URL parameters.

---

## 1. BL-88 — one data-tools surface, driven by the selection

**Direction.** The heatmap (and, through §2, Usage Stats) is where a range is chosen; every tool is
an action on that selection, in the same menu the re-fetch already uses:

| on a selection | what it does | from today's |
|---|---|---|
| **Re-fetch from Octopus** | as now | re-fetch |
| **Fill gaps from Octopus** | the gaps inside the selection; the long-period engine (chunked, paced, resumable) is gap fill's, kept | gap fill, recover all |
| **Import a CSV for this range** | the CSV path, its span checked against the selection | CSV import |
| **Correct the rate…** | the corrections form, its range pre-filled and shown, the meter choosable | corrections |
| **Delete…** | soft by default (§3) | delete blocks |
| **Attribute devices from HA history** | recorder attribution for the selection | recorder attribution |

**One shape for every action** — the one the re-fetch proved: *select → preview (what would change,
nothing written) → apply after a safety backup that is required, not best-effort → a result that
says what happened and what did not.* That closes three gaps in §0: purge and gap fill without a
required backup, and corrections and CSV import outside the job coordinator.

**Ranges, once.** Selections are half-open UTC `[from, to)` from the grid, shown in local time with
the clocks-change days' 46 / 50 half-hours. The pages' local date + time inputs, with their
"23:59 means midnight" rule, go — or stay only as a way to type a selection.

**Whole-history jobs** (the first API import, purge) are not a selection; they stay on Data
Management, reached from the same menu with no range.

*Open:* (a) does each page go, or stay as a thin form that opens the heatmap? (b) a phone has no
drag (design §7 (b)): is the day column enough, or does a typed range stay? (c) gap fill's long
periods versus the re-fetch's 31-day chunks — one chunking rule for both?

---

## 2. BL-89 — a data-quality strip on Usage Stats

**Direction.** Usage Stats is a Chart.js stacked bar chart on a category axis (half-hour / day /
month / year bars). Beneath (or above) the axis, a thin strip, one cell per bar, coloured by the
worst settlement state in that bar — the Import bill colours (settlement surfaces design §9.3),
and the split's where the account has one. A bar that is all green draws nothing, so the strip
only shows where there is something to look at.

**Clicking a strip cell** opens the heatmap at that day (a day or half-hour bar) or month (a month
or year bar), with that span selected and its menu open. That needs the deep link §0 says does not
exist: a `#heatmap/2026-08-24` (or `?view=heatmap&month=2026-08&select=…`) the Charts page reads
on load, calling the existing `loadMonth`.

**Data.** The per-half-hour states already exist (`include=settle`); the strip needs them rolled up
per bar — a `settle_summary` facet at day / month resolution (counts per state), so a year view
does not fetch 17,520 half-hours.

*Open:* (d) above or below the axis, or both (import above, export below)? (e) which states count
— red only, or amber too, which on a recent day is normal? (f) the same strip on the Billing tab?

---

## 3. BL-90 — soft delete

*18–19 Oct 2026: designed further in
`EMT-v5_soft_delete_design.md` — a shelf instead of a flag; per-channel delete only for an imported export.*

**Today** a delete removes the half-hour, its HA register reads (`reads`), its generation mix and its
segments outright (§0); only a tombstone of the range is kept. The HA readings are gone unless the
HA recorder still has them — its default keeps detail for about 10 days, and EMT reads only its
long-term hourly statistics.

**Direction.** *Delete* hides; *Delete permanently* removes.

- A soft-deleted half-hour is out of every total, chart, bill and settlement surface, and out of
  the poll's and gap fill's way (as a tombstone is today); its rows — including `reads` — are kept.
- *Restore* brings it back as it was; *Delete permanently* runs today's delete. Both from the same
  selection menu (§1), both previewed.
- What it is replaced by matters: if gap fill or a re-fetch writes a new block over a soft-deleted
  span, the hidden rows are superseded — kept until a permanent delete, or dropped then? (g)

**How, roughly.** A `deleted_at` (and `deleted_reason`) on `blocks`, every reader filtering
`deleted_at IS NULL` — the readers already go through ReadStore, so the filter has one home per
query; or a side table the readers anti-join. Either way it is a schema change (the importer drops
nothing it does not understand — import spec §1.2) and a reader audit like step 5's.

*Open:* (h) does a v4 restore (which has no soft deletes) need anything? (i) the purge of imported
history — soft too, or permanent only? (j) a retention limit on soft-deleted rows?

---

## 4. Order, if agreed

*17 Oct 2026:* the heatmap's own step before the tools — issues first, missing half-hours, one
width — is `EMT-v5_heatmap_issues_design.md`.

1. The deep link (§2's prerequisite, and useful alone): open the heatmap at a day, selected.
2. BL-88's shape for the tools that lack it: required backups for purge and gap fill; corrections
   and CSV import as coordinator jobs.
3. BL-88's menu actions, one tool at a time — gap fill first (the long-period engine), then
   corrections, then delete (with BL-90).
4. BL-89's strip, once the summary facet exists.
5. BL-90 with the delete action.
