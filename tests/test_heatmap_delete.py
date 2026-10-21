"""
Delete… on the heatmap (soft delete design §3, PR 4; 20 Oct 2026).

The selection's whole half-hours — every meter, both channels — are deleted after the required
backup, by the delete job as before, over a [from, to) UTC span in place of local dates and times.
The preview says it is not one channel, names the devices, says HA's readings are kept (a refill
rebuilds the devices from them, PR 3), and names Re-fetch where it can help. Offered on the cell,
the day and the drag, on every view.

Discriminating (fail on the parent: none of it exists), except the GUARDs marked.
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

A, B = "2026-10-01T10:00:00", "2026-10-01T11:00:00"
STARTS = ("2026-10-01T09:30:00", "2026-10-01T10:00:00", "2026-10-01T10:30:00",
          "2026-10-01T11:00:00")


def _st():
    st = _store()
    for bs in STARTS:
        end = bs[:11] + "%02d:%02d:00" % divmod(int(bs[11:13]) * 60 + int(bs[14:16]) + 30, 60)
        st.append_block({"start": bs, "end": end, "meters": {
            "electricity_main": {"meta": {}, "channels": {"import": {
                "kwh": 1.0, "read_start": 100.0, "read_end": 101.0}}},
            "ev_charger": {"meta": {"sub_meter": True}, "channels": {"import": {
                "kwh": 0.4, "read_start": 5.0, "read_end": 5.4}}}}},
            ha_meters=["electricity_main", "ev_charger"])
    return st


def _starts(st):
    return [r[0] for r in st._conn.execute("SELECT DISTINCT block_start FROM blocks "
                                           "ORDER BY block_start")]


class TheStore(unittest.TestCase):

    def test_the_preview_counts_half_hours_devices_and_hass_reads(self):
        p = _st().delete_span_preview(A, B)
        self.assertEqual((p["half_hours"], p["rows"], p["devices"], p["ha_reads"]),
                         (2, 4, ["ev_charger"], 4))

    def test_it_deletes_exactly_the_span_every_meter(self):
        st = _st()
        r = st.delete_blocks_for_date_range("", "", tz_name="UTC", utc_span=(A, B))
        self.assertEqual(r["deleted"], 4)
        self.assertEqual(_starts(st), [STARTS[0], STARTS[3]])

    def test_the_tombstone_is_the_span(self):
        st = _st()
        st.delete_blocks_for_date_range("", "", tz_name="UTC", utc_span=(A, B))
        self.assertEqual([tuple(r) for r in st._conn.execute(
            "SELECT start_utc, end_utc FROM deleted_ranges")], [(A, B)])

    def test_the_count_agrees_with_the_delete(self):
        st = _st()
        n = st.count_blocks_for_date_range("", "", tz_name="UTC", utc_span=(A, B))["blocks"]
        self.assertEqual(n, st.delete_blocks_for_date_range("", "", tz_name="UTC",
                                                            utc_span=(A, B))["deleted"])

    def test_hass_reads_stay(self):
        """GUARD of PR 2 through this path (fails on the parent only by the span argument)."""
        st = _st()
        st.delete_blocks_for_date_range("", "", tz_name="UTC", utc_span=(A, B))
        self.assertEqual(st._conn.execute("SELECT COUNT(*) FROM ha_reads").fetchone()[0], 8)

    def test_a_backwards_span_is_refused(self):
        with self.assertRaises(ValueError):
            _st().delete_blocks_for_date_range("", "", tz_name="UTC", utc_span=(B, A))


class TheRoutes(unittest.TestCase):

    def setUp(self):
        from tests.test_server import make_client, server
        self.server = server
        self.st = _st()
        self.client = make_client(store=self.st)
        eng = sys.modules["engine"]
        self.p = [mock.patch.object(server.store_access, "store", lambda: self.st),
                  mock.patch.object(server, "_own_store", lambda: self.st),
                  mock.patch.object(server, "_stop_running_import", lambda *a, **k: True),
                  mock.patch.object(server, "_safety_backup", lambda label: "%s.zip" % label),
                  mock.patch.object(eng, "delete_in_progress", create=True, return_value=False),
                  mock.patch.object(eng, "poll_in_progress", create=True, return_value=False),
                  mock.patch.object(eng, "set_delete_active", create=True)]
        for x in self.p:
            x.start()
        server._delete_job.clear()
        server._delete_job["status"] = "idle"

    def tearDown(self):
        for x in self.p:
            x.stop()
        self.server._delete_job.clear()
        self.server._delete_job["status"] = "idle"

    def _post(self, url, body):
        r = self.client.post(url, data=json.dumps(body), content_type="application/json")
        return r.status_code, json.loads(r.data)

    def test_the_preview(self):
        code, d = self._post("/api/blocks/delete/preview", {"from_utc": A, "to_utc": B})
        self.assertEqual((code, d["half_hours"], d["devices"]), (200, 2, ["ev_charger"]))

    def test_unconfirmed_is_refused(self):
        """GUARD (the parent refuses it too, for the missing dates)."""
        self.assertEqual(self._post("/api/blocks/delete", {"from_utc": A, "to_utc": B})[0], 400)

    def test_it_runs_as_the_delete_job_after_its_backup(self):
        class Inline:      # the worker inline: an in-memory store is one thread's
            def __init__(self, target, args=(), kwargs=None, **k):
                self.f, self.a, self.k = target, args, kwargs or {}

            def start(self):
                self.f(*self.a, **self.k)
        with mock.patch.object(self.server.threading, "Thread", Inline):
            code, _ = self._post("/api/blocks/delete", {"from_utc": A, "to_utc": B,
                                                        "confirmed": True})
        self.assertEqual(code, 200)
        for _ in range(50):
            if self.server._delete_job.get("status") != "running":
                break
            time.sleep(0.05)
        j = self.server._delete_job
        self.assertEqual((j["status"], j["kind"], j["backup"]), ("done", "delete", "pre-delete.zip"))
        self.assertEqual(_starts(self.st), [STARTS[0], STARTS[3]])


class TheHeatmap(unittest.TestCase):
    _SRC = open(os.path.join(_ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8").read()

    def _fn(self, name):
        i = self._SRC.index("function " + name + "(")
        return self._SRC[i:self._SRC.index("\n  }\n", i)]

    def test_on_the_cell_the_day_and_the_drag_on_every_view(self):
        self.assertEqual(self._SRC.count("items.push(deleteItem("), 3)
        self.assertNotIn("state.metric", self._fn("deleteItem"))

    def test_the_warning(self):
        body = self._fn("openDelete")
        self.assertIn("postJSON('/api/blocks/delete/preview', {from_utc: a, to_utc: b})", body)
        self.assertIn("Deletes everything in these half-hours, not only the ' + chan + '.", body)
        self.assertIn("var chan = state.metric === 'export' ? 'export' : 'import';", body)
        self.assertIn("HA\\u2019s own readings are kept", body)
        self.assertIn("state.canRefetch && p.from_utc >= twoYears", body)

    def test_delete_then_redraw(self):
        body = self._fn("openDelete")
        # 21 Oct 2026: with the choice of only the imported half-hours (data management §1 (6))
        self.assertIn("postJSON('/api/blocks/delete', {from_utc: p.from_utc, to_utc: p.to_utc, confirmed: true,",
                      body)
        self.assertIn("reloadRange(p.from_utc, p.to_utc);", body)


if __name__ == "__main__":
    unittest.main()
