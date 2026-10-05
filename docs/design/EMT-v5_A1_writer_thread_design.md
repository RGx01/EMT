# EMT v5 — A.1: the engine loop must not wait on the SD card

*Design, 5 Oct 2026. ROADMAP §A.1. Not yet built: step 0 is a measurement, and it may change
the plan.*

---

## 1. The problem, measured

The engine runs on one asyncio loop. Every database write it makes is synchronous **on that loop**,
so while a commit waits on the device, nothing else on the loop runs: not the live tick, not the
HA WebSocket's keepalive, not the hops web routes make to the engine. One such stall dropped the HA
WebSocket (ROADMAP §A.1, fifth soak).

Sixteen soaks have taken the stall count down by removing *how many* commits there are:

| soak | change | over-budget lags | slow commits | worst lag |
|---|---|---|---|---|
| 5 | — | 80 (over 1 s) | — | 52.7 s |
| 6 | pre-rendered charts deleted (C) | — | 83 | 22.3 s |
| 12 | share copy daily (#68), carbon tick one transaction (#66) | quiet phase passes | — | 12.9 s (sweep) |
| 15 | dispatch poll writes only what changed (#91) | 59 | 97 | 18.5 s |
| **16** | **sweep commits once per chunk (#95)** | **10** | **11** | **18.3 s** |

Soak 16 is the turning point. Each remaining slow commit is **one** commit: a sweep chunk's single
`batch` commit (2–5.5 s, seven of them), and the half-hour finalise's `save_current_block` (10.4 s)
and `_insert_block_rows` (8.2 s). There is nothing left to batch. A single commit that takes 10 s
cannot be made faster by committing less often.

### What the time is spent on

The database is WAL with `synchronous = NORMAL` (`block_store._apply_pragmas`). In that mode a
commit appends frames to the WAL and **does not fsync**; only a checkpoint syncs. In soak 16, 10 of
the 11 slow commits had no checkpoint inside them. So the likeliest cost is the commit's `write()`
itself, blocked by the kernel while the card drains dirty pages — and the card is shared: Home
Assistant's recorder writes to it, and so does **a second EMT instance (v4 4.5.20 dev) on the same
Pi**. This is an inference from the pragma and the log, not yet a measurement. Step 0 tests it.

## 2. What must not change

The fix may move *where* commits happen. It must keep every property the earlier workstreams paid
for:

1. **One writer connection.** SQLite allows one writer at a time; the engine is that writer, and
   web routes ask it to write (`_engine_write`, `_config_mutation_on_loop`). B §4 pinned the
   request-thread writers to one (`api_db_vacuum`).
2. **Thread affinity.** Connections are opened `check_same_thread=True` (A acceptance 5): whichever
   thread owns the write connection is the only thread that touches it.
3. **Per-block atomicity.** A block's read-modify-write runs with no interleaving (B §1, §3.4;
   `tests/test_b_per_block_no_admission.py`). Today that holds because it is synchronous code with
   no `await`. It must hold by construction after the change too.
4. **Admission at job start and chunk boundaries only** (B §3.4). The coordinator stays on the
   loop.
5. **Readers unchanged.** Request threads read through their own read-only connections
   (`store_access.store()`); nothing in this design touches them.

## 3. Options

**(a) A writer thread that runs whole write units — proposed.** One dedicated thread owns the
engine's write connection. Engine code hands it a *unit* — a synchronous function that does a
complete piece of work: a block's finalise, a sweep chunk, a dispatch poll's batch — and `await`s
the result. The unit runs start to finish on the writer, so per-block atomicity is the unit's
boundary, exactly as the synchronous code's is today. Units run one at a time, first in, first out,
so order is preserved. While one runs, the loop is free: the WebSocket, the tick's non-database
work, other coroutines.

**(b) Hand only the COMMIT to a thread.** Rejected: a connection is busy while it commits. The loop
could not issue its next statement, so any other database work on the loop would block on the
connection anyway, and thread affinity forbids touching it from two threads at all.

**(c) An async SQLite library (`aiosqlite`).** Rejected for now: underneath it is (a) — a thread
per connection — but at the granularity of each statement rather than each unit, so per-block
atomicity would become a matter of convention again. A new dependency for a worse boundary.

**(d) Reduce the I/O instead.** Complementary, not a substitute: storage advice (USB or SSD rather
than SD), a larger `wal_autocheckpoint` so checkpoints are rarer. Neither stops a slow write from
holding the loop.

## 4. Plan

### Step 0 — measure first

Before building anything, establish what a slow commit is waiting on:

1. **Stop the v4 dev add-on and re-run the soak** (same restore of `~/soak-db-4520`). If the
   finalise commits drop well under 2 s, contention between two EMT instances on one card is most
   of the cost — worth knowing before building a thread to hide it.
2. **Split each slow commit's time** into WAL write and checkpoint: log `PRAGMA wal_checkpoint`'s
   own duration separately when it runs, and the commit's duration without it. The existing
   `slow commit … CHECKPOINT` tag already shows which commits included one.

If step 0 shows the stalls vanish without the second instance, the writer thread is still worth
building — a household Pi also runs HA's recorder on the same card — but its priority drops.

### Step 1 — the writer

- A `WriterThread`: one thread, owning the engine's `WriteStore` (opened on that thread), with
  `await run_write(unit)`. Exceptions propagate to the awaiting coroutine. `store_access.writer()`'s
  affinity check moves to the writer thread.
- **A ratchet**, as A's was: a structural tool counts engine code that touches `_store` on the loop
  thread outside a unit. It may only fall. Test fixtures keep a synchronous path (the writer runs
  units inline when no thread is started), so the suite's direct `engine._store` use keeps working.

### Step 2 — the slow units first, from the soak data

1. `finalise_block` — `save_current_block` and `_insert_block_rows` (the 10.4 s and 8.2 s);
2. the sweep's chunk — already one `batch()`, so it is a unit as it stands;
3. the dispatch poll's batch, the carbon tick's write, the settlement / Kraken poll upserts;
4. the marker writes (`set_kraken_state`, `set_meta`) — 45 call sites, mostly inside the above.

### Step 3 — the rest, and the reads

Engine code that reads and then writes must put both in one unit. Engine reads that do not need to
see the engine's own just-written data can use a read connection on the loop thread. The ratchet
reaches zero.

## 5. Acceptance

1. A soak on the SD card — **with the second instance running**, as a household's HA recorder would
   be — shows `scan-log` `A (loop blocking): PASS` across the whole run, sweep and finalises
   included.
2. The ratchet is at zero: no engine code touches the write connection on the loop thread.
3. Every B test still passes; per-block atomicity is pinned to unit boundaries.
4. No HA WebSocket reconnect during a soak.

## 6. Risks and open questions

1. **Code that assumed a write had happened.** Synchronous writes complete before the next line;
   an awaited unit completes before the next line too, but other coroutines may now run in between.
   Any loop code that read the database expecting to see another coroutine's write *mid-way* is
   exposed. Find these by the ratchet's list, not by hope.
2. **Teardown.** A restore closes the engine store on the engine loop today (`_restore_worker`). It
   must close it on the writer thread instead, after the queue drains.
3. **Queue depth.** A unit that waits 10 s on the card holds the queue for 10 s. The loop no longer
   blocks, but the next finalise waits behind it. Is a bounded queue with a logged backlog enough, or
   does the live tick need priority?
4. **Size.** 60 engine functions touch `_store` today (24 async, 36 sync; 225 call sites). Most
   writes sit inside a small number of synchronous functions, which is what makes step 2 tractable;
   the long tail is step 3.
