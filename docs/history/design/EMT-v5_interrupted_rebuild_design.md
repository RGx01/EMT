# EMT v5 — a device history rebuild that is cut off

> **History — a design record.** What was decided, when and why, as it stood then; parts describe code
> that has since changed. How it works today: [reference/data-management-and-devices.md](../../reference/data-management-and-devices.md)
> and the code. PR numbers (`#NNN`) are in this repository, `RGx01/EMT`: Phase 2 #211, Phase 1 #212,
> Phase 3 the PR after.

*Status: decided 21 Nov 2026 (the user: "write the design note and start Phase 2"). Phase 2 built
21 Nov 2026 (`_restore_worker` step 1c, `engine.database_replaced`); Phase 1 built 21 Nov 2026
(the ledger run from its start, `mark_cut_off_rebuilds`, the card); Phase 3 built 21 Nov 2026
(`_rebuild_refuses_backup`, the daily copy's wait). All three built: the note moved to history. Evidence from the owner's soak Pi on `dev`, 21 Nov 2026.*

---

## 1. The question

The roadmap's open question was *"a backup taken mid-attribution — does a database with a device
rebuild part-done import wrongly?"* The user's first answer was to refuse backups while a rebuild
runs. That closes one route of four. The same half-done database comes from:

1. **a restart or a power cut** mid-rebuild — the run's progress lives only in memory
   (`_attribution_jobs`), and its ledger entry is written when it finishes;
2. **Home Assistant's own backups** — `config.yaml` sets no `backup:` mode, so HA takes them hot,
   copying `/data` whenever its schedule says;
3. **EMT's own backups** — *Backup now*, the daily copy, and a restore's safety backup;
4. **a restore while a rebuild runs** — found while planning this (§2, T3).

So the question became: is a half-done rebuild recoverable, and does EMT say so?

## 2. What the soak showed

Four runs on the `ev_charger` device, about 30,000 rebuilt half-hours, each compared half-hour by
half-hour with the database taken before them.

| Run | Cut off | Afterwards |
|---|---|---|
| T1 | restart 18 s into *Rebuild history* | nothing to test: the device had all its history, so the run only skipped |
| T2 | restart 30 s into *Replace history*'s writing | the card showed a partial span (about a quarter); *Rebuild history* finished it |
| T2a | restart during *Replace history*'s "Giving the house back its share" | the old history dropped, the re-split part done; a second Replace dropped nothing and so skipped the re-split; a restart mid-write, then *Rebuild history* |
| T3 | restore of the pre-replace backup during *Replace history*'s writing | the restored database exactly the backup's; the card wrong (below) |

**Recovery is exact.** After T2, and after T2a with T2b, every device, house-battery and meter
figure in the span matched the database before them (one battery half-hour differed by 0.00001
kWh, the split's whole-Wh rounding). The recorder hours are kept before anything is dropped
(`replace_device_history` step 1), and the rebuild re-splits each half-hour it gives the device
(`_write_device_into_block` runs `_apply_pass2`): a half-hour it does not touch is one where the
device drew nothing, whose split does not depend on it.

**What is wrong:**

- **Nothing says a rebuild was cut off.** After T2a the card showed no rebuilt history; after T2 a
  partial span. Both read as a finished state.
- **The undo list misses a cut-off run.** *Replace history* removes the device's ledger entries
  before it writes, and a run is entered only when it finishes, so after T2b the ledger held one
  entry for the last run's share. The card's *Remove* deletes by device (`{meter_id}`), so it still
  clears everything; undo by run would not.
- **A restore does not stop a rebuild** (T3). The rebuild waits only for an import or a delete
  (`job_coordinator`: `"attribution": (("import",), ("delete",), WAIT)`), so through the restore's
  13 s of checking and safety backup it kept writing to the old database — the safety backup is a
  mid-rebuild copy. It then failed — "BlockStore not initialised" — because it asked for the store
  between the restore closing it and reopening it. **That was timing, not design:** closing and
  reopening both run on the engine loop, the rebuild between them, waking every 200 hours after a
  0.05 s pause. With a file copy quicker than that pause, the rebuild could wake after the reopen and
  write the old run's device rows into the restored database.
- **The card after a restore.** It kept the progress it last fetched (it polls only while a run is
  active) and showed the dead run's error — "Rebuild error: Something went wrong (attribution, …)" —
  from in-memory state a restore does not clear, as if the restored data were broken.

## 3. Decided

### Phase 2 — a restore stops a rebuild first (built first: the one with a data-integrity edge)

- **The restore cancels any rebuild or replace and waits for it to stop** before its safety backup,
  as it already does for an import (Step 1b); one that will not stop aborts the restore, saying so.
- **A replace checks for cancel** between its re-split chunks and before its drop, so it stops in
  seconds, not after its full pass.
- **A backstop on the store itself:** the engine counts store resets (`reset_store`), and a rebuild
  that finds the count moved stops — "the database was replaced" — whatever the timing.
- **The restored database starts clean:** finished runs' in-memory state is cleared on a restore, so
  the card shows the restored history and no other database's error.

### Phase 1 — a cut-off rebuild is visible

- **A durable run record**, written when the run starts, its kind (rebuild, replace, refill window),
  span and step, updated with its progress (every 200 hours, where the in-memory progress already
  is) and closed done, cancelled or failed.
- **On startup** a record still open was cut off: the card says so — *"Replacing history stopped at
  ⟨date⟩; Rebuild history finishes it"* — and the undo list covers what the run wrote.
- **As built:** the record is the ledger run itself (`recorder_attribution_runs`), entered when the run
  starts with `status`, `kind` (rebuild, replace, refill), `step` (dropping, re-splitting, writing),
  `done_to`. A replace stopped after its drop closes `interrupted`, not `cancelled`: its history is
  dropped. A run that finishes having written nothing leaves the ledger, as before.
- **No new resume path.** *Rebuild history* already finishes a cut-off run exactly (§2).

### Phase 3 — EMT's own backups are whole

- *Backup now* is refused while a rebuild runs, saying which device.
- The daily copy waits for it to end, and is taken anyway after a few hours, so a stuck run cannot
  stop backups.
- **As built:** `/api/backup` and `/api/backup/create` answer 409 `rebuild_running`, naming the device
  (the restore page's pre-restore copy is non-fatal; the restore stops the rebuild and takes its own).
  The daily copy already runs only in two 10-minute windows from 03:15 and 03:45 and skips busy ones; a
  rebuild is busy while the last copy is under 48 hours old, so a stuck or long-paused rebuild costs at
  most a night's copy. EMT's own pre-change backups are not refused: they are taken before the run.

## 4. Rejected

- **Refusing backups alone** — closes one route of four (§1).
- **Declaring `restore` in the rebuild's `exclusive_with`.** The coordinator's answer to a conflict
  is to make the rebuild wait (`WAIT`), and a rebuild that waits through a restore resumes writing
  the old run into the restored database — the failure, made certain.
- **A new resume mechanism** — *Rebuild history* is one (§2).
