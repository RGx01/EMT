from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo
from collections import defaultdict
import json
import carbon as _carbon
import os
import re


# ─────────────────────────────────────────────────────────────
# Shared period rolodex/spinner (single source of truth)
# ─────────────────────────────────────────────────────────────
# The Billing charts render inside a sandboxed iframe as a self-contained
# document, so they cannot Jinja-{% include %} the shared partial the way
# Usage Stats / Insights do. Instead we read that same partial at generate
# time and inline its <style>+<script> into the page. Reading the one file
# keeps the spinner behaviour identical across all three pages.

def _load_period_spinner_html() -> str:
    """Return the shared spinner partial's <style>+<script>, ready to inline.

    Strips the leading Jinja {# ... #} comment (which would otherwise show as
    literal text in the non-Jinja iframe document). Returns '' if the partial
    can't be found, in which case the Billing page simply keeps its native
    <select> dropdowns.
    """
    path = os.path.join(os.path.dirname(__file__),
                        "web", "templates", "_period_spinner.html")
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return ""
    return re.sub(r"\{#.*?#\}", "", raw, flags=re.S).strip()


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
    get_all_billing_periods,
    get_all_calmonth_periods,
    get_all_quarter_periods,
    get_all_year_periods,
    get_billing_periods_from_config_periods,
    get_billing_periods_from_config_history,
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
# Billing summary renderer
# ─────────────────────────────────────────────────────────────

# Above this many distinct rates in one channel's period breakdown, collapse the
# per-rate rows into a single kWh-weighted average row. Agile has ~48 rates a day →
# hundreds across a bill period, which makes the billing summary unreadable; the
# Total row is unchanged, so only the (unusably long) per-rate detail is folded.
_MAX_RATE_ROWS = 5


def _bill_rate_rows(channels, currency):
    """Render rate breakdown rows for a channel dict {rate: {kwh, cost}}.
    Returns (html, total_kwh, total_cost) where totals are sum of rounded per-rate
    values. When a channel has more than _MAX_RATE_ROWS distinct rates (Agile), the
    rows collapse to ONE weighted-average row — the totals are computed identically
    (sum of the same rounded per-rate values), so the Total row never moves."""
    rates = sorted(channels)
    total_kwh = 0.0
    total_cost = 0.0
    if len(rates) > _MAX_RATE_ROWS:
        for rate in rates:
            total_kwh  += round(channels[rate]["kwh"],  3)
            total_cost += round(channels[rate]["cost"], 2)
        avg = (total_cost / total_kwh) if total_kwh else 0.0   # kWh-weighted £/kWh
        cost_str = f"({-total_cost:.2f})" if total_cost < 0 else f"{total_cost:.2f}"
        rows = f"""
            <tr>
              <td></td><td>{avg:.4f} <span style="opacity:0.6;">(avg of {len(rates)} rates)</span></td>
              <td>{total_kwh:.3f}</td><td>{cost_str}</td>
            </tr>"""
        return rows, round(total_kwh, 3), round(total_cost, 2)
    rows = ""
    for rate in rates:
        d = channels[rate]
        kwh      = round(d["kwh"],  3)
        cost_val = round(d["cost"], 2)
        total_kwh  += kwh
        total_cost += cost_val
        cost_str = f"({-cost_val:.2f})" if cost_val < 0 else f"{cost_val:.2f}"
        rows += f"""
            <tr>
              <td></td><td>{rate:.4f}</td>
              <td>{kwh:.3f}</td><td>{cost_str}</td>
            </tr>"""
    return rows, round(total_kwh, 3), round(total_cost, 2)


def _bill_total_row(kwh, cost):
    cost_str = f"({-cost:.2f})" if cost < 0 else f"{cost:.2f}"
    return f"""
        <tr class="channel-total">
          <td>Total</td><td></td><td>{kwh:.3f}</td><td>{cost_str}</td>
        </tr>"""


# Rate keys within this gap (£/kWh) are the SAME tariff band jittered by Octopus's
# per-half-hour settlement rounding (e.g. 0.323091/0.323092/0.323097 → 0.3230/0.3231/
# 0.3232). Fold them into one displayed band. Sub-penny, so it never merges two real
# IOG bands (off-peak ≈5p vs peak ≈32p vs any day rate are pence apart) — and the
# cap-transition block is a separate 'mixed' bucket, never grouped by rate at all.
_SPLIT_BAND_EPS = 0.0015


def _bill_split_rows(summary, currency, exc=False):
    """BL-9: EV/Home breakdown rows for the IOG 'Import — total grid' section — the
    house-vs-car split Octopus itemises on the statement. EV and Home are interleaved
    per rate band (matching the bill); near-identical rates are collapsed into one band
    (see _SPLIT_BAND_EPS) so per-half-hour settlement jitter doesn't shatter a band into
    look-alike rows, exactly as the bill-method view already does. The boundary
    (cap-transition) blocks are a separate 'mixed' bucket → one EV and one Home row at
    their blended average, untouched by the collapse. The rate shown is cost ÷ kWh, so
    it's correct in both inc and (derived) ex-VAT bases.

    Returns an HTML string, or None when there's no EV in the period (the caller
    then shows the plain per-rate rows)."""
    ev_by_rate   = summary.get("ev_by_rate") or {}
    home_by_rate = summary.get("home_by_rate") or {}
    ev_tr        = summary.get("ev_transition") or {}
    home_tr      = summary.get("home_transition") or {}
    _ck  = "cost_exc" if exc else "cost"
    _ckr = _ck + "_raw"                       # B6: unrounded cost for the canonical rate
    if (sum(v["kwh"] for v in ev_by_rate.values()) + (ev_tr.get("kwh") or 0.0)) <= 1e-9:
        return None

    def _row(label, kwh, cost, note="", rate=None):
        # B1: EV and Home in the SAME collapsed band are the same tariff rate. Show one
        # shared per-band rate (passed in) rather than each row re-deriving cost/kWh, which
        # diverges on small-kWh bands from independent per-block cost rounding. Per-band, so
        # a genuine in-period rate change (> _SPLIT_BAND_EPS) is a separate band → separate
        # rows at their own rates. kWh + cost printed here are unchanged (still exact).
        if rate is None:
            rate = (cost / kwh) if kwh else 0.0
        cs = f"({-cost:.3f})" if cost < 0 else f"{cost:.3f}"   # 3dp, as the bill
        return (f'\n        <tr><td>{label}</td><td>{rate:.4f}{note}</td>'
                f'<td>{kwh:.3f}</td><td>{cs}</td></tr>')

    # Cluster the rate keys of BOTH series together into bands (adjacent rates within
    # _SPLIT_BAND_EPS merge), so EV and Home stay aligned on the same displayed band.
    _rates = sorted(set(ev_by_rate) | set(home_by_rate))
    _bands: list[list[float]] = []
    for r in _rates:
        if _bands and (r - _bands[-1][-1]) <= _SPLIT_BAND_EPS:
            _bands[-1].append(r)
        else:
            _bands.append([r])

    html = ""
    for band in _bands:
        ek = ec = hk = hc = 0.0        # kWh + displayed (3dp) cost
        ecr = hcr = 0.0                # B6: unrounded cost, for the canonical band rate
        for r in band:
            e = ev_by_rate.get(r)
            if e:
                ek += e["kwh"]; ec += e.get(_ck, 0.0); ecr += e.get(_ckr, e.get(_ck, 0.0))
            h = home_by_rate.get(r)
            if h:
                hk += h["kwh"]; hc += h.get(_ck, 0.0); hcr += h.get(_ckr, h.get(_ck, 0.0))
        # Shared band rate — kWh-weighted over BOTH series for THIS band only, so EV and
        # Home always read identically within a band and a real in-period rate change (its
        # own band) keeps its own rate. B6: derive it from the UNROUNDED cost so a small-kWh
        # band reads the canonical tariff rate (not the 3dp-rounded-cost ÷ kWh drift); the
        # displayed cost columns (ec/hc) stay 3dp.
        _band_rate = ((ecr + hcr) / (ek + hk)) if (ek + hk) > 1e-9 else 0.0
        if ek > 1e-9:
            html += _row("EV", ek, ec, rate=_band_rate)
        if hk > 1e-9:
            html += _row("Home", hk, hc, rate=_band_rate)
    _note = ' <span style="opacity:0.6;">(transition)</span>'
    if (ev_tr.get("kwh") or 0.0) > 1e-9:
        _r = (ev_tr.get(_ckr, ev_tr.get(_ck, 0.0)) / ev_tr["kwh"]) if ev_tr["kwh"] > 1e-9 else None
        html += _row("EV", ev_tr["kwh"], ev_tr.get(_ck, 0.0), _note, rate=_r)
    if (home_tr.get("kwh") or 0.0) > 1e-9:
        _r = (home_tr.get(_ckr, home_tr.get(_ck, 0.0)) / home_tr["kwh"]) if home_tr["kwh"] > 1e-9 else None
        html += _row("Home", home_tr["kwh"], home_tr.get(_ck, 0.0), _note, rate=_r)
    return html


def _collapse_rate_kwh(rate_kwh):
    """Side-panel rate breakdown collapse. `rate_kwh` is a {rate: kwh} mapping (the
    day chart's `summary_rates[meter]`). Returns a list of (kwh, rate, n) tuples to
    render: one per rate normally, or a SINGLE kWh-weighted-average tuple (with n =
    the rate count) when there are more than _MAX_RATE_ROWS non-trivial rates — the
    same threshold as the billing summary's _bill_rate_rows, so the side panel's
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


from decimal import Decimal as _Decimal, ROUND_HALF_EVEN as _ROUND_HALF_EVEN








def _bill_method_breakdown(blocks, period_vat=None, standing_inc_by_day=None):
    """BL-24: ex-VAT bill-method breakdown for a period — import energy by rate band, plus
    ex-VAT standing, a VAT row and the inc total. Matches how Octopus actually reconciles
    (verified against a real bill): the per-half-hour figures are summed RAW and rounding
    happens only at the SUBTOTAL — NOT per half-hour (that destroys the many sub-0.01 kWh
    peak slots) and NOT the 1dp kWh the bill shows for display. VAT is derived from a real
    inc/exc pair (snapped 0/5/20%; 0% once VAT is removed), else the default. Ex-VAT cost is
    the stored exc where captured, else the billed inc ÷ (1+VAT) — same for standing, so the
    standing charge still lands before ex-VAT capture. Returns a dict or None."""
    from collections import defaultdict as _dd
    blocks = list(blocks or [])
    # ── Pass 1: VAT rate from a real inc/exc pair (else default) ────────────────
    raw_vat = None
    for b in blocks:
        imp = (((b or {}).get("meters") or {}).get("electricity_main") or {})
        imp = (imp.get("channels") or {}).get("import") or {}
        c, ce = imp.get("cost"), imp.get("cost_exc")
        if c and ce and ce != 0:
            raw_vat = float(c) / float(ce) - 1.0
            break
    # VAT rate: derived from a real inc/exc pair where present; else the period's
    # statutory rate from the VAT calendar (passed in), NOT a hardcoded 5% — so a 0%/20%
    # period labels + fills correctly. The *amount* is computed as inc − exc below, which
    # is right regardless of the rate; this `vat` only sets the label and the fallbacks.
    _default_vat = 0.05 if period_vat is None else float(period_vat)
    vat = _default_vat if raw_vat is None else min((0.0, 0.05, 0.20),
                                                   key=lambda x: abs(x - raw_vat))
    # ── Pass 2: raw ex-VAT sums per band + ex-VAT standing per day ──────────────
    bands = _dd(lambda: {"kwh": 0.0, "exc": 0.0, "inc": 0.0})
    tot_kwh = with_exc = 0.0
    stand_exc_day = {}   # local day → ex-VAT standing (first non-zero; stored else inc/(1+VAT))
    for b in blocks:
        m = ((b or {}).get("meters") or {}).get("electricity_main") or {}
        imp = (m.get("channels") or {}).get("import") or {}
        d = (b.get("start") or "")[:10]
        if d and d not in stand_exc_day:
            se = m.get("standing_charge_exc")
            if se:
                stand_exc_day[d] = float(se)
            else:
                sc = m.get("standing_charge") or 0
                if sc:
                    stand_exc_day[d] = float(sc) / (1.0 + vat)
        kwh = imp.get("kwh") or 0
        if not kwh:
            continue
        tot_kwh += kwh
        inc_rate = imp.get("rate")
        if imp.get("cost_exc") is not None or imp.get("rate_exc") is not None:
            with_exc += kwh
        # Raw ex-VAT cost for this half-hour: the billed cost, ex-VAT.
        if imp.get("cost_exc") is not None:
            xc = float(imp["cost_exc"])
        elif imp.get("cost") is not None:
            xc = float(imp["cost"]) / (1.0 + vat)
        elif inc_rate is not None:
            xc = kwh * (float(inc_rate) / (1.0 + vat))
        else:
            continue
        # Inc cost for this slot — paired with xc so the VAT amount is inc − exc (exact
        # for any mix of rates across a period, e.g. a VAT-holiday boundary).
        if imp.get("cost") is not None:
            ic = float(imp["cost"])
        elif inc_rate is not None:
            ic = kwh * float(inc_rate)
        else:
            ic = xc * (1.0 + vat)
        # Band by the CLEAN inc-rate tariff band, NOT the per-slot derived exc rate
        # (cost_exc ÷ kWh). The derived rate jitters with Octopus's per-slot rounding and
        # shatters the two real bands into many near-duplicates (0.3075/0.3077/0.3078/…);
        # the stored inc rate is a clean tariff value, so grouping on it reproduces the
        # bill's band structure. We group on inc (not the exc rate) because Octopus stores
        # only the exc COST on measurement slots — imp_rate_exc is NULL there — so there's
        # no reliable exc-rate column to group on; inc↔exc are 1:1 (÷ (1+VAT)) so it's
        # equivalent. Fall back to an inc-scale key from the exc figures only when a slot
        # has no inc rate.
        if inc_rate is not None:
            band_key = round(float(inc_rate), 6)
        elif imp.get("rate_exc") is not None:
            band_key = round(float(imp["rate_exc"]) * (1.0 + vat), 6)
        else:
            # No inc rate / stored exc rate — derive an inc-scale key from this slot's
            # ex-VAT cost so it still bands consistently (xc is always set by here).
            band_key = round(xc / float(kwh) * (1.0 + vat), 6)
        band = bands[band_key]
        band["kwh"] += kwh
        band["exc"] += xc
        band["inc"] += ic
    if not bands:
        return None
    # B2: cluster adjacent band keys within _SPLIT_BAND_EPS before emitting rows. The band
    # key is the CLEAN inc rate where the tariff supplies one, but rate-from-cost history
    # (CSV import) derives it as cost÷kWh, which jitters (e.g. 0.070000 vs 0.070003 — the
    # same off-peak tariff) and would otherwise show as two identical-looking rows. Sub-penny
    # EPS, and PER-CLUSTER, so a genuine in-period rate change (≥ pence) stays its own row.
    _keys = sorted(bands)
    _clusters = []
    for _k in _keys:
        if _clusters and (_k - _clusters[-1][-1]) <= _SPLIT_BAND_EPS:
            _clusters[-1].append(_k)
        else:
            _clusters.append([_k])
    rows, energy_raw, energy_inc = [], 0.0, 0.0
    for _cl in _clusters:
        bk = sum(bands[k]["kwh"] for k in _cl)
        be = sum(bands[k]["exc"] for k in _cl)
        bi = sum(bands[k]["inc"] for k in _cl)
        energy_raw += be
        energy_inc += bi
        # Displayed exc rate = the cluster's kWh-weighted inc-scale rate ÷ (1+VAT) — a
        # deterministic ex-VAT label that matches the bill. Cost to 3dp (mills), like the
        # bill — the summed raw cost is authoritative.
        _wk = (sum(k * bands[k]["kwh"] for k in _cl) / bk) if bk > 1e-12 else _cl[0]
        rows.append({"rate_exc": round(_wk / (1.0 + vat), 4),
                     "kwh": round(bk, 3), "cost_exc": round(be, 3)})
    # Collapse a long rate list (Agile: hundreds of distinct half-hourly rates across a
    # bill period) into ONE kWh-weighted average row so the ex-VAT summary stays
    # readable. Uses the raw sums (energy_raw = Σ exc), so Total (exc) is unchanged.
    if len(rows) > _MAX_RATE_ROWS:
        _tot_kwh = sum(bands[k]["kwh"] for k in bands)
        _avg_exc = (energy_raw / _tot_kwh) if _tot_kwh else 0.0
        rows = [{"rate_exc": round(_avg_exc, 4), "kwh": round(_tot_kwh, 3),
                 "cost_exc": round(energy_raw, 3),
                 "collapsed": True, "n_rates": len(bands)}]
    # Standing: prefer the summary's per-LOCAL-day inc figure (correct day count across
    # the BST midnight boundary), ex-VAT via ÷ (1+VAT) — exact-to-the-mill for a flat
    # charge. Fall back to the block-derived exc standing when no summary is supplied.
    def _group_standing(day_amounts, to_exc):
        # {day: amount} → one row per DISTINCT daily rate, so a mid-period standing
        # change (price-cap / tariff switch) shows as separate lines instead of a
        # single averaged rate that matches neither. `to_exc(amount)` converts a
        # daily amount to its ex-VAT value.
        by_rate: dict = {}
        for _d, _amt in day_amounts.items():
            k = round(_amt, 6)
            g = by_rate.setdefault(k, [0, 0.0])
            g[0] += 1
            g[1] += _amt
        return [{"days": g[0], "rate_exc": round(to_exc(k), 4),
                 "cost_exc": round(to_exc(g[1]), 2)}
                for k, g in sorted(by_rate.items())]
    if standing_inc_by_day:
        standing_days = len(standing_inc_by_day)
        standing_inc_raw = sum(standing_inc_by_day.values())
        standing_raw  = standing_inc_raw / (1.0 + vat)
        standing_rows = _group_standing(standing_inc_by_day, lambda a: a / (1.0 + vat))
    else:
        standing_days = len(stand_exc_day)
        standing_raw  = sum(stand_exc_day.values())
        standing_inc_raw = standing_raw * (1.0 + vat)
        standing_rows = _group_standing(stand_exc_day, lambda a: a)   # already ex-VAT
    standing_rate = round(standing_raw / standing_days, 4) if standing_days else 0.0
    subtotal_raw  = energy_raw + standing_raw            # ex-VAT subtotal
    inc_raw       = energy_inc + standing_inc_raw        # inc-VAT subtotal
    subtotal = round(subtotal_raw, 2)
    # VAT amount as inc − exc (a subtraction) — exact for any mix of rates across the
    # period, so a VAT-holiday boundary inside a bill period is handled correctly.
    vat_amount = round(inc_raw - subtotal_raw, 2)
    return {
        "rows": rows,
        "energy_exc":        round(energy_raw, 2),
        "standing_days":     standing_days,
        "standing_rate_exc": standing_rate,
        "standing_rows":     standing_rows,
        "standing_exc":      round(standing_raw, 2),
        "subtotal_exc":      subtotal,
        "vat_rate":          vat,
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


def render_billing_summary(summary, currency='£', site_name=None):
    if not summary:
        return ""

    meter_meta       = summary.get("meter_meta", {})
    main_import_raw  = summary.get("main_import_raw", {})
    main_reads       = summary.get("main_import_reads", {})

    # ── Site header ──
    if not site_name:
        site_name = next(
            (m.get("site") for m in meter_meta.values() if m.get("site") and not m.get("is_submeter")),
            None
        )
    site_header = f'''
        <tr class="bill-site-header">
          <td colspan="4"><span class="censored">{site_name}</span></td>
        </tr>''' if site_name else ""

    html = f'''
    <div class="billing-summary">
      <table class="billing-table">
        {site_header}
    '''

    # ── Separate export, main import remainder, and sub-meter imports ──
    export_keys    = sorted(k for k in summary["meters"] if k.endswith("/ Export"))
    remainder_keys = sorted(k for k in summary["meters"]
                            if k.endswith("/ Import")
                            and not (summary["totals"].get(k) or {}).get("is_submeter"))
    submeter_keys  = sorted(k for k in summary["meters"]
                            if k.endswith("/ Import")
                            and (summary["totals"].get(k) or {}).get("is_submeter"))

    # ── Export section(s) ──
    for meter_name in export_keys:
        channels   = summary["meters"][meter_name]
        totals     = summary["totals"].get(meter_name, {})
        meta       = meter_meta.get(meter_name, {})
        read_start = totals.get("read_start")
        read_end   = totals.get("read_end")
        mpan_html  = (f'&nbsp;&nbsp;|&nbsp;&nbsp;MPAN: <span class="censored">{meta["mpan"]}</span>'
                      if meta.get("mpan") else "")
        reads_html = ""
        if read_start is not None:
            read_total = (read_end or 0.0) - (read_start or 0.0)
            reads_html = (f'<br><span class="reads">'
                          f'Start: <span class="censored">{read_start:.3f}</span>'
                          f'&nbsp;&nbsp;End: <span class="censored">{read_end:.3f}</span>'
                          f'&nbsp;&nbsp;Total: {read_total:.3f} kWh</span>')
        html += f"""
        <tr class="channel-title">
          <td colspan="4">Export{mpan_html}{reads_html}</td>
        </tr>
        <tr class="channel-header">
          <td></td><td>Rate ({currency}/kWh)</td><td>kWh</td><td>Cost ({currency})</td>
        </tr>"""
        html += _bill_rate_rows(channels, currency)[0]
        html += _bill_total_row(totals["kwh"], totals["cost"])
    # BL-24: when "Bill Rounding" is on, the import charge is rendered EX-VAT (the bill
    # method) IN PLACE OF the inc-VAT display; otherwise the usual inc-VAT rows.
    _bm = summary.get("bill_method")
    _exc_lbl = ", exc" if _bm else ""
    if remainder_keys or submeter_keys:
        # Build raw totals from main_import_raw
        raw_kwh  = sum(d["kwh"]  for d in main_import_raw.values())
        raw_cost = sum(d["cost"] for d in main_import_raw.values())

        # Reads from main meter
        read_start = main_reads.get("read_start")
        read_end   = main_reads.get("read_end")
        reads_html = ""
        if read_start is not None:
            read_total = (read_end or 0.0) - (read_start or 0.0)
            reads_html = (f'<br><span class="reads">'
                          f'Start: <span class="censored">{read_start:.3f}</span>'
                          f'&nbsp;&nbsp;End: <span class="censored">{read_end:.3f}</span>'
                          f'&nbsp;&nbsp;Total: {read_total:.3f} kWh</span>')

        # MPAN from first remainder key meta
        mpan_html = ""
        if remainder_keys:
            _meta = meter_meta.get(remainder_keys[0], {})
            if _meta.get("mpan"):
                mpan_html = f'&nbsp;&nbsp;|&nbsp;&nbsp;MPAN: <span class="censored">{_meta["mpan"]}</span>'

        html += f"""
        <tr class="channel-title">
          <td colspan="4">Import — total grid{mpan_html}{reads_html}</td>
        </tr>
        <tr class="channel-header">
          <td></td><td>Rate ({currency}/kWh{_exc_lbl})</td><td>kWh</td><td>Cost ({currency}{_exc_lbl})</td>
        </tr>"""
        if _bm:
            # BL-24: the ex-VAT bill method REPLACES the inc-VAT import display when Bill
            # Rounding is on — per-rate ex-VAT (3dp, as the bill), Total (exc), the ex-VAT
            # standing charge, a VAT row, then the inc-VAT total. Octopus builds a bill this
            # way: sum the raw ex-VAT half-hours, round at the subtotal, then add VAT. The
            # Total Bill below is UNCHANGED — this only changes how the import is presented.
            # BL-9: on an IOG tariff show the EV/Home split (ex-VAT, derived from each
            # block's own exc/inc ratio) in place of the plain per-rate rows; the
            # Total (exc) below is the authoritative bill-method figure, unchanged.
            _split_html = _bill_split_rows(summary, currency, exc=True)
            if _split_html is not None:
                html += _split_html
            else:
                for _r in _bm["rows"]:
                    _rate_cell = (f"{_r['rate_exc']:.4f} <span style=\"opacity:0.6;\">(avg of {_r['n_rates']} rates)</span>"
                                  if _r.get("collapsed") else f"{_r['rate_exc']:.4f}")
                    html += f"""
        <tr><td></td><td>{_rate_cell}</td><td>{_r['kwh']:.3f}</td><td>{_r['cost_exc']:.3f}</td></tr>"""
            html += f"""
        <tr class="channel-total"><td>Total (exc)</td><td></td><td></td><td>{_bm['energy_exc']:.2f}</td></tr>"""
            if _bm["standing_days"]:
                _srows = _bm.get("standing_rows") or []
                if len(_srows) > 1:
                    # Standing-charge rate changed mid-period (price cap / tariff
                    # switch) — one line per rate, not a single averaged rate.
                    for _s in _srows:
                        html += f"""
        <tr class="standing"><td colspan="3">Standing charge (exc): {_s['days']} days @ {currency}{_s['rate_exc']:.4f}/day</td><td>{_s['cost_exc']:.2f}</td></tr>"""
                else:
                    html += f"""
        <tr class="standing"><td colspan="3">Standing charge (exc): {_bm['standing_days']} days @ {currency}{_bm['standing_rate_exc']:.4f}/day</td><td>{_bm['standing_exc']:.2f}</td></tr>"""
            html += f"""
        <tr class="bill-method"><td colspan="3">VAT @ {_bm['vat_rate'] * 100:.0f}%</td><td>{currency}{_bm['vat_amount']:.2f}</td></tr>
        <tr class="channel-total"><td colspan="3">Total incl. VAT</td><td>{currency}{_bm['inc_total']:.2f}</td></tr>"""
        else:
            _rate_html, raw_kwh_r, raw_cost_r = _bill_rate_rows(main_import_raw, currency)
            # BL-9: on an IOG tariff show the EV/Home split (as Octopus does) in place
            # of the plain per-rate rows; the Total is unchanged (the split sums to it).
            _split_html = _bill_split_rows(summary, currency)
            html += _split_html if _split_html is not None else _rate_html
            html += _bill_total_row(raw_kwh_r, raw_cost_r)

            # ── Standing charge folded into the import charge (display only) —
            # mirrors a real bill, where energy + standing make up the import total.
            # The kWh Total above is unchanged, and summary['total_cost'] (the Total
            # Bill) plus Usage Stats are computed elsewhere, so nothing here moves a
            # number — this only adds a cost-inclusive subtotal to the display.
            if summary["standing"]:
                _sc_groups = {}
                for _d, _amt in sorted(summary["standing"].items()):
                    _r = round(_amt, 4)
                    _sc_groups[_r] = _sc_groups.get(_r, 0) + 1
                for _r, _cnt in sorted(_sc_groups.items()):
                    html += f"""
        <tr class="standing">
          <td colspan="3">Standing charge: {_cnt} days @ {currency}{_r:.4f}/day</td>
          <td>{_r * _cnt:.2f}</td>
        </tr>"""
                _incl = raw_cost_r + round(sum(summary["standing"].values()), 2)
                _incl_str = f"({-_incl:.2f})" if _incl < 0 else f"{_incl:.2f}"
                html += f"""
        <tr class="channel-total">
          <td colspan="3">Total incl. standing charge</td><td>{_incl_str}</td>
        </tr>"""

        # ── Sub-meter breakdown (indented) ──
        if submeter_keys or remainder_keys:
            html += '''
        <tr class="submeter-breakdown-header">
          <td colspan="4">Breakdown by meter</td>
        </tr>'''

            # House remainder first
            for meter_name in remainder_keys:
                channels = summary["meters"][meter_name]
                totals   = summary["totals"].get(meter_name, {})
                html += f"""
        <tr class="channel-title submeter-indent">
          <td colspan="4">Direct import</td>
        </tr>
        <tr class="channel-header submeter-indent">
          <td></td><td>Rate ({currency}/kWh)</td><td>kWh</td><td>Cost ({currency})</td>
        </tr>"""
                _rate_html, tot_kwh_r, tot_cost_r = _bill_rate_rows(channels, currency)
                html += _rate_html
                html += _bill_total_row(tot_kwh_r, tot_cost_r)
            for meter_name in submeter_keys:
                channels = summary["meters"][meter_name]
                totals   = summary["totals"].get(meter_name, {})
                meta     = meter_meta.get(meter_name, {})
                label    = meta.get("device") or meter_name.split(" / ")[0].replace("(Sub-meter)", "").replace("(sub-meter)", "").strip()
                html += f"""
        <tr class="channel-title submeter-indent">
          <td colspan="4">{label}</td>
        </tr>
        <tr class="channel-header submeter-indent">
          <td></td><td>Rate ({currency}/kWh)</td><td>kWh</td><td>Cost ({currency})</td>
        </tr>"""
                _rate_html, tot_kwh_r, tot_cost_r = _bill_rate_rows(channels, currency)
                html += _rate_html
                html += _bill_total_row(tot_kwh_r, tot_cost_r)
    # Standing charge for export-only accounts (no import section above, so it
    # would otherwise not appear); the import case renders it in-section.
    if summary["standing"] and not (remainder_keys or submeter_keys):
        rate_groups = {}
        for day_date, amount in sorted(summary["standing"].items()):
            rate = round(amount, 4)
            rate_groups[rate] = rate_groups.get(rate, 0) + 1
        for rate, count in sorted(rate_groups.items()):
            html += f"""
        <tr class="standing">
          <td colspan="3">Standing Charge: {count} days @ {currency}{rate:.4f}/day</td>
          <td>{rate * count:.2f}</td>
        </tr>"""

    html += f"""
        <tr class="grand-total">
          <td colspan="3">Total Bill</td>
          <td>{currency}{summary['total_cost']:.2f}</td>
        </tr>"""

    html += """
      </table>
    </div>"""

    return html


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


def build_day_chart_html(day, day_blocks, meter_colors, chart_prefix='', block_minutes=30, currency='£', site_name=None, ev_slot_map=None, bill_rounding=False, fallback_vat=0.05, ev_fold_meter=None, ev_label=None, cap_from=None, import_schedule=None, store=None, timezone_name='UTC'):
    """The pre-rendered page's markup for one day, formatted from day_chart_data."""
    d = day_chart_data(day, day_blocks, meter_colors, block_minutes=block_minutes, currency=currency,
                       ev_slot_map=ev_slot_map, bill_rounding=bill_rounding, fallback_vat=fallback_vat,
                       ev_fold_meter=ev_fold_meter, ev_label=ev_label, cap_from=cap_from,
                       import_schedule=import_schedule, store=store, timezone_name=timezone_name)
    chart_data, panel = d["chart"], d["panel"]

    def cs(text, color, size="0.9em", bold=False):
        w = "font-weight:600;" if bold else ""
        return f'<span style="color:{color};font-size:{size};line-height:1.6;white-space:nowrap;{w}">{text}</span>'

    def rate_rows_colored(rates, color):
        out = ""
        for kwh, rate, n in rates:
            suffix = f' (avg of {n})' if n else ''
            out += cs(f'{kwh:.3f} kWh @ {currency}{rate:.4f}{suffix}', color, size="0.8em")
        return out

    main_color, export_color = panel["main_color"], panel["export_color"]
    totals_html = ''
    if panel["total_import"] > 0:
        totals_html += cs(f'Total import: {panel["total_import"]:.3f} kWh', main_color)
        totals_html += cs(f'Import cost: {currency}{panel["import_cost"]:.2f}', main_color)
        if panel["import_cost_exc"] is not None:
            totals_html += cs(f'Import cost (exc VAT): {currency}{panel["import_cost_exc"]:.2f}',
                              main_color)
    if panel["export_kwh"] > 0:
        totals_html += cs(f'Total export: {panel["export_kwh"]:.3f} kWh', export_color)
        totals_html += cs(f'Export credit: {currency}{panel["export_cost"]:.2f}', export_color)
        totals_html += rate_rows_colored(panel["export_rates"], export_color)

    breakdown_cols = []
    for row in panel["breakdown"]:
        col  = '<div class="scol">'
        col += cs(f'↳ {row["label"]}', row["color"], size="1em", bold=True)
        col += cs(f'{row["kwh"]:.3f} kWh', row["color"], size="1em")
        col += cs(f'{currency}{row["cost"]:.2f}', row["color"], size="1em")
        col += rate_rows_colored(row["rates"], row["rate_color"])
        col += '</div>'
        breakdown_cols.append(col)

    breakdown_html = f'<div class="scols">{"".join(breakdown_cols)}</div>' if breakdown_cols else ''

    summary_html = (
        f'<div class="chart-summary">'
        f'<div class="day-label">{day}</div>'
        f'<div class="stotals">{totals_html}</div>'
        + (f'<div class="sdivider"></div>' if breakdown_cols else '')
        + breakdown_html
        + '</div>'
    )

    chart_data_json = json.dumps(chart_data, separators=(',', ':'))

    chart_id      = f"{chart_prefix}chart_{day.replace('-', '_')}"
    chart_id_safe = chart_id.replace('-', '_')
    table_id      = f"tbl_{chart_id_safe}"

    return f"""
<div class="day-chart-wrap">
  {summary_html}
  <div id="{chart_id}" class="chart-container"></div>
  <div id="{table_id}" class="day-data-tables" style="display:none;"></div>
  <div class="day-tbl-toolbar">
    <button class="day-tbl-toggle" onclick="toggleDayTables('{table_id}',this)">&#9776; Data</button>
  </div>
  <script type="application/json" id="data_{chart_id}">{chart_data_json}</script>
  <script>
  (function() {{
    if (!window._pendingCharts) window._pendingCharts = {{}};
    window._pendingCharts['{chart_id}'] = '{chart_id}';
  }})();
  </script>
</div>
"""




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
        _bm = _bill_method_breakdown(period_blocks, period_vat=vat_at(ctx, period_start),
                                     standing_inc_by_day=_sm.get("standing"))
        if _bm:
            _sm["bill_method"] = _bm
    return _sm


# ─────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────

def generate_daily_import_export_charts(blocks, timezone_name="UTC", block_minutes=None, currency='£', cfg=None, store=None, cap_from=None, import_schedule=None):

    if not blocks:
        return "<html><body><p>No data available.</p></body></html>"

    try:
        _tz = ZoneInfo(timezone_name)
    except Exception:
        _tz = ZoneInfo("UTC")
    
# Use passed block_minutes, or derive from block meter meta, or default to 30
    if block_minutes is None:
        block_minutes = 30
        for b in blocks:
            bm = (((b or {}).get("meters") or {})
                  .get("electricity_main") or {})
            bm = (bm.get("meta") or {}).get("block_minutes")
            if bm:
                block_minutes = int(bm)
                break
    slots = 1440 // block_minutes
    today = datetime.now(tz=_tz).date()

    days_map = group_blocks_by_day(blocks, _tz, block_minutes)

    ctx = billing_context(cfg, store, blocks)
    meter_colors     = ctx["meter_colors"]
    _ev_phys_id      = ctx["ev_phys_id"]
    _ev_slot_map     = ctx["ev_slot_map"]
    _ev_fold_devices = ctx["ev_fold_devices"]
    _ev_label        = ctx["ev_label"]
    _ev_day_map      = ctx["ev_day_map"]

    # ── Billing periods ──
    # Use historically correct billing_day per block from config_periods join.
    # Falls back to reading from meters_config.json / block meta for legacy data.
    try:
        from energy_engine_io import load_json as _load_json
        import os as _os
        _cfg = _load_json("/data/energy_meter_tracker/meters_config.json", {})
        _main_meta = {}
        for _md in _cfg.get("meters", {}).values():
            if not (_md.get("meta") or {}).get("sub_meter"):
                _main_meta = _md.get("meta") or {}
                break
        site_name = _main_meta.get("site") or None
    except Exception:
        site_name = None

    periods = get_billing_periods_from_config_history(blocks, tz=_tz)
    # billing_day for display: use current config value (most recent period)
    try:
        billing_day = int(_main_meta.get("billing_day") or 1)
    except Exception:
        billing_day = 1

    # Pre-index blocks by date string for fast per-period lookup
    _blocks_by_date = defaultdict(list)
    for _b in blocks:
        if _b and _b.get("start"):
            try:
                _bd = datetime.fromisoformat(_b["start"]).replace(tzinfo=ZoneInfo("UTC")).astimezone(_tz)
                _blocks_by_date[_bd.date().isoformat()].append(_b)
            except Exception:
                pass

    def _blocks_for_period(p_start, p_end):
        """Return only the blocks that fall within [p_start, p_end)."""
        result = []
        d = p_start.date()
        while d < p_end.date():
            result.extend(_blocks_by_date.get(d.isoformat(), []))
            from datetime import timedelta as _td
            d += _td(days=1)
        return result

    _bill_rounding = ctx["bill_rounding"]

    def _vat_at(_when):
        return vat_at(ctx, _when)

    def _summ(_blks_for_calc, _s, _e):
        return period_summary(ctx, _blks_for_calc, _blocks_for_period(_s, _e), _s, _e,
                              store=store, tz_name=timezone_name)

    # ── Build per-period data ──
    period_sections = []   # list of dicts

    for i, (p_start, p_end) in enumerate(periods):
        period_blocks = _blocks_for_period(p_start, p_end)
        summary   = _summ(period_blocks, p_start, p_end)
        is_current = p_start.date() <= today < p_end.date()
        is_prev    = (len(periods) > 1) and (i == len(periods) - 2) and not is_current

        # days that belong to this period
        period_days = sorted(
            [d for d in days_map if p_start.date().isoformat() <= d < p_end.date().isoformat()],
            reverse=True
        )

        # Detect config-change boundary — period starts on a non-billing-day
        # meaning it was created by a config change mid-cycle, not a natural boundary
        _is_config_change = (
            i > 0 and
            p_start.day != billing_day
        )
        period_sections.append({
            "index":          i,
            "start":          p_start,
            "end":            p_end,
            "summary":        summary,
            "is_current":     is_current,
            "is_prev":        is_prev,
            "is_config_change": _is_config_change,
            "days":           period_days,
        })

    # Filter out periods that have no blocks at all (e.g. tiny config-change slivers
    # that fall between billing day and the config change date)
    period_sections = [ps for ps in period_sections if ps["days"] or ps["is_current"]]

    # Re-assign is_prev after filtering (second-to-last non-current period)
    non_current = [ps for ps in period_sections if not ps["is_current"]]
    if non_current:
        non_current[-1]["is_prev"] = True

    # Sort periods newest-first for display
    period_sections_display = list(reversed(period_sections))

    # ── Quarter periods ──
    quarter_sections = []
    for i, (q_start, q_end) in enumerate(get_all_quarter_periods(blocks, tz=_tz)):
        quarter_blocks = _blocks_for_period(q_start, q_end)
        summary    = _summ(quarter_blocks, q_start, q_end)
        is_current = q_start.date() <= today < q_end.date()
        q_num      = (q_start.month - 1) // 3 + 1
        quarter_sections.append({
            "index":      i,
            "start":      q_start,
            "end":        q_end,
            "summary":    summary,
            "is_current": is_current,
            "label":      f"Q{q_num} {q_start.year}",
            "days":       sorted([d for d in days_map
                                  if q_start.date().isoformat() <= d < q_end.date().isoformat()],
                                 reverse=True),
        })
    quarter_sections_display = list(reversed(quarter_sections))
    quarter_by_index = {qs["index"]: qs for qs in quarter_sections}

    # ── Year periods ──
    year_sections = []
    for i, (y_start, y_end) in enumerate(get_all_year_periods(blocks, tz=_tz)):
        summary    = _summ(blocks, y_start, y_end)
        is_current = y_start.date() <= today < y_end.date()
        year_sections.append({
            "index":      i,
            "start":      y_start,
            "end":        y_end,
            "summary":    summary,
            "is_current": is_current,
            "label":      str(y_start.year),
            "days":       sorted([d for d in days_map
                                  if y_start.date().isoformat() <= d < y_end.date().isoformat()],
                                 reverse=True),
        })
    year_sections_display = list(reversed(year_sections))
    year_by_index = {ys["index"]: ys for ys in year_sections}

    # ── Calendar month periods ──
    calmonth_sections = []
    for i, (cm_start, cm_end) in enumerate(get_all_calmonth_periods(blocks, tz=_tz)):
        summary    = _summ(blocks, cm_start, cm_end)
        is_current = cm_start.date() <= today < cm_end.date()
        calmonth_sections.append({
            "index":      i,
            "start":      cm_start,
            "end":        cm_end,
            "summary":    summary,
            "is_current": is_current,
            "label":      cm_start.strftime("%b %Y"),
            "days":       sorted([d for d in days_map
                                  if cm_start.date().isoformat() <= d < cm_end.date().isoformat()],
                                 reverse=True),
        })
    calmonth_sections_display = list(reversed(calmonth_sections))
    calmonth_by_index = {cs["index"]: cs for cs in calmonth_sections}

    # ── Dropdown options (month) ──
    dropdown_options = []
    for ps in period_sections_display:
        s_str = ps["start"].strftime("%d %b %Y")
        e_str = (ps["end"] - timedelta(seconds=1)).strftime("%d %b %Y")
        cost  = ps["summary"]["total_cost"]
        label = f"{s_str} → {e_str}  |  {currency}{cost:.2f}"
        if ps["is_current"]:
            label = "★ Current  " + label
        dropdown_options.append(f'<option value="period_{ps["index"]}">{label}</option>')

    # ── Dropdown options (calmonth) ──
    calmonth_options = []
    for cs in calmonth_sections_display:
        cost  = cs["summary"]["total_cost"]
        label = f"{cs['label']}  |  {currency}{cost:.2f}"
        if cs["is_current"]:
            label = "★ Current  " + label
        calmonth_options.append(f'<option value="calmonth_{cs["index"]}">{label}</option>')

    # ── Dropdown options (quarter) ──
    quarter_options = []
    for qs in quarter_sections_display:
        cost  = qs["summary"]["total_cost"]
        label = f"{qs['label']}  |  {currency}{cost:.2f}"
        if qs["is_current"]:
            label = "★ Current  " + label
        quarter_options.append(f'<option value="quarter_{qs["index"]}">{label}</option>')

    # ── Dropdown options (year) ──
    year_options = []
    for ys in year_sections_display:
        cost  = ys["summary"]["total_cost"]
        label = f"{ys['label']}  |  {currency}{cost:.2f}"
        if ys["is_current"]:
            label = "★ Current  " + label
        year_options.append(f'<option value="year_{ys["index"]}">{label}</option>')

    dropdown_html = f"""
<div class="period-nav">
  <div class="nav-left">
    <div class="period-mode-toggle">
      <span class="period-mode-label">Period:</span>
      <button class="pmode-btn active" data-mode="month"    onclick="setPeriodMode('month')">Bill</button>
      <button class="pmode-btn"        data-mode="calmonth" onclick="setPeriodMode('calmonth')">Month</button>
      <button class="pmode-btn"        data-mode="quarter"  onclick="setPeriodMode('quarter')">Quarter</button>
      <button class="pmode-btn"        data-mode="year"     onclick="setPeriodMode('year')">Year</button>
    </div>
    <span class="period-stepper">
      <button class="pstep-btn" title="Older period" onclick="_billStep(1)">&#8249;</button>
      <span id="period-rolodex-label" title="Scroll or drag to spin, or click to pick a period"></span>
      <button class="pstep-btn" title="Newer period" onclick="_billStep(-1)">&#8250;</button>
    </span>
    <div class="period-select-wrap" id="select-month">
      <label for="period-select-month" style="font-size:11px;">Billing Period:</label>
      <select id="period-select-month" onchange="showPeriod(this.value, 'month')">
        {chr(39)+chr(39).join(dropdown_options)}
      </select>
    </div>
    <div class="period-select-wrap" id="select-calmonth" style="display:none;">
      <label for="period-select-calmonth" style="font-size:11px;">Month:</label>
      <select id="period-select-calmonth" onchange="showPeriod(this.value, 'calmonth')">
        {chr(39)+chr(39).join(calmonth_options)}
      </select>
    </div>
    <div class="period-select-wrap" id="select-quarter" style="display:none;">
      <label for="period-select-quarter" style="font-size:11px;">Quarter:</label>
      <select id="period-select-quarter" onchange="showPeriod(this.value, 'quarter')">
        {chr(39)+chr(39).join(quarter_options)}
      </select>
    </div>
    <div class="period-select-wrap" id="select-year" style="display:none;">
      <label for="period-select-year" style="font-size:11px;">Year:</label>
      <select id="period-select-year" onchange="showPeriod(this.value, 'year')">
        {chr(39)+chr(39).join(year_options)}
      </select>
    </div>
  </div>
  <div id="sticky-bill-strip">
    <span id="sticky-bill-label"></span>
    <button id="sticky-bill-btn" onclick="stickyBillExpand()">&#8593; Show Bill</button>
  </div>
  <div class="nav-right view-toggle">
    <button class="view-btn active" data-view="vanilla" onclick="showView('vanilla')">Bill</button>
    <button class="view-btn" data-view="vs-prev"        onclick="showView('vs-prev')">vs Prev</button>
    <button class="view-btn vs-year-btn" data-view="vs-year" onclick="showView('vs-year')">vs Last Year</button>
    <button class="view-btn censor-btn" id="censor-toggle" onclick="toggleCensor()" title="Blur sensitive info">&#128065; Censor</button>
    <button class="view-btn" id="sort-toggle" onclick="toggleSortOrder()" title="Toggle period order">↓ Latest first</button>
    <button class="view-btn" id="expand-all-btn" onclick="toggleAllTables()" title="Show or hide all data tables">Show Data</button>
  </div>
</div>"""

    # ── Build period lookup for comparison ──
    period_by_index = {ps["index"]: ps for ps in period_sections}

    def find_prev_period(ps):
        return period_by_index.get(ps["index"] - 1)

    def find_year_period(ps):
        target = ps["start"].replace(year=ps["start"].year - 1)
        best, best_delta = None, timedelta(days=36)
        for other in period_by_index.values():
            if other["index"] == ps["index"]:
                continue
            delta = abs(other["start"] - target)
            if delta < best_delta:
                best_delta, best = delta, other
        return best

    # ── Period section HTML ──
    sections_html_parts = []

    for ps in period_sections_display:
        pid         = f"period_{ps['index']}"
        is_current  = ps["is_current"]
        is_prev     = ps["is_prev"]
        charts_open = is_current or is_prev

        s_str      = ps["start"].strftime("%d %b %Y")
        e_str      = (ps["end"] - timedelta(seconds=1)).strftime("%d %b %Y")
        bill_total = ps["summary"]["total_cost"]
        ph         = f"{s_str} &rarr; {e_str}"   # period heading shorthand

        # Current period bill HTML (with optional current-period highlight class)
        cur_bill = render_billing_summary(ps["summary"], currency=currency, site_name=site_name)
        extra    = " current-period" if is_current else ""
        cur_bill = cur_bill.replace('<div class="billing-summary">',
                                    f'<div class="billing-summary{extra}">', 1)

        def col(heading, bill_html, is_compare=False):
            cls = "bill-view-heading compare" if is_compare else "bill-view-heading"
            return f'<div class="bill-compare-col"><div class="{cls}"><h2>{heading}</h2></div>{bill_html}</div>'

        def empty_col(msg):
            return f'<div class="bill-compare-col bill-compare-empty"><p>{msg}</p></div>'

        # Vanilla
        vanilla_html = f'<div class="bill-compare-wrap">{col(ph, cur_bill)}</div>'

        # vs Previous
        prev_ps = find_prev_period(ps)
        if prev_ps:
            prev_h    = f"{prev_ps['start'].strftime('%d %b %Y')} &rarr; {(prev_ps['end'] - timedelta(seconds=1)).strftime('%d %b %Y')}"
            prev_bill = render_billing_summary(prev_ps["summary"], currency=currency, site_name=site_name)
            vs_prev_html = f'<div class="bill-compare-wrap">{col(ph, cur_bill)}{col(prev_h, prev_bill, True)}</div>'
        else:
            vs_prev_html = f'<div class="bill-compare-wrap">{col(ph, cur_bill)}{empty_col("No previous period available.")}</div>'

        # vs Last Year
        year_ps = find_year_period(ps)
        if year_ps:
            year_h    = f"{year_ps['start'].strftime('%d %b %Y')} &rarr; {(year_ps['end'] - timedelta(seconds=1)).strftime('%d %b %Y')}"
            year_bill = render_billing_summary(year_ps["summary"], currency=currency, site_name=site_name)
            vs_year_html = f'<div class="bill-compare-wrap">{col(ph, cur_bill)}{col(year_h, year_bill, True)}</div>'
        else:
            vs_year_html = f'<div class="bill-compare-wrap">{col(ph, cur_bill)}{empty_col("No data for same period last year.")}</div>'

        # Daily charts
        day_charts_html = ""
        for day in ps["days"]:
            day_charts_html += build_day_chart_html(day, days_map[day], meter_colors, block_minutes=block_minutes, currency=currency, site_name=site_name, ev_slot_map=_ev_day_map, bill_rounding=_bill_rounding, fallback_vat=_vat_at(day), ev_fold_meter=_ev_phys_id, ev_label=("EV" if _ev_phys_id else None), cap_from=cap_from, import_schedule=import_schedule, store=store, timezone_name=timezone_name)

        open_attr    = "open" if charts_open else ""
        toggle_label = f"Daily Charts &mdash; {ph} &nbsp;|&nbsp; {currency}{bill_total:.2f}"

        config_change_banner = ""
        if ps.get("is_config_change"):
            config_change_banner = f"""
<div style="display:flex;align-items:center;gap:10px;padding:8px 14px;margin-bottom:12px;
            background:rgba(124,106,247,0.08);border:1px solid rgba(124,106,247,0.3);
            border-radius:6px;font-size:12px;color:#7c6af7;">
  ⚙️ <strong>Configuration changed</strong> &mdash; new billing period started {ps['start'].strftime('%-d %b %Y')}
</div>"""

        sections_html_parts.append(f"""
<div class="period-section month-section" id="{pid}" style="display:none;">
  {config_change_banner}
  <details class="bill-toggle" open>
    <summary class="bill-toggle-summary">Bill Summary &mdash; {ph} &nbsp;|&nbsp; {currency}{bill_total:.2f}</summary>
    <div class="bill-toggle-body">
      <div class="bill-view" data-view="vanilla">{vanilla_html}</div>
      <div class="bill-view" data-view="vs-prev" style="display:none;">{vs_prev_html}</div>
      <div class="bill-view" data-view="vs-year" style="display:none;">{vs_year_html}</div>
    </div>
  </details>
  <details class="day-charts-toggle" {open_attr}>
    <summary class="day-charts-summary">{toggle_label}</summary>
    <div class="day-charts-body">
      {day_charts_html}
    </div>
  </details>
</div>
""")

    # ── Grouped section builder (quarters + years) ──
    def build_grouped_section(gs, pid_prefix, find_prev_fn, find_year_fn, show_year_btn=True):
        pid        = f"{pid_prefix}_{gs['index']}"
        ph         = gs["label"]
        bill_total = gs["summary"]["total_cost"]

        cur_bill = render_billing_summary(gs["summary"], currency=currency, site_name=site_name)
        extra    = " current-period" if gs["is_current"] else ""
        cur_bill = cur_bill.replace('<div class="billing-summary">',
                                    f'<div class="billing-summary{extra}">', 1)

        def col(heading, bill_html, is_compare=False):
            cls = "bill-view-heading compare" if is_compare else "bill-view-heading"
            return f'<div class="bill-compare-col"><div class="{cls}"><h2>{heading}</h2></div>{bill_html}</div>'

        def empty_col(msg):
            return f'<div class="bill-compare-col bill-compare-empty"><p>{msg}</p></div>'

        vanilla_html = f'<div class="bill-compare-wrap">{col(ph, cur_bill)}</div>'

        prev_gs = find_prev_fn(gs)
        if prev_gs:
            prev_bill    = render_billing_summary(prev_gs["summary"], currency=currency, site_name=site_name)
            vs_prev_html = f'<div class="bill-compare-wrap">{col(ph, cur_bill)}{col(prev_gs["label"], prev_bill, True)}</div>'
        else:
            vs_prev_html = f'<div class="bill-compare-wrap">{col(ph, cur_bill)}{empty_col("No previous period available.")}</div>'

        if show_year_btn:
            year_gs = find_year_fn(gs)
            if year_gs:
                year_bill    = render_billing_summary(year_gs["summary"], currency=currency, site_name=site_name)
                vs_year_html = f'<div class="bill-compare-wrap">{col(ph, cur_bill)}{col(year_gs["label"], year_bill, True)}</div>'
            else:
                vs_year_html = f'<div class="bill-compare-wrap">{col(ph, cur_bill)}{empty_col("No data for same period last year.")}</div>'
        else:
            vs_year_html = f'<div class="bill-compare-wrap">{col(ph, cur_bill)}{empty_col("Not available in year view.")}</div>'

        day_charts_html = ""
        for day in gs["days"]:
            day_charts_html += build_day_chart_html(day, days_map[day], meter_colors, chart_prefix=f"{pid_prefix}_", block_minutes=block_minutes, currency=currency, site_name=site_name, ev_slot_map=_ev_day_map, bill_rounding=_bill_rounding, fallback_vat=_vat_at(day), ev_fold_meter=_ev_phys_id, ev_label=("EV" if _ev_phys_id else None), cap_from=cap_from, import_schedule=import_schedule, store=store, timezone_name=timezone_name)

        toggle_label = f"Daily Charts &mdash; {ph} &nbsp;|&nbsp; {currency}{bill_total:.2f}"
        # Quarter/year sections can hold 90–365 day charts; default the panel
        # collapsed (summary-first) so switching to those modes is instant. The
        # charts build lazily when the panel is expanded (see _renderSection).
        open_attr    = ""
        return (
            f'<div class="period-section {pid_prefix}-section" id="{pid}" style="display:none;">'
            f'<details class="bill-toggle" open>'
            f'<summary class="bill-toggle-summary">Bill Summary &mdash; {ph} &nbsp;|&nbsp; {currency}{bill_total:.2f}</summary>'
            f'<div class="bill-toggle-body">'
            f'<div class="bill-view" data-view="vanilla">{vanilla_html}</div>'
            f'<div class="bill-view" data-view="vs-prev" style="display:none;">{vs_prev_html}</div>'
            f'<div class="bill-view" data-view="vs-year" style="display:none;">{vs_year_html}</div>'
            f'</div></details>'
            f'<details class="day-charts-toggle" {open_attr}>'
            f'<summary class="day-charts-summary">{toggle_label}</summary>'
            f'<div class="day-charts-body">{day_charts_html}</div>'
            f'</details></div>'
        )

    calmonth_html_parts = [
        build_grouped_section(
            cs, "calmonth",
            find_prev_fn=lambda c: calmonth_by_index.get(c["index"] - 1),
            find_year_fn=lambda c: next(
                (o for o in calmonth_by_index.values()
                 if o["start"].month == c["start"].month and o["start"].year == c["start"].year - 1),
                None),
        )
        for cs in calmonth_sections_display
    ]

    quarter_html_parts = [
        build_grouped_section(
            qs, "quarter",
            find_prev_fn=lambda q: quarter_by_index.get(q["index"] - 1),
            find_year_fn=lambda q: next(
                (o for o in quarter_by_index.values()
                 if o["start"].month == q["start"].month and o["start"].year == q["start"].year - 1),
                None),
        )
        for qs in quarter_sections_display
    ]

    year_html_parts = [
        build_grouped_section(
            ys, "year",
            find_prev_fn=lambda y: year_by_index.get(y["index"] - 1),
            find_year_fn=lambda y: None,
            show_year_btn=False,
        )
        for ys in year_sections_display
    ]

    # ── Determine first period to show ──
    default_period = next(
        (f"period_{ps['index']}" for ps in period_sections_display if ps["is_current"]),
        f"period_{period_sections_display[0]['index']}" if period_sections_display else ""
    )
    default_calmonth = next(
        (f"calmonth_{cs['index']}" for cs in calmonth_sections_display if cs["is_current"]),
        f"calmonth_{calmonth_sections_display[0]['index']}" if calmonth_sections_display else ""
    )
    default_quarter = next(
        (f"quarter_{qs['index']}" for qs in quarter_sections_display if qs["is_current"]),
        f"quarter_{quarter_sections_display[0]['index']}" if quarter_sections_display else ""
    )
    default_year = next(
        (f"year_{ys['index']}" for ys in year_sections_display if ys["is_current"]),
        f"year_{year_sections_display[0]['index']}" if year_sections_display else ""
    )


    # ── Full HTML ──
    spinner_html = _load_period_spinner_html()
    html = f"""<!DOCTYPE html>
<html data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate"/>
<meta http-equiv="Pragma" content="no-cache"/>
<meta http-equiv="Expires" content="0"/>
<script>
(function(){{
  var stored = localStorage.getItem('emt_chart_theme');
  var sys = window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', stored || sys);
}})();
function _getThemeColours() {{
  var dark = document.documentElement.getAttribute('data-theme') !== 'light';
  return {{
    plotBg:  dark ? '#1a1d27' : '#f8f9fa',
    paperBg: dark ? '#1a1d27' : '#ffffff',
    axisC:   dark ? '#6b7080' : '#555566',
    gridC:   dark ? '#2a2d3a' : '#e5e5e5',
  }};
}}
</script>
<script src="https://cdn.plot.ly/plotly-3.0.1.min.js"></script>
{spinner_html}
<style>

/* ── Theme variables ──────────────────────────── */
:root {{
  --bg:      #f0f2f5; --surface: #ffffff; --border: #d0d5dd;
  --text:    #1a1d27; --muted:   #555970; --accent: #0a8c6a;
  --card:    #ffffff; --input-bg:#fafafa; --radius: 8px;
}}
[data-theme="dark"] {{
  --bg:      #0f1117; --surface: #1a1d27; --border: #2a2d3a;
  --text:    #e8eaf0; --muted:   #6b7080; --accent: #00d4aa;
  --card:    #1a1d27; --input-bg:#0f1117;
}}

/* ── Period rolodex ───────────────────────────── */
/* The native <select> per mode stays in the DOM as the data source and for
   programmatic value-setting, but is hidden -- the rolodex label replaces it
   visually, matching Usage Stats / Insights. */
.period-select-wrap {{ display: none !important; }}
.period-stepper {{ display: inline-flex; align-items: center; gap: 2px; }}
#period-rolodex-label {{
  font-size: 13px; font-weight: 600; color: var(--text);
  /* Fixed width (not min-width) so the ‹ › arrows stay put while stepping:
     proportional-font date labels differ in pixel width between periods even
     at equal character counts, which would otherwise nudge the right arrow. */
  width: 210px; display: inline-block; text-align: center;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}}
.pstep-btn {{
  border: 1px solid var(--border); background: var(--surface); color: var(--text);
  border-radius: var(--radius); cursor: pointer; font-size: 15px; line-height: 1;
  padding: 3px 9px;
}}
.pstep-btn:hover {{ background: var(--border); }}

/* ── Base ─────────────────────────────────────── */
*, *::before, *::after {{ box-sizing: border-box; }}

/* ── Censor mode ───────────────────────────────── */
.censored {{
  filter: none;
  transition: filter 0.2s;
}}
body.censor-on .censored {{
  filter: blur(6px);
  user-select: none;
}}
.censor-btn.active {{
  background: #c0392b !important;
  color: white !important;
  border-color: #a93226 !important;
}}

html {{
  scroll-padding-top: 80px;
}}
body {{
  margin: 0;
  padding: 0 16px 16px 16px;
  background: var(--bg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  font-size: 14px;
  color: var(--text);
}}

.page-wrap {{
  max-width: 100%;
  margin: 0;
  padding: 0 8px;
}}

/* ── Period nav ───────────────────────────────── */
.period-nav {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  background: var(--surface);
  padding: 8px 12px;
  border-radius: 0 0 8px 8px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.4);
  margin-bottom: 12px;
  flex-wrap: wrap;
  position: sticky;
  top: 0;
  z-index: 100;
  transition: box-shadow 0.2s;
}}
.nav-left {{
  display: flex;
  align-items: center;
  gap: 6px;
}}
.nav-right {{
  display: flex;
  align-items: center;
  gap: 6px;
}}
.period-nav select {{
  font-size: 11px;
  padding: 2px 7px;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--bg);
  color: var(--text);
  cursor: pointer;
  min-width: 280px;
}}
.view-btn {{
  font-size: 11px;
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--surface);
  color: var(--muted);
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
}}
.view-btn:hover {{
  background: var(--border);
}}
.view-btn.active {{
  background: var(--accent);
  color: var(--bg);
  border-color: var(--accent);
}}

/* ── Bill comparison layout ───────────────────── */
.bill-compare-wrap {{
  display: flex;
  gap: 20px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}}
.bill-compare-col {{
  flex: 1;
  min-width: 300px;
}}
.bill-compare-empty {{
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--surface);
  border: 1px dashed var(--border);
  border-radius: 8px;
  color: var(--muted);
  font-style: italic;
  padding: 20px;
}}
.bill-view-heading h2 {{
  font-size: 15px;
  font-weight: 600;
  margin: 0 0 8px 0;
  color: var(--text);
}}
.bill-view-heading.compare h2 {{
  color: var(--muted);
}}

/* ── Billing card ─────────────────────────────── */
.billing-summary {{
  background: var(--surface);
  padding: 16px 20px;
  margin-bottom: 12px;
  border-radius: 8px;
  border: 1px solid var(--border);
  position: relative;
  overflow: hidden;
}}
.billing-summary::after,
.day-chart-wrap::after {{
  content: "Informational — not authoritative";
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%) rotate(-30deg);
  font-size: 22px;
  font-weight: 700;
  color: rgba(255,255,255,0.06);
  pointer-events: none;
  user-select: none;
  letter-spacing: 0.05em;
  z-index: 9999;
}}

.billing-summary.current-period {{
  border: 2px solid var(--accent);
  background: rgba(0,212,170,0.05);
}}
.bill-site-header td {{
  text-align: left;
  font-size: 15px;
  font-weight: 700;
  color: var(--text);
  padding: 0 0 12px 0;
  border-bottom: 2px solid var(--border);
}}
.reads {{
  font-size: 11px;
  font-weight: 400;
  color: var(--muted);
}}

.billing-summary h2 {{
  margin: 0 0 18px 0;
  font-size: 18px;
  font-weight: 600;
  color: var(--accent);
}}

/* ── Billing table ────────────────────────────── */
.billing-table {{
  border-collapse: collapse;
  width: 100%;
  font-size: 12px;
}}
.billing-table td {{
  padding: 3px 0;
  text-align: right;
}}
.channel-title td {{
  text-align: left;
  padding-top: 10px;
  padding-bottom: 2px;
  font-weight: 600;
  font-size: 13px;
  color: var(--text);
  border-top: 1px solid var(--border);
}}
.channel-header td {{ font-size: 11px; color: var(--muted); padding-bottom: 2px; }}
.channel-total td  {{ padding-top: 2px; font-weight: 600; }}
.standing td       {{ padding-top: 8px; }}
.submeter-breakdown-header td {{
  text-align: left;
  padding-top: 10px;
  padding-bottom: 3px;
  font-size: 11px;
  font-weight: 600;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  border-top: 1px solid var(--border);
}}
.submeter-indent td:first-child {{ padding-left: 12px; }}
.channel-title.submeter-indent td {{
  border-top: none;
  padding-top: 6px;
  font-size: 12px;
  font-weight: 600;
}}
.channel-header.submeter-indent td {{ padding-left: 12px; }}
.grand-total td    {{
  padding-top: 8px;
  font-size: 14px;
  font-weight: 700;
  color: var(--accent);
  border-top: 2px solid var(--accent);
}}

/* ── Details / toggle ─────────────────────────── */
.day-charts-toggle {{
  background: var(--surface);
  border-radius: 8px;
  border: 1px solid var(--border);
  margin-bottom: 24px;
  overflow: hidden;
}}

.day-charts-summary {{
  cursor: pointer;
  padding: 12px 20px;
  font-weight: 600;
  font-size: 14px;
  color: var(--accent);
  background: rgba(0,212,170,0.08);
  user-select: none;
  list-style: none;
  display: flex;
  align-items: center;
  gap: 8px;
}}
.day-charts-summary::-webkit-details-marker {{ display: none; }}
.day-charts-summary::before {{
  content: "▶";
  font-size: 10px;
  transition: transform 0.2s;
  display: inline-block;
}}
.day-charts-toggle[open] .day-charts-summary::before {{
  transform: rotate(90deg);
}}

.day-charts-body {{
  padding: 12px 0 0 0;
}}

/* ── Day chart row ────────────────────────────── */
.day-chart-wrap {{
  display: flex;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  position: relative;
  overflow: hidden;
}}
.day-chart-wrap:last-child {{ border-bottom: none; }}

/* ── Summary panel ────────────────────────────── */
.chart-summary {{
  flex: 0 0 auto;
  width: auto;
  min-width: 120px;
  max-width: 220px;
  padding: 12px 10px;
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  gap: 0;
  overflow: hidden;
  font-size: clamp(10px, 1vw, 13px);
}}

.day-label {{
  font-weight: 700;
  font-size: 1em;
  color: var(--accent);
  margin-bottom: 8px;
}}

/* Meter sections inside the panel */
.scol {{
  display: flex;
  flex-direction: column;
  margin-bottom: 7px;
}}
.scol.sub    {{ color: var(--muted); }}
.scol.export {{ color: #ff9944; }}

.slabel {{
  font-weight: 600;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--muted);
  margin-bottom: 1px;
}}
.scol.export .slabel {{ color: #ffaa55; }}

.stotals {{
  display: flex;
  flex-direction: column;
  gap: 1px;
  margin-bottom: 4px;
}}

.sval {{
  font-size: 12px;
  line-height: 1.6;
  color: var(--text);
}}
.sval.export-val {{ color: #ff9944; }}
.sval.sub-val    {{ color: var(--muted); font-size: 11px; }}
.scol.export .sval {{ color: #ff9944; }}

.sdivider {{
  border-top: 1px solid var(--border);
  margin: 6px 0;
}}

.rate-row {{
  font-size: 0.8em;
  color: #4b5563;
  line-height: 1.4;
  white-space: nowrap;
}}
.rate-row.export {{ color: #ffaa55; }}
.rate-row.sub    {{ color: #4b5563; }}

/* ── Period mode toggle ───────────────────────── */
.period-mode-toggle {{
  display: flex;
  align-items: center;
  gap: 4px;
  margin-bottom: 0px;
}}
.period-mode-label {{
  font-size: 12px;
  color: var(--muted);
  margin-right: 4px;
}}
.pmode-btn {{
  padding: 2px 7px;
  font-size: 11px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--surface);
  cursor: pointer;
  color: var(--muted);
}}
.pmode-btn.active {{
  background: var(--accent);
  color: var(--bg);
  border-color: var(--accent);
}}
.period-select-wrap {{
  display: flex;
  align-items: center;
  gap: 8px;
}}


/* ── Sticky bill strip ────────────────────────── */
#sticky-bill-strip {{
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 7px 4px 2px 4px;
  border-top: 1px solid var(--border);
  font-size: 13px;
  color: var(--text);
  order: 3;
  visibility: hidden;  /* always takes up space — never changes nav height */
}}
#sticky-bill-label {{
  font-weight: 600;
  color: var(--accent);
}}
#sticky-bill-btn {{
  font-size: 12px;
  padding: 4px 12px;
  border: 1px solid var(--accent);
  border-radius: 4px;
  background: rgba(0,212,170,0.08);
  color: var(--accent);
  cursor: pointer;
  white-space: nowrap;
}}
#sticky-bill-btn:hover {{
  background: var(--accent);
  color: var(--bg);
}}

/* ── Bill toggle ──────────────────────────────── */
.bill-toggle {{
  background: var(--surface);
  border-radius: 8px;
  border: 1px solid var(--border);
  margin-bottom: 12px;
  overflow: hidden;
}}
.bill-toggle-summary {{
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 20px;
  font-size: 14px;
  font-weight: 600;
  color: var(--accent);
  cursor: pointer;
  user-select: none;
  list-style: none;
  background: var(--surface);
  border-radius: 8px;
}}
.bill-toggle-summary::-webkit-details-marker {{ display: none; }}
.bill-toggle-summary::before {{
  content: '▶';
  font-size: 11px;
  color: var(--muted);
  transition: transform 0.2s;
  display: inline-block;
  width: 14px;
}}
.bill-toggle[open] .bill-toggle-summary::before {{
  transform: rotate(90deg);
}}
.bill-toggle-body {{
  padding: 0 0 4px 0;
}}

/* ── Chart container ──────────────────────────── */
.chart-container {{
  flex: 1 1 0;
  min-width: 0;
  /* Reserve the day-chart height BEFORE it's lazily built, so a chart that
     materialises above the viewport (scrolling up) doesn't grow from ~0 and shove
     the scroll position. Matches the built layout height floor below. */
  min-height: 320px;
}}
/* ── Day data table ───────────────────────────── */
.day-chart-wrap {{ flex-wrap: wrap; }}
.day-data-tables {{
  flex: 0 0 100%;
  overflow-x: auto;
  border-top: 1px solid var(--border);
}}
.day-meter-table {{ width: 100%; }}
.day-meter-table table {{
  border-collapse: collapse;
  width: 100%;
  font-size: 11px;
  white-space: nowrap;
}}
.day-meter-table th {{
  padding: 3px 8px;
  text-align: right;
  font-weight: 600;
  font-size: 10px;
  text-transform: uppercase;
  color: var(--muted);
  border-bottom: 1px solid var(--border);
  border-right: 1px solid var(--border);
  background: var(--bg);
}}
.day-meter-table th:first-child {{ text-align: left; position: sticky; left: 0; z-index: 2; }}
.day-meter-table th[colspan] {{ text-align: center; }}
.day-meter-table td {{
  padding: 2px 8px;
  text-align: right;
  border-bottom: 1px solid var(--border);
  border-right: 1px solid var(--border);
  color: var(--text);
}}
.day-meter-table td:first-child {{
  text-align: left;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
  position: sticky;
  left: 0;
  background: var(--surface);
  z-index: 1;
}}
.day-meter-table tr:hover td {{ background: rgba(0,212,170,0.06); }}
.day-meter-table tr:hover td:first-child {{ background: var(--surface); }}
.day-meter-table tfoot td {{
  font-weight: 700;
  color: var(--accent);
  border-top: 1px solid var(--border);
  background: var(--surface);
}}
.day-tbl-toolbar {{
  flex: 0 0 100%;
  padding: 3px 8px;
  border-top: 1px solid var(--border);
  background: var(--surface);
}}
.day-tbl-toggle {{
  font-size: 11px;
  padding: 2px 8px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--surface);
  color: var(--muted);
  cursor: pointer;
  transition: background 0.15s;
}}
.day-tbl-toggle:hover, .day-tbl-toggle.active {{
  background: var(--accent);
  color: var(--bg);
  border-color: var(--accent);
}}
/* ── Landscape mobile — let period-nav scroll off for more chart space ── */
@media (max-height: 500px) and (orientation: landscape) {{
  .period-nav {{
    position: static;
  }}
}}
/* ── Mobile responsive ────────────────────────── */
@media (max-width: 600px) {{
  .period-nav {{
    padding: 6px 10px;
    gap: 4px;
  }}
  .nav-left {{
    flex-wrap: wrap;
    gap: 4px;
  }}
  .period-mode-toggle {{
    margin-bottom: 0;
  }}
  .period-nav select {{
    font-size: 11px;
    padding: 2px 6px;
    border: 1px solid var(--border);
    border-radius: 5px;
    background: var(--bg);
    cursor: pointer;
    min-width: 0;
    width: 100%;
  }}
  .pmode-btn {{
    padding: 2px 6px;
    font-size: 11px;
  }}
  .view-btn {{
    padding: 2px 6px;
    font-size: 11px;
  }}
  .period-select-wrap {{
    flex-direction: column;
    align-items: flex-start;
    gap: 3px;
    width: 100%;
  }}
  .day-chart-wrap {{
    flex-direction: column;
  }}
  .chart-summary {{
    flex: none;
    width: 100%;
    border-right: none;
    border-bottom: 1px solid var(--border);
    padding: 8px 12px;
    flex-direction: row;
    flex-wrap: wrap;
    gap: 6px 16px;
  }}
  .day-label {{
    width: 100%;
    margin-bottom: 2px;
  }}
  .scol {{
    margin-bottom: 2px;
  }}
  .bill-compare-wrap {{
    flex-direction: column;
  }}
  .bill-compare-col {{
    min-width: 0;
  }}
}}

</style>
</head>
<body>
<div class="page-wrap">

{dropdown_html}

<div id="period-sections-wrap" data-asc="0">
{''.join(sections_html_parts)}
</div>
{''.join(calmonth_html_parts)}
{''.join(quarter_html_parts)}
{''.join(year_html_parts)}

</div>

<script>
// ── Mobile chart scaling ──────────────────────────────────
var _MIN_CHART_W = 380; // px — below this we scale rather than reflow
var _relayoutTimer = null;

function _scaleChartEl(el, deferRelayout) {{
  var wrap = el.closest('.day-chart-wrap');
  if (!wrap) return;
  // Use el.offsetWidth — the actual flex-computed chart container width
  var avail = el.offsetWidth;
  if (avail < 1) return;
  var isMobile = window.getComputedStyle(wrap).flexDirection === 'column';
  if (isMobile) {{
    el.style.transform = '';
    el.style.transformOrigin = '';
    el.style.width = '';
    el.style.height = '';
    if (!deferRelayout && el._fullData && avail < 480) {{
      var skipEvery = avail < 320 ? 5 : 3;
      var vals = [], texts = [];
      el._fullLayout.xaxis.tickvals.forEach(function(v, i) {{
        if (i % skipEvery === 0) {{ vals.push(v); texts.push(el._fullLayout.xaxis.ticktext[i]); }}
      }});
      Plotly.relayout(el, {{'xaxis.tickvals': vals, 'xaxis.ticktext': texts}});
    }}
  }} else if (avail < _MIN_CHART_W) {{
    var scale = avail / _MIN_CHART_W;
    el.style.transformOrigin = 'top left';
    el.style.transform = 'scale(' + scale + ')';
    el.style.width = _MIN_CHART_W + 'px';
    el.style.height = (el.offsetHeight / scale) + 'px';
  }} else {{
    el.style.transform = '';
    el.style.transformOrigin = '';
    el.style.width = '';
    el.style.height = '';
    // Plotly relayout deferred — batched after resize settles
    if (!deferRelayout && window.Plotly && window._energyCharts && window._energyCharts[el.id]) {{
      Plotly.relayout(el, {{autosize: true, width: avail}});
    }}
  }}
}}

function _scaleDayCharts() {{
  // Pass 1: CSS transforms only — instant, no Plotly calls
  document.querySelectorAll('.chart-container').forEach(function(el) {{
    if (window._energyCharts && window._energyCharts[el.id]) _scaleChartEl(el, true);
  }});
  // Pass 2: Plotly relayouts — debounced, runs after resize animation settles
  clearTimeout(_relayoutTimer);
  _relayoutTimer = setTimeout(function() {{
    document.querySelectorAll('.chart-container').forEach(function(el) {{
      if (window._energyCharts && window._energyCharts[el.id]) _scaleChartEl(el, false);
    }});
  }}, 400);
}}

window.addEventListener('resize', _scaleDayCharts, {{passive: true}});

// ── Deferred chart renderer ──────────────────────────────────
// Charts store data in <script type="application/json"> blocks.
// _renderSection reads the JSON and builds Plotly traces only when
// the section becomes visible — no JS parsing at page load time.
function _buildDayChart(chartId) {{
  var el = document.getElementById(chartId);
  if (!el) return;
  var dataEl = document.getElementById('data_' + chartId);
  if (!dataEl) return;
  var d;
  try {{ d = JSON.parse(dataEl.textContent); }} catch(e) {{ return; }}

  var slots = d.slots;
  var bm    = d.block_minutes;
  var cur   = d.currency;
  var xBar  = Array.from({{length: slots}}, function(_, i) {{ return i; }});
  var xRanges = d.x_ranges;
  var xLabels = d.x_labels;

  var totalHhKwh = Array(slots).fill(0);
  Object.values(d.meters).forEach(function(m) {{
    if (!m.is_export) m.y.forEach(function(v, i) {{ totalHhKwh[i] += v; }});
  }});

  var traces = [];
  Object.entries(d.meters).forEach(function(entry) {{
    var mid = entry[0]; var m = entry[1];
    var useArea = bm < 15;
    var customdata = m.y.map(function(v, i) {{
      return [xRanges[i], totalHhKwh[i], Math.abs(v)];
    }});
    var hoverTotal = m.is_export ? '' : ' (%{{customdata[1]:.3f}} total)';
    var trace = {{
      x: xBar, y: m.y,
      type: useArea ? 'scatter' : 'bar',
      name: m.nice_name,
      customdata: customdata,
      hovertemplate: m.nice_name + '<br>%{{customdata[0]}}<br>%{{customdata[2]:.3f}} kWh' + hoverTotal + '<extra></extra>'
    }};
    if (useArea) {{
      trace.mode = 'lines'; trace.fill = 'tozeroy';
      trace.line = {{shape: 'hv', color: m.bar_color}};
    }} else {{
      trace.width = 0.7; trace.marker = {{color: m.bar_color}};
    }}
    traces.push(trace);
    if (m.has_rate) {{
      traces.push({{
        x: m.rate_x, y: m.rate,
        type: 'scatter', mode: 'lines',
        line: {{shape: 'hv', width: 2, color: m.line_color, dash: m.is_export ? 'dash' : 'solid'}},
        name: m.nice_name + ' rate', yaxis: 'y2',
        customdata: xRanges.concat([xRanges[xRanges.length - 1]]),
        hovertemplate: m.nice_name + ' rate<br>%{{customdata}}<br>' + cur + '%{{y:.4f}}<extra></extra>'
      }});
    }}
  }});

  var tickStep = Math.max(1, Math.round(30 / bm));
  var tickVals  = xBar.filter(function(_, i) {{ return i % tickStep === 0; }});
  var tickTexts = xLabels.filter(function(_, i) {{ return i % tickStep === 0; }});
  var tc = _getThemeColours();
  var layout = {{
    autosize: true, barmode: 'relative',
    margin: {{l: 46, r: 52, t: 16, b: 80}},
    plot_bgcolor: tc.plotBg, paper_bgcolor: tc.paperBg,
    xaxis: {{tickmode:'array', tickvals:tickVals, ticktext:tickTexts, tickangle:-45, showgrid:false}},
    yaxis:  {{title:'kWh', showgrid:true, gridcolor:tc.gridC, titlefont:{{size:11,color:tc.axisC}}, tickfont:{{color:tc.axisC}}}},
    yaxis2: {{title:cur+'/kWh', overlaying:'y', side:'right', showgrid:false, titlefont:{{size:11,color:tc.axisC}}, tickfont:{{color:tc.axisC}}}},
    legend: {{orientation:'h', x:0.5, xanchor:'center', y:-0.28, yanchor:'top', font:{{size:11,color:tc.axisC}}}}
  }};
  // Build to the CONTAINER's own (reserved) height — not the whole wrap. The wrap
  // also includes the full-width data tables below the chart, so measuring it made
  // the built chart overshoot AND, because the empty container had no reserved
  // height, the wrap grew when the chart appeared — the scroll-jump on lazy build.
  // The container carries min-height:320, so this is a stable 320 before and after.
  var elH = el.offsetHeight;
  layout.height = Math.max(elH > 0 ? elH : 0, 320);

  function _alignY2() {{
    var y1range = el._fullLayout.yaxis.range;
    var y2max = el._fullLayout.yaxis2._range ? el._fullLayout.yaxis2._range[1] : el._fullLayout.yaxis2.range[1];
    if (y2max <= 0) return;
    var hasImport = traces.some(function(t) {{ return t.yaxis !== 'y2' && t.y && t.y.some(function(v) {{ return v > 0.001; }}); }});
    var hasExport = traces.some(function(t) {{ return t.yaxis !== 'y2' && t.y && t.y.some(function(v) {{ return v < -0.001; }}); }});
    var y1min = y1range[0]; var y1top, y2min;
    var rawStep = y2max / 4;
    var mag  = Math.pow(10, Math.floor(Math.log10(rawStep)));
    var step = [1, 2, 2.5, 5, 10].map(function(f) {{ return f * mag; }}).find(function(s) {{ return s >= rawStep; }}) || mag;
    var ticks = [];
    for (var t = 0; t <= y2max + step * 0.01; t += step) ticks.push(parseFloat(t.toFixed(10)));
    if (hasExport && !hasImport) {{
      var exportDepth = -y1min; y1top = exportDepth * 0.5;
      var frac = exportDepth / (exportDepth + y1top);
      y2min = -frac * (y2max / (1 - frac));
    }} else if (hasImport && hasExport) {{
      y1top = y1range[1];
      var negFrac = -y1min / (y1range[1] - y1min);
      y2min = -negFrac * (y2max / (1 - negFrac));
    }} else {{
      // Import-only day (no export to pull the axes below zero). Normally the rate
      // axis floors at 0 — but an Agile plunge-price slot has a NEGATIVE rate, which
      // then clips flat at the baseline. Give the rate axis room below zero for the
      // lowest negative rate, and give the energy axis a matching zero-fraction so
      // the two zero-lines stay aligned (same trick as the export branches).
      y1top = y1range[1];
      var rateMin = 0;
      traces.forEach(function(t) {{
        if (t.yaxis === 'y2' && t.y) t.y.forEach(function(v) {{ if (v < rateMin) rateMin = v; }});
      }});
      if (rateMin < 0) {{
        y2min = rateMin - (-rateMin) * 0.08;          // small headroom below the trough
        var f = -y2min / (y2max - y2min);             // zero fraction from the bottom
        y1min = -(f / (1 - f)) * y1top;               // matching negative headroom on y1
      }} else {{
        y2min = 0;
      }}
    }}
    // Label the below-zero region of the rate axis when it dips negative (plunge
    // price / export), so the line has readable ticks rather than an unlabelled tail.
    if (y2min < 0) {{
      for (var tn = -step; tn >= y2min - step * 0.01; tn -= step) ticks.unshift(parseFloat(tn.toFixed(10)));
    }}
    Plotly.relayout(el, {{
      'yaxis.range': [y1min, y1top], 'yaxis2.range': [y2min, y2max],
      'yaxis2.tickmode': 'array', 'yaxis2.tickvals': ticks,
      'yaxis2.ticktext': ticks.map(function(v) {{ return v.toFixed(2); }})
    }});
  }}

  Plotly.newPlot(el, traces, layout, {{responsive:true, displayModeBar:false}}).then(function() {{
    _alignY2();
    el.on('plotly_restyle', function() {{ setTimeout(_alignY2, 50); }});
  }});
  if (!window._energyCharts) window._energyCharts = {{}};
  window._energyCharts[chartId] = el;
  _scaleChartEl(el);
}}

// One shared observer builds each day chart only as it scrolls near the
// viewport, so revealing a year (~365 charts) never renders more than the few
// on screen. The callback ignores anything in a section that's now hidden, so a
// stray event that fires just after a mode switch is a no-op. There are no
// background workers: each chart is one synchronous Plotly build on its own
// intersection callback, and switching section disconnects the observer (below),
// dropping everything still queued for the section you left.
var _chartObserver = null;
function _ensureChartObserver() {{
  if (_chartObserver || typeof IntersectionObserver === 'undefined') return _chartObserver;
  _chartObserver = new IntersectionObserver(function(entries) {{
    entries.forEach(function(entry) {{
      if (!entry.isIntersecting) return;
      var el = entry.target;
      _chartObserver.unobserve(el);
      var sec = el.closest('.period-section');
      if (!sec || sec.style.display === 'none' || sec.style.visibility === 'hidden') return;   // section left the screen
      var chartId = window._pendingCharts && window._pendingCharts[el.id];
      if (chartId) {{
        delete window._pendingCharts[el.id];
        _buildDayChart(chartId);
      }}
    }});
  }}, {{ rootMargin: '600px 0px' }});   // build a little before they scroll in
  return _chartObserver;
}}

function _renderSection(section) {{
  if (!section || !window._pendingCharts) return;
  var obs = _ensureChartObserver();
  // Stop watching the previous section's charts — anything queued there is
  // dropped, so switching mode mid-render leaves nothing rendering off-screen.
  if (obs) obs.disconnect();
  section.querySelectorAll('.chart-container').forEach(function(el) {{
    // Skip day charts inside a collapsed panel — they get observed when the
    // panel is expanded (see the toggle listener in _revealSection).
    var det = el.closest('details.day-charts-toggle');
    if (det && !det.open) return;
    if (!window._pendingCharts[el.id]) return;   // already built
    if (obs) {{
      obs.observe(el);                            // build when it nears the viewport
    }} else {{
      var chartId = window._pendingCharts[el.id]; // no IntersectionObserver — build now
      delete window._pendingCharts[el.id];
      _buildDayChart(chartId);
    }}
  }});
}}

var _currentMode = 'month';

function setPeriodMode(mode) {{
  if (window.parent) window.parent.postMessage({{ type: 'suppressResize' }}, '*');
  _currentMode = mode;
  sessionStorage.setItem('energyPeriodMode', mode);
  // Toggle dropdowns
  document.getElementById('select-month').style.display    = (mode==='month')    ? '' : 'none';
  document.getElementById('select-calmonth').style.display = (mode==='calmonth') ? '' : 'none';
  document.getElementById('select-quarter').style.display  = (mode==='quarter')  ? '' : 'none';
  document.getElementById('select-year').style.display     = (mode==='year')     ? '' : 'none';
  // Toggle mode buttons
  document.querySelectorAll('.pmode-btn').forEach(function(b) {{
    b.classList.toggle('active', b.dataset.mode === mode);
  }});
  // Hide vs Last Year in year mode
  var vyb = document.querySelector('.vs-year-btn');
  if (vyb) vyb.style.display = (mode==='year') ? 'none' : '';
  // Drop back to vanilla if vs-year was active in year mode
  if (mode==='year') {{
    var sv = sessionStorage.getItem('energyView') || 'vanilla';
    if (sv==='vs-year') showView('vanilla');
  }}
  // Hide all sections, show correct one for mode. Use display:none (NOT
  // visibility:hidden+absolute): an absolutely-positioned hidden section is out
  // of flow but still EXTENDS the scroll height, so a tall built-out year view
  // left a huge dead scroll void under a short month. display:none removes it.
  document.querySelectorAll('.period-section').forEach(function(el) {{
      el.style.display = 'none';
  }});
  var defaults = {{ month: '{default_period}', quarter: '{default_quarter}', year: '{default_year}' }};
  var savedId  = sessionStorage.getItem('energyPeriod_' + mode);
  // Validate saved ID still exists in DOM (blocks update may have changed period count)
  var initial  = (savedId && document.getElementById(savedId)) ? savedId : defaults[mode];
  // If default also missing (shouldn't happen) fall back to first visible section
  if (!document.getElementById(initial)) {{
    var first = document.querySelector('.' + mode + '-section');
    if (first) initial = first.id;
  }}
  sessionStorage.setItem('energyPeriod_' + mode, initial);
  _revealSection(initial);
  var selMap = {{ month:'period-select-month', calmonth:'period-select-calmonth', quarter:'period-select-quarter', year:'period-select-year' }};
  var sel = document.getElementById(selMap[mode]);
  if (sel) sel.value = initial;
  _syncRolodexLabel();
}}

// ── Period rolodex adapter (shared PeriodSpinner) ──────────
// The native <select> per mode is the data source; the rolodex label is the
// visible control. getPeriods reads the active mode's options (already
// newest-first); onSelect drives the existing showPeriod() path.
var _ROLODEX_SEL = {{ month:'period-select-month', calmonth:'period-select-calmonth', quarter:'period-select-quarter', year:'period-select-year' }};
function _rolodexSelect() {{ return document.getElementById(_ROLODEX_SEL[_currentMode]); }}
function _rolodexClean(text) {{ return String(text || '').replace(/^★ Current\\s+/, ''); }}

function _billGetPeriods() {{
  var sel = _rolodexSelect();
  if (!sel) return {{ list: [], curIdx: -1 }};
  var list = [];
  for (var i = 0; i < sel.options.length; i++) {{
    list.push({{ label: _rolodexClean(sel.options[i].text), value: sel.options[i].value }});
  }}
  return {{ list: list, curIdx: sel.selectedIndex }};
}}
function _billSelectPeriod(value) {{
  var sel = _rolodexSelect();
  if (sel) sel.value = value;
  showPeriod(value, _currentMode);   // showPeriod() re-syncs the label
}}
// ‹ / › arrows: step one period within the current mode. Options are
// newest-first, so delta +1 = older, -1 = newer.
function _billStep(delta) {{
  var sel = _rolodexSelect();
  if (!sel || !sel.options.length) return;
  var ni = Math.min(sel.options.length - 1, Math.max(0, sel.selectedIndex + delta));
  if (ni === sel.selectedIndex) return;
  _billSelectPeriod(sel.options[ni].value);
}}
function _syncRolodexLabel() {{
  var lbl = document.getElementById('period-rolodex-label');
  if (!lbl) return;
  var sel = _rolodexSelect();
  if (!sel || sel.selectedIndex < 0) {{ lbl.textContent = ''; return; }}
  var t = _rolodexClean(sel.options[sel.selectedIndex].text);
  var bar = t.indexOf('  |  ');            // drop the cost tail for the collapsed label
  lbl.textContent = (bar >= 0 ? t.slice(0, bar) : t).trim();
}}
function _initRolodex() {{
  var lbl = document.getElementById('period-rolodex-label');
  if (lbl && window.PeriodSpinner) {{
    PeriodSpinner.attach(lbl, {{ getPeriods: _billGetPeriods, onSelect: _billSelectPeriod }});
  }}
  _syncRolodexLabel();
}}

function _revealSection(id) {{
    var section = document.getElementById(id);
    if (section) {{
      section.style.display = '';
      section.style.visibility = 'visible';
      section.style.position = 'relative';
    // Restore day-charts-toggle open/closed state
    section.querySelectorAll('.day-charts-toggle').forEach(function(det) {{
      var key = 'energyChartsOpen_' + det.id;
      var saved = sessionStorage.getItem(key);
      if (saved !== null) {{ det.open = saved === '1'; }}
      if (!det._listenerAdded) {{
        det.addEventListener('toggle', function() {{
          sessionStorage.setItem('energyChartsOpen_' + det.id, det.open ? '1' : '0');
          // Build the (previously skipped) day charts now the panel is open.
          if (det.open) _renderSection(section);
        }});
        det._listenerAdded = true;
      }}
    }});
    // Re-apply current view so bill-view divs inside this section are correct
    var currentView = sessionStorage.getItem('energyView') || 'vanilla';
    section.querySelectorAll('.bill-view').forEach(function(el) {{
      el.style.display = (el.dataset.view === currentView) ? 'block' : 'none';
    }});
    if (window._energyCharts) {{
      section.querySelectorAll('.chart-container').forEach(function(c) {{
        if (window._energyCharts[c.id]) Plotly.relayout(c, {{autosize: true}});
      }});
    }}
    _renderSection(section);
    _attachStickyObserver(section);
  }}
}}

function showPeriod(id, mode) {{
  if (window.parent) window.parent.postMessage({{ type: 'suppressResize' }}, '*');
  if (!mode) mode = _currentMode;
  sessionStorage.setItem('energyPeriod_' + mode, id);
  // Hide all sections belonging to this mode, show only the selected one
  var clsMap = {{ month:'month-section', calmonth:'calmonth-section', quarter:'quarter-section', year:'year-section' }};
  document.querySelectorAll('.' + clsMap[mode]).forEach(function(el) {{
    el.style.display = 'none';
  }});
  document.getElementById('sticky-bill-strip').style.visibility = 'hidden';
  _revealSection(id);
  // A deliberate period change swaps in a section of a completely different
  // length. Reset the scroll to the top so the new charts are visible at once --
  // otherwise, coming from a long year view scrolled to the bottom, the shorter
  // month view shows only blank space until you scroll back up (and the
  // "Show Bill" strip only appears once a chart re-enters view). Also clear the
  // saved scroll so a later reload / tab-return doesn't restore the stale deep
  // position for this shorter section.
  window.scrollTo(0, 0);
  try {{ sessionStorage.setItem('energyScroll', '0'); }} catch (e) {{}}
  _syncRolodexLabel();
}}

// ── Sticky bill strip ─────────────────────────────────────
var _stickyObserver = null;
var _stickySection  = null;

function _getSectionBillLabel(section) {{
  // Try to extract period label + cost from the bill-toggle-summary context
  // and the h2 inside the visible bill-view
  var h2 = section.querySelector('.bill-view-heading h2');
  return h2 ? h2.textContent.trim() : '';
}}

function _getSectionBillCost(section) {{
  // Grab the total cost line from the billing table
  var el = section.querySelector('.bill-total td:last-child');
  return el ? el.textContent.trim() : '';
}}

function _attachStickyObserver(section) {{
  if (_stickyObserver) {{ _stickyObserver.disconnect(); _stickyObserver = null; }}
  _stickySection = section;
  var billToggle = section ? section.querySelector('.bill-toggle') : null;
  if (!billToggle) return;
  var strip = document.getElementById('sticky-bill-strip');
  var label = document.getElementById('sticky-bill-label');
  // Populate label directly from the bill-toggle summary text
  var summary = billToggle.querySelector('.bill-toggle-summary');
  label.textContent = summary ? summary.textContent.trim() : '';
  _stickyObserver = new IntersectionObserver(function(entries) {{
    var entry = entries[0];
    if (!entry.isIntersecting && entry.boundingClientRect.top < 0) {{
      // Bill toggle has scrolled above viewport — show strip and stop observing
      // to prevent oscillation from the nav growing taller
      strip.style.visibility = 'visible';
      _stickyObserver.disconnect();
      _stickyObserver = null;
      // Re-attach a one-shot observer to hide strip when user scrolls back up
      var hideObserver = new IntersectionObserver(function(e2) {{
        if (e2[0].isIntersecting) {{
          strip.style.visibility = 'hidden';
          hideObserver.disconnect();
          // Re-arm the main observer now that bill toggle is visible again
          _attachStickyObserver(_stickySection);
        }}
      }}, {{ threshold: 0 }});
      hideObserver.observe(billToggle);
    }}
  }}, {{ threshold: 0, rootMargin: '0px 0px 0px 0px' }});
  _stickyObserver.observe(billToggle);
}}

function stickyBillExpand() {{
  if (!_stickySection) return;
  // Open the bill toggle if collapsed
  var billToggle = _stickySection.querySelector('.bill-toggle');
  if (billToggle && !billToggle.open) billToggle.open = true;
  // Scroll section into view (below sticky nav)
  var navH = document.querySelector('.period-nav').offsetHeight;
  var top  = _stickySection.getBoundingClientRect().top + window.scrollY - navH - 8;
  window.scrollTo({{ top: top, behavior: 'smooth' }});
}}

function toggleTheme() {{
  var current = document.documentElement.getAttribute('data-theme');
  var next = current === 'light' ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('emt_chart_theme', next);
  var btn = document.getElementById('theme-toggle');
  if (btn) btn.textContent = next === 'dark' ? '\u263e' : '\u2600';
  // Notify parent shell so it syncs all other iframes and shell UI
  if (window.parent && window.parent !== window) {{
    window.parent.postMessage({{type:'emt-theme-change', theme:next}}, '*');
  }}
  // Relayout Plotly charts in this iframe
  var tc = _getThemeColours();
  if (window._energyCharts) {{
    Object.keys(window._energyCharts).forEach(function(id) {{
      var el = document.getElementById(id);
      if (el) {{
        Plotly.relayout(el, {{
          plot_bgcolor: tc.plotBg,
          paper_bgcolor: tc.paperBg,
          'xaxis.tickfont.color': tc.axisC,
          'yaxis.gridcolor': tc.gridC,
          'yaxis.titlefont.color': tc.axisC,
          'yaxis.tickfont.color': tc.axisC,
          'yaxis2.titlefont.color': tc.axisC,
          'yaxis2.tickfont.color': tc.axisC,
          'legend.font.color': tc.axisC,
        }});
      }}
    }});
  }}
}}

function toggleAllTables() {{
  var tables = document.querySelectorAll('.day-data-tables');
  var anyOpen = Array.from(tables).some(function(t) {{ return t.style.display === 'block' || t.style.display === 'flex'; }});
  var btn = document.getElementById('expand-all-btn');
  tables.forEach(function(t) {{
    if (anyOpen) {{
      t.style.display = 'none';
    }} else {{
      if (!t._built) {{
        var chartId = t.id.replace(/^tbl_/, '');
        var dataEl  = document.getElementById('data_' + chartId);
        if (dataEl) _buildTableContent(t, dataEl.textContent);
      }}
      t.style.display = 'flex';
    }}
  }});
  var nowOpen = !anyOpen;
  if (btn) {{
    btn.classList.toggle('active', nowOpen);
    btn.textContent = nowOpen ? 'Hide Data' : 'Show Data';
  }}
}}

function _buildTableContent(wrap, jsonText) {{
  var d; try {{ d = JSON.parse(jsonText); }} catch(e) {{ return; }}
  var cur = d.currency || '\xa3';
  var exportKey = 'electricity_main_export';
  var directKey = 'electricity_main';
  var deviceKeys = Object.keys(d.meters).filter(function(k) {{
    return k !== exportKey && k !== directKey;
  }}).sort();
  var orderedKeys = [exportKey, directKey].concat(deviceKeys).filter(function(k) {{ return d.meters[k]; }});
  var colNames = ['Total Import'].concat(orderedKeys.map(function(k) {{ return d.meters[k].nice_name; }}));
  var numMeters = colNames.length;

  // BL-24 opt-in: ex-VAT columns on IMPORT meters (export is zero-rated → unchanged).
  var br = !!d.bill_rounding;
  var ratio  = d.exc_ratio  || [];
  var approx = d.exc_approx || [];
  var invat  = d.fallback_invat || (1.0 / 1.05); // period statutory 1/(1+VAT) when a slot has no ratio
  // is a given column an import meter? Total Import (index 0) always is.
  function isImp(idx) {{ return idx === 0 ? true : !d.meters[orderedKeys[idx-1]].is_export; }}
  function money(v) {{ return (Math.abs(v) >= 0.000005) ? (cur + v.toFixed(5)) : '—'; }}

  var html = '<div class="day-meter-table"><table><thead>';
  html += '<tr><th rowspan="2">Period</th>';
  colNames.forEach(function(n, idx) {{
    var span = (br && isImp(idx)) ? 4 : 3;
    html += '<th colspan="' + span + '">' + n + '</th>';
  }});
  html += '</tr><tr>';
  for (var c = 0; c < numMeters; c++) {{
    if (br && isImp(c)) {{
      html += '<th>Rate (exc)</th><th>kWh</th><th>' + cur + ' exc</th><th>' + cur + ' inc</th>';
    }} else {{
      html += '<th>Rate</th><th>kWh</th><th>' + cur + '</th>';
    }}
  }}
  html += '</tr></thead><tbody>';

  // Per-column ex-VAT cost accumulators for the footer (import columns only).
  var excTot = new Array(numMeters); for (var e = 0; e < numMeters; e++) excTot[e] = 0;

  for (var i = 0; i < d.slots; i++) {{
    var rt = (ratio[i] == null) ? invat : ratio[i];
    var ap = !!approx[i];
    html += '<tr><td>' + d.x_ranges[i].split(' - ')[0] + '</td>';
    var ti_kwh  = parseFloat(Math.abs(d.ti_kwh  ? (d.ti_kwh[i]  || 0) : 0).toFixed(3));
    var ti_cost = parseFloat(Math.abs(d.ti_cost ? (d.ti_cost[i] || 0) : 0).toFixed(5));
    var ti_rate = d.ti_rate ? (d.ti_rate[i] || 0) : 0;
    if (br) {{
      var tiExc = ti_cost * rt; excTot[0] += tiExc;
      html += '<td>' + (ti_rate ? (cur + (ti_rate * rt).toFixed(4)) : '—') + '</td>';
      html += '<td>' + ti_kwh.toFixed(3) + '</td>';
      html += '<td>' + (ti_cost >= 0.000005 ? ((ap ? '≈' : '') + money(tiExc)) : '—') + '</td>';
      html += '<td>' + (ti_cost >= 0.000005 ? (cur + ti_cost.toFixed(5)) : '—') + '</td>';
    }} else {{
      html += '<td>' + (ti_rate ? (cur + ti_rate.toFixed(4)) : '—') + '</td>';
      html += '<td>' + ti_kwh.toFixed(3) + '</td>';
      html += '<td>' + (ti_cost >= 0.000005 ? (cur + ti_cost.toFixed(5)) : '—') + '</td>';
    }}
    orderedKeys.forEach(function(k, ki) {{
      var m    = d.meters[k];
      var imp  = !m.is_export;
      var kwh  = parseFloat(Math.abs(m.y[i] || 0).toFixed(3));
      var cost = parseFloat(Math.abs((m.cost && m.cost[i]) || 0).toFixed(5));
      var rr   = m.rate;
      var rate = rr[i] !== undefined ? rr[i] : (rr[rr.length-1] || 0);
      if (br && imp) {{
        var exc = cost * rt; excTot[ki + 1] += exc;
        html += '<td>' + (rate ? (cur + (rate * rt).toFixed(4)) : '—') + '</td>';
        html += '<td>' + kwh.toFixed(3) + '</td>';
        html += '<td>' + (cost >= 0.000005 ? ((ap ? '≈' : '') + money(exc)) : '—') + '</td>';
        html += '<td>' + (cost >= 0.000005 ? (cur + cost.toFixed(5)) : '—') + '</td>';
      }} else {{
        html += '<td>' + (rate ? (cur + rate.toFixed(4)) : '—') + '</td>';
        html += '<td>' + kwh.toFixed(3) + '</td>';
        html += '<td>' + (cost >= 0.000005 ? (cur + cost.toFixed(5)) : '—') + '</td>';
      }}
    }});
    html += '</tr>';
  }}

  html += '</tbody><tfoot><tr><td>Total</td>';
  var tot_ti_kwh = 0, tot_ti_cost = 0;
  orderedKeys.forEach(function(k) {{
    var t = (d.meter_totals && d.meter_totals[k]) || {{kwh:0,cost:0}};
    if (!d.meters[k].is_export) {{ tot_ti_kwh += t.kwh; tot_ti_cost += t.cost; }}
  }});
  if (br) {{
    html += '<td></td><td>' + tot_ti_kwh.toFixed(3) + '</td><td>' + cur + excTot[0].toFixed(2) + '</td><td>' + cur + tot_ti_cost.toFixed(2) + '</td>';
  }} else {{
    html += '<td></td><td>' + tot_ti_kwh.toFixed(3) + '</td><td>' + cur + tot_ti_cost.toFixed(2) + '</td>';
  }}
  orderedKeys.forEach(function(k, ki) {{
    var t = (d.meter_totals && d.meter_totals[k]) || {{kwh:0,cost:0}};
    if (br && !d.meters[k].is_export) {{
      html += '<td></td><td>' + t.kwh.toFixed(3) + '</td><td>' + cur + excTot[ki + 1].toFixed(2) + '</td><td>' + cur + t.cost.toFixed(2) + '</td>';
    }} else {{
      html += '<td></td><td>' + t.kwh.toFixed(3) + '</td><td>' + cur + t.cost.toFixed(2) + '</td>';
    }}
  }});
  html += '</tr></tfoot></table></div>';
  wrap.innerHTML = html;
  wrap._built = true;
}}

function toggleDayTables(tableId, btn) {{
  var wrap = document.getElementById(tableId);
  if (!wrap) return;
  var open = wrap.style.display === 'none' || wrap.style.display === '';
  if (open) {{
    if (!wrap._built) {{
      var dataEl = document.getElementById('data_' + tableId.replace(/^tbl_/, ''));
      if (!dataEl) return;
      _buildTableContent(wrap, dataEl.textContent);
    }}
    wrap.style.display = 'flex';
    if (btn) btn.classList.add('active');
  }} else {{
    wrap.style.display = 'none';
    if (btn) btn.classList.remove('active');
  }}
  // Sync the global Show/Hide Data button label
  var allTables = document.querySelectorAll('.day-data-tables');
  var anyOpen = Array.from(allTables).some(function(t) {{ return t.style.display === 'flex' || t.style.display === 'block'; }});
  var globalBtn = document.getElementById('expand-all-btn');
  if (globalBtn) {{
    globalBtn.classList.toggle('active', anyOpen);
    globalBtn.textContent = anyOpen ? 'Hide Data' : 'Show Data';
  }}
}}

function toggleCensor() {{
  var on = document.body.classList.toggle('censor-on');
  var btn = document.getElementById('censor-toggle');
  if (btn) btn.classList.toggle('active', on);
  sessionStorage.setItem('energyCensor', on ? '1' : '0');
}}

function toggleSortOrder() {{
  var asc = localStorage.getItem('emt_bill_sort_asc') === '1';
  var nowAsc = !asc;
  localStorage.setItem('emt_bill_sort_asc', nowAsc ? '1' : '0');

  // Reverse day charts within each period section
  document.querySelectorAll('.day-charts-body').forEach(function(body) {{
    var children = Array.from(body.children);
    children.reverse().forEach(function(c) {{ body.appendChild(c); }});
  }});

  // Update button
  var btn = document.getElementById('sort-toggle');
  if (btn) {{
    btn.classList.toggle('active', nowAsc);
    btn.textContent = nowAsc ? '↑ Oldest first' : '↓ Latest first';
  }}
}}

function showView(view) {{
  sessionStorage.setItem('energyView', view);
  document.querySelectorAll('.view-btn').forEach(function(b) {{
    b.classList.toggle('active', b.dataset.view === view);
  }});
  document.querySelectorAll('.bill-view').forEach(function(el) {{
    el.style.display = (el.dataset.view === view) ? 'block' : 'none';
  }});
}}


(function() {{
  var savedMode = sessionStorage.getItem('energyPeriodMode') || 'month';
  // Restore view
  var savedView = sessionStorage.getItem('energyView') || 'vanilla';
  if (savedMode==='year' && savedView==='vs-year') savedView = 'vanilla';
  showView(savedView);
  // Restore censor
  if (sessionStorage.getItem('energyCensor')==='1') toggleCensor();
  // Restore sort order button state and day chart order
  if (localStorage.getItem('emt_bill_sort_asc') === '1') {{
    document.querySelectorAll('.day-charts-body').forEach(function(body) {{
      var children = Array.from(body.children);
      children.reverse().forEach(function(c) {{ body.appendChild(c); }});
    }});
    var btn = document.getElementById('sort-toggle');
    if (btn) {{ btn.classList.add('active'); btn.textContent = '↑ Oldest first'; }}
  }}
  // Listen for theme changes from parent shell
  window.addEventListener('message', function(e) {{
    if (e.data && e.data.type === 'emt-theme') {{
      document.documentElement.setAttribute('data-theme', e.data.theme);
      var btn = document.getElementById('theme-toggle');
      if (btn) btn.textContent = e.data.theme === 'dark' ? '\u263e' : '\u2600';
      var tc = _getThemeColours();
      if (window._energyCharts) {{
        Object.keys(window._energyCharts).forEach(function(id) {{
          var el = document.getElementById(id);
          if (el) {{
            Plotly.relayout(el, {{
              plot_bgcolor: tc.plotBg, paper_bgcolor: tc.paperBg,
              'xaxis.tickfont.color': tc.axisC, 'yaxis.gridcolor': tc.gridC,
              'yaxis.titlefont.color': tc.axisC, 'yaxis.tickfont.color': tc.axisC,
              'yaxis2.titlefont.color': tc.axisC, 'yaxis2.tickfont.color': tc.axisC,
              'legend.font.color': tc.axisC,
            }});
          }}
        }});
      }}
    }}
  }});
  // Sync toggle button icon to current theme
  var _themeBtn = document.getElementById('theme-toggle');
  if (_themeBtn) _themeBtn.textContent = document.documentElement.getAttribute('data-theme') === 'light' ? '\u2600' : '\u263e';
  // Activate mode
  setPeriodMode(savedMode);
  // Attach the shared rolodex spinner to the period label
  _initRolodex();
  // Render charts in the initially visible section
  var _modeDefaults = {{ month: '{default_period}', calmonth: '{default_calmonth}', quarter: '{default_quarter}', year: '{default_year}' }};
  var _initId = sessionStorage.getItem('energyPeriod_' + savedMode);
  if (!_initId || !document.getElementById(_initId)) _initId = _modeDefaults[savedMode];
  _renderSection(document.getElementById(_initId));
  _attachStickyObserver(document.getElementById(_initId));
  // Restore scroll
  var savedScroll = sessionStorage.getItem('energyScroll');
  if (savedScroll) window.scrollTo(0, parseInt(savedScroll, 10));
  window.addEventListener('scroll', function() {{
    sessionStorage.setItem('energyScroll', window.scrollY.toString());
  }}, {{passive:true}});
}})();
</script>

<script>
(function() {{
  // Listen for resize notifications from the EMT parent page (charts.html).
  // The parent's ResizeObserver detects the sidebar toggle and posts the
  // new available width — we use it to relayout charts without reloading.
  window.addEventListener('message', function(e) {{
    if (!e.data) return;
    if (e.data.type === 'emt-resize') {{
      var w = e.data.width || window.innerWidth;
      if (typeof _scaleDayCharts === 'function') _scaleDayCharts();
      if (typeof scaleChart === 'function') scaleChart(w);
    }}
    if (e.data.type === 'emt-restore-tables' && Array.isArray(e.data.open)) {{
      e.data.open.forEach(function(id) {{
        var wrap = document.getElementById(id);
        if (!wrap) return;
        var btn = document.querySelector('.day-tbl-toggle[onclick*="' + id + '"]');
        var chartId = id.replace(/^tbl_/, '');
        function _tryOpen() {{
          if (wrap.style.display === 'none' || !wrap.style.display) {{
            toggleDayTables(id, btn);
          }}
        }}
        if (!window._pendingCharts || !window._pendingCharts[chartId]) {{
          _tryOpen();
        }} else {{
          var tries = 0;
          var poll = setInterval(function() {{
            if (!window._pendingCharts || !window._pendingCharts[chartId] || ++tries > 40) {{
              clearInterval(poll);
              _tryOpen();
            }}
          }}, 75);
        }}
      }});
    }}
  }});
}})();
</script>
</body>
</html>
"""

    return html





# ─────────────────────────────────────────────────────────────
# Net heatmap
# ─────────────────────────────────────────────────────────────
