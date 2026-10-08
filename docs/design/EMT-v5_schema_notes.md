# EMT v5 — schema shape: what goes, what stays, and what was rejected

*A decision record for the `blocks` table. The drop list itself lives in the import spec (§5.2);
this document holds the reasoning, the measurements, and the alternative that was considered and
rejected — so it is not re-proposed in six months.*

**Status:** decided 18 Sep 2026. Measured against a real 4.5.13 production database
(136,202 rows, 45 columns, 78 MB).

---

## 1. The decision

**Keep the wide, one-row-per-block shape. Drop 12 columns. Fix the channel bug at the flag.**

---

## 2. Why the columns go — correctness, not space

This is the part most likely to be misremembered, so it is stated first: **columns are removed
because derived values drift from their source, not to save bytes.**

Every column on the drop list is reproducible from `block_segments`, which v5 carries verbatim. A
value stored in two places eventually disagrees with itself, and in v4 that produced a recurring bug
class of real cost:

> **One column came OFF this list on 24 Sep 2026.** `imp_kwh_grid` was listed as derivable from
> segments. It is not, on the meters that carry it: sub-meters have **zero** rows in
> `block_segments`, and 70,497 sub-meter blocks set `imp_kwh_grid`. The reasoning below is unchanged
> and correct — a value stored in two places drifts — but "reconstructable from segments" is a
> property of a column **on a meter that has segments**, and that qualifier was missing. See import
> spec §5.2 and C §3.

- a rate divided back out of a billed cost landed a whisker off the published band, so the rate line
  plotted peak across half-hours the bill had charged off-peak;
- an EV split stored on the block went stale against the segments that actually priced it;
- ex-VAT figures were recomputed differently in two places.

None of those were storage problems. They were two-sources-of-truth problems, and the fix is to keep
one source.

**The drop list: ~~15~~ ~~14~~ 12 columns.** The ~~13~~ ~~12~~ **11** in import spec §5.2, plus:

| extra | why |
|---|---|
| `rate_reconciled` | A second provenance flag beside `rate_source`. Fold into the provenance model. |
| ~~`imp_provisional`~~ | ~~Duplicate of `is_provisional`~~ — **wrong, and kept (5 Oct 2026).** `imp_provisional` came OFF the drop list on 5 Oct 2026: it is not a duplicate of `is_provisional`. It marks a SUB-METER block written without its post-boundary read (v4 2.10.0); `is_provisional` marks a MAIN block not yet DCC-settled (3.0.0). `_amend_provisional_sub_meter_blocks` finds and corrects the former. They looked alike because both are 0 on every row measured — data is not proof. |

---

## 3. The rejected alternative: normalise to one row per channel

**Considered:** replace the wide row (`imp_*` and `exp_*` side by side) with one row per
*(block, channel)*, which would retire roughly seven mostly-empty columns by normalising rather than
deleting. The apparent case: `exp_kwh` is 72% NULL, `exp_read_start/end` 93%, `imp_read_start/end`
79%, `imp_kwh_api`/`exp_kwh_api` 93%.

**Rejected.** Measured, by rebuilding the table three ways at `page_size=4096`:

| layout | rows | bytes | vs wide |
|---|---|---|---|
| wide, all 45 columns | 136,202 | 28,549,120 | — |
| wide, 15 dropped | 136,202 | 24,477,696 | **−14.3%** |
| long, one row per channel | 174,402 | 26,943,488 | −5.6% |

Normalising saves **5.6%** while adding **28% more rows**. Simply dropping the derivable columns
saves **14.3%** with no row growth. The wide table wins outright.

**And the read cost is worse than the storage cost.** The v5 read path already pays one join —
`blocks ⋈ block_segments` — because segments are the unit of truth and v5 leans on that harder, not
less. The wide block row is exactly what keeps that to a *single* join. Normalising channels would
make every chart a three-way join, landing the cost precisely on the surface workstream C exists to
make faster.

**The "sparse rows cost pages" argument does not hold.** For SQLite: a NULL is stored as a type code in
the record header with **zero payload bytes**, so twenty empty columns cost about twenty bytes per
row — ~2.7 MB across this database, not the tens of MB implied. The "fewer pages per scan" case does
not hold, and the measurement above confirms it.

---

## 3a. `block_segments.band` — the vocabulary is a 2×2, and stays a 2×2

**Decided 18 Sep 2026.** Recorded here because the shape of this column is a schema decision, and
because an earlier draft of the import spec got it backwards and would have destroyed information.

The `blocks` drop list removes `imp_ev_band` and `imp_home_band`, so from v5 onward **the band lives
only in `block_segments`**, read together with the segment's attribution. That makes the vocabulary
load-bearing in a way it was not in v4, where the column name itself carried the attribution.

| | low | high |
|---|---|---|
| **house** | `night` *(v4 wrote `off_peak`)* | `day` |
| **EV / device** | `off_peak` | `peak` |

plus `standard` — **unknown**, not a band.

**All four are kept.** They are two different determinations, not two names for cheap and expensive:

- **house `day`/`night` is clock-determined** — Economy-7, the same boundary every day.
- **EV `peak`/`off_peak` is dispatch-determined** — a smart-charge slot is off-peak; drawing outside
  one is peak.

Under IOG-SMB the supplier states the 2×2 directly as four rate buckets
(`CONSUMPTION_CHARGE_ECO7_DAY_x`, `ECO7_NIGHT_x`, `EV_DEVICE_PEAK_x`, `EV_DEVICE_OFF_PEAK_x`), so the
schema is recording what the bill records, not an invention of ours.

### EV·`peak` is reachable and must never be pruned as vestigial

It looks impossible — the whole point of Intelligent Octopus is that the car charges in cheap slots —
and that appearance is exactly the trap. **The EV can legitimately draw at peak:**

- a **bump charge**, which the user requests outside any planned dispatch;
- charging **beyond the 6-hour off-peak cap**, after which the discount stops applying.

Measured on the reference database: **169 blocks carry `imp_ev_band = 'peak'`.** Any future pass that
prunes "unused" enum values, or any surface that assumes an EV segment implies off-peak, is wrong on
real data. Both cells of the EV row are live.

### Measured against a 4.5.14 prod database, 24 Sep 2026

*Added because three claims in this section rested on the wrong axis, the wrong
account shape, or a comment that turns out to be false. 137,099 blocks / 37,525
import segments, June 2023 → Sep 2026, tariff migrated to IOG-SMB on
**26 Aug 2026** (recorded here so the era boundary is not re-derived: it is the
first `measured_cost` row, `2026-08-25T23:00:00`, midnight BST).*

**The vocabulary actually in use:**

| attribution | band | segments | kWh |
|---|---|---|---|
| house | `off_peak` | 15,021 | 25,325 |
| house | `standard` | 12,753 | 4,349 |
| house | `day` | 9,191 | 937 |
| ev | `off_peak` | 548 | 1,289 |
| ev | `peak` | **12** | 2.27 |
| house | `peak` | **0** | — |

**1. `house`/`peak` is structurally unreachable, and the note claiming otherwise is
wrong.** `_band_rate_snap_core` (deleted in v5) asserted *"a house segment banded
'peak' may be perfectly correct — common on an account with no home battery, where
house draw lands in a peak bucket routinely."* That is false, and not merely
unobserved on a battery account. Every writer of a house band into
`block_segments` uses the house vocabulary — `apply_measured_to_block` and the
four sites in `reconcile_dispatch_overlay` all emit `['day', 'off_peak']`. The one
`['off_peak', 'peak']` assignment, `kraken_api_client._parse_breakdown_node`, is
consumed **only** to collapse into a three-valued `measured_cost.label`
(`OFF_PEAK` / `STANDARD_RATE` / `mixed`) and is never stored as a band. So no
account shape can produce that cell. *(Read the write paths before the
distribution: a distribution tells you what happened on one account, the writers
tell you what can happen on all of them.)*

**2. EV·`peak` is live, but this section's evidence is on a column v5 DROPS.** The
"169 blocks carry `imp_ev_band = 'peak'`" measurement is now 212 blocks — and the
segment-level count of the same cell is **12**. Both non-zero, so the conclusion
stands and the cell must not be pruned. But `imp_ev_band` is on the 13-column drop
list, so once it goes this argument loses its footing; the figure to quote is the
`block_segments` one.

**3. `standard` before 2025 is OUT OF SCOPE — deliberately, not by oversight.**
12,746 of the 12,753 unknown-band segments are pre-migration, almost all 2023–24,
and they sit on days that WERE banded (208 of 210 days in 2023 carried two
distinct import rates — Economy-7, years before SMB, so the house day/night axis
long predates IOG). The band was therefore determinable and was not determined.

It stays that way. Those blocks were **authoritative when written and will never be
re-costed**, so a label nobody will read is not worth an importer rule. Recorded as
closed so it is not reopened as an open question.

**4. The SMB era is clean, which is the era that matters.** If Octopus's own model
ever needs re-reconciling, the buckets must be recoverable from what EMT stored.
Measured across the month since migration:

| | | |
|---|---|---|
| `ev`/`off_peak` | 198 | 457.02 kWh |
| `house`/`off_peak` | 390 | 425.36 kWh |
| `house`/`day` | 209 | 0.92 kWh |
| `house`/`standard` | 7 | 3.92 kWh |

and the label→band mapping is coherent: `OFF_PEAK` → the two off-peak cells,
`STANDARD_RATE` → `house/day` and nothing else, `ZERO` → no segments (BL-70). The
7 unknowns are all dated within 24 hours of the snapshot, priced at exactly the two
published SMB rates (5.493p / 32.3092p), so they are the hourly settlement pass
lagging rather than a gap — `standard` meaning "not yet", which is what it is for.

**`house`/`day` carrying 209 segments for 0.92 kWh** is a battery covering daytime
draw almost entirely. The cell is labelled correctly while carrying nearly nothing;
on an account without storage the same segments would carry real energy. Worth
knowing before reading any band distribution as evidence about the code.

**5. Seven segments where the two copies disagree** — block `imp_home_band =
'off_peak'` against segment `band = 'day'`, July to September. `imp_home_band` is
populated on only ~900 of 37,525 house segments, and the segment is what v5 keeps.
Seven instances of exactly the drift §2 gives as the reason these columns go.

### The relabel is the importer's job, once

The correction `house off_peak → night` is **SMB-era only** and happens in the importer as a
declarative column rule (import spec §5.2), not as a heal and not in domain code. Pre-SMB house low
means "a dispatch discounted this slot" — the dispatch sense — and `off_peak` is the right word
there. The vocabulary differs by era because the tariff mechanism does.

---

## 4. The channel bug is fixed at the flag, not in the schema

Normalising had one genuine attraction: it would make the `needs_pass2_rerun` defect structurally
impossible. That flag is **block-level and carries no channel**, so an export-only settlement
re-priced the import channel against unchanged kWh, rewriting a settled-looking historical block a
day later (v4 BL, issue #453).

That is a one-column problem, not a table-shape problem. Fix it where it lives: make the flag
channel-aware, or move the work queue to a small dedicated table keyed by *(block_start, channel)*.
Either is cheaper than reshaping the table every reader depends on, and neither taxes the read path.

---

## 5. ~~Still open: seven columns needing a real trace~~ — RESOLVED, 24 Sep 2026

These look dead but are **not** settled, and must not be dropped on the current evidence:

`finalised_from_cad` · `review_dismissed` · `derivation_id` ·
`imp_read_start` · `imp_read_end` · `exp_read_start` · `exp_read_end`

**Why the evidence is not sufficient yet:**

- *Data is not proof.* `finalised_from_cad` and `is_provisional` hold a single value across 136,202
  rows on the reference account — but that account runs `mini_provisional=off`, so those columns are
  live for configurations this database never exercises. Single-valued on one account ≠ dead.
- *Grep is not proof.* A pattern search suggested these have no consumer outside the store layer.
  But the `imp_read_*` pair is demonstrably live — startup logs
  `gap detection — last_block … read_end=33353.109` — it is simply reached through a store method
  rather than by naming the column. "Only named in `block_store.py`" usually means *correctly
  encapsulated*, which is the opposite of dead.
- *Pattern search is demonstrably unreliable here.* It classifies `needs_pass2_rerun` as write-only,
  while `_drain_pass2_queue` plainly reads it. Four `SELECT * FROM blocks` sites and a `_row_to_block`
  mapper mean columns are returned without ever being named.

**The sound method** is the one that worked for the dead-code question: an AST reachability trace
from real entry points, deliberately over-approximating so that anything still reported unreachable
is confidently unreachable — corroborated by "never non-null in *any* database we hold", not just
one. Do it with the databases to hand, and only then extend the drop list.

### The trace, done — six are live, one is write-only

*24 Sep 2026. Method: AST over every string literal in the application EXCLUDING
docstrings — the counts a first pass produced were inflated by prose, which is the
trap §4a names — cross-checked against a real 4.5.14 database (137,099 blocks).
Each column was then followed THROUGH `_row_to_block`, because a column can be
live without ever being named outside the store.*

| column | on prod | verdict | evidence |
|---|---|---|---|
| `imp_read_start` / `imp_read_end` | 29,145 rows, 7,233 distinct | **LIVE** | mapped to `read_start`/`read_end` by `_row_to_block`; 12 consumer sites in `engine.py` |
| `exp_read_start` / `exp_read_end` | 9,841 rows, 7,206 distinct | **LIVE** | same mapper, same consumers |
| `finalised_from_cad` | 1 distinct (`0`) | **LIVE** | a PREDICATE: `AND finalised_from_cad = 0` in `get_unsettled_blocks`, `count_unsettled_blocks`, `finalise_past_horizon_blocks` |
| `review_dismissed` | 1 distinct (`0`) | **LIVE** | a PREDICATE: `WHERE … AND review_dismissed = 0` (`block_store.py:5191`) — what stops a dismissed review being resurrected by the next reconcile scan |
| `derivation_id` | 17,884 rows, 1 distinct | **write-only** | see below |

**Five of the six would have read as dead to a method that stopped at "who names
this column".** Two are single-valued on the reference account and both are
predicates; four never appear outside `block_store.py` and reach the application
under different names. §5's own warnings — *data is not proof*, *grep is not
proof* — were both correct, and the thing that actually settled it was following
the value through the mapper.

**`derivation_id` is write-only, and stays anyway.** It is written
(`tag_blocks_with_derivation`, preserved by `COALESCE` on imported upserts) and
17,884 blocks carry it, but it is **absent from `_row_to_block`**, so the value
never reaches the application; nothing reads it, joins it or tests it; and its FK
target's accessors — `get_historical_derivation`, `list_historical_derivations` —
have no caller outside the test suite.

The TABLE is not dead: `historical_derivation` holds four real rows with tier
breakdowns, confidence scores and provenance strings (*"rate-from-cost (CSV
import)"*). What is unbuilt is the link BACK — nothing asks "which derivation
produced this block?". That is an unfinished feature, not dead weight, and §4a's
asymmetry decides it: leaving it costs a reader's attention, dropping it costs the
ability to answer a provenance question someone deliberately made answerable. It
stays, and the import spec already carries it.

### Two columns nobody had listed

- **`needs_pass2_rerun`** — single-valued (`0`) across 137,099 rows. Not a
  candidate: §4 already decides its future (make the flag channel-aware, or move
  the queue to a table keyed by *(block_start, channel)*). Noted because it looks
  exactly like a drop candidate to anyone who meets it without §4.
- **`review_reason`** — **one** non-null row in 137,099. `needs_review` is live,
  so the column is not dead; the figure says the review surface has fired
  essentially never on this account. Worth knowing before anything is built on it.

---

## 5a. Before any column goes — goldens per tariff (5 Oct 2026)

Every real database on hand is IOG, so parity on real data alone proves only that IOG accounts are
unchanged — and the ex-VAT family touches every bill on every tariff. Two checks come first:

- **`tests/test_tariff_goldens.py`** (CI): synthetic flat, Economy 7, Go and Agile databases
  (`tests/tariff_fixtures.py`), each with and without an EV charger and a battery, across the 1 Oct
  2026 VAT change, with segments and ex-VAT built by v5's own sweep. What every data surface answers
  is recorded (`.build/tariff_goldens.py`) and must not move. A reader mis-reading the stored cost by
  1% moves six bills on each fixture, so the goldens are not vacuous.
- **`.build/real_db_parity.py`** (local only): the same surfaces on a real pre-segments database — an
  Agile backup with 737 negative-price and 312 free half-hours — brought to v5's shape offline, with a
  schedule rebuilt from its own stored rates. The data stays out of the repo.

A first probe, on the fixtures (with remainders written as `_apply_pass2` writes them — the first
version of the fixtures left them NULL, which made Usage Stats count every device twice and made
this probe vacuous; fixed 6 Oct 2026): blanking the **bill** reader's ex-VAT columns and its
remainder column moved **no** answer — the bill takes those from segments and its own subtraction.
**Usage Stats, the heatmap and Insights usage do read the stored remainder**: changing it moved them.
So the remainder family's readers to move first are those, not the bill. On real
data a column and its replacement can disagree (497 stored cost remainders differ from main − subs
on one database), so the probe says the readers do not *need* those columns, not that dropping them
can never move a figure.

**6 Oct 2026 — the bill, the Overview cards and Usage Stats no longer read the remainder.** Their
Direct import is `direct_import.py`'s per-half-hour rule, main − Σ device grid kWh, and they agree
on every fixture (`tests/test_direct_import.py`). Device grid shares are clipped to the main by the
split (`_apply_pass2`) at write time; the readers add no second clip. On the five local databases with devices, the
stored `imp_kwh_remainder` equals the rule on every block. Where they differ it is the stored value
that is wrong: on two databases a device's rows had gone and its remainder stayed (151 blocks on
one, one night on the other). Usage Stats then read those days below the main meter, and the rule
puts them back on it. Still reading the column: Insights usage (`_aggregate_usage`), the HA
cumulative sensors (`get_cumulative_totals`), `_all_history_utc`, energy_charts' main-import helper,
and the day fingerprints. Those are the family's next readers to move.

---

## 5b. `rate_reconciled` — readers and writers moved (8 Oct 2026)

The first family. Drops wait for the importer (decided 8 Oct 2026): the importer is where v4's
shape is converted (import spec §1.2), so a column leaves v5's schema when the importer drops it,
in the same change. Until then a moved column is dead weight, not a second source of truth.

* **Writers.** The overlay reconcile verdict (`apply_reconcile_verdict`) set `rate_reconciled = 1`
  and left `rate_source` alone, so a reconciled block could still say `schedule`. It now writes
  `rate_source = 'reconciled'` on the main and its devices (a `measured` or `corrected` row keeps
  its word) and sets no flag. Measured settlement (`apply_measured_to_main`) set the flag beside
  `rate_source = 'measured'`; it no longer does. Nothing sets it now; the round-trip still carries
  it (so a legacy value survives a rebuild).
* **The reader.** The settlement re-run held a rate if `rate_corrected`, `rate_reconciled` or
  `rate_source = 'measured'`. It now holds on `rate_corrected` or `rate_source` in `reconciled`,
  `measured`, `corrected`, and reads the flag only as a **legacy fallback** for rows a reconcile
  flagged while leaving `schedule`: 11 on prod, 0 on prod-dev, 1 on PianSom, 2 on the old dev
  database. The importer folds them into `'reconciled'` (spec §5.2) and the fallback goes with the
  column. The decision only matters under CAD billing: on the DCC path every predicted band is
  held anyway (4.5.15).
* **Still touching the column:** the schema, the round-trip carry (`_block_rows` /
  `_row_to_block`), BL-53's one-off `rate_source` backfill (reads it for NULL rows), and the
  fallback read in `get_block_rate_authority`. All four go when the importer drops it.

`tests/test_rate_reconciled_folded.py` pins it; the tariff goldens did not move.

## 6. Summary

| | |
|---|---|
| **Shape** | Wide, one row per block. Unchanged from v4. |
| **Dropped** | 12 columns — 11 derivable from segments, plus `rate_reconciled`. `imp_provisional` was removed from the list on 5 Oct 2026: it is live and distinct (§2). `imp_kwh_grid` was removed from the list on 24 Sep 2026: sub-meters carry it and have NO segments (import spec §5.2, C §3). |
| **Rejected** | One row per channel: −5.6% space, +28% rows, and a three-way join on every chart. |
| **Deferred** | ~~7 columns pending a trace~~ — **traced 24 Sep 2026 (§5): six live, `derivation_id` write-only and kept.** |
| **Justification** | Correctness. One source of truth per fact. The 14.3% is a side effect. |
