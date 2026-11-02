# Contributing

EMT is developed by one person, and is in beta. Thank you for trying it. These conventions keep it
workable — and most of them pay for themselves solo.

## Reporting a problem

[Open an issue](https://github.com/RGx01/EMT/issues/new/choose) and pick the form:

- **Something's wrong** — a figure, a page, an error.
- **Bringing v4's history across** — a restore of a v4 backup that was refused, failed or looks wrong.
- **Beta feedback** — anything else: what worked, what was confusing, what's missing, an idea.

The most useful attachment is a **support file**: Data Management → **Backup & restore** → **Make a
support file…**, for the days in question. Keep **Hide my meter and account details** ticked (it is by
default): MPANs, meter serials, the account number and the postcode become placeholders, and the
Octopus API key is never in it. Please don't paste MPANs, an account number or an address into an
issue — an issue is public.

A **security problem** is reported privately, not as an issue: see [SECURITY.md](../SECURITY.md).

## Pull requests

Every change arrives by PR, including the owner's. From a fork: branch from `dev`, PR to `dev` —
`main` takes only releases, from `dev`. The branch model, running the tests and looking at a change
locally are in [DEVELOPMENT.md](DEVELOPMENT.md). For anything larger than a fix, open an issue first:
the design may already be decided, or decided against, in [docs/history/](history/).

A PR should be **one idea**. A PR that fixes a bug and tidies three unrelated things is a PR whose
diff nobody can read, and whose revert is a gamble.

## What a change must carry

- **Evidence.** A claim about behaviour cites the code, a log line, or a query against a real
  database. "This should fix it" is not a rationale.
- **A test that would have failed before.** For a bug fix, the test encodes the bug, not just the
  fix. Name what regression it guards in the docstring.
- **Raw precision in any verification.** Never conclude from a rounded or formatted value. Several
  wrong diagnoses in the v4 codebase came from 4dp log lines and 6dp histograms agreeing with a
  hypothesis that the stored values did not support.
- **One implementation for a figure shown in two places.** If a second surface shows a figure that
  one already shows (a bill line, a device's share, a total), it calls the code that computes it.
  It does not work the figure out again from the same columns. Where one side is SQL, the rule
  carries a SQL form next to its Python, and a test holds the two equal (see
  `direct_import.py`). Then add the new surface to the test that asks every surface for the same
  periods (`tests/test_direct_import.py` is the model). v4 grew this bug class one surface at a
  time: on 6 Oct 2026 three surfaces gave three answers for one Direct import, and a fourth
  copy had no caller.
- **The rationale, where it will be found later.** Non-obvious decisions belong in a comment at the
  code, not only in the PR description — PRs are hard to find from a line of code two years on.

## Licence of a contribution

EMT is under the [PolyForm Noncommercial License 1.0.0](../LICENSE): free for any noncommercial use,
and commercial use only with the licensor's permission. By opening a PR you agree that your
contribution is licensed under those terms, and that RGx01 may also license it to others on other
terms (a commercial licence, or a later licence for the whole project) — so one contributor's code
never blocks a decision about all of it.

## Code style

- Python: the existing style — 4-space indent, type hints where the surrounding code has them,
  `logger.info` / `warning` / `error`, never `print`.
- The pages: Jinja templates, plain JavaScript with no build step, working in Safari; styles through
  the existing CSS variables, so both themes work.
- Comments carry the *why*, at the code — see above.

## What won't be accepted

- A change that moves already-settled figures (kWh, cost, carbon) without saying why, and showing it
  on a real database. Accuracy is the project's first commitment.
- A new dependency without a strong reason. The add-on runs on a Raspberry Pi.
- A change that writes to Home Assistant. EMT reads sensors and creates no entities.
- New Octopus API calls that are not capped, paced, and remembered across a restart: every install
  shares its account's limit.
- Removing the bills' *Informational — not authoritative* mark, or the README's disclaimer.

## Another supplier

EMT knows one API supplier, Octopus (`suppliers.py`'s `API_CAPABLE`), and *My supplier isn't listed*
for local metering only. Other suppliers on Octopus's Kraken platform share its GraphQL shapes but
sign in differently (email and password rather than an API key). Open an issue before starting — the
seam (a supplier profile) is worth agreeing first.

## What not to do

- Do not commit code that nothing calls. Unreferenced code reads as load-bearing to whoever comes
  next, and sends them down it. The v4 codebase carried a 169-line pricing module with zero callers
  for over a year and it actively misdirected debugging.
- **Never commit real account data.** No MPANs, account numbers, serial numbers, addresses, or
  `.db` files — `.gitignore` blocks databases, but fixtures and pasted log lines are the real risk.
  This matters more than it looks: this repository is public and so is its **entire history**, so
  a value committed once and removed later is exposed for good (the history was rewritten once, on
  2 Nov 2026, to remove some). Use the placeholders the fixtures use — MPANs `2600000000001` /
  `2600000000002`, postcode `SW1A`, account `A-0000ABC7` — or mask (`mpan=2…1`), and check before
  committing, not after.
- Do not add a parameter that defaults to "behave as if you had no context". If a function needs the
  tariff era to be correct, it takes the tariff era as a required argument. A silent default is how
  one caller in v4 rendered charts differently from another for two days without anyone noticing.
