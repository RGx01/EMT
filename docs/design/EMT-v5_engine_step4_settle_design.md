# EMT v5 — Engine step 4: one settle() for every half-hour

*Draft, 25 Oct 2026. Step 4 of the engine review (`EMT-v5_engine_review.md` §4.2, §5). Every code fact
read at `dev` d4c8486; line numbers are `engine.py` unless named. The map was made by reading every
writer; the claims that decide the design were checked again by hand.*

**What this note decides:** one function that writes a main-meter half-hour's cost, band, segments,
EV/house split and devices from its facts, the same way every time — and which of today's writers
become "a fact arrived, settle these half-hours". It does not change the facts (`ha_reads`,
`imp_kwh_api`, `measured_cost`, `dispatch_slots`, `dispatch_history`, corrections, the tariff), the
bill reader, the asker, the hold-back's rule or the cap's arithmetic (`iog_cap`).

---

## 1. Today: eleven writers, each its own rule

| writer | runs | price | band | segments | split | devices |
|---|---|---|---|---|---|---|
| **finalise** (`finalise_block` → `_reprice_main_import_block`) | block boundary | sensor or live schedule × kWh | overlay (planned slot ≥ 0.1 kWh), cap blend | staged | completed dispatch, if recorded | PASS 2 at the main's rate |
| **gap blocks** (`build_gap_blocks`) | after an outage ≤ 12 h | the pre-gap rate | — | — | — | PASS 2 |
| **PASS 2 re-run** (`_rerun_pass2_for_settled_block`) | DCC kWh new or changed, every tick | held (a)/(b), or re-resolved (d) | (d) only | refit / staged | re-carved at the held rate | PASS 2 |
| **carve** (`run_iog_split_carve`) | dispatch capture, hourly | — | labels | ✓ | completed dispatch | re-fit |
| **reconcile** (`reconcile_dispatch_overlay`) | hourly | ✓ | the dispatch lifecycle | ✓ one rate | keeps the stored EV | ✓ |
| **bill apply** (`apply_measured_to_block`) | hourly, after import, the asker, the re-fetch | kWh × the settled band's rate | Octopus's | ✓ | the bill's EV, else the dispatch | re-cost + re-fit |
| **history sweep** (`_run_historical_reprice_sweep`) | start-up, after import | ex-VAT where missing | — | where missing | where missing | re-fit |
| **API import** (`import_api_history`) | user, gap fill | kWh × the label's rate | label | — (left as they were) | — | — |
| **CSV import** (`apply_csv_import`) | user | the row's rate | — | — | — | — |
| **correction** (`api_corrections_apply`) | user | the user's rate | flattened | rates only | kept | ✓ |
| **device repairs** (`_refit_devices_to_house`, `run_device_rate_drift_repair`, attribution) | after the above, hourly, user | — | — | — | — | ✓ |

The writers the review would fold into `settle()` — the seam and its helpers, the re-run's pricing,
the carve, the reconcile, the bill apply and the sweep — are about **1,700 lines**.

## 2. Where they disagree (each checked in the code)

1. **The rate comes from six places:** a sensor or the live schedule (finalise), the historical
   per-agreement schedule (import), the live `day_rate_bounds` (bill apply), the live
   `off_peak_rate_near` / `resolve` (reconcile), the pre-gap rate (gap blocks), the row (CSV).
2. **Ex-VAT four ways:** the live schedule's exc/inc ratio, the historical schedule's, the VAT
   calendar, the block's own stored pair.
3. **A capped half-hour five ways:** finalise stores the 4-rate blend as `imp_rate`; the sweep
   computes it but keeps the stored rate; the carve prices the EV at the block's single rate; the
   re-run holds every leg at one rate; a reconcile verdict flattens it to one rate. A bill-settled
   `mixed` half-hour stores `imp_rate` = the **peak** rate (11861–11864) with `imp_cost` = its four
   legs — so `recost_sub_meters` prices its devices at peak, and the drift repair agrees, because it
   compares against `imp_rate`. *(Latent: none on dev today.)*
4. **The kWh:** the reconcile prices whatever `imp_kwh` holds (CAD before the re-run); the bill apply
   refuses until it is the DCC figure; the re-run moves a settled block's kWh to DCC and keeps its
   cost, so `imp_cost` ≠ `imp_kwh × imp_rate`.
5. **The EV:** completed dispatch (seam, carve, sweep); the bill's EV (bill apply on IOG-SMB); the
   stored EV unchanged (reconcile, correction).
6. **Who re-bands after the kWh settles:** #475's rule is "a settled kWh may re-cost and must never
   re-band" (the re-run's branch (b) holds the rate) — yet the reconcile re-bands a DCC-settled block
   both ways, and the re-run's branch (d) re-runs the overlay and cap when the stored rate is 0/NULL.
7. **Settled or corrected overwritten:** the API import's upsert has no guard (and leaves
   `rate_source` and segments as they were); the correction route overwrites a settled block;
   `apply_measured_to_main` and `apply_reconcile_verdict` rely on their callers' queries.
8. **Stale segments:** an import overwrite, a CSV overwrite and a gap block write the columns and
   leave the segments; nothing invalidates them, and the sweep then skips the block (it has segments).
9. **Agreement gates are uneven:** `_agreement_priced_ok` (overlay, reconcile, bill apply), `capped_at`
   (split, bill-apply mixed), `tariff_kind_at` (bill apply, asker) — neither in the carve, the sweep
   or the gap blocks.
10. **Order matters:** the hourly order is fixed (reconcile → carve → asker → apply → drift repair),
    and the drift repair must run last because the bill apply's device re-cost is best-effort.

**Also found, not part of settle():**
- the store's `rate_source` backfill (`NULL` → `'schedule'`) runs on **every** writer open, not once
  (block_store 1511–1517) — harmless, as every reader treats the two alike;
- a round-trip write (`append_block_replace`) re-points a block at the **current** config period
  (block_store 7054) — *latent: dev has one period*;
- the correction route sets the rate on **every** meter's segments at those half-hours (no meter
  filter, web/server.py 9531–9538).

## 3. The proposal: settle(half-hour)

One pure rule, then one write:

| part | from | rule |
|---|---|---|
| **kWh** | DCC if settled, else the live reading; an imported block's own | — |
| **band** | a bill if Octopus has settled it (`settled_band`: the carrying bucket on IOG-SMB, else the label); else the prediction (`predicted_band`: the dispatch slot, then the lifecycle, as step 2b–2c read it); a published tariff → the clock | billed wins (decided 23 Oct) |
| **rate** | the tariff schedule for that band, on the half-hour's **own** agreement (the stitched schedule) — one function for live and history | — |
| **split** | on IOG-SMB with the bill's buckets, the bill's EV; else the completed dispatch, clipped to the kWh; capped → `iog_cap.compute_iog_split`'s legs | the hold-back keeps EMT's split where the bill drops the car (unchanged) |
| **segments** | the legs: EV and house, each at its band's rate | — |
| **cost** | Σ segment kWh × rate | always |
| **ex-VAT** | each leg's rate through the VAT calendar | one way |
| **correction** | the user's rate on every leg, and the half-hour reads corrected | a correction outranks everything but a bill re-fetched on purpose (design §5.2) |
| **devices** | PASS 2's grid clip, priced from the legs (§4 (b)) | — |
| **provenance** | `rate_source` *written* as what settled it (`settled` / `predicted` / `corrected`), never read to decide whether to write — except `corrected` | — |

**Every writer becomes a trigger:** a fact arrives → `settle(those half-hours)`.

| fact | today's writer | becomes |
|---|---|---|
| a live reading at the boundary | finalise | write the kWh, settle |
| DCC kWh | PASS 2 re-run | write `imp_kwh_api`, settle |
| a completed dispatch | carve | settle the half-hour |
| the lifecycle settles (hourly) | reconcile | settle the half-hours it touches — the reconcile's rule is inside the prediction |
| a bill | bill apply | cache it (as now), settle |
| an import / gap fill / CSV | import's own pricing | write the kWh (and the bill), settle the range |
| a correction | the correction route | record it, settle |
| start-up / a schema change | sweep | settle a range |

Running `settle` twice gives the same answer; nothing needs an authority ladder or a hold.

## 4. Open — for the user

- (a) **`imp_rate` on a half-hour with legs at different rates** (capped, or a bill's `mixed`): the
  blend, cost ÷ kWh (so `imp_cost = imp_kwh × imp_rate` always; the legs keep the real rates), or the
  main band's rate (today's bill apply: peak)?
- (b) **A device's price on such a half-hour:** the main's single rate (today), or the rate of the
  leg its kWh sits in (the EV charger at the EV legs' rate)?
- (c) **Ex-VAT one way:** the VAT calendar on each rate (as the bill apply does since 2b), rather than
  the schedule's own exc rates — they differ only by Octopus's rounding.
- (d) **#475 under settle():** a predicted band is a function of the facts, and the dispatch slot's
  0.1 kWh floor reads the kWh — so a DCC kWh crossing it would re-band a half-hour not yet billed.
  Keep #475 (once a half-hour's kWh has settled, its *predicted* band is frozen until a bill or a new
  dispatch fact arrives), or let the prediction follow the settled kWh?

## 5. Order (each its own PR; step 2's §3a safeguards: a backup before staging takes each,
parity seen first, additive first)

| PR | what | writes |
|---|---|---|
| **4a** | `settle()` as a pure function, and a **settle parity report**: over every half-hour, today's stored cost, band, segments and devices beside `settle()`'s, every difference with its reason — run on every instance | nothing |
| **4b** | the bill apply and the PASS 2 re-run call `settle()` | settled and re-run half-hours |
| **4c** | the carve and the reconcile become triggers | dispatched half-hours |
| **4d** | the finalise seam, the gap blocks, the imports and the sweep | new and imported half-hours |
| **4e** | the removals (~1,700 lines), and the drift repair once `settle` writes devices with the main | — |
