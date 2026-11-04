# Flow — export by rate band, and negative prices in £ (design)

> **History — a design record.** What was decided, when and why, as it stood then; parts describe code
> that has since changed. How the feature works today: its help page in the app (Usage Stats → Flow),
> and the code. PR numbers (`#NNN`) are in this repository, `RGx01/EMT`.

*Status: built, 4 Nov 2026, in the PR that carries this record. An addition to the Flow design
([EMT-v5_flow_design.md](EMT-v5_flow_design.md), #43–#46), which it changes in two places: export was
one flow out of the house (§2 and §4 there), and a negative £ was told in the head, not drawn (§3).*

*Decided, 4 Nov 2026, with the user ("can they handle plunge pricing? I think we need to rate band the
export too"):*
- *export by its own rate bands;*
- *a plunge's credit drawn in £, from whoever used it;*
- *a negative export price drawn in £ as a cost, Paid to export.*

## 1. What Flow did

- **kWh and CO₂:** a plunge's brackets (below 0p) were bands like any other, blue, and their kWh flowed
  to the house and the devices. That stays.
- **£:** a plunge earns a negative £, and a Sankey cannot draw a negative width. Flow left those flows
  out and said in the head that the credit "is in the net, not drawn". The totals were right; the
  picture did not show the plunge at all.
- **Export:** one flow, the house → Export, though the brackets already carried the export by its own
  rate (`_rate_brackets`' `export` series, at `exp_rate`), as Rate brackets shows it.

## 2. Export by rate band

The house's export leaves through its own bands, a fourth column:

```
                                              ┌── Export 7.5–10p
  … ─── Direct (house) ───────────────────────┼── Export 12.5–15p
                                              └── Other export
```

- **The bands:** Rate brackets' 2.5p brackets of the export rate, labelled "Export 12.5–15p", in Rate
  brackets' export colours (blue to purple, the best-paid the deepest; a negative export price red) —
  `barBrColor`'s side now named by Flow (`'import'`, `'export'`), not read from Rate brackets' series.
- **Other export:** a band under `FLOW_OTHER_SHARE` (2%) of the span's export, in the metric shown, as
  the import's Other; its hover lists what it holds.
- **Hover:** the flow's kWh, £ and p/kWh (the export rate), and the caveat that export is energy EMT
  did not meter coming in.
- **The head:** Export is the sum of the bands, as before.

## 3. A negative price in £

Money that comes back to you sits on the right, beside the export bands:

- **Plunge credit.** An import bracket with a negative £ adds a flow from the series that used it (the
  house, a device, the car) to **Plunge credit**, sized at the credit. It is the band's kWh, grams and
  £ turned positive, marked `credit`.
- **Paid to export.** An export bracket with a negative £ (paying to export) adds a flow from the house
  to **Paid to export**, red, sized at the cost — so no £ flow is hidden.
- **Drawn in £ only.** A credit flow is drawn when the metric is £; in kWh and CO₂ the plunge band and
  the negative export band are drawn as they were, and a credit flow would count their kWh twice.
- **The table** leaves credit flows out: the band's own rows already carry their £, negative.
- **The head:** "Import is net of £X earned at negative prices: Plunge credit", and "Export is net of
  £X paid to export at negative prices".

A node is sized by the larger of its in and out (the plugin's `size: 'max'`), so the house in £ shows
its import cost or what leaves it, whichever is larger, as it did with export.

## 4. Checked

Tests (`tests/test_flow_view.py`): the export bands and their Other, export's colours, a plunge's
credit from each series that used it, paying to export, credit flows drawn in £ only and left out of the
table, the head; a GUARD that a span with no negative price has no credit node.

The local preview, on a copy of the demo household with a plunge (-3p, 12 Oct 00:00–16:00 UTC) and an
Agile-Outgoing-like export rate (8p midday, 24p 16:00–19:00, 14p otherwise, -1p in the plunge) put in,
a billing month: Plunge credit £0.737, the plunge's 24.58 kWh × 3p; the export bands' £1.743 less
Paid to export's £0.016, the head's £1.73; in kWh the export bands' 20.075 kWh, the head's.
