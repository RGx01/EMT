"""
v4import — bring a v4 database into v5 (architecture step 7; import spec; E design).

One package, one direction. Nothing outside it imports from it except the restore paths in
web/server.py, through `import_v4` and `gate` (tests/test_v4import.py enforces that). When v4
sources stop being supported, the package is deleted and the restore loses one branch
(spec §1.2).

    import_v4(source, target)  ->  report (dict)

`source` is a candidate that has already passed `gate.check`. It is attached read-only
(`immutable`) and never written. `target` is created fresh with v5's own schema, filled in
one transaction (E design §3), and checked against the source before the transaction
commits (§6). Any failure rolls back, deletes the target and raises ImportFailed with a
message for the user. The live database is never touched here: the restore swaps the
target in afterwards, as it swapped the candidate in before.
"""
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import quote

from . import gate, rules, transforms, verify

# The importer's own revision, recorded in the audit record so a support question can say
# which rules a database was imported under.
IMPORTER_REVISION = 1


class ImportFailed(Exception):
    """The import could not be completed. The message is for the user; the target is gone."""


def _remove(path):
    for p in (path, path + "-wal", path + "-shm", path + "-journal"):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def _src_names(conn, sql):
    return [r[0] for r in conn.execute(sql).fetchall()]


def check_source(conn):
    """Every source table, carried column and state key has a rule. Returns the problems,
    worst first, as one-line descriptions (empty when the source is fully classified)."""
    problems = []
    tables = _src_names(conn, "SELECT name FROM src.sqlite_master WHERE type = 'table' "
                              "AND name NOT LIKE 'sqlite_%' ORDER BY name")
    known = {t for t, _ in rules.CARRY} | rules.DROP_TABLES | set(rules.KEYED_TABLES)
    for t in tables:
        if t not in known:
            problems.append("table %s" % t)
    for t, cols in rules.CARRY:
        if t not in tables:
            continue
        have = [r[1] for r in conn.execute('PRAGMA src.table_info("%s")' % t).fetchall()]
        dropped = rules.DROP_COLUMNS.get(t, frozenset())
        for c in have:
            if c not in cols and c not in dropped:
                problems.append("column %s.%s" % (t, c))
    for t in rules.KEYED_TABLES:
        if t not in tables:
            continue
        for k in _src_names(conn, 'SELECT key FROM src."%s" ORDER BY key' % t):
            if rules.key_rule(t, k) is None:
                problems.append("%s key %s" % (t, k))
    return problems


def _carry_tables(conn, report, progress):
    for t, cols in rules.CARRY:
        have = {r[1] for r in conn.execute('PRAGMA src.table_info("%s")' % t).fetchall()}
        if not have:                     # absent from the source: nothing to carry
            report["tables"][t] = {"in": 0, "out": 0}
            continue
        if progress:
            progress("importing %s" % t)
        sel = ", ".join('"%s"' % c for c in cols if c in have)
        conn.execute('DELETE FROM main."%s"' % t)
        conn.execute('INSERT INTO main."%s" (%s) SELECT %s FROM src."%s"' % (t, sel, sel, t))
        n = conn.execute('SELECT COUNT(*) FROM main."%s"' % t).fetchone()[0]
        report["tables"][t] = {"in": n, "out": n}


def _source_facts(source):
    conn = gate._connect(source)
    try:
        facts = {"written_by": gate._meta(conn, "written_by"),
                 "db_uuid": gate._meta(conn, "db_uuid")}
    finally:
        conn.close()
    supplier, inferred = gate.source_supplier(source)
    facts["supplier"] = supplier
    facts["supplier_inferred"] = inferred
    return facts


def import_v4(source, target, progress=None):
    """Import the v4 database at `source` into a new v5 database at `target`.
    Returns the report (also stored as store_meta.import_audit). Raises ImportFailed."""
    from block_store import BlockStore, EMT_SCHEMA

    t0 = time.monotonic()
    _remove(target)
    BlockStore(target).close()           # v5's own schema; it marks itself emt_schema
    report = {"importer": IMPORTER_REVISION, "emt_schema": EMT_SCHEMA,
              "imported_at": datetime.now(timezone.utc).replace(tzinfo=None)
                                     .isoformat(timespec="seconds"),
              "source": _source_facts(source), "tables": {}, "keys": {},
              "transforms": {}, "gates": {}, "seconds": {}}
    secs = report["seconds"]       # per phase — the Pi's SD card is the time that matters

    def _lap(phase, since):
        secs[phase] = round(time.monotonic() - since, 3)
        return time.monotonic()
    conn = sqlite3.connect("file:%s" % quote(os.path.abspath(target)), uri=True,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    ok = False
    try:
        conn.execute("ATTACH DATABASE ? AS src",
                     ("file:%s?immutable=1" % quote(os.path.abspath(source)),))
        problems = check_source(conn)
        if problems:
            raise ImportFailed(
                "This backup holds data this version of EMT does not know how to import "
                "(%s). Nothing was changed." % ", ".join(problems))
        t = _lap("prepare", t0)
        conn.execute("BEGIN")
        _carry_tables(conn, report, progress)
        t = _lap("carry", t)
        if progress:
            progress("translating")
        transforms.run(conn, report)
        t = _lap("transforms", t)
        if progress:
            progress("checking the import")
        failures = verify.run(conn, report)
        t = _lap("verify", t)
        if failures:
            raise ImportFailed(
                "The import did not reconcile with the backup (%s). Nothing was changed."
                % "; ".join(failures))
        conn.execute("INSERT OR REPLACE INTO main.store_meta (key, value) VALUES (?, ?)",
                     ("import_audit", json.dumps(report, sort_keys=True)))
        conn.execute("COMMIT")
        conn.execute("DETACH DATABASE src")
        # One self-contained file for the swap: no -wal beside it to leave behind.
        conn.execute("PRAGMA journal_mode=DELETE")
        _lap("commit", t)          # after the audit was written: in the returned report only
        secs["total"] = round(time.monotonic() - t0, 3)
        ok = True
        return report
    except ImportFailed:
        raise
    except sqlite3.Error as e:
        raise ImportFailed("The import failed (%s). Nothing was changed." % e) from e
    finally:
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        conn.close()
        if not ok:
            _remove(target)
