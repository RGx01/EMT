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

**(8) The jobs in the topbar** (decided 21 Oct 2026, the user, after staging). Below the heatmap the
Whole history and Backups & storage cards moved as its months loaded, and reaching them meant
scrolling past all of it; Whole history's buttons showed only once two requests answered, behind
the health strip's on the Pi. The page's body is the health strip and the heatmap, nothing else.
The topbar has:
- **Whole history ▾**, a menu of the card's jobs, each opening its modal as before. Its items
  are decided when the page is rendered: the API's two with an API connection, the purge when there
  is imported history.
- **Backup & restore**, a modal holding the Backups & storage card: create, compact and the support
  file in its head.
- **Backup now**, kept for the one click before a risky change.

On a phone the three share one row, labelled *History*, *Backups* and *Backup now*. A backup
dropped anywhere on the page opens the restore, since the drop row is in a modal now.
`?open=backups` and `?open=support` open their modal from a link.

**(9) After the layout, on staging** (decided 21 Oct 2026, the user):
- **A drag across rows takes whole days.** Within one row a drag runs cell to cell; once it leaves
  its row, every day it crosses is taken whole, its first row too. Rows run newest first, so one
  span of time had taken the origin row from midnight up to the cell, the wrong way from where the
  drag began.
- ***Issues only* folds whole months.** A month with nothing to look at in the view folds, a run of
  them into one line ("Apr – Sep 2026: nothing to look at"), which a click shows.
  - The counts come by local month from the health strip's cached pass (`months` on
    `/api/data-health`), so a clean month is never fetched to find out.
  - The newest month, which is live, and the month `?day=` opens are always shown.
- **A Whole history task hides the choosers.** Opened from the menu, Historical Import's source and
  task choosers are not offered: picking API again had reset the task and left the modal half on
  one flow. Its heading gives way to the modal's title, and the intro says what this task does.
- ***Missing* only without a bill view.** With the API, a missing half-hour is an issue in the Import
  and Export bill views already, and the health strip's *Missing* opens the Import bill. CAD-only,
  which has no bill view, keeps it.

**(10) The second round, on staging** (21 Oct 2026, the user):
- **Import history from Octopus loses *Reprice from CSV*.** Import a CSV fills half-hours; the
  pricing-health panel's per-slot CSV for half-hours Octopus could not price stays.
- ***Preview plan* opens enabled on a task.** It stayed greyed, and not because the history was
  complete. Picking the task started a fresh flow before the page's first status check, and a
  fresh flow skipped the verify check the run lock waits on, so the lock never lifted.
- **A Billing day's chart fills its row.** It is as tall as the summary beside it, never under
  320 px, and the summary's lines are closer. A summary of 460 px is now 367, and the chart beside
  it 367, not 320 with space beneath.
- **The health strip counts a month at a time.** Each month is kept until its own days'
  fingerprints move, so a new half-hour counts the current month and not the history:
  - *Counted again:* a month within the age window (bills turn overdue at 9 days) every ten
    minutes; every month hourly, or when the bills given up or held change.
  - *After start:* one count, off the request path.
  - *The heatmap* waits 1.5 s at most for the month counts, then folds when they come.
- **The support file's log is the log.** The Supervisor sends its last 100 entries unless asked for
  more: a support file held 99 lines. It and the Logs page now pass `lines`.

**(11) The third round, on staging** (21 Oct 2026, the user):
- **An Octopus import with nothing to fetch isn't offered.** Both channels' windows empty means the
  history already covers what Octopus holds. The plan says so, *Nothing to import*, and has no
  Start. It used to run, write 0 and look as though it never finished: the page held for a verify
  the server skips when nothing was written (`verify_skipped`). The page now reads that, and a
  run that wrote nothing ends *nothing new*.
- **The Bill Summary card is compact.** Its rows are 1.25 high with 1 px padding, and its section
  gaps are halved: a 735 px card is 622.
- **One bill keeps its width.** Without a comparison, a bill is as wide as it is beside one, half
  the row, not the whole of it.

**(12) Deleting imported history takes whole half-hours, back to the soft delete design §3**
(21 Oct 2026, the user, after a staging purge left the devices' rows with no meter beside them,
plotted and costed: the day's bill, `max(0, meter − Σdevices) + Σdevices`, is the devices alone).
- **The purge and *Delete… only the imported half-hours*** remove the devices' rows in the
  imported half-hours with the meter's: Device History's, and a live device row where a gap was
  filled under it.
- **HA's reads stay in `ha_reads`.** On the staging support file, every one of the 59,526 device
  half-hours the purge had left had its hour there.
- **The Octopus import rebuilds the devices** over the imported history once it has written, as
  the gap fill and the CSV import already did. §3 named all three; the API import never had it.
- **The Backup & restore modal is wider** (1,040 px), and a backup's row stays on one line.

**(13) The month counts kept across restarts** (21 Oct 2026, the user). The prod box, a thin
client, counted the whole history in 22 s at each restart (the warm-up's log line); the same count
is 0.9 s on a Mac. And (10)'s hourly full count would have repeated it hourly on a page load.
- **Kept in a file beside the database** (`health_counts.json`): each month with the digest it was
  counted under. A restart counts only what moved; a digest holds across processes, `hash()`
  doesn't.
- **No hourly count.** What it was there to notice joins the shared context: the VAT calendar, and
  the agreements once discovery has found them. Counts made before discovery are counted again once
  it has; a restart before discovery keeps what it has.
- **No warm-up after start.**

**(14) The support file says what EMT runs on** (21 Oct 2026, the user): `host` in support.json.
- **From the machine:** the platform; the processor (x86's model name, or a Pi's board); memory;
  uptime; the disk under the database (its device, rotational or not, its model, and whether an SD
  card or eMMC, NVMe or an SSD).
- **From the Supervisor's /info:** the board, the OS and the versions.
- **Nothing that names the machine:** no hostname, no address.

A helper reads the log's timings against it; the dialog says the file holds it.

**(15) An imported half-hour with no export is a gap** (21 Oct 2026, the user, after an Octopus
import). Octopus returned import and no export for three days of March 2025. Those half-hours had
no export figure at all, and the Export bill read them `none`, grey, as nothing exported.
- **Where the meter exports,** an imported half-hour (the API's, a CSV's) with neither export kWh
  nor a settled one is `no_data`: red, hatched yellow, an issue. *Re-fetch* may get it, or a CSV
  can supply it.
- **The imported-export delete records the span it cleared** (store_meta `export_deleted`), so a
  deliberate delete keeps reading grey *no export*, as the soft delete design §4 decided.

And pages carry `Cache-Control: no-cache` (Data Management after the upgrade needed a hard
refresh: a page's script is inline, so a cached page is stale code).

**(16) A bill clears the import's no-cost record** (21 Oct 2026, staging: 21 Jul's red blocks).
The refill of 21 Jul put 15 half-hours Octopus's measurements gave no cost for on the import's
no-cost record. The bill drain cached and applied their bills, but nothing took them off the
record, so they read "no cost, no longer asked" with the right cost on them.
- **An imported half-hour with its bill cached** is `applied` (or `differs`, if its cost isn't
  the bill), never `abandoned`.
- **Caching a bill** (`engine._store_bill`) takes the slot off the record.
- **The health counts file** goes to version 3.

The pricing check's log line also says the neighbours it re-priced apart, instead of
"corrected 28 of 26 re-checked".

---

## 2. Open

(a) **CAD-only and HA's cost sensors.** Does setup's *Disconnect Octopus* (or a mode change to
`cad`) ask for the rate sensors CAD-only pricing needs? **No** (traced 21 Oct 2026). The disconnect
lands on `cad` and drops the rate schedule (`engine.disconnect_kraken`); the page reloads with the
rate and standing-charge sensor fields shown, but nothing asks for them — its modal says only "add
a local meter sensor". With the fields empty a half-hour takes the last rate carried forward
(`last_known_rates`, one flat value) and then £0, with an hourly warning in the log. **Done (21 Oct 2026):** the disconnect's
modal names the import rate and standing charge (and export rate); Settings in `cad` says above
the import channel which is missing and what that does; the wizard without the API requires the
import rate sensor and asks before going on without a standing charge (or an export rate, with
export). And on the way: a channel's "use the supplier API" choice outlived the API — a
disconnect keeps `standing_charge_source = 'api'`, which forced the standing charge to 0 over a
mapped sensor (a staging copy's config: both sensors mapped, both sources `api`). Finalise now
honours the choice only while the mode uses the API.

(b) **The billing source's code.** The toggle's UI goes, and so does the wizard's step and the
POST (PR 7). **Decided (21 Oct 2026, the user): reset, not honoured.** The importer no longer
carries `billing_source`; a v4 on an API mode that chose `cad` has its main meter's half-hours
re-run against the settled kWh (transform 5, spec v25). With nothing left to store it,
`_get_billing_source` reads `dcc`; whether the passes' `cad` branch then goes is a separate
clean-up.

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
   sent as the delete's existing `reconstructed_only` with `utc_span`. As built, only the imported
   main-meter rows went and the devices' rows over them stayed "for a re-import to rejoin" — against
   the soft delete design §3; corrected in PR 12 (§1 (12)).
5. **The support file** — *built 21 Oct 2026*: `support_file.py` (identifiers, the cut, the zip,
   the estimate) and `/api/support/estimate`, `/build`, `/status`, `/download`; from the card and
   from a heatmap selection. Identifiers as found on a staging copy: `measured_cost.mpan`;
   `meter_channels.mpan`, which holds `serial-MPAN`; `kraken_state.kraken_account_number`;
   `meters.postcode_prefix` and `carbon_intensity.postcode` (the outward code) — none left in the
   copy, the log or the summary. The estimate is weighted by every time-indexed table's rows in the
   span: by the blocks alone it said 0.47 MB for a 30-day file that built at 2.0 (the rolling power
   and SoC histories, two days dense); weighted, 2.8 / 0.60 / 13.1 MB against 2.0 / 0.57 / 11.8 for
   30 days / July / everything. Clicked through: the dialog's estimate raced (an earlier choice's
   reply landing last) — only the latest is shown now.
6. **Corrections on the Billing tab** — *built 21 Oct 2026*: right-click (long-press) a day chart
   → *Correct the import (export) rate, HH:MM – HH:MM…* or the day's standing charge, which the
   day's summary panel offers too; CAD-only (`/api/billing-source`'s `uses_api`), on the Charts
   page and not the Lovelace card. The corrections routes take `{day, slot}` (a rate) or `{day}`
   (the standing charge) in place of the dates and times — not `from_utc` / `to_utc`, so the page
   never converts a time: the slot is the chart's, the local clock position, so on the clocks-back
   day the repeated hour is the later block, as its bar is, and the clocks-forward day's missing
   slots match nothing. The modal: the half-hour as it stands (the preview with no value), the
   nearby rates, the value, *recalculate the cost*, the preview with the devices under the meter
   and only the meter's £ totalled, the apply behind the required `pre-correction` backup, the
   page refreshed. The preview had listed each device twice under `meter_id: "all"` (once among
   the meter rows at its raw kWh); now once, at its grid kWh, flagged `device`. Clicked through on
   a copy of staging's database set CAD-only: 19 Oct 02:30 BST corrected alone (its neighbours
   untouched, the battery's cost from its grid kWh), the standing charge on exactly 48 half-hours.
7. **Retire the sub-pages** — *built 21 Oct 2026*: Fill History & Gaps, Delete Blocks and Cost
   Corrections gone, `/fill-history` and `/delete-blocks` → Data Management, `/corrections` →
   `/charts?tab=daily` (Charts takes `?tab=` once, over the remembered tab); Data Management's
   topbar keeps *Backup now*. The routes only those pages called go with them: the deleted-ranges
   list and its *Re-import* (`/api/blocks/deleted-ranges`, `/api/blocks/reimport` — a deleted
   half-hour is a missing one on the heatmap, "deleted on …", and filling it lifts the deletion),
   *Review unsettled blocks* and *Retry settlement* (`/api/unsettled-blocks`,
   `/api/retry-settlement`), `/api/import/extract-zip` and `…-by-name`; and with them
   `apply_billing_source_change`, `flag_all_for_pass2_rerun` and `get_unsettled_blocks`, left
   with no caller. The settlement source: the card, the wizard's step and the POST gone; §2 (b).

8. **The jobs in the topbar** — *built 21 Oct 2026*: §1 (8). Clicked through on a copy of
   staging's database:
   - the menu is whole on first paint;
   - the modal opens and closes, and Escape leaves a modal above it first;
   - a zip dropped on the heatmap opens the restore, and one dropped on the modal's row opens it
     once;
   - `?open=backups` works;
   - at phone width the three fit one row.

9. **After the layout** — *built 21 Oct 2026*: §1 (9). Clicked through on a copy of staging's
   database:
   - **CAD + API:** 6 of 41 months shown, the rest in 2 lines; an expanded clean month held no
     issue row; no Missing view.
   - **Set CAD-only:** Missing the only view, 2 months shown.
   - **A drag** from 21 Oct 05:00 to 19 Oct 15:00: 19 and 20 Oct whole, and 21 Oct up to its last
     finished half-hour. Within one row it stays cell to cell.
   - **Each Whole history task** opens on its own flow.

10. **The second round** — *built 21 Oct 2026*: §1 (10). On a copy of staging's database:
    - **Health:** first count 0.88 s; after new data with nothing old moved, 0.31 s with no month
      counted again (the days' fingerprints are most of it); cached 0.07 s.
    - **Billing:** at 1400 px the chart matches its summary; at phone width it stays 320.
    - **Octopus import:** *Preview plan* is enabled on opening.

11. **The third round** — *built 21 Oct 2026*: §1 (11). Locally, with the plan Octopus answered
    on staging, there is no Start and the message is shown; at 1400 px a bill is 554 px with or
    without its comparison.

12. **Deletes take the devices** — *built 21 Oct 2026*: §1 (12). On a copy of staging's database:
    - **The purge:** 48,098 imported meter half-hours and 59,622 device rows in them, in 0.8 s.
      That's the 59,526 the staging purge left, plus 96 live device rows on 21 Jul 2026, a day
      filled under live devices. None is left without its meter; `ha_reads` (74,201) and the live
      meter rows (11,110) are untouched.
    - ***Only the imported* over 1–4 Mar:** 288 rows went, 1–2 Mar's meter and both devices; 3–4 Mar,
      live, stayed whole. The preview counts the same 288.

13. **The month counts kept** — *built 21 Oct 2026*: §1 (13). On a copy of the prod box's
    database (141k blocks, Mac): the first count 0.89 s, and after a restart 0.32 s with no month
    counted again, the same answer. The file is 9.8 KB.

14. **The machine in the support file** — *built 21 Oct 2026*: §1 (14).

15. **Imported export gaps** — *built 21 Oct 2026*: §1 (15). On the support file's database: 144
    half-hours `no_data`, exactly 5–7 Mar 2025, and nothing else in the 23,865 imported changed
    (`applied` 16,104, `none` 7,617).

16. **A bill clears the no-cost record** — *built 21 Oct 2026*: §1 (16). On the support file's
    database, 21 Jul reads 22 `applied` and 26 `none` (no import), where it was 15 `abandoned`; across
    the whole history no half-hour is `abandoned` or `differs`.

1 comes first, 2 after it; 3–6 stand alone; 7 last, once 2, 4 and 6 have taken the pages' jobs. 8 after staging, once all seven had shipped.
