"""
v4import/transforms.py — the ordered rewrites applied to the target after the carry
(E design §4).

Each transform reads and writes only the target (`main`), may read the attached source
(`src`), and records how many rows it touched in report["transforms"]. None is a heal:
no marker, no version gate, no second run. Each is a translation applied once, to the
importer's copy.

    1  provenance          'measured' -> 'settled', the reconciled fold
    2  EV segments         from the legacy columns, where a block has none
    3  held-rate refit     segments fitted to a settled kWh
    4  band relabel        house off_peak -> night, SMB era (waits on ROADMAP BL-81)
    5  meta                the allow-listed store_meta / kraken_state keys

4 waits on BL-81 (E design §10); the others run in this order.

Blocks whose segments 2 and 3 write are recorded in the temp table `v4i_resegmented`, so
verify.py can check them by their own gates and the rest as carried exactly.
"""
import json

from . import rules

MAIN = "electricity_main"
_EPS = 1e-6                      # the spec's tolerance for "segments miss the kWh"
HELD = ("corrected", "reconciled", "settled")     # a held rate, v5's words (after 1)


def _resegmented(conn, block_start, why):
    conn.execute("INSERT OR REPLACE INTO temp.v4i_resegmented (block_start, why) VALUES (?, ?)",
                 (block_start, why))


def _write_segments(conn, block_start, segs):
    """BlockStore._replace_segments' shape, on the target: the main meter's import channel,
    in position order."""
    conn.execute("DELETE FROM main.block_segments WHERE block_start = ? AND meter_id = ? "
                 "AND channel = 'import'", (block_start, MAIN))
    for seq, seg in enumerate(segs):
        kwh, inc_rate, exc_rate, band, attribution = seg
        conn.execute("INSERT INTO main.block_segments (block_start, meter_id, channel, seq, kwh, "
                     "inc_rate, exc_rate, band, attribution) VALUES (?, ?, 'import', ?, ?, ?, ?, "
                     "?, ?)", (block_start, MAIN, seq, kwh, inc_rate, exc_rate, band, attribution))


def _vat_lookup(conn):
    """VAT on a block's date: the seed plus the source's learned calendar (as gate 4 reads it)."""
    import vat_calendar
    row = conn.execute("SELECT value FROM src.store_meta WHERE key = 'vat_calendar'").fetchone()
    learned = []
    try:
        for d, r in (json.loads(row[0]) if row and row[0] else []) or []:
            learned.append((str(d)[:10], float(r)))
    except (TypeError, ValueError):
        learned = []
    return vat_calendar.rate_lookup(learned)


def carry_keys(conn, report):
    """5: carry the allow-listed keys as they are, values untouched (store_meta values are
    JSON text, and v5 decodes them as v4 did). `written_by`, `db_uuid` and the markers are
    dropped; v5 stamps its own written_by and db_uuid at the first start (spec §6)."""
    for table in rules.KEYED_TABLES:
        carried, dropped = [], []
        cols = [r[1] for r in conn.execute('PRAGMA src.table_info("%s")' % table).fetchall()]
        if not cols:
            report["keys"][table] = {"carried": [], "dropped": []}
            continue
        sel = ", ".join('"%s"' % c for c in cols)
        for row in conn.execute('SELECT %s FROM src."%s" ORDER BY key' % (sel, table)).fetchall():
            if rules.key_rule(table, row["key"]) == "carry":
                conn.execute('INSERT OR REPLACE INTO main."%s" (%s) VALUES (%s)'
                             % (table, sel, ", ".join("?" * len(cols))), tuple(row))
                carried.append(row["key"])
            else:
                dropped.append(row["key"])
        report["keys"][table] = {"carried": carried, "dropped": dropped}


def provenance(conn, report):
    """1: v5's provenance words (spec §5.2, BL-61(5)). A bill-settled block says 'settled'
    where v4 said 'measured'. A block a dispatch reconciliation flagged (rate_reconciled = 1)
    while leaving 'schedule' or no word says 'reconciled'; a settled or corrected one keeps
    its word, which outranks a reconciliation. Every meter: v4 flagged sub-meter rows too.
    The flag column itself stays until the column drop."""
    renamed = conn.execute(
        "UPDATE main.blocks SET rate_source = 'settled' WHERE rate_source = 'measured'").rowcount
    folded = conn.execute(
        "UPDATE main.blocks SET rate_source = 'reconciled' WHERE rate_reconciled = 1 "
        "AND (rate_source IS NULL OR rate_source = 'schedule')").rowcount
    report["transforms"]["provenance"] = {"settled": renamed, "reconciled_folded": folded}


def ev_segments(conn, report):
    """2 (spec §5.2, v14): a main block with an EV split in the columns and no segments at all
    gets its segments from those columns before they are dropped, by the backfill's own
    reconstruction (`segments_from_legacy`): an EV segment at the split's rate and band, the
    house remainder at its derived rate, the costs summing to imp_cost. A block that has
    segments keeps them, whatever its columns say: the segments are what the bill priced."""
    import pricing_segments as ps
    vat_at = _vat_lookup(conn)
    rows = conn.execute(
        "SELECT b.block_start, b.imp_kwh, b.imp_cost, b.imp_rate, b.imp_rate_exc, b.imp_kwh_ev, "
        "b.imp_cost_ev, b.imp_rate_ev, b.imp_ev_band, b.imp_home_band FROM main.blocks b "
        "WHERE b.meter_id = ? AND b.imp_kwh_ev > ? AND NOT EXISTS (SELECT 1 FROM "
        "main.block_segments s WHERE s.block_start = b.block_start AND s.meter_id = b.meter_id "
        "AND s.channel = 'import') ORDER BY b.block_start", (MAIN, _EPS)).fetchall()
    kwh_ev = 0.0
    for r in rows:
        rate = float(r["imp_rate"] or 0.0)
        ratio = (float(r["imp_rate_exc"]) / rate if r["imp_rate_exc"] is not None and rate
                 else 1.0 / (1.0 + vat_at(r["block_start"])))
        segs = ps.segments_from_legacy(
            imp_kwh=float(r["imp_kwh"] or 0.0), imp_cost=float(r["imp_cost"] or 0.0),
            imp_rate=rate, kwh_ev=r["imp_kwh_ev"], cost_ev=r["imp_cost_ev"],
            rate_ev=r["imp_rate_ev"], ev_band=r["imp_ev_band"], home_band=r["imp_home_band"],
            exc_ratio=ratio)
        _write_segments(conn, r["block_start"], segs)
        _resegmented(conn, r["block_start"], "ev_segments")
        kwh_ev += float(r["imp_kwh_ev"])
    report["transforms"]["ev_segments"] = {"blocks": len(rows), "kwh_ev": round(kwh_ev, 6)}


def refit_held(conn, report):
    """3 (spec §5.2, v16): a held-rate main block whose segments miss its kWh is fitted to it
    (`refit_to_kwh`, the function v5's own settlement re-run uses). v4 re-costs such a block
    to a later settled kWh and leaves its segments (BL-79 (1), fixed in v5 only). No segment is
    re-priced: the house takes the change; only once it is gone does the EV shrink; a
    house-less block gains a house segment at the held rate. Every column is carried as it is.

    A main block whose segments miss its kWh and whose rate is NOT held is reported, not
    refitted: its cause is not this one, and a refit would hide it."""
    import pricing_segments as ps
    rows = conn.execute(
        "SELECT b.block_start, b.imp_kwh, b.imp_rate, b.imp_rate_exc, b.imp_home_band, "
        "b.rate_source, SUM(s.kwh) AS seg_kwh FROM main.blocks b JOIN main.block_segments s "
        "ON s.block_start = b.block_start AND s.meter_id = b.meter_id AND s.channel = 'import' "
        "WHERE b.meter_id = ? GROUP BY b.block_start HAVING ABS(SUM(s.kwh) - "
        "COALESCE(b.imp_kwh, 0)) > ? ORDER BY b.block_start", (MAIN, _EPS)).fetchall()
    refitted, reported = [], []
    for r in rows:
        if r["rate_source"] not in HELD:
            reported.append(r["block_start"])
            continue
        segs = [ps.Segment(x["kwh"], x["inc_rate"], x["exc_rate"], x["band"], x["attribution"])
                for x in conn.execute(
                    "SELECT kwh, inc_rate, exc_rate, band, attribution FROM main.block_segments "
                    "WHERE block_start = ? AND meter_id = ? AND channel = 'import' ORDER BY seq",
                    (r["block_start"], MAIN))]
        hb = r["imp_home_band"]
        new = ps.refit_to_kwh(segs, float(r["imp_kwh"] or 0.0), rate=r["imp_rate"],
                              exc_rate=r["imp_rate_exc"],
                              band=hb if hb in ("off_peak", "day") else "standard")
        _write_segments(conn, r["block_start"], new)
        _resegmented(conn, r["block_start"], "refit")
        refitted.append(r["block_start"])
    report["transforms"]["refit_held"] = {"refitted": len(refitted), "reported": reported}


def run(conn, report):
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS v4i_resegmented "
                 "(block_start TEXT PRIMARY KEY, why TEXT)")
    conn.execute("DELETE FROM temp.v4i_resegmented")
    provenance(conn, report)
    ev_segments(conn, report)
    refit_held(conn, report)
    carry_keys(conn, report)
