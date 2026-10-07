"""
import_gate.py — may this database replace the live one? (import spec §3, phase 1)

v5 carries no v4 heals. What makes that safe is the floor: a database last opened by
v4 release X has had every heal X carries (every heal is marker-gated, and ungated
markers run at startup), so v5 accepts only a database stamped at or above the last
release that added one. Until 4 Oct 2026 nothing enforced it — `written_by` was
stamped (engine.stamp_version) and never read, so a restore took any v4 backup, and a
below-floor one arrived without the 4.5.15–4.5.21 heals.

The spec's four phase-1 gates live here, checked in its order on the CANDIDATE file
before it is swapped in (the live database is not touched by a refusal):

  gate 1  not a live copy (no non-empty `-wal` beside it), and intact
          (`quick_check` and `foreign_key_check` clean) — a marker read out of a
          damaged database is not evidence of anything, so this comes first
  gate 2  `store_meta.written_by` present and >= FLOOR_VERSION
  gate 3  `store_meta.supplier` one this build understands — `octopus`,
          `not-listed`, or absent (never asked); only an unknown key refuses
  gate 4  every priced main-meter import block in pounds, not pence, judged against
          its own ex-VAT figure (v4's pence repair was deleted from v5 with BL-33, and
          the stamp does not prove v4's sweep ever finished it)

`source_supplier` reads gate 3's answer the way the first-run wizard must (spec §3,
amended 4 Oct 2026): an absent supplier with a stamped Octopus account is Octopus.

Raising the floor: change FLOOR_VERSION, here only, in the same change as the import
spec §1.1, ROADMAP §E, programme §E and CLAUDE.md.
"""
import json
import os
import re
import sqlite3

# The last v4 release that carries a heal (4.5.21, raised 7 Oct 2026: the EV-priority
# re-split, the dispatch-only device re-fit and the corrected-device ex-VAT repair).
# 4.5.20 had none and did not move it; 4.5.19 was the floor before.
FLOOR_VERSION = "4.5.21"

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
HOT_WAL = (
    "This looks like a copy taken while Energy Meter Tracker was running, and some of "
    "your most recent half-hours are not in it yet. In the old add-on take a backup, "
    "then restore the zip it writes.")
DAMAGED = "This database is damaged and cannot be restored safely."
UNSUPPORTED_SUPPLIER = (
    "This backup is from a {supplier} account, which this version of EMT cannot "
    "import yet.")

# Gate 3's "understands": a supplier with an API this build can authenticate, or the
# deliberate local-only answer (suppliers.py). Absent is "never asked", admitted too.
from suppliers import API_CAPABLE, LOCAL_ONLY


def parse_version(v):
    """'4.5.21-a22' -> (4, 5, 21); '2026.1.0' -> (2026, 1, 0); unparseable -> None.

    Numeric, never lexical ('4.5.9' > '4.5.13' is true as strings). A suffix after the
    third field — a v5 dev build's '-a22' — is dropped: that build runs 4.5.21's code.
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


def has_hot_wal(path):
    """Gate 1's filesystem half: a non-empty `-wal` beside the file is committed
    history the main file does not yet contain. Checked before the file is opened."""
    wal = path + "-wal"
    return os.path.exists(wal) and os.path.getsize(wal) > 0


def check_intact(conn):
    """Gate 1's database half. None if clean, else the refusal."""
    rows = conn.execute("PRAGMA quick_check").fetchall()
    if [tuple(r) for r in rows] != [("ok",)]:
        return DAMAGED
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        return DAMAGED
    return None


def check_supplier(conn):
    """Gate 3. None if admissible, else the refusal. Never refuses `not-listed` or an
    absent key: both hold real meter history (spec §3)."""
    supplier = _meta(conn, "supplier")
    if not supplier or supplier in API_CAPABLE or supplier == LOCAL_ONLY:
        return None
    return UNSUPPORTED_SUPPLIER.format(supplier=supplier)


def _account_stamped(conn):
    try:
        row = conn.execute("SELECT value FROM kraken_state "
                           "WHERE key = 'kraken_account_number'").fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row["value"])


def source_supplier(path):
    """(supplier, inferred): whose credentials the database at `path` needs — logged
    at restore now (the start of spec §9's audit record), and what the first-run
    wizard will prompt for. `inferred` is True when the supplier was not stamped and is read from an
    Octopus account stamp instead (spec §3, amended 4 Oct 2026): v4's supplier field
    is optional free text, real Octopus installs have it empty, and Octopus is the
    only API v4 ever had. (None, False) is "never asked, nothing to prompt for"."""
    conn = _connect(path)
    try:
        supplier = _meta(conn, "supplier")
        if supplier:
            return supplier, False
        if _account_stamped(conn):
            return "octopus", True
        return None, False
    finally:
        conn.close()


def describe_supplier(path):
    """source_supplier as one log phrase."""
    try:
        supplier, inferred = source_supplier(path)
    except sqlite3.Error:
        return "unreadable"
    if not supplier:
        return "none (never asked)"
    return supplier + (" (inferred from the Octopus account stamp)" if inferred else "")


def check(path):
    """Phase-1 gates 1–4 on the database at `path`, in the spec's order.
    Returns None when it may be restored, else the user-facing refusal."""
    if has_hot_wal(path):
        return HOT_WAL
    try:
        conn = _connect(path)
    except sqlite3.Error as e:
        return UNREADABLE.format(error=e)
    try:
        return (check_intact(conn) or check_written_by(conn)
                or check_supplier(conn) or check_units(conn))
    except sqlite3.Error as e:
        # "malformed" is a damaged database (gate 1); anything else — "file is not a
        # database", a missing table — is not one EMT can read.
        return DAMAGED if "malformed" in str(e) else UNREADABLE.format(error=e)
    finally:
        conn.close()
