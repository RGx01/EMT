# EMT v5 — Engine step 3: one queue of half-hours waiting for Octopus

*Decided 24 Oct 2026 (§4); drafted the same day. Step 3 of the engine review (`EMT-v5_engine_review.md` §4.4, §5). Every code
fact read at `dev` 419b701; line numbers are `engine.py` unless named.*

**What this note decides:** how EMT chooses which half-hours to ask Octopus about, how it remembers
each ask, and what that replaces. It does not change how a bill is read (`_fetch_bill_nodes`,
`parse_bill`), how it is applied (#279), or the hold-back's rule (`_bill_drops_ev`).

---

## 1. Today: three askers, four records

| asker | runs | selects | window | records it keeps |
|---|---|---|---|---|
| **live pass** `measure_settled_dispatched_blocks` (88 lines) | hourly tick | DCC-settled live half-hours **with a dispatch slot**, newest first, 40 a pass; gate: the **current** tariff is Intelligent | per the half-hour's kind (below) | `bill_ask`, `bill_split_wait` |
| **drain phase 1** `run_measured_history_drain` (160 lines with phase 2) | hourly tick, after an import or fill | every settleable half-hour with **no bill**, per Intelligent window, oldest first, pages of 40 | same | `bill_ask`, `bill_split_wait` |
| **drain phase 2** | same | IOG-SMB half-hours with a **cost-only** bill and a completed dispatch (no split yet) | same | `split_ask`, `bill_split_wait` |
| **range re-fetch** (293 lines) | the user | every Intelligent half-hour in the range | same | **none** |

Plus the import (60-day chunks, the only path that checks the rate limit), which writes bills and
`import_no_cost`.

**The window depends on the half-hour's kind, not the asker:** a cost half-hour by its local day,
then the month (`recover_measurement_costs`); an IOG-SMB breakdown in runs of under 12 half-hours,
no retry (`recover_device_breakdown`).

| record | shape | clock | read outside the askers |
|---|---|---|---|
| `bill_ask` | `{slot: {first, last, gave_up?}}` | give up 30 d after the first ask; hourly for 2 d, then daily | `bill_no_cost` → "abandoned"; `no_cost_since` (panel); `settlement_awaiting` (pill); the support file |
| `split_ask` | the same | the same (it uses `bill_ask`'s functions) | — |
| `bill_split_wait` | `{slot: {first, kept?}}` | kept 28 d after the **half-hour** | `bill_split_held` → split "held"/"kept"; `settlement_awaiting`; the v4 importer carries it |
| `import_no_cost` | `{slot: first noted}` | none | `bill_no_cost` → "abandoned"/asked; the drain's SQL (`_ASKED_OFF_SMB_SQL`) |

## 2. Where they disagree (each read in the code)

1. **The live pass and drain phase 1 ask for the same half-hours.** Phase 1's predicate covers every
   DCC-settled live block, dispatched ones included; the two can run at once (`_save_changes` exists
   for that race).
2. **A zero answer:** the live pass caches it as `ZERO` and calls it answered (10648); the drain
   calls it unanswered and asks again (11492).
3. **"Is there a bill?"** is asked for this MPAN (`measured_slots_missing`) by the live pass and any
   MPAN (`NOT EXISTS … measured_cost`) by the drain and the pill.
4. **A held half-hour skips the backoff:** it is taken off `bill_ask` before the hold (10647 → 10656),
   so it is asked every pass, hourly, for up to 28 days.
5. **The drain asks held half-hours, its backlog doesn't count them** (11476 vs 11595): one that isn't
   dispatched is asked only when other backlog starts the drain.
6. **The re-fetch reads and writes no record:** it re-derives "held" itself, writes bills outside
   `_store_bill` (so `import_no_cost` keeps the half-hour), and has no retention floor.
7. **Eligibility three ways:** the current tariff (live pass), each Intelligent window (drain), each
   half-hour's tariff (re-fetch).
8. **Who applies:** the tick (after the live pass), the drain over the whole backlog (phase 1), per
   half-hour (phase 2, the re-fetch).
9. **No asker checks the rate limit.** An API error inside the fetchers arrives as "no answer" and
   starts the 30-day give-up clock.
10. **The corrected test:** the drain tests `rate_source`, not `rate_corrected`; the others both.

## 3. The proposal

**One record per half-hour asked, one selector, one asker.**

- **The record** — `ask` per half-hour: `kind` (bill | split), `first`, `last`, `state` (waiting |
  held | kept | given_up), `reason` it was last asked (new | unanswered | held | user). Answered
  half-hours leave it. Replaces `bill_ask`, `split_ask` and `bill_split_wait`; their readers
  (`bill_no_cost`, `bill_split_held`, `no_cost_since`, `settlement_awaiting`) read it instead, with the
  same answers.
- **The selector** — one query, per half-hour on its own agreement (`tariff_kind_at` /
  `_intelligent_windows`), above the two-year retention floor, under the 6-hour settle gate,
  kWh > 0, not corrected (`rate_corrected` and `rate_source`):
  - **a bill:** no bill cached for this MPAN; or
  - **a split:** IOG-SMB, a cost-only bill, a completed dispatch;
  - minus records resting (the backoff), given up or kept. **Held follows the same backoff** (fixes §2.4).
- **The asker** — one pass, newest first, pages of a local day's half-hours, one rule for each
  answer: a bill → cached through `_store_bill` (§2.2: a zero answer with EMT's kWh at zero is a
  `ZERO` bill everywhere); drops the car → held; none → unanswered; then `_apply_bills_paced`.
  **Checks the rate limit** as the import does, and an API error is **not** an unanswered ask (§2.9).
  Started by the hourly tick and `_after_import_settle` — the live pass and both drain phases go.
- **A banner while it works through a backlog** (the user, 24 Oct 2026): after an upgrade or an
  import, the first passes can take hours (decided (b)) — the page says so, with how many half-hours
  are left and about how long, as the import's banner does, and the "waiting for a bill" counts are
  read against it.
- **The re-fetch** stays the user's action, with its preview and diff, but its answers go through the
  same writes and update the record (answered, held, released) — §2.6.

**Kept:** the bill reader, the windows, the hold-back's rule, the 30-day give-up, the 2-day-hourly-
then-daily backoff, the 28-day keep, the coordinator membership (`measured_drain`).

## 4. Decided (24 Oct 2026, the user)

- (a) **The record is a table of its own** — one row per half-hour asked (`kind`, `first`, `last`,
  `state`, `reason`); its queries and counts in SQL. The v4 importer maps `bill_split_wait` into it
  (held → `held`, kept → `kept`), and `bill_ask` / `split_ask` stay on its drop list as runtime
  state, rebuilt by the asking.
- (b) **Imported history is asked like any other** — no `import_no_cost` gate. Since #282 a newly
  imported Intelligent half-hour has its bill; history imported before it, and every v4 database
  migrated into v5, has none, so each such half-hour is a candidate and the queue asks Octopus for
  it: about one query per local day of imported Intelligent history, **at most ~730 per instance,
  once**, newest first, paced, under the headroom of (c). It settles itself over some hours, and the
  "waiting for a bill" counts read high meanwhile. `import_no_cost` goes. (Rejected: keeping the gate
  — that history would stay on its copied cost, outside the rule, for good; re-importing — ~70
  queries, but nothing would cover a v4 migrant who did not.)
- (c) **The asker stops at 25% of the allowance left**, the import's headroom, and resumes on the next
  pass; an API error is not an unanswered ask.

## 5. Order (each its own PR, the §3a safeguards of step 2)

*Done 24 Oct 2026:* 3a #291 (+ #292, what it read), 3b #293, 3c #294 (+ #295, a page in one commit),
3d #296, 3e. In 3e the import's no-cost record went too: an imported half-hour with no bill waits as a
live one does (light amber, red past 9 days, abandoned after 30 unanswered); the dry run stays, as
what the asker would ask now.

| PR | what | writes |
|---|---|---|
| **3a** | a dry run: for each instance, what the one selector would ask against what the live pass and the drain ask today, and why each differs — read-only, no Octopus call | nothing |
| **3b** | the record, filled from the three it replaces; their readers moved onto it | the record |
| **3c** | the one asker; the live pass and both drain phases go | bills, as today |
| **3d** | the re-fetch through the same writes and the record | — |
| **3e** | the three old records and their helpers go (~150 lines of helpers, ~250 of askers) | — |
