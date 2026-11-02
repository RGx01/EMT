# EMT documentation

| Document | Purpose |
|---|---|
| [ROADMAP.md](ROADMAP.md) | Workstream status — what is open, in progress and done |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Branch model, testing a branch on another machine, releasing |
| [CONTRIBUTING.md](CONTRIBUTING.md) | What a change must carry, and what not to commit |
| [design/EMT-v5_architecture.md](design/EMT-v5_architecture.md) | **The agreed picture** — four layers, what carries, what dies. Read first. |
| [design/EMT-v5_programme.md](design/EMT-v5_programme.md) | The programme map: five workstreams in dependency order |
| [design/EMT-v5_A_storage_design.md](design/EMT-v5_A_storage_design.md) | Workstream A — storage and access model (the root) |
| [design/EMT-v5_C_surfaces_design.md](design/EMT-v5_C_surfaces_design.md) | Workstream C — one API, retiring pre-generated HTML |
| [design/EMT-v5_schema_notes.md](design/EMT-v5_schema_notes.md) | `blocks` table shape — what goes, what stays, what was rejected |
| [design/EMT-v5_import_spec.md](design/EMT-v5_import_spec.md) | The v4 → EMT migration contract |

## Conventions

- **One design document per workstream**, created when that workstream is picked up. The programme
  map frames them; it does not replace them.
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
  ([architecture §4a](design/EMT-v5_architecture.md)) rather than relying on care.
- **Removal follows the deletion protocol, without exception.** AST trace, suite green via
  `run_tests.sh`, one deletion per commit, evidence in the message. A surface with no test covering
  it additionally needs a before/after comparison against a real database.