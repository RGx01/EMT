"""
overview_days — the Overview's today-against-yesterday lines (overview rows design, part 2, 22 Nov 2026).

Pure: points [minute since that day's local midnight, value]. Live Power from the main meter's half-hours plus the
half-hour in progress; a device from its power sensor's samples (trapezoid, gaps over 5 minutes add nothing,
positive in, negative out); a battery's SoC.

New module: every test fails on the parent tree (no module).
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import overview_days as od     # noqa: E402
from block_store import BlockStore  # noqa: E402

LON = ZoneInfo("Europe/London")
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=LON)          # GMT: local = UTC
Y0, T0, T1 = od.day_bounds(NOW)


def _iso(t):
    return t.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


def _samples(start, minutes_, kw, step_s=30, soc=None):
    """A sample every `step_s` from `start` for `minutes_`, at `kw` (and a constant SoC)."""
    out, t = [], start
    while t <= start + timedelta(minutes=minutes_):
        out.append({"captured_at": _iso(t), "inverter_kw": kw, "soc_pct": soc})
        t += timedelta(seconds=step_s)
    return out


def _store_with(samples):
    import tempfile
    st = BlockStore(os.path.join(tempfile.mkdtemp(), "blocks.db"))
    st._conn.executemany(
        "INSERT INTO sub_meter_history (captured_at, meter_id, soc_pct, inverter_kw) VALUES (?, 'dev', ?, ?)",
        [(x["captured_at"], x.get("soc_pct"), x.get("inverter_kw")) for x in samples])
    st._conn.commit()
    return st


def _device_lines(samples, out_too=True):
    st = _store_with(samples)
    return {name: od.energy_lines(st.get_sample_energy_steps("dev", _iso(a), _iso(b), _iso(NOW)), out_too=out_too)
            for name, a, b in (("yesterday", Y0, T0), ("today", T0, T1))}


class TheDays(unittest.TestCase):

    def test_midnights(self):
        self.assertEqual((Y0.isoformat(), T0.isoformat(), T1.isoformat()),
                         ("2026-11-21T00:00:00+00:00", "2026-11-22T00:00:00+00:00", "2026-11-23T00:00:00+00:00"))

    def test_the_day_the_clocks_go_back_is_25_hours(self):
        y0, t0, t1 = od.day_bounds(datetime(2026, 10, 25, 12, 0, tzinfo=LON))
        self.assertEqual(od.minutes(t1, t0), 1500.0)
        self.assertEqual(od.minutes(t0, y0), 1440.0)


class TheMainMeter(unittest.TestCase):

    ROWS = [{"block_start": "2026-11-21T23:30:00", "imp_kwh": 1.0, "exp_kwh": 0.0},   # yesterday's last
            {"block_start": "2026-11-22T00:00:00", "imp_kwh": 0.5, "exp_kwh": 0.1},
            {"block_start": "2026-11-22T00:30:00", "imp_kwh": 0.25, "exp_kwh": None}]

    def test_each_day_steps_up_at_each_half_hours_end(self):
        got = od.half_hour_lines(self.ROWS, Y0, T0, T1)
        self.assertEqual(got["today"]["imp"], [[0, 0.0], [30.0, 0.5], [60.0, 0.75]])
        self.assertEqual(got["today"]["exp"], [[0, 0.0], [30.0, 0.1], [60.0, 0.1]])
        self.assertEqual(got["yesterday"]["imp"], [[0, 0.0], [1440.0, 1.0]])

    def test_the_half_hour_in_progress_is_added_now(self):
        today = od.half_hour_lines(self.ROWS, Y0, T0, T1)["today"]
        od.with_partial(today, 70.0, {"imp": 0.2, "exp": None}, starts_min=60.0)
        self.assertEqual(today["imp"][-1], [70.0, 0.95])
        self.assertEqual(today["exp"][-1], [60.0, 0.1])            # no export reads: nothing added

    def test_a_half_hour_in_progress_left_from_before_a_restart_is_not_added(self):
        today = od.half_hour_lines(self.ROWS, Y0, T0, T1)["today"]
        od.with_partial(today, 400.0, {"imp": 3.0}, starts_min=120.0)
        self.assertEqual(today["imp"][-1], [60.0, 0.75])

    def test_the_kwh_so_far_from_register_reads(self):
        self.assertAlmostEqual(od.partial_kwh([{"ts": "a", "value": 100.0}, {"ts": "b", "value": 100.4}]), 0.4)
        self.assertIsNone(od.partial_kwh([{"ts": "a", "value": 100.0}]))
        self.assertIsNone(od.partial_kwh([{"ts": "a", "value": 100.0}, {"ts": "b", "value": 3.0}]))   # a reset


class ADevice(unittest.TestCase):

    def _lines(self, samples, out_too=True):
        # through a real store: the steps are summed in SQLite (get_sample_energy_steps) and drawn by
        # energy_lines — 22 Nov 2026, moved out of a per-sample sum in Python; these expectations are that sum's
        return _device_lines(samples, out_too)

    def test_an_hour_at_2kw_is_2kwh_in(self):
        got = self._lines(_samples(T0 + timedelta(hours=1), 60, 2.0))
        self.assertAlmostEqual(got["today"]["in"][-1][1], 2.0, places=3)
        self.assertEqual(got["today"]["out"], [[0, 0.0], [120.0, 0.0]])     # nothing out: level to the last sample

    def test_negative_power_is_out(self):
        got = self._lines(_samples(T0 + timedelta(hours=1), 30, -3.0))
        self.assertAlmostEqual(got["today"]["out"][-1][1], 1.5, places=3)
        self.assertEqual(got["today"]["in"][-1][1], 0.0)

    def test_a_consumer_has_no_out_line(self):
        self.assertNotIn("out", self._lines(_samples(T0, 10, 1.0), out_too=False)["today"])

    def test_a_gap_adds_nothing(self):
        s = _samples(T0 + timedelta(hours=1), 10, 6.0) + _samples(T0 + timedelta(hours=3), 10, 6.0)
        got = self._lines(s)["today"]["in"]
        self.assertAlmostEqual(got[-1][1], 2.0, places=3)                   # 2 × 10 min at 6 kW, not the 2 h between

    def test_a_line_holds_level_through_an_idle_stretch(self):
        s = (_samples(T0 + timedelta(hours=1), 60, 2.0) + _samples(T0 + timedelta(hours=2, seconds=30), 180, 0.0)
             + _samples(T0 + timedelta(hours=5, seconds=30), 60, 2.0))
        pts = self._lines(s)["today"]["in"]
        idle = [p for p in pts if 120.5 <= p[0] <= 300.5]
        # level from the end of the first hour (2 kWh, and the half-sample ramp down) to where the next starts
        self.assertEqual([p[0] for p in idle], [120.5, 300.5])
        self.assertEqual(idle[0][1], idle[1][1])
        self.assertAlmostEqual(idle[0][1], 2.0083, places=4)

    def test_one_point_per_five_minutes(self):
        pts = self._lines(_samples(T0 + timedelta(hours=1), 60, 2.0))["today"]["in"]
        # midnight, the level start at 01:00, then one per 5 minutes to 02:00 (13: 02:00 opens its own)
        self.assertEqual(len(pts), 1 + 1 + 13)

    def test_a_segment_across_midnight_is_yesterdays_and_ends_at_midnight(self):
        s = [{"captured_at": _iso(T0 - timedelta(seconds=60)), "inverter_kw": 3.6},
             {"captured_at": _iso(T0 + timedelta(seconds=60)), "inverter_kw": 3.6}]
        got = self._lines(s)
        self.assertEqual(got["yesterday"]["in"][-1], [1440.0, 0.12])
        self.assertEqual(got["today"]["in"], [[0, 0.0], [1.0, 0.0]])

    def test_nothing_after_now(self):
        got = self._lines(_samples(NOW - timedelta(minutes=10), 30, 6.0))
        self.assertAlmostEqual(got["today"]["in"][-1][1], 1.0, places=3)


class TheSoc(unittest.TestCase):

    def test_the_last_reading_in_each_step(self):
        s = [{"captured_at": _iso(T0 + timedelta(minutes=m)), "soc_pct": p, "inverter_kw": None}
             for m, p in ((1, 40.0), (3, 41.0), (6, 45.5))]
        st = _store_with(s)
        self.assertEqual(st.get_sample_level_steps("dev", "soc_pct", _iso(T0), _iso(T1), _iso(NOW)),
                         [[3.0, 41.0], [6.0, 45.5]])


if __name__ == "__main__":
    unittest.main()
