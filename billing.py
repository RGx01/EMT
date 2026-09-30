"""
billing.py — the billing arithmetic, extracted from energy_charts.py.

Deconstruction step 2 (EMT-v5_architecture §3): *rescue, then demolish.*
`energy_charts.py` is 79% presentation and dies; these ~900 lines are the domain
logic every surface has to agree on, and they are what "one API for every
surface" is built from.

**This is a MOVE, not an untangling.** §3 originally called
`calculate_billing_summary_for_period` the crux of the workstream, on the grounds
that the arithmetic and its markup were the same 377 lines. Measured, they were
already separate: this function emits no HTML and returns a dict, and the markup
half — `render_billing_summary` — is a different function that consumes that dict.
So every function here arrived unchanged, and the diff should be readable as a
relocation. Corrected in §3 on 24 Sep 2026.

(That §3 note also claimed `render_billing_summary` had no caller and was already
dead. It has six, all inside `energy_charts.py`; it dies with the chart generators
at step 4 rather than separately. The claim came from a grep that excluded the
defining file and therefore its callers too — corrected the same day.)

**Depends on stdlib only.** No `carbon`, no `os`, no `json`, and critically no
`block_store`: `calculate_billing_summary_for_period` takes a `store` as an
ARGUMENT and calls methods on it. That keeps this module a leaf — the domain does
not reach for storage, storage is handed to it — which is the property that lets
the API layer above it be the only thing that knows where data comes from.

**Nothing imports this yet except `energy_charts.py`**, which re-exports these
names so existing callers keep working through the demolition. When the chart
module goes, the callers move here; until then a caller can use either and get
the same function object.
"""
import re
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal as _Decimal, ROUND_HALF_EVEN as _ROUND_HALF_EVEN
# Module level, not per-function. The moved functions each imported this locally,
# which is why the three rewritten wrappers below raised NameError the moment they
# stopped carrying their own copy — caught by tests/test_periods_endpoint.py.
from zoneinfo import ZoneInfo


def _utc_to_local(dt_naive: datetime, tz: "ZoneInfo") -> datetime:
    """Attach UTC, then convert to local timezone."""
    return dt_naive.replace(tzinfo=timezone.utc).astimezone(tz)


def _parse_block_start(iso_str: str, tz: "ZoneInfo") -> datetime:
    """Parse a UTC ISO block-start string and return it in local time."""
    return _utc_to_local(datetime.fromisoformat(iso_str), tz)


def calendar_periods(kind, first, last):
    """Calendar period boundaries of `kind` spanning [first, last].

    `first` / `last` are local-naive datetimes; returns [(start, end), ...] with
    end exclusive. Pure arithmetic — no blocks, no store, no timezone lookup.

      month    Jan 1 -> Feb 1, Feb 1 -> Mar 1, ...
      quarter  Q1 = Jan-Apr, Q2 = Apr-Jul, Q3 = Jul-Oct, Q4 = Oct-Jan
      year     Jan 1 -> Jan 1

    One month-arithmetic implementation instead of three. Each of the wrappers
    below carried its own `if nm > 12` year rollover.
    """
    if first is None or last is None:
        return []
    if kind == "month":
        step, cur = 1, first.replace(day=1, hour=0, minute=0, second=0,
                                     microsecond=0, tzinfo=None)
    elif kind == "quarter":
        step = 3
        qm = ((first.month - 1) // 3) * 3 + 1
        cur = first.replace(month=qm, day=1, hour=0, minute=0, second=0,
                            microsecond=0, tzinfo=None)
    elif kind == "year":
        step, cur = 12, first.replace(month=1, day=1, hour=0, minute=0, second=0,
                                      microsecond=0, tzinfo=None)
    else:
        raise ValueError("calendar_periods: kind must be month|quarter|year, got %r" % kind)
    periods = []
    while True:
        nm = cur.month + step
        ny = cur.year + (nm - 1) // 12
        nm = (nm - 1) % 12 + 1
        nxt = cur.replace(year=ny, month=nm, day=1)
        periods.append((cur, nxt))
        if nxt > last:
            break
        cur = nxt
    return periods


def get_billing_periods_from_config_periods(config_periods, tz=None):
    """
    Fast alternative to get_billing_periods_from_config_history that takes
    config_periods rows (from store.get_config_periods()) instead of all blocks.

    Avoids loading every block from the database — only needs the config period
    metadata and the first/last block_start per period.

    Returns the same list of (start_datetime, end_datetime) tuples as
    get_billing_periods_from_config_history.
    """
    if not config_periods:
        return []

    _tz = tz or ZoneInfo("UTC")

    # Build synthetic block-like segments from config_period rows
    # Each segment: (billing_day, ef_date, seg_last_naive)
    segments = []
    for p in config_periods:
        if not p.get("effective_from"):
            continue
        billing_day = int(p.get("billing_day") or 1)

        # Parse effective_from UTC → local date
        from datetime import datetime as _dt2, timezone as _tz2
        try:
            ef_iso = p["effective_from"].replace(" ", "T").split(".")[0]
            ef_utc = _dt2.fromisoformat(ef_iso).replace(tzinfo=_tz2.utc)
            ef_local_date = ef_utc.astimezone(_tz).date()
        except Exception:
            continue

        # seg_last: last block date in this period (local), or ef_date if no blocks
        last_bs = p.get("last_block_start")
        if last_bs:
            try:
                last_utc = _dt2.fromisoformat(
                    str(last_bs).replace(" ", "T").split(".")[0]
                ).replace(tzinfo=_tz2.utc)
                last_local = last_utc.astimezone(_tz).date()
            except Exception:
                last_local = ef_local_date
        else:
            last_local = ef_local_date

        segments.append((billing_day, ef_local_date, last_local))

    if not segments:
        return []

    # Use the same period-generation logic as get_billing_periods_from_config_history
    # Build synthetic blocks with _effective_from/_billing_day/_timezone set,
    # then delegate to the existing function.
    # Simpler: reconstruct the period list directly using the same algorithm.

    import calendar as _cal
    from datetime import datetime as _dt3, timedelta as _td

    def _date_to_naive(d):
        return _dt3(d.year, d.month, d.day, 0, 0, 0)

    def _period_end_from_start(p_start, billing_day):
        m = p_start.month + 1
        y = p_start.year + (1 if m > 12 else 0)
        m = m - 12 if m > 12 else m
        try:
            return p_start.replace(year=y, month=m, day=billing_day,
                                   hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        except ValueError:
            last_day = _cal.monthrange(y, m)[1]
            return p_start.replace(year=y, month=m, day=last_day,
                                   hour=0, minute=0, second=0, microsecond=0, tzinfo=None)

    def _transition_date(ef_local_date, new_bd):
        if ef_local_date.day < new_bd:
            try:
                return ef_local_date.replace(day=new_bd)
            except ValueError:
                import calendar
                last = calendar.monthrange(ef_local_date.year, ef_local_date.month)[1]
                return ef_local_date.replace(day=last)
        else:
            m = ef_local_date.month + 1
            y = ef_local_date.year + (1 if m > 12 else 0)
            m = m - 12 if m > 12 else m
            try:
                return ef_local_date.replace(year=y, month=m, day=new_bd)
            except ValueError:
                import calendar
                last = calendar.monthrange(y, m)[1]
                return ef_local_date.replace(year=y, month=m, day=last)

    # Compute transitions between segments
    transitions = []
    for i in range(len(segments) - 1):
        next_bd, next_ef, _ = segments[i + 1]
        transitions.append(_transition_date(next_ef, next_bd))

    # first_block_date: first block across all periods
    first_block_date = None
    for p in config_periods:
        fb = p.get("first_block_start")
        if fb:
            try:
                from datetime import datetime as _dt4, timezone as _tz3
                fb_utc = _dt4.fromisoformat(
                    str(fb).replace(" ", "T").split(".")[0]
                ).replace(tzinfo=_tz3.utc)
                fb_local = fb_utc.astimezone(_tz).date()
                if first_block_date is None or fb_local < first_block_date:
                    first_block_date = fb_local
            except Exception:
                pass

    if first_block_date is None:
        return []

    periods = []

    for seg_idx, (billing_day, ef_date, seg_last) in enumerate(segments):
        seg_end = _date_to_naive(transitions[seg_idx]) if seg_idx < len(transitions) else None
        seg_last_dt = _date_to_naive(seg_last) + _td(days=1)

        if seg_idx == 0:
            ref = first_block_date
            m, y = ref.month, ref.year
            if ref.day < billing_day:
                m -= 1
                if m == 0: m = 12; y -= 1
            try:
                period_start = _date_to_naive(ref.replace(year=y, month=m, day=billing_day))
            except ValueError:
                period_start = _date_to_naive(ref.replace(
                    year=y, month=m, day=_cal.monthrange(y, m)[1]))
        else:
            period_start = _date_to_naive(transitions[seg_idx - 1])

        truncate_from = None
        if seg_end:
            p0 = period_start
            while True:
                pe0 = _period_end_from_start(p0, billing_day)
                if pe0 > seg_end - _td(days=1):
                    truncate_from = p0
                    break
                p0 = pe0

        while True:
            period_end = _period_end_from_start(period_start, billing_day)

            if seg_end and truncate_from is not None and period_start == truncate_from:
                period_end = seg_end
            elif seg_end and truncate_from is not None and period_end == truncate_from:
                next_ef_naive = _date_to_naive(segments[seg_idx + 1][1])
                if period_start < next_ef_naive <= truncate_from:
                    period_end = seg_end
            elif seg_end and period_end > seg_end:
                period_end = seg_end

            if period_end > period_start:
                entry = (period_start, period_end)
                if not periods or periods[-1] != entry:
                    periods.append(entry)

            if seg_end and period_end >= seg_end:
                break
            if period_end >= seg_last_dt:
                if seg_end and truncate_from is not None and period_end <= truncate_from:
                    truncated = (truncate_from, seg_end)
                    if not periods or periods[-1] != truncated:
                        periods.append(truncated)
                break

            period_start = period_end

    return periods


def bill_net_from_day_rollups(rows) -> float:
    """The bill's total over local-day rollups (`BlockStore.get_day_rollups`): per
    day, on the MAIN meter, round(round(imp,4) + round(standing,4) - round(exp,4), 2),
    summed. The same rule as `BlockStore.compute_period_net` — the Billing card —
    whose per-day penny rounding is what makes a bill's total; sub-meters attribute
    the main meter's supply and never add to it.

    ONE derivation for every surface (C §7.1, 30 Sep 2026). `_aggregate_usage`
    rounds once over the whole period instead, and against v4's Billing card on a
    real database it was up to 3p out in 49 of 80 periods; this was 0p in all 80
    (`.build/reconcile_v4.py`)."""
    total = 0.0
    for r in rows:
        if r.get("meter_id") != "electricity_main":
            continue
        total += round(round(r.get("imp_cost") or 0.0, 4)
                       + round(r.get("standing_charge") or 0.0, 4)
                       - round(r.get("exp_cost") or 0.0, 4), 2)
    return round(total, 2)

def calculate_billing_summary_for_period(blocks, period_start, period_end, store=None, tz_name=None):
    meter_summary  = defaultdict(lambda: defaultdict(lambda: {"kwh": 0.0, "cost": 0.0, "read_start": None, "read_end": None}))
    meter_totals   = defaultdict(lambda: {"kwh": 0.0, "cost": 0.0, "read_start": None, "read_end": None})
    main_import_raw = defaultdict(lambda: {"kwh": 0.0, "cost": 0.0})
    # BL-9: IOG house/EV split by rate band (inc + derived exc). ev_by_rate is only
    # populated on dispatched IOG blocks; home_by_rate carries the remainder of every
    # main-import block, so together they reconstruct the grid total. exc is derived
    # from each block's own exc/inc ratio (flat VAT → exact, even across a VAT change).
    ev_by_rate   = defaultdict(lambda: {"kwh": 0.0, "cost": 0.0, "cost_exc": 0.0})
    home_by_rate = defaultdict(lambda: {"kwh": 0.0, "cost": 0.0, "cost_exc": 0.0})
    # Boundary (cap-transition) blocks carry a mixed band and a per-day blended
    # rate; collapse them into ONE transition row each rather than a row per blend.
    ev_transition   = {"kwh": 0.0, "cost": 0.0, "cost_exc": 0.0}
    home_transition = {"kwh": 0.0, "cost": 0.0, "cost_exc": 0.0}
    main_export_raw = {"kwh": 0.0, "cost": 0.0}  # raw main meter export totals
    # Main-meter-only daily accumulators for total_cost.
    # Matches _fmt_total in server.py: raw main meter imp/exp/sc per day,
    # rounded to 4dp, net_cost = round(imp + sc - exp, 2) per day, summed.
    # Sub-meter per-block subtraction is used for the detailed rate breakdown
    # display only — not for total_cost.
    _main_day_imp = defaultdict(float)
    _main_day_exp = defaultdict(float)
    _main_day_sc  = defaultdict(float)
    standing_by_day = defaultdict(float)
    charged_days   = set()
    meter_meta     = {}

    sorted_blocks = sorted([b for b in blocks if b and b.get("start")], key=lambda b: b["start"])

    _period_utc_starts = []  # UTC starts of blocks that fall IN this period (for total_cost)

    # Once per summary, not once per block (C, 30 Sep 2026: on a year's bill these were
    # ~15% of the time). `vat_rate_at` reads and parses the VAT calendar on every call,
    # and resolves on the DATE alone (vat_calendar.resolve_vat uses date_iso[:10]), so
    # memoising by date is exact. Timezone objects are built once per name.
    _vat_memo = {}

    def _vat_for(start):
        day = str(start)[:10]
        if day not in _vat_memo:
            _vat_memo[day] = store.vat_rate_at(start)
        return _vat_memo[day]

    from zoneinfo import ZoneInfo as _ZI
    _UTC_ZI = _ZI("UTC")
    _tz_objs = {}

    for block in sorted_blocks:
        # block["start"] is a UTC ISO string; parse it and convert to local naive
        # so BST blocks at 23:xx UTC compare correctly against local period boundaries.
        _block_utc = datetime.fromisoformat(block["start"])
        _tz_name = ((block.get("meters") or {})
                    .get("electricity_main", {}) or {})
        _tz_name = (_tz_name.get("meta") or {}).get("timezone") or block.get("_timezone")
        try:
            _tzo = _tz_objs.get(_tz_name)
            if _tzo is None:
                _tzo = _tz_objs[_tz_name] = _ZI(_tz_name or "UTC")
            block_start = _block_utc.replace(tzinfo=_UTC_ZI).astimezone(_tzo).replace(tzinfo=None)
        except Exception:
            block_start = _block_utc
        if not (period_start <= block_start < period_end):
            continue
        _period_utc_starts.append(block["start"])

        day_key = block_start.date()
        meters = block.get("meters", {}) or {}

        # ── Pass 1: accumulate sub-meter grid-attributed kwh/cost per rate ──
        sub_by_rate = defaultdict(lambda: {"kwh": 0.0, "cost": 0.0})
        for meter_name, meter in meters.items():
            if not (meter or {}).get("meta", {}).get("sub_meter"):
                continue
            for channel_name, channel in (meter.get("channels", {}) or {}).items():
                if channel_name.lower().endswith("export"):
                    continue
                try:
                    channel = channel or {}
                    rate = round(float(channel.get("rate_used", channel.get("rate")) or 0.0), 4)
                    # Use kwh_grid (grid-attributed portion) not kwh (total device consumption)
                    sub_by_rate[rate]["kwh"]  += float(channel.get("kwh_grid", channel.get("kwh")) or 0.0)
                    sub_by_rate[rate]["cost"] += float(channel.get("cost") or 0.0)
                except Exception:
                    pass

        # ── Pass 2: accumulate all meters, subtracting sub-meter totals from main import ──
        for meter_name, meter in meters.items():
            meter_m = (meter or {}).get("meta", {}) or {}
            is_sub  = meter_m.get("sub_meter", False)
            if is_sub:
                display_name = meter_m.get("device") or meter_name.replace("_", " ").title()
            else:
                display_name = meter_m.get("site") or meter_m.get("site_name") or meter_name.replace("_", " ").title()
            if not display_name:
                display_name = meter_name.replace("_", " ").title()
            is_main_import = (meter_name == "electricity_main")

            for channel_name, channel in (meter.get("channels", {}) or {}).items():
                # Sub-meter export channels are already captured in the main meter's
                # export total — skip them to prevent phantom export sections in the bill.
                if is_sub and channel_name.lower().endswith("export"):
                    continue
                try:
                    channel = channel or {}
                    channel_m = (channel.get("meta", {}) or {})

                    # Sub-meter rows show the device's GRID-attributed share
                    # (kwh_grid), NOT its total consumption (kwh_total). Total would
                    # include energy the device drew from battery/solar, which never
                    # touched the grid — so the breakdown would overshoot the grid
                    # import total and the kWh would no longer correspond to the
                    # grid-based cost. This mirrors the sub_by_rate reconciliation
                    # above and the Usage-Stats aggregation. The main meter keeps its
                    # total (grid) figure. (Total_cost is unaffected — see note at top.)
                    if is_sub:
                        kwh = float(channel.get("kwh_grid",
                                    channel.get("kwh_total", channel.get("kwh"))) or 0.0)
                    else:
                        kwh = float(channel.get("kwh_total", channel.get("kwh")) or 0.0)
                    cost = float(channel.get("cost") or 0.0)
                    rate = round(float(channel.get("rate_used", channel.get("rate")) or 0.0), 4)
                    is_export = channel_name.lower().endswith("export")

                    if is_export:
                        if is_main_import:
                            main_export_raw["kwh"]  += float(channel.get("kwh") or 0.0)
                            main_export_raw["cost"] += float(channel.get("cost") or 0.0)
                        cost = -abs(cost)
                    elif is_main_import:
                        # Store raw total before sub-meter subtraction
                        main_import_raw[rate]["kwh"]  += kwh
                        main_import_raw[rate]["cost"] += cost
                        # BL-9: split this block's import into EV (dispatch-derived,
                        # stored) and Home (remainder), grouped by each portion's own
                        # rate. exc derived from the block's exc/inc ratio (flat VAT →
                        # exact). ev_by_rate only on dispatched IOG blocks; home_by_rate
                        # carries every main block's remainder → they sum to the total.
                        _cx = channel.get("cost_exc")
                        if _cx is not None and cost:
                            _ratio = float(_cx) / cost
                        else:
                            # Not-yet-settled block has no stored ex-VAT cost. Counting
                            # its kWh at £0 exc would DILUTE the displayed ex-VAT rate
                            # (EV/Home read below the true off-peak rate until every
                            # block settles). Fall back to the same inc ÷ (1+VAT) basis
                            # the rest of the bill uses for uncovered slots, so the rate
                            # is correct immediately and self-corrects to the exact
                            # figure when the block settles and stamps cost_exc.
                            _vat = _vat_for(block.get("start")) if store is not None else 0.05
                            _ratio = 1.0 / (1.0 + _vat)
                        # BL-27: price the split from the block's priced SEGMENTS when they
                        # travel with it (block_store attaches them to the import channel).
                        # Each segment already carries its own inc/exc rate + attribution, so
                        # a boundary block simply presents its constituent rate rows — the
                        # mixed-band "transition" special-case is retired for segment blocks.
                        # Legacy blocks (pre-backfill / no segments) keep the column path.
                        _segs = channel.get("segments")
                        if _segs:
                            for _s in _segs:
                                _sk = float(_s.get("kwh") or 0.0)
                                if _sk <= 1e-9:
                                    continue
                                _sr = round(float(_s.get("inc_rate") or 0.0), 4)
                                _sc = _sk * float(_s.get("inc_rate") or 0.0)
                                _sxr = _s.get("exc_rate")
                                _sx = (_sk * float(_sxr)) if _sxr is not None \
                                    else _sc * _ratio
                                _grp = ev_by_rate if _s.get("attribution") == "ev" \
                                    else home_by_rate
                                _grp[_sr]["kwh"]      += _sk
                                _grp[_sr]["cost"]     += _sc
                                _grp[_sr]["cost_exc"] += _sx
                        else:
                            _evk = float(channel.get("kwh_ev") or 0.0)
                            _evc = float(channel.get("cost_ev") or 0.0)
                            if _evk > 1e-9:
                                _evx = _evc * _ratio
                                if channel.get("ev_band") == "mixed":
                                    ev_transition["kwh"]      += _evk
                                    ev_transition["cost"]     += _evc
                                    ev_transition["cost_exc"] += _evx
                                else:                          # clean band → exact rate row
                                    _evr = round(float(channel.get("rate_ev") or 0.0), 4)
                                    ev_by_rate[_evr]["kwh"]      += _evk
                                    ev_by_rate[_evr]["cost"]     += _evc
                                    ev_by_rate[_evr]["cost_exc"] += _evx
                            _hk = kwh - _evk
                            _hc = cost - _evc
                            if _hk > 1e-9:
                                _hx = _hc * _ratio
                                if channel.get("home_band") == "mixed":
                                    home_transition["kwh"]      += _hk
                                    home_transition["cost"]     += _hc
                                    home_transition["cost_exc"] += _hx
                                else:
                                    _hr = round(_hc / _hk, 4)
                                    home_by_rate[_hr]["kwh"]      += _hk
                                    home_by_rate[_hr]["cost"]     += _hc
                                    home_by_rate[_hr]["cost_exc"] += _hx
                        # Use kwh_remainder directly when available. With no
                        # sub-meters this key is absent (NULL in DB, omitted by
                        # reconstruction), so the raw kwh is kept unchanged —
                        # api+mini blocks are identical to CAD here.
                        # Floor the sub-meter subtraction only when the main cost is
                        # >= 0 (kills a power-integration over-subtraction artifact);
                        # a genuinely negative main cost (Agile plunge-price credit)
                        # must pass through, not clamp to 0.
                        if "kwh_remainder" in channel:
                            kwh  = float(channel["kwh_remainder"])
                            _c = cost - sub_by_rate[rate]["cost"]
                            cost = _c if cost < 0 else max(0.0, _c)
                        else:
                            kwh  = max(0.0, kwh  - sub_by_rate[rate]["kwh"])
                            _c = cost - sub_by_rate[rate]["cost"]
                            cost = _c if cost < 0 else max(0.0, _c)

                    key = f"{display_name} / {channel_name.replace('_', ' ').title()}"
                    if key not in meter_meta:
                        meter_meta[key] = {
                            "site":        meter_m.get("site"),
                            "device":      meter_m.get("device"),
                            "mpan":        channel_m.get("mpan"),
                            "tariff":      channel_m.get("tariff"),
                            "is_submeter": bool(meter_m.get("sub_meter")),
                        }
                    meter_summary[key][rate]["kwh"]  += kwh
                    meter_summary[key][rate]["cost"] += cost
                    meter_totals[key]["kwh"]         += kwh
                    meter_totals[key]["cost"]        += cost
                    meter_totals[key]["is_submeter"]  = bool(meter_m.get("sub_meter"))

                    if not meter_m.get("sub_meter"):
                        rs = channel.get("read_start")
                        re = channel.get("read_end")
                        # A cumulative meter register only increases, so the
                        # period's opening read is the SMALLEST real read and the
                        # closing is the LARGEST. Ignore 0/None reads: gap /
                        # interpolated / reset blocks and register-less sources
                        # (CAD power sensors, API Measurements) carry 0, and the old
                        # MIN-start / last-wins-end aggregation let a single such
                        # block drag the displayed Start/End to 0.000 even when real
                        # reads exist for the period.
                        for _tgt in (meter_summary[key][rate], meter_totals[key]):
                            if rs is not None and rs > 0 and (
                                    _tgt["read_start"] is None or rs < _tgt["read_start"]):
                                _tgt["read_start"] = rs
                            if re is not None and re > 0 and (
                                    _tgt["read_end"] is None or re > _tgt["read_end"]):
                                _tgt["read_end"] = re
                except Exception:
                    pass

        # Standing charge
        for meter in (meters or {}).values():
            if (meter or {}).get("meta", {}).get("sub_meter"):
                continue
            sc = float((meter or {}).get("standing_charge") or 0.0)
            if sc > 0:
                # Start-of-day: keep the first non-zero charge seen (blocks are
                # processed in ascending order). Not MAX (over-bills on a drop).
                if not standing_by_day.get(day_key):
                    standing_by_day[day_key] = sc
            elif day_key not in charged_days:
                standing_by_day.setdefault(day_key, 0.0)
        charged_days.add(day_key)

    # ── Round accumulated raw values once at the end ──
    # This matches how Octopus bills: sum raw block costs, round once to 2dp.
    # No intermediate per-day rounding — that would introduce cumulative error.
    for key in meter_summary:
        for rate in meter_summary[key]:
            meter_summary[key][rate]["kwh"]  = round(meter_summary[key][rate]["kwh"],  3)
            meter_summary[key][rate]["cost"] = round(meter_summary[key][rate]["cost"], 2)
        meter_totals[key]["kwh"]  = round(meter_totals[key]["kwh"],  3)
        meter_totals[key]["cost"] = round(meter_totals[key]["cost"], 2)
    total_standing = round(sum(standing_by_day.values()), 2)
    # Round main_import_raw for display
    for rate in main_import_raw:
        main_import_raw[rate]["kwh"]  = round(main_import_raw[rate]["kwh"],  3)
        main_import_raw[rate]["cost"] = round(main_import_raw[rate]["cost"], 2)
    # BL-9: round the house/EV split — costs to 3dp so the per-rate breakdown shows
    # like the statement (e.g. £8.001), while the section Total stays 2dp.
    for _grp in (ev_by_rate, home_by_rate):
        for _r in _grp:
            # B6: keep the UNROUNDED cost for the per-band RATE derivation in
            # _bill_split_rows. Rounding the cost to 3dp BEFORE the rate = Σcost ÷ Σkwh
            # drifts a small-kWh band off its canonical rate (a 3.7 kWh peak band read
            # 0.3076, not the tariff 0.3077, even with canonical segments post-M1). Costs
            # are still SHOWN to 3dp (statement style); only the rate reads the raw.
            _grp[_r]["cost_raw"]     = _grp[_r]["cost"]
            _grp[_r]["cost_exc_raw"] = _grp[_r]["cost_exc"]
            _grp[_r]["kwh"]      = round(_grp[_r]["kwh"], 3)
            _grp[_r]["cost"]     = round(_grp[_r]["cost"], 3)
            _grp[_r]["cost_exc"] = round(_grp[_r]["cost_exc"], 3)
    for _t in (ev_transition, home_transition):
        _t["cost_raw"]     = _t["cost"]
        _t["cost_exc_raw"] = _t["cost_exc"]
        _t["kwh"]      = round(_t["kwh"], 3)
        _t["cost"]     = round(_t["cost"], 3)
        _t["cost_exc"] = round(_t["cost_exc"], 3)
    # total_cost = sum of daily net_cost values.
    # Same method as api_blocks_summary: round each day's imp/sc/exp to 4dp,
    # compute net = round(imp + sc - exp, 2) per day, sum across days.
    # This ensures billing chart total agrees with Usage Stats at every level.
    # Populate main-meter-only daily accumulators from blocks.
    # Raw main meter imp/exp/sc per day — no sub-meter adjustment.
    # Derive timezone from first block metadata (same as main loop does per-block).
    _first_tz_name = None
    for _fb in sorted_blocks:
        _fmeta = ((_fb.get("meters") or {}).get("electricity_main") or {})
        _first_tz_name = (_fmeta.get("meta") or {}).get("timezone") or _fb.get("_timezone")
        if _first_tz_name:
            break
    try:
        from zoneinfo import ZoneInfo as _ZI2
        _main_tz = _ZI2(_first_tz_name or "UTC")
    except Exception:
        from zoneinfo import ZoneInfo as _ZI2
        _main_tz = _ZI2("UTC")
    for _blk in sorted_blocks:
        _blk_dt = datetime.fromisoformat(_blk["start"])
        _blk_local = _blk_dt.replace(tzinfo=timezone.utc).astimezone(_main_tz).replace(tzinfo=None)
        if not (period_start <= _blk_local < period_end):
            continue
        _blk_day = _blk_local.date()
        _blk_meters = _blk.get("meters", {}) or {}
        for _blk_mid, _blk_meter in _blk_meters.items():
            _blk_meta = (_blk_meter or {}).get("meta", {}) or {}
            if _blk_meta.get("sub_meter"):
                continue  # main meter only
            _blk_sc = float((_blk_meter or {}).get("standing_charge") or 0.0)
            if _blk_sc > 0 and not _main_day_sc.get(_blk_day):
                # Start-of-day (first non-zero, ascending order); not MAX.
                _main_day_sc[_blk_day] = _blk_sc
            for _blk_ch_name, _blk_ch in ((_blk_meter or {}).get("channels", {}) or {}).items():
                if not _blk_ch:
                    continue
                _blk_cost = float((_blk_ch or {}).get("cost") or 0.0)
                if _blk_ch_name.lower().endswith("export"):
                    _main_day_exp[_blk_day] += abs(_blk_cost)
                else:
                    _main_day_imp[_blk_day] += _blk_cost

    # Compute total_cost via BlockStore.compute_period_net — the single shared
    # implementation used by Live Power, Usage Stats and billing chart.
    # Fall back to local accumulation if store not provided.
    if store is not None and tz_name and _period_utc_starts:
        # Bounds from the blocks IN THIS PERIOD only — using min/max of ALL
        # sorted_blocks spanned the whole dataset, so every period returned the
        # same (all-time) net. Now compute_period_net runs over just this period.
        _utc_s = min(_period_utc_starts)
        _utc_e_dt = max(datetime.fromisoformat(s) for s in _period_utc_starts)
        # utc_e must be past the last block — add 30 minutes
        from datetime import timedelta as _timedelta
        _utc_e = (_utc_e_dt + _timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")
        total_cost = store.compute_period_net(_utc_s, _utc_e, tz_name)
    elif store is not None and tz_name:
        total_cost = 0.0   # no blocks fall in this period
    else:
        _daily_nets = [
            round(
                round(_main_day_imp[_d], 4)
                + round(_main_day_sc.get(_d, 0.0), 4)
                - round(_main_day_exp.get(_d, 0.0), 4),
                2
            )
            for _d in sorted(_main_day_imp)
        ]
        total_cost = round(sum(_daily_nets), 2)

    # Collect main meter read_start / read_end for the raw import header
    _main_key = next((k for k in meter_totals if "Electricity Main / Import" in k
                      and not meter_totals[k].get("is_submeter")), None)
    main_import_reads = {
        "read_start": meter_totals[_main_key]["read_start"] if _main_key else None,
        "read_end":   meter_totals[_main_key]["read_end"]   if _main_key else None,
    }

    return {
        "start":             period_start,
        "end":               period_end,
        "meters":            meter_summary,
        "totals":            meter_totals,
        "main_import_raw":   dict(main_import_raw),
        "ev_by_rate":        dict(ev_by_rate),
        "home_by_rate":      dict(home_by_rate),
        "ev_transition":     ev_transition,
        "home_transition":   home_transition,
        "main_import_reads": main_import_reads,
        "standing":          standing_by_day,
        "total_standing":    total_standing,
        "total_cost":        total_cost,
        "meter_meta":        meter_meta,
    }


def bankers_round(x, ndigits=0):
    """Round half-to-even (banker's rounding) — how Octopus rounds a bill (0.015→0.02,
    0.025→0.02). Uses Decimal(str(x)) so exact decimal halves round correctly, which
    the float built-in round() cannot (0.015 is 0.01499… in binary). BL-24 groundwork."""
    q = _Decimal(1).scaleb(-int(ndigits))
    return float(_Decimal(str(x)).quantize(q, rounding=_ROUND_HALF_EVEN))


def octopus_bill_total(slots, vat_rate=0.05):
    """Ex-VAT energy total for a set of (kwh, exc_rate) slots (exc_rate £/kWh), rounding
    only at the TOTAL (whole penny); VAT on top. Empirically — verified against a real
    Octopus bill — the per-half-hour figures are summed RAW; Octopus does NOT round each
    half-hour's kWh/cost (that would zero the many sub-0.01 kWh slots). See
    _bill_method_breakdown, which is what the Billing summary uses. Returns pence + £."""
    exc_pence = bankers_round(sum(kwh * float(exc_rate) * 100.0 for kwh, exc_rate in slots), 0)
    inc_pence = bankers_round(exc_pence * (1.0 + vat_rate), 0)
    return {"exc_pence": exc_pence, "inc_pence": inc_pence,
            "exc_gbp": round(exc_pence / 100.0, 2),
            "inc_gbp": round(inc_pence / 100.0, 2)}


def bill_slots_from_blocks(blocks, vat_rate=0.05):
    """Extract (kwh, exc_rate) main-import slots from block dicts for octopus_bill_total.
    Uses the stored exc-VAT cost (BL-23 imp_cost_exc → channel 'cost_exc') to derive the
    exc rate when present; otherwise falls back to the inc rate ÷ (1+VAT) — an
    approximation until a re-import captures the real exc figure."""
    slots = []
    for b in blocks or []:
        imp = (((b or {}).get("meters") or {}).get("electricity_main") or {})
        imp = (imp.get("channels") or {}).get("import") or {}
        kwh = imp.get("kwh")
        if not kwh:
            continue
        # Prefer the stored published ex-VAT rate, then the ex-VAT cost, then the
        # inc rate ÷ (1+VAT) approximation (until a re-import captures real exc).
        if imp.get("rate_exc") is not None:
            exc_rate = float(imp["rate_exc"])
        elif imp.get("cost_exc") is not None:
            exc_rate = float(imp["cost_exc"]) / float(kwh)
        else:
            rate = imp.get("rate")
            if rate is None:
                continue
            exc_rate = float(rate) / (1.0 + vat_rate)
        slots.append((float(kwh), exc_rate))
    return slots
