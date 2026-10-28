# EMT v5 — G2: tariff rates, stored

*Draft, 28 Oct 2026 (the user: "with G2 aren't we in danger of locking in rates that may get changed by
octopus … so we could never refetch"). The second half of ROADMAP §G; the agreements are G1. Code facts
read at `dev` ec64f8c.*

**What this note decides:** where EMT keeps the rates Octopus publishes for each of the account's
tariffs — unit rates and standing charges, import and export — how a refresh replaces them, how a change
Octopus makes to a past rate re-settles what it priced, and how a broken answer is told from a real
edit. It is about the **published rates for an agreement's tariff**, not the rates on the blocks: those
are `settle()`'s output (a bill first, else the settled kWh × the published rate), nothing reads them
back as evidence, and a re-bill or a re-fetch overwrites them as now. It does not change the bills.

---

## 1. What exists

- **Where rates come from.** Octopus's REST products API: per tariff, `standard-unit-rates`; for
  IOG-SMB's four-rate feed, `day-unit-rates`, `night-unit-rates` and the EV device rates; and
  `standing-charges`. Each record: `value_inc_vat`, `value_exc_vat`, `valid_from`, `valid_to`
  (`payment_method` on some). `kraken_api_client.get_unit_rates` pages them.
- **What EMT builds from them,** in memory only (`_kraken_rate_schedules`, `_kraken_standing_schedule`):
  each agreement's schedule as published (`kraken_rates.agreement_schedules`), IOG-SMB's night windows
  **synthesised** from its flat day/night records 23:30–05:30 local, two days ahead (BL-52,
  `_synthesize_iog_tou_windowed`), the import stitched across the agreements (BL-54), and EMT's VAT
  calendar applied once to the stitch.
- **When.** Every 6-hour poll cycle (`_KRAKEN_POLL_INTERVAL_S`) and on connect. A closed agreement's
  rates are cached for the life of the process "as they cannot change"; a running one is fetched each
  time (v4 #513).
- **What goes wrong.**
  - A restart with the API down has no rates: nothing to price from until it answers.
  - **Octopus does edit past rates in place** — the 0% VAT change of 1 Oct 2026 restated rate records
    (ROADMAP §G: "When Octopus edited the 1 Oct 2026 rate records in place"). A closed agreement's edit is not seen until the
    engine restarts, and when it is, nothing re-prices: no copy to compare against. v4 needed three heals
    for it (4.5.17–4.5.19).
  - The REST API has no schema to introspect, so a deprecation there would not be caught (ROADMAP §G).

## 2. What is stored: Octopus's records, not EMT's schedules

```
tariff_rates(tariff_code, rate_type,               -- 'standard-unit-rates', 'day-unit-rates', …,
                                                   -- 'standing-charges'
             valid_from, valid_to,                 -- naive UTC; valid_to NULL = open-ended
             value_inc_vat, value_exc_vat, payment_method,
             fetched_at, superseded_at)            -- superseded_at NULL = current
```

The records **as Octopus returns them** — never the synthesised windows, the stitch or the VAT-applied
schedule. Those stay built in memory from the records, as now; a change to how EMT builds them (BL-52's
window, the VAT calendar) needs no migration of stored data. **A cache of Octopus's answer, never a
second truth:** a fetch of one tariff's rate type replaces that pair's current records wholesale — a
record the answer still holds stays, one it no longer holds or holds with another value or window is
superseded (kept as history, never priced), a new one is inserted.

## 3. Nothing is locked in: every edit is taken

The rates' guard is the **opposite of the agreements'**. Octopus edits past rates for real — the VAT
change did — so a rule on age would lock in exactly the edits that matter. The guard catches only an
answer that is **broken**, not one that differs:

- a tariff's rate type that held records answering **empty**, or
- records **missing a field** the build reads (`value_inc_vat`, `valid_from`), or a window with
  `valid_from >= valid_to`,

keeps the last good copy and logs a warning naming the tariff — an API change being the likely cause,
the REST check §G asked for. Any other answer is taken, at any age: a changed value, a moved window, a
record added or removed.

## 4. A change re-settles what it priced

The replace's diff gives each changed record's window; clipped to the agreements that use the tariff
(G1's stored agreements), those are spans, queued to **G1d's re-settle queue** and worked off by the
settle sweep's job, after the schedules are rebuilt from the new records. A bill still outranks the
published rate, so a billed half-hour is unchanged; an unbilled or published-tariff one is re-priced.

**The first fill is not a change.** A table with no records for a tariff — the upgrade, a v4 import —
stores the answer as the baseline: what it priced was already priced from the same API in memory, and a
v4 import's history is settled by the sweep after the import.

## 5. Offline first, and closed agreements re-checked

- **Start-up builds the schedules from the stored records** before any fetch, so a restart with the API
  down prices as before it went down. A fetch replaces them when it answers.
- **Closed agreements are re-checked daily,** not once a process: with the rate refresh after the
  daily re-discovery, each closed agreement's records are fetched again and diffed. About one call per
  closed agreement a day (staging: seven — six import, one export; its two empty export windows are
  skipped) — the in-place edit is what this is for. A running agreement is
  fetched each 6-hour cycle, as now.
- **A v5 backup** carries its records; **a v4 import** has none, and the first refresh fills them (§4).

## 6. Not in this step

The bills (G2 does not store or guard them); eras (§G.1); the live write guard's canonical-rate check
(§G "Also uses it"), which can follow once the records are stored.

## 7. Open

1. **The products API's reach.** As far as we know it serves a tariff's records for the tariff's whole
   life, not two years. A probe against staging's oldest agreement (Flexible, June 2023) should confirm
   it before §5's offline pricing relies on it.
2. **Size.** An Agile tariff has 48 records a day — about 17,500 a year for import, the same for Agile
   export. Fine for SQLite; the support file should keep the table (no time column, so whole) or cut it
   to the span (§2 can add one).

## 8. Order (each its own PR)

| PR | what |
|---|---|
| G2-0 | the probe: the products API's reach for each of staging's agreements (`.build/`) |
| G2a | the table; each fetch replaces its tariff's rate type; the diff and the guard logged only |
| G2b | start-up builds the schedules from the stored records before any fetch |
| G2c | a change's spans to G1d's queue; the first fill a baseline |
| G2d | closed agreements re-checked daily |
