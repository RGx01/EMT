"""
The Billing tab's data, for /api/energy?include=billing — C, 30 Sep 2026.

The pre-rendered Billing page (energy_charts.generate_daily_import_export_charts)
computes, per period, a bill summary and, per day, a chart's series and side panel, and
bakes both into one HTML file for all of history. This module computes the same two
things for ONE requested range, from the same functions:

  * period — energy_charts.period_summary, under billing_context: the summary, with
    the EV split and (opt-in) the bill method, and bill_card.card — its rows as the
    card prints them;
  * day    — energy_charts.day_chart_data, for each local day in the range: the JSON
    the page embedded per day, the side panel as data, and bill_card.panel_lines.

Nothing here derives a figure. It chooses the inputs (the range's blocks, the
store-wide ex-VAT gate, the engine's cap date and import schedule) and makes the
result JSON-safe. Byte-identity with the pre-render is the point, and
tests/test_billing_view.py holds it.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import bill_card as _card
import energy_charts as _ec


def _main_meta(cfg):
    for md in (cfg.get("meters") or {}).values():
        meta = md.get("meta") or {}
        if not meta.get("sub_meter"):
            return meta
    return {}


def _local_naive(utc_iso, tz):
    return (datetime.fromisoformat(utc_iso).replace(tzinfo=timezone.utc)
            .astimezone(tz).replace(tzinfo=None))


def _local_days(blocks, tz):
    """The local dates the blocks fall on — group_blocks_by_day's keys (the same
    UTC → local conversion), without building its per-day lists."""
    utc, out = timezone.utc, set()
    for b in blocks:
        s = (b or {}).get("start")
        if s:
            try:
                out.add(datetime.fromisoformat(s).replace(tzinfo=utc).astimezone(tz).date().isoformat())
            except Exception:
                pass
    return out


def _has_exc(store):
    """BL-24's capability gate for the WHOLE store: is any ex-VAT import cost held?
    The pre-render answers it over all of history; a single period cannot."""
    row = store._conn.execute(
        "SELECT 1 FROM blocks WHERE meter_id = 'electricity_main' "
        "AND imp_cost_exc IS NOT NULL LIMIT 1").fetchone()
    return row is not None


def rate_context():
    """The engine's cap date and import schedule — what the rate lines are gated on.

    The pre-render took both from the engine (engine.generate_charts, deleted with it
    by C); so does this.
    Neither is defaulted when the engine is not running: None is what the pre-render
    passes too, and it means uncapped TOU days and stored house rates."""
    try:
        import engine as _eng
        return _eng._chart_cap_from(), _eng._kraken_rate_schedules.get("import")
    except Exception:
        return None, None


# ── JSON-safe summary ────────────────────────────────────────────────────────

def _by_rate(mapping):
    """{rate: {...}} → [{"rate": rate, ...}] ascending by rate — the order every
    renderer iterates in (`sorted(channels)`)."""
    return [dict(v, rate=r) for r, v in sorted((mapping or {}).items())]


def summary_json(summary):
    """calculate_billing_summary_for_period's dict, JSON-safe and lossless.

    Rate-keyed maps (float keys) become lists sorted by rate; `standing`
    (date → amount) becomes a list sorted by day; period bounds are local ISO.
    Everything else is already plain."""
    if not summary:
        return None
    out = {
        "start":             summary["start"].isoformat(),
        "end":               summary["end"].isoformat(),
        "meters":            {k: _by_rate(v) for k, v in summary["meters"].items()},
        "totals":            {k: dict(v) for k, v in summary["totals"].items()},
        "main_import_raw":   _by_rate(summary.get("main_import_raw")),
        "ev_by_rate":        _by_rate(summary.get("ev_by_rate")),
        "home_by_rate":      _by_rate(summary.get("home_by_rate")),
        "ev_transition":     dict(summary.get("ev_transition") or {}),
        "home_transition":   dict(summary.get("home_transition") or {}),
        "main_import_reads": dict(summary.get("main_import_reads") or {}),
        "standing":          [{"day": d.isoformat(), "amount": a}
                              for d, a in sorted((summary.get("standing") or {}).items())],
        "total_standing":    summary.get("total_standing"),
        "total_cost":        summary.get("total_cost"),
        "meter_meta":        {k: dict(v) for k, v in (summary.get("meter_meta") or {}).items()},
    }
    if "bill_method" in summary:
        out["bill_method"] = summary["bill_method"]
    return out


# ── the two facets ───────────────────────────────────────────────────────────

def _context(store, cfg, blocks):
    return _ec.billing_context(cfg, store, blocks, has_exc=_has_exc(store))


# get_bill_rows' positions, by name (step 5, 8 Oct 2026): hard-coded indexes broke when the
# remainder left the row, and would break again with each column that goes.
_C = None


def _col():
    global _C
    if _C is None:
        import billing as _b
        _C = {name: i for i, name in enumerate(_b.BILL_ROW_COLUMNS)}
    return _C


def _bill_blocks(rows, segments, ev_meter):
    """The period's blocks as the bill's other readers see them, from get_bill_rows (#48).

    Those readers are the EV slot maps, the EV carve, the bill method and the day list.
    Between them they read a block's start, the main meter's import channel (kWh,
    cost, rates, ex-VAT figures, EV split and segments) and standing charges, and the
    EV meter's import kWh, cost and rate. Each value is the one get_blocks_lightweight
    puts there, and a key is present exactly when it puts it there. Nothing else is
    built: the other meters and the export channels cost a year's bill half its time
    on the Pi."""
    c = _col()
    i_kwh, i_cost, i_rate = c["imp_kwh"], c["imp_cost"], c["imp_rate"]
    i_sc, i_sc_exc = c["standing_charge"], c["standing_charge_exc"]
    opt = tuple((k, c[col]) for k, col in (("cost_exc", "imp_cost_exc"), ("rate_exc", "imp_rate_exc"),
                                           ("kwh_ev", "imp_kwh_ev"), ("cost_ev", "imp_cost_ev"),
                                           ("rate_ev", "imp_rate_ev")))
    out, cur, last = [], None, None
    for r in rows:
        bs, mid = r[0], r[1]
        if bs != last:
            cur = {"start": bs, "meters": {}}
            out.append(cur)
            last = bs
        if mid != "electricity_main" and mid != ev_meter:
            continue
        imp = {"kwh": float(r[i_kwh] or 0), "cost": float(r[i_cost] or 0)}
        if r[i_rate] is not None:
            imp["rate"] = float(r[i_rate])
        if mid != "electricity_main":
            cur["meters"][mid] = {"channels": {"import": imp}}
            continue
        for k, i in opt:
            if r[i] is not None:
                imp[k] = float(r[i])
        segs = segments.get((bs, mid))
        if segs:
            imp["segments"] = [{"kwh": k, "inc_rate": i, "exc_rate": x, "band": b,
                                "attribution": a} for k, i, x, b, a in segs]
        m = cur["meters"][mid] = {"standing_charge": float(r[i_sc] or 0),
                                  "channels": {"import": imp}}
        if r[i_sc_exc] is not None:
            m["standing_charge_exc"] = float(r[i_sc_exc])
    return out


def period_summary_raw(store, cfg, tz_name, utc_start, utc_end):
    """(summary, ctx, blocks) for the range — the summary as Python objects, before
    summary_json. What render_billing_summary takes, so a check can format it.

    The summary reads flat rows (#48, 3 Oct 2026); `blocks` are _bill_blocks. With no
    configuration, billing_context colours meters from the blocks themselves, so
    that case keeps the full fetch."""
    tz = ZoneInfo(tz_name)
    ps, pe = _local_naive(utc_start, tz), _local_naive(utc_end, tz)
    if not cfg:
        blocks = store.get_blocks_lightweight(utc_start, utc_end)
        ctx = _context(store, cfg, blocks)
        summary = _ec.period_summary(ctx, blocks, blocks, ps, pe, store=store, tz_name=tz_name)
        return summary, ctx, blocks
    rows, segments = store.get_bill_rows(utc_start, utc_end)
    blocks = _bill_blocks(rows, segments, _ec._ev_meter_id(cfg))
    ctx = _context(store, cfg, blocks)
    summary = _ec.period_summary(ctx, None, blocks, ps, pe, store=store, tz_name=tz_name,
                                 bill_rows=(rows, segments))
    return summary, ctx, blocks


def period_billing(store, cfg, tz_name, utc_start, utc_end, meta):
    """`resolution=period&include=billing`: the bill summary for the range, plus what
    the tab shows around it — the local days that hold data (newest first), the site
    name and currency the card is headed with, and whether this billing period was
    started by a configuration change rather than the billing day."""
    tz = ZoneInfo(tz_name)
    summary, ctx, blocks = period_summary_raw(store, cfg, tz_name, utc_start, utc_end)
    ps = _local_naive(utc_start, tz)
    mm = _main_meta(cfg)
    days = sorted(_local_days(blocks, tz), reverse=True)
    site, currency = mm.get("site") or None, mm.get("currency_symbol", "£")
    out = {"summary": summary_json(summary),
           # What the card shows, row by row, every figure already formatted
           # (bill_card) — the browser prints it and decides nothing.
           "card": _card.card(summary, currency=currency, site_name=site),
           "days": days,
           "site": site,
           "currency": currency,
           "bill_rounding": ctx["bill_rounding"]}
    if (meta or {}).get("kind") == "billing":
        out["is_config_change"] = _is_config_change(store, tz, ps, mm)
    return out


def _is_config_change(store, tz, ps, mm):
    """The pre-render's rule: not the first period, and it starts on a day other than
    the (current) billing day — a mid-cycle configuration change opened it."""
    import billing as _billing
    periods = _billing.get_billing_periods_from_config_periods(store.get_config_periods(), tz=tz)
    starts = [p[0].replace(tzinfo=None) for p in periods]
    try:
        billing_day = int(mm.get("billing_day") or 1)
    except (TypeError, ValueError):
        billing_day = 1
    return bool(starts) and ps != starts[0] and ps.day != billing_day


def day_billing(store, cfg, tz_name, utc_start, utc_end):
    """`resolution=day&include=billing`: every local day in the range with data,
    newest first — {"day", "chart", "panel"} each, from day_chart_data."""
    tz = ZoneInfo(tz_name)
    mm = _main_meta(cfg)
    block_minutes = int(mm.get("block_minutes") or 30)
    currency = mm.get("currency_symbol", "£")
    blocks = store.get_blocks_lightweight(utc_start, utc_end)
    ctx = _context(store, cfg, blocks)
    cap_from, import_schedule = rate_context()
    days_map = _ec.group_blocks_by_day(blocks, tz, block_minutes)
    ev_phys = ctx["ev_phys_id"]
    out = []
    for day in sorted(days_map, reverse=True):
        d = _ec.day_chart_data(
            day, days_map[day], ctx["meter_colors"], block_minutes=block_minutes,
            currency=currency, ev_slot_map=ctx["ev_day_map"],
            bill_rounding=ctx["bill_rounding"], fallback_vat=_ec.vat_at(ctx, day),
            ev_fold_meter=ev_phys, ev_label=("EV" if ev_phys else None),
            cap_from=cap_from, import_schedule=import_schedule, store=store,
            timezone_name=tz_name)
        out.append({"day": day, "chart": d["chart"], "panel": d["panel"],
                    "panel_lines": _card.panel_lines(d["panel"], currency)})
    return {"days": out,
            "rate_context": {"cap_from": cap_from, "house_tou": import_schedule is not None}}
