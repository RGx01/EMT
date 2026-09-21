# Workstream C — one API for every surface

*Retiring pre-generated HTML and converging every surface on a single data API. The programme map
([EMT-v5_programme.md](EMT-v5_programme.md)) frames why this sits where it does; this document is
the design.*

**Status:** draft, 17 Sep 2026. Depends on workstream A (storage and access model).

---

## 1. What is actually wrong

Not the format. **A write triggers a wholesale recompute.**

In v4, 17 call sites schedule a chart regeneration (13 in `engine.py`, 4 in `web/server.py`), and
regeneration is wholesale: one `daily_usage.html` covering every day held. So one half-hour block
finalising re-renders two years of history. A burst of writes queues a burst of full re-renders
behind it, and a heavy read then contends with the writer on a shared connection.

The consequence is documented in `_schedule_chart_regen`'s own docstring:

> *"A synchronous render over 2+ years of blocks takes long enough to stall the HA WebSocket
> heartbeat, which drops the connection and re-runs startup: a reconnect→re-finalise→re-render
> storm."*

A heavy read starves the event loop, HA disconnects, reconnect re-runs startup, startup renders
again. That is a feedback loop, not a slow page.

**Five layers of compensation** have accumulated around that one coupling. Each is sound engineering
*given* the coupling; none should be inherited.

| # | Compensation | Provoked by |
|---|---|---|
| 1 | Renderer gets its own read-only `BlockStore` | the 3.1.3 cross-thread SQLite segfault |
| 2 | Render offloaded to a worker thread | render stalling the HA WebSocket heartbeat |
| 3 | In-progress guard + "re-run after it" / "change arrived mid-render" | renders piling on each other |
| 4 | Deferring reads to a calm period | readers contending with the writer |
| 5 | `_blocks_data_version` change token | telling a client its pre-generated file is stale |

Break the coupling and all five stop being needed. Layer 5 is the clearest tell: a token whose only
job is to detect staleness in a file that exists only because we pre-generated it.

---

## 2. The two rules

**Rule 1 — a write records that data changed. It does not trigger a recompute.**

Nothing renders. Nothing is scheduled. A surface asks for the range it is showing, when someone is
looking at it, and compares a **fingerprint** to know whether its copy is current. The mechanism is
specified in §2a.

**Rule 2 — a function whose output depends on tariff context takes that context as a required
argument, with no default.**

This is the lesson of the 4.5.13 `/api/charts/regenerate` episode. The scheduled renderer passed
`cap_from` and `import_schedule`; the on-demand endpoint silently did not, so `capped=False` and the
same function produced different charts depending on who called it. Two days went into diagnosing it
in the rendering layer, because the divergence was invisible at the call site.

`cap_from=None` meaning "render as if uncapped" is what made the omission silent. A single API
surface does **not** prevent this on its own — it reduces the number of callers, but one caller can
still omit context. The default is the defect.

---

## 2a. The fingerprint

### Two tempos, both slow

EMT does **not** surface the in-progress block growing. Every charting surface draws finalised data,
so there is no live tempo to serve and nothing needs a push or a second high-frequency endpoint:

| event | tempo | effect |
|---|---|---|
| block finalises | every 30 min | today's fingerprint moves — the user sees their half-hour arrive |
| settlement / reconcile | hours to ~2 days | a recent day's fingerprint moves |

A poll every 30–60 s over a viewport-sized range makes arrival feel immediate and costs essentially
nothing. No separate live endpoint and no push: there is no live tempo for them to serve.

> **Not the HA WebSocket.** That connection is EMT's *inbound data source*, carried unchanged —
> sensor subscriptions feed block derivation
> (`ha.subscribe_state(main_import_sensor, on_import_meter_update)`), HA recorder history feeds
> device attribution, and the overview page reads live state through it. The fingerprint poll is
> **browser ↔ EMT's own server** and never touches HA's socket. The constraint on the HA connection
> is that the event loop must not be starved — that is what delayed the heartbeat and caused the
> render storm — not how much traffic crosses it.

### Measured: routine change is recent and small

prod-dev, 07:00 → 20:28 on 17 Sep 2026 — 13½ hours of ordinary running:

```
NEW blocks      27    13.5h ÷ 30min = 27 — exactly the clock, nothing else
CHANGED blocks  20    all within 2026-09-16 → 2026-09-17
                      oldest changed block: 1.8 days old
```

**Nothing older than two days moved.** Reconcile scanned 267 candidates on all seven runs that day
and changed zero; `_measured_floor()` and the settle window bound it by construction. So a per-day
fingerprint is sufficient *and* cheap: today moves twice an hour, 2023 never moves.

### The mechanism

1. **Per-day fingerprint**, not one global token. v4's `_blocks_data_version` is global, so any
   change invalidates everything — a new half-hour would tell a user looking at 2023 to re-fetch four
   years. That is the wholesale problem migrated to the client.
2. **Derived from the rows in range, not declared by the writer.** Compute it from the data (a max
   change-counter over the blocks and segments in range). A job that forgets to declare its scope
   then cannot serve stale data; the fingerprint can only be slow, never wrong — and over a viewport
   it is not slow.
3. **It must cover everything displayed, not just totals.** A re-price can be **cost-neutral**: the
   4.5.13 band snap changed 24,128 rates with `cost_drift` of £0.0028. Totals identical, bands and
   rate lines different. A fingerprint over money alone would have shown that user a stale rate line
   indefinitely. This is precisely why v4's `_blocks_data_version` fingerprints segment *rates*
   (BL-32) — carry that property forward, do not rediscover it.
4. **An epoch counter for bulk operations.** One-time heals, imports and deletes legitimately touch
   all history and are rare and deliberate. Rather than expressing that through thousands of day
   fingerprints, bump a single epoch that invalidates all client state at once. These operations
   already sit behind the job coordinator (workstream B), which is the natural place to bump it.

Two levels, matching the two real shapes of change: per-day for the routine 99.9%, epoch for the
handful of operations that mean *throw everything away*.

---

## 3. What the API returns

One endpoint shape, serving every surface:

```
GET /api/energy?from=<iso>&to=<iso>&resolution=block|day|period&include=<facets>
```

The response is **priced blocks and their segments**, not chart geometry. Series, stacks, heatmap
cells and billing totals are all projections a client can make from the same payload; baking them
server-side is what produced two renderers that disagreed.

Every response carries:

- **`fingerprint`** — per-day for the range, plus the current `epoch` (§2a). Derived from the rows,
  covering every displayed field rather than totals alone. Replaces `_blocks_data_version`, and is a
  property of the data rather than of a pre-generated file.
- **`tariff_context`** — the agreement(s) covering the range, their bands and cap state. Explicit in
  the payload so the client never infers a band from a rate, and so the rule-2 argument has an
  obvious source. **Its band shape is a 2×2, not a pair.** A `(min, max)` rate range or a
  `day_rate_bounds` field assumes two bands; IOG-SMB has four rate buckets, so the context carries
  **four series keyed by *(attribution, band)*** — house·`day`, house·`night`, EV·`peak`,
  EV·`off_peak` — each valid over a stated interval. A two-band context cannot express an EV
  charging at peak while the house is on night, which is a real and billable state (§4).
- **`provenance` per block** — `rate_source`, whether a device breakdown is held, whether the block
  is settled. Surfaces differ today partly because each re-derives this; it should be stated once.

**Segments are the unit of truth.** v4 established this (BL-27) and the import spec depends on it —
legacy derived columns are dropped precisely because segments reproduce them. The API must not
reintroduce a parallel derivation.

---

## 4. Billing charts specifically

Billing is the surface with the least room for error: it is the number a user reconciles against a
real bill. Three constraints carry over unchanged from v4.

**Stored cost is authoritative.** `imp_cost` on a settled block *is the bill*. It is never recomputed
from `rate × kWh` — segments intentionally differ by a sub-penny sliver which the remainder absorbs.
The API returns the stored figure; the client sums what it is given.

**Band vocabulary is a 2×2, and the API must preserve it.** *(Corrected 18 Sep 2026 — this section
previously said "one word per band" and committed v5 to `off_peak` / `peak`. That was wrong.)* The
four words are **attribution × band**: house `day` / `night`, EV `peak` / `off_peak`, plus
`standard` for *unknown*. House day/night is clock-determined (Economy-7); EV peak/off-peak is
dispatch-determined. They are different questions with the same answers, and a billing surface whose
whole job is explaining *why* a half-hour cost what it did cannot afford to conflate them. v5 keeps
all four; the importer's only change is the era-scoped house `off_peak → night` relabel
(import spec §5.2).

The API must not collapse unknown into a band either — a rate equal to the day's low bound does not
prove the supplier billed it off-peak.

**A stated zero is not a missing value.** A bill that says nothing was drawn is an answer
(4.5.13/BL-70). Three states must remain distinguishable in the payload: figures present, stated
zero, and never fetched.

### What replaces the rate lines

The rate lines are the part of the billing chart with the most accumulated v4 logic — cap-gating,
held-last-rate behaviour, the house/EV distinction. That logic moves to the API as **explicit
per-half-hour band and rate series in `tariff_context`, one per *(attribution, band)* cell**, rather
than being reconstructed by a renderer from block rates. A client draws what it is told. This is the
direct fix for the class of bug where a stored rate a whisker off the published band caused a line to
plot peak across a half-hour the bill had charged off-peak.

It is also why the 2×2 must survive into the payload rather than being flattened at the edge: the
house and EV lines are **independent** and routinely disagree. A house half-hour can sit on `night`
while the EV on the same half-hour is billed at `peak`, because the clock and the dispatch are
answering different questions. A renderer given a single low/high band for the slot cannot draw that,
and would quietly draw one of the two lines wrong.

---

## 5. Why it is wholesale: the document is the unit of update

v4 does not pre-render once. It re-renders **everything, every time anything changes** — so
"on-demand versus pre-render" is not the trade-off it appears to be.

The cause is structural, not a choice:

```
charts.html embeds an <iframe>                      (.chart-frame-wrap iframe)
   └─ loads /charts/daily_usage.html                (server.py:2242)
        └─ a COMPLETE standalone document           (<!DOCTYPE html> … </html>)
             └─ so the whole document is the unit of update
                  └─ so ANY change re-renders EVERY year held
```

A half-hour block from four years ago that has not changed is re-rendered because the last few
blocks did. There is no mechanism to update part of the artefact, because the artefact is a document
and the document is atomic. The five compensation layers in §1 all exist to manage the cost of that,
none of them to reduce it.

**So there is nothing to trade off.** Rendering only what the user can see, plus a buffer, is
strictly cheaper than rendering four years — at every database size, on every machine. The question
is not whether to scope to the viewport; it is only what the buffer should be.

### Two independent wins

This workstream delivers two things easily mistaken for one. They are separable, and keeping them
separate clarifies what is being bought:

| | win | why |
|---|---|---|
| **Scope to the viewport** | performance | Stops re-rendering four years for a change to the last half-hour. Available *even if the server kept rendering markup* — it needs the iframe-document model gone, nothing more. |
| **Serve data, not markup** | correctness | One derivation instead of two. This is what stops two surfaces disagreeing about the same number, and it is the reason `billing.py` must come out of `energy_charts.py` first. |

v5 takes both, for different reasons. The performance win needs no measurement; the correctness win
is not a performance argument at all.

### What genuinely remains to measure

Once a request is scoped to a viewport range, one narrow question is left, and it *is* worth
benchmarking on the Pi rather than assuming:

> Is computing a viewport's worth of data per request cheap enough, or is a **materialised period
> summary, maintained incrementally by the writer**, warranted?

Note the fallback still respects rule 1 — the writer updates one period's row, it does not recompute
everything. Measure: time-to-first-byte for a month and for a year, cold and warm, and queries issued
per surface. A surface making 200 small queries is the likely shape of any problem, and is invisible
in a wall-clock figure.

---

## 6. Acceptance

- No pre-generated chart artefacts exist. `CHART_DIR` and its 28 call sites are gone.
- **A cost-neutral re-price invalidates.** Test: change segment bands without moving any total, assert
  the affected day's fingerprint moves. This is the property `_blocks_data_version` had and the one
  most easily lost in translation.
- **A change to today does not invalidate a historical day.** Test: finalise a block, assert only
  today's fingerprint moved.
- A write performs no rendering work. Demonstrated by a test asserting no render path is reachable
  from block finalisation.
- Every surface is served by the one endpoint; no surface has a private aggregation path.
- No function in the pricing/serving path accepts tariff context with a default value. Enforced by a
  test, in the style of the existing AST-based structural tests.
- Billing totals reconcile exactly with v4 over the same range on the same database — to the penny,
  using the stored costs.
- Per-surface time-to-first-byte budgets, asserted on the Pi.

---

## 7. Open questions

1. **Does the client re-aggregate, or does the API serve day/period rollups?** Serving rollups risks
   a second derivation path — the thing this workstream exists to remove. Re-aggregating in the
   client keeps one source but moves cost to the browser, which on a phone may be worse than on the
   Pi.
2. ~~**Push or poll for the watermark?**~~ **Closed by §2a: poll.** With no live tempo to serve,
   both real tempos are slow enough that a 30–60 s poll over a viewport range is negligible. This is
   a browser↔EMT decision and has no bearing on the HA WebSocket, which is a separate, inbound
   connection carried unchanged.
3. **How much history may one request cover?** An unbounded range re-creates the wholesale problem
   at request time instead of write time. A cap with pagination is probably right, but the number
   should come from the Pi benchmark, not from taste.
4. ~~**Does retiring `_blocks_data_version` lose anything?**~~ **Closed by §2a(3):** the property is
   carried forward explicitly — the fingerprint covers every displayed field, not just money, so a
   cost-neutral re-price still invalidates.
