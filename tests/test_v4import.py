"""
The importer, architecture step 7 (E design; import spec §5, §6, §9). 10 Oct 2026.

Until this change a restore swapped a v4 database in as it was. Every heal marker and sweep
cursor v4 had written came with it, so v5's own passes over that history were suppressed,
and nothing checked the swapped file against its source. Now a v4-shaped database is
imported. A fresh v5 database is filled from the attached source in one transaction.
Every table, column and key must have a rule. Spec §9's gates must hold before it commits,
and the restore swaps in the result. A v5 backup (it carries emt_schema) is swapped in as
it is.

Every test here fails on the parent tree, where `v4import` does not exist and a restore
carries every key, except those labelled GUARD. Checked by hand against seven real
databases (blocks-20 to blocks-24, PianSom, dev v4 011026): each imports with every gate
holding.
"""
import ast
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

import v4import                                   # noqa: E402
from v4import import gate as G, rules             # noqa: E402
import job_coordinator as C                       # noqa: E402
from block_store import BlockStore, EMT_SCHEMA, read_emt_schema   # noqa: E402

MAIN = "electricity_main"
B1, B2 = "2026-09-15T12:00:00", "2026-09-15T12:30:00"


def v4_db(path, *, written_by=G.FLOOR_VERSION, meta=None, state=None, extra_sql=()):
    """A v4-shaped database: v5's schema with v5's marker removed, two priced main blocks
    (one corrected), segments, a measured row and a ZERO one, a tombstone, a mix row
    keyed to a block id, an in-flight current_block, and the given keys."""
    st = BlockStore(path)
    c = st._conn
    c.execute("DELETE FROM store_meta WHERE key = 'emt_schema'")
    if written_by is not None:
        st.set_meta("written_by", written_by)
    for k, v in (meta or {}).items():
        st.set_meta(k, v)
    for k, v in (state or {}).items():
        st.set_kraken_state(k, v)
    c.execute("INSERT INTO config_periods (id, effective_from) VALUES (1, '2020-01-01')")
    c.execute("INSERT INTO blocks (id, block_start, block_end, meter_id, config_period_id, "
              "imp_kwh, imp_rate, imp_rate_exc, imp_cost, imp_cost_exc, rate_source) "
              "VALUES (40, ?, ?, ?, 1, 2.0, 0.245, 0.2333, 0.49, 0.4667, 'measured')",
              (B1, B2, MAIN))
    c.execute("INSERT INTO blocks (id, block_start, block_end, meter_id, config_period_id, "
              "imp_kwh, imp_rate, imp_rate_exc, imp_cost, imp_cost_exc, rate_source, "
              "rate_corrected) VALUES (41, ?, ?, ?, 1, 1.0, 0.07, 0.0667, 0.07, 0.0667, "
              "'corrected', 1)", (B2, "2026-09-15T13:00:00", MAIN))
    c.execute("INSERT INTO block_segments (block_start, meter_id, channel, seq, kwh, inc_rate, "
              "exc_rate, band, attribution) VALUES (?, ?, 'import', 0, 2.0, 0.245, 0.2333, "
              "'day', 'house')", (B1, MAIN))
    c.execute("INSERT INTO measured_cost (slot_start, mpan, direction, cost_incl, cost_excl, "
              "label, kwh) VALUES (?, 'M1', 'CONSUMPTION', 0.49, 0.4667, 'STANDARD_RATE', 2.0)",
              (B1,))
    c.execute("INSERT INTO measured_cost (slot_start, mpan, direction, cost_incl, cost_excl, "
              "label, kwh) VALUES (?, 'M1', 'CONSUMPTION', 0, 0, 'ZERO', 0)", (B2,))
    c.execute("INSERT INTO deleted_ranges (meter_id, start_utc, end_utc, created_at, reason) "
              "VALUES ('*', '2026-08-01T00:00:00', '2026-08-02T00:00:00', '2026-08-03', "
              "'user_delete')")
    c.execute("INSERT INTO generation_mix (block_id, fuel, perc) VALUES (40, 'wind', 41.5)")
    c.execute("INSERT INTO current_block (id, block_start, block_end) VALUES (1, ?, ?)",
              (B2, "2026-09-15T13:00:00"))
    for sql in extra_sql:
        c.execute(sql)
    c.commit()
    st.close()
    return path


class _Dir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.target = os.path.join(self.dir, "import_target.db")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def src(self, **kw):
        return v4_db(os.path.join(self.dir, "src.db"), **kw)

    def imported(self, **kw):
        report = v4import.import_v4(self.src(**kw), self.target)
        conn = sqlite3.connect(self.target)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn, report

    def meta(self, conn, key, table="store_meta"):
        row = conn.execute("SELECT value FROM %s WHERE key = ?" % table, (key,)).fetchone()
        return None if row is None else row[0]


class TheCarry(_Dir):

    def test_every_carried_table_holds_the_sources_rows(self):
        conn, report = self.imported()
        for t in ("blocks", "block_segments", "measured_cost", "deleted_ranges",
                  "generation_mix", "config_periods"):
            with self.subTest(t=t):
                self.assertEqual(report["tables"][t]["out"],
                                 conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0])
        self.assertEqual(report["tables"]["blocks"], {"in": 2, "out": 2})
        self.assertTrue(all(g["ok"] for g in report["gates"].values()), report["gates"])

    def test_row_ids_are_kept_so_references_still_hold(self):
        conn, _ = self.imported()
        row = conn.execute("SELECT b.block_start FROM generation_mix g JOIN blocks b "
                           "ON b.id = g.block_id").fetchone()
        self.assertEqual(row[0], B1)

    def test_a_zero_measured_row_is_carried(self):
        """Spec §5.1: 'the bill says nothing was drawn' is data, not an empty row."""
        conn, _ = self.imported()
        self.assertEqual(conn.execute("SELECT label FROM measured_cost WHERE slot_start = ?",
                                      (B2,)).fetchone()[0], "ZERO")

    def test_in_flight_state_is_not_carried(self):
        conn, _ = self.imported()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM current_block").fetchone()[0], 0)

    def test_the_source_is_never_written(self):
        src = self.src()
        before = (open(src, "rb").read(), os.path.getmtime(src))
        v4import.import_v4(src, self.target)
        self.assertEqual((open(src, "rb").read(), os.path.getmtime(src)), before)

    def test_the_target_is_one_self_contained_file(self):
        self.imported()
        self.assertFalse(os.path.exists(self.target + "-wal"))
        self.assertEqual(sqlite3.connect(self.target).execute(
            "PRAGMA journal_mode").fetchone()[0], "delete")

    def test_importing_twice_gives_the_same_rows(self):
        """GUARD (by construction: the target is rebuilt from nothing each time)."""
        conn, _ = self.imported()
        first = [tuple(r) for r in conn.execute("SELECT * FROM blocks ORDER BY id")]
        v4import.import_v4(os.path.join(self.dir, "src.db"), self.target)
        again = sqlite3.connect(self.target)
        self.assertEqual([tuple(r) for r in again.execute("SELECT * FROM blocks ORDER BY id")],
                         first)
        again.close()


class TheProvenanceWords(_Dir):
    """Transform 1 (spec §5.2, BL-61(5)): v4's 'measured' is v5's 'settled', and a block a
    reconciliation flagged while leaving 'schedule' says 'reconciled'. Every meter."""

    ROWS = (  # id, meter, rate_source, rate_reconciled, rate_corrected
        (50, MAIN, "schedule", 1, 0),          # flagged, word left behind  -> reconciled
        (51, MAIN, None, 1, 0),                # flagged, no word           -> reconciled
        (52, MAIN, "measured", 1, 0),          # settled outranks the flag  -> settled
        (53, MAIN, "corrected", 1, 1),         # corrected outranks it      -> corrected
        (54, MAIN, "schedule", 0, 0),          # untouched
        (55, "ev_charger", "schedule", 1, 0),  # a sub-meter row            -> reconciled
    )

    def _src(self):
        sql = tuple(
            "INSERT INTO blocks (id, block_start, block_end, meter_id, config_period_id, "
            "imp_kwh, rate_source, rate_reconciled, rate_corrected) VALUES "
            "(%d, '2026-09-1%dT00:00:00', '2026-09-1%dT00:30:00', '%s', 1, 1.0, %s, %d, %d)"
            % (i, i - 50, i - 50, m, "NULL" if w is None else "'%s'" % w, f, c)
            for i, m, w, f, c in self.ROWS)
        return dict(extra_sql=sql)

    def test_each_word_follows_the_rule(self):
        conn, _ = self.imported(**self._src())
        got = dict(conn.execute("SELECT id, rate_source FROM blocks WHERE id >= 50"))
        self.assertEqual(got, {50: "reconciled", 51: "reconciled", 52: "settled",
                               53: "corrected", 54: "schedule", 55: "reconciled"})

    def test_no_measured_word_survives(self):
        conn, _ = self.imported(**self._src())
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM blocks "
                                      "WHERE rate_source = 'measured'").fetchone()[0], 0)

    def test_the_report_counts_them(self):
        _, report = self.imported(**self._src())
        # the fixture's block 40 is 'measured' too
        self.assertEqual(report["transforms"]["provenance"],
                         {"settled": 2, "reconciled_folded": 3})

    def test_v5_reads_an_imported_settled_block_as_settled(self):
        self.imported()
        st = BlockStore(self.target)
        self.addCleanup(st.close)
        self.assertEqual([r[0] for r in st.measured_main_costs_since("2026-01-01")], [B1])


class TheKeys(_Dir):
    """E design §7: setup facts and user choices cross; claims that work already ran do
    not, because v5 carries no v4 heals and its own passes have not run."""

    KW = dict(
        meta={"vat_calendar": [["2023-06-02", 0.05]], "settings": {"bill_rounding_summary": True},
              "reprice_history_state": {"done": True}, "db_uuid": "abc123",
              "region_probe_clobber_done": "2026-08-05T09:29:51",
              "bill_resettle_v4_done": {"done": True}},
        state={"kraken_account_number": "A-1234", "data_source_mode": "api",
               "billing_source": "dcc", "smb_rate_repair_done_v2": "1",
               "last_poll_utc": "2026-09-15T12:00:00", "api_import_done_import": "1",
               "measured_vat_resync": "{}"})

    def test_heal_markers_and_sweep_cursors_do_not_cross(self):
        conn, _ = self.imported(**self.KW)
        for k, t in (("reprice_history_state", "store_meta"),
                     ("bill_resettle_v4_done", "store_meta"),
                     ("smb_rate_repair_done_v2", "kraken_state"),
                     ("last_poll_utc", "kraken_state"),
                     ("measured_vat_resync", "kraken_state")):
            with self.subTest(k=k):
                self.assertIsNone(self.meta(conn, k, t))

    def test_setup_facts_and_choices_cross_unchanged(self):
        conn, _ = self.imported(**self.KW)
        self.assertEqual(json.loads(self.meta(conn, "vat_calendar")), [["2023-06-02", 0.05]])
        self.assertEqual(json.loads(self.meta(conn, "settings")), {"bill_rounding_summary": True})
        for k, v in (("kraken_account_number", "A-1234"), ("data_source_mode", "api"),
                     ("billing_source", "dcc"), ("api_import_done_import", "1")):
            with self.subTest(k=k):
                self.assertEqual(self.meta(conn, k, "kraken_state"), v)

    def test_the_region_overwrite_marker_crosses(self):
        """Re-run on an import, v5's one-time region overwrite would replace the carried
        regions from the API: its marker is a setup fact in effect."""
        conn, _ = self.imported(**self.KW)
        self.assertIsNotNone(self.meta(conn, "region_probe_clobber_done"))

    def test_written_by_and_db_uuid_are_left_for_v5_to_stamp(self):
        conn, report = self.imported(**self.KW)
        self.assertIsNone(self.meta(conn, "written_by"))
        self.assertIsNone(self.meta(conn, "db_uuid"))
        self.assertEqual(report["source"]["written_by"], G.FLOOR_VERSION)   # recorded instead
        self.assertEqual(report["source"]["db_uuid"], "abc123")

    def test_an_unseen_completion_marker_is_dropped(self):
        conn, report = self.imported(state={"some_future_heal_done_v3": "1"})
        self.assertIsNone(self.meta(conn, "some_future_heal_done_v3", "kraken_state"))
        self.assertIn("some_future_heal_done_v3", report["keys"]["kraken_state"]["dropped"])


class TheMarkerAndTheAudit(_Dir):

    def test_the_import_is_marked_a_v5_database(self):
        self.imported()
        self.assertEqual(read_emt_schema(self.target), EMT_SCHEMA)

    def test_the_audit_record_is_stored(self):
        conn, report = self.imported(meta={"supplier": "octopus"})
        audit = json.loads(self.meta(conn, "import_audit"))
        self.assertEqual(audit["source"]["written_by"], G.FLOOR_VERSION)
        self.assertEqual(audit["source"]["supplier"], "octopus")
        self.assertFalse(audit["source"]["supplier_inferred"])
        self.assertEqual(audit["tables"]["blocks"]["out"], 2)
        self.assertTrue(audit["gates"]["cost_parity"]["ok"])

    def test_a_database_v5_creates_is_marked(self):
        p = os.path.join(self.dir, "fresh.db")
        BlockStore(p).close()
        self.assertEqual(read_emt_schema(p), EMT_SCHEMA)

    def test_a_v4_database_opened_by_v5_is_not_marked(self):
        """A v4-shaped live database (a v5 dev install) must still import on a restore."""
        p = self.src()
        BlockStore(p).close()
        self.assertIsNone(read_emt_schema(p))


class NothingPassesSilently(_Dir):
    """E design §3: a table, column or key with no rule aborts the import and is named."""

    def _refused(self, says, **kw):
        with self.assertRaises(v4import.ImportFailed) as cm:
            v4import.import_v4(self.src(**kw), self.target)
        self.assertIn(says, str(cm.exception))
        self.assertFalse(os.path.exists(self.target), "a refused import left its target")

    def test_an_unknown_table(self):
        self._refused("table mystery", extra_sql=("CREATE TABLE mystery (x)",))

    def test_an_unknown_column(self):
        self._refused("column blocks.mystery_col",
                      extra_sql=("ALTER TABLE blocks ADD COLUMN mystery_col REAL",))

    def test_an_unknown_key(self):
        self._refused("kraken_state key mystery_cursor", state={"mystery_cursor": "x"})

    def test_every_key_on_hand_has_a_rule(self):
        """GUARD: the keys in E design §7's table, in the table each lives in."""
        for t in rules.KEYED_TABLES:
            for k in rules.CARRY_KEYS[t] | rules.DROP_KEYS[t]:
                self.assertIsNotNone(rules.key_rule(t, k))


class TheGatesBite(_Dir):
    """Spec §9: a carry that does not reconcile does not commit. Each case breaks the
    target after the carry, where a faulty rule would."""

    def _broken(self, sql, says):
        real = v4import.transforms.run

        def _run(conn, report):
            real(conn, report)
            conn.execute(sql)
        with mock.patch.object(v4import.transforms, "run", _run):
            with self.assertRaises(v4import.ImportFailed) as cm:
                v4import.import_v4(self.src(), self.target)
        self.assertIn(says, str(cm.exception))
        self.assertFalse(os.path.exists(self.target))

    def test_a_cost_that_moved(self):
        self._broken("UPDATE main.blocks SET imp_cost = imp_cost + 0.000001 WHERE id = 40",
                     "block cost(s) changed")

    def test_a_lost_row(self):
        self._broken("DELETE FROM main.measured_cost WHERE label = 'ZERO'", "row counts")

    def test_a_lost_correction(self):
        self._broken("UPDATE main.blocks SET rate_corrected = 0 WHERE id = 41",
                     "lost their correction")

    def test_a_lost_tombstone(self):
        self._broken("DELETE FROM main.deleted_ranges", "deleted range(s) not carried")

    def test_a_moved_provenance_word(self):
        self._broken("UPDATE main.blocks SET rate_source = 'schedule' WHERE id = 40",
                     "provenance counts moved")

    def test_a_word_swapped_between_two_blocks(self):
        """Counts alone would pass: the row-by-row half of the gate catches it."""
        self._broken("UPDATE main.blocks SET rate_source = CASE id WHEN 40 THEN 'corrected' "
                     "WHEN 41 THEN 'settled' END WHERE id IN (40, 41)",
                     "changed provenance outside the rule")

    def test_a_changed_key(self):
        def _src(**kw):
            return v4_db(os.path.join(self.dir, "src.db"),
                         state={"data_source_mode": "api"})
        self.src = _src
        self._broken("UPDATE main.kraken_state SET value = 'cad' WHERE key = 'data_source_mode'",
                     "keys not carried")


def _server():
    spec = importlib.util.spec_from_file_location("server_v4i", os.path.join(_ROOT, "web", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv


class TheRestore(unittest.TestCase):
    """The restore paths: a v4-shaped backup is imported, a v5 backup is swapped in."""

    @classmethod
    def setUpClass(cls):
        cls.srv = _server()

    def setUp(self):
        C.reset_for_tests()
        self.data = tempfile.mkdtemp()
        self.share = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.share, "backups"))
        self.live = os.path.join(self.data, "blocks.db")
        BlockStore(self.live).close()
        self.live_bytes = open(self.live, "rb").read()
        eng = mock.MagicMock()
        eng.api_import_running.return_value = False
        self.p = [mock.patch.object(self.srv, "DATA_DIR", self.data),
                  mock.patch.object(self.srv, "_share_backup_dir", lambda: self.share),
                  mock.patch.object(self.srv, "_create_backup_zip", lambda label="b": None),
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

    def _zip(self, db):
        with zipfile.ZipFile(os.path.join(self.share, "backups", "b.zip"), "w") as zf:
            zf.write(db, "blocks.db")
        return "b.zip"

    def _live(self, sql, *a):
        conn = sqlite3.connect(self.live)
        try:
            return conn.execute(sql, a).fetchone()
        finally:
            conn.close()

    def _done(self):
        j = self.srv._restore_job
        self.assertEqual(j["status"], "done", j)
        self.assertEqual(sorted(os.listdir(self.data)), ["blocks.db"],
                         "an extracted or imported file was left behind")

    def test_a_v4_backup_zip_is_imported(self):
        src = v4_db(os.path.join(self.share, "v4.db"),
                    meta={"reprice_history_state": {"done": True}})
        self.srv._restore_worker(self._zip(src), None, False)
        self._done()
        self.assertEqual(read_emt_schema(self.live), EMT_SCHEMA)
        self.assertIsNotNone(self._live("SELECT value FROM store_meta WHERE key='import_audit'"))
        self.assertIsNone(self._live("SELECT value FROM store_meta "
                                     "WHERE key='reprice_history_state'"))
        self.assertEqual(self._live("SELECT COUNT(*) FROM blocks")[0], 2)

    def test_a_v4_upload_is_imported(self):
        staged = v4_db(os.path.join(self.data, "uploaded_restore.db"))
        self.srv._restore_worker("", ["blocks.db"], False, staged=staged)
        self._done()
        self.assertEqual(read_emt_schema(self.live), EMT_SCHEMA)

    def test_a_v4_flat_copy_is_imported(self):
        v4_db(os.path.join(self.share, "blocks.db"))
        self.srv._restore_worker("", None, True)
        self._done()
        self.assertEqual(read_emt_schema(self.live), EMT_SCHEMA)

    def test_a_v5_backup_is_swapped_in_as_it_is(self):
        """It takes gate 1 alone: no stamp is needed, and nothing is dropped."""
        src = os.path.join(self.share, "v5.db")
        st = BlockStore(src)
        st.set_meta("reprice_history_state", {"done": True})     # an import would drop it
        st.close()
        self.srv._restore_worker(self._zip(src), None, False)
        self._done()
        self.assertIsNotNone(self._live("SELECT value FROM store_meta "
                                        "WHERE key='reprice_history_state'"))
        self.assertIsNone(self._live("SELECT value FROM store_meta WHERE key='import_audit'"))

    def _refused(self, src, says):
        self.srv._restore_worker(self._zip(src), None, False)
        j = self.srv._restore_job
        self.assertEqual(j["status"], "error", j)
        self.assertIn(says, j["error"])
        self.assertEqual(open(self.live, "rb").read(), self.live_bytes, "the live database changed")
        self.assertEqual(sorted(os.listdir(self.data)), ["blocks.db"])

    def _v5_at(self, rev):
        src = os.path.join(self.share, "v5r.db")
        st = BlockStore(src)
        st.set_meta("emt_schema", rev)
        st.close()
        return src

    def test_a_v5_backup_from_an_earlier_build_is_refused(self):
        self._refused(self._v5_at(EMT_SCHEMA - 1), "earlier development build")

    def test_a_v5_backup_from_a_newer_build_is_refused(self):
        self._refused(self._v5_at(EMT_SCHEMA + 1), "newer version of EMT")

    def test_an_import_that_cannot_complete_leaves_everything_untouched(self):
        src = v4_db(os.path.join(self.share, "odd.db"),
                    extra_sql=("ALTER TABLE blocks ADD COLUMN mystery_col REAL",))
        self._refused(src, "mystery_col")

    def test_a_below_floor_v4_backup_is_still_refused(self):
        """GUARD: the v4 gate runs before the importer, as before."""
        self._refused(v4_db(os.path.join(self.share, "old.db"), written_by="4.5.13"),
                      "last v4 release")

    def test_from_zip_imports_a_v4_backup(self):
        src = v4_db(os.path.join(self.share, "v4.db"))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.write(src, "blocks.db")
        buf.seek(0)
        r = self.srv.app.test_client().post("/api/import/from-zip",
                                            data={"zipfile": (buf, "v4.zip")},
                                            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(read_emt_schema(self.live), EMT_SCHEMA)


class TheBoundary(unittest.TestCase):
    """Spec §1.2: no domain code imports from the importer. The restore paths in
    web/server.py are its only door, so deleting the package removes one branch."""

    def test_only_the_server_imports_v4import(self):
        offenders = []
        for d in (_ROOT, os.path.join(_ROOT, "web")):
            for f in sorted(os.listdir(d)):
                if not f.endswith(".py") or f == "server.py":
                    continue
                tree = ast.parse(open(os.path.join(d, f)).read())
                for node in ast.walk(tree):
                    names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                             else [node.module or ""] if isinstance(node, ast.ImportFrom)
                             else [])
                    if any(n == "v4import" or n.startswith("v4import.") for n in names):
                        offenders.append(f)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
