"""
Direct import — one rule, and every surface that shows it agrees (6 Oct 2026).

`direct_import.py` holds the rule: per half-hour, Direct = max(0, main − devices) (a negative main £,
a plunge credit, passes through), and over any span Direct + devices − beyond = main. Three surfaces
show a Direct import line: the Bill card (`/api/energy … include=billing`), the Overview cost cards
(`bill_card.live_rows` over `BlockStore.get_day_live_rollups`, as `/api/periods?kind=live` builds
them) and Usage Stats (`/api/energy … fields=stats`). Before the rule they floored in three places —
per half-hour, per window, per day — and gave three answers on the fixture where devices out-read
the main ("over"): 87.64 kWh / £22.63, 8.96 kWh / £13.57, and 87.64 kWh / £17.43.

Asserted here, on every tariff fixture, for a billing period and a calendar month:

  * the three surfaces show the same Direct import, the same figure for every device, and the
    same `beyond` (or none);
  * on each, Direct + devices − beyond is the main meter's own metered import.

A surface that works Direct out its own way again fails here. kWh are compared to the 3 dp every
surface prints. £ to a penny: the bill sums a section's rate rows each rounded to 2 dp, the Overview
rounds once, and on a long section the two can sit a penny apart — a display rounding, not a rule.

DISCRIMINATING: on the parent tree the "over" fixture fails the agreement tests (the three answers
above). The other fixtures are guards — no floor binds on them.
"""
import itertools
import os
import sqlite3
import sys
import tempfile
import unittest
import importlib.util

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, ".build"))
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

import direct_import as D                         # noqa: E402

# (label, /api/energy period query, Usage Stats day range, [local days])
WINDOWS = [
    ("billing period from 3 Oct", "kind=billing&period_start=2026-10-03",
     ("2026-10-03", "2026-11-02")),
    ("September", "kind=month&year=2026&month=9", ("2026-09-01", "2026-09-30")),
]
PENNY = 0.0101


class TheRule(unittest.TestCase):

    def test_direct_floors_and_keeps_a_credit(self):
        self.assertEqual(D.direct(1.0, 0.30, 0.4, 0.12), (0.6, 0.30 - 0.12))
        self.assertEqual(D.direct(1.0, 0.30, 3.0, 0.90), (0.0, 0.0))
        self.assertEqual(D.direct(1.0, -0.05, 0.4, 0.02), (0.6, -0.07))     # a plunge credit

    def test_beyond_closes_the_sum(self):
        main_k, main_c, dev_k, dev_c = 1.0, 0.30, 3.0, 0.90
        dk, dc = D.direct(main_k, main_c, dev_k, dev_c)
        bk, bc = D.beyond(main_k, main_c, dev_k, dev_c, dk, dc)
        self.assertAlmostEqual(dk + dev_k - bk, main_k)
        self.assertAlmostEqual(dc + dev_c - bc, main_c)

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
    """One fixture database and the three surfaces' Direct, devices and beyond per window."""

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

    def main(self, days):
        lo, hi = self._utc(days)
        return sqlite3.connect(self.path).execute(
            "SELECT COALESCE(SUM(imp_kwh), 0), COALESCE(SUM(imp_cost), 0) FROM blocks "
            "WHERE meter_id = 'electricity_main' AND block_start >= ? AND block_start < ?",
            (lo, hi)).fetchone()

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
        out, cur, beyond = {}, None, None
        for r in b["card"]["rows"]:
            if r["type"] == "breakdown":
                cur = "breakdown"
            elif r["type"] == "title" and cur is not None:
                cur = r["text"]
            elif r["type"] == "total" and cur not in (None, "breakdown"):
                out[cur] = (float(r["kwh"]), float(r["cost"]))
            elif r.get("cls") == "beyond":
                v = r["value"]
                # printed as a deduction, "(9.05)": the beyond itself is the positive 9.05
                beyond = (_label_kwh(r["text"])[1], float(v.strip("()")) * (1 if v.startswith("(") else -1))
        direct = out.pop("Direct import")
        # The card prints a section for a device that drew nothing; the other surfaces leave
        # it out (as on the flat fixture, where nothing is cheap enough for the devices).
        return direct, {(self.ev if _norm(k) == "ev" else _norm(k)): v for k, v in out.items()
                        if v[0] > 0.001 or v[1] > 0.001}, beyond

    def overview(self, days):
        from datetime import date, timedelta
        import bill_card
        lo, hi = self._utc(days)
        live = self.store.get_day_live_rollups(lo, hi, "Europe/London", self.ev)
        d0, d1 = date.fromisoformat(days[0]), date.fromisoformat(days[1])
        ds = [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]
        direct, devs, beyond = None, {}, None
        for r in bill_card.live_rows(live, ds, self.cfg, self.ev):
            name, kwh = (_label_kwh(r["label"]) if "kWh)" in r["label"] else (r["label"], None))
            if name == "Direct import":
                direct = (kwh, r["cost"])
            elif name == D.BEYOND_LABEL:
                beyond = (kwh, -r["cost"])
            elif r["label"].startswith("↳"):
                devs[_norm(self.ev if name == "EV" else name)] = (kwh, r["cost"])
        return direct, devs, beyond

    def usage(self, days):
        rows = self.client.get("/api/energy?resolution=day&kind=range&start=%s&end=%s&fields=stats"
                               % days).get_json()["stats"]["rows"]
        acc, bey = {}, [0.0, 0.0]
        for r in rows:
            for mid, m in r["meters"].items():
                a = acc.setdefault(mid, [0.0, 0.0])
                a[0] += m.get("imp_kwh") or 0.0
                a[1] += m.get("imp_cost") or 0.0
            if r.get("beyond"):
                bey[0] += r["beyond"]["imp_kwh"]
                bey[1] += r["beyond"]["imp_cost"]
        direct = tuple(acc.pop("electricity_main"))
        devs = {_norm(k): tuple(v) for k, v in acc.items() if v[0] > 0.001 or v[1] > 0.001}
        return direct, devs, (tuple(bey) if D.shows_beyond(*bey) else None)


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
                        "main": fx.main(days),
                    }
            finally:
                fx.close()

    def _close(self, a, b, kwh_places=3):
        return (round(a[0], kwh_places) == round(b[0], kwh_places)
                and abs(a[1] - b[1]) <= PENNY)

    def test_the_same_direct_import(self):
        for key, s in self.answers.items():
            ref = s["Bill card"][0]
            for surface in ("Overview card", "Usage Stats"):
                with self.subTest(fixture=key[0], window=key[1], surface=surface):
                    got = s[surface][0]
                    self.assertTrue(self._close(got, ref),
                                    "Direct import %r on the %s, %r on the Bill card" % (got, surface, ref))

    def test_the_same_devices(self):
        for key, s in self.answers.items():
            ref = s["Bill card"][1]
            for surface in ("Overview card", "Usage Stats"):
                got = s[surface][1]
                with self.subTest(fixture=key[0], window=key[1], surface=surface):
                    self.assertEqual(sorted(got), sorted(ref))
                    for mid in ref:
                        self.assertTrue(self._close(got[mid], ref[mid]),
                                        "%s: %r on the %s, %r on the Bill card"
                                        % (mid, got[mid], surface, ref[mid]))

    def test_the_same_beyond(self):
        for key, s in self.answers.items():
            ref = s["Bill card"][2]
            for surface in ("Overview card", "Usage Stats"):
                got = s[surface][2]
                with self.subTest(fixture=key[0], window=key[1], surface=surface):
                    self.assertEqual(got is None, ref is None, "%r against %r" % (got, ref))
                    if ref is not None:
                        self.assertTrue(self._close(got, ref), "%r against %r" % (got, ref))

    def test_each_surface_adds_up_to_the_main_meter(self):
        for key, s in self.answers.items():
            main_k, main_c = s["main"]
            for surface in ("Bill card", "Overview card", "Usage Stats"):
                direct, devs, beyond = s[surface]
                bk, bc = beyond or (0.0, 0.0)
                lines = 1 + len(devs) + (1 if beyond else 0)    # each within a penny
                with self.subTest(fixture=key[0], window=key[1], surface=surface):
                    self.assertAlmostEqual(direct[0] + sum(v[0] for v in devs.values()) - bk,
                                           main_k, places=2)
                    self.assertLessEqual(abs(direct[1] + sum(v[1] for v in devs.values()) - bc - main_c),
                                         PENNY * lines,
                                         "the lines miss the main meter by more than their rounding")

    def test_the_over_fixture_has_a_beyond(self):
        """The fixture does what it is for: devices out-read the main in the billing period."""
        self.assertIsNotNone(self.answers[("e7_over", "billing period from 3 Oct")]["Bill card"][2])


if __name__ == "__main__":
    unittest.main()
