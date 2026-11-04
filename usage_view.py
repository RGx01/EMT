"""
usage_view.py — the Usage Stats and Spiral data: per-day and per-half-hour rows.

Served by `/api/energy` (`include=stats`): day_stats for a range of local days, slot_stats for
the half-hours of a range. C step 1 (1 Oct 2026) moved this here, unchanged, from the two
routes that built it in web/server.py — `/api/charts/blocks-summary` (a day per row, all
history) and `/api/charts/blocks-day` (a half-hour per row, one day) — and then deleted them
once the Charts page read `/api/energy`. Before deletion both routes were byte-identical to
this code on real databases, and the day rows equal on every day.

The aggregation is `_aggregate_block_rows`: devices at their grid-attributed kWh (`imp_kwh_grid`),
the Direct import by direct_import's rule on each half-hour (main − Σ devices, floored, a credit
kept; it was the stored remainder in kWh and a per-bucket floor in £ until 6 Oct 2026), the
carbon apportioned to import and export, and the standing charge once per bucket. The EV split is display-only and leaves every row total untouched: from
completed dispatches for an account without sub-meters (`_dispatch_ev_split_by_bucket`), or
the hybrid fold for one with an EV meter (`_hybrid_ev_by_block`).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import direct_import as _direct
import ev_split as _evsplit

logger = logging.getLogger("usage_view")


def _aggregate_block_rows(raw_rows, bucket_of, standing_scope="bucket",
                          billing_day_fallback=1):
    """Billing-accurate per-block aggregation shared by the Usage Stats endpoints
    (/blocks-summary buckets by local day; /blocks-day by half-hour slot). Buckets
    by ``bucket_of(block_start)`` and applies the SAME rate-keyed direct-cost
    subtraction, sub-meter split and carbon apportioning as before — this is a pure
    extraction, so the day path is byte-identical (guarded by a golden-output check
    + test_usage_stats_vs_billing).

    ``standing_scope='bucket'`` books the standing charge as the first non-zero
    within EACH bucket (the daily rule). ``'global'`` books it once across ALL rows
    (used by the HH view, whose buckets are slots of one day, so the day's single
    standing charge lands on one slot and the slots still sum to it).

    Returns ``{bucket_key: agg}`` with main ``imp_cost`` already resolved. Row rows
    must carry: block_start, meter_id, is_sub_meter, imp_kwh, imp_kwh_grid,
    imp_rate, imp_cost, exp_kwh, exp_cost, standing_charge, carbon_g (and optionally
    billing_day). The stored remainder is not read (step 5, 8 Oct 2026): Direct is
    direct_import's rule on the grid shares."""
    rows = []
    for _r in raw_rows:
        _k = _r.keys()
        _g = lambda c: _r[c] if c in _k else None     # a row may omit what its kind never reads
        rows.append((_r["block_start"], _g("meter_id"), _r["is_sub_meter"], _g("billing_day"),
                     _r["imp_kwh"], _g("imp_kwh_grid"), _r["imp_rate"],
                     _r["imp_cost"], _r["exp_kwh"], _r["exp_cost"], _r["standing_charge"],
                     _r["carbon_g"]))
    return _aggregate_tuples(rows, bucket_of, standing_scope, billing_day_fallback)


def _aggregate_tuples(rows, bucket_of, standing_scope="bucket", billing_day_fallback=1):
    """`_aggregate_block_rows` over plain tuples — the one implementation of its rules.

    Each row is `(block_start, meter_id, is_sub_meter, billing_day, imp_kwh, imp_kwh_grid,
    imp_rate, imp_cost, exp_kwh, exp_cost, standing_charge, carbon_g)`.
    Split out for the all-days load (#47, 2 Oct 2026), which reads these columns as tuples
    (`_day_tuples`): over `sqlite3.Row`s, looked up by name a dozen times a block, the
    aggregation took 0.143 s on the dev database's 58k blocks; over tuples, about 0.07 s. SQLite's
    SUM was tried and rejected: it adds with error compensation, and moved 31 of 1,214 days by
    0.0001 on the production-like database. (The Direct import's move to a per-half-hour rule, 6
    Oct 2026, moved a handful of days by 0.0001 the same way: it sums per block, then per bucket.)"""
    _day_data = {}
    _global_sc_done = False
    # Each block's main and devices (kWh, £), for its Direct import (direct_import's rule,
    # per half-hour) — kept per block because the rows arrive in no order within one.
    _blocks = {}
    for (_bs, _mid, _is_sub, _bd_val, _ik, _ig, _irate, _ic, _ek, _ec, _sc,
         _cg) in rows:
        _bk = bucket_of(_bs)
        _dd = _day_data.get(_bk)
        if _dd is None:
            _dd = _day_data[_bk] = {
                "main":        {"imp_kwh": 0.0, "imp_cost": 0.0, "exp_kwh": 0.0, "exp_cost": 0.0,
                                "carbon_g": None, "cg_imp": None, "cg_exp": None,
                                "ci_abs_carbon": 0.0, "ci_abs_net_kwh": 0.0,
                                "_main_by_rate": {}, "_sub_by_rate": {}},
                "subs":        {},
                "standing":    0.0,
                "billing_day": int(_bd_val or billing_day_fallback),
            }
        _imp = float(_ik or 0)
        _exp = float(_ek or 0)
        _cost_imp = float(_ic or 0)
        _cost_exp = float(_ec or 0)
        _sc = float(_sc or 0)
        _cg = float(_cg) if _cg is not None else None
        _rate = round(float(_irate or 0), 6)
        _blk = _blocks.get(_bs)
        if _blk is None:
            _blk = _blocks[_bs] = [_bk, None, 0.0, 0.0]   # bucket, main (kWh, £), devices
        if not _is_sub:
            _m = _dd["main"]
            # The main's grid kWh. Its Direct is no longer the stored imp_kwh_remainder
            # (equal to the rule's on every real block measured, 6 Oct 2026; on the drop list).
            _m_imp = float(_ig) if _ig is not None else _imp
            _blk[1] = (_m_imp, _cost_imp) if _blk[1] is None else (
                _blk[1][0] + _m_imp, _blk[1][1] + _cost_imp)
            _br = _m["_main_by_rate"].get(_rate)
            if _br is None:
                _br = _m["_main_by_rate"][_rate] = {"kwh": 0.0, "cost": 0.0}
            _br["kwh"]  += _m_imp
            _br["cost"] += _cost_imp
            _m["exp_kwh"]  += _exp
            _m["exp_cost"] += _cost_exp
            if _sc > 0:
                if standing_scope == "global":
                    if not _global_sc_done:
                        _dd["standing"] = _sc
                        _global_sc_done = True
                elif not _dd["standing"]:
                    _dd["standing"] = _sc
            if _cg is not None:
                _m["carbon_g"] = (_m["carbon_g"] or 0.0) + _cg
                _b_net = _imp - _exp
                if _b_net != 0:
                    _b_int = abs(_cg / _b_net)
                    _m["cg_imp"] = (_m["cg_imp"] or 0.0) + _imp * _b_int
                    _m["cg_exp"] = (_m["cg_exp"] or 0.0) + _exp * _b_int
                    _m["ci_abs_carbon"]  += abs(_cg)
                    _m["ci_abs_net_kwh"] += abs(_b_net)
                elif _exp == 0:
                    _m["cg_imp"] = (_m["cg_imp"] or 0.0) + abs(_cg)
                    _m["ci_abs_carbon"]  += abs(_cg)
                    _m["ci_abs_net_kwh"] += _imp
                else:
                    _m["cg_exp"] = (_m["cg_exp"] or 0.0) + abs(_cg)
                    _m["ci_abs_carbon"]  += abs(_cg)
                    _m["ci_abs_net_kwh"] += _exp
        else:
            _s = _dd["subs"].get(_mid)
            if _s is None:
                _s = _dd["subs"][_mid] = {"imp_kwh": 0.0, "imp_cost": 0.0, "exp_kwh": 0.0,
                                          "exp_cost": 0.0, "carbon_g": None}
            # Grid portion only; a real 0 (fully solar/battery-charged block) must
            # stay 0, not fall back to total consumption (see _aggregate_usage).
            _sub_imp = float(_ig) if _ig is not None else _imp
            _s["imp_kwh"]  += _sub_imp
            _s["imp_cost"] += _cost_imp
            _s["exp_kwh"]  += _exp
            _s["exp_cost"] += _cost_exp
            if _cg is not None:
                # The device's GRID carbon: its stored carbon (its own read × the half-hour's
                # intensity) scaled to its grid share — the kWh it is shown and priced on, and
                # what the house's remainder subtracts (27 Oct 2026, the user). What it drew from
                # the battery or solar carries no grid carbon here; Insights' device cards keep
                # the gross figure. Scaling keeps whatever the row stored (an import's decision).
                _s["carbon_g"] = (_s["carbon_g"] or 0.0) + (
                    _cg * (_sub_imp / _imp) if _imp > 1e-9 else 0.0)
            _sr = _dd["main"]["_sub_by_rate"].get(_rate)
            if _sr is None:
                _sr = _dd["main"]["_sub_by_rate"][_rate] = {"kwh": 0.0, "cost": 0.0}
            _sr["kwh"]  += _sub_imp
            _sr["cost"] += _cost_imp
            _blk[2] += _sub_imp
            _blk[3] += _cost_imp

    # The bucket's Direct import: direct_import's rule on each half-hour, summed. It was
    # floored per DAY here (kWh per half-hour, from the stored remainder), per half-hour on
    # the bill and per window on Live Power — three answers for one figure on a day the
    # devices out-read the main (6 Oct 2026). A genuinely negative main £ (an Agile plunge
    # credit) still passes through, as it did.
    for _bk, _main, _dk, _dc in _blocks.values():
        if _main is None:
            continue                               # devices with no main row
        _x = _day_data[_bk]["main"]
        _k, _c = _direct.direct(_main[0], _main[1], _dk, _dc)
        _x["imp_kwh"] += _k
        _x["imp_cost"] += _c
    for _ld, _dd in _day_data.items():
        _m = _dd["main"]
        _tot_main = sum(_mv["cost"] for _mv in (_m.get("_main_by_rate") or {}).values())
        # `imp_cost` is the DISPLAYED Direct/House line. `imp_cost_metered` is the MAIN
        # METER's cost, and what the row totals use: Direct + SUM(subs) followed the devices
        # on any day they out-read the main, which inflated one 4.5.14 report by £22.42 over
        # 29 days. The split clips each device's grid share to the main, so they cannot —
        # except on an unsettled gap block, until settlement re-clips it.
        _m["imp_cost_metered"] = _tot_main
    return _day_data


def _bucket_to_row_values(_dd):
    """The value fields of one aggregated bucket (from _aggregate_block_rows), in the
    shape barAggRows consumes. Pure extraction of the /blocks-summary row build, so
    it's byte-identical; each endpoint merges these with its own identity fields
    (year/month/day for the day view, slot/label for HH)."""
    _m = _dd["main"]
    _avg_intensity = (round(_m["ci_abs_carbon"] / _m["ci_abs_net_kwh"], 1)
                      if _m["ci_abs_net_kwh"] > 0 else None)
    # the house: the main's import carbon less the devices' GRID carbon (their grid shares), so
    # it cannot go below zero on a half-hour the devices drew from the battery or solar, and
    # house + devices is the grid's import carbon, as their kWh and £ are the grid's (27 Oct 2026;
    # until then the devices' gross carbon was subtracted — staging: 340 half-hours below zero)
    _sub_carbon_for_remainder = sum(
        float(st["carbon_g"]) for st in _dd["subs"].values()
        if st["carbon_g"] is not None and st["carbon_g"] != 0.0)
    _cg_imp = _m["cg_imp"]
    _main_carbon_remainder = (round(_cg_imp - _sub_carbon_for_remainder, 4)
                              if _cg_imp is not None else None)
    meters_out = {"electricity_main": {
        "imp_kwh":      round(_m["imp_kwh"],  4),
        "imp_cost":     round(_m["imp_cost"], 4),
        "exp_kwh":      round(_m["exp_kwh"],  4),
        "exp_cost":     round(_m["exp_cost"], 4),
        "carbon_g":     _main_carbon_remainder,
        "carbon_g_imp":  round(_cg_imp, 4) if _cg_imp is not None else None,
        "carbon_g_exp":  round(_m["cg_exp"], 4) if _m["cg_exp"] is not None else None,
    }}
    for _mid, _st in _dd["subs"].items():
        meters_out[_mid] = {
            "imp_kwh":  round(_st["imp_kwh"],  3),
            "imp_cost": round(_st["imp_cost"], 4),
            "exp_kwh":  round(_st["exp_kwh"],  3),
            "exp_cost": round(_st["exp_cost"], 4),
            "carbon_g": round(_st["carbon_g"], 4) if _st["carbon_g"] is not None and _st["carbon_g"] != 0.0 else None,
        }
    _main_imp_kwh  = round(_m["imp_kwh"],  4)
    _main_imp_cost = round(_m["imp_cost"], 4)
    _main_exp_kwh  = round(_m["exp_kwh"],  4)
    _main_exp_cost = round(_m["exp_cost"], 4)
    meters_out["electricity_main"]["imp_kwh"]  = _main_imp_kwh
    meters_out["electricity_main"]["imp_cost"] = _main_imp_cost
    meters_out["electricity_main"]["exp_kwh"]  = _main_exp_kwh
    meters_out["electricity_main"]["exp_cost"] = _main_exp_cost
    # Row totals price from the METERED main cost, not from the clamped Direct
    # line plus the sub-meters. `meters_out[...]["imp_cost"]` keeps the clamped
    # value because that is what the chart draws; only the totals differ, and only
    # on days where the clamp would have fired.
    _metered_imp_cost = round(_m.get("imp_cost_metered", _main_imp_cost + sum(
        m["imp_cost"] for mid, m in meters_out.items() if mid != "electricity_main")), 4)
    out = {
        "standing":  round(_dd["standing"], 4),
        "meters":    meters_out,
        "imp_kwh":  round(_main_imp_kwh  + sum(m["imp_kwh"]  for mid, m in meters_out.items() if mid != "electricity_main"), 4),
        "exp_kwh":  round(_main_exp_kwh,  4),
        "imp_cost": _metered_imp_cost,
        "exp_cost": round(_main_exp_cost, 4),
        "net_cost": round(
            _metered_imp_cost
            + round(_dd["standing"], 4)
            - round(_main_exp_cost, 4),
            2),
        "carbon_g_net":   round(_m["carbon_g"], 4) if _m["carbon_g"] is not None else None,
        "carbon_g_total": round(_m["carbon_g"], 4) if _m["carbon_g"] is not None else None,
        "avg_intensity":  _avg_intensity,
    }
    return out


def _configured_ev_meter_id(cfg):
    """The configured EV sub-meter id, or None. The dispatch-derived EV device only
    applies when there's no real EV meter measuring it — so this gates the whole
    feature: any account WITH an EV sub-meter is unaffected (byte-identical)."""
    for mid, md in (cfg.get("meters") or {}).items():
        meta = md.get("meta") or {}
        if not meta.get("sub_meter"):
            continue
        if (meta.get("meter_type") in ("ev", "ev_charger")   # BL-40: config UI writes 'ev'
                or any(kw in mid.lower() for kw in ("ev", "charger"))):
            return mid
    return None


def _dispatch_derived_ev_kwh(dispatch_rows) -> dict:
    """EV kWh per half-hour slot reconstructed from COMPLETED dispatch deltas —
    Octopus's own per-slot EV energy. Excludes planned/started phantoms (a scheduled
    charge the car never took) and slots with no delivery. Validated ~99% against a
    real CT-clamp EV meter over 19 days (residual is clamp scatter, not missed
    energy). Returns {slot_start: kwh}. Pure — takes dispatch_history-shaped rows."""
    out: dict = {}
    for r in dispatch_rows:
        if r.get("kind") != "completed":
            continue
        e = r.get("energy_kwh")
        if e is None:
            continue
        v = abs(float(e))
        if v > 1e-9:
            out[r["slot_start"]] = out.get(r["slot_start"], 0.0) + v
    return out


def _ev_slot_share(slot, main_by_slot, ev_by_slot, stored_by_slot, bill_split_slots):
    """One half-hour's EV of an account with no EV device: (kWh, £, from_stored) or None — the
    rule `_dispatch_ev_split_by_bucket` explains below, for it and the rate brackets (Flow design
    §5, 4 Nov 2026), so the two cannot disagree. `from_stored`: the stored EV slices, scaled by
    kWh / their kWh (the clip to the meter), are the share; else a pro-rata carve at the
    half-hour's blended rate."""
    _e = ev_by_slot.get(slot, 0.0)
    _mk, _mc = main_by_slot.get(slot, (0.0, 0.0))
    _sv = stored_by_slot.get(slot)
    _bill_spoke = ((_sv is not None) if bill_split_slots is None
                   else (slot in bill_split_slots))
    if _bill_spoke:
        if _sv is None or _sv[0] <= 1e-9:
            return None                                   # the bill billed no EV this slot
        _ek = min(_sv[0], _mk) if _mk > 0 else 0.0
        _ec = _sv[1] * (_ek / _sv[0]) if _sv[0] > 0 else 0.0
        _stored = True
    else:
        if slot not in ev_by_slot:
            return None                                   # no dispatch -> no predicted EV
        if _sv is not None and _sv[0] > 1e-9:
            _ek = min(_sv[0], _mk) if _mk > 0 else 0.0
            _ec = _sv[1] * (_ek / _sv[0]) if _sv[0] > 0 else 0.0
            _stored = True
        else:
            _ek = min(_e, _mk) if _mk > 0 else 0.0        # can't exceed the grid that slot
            _ec = _mc * (_ek / _mk) if _mk > 0 else 0.0
            _stored = False
    if _ek <= 1e-9:
        return None
    return _ek, _ec, _stored


def _dispatch_ev_split_by_bucket(main_by_slot, ev_by_slot, bucket_fn,
                                 stored_by_slot=None, bill_split_slots=None) -> dict:
    """Grid-clip the per-slot dispatch EV to that slot's main import and apportion its
    cost, then bucket to {bucket: {"kwh","cost"}}. Pure — `main_by_slot` is
    {slot: (imp_kwh, imp_cost)}, `ev_by_slot` is {slot: kwh}.

    BL-9: when `stored_by_slot` supplies a slot's STORED split ({slot: (ev_kwh,
    ev_cost)} — the bill-authoritative 4-rate figures), that is used in preference to
    the pro-rata carve, so this matches the billing summary on a capped account. Slots
    without a stored split (un-backfilled / older history) fall back to the pro-rata
    carve (EV at the block's own rate). On an uncapped account the two are identical, so
    the bucketed result is byte-identical there. The clip keeps house + EV == the slot's
    grid import, so a caller can split a chart segment without moving any total."""
    stored_by_slot = stored_by_slot or {}
    out: dict = {}
    # WHETHER THE BILL HAS SPOKEN picks the authority for a slot; the dispatch map is not
    # the index, and neither is settlement.
    #
    #   BILL HAS STATED THE SPLIT (a device breakdown is held for this half-hour) -> the
    #     BILL decides. Octopus has priced it into four buckets, so the stored split IS the
    #     answer and its ABSENCE is also an answer: the bill billed no EV here, and a
    #     dispatch row must not manufacture one. Such slots need no dispatch record at all,
    #     which is what makes imported history work — Octopus serves a short rolling
    #     dispatch window and keeps no history, so no historic slot has one.
    #   BILL SILENT -> DISPATCH decides: no completed dispatch, no EV. The stored column,
    #     where the live split already wrote one, is that same dispatch answer priced
    #     cap-aware across the bands, so it is preferred over re-deriving a pro-rata carve
    #     at the block's blended rate (BL-9 — unchanged behaviour).
    #
    # Keyed on the breakdown, NOT on `rate_source`: that records a settled COST, which is
    # equally true of a slot whose four buckets were never fetched. Reading the missing
    # split as a denial hid EV on half-hours with a completed dispatch proving otherwise.
    #
    # bill_split_slots=None means the caller cannot say; a stored split is then taken at
    # face value, which is the pre-existing behaviour. Sorted walk keeps sums order-stable.
    for _slot in sorted(set(ev_by_slot) | set(stored_by_slot)):
        _share = _ev_slot_share(_slot, main_by_slot, ev_by_slot, stored_by_slot, bill_split_slots)
        if _share is None:
            continue
        _ek, _ec, _stored = _share
        _b = bucket_fn(_slot)
        _acc = out.setdefault(_b, {"kwh": 0.0, "cost": 0.0})
        _acc["kwh"]  += _ek
        _acc["cost"] += _ec
    return out


def _hybrid_ev_by_block(main_ev_by_block, phys_ev_by_block=None):
    """Single source of truth for the EV of a block, HYBRID across the dispatch seam —
    shared by every surface (charts/spiral, Usage Stats, usage + carbon insights) so they
    can never disagree. Per block:

      * SYNTHETIC (dispatch) is authority WHENEVER it exists — the seam signal is
        the block HAS an EV split (ev_split: its EV segments, the ones the bill prices; it was
        `imp_kwh_ev IS NOT NULL` until step 5), written only on dispatched IOG slots, so it is
        exactly the seam, per block, no date hard-coded). Yields (ev kWh, ev £, 'synthetic'); the physical EV meter is SUPERSEDED
        for that block — its metered draw is the same car, so it is never added.
      * else RECORDED — the physical EV sub-meter (HA-recorder / live Zappi) is authority:
        its metered grid kWh + cost at the block's own rate. Yields (kwh, cost, 'recorded').
      * neither → the block has no EV; omitted.

    Pure/testable. `main_ev_by_block` is {block_start: (ev kWh|None, ev £|None)} (ev_split);
    `phys_ev_by_block` is {block_start: (kwh, cost)} or None (no physical EV meter). A
    non-IOG / no-dispatch account has no EV split anywhere, so with no physical EV
    meter this returns {} and every caller stays byte-identical (the standing guard)."""
    phys = phys_ev_by_block or {}
    out: dict = {}
    for _bs in (set(main_ev_by_block) | set(phys)):
        _syn = main_ev_by_block.get(_bs)
        if _syn is not None and _syn[0] is not None:      # an EV split → synthetic wins
            _k = float(_syn[0]); _c = float(_syn[1] or 0.0)
            if _k > 1e-9 or abs(_c) > 1e-12:
                out[_bs] = {"kwh": _k, "cost": _c, "source": "synthetic"}
            continue                                       # synthetic is authority even at 0 — no fallback
        _p = phys.get(_bs)                                 # else the recorded physical device
        if _p is not None and float(_p[0] or 0.0) > 1e-9:
            out[_bs] = {"kwh": float(_p[0]), "cost": float(_p[1] or 0.0), "source": "recorded"}
    return out


def _apply_ev_split_to_summary_rows(rows, meters_list, ev_by_bucket,
                                    color="#8b5cf6", label="EV",
                                    key_fn=None) -> bool:
    """Split each row's electricity_main import segment into a derived EV slice + a
    reduced house slice, per {bucket: {"kwh","cost"}} from _dispatch_ev_split_by_bucket.
    DISPLAY-ONLY: the row's top-level imp_kwh/imp_cost/net_cost are never touched, so the
    chart totals, the data-table Totals row, and the bill stay byte-identical — only the
    'Direct' stack segment is subdivided. Appends an 'ev_dispatch' meter to meters_list
    when any row splits. Mutates in place; returns True if applied. Pure/testable.

    `key_fn(row)` maps a row to its bucket key — defaults to the local date (the daily /
    monthly / yearly views); the HH view passes `lambda r: r["slot"]` so the same carve
    lands on the half-hour bars."""
    def _date_key(_row):
        return "%04d-%02d-%02d" % (_row["year"], _row["month"], _row["day"])
    _key = key_fn or _date_key
    applied = False
    for _row in rows:
        _ev = ev_by_bucket.get(_key(_row))
        _mm = (_row.get("meters") or {}).get("electricity_main")
        if not _ev or not _mm:
            continue
        _evk, _evc = _ev["kwh"], _ev["cost"]
        if _mm["imp_kwh"] >= 0:            # don't over-split a normal day
            _evk = min(_evk, _mm["imp_kwh"])
        if _mm["imp_cost"] >= 0:           # a plunge-price CREDIT keeps its sign
            _evc = min(_evc, _mm["imp_cost"])
        if _evk <= 1e-9:
            continue
        _mm["imp_kwh"]  = round(_mm["imp_kwh"]  - _evk, 4)
        _mm["imp_cost"] = round(_mm["imp_cost"] - _evc, 4)
        _row["meters"]["ev_dispatch"] = {
            "imp_kwh": round(_evk, 4), "imp_cost": round(_evc, 4),
            "exp_kwh": 0.0, "exp_cost": 0.0, "carbon_g": None,
        }
        applied = True
    if applied:
        meters_list.append({"id": "ev_dispatch", "label": label,
                            "color": color, "is_sub": True})
    return applied


def _apply_hybrid_ev_to_summary_rows(rows, meters_list, ev_meter_id, hybrid_by_bucket,
                                     key_fn=None, color="#8b5cf6", label="EV"):
    """Fold a PHYSICAL EV sub-meter's chart series into the HYBRID EV series so every
    surface tells one story. Per bucket the EV value becomes the resolver's hybrid figure
    (synthetic post-seam, recorded physical pre-seam, via _hybrid_ev_by_block); the
    DIFFERENCE against the physical value already in the row is moved to/from the 'Direct'
    (electricity_main) segment, so the bucket TOTAL — and the bill — never move. The series
    is relabelled to one continuous 'EV' identity (colour + label). Import kWh/cost only;
    carbon is unified separately (its axis is grid-intensity derived, not a stored sub-meter
    column). Mutates in place; returns True if any bucket's split changed. Pure/testable.

    `hybrid_by_bucket` is {bucket: {'kwh','cost'}}. `key_fn(row)` -> bucket key; defaults to
    the local date (daily/monthly/yearly); the HH view passes the slot key."""
    def _date_key(_row):
        return "%04d-%02d-%02d" % (_row["year"], _row["month"], _row["day"])
    _key = key_fn or _date_key
    changed = False
    for _row in rows:
        _meters = _row.get("meters")
        if not _meters:
            continue
        _hv = hybrid_by_bucket.get(_key(_row))
        _new_k = float(_hv["kwh"])  if _hv else 0.0
        _new_c = float(_hv["cost"]) if _hv else 0.0
        _mm = _meters.get(ev_meter_id)
        _old_k = float(_mm["imp_kwh"])  if _mm else 0.0
        _old_c = float(_mm["imp_cost"]) if _mm else 0.0
        _dk = round(_new_k - _old_k, 6)
        _dc = round(_new_c - _old_c, 6)
        if _mm is not None and abs(_dk) <= 1e-9 and abs(_dc) <= 1e-9:
            continue                                    # pre-seam / recorded: identical, no move
        _main = _meters.get("electricity_main")
        # H2-fix: never push the house remainder (Direct import) below zero. On a slot where
        # the BATTERY also drew from the grid, the dispatch-derived synthetic EV can exceed the
        # grid actually available to the car (the rest of its charge came from the battery, which
        # dispatch can't see); moving that excess out of Direct would render a spurious NEGATIVE
        # bar. Cap the move to what Direct holds (kWh + its proportional cost); the EV then shows
        # the grid-available amount and the bucket total is still preserved.
        if _main is not None and _dk > 0:
            _avail = max(float(_main["imp_kwh"]), 0.0)
            if _dk > _avail:
                # B5 (H2-cost-fix): the kWh cap floors Direct to 0 — its COST must floor with
                # it. Move ALL of Direct's held cost, not the kWh-proportional slice (which
                # prices the excess at the car's rate and leaves a spurious ± residual — a
                # negative Direct £ bar — when the car's rate differs from the displaced house
                # rate). EV absorbs the whole grid slot; the bucket total is still preserved.
                _dc = round(float(_main["imp_cost"]), 6)
                _dk = round(_avail, 6)
        if _main is not None:                           # hold the bucket total invariant
            _main["imp_kwh"]  = round(float(_main["imp_kwh"])  - _dk, 4)
            _main["imp_cost"] = round(float(_main["imp_cost"]) - _dc, 4)
        _meters[ev_meter_id] = {
            "imp_kwh": round(_old_k + _dk, 4), "imp_cost": round(_old_c + _dc, 4),
            "exp_kwh": 0.0, "exp_cost": 0.0, "carbon_g": (_mm or {}).get("carbon_g"),
        }
        changed = True
    for _m in meters_list:                              # one continuous 'EV' identity
        if _m.get("id") == ev_meter_id:
            _m["label"] = label
            # H6c: keep the device's own legend colour (its palette index) — do NOT force the
            # synthetic purple, so 'EV' sits at the device colour that came before and stays
            # distinguishable from the blue house line (accessibility — see roadmap BL-29).
            break
    return changed


# ── The rows, for /api/energy and (until deleted) the two /api/charts routes ──────────

def _main_meta(cfg) -> dict:
    """The first non-sub-meter's meta: currency, billing day, block size (the site's)."""
    for md in (cfg.get("meters") or {}).values():
        if not (md.get("meta") or {}).get("sub_meter"):
            return md.get("meta") or {}
    return {}


def page_meta(cfg) -> dict:
    """What the Usage Stats and Spiral pages draw with besides the rows: the meters in
    stack order with label, colour and whether each is a device; the export colour; the
    currency and block size; whether a postcode (regional carbon) is configured."""
    import energy_charts as _ec
    meta = _main_meta(cfg)
    colors = _ec.build_meter_colors_from_config(cfg)
    labels = {}
    for mid, mc in (cfg.get("meters") or {}).items():
        m = mc.get("meta") or {}
        labels[mid] = ((m.get("device") or m.get("site") or mid) if m.get("sub_meter") else "Direct")
    meters = [{"id": mid, "label": labels.get(mid, mid), "color": colors[mid],
               "is_sub": mid != "electricity_main"}
              for mid in colors if mid != "electricity_main_export"]
    postcode = ""
    for md in (cfg.get("meters") or {}).values():
        postcode = ((md.get("meta") or {}).get("postcode_prefix") or "").strip()
        if postcode:
            break
    return {"currency": meta.get("currency_symbol", "£"),
            "billing_day": int(meta.get("billing_day") or 1),
            "block_minutes": int(meta.get("block_minutes") or 30),
            "meters": meters,
            "export_color": colors.get("electricity_main_export", "#ff7f0e"),
            "has_postcode": bool(postcode)}


def _raw_rows(store, utc_start, utc_end):
    """Every block in [utc_start, utc_end), with the meter's device flag from the ACTIVE
    config period (as the two routes always read it).

    Ordered by block, then insertion (`id`). Until #47 (2 Oct 2026) it said `ORDER BY
    block_start` alone, and the order of meters within a half-hour was the plan's. That
    order decides how two devices' costs in one half-hour interleave in a float sum, so
    the last bits of a day's total depended on the SQLite build: rowid order on the Mac,
    another on CI's Linux runners."""
    active = ("SELECT id FROM config_periods "
              "WHERE effective_to IS NULL ORDER BY effective_from DESC LIMIT 1")
    return store._conn.execute(f"""
        SELECT b.block_start, b.meter_id, m.is_sub_meter, m.parent_meter_id,
               b.imp_kwh, b.imp_kwh_grid,
               b.imp_rate, b.imp_cost, b.exp_kwh, b.exp_cost,
               {_evsplit.sql_kwh('b')} AS ev_kwh, {_evsplit.sql_cost('b')} AS ev_cost, b.rate_source, b.source,
               b.standing_charge, b.carbon_g,
               b.interpolated, cp.billing_day
        FROM blocks b
        JOIN config_periods cp ON b.config_period_id = cp.id
        LEFT JOIN meters m ON m.meter_id = b.meter_id
          AND m.config_period_id = ({active})
        WHERE b.block_start >= ? AND b.block_start < ?
        ORDER BY b.block_start, b.id
    """, (utc_start, utc_end)).fetchall()


_ACTIVE_PERIOD = ("SELECT id FROM config_periods "
                  "WHERE effective_to IS NULL ORDER BY effective_from DESC LIMIT 1")


def _day_tuples(store, utc_start, utc_end):
    """`_raw_rows`' blocks as `_aggregate_tuples` rows: the columns the aggregation reads,
    as plain tuples, with no joins (#47, 2 Oct 2026).

    The two joins `_raw_rows` makes are resolved first and written into the query as
    CASEs: a meter's device flag in the ACTIVE config period (absent → not a device), and
    a config period's billing day. A block whose config period does not exist is left
    out, as the inner join left it out.

    Order: `_raw_rows`' order — by block, then insertion (`id`). It decides how two
    devices' costs in one half-hour interleave in a float sum: ordered by `meter_id`
    instead, 134 of 1,214 days on the production-like database moved in the last bits. The unary `+` keeps the
    planner on `idx_blocks_start`, which yields that order with no sort; with the period
    term indexable it chose `idx_blocks_period` and sorted. The covering index
    `idx_blocks_insights` was measured too, and was slower on the Mac (0.109 s against
    0.097 s, dev database): it needs a sort within each half-hour for the `id` tie-break."""
    c = store._conn
    act = c.execute(_ACTIVE_PERIOD).fetchone()
    subs = [r[0] for r in c.execute(
        "SELECT meter_id FROM meters WHERE config_period_id = ? AND is_sub_meter",
        (act[0] if act else None,))]
    periods = c.execute("SELECT id, billing_day FROM config_periods").fetchall()
    if not periods:
        return []
    sub_case = ("CASE WHEN b.meter_id IN (%s) THEN 1 ELSE 0 END" % ",".join("?" * len(subs))
                if subs else "0")
    bd_case = "CASE b.config_period_id %s END" % " ".join("WHEN ? THEN ?" for _ in periods)
    params = list(subs) + [x for p in periods for x in p] + [p[0] for p in periods] \
        + [utc_start, utc_end]
    cur = c.cursor()
    cur.row_factory = None
    return cur.execute(f"""
        SELECT b.block_start, b.meter_id, {sub_case}, {bd_case},
               b.imp_kwh, b.imp_kwh_grid, b.imp_rate, b.imp_cost,
               b.exp_kwh, b.exp_cost, b.standing_charge, b.carbon_g
        FROM blocks b
        WHERE +b.config_period_id IN ({",".join("?" * len(periods))})
          AND b.block_start >= ? AND b.block_start < ?
        ORDER BY b.block_start, b.id
    """, params).fetchall()


def _ev_rows(store, cfg, meters, utc_start, utc_end):
    """The blocks `_apply_ev` reads, and only those — never all history (#47).

    `_apply_ev` keys its maps by `block_start` and lets a later non-device row at the same
    instant overwrite an earlier one, so for each instant it reads, EVERY non-device row is
    returned, in block order. The instants are the ones that can carry EV: an EV split
    (ev_split), a completed dispatch, or, with an EV sub-meter, that meter's blocks."""
    ev_mid = _configured_ev_meter_id(cfg)
    dispatch = ev_mid is None and len(meters) == 1 and meters[0]["id"] == "electricity_main"
    if ev_mid is None and not dispatch:
        return []
    instants = _evsplit.sql_instants()
    params = [utc_start, utc_end]
    if dispatch:
        instants += (" UNION SELECT slot_start FROM dispatch_history WHERE kind = 'completed' "
                     "AND slot_start >= ? AND slot_start < ?")
        params += [utc_start, utc_end]
    own = ""
    if ev_mid is not None:
        own = "OR b.meter_id = ?"
        params.append(ev_mid)
    return store._conn.execute(f"""
        SELECT b.block_start, b.meter_id, m.is_sub_meter, b.imp_kwh, b.imp_kwh_grid,
               b.imp_cost,
               CASE WHEN COALESCE(m.is_sub_meter, 0) = 0 THEN {_evsplit.sql_kwh('b')} END AS ev_kwh,
               CASE WHEN COALESCE(m.is_sub_meter, 0) = 0 THEN {_evsplit.sql_cost('b')} END AS ev_cost
        FROM blocks b
        JOIN config_periods cp ON b.config_period_id = cp.id
        LEFT JOIN meters m ON m.meter_id = b.meter_id
          AND m.config_period_id = ({_ACTIVE_PERIOD})
        WHERE b.block_start >= ? AND b.block_start < ?
          AND ((COALESCE(m.is_sub_meter, 0) = 0 AND b.block_start IN ({instants})) {own})
        ORDER BY b.block_start, b.id
    """, [utc_start, utc_end] + params).fetchall()


def _apply_ev(store, cfg, raw, rows, meters, utc_start, utc_end, bucket_of, key_fn,
              per_slot=False):
    """The display-only EV split onto `rows` (in place), bucketed by `bucket_of`.
    `per_slot`: the rows ARE half-hour slots (the HH view)."""
    if (_configured_ev_meter_id(cfg) is None
            and len(meters) == 1 and meters[0]["id"] == "electricity_main"):
        try:
            drows = store._conn.execute(
                "SELECT slot_start, kind, energy_kwh FROM dispatch_history "
                "WHERE kind='completed' AND slot_start >= ? AND slot_start < ?",
                (utc_start, utc_end)).fetchall()
            ev_slot = _dispatch_derived_ev_kwh(
                [{"slot_start": r["slot_start"], "kind": r["kind"],
                  "energy_kwh": r["energy_kwh"]} for r in drows])
            main_slot, stored_slot = {}, {}
            for rr in raw:
                if rr["is_sub_meter"]:
                    continue
                main_slot[rr["block_start"]] = (float(rr["imp_kwh"] or 0), float(rr["imp_cost"] or 0))
                if rr["ev_kwh"] is not None:          # ev_split: the EV segments
                    stored_slot[rr["block_start"]] = (float(rr["ev_kwh"]),
                                                      float(rr["ev_cost"] or 0))
            # Slots the BILL has actually described — not merely cost-settled ones.
            bill_slot = store.slots_with_bill_split(utc_start, utc_end)
            by_bucket = _dispatch_ev_split_by_bucket(
                main_slot, ev_slot, bucket_of, stored_by_slot=stored_slot,
                bill_split_slots=bill_slot)
        except Exception:
            by_bucket = {}
        import energy_charts as _ec
        _apply_ev_split_to_summary_rows(rows, meters, by_bucket, key_fn=key_fn,
                                        color=_ec.ev_dispatch_style(store)["bar"])
    elif _configured_ev_meter_id(cfg) is not None:
        # Hybrid EV (H2): physical sub-meter pre-seam, synthetic dispatch post-seam.
        ev_mid = _configured_ev_meter_id(cfg)
        try:
            main_ev, phys_ev = {}, {}
            for rr in raw:
                if rr["meter_id"] == ev_mid:
                    gk = rr["imp_kwh_grid"]
                    phys_ev[rr["block_start"]] = (float(gk if gk is not None else (rr["imp_kwh"] or 0.0)),
                                                  float(rr["imp_cost"] or 0.0))
                elif not rr["is_sub_meter"]:
                    main_ev[rr["block_start"]] = (rr["ev_kwh"], rr["ev_cost"])
            hyb_slot = _hybrid_ev_by_block(main_ev, phys_ev)
        except Exception:
            hyb_slot = {}
        if per_slot:
            _apply_hybrid_ev_to_summary_rows(rows, meters, ev_mid, hyb_slot, key_fn=key_fn)
        else:
            _apply_hybrid_ev_by_slot(store, rows, meters, ev_mid, hyb_slot, phys_ev,
                                     bucket_of, key_fn)


def _slot_rows_for(store, slots):
    """`_raw_rows`' columns for the blocks at `slots` (every meter), in `_raw_rows`' order."""
    slots = sorted(slots)
    active = ("SELECT id FROM config_periods "
              "WHERE effective_to IS NULL ORDER BY effective_from DESC LIMIT 1")
    out = []
    for i in range(0, len(slots), 500):
        part = slots[i:i + 500]
        out += store._conn.execute(f"""
            SELECT b.block_start, b.meter_id, m.is_sub_meter, m.parent_meter_id,
                   b.imp_kwh, b.imp_kwh_grid,
                   b.imp_rate, b.imp_cost, b.exp_kwh, b.exp_cost,
                   {_evsplit.sql_kwh('b')} AS ev_kwh, {_evsplit.sql_cost('b')} AS ev_cost, b.rate_source, b.source,
                   b.standing_charge, b.carbon_g,
                   b.interpolated, cp.billing_day
            FROM blocks b
            JOIN config_periods cp ON b.config_period_id = cp.id
            LEFT JOIN meters m ON m.meter_id = b.meter_id
              AND m.config_period_id = ({active})
            WHERE b.block_start IN ({",".join("?" * len(part))})
            ORDER BY b.block_start, b.id
        """, part).fetchall()
    return out


def _apply_hybrid_ev_by_slot(store, rows, meters, ev_mid, hyb_slot, phys_ev, bucket_of,
                             key_fn):
    """The hybrid EV fold for buckets coarser than a half-hour (day, month, year), done PER
    SLOT and summed into each bucket (#31, 5 Oct 2026).

    The fold caps what it moves out of Direct at what Direct holds. Folded per DAY, one
    slot's surplus covered another's shortfall, so the day view moved more EV than its own
    48 half-hours summed — the same day read differently in the two views (36 of 60 days on a
    production-like database). The half-hour cap is the physical one: a car cannot take more
    grid in a half-hour than that half-hour had. So each slot the fold can change is folded
    exactly as the HH view folds it — its Direct from `_aggregate_block_rows` bucketed by
    slot, the move from `_apply_hybrid_ev_to_summary_rows` itself — and the moves are added to
    the slot's bucket. Bucket totals still never move; a day now equals its slots."""
    slots = {bs for bs in set(hyb_slot) | set(phys_ev)
             if abs(float((hyb_slot.get(bs) or {}).get("kwh", 0.0))
                    - float((phys_ev.get(bs) or (0.0, 0.0))[0])) > 1e-9
             or abs(float((hyb_slot.get(bs) or {}).get("cost", 0.0))
                    - float((phys_ev.get(bs) or (0.0, 0.0))[1])) > 1e-9}
    moves = {}
    if slots:
        data = _aggregate_block_rows(_slot_rows_for(store, slots), lambda bs: bs,
                                     standing_scope="global")
        for bs, dd in data.items():
            row = {"slot": bs}
            row.update(_bucket_to_row_values(dd))
            before = dict(row["meters"].get(ev_mid) or {"imp_kwh": 0.0, "imp_cost": 0.0})
            _apply_hybrid_ev_to_summary_rows([row], [], ev_mid, hyb_slot,
                                             key_fn=lambda r: r["slot"])
            after = row["meters"].get(ev_mid) or before
            acc = moves.setdefault(bucket_of(bs), [0.0, 0.0])
            acc[0] += float(after["imp_kwh"]) - float(before.get("imp_kwh") or 0.0)
            acc[1] += float(after["imp_cost"]) - float(before.get("imp_cost") or 0.0)
    for row in rows:
        mv = moves.get(key_fn(row))
        _meters = row.get("meters")
        if not mv or not _meters or (abs(mv[0]) <= 1e-9 and abs(mv[1]) <= 1e-9):
            continue
        _main = _meters.get("electricity_main")
        if _main is not None:
            _main["imp_kwh"] = round(float(_main["imp_kwh"]) - mv[0], 4)
            _main["imp_cost"] = round(float(_main["imp_cost"]) - mv[1], 4)
        _mm = _meters.get(ev_mid)
        _meters[ev_mid] = {
            "imp_kwh": round(float((_mm or {}).get("imp_kwh") or 0.0) + mv[0], 4),
            "imp_cost": round(float((_mm or {}).get("imp_cost") or 0.0) + mv[1], 4),
            "exp_kwh": 0.0, "exp_cost": 0.0, "carbon_g": (_mm or {}).get("carbon_g"),
        }
    _apply_hybrid_ev_to_summary_rows([], meters, ev_mid, {})     # the one 'EV' identity


def _local_day_of(utc_start, utc_end, tz):
    """`bucket_of(block_start) -> local ISO date` for blocks in [utc_start, utc_end).

    Each local day's UTC start is worked out ONCE for the span; a block's day is then a
    binary search on its naive-UTC `block_start` string (same format, so string order is
    time order). Exact for any timezone — a 30- or 45-minute UTC offset included, which
    memoising by UTC hour would not be. It was a datetime parse + astimezone + strftime
    per block: 145,300 calls, 0.58 s of an all-history day_stats' 1.44 s on 138k blocks."""
    import bisect
    from datetime import timezone as _utc
    lo = datetime.fromisoformat(utc_start).replace(tzinfo=_utc.utc)
    hi = datetime.fromisoformat(utc_end).replace(tzinfo=_utc.utc)
    starts, names = [], []
    d = lo.astimezone(tz).date()
    while True:
        s0 = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(_utc.utc)
        if s0 >= hi:
            break
        starts.append(s0.replace(tzinfo=None).isoformat())
        names.append(d.isoformat())
        d += timedelta(days=1)
    utc = ZoneInfo("UTC")

    def _day(bs):
        i = bisect.bisect_right(starts, bs) - 1
        if i < 0:   # before the first day's start (outside the span) — compute it exactly
            return datetime.fromisoformat(bs).replace(tzinfo=utc).astimezone(tz).strftime("%Y-%m-%d")
        return names[i]
    return _day


def day_stats(store, cfg, tz_name, utc_start, utc_end) -> dict:
    """One row per LOCAL day with blocks in [utc_start, utc_end) — what /blocks-summary
    returned for the whole history before it was deleted: `year`, `month`, `day`, the billing period the day
    falls in, and `_bucket_to_row_values`' figures, EV split applied. Plus page_meta."""
    import energy_charts as _ec
    meta = page_meta(cfg)
    tz = ZoneInfo(tz_name)
    utc = ZoneInfo("UTC")

    _day = _local_day_of(utc_start, utc_end, tz)

    data = _aggregate_tuples(_day_tuples(store, utc_start, utc_end), _day,
                             standing_scope="bucket", billing_day_fallback=meta["billing_day"])
    bp = {}
    try:
        for ps, pe in _ec.get_billing_periods_from_config_periods(store.get_config_periods(), tz=tz):
            d = ps.date()
            while d < pe.date():
                bp[d.isoformat()] = (ps.date().isoformat(), pe.date().isoformat())
                d += timedelta(days=1)
    except Exception as e:
        logger.warning("day_stats: billing period pre-compute failed: %s", e)
    rows = []
    for ds in sorted(data):
        dd, d = data[ds], datetime.strptime(ds, "%Y-%m-%d").date()
        e = bp.get(ds)
        # Kept at 4 dp so the page can sum across days without per-day rounding error.
        row = {"year": d.year, "month": d.month, "day": d.day,
               "billing_day": dd["billing_day"],
               "billing_period_start": e[0] if e else ds,
               "billing_period_end": e[1] if e else None}
        row.update(_bucket_to_row_values(dd))
        rows.append(row)
    _apply_ev(store, cfg, _ev_rows(store, cfg, meta["meters"], utc_start, utc_end), rows,
              meta["meters"], utc_start, utc_end, _day,
              key_fn=lambda r: "%04d-%02d-%02d" % (r["year"], r["month"], r["day"]))
    return dict(meta, rows=rows)


def slot_stats(store, cfg, tz_name, utc_start, utc_end) -> dict:
    """One row per half-hour SLOT in [utc_start, utc_end), every slot present (zero-filled),
    keyed by its UTC start: `slot` (naive-UTC ISO) and `label` (local HH:MM). Keyed by the
    instant, a clock-change day has its 46 or 50 slots — /blocks-day keyed by local HH:MM, so
    the autumn change summed both 01:xx hours into one bar and spring showed an empty hour.
    The day's standing charge is booked once (on its first slot carrying one)."""
    meta = page_meta(cfg)
    bm = meta["block_minutes"]
    tz = ZoneInfo(tz_name)
    utc = ZoneInfo("UTC")
    raw = _raw_rows(store, utc_start, utc_end)
    data = _aggregate_block_rows(raw, lambda bs: bs, standing_scope="global",
                                 billing_day_fallback=meta["billing_day"])
    rows = []
    t, end = datetime.fromisoformat(utc_start), datetime.fromisoformat(utc_end)
    while t < end:
        key = t.isoformat()
        label = t.replace(tzinfo=utc).astimezone(tz).strftime("%H:%M")
        if key in data:
            row = {"slot": key, "label": label}
            row.update(_bucket_to_row_values(data[key]))
        else:
            row = {"slot": key, "label": label, "standing": 0,
                   "imp_kwh": 0, "exp_kwh": 0, "imp_cost": 0, "exp_cost": 0,
                   "net_cost": 0, "carbon_g_net": None, "carbon_g_total": None,
                   "avg_intensity": None,
                   "meters": {"electricity_main": {"imp_kwh": 0, "imp_cost": 0,
                              "exp_kwh": 0, "exp_cost": 0, "carbon_g": None,
                              "carbon_g_imp": None, "carbon_g_exp": None}}}
        rows.append(row)
        t += timedelta(minutes=bm)
    _apply_ev(store, cfg, raw, rows, meta["meters"], utc_start, utc_end, lambda bs: bs,
              key_fn=lambda r: r.get("slot"), per_slot=True)
    return dict(meta, rows=rows)


# ── Rate brackets (rate brackets design, BL-93) ───────────────────────────────────────────────
# Where grid import fell by the price it was charged: 2.5p brackets of the inc-VAT rate, for the
# whole meter, the house and each device. One rule for every tariff — the rate already carries
# IOG's dispatches and cap, a time-of-use window and Agile's price; the stored band label is set
# only on Intelligent (design §2). Import, and the main's export as a series of its own (§3a); no
# standing charge or non-grid use. A negative price (an Agile plunge) has brackets below 0p.

RATE_BRACKET_P = 2.5            # pence; the bracket for rate r is [2.5·n, 2.5·(n+1))
METER_SERIES = "meter"          # the whole main meter; the house is "electricity_main" (Direct)
EXPORT_SERIES = "export"        # the main's export: kWh at its export rate, £ the credit


def rate_bracket(rate: float) -> int:
    """The bracket index `n` of a rate in £/kWh: [2.5·n, 2.5·(n+1)) pence. Rounded to 4 dp of a
    penny first, so a rate on an edge is not put in the bracket below by a float's last bit
    (0.575 × 100 is 57.49999999999999)."""
    import math
    return math.floor(round(float(rate) * 100.0, 4) / RATE_BRACKET_P)


EV_SERIES = "ev_dispatch"       # an account with no devices: Octopus's EV split, as the Devices view's


def _add(acc, n, k, c, g=0.0):
    v = acc.get(n)
    if v is None:
        acc[n] = [k, c, g]
    else:
        v[0] += k
        v[1] += c
        v[2] += g


def _import_intensity(imp, exp, cg):
    """g/kWh of a main half-hour from its stored NET carbon, as the day rows split it into import
    and export (`_aggregate_tuples`): |carbon| ÷ |import − export|, or all of it on the side that
    has kWh. 0 where the half-hour has no carbon."""
    if cg is None:
        return 0.0
    net = imp - exp
    if net != 0:
        return abs(cg / net)
    if exp == 0 and imp > 0:
        return abs(cg) / imp
    if imp == 0 and exp > 0:
        return abs(cg) / exp
    return 0.0


def _rate_brackets(store, utc_start, utc_end, bucket_of) -> dict:
    """{bucket: {series: {n: [kWh, £, g]}}} over [utc_start, utc_end) (design §3):

    - `meter`: each of the main's import slices (`block_segments`) at its own rate — on IOG-SMB
      the car's and the house's rates in one half-hour stay apart;
    - a device: its grid kWh (`COALESCE(imp_kwh_grid, imp_kwh)`) at its stored rate, with its
      stored £, so its brackets sum to its device cost;
    - `ev_dispatch`, on an account with no devices: the car as the Devices view has it
      (`_ev_slot_share`) — its stored EV slices at their own rates, or a pro-rata share at the
      half-hour's blended rate (Flow design §5, 4 Nov 2026);
    - `electricity_main` (the house, Direct): per half-hour, the meter's bracket less the devices'
      (and the car's) in it, clipped at 0 where a device's one rate sits beside the meter's two;
    - `export`: the main's export kWh at its export rate, with its stored £ (the credit).

    `g`, the grams of CO₂ (Flow design §5): a main slice's and the export's at the half-hour's
    intensity (`_import_intensity`); a device's its stored carbon scaled to its grid share, as the
    day rows'; the car's its kWh at the half-hour's intensity. 0 where a half-hour has no carbon.

    A negative rate keeps its sign through all of it: an Agile plunge's kWh earn a negative £."""
    c = store._conn
    act = c.execute(_ACTIVE_PERIOD).fetchone()
    subs = [r[0] for r in c.execute(
        "SELECT meter_id FROM meters WHERE config_period_id = ? AND is_sub_meter",
        (act[0] if act else None,))]
    intensity, main_slot, exp = {}, {}, {}
    for bs, ik, ic, ek, er, ec, cg in c.execute(
            "SELECT block_start, imp_kwh, imp_cost, exp_kwh, exp_rate, exp_cost, carbon_g FROM blocks "
            "WHERE meter_id = 'electricity_main' AND block_start >= ? AND block_start < ? "
            "ORDER BY block_start",
            (utc_start, utc_end)):
        ik, ek = float(ik or 0.0), float(ek or 0.0)
        g_per = _import_intensity(ik, ek, None if cg is None else float(cg))
        intensity[bs] = g_per
        main_slot[bs] = (ik, float(ic or 0.0))
        if ek > 0:
            cost = float(ec or 0.0)
            rate = float(er) if er is not None else cost / ek
            _add(exp.setdefault(bs, {}), rate_bracket(rate), ek, cost, ek * g_per)
    main: dict = {}
    ev_slices: dict = {}
    for bs, k, r, att in c.execute(
            "SELECT block_start, kwh, inc_rate, attribution FROM block_segments "
            "WHERE meter_id = 'electricity_main' AND channel = 'import' "
            "AND block_start >= ? AND block_start < ? ORDER BY block_start, seq",
            (utc_start, utc_end)):
        k = float(k or 0.0)
        if k <= 0.0:
            continue
        g_per = intensity.get(bs, 0.0)
        _add(main.setdefault(bs, {}), rate_bracket(r), k, k * float(r), k * g_per)
        if att == "ev":
            ev_slices.setdefault(bs, []).append((k, float(r)))
    devs: dict = {}
    if subs:
        for bs, mid, k, raw, r, cost, cg in c.execute(
                "SELECT block_start, meter_id, COALESCE(imp_kwh_grid, imp_kwh), imp_kwh, imp_rate, "
                "imp_cost, carbon_g "
                "FROM blocks WHERE meter_id IN (%s) AND block_start >= ? AND block_start < ? "
                "ORDER BY block_start, id" % ",".join("?" * len(subs)),
                list(subs) + [utc_start, utc_end]):
            k = float(k or 0.0)
            if k <= 0.0:
                continue
            cost = float(cost or 0.0)
            rate = float(r) if r is not None else cost / k
            raw = float(raw or 0.0)
            g = float(cg) * (k / raw) if cg is not None and raw > 1e-9 else 0.0
            _add(devs.setdefault(bs, {}).setdefault(mid, {}), rate_bracket(rate), k, cost, g)
    else:
        # The car of an account with no devices, half-hour by half-hour as the Devices view splits
        # it (`_apply_ev`): its dispatches, its stored split, and whether the bill has spoken.
        ev_by_slot = _dispatch_derived_ev_kwh([
            {"slot_start": r[0], "kind": r[1], "energy_kwh": r[2]} for r in c.execute(
                "SELECT slot_start, kind, energy_kwh FROM dispatch_history "
                "WHERE kind = 'completed' AND slot_start >= ? AND slot_start < ?",
                (utc_start, utc_end))])
        stored = {bs: (sum(k for k, _r in sl), sum(k * r for k, r in sl)) for bs, sl in ev_slices.items()}
        bill_slot = store.slots_with_bill_split(utc_start, utc_end)
        for bs in sorted(set(ev_by_slot) | set(stored)):
            share = _ev_slot_share(bs, main_slot, ev_by_slot, stored, bill_slot)
            if share is None:
                continue
            ek, ecost, from_stored = share
            g_per = intensity.get(bs, 0.0)
            acc = devs.setdefault(bs, {}).setdefault(EV_SERIES, {})
            if from_stored:
                f = ek / stored[bs][0]
                for k, r in ev_slices[bs]:
                    _add(acc, rate_bracket(r), k * f, k * r * f, k * f * g_per)
            else:
                _add(acc, rate_bracket(ecost / ek), ek, ecost, ek * g_per)
    out: dict = {}
    for bs in sorted(set(main) | set(devs) | set(exp)):
        b = out.setdefault(bucket_of(bs), {})
        m = main.get(bs) or {}
        d = devs.get(bs) or {}
        for n, (k, cost, g) in m.items():
            _add(b.setdefault(METER_SERIES, {}), n, k, cost, g)
            dk = sum(x[n][0] for x in d.values() if n in x)
            dc = sum(x[n][1] for x in d.values() if n in x)
            dg = sum(x[n][2] for x in d.values() if n in x)
            if k - dk > 1e-9:
                _add(b.setdefault("electricity_main", {}), n, k - dk, cost - dc, max(0.0, g - dg))
        for mid, nb in d.items():
            for n, (k, cost, g) in nb.items():
                _add(b.setdefault(mid, {}), n, k, cost, g)
        for n, (k, cost, g) in (exp.get(bs) or {}).items():
            _add(b.setdefault(EXPORT_SERIES, {}), n, k, cost, g)
    return out


def _bracket_values(b):
    return {s: {str(n): [round(v[0], 4), round(v[1], 4), round(v[2], 4)] for n, v in sorted(nb.items())}
            for s, nb in b.items()}


def day_brackets(store, tz_name, utc_start, utc_end) -> dict:
    """The rate brackets per LOCAL day in [utc_start, utc_end): `rows` [{day, b: {series:
    {n: [kWh, £, g]}}}], `n` the bracket index (`width_p` × n pence and up), `g` grams of CO₂ (Flow
    design §5). Kept at 4 dp so the page sums days into months and years without per-day rounding."""
    data = _rate_brackets(store, utc_start, utc_end,
                          _local_day_of(utc_start, utc_end, ZoneInfo(tz_name)))
    return {"width_p": RATE_BRACKET_P, "meter_series": METER_SERIES, "export_series": EXPORT_SERIES,
            "ev_series": EV_SERIES,
            "rows": [{"day": d, "b": _bracket_values(data[d])} for d in sorted(data)]}


def slot_brackets(store, utc_start, utc_end) -> dict:
    """The rate brackets per half-hour in [utc_start, utc_end), keyed by its naive-UTC start
    (`slot`, as slot_stats). Only half-hours with grid import appear."""
    data = _rate_brackets(store, utc_start, utc_end, lambda bs: bs)
    return {"width_p": RATE_BRACKET_P, "meter_series": METER_SERIES, "export_series": EXPORT_SERIES,
            "ev_series": EV_SERIES,
            "rows": [{"slot": s, "b": _bracket_values(data[s])} for s in sorted(data)]}
