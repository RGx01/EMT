# Development

## Branches

| Branch | Holds | Merged via |
|---|---|---|
| `main` | Released. Every commit is a version that has run on real hardware. | PR from `dev` |
| `dev` | Integration. Complete work, testable on other machines, not yet released. | PR from a work branch |
| `fix/…` `feat/…` `chore/…` | One change in progress. Short-lived; deleted after merge. | PR to `dev` |

Branch names say what kind of change it is: `fix/rate-line-zero-on-current-day`, `feat/gas-meter`,
`chore/update-dependencies`, `docs/…` (the same as v4's).

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

## Running the tests

```bash
pip install -r requirements.txt pytest       # in a virtualenv OUTSIDE the repository
bash run_tests.sh                            # the whole suite: each test file in its own process
bash run_tests.sh -k settle                  # extra arguments go to pytest, for every file
```

Always through `run_tests.sh`: it links the modules into `tests/` and runs each file on its own,
because several install `sys.modules` stubs at import; a plain `pytest tests/` collides at collection.
Everything runs against temporary SQLite databases — no Home Assistant, no Octopus, no real data. A
few tests drive the pages' JavaScript in Node, and skip without `node` on the `PATH` (CI skips 8). CI
runs the suite on every PR to `dev` (Python 3.12, the add-on's), and on `main` on 3.12 and 3.13.

## Looking at a change in the browser

```bash
python .build/demo_db.py ~/emt-demo                  # a made-up household, the same every time
python .build/serve_local.py ~/emt-demo              # http://127.0.0.1:8766
```

`serve_local.py` runs the web server and the engine's loop against `<dir>/blocks.db` — no Home
Assistant, no Octopus, so nothing new arrives: the pages show what the database holds, and their
writes work. Point it at a **copy** of a backup's `blocks.db` to see real history; never the live file.
For EMT reading real sensors, run it standalone against a Home Assistant (the README's *Standalone
Docker*) or as a local add-on (below).

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
directory serves it (`EMT_INSTANCE_NAME=Demo python .build/serve_local.py <dir>`, so the footer
names no machine). Take each page at
about 1440 × 900, dark theme, the page only.

## The repositories

| Repository | Holds |
|---|---|
| `RGx01/EMT` (this one, public) | EMT from 2 Nov 2026, when its history was rewritten clean of real account details |
| `RGx01/EMT-archive` (private) | The same work before that: its issues and PRs. A `#NNN` in a history document, or `RGx01/EMT-archive#NNN` in a commit message, is there. |
| `RGx01/energy-meter-tracker-addon` | v4. Critical fixes only. Its `docs/ROADMAP_archive.md` records why v4's decisions were made. |

Never push from a clone of the archive to this repository: the archive's history still holds what
was scrubbed.

## Releasing

Versions are CalVer, `YYYY.M.PATCH`, the month the release's: `2026.11.0`, `2026.11.1`, `2026.12.0`.
A beta is Home Assistant's `bN` on the version it leads to: `2026.11.0b1`, `2026.11.0b2`, then
`2026.11.0`. `_version_key` (`web/server.py`) sorts a beta before its release.

1. On a branch from `dev`: `config.yaml`'s `version:`, and `CHANGELOG.md`'s `[Unreleased]` becomes
   that version with its date. PR to `dev`, merge.
2. PR `dev` → `main`; the PR body is the release note. Merge.
3. Tag `main` and push the tag: `git tag -s v2026.11.0b1 -m 2026.11.0b1 && git push origin v2026.11.0b1`.
4. A GitHub release on the tag, marked **pre-release** for a beta.

A supervised install sees the update from `config.yaml` on `main` (the add-on store reads the default
branch). A standalone install has no store: its *update available* notice reads this repository's
GitHub releases (`/api/update-check`) — the latest release, or, on a beta, the newest of the last 20
including pre-releases. So step 4 is what tells standalone users.

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

- The full suite passes through `run_tests.sh` (above); CI runs it again on the PR.
- A change to timing, the engine's loop or the store is soaked on a Pi as well (the owner's soak,
  `.build/soak.py`): ARM and SD-card I/O are the target's real constraints, and a timing-sensitive
  test that passes on a laptop proves little.
- **A PR that removes code satisfies the deletion protocol**
  ([architecture §4a](history/design/EMT-v5_architecture.md)): AST trace, suite green, **one deletion per
  commit**, evidence in the message rather than intention. A reviewer should be able to revert a
  single sha if the removal turns out to have been load-bearing.
- Figures quoted in a commit message or doc name their source and date.
- No probe or scratch file is included — `.gitignore` anchors `/*probe*.py` at the repo root only,
  so a deliberately committed `tests/..._probe.py` is kept while a throwaway at the root is ignored.