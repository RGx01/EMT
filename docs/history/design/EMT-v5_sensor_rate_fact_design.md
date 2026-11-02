# EMT v5 — v4's live rate, kept as a fact

> **History — a design record.** What was decided, when and why, as it stood then; parts describe code that has since changed. How EMT works today: [reference/pricing-and-settlement.md](../../reference/pricing-and-settlement.md). PR numbers (`#NNN`) are in the private archive, `RGx01/EMT-archive`.

*Draft, 28 Oct 2026 (§4 decided the same day, the user: "write the note and then upgrade the importer";
no hard-coded date). Every code fact read at `dev` 5a5c282 and v4 `RGx01/energy-meter-tracker-addon`
HEAD; the staging and v4 figures are from the 28 Oct support file and the v4 database it imported.*

**What this note decides:** how the v4 import keeps the one piece of evidence v4 had about a live
half-hour's band before it recorded dispatches — the rate Octopus's own sensor showed — and how
`settle()` uses it. It does not change the bill, the asker, the prediction's rules or the live engine.

---

## 1. What v4 did

v4 priced a live half-hour, when it closed, at **the last reading of the import channel's rate
sensor** (`compute_channel`: `rates[-1]`, the readings taken each tick before the boundary). On an
account with BottlecapDave's Octopus Energy integration that sensor is
`sensor.octopus_energy_electricity_current_rate` — Octopus's live rate, which goes off-peak while an
Intelligent dispatch runs, inside the night window or out. v4 kept no reading (`reads` is empty) —
only the half-hour's rate and cost, kWh × that rate.

Once the account was connected to Octopus's API (`rate_source = 'api'`), the tariff schedule
replaced the sensor on the main meter when the half-hour closed (v4 `engine.py`, "Honour an explicit
'use supplier API rate' choice … the config-screen / wizard toggle wins over the sensor"), the sensor
kept only as the fallback; the dispatch overlay came with dispatch capture, and the bill settled the
cost later. So a v4 database has up to three eras of live history, each install's at its own dates:

| era | v4's rate for a live half-hour | known from |
|---|---|---|
| **sensor** | the rate sensor's last reading — dispatch-aware | a rate sensor on the import channel, and before API go-live |
| schedule | the tariff schedule (and the overlay once dispatches were recorded) | `rate_source = 'api'` after go-live |
| billed | Octopus's bill | `measured_cost` |

**The evidence (staging's v4 database).** Live half-hours, 3 Mar – 6 Jul 2026, outside 23:30–05:30,
over 0.05 kWh — the ones a clock cannot price — against the bills staging's asker has since fetched:
v4 off-peak / Octopus OFF_PEAK 151, v4 peak / Octopus STANDARD_RATE 34, none disagreeing (3 have no
bill). v4 had no dispatch record before late June (`dispatch_slots` from 27 Jun, `dispatch_history`
from 7 Jul) and no API before 26 Jun 2026 (`pre_live_snapshot_done`). Every stored rate is one of the
day's two tariff rates — no blend — as one reading a half-hour gives.

## 2. What the import loses

The importer carries v4's rate and cost onto the block, and `settle()` replaces them with its own
answer from the facts — by design (step 4 §4 (a): no stored rate or cost is read). For a live
half-hour that is Octopus's bill once the asker fetches it. Where Octopus will not answer, it is the
prediction; and in the sensor era there is no dispatch fact, so the prediction is the clock, and a
smart charge outside the night window reads peak. v4's stored rate — the only trace of what the
sensor said — is lost.

On staging, 87 half-hours of 31 Mar, 1, 5, 9 and 24 Apr 2026 have no bill (Octopus has returned no
cost for them, by any window, since 24 Oct 2026; `.build/probe_window_shapes.py --costs`). 84 are
priced as the April statement prices them; **3 are not** — 31 Mar 23:00, 1 Apr 05:30, 5 Apr 05:30
BST, a smart charge running past the window: £2.43 against the statement's £0.51. v4's rate had all
three right.

## 3. Proposal

**A fact: `sensor_rates`** — `(meter_id, channel, block_start, rate)`, the rate (£/kWh inc VAT) the
import channel's rate sensor showed as the half-hour closed, and `origin` ('v4'). One row a
half-hour, written by what heard it: the importer, from v4's stored rate, for the sensor era only.

**Transform 3d, the sensor's rate.** For each live main-meter import half-hour of the source (`source`
NULL; an imported one has its IMPORTED bill, 3c) with kWh and a rate, in its config period's sensor
era:
- the period's import channel names a rate sensor (`meter_channels.rate_sensor`), and
- the half-hour is before the source's API go-live — `kraken_state.pre_live_snapshot_done`, the time
  v4 first wrote Octopus's data into blocks — where the channel's `rate_source` is `'api'`. Where it is
  not `'api'` the sensor priced every live half-hour; where the account never went live (no marker),
  likewise.

Each database dates its own era; nothing is hard-coded. Where the evidence is missing — a rate
sensor configured, `rate_source = 'api'`, and no go-live marker — nothing is written: the half-hour
keeps today's behaviour. The transform writes no bill, so the asker still asks Octopus for each.

**`settle()`'s band, in order:** Octopus's bill; else **the sensor's band** — the sensor rate against
the day's two tariff rates (the nearer, within 0.05p of it; none where it is near neither), the same
test as an IMPORTED bill's cost ÷ kWh; else the prediction (the dispatches, then the lifecycle); else
the clock. The sensor is Octopus's own live view, so it outranks EMT's rules (§4 (a)'s exception for a
rate Octopus published, like a bill's bucket rate) — and in its era there is no dispatch fact for it to
disagree with. The status stays `predicted`: only a bill settles a band.

**Not recorded live.** v5 still reads the sensor each tick where one is configured, and the schedule
overrides it on an API account when the half-hour closes. Now that dispatches are recorded it adds
nothing `settle()` lacks, so v5 does not start keeping it (28 Oct 2026, the user). `sensor_rates` is
an import-only fact.

## 4. Decided (28 Oct 2026)

- The fix is the import's, and the era is each database's own (no hard-coded date).
- The sensor's band ranks below a bill and above the prediction.
- Live recording: no.

## 5. Order

One PR: the table, transform 3d and its gate (every 3d row is a live source block's stored rate, in its
era), import spec v28, `settle_facts` reading the fact, and the tests — a v4 fixture with a sensor era
and a schedule era, and the three staging half-hours' shape (a smart charge at 05:30 with no dispatch
record) settling off-peak.
