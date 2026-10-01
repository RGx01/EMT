#!/usr/bin/env python3
"""
stats_parity.py — /api/energy's Usage Stats rows against the two routes they replace.

C step 1 (1 Oct 2026). Runs EMT's real Flask app against a COPY of a database and asks for
`/api/charts/blocks-summary` and `/api/energy?resolution=day&kind=all&include=stats`, and for
each date given, `/api/charts/blocks-day?date=` and `/api/energy?resolution=block&kind=day
&date=&include=stats`. Reports whether the day rows and page metadata are identical, and per
date whether the half-hour rows are (a clock-change day differs on purpose: 46 or 50 slots
where blocks-day always drew 48), and that each date's slots sum to its day row.

    python3 .build/stats_parity.py <blocks.db> [YYYY-MM-DD ...]

Exit 1 on any difference other than a clock-change day's slot count.
"""
import importlib.util
import os
import shutil
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "web"))


def main(argv):
    src, dates = argv[0], argv[1:] or ["2025-10-26", "2026-03-29", "2026-08-20"]
    d = tempfile.mkdtemp()
    db = os.path.join(d, "blocks.db")
    shutil.copy(src, db)
    import store_access
    store_access.bind(db)
    spec = importlib.util.spec_from_file_location("server", os.path.join(REPO, "web", "server.py"))
    server = importlib.util.module_from_spec(spec)
    sys.modules["server"] = server
    spec.loader.exec_module(server)
    server.DATA_DIR = d
    c = server.app.test_client()
    get = lambda u: c.get(u).get_json()
    bad = 0
    old = get("/api/charts/blocks-summary")
    new = get("/api/energy?resolution=day&kind=all&include=stats")["stats"]
    same = new["rows"] == old["rows"]
    meta = all(new[k] == old[k] for k in ("currency", "billing_day", "block_minutes", "meters",
                                          "export_color", "has_postcode"))
    print("days: %d rows, identical=%s, metadata identical=%s" % (len(new["rows"]), same, meta))
    bad += (not same) + (not meta)
    days = {"%04d-%02d-%02d" % (r["year"], r["month"], r["day"]): r for r in new["rows"]}
    for ds in dates:
        o = get("/api/charts/blocks-day?date=" + ds)["rows"]
        n = get("/api/energy?resolution=block&kind=day&date=%s&include=stats" % ds)["stats"]["rows"]
        strip = lambda r: {k: v for k, v in r.items() if k != "slot"}
        ident = len(o) == len(n) and all(strip(a) == strip(b) for a, b in zip(o, n))
        sums = ""
        if ds in days:
            off = [f for f in ("imp_kwh", "exp_kwh", "imp_cost", "standing")
                   if abs(sum(r[f] or 0 for r in n) - (days[ds][f] or 0)) > 0.011]
            sums = "slots sum to the day" if not off else "slots do NOT sum: %s" % off
            bad += bool(off)
        dst = len(n) in (46, 50)
        print("%s: blocks-day %d, stats %d, %s%s; %s" % (
            ds, len(o), len(n), "identical" if ident else "differ",
            " (clock change)" if dst and not ident else "", sums))
        bad += (not ident) and not dst
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
