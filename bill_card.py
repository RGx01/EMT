"""
The Billing tab's bill card and day side panel, as ROWS — C, 30 Sep 2026.

Everything the card shows is decided here: which sections appear, in what order,
every figure rounded, summed, banded and collapsed, and formatted as the string the
card prints. A renderer (the browser's billing_module.html, and until the pre-render
is deleted energy_charts.render_billing_summary) turns rows into table markup and
decides nothing.

It was the other way round for a day. #21 ported render_billing_summary to JavaScript,
arithmetic included — the EV/Home band clustering, the Agile collapse, per-row
rounding — which put that arithmetic where the suite cannot run it, and the one
browser check that held it against Python needed the pre-rendered page that step 3
deletes. Moving the rows here keeps it tested in the suite for good.

Split from energy_charts' _bill_rate_rows / _bill_total_row / _bill_split_rows /
render_billing_summary and build_day_chart_html's side panel, unchanged in behaviour:
formatting these rows with the old templates reproduces the pre-rendered page
byte for byte (md5 of the whole page, on a real database).

Takes calculate_billing_summary_for_period's dict (rate-keyed maps, `standing` keyed
by date) — the Python form, before billing_view.summary_json. Pure: no store.
"""

# Above this many distinct rates in one channel's breakdown, collapse the per-rate rows
# into a single kWh-weighted average row. Agile has ~48 rates a day → hundreds across a
# bill period; the Total row is unchanged, so only the unusably long detail folds.
MAX_RATE_ROWS = 5

# Rate keys within this gap (£/kWh) are the SAME tariff band jittered by Octopus's
# per-half-hour settlement rounding (e.g. 0.323091/0.323092/0.323097). Sub-penny, so it
# never merges two real IOG bands (off-peak ≈5p vs peak ≈32p) — and the cap-transition
# block is a separate 'mixed' bucket, never grouped by rate at all.
SPLIT_BAND_EPS = 0.0015


def paren(v, n):
    """A negative figure in parentheses, as a bill prints it."""
    return f"({-v:.{n}f})" if v < 0 else f"{v:.{n}f}"


def rate_rows(channels):
    """Per-rate rows for a channel {rate: {kwh, cost}}, ascending by rate.
    Returns (rows, total_kwh, total_cost): totals are the sum of the ROUNDED per-rate
    values, which is what the Total row prints. More than MAX_RATE_ROWS rates collapse
    to ONE kWh-weighted average row, with the totals computed identically."""
    rates = sorted(channels)
    total_kwh = 0.0
    total_cost = 0.0
    if len(rates) > MAX_RATE_ROWS:
        for rate in rates:
            total_kwh  += round(channels[rate]["kwh"],  3)
            total_cost += round(channels[rate]["cost"], 2)
        avg = (total_cost / total_kwh) if total_kwh else 0.0
        rows = [{"type": "rate", "rate": f"{avg:.4f}", "note": {"avg": len(rates)},
                 "kwh": f"{total_kwh:.3f}", "cost": paren(total_cost, 2)}]
        return rows, round(total_kwh, 3), round(total_cost, 2)
    rows = []
    for rate in rates:
        d = channels[rate]
        kwh      = round(d["kwh"],  3)
        cost_val = round(d["cost"], 2)
        total_kwh  += kwh
        total_cost += cost_val
        rows.append({"type": "rate", "rate": f"{rate:.4f}", "note": None,
                     "kwh": f"{kwh:.3f}", "cost": paren(cost_val, 2)})
    return rows, round(total_kwh, 3), round(total_cost, 2)


def total_row(kwh, cost):
    return {"type": "total", "kwh": f"{kwh:.3f}", "cost": paren(cost, 2)}


def split_rows(summary, exc=False):
    """BL-9: EV/Home rows for the IOG 'Import — total grid' section — the house-vs-car
    split Octopus itemises. EV and Home interleave per rate band; near-identical rates
    collapse into one band (SPLIT_BAND_EPS); the cap-transition blocks are one EV and
    one Home row at their blended average. The rate is cost ÷ kWh, so it is right in
    both the inc and the (derived) ex-VAT basis. None when the period has no EV."""
    ev_by_rate   = summary.get("ev_by_rate") or {}
    home_by_rate = summary.get("home_by_rate") or {}
    ev_tr        = summary.get("ev_transition") or {}
    home_tr      = summary.get("home_transition") or {}
    _ck  = "cost_exc" if exc else "cost"
    _ckr = _ck + "_raw"                       # B6: unrounded cost for the canonical rate
    if (sum(v["kwh"] for v in ev_by_rate.values()) + (ev_tr.get("kwh") or 0.0)) <= 1e-9:
        return None

    def _row(label, kwh, cost, note=None, rate=None):
        # B1: EV and Home in the SAME band share one per-band rate rather than each
        # re-deriving cost/kWh, which diverges on small bands from per-block rounding.
        if rate is None:
            rate = (cost / kwh) if kwh else 0.0
        return {"type": "split", "label": label, "rate": f"{rate:.4f}", "note": note,
                "kwh": f"{kwh:.3f}", "cost": paren(cost, 3)}      # cost 3dp, as the bill

    _rates = sorted(set(ev_by_rate) | set(home_by_rate))
    _bands = []
    for r in _rates:
        if _bands and (r - _bands[-1][-1]) <= SPLIT_BAND_EPS:
            _bands[-1].append(r)
        else:
            _bands.append([r])

    rows = []
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
        # Shared band rate, kWh-weighted over BOTH series for this band only, from the
        # UNROUNDED cost (B6) so a small band reads the canonical tariff rate.
        _band_rate = ((ecr + hcr) / (ek + hk)) if (ek + hk) > 1e-9 else 0.0
        if ek > 1e-9:
            rows.append(_row("EV", ek, ec, rate=_band_rate))
        if hk > 1e-9:
            rows.append(_row("Home", hk, hc, rate=_band_rate))
    if (ev_tr.get("kwh") or 0.0) > 1e-9:
        _r = (ev_tr.get(_ckr, ev_tr.get(_ck, 0.0)) / ev_tr["kwh"]) if ev_tr["kwh"] > 1e-9 else None
        rows.append(_row("EV", ev_tr["kwh"], ev_tr.get(_ck, 0.0), "transition", rate=_r))
    if (home_tr.get("kwh") or 0.0) > 1e-9:
        _r = (home_tr.get(_ckr, home_tr.get(_ck, 0.0)) / home_tr["kwh"]) if home_tr["kwh"] > 1e-9 else None
        rows.append(_row("Home", home_tr["kwh"], home_tr.get(_ck, 0.0), "transition", rate=_r))
    return rows


def _reads(read_start, read_end):
    if read_start is None:
        return None
    total = (read_end or 0.0) - (read_start or 0.0)
    return {"start": f"{read_start:.3f}", "end": f"{read_end:.3f}", "total": f"{total:.3f}"}


def _standing_groups(standing):
    """{date: amount} → [(rate, days)] — one row per distinct daily rate (rounded to
    4dp), ascending, so a mid-period change shows as separate lines."""
    groups = {}
    for _d, amt in sorted(standing.items()):
        r = round(amt, 4)
        groups[r] = groups.get(r, 0) + 1
    return sorted(groups.items())


def card(summary, currency="£", site_name=None):
    """The card for one period: {"site", "rows"}. `site_name`, when given, heads it;
    otherwise the main meter's own site from the summary, if any."""
    if not summary:
        return None
    meter_meta      = summary.get("meter_meta", {})
    main_import_raw = summary.get("main_import_raw", {})
    main_reads      = summary.get("main_import_reads", {})
    if not site_name:
        site_name = next((m.get("site") for m in meter_meta.values()
                          if m.get("site") and not m.get("is_submeter")), None)
    rows = []

    export_keys    = sorted(k for k in summary["meters"] if k.endswith("/ Export"))
    remainder_keys = sorted(k for k in summary["meters"]
                            if k.endswith("/ Import")
                            and not (summary["totals"].get(k) or {}).get("is_submeter"))
    submeter_keys  = sorted(k for k in summary["meters"]
                            if k.endswith("/ Import")
                            and (summary["totals"].get(k) or {}).get("is_submeter"))

    for meter_name in export_keys:
        totals = summary["totals"].get(meter_name, {})
        meta   = meter_meta.get(meter_name, {})
        rows.append({"type": "title", "text": "Export", "mpan": meta.get("mpan") or None,
                     "reads": _reads(totals.get("read_start"), totals.get("read_end")), "indent": False})
        rows.append({"type": "header", "rate": f"Rate ({currency}/kWh)", "cost": f"Cost ({currency})", "indent": False})
        rows += rate_rows(summary["meters"][meter_name])[0]
        rows.append(total_row(totals["kwh"], totals["cost"]))

    # BL-24: with "Bill Rounding" on, the import charge is shown EX-VAT (the bill method)
    # in place of the inc-VAT rows.
    bm = summary.get("bill_method")
    exc_lbl = ", exc" if bm else ""
    if remainder_keys or submeter_keys:
        mpan = None
        if remainder_keys:
            mpan = meter_meta.get(remainder_keys[0], {}).get("mpan") or None
        rows.append({"type": "title", "text": "Import — total grid", "mpan": mpan,
                     "reads": _reads(main_reads.get("read_start"), main_reads.get("read_end")), "indent": False})
        rows.append({"type": "header", "rate": f"Rate ({currency}/kWh{exc_lbl})",
                     "cost": f"Cost ({currency}{exc_lbl})", "indent": False})
        if bm:
            split = split_rows(summary, exc=True)
            if split is not None:
                rows += split
            else:
                for r in bm["rows"]:
                    rows.append({"type": "split", "label": "", "rate": f"{r['rate_exc']:.4f}",
                                 "note": {"avg": r["n_rates"]} if r.get("collapsed") else None,
                                 "kwh": f"{r['kwh']:.3f}", "cost": f"{r['cost_exc']:.3f}"})
            rows.append({"type": "total_exc", "value": f"{bm['energy_exc']:.2f}"})
            if bm["standing_days"]:
                srows = bm.get("standing_rows") or []
                if len(srows) > 1:
                    for s in srows:
                        rows.append({"type": "line", "cls": "standing",
                                     "text": f"Standing charge (exc): {s['days']} days @ {currency}{s['rate_exc']:.4f}/day",
                                     "value": f"{s['cost_exc']:.2f}"})
                else:
                    rows.append({"type": "line", "cls": "standing",
                                 "text": f"Standing charge (exc): {bm['standing_days']} days @ {currency}{bm['standing_rate_exc']:.4f}/day",
                                 "value": f"{bm['standing_exc']:.2f}"})
            rows.append({"type": "line", "cls": "bill-method", "text": f"VAT @ {bm['vat_rate'] * 100:.0f}%",
                         "value": f"{currency}{bm['vat_amount']:.2f}"})
            rows.append({"type": "line", "cls": "channel-total", "text": "Total incl. VAT",
                         "value": f"{currency}{bm['inc_total']:.2f}"})
        else:
            plain, raw_kwh_r, raw_cost_r = rate_rows(main_import_raw)
            split = split_rows(summary)
            rows += split if split is not None else plain
            rows.append(total_row(raw_kwh_r, raw_cost_r))
            # Standing folded into the import charge, as a real bill does — display
            # only; the Total Bill is summary['total_cost'], computed elsewhere.
            if summary["standing"]:
                for r, cnt in _standing_groups(summary["standing"]):
                    rows.append({"type": "line_block", "cls": "standing",
                                 "text": f"Standing charge: {cnt} days @ {currency}{r:.4f}/day",
                                 "value": f"{r * cnt:.2f}"})
                incl = raw_cost_r + round(sum(summary["standing"].values()), 2)
                rows.append({"type": "line_inline", "cls": "channel-total",
                             "text": "Total incl. standing charge", "value": paren(incl, 2)})

        rows.append({"type": "breakdown"})
        for meter_name in remainder_keys + submeter_keys:
            if meter_name in remainder_keys:
                label = "Direct import"
            else:
                meta = meter_meta.get(meter_name, {})
                label = meta.get("device") or meter_name.split(" / ")[0].replace("(Sub-meter)", "").replace("(sub-meter)", "").strip()
            rows.append({"type": "title", "text": label, "mpan": None, "reads": None, "indent": True})
            rows.append({"type": "header", "rate": f"Rate ({currency}/kWh)", "cost": f"Cost ({currency})", "indent": True})
            rr, tk, tc = rate_rows(summary["meters"][meter_name])
            rows += rr
            rows.append(total_row(tk, tc))

    # Standing for an export-only account (no import section to carry it).
    if summary["standing"] and not (remainder_keys or submeter_keys):
        for r, cnt in _standing_groups(summary["standing"]):
            rows.append({"type": "line_block", "cls": "standing",
                         "text": f"Standing Charge: {cnt} days @ {currency}{r:.4f}/day",
                         "value": f"{r * cnt:.2f}"})

    total = f"{currency}{summary['total_cost']:.2f}"
    rows.append({"type": "line_block", "cls": "grand-total", "text": "Total Bill", "value": total})
    return {"site": site_name, "total": total, "rows": rows}


# ── the day side panel ───────────────────────────────────────────────────────

def panel_lines(panel, currency="£"):
    """day_chart_data's `panel` as the lines the side column prints: {"totals": [...],
    "cols": [[...], ...]}, each line {"text", "color", "size", "bold"}."""
    def ln(text, color, size="0.9em", bold=False):
        return {"text": text, "color": color, "size": size, "bold": bold}

    def rates(rs, color):
        return [ln(f'{kwh:.3f} kWh @ {currency}{rate:.4f}' + (f' (avg of {n})' if n else ''), color, "0.8em")
                for kwh, rate, n in rs]

    main_c, exp_c = panel["main_color"], panel["export_color"]
    totals = []
    if panel["total_import"] > 0:
        totals.append(ln(f'Total import: {panel["total_import"]:.3f} kWh', main_c))
        totals.append(ln(f'Import cost: {currency}{panel["import_cost"]:.2f}', main_c))
        if panel["import_cost_exc"] is not None:
            totals.append(ln(f'Import cost (exc VAT): {currency}{panel["import_cost_exc"]:.2f}', main_c))
    if panel["export_kwh"] > 0:
        totals.append(ln(f'Total export: {panel["export_kwh"]:.3f} kWh', exp_c))
        totals.append(ln(f'Export credit: {currency}{panel["export_cost"]:.2f}', exp_c))
        totals += rates(panel["export_rates"], exp_c)
    cols = []
    for row in panel["breakdown"]:
        cols.append([ln(f'↳ {row["label"]}', row["color"], "1em", True),
                     ln(f'{row["kwh"]:.3f} kWh', row["color"], "1em"),
                     ln(f'{currency}{row["cost"]:.2f}', row["color"], "1em")]
                    + rates(row["rates"], row["rate_color"]))
    return {"totals": totals, "cols": cols}
