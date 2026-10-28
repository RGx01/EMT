# EMT v5 — G1: tariff agreements, stored

*Decided 28 Oct 2026 (the user: agreements first, with history, read before discovery, the re-settle
and recount on a change; discovery in the restore's phase 2 on both routes; an Octopus backup with no
credentials imports unconnected; `no_agreement` with no asks; a non-Octopus period excluded before
discovery). The first half of ROADMAP §G; the rates are G2. Code facts read at `dev` after #319.*

**What this note decides:** where EMT keeps the account's tariff agreements, who writes and reads
them, what happens when they change, how a v4 import gets them before it settles, and how a half-hour
no agreement covers — another supplier's, or a gap — reads. It does not store rates (G2) or eras
(§G.1).

---

## 1. What exists

- **Discovery** (`kraken_api_client.auto_discover`) returns each meter point's MPAN, meter serial,
  current tariff and every agreement it has had (`tariff_code`, `valid_from`, `valid_to`). EMT holds it
  in memory (`engine._kraken_discovery`); only the account number is persisted. It runs at start-up
  (`_kraken_startup_discovery`) and once a day (`_maybe_rediscover`, which takes a new answer only for the
  same meter points, keeps the last on a failure, and logs a change).
- **Every reader judged by agreement** goes through `engine._agreement_windows`: `tariff_kind_at`,
  `split_at`, `capped_at`, the SMB windows, `export_agreement_at`, `published_rate_at`, the asker's gate.
  Until discovery answers after a restart, they all see nothing.
- **`tariff_kind_at` falls back to the current tariff** where no agreement covers a half-hour, and the
  asker's only floor is Octopus's two years (`_ask_selection`). On an account on Intelligent now, a live
  half-hour from before it joined Octopus, inside the two years, is asked of Octopus (an API call, no
  answer), reads overdue, then hatched.
- **A changed agreement** rebuilds the rate schedules; nothing re-settles the history it covers, and the
  health strip's kept month counts do not see it.
- **The restore's phase 2** (`api_restore_confirm`, first-run design §2.3) already calls Octopus to test
  a typed key against the backup's account stamp before saving it; a v4 backup is then imported into
  `import_target.db` (`_restore_worker`) and settled by the history sweep after `engine_startup`.
- **A config period names its supplier** (`config_periods.supplier`, the user's label); the importer's
  gate 3 reads the source's.

## 2. The table

```
tariff_agreements(account, mpan, direction,          -- direction 'import' | 'export'
                  tariff_code, product_code,
                  valid_from, valid_to,               -- naive UTC; valid_to NULL = open-ended
                  fetched_at, superseded_at)          -- superseded_at NULL = current
```

One row per agreement per answer that first held it. **A cache of Octopus's answer, never a second
truth:** a discovery answer replaces the current rows wholesale in one transaction — a row the answer
still holds stays as it is; one it no longer holds gets `superseded_at`; a new one is inserted. The
superseded rows are history (an agreement withdrawn in cooling-off, a window Octopus edited), never read
for pricing. `account` is the stamp; rows for another account are never read.

**The guard** (28 Oct 2026, the user: "if there's any api/graphql deprecations we don't get in a mess").
A customer can change only recent agreements — 14 days' cooling-off, a switch reversed — so an answer
that would drop or move an agreement that started *and* ended over 30 days ago, on a meter point it
still has, is not taken: the last good copy is kept and a warning names the agreements, an API change
being the likely cause. A new agreement is always taken; so is a reversal of a recent one (its
predecessor's end reopened), and a new meter point's. Every reader — the rate stitch and the
per-agreement fetch among them — reads the stored copy, so an answer not taken is not priced either.

## 3. Writing and reading

- **Written by discovery** — start-up, the daily re-discovery, and the restore's phase 2 (§5) — and only
  from an answer discovery would take: a failure, an empty answer or changed meter points write nothing,
  as re-discovery keeps the last today.
- **Read before discovery.** `engine_startup` loads the current rows before it starts discovery, and
  `_agreement_windows` reads them (cached, invalidated on a write). A restart with the API down still
  knows every agreement.
- **No fallback once any agreement is known.** Where the table holds agreements for the account, a
  half-hour none covers is *not on an Octopus agreement*: `tariff_kind_at` and `bill_kind_at` answer
  `none`, `published_rate_at` None. The fallback to the current tariff stays only for an install whose
  discovery has never answered.

## 4. When the agreements change

The write's diff gives exact spans: an agreement added, removed, or with a moved `valid_from` /
`valid_to`. Each span goes to the history settle sweep as a span (a coordinator job, so it does not
overlap an import or a delete), and the health strip's counts are made again (its token takes the
agreements' signature). No agreements to some — an install connected after an unconnected import — is
a change over the whole history. Settled half-hours keep their bills, as the sweep does now.

## 5. A v4 import gets them first

A v4 database holds no agreements (E design: "A v4 database does not hold the agreement history"). Both routes into an import go through the restore's
phase 2, so discovery runs there, before the import worker settles anything:

- **A fresh install (the welcome page):** the key typed for the backup is tested against its stamp
  (today's phase 2); on a match, the same client runs discovery and its agreements are written into
  `import_target.db` before the swap.
- **An existing install (Data Management):** the key on disk, checked against the backup's stamp the
  same way, then discovery, then the import.
- **A v5 backup** carries its rows; discovery refreshes them before the swap, through §4's diff.
- **No credentials** (the skip, or `accept_mismatch`): the import goes ahead unconnected with no
  agreements, and reads as today. The first discovery answer after connecting is §4's whole-history
  change.

One discovery call a restore; the key test already opens the account.

## 6. Outside every agreement: another supplier, or a gap

Octopus's agreements cover Octopus's time only. A half-hour none covers — before the household joined
Octopus, between leaving and coming back, a period with another supplier:

- **Not asked.** The asker requires the half-hour's own agreement (no fallback, §3).
- **A new Import bill state, `no_agreement`** (grey, rank 0, as the Export bill's): "not on an Octopus
  agreement — EMT's price, kWh × the configured rate". `classify` reads it after `none` and `corrected`,
  for a live or API half-hour; CSV history keeps `imported` (where its cost came from matters more).
  `server._HEALTH_FILE_V` goes up.
- **The config period's supplier, before discovery.** A period whose `config_periods.supplier` is not
  Octopus is `no_agreement` and never asked, discovery or not — a fresh install whose discovery has not
  run is protected.
- **Where the label and the agreements disagree** — a period labelled Octopus that no agreement covers —
  the agreement wins, and the mismatch is logged once a day.

**CAD-only periods.** On Octopus without the API at the time: an agreement covers them, so the asker can
still fetch Octopus's bills inside the two years once connected; past them the kWh is a CAD reading, not
settled, so they stay grey (the published rule needs settled kWh). With another supplier: `no_agreement`.

## 7. Not in this step

The rates (G2: a rates table, offline pricing, a changed rate record re-pricing its span — the 1 Oct
2026 VAT edit); eras (§G.1); the export's own `no_agreement`, which exists.

## 8. Order (each its own PR)

| PR | what |
|---|---|
| G1a | the table; discovery writes it (start-up, daily); `_agreement_windows` reads it, loaded before discovery; import spec notes the table (v4 has none) |
| G1b | the restore's phase 2 runs discovery on both routes and writes the import target's agreements before the swap |
| G1c | `no_agreement` on the Import bill; the asker and `tariff_kind_at` without the fallback once agreements are known; the config period's supplier; the label mismatch log |
| G1d | §4: the change's spans re-settled by the sweep, the health counts made again |

**Tests:** a restart with discovery failing still answers `tariff_kind_at` from the table; a wholesale
replace supersedes a withdrawn agreement and keeps the rest; changed meter points write nothing; each
route writes the agreements before the import settles; an unconnected import, then a first answer,
re-settles the whole history; a half-hour before the first agreement is `no_agreement` and not asked; a
non-Octopus config period is excluded with no discovery; a moved `valid_to` re-settles only its span.
