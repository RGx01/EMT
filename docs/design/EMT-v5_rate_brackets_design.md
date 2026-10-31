# EMT v5 — rate brackets: where a device's usage fell by price

*Status: built, 31 Oct 2026 (ROADMAP BL-93): PR 1, the read, and PR 2, the view. What it is: a view in Usage Stats that shows, for the
whole meter, the house or one device, how much grid import fell in each 2.5p price bracket, per day,
month and year. Decided with the user the same day (§6): brackets for every tariff and no band
labels; billed and not-yet-billed half-hours alike; grid import only; 2.5p brackets. Code facts read
at `dev` after #363.*

---

## 1. The question it answers

*Where does my usage fit in the rates?* For example: how much of the car's charging was cheap, how
much of the house's draw fell on the peak rate, whether a battery charged when the price was low, and
how that changes month by month and year on year.

## 2. Why brackets and not bands

The first sketch named the bands (off-peak, peak, a dispatch outside the window, the IOG cap). Two
findings ruled that out:

- **The stored band label is only meaningful on Intelligent.** `block_segments.band` is set by the
  Intelligent dispatch logic (`predicted_band`, `settle.py`). Every other tariff writes `standard`.
  On the staging copy, all of a Go agreement's segments are `standard` despite Go's night window,
  and Agile, Cosy and Flux would be the same. Named bands would need a rule per tariff kind.
- **Agile has no bands.** Its price changes every half-hour.

**A half-hour's rate already carries everything the band would say.** IOG's dispatches and the
6-hour cap, Go's and Cosy's windows, and Agile's price are all in the rate the half-hour was charged.
So the view groups by **price**, with one rule for every tariff. It never asks what the tariff is.

## 3. What is counted

**Each half-hour's grid import, at the rate it was charged, inc VAT**, as the other charts count it.

| row | kWh | rate |
|---|---|---|
| whole meter | each `block_segments` slice of the main's import | the slice's `inc_rate` |
| a device | its `COALESCE(imp_kwh_grid, imp_kwh)` | its stored `imp_rate` |
| house | the meter's bracket minus the devices' in the same bracket, per half-hour, clipped at 0 | — |

- **The meter is read by slice, not by block.** On IOG-SMB, Octopus bills the car and the house at
  different rates in one half-hour, and the slices keep the two apart. A block's `imp_rate` would
  blend them.
- **A device by its own stored rate**, so its bracket £ adds up to its device cost on every other
  surface. PASS 2 gives a device the main's effective rate (`engine._apply_pass2`); on the staging
  copy's split half-hours each device's rate equals one of the main's slice rates.
- **The house clip.** Where a half-hour carries more than one rate, a device has one rate and the
  meter has two. In principle the house remainder can then come out a few hundredths of a kWh below
  zero in one bracket and over in the other. This happens in 9 of about 37,900 main half-hours on the
  staging copy. The clip at 0 is a display rule only, and the table's total row is the meter's own.
- **Not-yet-billed half-hours count the same as billed ones** (the user). Their rate is EMT's
  prediction until the bill arrives, as on every other chart.
- **Not counted:** the standing charge (it is not a unit price) and usage that did not come from the
  grid (`imp_kwh − imp_kwh_grid`, the user: not needed).
- **No new storage, writers or Octopus calls.** It is one read over `block_segments` and the
  blocks.

### 3a. Export and plunge pricing (added 31 Oct 2026, the user)

- **Export is a series of its own** (`export`): the main's `exp_kwh` at its `exp_rate`, with its
  stored `exp_cost` as the credit. On the staging copy every exporting half-hour's cost is its kWh ×
  rate, and the series sums to the stored export. It matters most on Agile Outgoing, whose price
  moves by the half-hour; on a flat export tariff it is one bracket.
- **A plunge keeps its sign** throughout: a negative rate's kWh earn a negative £ on the meter, the
  house and a device, below 0p. Nothing in the read clipped one before export was added; a test now
  pins it, as the staging copy has never been on Agile.
- **A rate of exactly 0p** is a rate, not a missing one: £ ÷ kWh is used only when the rate is NULL.

## 4. The brackets

- **2.5p wide, inc VAT** (the user). The bracket for rate *r* p/kWh is `[2.5·n, 2.5·(n+1))` with
  `n = floor(round(r, 4) / 2.5)`. Rounding first matters: stored rates are £/kWh floats, and a rate
  of exactly 7.5p must not fall into 5–7.5p through a representation error.
- **Fixed edges**, the same in every period, so a month's bars compare with any other month's.
- **Negative prices** (Agile's plunges) get brackets of their own on the same 2.5p grid, such as
  −2.5–0p.
- **Only brackets with usage are shown.** On the staging copy, three years of Go and Intelligent fall
  into about ten brackets. A year of Agile could fill thirty or more, so colour carries the order and
  not the identity (§5).
- **VAT.** Brackets are by what was paid, so the 0% VAT change on 1 Oct 2026 moves a tariff's
  unchanged ex-VAT rate into a lower bracket. That is correct for "what did it cost", and the tariff
  overlay's VAT tick marks it.

## 5. How it is shown

In Usage Stats, not a new page. It reuses the period buttons (Monthly, Daily, HH), the
Billing / Calendar toggle, the date panel, the kWh / Cost switch, the tariff overlay and the
resizable table.

- **A Show switch: Devices | Rate brackets.** Devices is today's chart. Rate brackets adds a picker:
  whole meter, house, or one device. One series at a time keeps the stack readable.
- **The chart:** a stacked bar per bucket, one segment per bracket. kWh or £ follows the metric
  switch, and a **%** option shows each bracket's share of the bucket's **kWh**, in Cost too: a
  plunge's credit takes from a bar's £, so a share of £ could pass 100% beside it. Export is drawn
  below the axis, as on the Devices view, its figures positive in the hover and table (Credit), and
  its colour ramp reversed, the best-paid bracket green. A negative bracket is named "−5 to −2.5p".
  Brackets are coloured green
  through amber to red by price across the brackets in view, and neighbours alternately lighter and
  darker; a negative price is blue. *As built:* a fixed 0–40p scale made 5–7.5p and 7.5–10p the same
  green on the staging copy, and spreading the colours by position made 22.5p read as cheap. The
  legend names each bracket in view, as do the hover and the table, so the colour need carry only
  the order.
- **HH:** a half-hour usually falls in one bracket, so each bar is one colour.
- **The table:** one row per bracket for the selected range, cheapest first. Columns: kWh, £,
  average p/kWh (£ ÷ kWh, which matches the bill rather than the bracket's midpoint) and share of
  kWh, with a total row. The table goes down the brackets, not across them: a period-by-bracket grid
  of thirty columns is unreadable. Copy to clipboard copies it as it stands.
- **One line above the table:** the series' average p/kWh against the whole meter's for the range
  (illustrative: "EV charger 7.4p/kWh, whole meter 14.9p/kWh"). It says whether the device chased
  the cheap half-hours, on any tariff.
- **Running total, Net, Standing charge, CO₂ and the period sort** are hidden in this view. They are
  about the bill, carbon or the period table, not the price. The tariff overlay stays.
- **When it is read** *(as built)*: only while the view is shown; all history's days once per data
  version, and an HH day's half-hours once per day and version. A new half-hour moves the version, so
  a page left on the view reads the days again: 0.37 s on the staging copy on the Mac.

## 6. Decided (the user, 31 Oct 2026)

- **Brackets on every tariff, no band labels.** That covers IOG, the cap, the time-of-use tariffs and
  Agile with one rule.
- **Not yet billed is treated as billed**, as on the other charts.
- **Grid import only** at first; **export added** the same day as a series of its own (§3a). No
  not-from-grid bucket.
- **2.5p brackets.**

## 7. Open

- **Bracket width as a setting.** Fixed at 2.5p for now. A setting is cheap to add later.
- **Ex-VAT brackets.** These would make a tariff's brackets steady across the 1 Oct 2026 change. Not
  proposed: inc VAT is what the user paid and what the other charts show.

## 8. Order

| PR | what |
|---|---|
| 1 | **built (PR 1):** `usage_view.day_brackets` / `slot_brackets` — two reads (the main's import slices, the devices' grid kWh, rate and £) over the range, bracketed per half-hour, the house and its clip, summed per local day or half-hour as `{series: {n: [kWh, £]}}` at 4 dp; the page sums days into its buckets and the range, as it does the stats. `/api/energy?fields=brackets` at `day` or `block`, asked for on its own, so the stats payload is unchanged. On the staging copy, all history: 0.37 s, and house + devices equals the meter in kWh and £ |
| 2 | **built (PR 2):** the Show switch, the series picker, the stacked chart, the % option, the table and the average line in `charts.html`; the day's brackets summed into each bucket in `barAggRows`, as the meters are, so Monthly, Daily, Yearly and Billing / Calendar need nothing of their own |
| 3 | **built:** export as a series (§3a), drawn below the axis with its ramp reversed; % of kWh; a plunge's sign pinned by a test; a 0p rate read as a rate |

Tests:
- a rate on a bracket edge lands in the upper bracket;
- an IOG-SMB half-hour with two slice rates puts the car and the house in different brackets;
- the brackets' £ sum to each device's and the meter's cost for the range;
- the house clip appears only on a split half-hour.
