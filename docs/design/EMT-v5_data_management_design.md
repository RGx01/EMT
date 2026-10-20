# EMT v5 — Data Management rebuilt around the data heatmap

**Decided so far (the user, 20 Oct 2026):**
- **(a) Charts is read-only, with the energy views only** (kWh, gCO₂, gCO₂/kWh); the data views and
  every tool are on Data Management; a cell's detail panel links *Open in Data Management* for its
  day (`?day=`). The Lovelace card uses chart mode.
- **(f) The health strip stays**, counting with the heatmap's own rule: one call returns each
  view's counts per state over the whole history, and the page sums them with `isIssue` (0.5 s
  over staging's 59,162 half-hours). The Charts pill (`settlement_awaiting`) is a different model
  and is not used.
- **(3) Corrections are not a heatmap action.** A wrong rate is noticed on the Billing tab: a
  right-click (long-press) on a half-hour's bar or rate line → *Correct this rate…* → a modal for
  **that half-hour only** (no run, no repeating span), the standing charge from the day's summary
  row. **Offered in CAD-only mode only**: with the supplier API, Octopus's bill (or settled kWh × a
  published rate) is the answer and *Re-fetch* the tool — and a correction sets one rate over the
  whole half-hour, which cannot express IOG-SMB's EV and house rates. CSV history and bills that
  never come could use it, but not without SMB; not worth it. The Cost Corrections page goes.
- **Migrated corrections are left for the user to fix with *Re-fetch*,** which already replaces a
  correction Octopus's bill agrees with (`confirms`, 15 Oct) or differs from.
- **(e) The billing settlement source toggle is retired, not moved.** It only means anything in
  `cad+api` (v4's `mode_ui_design.md`), re-runs every main-meter half-hour when changed, and on an
  Intelligent tariff Octopus's bill is final whichever kWh EMT holds. Switching to CAD-only is
  setup's existing *Disconnect Octopus* — *open:* whether CAD-only setup asks for the HA cost
  (rate) sensors it needs.

*Draft, 20 Oct 2026 — the user: "integrate the data heatmap into Data Management instead of the
charting heatmap; the whole screen needs a rework, including the Safety & housekeeping layout."
Builds on the data tools design (BL-88: every tool an action on a heatmap selection) and the
heatmap issues design. Every line in §0 was read in the templates and routes on `dev`; nothing here
is decided.*

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

## 1. The proposal

**Data Management becomes the data heatmap's page.** Charts keeps the heatmap as a chart; Data
Management hosts it as the data-quality surface, with the tools.

```
┌ Data Management ───────────────────────────────────────── [💾 Backup now] ┐
│ ┌ Health ──────────────────────────────────────────────────────────────┐  │
│ │ Import bill 12 to look at · Export bill 3 · missing 0 · last backup  │  │
│ │ 14:02 · database 87 MB, runway 9 years                               │  │
│ └──────────────────────────────────────────────────────────────────────┘  │
│ [Import bill] [Export bill] [Missing]   ☑ Issues only                     │
│ ┌ the heatmap, a month at a time, newest first ────────────────────────┐  │
│ │  select a half-hour, a day or a drag → the menu:                     │  │
│ │   Re-fetch · Fill this gap… · Correct the rate… · Delete… ·          │  │
│ │   Delete the imported export…                                        │  │
│ └──────────────────────────────────────────────────────────────────────┘  │
│ ┌ Whole history ───────────────────────────────────────────────────────┐  │
│ │ Import from Octopus · Import a CSV or PDF bills · Recover every gap · │  │
│ │ Delete all imported history                                           │  │
│ └──────────────────────────────────────────────────────────────────────┘  │
│ ┌ Safety & housekeeping ───────────────────────────────────────────────┐  │
│ │ Backups: create · the list (restore / download each) · restore a file │  │
│ │ Storage: size, free, growth, runway · Compact                        │  │
│ └──────────────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────────┘
```

**(1) The heatmap in a data mode.** `EMTHeatmap.init(el, {mode: 'data'})`: the data views only —
Import bill and Export bill (where they show today: the supplier API, and an export channel for
the Export bill — `_settle_views`), and a **Missing** view (the kWh grid
drawn as present / missing, so a no-API install has a data view too); *Issues only* on by default;
every tool in the menu. Charts: `{mode: 'chart'}`, the energy views only, read-only — a cell's
detail panel keeps an *Open in Data Management* link to that day, which needs the day URL
parameter (`/data-management?day=2026-07-21`).

**(2) A health strip.** What the page is for, in one line: the *To look at* count per view, missing
half-hours, the last backup's time, the database size and runway. Each figure is a link: a count
scrolls the heatmap to the newest month with that issue; the backup to its card.

**(3) The selection menu, complete** (data tools design §1): today's four, plus **Correct the
rate…** — the Cost Corrections form as a modal on the selection, its range shown, not typed.

**(4) Whole-history actions,** one card — the jobs with no range: the first or a further **import
from Octopus** (Historical Import's API task), **a CSV or PDF bills** (its other tasks, as now),
**Recover every gap** (the blanket fill that skips tombstones), and **Delete all imported history**
(the purge, from Delete Blocks). Each opens Historical Import's flow as a modal, as the gap fill
does, so its forms are used, not copied.

**(5) Safety & housekeeping, two cards instead of four:**
- **Backups** — *Create backup now* once (the topbar keeps the shortcut); the list as now (Download,
  Restore, Delete), each row saying what made it — its label (`pre-delete`, `pre-gap-fill`,
  `manual` …) and age read out, not the bare file name; and *Restore from a file…* (today's drop
  zone) as a row at the end, not a card of its own.
- **Storage** — today's four figures and the bar, with *Compact* under them: compacting is a
  storage action, and the card says when it would help ("x MB free inside the file").

**(6) What goes:**

| goes | to |
|---|---|
| Fill History & Gaps page | nothing — its two links are the Whole history card and the device cards |
| Delete Blocks page | *Delete…* on the heatmap; the purge to Whole history |
| Cost Corrections page | *Correct the rate…* on the heatmap; **Billing settlement source** to Settings (it is a setting, not a tool); **Review unsettled / Retry settlement** — superseded by the Import bill's amber and red with *Re-fetch*, if (d) agrees |
| the Missing Data nudge | the health strip's missing count |
| Historical Import page | stays, as the flows the Whole history card and *Fill this gap…* open; reachable by URL |

---

## 2. Open

(a) **Charts: read-only, or keep the tools?** Proposed read-only, the tools in one place. A user
spotting a red cell on Charts gets *Open in Data Management*.

(b) **The Missing view** — a new view, or the Import bill's black hatching is enough (no-API
installs have no Import bill view)?

(c) **Correct the rate…** as a modal over the heatmap (Cost Corrections' form, embedded as the
gap fill embeds Historical Import), or rebuilt in the heatmap's own dialog style?

(d) **Review unsettled / Retry settlement**: does anything in it still earn its place once the
Import bill shows every unsettled half-hour and Re-fetch fixes a range? *Retry settlement*
re-fetches DCC kWh from the oldest unsettled block to now — a whole-history action if kept.

(e) **Billing settlement source** to Settings — or does it stay on this page as a data choice?

(f) **The health strip's numbers** — computed in the page from the heatmap's months (only the
months drawn), or one server call over the whole history (`settlement_awaiting` already counts the
bill stages)?

---

## 3. PRs, if agreed

1. **The heatmap's mode and the day parameter** — `init(el, opts)`; Charts in chart mode;
   `?day=` opens a month and marks the day.
2. **Data Management's new layout** — the health strip, the heatmap in data mode, the Whole history
   card (modals onto Historical Import), Safety & housekeeping as two cards.
3. **Correct the rate… on the selection** — the corrections form on a selection.
4. **Retire the sub-pages** — Fill History & Gaps, Delete Blocks, Cost Corrections (settlement source
   to Settings), the nudge; redirects from the old URLs.
