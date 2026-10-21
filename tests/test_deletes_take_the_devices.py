"""
A delete of imported history takes whole half-hours, the devices included (soft delete design §3;
21 Oct 2026, staging).

Two things had drifted from the design:
- *Delete all imported history* (the purge) removed only the imported meter rows.
- *Delete… only the imported half-hours* kept the devices' rows "for a re-import to rejoin".

Either way the devices' rows were left with no meter beside them, and plotted and costed: the day's
bill is max(0, meter − Σdevices) + Σdevices, which with no meter is the devices alone. A staging purge
left 59,526 device half-hours from Jun 2024 to Mar 2026. HA's reads stay in `ha_reads` — every one of
those half-hours had its hour there — and an import that brings the meter back rebuilds the devices
from them. The gap fill and the CSV import already did; the Octopus import now does too.

Discriminating: every test here fails on the parent but the GUARDs.
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from tests.test_device_sensors_storage import _store      # noqa: E402

IMP = ("2025-01-10T08:00:00", "2025-01-10T08:30:00")      # imported meter half-hours
LIVE = "2026-10-20T08:00:00"                               # a live one


def _st():
    st = _store()
    with st._conn:
        for bs in IMP:
            st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                             "imp_kwh, source) VALUES (?, ?, 'electricity_main', 1, 1.0, 'imported_api')", (bs, bs))
        # the device in the imported half-hours: Device History's row, and a live one (a gap filled
        # under a day the devices had recorded live)
        st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                         "imp_kwh, imp_cost, source) VALUES (?, ?, 'ev_charger', 1, 0.4, 0.1, "
                         "'recorder_attributed')", (IMP[0], IMP[0]))
        st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                         "imp_kwh, imp_cost) VALUES (?, ?, 'ev_charger', 1, 0.3, 0.1)", (IMP[1], IMP[1]))
        st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                         "imp_kwh) VALUES (?, ?, 'electricity_main', 1, 2.0)", (LIVE, LIVE))
        st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                         "imp_kwh) VALUES (?, ?, 'ev_charger', 1, 0.5)", (LIVE, LIVE))
        st._conn.execute("INSERT INTO ha_reads (meter_id, channel, origin, period_start, minutes, kwh, "
                         "recorded_at) VALUES ('ev_charger', 'import', 'recorder', '2025-01-10T08:00:00', "
                         "60, 0.7, '2025-01-11T00:00:00')")
    return st


def _rows(st):
    return sorted((r[0], r[1]) for r in st._conn.execute("SELECT block_start, meter_id FROM blocks"))


class ThePurge(unittest.TestCase):

    def test_the_imported_half_hours_go_whole(self):
        st = _st()
        r = st.purge_imported_history()
        self.assertEqual(_rows(st), [(LIVE, "electricity_main"), (LIVE, "ev_charger")])
        self.assertEqual((r["blocks"], r["device_rows"]), (2, 2))    # the live-sourced one counted too

    def test_has_reads_are_kept(self):
        """GUARD: the purge never touched ha_reads; what the rebuild reads from."""
        st = _st()
        st.purge_imported_history()
        self.assertEqual(st._conn.execute("SELECT COUNT(*) FROM ha_reads").fetchone()[0], 1)

    def test_no_device_is_left_without_its_meter(self):
        st = _st()
        st.purge_imported_history()
        n = st._conn.execute(
            "SELECT COUNT(*) FROM blocks d WHERE d.meter_id = 'ev_charger' AND NOT EXISTS "
            "(SELECT 1 FROM blocks m WHERE m.block_start = d.block_start AND m.meter_id = 'electricity_main')"
        ).fetchone()[0]
        self.assertEqual(n, 0)


class TheOctopusImport(unittest.TestCase):

    def test_rebuilds_the_devices_after_writing(self):
        with open(os.path.join(_ROOT, "engine.py"), encoding="utf-8") as f:
            src = f.read()
        fn = src[src.index("async def run_api_import_job("):]
        fn = fn[:fn.index("\nasync def ", 10)]
        wrote = fn[fn.index("_wrote = _run_wrote_blocks(j)"):fn.index('j["verify_skipped"] = "no_blocks_written"')]
        self.assertIn("rebuild_devices_after_refill(", wrote)
        self.assertLess(wrote.index("if _wrote:"), wrote.index("rebuild_devices_after_refill("))


class ThePages(unittest.TestCase):

    def _t(self, name):
        with open(os.path.join(_ROOT, "web", "templates", name), encoding="utf-8") as f:
            return f.read()

    def test_the_delete_choice_says_the_devices_go(self):
        hm = self._t("heatmap_module.html")
        self.assertNotIn("for a re-import to rejoin", hm)
        self.assertIn("with the devices\\u2019 data in them", hm)

    def test_the_purge_says_so(self):
        self.assertIn("a re-import rebuilds the devices from them", self._t("data_management.html"))

    def test_the_backups_fit_a_row(self):
        dm = self._t("data_management.html")
        self.assertIn(".dm-modal { max-width: 1040px;", dm)


if __name__ == "__main__":
    unittest.main()
