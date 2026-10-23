# EMT v5 — Engine step 2: Octopus's band as evidence, the cost as kWh × rate

*Rewritten 23 Oct 2026 (the first draft, 22 Oct, had the import keep Octopus's COST as a bill; the
user's model, below, replaces it). Step 2 of the engine review (`EMT-v5_engine_review.md` §4, §5).
Every figure was measured on a staging copy (the support file of 22 Oct 2026, 141k blocks); every
code fact read at `dev`.*

**What this note decides:** how every half-hour — live or imported, any tariff — gets its cost: from
its kWh and its band, the band predicted by EMT and then settled by Octopus's own answer; what Octopus's
answer is kept as; what that lets go; and the order, with the safeguards of §3a.

---

## 0. Why (23 Oct 2026, the user)

> "The dispatch we record for EMT is the prediction before the rates land; we keep the history of
> dispatches as Octopus throws them away. The finalised EMT bill starts off as a prediction of CAD and
> any dispatch activity; when the settled kWh lands the prediction gets tighter; when the rates and
> bands are known the bill is settled. The machinery has got complex because we were trying to use cost
> as the band identifier, working around quirks that possibly don't exist in the form we thought."

**The cost only ever told EMT the band.** On staging's copy:

| measured | result |
|---|---|
| half-hours settled from an Octopus bill | 3,265 |
| … where kWh × a clean tariff rate equals Octopus's cost (≤ 0.05p) | **3,265** — £448.77 either way |
| … the rates they carry | the tariff's own: 5.493p, 5.2314p, 7p, 9p, 32.309p, 28.124p, 35.816p, 30.771p |
| IOG-SMB half-hours with home and EV both drawing | 342 |
| … billed at two different rates | **0** |

The bill apply already reads it so: band from cost ÷ kWh, the rate snapped to the clean tariff rate —
and then stores Octopus's cost anyway.

**EMT's own prediction gets the band right.** All 808 IOG-SMB half-hours with a breakdown (8 Jul – 22 Oct
2026), EMT's rule against Octopus's buckets: the night window 674/674, outside it with a completed
dispatch 129/129, with none 4/4 peak, a planned dispatch that never completed 1/1 peak. EV kWh outside
the night window: 267.5 predicted from completed dispatches, 274.9 billed (2.7%).

---

## 1. The model (decided)

**For every half-hour, on every tariff: cost = kWh × the band's rate** — the tariff schedule's rate for
that half-hour, as the live pricing already uses. Never Octopus's cost copied.

| stage | kWh | band |
|---|---|---|
| **predicted** | CAD (live) | the clock, the completed dispatches EMT recorded (kept: Octopus throws them away), the 6-hour cap, Octopus's four-rate rules — the pricing seam (`_reprice_main_import_block`, `_dispatch_overlay_rate`, `_apply_iog_split`, `iog_cap`) |
| **kWh settled** | DCC | the same prediction |
| **band settled** | DCC | **Octopus's**: on IOG-SMB the four buckets (band, split and cap fall out of them); on earlier Intelligent tariffs the half-hour's label, asked a local day at a time (#269 — the day matched the March 2025 statement 48/48); on a published tariff the clock, settled with the kWh |

**The split** (House / EV): the buckets' EV kWh on IOG-SMB; otherwise the completed dispatch, clipped to
the grid (PASS 2).

**Where Octopus's band contradicts its own rules — billed wins** (decided 23 Oct 2026, (a)): EMT shows
what was billed, flagged "Octopus's rules give peak/off-peak here", until Octopus corrects it and the
corrected band arrives. The known case: IOG-SMB appearing to bill all EV charging off-peak, an Octopus bug
the user expects fixed. (None of the 808 above showed it.)

**The prediction is never stored as a second answer;** it is a function of the facts (the dispatch record,
the tariff, the kWh), so the flag is computed when it is read — as the settlement states are.

---

## 2. What Octopus's answer is kept as

`measured_cost`, as it is — no shape change. Per half-hour: the **label** (cost-only answers) or the four
buckets' **kWh and unit rate** (IOG-SMB), `kwh`, `fetched_at`; the cost columns kept as Octopus's own
figure, read only to display and to check, never written into the block. The band is read from it in one
place: the bucket that carries the kWh (`bucket_side`, `label_band`), else the label.

**The import keeps Octopus's band too.** Its measurements carry the same label per half-hour; it writes it
to `measured_cost` in the chunk's own transaction (a bulk write — `upsert_measured_cost` commits per row),
never over an IOG-SMB breakdown already cached. A half-hour the import got no label for has its band
unsettled, and the drain asks Octopus for it by the local day (`_drain_asks`), backing off as every ask does.

---

## 3. What it lets go

| goes | why it existed |
|---|---|
| copying Octopus's cost into the block (`apply_measured_to_block`'s `imp_cost = cost_incl`, the band from cost ÷ kWh) | the cost was the band identifier |
| `_billed_rate` and the import's own pricing | the import priced from cost ÷ kWh |
| the VAT resync (#505) and its hint | copied costs carried Octopus's wrong VAT after 1 Oct 2026 — kWh × the schedule's rate never did |
| the in-pass recovery, the reprice queue, `repair_import_pricing`, the deferred verify pass, the pricing-health card and its demo, `/api/historical/reprice-repair`, `…/verify-status`, `…/health` | no-cost and "mislabelled" imported half-hours; a missing band is now just an unsettled one, asked |
| `rate_source`'s authority ladder between costs | one rule, applied the same way every time |

About 1,250 lines of `engine.py` (the repair industry's ~1,090, the VAT resync's 143, the apply's cost copy). **Stays:** the prediction seam, the one bill reader and its windows,
the hold-back (it is about the split, not the cost), the ask backoff, the cap arithmetic, per-agreement
coverage.

---

## 3a. Safeguards (22 Oct 2026, the user)

Step 1 changed only code and reverts cleanly (#272's revert tested: no conflict, the suite passed). The
state before the review is tagged `pre-engine-review` (0d88e68). Step 2 changes stored values, and
reverting its code does not unwrite them. So:

1. **A backup before staging takes each PR,** named for it (`pre-step2a` …) and kept until the next step is
   decided — restored, it is the way back for the data; the merge's revert is the way back for the code.
2. **Nothing goes until the parity is clean** and the user has seen it (§4, 2a).
3. **Additive first:** the new rule computed beside the old before it writes anything; the old path
   removed only after.
4. **v4 is untouched** — production runs v4; all of this runs on v5 staging.

---

## 4. Order

| PR | what | writes |
|---|---|---|
| **2a** | `settled_band(half-hour)` and `predicted_band(half-hour)` from the facts, and a parity report: for every main-meter half-hour, today's stored cost against kWh × rate(band), and the billed band against the predicted — run on staging, every difference listed | nothing |
| **2b** | the bill apply writes kWh × rate(band) from the settled band; the flag where it contradicts the prediction; the heatmap's "differs" becomes "Octopus's band differs from its rules" | the settled half-hours' cost and rate |
| **2c** | the import keeps the label as band evidence (bulk); imported half-hours settle through the same rule; no-label ones are asked | imported half-hours |
| **2d** | the removals of §3, the gap fill's finish with them | — |

**What 2a's report is expected to show**, so a surprise stands out: no cost change on the 3,265 settled
half-hours; changes only where a cached bill's band was wrong (the 11 evening half-hours of 5–7 Mar 2025,
£9.01 cached against £2.48 at the off-peak rate — the statement's figure); no VAT change (Octopus restated
October on 13 Oct 2026: none of the 295 cached October costs carries 5%); and on the imported half-hours, a
change only where `_billed_rate`'s cost ÷ kWh and the band's rate disagree.

---

## 5. For the user

- (a) **Export** fits the model as it is (the band is the clock; the cost kWh × the export rate). No change
  proposed.
- (b) **"Fix a date range"** goes with the repair (§3); the heatmap's re-fetch asks Octopus for a span.
  Agreed?
- (c) **Historical Import's pricing-health card** goes; a one-line note in the import's summary instead —
  "N half-hours came back without a band; EMT is asking Octopus"?
- (d) **The other instances.** The figures in §0 are one account (IOG, then IOG-SMB, a Zappi). Run 2a's
  report on each instance's database before 2b?

**Decided:** billed wins where it contradicts Octopus's rules, flagged (23 Oct 2026); one start-up backup
per version, the raw pre-migration copy (22 Oct 2026, review §6 (e)).
