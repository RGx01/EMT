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

import direct_import as _direct


def _utc_to_local(dt_naive: datetime, tz: "ZoneInfo") -> datetime:
    """Attach UTC, then convert to local timezone."""
    return dt_naive.replace(tzinfo=timezone.utc).astimezone(tz)


def _parse_block_start(iso_str: str, tz: "ZoneInfo") -> datetime:
    """Parse a UTC ISO block-start string and return it in local time."""
    return _utc_to_local(datetime.fromisoformat(iso_str), tz)


def current_billing_period(config_periods, tz, now_local, billing_day):
    """The billing period containing `now_local`'s date: `(start, end_exclusive)`, naive
    LOCAL midnights — Live Power's "This Bill" (moved here from `/api/billing`, C 1 Oct 2026).

    From the config periods' own billing days where a generated period holds today. The
    generator stops at the last block, so when the newest data predates the current period
    (or there are no periods) none does, and the current one is built from `billing_day`
    (the config file's, as /api/billing used) — issue #221: "This Bill" must show the
    CURRENT period even with no data yet, not the last historical one."""
    import calendar as _cal
    today = now_local.date()
    for ps, pe in get_billing_periods_from_config_periods(config_periods, tz=tz):
        if ps.date() <= today < pe.date():
            return ps.replace(tzinfo=None), pe.replace(tzinfo=None)
    bd = int(billing_day or 1)
    dim = _cal.monthrange(now_local.year, now_local.month)[1]
    if now_local.day >= min(bd, dim):
        sy, sm = now_local.year, now_local.month
    else:
        sm = now_local.month - 1 or 12
        sy = now_local.year if now_local.month > 1 else now_local.year - 1
    start = datetime(sy, sm, min(bd, _cal.monthrange(sy, sm)[1]))
    ey, em = (sy + 1, 1) if sm == 12 else (sy, sm + 1)
    return start, datetime(ey, em, min(bd, _cal.monthrange(ey, em)[1]))


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


def get_billing_periods_from_config_periods(config_periods, tz):
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



def _channel_from_dict(channel_name, channel, is_sub, is_export, display_name):
    """One channel of a dict block, every `.get` fallback the summary reads resolved
    once (#48). The record's fields are listed in `_summarise`. None when a value
    cannot be converted: the old loop's try/except skipped the channel there too."""
    nonempty = bool(channel)
    channel = channel or {}
    channel_m = (channel.get("meta", {}) or {})
    key = f"{display_name} / {channel_name.replace('_', ' ').title()}"
    try:
        rate = round(float(channel.get("rate_used", channel.get("rate")) or 0.0), 4)
        cost = float(channel.get("cost") or 0.0)
        if is_sub:
            # Pass 1 nets the device's grid share (kwh_grid, else kwh); pass 2 shows
            # it (kwh_grid, else kwh_total, else kwh). See _summarise.
            sub_kwh = float(channel.get("kwh_grid", channel.get("kwh")) or 0.0)
            kwh = float(channel.get("kwh_grid",
                        channel.get("kwh_total", channel.get("kwh"))) or 0.0)
        else:
            sub_kwh = 0.0
            kwh = float(channel.get("kwh_total", channel.get("kwh")) or 0.0)
        raw_kwh = float(channel.get("kwh") or 0.0) if is_export else 0.0
        _cx = channel.get("cost_exc")
        cost_exc = float(_cx) if _cx is not None else None
        _segs = channel.get("segments")
        segs = None
        if _segs:
            segs = []
            for _s in _segs:
                _sxr = _s.get("exc_rate")
                segs.append((float(_s.get("kwh") or 0.0), float(_s.get("inc_rate") or 0.0),
                             float(_sxr) if _sxr is not None else None,
                             _s.get("attribution") == "ev"))
        evk = float(channel.get("kwh_ev") or 0.0)
        evc = float(channel.get("cost_ev") or 0.0)
        evr = round(float(channel.get("rate_ev") or 0.0), 4)
    except Exception:
        return None
    # The stored remainder is not read (step 5, 8 Oct 2026): Direct is direct_import's rule.
    return (key, is_export, rate, sub_kwh, kwh, cost, raw_kwh, cost_exc, segs,
            evk, evc, evr, channel.get("ev_band"), channel.get("home_band"),
            channel.get("read_start"), channel.get("read_end"),
            channel_m.get("mpan"), channel_m.get("tariff"), nonempty)


def _display_name(meter_name, is_sub, site, site_name, device):
    if is_sub:
        name = device or meter_name.replace("_", " ").title()
    else:
        name = site or site_name or meter_name.replace("_", " ").title()
    return name or meter_name.replace("_", " ").title()


def _records_from_blocks(blocks):
    """Block dicts (get_blocks_lightweight's, the engine's, a test's) as _summarise's
    records, in start order."""
    for block in sorted([b for b in blocks if b and b.get("start")], key=lambda b: b["start"]):
        _tzm = ((block.get("meters") or {}).get("electricity_main", {}) or {})
        tz_name = (_tzm.get("meta") or {}).get("timezone") or block.get("_timezone")
        meters = []
        for meter_name, meter in (block.get("meters", {}) or {}).items():
            meter_m = (meter or {}).get("meta", {}) or {}
            is_sub = meter_m.get("sub_meter", False)
            display_name = _display_name(meter_name, is_sub, meter_m.get("site"),
                                         meter_m.get("site_name"), meter_m.get("device"))
            channels = []
            for channel_name, channel in (meter.get("channels", {}) or {}).items():
                is_export = channel_name.lower().endswith("export")
                if is_sub and is_export:
                    continue    # neither pass reads a sub-meter's export
                channels.append(_channel_from_dict(channel_name, channel, is_sub,
                                                   is_export, display_name))
            sc = 0.0 if is_sub else float((meter or {}).get("standing_charge") or 0.0)
            meters.append((meter_name, is_sub, meter_m.get("site"), meter_m.get("device"),
                           sc, channels))
        yield block["start"], tz_name, meters


# The columns summarise_bill_rows unpacks, in order: BlockStore.BILL_ROW_COLUMNS
# (this module imports no storage; tests/test_bill_rows.py holds the two equal).
BILL_ROW_COLUMNS = (
    "block_start", "meter_id", "is_sub_meter", "device_label", "timezone",
    "imp_kwh", "imp_kwh_grid", "imp_rate", "imp_cost",
    "imp_read_start", "imp_read_end", "imp_cost_exc",
    "exp_kwh", "exp_rate", "exp_cost", "exp_read_start", "exp_read_end",
    "standing_charge")


def _records_from_rows(rows, segments):
    """BlockStore.get_bill_rows' rows as _summarise's records — the values
    get_blocks_lightweight would have put in its dicts, read the way
    _channel_from_dict reads them (no kwh_total or rate_used: the store has none)."""
    names = {}
    rec = None
    for (bs, mid, is_sub_meter, device, tz, imp_kwh, kwh_grid, imp_rate, imp_cost,
         imp_rs, imp_re, cost_exc,
         exp_kwh, exp_rate, exp_cost, exp_rs, exp_re, sc) in rows:
        if rec is None or rec[0] != bs:
            if rec is not None:
                yield rec[0], rec[1], rec[2]
            rec = [bs, tz, []]                       # the block's _timezone: its first row's
        if mid == "electricity_main" and tz:
            rec[1] = tz                              # the main meter's own timezone wins
        is_sub = bool(is_sub_meter)
        nk = (mid, is_sub, device)
        keys = names.get(nk)
        if keys is None:
            dn = _display_name(mid, is_sub, None, None, device)
            keys = names[nk] = (f"{dn} / Import", f"{dn} / Export")
        # Every number here is a REAL column, which SQLite returns as a float or None,
        # so `x or 0.0` is get_blocks_lightweight's `float(x or 0)` followed by the
        # summary's `float(... or 0.0)`, without either call (-0.0 becomes 0.0 too).
        imp = imp_kwh or 0.0
        icost = imp_cost or 0.0
        if is_sub:
            sub_kwh = kwh = (kwh_grid if kwh_grid is not None else imp) or 0.0
        else:
            sub_kwh, kwh = 0.0, imp
        segs = None
        if mid == "electricity_main":
            _sg = segments.get((bs, mid))
            if _sg:
                segs = [(k or 0.0, i or 0.0, x, a == "ev") for k, i, x, _band, a in _sg]
        channels = [(keys[0], False, round(imp_rate or 0.0, 4), sub_kwh, kwh, icost, 0.0,
                     # the EV split is the segments' (step 5c): a block without them has none
                     cost_exc, segs, 0.0, 0.0, 0.0, None, None,
                     imp_rs, imp_re, None, None, True)]
        if not is_sub:
            exp = exp_kwh or 0.0
            channels.append((keys[1], True, round(exp_rate or 0.0, 4), 0.0, exp,
                             exp_cost or 0.0, exp, None, None, 0.0, 0.0, 0.0,
                             None, None, exp_rs, exp_re, None, None, True))
        rec[2].append((mid, is_sub, None, device, 0.0 if is_sub else (sc or 0.0), channels))
    if rec is not None:
        yield rec[0], rec[1], rec[2]


def calculate_billing_summary_for_period(blocks, period_start, period_end, store=None, *, tz_name):
    return _summarise(_records_from_blocks(blocks), period_start, period_end, store,
                      tz_name=tz_name)


def summarise_bill_rows(rows, segments, period_start, period_end, store=None, *, tz_name):
    """calculate_billing_summary_for_period over BlockStore.get_bill_rows (#48): the
    same summary, from flat rows instead of block dicts."""
    return _summarise(_records_from_rows(rows, segments), period_start, period_end, store,
                      tz_name=tz_name)


def _summarise(records, period_start, period_end, store=None, *, tz_name):
    """The period's bill summary over records `(start_utc, timezone, meters)`, in start
    order. A meter is `(name, is_sub, site, device, standing_charge, channels)`; a
    channel is the tuple _channel_from_dict documents by building it, or None (skipped).

    One loop for both sources (#48, 3 Oct 2026). It was a loop over block dicts that
    re-read every field through `.get` chains, 2M calls on a year's bill. The
    arithmetic and its order are unchanged, so the summary is the same to the bit."""
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
    # Main-meter-only daily accumulators for total_cost, when no store is given.
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

    # Without a store (or a timezone) the total comes from the records themselves,
    # in a second pass below; only then are they kept.
    _fallback = store is None or not tz_name
    if _fallback:
        records = list(records)

    _period_utc_starts = []  # UTC starts of blocks that fall IN this period (for total_cost)

    # The VAT calendar is read ONCE per summary (#48, 2 Oct 2026). `store.vat_rate_at`
    # reads it from the database on every call: memoised by UTC hour (C, 30 Sep) that
    # was still 5,440 reads for a year's bill. `vat_rate_lookup` is the same rule —
    # `resolve_vat` on the start's UK-local day — over one merged calendar.
    # Timezone objects are built once per name.
    _vat_for = store.vat_rate_lookup() if store is not None else None

    from zoneinfo import ZoneInfo as _ZI
    _UTC_ZI = _ZI("UTC")
    _tz_objs = {}

    for start, _tz_name, meters in records:
        # start is a UTC ISO string; parse it and convert to local naive
        # so BST blocks at 23:xx UTC compare correctly against local period boundaries.
        _block_utc = datetime.fromisoformat(start)
        try:
            _tzo = _tz_objs.get(_tz_name)
            if _tzo is None:
                _tzo = _tz_objs[_tz_name] = _ZI(_tz_name or "UTC")
            block_start = _block_utc.replace(tzinfo=_UTC_ZI).astimezone(_tzo).replace(tzinfo=None)
        except Exception:
            block_start = _block_utc
        if not (period_start <= block_start < period_end):
            continue
        _period_utc_starts.append(start)

        day_key = block_start.date()

        # ── Pass 1: accumulate sub-meter grid-attributed kwh/cost per rate ──
        # [kwh, cost] per rate. Use kwh_grid (grid-attributed portion) not kwh
        # (total device consumption).
        # The block's devices in total, for its Direct import (direct_import).
        _dev_k = _dev_c = 0.0
        for _mn, is_sub, _site, _dev, _sc, channels in meters:
            if not is_sub:
                continue
            for ch in channels:
                if ch is None or ch[1]:            # a device's import only
                    continue
                _dev_k += ch[3]
                _dev_c += ch[5]

        # ── Pass 2: accumulate all meters, subtracting sub-meter totals from main import ──
        for meter_name, is_sub, site, device, _sc, channels in meters:
            is_main_import = (meter_name == "electricity_main")
            for ch in channels:
                if ch is None:
                    continue
                (key, is_export, rate, _subk, kwh, cost, raw_kwh, _cx, _segs,
                 _evk, _evc, _evr, ev_band, home_band,
                 rs, re, mpan, tariff, _nonempty) = ch
                # Sub-meter rows show the device's GRID-attributed share
                # (kwh_grid), NOT its total consumption (kwh_total). Total would
                # include energy the device drew from battery/solar, which never
                # touched the grid — so the breakdown would overshoot the grid
                # import total and the kWh would no longer correspond to the
                # grid-based cost. This mirrors the sub_by_rate reconciliation
                # above and the Usage-Stats aggregation. The main meter keeps its
                # total (grid) figure. (Total_cost is unaffected — see note at top.)
                if is_export:
                    if is_main_import:
                        main_export_raw["kwh"]  += raw_kwh
                        main_export_raw["cost"] += cost
                    cost = -abs(cost)
                elif is_main_import:
                    # Store raw total before sub-meter subtraction
                    _mr = main_import_raw[rate]
                    _mr["kwh"]  += kwh
                    _mr["cost"] += cost
                    # BL-9: split this block's import into EV (dispatch-derived,
                    # stored) and Home (remainder), grouped by each portion's own
                    # rate. exc derived from the block's exc/inc ratio (flat VAT →
                    # exact). ev_by_rate only on dispatched IOG blocks; home_by_rate
                    # carries every main block's remainder → they sum to the total.
                    if _cx is not None and cost:
                        _ratio = _cx / cost
                    else:
                        # Not-yet-settled block has no stored ex-VAT cost. Counting
                        # its kWh at £0 exc would DILUTE the displayed ex-VAT rate
                        # (EV/Home read below the true off-peak rate until every
                        # block settles). Fall back to the same inc ÷ (1+VAT) basis
                        # the rest of the bill uses for uncovered slots, so the rate
                        # is correct immediately and self-corrects to the exact
                        # figure when the block settles and stamps cost_exc.
                        _vat = _vat_for(start) if store is not None else 0.05
                        _ratio = 1.0 / (1.0 + _vat)
                    # BL-27: price the split from the block's priced SEGMENTS when they
                    # travel with it (block_store attaches them to the import channel).
                    # Each segment already carries its own inc/exc rate + attribution, so
                    # a boundary block simply presents its constituent rate rows — the
                    # mixed-band "transition" special-case is retired for segment blocks.
                    # Legacy blocks (pre-backfill / no segments) keep the column path.
                    if _segs:
                        for _sk, _si, _sxr, _s_ev in _segs:
                            if _sk <= 1e-9:
                                continue
                            _sr = round(_si, 4)
                            _sc = _sk * _si
                            _sx = (_sk * _sxr) if _sxr is not None else _sc * _ratio
                            _grp = (ev_by_rate if _s_ev else home_by_rate)[_sr]
                            _grp["kwh"]      += _sk
                            _grp["cost"]     += _sc
                            _grp["cost_exc"] += _sx
                    else:
                        if _evk > 1e-9:
                            _evx = _evc * _ratio
                            if ev_band == "mixed":
                                ev_transition["kwh"]      += _evk
                                ev_transition["cost"]     += _evc
                                ev_transition["cost_exc"] += _evx
                            else:                          # clean band → exact rate row
                                _g = ev_by_rate[_evr]
                                _g["kwh"]      += _evk
                                _g["cost"]     += _evc
                                _g["cost_exc"] += _evx
                        _hk = kwh - _evk
                        _hc = cost - _evc
                        if _hk > 1e-9:
                            _hx = _hc * _ratio
                            if home_band == "mixed":
                                home_transition["kwh"]      += _hk
                                home_transition["cost"]     += _hc
                                home_transition["cost_exc"] += _hx
                            else:
                                _g = home_by_rate[round(_hc / _hk, 4)]
                                _g["kwh"]      += _hk
                                _g["cost"]     += _hc
                                _g["cost_exc"] += _hx
                    # The Direct import: direct_import's one rule, per half-hour (6 Oct
                    # 2026). It was the stored kwh_remainder (equal to the rule on every
                    # real block measured) and this block's devices AT THE MAIN'S RATE.
                    kwh, cost = _direct.direct(kwh, cost, _dev_k, _dev_c)

                if key not in meter_meta:
                    meter_meta[key] = {
                        "site":        site,
                        "device":      device,
                        "mpan":        mpan,
                        "tariff":      tariff,
                        "is_submeter": bool(is_sub),
                    }
                _ms = meter_summary[key][rate]
                _mt = meter_totals[key]
                _ms["kwh"]  += kwh
                _ms["cost"] += cost
                _mt["kwh"]  += kwh
                _mt["cost"] += cost
                _mt["is_submeter"] = bool(is_sub)

                if not is_sub:
                    # A cumulative meter register only increases, so the
                    # period's opening read is the SMALLEST real read and the
                    # closing is the LARGEST. Ignore 0/None reads: gap /
                    # interpolated / reset blocks and register-less sources
                    # (CAD power sensors, API Measurements) carry 0, and the old
                    # MIN-start / last-wins-end aggregation let a single such
                    # block drag the displayed Start/End to 0.000 even when real
                    # reads exist for the period.
                    for _tgt in (_ms, _mt):
                        if rs is not None and rs > 0 and (
                                _tgt["read_start"] is None or rs < _tgt["read_start"]):
                            _tgt["read_start"] = rs
                        if re is not None and re > 0 and (
                                _tgt["read_end"] is None or re > _tgt["read_end"]):
                            _tgt["read_end"] = re

        # Standing charge
        for _mn, is_sub, _site, _dev, sc, _chs in meters:
            if is_sub:
                continue
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
    # Same method as the Usage Stats rows (usage_view): round each day's imp/sc/exp to 4dp,
    # compute net = round(imp + sc - exp, 2) per day, sum across days.
    # This ensures billing chart total agrees with Usage Stats at every level.
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
        # No store: the day nets come from the blocks themselves. This pass is the
        # fallback's alone; with a store, compute_period_net is the total (#48).
        # Raw main meter imp/exp/sc per day — no sub-meter adjustment.
        # The first block's timezone, for every block (the main loop's is per block).
        _first_tz_name = next((_tzn for _st, _tzn, _ms in records if _tzn), None)
        try:
            from zoneinfo import ZoneInfo as _ZI2
            _main_tz = _ZI2(_first_tz_name or "UTC")
        except Exception:
            from zoneinfo import ZoneInfo as _ZI2
            _main_tz = _ZI2("UTC")
        for _st, _tzn, _blk_meters in records:
            _blk_dt = datetime.fromisoformat(_st)
            _blk_local = _blk_dt.replace(tzinfo=timezone.utc).astimezone(_main_tz).replace(tzinfo=None)
            if not (period_start <= _blk_local < period_end):
                continue
            _blk_day = _blk_local.date()
            for _mn, _is_sub, _site, _dev, _blk_sc, _blk_chs in _blk_meters:
                if _is_sub:
                    continue  # main meter only
                if _blk_sc > 0 and not _main_day_sc.get(_blk_day):
                    # Start-of-day (first non-zero, ascending order); not MAX.
                    _main_day_sc[_blk_day] = _blk_sc
                for ch in _blk_chs:
                    if ch is None or not ch[18]:     # an empty channel (the tuple's last field)
                        continue
                    if ch[1]:                        # export
                        _main_day_exp[_blk_day] += abs(ch[5])
                    else:
                        _main_day_imp[_blk_day] += ch[5]
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


def octopus_bill_total(slots, vat_rate):
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


def bill_slots_from_blocks(blocks, vat_rate):
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
