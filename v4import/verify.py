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

# Rows a transform replaces wholesale: the segments of the blocks transforms 2 and 3 wrote
# (temp.v4i_resegmented). Counted and compared exactly everywhere else; checked by
# `ev_segments` and `held_fit` where they were written. The id is not compared: a rewritten
# block's rows are new rows.
_UNTOUCHED = {"block_segments": "WHERE block_start NOT IN (SELECT block_start FROM "
                                "temp.v4i_resegmented)"}
_HELD_SRC = ("(rate_source IN ('corrected', 'reconciled', 'measured') OR (rate_reconciled = 1 "
             "AND (rate_source IS NULL OR rate_source = 'schedule')))")
_SEG = ("(SELECT block_start, SUM(kwh) AS k, SUM(kwh * inc_rate) AS c, "
        "SUM(CASE WHEN attribution = 'ev' THEN kwh ELSE 0 END) AS ev, "
        "SUM(CASE WHEN attribution = 'ev' THEN kwh * inc_rate ELSE 0 END) AS ec FROM %s.block_segments "
        "WHERE meter_id = 'electricity_main' AND channel = 'import' GROUP BY block_start)")


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
        where = _UNTOUCHED.get(table, "")
        a = _one(conn, 'SELECT COUNT(*) FROM src."%s" %s' % (table, where))
        b = _one(conn, 'SELECT COUNT(*) FROM main."%s" %s' % (table, where))
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
            where = _UNTOUCHED.get(table, "")
            n = _one(conn, 'SELECT COUNT(*) FROM (SELECT %s FROM %s."%s" %s EXCEPT '
                           'SELECT %s FROM %s."%s" %s)' % (sel, a, table, where,
                                                         sel, b, table, where))
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


def ev_segments(conn, report):
    """Spec §5.2 v14: no main block keeps an EV split with no segments, and each block given
    them reproduces its columns: kWh to imp_kwh, the EV to imp_kwh_ev, the cost to imp_cost.
    The count equals the source's own (blocks with an EV split and no segments)."""
    want = _one(conn, "SELECT COUNT(*) FROM src.blocks b WHERE b.meter_id = 'electricity_main' "
                      "AND b.imp_kwh_ev > 1e-6 AND NOT EXISTS (SELECT 1 FROM src.block_segments s "
                      "WHERE s.block_start = b.block_start AND s.meter_id = b.meter_id "
                      "AND s.channel = 'import')")
    got = report["transforms"].get("ev_segments", {}).get("blocks", 0)
    report["gates"]["ev_segments"]["blocks"] = got
    if got != want:
        return "EV segments written for %d block(s), expected %d" % (got, want)
    # the EV split is a v4-only column (rules.DROP_COLUMNS): the source's, joined on id
    left = _one(conn, "SELECT COUNT(*) FROM src.blocks v WHERE v.meter_id = 'electricity_main' "
                      "AND v.imp_kwh_ev > 1e-6 AND NOT EXISTS (SELECT 1 FROM main.block_segments "
                      "s WHERE s.block_start = v.block_start AND s.meter_id = v.meter_id "
                      "AND s.channel = 'import')")
    bad = _one(conn, "SELECT COUNT(*) FROM main.blocks b JOIN src.blocks v ON v.id = b.id "
                     "JOIN " + _SEG % "main" + " g "
                     "ON g.block_start = b.block_start JOIN temp.v4i_resegmented t "
                     "ON t.block_start = b.block_start AND t.why = 'ev_segments' "
                     "WHERE b.meter_id = 'electricity_main' AND (ABS(g.k - b.imp_kwh) > 1e-6 "
                     "OR ABS(g.ev - v.imp_kwh_ev) > 1e-6 OR ABS(g.c - b.imp_cost) > 1e-5)")
    if left or bad:
        return ("%d EV-split block(s) still without segments, %d not reproducing their columns"
                % (left, bad))
    return None


def split_reproduced(conn, report):
    """Spec §9.5: the dropped EV-split columns reproduce from the segments for EVERY main block,
    at stored precision — the gate that makes the column drop safe. EV kWh to imp_kwh_ev, EV £ to
    imp_cost_ev, and no block with EV segments where the columns said none. The one expected
    difference is a held block whose refit shrank the EV because the house was gone (gate 11's
    note): there the segments' EV is smaller than the column's, never larger. Reports the worst
    divergence (£), not just pass/fail."""
    seg = _SEG % "main"
    base = ("FROM main.blocks b JOIN src.blocks v ON v.id = b.id LEFT JOIN " + seg + " g "
            "ON g.block_start = b.block_start "
            "LEFT JOIN temp.v4i_resegmented t ON t.block_start = b.block_start "
            "WHERE b.meter_id = 'electricity_main' ")
    shrunk = "(t.why = 'refit' AND COALESCE(g.ev, 0) <= v.imp_kwh_ev + 1e-6)"
    kwh = _one(conn, "SELECT COUNT(*) " + base + "AND v.imp_kwh_ev > 1e-6 AND "
                     "ABS(COALESCE(g.ev, 0) - v.imp_kwh_ev) > 1e-6 AND NOT " + shrunk)
    cost = _one(conn, "SELECT COUNT(*) " + base + "AND v.imp_kwh_ev > 1e-6 AND "
                      "ABS(COALESCE(g.ec, 0) - COALESCE(v.imp_cost_ev, 0)) > 1e-5 AND NOT " + shrunk)
    phantom = _one(conn, "SELECT COUNT(*) " + base + "AND COALESCE(v.imp_kwh_ev, 0) <= 1e-6 "
                         "AND g.ev > 1e-6")
    worst = _one(conn, "SELECT MAX(ABS(COALESCE(g.ec, 0) - COALESCE(v.imp_cost_ev, 0))) " + base
                       + "AND v.imp_kwh_ev > 1e-6 AND NOT " + shrunk)
    report["gates"]["split_reproduced"].update(
        worst_cost=round(worst or 0.0, 9), kwh_differ=kwh, cost_differ=cost, phantom=phantom)
    if kwh or cost or phantom:
        return ("the EV segments do not reproduce the EV-split columns: %d kWh, %d £, %d with EV "
                "segments where the columns had none" % (kwh, cost, phantom))
    return None


def held_fit(conn, report):
    """Spec §9.11: no held main block's segments miss its kWh by more than 1e-6. The refitted
    and reported counts equal a query of the source, and no segment was re-priced: every rate
    in a refitted block's segments was in its source segments, or is the block's held rate."""
    miss = ("SELECT b.block_start FROM src.blocks b JOIN " + _SEG % "src" + " g ON "
            "g.block_start = b.block_start WHERE b.meter_id = 'electricity_main' "
            "AND ABS(g.k - COALESCE(b.imp_kwh, 0)) > 1e-6")
    want_refit = _one(conn, "SELECT COUNT(*) FROM (%s AND %s)" % (miss, _HELD_SRC.replace(
        "rate_", "b.rate_")))
    want_report = _one(conn, "SELECT COUNT(*) FROM (%s AND NOT %s)" % (miss, _HELD_SRC.replace(
        "rate_", "b.rate_")))
    t = report["transforms"].get("refit_held", {"refitted": 0, "reported": []})
    report["gates"]["held_fit"].update(refitted=t["refitted"], reported=len(t["reported"]))
    if (t["refitted"], len(t["reported"])) != (want_refit, want_report):
        return ("held-rate refit: %d refitted and %d reported, expected %d and %d"
                % (t["refitted"], len(t["reported"]), want_refit, want_report))
    still = _one(conn, "SELECT COUNT(*) FROM main.blocks b JOIN " + _SEG % "main" + " g ON "
                       "g.block_start = b.block_start WHERE b.meter_id = 'electricity_main' "
                       "AND b.rate_source IN ('corrected', 'reconciled', 'settled') "
                       "AND ABS(g.k - COALESCE(b.imp_kwh, 0)) > 1e-6")
    repriced = _one(conn, "SELECT COUNT(*) FROM main.block_segments m JOIN main.blocks b ON "
                          "b.block_start = m.block_start AND b.meter_id = m.meter_id "
                          "JOIN temp.v4i_resegmented t ON t.block_start = m.block_start "
                          "AND t.why = 'refit' WHERE m.meter_id = 'electricity_main' "
                          "AND m.inc_rate IS NOT b.imp_rate AND NOT EXISTS (SELECT 1 FROM "
                          "src.block_segments s WHERE s.block_start = m.block_start "
                          "AND s.meter_id = m.meter_id AND s.inc_rate IS m.inc_rate)")
    if still or repriced:
        return ("%d held block(s) still miss their kWh, %d segment(s) re-priced by the refit"
                % (still, repriced))
    return None


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
         ("ev_segments", ev_segments), ("split_reproduced", split_reproduced),
         ("held_fit", held_fit),
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
