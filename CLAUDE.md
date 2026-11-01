# EMT — working context

Energy Meter Tracker: a Home Assistant add-on that meters household electricity per
half-hour block, prices it against the supplier's tariff (Octopus Kraken API, IOG /
Intelligent with dispatch-driven off-peak bands), attributes consumption to sub-metered
devices, and reconciles against the supplier's settled figures.

## Where we are

**v4 is at 4.5.24** (repo `RGx01/energy-meter-tracker-addon`). 4.5.14 was the last feature
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
bill, silently — no heal, so the floor does not move. The EV_BOOST band is still expected.

**4.5.23 is the v5 import floor** (raised 12 Oct 2026). It carries a heal, and the rule is that a fix adding a heal
raises the floor EMT will import from. A below-floor database must be **refused** with "open
this in the last v4 release first", not imported with a warning. v5 carries **no v4 heals** —
the floor is what makes that safe — so a v4 fix is ported as its forward fix only.

**The work now is v5**, in this repo (`RGx01/EMT`). It was seeded from v4 4.5.14 and is being
deconstructed in the open; the forward fixes from 4.5.15 (#473, #475) and 4.5.16 (#481) were
ported afterwards, and 4.5.17–4.5.19's VAT forward fixes with them, 4.5.20's gap-fill fix, 4.5.21's four, 4.5.22's VAT resync (as forward code: it waits on Octopus, so the floor alone cannot guarantee it ran; removed in engine step 2d-3, 23 Oct 2026 — a v4 backup from before 13 Oct is opened in v4 first, import spec v26), 4.5.23's hold-back, and 4.5.24's stitch cache (EMT #209, found here first). **Each new v4 release:** port its forward fixes, not its heal, and raise the
floor if it carries a heal (`v4import/gate.py` `FLOOR_VERSION`, import spec, ROADMAP §E, programme §E, this file).
Steps: 1 dead code, 2 extract `billing.py`, 3 ReadStore/WriteStore, 4 API consolidated + HTML
pipeline deleted, 5 schema (11 columns dropped, band moves to `block_segments`; done 10 Oct 2026 in PRs 5a–5c), 6 client +
first-run fork (the fork done 12 Oct 2026: `/welcome`, the staged restore, run on the Pi — first-run design §9), 7 importer. Workstreams A (store split) and B (job coordinator) merged in PR #2.
**A is closed (30 Sep 2026)** after five Pi soaks — A design §5e; **B is closed (5 Oct 2026)**,
all six acceptances met (B §5); **C's eight acceptances are met** (C §0, 3 Oct 2026 — the pre-rendered
pipeline went on 30 Sep); #31 and C §7.3 closed 5 Oct, so nothing in C is open. The Pi runs from an **SD card**: ROADMAP §A.1 is closed with a caveat — about a minute of 2–6 s commit stalls after a restore or upgrade; the writer-thread design is held in reserve.

**Soaking on the Pi:** `.build/soak_prepare_db.py` → restore via Data Management (through ingress
since 14 Oct 2026, when the upload went up in pieces; `http://<pi>:8098/data-management` still works) → `.build/soak.py run` → save the log with
`ha apps logs local_emt_soak --lines 50000` (`ha addons` is deprecated) → `.build/soak.py scan-log <log> --since <restore time>`.

## Read these before v5 work

| document | what it settles |
|---|---|
| `docs/design/EMT-v5_architecture.md` | the plan, the four layers, the deconstruction steps, open questions |
| `docs/design/EMT-v5_programme.md` | workstreams A–E in dependency order, and why that order |
| `docs/design/EMT-v5_import_spec.md` | how v5 reads a v4 database; the floor (§1.1) |
| `docs/design/EMT-v5_A_storage_design.md`, `…_B_coordinator_design.md`, `…_C_surfaces_design.md` | each workstream's design |
| `docs/design/EMT-v5_E_importer_design.md` | step 7: the `v4import/` package, the restore wiring, transform order, key allow-lists, PR order |
| `docs/design/EMT-v5_first_run_design.md` | step 6: the first-run fork — the welcome page, the two-step restore, where the account check runs |
| `docs/design/EMT-v5_settlement_surfaces_design.md` | ROADMAP BL-85 parts 2–3 and BL-86: settlement state per half-hour and channel (kWh, bill, split), the heatmap views, the detail panel, the range re-fetch (draft) |
| `docs/design/EMT-v5_bill_and_split_paths_design.md` | the five bill-fetch paths and six EV-split writers, where they disagree, and the consolidation order (draft) |
| `docs/design/EMT-v5_capped_pricing_by_agreement_design.md` | whether a half-hour is priced under the 6-hour cap by its own agreement or the account's current one; the gates, the soak's latent cases, `capped_at` (draft) |
| `docs/design/EMT-v5_heatmap_issues_design.md` | the heatmap as the data-quality surface: missing half-hours, collapsing clean days, one cell width; the step before the data tools (draft) |
| `docs/design/EMT-v5_gap_fill_design.md` | gap fill as an action on the heatmap's selection: what is filled, the preview, the required backup, one job over several runs (draft) |
| `docs/design/EMT-v5_data_tools_design.md` | ROADMAP BL-88–90: the data tools as actions on a heatmap selection, a data-quality strip on Usage Stats, soft delete (draft) |
| `docs/design/EMT-v5_device_sensors_design.md` | a device's sensors in one place: one primary, secondaries for history, the v5 upgrade moving Device History's sensors in (draft) |
| `docs/design/EMT-v5_soft_delete_design.md` | BL-90: HA's reads kept in a table of their own (a recorder hour as the hour), whole half-hours deleted from the heatmap, a refill rebuilding the devices; and the one per-channel delete kept, an imported export (draft) |
| `docs/design/EMT-v5_data_management_design.md` | Data Management rebuilt around the data heatmap: a health strip, the tools on a selection, a Whole history card, one Backups & storage card, a support file; Charts read-only; corrections on the Billing tab, CAD-only; the sub-pages retired; 7 PRs (decided) |
| `docs/design/EMT-v5_device_cost_check_design.md` | BL-91: a settled half-hour whose devices cost more than the meter, flagged red, fixed by a re-fetch, reported by the importer; the tolerance a thousandth of a penny (decided) |
| `docs/design/EMT-v5_published_tariff_final_design.md` | a published tariff's half-hour final by its own agreement's rate, checked, at any age; CSV grey on any tariff (decided) |
| `docs/design/EMT-v5_G_tariff_agreements_design.md` | ROADMAP §G's first half: the account's agreements stored, read before discovery, fetched in the restore's phase 2 before a v4 import settles; `no_agreement` for another supplier's or a gap's half-hours (decided) |
| `docs/design/EMT-v5_G2_tariff_prices_design.md` | §G's second half, re-scoped: each agreement's standing charge and unit rates as dated price periods, for the tariff overlay's hover only; Agile's unit rates left out; never read for pricing; minor ticks at each price change (decided) |
| `docs/design/EMT-v5_battery_sensor_and_device_split_design.md` | BL-76: the device grid split proportional after the EV, one rule for PASS 2 and settle(); a battery sensor caught counting solar flagged loudly, its data unchanged (decided) |
| `docs/design/EMT-v5_agile_for_everyone_design.md` | BL-103: the Agile card on every account as *Agile in your region* — the product list weekly, the prices daily after 4pm, kept in the store's meta; Agile Outgoing; the last 30 days at Agile's rates (built) |
| `docs/design/EMT-v5_accumulation_design.md` | BL-104: the spirals into Usage Stats as *Running total* — cards side by side, line or spiral, a series, Lifetime / Year by year, the year's start, a table a year; the Spiral tab retired (built) |
| `docs/design/EMT-v5_home_eras_design.md` | BL-101: the home's eras — dated notes for display, kept in the store's meta, edited in a modal behind the Charts ⚙︎; markers on Usage Stats, eras found from the data, a bar colours modal (decided; PR 1 built) |
| `docs/design/EMT-v5_iog_overview_card_design.md` | BL-99: the Overview's smart charging card — cost, the 6-hour allowance per cap-day (≈), peak parts by price with their reason, charging outside a dispatch (built) |
| `docs/design/EMT-v5_api_ev_device_design.md` | BL-97: the EV of an account with no EV device — "EV", its colour on a Settings tab of its own, kept in the store's meta; a new EV device inherits it (built) |
| `docs/design/EMT-v5_rate_brackets_design.md` | BL-93: a Usage Stats view of grid import in 2.5p rate brackets per device, day, month and year; one rule for every tariff, no band labels (built) |
| `docs/design/EMT-v5_A1_writer_thread_design.md` | ROADMAP §A.1: why commits stall the loop on the SD card, and the writer thread |
| `docs/ROADMAP.md` | the open backlog and each item's status — ✅ done on `dev` with its PR (no release has shipped) |
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
- **Doc figures are asserted by tests** (`tests/test_b_design_figures.py`). A change that moves a
  tool's count must update the doc in the same change.
