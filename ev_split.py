"""
The EV split of a main-meter half-hour: what the dispatch EV drew from the grid and what it cost,
read from the half-hour's priced segments (step 5, 8 Oct 2026).

`block_segments` rows with attribution='ev' ARE the split. The bill prices from them (BL-27), and
the importer carries them verbatim while it drops the imp_*_ev columns (import spec §5). A
half-hour that has segments but no EV segment has no dispatch EV, whatever its columns say: the
EV-split writers commit the columns and the segments separately (ROADMAP BL-79), so the two can part,
and the segments are the record that survives.

The split being present is the seam every hybrid-EV surface keys on: where it is, the dispatch EV
is the EV and a physical EV meter in that half-hour is superseded; where it is not, the physical
meter (if any) is. Until step 5 that test was `imp_kwh_ev IS NOT NULL`.

LEGACY: a half-hour with NO segments at all falls back to its imp_kwh_ev / imp_cost_ev /
imp_rate_ev columns, as the bill does. The importer writes the EV segments from those columns for
such a block before it drops them (import spec §5.2), and the fallback goes with the columns.
"""


def _seg(b, what, ev_only=True):
    return (f"(SELECT {what} FROM block_segments _s WHERE _s.block_start = {b}.block_start "
            f"AND _s.meter_id = {b}.meter_id AND _s.channel = 'import'"
            + (" AND _s.attribution = 'ev'" if ev_only else "") + ")")


def _sql(b, ev_sum, legacy_col):
    return (f"(CASE WHEN EXISTS {_seg(b, '1', ev_only=False)} THEN {_seg(b, ev_sum)} "
            f"ELSE {b}.{legacy_col} END)")


def sql_kwh(b="b"):
    """SQL: the EV kWh of the block row aliased `b`, NULL where it has no EV split."""
    return _sql(b, "SUM(_s.kwh)", "imp_kwh_ev")


def sql_cost(b="b"):
    """SQL: the EV £ of the block row aliased `b`, NULL where it has no EV split."""
    return _sql(b, "SUM(_s.kwh * _s.inc_rate)", "imp_cost_ev")


def sql_join(alias="_g"):
    """SQL: a LEFT JOIN of each main block's segments, grouped once over a window — for a query
    over many blocks, where sql_kwh's per-row subqueries cost a third more. Takes two parameters,
    the window's [start, end). Join it ON {alias}.block_start = b.block_start, then read the split
    with joined_kwh / joined_cost."""
    return (f"LEFT JOIN (SELECT block_start, meter_id, "
            f"SUM(CASE WHEN attribution = 'ev' THEN kwh END) AS ev_kwh, "
            f"SUM(CASE WHEN attribution = 'ev' THEN kwh * inc_rate END) AS ev_cost "
            f"FROM block_segments WHERE channel = 'import' AND block_start >= ? AND block_start < ? "
            f"GROUP BY block_start, meter_id) {alias}")


def joined_kwh(b="b", alias="_g"):
    """SQL: sql_kwh over sql_join's `alias` (joined on block_start and meter_id)."""
    return f"(CASE WHEN {alias}.block_start IS NOT NULL THEN {alias}.ev_kwh ELSE {b}.imp_kwh_ev END)"


def joined_cost(b="b", alias="_g"):
    """SQL: sql_cost over sql_join's `alias` (joined on block_start and meter_id)."""
    return f"(CASE WHEN {alias}.block_start IS NOT NULL THEN {alias}.ev_cost ELSE {b}.imp_cost_ev END)"


def sql_instants():
    """SQL: the block_starts in [?, ?) whose main block has an EV split — the instants sql_kwh is
    not NULL at, from two index scans rather than a subquery per block. Takes four parameters,
    the window twice."""
    return ("SELECT block_start FROM block_segments WHERE channel = 'import' "
            "AND attribution = 'ev' AND block_start >= ? AND block_start < ? "
            "UNION SELECT b.block_start FROM blocks b WHERE b.imp_kwh_ev IS NOT NULL "
            "AND b.block_start >= ? AND b.block_start < ? AND NOT EXISTS " + _seg("b", "1", False))


def from_channel(imp):
    """(kwh, cost, rate) of an import channel's EV split, or None where it has none. `imp` is a
    block dict's import channel as block_store builds it, with its `segments` attached."""
    imp = imp or {}
    segs = imp.get("segments")
    if segs:
        ev = [s for s in segs if s.get("attribution") == "ev"]
        if not ev:
            return None
        k = sum(float(s.get("kwh") or 0.0) for s in ev)
        c = sum(float(s.get("kwh") or 0.0) * float(s.get("inc_rate") or 0.0) for s in ev)
        return k, c, (c / k if k > 1e-9 else None)
    k = imp.get("kwh_ev")                                  # LEGACY: no segments at all
    if k is None:
        return None
    r = imp.get("rate_ev")
    return float(k), float(imp.get("cost_ev") or 0.0), (float(r) if r is not None else None)
