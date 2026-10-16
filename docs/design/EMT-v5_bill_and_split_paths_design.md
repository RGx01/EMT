# EMT v5 — One bill reader, one split rule (consolidation)

*Draft, 16 Oct 2026; §6's three questions decided the same day. From the PR 5 Pi check: the range re-fetch (BL-86) added a fifth way to ask
Octopus for a bill, and the next two fixes on the table (a wider cost window; a re-import that keeps
the bill's split) would each have added another copy. The user asked for the duplication first.
Every line below was read in the code; nothing is taken from a docstring.*

**What this note does:** names the parallel implementations, shows where they already disagree,
and sets out a consolidation in families — one family per PR, each checked against the tariff
goldens (`tests/test_tariff_goldens.py`) and real-database parity (`.build/real_db_parity.py`),
as step 5 was. No stored shape changes.

---

## 1. Asking Octopus for a half-hour's bill

### 1.1 The paths today

| path | what it asks | window | breakdown or cost-only decided by | parses with | writes |
|---|---|---|---|---|---|
| live settled-cost pass, `measure_settled_dispatched_blocks` (`engine.py` 11613) | breakdown (SMB) / billed cost | `recover_device_breakdown` ±30 min chunks; `recover_measurement_costs` | `_import_is_smb_capped()` — the account's **current** tariff | its own inline code (~11669, ~11725) | `upsert_measured_cost` / `_breakdown`; applied later |
| history drain, `run_measured_history_drain` (12313) | breakdown only | as above | runs only if `_import_is_smb_capped()` | its own inline copy (~12375) | upsert, then `apply_measured_settled` |
| range re-fetch, `_refetch_octopus` (11926) | breakdown where `split_at`, else billed cost; billed cost for what the breakdown leaves | as above | **`split_at(start)` — the half-hour's own agreement** | `_vat_resync_row` | upsert, `apply_measured_to_block`, split heal |
| VAT resync (#505), `run_measured_vat_resync` (12199) | breakdown (SMB) / billed cost | as above | `_import_is_smb_capped()` | `_vat_resync_row` | upsert, `apply_measured_to_block` where the £ differs |
| API import, `import_api_history` (8088) | measurements (cost + label) | `chunk_days` (60) | — (no breakdown) | `_parse_measurement_node`, rate by `_billed_rate` | `imp_cost` straight into the block; no `measured_cost` row |
| import cost repair, `repair_import_pricing` (8579) | measurements, then `recover_measurement_costs` | groups ≤ 7 days **padded ±1 day**, then −30 min…+2/3 h | — | `_billed_rate` (three call sites) | `reprice_imported_block` |

### 1.2 Where they disagree

- **The breakdown is parsed three times** (~11669-11716, ~12375-12394, `_vat_resync_row` ~11849-11861):
  the same cost / ex-VAT / band → label / kWh code. They **disagree on a zero answer** — the live pass
  caches it (`label="ZERO"`), the drain skips it without caching, the re-fetch and resync drop it. The
  cost-only branch is duplicated too (~11730-11737 vs ~11864-11867). `_vat_resync_row`'s docstring
  says "the same parsing as measure_settled_dispatched_blocks"; it is a copy, not a share.
- **Breakdown or cost-only is decided three ways:** the current tariff (live pass, drain, resync),
  the half-hour's agreement (re-fetch, `split_at`), and whether a rate schedule happens to be loaded
  (`apply_measured_to_block` ~12724, `_attribute_missing_ev_split` ~12882) — the test
  `_import_is_smb_capped`'s own docstring says can go false during a refresh. A pre-agreement
  half-hour gets the breakdown from the VAT resync and the billed cost from the re-fetch: the
  24 Aug 2026 problem (the breakdown said £0.5169 at the day rate where the bill said £0.0879
  off-peak), fixed on one path only (#170).
- **The window for the same OFF_PEAK-label problem** is −30 min…+2/3 h in `recover_measurement_costs`
  and ±1 day in the repair. Measured on the Pi, 15 Oct: small windows came back stripped (kWh, no
  cost) for the same half-hour a tight or whole-day window costed (`.build/probe_measurement_costs.py`);
  a whole-month import query costed all 1,488 half-hours of August.
- **The scope preamble is copied** (IOG/INTELLI + MPAN at ~11624, ~12204 and `bill_stage` ~11560).

### 1.3 Proposal — one bill reader

`bill_source.py` (or a section of `engine.py`), used by every path:

1. **`source_for(start)`** → `breakdown` | `cost` — one rule, the half-hour's own agreement
   (`split_at`). The live pass, drain and resync stop reading the current tariff.
2. **`parse_bill(node, kind)`** → the `measured_cost` row (or `None`, or `ZERO`) — one parser, one
   zero rule: cache a zero answer on a zero-kWh half-hour (the live pass's rule; it keeps the row out
   of the split heal by leaving the breakdown NULL), drop it otherwise.
3. **`fetch_bills(starts)`** → `{start: row}` — groups by source, asks the breakdown for its share,
   the billed cost for the rest and for what the breakdown leaves; the window ladder lives here once:
   −30 min…+2 h, +3 h, then the half-hour's day, then the month around it (§1.2's measurements). The
   wider-window fix is this one function.
4. Paths keep their own **scope** (which half-hours) and their own **apply** (when to settle); they
   stop owning the fetch and the parse.

The API import keeps its bulk measurements fetch (it is the first read, not a recovery), but its
missing-cost recovery and the repair use `fetch_bills`' ladder.

---

## 2. The 4.5.23 hold-back

`_bill_drops_ev(emt_ev, bill_ev)` is one function, but "EMT's EV" is read three ways: the segments
(live pass ~11645, drain ~12363), the cached breakdown's EV (resync ~12250), and the larger of the
two (re-fetch ~12037). The same half-hour can be held on one path and taken on another.

**Proposal:** one `emt_ev_at(start)` — the EV segments (what EMT shows, and what the user would lose);
the cached bill is the thing being judged, not the reference. Used by all four.

---

## 3. Deciding and writing the House/EV split

### 3.1 The writers today

| writer | source of the EV kWh | scope | writes with |
|---|---|---|---|
| `apply_measured_to_block` → `_measured_ev_kwh` (12577) | completed dispatch as a ceiling; bill EV preferred, then stored, then dispatch; bill EV when the ceiling is unknown | a settled bill | `apply_measured_to_main` (its own two-segment build ~12754, `compute_iog_split` inline ~12735) |
| `run_settled_ev_split_heal` (12431) | re-asks `_measured_ev_kwh` | SMB; settled; no split; cached `ev_kwh > 0` | `apply_measured_to_block` again |
| carve, `run_iog_split_carve` (7208) | `_iog_slot_ev_kwh` (611): the snapped completed dispatch, grid-clipped | `get_blocks_awaiting_ev_carve` (block_store 8299): no split, **not** settled/corrected, completed dispatch; **imported included**, no capped gate | `carve_ev_split` (its own two-segment build ~7263) |
| `_attribute_missing_ev_split` (12863) via `reconcile_dispatch_overlay` | `_apply_iog_split` → `_iog_slot_ev_kwh` | `get_blocks_missing_ev_split` (8167): completed dispatch, NULL split or a gap-filled block off its dispatch; **imported excluded**, no `rate_source` filter; uncapped only (schedule presence) | `replace_ev_split` |
| `reconcile_dispatch_overlay` BL-58b / revert | keeps the stored EV kWh, re-prices it | dispatched blocks | its own two-segment builds (~13140, ~13240) |
| `_reprice_history_block` (7283) | `_apply_iog_split` (dispatch) | `_NEEDS_REPRICE_WHERE` — **includes imported** blocks with no segments | `set_blocks_iog_split` |
| API import, gap fill | — writes no EV segments | | |

### 3.2 Where they disagree

- **The carve and the attribution select the same half-hours** (completed dispatch, no split) from two
  queries, with opposite scope: the carve takes imported history and skips settled blocks; the
  attribution skips imported history and takes settled ones. On an uncapped install both run hourly.
- **The dispatch EV figure is computed twice**: `_iog_slot_ev_kwh` (the snapped slot, through
  `_reader()`) and `completed_dispatch_kwh` + min in `_measured_ev_kwh` (the exact slot).
- **"Is this capped?" is tested three ways** (~717, ~12724, ~12882).
- **The two-segment EV / house build at one rate is written four times** (~7263, ~12754, ~13140,
  ~13240).
- **After a re-import the split comes from the dispatch, not the bill** (measured on the Pi, 15 Oct:
  August deleted and re-imported — cost, kWh, rate identical on all 1,488 half-hours; five half-hours on
  the IOG-SMB agreement now carry the completed dispatch's EV, e.g. 3.46 kWh where the cached bill says
  3.409). The import writes no EV; `_reprice_history_block` (log: "swept 669 block(s)") rebuilt it from
  the dispatch; nothing re-applies the cached bill's split to an imported block. The Split view still
  reads "from the bill", from the cached row.

### 3.3 Proposal — one split rule

1. **`ev_for(start)`** → `(kwh, source)` — one rule for where a half-hour's EV comes from:
   the bill's breakdown where `split_at(start)` and a bill split is cached (held back by §2's rule);
   otherwise the completed dispatch, snapped and grid-clipped (one computation); otherwise none.
2. **One population** — "blocks whose stored EV is not `ev_for`'s" — replacing the carve's and the
   attribution's two queries; imported history included (a re-import then takes the bill's split, which
   is §3.2's last case), corrections excluded.
3. **One two-segment builder** for EV + house at a rate, used by the four sites.
4. **One capped test** — `split_at` for the split, the tariff kind for pricing (settlement surfaces
   design §9.1).

---

## 4. A live bug, fixed first and on its own

**`reprice_imported_block` (block_store 3386) overwrites a settled or corrected imported half-hour.**
The import's cost repair (`repair_import_pricing`, from the reprice queue and from the deferred verify
pass with `suspect_only`) picks imported blocks by `source LIKE 'imported%'` alone
(`get_imported_block_pricing`, the queue); neither it nor `reprice_imported_block` checks
`rate_source` or `rate_corrected`. The UPDATE writes when the rate or cost differs, and then deletes
the block's import segments so the reprice sweep rebuilds them from the column. So on an imported
half-hour settled from a bill whose rate is not cost ÷ kWh (a mixed or capped IOG-SMB half-hour), or
one carrying the user's correction, a verify pass replaces the bill or the correction with the
measurements' cost and `_billed_rate`, and the bill's split is rebuilt from the dispatch — or lost,
on imported history with no dispatch record. **Fix:** the UPDATE leaves `rate_source IN
('settled','corrected')` and `rate_corrected = 1` alone (the one write point, so every caller is
covered).

---

## 5. Order

| PR | family | parity |
|---|---|---|
| 1 | §4: the repair leaves settled and corrected half-hours alone | a test per case; no golden moves |
| 2 | §1.3 1–2: `source_for`, `parse_bill` — every path parses through one function | goldens; replay the four paths' fixtures; the zero rule's one behaviour change named. *Built 16 Oct 2026:* `bill_kind_at` (the name for `source_for`), `parse_bill` + `_zero_answer`, `_fetch_bill_nodes`, `_store_bill`, `BlockStore.get_imp_kwh`; `_vat_resync_row` and the two inline copies deleted. Behaviour changes: the source is per half-hour in all four paths; a breakdown half-hour the breakdown leaves unanswered is no longer asked the billed cost (#167's fallback cached a split-less bill that froze); the zero rule applies to cost-only answers and the VAT resync too. |
| 3 | §1.3 3: `fetch_bills` with the ladder (+ day, + month) | the probe's eight half-hours as a fixture; real-DB parity on the re-fetch |
| 4 | §2: one `emt_ev_at` | the 4.5.23 tests (`test_bill_split_wait.py`) on every path |
| 5 | §3.3 1, 3, 4: `ev_for`, one builder, one capped test | goldens; real-DB parity on `blocks-35` (before) / `blocks-36` (after the re-import) |
| 6 | §3.3 2: one population; a re-import takes the bill's split | the 15 Oct re-import: the five SMB half-hours take the bill's EV |
| 7 | §6 (c): the API import stores its cost as a bill; its recovery through `fetch_bills` | a re-import of the soak's August: every half-hour gains a bill row, no cost moves; published-rate history keeps its rate |

## 6. Decided (16 Oct 2026, the user)

**The tower.** A half-hour's figures settle in three stages, each replacing the one below:

| stage | kWh | House/EV split | Import bill view |
|---|---|---|---|
| prediction | local HA readings (CAD) | the dispatch | amber |
| intermediate | DCC | the dispatch | light amber |
| final | DCC | Octopus's cost **and split** | green (teal while the split is still the dispatch's) |

a. **One zero rule, in the shared parser.** Octopus answers zero and EMT holds zero kWh → cache it as
   ZERO (the bill confirms the half-hour; the breakdown stays NULL, so the split heal leaves it).
   Octopus answers zero where EMT holds kWh → not an answer: not cached, asked again, logged once as a
   kWh disagreement. On the soak all 90 cached ZERO rows (1 Aug – 14 Oct) are on zero-kWh half-hours;
   no disagreeing case has occurred. No path changes behaviour — the drain and the re-fetch only ever
   ask half-hours with kWh, so they already followed it.
b. **At the final stage the bill's split wins.** `ev_for` = the bill's EV bucket, clipped to the
   meter's kWh, where a bill split is cached; the dispatch only below the final stage. This replaces
   v4 4.5.9's rule in `_measured_ev_kwh` (EV = min(bill, dispatch, meter): the dispatch capped the
   bill, against a battery charging concurrently inflating the EV bucket). Measured on the soak, 26 Aug
   – 15 Oct: of 382 half-hours with both figures, the bill's EV was higher in 220, the dispatch in 149
   (by up to 2.2 kWh; 14 Oct 04:30 BST the dispatch said 3.02 kWh where the meter imported 0.809).
   Letting the bill win changes 231 half-hours: EV 914.5 → 937.1 kWh (+22.65, of which +23.18 where
   the dispatch capped the bill, −0.53 where the stored split was the dispatch's), the same off the
   house; the total cost is unchanged.

c. **The API import keeps its bulk fetch, as the first read.** Measured on the Pi, 15 Oct: the
   month-wide measurements query costed all 1,488 half-hours of August where EMT's small recovery
   windows came back stripped for the same half-hours; it is also the cheapest (one query per 60 days
   against two to four per half-hour). Two changes so it is not a separate world:
   1. **Its cost is stored as a bill** — a `measured_cost` row per half-hour, as every other path —
      not only in the block. The bill views then read one table, and `settlement_state`'s special case
      ("an API import counts as Octopus's cost") goes: an imported half-hour reaches the final stage
      for its cost like any other. **A stored bill is applied only on an Intelligent half-hour**
      (settlement surfaces design §9.2): applying snaps the rate to the day's lowest or highest band, a
      two-rate model, wrong for Agile / Go / Cosy history, which keeps the import's own rate
      (`_billed_rate`).
   2. **Its missing-cost recovery and the reprice queue use `fetch_bills`** (§1.3 3) and its ladder.

   The split stays with the drain: the bulk measurements carry none, and the device breakdown is
   asked in small chunks only.
