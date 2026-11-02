"""
The v4 importer's transform 3e (E design §4a; spec v31, 31 Oct 2026): an imported block's standing
charge is its local day's.

v4's imports filed the charge by the UTC date (every block of a UTC date one value), and in summer a
local day starts at 23:00 UTC the day before, so the readers — a local day's first non-zero
half-hour — took another day's. The staging copy's 31 Mar 2024 (a CSV import) held that day's charge
and the next day's; its autumn Sunday held none; its API import's summer price changes read a day late.
The user: v5 needs no repair, a re-import from v4 brings history through this.

What they pin, on v4-shaped sources written as v4 wrote them:

- **CSV, a run across both clock changes:** every local day takes its own charge — the spring Sunday
  and Monday split from the Sunday's date (a + b), the autumn Sunday from the Saturday's.
- **CSV, a run that cannot be proven** (summer only, or a file that spread the charge, whose spring
  Sunday matches no split) is reported and left as it was; **one wholly in winter** is already right.
- **API:** a summer price change lands on its day.
- **Live blocks** are not touched, and the `standing_by_day` gate holds.

Measured on two real 4.5.24 databases (E design §4a). Discriminating: every test fails on the parent
(no transform; and `standing_charge` is carried exactly).
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.test_v4import import v4_db                       # noqa: E402
import v4import                                             # noqa: E402

LONDON = ZoneInfo("Europe/London")
MAIN = "electricity_main"
A, B = 0.48667, 0.53939                                     # before and after a price change


def _slots(day):
    y, m, d = map(int, day.split("-"))
    a = datetime(y, m, d, tzinfo=LONDON).astimezone(timezone.utc)
    nd = datetime(y, m, d) + timedelta(days=1)
    b = datetime(nd.year, nd.month, nd.day, tzinfo=LONDON).astimezone(timezone.utc)
    n = int((b - a).total_seconds() // 1800)
    return [(a + timedelta(minutes=30 * i)).replace(tzinfo=None).isoformat() for i in range(n)]


def _local(utc):
    return datetime.fromisoformat(utc).replace(tzinfo=timezone.utc).astimezone(LONDON).date().isoformat()


def csv_rows(days, spread=False):
    """[(block_start, standing)] as v4's CSV import wrote them: the file gives a local day's charge
    on its first row (or `spread`, a share on every row), added up by the UTC date."""
    by_utc, starts = {}, []
    for day, sc in days:
        sl = _slots(day)
        for i, s in enumerate(sl):
            share = (sc / len(sl)) if spread else (sc if i == 0 else 0.0)
            by_utc[s[:10]] = by_utc.get(s[:10], 0.0) + share
            starts.append(s)
    return [(s, round(by_utc[s[:10]], 6)) for s in starts]


def api_rows(days):
    """As v4's API import wrote them: the schedule at noon UTC on each block's UTC date."""
    sc = dict(days)
    return [(s, sc.get(s[:10], sc[_local(s)])) for day, _ in days for s in _slots(day)]


class _Import(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run(self, rows, source):
        src, dst = os.path.join(self.dir, "v4.db"), os.path.join(self.dir, "v5.db")
        v4_db(src)
        c = sqlite3.connect(src)
        c.execute("UPDATE config_periods SET timezone = 'Europe/London'")
        for i, (s, sc) in enumerate(rows):
            c.execute("INSERT INTO blocks (id, block_start, block_end, meter_id, config_period_id, "
                      "imp_kwh, standing_charge, source) VALUES (?, ?, ?, ?, 1, 0.5, ?, ?)",
                      (1000 + i, s, s, MAIN, sc, source))
        c.commit(); c.close()
        report = v4import.import_v4(src, dst)
        got = {}
        for r in sqlite3.connect(dst).execute(
                "SELECT block_start, standing_charge FROM blocks WHERE meter_id = ? AND source = ?",
                (MAIN, source)):
            got.setdefault(_local(r[0]), set()).add(round(r[1], 6))
        return report, got


class TheCsvImport(_Import):

    def test_a_run_across_both_clock_changes_gets_every_days_own_charge(self):
        days = [("2023-10-28", A), ("2023-10-29", A), ("2023-10-30", A)] + \
               [("2023-10-%02d" % d, A) for d in range(31, 32)] + \
               [("2023-11-%02d" % d, A) for d in range(1, 31)] + \
               [("2023-12-%02d" % d, A) for d in range(1, 32)] + \
               [("2024-01-%02d" % d, A) for d in range(1, 32)] + \
               [("2024-02-%02d" % d, A) for d in range(1, 30)] + \
               [("2024-03-%02d" % d, A) for d in range(1, 32)] + \
               [("2024-04-01", B), ("2024-04-02", B)]
        report, got = self._run(csv_rows(days), "imported_csv")
        self.assertEqual(got, {d: {sc} for d, sc in days})
        t = report["transforms"]["standing_by_day"]["csv"]
        self.assertEqual((t["runs_moved"], t["runs_reported"]), (1, []))
        self.assertTrue(report["gates"]["standing_by_day"]["ok"])
        self.assertFalse(report.get("failures"))

    def test_a_summer_only_run_is_reported_and_left(self):
        rows = csv_rows([("2024-06-10", A), ("2024-06-11", B), ("2024-06-12", B)])
        report, got = self._run(rows, "imported_csv")
        self.assertEqual(report["transforms"]["standing_by_day"]["csv"]["runs_reported"][0]["why"],
                         "summer only: no clock change to prove the file's shape")
        self.assertEqual(got["2024-06-10"], {A, B})          # as v4 left it
        self.assertTrue(report["gates"]["standing_by_day"]["ok"])

    def test_a_file_that_spread_the_charge_is_not_guessed_at(self):
        rows = csv_rows([("2024-03-30", A), ("2024-03-31", A), ("2024-04-01", B), ("2024-04-02", B)],
                        spread=True)
        report, _ = self._run(rows, "imported_csv")
        why = report["transforms"]["standing_by_day"]["csv"]["runs_reported"][0]["why"]
        self.assertEqual(why, "spring 2024-03-31's date matches no split")

    def test_a_winter_run_is_already_right(self):
        days = [("2024-01-15", A), ("2024-01-16", B)]
        report, got = self._run(csv_rows(days), "imported_csv")
        self.assertEqual(report["transforms"]["standing_by_day"]["csv"]["runs_right"], 1)
        self.assertEqual(got, {"2024-01-15": {A}, "2024-01-16": {B}})


class TheApiImport(_Import):

    def test_a_summer_price_change_lands_on_its_day(self):
        days = [("2024-09-29", A), ("2024-09-30", A), ("2024-10-01", B), ("2024-10-02", B)]
        report, got = self._run(api_rows(days), "imported_api")
        self.assertEqual(got, {d: {sc} for d, sc in days})
        self.assertEqual(report["transforms"]["standing_by_day"]["api"]["blocks"], 2)
        self.assertTrue(report["gates"]["standing_by_day"]["ok"])


class TheLiveBlocks(_Import):

    def test_they_are_not_touched(self):
        report, _ = self._run(csv_rows([("2024-03-31", A), ("2024-04-01", B)]), "imported_csv")
        q = "SELECT id, standing_charge FROM blocks WHERE id IN (40, 41) ORDER BY id"
        before = sqlite3.connect(os.path.join(self.dir, "v4.db")).execute(q).fetchall()
        after = sqlite3.connect(os.path.join(self.dir, "v5.db")).execute(q).fetchall()
        self.assertEqual(after, before)
        self.assertEqual(len(after), 2)
        self.assertTrue(report["gates"]["standing_by_day"]["ok"])


if __name__ == "__main__":
    unittest.main()
