# Contributing

EMT is currently developed by one person. These conventions exist so that stays workable when it
isn't — and because most of them pay for themselves solo.

> **Scope note.** This file covers *what a change must carry*. The outward-facing half — reporting
> bugs, suggesting features, adding support for another supplier, what won't be accepted — is ported
> from the v4 add-on's `CONTRIBUTING.md` when this repository goes public and there is an audience
> for it. See [DEVELOPMENT.md](DEVELOPMENT.md#relationship-to-the-v4-add-ons-docs).

## Pull requests

Every change arrives by PR, including the owner's. Branch naming and the branch model follow the v4
add-on unchanged — see [DEVELOPMENT.md](DEVELOPMENT.md).

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

## What not to do

- Do not commit code that nothing calls. Unreferenced code reads as load-bearing to whoever comes
  next, and sends them down it. The v4 codebase carried a 169-line pricing module with zero callers
  for over a year and it actively misdirected debugging.
- **Never commit real account data.** No MPANs, account numbers, serial numbers, addresses, or
  `.db` files — `.gitignore` blocks databases, but fixtures and pasted log lines are the real risk.
  This matters more than it looks: once the repository is public its **entire history** is public,
  so a value committed once and removed later is exposed permanently. Mask in the v4 style
  (`mpan=2…1`) and verify the masking before committing, not after.
- Do not add a parameter that defaults to "behave as if you had no context". If a function needs the
  tariff era to be correct, it takes the tariff era as a required argument. A silent default is how
  one caller in v4 rendered charts differently from another for two days without anyone noticing.
