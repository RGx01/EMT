# Changelog

All notable changes to EMT are recorded here.

Versions are CalVer — `YYYY.M.PATCH` — matching Home Assistant's own convention. `v5` is the
internal name for everything before the first public release and never appears in a version string.

## [2026.11.0b9] — 2026-11-05

- **A new install's first day or two get their devices.** After the history import, EMT records live from
  its first reading, and Octopus fills the gap between a day or two later. Those half-hours had the
  meter's figures but no devices, so an EV's charging there counted as the house's. Now, when Octopus
  fills half-hours in, EMT splits out the devices from what Home Assistant recorded. **On b8 or
  earlier:** press *Rebuild history* on each device's card to fill those days.
- **With a CAD, a new install's first day or two are there from the start.** If EMT reads your meter
  through a CAD in Home Assistant as well as Octopus's API, the history import now fills the half-hours
  between its last day and EMT's first live reading from what Home Assistant recorded. They show EMT's
  prediction, like every other day, until Octopus's figures and then the bill settle them, rather than
  appearing a day or two later. Any half-hour Home Assistant didn't record in full is left for Octopus
  to fill, as before. A CSV import does the same.
- **Flow in kWh and CO₂ shows where the energy went, without price bands.** The price bands now appear
  only in Cost, where they mean something. In kWh and CO₂ the grid goes straight to the house and each
  device. Hover a flow for its kWh, cost and carbon together, with its average price and grams per kWh.
  In CO₂, export isn't drawn: its carbon is what it displaced on the grid, which the figures above the
  chart now give as *Displaced by export*.
- **EMT stops asking Octopus questions it can't answer.** On a tariff that isn't Intelligent, EMT no
  longer asks for smart-charging dispatches every five minutes; Octopus could only say there's no
  device. An old tariff whose rates Octopus refuses is asked once, not at every rate refresh. And a slow
  history import no longer fetches your tariff's rates again each time it retries.
- **Economy 7 is priced.** With Octopus's API, an Economy 7 tariff had no rates, because Octopus gives
  its day and night prices without saying which hours are night. EMT now uses Octopus's own rule for
  smart meters: night is 00:30 to 07:30 UTC all year, which is 01:30 to 08:30 in summer. After a
  history import, EMT checks the rule against what Octopus actually charged you and says in its log
  if your meter seems to switch at other times.
- **No Export bill without an export meter.** On an account that only imports, Data Management showed an
  Export bill with every half-hour of your history "to look at", because setup had given the main
  meter an export channel with nothing to read. Export now appears only where there's an export
  sensor or Octopus knows an export meter.

## [2026.11.0b8] — 2026-11-04

- **Replace history no longer loses a device's history when Home Assistant doesn't answer.** In b7,
  *Replace history* dropped the old history first and then asked Home Assistant for the new sensor's.
  Dropping it held EMT busy for over a minute, long enough for Home Assistant's connection to drop, so
  the request went unanswered and the device was left with no history. Now EMT asks Home Assistant
  first and drops nothing unless it gets an answer, and it no longer holds everything up while it works.
  The device's card shows the replace from the moment you click, and its buttons wait until it's done.
  **If this happened to you:** press *Rebuild history* on the device's card. Your old history is also in
  the `pre-replace-history` backup.
- **Removing a device's rebuilt history works.** *Remove* on a device's card in Setup failed every time
  with "The undo failed". It now runs in the background, and the card says when it's done. Deleting a
  range of a device's data now also corrects the other devices' share of the house, which it was
  meant to and didn't. And removing a device no longer holds EMT busy while it tidies up, which could
  drop its connection to Home Assistant.

## [2026.11.0b7] — 2026-11-04

- **Flow's periods are named by what it shows.** In Flow, the period buttons now read *Daily*,
  *Monthly* and *Yearly*: a day's flow, a month's, and the years you've ticked. They were HH, Daily and
  Monthly, named after the bars the other views draw. Devices, Rate brackets and Running total keep
  theirs.
- **Setup keeps a battery's Solar / PV power sensor.** The setup wizard asks for a battery's PV power
  sensor, but never saved it, so the battery's card in Setup came up without it and the PV dial didn't
  appear. It is saved now. If you set up a battery before this, add the sensor on its card in Setup.

- **Importing your history rebuilds your devices' history too.** After you set up a device and import
  your history from Octopus or a CSV, EMT now fetches the device's history from Home Assistant and fits
  it into the imported half-hours, without asking. Before, it only offered to, the next time you opened
  Setup. It checks the sensor first, as *Rebuild history* does: if it reads more than your house drew, or
  far less, nothing is written and the device's card in Setup says why. Devices that already have
  history are left as they are.

- **You can replace a device's history after picking the wrong sensor.** Changing a device's sensor
  now asks why: Home Assistant renamed it, it's a new device, or it was the wrong sensor. Before, EMT
  assumed a rename. If it was the wrong sensor, EMT offers to replace the device's history once you've
  saved. It takes a backup, drops what the wrong sensor recorded and the history rebuilt from it, and
  rebuilds the device from the new sensor. The device's card in Setup has a *Replace history* button
  for the same thing, and shows which sensors its history came from, flagging any it no longer uses.
  Before, the wrong sensor's figures stayed: *Rebuild history* only fills half-hours that have none.

## [2026.11.0b6] — 2026-11-04

- **IOG-SMB history imported from Octopus gets its House/EV split.** After you import your history on
  IOG-SMB, EMT is meant to ask Octopus for each half-hour's House/EV breakdown, which is what settles
  the half-hour's price there. It only asked where it had seen the car charge, and imported history
  has no record of that, so it asked for none. The half-hours stayed light amber, and a smart charge
  Octopus billed off-peak in the day stayed at the day rate. EMT now asks for every imported IOG-SMB
  half-hour, newest first and paced, and settles each one as its answer arrives.
- **Flow shows your export by price, and plunges in Cost.** Your export now leaves the house through
  the price it earned, in the same 2.5p bands as your import, so on Agile Outgoing or Flux you can see
  what each price paid. In Cost, an Agile plunge's credit is drawn too: from whatever used the energy to
  *Plunge credit*, and paying to export at a negative price to *Paid to export*. Before, the credit
  was only in the figures above the chart.

## [2026.11.0b5] — 2026-11-04

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
