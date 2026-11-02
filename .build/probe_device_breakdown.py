#!/usr/bin/env python3
"""
Ask Octopus, read-only, what its device breakdown says for one night: every bucket's raw label,
kWh and unit rate, per half-hour. For checking a settled EV/Home split against the source.

    KRAKEN_API_KEY=... python3 .build/probe_device_breakdown.py DB [--day 2026-10-10] [--day ...]

The account number and MPAN are read from DB (opened immutable; nothing is written). The key is
taken from the environment only, and never printed. Each --day covers 00:00–06:00 UTC, the
dispatch window. Each used bucket shows how v5 and v4 classify its label (home / ev / None).
"""
import argparse
import importlib.util
import json
import os
import sqlite3
import sys
import types
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRAPHQL = "https://api.octopus.energy/v1/graphql/"


def _bucket_side(path):
    """bucket_side from a kraken_api_client.py, loaded without its HTTP library: only the label
    rule is wanted (stdlib only, so this runs from any python3)."""
    if not os.path.exists(path):
        return None
    sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))
    spec = importlib.util.spec_from_file_location("kac_%d" % abs(hash(path)), path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.bucket_side


v5_side = _bucket_side(os.path.join(_ROOT, "kraken_api_client.py"))
# v4's own reading of a label, beside v5's: the v4 repo, if it is checked out next to this one.
v4_side = _bucket_side(os.path.expanduser("~/code/energy-meter-tracker-addon/kraken_api_client.py"))


def _post(query, variables, token=None):
    req = urllib.request.Request(GRAPHQL, data=json.dumps(
        {"query": query, "variables": variables}).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 **({"Authorization": "JWT " + token} if token else {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = json.loads(r.read())
    if body.get("errors"):
        sys.exit("Octopus answered with errors: %s" % [e.get("message") for e in body["errors"]])
    return body.get("data") or {}


QUERY = (
    "query breakdown($acc: String!, $mpan: String!, $start: DateTime!, $end: DateTime!) {"
    "  account(accountNumber: $acc) { properties {"
    "    measurements(first: 100, startAt: $start, endAt: $end, timezone: \"Europe/London\", "
    "      utilityFilters: [{ electricityFilters: { readingFrequencyType: THIRTY_MIN_INTERVAL, "
    "        marketSupplyPointId: $mpan, readingDirection: CONSUMPTION } }]) {"
    "      edges { node { value ... on IntervalMeasurementType { startAt "
    "        metaData { statistics { type label value "
    "          costInclTax { estimatedAmount pricePerUnit { amount } } } } } } } } } } }")


def _from_db(path):
    from urllib.parse import quote
    c = sqlite3.connect("file:%s?immutable=1" % quote(os.path.abspath(path)), uri=True)
    try:
        acct = c.execute("SELECT value FROM kraken_state WHERE key='kraken_account_number'").fetchone()
        mpan = c.execute("SELECT mpan FROM measured_cost WHERE direction='CONSUMPTION' "
                         "ORDER BY fetched_at DESC LIMIT 1").fetchone()
    finally:
        c.close()
    return (acct[0] if acct else None), (mpan[0] if mpan else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--day", action="append", default=None)
    a = ap.parse_args()
    key = os.environ.get("KRAKEN_API_KEY", "").strip()
    if not key:
        sys.exit("set KRAKEN_API_KEY in the environment")
    acct, mpan = _from_db(a.db)
    if not (acct and mpan):
        sys.exit("no account stamp or MPAN in that database")
    token = _post("mutation getToken($input: ObtainJSONWebTokenInput!) {"
                  "  obtainKrakenToken(input: $input) { token } }",
                  {"input": {"APIKey": key}})["obtainKrakenToken"]["token"]
    side = lambda f, lab: f(lab) if f else "?"
    for day in a.day or ["2026-10-09", "2026-10-10"]:
        data = _post(QUERY, {"acc": acct, "mpan": mpan, "start": day + "T00:00:00Z",
                             "end": day + "T06:00:00Z"}, token)
        print("\n== %s (UTC 00:00-06:00)" % day)
        for p in (data.get("account") or {}).get("properties") or []:
            for e in ((p.get("measurements") or {}).get("edges") or []):
                n = e.get("node") or {}
                stats = [s for s in ((n.get("metaData") or {}).get("statistics") or [])
                         if s.get("type") != "STANDING_CHARGE_COST"]
                used = [s for s in stats if float(s.get("value") or 0) > 1e-9]
                print("%s  total %s kWh" % (n.get("startAt"), n.get("value")))
                for s in used or stats[:1]:
                    ppu = ((s.get("costInclTax") or {}).get("pricePerUnit") or {}).get("amount")
                    print("    %-28r %8s kWh  %8s p/kWh  -> v5 %s, v4 %s" % (
                        s.get("label"), s.get("value"), ppu,
                        side(v5_side, s.get("label")), side(v4_side, s.get("label"))))


if __name__ == "__main__":
    main()
