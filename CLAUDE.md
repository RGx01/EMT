# EMT — working context

Energy Meter Tracker: a Home Assistant add-on that meters household electricity per
half-hour block, prices it against the supplier's tariff (Octopus Kraken API, IOG /
Intelligent with dispatch-driven off-peak bands), attributes consumption to sub-metered
devices, and reconciles against the supplier's settled figures.

## Where we are

**v4 is at 4.5.25** (repo `RGx01/energy-meter-tracker-addon`). 4.5.14 was the last feature
release; since then it accepts **critical fixes only** — data loss, crash-on-start, or a pricing
error. 4.5.15 shipped #473 (device cost from raw `imp_kwh` instead of the grid-clipped
`imp_kwh_grid`, with a heal) and #475 (a settled kWh re-banding an unpriced block). 4.5.16 shipped
#481 (Octopus relabelled the device-breakdown buckets; with a heal). 4.5.17 (1 Oct 2026) shipped the
0% VAT backdating fix with a heal; its learner undid the heal, so 4.5.18 (same day) fixes the
learner and re-arms the heal; 4.5.19 fixes the Bill Summary's VAT across the change and
re-arms the heal for sub-meter rows. 4.5.20 (2 Oct 2026) stops a gap-fill opening from a 0.0
register after a power cut (v4 #493) — no heal, so the floor does not move. 4.5.21 (7 Oct 2026) keeps an EV meter's
grid priority on a re-split (a rebuilt block had lost `meter_type`), fits a dispatch-only account's devices inside
the house, and carries a correction's ex-VAT rate to devices — three heals. 4.5.22 (9 Oct 2026) picks up Octopus's
restated October costs (its half-hourly data carried 5% VAT after the 0% change, v4 #505) once every half-hour since
the change agrees with the VAT calendar — a heal that waits on Octopus. 4.5.23 (12 Oct 2026, v4 #510) holds back a
settled breakdown with no EV where EMT's split has the car (Octopus's first answer for 10 Oct put the night on the
house, and corrected it days later), for up to 28 days, and re-fetches the ones already cached — a heal. 4.5.24 (18 Oct 2026, v4
#513) rebuilds a running fixed-term agreement's rates at every refresh: the stitch cached any agreement with a
`valid_to`, and IOG-SMB's windows reach only now + 2 days, so two days after a restart the bill apply refused every
bill, silently — no heal, so the floor does not move. 4.5.25 (4 Nov 2026, v4 #523) is v5's #53 ported back:
`_current_agreement` takes the agreement in force now, never a zero-length blip — a blip after an IOG-SMB fixed read
the account as on Agile, stopping the bill passes and taking the blip's standing charge — no heal, so the floor does
not move. The EV_BOOST band is still expected.

**4.5.23 is the v5 import floor** (raised 12 Oct 2026). It carries a heal, and the rule is that a fix adding a heal
raises the floor EMT will import from. A below-floor database must be **refused** with "open
this in the last v4 release first", not imported with a warning. v5 carries **no v4 heals** —
the floor is what makes that safe — so a v4 fix is ported as its forward fix only.

**The work now is v5**, in this repo (`RGx01/EMT`, public). It was seeded from v4 4.5.14 and
deconstructed in the open; the forward fixes from 4.5.15 (#473, #475) and 4.5.16 (#481) were
ported afterwards, and 4.5.17–4.5.19's VAT forward fixes with them, 4.5.20's gap-fill fix, 4.5.21's four, 4.5.22's VAT resync (as forward code: it waits on Octopus, so the floor alone cannot guarantee it ran; removed in engine step 2d-3, 23 Oct 2026 — a v4 backup from before 13 Oct is opened in v4 first, import spec v26), 4.5.23's hold-back, 4.5.24's stitch cache (EMT-archive #209, found here first), and 4.5.25's current agreement (#53, found here first: the
fix went v5 → v4). **Each new v4 release:** port its forward fixes, not its heal, and raise the
floor if it carries a heal (`v4import/gate.py` `FLOOR_VERSION`, the import spec, `docs/reference/v4-import-and-first-run.md`,
README, BETA.md, the v4-import issue form, this file — `git grep 4.5.23` finds them).
The deconstruction (steps 1–7, workstreams A–E) is done; its record is `docs/history/design/` and
`docs/history/ROADMAP-done.md`. The Pi runs from an **SD card**: about a minute of 2–6 s commit stalls
after a restore or upgrade; the writer-thread design is held in reserve.

**Repositories (2 Nov 2026).** This repo was created that day from a history rewritten clean of real
account details; the work before it, with its issues and PRs, is the private `RGx01/EMT-archive`
(local clone `~/code/EMT-archive`). A `#NNN` before 2 Nov in a history doc, or `RGx01/EMT-archive#NNN`
in a commit message, is there. **Never push from the archive clone to `RGx01/EMT`.**

**Versions:** CalVer `YYYY.M.PATCH`, betas `2026.11.0b1`, `b2`, …; `main` holds the latest beta,
2026.11.0b12. The slug is `emt`, the direct port 8098. Releasing: `docs/DEVELOPMENT.md`.

**Soaking on the Pi:** `.build/soak_prepare_db.py` → restore via Data Management (through ingress
since 14 Oct 2026, when the upload went up in pieces; `http://<pi>:8098/data-management` still works) → `.build/soak.py run` → save the log with
`ha apps logs local_emt_soak --lines 50000` (the Pi's clone keeps `slug: "emt_soak"` locally; the repo's is `emt` since 2 Nov 2026 — docs/DEVELOPMENT.md) (`ha addons` is deprecated) → `.build/soak.py scan-log <log> --since <restore time>`.

## Read these before v5 work

| document | what it settles |
|---|---|
| `docs/reference/` | **how EMT works today** — architecture, pricing and settlement, Data Management and devices, the v4 import and first run, the API. Fix a page in the same PR as the code it describes. |
| `docs/design/EMT-v5_import_spec.md` | how v5 reads a v4 database; the floor (§1.1). A living contract. |
| `docs/design/EMT-v5_A1_writer_thread_design.md` | why commits stall the loop on the SD card, and the writer thread held in reserve |
| `docs/ROADMAP.md` | what is open; done items are in `docs/history/ROADMAP-done.md` |
| `docs/history/design/` | each design **as it was decided** — the why, and rejected alternatives. Records: never updated, parts describe code since changed. Indexed in `docs/README.md`. |
| `docs/DEVELOPMENT.md`, `docs/CONTRIBUTING.md` | branches, tests, `.build/serve_local.py`, the repositories, releasing; what a change must carry |
| `CHANGELOG.md` | user-facing history in the project's voice |

A new design is written in `docs/design/` while it is decided and built; once built it moves to
`docs/history/design/` with the history header, and what it settled goes into `docs/reference/`.

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

**Branches and PRs.** Work on `fix/…`, `feat/…`, `chore/…` or `docs/…` branched from `dev`; PR to `dev`.
Nothing is committed straight to `main` or `dev`. Don't push or open a PR without asking.

**No real customer figures or identifiers** in the changelog, code, comments, docs, commits or issues — the
repository and its whole history are public. Placeholders: MPANs 2600000000001/2, postcode SW1A, account A-0000ABC7.

**Tests must discriminate.** A test that passes both before and after a fix is a guard, not a
regression test — label it as such. Check new tests fail on the unpatched tree.

**Heals** (v4 only) are marker-gated, self-marking, idempotent, retire under BL-33, and raise the
import floor. v5 has none of its own.

## Invariants worth knowing

- **"Settled" is two events.** `imp_kwh_api` (DCC, ~1–2 days, says *how much*) and
  `measured_cost` → `rate_source='measured'` (the bill, later or never, says *what it cost*).
  Cost settlement implies kWh settlement; the reverse does not. Reading
  `imp_kwh_api IS NOT NULL` as "settled" has caused at least six defects. A settled kWh may
  re-**cost** a block, and may move EMT's *predicted* band only through the prediction's own
  thresholds (0.1 kWh, 0.4 kWh); only a bill settles a band. Since engine step 4b `settle()`
  decides it from the facts, so arrival order cannot (#475's defect; its `hold_rate` freeze is gone).
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
- **Doc figures are asserted by tests** (`tests/test_b_design_figures.py`, against
  `docs/reference/architecture.md`). A change that moves a tool's count must update the page in the
  same change.
