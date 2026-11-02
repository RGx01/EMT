# Changelog

All notable changes to EMT are recorded here.

Versions are CalVer — `YYYY.M.PATCH` — matching Home Assistant's own convention. `v5` is the
internal name for everything before the first public release and never appears in a version string.

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
