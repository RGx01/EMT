#!/usr/bin/env python3
"""
C, Billing tab step 1: /api/energy?include=billing gives EXACTLY what the pre-rendered
Billing page holds, on a real database.

    python3 .build/billing_payload_parity.py --db blocks.db

The page (energy_charts.generate_daily_import_export_charts) is built from ALL of
history at once. The API builds one range at a time (billing_view). They call the same
functions, so what can differ is only the INPUTS: the range's blocks instead of every
block, the store-wide ex-VAT gate asked in SQL, the period list, local bounds, the
EV slot map built from one period. This checks every one of them by the output:

  day     each day's chart JSON (`data_chart_<day>`) equals billing_view.day_billing's
          `chart`, and its side panel formats to the page's panel markup;
  period  every billing period, month, quarter and year: render_billing_summary of
          billing_view.period_billing's summary is a bill card the page holds, and
          the summary survives JSON unchanged (what the browser will be given).

It never writes to --db (a private copy is opened) and prints no money: PASS/FAIL,
counts, and the first differing path.
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FMT = "%Y-%m-%dT%H:%M:%S"


def _first_diff(a, b, path="$"):
    if type(a) is not type(b):
        return "%s: type %s != %s" % (path, type(a).__name__, type(b).__name__)
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a or k not in b:
                return "%s.%s: present on one side only" % (path, k)
            d = _first_diff(a[k], b[k], "%s.%s" % (path, k))
            if d:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return "%s: length %d != %d" % (path, len(a), len(b))
        for i, (x, y) in enumerate(zip(a, b)):
            d = _first_diff(x, y, "%s[%d]" % (path, i))
            if d:
                return d
        return None
    return None if a == b else "%s: differs" % path


def _utc(dt, tz):
    dt = dt if dt.tzinfo else dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc).strftime(FMT)


def run(db, limit_days=None):
    import energy_charts as ec
    import billing
    import billing_view as bv
    from block_store import BlockStore

    with tempfile.TemporaryDirectory() as d:
        copy = os.path.join(d, "blocks.db")
        with sqlite3.connect("file:%s?mode=ro" % os.path.abspath(db), uri=True) as s, \
                sqlite3.connect(copy) as t:
            s.backup(t)
        st = BlockStore(copy)
        cfg = st.config_from_db(st.get_current_config_period_id())
        mm = bv._main_meta(cfg)
        tz_name = mm.get("timezone") or "UTC"
        tz = ZoneInfo(tz_name)
        bm = int(mm.get("block_minutes") or 30)
        cur = mm.get("currency_symbol", "£")
        cap_from, sched = bv.rate_context()
        blocks = st.get_blocks_lightweight()
        page = ec.generate_daily_import_export_charts(
            blocks, timezone_name=tz_name, block_minutes=bm, currency=cur, cfg=cfg,
            store=st, cap_from=cap_from, import_schedule=sched)
        lines, fails = [], 0

        # ── days ──
        embedded = {m.group(1).replace("_", "-"): json.loads(m.group(2)) for m in re.finditer(
            r'<script type="application/json" id="data_chart_(\d{4}_\d\d_\d\d)">(.*?)</script>', page)}
        panels = {m.group(1): m.group(0) for m in re.finditer(
            r'<div class="chart-summary"><div class="day-label">(\d{4}-\d\d-\d\d)</div>.*?</div>(?=\s*<div id="chart_)',
            page, re.S)}
        days = sorted(embedded)
        if limit_days:
            days = days[-limit_days:]
        bad_days = []
        # One request per billing period, as a client would ask.
        periods = billing.get_billing_periods_from_config_periods(st.get_config_periods(), tz=tz)
        got = {}
        for ps, pe in periods:
            for row in bv.day_billing(st, cfg, tz_name, _utc(ps, tz), _utc(pe, tz))["days"]:
                got[row["day"]] = row
        for day in days:
            row = got.get(day)
            if row is None:
                bad_days.append((day, "not served"))
                continue
            diff = _first_diff(embedded[day], json.loads(json.dumps(row["chart"])))
            if diff:
                bad_days.append((day, diff))
                continue
            if day in panels and panels[day] not in _panel_html(ec, day, row, cur):
                bad_days.append((day, "side panel markup differs"))
        fails += len(bad_days)
        lines.append("days     %d checked, %d differ" % (len(days), len(bad_days)))
        lines += ["  %s  %s" % x for x in bad_days[:10]]

        # ── periods ──
        page_site = _page_site()
        # The page marks the current period's card `billing-summary current-period`
        # (a highlight); the card itself is render_billing_summary's, so normalise it.
        cards = {c.replace('<div class="billing-summary current-period">', '<div class="billing-summary">', 1)
                 for c in re.findall(r'\n    <div class="billing-summary(?: current-period)?">.*?\n      </table>\n    </div>',
                                     page, re.S)}
        kinds = [("billing", periods)]
        first, last = billing._bounds_from_blocks(blocks, tz)
        for k in ("month", "quarter", "year"):
            kinds.append((k, billing.calendar_periods(k, first, last)))
        bad_periods, n = [], 0
        for kind, plist in kinds:
            for ps, pe in plist:
                meta = {"kind": kind}
                out = bv.period_billing(st, cfg, tz_name, _utc(ps, tz), _utc(pe, tz), meta)
                if not out["days"]:
                    continue                       # the page drops empty periods too
                n += 1
                raw, _, _ = bv.period_summary_raw(st, cfg, tz_name, _utc(ps, tz), _utc(pe, tz))
                # The page heads its card with the site from the legacy
                # meters_config.json; the API with the DB config's (the source of
                # truth). A deliberate difference, so format with the page's.
                card = ec.render_billing_summary(raw, currency=cur, site_name=page_site)
                if bv.summary_json(raw) != out["summary"]:
                    bad_periods.append(("%s %s" % (kind, ps.date()), "served summary is not this summary"))
                    continue
                label = "%s %s" % (kind, ps.date())
                if card not in cards:
                    bad_periods.append((label, "bill card not on the page"))
                    continue
                rt = json.loads(json.dumps(out["summary"]))
                if rt != out["summary"]:
                    bad_periods.append((label, "summary does not survive JSON: %s"
                                        % _first_diff(out["summary"], rt)))
        fails += len(bad_periods)
        lines.append("periods  %d checked, %d differ" % (n, len(bad_periods)))
        lines += ["  %s  %s" % x for x in bad_periods[:10]]
        lines.append("verdict: %s" % ("PASS" if not fails else "FAIL"))
        st.close()
        return "\n".join(lines), fails == 0


def _page_site():
    """The site name exactly as generate_daily_import_export_charts reads it."""
    try:
        from energy_engine_io import load_json
        cfg = load_json("/data/energy_meter_tracker/meters_config.json", {})
        meta = next(((m.get("meta") or {}) for m in cfg.get("meters", {}).values()
                     if not (m.get("meta") or {}).get("sub_meter")), {})
        return meta.get("site") or None
    except Exception:
        return None


def _panel_html(ec, day, row, currency):
    """Format the served panel with the page's own renderer (build_day_chart_html's
    markup half) by feeding it back through day_chart_data's result shape."""
    orig = ec.day_chart_data
    try:
        ec.day_chart_data = lambda *a, **k: {"chart": row["chart"], "panel": row["panel"]}
        return ec.build_day_chart_html(day, [], {}, currency=currency)
    finally:
        ec.day_chart_data = orig


def main(argv=None):
    ap = argparse.ArgumentParser(description="C: /api/energy include=billing vs the pre-rendered page")
    ap.add_argument("--db", required=True, help="a blocks.db — read, never written")
    ap.add_argument("--last-days", type=int, help="only the last N days")
    a = ap.parse_args(argv)
    report, ok = run(a.db, a.last_days)
    print(report)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
