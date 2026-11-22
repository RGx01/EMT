"""
The Overview's device lines summed in SQLite, finished steps kept (22 Nov 2026, the user: "can the recent changes we've
made impact browser performance?" — no, but the server's: `/api/overview/days` summed every power sample of two days
in Python once a minute per open tab, 0.44 s for three devices' 50 hours on a Mac).

- `BlockStore.get_sample_energy_steps` / `get_sample_level_steps` sum and pick in SQLite, per 5-minute step; the line
  rules (`overview_days.energy_lines`) are pinned by test_overview_days, whose expectations are the per-sample sum's.
- `server._ov_day_steps` keeps each day's finished steps and fetches only from the first unfinished one; a finished
  day whole. Here: what it returns minute after minute is what a full fetch returns, and a kept day is not fetched.

New functions: every test fails on the parent tree.
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "web"))

from block_store import BlockStore      # noqa: E402
import overview_days as od              # noqa: E402

LON = ZoneInfo("Europe/London")
Y0, T0, T1 = od.day_bounds(datetime(2026, 11, 22, 12, 0, tzinfo=LON))


def _u(t):
    return t.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


def _server():
    spec = importlib.util.spec_from_file_location("server_daysteps", os.path.join(_ROOT, "web", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv


def _store():
    """Today from midnight to 03:00 every 10 s: charging 3 kW to 01:00, idle, a 7-minute gap at 01:30, out at
    -2 kW from 02:00 with a sign change mid-way; an SoC on every sample, rising."""
    st = BlockStore(os.path.join(tempfile.mkdtemp(), "blocks.db"))
    rows, t, soc = [], T0, 40.0
    while t < T0 + timedelta(hours=3):
        m = (t - T0).total_seconds() / 60
        if 90 <= m < 97:
            t += timedelta(seconds=10)
            continue
        kw = 3.0 if m < 60 else (0.0 if m < 120 else (-2.0 if m < 150 else 1.0))
        soc += 0.01
        rows.append((_u(t), "dev", round(soc, 1), kw))
        t += timedelta(seconds=10)
    st._conn.executemany("INSERT INTO sub_meter_history (captured_at, meter_id, soc_pct, inverter_kw) "
                         "VALUES (?, ?, ?, ?)", rows)
    st._conn.commit()
    return st


class KeptStepsAreTheFullFetch(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv = _server()
        cls.st = _store()

    def setUp(self):
        self.srv._OV_DAY_CACHE.clear()

    def test_energy_minute_after_minute(self):
        keep = (_u(Y0), _u(T0))
        for minute in range(3, 185, 1):            # across every step boundary, the gap and the sign change
            now = T0 + timedelta(minutes=minute, seconds=7)
            full = self.st.get_sample_energy_steps("dev", _u(T0), _u(T1), _u(now))
            kept = self.srv._ov_day_steps(("energy", "dev", _u(T0)), "energy", now, T0, T1,
                                          lambda f: self.st.get_sample_energy_steps("dev", _u(T0), _u(T1), _u(now),
                                                                                    from_m=f), keep)
            self.assertEqual(od.energy_lines(kept), od.energy_lines(full), "at minute %d" % minute)

    def test_level_minute_after_minute(self):
        keep = (_u(Y0), _u(T0))
        for minute in range(3, 185, 1):
            now = T0 + timedelta(minutes=minute, seconds=7)
            full = self.st.get_sample_level_steps("dev", "soc_pct", _u(T0), _u(T1), _u(now))
            kept = self.srv._ov_day_steps(("soc_pct", "dev", _u(T0)), "level", now, T0, T1,
                                          lambda f: self.st.get_sample_level_steps("dev", "soc_pct", _u(T0), _u(T1),
                                                                                   _u(now), from_m=f), keep)
            self.assertEqual(kept, full, "at minute %d" % minute)

    def test_only_the_unfinished_steps_are_fetched(self):
        keep = (_u(Y0), _u(T0))
        asked = []

        def fetch(f, now):
            asked.append(f)
            return self.st.get_sample_energy_steps("dev", _u(T0), _u(T1), _u(now), from_m=f)
        for minute in (60, 61):
            now = T0 + timedelta(minutes=minute, seconds=7)
            self.srv._ov_day_steps(("energy", "dev", _u(T0)), "energy", now, T0, T1, lambda f: fetch(f, now), keep)
        self.assertEqual(asked, [0, 55])            # the second minute from the first unfinished step on

    def test_a_finished_day_is_kept_whole_until_the_database_changes(self):
        keep = (_u(T0), _u(T1))
        n = []

        def fetch(f):
            n.append(f)
            return self.st.get_sample_level_steps("dev", "soc_pct", _u(T0), _u(T1), _u(T1))
        later = T1 + timedelta(minutes=30)
        for _ in range(3):
            self.srv._ov_day_steps(("soc_pct", "dev", _u(T0)), "level", later, T0, T1, fetch, keep)
        self.assertEqual(n, [0])
        import engine
        with mock.patch.object(engine, "_db_generation", getattr(engine, "_db_generation", 0) + 1, create=True):
            self.srv._ov_day_steps(("soc_pct", "dev", _u(T0)), "level", later, T0, T1, fetch, keep)
        self.assertEqual(n, [0, 0])                 # a restore: fetched again


if __name__ == "__main__":
    unittest.main()
