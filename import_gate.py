"""
import_gate.py — may this database replace the live one? (import spec §3, phase 1)

v5 carries no v4 heals. What makes that safe is the floor: a database last opened by
v4 release X has had every heal X carries (every heal is marker-gated, and ungated
markers run at startup), so v5 accepts only a database stamped at or above the last
release that added one. Until 4 Oct 2026 nothing enforced it — `written_by` was
stamped (engine.stamp_version) and never read, so a restore took any v4 backup, and a
below-floor one arrived without the 4.5.15–4.5.19 heals.

Two of the spec's phase-1 gates live here, checked on the CANDIDATE file before it is
swapped in (the live database is not touched by a refusal):

  gate 2  `store_meta.written_by` present and >= FLOOR_VERSION
  gate 4  every priced main-meter import block in pounds, not pence, judged against
          its own ex-VAT figure (v4's pence repair was deleted from v5 with BL-33, and
          the stamp does not prove v4's sweep ever finished it)

Not here yet: gate 1's integrity check and gate 3's supplier check. A database with
no supplier key is admissible under gate 3 (never asked), so its absence refuses
nothing.

Raising the floor: change FLOOR_VERSION, here only, in the same change as the import
spec §1.1, ROADMAP §E, programme §E and CLAUDE.md.
"""
import json
import os
import re
import sqlite3

# The last v4 release that carries a heal (4.5.19, raised 1 Oct 2026). 4.5.20 has
# none, so it did not move the floor.
FLOOR_VERSION = "4.5.19"

# A pence block reads ~100 against its own ex-VAT figure; healthy blocks sit at
# 1.000 at the median, worst 4.8% (measured 4 Oct 2026). Two orders either side.
PENCE_RATIO = 10.0

# Below this (£/kWh, or £ for a cost) the ex-VAT figure is too small for the ratio to
# mean anything: rounding a 0.00004 exc rate to four places can make it read 2.5.
MIN_EXC = 0.001

OLDER_THAN_STAMPS = (
    "This backup does not say which version of Energy Meter Tracker wrote it, so it is "
    "older than EMT can restore. Open this in the last v4 release first, then take a "
    "backup and restore that.")
BELOW_FLOOR = (
    "This backup was last opened by Energy Meter Tracker {version}, which is older than "
    "EMT can restore. Open this in the last v4 release first, then take a backup and "
    "restore that.")
IN_PENCE = (
    "This backup holds prices in pence rather than pounds (the first is the half-hour "
    "at {block_start}). Open this in the last v4 release first and let it finish "
    "re-pricing, then take a backup and restore that.")
UNREADABLE = "This file could not be read as an EMT database: {error}"


def parse_version(v):
    """'4.5.20-a21' -> (4, 5, 20); '2026.1.0' -> (2026, 1, 0); unparseable -> None.

    Numeric, never lexical ('4.5.9' > '4.5.13' is true as strings). A suffix after the
    third field — a v5 dev build's '-a21' — is dropped: that build runs 4.5.20's code.
    v5's CalVer sorts above every 4.x on the first field."""
    m = re.match(r"\s*[vV]?(\d+)\.(\d+)(?:\.(\d+))?", str(v or ""))
    if not m:
        return None
    return tuple(int(x or 0) for x in m.groups())


def _connect(path):
    # immutable: the candidate is a file the caller staged, not a live database, so
    # nothing is writing it, and a read-only open of a WAL-mode file without its -shm
    # would otherwise fail. Never opened read-write.
    from urllib.parse import quote
    conn = sqlite3.connect("file:%s?immutable=1" % quote(os.path.abspath(path)), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _meta(conn, key):
    row = conn.execute("SELECT value FROM store_meta WHERE key = ?", (key,)).fetchone()
    if row is None or row["value"] is None:
        return None
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return row["value"]


def check_written_by(conn):
    """Gate 2. None if admissible, else the refusal."""
    ver = _meta(conn, "written_by")
    if not ver:
        return OLDER_THAN_STAMPS
    parsed = parse_version(ver)
    if parsed is None or parsed < parse_version(FLOOR_VERSION):
        return BELOW_FLOOR.format(version=ver)
    return None


def check_units(conn):
    """Gate 4. None if admissible, else the refusal naming the first pence block.

    VAT is never negative, so expected = 1 + VAT >= 1, and ratio / expected > 10
    needs ratio > 10: SQL finds those, and only they are judged against the VAT on
    their own date (seed plus the source's learned calendar)."""
    import vat_calendar
    rows = conn.execute(
        """
        SELECT block_start, ratio FROM (
          SELECT block_start,
                 CASE WHEN imp_rate IS NOT NULL AND ABS(imp_rate_exc) >= :m
                        THEN ABS(imp_rate / imp_rate_exc)
                      WHEN imp_cost IS NOT NULL AND ABS(imp_cost_exc) >= :m
                        THEN ABS(imp_cost / imp_cost_exc)
                 END AS ratio
            FROM blocks
           WHERE meter_id = 'electricity_main' AND imp_kwh > 0)
         WHERE ratio > :r
         ORDER BY block_start
        """, {"m": MIN_EXC, "r": PENCE_RATIO}).fetchall()
    if not rows:
        return None
    learned = []
    for e in _meta(conn, "vat_calendar") or []:
        try:
            d, r = e
            learned.append((str(d)[:10], float(r)))
        except (TypeError, ValueError):
            continue
    vat_at = vat_calendar.rate_lookup(learned)
    for row in rows:
        if row["ratio"] / (1.0 + vat_at(row["block_start"])) > PENCE_RATIO:
            return IN_PENCE.format(block_start=row["block_start"])
    return None


def check(path):
    """Phase-1 gates 2 and 4 on the database at `path`, in the spec's order.
    Returns None when it may be restored, else the user-facing refusal."""
    try:
        conn = _connect(path)
    except sqlite3.Error as e:
        return UNREADABLE.format(error=e)
    try:
        return check_written_by(conn) or check_units(conn)
    except sqlite3.Error as e:
        return UNREADABLE.format(error=e)
    finally:
        conn.close()
