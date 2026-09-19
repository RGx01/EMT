# Assumption log — NOT COMMITTED

`/scratch/` is gitignored. This file tracks positions that changed during design, and why.
It exists so the design docs can state the current position cleanly without carrying a history
of how it was reached. If a rejected alternative is worth warning future readers about, it belongs
in the doc as a rejected alternative — not here. **Here is only: what I believed, why it was wrong,
and what corrected it.**

Kept because the *pattern* of error is more useful than any single correction: nearly every entry
below was corrected by a measurement, and most of the wrong positions were plausible.

---

## 2026-09-17/18 — EMT v5 design

### 1. "v4 shares one connection; v5 should give readers their own"
**Wrong.** v4 invented the read-only companion three times (chart renderer after the 3.1.3 segfault,
`_get_read_store()` after a SQLITE_MISUSE incident, the `read_only=True` constructor).
**Corrected by:** counting adoption — 3 call sites vs 75, and 41 of 88 GET routes still on the
writer. The problem was never the mechanism; it was that the safe path is opt-in.
**Landed as:** A's thesis — make the unsafe path hard to reach, not build the mechanism.

### 2. "BL-50's conclusion (serialise reads too) is right"
**Wrong.** That conclusion came from the 26 Aug `/api/config` 500 — but `/api/config` is one of the
41 routes that never adopted the companion built to prevent exactly that crash.
**Landed as:** A takes the opposite position, with the adoption figure as evidence.

### 3. "Normalise blocks to one row per channel; sparse wide rows cost pages per scan"
**Wrong on both counts.** SQLite stores NULL as a header type code with zero payload.
**Corrected by:** rebuilding the table three ways — normalising saves 5.6% and adds 28% more rows;
dropping derivable columns saves 14.3% with no row growth. User's objection ("good for writers not
readers") was right, and the read path already pays one join.
**Landed as:** schema notes §3, wide shape kept.

### 4. "Is on-demand computation cheaper than pre-rendering once? — measure it"
**False dichotomy.** v4 does not pre-render once; it re-renders everything on every change, because
the chart is a complete document inside an iframe and the document is the unit of update.
**Corrected by:** user pointing at the iframe; confirmed in `charts.html` / `server.py:2242`.
**Landed as:** C §5, question withdrawn rather than answered.

### 5. "Reconcile touches slots scattered across years"
**Wrong — conflated scanned with changed.** Reconcile scanned 267 candidates on all seven runs and
changed zero.
**Corrected by:** diffing two prod-dev snapshots 13.5h apart — 27 new blocks (exactly the clock),
20 changed, nothing older than 1.8 days.
**Landed as:** C §2a, per-day fingerprint justified by measurement.

### 6. "Three update tempos; the live block needs its own endpoint and maybe a push"
**Wrong.** EMT does not surface the in-progress block at all. Two tempos, both slow.
**Corrected by:** user, flatly.
**Landed as:** C §2a — poll, no live endpoint, no push.

### 7. "Don't push, because it would add traffic to the HA WebSocket"
**Wrong twice.** The browser never touches HA's socket (that's browser↔EMT), and the render storm
was caused by event-loop starvation delaying the heartbeat, not by socket traffic.
**Corrected by:** user noting the HA socket is used for the overview page and device attribution.
**Landed as:** architecture §1 records it as an inbound edge into the domain layer.

### 8. "Port module by module" → "refactor in place across 4.5.14–.17"
**Both wrong.** The first can't run the suite until the port completes. The second rides users
through the entire restructuring — inverting the premise that users stay on something stable.
**Corrected by:** user proposing copy-verbatim-then-deconstruct.
**Landed as:** architecture §6.

### 9. "BL-68 is the next free backlog number"
**Wrong** — already taken by the reprice-banner work. Next free was BL-70.
**Corrected by:** grepping the tree instead of trusting the session's task list.

---

## Recurring failure modes worth naming

1. **Measuring the formatted value, not the stored one.** Four times in 4.5.13 work: a UTC/BST
   comparison, a 4dp log line, a 6dp histogram, a rounded distinct-list. Always compare at raw
   precision.
2. **Pattern-matching where reachability is needed.** Greps classified `needs_pass2_rerun` as
   write-only when `_drain_pass2_queue` reads it. AST traces over-approximating are the sound method.
3. **Inventing a trade-off that isn't there.** #4 and #6 both proposed benchmarking a comparison
   where one side didn't exist.
4. **Single-account data read as universal.** `finalised_from_cad` is single-valued here only because
   this install runs `mini_provisional=off`.
