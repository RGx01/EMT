# EMT v5 — the accumulation chart: the spirals into Usage Stats (BL-104)

> **History — a design record.** What was decided, when and why, as it stood then; parts describe code that has since changed. How the feature works today: its help page in the app, and the code. PR numbers (`#NNN`) are in the private archive, `RGx01/EMT-archive`.

*Status: decided and built, 1 Nov 2026 (§5); the Spiral tab retired. The user's idea (1 Nov 2026): "move the spirals into Usage Stats as an
accumulation chart which can be viewed on a linear or spiral". Code facts read at `dev` after #380.*

---

## 1. What the Spiral tab is today

`EMTSpiral` in `charts.html`, from the same day rows Usage Stats reads (`EMTDayRows`:
`/api/energy?resolution=day&kind=all&fields=stats`).

- **Four spirals side by side:** Cost, Usage, Carbon (with a postcode or carbon data) and Standing charges, each a
  running total wound once a year, January at the top.
- **One shared grouping:** Net (import − export), Import, Export, or a device.
- **Winding:** *Lifetime*, one coil from the first day; or *Year-aligned*, each year from nought on
  the same scale, so the loops overlay.
- **Years** to hide; *include the standing charge in net cost*; an expand view; the PDF.

Usage Stats already has the period buttons, the metric switch (kWh, £, CO₂), the **Show** switch
(Devices, Rate brackets), the years, the table, the tariff and era overlays and the PDF.

## 2. Proposed: a third view in Usage Stats

**Show: Devices | Rate brackets | Running total.** In *Running total*:

- **Line or spiral** — a switch. The same running total, drawn along a time axis or wound by year.
- **A card a measure, side by side, as the spiral has them** (the user): Cost, Usage, and Carbon where
  there is any. The metric switch is hidden in the view. The *Standing charges* spiral becomes a
  series (below): with it chosen, the Cost card alone.
- **Series** — a picker, as Rate brackets has: Net, Import, Export, each device, Standing charges.
  *Include the standing charge* stays, beside £.
- **Winding:** *Lifetime* (one line, or one coil, from the first day) or *Year by year* (each year
  from nought: overlaid lines on one year's axis, or the spiral's overlaid loops).
- **The year starts** (home eras design §3.2): 1 January, the billing anniversary, a tariff's start, or
  an era's start. *Year by year* counts each year from that day; the spiral turns so it is at the
  top. A year is named by its start (*from 14 Mar 2025*).
  The picker groups them: the year's own two, the tariffs newest first, then the eras as the eras
  modal lists them — by kind, Solar and Battery first, newest first in each (1 Nov 2026).
  Under each tariff, indented and newest first, the days its prices changed inside it (the bars'
  minor ticks), up to today: *Intelligent Go — price change 1 Oct 2025*; a day only VAT changed (every
  price the same before VAT, as a fixed tariff on 1 Oct 2026) is a *VAT change*, and its tip says so.
- **Years:** Usage Stats' own year chips.
- **Hidden in this view:** the period buttons and Billing / Calendar (a running total is by day),
  Net, the period sort.
- **The table:** a row per year — each measure's total, its change on the year before (an amount:
  a net that crosses nought makes a percentage meaningless), and its total to the same day of the
  year as the latest day, so this year compares with the last like for like.
- **Eras and tariffs** on the line, as on the bars; on the spiral, an era as a tick at its angle.
  On *Year by year* the import tariffs are dots on the lines (1 Nov 2026): a filled one at each
  agreement's start, a smaller hollow one at each price change inside it, on its year's line at that
  day's total, in the year's colour; the hover is the bars' tariff tip, with the prices from that day.
  The spiral has the same dots on its coil, on both windings. The Tariffs box hides them.
- **The PDF** draws whichever is shown.

## 3. The Spiral tab

Retired once the view has what it had: `?tab=spiral` and a remembered Spiral tab open Usage Stats on
*Running total, spiral*, and the spiral's saved state (grouping, winding, hidden years) carries over.

## 4. Not changed

The day rows and their read (`EMTDayRows`), the figures, the spiral's drawing (moved, not redrawn).
No new storage, writers or Octopus calls.

## 5. Decided (the user, 1 Nov 2026)

1. **Cards side by side, as the spiral** — not one chart following the metric.
2. **Standing charges a series.**
3. **The table's like-for-like column**, yes.
4. **The Spiral tab retired** once the view matches it.

## 6. Order

| PR | what |
|---|---|
| 1 | **built:** *Running total* in Usage Stats, **line**: the cards, the series, Lifetime / Year by year, the years, the table, the PDF |
| 2 | **built:** the **spiral** in that view — the spiral's drawing moved in, the expand view, the PDF's spirals. *As built:* the lifetime coil starts at the first year shown, as the line does (the tab counted hidden years in) |
| 3 | **built:** **the year's start** — 1 January, the billing anniversary, an import tariff's, an era's; the years named *2025/26*; a start on 29 February the month's last; a choice gone, 1 January |
| 4 | **built:** eras and tariffs on the line — the tariffs along Lifetime (and, since 1 Nov 2026, as dots on Year by year and on the spiral), the eras in their year's colour on Year by year — and an era's chip on the spiral |
| 5 | **built:** the Spiral tab retired — its button, markup, `EMTSpiral`, PDF branch and refresh; a remembered tab or `?tab=spiral` opens the Running total's spiral, its saved view carried over once |
