"""
v4import/verify.py — the import reconciles with its source, or it does not commit
(import spec §9; E design §6).

Runs inside the import's transaction, with the source still attached, so every gate
compares the target (`main`) with the source (`src`) directly. Returns the failures as
one-line descriptions, empty when every gate holds. Each gate's result also goes into
report["gates"], and from there into the audit record (spec §9.9).

"Carried exactly" is the broad gate: every carried column of every carried table holds
the source's rows, no more and no fewer, compared at stored precision (spec §9.5: never
rounded display values). The named gates below it state the spec's invariants one by
one, so each keeps its own message once the transforms change some of those columns.
"""
from . import rules

# Columns a transform rewrites, by table: excluded from "carried exactly" and checked by
# their own gate instead.
REWRITTEN = {"blocks": ("rate_source",)}           # transform 1, checked by `provenance`


def _src_cols(conn, table):
    return [r[1] for r in conn.execute('PRAGMA src.table_info("%s")' % table).fetchall()]


def _one(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


def intact(conn):
    rows = [tuple(r) for r in conn.execute("PRAGMA main.quick_check").fetchall()]
    if rows != [("ok",)]:
        return "the imported database failed its integrity check"
    if conn.execute("PRAGMA main.foreign_key_check").fetchone() is not None:
        return "the imported database has a broken reference"
    return None


def row_counts(conn, report):
    """Spec §9.1."""
    bad = []
    for table, _ in rules.CARRY:
        if not _src_cols(conn, table):
            continue
        a = _one(conn, 'SELECT COUNT(*) FROM src."%s"' % table)
        b = _one(conn, 'SELECT COUNT(*) FROM main."%s"' % table)
        if a != b:
            bad.append("%s %d of %d rows" % (table, b, a))
    return ("row counts: " + ", ".join(bad)) if bad else None


def carried_exactly(conn, report):
    bad = []
    for table, cols in rules.CARRY:
        have = set(_src_cols(conn, table))
        sel = ", ".join('"%s"' % c for c in cols
                        if c in have and c not in REWRITTEN.get(table, ()))
        if not sel:
            continue
        for a, b in (("src", "main"), ("main", "src")):
            n = _one(conn, 'SELECT COUNT(*) FROM (SELECT %s FROM %s."%s" EXCEPT '
                           'SELECT %s FROM %s."%s")' % (sel, a, table, sel, b, table))
            if n:
                bad.append("%s: %d row(s) differ" % (table, n))
                break
    return ("not carried exactly: " + ", ".join(bad)) if bad else None


def cost_parity(conn, report):
    """Spec §9.2, per block: a block's bill does not move by a penny, or by anything."""
    n = _one(conn, "SELECT COUNT(*) FROM src.blocks s LEFT JOIN main.blocks m ON m.id = s.id "
                   "WHERE m.id IS NULL OR m.imp_cost IS NOT s.imp_cost "
                   "OR m.exp_cost IS NOT s.exp_cost OR m.imp_cost_exc IS NOT s.imp_cost_exc")
    report["gates"]["cost_parity"]["blocks_differing"] = n
    return ("%d block cost(s) changed" % n) if n else None


def provenance(conn, report):
    """Spec §9.3: counts by provenance word match the source's, read through spec §5.2's
    rule. Worked out here from the source's (word, flag) pairs, not by re-running the
    transform's SQL, so a faulty transform cannot agree with itself. Every meter."""
    want = {}
    for word, flag, n in conn.execute(
            "SELECT rate_source, rate_reconciled, COUNT(*) FROM src.blocks GROUP BY 1, 2"):
        if word == "measured":
            word = "settled"
        elif flag == 1 and word in (None, "schedule"):
            word = "reconciled"
        want[word or ""] = want.get(word or "", 0) + n
    got = dict(conn.execute("SELECT COALESCE(rate_source, ''), COUNT(*) FROM main.blocks "
                            "GROUP BY 1").fetchall())
    report["gates"]["provenance"]["counts"] = got
    if got != want:
        return "provenance counts moved: expected %s, got %s" % (want, got)
    # and row by row: a block's word may change only by that rule
    n = conn.execute(
        "SELECT COUNT(*) FROM src.blocks s JOIN main.blocks m ON m.id = s.id WHERE "
        "m.rate_source IS NOT s.rate_source AND NOT ("
        "(s.rate_source = 'measured' AND m.rate_source = 'settled') OR "
        "(s.rate_reconciled = 1 AND (s.rate_source IS NULL OR s.rate_source = 'schedule') "
        " AND m.rate_source = 'reconciled'))").fetchone()[0]
    return ("%d block(s) changed provenance outside the rule" % n) if n else None


def corrections(conn, report):
    """Spec §9.4: every corrected block keeps its corrected rate and cost."""
    n = _one(conn, "SELECT COUNT(*) FROM src.blocks s LEFT JOIN main.blocks m ON m.id = s.id "
                   "WHERE s.rate_corrected = 1 AND (m.id IS NULL OR m.rate_corrected IS NOT 1 "
                   "OR m.imp_rate IS NOT s.imp_rate OR m.imp_cost IS NOT s.imp_cost)")
    report["gates"]["corrections"]["kept"] = _one(
        conn, "SELECT COUNT(*) FROM main.blocks WHERE rate_corrected = 1")
    return ("%d corrected block(s) lost their correction" % n) if n else None


def tombstones(conn, report):
    """Spec §9.6, the import's half: every deliberate delete is carried. (That a backfill
    does not re-create one is the engine's behaviour, tested there.)"""
    n = _one(conn, "SELECT COUNT(*) FROM (SELECT meter_id, start_utc, end_utc FROM "
                   "src.deleted_ranges EXCEPT SELECT meter_id, start_utc, end_utc FROM "
                   "main.deleted_ranges)")
    return ("%d deleted range(s) not carried" % n) if n else None


def keys(conn, report):
    """Every carried key holds the source's value."""
    bad = []
    for table in rules.KEYED_TABLES:
        for k in report["keys"].get(table, {}).get("carried", []):
            a = conn.execute('SELECT value FROM src."%s" WHERE key = ?' % table, (k,)).fetchone()
            b = conn.execute('SELECT value FROM main."%s" WHERE key = ?' % table, (k,)).fetchone()
            if b is None or a[0] != b[0]:
                bad.append(k)
    return ("keys not carried: " + ", ".join(bad)) if bad else None


GATES = (("row_counts", row_counts), ("carried_exactly", carried_exactly),
         ("cost_parity", cost_parity), ("provenance", provenance),
         ("corrections", corrections), ("tombstones", tombstones), ("keys", keys))


def run(conn, report):
    failures = []
    for name, gate in GATES:
        report["gates"][name] = {}
        why = gate(conn, report)
        report["gates"][name]["ok"] = why is None
        if why:
            failures.append(why)
    why = intact(conn)
    report["gates"]["intact"] = {"ok": why is None}
    if why:
        failures.append(why)
    return failures
