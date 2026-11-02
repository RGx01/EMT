# EMT v5 — Agile for everyone (BL-103)

*Status: decided and built, 1 Nov 2026 (§5). The user's idea (1 Nov 2026). Code facts read at `dev` after #385.*

---

## 1. What it is

The Overview's Agile card (BL-100) shows an Agile account its own half-hourly prices, from the rate
schedules the poll already holds. **On every other account it shows nothing.** The user: show it to
everyone, as *Agile in your region*.

Octopus publishes Agile's prices for anyone, without an account
(`/v1/products/<product>/electricity-tariffs/<tariff>/standard-unit-rates/`). For an account not on
Agile, EMT needs three things it does not have:

- **the current Agile product** — its code changes as Octopus launches versions
  (`AGILE-24-10-01`), so it is read from the product list (`/v1/products/`) and kept;
- **the region** — the letter that ends the account's own import tariff code
  (`E-1R-…-C` → C), which EMT already holds;
- **the prices** — a new fetch.

## 2. The Octopus calls (the API rule: capped, paced, persisted)

- **The product:** the product list once a week, the newest import Agile product still on sale
  (`AGILE-…`, not `OUTGOING`, no `available_to`), kept in the store's meta.
- **The prices:** once a day, soon after Octopus publishes tomorrow's (about 4pm; EMT asks at 16:30
  local and again at 18:00 if they were not there), from yesterday to the end of tomorrow — one call.
  Kept in the store's meta, so a restart asks for nothing it has.
- **Not on an Agile account** (it has them from its own schedules), nor without API credentials.
- So: about one call a day, and one a week. No GraphQL, so the account's points are not spent.

## 3. What the card shows

The BL-100 card, as it is, titled **Agile in your region (C)** — the same bars, now line and cheapest
hour; the price is Agile's, not the account's. Import, and Agile Outgoing for an account that
exports (§5), with the comparison line beneath.

## 4. Not changed

The card on an Agile account; pricing, settlement and bills. Agile's prices here are display only.

## 5. Decided (the user, 1 Nov 2026)

1. **The comparison line**, yes: *"Your last 30 days at Agile's unit rates: import £x against £y paid
   · export £a against £b earned (standing charges aside)"* — the account's own half-hours at the
   kept prices, over the half-hours with both. The first fetch reaches 31 days back.
2. **Agile Outgoing too**, for an account that exports.
3. **Shown by default** on every account with the supplier API and a region.

*As built:* the prices asked from 16:30 local, then 90 minutes apart while they are not there, at
most four times a day; kept 32 days.

## 6. Order

One PR (built): the product list method, the daily fetch and its record (`agile_public`,
`engine._fetch_agile_public`), the card on every account, the comparison, Agile Outgoing.
