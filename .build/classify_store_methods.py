#!/usr/bin/env python3
"""
Partition BlockStore's methods into READ-ONLY and WRITE, by the SQL they execute.

WHY THIS EXISTS
    Workstream A (docs/design/EMT-v5_A_storage_design.md) splits the store into
    `ReadStore` and `WriteStore` so that a read path CANNOT write — not by
    convention but because the object has no such attribute. That split has to be
    drawn somewhere, and drawing it by hand across 204 methods is how you end up
    with one wrong and no way to notice.

    A §2 asserts the boundary "can be drawn mechanically rather than by judgement".
    This is the machine that draws it. The numbers in that section came from an
    ad-hoc pass; like the reachability figures before them, they are not
    reproducible until something commits them. This commits them.

WHICH WAY TO BE WRONG
    The two errors are NOT symmetric, and neither is safe:

      * A writer mislabelled READ-ONLY lands on `ReadStore`, where `query_only`
        refuses it at runtime. Loud, immediate, caught by any test that exercises
        the path. Recoverable.
      * A reader mislabelled WRITE lands only on `WriteStore`, so a route that
        needs it must reach for `writer()`. Nothing breaks, nothing complains, and
        the codebase drifts straight back toward v4's 4% adoption — the exact
        failure this workstream exists to prevent.

    So this does NOT "over-approximate toward write" for safety; that would
    manufacture the second error. Anything it cannot settle goes to UNDECIDED to be
    adjudicated by a human, and the undecided set is meant to stay small enough
    that adjudicating it is realistic.

WHAT IT READS
    * Direct SQL: `self._conn.execute/executemany/executescript(...)`, classified
      by the first keyword of the statement. String-concatenated and f-string SQL
      is reassembled from its literal parts, which is enough to see the verb.
    * `self._conn.backup(...)` — a write to the DESTINATION, not this store; read.
    * Transitive: a method with no SQL of its own inherits from the methods it
      calls, to a fixed point. `set_meta` makes its callers writers.
    * `with self._conn:` is a transaction, not evidence of a write on its own.

USAGE
    python3 .build/classify_store_methods.py            # human-readable
    python3 .build/classify_store_methods.py --json
    python3 .build/classify_store_methods.py --baseline .build/store_split_baseline.json
    python3 .build/classify_store_methods.py --update-baseline <path>
"""
import argparse
import ast
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, "block_store.py")
CLASS = "BlockStore"

WRITE_VERBS = {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP",
               "ALTER", "TRUNCATE", "VACUUM", "REINDEX"}
READ_VERBS = {"SELECT", "WITH", "PRAGMA", "EXPLAIN", "BEGIN", "COMMIT", "ROLLBACK",
              "ANALYZE", "ATTACH", "DETACH",
              # transaction control, as BEGIN / COMMIT: a batch's savepoints (24 Oct 2026)
              "SAVEPOINT", "RELEASE"}

# PRAGMA is in READ_VERBS because most pragmas interrogate or configure the
# connection -- `table_info`, `query_only`, `busy_timeout`. A few MUTATE THE
# DATABASE FILE, and filing those as reads puts them on ReadStore, where they are
# callable from any thread with no affinity check. `wal_checkpoint` is the one
# that matters: it rewrites the main database from the write-ahead log, and the
# restore path in web/server.py already documents what a cross-thread checkpoint
# on the shared connection does --
#
#     "a PRAGMA wal_checkpoint / close() issued here while the loop is mid-query
#      on the SAME connection is a concurrent use of one SQLite connection from
#      two threads -> Segmentation fault"
#
# So the pragma NAME is consulted, not just the verb. Matched on the token after
# PRAGMA, lowercased, with any argument list already split off by first_verb's
# paren handling.
MUTATING_PRAGMAS = {"wal_checkpoint", "incremental_vacuum", "optimize",
                    "shrink_memory", "journal_mode", "wal_autocheckpoint",
                    "journal_size_limit", "auto_vacuum", "user_version",
                    "application_id", "secure_delete"}

READ, WRITE, NONE, UNDECIDED = "read", "write", "no-sql", "undecided"


def sql_text(node):
    """Reassemble a SQL argument from its literal parts, IN SOURCE ORDER.

    `ast.walk` is breadth-first, so it returns the fragments of a concatenation or
    an f-string jumbled — which is how `get_blocks_lightweight` came back reading
    " ORDER BY ... SELECT ..." and landed in UNDECIDED. Depth-first pre-order
    visits a BinOp's left before its right and a JoinedStr's values in order,
    which is what "the first verb" means.
    """
    out = []

    def walk(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
            return
        for child in ast.iter_child_nodes(n):
            walk(child)

    walk(node)
    return " ".join(out)


_VERBS = WRITE_VERBS | READ_VERBS


def first_verb(sql):
    """The first recognised SQL verb, scanning tokens in order.

    Not "the first word": SQL arrives here with leading whitespace, a stray
    fragment from a concatenation, or a `--` comment ahead of the statement, and
    taking the first word made those UNDECIDED. Scanning for the first token that
    is actually a verb is both more robust and more honest — if no token in the
    whole string is a SQL verb, this genuinely cannot tell.
    """
    for raw in sql.replace("(", " ").replace(")", " ").replace(";", " ").split():
        w = raw.strip().upper()
        if w in _VERBS:
            return w
    return ""


def _mutating_pragma(sql):
    """True when the statement is a PRAGMA that writes the database file.

    Reads the token AFTER `PRAGMA`. Note `first_verb` already replaces parens
    with spaces, so `wal_checkpoint(TRUNCATE)` arrives as two tokens and the name
    is matched cleanly. A `PRAGMA` with no following token (dynamic string) is
    NOT treated as mutating -- it falls through to the normal read verdict and,
    if genuinely undecidable, to UNDECIDED, which is the bucket that gets looked
    at rather than the one that gets trusted.
    """
    toks = sql.replace("(", " ").replace(")", " ").replace(";", " ").replace("=", " ").split()
    for i, raw in enumerate(toks):
        if raw.strip().upper() == "PRAGMA" and i + 1 < len(toks):
            return toks[i + 1].strip().lower() in MUTATING_PRAGMAS
    return False


def local_sql_vars(fn):
    """SQL is routinely assembled in a local before being executed:

        q = "SELECT ... FROM blocks"
        if only_missing:
            q += " AND imp_rate IS NULL"
        q += " ORDER BY block_start"
        self._conn.execute(q, args)

    Reading only the execute() argument sees a bare Name and gives up, which put
    16 public readers in UNDECIDED on the first run. Resolving the local is
    mechanical, so it is done here rather than left to a human to eyeball — the
    whole point of A §2 is that this boundary needs no judgement.

    Takes the FIRST assignment's literal text for each name, which is where the
    verb lives; later `+=` clauses only ever append.
    """
    out = {}
    for n in ast.walk(fn):
        targets = []
        if isinstance(n, ast.Assign):
            targets = [t.id for t in n.targets if isinstance(t, ast.Name)]
            value = n.value
        elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name):
            targets, value = [n.target.id], n.value
        elif isinstance(n, (ast.AnnAssign,)) and isinstance(n.target, ast.Name):
            targets, value = [n.target.id], n.value
        else:
            continue
        if value is None:
            continue
        text = sql_text(value)
        for t in targets:
            if t not in out and text.strip():
                out[t] = text
    return out


def class_sql_consts(cls_node):
    """SQL held as a CLASS-LEVEL constant, e.g.

        class BlockStore:
            _EV_RESTAMP_SQL = "UPDATE blocks SET ..."

            def restamp_ev_columns(self, ...):
                self._conn.execute(self._EV_RESTAMP_SQL, (...))

    `local_sql_vars` only resolves names assigned inside the function, so a
    statement factored onto the class came back UNDECIDED -- which is the honest
    bucket, but it means the first method to share its SQL between two callers
    cannot be classified, and sharing is exactly what you do when a statement
    appears three times. Keyed by bare name; both `self.X` and `Cls.X` resolve
    to it.
    """
    out = {}
    for n in cls_node.body:
        if isinstance(n, ast.Assign) and isinstance(n.value, (ast.Constant, ast.BinOp, ast.JoinedStr)):
            text = sql_text(n.value)
            if not text.strip():
                continue
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = text
    return out


def classify_body(fn, class_consts=None):
    """Direct verdict from this method's own SQL, ignoring what it calls."""
    verdicts = set()
    unknown = []
    locals_ = dict(class_consts or {})
    locals_.update(local_sql_vars(fn))
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if not isinstance(f, ast.Attribute):
            continue
        if f.attr in ("execute", "executemany", "executescript"):
            # self._conn.execute(...) or a cursor from it
            if not n.args:
                continue
            arg = n.args[0]
            sql = sql_text(arg)
            verb = first_verb(sql)
            if verb == "PRAGMA" and _mutating_pragma(sql):
                verb = "VACUUM"          # any WRITE_VERB; the bucket is what matters
            if verb not in WRITE_VERBS and verb not in READ_VERBS:
                # the argument is (or starts from) a local — resolve it
                names = [x.id for x in ast.walk(arg) if isinstance(x, ast.Name)]
                # `self._EV_RESTAMP_SQL` is an Attribute, not a Name -- add the
                # attribute names so a class-level constant resolves too.
                names += [x.attr for x in ast.walk(arg) if isinstance(x, ast.Attribute)]
                for nm in names:
                    if nm in locals_:
                        v2 = first_verb(locals_[nm])
                        if v2 in WRITE_VERBS or v2 in READ_VERBS:
                            verb = v2
                            break
            if verb in WRITE_VERBS:
                verdicts.add(WRITE)
            elif verb in READ_VERBS:
                verdicts.add(READ)
            elif f.attr == "executescript":
                verdicts.add(WRITE)          # schema scripts
            else:
                unknown.append(verb or "<dynamic>")
    if WRITE in verdicts:
        return WRITE, unknown
    if unknown:
        return UNDECIDED, unknown
    if READ in verdicts:
        return READ, unknown
    return NONE, unknown


def self_calls(fn):
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            v = n.func.value
            if isinstance(v, ast.Name) and v.id == "self":
                out.add(n.func.attr)
    return out


def analyse():
    tree = ast.parse(open(TARGET, encoding="utf-8").read(), filename="block_store.py")
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == CLASS)
    methods = {m.name: m for m in cls.body
               if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}

    consts = class_sql_consts(cls)
    direct, unknowns, calls = {}, {}, {}
    for name, fn in methods.items():
        v, unk = classify_body(fn, consts)
        direct[name] = v
        if unk:
            unknowns[name] = sorted(set(unk))
        calls[name] = self_calls(fn) & set(methods)

    # Transitive closure: a method inherits WRITE from anything it calls.
    verdict = dict(direct)
    changed = True
    while changed:
        changed = False
        for name in methods:
            if verdict[name] == WRITE:
                continue
            for callee in calls[name]:
                if verdict[callee] == WRITE:
                    verdict[name] = WRITE
                    changed = True
                    break
    # A no-SQL method that reaches a reader is a reader; one that reaches nothing
    # is a pure helper.
    changed = True
    while changed:
        changed = False
        for name in methods:
            if verdict[name] != NONE:
                continue
            for callee in calls[name]:
                if verdict[callee] in (READ, UNDECIDED):
                    verdict[name] = verdict[callee]
                    changed = True
                    break
    return methods, verdict, unknowns


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--baseline")
    ap.add_argument("--update-baseline")
    a = ap.parse_args()

    methods, verdict, unknowns = analyse()
    buckets = {WRITE: [], READ: [], NONE: [], UNDECIDED: []}
    for name in sorted(methods):
        buckets[verdict[name]].append(name)

    pub = lambda xs: [x for x in xs if not x.startswith("_")]
    payload = {
        "class": CLASS,
        "methods": len(methods),
        "write": buckets[WRITE],
        "read": buckets[READ],
        "no_sql": buckets[NONE],
        "undecided": buckets[UNDECIDED],
        "undecided_detail": {k: v for k, v in unknowns.items()
                             if verdict[k] == UNDECIDED},
        "counts": {k: {"total": len(v), "public": len(pub(v))}
                   for k, v in (("write", buckets[WRITE]), ("read", buckets[READ]),
                                ("no_sql", buckets[NONE]),
                                ("undecided", buckets[UNDECIDED]))},
    }

    if a.update_baseline:
        with open(a.update_baseline, "w", encoding="utf-8") as fh:
            json.dump({k: payload[k] for k in ("write", "read", "no_sql", "undecided")},
                      fh, indent=2)
            fh.write("\n")
        print("baseline written: %d write, %d read, %d no-sql, %d undecided"
              % tuple(len(payload[k]) for k in ("write", "read", "no_sql", "undecided")))
        return 0

    if a.json:
        print(json.dumps(payload, indent=2))
    else:
        print("%s — %d methods\n" % (CLASS, payload["methods"]))
        for k in ("write", "read", "no_sql", "undecided"):
            c = payload["counts"][k]
            print("  %-10s %4d  (%d public)" % (k, c["total"], c["public"]))
        sql = payload["counts"]["write"]["public"] + payload["counts"]["read"]["public"]
        if sql:
            print("\n  read-only share of the public SQL surface: %.0f%%"
                  % (100.0 * payload["counts"]["read"]["public"] / sql))
        if payload["undecided"]:
            print("\nUNDECIDED — adjudicate these by hand; they cannot go in either type yet:")
            for n in payload["undecided"]:
                print("    %-42s %s" % (n, ", ".join(payload["undecided_detail"].get(n, []))))

    if a.baseline:
        with open(a.baseline, encoding="utf-8") as fh:
            known = json.load(fh)
        bad = []
        for k in ("write", "read", "no_sql", "undecided"):
            moved = set(payload[k]) - set(known.get(k, []))
            for m in sorted(moved):
                was = next((j for j in known if isinstance(known[j], list) and m in known[j]),
                           "(new)")
                bad.append("%s: %s -> %s" % (m, was, k))
        if bad:
            print("\nFAIL: the split has moved since the baseline:")
            for b in bad:
                print("  " + b)
            print("\nA method changing side changes which TYPE it lives on. Re-run "
                  "--update-baseline only once you have decided that is correct.")
            return 1
        print("\nOK: the read/write split is unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
