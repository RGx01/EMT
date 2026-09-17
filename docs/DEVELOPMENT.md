# Development

## Branches

| Branch | Holds | Merged via |
|---|---|---|
| `main` | Released. Every commit is a version that has run on real hardware. | PR from `dev` |
| `dev` | Integration. Complete work, testable on other machines, not yet released. | PR from a work branch |
| `work/<topic>` | One change in progress. Short-lived; deleted after merge. | — |

Nothing is committed straight to `main` or `dev`, including by the repository owner. The rule holds
when there is one developer for the same reason it holds when there are five: the PR is where the
diff gets read as a whole, and a change that was obvious while writing it stops being obvious a
month later.

```bash
git switch dev
git switch -c work/storage-readonly-connections
# ... commits ...
git push -u origin work/storage-readonly-connections
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

If a released version is already installed from a repository, stop it first — two instances of the
same add-on running against the same Home Assistant is exactly the entity-collision case that is
still an open question (see [ROADMAP.md](ROADMAP.md)).

## Releasing

1. PR `dev` → `main`. The PR body is the release note.
2. Merge, then tag on `main`: `git tag 2026.10.0 && git push --tags`.
3. CalVer — `YYYY.M.PATCH`. The month is the release month, not the sprint.

## Before opening a PR

- The full test suite passes on the **Pi**, not only on a development machine. ARM and SD-card I/O
  are the target's real constraints; a timing-sensitive test that passes on a laptop proves little.
- Figures quoted in a commit message or doc name their source and date.
- No probe or scratch file is included — `.gitignore` anchors `/*probe*.py` at the repo root only,
  so a deliberately committed `tests/..._probe.py` is kept while a throwaway at the root is ignored.
