# EMT — Energy Meter Tracker

A Home Assistant add-on for Octopus Energy accounts: each half-hour's electricity cost, your devices
(a car, a battery, a heat pump) split out of the meter, Intelligent Octopus Go's smart charging and
its 6-hour cap, and your history settled against Octopus's own bill.

> **Beta.** EMT is being tested by a few invited people before its first release. Expect rough
> edges; please report them — see [BETA.md](BETA.md).

EMT is the successor to [Energy Meter Tracker v4](https://github.com/RGx01/energy-meter-tracker-addon).
It is a **new add-on with its own slug** (`emt`), not an upgrade: it installs beside v4, runs beside
it for as long as you like, and can bring v4's history across from a v4 backup.

| | |
|---|---|
| ![The bill for a billing period](docs/screenshots/charts-billing.webp) | ![Usage Stats: each device's import, month by month](docs/screenshots/usage-devices.webp) |
| ![The net energy heatmap, a half-hour a cell](docs/screenshots/charts-heatmap.webp) | ![The running total, a year a turn of the spiral](docs/screenshots/running-total-spiral.webp) |

*A made-up household — `.build/demo_db.py`.*

## Install

1. In Home Assistant: **Settings → Add-ons → Add-on store → ⋮ → Repositories**, and add
   `https://github.com/RGx01/EMT`.
2. Install **EMT — Energy Meter Tracker**, start it, and open it from the sidebar (**EMT**).
3. The welcome page offers two ways in: **Set up a new account** (from scratch), or **Restore from
   an Energy Meter Tracker backup** (a v4 backup's `.zip`, or its `blocks.db`).

Bringing v4's history across needs a backup from **v4 4.5.23 or later**; an older one is refused,
with the reason. Open it in the latest v4 first, then back it up again.

## Versions

EMT uses CalVer, as Home Assistant does: `YYYY.M.PATCH` — `2026.11.0` is the first release in
November 2026, `2026.11.1` the next. Betas are `2026.11.0b1`, `b2`, … A release that changes
something you rely on says so under **⚠️ Breaking** in the [changelog](CHANGELOG.md).

## Help and support

- **In the app:** the **?** beside each page's title opens its help.
- **Something wrong?** Open an [issue](https://github.com/RGx01/EMT/issues). A **support file** (Data
  Management → Backup & restore) holds what is usually needed, with your meter and account details
  replaced by placeholders.
- **A security problem?** See [SECURITY.md](SECURITY.md) — please don't open a public issue.

## For developers

| Document | What it covers |
|---|---|
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Branches, running the tests, testing a branch on another machine |
| [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) | What a change must carry |
| [docs/ROADMAP.md](docs/ROADMAP.md) | What is open, in progress and done |
| [docs/design/EMT-v5_architecture.md](docs/design/EMT-v5_architecture.md) | How EMT is built, and why |
| [docs/design/EMT-v5_import_spec.md](docs/design/EMT-v5_import_spec.md) | How a v4 database is brought across |

Run the suite with `bash run_tests.sh` at the repository root (each test file in its own process).

## Licence

[Business Source License 1.1](LICENSE).
