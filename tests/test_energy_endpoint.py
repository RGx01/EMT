"""
`/api/energy` — C §3's consolidated read surface, `resolution=period` half.

Replaces six routes — `/api/{usage,insights}/{billing-period,calendar-month,
calendar-year}` — whose only differences are WHICH period and WHAT to aggregate.
Two independent axes, previously six endpoints, at 77–83% textual duplication
between them.

THREE PROPERTIES, each pinned here because losing any of them re-creates what
this replaces:

  * `include=` rather than separate endpoints, because both aggregators scan the
    same blocks over the same range. One request, one scan.
  * `resolution=block|day` return series (blocks / local-day rollups), never a
    period rollup — a caller that asks for series and silently receives totals has
    no way to tell. (This said they returned 501; they have not since they were
    implemented. Corrected 30 Sep 2026.)
  * period resolution is ONE implementation for all four kinds, so a fix to the
    billing-period lookup cannot reach three routes and miss three.
"""
import json
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def _client():
    from tests.test_server import make_client
    return make_client()


def _get(qs=""):
    r = _client().get("/api/energy" + qs)
    return r.status_code, json.loads(r.data)


class ResolutionGate(unittest.TestCase):

    def test_an_unknown_resolution_is_a_400(self):
        self.assertEqual(_get("?resolution=fortnight")[0], 400)

    def test_period_is_the_default(self):
        code, d = _get()
        self.assertEqual(code, 200)
        self.assertEqual(d["resolution"], "period")

    def test_all_three_resolutions_answer(self):
        for res in ("period", "day", "block"):
            code, d = _get("?resolution=%s&kind=month" % res)
            self.assertEqual(code, 200, "%s -> %s" % (res, d))
            self.assertEqual(d["resolution"], res)


class SeriesResolutions(unittest.TestCase):

    def test_day_returns_rollups_and_no_blocks(self):
        code, d = _get("?resolution=day&kind=month")
        self.assertIn("days", d)
        self.assertNotIn("blocks", d, "day resolution must not carry per-block rows")

    def test_block_returns_blocks_and_segments(self):
        code, d = _get("?resolution=block&kind=month")
        self.assertIn("blocks", d)
        self.assertIn("segments", d)
        self.assertNotIn("days", d)

    def test_an_aggregate_is_not_forced_onto_a_series_request(self):
        """`include` defaults to `usage` for a period request — that IS the
        answer — and to nothing for day/block, where the rows are the answer and
        an aggregate is an extra scan nobody asked for."""
        code, d = _get("?resolution=block&kind=month")
        self.assertNotIn("usage", d)
        self.assertNotIn("carbon", d)

    def test_but_it_can_still_be_asked_for(self):
        code, d = _get("?resolution=day&kind=month&include=usage")
        self.assertIn("days", d)
        self.assertIn("usage", d)

    def test_every_series_response_carries_a_fingerprint(self):
        for res in ("day", "block"):
            code, d = _get("?resolution=%s&kind=month" % res)
            self.assertIn("fingerprint", d)
            self.assertIn("epoch", d["fingerprint"])


class IncludeFacets(unittest.TestCase):

    def test_usage_is_the_default_facet(self):
        code, d = _get("?kind=month")
        self.assertEqual(code, 200)
        self.assertIn("usage", d)
        self.assertNotIn("carbon", d, "carbon should cost an explicit include")

    def test_both_facets_in_one_request(self):
        """The reason include= beats separate endpoints: one range, one scan."""
        code, d = _get("?kind=month&include=usage,carbon")
        self.assertEqual(code, 200)
        self.assertIn("usage", d)
        self.assertIn("carbon", d)

    def test_carbon_alone(self):
        code, d = _get("?kind=month&include=carbon")
        self.assertEqual(code, 200)
        self.assertIn("carbon", d)
        self.assertNotIn("usage", d)

    def test_an_unknown_facet_is_a_400(self):
        """Refused rather than ignored — a typo that silently drops a facet
        returns a response that looks complete and is not."""
        code, d = _get("?include=usage,carbonn")
        self.assertEqual(code, 400)
        self.assertIn("carbonn", str(d["error"]))


class PeriodResolution(unittest.TestCase):

    def test_all_four_kinds_resolve(self):
        for kind in ("billing", "month", "quarter", "year"):
            code, d = _get("?kind=%s" % kind)
            self.assertEqual(code, 200, "%s -> %s" % (kind, d))
            self.assertEqual(d["period"]["kind"], kind)

    def test_the_range_is_utc_and_the_period_is_local(self):
        """A billing day is a wall-clock thing; the API speaks UTC. Both are
        returned so no consumer has to guess which it got."""
        code, d = _get("?kind=month&year=2026&month=3")
        self.assertEqual(code, 200)
        self.assertEqual(d["period"]["period_start"], "2026-03-01")
        self.assertIn("start_utc", d["range"])
        self.assertIn("end_utc", d["range"])

    def test_month_end_is_inclusive_in_the_label(self):
        """`period_end` is the last day IN the period, matching what the six
        routes returned; `range.end_utc` is exclusive, matching how it is
        queried. Two different conventions, both deliberate, both stated."""
        code, d = _get("?kind=month&year=2026&month=3")
        self.assertEqual(d["period"]["period_end"], "2026-03-31")
        self.assertTrue(d["range"]["end_utc"].startswith("2026-03-31"),
                        d["range"]["end_utc"])

    def test_quarter_snaps_to_the_quarter(self):
        code, d = _get("?kind=quarter&year=2026&quarter=2")
        self.assertEqual(d["period"]["period_start"], "2026-04-01")
        self.assertEqual(d["period"]["period_label"], "Q2 2026")

    def test_year_is_the_whole_year(self):
        code, d = _get("?kind=year&year=2025")
        self.assertEqual(d["period"]["period_start"], "2025-01-01")
        self.assertEqual(d["period"]["period_end"], "2025-12-31")

    def test_december_rolls_the_year(self):
        code, d = _get("?kind=month&year=2026&month=12")
        self.assertEqual(d["period"]["period_end"], "2026-12-31")

    def test_an_unknown_kind_is_a_400(self):
        self.assertEqual(_get("?kind=fortnight")[0], 400)

    def test_a_missing_billing_period_is_a_404(self):
        code, d = _get("?kind=billing&period_start=1999-01-01")
        self.assertEqual(code, 404)


class TheSixAreGone(unittest.TestCase):
    """It WAS additive: the six routes stayed while the Insights page called four of them.
    C step 2 (1 Oct 2026) moved the page onto /api/energy and deleted all six — nothing
    outside the add-on called them (confirmed by the user)."""

    def test_only_the_one_endpoint_remains(self):
        import ast
        tree = ast.parse(open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read())
        paths = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "route" and n.args:
                try:
                    paths.add(ast.literal_eval(n.args[0]))
                except Exception:
                    pass
        for p in ("/api/usage/billing-period", "/api/usage/calendar-month",
                  "/api/usage/calendar-year", "/api/insights/billing-period",
                  "/api/insights/calendar-month", "/api/insights/calendar-year"):
            self.assertNotIn(p, paths)
        self.assertIn("/api/energy", paths)


class OneImplementationOfPeriodResolution(unittest.TestCase):

    def test_resolve_period_is_the_only_caller_of_the_billing_lookup_here(self):
        """A fix to the billing-period lookup must not be able to reach three
        routes and miss three. Asserted structurally: the new endpoint resolves
        periods through `_resolve_period` and nowhere else."""
        import ast
        src = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "api_energy")
        called = {getattr(c.func, "id", None) or getattr(c.func, "attr", None)
                  for c in ast.walk(fn) if isinstance(c, ast.Call)}
        self.assertIn("_resolve_period", called)
        self.assertNotIn("get_billing_periods_from_config_periods", called)


if __name__ == "__main__":
    unittest.main()
