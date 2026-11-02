#!/usr/bin/env python3
"""
Ask Octopus, read-only, what its half-hourly measurements say about the cost of half-hours EMT
could not get a cost for: the raw statistics (type, label, kWh, cost) for each one, through the
same windows EMT's billed-cost recovery uses and two more, so a missing cost can be told apart
as a zero, an absent half-hour, or a stripped answer (statistics empty, kWh intact).

    KRAKEN_API_KEY=... python3 .build/probe_measurement_costs.py DB [--slot 2026-08-19T05:30:00 ...]

Without --slot it takes, from DB, the main-meter half-hours that imported something, are not
imported history, and hold no settled bill — the stubborn red cells — newest first, up to
--limit. Slots are naive UTC, as EMT stores them. The account number and MPAN are read from DB
(opened immutable; nothing is written). The key is taken from the environment only, and never
printed.

Windows asked, per slot (all UTC):
  tight     the half-hour itself                      [slot, slot+30m)
  ladder 2  EMT's first rung                          [slot-30m, slot+2h)
  ladder 3  EMT's second rung                         [slot-30m, slot+3h)
  day       the local day around it, 00:00-24:00 UTC
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

GRAPHQL = "https://api.octopus.energy/v1/graphql/"

QUERY = (
    "query meas($acc: String!, $mpan: String!, $start: DateTime!, $end: DateTime!) {"
    "  account(accountNumber: $acc) { properties {"
    "    measurements(first: 100, startAt: $start, endAt: $end, timezone: \"Europe/London\", "
    "      utilityFilters: [{ electricityFilters: { readingFrequencyType: THIRTY_MIN_INTERVAL, "
    "        marketSupplyPointId: $mpan, readingDirection: CONSUMPTION } }]) {"
    "      edges { node { value ... on IntervalMeasurementType { startAt "
    "        metaData { statistics { type label value "
    "          costInclTax { estimatedAmount } costExclTax { estimatedAmount } } } } } } } } } }")


def _post(query, variables, token=None):
    req = urllib.request.Request(GRAPHQL, data=json.dumps(
        {"query": query, "variables": variables}).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 **({"Authorization": "JWT " + token} if token else {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = json.loads(r.read())
    return body.get("data") or {}, [e.get("message") for e in (body.get("errors") or [])]


def _db(path):
    return sqlite3.connect("file:%s?immutable=1" % quote(os.path.abspath(path)), uri=True)


def _from_db(path):
    c = _db(path)
    try:
        acct = c.execute("SELECT value FROM kraken_state WHERE key='kraken_account_number'").fetchone()
        mpan = c.execute("SELECT mpan FROM measured_cost WHERE direction='CONSUMPTION' "
                         "ORDER BY fetched_at DESC LIMIT 1").fetchone()
    finally:
        c.close()
    acct = acct[0] if acct else None
    if isinstance(acct, str) and acct.startswith('"'):
        acct = json.loads(acct)                       # stored as a JSON string
    return acct, (mpan[0] if mpan else None)


def _stubborn(path, limit):
    c = _db(path)
    try:
        rows = c.execute(
            "SELECT b.block_start, b.imp_kwh, b.imp_cost FROM blocks b "
            "WHERE b.meter_id = 'electricity_main' AND b.imp_kwh > 1e-9 "
            "  AND COALESCE(b.source, '') NOT LIKE 'imported%' "
            "  AND COALESCE(b.rate_corrected, 0) = 0 AND COALESCE(b.rate_source, '') <> 'corrected' "
            "  AND NOT (b.rate_source = 'settled' AND EXISTS "
            "           (SELECT 1 FROM measured_cost m WHERE m.slot_start = b.block_start)) "
            "  AND b.block_start < datetime('now', '-9 days') "
            "ORDER BY b.block_start DESC LIMIT ?", (limit,)).fetchall()
    finally:
        c.close()
    return [(r[0], r[1], r[2]) for r in rows]


def _z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def _windows(slot):
    s = datetime.fromisoformat(slot)
    day = s.replace(hour=0, minute=0, second=0)
    return [("tight", s, s + timedelta(minutes=30)),
            ("ladder 2", s - timedelta(minutes=30), s + timedelta(hours=2)),
            ("ladder 3", s - timedelta(minutes=30), s + timedelta(hours=3)),
            ("day", day, day + timedelta(days=1))]


def _node_for(data, slot):
    want = slot[:16]                                   # minute precision; startAt carries a zone
    n_nodes = 0
    for p in (data.get("account") or {}).get("properties") or []:
        for e in ((p.get("measurements") or {}).get("edges") or []):
            n = e.get("node") or {}
            n_nodes += 1
            st = n.get("startAt") or ""
            try:
                key = (datetime.fromisoformat(st.replace("Z", "+00:00"))
                       .astimezone(timezone.utc).replace(tzinfo=None).isoformat()[:16])
            except ValueError:
                key = st[:16]
            if key == want:
                return n, n_nodes
    return None, n_nodes


def _amt(block):
    try:
        return float((block or {}).get("estimatedAmount"))
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--slot", action="append", default=None, help="naive-UTC half-hour start")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--pace", type=float, default=0.5, help="seconds between requests")
    a = ap.parse_args()
    key = os.environ.get("KRAKEN_API_KEY", "").strip()
    if not key:
        sys.exit("set KRAKEN_API_KEY in the environment")
    acct, mpan = _from_db(a.db)
    if not (acct and mpan):
        sys.exit("no account stamp or MPAN in that database")
    slots = ([(s, None, None) for s in a.slot] if a.slot else _stubborn(a.db, a.limit))
    if not slots:
        sys.exit("no half-hours to probe")
    data, errs = _post("mutation getToken($input: ObtainJSONWebTokenInput!) {"
                       "  obtainKrakenToken(input: $input) { token } }",
                       {"input": {"APIKey": key}})
    if errs or not data.get("obtainKrakenToken"):
        sys.exit("could not get a token: %s" % errs)
    token = data["obtainKrakenToken"]["token"]
    for slot, kwh, cost in slots:
        print("\n== %s UTC%s" % (slot, "" if kwh is None else
                                 "   EMT holds %.3f kWh, £%.6f (kWh x rate)" % (kwh, cost or 0)))
        for name, ws, we in _windows(slot):
            data, errs = _post(QUERY, {"acc": acct, "mpan": mpan, "start": _z(ws), "end": _z(we)}, token)
            node, n_nodes = _node_for(data, slot)
            head = "  %-8s %s..%s  %3d node(s)" % (name, _z(ws)[11:16], _z(we)[11:16], n_nodes)
            if errs:
                print(head + "  ERRORS: %s" % errs)
            elif node is None:
                print(head + "  -> this half-hour ABSENT")
            else:
                stats = (node.get("metaData") or {}).get("statistics") or []
                energy = [s for s in stats if s.get("type") != "STANDING_CHARGE_COST"]
                if not energy:
                    print(head + "  -> %s kWh, NO energy statistics (stripped)" % node.get("value"))
                else:
                    tot = sum(_amt(s.get("costInclTax")) or 0.0 for s in energy)
                    print(head + "  -> %s kWh, energy cost %.4fp inc VAT" % (node.get("value"), tot))
                    for s in energy:
                        print("             %-18s %-24r %8s kWh  inc %8sp  exc %8sp" % (
                            s.get("type"), s.get("label"), s.get("value"),
                            _amt(s.get("costInclTax")), _amt(s.get("costExclTax"))))
            if a.pace:
                time.sleep(a.pace)


if __name__ == "__main__":
    main()
