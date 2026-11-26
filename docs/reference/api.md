# The pages and the API

*Reference — describes the code on `dev` (`web/server.py`). When this page and the code disagree, the
code is right and this page is fixed.*

## Pages are shells

Each page (`/live-power` — the Overview — `/charts`, `/insights`, `/data-management`,
`/historical-import`, `/settings`, `/billing-history`, `/logs`, `/help/…`, `/welcome`) is a Jinja
template that renders no figures: it fetches JSON from the routes below and draws it in the browser (Chart.js). Nothing is pre-rendered.
There are no dashboard-card pages: v4's `/lovelace/billing` and `/lovelace/heatmap` were removed on
20 Nov 2026, so the direct port serves the signed-in app and nothing else.

Charts and its heatmap poll a fingerprint (`/api/energy?fields=fingerprint`) and
refetch only what changed. The poll runs every minute while the page is in view, and again 90 s after each
half-hour boundary. A tab notes the fingerprint it read just before loading, so a half-hour that lands while it
loads still counts as a change at the next poll. Live Power's cost cards check it every minute too, and fetch
`/api/periods?kind=live` again only when it has moved.

## The Overview

Rows of cards ([design record](../history/design/EMT-v5_overview_rows_design.md)): Power (Live Power and the
device cards), Grid and carbon, Smart Charging, Agile import, Agile export, Costs — every row orderable, and the
cards along each (22 Nov 2026, the user, after the record: the costs were fixed last and only the Power row's
cards moved). The user's choices are settings beside `overview_hidden`: `overview_rows` (row order),
`overview_card_order` (every row's cards, one list), `overview_costs_compact` (cost totals only) and
`overview_power_compact` (the power cards as gauges only) — each switched from a button on every card of its
row, the same icon on both — and
`overview_battery_view` ({card key: `soc` | `energy`}). Unlocked (the padlock), a row is dragged by its strip and a
card along its row by its handle, with the pointer, so a finger works as a mouse does. The Power row is spread evenly: as few
lines as fit at 220 px, then as even as they can be. In the Grid and carbon row a hidden card's neighbours fill
its place.

`/api/overview/days` also carries `ev_cap` with a car on a capped agreement — today's noon-to-noon 6 hours,
used, planned and the plan's windows ([pricing](pricing-and-settlement.md#the-6-hour-cap-iog-smb)) — and gives
each power card today against yesterday, as cumulative [minute, value] points since
each day's local midnight (`overview_days.py`):

- **Live Power**: the main meter's half-hours, plus the half-hour in progress from its register reads.
- **A device**: its power sensor's samples (`sub_meter_history`, kept 50 hours), trapezoid-summed in SQLite into
  5-minute steps (`BlockStore.get_sample_energy_steps`). A gap over 5 minutes adds nothing. The route keeps a
  day's finished steps and sums only the samples from the first unfinished one (`_ov_day_steps`; a restore
  starts again): about 2 ms a minute where the per-sample sum in Python took 0.44 s for three devices on a Mac
  (22 Nov 2026). Positive is in; negative is out, shown for a battery or a V2X car. The charts draw
  export and out below zero. This is the
  device's own measurement, not its billed grid share. A device without a power sensor: its half-hours, in only.
- **A battery's SoC**, the last reading in each 5 minutes; and **a heat pump's outdoor temperature** the same
  way (`outdoor_temp_sensor` on the heat pump, sampled with its power into `sub_meter_history.temp_c`, °C — °F
  and K converted), drawn on the chart's second axis.

## Who may ask

Through Home Assistant's sidebar (ingress, a request from the Supervisor) every page and route is open:
Home Assistant has signed the user in. On the **direct port** (8098) every request needs EMT's session
cookie, `emt_session_<10 hex>` (`web/signin.py`, `cookie_name()`: a hash of the install's id, so two EMTs at
one host — a browser keeps cookies by host, not port or path — keep a cookie each; until 20 Nov 2026 it was
`emt_session` for every install, and signing in to one signed the other out), apart from `/signin`, `/signin/start`, `/signin/done`,
`/signin/check` and `/static/…`:

- without it, a page redirects to `/signin?next=…` and an API, or any write, answers 401;
- with it, a write (anything but GET, HEAD and OPTIONS) must carry an `Origin` (else `Referer`) on EMT's
  own host, or it answers 403.

`/signin` sends the browser to Home Assistant's sign-in (OAuth, PKCE). Where to send it is
`signin.ha_addresses()`: Home Assistant's own local address (its core config's `internal_url`; standalone,
`HA_PUBLIC_URL` or `HA_URL`'s port on this host), then plain HTTP and HTTPS at this host's port 8123. The page
tries each from the browser and takes the first that answers; `/signin/start?ha=N` goes to the list's
place N, and to the first for anything else — never to an address the request names. (A local address set
to `https://` while Home Assistant serves plain HTTP left the browser unable to connect, 20 Nov 2026.) `/signin/done` finishes it with Home
Assistant, admits an administrator only, revokes Home Assistant's tokens and sets the cookie for 30 days;
`/signin/check` says so when the browser did not keep it; `POST /signin/out` signs out. The cookie is signed
with `session_secret`, a file beside `blocks.db`.

`.build/serve_local.py` runs with no sign-in unless
`EMT_PREVIEW_SIGNIN=1`. Standalone, `EMT_DIRECT_LOGIN=off` turns all of this off,
and `HA_PUBLIC_URL` says where the browser reaches Home Assistant. The design:
[EMT-v5_direct_port_login_design.md](../history/design/EMT-v5_direct_port_login_design.md).

## The main read: `/api/energy`

- `resolution` = `period` | `day` | `block`.
- `include` = `usage`, `carbon`, `billing`, `stats`, `settle` (each half-hour's settlement state), `gaps`.
- `fields=brackets` (at `day` or `block`): grid import by 2.5p rate bracket (`usage_view._rate_brackets`),
  `{series: {n: [kWh, £, g]}}` per day or half-hour, for `meter`, the house (`electricity_main`), each
  device, `export`, and on an account with no EV meter the car (`ev_series`, `ev_dispatch`) by the Devices
  view's split. `g` is grams of CO₂ (since 4 Nov 2026, for Flow).
- Capped: 31 days at `block`, 366 at `period` (`_ENERGY_MAX_DAYS`).
- `/api/periods` lists the periods to choose from (`kind` = billing, month, quarter, year, live).
  With `include=net_cost`, each period's total is each local day's net to the penny, summed — except
  `kind=live`'s *This Bill*, which is the Billing card's Total Bill (`bill_card.bill_total`): the
  statement's rounding, import and standing rounded once less the export credit. The two can differ by
  pennies; the Overview card matches the Billing tab (22 Nov 2026). On Agile the Billing tab's cards take
  each half-hour at the statement's kWh (`statement_kwh`), so for every kind but `live` each day's net takes
  what that moves it by (`agile_bill.statement_day_adjustments`), and a label stays within pennies of its
  card; Today and This Year stay what was used. This Bill carries `statement_rounded` — its period has
  Agile half-hours — for the card's **?**: its total is the statement's, its rows the meter's (26 Nov 2026).

## Route families

| Family | For |
|---|---|
| `/api/power*`, `/api/carbon*`, `/api/charge-sessions`, `/api/agile-prices`, `/api/overview/days` | the Overview: live power, carbon, the smart-charging card, Agile in your region, the cards' today against yesterday |
| `/api/blocks/settlement`, `/api/data-health`, `/api/settlement/*` (re-fetch, the asker's state, parity) | Data Management: a half-hour's details, the health strip, the re-fetch |
| `/api/gaps/*`, `/api/backfill/*`, `/api/historical/*` | filling gaps; importing from Octopus, a CSV or PDF bills |
| `/api/blocks/delete*` | deletes |
| `/api/backup/*`, `/api/restore/*` | backups; the two-step restore |
| `/api/config*`, `/api/settings`, `/api/kraken-config` | the configuration; the Octopus connection |
| `/api/tariff-agreements*` | the account's agreements and their prices (display only); accepting a changed answer |
| `/api/home-eras`, `/api/battery-sensor/*`, `/api/update-check` | home eras; the battery-sensor flag; whether a newer release is out |
| `/api/customisations`, `/api/customisations/import` | the eras and chart colours as a file, from this database or a kept backup; a file applied (a preview without `apply`) |

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
