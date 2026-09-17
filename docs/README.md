# EMT documentation

| Document | Purpose |
|---|---|
| [ROADMAP.md](ROADMAP.md) | Workstream status — what is open, in progress and done |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Branch model, testing a branch on another machine, releasing |
| [CONTRIBUTING.md](CONTRIBUTING.md) | What a change must carry, and what not to commit |
| [design/EMT-v5_programme.md](design/EMT-v5_programme.md) | The programme map: five workstreams in dependency order |
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
  without its reasoning gets relitigated.
