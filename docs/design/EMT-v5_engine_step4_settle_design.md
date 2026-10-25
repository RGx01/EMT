# EMT v5 — Engine step 4: one settle() for every half-hour

*Draft, 25 Oct 2026; §4's four decisions made the same day. Step 4 of the engine review (`EMT-v5_engine_review.md` §4.2, §5). Every code fact
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
| **band** | a bill if Octopus has settled it (`settled_band`: the carrying bucket on IOG-SMB, else the label; an imported bill with neither, Octopus's cost ÷ its own kWh — §6 (e)); else the prediction (`predicted_band`: the dispatch slot, then the lifecycle, as step 2b–2c read it), its thresholds judged on the DCC kWh once there is one (§4 (d)); a published tariff → the clock | billed wins (decided 23 Oct); **never read back from a rate** (§4 (a)) |
| **rate** | the tariff schedule for that band, on the half-hour's **own** agreement (the stitched schedule) — one function for live and history; `imp_rate` = cost ÷ kWh (§4 (a)) | — |
| **split** | on IOG-SMB with the bill's buckets, the bill's EV; else the completed dispatch, clipped to the kWh; capped → `iog_cap.compute_iog_split`'s legs | the hold-back keeps EMT's split where the bill drops the car (unchanged) |
| **segments** | the legs: EV and house, each at its band's rate | — |
| **cost** | Σ segment kWh × rate, and `imp_cost = imp_kwh × imp_rate` | always |
| **ex-VAT** | each leg's rate ÷ (1 + the VAT calendar at the half-hour's UK-local date) (§4 (c)) | one way |
| **correction** | the user's rate on every leg, and the half-hour reads corrected | a correction outranks everything but a bill re-fetched on purpose (design §5.2) |
| **devices** | PASS 2's grid clip; each device priced at the rate of the legs its kWh sits in (§4 (b)) | Σ device cost ≤ the main's cost |
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

## 4. Decided (25 Oct 2026)

Each applies only where a half-hour's legs are at different rates — a capped half-hour, or a bill's
`mixed` — except (c) and (d), which apply to every half-hour.

**(a) `imp_rate` is the blend, cost ÷ kWh.** So `imp_cost = imp_kwh × imp_rate` holds on every
half-hour and 4a's parity report reads any break as a defect, with no list of exceptions. The legs
keep the real rates, and the charts already draw from them (the house line from the house segments,
`energy_charts.py` 783–786; the EV line from the EV segments). finalise already stores the blend
(`iog_cap`'s `effective_rate`); the bill apply's peak (11861–11864) goes.
- *Rejected:* the main band's rate — `imp_cost ≠ imp_kwh × imp_rate` on every such half-hour, and
  devices priced at peak.
- *v4's lesson, and the condition it sets.* v4's bill apply snapped to the nearer tariff rate
  ("never a cost/kWh derivation", 4.5.7), because v4 read the band **back from the rate**: a blended
  rate slipped past the reconcile's `abs(cur_rate − off_peak) < 1e-6` test and silently disarmed the
  bump revert (BL-62; v4 ROADMAP archive, "the residue that is not solved"). Under `settle()` the band
  comes from the evidence and the rate from the band, never the reverse. **Nothing may band, or match
  for equality, on `imp_rate` or an ex-VAT rate** — stated here, and guarded by a test in 4a
  (`settle.Facts` holds no stored rate, cost or split, and `settle_facts` reads none).
  The band-from-rate reads still in v5 (grep, 25 Oct 2026, at 4a's start):

  | where | reads | what happens to it |
  |---|---|---|
  | the reconcile's `currently_off_peak` and `_blk_band` | the **stored** `imp_rate`, nearer of the day's two — a decision input | live today: finalise stores the cap's blend and the reconcile does not skip capped half-hours (§2 item 3); goes in 4c |
  | the bill apply's `_prior_band` | the stored `imp_rate` | the review note only; goes in 4b |
  | `predicted_band`, `iog_cap._overlay_is_peak` | the overlay's **computed** rate | a clean tariff rate, not stored — safe; `settle()` takes the overlay's decision as a band when the finalise seam goes through it (4d) |
  | `_bucket_bands` (`settled_band`) | **Octopus's** bucket unit rate | the bill's own price, the one rate a band may be read from |
  | the drift repair | devices against the main's `imp_rate`, for equality | wrong on a two-rate half-hour under (b); goes in 4e |
  | `server.py` `_cluster_nearby_rates` | rates, for Insights' tiers | display, not a band |

**(b) A device is priced at the rate of the legs its kWh sits in.** In PASS 2's existing order
(`_grid_priority`: a `meter_type` `ev`/`ev_charger` meter first):
1. the EV charger fills the EV legs and pays their rate; any kWh beyond them (the charger read more
   than the bill's EV) pays the house legs' rate;
2. every other device pays the house legs' rate;
3. where one group of legs has two rates (a capped EV: part off-peak, part peak), the device pays that
   group's blend — a half-hour read cannot place it more finely.

The devices are clipped to the main's kWh already, so Σ device cost ≤ the main's cost and the house
never clamps to zero. v4 priced every device at the main's single rate (`claimed × parent_rate`) and
never by leg; on a single-rate half-hour the two agree. *Latent:* the local staging copy has no
half-hour with legs at two rates, so none with a device drawing; dev is not yet counted. 4a counts
both. `recost_sub_meters` and the drift repair assume one rate per half-hour, so `settle()` writes the
devices with the main and 4e removes them.
- *Rejected:* the main's rate (the blend after (a)) — right in total, wrong per line: the charger reads
  too dear, the house's devices too cheap.

**(c) Ex-VAT one way: the VAT calendar on every leg** — inc ÷ (1 + VAT at the half-hour's UK-local
date), as the bill apply does since 2b. It covers every source (schedule, correction, published
tariff, imported row), so a rate's ex-VAT never depends on where the rate came from. Since the 0%
change `RateSchedule.with_vat` rebuilds the schedule's inc from Octopus's exc, rounded to 4 dp, so the
calendar's figure is within ~0.00005 p/kWh of Octopus's published exc (30.7707 × 1.05 → 32.3092;
32.3092 ÷ 1.05 = 30.770667). The block's own stored pair — the one way that could be wrong after a VAT
change — is not read.
- *Rejected:* the schedule's exc where there is one — exact, but a second way, for corrections.

**(d) The prediction follows the settled kWh.** #475 (v4, 24 Sep) was two instances of one site
pricing the same half-hour off-peak and peak: the `completed` dispatch lands 45–75 minutes after the
half-hour closes, and the PASS 2 re-run, on the DCC kWh, re-ran the overlay on whatever it had
captured by then. The defect was **arrival order**; the freeze (`hold_rate`, "the rate stays where it
was") was the right fix for a writer that re-decided on what it happened to know. `settle()` is a
function of the facts, so the order cannot matter — and a freeze would make it read *when* the band
was frozen, which is #475's dependence in a new place. The DCC kWh reaches the band only through the
prediction's two thresholds (the overlay's 0.1 kWh floor, the lifecycle's 0.4 kWh "not materially
run"), which ask a quantity question the DCC figure answers better. A predicted band therefore moves
**at most once before the bill**, when the DCC kWh crosses one of them; the band note says so
("predicted, on the settled kWh"). Only a bill settles a band. (#475's own half-hour — 0.156 kWh, a
`completed` with no `started` — reads peak under the 2c rules either way.)
- *Rejected:* keep #475's freeze until a bill or a new dispatch fact.
- *What it reverses:* CLAUDE.md's invariant "a settled kWh … must never re-band" and v4's changelog
  promise. The invariant is reworded **when 4b ships**, not before (the code freezes until then): a
  settled kWh may change a prediction only through the prediction's own thresholds, and only a bill
  settles a band. 4a's report counts the half-hours where it would move; 4b's changelog says so.

## 5. Order (each its own PR; step 2's §3a safeguards: a backup before staging takes each,
parity seen first, additive first)

| PR | what | writes |
|---|---|---|
| **4a** | `settle()` as a pure function, and a **settle parity report**: over every half-hour, today's stored cost, band, segments and devices beside `settle()`'s, every difference with its reason — run on every instance. It counts what §4 moves: half-hours with legs at two rates (and those with a device drawing), and predicted bands the DCC kWh would move. The guard test of §4 (a) | nothing |
| | *4a as built (25 Oct 2026):* `settle.py` (pure: `Facts` → `Settled`); `engine.settle_facts` reads the facts — the DCC kWh, the tariff, the bill's band and buckets, the prediction on that kWh, the EV (the bill's on IOG-SMB, else the completed dispatch), the cap, the devices from their own reads; `run_settle_parity` → `settle_parity_<ts>.json/.csv` in the share's reports, `POST /api/settlement/settle-parity/start` (`{from?, to?}`), `…/status`, `…/download?file=json\|csv`. A correction is listed, not settled: its rate lives only on the block today, and settle() reads no stored rate | |
| **4a-2** | imported cost as a bill (§6 (e)): the v4 importer writes a bill row for each imported half-hour without one, from its Octopus cost; `settled_band` bands a bill with no label and no buckets by Octopus's cost ÷ its own kWh; the report's CSV carries the stored and settled EV kWh and each device's own read (§6) | the importer's copy only |
| | *4a-2 as built (25 Oct 2026):* importer transform 3c `imported_bills` (spec v27) and its gate; `settled_band(…, cost=, bill_kwh=, own_kwh=)` → `_imported_cost_band`: the cost within £0.0005 of kWh × one of the day's two rates and not the other, else no band (a blend matches neither, a tiny kWh both); passed by the bill apply, `band_note` (the detail panel), `band_parity_row` and `settle_facts`. The CSV gains `stored_ev`, `ev`, and each device's read | |
| **4b** | the bill apply and the PASS 2 re-run call `settle()`; `hold_rate` goes; CLAUDE.md's invariant reworded (§4 (d)); a changelog line | settled and re-run half-hours |
| **4c** | the carve and the reconcile become triggers — the carve's on a completed dispatch that **arrives or changes** (§6: Octopus restates the completed energy, and the carve takes only half-hours with no split yet) | dispatched half-hours |
| **4d** | the finalise seam, the gap blocks, the imports and the sweep | new and imported half-hours |
| **4e** | the removals (~1,700 lines), and the drift repair once `settle` writes devices with the main | — |

## 6. What 4a's reports found (25 Oct 2026)

The settle parity report on staging (23 Oct 2024 – 25 Oct 2026) and dev (5 Jun 2023 – 25 Oct 2026). §4's
counts were zero on both: no half-hour has legs at two rates, so none has a device drawing on one, and
the DCC kWh moves no predicted band. The four decisions change nothing either instance holds.

| | staging | dev |
|---|---|---|
| same | 16,822 | 23,864 |
| differs | 43 | 13,871 |
| empty (no grid import) | 18,227 | 21,659 |
| corrected (listed, not settled) | 21 | — |
| stored £ / settle() £ | 1,589.87 / 1,589.87 | 2,956.80 / 3,432.94 |

**Every difference, by cause:**

| cause | staging | dev | £ | what happens |
|---|---|---|---|---|
| **imported history whose band survives only in its stored cost** — evening smart charges Octopus billed off-peak, imported with no bill row; settle() finds no bill, and with no dispatch record from then the prediction is peak | — | 1,084 + 5 at a negligible kWh (Jun–Oct 2024, Jun–Sep 2023; 13 in Jan 2025, Apr 2025 and Mar–Apr 2026) | +476.10 | **decided (e)** below |
| a published tariff's rate worked back from Octopus's cost (0.089985 for 0.09) — v4's scatter residue | — | 12,720 (Sep 2023 – Jun 2024) | +0.04 in all, ≤ £0.00007 a half-hour | settle() writes the tariff's rate |
| finalise labels a live half-hour's house leg `standard` until its bill relabels it (the older staging copy: every unsettled half-hour on 23 Oct `standard`, every settled one on 20–22 Oct `day`/`off_peak`) | 33 | 41 | 0 | settle()'s label from 4d |
| imported half-hours labelled `standard` where settle() reads `off_peak` at the same rate (Jun 2024, 31 Mar 2026); one Oct 2026 label with its ex-VAT — not yet read against the database | — | 12 + 1 | 0 | the 4a-2 report's columns |
| the importer's bill-split transform (#184) left v4's dispatch-capped EV where the bill's was within 0.0005 kWh (`v4import/transforms.py`, "already the bill's"): of staging's 438 settled IOG-SMB half-hours with an EV bucket, 244 of 244 with the bill > 0.0005 kWh above the dispatch hold the bill, and 8 of 9 within 0.0005 hold the dispatch | 8 | 7 (the same dates) | 0 (one rate) | settle() takes the bill's, when 4d's sweep settles them |
| the carve never re-carves a half-hour that has a split: 24 Oct 04:00 holds 2.67 kWh EV, the completed dispatch now 2.55 (the slot re-captured at 16:20), the charger 2.65 — most likely Octopus's first figure, restated (the log starts after 05:12's carve, so not proved) | 1 | 1 | 0 (one rate) | 4c's carve triggers on a dispatch that arrives **or changes** |
| a battery's grid share 0 against settle()'s 0.0001–0.0003 kWh | 2 | — | < 0.0001 | rounding |

The 13 dev half-hours inside Octopus's two years sit on rate-change dates (1 Jan, 1 Apr, the 2026
agreement's start); not yet read against dev's database — the 4a-2 report's columns should say.

**(e) Imported cost is a bill (decided 25 Oct 2026).** v4's API import wrote Octopus's cost into the
block (`imp_cost`, the rate worked back from it) and **no bill row** (v4 `import_api_history`), so
every v4 database brings history in this shape — and the part of it older than Octopus's two years
when it reaches v5 can never be asked again: its stored cost is the only record of Octopus's band.
The import heard Octopus's cost, so the import writes it as a bill, as step 2c does for v5's own
imports:
1. **the importer** writes a bill row (`measured_cost`: Octopus's cost and ex-VAT, the block's kWh,
   no label) for each `imported_api` half-hour that has none;
2. **`settled_band`**, for a bill with no label and no buckets whose kWh is the block's own, bands it
   by Octopus's cost ÷ that kWh against the day's two rates — Octopus's own price, like a bucket's
   unit rate (§4 (a)'s table: the one kind of rate a band may be read from).

v4 refused cost ÷ kWh in 4.5.7 because the kWh it divided by was CAD and the bill's was settled
("the CAD/settled mismatch (0.004 vs 0.001) sagged cost/kWh into the wrong band"); here both figures
are Octopus's, so the division is exact.
- *Rejected:* `settle_facts` reads an imported block's stored cost where it has no bill — narrower, but
  it breaks §4 (a)'s guarded rule (no stored rate or cost), and the fact stays where only one reader
  knows to look.
- *Dev:* its history imported before #282 is v5's own and past Octopus's two years, so it cannot be
  re-fetched; it reads right only if dev is restored again from a v4 backup through the fixed importer.
  Otherwise its oldest months read the prediction once 4d settles them (dev is dev).
- *The report's gap:* its CSV said a split or a device differed, not by how much — staging's nine
  split rows needed the database. 4a-2 adds the stored and settled EV kWh and each device's own read.
