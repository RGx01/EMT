#!/usr/bin/env python3
"""
Parity on a REAL database for a tariff the fixtures can only imitate (5 Oct 2026).

`tests/tariff_fixtures.py` covers flat, Economy 7, Go and Agile synthetically, in CI. This is the
local companion for a real database that predates `block_segments` — an old v4 backup from a
non-IOG account, whose own API key we do not have. It:

  1. copies the database (the source is never written), and opens the copy with v5's store,
     which adds the tables and columns it lacks;
  2. builds segments and ex-VAT with v5's own historical re-price sweep, from a rate schedule
     REBUILT FROM THE STORED RATES: each half-hour's inc rate is the one the block already holds
     (on Agile, the real published price), its ex-VAT the inc divided by 1 + VAT on its local day
     (vat_calendar's seed);
  3. records what every data surface answers, as SHA-256s (and, with --dump, in full).

Run it before and after a change and compare. Nothing it reads or writes belongs in the repo:
point --out at a scratch directory. Real customer figures stay out of commits and issues.

    python3 .build/real_db_parity.py SOURCE.db --out DIR            # record
    python3 .build/real_db_parity.py SOURCE.db --out DIR --compare  # what moved since
"""
import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, ".build"))


class _StoredExc:
    def __init__(self, sched): self.s = sched
    def resolve(self, ts): return self.s.exc_at(ts)
    def flat_rate(self): return self.s.exc_at(self.s.first)


class StoredSchedule:
    """The sweep's rate-schedule interface, answered from the blocks' own stored inc rates."""
    def __init__(self, conn):
        import vat_calendar
        self.inc = {r[0]: float(r[1]) for r in conn.execute(
            "SELECT block_start, imp_rate FROM blocks WHERE meter_id = 'electricity_main' "
            "AND imp_rate IS NOT NULL")}
        self.first = min(self.inc) if self.inc else "2000-01-01T00:00:00"
        self._vat = vat_calendar.rate_lookup([])
        self.exc = _StoredExc(self)

    @staticmethod
    def _key(ts):
        d = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if d.tzinfo is not None:
            d = d.astimezone(timezone.utc).replace(tzinfo=None)
        return d.replace(minute=0 if d.minute < 30 else 30, second=0, microsecond=0).isoformat()

    def resolve(self, ts):
        return self.inc.get(self._key(ts), 0.0)

    def exc_at(self, ts):
        k = self._key(ts)
        return round(self.inc.get(k, 0.0) / (1 + self._vat(k)), 8)

    def flat_rate(self):
        return self.inc.get(self.first, 0.0)

    def is_empty(self):
        return not self.inc


def prepare(source, workdir):
    """A v5-shaped copy of `source` in `workdir`, with segments and ex-VAT built."""
    import engine
    import job_coordinator
    from block_store import BlockStore
    path = os.path.join(workdir, "blocks.db")
    shutil.copyfile(source, path)
    st = BlockStore(path)
    sched = StoredSchedule(st._conn)

    async def _segs(ch):
        return [("2000-01-01T00:00:00", None, sched)]
    job_coordinator.reset_for_tests()
    with mock.patch.object(engine, "_store", st), \
         mock.patch.object(engine, "_kraken_rate_schedules", {"import": sched}), \
         mock.patch.object(engine, "_build_channel_rate_segs", _segs), \
         mock.patch.object(engine, "_hist_rate_segs_cache", {}), \
         mock.patch.object(engine, "_write_reprice_report", lambda **k: None):
        st.set_meta(engine._REPRICE_HISTORY_MARKER, {})
        for _ in range(500):
            asyncio.run(engine._run_historical_reprice_sweep(batch=500, pace_s=0, force=True))
            if (st.get_meta(engine._REPRICE_HISTORY_MARKER, {}) or {}).get("done"):
                break
    job_coordinator.reset_for_tests()
    last = st._conn.execute("SELECT MAX(block_start) FROM blocks").fetchone()[0]
    st.close()
    return path, last


def requests_for(last):
    """The surfaces over the last full month, quarter and year before `last`."""
    d = datetime.fromisoformat(last)
    y, m = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    q = (m - 1) // 3 + 1
    return [
        ("Usage Stats: all days", "/api/energy", "resolution=day&kind=all&fields=stats"),
        ("Heatmap: last full month", "/api/energy", "resolution=block&kind=month&year=%d&month=%d" % (y, m)),
        ("Bill: last full month", "/api/energy", "resolution=period&kind=month&year=%d&month=%d&include=billing" % (y, m)),
        ("Bill: its quarter", "/api/energy", "resolution=period&kind=quarter&year=%d&quarter=%d&include=billing" % (y, q)),
        ("Bill: its year", "/api/energy", "resolution=period&kind=year&year=%d&include=billing" % y),
        ("Day charts: last full month", "/api/energy", "resolution=day&kind=month&year=%d&month=%d&include=billing" % (y, m)),
        ("Insights carbon", "/api/energy", "resolution=period&kind=month&year=%d&month=%d&include=carbon" % (y, m)),
        ("Insights usage", "/api/energy", "resolution=period&kind=month&year=%d&month=%d&include=usage" % (y, m)),
        ("Periods: picker totals", "/api/periods", "kind=billing&include=net_cost"),
        ("Periods: trend strip", "/api/periods", "kind=month&include=carbon"),
    ]


def capture(source):
    import importlib.util
    import store_access
    import tariff_goldens as G
    work = tempfile.mkdtemp()
    path, last = prepare(source, work)
    spec = importlib.util.spec_from_file_location("server_rp", os.path.join(ROOT, "web", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    srv.DATA_DIR = work
    store_access.reset_for_tests()
    store_access.bind(path)
    out = {}
    try:
        client = srv.app.test_client()
        for label, route, q in requests_for(last):
            r = client.get(route + "?" + q)
            out[label] = {"status": r.status_code, "body": G._normalise(r.get_json())}
    finally:
        store_access.reset_for_tests()
        shutil.rmtree(work, ignore_errors=True)
    return out


def main():
    import tariff_goldens as G
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--out", required=True)
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--dump", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    full = capture(a.source)
    got = G.digest(full)
    rec = os.path.join(a.out, "parity.json")
    if a.dump:
        with open(os.path.join(a.out, "answers.json"), "w") as fh:
            json.dump(full, fh, indent=1, sort_keys=True)
    if a.compare:
        want = json.load(open(rec))
        moved = [k for k in got if got[k] != want.get(k)]
        for k in moved:
            print("MOVED ", k)
        print("%d of %d answer(s) moved" % (len(moved), len(got)))
        sys.exit(1 if moved else 0)
    with open(rec, "w") as fh:
        json.dump(got, fh, indent=1, sort_keys=True)
    bad = [k for k, v in got.items() if v["status"] != 200]
    print("recorded %d answers to %s%s" % (len(got), rec, ("; NOT 200: %s" % bad) if bad else ""))


if __name__ == "__main__":
    main()
