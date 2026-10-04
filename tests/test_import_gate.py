"""
The import floor and the units gate, enforced at restore (import spec §3, gates 2 and 4).

v5 carries no v4 heals; the floor is what makes that safe. Until 4 Oct 2026 nothing
read `written_by` — a restore took any v4 backup, so a database last opened by 4.5.14
arrived without the 4.5.15–4.5.19 heals, and a pence block from the 4.3.0 IOG cap bug
(whose repair v5 deleted with BL-33) arrived unrepaired.

Every test here fails on the parent tree — `import_gate` does not exist there, and the
restore paths swap in whatever they are given — except where labelled GUARD.

Checked against every database on hand (4 Oct 2026): each one stamped 4.5.19 or later
is admitted, each older or unstamped one is refused, and none is refused for units.
"""
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import import_gate as G                           # noqa: E402
import job_coordinator as C                       # noqa: E402
from block_store import BlockStore                # noqa: E402


def make_db(path, written_by=G.FLOOR_VERSION, blocks=(), vat_calendar=None):
    """A real schema, a stamp (None for none), and blocks given as
    (block_start, meter_id, imp_rate, imp_rate_exc, imp_cost, imp_cost_exc)."""
    st = BlockStore(path)
    if written_by is not None:
        st.set_meta("written_by", written_by)
    if vat_calendar is not None:
        st.set_meta("vat_calendar", vat_calendar)
    st._conn.execute("INSERT INTO config_periods (id, effective_from) VALUES (1, '2020-01-01')")
    for bs, meter, rate, rate_exc, cost, cost_exc in blocks:
        st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
            "imp_kwh, imp_rate, imp_rate_exc, imp_cost, imp_cost_exc) "
            "VALUES (?, ?, ?, 1, 1.0, ?, ?, ?, ?)",
            (bs, bs, meter, rate, rate_exc, cost, cost_exc))
    st._conn.commit()
    st.close()
    return path


def below_floor():
    """One patch release below the floor, generated so the test keeps testing something
    when the floor moves (ROADMAP §E: never a fixed below-floor version)."""
    a, b, c = G.parse_version(G.FLOOR_VERSION)
    return "%d.%d.%d" % ((a, b, c - 1) if c else (a, b - 1, 99))


BELOW = below_floor()
MAIN = "electricity_main"
HEALTHY = ("2026-09-15T12:00:00", MAIN, 0.245, 0.2333, 0.245, 0.2333)
PENCE = ("2026-09-15T12:30:00", MAIN, 24.5, 0.2333, 24.5, 0.2333)


class _Dir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def db(self, name="c.db", **kw):
        return make_db(os.path.join(self.dir, name), **kw)


class TheVersionCompare(unittest.TestCase):

    def test_numeric_not_lexical(self):
        self.assertLess(G.parse_version("4.5.9"), G.parse_version("4.5.13"))

    def test_a_dev_build_suffix_is_its_release(self):
        self.assertEqual(G.parse_version("4.5.20-a21"), (4, 5, 20))

    def test_calver_sorts_above_every_v4(self):
        self.assertGreater(G.parse_version("2026.1.0"), G.parse_version("4.99.99"))

    def test_the_generated_below_floor_version_is_below_it(self):
        self.assertLess(G.parse_version(BELOW), G.parse_version(G.FLOOR_VERSION))

    def test_garbage_is_none(self):
        self.assertIsNone(G.parse_version("unknown"))


class TheFloor(_Dir):

    def test_no_stamp_is_refused(self):
        self.assertEqual(G.check(self.db(written_by=None)), G.OLDER_THAN_STAMPS)

    def test_below_the_floor_is_refused_and_named(self):
        r = G.check(self.db(written_by=BELOW))
        self.assertIn(BELOW, r)
        self.assertIn("last v4 release", r)

    def test_the_floor_itself_and_above_are_admitted(self):
        for v in (G.FLOOR_VERSION, "4.5.20", "4.5.20-a21", "2026.1.0"):
            with self.subTest(v=v):
                self.assertIsNone(G.check(self.db(name=v + ".db", written_by=v)))

    def test_the_stamp_is_read_as_json(self):
        """store_meta values are JSON-encoded: the raw cell is '"4.5.20"'."""
        p = self.db(written_by="4.5.20")
        raw = sqlite3.connect(p).execute(
            "SELECT value FROM store_meta WHERE key='written_by'").fetchone()[0]
        self.assertEqual(raw, json.dumps("4.5.20"))
        self.assertIsNone(G.check(p))

    def test_an_unreadable_stamp_is_refused(self):
        self.assertIsNotNone(G.check(self.db(written_by="unknown")))

    def test_a_file_that_is_not_a_database_is_refused(self):
        p = os.path.join(self.dir, "x.db")
        open(p, "wb").write(b"not a database" * 100)
        self.assertTrue(G.check(p).startswith("This file could not be read"))


class TheUnits(_Dir):

    def test_healthy_blocks_are_admitted(self):
        self.assertIsNone(G.check(self.db(blocks=[HEALTHY])))

    def test_a_pence_block_is_refused_and_named(self):
        r = G.check(self.db(blocks=[HEALTHY, PENCE]))
        self.assertIn("pence", r)
        self.assertIn(PENCE[0], r)

    def test_the_cost_pair_judges_a_block_with_no_exc_rate(self):
        b = ("2026-09-15T13:00:00", MAIN, 24.5, None, 24.5, 0.2333)
        self.assertIsNotNone(G.check(self.db(blocks=[b])))

    def test_it_is_judged_against_the_vat_on_its_own_date(self):
        """A ratio of 10.4 is 9.9 at 5% (admitted) and 10.4 at 0% (refused): the
        bound is divided by 1 + VAT on the block's own UK-local day, so the seed's
        1 Oct 2026 boundary decides it."""
        at_5 = ("2026-09-15T12:00:00", MAIN, 1.04, 0.1, None, None)
        at_0 = ("2026-10-02T12:00:00", MAIN, 1.04, 0.1, None, None)
        self.assertIsNone(G.check(self.db(name="a.db", blocks=[at_5])))
        self.assertIsNotNone(G.check(self.db(name="b.db", blocks=[at_0])))

    def test_the_sources_learned_calendar_is_used(self):
        """A learned 20% from 2026-09-01 takes the same 10.4 ratio to 8.7."""
        b = ("2026-09-15T12:00:00", MAIN, 1.04, 0.1, None, None)
        self.assertIsNone(G.check(self.db(name="a.db", blocks=[b],
                                          vat_calendar=[["2026-09-01", 0.2]])))
        self.assertIsNotNone(G.check(self.db(name="b.db", blocks=[b],
                                             vat_calendar=[["2026-09-01", 0.0]])))

    def test_a_tiny_exc_figure_is_not_judged(self):
        """Rounding a 0.00004 exc rate makes the ratio meaningless, not pence."""
        b = ("2026-09-15T12:00:00", MAIN, 0.0005, 0.00004, None, None)
        self.assertIsNone(G.check(self.db(blocks=[b])))

    def test_a_block_with_no_exc_figure_is_not_judged(self):
        b = ("2026-09-15T12:00:00", MAIN, 24.5, None, 24.5, None)
        self.assertIsNone(G.check(self.db(blocks=[b])))

    def test_only_the_main_meter_is_judged(self):
        b = ("2026-09-15T12:00:00", "heat_pump", 24.5, 0.2333, 24.5, 0.2333)
        self.assertIsNone(G.check(self.db(blocks=[b])))

    def test_the_floor_is_checked_first(self):
        self.assertEqual(G.check(self.db(written_by=None, blocks=[PENCE])),
                         G.OLDER_THAN_STAMPS)


def _server():
    spec = importlib.util.spec_from_file_location("server_ig", os.path.join(_ROOT, "web", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv


class TheRestorePaths(unittest.TestCase):
    """The gate on the candidate, before anything is stopped, backed up or swapped."""

    @classmethod
    def setUpClass(cls):
        cls.srv = _server()

    def setUp(self):
        C.reset_for_tests()
        self.data = tempfile.mkdtemp()
        self.share = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.share, "backups"))
        self.live = make_db(os.path.join(self.data, "blocks.db"), written_by=G.FLOOR_VERSION,
                            blocks=[HEALTHY])
        self.live_bytes = open(self.live, "rb").read()
        self.backups, self.cancels = [], []
        eng = mock.MagicMock()
        eng.api_import_running.return_value = True          # an import is running
        eng.api_import_control.side_effect = lambda a: self.cancels.append(a)
        self.p = [mock.patch.object(self.srv, "DATA_DIR", self.data),
                  mock.patch.object(self.srv, "_share_backup_dir", lambda: self.share),
                  mock.patch.object(self.srv, "_create_backup_zip",
                                    lambda label="b": self.backups.append(label)),
                  mock.patch.object(self.srv, "_event_loop", None),
                  mock.patch.dict(sys.modules, {"engine": eng})]
        for x in self.p:
            x.start()
        self.srv._restore_job.clear()
        self.srv._restore_job["status"] = "running"

    def tearDown(self):
        for x in self.p:
            x.stop()
        shutil.rmtree(self.data, ignore_errors=True)
        shutil.rmtree(self.share, ignore_errors=True)
        C.reset_for_tests()

    def _refused_untouched(self):
        j = self.srv._restore_job
        self.assertEqual(j["status"], "error", j)
        self.assertIn("last v4 release", j["error"])
        self.assertEqual(open(self.live, "rb").read(), self.live_bytes, "the live database changed")
        self.assertEqual(self.backups, [], "a safety backup was taken for a refused restore")
        self.assertEqual(self.cancels, [], "a refused restore cancelled the running import")
        self.assertEqual([f for f in os.listdir(self.data) if f != "blocks.db"], [],
                         "a staged or extracted file was left behind")

    def _zip(self, name, db):
        z = os.path.join(self.share, "backups", name)
        with zipfile.ZipFile(z, "w") as zf:
            zf.write(db, "blocks.db")
        return name

    def test_a_below_floor_upload_is_refused(self):
        staged = make_db(os.path.join(self.data, "uploaded_restore.db"), written_by=BELOW)
        self.srv._restore_worker("", ["blocks.db"], False, staged=staged)
        self._refused_untouched()

    def test_a_below_floor_backup_zip_is_refused(self):
        src = make_db(os.path.join(self.share, "old.db"), written_by=BELOW)
        self.srv._restore_worker(self._zip("old.zip", src), None, False)
        self._refused_untouched()

    def test_a_pence_backup_zip_is_refused(self):
        src = make_db(os.path.join(self.share, "p.db"), written_by=G.FLOOR_VERSION, blocks=[PENCE])
        self.srv._restore_worker(self._zip("p.zip", src), None, False)
        self._refused_untouched()
        self.assertIn(PENCE[0], self.srv._restore_job["error"])

    def test_a_below_floor_flat_copy_is_refused(self):
        make_db(os.path.join(self.share, "blocks.db"), written_by=None)
        self.srv._restore_worker("", None, True)
        self._refused_untouched()

    def test_an_admitted_zip_is_swapped_in_from_the_checked_copy(self):
        """GUARD: the zip's blocks.db is extracted once, checked, and renamed into
        place — passes on the parent too, which copied it straight from the zip."""
        src = make_db(os.path.join(self.share, "new.db"), written_by=G.FLOOR_VERSION,
                      blocks=[HEALTHY, ("2026-09-16T00:00:00", MAIN, 0.1, 0.095, 0.1, 0.095)])
        mock.patch.object(sys.modules["engine"], "api_import_running", lambda: False).start()
        self.srv._restore_worker(self._zip("new.zip", src), None, False)
        self.assertEqual(self.srv._restore_job["status"], "done", self.srv._restore_job)
        n = sqlite3.connect(self.live).execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
        self.assertEqual(n, 2)
        self.assertFalse(os.path.exists(os.path.join(self.data, "restore_candidate.db")))

    def test_from_zip_refuses_below_the_floor(self):
        src = make_db(os.path.join(self.share, "old.db"), written_by=BELOW)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.write(src, "blocks.db")
        buf.seek(0)
        r = self.srv.app.test_client().post("/api/import/from-zip",
                                            data={"zipfile": (buf, "old.zip")},
                                            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        self.assertIn(BELOW, r.get_json()["error"])
        self.assertEqual(open(self.live, "rb").read(), self.live_bytes)
        self.assertFalse(os.path.exists(os.path.join(self.data, "blocks.db.import_tmp")))


if __name__ == "__main__":
    unittest.main()
