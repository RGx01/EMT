# EMT v5 — G2: tariff prices, stored for display

*Decided, 31 Oct 2026 (§8): display only; the timeline's minor ticks; the hover's prices for the tick period under the pointer; only what changes by the half-hour left out. The second half of ROADMAP §G, re-scoped. G2 as first proposed — a store of
Octopus's published rate records that pricing reads, with a diff that re-prices — was dropped on
28 Oct 2026 (#326). The user brought it back on 31 Oct 2026, narrower: the tariff's standing charge
and unit rates, for the Usage Stats tariff overlay's hover (#353, #354), **Agile's unit rates left
out**. Code facts read at `dev` after #353.*

**What this note decides:** what EMT keeps of each agreement's prices, how it gets them, where they
are shown, and what they are **not** used for. It does not change how anything is priced.

---

## 1. Why the first G2 was dropped, and why this one is different

ROADMAP §G records the drop. Two reasons:

- **Unproven:** Octopus has changed the carve and the bill after the fact, not the rates. The one
  rate edit seen, the 1 Oct 2026 VAT restatement, does not reach v5, because EMT prices VAT from its
  own calendar.
- **Costly:** on Agile, every half-hour's rate would be stored twice: the published record and the
  block's priced rate. It needed a table, a diff and a guard.

This G2 avoids both:

| | dropped G2 | this G2 |
|---|---|---|
| read by | pricing (offline-first) | the overlay's hover, and nothing else |
| on a change | diff, re-price the span | replace the stored prices; nothing re-priced |
| Agile | every half-hour stored | standing charge only; the unit rates are "half-hourly" |
| shape | Octopus's records | **price periods**: one row per price, from and to |

Because no pricing path reads it, a wrong or missing row can only make a hover wrong, never a bill.

## 2. What the data looks like

Standing charges change **inside** an agreement. On the staging copy, the daily standing charge
(inc VAT, from the blocks) changed seven times between Jun 2023 and Oct 2026:

- **Four changes fell inside a variable agreement.** These were at the price-cap quarters: Apr 2024,
  Oct 2024, Apr 2025 and Oct 2025.
- **Two coincided with a change of agreement.** One of the agreement changes (to IOG-SMB on 26 Aug
  2026) left the standing charge as it was.
- **One fell inside a fixed agreement.** On 1 Oct 2026 the inc-VAT figure moved only by the VAT
  change, 5% to 0% (exactly ÷ 1.05); its ex-VAT figure held.

One day is left out of that count: the blocks of 31 Mar 2024 carry the day before's standing charge
and the day after's added together. That is a data finding of its own, not a price change, and is
looked at separately.

So **one row per agreement cannot hold a tariff's prices.** Each agreement has dated price periods
for:

- its standing charge;
- each of its unit-rate bands (none for a flat tariff, off-peak and peak for Go and Intelligent);
- its export rate.

Unit-rate changes inside agreements were not measured here; the blocks' stored rates are mixed
with bills and corrections. The first PR measures them from the fetched records (§7).

**How Octopus sends them:**

- **Standing charges:** one record per price change (`standing-charges`).
- **A flat unit rate:** the same.
- **Go and the uncapped Intelligent (`standard-unit-rates`):** two time-windowed records for every
  day — off-peak 23:30–05:30 local, peak the rest. That is about 730 records a year for two prices.
- **IOG-SMB:** two flat records, day and night. EMT rebuilds the windows (BL-52,
  `_synthesize_iog_tou_windowed`), only to two days ahead.
- **Agile:** a record every half-hour, about 17,500 a year.

**Stored as Octopus sends them, Go's records would be nearly as verbose as Agile's.** So the store
keeps the prices, not the records: consecutive days with the same price in each band become one
period.

## 3. The table

```
tariff_prices(tariff_code, agreement_from,      -- the agreement (G1's tariff_agreements row)
              kind,                             -- 'standing' | 'unit'
              band,                             -- '' (flat) | 'off_peak' | 'peak' | 'dynamic'
              window,                           -- 'HH:MM-HH:MM' local for a windowed band, else NULL
              value_exc,                        -- pence (per kWh, or per day), ex-VAT; NULL for 'dynamic'
              valid_from, valid_to,             -- naive UTC; valid_to NULL = open
              fetched_at)
```

- **Ex-VAT, as published.** The inc-VAT figure is made at read time from EMT's VAT calendar
  (`vat_calendar.rate_lookup`), so a price period across a VAT change reads as two. 1 Oct 2026 needs
  no rewrite, and the store never carries Octopus's restated VAT.
- **Replaced per agreement, per kind.** A new summary for an agreement replaces its rows in one
  transaction. Nothing is kept as history, and no diff is made: this is a display cache.
- **`dynamic`: a tariff whose unit rate changes by the half-hour.** One row with no value, and its
  standing charge still stored. That is:
  - **Agile** and Agile Outgoing (the AGILE family);
  - **any agreement whose records hold more than 8 windows in a local day**, for such a product
    not named. Agile has 48; Go and Intelligent have 2, and Flux and Cosy a handful.

  **Tracker is stored.** Its price changes daily, one rate for the whole day, so it summarises to
  about 365 periods a year (the user, 31 Oct 2026: only what changes per half-hour is left out).

## 4. How the prices arrive

**What the code fetches today:**

- **The 6-hourly refresh** (`_refresh_kraken_rate_schedules`):
  - the **current** import and export tariffs' rates;
  - the **current** import standing charge;
  - the import rates **of every agreement**, through `kraken_rates.agreement_schedules` (stitched).
    Closed agreements are cached for the process; the running one is fetched every cycle.
- **The API import** (`_build_channel_rate_segs`, `_build_channel_standing_segs`): every agreement's
  rates for each channel, and its standing charge for import, through the same
  `agreement_schedules`.

**The summary is made where `agreement_schedules` builds a schedule**, and written to the table only
when it differs from the stored rows. Import's unit rates then cost no new call.

**The running agreement's prices** come from what the refresh already fetches each cycle: the current
import and export rates and the current standing charge. The summary is made from those schedules too.

**Two kinds the refresh does not fetch per closed agreement:**

- **standing charges;**
- **export rates.**

These need one fetch per closed agreement that has no stored rows, and per kind. Once a closed
agreement's rows are stored, it is never fetched for this again: a closed agreement's prices cannot
change, and now the table remembers that, not the process.

On the staging copy that is **about 7 calls, once**: 6 closed import agreements' standing charges and
1 closed export agreement's rates. Its other export agreements are a running one and two blips
`agreement_schedules` already skips. The calls are capped and paced as EMT's other new Octopus calls
are, since four instances share an account's rate limit.

**As built (G2b, 31 Oct 2026):** `engine._fetch_closed_agreement_prices`, in each poll cycle after the
rate refresh:

- at most **4 agreement-kinds a cycle**, 2 s apart, each logged;
- the same REST tariff pages `agreement_schedules` reads (`standing-charges`,
  `standard-unit-rates`); not GraphQL, and not EMT's own store, whose rates mix bills and corrections
  with the tariff;
- a fetch that brings nothing back is tried again after a day, three times at most, counted in
  kraken_state (`tariff_prices_tried`) so a restart does not reset it;
- an Agile agreement's unit rates are never fetched: its `dynamic` row is written from its window.

Worst case on staging: 7 agreement-kinds, two cycles, once ever.

**Without the API (CAD, local billing, another supplier):** no agreements, so no prices. The hover
shows what it shows now.

**A v4 import:** v4 has no such table. The prices fill on the first refresh after discovery has
stored the agreements (G1 §5). The import spec notes the table, as it noted `tariff_agreements`.

## 5. Where it is shown

`GET /api/tariff-agreements` (#353) adds each agreement's price periods:

- inc and ex VAT;
- the band and its window;
- from and to, as local dates.

It still carries no MPAN and no account number.

**The overlay's hover** gains the prices **in force at the hovered date**, for example:

    Import: Intelligent Go
    E-1R-INTELLI-VAR-22-10-14-…
    12 Jun 2024 – 30 Jun 2025 · 12.6 months
    Off-peak 7.00p/kWh (23:30–05:30) · Peak 29.84p/kWh
    Standing charge 54.85p/day
    Prices changed 3 times in this tariff

(the figures are illustrative)

For Agile the unit-rate line reads "Unit rates: half-hourly".

Monthly by Date, Daily and HH all show it. On Monthly, the hovered date is the date under the pointer
within its bar. **Between two ticks, the hover shows that tick period's prices** (the user, 31 Oct
2026): the prices in force from the tick before the pointer to the one after it.

**Minor ticks on the timeline** (the user, 31 Oct 2026). Each band already has a tick at a change of
agreement. It gains a **minor tick** — shorter and lighter — at each price change inside an
agreement:

- **What ticks:** a change of the standing charge, and a change of any unit rate (each band of
  Go and Intelligent, the flat rate, the export rate).
- **Agile and anything else `dynamic`:** standing-charge changes only, since the table stores no
  unit rates for them. Tracker ticks daily.
- **Where:** at the price period's start, placed by date as the agreement ticks are. A minor tick
  that falls on an agreement tick is not drawn: the agreement tick already marks it.
- **Which price:** as shown, inc VAT, so a VAT change (1 Oct 2026) ticks too, although the stored
  ex-VAT price does not change.
- **The import and export bands each carry their own.** The standing charge is import's: it ticks
  on the import band.

On Monthly several changes can fall in one bar. They are drawn at their dates, and two closer than
2 px are drawn once.

## 6. Not in this step

- **Pricing from the store.** The restart fallback that §G keeps as an option (price from the
  last good schedule while the API is down) would read this table for non-dynamic tariffs. It is a
  separate decision: it makes the store a pricing source, which brings back the first G2's "a wrong
  row is a wrong bill".
- **Re-pricing when a stored price changes.** Unproven, as in #326.
- **Gas.** None of this applies to gas.
- **Eras** (§G.1).

## 7. Order (each its own PR)

| PR | what |
|---|---|
| G2a | the table; the summary (records → price periods, the `dynamic` rule), written where `agreement_schedules` builds and from the refresh's current schedules; a log line of each agreement's period counts, which measures §2's open question on the real records |
| G2b | the one-off fetches: closed agreements' standing charges and export rates, capped and paced |
| G2c | the route's price periods (VAT from the calendar); the hover's prices at the hovered date; the minor ticks at each price change |

## 8. Decided

**Decided** (the user, 31 Oct 2026):

- **Display only.** The restart fallback (§6) is not part of G2.
- **The timeline:** a minor tick at each standing-charge and unit-rate change; on Agile, standing
  charges only, since no Agile rates are stored (§5).
- **The hover:** the prices in force at the hovered date (§5). The minor ticks show where each price
  period starts.
- **The `dynamic` rule:** only what changes by the half-hour — the AGILE family, and any agreement
  with more than 8 windows in a day (§3). Tracker, a daily price, is stored. The 50-period guard is
  dropped (the user: too much).
- **The hover between ticks:** that tick period's prices (§5).
