# EMT v5 — Step 6: the first-run fork

> **History — a design record.** What was decided, when and why, as it stood then; parts describe code that has since changed. How EMT works today: [reference/v4-import-and-first-run.md](../../reference/v4-import-and-first-run.md). PR numbers (`#NNN`) are in the private archive, `RGx01/EMT-archive`.

*How [import spec §3a](../../design/EMT-v5_import_spec.md) is built. The spec says a first start offers new
setup or a restore, and that on the restore path the backup decides which credentials to ask for.
This says **which page, which routes, where the account check runs, and in what order the work
lands**. Where the two disagree the spec wins until it is amended; the one amendment this needs is
§7.*

**Status:** built and run on the Pi, 12 Oct 2026 (§9) — #139–#141, #144, #145. Decided 11 Oct 2026
(§8). Architecture step 6. Three choices were made by the user
before this was written: the fork is its own **welcome page**, the restore becomes **two steps**
(check, then confirm) so the credential prompt can sit between them, and an **account mismatch on
the fork asks and defaults to importing** without connecting.

---

## 0. What exists today, and what is missing

Measured on dev at 538d910.

**A fresh install lands in the wizard.** `GET /` (`web/server.py:index`) sends an instance with no
meters to `/settings`, and `meter_config.html` opens the setup wizard when `config.meters` is empty.
The wizard is about meters: survey (supplier, local readings, API) → credentials → main → sensors →
devices → settlement. Nothing in it mentions a backup.

**A restore is reachable, but only by knowing where to look.** Data Management is always in the nav
and none of its routes checks configuration, so a v4 user *can* restore before setting anything up.
They have to dismiss the wizard to do it, and the page that greets them never says it is possible.

**The restore is one shot.** `POST /api/import` stages the upload, then starts `_restore_worker`
straight away; `_prepare_restore_db` runs phase 1 (`v4import.gate.check`, or gate 1 alone for a v5
backup), builds the v5 database, and the worker swaps it in and reruns `engine_startup`. There is
no point between "the file is admissible" and "the file is live" at which the page could ask for
anything.

**Phase 2 exists, but only after the swap.** The account check is `_kraken_startup_discovery`'s
stamp guard (#357): after a restore, if credentials are already on disk and their account differs
from the database's `kraken_account_number`, the API is not activated and Settings offers a
reconnect. That is the spec §3 table's behaviour for an instance that was *already* connected, and
it stays. What is missing is the fork's version: credentials typed for *this* backup, checked
against *its* stamp before it goes live.

**`/api/import/from-zip` is a second, weaker restore.** It runs in the request thread, takes no
`restore` job claim, swaps with a plain `os.replace`, and does not rerun `engine_startup`. On a
configured instance the next config save hides that. On a first run nothing else restarts the
engine, so a zip restored on a fresh install would sit unpriced until a restart. The fork cannot be
built on it.

**What the backup knows is not exposed.** `v4import.gate.source_supplier` returns the supplier
(stamped, or inferred from the Octopus account stamp); its docstring says it is what the first-run
wizard will prompt for. Nothing calls it except the restore log and the audit record.

---

## 1. The fork: a welcome page

```
GET /   no meters configured, no restore running  ──►  /welcome
        no meters, a restore running              ──►  /welcome (shows its progress)
        meters configured                         ──►  last page, as today
```

`/welcome` is one template with two cards, and nothing else on it:

- **Set up a new account** → `/settings?wizard=1`. The wizard is unchanged; its auto-open on an
  empty config stays, so reaching Settings directly behaves as it does now.
- **Restore from an Energy Meter Tracker backup** → the restore panel on the same page (§2). It
  takes a `.zip` (from either add-on's backup list or the share) or a `blocks.db`.

"Unconfigured" means what it already means to `index`: `load_config()` has no meters, which is no
open config period. A restored backup carries its config periods, so after a restore `/` routes on
as it would for any configured instance. No new flag is stored.

**The nav stays.** Logs and Help are what a user reaches for when a restore is refused, and
hiding the nav would be a second definition of "unconfigured" to keep in step with the first.

**Why not a step 0 in the wizard.** The wizard is ~1,000 lines about meters, and every step
after the survey assumes the user is building a configuration. A restore builds none: the
configuration arrives with the backup. Putting the fork in front of the wizard would mean every
later step learning to be skipped.

---

## 2. The restore, in two steps

```
POST /api/restore/stage      multipart: file (.zip or .db)
   ├─ refuse if a restore is running                         (as /api/import does)
   ├─ save to DATA_DIR/uploaded_restore-*.db (mkstemp)        a zip: blocks.db extracted to it;
   │                                                           a hot WAL in the zip is refused
   ├─ PHASE 1 on the staged file                               §3 gates 1–5, or gate 1 + emt_schema
   │     └─ refused → 400 {error}; the staged file is removed
   └─ 200 {token, facts}
          facts = {kind: 'v4'|'v5', written_by, supplier, supplier_inferred,
                   prompt: 'octopus'|null, account_stamped: bool,
                   blocks: n, first_day, last_day}

POST /api/restore/confirm    json: {token, credentials?: {api_key, account_number?, base_url?},
                                    accept_mismatch?: bool}
   ├─ token names a staged file in DATA_DIR that still exists   (§2.2)
   ├─ credentials given?
   │     ├─ PHASE 2: test them (a throwaway client — nothing saved, nothing polled)
   │     │     ├─ the test fails            → 400 {error}        page: fix the key, or skip
   │     │     ├─ stamp ≠ key's account,
   │     │     │  accept_mismatch not set   → 409 {mismatch: {backup, key}}
   │     │     └─ match (or no stamp)       → save the credentials
   │     └─ accept_mismatch → credentials NOT saved; the import goes ahead unconnected
   └─ start _restore_worker(staged=…) → 200 {status: 'running'}
          the page polls /api/backup/restore/status, as Data Management does
```

### 2.1 What the prompt is keyed on

`facts.prompt` is the supplier whose credentials to ask for, or null for none:

| the backup says | `prompt` | the page |
|---|---|---|
| `octopus`, stamped or inferred | `octopus` | the Kraken key and account, **Skip** offered |
| `not-listed` | null | no prompt — local meter history, never priced by an API |
| no supplier, no stamp (never asked) | null | no prompt |

The rows are spec §3 gate 3's. An unsupported supplier never reaches this table — phase 1 refuses
it. `prompt` is the supplier key itself, so a second API-capable supplier adds a row and its own
prompt shape; the routes do not change.

**Where the facts are read.** In `web/server.py`, from the staged file, the same way for both
kinds: `store_meta.supplier`, and where it is absent but `kraken_state.kraken_account_number` is
set, Octopus — the only API EMT has had (spec §3, as `v4import.gate.source_supplier` reads it).
Either kind can lack the supplier stamp: v5 writes it only when its config names a supplier, and
real v4 Octopus installs have it empty. Reading it here means a v5 backup's facts take nothing
v4-shaped. (Its gate 1, `gate.check_intact_file`, does still live in `v4import`; it is the one
piece that moves out when the package retires.)

### 2.2 The token

The staged file's basename, `uploaded_restore-XXXXXXXX.db`. `confirm` accepts it only when it
matches that pattern and the file is in `DATA_DIR` (`_in_data_dir`), so a token can name nothing
else. A stage that is never confirmed leaves the file behind; the next `stage` removes any
`uploaded_restore-*.db` older than an hour, and the web server's start removes them all (only the
process that staged a file can confirm it). One restore runs at a time, so at most one
staged file is live.

`confirm` does not run phase 1 again: the staged file is EMT's own, in its data directory, and
`_prepare_restore_db` gets a `checked=True` that skips `gate.check` (the importer's own gates still
run). On the Pi, `quick_check` of an 80 MB database is the cost being saved.

### 2.3 Phase 2 without side effects

The check builds a `KrakenAPIClient` from the typed key, calls `test_connection` (one account
query; never raises), and closes it. **The account it opens is the typed one, or — left blank — the
one the backup records** (`kraken_state.kraken_account_number`). Octopus cannot name the account
behind a key; a key is only tested by opening an account (#144, from the Pi run: blank was sent as
none, and every key failed). A key that cannot open the backup's account is refused with that
account named, masked, so a user whose key is for another account knows to type its number — and a
typed account that differs from the stamp is the mismatch of §2.4. A backup with no stamp needs the
number typed. It does not call `save_kraken_credentials` or
`connect_kraken_now`, because both act on the **live** database: discovery would stamp the empty
first-run database with the account and start the poll task, and the restore would then have to
cancel an API import it caused (`_restore_worker` step 1b).

Only on a match are the credentials saved — before the worker starts, so the worker's own
`engine_startup` finds them, sees the restored stamp agree, and activates the API in the ordinary
way. Nothing new runs after the swap.

The comparison is `engine._norm_account` (strip, upper case) on both sides. Where the backup has no
stamp there is nothing to compare: the key is saved and the first discovery stamps the restored
database, as it would any unstamped one.

One case activates nothing: credentials that match, on a backup whose `data_source_mode` does not
use the API (an Octopus account on local readings only). The key is saved and the mode is the
backup's; Settings shows the connection as available, not active. That is the backup's choice and
the fork does not override it.

### 2.4 The mismatch

A 409 is not a refusal. The page says which account the backup belongs to and which the key is for
— both masked as the logs mask them, first and last character (`kraken_api_client._mask`) — and offers:

- **Import without connecting** (the default button) → `confirm` with `accept_mismatch`. The
  history imports; the key is discarded; no account is associated. Settings then shows "no
  credentials" and its reconnect, as for a skip.
- **Enter a different key** → back to the prompt.

This is the spec §3 table's rule — a mismatch imports and does not associate — with one difference
from the configured path: here the key was typed for this restore and is not kept, because keeping
it would leave the instance holding credentials for an account its data does not belong to.

### 2.5 Data Management moves onto the same routes

Its `.db` and `.zip` drops call `stage` then `confirm` with no credentials: the instance is
configured, so whatever credentials are on disk already meet the restored stamp in discovery, as
today. `/api/import` and `/api/import/from-zip` are deleted, and with them the second restore path
(§0). The share and backup-list restores (`/api/backup/restore`) stay as they are; they already run
the worker.

---

## 3. After the restore

The worker reruns `engine_startup`, which is what makes the history live. The page then goes to
`/`, and `index` routes on the restored configuration:

| state after the restore | where the user lands |
|---|---|
| credentials saved, matched | Charts — costing resumes on the next poll |
| skipped, or a mismatch accepted, on an API mode | Settings, whose existing "API mode is set, but no API credentials are stored" banner and **Re-enter API key** are the hand-off to account setup the spec asks for |
| no supplier, or `not-listed` | Charts |

So "hand off to account setup" (spec §3a, last box) needs no new screen. The welcome page links
to Settings when `/api/kraken-config` reports an API mode and no credentials.

---

## 4. Copy

Every refusal is phase 1's own message, unchanged — they already name what to do ("open this in
the last v4 release first"). Two strings are new:

- the prompt's header: **"This backup is from an Octopus account. Connect it to keep costing
  live, or skip and connect later — your history imports either way."**
- the mismatch: **"This backup belongs to account A…3, and that key is for A…6."**

---

## 5. Tests

- **Routes.** `stage` refuses while a restore runs, refuses a non-database, refuses below the floor
  and removes the staged file; returns `prompt` per §2.1's table, from fixtures for each row and
  for a v5 backup. `confirm` refuses a token outside the pattern and a missing file; saves nothing
  on a mismatch; saves on a match; starts the worker in every accepted case. The Kraken client is
  faked — no network.
- **The first-run landing.** `/` → `/welcome` with no meters; to the last page with meters.
- **One restore path.** No route other than `confirm` and `/api/backup/restore` starts a swap —
  structural, as `test_upload_reply` is for the uploads. Fails on the parent (`from-zip` swaps).
- **The page.** Every upload on it reads its reply through `uploadReply` (the existing test's
  rule, extended to `welcome.html`).
- **The zip on a first run reruns the engine** — fails on the parent.

---

## 6. Order of work

| PR | contents |
|---|---|
| 1 | this document; spec §3a's amendment (§7) |
| 2 | `stage` / `confirm`, phase 2, the facts reader, stale-stage cleanup; Data Management onto them; `/api/import` and `/api/import/from-zip` deleted |
| 3 | `/welcome`, `index`'s redirect, the restore panel and its prompt |
| 4 | a Pi run of both paths from a fresh data directory: a 4.5.23 zip with the right key, with Skip, and with a wrong key; and "new account" through to the first priced block. *Run 12 Oct 2026 — §9; its fixes are #144 and #145.* |

PR 2 is usable on its own — it is the restore fix — and PR 3 adds only the page.

---

## 7. Spec amendment

Import spec §3a, "Every path ends with the history imported": *"The only outcomes that stop an
import are the phase 1 refusals … and a genuine account mismatch."* The §3 table says a mismatch
imports and does not associate the account, and that is what both paths do. Amend §3a to: the only
outcomes that stop an import are the phase 1 refusals; a mismatch asks, and imports without
connecting unless the user goes back to enter another key.

---

## 8. Decided

1. **A welcome page, not a wizard step** (11 Oct 2026). §1.
2. **Two-step restore** (11 Oct 2026): check, prompt, confirm; one restore path. §2.
3. **A mismatch on the fork asks and defaults to importing unconnected**, and the typed key is
   not kept (11 Oct 2026). §2.4, §7.

---

## 9. The Pi run, 12 Oct 2026

PR 4. `local_emt_soak` on the Pi (SD card) at 4.5.23-a24, each test from an empty data directory
(Settings → delete the main meter), through `:8098`. The backups were the dev install's 4.5.23
backup — a zip, a `.db`, and a copy with its account stamp changed to `A-0000000X` — and a v5
backup (`4.5.22-a23`, emt_schema 5). Every one passed phase 1 on the Mac first.

| test | result |
|---|---|
| 1. a made-up key, then the right one (zip) | made-up: refused, naming the backup's account (masked). Right key, account left blank: matched and saved; imported in 30.2 s (commit 21.2); connected (`kraken_discovery: account verified`) and polling, `errors=0`; Charts. Click to Charts: 33 s |
| 2. Skip (`.db`) | nothing tested or saved; imported in 30.5 s; `no API key configured`; Settings, with the "no API credentials are stored" banner — the hand-off of §3 |
| 3. a key for another account | blank account: refused against `A…X`. Account typed: the mismatch box (`A…X` / `A…7`); "Import without connecting": imported, the key not kept (no "saved" line), Settings with the banner. "Enter a different key" was checked in the browser pane against the real page: the prompt returns, focused, with what was typed |
| 4. a v5 backup | "A backup from Energy Meter Tracker 4.5.22-a23"; `a v5 backup (emt_schema 5)`, swapped in with no import; key matched; connected. Click to done: 16 s |
| 5. Set up a new account | `/welcome` → the wizard; the credentials step connected, the mode went `unset` → `api`, config saved, the poll started, and the first half-hour finalised at the next boundary. Not followed to Octopus's settled cost the next day: block settlement has been proven by weeks of soaks, and nothing here changes it |

**Findings.**

1. *Fixed, #144:* a blank account number reached the key check as no account at all, and every key
   failed (`account_number is required for get_account`) — §2.3.
2. *Fixed, #144:* deleting the main meter went back to `/settings`, whose wizard opens on an empty
   install, past the fork. It goes to `/`.
3. *Fixed, #145:* nothing showed while the reset ran. Since BL-6 Settings' notices go through the
   notification region and its alert divs stay hidden; ten places still wrote to them directly, so
   the reset's progress and every failure of a reset, delete, disconnect or reactivate were
   invisible. v4 has the same; display only, so it stays.
4. *Fixed, #145:* finishing the wizard drew the page before the mode was saved — "API is
   configured but inactive … Data source: unset" until the next refresh. v4 the same, likewise.
5. *Watch:* one import's "prepare" took 20.0 s (1.0 and 1.4 s in the others), after the page had
   sat for 4½ minutes; and one restore spent 15 s between "taking a safety backup" and the WAL
   checkpoint (1 s in test 1). Single SD-card stalls in otherwise ordinary runs; not acted on
   unless one repeats.

**Timing.** Staging took 6.4 s and 0.6 s for the two `.db` uploads (35 and 42 MB), and 29.3 s and
16.5 s for the two 5 MB zips — a zip is written, then its 35 MB database extracted and synced, on
the SD card. A v4 import is ~30 s, two thirds of it the commit — the
same shape as the E design's soaks (§8a). The 5 MB zip's upload was too quick for the progress line
to show; the 35 MB `.db`'s showed it.

## The only way in before setup (19 Oct 2026)

Found setting up staging: only `/` sent an unconfigured install to `/welcome`, so a user who opened
Charts or any other page instead of following the welcome screen could not get back to it. Now every
page redirects to `/welcome` until there is a configuration (`server._first_run_guard`), except the
fork's own pages — Settings, for the setup wizard and for a restore that lands without a key — and
Help and Logs. APIs, static files and the Lovelace cards are not touched, and a failed config read
never locks the user out. The sidebar dims the links that would bounce.
