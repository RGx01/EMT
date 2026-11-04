# EMT documentation

Four kinds of document, kept apart so each can be trusted for what it is:

| Where | What it is | Kept true by |
|---|---|---|
| [reference/](reference/README.md) | **How EMT works today** — how it is built, pricing and settlement, Data Management and devices, the v4 import and first run, the pages and the API | Fixed in the same PR as the code it describes; when a page and the code disagree, the code is right |
| [design/](design/) | Living contracts, and designs being decided: [the import spec](design/EMT-v5_import_spec.md) (how a v4 database is brought across, and the floor), [the writer thread](design/EMT-v5_A1_writer_thread_design.md), designed and held in reserve | Versioned; changed with the code |
| [history/](history/) | **Records**: each design as it was decided ([history/design/](history/design/)), and the done half of the roadmap ([ROADMAP-done.md](history/ROADMAP-done.md)). Each says what it was and where today's account is. `#NNN` there is in the private archive. | Not updated: a record stays as it stood |
| [ROADMAP.md](ROADMAP.md) | What is open: before the first release, the backlog, code to tidy, open questions | Items leave when they are done |

And:

| Document | Purpose |
|---|---|
| [DEVELOPMENT.md](DEVELOPMENT.md) | Branches, running the tests, looking at a change locally, the repositories, releasing |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Reporting a problem; what a change must carry, and what won't be accepted |
| [../CHANGELOG.md](../CHANGELOG.md) | What changed, for the people using EMT |
| [../BETA.md](../BETA.md), [../SECURITY.md](../SECURITY.md) | For beta testers; reporting a vulnerability, and what EMT holds |
| The help, in the app | How to use each page (`web/templates/help/`) |

## Conventions

- **A design document before a change of shape.** It is written in `design/` while it is decided and
  built; once built, it moves to `history/design/` with the history header, and what it settled is
  written into `reference/`.
- **Evidence over assertion.** A claim about behaviour cites the code, a log line, or a query
  against a real database. Figures measured from a database name which database and when — they
  move, and a stale figure presented as current is worse than none.
- **Raw precision.** Never reason from a rounded or formatted value. Several wrong conclusions
  during 4.5.13 came from 4dp log lines and 6dp histograms; compare stored values at full precision.
- **Decisions are recorded with their rationale**, including rejected alternatives. A decision
  without its reasoning gets relitigated — and, more usefully, a decision stated *with* its reasoning
  is falsifiable by a reader. "Add `account_ref`" cannot be argued with; "add `account_ref` because
  reading a Kraken-specific key would leak v4's layout into v5" can, and was.
- **Check v4 before specifying anything new.** Before a document proposes a field, function, table
  or constant, search the v4 tree for it. On 19 Sep 2026 three consecutive proposals here —
  `account_ref`, an account-mismatch guard, and a supplier token — described things v4 already had
  (`kraken_state.kraken_account_number`, `kraken_account_mismatch()`, and the `WIZ_SUPPLIERS`
  registry with `normalize_supplier()`). Each was caught in review; none should have reached it.

  The additive version of this mistake is cheap — a redundant field, spotted and dropped. **The
  subtractive version is not**, and this programme is subtractive: the same failure to look becomes
  "nothing calls this, remove it" when something does. That is why deletions have their own protocol
  ([architecture §4a](history/design/EMT-v5_architecture.md)) rather than relying on care.
- **Removal follows the deletion protocol, without exception.** AST trace, suite green via
  `run_tests.sh`, one deletion per commit, evidence in the message. A surface with no test covering
  it additionally needs a before/after comparison against a real database.