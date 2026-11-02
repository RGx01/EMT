"""
Delete… can take only the imported half-hours (data management design §1 (6), PR 4; 21 Oct 2026).

Where a selection holds both imported (CSV, PDF bills, the API import) and live half-hours, the
heatmap's delete preview offers *Everything* or *Only the imported half-hours (N of M)* — the Delete
Blocks page's *reconstructed only*, the way to roll back a bad import over part of the history.
The store already took `utc_span` and `reconstructed_only` together; the preview now counts the
imported ones and the dialog sends the choice. The imported half-hours go whole — the meter's rows
and the devices' in them (soft delete design §3, 21 Oct 2026: the devices' rows had been kept "for a
re-import to rejoin", and plotted and costed with no meter beside them). HA's reads stay in
`ha_reads`, and a refill rebuilds the devices from them. Live and settled half-hours stay whole.

Discriminating (fail on the parent), except the GUARDs marked.
"""
import json
import os
import sys
import time
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from tests.test_device_sensors_storage import _store      # noqa: E402

A, B = "2026-03-02T23:00:00", "2026-03-03T00:30:00"
# 23:00 and 23:30 imported on the main meter, 00:00 live; a device row on every one
ROWS = (("2026-03-02T23:00:00", "imported_api"), ("2026-03-02T23:30:00", "imported_csv"),
        ("2026-03-03T00:00:00", None))


def _st():
    st = _store()
    for bs, src in ROWS:
        st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                         "imp_kwh, source) VALUES (?, ?, 'electricity_main', 1, 1.0, ?)", (bs, bs, src))
        st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                         "imp_kwh, source) VALUES (?, ?, 'ev_charger', 1, 0.4, ?)",
                         (bs, bs, "recorder_attributed" if src else None))
    st._conn.commit()
    return st


def _rows(st):
    return sorted((r[0], r[1]) for r in st._conn.execute("SELECT block_start, meter_id FROM blocks"))


class TheStore(unittest.TestCase):

    def test_the_preview_counts_the_imported(self):
        p = _st().delete_span_preview(A, "2026-03-03T01:00:00")
        # the imported rows: the meter's two and the device's two in those half-hours
        self.assertEqual((p["half_hours"], p["imported_half_hours"], p["imported_rows"]), (3, 2, 4))

    def test_the_imported_half_hours_go_whole(self):
        """The devices' rows in the imported half-hours go with the meter's; the live half-hour
        stays whole. Fails on the parent, which left the device's two."""
        st = _st()
        r = st.delete_blocks_for_date_range("", "", tz_name="UTC", reconstructed_only=True,
                                            utc_span=(A, "2026-03-03T01:00:00"))
        self.assertEqual(r["deleted"], 4)
        self.assertEqual(_rows(st), [("2026-03-03T00:00:00", "electricity_main"),
                                     ("2026-03-03T00:00:00", "ev_charger")])

    def test_everything_takes_every_row(self):
        """GUARD: the default, as before (fails on the parent only by the preview's new counts)."""
        st = _st()
        st.delete_blocks_for_date_range("", "", tz_name="UTC", utc_span=(A, "2026-03-03T01:00:00"))
        self.assertEqual(_rows(st), [])
        self.assertIn("imported_half_hours", st.delete_span_preview(A, "2026-03-03T01:00:00"))


class TheRoute(unittest.TestCase):

    def test_the_choice_reaches_the_delete(self):
        from tests.test_server import make_client, server
        st = _st()
        client = make_client(store=st)
        eng = sys.modules["engine"]

        class Inline:
            def __init__(self, target, args=(), kwargs=None, **k):
                self.f, self.a, self.k = target, args, kwargs or {}

            def start(self):
                self.f(*self.a, **self.k)
        p = [mock.patch.object(server.store_access, "store", lambda: st),
             mock.patch.object(server, "_own_store", lambda: st),
             mock.patch.object(server, "_stop_running_import", lambda *a, **k: True),
             mock.patch.object(server, "_safety_backup", lambda label: "%s.zip" % label),
             mock.patch.object(eng, "delete_in_progress", create=True, return_value=False),
             mock.patch.object(eng, "poll_in_progress", create=True, return_value=False),
             mock.patch.object(eng, "set_delete_active", create=True),
             mock.patch.object(server.threading, "Thread", Inline)]
        for x in p:
            x.start()
        try:
            server._delete_job.clear(); server._delete_job["status"] = "idle"
            r = client.post("/api/blocks/delete", content_type="application/json", data=json.dumps(
                {"from_utc": A, "to_utc": "2026-03-03T01:00:00", "confirmed": True,
                 "reconstructed_only": True}))
            self.assertEqual(r.status_code, 200)
            for _ in range(50):
                if server._delete_job.get("status") != "running":
                    break
                time.sleep(0.05)
            self.assertEqual(server._delete_job["result"]["deleted"], 4)   # the meter's 2, the device's 2
            pv = json.loads(client.post("/api/blocks/delete/preview", content_type="application/json",
                                        data=json.dumps({"from_utc": A, "to_utc": "2026-03-03T01:00:00"})).data)
            self.assertEqual(pv["imported_half_hours"], 0)
        finally:
            for x in p:
                x.stop()
            server._delete_job.clear(); server._delete_job["status"] = "idle"


class TheDialog(unittest.TestCase):
    _SRC = open(os.path.join(_ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8").read()

    def _fn(self, name):
        i = self._SRC.index("function " + name + "(")
        return self._SRC[i:self._SRC.index("\n  }\n", i)]

    def test_offered_where_a_selection_holds_both(self):
        body = self._fn("openDelete")
        self.assertIn("var both = p.imported_half_hours > 0 && p.imported_half_hours < p.half_hours;", body)
        self.assertIn("'<b>Only the imported half-hours</b> \\u2014 ' + p.imported_half_hours + ' of ' + p.half_hours", body)

    def test_the_choice_is_sent(self):
        body = self._fn("openDelete")
        self.assertIn("go(p, !!(pick && pick.value === 'imported'));", body)
        self.assertIn("reconstructed_only: importedOnly}", body)

    def test_its_labels_read_as_text(self):
        """Clicked through: base.html styles every label as an upper-case caption."""
        self.assertIn(".hm-choice label { text-transform:none;", self._SRC)


if __name__ == "__main__":
    unittest.main()
