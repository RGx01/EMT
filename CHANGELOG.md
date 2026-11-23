# Changelog

All notable changes to EMT are recorded here.

Versions are CalVer — `YYYY.M.PATCH` — matching Home Assistant's own convention. `v5` is the
internal name for everything before the first public release and never appears in a version string.

## [Unreleased]

- **A slow moment no longer restarts EMT's work.** On an SD card, saving can hold EMT for 20 seconds or
  more; Home Assistant may drop the connection meanwhile, and on reconnecting EMT ran its whole startup
  again, which held it as long a second time. It now reconnects and carries on, and runs its startup
  again only when Home Assistant itself was down.

## [2026.11.0b33] — 2026-11-23

**The last beta here.** EMT's first release, **2026.12.0**, comes to this repository on 1 December 2026,
and every install from it updates to that. Betas after it come from a separate repository,
[EMT_beta](https://github.com/RGx01/EMT_beta): a separate add-on, "EMT (beta)", installed beside or
instead of EMT; your data moves across with a backup and a restore.

- **The Agile cards show everything published.** They ran 24 hours either side of now, so once tomorrow's
  prices were out (about 4pm) the last few hours to 23:00 tomorrow — the evening peak — were cut off. They now
  run from 24 hours back to the last published half-hour.
- **The Overview's text is 12px at least.** The power cards' charts, their axes and legends, the gauges'
  units and every card title were 9–11px; none is under 12px now, and the expanded cards follow. The Meter's
  reading is the car's size and stays on one line, and the gauges' tiny end labels (Export/Import, Out/In, 0)
  are gone — the reading, its sign and the Meter's pill already say which way the power is going.

## [2026.11.0b32] — 2026-11-22

- **A hidden Agile card comes back up to date.** Hiding an Agile card stopped EMT fetching its prices, so
  shown again it was a day behind, and the link at the foot of the page could fail to bring it back at all
  until a hard refresh. EMT now keeps both directions' prices whatever is hidden — hiding only hides — and
  the page never reuses an old answer from the browser's cache. In Safari a card shown again, Agile or any
  other alone in its row, stayed invisible until the page was reloaded; it appears at once now.
- **Usage Stats: zoom.** A bar in the data table's heading is a window on the chart: drag either end to zoom
  from that side, the middle to move along, ↺ to see everything again. The table follows it.
- **Usage Stats: Rate brackets' colours in month order.** Ordered by Month, the brackets' legend lost its colours
  and pointing at a bracket did not pick it out. Both work again.
- **Usage Stats: Month order separates the months.** On Monthly, ordered by Month, a line now separates each
  month's group of bars, and the Net line runs within a month instead of joining it to the next.
- **Usage Stats: Span by the day, the month or the year, and by Month.** A span's bars can be a day, a month or
  a year each, and grouped by month across the years as Monthly's can.
- **The car's 6 hours on its Overview card.** On Intelligent Octopus Go's capped agreement (IOG-SMB), the EV
  card shows today's noon-to-noon allowance beside its gauge: about how much smart charging has been used, how
  much Octopus's current plan would add, and how much is left — or how far the plan runs over — with the next
  planned window. These are EMT's estimates, counted as the Smart Charging card counts them.
- **A heat pump's outdoor temperature.** Give your heat pump an outdoor temperature sensor in Meter Config and
  its Overview card shows a temperature gauge beside its power, and its chart draws the temperature since
  midnight, today over yesterday, on a scale of its own. A °F sensor is shown in °C.
- **Today against yesterday on the Overview's power cards.** The 🏠 Meter card (was Live Power) shows your
  import and export since midnight, today's line over yesterday's, export below the line. Each device card
  shows the energy its power sensor measured since midnight; a battery or a V2X car shows what went out, below
  the line, as well as in. A battery with a charge sensor switches between its charge and its energy. Point at
  a chart (or tap it) for the figures at that time; the expand button opens a card large, with its times and
  scale. The compact button on any power card shows them all as gauges only. The gauges are smaller and line
  up across the row, the Meter's import/export pill sits beside its gauge, and a battery's level stands upright
  beside its inverter and solar gauges, which are now the same size.
- **Arrange the Overview.** The Overview's cards now sit in rows: Power (the Meter and your devices), Grid
  and carbon (the generation mix, carbon intensity and the 48-hour history together), Smart Charging, Agile
  import and Agile export prices, and the cost cards. Unlock the padlock at the top of the page and drag the
  rows, and the cards along each row, into place; while it is unlocked a gentle wave runs down the rows now and
  then (not if your device asks for less motion). When the Power row's cards do not fit on one line, they
  split evenly, three and two rather than four and one. Carbon Intensity no longer grows and shrinks beside
  the gauges, a hidden card's neighbours fill its place, and the 48-hour history fills the height of its row.
  The compact button on any cost card shows them all as a total each, on one line. The Agile chart is drawn
  at its card's size from the start (on a Pi it could load stretched until expanded).
- **The Overview's bill card shows the Billing tab's total.** The two worked out the same money with
  different rounding: the Overview rounded each day to the penny and added up the days, the Billing tab
  rounds once, as your Octopus statement does. They could sit a few pence apart (£19.86 against £19.88).
  The Overview's *This Bill* now shows the statement figure. Today and This Year are unchanged.
- **Backups wait for a device's history rebuild.** *Backup now* is refused while *Rebuild history* or
  *Replace history* is running, saying which device, as a backup then would hold its history half done.
  The nightly copy waits for it too, unless the last copy is two days old.
- **A device's history rebuild that was cut off says so.** If EMT restarts, the power goes, or a backup
  taken mid-rebuild is restored while *Rebuild history* or *Replace history* is running, the device's card
  now says the rebuild was cut off and that *Rebuild history* finishes it. Before, a part-done history
  looked finished. The run is in the undo list from its start, so it covers what was written.
- **Restoring a backup stops a device's history rebuild first.** A restore while *Rebuild history* or
  *Replace history* was running let the rebuild carry on: it went on writing the old database during the
  restore's checks, and could, with the timing against it, have written into the restored one. The restore
  now stops it first (and says so if it will not stop), and afterwards the device's card shows the
  restored history, not the stopped run's error. *Rebuild history* finishes a rebuild that was cut off.
- **Your car is its own line beside a battery too.** On Intelligent Octopus Go with no EV sensor in EMT,
  the car shows as an "EV" line, from Octopus's split and its dispatches. With another device set up — a
  battery, a heat pump — the Overview and Billing's day charts showed it, but the Bill card's breakdown,
  Usage Stats and Flow left it inside Direct import. Every view now shows it the same way. Bill totals do
  not change.
- **Insights counts a charge Octopus billed to your car.** On IOG-SMB, where Octopus's bill splits house
  and car, a charge it put on the car that EMT had no dispatch for stayed in the house on Insights'
  carbon figures. It is the car's now, as on the bill.
- **EMT talks to Octopus at its new address.** Octopus has moved its API to `api.oegb-kraken.energy`; EMT
  now asks there for your history, rates and bills. The old address still answers for now, and nothing
  else changes: the same API key, the same data.
- **A change to Octopus's rates feed is said, and your rates are kept.** If Octopus's rates feed answers
  with no rates for a tariff that had them, or with rates EMT cannot read, EMT now says so in the log
  (`kraken_rest_drift`), and keeps the rates it had rather than dropping them. Before, the import's rates
  were dropped and the "tariff not supported" banner showed, and an export tariff's went quietly.
- **A heat-pump tariff keeps the car's split.** Octopus has added a heat-pump band to its tariffs.
  If a heat pump's use arrives in Octopus's house/car breakdown under a name EMT did not know, EMT set
  aside that half-hour's whole breakdown, car included. It now counts the heat pump as part of the house,
  says so in the log the first time, and keeps the house's own rate apart from the heat pump's.

## [2026.11.0b31] — 2026-11-21

- **Billing shows a new half-hour without a reload.** Opened or reloaded just before a half-hour arrived,
  Billing (and Heatmaps) could miss it until something else changed, often the next half-hour, and the refresh just after
  each half-hour only redrew what was already on the page. Billing now takes each new half-hour within
  about a minute, however recently the page was opened.
- **The Overview's cost cards keep up.** Today, This Bill and This Year updated once, a minute after each
  half-hour, so a half-hour EMT finished later than that, or a bill arriving in between, waited half an
  hour to show. They now update within about a minute of anything changing.
- **The settlement pill is on the Overview too.** The pill at the top of Charts that counts half-hours still
  waiting for Octopus (their kWh, their bill, their split) now shows at the top of the Overview as well,
  and works the same: a click opens Data Management on them, a right-click or long press retries
  settlement.

## [2026.11.0b30] — 2026-11-21

- **EMT pauses less on an SD card.** Every ten seconds EMT checked for devices' half-hours that were waiting
  on a later reading, and did it by reading your whole history, so a busy SD card could hold EMT for
  seconds, once for over three minutes. It now looks only at those half-hours, of which there are usually
  none.
- **Charts keeps what you chose when you come back to it.** Returning to Charts from another page, three
  choices could quietly reset: Running total's year start (a tariff or an era) went back to 1 January, a
  year ticked before the first calendar year of your data came back unticked, and Rate brackets' device or
  Export went back to Import. They now stay where you left them.
- **EMT no longer reads a field Octopus is retiring.** From May 2027 Octopus is removing the field
  that said whether a smart charger or car was set up, and EMT has warned about it at every start
  since 17 November. It now reads the device's state instead, and still picks the same charger
  for your planned charges, plugged in or not, when the account lists more than one device.
- **The carbon backfill stops warning every day about half-hours that have no carbon figure.** The Carbon
  Intensity API has nothing for some old stretches, so EMT asked for them again each day, filled nothing and
  logged a warning, for ever. A half-hour that comes back empty on three days is now set aside: EMT stops
  asking for it, says so once in the log, naming the stretches, and charts show it as missing, as before.
  Newer half-hours without carbon are still fetched, and changing your region asks again.

## [2026.11.0b29] — 2026-11-20

- **Signing in to one EMT no longer signs you out of another on the same machine.** A browser keeps one
  set of cookies for a machine, whatever the port, so two EMTs on one box (a test copy beside the real one)
  shared their sign-in cookie, and each sign-in replaced the other's. Each EMT now keeps a cookie of its
  own. After updating, sign in on EMT's own port once more.

## [2026.11.0b28] — 2026-11-20

- **Signing in on EMT's own port works when Home Assistant's local address is set wrong.** EMT sent your
  browser to the local address set in Home Assistant (Settings → System → Network). Where that said
  `https://` but Home Assistant answers plain HTTP, the browser could not connect. The sign-in page now
  tries that address, then this machine's port 8123 over HTTP and HTTPS, and uses the first that answers.

## [2026.11.0b27] — 2026-11-20

- **A device renamed on Settings is renamed on the charts.** Usage Stats, Flow, Running total and Spiral
  kept the old name until some day's figures changed, which on an Octopus-only setup could be a day. They
  now redraw with the new name as soon as you save.
- **The Data Management heatmap's key is easier to read.** It now starts at the left, and hovering an item
  shows that colour as a cell of the grid, at the grid's own size, so a hatch looks as it does on the
  heatmap.
- **The direct port no longer takes Home Assistant's sidebar headers from just anyone.** EMT believed a
  request came through Home Assistant's sidebar if it said so. Now it believes that only from Home
  Assistant's own Supervisor; a request to port 8098 is always a direct one. This is the first step
  towards a login on that port.
- **Port 8098 asks you to sign in with Home Assistant.** Opening EMT directly on its port, rather than from
  Home Assistant's sidebar, now takes you to Home Assistant's own sign-in, two-factor included, and only a
  Home Assistant administrator gets in. You stay signed in on that browser for 30 days, through restarts
  and updates, and **Sign out** is at the foot of the sidebar. Every change made on that port must come from
  EMT's own pages. The sidebar is unchanged. On standalone Docker, set `HA_PUBLIC_URL` if Home Assistant
  runs on another machine or port from EMT's; `EMT_DIRECT_LOGIN=off` turns the sign-in off.

## [2026.11.0b26] — 2026-11-19

- **Yesterday's export no longer reads as a gap after an import.** Importing your history before Octopus
  had settled yesterday's export brought that day in with no export figure, and Data Management showed it
  hatched red, night included, as if Octopus would never send it. It now waits, amber, like any half-hour
  Octopus hasn't settled yet, and only reads as a gap once it's older than Octopus takes.

## [2026.11.0b25] — 2026-11-19

- **The Agile cards show your power under the prices.** On the Overview, with a power sensor, the Agile
  import card draws what you imported over the last 24 hours, and the export card what you exported, as a
  line on its own kW scale on the right. Hovering a half-hour gives its price and its average power.
- **Flow shows the house at its own size, and the export growing out of it.** The house's box was
  drawn as tall as the export when the export was larger, so a house that imported little looked as
  big as what it exported. The house is now as tall as its own import, and the export flows out of it,
  widening or narrowing to the export's size. In Cost the export's price bands come back together in
  one Export bar; in kWh and CO₂ the export goes straight to it. The bar and the Grid are each named
  with their total.
- **Flow shows the standing charge by its daily rate.** With Inc. standing charge, a span where the
  standing charge changed showed it as one amount; it now goes through each rate it was charged at,
  as the import goes through its price, and hovering a rate says how many days it was charged.
- **Running total's Lifetime line marks your tariffs' price changes.** With Tariffs ticked, a dot sits on
  the line where each tariff started and where its unit rate or standing charge changed, import and
  export, as on Year by year, and the tariff band above has a tick at each change. Hovering the band
  shows the prices from the day under the pointer. Agile and Tracker, whose prices move every half-hour
  or day, mark only where they started, on Year by year too.
- **Your eras and chart colours can go to a file and come back.** Data Management → Backup & restore can
  download your home's eras and every chart colour and rate line as a small `.json` file, from the
  database or from any kept backup, and import one on any EMT, on this machine or another. An import
  shows what it will change first; its eras replace yours, and each colour goes to the meter of the
  same kind (the house, export, an EV, a battery; the first of a kind to the first), so a theme can be
  shared between homes whose devices have different names.

## [2026.11.0b24] — 2026-11-18

- **More of the half-hours left waiting for a bill get one.** On some days Octopus answers a request
  that starts at midnight with no costs, for the day and for the month alike, though it has them.
  When the day and the month leave a half-hour without a cost, EMT now also asks from noon the day
  before to noon (or noon to noon the next day), and Octopus answers that. A night Octopus cannot
  cost in any request stays waiting; importing that month's statement settles it.

## [2026.11.0b23] — 2026-11-16

- **Octopus's costs come back for half-hours EMT kept asking about.** Asking again, and the nightly
  check for bills, asked Octopus a window running from half an hour before the day to three hours
  after it, and Octopus answers a window like that with no costs. Both now ask exactly the day, then
  exactly the month, as the import always has, so the half-hours left waiting for a bill get one.
- **EMT no longer reads a field Octopus is retiring.** Octopus is removing a smart device's `provider`
  from April 2027. EMT used it only when a device had no make, so it now goes by the make alone; the
  charger whose planned charges EMT reads is picked as before.

## [2026.11.0b22] — 2026-11-16

- **Usage Stats lists your export tariffs as well as your import ones.** Span, the Running total's year
  start, and the Running total's tariff band and dots showed only the import tariffs; each export tariff
  now has its own entries, under its own heading in the pickers and its own band on the Lifetime line.
- **Rate brackets and Running total pick what they show in the same place.** The choice of import,
  export or a device now sits first on the row below the toolbar, just before the Span or year-start
  picker, in both views; Rate brackets' "Whole meter" is now "Import", as Running total already said.
- **A Span stays a Span after a look at Running total.** Going from Devices or Rate brackets on a Span to
  Running total and back showed Yearly instead; it now comes back to the Span you left.

## [2026.11.0b21] — 2026-11-15

- **A v4 backup is no longer refused over a few half-hours whose car/house split doesn't add up.** v4 kept
  the split twice, and the two could disagree. Up to ten such half-hours now come across with the split v5
  uses, and each is named in the log and in a support file, so it can be looked into.
- **A device is the kind you set it to, whatever it's called.** Charts, Usage Stats and Settings told
  the EV apart partly by its name, so a battery or heat pump whose name happened to contain "ev"
  (`battery_level`) could be taken for the car, and a second car's price line on Billing followed the
  house. Each device is now what its type says; the name is only read for a device with no type.

## [2026.11.0b20] — 2026-11-14

- **The EV's price line on Billing no longer follows the house when the car isn't charging.** On an install
  set up in v5, the day chart drew the car's line on the house's price all day on Intelligent Octopus Go,
  instead of resting at the off-peak rate between charges as it does on a v4 import. Only the line was wrong;
  the bars, costs and bills were right.

## [2026.11.0b19] — 2026-11-14

- **A v4 backup with Intelligent Octopus Go history is no longer refused over a sliver of house.** When EMT
  moved a half-hour's car/house split to Octopus's bill and the bill left almost nothing for the house, the
  house share's price strayed by a fraction of a penny and the import refused the whole backup. Each such
  half-hour now keeps one price for both shares, and costs what it did.
- **A refused v4 import says which half-hours.** The log names the first few with their figures, so a report
  is enough to look into without the backup.

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
