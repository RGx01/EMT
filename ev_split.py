"""
The EV split of a main-meter half-hour: what the dispatch EV drew from the grid and what it cost,
read from the half-hour's priced segments (step 5, 8 Oct 2026).

`block_segments` rows with attribution='ev' ARE the split. The bill prices from them (BL-27), and
the importer carries them verbatim (import spec §5). A half-hour with no EV segment has no
dispatch EV.

The split being present is the seam every hybrid-EV surface keys on: where it is, the dispatch EV
is the EV and a physical EV meter in that half-hour is superseded; where it is not, the physical
meter (if any) is. Until step 5 that test was `imp_kwh_ev IS NOT NULL`.

Step 5c (10 Oct 2026): the imp_*_ev columns are gone, and with them the LEGACY fallback for a
half-hour with no segments at all. The importer wrote the EV segments from those columns for every
such block before it dropped them (import spec §5.2), and v5 writes a block's segments in the
block's own transaction, so no split is held anywhere else.
"""


def _seg(b, what, ev_only=True):
    return (f"(SELECT {what} FROM block_segments _s WHERE _s.block_start = {b}.block_start "
            f"AND _s.meter_id = {b}.meter_id AND _s.channel = 'import'"
            + (" AND _s.attribution = 'ev'" if ev_only else "") + ")")


def sql_kwh(b="b"):
    """SQL: the EV kWh of the block row aliased `b`, NULL where it has no EV split."""
    return _seg(b, "SUM(_s.kwh)")


def sql_cost(b="b"):
    """SQL: the EV £ of the block row aliased `b`, NULL where it has no EV split."""
    return _seg(b, "SUM(_s.kwh * _s.inc_rate)")


def sql_band(b="b", attribution="ev"):
    """SQL: the band of the block row's `attribution` segments — the one band when they share
    it, 'mixed' when they do not (the old imp_ev_band / imp_home_band vocabulary), NULL when the
    block has none."""
    return (f"(SELECT CASE WHEN COUNT(DISTINCT _s.band) = 1 THEN MAX(_s.band) "
            f"WHEN COUNT(*) > 0 THEN 'mixed' END FROM block_segments _s "
            f"WHERE _s.block_start = {b}.block_start AND _s.meter_id = {b}.meter_id "
            f"AND _s.channel = 'import' AND _s.attribution = '{attribution}')")


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
    return f"{alias}.ev_kwh"


def joined_cost(b="b", alias="_g"):
    """SQL: sql_cost over sql_join's `alias` (joined on block_start and meter_id)."""
    return f"{alias}.ev_cost"


def sql_instants():
    """SQL: the block_starts in [?, ?) whose main block has an EV split — the instants sql_kwh is
    not NULL at, from one index scan rather than a subquery per block. Takes two parameters,
    the window."""
    return ("SELECT block_start FROM block_segments WHERE channel = 'import' "
            "AND attribution = 'ev' AND block_start >= ? AND block_start < ?")


def from_channel(imp):
    """(kwh, cost, rate) of an import channel's EV split, or None where it has none. `imp` is a
    block dict's import channel: with its `segments` attached (get_blocks_lightweight), they
    decide; without (_row_to_block, or a channel pricing has just split in memory), its
    `kwh_ev` / `cost_ev` / `rate_ev` — which a loaded block takes from the EV segments."""
    imp = imp or {}
    segs = imp.get("segments")
    if segs:
        ev = [s for s in segs if s.get("attribution") == "ev"]
        if not ev:
            return None
        k = sum(float(s.get("kwh") or 0.0) for s in ev)
        c = sum(float(s.get("kwh") or 0.0) * float(s.get("inc_rate") or 0.0) for s in ev)
        return k, c, (c / k if k > 1e-9 else None)
    k = imp.get("kwh_ev")                                  # no segments attached
    if k is None:
        return None
    r = imp.get("rate_ev")
    return float(k), float(imp.get("cost_ev") or 0.0), (float(r) if r is not None else None)
