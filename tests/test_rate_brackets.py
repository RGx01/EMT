"""
BL-93 (rate brackets design, 31 Oct 2026) — grid import by the 2.5p bracket of the rate it was
charged, for the whole meter, the house and each device.

`usage_view.rate_brackets` reads the main's import slices (`block_segments`) at their own rates and
each device's grid kWh at its stored rate and £; the house is the meter less the devices in each
bracket, per half-hour, clipped at 0. `/api/energy?fields=brackets` serves it per local day or per
half-hour. The first ten failed on #364's tree, where none of it existed. Of the three added with
the export (on #366's tree), the export's fails there; the two marked GUARD pass there, pinning what
was already right.
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
META = {"timezone": TZ, "currency_symbol": "£", "billing_day": 1, "block_minutes": 30}
ALL = ("2000-01-01T00:00:00", "2100-01-01T00:00:00")


def _store():
    st = BlockStore(os.path.join(tempfile.mkdtemp(), "blocks.db"))
    c = st._conn
    c.execute("PRAGMA foreign_keys=OFF")
    c.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, timezone) "
              "VALUES (1, '2026-01-01T00:00:00', 1, 30, ?)", (TZ,))
    c.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter) VALUES (1, 'electricity_main', 0)")
    for d in ("ev_charger", "heat_pump"):
        c.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter) VALUES (1, ?, 1)", (d,))
    return st


def _block(st, bs, mid, kwh, rate, cost=None, grid=None, slices=(), exp=None):
    """A block, and for the main its import slices [(kWh, rate, attribution)]; `exp` (kWh, rate)
    its export."""
    be = (datetime.fromisoformat(bs) + timedelta(minutes=30)).isoformat()
    cost = round(kwh * rate, 6) if cost is None else cost
    ek, er = exp or (None, None)
    st._conn.execute(
        "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, is_provisional, "
        "interpolated, imp_kwh, imp_kwh_grid, imp_rate, imp_cost, exp_kwh, exp_rate, exp_cost) "
        "VALUES (?, ?, ?, 1, 0, 0, ?, ?, ?, ?, ?, ?, ?)",
        (bs, be, mid, kwh, grid, rate, cost, ek, er, None if ek is None else round(ek * er, 6)))
    for seq, (k, r, att) in enumerate(slices):
        st._conn.execute(
            "INSERT INTO block_segments (block_start, meter_id, channel, seq, kwh, inc_rate, exc_rate, "
            "band, attribution) VALUES (?, ?, 'import', ?, ?, ?, NULL, 'standard', ?)",
            (bs, mid, seq, k, r, att))


def _main(st, bs, slices, exp=None):
    k = sum(s[0] for s in slices)
    c = sum(s[0] * s[1] for s in slices)
    _block(st, bs, "electricity_main", k, c / k, cost=c, slices=slices, exp=exp)


def _totals(rows):
    out = {}
    for row in rows:
        for s, nb in row["b"].items():
            for n, (k, c) in nb.items():
                a = out.setdefault(s, {}).setdefault(int(n), [0.0, 0.0])
                a[0] += k
                a[1] += c
    return out


class TheBracket(unittest.TestCase):

    def test_an_edge_rate_lands_in_the_upper_bracket(self):
        # 57.5p is 23 × 2.5p: [57.5, 60), not [55, 57.5) — 0.575 × 100 is 57.49999999999999
        # as a float, so it needs the rounding
        self.assertEqual(uv.rate_bracket(0.575), 23)
        self.assertEqual(uv.rate_bracket(0.075), 3)
        self.assertEqual(uv.rate_bracket(0.0749), 2)
        self.assertEqual(uv.rate_bracket(0.0), 0)

    def test_a_negative_price_has_brackets_of_its_own(self):
        self.assertEqual(uv.rate_bracket(-0.001), -1)       # −2.5–0p
        self.assertEqual(uv.rate_bracket(-0.025), -1)
        self.assertEqual(uv.rate_bracket(-0.0251), -2)
        self.assertEqual(uv.rate_bracket(-0.275), -11)      # −27.500000000000004 as a float


class TheSeries(unittest.TestCase):

    def setUp(self):
        self.st = _store()
        self.addCleanup(self.st.close)

    def test_two_rates_in_one_half_hour_stay_apart(self):
        # IOG-SMB: Octopus billed the car at 7p and the house at 28p in the same half-hour
        bs = "2026-09-10T01:00:00"
        _main(self.st, bs, [(1.0, 0.07, "ev"), (0.5, 0.28, "house")])
        _block(self.st, bs, "ev_charger", 1.0, 0.07)
        t = _totals(uv.day_brackets(self.st, TZ, *ALL)["rows"])
        self.assertEqual(sorted(t["meter"]), [2, 11])
        self.assertAlmostEqual(t["meter"][2][0], 1.0)
        self.assertAlmostEqual(t["meter"][11][1], 0.14)
        self.assertEqual(sorted(t["ev_charger"]), [2])
        self.assertEqual(sorted(t["electricity_main"]), [11], "the house is the 28p slice")
        self.assertAlmostEqual(t["electricity_main"][11][0], 0.5)

    def test_brackets_sum_to_each_series_cost(self):
        # (start, the main's slices, EV kWh and rate, heat pump kWh and rate)
        rows = [("2026-09-10T01:00:00", [(1.2, 0.07, "house")], 0.4, 0.07, 0.3, 0.07),
                ("2026-09-10T12:00:00", [(0.9, 0.2875, "house")], 0.2, 0.2875, 0.5, 0.2875),
                ("2026-09-11T03:00:00", [(2.0, 0.07, "ev"), (0.3, 0.2875, "house")],
                 2.0, 0.07, 0.1, 0.2875)]
        dev = {"ev_charger": 0.0, "heat_pump": 0.0}
        meter = 0.0
        for bs, slices, ev, ev_rate, hp, hp_rate in rows:
            _main(self.st, bs, slices)
            meter += sum(k * r for k, r, _a in slices)
            _block(self.st, bs, "ev_charger", ev, ev_rate)
            dev["ev_charger"] += ev * ev_rate
            _block(self.st, bs, "heat_pump", hp, hp_rate)
            dev["heat_pump"] += hp * hp_rate
        t = _totals(uv.day_brackets(self.st, TZ, *ALL)["rows"])
        # each local day is kept at 4 dp, so two days may sit 0.0001 from the unrounded sum
        self.assertAlmostEqual(sum(v[1] for v in t["meter"].values()), meter, delta=0.0003)
        for d, c in dev.items():
            self.assertAlmostEqual(sum(v[1] for v in t[d].values()), c, delta=0.0003, msg=d)
        # no clip binds here, so house + devices is the meter, bracket by bracket
        for n, (k, c) in t["meter"].items():
            parts = sum(t[s].get(n, [0, 0])[0] for s in ("electricity_main", "ev_charger", "heat_pump"))
            self.assertAlmostEqual(parts, k, places=4)

    def test_a_device_counts_its_grid_share_not_its_reading(self):
        bs = "2026-09-10T13:00:00"
        _main(self.st, bs, [(0.5, 0.28, "house")])
        _block(self.st, bs, "heat_pump", 1.5, 0.28, cost=0.084, grid=0.3)   # 1.2 kWh from the battery
        t = _totals(uv.slot_brackets(self.st, *ALL)["rows"])
        self.assertAlmostEqual(t["heat_pump"][11][0], 0.3)
        self.assertAlmostEqual(t["electricity_main"][11][0], 0.2)

    def test_the_house_clip_binds_only_on_a_split_half_hour(self):
        # Split: the car's one rate (7p) on more kWh than the meter's 7p slice
        _main(self.st, "2026-09-10T01:00:00", [(1.0, 0.07, "ev"), (0.5, 0.28, "house")])
        _block(self.st, "2026-09-10T01:00:00", "ev_charger", 1.2, 0.07)
        # One rate: house = meter − device, no clip
        _main(self.st, "2026-09-10T01:30:00", [(1.5, 0.07, "house")])
        _block(self.st, "2026-09-10T01:30:00", "ev_charger", 1.2, 0.07)
        rows = {r["slot"]: _totals([r]) for r in uv.slot_brackets(self.st, *ALL)["rows"]}
        split = rows["2026-09-10T01:00:00"]["electricity_main"]
        self.assertNotIn(2, split, "clipped: no negative house in the 7p bracket")
        self.assertAlmostEqual(split[11][0], 0.5)
        one = rows["2026-09-10T01:30:00"]["electricity_main"]
        self.assertAlmostEqual(one[2][0], 0.3)
        self.assertAlmostEqual(one[2][1], 0.021)

    def test_a_plunge_keeps_its_sign(self):
        # GUARD (passes on #365's tree too): nothing in the read clipped a negative price; this
        # pins it, as staging has never been on Agile. Agile at −3.2p: the meter, the house and a device all earn a credit, below 0p
        bs = "2026-09-10T13:00:00"
        _main(self.st, bs, [(2.0, -0.032, "house")])
        _block(self.st, bs, "heat_pump", 0.5, -0.032)
        t = _totals(uv.slot_brackets(self.st, *ALL)["rows"])
        self.assertEqual(sorted(t["meter"]), [-2])                    # −5 to −2.5p
        self.assertAlmostEqual(t["meter"][-2][1], -0.064)
        self.assertAlmostEqual(t["heat_pump"][-2][1], -0.016)
        self.assertAlmostEqual(t["electricity_main"][-2][0], 1.5)
        self.assertAlmostEqual(t["electricity_main"][-2][1], -0.048, msg="the house keeps its credit")

    def test_a_zero_rate_is_not_a_missing_one(self):
        # GUARD: a device priced at exactly 0p with no £ lands in bracket 0. Its rate used to be read
        # as missing (`if r`), and £ ÷ kWh gave the same 0; now it is read as a rate
        bs = "2026-09-10T13:30:00"
        _main(self.st, bs, [(1.0, 0.0, "house")])
        _block(self.st, bs, "heat_pump", 0.4, 0.0, cost=0.0)
        t = _totals(uv.slot_brackets(self.st, *ALL)["rows"])
        self.assertEqual(sorted(t["heat_pump"]), [0])

    def test_export_at_its_own_rate(self):
        _main(self.st, "2026-09-10T12:00:00", [(0.2, 0.28, "house")], exp=(1.5, 0.15))
        _main(self.st, "2026-09-10T12:30:00", [(0.2, 0.28, "house")], exp=(0.8, 0.041))   # Agile Outgoing
        _main(self.st, "2026-09-10T13:00:00", [(0.2, 0.28, "house")])                     # none
        r = uv.day_brackets(self.st, TZ, *ALL)
        self.assertEqual(r["export_series"], "export")
        t = _totals(r["rows"])
        self.assertEqual(sorted(t["export"]), [1, 6])
        self.assertAlmostEqual(t["export"][6][0], 1.5)
        self.assertAlmostEqual(t["export"][6][1], 0.225)
        self.assertAlmostEqual(t["export"][1][1], 0.0328)
        self.assertNotIn("export", t["meter"], "export is a series of its own, not the meter's")

    def test_a_half_hour_goes_to_its_local_day(self):
        # 23:30 UTC on 9 Sep is 00:30 BST on 10 Sep
        _main(self.st, "2026-09-09T23:30:00", [(1.0, 0.07, "house")])
        days = [r["day"] for r in uv.day_brackets(self.st, TZ, *ALL)["rows"]]
        self.assertEqual(days, ["2026-09-10"])


def _server():
    spec = importlib.util.spec_from_file_location("server_rb", os.path.join(_ROOT, "web", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv


class TheField(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv = _server()
        cls.st = _store()
        _main(cls.st, "2026-09-10T01:00:00", [(1.0, 0.07, "ev"), (0.5, 0.28, "house")])
        _block(cls.st, "2026-09-10T01:00:00", "ev_charger", 1.0, 0.07)
        cls.st._conn.commit()
        cls.cfg = {"meters": {"electricity_main": {"meta": dict(META), "channels": {}},
                              "ev_charger": {"meta": {"sub_meter": True, "device": "EV",
                                                      "meter_type": "ev_charger"}, "channels": {}}}}

    @classmethod
    def tearDownClass(cls):
        cls.st.close()

    def _get(self, q):
        with mock.patch.object(self.srv.store_access, "store", lambda: self.st), \
             mock.patch.object(self.srv, "load_config", lambda: self.cfg), \
             mock.patch.object(self.srv, "_config_timezone", lambda: TZ):
            return self.srv.app.test_client().get("/api/energy?" + q)

    def test_by_day(self):
        j = self._get("resolution=day&kind=all&fields=brackets").get_json()
        self.assertEqual(sorted(j), ["brackets", "period", "range", "resolution", "timezone"])
        b = j["brackets"]
        self.assertEqual(b["width_p"], 2.5)
        self.assertEqual([r["day"] for r in b["rows"]], ["2026-09-10"])
        self.assertEqual(sorted(b["rows"][0]["b"]["meter"]), ["11", "2"])

    def test_by_half_hour(self):
        j = self._get("resolution=block&kind=day&date=2026-09-10&fields=brackets").get_json()
        self.assertEqual([r["slot"] for r in j["brackets"]["rows"]], ["2026-09-10T01:00:00"])

    def test_not_at_period(self):
        self.assertEqual(self._get("resolution=period&kind=month&fields=brackets").status_code, 400)


if __name__ == "__main__":
    unittest.main()
