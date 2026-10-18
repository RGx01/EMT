"""
A bill applied moves the fingerprints, and the heatmap re-asks every drawn month (18 Oct 2026, the Pi).

The history drain applied August's bills about five minutes after a restart, and no chart or
heatmap redrew. Two reasons. The fingerprints — the page's global change token
(`server._blocks_data_version`) and the per-day ones (`BlockStore.day_fingerprints`) — sum costs to
the penny: a bill that agrees with EMT's estimate moves `imp_cost` by less, and what it does change
(`rate_source` -> 'settled', a `measured_cost` row) was in neither. And the heatmap's `refresh()`
re-fetched the current month only, so an older month never learnt even when a token moved.

Now both fingerprints count the settled / corrected blocks and the cached bills, and `refresh()`
asks each drawn month's per-day fingerprints (`fields=days`) and re-fetches the ones that moved.

Discriminating (fail on the parent): all but test_an_unchanged_store_keeps_its_fingerprints (a
guard).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from block_store import BlockStore               # noqa: E402

S = "2026-08-09T02:00:00"
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _store():
    st = BlockStore(":memory:")
    st._conn.execute("INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, "
                     "block_minutes, timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
    st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
                     "imp_kwh_api, imp_rate, imp_cost, rate_source, rate_corrected) "
                     "VALUES (?,?,?,1,0.4,0.4,0.25,0.1,'schedule',0)", (S, S, "electricity_main"))
    st._conn.commit()
    return st


def _day(st):
    return st.day_fingerprints("2026-08-09T00:00:00", "2026-08-10T00:00:00")


def _token(st):
    sys.path.insert(0, os.path.join(_ROOT, "web"))
    from tests.test_server import server
    return server._blocks_data_version(st)


class TheDayFingerprints(unittest.TestCase):

    def test_a_bill_applied_at_the_same_cost_moves_it(self):
        st = _store(); before = _day(st)
        st._conn.execute("UPDATE blocks SET rate_source = 'settled', imp_cost = 0.100001 "
                         "WHERE block_start = ?", (S,))
        self.assertNotEqual(_day(st), before)

    def test_a_bill_cached_moves_it(self):
        st = _store(); before = _day(st)
        st.upsert_measured_cost(S, mpan="m", cost_incl=0.1, cost_excl=0.1, label="OFF_PEAK", kwh=0.4)
        self.assertNotEqual(_day(st), before)

    def test_a_correction_moves_it(self):
        st = _store(); before = _day(st)
        st._conn.execute("UPDATE blocks SET rate_corrected = 1 WHERE block_start = ?", (S,))
        self.assertNotEqual(_day(st), before)

    def test_an_unchanged_store_keeps_its_fingerprints(self):
        """GUARD."""
        st = _store()
        self.assertEqual(_day(st), _day(st))


class TheChangeToken(unittest.TestCase):

    def test_a_bill_applied_at_the_same_cost_moves_it(self):
        st = _store(); before = _token(st)
        st._conn.execute("UPDATE blocks SET rate_source = 'settled' WHERE block_start = ?", (S,))
        self.assertNotEqual(_token(st), before)

    def test_a_bill_cached_moves_it(self):
        st = _store(); before = _token(st)
        st.upsert_measured_cost(S, mpan="m", cost_incl=0.1, cost_excl=0.1, label="OFF_PEAK", kwh=0.4)
        self.assertNotEqual(_token(st), before)


class TheHeatmap(unittest.TestCase):

    def test_refresh_asks_every_drawn_month(self):
        src = open(os.path.join(_ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8").read()
        i = src.index("  function refresh() {")
        body = src[i:src.index("\n  }\n", i)]
        self.assertIn("state.keys.slice(1).forEach", body)
        self.assertIn("/api/energy?fields=days&resolution=block&kind=month", body)
        self.assertIn("if (!(state.panels[o] && state.panels[o].drawn)) { delete state.months[o]; return; }", body)


if __name__ == "__main__":
    unittest.main()
