# Flow — where the grid's energy, cost and carbon went (design)

*Decided, 4 Nov 2026. A Usage Stats view beside Devices, Rate brackets and Running total. Decided with
the user on 4 Nov 2026:*
- *three columns (grid → rate band → device);*
- *the period as the other views have it;*
- *the Chart.js Sankey plugin;*
- *export as a flow out of the house;*
- *small bands grouped into "Other";*
- *export's caveat as a note in the hover;*
- *retired devices under their name.*

*Nothing is open (§7).*

## 1. What it shows

One Sankey for the span on screen, in the metric chosen (kWh, £, CO₂):

```
            ┌─ 5–7.5p ──┐──────────── EV
  Grid ─────┼─ 7.5–10p ─┤──────────── Heat pump
  import    ├─ 25–27.5p ┤──────────── House (Direct import) ───── Export
            └─ …        ┘──────────── Battery (its grid charge)
  Standing charge (£ only) ─────────────────────────────────────── (its own node)
```

- **Left, the grid:** import over the span — the whole meter.
- **Middle, the rate band:** where that import fell by the price it was charged.
- **Right, where it went:** the house (Direct import) and each device's grid share.
- **Out of the house, export:** kWh sent to the grid, with its credit in £.
- **£ only, the standing charge:** a flow from the grid to a node of its own. It belongs to no band
  and no device.

The figures above the chart are the span's totals: import, export, net. They are the Devices view's
own, so the two views agree.

## 2. What EMT can and cannot draw

EMT meters the grid side only.

| Can | From |
|---|---|
| Import by rate band, per device and the house | the rate brackets (`usage_view._rate_brackets`, BL-93): per series, per 2.5p bracket, kWh and £ |
| The car on an account with no EV device | Octopus's split, the `block_segments` slices attributed `ev` — **not yet in the brackets** (§5) |
| Export and its credit | the brackets' `export` series |
| Standing charge | the day rows' `standing` |
| Carbon per device | the day rows (`carbon_g` per meter) — **not yet by bracket** (§5) |

| Cannot | Why |
|---|---|
| Solar or battery → home | no generation or discharge is metered; a battery's sensor is its grid import |
| Where export came from | the same |

So the house node's inflow is its grid import, and export leaves it as a flow of its own. Export can
be larger than the house's import on a sunny month. The plugin sizes a node by the larger of its in
and out, and the hover says so (§4).

## 3. The rate band

**The Rate brackets view's 2.5p brackets of the inc-VAT rate** (`RATE_BRACKET_P`). It is one rule for
every tariff, and the same colours and order as that view (`barBrColor`, cheapest first):

- on Intelligent, the off-peak and peak rates, and the car's cap;
- on a flat tariff, one band;
- on Agile, the spread of its prices.

The stored band label (`off_peak` / `peak`) is set only on Intelligent tariffs. BL-93 rejected it for
that reason (rate brackets design §2), and the same reason holds here.

Each bracket is labelled by its range, "7.5–10p". A negative price, an Agile plunge, has brackets
below 0p and earns a negative £.

## 4. The chart

- **The plugin:** `chartjs-chart-sankey`, by Chart.js's own maintainer, loaded beside Chart.js 4.4.1 as
  the heatmap loads `chartjs-chart-matrix`. It needs a pinned exact version; the latest is 0.19.1
  (12 Sep 2026), against `chart.js >= 3.3.0`. Which version is checked in the local preview before
  PR 2 (§6).
- **Colours:**
  - flows into a band take the band's bracket colour;
  - flows out of a band take the device's chosen bar colour, as the Devices view's;
  - the house is the main meter's colour;
  - the API EV takes its colour from the EV tab (BL-97);
  - export takes the main meter's export colour.
- **The period:** as the other views' (HH, Daily, Monthly, Yearly), and the same navigator. Flow
  totals the span those bars show: HH a day, Daily a month, Monthly a year, Yearly all years. The
  billing/calendar choice and the standing charge box apply as they do there.
- **Hover:**
  - a flow: "7.5–10p → EV: 120 kWh, £10.80" — kWh × the band's average rate, so the two figures
    reconcile;
  - a node: its total.
  - The house adds: "Export is energy EMT did not meter coming in: solar or a battery."
- **Small bands:** on Agile a month can touch a dozen brackets. A bracket under 2% of the span's
  import, in the metric shown, is folded into one band, **Other**. Other is grey, last in the column,
  and its hover lists the brackets it holds with their figures, so nothing is hidden. The threshold is
  a constant (`FLOW_OTHER_SHARE = 0.02`). A span whose brackets all clear it shows no Other.
- **The PDF:** the canvas, as the other Usage Stats charts are taken.
- **The table below:** a row a flow (from, to, kWh, £, kg), with Copy to clipboard as the other views.

## 5. The data it needs

Two gaps in the rate brackets, both server-side, both testable against a store:

1. **The car of an account with no EV device.** `_rate_brackets` puts the main's slices in `meter`
   and gives the house what the devices leave. Octopus's EV slices (`block_segments.attribution =
   'ev'`) therefore land in the house. A series of its own, `ev_dispatch`, is the slices attributed
   `ev`; the house is the rest. The Devices view already splits it this way, so this is the same split
   at the brackets' grain.
2. **Carbon by bracket.** Brackets carry kWh and £. A slice's carbon is its half-hour's import carbon
   (`carbon_g`, or `carbon_g_imp` on the main) pro rata by kWh; a device's is its own stored carbon.
   A third value, `[kWh, £, g]`, is added to each bracket. The Rate brackets view reads the first two
   and is unchanged.

**Retired devices** need nothing new. Retiring one sets `retired_at` on its row (`retire_meter`), and
the meter stays in the active config period. The brackets read that period's sub-meters, so they
already include it, and Flow names it as the Devices view does, from the same config. A test with a
device retired mid-span keeps it that way.

**Reconciliation (the tests):** over any span,
- Σ a device's flows = the Devices view's figure for it (kWh, £, CO₂);
- Σ every band = the meter's import;
- export = the Devices view's export.

These are asserted against a fixture with an Intelligent day, an Agile day, a device and an API EV.

## 6. PRs

1. **The brackets:** the `ev_dispatch` series and carbon by bracket (§5), with the reconciliation
   tests, including a device retired mid-span. No page change; the query budgets are re-measured (the soak surface for brackets).
2. **Flow, kWh and £:** the view, the plugin (version checked first), hover, the table, the totals,
   in the local preview on the demo household.
3. **CO₂, the PDF, the help page** (Usage Stats → Flow), and the reference page.

## 7. Open

Nothing. Decided on 4 Nov 2026:
- **Small bands on Agile:** grouped into Other (§4).
- **The house's export caveat:** a note in the house node's hover (§4), not a line under the chart.
- **Retired devices** in the span: shown under their name, as the Devices view shows them.
