# Data Management and devices

*Reference — describes the code on `dev`. When this page and the code disagree, the code is right and
this page is fixed.*

## The page

`/data-management` is a topbar and two things below it:

- **Topbar:** a *Whole history ▾* menu (the Octopus import and *Recover every gap* with the supplier
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

*Delete all imported history* (Whole history) removes imported blocks and their devices' rows after a
required `pre-purge-import` backup, and keeps `ha_reads`.

## Backups

- Zips in `/share/energy_meter_tracker_backup_<site>/backups/` (supervised; standalone, inside the
  data volume in `backup/`), named `<UTC time>_<label>.zip`; the newest 20 kept (`_BACKUP_KEEP`).
- Made by *Backup now*, before every change that cannot be undone (the labels above, and
  `pre_config_save`, `pre_restore`, `pre-attribution`, `pre-api-import`, `pre-csv-import`), and once per
  new version before it starts (`pre_upgrade_<version>`, a raw copy).
- A daily copy of `blocks.db` beside them, at about 03:15 or 03:45 local.
- The `/share` folder is claimed by an instance-id marker, so two installs never share or restore each
  other's backups (`instance.py`).

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
- **Sensors:** `device_sensors` holds a device's primary sensor and its history sensors; each block
  records the sensor that wrote it. *Rebuild history* and the history sensors are on the device's card
  in Settings.
- **A battery counting solar** (`check_battery_sensors`, at start-up and hourly): a half-hour counts
  against a battery when it reads over 1.5× the grid import and over 0.25 kWh above it; on 5 days in 30
  the sensor is flagged. Data is never changed — a banner and a card note, and *Keep this sensor
  anyway* per sensor.
