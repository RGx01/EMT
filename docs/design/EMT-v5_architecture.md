# EMT v5 — architecture

*The agreed picture of what v5 is made of, what carries across from v4, and what does not.
Workstreams A and C hang off this; so does the porting decision. Where this document and a
workstream design disagree, this one is wrong and should be fixed.*

**Status:** draft, 18 Sep 2026. Every figure measured against merged 4.5.13.

---

## 1. Four layers

v4 has these layers too — they are simply not separated. v5's whole architectural change is to make
the boundaries real.

```
┌──────────────────────────────────────────────────────┐
│  presentation   a client draws from the API           │  NEW — nothing carried
├──────────────────────────────────────────────────────┤
│  API            one data surface, every consumer      │  consolidate 126 routes
├──────────────────────────────────────────────────────┤
│  domain         pricing, settlement, billing, carbon  │  CARRIED substantially as-is
├──────────────────────────────────────────────────────┤
│  storage        read/write split, one writer          │  carried + A's boundary
└──────────────────────────────────────────────────────┘
```

**The HA WebSocket is an inbound edge into the domain layer**, not part of the API or presentation
layers. It carries sensor subscriptions that drive block derivation, HA recorder history for device
attribution, and live state for the overview page. It is carried unchanged, and it is
unrelated to how the browser talks to EMT.

**The rule that makes the layering hold:** each layer may only call downward, and the domain layer
emits **values, never markup**. v4's central architectural fault is that its billing calculation and
its HTML generation live in the same functions — which is why two surfaces could disagree about the
same number.

---

## 2. What each v4 file becomes

| v4 file | lines | v5 |
|---|---|---|
| `block_store.py` | 7,332 | **Carried.** Gains A's read/write type split (104 write methods, 77 read-only, measured). |
| `engine.py` | 14,009 | **Carried.** 182 lines of chart wiring deleted (1.3%), 330 lines unreachable deleted (2.5%). ~96% survives. |
| `energy_charts.py` | 5,436 | **Split and mostly deleted.** 79% presentation dies; ~1,086 lines of billing/period logic move to a new `billing.py`. |
| `web/server.py` | 9,109 | **Consolidated, not rewritten.** 126 `/api/*` routes triaged into one surface; 22 page routes and the `CHART_DIR` pipeline go. |
| `web/templates/` | 19,233 | **Replaced** by a client that fetches from the API. |
| `reprice.py` | 169 | **Deleted.** Zero reachable functions. |
| `carbon.py` | 146 | **Carried** minus `carbon_from_reprice` and `house_carbon` (deleted 4 Oct 2026, BL-63). **`ev_carbon` is live** — `web/server.py` → `period_ev_saving` → `ev_carbon`. This row used to list it for deletion too (§4, corrected). |
| `pricing_segments.py` | — | **Carried** minus 3 unreachable functions. |
| `kraken_*`, `ha_client`, `iog_cap`, `bill_parser`, `instance`, `vat_calendar`, … | — | **Carried.** Untouched by this restructuring. |

---

## 3. Removing chart generation is a small, contained job

This was assumed to be the hard part. Measured, it isn't:

```
engine.py chart wiring       182 lines of 14,009   (1.3%)   7 functions, 17 call sites
block_store.py                 0 lines             (0%)     serves data either way
energy_charts.py           4,199 lines of 5,436   (79%)     presentation — deletes
```

The engine **pokes** the renderer; it is not woven into it. The three largest chart functions —
`generate_daily_import_export_charts` (2,027 lines), `generate_net_heatmap` (848),
`build_day_chart_html` (615) — are 3,490 lines of pure HTML emission with no domain logic to rescue.

### The real work: rescue the billing logic first

Only **5 of `energy_charts.py`'s 17 public functions** have any external caller. Two of those are the
HTML generators that die. The other three are domain logic that every surface depends on:

| function | lines | fate |
|---|---|---|
| `calculate_billing_summary_for_period` | 377 | **Move as-is.** Pure domain — see the correction below. |
| `get_billing_periods_from_config_periods` | 177 | Move to `billing.py` as-is. |
| `get_billing_periods_from_config_history` | 212 | Move to `billing.py` as-is. |
| `render_billing_summary` | 219 | **Dies with the charts.** HTML; its six callers are all inside `energy_charts.py`. |
| `build_meter_colors_from_config` | — | Presentation. Moves to the client. |

**Sequence:** extract `billing.py` *before* deleting anything. Rescue, then demolish.

> **CORRECTION, 24 Sep 2026 — the crux is not a crux.** This section said
> `calculate_billing_summary_for_period` was *"the crux of the whole workstream"* because *"the
> number and its rendering are the same 377 lines"*, and that until they were separable *"one API
> for every surface cannot be true"*. **Measured, they are already separate:**
>
> | function | lines | HTML tags |
> |---|---|---|
> | `calculate_billing_summary_for_period` | 377 | **0** |
> | `get_billing_periods_from_config_periods` | 177 | **0** |
> | `get_billing_periods_from_config_history` | 212 | **0** |
> | `bill_slots_from_blocks` · `octopus_bill_total` | 36 | **0** |
> | `render_billing_summary` | 219 | 78 |
>
> `calculate_billing_summary_for_period` returns a dict — `{start, end, meters, totals, …}` — and
> emits no markup at all. The HTML half is `render_billing_summary`, a separate function that
> consumes that dict.
>
> **Second correction, same day: `render_billing_summary` is NOT dead.** An earlier version of this
> note said it had "no caller anywhere" and was already dead. It has **six**, all inside
> `energy_charts.py` — the chart generators call it to render the billing panel they embed. The
> error came from a `grep` that excluded the defining file to avoid matching the `def` line, and so
> excluded its callers with it. `.build/check_reachability.py` exists for precisely this question and
> would have answered it correctly; grep was reached for because the question felt small. That is
> §4a's *"grep is not proof"*, committed twice in one session.
>
> The conclusion is unchanged and slightly strengthened: the renderer is the presentation consuming
> the domain's dict, which is the shape v5 wants. It dies WITH the chart generators at step 4, not
> as a separate deletion.
>
> So `billing.py` is a **move of roughly 1,000 pure-domain lines**, not an untangling, and the
> renderer dies with the other presentation rather than being dissected out of it. The step this
> document flagged as the one most likely to hurt is the cheapest in the sequence. *(The claim was
> plausible from the function's size and its neighbours; it was never checked against the body.)*

### Plotly is a free choice, not a migration

`plotly` appears in exactly **four places**: a `<script src>` in `energy_charts.py`, one
`plotly_restyle` handler, and two DOM lookups for `.plotly-plot` in `charts.html`. The 3,482 lines
of `generate_daily_import_export_charts` (2,027), `generate_net_heatmap` (848) and
`build_day_chart_html` (607) are **string building** — they emit HTML with a Plotly payload inside.

There is no Plotly abstraction to port. Whatever the client draws with is decided once, in the
client, and costs nothing in the demolition either way.

### Summary and series are different requests — state it before building

The chart render is not merely slow; `main.py` carries a reconnect-storm guard whose comment records
what it costs:

> *the render is CPU/GIL-bound, so it stalls the WS heartbeat for **MINUTES** — longer than the
> startup cooldown — and **IS** the usual flap trigger*

HA drops the socket, EMT reconnects, startup re-runs, it renders again. Breaking that loop needed a
300-second cooldown AND a `render_recently_active()` check. That is today's behaviour on the
reference account.

The cause is structural: a billing period's **totals** and its **series** are one pre-rendered
artefact, so asking for a quarter costs a quarter's rendering even when the answer wanted is four
numbers. v5 should separate them at the API:

- **summary** — totals per meter and per band for a period. Cheap, bounded, and what most of the
  billing surface actually shows.
- **series** — per-block points for plotting. Expensive, and requested only when something is going
  to draw them.

Then "show a summary and no charts for a long range" is not a performance switch to add later; it is
the client declining to make a call. The expensive path stops being the default rather than being
guarded.

---

## 4. The unwired paths

Code nothing calls is not free. It misleads whoever reads next. None of this is ported.

> **The example this section used to give was wrong, and the true version is more useful.** It read:
> *"a bug was diagnosed at length against `_hybrid_ev_by_block`, confidently and wrongly, because
> the unreferenced path read as load-bearing."* `_hybrid_ev_by_block` is **not** unreferenced — it
> has two live callers (`web/server.py:2763`, `:2926`). It is the EV rule for accounts **with** a
> physical EV meter, while `_dispatch_ev_split_by_bucket` serves accounts without one.
>
> So the misdiagnosis was not dead code reading as live. It was **the right function for a different
> account shape** — which is harder to spot, because no amount of reachability analysis would have
> flagged it. Dead code misleads; *conditionally live* code misleads more quietly, and this section
> cannot help with that. What can: reading which branch selects the function before assuming it is
> the one that ran.

AST reachability from real entry points, deliberately over-approximating (a call by bare name or
attribute matches any function of that name), so anything still reported unreachable is confidently
unreachable.

**This is now a committed tool, not a one-off analysis:** `.build/check_reachability.py`, with
`.build/reachability_baseline.json` as a regression guard and `tests/test_reachability_tool.py`
checking it against cases established by hand. §4a required a trace before any deletion; until the
tool existed that was a rule with nothing enforcing it.

**Whole codebase: 40 of 1,402 definitions unreachable** (29 Oct 2026, with near-equal rates one row on the bill card, the day panel and Insights — `bill_card.merge_near_rates`; 40 of 1,401 earlier that day, with corrections refused with the supplier API — `_corrections_api_gate_active` and `_corrections_unreconciled_count` gone, `_corrections_refused` added; 40 of 1,402 earlier that day, with a battery sensor counting solar flagged — `battery_sensor_flags`, `confirm_battery_sensor`, the two routes; 40 of 1,398 earlier that day, with a battery sensor's evidence of counting solar recorded — `battery_devices`, `battery_evidence_rows`, `battery_sensor_verdict`, `check_battery_sensors`, `_maybe_check_battery_sensors`; 40 of 1,393 earlier, with Octopus's changed agreements kept for the user to accept — `_store_agreements`, `_remember_refused`, `_forget_refused`, `refused_agreements`, `accept_refused_agreements`, the two routes; 40 of 1,385 earlier that day, with an answer no customer change explains not taken — `_unexpected_drops`; 40 of 1,384 earlier that day, with a change of agreements re-settling its history — G1d: `_merge_spans`, `_agreement_change_spans`, `_queue_settle_spans`, `_settle_spans_pending`, `_settle_spans_pass`; 40 of 1,379 earlier that day, with a half-hour on no Octopus agreement — G1c: `settlement_state.other_supplier`, `_ss_other_supplier`, `octopus_labelled_outside`, `_log_label_mismatch`; 40 of 1,375 earlier that day, with discovery retried until it answers — G1b: `_maybe_retry_discovery`, `_start_discovery_retry`; 40 of 1,372 earlier that day, with the tariff agreements stored — G1a: the table's two store methods, `_agreement_info`, `_agreement_rows`, `_keep_agreements`, `_load_stored_agreements`; 40 of 1,366 earlier that day, with a published tariff's half-hour final by its agreement — `engine.published_rate_at`, `settlement_state._published`; 40 of 1,364 earlier that day, with the panel saying an API import's bill is v4's price — `_reach_floor`; 40 of 1,363 earlier that day, with the detail panel naming an import's kWh and cost — `_origins`; 40 of 1,362 earlier that day, with v4's sensor-era rate kept by the import — transform 3d, its gate, `_sensor_band`; 40 of 1,358 earlier that day, with each agreement's window parsed once, `_agreement_windows`; 40 of 1,357 on 27 Oct, with engine step 5c — one per-agreement fetch, `kraken_rates.agreement_schedules`, read by the stitch and the import; 40 of 1,355 earlier that day, with engine step 5b — carbon's one formula, `_carbon_of`; 40 of 1,354 earlier that day, with engine step 5a — the upgrade backup after the store opens and the pre-live snapshot gone; 40 of 1,355 earlier that day, with engine step 4e-2 — the history reprice sweep and what only it reached: `_reprice_history_block(s)`, `_apply_iog_split`, `iog_cap.compute_iog_split`, the dispatch-only house refit, their store methods; 40 of 1,371 earlier that day, with engine step 4e's first removals — the device rate drift repair, step 2a's band parity report, finalise's dispatch overlay and the PASS 2 re-run's overlay re-resolve; 40 of 1,380 earlier that day, with the Import bill's `imported` for an IMPORTED bill and for no bill past Octopus's two years; 40 of 1,379 earlier that day, with every block write, the history sweep and v5's CSV import through settle() — engine step 4d, the settle sweep with them; 40 of 1,370 on 26 Oct, with the carve and the reconcile through settle() — engine step 4c, their seven store writes and reads gone with them; 40 of 1,373 on 25 Oct, with the bill apply and the PASS 2 re-run through settle() — engine step 4b, `_measured_ev_kwh` and `recost_sub_meters` gone with them; 40 of 1,369 earlier that day, with imported cost as a bill — engine step 4a-2; 40 of 1,366 earlier that day, with engine step 4a's settle() and its parity report; 40 of 1,349 on 24 Oct, with engine step 3e's removals — the import's no-cost record, the old askers' queries; 40 of 1,358 earlier that day, with the re-fetch writing through the asker's path — engine step 3d; 40 of 1,357 earlier that day, with the asker's page written in one commit; 40 of 1,354 earlier that day, with the one asker — engine step 3c; 40 of 1,352 earlier that day, with the ask record in a table of its own — engine step 3b; 40 of 1,348 earlier that day, with the ask dry run saying what it read; 40 of 1,347 earlier that day, with engine step 3a's ask dry run; 40 of 1,337 on 23 Oct, with the October VAT resync gone — engine step 2d-3; 40 of 1,346 earlier that day, with the import pricing by the rule — `_billed_rate` gone, engine step 2d-2; 40 of 1,347 earlier that day, with an import's bills applied straight after it, a page at a time; 40 of 1,346 earlier that day, with the import's repair machinery gone — engine step 2d-1; 40 of 1,383 earlier that day, with the API import keeping Octopus's bands; 40 of 1,381 earlier that day, with the predicted band's second stage, the dispatch lifecycle; 40 of 1,380 earlier that day, with the detail panel's band note; 40 of 1,378 earlier that day, with the bill apply settling on Octopus's band at kWh × rate; 40 of 1,379 earlier that day, with the band parity report reading IOG-SMB's band from its buckets; 40 of 1,378 earlier that day, with engine step 2a's band parity report; 40 of 1,369 on 22 Oct, with the engine review's step 1's scaffolding gone — the observe-only dispatch log, the reprice conformance tool, three page-less history probes and `consumption_probe.py`; 40 of 1,389 earlier that day, with the Mini boundary chain and five other dead functions removed; 50 of 1,402 earlier that day, with the bill reader asking a local day at a time; 50 of 1,399 earlier that day, with the health counts standing while a job rewrites history; 50 of 1,398 on 21 Oct, with an imported half-hour's missing export shown as a gap; 50 of 1,396 earlier that day, with the machine in the support file; 50 of 1,392 earlier that day, with the month counts kept across restarts; 50 of 1,388 earlier that day, with the health counted a month at a time; 50 of 1,386 earlier that day, with Issues only folding whole months; 50 of 1,385 earlier that day, with the health strip's counts cached; 50 of 1,384 earlier that day, with the sub-pages retired and the settlement source not carried; 50 of 1,391 earlier that day, with corrections on the Billing tab; 50 of 1,390 earlier that day, with the support file; 50 of 1,370 earlier that day, with the Backups & storage card; 50 of 1,369 earlier that day, with Data Management's health strip; 50 of 1,366 on 20 Oct, with the bill ask's hourly-then-daily backoff; 50 of 1,365 earlier that day, with an imported half-hour the drain asks for waiting for its bill; 50 of 1,364 earlier that day, with a refill's rebuild queued until admitted; 50 of 1,363 earlier that day, with the `reads` table's five methods deleted; 55 of 1,368 earlier that day, with Delete… on the heatmap; 55 of 1,366 earlier that day, with the rebuild reading HA's reads; 55 of 1,360 earlier that day, with HA's reads kept apart; 55 of 1,356 on 19 Oct, with deleting an imported export; 55 of 1,348 earlier that day, with a cost EMT no longer asks Octopus for shown as such; 55 of 1,344 earlier that day, with the welcome screen the only way in before setup; 55 of 1,343 earlier that day, with a waiting gap asking the Mini for its latest read; 55 of 1,341 earlier that day, with a device's sensors on the device screen; 55 of 1,340 earlier that day, with Device History on the device's own sensors; 55 of 1,337 earlier that day, with the importer moving Device History's sensors onto the device; 55 of 1,335 earlier that day, with a device's sensors stored in one place; 55 of 1,331 on 18 Oct, with a gap filled as a whole and from three sources; 55 of 1,328 earlier that day, with the export states knowing whether there is an export sensor; 55 of 1,327 earlier that day, with the heatmap's gap fill on the server and where Octopus's history starts — it calls `tombstoned_slots_in`, dead until then; 56 of 1,316 earlier that day, with a running agreement's schedule rebuilt; 56 of 1,315 earlier that day, with the apply's refusals logged; 56 of 1,314 earlier that day, with the stale-page notice; 56 of 1,313 earlier that day, with the teal half-hours' breakdown asked; 56 of 1,309 earlier that day, with the bills asked on every Intelligent agreement, back to Octopus's two years; 56 of 1,307 on 17 Oct, with the delete's tombstone on block boundaries; 56 of 1,305 earlier that day, with the daily re-discovery; 56 of 1,300 earlier that day, with the export state keyed on settlement and its agreement; 56 of 1,298 earlier that day, with the bill records' merge-save; 56 of 1,296 earlier that day, with capped history judged by its agreements; 56 of 1,291 earlier that day, with capped pricing by the half-hour's agreement; 56 of 1,290 earlier that day, with the uncapped seam's bands from its rate; 56 of 1,289 earlier that day, with the bill-ask give-up; 56 of 1,284 on 16 Oct, with one dispatch-split population; 56 of 1,286 earlier that day, with the runtime split heal removed; 56 of 1,288 earlier that day, with the importer's bill-split transform; 56 of 1,286 earlier that day, with one EV + house builder; 56 of 1,285 earlier that day, with one hold-back reference; 56 of 1,281 earlier that day, with the cost recovery's day and month rungs; 56 of 1,280 earlier that day, with one bill reader; 56 of 1,277 on 14 Oct, with the split stage by agreement; 56 of 1,276 earlier that day, with the Import bill view by tariff kind; 56 of 1,273 earlier that day, with the Bill view by cost provenance; 56 of 1,272 earlier that day, with the re-fetch diff compared like with like; 56 of 1,271 earlier that day, with the restore upload in pieces; 56 of 1,265 on 13 Oct, with the settlement stages, views, detail panel and range re-fetch; 56 of 1,243 earlier that day, with the stages; 56 of 1,241 on 12 Oct, with the bill split hold-back; 56 of 1,234 on 11 Oct, with the staged restore and the welcome page; 56 of 1,225 on 10 Oct, with the importer package and its transforms; 58 of 1,188 on 6 Oct — 84 of 1,162 at the 4.5.14
seed, 79 of 1,157 after the five deletions below, and the total has since grown with workstreams A,
B and C; on 6 Oct `_format_billing`, a fourth copy of the Direct-import subtraction with no caller in
v4 or v5, went with `direct_import.py`). **This figure is asserted against the tool by `tests/test_b_design_figures.py`** — it had
drifted five times before that check existed, each time right when written and wrong a week later.
The
figures below predate the tool and were produced by an ad-hoc run; re-run it before acting on any
of them.

> **84, not the 88 first reported.** The tool's first real use — the `reprice.py` deletion — found
> it calling `IngressMiddleware.__call__` unreachable. That is the WSGI entry point: every request
> in the add-on passes through it. Python invokes it through syntax, so no AST node anywhere
> carries the name, and a pure name-trace sees no caller. `__aenter__`/`__aexit__` (`async with`)
> and `__len__` (`len()`) were wrong the same way. The whole dunder namespace is now excluded.
>
> The lesson is not the four names. It is that **"it never lies about dead" was an intent, not a
> property** — it held only until something was invoked other than by writing its name. §4a's
> suite-green gate is what catches this class, so it is not optional.

> **Read a clean report as "no evidence of death", never as "proof of life".** The trace treats any
> identifier-shaped string literal as a possible `getattr`, so a name that appears as a JSON key is
> reported live. *(The worked example given here until 4 Oct 2026 — `carbon.ev_carbon`, said to be
> reachable only through the dead `carbon_from_reprice` — was wrong: `period_ev_saving`, which is
> live, has called `ev_carbon` since `carbon.py` was first added. The trace reported it live
> because it is.)* Narrowing that rule
> would lose genuine dispatch-by-name and could report a live function as dead — the direction that
> deletes working code.

> **Unreachable is not unreferenced, and neither is deletable.** The scan skips `tests/`, because
> the question it answers is "does the application reach this". A name with no application caller
> can still have fixtures, builders and whole test modules standing on it, so the deletion footprint
> is a separate measurement and is usually larger than the function. Of the 84 at the seed, only
> **22 (272 lines)** were never so much as named by the suite — and of those, only **five had no
> consumer, no recorded intent and nothing behind them**: `io_save`, `iog_cap._hours`, `save_json`
> (the non-atomic sibling of the live `save_json_atomic`), `BlockStore.get_setting` (a wrapper over
> `get_settings()`) and the nested `rate_breakdown_html`. Twenty lines, removed 22 Sep 2026 as the
> first deletions under §4a — deliberately boring ones, chosen to test the protocol's mechanics
> rather than its judgement. `reprice.py` is the worked example of everything else — §4b.

### `engine.py` — 14 functions, 330 lines of 13,129 (2.5%)

| function | lines | note |
|---|---|---|
| `_maybe_backfill_historical_exc` | 62 | one of the three legacy backfills |
| `_apply_mini_boundary_read` | 54 | Mini-mode path |
| `_maybe_backfill_historical_iog_split` | 50 | legacy backfill |
| `_maybe_backfill_historical_segments` | 50 | legacy backfill |
| `_naive_iso` · `fetch_api_window` | 38 | |
| `_regen_charts_after_import` | 17 | dies with charts anyway |
| `_mini_read_and_apply` · `_mini_boundary_callback` | 27 | Mini-mode |
| `_review_band_reason` | 15 | |
| `register_block_boundary_callback` | 9 | |
| `is_mode_configured` · `floor_to_hh` · `io_save` | 8 | |

**The three legacy backfills (162 lines) were "retired" in 4.5.x by unwiring rather than deleting.**
That is the pattern this section exists to stop: the retirement was real, the removal never happened,
and the code has sat since looking like part of the system.

*Deleted 4 Oct 2026 (BL-33).* The 162 lines above were the three schedulers alone. The real footprint
was their workers, `_net_alarm`, `_reprice_sweep_done` and twelve marker/flag constants — **524 lines of
`engine.py`** (12 comment lines added back) — plus two whole test files and tests in six more. The trace reported 8 names
unreachable and none newly so after; the call site at `engine_startup` said "dormant net, to be
deleted a release later". The house/EV split for a late completed dispatch was never theirs: that is
`run_iog_split_carve` (every dispatch-capture tick) and `_attribute_missing_ev_split` (the reconcile).

### Whole modules and tables

- **`reprice.py` — 169 lines, all four functions unreachable from the application. IT STILL COMES
  ACROSS.** *(Reversed 22 Sep 2026, the first time §4a was exercised for real.)* Three things the
  trace could not see:
  1. **It is not 169 lines.** `tests/test_reprice_core.py` (12 tests), `tests/fixture_capped.py`
     and `tests/test_fixture_capped.py` stand on it — **480 lines and 15 tests**, because the scan
     skips `tests/` by design.
  2. **It is unwired, not abandoned.** 4.4.0's *"single, pure per-block pricing model"*, built and
     tested and never called. Production prices through `engine.py`'s `_reprice_main_import_block`.
  3. **It and v4 already share the arithmetic.** Both bottom out in `iog_cap.price_slot`, which
     *is* the four-rate rule. `tests/test_adapter_differential.py` drives both wrappers over 144
     reachable scenarios — capped/uncapped, boost, in/out of window, three cap-boundary shapes,
     Agile plunge/zero/spike — and they agree exactly. Two differences, both pinned there: v4 clips
     EV-above-grid at each of its two call sites where reprice.py clips inside the core, and
     reprice.py's `_reconcile_house` moves a segment rate by 1e-6 on a cap-boundary straddle with
     the block total identical.
- ~~**The `reads` table — unreachable, not merely empty.**~~ **Deleted 20 Oct 2026**, the table with
  its five methods: v4 2.0.0's raw-sample store (`SQLITE_MIGRATION_PLAN.md`), never written; HA's
  data is kept at the half-hour in `ha_reads` (soft delete design §2). Five store methods (`insert_read`,
  `link_reads_to_block`, `get_reads_for_block`, `get_reads_for_range`, `purge_reads_older_than`)
  with **no caller outside the test suite**. Nothing has ever written a row; it is empty in all
  three real databases because it cannot be reached. The `# ── Reads (Phase 2+) ──` banner above it
  dates the intent: a raw-read store the block model made unnecessary. Same pattern as the three
  legacy backfills — planned, superseded, never removed.
- ~~**`web/server.py` — `_format_billing` (server.py:857).**~~ **Deleted 6 Oct 2026**, with
  `direct_import.py`: it was a fourth copy of the Direct-import subtraction. No callers. An older copy of the Overview
  card's row formatter, superseded by `_fmt_total` inside `api_billing()`. Found while fixing #468,
  and it carries that same bug: it builds its breakdown from `totals` alone, so it would show no EV
  on a no-device account. A second formatter for one surface, silently behind by a bug fix — exactly
  the two-implementations-of-one-idea pattern A §3.4 names.
- **`carbon.py` — two functions, not three** *(corrected 4 Oct 2026)*. `carbon_from_reprice` has no
  caller anywhere, and `house_carbon` is called only from inside it. **`ev_carbon` is live**: the live
  `period_ev_saving` calls it (`carbon.py`, since the file's first version), so it does not fall with
  them. Following the three-function version of this line would have deleted working code. Both
  went on 4 Oct 2026 (BL-63).
  *(An earlier version of this list said "`carbon_from_reprice` only" — it under-reported by two,
  because the apparent callers of `ev_carbon` and `house_carbon` in `web/server.py` are local
  variable names, `ev_carbon_block` and `house_carbon_g`, not calls.)* `period_ev_saving` and
  `slot_intensity` are live and stay.
- `pricing_segments.py` — `attribute_devices`, `import_segments`, `total_cost_exc`. **Three of
  eleven public functions**, and the other eight are live: six of them have no *external* caller
  but are reached transitively from `segments_from_legacy` and `price_devices_hybrid`. *Moved to
  `tests/segment_helpers.py` on 4 Oct 2026 (BL-63), not deleted: the suite used them to build
  segments for tests of the eight live ones.*

**Correct the v4 roadmap — and this note was itself half wrong until 19 Sep 2026.** BL-63 describes
the shelved layer as including `carbon.ev_carbon` / `house_carbon` and *"9 of 11 public functions in
`pricing_segments`"*.

- **`pricing_segments`: BL-63 is wrong, and dangerously so.** Three of eleven are unreachable, not
  nine. Acting on "retire the projection layer" would delete six working functions. This is the
  headline correction for the v4 roadmap.
- **Carbon: BL-63 is right and this document was wrong.** An earlier version of this paragraph
  claimed *"the Δ5 wiring made the carbon functions live"*. Δ5 made `period_ev_saving` and
  `slot_intensity` live; it did not touch `ev_carbon`/`house_carbon`. *(Corrected 4 Oct 2026:
  `ev_carbon` was reachable all along, through `period_ev_saving`. Only `house_carbon` was reachable
  solely through the dead `carbon_from_reprice`.)*

The two errors point opposite ways, which is the useful part. **Under-reporting dead code is safe
— you delete less than you could. Over-reporting it is not.** BL-63 over-reported, and its
recommendation would have acted on the over-report. That asymmetry is why §4a fixes the default at
*when the trace is ambiguous, it stays*.

---

### 4b. Two boundary facts the inventory turned up

Neither was anyone's finding at the time; both fell out of asking why a function had no caller.

**EMT never writes to Home Assistant.** `HAClient` has nine read methods, all live, and two write
methods — `set_state` and `call_service` — both unreachable. There is no MQTT publisher and no
other `POST /api/states` anywhere in the codebase, so those two are the whole write surface and
neither is wired. `call_service`'s docstring names the feature it was built for
(*"persistent_notification create/dismiss"*), which was never finished.

> **v5 should inherit this deliberately and be able to enforce it.** A read-only HA boundary is a
> real architectural property — it means EMT cannot corrupt a user's Home Assistant state, ever —
> and right now it holds by accident. It is one structural test away from holding on purpose.

**`reprice.py` is a pricing core for one channel on one tariff family.** Its docstring calls itself
*"the one model every code path conforms to"*. Measured against 4.5.14, it is the one model for
**dispatched IOG import**, and nothing else reaches it:

| | reaches `price_slot`? | priced by |
|---|---|---|
| IOG capped | yes — four rates, cap boundary | the rule |
| IOG uncapped | yes — fall-through, four equal rates | the rule (totals unchanged) |
| Agile / flat / fixed / Eco7 import | **no** — `_apply_iog_split` returns at `ev_kwh <= 1e-9` | schedule resolver |
| Agile carrying legacy dispatch rows | yes, as uncapped — *"Agile never caps"* | the rule |
| **export, any tariff** | **never** | `exp_cost = exp_kwh × exp_rate`, flat |

Export is not an oversight to be fixed later: `_dispatch_overlay_rate` refuses it on its first line
(*"Import channel only"*), `_apply_iog_split` only touches `imp_ch`, and `reprice.py` contains no
export concept at all. So adopting it as v5's pricing core still leaves every non-IOG import tariff
and all of export on the schedule-resolver path — including `_billed_rate`, which carries its own
tariff-type discriminator and its own two documented hazards (export fragmentation, the price-cap
transition-day trap). **Plan the port around that scope rather than discovering it mid-port.**

---

### 4a. The deletion protocol

*Added 19 Sep 2026. v5 is a **subtractive** project against a codebase that works: three years of
tariff edge cases, 2,722 passing tests, and a production install people rely on. The risk that
matters is not writing a bug — it is removing something that was load-bearing and not finding out
for months.*

**Nothing is removed unless all five hold.**

1. **An over-approximating AST reachability trace reports it unreachable.** Deliberately
   over-approximating: a call by bare name or attribute matches *any* function of that name, so the
   trace over-reports what is live and anything it still calls dead is confidently dead.
2. **The full suite is green after the removal**, run through `run_tests.sh` — the suite is
   file-isolated and a plain `pytest tests/` collides at collection, so a "passing" run that did not
   use the runner has proved nothing.
3. **The removal is its own commit.** No mixed refactor. If it turns out to have been load-bearing
   in six months, the fix is `git revert <sha>`, not an archaeology session picking one deletion out
   of a rewrite.
4. **The commit message records the evidence**, not the intention: what the trace said, and what
   was searched. "Appears unused" is not evidence.
5. **The comment at the site that would have called it has been read.** *(Added 22 Sep 2026.)*
   Dead code is sometimes dead on purpose, and the reason is recorded where the wiring isn't — not
   at the definition, where anyone deleting it would look. The Mini boundary chain
   (`_mini_boundary_callback` → `_mini_read_and_apply` → `_apply_mini_boundary_read`, 81 lines) is
   the case: the trace calls it dead, the suite stays green without it, and `engine.py` says at the
   point it declines to register it —

   > *"We deliberately do NOT register the old post-finalise boundary callback here: that path is
   > kept defined as a dormant fallback, but registering it too would double-write the block."*

   *Deleted 22 Oct 2026* (engine review §3.5, step 1), with the boundary hook it would have plugged
   into (`register_block_boundary_callback`, `_fire_block_boundary`), the reader's
   `read_at_boundary` and its rate-limit check, and `store_mini_import`. The call-site comment was
   read: the fallback was a choice to keep code that never runs, in case it were wired again by
   hand — nothing falls back to it. The reason lives on here and in git.

   Gates 1–4 all pass on that code. Only reading the would-be call site stops the deletion.

#### What none of that covers, and where the real exposure is

**Unreachable is not unreferenced, and neither one is deletable.** The trace answers *"does the
application reach this"* — `tests/` is out of scope by construction, so a name with no application
caller may still carry fixtures, builders and whole test modules. Of the 84 unreachable at the seed,
only **22 (272 lines) were never so much as mentioned by the suite**, and reading a name off the report
as a deletion candidate is how `reprice.py` got listed at 169 lines when the real footprint was 480.
Measure the footprint separately, every time.

The suite proves the **engine and the store**. It does not prove a **surface**. Delete a chart
function and nothing goes red, because what it produced was HTML no test asserts on — and the
presentation layer is exactly what this programme demolishes. So for anything in the surfaces:

> **A deletion with no test covering it needs a before/after comparison against a real database**
> — the parity harness. The programme map downgrades that harness from prerequisite to "useful"
> because the copied suite is the primary check. That downgrade is correct for the domain and
> **wrong for the surfaces**, which are the part with no test to be primary.

#### Three kinds of proof that do not count

Both have already produced a wrong answer in this project, so they are named rather than assumed:

- **Grep is not proof.** A pattern search classified `needs_pass2_rerun` as write-only while
  `_drain_pass2_queue` plainly reads it. Four `SELECT * FROM blocks` sites and a `_row_to_block`
  mapper mean columns are returned without ever being named.
- **A clean reachability report is not proof.** The tool's own docstring claimed it *"never lies
  about dead"*. Its first real use falsified that: `IngressMiddleware.__call__` was reported
  unreachable, and it is the WSGI entry point that every request in the add-on passes through.
  Python invokes it through syntax, so no AST node carries the name. `__aenter__`/`__aexit__`
  (`async with`) and `__len__` (`len()`) were wrong the same way; the dunder namespace is now
  excluded, which is what moved the headline from 88 to 84. The general form: **anything invoked
  other than by writing its name is invisible to a name-trace.** Read a clean report as "no
  evidence of death", never as "proof of life".
- **Data is not proof.** `finalised_from_cad` holds a single value across 136,202 rows on the
  reference account — because that account runs `mini_provisional=off`, not because the column is
  dead. Single-valued on one database is not dead; it is one configuration.

#### The asymmetry that sets the default

Leaving dead code in costs a reader's attention — real, and the reason §4 exists at all. Removing
live code costs a user their data or their surfaces, silently, and is found by someone else later.
**When the trace is ambiguous, it stays.** A second pass with better evidence is cheap; an
unnoticed regression in a billing figure is not.

---

## 5. The API is already there

```
/api/* routes : 126
page routes   :  22
```

85% of `server.py`'s routes are already data endpoints. C is therefore **triage, not invention** —
consolidate 126 endpoints onto one coherent shape, delete the 22 pages and the pre-render pipeline
behind them. Far less risk than "build an API", and the work is mostly deciding what the shape is
(see [EMT-v5_C_surfaces_design.md](EMT-v5_C_surfaces_design.md)).

---

## 6. How v5 gets built: freeze v4, seed EMT with a verbatim copy

**Decided 18 Sep 2026.**

### The shape

```
4.5.14   BL-71 + BL-72 + BL-73. The LAST v4 feature release.
   │     BL-72 is mandatory: the app version must be stamped into the
   │     user's database BY v4 — EMT cannot retrofit a marker it was not
   │     present for, and one added later never covers the backups already
   │     sitting on users' disks.
   │     That stamp is what makes .14 the importer's EARLIEST source and
   │     collapses E from two schema eras to one (import spec §1.1, §3, §4).
   │     NOT a computed heal level — heals are marker-gated and ungated
   │     markers run at startup, so a version is sufficient.
   │     BL-73 stamps the supplier registry key. NOT optional: v4 is not
   │     Octopus-only — `not-listed` (local metering, no supplier API) is a
   │     supported state with real kWh history, so the supplier cannot be
   │     derived from `written_by`. The ACCOUNT stamp needs nothing:
   │     `kraken_account_number` and the mismatch guard are already built
   │     (import spec §1.1, §7).
   │
   ├──▶  v4 FROZEN. Users sit on a stable release for as long as they like.
   │
   └──▶  EMT repo, commit one: 4.5.14 copied VERBATIM. Suite green on day one.
         Then deconstruct in the open, at whatever pace:
           1. dead code removed (§4)
           2. billing.py extracted from energy_charts.py (§3)
           3. A — ReadStore / WriteStore
           4. C — API consolidated, HTML pipeline deleted
           5. schema — 11 columns dropped; band moves to block_segments alone
           6. client — including the FIRST-RUN FORK: new setup vs restore
                       from a v4 backup (import spec §3a)
           7. importer (E) — its own module, one-directional, with a retirement rule
         Users arrive when it is ready, install alongside, and retire v4 themselves.
```

**Note what step 5 does *not* include.** An earlier draft had the band vocabulary "unified" here.
It is not: v5 keeps all four labels (house `day`/`night`, EV `peak`/`off_peak`), because the two
axes are different determinations — clock versus dispatch — not two names for cheap and expensive.
*(Closed 10 Oct 2026, ROADMAP BL-81: the relabel is not wanted. Octopus's 28 Sep rename made its own buckets side × rate — HOME/EV LOW RATE, HOME/EV STANDARD RATE — with no "night"; house `off_peak` stays in every era and the importer relabels nothing.)*
The only change was to be a **house `off_peak → night` relabel, SMB era only**, and it belongs to **step 7,
in the importer**, as a declarative column rule. See [EMT-v5_schema_notes.md](EMT-v5_schema_notes.md)
§3a and the import spec §5.2.

Step 5 is therefore a pure *column* change: bands stop having a block-level home and live only in
`block_segments`, read alongside the segment's attribution.

### Why a verbatim copy

Copy as **commit one**, followed by deconstruction — not copy-and-leave-it:

- **The suite runs from the first commit and stays green through every step.** In a module-by-module
  port it cannot run until the port completes, so the refactoring happens blind and integration is
  where you learn. Here, all 2,711 tests are the safety net continuously.
- **The parity harness becomes optional rather than essential.** The suite *is* the parity check.
  (A harness is still worth having for cross-surface reconciliation against real databases — but it
  stops being a prerequisite.)
- **Every refactor is a diff against known-working code**, reviewable as such.

### Alternatives rejected

**Refactoring in place across 4.5.14–.17** rides users through the whole restructuring. A–C touch the storage boundary, the API and
the entire presentation layer; shipping that as four production releases exposes every user to every
intermediate state. **The premise of the whole plan is that users stay on something stable while the
kinks are ironed out.** The schema-neutrality of A and C makes in-place look attractive; it is not
the constraint that matters.

**Porting module by module** cannot run the test suite until the port completes, so the refactoring
happens blind and integration is where the divergence is discovered.

### The divergence rule

A copy means a fix can land twice. That needs a stated rule, not good intentions:

> **After 4.5.14, v4 accepts critical fixes only — data-loss, crash-on-start, or a pricing error. No
> features, no refactors. Every such fix is cherry-picked into EMT the same day it lands in v4.**

**A critical fix that repairs already-written data adds a heal**, and a pricing-error fix is exactly
that kind. So the rule has a second clause:

> **Any v4 fix that adds a heal also bumps EMT's `FLOOR_VERSION`, in the same cherry-pick.**

Otherwise the importer keeps admitting backups written before that heal existed, and v5 has no
v4-era heals to fix them on arrival. This is why .14 is the *earliest* source rather than the floor:
the actual bar is wherever v4 stops, and it is pinned before v5's first release (import spec §1.1,
Q9).

Without it the two drift until reconciling them is its own project. With it, divergence stays a
handful of small commits that apply cleanly, because EMT's early history is v4's history.

### A side benefit worth exploiting

EMT's schema only diverges from v4's at step 5. For the whole of steps 1–4, an EMT build can read a
v4 database almost directly — so the migration path is testable from very early, long before the
importer is written. Use that: run EMT against a copy of a real v4 database at every step and
compare surfaces. It is the cheapest parity signal available and it exists for free until the schema
moves.

## 7. Scale of the job, honestly

```
carried substantially as-is   ~21,000 lines   block_store + engine + kraken_* + friends
consolidated                   ~9,000 lines   server.py's 126 routes
rescued then demolished         ~5,400 lines   energy_charts.py (1,086 survive as billing.py)
deleted outright                  ~700 lines   reprice.py + 85 unreachable names
replaced entirely              ~19,000 lines   templates → a client
```

The domain — the expensive part, three years of tariff edge cases — is carried. The restructuring is
concentrated in presentation and the seam between domain and presentation.

---

## 8. Open questions

1. **What is the client?** **Defaulted 19 Sep 2026: server-routed pages, no framework.** Revisit
   only against the trigger below.

   *Why this stopped being a big question.* It was listed as "the only genuinely unscoped layer",
   which overstated it. Both options — pages or a single-page app — fetch from the same API over a
   viewport range, and neither pre-renders anything. **The choice has no bearing on the
   write-triggers-wholesale-recompute fix, the fingerprint, or the API shape**; all of that is
   settled in C regardless. It decides one thing only: whether clicking a nav link reloads the
   browser document or swaps it in JavaScript.

   *Why pages is the default.* Closest to v4's 22-page shape, so the client is the layer that
   changes least. No bundler and no build step. And EMT runs inside **Home Assistant's ingress
   iframe**, where path rewriting has already caused trouble once — the 4.5.13 health-panel demo
   picker had to move in-page because a URL parameter could not reach the iframe. SPA routing under
   ingress inherits that whole class of problem for a benefit we have not yet shown we need.

   *The one trigger to revisit:* if carrying **date range and meter selection across navigation**
   turns out to be annoying enough to pay for tooling. That is the SPA's real advantage here — state
   survives a screen change — and it is a judgement to make once the surfaces exist, not before.
2. ~~**Where does `billing.py` live in v4's lifetime?**~~ **Closed by §6.** It is extracted in the
   EMT repo, as deconstruction step 2, with the copied suite green throughout. Extracting it in v4
   would have meant another release for users to absorb, which §6 rejects.
3. ~~**Read snapshot lifetime.**~~ **Closed — and it was a duplicate.** The same question was
   recorded here and in [A §7.1](EMT-v5_A_storage_design.md); the decision now lives there only, so
   it cannot be answered twice differently. **`ReadStore` ends its read transaction between
   requests**, so every request takes a fresh snapshot.
4. ~~**Does the parity harness come first?**~~ **Closed by §6: no.** The copied suite is the primary
   check from commit one, which downgrades the harness from prerequisite to useful. Still worth
   building for cross-surface reconciliation against real databases, and §6's side benefit (EMT
   reads a v4 database directly until step 5) makes it cheap to run continuously — but nothing waits
   on it.