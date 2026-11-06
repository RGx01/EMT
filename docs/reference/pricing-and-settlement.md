# Pricing and settlement

*Reference — describes the code on `dev`. When this page and the code disagree, the code is right and
this page is fixed.*

## One rule prices every half-hour

`settle.settle(Facts)` prices a main-meter half-hour from its facts and reads no stored rate or cost.
`engine.settle_half_hour` gathers the facts (`settle_facts`) and writes only the parts that differ:
every block write, the carve, the reconcile, a bill applied and the settle sweep all go through it. A
half-hour you corrected is skipped.

- **The kWh** is Octopus's settled figure once it has one (`imp_kwh_api`), else the live reading; an
  imported half-hour's own.
- **The tariff kind** comes from the half-hour's own agreement (`tariff_kind_at`): *intelligent*,
  *published* or *none*. Published is one leg at the schedule's rate; none is `no_agreement`, priced at
  the configured rate. Rates come from the stitched schedule of each agreement. The current tariff's own schedule
  (an account with one agreement, the standing charge, export) is fetched from a day before the earlier
  of the channel's first agreement and the first half-hour stored (`_rate_history_from`) — not from the
  product's launch, which on Agile is years of half-hours.
- **Fetching an agreement's rates** (`kraken_rates.agreement_schedules`): a closed agreement is cached
  for the process once fetched, and so is one Octopus **refuses** with a 4xx (not 401/403 or 429) — an
  empty schedule, not asked again until a restart. The running agreement
  is fetched at every refresh (v4 #513). An API import builds its segments once a job
  (`import_api_history(segs=…)`), not on every retry of a chunk. A chunk Octopus does not answer
  halves the days the job asks at a time — 60, 30, 15, then 7, one page of measurements — for the
  rest of the job (`_import_chunk_after`), so a slow account's import keeps each part that lands.
  Each channel keeps its own top from its first call (`api_import_top_<channel>` in kraken_state,
  cleared on a restart): until a chunk lands a channel has no checkpoint, and the oldest block of any
  source — the other channel's rows — must not become its ceiling.
- **Economy 7** (BL-102): a two-rate tariff (`E-2R-…`) has no standard rates — Octopus answers 400 —
  so its `day-unit-rates` and `night-unit-rates` are fetched and placed by Octopus's smart-meter rule,
  night **00:30–07:30 UTC** all year (01:30–08:30 in summer; `kraken_rates._E7_*`, the IOG synthesis
  with its window and zone passed). No API gives the times; every account EMT reads half-hourly from
  Octopus has a smart meter. After an API import `check_e7_window` compares the rule with what Octopus
  charged and logs the result; it changes nothing.
- **Dispatches** are asked for every 5 minutes only while the import tariff is Intelligent
  (`_tick_dispatch_capture`); on another tariff the account has no device to ask about.
- **Cost** = Σ legs kWh × rate; `imp_rate` is the blend. Ex-VAT is from the VAT calendar
  (`vat_calendar.py`). Devices are priced on their grid shares at the legs their kWh sits in.

## The band, on an Intelligent tariff

In order — the first that answers wins:

1. **Octopus's settled band** (`settled_band`): on IOG-SMB the carrying bucket's unit rate against the
   day's two rates; elsewhere the bill's label. An `IMPORTED` bill (an import's own cost) is banded by
   its cost ÷ kWh.
2. **v4's sensor rate** for history v4 recorded before it had Octopus's API (`_sensor_band`).
3. **EMT's prediction** (`predicted_band`): a dispatch slot moves the half-hour off-peak above 0.1 kWh;
   then, on a live half-hour, the dispatch lifecycle decides — a completed dispatch with no plan or
   start while EMT was recording is a bump, and peak (under 0.4 kWh completed, it did not run).

The bill wins over the prediction; where they disagree the detail panel notes it (`band_note`).

## The 6-hour cap (IOG-SMB)

- Applies to a half-hour whose **own agreement** is IOG-SMB with the EV-device rates loaded
  (`capped_at`).
- Counts **actual completed charging** per noon→noon cap-day (`iog_cap.cap_usage`): a slot's charge time
  is its energy ÷ the charge power, taken as twice the cap-day's fullest half-hour, so a part-filled
  slot counts the part it charged and the 6 hours can run out mid-slot.
- **Left out of the count** (`cap_excluded`): a bump or boost by its source; a charge whose EV bucket
  Octopus billed at peak; with no EV bucket billed, a lifecycle-peak charge outside the guaranteed
  window. A charge past the 6 hours not yet billed still counts.
- On a capped half-hour with EV, the cap's four rates price the legs (EV off-peak within the allowance,
  EV peak past it, the house's).

## "Settled" is two events

- **The kWh settles** when Octopus's smart-meter figure arrives (`imp_kwh_api`), a day or two later.
- **The bill settles** when Octopus's cost for the half-hour is applied (`apply_measured_to_block`,
  `rate_source='settled'`), which needs the settled kWh first. The cost is then kWh × Octopus's band
  rate; Octopus's cost decides the band, and a disagreement shows red.
- On a **published** tariff the settled kWh × the agreement's published rate is the bill, at any age
  (`settlement_state._published`).

## Asking Octopus for bills

- **One asker**, `run_measured_history_drain`, fed by `_ask_queue`: settled, not corrected,
  Intelligent, older than 6 hours, within Octopus's two years. Kinds: a bill, a split (an IOG-SMB
  breakdown, where the cached bill is cost-only and a completed dispatch drew — or the half-hour is an
  API import's, which has no dispatch to say), an imported half-hour's own (asked after everything else).
- Newest first, a page at a time (3 local days of costs, or 48 breakdown half-hours); it stops when
  under 25% of the API allowance is left. Runs from the 5-minute dispatch tick and after an import.
- **Unanswered**: hourly for 2 days, then daily; given up 30 days after the first unanswered ask
  (`abandoned`, red hatched).
- **Hold-back**: a breakdown with no EV where EMT's split has the car is held and asked again on the
  same backoff; after 28 days EMT's split is kept.

## The Import bill's states (Data Management's heatmap)

From `settlement_state.classify`, drawn by `heatmap_module.html`:

| State | Colour | Means |
|---|---|---|
| `applied` | green | final: Octopus's cost, or settled kWh × a published rate |
| `applied_emt_split` | teal | Octopus's cost, EMT's house/EV split |
| `estimate_dcc` | light amber | settled kWh × rate, Octopus's cost not in yet |
| `estimate_cad` | amber | kWh not settled yet |
| `split_pending` | light amber | an API-imported IOG-SMB half-hour: Octopus's cost is cached, waiting for its House/EV breakdown to settle the band and apply it |
| `overdue`, `differs`, `flagged` | red | not final 9 days after the half-hour (7 after its kWh); Octopus's figures disagreeing; or a flag (the bill's kWh, its VAT, a missing split, devices costing more than the meter) |
| `abandoned` | red, hatched yellow | asked 30 days, no cost — kWh × rate |
| `corrected` | blue | your correction |
| `imported`, `no_agreement`, `none` | grey | imported history (or past Octopus's two years); no Octopus agreement; nothing imported |
| missing | black, hatched | no block for the half-hour, or one with no `imp_kwh` (`block_store._HAS_IMPORT`) |

**Export** has no bill: kWh × the export rate is final once the export kWh settles — `applied`,
`waiting`, `overdue`, `no_data` (no export figure, hatched), `no_agreement`, `none`.

## The re-fetch

User-driven, on a selection (`run_refetch_preview`, then apply): fetches the span's kWh and bills into
memory (at most 31 days a time), lists only what differs, and on apply — after a `pre-refetch` backup —
writes those half-hours through the asker's own path (`_store_bill` → `apply_measured_to_block`). A
half-hour Octopus does not answer is left as it was: a re-fetch never clears a cost. It asks half-hours
the asker has given up on, and on an Intelligent tariff replaces a correction.

## A CSV or PDF bill's cost

A CSV import (PDF bills build CSVs) fills missing half-hours, and leaves held ones — except that a
settled Intelligent half-hour with no bill and no correction, whose CSV kWh is its settled kWh, takes
the CSV's cost as an `IMPORTED` bill, its block untouched; it is then banded from it and reads grey.
