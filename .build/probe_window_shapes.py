#!/usr/bin/env python3
"""
Does Octopus's half-hourly cost depend on the window it is asked over? Read-only.

    KRAKEN_API_KEY=... python3 .build/probe_window_shapes.py DB --statement BILL.pdf \\
        --day 2025-03-06 [--day ...] [--ladder-max 3] [--pace 1.0] [--dry-run]

One day with the default ladder is about 19 queries; --dry-run says how many before any is made.

    KRAKEN_API_KEY=... python3 .build/probe_window_shapes.py DB --breakdown --day 2026-07-14 [...]

--breakdown is for IOG-SMB days, where EMT reads the four-bucket Home/EV breakdown
(recover_device_breakdown) and there is no half-hourly statement to compare with. It asks the
same day three ways and compares Octopus with itself, half-hour by half-hour — Home kWh, EV kWh
and cost:
  chunks    EMT's: the day's half-hours with consumption, in runs of at most 12, each asked
            [first - 30m, last + 30m)
  day       the local day
  month     the local month, paged
If all three agree, the window does not matter there. If they differ, it does, and the month —
the shape that matched the statement on the earlier tariff — is the one to trust. Up to about 8
queries a day (the month's pages are shared by the days in it).

22 Oct 2026, the user: EMT's bill reader asks Octopus a narrow window per half-hour (30 min before
to 2 h, then 3 h, after), on the belief that a smart charge outside the night window is labelled
off-peak only when the window reaches ~2 h past it. Nobody else has reported that. Against the
March 2025 statement, EMT's import — 60-day windows — matched 144 of 144 half-hours of 5-7 Mar,
while the narrow windows priced 11 half-hours of long evening charging at the day rate (£14.04
against the statement's £7.52). This asks the same half-hours several ways and compares each with
the statement:

  ladder    EMT's bill reader: [slot-30m, slot+2h), then [slot-30m, slot+3h) if the first is not
            OFF_PEAK; OFF_PEAK accepted at once, else the last cost kept (kraken_api_client
            .recover_measurement_costs). For at most --ladder-max half-hours a day: those the
            statement charged at the day's lowest rate outside 23:30-05:30, the interesting ones.
  day/500   the local day, 00:00-24:00 Europe/London, page size 500 (one page)
  day/48    the same, page size 48
  day/10    the same, page size 10 (five pages) — EMT narrowed its windows because dense nights
            came back with kWh and no cost; this tells whether page size, not window, does that
  60-day    one window from 30 days before the first --day to 30 days after the last, page size
            500, as EMT's import asks (get_measurements)

The account number and MPAN are read from DB (opened immutable; nothing is written); the
statement's MPAN must be the same one. The key is taken from the environment only, and never
printed; nor are the account number or MPAN. --dry-run lists the queries and makes none.
Needs pypdf for the statement (EMT's own bill_parser reads it); the queries are stdlib only.
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRAPHQL = "https://api.octopus.energy/v1/graphql/"
LONDON = ZoneInfo("Europe/London")
MATCH_GBP = 0.0006            # the statement prints pence to three places; VAT rounds the rest
SHAPES = ("ladder", "day/500", "day/48", "day/10", "60-day")


def query(page_size):
    """The measurements query EMT's get_measurements sends, with `value` per statistic (the bucket
    that carries the charge) and a page cursor."""
    return (
        "query meas($acc: String!, $mpan: String!, $start: DateTime!, $end: DateTime!, "
        "$after: String) {"
        "  account(accountNumber: $acc) { properties {"
        "    measurements(first: %d, after: $after, startAt: $start, endAt: $end, "
        "      timezone: \"Europe/London\", "
        "      utilityFilters: [{ electricityFilters: { readingFrequencyType: THIRTY_MIN_INTERVAL, "
        "        marketSupplyPointId: $mpan, readingDirection: CONSUMPTION } }]) {"
        "      edges { node { value ... on IntervalMeasurementType { startAt "
        "        metaData { statistics { type label value "
        "          costInclTax { estimatedAmount } } } } } }"
        "      pageInfo { hasNextPage endCursor } } } } }" % page_size)


# ── the statement ────────────────────────────────────────────────────────────
def statement_slots(path, days):
    """{naive-UTC start: {"kwh", "rate_pre", "gbp", "offpeak"}} for the --days, from the PDF's
    half-hourly tables. `gbp` is inc VAT; `offpeak` is the day's lowest printed rate."""
    sys.path.insert(0, _ROOT)
    import bill_parser
    if not bill_parser.pdf_support():
        sys.exit("reading the statement needs pypdf: python3 -m pip install pypdf")
    bill = bill_parser.parse_bill(path)
    want = set(days)
    out = {}
    for slots in bill.hh_days:
        if not slots or slots[0].d not in want:
            continue
        low = min(s.rate_pre for s in slots)
        seen = set()
        for s in slots:
            fold = 1 if (s.sh, s.sm) in seen else 0          # the clocks-back day's repeated hour
            seen.add((s.sh, s.sm))
            local = datetime(s.d.year, s.d.month, s.d.day, s.sh, s.sm, fold=fold, tzinfo=LONDON)
            utc = local.astimezone(timezone.utc).replace(tzinfo=None).isoformat()
            out[utc] = {"kwh": s.kwh, "rate_pre": s.rate_pre, "local": "%02d:%02d" % (s.sh, s.sm),
                        "gbp": round(s.cost_pre * (1 + bill.vat_import) / 100.0, 4),
                        "offpeak": abs(s.rate_pre - low) < 1e-6, "day": s.d}
    return out, bill.mpan_import


def core_night(local_hhmm):
    return local_hhmm >= "23:30" or local_hhmm < "05:30"


def ladder_slots(stmt, limit, min_kwh=0.5):
    """The half-hours worth asking the ladder about: charged at the night rate outside the night
    window, with real consumption — per day at most `limit`, spread evenly from the first to the
    last (on 6 Mar 2025 the narrow windows had the start of the evening's charging right and its
    middle wrong, so both ends and the middle are asked)."""
    by_day = {}
    for k, v in stmt.items():
        if v["offpeak"] and not core_night(v["local"]) and v["kwh"] >= min_kwh:
            by_day.setdefault(v["day"], []).append(k)
    out = []
    for d in sorted(by_day):
        ks = sorted(by_day[d])
        if len(ks) <= limit:
            out += ks
        elif limit == 1:
            out.append(ks[len(ks) // 2])
        else:
            out += sorted({ks[round(i * (len(ks) - 1) / (limit - 1))] for i in range(limit)})
    return out


# ── the windows ──────────────────────────────────────────────────────────────
def z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def local_day(d):
    a = datetime(d.year, d.month, d.day, tzinfo=LONDON).astimezone(timezone.utc)
    b = (datetime(d.year, d.month, d.day, tzinfo=LONDON) + timedelta(days=1)).astimezone(timezone.utc)
    return a.replace(tzinfo=None), b.replace(tzinfo=None)


def rungs(slot):
    s = datetime.fromisoformat(slot)
    return [(s - timedelta(minutes=30), s + timedelta(hours=h)) for h in (2, 3)]


def wide(days):
    return (datetime.combine(min(days), datetime.min.time()) - timedelta(days=30),
            datetime.combine(max(days), datetime.min.time()) + timedelta(days=31))


def queries_for(windows):
    """How many queries the windows take: one a page, a page holding `first` half-hours."""
    n = 0
    for shape, what, a, b, first in windows:
        slots = max(1, int((b - a).total_seconds() // 1800))
        n += 1 if shape == "ladder" else -(-slots // first)
    return n


def plan(days, ladder):
    """[(shape, day or slot, start, end, page size)] — every window, before any is asked."""
    out = []
    for s in ladder:
        for a, b in rungs(s):
            out.append(("ladder", s, a, b, 100))
    for d in days:
        a, b = local_day(d)
        for n in (500, 48, 10):
            out.append(("day/%d" % n, d, a, b, n))
    a, b = wide(days)
    out.append(("60-day", None, a, b, 500))
    return out


# ── a node ───────────────────────────────────────────────────────────────────
def utc_key(st):
    return (datetime.fromisoformat(st.replace("Z", "+00:00"))
            .astimezone(timezone.utc).replace(tzinfo=None).isoformat())


def read_node(n):
    """(kwh, gbp inc VAT or None if no energy statistic, carrying label or None)"""
    energy = [s for s in ((n.get("metaData") or {}).get("statistics") or [])
              if s.get("type") != "STANDING_CHARGE_COST"]
    if not energy:
        return float(n.get("value") or 0), None, None
    pence, labels = 0.0, set()
    for s in energy:
        try:
            c = float((s.get("costInclTax") or {}).get("estimatedAmount") or 0)
        except (TypeError, ValueError):
            c = 0.0
        pence += c
        try:
            carries = abs(float(s.get("value"))) > 1e-9
        except (TypeError, ValueError):
            carries = abs(c) > 1e-9
        if carries and s.get("label"):
            labels.add(s["label"])
    return (float(n.get("value") or 0), round(pence / 100.0, 6),
            labels.pop() if len(labels) == 1 else ("+".join(sorted(labels)) or None))


def ladder_pick(answers):
    """EMT's rule over the rungs' answers for one half-hour: OFF_PEAK at the first rung that gives
    it; else the last rung's cost that had one (recover_measurement_costs keeps it as fallback)."""
    keep = None
    for a in answers:
        if a is None or a[1] is None:
            continue
        if a[2] and "OFF_PEAK" in a[2]:
            return a
        keep = a
    return keep


# ── asking ───────────────────────────────────────────────────────────────────
class Octopus:
    def __init__(self, key, acct, mpan, pace):
        self.acct, self.mpan, self.pace, self.queries = acct, mpan, pace, 0
        data, errs = self._post("mutation t($input: ObtainJSONWebTokenInput!) {"
                                "  obtainKrakenToken(input: $input) { token } }",
                                {"input": {"APIKey": key}})
        if errs or not data.get("obtainKrakenToken"):
            sys.exit("could not get a token: %s" % errs)
        self.token = data["obtainKrakenToken"]["token"]

    def _post(self, q, variables, token=None):
        req = urllib.request.Request(GRAPHQL, data=json.dumps(
            {"query": q, "variables": variables}).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     **({"Authorization": "JWT " + token} if token else {})})
        with urllib.request.urlopen(req, timeout=90) as r:
            body = json.loads(r.read())
        return body.get("data") or {}, [e.get("message") for e in (body.get("errors") or [])]

    def window(self, a, b, page_size):
        """({utc start: node}, pages, errors) over [a, b), every page."""
        nodes, after, pages, errs = {}, None, 0, []
        while True:
            if self.queries and self.pace:
                time.sleep(self.pace)
            data, e = self._post(query(page_size), {"acc": self.acct, "mpan": self.mpan,
                                                    "start": z(a), "end": z(b), "after": after},
                                 self.token)
            self.queries += 1; pages += 1; errs += e
            conn = None
            for p in (data.get("account") or {}).get("properties") or []:
                if p.get("measurements"):
                    conn = p["measurements"]; break
            if conn is None:
                break
            for ed in conn.get("edges") or []:
                n = ed.get("node") or {}
                if n.get("startAt"):
                    nodes[utc_key(n["startAt"])] = n
            pi = conn.get("pageInfo") or {}
            if not pi.get("hasNextPage") or not pi.get("endCursor") or pages >= 200:
                break
            after = pi["endCursor"]
        return nodes, pages, errs


def _db(path):
    return sqlite3.connect("file:%s?immutable=1" % quote(os.path.abspath(path)), uri=True)


def from_db(path):
    c = _db(path)
    try:
        acct = c.execute("SELECT value FROM kraken_state WHERE key='kraken_account_number'").fetchone()
        mpan = c.execute("SELECT mpan FROM measured_cost WHERE direction='CONSUMPTION' "
                         "ORDER BY fetched_at DESC LIMIT 1").fetchone()
    finally:
        c.close()
    acct = acct[0] if acct else None
    if isinstance(acct, str) and acct.startswith('"'):
        acct = json.loads(acct)
    return acct, (mpan[0] if mpan else None)


# ── the report ───────────────────────────────────────────────────────────────
def compare(stmt, answers):
    """{shape: {"n", "match", "gbp", "want", "stripped", "absent"}} over the statement's half-hours
    each shape answered (the ladder only its own)."""
    out = {}
    for shape in SHAPES:
        got = answers.get(shape) or {}
        r = {"n": 0, "match": 0, "gbp": 0.0, "want": 0.0, "stripped": 0, "absent": 0}
        for k, v in stmt.items():
            if shape == "ladder" and k not in got:
                continue
            a = got.get(k)
            if a is None:
                r["absent"] += 1; continue
            if a[1] is None:
                if v["kwh"] > 0:
                    r["stripped"] += 1
                continue
            r["n"] += 1; r["gbp"] += a[1]; r["want"] += v["gbp"]
            r["match"] += abs(a[1] - v["gbp"]) <= MATCH_GBP
        out[shape] = r
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--statement", help="the Octopus PDF statement covering the days")
    ap.add_argument("--breakdown", action="store_true",
                    help="IOG-SMB days: compare the Home/EV breakdown across windows (no statement)")
    ap.add_argument("--day", action="append", required=True, help="local day, YYYY-MM-DD")
    ap.add_argument("--ladder-max", type=int, default=3)
    ap.add_argument("--ladder-slot", action="append", default=None,
                    help="naive-UTC half-hour start to ask the ladder about, in place of the picks")
    ap.add_argument("--pace", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    days = sorted(date.fromisoformat(d) for d in a.day)
    if a.breakdown:
        return breakdown_main(a, days)
    if not a.statement:
        sys.exit("--statement is needed (or --breakdown for IOG-SMB days)")
    stmt, stmt_mpan = statement_slots(a.statement, days)
    missing = [d for d in days if not any(v["day"] == d for v in stmt.values())]
    if missing:
        sys.exit("the statement has no half-hourly table for %s" % ", ".join(map(str, missing)))
    acct, mpan = from_db(a.db)
    same = bool(stmt_mpan and mpan and stmt_mpan == mpan)
    if not a.dry_run:
        # a support file's database will not do: its account number and MPAN are redacted
        if not (acct and mpan):
            sys.exit("no account stamp or MPAN in that database")
        if stmt_mpan and not same:
            sys.exit("the statement is for a different meter than the database's — a support "
                     "file's database has its identifiers redacted; use a backup's blocks.db")
    ladder = sorted(a.ladder_slot) if a.ladder_slot else ladder_slots(stmt, a.ladder_max)
    unknown = [s for s in ladder if s not in stmt]
    if unknown:
        sys.exit("not on the statement's days: %s" % ", ".join(unknown))
    windows = plan(days, ladder)
    print("%d day(s), %d half-hour(s) on the statement, %d for the ladder; %d window(s), about %d "
          "quer%s" % (len(days), len(stmt), len(ladder), len(windows), queries_for(windows),
                      "y" if queries_for(windows) == 1 else "ies"))
    for shape, what, ws, we, n in windows:
        print("  %-8s %-19s %s .. %s  first=%d" % (shape, what or "", z(ws), z(we), n))
    if a.dry_run:
        print("(dry run: nothing asked; the statement and database are %s)"
              % ("the same meter" if same else "NOT the same meter — check before a real run"))
        return
    key = os.environ.get("KRAKEN_API_KEY", "").strip()
    if not key:
        sys.exit("set KRAKEN_API_KEY in the environment")
    o = Octopus(key, acct, mpan, a.pace)
    answers = {s: {} for s in SHAPES}
    rung_answers = {}
    for shape, what, ws, we, n in windows:
        nodes, pages, errs = o.window(ws, we, n)
        if errs:
            print("  %s %s: ERRORS %s" % (shape, what or "", errs))
        if shape == "ladder":
            node = nodes.get(what)
            rung_answers.setdefault(what, []).append(read_node(node) if node else None)
            continue
        for k in stmt:
            if k in nodes and (shape == "60-day" or stmt[k]["day"] == what):
                answers[shape][k] = read_node(nodes[k])
        print("  %-8s %-10s %d page(s), %d node(s)" % (shape, what or "", pages, len(nodes)))
    for s, rs in rung_answers.items():
        answers["ladder"][s] = ladder_pick(rs)

    print("\nhalf-hour (local)   statement            " + "  ".join("%-22s" % s for s in SHAPES))
    for k in sorted(stmt):
        v = stmt[k]
        if v["kwh"] < 0.05 and k not in answers["ladder"]:
            continue
        cells = []
        for s in SHAPES:
            x = answers[s].get(k)
            if x is None:
                cells.append("%-22s" % ("" if s == "ladder" else "absent"))
            elif x[1] is None:
                cells.append("%-22s" % "no cost (stripped)")
            else:
                mark = "=" if abs(x[1] - v["gbp"]) <= MATCH_GBP else "X"
                cells.append("%-22s" % ("%s £%.4f %s" % (mark, x[1], (x[2] or "")[:9])))
        print("%s %s  %5.2f kWh £%.4f%s  %s" % (v["day"], v["local"], v["kwh"], v["gbp"],
                                              " op" if v["offpeak"] else "   ", "  ".join(cells)))
    print("\nshape      matches the statement      EMT £    statement £   no cost   absent")
    for s, r in compare(stmt, answers).items():
        print("%-9s  %4d of %-4d             %8.2f  %8.2f      %4d     %4d" % (
            s, r["match"], r["n"], r["gbp"], r["want"], r["stripped"], r["absent"]))
    print("\n%d quer%s to Octopus." % (o.queries, "y" if o.queries == 1 else "ies"))


# ── --breakdown: IOG-SMB days, Octopus against itself ───────────────────────
def load_bucket_side():
    """EMT's own bucket_side, loaded without its HTTP library (as probe_device_breakdown.py does)."""
    import importlib.util
    import types
    sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))
    spec = importlib.util.spec_from_file_location("kac_probe", os.path.join(_ROOT, "kraken_api_client.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.bucket_side


def read_split(n, side):
    """(home kWh, ev kWh, £ inc VAT or None, unknown labels) from a node's buckets."""
    home = ev = 0.0
    pence, unknown, saw = 0.0, set(), False
    for s in ((n.get("metaData") or {}).get("statistics") or []):
        if s.get("type") == "STANDING_CHARGE_COST":
            continue
        saw = True
        try:
            v = float(s.get("value") or 0)
        except (TypeError, ValueError):
            v = 0.0
        try:
            pence += float((s.get("costInclTax") or {}).get("estimatedAmount") or 0)
        except (TypeError, ValueError):
            pass
        if abs(v) <= 1e-9:
            continue
        k = side(s.get("label"))
        if k == "ev":
            ev += v
        elif k == "home":
            home += v
        else:
            unknown.add(s.get("label"))
    return round(home, 4), round(ev, 4), (round(pence / 100.0, 6) if saw else None), unknown


def chunks(starts, size=12):
    """recover_device_breakdown's grouping: runs of at most `size` half-hours from the first."""
    out, cur = [], []
    for s in sorted(starts):
        if cur and (datetime.fromisoformat(s) - datetime.fromisoformat(cur[0])) >= timedelta(minutes=30 * size):
            out.append(cur); cur = []
        cur.append(s)
    if cur:
        out.append(cur)
    return out


def local_month(d):
    n = (d.replace(day=1) + timedelta(days=32)).replace(day=1)
    a = datetime(d.year, d.month, 1, tzinfo=LONDON).astimezone(timezone.utc).replace(tzinfo=None)
    b = datetime(n.year, n.month, 1, tzinfo=LONDON).astimezone(timezone.utc).replace(tzinfo=None)
    return a, b


def same(x, y):
    if x is None or y is None:
        return x is y
    return (abs(x[0] - y[0]) <= 0.0005 and abs(x[1] - y[1]) <= 0.0005
            and (x[2] is None) == (y[2] is None)
            and (x[2] is None or abs(x[2] - y[2]) <= MATCH_GBP))


def breakdown_main(a, days):
    acct, mpan = from_db(a.db)
    months = sorted({local_month(d) for d in days})
    print("%d day(s) as the local day, their consuming half-hours in EMT's chunks, and %d local "
          "month(s) — up to about %d queries" % (len(days), len(months), len(days) * 5 + 3 * len(months)))
    for d in days:
        da, db = local_day(d)
        print("  day      %s  %s .. %s  first=500, then its chunks of up to 12" % (d, z(da), z(db)))
    for ma, mb in months:
        print("  month             %s .. %s  first=500, paged" % (z(ma), z(mb)))
    if a.dry_run:
        print("(dry run: nothing asked)")
        return
    if not (acct and mpan):
        sys.exit("no account stamp or MPAN in that database — a support file's are redacted; "
                 "use a backup's blocks.db")
    key = os.environ.get("KRAKEN_API_KEY", "").strip()
    if not key:
        sys.exit("set KRAKEN_API_KEY in the environment")
    side = load_bucket_side()
    o = Octopus(key, acct, mpan, a.pace)
    month_nodes = {}
    for ma, mb in months:
        nodes, pages, errs = o.window(ma, mb, 500)
        if errs:
            print("  month ERRORS %s" % errs)
        month_nodes.update(nodes)
        print("  month    %s  %d page(s), %d node(s)" % (z(ma)[:7], pages, len(nodes)))
    tot = {"n": 0, "chunk=day": 0, "day=month": 0, "chunk=month": 0}
    unknown = set()
    for d in days:
        da, db = local_day(d)
        day_nodes, pages, errs = o.window(da, db, 500)
        if errs:
            print("  day %s ERRORS %s" % (d, errs))
        want = [k for k, n in day_nodes.items() if float(n.get("value") or 0) > 0.05]
        chunk_nodes = {}
        for ch in chunks(want):
            nodes, _p, errs = o.window(datetime.fromisoformat(ch[0]) - timedelta(minutes=30),
                                       datetime.fromisoformat(ch[-1]) + timedelta(minutes=30), 500)
            if errs:
                print("  chunk %s ERRORS %s" % (ch[0], errs))
            for k in ch:
                if k in nodes:
                    chunk_nodes[k] = nodes[k]
        print("\n== %s: %d half-hour(s) with consumption, %d chunk(s)" % (d, len(want), len(chunks(want))))
        print("half-hour (local)   chunks: home / ev / £          day: home / ev / £             month: home / ev / £")
        for k in sorted(want):
            r = {}
            for name, src in (("chunk", chunk_nodes), ("day", day_nodes), ("month", month_nodes)):
                r[name] = read_split(src[k], side) if k in src else None
                if r[name]:
                    unknown |= r[name][3]
            tot["n"] += 1
            for x, y in (("chunk", "day"), ("day", "month"), ("chunk", "month")):
                tot["%s=%s" % (x, y)] += same(r[x] and r[x][:3], r[y] and r[y][:3])
            loc = datetime.fromisoformat(k).replace(tzinfo=timezone.utc).astimezone(LONDON).strftime("%H:%M")
            flag = "" if same(r["chunk"] and r["chunk"][:3], r["month"] and r["month"][:3]) \
                and same(r["day"] and r["day"][:3], r["month"] and r["month"][:3]) else "   <- differs"
            print("%s %s   %s  %s  %s%s" % (d, loc, *[
                ("%6.3f / %6.3f / %s" % (x[0], x[1], "no cost" if x[2] is None else "£%.4f" % x[2])).ljust(30)
                if x else "absent".ljust(30) for x in (r["chunk"], r["day"], r["month"])], flag))
    print("\nhalf-hours agreeing (Home kWh, EV kWh and £):  chunks = day %d of %d · day = month %d · "
          "chunks = month %d" % (tot["chunk=day"], tot["n"], tot["day=month"], tot["chunk=month"]))
    if unknown:
        print("bucket labels EMT does not place on either side: %s" % sorted(unknown))
    print("%d quer%s to Octopus." % (o.queries, "y" if o.queries == 1 else "ies"))


if __name__ == "__main__":
    main()
