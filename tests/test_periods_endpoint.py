"""
`/api/periods` — C §3a's period-selection endpoint, and the first thing that
actually USES `billing.py` rather than housing it.

`/api/energy` answers "what happened between these two instants". It cannot
answer "which instants are worth asking about" — that needs config-period
boundaries, calendar arithmetic and the earliest block. Folding it in would give
an endpoint that sometimes takes a range and sometimes returns the ranges
available.

TWO THINGS THIS DELIBERATELY DOES NOT DO, each pinned below:

  * **No aggregates.** `api_insights_periods`, which this replaces, runs one SQL
    query PER PERIOD to attach a carbon summary — thirty-six queries on a
    three-year account, to populate a picker. Totals are `/api/energy` with
    `resolution=period`, for the one period the user selects.
  * **No full block scan.** The calendar kinds need two timestamps. They used to
    be handed every block and sort it to find them; they now come from
    `get_block_bounds()`, one query.
"""
import json
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import billing                                    # noqa: E402


class CalendarPeriodArithmetic(unittest.TestCase):
    """`calendar_periods` is pure — no blocks, no store, no timezone lookup."""

    def _p(self, kind, first, last):
        from datetime import datetime
        return billing.calendar_periods(kind, datetime(*first), datetime(*last))

    def test_months_span_the_range(self):
        out = self._p("month", (2026, 1, 15), (2026, 3, 2))
        self.assertEqual([(a.month, b.month) for a, b in out], [(1, 2), (2, 3), (3, 4)])

    def test_quarters_start_on_the_quarter(self):
        out = self._p("quarter", (2026, 5, 20), (2026, 5, 21))
        self.assertEqual(out[0][0].month, 4, "Q2 starts in April, not May")

    def test_years_roll_over(self):
        out = self._p("year", (2025, 6, 1), (2026, 2, 1))
        self.assertEqual([a.year for a, _ in out], [2025, 2026])

    def test_december_rolls_the_year(self):
        """The month arithmetic crosses a year boundary. Each of the three
        wrappers used to carry its own ad-hoc `if nm > 12` for this."""
        out = self._p("month", (2026, 12, 3), (2027, 1, 4))
        self.assertEqual([(a.year, a.month) for a, _ in out], [(2026, 12), (2027, 1)])

    def test_quarter_rolls_the_year(self):
        out = self._p("quarter", (2026, 11, 1), (2027, 2, 1))
        self.assertEqual([(a.year, a.month) for a, _ in out], [(2026, 10), (2027, 1)])

    def test_an_unknown_kind_is_refused(self):
        from datetime import datetime
        with self.assertRaises(ValueError):
            billing.calendar_periods("fortnight", datetime(2026, 1, 1), datetime(2026, 2, 1))

    def test_no_bounds_is_empty_not_an_error(self):
        self.assertEqual(billing.calendar_periods("month", None, None), [])


class TheWrappersStillWork(unittest.TestCase):
    """The three block-taking functions keep their signature — their callers pass
    blocks and nothing was asked to change. Only the work moved."""

    _BLOCKS = [{"start": "2026-01-15T00:00:00"}, {"start": "2026-03-02T00:00:00"}]

    def test_calmonth(self):
        out = billing.get_all_calmonth_periods(self._BLOCKS)
        self.assertEqual([(a.month, b.month) for a, b in out], [(1, 2), (2, 3), (3, 4)])

    def test_quarter_and_year(self):
        self.assertTrue(billing.get_all_quarter_periods(self._BLOCKS))
        self.assertTrue(billing.get_all_year_periods(self._BLOCKS))

    def test_empty_input_is_empty(self):
        for fn in (billing.get_all_calmonth_periods, billing.get_all_quarter_periods,
                   billing.get_all_year_periods):
            self.assertEqual(fn([]), [])
            self.assertEqual(fn(None), [])

    def test_blocks_without_a_start_are_ignored(self):
        self.assertEqual(billing.get_all_calmonth_periods([{}, {"start": None}]), [])

    def test_order_does_not_matter(self):
        """It used to sort the whole list; it now takes min and max. Same answer
        either way, and that equivalence is why the change is safe."""
        fwd = billing.get_all_calmonth_periods(self._BLOCKS)
        rev = billing.get_all_calmonth_periods(list(reversed(self._BLOCKS)))
        self.assertEqual(fwd, rev)


class BlockBounds(unittest.TestCase):

    def _store(self):
        import tempfile
        from block_store import BlockStore
        st = BlockStore(tempfile.mktemp(suffix=".db"))
        st._conn.execute("PRAGMA foreign_keys=OFF")
        self.addCleanup(st.close)
        return st

    def _blk(self, st, start, carbon=None):
        st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, config_period_id, meter_id, "
            "imp_kwh, carbon_g) VALUES (?,?,1,'electricity_main',1.0,?)",
            (start, start, carbon))
        st._conn.commit()

    def test_empty_is_all_none(self):
        b = self._store().get_block_bounds()
        self.assertEqual(set(b.values()), {None})

    def test_energy_and_carbon_ranges_differ(self):
        """The reason there are two pairs: carbon only exists from v2.3.0, so an
        account with imported history has energy going back years and carbon
        going back months. Gating a comparison on the carbon range would wrongly
        disable it over backfilled months."""
        st = self._store()
        self._blk(st, "2024-01-01T00:00:00")                # energy only
        self._blk(st, "2026-06-01T00:00:00", carbon=120.0)  # energy + carbon
        b = st.get_block_bounds()
        self.assertEqual(b["earliest"], "2024-01-01T00:00:00")
        self.assertEqual(b["carbon_earliest"], "2026-06-01T00:00:00")
        self.assertEqual(b["latest"], b["carbon_latest"])

    def test_it_is_meter_scoped(self):
        st = self._store()
        self._blk(st, "2026-01-01T00:00:00")
        self.assertIsNone(st.get_block_bounds(meter_id="sub_meter_ev")["earliest"])


class TheEndpoint(unittest.TestCase):

    def _client(self):
        from tests.test_server import make_client          # noqa: F401
        return make_client()

    def test_it_returns_periods_and_bounds(self):
        r = self._client().get("/api/periods?kind=month")
        self.assertEqual(r.status_code, 200)
        d = json.loads(r.data)
        self.assertEqual(d["kind"], "month")
        self.assertIn("periods", d)
        for k in ("earliest", "latest", "carbon_earliest", "carbon_latest"):
            self.assertIn(k, d["bounds"])

    def test_every_period_carries_local_and_utc(self):
        """Boundaries are LOCAL (a billing day is a wall-clock thing); the API
        speaks UTC. Both, explicitly, so no consumer has to guess which it got."""
        d = json.loads(self._client().get("/api/periods?kind=month").data)
        for p in d["periods"]:
            for k in ("start", "end", "start_utc", "end_utc", "is_current"):
                self.assertIn(k, p)

    def test_billing_is_the_default_kind(self):
        self.assertEqual(json.loads(self._client().get("/api/periods").data)["kind"], "billing")

    def test_an_unknown_kind_is_a_400(self):
        self.assertEqual(self._client().get("/api/periods?kind=fortnight").status_code, 400)

    def test_it_returns_no_aggregates(self):
        """The point of replacing `insights/periods`: a picker does not need
        totals, and computing them per period is a query per row."""
        d = json.loads(self._client().get("/api/periods?kind=month").data)
        for p in d["periods"]:
            for banned in ("carbon_g_net", "has_carbon", "imp_kwh", "imp_cost"):
                self.assertNotIn(banned, p, "an aggregate leaked into the picker")

    def test_all_four_kinds_answer(self):
        for kind in ("billing", "month", "quarter", "year"):
            r = self._client().get("/api/periods?kind=%s" % kind)
            self.assertEqual(r.status_code, 200, kind)


class TheOldRoutesStillExist(unittest.TestCase):
    """Additive, deliberately. The current templates call these and the client is
    replaced at step 6; removing them now would break a running install."""

    def test_both_remain_alongside_the_new_one(self):
        import ast
        tree = ast.parse(open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read())
        paths = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "route" and n.args:
                try:
                    paths.add(ast.literal_eval(n.args[0]))
                except Exception:
                    pass
        for p in ("/api/insights/periods", "/api/insights/data-bounds", "/api/periods"):
            self.assertIn(p, paths)


if __name__ == "__main__":
    unittest.main()
