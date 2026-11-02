#!/usr/bin/env python3
"""
Prepare a COPY of a v4 database so the soak gets one heavy, local-only write window.

    python3 .build/soak_prepare_db.py SOURCE.db OUT_DIR

WHAT IT DOES, to a copy (the source is opened read-only and never modified):

  1. Copies SOURCE to OUT_DIR/blocks.db with SQLite's backup API, so a live WAL is
     captured consistently.
  2. Snapshots every IMPORT `block_segments` row on non-sub-meter blocks to
     OUT_DIR/segments_before.db.
  3. Deletes those rows from the copy and clears the `settle_history_state` marker.

On the next start the engine's settle sweep (engine step 4d; it replaced the history
re-price sweep in 4e) walks the whole history, finds every priced half-hour's segments
differ from settle()'s — they are gone — and writes them, through the writer, day by day.

WHY THIS AND NOT A ROUTE. Every route that re-prices history re-fetches from Octopus
(`/api/historical/reprice-repair` re-queries Measurements window by window). This
sweep does not: it fetches one rate schedule per tariff agreement per channel, then
re-prices from the database. It is also ONE-SHOT by construction: it marks itself
done when it reaches the newest block, so it cannot repeat within a soak.

After the soak, `soak.py --compare-segments` diffs the rebuilt segments against
segments_before.db. A difference is a finding to read, not automatically a failure:
the sweep re-derives with today's code, which may legitimately differ from the code
that wrote the originals.
"""
import argparse
import json
import os
import sqlite3
import sys

MARKER = "settle_history_state"       # engine._SETTLE_HISTORY_MARKER

# Priced main-meter half-hours: settle() writes their segments (or, where it declines one,
# the fallback does from its own price — engine._settle_fallback). Only these rows are
# removed, so the sweep rebuilds exactly what lost its segments — no more, no fewer.
_MAIN_PRICED = ("b.imp_kwh IS NOT NULL AND b.imp_kwh > 0 AND b.imp_cost IS NOT NULL "
                "AND b.meter_id NOT IN (SELECT meter_id FROM meters WHERE is_sub_meter = 1)")
_SEG_WHERE = ("s.channel = 'import' AND EXISTS (SELECT 1 FROM blocks b WHERE "
              "b.block_start = s.block_start AND b.meter_id = s.meter_id AND " + _MAIN_PRICED + ")")


def _meta(conn, key):
    row = conn.execute("SELECT value FROM store_meta WHERE key = ?", (key,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return row[0]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("source")
    ap.add_argument("out_dir")
    a = ap.parse_args(argv)

    src = os.path.abspath(a.source)
    out_dir = os.path.abspath(a.out_dir)
    out_db = os.path.join(out_dir, "blocks.db")
    snap_db = os.path.join(out_dir, "segments_before.db")
    if not os.path.isfile(src):
        sys.exit("no such database: %s" % src)
    if os.path.dirname(src) == out_dir:
        sys.exit("OUT_DIR must not be the source's own directory")
    for p in (out_db, snap_db):
        if os.path.exists(p):
            sys.exit("refusing to overwrite %s" % p)
    os.makedirs(out_dir, exist_ok=True)

    with sqlite3.connect("file:%s?mode=ro" % src, uri=True) as s:
        written_by = _meta(s, "written_by")
        with sqlite3.connect(out_db) as d:
            s.backup(d)

    conn = sqlite3.connect(out_db)
    try:
        conn.execute("ATTACH DATABASE ? AS snap", (snap_db,))
        conn.execute("CREATE TABLE snap.block_segments AS "
                     "SELECT s.* FROM block_segments s WHERE " + _SEG_WHERE)
        n_snap = conn.execute("SELECT COUNT(*) FROM snap.block_segments").fetchone()[0]
        n_blocks = conn.execute("SELECT COUNT(DISTINCT block_start || '|' || meter_id) "
                                "FROM snap.block_segments").fetchone()[0]
        conn.commit()
        conn.execute("DETACH DATABASE snap")
        with conn:
            n_del = conn.execute("DELETE FROM block_segments WHERE id IN ("
                                 "SELECT s.id FROM block_segments s WHERE " + _SEG_WHERE + ")"
                                 ).rowcount
            before = _meta(conn, MARKER)
            conn.execute("DELETE FROM store_meta WHERE key = ?", (MARKER,))
        pending = conn.execute("SELECT COUNT(*) FROM blocks b WHERE " + _MAIN_PRICED + " AND NOT EXISTS "
                               "(SELECT 1 FROM block_segments s WHERE s.block_start = b.block_start "
                               "AND s.meter_id = b.meter_id AND s.channel = 'import')").fetchone()[0]
    finally:
        conn.close()

    if n_del != n_snap:
        sys.exit("snapshot/delete mismatch: %d snapshotted, %d deleted" % (n_snap, n_del))
    print("source            %s  (written_by=%s)" % (src, written_by))
    print("soak database     %s" % out_db)
    print("segment snapshot  %s" % snap_db)
    print("import segments removed: %d rows over %d blocks" % (n_del, n_blocks))
    print("blocks the sweep will rebuild: %d" % pending)
    print("cleared %s (was %s)" % (MARKER, json.dumps(before)))
    print("\nCopy blocks.db into the soak add-on's data directory with the add-on STOPPED.")


if __name__ == "__main__":
    main()
