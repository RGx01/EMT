"""
G2a (tariff prices design, 31 Oct 2026): each agreement's prices as dated price periods, for the Usage
Stats tariff overlay only — no pricing path reads them.

What they pin:

- **The summary** (`tariff_prices.summarise`): a standing charge or flat rate one period per price;
  Go's and Intelligent's two windowed records a day to two bands, `off_peak` the cheaper, a period
  per price; a record clipped at the agreement's edge folded into its window; a clock change not a
  new window; Tracker (a rate a day) a period a day; Agile — the AGILE family, or more than 8
  windows in a day — `dynamic`, one row with no value.
- **The table** (`replace_tariff_prices`): replaced only when the summary differs.
- **Where it is kept**: each schedule `agreement_schedules` fetches (not a cached one) and the
  refresh's current ones, ex-VAT as published, clipped to the agreement.

Discriminating: every test fails on the parent (the module, table and hook are new).
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

LONDON = ZoneInfo("Europe/London")
GO = "E-1R-GO-VAR-22-10-14-A"


def _utc(day, hhmm):
    y, m, d = map(int, day.split("-"))
    h, mi = map(int, hhmm.split(":"))
    return datetime(y, m, d, h, mi, tzinfo=LONDON).astimezone(timezone.utc).replace(tzinfo=None).isoformat()


def _days(first, n):
    d0 = datetime.fromisoformat(first)
    return [(d0 + timedelta(days=i)).date().isoformat() for i in range(n)]


def go_records(first, n, night, day, change_on=None, night2=None, day2=None):
    """Octopus's Go records: 05:30-23:30 at `day`, 23:30-05:30 at `night`, every local day."""
    out = []
    for d in _days(first, n):
        nd = (datetime.fromisoformat(d) + timedelta(days=1)).date().isoformat()
        late = change_on and d >= change_on
        out.append((_utc(d, "05:30"), _utc(d, "23:30"), day2 if late else day))
        out.append((_utc(d, "23:30"), _utc(nd, "05:30"), night2 if late else night))
    return out


class TheSummary(unittest.TestCase):

    def setUp(self):
        import tariff_prices
        self.tp = tariff_prices

    def test_a_standing_charge_is_a_period_per_price(self):
        rows = self.tp.summarise([("2024-01-01T00:00:00", "2024-04-01T00:00:00", 46.35),
                                  ("2024-04-01T00:00:00", "2024-07-01T00:00:00", 46.35),
                                  ("2024-07-01T00:00:00", None, 51.37)],
                                 kind="standing", tariff_code=GO)
        self.assertEqual([(r["valid_from"], r["valid_to"], r["value_exc"]) for r in rows],
                         [("2024-01-01T00:00:00", "2024-07-01T00:00:00", 46.35),
                          ("2024-07-01T00:00:00", None, 51.37)])

    def test_go_is_two_bands_a_period_per_price(self):
        rows = self.tp.summarise(go_records("2024-01-10", 10, 8.0, 28.0, "2024-01-15", 9.0, 29.0),
                                 kind="unit", tariff_code=GO)
        got = [(r["band"], r["window"], r["value_exc"]) for r in rows]
        self.assertEqual(sorted(got), sorted([("off_peak", "23:30-05:30", 8.0), ("off_peak", "23:30-05:30", 9.0),
                                              ("peak", "05:30-23:30", 28.0), ("peak", "05:30-23:30", 29.0)]))
        self.assertEqual(len(rows), 4)                  # of 20 records

    def test_a_record_clipped_at_the_agreements_start_is_its_window(self):
        recs = go_records("2024-01-10", 5, 8.0, 28.0)
        recs.insert(0, (_utc("2024-01-10", "00:00"), _utc("2024-01-10", "05:30"), 8.0))   # the clipped night
        rows = self.tp.summarise(recs, kind="unit", tariff_code=GO)
        self.assertEqual(sorted({r["window"] for r in rows}), ["05:30-23:30", "23:30-05:30"])
        self.assertEqual({r["band"] for r in rows}, {"off_peak", "peak"})

    def test_a_night_split_at_a_midnight_price_change_is_still_the_night(self):
        """31 Oct 2026, the Pi: Octopus splits the night where the price changes at local midnight
        (23:30-00:00 old, 00:00-05:30 new); with two changes the pieces recur, and the hover showed
        00:00-05:30 as a band of its own. Each piece goes to the 23:30-05:30 night it is part of."""
        def split_night(day, old, new):
            nd = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
            return [(_utc(day, "23:30"), _utc(nd, "00:00"), old), (_utc(nd, "00:00"), _utc(nd, "05:30"), new)]
        recs = [r for r in go_records("2025-07-01", 120, 7.0, 27.0) if r[0] not in
                (_utc("2025-08-31", "23:30"), _utc("2025-09-30", "23:30"))]
        recs += split_night("2025-08-31", 7.0, 7.5) + split_night("2025-09-30", 7.5, 8.0)
        rows = self.tp.summarise(recs, kind="unit", tariff_code=GO)
        self.assertEqual(sorted({r["window"] for r in rows}), ["05:30-23:30", "23:30-05:30"])
        self.assertEqual({r["band"] for r in rows}, {"off_peak", "peak"})

    def test_a_clock_change_is_not_a_new_window(self):
        rows = self.tp.summarise(go_records("2024-03-28", 7, 8.0, 28.0), kind="unit", tariff_code=GO)
        self.assertEqual(len(rows), 2)

    def test_tracker_is_a_period_a_day(self):
        recs = [(d + "T00:00:00", (datetime.fromisoformat(d) + timedelta(days=1)).isoformat(), 20.0 + i)
                for i, d in enumerate(_days("2024-01-10", 5))]
        rows = self.tp.summarise(recs, kind="unit", tariff_code="E-1R-SILVER-FLEX-22-11-25-A")
        self.assertEqual(len(rows), 5)
        self.assertNotIn("dynamic", {r["band"] for r in rows})

    def test_agile_is_dynamic(self):
        recs = [((datetime(2024, 1, 10) + timedelta(minutes=30 * i)).isoformat(),
                 (datetime(2024, 1, 10) + timedelta(minutes=30 * (i + 1))).isoformat(), 10.0 + i % 7)
                for i in range(96)]
        for code in ("E-1R-AGILE-24-10-01-A", "E-1R-AGILE-OUTGOING-19-05-13-A", "E-1R-NEWTHING-1-A"):
            rows = self.tp.summarise(recs, kind="unit", tariff_code=code)
            self.assertEqual([(r["band"], r["value_exc"]) for r in rows], [("dynamic", None)], code)

    def test_agiles_standing_charge_is_kept(self):
        rows = self.tp.summarise([("2024-01-01T00:00:00", None, 47.0)], kind="standing",
                                 tariff_code="E-1R-AGILE-24-10-01-A")
        self.assertEqual(rows[0]["value_exc"], 47.0)


def _stored(st, code):
    return [dict(r) for r in st._conn.execute(
        "SELECT agreement_from, kind, band, window, value_exc, valid_from, valid_to FROM tariff_prices "
        "WHERE tariff_code = ? ORDER BY valid_from, band", (code,))]


class TheTable(unittest.TestCase):

    def test_replaced_only_when_it_differs(self):
        from block_store import BlockStore
        st = BlockStore(":memory:")
        a = [{"band": "", "window": None, "value_exc": 46.35, "valid_from": "2024-01-01T00:00:00", "valid_to": None}]
        b = [dict(a[0], value_exc=51.37)]
        self.assertTrue(st.replace_tariff_prices(GO, "2024-01-01T00:00:00", "standing", a, "t1"))
        self.assertFalse(st.replace_tariff_prices(GO, "2024-01-01T00:00:00", "standing", a, "t2"))
        self.assertTrue(st.replace_tariff_prices(GO, "2024-01-01T00:00:00", "standing", b, "t3"))
        self.assertEqual([r["value_exc"] for r in _stored(st, GO)], [51.37])


class WhereItIsKept(unittest.TestCase):

    def setUp(self):
        import engine
        from block_store import BlockStore
        from kraken_rates import RateSchedule
        self.eng, self.RS = engine, RateSchedule
        engine._store = self.st = BlockStore(":memory:")

    def test_ex_vat_clipped_to_the_agreement(self):
        inc = [("2023-01-01T00:00:00", "2024-04-01T00:00:00", 24.3), ("2024-04-01T00:00:00", None, 25.2)]
        exc = [(a, b, round(v / 1.05, 4)) for a, b, v in inc]
        self.eng._keep_tariff_prices(GO, "2024-02-01T00:00:00Z", "standing", self.RS(inc, exc))
        rows = _stored(self.st, GO)
        self.assertEqual([(r["valid_from"], r["value_exc"]) for r in rows],
                         [("2024-02-01T00:00:00", round(24.3 / 1.05, 4)),
                          ("2024-04-01T00:00:00", round(25.2 / 1.05, 4))])
        self.assertEqual(rows[0]["agreement_from"], "2024-02-01T00:00:00")

    def test_no_ex_vat_series_is_not_kept(self):
        self.eng._keep_tariff_prices(GO, "2024-02-01T00:00:00", "rate",
                                     self.RS([("2024-02-01T00:00:00", None, 24.3)]))
        self.assertEqual(_stored(self.st, GO), [])

    def test_each_fetched_schedule_is_told_and_a_cached_one_is_not(self):
        import kraken_rates as kr
        built = []

        async def fake_build(client, pc, tc, *, vat, period_from=None, period_to=None):
            return kr.RateSchedule([(period_from, period_to, 20.0)], [(period_from, period_to, 19.0)])
        ags = [{"tariff_code": GO, "valid_from": "2023-01-01T00:00:00Z", "valid_to": "2023-06-01T00:00:00Z"}]
        orig = kr.build_rate_schedule
        kr.build_rate_schedule = fake_build
        kr._AGREEMENT_SCHED_CACHE.clear()
        try:
            for _ in range(2):
                asyncio.run(kr.agreement_schedules(object(), ags, product_of=lambda c: "GO-22",
                                                   on_built=lambda *a: built.append(a[0])))
        finally:
            kr.build_rate_schedule = orig
            kr._AGREEMENT_SCHED_CACHE.clear()
        self.assertEqual(built, [GO])                 # the second call is the cache's

    def test_the_refresh_keeps_the_current_ones(self):
        src = open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read()
        self.assertIn('_keep_current_tariff_prices(ch, tariff, "rate", sched)', src)
        self.assertIn('_keep_current_tariff_prices("import", imp["tariff_code"], "standing", sc)', src)
        self.assertIn("on_built=_keep_tariff_prices)", src)


if __name__ == "__main__":
    unittest.main()
