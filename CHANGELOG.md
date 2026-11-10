# Changelog

All notable changes to EMT are recorded here.

Versions are CalVer — `YYYY.M.PATCH` — matching Home Assistant's own convention. `v5` is the
internal name for everything before the first public release and never appears in a version string.

## [2026.11.0b18] — 2026-11-10

- **A support file hides your site name too.** People often name the site after the house. With *Hide my
  meter and account details* ticked, every site name EMT holds, including your home eras' and an old
  address's, now becomes a placeholder like the MPANs and account number.
- **The support file says what it still holds.** The dialog, its help, BETA.md and the issue forms now say
  that anything attached to a GitHub issue can be downloaded by anyone, and that the file still holds your
  usage and costs, device names and the log, with a way to report a problem without posting it.

## [2026.11.0b17] — 2026-11-10

- **EMT is now a public beta.** Anyone can add it to Home Assistant and try it before its first release; the
  README and BETA.md say how, and what to send back.
- **Home eras can be added again after closing the window mid-way.** Closing it with ✕, a click outside or
  Escape while adding an era left "+ Add an era" hidden, so the next time it offered Edit only, until an edit was
  cancelled. It now opens with the button every time.
- **A v4 backup too old to restore now says what you can do whatever v4's add-on store offers.** It said to open
  the backup in the last v4 release first, which helps only while Home Assistant can still install v4. It now
  says to update v4 to 4.5.23 or later if the store still offers it, and otherwise to bring back about two years
  of your history from Octopus. A backup holding prices in pence now asks you to open it in v4 again, which works
  with the v4 you have.

## [2026.11.0b16] — 2026-11-07

- **Any card on the Overview can be hidden.** Each one now has the hide button the Agile cards have, including the
  live gauges, your devices and the Today, This Bill and This Year cards. Hiding applies to everyone using this EMT,
  and a link at the foot of the page brings a card back.
- **The Smart Charging card no longer calls your earlier charges "outside a smart charge".** Octopus only gives
  recent dispatches, so after a fresh install every night before EMT's first check read that way, in red, and the
  6-hour bar on the first night was far too low. Those nights now come from Octopus's split of your bill, as
  ordinary charges with their bars, cost and share of the 6 hours.
- **The card's "Also charged outside a smart charge" line stays with its charge.** It showed the latest such run in
  the last 7 days, even under a charge days later. It now appears only for the same noon-to-noon day.
- **Repairing a gap no longer re-prices your whole history.** Filling 13 half-hours started a walk over every day
  you have, under a banner saying "Finishing your import". A repair now prices only what it filled, with no banner.
- **The re-pricing banner no longer tells you to avoid restarting.** A restart is safe, because the walk carries on
  from where it stopped. It also now says "days of history left", as that count is not a time estimate.
- **Agile cards now show tomorrow's prices.** EMT looked for prices up to midnight, but Agile's day ends at 23:00,
  so it kept asking Octopus through the night and had used all its tries for the day by 04:30. When tomorrow's
  prices came out at about 4pm, it had none left, and the card stopped at 23:00 tonight. It now asks once they
  are out, and tries made earlier in the day can no longer use up the ones that fetch needs.
- **Agile import and export have a card each.** Each has its own scale, the price now, its best hour, and what
  your last 30 days would have come to.
- **The Agile cards can be expanded and hidden.** Hiding one applies to everyone using this EMT, and it stops
  asking Octopus for those prices. A link at the foot of the Overview brings it back.
- **Billing History says when it is taking a backup.** Saving, adding or deleting a period, and applying a site
  change, all take a backup first. Each now says so while it works. Apply had shown nothing and could be pressed
  twice, and Escape could close the dialog during a save.
- **The rest of EMT says so too.** Finishing the setup wizard, saving a postcode, rebuilding a new device's
  history, deleting a device, applying a re-fetch and saving sites before an import all now say they are backing up.
  A postcode save and the rebuild could also be pressed twice, which took a second backup.
- **Filling a gap no longer says it is backing up.** It said so, but a gap fill takes no backup.

## [2026.11.0b15] — 2026-11-06

- **Save Config shows when there is something to save.** It is dimmed until you change a setting, then lights up.
- **Rate brackets' legend takes one row on Agile.** More than a dozen brackets now show as a single colour scale
  instead of a key each. Hover a colour to name its bracket and pick it out in the bars. Tariffs with fewer
  brackets keep their named keys.
- **Save Config says what it is doing.** A save takes a backup first, which took a moment with nothing on screen.
  EMT now says so while it works, and the button can't be pressed a second time, which took a second backup.
- **Removing a device's read sensor asks if it was wrong.** If it was, the history it left is wrong too. After the
  save, EMT says so and offers Clear history there and then.
- **History imports ask Octopus for 30 days at a time, not 60.** The first 60-day ask often timed out on
  slower accounts, which cost one tester 8 minutes of a 20-minute import before EMT asked for less.
  Gap fills ask the same way.
- **The half-hours with no usage figure can be filled, this time from a click.** They showed as missing, but
  clicking one showed its details and only Re-fetch, which cannot add the figure. It now offers Fill this gap.
- **A device can go without a read sensor.** A battery whose inverter reports no grid charge can now be
  set up with the read sensor empty, or have a wrong one removed. It keeps its Overview card and its charge
  counts in the house. EMT no longer warns about a sensor that has gone, and **Clear history** on the
  device's card removes what the old sensor recorded, after a backup, and gives that usage back to the house.

## [2026.11.0b14] — 2026-11-06

- **The last half-hours with no usage figure can be filled too.** A half-hour that had Octopus's settled
  figure but no usage of its own showed as missing on the heatmap, while the gap fill said there was
  nothing to fill, and its kWh was left out of your totals. Both now agree it is missing, and filling
  it puts the kWh back.
- **Billing charts mark a rate with a line.** In the legend and the hover, a rate was a box, and a bar
  was a box with a white outline, so they were hard to tell apart. A rate is now a line, dashed where
  its line is, and a bar a plain block.
- **Billing charts keep your place when new data arrives.** The page redraws itself when data changes,
  often as you come back to it. It used to jump back to where you were when the redraw began, and close
  any data tables you had open, which moved you further still. It now stays where you are, tables open.
- **Billing charts stop pushing you back as they load, in Safari above all.** A day grows when its figures
  arrive. If that day was above where you were reading, everything moved down the screen, so scrolling
  straight after arriving could throw you back. What you are reading now stays still.
- **Billing and Usage Stats remember your place.** EMT rebuilds these pages each time you visit them, so
  you always started at the top, with your data tables shut. Come back now and you are where you left
  off, tables open, unless you have already started scrolling. This lasts as long as the browser tab.

## [2026.11.0b13] — 2026-11-05

- **A battery is no longer flagged for counting solar because of missing usage.** The battery check
  compares what the battery charged with what the house took from the grid. Where EMT had no usage
  figure for a half-hour, it read that as nothing taken from the grid, so a battery charging from the
  grid overnight looked like it had charged from the panels, and its sensor was flagged. Half-hours
  with no usage figure are now left out of the check, and a flag they caused clears within the hour.
- **No more repeated errors for half-hours with no usage figure.** EMT re-checks a half-hour once Octopus
  settles it, and for one holding only its export that re-check failed, logged an error, and was tried
  again every time, without changing anything. It now runs.

## [2026.11.0b12] — 2026-11-05

- **A history import no longer skips its newest months when Octopus fails once.** If Octopus didn't
  answer for the newest two months of your usage but did for your export, the import moved on and
  never asked for those months again: they came in with export and no usage, and your devices' share
  of them showed as nothing. Each side of the meter now keeps its own place and asks again for what it
  missed, and the log says when Octopus didn't answer.
- **Half-hours with no usage figure show as missing, so you can fill them.** Where history already came
  in that way, it had a row for each half-hour, so Data Management showed no gap and nothing would ask
  Octopus again. A half-hour with no usage figure is now missing on the heatmap, in the gaps list and
  in Data health, and filling it fetches the usage from Octopus.

## [2026.11.0b11] — 2026-11-05

- **A history import keeps going when Octopus is slow.** On some accounts Octopus takes too long to
  answer for two months of half-hours at once, and the import asked for the same two months again and
  again, sometimes for hours without getting further. Now, each time a stretch fails, it asks for half
  as many days (down to a week), and keeps every stretch that arrives.
- **EMT says when a device's sensor is reading the whole house.** A device's sensor has to measure that
  device. Some inverters and clamps offer a reading of the house's whole grid import, which looks right
  but makes the device take the house's usage — and the battery check, which looks for a battery
  reading *more* than the grid, could not see it. EMT now compares each device with the meter: where it
  matches the meter's import in most half-hours over the last 30 days, it says so at the top of every
  page and on the device's card, and how to fix it. It changes nothing in your data, and *Keep this
  sensor anyway* clears the warning.

## [2026.11.0b10] — 2026-11-05

- **The export carbon setting works.** Settings has always let you count the carbon your export saves
  at a fixed intensity of your own instead of the grid's average, but nothing used it: every figure
  stayed at the grid's average. Now, if you choose *Custom fixed intensity*, your export's carbon in
  Usage Stats, Running total, Flow and Insights is counted at your figure, and the net carbon follows.
  With the default, *Grid average*, nothing changes.
- **Flow's CO₂ view shows what your export saved.** The export now flows out of the house, in green,
  to *Displaced by export*: the grid carbon your export saved. Before, it was left off the chart and
  only the figures above it gave it.
- **Span's list is simpler.** *Span* (in Devices, Rate brackets and Flow) now lists your tariffs first,
  then your eras grouped by kind, in the same order as Running total's year-start list. The chapters
  are gone: each was named after what started it, not what your home had then, so they were hard to
  read. Running total's year-start list now sits where Span's does, in the row under the buttons.
- **Flow's £ view shows each device's net cost.** On Agile, when a price goes below zero, each device
  that used power then is paid back, drawn as a flow out of it to *Plunge credit*. The device's block is
  its cost before that credit, so the net was nowhere on the chart. Now each device is named with its
  net cost, and hovering its *Plunge credit* flow does the sum: what it cost, less what it was paid back.
- **Help on a device's sensor.** A *?* on a device's *Import Channel* and *History from Home Assistant*
  headings opens new help: what EMT needs from a sensor, and how to fix one, from a sensor Home
  Assistant renamed to one that was wrong from the start. For a wrong one, it goes step by step: put in
  the right sensor, answer *It was the wrong sensor*, and replace the device's history from it.
- **Colours are set in one place.** The *Chart style* sections on Settings' device, Main meter and EV
  tabs are gone: every meter's colour and rate line is set on the Charts page, from its ⚙︎ menu, *Bar
  colours*, where they already were. Colours you chose are kept.

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
