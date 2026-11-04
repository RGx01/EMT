"""
Flow design §5 (4 Nov 2026): the rate brackets carry carbon, and the car of an account with no devices.

Flow draws grid → rate band → house and devices from the rate brackets (`usage_view._rate_brackets`,
BL-93). They gave each series kWh and £ per 2.5p bracket. Two things Flow needs were not there:

- **carbon:** a bracket is `[kWh, £, g]` now. A main slice and the export take their half-hour's
  intensity, as the day rows split its stored net carbon (`_import_intensity`); a device takes its
  stored carbon scaled to its grid share, as the day rows'.
- **the car of an account with no devices:** Octopus's EV split was in the house. It is a series of
  its own, `ev_dispatch`, by the Devices view's own rule half-hour by half-hour (`_ev_slot_share`, the
  rule `_dispatch_ev_split_by_bucket` now calls too): the stored EV slices at their rates where the bill
  has spoken or the live split wrote them, else a dispatch's share at the half-hour's blended rate.

What Flow will rest on, asserted against the Devices view's day rows (`day_stats`): a device's brackets
sum to its kWh, £ and carbon there; the car's to its EV there; the meter's to the main's import.

Every test fails on the parent, whose brackets were [kWh, £] with no `ev_dispatch`. TheHybridEV (4 Nov
2026, later) fails on the tree before it, whose brackets gave an EV meter its own reading. Those marked GUARD
fail there only for that: what they check held there too.
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import usage_view as uv                         # noqa: E402
from block_store import BlockStore              # noqa: E402

TZ = "Europe/London"
ALL = ("2000-01-01T00:00:00", "2100-01-01T00:00:00")
META = {"timezone": TZ, "currency_symbol": "£", "billing_day": 1, "block_minutes": 30, "site": "Test"}


def _store(devices=("heat_pump",)):
    st = BlockStore(os.path.join(tempfile.mkdtemp(), "blocks.db"))
    c = st._conn
    c.execute("PRAGMA foreign_keys=OFF")
    c.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, timezone) "
              "VALUES (1, '2026-01-01T00:00:00', 1, 30, ?)", (TZ,))
    c.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter) VALUES (1, 'electricity_main', 0)")
    for d in devices:
        c.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter) VALUES (1, ?, 1)", (d,))
    return st


def _cfg(devices=("heat_pump",)):
    m = {"electricity_main": {"meta": dict(META), "channels": {}}}
    for d in devices:
        m[d] = {"meta": {"sub_meter": True, "device": d.replace("_", " ").title(),
                         "meter_type": d}, "channels": {}}
    return {"meters": m}


def _block(st, bs, mid, kwh, rate, *, grid=None, carbon=None, exp=None):
    be = (datetime.fromisoformat(bs) + timedelta(minutes=30)).isoformat()
    ek, er = exp or (None, None)
    st._conn.execute(
        "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, is_provisional, "
        "interpolated, imp_kwh, imp_kwh_grid, imp_rate, imp_cost, exp_kwh, exp_rate, exp_cost, carbon_g) "
        "VALUES (?, ?, ?, 1, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?)",
        (bs, be, mid, kwh, grid, rate, round(kwh * rate, 6), ek, er,
         None if ek is None else round(ek * er, 6), carbon))


def _main(st, bs, slices, *, carbon=None, exp=None):
    """The main's block and its import slices [(kWh, rate, attribution)]."""
    k = sum(s[0] for s in slices)
    c = sum(s[0] * s[1] for s in slices)
    _block(st, bs, "electricity_main", k, c / k, carbon=carbon, exp=exp)
    st._conn.execute("UPDATE blocks SET imp_cost = ? WHERE block_start = ? AND meter_id = 'electricity_main'",
                     (c, bs))
    for seq, (sk, r, att) in enumerate(slices):
        st._conn.execute(
            "INSERT INTO block_segments (block_start, meter_id, channel, seq, kwh, inc_rate, exc_rate, "
            "band, attribution) VALUES (?, 'electricity_main', 'import', ?, ?, ?, NULL, 'standard', ?)",
            (bs, seq, sk, r, att))


def _totals(rows):
    out = {}
    for row in rows:
        for s, nb in row["b"].items():
            for n, v in nb.items():
                a = out.setdefault(s, {}).setdefault(int(n), [0.0, 0.0, 0.0])
                for i in range(3):
                    a[i] += v[i]
    return out


def _sum(t, series, i):
    return sum(v[i] for v in (t.get(series) or {}).values())


class TheCarbon(unittest.TestCase):

    def setUp(self):
        self.st = _store()
        self.addCleanup(self.st.close)

    def test_a_slice_takes_its_half_hours_intensity(self):
        # 1.5 kWh at 200 g/kWh: the 7p slice 200 g, the 28p slice 100 g
        _main(self.st, "2026-09-10T01:00:00", [(1.0, 0.07, "house"), (0.5, 0.28, "house")], carbon=300.0)
        t = _totals(uv.slot_brackets(self.st, *ALL)["rows"])
        self.assertAlmostEqual(t["meter"][2][2], 200.0)
        self.assertAlmostEqual(t["meter"][11][2], 100.0)

    def test_export_takes_the_same_intensity(self):
        # stored carbon is net: (1 − 3 kWh) × 150 g/kWh = −300 g
        _main(self.st, "2026-09-10T12:00:00", [(1.0, 0.28, "house")], carbon=-300.0, exp=(3.0, 0.15))
        t = _totals(uv.slot_brackets(self.st, *ALL)["rows"])
        self.assertAlmostEqual(t["meter"][11][2], 150.0)
        self.assertAlmostEqual(t["export"][6][2], 450.0)

    def test_a_device_its_grid_share_of_its_carbon(self):
        # 1.5 kWh read, 0.3 kWh from the grid: 300 g × 0.3 / 1.5 = 60 g; the house the rest
        _main(self.st, "2026-09-10T13:00:00", [(0.5, 0.28, "house")], carbon=100.0)
        _block(self.st, "2026-09-10T13:00:00", "heat_pump", 1.5, 0.28, grid=0.3, carbon=300.0)
        t = _totals(uv.slot_brackets(self.st, *ALL)["rows"])
        self.assertAlmostEqual(t["heat_pump"][11][2], 60.0)
        self.assertAlmostEqual(t["electricity_main"][11][2], 40.0)

    def test_GUARD_no_carbon_is_none_of_it(self):
        _main(self.st, "2026-09-10T14:00:00", [(1.0, 0.28, "house")])
        t = _totals(uv.slot_brackets(self.st, *ALL)["rows"])
        self.assertEqual(t["meter"][11][2], 0.0)

    def test_it_sums_to_the_devices_view(self):
        days = [("2026-09-10T01:00:00", 0.07, 1.2, 0.4, 240.0, 80.0),
                ("2026-09-10T12:00:00", 0.2875, 0.9, 0.5, 180.0, 100.0),
                ("2026-09-11T03:00:00", 0.07, 2.0, 0.1, 300.0, 15.0)]
        for bs, r, main_k, hp_k, main_g, hp_g in days:
            _main(self.st, bs, [(main_k, r, "house")], carbon=main_g)
            _block(self.st, bs, "heat_pump", hp_k, r, carbon=hp_g)
        t = _totals(uv.day_brackets(self.st, TZ, *ALL)["rows"])
        rows = uv.day_stats(self.st, _cfg(), TZ, *ALL)["rows"]
        hp = sum(r["meters"]["heat_pump"]["carbon_g"] for r in rows)
        main_imp = sum(r["meters"]["electricity_main"]["carbon_g_imp"] for r in rows)
        house = sum(r["meters"]["electricity_main"]["carbon_g"] for r in rows)
        self.assertAlmostEqual(_sum(t, "heat_pump", 2), hp, places=2)
        self.assertAlmostEqual(_sum(t, "meter", 2), main_imp, places=2)
        self.assertAlmostEqual(_sum(t, "electricity_main", 2), house, places=2)


class TheCar(unittest.TestCase):
    """An account with no devices: Octopus's EV, out of the house."""

    def setUp(self):
        self.st = _store(devices=())
        self.addCleanup(self.st.close)

    def _bill(self, bs, ev_kwh):
        self.st._conn.execute(
            "INSERT INTO measured_cost (slot_start, mpan, direction, kwh, home_kwh, ev_kwh) "
            "VALUES (?, '2600000000001', 'CONSUMPTION', 1.5, 0.5, ?)", (bs, ev_kwh))

    def _dispatch(self, bs, kwh):
        self.st._conn.execute("INSERT INTO dispatch_history (slot_start, kind, energy_kwh, first_seen, last_seen) "
                              "VALUES (?, 'completed', ?, ?, ?)", (bs, -kwh, bs, bs))

    def test_where_the_bill_has_spoken_its_slices(self):
        bs = "2026-09-10T01:00:00"
        _main(self.st, bs, [(1.0, 0.07, "ev"), (0.5, 0.28, "house")], carbon=300.0)
        self._bill(bs, 1.0)
        t = _totals(uv.slot_brackets(self.st, *ALL)["rows"])
        self.assertEqual(sorted(t[uv.EV_SERIES]), [2])
        self.assertAlmostEqual(t[uv.EV_SERIES][2][0], 1.0)
        self.assertAlmostEqual(t[uv.EV_SERIES][2][1], 0.07)
        self.assertAlmostEqual(t[uv.EV_SERIES][2][2], 200.0)
        self.assertEqual(sorted(t["electricity_main"]), [11], "the car is out of the house")

    def test_where_it_is_silent_a_dispatchs_share_at_the_blended_rate(self):
        # no stored split, a completed dispatch of 0.6 kWh: 0.6 of 1.5 kWh at 0.105/1.5 = 7p
        bs = "2026-09-10T02:00:00"
        _main(self.st, bs, [(1.5, 0.07, "house")])
        self._dispatch(bs, 0.6)
        t = _totals(uv.slot_brackets(self.st, *ALL)["rows"])
        self.assertAlmostEqual(t[uv.EV_SERIES][2][0], 0.6)
        self.assertAlmostEqual(t[uv.EV_SERIES][2][1], 0.042)
        self.assertAlmostEqual(t["electricity_main"][2][0], 0.9)

    def test_a_bill_with_no_car_is_no_car(self):
        bs = "2026-09-10T03:00:00"
        _main(self.st, bs, [(1.5, 0.07, "house")])
        self._bill(bs, 0.0)
        self._dispatch(bs, 0.6)
        t = _totals(uv.slot_brackets(self.st, *ALL)["rows"])
        self.assertNotIn(uv.EV_SERIES, t)

    def test_GUARD_with_a_device_the_car_is_the_devices(self):
        st = _store(devices=("ev_charger",))
        self.addCleanup(st.close)
        _main(st, "2026-09-10T01:00:00", [(1.0, 0.07, "ev"), (0.5, 0.28, "house")])
        _block(st, "2026-09-10T01:00:00", "ev_charger", 1.0, 0.07)
        t = _totals(uv.slot_brackets(st, *ALL)["rows"])
        self.assertNotIn(uv.EV_SERIES, t)

    def test_it_sums_to_the_devices_views_ev(self):
        _main(self.st, "2026-09-10T01:00:00", [(1.0, 0.07, "ev"), (0.5, 0.28, "house")])
        self._bill("2026-09-10T01:00:00", 1.0)
        _main(self.st, "2026-09-10T02:00:00", [(1.5, 0.07, "house")])
        self._dispatch("2026-09-10T02:00:00", 0.6)
        _main(self.st, "2026-09-10T13:00:00", [(0.8, 0.28, "house")])
        t = _totals(uv.day_brackets(self.st, TZ, *ALL)["rows"])
        rows = uv.day_stats(self.st, _cfg(devices=()), TZ, *ALL)["rows"]
        ev = [r["meters"]["ev_dispatch"] for r in rows if "ev_dispatch" in r["meters"]]
        self.assertAlmostEqual(_sum(t, uv.EV_SERIES, 0), sum(e["imp_kwh"] for e in ev), places=3)
        self.assertAlmostEqual(_sum(t, uv.EV_SERIES, 1), sum(e["imp_cost"] for e in ev), places=3)
        house = sum(r["meters"]["electricity_main"]["imp_kwh"] for r in rows)
        self.assertAlmostEqual(_sum(t, "electricity_main", 0), house, places=3)

    def test_the_reply_names_the_series(self):
        self.assertEqual(uv.day_brackets(self.st, TZ, *ALL)["ev_series"], "ev_dispatch")


class TheHybridEV(unittest.TestCase):
    """An account WITH an EV meter (4 Nov 2026, staging): the Devices view shows the hybrid EV —
    Octopus's split where a half-hour has one, else the charger's own reading — and moves the
    difference to or from the house (`_hybrid_ev_slot_folds`). The brackets gave the car the
    charger's reading, ~0.04% off over staging's solar era, the house off the other way. Now its
    brackets are the fold's, at the EV slices' rates.

    The EV meter sits before another device: the first build found it with a comprehension that kept
    only the last sub-meter (house_battery on staging), and changed nothing."""

    DEVICES = ("ev_charger", "house_battery")

    def setUp(self):
        self.st = _store(devices=self.DEVICES)
        self.st._conn.execute("UPDATE meters SET meter_type = 'ev' WHERE meter_id = 'ev_charger'")
        self.st._conn.execute("UPDATE meters SET meter_type = 'battery' WHERE meter_id = 'house_battery'")
        self.addCleanup(self.st.close)
        # Octopus split this half-hour: the car 1.0 kWh at 7p; the charger read 1.3
        _main(self.st, "2026-09-10T01:00:00", [(1.0, 0.07, "ev"), (0.5, 0.28, "house")])
        _block(self.st, "2026-09-10T01:00:00", "ev_charger", 1.3, 0.07)
        # no split: the charger's reading stands
        _main(self.st, "2026-09-10T13:00:00", [(1.5, 0.28, "house")])
        _block(self.st, "2026-09-10T13:00:00", "ev_charger", 0.4, 0.28)
        _block(self.st, "2026-09-10T13:00:00", "house_battery", 0.2, 0.28)

    def test_the_split_half_hour_is_octopuss(self):
        t = _totals(uv.slot_brackets(self.st, *ALL)["rows"])
        self.assertAlmostEqual(t["ev_charger"][2][0], 1.0)
        self.assertAlmostEqual(t["ev_charger"][2][1], 0.07)
        self.assertAlmostEqual(t["ev_charger"][11][0], 0.4)            # the unsplit one, as read
        self.assertAlmostEqual(t["electricity_main"][11][0], 0.5 + 1.5 - 0.4 - 0.2)

    def test_it_sums_to_the_devices_view(self):
        t = _totals(uv.day_brackets(self.st, TZ, *ALL)["rows"])
        rows = uv.day_stats(self.st, _cfg(devices=self.DEVICES), TZ, *ALL)["rows"]
        for s in ("ev_charger", "electricity_main", "house_battery"):
            with self.subTest(series=s):
                self.assertAlmostEqual(_sum(t, s, 0), sum(r["meters"][s]["imp_kwh"] for r in rows), places=3)
                self.assertAlmostEqual(_sum(t, s, 1), sum(r["meters"][s]["imp_cost"] for r in rows), places=3)


class ARetiredDevice(unittest.TestCase):

    def test_GUARD_keeps_its_brackets_under_its_name(self):
        # retiring sets retired_at; the meter stays in the active period, which the brackets read
        st = _store()
        self.addCleanup(st.close)
        _main(st, "2026-09-10T01:00:00", [(1.0, 0.07, "house")])
        _block(st, "2026-09-10T01:00:00", "heat_pump", 0.4, 0.07)
        st.retire_meter("heat_pump", "2026-09-10")
        _main(st, "2026-09-12T01:00:00", [(1.0, 0.07, "house")])
        t = _totals(uv.day_brackets(st, TZ, *ALL)["rows"])
        self.assertAlmostEqual(t["heat_pump"][2][0], 0.4)


if __name__ == "__main__":
    unittest.main()
