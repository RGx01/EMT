# Development

## Branches

| Branch | Holds | Merged via |
|---|---|---|
| `main` | Released. Every commit is a version that has run on real hardware. | PR from `dev` |
| `dev` | Integration. Complete work, testable on other machines, not yet released. | PR from a work branch |
| `fix/…` `feat/…` `chore/…` | One change in progress. Short-lived; deleted after merge. | PR to `dev` |

Branch naming follows the v4 add-on unchanged — `fix/rate-line-zero-on-current-day`,
`feat/gas-meter`, `chore/update-dependencies`. There is no reason for the two repositories to differ
here, and muscle memory is worth more than novelty.

Nothing is committed straight to `main` or `dev`, including by the repository owner. The rule holds
when there is one developer for the same reason it holds when there are five: the PR is where the
diff gets read as a whole, and a change that was obvious while writing it stops being obvious a
month later.

```bash
git switch dev
git switch -c feat/storage-readonly-connections
# ... commits ...
git push -u origin feat/storage-readonly-connections
# open PR → dev
```

## Testing a branch on another machine

Home Assistant's add-on store reads a repository's **default branch**; there is no branch selector.
So `dev` is not installable from the store, and that is fine — the documented way to test an
unreleased branch is a **local add-on**.

On each test machine, clone into the add-ons directory and check out the branch:

```bash
cd /addons                      # or /addons/local, depending on your install
git clone https://github.com/RGx01/EMT.git emt
cd emt
git switch dev
```

Home Assistant then offers it as a *Local add-on*, separate from anything installed from a store
repository. To pick up new commits: `git pull`, then rebuild the add-on from its page.

If a released version is already installed from a repository, stop it first: both would be the
same add-on (`emt`), sharing one name in the sidebar.

**The slug is the add-on's identity.** Home Assistant names a local add-on `local_<slug>` and keeps its
data under that name, so a pull that changes `config.yaml`'s `slug:` makes it a **different add-on**,
and the old one's data does not follow. The slug became `emt` on 2 Nov 2026 (it was `emt_soak`): a
clone made before that, such as the Pi soak's `local_emt_soak`, keeps its data only if its local
`config.yaml` keeps `slug: "emt_soak"` — edit it after the pull, before rebuilding.

## Screenshots

`docs/screenshots/` is taken from a made-up household, never a real install: `python
.build/demo_db.py <dir>` writes it (seeded — the same database every time), and a local server on that
directory serves it, with `EMT_INSTANCE_NAME=Demo` so the footer names no machine. Take each page at
about 1440 × 900, dark theme, the page only.

## Relationship to the v4 add-on's docs

The v4 repository has its own `CONTRIBUTING.md` and `DEVELOPMENT.md`. What carries across, and what
deliberately does not:

| v4 content | Here |
|---|---|
| Branch naming, fork-and-PR workflow, `dev`-not-`main` rule | **Reused as-is** — see above |
| Bug reports, feature suggestions, adding a supplier, what won't be accepted | **Port when the repo goes public.** Written for external contributors; no audience yet. |
| Running tests, local development modes, swapping databases between environments | **Port when there is code to run** — the process is unchanged, the commands are not yet true here |
| Architecture: schema, block lifecycle, billing periods, file structure (~250 of its 375 lines) | **Do not port.** It documents v4's implementation, and the storage model, job boundaries and surfaces are precisely what EMT is changing. |

That last row is the important one. Copying v4's architecture sections now would produce a document
that is wrong on the day it lands and drifts further with every workstream — the same failure as the
v4 roadmap's BL-63 entry, which described a shelved code layer that had since been half-wired, and
so misled anyone who acted on it.

**So: written as each workstream lands, not up front.** Workstream A writes the storage section
because A decides what it says. Until then this file covers process only, which is the part that is
actually true today.

## Releasing

1. PR `dev` → `main`. The PR body is the release note.
2. Merge, then tag on `main`: `git tag 2026.10.0 && git push --tags`.
3. CalVer — `YYYY.M.PATCH`. The month is the release month, not the sprint.

## A pypdf bump

`bill_parser` reads a PDF bill's text through pypdf and parses it with regular expressions, so a
pypdf release can change a parsed figure without an error, and the parser's tests (placeholder
text) cannot see it. Dependabot raises pypdf on its own PR; merge it only once the bill gate passes,
run against bills kept **outside the repo** — no bill, redacted or not, and no snapshot is committed:

```bash
python3 .build/pypdf_gate.py snapshot ~/bills                         # with the pinned pypdf
python3 .build/pypdf_gate.py compare ~/bills --against <pinned>       # in a venv outside the repo, candidate installed
```

`compare` exits 1 if any bill parses differently, naming each by a hash and each difference by its
field — never a value. The snapshots live in `<bills>/.pypdf-snapshots/`.

## Before opening a PR

- The full test suite passes on the **Pi**, not only on a development machine. ARM and SD-card I/O
  are the target's real constraints; a timing-sensitive test that passes on a laptop proves little.
- **Run it through `run_tests.sh`.** The suite is file-isolated — several modules install
  `sys.modules` stubs at import time, so a plain `pytest tests/` collides at collection and a
  "passing" run that skipped the runner has proved nothing.
- **A PR that removes code satisfies the deletion protocol**
  ([architecture §4a](design/EMT-v5_architecture.md)): AST trace, suite green, **one deletion per
  commit**, evidence in the message rather than intention. A reviewer should be able to revert a
  single sha if the removal turns out to have been load-bearing.
- Figures quoted in a commit message or doc name their source and date.
- No probe or scratch file is included — `.gitignore` anchors `/*probe*.py` at the repo root only,
  so a deliberately committed `tests/..._probe.py` is kept while a throwaway at the root is ignored.