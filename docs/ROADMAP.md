# Roadmap

*Workstream status and the open backlog. The agreed picture of what v5 is made of is
[design/EMT-v5_architecture.md](design/EMT-v5_architecture.md); the reasoning behind the ordering is
in [design/EMT-v5_programme.md](design/EMT-v5_programme.md) — this file tracks state only.*

**No release has shipped** — EMT is pre-release. ✅ marks work done on `dev`; each names the PR or commit that did it.

**How this gets built** ([architecture §6](design/EMT-v5_architecture.md)): v4 shipped **4.5.14** and
is frozen to **critical fixes only** (data loss, crash-on-start, a pricing error), each ported here as
its forward fix. This repository was seeded with a verbatim copy of 4.5.14 (suite **2,764** green at
the seed, 21 Sep 2026; **3,605** on 4 Oct 2026; **4,552** on 28 Oct 2026) and the workstreams below
deconstruct it. v4 is at **4.5.24**; the import floor is **4.5.23** (E).

*Refreshed 29 Oct 2026 — every item's status checked against the code and the merged PRs.*

| # | Workstream | Depends on | State |
|---|---|---|---|
| A | Storage and access model | — | ✅ closed 30 Sep 2026; A.1 closed with a caveat 5 Oct 2026 — a ~1-min burst after a restore |
| B | Job coordinator (v4 BL-50) | A | ✅ all six acceptances met — 6 on the Pi, 5 Oct 2026 |
| C | One API for every surface | A | ✅ acceptance rows 1–8 met (C design §0) |
| D | Surfacing performance | A, C | ✅ closed 5 Oct 2026 — queries asserted in CI (#102), time on the Pi (#64) |
| E | Importer | A–D | 🔨 built 10 Oct 2026 (#122–#128, step 5 done), spec v29, floor 4.5.23 (positive fixture taken 12 Oct) — open: the Pi timing |
| F | Carried from v4's backlog | — | ✅ all done or closed — BL-64, the last, 30 Oct 2026 (§F) |
| G | Local tariff store | A, B | ✅ G1, the agreements, done 28 Oct 2026 (#320–#325, #327); G2, the rates as a pricing store, dropped (#326); ✅ G2 re-scoped as prices for display, built 31 Oct 2026 (#357, #358, G2c) |
| H | Before going public | — | 📋 1 item (BL-77), deferred until EMT goes to `main` |

---

## A. Storage and access model

*The root. One writer owned by the engine; read-only connections per request.*
Design: [design/EMT-v5_A_storage_design.md](design/EMT-v5_A_storage_design.md).

Not "build the mechanism" — v4 built it three times. **4% of call sites use it**, and 41 GET routes
still read on the engine's writer handle. A is about making the unsafe path hard to reach.

Generalises a fix v4 already made once under duress — `_generate_charts_offloaded` has its own
read-only `BlockStore` because of a cross-thread SQLite segfault — and applies it to every reader
rather than to the chart renderer alone.

**Done when:** a soak hammering every read surface while the engine writes shows no torn rows and
no read blocked behind a write.

**Done 30 Sep 2026.** Five Pi soaks (A design §5e): no torn rows, 0 SQLite faults, no read
blocked in SQLite, no GET waiting on the engine loop for a database read. What they left is §A.1.

### A.1 Engine writes stall on the SD card

*Not a read-path problem, so not A's — but found by A's soak, and it has a user-visible cost.*

On the Pi's SD card, the engine loop still stalls 10–53 s when several things write at once, and
every stall has the engine thread inside a **commit** (fifth soak: 80 over 1 s, worst 52.7 s, 69 in
`set_block_segments` during the re-price sweep). One stall dropped the HA WebSocket. Candidates,
largest first:

- ~~**The pre-rendered chart files**~~ — deleted by C (30 Sep 2026, #23). The sixth soak
  (4.5.16-a20, same restore and sweep) had no render at all and a worst stall of **22.3 s**, down
  from 52.7 s: 83 slow commits, 74 in `set_block_segments` during the sweep, none with a WAL
  checkpoint inside; one was followed by an HA WebSocket reconnect (C design §5).
- **The pre-restore safety zip**, written while the restored engine starts — it overlapped the
  worst commit. Now one step (#56, #58): 52 s in the twelfth soak, against 3 min 50 s restarting.
- ~~**`_backup_to_share` after every block finalise**~~ — daily since #68 (#67, 4 Oct 2026). Its
  ~80 s windows held 18 of a night's 25 loop-lag warnings.
- ~~**The carbon tick**~~ — #66 (#65): a blocking API fetch on the loop and ~190 single-row
  commits every 15 minutes; lag followed 100% of idle ticks before, 13% after (those overlapped
  the share copy above).
- **Commits on the engine thread at all.** If the above are not enough, route engine writes
  through a writer thread so the loop never waits on the device. An architecture change, and
  deliberately last.

**WAL checkpoint starvation — measured in the eighth soak (4.5.20-a21, 3 Oct 2026).** SQLite can
only reset the WAL when no reader is using it. The soak's two workers kept a read open almost
continuously, and its slowest requests ran 13–29 s (the all-days load, a full year's bill), so the
checkpoint never got its gap:

- the WAL grew from ~25 MB at 14:08 to **343 MB** by the start of the quiet phase (14:13). A
  checkpoint at 14:11 copied 51,369 of 57,961 pages and could not truncate ("a reader holds an
  open transaction");
- it stayed at **~346 MB** for the whole 30-minute quiet phase, and dropped to **4 MB** at 14:50,
  once the soak stopped;
- every quiet-phase read went through it. Requests whose code had not changed since the seventh
  soak ran **1.3–1.9× slower** (Live Power cards 0.56 → 1.08 s p50, picker totals 0.84 → 1.56 s);
- commits under it were slow too: `save_current_block` 20.5 s at 14:32, WAL 346 MB.

The seventh soak's WAL peaked at 115 MB and kept draining: its slowest surface then took ~6 s at the median (a partial year's bill).
A household is unlikely to cause this, but overlapping long reads would, for example a dashboard
card refreshing while someone opens year views. Candidates: make the long reads short (#47, #48),
and/or have the engine force a checkpoint (`wal_checkpoint(RESTART)`) after a sweep, before readers
pile on. **The soak now waits for the WAL to drain** after the sweep (a settle phase), samples it
through the quiet phase, and warns when it starves, so its budgets measure normal use.

**Twelfth soak (4 Oct 2026, after #66 and #68):** the quiet phase **passes** loop blocking for the
first time (worst lag 1.46 s). The whole run still fails, on the re-price sweep alone: worst lag
12.9 s and worst commit 12.3 s, in `set_block_segments` (72.6 s and 48.4 s that morning, before the
share copy went). What is left is the sweep's own commits — candidates: one transaction per chunk,
as #66 did for carbon; then the writer thread, last.

**Fifteenth soak (5 Oct 2026):** the quiet phase passes again — one over-budget lag, a 4.8 s
log-pipe flush, and no slow commit (the dispatch poll's commits went with #91). The sweep still
fails: 85 of the run's 97 slow commits are `set_block_segments`, worst 16.8 s. *Then, the same day:*
the sweep commits **once per chunk** — each block's segment write had been its own transaction,
and the chunk's split, exc and cursor three more (63 commits for a 60-block chunk; now at most 3).
The writer thread stays last, pending a soak.

**Sixteenth soak (5 Oct 2026, after #95) — the cheap half is done, and was not enough.** The
sweep took **1 min 16 s** (about 7 min in soak 15); over-budget loop lags fell from 59 to **10**,
slow commits from 97 to **11**, and the quiet phase passed every surface. But the run still fails:

- inside the sweep, each chunk's single `batch` commit took 2–5.5 s, and a lag of the same length
  followed each (7 of them);
- the worst lag, 18.3 s at 10:30:37, is not the sweep at all: the half-hour finalise's
  `save_current_block` commit took 10.4 s and `_insert_block_rows` 8.2 s, both single small
  commits. Soak 13 had the same (18.8 s at 11:00).

The database runs WAL with `synchronous = NORMAL`, so an ordinary commit does not fsync; only a
checkpoint does, and 10 of the 11 slow commits had none inside. The time is therefore most likely
the commit's own `write()` blocking while the card drains dirty pages — Home Assistant's recorder
writes to the same card. Fewer commits cannot fix a single commit that takes 10 s; keeping the loop
from waiting on one can. **Next: the writer thread** —
[design/EMT-v5_A1_writer_thread_design.md](design/EMT-v5_A1_writer_thread_design.md).

**Seventeenth soak (5 Oct 2026) — the design's step 0, with the Pi's second EMT instance (v4
4.5.20 dev) stopped.** The quiet phase had **no** over-budget lag, finalise included (the 12:00
finalise made no slow commit); soak 16's worst, 18.3 s at 10:30:37, fell on a half-hour boundary,
when the other instance finalises too. Over the whole run, 4 over-budget lags (10 in soak 16) and
8 slow commits (11), worst 7.7 s — **all within 47 s after the restore**, where the sweep's chunk
commits met startup's own writes: the first Kraken poll's BL-8 outage backfill
(`create_backfill_block`, 7.7 s), its settlement (`upsert_kraken_block`, 3.2 s) and the carbon
backfill. *Then:* the sweep holds its first pass until startup has finished, no poll runs and 60 s
have passed (capped at 5 min). Two of the sweep's own chunk commits (3.0 s, 5.1 s) ran with nothing
beside them, so the hold is not expected to pass the run on its own; it removes the overlap. **Two
EMT instances on one SD card** — a v4 and a v5 side by side — is a known cost: their half-hour
finalises commit together.

**Eighteenth soak (5 Oct 2026, after #97, v4 stopped) — closed with a caveat.** The sweep held
61 s for startup's own writes, then ran in 49 s. Over-budget lags 5 (4 in soak 17), worst **5.8 s**
(11.2 s), slow commits 6; the quiet phase, the 13:30 finalise included, had none. Each that remained
was **one commit with nothing beside it**: the first poll's outage backfill
(`_insert_block_rows_replace`, 5.75 s, during the hold) and four of the sweep's chunk commits
(1.4–4.4 s). There is nothing left to batch or to separate.

**Closed 5 Oct 2026, by decision, with this caveat recorded:** on SD storage, for about a minute
after a restore or an upgrade, single commits take 2–6 s and the engine loop waits on each; normal
running is clean. The original criterion — no stall over 2 s across the whole run — is not met.
The fix for the remainder is the writer thread, designed and not built
([design/EMT-v5_A1_writer_thread_design.md](design/EMT-v5_A1_writer_thread_design.md)); reopen if
real use shows the burst matters, or when HA's recorder sharing a card is shown to bring stalls into
normal running. Known cost, also recorded: **two EMT instances on one SD card** commit their
half-hour finalises together (soak 16's 18.3 s stall).

## B. Job coordinator

*One coordinator for user-mutating jobs; deny-if-busy with idempotent retry.* Carried from v4's
BL-50. Design: [design/EMT-v5_B_coordinator_design.md](design/EMT-v5_B_coordinator_design.md).

**State, 4 Oct 2026.** Membership **16 of 17** (only `api_db_vacuum`, which never joins); **6 of the 8**
members that declare an edge consult `admission()`, and the other two (`delete`, `restore`)
pre-empt the import. Acceptances (B §5):

| # | acceptance | state |
|---|---|---|
| 1 | every writing job is a member | ✅ ratchet at 1 (`api_db_vacuum`) — #69 |
| 2 | no unregistered write from a request thread | ✅ pinned to `api_db_vacuum` — #75 |
| 3 | declared dependencies honoured | ✅ a test per edge — #70, #71, #72 |
| 4 | a claim before any slow work | ✅ the API import claimed before its backup — #73 |
| 5 | admission never per block | ✅ nine per-block writers, no await or admission — #74 |
| 6 | Pi soak, members concurrent | ✅ soak 15, 5 Oct 2026 — every edge held (B §5) |

**Acceptance 6, met 5 Oct 2026:** the soak database restored (starting the sweep), then a bounded
gap fill and a one-day range delete started mid-sweep. The sweep deferred to each in the same
second and was re-admitted when each finished; `scan-log` coordinator PASS, no refusal left
standing. Two cosmetic findings: the sweep status labels a restore-armed sweep `upgrade`, and the
dispatch poll logs "completed energy on N slot(s)" for slots it no longer writes.

*Fixed 4 Oct 2026:* a restart no longer loses a deferred, paused or mid-chunk verify pass — the
tick resumes it from its persisted cursor, once per database; and a pass **paused for API
allowance** (120 waits, ~2 h), which kept its `verify_pricing` claim and so held attribution until
a restart, is retried from its cursor after `_VERIFY_PAUSE_RETRY_S` (1 h). *(`/api/historical/api-import/apply`, which
ran the import without claiming it, was deleted 4 Oct 2026: no UI had ever called it.)*

**Done 5 Oct 2026.** What remains of B is the open questions in B §6, none blocking C.

## C. One API for every surface

*Break the write → wholesale-recompute coupling. No pre-generated HTML.*
Design: [design/EMT-v5_C_surfaces_design.md](design/EMT-v5_C_surfaces_design.md).

**State:** acceptance rows 1–8 met (C design §0) — no pre-generated artefacts and `CHART_DIR` gone,
every surface on `/api/energy`, no defaulted tariff context, billing reconciled with v4 to the
penny (with v4's sub-meter rounding explained, #63), and every surface bounded on the Pi.

**#31 — fixed 5 Oct 2026.** The hybrid EV fold capped each bucket's move at its own Direct, so a
day let one slot's surplus cover another's shortfall and moved more EV than its half-hours summed
(36 of 60 days on a production-like database). Coarser buckets now fold per slot, as the HH view
does, and sum: over 1,216 days a day equals its slots, and no day total moved.

**C §7.3 — closed 5 Oct 2026.** `/api/energy` caps a request at 31 days of blocks and 366 days
for a period total; by-day is uncapped. Only `kind=range` (and `kind=all` for a period) could
exceed either, and no page does.

**Direct import — one rule, 6 Oct 2026.** Three surfaces showed the house's share of the main
meter, and each worked it out its own way: the bill floored it each half-hour, the Overview cards
over the whole window, Usage Stats in kWh per half-hour and in £ per day. Where devices out-read the
main (fixture `e7_over`) the same period read 87.64 kWh / £22.63, 8.96 kWh / £13.57 and
87.64 kWh / £17.43. `direct_import.py` is now the rule, in Python and in SQL, and all three use it.
`tests/test_direct_import.py` asks every surface for the same periods on every tariff fixture
and fails if they disagree. On two real databases this also corrected days where a stored remainder
had outlived its device's rows.

**The grid boundary is the clip, 6 Oct 2026.** The first version also showed a "Devices beyond the
metered supply" line, for half-hours where the devices exceeded the main. It came out the same day.
The house/device split (`_apply_pass2`) clips each device's grid share to the main when the block
is written, so the devices cannot exceed the main, and the read side holds no second rule. The
device's own reading from Home Assistant (`imp_kwh` and its register reads) is kept as recorded;
only `imp_kwh_grid` is cut. The one exception is an unsettled gap-fill block, which the split
records as-is because the main there is an estimate. On such a day the breakdown reads above the
meter until settlement re-clips it, which takes a day or two. That was accepted rather than adding
a display line. The fixtures now take every grid share from `_apply_pass2`: `e7_over` is clipped
and `e7_gap` is the exception.

**Open — two Direct readers not yet on the rule.** (1) Insights usage (`_aggregate_usage`) floors
house = main − devices − EV once per period, through its own segment re-pricing. (2) The IOG hybrid
EV: the bill carves EV out of Direct per rate while the Overview counts it as a device. They agree on
every fixture, but no fixture is IOG, so that agreement is unasserted. Both belong in
`test_direct_import.py` when they move.

**The EV split — one reader, 8 Oct 2026 (step 5).** The bill priced a half-hour's EV from its EV
segments (BL-27). The Overview card, Usage Stats and Insights' EV carbon read the `imp_kwh_ev` /
`imp_cost_ev` columns instead, and the day charts fell to `imp_rate_ev` whenever the EV segments
summed to nothing, even on a block whose segments were house-only. `ev_split.py` is now the reader,
in Python and SQL: the EV segments, or for a block with no segments at all its columns (LEGACY,
until the importer writes those blocks' segments — spec §5.2, v14). On five real databases the two
agree wherever both exist, so nothing moved; `tests/test_ev_split_readers.py` holds a stale column
on each side of the seam and fails on the parent.

**BL-79 — the EV-split writers commit the columns and the segments separately.** Proposed
8 Oct 2026. Every writer of the split (the close, the settlement re-run, the reprice sweep, the
carve, the back-attribution, measured settlement, the reconcile restamp and verdict, the corrections
route) writes `imp_*_ev` and `block_segments` in two transactions, and the segment write is
best-effort. Traced in the code (the carve's order checked by hand; the rest not yet confirmed by a
test), some paths write one without the other:

- **columns, no EV segments:** the settlement re-run's override branch (a held corrected,
  reconciled or measured rate), the round-trip rewrites (`append_block_replace`), the reprice
  sweep where a block already has segments, and the uncapped reconcile restamp (it rewrites the
  columns' rate and relabels the segments' bands, but not their rate);
- **EV segments, no columns:** a carve that loses its race (the segments are replaced, the guarded
  UPDATE touches nothing);
- **a value blanked:** a gap-fill rewrite over an existing row NULLs all five columns and leaves the
  old EV segments. (Measured settlement also writes NULL over an earlier split when the bill shows
  no EV, but it rewrites the segments house-only from the same figures, so the two agree.)

Checked on four real databases (8 Oct 2026), none of those four has left a mismatch. One path not
in the list does. **(1) The held-rate re-cost — fixed 8 Oct 2026.** When the settlement re-run holds
a corrected, reconciled or measured rate, it re-costs the block to the settled kWh and re-derived
nothing else, so the segments stayed at the kWh the block closed with: 11–12 measured blocks per
install (4 with EV) and one corrected block no longer summed to `imp_kwh`, and the bill's EV/Home
rows with them. The re-run now fits the segments to the settled kWh at the rates they carry
(`pricing_segments.refit_to_kwh`; the house takes the change, the EV only once the house is gone),
and `tests/test_held_rate_segments.py` fails on the parent. Blocks v4 already wrote that way — and
v4 keeps writing them, being unfixed — are refitted by the importer (spec §5.2, v16), with any
non-held block whose segments miss its kWh reported rather than changed.

With the surfaces on `ev_split`, the segments are the record. So the fix is in the writers, not the
readers: each writes its segments with its columns in one transaction, or (once the importer drops
the columns) only the segments.

**(2) One write per split — 9 Oct 2026.** Each writer passes its segments to the store method that
writes its columns, inside that method's transaction; the separate writers stay separate (a carve
fills a blank, a re-attribution overwrites — one method with a flag is how a correction gets
stomped). A failed write now changes neither, and the paths that wrote one without the other are
closed. Each has a test in `tests/test_split_writes_are_atomic.py`, `test_carve_one_write.py` or
`test_held_rate_segments.py` that fails on the parent:

- **a held prediction** with no seam segments rebuilt its segments from the block dict's column
  copies (`get_block_dict_by_start` attaches none), collapsing a multi-band block to one
  `standard` segment; it now keeps and refits the stored ones;
- **the carve** wrote its segments before its guarded UPDATE: a block carved in between lost its
  segments and was still counted and re-fitted, and a band COALESCE kept differed from the
  segment's;
- **the reprice sweep** filled a split's columns over a block's existing house-only segments;
- **back-attribution, measured settlement, the reconcile verdict and its stale-rated split heal**
  wrote columns and segments in two transactions — a failed measured write left the block stamped
  `measured`, which the drain never revisits, under its pre-bill segments;
- **the corrections route** committed a rate correction in four steps; it is one batch now.

Left as they are: the uncapped reconcile restamp only brings the columns up to segments that are
already right, and the round-trip rewrites and gap fill touch the columns alone — with every reader
on the segments, only the engine's own gates still read those columns.

**(3) The engine reads the segments — 9 Oct 2026.** Its checks (`imp_kwh_ev IS NULL` as "not split
yet" in the carve, the back-attribution, the settled-split heal and the reprice sweep's fill guard;
`imp_kwh_ev > 0` in the dispatch-only re-fit) and the blocks and rows it loads (PASS 2's EV
reservation, the re-fit, the reconcile's segment rebuilds, measured settlement's stored EV) read
ev_split: the EV segments, the columns only for a block with no segments. Tests in
`tests/test_ev_split_gates.py` and the two files above fail on the parent on the shapes where the
two disagree — blank columns over EV segments (what a gap-fill rewrite leaves) and a stale column
over house-only segments. A round trip now writes the segments' split back to the columns.

**BL-79 is closed.** What still reads the `imp_*_ev` columns is the LEGACY fallback for a block with
no segments (`ev_split`), the uncapped reconcile restamp and the corrections route's `CASE` (both
keep the columns coherent with each other), the data-version tokens and two dead selectors
(`get/count_blocks_missing_iog_split`). All go with the columns when the importer drops them.

**Ex-VAT — the stored cost is the authority, 8 Oct 2026 (step 5).** `imp_cost_exc` is carried by the
importer, not dropped (spec §5.2, v15): it is the ex-VAT twin of `imp_cost`, and the segments'
Σ kWh × `exc_rate` was no closer to two Octopus statements. The other three ex-VAT columns are now
derived where read: the bill method takes the standing charge's ex-VAT as inc ÷ (1 + the day's VAT),
the data table's ex-VAT ratio is `cost_exc ÷ cost`, and `exc_source` had no reader. Over 41 months of
a production-like database no bill-method figure moved; `tests/test_exvat_readers.py` holds stale
`imp_rate_exc` and `standing_charge_exc` values and fails on the parent.

**BL-80 — CSV-imported history: `imp_cost` is Octopus's estimate, `imp_rate` is what it billed.**
Found 8 Oct 2026; settled 9 Oct. On blocks imported from the Octopus CSV (Jun 2023 – Aug 2024 on a
production-like database: up to 252 half-hours a month), `imp_cost` is not kWh × `imp_rate`, by up
to a few hundredths of a penny per half-hour and a few pence a month. Live and API data do not show
it, so no v4 fix regressed. It surfaces as a 1–2p disagreement between the segments (priced at
`imp_rate`) and the stored ex-VAT cost (`imp_cost ÷ 1.05`) on those months.

**Where each figure comes from.** `csv_import` stores the CSV's per-half-hour *Estimated Cost Inc.
Tax* as `imp_cost`; `imp_rate` is the tariff's published rate. For June 2024 (Intelligent Octopus
Go, region B — the statement's "postcode area alpha identifier" is not the tariff region) Octopus's
public API publishes off-peak 7.1428p ex-VAT / 7.49994p inc and peak 25.044p / 26.2962p: exactly
EMT's `imp_rate`. The CSV's estimate is priced at those rates **rounded to 2 dp** — 7.14p and 25.04p
ex-VAT, i.e. 7.497p and 26.292p inc — which is what the statement prints.

**Which Octopus billed.** The statement for 12–30 Jun 2024 rules on off-peak: no kWh that rounds to
its stated figure gives its stated cost at 7.14p, and every one does at 7.1428p. So the bill is
kWh × the **published** rate, and the CSV cost is an estimate. Peak is consistent with either (its
kWh is printed to 0.1, which is too coarse to separate them). The API's half-hourly
`costInclTax.estimatedAmount` is named the same way, and from 1 Oct 2026 it is wrong for another
reason (v4 #505, VAT): Octopus's estimated amounts are not its bill.

**Open — what to do with it.** History stays as stored until decided. The candidate fix is to
re-cost CSV-imported blocks to kWh × `imp_rate` (moving those months a few pence each towards the
statements), in v5 by the importer — v4 takes critical fixes only, and this is not one. It would need
an explicit exception to the import spec's gate 2 (total cost matches the source exactly), and it
belongs with the wider question parked on 9 Oct: whether EMT should cost every block as kWh × the
published ex-VAT rate per band with VAT from its own calendar, rather than trusting Octopus's
estimated amounts. The June 2024 statement and the region B rates above are the test case.

**Where a rate comes from (9 Oct 2026).** A cost is kWh × a rate, and the rate printed on a
statement is not precise enough to be it: Octopus prints 2 dp (7.14p for 7.1428p, 25.04p for
25.044p, 5.23p for 5.2314p), so kWh × the printed rate reproduces the CSV's estimate — this entry's
error — rather than the bill. In order of preference:

1. **The supplier's published rate** where it has an API. For Octopus that is every account and all
   of its history (the public tariff endpoint, no key), and it is what `imp_rate` already holds.
2. **With no API** (a `not-listed` supplier): the rate as precisely as the supplier states it (a
   tariff sheet or contract), else recovered from the statement's own band totals — band cost ÷ band
   kWh. On the June 2024 off-peak band that gives 7.1431p against the true 7.1428p (the printed kWh
   is rounded to 0.1), far nearer than the printed 7.14p and within a fraction of a penny a month.
   `bill_parser` already reads each band's printed rate, kWh and £ (`_TIER_RE`) and takes the printed
   rate unless a `rate_resolver` supplies one; the recovered rate belongs in that resolver.
3. **The printed rate** only as a last resort, flagged approximate, as the inc ÷ VAT fallback is.

The exact rate still leaves a gap to the statement wherever EMT's band split differs from Octopus's
(0.1–0.3 kWh a month more in peak on the months checked) — a kWh question, BL-78's remainder, not a
rate one.

**BL-78 — with Bill Rounding on, the Total Bill should be the bill method's total.** Proposed
8 Oct 2026. The card prints two different roundings of one bill. Its lines (the ex-VAT bands, the
standing charge, VAT on the subtotal, export) are summed and rounded once, the way Octopus's own
bill is built. Its **Total Bill** is `compute_period_net`, which rounds each local day's net to the
penny and sums the days, chosen so the Billing tab agrees with Usage Stats day by day. Over a
31-day bill the two came out 3p apart on a real account, both installs identical. The lines were
the closer of the two to Octopus's statement; the remaining 1–2p was the peak/off-peak kWh split,
not rounding.

Proposal: with Bill Rounding **on**, Total Bill = the bill method's import incl. VAT − export, so the
card's lines and its total agree and follow the supplier's method. With it **off**, keep the
per-day rule, so the total still matches Usage Stats. A display change only: no stored figure
moves. Not a v4 fix (display, not pricing — v4 takes critical fixes only). Check against the bill
goldens (`tests/test_tariff_goldens.py`), which would move only with Bill Rounding on, and say so.

**Done 10 Oct 2026.** `bill_card.card`: with Bill Rounding on, Total Bill = the bill method's
`inc_total` less each export credit as printed; off, it stays `compute_period_net`. The bill
goldens run with it off, and 0 answers moved. With it on, across nine 2026 billing periods of a
production-like database the total moved −1p to +4p a period. Against the 3 Jul – 2 Aug statement,
every card line but one now matches the statement (standing charge, export, VAT within 1p) and the
total follows them; the one that doesn't is the energy, 10p high because EMT puts 0.34 kWh more
into peak than Octopus did, which carries into the total. That band split is the open remainder: a
kWh question, not a rounding one (see BL-80's note).

**BL-82 — the settlement re-run replaces a settled block's bill with kWh × rate.** Found 10 Oct 2026;
**measured and decided in part 12 Oct 2026; fixed in v5 13 Oct 2026** (`_rerun_pass2_for_settled_block`
keeps a settled block's cost and ex-VAT; a settled kWh that differs from the bill's is logged, the
bill kept; `tests/test_settled_keeps_bill.py`). v4 unchanged — pennies, below its bar. When `_rerun_pass2_for_settled_block` reaches a block
whose rate is held — `rate_source` `settled`, `corrected` or `reconciled` — it keeps the rate and
re-costs the block to the settled kWh: `imp_cost = chosen × imp_rate`. On a `settled` block `imp_cost`
WAS the bill, so the re-run swaps what Octopus charged for kWh × rate. The import spec says the
opposite: "`imp_cost` on a settled block **is the bill** and must not be recomputed from segments"
(§5.2). v4 behaves the same.

*Why kWh × rate exists (the user, 12 Oct):* it is the best cost **before** the bill — a provisional
figure that firms as the kWh settles. It is right for every half-hour whose cost is never billed
separately (export; undispatched import, priced at the known tariff rate) and for a corrected or
reconciled rate. It is wrong only once a bill has arrived.

*Measured 12 Oct 2026, read-only, on the 4.5.22 production backup and the 4.5.23 dev backup:*

| | prod | dev |
|---|---|---|
| settled blocks with a cached bill | 1,021 | 1,319 |
| stored cost equals the bill | 759 | 1,072 |
| stored cost ≠ the bill | 262 | 247 |
| … of which = kWh × rate exactly (the re-cost) | 261 | 247 |
| … October, where the bill = stored × 1.05 (v4 #505's 5%) | 230 | 221 |
| … September (rounding: the bill's per-bucket £ against kWh × rate) | 34, net −£0.02 | 26, net £0.00 |
| the bill's kWh ≠ the block's kWh | **0** | **0** |

**The re-cost never had a different kWh to apply.** It fires when DCC kWh arrives or is re-confirmed,
not when it changes.

*Decided:* **re-cost kWh × rate only while the cost is not settled.** A settled block keeps its bill;
if the settled kWh ever differs from the kWh the bill priced, the block is flagged for attention
(BL-85), not recomputed — the bill is Octopus's own kWh, and the difference is a question to ask
Octopus (BL-86), not a figure for EMT to invent.

*Decided 12 Oct 2026 (the user): the fix waits for Octopus to restate October. **Octopus restated on 13 Oct 2026:** prod's VAT resync logged the hint at 12:02:13 and "restated the period … stored 279 verified half-hour(s), re-applied 24 block(s)" at 12:02:53; on that afternoon's backup all 176 October bills and 187 October blocks read 0%, and 1,041 of 1,073 settled blocks equal their bill — the other 32 September rounding, net £0.00. So the fix landed the same day.* The question was: the 230 October blocks read 0% VAT only because the re-cost
overwrote a bill that still carries 5% (#505, not restated as of 12 Oct). Keeping the bill makes them
match Octopus's half-hourly figures and 5% high against the statement until the VAT resync (v4 4.5.22,
v5 forward) picks up the restatement. #505's own rule — the VAT calendar recognises Octopus's
figures and never replaces them — says keep the bill. The fix can wait for Octopus to restate, or
land and leave October 5% high until it does. Octopus is reported (12 Oct) to have fixed the VAT on
its online charts; the half-hourly data the resync reads is separate, and the resync's own log
(`vat resync (#505)`: the hint, then "Octopus restated the period … re-applied N block(s)") is what
releases the fix.

*The model under it (12 Oct 2026).* A half-hour carries up to three settled facts, from two sources,
and each settles on its own:

| fact | source | when (prod since 1 Sep) | which half-hours |
|---|---|---|---|
| kWh | DCC consumption (`imp_kwh_api`, `exp_kwh_api`) | 1–2 days | every one, import and export |
| cost | the bill per half-hour (`measured_cost`) | 10–279 h after the slot, 68 h on average | import only, and only **after** its kWh. Octopus bills every half-hour on every Intelligent tariff; what differs is what EMT **fetches**. On IOG-SMB, every half-hour: the live pass asks for dispatched ones first, the history drain for the rest (prod, last 14 days: every import half-hour with settled kWh has its bill). On the other Intelligent tariffs (pre-SMB IOG, INTELLI), dispatched half-hours only — the rest keep kWh × the tariff rate and are never compared with the bill (a gap; BL-86) |
| House/EV split | the same bill's four buckets (HOME/EV × LOW/STANDARD) | with the cost, **or later** | IOG-SMB only. The buckets matter most in a dispatch or bump, where house and EV can be at different rates — the 6-hour EV cap is SMB's; pre-SMB had none |

- kWh before cost is EMT's rule, by construction: the live pass asks only for blocks with DCC kWh
  (prod: all 1,021 settled blocks have it).
- **The split can settle after the cost — a third level (the user, 12 Oct).** It never arrives before
  it: one breakdown carries both, and every bill on hand without a split is a "nothing drawn" zero
  (46). But on 10 Oct the first breakdown (fetched 11 Oct) put all six dispatched half-hours on the
  house, and the restated one (12 Oct) split them. For five of the six the cost was **identical to the
  penny** — the cost had settled, the split had not. For the sixth, 05:30–06:00 BST, the split decides
  the rate (the EV's dispatch makes it low rate outside the window), so the late split changed the
  cost too: £0.88 to £0.15. So: **kWh → cost → split**, and a late split can move a cost that read as
  settled. Off SMB the split never comes from a bill, and EMT's dispatch split is final.
- Export settles kWh only; its cost is kWh × the export rate, final once the kWh is.
- So settlement is a **pipeline**, not a flag: a half-hour waits for its kWh, then its bill, then —
  on IOG-SMB, where it charged — its split. A half-hour awaiting kWh is awaiting the rest too.

Three representations of one idea today — `imp_kwh_api IS NOT NULL` (kWh), `rate_source` (cost) and
4.5.23's wait map (split) — and BL-82 is the defect that comes from reading them as one. Any surface
that says "settled" should say which of the three.

**BL-83 — the bill's VAT line takes each day's rate from its first half-hour.** Found 10 Oct 2026. **Resolved by Octopus's restatement, 13 Oct 2026** (every October half-hour's pair now agrees with the calendar). **Parked 13 Oct 2026 (the user):** every symptom comes from half-hours still billed at 5% (#505), so it resolves itself once Octopus restates October and the VAT resync re-applies them. Revisit only if a later VAT change repeats #505; then label each day from the VAT calendar (`vat_at(day)`) and keep the amount as inc − exc.
`energy_charts._bill_method_breakdown` names the VAT in force per local day with
`vat_by_day.setdefault(day, the half-hour's own VAT)` — the FIRST priced half-hour, which in BST is
23:00 UTC the day before. With #505's 5% on 9 Oct's settled half-hours (UTC), the line read "0% … to
9 Oct, 5% from 10 Oct": 9 Oct, almost all 5%, named 0%; 10 Oct, the next day, named 5% from its first
half-hour, and the last span ran on to the period's end. v4 has had the rule since 4.5.19 (it is the
"interesting take on the VAT line" of 8 Oct).

*Proposed:* name the rate from the VAT calendar (the authority for what is in force), keep the AMOUNT
as each half-hour's own inc − exc, and where any half-hour's own pair disagrees with the calendar say
so on the line ("VAT @ 0% — includes … on half-hours where Octopus still charges 5%, not yet
restated"). Display only; not a pricing error, so outside v4's critical-fix bar. Less pressing while
BL-82's re-run happens to re-cost those half-hours to 0%; it is the next settled days that will show it.

**v4 #505 (Octopus's half-hourly costs at 5% after 1 Oct) — resolved 13 Oct 2026:** Octopus restated, and 4.5.22's resync re-applied the period on prod (BL-82 above). *As of 10 Oct:* Octopus has
not restated: on the dev database every settled October half-hour in `measured_cost`, 1–9 Oct, is
`cost_incl = cost_excl × 1.05`, including 9 Oct's fetched on 10 Oct at 12:03 — so newly settled
half-hours still arrive at 5%. No resync hint has fired (`measured_vat_resync` absent). The blocks read
0% only where BL-82's re-run re-costed them.

**BL-84 — a heatmap of half-hours missing their House/EV split, on IOG tariffs.** Proposed 12 Oct
2026; **folded into BL-85 the same day**, as its split layer — done with it (the split states on the Import
bill, #196). On 10 Oct Octopus's first breakdown
put a night's charging on the house, and nothing on any surface showed it (v4 4.5.23, #510). On the 12
Oct production copy, 7 of 707 completed dispatches since 7 Jul had no split: 10 Oct's six, and one the
hourly carve had not reached.

**BL-85 — surfaces that say what has settled, and what needs attention.** ✅ *Parts 1–3 done 13–17 Oct
2026: the pill (#150), the views by settlement state (#153; the Split view folded into the Import bill,
#196), the detail panel (#154) and acting on a selection (#156, #162). Part 4 closed 30 Oct 2026: it no
longer happens (below).* Proposed 12 Oct 2026 (the
user), from BL-82's model. *Design: [EMT-v5_settlement_surfaces_design.md](design/EMT-v5_settlement_surfaces_design.md) (parts 2–3 and BL-86, draft 13 Oct 2026).* Three parts; the detail needs refining before it is built.

1. **The chart pill tells the truth.** *Done 13 Oct 2026: it names the stages — "⏳ 63 awaiting kWh · … awaiting bill · … awaiting split" — from `/api/billing-source`'s `awaiting`; the dev backup of that morning read 63 / 0 / 0.* The Charts pill reads "N blocks awaiting **cost** settlement",
   and counts blocks without DCC **kWh** (`_UNSETTLED_WHERE`: `imp_kwh_api` or `exp_kwh_api` NULL) —
   including half-hours with no energy, and export, which has no bill stage at all. Cost settlement
   is a pipeline (BL-82's model), so the pill should name its stages. Production copy, last 14 days,
   import half-hours with energy: **31 awaiting kWh (and then their bill) · 0 with kWh, awaiting their
   bill · 303 holding their bill** — and, separately as *needs attention*, the 255 of those whose
   stored cost is no longer the bill (BL-82), which today count nowhere. The middle stage was empty at
   that snapshot, but is the one that fills while a bill lags its kWh.
2. **Heatmaps by settlement state, per channel.** Import, export, and on IOG the split (BL-84): each
   half-hour coloured by its state — provisional; kWh settled; bill settled; split from the bill, or
   EMT's own, or held back (4.5.23's wait), or kept EMT's after 28 days; and **needs attention**.
   *Needs attention* is what is worth a user's time: a completed dispatch with no split; a settled
   block whose stored cost is not its bill (BL-82); a bill whose kWh differs from the settled kWh; a
   bill held back past N days; a VAT pair that disagrees with the calendar (#505).
3. **Acting on it — context menus** on a half-hour or a selected range: re-fetch the kWh, re-fetch the
   bill, re-fetch the split, and show what was fetched against what is stored. The re-fetch itself is
   BL-86. *To refine:* which actions each state offers, how a range is selected on a heatmap, and what
   is confirmed before anything is written.
4. **A provisional half-hour looks provisional, not half-right.** 13 Oct 2026: while 05:30 BST
   waited for its split, the chart drew it House with the import rate line stepping to peak at
   05:30 — yet its cost was already at the low rate ("16.887 kWh @ £0.0523" included it). The line
   follows the split; the cost follows the dispatch overlay. Until the split lands (within ~5
   minutes of the completed record since #148; up to an hour before), the two disagree, which read
   as 4.5.23's bug returning. The line, the bar and the cost should tell one story, or the half-hour
   should be marked provisional.
   *Closed 30 Oct 2026: they tell one story now, so nothing is marked.* Before the completed record,
   `settle()` (engine step 4) prices the half-hour on its predicted band with no EV share: one house
   leg at the low rate, banded `off_peak`. The chart's house line takes a band the leg names
   (`chart_emit.day_rate_series`), and the devices are priced from that leg, so every line sits on
   its bar. Checked on the staging copy: 13 Oct 05:30 BST re-priced that way drew every meter's line
   at its bar's rate; the same leg with no band drew the house line at peak over a low-rate bar — the
   symptom, so it is the band the line needed. The capped days 26 Aug – 28 Oct (1,824 half-hours
   with energy) had no line off its bar, and every main-meter leg written 20–28 Oct carries a band.

**BL-86 — re-fetch Octopus's figures for a range without deleting it.** ✅ *Done 13–14 Oct 2026: the
engine (#155), the UI (#156), and its fixes (#159, #162, #163, #167, #170, #171).* Proposed 12 Oct 2026 (the
user). *Engine built 13 Oct 2026 (settlement surfaces PR 3): preview into memory, then apply only
what differs; the UI is PR 4.* Octopus restates history: #505's October VAT (not yet seen to happen) and 10 Oct's split
(seen, two days later). EMT asks once and keeps the answer, so a restatement only reaches it through a
purpose-built heal. A user's only lever today is to delete the blocks and import them again — which
destroys the recorded device history: a re-import brings back Octopus's half-hourly kWh, but device
attribution then comes from Home Assistant's recorder at its 1-hour long-term-statistics resolution
(BL-87), not the 10–30 s readings EMT recorded live. Wanted: a re-fetch of kWh, bill and split for a
range, compared against what is stored, applied only where it differs, leaving devices, segments
that did not change and every user correction alone. The general form of 4.5.22's resync and 4.5.23's
re-fetch; BL-85's context menus are its front end.

**BL-87 — apply recorder history automatically on a historical API import.** ✅ *Closed 29 Oct 2026
(the user), not built as asked: a first install already gets it.* Proposed 12 Oct 2026 (the user). A
historical import from Octopus brings the main meter's half-hours, not the devices'. Three pieces, each
built for another reason, give a first install its device history in either order:

- **Devices set up before the import.** A new device's save offers *Rebuild this device's history
  from Home Assistant now*, ticked by default (device sensors design §4). With no house history its
  sanity check passes — `attribution_preflight` compares only against a house with kWh — and the run
  keeps the recorder's hours in `ha_reads` (`record_recorder_hours`), writing no device row, as a
  device row needs a house half-hour (`_write_device_into_block`). The import's finish then rebuilds
  each device with hours kept in its span (#253), from those alone, by the house's shape.
- **The import first.** The same offer rebuilds the devices straight onto the imported history.

The one miss is *Later* on the offer: nothing is kept, and the device waits for *Rebuild history* on its
card — the user's choice, undone where it was made. A fetch from the import itself, as first asked, is not
built: it would skip the sanity check the offer runs (the wrong sensor, a Wh/kWh mix-up), and write two
years of a device's history unasked on a guess. `tests/test_first_install_device_history.py` pins the
chain (GUARDs: they pass today; a run kept from keeping its hours fails two of them).

**BL-88 — one data-tools surface, driven by the selection.** ✅ *Done 17–22 Oct 2026, through the Data
Management rework (data management design, #239): gap fill on the heatmap (#213), Delete… on it (#232),
the page rebuilt round it (#241), the selection's actions (#262); the sub-pages retired.* Proposed 15 Oct 2026 (the user, from
the PR 5 Pi check). Delete Blocks, Gap fill, Historical import, Corrections and the re-fetch each have
their own page and range inputs, so fixing an issue means remembering a date from a chart and typing it
again. Wanted: every tool an action on a heatmap selection, in one shape — preview, a required backup,
apply. Gap fill's long-period engine is kept. Draft: `docs/design/EMT-v5_data_tools_design.md` §1.

**BL-89 — a data-quality strip on Usage Stats.** ✅ *Done differently, 21 Oct 2026: the strip is Data
Management's health strip (#241, #248, #254), not one on Usage Stats (data management design).* Proposed 15 Oct 2026 (the user). A thin strip by the
date axis, one cell per bar, coloured by the worst settlement state; clicking it opens the heatmap at
that day or month, selected. Needs a heatmap deep link (none exists) and a per-bar settlement summary.
Draft: same note §2.

**BL-90 — soft delete.** ✅ *Done 19–21 Oct 2026 (soft delete design): HA's reads kept apart (#229, #230), a
refill rebuilding its devices from them (#231), whole half-hours deleted from the heatmap (#232, #253),
an imported export deleted on its own (#228).* Proposed 15 Oct 2026 (the user). A delete today removes the half-hours recorded
live from HA — the blocks and HA's registers at their edges — and they cannot be got back (HA's
recorder keeps detail ~10 days; `reads`, once thought lost with them, has never been written). Wanted:
*Delete* hides and keeps the rows, *Restore* brings them back, *Delete permanently* runs today's delete.
A schema change and a reader audit. Draft: same note §3. 18 Oct 2026: with deleting by channel
(the user, 17 Oct), `docs/design/EMT-v5_soft_delete_design.md` (draft): per-channel delete dropped on 19 Oct
except deleting an imported export; on 20 Oct the shelf (18–19 Oct) dropped for HA's reads kept in a
table of their own, removed only with the device, a recorder hour kept as the hour; a live block points at its sensor's `device_sensors` entry (device sensors design §7).

**BL-91 — devices that cost more than the meter.** ✅ *Done 31 Oct 2026 (device cost check design).*
Found through Usage Stats' Import column (#346). On one install, three half-hours on 14 Sep 2026 had
been corrected by hand in v4 to the rate the bill agreed with. Their devices kept the peak rate × their
raw reading, and v4's 4.5.21 heal carried only their ex-VAT side. The bill total was right and the
device breakdown too high. Nothing repaired it, since every automatic pass skips a corrected
half-hour. Now:
- a settled half-hour whose devices cost more than the meter, by more than a thousandth of a penny,
  is flagged red on the data heatmap (`devices_exceed`);
- Re-fetch lists it, and its apply prices the devices again;
- the v4 importer reports what it carries of the kind, and writes nothing.

**BL-92 — an imported day's standing charge on the wrong day.** *31 Oct 2026, found with G2's draft
(E design §4a).* The staging copy's 31 Mar 2024 (a spring Sunday, CSV-imported) held that day's standing
charge and the next day's, and both local days read the total; its autumn Sunday held none. v4's CSV and
API imports filed the charge by UTC date, so in summer a local day's first half-hour (23:00 UTC the day
before) carried another day's. The API import's shows only at a summer price change, a day late.
- v5's CSV and API imports write each local day's charge on its own blocks;
- the v4 importer repairs imported history (transform 3e), and reports a CSV run it cannot prove;
- v5 needs no repair (the user): re-importing from v4 brings the history through the transform.

**BL-93 — rate brackets: where a device's usage fell by price.** ✅ *Done 31 Oct 2026 (rate brackets
design): the read (`/api/energy?fields=brackets`), and the view in Usage Stats.* A Usage Stats view: the whole meter's, the house's or one device's grid import in 2.5p
brackets of the rate it was charged (inc VAT), per day, month and year, with each bracket's kWh, £ and
average p/kWh. One rule for every tariff: the rate already carries IOG's dispatches and cap, the
time-of-use windows and Agile's price, and the stored band label is set only on Intelligent. No new
storage or Octopus calls.

## D. Surfacing performance

*Measure before optimising.* Target hardware is a Raspberry Pi 4 class box on SD storage.

**State:** `.build/soak.py` names 17 surfaces and judges each against `.build/soak_budgets.json`,
all 17 set from the tenth soak (#64). #46, #47 and #48 took the slowest: a year's bill **18.2 →
4.8 s**, the all-days load **11.6 → 2.8 s** (p50, Pi, under the soak's load).

**Queries per surface, asserted in CI (5 Oct 2026).** `.build/query_budgets.py` requests every soak
surface against a synthetic database (`tests/surface_fixture.py`: a main meter, an EV charger and a
battery) at 40 and 200 days of history, and counts the SQL statements each issues;
`tests/test_surface_query_budgets.py` asserts each stays within `.build/query_budgets.json` (9–31
statements) and is the **same at both lengths** — no query per day, block or period. Measured on a
production-like database too: the same shape (a month's, a quarter's and a year's bill all issue 27).
An N+1 injected into the all-days load is caught. A statement count is deterministic, so CI asserts it
exactly; time is not, so **time to first byte stays Pi-asserted** by the soak (17 budgets).

**Closed 5 Oct 2026, by decision:** queries per surface are CI-asserted (#102); time to first byte
is Pi-asserted by the soak's 17 budgets, since a CI runner's timings say nothing about a Pi on SD
storage. The original criterion asked for both in CI; the split is the accepted reading.

## E. Importer

*Ingest a backup at or above the floor version, currently **4.5.23**. Spec at
[design/EMT-v5_import_spec.md](design/EMT-v5_import_spec.md).*

Three things the importer needs from a source. **Two are new in 4.5.14; one already exists** — an
earlier draft of BL-73 proposed adding a duplicate of that one, before the 4.5.13 schema was
checked. What genuinely cannot be added later is a *new* marker, because a backup already written
can never be annotated:

| marker | from | why v5 needs it |
|---|---|---|
| `written_by` | BL-72 | the app version that last opened the database. **The §3 gate.** |
| `supplier` | **BL-73** | the normalised supplier registry key (`octopus` / `not-listed` / absent). **Not optional after all** — v4 is *not* Octopus-only: `not-listed` is a supported local-metering state, so it cannot be derived from `written_by`. |
| *account stamp* | **already exists** | `kraken_state.kraken_account_number`, with `get_db_account()` and `kraken_account_mismatch()` already implemented. No new field. |

EMT keeps the marker-gated heal mechanism and starts its own heal set from an empty one, and stamps
its **own** release version on the imported database — so "last opened by X means healed to X's
standard" keeps holding on the far side of the migration.

**The floor is the last release of v4 — not necessarily 4.5.14** (spec v6, 19 Sep 2026). 4.5.14 is
only the *earliest* a source can be, because it is the first release that stamps its version. v4 is
frozen to critical fixes after .14, and a fix that repairs already-written data adds a heal, so the
floor may end at .15, .17, wherever v4 actually stops. It is pinned when v5 is written.
**Currently 4.5.19** (spec v12, 1 Oct 2026): 4.5.15 carried #473's heal, 4.5.16 #481's, 4.5.17 the
VAT backdate heal (1 Oct 2026's 0% VAT), 4.5.18 that heal re-armed after 4.5.17's learner undid it, 4.5.19 re-armed again for sub-meter rows.
4.5.20 (2 Oct 2026, the gap-fill zero-opener fix, v4 #494) carries no heal, so the floor did not move.
**Raised to 4.5.21** (spec v13, 7 Oct 2026): three heals — the EV-priority re-split (a rebuilt block lost
`meter_type`, so a re-split gave a battery the EV's grid), the dispatch-only device re-fit, and the
corrected-device ex-VAT repair. A 4.5.20 database, or a v5 dev build of it (4.5.20-a21), is now refused.
**Raised to 4.5.22** (spec v17, 9 Oct 2026): the October VAT resync (v4 #505). Octopus's half-hourly
costs carried 5% VAT after the 1 Oct change; 4.5.22 detects Octopus's restatement and re-settles the
period. Because that waits on Octopus, a 4.5.22 database may still hold 5% October blocks, so v5
carries the detection and resync as forward code rather than relying on the floor alone.
**Raised to 4.5.23** (spec v21, 12 Oct 2026): the re-fetch of device breakdowns cached with no EV
against a completed dispatch (v4 #510). Octopus's first breakdown for 10 Oct put the night's charging
on the house and was corrected days later; v4 had kept the first answer. v5 ports the forward fix —
hold such a bill back for up to 28 days, keeping EMT's split — but not the one-off. A 4.5.22 database,
or a v5 dev build of it (4.5.22-a23), is now refused.

Why a version suffices: every heal is marker-gated and an ungated marker runs at startup, so
upgrading any older database runs every heal that release carries. **"Last opened by X" means
"healed to X's standard."** *An earlier design proposed a computed `heal_level` integer; dropped
19 Sep 2026 as unjustified — heals are historical, so a later release that adds one simply raises
the floor.*

Because only 4.5.14+ stamps a version at all, the importer supports **one schema era**: the
4.4.0–4.5.4 era (segments present, settled provenance entirely absent) cannot reach it. Three gates
collapse into one, the era-detection code is never written, and 4.5.12/4.5.13 databases become
**negative** fixtures — near-misses with every table the importer wants and no attestation.

**Gate verified against a real 4.5.14 database** (prod-dev, 21 Sep 2026): `written_by='4.5.14'`,
`supplier='octopus'`, account stamped — admitted, restore path prompts for an Octopus key. That is
the first genuine *positive* fixture; keep it. Two failure modes it exposed are now §9 gates: values
in `store_meta` are **JSON-encoded** (the column holds `'"4.5.14"'`, so a plain SELECT-and-compare
finds no marker on a perfectly good database), and versions must compare **numerically** — under
string comparison v5's own CalVer `2026.9.0` sorts *below* the v4 floor.

**Done when** (in addition to the spec's §9 gates):

- `FLOOR_VERSION` exists as a **single named constant**, no version literal anywhere in the gate or
  its messages.
- Versions compare **numerically**, never as strings — `"4.5.9" > "4.5.13"` lexically — and v4's
  `4.5.x` orders below v5's CalVer `2026.x.y`.
- The below-the-floor test **generates** its fixture by rewriting the positive one's `written_by`,
  so it keeps testing something after the floor moves.

**Enforced at restore since 4 Oct 2026** (`import_gate.py`, moved to `v4import/gate.py` on 10 Oct with the importer): gates 2 (the floor) and 4 (units) run
on the candidate file before anything is stopped, backed up or swapped, on every path that replaces
the live database — a backup zip, the flat share copy, and an upload (since 11 Oct 2026 one route,
`/api/restore/stage`, for a `.db` or a zip; `/api/import/from-zip` went with it). Until
then `written_by` was stamped and never read, so a restore took any v4 backup. Gates 1 (live copy +
integrity) and 3 (supplier) followed the same day; all seven admissible databases on hand pass all
four, and `quick_check` + `foreign_key_check` take under 0.5 s on the largest (79 MB).

> ⚠️ **Release-day checklist item, easily missed.** `FLOOR_VERSION` is pinned as the last step
> before v5's first release, from whatever v4 has actually reached by then, and the positive fixture
> is regenerated at that version — not carried forward from development. Until then it is a
> placeholder, and every post-.14 v4 fix that adds a heal bumps it in the same cherry-pick
> ([architecture §6](design/EMT-v5_architecture.md)).

Last deliberately: its contract is "write the EMT schema", so it cannot be finished until A–D have
settled what that schema is.

**BL-81 is closed, 10 Oct 2026: the `night` relabel is not wanted.** Octopus's own vocabulary
answered it. On 28 Sep 2026 it renamed the IOG-SMB buckets from `ECO7_DAY` / `ECO7_NIGHT` /
`EV_DEVICE_PEAK` / `EV_DEVICE_OFF_PEAK` to HOME STANDARD RATE / HOME LOW RATE / EV STANDARD RATE / EV
LOW RATE, and its app shows them that way: side × rate, with no "night". EMT's segments already hold
exactly that, the side as `attribution` and the rate as `band`, so house `off_peak` stays the word in
every era, on both sides of the import. No per-block era, no relabel pass, no transform 4. The
clock-versus-dispatch reading the spec argued for (§5.2) is still true of the tariff, but nothing reads
it from the label, and Octopus has stopped naming it.

**Where the band vocabulary does have to move: EV_BOOST.** Octopus added it to its rate-type enums on
the same day; no product carries it yet. `bucket_side` now books any boost spelling to the EV
(10 Oct 2026; `EV_BOOST` was refused and `EV_BOOST_PEAK` booked to the house before) and logs the
first one it sees. When boost actually appears in a bill, the EV band word for it (`boost`) and the
readers that test an EV band by name (`energy_charts` checks `peak` / `mixed`) are the work — an open
label on one attribution, not an era.

*What follows is the analysis that closed it.*

**BL-81 — the house `night` band needs a per-block tariff era first.** Proposed 10 Oct 2026, split
out of the importer's PR 3 (E design §5, §10). The importer can relabel an imported SMB-era house
`off_peak` segment to `night` (import spec §5.2). But the relabel only holds if v5's own writers say
`night` too, and v5 cannot tell which tariff era a past block is in. A trace of every writer of a house
segment's band (10 Oct) found about ten paths:
- the cap classifier (`iog_cap.classify_slot`), and the seam (`_apply_iog_split`);
- `_build_block_segments` / `segments_from_legacy`, and `_refit_held_segments`;
- the carve, settlement from the bill (`apply_measured_to_block`), back-attribution;
- reconcile, three ways;
- the whole-history sweep.

Every one decides "capped / SMB" from the **current** tariff (`ev_device_*` schedules,
`_import_is_smb_capped`), not the block's own date. The sweep also rebuilds a block's segments from
a fresh channel and discards the band the bill gave it. All of them write through
`BlockStore._replace_segments`.

The likely shape: one labelling rule at that write point, keyed to a per-block SMB era that the
importer shares. The importer's rule takes the era from the earliest four-bucket `measured_cost`
row, decided 10 Oct. Open: how a new SMB account labels its first days, before any four-bucket row
arrives; and whether the era should come from the agreement history when the API is connected.
Until BL-81, v5 and the importer both keep house `off_peak` in every era. One label, so nothing
writes two.

*Measured on the 10 Oct 2026 soak:* with every segment stripped (the soak prep), the sweep rebuilt all
37,415 blocks, and 24,617 of 38,048 segment rows came back different from the originals — mostly 2023
CSV history, a house `off_peak` at the clean rate (0.0749994) rebuilt as `standard` at the cost-derived
rate (0.07497; BL-80: there `imp_cost` is not kWh × rate). A real import keeps its segments, so this is
the sweep's rebuild path, not the importer — the same weakness: a band the sweep cannot re-derive is
lost.

*Checked while tracing (10 Oct 2026), and not a defect:* the sweep can rebuild a held block's
segments without touching `imp_cost`, which looked as if it would let the segments drift from the
bill. On three real databases no reconciled block drifts at all. 18 settled blocks do, by up to 2.6p:
all are 1–6 Oct 2026, single-rate, and in the ratio 1.050 exactly. That is v4 #505 (Octopus's
settled cost still at 5% VAT), not the sweep.

---

## F. Carried from v4's backlog

*v4's open backlog (its `docs/ROADMAP.md`, 4 Oct 2026), assessed against v5's code. v4 takes
critical fixes only, so everything below is v5 work; the v4 entries point here. Each keeps its v4
number. Order is the agreed one — deletions first, while they are cheapest.*

| # | Item | Kind | State in v5 (checked 29 Oct 2026) |
|---|---|---|---|
| — | **BL-50** — unify user-job mutual exclusion | architecture | ✅ **is workstream B** |
| 1 | **BL-33** — remove the one-time legacy migrations | deletion | ✅ **done 4 Oct 2026** — #78 (below) |
| 2 | **BL-61 (1)** — remove the SMB rate-repair migration | deletion | ✅ **already done, 28 Sep 2026** (1156508; guarded by `tests/test_v4_heals_removed.py`) — §F's first draft said otherwise without checking |
| 3 | **BL-63** — two pricing models: adopt the live one | deletion | ✅ **deletion done 4 Oct 2026**; the rounding residue done 29 Oct 2026 (below) |
| 4 | **BL-74** — retire the `needs_review` writes nothing displays | deletion + fix | ✅ **done 4 Oct 2026** — all of it, and the review list with it (below) |
| 5 | **BL-61 (5)** — rename `rate_source` `'measured'` → `'settled'` | schema | ✅ **done 10 Oct 2026** — schema 2 (205a973); the importer maps the value |
| 6 | **BL-76** — proportional device grid split | correctness | ✅ **the split built 29 Oct 2026** — proportional after the EV, in whole watt-hours, one rule (`settle.device_grid_shares`) for PASS 2 and settle() (#331); a battery sensor counting solar flagged on every page, its data unchanged, and the setup text (the evidence #333; the flag #334; `EMT-v5_battery_sensor_and_device_split_design.md`) |
| 7 | **BL-75** — settlement visibility | correctness | ✅ done (below) — (e) the corrections refused with the supplier API, and the modal's note (29 Oct 2026); (d) closed, not built |
| 8 | **BL-47** — auto-attribution after a gap fill | feature | ✅ **closed 29 Oct 2026** (the user) — a refill rebuilds its devices from HA's kept reads (#231, BL-90); an outage's has nothing to rebuild from (below) |
| 9 | **BL-60** — block inspector | feature | ✅ **closed 29 Oct 2026** (the user) — the heatmap's detail panel (#154) is it |
| 10 | **BL-64** — a pypdf bump gate | tooling | ✅ **done 30 Oct 2026** — `.build/pypdf_gate.py`, run against bills kept outside the repo; pypdf bumps on their own PR (below) |

**Deletions (1–4).** The floor (E) is what makes them safe: every database v5 admits was opened by
4.5.19, so every one-time migration and heal in that range has already run. They go under the
deletion protocol ([architecture §4a](design/EMT-v5_architecture.md)), and two checks come first:

- **Done-markers on real databases.** Confirm each migration's marker reads done on the
  production-like and dev databases before deleting its path; the segment backfill in particular
  may share code with the live sweep.
- **Reachability, re-run.** BL-63's first count ("9 of 11 `pricing_segments` functions dead") was
  wrong in the direction that deletes working code — 8 are live. Trace from real entry points,
  over-approximating, not by grep. BL-63's rounding residue is done (29 Oct 2026): the
  reconcile compares bands by nearness (#60), and no other exact-equality comparison against a
  canonical rate remains (below).

**BL-33 — done, 4 Oct 2026 (#78).** Three removals, one commit each, each with its evidence:

1. **The three legacy historical backfills** (ex-VAT, IOG-split, segment), their schedulers,
   `_net_alarm` and `_reprice_sweep_done` — unreachable, and `engine_startup` called them a "dormant
   net, to be deleted a release later". 524 lines of `engine.py`. The house/EV split for a late
   completed dispatch is unaffected: that is `run_iog_split_carve` and `_attribute_missing_ev_split`.
2. **The one-off stale ex-VAT repair** — reachable but provably done: v4 4.5.14 runs it at startup at
   the same scope, and all 108 databases on hand stamped 4.5.14+ read done.
3. **The sweep's pence repair**, paired with a new **import gate** (import spec §3 phase 1, gate 4):
   refuse a source when any priced main block's (inc ÷ exc) ÷ (1 + VAT on its date) exceeds 10.
   Healthy blocks measure 1.000 (worst 4.8%); a pence block ~100. The write-point unit guard
   (`_sanitise_inc_units`) stays.

**`run_settled_ev_split_heal` — traced, kept; removed 16 Oct 2026** (bill and split paths design
§7.3: v5's settle takes the bill's split, the importer gives imported history it, so the heal had
nothing left but rewriting history at runtime). The 4 Oct note: Not a one-off: no marker, runs every dispatch-capture
tick, and its candidate set is empty on every real database checked. The trace found a defect instead:
on the 12th Pi soak it "restored" the same 20–23 blocks every hour, changing nothing — where the
dispatch ceiling is a known zero the settle keeps a block house-only (measured is king), and the heal
re-settled it each pass. Fixed in the same PR as this note: the heal asks the settle's own rule
(`_measured_ev_kwh`) first, and counts only a split actually written.

**BL-63 — the deletion done, 4 Oct 2026.** Option 2, adopt the live model, in three commits:
`reprice.py` and its test consumers (876 lines, including the adapter differential that existed to
judge option 1 and a fixture generated through `reprice_block`); `carbon_from_reprice` and
`house_carbon`; and `pricing_segments`' three unreachable builders, **moved** to
`tests/segment_helpers.py` rather than deleted, because tests of the eight live projections build
their segments with them. **`ev_carbon` is live** (`period_ev_saving` calls it) — the architecture's
module table had listed it for deletion; corrected. **The rounding residue — done 29 Oct 2026.** The
worry: derived rates back-computed from a 6-dp rounded cost rather than carrying the canonical band
rate, so an exact-equality comparison against a canonical rate could be defeated. #60 fixed the one
that caused BL-62 (the reconcile's band test). The sweep found none left in pricing: settle() and the
API import take the schedule's rate at 6 dp (`_band_rates`, `_tariff_rate_for`), a single-rate
half-hour storing it exactly and a blend computed from the unrounded sum; cost ÷ kWh is only a
fallback; `_sensor_band`, `_imported_cost_band`, `is_off_peak` and the published-tariff check all
compare with a tolerance against the schedule. v5's own rows (staging from 1 Oct 2026) carry one rate
per band. What remained was v4's history on the rate-keyed displays: one band at two precisions
(Octopus's published 0.323092 and exc × 1.05, 0.32309235) and blends worked back from a rounded cost
(0.290211, 0.290227, …). The bill card keys rates at 4 dp and folds above 5 rows, so the
Agile-export user's import for Nov 2024 – Jan 2025 read one "avg of 6–8 rates" row. Now rates within
`bill_card.RATE_MERGE_EPS` (1e-4 £, a unit of the 4-dp rate shown; not chained) are one row on the
bill card, the day panel and Insights' tiers, and a rate row of 0 kWh and £0 is not shown or counted
(the day panel already dropped it); the fold's "avg of N" counts the rates themselves. On the Agile
copy those three months show their four or five real rates. Insights' main tiers were already right
there — rebuilt from the segments (P4.2) — so only its device tiers (staging's battery, Mar 2026) and
a period with a half-hour lacking segments change. The goldens moved only by the dropped empty rows
and the fold's N (187 answers, 0 otherwise).

**BL-74 — done 4 Oct 2026, wider than v4 scoped it.** Nothing in v5 sets or clears `needs_review`
now: the rogue-block clamps stop flagging (the clamps and warnings stay); settlement reports drift
for the poll log and no longer flags it (the old defect: a cost-settled block re-flagged on every
poll); and the dispatch reconcile's **review list goes too** — an ambiguous band is decided by cost
settlement when Octopus bills the block, so the Corrections page's "Flagged for review" card, its
banner, `/api/review-blocks` and the store's review methods are deleted. The three startup
self-heals (#307 spike, register dip-and-recover, BL-19 grid invariant) are deleted on the floor
argument — their markers are set in every admissible database — with their write-point guards kept.
**No stored data changed:** the review columns, their index and every row are left as they were.
*Fixed after, 4 Oct 2026:* `append_block_replace` now carries the review columns, and
`finalised_from_cad` and `derivation_id`, from the stored row — a round-trip rewrite reset all five.

**BL-61 (1)–(4) — re-checked 4 Oct 2026.** (1) went on 28 Sep with the other v4 one-off heals
(`run_smb_rate_repair`, `run_smb_device_recost`, `run_smb_ev_resplit`, `run_band_rate_snap`), on the
import-floor argument `test_v4_heals_removed.py` records. The rest are not floor-safe deletions:

- **(2) the `chart_emit` house-TOU override is live** — the Billing tab's day charts reach
  `chart_emit.day_rate_series(…, house_tou=…)` through `energy_charts.day_chart_data`. BL-61's case
  for retiring it is that the stored idle-slot rate is already the clean rate, so the override only
  re-affirms it. That is a surface change no test covers, so it needs a before/after of day charts on
  a real database (architecture §4a) before the override goes.
- **(3) the single charting API** is partly C's: every surface reads `/api/energy`, but day charts
  still draw their rate lines through `chart_emit`.
- **(4) the import-recovery ladder** — *done 22 Oct 2026.* `recover_measurement_costs` asks the
  half-hour's local day, then its month; `lookahead_ladder` is gone. Proved against the live API
  (`.build/probe_window_shapes.py`, the user's March 2025 statement): the ladder priced the middle
  of a long evening charge at the day rate; the day matched all 48 half-hours.

**BL-61 (5) — the rename.** Deferred in v4 because it forces a data migration of every `'measured'`
row. In v5 it belongs to schema step 5, and the importer writes `'settled'` from a v4 `'measured'` —
nothing in v4 migrates. BL-61's (2)–(4) (the `chart_emit` house-TOU override, the single charting
API, the import-recovery `lookahead_ladder`) are re-checked against what C left: `chart_emit.py` still
has the `house_tou` override path — whether anything still reaches it is unchecked.

**BL-76 — proportional split.** `_apply_pass2` gives each device after the EV
`grid_remaining × (its kWh / Σ kWh)` instead of the greedy ranked draw — bounded, order-independent,
and bill-neutral (Σ device cost is unchanged). The EV pre-allocation stays. What v4 would have needed
as a heal becomes the importer re-deriving `imp_kwh_grid` from stored `imp_kwh` — no v4 heal, no floor
change. Delete the dead `unprotected` branch and decide on `kwh_battery` (computed, discarded) in the
same change.

**BL-75 — settlement visibility.** *Status, 29 Oct 2026:* (a) done — the pill names its stages (#150);
(b) decided — only an Intelligent agreement's half-hours are asked, a published tariff's being final by
its own rate (#319); (c) done — the ask record, its backoff and the 30-day give-up (engine step 3,
#293–#296; `abandoned`, #227); (e) done — a range re-fetch replaces a correction with the bill (#163), and the corrections are
CAD-only on the server as on the page (below);
(f) done differently — a re-fetch from the heatmap's selection (#156), not from the pill; (d) closed, not
built (below). Split into: (b) widen the cost fetch beyond IOG/INTELLI (still
gated at `measure_settled_dispatched_blocks`) and keep the agreement floor; (c) a per-slot attempt
count with backoff and a terminal state; (a) the pill reporting both frontiers; (e)/(f) the
corrections tool as last resort and retry from the pill. *(e) in part, 14 Oct 2026: a range
re-fetch can now replace a correction with the bill, so a correction no longer locks the bill
out for good — the corrections tool's own warning at the point of use is still open.)* **(e) done
(29 Oct 2026):** v4's worry was a correction shutting the bill out of a half-hour for good, so the tool was
to be the last resort, with a warning. v5 offers it only without the supplier API (data management design
§1 (3)), where no bill comes to be shut out, and the range re-fetch replaces a correction. The routes
held the rule only on the page: `/api/corrections/preview` and `/apply` now answer 409 `supplier_api`
in any API mode, and the API-settled gate they kept for an API account (`api_settled_only`,
`_corrections_unreconciled_count`) is gone with it. The modal says what replaces a correction: nothing
automatically; with the supplier API, *Re-fetch* on the data heatmap. On real data staging (an API
account) carries 35 corrections from v4, 30 Jun – 11 Sep 2026, all with DCC kWh and one with a bill
cached (agreeing); the other 34 have never been asked, as no automatic pass reaches a corrected
half-hour — left for *Re-fetch*, as the design decided for migrated corrections. A count of them on
the health strip would be a new feature (BL-88–90). **(d) closed, not built (29 Oct 2026, the user):** the
proposal was to stop stamping `finalised_from_cad` automatically past the horizon
(`finalise_past_horizon_blocks`) and stamp it only on the user's acceptance that the usage is
unrecoverable. In v4 the flag made a half-hour look final; in v5 it means only that EMT stops asking.
It is read by the poll's window (`get_oldest_unsettled_block_start`, the 24 Oct 2026 fix: one half-hour
that never settles had held every poll back to it), the sweep's early return and the unsettled count —
whose pill already floors at the 30-day horizon — and by no surface: `settlement_state` never reads it,
so the heatmap and detail panel still show the half-hour as EMT's estimate, red once overdue, and the
heatmap's range re-fetch (#156) asks Octopus directly and clears the flag on a settled answer. Nothing
is hidden, so there is nothing for an acceptance to gate. On real data neither staging nor the
Agile-export user's imported database has a half-hour flagged, or one of its own past the horizon
without DCC kWh. Should a red half-hour ever need accepting as final, that is a new feature (as
`abandoned` is for a bill), not this flag.

**BL-47 — closed 29 Oct 2026** (the user). Every refill but one rebuilds its devices or never loses
them: EMT down 12 hours or less, the startup gap fill (`build_gap_blocks`) spreads each device's register
delta over the gap; a refill over half-hours EMT recorded rebuilds them from `ha_reads` (#231). The one
left is an outage past the 12-hour limit, which the DCC poll backfills (`create_backfill_block`, "NO
sub-meter split") — and there HA's recorder has nothing to give: EMT is an add-on on HA's own box, so
the box was off. On real data it has not happened: neither staging nor the Agile-export user's database
holds a backfilled half-hour (`interpolated = 2`), and every main half-hour on a day the devices recorded
has its device rows, but for the day each install's devices began beside a CSV import. *If it is ever
wanted:* a device whose sensor is its own lifetime counter (a Zappi's, a battery's) still knows its
energy over the outage — the register delta — which could be spread over the backfilled half-hours by
the house's shape. A sensor HA integrates from power stops with the box and has no delta. BL-87 is not
this: a first install's history was recorded with the box on (closed the same day, §C).

**BL-64 — done 30 Oct 2026.** (BL-60, the block inspector, closed 29 Oct 2026: the heatmap's detail panel, #154, is it.) BL-60's design was in the v4 repository
(`docs/design/EMT-BL60_block_inspector_pre_post_settlement.md`). BL-64 is the pypdf bump gate only (29 Oct
2026, the user): bills and their snapshots stay outside the repo, and no bill's text is committed, redacted
or not; v5 pins pypdf 6.18.0. `.build/pypdf_gate.py` snapshots each bill's parse (the parsed bill and its
CSV rows) with the pinned pypdf, into the bills' own folder, and compares a candidate's against it; a bill
is named by a hash and a difference by its field, never a value, and a folder inside the repo is refused.
Dependabot raises pypdf on its own PR (excluded from the weekly group), merged once the gate passes —
[DEVELOPMENT.md](DEVELOPMENT.md) "A pypdf bump". The gate's logic is tested on stand-in bills
(`tests/test_pypdf_gate.py`); its first real run is the next bump.

---

## G. Local tariff store

*Proposed 4 Oct 2026. The agreements half is designed: G1, `docs/design/EMT-v5_G_tariff_agreements_design.md`
(28 Oct 2026). **The rates (G2) are dropped** (28 Oct 2026, the user) — see "G2, dropped" below; the rest of
this section records the problem and the shape first agreed, kept for the reasoning. **G2 is back, re-scoped**
(31 Oct 2026, the user): each agreement's standing charge and unit rates as dated price periods, for the
Usage Stats tariff overlay's hover only, Agile's unit rates left out —
`docs/design/EMT-v5_G2_tariff_prices_design.md` (built 31 Oct 2026: G2a–G2c). No pricing path reads it, so the reasons below do
not apply to it.*

**G2, dropped (28 Oct 2026, the user: "not sure what adding an additional complication is worth on an
unproven issue").** G2 would have stored Octopus's published rate records per tariff — not EMT's built
schedules — and re-settled what a changed past rate priced. Dropped because:

- **The change it guards against is unproven.** What Octopus has changed after the fact is the carve and
  the bill — the 10 Oct 2026 EV split, the April 2026 costs it withdrew, the bump/boost labels — not the
  rates. The one rate edit seen, the 1 Oct 2026 VAT restatement, does not reach v5: EMT prices VAT from its
  own calendar, not from Octopus's restated records, and a bill outranks the rate.
- **The cost is real.** On Agile every half-hour's rate would be stored twice — the published record and
  the block's priced rate — with a table, a diff and a guard to keep.

Two smaller things G2 would have given, should either be needed — neither needs a rates table:

- **A rate fallback with the API down at a restart** (non-Agile tariffs): keep the last good *built*
  schedule — a handful of periods — and price from it until a fetch answers.
- **Agile's fetch cost:** a running agreement's rates are fetched in full every 6-hour cycle (v4 #513) —
  on two years of Agile about 24 calls at 1,500 records a page. The process can keep the running
  agreement's earlier records in memory and fetch only the last few days each cycle; worth it only if an
  Agile account meets the rate limit.

**The problem.** EMT holds no copy of the tariff. Unit rates and standing charges are fetched from
the Octopus REST products API and kept **in memory only** (`_kraken_rate_schedules`, refreshed each
6-hour poll cycle and on connect), and so is the agreement history (`_kraken_discovery`; only the
account number is persisted). So:

- **an add-on restart while the API is unavailable leaves EMT with no prices** — the resolver returns
  nothing, and new blocks are written at a zero rate and flagged for repair;
- **there is no record of what a block was priced against.** When Octopus edited the 1 Oct 2026 rate
  records in place (0% VAT), nothing could see that a rate EMT had already used had changed — which
  is why v4 needed heals (4.5.17–4.5.19) rather than a re-price.

**The shape.** Two tables — tariff **agreements** (account, MPAN, direction, tariff and product
code, valid from/to) and tariff **rates** (tariff code, unit or standing, band, valid from/to, inc
and ex VAT) — each row carrying when it was fetched and what replaced it.

- **A cache of the supplier's truth, never a second truth.** Each refresh *replaces* the account's
  agreements and rates wholesale with what the API now says; the previous version is kept as history
  (fetched-at, superseded-at), not used for pricing.
- **Re-price when a rate changes.** Each refresh diffs the new version against the old. A changed
  rate record, a changed agreement window, or an agreement that disappeared gives an exact span; the
  blocks in it go on the existing re-price queue. This is what "Octopus edited it in place" needed,
  and nothing does it today.
- **A customer who backs out of an agreement** (cooling-off, a reversed switch) is the same case: the
  next refresh no longer contains it, the diff marks its span, and the span re-prices on whatever
  agreement Octopus now reports. Settled blocks keep the bill's figures (`rate_source`), as now.
- **Cadence.** Daily is enough — tariffs change rarely, agreements more rarely. The 6-hourly poll
  already fetches the rates, so persisting them costs no extra API calls. The refresh is a
  coordinator member (B), so it does not overlap an import or a delete.
- **Offline first.** Pricing reads the local copy; the API refreshes it. With the API down, EMT keeps
  pricing from the last good copy instead of writing zero rates.

**The importer (E).** A v4 database has no tariff tables. The importer fills them on the first
refresh after the user authenticates; until then the in-file checks apply (each block's own ex-VAT
figure — see BL-33 in §F). With them, a post-authentication gate can check stored rates against the
canonical published rate per slot.

**Also uses it.** The live write guard (`_sanitise_inc_units`, today a fixed £3/kWh ceiling) can compare
against the slot's canonical rate from the local copy, with the API down too.

**Watching for Kraken API changes.** GraphQL deprecations are covered: the client introspects the live
schema and warns (`check_field_deprecations`), with `.build/check_graphql_deprecations.py`, the field
watch baseline and their tests. **The REST products API — where the rates come from — has no schema to
introspect, so a REST deprecation would not be caught.** The store gives that a natural check: a
refresh that suddenly returns nothing, or a different shape, for a tariff that had rates yesterday is a
signal worth surfacing rather than a reason to clear the copy.

### G.1 Eras — future idea

*Recorded 4 Oct 2026, not planned.* Historical tariff agreements, stored locally, are eras: a span with
a tariff, a start and an end. Combined with config periods (which already split history) and
**user-defined periods** (a new heat pump, a house move, a change of habit), they give a set of eras to
compare — usage, cost and carbon per era, and like-for-like comparisons across them — as new Insights
and Usage Stats views, with no API call. The tariff store's schema should allow for this from the
start: agreements addressable as periods, and a user-periods table alongside.

---

## H. Before going public

*Items deliberately held until EMT is pushed to `main` and made public — nothing here is urgent.*

### BL-77 — Kraken schema watch: review the 5 Oct 2026 additions, and make the watch run for v5

**What happened.** v4's weekly GraphQL check failed on 5 Oct 2026 (run 37318049738, schedule on
`main`). The *deprecation* check passed; the **field watch** (`check_graphql_field_watch.py
--fail-on-change`) failed, as designed, on three additions — nothing removed:

| change | what it is | relevant? |
|---|---|---|
| `ContractedVolumeMeasurementPeriod` — new enum `WHOLE_PERIOD`, `DAILY`, `HOURLY` | cadence of a *contracted volume*; used only by `ContractedVolumePeriodType` and `QuoteContractedVolumePeriodType` | no — quotes/contracts (business supply) |
| `ApplicableRateType.params` — new `JSONString` | "raw parameters associated with the rate, e.g. step information for stepped rates" | unlikely — stepped tariffs |
| `TimeOfUseBandUsageType` — new `band`, `value`, `units`, `qualities` | under `TimeOfUseDevice` / `TimeOfUseDeviceRegister.timeOfUseUsage`: usage "organised into calendar periods and split by time-of-use band", with a reading-quality breakdown | **possibly** — Octopus's own per-band consumption would be an independent check on EMT's peak/off-peak split |

Introspection sees that the schema exists, not whether a domestic account may query it or whether
it is populated (the watch's own stated boundary).

**To do, when going public:**

1. **Refresh the baselines** — `check_graphql_field_watch.py --update-baseline` in v4 (a CI-tooling
   change: branch, PR, signed commits for `main`; not an app release) and in EMT (whose baseline
   is identical to v4's), so the check is green and watches from the new surface.
2. **Make the watch run for v5.** GitHub runs scheduled workflows from the default branch only;
   EMT's is `main`, and `.github/workflows/graphql-deprecations.yml` is not on it — so v5 has had
   no weekly deprecation or field watch at all. It runs once the workflow reaches `main`.
3. **Probe `timeOfUseUsage`** with an authenticated query on a real account: is it open to a
   domestic IOG account, is it populated, and do its band totals match the bill's? If so, it is a
   candidate settlement cross-check (or more) — a separate item then.

*Recorded 5 Oct 2026. Nothing in the additions is a removal or a deprecation, so there is time.*

---

## Open questions

Carried from the programme map §4 and the import spec §10. These need answers before the
workstreams that depend on them start.

- [x] **Does EMT publish anything to Home Assistant?** **No** (18 Sep 2026). EMT is read-only
      toward HA: it subscribes to sensors and creates no entities or statistics. The one namespace
      `instance.py` cannot arbitrate is one EMT never enters, so side-by-side is unconditionally safe
      on that axis. Accepted consequence: EMT's figures appear only in EMT's own UI. Reopens only if
      EMT ever starts publishing.
- [x] **Is on-demand computation cheaper than pre-rendering once?** **Withdrawn — false dichotomy**
      (18 Sep 2026). v4 does not pre-render once: the chart is a complete document inside an iframe,
      so the document is the unit of update and *any* change re-renders every year held. Scoping to
      the viewport is strictly cheaper at every size; there is nothing to trade off. See
      [C §5](design/EMT-v5_C_surfaces_design.md).
- [x] **What is the client?** **Defaulted: server-routed pages, no framework** (19 Sep 2026). The
      choice has no bearing on the API, the fingerprint, or the write-triggers-recompute fix — both
      options fetch from the same API over a viewport range and neither pre-renders anything. It
      decides only whether a nav link reloads the document. Pages keeps the client closest to v4,
      needs no bundler, and avoids SPA routing inside HA's ingress iframe. *Revisit only if* carrying
      date range and meter selection across navigation proves annoying enough to pay for tooling.
      See [architecture §8.1](design/EMT-v5_architecture.md).
- [x] **Read snapshot lifetime** — **per-request freshness**; `ReadStore` ends its read transaction
      between requests (19 Sep 2026). Was recorded in two places; now lives in
      [A §7.1](design/EMT-v5_A_storage_design.md) alone, with an acceptance test, because the
      failure is a coherent page showing stale numbers and no error anywhere. *(A)*
- [x] **How does a v4 user actually reach the importer?** **A first-run fork** (19 Sep 2026). Every
      earlier draft described what the importer does and never how it is reached, implicitly assuming
      a fresh install — the wrong default for the entire population E exists to serve. v5's first
      start now offers *new setup* or *restore from a v4 backup*, and on the restore path the backup
      drives the credential prompt rather than the reverse. Forces §3's gates to split: everything
      checkable from the file runs **before** the user is asked to authenticate.
      See [import spec §3a](design/EMT-v5_import_spec.md). *(client + E)*
- [ ] **Viewport range vs materialised period summary** — once scoped to what the user can see, is
      per-request computation cheap enough, or does the writer maintain an incremental summary?
      Benchmark on the Pi: TTFB for a month and a year, cold and warm, plus queries-per-surface.
      *(C/D)* — *Evidence so far (4 Oct 2026): per-request, a month's bill is 0.42 s and a full
      year's 4.8 s at p50 on the Pi under the soak's load (#62); no summary built yet.*
- [ ] **Test-suite portage** — wholesale (keeps the v4-equivalence proof) or rebuilt against the new
      API (suits the new shape)? Probably both, in that order. *(C)*
- [ ] **What is the first public release's bar?** "Imports my history and shows it correctly" is a
      far earlier date than "replaces v4 for daily use", and decides how much of B–D is pre-release.
- [x] **Is the `reads` table safe to drop?** **Yes — it is dead, not transient** (19 Sep 2026).
      Its five store methods have no caller outside tests; nothing has ever written a row. Goes in
      the unwired-paths inventory, not the import question list. *(E)* **Dropped 20 Oct 2026**: the
      table and its methods are gone; v5's own database drops its empty one on open.
- [x] **Does anything read the DB off the engine thread?** **No** (19 Sep 2026). v4's rule is
      offload network/CPU, keep DB access on the loop thread; the two workers that do touch SQLite
      each own their connection. v5's `store()` makes that structural instead of comment-enforced.
      *(A)*
- [ ] **Attribution state in a backup** — is a source with an unfinished attribution job importable,
      or refused? An earlier note leaned on the computed `heal_level` catching this for free; that
      mechanism was dropped, so the question stands on its own terms again — does a half-finished
      attribution leave the database in a state v5 would import wrongly, or does the ledger it
      carries make it recoverable? *(B/E)*
- [x] **Does v5's required version ever rise?** **Yes, and it is not a policy question**
      (19 Sep 2026). The floor is the last release of v4; it moves whenever a post-.14 critical fix
      adds a heal, and previously-admissible backups correctly stop being admissible. Nothing to
      decide — only a version to supply on release day. See E above.