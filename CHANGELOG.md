# Changelog

All notable changes to EMT are recorded here.

Versions are CalVer — `YYYY.M.PATCH` — matching Home Assistant's own convention. `v5` is the
internal name for everything before the first public release and never appears in a version string.

## [Unreleased]

- **IOG-SMB history waiting for its split is amber, not red.** After importing your history on
  IOG-SMB, a half-hour Octopus billed off-peak in the day showed red on the heatmap until EMT had
  fetched its House/EV breakdown. It is light amber now — *Octopus's cost is in, waiting for its
  split* — and turns green when the breakdown arrives.
- **The tariff you're on, when Octopus lists a blip.** EMT took the latest-starting agreement as
  your current tariff when every agreement had an end date — so on a fixed-term tariff, a
  zero-length agreement Octopus lists after it (an Agile blip on one tester's account) became the
  "current" one, and new half-hours took its standing charge. It now takes the agreement in force
  today.
- **Flow and Rate brackets agree with Devices on an EV charger.** With an EV device, the Devices view
  shows Octopus's split of the car where it has one; Flow and Rate brackets showed the charger's own
  reading, a little off, with the house off the other way. They show the same as Devices now.
- **Span, in Devices, Rate brackets and Flow.** Pick a stretch to look at: a chapter of your home,
  from one era's start to the next era's (and *Before the first era*); an era on its own, from its
  start to its own end or today; or a tariff, from its start to its end, with each stretch between its
  price changes under it. Devices and Rate brackets draw a bar a
  day for a span of up to two months, a bar a month beyond; Flow draws the span as one flow. In Flow,
  Span takes Yearly's place, which showed the same as Monthly.

## [2026.11.0b4] — 2026-11-04

- **Usage Stats → Flow.** Where the grid's electricity went, as a Sankey: from the grid, through the
  price it was charged (in 2.5p bands, the small ones together as *Other*), to the house and each
  device, with export leaving the house — in kWh, £ or CO₂, for the day, month or year on screen.
  Hover a flow for its kWh, cost and price; the table below lists every flow.
- **Rate brackets shows the car on an account with no devices.** Octopus's split of your EV was
  counted in the house there; it is a series of its own now, as in the Devices view.
- **A new account is offered its history.** Finishing the setup wizard on Octopus's API now asks,
  once, whether to bring in your history — up to the two years Octopus keeps. *Import my history*
  opens the import, which shows what it will fetch before it starts.
- **Data Management's *Whole history* menu is *History*.**
- **Running total: one legend, and a year keeps its colour.** The line view had a legend on each
  card; it now has the spiral's, one above the cards. And a year's colour no longer changes when you
  hide another year: each year has its own, on the line, the spiral and the legend.

## [2026.11.0b3] — 2026-11-03

- **Setting up a new account on Agile works.** Connecting to Octopus in the setup wizard waited for
  your tariff's whole price history before it answered — on Agile, years of half-hours — and gave
  up after 45 seconds, so the wizard stayed on its first page with "Could not connect" and never
  set up your meter. It now answers as soon as Octopus has, and fetches the prices after. The same
  applies to reconnecting in Settings.
- **Fewer requests to Octopus for your prices.** EMT fetched your tariff's prices from the day the
  tariff launched — on Agile, back to 2018, close to a hundred requests each time it refreshed them.
  It now starts from when your account joined the tariff, or your oldest recorded half-hour if that
  is earlier.
- **A new account keeps its supplier.** Setting up from scratch saved the meter and the site but
  not the supplier you chose.
- **A configuration that could not be saved says so.** Saving Settings could report *Saved* when
  the database refused the change; it now shows the error.
- **No "Finishing your upgrade" on a new install.** Connecting to Octopus before your meter was set
  up showed the upgrade banner, with "null days left".
- **The EV's colour reaches the charts straight away.** On an account with no EV device, a colour
  chosen on Settings' EV tab now redraws an open Usage Stats page at its next check, as a device's
  colour always did. Before, it waited until something else changed.
- **The Running total spiral has a legend** — each year in its colour, above the cards and in the
  enlarged view, and in the PDF.

## [2026.11.0b2] — 2026-11-02

- **Error messages say what happened, and nothing of yours.** When Octopus refuses or fails a
  request, the page now says so in plain words — *Octopus refused the API key — check it in
  Settings*, *Octopus's rate limit was reached; EMT tries again later*, *Octopus answered with an
  error (HTTP 404)*. Before, an import, gap fill or re-fetch that failed, or connecting in Settings,
  could show Octopus's own error text, which named your account number, MPAN or meter serial. Any
  other unexpected error says where and when it happened, and the log has the detail.
- **Backups are only ever read from the backups folder.** Restoring or listing a backup checks its
  name the way downloading and deleting one always did.

## [2026.11.0b1] — 2026-11-02

**The first beta**, for a few invited testers (see [BETA.md](BETA.md)): EMT installs
beside v4 as its own add-on (`emt`), and starts from scratch or brings v4's history across from a
backup of v4 4.5.23 or later. [docs/ROADMAP.md](docs/ROADMAP.md) has the state of each workstream.
EMT is under the [PolyForm Noncommercial License 1.0.0](LICENSE): free for any noncommercial use.
