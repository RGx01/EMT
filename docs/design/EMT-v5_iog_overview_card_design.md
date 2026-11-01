# EMT v5 — the Overview's smart charging card (BL-99)

*Status: decided and built, 1 Nov 2026 (§5). Code facts read at `dev` after #374; figures from the staging copy.*

---

## 1. What the user asked

*"The IOG view needs improving, the way data is presented is a bit confusing. We need to see how much
of the 6hr allowance was used, how long the session was, we need to see bump charges / charges
outside of a dispatch."*

## 2. What it is today

**🚗 Smart charging** on Overview (`live_power.html`), from `GET /api/charge-sessions`
(`_build_charge_sessions` over `dispatch_history`). The face is the latest session in one line:

> Completed 23.0 kWh · 01:30–05:59 · ≈3h 20m charging · 3h 59m dispatched ⓘ · 8 slots · all off-peak · saved £5.88

The modal lists 7, 30 or 90 days of sessions, each with the same line and a bar per half-hour.

**What is confusing, read from the page:**

- **Eight facts in one run-on line**, two of them durations ("≈3h 20m charging", "3h 59m
  dispatched") the user must tell apart through an ⓘ.
- **A session's window and its bars disagree.** Delivered half-hours up to the bridge apart join one
  session, so 27 Oct reads 03:00–06:30 with bars at 03:00, 03:30 and 06:00.
- **No allowance.** The 6 hours are counted per noon→noon cap-day (`iog_cap.cap_usage`); a cap-day
  can hold two sessions (26 Oct has two), and the card says nothing of either.
- **No cost**, only "saved", the saving against the day's peak rate.
- **Peak charging is "all off-peak" / "x kWh peak", with no reason**, and only for dispatched
  half-hours: charging outside any dispatch is not on the card at all.

## 3. What the data can say (the staging copy, IOG-SMB since 25 Aug 2026)

- **kWh:** the card's dispatch energy and the EV's billed share agree where billed (28 Oct: 23.02 kWh
  both; the EV's share is the bill's split, `block_segments.attribution = 'ev'`).
- **Cost:** the EV segments' £ — Octopus's where the bill has spoken, EMT's prediction until then.
  28 Oct: £1.20.
- **Allowance:** `iog_cap.cap_usage` — charged hours per cap-day, from the completed dispatches
  (energy ÷ inferred power, as the card's ≈charging). It is EMT's estimate of Octopus's count, not
  Octopus's own figure (the API gives none).
- **At peak, and why**, one reason a half-hour, in this order:
  1. **a bump charge** — a dispatch whose source is `bump-charge` / `boost` (none on staging);
  2. **over the 6 hours** — a capped agreement (`engine.capped_at`), the cap-day past its boundary;
  3. **outside a smart charge** — the EV charged in a half-hour with no completed dispatch (none on
     staging since July).

  "At peak" by **price** — above the agreement's off-peak rate — not by the stored band: on 30 Aug a
  daytime dispatch (12:00–13:00, 6.8 kWh) is banded `peak` at 5.493p, the off-peak price. *Flagged,
  not changed:* whether that band is right is the settlement's question, not this card's.

## 4. Proposed

### 4.1 The face: the last charge, in three lines

```
Completed · Wed 28 Oct · 01:30–05:59                       23.0 kWh · £1.20
▕████████████████████░░░░░░░▏ ≈3h 20m of 6h  (cap-day Tue noon → Wed noon)
All off-peak
```

- **Line 1:** status, day, window, kWh and its cost (*est.* until billed).
- **Line 2, on a capped agreement only:** the cap-day's allowance — a bar to 6 h, *over by 25m* in
  red past it. On an uncapped agreement the line is the charging time instead (≈3h 20m charging).
- **Line 3:** off-peak or not; any peak part in red with its reason (*1.2 kWh at peak, £0.36 — over
  the 6 hours*).
- **Charging outside a smart charge** in the range adds a line of its own: *Also charged outside a
  smart charge: 2.1 kWh at peak, £0.62 (Thu 17:00–18:00).*
- The next planned charge stays as it is.

### 4.2 The modal: grouped by cap-day

A header per cap-day — *Tue 27 Oct noon → Wed 28 Oct noon · 3h 20m of 6h* — and its sessions under
it, each: window, ≈charging time (the dispatched time on hover), kWh, £, the peak part and its
reason, the bars. The bars keep the half-hours' own times, and a gap the bridge crossed is drawn as a
gap, so the window and the bars agree. Charging outside a smart charge is a row under its cap-day.

### 4.3 The API

`/api/charge-sessions` gains, additively:

- per session: `cost`, `billed` (every half-hour's EV share from the bill), `peak_kwh` /
  `peak_cost` with `peak_reason` (`bump`, `over_cap`, `outside`), `cap_day`;
- `cap_days`: `{cap_day: {used_minutes, cap_minutes, over, capped}}` over the range;
- `outside`: runs of EV charging with no completed dispatch, `{start, end, kwh, cost, local}`.

No new storage, writers or Octopus calls: it reads `dispatch_history`, `block_segments` and the
agreements EMT holds.

## 5. Decided (the user, 1 Nov 2026)

1. **The face as in §4.1.**
2. **"saved £x" is dropped**, the cost shown in its place.
3. **The allowance as an estimate:** *≈3h 20m of 6h*.

*As built:* a peak half-hour with no named reason (neither a bump nor over the cap) reads *at peak*
without one. On the staging copy that is a few daytime dispatches' 0.05–0.08 kWh at the peak price,
inside a dispatched half-hour; the card does not guess why.

## 6. Order

One PR: the API's additions (§4.3), the face (§4.1), the modal (§4.2). Tests: a cap-day's allowance
from its dispatches; the peak reasons in their order, by price; charging outside a dispatch found
from the EV segments; the cost from the segments; the face on an uncapped agreement.
