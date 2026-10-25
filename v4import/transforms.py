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
    3b the bill's split    a settled IOG-SMB block takes its cached bill's EV (one-rate blocks)
    3c imported bills      an imported half-hour's Octopus cost becomes its bill (engine step 4a-2)
    4  band relabel        not built: BL-81 closed (house off_peak stays in every era)
    5  meta                the allow-listed store_meta / kraken_state keys
    6  device sensors      each configured read a primary; Device History's other sensors
                           secondaries (device sensors design §6)
    7  HA's reads          what HA reported, out of the blocks into `ha_reads` (soft delete
                           design §2)

4 is not built (ROADMAP BL-81, closed 10 Oct 2026); the others run in this order.

Blocks whose segments 2, 3 and 3b write are recorded in the temp table `v4i_resegmented`, so
verify.py can check them by their own gates and the rest as carried exactly. A block can carry
more than one record — 3 refits it and 3b then moves its split — and keeps each (19 Oct 2026: one
record per block let 3b's overwrite 3's, so the gates judged a refitted block against its source
segments). 3b also notes the segments it started from (`v4i_split_from`), for its gate.
"""
import json

from . import rules

MAIN = "electricity_main"
_EPS = 1e-6                      # the spec's tolerance for "segments miss the kWh"
_ONE_RATE = 1e-5                 # £/kWh (0.001p): within it, a block's segments carry one rate
HELD = ("corrected", "reconciled", "settled")     # a held rate, v5's words (after 1)


def _resegmented(conn, block_start, why):
    conn.execute("INSERT OR IGNORE INTO temp.v4i_resegmented (block_start, why) VALUES (?, ?)",
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
    _billing_source(conn, report)


def _billing_source(conn, report):
    """5, the settlement source (spec v25, 21 Oct 2026): not carried — v5 has no choice, and
    settles against the supplier wherever the API is on. A v4 on an API mode that chose its own
    readings ('cad') priced its main meter's half-hours from them, so each is flagged for the
    pass-2 re-run that re-prices it against the settled kWh: what v4's toggle did, back to the
    default. Without the API, or on 'dcc', nothing moves."""
    def state(key):
        row = conn.execute("SELECT value FROM src.kraken_state WHERE key = ?", (key,)).fetchone()
        return str(row[0]).strip().strip('"') if row and row[0] is not None else None
    was, mode = state("billing_source"), state("data_source_mode") or ""
    flagged = 0
    if was == "cad" and "api" in mode:
        flagged = conn.execute(
            "UPDATE main.blocks SET needs_pass2_rerun = 1 WHERE meter_id IN "
            "(SELECT meter_id FROM main.meters WHERE is_sub_meter = 0)").rowcount
    report["billing_source"] = {"was": was, "mode": mode or None, "rerun": flagged}


def device_sensors(conn, report):
    """6 (device sensors design §6, import spec v23): a device's sensors in one place. After 5,
    which carries the attribution ledger. For the current config period:
    - each channel's `read_sensor` becomes its PRIMARY (`origin='configured'`);
    - for a sub-meter, every sensor its Device History runs used (`recorder_attribution_runs`,
      the latest run first, each run's own order) that is not its primary becomes a SECONDARY,
      in that order, deduplicated (`origin='v4_attribution'`).
    v4 kept those sensors only in the ledger, so a device's older entity ids reached nothing but
    the runs. No block points at an entry: v4 recorded no sensor per block."""
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    pid = conn.execute("SELECT id FROM main.config_periods WHERE effective_to IS NULL "
                       "ORDER BY effective_from DESC LIMIT 1").fetchone()
    out = {"primaries": 0, "secondaries": 0, "devices": []}
    if pid is None:
        report["transforms"]["device_sensors"] = out
        return
    chans = conn.execute(
        "SELECT m.meter_id, m.is_sub_meter, mc.channel, mc.read_sensor FROM main.meters m "
        "JOIN main.meter_channels mc ON mc.meter_id = m.id WHERE m.config_period_id = ? "
        "ORDER BY m.meter_id, mc.channel", (pid[0],)).fetchall()
    raw = conn.execute("SELECT value FROM main.store_meta WHERE key = "
                       "'recorder_attribution_runs'").fetchone()
    try:
        runs = json.loads(raw[0]) if raw and raw[0] else []
    except (TypeError, ValueError):
        runs = []
    runs = sorted((r for r in runs if isinstance(r, dict)),
                  key=lambda r: str(r.get("created_at") or ""), reverse=True)
    for c in chans:
        mid, ch, read = c["meter_id"], c["channel"], (c["read_sensor"] or "").strip() or None
        if read:
            conn.execute("INSERT INTO main.device_sensors (meter_id, channel, entity_id, role, "
                         "origin, added_at) VALUES (?, ?, ?, 'primary', 'configured', ?)",
                         (mid, ch, read, now))
            out["primaries"] += 1
        # Device History rebuilds a device's import (v4: one channel per device)
        if not (c["is_sub_meter"] and ch == "import"):
            continue
        extra = []
        for r in runs:
            if r.get("meter_id") != mid:
                continue
            for e in r.get("sensor_ids") or []:
                e = (str(e) if e else "").strip()
                if e and e != read and e not in extra:
                    extra.append(e)
        for i, e in enumerate(extra):
            conn.execute("INSERT INTO main.device_sensors (meter_id, channel, entity_id, role, "
                         "position, origin, added_at) VALUES (?, ?, ?, 'secondary', ?, "
                         "'v4_attribution', ?)", (mid, ch, e, i, now))
        if extra:
            out["secondaries"] += len(extra)
            out["devices"].append(mid)
    report["transforms"]["device_sensors"] = out


# 7's kWh for a live channel: the block's, unless Octopus settled it — v4 then put Octopus's kWh
# on the block (`imp_kwh` = `imp_kwh_api`), and HA's is the registers' difference. A device is
# never settled, and keeps its kWh: an EV charger's sensor resets each session.
_HA_KWH = ("CASE WHEN b.{c}_kwh_api IS NULL THEN b.{c}_kwh "
           "ELSE MAX(b.{c}_read_end - b.{c}_read_start, 0) END")


def ha_reads(conn, report):
    """7 (soft delete design §2, import spec v24): HA's reads, kept apart from the blocks. After 6,
    whose primaries the live rows name.
    - a live block (`source` NULL or 'kraken_api') gives a half-hour row per channel holding both
      registers (on the main meter not compute_channel's no-reads 0.0 / 0.0 — a grid register
      is never 0; a device's session sensor sits at 0 between charges), `sensor_id` its channel's primary
      marked inferred — v4 recorded no sensor per block;
    - Device History's `recorder_attributed` half-hours give back their hour, the sum of a whole
      hour's blocks (the split sums to the hour, within its 6-dp rounding); an hour with a block
      missing — no house block, or the device's own live row — keeps its half-hours as they are.
    Imported blocks give nothing: they are not HA's."""
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    live = 0
    for c, ch in (("imp", "import"), ("exp", "export")):
        live += conn.execute(
            "INSERT INTO main.ha_reads (meter_id, channel, origin, period_start, minutes, "
            "sensor_id, sensor_inferred, kwh, read_start, read_end, interpolated, recorded_at) "
            "SELECT b.meter_id, ?, 'live', b.block_start, "
            "CAST(ROUND((julianday(b.block_end) - julianday(b.block_start)) * 1440) AS INTEGER), "
            "s.id, s.id IS NOT NULL, " + _HA_KWH.format(c=c) + ", "
            "b.{c}_read_start, b.{c}_read_end, b.interpolated, ? "
            "FROM main.blocks b LEFT JOIN main.device_sensors s ON s.meter_id = b.meter_id "
            "AND s.channel = ? AND s.role = 'primary' "
            "WHERE (b.source IS NULL OR b.source = 'kraken_api') AND b.{c}_kwh IS NOT NULL "
            "AND b.{c}_read_start IS NOT NULL AND b.{c}_read_end IS NOT NULL "
            "AND NOT (b.{c}_read_start = 0 AND b.{c}_read_end = 0 AND b.meter_id NOT IN "
            "(SELECT meter_id FROM main.meters WHERE is_sub_meter = 1))".replace("{c}", c),
            (ch, now, ch)).rowcount
    bm = conn.execute("SELECT block_minutes FROM main.config_periods WHERE effective_to IS NULL "
                      "ORDER BY effective_from DESC LIMIT 1").fetchone()
    per_hour = max(1, 60 // int((bm[0] if bm else None) or 30))
    # the whole hours, once: each recorder half-hour keyed to its hour
    conn.execute("DROP TABLE IF EXISTS temp.v4i_rec")
    conn.execute("CREATE TEMP TABLE v4i_rec AS SELECT meter_id, block_start, block_end, imp_kwh, "
                 "substr(block_start, 1, 13) || ':00:00' AS h FROM main.blocks "
                 "WHERE source = 'recorder_attributed' AND imp_kwh IS NOT NULL")
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS v4i_whole (meter_id TEXT, h TEXT, "
                 "PRIMARY KEY (meter_id, h))")
    conn.execute("DELETE FROM temp.v4i_whole")
    conn.execute("INSERT INTO temp.v4i_whole SELECT meter_id, h FROM temp.v4i_rec "
                 "GROUP BY meter_id, h HAVING COUNT(*) = ?", (per_hour,))
    hours = conn.execute(
        "INSERT INTO main.ha_reads (meter_id, channel, origin, period_start, minutes, kwh, "
        "recorded_at) SELECT r.meter_id, 'import', 'recorder', r.h, 60, ROUND(SUM(r.imp_kwh), 6), ? "
        "FROM temp.v4i_rec r JOIN temp.v4i_whole w ON w.meter_id = r.meter_id AND w.h = r.h "
        "GROUP BY r.meter_id, r.h", (now,)).rowcount
    halves = conn.execute(
        "INSERT INTO main.ha_reads (meter_id, channel, origin, period_start, minutes, kwh, "
        "recorded_at) SELECT r.meter_id, 'import', 'recorder', r.block_start, "
        "CAST(ROUND((julianday(r.block_end) - julianday(r.block_start)) * 1440) AS INTEGER), "
        "r.imp_kwh, ? FROM temp.v4i_rec r WHERE NOT EXISTS (SELECT 1 FROM temp.v4i_whole w "
        "WHERE w.meter_id = r.meter_id AND w.h = r.h)", (now,)).rowcount
    conn.execute("DROP TABLE temp.v4i_rec")
    report["transforms"]["ha_reads"] = {"live": live, "recorder_hours": hours,
                                        "recorder_half_hours": halves}


def provenance(conn, report):
    """1: v5's provenance words (spec §5.2, BL-61(5)). A bill-settled block says 'settled'
    where v4 said 'measured'. A block a dispatch reconciliation flagged (rate_reconciled = 1)
    while leaving 'schedule' or no word says 'reconciled'; a settled or corrected one keeps
    its word, which outranks a reconciliation. Every meter: v4 flagged sub-meter rows too.
    The flag itself is not carried."""
    renamed = conn.execute(
        "UPDATE main.blocks SET rate_source = 'settled' WHERE rate_source = 'measured'").rowcount
    # rate_reconciled is not carried (rules.DROP_COLUMNS): read from the source, by the
    # carried id
    folded = conn.execute(
        "UPDATE main.blocks SET rate_source = 'reconciled' WHERE id IN (SELECT id FROM "
        "src.blocks WHERE rate_reconciled = 1) AND (rate_source IS NULL OR "
        "rate_source = 'schedule')").rowcount
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
        # the EV split and imp_rate_exc are v4-only columns (rules.DROP_COLUMNS): read from the
        # source, by the carried id
        "SELECT b.block_start, b.imp_kwh, b.imp_cost, b.imp_rate, v.imp_rate_exc, v.imp_kwh_ev, "
        "v.imp_cost_ev, v.imp_rate_ev, v.imp_ev_band, v.imp_home_band FROM main.blocks b "
        "JOIN src.blocks v ON v.id = b.id "
        "WHERE b.meter_id = ? AND v.imp_kwh_ev > ? AND NOT EXISTS (SELECT 1 FROM "
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
        "SELECT b.block_start, b.imp_kwh, b.imp_rate, v.imp_rate_exc, v.imp_home_band, "
        "b.rate_source, SUM(s.kwh) AS seg_kwh FROM main.blocks b JOIN src.blocks v "
        "ON v.id = b.id JOIN main.block_segments s "
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


def bill_split(conn, report):
    """3b (bill and split paths design §7.1, 16 Oct 2026): a settled, uncorrected main block in the
    IOG-SMB era — from the earliest cached bill that states a split (a four-bucket breakdown; the
    importer has no agreements to ask) — with a bill split cached, whose EV segments are not the
    bill's EV clipped to the meter, takes it: EV + house at the block's one rate
    (`pricing_segments.ev_house_at`), bands as they were. One rate, so Σ kWh × rate — the cost, the
    rate, every total — is unchanged; only the House/EV attribution moves.

    v4 settled IOG-SMB history under 4.5.9's rule (the completed dispatch capped the bill's EV); v5's
    settle takes the bill's split (#180). This is where imported history meets that rule — once, in
    the importer's copy — instead of a runtime heal.

    Reported, not moved: a block whose segments carry more than one rate (a capped boundary — moving
    kWh would change the cost, and only the runtime settle can rebuild it from the rate schedules),
    and the 4.5.23 case (a bill with no EV where EMT's split has the car). "One rate" is within
    0.001p/kWh: v4's settle priced the house leg as the residual absorber, a few millionths off the
    EV leg's rate; the moved block keeps that — the house leg takes the residual, so the segments
    cost exactly what they did."""
    import pricing_segments as ps
    era = conn.execute("SELECT MIN(slot_start) FROM main.measured_cost WHERE direction = "
                       "'CONSUMPTION' AND ev_kwh IS NOT NULL").fetchone()[0]
    moved, mixed, held, kwh_ev = [], [], [], 0.0
    if era is not None:
        rows = conn.execute(
            "SELECT b.block_start, b.imp_kwh, m.ev_kwh AS bill_ev FROM main.blocks b "
            "JOIN main.measured_cost m ON m.rowid = (SELECT m2.rowid FROM main.measured_cost m2 "
            "  WHERE m2.slot_start = b.block_start AND m2.direction = 'CONSUMPTION' "
            "  ORDER BY m2.fetched_at DESC LIMIT 1) "
            "WHERE b.meter_id = ? AND b.rate_source = 'settled' AND COALESCE(b.rate_corrected, 0) = 0 "
            "AND COALESCE(b.imp_kwh, 0) > ? AND b.block_start >= ? AND m.ev_kwh IS NOT NULL "
            "ORDER BY b.block_start", (MAIN, _EPS, era)).fetchall()
        for r in rows:
            segs = conn.execute(
                "SELECT kwh, inc_rate, exc_rate, band, attribution FROM main.block_segments "
                "WHERE block_start = ? AND meter_id = ? AND channel = 'import' ORDER BY seq",
                (r["block_start"], MAIN)).fetchall()
            if not segs:
                continue
            kwh = float(r["imp_kwh"])
            seg_ev = sum(float(x["kwh"]) for x in segs if x["attribution"] == "ev")
            want = min(max(float(r["bill_ev"]), 0.0), kwh)
            if abs(want - seg_ev) <= 0.0005:
                continue                                       # already the bill's
            if seg_ev > 0.05 and want <= 1e-9:                # v4 4.5.23: not the car taken away
                held.append(r["block_start"])
                continue
            rates = [float(x["inc_rate"] or 0.0) for x in segs]
            if max(rates) - min(rates) > _ONE_RATE:
                mixed.append(r["block_start"])          # a capped boundary: the runtime settle's
                continue
            ev_seg = next((x for x in segs if x["attribution"] == "ev"), None)
            house_seg = next((x for x in segs if x["attribution"] != "ev"), None)
            lead = ev_seg or segs[0]
            new = ps.ev_house_at(
                kwh, want, inc_rate=lead["inc_rate"], exc_rate=lead["exc_rate"],
                ev_band=(ev_seg or house_seg)["band"], home_band=(house_seg or ev_seg)["band"])
            # v4's settle priced the house leg as the residual absorber (its rate a few
            # millionths off the EV leg's, so the legs sum to the bill exactly); keep that: the
            # house leg takes whatever the source segments cost beyond the EV leg's
            cost = sum(float(x["kwh"]) * float(x["inc_rate"] or 0.0) for x in segs)
            if len(new) == 2 and new[1].kwh > _EPS:
                hr = round((cost - new[0].kwh * float(new[0].inc_rate)) / new[1].kwh, 7)
                ratio = (float(new[0].exc_rate) / float(new[0].inc_rate)
                         if new[0].exc_rate is not None and new[0].inc_rate else None)
                new[1] = new[1]._replace(inc_rate=hr, exc_rate=(round(hr * ratio, 7)
                                                                if ratio is not None else None))
            # what this move started from — after transform 3's refit, where it ran — so the gate
            # can tell "the attribution moved, nothing else" from the refit's own change
            conn.execute("INSERT OR REPLACE INTO temp.v4i_split_from (block_start, k, c) "
                         "VALUES (?, ?, ?)", (r["block_start"],
                                              sum(float(x["kwh"]) for x in segs), cost))
            _write_segments(conn, r["block_start"], new)
            _resegmented(conn, r["block_start"], "bill_split")
            moved.append(r["block_start"])
            kwh_ev += want - seg_ev
    report["transforms"]["bill_split"] = {"moved": len(moved), "kwh_ev": round(kwh_ev, 6),
                                          "reported_mixed": mixed, "reported_held": held,
                                          "era_from": era}


def imported_bills(conn, report):
    """3c (engine step 4 design §6 (e), 25 Oct 2026): an imported half-hour's Octopus cost becomes
    its bill. v4's API import kept Octopus's cost on the block (`imp_cost`, the rate worked back from
    it) and wrote no `measured_cost` row, so history past Octopus's two years when it reaches v5 can
    never be asked again — the block's cost is the only record of the band Octopus billed, and
    settle() reads no stored cost. Each main `imported_api` half-hour with kWh, a cost v4 took from
    Octopus (`exc_source = 'measurement'`; 'tariff' is v4's own kWh × rate, not Octopus's) and no
    bill gets one: Octopus's cost and ex-VAT, the block's kWh, no label. settled_band bands it by
    cost ÷ kWh. The MPAN is the source's bills' (the one most of them carry), else the main import
    channel's; with neither, nothing is written and the report says so.

    The rows are recorded in temp.v4i_bills, so `row_counts` and `carried_exactly` compare every
    other bill exactly, and the `imported_bills` gate checks these against their blocks."""
    from datetime import datetime, timezone
    have = {r[1] for r in conn.execute("PRAGMA src.table_info(blocks)").fetchall()}
    out = {"written": 0, "skipped": None}
    if "exc_source" not in have:
        out["skipped"] = "the source records no cost provenance (exc_source)"
        report["transforms"]["imported_bills"] = out
        return
    row = conn.execute("SELECT mpan FROM src.measured_cost WHERE direction = 'CONSUMPTION' "
                       "AND COALESCE(mpan, '') <> '' GROUP BY mpan ORDER BY COUNT(*) DESC "
                       "LIMIT 1").fetchone()
    mpan = row[0] if row else None
    if not mpan:
        # meter_channels.meter_id is the meters row's integer id, not its name
        row = conn.execute("SELECT c.mpan FROM main.meter_channels c JOIN main.meters m ON "
                           "m.id = c.meter_id WHERE m.meter_id = ? AND c.channel = 'import' "
                           "AND COALESCE(c.mpan, '') <> '' ORDER BY m.config_period_id DESC LIMIT 1",
                           (MAIN,)).fetchone()
        mpan = row[0] if row else None
    if not mpan:
        out["skipped"] = "no MPAN in the source's bills or its main import channel"
        report["transforms"]["imported_bills"] = out
        return
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    rows = conn.execute(
        "SELECT b.block_start, b.imp_kwh, b.imp_cost, b.imp_cost_exc FROM main.blocks b "
        "JOIN src.blocks v ON v.id = b.id "
        "WHERE b.meter_id = ? AND b.source = 'imported_api' AND COALESCE(b.imp_kwh, 0) > ? "
        "AND b.imp_cost IS NOT NULL AND v.exc_source = 'measurement' "
        "AND NOT EXISTS (SELECT 1 FROM main.measured_cost m WHERE m.slot_start = b.block_start "
        "                AND m.direction = 'CONSUMPTION') "
        "ORDER BY b.block_start", (MAIN, _EPS)).fetchall()
    for r in rows:
        conn.execute("INSERT INTO main.measured_cost (slot_start, mpan, direction, cost_incl, "
                     "cost_excl, label, kwh, fetched_at) VALUES (?, ?, 'CONSUMPTION', ?, ?, NULL, ?, ?)",
                     (r["block_start"], mpan, r["imp_cost"], r["imp_cost_exc"], r["imp_kwh"], now))
        conn.execute("INSERT OR IGNORE INTO temp.v4i_bills (slot_start) VALUES (?)",
                     (r["block_start"],))
    out["written"] = len(rows)                # the MPAN itself never goes in the audit
    report["transforms"]["imported_bills"] = out


def run(conn, report):
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS v4i_resegmented "
                 "(block_start TEXT, why TEXT, PRIMARY KEY (block_start, why))")
    conn.execute("DELETE FROM temp.v4i_resegmented")
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS v4i_split_from "
                 "(block_start TEXT PRIMARY KEY, k REAL, c REAL)")
    conn.execute("DELETE FROM temp.v4i_split_from")
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS v4i_bills (slot_start TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM temp.v4i_bills")
    provenance(conn, report)
    ev_segments(conn, report)
    refit_held(conn, report)
    bill_split(conn, report)
    imported_bills(conn, report)
    carry_keys(conn, report)
    device_sensors(conn, report)
    ha_reads(conn, report)
