# EMT v5 — the accumulation chart: the spirals into Usage Stats (BL-104)

*Status: decided 1 Nov 2026 (§5); PRs 1–4 built. The user's idea (1 Nov 2026): "move the spirals into Usage Stats as an
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
- **Years:** Usage Stats' own year chips.
- **Hidden in this view:** the period buttons and Billing / Calendar (a running total is by day),
  Net, the period sort.
- **The table:** a row per year — each measure's total, its change on the year before (an amount:
  a net that crosses nought makes a percentage meaningless), and its total to the same day of the
  year as the latest day, so this year compares with the last like for like.
- **Eras and tariffs** on the line, as on the bars; on the spiral, an era as a tick at its angle.
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
| 4 | **built:** eras and tariffs on the line — the tariffs along Lifetime only, the eras in their year's colour on Year by year — and an era's chip on the spiral |
| 5 | the Spiral tab retired, its links and saved state carried over |
