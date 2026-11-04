# A new device's history on the import (design)

> **History — a design record.** What was decided, when and why, as it stood then; parts describe code
> that has since changed. How it works today: [reference/data-management-and-devices.md](../../reference/data-management-and-devices.md)
> (Devices), and the code. PR numbers (`#NNN`) are in this repository, `RGx01/EMT`.

*Status: built, 4 Nov 2026, in the PR that carries this record. It reverses part of BL-87's closure
([ROADMAP-done.md](../ROADMAP-done.md), 29 Oct 2026), for the reason in §1.*

*Decided, 4 Nov 2026, with the user (a scratch setup: "the rebuild history sensor attribution wasn't
offered until I went to the set up page. could we do it automatically?"):* an import rebuilds each
device whose history EMT never had, from Home Assistant, without asking — after the sensor check.

## 1. What happened

BL-87 asked for this on 12 Oct and was closed on 29 Oct, not built: a first install already got its
device history from three pieces in either order. The order that mattered was *devices first*: a new
device's save offers *Rebuild history*, which with no house history yet keeps the recorder's hours in
`ha_reads`; the import's finish then rebuilds each device from the hours in its span.

On 4 Nov a first setup on Octopus's API began offering the history import as the wizard finishes
(`_offerHistory`). The two offers met: the rebuild offer waits for Setup to reload, the import offer
leaves Setup for Data Management, so the rebuild offer never ran before the import. The user's log:

- 19:30 the wizard saves the main meter, a Zappi and a battery; the import starts;
- 19:38 the import's finish — "no device reads kept there; nothing to rebuild";
- 19:51 the user opens Setup: the rebuild offer, and both devices rebuilt by 19:55.

BL-87's closure said a fetch from the import "would skip the sanity check the offer runs, and write two
years of a device's history unasked on a guess". The first is answered in §3: the check runs. The second
is the user's call, made here.

## 2. Which devices

`devices_without_history()` (block_store): the active sub-meters of the current config period with

- **no recorder hours** in `ha_reads` — Home Assistant was never asked for this device; and
- **no device row before the first half-hour HA recorded live** (none at all if it has not recorded
  live yet) — so not a device whose history came from v4, an earlier rebuild, an API import's device
  split, or anywhere else.

And with a sensor (`device_stitch_sensors`). Checked: on the user's database as it stood at 19:38 (the
rebuild's rows taken out) both devices; on it after the rebuild, none; on four other databases with
device history (three v4-imported households, one with its recorder hours removed, and the demo, whose
devices are `imported_api`), none.

## 3. When, and the check

- **Which imports:** Octopus's API import and a CSV import — the two that make a household's history.
  Not a gap fill: a device the user chose *Later* for is not rebuilt because a gap was filled.
- **When:** at the import's finish, `rebuild_devices_after_refill(..., fetch_new=True)`, queued like the
  refill's own rebuilds (`_rebuild_when_admitted`): claimed once the coordinator admits it, after the
  import and the pricing verify.
- **What:** the device's whole history from HA's recorder — `run_attribution_job` as *Rebuild history*
  runs it, no window. It keeps the recorder's hours and fills only absent device rows.
- **The check first:** `attribution_preflight`, as *Rebuild history* runs it. A sensor that reads more
  than the house drew, or a two-thousandth of it, is **held**: nothing written, the claim released, a
  warning logged, and the device's card says so ("Not rebuilt after the import: this sensor reads about
  … kWh against the house's … kWh … check the sensor, then use Rebuild history"). *Rebuild history*
  asks before it goes ahead, as it does today. A held state lives in memory: a restart clears the note,
  and the device is left for *Rebuild history*.
- **No extra backup.** *Rebuild history* takes a pre-attribution backup; the import took one before it
  began, and the run is in the attribution ledger, so *Remove* on the card undoes it.
- A device with history keeps the refill's rebuild from its `ha_reads`, unchanged.

## 4. The setup page

Taking the import from the first-setup offer drops the queued rebuild offer: the import rebuilds the
devices itself, once the house is in. *Not now* keeps it, and Setup's reload shows it, as before.

Not built: the rebuild offer for a device added later, onto history EMT already has. It stays an offer
— the user, 4 Nov 2026.

## 5. Checked

`tests/test_device_history_on_import.py`: which devices (new, recording live, asked before, rows before
live, retired); an import rebuilding a new device whole from HA's recorder; a sensor that fails the check
held and its claim free; a gap fill not asking HA; a device with history rebuilt from its reads as
before; the API and CSV imports passing `fetch_new`, the gap fill not; the card's held note and the
offer dropped. The setup wizard's PV sensor, found in the same scratch setup, is fixed alongside
(`tests/test_wizard_battery_sensors.py`).
