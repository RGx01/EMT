"""
A day's standing charge on that local day's blocks (31 Oct 2026, ROADMAP BL-92; E design §4a).

The staging copy's 31 Mar 2024, a spring Sunday imported from a CSV, held that day's standing charge
and the next day's added together, and both local days read the total; its autumn Sunday held none.
v4's and v5's imports filed the charge by the UTC date, and in summer a local day starts at 23:00 UTC
the day before. The API import's version: a summer price change read a day late.

What they pin (v5's writers):

- **The CSV import** adds up the standing column by the block's LOCAL day (the config period's
  timezone) and writes it on every block of that day — across a spring and an autumn Sunday, and for
  a file that gives the charge once a day as for one that spreads it.
- **The API import** files its days by the local date; a clock-change day's fallback counts its 46
  or 50 half-hours.

The importer's transform for v4 history (3e) has its own tests (test_v4import_standing).

Discriminating: every test fails on the parent, except the GUARD (a winter day, which UTC and local
agree on).
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from block_store import BlockStore                                       # noqa: E402


def _local(utc):
    return datetime.fromisoformat(utc).replace(tzinfo=timezone.utc).astimezone(LONDON).date().isoformat()

HEADER = "Start,End,Consumption (kWh),Unit Rate (p/kWh),Estimated Cost Inc. Tax (p),Standing Charge Inc. Tax (p)\n"


def _csv(days, spread=False):
    """Every half-hour of each local day in `days` [(YYYY-MM-DD, standing_p)], local-offset
    timestamps as Octopus's export gives them. The standing charge once, on the day's first row —
    or, `spread`, a share on every row."""
    out = HEADER
    for day, sc_p in days:
        y, m, d = map(int, day.split("-"))
        a = datetime(y, m, d, tzinfo=LONDON).astimezone(timezone.utc)
        nd = datetime(y, m, d) + timedelta(days=1)
        b = datetime(nd.year, nd.month, nd.day, tzinfo=LONDON).astimezone(timezone.utc)
        n = int((b - a).total_seconds() // 1800)
        for i in range(n):
            s = (a + timedelta(minutes=30 * i)).astimezone(LONDON)
            e = (a + timedelta(minutes=30 * (i + 1))).astimezone(LONDON)
            sc = (sc_p / n) if spread else (sc_p if i == 0 else "")
            out += "%s,%s,0.5,20,,%s\n" % (s.isoformat(), e.isoformat(), sc)
    return out


class TheCsvImport(unittest.TestCase):

    def setUp(self):
        import types
        eio = types.ModuleType("energy_engine_io"); eio.load_json = lambda *a, **kw: {}
        sys.modules.setdefault("energy_engine_io", eio)
        self.store = BlockStore(":memory:")
        self.store.insert_config_period({"meters": {"electricity_main": {"meta": {
            "billing_day": 1, "block_minutes": 30, "timezone": "Europe/London",
            "currency_symbol": "£", "currency_code": "GBP", "site": "Home"}}}})

    def _by_local_day(self):
        out = {}
        for r in self.store._conn.execute("SELECT block_start, standing_charge FROM blocks "
                                          "WHERE meter_id = 'electricity_main'"):
            out.setdefault(_local(r["block_start"]), set()).add(
                round(r["standing_charge"], 6))
        return out

    def test_the_spring_sunday_holds_its_own_charge(self):
        self.store.apply_csv_import({"import": _csv([("2024-03-30", 48.667), ("2024-03-31", 48.667),
                                                     ("2024-04-01", 53.939), ("2024-04-02", 53.939)])})
        self.assertEqual(self._by_local_day(), {"2024-03-30": {0.48667}, "2024-03-31": {0.48667},
                                                "2024-04-01": {0.53939}, "2024-04-02": {0.53939}})

    def test_the_autumn_sunday_is_not_left_at_nothing(self):
        self.store.apply_csv_import({"import": _csv([("2023-10-28", 48.667), ("2023-10-29", 48.667),
                                                     ("2023-10-30", 48.667)])})
        self.assertEqual(self._by_local_day(), {"2023-10-28": {0.48667}, "2023-10-29": {0.48667},
                                                "2023-10-30": {0.48667}})

    def test_a_summer_price_change_lands_on_its_day(self):
        self.store.apply_csv_import({"import": _csv([("2024-09-30", 53.939), ("2024-10-01", 54.847)])})
        self.assertEqual(self._by_local_day(), {"2024-09-30": {0.53939}, "2024-10-01": {0.54847}})

    def test_a_file_that_spreads_the_charge_gives_the_day(self):
        self.store.apply_csv_import({"import": _csv([("2024-03-31", 48.667), ("2024-04-01", 53.939)],
                                                    spread=True)})
        self.assertEqual(self._by_local_day(), {"2024-03-31": {0.48667}, "2024-04-01": {0.53939}})

    def test_a_winter_day(self):
        """GUARD: in winter the UTC date and the local day agree."""
        self.store.apply_csv_import({"import": _csv([("2024-01-15", 48.667)])})
        self.assertEqual(self._by_local_day(), {"2024-01-15": {0.48667}})


class TheApiImport(unittest.TestCase):

    def setUp(self):
        self.src = open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read()

    def test_its_days_are_local(self):
        self.assertIn("d = _sc_day(st)", self.src)
        self.assertIn('_standing = day_standing.get(_sc_day(st)) if channel == "import" else None', self.src)
        self.assertNotIn("_standing = day_standing.get(st[:10])", self.src)

    def test_a_clock_change_days_fallback_counts_its_half_hours(self):
        self.assertIn("day_standing[d] = (round(meas_standing[d] / n * _slots_in(d), 6)", self.src)

    def test_the_local_date(self):
        from block_store import utc_to_local_date
        for utc, local in (("2024-03-31T23:00:00", "2024-04-01"), ("2024-03-31T00:30:00", "2024-03-31"),
                           ("2023-10-28T23:00:00", "2023-10-29"), ("2023-10-29T23:00:00", "2023-10-29"),
                           ("2024-01-15T23:30:00", "2024-01-15")):
            self.assertEqual(utc_to_local_date(utc, "Europe/London"), local, utc)


if __name__ == "__main__":
    unittest.main()
