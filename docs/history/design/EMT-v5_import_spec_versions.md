# The import specification's versions, v2–v33

> **History.** How the import specification changed, version by version, from 17 Sep to 10 Nov 2026 —
> what each revision decided and why. The contract itself is
> [design/EMT-v5_import_spec.md](../../design/EMT-v5_import_spec.md); PR numbers (`#NNN`) are in the
> private archive, `RGx01/EMT-archive`.

*Changes in v2: §1 records the side-by-side container decision and closes Q1/Q4; §3 gains a source
**provenance** gate; §5.1 covers the `label='ZERO'` measured row (BL-70, 4.5.13); §5.2 makes the band
vocabulary an explicit importer mapping; §9 gate 5 widens from a sample to every block.*

*Changes in v3: **§5.2's band rule is reversed.** v2 had v5 unifying to `off_peak` / `peak` and
mapping `day → peak`. That was wrong — it flattens a real distinction between two different
concepts. v5 keeps all four labels and performs one era-scoped relabel instead. §1 and Q5 gain the
importer's retirement rule.*

*Changes in v4: **the source floor moves from 4.4.0 to v4's final heal high-water mark, and the
importer supports one schema era instead of two.** v1–v3 were written before 4.5.14 was decided and
still claimed there would be no final v4 release. There is: 4.5.14 ships BL-72's stamp, and that
marker — not table presence, not a "done" flag — is now the gate. *(v4 described the stamp as a
computed `heal_level`; v6 corrects that to a plain version.)* §3's first three gates
collapse into one that is strictly stronger, §4's era table is deleted, and the 4.5.12/4.5.13
databases on hand become **negative** test cases alongside the 4.3.1 one.*

*A first pass at v4 wrote the floor as the literal `4.5.14`. Corrected same day: the bar is wherever
v4 **stops**, which post-.14 critical fixes can move, and it is a release-day input rather than a
design-time constant (§1.1, Q9).*

*Changes in v5 (19 Sep 2026, after reading 4.5.13): **`not-listed` is a real supplier value and this
spec previously denied it.** v4 has had a supplier registry (`octopus` / `not-listed`) and
`normalize_supplier()` since the setup wizard shipped; BL-73 now stamps the normalised key into
`store_meta`. The retracted claim was "every v4 database that will ever exist is Octopus" — false,
because a local-only install has real CAD kWh history and no Octopus account at any point. §3 gate 3
becomes four cases, and the supplier is read **before** the account check rather than after.*

*Changes in v6 (19 Sep 2026): **`heal_level` is dropped.** BL-72 shipped as a plain version stamp,
`store_meta.written_by`, and the gate is a version comparison against a floor set when v5 is
written. Heals are historical: a later v4 release that adds one just raises the floor. The computed
level bought nothing the markers did not already provide, and its premise did not survive the code
(more than eight heals, mostly in `kraken_state`, one of them a backup with no invariant). §1.1,
§3, §6, §9 and Q9 all change; the *reasoning* about a moving bar survives intact — only the thing
being compared is now a version, not an integer.*

*Changes in v7 (21 Sep 2026, verified against a real 4.5.14 prod-dev database): §9 gains two
acceptance gates that were prose before and are now demonstrated — `store_meta` values must be
**JSON-decoded** (the column holds `'"4.5.14"'`, quotes included), and versions must compare
**numerically** (`2026.9.0` sorts BELOW `4.5.14` as a string, so v5 would reject its own backup).
That database becomes the first real **positive** fixture; everything held until now was a negative.*

*Changes in v8 (28 Sep 2026): **`FLOOR_VERSION` is pinned at `4.5.15`.** v4 is frozen at 4.5.15,
which shipped a heal (#473, device cost from `imp_kwh_grid`), so by §1.1 the floor rises to it.
v5 ports the two 4.5.15 forward fixes (#473, #475) and not the heal. The 4.5.14 prod-dev database
moves from the positive set to the negative set — the first real **below-floor** case — and a
4.5.15 database is needed as the positive fixture (§9).*

*Changes in v9 (29 Sep 2026): **the floor rises to `4.5.16`.** 4.5.16 shipped a heal
(`run_ev_bucket_refetch`, #481 — Octopus's retrospective bucket relabel), so by §1.1 the floor
follows it. v5 ports the forward fix (`bucket_side`) and not the heal. v4 is not necessarily
finished: a 4.5.17 is expected (EV_BOOST band), and if it carries a heal the floor moves again.
The positive fixture is now a 4.5.16 database; 4.5.14 and 4.5.15 are both below-floor negatives.*

*Changes in v10 (1 Oct 2026): **the floor rises to `4.5.17`.** 4.5.17 carries a heal
(`run_vat_backdate_heal` — 1 Oct 2026's 0% VAT, which Octopus applied by editing its open rate
records in place and 4.5.16 backdated to the record's start), so by §1.1 the floor follows it.
v5 ports the forward fix (`RateSchedule.with_vat`, the seeded 1 Oct boundary, the learner's
dating) and not the heal. 4.5.16 joins the below-floor negatives; the positive fixture is a
4.5.17 database, which does not exist until 4.5.17 ships.*

*Changes in v11 (1 Oct 2026, same day): **the floor rises to `4.5.18`.** 4.5.17's learner
re-learned the backdated 0% from IOG's own closed day/night windows, so its heal's work was
undone on the next refresh. 4.5.18 re-arms the heal (`vat_backdate_heal_done_v2`), and by §1.1
the floor follows it. v5 ports the learner fix (`learn_from_records` learns about today and
later only). 4.5.17 joins the below-floor negatives; the positive fixture is a 4.5.18 database.*

*Changes in v12 (1 Oct 2026, same day): **the floor rises to `4.5.19`.** Its heal (re-armed as
`vat_backdate_heal_done_v4`) also re-prices sub-meter rows left at 5% beside a 0% main on
1 Oct 2026. v5 ports the forward fix (the Bill Summary's per-date VAT) and carries no heal.
4.5.18 joins the below-floor negatives; the positive fixture is a 4.5.19 database.*

*Changes in v13 (7 Oct 2026): **the floor rises to `4.5.21`.** It carries three heals: the
EV-priority re-split (`ev_priority_resplit_done` — a rebuilt block lost `meter_type`, so every
re-split since 3.0.0 could give a concurrently charging battery the EV's grid share), the
dispatch-only device re-fit (`dispatch_house_refit_done`), and the corrected-device ex-VAT repair
(`corrected_device_exc_done`). v5 ports the four forward fixes and carries no heal. 4.5.19 and
4.5.20 join the below-floor negatives, and so does a v5 dev build stamped `4.5.20-a21`; v5's own
builds now stamp `4.5.21-a22`. The positive fixture is a 4.5.21 database.*

*Changes in v14 (8 Oct 2026): §5.2 — **a block with an EV split and no segments gets its segments
from the columns before they are dropped.** v5's surfaces now read the EV split from the EV segments
(`ev_split.py`), falling back to the `imp_*_ev` columns only for a block with no segments at all.
On one real install 10 such blocks hold 28.43 kWh of EV that the drop would otherwise lose. The
fallback goes with the columns.*

*Changes in v15 (8 Oct 2026): §5.2 — **`imp_cost_exc` is CARRIED, not dropped** (the drop list
falls from 11 columns to 10). It is the per-block ex-VAT authority, as `imp_cost` is the inc one: on
blocks with segments, Σ kWh × `exc_rate` differs from it by up to ±2.3p a month on real data, and
checked against two Octopus statements neither source matched every band, so the figure EMT has
always shown is kept. `imp_rate_exc`, `standing_charge_exc` and `exc_source` still drop: v5's
surfaces now derive the first two and never read the third.*

*Changes in v16 (9 Oct 2026): §5.2 — **a held-rate block whose segments do not sum to its kWh is
refitted on import** (`pricing_segments.refit_to_kwh`). v4 re-costs a corrected, reconciled or
measured block to a later settled kWh without touching its segments (ROADMAP BL-79 (1), fixed in v5
only), so every v4 database carries such blocks, more each month. Any other block whose segments
miss its kWh is reported, not changed.*

*Changes in v17 (9 Oct 2026): **the floor rises to `4.5.22`.** Its heal is the October VAT resync
(v4 #505): from 1 Oct 2026 Octopus's half-hourly costs still carried 5% VAT though it bills 0%;
4.5.22 detects Octopus's restatement per install and re-settles the period once every half-hour
agrees with the VAT calendar. **It is conditional on Octopus**: a 4.5.22 database has run the
mechanism, not necessarily the heal — if Octopus has not restated, its October measured blocks
still carry 5%. So v5 carries the detection and resync as **forward code** (it re-arms on every VAT
change and on imported data alike), not as a v4 heal (until v26 removed it, 23 Oct 2026); the floor guarantees only that 4.5.21's heals
ran and that 4.5.22's state marker travels with the data. 4.5.21 and the v5 dev build `4.5.21-a22`
join the below-floor negatives; v5's own builds now stamp `4.5.22-a23`.*

*Changes in v18 (10 Oct 2026): **the importer is built** (`v4import/`; [E design](EMT-v5_E_importer_design.md)),
and five decisions from building it change the text:*
1. *§3 — **classification by `store_meta.emt_schema`.** v5 marks a database it creates; a restore
   imports any database without the marker (v4, or a v5 build before it) and swaps a v5 backup in
   as it is, refusing one at another schema revision until the first release.*
2. *§5.2 — **the house `night` relabel is not built.** v5's own writers cannot tell which tariff era
   a past block is in, so the relabel alone would label one band two ways. It waits on ROADMAP
   BL-81; until then house `off_peak` stands in every era, on both sides of the import.*
3. *§6 — **every key has a rule**, in `v4import/rules.py` (E design §7): v4's whole key history,
   the backups' keys and v5's own. Any key ending `_done` / `_done_v<n>` is a completion marker and
   drops; any other unknown key, table or column aborts the import and is named.*
4. *§8 — **one transaction into a fresh target, which REPLACES the live database.** Not chunked and
   resumable: all-or-nothing by construction, and a re-import gives an identical database. Not a
   merge: the `pre_restore` safety backup keeps what was replaced (decided 10 Oct 2026).*
5. *§9 — **the gates run in the import's transaction**, against the attached source, and a failure
   rolls everything back. Gate 5 is `verify.split_reproduced`; gate 11 is `verify.held_fit`. The
   §9 corpus runs against real backups with `tests/test_import_corpus.py` (EMT_IMPORT_CORPUS).*

*Changes in v21 (12 Oct 2026): **the floor rises to `4.5.23`.** Its heal re-fetches the device
breakdowns cached with no EV against a completed dispatch (v4 #510): on 10 Oct Octopus's first
breakdown put a whole night's charging on the house and billed its last half-hour at the standard
rate, then served the true split days later, and v4 had kept the first answer. Unlike 4.5.22's,
this heal is not conditional: the one-off re-fetch runs once on every install, and where Octopus
now serves the car the recurring split heal re-settles cost and split. v4's forward fix — hold back
a bill whose EV is zero where EMT's own split has the car, for up to 28 days — is ported to v5 as
forward code. 4.5.22 and the v5 dev build `4.5.22-a23` join the below-floor negatives; v5's own
builds now stamp `4.5.23-a24`. The 4.5.22 positive fixture (prod-dev, 10 Oct) is below the floor
now; the fixture is re-taken at 4.5.23.*

*Changes in v29 (28 Oct 2026): **`tariff_agreements` is v5's, and the import leaves it empty** (G1
design §5). A v4 database does not hold the agreement history, so nothing is carried; discovery writes
it at start-up after the import, before the settle sweep, which waits for discovery (G1b retries it
hourly until it answers).*

*Changes in v28 (28 Oct 2026): **v4's sensor-era rate becomes a fact** (transform 3d, gate
`sensor_rates`; sensor rate fact design). Before it was connected to Octopus's API, v4 priced a live
half-hour at the last reading of the import channel's rate sensor — Octopus's live rate, off-peak
during an Intelligent dispatch — and kept no reading, only the rate. v5's settle() reads no stored
rate, and before v4 recorded dispatches the prediction can only be the clock, so where Octopus gives
no bill a smart charge outside the night window read peak. The importer keeps each such half-hour's
stored rate in `sensor_rates`, the era dated by each database's own go-live stamp, and v5 bands by it
below a bill (see SENSOR RATES, below). No bill is written; the asker still asks Octopus.*

*Changes in v33 (10 Nov 2026): **a below-floor refusal's way on does not depend on v4 being offered.**
Both gate-2 refusals said "Open this in the last v4 release first", which assumes v4 can always be
installed from the add-on store. They now name `FLOOR_VERSION` (from the constant,
so it moves with the floor) for while the add-on store still offers it, and Octopus's two years of
history for when it doesn't. The units refusal asks for the v4 the user already has, since it runs
after the floor. Nothing the gate admits or refuses changed.*

*Changes in v32 (31 Oct 2026): **`tariff_prices` is v5's, and the import leaves it empty** (G2 tariff
prices design §3). v4 has no such table. It fills on the first rate refresh after discovery has stored
the agreements, and it is display only, so nothing the import settles waits for it.*

*Changes in v31 (31 Oct 2026): **an imported block's standing charge is its local day's** (transform 3e,
gate `standing_by_day`; E design §4a). v4's API and CSV imports filed the charge by UTC date: in summer a
local day's first half-hour read the day before's, and a CSV run's spring Sunday held two days' charges
(the autumn Sunday none). The importer gives each main imported block its local day's charge, worked out
from the stored rows; a CSV run without the once-a-day signature is reported, not moved (see STANDING BY
LOCAL DAY, below).*

*Changes in v30 (31 Oct 2026): **the import reports devices that cost more than the meter** (device cost
check design §4). Report only, the last step: the half-hours the import carries whose devices cost more,
between them, than the meter (`settlement_state.devices_exceed`), under
`transforms.device_costs.reported`, and a restore warning. Nothing is written; the heatmap flags them and
a re-fetch prices their devices again.*

*Changes in v27 (25 Oct 2026): **what the import heard becomes a bill** (transform 3c, gate
`imported_bills`; engine step 4 design §6 (e)). v4 kept an imported half-hour's band only in the rate
it stored and wrote no `measured_cost` row, so history older than Octopus's two years when it
reaches v5 could never be asked again, and v5's settle(), which reads no stored rate or cost, would
price it at EMT's prediction (dev's settle parity report: 1,084 evening smart charges Octopus billed
off-peak read peak, +£476.10). The importer writes each imported half-hour's stored cost as a bill
labelled `IMPORTED`, and v5 bands it by cost ÷ its own kWh (see IMPORTED BILLS, below). No block
column moves; the floor does not move.*

*Changes in v26 (23 Oct 2026): **v5 no longer carries the October VAT resync** (engine step 2d-3,
the user). Since engine step 2b a settled half-hour's cost is kWh × the schedule's rate with the
VAT calendar's VAT, so Octopus's VAT never reaches a block that settles in v5; and Octopus restated
October on 13 Oct 2026, so the resync had nothing left to wait for. **The gap, accepted:** a v4
database taken before 13 Oct 2026 — at the 4.5.23 floor (12 Oct) or a 4.5.22 one — may hold
October's settled half-hours at 5%, and nothing in v5 re-settles a block that is already settled.
**Open such a backup in v4 first**, so 4.5.22's resync picks up the restatement, then take a new
backup. The floor does not move: 4.5.24 carries no heal. `measured_vat_resync` stays on the drop
list.*

*Changes in v25 (21 Oct 2026): **the settlement source is not carried** (data management design
§2 (b)). v5 retired the choice: it settles against the supplier wherever the API is on.
`billing_source` moves to the drop list, and a v4 on an API mode that chose its own readings
(`cad`) has every main-meter half-hour flagged `needs_pass2_rerun`, the re-price v4's own toggle
ran, back to the default. Gate `billing_source` checks the flags: every main-meter block where
the re-run is due, every other block as the source held it. On `dcc`, or with no API, nothing
moves.*

*Changes in v24 (20 Oct 2026): **HA's reads move out of the blocks** (soft delete design §2).
v4 kept what HA reported only on the blocks built from it, so deleting a block lost it. v5 holds it in
`ha_reads`, which a delete of blocks leaves. Transform 7, after 6 (whose primaries the live rows
name): a live block (`source` NULL or `kraken_api`) gives a half-hour row per channel holding both
registers, its kWh the block's — or, where Octopus settled it, the registers' difference, since v4
then put Octopus's kWh on the block — and its channel's primary as an **inferred** sensor. The main
meter's no-reads 0.0 / 0.0 is left out; a device's is kept (a session sensor sits at 0 between
charges). Device History's `recorder_attributed` half-hours give back their hour, the sum of a whole
hour's blocks; an hour with one missing keeps its half-hours. Imported blocks give nothing. Gate
`ha_reads` checks the live count and each device's recorder kWh against the source. On prod's 4.5.24
backup (19 Oct): 43,900 live rows, 29,951 hours and 2 lone half-hours, in 3.4 s.*

*Changes in v23 (19 Oct 2026): **a device's sensors move into one place** (device sensors design §6).
v4 kept a device's live sensor in its config and the sensors Device History stitched — a renamed
device's older entity ids among them — only in the run ledger. v5 holds both in `device_sensors`.
Transform 6, after 5 (which carries the ledger): each current channel's `read_sensor` becomes its
primary; a sub-meter's Device History sensors that are not its primary become its secondaries, the
latest run first, deduplicated. Gate `device_sensors` checks the result against the source. The
ledger is still carried as it is. No block points at an entry: v4 recorded no sensor per block.*
