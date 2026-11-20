# Security

## Reporting a vulnerability

Please **don't open a public issue** for a security problem. Report it privately:
**[Security → Report a vulnerability](https://github.com/RGx01/EMT/security/advisories/new)** on this
repository. EMT is maintained by one person, so a reply may take a few days; a fix for something
serious goes out as a release of its own.

Only the latest release, and the latest beta while there is one, gets fixes.

## What EMT holds, and where

- **Your Octopus API key** is in its own file, `/data/energy_meter_tracker/kraken_credentials.json`,
  inside the add-on's private storage. It is never written to the database, a backup or a support
  file, and EMT sends it only to Octopus.
- **Your history** is one database, `blocks.db`, in the same private storage. It holds your MPANs,
  meter serials and Octopus account number as well as your readings.
- **Backups** are copies of that database, in `/share/energy_meter_tracker_backup_<site>/` — which
  other add-ons can read. Treat a backup like the database itself.
- **A support file** carries the database for the span you choose, with *Hide my meter and account
  details* (on by default) turning MPANs, meter serials, the account number, the postcode and the site
  name into placeholders. It is downloaded by you; EMT sends it nowhere. Attached to an issue, anyone can
  download it, and it still holds the span's usage and costs, device names and the log.

## Who can open EMT

EMT is reached two ways:

- **Through Home Assistant's sidebar** (ingress): behind Home Assistant's own login. EMT believes a
  request came this way only when it comes from Home Assistant's Supervisor.
- **Directly**, at `http://<your Home Assistant host>:8098`. This port asks you to **sign in with Home
  Assistant**, on Home Assistant's own page, two-factor included, and lets in **administrators only**.
  EMT then keeps its own sign-in for 30 days on that browser and throws Home Assistant's tokens away: it
  needs to know who you are, not to act as you. A change made on this port must come from EMT's own
  pages.

The sign-in's signing secret is a file beside EMT's database, not in it: a backup or a support file
never carries it.

If you don't use the direct port, turn it off: the add-on's page → **Network** →
clear the port → **Save**, then restart the add-on. Never expose the direct port to the internet: it
is plain `http`.

**Standalone Docker** has no sidebar: the direct port is the whole of EMT and signs in the same way.
`EMT_DIRECT_LOGIN=off` turns the sign-in off, for a setup whose browser cannot reach Home Assistant's
sign-in; then anyone who can reach the port can use every tool, as before this sign-in existed.

## What EMT talks to

- **Home Assistant** — reading your sensors and their history; EMT creates no entities.
- **Octopus Energy's API** — your consumption, costs, tariffs and dispatches, with your key, and
  Octopus's public product prices without it.
- **The Carbon Intensity API** (carbonintensity.org.uk) — with your postcode's first part, if set.
- **GitHub** — once a day, to see whether a newer release is out.
