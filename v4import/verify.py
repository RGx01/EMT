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
import json

from . import rules

# Columns a transform rewrites, by table: excluded from "carried exactly" and checked by
# their own gate instead.
# rate_source: transform 1, checked by `provenance`; needs_pass2_rerun: transform 5's settlement
# source (spec v25), checked by `billing_source`.
REWRITTEN = {"blocks": ("rate_source", "needs_pass2_rerun")}

# Rows a transform replaces wholesale: the segments of the blocks transforms 2 and 3 wrote
# (temp.v4i_resegmented). Counted and compared exactly everywhere else; checked by
# `ev_segments` and `held_fit` where they were written. The id is not compared: a rewritten
# block's rows are new rows.
_UNTOUCHED = {"block_segments": "WHERE block_start NOT IN (SELECT block_start FROM "
                                "temp.v4i_resegmented)",
              # transform 3c's bills (temp.v4i_bills) are new rows, checked by `imported_bills`
              "measured_cost": "WHERE slot_start NOT IN (SELECT slot_start FROM temp.v4i_bills)"}
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
    divergence (£), not just pass/fail.

    The £ comparison skips a block transform 2 rebuilt from the columns: the reconstruction
    gives an all-EV block's EV segment the whole bill so the segments sum to imp_cost, and
    imp_cost_ev is kWh × rate, which differs wherever the bill does (#505's 5% October bill on a
    0% rate). Those blocks are judged by `ev_segments` against the reconstruction's own rule;
    their kWh is still compared here (10 Oct 2026, found on a soak copy whose segments the prep
    had stripped)."""
    seg = _SEG % "main"
    base = ("FROM main.blocks b JOIN src.blocks v ON v.id = b.id LEFT JOIN " + seg + " g "
            "ON g.block_start = b.block_start "
            # a block's records together: 3 can refit it and 3b then move it
            "LEFT JOIN (SELECT block_start, MAX(why = 'refit') AS refit, "
            "  MAX(why = 'bill_split') AS moved, MAX(why = 'ev_segments') AS rebuilt "
            "  FROM temp.v4i_resegmented GROUP BY block_start) t ON t.block_start = b.block_start "
            "WHERE b.meter_id = 'electricity_main' ")
    # a refit that actually shrank the EV (by more than rounding); a refit that left the EV as
    # it was is compared like any other block
    shrunk = "(COALESCE(t.refit, 0) = 1 AND COALESCE(g.ev, 0) < v.imp_kwh_ev - 1e-6)"
    # transform 3b moved the EV to the bill's on purpose: judged by `bill_split`, not the column
    moved = "COALESCE(t.moved, 0) = 1"
    kwh = _one(conn, "SELECT COUNT(*) " + base + "AND v.imp_kwh_ev > 1e-6 AND "
                     "ABS(COALESCE(g.ev, 0) - v.imp_kwh_ev) > 1e-6 AND NOT " + shrunk
                     + " AND NOT " + moved)
    rebuilt = "COALESCE(t.rebuilt, 0) = 1"
    cost = _one(conn, "SELECT COUNT(*) " + base + "AND v.imp_kwh_ev > 1e-6 AND "
                      "ABS(COALESCE(g.ec, 0) - COALESCE(v.imp_cost_ev, 0)) > 1e-5 AND NOT " + shrunk
                      + " AND NOT " + rebuilt + " AND NOT " + moved)
    phantom = _one(conn, "SELECT COUNT(*) " + base + "AND COALESCE(v.imp_kwh_ev, 0) <= 1e-6 "
                         "AND g.ev > 1e-6 AND NOT " + moved)
    worst = _one(conn, "SELECT MAX(ABS(COALESCE(g.ec, 0) - COALESCE(v.imp_cost_ev, 0))) " + base
                       + "AND v.imp_kwh_ev > 1e-6 AND NOT " + shrunk + " AND NOT " + rebuilt
                       + " AND NOT " + moved)
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
                          "AND s.meter_id = m.meter_id AND s.inc_rate IS m.inc_rate) "
                          # a block 3b then moved holds 3b's segments (its house leg the
                          # residual absorber): its refit is judged below, by what 3b started from
                          "AND NOT EXISTS (SELECT 1 FROM temp.v4i_resegmented x WHERE "
                          "x.block_start = m.block_start AND x.why = 'bill_split')")
    # refitted, then moved by 3b: the refit's result (3b's starting point) sums to the kWh and
    # costs the block's own stored cost — the held rate, not re-priced
    repriced += _one(conn, "SELECT COUNT(*) FROM main.blocks b JOIN temp.v4i_split_from f ON "
                           "f.block_start = b.block_start JOIN temp.v4i_resegmented t ON "
                           "t.block_start = b.block_start AND t.why = 'refit' "
                           "WHERE b.meter_id = 'electricity_main' AND (ABS(f.k - b.imp_kwh) > 1e-6 "
                           "OR ABS(f.c - b.imp_cost) > 1e-5)")
    if still or repriced:
        return ("%d held block(s) still miss their kWh, %d segment(s) re-priced by the refit"
                % (still, repriced))
    return None


def bill_split(conn, report):
    """Bill and split paths design §7.1 (transform 3b): each block 3b moved now holds the bill's EV
    clipped to its kWh, its segments sum to its kWh, carry one rate (within 0.001p/kWh), and cost what its
    segments cost before the move (Σ kWh × rate, to 1e-5) — the attribution moved, nothing else. A block it reported
    (more than one rate, or the 4.5.23 case) kept its source segments.

    "Before the move" is its source segments — or, for a block transform 3 refitted first, what 3b
    started from (`v4i_split_from`), which the `held_fit` gate checks against the source on its own
    terms (19 Oct 2026: the staging import of prod refused 4 such blocks, judged against segments
    the refit had correctly changed)."""
    t = report["transforms"].get("bill_split", {"moved": 0, "reported_mixed": [],
                                                 "reported_held": []})
    report["gates"]["bill_split"].update(moved=t["moved"], reported=len(t["reported_mixed"])
                                         + len(t["reported_held"]))
    sel = ("FROM main.blocks b JOIN temp.v4i_resegmented t ON t.block_start = b.block_start "
           "AND t.why = 'bill_split' JOIN " + _SEG % "main" + " g ON g.block_start = b.block_start "
           "JOIN " + _SEG % "src" + " o ON o.block_start = b.block_start "
           "JOIN temp.v4i_split_from f ON f.block_start = b.block_start "
           "JOIN main.measured_cost m ON m.rowid = (SELECT m2.rowid FROM main.measured_cost m2 "
           "  WHERE m2.slot_start = b.block_start AND m2.direction = 'CONSUMPTION' "
           "  ORDER BY m2.fetched_at DESC LIMIT 1) WHERE b.meter_id = 'electricity_main' ")
    # the cost before the move: the source's, unless transform 3 refitted the block first
    before = ("(CASE WHEN EXISTS (SELECT 1 FROM temp.v4i_resegmented r WHERE r.block_start = "
              "b.block_start AND r.why = 'refit') THEN f.c ELSE o.c END)")
    n = _one(conn, "SELECT COUNT(*) " + sel)
    bad = _one(conn, "SELECT COUNT(*) " + sel + "AND (ABS(g.k - b.imp_kwh) > 1e-6 "
                     "OR ABS(g.ev - MIN(m.ev_kwh, b.imp_kwh)) > 1e-6 OR ABS(g.c - " + before + ") > 1e-5 "
                     "OR (SELECT MAX(s.inc_rate) - MIN(s.inc_rate) FROM main.block_segments s "
                     "WHERE s.block_start = b.block_start AND s.meter_id = b.meter_id "
                     "AND s.channel = 'import') > 1e-5)")
    kept = [bs for bs in t["reported_mixed"] + t["reported_held"]
            if _one(conn, "SELECT COUNT(*) FROM temp.v4i_resegmented WHERE block_start = ? "
                          "AND why = 'bill_split'", bs)]
    if n != t["moved"] or bad or kept:
        return ("the bill's split: %d moved, %d recorded, %d not the bill's or not costing what "
                "they did, %d reported yet moved" % (t["moved"], n, bad, len(kept)))
    return None


def imported_bills(conn, report):
    """Engine step 4 design §6 (e) (transform 3c): each bill 3c wrote is its block's — an imported main
    half-hour — with its cost, ex-VAT and kWh and the label IMPORTED; and it wrote one for every
    imported half-hour with kWh and a cost that had no bill."""
    from .transforms import IMPORTED_LABEL, IMPORTED_SOURCES
    t = report["transforms"].get("imported_bills", {"written": 0})
    n = _one(conn, "SELECT COUNT(*) FROM temp.v4i_bills")
    bad = _one(conn,
               "SELECT COUNT(*) FROM temp.v4i_bills t JOIN main.measured_cost m ON m.slot_start = "
               "t.slot_start AND m.direction = 'CONSUMPTION' LEFT JOIN main.blocks b ON "
               "b.block_start = t.slot_start AND b.meter_id = 'electricity_main' "
               "WHERE b.id IS NULL OR b.source NOT IN " + IMPORTED_SOURCES + " "
               "OR m.cost_incl IS NOT b.imp_cost OR m.cost_excl IS NOT b.imp_cost_exc "
               "OR m.kwh IS NOT b.imp_kwh OR m.label IS NOT ?", IMPORTED_LABEL)
    missed = 0 if t.get("skipped") else _one(
        conn, "SELECT COUNT(*) FROM main.blocks b WHERE b.meter_id = 'electricity_main' "
              "AND b.source IN " + IMPORTED_SOURCES + " AND COALESCE(b.imp_kwh, 0) > 1e-6 "
              "AND b.imp_cost IS NOT NULL AND NOT EXISTS (SELECT 1 FROM main.measured_cost m "
              "WHERE m.slot_start = b.block_start AND m.direction = 'CONSUMPTION')")
    report["gates"]["imported_bills"].update(written=t["written"], skipped=t.get("skipped"))
    if n != t["written"] or bad or missed:
        return ("imported bills: %d written, %d recorded, %d not their block's cost, "
                "%d imported half-hour(s) left without one" % (t["written"], n, bad, missed))
    return None


def sensor_rates(conn, report):
    """Transform 3d: every `sensor_rates` row is a live main half-hour's — no source, not corrected —
    at that block's stored rate, in a config period whose import channel names a rate sensor, and
    before the go-live where that channel's rate_source is 'api'; and as many as 3d wrote."""
    t = report["transforms"].get("sensor_rates", {"written": 0})
    report["gates"]["sensor_rates"].update(written=t.get("written", 0), go_live=t.get("go_live"))
    n = _one(conn, "SELECT COUNT(*) FROM main.sensor_rates")
    bad = _one(conn,
               "SELECT COUNT(*) FROM main.sensor_rates r LEFT JOIN src.blocks b ON "
               "b.block_start = r.block_start AND b.meter_id = r.meter_id "
               "LEFT JOIN src.meters m ON m.meter_id = b.meter_id AND m.config_period_id = b.config_period_id "
               "LEFT JOIN src.meter_channels mc ON mc.meter_id = m.id AND mc.channel = 'import' "
               "WHERE b.id IS NULL OR b.source IS NOT NULL OR r.rate IS NOT b.imp_rate "
               "OR COALESCE(b.rate_corrected, 0) <> 0 OR COALESCE(mc.rate_sensor, '') = '' "
               "OR (mc.rate_source = 'api' AND (? IS NULL OR r.block_start >= ?))",
               t.get("go_live"), t.get("go_live"))
    if n != t.get("written", 0) or bad:
        return ("sensor rates: %d written, %d held, %d not a live sensor-era half-hour's rate"
                % (t.get("written", 0), n, bad))
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


def billing_source(conn, report):
    """Transform 5's re-run (spec v25): every main-meter block flagged where v4 chose its own
    readings on an API mode, and every other block's flag as the source held it."""
    rerun = (report.get("billing_source") or {}).get("rerun")
    main = "m.meter_id IN (SELECT meter_id FROM main.meters WHERE is_sub_meter = 0)"
    bad = _one(conn, "SELECT COUNT(*) FROM main.blocks m WHERE %s AND m.needs_pass2_rerun != 1"
               % main) if rerun else 0
    bad += _one(conn, "SELECT COUNT(*) FROM main.blocks m JOIN src.blocks s ON s.id = m.id "
                      "WHERE %s AND m.needs_pass2_rerun IS NOT s.needs_pass2_rerun"
                % (("NOT (%s)" % main) if rerun else "1"))
    report["gates"]["billing_source"]["differ"] = bad
    return ("settlement source re-run: %d block(s) not as expected" % bad) if bad else None


def device_sensors(conn, report):
    """Transform 6, against the SOURCE: every channel of its current config period with a read
    sensor has that sensor, and only it, as its primary; every sensor a sub-meter's Device History
    runs used is that device's primary or one of its secondaries. (An entry twice cannot be:
    the table is unique on device, channel and entity.)"""
    t = report["transforms"].get("device_sensors", {"primaries": 0, "secondaries": 0})
    report["gates"]["device_sensors"].update(primaries=t["primaries"], secondaries=t["secondaries"])
    pid = conn.execute("SELECT id FROM src.config_periods WHERE effective_to IS NULL "
                       "ORDER BY effective_from DESC LIMIT 1").fetchone()
    if pid is None:
        return None
    wrong = _one(conn,
        "SELECT COUNT(*) FROM src.meters m JOIN src.meter_channels mc ON mc.meter_id = m.id "
        "WHERE m.config_period_id = ? AND COALESCE(mc.read_sensor, '') != '' AND "
        "(SELECT COUNT(*) FROM main.device_sensors s WHERE s.meter_id = m.meter_id AND "
        " s.channel = mc.channel AND s.role = 'primary' AND s.entity_id = mc.read_sensor) != 1",
        pid[0])
    raw = conn.execute("SELECT value FROM src.store_meta WHERE key = "
                       "'recorder_attribution_runs'").fetchone()
    try:
        runs = json.loads(raw[0]) if raw and raw[0] else []
    except (TypeError, ValueError):
        runs = []
    subs = {r[0] for r in conn.execute("SELECT meter_id FROM src.meters WHERE config_period_id = ? "
                                       "AND is_sub_meter = 1", (pid[0],))}
    lost = 0
    for r in runs if isinstance(runs, list) else []:
        mid = r.get("meter_id") if isinstance(r, dict) else None
        if mid not in subs:
            continue
        for e in r.get("sensor_ids") or []:
            if e and not _one(conn, "SELECT COUNT(*) FROM main.device_sensors WHERE meter_id = ? "
                                    "AND channel = 'import' AND entity_id = ?", mid, e):
                lost += 1
    if wrong or lost:
        return ("device sensors: %d configured read(s) not their channel's primary, %d Device "
                "History sensor(s) not carried" % (wrong, lost))
    return None


def ha_reads(conn, report):
    """Transform 7, against the SOURCE: one live row per live channel holding both registers, and
    the recorder rows' kWh the source's recorder_attributed kWh, per device, within the split's
    rounding (6 dp per half-hour)."""
    t = report["transforms"].get("ha_reads", {})
    report["gates"]["ha_reads"].update(t)
    want = sum(_one(conn,
        "SELECT COUNT(*) FROM src.blocks WHERE (source IS NULL OR source = 'kraken_api') "
        "AND {c}_kwh IS NOT NULL AND {c}_read_start IS NOT NULL AND {c}_read_end IS NOT NULL "
        "AND NOT ({c}_read_start = 0 AND {c}_read_end = 0 AND meter_id NOT IN (SELECT meter_id "
        "FROM src.meters WHERE is_sub_meter = 1))".replace("{c}", c)) for c in ("imp", "exp"))
    have = _one(conn, "SELECT COUNT(*) FROM main.ha_reads WHERE origin = 'live'")
    off = []
    for mid, n, k in conn.execute(
            "SELECT meter_id, COUNT(*), SUM(imp_kwh) FROM src.blocks WHERE source = "
            "'recorder_attributed' AND imp_kwh IS NOT NULL GROUP BY meter_id").fetchall():
        got = _one(conn, "SELECT COALESCE(SUM(kwh), 0) FROM main.ha_reads WHERE meter_id = ? "
                         "AND origin = 'recorder'", mid)
        if abs(float(got) - float(k)) > 1e-6 * n:
            off.append(mid)
    if want != have or off:
        return ("HA's reads: %d live row(s) for %d live channel(s); recorder kWh not the "
                "source's for %s" % (have, want, ", ".join(off) or "none"))
    return None


GATES = (("row_counts", row_counts), ("carried_exactly", carried_exactly),
         ("cost_parity", cost_parity), ("provenance", provenance),
         ("ev_segments", ev_segments), ("split_reproduced", split_reproduced),
         ("held_fit", held_fit), ("bill_split", bill_split), ("imported_bills", imported_bills),
         ("sensor_rates", sensor_rates),
         ("corrections", corrections), ("tombstones", tombstones), ("keys", keys),
         ("billing_source", billing_source), ("device_sensors", device_sensors),
         ("ha_reads", ha_reads))


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
