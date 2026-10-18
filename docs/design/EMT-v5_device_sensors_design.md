# EMT v5 — A device's sensors in one place

*Draft, 18 Oct 2026. From the per-channel soft delete design (§1, tagging what is shelved with the
HA sensor it came from): a device's sensors are configured in two places today, and the user
decided they should be in one. Decided the same day by the user: §2's model, the rename prompt
(§3), a device delete stays permanent (§5). Every line in §0 was read in the code or queried on the
user's prod and prod-dev databases (28 Aug 2026); none is from a docstring.*

---

## 0. What exists

**The device config** (Settings → devices, `meter_config.html`; stored in `config_periods` →
`meters` → `meter_channels`, the database being the config's single source, `load_config`): one
`read` sensor per channel, `meter_channels.read_sensor`. Live capture (`capture_samples`) reads it.
Changing it **overwrites** the old one (`BlockStore._write_meters` upserts the row), so a device's
past sensor names are not kept.

**Device History** (Historical import → Device History, `historical_probe.html`; `/api/historical/
attribute/start`): rebuilds a device's half-hours from HA's long-term statistics. It takes its own
list of sensors per run, `sensor_ids`, and `_stitch_hourly_energy` combines them in that order — the
first sensor holding an hour wins, so `[new name, old name]` covers a rename. The sensors are kept
only in the **run ledger** (`store_meta` `recorder_attribution_runs`: run_id, meter_id, span,
sensors, blocks written), and the page pre-fills from the last run while its layer exists
(`/api/historical/probe-devices`, `history_sensors`).

**So a device's older sensor names live only in a ledger of past runs.** Nothing else in EMT can
see them. On both of the user's databases the EV charger's config holds its current entity id,
and its seven to nine attribution runs all list that **and** an older entity id of the same
charger, stitched below it; the battery uses one sensor in both places.

**HA readings** (`reads`: captured_at, meter_id, channel, reading_kwh, rate, block_id) do not record
their sensor.

**A device delete** (`/api/meter/<id>/delete-data`, `@job("delete")`) takes a required backup and
removes every row the device has, permanently: "This will permanently delete all historical data
for this device from the database. This action cannot be undone."

## 1. The three device actions

| action | what it means | its data |
|---|---|---|
| **Archive** (`retired_at`) | the device was replaced or removed — EMT stops tracking it from that date | kept, unchanged |
| **Delete** | the device was wrong (e.g. the wrong sensor) | **permanent** — no shelf, no undo (§5) |
| **Recreate** | after a delete: a new device, the right sensors | its history rebuilt from them (§4) |

## 2. The model: one primary, secondaries for history

- **One primary sensor per device channel** — the live recorder, as today (`read_sensor`).
- **Secondary sensors, for history only** — an ordered list. Device History stitches **primary,
  then the secondaries in order**, first sensor holding an hour winning (`_stitch_hourly_energy`,
  unchanged). They are never read live.
- **No dates on sensors.** A sensor that was wrong is not given an end date: the device is deleted
  and recreated (§1).
- **Devices only.** The main meter's history comes from Octopus or a CSV, never from HA's
  statistics, so its channels keep one sensor.

**Storage:** `meter_channel_history_sensors` (`meter_channel_id`, `position`, `entity_id`, `origin`
— `configured` | `rename` | `v4_attribution` — and `added_at`), copied with the meters into a new
config period (`_copy_meters_to_period`) as `meter_channels` is. `load_config` gives each channel a
`history_reads` list beside `read`.

**Device History then needs no sensors of its own.** It runs on the device's list; the page shows the
list, read-only, with a link to edit it on the device. The run ledger stays — the record of what each
run used, for its undo — and is never read as config again.

## 3. A rename keeps the old sensor

An HA integration update or reinstall can give the same device's sensors new entity ids. The old id
stops (its statistics end that day), the new one starts, and the device measures what it always did.
EMT's live capture then gets nothing until the device's primary is changed.

**When the primary is changed** on the device screen, EMT asks: *"Keep `<old id>` as a history
sensor?"* — **yes by default**. Yes puts it first among the secondaries (`origin='rename'`), so the
stitch reaches the device's history from before the rename. The device, its id and its recorded
data are untouched.

## 4. Recreate, and rebuilding history

The add-device wizard and the device screen get a **"History sensors (optional)"** list under the
primary, with the same entity picker. Saving a new device with any history sensors offers **"Rebuild
this device's history from Home Assistant now"** — on by default — which starts a Device History run
over the device's list, as the page's own start does (its preflight and backup included).

## 5. A device delete says what is lost

It stays permanent — the shelf of the soft delete design is for a selection's half-hours or channel,
not a whole device. The warning says what goes, from figures EMT holds:

> This deletes **N half-hours** of *<device>* recorded from *<primary>* since *<first date>*. EMT
> can rebuild history from Home Assistant's long-term statistics, which are hourly — not the
> half-hour readings it recorded. **This cannot be undone.**

## 6. The v5 upgrade

A new importer transform, **`history_sensors`** (`v4import/transforms.py`, after `carry_keys`; import
spec v23): for each sub-meter of the latest config period, every sensor any of its attribution runs
used (`recorder_attribution_runs`, the latest run's order first, then earlier runs' extras) that is
not its primary becomes a secondary, deduplicated, `origin='v4_attribution'`. The report counts
devices and sensors added. The ledger itself is already carried (`rules.py` allow-list).

On the user's databases: the EV charger gains its older entity id as its one secondary; the battery
gains none. A v4 restore with no attribution runs changes nothing.

This is an import transform, not a heal: v5 code reads the new table from then on, and v5 carries no
v4 heals.

## 7. Readings record their sensor

`reads` (and `current_reads`, the block in progress) gain `sensor`, written by `capture_samples`,
which has the entity id in hand when it reads. For the soft delete's shelf: a reading recorded from
now on carries its sensor; an older one is tagged with its device's primary when shelved, marked
**inferred**.

## 8. PRs

1. **Storage and config:** `meter_channel_history_sensors`, `load_config`'s `history_reads`, the copy
   into a new config period; `reads.sensor` written at capture. Tests: the list round-trips through a
   config save and a new period; a reading carries its sensor.
2. **The upgrade transform** `history_sensors`, import spec v23. Tests: a v4 database with attribution
   runs gets its secondaries; the primary is never duplicated; none added without runs.
3. **Device History on the device's list:** the start route takes the device, not `sensor_ids`; the
   page shows the list. The ledger is written as now.
4. **The device screen and the wizard:** the history-sensors list, the rename prompt, the rebuild
   offer, the delete warning with its figures.
