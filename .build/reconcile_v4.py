#!/usr/bin/env python3
"""
C acceptance 7: billing totals reconcile EXACTLY with v4, to the penny, on the same
database — before any surface is switched onto /api/energy.

    python3 .build/reconcile_v4.py --v4 ~/code/energy-meter-tracker-addon --db blocks.db

THE REFERENCE is v4's own code, loaded from a v4 checkout (4.5.16): the Billing
card's total is `BlockStore.compute_period_net(utc_s, utc_e, tz)`. Comparing v5's
carried copy of a function with itself proves nothing, so v4's module is imported
from v4's tree, under its own name.

WHAT IS CHECKED against it, per billing period and per calendar month:

    v5 card     v5's compute_period_net — the same Billing card, after A's changes
    v5 client   /api/energy?resolution=day as a client will use it: the local-day
                rollups (`get_day_rollups`, exactly what the route serves) folded
                with the bill's per-day rule, round(imp + standing - export, 2) on
                the MAIN meter. This is the new derivation, and the one the new
                Charts page will make.
    v5 period   /api/energy?resolution=period&include=usage — its `net_cost`. Until
                30 Sep 2026 that was `_aggregate_usage`'s once-rounded total, up to
                3p from v4 in 49 of 80 real periods; it now comes from the day rule.
                `--legacy-period` measures the old figure (the six legacy routes
                still serve it).

A period PASSES when all three equal v4 to the penny.

A period that differs is EXPLAINED when all three equal v4's own code run on a copy
with the sub-meter rows removed. v4 bills a day's import as round(direct, 4) +
Σ round(sub, 4), with direct clamped at zero; v5 bills round(main, 4), the main meter's
cost (the clamp overstated a 4.5.14 bill by 90%, so v5 dropped it). With no sub-meters
the two are the same rule, in v4's code. On a production-like database (3 Oct 2026)
that rounding moved 3 days of 1,214 across a half-penny, 1p each, in 6 of 81 periods.
An explained difference is by design and does not fail the verdict; any other does.

It never writes to --db: each implementation opens its own temporary copy (v4's
store may run schema DDL when it opens). It prints no money unless --show-figures
is given, so its output can be pasted into an issue (CLAUDE.md: no real customer
figures) — only PASS/FAIL, how many pence apart, and where.
"""
import argparse
import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FMT = "%Y-%m-%dT%H:%M:%S"


def _copy(db, d, name):
    """A consistent private copy (SQLite backup API, so a live WAL is captured)."""
    dst = os.path.join(d, name)
    with sqlite3.connect("file:%s?mode=ro" % os.path.abspath(db), uri=True) as s, \
            sqlite3.connect(dst) as t:
        s.backup(t)
    return dst


def _without_sub_meters(path):
    """Delete every sub-meter's blocks from a private copy: v4's import rule then
    reduces to the main meter's cost, v5's rule (see the module docstring)."""
    with sqlite3.connect(path) as c:
        c.execute("DELETE FROM blocks WHERE EXISTS (SELECT 1 FROM meters m "
                  "WHERE m.meter_id = blocks.meter_id "
                  "AND m.config_period_id = blocks.config_period_id AND m.is_sub_meter = 1)")
    return path


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def client_net_from_days(rows):
    """What a client makes of `resolution=day` — `billing.bill_net_from_day_rollups`,
    the one rule the period payload now uses too."""
    import billing
    return billing.bill_net_from_day_rollups(rows)


def _period_net(server, store, cfg, s, e, tz_name):
    """What /api/energy?resolution=period&include=usage returns as `net_cost`:
    `_aggregate_usage`, with the bill total from the day rule (see api_energy)."""
    import billing
    return billing.bill_net_from_day_rollups(store.get_day_rollups(s, e, tz_name))


def periods(v5store, billing, tz):
    """(label, utc_start, utc_end): every billing period and calendar month held."""
    out = []
    try:
        for p in billing.get_billing_periods_from_config_periods(v5store.get_config_periods(), tz) or []:
            s, e = (p[0], p[1]) if isinstance(p, (tuple, list)) else (p["start"], p["end"])
            out.append(("billing " + str(s)[:10], s, e))
    except Exception as ex:
        print("warning: billing periods unavailable (%s)" % ex)
    row = v5store._conn.execute(
        "SELECT MIN(block_start), MAX(block_start) FROM blocks "
        "WHERE meter_id='electricity_main'").fetchone()
    if row and row[0]:
        lo = datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc).astimezone(tz)
        hi = datetime.fromisoformat(row[1]).replace(tzinfo=timezone.utc).astimezone(tz)
        y, m = lo.year, lo.month
        while (y, m) <= (hi.year, hi.month):
            ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
            out.append(("month %04d-%02d" % (y, m), datetime(y, m, 1, tzinfo=tz),
                        datetime(ny, nm, 1, tzinfo=tz)))
            y, m = ny, nm
    res = []
    for label, s, e in out:
        s = s if getattr(s, "tzinfo", None) else s.replace(tzinfo=tz)
        e = e if getattr(e, "tzinfo", None) else e.replace(tzinfo=tz)
        res.append((label, s.astimezone(timezone.utc).strftime(FMT),
                    e.astimezone(timezone.utc).strftime(FMT)))
    return res


def run(v4_root, db, show=False, limit=None, legacy=False):
    sys.path.insert(0, ROOT)
    with tempfile.TemporaryDirectory() as d:
        v4db, v5db = _copy(db, d, "v4.db"), _copy(db, d, "v5.db")
        v4mdb = _without_sub_meters(_copy(db, d, "v4main.db"))
        v4bs = _load(os.path.join(v4_root, "block_store.py"), "v4_block_store")
        import block_store as v5bs
        import billing
        server = _load(os.path.join(ROOT, "web", "server.py"), "emt_server_reconcile")
        v4 = v4bs.BlockStore(v4db)
        v4m = v4bs.BlockStore(v4mdb)       # v4's code, v5's import rule
        v5 = v5bs.BlockStore(v5db)
        cp = v5.get_current_config_period_id()
        cfg = v5.config_from_db(cp) if cp is not None else {"meters": {}}
        tz_name = next((m.get("meta", {}).get("timezone") for m in (cfg.get("meters") or {}).values()
                        if m.get("meta", {}).get("timezone")), None) \
            or (v5.get_config_period(cp) or {}).get("timezone") or "UTC"
        tz = ZoneInfo(tz_name)
        todo = periods(v5, billing, tz)
        if limit:
            todo = todo[-limit:]
        lines, fails, explained = ["timezone %s; %d periods" % (tz_name, len(todo))], 0, 0
        worst = {"card": 0, "client": 0, "period": 0}
        for label, s, e in todo:
            ref = round(v4.compute_period_net(s, e, tz_name), 2)
            got = {
                "card": round(v5.compute_period_net(s, e, tz_name), 2),
                "client": client_net_from_days(v5.get_day_rollups(s, e, tz_name)),
                "period": (round(server._aggregate_usage(v5, cfg, s, e, tz_name)["net_cost"], 2)
                           if legacy else _period_net(server, v5, cfg, s, e, tz_name)),
            }
            pence = {k: round((v - ref) * 100) for k, v in got.items()}
            for k, p in pence.items():
                worst[k] = max(worst[k], abs(p))
            bad = {k: p for k, p in pence.items() if p}
            why = ""
            if bad:
                ref_m = round(v4m.compute_period_net(s, e, tz_name), 2)
                if all(round((v - ref_m) * 100) == 0 for v in got.values()):
                    explained += 1
                    why = "  (explained: v4 rebuilds the import from sub-meters)"
                else:
                    fails += 1
            tag = ("PASS" if not bad else
                   ("DIFF " if why else "FAIL ") + ", ".join("%s %+dp" % kv for kv in bad.items()))
            fig = ("  v4 %.2f  " % ref + "  ".join("%s %.2f" % kv for kv in got.items())) if show else ""
            lines.append("  %-20s %s%s%s" % (label, tag, why, fig))
        lines.append("\nworst difference from v4, in pence: " +
                     ", ".join("%s %d" % kv for kv in worst.items()))
        lines.append("verdict: %s (%d of %d periods differ unexplained; %d by v4's sub-meter rebuild)"
                     % ("PASS" if not fails else "FAIL", fails, len(todo), explained))
        for st in (v4, v4m, v5):
            try:
                st.close()
            except Exception:
                pass
        return "\n".join(lines), fails == 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="C acceptance 7: v5 billing totals vs v4, to the penny")
    ap.add_argument("--v4", required=True, help="a v4 checkout (4.5.16)")
    ap.add_argument("--db", required=True, help="a blocks.db — read, never written")
    ap.add_argument("--show-figures", action="store_true",
                    help="print the money (do not paste the output anywhere public)")
    ap.add_argument("--last", type=int, help="only the last N periods")
    ap.add_argument("--legacy-period", action="store_true",
                    help="measure _aggregate_usage's own net_cost (the six legacy routes)")
    a = ap.parse_args(argv)
    report, ok = run(a.v4, a.db, a.show_figures, a.last, a.legacy_period)
    print(report)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
