# Roadmap

*What is open, as of 2026.11.0b33 (23 Nov 2026). Each item says what is wrong or
missing and where it stands; the done history, with every decision's story, is in
[history/ROADMAP-done.md](history/ROADMAP-done.md), and what users see is in the
[changelog](../CHANGELOG.md). Numbers like BL-80 carry over from that record. A PR number written
`EMT-archive#NNN` is in the private pre-release archive.*

*Checked against the code on 21 Nov 2026: an item is here only if the code shows it open.*

## Before the first release

- **On release day (2026.12.0, 1 December 2026): confirm the import floor.** Pinned at 4.5.23 on
  23 Nov 2026 (history/ROADMAP-done.md, *The first release's commitments*). If v4 ships a release with a heal before then, the floor rises to it and
  the positive import fixture is retaken there; otherwise nothing.

## Open

- **EV_BOOST, when Octopus bills it.** Octopus added `EV_BOOST` to its band enums on 28 Sep 2026.
  `bucket_side` books any boost bucket to the EV and logs the first one it sees
  (kraken_api_client.py `bucket_side`), but no reader knows a `boost` band: when one appears in a bill,
  the EV band word and the readers that test an EV band by name (`energy_charts`'s `peak` / `mixed`)
  are the work.
- **`HP_DEVICE`, when Octopus bills it.** Added to the same enums by 2 Nov 2026. Since 21 Nov 2026
  `bucket_side` books any heat-pump bucket (`HP_DEVICE`, `HP_…`, `HP …`, `HEAT PUMP`) to the house,
  before the boost test, and logs the first one it sees; the house's rate and band come from its own
  buckets, the heat pump's only when no other house bucket carried energy (kraken_api_client.py
  `bucket_side`, `_parse_breakdown_node`). Before, a bare `HP_DEVICE` refused the whole half-hour's
  breakdown. Still unseen: whether a heat-pump tariff's bill should price the heat-pump device apart
  from the house, the work when the first log line appears.
- **`timeOfUseUsage`, unprobed** (BL-77 item 3). Kraken added per-band usage
  (`TimeOfUseBandUsageType`, 5 Oct 2026). Whether a domestic IOG account may query it, whether it is
  populated, and whether its band totals match the bill would make it a settlement cross-check. Needs
  an authenticated query on a real account.
- **Comparing eras.** Home eras mark the home's changes on the charts; usage, cost and carbon *per
  era*, compared like for like, is not built (history G.1).
- **The writer thread, held in reserve** (A.1). On an SD card single commits stall the loop 2–6 s for
  about a minute after a restore or an update, and — the soak of 21–23 Nov 2026 showed — in normal
  running too: several a day, mostly at a half-hour rollover, up to ~26 s. Nothing is lost; a
  reconnect no longer re-runs startup on top of one unless Home Assistant was down. A writer thread is
  designed ([design/EMT-v5_A1_writer_thread_design.md](design/EMT-v5_A1_writer_thread_design.md)), not
  built; the stall was accepted on 5 Oct 2026, and again for the first release.

## Open questions

- **Per-request reads or a summary?** Is computing each view from the half-hours on request cheap
  enough on a Pi, or should the writer keep a period summary? Measured so far: a month's bill 0.42 s, a
  year's 4.8 s at p50 on the Pi under load (4 Oct 2026); no summary built.
- **The test suite's shape.** It was carried from v4 and grew to ~5,000 tests; whether to rebuild
  parts of it against the API rather than the store is undecided.

## Resolved since the record

- **A backup taken mid-attribution** (21 Nov 2026; the user: "close it"). Recoverable, and now said.
  A half-done rebuild comes from a restart, a power cut, Home Assistant's own hot backups, or a backup
  restored from mid-run, not only from EMT's backups. On the owner's soak Pi a *Replace history* cut
  off in its re-split and in its writing was finished exactly by *Rebuild history* — every device and
  meter figure matched the database from before, to the Wh. What was wrong: nothing said a rebuild was
  cut off, undo by run missed it, and a restore did not stop a running rebuild. Built: a restore stops
  a rebuild first (#211); the ledger run from its start, and the card says a run was cut off (#212);
  *Backup now* and the daily copy wait for a rebuild (#213). The record:
  [history/design/EMT-v5_interrupted_rebuild_design.md](history/design/EMT-v5_interrupted_rebuild_design.md).
- **A settlement re-run flag per channel** (21 Nov 2026; the user: "close the re-run flag per
  channel"). `blocks.needs_pass2_rerun` stays one flag a block. The schema notes (§4) proposed one per
  channel for v4 #453: an export-only settlement re-priced the import against unchanged kWh and
  rewrote a settled-looking block. Since engine step 4b the re-run prices the import through
  `settle()`, from the facts (`_rerun_pass2_for_settled_block`), so with the import's facts unchanged
  it derives what the block holds; a single flag costs a redundant pass, not a wrong figure.
- **EMT's own `/share` folder** (21 Nov 2026; the user: "we just leave as is"). Backups stay in v4's
  namespace, `/share/energy_meter_tracker_backup[_<site>]`, as v4 names them: each install owns its
  folder by the `.emt_instance` marker, so v4 and EMT installs on one machine never share one
  (`instance.py` `resolve_backup_dir`; [reference](reference/data-management-and-devices.md)). The
  import spec's §7a `emt` folder is not taken.
- **The importer's time on a Pi** (20 Nov 2026). The soak's v4 backup imports in about 80 s on the Pi
  from an SD card (importer design §8a); there was no target, and the user accepted that time: "80 secs
  is acceptable".
- **A login on the direct port** (20 Nov 2026, a blocker for the first release; the user: "could we just
  use users HA's login?"). Port 8098 had none: anyone on the network reached every tool. Now a request is
  ingress only from the Supervisor (#167); the direct port signs in with Home Assistant's own page,
  administrators only, for a 30-day EMT session (#169). Run whole on the owner's soak Pi on `dev`, 20 Nov
  2026: signed in with Home Assistant on the direct port, used a page and saved a change, signed out. The
  dashboard cards' read-only key (#170) worked there too, and the cards were then removed before b27
  shipped them (the user: "I don't want the dashboard cards functionality"). The record:
  [history/design/EMT-v5_direct_port_login_design.md](history/design/EMT-v5_direct_port_login_design.md).
- **BL-108: a device sensor that reads the whole house** (5 Nov 2026, for 2026.11.0b11; a tester's support
  file; the user: "add the BL item then build it"). A GivEnergy battery set up with GivTCP's `import_energy_today_kwh`
  — the house's grid import at the inverter's meter, not the battery's grid charge — matched the main
  meter at every hour of the day; in the 44 half-hours its Zappi charged, the "battery" read 261.5 kWh
  of the meter's 262.0, the car's 139.9 among them. The battery took everything but the car and the
  house read near zero. The battery check (`check_battery_sensors`) looks for a sensor reading
  *more* than the grid, and this one read the same, so nothing said so. To build: a check, flag only,
  for any device whose sensor reads the meter's import within 5% in most of the half-hours the house
  imported over the last 30 days — 86% for that battery, 9% at most for every other device in the
  databases to hand — warned of as the battery's is (banner, card note, *Keep this sensor anyway*).
  Built: `check_whole_house_sensors`, hourly with the battery check; `whole_house_evidence` in the
  store; the evidence in the support file; the flags in `/api/battery-sensor/flagged`'s `whole_house`.
- **BL-102: Economy 7 priced** (5 Nov 2026, for 2026.11.0b9; the user: "mirroring what bcd has done") —
  a two-rate tariff (`E-2R-…`) answers `standard-unit-rates` with HTTP 400, so an Economy 7 agreement
  had no rate schedule. No Octopus or Kraken API gives the night as times (Kraken's `DayNightTariff`
  has `dayRate` / `nightRate`; the meter point's `ssc` and a register's `timePatternRegime` map to times
  only through Elexon's Market Domain Data, not a public API). Octopus publishes a smart meter's night
  as 00:30–07:30 UTC all year, and BottlecapDave's integration applies it (`__is_night_rate`); every
  account EMT reads half-hourly from Octopus has a smart meter, so EMT applies that rule:
  `build_rate_schedule` fetches `day-unit-rates` and `night-unit-rates` for an `E-2R` code (or on that
  400) and places them with the IOG synthesis, its window and zone now arguments. After an API import,
  `check_e7_window` compares the rule with what Octopus charged (its cached bills, the import's own
  costs) and only says so — a meter that switches otherwise shows as disagreements at the window's
  edges. Checked against the real `E-2R-VAR-22-11-01-A` (public product rates, Apr–Jul 2023): night
  00:30–07:30 UTC at the night rate, both price changes followed, the direct-debit rate as the
  one-rate path picks. An old-meter Economy 7 account has no half-hourly data from Octopus and prices
  from Home Assistant's rate sensor in `cad` mode, as before. No tester is on Economy 7 yet; the first
  account's import log ("Economy 7: …") is the check.
- **Flow, a Usage Stats Sankey** (the user, 4 Nov 2026) — grid → rate band → house and devices, export
  out of the house, in kWh, £ and CO₂, for the span on screen; small bands as Other. Built 4 Nov 2026
  (#43–#46); the record is [history/design/EMT-v5_flow_design.md](history/design/EMT-v5_flow_design.md).
  Export by rate band and negative prices drawn in £ (the user, 4 Nov 2026): the record is
  [history/design/EMT-v5_flow_export_bands_design.md](history/design/EMT-v5_flow_export_bands_design.md).
- **A new device's history on the import** (the user, 4 Nov 2026) — Octopus's or a CSV import rebuilds
  each device whose history EMT never had from Home Assistant, after the sensor check; it reverses part
  of BL-87's closure, as the first-setup import offer had broken its chain. The record is
  [history/design/EMT-v5_device_history_on_import_design.md](history/design/EMT-v5_device_history_on_import_design.md).
- **Replacing a device's history after a wrong sensor** (the user, 4 Nov 2026) — a sensor change asks
  why; *Replace history* drops the rebuilt layer and what sensors the device no longer uses recorded,
  then rebuilds it whole; the card says where the history came from. The record is
  [history/design/EMT-v5_replace_device_history_design.md](history/design/EMT-v5_replace_device_history_design.md).
- **Going public** — the repository public on 2 Nov 2026, with: rulesets on `main` (a PR, signed
  commits, no force push or deletion) and `dev` (the same without signatures); no required check,
  because a docs-only PR skips the tests and a required check that never reports would block it;
  secret scanning with push protection (0 alerts on the rewritten history); Dependabot alerts and
  security updates (all 30 alerts were against the old pins, already fixed); private vulnerability
  reporting; CodeQL's default setup (its first scan's alerts below); the labels `bug`,
  `v4-import`, `beta-feedback`. 2026.11.0b1 tagged and released as a pre-release the same day.
- **CodeQL's first scan** — 213 alerts on 2 Nov 2026, each fixed or dismissed with a reason. Fixed:
  the backup and upload paths checked inside the function that uses them (#22, #29, #31), unexpected
  errors answered with where and when instead of the exception's text (#23, #24), the tests'
  temporary files (#24). Dismissed: replies carrying EMT's own messages, the support file's
  diagnostics, the outward code and site name in logs, the page cookie (8 of them again after the
  code they were on moved). Beyond what CodeQL traces, an Octopus error is told in plain words, never
  by its URL, which named the account number, MPAN and serial (#26). Closed by `main`'s scan of
  2026.11.0b3 (3 Nov 2026): 186 fixed, 35 dismissed, none open.
- **The licence** — EMT is under the PolyForm Noncommercial License 1.0.0 from 2 Nov 2026 (it was
  v4's Business Source License 1.1): no commercial use without permission, and no date on which that
  lifts — BUSL's 4-year change to GPL would have let any version be sold four years on.
- **BL-80, CSV history's cost** — the CSV's *estimated* cost differed from kWh × the published rate
  by fractions of a penny. Since engine step 4d every half-hour, CSV history included, is priced by
  `settle()` at kWh × the rate (engine.py `settle_half_hour`, which skips only corrections): on a
  real database all 19,203 CSV-imported half-hours now cost exactly kWh × `imp_rate` (checked 2 Nov
  2026).
- **The first release's bar** — answered by the beta: "imports my history and shows it correctly"
  (BETA.md), not "replaces v4 for daily use".
- **Marked done in the record but unticked there:** BL-78, BL-79, BL-82, BL-83, BL-84, BL-92 and
  BL-101 are all built or closed.
