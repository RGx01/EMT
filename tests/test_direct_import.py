"""
Direct import — one rule, and every surface that shows it agrees (6 Oct 2026).

`direct_import.py` holds the rule: per half-hour, Direct = max(0, main − device grid shares), a
negative main £ (a plunge credit) passing through. The clip that keeps the devices' grid shares
within the main is the house/device split's (`engine._apply_pass2`), at write time; the fixtures
take every grid share from that function, so these tests hold no copy of it.

Four surfaces show a Direct import line: the Bill card (`/api/energy … include=billing`), the
Overview cost cards (`bill_card.live_rows` over `BlockStore.get_day_live_rollups`, as
`/api/periods?kind=live` builds them), Usage Stats (`/api/energy … fields=stats`) and the Billing
tab's day charts (`/api/energy resolution=day … include=billing`, each day's panel breakdown —
joined 8 Oct 2026, step 5, when it stopped reading the stored remainder). Before the rule they
worked it out their own ways and, on a fixture whose devices out-read the main, gave different
answers for one period. Asserted here, on every tariff fixture, for a billing period and a calendar month:

  * the three surfaces show the same Direct import and the same figure for every device;
  * on each, Direct + devices is the main meter's own import — except on `e7_gap`, whose
    unsettled gap blocks the split records as-is: there the breakdown reads above the meter by
    exactly the devices' unclipped excess, until settlement re-clips it (accepted, 6 Oct 2026);
  * the devices' own readings from Home Assistant are stored as recorded, whatever was clipped.

kWh are compared to the 3 dp every surface prints. £ to a penny: the bill sums a section's rate
rows each rounded to 2 dp, the Overview rounds once, and on a long section the two can sit a penny
apart — a display rounding, not a rule.

DISCRIMINATING: on the tree before the rule (2026-10-06, 97f40a5) the agreement tests failed on the
over-the-main fixture and on Agile; with every grid share clipped by the split as it now is, they
are guards there. `e7_gap` is the one fixture the rule's floor still acts on.
"""
import importlib.util
import itertools
import os
import sqlite3
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, ".build"))
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

import direct_import as D                         # noqa: E402

# (label, /api/energy period query, Usage Stats day range)
WINDOWS = [
    ("billing period from 3 Oct", "kind=billing&period_start=2026-10-03",
     ("2026-10-03", "2026-11-02")),
    ("September", "kind=month&year=2026&month=9", ("2026-09-01", "2026-09-30")),
]
PENNY = 0.0101
SURFACES = ("Bill card", "Overview card", "Usage Stats", "Day charts")


class TheRule(unittest.TestCase):

    def test_direct_floors_and_keeps_a_credit(self):
        self.assertEqual(D.direct(1.0, 0.30, 0.4, 0.12), (0.6, 0.30 - 0.12))
        self.assertEqual(D.direct(1.0, 0.30, 3.0, 0.90), (0.0, 0.0))
        self.assertEqual(D.direct(1.0, -0.05, 0.4, 0.02), (0.6, -0.07))     # a plunge credit

    def test_the_sql_form_is_the_same_rule(self):
        """The store sums Direct in SQL; its expressions must be `direct`, not a cousin."""
        conn = sqlite3.connect(":memory:")
        vals = (-0.4, -0.05, 0.0, 0.2, 0.6, 3.0)
        for m, d in itertools.product(vals, repeat=2):
            got_k, got_c = conn.execute("SELECT %s, %s" % (D.sql_kwh("?", "?"), D.sql_cost("?", "?")),
                                        (m, d, m, m, d, m, d)).fetchone()
            want_k, want_c = D.direct(m, m, d, d)
            with self.subTest(main=m, devices=d):
                self.assertAlmostEqual(got_k, want_k)
                self.assertAlmostEqual(got_c, want_c)


def _norm(name):
    return name.strip().lower().replace(" ", "_")


def _label_kwh(label):
    """('heat_pump_1', 7.2) from '↳ heat_pump_1 (7.200 kWh)'."""
    head, kwh = label.rsplit(" (", 1)
    return head.replace("↳", "").strip(), float(kwh.split()[0])


class _Fixture:
    """One fixture database and the three surfaces' Direct and devices per window."""

    def __init__(self, tariff, devices):
        import store_access
        import tariff_fixtures as T
        d = tempfile.mkdtemp()
        self.path = T.build(os.path.join(d, "blocks.db"), tariff, devices)
        spec = importlib.util.spec_from_file_location("server_di", os.path.join(_ROOT, "web", "server.py"))
        srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(srv)
        srv.DATA_DIR = d
        store_access.reset_for_tests()
        store_access.bind(self.path)
        self.client = srv.app.test_client()
        self.store = store_access.store()
        self.cfg = srv.load_config()
        self.ev = None
        for mid, m in (self.cfg.get("meters") or {}).items():
            if ((m.get("meta") or {}).get("meter_type") or "").startswith("ev"):
                self.ev = mid

    def close(self):
        import store_access
        store_access.reset_for_tests()

    def _q(self, sql, days):
        lo, hi = self._utc(days)
        return sqlite3.connect(self.path).execute(sql, (lo, hi)).fetchone()

    def main(self, days):
        return self._q("SELECT COALESCE(SUM(imp_kwh), 0), COALESCE(SUM(imp_cost), 0) FROM blocks "
                       "WHERE meter_id = 'electricity_main' AND block_start >= ? AND block_start < ?",
                       days)

    def unclipped_excess(self, days):
        """(kWh, £) by which the devices' grid shares exceed the main, summed over half-hours —
        nonzero only where the split recorded a device as-is."""
        # Excess = what the rule's floor added back: direct − (main − devices). From the rule's
        # own SQL form, so a plunge credit (main £ < 0, passed through unfloored) counts as none.
        dk, dc = D.sql_kwh("m.imp_kwh", "s.k"), D.sql_cost("m.imp_cost", "s.c")
        return self._q(
            "WITH s AS (SELECT block_start, SUM(COALESCE(imp_kwh_grid, imp_kwh)) k, SUM(imp_cost) c "
            "           FROM blocks WHERE meter_id != 'electricity_main' GROUP BY block_start) "
            "SELECT COALESCE(SUM(%s - (m.imp_kwh - s.k)), 0), COALESCE(SUM(%s - (m.imp_cost - s.c)), 0) "
            "FROM blocks m JOIN s ON s.block_start = m.block_start "
            "WHERE m.meter_id = 'electricity_main' AND m.block_start >= ? AND m.block_start < ?"
            % (dk, dc), days)

    @staticmethod
    def _utc(days):
        from datetime import date, datetime, timedelta, timezone
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/London")
        a, b = date.fromisoformat(days[0]), date.fromisoformat(days[1]) + timedelta(days=1)
        f = lambda x: datetime(x.year, x.month, x.day, tzinfo=tz).astimezone(
            timezone.utc).replace(tzinfo=None).isoformat()
        return f(a), f(b)

    def bill(self, query):
        b = self.client.get("/api/energy?resolution=period&%s&include=billing" % query).get_json()["billing"]
        out, cur = {}, None
        for r in b["card"]["rows"]:
            if r["type"] == "breakdown":
                cur = "breakdown"
            elif r["type"] == "title" and cur is not None:
                cur = r["text"]
            elif r["type"] == "total" and cur not in (None, "breakdown"):
                out[cur] = (float(r["kwh"]), float(r["cost"]))
        direct = out.pop("Direct import")
        # The card prints a section for a device that drew nothing; the other surfaces leave
        # it out (as on the flat fixture, where nothing is cheap enough for the devices).
        return direct, {(self.ev if _norm(k) == "ev" else _norm(k)): v for k, v in out.items()
                        if v[0] > 0.001 or v[1] > 0.001}

    def overview(self, days):
        from datetime import date, timedelta
        import bill_card
        lo, hi = self._utc(days)
        live = self.store.get_day_live_rollups(lo, hi, "Europe/London", self.ev)
        d0, d1 = date.fromisoformat(days[0]), date.fromisoformat(days[1])
        ds = [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]
        direct, devs = None, {}
        for r in bill_card.live_rows(live, ds, self.cfg, self.ev):
            name, kwh = (_label_kwh(r["label"]) if "kWh)" in r["label"] else (r["label"], None))
            if name == "Direct import":
                direct = (kwh, r["cost"])
            elif r["label"].startswith("↳"):
                devs[_norm(self.ev if name == "EV" else name)] = (kwh, r["cost"])
        return direct, devs

    def usage(self, days):
        rows = self.client.get("/api/energy?resolution=day&kind=range&start=%s&end=%s&fields=stats"
                               % days).get_json()["stats"]["rows"]
        acc = {}
        for r in rows:
            for mid, m in r["meters"].items():
                a = acc.setdefault(mid, [0.0, 0.0])
                a[0] += m.get("imp_kwh") or 0.0
                a[1] += m.get("imp_cost") or 0.0
        direct = tuple(acc.pop("electricity_main"))
        return direct, {_norm(k): tuple(v) for k, v in acc.items() if v[0] > 0.001 or v[1] > 0.001}

    def day_charts(self, days):
        """The Billing tab's day charts over the window: each day's panel breakdown, summed."""
        b = self.client.get("/api/energy?resolution=day&kind=range&start=%s&end=%s&include=billing"
                            % days).get_json()["billing"]
        acc = {"electricity_main": [0.0, 0.0]}
        for day in b["days"]:
            panel = day["panel"]
            if not panel["breakdown"]:
                # No devices: nothing to break down — the whole import is the house.
                acc["electricity_main"][0] += panel["total_import"] or 0.0
                acc["electricity_main"][1] += panel["import_cost"] or 0.0
            for line in panel["breakdown"]:
                a = acc.setdefault(line["meter"], [0.0, 0.0])
                a[0] += line["kwh"] or 0.0
                a[1] += line["cost"] or 0.0
        direct = tuple(acc.pop("electricity_main"))
        return direct, {(self.ev if _norm(k) in ("ev", "ev_dispatch") else _norm(k)): tuple(v)
                        for k, v in acc.items() if v[0] > 0.001 or v[1] > 0.001}


class EverySurfaceAgrees(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import tariff_goldens as G
        cls.answers = {}
        for tariff, devices in G.fixtures():
            fx = _Fixture(tariff, devices)
            try:
                for label, query, days in WINDOWS:
                    cls.answers[(G.name(tariff, devices), label)] = {
                        "Bill card": fx.bill(query),
                        "Overview card": fx.overview(days),
                        "Usage Stats": fx.usage(days),
                        "Day charts": fx.day_charts(days),
                        "main": fx.main(days),
                        "excess": fx.unclipped_excess(days),
                    }
            finally:
                fx.close()

    def _close(self, a, b):
        return round(a[0], 3) == round(b[0], 3) and abs(a[1] - b[1]) <= PENNY

    def test_the_same_direct_import(self):
        for key, s in self.answers.items():
            ref = s["Bill card"][0]
            for surface in SURFACES[1:]:
                with self.subTest(fixture=key[0], window=key[1], surface=surface):
                    got = s[surface][0]
                    self.assertTrue(self._close(got, ref),
                                    "Direct import %r on the %s, %r on the Bill card" % (got, surface, ref))

    def test_the_same_devices(self):
        for key, s in self.answers.items():
            ref = s["Bill card"][1]
            for surface in SURFACES[1:]:
                got = s[surface][1]
                with self.subTest(fixture=key[0], window=key[1], surface=surface):
                    self.assertEqual(sorted(got), sorted(ref))
                    for mid in ref:
                        self.assertTrue(self._close(got[mid], ref[mid]),
                                        "%s: %r on the %s, %r on the Bill card"
                                        % (mid, got[mid], surface, ref[mid]))

    def test_each_surface_adds_up_to_the_main_meter_but_for_unclipped_excess(self):
        for key, s in self.answers.items():
            main_k, main_c = s["main"]
            xk, xc = s["excess"]
            for surface in SURFACES:
                direct, devs = s[surface]
                lines = 1 + len(devs)                      # each within a penny
                with self.subTest(fixture=key[0], window=key[1], surface=surface):
                    self.assertAlmostEqual(direct[0] + sum(v[0] for v in devs.values()),
                                           main_k + xk, places=2)
                    self.assertLessEqual(
                        abs(direct[1] + sum(v[1] for v in devs.values()) - main_c - xc),
                        PENNY * lines, "the lines miss the meter (+ excess) by more than rounding")

    def test_only_the_gap_fixture_has_unclipped_excess(self):
        """The split clips every ordinary block; only an unsettled gap block keeps a device over
        the main — and the gap fixture does have one."""
        for key, s in self.answers.items():
            with self.subTest(fixture=key[0], window=key[1]):
                if key == ("e7_gap", "billing period from 3 Oct"):
                    self.assertGreater(s["excess"][0], 1.0)
                elif not key[0].startswith("e7_gap"):
                    self.assertAlmostEqual(s["excess"][0], 0.0, places=6)


class TheDeviceReadingsAreKept(unittest.TestCase):
    """The split clips the devices' GRID shares; the readings Home Assistant gave stay as recorded
    (6 Oct 2026). On `e7_over` battery_1 records 3 kWh the main never saw in the cheap slots of every
    fifth day. Which device the split then clips is its priority order's business; what is pinned
    here is that every device's stored imp_kwh is what it reported, and only imp_kwh_grid is cut."""

    def test_the_devices_keep_what_they_recorded(self):
        from datetime import timedelta, timezone
        import tariff_fixtures as T
        over = sqlite3.connect(T.build(os.path.join(tempfile.mkdtemp(), "blocks.db"), "e7", "over"))
        many = sqlite3.connect(T.build(os.path.join(tempfile.mkdtemp(), "blocks.db"), "e7", "many"))
        q = ("SELECT meter_id, SUM(imp_kwh), SUM(imp_kwh_grid) FROM blocks "
             "WHERE meter_id != 'electricity_main' GROUP BY meter_id")
        o = {m: (r, g) for m, r, g in over.execute(q)}
        n = {m: (r, g) for m, r, g in many.execute(q)}
        # What battery_1 reported beyond "many": 3 kWh in each half-hour _unmetered names.
        extra, t = 0.0, T.FROM.astimezone(timezone.utc).replace(tzinfo=None)
        end = T.TO.astimezone(timezone.utc).replace(tzinfo=None)
        while t < end:
            loc = t.replace(tzinfo=timezone.utc).astimezone(T.LONDON)
            extra += T._unmetered("over", loc, loc.hour * 60 + loc.minute,
                                  T.inc_rate("e7", t) < 0.13).get("battery_1", 0.0)
            t += timedelta(minutes=30)
        self.assertGreater(extra, 0)
        for mid in n:
            with self.subTest(device=mid):
                want = n[mid][0] + (extra if mid == "battery_1" else 0.0)
                self.assertAlmostEqual(o[mid][0], want, places=3, msg="the HA reading was not kept")
        self.assertLess(sum(g for _, g in o.values()), sum(r for r, _ in o.values()) - 1.0,
                        "the split clipped nothing")


class AStaleRemainderIsNotRead(unittest.TestCase):
    """The real shape that showed two databases' Usage Stats days below the meter (6 Oct 2026): a
    device's rows gone, its stored remainder left behind. No surface may read it — Direct is the
    rule on what is there. Discriminating for the day charts (they read the stored remainder before
    step 5, 8 Oct 2026); the other surfaces had already moved."""

    def test_every_surface_ignores_a_stale_remainder(self):
        import tariff_fixtures as T  # noqa: F401
        fx = _Fixture("e7", False)
        try:
            days = ("2026-10-05", "2026-10-05")
            lo, hi = fx._utc(days)
            with sqlite3.connect(fx.path) as c:
                c.execute("UPDATE blocks SET imp_kwh_remainder = imp_kwh / 2, "
                          "imp_cost_remainder = imp_cost / 2 WHERE meter_id = 'electricity_main' "
                          "AND block_start >= ? AND block_start < ?", (lo, hi))
            main_k, main_c = fx.main(days)
            for surface, got in (("Usage Stats", fx.usage(days)[0]),
                                 ("Day charts", fx.day_charts(days)[0])):
                with self.subTest(surface=surface):
                    self.assertAlmostEqual(got[0], main_k, places=3)
                    self.assertLessEqual(abs(got[1] - main_c), PENNY)
        finally:
            fx.close()


if __name__ == "__main__":
    unittest.main()
