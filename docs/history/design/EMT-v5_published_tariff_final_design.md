# EMT v5 — a published tariff's half-hour is final by its agreement

> **History — a design record.** What was decided, when and why, as it stood then; parts describe code that has since changed. How EMT works today: [reference/pricing-and-settlement.md](../../reference/pricing-and-settlement.md). PR numbers (`#NNN`) are in the private archive, `RGx01/EMT-archive`.

*Decided 28 Oct 2026 (the user: "if we have all the published rates for the tou or agile and the
settled kwh then those blocks should be green regardless of api reachability"; "csv/pdf can only ever
be grey on any tariff"). Code facts read at `dev` 7200507; staging's from its 28 Oct whole-history
support file.*

**What this note decides:** when the Import bill reads a published-tariff half-hour as final
(`applied`, green) — by the half-hour's own agreement and the settled kWh × that agreement's published
rate, checked, at any age. It does not change Intelligent, CSV history, the asker, or `settle()`.

---

## 1. What happens now

On a published-rate tariff (Agile, Go, Cosy, Flux, Flexible, Tracker) Octopus's bill for a half-hour
is the settled kWh × that half-hour's published rate; nothing re-bands it afterwards, as a dispatch
does on Intelligent. `settle()` already prices every such half-hour that way, the bill aside
(`settle.settle`, `kind != 'intelligent'`: the schedule's `clock_rate`), wherever the stitched schedule
covers it (`_agreement_priced_ok`); where it does not, `no_rate`, and the half-hour keeps what it holds.

`settlement_state.classify` reads it differently by age and origin:

| half-hour on a published tariff | reads | why |
|---|---|---|
| live, settled kWh, inside Octopus's two years | `applied` | the published rule (no cost check) |
| live, settled kWh, past the two years | `imported` (grey) | "no bill, past the two years" is tested first |
| v4 API import (an `IMPORTED` bill), any age | `imported` (grey) | the `IMPORTED` bill is tested first |
| CSV import | `imported` (grey) | the CSV's own price |

The two-year test was written for Intelligent, where only Octopus's cost is final; it says nothing
about a published tariff, whose rule needs no consumption data from Octopus. Published rates do not
age out: the reach limits consumption, not the products' rates.

`tariff_kind_at` falls back to the **current** tariff where no agreement covers the half-hour — safe
for the asker, wrong for this: a household on Intelligent before discovery's oldest agreement and on
Agile now would read that history green on Agile's rule.

The agreements are **not stored**: discovery fetches them from Octopus at start-up and daily and holds
them in memory (`_kraken_discovery`, `_agreement_windows`). Until it has run, nothing is known.

## 2. The rule

A half-hour is final on a published tariff where:

1. **its own agreement is known and published** — `_agreement_era('import', start)` covers it, and the
   code is not Intelligent; no fallback to the current tariff;
2. **the stitched schedule prices it** (`_agreement_priced_ok`) and gives a rate there;
3. **its kWh is settled** — the DCC's (`imp_kwh_api`), or an API import's, which is Octopus's;
4. **it is not CSV history** — CSV and PDF stay grey on any tariff;
5. **no Octopus bill decides it** — an Octopus bill (not `IMPORTED`) keeps today's comparison against
   the bill, the better evidence.

Then it reads `applied` where the stored cost is the settled kWh × that published rate (within
£0.00005, a two-hundredth of a penny: rounding), and `differs` (red) where it is not. The check is the
point on Agile, where every half-hour has its own rate; for a v4 API import it checks v4's arithmetic
against the tariff instead of taking it on trust. At any age.

Where any condition fails, `classify` reads as it does today.

`engine.published_rate_at(start)` gives classify the rate (£/kWh inc VAT) where 1–2 hold, else None;
the server passes it with `kind_at`. The panel's note for a v4-priced half-hour that reads `applied`
says it is a published tariff's bill: "v4's price, <kWh> × <rate> — final on a published tariff".

**Not changed:** Intelligent (only Octopus's cost is final); a live half-hour with no settled kWh
(red, then hatched); zero usage (`none`); a correction (blue); the export states.

## 3. What it moves

Staging's account has two published agreements, Go and Flexible, both before 30 Jun 2024 — all CSV
history, so **nothing on staging moves**. The cases are fixtures: an Agile agreement with a rate a
half-hour; a published half-hour past the two years; a v4 API import on Go; a cost that is not kWh ×
rate; a half-hour before the oldest agreement; CSV on a published agreement; an Octopus bill on one.

The health strip keeps its month counts across restarts, so `server._HEALTH_FILE_V` goes up. Its
counts depend on discovery, as `kind_at` already does.

## 4. Order

One PR: `engine.published_rate_at`, `classify`'s rule, the server's context, the health file version,
the panel note, the settlement surfaces design's `applied` row, and the tests above.
