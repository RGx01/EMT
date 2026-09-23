# Workstream A — storage and access model

*The root workstream. B, C and D all get cheaper once this is settled. The programme map
([EMT-v5_programme.md](EMT-v5_programme.md)) frames the ordering; this document is the design.*

**Status:** the surfaces are ported and the split is live, 22 Sep 2026. Measured against merged
4.5.13; built on the 4.5.14 seed.

| | | |
|---|---|---|
| GET routes on the writer | 44 of 44 | **0 of 44** |
| adoption | 0% | **100%** |
| suite | 2,834 | **2,833**, green |

**What remains in A is the engine, not the design.** `block_store.py:1001` holds the last
`check_same_thread=False` (§5.5) and goes when `engine.py` itself moves onto `writer()`. §6 says
that port produces B's write inventory as a side effect, which is the next step.

---

## 1. The finding — the mechanism existed, and was used 0% of the time

This workstream was originally framed as *"v4 shares one connection; v5 should give readers their
own."* **That framing is wrong, and the truth is more useful.**

v4 has invented the read-only companion **three separate times**, each after a production incident:

| # | Where | Provoked by |
|---|---|---|
| 1 | `_generate_charts_offloaded` — its own read-only `BlockStore` | the 3.1.3 cross-thread SQLite **segfault** |
| 2 | `web/server.py` `_get_read_store()` — per-thread, `read_only=True` | **SQLITE_MISUSE** under concurrent page-load reads (BL-18c) |
| 3 | `BlockStore(..., read_only=True)` — `query_only`, `busy_timeout=5000`, no DDL | the constructor both of the above rely on |

The mechanism is sound. It is simply **opt-in, and almost nobody opts in**:

```
GET-capable routes                         : 87
  reaching the WRITER connection           : 44   (40 in the handler, 4 via a helper)
  ISOLATED from the writer                 :  0
  no store access                          : 43
                            adoption       :  0%
```

*Measured by `.build/check_route_store_use.py`, guarded by
`.build/route_store_baseline.json` and `tests/test_route_store_use.py` — the acceptance-3 test
itself, written first so it measures the port before the port starts.*

**Adoption is 0%, not 4%.** The earlier hand count credited one route with using the companion.
`api_review_blocks` does call `_get_read_store()` — and then calls `load_config()`, which reaches
for the writer. **No GET route in v4 is actually isolated from the writer connection.** The hand
count also missed four routes that reach the writer only through a helper, which is why the tool
follows the call graph rather than scanning handler bodies.

**And there is no leverage point.** The obvious hope is that one shared helper accounts for the
44; it does not. Freeing `load_config`, `_active_period_timezone` and `_config_timezone` together
releases **4 routes of 44**. The other 40 have `_get_store()` written in the handler body. That is
the honest size of the surfaces port: forty individual conversions, not one helper fix.

> **Adopting the companion does not guarantee isolation, even where it is used.** `_get_read_store()`
> ends with `return _get_store()` when the database is in-memory or not yet created —
> *"correctness over the concurrency win"*. A fallback to the shared handle means the safe call can
> silently hand back the unsafe object. **v5's `ReadStore` must have no such fallback** (§3.2's
> whole point is that the write methods are absent, which nothing can fall back from); with one, the
> type split is theatre. §7 question 2 is what makes that affordable — a named shared-cache
> in-memory database removes the reason the fallback existed.

Forty-one GET routes — `/charts`, `/api/billing`, `/api/power`, `/api/power/history`, and the rest —
read on the engine's writer handle. `/api/config`, whose 500 on 26 Aug 2026 produced the *"BL-50 must
serialise reads too"* conclusion, is one of them. **That crash was not evidence that reads must be
serialised. It was evidence that this route never adopted the companion built to prevent it.**

### Why adoption failed, and why v5 must not repeat it

Nothing about the wrong choice is punished at the moment it is made. `_get_store()` is shorter,
older, autocompletes first, and works perfectly in development — where there is one request at a
time and no writer contending. The failure surfaces months later, on someone else's box, as an
intermittent 500 or a garbled row.

**A safe path that is opt-in will sit at 4% forever.** The design below is therefore not "add read
connections". It is **make the unsafe one hard to reach**.

---

## 2. The boundary is real and already classifiable

`BlockStore` has **204 methods**. Classified by the SQL they execute — **by a committed tool**,
`.build/classify_store_methods.py`, with `.build/store_split_baseline.json` as the regression guard
and `tests/test_store_split_classifier.py` checking it against cases established by hand:

| | methods | public |
|---|---|---|
| **Write** (INSERT / UPDATE / DELETE / DDL, or transitively reaching one) | 93 | 86 |
| **Read-only** (SELECT / PRAGMA only) | 102 | 94 |
| Connection lifecycle + pure helpers (no SQL) | 9 | 2 |
| **Undecided** | **0** | **0** |

So **52% of the public SQL surface is read-only** — a slight majority, not the 43% minority an
earlier ad-hoc pass reported. The split is drawn mechanically, nothing is left to judgement, and
the boundary can therefore be enforced by construction instead of by convention.

*(The earlier figures — 104 write / 77 read / 27 helpers — were a hand pass against 4.5.13 and are
not reproducible. Two things moved: the tool resolves **transitive** writes, so a method executing
no SQL of its own but calling `set_meta` is correctly a writer rather than a "helper"; and it
resolves readers the same way, which is most of the 27→9 collapse. Same lesson as the reachability
figures: a number nothing regenerates is a number that drifts.)*

**Which way to be wrong is not symmetric, and neither direction is safe.** A writer mislabelled
read-only lands on `ReadStore` and is refused at runtime by `query_only` — loud, immediate,
recoverable. A reader mislabelled write lands only on `WriteStore`, so a route that needs it
reaches for `writer()`; nothing breaks, nothing complains, and adoption drifts straight back toward
the 4% this workstream exists to fix. **So the tool does not "round up to write" for safety** —
that manufactures the second, silent error. Anything it cannot settle is reported UNDECIDED for a
human, and the count above is the point: there is currently nothing to adjudicate.

---

## 3. The design

### 3.1 Invert the default

Reads are the overwhelming majority of call sites, so reads get the short, obvious name:

```python
store()              # → ReadStore. Per-thread, read-only. The default.
writer()             # → WriteStore. Engine thread only. Raises elsewhere.
```

A developer reaching for the store gets the safe object without deciding anything. Getting the
writer requires typing something different and meaning it.

### 3.2 Split the type, don't just split the connection

`ReadStore` **does not define the 86 public write methods.** A read path cannot write, because the
object has no such attribute — an `AttributeError` at first execution, not a silent cross-thread
write months later.

This is the part that makes adoption structural rather than cultural. v4's `read_only=True` guards
the *connection* (`query_only`), which correctly stops the SQL — but only once a request has already
taken the wrong path and reached SQLite. Splitting the type moves the failure to the call site.

### 3.3 The writer is thread-affine and asserts it

`writer()` raises if called from anything but the engine thread. v4's incidents were all
cross-thread access to the writer handle; making that an immediate, loud error costs one check and
removes the entire class.

Note this does **not** mean routes cannot cause writes — it means a route causes a write by asking
the engine to perform it, which is the boundary workstream B then formalises into a coordinator.

### 3.4 One mechanism, not three

v5 carries **one** read-store implementation. The chart-renderer companion and the server companion
both disappear into `store()`. Three implementations of one idea is how you end up with two of them
being right and the third being whatever it was in 3.1.3.

---

## 4. What this buys the other workstreams

- **B (job coordinator)** coordinates *writers only*. With reads structurally unable to mutate and
  the writer thread-affine, the scope is the 86 public write methods and the callers that reach
  them — not "serialise everything". **Measured**, by `.build/write_inventory.py` (guarded by
  `tests/test_write_inventory.py`):

  | | functions | write | note |
  |---|---|---|---|
  | `engine.py` | 292 | **92** | 62 directly, 30 only through a callee; 7 mutate via raw `_conn` |
  | `web/server.py` | 245 | **26** | of which **19 are route handlers still writing on a request thread** |
  | `kraken_ingester.py` | 5 | 1 | |
  | `backfill.py` · `csv_import.py` · `api_import.py` | 27 | **0** | pure transforms; the caller persists |

  Two things fall out of that table. **The 19 are all POST/PUT/DELETE — no GETs left** — so the
  surfaces port closed the GET hazard and what remains is the declared-mutating surface. And the
  transform modules writing nothing is worth keeping true: they are the likeliest place for a
  convenience write to appear, and it would land outside the engine thread.

  Note this is *not* a shrink from where B started. A §7 q3 established that all 18 long-running
  jobs are writers, so the minutes-long work is exactly what B must coordinate. What A bought B is
  not less work — it is a **measured boundary** instead of "serialise everything".
- **C (one API)** stops being a concurrency risk. Every surface becomes a reader by construction, so
  adding surfaces cannot add contention.
- **D (performance)** gets its ceiling lifted: readers stop queueing behind the writer. This is where
  most of D's win actually comes from — see the programme map's note that D is largely delivered by
  A and C rather than by optimisation of its own.

---

## 5. Acceptance

*Where each one lives, as of 22 Sep 2026. All but the soak are met. Acceptance 6 is file-backed
by necessity: on an in-memory database there is no WAL, so there are no snapshots and the test would
pass whether or not the implementation were right.*

| | test | state |
|---|---|---|
| 1 no read path can write | `tests/test_store_types.py` | met |
| 2 writer unreachable off-thread | `tests/test_store_types.py` | met |
| 3 adoption is 100% and stays | `tests/test_route_store_use.py` | met — baseline empty |
| 4 soak | — | **not written**; needs the Pi |
| 5 no `check_same_thread=False` | `tests/test_store_types.py` | census pinned at one, in `block_store.py` |
| 6 no stale snapshot | `tests/test_store_types.py` | file-backed; the freshness assertions cannot currently fail (see §5a), so the mechanism is asserted directly instead |


1. **No read path can write.** `ReadStore` lacks every public write method; asserted by a test that
   enumerates both types and fails if any of the 86 public writers appears on `ReadStore`.

   *(Corrected 22 Sep 2026. This previously said "fails if their method sets intersect" — which
   would be wrong, because `WriteStore` must be able to read. Measured: **40 of the 93 write methods
   (43%) need read access**, either running their own `SELECT` or calling a read method. So
   `WriteStore` extends `ReadStore` rather than standing beside it, and the invariant is one-way:
   nothing that writes may appear on the read type. Keeping them disjoint would also split a
   read-then-write across two connections and lose the consistency v4 gets for free.)*
2. **The writer is unreachable off-thread.** A test calling `writer()` from a worker thread expects a
   raise.
3. **Adoption is 100%, and stays there.** A structural test — in the style of v4's existing AST tests
   — fails if any route handler references the writer directly. The 4% figure above is what happens
   without this test; it is the single most important item on this list.
4. **Soak** *(file-backed store only, same reason as 6)*: every read surface hammered while the engine writes continuously, asserting no torn
   rows, no `SQLITE_MISUSE`, and no read blocked behind a write. Run on the Pi.
5. **No `check_same_thread=False`** anywhere in the codebase. Its presence is what made the whole
   class of bug expressible.
6. **A reader never serves a stale snapshot.** *(File-backed store only — an in-memory
   database has no WAL, so this passes vacuously on one. See §7 question 2.)* Open a read on a thread, have the engine commit, and
   assert the next request on that same thread sees the new value (§7.1). The failure mode is a
   coherent page showing yesterday's numbers with no error anywhere, so it cannot be left to review.

---

## 5a. What the port found — three bugs, and one I got wrong

Recorded because they are the argument for porting a surface at a time rather than in one
sweep, and because three of the four were invisible to review and visible only to the suite.

- **A bug I reported and then withdrew.** Mid-port I read a failing read-after-write as
  `ReadStore` pinning a WAL snapshot, added `isolation_level=None`, and wrote it up as the fix.
  It was not: the failure was a test fixture binding the wrong store, and measurement shows
  Python's sqlite3 never begins a transaction for a SELECT under either setting
  (`in_transaction` is False after a read both ways). The setting stays as a statement of intent;
  the claim does not. Recorded because the §4a habit that caught it — make the test fail on
  purpose before believing it — is worth more than the tidier story.
- **Bound staticmethods silently shift every argument.** `BlockStore._finalised_clause` is a
  `staticmethod`; `_BS._finalised_clause` yields the plain function, and assigning that as a class
  attribute makes it an instance method again, so `self` lands where the first parameter belongs.
  It appeared as `no such column: False.is_provisional` — a boolean formatted into SQL where a
  table alias should be. The generator re-wraps them now.
- **Class constants were omitted entirely** — all nine. A route reading a SQL fragment off the
  class got `'ReadStore' object has no attribute 'AUTO_CORRECTION_REASONS'`.
- **`BlockStore` could not open a URI path**, so §7 question 2's named shared-cache answer was
  unusable in practice. One line, and it is the difference between a decision and a fixture.

**One deliberate hole:** `store_access.bind_object_for_tests()`. Parts of the suite hand the server
a `MagicMock` or a pre-built store with no openable path, and a second connection to such a thing
does not exist. Production calls `bind()`, which takes a path and nothing else, so the hole is
unreachable from the application and conspicuous in a diff.

`ReadStore` also carries **no `__slots__`**. It saved nothing here and blocked the instance
monkeypatching the suite depends on; a boundary that makes tests harder to write costs more than it
buys. The boundary that matters is the absent write methods.

---

## 5b. The pre-wizard state, and where `bind()` goes

`bind()` does not belong inside either subsystem's startup, and the reason is a state neither
workstream had written down: on a fresh install — and after a main-meter delete — the application
runs **fully configuration-less**, and the web server is the only thing with anything to do.

What v4 actually does, measured rather than assumed:

- `main.py` starts `engine_loop_task` **unconditionally** (inside the final `gather`). There is no
  configuration precondition anywhere between `setup()` and the loop. The engine is not deferred —
  it ticks every 10 seconds from boot.
- It is nonetheless **inert**. `ensure_correct_block` refuses to form the first block until either a
  main import read sensor exists or an API mode has been explicitly chosen. Until then every tick
  returns without creating, capturing or finalising anything, and `capture_samples` bails on
  `if not config` as a second line of defence.
- `api_meter_main_reset` puts the install back into exactly that state: it deletes `blocks.db` and
  its `-wal`/`-shm`, rewrites `meters_config.json` to `{"meters": {}}`, clears the stored API
  credentials — *"a DB wipe alone leaves them orphaned and the API wrongly re-activates on next
  start"* — and restarts the add-on. The mode reverts to `'unset'`, which `_detect_upgrade_mode`
  deliberately leaves alone on an install with no import sensor.

So "the engine does not start until there is configuration" is true of its **effects** and false of
its **lifecycle**. That distinction is the point for A: the process is up, the loop is running, and
the user is in the setup wizard — which is served entirely by `store()` reads. A `bind()` inside
`engine_startup()` would happen to work today, precisely because startup is unconditional, but it
would make the read path depend on a lifecycle decision that has nothing to do with reading.

**Therefore:** `bind()` is called in `main.py`, after `setup()` has ensured `DATA_DIR` and before
**both** `engine_startup()` and `server.init()`. One call site, no ordering subtlety, and the
pre-wizard state is served by construction rather than by luck.

One trap worth recording, because it is invisible to review: `main()` imports `os` twice *inside its
own body*, which makes `os` a local name for the whole function. A `bind(os.path.join(...))` placed
above those imports raises `UnboundLocalError` at boot — on the very first line of a fresh install.
The call uses a distinct name rather than relying on statement order.

### The gate had a hole, and the wizard ordering is load-bearing

The API arm of the first-run gate was added after the CAD arm and opened on the mode alone. The
setup wizard writes the mode **before** it writes the meters, so a tick landing in that window
created a block with the default `block_minutes` and an empty seeded shell — precisely the
block-size mismatch the CAD arm's own comment exists to prevent. Measured: a `12:00–12:30` block at
`block_minutes=30` on an install about to save 5.

The fix is on the gate, **not** the ordering. The wizard's order is deliberate: of the three POSTs it
makes, only the config save reschedules `engine_startup`, and startup is what activates the supplier
API. Put the config first and a `cad+api` install finishes setup with the API dormant until the next
restart, while `_detect_upgrade_mode` stamps `'cad'` and logs *"upgrade detected"* on a brand-new
install. The last call has to be the one that triggers startup.

So the arm now requires a configured main meter as well as a mode, and it tolerates the window
rather than trying to close it. `tests/test_first_run_gate.py` pins all five states — both arms
closed, both arms open, and the window itself.

Recorded here because nothing in the suite had asserted the gate's behaviour before, which is how an
arm could be added that reopened a hazard the adjacent comment described in full.

---

## 6. Migration and porting implications

This workstream lands **before** the pricing engine is ported, not after. Porting 21k lines of
`engine.py` and `block_store.py` onto the old access model and then retrofitting the boundary would
mean doing the audit twice, with the second pass against unfamiliar code.

Practically: define `ReadStore`/`WriteStore` first, then port modules onto them one at a time. A
module that will not compile against `ReadStore` is telling you it writes — which is exactly the
inventory workstream B needs, produced as a side effect.

---

## 7. Open questions

1. ~~**Does `ReadStore` need a snapshot, or is per-request freshness right?**~~ **Closed
   19 Sep 2026: per-request freshness. `ReadStore` ends its read transaction between requests.**

   WAL gives each connection a consistent snapshot at the start of its read transaction, and a
   long-lived per-thread connection that never commits pins that snapshot indefinitely — so a page
   renders a coherent, internally consistent, *stale* view and looks perfectly healthy while doing
   it. That is the worst failure shape available here: no error, no torn row, just yesterday's
   numbers presented with confidence. v4's companion sets `busy_timeout` and says nothing about
   snapshot lifetime, which is how it stayed unexamined.

   **Acceptance (added to §5):** a test that opens a read, has the engine commit a write, and
   asserts the next request on the *same thread* observes it. One line of implementation, but it
   fails silently if forgotten, so it is tested rather than trusted.

   *This question was also recorded in architecture §8.3 and is now closed there as a pointer here —
   one decision, one home.*
2. ~~**What happens to the in-memory/injected-store fallback?**~~ **Closed 22 Sep 2026: a named
   shared-cache in-memory database — except for the tests that need WAL, and those are now named.**

   First the scale, because it rules out the obvious answer. **73 test files use `:memory:`, 246
   times.** "Convert the suite to temp files" is not a migration, it is a rewrite of the fixtures,
   and it slows every test on the way. The shared handle has to go without that.

   It can. A **named** in-memory database with a shared cache is a real, shareable database:

   ```
   file:emtfix1?mode=memory&cache=shared   +   PRAGMA query_only=ON
       companion reads the writer's table  : (1,)
       companion refused a write           : attempt to write a readonly database
       companion sees a later commit       : (2,)
   ```

   So a `ReadStore` over an in-memory fixture is a real second connection that really cannot write.
   §5's acceptance 1 and 2 hold over the existing fixture idiom, and the back door stays shut.
   v4's fallback existed because `":memory:"` is **anonymous**, not because in-memory is
   unshareable — the fix is a name, not a file.

   **The catch, and it is the important half:**

   ```
   PRAGMA journal_mode on an in-memory database → 'memory'    (WAL is unavailable)
   ```

   No WAL means no snapshots, which means nothing for a stale-snapshot test to get wrong.
   **Acceptance 6 would pass vacuously on an in-memory store** — and acceptance 6 guards the one
   failure that shows no error at all, just yesterday's numbers rendered with confidence. So it is
   file-backed, and §5 says so rather than leaving it to whoever writes the test.

   **Rule:** named shared-cache in-memory for the bulk of the suite; file-backed for acceptance 6
   and the soak. Anything asserting on snapshot lifetime or WAL behaviour may not use an in-memory
   store.
3. ~~**Where do long-running read jobs sit?**~~ **Closed 22 Sep 2026: there are none. The question
   had a false premise, and correcting it moves work INTO B rather than out of it.**

   The question assumed the importer and the verification passes "are readers, so they need no
   coordination". Enumerated across `engine.py`'s long-running jobs — every `run_*` / `_run_*` —
   **18 of 18 are writers.** Not readers that occasionally write: batch writers whose entire
   purpose is to mutate history.

   | job | mutates via |
   |---|---|
   | `run_api_import_job` — *the importer* | block appends, `set_meta` |
   | `run_deferred_verify_pricing` — *the verify pass* | re-pricing, `set_kraken_state` |
   | `_run_historical_reprice_sweep` · `_segment_backfill` · `_exc_backfill` · `_iog_split_backfill` · `_carbon_backfill` | `set_block_segments`, `set_blocks_exc`, … |
   | `run_measured_history_drain` | `upsert_measured_cost` |
   | `run_gap_fill_job` · `run_iog_split_carve` · `run_attribution_job` · the four `run_smb_*` repairs · `run_settled_ev_split_heal` | all mutate |

   The three that first scanned as readers were a six-line arithmetic helper that is not a job at
   all (`_run_wrote_blocks`) and two thin wrappers delegating straight to a writer
   (`run_reprice_history_sweep_to_done`, `run_settled_ev_split_heal`).

   **Three consequences:**

   - **A needs no special case.** There is no long-running reader to hand its own connection or a
     longer `busy_timeout` to. `store()` and `writer()` cover the field.
   - **§4's "B shrinks to coordinating writers only" is true, but much less of a shrink than it
     sounds.** The minutes-long jobs *are* the writers, so they are precisely what B must
     coordinate. B doesn't get cheaper here — it gets its scope named.
   - **v4 already solves the duration, and the solution must survive the port.** Not a second
     connection: **cooperative pacing on the engine loop** — `_MEASURED_HISTORY_DRAIN_PACE`,
     `_EXC_BACKFILL_PASS_PACE`, the sweeps' batch cursors and resumable markers. That is the
     existing answer to "a minutes-long write must not starve the loop", it is load-bearing, and
     B should formalise it rather than reinvent it.
4. ~~**Does the engine ever legitimately need to read on a worker thread?**~~ **Closed
   19 Sep 2026: no — and v4 already states the rule.** Enumerated across 4.5.13 rather than assumed.

   **v4's discipline is "offload the blocking work, keep the DB on the loop thread."** The startup
   carbon-intensity fetch says why, in a comment that is really a bug report:

   > *"Offload ONLY the blocking NETWORK fetch to the executor. All DB writes stay on the loop
   > thread — the engine owns a single SQLite connection (`check_same_thread=False`), so a DB write
   > from the executor thread races the loop-thread writers on that one connection and corrupts it:
   > 'cannot commit - no transaction is active' / 'error return without exception set'."*

   It even extracts the postcode on the loop thread *before* entering the executor, because
   `_get_postcode()` → `load_config()` touches the connection.

   **Exactly two workers touch the database, and both own their connection:**
   `_generate_charts_offloaded` (its own read-only `BlockStore`; dies with the chart pipeline) and
   `_backup_to_share` (`sqlite3.connect` in that thread). Everything else offloaded is network or
   CPU.

   So v5 inherits no task needing a special case. `store()` — per-thread, read-only — turns v4's
   comment-enforced rule into a structural one, and the corruption mode quoted above is the clearest
   statement of why this workstream exists.
