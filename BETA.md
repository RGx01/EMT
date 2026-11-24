# Testing EMT's betas

Betas carry the next version of EMT ahead of its release. They come from a separate repository,
[EMT_beta](https://github.com/RGx01/EMT_beta), as a separate add-on, **EMT (beta)**, so only those who
add it get them. This page says how to install one, what to look out for, and what to send back.

## Installing EMT (beta)

- **Add the repository.** In Home Assistant's add-on store, **⋮ → Repositories**, add
  `https://github.com/RGx01/EMT_beta`. It offers **EMT (beta) — Energy Meter Tracker**.
- **It is its own add-on.** EMT (beta) has its own data, its own sidebar entry (**EMT beta**) and its
  own direct port, **8090** (EMT's is 8098, v4's 8099), so it runs beside EMT or instead of it. Nothing
  it does changes EMT's data, or v4's.
- **Bring your history across** with a backup: on EMT, **Data Management → Backup & restore**, **Backup
  now**, then **⬇ Download** it from the list; on EMT (beta)'s welcome page, **Restore from an Energy Meter Tracker backup** and choose it.
  A backup from v4 (**4.5.23 or later**) works the same way.
- **Ports.** The direct port asks you to sign in with Home Assistant (administrators only). Turn it off
  in the add-on's **Network** settings if you only use the sidebar.

Running both? The two record the same meter separately. Compare them, but settle on one in the end: a
beta's data goes back to EMT the same way, with a backup and a restore, once the release it led to is out.

## What to look out for

- A figure that differs from the release's, or from your bill — the half-hour, and both figures.
- Red on Data Management's heatmap you can't explain — its help says what each colour means.
- Anything slow: a page that takes more than a few seconds, or the log's `engine loop lag` warnings.
- Wording that is unclear, wrong or missing.

Each beta has a [release](https://github.com/RGx01/EMT_beta/releases) on EMT_beta saying what it changes;
the [changelog](CHANGELOG.md)'s **Unreleased** section is the same list, building to the next release.

## What to send

Open an [issue](https://github.com/RGx01/EMT/issues) on **EMT** (EMT_beta has none), say it is a beta and
which one, and attach a **support file**: Data Management → **Backup & restore** → **Make a support
file…**, for the days in question. Keep **Hide my meter and account details** ticked (it is by default):
your MPANs, meter serials, account number, postcode and site name become placeholders, and your Octopus
API key is never in it.

**An issue is public: anyone can download what you attach.** The support file still holds your
half-hourly usage and costs for the days you choose, your device and sensor names, and EMT's log. If
you'd rather not post it, open the issue without it and say so, and we'll arrange another way.

Please don't paste MPANs, your account number or your address into an issue.

## Updating and removing

- **Updates** to EMT (beta) arrive in the add-on store like any other add-on's; each beta is `b1`, `b2`,
  … on the version it leads to (`2027.1.0b1`).
- **Removing EMT (beta)** deletes its data (`/data`). Its backups are in
  `/share/energy_meter_tracker_backup_<site>/` and stay, unless you delete them. EMT and v4 are
  unaffected.

## Before the first release

EMT's own public beta ran from 2 November 2026 (`2026.11.0b1`) to 23 November 2026 (`2026.11.0b33`), from
this repository; the first release, `2026.12.0`, followed on 1 December 2026.
