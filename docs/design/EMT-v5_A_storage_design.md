# Workstream A — storage and access model

*The root workstream. B, C and D all get cheaper once this is settled. The programme map
([EMT-v5_programme.md](EMT-v5_programme.md)) frames the ordering; this document is the design.*

**Status:** draft, 18 Sep 2026. Measured against merged 4.5.13.

---

## 1. The finding — the mechanism exists, and is used 4% of the time

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
_get_read_store()  call sites in server.py :  3
_get_store()       call sites in server.py : 75
                            adoption       :  4%

GET-capable routes examined                : 88
  using the read companion                 :  1
  reading on the WRITER connection         : 41
  no store access                          : 46
```

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

`BlockStore` has 208 methods. Classified by the SQL they execute:

| | methods | public |
|---|---|---|
| **Write** (INSERT / UPDATE / DELETE / DDL) | 104 | 92 |
| **Read-only** (SELECT only) | 77 | 72 |
| Helpers / conversion (no SQL) | 27 | — |

So **43% of the SQL-bearing API is read-only**, and the split can be drawn mechanically rather than
by judgement. That matters: it means the boundary can be enforced by construction instead of by
convention.

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

`ReadStore` **does not define the 92 public write methods.** A read path cannot write, because the
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

- **B (job coordinator)** shrinks to coordinating *writers only*. With reads structurally unable to
  mutate and the writer thread-affine, the coordinator's scope is the 92 public write methods and
  the jobs that call them — not "serialise everything".
- **C (one API)** stops being a concurrency risk. Every surface becomes a reader by construction, so
  adding surfaces cannot add contention.
- **D (performance)** gets its ceiling lifted: readers stop queueing behind the writer. This is where
  most of D's win actually comes from — see the programme map's note that D is largely delivered by
  A and C rather than by optimisation of its own.

---

## 5. Acceptance

1. **No read path can write.** `ReadStore` lacks every public write method; asserted by a test that
   enumerates both types and fails if their method sets intersect.
2. **The writer is unreachable off-thread.** A test calling `writer()` from a worker thread expects a
   raise.
3. **Adoption is 100%, and stays there.** A structural test — in the style of v4's existing AST tests
   — fails if any route handler references the writer directly. The 4% figure above is what happens
   without this test; it is the single most important item on this list.
4. **Soak:** every read surface hammered while the engine writes continuously, asserting no torn
   rows, no `SQLITE_MISUSE`, and no read blocked behind a write. Run on the Pi.
5. **No `check_same_thread=False`** anywhere in the codebase. Its presence is what made the whole
   class of bug expressible.
6. **A reader never serves a stale snapshot.** Open a read on a thread, have the engine commit, and
   assert the next request on that same thread sees the new value (§7.1). The failure mode is a
   coherent page showing yesterday's numbers with no error anywhere, so it cannot be left to review.

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
2. **What happens to the in-memory/injected-store fallback?** v4's `_get_read_store()` falls back to
   the shared handle for `:memory:` stores because a companion would open a different, empty
   database — which is how the tests inject fixtures. v5's tests need a story that doesn't reintroduce
   the shared handle by the back door.
3. **Where do long-running read jobs sit?** The importer and any parity/verification pass read
   heavily for minutes. They are readers, so they need no coordination — but they may want their own
   connection rather than a pooled per-thread one, and possibly a different `busy_timeout`.
4. **Does the engine ever legitimately need to read on a worker thread?** If any background task is
   not on the engine thread and must read, it uses `store()` like anything else — but it is worth
   enumerating those tasks before assuming so.