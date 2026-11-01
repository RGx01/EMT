"""
BL-99 (1 Nov 2026) — the Overview's smart charging card (IOG overview card design).

The user: "we need to see how much of the 6hr allowance was used, how long the session was, we
need to see bump charges / charges outside of a dispatch". The card was one run-on line of eight
facts, with "saved" in place of a cost and nothing of the allowance or why a part was at peak.

- `/api/charge-sessions` adds each session's `cost` (the EV's share of each half-hour — Octopus's
  where billed — or the dispatch kWh at the block's rate until it is), `billed`, `cap_day` and
  `at_peak` {kWh, £, reason}; `cap_days` (EMT's estimate of each noon→noon day's charged minutes,
  `iog_cap.cap_usage`); and `outside` (the EV's charging in half-hours with no completed dispatch).
- "At peak" is by price — over the day's cheapest import rate — not by the stored band: a daytime
  dispatch at the off-peak price carries a `peak` band on the staging copy (30 Aug 2026). The
  reason, first that holds: a bump charge, over the 6 hours (a capped half-hour from its cap-day's
  boundary on), else none named.
- The face: the day and window with kWh and £; the allowance (≈, the user) on a capped agreement,
  else ≈ the charging time; the off-peak or peak line. "saved" is gone (the user). The modal groups
  by cap-day, and a gap a session bridged is drawn as one.

Checked in the local preview on 1 Nov 2026, on a scratch copy of staging (on IOG-SMB since 25 Aug
2026): 28 Oct's 23.02 kWh at £1.20, not yet billed; the face uncapped there (the preview loads no
agreements, so `capped_at` is false), and capped with a cap-day fed in memory; the modal by cap-day.

Discriminating: every test fails on the parent tree.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
PAGE = open(os.path.join(_ROOT, "web", "templates", "live_power.html"), encoding="utf-8").read()


def _server():
    spec = importlib.util.spec_from_file_location("server_bl99", os.path.join(_ROOT, "web", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv


OFF, PEAK = 0.05493, 0.30771
DAY = {"2026-10-28": OFF}


def _session(start, slots):
    return {"start": start, "fill": [{"slot": k, "kwh": q} for k, q in slots]}


class TheCosts(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv = _server()

    def _run(self, sessions, ev, sources=None, cap=None, capped=lambda k: True, bill=()):
        return self.srv._charge_session_costs(sessions, ev, DAY, {}, sources or {}, cap or {}, capped,
                                              set(bill), lambda k: "2026-10-27")

    def test_the_cost_is_the_evs_share(self):
        s = self._run([_session("2026-10-28T01:30:00", [("2026-10-28T01:30:00", 3.0), ("2026-10-28T02:00:00", 2.0)])],
                      {"2026-10-28T01:30:00": [(3.0, OFF)], "2026-10-28T02:00:00": [(2.0, OFF)]},
                      bill=("2026-10-28T01:30:00", "2026-10-28T02:00:00"))[0]
        self.assertAlmostEqual(s["cost"], 5.0 * OFF, places=4)
        self.assertTrue(s["billed"])
        self.assertIsNone(s["at_peak"])
        self.assertEqual(s["cap_day"], "2026-10-27")

    def test_until_billed_the_dispatch_kwh_at_the_blocks_rate(self):
        srv = self.srv
        s = srv._charge_session_costs([_session("2026-10-28T01:30:00", [("2026-10-28T01:30:00", 3.0)])], {},
                                      DAY, {"2026-10-28T01:30:00": OFF}, {}, {}, lambda k: True, set(),
                                      lambda k: "2026-10-27")[0]
        self.assertAlmostEqual(s["cost"], 3.0 * OFF, places=4)
        self.assertFalse(s["billed"])

    def test_at_peak_by_price_not_band(self):
        # an off-peak price is not at peak whatever its band says; a peak price is
        s = self._run([_session("2026-10-28T03:00:00", [("2026-10-28T03:00:00", 2.0), ("2026-10-28T03:30:00", 1.0)])],
                      {"2026-10-28T03:00:00": [(2.0, OFF)], "2026-10-28T03:30:00": [(1.0, PEAK)]})[0]
        self.assertEqual((s["at_peak"]["kwh"], round(s["at_peak"]["cost"], 4)), (1.0, round(PEAK, 4)))

    def test_a_bump_first(self):
        s = self._run([_session("2026-10-28T03:00:00", [("2026-10-28T03:00:00", 1.0)])],
                      {"2026-10-28T03:00:00": [(1.0, PEAK)]}, sources={"2026-10-28T03:00:00": "bump-charge"},
                      cap={"2026-10-27": {"over": True, "boundary_slot": "2026-10-28T02:00:00"}})[0]
        self.assertEqual(s["at_peak"]["reason"], "bump")

    def test_over_the_cap_from_its_boundary(self):
        cap = {"2026-10-27": {"over": True, "boundary_slot": "2026-10-28T03:00:00"}}
        s = self._run([_session("2026-10-28T03:00:00", [("2026-10-28T03:00:00", 1.0)])],
                      {"2026-10-28T03:00:00": [(1.0, PEAK)]}, cap=cap)[0]
        self.assertEqual(s["at_peak"]["reason"], "over_cap")
        s = self._run([_session("2026-10-28T03:00:00", [("2026-10-28T03:00:00", 1.0)])],
                      {"2026-10-28T03:00:00": [(1.0, PEAK)]}, cap=cap, capped=lambda k: False)[0]
        self.assertEqual(s["at_peak"]["reason"], "peak")             # uncapped: no cap to be over

    def test_charging_outside_a_dispatch(self):
        ev = {"2026-10-28T16:00:00": [(1.0, PEAK)], "2026-10-28T16:30:00": [(1.1, PEAK)],
              "2026-10-28T01:30:00": [(3.0, OFF)]}
        runs = self.srv._ev_outside_runs(ev, {"2026-10-28T01:30:00"}, DAY)
        self.assertEqual(len(runs), 1)
        self.assertEqual((runs[0]["start"], runs[0]["end"], runs[0]["kwh"], runs[0]["peak_kwh"]),
                         ("2026-10-28T16:00:00", "2026-10-28T17:00:00", 2.1, 2.1))


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheFace(unittest.TestCase):

    def _run(self, expr, caps):
        names = ("_statusPill", "_fmtDur", "_chargeTime", "_estChargeTime", "_dayName", "_money", "_costText",
                 "_capDayLabel", "_capBar", "_capLine", "_peakText", "_fillChartSVG", "_chargeSummaryHtml")
        def fn(n):
            i = PAGE.index("function " + n + "(")
            return PAGE[i:PAGE.index("\n}\n", i) + 2]
        js = ("var _chargeCurrency = '£', _chargeCapDays = %s, _PEAK_WHY = {bump:'a bump charge', over_cap:'over the 6 hours'};\n"
              "function _cesc(s){return String(s);}\n" % json.dumps(caps)) + "\n".join(fn(n) for n in names) + \
             "\nconsole.log(JSON.stringify(" + expr + "));"
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    S = {"status": "completed", "start": "2026-10-28T01:30:00", "kwh": 23.02, "cost": 1.2043, "billed": False,
         "cap_day": "2026-10-27", "est_charge_minutes": 200, "dispatch_minutes": 239,
         "at_peak": {"kwh": 1.4, "cost": 0.43, "reason": "over_cap"},
         "local": {"start": {"date": "2026-10-28", "time": "01:30"}, "end": {"date": "2026-10-28", "time": "05:59"}},
         "fill": [{"slot": "2026-10-28T01:30:00", "kwh": 3, "off_peak": True},
                  {"slot": "2026-10-28T03:00:00", "kwh": 3, "off_peak": True}]}

    def test_capped_the_allowance_and_the_reason(self):
        out = self._run("_chargeSummaryHtml(%s)" % json.dumps(self.S),
                        {"2026-10-27": {"used_minutes": 385, "cap_minutes": 360, "over": True, "capped": True}})
        self.assertIn("23.0 kWh", out)
        self.assertIn("£1.20", out)
        self.assertIn("est.", out)
        self.assertIn("≈6h 25m of 6h", out)
        self.assertIn("over by ≈25m", out)
        self.assertIn("1.4 kWh at peak, £0.43 — over the 6 hours", out)
        self.assertNotIn("saved", out)

    def test_uncapped_the_charging_time(self):
        out = self._run("_chargeSummaryHtml(%s)" % json.dumps(dict(self.S, at_peak=None)), {})
        self.assertIn("≈3h 20m charging", out)
        self.assertNotIn("of 6h", out)
        self.assertIn("All off-peak", out)

    def test_a_bridged_gap_is_drawn(self):
        out = self._run("_fillChartSVG(%s)" % json.dumps(self.S), {})
        self.assertIn(">…</text>", out)


class TheModal(unittest.TestCase):

    def test_grouped_by_cap_day(self):
        i = PAGE.index("function _renderChargeModalBody(")
        f = PAGE[i:PAGE.index("\n}\n", i)]
        self.assertIn("(days[s.cap_day]||(days[s.cap_day]=[])).push", f)
        self.assertIn("_chargeOutside.forEach", f)


if __name__ == "__main__":
    unittest.main()
