# EMT v5 — a battery sensor that counts solar, and the device grid split (BL-76)

*Decided 29 Oct 2026 (the user). Code facts read at `dev` 9c96752; the figures are from the Agile-export
user's v4 database (heat pump, battery, EV — the only database in hand with two non-EV devices).*

**What this note decides:** how a block's grid import is shared between devices (BL-76), and what EMT
does when a battery's sensor turns out to count solar charging as well as grid charging — which the
device rule forbids and which EMT cannot check at setup.

---

## 1. What happens now

- **The split exists twice**, and both copies agree: `engine._apply_pass2` (a block as it closes, a gap
  block) and `settle._devices` (every settled half-hour since engine step 4). The EV's grid is reserved
  first — an EV meter's reading, or with none the main's dispatch EV — then the other devices take what
  is left **largest first**, each `min(its kWh, what is left)`; a device's reading beyond its grid share
  is its non-grid supply. The grid boundary is the authority: devices are clipped to it once, at write
  time, and a device's own reading is kept as recorded.
- **The cliff (BL-76).** Two devices short of grid do not each get less: the larger is served first and
  the smaller absorbs the whole shortfall. On the user's history 3,475 of 37,283 half-hours (9%) are
  contested between the heat pump and the battery, and in 579 the heat pump gets no grid at all. A
  proportional share would move 23.2 kWh of grid, £1.53, from the battery to the heat pump over the
  whole history (the heat pump's grid cost +25%). The bill does not move.
- **`kwh_battery`** is computed on every device and only logged; it is `imp_kwh − imp_kwh_grid`.
- **The device rule** (CLAUDE.md): a pure consumer (heat pump, EV charger) reports energy *used*, and
  non-grid energy on it is normal; a **battery must report grid import**, because it is what creates
  the non-grid supply. Not every inverter offers a grid-charge sensor, and EMT cannot tell at setup
  which kind it was given: in winter a total-charge sensor reads like grid import.

**The user's battery reports its total charge.** Its reading as a share of the half-hour's whole grid
import:

| battery ÷ grid import | while exporting | not exporting |
|---|---|---|
| ≤ 1 (possible) | 121 | 1,193 |
| 1 – 1.1 (rounding, timing) | 4 | 249 |
| 1.1 – 3 | 8 | 0 |
| **> 3** | **1,357** | 1 |

A grid-only charge cannot exceed the grid import, exporting or not. The impossible half-hours are
almost all over three times the import — the panels (nearly all of them while exporting, as solar
would). The near-1 band at night is rounding and timing between the two meters' half-hours.

## 2. The split: proportional after the EV (BL-76)

The EV's reservation stays first — on Intelligent the supplier pulls cheap grid for the car, and
without it the car vanished from the grid view (v4 #212). **Every other device then shares what is
left in proportion to its reading:** `left × (its kWh / Σ kWh)` where the devices read more than is
left, else each takes its reading. Bounded (no device is given more grid than it read),
order-independent, no cliff.

**One rule, one place:** a pure function of (grid, the main's dispatch EV, the devices' readings and
which is an EV meter, whether the main is authoritative) → each device's grid share, called by
`_apply_pass2` and by `settle._devices` (which keeps its pricing by leg). `kwh_battery` goes.

**The case it accepts** (the user, 29 Oct 2026): a correctly configured battery's reading *is* grid,
so in a contested half-hour a proportional share can give it less grid than its sensor recorded — the
heat pump having run partly on the battery's discharge or the sun. Taken for the simpler,
order-independent rule.

**History** is re-split by the settle sweep (a new `_SETTLE_HISTORY_VERSION`): it reads each device's
stored reading and the main's kWh, nothing else.

*As built (PR 1, 29 Oct 2026):* `settle.device_grid_shares`, shared **in whole watt-hours** by largest
remainder — the meters read to 1 Wh, so the shares add up to exactly what is left, never past the main,
and every surface sums the same figures (shares with more decimals made Usage Stats and the Bill card
differ in the third decimal). `_SETTLE_HISTORY_VERSION` 2. Goldens: only `e7_over` moved — its devices'
shares; the Total Bill unchanged.

## 3. A battery sensor that counts solar is removed

**The evidence** — only where it is conclusive, so a sensor is never removed on a guess. A half-hour
counts against a battery's sensor when the battery reads **more than 1.5 × the half-hour's whole grid
import, and more than 0.25 kWh above it**: a grid-only charge cannot exceed the grid import. **Export is
not required** (the user, 29 Oct 2026): it adds nothing past the threshold, and would miss an
export-limited site, an install with no live export reading (API-only, until it settles), and solar
charging the battery while the house still imports.

The sensor is removed when such half-hours fall on **at least five days within 30**. Silence proves
nothing — a total-charge sensor passes all winter — so the check runs on every closed half-hour, and
once over history at start-up (a v4 import, an upgrade).

**Why the floor and the five days** (the user, 29 Oct 2026: staging's battery must not be judged wrong
for its firmware's over-reads). Staging's battery reports grid import, as it should, yet on the ratio
alone it would have been removed: 18 half-hours on 10 days — trickles of 0.035–0.18 kWh against almost no
import (the inverter's small flows, or timing), and on 14 Oct 2026 five half-hours of a firmware update
that left the sensor counting. The floor keeps out the trickles; five days within a month keeps out an
incident:

| | over 1.5 × import | and over 0.25 kWh above it | verdict |
|---|---|---|---|
| staging (reports grid import) | 20 half-hours, 12 days | 5 half-hours, 1 day — 14 Oct | kept |
| the Agile user (reports total charge) | 1,361 half-hours, 129 days | 634 half-hours, 96 days | removed on 4 Jul 2026 |

**What removal does** (the user: "remove it and recalculate everything and tell the user why"):

- the sensor is taken off the battery (`device_sensors`), so no new reading is taken from it; its
  reads already kept in `ha_reads` stay — raw device reads are never deleted;
- the battery's device rows go from the blocks, so it drops out of the split and the breakdowns, and
  its grid charge counts in the house — the main meter's figure, which is right;
- the whole history is re-settled (the settle sweep) and the devices rebuilt without it, so a heat
  pump that shared half-hours with the battery gets its share;
- the battery device itself stays, with no sensor, showing why on its screen.

**Telling the user** — a notice on every page, as for changed agreements, until dismissed:
"Your battery's sensor counts solar charging as well as grid charging — on ‹date› it recorded
‹x› kWh while the house imported ‹y› kWh. EMT has stopped using it: your battery's
grid charge now counts in the house, and your history has been re-split. To add the battery back,
choose a sensor that reports its charge from the grid only." The device screen shows the same beside
the battery, with the dates of the evidence.

**Setup says it first** — beside the battery's sensor field: "This must be the battery's charge **from
the grid** only. Many inverters report the total charge, solar included, and EMT can only tell when
the battery charges more than the house imports. If it finds this sensor counting solar, it stops using it, counts the battery's charge in
the house, re-splits your history and tells you."

**Battery only** (the user): a heat pump or EV charger reports energy used; non-grid energy on it is
normal, not evidence.

## 4. Order (each its own PR)

| PR | what |
|---|---|
| 1 | the one split rule, proportional after the EV, in `_apply_pass2` and `settle._devices`; `kwh_battery` gone; the sweep version up (BL-76) |
| 2 | the evidence: each closed half-hour and the start-up pass record it; the support file shows it (log only) |
| 3 | removal: the sensor off, the device rows gone, the history re-settled, the notice and the device screen |
| 4 | the setup text |

Tests: a contested half-hour shared in proportion and the EV still first, the same from both callers;
a half-hour over 1.5 × the import and 0.25 kWh above it counts, exporting or not, and a trickle or a near-1 night one does not; four days in a month do not remove, five do — staging's 14 Oct firmware over-reads are one day; removal keeps `ha_reads` and the device, drops its rows, re-settles, and the notice names the
evidence; a heat pump is never judged.
