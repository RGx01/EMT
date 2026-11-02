# The v4 import and the first run

*Reference — describes the code on `dev`. The full contract, table by table, is
[../design/EMT-v5_import_spec.md](../design/EMT-v5_import_spec.md). When this page and the code
disagree, the code is right and this page is fixed.*

## The first run

- With no meters configured, `/` goes to `/welcome`, and `_first_run_guard` sends every other page
  there too, except Settings (the setup wizard), Help and Logs.
- **Set up a new account** opens the wizard: the supplier, the main meter, where the readings come from.
- **Restore from an Energy Meter Tracker backup** is two steps:
  1. **Upload** — in 512 KiB pieces (`/api/restore/upload/begin|chunk|finish`), because Home Assistant's
     ingress refuses large bodies. The backup is staged and phase 1 runs (the gate, below), before
     anything is asked; it returns what the backup is (`facts`: v4 or EMT, `written_by`, the supplier,
     whether it is stamped to an account).
  2. **Confirm** (`/api/restore/confirm`) — with an Octopus key where the backup needs one: tested
     against the backup's account first; a different account is refused unless accepted, and then the
     key is not kept. Then a safety backup, the swap, and the engine starts on the restored database.
- Stages older than an hour are removed.

## Which import a backup gets

A database with `store_meta.emt_schema` is EMT's own: it must equal `EMT_SCHEMA` (5) — older or newer
is refused — and only the integrity gate runs. Anything else is a v4 database and goes through the v4
gate and import.

## The gate (phase 1, `v4import/gate.py`)

In order; the first that fails refuses the backup, with its reason:

1. **Integrity** — no hot `-wal`; `quick_check` and `foreign_key_check` clean.
2. **The floor** — `store_meta.written_by` present and ≥ `FLOOR_VERSION` (**4.5.23**), compared as
   numbers. Below it: "Open this in the last v4 release first." EMT carries none of v4's heals; the
   floor is what guarantees they ran.
3. **The supplier** — Octopus, not listed, or absent. Another supplier is refused.
4. **Units** — rates stored in pence rather than pounds are refused.

## The import (`v4import/`)

- **Nothing unknown passes.** Every source table, column and state key needs a rule, or the import
  stops and names it (`check_source`).
- **A fresh target.** A new database with EMT's schema; the source attached read-only; one transaction:
  carry, transform, verify. The audit is kept in `store_meta.import_audit`. Any failure deletes the
  target and leaves the live database untouched.
- **The transforms, in order** (`transforms.run`): provenance; EV segments; the held refit; the bill
  split; imported bills (an import's cost as an `IMPORTED` bill); v4's sensor rates; the standing charge
  by local day; the meta keys; device sensors; `ha_reads`; devices costing more than the meter
  (reported, not changed). v4's `off_peak` band is kept as it is — no relabel.
- **Not carried:** tariff agreements and prices start empty and fill from Octopus after discovery.
- **The import replaces** the live database; it never merges. A `pre_restore` backup is taken first.
- After it, the settle sweep prices the history by EMT's rule, and the asker asks Octopus for what has
  no bill.
