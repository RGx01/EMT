"""
store_access.py — `store()` and `writer()` (workstream A §3.1, §3.3).

**The default is the safe one.** Reads are the overwhelming majority of call
sites, so reads get the short, obvious name. A developer reaching for the store
gets an object that cannot write, without deciding anything; getting the writer
means typing something different and meaning it.

That inversion is the whole mechanism. v4 already had a read companion — built
three separate times, after three production incidents — and it reached 0%
adoption, because nothing about the wrong choice is punished at the moment it is
made. `_get_store()` was shorter, older, and worked perfectly in development.
A safe path that is opt-in stays opt-in.

Thread affinity lives here rather than on `WriteStore` so that constructing a
store in a test does not need an engine loop.
"""
import threading

from store_types import ReadStore, WriteStore

_lock = threading.Lock()
_db_path = None
_engine_thread = None
_writer = None
_local = threading.local()
# Bumped when the database FILE is replaced (restore, upload). Each thread's
# ReadStore remembers the generation it opened at; `store()` reopens on a mismatch.
_generation = 0


class WrongThread(RuntimeError):
    """The writer was reached from somewhere other than the engine thread.

    Every cross-thread incident in v4 was this: one connection, opened
    `check_same_thread=False`, touched from a worker. The engine's own comment
    names the result — *"a DB write from the executor thread races the
    loop-thread writers on that one connection and corrupts it: 'cannot commit -
    no transaction is active' / 'error return without exception set'"*. Making it
    an immediate, loud error costs one check and removes the class.
    """


def bind(db_path: str, *, engine_thread: int = None) -> None:
    """Call once from the engine thread at startup, before any task runs."""
    global _db_path, _engine_thread, _writer
    global _generation
    with _lock:
        _db_path = db_path
        _engine_thread = (engine_thread if engine_thread is not None
                          else threading.get_ident())
        _writer = None
        _generation += 1


def invalidate() -> None:
    """The database file at the bound path has been REPLACED. Call after the swap.

    A SQLite connection holds the file it opened, not the path: after a restore
    renames a new blocks.db into place, every open reader keeps reading the old
    one — a whole replaced database served with no error anywhere, until restart.
    Found by the first Pi soak (29 Sep 2026): after a Data Management restore,
    storage-info reported the new file's size while every read returned the old
    file's five days.

    Readers are per-thread, and a connection may only be closed on its own thread,
    so this cannot close them. It bumps a generation; each thread's next
    `store()` sees the mismatch, closes its own connection and reopens.
    """
    global _generation, _writer
    with _lock:
        _generation += 1
        _writer = None


def on_engine_thread() -> bool:
    """True on the thread `bind()` recorded as the engine's (and before any bind,
    when there is no engine to be off)."""
    return _engine_thread is None or threading.get_ident() == _engine_thread


def store() -> ReadStore:
    """A per-thread read-only store. The default — use this unless you are
    writing.

    Per-thread because a SQLite connection is not safe to share across threads,
    and one per thread is cheap. **No fallback to a writable store**: v4's
    companion returned the shared writer handle when it could not open its own,
    which meant the safe call could hand back the unsafe object. A `ReadStore`
    that might not be read-only is not a boundary.
    """
    if _db_path is None:
        raise RuntimeError("store_access.bind() has not been called")
    st = getattr(_local, "read", None)
    if st is not None and getattr(_local, "generation", None) != _generation:
        # RETIRE, don't close (#57, 3 Oct 2026). The caller that opened this connection
        # may still be using it: a route takes `store()` at its top and calls helpers
        # that call `store()` again (`load_config()` does). Closing here, mid-request,
        # failed that route with "Cannot operate on a closed database" — a 500 on the
        # Pi soak, a request in flight as a restore swapped the file. The retired one
        # is closed at the end of the request (`close_retired()`, Flask's
        # teardown_request), or at the next swap at the latest.
        for old in getattr(_local, "retired", ()):
            try:
                old.close()
            except Exception:
                pass
        _local.retired = [st]
        st = None
    if st is None:
        st = ReadStore(_db_path)
        _local.read = st
        _local.generation = _generation
    return st


def close_retired() -> None:
    """Close this thread's connections retired by a file swap. Call where nothing on
    this thread can still hold one — the end of a request."""
    for old in getattr(_local, "retired", ()):
        try:
            old.close()
        except Exception:
            pass
    _local.retired = []


def writer() -> WriteStore:
    """The engine's write store. Raises off the engine thread.

    A route never calls this. A route that needs a write asks the engine to
    perform it — which is the boundary workstream B formalises into a
    coordinator.
    """
    global _writer
    if _db_path is None:
        raise RuntimeError("store_access.bind() has not been called")
    here = threading.get_ident()
    if here != _engine_thread:
        raise WrongThread(
            "writer() called from thread %s; the write store belongs to the "
            "engine thread %s. Read with store(), or ask the engine to perform "
            "the write." % (here, _engine_thread))
    if _writer is None:
        with _lock:
            if _writer is None:
                _writer = WriteStore(_db_path)
    return _writer


def bind_object_for_tests(obj) -> None:
    """Make `store()` return `obj` verbatim. TEST INFRASTRUCTURE ONLY.

    Named this plainly because it is the one hole in the boundary: parts of the
    suite hand the server a `MagicMock` or a pre-built store that has no openable
    path, and a second connection to it is not a thing that exists. Production
    never reaches this — `bind()` takes a path and nothing else — and the name is
    meant to be obvious in a diff if it ever appears outside `tests/`.
    """
    global _db_path, _engine_thread, _writer
    with _lock:
        _db_path = _OBJECT
        _engine_thread = threading.get_ident()
        _writer = obj
    _local.read = obj
    _local.generation = _generation


_OBJECT = "<bound-object-for-tests>"


def reset_for_tests() -> None:
    """Test infrastructure living in production code.

    `.build/check_reachability.py` skips `tests/`, so this traces as unreachable
    and sits in the baseline as a named exception. That is deliberate: the
    alternative is a rule like "names ending _for_tests are exempt", which is a
    loophole anyone can walk through. One visible exception beats a general
    excuse.
    """
    global _db_path, _engine_thread, _writer
    with _lock:
        _db_path = _engine_thread = _writer = None
    if hasattr(_local, "read"):
        del _local.read
    if hasattr(_local, "generation"):
        del _local.generation
