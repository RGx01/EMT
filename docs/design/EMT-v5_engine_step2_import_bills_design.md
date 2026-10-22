# EMT v5 — Engine step 2: the import writes Octopus's cost as a bill

*Draft, 22 Oct 2026. Step 2 of the engine review (`EMT-v5_engine_review.md` §4.3, §5). Every
statement below was read in the code at `dev` 2a5462b or measured on a staging copy; a docstring
is a claim.*

**What this note decides:** how an imported half-hour gets its cost, band and segments once
Octopus's own answer for it is kept as a fact (a cached bill) instead of being priced by the
import's own rules; what that lets go; how it is checked; and in what order it is built.

---

## 0. Why

**Two routes price the same kind of half-hour.** A half-hour whose bill the drain or the live
pass fetches is settled by `apply_measured_to_block` — Octopus's cost, the band nearest
cost ÷ kWh, the rate snapped to the clean tariff rate, segments, devices re-fitted. A half-hour
the **import** brings is priced by the import itself: `_billed_rate` (cost ÷ kWh unless the
schedule agrees within 0.1p), the cost straight into the block, no `rate_source`, segments left to
the reprice sweep. On staging's copy (support file, 22 Oct 2026): 23,885 imported half-hours, every
one with `rate_source` NULL, 11,813 with segments.

**And the import's answer is the better one.** It asks Octopus 60 days at a time; on 22 Oct 2026 its
costs matched the user's March 2025 statement on all 144 half-hours of 5–7 Mar
(`.build/probe_window_shapes.py`, engine review §2). Yet it is not kept as a bill, so a bill fetched
later — that day, a wrong one — outranked it.

**Around the import's own pricing sits a repair industry** for the half-hours it came back without
a cost for: an in-pass recovery, the reprice queue, `repair_import_pricing` (328 lines), the deferred
verify pass (262 lines, re-checking the whole imported history in 30-day chunks), the pricing-health
panel and its demo payloads — about 1,090 lines of `engine.py`, three routes and a card on Historical
Import. Their premise is partly disproved: the verify pass exists because "the bulk import mislabels
the edges of a smart charge", and the bulk import is what matched the statement.

---

## 1. What the import does today (read in the code)

Per channel, newest first, in 60-day chunks (`import_api_history`, `engine.py` 7906–8385):

1. **Fetch** Octopus's measurements (cost + label per half-hour); for export, fill what REST
   consumption returns where measurements has nothing.
2. **Price each half-hour:** `meas_cost` if Octopus gave one, else kWh × the schedule's rate;
   `rate` by `_billed_rate`; ex-VAT from the cost or the schedule; the day's standing charge from the
   schedule (else measurements, extrapolated to a day).
3. **Record coverage** per agreement (`import_cost_coverage`): slots, costed, banded — the finding
   "Octopus never costs this product" (three INTELLI agreements, 23 months, on one account).
4. **The no-cost half-hours:** an in-pass re-fetch (`recover_measurement_costs`), then whatever is
   left goes on the **reprice queue** and the **import's no-cost record** (`note_import_cost`).
5. **Write** the chunk in one transaction (`upsert_imported_blocks`).

Then the job's finish: the queue drained by `repair_import_pricing`; the **deferred verify pass**
over the whole imported span; the **reprice sweep** (segments); the **history drain** asks bills for
every Intelligent window's half-hours; the device rebuild.

---

## 2. The change

**(a) The import keeps Octopus's answer as a bill.** Every import half-hour Octopus costed is also
written to `measured_cost`: `cost_incl`, `cost_excl`, the carrying bucket's `label`, `kwh`,
`fetched_at` = now. In the chunk's own transaction, through a new bulk method — `upsert_measured_cost`
commits per row, which on the Pi's SD card would be ~35,000 commits for two years.

- **Never over a breakdown.** A half-hour with an IOG-SMB breakdown cached keeps it: the bulk answer
  is cost-only (engine review §2: the breakdown is window-insensitive, and the bulk feed has no
  split). The import skips those slots' bill rows.
- **A newer answer replaces an older one** from the same source otherwise — a re-import refreshes the
  cache (22 Oct 2026: a re-import's correct 7p lost to a cached 25.4p).

**(b) An Intelligent half-hour is settled from its bill, the one way.** The import writes the block's
kWh, standing charge and a provisional cost; `apply_measured_to_block` then settles it, as it does
every other billed half-hour (band, clean rate, segments, devices). Run **by the import job, in
pages** after each chunk's write — coordinator-admitted, yielding between pages — not left to the
hourly pass, which would meet ~35,000 unapplied bills at once.

**(c) A published-rate half-hour is unchanged.** The apply skips published tariffs
(`tariff_kind_at`); DCC kWh × rate is their bill, and the import's cost already is that.

**(d) A half-hour with no cost is a half-hour with no bill yet.** It stays on the import's no-cost
record. Inside an Intelligent agreement, within Octopus's two years, the history drain asks Octopus
for it by the local day (#269, `_drain_asks`), backing off as any unanswered bill does (hourly 2
days, daily to 30, then given up); on a published tariff it is priced kWh × rate, which is its bill. The in-pass recovery, the reprice
queue, `repair_import_pricing` and the deferred verify pass have nothing left to do.

**(e) What goes:** `_billed_rate`, `_reprice_suspect`, `_import_pricing_flag`, the in-pass recovery,
`repair_import_pricing`, `_drain_reprice_queue_after_import`, the verify pass and its six helpers,
the import health summary, `api_import_health` and its demo, `debug_mode`; the store's reprice-queue
methods and `reprice_imported_block` (only the repair calls it); the routes
`/api/historical/reprice-repair`, `/api/historical/api-import/verify-status`,
`/api/historical/api-import/health`; Historical Import's pricing-health card, its "Retry N" and the
queued-slots CSV; the gap fill's calls into the repair and the verify. About 1,100 lines of
`engine.py` and a third of Historical Import's script.

**(f) What stays:** per-agreement coverage (`import_cost_coverage`, read by the CSV apply's replace
rule and the import's own skip of known-uncostable spans) — it can later be derived from the bills;
the import's no-cost record (step 3 merges it with the other ask records); the reprice sweep (step 4
folds it into `settle()`).

---

## 3. Checks before and while building

1. **Parity on a real database.** For the imported Intelligent half-hours of a staging copy, the
   block's cost, rate, band and segments after (b) against today's. The cost must be identical (both
   are Octopus's); the rate and band may move only where `_billed_rate` and the band snap disagree —
   each such half-hour listed and explained before (b) ships. Needs the tariff schedules, so it runs
   on staging (a soak step), not offline.
2. **Throughput of the paged apply** on the Pi and the Wyse: half-hours per second, the loop's lag
   while it runs; the page size set from that.
3. **Growth:** `measured_cost` rows for two years of import, and the database's size.
4. **The heatmap.** An imported half-hour reads `applied` once settled from its bill (green); a no-cost
   one stays "no cost from Octopus, asking" until the drain answers or gives up.

---

## 4. Order

| PR | what | changes behaviour |
|---|---|---|
| 2a | the bulk bill write; the import writes bill rows (skipping breakdown slots) | the hourly apply would start settling imported half-hours — so 2a lands with 2b |
| 2b | the import job applies its bills in pages; parity (§3.1) and throughput (§3.2) on staging | yes: imported Intelligent half-hours settled from their bill |
| 2c | the in-pass recovery, the queue, the repair and the verify pass go; their routes and the pricing-health card | yes: no second pass over imported history |
| 2d | the gap fill's finish follows (it called the repair and the verify) | yes |

---

## 5. For the user

- (a) **Export.** The import gets Octopus's export cost too. Export is settled today as kWh × the
  export rate, which Octopus's figure equals on a flat export tariff. Keep export as it is for now
  (proposed), or keep its answers as bills as well?
- (b) **"Fix a date range"** (`/api/historical/reprice-repair`) goes with the repair. Its job — ask
  Octopus again for a span — is the heatmap's re-fetch now, which since #269 asks the right window.
  Agreed?
- (c) **Historical Import's pricing-health card** goes. Its per-half-hour story is on the heatmap
  (no-cost half-hours hatched, asked until answered or given up). Keep a one-line note in the
  import's summary instead — "N half-hours came back without a cost; EMT is asking Octopus for them"?

**Decided alongside (22 Oct 2026, the user):** of the two per-version backups at start-up
(engine review §3.5), only the raw pre-migration copy is kept; the second, a zip after the store
opens, goes — its own small PR.
