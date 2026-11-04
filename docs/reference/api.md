# The pages and the API

*Reference — describes the code on `dev` (`web/server.py`). When this page and the code disagree, the
code is right and this page is fixed.*

## Pages are shells

Each page (`/live-power` — the Overview — `/charts`, `/insights`, `/data-management`,
`/historical-import`, `/settings`, `/billing-history`, `/logs`, `/help/…`, `/welcome`) is a Jinja
template that renders no figures: it fetches JSON from the routes below and draws it in the browser (Chart.js). Nothing is pre-rendered. The Lovelace cards (`/lovelace/billing`,
`/lovelace/heatmap`) are pages of the same kind.

Charts, its heatmap and the Lovelace heatmap poll a fingerprint (`/api/energy?fields=fingerprint`) and
refetch only what changed.

## The main read: `/api/energy`

- `resolution` = `period` | `day` | `block`.
- `include` = `usage`, `carbon`, `billing`, `stats`, `settle` (each half-hour's settlement state), `gaps`.
- `fields=brackets` (at `day` or `block`): grid import by 2.5p rate bracket (`usage_view._rate_brackets`),
  `{series: {n: [kWh, £, g]}}` per day or half-hour, for `meter`, the house (`electricity_main`), each
  device, `export`, and on an account with no devices the car (`ev_series`, `ev_dispatch`) by the Devices
  view's split. `g` is grams of CO₂ (since 4 Nov 2026, for Flow).
- Capped: 31 days at `block`, 366 at `period` (`_ENERGY_MAX_DAYS`).
- `/api/periods` lists the periods to choose from (`kind` = billing, month, quarter, year, live).

## Route families

| Family | For |
|---|---|
| `/api/power*`, `/api/carbon*`, `/api/charge-sessions`, `/api/agile-prices` | the Overview: live power, carbon, the smart-charging card, Agile in your region |
| `/api/blocks/settlement`, `/api/data-health`, `/api/settlement/*` (re-fetch, the asker's state, parity) | Data Management: a half-hour's details, the health strip, the re-fetch |
| `/api/gaps/*`, `/api/backfill/*`, `/api/historical/*` | filling gaps; importing from Octopus, a CSV or PDF bills |
| `/api/blocks/delete*` | deletes |
| `/api/backup/*`, `/api/restore/*` | backups; the two-step restore |
| `/api/config*`, `/api/settings`, `/api/kraken-config` | the configuration; the Octopus connection |
| `/api/tariff-agreements*` | the account's agreements and their prices (display only); accepting a changed answer |
| `/api/home-eras`, `/api/battery-sensor/*`, `/api/update-check` | home eras; the battery-sensor flag; whether a newer release is out |

The retired sub-pages (`/bill-rounding`, `/delete-blocks`, `/corrections`, `/historical-probe`)
redirect to where their tools now are.

## Tariff agreements and prices

- `tariff_agreements` is Octopus's answer for the account, replaced whole on each discovery (dropped
  rows kept as superseded). Read before discovery, so a restart with Octopus down still knows them.
- A changed answer that drops or moves an agreement that started and ended over 30 days ago is not
  taken: it is kept for the user to accept. A taken change re-settles the spans it moved.
- Every reader that needs "which agreement covers this half-hour" asks the agreements, and a half-hour
  outside them is `no_agreement`.
- `tariff_prices` holds each agreement's price periods ex-VAT, for the Usage Stats tariff overlay's
  hover only — no pricing path reads it. Closed agreements' prices are fetched a few at a time, paced.
