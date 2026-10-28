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


def rate_rows(channels, totals=None):
    """Per-rate rows for a channel {rate: {kwh, cost}}, ascending by rate.
    Returns (rows, total_kwh, total_cost): totals are the sum of the ROUNDED per-rate
    values, which is what the Total row prints. More than MAX_RATE_ROWS rates collapse
    to ONE kWh-weighted average row.

    Collapsed, the total is the section's own, rounded once (`totals`, the summary's
    {kwh, cost} for it), when given (6 Oct 2026). Summed from hundreds of per-rate figures each
    rounded to the penny, it drifted: an Agile battery's September read £16.08 on the bill
    against £16.10 on the Overview and Usage Stats. With the rate rows shown, the Total stays
    their sum, so the rows on the page add up."""
    rates = sorted(channels)
    total_kwh = 0.0
    total_cost = 0.0
    if len(rates) > MAX_RATE_ROWS:
        if totals is not None:
            total_kwh, total_cost = float(totals["kwh"]), float(totals["cost"])
        else:
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

    if exc:
        # The ex-VAT table bands on the EX-VAT rate. Keyed by inc rate, the same tariff
        # band either side of a VAT change — 5.493 inc at 5% to 30 Sep 2026, 5.2314 inc
        # at 0% from 1 Oct — showed as two identical-looking 0.0523 rows (v4 4.5.19).
        def _by_exc(series):
            out = {}
            for _r, _v in series.items():
                _k = _v["kwh"]
                _x = (_v.get(_ckr, _v.get(_ck, 0.0)) / _k) if _k > 1e-9 else _r
                g = out.setdefault(round(_x, 6), {"kwh": 0.0, _ck: 0.0, _ckr: 0.0})
                g["kwh"] += _k
                g[_ck] += _v.get(_ck, 0.0)
                g[_ckr] += _v.get(_ckr, _v.get(_ck, 0.0))
            return out
        ev_by_rate, home_by_rate = _by_exc(ev_by_rate), _by_exc(home_by_rate)
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
        # the folded row is the Total, as the import's (28 Oct 2026: an Agile export's differed by pence)
        rows += rate_rows(summary["meters"][meter_name], totals or None)[0]
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
            rows.append({"type": "line", "cls": "bill-method", "text": bm.get("vat_label") or f"VAT @ {bm['vat_rate'] * 100:.0f}%",
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
            rr, tk, tc = rate_rows(summary["meters"][meter_name], summary["totals"].get(meter_name))
            rows += rr
            rows.append(total_row(tk, tc))

    # Standing for an export-only account (no import section to carry it).
    if summary["standing"] and not (remainder_keys or submeter_keys):
        for r, cnt in _standing_groups(summary["standing"]):
            rows.append({"type": "line_block", "cls": "standing",
                         "text": f"Standing Charge: {cnt} days @ {currency}{r:.4f}/day",
                         "value": f"{r * cnt:.2f}"})

    # BL-78 (10 Oct 2026): with Bill Rounding on, the card is the supplier's bill — its lines are
    # summed and rounded once, as Octopus builds one — so its total is those lines' total: the
    # bill method's import incl. VAT less each export credit as printed above. Off, Total Bill is
    # compute_period_net (each local day's net to the penny, summed), which agrees with Usage
    # Stats day by day. The two can differ by pennies; each matches its own lines.
    if bm:
        total_val = round(bm["inc_total"] + sum(
            float((summary["totals"].get(k) or {}).get("cost") or 0.0) for k in export_keys), 2)
    else:
        total_val = summary["total_cost"]
    total = f"{currency}{total_val:.2f}"
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


def live_rows(live, days, cfg, ev_meter_id):
    """Live Power's card rows for a window — `days` (local ISO dates) of
    `BlockStore.get_day_live_rollups(...)`'s figures summed. The same rows, in the same
    order and wording, /api/billing built from window SQL (C, 1 Oct 2026):

      Total Import (raw grid, bold) · Direct import (direct_import's rule, per half-hour) ·
      a row per device, the EV as the hybrid figure in place of the physical EV meter's own
      (#468) · Grid Export · Standing Charge.

    Costs to 2 dp, each row on its own — so, as before, the rows can sum a penny away
    from the window's total (which is the bill's per-day rule, not their sum)."""
    meters_cfg = cfg.get("meters") or {}
    main = {"grid_kwh": 0.0, "imp_cost": 0.0, "exp_kwh": 0.0, "exp_cost": 0.0, "standing": 0.0}
    subs, ev = {}, {"kwh": 0.0, "cost": 0.0}
    direct_kwh = direct_cost = 0.0
    for d in days:
        sc = 0.0
        for mid, m in (live["meters"].get(d) or {}).items():
            if m["is_sub"]:
                acc = subs.setdefault(mid, {"kwh": 0.0, "cost": 0.0})
                acc["kwh"] += m["grid_kwh"]; acc["cost"] += m["imp_cost"]
            else:
                for k in ("grid_kwh", "imp_cost", "exp_kwh", "exp_cost"):
                    main[k] += m[k]
                if m["standing"] > 0 and not sc:
                    sc = m["standing"]           # the day's charge, once (first main meter's)
        main["standing"] += sc
        e = live["ev"].get(d)
        if e:
            ev["kwh"] += e["kwh"]; ev["cost"] += e["cost"]
        x = (live.get("direct") or {}).get(d)
        if x:
            direct_kwh += x["kwh"]; direct_cost += x["cost"]

    def _device(mid):
        return ((meters_cfg.get(mid) or {}).get("meta") or {}).get("device") or mid

    sub_rows = []
    for mid, v in sorted(subs.items()):          # GROUP BY meter_id's order, as the card had
        if not (v["kwh"] > 0.001 or v["cost"] > 0.001) or mid == ev_meter_id:
            continue                             # the hybrid EV replaces the EV meter's own row
        sub_rows.append({"label": "↳ %s (%.3f kWh)" % (_device(mid), v["kwh"]),
                         "cost": round(v["cost"], 2), "bold": False})
    ev_cost = round(ev["cost"], 2)
    if ev["kwh"] > 0.001 or abs(ev_cost) > 0.001:
        label = ((((meters_cfg.get(ev_meter_id) or {}).get("meta") or {}).get("device") or "EV")
                 if ev_meter_id else "EV (from dispatch)")
        sub_rows.append({"label": "↳ %s (%.3f kWh)" % (label, ev["kwh"]), "cost": ev_cost, "bold": False})

    rows = []
    total_cost = round(main["imp_cost"], 2)
    direct_cost = round(direct_cost, 2)
    if main["grid_kwh"] > 0.001 or total_cost > 0.001:
        rows.append({"label": "Total Import (%.3f kWh)" % main["grid_kwh"], "cost": total_cost, "bold": True})
    if direct_kwh > 0.001 or direct_cost > 0.001:
        rows.append({"label": "Direct import (%.3f kWh)" % direct_kwh, "cost": direct_cost, "bold": False})
    rows.extend(sub_rows)
    if round(main["exp_kwh"], 4) > 0.001:
        rows.append({"label": "Grid Export (%.3f kWh)" % round(main["exp_kwh"], 4),
                     "cost": -round(round(main["exp_cost"], 4), 2), "bold": False})
    standing = round(round(main["standing"], 4), 2)
    if standing > 0.001:
        rows.append({"label": "Standing Charge", "cost": standing, "bold": False})
    return rows
