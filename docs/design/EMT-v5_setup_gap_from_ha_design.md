# The setup gap, from Home Assistant's recorder (design)

*Design, 5 Nov 2026. **Not built.** Decided with the user that day: the half-hours between a new
install's history import and its first live reading should go through the same three stages as every
other day — predicted, then kWh settled, then cost settled — rather than appearing a day or two later
already kWh-settled. ("that 2 day window gets the 'predicted view' that we see rolling forward every
day then".)*

---

## 1. What happens today

A new install on `cad+api` records live from its first tick and imports Octopus's history to the last
half-hour Octopus has settled. The half-hours between are nobody's:

- the import cannot reach them — Octopus has not settled them yet;
- the live tick starts at its first reading and does not look back;
- the restart gap fill (`GAP_FILL_LIMIT_HOURS = 12`) is for an EMT that was running and stopped, with
  a read either side; a new install has no read before.

They appear only when the Octopus poll's BL-8 backfill creates them from Octopus's settled figures, a
day or two later — kWh-settled from the start, never predicted. Since #70 their devices are rebuilt
then, from the recorder hours EMT keeps.

The scratch install of 4 Nov 2026 (set up 19:30 UTC): the import ran to 2 Nov 23:30; 3 Nov came from
the poll's backfill; 4 Nov 00:00–19:00 (39 half-hours) was still empty when the support file was
taken. Home Assistant had recorded the CAD the whole time.

## 2. The fill

After an import — Octopus's or a CSV — on `cad+api`, EMT fills the main meter's half-hours **that have
no block** between the import's last half-hour and the first live one, from HA's statistics for the
CAD sensors the main meter is configured with (import, and export where configured).

**Which half-hours.** From the half-hour after the import's last (`count_imported_history()['to']`) to
the first the live tick wrote (the main meter's first `ha_reads` row with `origin = 'live'`), or the
current half-hour where it has written none yet. Only half-hours with no main-meter block at all; a
tombstoned half-hour is left deleted. Nothing EMT already holds — live, imported, backfilled, corrected
— is touched.

**From what.** `recorder/statistics_during_period` with `period = '5minute'`, the `change` column: six
rows make a half-hour. HA keeps 5-minute statistics as long as its recorder keeps history — 10 days by
default — and a setup gap is a day or two, so they are there. A sensor without a `state_class` has no
statistics; that install gets no fill, and the log says why (`statistics_probe` already reads a
sensor's statistics and can tell).

**What is refused** — left empty, for the poll's BL-8 backfill as today:

- a half-hour without all six 5-minute rows (HA was down for part of it);
- the first half-hour after a missing row: HA's first statistic after a hole carries the whole hole's
  energy in one row (the box was off, §4);
- a negative `change`, or a half-hour over the live path's ceiling (`_ROGUE_BLOCK_KWH_CEILING`): a
  register reset or a glitch (v4 #493);
- every half-hour, if the fetch fails or HA is not connected — nothing is retried; the poll fills them.

**What it writes.** One store method, after `create_backfill_block`: a main-meter block with `imp_kwh`
(and `exp_kwh`), **`imp_kwh_api` left NULL** — not settled — `needs_pass2_rerun = 1`, a `source` of its
own (`ha_recorder`), and an `interpolated` value of its own, so it can be told from a locally
interpolated (1) or a supplier-backfilled (2) half-hour. One transaction.

**Then the devices.** The fill runs before the import's device rebuild, and that rebuild's span is
widened to the fill's end, so the devices are split over the filled half-hours in the same pass (from
the recorder hours `ha_reads` keeps, or HA's recorder for a device EMT never had).

## 3. What follows, unchanged

Every stage after the fill is the existing path:

1. **Predicted.** PASS 2 prices the half-hour (`needs_pass2_rerun`): the rate for its time, the band
   settle() predicts from the CAD's kWh.
2. **kWh settled.** The poll finds the block exists, so Octopus's figure goes through
   `upsert_kraken_block` — `stored`, with the drift check — not BL-8's `missing_block`.
3. **Cost settled.** The bill, as for any half-hour.

To check while building: what `upsert_kraken_block` does to the `source` and `interpolated` of a
filled block when it stores Octopus's figure — they should say where the kWh came from at each stage.

## 4. Where it cannot help

- **The box was off.** HA records nothing while it is down, and its first statistic afterwards holds
  the whole jump. A power cut inside the gap leaves those half-hours to the poll (§2's refusals). This
  fill adds to Octopus's gap fill; it never replaces it.
- **Intelligent Octopus Go's dispatches.** A predicted band outside the fixed off-peak window needs
  the dispatch. EMT captures them live, and creates off-peak slots from `completedDispatches` it was
  not there to see — but on two installs, the oldest completed dispatch Octopus returned on EMT's first
  poll was 12–13 hours old (a long-running install's first poll, 8 Jul; a beta tester's, 4 Nov). A
  smart charge earlier in the gap is predicted at peak until the bill settles it — as for a dispatch
  EMT missed today. Accepted: the bill corrects it, and the gap is a day or two.
- **`api` and `api+mini`.** No CAD sensor is configured; there is nothing to read. (The Mini's
  readings are not in HA.)
- **`cad` without the API.** There is no Octopus import to bound the gap, and whether PASS 2 can price
  a past half-hour from HA's rate sensors is not established. Not in scope.

## 5. Home Assistant's load

One `statistics_during_period` call per configured sensor (import, export), over the gap — a day or two
of 5-minute rows, a few hundred each — on the engine loop, with a timeout. The writes are one
transaction for the span. No Octopus calls.

## 6. Not built

- **Restart gaps** (EMT stopped while HA ran). The same reading would shape them better than today's
  even spread, and lift the 12-hour limit while HA has the statistics. A follow-on, once this works.
- **History from HA for `cad`-only installs.** HA keeps hourly statistics indefinitely; importing a
  `cad` install's history from them needs past rates. A separate idea.
- **History sensors for the main meter.** The fill reads the sensor configured now; a CAD replaced
  inside the gap leaves the half-hours it has no statistics for to the poll.
- **Any page change.** The filled half-hours are predicted half-hours like the rest; their provenance
  is in the data.

## 7. Tests (planned)

Against an in-memory store and a fake HA client, each failing on the parent:

- a gap between an import and the first live half-hour is filled from 5-minute rows, `imp_kwh_api`
  NULL, `needs_pass2_rerun` set, its source and marker;
- a half-hour missing a row, the one after a hole, a negative change and one over the ceiling are left
  empty;
- a half-hour with a block, and a tombstoned one, are untouched (GUARD on the first);
- no HA, a failed fetch, and `api` mode fill nothing;
- the device rebuild that follows covers the filled span;
- the poll then stores Octopus's figure on a filled block as `stored`, not `missing_block`.

## 8. Open

1. **The marker.** A new `interpolated` value (3), or `source` alone? `interpolated` is read by the
   import page and by `count_backfilled_blocks`; a new value needs each reader checked.
2. **CSV imports.** A CSV import on `cad+api` bounds a gap the same way. In scope (the default here), or
   Octopus's import only?
