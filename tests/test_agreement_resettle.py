"""
G1d: a change of agreements re-settles the history it moves (G1 design §4, 28 Oct 2026).

A changed agreement rebuilt the rate schedules, but nothing re-settled the history it covered, and the
health strip's month counts read discovery's agreements only — none before it answered. Now:

- the stored answer's diff gives spans — each added or superseded agreement's window, a moved one both,
  clipped to the history and merged; the first answer stored is the whole history;
- they are queued in store_meta, and the settle sweep's job works them off a day at a time once the
  history walk is done, after the rate refresh that builds the new agreements' rates;
- the health strip's agreements digest reads the stored agreements too, and the same for both forms.

Discriminating: every test here fails on the parent, but those labelled GUARD.
"""
import inspect
import os
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
