"""
The soak harness (.build/soak.py, .build/soak_prepare_db.py) — A §5.4, B §5.6.

The soak runs against a live add-on whose Octopus account is the production one,
so the guarantees that matter most are what it will NOT do: send anything but GET,
or request a route that reaches Octopus, Home Assistant, the Supervisor or the
internet. Those are pinned structurally here. The checks it makes (report verdict,
log scan, segment comparison) and the database preparation are exercised on
synthetic inputs and a real file-backed store; `run` is driven end to end against
a local stub server.
"""
import ast
import http.server
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, ".build"))

import soak                      # noqa: E402
import soak_prepare_db           # noqa: E402

_SOAK_SRC = os.path.join(_ROOT, ".build", "soak.py")


class OnlyEverGET(unittest.TestCase):

    def test_the_one_request_the_script_builds_is_a_GET(self):
        tree = ast.parse(open(_SOAK_SRC, encoding="utf-8").read())
        builds = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                  and getattr(n.func, "attr", "") in ("Request", "urlopen", "request",
                                                       "HTTPConnection", "HTTPSConnection")]
        reqs = [n for n in builds if getattr(n.func, "attr", "") == "Request"]
        self.assertEqual(len(reqs), 1, "exactly one place constructs a request")
        method = {kw.arg: kw.value for kw in reqs[0].keywords}.get("method")
        self.assertIsInstance(method, ast.Constant)
        self.assertEqual(method.value, "GET")
        self.assertFalse([n for n in builds if getattr(n.func, "attr", "") in
                          ("request", "HTTPConnection", "HTTPSConnection")])
        for n in builds:
            if getattr(n.func, "attr", "") == "urlopen":
                self.assertIsInstance(n.args[0], ast.Name, "urlopen only takes the GET Request")
        self.assertFalse(any(isinstance(n, ast.keyword) and n.arg == "data"
                             for n in ast.walk(tree)), "no request body anywhere")


class RouteSelection(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.routes = soak.discover_get_routes()

    def test_it_finds_the_read_surface(self):
        self.assertGreaterEqual(len(self.routes), 60)
        for r in ("/api/billing-source", "/api/periods", "/api/energy"):
            self.assertIn(r, self.routes)

    def test_nothing_denied_is_requested(self):
        self.assertFalse(set(self.routes) & set(soak.DENY))

    def test_the_traced_external_callers_are_denied(self):
        """Each traced from the code: Octopus (conformance), Supervisor (logs), HA
        (power, entities, detect-bcd), the internet (carbon, update-check)."""
        for r in ("/api/reprice-history-conformance", "/api/logs", "/api/power",
                  "/api/entities", "/api/detect-bcd", "/api/carbon", "/api/update-check"):
            self.assertIn(r, soak.DENY)

    def test_a_handler_that_also_takes_POST_is_never_selected(self):
        """A handler taking GET *and* POST (/api/carbon/pause) may act on a GET. A path
        with SEPARATE GET and POST handlers (/api/settings) is fine: a GET only ever
        reaches the read handler."""
        tree = ast.parse(open(os.path.join(_ROOT, "web", "server.py"),
                              encoding="utf-8").read())
        mixed = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "route":
                for kw in n.keywords:
                    if kw.arg == "methods":
                        ms = {e.value for e in kw.value.elts}
                        if "GET" in ms and ms - {"GET"}:
                            mixed.add(n.args[0].value)
        self.assertIn("/api/carbon/pause", mixed)
        self.assertFalse(set(self.routes) & mixed)

    def test_routes_that_need_arguments_get_them(self):
        """First Pi soak: these three answered 400 on every request, so they were
        never actually exercised."""
        import datetime as dt
        ctx = {"today": dt.date(2026, 9, 29), "periods": [], "months": [], "years": [],
               "sub_meters": ["ev_charger"]}
        gt = soak.query_variants("/api/historical/gap-template", ctx)
        self.assertTrue(gt and all("from=" in u and "to=" in u for u in gt))
        nr = soak.query_variants("/api/corrections/nearby-rates", ctx)
        self.assertTrue(nr and all("from_date=" in u and "to_date=" in u for u in nr))
        self.assertEqual(soak.query_variants("/api/sub-meter/history", ctx),
                         ["/api/sub-meter/history?meter_id=ev_charger&hours=24"])

    def test_with_no_sub_meter_its_history_is_not_requested(self):
        import datetime as dt
        ctx = {"today": dt.date(2026, 9, 29), "periods": [], "months": [], "years": [],
               "sub_meters": []}
        self.assertEqual(soak.query_variants("/api/sub-meter/history", ctx), [])

    def test_energy_is_asked_for_all_three_resolutions(self):
        """The route's docstring claimed day|block returned 501; they did not, and
        the first soaks exercised only period."""
        import datetime as dt
        ctx = {"today": dt.date(2026, 9, 29), "periods": [], "months": [], "years": []}
        us = soak.query_variants("/api/energy", ctx)
        for res in ("period", "day", "block"):
            self.assertTrue(any("resolution=%s" % res in u for u in us), res)
        self.assertFalse(any("resolution=block" in u and "kind=year" in u for u in us),
                         "a year of blocks is not a viewport")

    def test_the_billing_tabs_requests_are_exercised(self):
        """REGRESSION (C, 30 Sep 2026): the Billing tab builds a bill and a month of
        day charts per request since the pre-render was deleted; a soak that never
        asks for include=billing cannot say what that costs on the Pi."""
        import datetime as dt
        ctx = {"today": dt.date(2026, 9, 29), "periods": ["2026-08-03", "2026-09-03"],
               "months": [(2026, 9), (2026, 8), (2026, 7), (2026, 6)], "years": []}   # build_context's four
        us = soak.query_variants("/api/energy", ctx)
        for k in ("billing", "month", "quarter", "year"):
            self.assertTrue(any("resolution=period" in u and "kind=%s" % k in u
                                and "include=billing" in u for u in us), k)
        self.assertTrue(any("period_start=2026-08-03" in u and "include=billing" in u for u in us))
        self.assertTrue(any("resolution=day" in u and "month=8" in u and "include=billing" in u
                            for u in us))
        ps = soak.query_variants("/api/periods", ctx)
        self.assertEqual(sum("include=net_cost" in u and "kind=live" not in u for u in ps), 4)

    def test_the_pages_most_frequent_requests_are_exercised(self):
        """REGRESSION (C row 8, 2 Oct 2026): the six soaks before this never sent the
        Charts change poll, the recent-day check, the moved-day re-fetch or Live
        Power's cards — the requests a browser makes most often."""
        import datetime as dt
        ctx = {"today": dt.date(2026, 10, 2), "periods": [],
               "months": [(2026, 10), (2026, 9), (2026, 8), (2026, 7)], "years": []}
        us = soak.query_variants("/api/energy", ctx)
        self.assertIn("/api/energy?fields=fingerprint", us)
        self.assertIn("/api/energy?resolution=day&kind=all&fields=days&since=2026-08-28", us)
        self.assertTrue(any("kind=range" in u and "fields=stats" in u for u in us))
        self.assertIn("/api/energy?resolution=block&kind=month&year=2026&month=9", us)
        self.assertIn("/api/periods?kind=live&include=net_cost%2Crows",
                      soak.query_variants("/api/periods", ctx))

    def test_budgeted_bills_ask_for_a_full_period(self):
        """REGRESSION (seventh soak, 2 Oct 2026): "this month" on the 2nd is two days of
        data, so a month's bill measured 0.05 s; "this year" grows until December. A
        bound judged on either moves with the calendar."""
        import datetime as dt
        ctx = {"today": dt.date(2026, 10, 2), "periods": ["2026-08-03", "2026-09-03", "2026-10-03"],
               "months": [(2026, 10), (2026, 9), (2026, 8), (2026, 7)], "years": []}
        us = [u for u in soak.query_variants("/api/energy", ctx) if soak.surface_of(u)]
        for u in us:
            self.assertNotIn("month=10", u, u)
            self.assertNotIn("year=2026&include", u, u)
        self.assertIn("/api/energy?resolution=period&kind=quarter&year=2026&quarter=3&include=billing", us)
        self.assertIn("/api/energy?resolution=period&kind=year&year=2025&include=billing", us)
        self.assertIn("/api/energy?resolution=period&kind=month&year=2026&month=9&include=billing", us)
        self.assertIn("/api/energy?resolution=block&kind=month&year=2026&month=9", us)
        self.assertFalse([u for u in us if "kind=billing" in u and "period_start" not in u
                          and "include=billing" in u], "the current billing period is part-full")
        jan = dict(ctx, today=dt.date(2027, 1, 5))
        self.assertIn("/api/energy?resolution=period&kind=quarter&year=2026&quarter=4&include=billing",
                      soak.query_variants("/api/energy", jan))

    def test_the_budgets_are_set(self):
        """C row 8: every surface bounded, from the tenth soak; no p95 beyond the timeout."""
        b = soak.load_budgets()
        self.assertEqual(sum(v["p50"] is not None for v in b.values()), len(soak.SURFACES))
        self.assertTrue(all(v["p95"] is None or v["p95"] <= 30 for v in b.values()))



class TheWal(unittest.TestCase):
    """The 8th soak (3 Oct 2026): a starved WAL (346 MB through the whole quiet phase)
    slowed every read, and nothing in the report said so. New-facet tests: they fail on
    the parent tree, where the report has no WAL lines."""

    def test_a_starved_quiet_phase_is_said_out_loud(self):
        p = _write_rows(_rows("quiet", "/api/config", 0.1, 5)
                        + [{"t": 1.0, "wal_mb": 4.0}, {"t": 2.0, "wal_mb": 346.1}])
        r = soak.summarise(p)
        self.assertIn("quiet-phase WAL: first 4.0 MB, max 346.1 MB", r)
        self.assertIn("WAL STARVED", r)

    def test_a_drained_wal_is_not(self):
        """GUARD: passes on the parent tree too (it never warned at all)."""
        p = _write_rows(_rows("quiet", "/api/config", 0.1, 5)
                        + [{"t": 1.0, "wal_mb": 4.0}, {"t": 2.0, "wal_mb": 6.5}])
        self.assertNotIn("WAL STARVED", soak.summarise(p))

    def test_a_settle_that_timed_out_is_reported(self):
        p = _write_rows(_rows("quiet", "/api/config", 0.1, 5)
                        + [{"t": 1.0, "settle": {"wal_mb": 120.0, "secs": 300.0, "settled": False}}])
        self.assertIn("settle: WAL did NOT settle at 120.0 MB after 300.0 s", soak.summarise(p))

class Surfaces(unittest.TestCase):
    """C §6 row 8: the page requests by name, and a budget for each. New-surface tests:
    they fail on the parent tree, where SURFACES and `report` do not exist."""

    def _urls(self):
        import datetime as dt
        ctx = {"today": dt.date(2026, 10, 2), "periods": ["2026-08-03", "2026-09-03"],
               "months": [(2026, 10), (2026, 9)], "years": [2026, 2025]}
        return [u for p in ("/api/energy", "/api/periods") for u in soak.query_variants(p, ctx)]

    def test_every_surface_is_soaked(self):
        hit = {soak.surface_of(u) for u in self._urls()}
        self.assertEqual([n for n, _, _ in soak.SURFACES if n not in hit], [])

    def test_the_budget_file_names_exactly_the_surfaces(self):
        """No ghost and no missing budget: a renamed surface must be renamed there."""
        self.assertEqual(sorted(soak.load_budgets()), sorted(n for n, _, _ in soak.SURFACES))

    def test_the_most_specific_surface_wins(self):
        self.assertEqual(soak.surface_of("/api/periods?kind=billing&include=carbon"),
                         "Insights: trend strip")
        self.assertEqual(soak.surface_of("/api/periods?kind=billing"), "Period picker")
        self.assertEqual(soak.surface_of("/api/periods?kind=live&include=net_cost%2Crows"),
                         "Live Power: cards")
        self.assertEqual(soak.surface_of("/api/energy?resolution=block&kind=month&year=2026&month=9"),
                         "Heatmap: a month of blocks")
        self.assertIsNone(soak.surface_of("/api/energy?resolution=block&kind=month"))
        self.assertIsNone(soak.surface_of("/api/config"))

    def _report(self, rows, budgets):
        return soak.summarise(_write_rows(rows), budgets=budgets)

    def test_a_surface_over_its_budget_fails(self):
        poll = "/api/energy?fields=fingerprint"
        r = self._report(_rows("quiet", poll, 0.40, 20),
                         {"Charts: change poll": {"p50": 0.25, "p95": None}})
        self.assertIn("verdict: FAIL", r)
        self.assertIn("OVER BUDGET Charts: change poll: p50 0.400s > 0.25s", r)

    def test_within_budget_passes(self):
        r = self._report(_rows("quiet", "/api/energy?fields=fingerprint", 0.10, 20),
                         {"Charts: change poll": {"p50": 0.25, "p95": 0.5}})
        self.assertIn("verdict: PASS", r)
        self.assertRegex(r, r"Charts: change poll +n=20 +p50 0\.100s")

    def test_a_null_budget_is_measured_not_asserted(self):
        r = self._report(_rows("quiet", "/api/energy?fields=fingerprint", 9.0, 20),
                         {"Charts: change poll": {"p50": None, "p95": None}})
        self.assertIn("verdict: PASS", r)
        self.assertIn("budget - / -", r)

    def test_only_the_quiet_phase_is_judged(self):
        """The sweep is a deliberate whole-history write, not what a user meets."""
        poll = "/api/energy?fields=fingerprint"
        r = self._report(_rows("sweep", poll, 5.0, 20) + _rows("quiet", poll, 0.1, 20),
                         {"Charts: change poll": {"p50": 0.25, "p95": 0.5}})
        self.assertIn("verdict: PASS", r)

    def test_an_unmeasured_surface_says_so(self):
        self.assertRegex(self._report(_rows("quiet", "/api/config", 0.1, 5), {}),
                         r"Live Power: cards +NOT MEASURED")

    def test_report_re_judges_a_saved_run(self):
        path = _write_rows(_rows("quiet", "/api/energy?fields=fingerprint", 0.4, 20))
        fd, bud = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"budgets": {"Charts: change poll": {"p50": 0.25, "p95": None}}}, f)
        self.assertEqual(soak.main(["report", path, "--budgets", bud]), 1)


def _write_rows(rows):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


def _rows(phase, url, secs, n, status=200, **extra):
    return [dict({"phase": phase, "url": url, "status": status, "secs": secs}, **extra)
            for _ in range(n)]


class Verdict(unittest.TestCase):

    def test_clean_run_passes(self):
        p = _write_rows(_rows("sweep", "/api/billing", 0.12, 20)
                        + _rows("quiet", "/api/billing", 0.10, 20))
        self.assertIn("verdict: PASS", soak.summarise(p))

    def test_a_500_fails(self):
        p = _write_rows(_rows("sweep", "/api/billing", 0.1, 10)
                        + _rows("quiet", "/api/billing", 0.1, 10)
                        + _rows("sweep", "/api/config", 0.1, 1, status=500, body="boom"))
        self.assertIn("verdict: FAIL", soak.summarise(p))

    def test_a_501_is_not_a_server_fault(self):
        p = _write_rows(_rows("sweep", "/api/billing", 0.1, 10)
                        + _rows("quiet", "/api/billing", 0.1, 10)
                        + _rows("quiet", "/api/energy", 0.1, 1, status=501))
        self.assertIn("verdict: PASS", soak.summarise(p))

    def test_invalid_json_fails(self):
        p = _write_rows(_rows("sweep", "/api/billing", 0.1, 10)
                        + _rows("quiet", "/api/billing", 0.1, 10, bad_json=True))
        self.assertIn("verdict: FAIL", soak.summarise(p))

    def test_a_connection_failure_fails(self):
        p = _write_rows(_rows("sweep", "/api/billing", 0.1, 10)
                        + _rows("quiet", "/api/billing", 0.1, 10)
                        + [{"phase": "sweep", "url": "/api/billing", "status": None,
                            "error": "timeout"}])
        self.assertIn("verdict: FAIL", soak.summarise(p))

    def test_a_route_slower_during_the_sweep_is_information_not_a_verdict(self):
        """Second soak: a static PNG slowed x54 under the sweep. Latency cannot tell
        a saturated Pi from blocking; scan-log's loop lag judges blocking."""
        p = _write_rows(_rows("sweep", "/api/billing", 4.0, 20)
                        + _rows("quiet", "/api/billing", 0.1, 20))
        r = soak.summarise(p)
        self.assertIn("verdict: PASS", r)
        self.assertIn("SLOWER DURING THE SWEEP", r)
        self.assertIn("/api/billing", r)

    def test_a_small_absolute_slowdown_is_not_blocking(self):
        """x10 on a 20 ms route is 200 ms — under the 1 s floor, so not a finding."""
        p = _write_rows(_rows("sweep", "/api/billing", 0.2, 20)
                        + _rows("quiet", "/api/billing", 0.02, 20))
        self.assertIn("verdict: PASS", soak.summarise(p))

    def test_a_timeout_is_slow_not_a_failure(self):
        p = _write_rows(_rows("sweep", "/api/billing", 0.1, 20)
                        + _rows("quiet", "/api/billing", 0.1, 20)
                        + _rows("quiet", "/api/charts/blocks-summary", 30.0, 1, status=None,
                                timeout=True, error="TimeoutError"))
        r = soak.summarise(p)
        self.assertIn("verdict: PASS", r)
        self.assertIn("SLOW", r)
        self.assertIn("/api/charts/blocks-summary", r)

    def test_timeouts_during_the_sweep_still_show_in_the_slowdown(self):
        """Excluding timeouts from latency would hide the worst slowdown."""
        p = _write_rows(_rows("sweep", "/api/billing", 30.0, 20, status=None, timeout=True)
                        + _rows("quiet", "/api/billing", 0.1, 20))
        r = soak.summarise(p)
        self.assertIn("SLOWER DURING THE SWEEP", r)
        self.assertIn("/api/billing: sweep p95 30.000s", r)

    def test_no_sweep_phase_is_said_out_loud(self):
        p = _write_rows(_rows("quiet", "/api/billing", 0.1, 20))
        self.assertIn("NO SWEEP PHASE", soak.summarise(p))


def _log(*lines):
    fd, path = tempfile.mkstemp(suffix=".log")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _c(ts, msg):
    return "2026-09-29T%s  INFO      job_coordinator  coord: %s" % (ts, msg)


class LogScan(unittest.TestCase):

    def test_an_orderly_log_passes(self):
        p = _log(_c("10:00:00", "claim job=import key=import"),
                 _c("10:00:01", "defer job=reprice_sweep blocker=import edge=depends_on"),
                 _c("10:05:00", "mark job=import key=import status=done was=running"),
                 _c("10:05:30", "admit_after job=reprice_sweep was=defer blocker=import"),
                 _c("10:05:31", "claim job=verify_pricing key=verify_pricing"))
        report, ok = soak.scan_log(p)
        self.assertTrue(ok, report)
        self.assertIn("reprice_sweep", report)

    def test_a_dependant_starting_during_its_dependency_fails(self):
        p = _log(_c("10:00:00", "claim job=import key=import"),
                 _c("10:00:05", "claim job=verify_pricing key=verify_pricing"))
        report, ok = soak.scan_log(p)
        self.assertFalse(ok)
        self.assertIn("DEPENDENCY VIOLATION", report)

    def test_the_dependency_ending_first_is_fine(self):
        p = _log(_c("10:00:00", "claim job=import key=import"),
                 _c("10:00:01", "release job=import key=import"),
                 _c("10:00:05", "claim job=verify_pricing key=verify_pricing"))
        self.assertTrue(soak.scan_log(p)[1])

    def test_a_wait_that_never_ends_is_starvation(self):
        p = _log(_c("10:00:00", "claim job=import key=import"),
                 _c("10:00:01", "defer job=carbon_backfill blocker=import edge=depends_on"),
                 _c("12:30:00", "mark job=gap_fill key=gap_fill status=done was=running"))
        report, ok = soak.scan_log(p, starve_min=60)
        self.assertFalse(ok)
        self.assertIn("STARVATION carbon_backfill", report)

    def test_a_waitress_client_disconnect_is_not_a_fault(self):
        p = _log("2026-09-29T20:31:17  ERROR     waitress  Socket error",
                 "Traceback (most recent call last):",
                 '  File "/usr/lib/python3.12/site-packages/waitress/channel.py", line 125',
                 "TimeoutError: [Errno 110] Operation timed out")
        report, _ = soak.scan_log(p)
        self.assertIn("verdict A (database faults): PASS", report)
        self.assertNotIn("other traceback", report)
        self.assertRegex(report, r"waitress client disconnects\s+1")

    def test_a_log_with_no_coordinator_activity_says_B_was_not_tested(self):
        p = _log("2026-09-29T10:00:00  INFO  engine  finalise_block: done")
        report, _ = soak.scan_log(p)
        self.assertIn("NOT EXERCISED", report)
        self.assertIn("B (coordinator): NOT EXERCISED", report)

    _MON = "2026-09-29T21:00:00  INFO  engine  engine loop lag monitor started (interval 0.5 s, warn above 1.0 s)"

    def _lag(self, ts, s):
        return "2026-09-29T%s  WARNING  engine  engine loop lag: %.2f s (loop held past a 0.5 s wake-up)" % (ts, s)

    def test_loop_lag_within_budget_passes(self):
        report, ok = soak.scan_log(_log(self._MON, self._lag("21:10:00", 1.3)))
        self.assertTrue(ok, report)
        self.assertIn("A (loop blocking): PASS", report)

    def test_loop_lag_over_budget_is_blocking(self):
        """The second soak's shape: the sweep held the loop ~14 s per chunk."""
        report, ok = soak.scan_log(_log(self._MON, self._lag("21:10:00", 14.2)))
        self.assertFalse(ok)
        self.assertIn("A (loop blocking): FAIL", report)
        self.assertIn("14.20 s", report)

    def test_no_monitor_and_no_lag_lines_is_not_measured_not_pass(self):
        """A build without the monitor: nothing to measure, so no verdict either way."""
        report, ok = soak.scan_log(_log("2026-09-29T21:10:00  INFO  engine  finalise_block: done"))
        self.assertIn("A (loop blocking): NOT MEASURED", report)

    def test_lag_lines_without_the_start_line_are_still_measured(self):
        """Third soak: the HA download was capped at 1,000 lines and began six
        seconds after the monitor started — 130 lag warnings, reported NOT MEASURED."""
        report, ok = soak.scan_log(_log(self._lag("21:10:00", 445.54)))
        self.assertIn("A (loop blocking): FAIL", report)
        self.assertIn("truncated", report)

    def test_a_watchdog_dump_is_summarised_with_its_application_frame(self):
        dump = [
            "2026-09-30T07:45:44  WARNING   engine  engine loop stalled: 12.0 s — thread stacks "
            "follow (engine loop = MainThread)",
            "  --- ENGINE LOOP MainThread (1)",
            "    base_events.py:1910 in _run_once: event_list = self._selector.select(timeout)",
            "    selectors.py:468 in select: fd_event_list = self._selector.poll(timeout, max_ev)",
            "  --- thread asyncio_0 (2)",
            "    engine.py:2467 in _work: generate_charts(ro, config=cfg)",
            "    energy_charts.py:812 in generate_daily_import_export_charts: fig = _build(rows)",
            "    encoder.py:258 in iterencode: return _iterencode(o, 0)",
        ]
        report, _ = soak.scan_log(_log(self._MON, *dump))
        self.assertIn("watchdog stack dumps: 1", report)
        self.assertIn("ENGINE LOOP", report)
        self.assertIn("selectors.py:468 select", report)
        self.assertIn("(app: energy_charts.py:812 generate_daily_import_export_charts)", report)

    def test_since_skips_older_builds_in_a_saved_log(self):
        """Fourth soak: the saved log reached back to 24 Sep and counted a 29 Sep
        thread-affinity error that the build under test did not have."""
        p = _log("2026-09-29T19:57:06  ERROR  engine  load_config: DB read failed: SQLite objects "
                 "created in a thread can only be used in that same thread",
                 self._MON.replace("2026-09-29T21", "2026-09-30T09"))
        self.assertIn("database faults): FAIL", soak.scan_log(p)[0])
        self.assertIn("database faults): PASS",
                      soak.scan_log(p, since="2026-09-30T00:00:00")[0])

    def test_slow_commits_are_summarised_with_checkpoints_marked(self):
        p = _log(self._MON,
                 "2026-09-29T21:10:00  WARNING  block_store  slow commit: 24.51 s in "
                 "save_current_block (WAL 38.2 MB -> 4.0 MB — a checkpoint ran in this commit)",
                 "2026-09-29T21:11:00  WARNING  block_store  slow commit: 1.20 s in "
                 "set_block_segments (WAL 5.1 MB -> 5.3 MB)")
        report, _ = soak.scan_log(p)
        self.assertIn("slow commits (over 1 s): 2, of which 1 had a WAL checkpoint", report)
        self.assertIn("CHECKPOINT", report)
        self.assertIn("save_current_block 1", report)

    def test_the_budget_is_configurable(self):
        report, ok = soak.scan_log(_log(self._MON, self._lag("21:10:00", 3.0)), lag_budget=5)
        self.assertTrue(ok)

    def test_database_errors_fail(self):
        p = _log("2026-09-29T10:00:00  ERROR  engine  sqlite3.ProgrammingError: "
                 "SQLite objects created in a thread can only be used in that same thread")
        report, ok = soak.scan_log(p)
        self.assertFalse(ok)
        self.assertIn("ProgrammingError", report)

    def test_a_prefixed_supervisor_line_still_parses(self):
        p = _log("\x1b[32m" + _c("10:00:00", "claim job=import key=import"),
                 "\x1b[32m" + _c("10:00:05", "claim job=verify_pricing key=verify_pricing"))
        self.assertIn("DEPENDENCY VIOLATION", soak.scan_log(p)[0])


def _make_db(path):
    from block_store import BlockStore
    st = BlockStore(path)
    st._conn.execute("PRAGMA foreign_keys=OFF")
    st._conn.execute("INSERT INTO meters (meter_id, config_period_id, is_sub_meter) "
                     "VALUES ('electricity_main', 1, 0), ('ev', 1, 1)")
    for i, bs in enumerate(("2026-09-01T00:00:00", "2026-09-01T00:30:00",
                            "2026-09-01T01:00:00")):
        st._conn.execute("INSERT INTO blocks (block_start, block_end, config_period_id, "
                         "meter_id, imp_kwh, imp_cost) VALUES (?, ?, 1, 'electricity_main', "
                         "1.0, 0.3)", (bs, bs))
        st._conn.execute("INSERT INTO block_segments (block_start, meter_id, channel, seq, "
                         "kwh, inc_rate, band, attribution) VALUES (?, 'electricity_main', "
                         "'import', 0, 1.0, 0.3, 'day', 'house')", (bs,))
    # an EXPORT segment and a SUB-METER segment: neither is the sweep's to rebuild
    st._conn.execute("INSERT INTO block_segments (block_start, meter_id, channel, seq, kwh, "
                     "inc_rate, band, attribution) VALUES ('2026-09-01T00:00:00', "
                     "'electricity_main', 'export', 0, 0.5, 0.15, 'day', 'house')")
    st._conn.execute("INSERT INTO blocks (block_start, block_end, config_period_id, meter_id, "
                     "imp_kwh, imp_cost) VALUES ('2026-09-01T00:00:00', "
                     "'2026-09-01T00:00:00', 1, 'ev', 0.4, 0.1)")
    st._conn.execute("INSERT INTO block_segments (block_start, meter_id, channel, seq, kwh, "
                     "inc_rate, band, attribution) VALUES ('2026-09-01T00:00:00', 'ev', "
                     "'import', 0, 0.4, 0.3, 'day', 'ev')")
    st.set_meta("reprice_history_state", {"done": True, "scope": 2})
    st.set_meta("written_by", "4.5.16")
    st._conn.commit()
    return st


class PrepareDb(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "source.db")
        self.out = os.path.join(self.tmp.name, "soak")
        self.st = _make_db(self.src)
        soak_prepare_db.main([self.src, self.out])

    def tearDown(self):
        self.st._conn.close()
        self.tmp.cleanup()

    def _q(self, db, sql):
        c = sqlite3.connect(db)
        try:
            return c.execute(sql).fetchall()
        finally:
            c.close()

    def test_the_source_is_untouched(self):
        self.assertEqual(self._q(self.src, "SELECT COUNT(*) FROM block_segments")[0][0], 5)
        self.assertTrue(self._q(self.src, "SELECT value FROM store_meta "
                                          "WHERE key='reprice_history_state'"))

    def test_only_the_sweeps_segments_are_removed_and_snapshotted(self):
        out_db = os.path.join(self.out, "blocks.db")
        left = self._q(out_db, "SELECT meter_id, channel FROM block_segments ORDER BY 1, 2")
        self.assertEqual(left, [("electricity_main", "export"), ("ev", "import")])
        snap = self._q(os.path.join(self.out, "segments_before.db"),
                       "SELECT COUNT(*) FROM block_segments")[0][0]
        self.assertEqual(snap, 3)

    def test_the_marker_is_cleared_and_the_sweep_sees_every_block(self):
        from block_store import BlockStore
        st = BlockStore(os.path.join(self.out, "blocks.db"))
        try:
            self.assertIsNone(st.get_meta("reprice_history_state", None))
            self.assertEqual(st.count_blocks_needing_reprice(), 3)
        finally:
            st._conn.close()

    def test_it_refuses_to_overwrite(self):
        with self.assertRaises(SystemExit):
            soak_prepare_db.main([self.src, self.out])

    def test_compare_reports_identical_after_a_faithful_rebuild(self):
        out_db = os.path.join(self.out, "blocks.db")
        c = sqlite3.connect(out_db)
        c.execute("ATTACH DATABASE ? AS b", (os.path.join(self.out, "segments_before.db"),))
        c.execute("INSERT INTO block_segments SELECT * FROM b.block_segments")
        c.commit()
        c.close()
        r = soak.compare_segments(os.path.join(self.out, "segments_before.db"), out_db)
        self.assertIn("3 identical, 0 differ, 0 missing, 0 new", r)

    def test_compare_reports_a_changed_band(self):
        out_db = os.path.join(self.out, "blocks.db")
        c = sqlite3.connect(out_db)
        c.execute("ATTACH DATABASE ? AS b", (os.path.join(self.out, "segments_before.db"),))
        c.execute("INSERT INTO block_segments SELECT * FROM b.block_segments")
        c.execute("UPDATE block_segments SET band='off_peak' "
                  "WHERE block_start='2026-09-01T00:30:00' AND channel='import'")
        c.commit()
        c.close()
        r = soak.compare_segments(os.path.join(self.out, "segments_before.db"), out_db)
        self.assertIn("2 identical, 1 differ", r)


class _Stub(http.server.BaseHTTPRequestHandler):
    """A stand-in add-on: the sweep runs for the first few status polls, reads are
    slow while it does, and one route always fails."""
    polls = 0
    methods = []
    starts_at = 0          # polls before the sweep is reported running
    wal_polls = 0
    wal_mb = (50.0, 50.0, 4.0)   # /api/storage-info: high just after the sweep, then drained

    def log_message(self, *a):
        pass

    def do_GET(self):
        _Stub.methods.append("GET")
        path = self.path.split("?")[0]
        if path == soak.STATUS:
            _Stub.polls += 1
            running = _Stub.starts_at < _Stub.polls < _Stub.starts_at + 4
            body = {"in_progress": running, "swept": _Stub.polls,
                    "done": _Stub.polls >= _Stub.starts_at + 4}
        elif path == soak.STORAGE:
            _Stub.wal_polls += 1
            body = {"wal_mb": _Stub.wal_mb[min(_Stub.wal_polls, len(_Stub.wal_mb)) - 1]}
        elif path == "/api/periods":
            body = {"periods": [{"start": "2026-08-01", "end": "2026-09-01"}]}
        elif path == "/api/historical/csv-template":
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.end_headers()
            self.wfile.write(b"a,b\n1,2\n")
            return
        elif path == "/api/config":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"torn")
            return
        else:
            body = {"ok": True}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        _Stub.methods.append("POST")
        self.send_response(405)
        self.end_headers()


class EndToEnd(unittest.TestCase):

    def setUp(self):
        _Stub.polls, _Stub.methods, _Stub.starts_at = 0, [], 0
        _Stub.wal_polls, _Stub.wal_mb = 0, (50.0, 50.0, 4.0)

    def test_it_waits_for_a_sweep_that_starts_after_it_does(self):
        """The Data Management restore case: the sweep starts only after
        engine_startup re-runs and Octopus connects."""
        _Stub.starts_at = 3
        self.test_run_goes_sweep_then_quiet_and_reports()

    def test_run_goes_sweep_then_quiet_and_reports(self):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        old_poll = soak.POLL_S
        soak.POLL_S = 0.2
        try:
            with tempfile.TemporaryDirectory() as d:
                out = os.path.join(d, "soak")
                soak.main(["run", "--base", "http://127.0.0.1:%d" % srv.server_port,
                           "--rate", "200", "--workers", "3", "--timeout", "2",
                           "--quiet-min", "0.02", "--settle-max-s", "5", "--out", out])
                report = open(os.path.join(out, "report.txt")).read()
                rows = [json.loads(l) for l in open(os.path.join(out, "requests.jsonl"))]
        finally:
            soak.POLL_S = old_poll
            srv.shutdown()
        phases = {r["phase"] for r in rows if "url" in r}
        self.assertEqual(phases, {"sweep", "quiet"})        # nothing is sent while settling
        settle = [r["settle"] for r in rows if "settle" in r]
        self.assertEqual(len(settle), 1)
        self.assertTrue(settle[0]["settled"])
        self.assertEqual(settle[0]["wal_mb"], 4.0)
        self.assertIn("settle: WAL settled at 4.0 MB", report)
        settled_at = next(r["t"] for r in rows if "settle" in r)
        self.assertFalse([r for r in rows if "url" in r and r["phase"] == "quiet"
                          and r["t"] < settled_at], "quiet requests began before the WAL settled")
        self.assertIn("verdict: FAIL", report)        # the stub's /api/config 500
        self.assertIn("500 /api/config", report)
        self.assertEqual(set(_Stub.methods), {"GET"})
        self.assertNotIn("invalid JSON", report, "a CSV under /api is not bad JSON")
        requested = {r["url"].split("?")[0] for r in rows if "url" in r}
        self.assertFalse(requested & set(soak.DENY))


if __name__ == "__main__":
    unittest.main()
