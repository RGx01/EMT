from datetime import datetime, timedelta, timezone
from collections import defaultdict


# ─────────────────────────────────────────────────────────────
# Timezone helpers (credit: KShips)
# ─────────────────────────────────────────────────────────────

# ── Billing arithmetic lives in billing.py (deconstruction step 2) ───────────
# Re-exported so every existing caller — `_ec.calculate_billing_summary_for_period`
# and friends in web/server.py — keeps working unchanged through the demolition.
# This module is presentation; `billing` is the domain it draws. The dependency
# runs one way only: the six presentation functions below call INTO billing, and
# billing never calls back.
from billing import (                                     # noqa: F401
    _utc_to_local,
    _parse_block_start,
    bankers_round,
    bill_slots_from_blocks,
    octopus_bill_total,
    get_billing_periods_from_config_periods,
    calculate_billing_summary_for_period,
)




# ─────────────────────────────────────────────────────────────
# Colour helpers
# ─────────────────────────────────────────────────────────────

def adjust_color(hex_color, factor=0.85):
    try:
        h = hex_color.lstrip('#')
        if len(h) == 3:
            h = ''.join(c*2 for c in h)
        r = max(0, min(255, int(int(h[0:2], 16) * factor)))
        g = max(0, min(255, int(int(h[2:4], 16) * factor)))
        b = max(0, min(255, int(int(h[4:6], 16) * factor)))
        return f'#{r:02x}{g:02x}{b:02x}'
    except Exception:
        return '#333333'


# ─────────────────────────────────────────────────────────────
# Meter colour palette
# ─────────────────────────────────────────────────────────────

COLOR_PALETTE = [
    "#1f77b4",  # blue       → main electricity meter
    "#e377c2",  # pink       → first sub-meter
    "#ff7f0e",  # orange     → second sub-meter
    "#7f7f7f",  # grey
    "#8c564b",  # brown
    "#bcbd22",  # lime
    "#d62728",  # red
    "#9467bd",  # purple
    "#17becf",  # cyan
]


def build_meter_colors(blocks):
    """Build meter colour map from a list of blocks (legacy — misses meters not active on sampled day)."""
    all_meters = []
    for block in blocks:
        meters = block.get("meters", {}) or {}
        if "electricity_main" not in all_meters:
            all_meters.append("electricity_main")
        for meter_name, meter in meters.items():
            if (meter or {}).get("meta", {}).get("sub_meter") and meter_name not in all_meters:
                all_meters.append(meter_name)
        main = meters.get("electricity_main", {}) or {}
        export = (main.get("channels", {}) or {}).get("export", {}) or {}
        if (export.get("kwh") or 0.0) > 0 and "electricity_main_export" not in all_meters:
            all_meters.append("electricity_main_export")
    return {m: COLOR_PALETTE[i % len(COLOR_PALETTE)] for i, m in enumerate(all_meters)}


def build_meter_colors_from_config(cfg: dict) -> dict:
    """
    Build meter colour map from the config dict rather than from blocks.
    This ensures all configured sub-meters get a colour even if they were
    added after the first block date and would be absent from a sampled day.
    Order: electricity_main, sub-meters (in config order),
           electricity_main_export (always last).
    """
    all_meters = ["electricity_main"]
    for meter_id, meter_cfg in (cfg.get("meters") or {}).items():
        meta = (meter_cfg.get("meta") or {})
        if meta.get("sub_meter") and meter_id not in all_meters:
            all_meters.append(meter_id)
    # Export gets a slot whether or not there is export data
    for meter_id, meter_cfg in (cfg.get("meters") or {}).items():
        if not (meter_cfg.get("meta") or {}).get("sub_meter"):
            if "export" in (meter_cfg.get("channels") or {}):
                if "electricity_main_export" not in all_meters:
                    all_meters.append("electricity_main_export")
    return {m: COLOR_PALETTE[i % len(COLOR_PALETTE)] for i, m in enumerate(all_meters)}


# ─────────────────────────────────────────────────────────────
# Billing period helpers
# ─────────────────────────────────────────────────────────────















# ─────────────────────────────────────────────────────────────
# Rate-row thresholds
# ─────────────────────────────────────────────────────────────

# bill_card owns the card's rows (C, 30 Sep 2026); these names stay for the day
# panel's collapse below and for Usage Insights (server._collapse_rate_tiers).
import bill_card as _bc
_MAX_RATE_ROWS = _bc.MAX_RATE_ROWS
_SPLIT_BAND_EPS = _bc.SPLIT_BAND_EPS


def _collapse_rate_kwh(rate_kwh):
    """Side-panel rate breakdown collapse. `rate_kwh` is a {rate: kwh} mapping (the
    day chart's `summary_rates[meter]`). Returns a list of (kwh, rate, n) tuples to
    render: one per rate normally, or a SINGLE kWh-weighted-average tuple (with n =
    the rate count) when there are more than _MAX_RATE_ROWS non-trivial rates — the
    same threshold as the billing summary's bill_card.rate_rows, so the side panel's
    Direct-import and per-device lines fold the same way on Agile. Weighting is by
    kWh (Σ rate·kwh ÷ Σ kwh) since only kWh is available here; that equals cost ÷ kWh
    when cost = rate·kwh, so it agrees with the summary's cost-weighted average. Only
    rates with kwh > 0.0001 are counted (matching the renderers' own filter)."""
    items = [(k, r) for r, k in sorted(rate_kwh.items()) if k > 0.0001]
    if len(items) > _MAX_RATE_ROWS:
        tot_kwh = sum(k for k, _ in items)
        avg = (sum(r * k for k, r in items) / tot_kwh) if tot_kwh else 0.0
        return [(tot_kwh, avg, len(items))]
    return [(k, r, None) for k, r in items]










def _bill_method_breakdown(blocks, period_vat=None, standing_inc_by_day=None, vat_at=None):
    """BL-24: ex-VAT bill-method breakdown for a period — import energy by rate band, plus
    ex-VAT standing, a VAT row and the inc total. Matches how Octopus actually reconciles
    (verified against a real bill): the per-half-hour figures are summed RAW and rounding
    happens only at the SUBTOTAL — NOT per half-hour (that destroys the many sub-0.01 kWh
    peak slots) and NOT the 1dp kWh the bill shows for display. Ex-VAT cost is the stored
    exc where captured, else the billed inc ÷ (1+VAT) — same for standing, so the standing
    charge still lands before ex-VAT capture. Returns a dict or None.

    VAT is PER DATE (1 Oct 2026; v4 4.5.19). A half-hour's VAT is its own inc/exc pair (snapped
    0/5/20%), else `vat_at(start)` — the VAT calendar; a standing-charge day's is
    `vat_at(day)`. Without `vat_at` every date falls back to one period rate: the first
    real pair, else `period_vat` — which is what the pre-render did for every period, so a bill
    period spanning 1 Oct 2026's change to 0% took 1 Oct's standing charge ÷ 1.05,
    labelled the VAT "@ 5%", and showed 1 Oct's ex-VAT rows twice. Rows are grouped by
    their ex-VAT rate (the table's own basis); the VAT line names each rate in force."""
    from collections import defaultdict as _dd
    import vat_calendar as _vc
    _local_day = _vc.local_day_fn()     # 17,520 half-hours a year (#48, 2 Oct 2026)
    blocks = list(blocks or [])

    def _snap(x):
        return min((0.0, 0.05, 0.20), key=lambda r: abs(r - x))

    # ── Pass 1: the period fallback rate — a real inc/exc pair, else the default ──
    raw_vat = None
    for b in blocks:
        imp = (((b or {}).get("meters") or {}).get("electricity_main") or {})
        imp = (imp.get("channels") or {}).get("import") or {}
        c, ce = imp.get("cost"), imp.get("cost_exc")
        if c and ce and ce != 0:
            raw_vat = float(c) / float(ce) - 1.0
            break
    _default_vat = 0.05 if period_vat is None else float(period_vat)
    vat = _default_vat if raw_vat is None else _snap(raw_vat)

    def _vat_of(when):
        return float(vat_at(when)) if vat_at is not None else vat

    vat_by_day = {}                      # local day → VAT in force (for the label)
    # ── Pass 2: raw ex-VAT sums per (band, VAT) + ex-VAT standing per local day ──
    bands = _dd(lambda: {"kwh": 0.0, "exc": 0.0, "inc": 0.0})
    tot_kwh = with_exc = 0.0
    stand_exc_day = {}   # local day → (ex-VAT standing, VAT) — first non-zero
    for b in blocks:
        m = ((b or {}).get("meters") or {}).get("electricity_main") or {}
        imp = (m.get("channels") or {}).get("import") or {}
        start = b.get("start") or ""
        d = _local_day(start) if start else ""
        if d and d not in stand_exc_day:
            se = m.get("standing_charge_exc")
            sc = m.get("standing_charge") or 0
            if se or sc:
                dv = _vat_of(d)
                stand_exc_day[d] = (float(se) if se else float(sc) / (1.0 + dv), dv)
        kwh = imp.get("kwh") or 0
        if not kwh:
            continue
        tot_kwh += kwh
        inc_rate = imp.get("rate")
        c, ce = imp.get("cost"), imp.get("cost_exc")
        bvat = _snap(float(c) / float(ce) - 1.0) if (c and ce) else _vat_of(start)
        if d:
            vat_by_day.setdefault(d, bvat)
        if ce is not None or imp.get("rate_exc") is not None:
            with_exc += kwh
        # Raw ex-VAT cost for this half-hour: the billed cost, ex-VAT.
        if ce is not None:
            xc = float(ce)
        elif c is not None:
            xc = float(c) / (1.0 + bvat)
        elif inc_rate is not None:
            xc = kwh * (float(inc_rate) / (1.0 + bvat))
        else:
            continue
        # Inc cost for this slot — paired with xc so the VAT amount is inc − exc (exact
        # for any mix of rates across a period).
        if c is not None:
            ic = float(c)
        elif inc_rate is not None:
            ic = kwh * float(inc_rate)
        else:
            ic = xc * (1.0 + bvat)
        # Band by the CLEAN inc-rate tariff band (not the per-slot derived exc rate, which
        # jitters with Octopus's per-slot rounding) — WITH its VAT, so the displayed exc
        # rate is inc ÷ (1 + that half-hour's VAT).
        if inc_rate is not None:
            band_key = round(float(inc_rate), 6)
        elif imp.get("rate_exc") is not None:
            band_key = round(float(imp["rate_exc"]) * (1.0 + bvat), 6)
        else:
            band_key = round(xc / float(kwh) * (1.0 + bvat), 6)
        band = bands[(band_key, bvat)]
        band["kwh"] += kwh
        band["exc"] += xc
        band["inc"] += ic
    if not bands:
        return None
    # B2: cluster adjacent band keys within _SPLIT_BAND_EPS (per VAT rate) before emitting
    # rows, so cost÷kWh jitter doesn't show one tariff as two rows; a genuine in-period
    # rate change (≥ pence) stays its own row.
    _clusters = []
    for _k in sorted(bands, key=lambda kv: (kv[1], kv[0])):
        if _clusters and _k[1] == _clusters[-1][-1][1] and (_k[0] - _clusters[-1][-1][0]) <= _SPLIT_BAND_EPS:
            _clusters[-1].append(_k)
        else:
            _clusters.append([_k])
    by_exc, energy_raw, energy_inc = {}, 0.0, 0.0
    for _cl in _clusters:
        bk = sum(bands[k]["kwh"] for k in _cl)
        be = sum(bands[k]["exc"] for k in _cl)
        bi = sum(bands[k]["inc"] for k in _cl)
        energy_raw += be
        energy_inc += bi
        # Displayed exc rate = the cluster's kWh-weighted inc-scale rate ÷ (1+its VAT).
        _wk = (sum(k[0] * bands[k]["kwh"] for k in _cl) / bk) if bk > 1e-12 else _cl[0][0]
        rx = round(_wk / (1.0 + _cl[0][1]), 4)
        # The table is ex-VAT: one row per ex-VAT rate, so the same tariff band either
        # side of a VAT change (5.493 inc at 5%, 5.2314 inc at 0%) is ONE 5.2314 row.
        r = by_exc.setdefault(rx, {"rate_exc": rx, "kwh": 0.0, "cost_exc": 0.0})
        r["kwh"] += bk
        r["cost_exc"] += be
    rows = [{"rate_exc": r["rate_exc"], "kwh": round(r["kwh"], 3), "cost_exc": round(r["cost_exc"], 3)}
            for _, r in sorted(by_exc.items())]
    # Collapse a long rate list (Agile) into ONE kWh-weighted average row.
    if len(rows) > _MAX_RATE_ROWS:
        _tot_kwh = sum(bands[k]["kwh"] for k in bands)
        _avg_exc = (energy_raw / _tot_kwh) if _tot_kwh else 0.0
        rows = [{"rate_exc": round(_avg_exc, 4), "kwh": round(_tot_kwh, 3),
                 "cost_exc": round(energy_raw, 3),
                 "collapsed": True, "n_rates": len(bands)}]

    def _group_standing(day_exc):
        # {day: ex-VAT amount} → one row per DISTINCT ex-VAT daily rate, so a mid-period
        # standing change shows as separate lines (and a VAT change alone does not).
        by_rate: dict = {}
        for _d, _amt in day_exc.items():
            g = by_rate.setdefault(round(_amt, 6), [0, 0.0])
            g[0] += 1
            g[1] += _amt
        return [{"days": g[0], "rate_exc": round(k, 4), "cost_exc": round(g[1], 2)}
                for k, g in sorted(by_rate.items())]

    # Standing: prefer the summary's per-LOCAL-day inc figure (correct day count across the
    # BST midnight boundary), ex-VAT at THAT day's VAT. Fall back to the block-derived exc.
    if standing_inc_by_day:
        _inc = {str(d)[:10]: a for d, a in standing_inc_by_day.items()}   # keys may be dates
        _vd = {d: _vat_of(d) for d in _inc}
        day_exc = {d: a / (1.0 + _vd[d]) for d, a in _inc.items()}
        standing_inc_raw = sum(standing_inc_by_day.values())
    else:
        _vd = {d: v for d, (_x, v) in stand_exc_day.items()}
        day_exc = {d: x for d, (x, _v) in stand_exc_day.items()}
        standing_inc_raw = sum(x * (1.0 + v) for x, v in stand_exc_day.values())
    for d, v in _vd.items():
        vat_by_day.setdefault(d, v)
    standing_days = len(day_exc)
    standing_raw = sum(day_exc.values())
    standing_rows = _group_standing(day_exc)
    standing_rate = round(standing_raw / standing_days, 4) if standing_days else 0.0
    subtotal_raw  = energy_raw + standing_raw            # ex-VAT subtotal
    inc_raw       = energy_inc + standing_inc_raw        # inc-VAT subtotal
    subtotal = round(subtotal_raw, 2)
    # VAT amount as inc − exc — exact for any mix of rates across the period.
    vat_amount = round(inc_raw - subtotal_raw, 2)
    # Label: each VAT rate in force, in date order, with the day it changes.
    spans = []
    for d in sorted(vat_by_day):
        if not spans or spans[-1][1] != vat_by_day[d]:
            spans.append([d, vat_by_day[d], d])
        else:
            spans[-1][2] = d
    if len(spans) <= 1:
        _v = spans[0][1] if spans else vat
        vat_label = f"VAT @ {_v * 100:.0f}%"
        vat_rate = _v
    else:
        from datetime import date as _date
        _f = lambda s: _date.fromisoformat(s).strftime("%-d %b")
        vat_label = "VAT @ " + ", ".join(
            f"{v * 100:.0f}% " + (f"to {_f(e)}" if i == 0 else
                                  f"from {_f(s)}" if i == len(spans) - 1 else
                                  f"{_f(s)}–{_f(e)}")
            for i, (s, v, e) in enumerate(spans))
        vat_rate = None
    return {
        "rows": rows,
        "energy_exc":        round(energy_raw, 2),
        "standing_days":     standing_days,
        "standing_rate_exc": standing_rate,
        "standing_rows":     standing_rows,
        "standing_exc":      round(standing_raw, 2),
        "subtotal_exc":      subtotal,
        "vat_rate":          vat_rate,
        "vat_label":         vat_label,
        "vat_amount":        vat_amount,
        "inc_total":         round(inc_raw, 2),
        "coverage":          round(with_exc / tot_kwh, 3) if tot_kwh else 0.0,
    }


def _has_sub_meters(cfg):
    """True if any configured meter is a sub-meter (EV, battery, heat-pump, …). The
    derived-EV billing breakdown only applies to accounts with NONE, so a real EV meter
    always wins."""
    for md in ((cfg or {}).get("meters") or {}).values():
        if (md.get("meta") or {}).get("sub_meter"):
            return True
    return False


def _ev_meter_id(cfg):
    """The configured physical EV-charger meter_id (meter_type 'ev_charger', or an
    'ev'/'charger' id), or None. Used for the coverage-based synthetic-EV gate."""
    for mid, md in ((cfg or {}).get("meters") or {}).items():
        meta = md.get("meta") or {}
        if (meta.get("meter_type") in ("ev", "ev_charger")   # BL-40: config UI writes 'ev'
                or any(kw in str(mid).lower() for kw in ("ev", "charger"))):
            return mid
    return None


def _has_stored_split(b):
    """True when this block carries a bill-authoritative EV split of its own — an
    EV-attributed segment or a stored kwh_ev column — independent of any dispatch row."""
    imp = (((b.get("meters") or {}).get("electricity_main") or {})
           .get("channels", {}) or {}).get("import", {}) or {}
    _segs = imp.get("segments") or []
    if any((x.get("attribution") == "ev") and float(x.get("kwh") or 0.0) > 1e-9
           for x in _segs):
        return True
    _sk = imp.get("kwh_ev")
    try:
        return _sk is not None and float(_sk) > 1e-9
    except (TypeError, ValueError):
        return False


def _dispatch_ev_slot_map(store, blocks, cfg, gated=True):
    """{slot_start(UTC ISO): {"kwh","cost","rate"}} — per-slot EV reconstructed from
    COMPLETED dispatches (Octopus's own per-slot EV energy), grid-clipped to that slot's
    main import and cost-apportioned from the slot's import cost. Gated to no-sub-meter
    accounts. Empty when gated off / no store / no dispatch. This is the display-only
    source for the derived-EV billing breakdown; it never affects a bill total."""
    if store is None or not blocks:
        return {}
    # Coverage-based gate: synthesise EV only for slots the PHYSICAL EV device doesn't
    # already have a block for. An active EV meter covers every slot (synthetic stays
    # off, as before); a retired/decommissioned one stops writing blocks, so the
    # synthetic takes over from the cutover automatically. A battery or other sub-meter
    # never blocks the EV synthetic — only the EV device's OWN coverage does.
    _evm = _ev_meter_id(cfg)
    covered = ({b["start"] for b in blocks
                if b and b.get("start") and _evm in (b.get("meters") or {})}
               if (_evm and gated) else set())
    starts = [b["start"] for b in blocks if b and b.get("start")]
    if not starts:
        return {}
    try:
        drows = store._conn.execute(
            "SELECT slot_start, energy_kwh FROM dispatch_history "
            "WHERE kind='completed' AND slot_start >= ? AND slot_start <= ?",
            (min(starts), max(starts))).fetchall()
    except Exception:
        return {}
    # Slots for which Octopus's own device breakdown is held, i.e. the bill has actually
    # STATED the split. Not the same as "settled" — see BlockStore.slots_with_bill_split.
    try:
        bill_slots = store.slots_with_bill_split(min(starts), max(starts))
    except Exception:
        bill_slots = set()
    ev_raw = {}
    for r in drows:
        e = r["energy_kwh"]
        if e is None:
            continue
        v = abs(float(e))
        if v > 1e-9:
            ev_raw[r["slot_start"]] = ev_raw.get(r["slot_start"], 0.0) + v
    out = {}
    for b in blocks:
        slot = (b or {}).get("start")
        if not slot or slot in covered:
            continue
        # WHETHER THE BILL HAS SPOKEN picks the authority. Where Octopus supplied a device
        # breakdown, that half-hour is the bill's to describe: its stored/segmented split is
        # the answer and needs no dispatch row (imported history never has one — Octopus
        # serves a short rolling window and keeps no history), while the ABSENCE of a split
        # means the bill billed no EV, so a stray dispatch row must not manufacture one.
        # Where no breakdown was ever retrieved the bill has said nothing, and dispatch
        # remains the gate exactly as before. Keyed on the breakdown and not on settlement:
        # `rate_source` records a settled COST, which is equally true of a slot whose four
        # buckets were never fetched, so reading its missing split as a denial hid EV on
        # half-hours a completed dispatch proves the car charged through.
        if slot in bill_slots:
            if not _has_stored_split(b):
                continue
        elif slot not in ev_raw:
            continue
        imp = (((b.get("meters") or {}).get("electricity_main") or {})
               .get("channels", {}) or {}).get("import", {}) or {}
        try:
            mk = float(imp.get("kwh_total", imp.get("kwh")) or 0.0)
            mc = float(imp.get("cost") or 0.0)
            rate = round(float(imp.get("rate_used", imp.get("rate")) or 0.0), 4)
        except Exception:
            continue
        if mk <= 0:
            continue
        # BL-9: prefer the STORED, bill-authoritative split (imp_kwh_ev/imp_cost_ev/
        # imp_rate_ev — the 4-rate figures the billing summary's grid-total section
        # uses) so every EV/house surface agrees with the bill on a capped account.
        # Fall back to the dispatch-derived pro-rata carve (EV at the block's own rate)
        # only where a block has no stored split — un-backfilled / older history. On an
        # uncapped account the two are identical (EV and house share the rate), so this
        # is byte-identical there.
        # BL-27: prefer the EV-attributed SEGMENTS (the single source of truth) — imp_cost_ev
        # is just their summed cost, so this is byte-identical to the stored split while
        # letting the reconcile re-stamp of the EV columns retire. Fall back to the stored
        # columns, then the dispatch pro-rata carve, for any block without segments.
        _segs = imp.get("segments")
        _seg_evk = (sum(float(x.get("kwh") or 0.0) for x in _segs
                        if x.get("attribution") == "ev") if _segs else 0.0)
        _sk = imp.get("kwh_ev")
        if _seg_evk > 1e-9:
            _seg_evc = sum(float(x.get("kwh") or 0.0) * float(x.get("inc_rate") or 0.0)
                           for x in _segs if x.get("attribution") == "ev")
            ek = min(_seg_evk, mk)
            ec = _seg_evc * (ek / _seg_evk) if _seg_evk > 0 else 0.0
            rate = round(ec / ek, 4) if ek else rate
        elif _sk is not None and float(_sk) > 1e-9:
            ek = min(float(_sk), mk)
            _sc = float(imp.get("cost_ev") or 0.0)
            ec = _sc * (ek / float(_sk)) if float(_sk) > 0 else 0.0
            _sr = imp.get("rate_ev")
            rate = round(float(_sr), 4) if _sr else (round(ec / ek, 4) if ek else rate)
        else:
            if slot not in ev_raw:                 # settled slots never reach here
                continue
            ek = min(ev_raw[slot], mk)
            if ek <= 1e-9:
                continue
            ec = mc * (ek / mk)
        if ek <= 1e-9:
            continue
        out[slot] = {"kwh": ek, "cost": ec, "rate": rate}
    return out


def _hybrid_ev_slot_map(store, blocks, cfg):
    """Per-slot HYBRID EV for the billing breakdown on a has-EV-meter account: SYNTHETIC
    (dispatch / stored imp_kwh_ev / segments — bill-authoritative) wherever it exists,
    else the RECORDED physical EV sub-meter's own metered slot. One continuous EV. The
    synthetic map is taken UNGATED (the coverage gate is what kept it off for an active
    meter); the recorded fallback fills the pre-seam slots. Total-safety is the caller's
    (fold the physical EV line into the remainder before carving this out)."""
    out = dict(_dispatch_ev_slot_map(store, blocks, cfg, gated=False))   # synthetic, all slots
    evm = _ev_meter_id(cfg)
    if evm:
        for b in blocks:
            slot = (b or {}).get("start")
            if not slot or slot in out:                    # synthetic already owns this slot
                continue
            ch = (((b.get("meters") or {}).get(evm) or {}).get("channels", {}) or {}).get("import", {}) or {}
            try:
                k = float(ch.get("kwh_total", ch.get("kwh")) or 0.0)
                c = float(ch.get("cost") or 0.0)
                r = round(float(ch.get("rate_used", ch.get("rate")) or 0.0), 4)
            except Exception:
                continue
            if k > 1e-9:
                out[slot] = {"kwh": k, "cost": c, "rate": r}   # recorded physical (pre-seam)
    return out


def _fold_ev_submeter_into_remainder(summary, ev_devices):
    """Move the physical EV sub-meter line(s) back into the main-import remainder so the
    hybrid EV can be carved from it. TOTAL-SAFE: energy only moves between the EV line and
    the remainder, so Total Import / total_cost are untouched. Matches sub-meter lines by
    device name (or an ev/charger keyword). Returns True if anything folded."""
    totals = summary.get("totals") or {}
    meters = summary.get("meters") or {}
    meta   = summary.get("meter_meta") or {}
    rem_key = next((k for k in totals
                    if k.endswith("/ Import") and not totals[k].get("is_submeter")), None)
    if not rem_key:
        return False
    rem = meters.setdefault(rem_key, {})
    def _is_ev_line(k):
        d = str((meta.get(k, {}) or {}).get("device") or "")
        return (d in ev_devices) or ("charger" in d.lower()) or ("charger" in k.lower())
    folded = False
    for key in [k for k in list(totals)
                if totals[k].get("is_submeter") and k.endswith("/ Import") and _is_ev_line(k)]:
        for rate, v in (meters.get(key) or {}).items():
            _r = rem.setdefault(rate, {"kwh": 0.0, "cost": 0.0, "read_start": None, "read_end": None})
            _r["kwh"]  = round(_r.get("kwh", 0.0)  + (v.get("kwh")  or 0.0), 3)
            _r["cost"] = round(_r.get("cost", 0.0) + (v.get("cost") or 0.0), 2)
        rt, rtr = totals[key], totals[rem_key]
        rtr["kwh"]  = round(rtr["kwh"]  + rt["kwh"],  3)
        rtr["cost"] = round(rtr["cost"] + rt["cost"], 2)
        meters.pop(key, None); totals.pop(key, None); meta.pop(key, None)
        folded = True
    return folded


def _inject_ev_breakdown_into_summary(summary, period_blocks, ev_slot_map,
                                      label="EV (from dispatch)", fold_devices=None):
    """Split the 'Direct import' remainder in a billing summary into a reduced house
    slice + a synthetic '<label> / Import' sub-meter, from ev_slot_map. DISPLAY-ONLY:
    main_import_raw ('Import — total grid'), standing, and total_cost are untouched, so
    the bill stays byte-identical — only the 'Breakdown by meter' section subdivides.
    Whatever EV is added is subtracted from the remainder, so the two always sum back to
    the original Direct import. No-op if a real sub-meter already exists, there's no
    remainder key, or there's no EV. Mutates summary in place; returns True if applied."""
    if not summary or not ev_slot_map:
        return False
    totals = summary.get("totals") or {}
    if fold_devices:                              # hybrid: fold the physical EV line into remainder,
        _fold_ev_submeter_into_remainder(summary, fold_devices)   # then carve the hybrid EV from it
    elif any(t.get("is_submeter") for t in totals.values()):
        return False                              # no-meter path: unchanged (byte-identical)
    remainder_key = next((k for k in totals
                          if k.endswith("/ Import") and not totals[k].get("is_submeter")), None)
    if not remainder_key:
        return False
    ev_by_rate = defaultdict(lambda: {"kwh": 0.0, "cost": 0.0})
    for b in period_blocks:
        slot = (b or {}).get("start")
        ev = ev_slot_map.get(slot) if slot else None
        if not ev:
            continue
        ev_by_rate[ev["rate"]]["kwh"]  += ev["kwh"]
        ev_by_rate[ev["rate"]]["cost"] += ev["cost"]
    if not ev_by_rate:
        return False
    rem_channels = summary["meters"][remainder_key]
    ev_channels = {}
    ev_tot_k = ev_tot_c = 0.0
    for rate, v in ev_by_rate.items():
        ek = round(v["kwh"], 3)
        ec = round(v["cost"], 2)
        if ek <= 0:
            continue
        ev_channels[rate] = {"kwh": ek, "cost": ec, "read_start": None, "read_end": None}
        ev_tot_k += ek
        ev_tot_c += ec
        if rate in rem_channels:
            _rk = rem_channels[rate]["kwh"] - ek
            _rc = rem_channels[rate]["cost"] - ec
            rem_channels[rate]["kwh"]  = round(_rk if rem_channels[rate]["kwh"] < 0 else max(0.0, _rk), 3)
            rem_channels[rate]["cost"] = round(_rc if rem_channels[rate]["cost"] < 0 else max(0.0, _rc), 2)
    if not ev_channels:
        return False
    ev_key = f"{label} / Import"
    summary["meters"][ev_key] = ev_channels
    summary["totals"][ev_key] = {"kwh": round(ev_tot_k, 3), "cost": round(ev_tot_c, 2),
                                 "is_submeter": True, "read_start": None, "read_end": None}
    rt = summary["totals"][remainder_key]
    _rtk = rt["kwh"] - ev_tot_k
    _rtc = rt["cost"] - ev_tot_c
    rt["kwh"]  = round(_rtk if rt["kwh"] < 0 else max(0.0, _rtk), 3)
    rt["cost"] = round(_rtc if rt["cost"] < 0 else max(0.0, _rtc), 2)
    summary.setdefault("meter_meta", {})[ev_key] = {
        "site": None, "device": label, "mpan": None, "tariff": None, "is_submeter": True}
    return True


# ─────────────────────────────────────────────────────────────
# Daily chart builder (returns HTML string for one day)
# ─────────────────────────────────────────────────────────────

def _day_segment_split(main_import, meters):
    """BL-27: on a CAPPED block (EV pushed to PEAK, or straddling the cap boundary) return
    the per-meter slot values driven by the block's dispatch SEGMENTS — the physical EV
    device on the EV bands (grid-clipped dispatch: off-peak within cap, peak beyond), house
    sub-devices at the house band rate, and 'Direct import' the house-segment remainder. So
    the house stays cleanly off-peak and the EV carries the peak, with no metered-vs-dispatch
    residual. Returns {meter_id: {"kwh","cost","rate"}} (incl. 'electricity_main' for the
    house remainder), or None — uncapped blocks (EV off-peak == house) and sensor-less
    accounts keep the column / synthetic paths, so they're byte-identical. Requires a
    physical EV sub-meter to attribute the EV bands onto."""
    segs = main_import.get("segments")
    if not segs:
        return None
    if not any(s.get("attribution") == "ev" and s.get("band") in ("peak", "mixed")
               for s in segs):
        return None
    _v = lambda x: float(x or 0.0)
    ev_k = sum(_v(s.get("kwh")) for s in segs if s.get("attribution") == "ev")
    ev_c = sum(_v(s.get("kwh")) * _v(s.get("inc_rate")) for s in segs if s.get("attribution") == "ev")
    ho_k = sum(_v(s.get("kwh")) for s in segs if s.get("attribution") != "ev")
    ho_c = sum(_v(s.get("kwh")) * _v(s.get("inc_rate")) for s in segs if s.get("attribution") != "ev")
    house_rate = (ho_c / ho_k) if ho_k > 1e-9 else 0.0
    ev_dev = None
    house_devs = []
    for _mn, _m in (meters or {}).items():
        if not (((_m or {}).get("meta", {}) or {}).get("sub_meter")):
            continue
        _mt = (((_m.get("meta") or {}).get("meter_type")) or "").lower()
        if _mt in ("ev", "ev_charger") or any(k in _mn.lower() for k in ("ev", "charger")):
            ev_dev = _mn
        else:
            house_devs.append((_mn, _m))
    if ev_dev is None or ev_k <= 1e-9:
        return None                      # no physical EV to carry the bands → leave as-is
    # The EV BAR is the physical charger's METERED grid kWh (the bar the user recognises);
    # its COST/RATE come from the dispatch split (ev_c / dispatch rate). The house remainder
    # absorbs the metered-vs-dispatch kWh difference, so Σ device kWh == grid import and the
    # remainder can never go negative from dispatch over-attributing the grid (design:
    # device-pricing "physical-EV-device question").
    _evsub = ((meters.get(ev_dev, {}).get("channels", {}) or {}).get("import", {}) or {})
    dev_ev = _v(_evsub.get("kwh_grid", _evsub.get("kwh"))) or ev_k   # metered; else dispatch
    grid_k = ev_k + ho_k                                             # block grid import (Σ seg)
    out = {ev_dev: {"kwh": round(dev_ev, 6), "cost": round(ev_c, 6),
                    "rate": round(ev_c / ev_k, 6)}}
    hk_used = hc_used = 0.0
    for _mn, _m in house_devs:
        _sub = ((_m.get("channels", {}) or {}).get("import", {}) or {})
        _gk = _v(_sub.get("kwh_grid", _sub.get("kwh")))
        _c = round(_gk * house_rate, 6)
        out[_mn] = {"kwh": round(_gk, 6), "cost": _c, "rate": round(house_rate, 6)}
        hk_used += _gk
        hc_used += _c
    out["electricity_main"] = {"kwh": round(grid_k - dev_ev - hk_used, 6),
                               "cost": round(ho_c - hc_used, 6),
                               "rate": round(house_rate, 6)}
    return out


def day_chart_data(day, day_blocks, meter_colors, *, cap_from, import_schedule, timezone_name, block_minutes=30, currency='£', ev_slot_map=None, bill_rounding=False, fallback_vat=0.05, ev_fold_meter=None, ev_label=None, store=None):
    """Everything one day's billing chart shows, as DATA — no markup.

    Returns {"chart": ..., "panel": ...}. `chart` is exactly the JSON the pre-rendered
    page embedded per day (`data_chart_<day>`): per-meter half-hourly kWh, cost and the
    charged-rate step series, plus the total-import rows the data table uses. `panel` is
    the side column: day totals, export credit, and the per-meter breakdown with its
    rate rows (already collapsed by _collapse_rate_kwh).

    Split out of build_day_chart_html (C, 30 Sep 2026) so /api/energy
    (`resolution=day&include=billing`) and the pre-rendered page are ONE derivation;
    build_day_chart_html now only formats what this returns. The rate lines' context —
    cap date, import schedule, timezone — is REQUIRED here (C rule 2): None is a valid
    answer for the first two, but the caller must give it.
    """
    slots = 1440 // block_minutes
    meter_kwh    = defaultdict(lambda: [0.0] * slots)
    meter_rate   = defaultdict(lambda: [0.0] * slots)
    meter_cost   = defaultdict(lambda: [0.0] * slots)
    slot_ti_kwh  = [0.0] * slots
    slot_ti_cost = [0.0] * slots
    slot_ti_rate = [0.0] * slots
    # BL-24 data table (opt-in): a per-slot ex-VAT/inc ratio so every IMPORT column
    # can show Cost (exc) alongside Cost (inc). Sourced from the block's stored ex-VAT
    # rate (authoritative for the main import after the 4.2 backfill); a slot with no
    # captured exc falls back to inc ÷ (1+VAT) and is flagged approximate. Only emitted
    # when the setting is on, so the default data table JSON is byte-identical.
    slot_exc_ratio = [None] * slots
    slot_exc_approx = [False] * slots
    summary_kwh  = defaultdict(float)
    summary_cost = defaultdict(float)
    summary_rates = defaultdict(lambda: defaultdict(float))
    meter_display_name = {}

    def _f(v, default=0.0):
        try:
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    for hh, block in day_blocks:
        try:
            meters = block.get("meters", {}) or {}
            main   = meters.get("electricity_main", {}) or {}
            main_import = (main.get("channels", {}) or {}).get("import", {}) or {}
            main_export = (main.get("channels", {}) or {}).get("export", {}) or {}

            # Use kwh_remainder when available (engine PASS 2 remainder — same field
            # usage stats uses). With no sub-meters this key is absent (NULL in DB,
            # omitted by both block reconstruction paths), so this falls through to
            # the main meter's kwh — api+mini blocks are structurally identical to
            # CAD here, so no mode-specific handling is needed.
            if "kwh_remainder" in main_import:
                main_kwh  = _f(main_import["kwh_remainder"])
                main_cost = _f(main_import.get("cost"))
                _raw_main_cost = main_cost   # pre-subtraction, to keep an Agile credit's sign
                # Cost: subtract sub-meter costs (no cost_remainder stored per-block)
                for meter_name, meter in meters.items():
                    if (meter or {}).get("meta", {}).get("sub_meter"):
                        sub = ((meter.get("channels", {}) or {}).get("import", {}) or {})
                        main_cost -= _f(sub.get("cost"))
                # Floor only the sub-subtraction artifact (raw >= 0); a genuinely negative
                # Agile plunge-price cost (a credit) must survive, not clamp to 0.
                main_cost = main_cost if _raw_main_cost < 0 else max(main_cost, 0.0)
            else:
                main_kwh  = _f(main_import.get("kwh_total", main_import.get("kwh")))
                main_cost = _f(main_import.get("cost"))
                _raw_main_cost = main_cost   # pre-subtraction, to keep an Agile credit's sign
                for meter_name, meter in meters.items():
                    if (meter or {}).get("meta", {}).get("sub_meter"):
                        sub = ((meter.get("channels", {}) or {}).get("import", {}) or {})
                        main_kwh  -= _f(sub.get("kwh_grid", sub.get("kwh")))
                        main_cost -= _f(sub.get("cost"))
                main_kwh  = max(main_kwh, 0.0)
                # Floor only the sub-subtraction artifact (raw >= 0); a genuine Agile
                # plunge-price credit (negative cost) must survive, not clamp to 0.
                main_cost = main_cost if _raw_main_cost < 0 else max(main_cost, 0.0)
            main_rate = _f(main_import.get("rate_used", main_import.get("rate")))
            _seg_split = _day_segment_split(main_import, meters)  # BL-27: applied after exc

            # Per-slot ex-VAT ratio for the opt-in data-table Cost (exc) column.
            if bill_rounding:
                _exc_r = main_import.get("rate_exc")
                _ratio = None
                if _exc_r is not None and main_rate:
                    _ratio = _f(_exc_r) / main_rate
                else:
                    _ce, _ci = main_import.get("cost_exc"), main_import.get("cost")
                    if _ce is not None and _ci not in (None, 0):
                        _ratio = _f(_ce) / _f(_ci)
                if _ratio is None:
                    # No captured exc → fall back to the period's statutory VAT rate
                    # (from the calendar), NOT a hardcoded 1.05, so a 0%/20% period is right.
                    slot_exc_ratio[hh]  = round(1.0 / (1.0 + fallback_vat), 8)
                    slot_exc_approx[hh] = True
                else:
                    slot_exc_ratio[hh]  = round(_ratio, 8)

            if _seg_split is not None:                      # BL-27: house from house segments
                _hh = _seg_split["electricity_main"]
                main_kwh, main_cost = _hh["kwh"], _hh["cost"]
                main_rate = _hh["rate"] or main_rate

            meter_kwh["electricity_main"][hh]  = main_kwh
            meter_rate["electricity_main"][hh] = main_rate
            meter_cost["electricity_main"][hh] = main_cost
            summary_kwh["electricity_main"]   += main_kwh
            summary_cost["electricity_main"]  += main_cost
            summary_rates["electricity_main"][round(main_rate, 4)] += main_kwh
            if "electricity_main" not in meter_display_name:
                meter_display_name["electricity_main"] = "Direct import"

            # Total import: use kwh_total from engine if available (authoritative),
            # otherwise sum remainder + sub-meter grid portions only.
            ti_kwh_has_total = "kwh_total" in main_import
            ti_kwh  = _f(main_import.get("kwh_total")) if ti_kwh_has_total else main_kwh
            ti_cost = main_cost  # remainder cost; sub-meter costs added below

            for meter_name, meter in meters.items():
                if (meter or {}).get("meta", {}).get("sub_meter"):
                    sub      = ((meter.get("channels", {}) or {}).get("import", {}) or {})
                    sub_kwh      = _f(sub.get("kwh"))
                    sub_kwh_grid = _f(sub.get("kwh_grid", sub_kwh))  # grid-attributed portion
                    sub_cost = _f(sub.get("cost"))
                    sub_rate = _f(sub.get("rate"))
                    if _seg_split is not None and meter_name in _seg_split:
                        _sv = _seg_split[meter_name]     # BL-27: EV on dispatch bands / house dev on house band
                        sub_kwh_grid = _sv["kwh"]
                        sub_cost = _sv["cost"]
                        sub_rate = _sv["rate"]
                    # H6b: the EV line shows the SYNTHETIC (dispatch) kWh wherever a dispatch
                    # segment exists — matching Usage Stats / the bill — with the metered-vs-
                    # dispatch difference pushed into the house remainder so the grid total is
                    # unchanged. No dispatch segment (pre-seam) → the recorded metered value
                    # stands. Display-only; the day total and the bill stay byte-identical.
                    if ev_fold_meter and meter_name == ev_fold_meter:
                        _segs = main_import.get("segments") or []
                        _syn_k = sum(_f(x.get("kwh")) for x in _segs if x.get("attribution") == "ev")
                        if _syn_k > 1e-9:
                            _syn_c = sum(_f(x.get("kwh")) * _f(x.get("inc_rate")) for x in _segs
                                         if x.get("attribution") == "ev")
                            # H2-fix: don't push Direct (house) below zero on a battery-assist slot.
                            # The dispatch-derived synthetic EV can exceed the grid actually available
                            # to the car (the rest of its charge came from the battery, invisible to
                            # dispatch); cap it to the grid available (its own grid draw + the current
                            # house remainder) so the house never goes negative. Total preserved.
                            _avail_ev = _f(meter_kwh["electricity_main"][hh]) + sub_kwh_grid
                            if _syn_k > _avail_ev:
                                # B5: EV claims the whole grid slot (its own draw + all house
                                # remainder), so its COST must be the whole grid cost — not the
                                # car-rate-scaled slice — else Direct (house) cost doesn't floor
                                # with its kWh and renders a spurious negative £ bar.
                                _syn_c = sub_cost + _f(meter_cost["electricity_main"][hh])
                                _syn_k = _avail_ev
                            _d_k = sub_kwh_grid - _syn_k
                            _d_c = sub_cost - _syn_c
                            meter_kwh["electricity_main"][hh]  += _d_k
                            meter_cost["electricity_main"][hh] += _d_c
                            summary_kwh["electricity_main"]    += _d_k
                            summary_cost["electricity_main"]   += _d_c
                            summary_rates["electricity_main"][round(main_rate, 4)] += _d_k
                            sub_kwh_grid = _syn_k
                            sub_cost     = _syn_c
                            sub_rate     = (_syn_c / _syn_k) if _syn_k > 1e-9 else sub_rate
                    meter_kwh[meter_name][hh]  = sub_kwh_grid  # grid-attributed, matches usage stats
                    meter_rate[meter_name][hh] = sub_rate
                    meter_cost[meter_name][hh] = sub_cost
                    summary_kwh[meter_name]   += sub_kwh_grid
                    summary_cost[meter_name]  += sub_cost
                    summary_rates[meter_name][round(sub_rate, 4)] += sub_kwh_grid
                    if meter_name not in meter_display_name:
                        meta = (meter or {}).get("meta", {}) or {}
                        label = meta.get("device") or meter_name.replace("_", " ").title()
                        meter_display_name[meter_name] = label
                    if not ti_kwh_has_total:
                        ti_kwh += sub_kwh_grid
                    ti_cost += sub_cost

            slot_ti_kwh[hh]  = ti_kwh
            slot_ti_cost[hh] = ti_cost
            slot_ti_rate[hh] = main_rate

            # ── Derived-EV split (display-only; see _dispatch_ev_slot_map) ──────
            # Re-partition the house slot into house + EV AFTER the total-import (ti)
            # figures are set, so the grid total is untouched — this only subdivides
            # the 'Direct import' segment. No-op unless an ev_slot_map is supplied
            # (gated to no-sub-meter accounts upstream).
            if ev_slot_map:
                _ev = ev_slot_map.get(block.get("start"))
                _mk = meter_kwh["electricity_main"][hh]
                if _ev and _mk > 0:
                    _ek = min(_ev["kwh"], _mk)
                    if _ek > 1e-9:
                        _mc = meter_cost["electricity_main"][hh]
                        # BL-9: EV cost from the (bill-authoritative) ev_slot_map cost,
                        # clipped consistently with _ek, so the EV bar, rate line and cost
                        # all match the bill on a capped account; house = main − EV keeps
                        # the grid total untouched. Uncapped → ev_slot_map cost == the
                        # pro-rata carve, so byte-identical. Fall back to pro-rata if the
                        # map somehow lacks kWh.
                        _evk_map = _f(_ev.get("kwh"))
                        _ec = (_f(_ev.get("cost")) * (_ek / _evk_map)
                               if _evk_map > 1e-9 else _mc * (_ek / _mk))
                        _r4 = round(main_rate, 4)
                        # The EV rate LINE follows the stored dispatch EV rate
                        # (imp_rate_ev) so it DIVERGES from the house line once the 6-h
                        # cap pushes EV charging to peak (the 4-rate rule). On an uncapped
                        # IOG account imp_rate_ev == the house rate, so the line sits
                        # exactly on the house line (no visible change). Energy/cost stay
                        # on the validated synthetic split (grid total untouched); only
                        # the rate the EV line is plotted at, and its summary rate bucket,
                        # change. Absent (non-IOG / no split) → falls back to main_rate.
                        # BL-27: the EV rate LINE follows the EV-attributed SEGMENT rate
                        # (full precision, == the old imp_rate_ev); the column is a dead
                        # fallback for any un-backfilled block, so the re-stamp can retire.
                        _segsr = main_import.get("segments")
                        _segr_k = (sum(_f(x.get("kwh")) for x in _segsr
                                       if x.get("attribution") == "ev") if _segsr else 0.0)
                        if _segr_k > 1e-9:
                            _evr = (sum(_f(x.get("kwh")) * _f(x.get("inc_rate")) for x in _segsr
                                        if x.get("attribution") == "ev") / _segr_k) or main_rate
                        else:
                            _evr = _f(main_import.get("rate_ev")) or main_rate
                        _r4e = round(_evr, 4)
                        meter_kwh["electricity_main"][hh]  = _mk - _ek
                        meter_cost["electricity_main"][hh] = _mc - _ec
                        meter_kwh["ev_dispatch"][hh]  = _ek
                        meter_cost["ev_dispatch"][hh] = _ec
                        meter_rate["ev_dispatch"][hh] = _evr
                        summary_kwh["electricity_main"]  -= _ek
                        summary_cost["electricity_main"] -= _ec
                        summary_kwh["ev_dispatch"]  += _ek
                        summary_cost["ev_dispatch"] += _ec
                        summary_rates["electricity_main"][_r4] -= _ek
                        summary_rates["ev_dispatch"][_r4e]     += _ek
                        meter_display_name.setdefault("ev_dispatch", "EV (from dispatch)")

            if main_export:
                exp_kwh  = abs(_f(main_export.get("kwh")))
                exp_cost = abs(_f(main_export.get("cost")))
                exp_rate = abs(_f(main_export.get("rate")))
                exp_name = "electricity_main_export"
                meter_kwh[exp_name][hh]  = -exp_kwh
                meter_rate[exp_name][hh] = exp_rate
                meter_cost[exp_name][hh] = exp_cost
                summary_kwh[exp_name]   += exp_kwh
                summary_cost[exp_name]  += exp_cost
                summary_rates[exp_name][round(exp_rate, 4)] += exp_kwh

        except Exception:
            pass

    # ── BL-27: the rate LINES come from the day-level emit (chart_emit.day_rate_series) —
    # ONE tested place for "what rate applies each half-hour"; the chart only plots it. See
    # the presentation read-contract in docs/design/4.4.0_iog_pricing_and_reprice_design.md.
    import chart_emit
    # Cap gating: the 6-h-cap held-peak / noon-reset rules apply only to days within the
    # capped IOG-SMB era (cap_from = the SMB agreement's start). Pre-cap days follow uncapped
    # TOU -- EV shows its charged rate where charging, else tracks the house line.
    _capped_day = bool(cap_from) and str(day) >= str(cap_from)
    # Authoritative per-slot house TOU from the tariff schedule, so the house rate line never
    # trusts an unreliable stored rate on a 0-kWh / seam-mis-priced block. resolve() is the
    # base tariff (dispatch overlays don't affect it); schedule is pence -> £.
    _house_tou = None
    if import_schedule is not None:
        _house_tou = [None] * slots
        # Resolve the tariff TOU for EVERY slot (not just slots with a block), so the house
        # rate line follows the day/night TOU across idle / no-usage slots instead of clinging
        # to a stale stored rate. Derive each slot's naive-UTC timestamp from the hh<->UTC
        # mapping the day's blocks give us (tz-agnostic; blocks are consecutive half-hours).
        from datetime import datetime as _dt_ht, timedelta as _td_ht
        _base_ht = None
        for _hh, _b in day_blocks:
            _bs = (_b or {}).get("start")
            if _bs:
                try:
                    _base_ht = _dt_ht.fromisoformat(_bs) - _td_ht(minutes=_hh * block_minutes)
                    break
                except Exception:
                    pass
        if _base_ht is not None:
            for _hh in range(slots):
                try:
                    _r = import_schedule.resolve(
                        (_base_ht + _td_ht(minutes=_hh * block_minutes)).isoformat())
                    if _r is not None:
                        _house_tou[_hh] = _r / 100.0
                except Exception:
                    pass
    # Authoritative held-peak signal for the EV line: over_cap[hh] True where the 6-hour
    # cap is EXCEEDED at this slot (at/after its noon->noon cap-day boundary). The boundary
    # comes from store.cap_day_boundary, which EXCLUDES bump/boost energy — so a bump or an
    # out-of-dispatch peak charge never latches the line (4-rate rules); only a genuine
    # over-cap smart-charge does. None on a pre-cap day / no store (chart_emit → no hold).
    _over_cap = None
    if _capped_day and store is not None:
        try:
            import iog_cap as _iogc
            from datetime import datetime as _dt_oc, timedelta as _td_oc
            _base_oc = None
            for _hh, _b in day_blocks:
                _bs = (_b or {}).get("start")
                if _bs:
                    try:
                        _base_oc = _dt_oc.fromisoformat(_bs) - _td_oc(minutes=_hh * block_minutes)
                        break
                    except Exception:
                        pass
            if _base_oc is not None:
                _over_cap = [False] * slots
                _bcache = {}
                for _hh in range(slots):
                    _ts = (_base_oc + _td_oc(minutes=_hh * block_minutes)).isoformat()
                    _k = _iogc.cap_day_key(_ts, timezone_name)
                    if _k not in _bcache:
                        _bcache[_k] = store.cap_day_boundary(_ts, timezone_name)
                    # < 1.0: boundary slot (blend) and every slot after it in the cap-day.
                    if _iogc._within_cap_frac(_ts, _bcache[_k]) < 1.0:
                        _over_cap[_hh] = True
        except Exception:
            _over_cap = None
    _series = chart_emit.day_rate_series(day_blocks, slots=slots, block_minutes=block_minutes, capped=_capped_day, house_tou=_house_tou, ev_slot_rate=meter_rate.get("ev_dispatch"), over_cap=_over_cap)
    _ev_fb = _series.get("ev_fallback")
    for _mid in list(meter_rate.keys()):
        if _mid.endswith("_export"):
            continue
        _is_ev = (_mid == "ev_dispatch" or "charger" in _mid.lower()
                  or _mid.lower().startswith("ev_") or _mid.lower().startswith("ev "))
        _curve = _series["ev"] if _is_ev else _series["house"]
        for hh in range(slots):
            if _curve[hh] is None:
                continue
            # BL-27 hardening: never let day_rate_series' off/held FALLBACK guess override a
            # real dispatch-derived per-slot EV rate. A slot with EV energy but no stored EV
            # segment (late-attributed / manually-corrected block) already carries the bar's
            # rate in meter_rate here; keep the line on it so line and bar agree.
            _keep = (_is_ev and _ev_fb is not None and _ev_fb[hh]
                     and meter_rate[_mid][hh])
            # 4.5.7: on a capped (IOG-SMB) day, do NOT preserve a physical EV device's own
            # stored rate on an IDLE (0-kWh) slot — that value is the stale plain-TOU rate the
            # sub-meter carries where the car isn't charging, and it's the ONLY EV surface not
            # already on the synthetic/hybrid path (bars, Usage Stats, Insights, bill all are).
            # Let the slot fall through to the synthetic curve (off-peak baseline within cap,
            # held-peak on a genuine over_cap break, noon reset), matching every other surface.
            # Charging slots (real EV kWh) still keep the bar's rate, so line and bar agree.
            if _keep and _capped_day and meter_kwh.get(_mid, [0.0] * slots)[hh] <= 1e-9:
                _keep = False
            if _keep:
                continue
            meter_rate[_mid][hh] = _curve[hh]

    # ── x axis labels — outside the loop ──
    total_hh_kwh = [sum(meter_kwh[m][i] for m in meter_kwh if not m.endswith('_export')) for i in range(slots)]
    x_labels = []
    x_ranges = []
    for i in range(slots):
        minutes_start = i * block_minutes
        h_s, m_s = divmod(minutes_start, 60)
        minutes_end = minutes_start + block_minutes
        h_e, m_e = divmod(minutes_end % 1440, 60)
        x_labels.append(f"{h_s:02d}:{m_s:02d}")
        x_ranges.append(f"{h_s:02d}:{m_s:02d} - {h_e:02d}:{m_e:02d}")

    # ── Summary panel ──
    # H6b: one continuous 'EV' identity on a has-EV-meter account (matches Usage Stats).
    if ev_fold_meter and ev_label and ev_fold_meter in meter_display_name:
        meter_display_name[ev_fold_meter] = ev_label
    sub_meter_names = [k for k in summary_kwh if k not in ("electricity_main", "electricity_main_export")]

    total_import      = sum(v for k, v in summary_kwh.items()  if not k.endswith("_export") and v > 0)
    total_import_cost = sum(v for k, v in summary_cost.items() if not k.endswith("_export") and summary_kwh.get(k, 0) > 0)

    house_kwh  = summary_kwh.get("electricity_main", 0.0)
    exp_kwh    = summary_kwh.get("electricity_main_export", 0.0)
    exp_cost   = summary_cost.get("electricity_main_export", 0.0)

    main_color   = meter_colors.get("electricity_main", "#1f77b4")
    export_color = meter_colors.get("electricity_main_export", "#ff7f0e")

    def _rates(meter_key):
        return [[kwh, rate, n] for kwh, rate, n in _collapse_rate_kwh(summary_rates[meter_key])]

    # ── Per-meter totals rounded to display precision ──────────────────────
    # Sum per-slot values at display precision (3dp kWh, 4dp cost) so the
    # sidebar, data table totals row, and any future views all use the same
    # single rounding path.
    meter_totals = {}
    for meter in meter_kwh:
        kwh_sum  = sum(abs(v) for v in meter_kwh[meter])
        # Import cost is summed SIGNED so an Agile plunge-price credit (negative
        # cost) reduces the total instead of being flipped positive; export is kept
        # as a magnitude (its sign is applied at display). No-op on positive days.
        _is_exp  = str(meter).lower().endswith("export")
        cost_sum = (sum(abs(v) for v in meter_cost[meter]) if _is_exp
                    else sum(meter_cost[meter]))
        meter_totals[meter] = {
            "kwh":  round(kwh_sum, 3),
            "cost": round(cost_sum, 4),
        }

    house_cost = meter_totals.get("electricity_main", {}).get("cost", 0.0)
    ti_total = {
        "kwh":  round(sum(meter_totals[m]["kwh"]  for m in meter_totals if not m.endswith("_export")), 3),
        "cost": round(sum(meter_totals[m]["cost"] for m in meter_totals if not m.endswith("_export")), 4),
    }
    # BL-24 (opt-in): the ex-VAT import total for the side panel, from the same per-slot
    # exc ratio the data table uses (so the two agree). Only when the setting is on.
    _import_exc_display = None
    if bill_rounding:
        _inv = 1.0 / (1.0 + fallback_vat)
        _import_exc_display = round(sum(
            slot_ti_cost[_h] * (slot_exc_ratio[_h] if slot_exc_ratio[_h] is not None else _inv)
            for _h in range(slots)), 2)

    # ── Side panel, as data ──
    # The breakdown lists the house remainder then each sub-meter, each only above
    # 0.0001 kWh, and only when the day has sub-meters at all. Colours travel with it:
    # rate rows are drawn in the meter colour darkened by adjust_color(…, 0.75).
    breakdown = []
    if sub_meter_names:
        if house_kwh > 0.0001:
            breakdown.append({"meter": "electricity_main",
                              "label": meter_display_name.get("electricity_main", "Direct import"),
                              "kwh": house_kwh, "cost": house_cost, "color": main_color,
                              "rate_color": adjust_color(main_color, 0.75),
                              "rates": _rates("electricity_main")})
        for meter_name in sub_meter_names:
            sub_kwh  = summary_kwh.get(meter_name, 0.0)
            sub_cost = meter_totals.get(meter_name, {}).get("cost", 0.0)
            if sub_kwh > 0.0001:
                sub_color = meter_colors.get(meter_name, "#e377c2")
                breakdown.append({"meter": meter_name,
                                  "label": meter_display_name.get(meter_name, meter_name.replace("_", " ").title()),
                                  "kwh": sub_kwh, "cost": sub_cost, "color": sub_color,
                                  "rate_color": adjust_color(sub_color, 0.75),
                                  "rates": _rates(meter_name)})
    panel = {
        "total_import":    total_import,
        "import_cost":     ti_total["cost"],
        "import_cost_exc": _import_exc_display,
        "export_kwh":      exp_kwh,
        "export_cost":     exp_cost,
        "export_rates":    _rates("electricity_main_export") if exp_kwh > 0 else [],
        "main_color":      main_color,
        "export_color":    export_color,
        "breakdown":       breakdown,
    }

    # ── Chart series ──
    meters_data = {}
    for meter in sorted(meter_kwh.keys()):
        bar_color  = meter_colors.get(meter, "#333333")
        line_color = adjust_color(bar_color, 0.8)
        if meter == "electricity_main_export":
            nice_name = meter_display_name.get(meter, "Grid Export")
        else:
            nice_name = meter_display_name.get(
                meter,
                meter.replace("_", " ")
                     .replace("electricity main", "Direct import")
                     .replace("export", "Grid Export")
                     .title()
            )
        raw_rates = meter_rate[meter]
        last_nonzero = max((i for i, v in enumerate(raw_rates) if v != 0.0), default=None)
        if last_nonzero is not None:
            truncated_rates = raw_rates[:last_nonzero + 1] + [raw_rates[last_nonzero]]
            trunc_x_line    = [i - 0.5 for i in range(last_nonzero + 2)]
        else:
            truncated_rates = raw_rates + [raw_rates[-1]]
            trunc_x_line    = [i - 0.5 for i in range(slots + 1)]
        meters_data[meter] = {
            "y":            meter_kwh[meter],
            "cost":         meter_cost[meter],
            "rate":         truncated_rates,
            "rate_x":       trunc_x_line,
            "has_rate":     any(v != 0.0 for v in raw_rates),
            "bar_color":    bar_color,
            "line_color":   line_color,
            "nice_name":    nice_name,
            "is_export":    meter.endswith("_export"),
        }

    chart_data = {
        "x_labels":      x_labels,
        "x_ranges":      x_ranges,
        "slots":         slots,
        "block_minutes": block_minutes,
        "currency":      currency,
        "meters":        meters_data,
        "ti_kwh":        slot_ti_kwh,
        "ti_cost":       slot_ti_cost,
        "ti_rate":       slot_ti_rate,
        "meter_totals":  meter_totals,
        "ti_total":      ti_total,
    }
    # Opt-in only — keep the default JSON byte-identical for users who haven't
    # enabled Bill Rounding.
    if bill_rounding:
        chart_data["bill_rounding"]  = True
        chart_data["exc_ratio"]      = slot_exc_ratio
        chart_data["exc_approx"]     = slot_exc_approx
        chart_data["fallback_invat"] = round(1.0 / (1.0 + fallback_vat), 8)
    return {"chart": chart_data, "panel": panel}


# ─────────────────────────────────────────────────────────────
# Billing context and period summary — shared by the pre-render and /api/energy
# ─────────────────────────────────────────────────────────────

def group_blocks_by_day(blocks, tz, block_minutes):
    """{local day ISO: [(slot index, block), …]} — how the Billing view groups blocks.

    The slot is the local clock position, so the repeated hour of a clocks-back day
    lands on slots it already used. day_chart_data ASSIGNS per-slot series (the bars and
    `meter_totals` keep the later block) but ACCUMULATES `summary_kwh` (the side panel
    counts both) — so on that one day a year the two disagree. Inherited from v4;
    kept as-is here, where the pre-render and the API must stay identical."""
    days_map = defaultdict(list)
    for block in blocks:
        try:
            if not block or not block.get("start"):
                continue
            start = _parse_block_start(block["start"], tz)
            day   = start.date().isoformat()
            hh    = (start.hour * 60 + start.minute) // block_minutes
            days_map[day].append((hh, block))
        except Exception:
            pass
    return days_map


def billing_context(cfg, store, blocks, has_exc=None):
    """What every period summary and day chart of one Billing view shares.

    Split out of generate_daily_import_export_charts (C, 30 Sep 2026) so
    /api/energy?include=billing builds the same thing from a period's blocks:

      * the EV identity — a has-EV-meter account gets one continuous 'EV' line
        (hybrid: synthetic post-seam, recorded physical pre-seam, H6); a no-EV-meter
        account the display-only dispatch split 'EV (from dispatch)' (BL-22);
      * the per-slot EV map for that. Every rule in it is per SLOT, so building it from
        one period's blocks gives the same entries as building it from all of history;
      * meter colours (EV dispatch purple when the map has entries);
      * BL-24 bill rounding: the opt-in setting, gated on EMT holding a captured ex-VAT
        figure ANYWHERE — not merely in these blocks. `has_exc` lets a caller holding
        one period answer that for the whole store (the API asks SQL); omitted, it is
        read from `blocks`, which is all of history for the pre-render;
      * the learned VAT calendar (vat_at).
    """
    meter_colors = build_meter_colors_from_config(cfg) if cfg else build_meter_colors(blocks)

    # Derived-EV split (display-only; BL-22): no-op unless this is a no-sub-meter
    # account with completed dispatches. Gives the Billing tab an "EV (from dispatch)"
    # breakdown line + per-day trace without moving any bill total.
    # Hybrid EV for the billing breakdown (H6): a has-EV-meter account gets one continuous
    # 'EV' line (synthetic post-seam, recorded physical pre-seam) via the fold-back; a no-EV-
    # meter account keeps the display-only dispatch split labelled 'EV (from dispatch)'.
    _ev_phys_id = _ev_meter_id(cfg)
    if _ev_phys_id:
        _ev_slot_map = _hybrid_ev_slot_map(store, blocks, cfg)
        _ev_fold_devices = [((cfg.get("meters", {}).get(_ev_phys_id, {}).get("meta") or {}).get("device")
                             or _ev_phys_id)]
        _ev_label = "EV"
    else:
        _ev_slot_map = _dispatch_ev_slot_map(store, blocks, cfg)
        _ev_fold_devices = None
        _ev_label = "EV (from dispatch)"
    # Day tables: has-EV-meter accounts fold the metered EV line to the SYNTHETIC segment
    # value in-place (ev_fold_meter) rather than carving a separate dispatch line, so the EV
    # bar matches Usage Stats. No-EV-meter accounts keep the dispatch carve. Grid totals hold.
    _ev_day_map = None if _ev_phys_id else _ev_slot_map
    if _ev_slot_map:
        meter_colors["ev_dispatch"] = "#8b5cf6"
    # H6c: the EV line keeps its DEVICE-INDEX palette colour (the colour the physical meter
    # had before), synthetic or recorded — not the synthetic purple — so it stays distinct
    # from the blue house line (accessibility — see roadmap BL-29).

    # BL-24 (opt-in): show the bill summary with Octopus's rounding method. Read once;
    # default off ⇒ nothing injected ⇒ the summary is byte-identical to today.
    _bill_rounding = False
    _vat_learned = []
    try:
        if store is not None:
            _bill_rounding = bool((store.get_settings() or {}).get("bill_rounding_summary"))
            _vat_learned = store.get_vat_calendar()
    except Exception:
        _bill_rounding = False
    # Gate on capability: ex-VAT is only meaningful where EMT can capture a real exc
    # figure (API/tariff). A CAD/cost-sensor-only user has no exc source at all, so we
    # never show it — the fallback would only ever be inc ÷ (1+VAT), a bare assumption.
    # Proxy "has API" by "any captured exc present in the data".
    if _bill_rounding:
        if has_exc is None:
            has_exc = any(
                ((((b or {}).get("meters") or {}).get("electricity_main") or {})
                 .get("channels", {}).get("import", {}) or {}).get("cost_exc") is not None
                for b in (blocks or []))
        if not has_exc:
            _bill_rounding = False

    return {"meter_colors": meter_colors, "ev_phys_id": _ev_phys_id,
            "ev_slot_map": _ev_slot_map, "ev_fold_devices": _ev_fold_devices,
            "ev_label": _ev_label, "ev_day_map": _ev_day_map,
            "bill_rounding": _bill_rounding, "vat_learned": _vat_learned}


def vat_at(ctx, when):
    """Statutory VAT rate at a date from the seed + learned calendar — the fallback
    rate used where no per-slot inc/exc pair exists (replaces the hardcoded 1.05)."""
    import vat_calendar as _vc
    return _vc.resolve_vat(when, ctx["vat_learned"])


def period_summary(ctx, blocks_for_calc, period_blocks, period_start, period_end, *, store, tz_name):
    """calculate_billing_summary_for_period + the display-only derived-EV split, and
    (opt-in) the bill-method figure. Both are additive: the bill anchors / Total Bill
    stay byte-identical (see _inject_ev_breakdown_into_summary / _bill_method_summary).

    `blocks_for_calc` may be wider than the period (the summary filters by local time);
    `period_blocks` must be the period's own blocks — the EV split and the bill method
    sum over them unfiltered."""
    _sm = calculate_billing_summary_for_period(
        blocks_for_calc, period_start, period_end, store=store, tz_name=tz_name)
    if ctx["ev_slot_map"]:
        _inject_ev_breakdown_into_summary(_sm, period_blocks, ctx["ev_slot_map"],
                                          label=ctx["ev_label"], fold_devices=ctx["ev_fold_devices"])
    if ctx["bill_rounding"]:
        import vat_calendar as _vcal
        # `vat_at(ctx, w)` per slot, with the calendar merged once (#48, 2 Oct 2026).
        _bm = _bill_method_breakdown(period_blocks, period_vat=vat_at(ctx, period_start),
                                     standing_inc_by_day=_sm.get("standing"),
                                     vat_at=_vcal.rate_lookup(ctx["vat_learned"]))
        if _bm:
            _sm["bill_method"] = _bm
    return _sm
