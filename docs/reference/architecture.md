# How EMT is built

*Reference — describes the code on `dev`. Why it is this way is in
[../history/](../history/); when this page and the code disagree, the code is right and this page is
fixed.*

## The pieces

| Layer | Modules | What it does |
|---|---|---|
| Entry | `main.py`, `run.sh` | `run.sh` picks the mode — supervised when a Supervisor token is present, else standalone (`HA_URL`, `HA_TOKEN` required) — and starts `main.py`, which opens the store, binds the readers (`store_access.bind`), and starts the engine loop and the web server. |
| Engine | `engine.py` | One asyncio loop: Home Assistant's WebSocket, finalising half-hours, the Octopus calls, the settle sweep, the asker, backups. |
| Domain | `settle.py`, `settlement_state.py`, `billing.py`, `iog_cap.py`, `kraken_rates.py`, `vat_calendar.py`, `pricing_segments.py`, `direct_import.py`, `ev_split.py`, `carbon.py` | Pure rules: pricing a half-hour, its settlement state, the bill, the 6-hour cap, rate schedules, VAT, the house's Direct import, the EV split, carbon. No store, no clock. |
| Storage | `block_store.py`, `store_access.py`, `store_types.py` | One SQLite database, `blocks.db`; the schema (`EMT_SCHEMA = 5`); typed readers. |
| Suppliers | `kraken_api_client.py`, `kraken_ingester.py`, `kraken_mini.py`, `agile_public.py`, `suppliers.py` | Octopus's GraphQL and REST APIs; the Home Mini; public Agile prices. |
| Import | `v4import/` | Bringing a v4 database across: the gate, the carry, the transforms, the checks. See [v4-import-and-first-run.md](v4-import-and-first-run.md). |
| Web | `web/server.py`, `web/templates/` | Flask: Jinja pages (each an HTML shell), and JSON routes they fetch from. See [api.md](api.md). |
| Home Assistant | `ha_client.py` | Reads sensors, their history and live state. **EMT writes nothing to Home Assistant** — it creates no entities; `set_state` and `call_service` have no callers. |

## Reading and writing the database

- **The engine's store.** The engine opens one `BlockStore` and is its only user: SQLite's default
  `check_same_thread` refuses it on any other thread.
- **Readers.** Every other thread reads through `store_access.store()`: a `ReadStore` per thread,
  opened `query_only`, exposing only the read methods (generated into `store_types.py` from
  `.build/store_split_baseline.json`). A restore swaps the file and bumps a generation, and each
  thread reopens on its next read.
- **Writes from a page** go to the engine's thread: `_engine_write` / `_engine_call` in
  `web/server.py` run the write on the loop and wait for it. Long background workers (a restore, a
  delete) open their own connection (`_own_store`).
- `store_access.writer()` and `WriteStore` exist but have no production caller.
- The database runs in WAL mode with `synchronous=NORMAL`. On an SD card a single commit can stall the
  loop for seconds for about a minute after a restore or an update; a writer thread is designed for
  it and held in reserve ([ROADMAP](../ROADMAP.md)).

## The job coordinator

`job_coordinator.py` keeps one manifest, `_MANIFEST`, of the jobs that rewrite history, each with
what it depends on and what it cannot run beside: `live_tick`, `import`, `prune_tombstones`,
`settle_sweep`, `carbon_backfill`, `measured_drain`, `attribution`, `gap_fill`, `iog_split_carve`,
`range_refetch`, `delete`, `restore`. A job is marked `@job(...)` where it is defined; asked to run,
it is admitted, deferred, made to wait or preempted. A paused engine's tick skips rather than queues,
and `drain_tick()` waits out one in flight. The one route that writes outside the coordinator is the
database vacuum (`api_db_vacuum`).

**The coordinator today, from the tools.** The suite asserts these lines against the tools that
produce them (`tests/test_b_design_figures.py`); a PR that moves one updates it here.

- **7 live engine jobs** (`.build/check_job_guards.py`): `run_api_import_job`,
  `_run_settle_history_sweep`, `_run_historical_carbon_backfill`, `run_measured_history_drain`,
  `run_gap_fill_job`, `run_iog_split_carve`, `run_attribution_job`.
- **13 members**, 12/13 registered with the coordinator (`.build/check_job_membership.py`), the live
  tick among them; the one outside is the vacuum.
- **1 bulk route handler** writing on the request thread (`.build/write_inventory.py`):
  `api_db_vacuum`. It rewrites the whole file and takes minutes on a Pi, so it pauses the engine
  rather than hop onto its thread.
- **7 of the 7 live jobs write `blocks`** (`.build/write_scope.py`), so a rule by table would be one
  global lock: conflicts are declared job by job instead.
- **Two jobs take a scope** — a span or a device: `run_attribution_job` and `run_gap_fill_job`. The
  others' `max_blocks`, `limit` and `force` are budgets, not scopes.

How the coordinator came to be, and the figures as they stood then:
[the coordinator record](../history/design/EMT-v5_B_coordinator_design.md).

## The tables

| Table | Holds |
|---|---|
| `blocks` | One row per meter per half-hour: import and export kWh (live and Octopus's settled `imp_kwh_api` / `exp_kwh_api`), the device's grid share (`imp_kwh_grid`), rate, cost, ex-VAT cost, standing charge, carbon, provenance (`source`, `rate_source`). |
| `block_segments` | Each half-hour's priced legs: kWh, rate, band and attribution (house / EV). |
| `measured_cost` | Octopus's bills: each half-hour's cost, label and kWh, and on IOG-SMB the house/EV buckets. |
| `octopus_ask` | What has been asked of Octopus and its state — the asker's record, the hold-back. |
| `ha_reads` | What Home Assistant reported for each device: live half-hours, and recorder hours kept as hours. |
| `device_sensors` | Each device's primary sensor and its history sensors. |
| `tariff_agreements`, `tariff_prices` | The account's agreements as Octopus states them; their prices, for display. |
| `deleted_ranges` | Tombstones over deleted spans, so nothing refills them unasked. |
| `config_periods`, `meters`, `meter_channels` | The configuration over time: the site, billing day, timezone, supplier; the meters and their channels. |
| `dispatch_slots`, `dispatch_history` | Intelligent Octopus Go's dispatches, as captured. |
| `store_meta`, `kraken_state` | Small keyed state: `written_by`, `emt_schema`, the import audit, home eras, the EV's style; Octopus's account and discovery state. |

## Checks that keep it this way

`.build/` holds tools the suite runs against committed baselines: the store's read/write split
(`classify_store_methods.py`), the job guards and membership, the write inventory and scope, the
query budgets, reachability (`check_reachability.py`, whose headline the architecture record states),
the GraphQL deprecation and field watch. A change that moves one of these updates its baseline in the
same PR.

**Reachability today: 40 of 1,517 definitions unreachable** (`check_reachability.py`). The suite
asserts this line (`tests/test_b_design_figures.py`): a PR that adds or removes a definition updates it.
The history of the figure is in the [architecture record](../history/design/EMT-v5_architecture.md) §4.
