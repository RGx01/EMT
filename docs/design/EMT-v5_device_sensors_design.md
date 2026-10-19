# EMT v5 — A device's sensors in one place

*Draft, 18 Oct 2026. From the soft delete design (tagging what is shelved with the
HA sensor it came from): a device's sensors are configured in two places today, and the user
decided they should be in one. Decided the same day by the user: §2's model, the rename prompt
(§3), a device delete stays permanent (§5); on 19 Oct, one table of a device's sensors keyed by its id,
and a live block pointing at the entry that recorded it (§2, §7). Every line in §0 was read in the code or queried on the
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

**HA's data is kept at the half-hour.** The samples within an open half-hour sit in `current_reads`
and become the finished block's values — the kWh and HA's register at the half-hour's start and end
(`imp_read_start` / `imp_read_end`, `exp_…`). Nothing keeps the samples: the `reads` table has never
been written in v4 or v5 (ROADMAP, 19 Sep 2026: "dead, not transient"; empty on both of the user's
databases). A block does not record which sensor its registers came from.

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
- **Secondaries are for devices only.** The main meter's history comes from Octopus or a CSV, never
  from HA's statistics, so its channels have a primary and nothing else.

**Storage: `device_sensors`, keyed by the device's id** — `id`, `meter_id` (the device's own id,
e.g. `ev_charger`, stable across config periods), `channel`, `entity_id`, `role` (`primary` |
`secondary`), `position` (the stitch order among secondaries), `origin` (`configured` | `rename` |
`v4_attribution`) and `added_at`. One table for every meter's sensors, the main meter's primaries
included. Not per config period: `meter_channels` is copied into each new period
(`_copy_meters_to_period`, its `meter_id` a per-period row number), but a device's sensors are the
device's, not a period's. `meter_channels.read_sensor` stays as the live engine's view of the
channel's primary, written from `device_sensors` on a config save; `load_config` gives each channel a
`history_reads` list beside `read`.

**Device History then needs no sensors of its own.** It runs on the device's list; the page shows the
list, read-only, with a link to edit it on the device. The run ledger stays — the record of what each
run used, for its undo — and is never read as config again.

## 3. A rename keeps the old sensor

An HA integration update or reinstall can give the same device's sensors new entity ids. The old id
stops (its statistics end that day), the new one starts, and the device measures what it always did.
EMT's live capture then gets nothing until the device's primary is changed.

**When the primary is changed** on the device screen, EMT asks: *"Keep `<old id>` as a history
sensor?"* — **yes by default**. Yes changes the old entry's `role` to `secondary`, first among them
(`origin='rename'`), **keeping its id** — so the blocks it recorded still point at it (§7) and name
the entity that recorded them; the stitch reaches the device's history from before the rename. The
new primary is a new entry. No drops the old entry, unless a block points at it — then it stays as
a secondary the stitch ignores (`position` NULL), so the tag stays true. The device, its id and its
recorded data are untouched.

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

A new importer transform, **`device_sensors`** (`v4import/transforms.py`, after `carry_keys`; import
spec v23), for each meter of the latest config period:
1. each channel's `read_sensor`, where it has one, becomes its **primary** (`origin='configured'`);
2. for a sub-meter, every sensor any of its attribution runs used (`recorder_attribution_runs`, the
   latest run's order first, then earlier runs' extras) that is not its primary becomes a
   **secondary**, deduplicated, `origin='v4_attribution'`.

The report counts devices and sensors added. The ledger itself is already carried (`rules.py`
allow-list). Imported blocks get no sensor pointer (§7).

On the user's databases: the EV charger gains its older entity id as its one secondary; the battery
gains none. A v4 restore with no attribution runs changes nothing.

This is an import transform, not a heal: v5 code reads the new table from then on, and v5 carries no
v4 heals.

## 7. A live block points at its sensor

The half-hour reading is what is kept (the user, 19 Oct 2026: the register at each half-hour, not
every sample), so the tag goes on the block — as a pointer to the device's entry, not a copy of an
entity id (decided 19 Oct): `imp_sensor_id` and `exp_sensor_id` on `blocks`, referencing
`device_sensors.id`, written at finalise from the channel's primary for a live-recorded block; NULL
for one imported, rebuilt by Device History, or recorded with no sensor (the Mini). A rename keeps the
entry (§3), so the pointer stays true. A block recorded before the columns, when shelved, points at
its device's primary entry as it is then, marked **inferred**.

It also gives a device delete its warning's sensors (§5) and the soft delete's shelf its tag.

## 8. PRs

1. **Storage and config:** `device_sensors` keyed by device id; `read_sensor` written from it on a config
   save; `load_config`'s `history_reads`; `blocks.imp_sensor_id` / `exp_sensor_id` written at finalise.
   Tests: the list round-trips through a config save and survives a new config period without a copy;
   a live block points at its primary's entry, an imported one at none; a rename keeps the entry's id.
2. **The upgrade transform** `device_sensors`, import spec v23. Tests: every configured read becomes a
   primary; a v4 database with attribution runs gets its secondaries; the primary is never duplicated;
   none added without runs.
3. **Device History on the device's list:** the start route takes the device, not `sensor_ids`; the
   page shows the list. The ledger is written as now.
4. **The device screen and the wizard:** the history-sensors list, the rename prompt, the rebuild
   offer, the delete warning with its figures.
