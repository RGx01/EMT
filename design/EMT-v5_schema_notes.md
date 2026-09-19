# EMT v5 — schema shape: what goes, what stays, and what was rejected

*A decision record for the `blocks` table. The drop list itself lives in the import spec (§5.2);
this document holds the reasoning, the measurements, and the alternative that was considered and
rejected — so it is not re-proposed in six months.*

**Status:** decided 18 Sep 2026. Measured against a real 4.5.13 production database
(136,202 rows, 45 columns, 78 MB).

---

## 1. The decision

**Keep the wide, one-row-per-block shape. Drop 15 columns. Fix the channel bug at the flag.**

---

## 2. Why the columns go — correctness, not space

This is the part most likely to be misremembered, so it is stated first: **columns are removed
because derived values drift from their source, not to save bytes.**

Every column on the drop list is reproducible from `block_segments`, which v5 carries verbatim. A
value stored in two places eventually disagrees with itself, and in v4 that produced a recurring bug
class of real cost:

- a rate divided back out of a billed cost landed a whisker off the published band, so the rate line
  plotted peak across half-hours the bill had charged off-peak;
- an EV split stored on the block went stale against the segments that actually priced it;
- ex-VAT figures were recomputed differently in two places.

None of those were storage problems. They were two-sources-of-truth problems, and the fix is to keep
one source.

**The drop list: 15 columns.** The 13 in import spec §5.2, plus:

| extra | why |
|---|---|
| `rate_reconciled` | A second provenance flag beside `rate_source`. Fold into the provenance model. |
| `imp_provisional` | Duplicate of `is_provisional`; one distinct value (`0`) across 136,202 rows. |

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

## 5. Still open: seven columns needing a real trace

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

---

## 6. Summary

| | |
|---|---|
| **Shape** | Wide, one row per block. Unchanged from v4. |
| **Dropped** | 15 columns — 13 derivable from segments, plus `rate_reconciled` and `imp_provisional`. |
| **Rejected** | One row per channel: −5.6% space, +28% rows, and a three-way join on every chart. |
| **Deferred** | 7 columns pending a reachability trace across multiple databases. |
| **Justification** | Correctness. One source of truth per fact. The 14.3% is a side effect. |
