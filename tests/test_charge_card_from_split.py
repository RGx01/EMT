"""
The Smart Charging card before EMT's dispatch record starts (7 Nov 2026, the user's support file).

Octopus returns only recent dispatches, so an install built from the API holds none for the nights
before its first poll. Octopus's split of the bill goes back further, so `_ev_outside_runs` read every
one of those nights as "Charged outside a smart charge", in red, and the 6-hour bar counted only the
half-hours on record (≈1h 13m on a cap-day that really had about 4h 18m). The user: "can we not work
out from the data in the octopus split what actually occurred without the dispatches?" Yes: on their
25 Aug - 28 Oct record, 476 half-hours in both, none with EV in the split alone, and six with a
dispatch alone, each 0.08 kWh or less.

So before the record's first half-hour the split gives the charges, and from it on the record decides.
And the face's "Also charged" line belongs to the charge it sits under: it showed a run from three
days before.

Discriminating: the tests not marked GUARD fail on the parent.
"""
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
PAGE = open(os.path.join(_ROOT, "web", "templates", "live_power.html"), encoding="utf-8").read()
OFF, PEAK = 0.0523, 0.3077
FULL = 3.373        # kWh in a saturated half-hour, 6.746 kW


def _server():
    spec = importlib.util.spec_from_file_location("server_split", os.path.join(_ROOT, "web", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv


class TheRows(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv = _server()

    def test_before_the_record_the_split_reads_as_completed_half_hours(self):
        rows = self.srv._split_charge_rows({"2026-11-04T19:00:00": [(1.8, OFF), (0.2, OFF)]},
                                           "2026-11-05T04:00:00")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["slot_start"], r["kind"], r["raw_start"], r["raw_end"]),
                         ("2026-11-04T19:00:00", "completed", "2026-11-04T19:00:00", "2026-11-04T19:30:00"))
        self.assertAlmostEqual(r["energy_kwh"], 2.0)

    def test_from_the_records_first_half_hour_the_record_decides(self):
        ev = {"2026-11-05T03:30:00": [(1.0, OFF)], "2026-11-05T04:00:00": [(1.0, OFF)],
              "2026-11-05T12:00:00": [(1.0, PEAK)]}
        self.assertEqual([r["slot_start"] for r in self.srv._split_charge_rows(ev, "2026-11-05T04:00:00")],
                         ["2026-11-05T03:30:00"])

    def test_GUARD_no_record_at_all_makes_no_charges(self):
        self.assertEqual(self.srv._split_charge_rows({"2026-11-04T19:00:00": [(1.0, OFF)]}, None), [])


class TheRoute(unittest.TestCase):
    """The real route over a real database, with the clock's night two days back."""

    def setUp(self):
        import block_store
        import store_access
        self.srv = _server()
        self.dir = tempfile.mkdtemp()
        self.store = block_store.BlockStore(os.path.join(self.dir, "blocks.db"))
        self.store_access = store_access
        self.conn = self.store._conn
        now = datetime.now(timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)
        self.night = (now - timedelta(days=3)).replace(hour=0)

    def tearDown(self):
        self.store._conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _at(self, hours):
        return (self.night + timedelta(hours=hours)).isoformat()

    def _ev(self, hours, kwh=FULL, rate=OFF):
        k = self._at(hours)
        self.conn.execute("INSERT INTO block_segments (block_start, meter_id, channel, seq, kwh, inc_rate, band, "
                          "attribution) VALUES (?, 'electricity_main', 'import', 0, ?, ?, 'off_peak', 'ev')",
                          (k, kwh, rate))

    def _completed(self, hours, kwh=FULL):
        k = self._at(hours)
        end = (self.night + timedelta(hours=hours + 0.5)).isoformat()
        self.conn.execute("INSERT INTO dispatch_history (slot_start, kind, provider, source, energy_kwh, first_seen, "
                          "last_seen, raw_start, raw_end) VALUES (?, 'completed', 'P', 'unknown', ?, ?, ?, ?, ?)",
                          (k, -kwh, k, k, k, end))

    def _get(self, days=7):
        self.conn.commit()
        fake_engine = mock.MagicMock()
        fake_engine.cap_excluded.return_value = False
        fake_engine.capped_at.return_value = True
        with mock.patch.dict(sys.modules, {"engine": fake_engine}), \
                mock.patch.object(self.store_access, "store", return_value=self.store), \
                mock.patch.object(self.srv, "load_config", return_value={"meters": {"electricity_main": {
                    "meta": {"timezone": "UTC"}}}}):
            r = self.srv.app.test_client().get("/api/charge-sessions?days=%d" % days)
        self.assertEqual(r.status_code, 200, r.data[:300])
        return json.loads(r.data)

    def test_nights_before_the_record_are_charges_not_outside(self):
        for h in (0, 0.5, 1):
            self._ev(h)
        self._ev(24); self._completed(24)         # the record starts the next night
        d = self._get()
        self.assertEqual(d["outside"], [])
        self.assertEqual([(s["start"], s["kwh"]) for s in d["sessions"]][-1], (self._at(0), round(FULL * 3, 3)))

    def test_the_first_cap_day_counts_the_half_hours_before_the_record(self):
        # one noon-to-noon day: 4 half-hours before the record, 2 on it; 6 full half-hours = 3 hours
        for h in (0, 0.5, 1, 1.5, 2, 2.5):
            self._ev(h)
        self._completed(2); self._completed(2.5)
        d = self._get()
        self.assertEqual(d["outside"], [])
        self.assertEqual(len(d["sessions"]), 1)
        self.assertEqual(d["sessions"][0]["n_slots"], 6)
        self.assertEqual(d["cap_days"][d["sessions"][0]["cap_day"]]["used_minutes"], 180)

    def test_a_charge_before_the_record_keeps_its_cost_and_peak(self):
        self._ev(0, 2.0, PEAK); self._ev(0.5, 2.0, OFF)
        self._ev(24); self._completed(24)
        s = self._get()["sessions"][-1]
        self.assertAlmostEqual(s["cost"], 2 * PEAK + 2 * OFF, places=3)
        self.assertEqual(s["at_peak"]["kwh"], 2.0)

    def test_GUARD_on_the_record_ev_with_no_dispatch_is_still_outside(self):
        self._completed(0); self._ev(0)
        self._ev(10, 1.0)                         # after the record's start, no dispatch
        d = self._get()
        self.assertEqual([o["start"] for o in d["outside"]], [self._at(10)])

    def test_GUARD_no_dispatches_means_no_card(self):
        self._ev(0)
        self.assertFalse(self._get()["has_data"])


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheFace(unittest.TestCase):

    def _pick(self, sessions, outside):
        i = PAGE.index("function _faceOutside(")
        js = PAGE[i:PAGE.index("\n}\n", i) + 2] + "\nconsole.log(JSON.stringify(_faceOutside(%s, %s)));" % (
            json.dumps(sessions), json.dumps(outside))
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
        try:
            r = subprocess.run(["node", fh.name], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(fh.name)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_a_run_from_another_cap_day_is_not_on_the_face(self):
        self.assertIsNone(self._pick([{"cap_day": "2026-11-06"}], [{"cap_day": "2026-11-04", "start": "a"}]))

    def test_the_newest_run_of_its_own_cap_day_is(self):
        out = [{"cap_day": "2026-11-06", "start": "a"}, {"cap_day": "2026-11-06", "start": "b"},
               {"cap_day": "2026-11-07", "start": "c"}]
        self.assertEqual(self._pick([{"cap_day": "2026-11-06"}], out)["start"], "b")

    def test_GUARD_no_charge_no_line(self):
        self.assertIsNone(self._pick([], [{"cap_day": "2026-11-04", "start": "a"}]))

    def test_the_page_uses_it(self):
        self.assertIn("_faceOutside(_chargeSessions, _chargeOutside)", PAGE)
        self.assertNotIn("_chargeOutside[_chargeOutside.length-1]", PAGE)


if __name__ == "__main__":
    unittest.main()
