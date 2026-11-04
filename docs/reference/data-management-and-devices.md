# Data Management and devices

*Reference — describes the code on `dev`. When this page and the code disagree, the code is right and
this page is fixed.*

## The page

`/data-management` is a topbar and two things below it:

- **Topbar:** a *History ▾* menu (the Octopus import and *Recover every gap* with the supplier
  API; *Import a CSV*, *Import PDF bills*; *Delete all imported history* when there is some), a
  *Backup & restore* modal (the backups, restore, compact, the support file) and *Backup now*.
- **The health strip** (`/api/data-health`): per view, the Import bill's and Export bill's half-hours
  "to look at" — counted by the heatmap's own `isIssue` — the missing half-hours and gaps, the last
  backup and what made it, the database's size. It refreshes every 60 s (30 s while a job runs), and
  its counts wait while a job rewrites history. Counts are kept per month in `health_counts.json`.
- **The data heatmap** (`heatmap_module.html`, data mode): the **Import bill** and **Export bill**
  views with the supplier API, else **Missing** only; a half-hour a cell, a day a row. *Issues only*
  folds clean days and clean months. `?day=` and `?issues=1` open it there. The states and colours are
  in [pricing-and-settlement.md](pricing-and-settlement.md).

Charts' heatmap is the same module in chart mode: kWh, gCO₂ and gCO₂/kWh, read-only, with *Open in
Data Management*.

## The tools on a selection

Drag (whole days across rows) or shift-click to select; right-click or long-press for the menu.

| Tool | What it writes | Backup first |
|---|---|---|
| **Re-fetch from Octopus** | only the half-hours Octopus's answer differs on | `pre-refetch` |
| **Fill this gap…** | the whole gap holding the selection, from Octopus, a CSV or PDF bills; lifts its tombstone; then rebuilds the devices | none |
| **Fill the missing export…** | export from a CSV or PDF bills, where import is held | — |
| **Delete…** | whole half-hours, every meter, leaving a tombstone (or *only the imported half-hours*) | `pre-delete` |
| **Delete the imported export…** | an import's export over whole local days, recorded in `export_deleted` | `pre-delete-export` |
| **Make a support file…** | nothing — a download | — |

*Delete all imported history* (History) removes imported blocks and their devices' rows after a
required `pre-purge-import` backup, and keeps `ha_reads`.

## Backups

- Zips in `/share/energy_meter_tracker_backup_<site>/backups/` (supervised; standalone, inside the
  data volume in `backup/`), named `<UTC time>_<label>.zip`; the newest 20 kept (`_BACKUP_KEEP`).
- Made by *Backup now*, before every change that cannot be undone (the labels above, and
  `pre_config_save`, `pre_restore`, `pre-attribution`, `pre-api-import`, `pre-csv-import`), and once per
  new version before it starts (`pre_upgrade_<version>`, a raw copy).
- A daily copy of `blocks.db` beside them, at about 03:15 or 03:45 local.
- The `/share` folder is claimed by an instance-id marker, so two installs never share or restore each
  other's backups (`instance.py`) — below.

### Two installs on one `/share`

`/data` is per add-on, so each install's `blocks.db` is its own; `/share`, where the backups live, is
shared by every add-on on the machine. Restoring one install's backup into another on the same
machine is routine (a test install restored from the real one), and a backup carries the site name.

- **Two identities.** `install_id` (`/data/energy_meter_tracker/instance_id`) is minted once per
  install and never changes on a restore; it alone owns a backup folder, through the folder's
  `.emt_instance` marker. `db_uuid` (`store_meta`) travels with the data and marks its lineage. A
  native database has `db_uuid == install_id`; a restored one carries the source's.
- **Why ownership is the install's.** Keyed on the data's `db_uuid`, a restored install would adopt
  the source install's live backup folder — two installs writing one folder.
- **Scan first** (`resolve_backup_dir`): the folder whose marker is this install's id, wherever it is
  and whatever the site is now called. Without the scan, a restored install would follow the source's
  site name, find that folder owned, and make a new folder at every start. A site rename moves the
  folder when the new name is free; otherwise the owned folder is kept. Names are cosmetic; the marker
  decides. Ownership is by identity, never by whether the other install is running.
- **The notice.** A database with `db_uuid != install_id` raises one dismissible notice, "restored
  data from another instance" (`/api/instance/notice`). Dismissals are kept per `db_uuid` in `/data`
  (`acknowledged_db_uuids`), not in `blocks.db`, so they survive the next restore of the same source.
- **Not handled:** a reinstall that lost `/data` gets a new id and does not recognise its old folder;
  its backups are still on `/share` and can be restored by hand. Standalone keeps backups in its own
  data volume, so none of this applies.

## The CSV import format

One file per channel (import, export); the bill parser (`bill_parser.py`) writes the same format from
PDF bills. Headers are matched by substring, ignoring case and order (`csv_import.parse_octopus_csv`):

```
Start, End, Consumption (kWh), Unit Rate (p/kWh), Estimated Cost Inc. Tax (p), Standing Charge Inc. Tax (p)
```

and, optionally, `Unit Rate Exc. Tax (p/kWh)` and `Estimated Cost Exc. Tax (p)`. The import page's
*Template* button writes a file for a span with every half-hour's Start and End filled in
(`csv_import.TEMPLATE_HEADERS`).

- **`Start` needs its offset** (`2024-07-03T00:00:00+01:00`); a row without one is refused. On the
  night the clocks go back, the repeated 01:00 and 01:30 carry `+01:00` then `+00:00`, or they land on
  one UTC half-hour.
- **Rate first.** A row with a unit rate is costed rate × kWh, and its cost column is ignored; a row
  with only a cost (Octopus's own export) uses the cost. Both inc-VAT, in pence. The ex-VAT columns
  follow the same rule and never change an inc-VAT figure.
- **Standing charge** is summed per day: the day's charge on one row, or a 48th on each — never the
  day's charge on every row.
- **Channels:** import is written first, export merges onto the same half-hours by `Start`.
- **What it may write** (`BlockStore.apply_csv_import`): an empty half-hour is filled. A half-hour
  already held is left as it is, except: an earlier import over an agreement Octopus does not cost may
  be replaced; and a half-hour whose kWh has settled and which has no bill takes the CSV's cost as its
  bill, its kWh and devices untouched — on an Intelligent tariff, when the CSV's kWh is the settled kWh
  and its cost ÷ kWh is one of the two rates (not a blend). Live and settled readings and user
  corrections are never replaced.
- A `pre-csv-import` backup is taken first; the import's half-hours can be deleted as imported
  history.

## The support file

`support_file.py`: one zip of `emt.log`, `support.json` (the configuration without secrets, counts, the
job coordinator's state, the host) and `blocks.db` cut to a chosen span (`VACUUM INTO`). *Hide my
meter and account details*, on by default, replaces MPANs, meter serials, the account number and the
postcode with placeholders (`MPAN-1` …) everywhere they appear, tariff agreements included. The API
key is never in it. A size estimate warns over 10 MB.

## Devices

- **One split rule**, `settle.device_grid_shares`, used by both `_apply_pass2` (at write) and
  `settle()`: an EV meter claims its reading of the grid first (with no EV meter, the dispatch's EV is
  reserved); the other devices share what is left in proportion to their readings, in whole Wh, never
  above their own reading. A device's reading beyond its share came from elsewhere (solar, the
  battery) and is not priced.
- **The house's Direct import** = the meter − the devices' grid shares (`direct_import.py`, one rule for
  every reader).
- **A device's cost** is its grid share at the half-hour's rates. A settled half-hour whose devices cost
  more than the meter (beyond a thousandth of a penny) is flagged red; a re-fetch prices them again.
- **`ha_reads`** keeps what Home Assistant reported: each live half-hour (written at finalise), and
  recorder hours as hours. Deleting half-hours keeps it; only removing a device removes its rows.
- **A refill rebuilds the devices** (`rebuild_devices_after_refill`): after a gap fill, an Octopus
  import or a CSV import, each device is rebuilt over the span from `ha_reads`, without asking Home
  Assistant.
- **An import rebuilds a new device from Home Assistant** (`fetch_new`): after an Octopus or a CSV
  import (not a gap fill), a device whose history EMT never had (`devices_without_history`: no recorder
  hours kept, no device row before its first live half-hour) is rebuilt over its whole history from
  HA's recorder, as *Rebuild history* does. The sensor check runs first; a sensor reading more than the
  house drew, or far less, is held — nothing written, a note on its card.
- **Sensors:** `device_sensors` holds a device's primary sensor and its history sensors; each block
  records the sensor that wrote it. *Rebuild history* and the history sensors are on the device's card
  in Settings.
- **A battery counting solar** (`check_battery_sensors`, at start-up and hourly): a half-hour counts
  against a battery when it reads over 1.5× the grid import and over 0.25 kWh above it; on 5 days in 30
  the sensor is flagged. Data is never changed — a banner and a card note, and *Keep this sensor
  anyway* per sensor.
