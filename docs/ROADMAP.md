# Roadmap

*What is open, as of 2026.11.0b3 (3 Nov 2026). Each item says what is wrong or
missing and where it stands; the done history, with every decision's story, is in
[history/ROADMAP-done.md](history/ROADMAP-done.md), and what users see is in the
[changelog](../CHANGELOG.md). Numbers like BL-80 carry over from that record. A PR number written
`EMT-archive#NNN` is in the private pre-release archive.*

*Checked against the code on 2 Nov 2026: an item is here only if the code shows it open.*

## Before the first release

- **The import floor, pinned on release day.** `v4import/gate.py` `FLOOR_VERSION` is 4.5.23, the last
  v4 release that added a heal. It is set again from what v4 has reached when EMT's first release is
  cut, and the positive import fixture retaken at that version.
- **The importer's time on a Pi.** The soak's v4 backup imported in about 80 s on the Pi from an SD
  card (importer design §8a); no target has been agreed, so this is not yet called met.
- **A login on the direct port.** Port 8098 (published by default, for the dashboard cards) has none:
  anyone on the network reaches every tool (SECURITY.md). Ingress is behind Home Assistant's login.

## Open

- **EV_BOOST, when Octopus bills it.** Octopus added `EV_BOOST` to its band enums on 28 Sep 2026.
  `bucket_side` books any boost bucket to the EV and logs the first one it sees
  (kraken_api_client.py `bucket_side`), but no reader knows a `boost` band: when one appears in a bill,
  the EV band word and the readers that test an EV band by name (`energy_charts`'s `peak` / `mixed`)
  are the work.
- **`HP_DEVICE`, flagged.** Added to the same enums by 2 Nov 2026. An `HP_DEVICE…` bucket would be
  booked to the house by `bucket_side`'s catch-all — the right side for the house/EV split, but
  silently. A heat-pump tariff's breakdown is where to look if one appears.
- **Octopus's REST API is not watched.** GraphQL deprecations and schema changes open issues weekly
  (`.github/workflows/graphql-deprecations.yml`); the REST products API, where tariff rates come from,
  has no schema to introspect. A refresh that suddenly returns nothing, or a different shape, for a
  tariff that had rates is the signal to surface.
- **`timeOfUseUsage`, unprobed** (BL-77 item 3). Kraken added per-band usage
  (`TimeOfUseBandUsageType`, 5 Oct 2026). Whether a domestic IOG account may query it, whether it is
  populated, and whether its band totals match the bill would make it a settlement cross-check. Needs
  an authenticated query on a real account.
- **The IOG Direct reader, unasserted.** `tests/test_direct_import.py` holds every Direct reader to
  one rule on every fixture, but no fixture is Intelligent: the bill carving the EV out of Direct per
  rate, and the Overview counting it as a device, agree untested there.
- **A settlement re-run flag per channel.** `blocks.needs_pass2_rerun` is one flag a block for both
  channels (block_store.py, the `blocks` DDL); the schema notes (§4) proposed one per channel.
  Undecided.
- **EMT's own `/share` folder.** Backups still go to v4's namespace,
  `/share/energy_meter_tracker_backup[_<site>]` (instance.py). Instance isolation keeps v4's and EMT's
  apart, so nothing collides, but the import spec's §7a proposal of an `emt` folder is undecided.
- **Comparing eras.** Home eras mark the home's changes on the charts; usage, cost and carbon *per
  era*, compared like for like, is not built (history G.1).
- **The writer thread, held in reserve** (A.1). On an SD card single commits stall the loop 2–6 s for
  about a minute after a restore or an update. A writer thread is designed
  ([design/EMT-v5_A1_writer_thread_design.md](design/EMT-v5_A1_writer_thread_design.md)), not built;
  the stall was accepted on 5 Oct 2026.

## Code to tidy

- **CodeQL's first scan: what is left** (2 Nov 2026; 213 alerts, triaged). Fixed on `dev` and
  closing when they reach `main`: the backup and upload path checks (#22), unexpected errors
  answered with where and when instead of the exception's text (#23, #24, and the BottleCapDave
  detection and attribution undo after them), the tests' temporary files (#24). Dismissed with a reason each (27): replies carrying EMT's own messages,
  the support file's diagnostics, the outward code and site name in logs, the page cookie. And
  beyond what CodeQL traces: the engine's job statuses and replies (18 sites) now say an Octopus
  error in plain words (`kraken_api_client.person_message`) — the error's own text named the URL,
  with the account number, MPAN and serial — and anything else as where and when. Left: the
  fixed alerts close when `dev` reaches `main`.
- **`WriteStore` and `store_access.writer()` have no production caller.** The engine writes through
  its own `BlockStore` and pages hand writes to it (`_engine_write`); the typed write path is unused.
  A candidate for the deletion protocol.
- **A test that passes on a docstring.** `test_run_exclusive_is_still_the_unused_primitive` finds
  `run_exclusive(lambda: None)` in a docstring (engine.py, near `drain_tick`); `run_exclusive` itself
  is gone. Delete it, or make it assert something.

## Open questions

- **Per-request reads or a summary?** Is computing each view from the half-hours on request cheap
  enough on a Pi, or should the writer keep a period summary? Measured so far: a month's bill 0.42 s, a
  year's 4.8 s at p50 on the Pi under load (4 Oct 2026); no summary built.
- **A backup taken mid-attribution.** Does a database with a device rebuild part-done import wrongly,
  or does the ask/attribution record make it recoverable?
- **The test suite's shape.** It was carried from v4 and grew to ~5,000 tests; whether to rebuild
  parts of it against the API rather than the store is undecided.

## Resolved since the record

- **Going public** — the repository public on 2 Nov 2026, with: rulesets on `main` (a PR, signed
  commits, no force push or deletion) and `dev` (the same without signatures); no required check,
  because a docs-only PR skips the tests and a required check that never reports would block it;
  secret scanning with push protection (0 alerts on the rewritten history); Dependabot alerts and
  security updates (all 30 alerts were against the old pins, already fixed); private vulnerability
  reporting; CodeQL's default setup (its alerts are under *Code to tidy*); the labels `bug`,
  `v4-import`, `beta-feedback`. 2026.11.0b1 tagged and released as a pre-release the same day.
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
