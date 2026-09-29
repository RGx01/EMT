#!/usr/bin/env python3
"""
EMT soak — workstream A §5.4 (and B §5.6 from the log).

Three modes, all run from a workstation against the soak add-on. Standard library
only, so it needs nothing installed.

  run             python3 .build/soak.py run --base http://192.168.20.16:8098
  scan a log      python3 .build/soak.py scan-log addon.log
  compare         python3 .build/soak.py compare-segments segments_before.db blocks.db

WHAT `run` DOES
  Drives every database-backed GET route at a capped rate while the engine writes,
  and records every status code and latency. The heavy write is the historical
  re-price sweep, which .build/soak_prepare_db.py arranges to rebuild segments for
  all history on startup. Phases follow /api/reprice-history-status:

      sweep   the sweep is in progress — reads racing a whole-history write
      quiet   after it finishes — the baseline the sweep phase is compared against

  A (§5 acceptance 4) passes when: no 5xx, no connection failure, every 200 that
  declares JSON parses, and read latency during the sweep stays within --block-ratio
  of quiet (p95, per route) — a read blocked behind the writer shows up as exactly
  that ratio. A request that hits --timeout is reported as SLOW, not as a failure,
  but counts at the timeout in the ratio so blocking cannot hide behind it.

WHAT IT WILL NEVER DO
  Send anything but GET. There is no code path here that issues a POST, so no job
  route (import, backfill, gap fill, re-price repair, restore, delete) can be
  reached, whatever the arguments. On top of that, the GET routes below are
  excluded because they reach Octopus, Home Assistant or the Supervisor — traced
  from the code, not assumed from their names:

      Octopus      /api/reprice-history-conformance  (rate fetch + full re-price per call)
      HA / Super.  /api/logs  /api/power  /api/entities  /api/detect-bcd
      internet     /api/carbon (carbonintensity.org.uk)  /api/update-check (GitHub)
      belt+braces  /api/backfill/reach  /api/historical/site-plan
                   /api/blocks/sweep-implausible (a repair route that also answers GET)

  The only Octopus traffic during a soak is the add-on's own normal polling.
"""
import argparse
import ast
import collections
import datetime as dt
import json
import os
import random
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DENY = {
    "/api/reprice-history-conformance": "calls Octopus (rate schedules) and re-prices history",
    "/api/logs": "Supervisor API",
    "/api/power": "Home Assistant state",
    "/api/entities": "Home Assistant state",
    "/api/detect-bcd": "Home Assistant state",
    "/api/backfill/reach": "plans an Octopus import",
    "/api/historical/site-plan": "discovers sites from Home Assistant history",
    "/api/blocks/sweep-implausible": "repair route",
    "/api/carbon": "calls carbonintensity.org.uk on every request, uncached",
    "/api/update-check": "calls GitHub",
    "/api/reprice-history-status": "polled by the phase tracker instead",
}
STATUS = "/api/reprice-history-status"
POLL_S = 15.0            # sweep-status poll interval


# ── route discovery ──────────────────────────────────────────────────────────

def discover_get_routes(server_py=os.path.join(ROOT, "web", "server.py")):
    """GET-only routes (a route that also takes POST is left out), with no path
    parameters, minus DENY. Read from the source so the list tracks the app."""
    tree = ast.parse(open(server_py, encoding="utf-8").read())
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for d in n.decorator_list:
            if not (isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "route"):
                continue
            path = d.args[0].value
            methods = ["GET"]
            for kw in d.keywords:
                if kw.arg == "methods":
                    methods = [e.value for e in kw.value.elts]
            if methods == ["GET"] and "<" not in path and path not in DENY:
                out.append(path)
    return sorted(set(out))


def query_variants(path, ctx):
    """Realistic query strings for the routes that read arguments. Anything not
    listed is requested bare."""
    today, periods, months, years = ctx["today"], ctx["periods"], ctx["months"], ctx["years"]
    recent = [(today - dt.timedelta(days=d)).isoformat() for d in (1, 2, 7, 30, 180)]
    week = [((today - dt.timedelta(days=d + 7)).isoformat(),
             (today - dt.timedelta(days=d)).isoformat()) for d in (1, 60)]
    table = {
        "/api/charts/blocks-day": [{"date": d} for d in recent],
        "/api/power/history": [{"hours": h} for h in (6, 24)],
        # meter_id is required; with no sub-meter there is nothing to ask for
        "/api/sub-meter/history": [{"meter_id": m, "hours": 24}
                                   for m in ctx.get("sub_meters", [])] or ["SKIP"],
        "/api/historical/gap-template": [{"from": f + "T00:00:00", "to": t + "T00:00:00"}
                                         for f, t in week],
        "/api/corrections/nearby-rates": [{"channel": "import", "from_date": f, "to_date": t}
                                          for f, t in week],
        "/api/unsettled-blocks": [{"limit": 50}],
        # resolution=day|block return 501 until C builds them — period only.
        "/api/energy": [{"resolution": "period", "kind": k, "include": "usage,carbon"}
                        for k in ("billing", "month", "year")],
        "/api/periods": [{"kind": k} for k in ("billing", "month", "year")],
        "/api/insights/billing-period": [{"period_start": p} for p in periods],
        "/api/usage/billing-period": [{"period_start": p} for p in periods],
        "/api/insights/calendar-month": [{"year": y, "month": m} for y, m in months],
        "/api/usage/calendar-month": [{"year": y, "month": m} for y, m in months],
        "/api/insights/calendar-year": [{"year": y} for y in years],
        "/api/usage/calendar-year": [{"year": y} for y in years],
        "/api/charge-sessions": [{"days": 30}],
    }
    return [path + ("?" + urllib.parse.urlencode(q) if q else "")
            for q in (table.get(path) or [None]) if q != "SKIP"]


def build_context(base, timeout):
    today = dt.date.today()
    ctx = {"today": today, "periods": [], "months": [], "years": [today.year, today.year - 1]}
    for m in range(4):
        y, mo = today.year, today.month - m
        while mo < 1:
            y, mo = y - 1, mo + 12
        ctx["months"].append((y, mo))
    try:
        body = json.loads(_get(base, "/api/periods?kind=billing", timeout)[2])
        ctx["periods"] = [p["start"] for p in (body.get("periods") or [])][-6:]
    except Exception as e:
        print("warning: could not read billing periods (%s); period routes run bare" % e)
    try:
        body = json.loads(_get(base, "/api/corrections/meters", timeout)[2])
        ctx["sub_meters"] = [m for m in (body.get("meters") or [])
                             if m != "electricity_main" and "export" not in m][:3]
    except Exception as e:
        ctx["sub_meters"] = []
        print("warning: could not list meters (%s); sub-meter history is skipped" % e)
    return ctx


# ── HTTP ─────────────────────────────────────────────────────────────────────

def _get(base, path, timeout):
    """(status, seconds, body, content_type). The only request this script can make."""
    req = urllib.request.Request(base.rstrip("/") + path, method="GET",
                                 headers={"User-Agent": "emt-soak"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, time.monotonic() - t0, body, r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return (e.code, time.monotonic() - t0, e.read() or b"",
                e.headers.get("Content-Type", "") if e.headers else "")


def _is_timeout(e):
    import socket
    if isinstance(e, (TimeoutError, socket.timeout)):
        return True
    reason = getattr(e, "reason", None)          # URLError wraps the socket error
    return isinstance(reason, (TimeoutError, socket.timeout))


class RateLimiter:
    def __init__(self, per_second):
        self.interval = 1.0 / per_second
        self.next = time.monotonic()
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next)
            self.next = slot + self.interval
        time.sleep(max(0.0, slot - now))


# ── run ──────────────────────────────────────────────────────────────────────

def cmd_run(a):
    os.makedirs(a.out, exist_ok=True)
    rec_path = os.path.join(a.out, "requests.jsonl")
    if os.path.exists(rec_path):
        sys.exit("refusing to overwrite %s" % rec_path)

    st = json.loads(_get(a.base, STATUS, a.timeout)[2])
    print("sweep status at start:", st)
    # The sweep starts from engine_startup AFTER a Data Management restore, and only
    # once Octopus has connected — so it may not be running yet when this starts.
    deadline = time.monotonic() + a.wait_for_sweep * 60
    while not st.get("in_progress") and time.monotonic() < deadline:
        print("waiting for the sweep to start…", end="\r", flush=True)
        time.sleep(POLL_S)
        try:
            st = json.loads(_get(a.base, STATUS, a.timeout)[2])
        except Exception:
            pass
    if st.get("in_progress"):
        print("sweep in progress:", st)
    else:
        print("warning: the sweep is not running — there will be no sweep phase, so "
              "blocking cannot be judged. Was the database prepared with "
              "soak_prepare_db.py, and is the add-on connected to Octopus?")

    ctx = build_context(a.base, a.timeout)
    urls = [u for p in discover_get_routes() for u in query_variants(p, ctx)]
    print("%d GET routes, %d URLs; excluded: %s" % (
        len({u.split("?")[0] for u in urls}), len(urls), ", ".join(sorted(DENY))))

    state = {"phase": "sweep" if st.get("in_progress") else "quiet",
             "quiet_since": None if st.get("in_progress") else time.monotonic(),
             "stop": False}
    lock = threading.Lock()
    out = open(rec_path, "w")
    limiter = RateLimiter(a.rate)
    start = time.monotonic()

    def tracker():
        while not state["stop"]:
            time.sleep(POLL_S)
            try:
                s = json.loads(_get(a.base, STATUS, a.timeout)[2])
            except Exception:
                continue
            with lock:
                if state["phase"] == "sweep" and not s.get("in_progress"):
                    state["phase"], state["quiet_since"] = "quiet", time.monotonic()
                    print("\nsweep finished (swept=%s) — quiet phase for %d min"
                          % (s.get("swept"), a.quiet_min))
                out.write(json.dumps({"t": time.time(), "status_poll": s}) + "\n")

    def worker():
        timeouts = 0
        while not state["stop"]:
            limiter.wait()
            url = random.choice(urls)
            with lock:
                phase = state["phase"]
            r = {"t": time.time(), "phase": phase, "url": url}
            try:
                code, secs, body, ctype = _get(a.base, url, a.timeout)
                r.update(status=code, secs=round(secs, 4), bytes=len(body))
                # Only a response that SAYS it is JSON has to parse — a CSV template
                # or a file download under /api is correct as whatever it declares.
                if code == 200 and "json" in (ctype or "").lower():
                    try:
                        json.loads(body)
                    except ValueError:
                        r["bad_json"] = True
                if code >= 500 and code != 501:
                    r["body"] = body[:300].decode("utf-8", "replace")
                timeouts = 0
            except Exception as e:
                r.update(status=None, error="%s: %s" % (type(e).__name__, e),
                         timeout=_is_timeout(e))
                if r["timeout"]:
                    r["secs"] = a.timeout
                timeouts += 1
                if timeouts >= 3:          # don't pile onto a struggling Pi
                    time.sleep(30)
                    timeouts = 0
            with lock:
                out.write(json.dumps(r) + "\n")

    threads = [threading.Thread(target=tracker, daemon=True)]
    threads += [threading.Thread(target=worker, daemon=True) for _ in range(a.workers)]
    for t in threads:
        t.start()
    try:
        while True:
            time.sleep(5)
            with lock:
                q = state["quiet_since"]
            if q is not None and time.monotonic() - q >= a.quiet_min * 60:
                break
            if time.monotonic() - start >= a.max_hours * 3600:
                print("\nmax duration reached")
                break
    except KeyboardInterrupt:
        print("\ninterrupted")
    state["stop"] = True
    for t in threads:
        t.join(timeout=a.timeout + 1)
    with lock:
        out.close()
    report = summarise(rec_path, a.block_ratio)
    open(os.path.join(a.out, "report.txt"), "w").write(report)
    print(report)


def _pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100.0 * (len(xs) - 1))))] if xs else None


def summarise(rec_path, block_ratio=5.0, min_abs_secs=1.0):
    rows = [json.loads(l) for l in open(rec_path) if l.strip()]
    rows = [r for r in rows if "url" in r]
    fails, lines = [], []
    by_phase = collections.defaultdict(list)
    by_route = collections.defaultdict(lambda: collections.defaultdict(list))
    codes = collections.Counter()
    slow = collections.Counter()
    for r in rows:
        codes[(r["phase"], "timeout" if r.get("timeout") else r.get("status"))] += 1
        if r.get("timeout"):
            slow[r["url"].split("?")[0]] += 1
        elif r.get("status") is None:
            fails.append("connection failure %s: %s" % (r["url"], r.get("error")))
        elif r["status"] >= 500 and r["status"] != 501:
            fails.append("%s %s: %s" % (r["status"], r["url"], r.get("body", "")[:120]))
        elif r.get("bad_json"):
            fails.append("invalid JSON from %s" % r["url"])
        # A timeout counts at the timeout: leaving it out would make a read that was
        # blocked behind the writer long enough to time out vanish from the ratio.
        if r.get("secs") is not None and (r.get("timeout") or (r.get("status") and r["status"] < 500)):
            by_phase[r["phase"]].append(r["secs"])
            by_route[r["url"].split("?")[0]][r["phase"]].append(r["secs"])

    lines.append("requests: %d" % len(rows))
    for (ph, c), n in sorted(codes.items(), key=lambda x: (x[0][0], str(x[0][1]))):
        lines.append("  %-6s status %-4s %6d" % (ph, c, n))
    for ph, xs in sorted(by_phase.items()):
        lines.append("  %-6s latency p50 %.3fs  p95 %.3fs  p99 %.3fs  max %.3fs"
                     % (ph, _pct(xs, 50), _pct(xs, 95), _pct(xs, 99), max(xs)))

    blocked = []
    if not by_phase.get("sweep"):
        lines.append("\nNO SWEEP PHASE — read-behind-write blocking was not exercised.")
    elif not by_phase.get("quiet"):
        lines.append("\nNO QUIET PHASE — nothing to compare the sweep phase against.")
    else:
        for route, ph in sorted(by_route.items()):
            s, q = ph.get("sweep"), ph.get("quiet")
            if not s or not q or len(s) < 5 or len(q) < 5:
                continue
            s95, q95 = _pct(s, 95), _pct(q, 95)
            if s95 > max(q95 * block_ratio, min_abs_secs):
                blocked.append("%s: sweep p95 %.3fs vs quiet p95 %.3fs (x%.1f)"
                               % (route, s95, q95, s95 / max(q95, 1e-6)))
    fails += ["read blocked behind the writer? " + b for b in blocked]

    if slow:
        lines.append("\nSLOW — timed out, not counted as failures (a surface-performance finding):")
        lines += ["  %-48s %d" % (u, n) for u, n in slow.most_common()]

    lines.append("\nverdict: %s" % ("PASS" if not fails else "FAIL (%d)" % len(fails)))
    lines += ["  " + f for f in fails[:50]]
    if len(fails) > 50:
        lines.append("  … and %d more" % (len(fails) - 50))
    return "\n".join(lines) + "\n"


# ── scan-log ─────────────────────────────────────────────────────────────────

ERROR_PATTERNS = [
    ("SQLITE_MISUSE / misuse", re.compile(r"misuse", re.I)),
    ("ProgrammingError", re.compile(r"ProgrammingError")),
    ("database is locked", re.compile(r"database is locked", re.I)),
    ("cannot commit", re.compile(r"cannot commit", re.I)),
    ("error return without exception set", re.compile(r"error return without exception")),
    ("thread affinity", re.compile(r"(created in a thread|same thread|off the engine thread)", re.I)),
    ("NoneType arithmetic", re.compile(r"unsupported operand type.*NoneType")),
    ("Traceback", re.compile(r"^Traceback|\bTraceback \(most recent")),
]
# Timestamp anywhere on the line: the Supervisor log may prefix or colour it.
_COORD = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d).*?coord: (\S+) job=(\S+)(.*)$")
_ACTIVE = ("claimed", "running", "paused")          # job_coordinator._ACTIVE
_TRACEBACK = re.compile(r"^Traceback|\bTraceback \(most recent")
_DISCONNECT = re.compile(r"waitress\s+Socket error")


def scan_log(path, starve_min=60.0):
    sys.path.insert(0, ROOT)
    import job_coordinator as C
    manifest = C._MANIFEST
    hits = collections.defaultdict(list)
    active = collections.defaultdict(set)          # job -> keys active
    waiting = {}                                   # job -> (since, blocker)
    violations, seen_jobs, last_t = [], set(), None
    disconnects, prev = 0, ""
    for ln, line in enumerate(open(path, errors="replace"), 1):
        # waitress logs "Socket error" then the traceback when it tries to answer a
        # client that already gave up (a slow response). That is a symptom of a slow
        # route, not a fault — counted apart so it cannot bury a real traceback.
        if _TRACEBACK.search(line) and _DISCONNECT.search(prev):
            disconnects += 1
            prev = line
            continue
        if line.strip():
            prev = line
        for label, rx in ERROR_PATTERNS:
            if rx.search(line):
                hits[label].append((ln, line.rstrip()[:200]))
        m = _COORD.search(line)
        if not m:
            continue
        ts, event, job, rest = m.groups()
        f = dict(kv.split("=", 1) for kv in rest.split() if "=" in kv)
        try:
            t = dt.datetime.fromisoformat(ts)
        except ValueError:
            t = last_t
        last_t = t
        seen_jobs.add(job)
        key = f.get("key", job)
        becomes_active = event == "claim" or (event == "mark" and f.get("status") in _ACTIVE)
        if becomes_active:
            deps = manifest.get(job, ((), (), None))[0]
            clash = [d for d in deps if active[d]]
            if clash:
                violations.append("line %d: %s became %s while dependency %s was active%s"
                                  % (ln, job, f.get("status", "claimed"), ",".join(clash),
                                     " (forced)" if f.get("force") else ""))
            active[job].add(key)
            waiting.pop(job, None)
        elif event == "release" or (event == "mark" and f.get("status") not in _ACTIVE):
            active[job].discard(key)
        elif event in ("defer", "wait", "preempt"):
            waiting[job] = (t, f.get("blocker"))
        elif event == "admit_after":
            waiting.pop(job, None)

    out = ["log: %s" % path]
    for label, _ in ERROR_PATTERNS:
        h = hits.get(label, [])
        out.append("  %-38s %d" % (label, len(h)))
        out += ["      line %d: %s" % x for x in h[:5]]
    out.append("  %-38s %d   (client gave up on a slow response — not a fault)"
               % ("waitress client disconnects", disconnects))
    out.append("\ncoordinator: %d coord: members seen: %s"
               % (len(seen_jobs), ", ".join(sorted(seen_jobs)) or "none"))
    if not seen_jobs:
        out.append("  NOT EXERCISED — no member claimed, marked or was refused, so B's rules "
                   "were not tested by this log. Start members that overlap to test them.")
    starving = []
    for job, (since, blocker) in sorted(waiting.items()):
        mins = ((last_t - since).total_seconds() / 60.0) if since and last_t else 0.0
        if mins >= starve_min:
            starving.append("%s still waiting on %s after %.0f min at end of log"
                            % (job, blocker, mins))
    out += ["  DEPENDENCY VIOLATION " + v for v in violations]
    out += ["  STARVATION " + s for s in starving]
    db_errors = sum(len(hits.get(l, [])) for l, _ in ERROR_PATTERNS if l != "Traceback")
    ok = not violations and not starving and db_errors == 0
    out.append("\nverdict A (database faults): %s   verdict B (coordinator): %s"
               % ("PASS" if db_errors == 0 else "FAIL",
                  "NOT EXERCISED" if not seen_jobs else
                  ("PASS" if not violations and not starving else "FAIL")))
    if hits.get("Traceback"):
        out.append("  %d other traceback(s) — read them; not all are DB faults"
                   % len(hits["Traceback"]))
    return "\n".join(out) + "\n", ok


def cmd_scan_log(a):
    report, ok = scan_log(a.log, a.starve_min)
    print(report)
    return 0 if ok else 1


# ── compare-segments ─────────────────────────────────────────────────────────

def compare_segments(before_db, after_db, tol=1e-9):
    c = sqlite3.connect("file:%s?mode=ro" % after_db, uri=True)
    c.execute("ATTACH DATABASE ? AS b", ("file:%s?mode=ro" % before_db,))
    key = "block_start, meter_id, seq"
    cols = "kwh, inc_rate, exc_rate, band, attribution"
    q = lambda db: ("SELECT %s, %s FROM %s WHERE channel='import'" % (key, cols, db))
    before = {r[:3]: r[3:] for r in c.execute(q("b.block_segments"))}
    blocks_before = {k[:2] for k in before}
    after = {r[:3]: r[3:] for r in c.execute(q("main.block_segments")) if r[:2] in blocks_before}
    same = diff = 0
    samples = []
    for k, v in before.items():
        w = after.get(k)
        if w is None:
            continue
        num_eq = all((x is None and y is None) or (x is not None and y is not None
                     and abs(x - y) <= tol) for x, y in zip(v[:3], w[:3]))
        if num_eq and v[3:] == w[3:]:
            same += 1
        else:
            diff += 1
            if len(samples) < 10:
                samples.append("  %s  before %s  after %s" % (k, v, w))
    missing = len(set(before) - set(after))
    extra = len(set(after) - set(before))
    rebuilt = len({k[:2] for k in after})
    out = ["blocks whose segments were removed: %d; rebuilt: %d" % (len(blocks_before), rebuilt),
           "segment rows: %d identical, %d differ, %d missing, %d new" % (same, diff, missing, extra)]
    out += samples
    out.append("\nA difference is a finding to read, not automatically a failure: the sweep "
               "re-derives with today's code.")
    return "\n".join(out) + "\n"


def cmd_compare(a):
    print(compare_segments(a.before, a.after))


def main(argv=None):
    ap = argparse.ArgumentParser(description="EMT soak (A §5.4, B §5.6)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--base", default="http://192.168.20.16:8098")
    # First Pi soak ran 4 workers at 8 req/s: heavy whole-history routes then queued
    # behind each other on waitress's 4 threads, and 57 requests hit the 30 s timeout
    # for reasons that had nothing to do with the writer. 2 at 4 leaves headroom.
    r.add_argument("--workers", type=int, default=2)
    r.add_argument("--rate", type=float, default=4.0, help="max requests/second, all workers")
    r.add_argument("--timeout", type=float, default=30.0)
    r.add_argument("--quiet-min", type=float, default=30.0,
                   help="minutes of quiet baseline after the sweep finishes")
    r.add_argument("--max-hours", type=float, default=12.0)
    r.add_argument("--wait-for-sweep", type=float, default=15.0,
                   help="minutes to wait for the sweep to start before running anyway")
    r.add_argument("--block-ratio", type=float, default=5.0)
    r.add_argument("--out", default="soak-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("scan-log")
    s.add_argument("log")
    s.add_argument("--starve-min", type=float, default=60.0)
    s.set_defaults(fn=cmd_scan_log)
    c = sub.add_parser("compare-segments")
    c.add_argument("before")
    c.add_argument("after")
    c.set_defaults(fn=cmd_compare)
    a = ap.parse_args(argv)
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
