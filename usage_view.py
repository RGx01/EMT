"""
usage_view.py — the Usage Stats and Spiral data: per-day and per-half-hour rows.

Served by `/api/energy` (`include=stats`): day_stats for a range of local days, slot_stats for
the half-hours of a range. C step 1 (1 Oct 2026) moved this here, unchanged, from the two
routes that built it in web/server.py — `/api/charts/blocks-summary` (a day per row, all
history) and `/api/charts/blocks-day` (a half-hour per row, one day) — and then deleted them
once the Charts page read `/api/energy`. Before deletion both routes were byte-identical to
this code on real databases, and the day rows equal on every day.

The aggregation is `_aggregate_block_rows`: the house remainder (`imp_kwh_remainder`), devices
at their grid-attributed kWh (`imp_kwh_grid`), the Direct cost as main − Σ devices (clamped per
bucket, a credit kept), the carbon apportioned to import and export, and the standing charge
once per bucket. The EV split is display-only and leaves every row total untouched: from
completed dispatches for an account without sub-meters (`_dispatch_ev_split_by_bucket`), or
the hybrid fold for one with an EV meter (`_hybrid_ev_by_block`).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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
    imp_kwh_remainder, imp_rate, imp_cost, exp_kwh, exp_cost, standing_charge,
    carbon_g (and optionally billing_day)."""
    _day_data = {}
    _global_sc_done = False
    for _r in raw_rows:
        _bk = bucket_of(_r["block_start"])
        if _bk not in _day_data:
            try:
                _bd_val = _r["billing_day"] if "billing_day" in _r.keys() else None
            except Exception:
                _bd_val = None
            _day_data[_bk] = {
                "main":        {"imp_kwh": 0.0, "imp_cost": 0.0, "exp_kwh": 0.0, "exp_cost": 0.0,
                                "carbon_g": None, "cg_imp": None, "cg_exp": None,
                                "ci_abs_carbon": 0.0, "ci_abs_net_kwh": 0.0,
                                "_main_by_rate": {}, "_sub_by_rate": {}},
                "subs":        {},
                "standing":    0.0,
                "billing_day": int(_bd_val or billing_day_fallback),
            }
        _dd = _day_data[_bk]
        _is_sub = bool(_r["is_sub_meter"])
        _imp = float(_r["imp_kwh"] or 0)
        _exp = float(_r["exp_kwh"] or 0)
        _cost_imp = float(_r["imp_cost"] or 0)
        _cost_exp = float(_r["exp_cost"] or 0)
        _sc = float(_r["standing_charge"] or 0)
        _cg = float(_r["carbon_g"]) if _r["carbon_g"] is not None else None
        if not _is_sub:
            _m = _dd["main"]
            _rate = round(float(_r["imp_rate"] or 0), 6)
            if _r["imp_kwh_remainder"] is not None:
                _m_imp = float(_r["imp_kwh_remainder"])
            elif _r["imp_kwh_grid"] is not None:
                _m_imp = float(_r["imp_kwh_grid"])
            else:
                _m_imp = _imp
            if _rate not in _dd["main"]["_main_by_rate"]:
                _dd["main"]["_main_by_rate"][_rate] = {"kwh": 0.0, "cost": 0.0}
            _dd["main"]["_main_by_rate"][_rate]["kwh"]  += _m_imp
            _dd["main"]["_main_by_rate"][_rate]["cost"] += _cost_imp
            _m["imp_kwh"] += _m_imp
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
            _mid = _r["meter_id"]
            if _mid not in _dd["subs"]:
                _dd["subs"][_mid] = {"imp_kwh": 0.0, "imp_cost": 0.0, "exp_kwh": 0.0, "exp_cost": 0.0, "carbon_g": None}
            _s = _dd["subs"][_mid]
            # Grid portion only; a real 0 (fully solar/battery-charged block) must
            # stay 0, not fall back to total consumption (see _aggregate_usage).
            _grid = _r["imp_kwh_grid"]
            _sub_imp = float(_grid) if _grid is not None else _imp
            _s["imp_kwh"]  += _sub_imp
            _s["imp_cost"] += _cost_imp
            _s["exp_kwh"]  += _exp
            _s["exp_cost"] += _cost_exp
            if _cg is not None:
                _s["carbon_g"] = (_s["carbon_g"] or 0.0) + _cg
            _rate = round(float(_r["imp_rate"] or 0), 6)
            if _rate not in _dd["main"]["_sub_by_rate"]:
                _dd["main"]["_sub_by_rate"][_rate] = {"kwh": 0.0, "cost": 0.0}
            _dd["main"]["_sub_by_rate"][_rate]["kwh"]  += _sub_imp
            _dd["main"]["_sub_by_rate"][_rate]["cost"] += _cost_imp

    for _ld, _dd in _day_data.items():
        _m = _dd["main"]
        # Derive the bucket's Direct/House cost the SAME way compute_period_net does — subtract
        # the sub-meter TOTAL from the main TOTAL once per bucket, clamping the remainder at zero
        # at the DAY level, NOT per rate band. A per-rate clamp floored each band's remainder
        # independently, so on a multi-band SMB day a battery-heavy off-peak band could clamp to 0
        # and drop a negative the other bands would have absorbed — leaving Usage Stats a few pence
        # above Billing. Per-day clamp matches compute_period_net exactly; single-rate days are
        # unchanged (one band → per-rate == per-day), so flat/Economy-7 stay byte-identical. A
        # genuinely negative main total (Agile plunge-price CREDIT) still survives.
        _tot_main = sum(_mv["cost"] for _mv in (_m.get("_main_by_rate") or {}).values())
        _tot_sub  = sum(_sv["cost"] for _sv in (_m.get("_sub_by_rate") or {}).values())
        _d = _tot_main - _tot_sub
        # `imp_cost` is the DISPLAYED Direct/House line and stays clamped -- a
        # negative remainder is nonsense to render as a bar.
        _m["imp_cost"] = _d if _tot_main < 0 else max(0.0, _d)
        # `imp_cost_metered` is the MAIN METER's cost, unclamped, and is what the
        # row totals must use. Keeping only the clamped value meant the row total
        # (`Direct + SUM(subs)`) became SUM(subs) on any day the sub-meters
        # out-read the main -- which is every sunny day on a site with a battery,
        # because the battery charges off solar and registers kWh the grid never
        # supplied. That inflated one 4.5.14 report by £22.42 over 29 days.
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
    return {
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
        _e = ev_by_slot.get(_slot, 0.0)
        _mk, _mc = main_by_slot.get(_slot, (0.0, 0.0))
        _sv = stored_by_slot.get(_slot)
        _bill_spoke = ((_sv is not None) if bill_split_slots is None
                       else (_slot in bill_split_slots))
        if _bill_spoke:
            if _sv is None or _sv[0] <= 1e-9:
                continue                                  # the bill billed no EV this slot
            _ek = min(_sv[0], _mk) if _mk > 0 else 0.0
            _ec = _sv[1] * (_ek / _sv[0]) if _sv[0] > 0 else 0.0
        else:
            if _slot not in ev_by_slot:
                continue                                  # no dispatch -> no predicted EV
            if _sv is not None and _sv[0] > 1e-9:
                _ek = min(_sv[0], _mk) if _mk > 0 else 0.0
                _ec = _sv[1] * (_ek / _sv[0]) if _sv[0] > 0 else 0.0
            else:
                _ek = min(_e, _mk) if _mk > 0 else 0.0    # can't exceed the grid that slot
                _ec = _mc * (_ek / _mk) if _mk > 0 else 0.0
        if _ek <= 1e-9:
            continue
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
        `imp_kwh_ev IS NOT NULL` (the BL-9 bill-authoritative column, written only on
        dispatched IOG slots, so it is exactly the seam, per block, no date hard-coded).
        Yields (imp_kwh_ev, imp_cost_ev, 'synthetic'); the physical EV meter is SUPERSEDED
        for that block — its metered draw is the same car, so it is never added.
      * else RECORDED — the physical EV sub-meter (HA-recorder / live Zappi) is authority:
        its metered grid kWh + cost at the block's own rate. Yields (kwh, cost, 'recorded').
      * neither → the block has no EV; omitted.

    Pure/testable. `main_ev_by_block` is {block_start: (imp_kwh_ev|None, imp_cost_ev|None)};
    `phys_ev_by_block` is {block_start: (kwh, cost)} or None (no physical EV meter). A
    non-IOG / no-dispatch account has imp_kwh_ev NULL throughout, so with no physical EV
    meter this returns {} and every caller stays byte-identical (the standing guard)."""
    phys = phys_ev_by_block or {}
    out: dict = {}
    for _bs in (set(main_ev_by_block) | set(phys)):
        _syn = main_ev_by_block.get(_bs)
        if _syn is not None and _syn[0] is not None:      # imp_kwh_ev IS NOT NULL → synthetic wins
            _k = float(_syn[0]); _c = float(_syn[1] or 0.0)
            if _k > 1e-9 or abs(_c) > 1e-12:
                out[_bs] = {"kwh": _k, "cost": _c, "source": "synthetic"}
            continue                                       # synthetic is authority even at 0 — no fallback
        _p = phys.get(_bs)                                 # else the recorded physical device
        if _p is not None and float(_p[0] or 0.0) > 1e-9:
            out[_bs] = {"kwh": float(_p[0]), "cost": float(_p[1] or 0.0), "source": "recorded"}
    return out


def _apply_ev_split_to_summary_rows(rows, meters_list, ev_by_bucket,
                                    color="#8b5cf6", label="EV (from dispatch)",
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
    config period (as the two routes always read it)."""
    active = ("SELECT id FROM config_periods "
              "WHERE effective_to IS NULL ORDER BY effective_from DESC LIMIT 1")
    return store._conn.execute(f"""
        SELECT b.block_start, b.meter_id, m.is_sub_meter, m.parent_meter_id,
               b.imp_kwh, b.imp_kwh_grid, b.imp_kwh_remainder,
               b.imp_rate, b.imp_cost, b.imp_cost_remainder, b.exp_kwh, b.exp_cost,
               b.imp_kwh_ev, b.imp_cost_ev, b.rate_source, b.source,
               b.standing_charge, b.carbon_g,
               b.interpolated, cp.billing_day
        FROM blocks b
        JOIN config_periods cp ON b.config_period_id = cp.id
        LEFT JOIN meters m ON m.meter_id = b.meter_id
          AND m.config_period_id = ({active})
        WHERE b.block_start >= ? AND b.block_start < ?
        ORDER BY b.block_start
    """, (utc_start, utc_end)).fetchall()


def _apply_ev(store, cfg, raw, rows, meters, utc_start, utc_end, bucket_of, key_fn):
    """The display-only EV split onto `rows` (in place), bucketed by `bucket_of`."""
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
                if rr["imp_kwh_ev"] is not None:      # BL-9: stored, bill-authoritative
                    stored_slot[rr["block_start"]] = (float(rr["imp_kwh_ev"]),
                                                      float(rr["imp_cost_ev"] or 0))
            # Slots the BILL has actually described — not merely cost-settled ones.
            bill_slot = store.slots_with_bill_split(utc_start, utc_end)
            by_bucket = _dispatch_ev_split_by_bucket(
                main_slot, ev_slot, bucket_of, stored_by_slot=stored_slot,
                bill_split_slots=bill_slot)
        except Exception:
            by_bucket = {}
        _apply_ev_split_to_summary_rows(rows, meters, by_bucket, key_fn=key_fn)
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
                    main_ev[rr["block_start"]] = (rr["imp_kwh_ev"], rr["imp_cost_ev"])
            hyb = {}
            for bs, v in _hybrid_ev_by_block(main_ev, phys_ev).items():
                acc = hyb.setdefault(bucket_of(bs), {"kwh": 0.0, "cost": 0.0})
                acc["kwh"] += v["kwh"]; acc["cost"] += v["cost"]
        except Exception:
            hyb = {}
        _apply_hybrid_ev_to_summary_rows(rows, meters, ev_mid, hyb, key_fn=key_fn)


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

    raw = _raw_rows(store, utc_start, utc_end)
    data = _aggregate_block_rows(raw, _day, standing_scope="bucket",
                                 billing_day_fallback=meta["billing_day"])
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
    _apply_ev(store, cfg, raw, rows, meta["meters"], utc_start, utc_end, _day,
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
              key_fn=lambda r: r.get("slot"))
    return dict(meta, rows=rows)
