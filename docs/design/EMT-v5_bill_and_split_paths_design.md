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

*Built 16 Oct 2026, refined:* `emt_ev_reference(segments, cached bill)` = the **larger** of the two,
read for all four paths by `emt_ev_at` (and by `_refetch_diff` from its facts). "The cached bill is the
thing being judged" holds for the live pass and the drain, where no bill is cached yet — there the
reference is the segments, as before. In the VAT resync and the re-fetch the thing judged is
Octopus's *new* answer and the cached bill its *earlier* one: an earlier answer with the car and a
later one without is the 10 Oct 2026 provisional answer the other way round, and segments alone would
let it through wherever EMT's split is the smaller (the dispatch cap, until PR 5). Behaviour change:
the VAT resync now also holds an answer dropping the car where EMT's split has it and the cached bill
had no breakdown.

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
| 3 | §1.3 3: `fetch_bills` with the ladder (+ day, + month) | the probe's eight half-hours as a fixture; real-DB parity on the re-fetch. *Built 16 Oct 2026:* the rungs live in `KrakenAPIClient.recover_measurement_costs` itself (`wide_rungs`, on by default) — the one billed-cost fetch every caller already shares (`_fetch_bill_nodes`, the API import's in-pass recovery, `repair_import_pricing`), so no `fetch_bills` wrapper was needed. After the −30 min…+2 h / +3 h ladder: one query per day with a half-hour still missing, then one per month, each [start − 30 min, end + 3 h) so the OFF_PEAK label is in view. The breakdown fetch is unchanged (no stripping seen there). |
| 4 | §2: one `emt_ev_at` | the 4.5.23 tests (`test_bill_split_wait.py`) on every path. *Built 16 Oct 2026* (§2's note): `emt_ev_reference`, `emt_ev_at`, `BlockStore.get_bill_ev_kwh`. |
| 5 | §3.3 1, 3, 4: `ev_for`, one builder, one capped test | goldens; real-DB parity on `blocks-35` (before) / `blocks-36` (after the re-import). *Built 16 Oct 2026, without the builder* (it moves to its own PR, 5b — the four builds differ in leg rates, ex-VAT and bands): the rule lives in `_measured_ev_kwh` — on an IOG-SMB agreement (`split_at`) with a bill split cached, the bill's EV clipped to kWh; otherwise the completed dispatch. `dispatch_ev_kwh` is the one dispatch computation (`_iog_slot_ev_kwh` deleted), `_capped_schedules` the one schedule check (four copies). `BlockStore.dispatch_coverage` deleted: the "unknown ceiling" branch it served is gone. Parity on `blocks-35`: of 1,115 settled half-hours with a bill split, 226 on the agreement take a different EV (net +22.72 kWh; the estimate was 231 / +22.65) and 1 before it (24 Aug 05:00 UTC takes back its 0.011 kWh). **These reach stored data only with PR 6** — the rule changes new settles; nothing re-selects a half-hour already settled with an EV split. Not done: capped pricing judged per half-hour by its agreement (pre-agreement history repriced by the seam on an SMB account) — a pricing change, for its own note: `EMT-v5_capped_pricing_by_agreement_design.md` (draft, 16 Oct 2026). |
| 5b | §3.3 3: one EV + house builder | *Built 16 Oct 2026:* `pricing_segments.ev_house_at`, for the carve, the settle's single-band fallback, BL-58b and the revert/restore verdict; equal to the hand-written form on 2,000 random cases, except that a remainder rounding to nothing gets no segment. |
| 6 | §3.3 2: one population; a re-import takes the bill's split | the 15 Oct re-import: the five SMB half-hours take the bill's EV. *Built 16 Oct 2026, for the final stage:* the split heal's population is every settled block whose split is not the settle's rule (`get_settled_blocks_off_their_split`, confirmed against `_measured_ev_kwh`), not only those with no split; it never takes EMT's car away entirely (the 4.5.23 case). Replayed on `blocks-35`: 227 blocks moved in one pass (226 on the agreement, +22.72 kWh EV; 24 Aug 05:00 UTC +0.011), a second pass none; 16 costs moved onto their bill by 1–2 millionths of a pound (BL-82-era rounding, net −£0.000028); no rate moved. The 15 Oct re-import's five were very likely a snapshot taken before the tick re-applied the cached bills (`blocks-36` was saved two minutes after the import). **6b**, the carve and the attribution as one population for the stages below the final, is its own PR (re-pricing involved). *6b built 16 Oct 2026:* `get_blocks_off_dispatch_split` (no EV split, or a gap-filled split off its dispatch — BL-39; not corrected; kWh and a completed dispatch) replaces the carve's and the attribution's queries; `run_iog_split_carve` is the one writer (EV at the block's own rate, cost-neutral; `carve_ev_split` for a blank, `replace_ev_split` for BL-39) and skips the final stage (settled, `split_at`, bill split cached); `_attribute_missing_ev_split` and its call in the reconcile removed. Changes: a settled block below the final stage (split pending, or before the agreement) takes the dispatch's split — no job did on a capped account; imported history and capped accounts are in the one population (the carve already took both; the attribution took neither). On the soak (`blocks-35`) it selects one block: 24 Aug 05:00 UTC, +0.011 kWh. |
| 7 | §6 (c): the API import stores its cost as a bill; its recovery through `fetch_bills` — **not built** (§6 (c), narrowed); instead the bill apply is gated on Intelligent half-hours | a re-import of the soak's August: every half-hour gains a bill row, no cost moves; published-rate history keeps its rate. **And:** on an IOG-SMB agreement a cost-only row must not stop the breakdown being asked — the drain (and the live pass) must still ask a half-hour whose cached bill has no breakdown, or its split stays the dispatch's for good (§6, final, split pending) |

## 6. Decided (16 Oct 2026, the user)

**The tower.** A half-hour's figures settle in three stages, each replacing the one below:

| stage | kWh | cost | House/EV split | Import bill view |
|---|---|---|---|---|
| prediction | local HA readings (CAD) | kWh × rate | the dispatch | amber |
| intermediate | DCC | kWh × rate | the dispatch | light amber |
| final, split pending | DCC | Octopus's | the dispatch, **until Octopus's split arrives** | teal |
| final | DCC | Octopus's | Octopus's | green |

*(The split-pending stage made explicit by the user, 16 Oct 2026: on IOG-SMB the split can arrive
after the cost — the third settlement event, ROADMAP BL-82's model. It needs the split to be asked
for again: a bill cached with no breakdown on the agreement is never re-asked, so it must not be
written there — PR 2 removed the re-fetch's cost-only fallback for that reason, and PR 7 must not
reintroduce it.)*

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

   **Narrowed, 16 Oct 2026 (the user): 1 is not built; 2 stands as done.** Checked before building:
   a `measured_cost` row for each imported half-hour would be read by `apply_measured_settled`,
   which applies every cached bill to any settleable block from the floor and had no tariff check —
   so an Agile / Go / Cosy / flat user's imported half-hours inside their current agreement would
   have been settled with the two-rate snap, and an Intelligent user's imported history re-settled
   at runtime (a history rewrite). And the drain asks only half-hours with no cached bill
   (`NOT EXISTS measured_cost`), so a cost-only row on an imported IOG-SMB half-hour would stop its
   split ever being asked. All fixable, but the gain — the bill views reading one table, and one
   line of `settlement_state` ("an API import counts as Octopus's cost") gone — is small; the
   import keeps its cost in the block. 2 is in place already: since PR 3 the import's own recovery
   and the reprice queue go through `recover_measurement_costs` and its day / month rungs (only
   `repair_import_pricing`'s ±1-day windows remain its own). **Done instead:**
   `apply_measured_settled` takes a cached bill on an Intelligent half-hour only
   (`tariff_kind_at`), the net under any bill that reaches the cache on a published-rate account.

---

## 7. Where the rule meets stored data — the importer, not a runtime heal (16 Oct 2026, the user)

**The question.** #180 changed the settle's rule (the bill's split at the final stage) and #182
widened the split heal so every settled block is brought to it. Its first run rewrites history —
227 half-hours on the soak — and it would do the same, at runtime and silently, to every v4 user's
IOG-SMB history after import (v4 settled it under 4.5.9's dispatch cap). The heal is not a v4-style
heal (no marker, no floor), but its effect is one, and the working rule is that v5 has no heals of
its own. Decided: **stored data meets the rule in the importer**, where the change is a counted,
reported transform of the importer's own copy.

**Where stale splits come from.** After #180 a new settle is on the rule from the start. Stale ones
are v4-imported history (the real users at release) and this project's own dev installs settled
before #180 (they re-import from a v4 backup).

### 7.1 Importer transform — the bill's split from the cached bill (E design §4, transform 3b)

After 1 (provenance: `measured` → `settled`) and 3 (held-rate refit), for every main block that is:

- settled, not corrected, with kWh;
- in the IOG-SMB era — from the earliest four-bucket `measured_cost` row (E design's rule; the
  importer has no agreements to ask), so a pre-agreement half-hour keeps the dispatch's split;
- with a bill split cached (the latest CONSUMPTION row states `ev_kwh`);
- whose EV segments differ from the bill's EV clipped to the meter by more than 0.0005 kWh;
- **and whose segments all carry one rate** —

its segments are rewritten at that rate (`pricing_segments.ev_house_at`): EV = the bill's EV
clipped to kWh, the house the remainder, bands as they were. One rate means Σ kWh × rate is
unchanged, so the cost, the rate and every total stay as v4 left them; only the House/EV
attribution moves. Recorded in `v4i_resegmented` (`why = "bill_split"`), so verify checks them by
their own gate.

**Not moved, reported:** a block whose segments carry more than one rate (a capped boundary, EV and
house on different bands): moving kWh would change the cost, and only the runtime settle can
rebuild it from the rate schedules. And the 4.5.23 case: a bill with no EV where EMT's split has the
car is not applied. Both are listed in the import report.

**Measured on the soak's v4 file** (`emt_first_run/blocks.db`, written by 4.5.23, 14 Oct): of 991
settled IOG-SMB half-hours with a bill split, 169 differ — all one-rate, none mixed; EV +12.91 kWh,
the same off the house; no cost moves. (blocks-35, a day later on v5, had 226.)

### 7.2 Half-hours with no cached split — the user's re-fetch

87 settled half-hours on the soak's agreement have a bill cached **without** a breakdown — fetched
28 Aug – 9 Sep, before v4 4.5.7 (11 Sep) fetched the four buckets. Octopus serves the buckets for
them now (the 8 Sep re-fetch, 14 Oct: 10/10 with the car), but nothing asks: a cached bill is not
re-asked. They read teal ("Octopus's cost, EMT's split") — the tower's split-pending stage. Decided:
**the user re-fetches them when they see teal**; the range re-fetch lists a split arriving on a
cost-only bill as a difference (#162) and Apply settles it from the bill (#180's rule). No automatic
re-ask.


*Reversed 18 Oct 2026 (the user):* the recovery asks them. On IOG-SMB, a half-hour the car drew in
(a completed dispatch) whose latest cached bill is cost-only is asked its breakdown by the history
drain (`BlockStore.get_cost_only_split_slots`); an answer is stored and the half-hour re-settled
from it (`apply_measured_to_block`), so its split is the bill's; a breakdown with no EV where EMT
had the car is held back (4.5.23); unanswered, it is asked for up to 30 days (`split_ask`). 69 on
the soak's `blocks-38`.

### 7.3 The runtime split heal — removed

With 7.1 at import and #180 at every settle, `run_settled_ev_split_heal` has nothing left that is
not history: a settle on the rule never writes a split off it, a re-fetch's Apply and the VAT resync
settle through `apply_measured_to_block` themselves. It goes, with #182's query
(`get_settled_blocks_off_their_split`) and its call after a re-fetch's Apply — so nothing at runtime
rewrites stored splits. (Its v4-era purpose, blocks settled house-only under the "unknown ceiling",
is also covered by 7.1: a block with no EV split and a bill EV is "off the bill".)

### 7.4 Order

| PR | contents | check |
|---|---|---|
| 8 | the importer transform 3b (7.1), its verify gate, its report lines; the E design and import spec updated | the soak's v4 file: 169 moved, 0 reported, no cost or rate moves; a fixture with a mixed block, reported not moved. *Built 16 Oct 2026:* `v4import.transforms.bill_split`, gate `verify.bill_split`, `split_reproduced` exempting moved blocks; spec v22. "One rate" is within 0.001p/kWh — v4's house leg was the residual absorber (30 of the 169 have it), and keeps that role. Measured as designed: 169 moved, 0 reported, +12.91 kWh, `imp_cost` total identical, no rate changed. |
| 9 | 7.3: the runtime split heal removed | the suite; the soak after an upgrade logs no heal. *Built 16 Oct 2026:* `run_settled_ev_split_heal`, its job (`settled_ev_split_heal`), its two calls (the dispatch tick, a re-fetch's Apply) and `get_settled_blocks_off_their_split` removed; the job-guard, membership and write-scope tools and the B design's figures updated (10 live jobs, 16 members, 9 of 10 write `blocks`); `tests/test_v4_heals_removed.py` now records the removal as deliberate. |

