"""
The 6-hour cap leaves out a bump and a peak-billed charge (1 Nov 2026, the user: "bumped charges and peak rate
costed charges shouldn't count toward 6hr limit").

The cap's count (BlockStore.cap_day_boundary, for settle() and the day chart; the Overview card's own count)
left out a bump only by its dispatch source, `bump-charge` / `boost`. Octopus no longer sends one: on the user's
account every planned dispatch reads `smart-charge` and every completed one `unknown`. On 1 Nov 2026 11:00–12:00
UTC a completed charge with no plan or start, while EMT was recording, was priced at peak by EMT's own rule — and
still counted about an hour of the 6. A charge the bill priced at peak counted too.

Now `engine.cap_excluded` leaves out a completed half-hour that is a bump by its source; whose bill priced the
EV's bucket at peak; or, with no EV bucket billed, that is outside the guaranteed window with the dispatch
lifecycle's verdict peak — the rule `predicted_band` prices it by. All three users of the count pass it.

The cap-day here is shaped on the user's (support file, 1 Nov 2026), with made-up energies and a cap of 2 hours so
the bump decides it: night smart charges 7.7 kWh, then a bump of 6.8 kWh at 11:00–12:00.

Discriminating: every test fails on the parent tree but test_a_smart_charge_still_counts, a GUARD.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine
import iog_cap
from block_store import BlockStore

TZ = "Europe/London"
DAY = "2026-09-10"            # a cap-day well past the reconcile's settle window


class _Sched:
    """The guaranteed window 23:30–05:30 local (22:30–04:30 UTC in September)."""
    def is_off_peak(self, ts):
        hm = ts[11:16]
        return hm >= "22:30" or hm < "04:30"


class TheCap(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = BlockStore(self.tmp.name)
        self._saved = (engine._store, dict(engine._kraken_rate_schedules), iog_cap.CAP_HOURS, engine._band_rates)
        engine._store = self.store
        engine._kraken_rate_schedules.clear()
        engine._kraken_rate_schedules["import"] = _Sched()
        engine._band_rates = lambda bs: (0.07, 0.30)
        iog_cap.CAP_HOURS = 2.0

    def tearDown(self):
        engine._store, sched, iog_cap.CAP_HOURS, engine._band_rates = self._saved
        engine._kraken_rate_schedules.clear()
        engine._kraken_rate_schedules.update(sched)
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def _row(self, slot, kind, energy=None, source=None):
        h, m = divmod(int(slot[11:13]) * 60 + int(slot[14:16]) + 30, 60)
        end = slot[:11] + "%02d:%02d:00" % (h % 24, m)
        with self.store._conn:
            self.store._conn.execute(
                "INSERT INTO dispatch_history (slot_start, kind, provider, source, energy_kwh, first_seen, "
                "last_seen, raw_start, raw_end) VALUES (?,?,'T',?,?,?,?,?,?)",
                (slot, kind, source, None if energy is None else -energy, slot, slot,
                 slot if kind == "completed" else None, end if kind == "completed" else None))

    def _smart(self, slot, energy):
        self._row(slot, "planned", 3.4, "smart-charge")
        self._row(slot, "started", None, "smart-charge")
        self._row(slot, "completed", energy, "unknown")

    def _night(self):
        nxt = "2026-09-11"
        for t, e in (("02:00", 3.2), ("02:30", 0.1), ("03:00", 3.2), ("03:30", 0.3), ("04:30", 0.9)):
            self._smart(nxt + "T" + t + ":00", e)

    def _bump(self):
        for t, e in (("10:00", 3.3), ("10:30", 3.5)):          # 11:00–12:00 BST: no plan, no start
            self._row("2026-09-11T" + t + ":00", "completed", e, "unknown")

    def _boundary(self):
        return engine._iog_cap_day_boundary("2026-09-11T03:00:00", TZ, self.store)

    def test_a_bump_octopus_labels_unknown_does_not_count(self):
        self._night()
        self._bump()
        self.assertTrue(engine.cap_excluded("2026-09-11T10:00:00", self.store))
        self.assertIsNone((self._boundary() or (None,))[0])    # 7.7 kWh at 6.4 kW: 1.2 h, under 2

    def test_a_smart_charge_still_counts(self):
        # GUARD: passes on the parent tree too — the night's charges count as they did
        self._night()
        self._smart("2026-09-11T10:00:00", 3.3)
        self._smart("2026-09-11T10:30:00", 3.5)
        self.assertEqual(self._boundary()[0], "2026-09-11T10:30:00")

    def test_a_charge_billed_at_peak_does_not_count(self):
        self._night()
        self._smart("2026-09-11T10:00:00", 3.3)
        self._smart("2026-09-11T10:30:00", 3.5)
        for s in ("2026-09-11T10:00:00", "2026-09-11T10:30:00"):
            self.store.upsert_measured_breakdown(s, home_kwh=0.1, home_rate=0.30, ev_kwh=3.3, ev_rate=0.30)
        self.assertTrue(engine.cap_excluded("2026-09-11T10:30:00", self.store))
        self.assertIsNone((self._boundary() or (None,))[0])

    def test_a_charge_billed_off_peak_counts_even_if_unplanned(self):
        self._night()
        self._bump()
        for s in ("2026-09-11T10:00:00", "2026-09-11T10:30:00"):
            self.store.upsert_measured_breakdown(s, home_kwh=0.1, home_rate=0.30, ev_kwh=3.3, ev_rate=0.07)
        self.assertFalse(engine.cap_excluded("2026-09-11T10:00:00", self.store))   # the bill decides
        self.assertEqual(self._boundary()[0], "2026-09-11T10:30:00")


class TheUsers(unittest.TestCase):

    def test_the_day_chart_and_the_overview_card_use_it(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        charts = open(os.path.join(root, "energy_charts.py"), encoding="utf-8").read()
        server = open(os.path.join(root, "web", "server.py"), encoding="utf-8").read()
        self.assertIn("exclude=lambda s: _eng_oc.cap_excluded(s, store))", charts)
        self.assertIn("and not _eng.cap_excluded(r[\"slot_start\"], store)], tz_name, None)", server)


if __name__ == "__main__":
    unittest.main()
