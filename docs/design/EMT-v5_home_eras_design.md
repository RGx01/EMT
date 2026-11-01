# EMT v5 — home eras (BL-101)

*Status: decided 1 Nov 2026 (§6); PRs 1 and 2 built. Takes up ROADMAP §G.1's "user-defined periods". Code facts
read at `dev` after #376.*

---

## 1. What the user asked

A timeline of the home's changes — *Added solar*, *Increased storage*, *Added ASHP*, *Added A2A* —
each with a start date, an optional end date and the user's own notes, and optional fields such as
the manufacturer, kW and kWh. Shown on Usage Stats as the tariffs are; a way for the spiral to line
its year up with a tariff or an era; kept with the site's settings, perhaps behind a settings cog
whose menu opens several modals (*Eras*, *Bar colours*).

## 2. What an era is

**A dated note about the home, for display.** Never read for pricing, attribution, settlement or a
bill. It changes no figure; it labels and lines them up.

| field | | |
|---|---|---|
| `label` | required | *Added solar*, free text |
| `kind` | required | `solar`, `battery`, `heat_pump`, `a2a`, `ev`, `insulation`, `house_move`, `meter_change`, `other` — its icon and which optional fields it offers |
| `start` | required | a local date |
| `end` | optional | a local date; none while it lasts |
| `manufacturer`, `model` | optional | free text |
| `kw` | optional | solar kWp, a heat pump's or A2A's rated kW, an EV charger's kW |
| `kwh` | optional | a battery's capacity, an EV's battery |
| `site` | optional | a house move's site name — **never an address** (the user): EMT stores no address, and a move is named as the site is |
| `notes` | optional | free text |
| `device` | optional | a configured device it is about (the battery's meter), for a link to it |

Eras overlap — the solar is still there when the battery is added — so they are a list, not a
sequence like the tariff agreements.

**Found, not only entered (PR 3).** Two kinds EMT can see for itself:
- **a house move:** a new supply point (MPAN) in the stored agreements — an era at its first
  agreement's start, named by the site name. The MPAN is never shown or put in the era.
- **a meter change:** the meter's serial changing at discovery. EMT keeps no serial history today,
  so it starts keeping a fingerprint of the serial (a hash, never the serial) and dates the change
  when it moves.

A found era is marked as found, can be edited or deleted, and is not found again once deleted.

**Kept in the store's meta** (`home_eras`, a JSON list with an id each), as BL-97's EV style: a
short list, read whole; no schema change and nothing for the importer (a v4 database has none). It
is in every backup with the rest of the store. Edited through `GET` / `POST /api/home-eras`; a save
does not restart the engine.

## 3. Where they show

### 3.1 Usage Stats: markers, not a lane

The tariffs are a lane because one follows another. Eras overlap, so a lane would stack. Each era is
a **marker**: a dashed line at its start, across the bars, with a small chip at the top holding its
icon (☀️ 🔋 ♨️ ❄️ 🚗 🧱 •); an ended era a second, fainter line at its end. Hover a chip for the
era's label, dates and fields. An **Eras** checkbox beside *Tariffs* turns them on and off, saved
with the view. On Monthly and Yearly a start falls within its bar by its day, as the tariffs' edges
do (`barTariffX`).

### 3.2 The year's start — on the accumulation chart

*The user (1 Nov 2026): move the spirals into Usage Stats as an accumulation chart, viewed as a line
or a spiral.* The **Year starts** choice belongs there: 1 January (as now), the billing anniversary,
a tariff's start, or an era's start. The year runs from the chosen day — the spiral turned so the
day is at the top, the line's year reset at it — so the year before the solar and the year after it
each start at nought together. Designed with the accumulation chart, its own roadmap item.

### 3.3 Before and after — not now

*The user:* not needed in the modal. Left for later, if at all; it would say *not weather-corrected*.

## 4. The cog

A **⚙︎** in the Charts page's header, beside *PDF*, for what belongs to the charts but not to one
view. Its menu:

- **Home eras…** — the eras modal: the list, newest first, with *Add*, *Edit* and *Delete*, and the
  form (§2's fields, the optional ones by kind);
- **Bar colours…** — every meter's chart style, as each device's Settings tab has it, in one place.

## 5. Not changed

- Pricing, attribution, settlement and bills: an era is display only.
- The tariff overlay, and the spiral's two modes, which keep their behaviour with *1 January*.
- No new Octopus calls.

## 6. Decided (the user, 1 Nov 2026)

1. **Markers**, not a lane (§3.1).
2. **No before-and-after** in the modal for now (§3.3).
3. **A bar colours modal** of its own (§4).
4. **House move and meter change** among the kinds, found from the data where it can be (§2); an
   address never stored — a house move is named by the site name.
5. **The spirals move into Usage Stats** as an accumulation chart, line or spiral; the year's start
   goes with it (§3.2).

## 7. Order

| PR | what |
|---|---|
| 1 | **built:** the store (`home_eras`), `GET` / `POST /api/home-eras` (cleaned: kinds, dates, lengths, numbers), and the eras modal behind the ⚙︎ on the Charts page — list, add, edit, delete |
| 2 | **built:** Usage Stats' markers (Monthly by Date and Daily; not HH, a day) and the *Eras* checkbox, on until turned off |
| 3 | eras found: a house move from a new supply point, a meter change from the serial's fingerprint |
| 4 | the bar colours modal behind the same ⚙︎ |
| — | the accumulation chart with the year's start (its own roadmap item) |
