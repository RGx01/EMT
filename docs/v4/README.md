# v4 documentation — frozen reference

*Copied verbatim from Energy Meter Tracker 4.5.14 when this repository was seeded, and **not
maintained**. Nothing here describes EMT; it describes the v4 application EMT was copied from.*

**Why it is kept.** These design documents are the expensive part of the project — three years of
tariff edge cases, settlement behaviour and dispatch semantics, written down. The code carries the
behaviour; this carries the reasoning.

**Why it is frozen.** `docs/DEVELOPMENT.md` explains that copying v4's architecture documentation
into EMT's own docs would produce something wrong on the day it landed and drifting thereafter —
the storage model, job boundaries and surfaces are precisely what EMT changes. Keeping it here,
clearly marked, gets the knowledge without the confusion.

**So:** read it to understand *why the domain works as it does*. Do not read it as a description of
EMT, and do not update it — if something here is wrong about EMT, the answer is a document in
`docs/`, not an edit here.
