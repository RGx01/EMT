"""
An imported half-hour with no export figure is a gap, not a quiet half-hour (21 Oct 2026, staging).

After an Octopus import, three days of March 2025 had import and no export at all. The Export bill
read them `none`, grey, as nothing exported. With the meter exporting, they are `no_data`: red,
hatched yellow, an issue. The span the imported-export delete cleared (soft delete design §4) keeps
reading `none`, as decided: the delete records it (store_meta `export_deleted`).

And pages are asked for again each time (`Cache-Control: no-cache`): their script is inline, so a
cached page after an upgrade was stale code, which needed a hard refresh.

Discriminating: every test here fails on the parent but the GUARDs.
"""
import json
import os
import sys
import unittest
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import settlement_state as ss                  # noqa: E402

NOW = datetime(2026, 10, 21, 12, 0)
BS = "2025-03-05T12:00:00"


def _f(**kw):
    f = {"block_start": BS, "imp_kwh": 0.4, "imp_cost": 0.1, "source": "imported_api",
         "exp_kwh": None, "exp_kwh_api": None}
    f.update(kw)
    return f


def _export(f, **ctx):
    ctx.setdefault("export_channel", True)
    return ss.classify(f, smb=False, now=NOW, **ctx)["export"]


class TheExportState(unittest.TestCase):

    def test_no_figure_is_a_gap(self):
        self.assertEqual(_export(_f()), "no_data")

    def test_a_csv_import_with_no_export_too(self):
        self.assertEqual(_export(_f(source="imported_csv")), "no_data")

    def test_a_deleted_export_stays_grey(self):
        self.assertEqual(_export(_f(), export_deleted=[("2025-03-05T00:00:00", "2025-03-06T00:00:00")]),
                         "none")

    def test_nothing_exported_is_still_none(self):
        """GUARD: a figure of 0 is a quiet half-hour."""
        self.assertEqual(_export(_f(exp_kwh=0.0)), "none")

    def test_no_export_channel_is_not_a_gap(self):
        """GUARD: an import-only meter has no export to miss."""
        self.assertEqual(_export(_f(), export_channel=False), "none")


class TheDelete(unittest.TestCase):

    def test_records_the_span_it_cleared(self):
        from block_store import BlockStore
        st = BlockStore(":memory:")
        st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, "
                         "timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
        for d in ("2025-03-05", "2025-03-06"):
            st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                             "imp_kwh, exp_kwh, source) VALUES (?, ?, 'electricity_main', 1, 0.4, 1.0, "
                             "'imported_csv')", (d + "T12:00:00", d + "T12:30:00"))
        st._conn.commit()
        st.delete_imported_export("2025-03-05", "2025-03-05", "UTC")
        st.delete_imported_export("2025-03-06", "2025-03-06", "UTC")
        self.assertEqual(st.get_meta("export_deleted"),
                         [["2025-03-05T00:00:00", "2025-03-07T00:00:00"]])       # merged


class TheRoutes(unittest.TestCase):

    def test_the_states_take_the_deleted_spans(self):
        with open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8") as f:
            src = f.read()
        fn = src[src.index("def _settlement_states(store, utc_start, utc_end)"):]
        fn = fn[:fn.index("\n\n\n")]
        self.assertIn("export_deleted=_export_deleted(store)", fn)

    def test_pages_revalidate(self):
        from tests import test_server as TS
        client = TS.make_client()
        r = client.get("/data-management")
        self.assertEqual(r.headers.get("Cache-Control"), "no-cache")

    def test_a_page_that_sets_its_own_keeps_it(self):
        """GUARD: the Lovelace pages send no-store, as before."""
        from tests import test_server as TS
        r = TS.make_client().get("/lovelace/heatmap")
        self.assertEqual(r.headers.get("Cache-Control"), "no-cache, no-store, must-revalidate")


class TheKeptCounts(unittest.TestCase):

    def test_are_dropped_for_the_new_rule(self):
        """Counts kept on disk by the old rule would hide the gaps until the month's data moved."""
        from tests import test_server as TS
        self.assertGreaterEqual(TS.server._HEALTH_FILE_V, 2)


class TheHeatmap(unittest.TestCase):

    def test_no_data_is_an_issue_and_says_so(self):
        with open(os.path.join(_ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8") as f:
            hm = f.read()
        self.assertIn("export: {waiting: 1, overdue: 1, no_data: 1, missing: 1},", hm)
        self.assertIn("no_data: 'no export figure from Octopus or the CSV", hm)
        self.assertIn("if (v === 'abandoned' || v === 'no_data') return hatchAbandoned();", hm)


if __name__ == "__main__":
    unittest.main()
