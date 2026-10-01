# Workstream C — one API for every surface

*Retiring pre-generated HTML and converging every surface on a single data API. The programme map
([EMT-v5_programme.md](EMT-v5_programme.md)) frames why this sits where it does; this document is
the design.*

**Status:** draft, 17 Sep 2026. Depends on workstream A (storage and access model) — closed
30 Sep 2026. **Review against the code, 30 Sep 2026, re-measured 1 Oct 2026: §0 below.**

## 0. Where the code stands — reviewed 30 Sep 2026, re-measured 1 Oct 2026

*Each §6 acceptance item measured against `dev` after workstream A closed; rows 4, 5, 6 and 8
re-measured 1 Oct 2026, after the pre-rendered pipeline was deleted.*

| # | acceptance (§6) | state | evidence |
|---|---|---|---|
| 1 | no pre-generated chart artefacts; `CHART_DIR` gone | **met, 30 Sep 2026** — both chart tabs are drawn in the browser from `/api/energy`: the heatmap (`heatmap_module.html`) and the Billing tab (`billing_module.html`, served by `billing_view.py` with the card and side panel as rows from `bill_card.py`). Before the page was deleted, `.build/billing_payload_parity.py` and `.build/billing_client_parity.js` held the data and its formatting identical to it on a real database (1,212 days, 98 periods), and the page itself was byte-identical across the split. Then `generate_daily_import_export_charts`, `build_day_chart_html`, `render_billing_summary`, the engine's regeneration path, `CHART_DIR`, `/api/charts/daily`, `/api/charts/regenerate` and `/charts/daily_usage.html` were deleted; `/lovelace/billing` serves the module; stale files are removed at setup | `energy_charts.py` 3,430 → 1,290 lines (the data half — `day_chart_data`, `billing_context`, `period_summary` — and the EV helpers remain); `tests/test_no_prerender.py` pins that nothing renders, serves or writes a chart file |
| 2 | a cost-neutral re-price invalidates | **met** | `test_fingerprint::test_a_cost_neutral_reprice_still_moves_it` |
| 3 | a change to today leaves history alone | **met** | per-day fingerprints (`PerDayNotGlobal`); the epoch now also moves across a restore / zip import (30 Sep) |
| 4 | a write does no rendering | **met, 30 Sep 2026** — went with #1: the 25 render triggers (10 engine incl. `finalise_block`, 15 server) were deleted with the renderer, and nothing outside the tests names a render path | `tests/test_no_prerender.py` (`_GONE`: `generate_charts`, `_schedule_chart_regen`, `_regen_charts_safely`, `render_recently_active`, … none defined, none called) |
| 5 | every surface on the one endpoint | **partly met** — **the whole Charts page reads only `/api/energy`** (1 Oct 2026, C step 1): Billing and the heatmap, and now Spiral and Usage Stats (`include=stats`, from `usage_view.py`), with the change poll on `fields=fingerprint`; `/api/charts/blocks-summary`, `blocks-day` and `data-version` are deleted. **Insights reads only `/api/periods` and `/api/energy`** (1 Oct 2026, C step 2: bounds and the trend strip from `/api/periods?include=carbon`, each view from `include=carbon` or `include=usage`); the eight routes it and nothing else called — `/api/insights/{periods,data-bounds}`, `/api/{insights,usage}/{calendar-month,calendar-year,billing-period}` — are deleted (nothing outside the add-on called them, confirmed by the user). Still on its own path: **Live Power, Corrections, Meter config** on `/api/billing`, which computes Today / This Bill / This Year in its own SQL — a private aggregation path | templates' requests in a browser and `grep` (incl. URLs built by concatenation), 1 Oct 2026; routes in `web/server.py` |
| 6 | no tariff context with a default, AST-enforced | **ratchet in place, not yet met** — **23** defaulted tariff-context parameters, down from 35 when the ratchet was written (30 Sep); the chart builders' `cap_from=None` / `import_schedule=None` went with the renderer. Remaining include `calculate_billing_summary_for_period(tz_name=None)`, `bill_slots_from_blocks(vat_rate=0.05)`, `reprice_block(vat=0.05)`, `_aggregate_usage(tz_name='UTC')`. New code follows the rule: 1 Oct's rate-schedule builders take a required `vat` (`kraken_rates.NO_VAT` for export) | `.build/check_tariff_defaults.py`, `tests/test_tariff_defaults.py` (the set may shrink, never grow) |
| 7 | billing reconciles with v4 to the penny | **met, 30 Sep 2026** | `.build/reconcile_v4.py`: v4.5.16's own `compute_period_net` against v5 on a real database — **80 of 80** periods (40 billing, 40 months, three years) to the penny for the Billing card, the local-day client path and `/api/energy`'s period total. It first found the day rollups' standing charge summed 48× and the period total rounded once (≤3p in 49 of 80); both fixed. Property pinned in `tests/test_bill_reconciliation.py` |
| 8 | TTFB budgets on the Pi | **measured, not budgeted** — the sixth soak (30 Sep) gives per-request p50/worst for every Billing-tab request (a year's bill 7.07 s p50 on the Pi before the year-bill savings, ~4.5 s estimated after; not yet re-measured); no budget is asserted | §5 "Measured on the Pi" and "after C — the sixth soak" |

**Found by the review, fixed 30 Sep 2026:** the day rollups' standing charge was a SUM over
blocks that each carry the full daily charge — 48× — and the period payload rounded its bill
total once over the period instead of per day. `resolution=day` grouped by **UTC** date, and a UTC
day's total cannot be split back into local days — every BST day was wrong; it now groups by local
day with `tz_name` required. Restore and zip import did not bump the epoch, which lives in the
swapped file. `api_energy`'s docstring said day/block returned 501; they did not.

**§7.1, answered by the evidence:** the API serves rollups at exactly two levels — the **period**
(its own query) and the **local day** (one row per day per meter; a year is ~900 rows) — and the
client does everything between from days, applying the bill's per-day formula. Blocks are fetched
for a viewport, never summed for totals: a year of them is ~650 KB gzipped, ~15 MB raw.

**Order agreed 30 Sep 2026:** (1) the fixes above and a rule-2 ratchet; (2) a penny-reconciliation
test, v4 against `/api/energy`, on a real database; then (3) a minimal Charts page drawn in the
browser from `/api/energy`, and the pre-rendered pipeline deleted behind it — the start of step 6.

**Done by 1 Oct 2026:** all three — the fixes and the ratchet; 80 of 80 periods to the penny; the
Billing tab and the heatmap drawn from `/api/energy`, the renderer deleted (rows 1 and 4).

**Next, in order (1 Oct 2026):**

1. ~~**Spiral and Usage Stats onto `/api/energy`**~~ — **done 1 Oct 2026** (#33–#35 and this
   part 4): the rows moved to `usage_view.py` unchanged and are served as `include=stats`
   (`kind=all` for every day, `kind=day&date=` for one day's half-hours — keyed by instant, so a
   clock-change day has its 46 or 50); before deletion the old routes were byte-identical to it
   on real databases. Spiral now refreshes and remembers its view. The poll reads
   `/api/energy?fields=fingerprint` (the same global token, 52 ms; every day's fingerprint was
   163 ms on 138k blocks). The three `/api/charts/*` routes are gone.
2. ~~**Insights onto `/api/energy`**~~ — **done 1 Oct 2026** (#37, #38 and this part 3): the
   trend strip's per-period carbon is `/api/periods?include=carbon` (one rollup pass; the old route
   ran a query per period and cut BST periods an hour late on a UTC host), each view is
   `/api/energy?include=carbon|usage` — key-for-key the old payloads except Usage `net_cost`, now
   the bill's per-day rounding. Eight routes deleted.
3. **`/api/billing`'s Today / This Bill / This Year from `/api/energy`'s period payload**, not its
   own SQL — Live Power, Corrections and Meter config read it.
4. ~~**The period routes with no page caller**~~ — **done 1 Oct 2026**, deleted with Insights'
   (no external caller).
5. **Row 6 to zero**, ratchet by ratchet; **row 8's budgets** set from a re-measured soak.

---

## 1. What is actually wrong

Not the format. **A write triggers a wholesale recompute.**

In v4, 17 call sites schedule a chart regeneration (13 in `engine.py`, 4 in `web/server.py`), and
regeneration is wholesale: one `daily_usage.html` covering every day held. So one half-hour block
finalising re-renders two years of history. A burst of writes queues a burst of full re-renders
behind it, and a heavy read then contends with the writer on a shared connection.

The consequence is documented in `_schedule_chart_regen`'s own docstring:

> *"A synchronous render over 2+ years of blocks takes long enough to stall the HA WebSocket
> heartbeat, which drops the connection and re-runs startup: a reconnect→re-finalise→re-render
> storm."*

A heavy read starves the event loop, HA disconnects, reconnect re-runs startup, startup renders
again. That is a feedback loop, not a slow page.

**Five layers of compensation** have accumulated around that one coupling. Each is sound engineering
*given* the coupling; none should be inherited.

| # | Compensation | Provoked by |
|---|---|---|
| 1 | Renderer gets its own read-only `BlockStore` | the 3.1.3 cross-thread SQLite segfault |
| 2 | Render offloaded to a worker thread | render stalling the HA WebSocket heartbeat |
| 3 | In-progress guard + "re-run after it" / "change arrived mid-render" | renders piling on each other |
| 4 | Deferring reads to a calm period | readers contending with the writer |
| 5 | `_blocks_data_version` change token | telling a client its pre-generated file is stale |

Break the coupling and all five stop being needed. Layer 5 is the clearest tell: a token whose only
job is to detect staleness in a file that exists only because we pre-generated it.

---

## 2. The two rules

**Rule 1 — a write records that data changed. It does not trigger a recompute.**

Nothing renders. Nothing is scheduled. A surface asks for the range it is showing, when someone is
looking at it, and compares a **fingerprint** to know whether its copy is current. The mechanism is
specified in §2a.

**Rule 2 — a function whose output depends on tariff context takes that context as a required
argument, with no default.**

This is the lesson of the 4.5.13 `/api/charts/regenerate` episode. The scheduled renderer passed
`cap_from` and `import_schedule`; the on-demand endpoint silently did not, so `capped=False` and the
same function produced different charts depending on who called it. Two days went into diagnosing it
in the rendering layer, because the divergence was invisible at the call site.

`cap_from=None` meaning "render as if uncapped" is what made the omission silent. A single API
surface does **not** prevent this on its own — it reduces the number of callers, but one caller can
still omit context. The default is the defect.

---

## 2a. The fingerprint

### Two tempos, both slow

EMT does **not** surface the in-progress block growing. Every charting surface draws finalised data,
so there is no live tempo to serve and nothing needs a push or a second high-frequency endpoint:

| event | tempo | effect |
|---|---|---|
| block finalises | every 30 min | today's fingerprint moves — the user sees their half-hour arrive |
| settlement / reconcile | hours to ~2 days | a recent day's fingerprint moves |

A poll every 30–60 s over a viewport-sized range makes arrival feel immediate and costs essentially
nothing. No separate live endpoint and no push: there is no live tempo for them to serve.

> **Not the HA WebSocket.** That connection is EMT's *inbound data source*, carried unchanged —
> sensor subscriptions feed block derivation
> (`ha.subscribe_state(main_import_sensor, on_import_meter_update)`), HA recorder history feeds
> device attribution, and the overview page reads live state through it. The fingerprint poll is
> **browser ↔ EMT's own server** and never touches HA's socket. The constraint on the HA connection
> is that the event loop must not be starved — that is what delayed the heartbeat and caused the
> render storm — not how much traffic crosses it.

### Measured: routine change is recent and small

prod-dev, 07:00 → 20:28 on 17 Sep 2026 — 13½ hours of ordinary running:

```
NEW blocks      27    13.5h ÷ 30min = 27 — exactly the clock, nothing else
CHANGED blocks  20    all within 2026-09-16 → 2026-09-17
                      oldest changed block: 1.8 days old
```

**Nothing older than two days moved.** Reconcile scanned 267 candidates on all seven runs that day
and changed zero; `_measured_floor()` and the settle window bound it by construction. So a per-day
fingerprint is sufficient *and* cheap: today moves twice an hour, 2023 never moves.

### The mechanism

1. **Per-day fingerprint**, not one global token. v4's `_blocks_data_version` is global, so any
   change invalidates everything — a new half-hour would tell a user looking at 2023 to re-fetch four
   years. That is the wholesale problem migrated to the client.
2. **Derived from the rows in range, not declared by the writer.** Compute it from the data (a max
   change-counter over the blocks and segments in range). A job that forgets to declare its scope
   then cannot serve stale data; the fingerprint can only be slow, never wrong — and over a viewport
   it is not slow.
3. **It must cover everything displayed, not just totals.** A re-price can be **cost-neutral**: the
   4.5.13 band snap changed 24,128 rates with `cost_drift` of £0.0028. Totals identical, bands and
   rate lines different. A fingerprint over money alone would have shown that user a stale rate line
   indefinitely. This is precisely why v4's `_blocks_data_version` fingerprints segment *rates*
   (BL-32) — carry that property forward, do not rediscover it.
4. **An epoch counter for bulk operations.** One-time heals, imports and deletes legitimately touch
   all history and are rare and deliberate. Rather than expressing that through thousands of day
   fingerprints, bump a single epoch that invalidates all client state at once. These operations
   already sit behind the job coordinator (workstream B), which is the natural place to bump it.

Two levels, matching the two real shapes of change: per-day for the routine 99.9%, epoch for the
handful of operations that mean *throw everything away*.

---

## 3. What the API returns

One endpoint shape, serving every surface:

```
GET /api/energy?from=<iso>&to=<iso>&resolution=block|day|period&include=<facets>
```

The response is **priced blocks and their segments**, not chart geometry. Series, stacks, heatmap
cells and billing totals are all projections a client can make from the same payload; baking them
server-side is what produced two renderers that disagreed.

Every response carries:

- **`fingerprint`** — per-day for the range, plus the current `epoch` (§2a). Derived from the rows,
  covering every displayed field rather than totals alone. Replaces `_blocks_data_version`, and is a
  property of the data rather than of a pre-generated file.
- **`tariff_context`** — the agreement(s) covering the range, their bands and cap state. **Its band
  shape is a 2×2, not a pair.** A `(min, max)` rate range or a `day_rate_bounds` field assumes two
  bands; IOG-SMB has four rate buckets, so the context carries **four series keyed by
  *(attribution, band)*** — house·`day`, house·`night`, EV·`peak`, EV·`off_peak` — each valid over a
  stated interval. A two-band context cannot express an EV charging at peak while the house is on
  night, which is a real and billable state (§4).

  > **It is OPTIONAL, and the payload must be complete without it — corrected 25 Sep 2026.** This
  > bullet said it was there *"so the client never infers a band from a rate"*. That goal is already
  > met, and not by this field: **`block_segments.band` carries the cell of the 2×2 on every segment
  > row.** The client is told, per segment, which band it is in; it never has to infer.
  >
  > What `tariff_context` adds is narrower and still worth having — the PUBLISHED rate for each band
  > and where the band boundaries fall in time. That is what draws a rate line, and what lets a
  > surface show a band no data landed in.
  >
  > **But it has no persistent source.** Measured against a 4.5.14 prod database: no table holds a
  > tariff schedule. `meter_channels.tariff` is a code, not rates; `block_segments.inc_rate` is what
  > was CHARGED, not what was PUBLISHED. The schedules live only in `engine._kraken_rate_schedules`,
  > rebuilt from the supplier API at every startup — so they are absent before discovery completes,
  > absent while the API is unreachable, and absent **permanently** for a `not-listed` install, which
  > §5 of the import spec insists is a real supported supplier with real kWh history.
  >
  > Therefore: **serve it when the schedules are loaded, omit the key when they are not, and never
  > let a surface depend on it for correctness.** A client that needs "what band was this?" reads the
  > segment. A client drawing a published-rate line degrades to drawing only what was charged.
  >
  > *Deriving it from the data instead was considered and is NOT equivalent.* Across 2,201
  > *(day, attribution, band)* cells, 1,918 carry a single rate and **283 carry more than one**
  > (worst: three). Those are concentrated in 2023–24 — 266 of 283, against 17 in 2026 — the same
  > historical shape as the `standard` band population (§3a of the schema notes). So an observed
  > "the rate for house·night on this day" is a well-defined value in the modern era and ambiguous
  > across a tariff change. Observed rates and published rates are different facts and the payload
  > should not blur them.
- **`provenance` per block** — `rate_source`, whether a device breakdown is held, whether the block
  is settled. Surfaces differ today partly because each re-derives this; it should be stated once.

  > **Measured, 25 Sep 2026 — "settled" is THREE facts, not one, and a device row's provenance is
  > about the wrong thing.**
  >
  > The three signals in use, on a 4.5.14 prod database (137,099 blocks):
  >
  > | fact | signal | main-meter blocks |
  > |---|---|---|
  > | the kWh is authoritative | `source LIKE 'imported%' OR imp_kwh_api IS NOT NULL` (`_SETTLEABLE_SQL`) | 57,822 |
  > | the bill has been FETCHED | a `measured_cost` row exists | 633 |
  > | the bill has been APPLIED | `rate_source = 'measured'` | 603 |
  >
  > They are orthogonal and they disagree. 48,050 imported blocks are settleable with **no**
  > `imp_kwh_api` at all; **29 blocks hold a cached bill that has not been applied** — a real,
  > durable intermediate state. A surface answering "is this settled?" gets a different answer per
  > signal, which is the divergence this bullet exists to stop. The payload must therefore carry the
  > three separately rather than a single `settled` boolean, and name them: `kwh_authoritative`,
  > `bill_cached`, `bill_applied`.
  >
  > **And a sub-meter's `rate_source` describes how its row was written, not where its number came
  > from.** Of the device blocks sitting under a settled parent, **1,206 carry
  > `rate_source='schedule'`** — because `recost_sub_meters` copies the parent's rate without
  > touching the child's provenance (A §5c).
  >
  > **CORRECTED 25 Sep 2026 — the evidence first offered for this was vacuous.** It read: *"their
  > rate matches the settled parent EXACTLY — 1,206 of 1,206 — that is £42.14 of cost derived from
  > Octopus's bill and labelled schedule-derived."* The match distinguishes nothing. Devices track
  > the parent's rate **100% of the time whatever the parent's state**:
  >
  > | parent `rate_source` | device blocks | rate matches parent |
  > |---|---|---|
  > | schedule | 77,854 | 77,854 (100%) |
  > | measured | 1,206 | 1,206 (100%) |
  > | corrected | 68 | 68 (100%) |
  >
  > That is simply PASS 2 costing every sub-meter at its parent's rate — already documented — and on
  > IOG-SMB, where the era has exactly two distinct rates, a match is nearly free regardless.
  >
  > The right framing is the LIFECYCLE one: **the newest rows are predicted and settlement lags
  > about two days.** A device is predicted and then corrected in lockstep with its parent, and its
  > `rate_source` records the path the row took, not the authority behind the number it now holds.
  > So the claim that survives is the narrow one — a consumer reading a device's own column will
  > describe cost carried at a SETTLED parent's rate as schedule-derived — and the £42.14 figure is
  > withdrawn, because that cost sits under a settled parent whatever the label says.
  >
  > *A second finding falls out of the same table: **no device block disagrees with its parent,
  > anywhere**. So `recost_sub_meters` and `repair_device_rate_drift` (A12) are both no-ops on this
  > database — which is precisely the steady state A12 predicts, now observed rather than argued.*
  >
  > So device provenance is **inherited**, and the payload should say so explicitly — a device row
  > carries its parent's `bill_applied`, not its own `rate_source`. A client that reads the device's
  > own column will under-report settled cost, silently, on every account with a physical EV charger
  > or battery.
  >
  > *(Whether `recost_sub_meters` should also stamp the child's `rate_source` is a v4 behaviour
  > question and is NOT settled here. It is recorded because the payload must be right either way,
  > and inheriting is right even if the column is later fixed.)*

#### What the payload actually weighs

*Measured 25 Sep 2026 on a 4.5.14 prod database — 137,099 blocks, 37,525 segments, 2.37 meters per
slot (the multiplier is sub-meter rows, which §3 requires as first-class items).*

| request | raw | gzip | ratio |
|---|---|---|---|
| one day @ `resolution=block` | 42.2 KB | **2.3 KB** | |
| one month @ `resolution=day` | 10.1 KB | **1.7 KB** | 93 rows |
| one month @ `resolution=block` | 1,291.5 KB | **54.9 KB** | 23.5× |
| one year @ `resolution=block` | ~15 MB | ~650 KB | |

**A month of half-hourly blocks with segments is 55 KB over the wire.** The raw figure looks
alarming and is not the figure that matters: row-per-object JSON repeats every field name on every
row, and gzip recovers 23.5× of exactly that. This is worth stating because the obvious reaction to
1.3 MB is to invent a columnar or tuple-positional payload — which would buy a fraction of what
transport compression already gives, at the cost of a format nobody can read in a browser's network
tab.

**So `resolution=block` does not need a hard range limit for a viewport.** A year (~650 KB) is the
point where it starts to matter, and by then `resolution=day` is the honest answer to the question
being asked — nobody reads 52,560 half-hours. A limit is a guard against a client asking for all
history (~1.8 MB gzip), not a constraint on normal use.

And it confirms §2a's polling model is cheap for the right reason: **the poll carries the
FINGERPRINT, not the data.** Thirty day-tokens for a month is a fraction of a kilobyte; the payload
is fetched only when one of them moves.

**Segments are the unit of truth — for the MAIN meter's import split, and nothing else.** v4
established this (BL-27) and the import spec depends on it — legacy derived columns are dropped
precisely because segments reproduce them. The API must not reintroduce a parallel derivation.

> **Measured against a 4.5.14 prod database, 24 Sep 2026 — the claim holds where it is load-bearing
> and has three boundaries the payload must respect.** 137,099 blocks; 37,027 main-meter import
> blocks carrying kWh.
>
> **Cost is exact.** Summing `kwh x inc_rate` across a block's segments matches `imp_cost` on
> **37,027 of 37,027** blocks, worst difference **0.0016** — inside the sub-penny sliver §5.2 already
> describes. A client may derive cost from segments.
>
> **kWh is not, on 13 blocks.** Segment kWh differs from `imp_kwh` by up to **0.029 kWh** on thirteen
> blocks, ALL on 2026-09-11, all `rate_source='measured'`. The block agrees with the BILL exactly
> (`imp_kwh == measured_cost.kwh` on every one); it is the segments that match neither, with the
> rates compensating so cost still lands. So **`imp_kwh` stays authoritative for energy** — which is
> what the import spec already says by CARRYing it — and a surface showing "total import kWh" must
> read the block, not sum the split.
>
> **SUB-METERS HAVE NO SEGMENTS AT ALL.** 6,845 `ev_charger` blocks and 7,792 `house_battery`
> blocks: **zero** segments between them. Device energy lives in `blocks` rows keyed by `meter_id`,
> and no amount of segment reading will produce it. A payload that returns "priced blocks and their
> segments" therefore has to carry the sub-meter ROWS as first-class items — a client projecting
> device series from segments alone gets an empty chart, silently.
>
> **One drop-list column is NOT safe on that evidence: `imp_kwh_grid`.** The EV/house split columns
> are indeed NULL on every sub-meter block, so nothing is lost there. But `imp_kwh_grid` is SET on
> **70,497 sub-meter blocks** (35,438 ev_charger, 35,059 house_battery) — meters with **zero**
> segments — and it differs from `imp_kwh` on 451 of them, accounting for **86.4 kWh** of device
> draw that did not come from the grid. It is the value `recost_sub_meters` bills on
> (`COALESCE(imp_kwh_grid, imp_kwh)`), because a battery or V2X device draws from storage as well as
> from the grid and only the grid portion is chargeable. Dropping it as "reconstructable from
> segments" would discard it with nothing to reconstruct it from. Flagged to the import spec §5.2.
>
> **And a user correction usually leaves no segments behind.** Of 34 `rate_source='corrected'` main
> blocks, **23 have no segments** and 11 do. A segments-only reader shows nothing for two thirds of
> the blocks a user has explicitly corrected — the ones they are most likely to go back and look at.
> Whether that is a bug in the correction path or an accepted consequence is not settled here; it is
> recorded because the payload cannot assume segments exist.

---

### 3a. What the seventeen routes map onto, measured

*24 Sep 2026, after extracting `billing.py`. §5 of the architecture puts C at
"triage 126 endpoints"; for the billing/usage/insights corner the position is
better than that, and the shape §3 proposes is already the de-facto one in the
wrong clothes.*

**Every one of these already returns data, not HTML:**

| today | lines | §3 equivalent |
|---|---|---|
| `/api/insights/billing-period` | 50 | `resolution=period&include=carbon` |
| `/api/insights/calendar-month` | 39 | ″ |
| `/api/insights/calendar-year` | 33 | ″ |
| `/api/usage/billing-period` | 49 | `resolution=period&include=usage` |
| `/api/usage/calendar-month` | 36 | ″ |
| `/api/usage/calendar-year` | 32 | ″ |
| `/api/charts/blocks-summary` | 233 | `resolution=day` |
| `/api/charts/blocks-day` | 161 | `resolution=block` |
| `/api/billing` | 283 | `resolution=period&include=billing` |
| `/api/insights/periods` · `/api/insights/data-bounds` | 81 | **not covered — see below** |
| `/api/charts/{daily,heatmap,data-version,regenerate}` | 61 | die with the pipeline |

**Six routes differ in two independent ways and are expressed as six.** The axes
are *which period* and *what to aggregate*; `api_usage_calendar_month` is 36 lines
and `calendar_year` 32, differing only in which of `billing.py`'s period
resolvers they call. That is what the extraction makes visible — all four period
functions now live in one module, so the duplication has one obvious home.

**Both aggregators are whole-period rollups, not series.** `_aggregate_insights`
(272 lines) returns `carbon_g_net`, `effective_intensity`, `generation_mix`;
`_aggregate_usage` (463) returns `imp_cost`, `rate_tiers`, `weighted_rate`,
`peak_window_start`. Neither emits per-block points. The series exist only in the
two `/api/charts/blocks-*` routes. So §3's `resolution=period` vs `block` is not a
new distinction to build — it is the one the code already makes, minus a name.

#### The constraint that makes `resolution=period` worth having

**A period rollup must be its own query, not a block fetch followed by a
reduction.** Both current aggregators already do this deliberately —
*"uses a direct lightweight SQL query rather than `get_blocks_for_range` to avoid
fetching unnecessary columns and joining reads"* — and that property must survive
the consolidation.

It is what the chart-render problem actually was. `main.py` carries a
reconnect-storm guard because *"the render is CPU/GIL-bound, so it stalls the WS
heartbeat for MINUTES ... and IS the usual flap trigger"* — 300-second cooldown
plus a `render_recently_active()` check to break the loop. A quarter costs a
quarter's work because totals and series are one artefact. If `resolution=period`
is implemented as "fetch the blocks, then sum", the quarter still pays for the
scan and the only thing that changed is where the reduction happens.

So: **a long range asking for totals must not read per-block rows at all.** Then
"show the summary and no charts for a quarter" is the client declining to make a
second request, not a threshold anyone has to tune — and the expensive path stops
being the default rather than being guarded.

#### Period SELECTION is a separate endpoint, and §3 does not cover it

`/api/energy` answers *"what happened between these two instants"*. It cannot
answer *"which periods can I ask about"*, which today needs `insights/periods`
(46 lines) and `insights/data-bounds` (35). That is a different question —
config-period boundaries, billing days, the earliest block — and it is now
answerable entirely from `billing.py`'s four period functions plus the store's
bounds.

Keep it separate: `GET /api/periods?kind=billing|month|quarter|year`. Folding it
into `/api/energy` would mean an endpoint that sometimes takes a range and
sometimes returns the ranges available.

#### `resolution=` should not grow

`block` and `day` are what the store answers cheaply; `period` is the rollup.
**Arbitrary bucketing — `week`, `month`, a `bucket_minutes` parameter — must not
be added.** Anything between `day` and `period` is arithmetic the client can do on
points it already has, and offering it server-side re-creates the pre-rendering
problem one parameter at a time: each new bucket is another thing to compute, to
cache, to invalidate, and to disagree with the others about.

---

## 4. Billing charts specifically

Billing is the surface with the least room for error: it is the number a user reconciles against a
real bill. Three constraints carry over unchanged from v4.

**Stored cost is authoritative.** `imp_cost` on a settled block *is the bill*. It is never recomputed
from `rate × kWh` — segments intentionally differ by a sub-penny sliver which the remainder absorbs.
The API returns the stored figure; the client sums what it is given.

**Band vocabulary is a 2×2, and the API must preserve it.** *(Corrected 18 Sep 2026 — this section
previously said "one word per band" and committed v5 to `off_peak` / `peak`. That was wrong.)* The
four words are **attribution × band**: house `day` / `night`, EV `peak` / `off_peak`, plus
`standard` for *unknown*. House day/night is clock-determined (Economy-7); EV peak/off-peak is
dispatch-determined. They are different questions with the same answers, and a billing surface whose
whole job is explaining *why* a half-hour cost what it did cannot afford to conflate them. v5 keeps
all four; the importer's only change is the era-scoped house `off_peak → night` relabel
(import spec §5.2).

The API must not collapse unknown into a band either — a rate equal to the day's low bound does not
prove the supplier billed it off-peak.

**A stated zero is not a missing value.** A bill that says nothing was drawn is an answer
(4.5.13/BL-70). Three states must remain distinguishable in the payload: figures present, stated
zero, and never fetched.

### What replaces the rate lines

The rate lines are the part of the billing chart with the most accumulated v4 logic — cap-gating,
held-last-rate behaviour, the house/EV distinction. That logic moves to the API as **explicit
per-half-hour band and rate series in `tariff_context`, one per *(attribution, band)* cell**, rather
than being reconstructed by a renderer from block rates. A client draws what it is told. This is the
direct fix for the class of bug where a stored rate a whisker off the published band caused a line to
plot peak across a half-hour the bill had charged off-peak.

It is also why the 2×2 must survive into the payload rather than being flattened at the edge: the
house and EV lines are **independent** and routinely disagree. A house half-hour can sit on `night`
while the EV on the same half-hour is billed at `peak`, because the clock and the dispatch are
answering different questions. A renderer given a single low/high band for the slot cannot draw that,
and would quietly draw one of the two lines wrong.

---

## 5. Why it is wholesale: the document is the unit of update

v4 does not pre-render once. It re-renders **everything, every time anything changes** — so
"on-demand versus pre-render" is not the trade-off it appears to be.

The cause is structural, not a choice:

```
charts.html embeds an <iframe>                      (.chart-frame-wrap iframe)
   └─ loads /charts/daily_usage.html                (server.py:2242)
        └─ a COMPLETE standalone document           (<!DOCTYPE html> … </html>)
             └─ so the whole document is the unit of update
                  └─ so ANY change re-renders EVERY year held
```

A half-hour block from four years ago that has not changed is re-rendered because the last few
blocks did. There is no mechanism to update part of the artefact, because the artefact is a document
and the document is atomic. The five compensation layers in §1 all exist to manage the cost of that,
none of them to reduce it.

**So there is nothing to trade off.** Rendering only what the user can see, plus a buffer, is
strictly cheaper than rendering four years — at every database size, on every machine. The question
is not whether to scope to the viewport; it is only what the buffer should be.

### Two independent wins

This workstream delivers two things easily mistaken for one. They are separable, and keeping them
separate clarifies what is being bought:

| | win | why |
|---|---|---|
| **Scope to the viewport** | performance | Stops re-rendering four years for a change to the last half-hour. Available *even if the server kept rendering markup* — it needs the iframe-document model gone, nothing more. |
| **Serve data, not markup** | correctness | One derivation instead of two. This is what stops two surfaces disagreeing about the same number, and it is the reason `billing.py` must come out of `energy_charts.py` first. |

v5 takes both, for different reasons. The performance win needs no measurement; the correctness win
is not a performance argument at all.

### What genuinely remains to measure

Once a request is scoped to a viewport range, one narrow question is left, and it *is* worth
benchmarking on the Pi rather than assuming:

> Is computing a viewport's worth of data per request cheap enough, or is a **materialised period
> summary, maintained incrementally by the writer**, warranted?

Note the fallback still respects rule 1 — the writer updates one period's row, it does not recompute
everything. Measure: time-to-first-byte for a month and for a year, cold and warm, and queries issued
per surface. A surface making 200 small queries is the likely shape of any problem, and is invisible
in a wall-clock figure.

### Measured on the Pi, 29–30 Sep 2026 — the baseline C starts from

*From workstream A's soaks (A design §5e): Raspberry Pi, **SD card**, a real 4.5.16 database with
58,166 main blocks from 2023-06-05; `.build/soak.py run` at 2 workers / 4 req/s. Quiet phase of the
fifth run (no sweep; 4,060 requests), so these are the surfaces' own costs. 30 s is the client
timeout, counted at 30 s.*

| route | p50 | p95 | timeouts | n |
|---|---|---|---|---|
| `/api/charts/blocks-summary` | 10.51 s | 19.36 s | 2 | 38 |
| `/api/charts/daily` | 3.90 s | 4.92 s | 0 | 35 |
| `/api/usage/calendar-year` | 2.49 s | 7.51 s | 2 | 72 |
| `/api/billing` | 1.95 s | 9.88 s | 0 | 27 |
| `/api/charts/heatmap` | 0.96 s | 1.46 s | 0 | 41 |
| `/api/energy` (`resolution=period`) | 0.63 s | 4.92 s | 0 | 117 |
| `/api/backfill/gaps` | 0.62 s | 4.66 s | 1 | 33 |
| `/api/history-gaps` | 0.61 s | 3.40 s | 1 | 47 |
| `/api/insights/calendar-year` | 0.33 s | 17.43 s | 0 | 66 |
| `/api/usage/billing-period` | 0.29 s | 1.47 s | 0 | 239 |
| `/api/usage/calendar-month` | 0.24 s | 1.51 s | 0 | 142 |
| `/api/insights/billing-period` | 0.13 s | 1.66 s | 0 | 243 |
| `/api/charts/blocks-day` | 0.04 s | 0.39 s | 0 | 198 |

59 of 76 routes have p95 under 1 s; the whole-history ones are the problem, as §1 predicted.

**§1's cost, measured.** The pre-rendered chart pipeline was the single largest cause of
engine-loop stalls in those soaks: after a restore, `generate_charts` ran **on the loop** for
**445 s** and **497 s** (third and fourth runs — `server._regen_charts_safely` rendered there by v4
design). #12 moved it into the executor, and the fifth run's worst stall fell to 52.7 s — but that
remaining stall is the engine's commits waiting on an SD card the chart files, the share backups
and the WAL are all writing to (ROADMAP §A.1), and one of them dropped the HA WebSocket. Deleting
the pre-rendered files (step 4) removes the largest of those writers. That makes it the first
thing C does, not the last.

### Measured on the Pi after C, 30 Sep 2026 — the sixth soak (4.5.16-a20)

*Same Pi, SD card and database; the same restore and re-price sweep; `.build/soak.py` at 2 workers /
4 req/s, now asking for the Billing tab's own requests (#24). 5,377 requests, all 200.*

**No chart is rendered anywhere.** `scan-log` from the restore: 0 database faults, 0 render frames,
and the worst engine-loop stall **22.3 s** (fifth run 52.7 s; third and fourth 445 s and 497 s).
All 13 watchdog dumps have the engine thread in a **commit** — 74 of the 83 slow commits are
`set_block_segments` during the whole-history re-price sweep, none with a WAL checkpoint inside —
so what remains is the storage stall of ROADMAP §A.1, not C. One of them (22:23:51) was followed by
an HA WebSocket reconnect.

**The Billing tab's requests, built per request now:**

| request | p50 | worst | n |
|---|---|---|---|
| a billing period's bill (`resolution=period&kind=billing&include=billing`) | 0.90 s | 3.19 s | 32 |
| the previous billing period (vs Prev) | 0.94 s | 4.70 s | 42 |
| a month's bill | 0.82 s | 4.84 s | 58 |
| a month of day charts (`resolution=day&kind=month&include=billing`) | 0.63–0.92 s | 7.50 s | 97 |
| a quarter's bill | 2.33 s | 6.72 s | 57 |
| **a year's bill** | **7.07 s** | **16.23 s** | 40 |
| the picker's totals (`/api/periods?include=net_cost`) | 0.79–0.91 s | 15.82 s | 194 |
| `/lovelace/billing` | 0.03 s | 0.20 s | 46 |

The year's bill is CPU, not contention — 6.90 s p50 outside the sweep as well. The work is
per block (a year is 13,044 of them) in Python: the fetch and `calculate_billing_summary_for_period`.
Exact savings followed (same payload, byte for byte; `.build/reconcile_v4.py` still 80 of 80 to
the penny): the VAT calendar read once per summary instead of per block, timezone objects built
once, the day list without regrouping every block, `sqlite3.Row` columns read once per row in
`get_blocks_lightweight` (identical output over all 58,166 blocks), and `date().isoformat()` for
`strftime` in `compute_period_net` — **a year 0.443 s → 0.282 s on the Mac (−36%)**, which puts the
Pi near 4.5 s. A server-side cache was measured and not built: an exact key (hashing the range's
blocks, segments, dispatch and bill-split rows) costs a third of the computation, the Billing tab
already keeps each bill for the session, and a key that misses one input serves a stale bill.

---

## 6. Acceptance

- No pre-generated chart artefacts exist. `CHART_DIR` and its 28 call sites are gone.
- **A cost-neutral re-price invalidates.** Test: change segment bands without moving any total, assert
  the affected day's fingerprint moves. This is the property `_blocks_data_version` had and the one
  most easily lost in translation.
- **A change to today does not invalidate a historical day.** Test: finalise a block, assert only
  today's fingerprint moved.
- A write performs no rendering work. Demonstrated by a test asserting no render path is reachable
  from block finalisation.
- Every surface is served by the one endpoint; no surface has a private aggregation path.
- No function in the pricing/serving path accepts tariff context with a default value. Enforced by a
  test, in the style of the existing AST-based structural tests.
- Billing totals reconcile exactly with v4 over the same range on the same database — to the penny,
  using the stored costs.
- Per-surface time-to-first-byte budgets, asserted on the Pi.

---

## 7. Open questions

1. **Does the client re-aggregate, or does the API serve day/period rollups?** Serving rollups risks
   a second derivation path — the thing this workstream exists to remove. Re-aggregating in the
   client keeps one source but moves cost to the browser, which on a phone may be worse than on the
   Pi.
2. ~~**Push or poll for the watermark?**~~ **Closed by §2a: poll.** With no live tempo to serve,
   both real tempos are slow enough that a 30–60 s poll over a viewport range is negligible. This is
   a browser↔EMT decision and has no bearing on the HA WebSocket, which is a separate, inbound
   connection carried unchanged.
3. **How much history may one request cover?** An unbounded range re-creates the wholesale problem
   at request time instead of write time. A cap with pagination is probably right, but the number
   should come from the Pi benchmark, not from taste.
4. ~~**Does retiring `_blocks_data_version` lose anything?**~~ **Closed by §2a(3):** the property is
   carried forward explicitly — the fingerprint covers every displayed field, not just money, so a
   cost-neutral re-price still invalidates.
