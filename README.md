# EMT

Energy tracking for Home Assistant, for Octopus Energy accounts — half-hourly cost, the EV/House
split on Intelligent tariffs, and history reconciled against the supplier's own bill.

> **Pre-release.** Nothing has shipped yet. This repository is at the design stage; the code lives
> in the v4 add-on (`energy-meter-tracker-addon`) while EMT is built.

## What this is

EMT is a **new add-on with a new slug**, not an upgrade. It installs alongside Energy Meter Tracker
v4, in its own container, and ingests a copy of that add-on's history. Both can run indefinitely —
there is no cutover deadline and no migration window.

- **Versioning.** Public releases are CalVer `YYYY.M.PATCH` (e.g. `2026.10.0`). `v5` is the internal
  discriminator for pre-release work only.
- **Migration.** A 4.5.13 backup zip, or a `blocks.db` taken from one. There is no cooperating
  exporter — the importer validates the source itself. See
  [the import spec](docs/design/EMT-v5_import_spec.md).

## Layout

```
.                       add-on manifest (once there is one to ship)
docs/                   design documents and the roadmap
docs/design/            one document per workstream
tests/                  the test suite
```

The shape follows the v4 add-on deliberately: its ~2,700-test suite and runner assume this layout,
and that suite is the main instrument for proving EMT computes what v4 computed.

## Where to start

| Document | What it covers |
|---|---|
| [docs/design/EMT-v5_programme.md](docs/design/EMT-v5_programme.md) | The five workstreams, why they are in that order, and what does not get carried across |
| [docs/design/EMT-v5_import_spec.md](docs/design/EMT-v5_import_spec.md) | The migration contract: eligibility gates, table-by-table disposition, acceptance |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Current state of each workstream |
