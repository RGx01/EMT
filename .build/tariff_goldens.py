#!/usr/bin/env python3
"""
Golden outputs per tariff shape — what step 5 (the schema) must not move (5 Oct 2026).

For each fixture in `tests/tariff_fixtures.py` (flat, Economy 7, Go, Agile; with and without an EV
charger and a battery), request every data surface the pages draw — bills, Usage Stats, the
heatmap, Insights, the period picker — over September, October and the periods spanning the 1 Oct
VAT change, and record the answers. `tests/test_tariff_goldens.py` asserts they are unchanged.

Every real database on hand is IOG; these are the other tariffs. Left out, because they answer
with the clock rather than the data: the change poll's token, the per-day fingerprints, and Live
Power's today / this bill. Keys that say whether a period is current are stripped for the same
reason, and floats are rounded to 6 dp.

    python3 .build/tariff_goldens.py                 # compare, print what moved
    python3 .build/tariff_goldens.py --update        # rewrite tests/goldens/
    python3 .build/tariff_goldens.py --dump DIR      # write every full answer, to diff

The goldens hold a SHA-256 of each normalised answer, not the answer (the bodies are ~14 MB, most
of it the heatmap's block months). When one moves, `--dump` on the parent and on the change gives
two trees to diff.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDENS = os.path.join(ROOT, "tests", "goldens")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

REQUESTS = [
    ("Usage Stats: all days",        "/api/energy", "resolution=day&kind=all&fields=stats"),
    ("Usage Stats: days over 1 Oct", "/api/energy", "resolution=day&kind=range&start=2026-09-28&end=2026-10-03&fields=stats"),
    ("Usage Stats: 30 Sep",          "/api/energy", "resolution=block&kind=day&date=2026-09-30&fields=stats"),
    ("Usage Stats: 1 Oct",           "/api/energy", "resolution=block&kind=day&date=2026-10-01&fields=stats"),
    ("Heatmap: September",           "/api/energy", "resolution=block&kind=month&year=2026&month=9"),
    ("Heatmap: October",             "/api/energy", "resolution=block&kind=month&year=2026&month=10"),
    ("Bill: September",              "/api/energy", "resolution=period&kind=month&year=2026&month=9&include=billing"),
    ("Bill: October",                "/api/energy", "resolution=period&kind=month&year=2026&month=10&include=billing"),
    ("Bill: billing period over 1 Oct", "/api/energy", "resolution=period&kind=billing&period_start=2026-09-03&include=billing"),
    ("Bill: Q3",                     "/api/energy", "resolution=period&kind=quarter&year=2026&quarter=3&include=billing"),
    ("Bill: Q4",                     "/api/energy", "resolution=period&kind=quarter&year=2026&quarter=4&include=billing"),
    ("Bill: 2026",                   "/api/energy", "resolution=period&kind=year&year=2026&include=billing"),
    ("Day charts: October",          "/api/energy", "resolution=day&kind=month&year=2026&month=10&include=billing"),
    ("Insights carbon: September",   "/api/energy", "resolution=period&kind=month&year=2026&month=9&include=carbon"),
    ("Insights usage: October",      "/api/energy", "resolution=period&kind=month&year=2026&month=10&include=usage"),
    ("Periods: picker totals",       "/api/periods", "kind=billing&include=net_cost"),
    ("Periods: trend strip",         "/api/periods", "kind=month&include=carbon"),
]
CLOCK_KEYS = {"is_current", "fingerprint", "fingerprints", "token", "epoch", "generated_at",
              "config", "days"}


def _normalise(v):
    if isinstance(v, dict):
        return {k: _normalise(x) for k, x in sorted(v.items()) if k not in CLOCK_KEYS}
    if isinstance(v, list):
        return [_normalise(x) for x in v]
    if isinstance(v, float):
        return round(v, 6)
    return v


def fixtures():
    import tariff_fixtures as T
    return [(t, d) for t in T.TARIFFS for d in (False, True)] + [("e7", "many"), ("e7", "over"), ("e7", "gap")]


def name(tariff, devices):
    return "%s%s" % (tariff, ("_" + devices) if isinstance(devices, str) else "_devices" if devices else "")


def capture(tariff, devices):
    """{request name: normalised answer} for one fixture."""
    import store_access
    import tariff_fixtures as T
    d = tempfile.mkdtemp()
    path = T.build(os.path.join(d, "blocks.db"), tariff, devices)
    spec = importlib.util.spec_from_file_location("server_tg", os.path.join(ROOT, "web", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    srv.DATA_DIR = d
    store_access.reset_for_tests()
    store_access.bind(path)
    out = {}
    try:
        client = srv.app.test_client()
        for label, route, q in REQUESTS:
            r = client.get(route + "?" + q)
            out[label] = {"status": r.status_code, "body": _normalise(r.get_json())}
    finally:
        store_access.reset_for_tests()
    return out


def digest(answers):
    """{request: {"status", "sha256"}} — what the goldens hold."""
    return {k: {"status": v["status"],
                "sha256": hashlib.sha256(json.dumps(v["body"], sort_keys=True,
                                                    separators=(",", ":")).encode()).hexdigest()}
            for k, v in answers.items()}


def golden_path(tariff, devices):
    return os.path.join(GOLDENS, "tariff_%s.json" % name(tariff, devices))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--dump")
    a = ap.parse_args()
    os.makedirs(GOLDENS, exist_ok=True)
    moved = 0
    for tariff, devices in fixtures():
        full = capture(tariff, devices)
        if a.dump:
            os.makedirs(a.dump, exist_ok=True)
            with open(os.path.join(a.dump, name(tariff, devices) + ".json"), "w") as fh:
                json.dump(full, fh, indent=1, sort_keys=True)
        got = digest(full)
        p = golden_path(tariff, devices)
        if a.update:
            with open(p, "w") as fh:
                json.dump(got, fh, indent=1, sort_keys=True)
                fh.write("\n")
            print("wrote", os.path.relpath(p, ROOT))
            continue
        want = json.load(open(p)) if os.path.exists(p) else {}
        for label in got:
            if got[label] != want.get(label):
                moved += 1
                print("MOVED  %-18s %s" % (name(tariff, devices), label))
    if not a.update:
        print("%d answer(s) moved" % moved)
        sys.exit(1 if moved else 0)


if __name__ == "__main__":
    main()
