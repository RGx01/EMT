#!/usr/bin/env python3
"""
Queries per surface — the half of workstream D's budget that CI can assert (5 Oct 2026).

For each surface `.build/soak.py` names, issue its request through the Flask test client against
a synthetic database (`tests/surface_fixture.py`) and count the SQL statements the request runs
on its read connection (SQLite's trace callback sees every one). Two properties, both
deterministic — the same on a laptop, a CI runner and a Pi:

  * a BUDGET per surface: no more statements than `.build/query_budgets.json` records;
  * SCALE INVARIANCE: the count does not grow with history. A surface issuing a query per day
    or per period — the "200 small queries" shape the programme map named as the likely cause of
    slowness — is caught here on any machine, long before a Pi soak would show it as time.

Time to first byte is NOT asserted here: a runner's timings say nothing about a Pi on SD storage.
That stays with `.build/soak.py` and `.build/soak_budgets.json` (ROADMAP §D).

    python3 .build/query_budgets.py            # print the counts
    python3 .build/query_budgets.py --update   # rewrite .build/query_budgets.json
"""
import argparse
import importlib.util
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUDGETS = os.path.join(ROOT, ".build", "query_budgets.json")
SMALL_DAYS, LARGE_DAYS = 40, 200
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, ".build"))


def _params(params):
    """A soak surface's parameters, filled with dates the fixture holds (September 2026)."""
    q = {k: v for k, v in params.items() if v is not None}
    if q.get("year") == "*":
        q["year"], q["month"] = "2026", "9"
    kind = q.get("kind")
    if kind in ("year", "quarter", "month"):
        q.setdefault("year", "2026")
    if kind == "quarter":
        q["quarter"] = "3"
    if kind == "month":
        q.setdefault("month", "9")
    if kind == "range":
        q["start"], q["end"] = "2026-09-01", "2026-09-07"
    if kind == "day" and q.get("resolution") == "block":
        q["date"] = "2026-09-15"
    return q


def measure(days):
    """{surface: (status, statements)} for a fixture of `days` days."""
    import soak
    import store_access
    import surface_fixture
    d = tempfile.mkdtemp()
    path = surface_fixture.build(os.path.join(d, "blocks.db"), days)
    spec = importlib.util.spec_from_file_location("server_qb", os.path.join(ROOT, "web", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    srv.DATA_DIR = d
    store_access.reset_for_tests()
    store_access.bind(path)
    client = srv.app.test_client()
    out = {}
    try:
        for name, route, params in soak.SURFACES:
            url = route + "?" + "&".join("%s=%s" % kv for kv in _params(params).items())
            client.get(url)                                  # warm caches, open the connection
            conn = store_access.store()._conn
            seen = []
            conn.set_trace_callback(seen.append)
            try:
                r = client.get(url)
            finally:
                conn.set_trace_callback(None)
            out[name] = (r.status_code, len(seen))
    finally:
        store_access.reset_for_tests()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true")
    a = ap.parse_args()
    small, large = measure(SMALL_DAYS), measure(LARGE_DAYS)
    for name in small:
        print("%-34s %3d  %3d statements (%d days)  %3d (%d days)"
              % (name, small[name][0], small[name][1], SMALL_DAYS, large[name][1], LARGE_DAYS))
    if a.update:
        with open(BUDGETS, "w") as fh:
            json.dump({n: max(small[n][1], large[n][1]) for n in small}, fh, indent=2,
                      sort_keys=True)
            fh.write("\n")
        print("wrote", BUDGETS)


if __name__ == "__main__":
    main()
