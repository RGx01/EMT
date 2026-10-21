# EMT v5 — Data Management rebuilt around the data heatmap

*Draft, 20–21 Oct 2026 — the user: "integrate the data heatmap into Data Management instead of the
charting heatmap; the whole screen needs a rework, including the Safety & housekeeping layout."
Builds on the data tools design (BL-88: every tool an action on a heatmap selection) and the
heatmap issues design. Every line in §0 was read in the templates and routes on `dev`. §1 is
decided (the user, 20–21 Oct 2026), section by section; §2 is what is left open.*

---

## 0. What exists

**Data Management** (`/data-management`, `data_management.html`):

| part | what it is |
|---|---|
| topbar | *Backup Now*; links to three sub-pages — *Fill History & Gaps*, *Cost Corrections*, *Delete Blocks* |
| Missing Data nudge | shown when `/api/history-gaps` finds gaps; links to Historical Import |
| **Safety & housekeeping** — Backups | where backups live, *Create Backup Now* (a second copy of the topbar button), the list — each zip with **Download**, **Restore**, **Delete**, five shown then a scroll; legacy v1–v2.0 files, if any, with their own restore |
| — Import DB | a drop zone for a backup zip or `blocks.db`; the staged restore (first-run design §2) |
| — Storage | database size, disk free, growth rate, runway, a usage bar |
| — Compact Database | VACUUM, the engine paused |

**Its sub-pages:**

| page | what it does | the heatmap already has |
|---|---|---|
| Fill History & Gaps (`fill_history.html`, 64 lines) | a landing page: two links, to Historical Import and to Settings' device cards | — |
| Historical Import (2,040 lines) | the API import (whole history, a date range, a gap), CSV, PDF bills; *Recover all* | *Fill this gap…* (API, or this page as a modal for CSV / PDF bills) |
| Cost Corrections (`corrections.html`) | a rate or standing-charge correction over typed local dates; **Billing settlement source** (DCC / CAD); **Review unsettled blocks** + *Retry settlement* | — (the Import bill shows corrected half-hours, blue) |
| Delete Blocks (`delete_blocks.html`) | delete over typed local dates and times, *reconstructed only*; **purge all imported history** | *Delete…* (whole half-hours), *Delete the imported export…* |

**The heatmap** (`heatmap_module.html`, on Charts and the bare Lovelace page; `EMTHeatmap.init(el)`
takes no options):

- views: **kWh, gCO₂, gCO₂/kWh** (energy) and **Import bill, Export bill** (data; the API views went
  on 14 Oct, the Split view on 17 Oct);
- **Issues only** (folds clean days), the *To look at* totals, the detail panel;
- on a selection (cell, day, drag): *Re-fetch from Octopus*, *Fill this gap…*, *Delete…*,
  *Delete the imported export…*.

So the data tools live on **Charts**, and Data Management is a set of links to typed-range forms.
Nothing opens a page at a given day (data tools design §0): a URL parameter for it does not exist.

---

## 1. The design (decided)

```
┌ Data Management ──────────────────────────────────────────────── [💾 Backup now] ┐
│ Health: Import bill 12 to look at · Export bill 3 · missing 0 half-hours ·       │
│         last backup 14:02 (before a gap fill) · database 87 MB                   │
│ [Import bill] [Export bill] [Missing]   ☑ Issues only                            │
│ ┌ the heatmap, a month at a time, newest first ─────────────────────────────┐    │
│ │ a half-hour, a day or a drag → Re-fetch · Fill this gap… · Delete… ·      │    │
│ │ Delete the imported export… · Make a support file for this range…         │    │
│ └───────────────────────────────────────────────────────────────────────────┘    │
│ ┌ Whole history ────────────────────────────────────────────────────────────┐    │
│ │ Import from Octopus · Import a CSV · Import PDF bills · Recover every gap ·│    │
│ │ Delete all imported history                                               │    │
│ └───────────────────────────────────────────────────────────────────────────┘    │
│ ┌ 💾 Backups & storage  [Create backup now] [Compact now] [Make a support file…]┐  │
│ │ Database 87 MB · 12 MB reclaimable · disk free 41 GB · ~9 years of room    │  │
│ │ What | When | Size | Download · Restore · Delete                           │  │
│ │ Drop a backup zip or blocks.db here to restore it                          │  │
│ └────────────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────────┘
```

**(1) The heatmap has two modes** (`EMTHeatmap.init(el, {mode})`):
- **data**, on Data Management — the **Import bill** and **Export bill** (where they show today:
  the supplier API, and an export channel for the Export bill, `_settle_views`) and a new
  **Missing** view (the kWh grid as present / missing — a CAD-only install's only data view);
  *Issues only* on by default; every tool on the selection.
- **chart**, on Charts and the Lovelace card — **kWh, gCO₂, gCO₂/kWh only, read-only**: no
  selection menu; the detail panel keeps an *Open in Data Management* link to its day.
- **`/data-management?day=YYYY-MM-DD`** opens that month, scrolls to the day and marks it.

**(2) The health strip:** *To look at* per view, missing half-hours (`/api/history-gaps`), the last
backup (what made it, when), the database size; each figure a link (the newest month with that
issue, Issues only on; the backup card). Its counts use **the heatmap's own rule**: one call returns
each view's counts **per state** over the whole history, and the page sums them with `isIssue`, so
the rule stays in the JS and the strip cannot disagree with the grid (0.5 s over staging's 59,162
half-hours here). Fetched on load and after a tool finishes; "counting…" until then. The Charts pill
(`settlement_awaiting`, a different model — waiting per stage) is not used.

**(3) Corrections move to the Billing tab, in CAD-only mode only.** A wrong rate is noticed on a
bill: a right-click (long-press) on a half-hour's bar or rate line on the Billing tab's day chart →
*Correct this rate…* → a modal for **that half-hour only** — its current rate, the nearby rates
(`/api/corrections/nearby-rates`) or a typed value, *recalculate cost*, a preview (£ before →
after), apply after a **required** `pre-correction` backup, the day redrawn. The standing charge
from the day's summary row, for that day. **Not offered with the supplier API**: Octopus's bill (or
the settled kWh × a published rate) is the answer and *Re-fetch* the tool — and a correction sets
one rate over the whole half-hour (`imp_cost = imp_kwh × rate`, the segments re-priced to it),
which cannot express IOG-SMB's EV and house rates. CSV history and bills that never come could use
it, but not without SMB, and that is not worth building. Usage Stats may take the same modal later.
**Migrated corrections** are left for the user to fix with *Re-fetch*, which already replaces a
correction Octopus's bill differs from or agrees with (`confirms`, 15 Oct 2026).

**(4) The Whole history card** — the jobs that are not a selection, each opening Historical
Import's flow as a modal (as *Fill this gap…* does), its forms used, not copied:

| action | from | shown when |
|---|---|---|
| Import history from Octopus | Historical Import's API task: the first import, or further back to a date | the supplier API |
| Import a CSV | its CSV task (a whole file, or one channel overlaid) | always |
| Import PDF bills | its bills task | always |
| Recover every gap | *Recover all* (leaves tombstoned ranges alone) | the supplier API |
| Delete all imported history | the Delete Blocks page's purge — **its backup now required** (today it carries on if the backup fails) | imported history exists |

Historical Import's date-range and gap tasks are a selection and *Fill this gap…* on the heatmap.
Device history stays on each device's card in Settings.

**(5) Backups & storage — one card:**
- **Create backup now**, **Compact now** and **Make a support file…** side by side in its head (the
  topbar keeps *Backup now* as a shortcut).
- **Storage is one line**: database size, how much a compact would reclaim (SQLite's free pages ×
  page size), disk free, runway.
- **The list as a table — What · When · Size · actions:** *what* read from the label in the name
  (`pre-gap-fill` → "Before a gap fill", `manual` → "Made by you", 13 labels in use), *when* with
  its age, *size* — `/api/backup/list` returns names only, so it gains each zip's size and time;
  Download, Restore, Delete as now. **The daily copy** of the database (`blocks.db` in the share)
  is a row of its own, restorable. The legacy v1–v2.0 `blocks.json` row goes (far below the floor).
- **Dropping a file opens the restore modal at once**, a zip or a `blocks.db` alike — today a zip
  opens it, a `.db` waits for *Import Selected Files*, below the fold; that button goes. The staged
  two-step restore is unchanged.

**The support file** (*Make a support file…*, or *…for this range* on a selection): one zip, to
email to whoever is helping —
1. **the log**, as much as the Supervisor holds (`/addons/self/logs`), gzipped;
2. **`support.json`**: version and mode, the config without secrets, row counts by meter, source
   and month, the import audit and its gates, the coordinator's state, the bill-ask record, the last
   errors;
3. **the database cut to a span** — a `VACUUM INTO` copy keeping that span's half-hours (blocks,
   segments, `ha_reads`, bills) and every config and state table — zipped.

**Default span: the last 30 days**; a month, everything, or the heatmap's selection otherwise. The
**estimated size is shown before building**, with a warning past 10 MB (many providers' limit;
Outlook 20, Gmail 25 — a full prod-sized backup is ~13 MB zipped). **Identifiers are redacted by
default**: MPANs, meter serials, the account number and the postcode become stable placeholders
(each the same everywhere it appears), with a box to include them. The Octopus API key is never in
it: it lives in `kraken_credentials.json`, outside the database. The dialog says what the file holds.

**(6) Delete… gains *only the imported half-hours*.** Where a selection holds both imported and
live half-hours, the preview offers *Everything* or *Only the imported half-hours (N of M)* — the
Delete Blocks page's *reconstructed only*, the way to roll back a bad import over part of the
history.

**(7) What goes:**

| goes | where its job goes |
|---|---|
| Fill History & Gaps page | its links: the Whole history card, the device cards. `/fill-history` → Data Management |
| Delete Blocks page | *Delete…* (with (6)); the purge to Whole history. `/delete-blocks` → Data Management |
| Cost Corrections page | the Billing tab's modal, CAD-only. `/corrections` → Charts' Billing tab |
| — its **Billing settlement source** | **retired, not moved**: it only means anything in `cad+api` (v4's `mode_ui_design.md`, 3.0.0), re-runs every main-meter half-hour when changed (`flag_all_for_pass2_rerun`), and on an Intelligent tariff Octopus's bill is final whichever kWh EMT holds. Switching to CAD-only is setup's existing *Disconnect Octopus* |
| — its **Review unsettled blocks** / *Retry settlement* | the Import bill's amber and red, and *Re-fetch* on a selection |
| the topbar's three links | — (*Backup now* stays) |
| the Missing Data nudge | the health strip |
| the Import DB card | the drop row of Backups & storage |
| Charts' data views and tools | Data Management |

**Stays:** Historical Import (the flows the modals open; reachable by URL); the restore modals and
the staged restore; the routes the pages called (corrections preview / apply, the delete, the purge).

---

## 2. Open

(a) **CAD-only and HA's cost sensors.** Does setup's *Disconnect Octopus* (or a mode change to
`cad`) ask for the rate sensors CAD-only pricing needs? To check before PR 7 relies on it.

(b) **The billing source's code.** The toggle's UI goes; `_get_billing_source` stays at `dcc` for
the passes that read it. Whether the `cad` branch is then dead, and goes, is a separate clean-up.

---

## 3. PRs

1. **The heatmap's two modes and `?day=`** — *built 21 Oct 2026*: `init(el, {mode})`; Charts and
   Lovelace in chart mode (energy views, no menu, *Open in Data Management*); the Missing view; the
   data-mode heatmap at the top of Data Management, so the tools have their page from this PR (PR 2
   builds the page round it). `?day=` draws every month above the day first, then unfolds, marks and
   scrolls it below both sticky headers (clicked through on a copy of staging's database: scrolled at
   once, the day ended above the view).
2. **Data Management's new page** — *built 21 Oct 2026*: the health strip (`/api/data-health`, the
   per-state counts; on a copy of staging's database the strip and the grid's summed "To look at"
   agreed exactly); the Whole history card, Historical Import's flows as a modal on a task
   (`?task=source:scope`); the purge's backup required (on the copy, with no backup possible:
   refused, nothing deleted); the Fill History & Gaps link and the nudge gone. *The Delete Blocks
   and Cost Corrections links stay* until PR 4 and PR 6 take their jobs — removing them now would
   leave *reconstructed only* and corrections unreachable.
3. **Backups & storage** — *built 21 Oct 2026*: one card; `/api/backup/list` with each zip's size
   and time (`zip_info`); the What · When · Size table, *when* from the time in the name (a copy
   keeps it; the file's modified time moves); the daily copy's row; the storage line with SQLite's
   free pages as "free inside the file" — what a compact reclaims **at least**: clicked through, a
   copy with no free pages still compacted 86.6 → 81.1 MB, since a compact also packs part-empty
   pages; a dropped file (zip or `.db`) opens the modal; the legacy row, the Import DB, Storage and
   Compact cards gone. `/api/import/extract-zip` has no caller left (PR 7 can retire it).
4. **Delete… only the imported half-hours** — *built 21 Oct 2026*: the preview counts the imported
   half-hours; where a selection holds both, *Everything* or *Only the imported half-hours (N of M)*,
   sent as the delete's existing `reconstructed_only` with `utc_span`. Only the imported main-meter
   rows go; the devices' rows over them stay, for a re-import to rejoin (clicked through on a copy of
   staging's database: 2–3 Mar 2026, 48 of 96 imported, 288 rows → 240 after the backup).
5. **The support file** — `support.json`, the log, the span's database, redaction, the size
   estimate; from the card and from a selection.
6. **Corrections on the Billing tab** — the context menu and the modal, CAD-only; one half-hour;
   the standing charge from the summary row; the required backup; `from_utc` / `to_utc` on the
   corrections routes.
7. **Retire the sub-pages** — Fill History & Gaps, Delete Blocks, Cost Corrections, with redirects;
   the billing settlement source's UI.

1 comes first, 2 after it; 3–6 stand alone; 7 last, once 2, 4 and 6 have taken the pages' jobs.
