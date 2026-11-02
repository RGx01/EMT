"""
#47 (2 Oct 2026) — the Spiral / Usage Stats all-days load.

`/api/energy?fields=stats` returns the Usage Stats rows and nothing else. With
`include=stats` the response also carried the day rollups and a fingerprint per day over
the range: for all history, 0.17 s of a 0.40 s load on the dev database. The Charts page
read neither, because it keeps its rows by day and asks `fields=days` what moved.

`usage_view.day_stats` now reads its blocks as tuples (`_day_tuples`), with no joins, and
reads only the blocks the EV split needs (`_ev_rows`). Before, it fetched every block's
`sqlite3.Row` twice over. The result must not move by a bit. `TheSameRows` runs
`day_stats` on the new sources and on the old ones (`_raw_rows`, whole) over a store
built to hit every edge, and compares the two.

`TheField` tests fail on the parent tree, where `fields=stats` was ignored. `TheSameRows`
fails there too, as `_day_tuples` did not exist, but what it checks is that nothing moved.
It is a guard in spirit, and the real-database check was run by hand on the Mac: the
all-days payload is byte-identical on the dev and production-like databases.

The tie order is part of the answer. `_raw_rows` said `ORDER BY block_start` alone until
this change, which left the order of meters within a half-hour to the SQLite build. On CI's
Linux runners it was not rowid order, so `test_to_the_last_bit` failed there and passed on
the Mac. Both paths now order by `(block_start, id)`.
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "web"))

import usage_view as uv                         # noqa: E402
from block_store import BlockStore              # noqa: E402

TZ = "Europe/London"
META = {"timezone": TZ, "currency_symbol": "£", "billing_day": 3, "block_minutes": 30}


def _blank():
    st = BlockStore(os.path.join(tempfile.mkdtemp(), "blocks.db"))
    st._conn.execute("PRAGMA foreign_keys=OFF")
    return st


def _ins(st, bs, mid, cp=1, ev=None, **v):
    """`ev`: (kWh, £) of a stored EV split, written as the block's EV segment (step 5c:
    the split is only segments)."""
    be = (datetime.fromisoformat(bs) + timedelta(minutes=30)).isoformat()
    cols = ["block_start", "block_end", "meter_id", "config_period_id", "is_provisional",
            "interpolated"] + list(v)
    st._conn.execute("INSERT INTO blocks (%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))),
                     [bs, be, mid, cp, 0, 0] + list(v.values()))
    if ev is not None:
        st._conn.execute("INSERT INTO block_segments (block_start, meter_id, channel, seq, kwh, "
                         "inc_rate, exc_rate, band, attribution) "
                         "VALUES (?, ?, 'import', 0, ?, ?, NULL, 'off_peak', 'ev')",
                         (bs, mid, ev[0], ev[1] / ev[0]))


def _edge_store(ev_meter):
    """Two config periods (billing days 3 and 15, the second from 12:00Z on 1 Sep — a day
    split between them); two devices whose insertion order is the reverse of their
    `meter_id` order on alternate blocks; a block in a config period that does not exist;
    a meter in no config period; INTEGER-typed figures; every carbon branch (net import,
    net export, import == export, no import or export); standing charge zero and changing within
    a day; a stored EV split, and a completed dispatch."""
    st = _blank()
    c = st._conn
    c.execute("INSERT INTO config_periods (id, effective_from, effective_to, billing_day, block_minutes, timezone) "
              "VALUES (1, '2026-01-01T00:00:00', '2026-09-01T12:00:00', 3, 30, ?)", (TZ,))
    c.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, timezone) "
              "VALUES (2, '2026-09-01T12:00:00', 15, 30, ?)", (TZ,))
    devices = ["ev_charger", "house_battery"] if ev_meter else ["house_battery", "heat_pump"]
    for cp in (1, 2):
        c.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter) VALUES (?, 'electricity_main', 0)", (cp,))
        for d in devices:
            c.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter) VALUES (?, ?, 1)", (cp, d))
    t = datetime(2026, 8, 30, 22, 0)                   # BST: local days 30 Aug .. 2 Sep
    i = 0
    while t < datetime(2026, 9, 2, 23, 0):
        bs = t.isoformat()
        cp = 1 if t < datetime(2026, 9, 1, 12, 0) else 2
        imp = [1.2, 0.4, 0.5, 0.0, 2][i % 5]            # the 2 is an INTEGER
        exp = [0.0, 0.9, 0.5, 0.0, 0][i % 5]
        cg = [120.5, -30.25, 10.0, 7.0, None][i % 5]
        sc = [0.0, 0.0, 0.52, 0.52, 0.61][i % 5] if i % 7 else 0.48
        _ins(st, bs, "electricity_main", cp, imp_kwh=imp, exp_kwh=exp,
             imp_kwh_grid=(imp * 0.9 if i % 3 == 1 else None),
             imp_rate=[0.07, 0.3, 0.07][i % 3], imp_cost=round(imp * [0.07, 0.3, 0.07][i % 3], 4),
             exp_cost=round(exp * 0.15, 4), standing_charge=sc, carbon_g=cg,
             ev=((0.3, 0.021) if i % 4 == 0 else None))
        order = devices if i % 2 else devices[::-1]
        for j, d in enumerate(order):
            _ins(st, bs, d, cp, imp_kwh=0.1 * (j + 1) + 0.0001 * i, imp_kwh_grid=(None if i % 6 == 0 else 0.05 * (j + 1)),
                 imp_rate=[0.07, 0.3][i % 2], imp_cost=0.0123 * (j + 1) + 0.00001 * i,
                 exp_kwh=0.0, exp_cost=0.0, carbon_g=(3.3 if j else None), standing_charge=0.0)
        if i % 9 == 0:
            _ins(st, bs, "orphan_meter", cp, imp_kwh=0.7, imp_rate=0.1, imp_cost=0.07)
        if i % 11 == 0:
            _ins(st, bs, "house_battery_ghost", 99, imp_kwh=5.0, imp_rate=0.1, imp_cost=0.5)
        if not ev_meter and i % 5 == 1:
            c.execute("INSERT INTO dispatch_history (slot_start, kind, energy_kwh, first_seen, last_seen) "
                      "VALUES (?, 'completed', ?, ?, ?)", (bs, -0.4, bs, bs))
        t += timedelta(minutes=30)
        i += 1
    c.commit()
    return st, devices


def _cfg(devices, ev_meter):
    m = {"electricity_main": {"meta": dict(META), "channels": {}}}
    for d in devices:
        m[d] = {"meta": {"sub_meter": True, "device": d,
                         "meter_type": "ev_charger" if d == "ev_charger" else "battery"}, "channels": {}}
    return {"meters": m}


def _single_meter_store():
    """One meter and dispatch: the path that reconstructs EV from completed dispatches."""
    st = _blank()
    c = st._conn
    c.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, timezone) "
              "VALUES (1, '2026-01-01T00:00:00', 3, 30, ?)", (TZ,))
    c.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter) VALUES (1, 'electricity_main', 0)")
    t = datetime(2026, 9, 30, 23, 0)
    for i in range(96):
        bs = t.isoformat()
        _ins(st, bs, "electricity_main", imp_kwh=1.0 + 0.01 * i, imp_rate=0.07, imp_cost=0.07 + 0.0007 * i,
             standing_charge=0.48, carbon_g=50.0 + i,
             ev=((0.6, 0.042) if i % 8 == 0 else None))
        if i % 3 == 0:
            c.execute("INSERT INTO dispatch_history (slot_start, kind, energy_kwh, first_seen, last_seen) "
                      "VALUES (?, 'completed', ?, ?, ?)", (bs, -0.5, bs, bs))
        t += timedelta(minutes=30)
    c.commit()
    return st


def _old_sources():
    """day_stats as it was: every block's row, through the adapter, for both the
    aggregation and the EV split."""
    def tuples(store, s, e):
        rows = uv._raw_rows(store, s, e)
        out = []
        for r in rows:
            out.append((r["block_start"], r["meter_id"], r["is_sub_meter"], r["billing_day"],
                        r["imp_kwh"], r["imp_kwh_grid"], r["imp_rate"],
                        r["imp_cost"], r["exp_kwh"], r["exp_cost"], r["standing_charge"],
                        r["carbon_g"]))
        return out
    return (mock.patch.object(uv, "_day_tuples", tuples),
            mock.patch.object(uv, "_ev_rows", lambda store, cfg, meters, s, e: uv._raw_rows(store, s, e)))


class TheSameRows(unittest.TestCase):

    def _both(self, st, cfg):
        lo = "2026-08-29T23:00:00"
        hi = "2026-10-03T23:00:00"
        new = uv.day_stats(st, cfg, TZ, lo, hi)
        a, b = _old_sources()
        with a, b:
            old = uv.day_stats(st, cfg, TZ, lo, hi)
        return new, old

    def test_devices_periods_and_every_carbon_branch(self):
        st, devices = _edge_store(ev_meter=False)
        self.addCleanup(st.close)
        new, old = self._both(st, _cfg(devices, False))
        self.assertTrue(new["rows"])
        self.assertEqual(new, old)
        # it exercised what it says: a day of each billing day, and both devices
        self.assertEqual({r["billing_day"] for r in new["rows"]}, {3, 15})
        self.assertTrue(all(set(devices) <= set(r["meters"]) for r in new["rows"]))

    def test_to_the_last_bit(self):
        """Unrounded: the rows above are rounded to 4 dp, which hides an addition made in
        another order. Two devices' costs in one half-hour share a running sum, so the
        tie-break between them is part of the answer."""
        from zoneinfo import ZoneInfo
        st, devices = _edge_store(ev_meter=False)
        self.addCleanup(st.close)
        lo, hi = "2026-08-29T23:00:00", "2026-10-03T23:00:00"
        day = uv._local_day_of(lo, hi, ZoneInfo(TZ))
        new = uv._aggregate_tuples(uv._day_tuples(st, lo, hi), day, billing_day_fallback=3)
        old = uv._aggregate_block_rows(uv._raw_rows(st, lo, hi), day, billing_day_fallback=3)
        self.assertEqual(new, old)

    def test_with_an_EV_sub_meter(self):
        st, devices = _edge_store(ev_meter=True)
        self.addCleanup(st.close)
        new, old = self._both(st, _cfg(devices, True))
        self.assertEqual(new, old)

    def test_one_meter_with_dispatch(self):
        st = _single_meter_store()
        self.addCleanup(st.close)
        cfg = {"meters": {"electricity_main": {"meta": dict(META), "channels": {}}}}
        new, old = self._both(st, cfg)
        self.assertEqual(new, old)
        self.assertIn("ev_dispatch", [m["id"] for m in new["meters"]], "the dispatch EV split ran")


def _server():
    spec = importlib.util.spec_from_file_location("server_fs", os.path.join(_ROOT, "web", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv


class TheField(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv = _server()
        cls.st, devices = _edge_store(ev_meter=False)
        cls.cfg = _cfg(devices, False)

    @classmethod
    def tearDownClass(cls):
        cls.st.close()

    def _get(self, q):
        with mock.patch.object(self.srv.store_access, "store", lambda: self.st), \
             mock.patch.object(self.srv, "load_config", lambda: self.cfg), \
             mock.patch.object(self.srv, "_config_timezone", lambda: TZ):
            return self.srv.app.test_client().get("/api/energy?" + q)

    def test_only_the_stats(self):
        j = self._get("resolution=day&kind=all&fields=stats").get_json()
        self.assertEqual(sorted(j), ["period", "range", "resolution", "stats", "timezone"])

    def test_the_same_stats_as_include(self):
        for q in ("resolution=day&kind=all", "resolution=day&kind=range&start=2026-08-31&end=2026-09-01",
                  "resolution=block&kind=day&date=2026-09-01"):
            a = self._get(q + "&fields=stats").get_json()
            b = self._get(q + "&include=stats").get_json()
            self.assertEqual(a["stats"], b["stats"], q)
            self.assertEqual((a["period"], a["range"]), (b["period"], b["range"]), q)

    def test_not_at_period(self):
        self.assertEqual(self._get("resolution=period&kind=month&fields=stats").status_code, 400)


if __name__ == "__main__":
    unittest.main()
