# EMT v5 — a battery sensor that counts solar, and the device grid split (BL-76)

> **History — a design record.** What was decided, when and why, as it stood then; parts describe code that has since changed. How EMT works today: [reference/data-management-and-devices.md](../../reference/data-management-and-devices.md). PR numbers (`#NNN`) are in the private archive, `RGx01/EMT-archive`.

*Decided 29 Oct 2026 (the user); §3's removal changed to a warning the same day. Code facts read at `dev` 9c96752; the figures are from the Agile-export
user's v4 database (heat pump, battery, EV — the only database in hand with two non-EV devices).*

**What this note decides:** how a block's grid import is shared between devices (BL-76), and what EMT
does when a battery's sensor turns out to count solar charging as well as grid charging — which the
device rule forbids and which EMT cannot check at setup: it says so loudly, and changes nothing.

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

## 3. A battery sensor that counts solar is flagged

**The evidence** — only where it is conclusive, so a sensor is never flagged on a guess. A half-hour
counts against a battery's sensor when the battery reads **more than 1.5 × the half-hour's whole grid
import, and more than 0.25 kWh above it**: a grid-only charge cannot exceed the grid import. **Export is
not required** (the user, 29 Oct 2026): it adds nothing past the threshold, and would miss an
export-limited site, an install with no live export reading (API-only, until it settles), and solar
charging the battery while the house still imports.

The sensor is flagged when such half-hours fall on **at least five days within 30**. Silence proves
nothing — a total-charge sensor passes all winter — so the check runs on every closed half-hour, and
once over history at start-up (a v4 import, an upgrade).

**Why the floor and the five days** (the user, 29 Oct 2026: staging's battery must not be judged wrong
for its firmware's over-reads). Staging's battery reports grid import, as it should, yet on the ratio
alone it would have been flagged: 18 half-hours on 10 days — trickles of 0.035–0.18 kWh against almost no
import (the inverter's small flows, or timing), and on 14 Oct 2026 five half-hours of a firmware update
that left the sensor counting. The floor keeps out the trickles; five days within a month keeps out an
incident:

| | over 1.5 × import | and over 0.25 kWh above it | verdict |
|---|---|---|---|
| staging (reports grid import) | 20 half-hours, 12 days | 5 half-hours, 1 day — 14 Oct | kept |
| the Agile user (reports total charge) | 1,361 half-hours, 129 days | 634 half-hours, 96 days | flagged on 4 Jul 2026 |

*As built (PR 2, 29 Oct 2026):* `engine.check_battery_sensors`, at start-up and hourly from the engine
tick (a whole-history query, cheap — not each half-hour as it closes), records each battery's evidence in
store_meta (`battery_sensor_evidence`: its half-hours and days, the first and last, the day it is
conclusive, the ten most recent) and shows it in the support file; it warns once when a sensor's
evidence becomes conclusive. Rows count that name the battery's primary sensor, or none — imported and
rebuilt history names none, so it is the device's sensor of the day; judging a replacement sensor afresh came
with the flag (PR 3). On real data: staging 5 half-hours, all 14 Oct
2026 — kept; the Agile user, imported, 634 on 96 days — conclusive 4 Jul 2026.

**Flagged, not removed** (the user, 29 Oct 2026, reversing "remove it and recalculate everything": "fail
it loudly"). Removal was built — the battery's rows deleted, the history re-split — and set aside before
it merged. It made EMT act on a verdict it can get wrong, where being wrong costs history:

- **a v4 import** is judged at its first start, so a wrong verdict deletes rows before the user has
  looked at anything — the path least tried (one real database);
- **a correctly configured but noisy sensor**: the floor and the five days were tuned on one good
  sensor, staging's; another inverter's timing could cross both;
- a refill or Device History would have to skip the battery for ever after, and a rebuild could not
  bring its rows back.

A wrong warning costs the user a look at their setup. So EMT changes nothing in the data:

- **a banner on every page**, not dismissible, naming the battery, an example half-hour (‹date›: the
  battery ‹x› kWh, the whole house ‹y› kWh), what it gets wrong — the battery is given grid it did not
  take, so its cost reads too high and the rest of the house's too low; the bill total is not
  affected, as the split never gives the devices more than the main imported (§2) — and how to fix it:
  choose the battery's grid-charge sensor, or remove the battery if the inverter has none;
- **the battery's card** says the same, with the half-hours and days, and that the half-hours already
  recorded keep the old sensor's readings (Device History never overwrites a row);
- **"Keep this sensor anyway"** on the card: the user's choice, for the noisy sensor or for one they
  know counts solar and keep regardless. It clears the banner for that sensor only (a new sensor is
  judged afresh), shows a quiet note on the card with "Show the warning again", and stops the warning
  in the log. Not a plain dismiss: a dismiss hides a real problem and is forgotten; this is recorded
  against the sensor, and the card says it was kept. *Reworded 31 Oct 2026* (was "This sensor reports
  grid charge only"): a user who keeps a total-charge sensor on purpose had to click a statement they
  knew was false, and the card and the log then repeated it. The button claims nothing about the
  sensor; the stored key and route are unchanged.
- **a new sensor** is judged on the rows that name it: the unnamed rows (imported, rebuilt) are the
  old sensor's, and would flag the new one at once.

*As built (PR 3, 29 Oct 2026):* `engine.check_battery_sensors` records `conclusive_on` (was
`would_remove_on`) and keeps the sensors it flagged (store_meta `battery_sensor_flagged`, so a
replacement is known) and warns once per sensor; `confirm_battery_sensor` keeps the user's word
(`battery_sensor_confirmed`, held for the sensor confirmed); `battery_sensor_flags` gives the page the
flags whose sensor is still the battery's primary. GET `/api/battery-sensor/flagged`, POST
`/api/battery-sensor/confirm`; both keys in the support file. On real data: the Agile-export user's
battery, imported, flagged (conclusive 4 Jul 2026), one warning and none on a second pass, its blocks
and segments unchanged; staging not flagged; on both, no half-hour's devices take more grid than the
main imported.

**Setup says it first** (built in PR 3, with the flag) — beside the battery's sensor field, in the
add-device form and the setup wizard, and in the card's sensor requirements (which had said "use the
consumption sensor"): the cumulative kWh the battery charged **from the grid** only, not its total
charge; many inverters report the total, solar included, which makes the device breakdown wrong; EMT
can only tell in sunny months, and warns if it finds it; with no grid-charge sensor, leave the
battery out and its charge counts in the house. And a clamp meter on the battery's cable will not do where the solar
feeds the house wiring: it counts the solar the battery takes as well as the grid. (The Agile-export
user's battery is read that way — a clamp meter's energy-returned total on the inverter's cable.)

**Battery only** (the user): a heat pump or EV charger reports energy used; non-grid energy on it is
normal, not evidence.

## 4. Order (each its own PR)

| PR | what |
|---|---|
| 1 | the one split rule, proportional after the EV, in `_apply_pass2` and `settle._devices`; `kwh_battery` gone; the sweep version up (BL-76) |
| 2 | the evidence: each closed half-hour and the start-up pass record it; the support file shows it (log only) |
| 3 | the flag: the banner, the device screen, the user's confirmation, and the setup text (PR 4 folded in) — nothing in the data changes |

Tests: a contested half-hour shared in proportion and the EV still first, the same from both callers;
a half-hour over 1.5 × the import and 0.25 kWh above it counts, exporting or not, and a trickle or a near-1 night one does not; four days in a month do not flag, five do — staging's 14 Oct firmware over-reads are one day; a flag
changes no block, is warned of once, holds for its sensor, and a confirmation clears it for that sensor
only; a new sensor is not flagged by the old one's unnamed rows; a heat pump is never judged.
