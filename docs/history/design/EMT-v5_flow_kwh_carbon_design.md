# Flow in kWh and CO₂, without the price bands (design)

> **History — a design record.** What was decided, when and why, as it stood then; parts describe code
> that has since changed. How it works today: the help (Charts → Flow) and the code
> (`barFlowPlain`, `barFlowData` in `web/templates/charts.html`). PR numbers (`#NNN`) are in this
> repository, `RGx01/EMT`.

*Status: built, 5 Nov 2026, in the PR that carries this record. It changes the Flow design
([EMT-v5_flow_design.md](EMT-v5_flow_design.md) §1, §3), which drew the rate band in every measure, and
the export bands design ([EMT-v5_flow_export_bands_design.md](EMT-v5_flow_export_bands_design.md) §1),
which kept a plunge's band in kWh and CO₂ "like any other".*

*Decided, 5 Nov 2026, with the user: "in kWh (and carbon) grouping should we really care about cost
rate bands or just show the flow of kWh (and Carbon). It could be useful to also aggregate the cost and
carbon in the hovers for those views too?" — and export left out of CO₂.*

## 1. Why

The band column is where the import fell by the price it was charged: a £ idea. In kWh and CO₂ it split
every flow by a price that says nothing about energy or grams, and Other — chosen in the measure shown
— grouped different brackets in each measure, so the three pictures did not share a shape.

## 2. What changed

| | £ | kWh | CO₂ |
|---|---|---|---|
| Grid → | band → the house and each device | the house and each device | the house and each device |
| Export | out of the house by its own bands | one flow, house → Export | not drawn |
| Standing charge, Plunge credit, Paid to export | drawn | — | — |

- **kWh and CO₂** (`barFlowPlain`): a flow per destination, the brackets summed; the house's export as
  one flow, in export's colour.
- **CO₂'s export** is kept in the data, marked `hide`, and not drawn: its grams are grid carbon the
  export displaced (`_rate_brackets` puts the export's kWh at the half-hour's intensity), not carbon
  through the house, and drawn it was sized like an import. The head says *Displaced by export*, and the
  table keeps the row.
- **The hover**, every measure: the flow's kWh, £ and kg summed, its average p/kWh, and now its g/kWh.
- **£** is as it was.

## 3. Checked

`tests/test_flow_view.py`: the band tests now run in £; kWh's flows summed per destination with no band,
standing charge or credit flow; CO₂'s export kept and hidden; a house with export but no import keeps
its node. In the local preview (the demo household, its latest era): kWh drew the grid to the house and
three devices and the house to Export; £ kept its bands; CO₂ — with device carbon put on the demo's
brackets in the browser, since the demo's devices carry none — drew no export, the head "Displaced by
export 288 kg", the hover "Grid → EV: 3409 kWh · £285.67 · 682 kg · 8.4p/kWh · 200 g/kWh".

Seen while checking: the demo's device rows carry no carbon, so its house holds the whole meter's grams
(975 g/kWh). Real installs' rebuilt device rows carry theirs (a support file: 1,584 of 1,584).
