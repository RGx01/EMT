# EMT v5 — Settlement surfaces (ROADMAP BL-85 parts 2–3, with BL-86)

*How EMT shows, per half-hour and per channel, what has settled and what needs attention, and how
a user acts on it. The model is ROADMAP BL-82's: a half-hour settles in three stages — its **kWh**
(DCC), then its **bill** (Octopus's cost for it), then on IOG-SMB its **House/EV split** — each on
its own clock, each able to be restated. Part 1, the Charts pill, is done (#150).*

**Status:** agreed 13 Oct 2026 — §7's five proposals accepted by the user as stated. Nothing is built.

---

## 0. What exists

**The Heatmaps tab already has the first layer.** For a supplier-API account, `heatmap_module.html`
adds two categorical views, *API import* and *API export*: each half-hour is green (DCC kWh
received), amber (none yet, under 48 h), red (none after 48 h) or grey (imported history, excluded),
with a per-day count of the half-hours still owed. They read `kwh_authoritative` /
`exp_kwh_authoritative` from `/api/energy?resolution=block`.

**The payload already carries most of the bill layer.** `BlockStore.get_energy_blocks` documents
provenance as "three facts, not one settled flag": `kwh_authoritative`, `bill_cached` (a bill is
held for the slot) and `bill_applied` (it was written onto the block; a device takes its parent's).
What it does not carry: whether the stored cost still equals the bill (BL-82), the bill's own kWh,
the split's source, or 4.5.23's held-back state.

**Actions exist, but none is per half-hour or per range of the user's choosing:**

| action | what it does | scope |
|---|---|---|
| Data Management → Retry settlement (`/api/retry-settlement`) | re-fetches DCC kWh from the oldest unsettled block to now | everything unsettled |
| Corrections → Review unsettled blocks | lists blocks without DCC kWh | everything unsettled |
| `/api/historical/reprice-repair` | re-queries Measurements window by window and re-prices **imported** half-hours; has a `preview` | imported history, a date range |
| `/api/historical/reprice-from-csv` | overlays an Octopus CSV's billed cost on imported blocks | imported history |
| Delete Blocks → reimport | deletes a range and imports it again | destroys recorded device history (BL-86) |
| v4 4.5.22 / 4.5.23 one-offs | the VAT resync; the re-fetch of house-only bills | purpose-built, run once |

**Measured on the 12–13 Oct backups:** 707 completed dispatches since 7 Jul, 7 with no split (10
Oct's six, one awaiting the carve); 255 recent settled blocks whose stored cost is not their bill
(BL-82, parked on October); 63 half-hours awaiting kWh, 0 awaiting a bill. So most of the grid will
be one colour, and the point of the views is that the exceptions stand out.

---

## 1. The states

One vocabulary, decided **server-side** and sent per main-meter half-hour, so every surface — the
heatmap, the detail panel, the pill — says the same thing (C's rule: the API states it, the client
draws it).

### 1.1 kWh — import and export (unchanged)

`imported` · `received` · `pending` (none yet, under 48 h) · `missing` (none after 48 h).

### 1.2 Bill — import

*Superseded where §9 differs (decided and built 14 Oct 2026).*

*Revised 14 Oct 2026 (the user, on the Pi).* The Bill view answers one question per half-hour:
**is the stored cost Octopus's, or EMT's kWh × rate?** Until a settled cost arrives EMT prices
from the CAD or settled kWh × the tariff rate; that figure is provisional inside the recency
window and a gap after it. The first version asked a different question — "is EMT waiting for a
bill?" — so a half-hour EMT never fetches a bill for (before the current agreement, or off SMB
an undispatched one) read grey, "no bill stage", when it holds only kWh × rate (9 Aug 2026 on the
soak: grid used all day, all grey).

| state | meaning | colour |
|---|---|---|
| `applied` | the stored cost is Octopus's: a fetched bill applied, or an **API import**'s cost (the importer reads Octopus's billed cost per half-hour) | green |
| `waiting` | kWh × rate (CAD or settled kWh), inside the window | amber |
| `overdue` | kWh × rate, past the window | red |
| `differs` | a bill is held and the stored cost is not it (BL-82) | red — §7 (c) |
| `corrected` | the user set the rate (Corrections) | blue |
| `imported` | a cost not from Octopus's API: **CSV** history | grey |
| `none` | no cost to settle: no import kWh in the half-hour | grey |

- **The window** is §7 (a)'s: 7 days after the kWh, measured from the half-hour + the kWh's
  2-day lag, as EMT does not record when the kWh arrived.
- **No tariff or agreement gate on the colour.** Which bills EMT fetches by itself (the current
  agreement's start, IOG/INTELLI, dispatched-only off SMB — `engine.bill_stage`) decides what
  turns green on its own, not what the cell says. A half-hour outside that scope that holds kWh ×
  rate goes red after the window; the remedy is a re-fetch (§5.2), which asks for its bill.
- **The pill counts something else, by design.** It counts the bills EMT is waiting for (floored
  at the agreement), so it does not count a red cell EMT will never fetch by itself. The view
  shows provenance; the pill shows the queue.
- `cached` (a bill held but not applied yet) is transient — the next pass applies it — and shows
  as `waiting`.

### 1.2b Bill — export

*Superseded where §9 differs (decided and built 14 Oct 2026).*

EMT fetches no export bill: an export cost is kWh × the export rate, final once the export kWh is
settled. So the export view follows the export kWh, on the same window:

| state | meaning | colour |
|---|---|---|
| `applied` | export kWh settled (DCC), or an API import's export cost | green |
| `waiting` | export kWh not settled, inside the window | amber |
| `overdue` | export kWh not settled, past the window | red |
| `imported` | CSV history | grey |
| `none` | no export in the half-hour | grey |

It differs from *API export* (§1.1) only in its window — 7 days to a cost, not 48 h to a reading.
Fetching Octopus's export statement (`PRODUCTION`) is a separate roadmap item; until then this is
what an export "bill" can mean.

### 1.3 Split — IOG-SMB, dispatched half-hours only

| state | meaning | colour |
|---|---|---|
| `n/a` | no completed dispatch (nothing for the car to have drawn) | none |
| `emt` | EMT's predicted split from the completed dispatch; the bill not in yet | teal — provisional by design |
| `bill` | the split is the bill's own buckets | green |
| `held` | the bill had no EV where EMT's split had the car; asking again (4.5.23) | amber |
| `kept` | 28 days on, still none: EMT's split kept | blue |
| `missing` | a completed dispatch, energy in the block, and no split at all | red |

### 1.4 Needs attention

A half-hour needs attention when any layer is red: kWh `missing`, bill `overdue` or `differs`,
split `missing` — plus two checks that are not a state of their own:

- **the bill's kWh ≠ the settled kWh** (0 cases on hand; BL-82 says flag, never recompute);
- **the bill's VAT disagrees with the calendar** (#505's 5% after 1 Oct; resolves on restatement).

These two add a red dot to an otherwise green cell, and name themselves in the detail panel.

---

## 2. The views

*Superseded where §9 differs (decided and built 14 Oct 2026).*

The Heatmaps tab's view selector gains two views beside *API import* and *API export*, for an
account where they mean something:

- **Bill** — import, §1.2. Shown when EMT fetches bills (an Intelligent tariff with the measured
  fetch on — the rule `engine.settlement_awaiting` already uses).
- **Export bill** — §1.2b. Shown beside it where the account has an export meter.
- **Split** — §1.3. Shown on IOG-SMB.

Same grid, same categorical drawing and legend as the API views; the day-total column counts the
half-hours amber or red. A cell's tooltip names the state in a sentence ("bill waiting — the
settled kWh is in, Octopus's cost is not yet"). No new page.

**Why not one combined "settlement" view.** Each stage has its own clock and its own fix; a single
colour per half-hour has to rank them, and the ranking hides the layer the user needs. Three views
of one grid cost nothing extra to read.

---

## 3. The payload

`get_energy_blocks` gains one object per **main-meter** row, `settle`:

```
"settle": {"bill": "applied", "split": "bill", "flags": []}
          flags ⊆ {"bill_kwh_differs", "vat_disagrees"}
```

Derived in SQL from what the row already joins plus three facts: `measured_cost.cost_incl` and
`.kwh` (vs `imp_cost`, `imp_kwh`), the block's EV segments (`ev_split.sql_kwh`), and a completed
dispatch (`dispatch_history`). The wait map (`kraken_state.bill_split_wait`) is read once per
request and applied in Python for `held` / `kept`. The tariff rule for `none` comes from the engine,
as for the pill. Devices get no `settle` — their provenance is their parent's.

The kWh layer stays on `kwh_authoritative` / `exp_kwh_authoritative`; nothing about it changes.

---

## 4. The detail panel

Clicking a cell (any view) opens a small panel for that half-hour — the evidence, not a verdict.
10 Oct 2026's 05:30 BST half-hour, from the dev backup after 4.5.23's re-fetch:

```
10 Oct 2026, 05:30–06:00 BST
kWh      2.738   DCC settled
bill     £0.1504 Octopus, fetched 12 Oct 07:05            stored cost £0.1504 ✓
split    EV 2.571 / House 0.167   from the bill            completed dispatch 2.60 kWh
rate     5.49p  low — the EV's dispatch, outside the 23:30–05:30 window
[Re-fetch from Octopus]  [Close]
```

It reads one new route, `GET /api/blocks/settlement?start=…`, which returns the three facts with
their sources and, where EMT stores one, a time. *EMT does not record when a half-hour's DCC kWh
arrived* — only the bill's `fetched_at` — so the kWh line says settled or not, with no time.
Read-only.

---

## 5. Acting on it (BL-85 part 3 — BL-86's front end)

### 5.1 Choosing what

- **One half-hour:** the panel's *Re-fetch from Octopus*.
- **A range:** drag across cells, or down the day column for whole days, across months if need
  be; then a context menu (right click, or a long press on touch) — *Re-fetch from Octopus…*,
  *Show details*. A tap on the day column offers that one day. The range
  is whole half-hours in local time; clocks-change days take their real 46 / 50. What a click, drag
  or day covers is outlined on the grid while it is held and while its menu or dialog is open.
  *(14 Oct 2026, on the Pi: a first version also made a range from two day-column taps; a kept
  first tap closed a two-month range unseen, so the day column now offers its day only.)*

There is one re-fetch, not three: the bill and its split are one Octopus answer (one breakdown
node), and the kWh is fetched with them for the comparison. The context menu offers it on any
selection; it does the parts that apply.

### 5.2 Re-fetch: preview, then apply (BL-86)

1. **Fetch into memory.** For the range: DCC consumption (kWh) and the device breakdown (bill +
   split, IOG-SMB) or the single-cost recovery (other Intelligent tariffs). Read-only, paced, on the
   coordinator (`job`), refused during a restore, an import or a delete.
2. **Compare and show.** A table, one row per half-hour that **differs**: stored → Octopus, for kWh,
   cost, EV kWh. Unchanged half-hours are counted, not listed. Nothing is written yet.
3. **Apply on confirm** — only the rows that differ, through the paths that already exist:
   `upsert_kraken_block` for kWh, `upsert_measured_cost` / `upsert_measured_breakdown` then
   `apply_measured_to_block` for the bill, the split heal for the split. 4.5.23's hold-back rule
   applies (a bill with no EV against EMT's split is held, not applied). A safety backup first,
   like Delete Blocks.

**Never touched:** device rows (they follow their parent through the existing re-split), segments
of half-hours that did not change, and anything outside the range.

**Before the agreement, and off SMB** (14 Oct 2026, with §1.2's revision): a re-fetch asks for the
bill of every half-hour in its range that holds kWh × rate — not only the automatic pass's scope
(the agreement's start; dispatched half-hours off SMB). A red cell outside that scope has no other
remedy. The automatic passes keep their scope.

**Corrected half-hours** (decided on the Pi, 14 Oct 2026): a re-fetch asks Octopus for them too,
and where the bill disagrees with the user's correction the row says *your correction*, the
dialog warns before Apply that a correction cannot be put back, and Apply settles the half-hour
from the bill — `rate_corrected` clears in the settle's own UPDATE
(`apply_measured_to_main(replace_correction=True)`), so a bill the block refuses leaves the
correction as it was. The first draft of this section listed corrected blocks as never touched.
That guard exists for EMT's automatic passes, which re-price unattended on every pass and would
silently overwrite a correction (v4 4.3.0, "so a manual correction is never stomped"); they
keep it. But `rate_corrected` is cleared nowhere else, so it also locked the bill out for good
(v4 ROADMAP_archive BL-75 (e)); a re-fetch is the user asking, which is the consent the guard
protects.

**What this replaces:** delete-and-reimport for a restatement. 4.5.22's VAT resync and 4.5.23's
re-fetch become special cases of it — the same fetch, compare and apply, run by EMT on a trigger
instead of by the user.

---

## 6. What it needs from elsewhere

- **BL-82's fix** before `differs` can mean "something to act on": today 255 recent blocks are
  `differs` only because of the re-cost BL-82 describes, and its fix waits on October (§7 (c)).
- **The pill** (#150) already counts by the same stage rules; `settle` is its per-half-hour form.
- No schema change. One payload field, one read route, one job-run write route.

---

## 7. Decided (13 Oct 2026, the user — all five as proposed)

a. **The bill's overdue threshold.** Bills on hand arrive 10–279 h after the half-hour, 68 h on
   average. Proposed: `overdue` at **7 days after the kWh arrived** — to come down if it proves
   generous (the user).
b. **Range selection on a touch screen.** Drag on desktop; on a phone, select days from the day
   column only (no half-hour drag). Proposed as stated.
c. **`differs` before BL-82 is fixed.** Red now would light ~255 cells for a known, parked defect.
   Proposed: show it as **amber with "known (BL-82)"** in the tooltip until the fix lands, then red.
   *BL-82 was fixed the same day (13 Oct): the re-run no longer re-costs a settled block. Blocks
   re-costed before the fix still differ — 32 on prod that afternoon, all September rounding, net
   £0.00 — so the amber "known (BL-82)" state is for those; a new `differs` is red.*
d. **Is the preview always shown?** Proposed: yes — even for one half-hour, the panel shows stored →
   Octopus before applying. A re-fetch that finds nothing different says so and writes nothing.
e. **Who may re-fetch how much.** A range is paced against Octopus's rate limit. Proposed: cap a
   single re-fetch at 31 days; longer ranges run month by month as one job with progress, like the
   API import.

---

## 8. Order of work

| PR | contents |
|---|---|
| 1 | `settle` in the payload; the *Bill* and *Split* views (read-only); tests on fixtures for every state. *Built 13 Oct 2026:* `settlement_state.py`, `BlockStore.get_settlement_facts`, `/api/energy?resolution=block&include=settle`, `/api/billing-source`'s `settle_views`. Two changes from §1: a stored cost within a thousandth of a penny of its bill is `applied` (float and rounding noise — prod's one other "difference"); and an attention flag outranks *known (BL-82)*, which had hidden the 5% VAT on 13 Oct's pre-restatement bills. The overdue clock runs from the half-hour + 2 days, as EMT does not record when the kWh arrived. |
| 2 | the detail panel and `GET /api/blocks/settlement`. *Built 13 Oct 2026:* a click or tap on a cell in any view opens it; `BlockStore.get_settlement_detail`. The Re-fetch button waits for PR 3. A clocks-back cell holds two half-hours; the panel shows the first and says so. |
| 3 | BL-86: the range re-fetch — preview and apply, as a job — with its tests against faked Octopus answers (a restated split, a VAT change, an unchanged range). *Built 13 Oct 2026:* `POST /api/settlement/refetch/preview {from, to, parts?}` → `GET …/status` (the differences and a token) → `POST …/apply {token}` (safety backup first); `engine.run_refetch_preview` / `run_refetch_apply`; job `range_refetch`, exclusive with import, delete and restore. Both parts by default (`parts`: kWh, bill). A kWh that differs is applied (the user, 13 Oct). Up to 31 days per preview; longer ranges month by month in PR 4. A preview expires after an hour or when its range is written. |
| 4 | the context menu and range selection, wired to PR 3. *Built 13 Oct 2026:* a click opens the panel; a drag selects a span; a right click (a long press) offers *Show details*, *Re-fetch this half-hour*, *Re-fetch the day*; the day column picks a day (a second day made a range until 14 Oct 2026 — see §5.1); the panel's *Re-fetch from Octopus*. A range over 31 days runs a chunk at a time — one with nothing different moves on, one that differs waits for Apply / Skip / Stop. Run in the browser pane against a fake restated Octopus on the 13 Oct dev backup: 10 Oct's 25 bills 5% → 0%, applied, the day reads billed. Found on the way: a quiet `refresh()` left a drawn month with a null grid (hover and click dead after the first poll); the apply route's check read the engine loop's connection from a web thread; the facts / detail queries joined every `measured_cost` row for a half-hour (now the latest fetched); the apply counted a bill the block refused as applied (now `not_settled`). |
| 5 | a Pi check on the dev install: the views over a month, a one-half-hour re-fetch, a one-day re-fetch |

PRs 1–2 are read-only and useful alone; 3 is the engine and testable without the UI.

---

## 9. Revision 2 — one Import bill view, gated on the API connection (decided and built 14 Oct 2026)

*From the Pi check (PR 5), the user's direction: the view is **Import bill**, shown wherever the
install has a supplier API connection, whatever the tariff; the tariff decides only what runs
inside it and what "final" means. It carries what *API import* shows today, so that view and *API
export* can go. A green cell whose split is still EMT's says so.*

### 9.1 Two kinds of tariff, one question

The question stays §1.2's — **is the half-hour's cost final, and whose is it?** What makes a cost
final depends on the tariff the half-hour was on:

- **Published-rate tariffs** (flat, Go, Cosy, Agile — every rate known in advance; EMT fetches
  Agile's 48 daily prices too): Octopus bills the settled kWh × that half-hour's published rate.
  Once the kWh is **settled** (DCC), EMT's kWh × rate *is* the bill, to rounding. Nothing a bill
  adds. This is how these tariffs have always reconciled: the DCC poll (`kraken_poll_task`) has no
  tariff gate, and the settled kWh re-runs the half-hour.
- **Intelligent tariffs** (IOG, INTELLI): a smart-charge dispatch can move a half-hour to the
  off-peak rate after the event, and on IOG-SMB the 6-hour cap decides how much. EMT cannot always
  derive that, so the cost is final only when **Octopus has costed it** — a bill, or an API
  import's cost. This is why the bill fetch exists, and why it is Intelligent-only
  (`measure_settled_dispatched_blocks`, `engine.py` ~11601).

**Per half-hour, by its own agreement.** A user who moved from Agile to Intelligent has both kinds
of history. EMT already holds the tariff history: discovery keeps every agreement with its tariff
code (`_kraken_discovery["import"]["agreements"]`), and `engine._agreement_era(channel, start)`
returns the tariff code live at a half-hour — the API importer's per-agreement coverage uses it.
The kind is read from that code (IOG / INTELLI → Intelligent; anything else → published-rate).
No agreement covering the half-hour (before the first, or discovery not run yet): the current
tariff's kind.

### 9.2 Which views, and what gates them

| view | shown when | replaces |
|---|---|---|
| **Import bill** (renamed from *Bill*) | the install has a supplier API connection (`mode_uses_api()` and credentials) — any tariff | *Bill*, *API import* |
| **Export bill** | as Import bill, and the main meter has an export channel | *API export* |
| **Split** | the account has a split to show — Octopus's device breakdown (IOG-SMB today) | — |

Inside them, the tariff gates only what must be gated:

| what | gated on |
|---|---|
| the settled kWh (DCC poll; a re-fetch's kWh part) | nothing — every tariff |
| the bill fetch, automatic and on a re-fetch (`recover_measurement_costs`; the device breakdown where the account has buckets, falling back to the billed cost — #167) | Intelligent half-hours only. A published-rate half-hour needs no bill, and **applying** one would be wrong there: `apply_measured_to_block` snaps the rate to the day's lowest or highest band (`day_rate_bounds`), a two-rate model — on Cosy a mid-rate half-hour, on Agile nearly every half-hour, would get a wrong rate and band (the cost would stay right) |
| the automatic bill pass's scope | unchanged: Intelligent, from the current agreement's start (`bill_stage`). No widening — published-rate tariffs reconcile through the kWh |

Today the view gate is the tariff (`_settle_views` shows Bill only when `bill_stage()["fetched"]`)
and so is the re-fetch's bill part; the view gate moves to the connection, the re-fetch's bill
part to the half-hour's own tariff kind.

### 9.3 Import bill — the states

| state | Intelligent half-hour | published-rate half-hour | colour |
|---|---|---|---|
| `applied` | Octopus's cost is held (a bill applied, or an API import's cost) | the kWh is settled — kWh × rate is final; or a bill / API import | green |
| `applied_emt_split` | Octopus's cost, but the split is still EMT's: a completed dispatch whose split is `emt`, `held` or `kept` (§1.3) | — (no split) | teal (the Split view's colour for EMT's split) |
| `estimate_dcc` | settled kWh × rate, Octopus's cost not in yet, inside the window | — (settled kWh is final: green) | light amber |
| `estimate_cad` | kWh not settled (CAD / HA reading) × rate, inside the window | the same | amber |
| `overdue` | past the window with no Octopus cost (either kWh) | past the window with the kWh still not settled | red |
| `differs` | a bill is held and the stored cost is not it (flags as §1.4) | the same, where a bill is held (an API import's, or one fetched before this rule) | red |
| `corrected` | the user's rate correction | the same | blue |
| `imported` | CSV history — a cost not from Octopus's API | the same | grey |
| `none` | nothing imported in the half-hour | the same | grey |

- **The window** stays §7 (a)'s: 7 days after the kWh, counted from the half-hour + the kWh's
  2-day lag. **Unsettled kWh turns red at the window, not at 48 h** (decided 14 Oct 2026) — the
  *API import* view's 48-hour step is not carried over.
- **A split `missing`** (a completed dispatch, energy in the block, no split at all) on a green
  cost is not teal: it stays in the Split view's red and §1.4's attention.
- The tooltip and the detail panel name which case a cell is (whose cost; which kWh).

### 9.4 Export bill — unchanged from §1.2b

EMT fetches no export bill; kWh × the export rate is final once the export kWh settles, on every
tariff. Green when settled (or API-imported); amber, then red at the window; grey for no export
or CSV history. It carries what *API export* shows, on the 7-day window instead of 48 h.

### 9.5 An assumption to verify

The per-half-hour billed cost (GraphQL measurements, `statistics` → `costInclTax`) carries no
tariff in its query, and the API importer has always used it ungated. **On hand it is proven only
on Intelligent tariffs:** the soak account's API-imported history (Sep 2024 – Aug 2026) has exactly
two import rates every month (five in March 2026, an agreement change). Under §9.1 a published-rate
half-hour does not depend on it — its settled kWh makes it final — so the assumption matters only
for an **API import's** cost on a published-rate tariff, which reads green either way once its kWh
is the API's. Verify on the first non-Intelligent install all the same (a one-day re-fetch, its log).

### 9.6 Decided (14 Oct 2026, the user)

a. Widening the automatic bill pass — **withdrawn**: published-rate tariffs reconcile through the
   kWh, and applying a bill there would mis-set the rate (§9.2).
b. Red at 48 h — **no**: unsettled kWh is red at the window.
c. *API import* and *API export* — **retired in the same change.**

### 9.7 Work — built 14 Oct 2026, in one change on top of #167

1. `engine.tariff_kind_at(start)` → `intelligent` | `published`, from `_agreement_era("import",
   start)`'s tariff code (the current tariff where no agreement covers it).
2. `settlement_state.classify` takes `kind_at`: `estimate_cad` / `estimate_dcc` from `waiting`;
   green on settled kWh for a published-rate half-hour; `applied_emt_split` from the split state.
3. `_settle_views`: Import bill and Export bill on the API connection; rename *Bill*; retire *API
   import* / *API export* (per (c)).
4. `_refetch_octopus`: the bill part for Intelligent half-hours only (its kWh part everywhere).
5. Legend, tooltips, panel text; tests for every state on both kinds; fold this section into
   §1–§2.
