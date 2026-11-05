"""
BL-102: Economy 7 priced (5 Nov 2026).

A two-rate tariff (`E-2R-…`) answers `standard-unit-rates` with HTTP 400 ("This tariff has day and night
rates, not standard"), and EMT tried day and night rates only when standard came back empty, so an
Economy 7 agreement had no rate schedule. Octopus's day and night rates carry no times of day. No
Octopus or Kraken API gives them (the meter point's `ssc`, a register's `timePatternRegime`, map to
times only through Elexon's Market Domain Data); Octopus publishes a rule for a smart meter — night
00:30-07:30 UTC all year — and BottlecapDave's integration applies it. Every account EMT reads
half-hourly from Octopus has a smart meter, so EMT applies that one rule: `build_rate_schedule` fetches
day and night rates for an `E-2R` code (or on that 400) and places them with the IOG synthesis, the
window and zone passed in. `check_e7_window` compares the rule with what Octopus charged and only says
so.

Discriminating: every test fails on the parent (no Economy 7 path, no window arguments, no check) but
the GUARDs, marked.
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import engine                                       # noqa: E402
import kraken_rates as kr                           # noqa: E402
from kraken_api_client import KrakenAPIError        # noqa: E402
from tests.test_device_sensors_storage import _store    # noqa: E402

DAY_P, NIGHT_P = 30.0, 12.0
DAY = [{"value_inc_vat": DAY_P, "value_exc_vat": DAY_P / 1.05,
        "valid_from": "2025-01-01T00:00:00Z", "valid_to": None}]
NIGHT = [{"value_inc_vat": NIGHT_P, "value_exc_vat": NIGHT_P / 1.05,
          "valid_from": "2025-01-01T00:00:00Z", "valid_to": None}]
E7 = "E-2R-VAR-22-11-01-A"


def _band(r):
    return {DAY_P: "day", NIGHT_P: "night"}.get(round(r, 6) if r is not None else None)


def _e7_sched(now=datetime(2026, 11, 6)):
    return kr.RateSchedule.from_api_records(kr._synthesize_iog_tou_windowed(
        DAY, NIGHT, now=now, tz_name=kr._E7_TZ_NAME,
        night_start=kr._E7_NIGHT_START, night_end=kr._E7_NIGHT_END))


class TheWindow(unittest.TestCase):

    def test_winter_night_is_0030_to_0730(self):
        s = _e7_sched()
        for t, band in (("2026-01-10T00:00:00", "day"), ("2026-01-10T00:30:00", "night"),
                        ("2026-01-10T07:00:00", "night"), ("2026-01-10T07:30:00", "day"),
                        ("2026-01-10T23:30:00", "day")):
            self.assertEqual(_band(s.resolve(t)), band, t)

    def test_in_summer_it_stays_utc(self):
        # 00:30-07:30 UTC is 01:30-08:30 BST: the UTC half-hours are the same as winter's
        s = _e7_sched()
        self.assertEqual(_band(s.resolve("2026-07-10T00:00:00")), "day")      # 01:00 BST
        self.assertEqual(_band(s.resolve("2026-07-10T00:30:00")), "night")    # 01:30 BST
        self.assertEqual(_band(s.resolve("2026-07-10T07:00:00")), "night")    # 08:00 BST
        self.assertEqual(_band(s.resolve("2026-07-10T07:30:00")), "day")      # 08:30 BST

    def test_GUARD_intelligent_keeps_its_window(self):
        s = kr.RateSchedule.from_api_records(kr._synthesize_iog_tou_windowed(
            DAY, NIGHT, now=datetime(2026, 1, 12), tz_name=kr._IOG_TZ_NAME))
        self.assertEqual(_band(s.resolve("2026-01-10T23:30:00")), "night")
        self.assertEqual(_band(s.resolve("2026-01-10T05:30:00")), "day")

    def test_the_code_says_two_rate(self):
        self.assertTrue(kr._is_two_rate(E7))
        self.assertTrue(kr._is_two_rate("E-2R-AVRO-VAR-21-09-26-A"))
        for code in ("E-1R-AGILE-24-10-01-A", "E-1R-GO-FIX-12M-26-10-30-A", "E-1R-INTELLI-VAR-22-10-14-A", None):
            self.assertFalse(kr._is_two_rate(code), code)


class Client:
    """Octopus's REST, as it answers a two-rate tariff: standard is a 400."""

    def __init__(self):
        self.asked = []

    async def get_unit_rates(self, product, tariff, rate_type="standard-unit-rates", **kw):
        self.asked.append(rate_type)
        if rate_type == "standard-unit-rates":
            raise KrakenAPIError("HTTP 400 for …: This tariff has day and night rates, not standard.",
                                 status=400)
        return DAY if rate_type == "day-unit-rates" else NIGHT


def _build(code):
    c = Client()
    s = asyncio.run(kr.build_rate_schedule(c, "P", code, vat=kr.NO_VAT, raise_on_error=True))
    return s, c.asked


class TheFetch(unittest.TestCase):

    def test_an_e7_code_asks_day_and_night_not_standard(self):
        s, asked = _build(E7)
        self.assertEqual(asked, ["day-unit-rates", "night-unit-rates"])
        self.assertEqual(_band(s.resolve("2026-11-05T03:00:00")), "night")
        self.assertEqual(_band(s.resolve("2026-11-05T12:00:00")), "day")

    def test_the_400_turns_any_code_to_day_and_night(self):
        s, asked = _build("E-1R-SOMETHING-ODD-A")
        self.assertEqual(asked, ["standard-unit-rates", "day-unit-rates", "night-unit-rates"])
        self.assertEqual(_band(s.resolve("2026-11-05T03:00:00")), "night")

    def test_a_closed_e7_agreement_is_priced_and_cached(self):
        kr._AGREEMENT_SCHED_CACHE.clear()
        c = Client()
        ag = {"tariff_code": E7, "valid_from": "2025-03-31T23:00:00Z", "valid_to": "2025-07-25T23:00:00Z"}
        try:
            segs = asyncio.run(kr.agreement_schedules(c, [ag], kind="rate", product_of=lambda tc: "P"))
            asyncio.run(kr.agreement_schedules(c, [ag], kind="rate", product_of=lambda tc: "P"))
        finally:
            kr._AGREEMENT_SCHED_CACHE.clear()
        self.assertEqual(len(segs), 1)
        self.assertEqual(_band(segs[0][2].resolve("2025-05-01T02:00:00")), "night")
        self.assertEqual(c.asked, ["day-unit-rates", "night-unit-rates"])      # once: cached


class TheCheck(unittest.TestCase):
    """The rule against what Octopus charged: said, nothing changed."""

    def setUp(self):
        self.st = _store()

    def _day(self, night_from, night_to, day="2026-01-10"):
        # a day of imported half-hours, 1 kWh each, charged night between night_from and night_to (UTC)
        with self.st._conn:
            for i in range(48):
                hm = "%02d:%02d" % (i // 2, 30 * (i % 2))
                p = NIGHT_P if night_from <= hm < night_to else DAY_P
                self.st._conn.execute(
                    "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
                    "imp_cost, source) VALUES (?, ?, 'electricity_main', ?, 1.0, ?, 'imported_api')",
                    ("%sT%s:00" % (day, hm), "%sT%s:00" % (day, hm),
                     self.st.get_current_config_period_id(), p / 100.0))

    def _check(self):
        with mock.patch.object(engine, "_agreement_windows",
                               lambda ch: [(E7, "2026-01-01T00:00:00", "2026-02-01T00:00:00")]):
            return engine.check_e7_window(self.st, _e7_sched())

    def test_a_meter_on_the_rule_agrees(self):
        self._day("00:30", "07:30")
        with self.assertLogs("engine", level="INFO") as cm:
            res = self._check()
        self.assertEqual((res["checked"], res["disagree"]), (48, 0))
        self.assertTrue(any("matches Octopus's costs" in m for m in cm.output))

    def test_a_meter_an_hour_late_is_said_at_the_edges(self):
        self._day("01:30", "08:30")
        before = self.st._conn.execute("SELECT SUM(imp_cost) FROM blocks").fetchone()[0]
        with self.assertLogs("engine", level="WARNING") as cm:
            res = self._check()
        self.assertEqual(res["disagree"], 4)
        self.assertEqual(set(res["at"]), {"00:30", "01:00", "07:30", "08:00"})
        self.assertTrue(any("may switch at other times" in m for m in cm.output))
        self.assertEqual(self.st._conn.execute("SELECT SUM(imp_cost) FROM blocks").fetchone()[0], before)

    def test_GUARD_no_two_rate_agreement_nothing_to_check(self):
        with mock.patch.object(engine, "_agreement_windows",
                               lambda ch: [("E-1R-AGILE-24-10-01-A", "2026-01-01T00:00:00", None)]):
            self.assertIsNone(engine.check_e7_window(self.st, _e7_sched()))

    def test_the_import_runs_it(self):
        src = open(engine.__file__, encoding="utf-8").read()
        i = src.index("async def run_api_import_job(")
        self.assertIn('check_e7_window(_store, _kraken_rate_schedules.get("import"))',
                      src[i:src.index("\nasync def ", i + 1)])


if __name__ == "__main__":
    unittest.main()
