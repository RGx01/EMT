# EMT v5 — The engine, looked at again

*Draft, 22 Oct 2026, at the user's request: "the engine has bloated and just needs a fresh look at
what we are trying to achieve and see if there are better more simple solutions to the known things
we work around vs what we've learned are needed." Engine first (`engine.py`, 15,325 lines, 341
functions); the web server later. Every mechanism below was read in the code; a docstring is a claim.*

**What this note does:** states what the engine is for, from first principles (§1); what we now know
about each source it reads, separating what was proved from what was believed (§2); lists every
mechanism in the engine against those (§3); and proposes a smaller engine and an order to reach it
(§4–5). Nothing changes until the user decides.

**Why now.** On 22 Oct 2026 a belief two months old — that Octopus labels a smart charge off-peak only
when the query window reaches ~2 h past it (4.5.7) — was disproven against a real statement
(`.build/probe_window_shapes.py`). The narrow windows it justified cached wrong bills; the cache's
settle-once rule (4.5.5) then protected them over the import's correct figures, and a purge did not
clear them. Three layers, each sound alone, made one wrong answer permanent. The question is how much
else in the engine is like that.

---

## 1. What the engine is for

**One record per half-hour of the main meter, holding three facts, each moving from EMT's estimate to
Octopus's answer and then staying put:**

| fact | EMT's estimate (live) | Octopus's answer | final when |
|---|---|---|---|
| **how much** — import and export kWh | the HA register's delta over the half-hour | DCC's half-hourly kWh (`/consumption/`), ~1–2 days | DCC answers |
| **what it cost** | kWh × the tariff's rate for the half-hour's band (dispatch-aware on Intelligent) | on a published-rate tariff: DCC kWh × rate; on Intelligent: Octopus's half-hourly cost (measurements; the four-bucket breakdown on IOG-SMB) | Octopus answers — or, past a limit, EMT's figure is the final one and says so |
| **how it splits** — House, EV, other devices | devices' HA reads, clipped to the grid; the car's share from its dispatch | on IOG-SMB, the breakdown's EV buckets | Octopus answers (held 28 days where it drops the car) |

**Around them:** the day's standing charge; the VAT calendar; export credit; carbon. **Per device:**
its kWh from HA, its grid share of the main meter's kWh, priced at the main meter's rate.

**What the engine must also be:** correct across clock changes and tariff changes (a half-hour is
priced by its own agreement); resumable (a restart loses nothing); gentle on Octopus (four instances
share one account's limit); and quiet on a Pi's SD card and a thin client's CPU (the loop is held
behind every synchronous write and every long pass in a web thread).

**What the engine is not for:** second-guessing a figure Octopus has answered — with one deliberate
exception, the 4.5.23 hold-back — or repairing its own stored data at runtime (v5 has no heals).

---

## 2. What we know about each source

*Proved = shown against a statement, a probe or a real database; believed = stated in code or a
design without that; disproven = shown wrong.*

| source | what we rely on | status |
|---|---|---|
| HA import/export registers | the delta over a half-hour is the half-hour's kWh | proved; with known faults: a lost or zeroed opener books the register as one half-hour (#307, #493), a power cut leaves 0.0 |
| DCC consumption (`/consumption/`) | kWh per half-hour, ~1–2 days late | proved |
| Measurements API — cost and label | `costInclTax.estimatedAmount` per half-hour is what was billed; the OFF_PEAK / STANDARD_RATE label says the band | **window-sensitive (proved 22 Oct 2026):** a smart charge reads off-peak only when the window holds the whole charge; a local day or wider matches the statement 48/48 |
| Measurements API — "stripped" answers | a narrow window over a dense run returns kWh and no cost (15 Oct 2026, the Pi) | believed for narrow windows; **not seen in day windows** (page size 500/48/10) — unproved either way since |
| Measurements API — the OFF_PEAK label "reserved for smart-charge credits", so a night half-hour reads STANDARD at the off-peak value | stated in the 4.5.5 design | believed; to probe |
| IOG-SMB four-bucket breakdown | Home = Σ ECO7, EV = Σ EV_DEVICE; the band from the bucket that carries the kWh | proved; **window-insensitive (22 Oct 2026, chunks = day = month on 37 half-hours)**; labels renamed once (#481), `EV_BOOST` new (28 Sep); a first answer can drop the car and be corrected days later (10 Oct, #510) |
| VAT in measurements | the cost carries the VAT that applied | **disproven once:** 5% after the 1 Oct 2026 0% change, until Octopus restated it (13 Oct) |
| Dispatches (GraphQL) | planned and completed smart-charge slots | proved for live capture; history is not kept by Octopus (only recent) |
| Tariffs and agreements | rates per agreement; a half-hour priced by its own | proved; IOG-SMB publishes windowless day/night rates (windows reconstructed); a running agreement's windows reach only now + 2 days (#513) |

---

## 3. The mechanisms

*Read 22 Oct 2026, the whole file in four quarters; callers grepped in `engine.py`, `web/server.py`,
`main.py` and `block_store.py`, tests not counted. About 75 mechanisms; grouped here by the fact they
serve. Line numbers are `engine.py` at `dev` 0d88e68.*

### 3.1 How much (kWh)

| mechanism | where | note |
|---|---|---|
| live capture, rollover, finalise | `capture_samples` 2100, `ensure_correct_block` 1947, `finalise_block` 4495 | the job itself |
| live gap fill | `detect_gap` 1805, `build_gap_blocks` 2361, in `_engine_tick` 5545 | needed; `GAP_FILL_LIMIT_HOURS` defined, 12 hard-coded |
| provisional sub-meter amend | `_amend_provisional_sub_meter_blocks` 4291, `_commit_provisional_as_final` 2969 | 2.10.0; rewrites the channel dict and drops fields it does not rebuild |
| rogue-read ceilings | `compute_channel` 2208 (500 kWh main, 60 kWh device), `_apply_pass2` 2621 (#307's 120 kW, #493's 500 kWh) | two ceilings for the same fault, in two places |
| restart opener reseed | `_reseed_opener_after_short_restart` 1111 | #493; needed |
| DCC poll and the settlement sweep | `kraken_poll_task` 14205, `retry_settlement_for_unsettled` 6737, `_maybe_run_settlement_sweep` 10602 | needed |
| PASS 2 re-run on settlement | `_rerun_pass2_for_settled_block` 4004, `_drain_pass2_queue` 4238 | also re-costs, repairs the standing charge, materialises export: see §3.2 |
| API history import | `import_api_history` 8398 | also prices: §3.2 |
| API gap fill | `run_gap_fill_job` 10144, `run_gap_fill_runs` 10400, `resolve_history_gaps` 14146 | three entry points; `run_gap_fill_runs` claims "import" by hand |
| recorder attribution, refill rebuild | `run_attribution_job` 3767, `rebuild_devices_after_refill` 3975 | needed |
| the Mini | `_maybe_setup_mini` 6354, `_collect_mini_into_block` 6497 | needed for API-only installs |

### 3.2 What it cost — seven places decide a half-hour's band or price

| mechanism | where | decides from |
|---|---|---|
| the finalise seam | `_reprice_main_import_block` 955 → `_dispatch_overlay_rate` 501, `_apply_iog_split` 657 | tariff + planned dispatch |
| PASS 2 re-run | `_rerun_pass2_for_settled_block` 4004 | holds or re-costs on settled kWh (#475) |
| history reprice sweep | `_run_historical_reprice_sweep` 7747 | the seam again, in chunks; replaced three backfills |
| predicted carve | `run_iog_split_carve` 7500 | completed dispatch |
| dispatch reconcile | `reconcile_dispatch_overlay` 13814 | completed/planned/started dispatch, hourly |
| import pricing and its repairs | `_billed_rate` 8027; `repair_import_pricing` 8903; `run_deferred_verify_pricing` 9577; the reprice queue | Octopus's cost ÷ kWh, else the schedule |
| the bill apply | `apply_measured_settled` 13474, `apply_measured_to_block` 13584 (four entry points) | Octopus's bill |

**Asking Octopus for a cost**, five ways: the live settled pass (`measure_settled_dispatched_blocks`
12511), the history drain (13228), the VAT resync (13121), the range re-fetch (12989), and the
import's own in-pass recovery and repair (8718, 8903). Since #269 they share one window; they still
treat a zero answer three ways (cached as ZERO, asked again for 30 days, skipped).

**Remembering what Octopus did not answer**, three records: the 4.5.23 hold-back (`bill_split_wait`,
28 days), the bill-ask backoff (`bill_ask`, 30 days, and its twin `split_ask`), and the import's
no-cost record (`note_import_cost`). Plus the reprice queue and the uncostable-era verdict.

**Status**, three stores for the import alone: run status, pricing health, verify summary.

### 3.3 How it splits

PASS 2's grid clip (`_apply_pass2` 2621), the 6-hour-cap split (`_apply_iog_split` 657), the carve,
the reconcile, the bill apply's EV (`_measured_ev_kwh` 13540), the dispatch-only refit
(`_refit_devices_to_house` 3238), the reprice sweep and recorder attribution: **eight writers** of
the House/EV/device division.

### 3.4 Around them

Carbon is written three ways (finalise PASS 3b, `_recover_missing_carbon` 3082, the historical
backfill 5140). Rate schedules are built twice (`_refresh_kraken_rate_schedules` 10726 live,
`_build_channel_rate_segs` 7224 historical). Two per-version backups at startup (14358, 14844).

### 3.5 Dead, inert or stale

- **Never called outside tests:** the Mini boundary path (6533–6620), so `_fire_block_boundary` at
  finalise is a no-op; `fetch_api_window` 8882; `engine_paused` 1423; `is_mode_configured` 290;
  `floor_to_hh` 1655.
- **Constant switches with a dead branch:** `_DISPATCH_OVERLAY_APPLY`, `_DISPATCH_RECONCILE_APPLY`,
  `_MEASURED_FETCH_ENABLED`, `_MEASURED_APPLY`, `_KRAKEN_DRY_RUN`; `_MEASURED_MATERIAL_GBP` has no
  reader.
- **Dead in place:** `_apply_pass2`'s `unprotected` loop (never filled); `finalise_block`'s
  `parent_sub_kwh` / `parent_sub_cost`; `_batt_kws`; `halted_at` (always None).
- **Scaffolding left running:** `_log_dispatches_observe_only` (step 1 of an overlay that now exists,
  a GraphQL call every 6 h that only logs); `reprice_history_conformance` (a gate for backfills
  already deleted); four overlapping "how far back" probes (6789–7148).
- **One-offs still in the start-up path:** `_detect_upgrade_mode` (3.0.0), `migrate_full_config_json`
  (2.0→2.1), `export_zero_fill_v1`, the region clobber, the pre-live snapshot, the legacy JSON-store
  warning, `_remove_stale_chart_files`.
- **Unvalidated:** the OHME capture branch (10943–11277), "UNVALIDATED" in its own comments.
- **Comments that contradict the code:** an age gate on provisional answers that does not exist
  (11519); `fetch_bills` (12779), which does not exist; `_measured_floor`'s third paragraph; the bill
  apply's "dispatch-bounded" EV (13617), where the code takes the bill's; "review-flagged by the
  caller" (13598); old window beliefs at 8718, 9043, 9415 (single-slot, bucket-less) after #269.

**Latent bugs found on the way** (confirmed in the code):
- `run_deferred_verify_pricing` 9643: the `else:` belongs to the span's `try`, not to `if restart`;
  a failed span write on a resumed pass leaves `cursor` unbound.
- `measure_settled_dispatched_blocks` 12557: a failed fetch returns without saving the ask records.
- `_bill_split_kept` (12248) saves the whole map, bypassing the 17 Oct merge-save race fix.
- Zero-export answers are "zero-value rows" at 8588 and "nothing" at 10263.

---

## 4. A smaller engine

**The idea: store what each source said, and derive everything else from it.** Most of the
mechanisms in §3.2 and §3.3 exist because each source's answer is written straight into the
block's price, band and split by its own writer, in its own order. A writer that comes later has to
know which earlier ones to respect (`rate_source`'s authority ladder, settle-once, `hold_rate`,
the hold-back), and a wrong answer that got in first stays (22 Oct 2026). Instead:

1. **Facts, one table each, written only by the thing that heard them:** HA's reads (`ha_reads`,
   done), DCC's kWh (`imp_kwh_api`, done), Octopus's cost and breakdown for a half-hour
   (`measured_cost` — the bill, with when it was asked), dispatches (`dispatch_slots`,
   `dispatch_history`), the user's corrections, the tariff. A newer answer from the same source
   replaces an older one; no source writes another's.
2. **One `settle(half-hour)`** that reads those facts and writes the block's cost, band, segments and
   split, every time, the same way: the bill if there is one (held where it drops the car); else
   DCC kWh × the tariff at the dispatch-aware band; else the live kWh. Devices clipped to the grid
   inside it. The finalise seam, the PASS 2 re-run's re-cost, the carve, the reconcile, the bill
   apply, the reprice sweep and import pricing all become **"a fact arrived — settle these
   half-hours"**. The sweep becomes `settle` over a range. Nothing needs an authority ladder: the
   rule is in one place, and running it twice gives the same answer.
3. **The import writes facts too.** Its kWh to the block, Octopus's cost to `measured_cost` — it is
   the same measurements answer the bill reader gets, asked over 60 days. Then a missing cost is
   just a half-hour with no bill yet, and `_billed_rate`, `repair_import_pricing`, the deferred
   verify pass, the in-pass recovery and the reprice queue are no longer needed.
4. **One queue of half-hours waiting for Octopus**, one record of each (asked when, answered or not,
   held, given up), asked a local day at a time, paced against the allowance. The live pass, the
   drain, the VAT resync and the re-fetch become rules for what goes on the queue: new and settled;
   unanswered (back off, give up at 30 days); held (until 28 days); a restatement suspected (the
   VAT case: the range again); the user asked (the re-fetch — compared with the block, §5 (c)).

This answers the cache question as well: the bill is a fact with a date, the block is derived from
it, and a re-fetch asks for a newer fact. A delete takes the half-hour's facts with it, and a purge
and re-import really starts again.

**What it keeps:** the job coordinator, PASS 2's grid clip, the cap split's arithmetic (`iog_cap`),
the hold-back's rule, the tariff stitching, the one bill reader, the window finding, the soft delete
design, the importer. **What it does not change:** the stored shape of the facts (the v4 import keeps
working); v5 still has no heals.

---

## 5. Order

Each step is its own PR, checked against the tariff goldens and real-database parity, and leaves the
engine working.

1. **Clear the dead and fix the latent** (§3.5): delete what nothing calls, the constant switches and
   their dead branches, the scaffolding and the one-offs the floor makes safe; fix the four latent
   bugs; correct the comments. No behaviour change but the bugs.
2. **The import writes Octopus's cost as a bill** (§4.3), and the import's repair, verify and queue
   go. The largest single cut, and the one today's problem came through.
3. **One ask queue and one record** (§4.4), replacing the live pass, the drain, the VAT resync's own
   fetch and the three give-up records.
4. **`settle()`** (§4.2): first as the bill apply and the PASS 2 re-run's re-cost, then the carve and
   the reconcile, then the finalise seam and the sweep.
5. **The rest:** carbon's three writers to one; the two schedule builders; the start-up backups.

---

## 6. For the user

- (a) Does §4's model — sources' answers kept as facts, everything else derived by one rule — match
  what EMT is meant to be?
- (b) The OHME branch has never been tested by anyone. Keep it, or take it out until someone with an
  OHME can?
- (c) The re-fetch's comparison: Octopus's answer now against the block (what the user sees), with
  the cached bill shown as history (22 Oct 2026 conversation). Inside §4 this is the natural rule; it
  can also go in before.
- (d) Step 1 first, on its own, before deciding the rest?
