# Overview: rows, and today against yesterday

> **History — a design record.** What was decided, when and why, as it stood then; parts describe code
> that has since changed. How it works today: [reference/api.md](../../reference/api.md#the-overview) and the
> code. PR numbers (`#NNN`) are in this repository, `RGx01/EMT`: part 1 (layout) #216, part 2 (lines) the PR after.

*Status: decided 22 Nov 2026 (the user, answering six questions); both parts built the same day.*

## Why

The Overview's layout is semi-fixed. `_applyLayout` counts the power cards. With 8 or fewer, the Carbon
Intensity card joins their row and flexes against them ("layout-a"). With more, it moves beside the
48-hour history card ("layout-b"). So the Carbon card grows and shrinks as devices come and go. Its
neighbours are the power gauges, not the cards it belongs with. A card can be hidden (`overview_hidden`,
7 Nov 2026), but none can be moved.

The Live Power card also has room it doesn't use, and the device cards show only the moment.

## What was decided (22 Nov 2026, the user)

### Rows

The page is a column of **rows**, each a fixed group of cards, by default in this order (Power first, as the
gauges were):

| Row key | Cards |
|---|---|
| `power` | Live Power · each device card |
| `grid` | Generation Mix · Carbon Intensity · 48-hour History (kW / CO₂ / Mix) |
| `charge` | Smart Charging (accounts with dispatch data) |
| `agile` | Agile import · Agile export |
| `costs` | Today · This Bill · This Year — **always last, not orderable** |

- **Row order** is the user's. An *Arrange* button gives each orderable row ▲▼ buttons, rather than
  dragging, so it works on a phone and in Safari. The order is saved on the server beside
  `overview_hidden`, as `overview_rows`: a list of row keys. Keys it doesn't name follow in the default
  order above, so a row added later appears without a migration.
- **Card order within the Power row** is the user's too, Live Power included. It is saved as
  `overview_power_order`, a list of card keys (`power`, `device-<meter_id>`). A device not in the list
  follows in config order; a key whose device has gone is ignored.
- **The Grid row:** Mix and Carbon take their natural width, and History takes the rest. Below a width
  where the three don't fit, History wraps to its own line.
- **The Power row is spread evenly.** With `n` visible cards and room for `k` across at the minimum
  card width, it uses `r = ceil(n / k)` lines with `ceil(n / r)` cards on each. Every card grows to fill
  its line. So with room for 4, 5 cards are 3 + 2, not 4 + 1, and 7 are 4 + 3. It is recomputed on
  resize and whenever a card is built, hidden or shown.
- **The Costs row** stays last and can be made compact: one switch (`overview_costs_compact`) shows
  each card's total and period only, without the breakdown rows.
- Hiding a card is unchanged, and a row whose cards are all hidden takes no space. On a phone (≤ 600 px)
  every row is one column, in the user's order.

### Today against yesterday

Cards **do not get taller**. To make room for the line, the gauges (and the battery's level bar) shrink.

- **Live Power:** cumulative **import** and **export** since local midnight, drawn over yesterday's from
  midnight to midnight. Yesterday is dimmed.
  - The source is the main meter's half-hours (`blocks`: the meter's own figures, with gaps filled from
    Octopus), plus the current half-hour from the live reading, so today's line moves as you watch.
- **A device card:** the device's cumulative energy **from its power sensor**, today over yesterday.
  - This is the device's own measurement: what it drew, from any source. It is not its grid share, so
    it is not what Billing charges to it.
  - **V2X** shows **in** and **out**, as a V2X device's energy sensor is import-only (Meter Config says
    so) and its power sensor is the only record of energy out.
  - The source is `sub_meter_history`: the engine samples each device's power sensor every tick.
    Energy is the trapezoid sum of the samples. A gap of over 5 minutes between samples (EMT was down)
    adds nothing, rather than guessing across it.
  - Kept **50 hours** (was 48), so yesterday is whole from midnight late in the day, across a clock
    change.
  - A device with no power sensor draws its line from its own half-hours (`blocks.imp_kwh`), as steps.
- **The battery card** flicks between **SoC** and **energy in / out**. Both are today over yesterday,
  from `sub_meter_history`, and in/out comes from the power sensor as for V2X. The choice is remembered
  per card (`overview_battery_view`: {meter_id: "soc" | "energy"}). Without an SoC sensor there is no
  switch, and it shows energy.

## Build

1. **Layout** (one PR): the rows, Arrange, the Power row's even spread, card order, compact costs.
   `_applyLayout` and the layout-a / layout-b classes go.
2. **Lines** (one PR): an endpoint giving each card's two days of cumulative series, the cards' small
   charts, the battery switch, and `sub_meter_history`'s retention.

## Not done

- The lines are not the billed figures, except Live Power's. A device's line is its sensor's view.
  Reconciling a device's sensor against its billed grid share belongs on Billing, not here.
- No drag and drop.

## As built (part 2)

- `overview_days.py` (pure) and `/api/overview/days`. A device line holds level through a stretch with no energy
  and runs level to its day's last sample; drawn straight from point to point it climbed through idle hours.
- `minutes()` subtracts in UTC: Python subtracts two times in the same zone by the wall clock, which made the day
  the clocks go back 24 hours, not 25 (caught by its test).
- The half-hour in progress is added only when it starts where the finished half-hours end.
- Heights: on a copy of a real database with a battery, a V2X car and a heat pump, the Power row was 244.8 px at
  1,400 px before and after. The charts first made it 20 px taller; 6 px gaps in the power cards (were 10) and
  gauges 112 × 62 px (were 180 × 100) won it back.
