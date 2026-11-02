# EMT v5 — the help pages (BL-102)

> **History — a design record.** What was decided, when and why, as it stood then; parts describe code that has since changed. How the feature works today: its help page in the app, and the code. PR numbers (`#NNN`) are in the private archive, `RGx01/EMT-archive`.

*Status: built 1 Nov 2026 (#387, #388 and PR 3); decided the same day (§4). Code facts read at `dev` after #386.*

---

## 1. What is there

One page, `help.html`, about 490 lines: fourteen cards written for v4 (3.x), most of them about
concepts, a few about pages. On a phone each card folds.

## 2. What is wrong with it (checked against the code)

- **Out of date:**
  - *"use dispatch overlay"* is offered as a rate option in Meter Config — v5 has no such option;
    a dispatch prices a half-hour through `settle()` (engine steps 4b–4e).
  - A *billing source toggle* (meter sensor vs supplier API) — not on v5's Settings.
  - *"From 3.0"*, *Meter Config*, *Change Setup*'s place, the device gap note — v4's words and limits.
  - The wiki and repository links point at v4's repository, which goes private when v5 launches.
- **Missing — most of what v5 is:**
  - **Data Management:** the data heatmap and its views, the health strip, the tools on a selection
    (re-fetch, gap fill, delete), backups and restore, the support file.
  - **Usage Stats:** Devices, Rate brackets, Running total (line or spiral, the year's start), the
    tariff band and the eras.
  - **Settlement:** the stages a half-hour waits at (kWh, bill, split), the pill, *Retry settlement*.
  - **Overview:** the smart charging card (the 6-hour allowance), the Agile card (and *Agile in your
    region*).
  - **Settings:** the tabs, a device's sensors and history, the EV with no device, home eras, bar colours.
  - **Insights:** the Usage tab; Rate Brackets.
- **No way in from a page:** nothing links a page to the help that explains it.

## 3. Proposed

**A page each, organised by where you are** (the user) — `/help` an index of them, each at
`/help/<section>` with a shared side list to move between them; the pages the app's own, in the
navigation's order, then the ideas they share:

1. **Getting started** — what EMT is, the two ways to feed it (local sensors; the supplier API), what
   it needs from Home Assistant.
2. **Overview** — the gauges, the 48-hour history, today and the bill, smart charging, Agile.
3. **Charts** — Billing (the day charts, a correction), Heatmaps, Usage Stats (each view), the ⚙︎
   (home eras, bar colours), the PDF.
4. **Insights** — Carbon and Usage.
5. **Data Management** — the heatmap as the data's health, the tools, backups and restore.
6. **Settings** — the site, the main meter, each device, the EV with no device, the data source.
7. **How EMT counts** — the half-hour, the house and the devices (grid first to the EV), settlement's
   stages, prices and VAT, Intelligent Octopus Go and the 6-hour cap, carbon.
8. **Running EMT** — storage and backups, the HA sensors, standalone Docker and Lovelace.

**A way in from each page:** a small **?** in each page's header, opening its help page (the user).

**A reader's guide** (the user): what each thing shows and why, in the app's words. The design detail
— how a half-hour settles, the rules behind the split — goes in the wiki when it is redone.

**Links** to v5's repository (`RGx01/EMT`), not v4's.

**Kept true:** each claim checked against the code as it is written (the working rule), and the
figures that can be — the sensor names, the backup path, the port — read from the code in a test.

## 4. Decided (the user, 1 Nov 2026)

1. **A page for each section.**
2. **The ? on each page**, yes.
3. **Links to v5's repository.**
4. **A reader's guide**; the design detail in the wiki when it is redone.

## 5. Order

| PR | what |
|---|---|
| 1 | the help index and its route (`/help/<section>`), the shared side list; Getting started, Overview, Charts, Insights |
| 2 | Data Management, Settings, How EMT counts, Running EMT; the old page's last cards gone |
| 3 | the **?** on each page |
