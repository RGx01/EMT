# Changelog

All notable changes to EMT are recorded here.

Versions are CalVer — `YYYY.M.PATCH` — matching Home Assistant's own convention. `v5` is the
internal name for everything before the first public release and never appears in a version string.

## [Unreleased]

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
