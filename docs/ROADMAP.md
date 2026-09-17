# Roadmap

*Workstream status. The reasoning behind the ordering is in
[design/EMT-v5_programme.md](design/EMT-v5_programme.md) — this file tracks state only.*

**Nothing here has shipped.** EMT is pre-release.

| # | Workstream | Depends on | State |
|---|---|---|---|
| A | Storage and access model | — | 📋 not started |
| B | Job coordinator (BL-50) | A | 📋 not started |
| C | One API for every surface | A | 📋 not started |
| D | Surfacing performance | A, C | 📋 not started |
| E | Importer | A–D | 📝 spec drafted (v2) |

---

## A. Storage and access model

*The root. One writer owned by the engine; read-only connections per request.*

Generalises a fix v4 already made once under duress — `_generate_charts_offloaded` has its own
read-only `BlockStore` because of a cross-thread SQLite segfault — and applies it to every reader
rather than to the chart renderer alone.

**Done when:** a soak hammering every read surface while the engine writes shows no torn rows and
no read blocked behind a write.

## B. Job coordinator

*One coordinator for user-mutating jobs; deny-if-busy with idempotent retry.*

Carried from v4's BL-50 design. The asymmetry is the bug: import and delete are well guarded
(17 and 14 references), attribution is not (3) — and attribution is the one that has crashed against
the engine. With A done this shrinks to coordinating writers only.

**Done when:** every mutating job acquires the coordinator, and a concurrency test proves a second
job is denied rather than interleaved.

## C. One API for every surface

*Break the write → wholesale-recompute coupling. No pre-generated HTML.*

Not a format change. In v4, 17 call sites schedule a regeneration and regeneration is wholesale, so
one half-hour finalising re-renders two years. Five layers of compensation have accumulated around
that; see the programme map.

**Done when:** a write records that data changed and triggers no recompute, and every surface is
served by one API over a requested range.

## D. Surfacing performance

*Measure before optimising.*

Most of the win is delivered by A and C. Target hardware is modest — a Raspberry Pi 4 class box on
SD or USB storage — so ARM CPU and I/O latency, not data volume, set the ceiling.

**Done when:** time-to-first-byte and queries-per-surface are instrumented, with a budget per
surface that CI can assert.

## E. Importer

*Ingest a 4.5.13 backup. Spec at [design/EMT-v5_import_spec.md](design/EMT-v5_import_spec.md).*

Last deliberately: its contract is "write the EMT schema", so it cannot be finished until A–D have
settled what that schema is.

**Done when:** the acceptance gates in §9 of the spec pass against the real 4.5.12 and prod-dev
databases, and the 4.3.1 database is refused.

---

## Open questions

Carried from the programme map §4 and the import spec §10. These need answers before the
workstreams that depend on them start.

- [ ] **Does EMT publish anything to Home Assistant?** Entity/statistic names are the one shared
      namespace `instance.py` does not arbitrate — two running add-ons would collide. Blocks the
      first release that publishes anything. *(A/C)*
- [ ] **Is on-demand computation actually cheaper than pre-rendering once?** True at 78 MB and one
      user; it is the assumption all of C rests on, so measure before it becomes doctrine. *(C/D)*
- [ ] **Test-suite portage** — wholesale (keeps the v4-equivalence proof) or rebuilt against the new
      API (suits the new shape)? Probably both, in that order. *(C)*
- [ ] **What is the first public release's bar?** "Imports my history and shows it correctly" is a
      far earlier date than "replaces v4 for daily use", and decides how much of B–D is pre-release.
- [ ] **Attribution state in a backup** — is a source with an unfinished attribution job importable,
      or refused like an unfinished 4.4.0 migration? *(B/E)*
