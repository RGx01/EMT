# EMT v5 — database import specification

*The contract for bringing an existing Energy Meter Tracker database — **4.5.14 or later, and at
v4's final heal high-water mark** (§1.1) — into the new EMT app. This is the artefact the rest of v5 depends on: the new schema, the charting rebuild and the
legacy-column removal all assume history arrives correctly once, from here.*

**Status:** v4 — revised 19 Sep 2026 against merged 4.5.13 plus the 4.5.14 decision. Numbers below
are measured from a real 4.5.12 production database (135,482 blocks, 3 meters, ~2 years) unless
stated; provenance counts in §9 are refreshed from a 4.5.13 prod-dev DB.

*Changes in v2: §1 records the side-by-side container decision and closes Q1/Q4; §3 gains a source
**provenance** gate; §5.1 covers the `label='ZERO'` measured row (BL-70, 4.5.13); §5.2 makes the band
vocabulary an explicit importer mapping; §9 gate 5 widens from a sample to every block.*

*Changes in v3: **§5.2's band rule is reversed.** v2 had v5 unifying to `off_peak` / `peak` and
mapping `day → peak`. That was wrong — it flattens a real distinction between two different
concepts. v5 keeps all four labels and performs one era-scoped relabel instead. §1 and Q5 gain the
importer's retirement rule.*

*Changes in v4: **the source floor moves from 4.4.0 to v4's final heal high-water mark, and the
importer supports one schema era instead of two.** v1–v3 were written before 4.5.14 was decided and
still claimed there would be no final v4 release. There is: 4.5.14 ships BL-72's `heal_level`, and
that marker — not table presence, not a "done" flag — is now the gate. §3's first three gates
collapse into one that is strictly stronger, §4's era table is deleted, and the 4.5.12/4.5.13
databases on hand become **negative** test cases alongside the 4.3.1 one.*

*A first pass at v4 wrote the floor as the literal `4.5.14`. Corrected same day: the bar is wherever
v4 **stops**, which post-.14 critical fixes can move, and it is a release-day input rather than a
design-time constant (§1.1, Q9).*

---

## 1. Purpose and shape

v5 ships as a **new add-on with a new slug**, so users get a *fresh install*, not an in-place
upgrade. There is therefore no schema migration inside the new app — instead there is a
**one-directional importer** that reads a **4.5.14+ database that is also at v4's final heal
high-water mark** (§1.1) and writes the v5 schema. 4.5.14 is the earliest such source, not
necessarily an admissible one.

Consequences that follow from that choice, and which this spec exists to protect:

- The new app **never contains** the pre-4.4.0 one-time heals (BL-33: 7 code sites already tagged
  `# DEPRECATED — remove in 5.0.0`), nor the 15 legacy derived columns, nor the `'measured'`
  naming. Those are known *to the importer only*.
- The old add-on keeps working throughout. A failed import costs the user nothing.
- **The two run side by side, in their own containers, indefinitely.** EMT 2026 is not a
  replacement the user must cut over to on a deadline; it is a second add-on that ingests a copy of
  the first one's history. v4's instance isolation already yields contended resources rather than
  fighting for them (`resolve_backup_dir` hands `/share/..._<site>` to whichever instance owns it),
  so the coexistence mechanism exists and is proven in production.
- There is **no cooperating exporter**. The source is what v4 already writes — the backup zip — and
  the importer validates it itself (§3) rather than trusting a contract.

### 1.1 The floor is v4's high-water mark; 4.5.14 is where the marker begins

**Decided 19 Sep 2026.** Earlier drafts of this spec set the floor at 4.4.0 and stated there would
be no final v4 release. Both are wrong. **4.5.14 ships BL-72**, whose job is to stamp the database
with a claim v5 can act on:

| marker | where | status |
|---|---|---|
| `heal_level` INTEGER | `store_meta` | **New — BL-72.** Which one-off heals hold **against this database's current contents**. Computed by each heal re-verifying its own invariant, not a sticky "I ran once" flag. |
| `written_by` TEXT | `store_meta` | **New — BL-72.** The software that last wrote the database. |
| account identity | `kraken_state.kraken_account_number` | **Already exists.** Populated in every real database checked (4.5.12, 4.5.13 prod and prod-dev). |
| supplier | `config_periods.supplier` | **Already exists, but as display text** (`'Octopus Energy'`), not a machine token. See below. |

EMT cannot retrofit a marker it was not present for — but two of the four are already there, and an
earlier draft of this section proposed adding duplicates of both. Corrected 19 Sep 2026 after
checking the schema.

#### The account is already stamped, and v4 already guards it

`engine.py` holds `_ACCOUNT_KEY = "kraken_account_number"` in `kraken_state`, written on first
association and on explicit reconnect (`engine.py:5507`, `:5509`). Two functions already exist:

- **`get_db_account()`** — *"The Octopus account this DB is stamped to, or None if never stamped."*
  Exactly the read the importer needs, including the never-stamped case §3 gate 3 branches on.
- **`kraken_account_mismatch()`** — the guard itself, already implemented.

So v5 does **not** need a new `account_ref` field. A draft of BL-73 proposed one on the grounds that
reading a Kraken-specific key would leak v4's layout into v5 — a weak argument, since the importer
reads v4's entire schema by definition, and a bad trade against duplicating a value that exists.

#### The supplier field that exists is the wrong kind

`config_periods.supplier` is documented in the schema as *"energy supplier name (display +
historical record)"* and holds `'Octopus Energy'`. Free text meant for a header, not a token to
switch an authentication flow on — nobody should branch on a string a user might type three ways.

**But a v4-side machine token may not be needed either.** v4 is frozen after .14, so it will never
support a second supplier: **every v4 database that will ever exist is Octopus.** `written_by`
already identifies a source as v4, which makes the supplier derivable with certainty, in one line,
inside the module that already knows everything about v4 and gets deleted when v4 support ends.

Where the requirement genuinely lives is **v5's own schema** — v5 *will* be multi-supplier, so v5's
databases and backups need a stable machine token from the start, for exactly the reason a marker
cannot be added after the fact. That is a v5 schema item, not a .14 item.

> **Open, and the user's call (see BL-73).** Ship a `store_meta.supplier` token in .14 anyway — it
> costs one string, and "derivable in the importer" is an argument that has to stay true — or rely
> on `written_by` and put the token only in v5's schema.

#### The level is a claim about invariants, not a version number

This is the distinction the whole gate rests on. `heal_level = 8` does not mean "this database came
from a release that had 8 heals". It means **eight named invariants have been checked and currently
hold**. A database cannot report a level for a heal its software has never heard of — so an install
sitting on .14 reports .14's mark however long it sits there, and updating is what raises it.

#### So the floor is not a fixed number, and must not be written as one

4.5.14 is the **earliest** a source can be, because it is the first release able to express a level
at all. It is not necessarily the floor. v4 is frozen to critical fixes after .14, and a pricing-error
fix is exactly the kind that repairs already-written data — so the mark can move to .15, .17,
wherever v4 actually stops.

> **v5's required level is v4's final high-water mark, and it is pinned before v5's release —
> not now.** It is a single named constant with one definition site, supplied as a release-day
> input (§10 Q9). The importer must never hard-code a version string, and no gate, message or test
> may assume the answer is 4.5.14's value.

The consequence is deliberate and correct: if v4 ends at .17, a user who migrated their backup from
.14 **is refused**, because three invariants .17 defines have never been verified against their
data — and v5 has no heals to repair what v4 didn't. They update v4, let it re-verify, back up
again, import. The refusal is a one-line inconvenience; importing silently-unrepaired history is
not.

**What this buys either way: the importer supports one schema era, not two.** Any database carrying
a `heal_level` is 4.5.14 or later, so it always has `block_segments`, `rate_source` and
`measured_cost`. The 4.4.0–4.5.4 era — segments present, settled provenance entirely absent — can no
longer reach the importer at all. §4 is deleted rather than defended, and §3's first three gates
become one.

**Nobody is stranded, at any level.** A user on 4.4.0, on 4.5.13, or on 4.5.14 when the mark has
moved past it, is never refused permanently — they are told to update the old add-on, let it finish,
take a backup, and import that. The heals run where they were always meant to: in v4, against v4's
schema, with v4's tests behind them. That is the whole reason .13 was declared the final *era* of
heals for v5's purposes and v5 starts from an empty heal set of its own.

### 1.2 The importer is a separable unit with a retirement rule

Everything v4-shaped — the gate (§3), the column rules (§5.2), the `store_meta` allow-list (§6) —
lives inside it and nowhere else:

> **E is one module. No domain code may import from it, and no domain concept may be
> importer-only. It is removed in the release after 4.5.14 sources stop being supported.**

This is not housekeeping. v4's three legacy backfills (162 lines) became the cautionary tale
*because they were embedded in `engine.py` with no boundary to cut along* — unwiring was the only
cheap move available, so deletion never happened and the code has sat since reading as
load-bearing. A module with one direction of dependency and a stated end date cannot decay that
way: when the date arrives, the deletion is a directory.

**Non-goals.** This is not a rewrite. The pricing/settlement engine (`engine.py`,
`block_store.py`, `pricing_segments.py`) is carried across substantially as-is; the importer must
not "improve" pricing on the way through. Re-deriving history is explicitly out of scope except
where §5 says so.

---

## 2. The governing invariant

> **Carry what cannot be re-fetched. Re-derive only what is reproducible. Never upgrade
> provenance on the way through.**

This is not a performance preference — some source data is *permanently* unavailable from Octopus:

| Data | Why it cannot be recovered |
|---|---|
| `measured_cost` (settled cost / band / four-bucket split) | Octopus does not always re-serve cost for a past half-hour. Evidenced: a 4.3.1 user database carries **2,091** half-hours for which the Measurements API returns kWh and **no cost**, permanently. |
| Cost-Corrections (`rate_corrected` / `rate_source='corrected'`) | Pure local user input. Nothing upstream has ever held it. |
| `dispatch_slots` / `dispatch_history` | EMT retains the lifecycle permanently *because* Octopus prunes at ~90 days (BL-35, shipped deliberately). |
| `deleted_ranges` | Deliberate-deletion tombstones. Lose them and the new app's backfill silently **re-creates data the user deleted on purpose**. |
| `historical_derivation` + `recorder_attribution_runs` | The device-attribution ledger that makes attribution reversible. |
| `vat_calendar` | Local VAT-rate calendar underpinning every ex-VAT figure. |

The third clause matters as much as the first two, and the 4.5.14 floor does **not** retire it. A
4.5.14 database still contains history written long before settlement existed — every block from the
4.4.0–4.5.4 era carries `rate_source='schedule'` because nothing ever settled it. The importer
carries that word across unchanged. What it must never do is treat "this database is from a version
that *has* settlement" as "this block *was* settled". We spent 4.5.12 fixing exactly that class of
bug — a block claiming to be settled when it wasn't.

The distinction the floor removes is only about the **schema**: every source now has the columns.
The distinction it preserves is about the **rows**: most of them are still unsettled, and say so.

---

## 3. Source eligibility — the gate

The importer's first act is to decide whether a source database is admissible.

**The gate runs in two phases, and the order matters.** Everything that can be checked from the file
alone is checked **before** the user is asked to authenticate; only the account match needs
credentials, and it comes last.

```
PHASE 1 — file only, no credentials                PHASE 2 — after auth
  1  no hot -wal sibling                             4  account stamp matches the
  2  opens read-only; integrity clean                   credentials just supplied (§7)
  3  heal_level ≥ REQUIRED_HEAL_LEVEL
  4  supplier present and supported
        ↓                                                    ↑
   supplier tells the wizard WHICH credential prompt to show ┘
```

**The order within phase 1 is operational, not arbitrary.** The WAL check is a filesystem test and
happens before the file is opened at all. Integrity comes next, because **a marker read out of a
damaged database is not evidence of anything** — `heal_level` and `supplier` are only trustworthy
once `quick_check` has passed. Then the two markers, cheapest and most decisive last.

**Why not check the account first?** Because asking someone to sign in and *then* telling them their
backup is unusable is the wrong way round — and because with multiple suppliers it is not even
possible. You cannot prompt for credentials until you know which supplier's credentials to ask for,
and the backup is what says so (§1.1, §3a). Phase 1 is therefore both kinder and structurally
necessary.

**Phase 1 — admissible from the file alone iff all hold, checked in this order:**

1. **The source is a backup, not a live database, and it is intact.** The accepted inputs are a
   backup **zip** as written by `_backup_to_share`, or a `blocks.db` **taken from one**. A file
   copied out of a running container is not admissible: v4 holds the database in WAL mode and
   checkpoints only at startup, so a `blocks.db` lifted mid-run can have committed history sitting
   in a `-wal` sibling that the main file does not yet contain. The importer must not silently drop
   the user's most recent days.

   Enforced as: if a `-wal` sibling is present and non-empty → **refuse**, naming the file and
   directing the user to take a proper backup. If it is absent, open the database **read-only**
   (`file:…?mode=ro`) and verify `PRAGMA quick_check` and `PRAGMA foreign_key_check` are clean.
   The importer never opens a source read-write, and never checkpoints someone else's database —
   recovering a hot WAL would mean writing to the live app's file from another container.

2. **`store_meta.heal_level` is present and ≥ `REQUIRED_HEAL_LEVEL`** — v4's final high-water mark,
   pinned at v5's release (§1.1), never a literal in the gate. This one gate replaces the three that
   earlier drafts used (does `block_segments` exist · did `reprice_history_state` say `done` · is
   `count_blocks_needing_reprice()` zero), and is **strictly stronger** than all three together.

   Stronger because of how BL-72 defines the level: each heal **re-verifies its own invariant
   against the database's current contents** rather than setting a sticky "I ran once" flag. A
   marker written optimistically, or written truthfully and then invalidated by a later import,
   restore or delete, does not survive re-verification. The old gate 3 existed precisely as "belt
   and braces against a marker written optimistically"; a computed level makes that belt structural.

   Absence of `heal_level` is itself decisive: only 4.5.14+ writes it, so a database without one is
   pre-4.5.14 whatever else it contains. `written_by` supplies the version to name in the refusal.

3. **If `store_meta.supplier` is present, this build must support it.** Today that means
   `'octopus'`. A future EMT that has not implemented a supplier refuses its backups plainly rather
   than importing history it cannot price or extend.

   **If `supplier` is absent, that is not a refusal — it is a branch.** BL-73 writes `supplier` and
   the account stamp only once credentials have been established, so an install that was never
   connected to an energy account carries no stamp (`get_db_account()` returns None). Such a database can still hold **real meter
   history**: kWh read locally from a CAD is genuine, irreplaceable consumption data that simply
   never got priced. Refusing it would throw away the user's record to enforce a check that has
   nothing to check.

   So: **no `supplier` ⇒ skip the credential prompt, skip phase 2 entirely, and import the
   history.** The user lands in the normal account-setup flow afterwards and starts costing from
   there — exactly what a v4 install does when credentials arrive after some history already exists.

   *The asymmetry decides it.* Wrongly importing unpriced kWh costs nothing — it is visible, it is
   the user's own data, and connecting an account later fixes it. Wrongly refusing it destroys
   history that no API can re-serve. When a check has no subject, skip the check; do not refuse the
   subject.

   **Treat the account stamp as the signal, not a marker pair.** `get_db_account()` already answers
   "was this database ever associated with an account?", which is the only question gate 3 needs.

   *(Two earlier drafts got this wrong in opposite directions — first "absence cannot occur, treat
   as corruption", then "absence means there is nothing to bring across, refuse". Both destroy
   importable history.)*

**Phase 2 — only when *both* sides present an account identity** (the source is stamped per
`get_db_account()`, and the target has credentials configured). Skipped otherwise, including on the
diagnostic path where the recipient has deliberately disconnected — see "Importing a database you do
not own" below:

4. **The source's account stamp matches the account those credentials belong to** (§7).

   **A mismatch does not refuse the import.** v4 already faces this exact situation and answers it
   better than an earlier draft of this spec did — `kraken_account_mismatch()`:

   > *"the loaded DB is stamped to a different Octopus account than the current credentials, so the
   > API was NOT auto-activated. Credentials are kept; an explicit in-app reconnect re-associates
   > this DB."*

   So the history **loads**; what does not happen is the account being associated or the API being
   auto-activated, and the user is told, with reconnect available if they meant it. v5 copies that
   behaviour rather than inventing a refusal. It protects the same thing — no silent mixing of two
   properties' data — without the failure mode of blocking someone from reading their own history.

> **Do not use `schema_version`.** It reads `1` on a 4.3.1, a 4.5.12 and a 4.5.13 database alike —
> it is not a version discriminator, and its uselessness is what BL-72 exists to fix.

**Refusal must be explicit and actionable**, never a partial import:

- *no `heal_level`* → "This database was written by Energy Meter Tracker `<written_by>`, which is too
  old to say whether its one-time repairs are complete. Update the old add-on, let it finish, then
  take a backup and import that." The user is delayed, not stranded — §1.1.
- *`heal_level` below `REQUIRED_HEAL_LEVEL`* → "This backup was checked against `<found>` of the
  `<required>` repairs EMT needs (written by `<written_by>`). Update the old add-on, let it
  re-check, then back up again."

  **Both messages name a version only from `written_by`, never a target.** The target version is
  whatever v4 ended at, which this document does not know and the code must not assert — so the
  instruction is *"update the old add-on"*, not *"update to 4.5.14"*. This second case also covers
  the two distinct situations the gate deliberately does not distinguish: a source whose repairs
  never finished, and a source that finished the repairs **its** release knew about while the mark
  has since moved. The remedy is identical, so the message is one message.

  This is also the case a presence check would wave through: the marker exists, and the invariants
  still don't all hold.
- *hot WAL present* → "This looks like a copy taken while Energy Meter Tracker was running, and
  some of your most recent half-hours are not in it yet. In the old add-on take a backup, then
  import the zip it writes."
- *integrity check fails* → "This database is damaged and cannot be imported safely." Never
  attempt repair: the user still has the working original.
- *unsupported supplier* → "This backup is from a `<supplier>` account, which this version of EMT
  cannot import yet."
- *account mismatch* → §7.

**Not a refusal:** *both* markers absent. That is the unconfigured-source branch — import proceeds
without a credential prompt (gate 3), and the user connects an account afterwards.

### Credentials are not in the backup, and a missing key is not a refusal

**Verified against merged 4.5.13.** `engine.py:save_kraken_credentials` writes the key to
`KRAKEN_CREDS_PATH` = `{DATA_DIR}/kraken_credentials.json`, mode 0600, and says why in its own
docstring: *"outside the DB so they are never included in backups"*. `_backup_to_share` copies
`blocks.db` plus `meters_config.json` — nothing else. Disconnect is a full teardown, not a file
delete: `/api/disconnect-kraken` → `disconnect_kraken()` stops polling, tears down the client and
removes the file.

Three consequences, and the third is the one earlier drafts got wrong.

1. **v5 never has a key to lift.** The question of whether to trust a secret found in a backup does
   not arise; there is never one there. A key can only ever arrive by the user typing it.
2. **That is what makes the account check meaningful.** Phase 2 compares the source's account stamp
   against the account behind credentials the *user supplied*, which is evidence they hold that
   account. Credentials read out of the same file as the stamp would have been checking the backup
   against itself.
3. **A database can therefore be imported with no credentials at all, on either side.** The
   credential step is **skippable**. Requiring a live account connection in order to load historical
   data would be requiring the wrong thing: the history is already in the file, and costing is a
   forward activity that begins whenever an account is connected. v4 already treats "no credentials"
   as an ordinary state — `has_kraken_credentials()` guards exist throughout — and v5 inherits that.

### Importing a database you do not own

**This is a real, supported workflow and the gate must not break it.** Users send their databases in
for diagnosis; the recipient disconnects their own account first, then imports. Disconnecting
removes the credentials file, so the target instance has no account identity at all.

The rule that follows is symmetric with §3 gate 3:

> **Phase 2 runs only when *both* sides present an account identity** — the source carries
> an account stamp, and the target has credentials configured. If either is absent there is nothing
> to compare, so the check is skipped rather than failed.

That single rule covers every case without a special diagnostic mode:

| source | target | phase 2 |
|---|---|---|
| stamped | connected | **checked** — on mismatch the history still imports; the account is not associated and the API is not auto-activated (§7) |
| stamped | disconnected | skipped — the diagnostic path |
| never stamped | connected | skipped — nothing to compare (§3 gate 3) |
| never stamped | disconnected | skipped |

No override is needed for the checked row either, because a mismatch was never a refusal — v4's
`kraken_account_mismatch()` imports the data and declines to associate the account (§7). Disconnecting
first remains the cleaner path, since it states the intent up front and avoids the warning entirely.

---

## 3a. First run: import or fresh setup

**Added 19 Sep 2026 — this was missing, and its absence would have shipped an app no v4 user could
migrate into.** Every earlier draft described what the importer *does* and never said how a user
reaches it. The implicit assumption was a fresh install that gets configured and then, somehow,
imports later. For the population this spec exists to serve — every current v4 user — that is the
wrong default path and in the multi-supplier case it is an impossible one.

### The fork

v5's first start offers a choice, before anything else is configured:

```
first start
   │
   ├── "Set up a new account"  ──────────────► supplier → auth → meters → done
   │
   └── "Restore from an Energy Meter Tracker backup"
              │
              ├─ upload the zip
              ├─ PHASE 1 gate: not-live · integrity · heal_level · supplier     (§3)
              │     └─ refuse here, before asking the user for anything
              │
              ├─ supplier present? ──── no ──┐   (never configured: real kWh
              │                              │    history, never priced)
              │   yes                        │
              │    ├─ show THAT supplier's   │
              │    │  credential prompt,     │
              │    │  with SKIP offered      │
              │    │  (keys are never in a   │
              │    │   backup — §3)          │
              │    │                         │
              │    ├─ skipped? ──────────────┤   (import history now,
              │    │   no                    │    connect an account later)
              │    └─ PHASE 2: account stamp │
              │       matches that account   │
              │            │                 │
              ├────────────┴─────────────────┘
              │
              ├─ import (§8) — one job, all-or-nothing, takes the job lock
              │     └─ NO heals run: v5 contains none, which is what the
              │        heal_level gate exists to make safe (§1.1)
              ├─ write the audit record (§9)
              └─ start the engine — history in place
                    └─ if no account was connected (no supplier, or the user
                       skipped), hand off to account setup. Costing begins
                       whenever that happens; the history is already there.
```

**Every path ends with the history imported.** The only outcomes that stop an import are the phase 1
refusals — too old, damaged, unsupported supplier, inconsistent markers — and a genuine account
mismatch. Absent credentials, on either side, never block it.

### Why the backup drives the credential prompt, not the reverse

This is the sequencing consequence of `supplier` existing at all. The wizard cannot ask *"which
supplier?"* on the restore path, because the backup already knows, and asking invites the user to
answer wrongly and then fail the account check for a reason that looks unrelated. Nor can it ask for
credentials before reading the file, because **the shape of the credential prompt depends on the
supplier** — Octopus wants a Kraken API key, another supplier may want OAuth or a password.

So the order is fixed: *read the backup → learn the supplier → ask for that supplier's
credentials → verify they match the account the backup names.* Each step's input is the previous
step's output, which is also why §3 splits in two.

### Both paths must still work

A user who picks "new account", uses EMT for a week, and *then* decides to bring their history
across is not blocked. The import runs against a configured instance, and §3's phase 2 does exactly
what it always did — compares the account stamp against the credentials already present, and on a
mismatch imports anyway without associating the account. The fork is about making the good path reachable, not about closing the other one.

> **Scope note.** The fork is client-layer work, not importer work — the gates and the ingest are
> the same in both orders. It lands with the client (architecture §6 step 6), and the only thing
> that has to be true earlier is that the account stamp exists — and it already does, in every
> version in scope.

---

## 4. One schema era

*Earlier drafts carried a two-era table here — 4.4.0–4.5.4 (segments present, settled provenance
entirely absent) versus 4.5.5+ — with detection by column presence and a rule that a 4.4.x import
must never produce settled history. **Deleted 19 Sep 2026.** With the floor at 4.5.14 (§1.1), the
first era cannot reach the importer: a database carrying a `heal_level` always has
`block_segments`, `rate_source` and `measured_cost`. There is no era detection, no column-presence
probing, and no conditional carry.*

**What remains true, and is not an era question:** a 4.5.14 database still holds history from before
settlement existed, and those rows say `schedule`. The importer carries each row's own provenance
word across; it does not infer provenance from the source's version. That is §2's third clause, and
it applies to every source equally now rather than to one era specially.

Observed provenance distribution on the 4.5.12 reference database (`electricity_main`):
`schedule` 56,932 · `measured` 360 · `corrected` 33 · `reconciled` 20 · null 7. The shape is
representative of what a .14 source looks like — overwhelmingly unsettled history with a thin recent
settled edge — which is why "most rows are unsettled" must stay a normal outcome and never a
warning.

---

## 5. Table-by-table disposition

20 tables in the source. Disposition codes: **CARRY** (verbatim) · **CARRY+REMAP** (shape changes)
· **DERIVE** (recompute in v5) · **DROP** (do not import) · **REGEN** (new value in v5).

### 5.1 Core history

| Table | Rows (ref) | Disposition | Notes |
|---|---|---|---|
| `blocks` | 135,482 | **CARRY+REMAP** | Per-column rules in §5.2. The row identity (`block_start`, `meter_id`) is the join key for everything else. |
| `block_segments` | 37,184 | **CARRY** | The pricing source of truth. Carry verbatim — never recompute from the legacy columns. |
| `measured_cost` | 360 | **CARRY — every row** | Irreplaceable. Carry all columns incl. `home_kwh/ev_kwh/home_rate/ev_rate` and `fetched_at`. 4.5.5+ only. **Including rows that look empty** — see below. |
| `config_periods` | 1 | **CARRY** | Block minutes / timezone / currency / billing day underpin every aggregate. |
| `meters` | 3 | **CARRY** | Device identity, parent/sub relationships, retirement state, sensor bindings. |
| `meter_channels` | 4 | **CARRY** | MPAN / tariff / rate-source bindings per channel. |

**`label='ZERO'` rows are data, not noise (4.5.13, BL-70).** A dispatched half-hour in which
nothing was drawn is answered by the supplier as a real reading with every bucket at zero. 4.5.13
caches that answer as `cost_incl=0, cost_excl=0, kwh=0, label='ZERO'`, with the four breakdown
columns left NULL. Such a row asserts *"the bill says nothing was drawn here"* — which is what stops
the settlement pass re-asking about the slot on every hourly tick, forever.

An importer that filters "empty-looking" rows, or that treats `cost_incl = 0` as *missing*, will
drop them and restart that loop in the new app. The three states are distinct and all three must
survive: **row with figures** (billed), **row with `label='ZERO'`** (billed nothing), **no row**
(never asked). The same distinction governs the split: breakdown columns present-and-zero means the
bill stated no EV; breakdown columns NULL means the bill stated nothing about the split, and a
completed dispatch still decides. v5's schema must keep those separable.

### 5.2 `blocks` — column rules

**CARRY (canonical facts and provenance):** `block_start`, `block_end`, `meter_id`,
`config_period_id`, `interpolated`, `imp_kwh`, `imp_kwh_api`, `exp_kwh`, `exp_kwh_api`,
`imp_read_start/end`, `exp_read_start/end`, `standing_charge`, `carbon_g`, `carbon_intensity_g`,
`source`, `is_provisional`, `finalised_from_cad`, `needs_review`, `review_reason`,
`review_dismissed`, `derivation_id`, `rate_corrected`.

**CARRY — authoritative totals:** `imp_rate`, `imp_cost`, `exp_rate`, `exp_cost`. `imp_cost` on a
settled block **is the bill** and must not be recomputed from segments; segments intentionally
differ by a sub-penny sliver which the remainder absorbs.

**CARRY+REMAP — provenance:** `rate_source` → v5 naming (`'measured'` → `'settled'`, BL-61(5)).
Always present on a .14 source (§4), so there is no absent-column branch — carry each row's own
word. Fold `rate_reconciled` into the provenance model rather than carrying a second flag.

**DROP — derived, reconstructable from segments** (13 columns; this is the legacy-column removal):
`imp_kwh_grid`, `imp_kwh_remainder`, `imp_cost_remainder`, `imp_provisional` (duplicate of
`is_provisional`), `imp_cost_exc`, `imp_rate_exc`, `standing_charge_exc`, `exc_source`,
`imp_kwh_ev`, `imp_cost_ev`, `imp_rate_ev`, `imp_ev_band`, `imp_home_band`.

> These are exactly the columns whose drift against segments caused the recurring bug class. They
> are dropped *because* `block_segments` is carried verbatim — the information is not lost, only
> de-duplicated. **Dependency:** every v5 surface must read segments first (BL-27) before this is
> safe.

**CARRY+REMAP — band vocabulary.** `imp_ev_band` / `imp_home_band` are dropped as columns, but the
*words* in them survive in `block_segments.band`. v4 uses four words, and they are **not** four names
for two bands. They are a **2×2 of attribution × band**:

| | low | high |
|---|---|---|
| **house** | `off_peak` ⚠️ *should be `night`* | `day` |
| **EV / device** | `off_peak` | `peak` |

plus `standard` — the `pricing_segments` fallback, meaning **unknown**, not a band at all.

Under IOG-SMB the supplier states this 2×2 explicitly, as four rate buckets:
`CONSUMPTION_CHARGE_ECO7_DAY_x` · `ECO7_NIGHT_x` · `EV_DEVICE_PEAK_x` · `EV_DEVICE_OFF_PEAK_x`
(`x` = region letter). House = Σ `ECO7_*`, EV = Σ `EV_DEVICE_*`; the band is whichever bucket in the
pair carries a non-zero `value`.

**The two axes are different concepts, not synonyms.** House `day`/`night` is **clock-determined** —
Economy-7, the same boundary every day regardless of what the car does. EV `peak`/`off_peak` is
**dispatch-determined** — a smart-charge slot is off-peak, and an EV drawing outside one is charged
at peak. They coincide in being "cheap" and "expensive", and nothing else. Collapsing them would
lose the ability to say *why* a half-hour was cheap, which is the question every billing surface
exists to answer.

### The one remap: house low, SMB era only

**v5 keeps all four labels.** The importer performs exactly one relabel:

```
block_segments.band  'off_peak' → 'night'   WHERE attribution = house
                                              AND the covering agreement is IOG-SMB
```

`day`, `peak` and EV `off_peak` are carried unchanged. `standard` is carried unchanged as an
explicit unknown.

**Why the era scope is principled, not convenient.** Pre-SMB there is no `ECO7_NIGHT` bucket. In the
legacy Intelligent era a house half-hour is low *because a dispatch discounted the whole slot* —
that is the dispatch sense of off-peak, and `off_peak` is the correct word for it. The vocabulary
differs by era because the **tariff mechanism** differs by era. A blanket rename would be the same
category error as the `day → peak` rule it replaces.

**Why this is a remap and not a heal.** A heal repairs damage — data that was wrong when written, or
that went stale against its source — and therefore needs a marker, a completion check and a level,
because you must be able to tell whether it ran. This is a **schema translation**: it happens exactly
once, in a one-directional importer, and the v5 schema simply does not contain the old value on the
house axis. It sits beside `rate_source 'measured' → 'settled'` above, which is the same operation
and has never been called a heal. It must be expressed **declaratively**, as a row in the importer's
column-rule table, not as a branch in domain code.

**The alternative was more code, not less.** Carrying `off_peak` through would leave the same tariff
era labelled two ways depending on which side of the migration a row landed — imported rows saying
`off_peak`, natively-derived rows saying `night`. Every reader (rate line, settlement labelling,
billing split) would then have to accept both, permanently, in the hot path. The remap moves that
cost into a module with a stated end of life (§1).

> Do not infer the band from the rate to "improve" a `standard` segment on the way through. A rate
> equal to the day's low bound does not prove the supplier billed it off-peak. Provenance is not
> upgraded in transit (§2). The relabel above is permitted precisely because it changes **no** fact:
> the attribution and the band are both already recorded, and only the word for one cell of the 2×2
> changes.

**Verification obligation.** Assert that the v5 segment-derived EV/house split and ex-VAT figures
reproduce the dropped columns' values (within rounding) for **every block**, not a sample — see §9
gate 5. A mismatch means BL-27 is incomplete, not that the data is bad.

### 5.3 Dispatch and attribution

| Table | Rows | Disposition | Notes |
|---|---|---|---|
| `dispatch_slots` | 826 | **CARRY** | Irreplaceable beyond Octopus's ~90-day window. |
| `dispatch_history` | 1,493 | **CARRY** | The planned/started/completed lifecycle — the basis of every smart-vs-bump decision. |
| `historical_derivation` | 4 | **CARRY** | Device-attribution ledger; makes attribution reversible. |
| `deleted_ranges` | 0 | **CARRY** | **Critical even when empty.** Tombstones for deliberate deletes; dropping them makes the new app re-create deleted data. |

### 5.4 Telemetry and recorder series

| Table | Rows | Disposition | Notes |
|---|---|---|---|
| `sub_meter_history` | 33,540 | **CARRY** | Device SoC / inverter series used by attribution. |
| `power_history` | 16,770 | **CARRY** | Live-power series. Large; see §8 on chunking. |
| `mix_history` | 1,719 | **CARRY** | Rolling generation mix. Could be re-fetched, but only for the recent window — carry. |
| `carbon_intensity` | 287 | **CARRY** | Per-postcode intensity; block `carbon_g` already stamped, this is the lookup history. |
| `generation_mix` | 774 | **CARRY or DERIVE** | Block-stamped mix; superseded by `mix_history` for display (BL-38). **Open question §10.** |

### 5.5 Transient — do not import

| Table | Rows | Disposition | Notes |
|---|---|---|---|
| `current_block` | 1 | **DROP** | In-flight block; the new app forms its own on first tick. |
| `current_reads` | 60 | **DROP** | In-flight read buffer. |
| `reads` | 0 | **DROP** | Empty in the reference DB; raw reads are consumed into blocks. Confirm before finalising. |

---

## 6. `store_meta` and `kraken_state`

These hold one-time markers, and **most must not be carried** — a stale "done" flag in the new app
suppresses a pass that should run. Default is **DROP**; carry only by explicit allow-list.

**CARRY:**

- `vat_calendar` — irreplaceable local calendar (`[["2023-06-02", 0.05]]` in the reference DB).
- `settings` — user preferences (e.g. `{"bill_rounding_summary": true}`).
- `recorder_attribution_runs` — the attribution back-out ledger; pairs with `historical_derivation`.
- `export_settlement_expected` — whether the account has an outgoing agreement.

**REGEN:** `db_uuid` — the v5 database is a new identity. Record the *source* uuid in the import
audit record (§9) for traceability.

**DROP, but record in the audit trail: `heal_level` and `written_by`.** These are the §3 gate's
input and they must **not** cross. v4's `heal_level` describes v4's heal set; v5 starts from an
empty one and computes its own level from its own invariants. Carrying the number would be the
purest form of the mistake this section exists to prevent — importing a "this already ran" claim
about passes the new app has never run. The two values are written into the §9 audit record instead,
where they answer "what produced this history" without suppressing anything.

**DROP (one-time heal / sweep markers — must not suppress v5 passes):** `reprice_history_state`
(no longer read at all — the §3 gate reads `heal_level`), `carbon_backfill_state`,
`carbon_backfill_done`,
`exc_backfill_state`, `iog_split_backfill_state`, `stale_exc_repair_state`,
`stale_iog_split_repair_state`, `region_probe_clobber_done`, `export_zero_fill_v1`,
`import_reprice_queue`, `region_reconcile_pending`, `schema_version`.

**`kraken_state` — DROP by default.** All 21 keys are runtime/sweep state
(`smb_rate_repair_done_v2`, `smb_ev_resplit_done`, `smb_device_recost_done`,
`verify_pricing_cursor`, `import_health_summary`, `last_poll_utc`, …). The two worth considering:

- `kraken_account_number` — **CARRY**, it is the account stamp (§7).
- `data_source_mode` — **CARRY** (Q3, closed 19 Sep 2026). The new install starts in the user's
  actual mode (cad / api / api+mini) rather than re-running the survey. From the user's point of
  view moving to EMT is an upgrade, and their hardware did not change on the way through.

  *Confirmed in 4.5.13:* `_MODE_KEY = "data_source_mode"` sits in `kraken_state` beside
  `_ACCOUNT_KEY` (engine.py:227, :235) and is set through `set_data_source_mode()` off an API
  endpoint. It is a **persisted setting**, not a runtime probe result — which is what makes carrying
  it meaningful rather than a guess.

  > **Carried, but not assumed serviceable.** The no-credentials import paths (§3) can land a
  > carried mode of `api` or `api+mini` on an instance with no key — deliberately, on the diagnostic
  > path. v5 must treat that as *mode as recorded, inactive until credentials exist*: keep the value,
  > do not poll, do not silently rewrite it to `cad`. Rewriting loses the user's real setting;
  > polling without a key fails on a loop. v4 already distinguishes these — `mode_uses_api()` and
  > `has_kraken_credentials()` are separate questions — and v5 keeps them separate.

> Rule of thumb: if a key's purpose is "this sweep already ran", it must **not** cross — the v5
> equivalent sweeps have not run. `data_source_mode` is on the other side of that line: it describes
> the user's **setup**, not work already done, so it crosses.

---

## 7. Account identity and multi-instance

Multi-property users run several instances via the repo-URL workaround; **this must keep working
in v5**, so the importer cannot assume a single global database.

- **Account stamp.** Reuse the existing guard (#357): a database is stamped to its account on first
  discovery, and **v4 already implements this whole guard** — `get_db_account()` reads the stamp,
  `kraken_account_mismatch()` compares it against the configured credentials. v5 reuses the
  behaviour rather than reinventing it: on a mismatch the history imports, the account is **not**
  associated, the API is **not** auto-activated, and the user is told — "this database belongs to
  account A…7; these credentials are for A…3" — with an explicit reconnect available if they meant
  it. This is §3's phase 2, the only gate that needs credentials.
- **Instance identity.** Carry `instance_name`; do not carry backup-directory ownership — the new
  instance negotiates its own share directory on first run (`resolve_backup_dir` already handles
  "ideal dir taken by another instance").
- **One import per instance.** The importer targets the instance it runs in. Importing property B's
  database into property A's instance is the failure this guard exists to prevent.

---

## 8. Mechanics

**Source = an uploaded backup/export file, not a live database.** Reasons: it cannot race a running
old instance; it is reproducible (the same file imports identically); it is testable; and the user
keeps a working old add-on throughout. Importing from a live path would require stopping the old
add-on and would make failures hard to reason about.

**Idempotent and re-runnable.** Re-importing the same file over the same target must converge, not
duplicate — key on (`block_start`, `meter_id`) for blocks and the natural keys elsewhere. A user who
retries after a failure must not end up with doubled history.

**Bounded and observable.** 135k blocks + 37k segments + 33k sub-meter rows is a large but
tractable import. Chunk it, report progress, and make it resumable. It is a bounded mutating job —
it must take the BL-50 job lock, not run alongside an import or delete.

**All-or-nothing per run.** On failure, the target is left empty (or rolled back), never partially
populated. A half-imported database that *looks* complete is the worst outcome here.

**Order of operations.** `config_periods` → `meters` → `meter_channels` → `blocks` →
`block_segments` → `measured_cost` → dispatch → attribution/ledger → telemetry → allow-listed meta.

---

## 9. Acceptance — how we know an import is correct

The import is not "done" when it completes; it is done when it **reconciles**. Minimum gates:

1. **Row-count parity** for every CARRY table.
2. **Total-cost parity.** Sum of `imp_cost` / `exp_cost` per billing period matches the source
   exactly. This is the headline invariant — a user's bill history must not move by a penny.
3. **Provenance parity.** Counts by provenance match the source exactly. Reference figures,
   `electricity_main`, 4.5.13 prod-dev (17 Sep 2026):
   `schedule` 57,092 · `settled` 457 · `reconciled` 12 · `corrected` 3. Note these move between
   snapshots as settlement proceeds — the gate is *parity with the source presented*, never a match
   against a figure written here.
4. **Corrections preserved.** Every `rate_corrected=1` block (33 in the reference DB) survives with
   its corrected rate intact.
5. **Segment reconstruction — every block, not a sample.** The dropped legacy columns (§5.2)
   reproduce from segments within rounding for **100%** of imported blocks. This is the gate that
   makes the legacy-column removal safe, and a sample tests the premise weakly; the import is a
   one-shot offline job with the whole database to hand, and the reference set is 78 MB, so there is
   no budget argument for sampling. Report the worst absolute divergence, not just a pass/fail —
   a drift that is within tolerance but trending is the early warning.

   *Method already proven:* the same comparison run across every measured block on 4.5.13 prod-dev
   returned 0 mismatches. Compare at **raw stored precision**; do not compare rounded display
   values, which is how several false results were reached during 4.5.13 development.
6. **Tombstones honoured.** After import, a backfill pass must not re-create any range in
   `deleted_ranges`.
7. **An import audit record** is written: source `db_uuid`, source `written_by` and `heal_level`
   (§6 — recorded here precisely because they must not cross as live markers), row counts in/out,
   and the reconciliation results above.

**Test corpus — and note the floor gives us a much better negative set.** The real databases already
on hand:

| database | expected | why |
|---|---|---|
| 4.3.1 | **refused** | no `heal_level`, and no `block_segments` either |
| 4.5.12 production | **refused** | no `heal_level`. Previously this was a *positive* case. |
| 4.5.13 prod-dev | **refused** | no `heal_level`, despite being one release short |
| at `REQUIRED_HEAL_LEVEL` | **admitted** | the only positive case |
| **below** `REQUIRED_HEAL_LEVEL` | **refused** | the marker is present and the bar has moved past it |

Two of these carry most of the value.

**The 4.5.13 near-miss** is the case the gate exists for: every table and column the importer wants,
and still inadmissible because nothing has attested the heals hold. Under the old three-gate design
it would have passed. Keep a .13 database as a permanent negative fixture.

**The below-the-bar case cannot be a fixed file**, because the bar moves (§1.1, Q9). Generate it —
take the positive fixture and decrement its `heal_level` — so the test stays honest whatever
`REQUIRED_HEAL_LEVEL` is pinned to. A test that hard-codes "level 8 is admissible" silently stops
testing anything the day v4 reaches 9.

> **The positive fixture is a release-day artefact.** It must be regenerated from a v4 install at
> the final high-water mark, not carried forward from whatever .14 produced during development.

---

## 10. Open questions

1. ~~**Versioning/naming.**~~ **Closed (17 Sep 2026).** Public releases use CalVer `YYYY.M.PATCH`
   (`2026.x.x`), matching Home Assistant's own convention; **`v5` is the internal discriminator for
   everything before the first public release** and never appears in a shipped version string. The
   slug change carries the breaking-change signal that semver would otherwise provide, once.
   *Carry-over:* EMT's own upgrade detector compares the stored version to the running one to arm
   the re-price sweep and the upgrade backup — it must order `2026.9.1 < 2026.10.0` correctly. A
   fresh install has no stored version so the first release is safe; the second is where a naive
   comparator would bite.
2. ~~**`generation_mix`**~~ **Closed 19 Sep 2026: CARRY.** Block-stamped, 774 rows. BL-38 moved the
   donut to `mix_history`, so it is superseded *for display* — but "superseded for one surface" is
   not "reconstructable". A mix stamped onto a block at the moment that block formed cannot be
   re-derived from a rolling series if the two ever disagreed, and §2's invariant is carry what
   cannot be re-fetched. Against 774 rows, dropping it would be a space argument, which is precisely
   the reasoning [the schema notes](EMT-v5_schema_notes.md) §2 rejects.

   *If it really is dead, it gets dropped by the same method as everything else:* the AST
   reachability trace that settles the seven deferred columns (schema notes §5), corroborated across
   every database on hand — not by a table-level guess made here.
3. ~~**`data_source_mode`**~~ **Closed 19 Sep 2026: CARRY** — as the original proposal had it. The
   new install starts in the user's actual mode (cad / api / api+mini) instead of re-running the
   survey. **Moving to EMT is an upgrade from the user's point of view; the meter, the CAD and the
   tariff are all the same on the far side of it.**

   *A DROP argument was floated the same day and is wrong, recorded here so it is not re-proposed.*
   It ran: §3's account gate compares against the target instance's configured credentials, so v5 is
   configured before the import runs and already holds its own mode. **That does not follow.**
   Configured with *credentials* is not the same as having completed *mode discovery* — the account
   stamp needs an account number, whereas the mode needs observation of whether a CAD is actually
   feeding readings. The two are established by different means at different times.

   The argument also invented a risk that existing machinery already handles: a carried mode that no
   longer matches the hardware (a CAD removed between installs) is the same situation as a CAD
   failing on a running v4 install, and v4 already promotes the mode on reconnect. Meanwhile the cost
   of dropping it is real — a window after import in which the new app has no mode and block
   derivation has nothing to work from.

   **The general lesson, since this is the second reclassification to get it backwards:** §6's rule
   of thumb is "does this key say *work already ran*?" `data_source_mode` says *what the user's setup
   is*. Setup facts cross; completion claims do not.
4. ~~**Export format.**~~ **Closed (17 Sep 2026), corrected 19 Sep.** The source is what v4 already
   produces: the **backup zip**, or a **`blocks.db` taken from one**. There is still no purpose-built
   exporter, and the importer validates the source itself rather than trusting a contract (§3).

   *What was wrong:* this answer also asserted "there is no final 4.5.x — 4.5.13 is the last v4 the
   importer will ever see", and drew from that the benefit that "every existing 4.5.13 install is
   already able to migrate, with nothing to ship first". **4.5.14 is shipping**, so nothing is
   already able to migrate — a .13 install must update first. That is a deliberate trade: one
   release of v4 in exchange for a gate that can assert the heals *currently hold* rather than
   inferring it from table presence (§1.1, §3).
5. ~~**Migration window.**~~ **Closed (17 Sep 2026), refined 18–19 Sep.** There is no window and no
   deadline: the two add-ons run side by side in separate containers for as long as the user likes
   (§1). So the importer is not deletable on a *date* — it is deletable when we decide 4.5.14
   sources are no longer supported, which is a product call rather than a migration one. What §1
   now adds is that the decision, whenever it comes, must be **cheap to act on**: E is one module,
   nothing depends on it inward, and no importer-only concept leaks into the domain. An open-ended
   lifetime is acceptable; an un-excisable one is not.
6. ~~**HA entity collision.**~~ **Closed 18 Sep 2026: EMT publishes nothing to Home Assistant.** It
   subscribes to sensors and creates no entities or statistics, so the one shared namespace
   `instance.py` does not arbitrate is never entered. Two add-ons run side by side indefinitely with
   no contention beyond the `/share` backup directory, which `instance.py` already resolves.
7. **`reads`** is empty in the reference DB — confirm it is always transient before DROP.
8. **Attribution state (new, 17 Sep 2026).** The device-history attribution ledger is carried
   (§5.3), but v5 is expected to re-found attribution on a job coordinator. Decide whether an
   attribution run that was *in flight* when the backup was taken is importable at all, or whether
   the gate should refuse it. *Sharpened 19 Sep:* with §3 now resting on a **computed** `heal_level`,
   the cleanest answer may be that no new gate is needed — if an in-flight attribution run leaves an
   invariant broken, the level re-verification should already fail. Worth checking against BL-72's
   actual heal set before adding a separate check.
9. ~~**Does v5's required `heal_level` ever rise?**~~ **Closed 19 Sep 2026, and it was never a
   policy question.** The bar is **v4's final high-water mark**, whatever that turns out to be. It
   rises whenever a post-.14 critical fix adds a heal, and previously-admissible backups do stop
   being admissible — correctly, because v5 has no heals and would otherwise import history v4 knows
   is unrepaired. There is nothing to decide, only something to **supply**: the number is a
   release-day input, not a design-time constant.

   *Action, not an answer:* `REQUIRED_HEAL_LEVEL` is pinned as the last step before v5's first
   release, from whatever v4 has actually reached by then, and §9's positive fixture is regenerated
   at that level. On the release checklist, not in this paragraph.