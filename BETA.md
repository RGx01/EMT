# Testing EMT's beta

Thank you for trying EMT before its first release. This page says what to try, what to look out for,
and what to send back.

## Before you start

- **Keep v4.** EMT installs beside Energy Meter Tracker v4 and never touches it: v4 keeps recording,
  and nothing EMT does changes v4's data. Run both while you test.
- **Take a v4 backup** if you will bring v4's history across, from v4's Data Management page. EMT
  needs a backup from **v4 4.5.23 or later**. An older one is refused with the reason: update v4 while
  Home Assistant's add-on store still offers 4.5.23 or later, then back it up again. If the store
  doesn't, start from scratch (below) and bring back your history from Octopus instead.
- **Ports.** EMT's page is in Home Assistant's sidebar (**EMT**). It is also published directly on
  port **8098** (v4 uses 8099), for the dashboard cards. That direct port has **no login** — anyone on
  your network can open it. Turn it off in the add-on's **Network** settings if you only use the
  sidebar.

## What to try

Either or both:

1. **Bring v4's history across.** On EMT's welcome page, **Restore from an Energy Meter Tracker
   backup** and choose v4's backup. EMT checks it before anything is written, then imports it. Then
   compare: does a month's bill on EMT's **Billing** tab match v4's, and your Octopus statement?
2. **Start from scratch.** **Set up a new account**: the wizard asks for your supplier, your meter and
   where your readings come from. With Octopus connected, **Data Management → History → Import
   history from Octopus** brings back about two years.

Then use it as you would v4 for a while: Overview, Charts (Billing, Heatmaps, Usage Stats), Insights,
Data Management. Each page's **?** opens its help.

## What to look out for

- A figure that differs from v4's, or from your bill — the half-hour, and both figures.
- Red on Data Management's heatmap you can't explain — its help says what each colour means.
- Anything slow: a page that takes more than a few seconds, or the log's `engine loop lag` warnings.
- Wording that is unclear, wrong or missing.

## What to send

Open an [issue](https://github.com/RGx01/EMT/issues), and attach a **support file**: Data
Management → **Backup & restore** → **Make a support file…**, for the days in question. Keep **Hide
my meter and account details** ticked (it is by default): your MPANs, meter serials, account number
and postcode become placeholders, and your Octopus API key is never in it.

Please don't paste MPANs, your account number or your address into an issue.

## Updating and removing

- **Updates** arrive in Home Assistant's add-on store like any other add-on's; each beta is
  `2026.11.0b1`, `b2`, …
- **Removing EMT** deletes its data (`/data`). Its backups are in
  `/share/energy_meter_tracker_backup_<site>/` and stay, unless you delete them. v4 is unaffected.

## Known gaps

The [roadmap](docs/ROADMAP.md) lists what is still open.
