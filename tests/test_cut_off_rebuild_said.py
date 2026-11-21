"""
A device history rebuild cut off is said, and undo covers it (interrupted rebuild design §3, Phase 1;
21 Nov 2026).

On the owner's soak Pi a *Replace history* cut off by a restart was finished exactly by *Rebuild
history* — but nothing said it had been cut off: after a restart in its re-split the card showed no
rebuilt history, after one in its writing a partial span, both reading as finished. And the run was
entered in the ledger only when it ended, so undo by run missed what a cut-off run wrote.

Now the run is entered when it starts (`running`, its kind and step), its progress goes in every 200
hours, and it is closed `done` / `cancelled` / `error` — a part-done replace `interrupted`, its
history being dropped. At startup a `running` run with no run of its device alive is marked
`interrupted`; the runs API names each device's, and the card says "… was cut off … Rebuild history
finishes it". A later run of the device that finishes marks it `resumed`.

The figures are made up. DISCRIMINATING: every test fails on the parent but
test_GUARD_a_run_that_wrote_nothing_leaves_no_entry, which pins the old behaviour kept;
test_GUARD_a_run_alive_in_this_process_is_left_alone fails there only because
`mark_cut_off_rebuilds` does not exist.
"""
import asyncio
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import engine                                       # noqa: E402
from tests.test_replace_device_history import RIGHT, HALF, _rows   # noqa: E402
from tests.test_restore_stops_rebuild import _Base   # noqa: E402

PAGE = open(os.path.join(_ROOT, "web", "templates", "meter_config.html"), encoding="utf-8").read()


def _runs(st, meter="ev_charger"):
    return [r for r in st.get_attribution_runs() if r.get("meter_id") == meter]


class TheLedgerEntry(_Base):

    def _rebuild(self, during=None):
        real, seen = engine._attribute_hour, []

        def hour(*a, **kw):
            if not seen and during:
                seen.append(during())
            return real(*a, **kw)
        self.assertTrue(engine.attribution_claim("ev_charger", [RIGHT]))
        with mock.patch.object(engine, "_attribute_hour", hour):
            return asyncio.run(engine.run_attribution_job("ev_charger", [RIGHT], fetch=False)), seen

    def test_it_is_entered_when_the_run_starts(self):
        _, seen = self._rebuild(during=lambda: [dict(r) for r in _runs(self.st)])
        self.assertEqual(len(seen[0]), 1, "no ledger entry while the run was writing")
        r = seen[0][0]
        self.assertEqual((r["status"], r["kind"], r["step"]), ("running", "rebuild", "writing"))
        self.assertTrue(r["from"] and r["to"])

    def test_it_is_closed_done_with_what_it_wrote(self):
        res, _ = self._rebuild()
        [r] = _runs(self.st)
        self.assertEqual((r["status"], r["blocks_written"]), ("done", res["written"]))
        self.assertEqual(r["blocks_written"], 4)

    def test_GUARD_a_run_that_wrote_nothing_leaves_no_entry(self):
        self._rebuild()
        engine._attribution_jobs.clear()
        self._rebuild()                                     # everything already there
        self.assertEqual(len(_runs(self.st)), 1)


class AtStartup(_Base):

    def _left_running(self, **kw):
        r = dict({"run_id": "cut|ev_charger", "meter_id": "ev_charger", "status": "running",
                  "kind": "replace", "step": "writing", "blocks_written": 2,
                  "done_to": "2026-10-01T10:00:00", "created_at": "2026-11-21T15:14:52"}, **kw)
        self.st.record_attribution_run(r)

    def test_a_run_left_running_is_marked_interrupted_and_said(self):
        self._left_running()
        with self.assertLogs("engine", level="WARNING") as logs:
            marked = engine.mark_cut_off_rebuilds()
        self.assertEqual([m["run_id"] for m in marked], ["cut|ev_charger"])
        self.assertEqual(_runs(self.st)[0]["status"], "interrupted")
        self.assertTrue(any("was cut off" in m and "Rebuild history" in m for m in logs.output))

    def test_GUARD_a_run_alive_in_this_process_is_left_alone(self):
        """Startup also runs on a config save or an HA reconnect, mid-run."""
        self._left_running()
        engine._attribution_jobs["ev_charger"] = {"device": "ev_charger", "status": "running"}
        self.assertEqual(engine.mark_cut_off_rebuilds(), [])
        self.assertEqual(_runs(self.st)[0]["status"], "running")

    def test_a_later_run_that_finishes_resumes_it(self):
        self._left_running(status="interrupted")
        self.assertTrue(engine.attribution_claim("ev_charger", [RIGHT]))
        asyncio.run(engine.run_attribution_job("ev_charger", [RIGHT], fetch=False))
        st = {r["run_id"]: r["status"] for r in _runs(self.st)}
        self.assertEqual(st["cut|ev_charger"], "resumed")


class AReplace(_Base):

    def _with_history(self):
        with self.st._conn:
            for s in HALF:
                self.st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, "
                                      "config_period_id, imp_kwh, source) VALUES (?, ?, 'ev_charger', "
                                      "1, 0.5, 'recorder_attributed')", (s, HALF[s]))
        self.st.record_attribution_run({"run_id": "old", "meter_id": "ev_charger", "blocks_written": 4})

    def test_it_is_entered_before_its_drop(self):
        self._with_history()
        real, seen = self.st.drop_device_history, []

        def drop(*a, **kw):
            seen.append([dict(r) for r in _runs(self.st) if r["run_id"] != "old"])
            return real(*a, **kw)
        self.assertTrue(engine.attribution_claim("ev_charger", [RIGHT]))
        with mock.patch.object(self.st, "drop_device_history", drop):
            asyncio.run(engine.replace_device_history("ev_charger", [RIGHT]))
        self.assertEqual([(r["status"], r["kind"], r["step"]) for r in seen[0]],
                         [("running", "replace", "dropping")])
        [r] = _runs(self.st)                                # the old run left the ledger, its own kept
        self.assertEqual((r["kind"], r["status"], r["blocks_written"]), ("replace", "done", 4))

    def test_one_stopped_after_its_drop_is_interrupted(self):
        self._with_history()
        real = self.st.drop_device_history

        def drop(*a, **kw):
            out = real(*a, **kw)
            engine.attribution_control("cancel", "ev_charger")
            return out
        self.assertTrue(engine.attribution_claim("ev_charger", [RIGHT]))
        with mock.patch.object(self.st, "drop_device_history", drop):
            asyncio.run(engine.replace_device_history("ev_charger", [RIGHT]))
        self.assertEqual(_rows(self.st), {})
        mine = [r for r in _runs(self.st) if r.get("kind") == "replace"]
        self.assertEqual([(r["status"], r["step"]) for r in mine], [("interrupted", "re-splitting")])


class TheCard(unittest.TestCase):

    def test_it_shows_a_cut_off_run_when_nothing_is_running(self):
        i = PAGE.index("function _renderHistoryState(")
        body = PAGE[i:PAGE.index("\n}\n", i)]
        self.assertIn("_hist.cutOff[m]", body)
        self.assertLess(body.index("st === 'error'"), body.index("_hist.cutOff[m]"))
        self.assertIn("_hist.cutOff = (res[1] || {}).cut_off || {};", PAGE)

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_what_it_says(self):
        i = PAGE.index("function _histCutOffText(")
        fn = PAGE[i:PAGE.index("\n}\n", i) + 2]
        js = ("var esc=function(x){return x};" + fn +
              "console.log(_histCutOffText({kind:'replace',blocks_written:0,step:'re-splitting'}));"
              "console.log(_histCutOffText({kind:'rebuild',blocks_written:7030,done_to:'2024-11-05T10:00:00'}));")
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True).stdout.splitlines()
        self.assertIn("Replacing this device’s history was cut off before it wrote anything", out[0])
        self.assertIn("Rebuild history", out[0])
        self.assertIn("cut off at 2024-11-05, 7,030 half-hours in.", out[1])


if __name__ == "__main__":
    unittest.main()
