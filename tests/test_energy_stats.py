"""
/api/energy?include=stats — the Usage Stats and Spiral rows (usage_view.py), C step 1, 1 Oct 2026.

`/api/charts/blocks-summary` (a row per local day, all history) and `/api/charts/blocks-day`
(a row per half-hour of one day) built their rows in web/server.py. The code moved to
usage_view.py unchanged; `/api/energy` serves the same rows at `resolution=day` and `block`,
with two new windows (`kind=all`, `kind=day&date=`), and the routes call it until the Charts
page moves (part 4). On real databases (dev: no devices, the dispatch EV split; production:
EV charger + battery, the hybrid fold) the two routes are byte-identical to before the move and
`/api/energy`'s day rows equal blocks-summary's on every day (1,212 and 1,214).

What changes on purpose: a half-hour slot is keyed by its UTC instant, not its local HH:MM,
so a clock-change day has its 46 or 50 slots. /blocks-day summed the autumn change's two
01:xx hours into one bar and drew spring's missing hour as two empty ones.

New-facet tests: they fail on the parent tree, where `include=stats`, `kind=day|all` and
`fingerprint.config` do not exist.
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
CFG = {"meters": {
    "electricity_main": {"meta": {"timezone": TZ, "currency_symbol": "£", "billing_day": 3,
                                  "block_minutes": 30}, "channels": {}},
    "house_battery": {"meta": {"sub_meter": True, "device": "Battery", "meter_type": "battery"},
                      "channels": {}}}}


def _store(start, hours):
    """`hours` of half-hours from naive-UTC `start`: main 1.0 kWh @ 0.25, a battery
    drawing 0.2 kWh of it from the grid, and a standing charge on every block."""
    st = BlockStore(tempfile.mktemp(suffix=".db"))
    st._conn.execute("PRAGMA foreign_keys=OFF")
    st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, "
                     "timezone) VALUES (1, '2025-01-01T00:00:00', 3, 30, ?)", (TZ,))
    st._conn.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter) "
                     "VALUES (1, 'electricity_main', 0)")
    st._conn.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter, parent_meter_id) "
                     "VALUES (1, 'house_battery', 1, 'electricity_main')")
    t = datetime.fromisoformat(start)
    for i in range(hours * 2):
        bs, be = t.isoformat(), (t + timedelta(minutes=30)).isoformat()
        for mid, kwh, grid, rem in (("electricity_main", 1.0, None, 0.8),
                                    ("house_battery", 0.2, 0.2, None)):
            st._conn.execute(
                "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
                "imp_kwh_grid, imp_kwh_remainder, imp_rate, imp_cost, standing_charge, "
                "is_provisional, interpolated) VALUES (?,?,?,1,?,?,?,0.25,?,0.5,0,0)",
                (bs, be, mid, kwh, grid, rem, kwh * 0.25))
        t += timedelta(minutes=30)
    st._conn.commit()
    return st


class TheSlotsAreInstants(unittest.TestCase):
    """A clock-change day has the half-hours it has."""

    def test_autumn_change_has_50_slots_and_no_merged_hour(self):
        # 26 Oct 2025: local midnight is 23:00Z on the 25th; the day is 25 hours long.
        st = _store("2025-10-25T23:00:00", 25)
        out = uv.slot_stats(st, CFG, TZ, "2025-10-25T23:00:00", "2025-10-27T00:00:00")
        self.assertEqual(len(out["rows"]), 50)
        self.assertEqual([r["label"] for r in out["rows"]].count("01:00"), 2)
        self.assertTrue(all(abs(r["imp_kwh"] - 1.0) < 1e-9 for r in out["rows"]))

    def test_spring_change_has_46_slots_and_no_empty_hour(self):
        # 29 Mar 2026: local midnight is 00:00Z; 01:00 local does not exist.
        st = _store("2026-03-29T00:00:00", 23)
        out = uv.slot_stats(st, CFG, TZ, "2026-03-29T00:00:00", "2026-03-29T23:00:00")
        self.assertEqual(len(out["rows"]), 46)
        self.assertNotIn("01:00", [r["label"] for r in out["rows"]])
        self.assertTrue(all(r["imp_kwh"] > 0 for r in out["rows"]))

    def test_an_empty_slot_is_zero_filled(self):
        st = _store("2026-08-19T23:00:00", 1)
        out = uv.slot_stats(st, CFG, TZ, "2026-08-19T23:00:00", "2026-08-20T23:00:00")
        self.assertEqual(len(out["rows"]), 48)
        self.assertEqual(out["rows"][5]["imp_kwh"], 0)


class TheDayRowsAreTheSlots(unittest.TestCase):
    """One aggregation, two buckets: a day's row is its slots summed."""

    def test_a_day_is_its_slots(self):
        st = _store("2025-10-25T23:00:00", 25)
        day = uv.day_stats(st, CFG, TZ, "2025-10-25T23:00:00", "2025-10-27T00:00:00")["rows"]
        slots = uv.slot_stats(st, CFG, TZ, "2025-10-25T23:00:00", "2025-10-27T00:00:00")["rows"]
        self.assertEqual(len(day), 1)
        self.assertEqual((day[0]["year"], day[0]["month"], day[0]["day"]), (2025, 10, 26))
        self.assertAlmostEqual(day[0]["imp_kwh"], sum(r["imp_kwh"] for r in slots), places=4)
        self.assertAlmostEqual(day[0]["standing"], 0.5, places=4)          # once per day
        self.assertAlmostEqual(sum(r["standing"] for r in slots), 0.5, places=4)

    def test_devices_at_grid_kwh_and_the_house_remainder(self):
        """GUARD (moved code): Direct is the house's share (direct_import's rule on the grid
        shares, since #105; it was the stored remainder), the battery its grid draw."""
        st = _store("2026-08-19T23:00:00", 24)
        row = uv.day_stats(st, CFG, TZ, "2026-08-19T23:00:00", "2026-08-20T23:00:00")["rows"][0]
        self.assertAlmostEqual(row["meters"]["electricity_main"]["imp_kwh"], 48 * 0.8, places=3)
        self.assertAlmostEqual(row["meters"]["house_battery"]["imp_kwh"], 48 * 0.2, places=3)


def _server():
    spec = importlib.util.spec_from_file_location("server_stats", os.path.join(_ROOT, "web", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv


class TheRoute(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv = _server()
        cls.st = _store("2025-10-25T23:00:00", 25)

    def _get(self, q):
        with mock.patch.object(self.srv.store_access, "store", lambda: self.st), \
             mock.patch.object(self.srv, "load_config", lambda: CFG), \
             mock.patch.object(self.srv, "_config_timezone", lambda: TZ):
            return self.srv.app.test_client().get("/api/energy?" + q)

    def test_day_and_all(self):
        j = self._get("resolution=day&kind=all&include=stats").get_json()
        self.assertEqual(j["period"]["kind"], "all")
        self.assertEqual([(r["year"], r["month"], r["day"]) for r in j["stats"]["rows"]], [(2025, 10, 26)])
        self.assertEqual(j["stats"]["meters"][0]["id"], "electricity_main")

    def test_block_and_one_day(self):
        j = self._get("resolution=block&kind=day&date=2025-10-26&include=stats").get_json()
        self.assertEqual(j["range"], {"start_utc": "2025-10-25T23:00:00", "end_utc": "2025-10-27T00:00:00"})
        self.assertEqual(len(j["stats"]["rows"]), 50)

    def test_the_fingerprint_sees_the_config(self):
        a = self._get("resolution=day&kind=all").get_json()["fingerprint"]["config"]
        self.st._conn.execute("UPDATE config_periods SET billing_day = 15")
        b = self._get("resolution=day&kind=all").get_json()["fingerprint"]["config"]
        self.st._conn.execute("UPDATE config_periods SET billing_day = 3")
        self.assertNotEqual(a, b)

    def test_what_is_refused(self):
        self.assertEqual(self._get("resolution=period&include=stats").status_code, 400)
        self.assertEqual(self._get("resolution=block&kind=all").status_code, 400)
        self.assertEqual(self._get("resolution=day&kind=day&date=26-10-2025").status_code, 400)


if __name__ == "__main__":
    unittest.main()


class TheLocalDayLookupIsExact(unittest.TestCase):
    """usage_view._local_day_of — a binary search on each local day's UTC start, in place
    of a datetime conversion per block (perf, 2 Oct 2026: −25-28% on all-history day_stats,
    byte-identical rows on two real databases). Exact for any offset, which memoising by UTC
    hour would not be."""

    def _check(self, tz_name, start, end):
        from datetime import datetime as _d, timedelta as _t, timezone as _u
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
        day = uv._local_day_of(start, end, tz)
        t, hi = _d.fromisoformat(start), _d.fromisoformat(end)
        while t < hi:
            want = t.replace(tzinfo=_u.utc).astimezone(tz).strftime("%Y-%m-%d")
            self.assertEqual(day(t.isoformat()), want, (tz_name, t))
            t += _t(minutes=15)

    def test_london_across_both_clock_changes(self):
        self._check("Europe/London", "2025-10-24T23:00:00", "2025-10-28T00:00:00")
        self._check("Europe/London", "2026-03-27T00:00:00", "2026-03-31T23:00:00")

    def test_a_45_minute_offset(self):
        """Nepal is UTC+5:45 — a UTC hour holds two local dates."""
        self._check("Asia/Kathmandu", "2026-05-01T18:15:00", "2026-05-04T18:15:00")
