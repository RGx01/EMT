"""
G1d: a change of agreements re-settles the history it moves (G1 design §4, 28 Oct 2026).

A changed agreement rebuilt the rate schedules, but nothing re-settled the history it covered, and the
health strip's month counts read discovery's agreements only — none before it answered. Now:

- the stored answer's diff gives spans — each added or superseded agreement's window, a moved one both,
  clipped to the history and merged; the first answer stored is the whole history;
- they are queued in store_meta, and the settle sweep's job works them off a day at a time once the
  history walk is done, after the rate refresh that builds the new agreements' rates;
- the health strip's agreements digest reads the stored agreements too, and the same for both forms;
- the guard (the user: "if there's any api/graphql deprecations we don't get in a mess"): an answer
  that drops or moves an agreement that started and ended over 30 days ago is not taken — a customer
  can change only recent ones (14 days' cooling-off) — and the readers, the rate stitch among them,
  keep the last good copy.

Discriminating: every test here fails on the parent, but those labelled GUARD.
"""
import inspect
import os
from datetime import datetime, timedelta
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine                                    # noqa: E402
import test_daily_rediscovery as R               # noqa: E402  (_run, _Harness)

FIRST, END = "2024-01-01T00:00:00", "2026-10-28T00:00:00"
IOG = "E-1R-INTELLI-VAR-22-10-14-B"
AGILE = "E-1R-AGILE-23-12-06-B"


class TheSpans(unittest.TestCase):

    def _spans(self, **diff):
        return engine._agreement_change_spans(dict({"added": [], "superseded": [], "kept": []}, **diff),
                                              FIRST, END)

    def test_a_moved_window_is_its_old_and_new(self):
        got = self._spans(added=[("import", IOG, "2025-01-01T00:00:00", "2026-03-17T00:00:00")],
                          superseded=[("import", IOG, "2025-01-01T00:00:00", None)],
                          kept=[("import", AGILE, "2024-01-01T00:00:00", "2025-01-01T00:00:00")])
        self.assertEqual(got, [["2025-01-01T00:00:00", END]])

    def test_a_withdrawn_agreement_is_its_window(self):
        got = self._spans(superseded=[("import", AGILE, "2024-06-01T00:00:00", "2024-07-01T00:00:00")],
                          kept=[("import", IOG, "2025-01-01T00:00:00", None)])
        self.assertEqual(got, [["2024-06-01T00:00:00", "2024-07-01T00:00:00"]])

    def test_the_first_answer_is_the_whole_history(self):
        self.assertEqual(self._spans(added=[("import", IOG, "2025-01-01T00:00:00", None)]), [[FIRST, END]])

    def test_no_change_is_nothing(self):
        self.assertEqual(self._spans(kept=[("import", IOG, "2025-01-01T00:00:00", None)]), [])

    def test_spans_merge(self):
        self.assertEqual(engine._merge_spans([["b", "d"], ["a", "c"], ["e", "f"]]), [["a", "d"], ["e", "f"]])


class TheQueue(R._Harness):

    def setUp(self):
        super().setUp()
        self._saved = engine._stored_agreements
        engine._AGREEMENT_WINDOWS.clear()
        self.st.set_kraken_state(engine._ACCOUNT_KEY, "A-1")
        self.st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, "
                              "block_minutes, timezone) VALUES (1, '2020-01-01T00:00:00', 1, 30, 'UTC')")
        for bs in ("2025-06-01T00:00:00", "2025-06-01T00:30:00", "2025-06-03T12:00:00"):
            self.st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                                  "imp_kwh) VALUES (?, ?, 'electricity_main', 1, 1.0)", (bs, bs))
        self.st._conn.commit()

    def tearDown(self):
        engine._stored_agreements = self._saved
        engine._AGREEMENT_WINDOWS.clear()
        super().tearDown()

    def _disc(self, vt=None):
        return {"import": {"mpan": "M1", "tariff_code": IOG, "agreements": [
            {"tariff_code": IOG, "valid_from": "2025-06-02T00:00:00Z", "valid_to": vt}]}}

    def test_a_change_queues_its_span(self):
        engine._keep_agreements(self._disc())
        self.st.set_meta(engine._SETTLE_SPANS_KEY, [])
        # a 2025 window moved: the guard would refuse it (TheGuard); here, what a taken change queues
        with mock.patch.object(engine, "_AGREEMENT_CHANGE_DAYS", 100000):
            engine._keep_agreements(self._disc(vt="2025-06-03T00:00:00Z"))
        self.assertEqual(self.st.get_meta(engine._SETTLE_SPANS_KEY),
                         [["2025-06-02T00:00:00", "2025-06-03T12:30:00"]])

    def test_the_pass_settles_only_the_span_a_day_at_a_time(self):
        self.st.set_meta(engine._SETTLE_SPANS_KEY, [["2025-06-02T00:00:00", "2025-06-03T12:30:00"]])
        calls = []
        with mock.patch.object(engine, "settle_range",
                               lambda st, lo, hi: calls.append((lo, hi)) or {"written": 0}), \
             mock.patch.object(engine._coord, "admission", lambda name: (engine.ADMIT, None)), \
             mock.patch.object(engine, "_SETTLE_HISTORY_PACE_S", 0):
            R._run(engine._settle_spans_pass())
        self.assertEqual(calls, [("2025-06-02T00:00:00", "2025-06-03T00:00:00"),
                                 ("2025-06-03T00:00:00", "2025-06-03T12:30:00")])
        self.assertEqual(self.st.get_meta(engine._SETTLE_SPANS_KEY), [])

    def test_the_sweep_takes_them_once_the_history_is_walked(self):
        self.st.set_meta(engine._SETTLE_HISTORY_MARKER, {"version": engine._SETTLE_HISTORY_VERSION,
                                                        "done": True})
        self.st.set_meta(engine._SETTLE_SPANS_KEY, [["2025-06-02T00:00:00", "2025-06-02T01:00:00"]])
        seen = []

        async def _pass(max_days=100000):
            seen.append(1)
            return 0
        with mock.patch.object(engine, "_settle_spans_pass", _pass):
            R._run(engine._run_settle_history_sweep.__wrapped__()
                   if hasattr(engine._run_settle_history_sweep, "__wrapped__")
                   else engine._run_settle_history_sweep())
        self.assertEqual(seen, [1])

    def test_a_walk_from_the_start_clears_the_spans_queued_before_it(self):
        """Staging, 28 Oct 2026: a restore stored the first answer (the whole history queued), the
        sweep walked the history, and the queue was left to walk it again."""
        self.st.set_meta(engine._SETTLE_SPANS_KEY, [["2023-06-05T23:00:00", "2026-10-28T21:30:00"]])
        engine._settle_history_restart({})
        self.assertEqual(self.st.get_meta(engine._SETTLE_SPANS_KEY), [])

    def test_GUARD_a_walk_resumed_at_its_cursor_keeps_them(self):
        """Spans queued during a walk stay: a pass resuming at its cursor does not restart it."""
        self.st.set_meta(engine._SETTLE_HISTORY_MARKER, {"version": engine._SETTLE_HISTORY_VERSION,
                                                        "cursor": "2025-06-02T00:00:00"})
        self.st.set_meta(engine._SETTLE_SPANS_KEY, [["2025-06-01T00:00:00", "2025-06-01T01:00:00"]])
        with mock.patch.object(engine, "_kraken_rate_schedules", {}):
            R._run(engine._run_settle_history_sweep())          # no schedule: defers, at its cursor
        self.assertEqual(len(self.st.get_meta(engine._SETTLE_SPANS_KEY)), 1)

    def test_pending_spans_schedule_the_sweep_when_the_history_is_done(self):
        self.st.set_meta(engine._SETTLE_HISTORY_MARKER, {"version": engine._SETTLE_HISTORY_VERSION,
                                                        "done": True})
        self.st.set_meta(engine._SETTLE_SPANS_KEY, [["2025-06-02T00:00:00", "2025-06-02T01:00:00"]])
        self.assertTrue(engine._settle_spans_pending())
        src = inspect.getsource(engine._maybe_settle_history)
        self.assertIn("if _settle_history_done() and not _settle_spans_pending():", src)

    def test_the_poll_cycle_asks_after_the_rate_refresh(self):
        src = inspect.getsource(engine)
        i = src.index("            await _refresh_kraken_rate_schedules()\n            # a change of agreements")
        self.assertIn("_maybe_settle_history()", src[i:i + 300])


class TheGuard(R._Harness):

    def setUp(self):
        super().setUp()
        self._saved = engine._stored_agreements
        engine._AGREEMENT_WINDOWS.clear()
        self.st.set_kraken_state(engine._ACCOUNT_KEY, "A-1")
        self.now = datetime.utcnow().replace(microsecond=0)

    def tearDown(self):
        engine._stored_agreements = self._saved
        engine._AGREEMENT_WINDOWS.clear()
        super().tearDown()

    def _ago(self, days):
        return (self.now - timedelta(days=days)).isoformat() + "Z"

    def _disc(self, ags, mpan="M1"):
        return {"import": {"mpan": mpan, "tariff_code": ags[-1]["tariff_code"], "agreements": ags}}

    def _codes(self):
        return [r["tariff_code"] for r in self.st.get_tariff_agreements("A-1")]

    def test_an_old_agreement_dropped_is_not_taken(self):
        engine._keep_agreements(self._disc([
            {"tariff_code": AGILE, "valid_from": self._ago(700), "valid_to": self._ago(400)},
            {"tariff_code": IOG, "valid_from": self._ago(400), "valid_to": None}]))
        # an API change: only the current agreement comes back
        self.assertIsNone(engine._keep_agreements(self._disc([
            {"tariff_code": IOG, "valid_from": self._ago(400), "valid_to": None}])))
        self.assertEqual(self._codes(), [AGILE, IOG])
        engine._AGREEMENT_WINDOWS.clear()
        engine._kraken_discovery = self._disc([{"tariff_code": IOG, "valid_from": self._ago(400),
                                                "valid_to": None}])
        self.assertEqual(engine.tariff_kind_at(self._ago(500)[:-1]), "published")   # the last good copy

    def test_an_old_window_moved_is_not_taken(self):
        engine._keep_agreements(self._disc([{"tariff_code": IOG, "valid_from": self._ago(400), "valid_to": None}]))
        self.assertIsNone(engine._keep_agreements(self._disc([
            {"tariff_code": IOG, "valid_from": self._ago(399), "valid_to": None}])))

    def test_GUARD_a_cooling_off_reversal_is_taken(self):
        engine._keep_agreements(self._disc([
            {"tariff_code": IOG, "valid_from": self._ago(400), "valid_to": self._ago(10)},
            {"tariff_code": AGILE, "valid_from": self._ago(10), "valid_to": None}]))
        diff = engine._keep_agreements(self._disc([
            {"tariff_code": IOG, "valid_from": self._ago(400), "valid_to": None}]))
        self.assertIsNotNone(diff)
        self.assertEqual(self._codes(), [IOG])

    def test_GUARD_a_new_agreement_is_taken(self):
        engine._keep_agreements(self._disc([{"tariff_code": IOG, "valid_from": self._ago(400), "valid_to": None}]))
        self.assertIsNotNone(engine._keep_agreements(self._disc([
            {"tariff_code": IOG, "valid_from": self._ago(400), "valid_to": None},
            {"tariff_code": AGILE, "valid_from": self._ago(900), "valid_to": self._ago(400)}])))

    def test_GUARD_a_new_meter_points_agreements_are_taken(self):
        engine._keep_agreements(self._disc([{"tariff_code": IOG, "valid_from": self._ago(400), "valid_to": None}]))
        self.assertIsNotNone(engine._keep_agreements(self._disc(
            [{"tariff_code": IOG, "valid_from": self._ago(5), "valid_to": None}], mpan="M2")))

    def test_the_rate_stitch_reads_the_stored_copy(self):
        src = inspect.getsource(engine._refresh_kraken_rate_schedules)
        self.assertIn('_imp_info = _agreement_info("import")', src)
        self.assertIn("info = _agreement_info(channel)", inspect.getsource(engine._agreement_segs))


class TheHealthDigest(unittest.TestCase):

    def setUp(self):
        engine._AGREEMENT_WINDOWS.clear()

    def tearDown(self):
        engine._AGREEMENT_WINDOWS.clear()

    def _digest(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web"))
        import ast
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "web", "server.py"), encoding="utf-8").read()
        mod = ast.parse(src)
        ns = {}
        for n in mod.body:
            if isinstance(n, ast.FunctionDef) and n.name in ("_health_agreements", "_health_digest"):
                exec(compile(ast.Module([n], []), "server.py", "exec"), ns)
        return ns["_health_agreements"](engine)

    def test_the_stored_agreements_before_discovery(self):
        with mock.patch.object(engine, "_kraken_discovery", None), \
             mock.patch.object(engine, "_stored_agreements", {"import": {"agreements": [
                 {"tariff_code": IOG, "valid_from": "2025-01-01T00:00:00", "valid_to": None}]}}):
            stored = self._digest()
        engine._AGREEMENT_WINDOWS.clear()
        with mock.patch.object(engine, "_kraken_discovery", {"import": {"agreements": [
                 {"tariff_code": IOG, "valid_from": "2025-01-01T00:00:00Z", "valid_to": None}]}}):
            discovered = self._digest()
        self.assertIsNotNone(stored)
        self.assertEqual(stored, discovered)


if __name__ == "__main__":
    unittest.main()
