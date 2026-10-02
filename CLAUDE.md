# EMT — working context

Energy Meter Tracker: a Home Assistant add-on that meters household electricity per
half-hour block, prices it against the supplier's tariff (Octopus Kraken API, IOG /
Intelligent with dispatch-driven off-peak bands), attributes consumption to sub-metered
devices, and reconciles against the supplier's settled figures.

## Where we are

**v4 is at 4.5.20** (repo `RGx01/energy-meter-tracker-addon`). 4.5.14 was the last feature
release; since then it accepts **critical fixes only** — data loss, crash-on-start, or a pricing
error. 4.5.15 shipped #473 (device cost from raw `imp_kwh` instead of the grid-clipped
`imp_kwh_grid`, with a heal) and #475 (a settled kWh re-banding an unpriced block). 4.5.16 shipped
#481 (Octopus relabelled the device-breakdown buckets; with a heal). 4.5.17 (1 Oct 2026) shipped the
0% VAT backdating fix with a heal; its learner undid the heal, so 4.5.18 (same day) fixes the
learner and re-arms the heal; 4.5.19 fixes the Bill Summary's VAT across the change and
re-arms the heal for sub-meter rows. 4.5.20 (2 Oct 2026) stops a gap-fill opening from a 0.0
register after a power cut (v4 #493) — no heal, so the floor does not move. The EV_BOOST band is still expected.

**4.5.19 is the v5 import floor** (raised 1 Oct 2026). It carries a heal, and the rule is that a fix adding a heal
raises the floor EMT will import from. A below-floor database must be **refused** with "open
this in the last v4 release first", not imported with a warning. v5 carries **no v4 heals** —
the floor is what makes that safe — so a v4 fix is ported as its forward fix only.

**The work now is v5**, in this repo (`RGx01/EMT`). It was seeded from v4 4.5.14 and is being
deconstructed in the open; the forward fixes from 4.5.15 (#473, #475) and 4.5.16 (#481) were
ported afterwards, and 4.5.17–4.5.19's VAT forward fixes with them, and 4.5.20's gap-fill fix. **Each new v4 release:** port its forward fixes, not its heal, and raise the
floor if it carries a heal (import spec, ROADMAP §E, programme §E, this file).
Steps: 1 dead code, 2 extract `billing.py`, 3 ReadStore/WriteStore, 4 API consolidated + HTML
pipeline deleted, 5 schema (15 columns dropped, band moves to `block_segments`), 6 client +
first-run fork, 7 importer. Workstreams A (store split) and B (job coordinator) merged in PR #2.
**A is closed (30 Sep 2026)** after five Pi soaks — A design §5e; B's close-out items are still
open (B §5); **C is next, starting with deleting the pre-rendered chart pipeline** (C §5, Pi
baseline). The Pi runs from an **SD card**: storage-bound commit stalls are ROADMAP §A.1.

**Soaking on the Pi:** `.build/soak_prepare_db.py` → restore via `http://<pi>:8098/data-management`
(not ingress — it rejects the upload) → `.build/soak.py run` → save the log with
`ha addons logs local_emt_soak --lines 50000` → `.build/soak.py scan-log <log> --since <restore time>`.

## Read these before v5 work

| document | what it settles |
|---|---|
| `docs/design/EMT-v5_architecture.md` | the plan, the four layers, the deconstruction steps, open questions |
| `docs/design/EMT-v5_programme.md` | workstreams A–E in dependency order, and why that order |
| `docs/design/EMT-v5_import_spec.md` | how v5 reads a v4 database; the floor (§1.1) |
| `docs/design/EMT-v5_A_storage_design.md`, `…_B_coordinator_design.md`, `…_C_surfaces_design.md` | each workstream's design |
| `docs/ROADMAP.md` | open backlog, priority ordered — nothing in it has shipped |
| `docs/CONTRIBUTING.md`, `docs/DEVELOPMENT.md` | branch model, PR flow, what a change must carry |
| `CHANGELOG.md` | user-facing history in the project's voice |

v4's `docs/ROADMAP_archive.md` records **why** past decisions were made; read it (in the v4 repo)
before re-opening anything.

## Working rules

**Verify before stating.** A docstring, a comment or a variable name is **not** evidence of
behaviour. Trace the assignment, or query the data. This project has already been bitten by
`_measured_floor()`'s docstring saying "SMB" when the bound is the current agreement, by
four heals prefixed `smb_` of which only one is SMB-gated, and by `recost_sub_meters` saying
"kWh, the grid-clipped device draw" while its SQL priced raw `imp_kwh`. If a claim can be checked
with a query against a database in the repo's test fixtures or a supplied backup, check it.

**Reconcile any two numbers stated together.** If a kWh figure and a £ figure appear in the
same sentence, multiply them and confirm they agree with the observed total before writing it
down.

**Branches and PRs.** Work on `fix/…`, `feat/…` or `chore/…` branched from `dev`; PR to `dev`.
Nothing is committed straight to `main` or `dev`. Don't push or open a PR without asking.

**No real customer figures** in the changelog, code comments or issues.

**Tests must discriminate.** A test that passes both before and after a fix is a guard, not a
regression test — label it as such. Check new tests fail on the unpatched tree.

**Heals** (v4 only) are marker-gated, self-marking, idempotent, retire under BL-33, and raise the
import floor. v5 has none of its own.

## Invariants worth knowing

- **"Settled" is two events.** `imp_kwh_api` (DCC, ~1–2 days, says *how much*) and
  `measured_cost` → `rate_source='measured'` (the bill, later or never, says *what it cost*).
  Cost settlement implies kWh settlement; the reverse does not. Reading
  `imp_kwh_api IS NOT NULL` as "settled" has caused at least six defects. A settled kWh may
  re-**cost** a block and must never re-**band** it (#475, `hold_rate`).
- **The round-trip pattern.** `get_block_dict_by_start` → mutate → `append_block_replace`
  (INSERT OR REPLACE) resets any column not explicitly carried. Fixed three times; prefer a
  targeted UPDATE.
- **Device cost derives from `imp_kwh_grid`**, never raw `imp_kwh`
  (`COALESCE(imp_kwh_grid, imp_kwh)`). The day-level bill total is
  `max(0, main − Σsubs) + Σsubs`, so an over-stated device total clamps `house` to zero and the
  bill follows the devices instead of the meter.
- **Device sensors:** a pure consumer (heat pump, EV charger) reports energy *used* and cannot
  know its source — non-grid energy on it is normal and clipping is correct. A **battery** must
  report **grid import**, because it is what creates the non-grid supply and cannot be inferred
  by clipping without double-counting.
- **Run the suite with `bash run_tests.sh` at the repo root** — it runs each test file in its own
  process (plain `pytest tests/` collides at collection) and creates/removes the test symlinks.
  Keep any virtualenv **outside** the repo.
- **Doc figures are asserted by tests** (`tests/test_b_design_figures.py`). A change that moves a
  tool's count must update the doc in the same change.
