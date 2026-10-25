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
    from tests.v4_shape import strip_v5_only
    strip_v5_only(c)
    # v4's blocks still carry the columns v5 dropped in steps 5a, 5b and 5c (10 Oct 2026).
    for col in ("imp_kwh_remainder REAL", "imp_cost_remainder REAL",
                "rate_reconciled INTEGER NOT NULL DEFAULT 0",
                "imp_rate_exc REAL", "standing_charge_exc REAL", "exc_source TEXT",
                "imp_kwh_ev REAL", "imp_cost_ev REAL", "imp_rate_ev REAL",
                "imp_ev_band TEXT", "imp_home_band TEXT"):
        c.execute("ALTER TABLE blocks ADD COLUMN " + col)
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

    def test_the_import_never_writes_a_wal(self):
        """10 Oct 2026 soak: built in WAL mode, the target took the whole import twice — into
        the WAL at COMMIT, then back into the file — 68 s of an 81 s import on the Pi's SD
        card. Built with its journal in memory, no -wal ever exists. Fails on the parent."""
        seen = []
        real = v4import.transforms.run

        def _run(conn, report):
            real(conn, report)
            seen.append(os.path.exists(self.target + "-wal"))
        with mock.patch.object(v4import.transforms, "run", _run):
            v4import.import_v4(self.src(), self.target)
        self.assertEqual(seen, [False])

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
        # what v5 reads as a settled main block (measured_main_costs_since's query, which went with
        # the VAT resync in engine step 2d-3, 23 Oct 2026)
        self.assertEqual([r[0] for r in st._conn.execute(
            "SELECT block_start FROM blocks WHERE meter_id = 'electricity_main' AND "
            "rate_source = 'settled' AND COALESCE(rate_corrected, 0) = 0 AND block_start >= ? "
            "ORDER BY block_start", ("2026-01-01",))], [B1])


def _blk(i, bs, kwh, rate, source, *, ev=None, flag=0):
    """SQL for one main block; `ev` = (kwh_ev, cost_ev, rate_ev, ev_band, home_band)."""
    cols = ("id, block_start, block_end, meter_id, config_period_id, imp_kwh, imp_rate, "
            "imp_rate_exc, imp_cost, imp_cost_exc, rate_source, rate_reconciled")
    vals = "%d, '%s', '%s', 'electricity_main', 1, %r, %r, %r, %r, %r, '%s', %d" % (
        i, bs, bs, kwh, rate, round(rate / 1.05, 6), round(kwh * rate, 6),
        round(kwh * rate / 1.05, 6), source, flag)
    if ev:
        cols += ", imp_kwh_ev, imp_cost_ev, imp_rate_ev, imp_ev_band, imp_home_band"
        vals += ", %r, %r, %r, '%s', '%s'" % ev
    return "INSERT INTO blocks (%s) VALUES (%s)" % (cols, vals)


def _seg(bs, seq, kwh, rate, band, attr):
    return ("INSERT INTO block_segments (block_start, meter_id, channel, seq, kwh, inc_rate, "
            "exc_rate, band, attribution) VALUES ('%s', 'electricity_main', 'import', %d, %r, "
            "%r, %r, '%s', '%s')" % (bs, seq, kwh, rate, round(rate / 1.05, 6), band, attr))


EV_BS, HELD_BS, LOOSE_BS, FOLD_BS = ("2026-08-25T23:00:00", "2026-08-26T00:00:00",
                                     "2026-08-26T01:00:00", "2026-08-26T02:00:00")
SEG_SQL = (
    # an EV split in the columns and no segments (spec §5.2 v14)
    _blk(60, EV_BS, 4.0, 0.07, "schedule", ev=(3.0, 0.21, 0.07, "off_peak", "off_peak")),
    # settled, re-costed by v4 to 7.5 kWh, segments still at 7.058 (BL-79 (1)); its EV split
    # in the columns matches its EV segment, as v4 wrote both
    _blk(61, HELD_BS, 7.5, 0.30, "measured", ev=(3.0, 0.21, 0.07, "off_peak", "day")),
    _seg(HELD_BS, 0, 3.0, 0.07, "off_peak", "ev"), _seg(HELD_BS, 1, 4.058, 0.30, "day", "house"),
    # not held, and its segments miss its kWh: reported, never refitted
    _blk(62, LOOSE_BS, 2.0, 0.30, "schedule"), _seg(LOOSE_BS, 0, 1.9, 0.30, "day", "house"),
    # flagged while left 'schedule': the fold makes it held, so it is refitted
    _blk(63, FOLD_BS, 1.0, 0.30, "schedule", flag=1), _seg(FOLD_BS, 0, 0.9, 0.30, "day", "house"),
)


class TheSegmentTransforms(_Dir):
    """Transforms 2 and 3 (spec §5.2 v14, v16; gate 11)."""

    def _segs(self, conn, bs):
        return [tuple(r) for r in conn.execute(
            "SELECT attribution, band, round(kwh, 6), inc_rate FROM block_segments "
            "WHERE block_start = ? ORDER BY seq", (bs,))]

    def test_an_ev_split_with_no_segments_gets_them_from_its_columns(self):
        conn, report = self.imported(extra_sql=SEG_SQL)
        self.assertEqual(self._segs(conn, EV_BS), [("ev", "off_peak", 3.0, 0.07),
                                                   ("house", "off_peak", 1.0, 0.07)])
        self.assertEqual(report["transforms"]["ev_segments"], {"blocks": 1, "kwh_ev": 3.0})

    def test_ev_split_readers_see_it_from_the_segments(self):
        """The EV-split columns are dropped (step 5c): only the segments can say it."""
        self.imported(extra_sql=SEG_SQL)
        conn = sqlite3.connect(self.target)
        self.addCleanup(conn.close)
        import ev_split
        n = conn.execute("SELECT %s FROM blocks WHERE block_start = ?" % ev_split.sql_kwh("blocks"),
                         (EV_BS,)).fetchone()[0]
        self.assertAlmostEqual(n, 3.0, places=6)

    def test_a_held_block_is_fitted_to_its_settled_kwh(self):
        conn, report = self.imported(extra_sql=SEG_SQL)
        self.assertEqual(self._segs(conn, HELD_BS), [("ev", "off_peak", 3.0, 0.07),
                                                     ("house", "day", 4.5, 0.30)])
        self.assertEqual(report["transforms"]["refit_held"]["refitted"], 2)

    def test_a_block_held_by_the_fold_is_refitted_too(self):
        conn, _ = self.imported(extra_sql=SEG_SQL)
        self.assertEqual(self._segs(conn, FOLD_BS), [("house", "day", 1.0, 0.30)])

    def test_a_block_that_is_not_held_is_reported_not_changed(self):
        conn, report = self.imported(extra_sql=SEG_SQL)
        self.assertEqual(self._segs(conn, LOOSE_BS), [("house", "day", 1.9, 0.30)])
        self.assertEqual(report["transforms"]["refit_held"]["reported"], [LOOSE_BS])

    def test_every_column_is_carried_as_it_is(self):
        """GUARD: the transforms write segments only (spec §5.2); the EV-split columns are
        v4-only and not carried (step 5c), so the EV is in the segments alone."""
        conn, _ = self.imported(extra_sql=SEG_SQL)
        self.assertEqual(tuple(conn.execute("SELECT imp_kwh, imp_cost FROM blocks "
                                            "WHERE id = 60").fetchone()), (4.0, 0.28))

    def test_an_all_ev_block_whose_bill_is_not_kwh_times_rate(self):
        """An EV split with no house share and no segments, whose bill is not kWh × rate
        (the 5%-VAT October bill against a 0% rate, v4 #505). The reconstruction gives the EV
        segment the whole bill, so the segments still sum to imp_cost; imp_cost_ev is kWh ×
        rate. Gate 5 refused it, comparing £ against the column on a block the importer had
        just rebuilt by its own rule (found on the 10 Oct soak copy). Fails on the parent."""
        bs = "2026-10-09T01:00:00"
        conn, report = self.imported(extra_sql=(
            "INSERT INTO blocks (id, block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "imp_rate, imp_cost, imp_cost_exc, rate_source, imp_kwh_ev, imp_cost_ev, imp_rate_ev, "
            "imp_ev_band, imp_home_band) VALUES (70, '%s', '%s', 'electricity_main', 1, 0.156, "
            "0.052314, 0.008569, 0.008161, 'measured', 0.156, 0.008161, 0.052314, 'off_peak', "
            "'off_peak')" % (bs, bs),))
        self.assertEqual(self._segs(conn, bs), [("ev", "off_peak", 0.156,
                                                 round(0.008569 / 0.156, 6))])
        self.assertTrue(report["gates"]["split_reproduced"]["ok"], report["gates"])

    def test_a_block_with_segments_keeps_them(self):
        """GUARD: the fixture's block 40 already has segments matching its kWh."""
        conn, _ = self.imported(extra_sql=SEG_SQL)
        self.assertEqual(self._segs(conn, B1), [("house", "day", 2.0, 0.245)])

    def test_the_gates_hold(self):
        _, report = self.imported(extra_sql=SEG_SQL)
        self.assertTrue(all(g["ok"] for g in report["gates"].values()), report["gates"])
        self.assertEqual(report["gates"]["held_fit"]["refitted"], 2)


def _bs(i):
    return "2026-09-%02dT02:00:00" % (1 + i)


def _bill_split_sql():
    """v4 IOG-SMB history settled under 4.5.9 (the dispatch capped the bill's EV). The era begins
    at the first cached bill that states a split (block 80, 1 Sep)."""
    rows, n = [], 80
    def block(bs, kwh, rate_source="measured", corrected=0):
        nonlocal n
        n += 1
        rows.append("INSERT INTO blocks (id, block_start, block_end, meter_id, config_period_id, "
                    "imp_kwh, imp_rate, imp_cost, imp_cost_exc, rate_source, rate_corrected) VALUES "
                    "(%d, '%s', '%s', 'electricity_main', 1, %s, 0.05493, %s, %s, '%s', %d)"
                    % (n, bs, bs, kwh, round(kwh * 0.05493, 6), round(kwh * 0.05231, 6),
                       rate_source, corrected))
    def segs(bs, *legs):
        # v4 carries the split in its columns too, in step with the segments (verify checks both)
        ev = [(k, r, band) for (k, r, band, attr) in legs if attr == "ev"]
        if ev:
            k, r, band = ev[0]
            rows.append("UPDATE blocks SET imp_kwh_ev = %s, imp_cost_ev = %s, imp_rate_ev = %s, "
                        "imp_ev_band = '%s' WHERE block_start = '%s'"
                        % (k, round(k * r, 6), r, band, bs))
        for i, (k, r, band, attr) in enumerate(legs):
            rows.append("INSERT INTO block_segments (block_start, meter_id, channel, seq, kwh, "
                        "inc_rate, exc_rate, band, attribution) VALUES ('%s', 'electricity_main', "
                        "'import', %d, %s, %s, %s, '%s', '%s')" % (bs, i, k, r, round(r / 1.05, 7),
                                                                   band, attr))
    def bill(bs, ev):
        rows.append("INSERT INTO measured_cost (slot_start, mpan, direction, cost_incl, cost_excl, "
                    "label, kwh, ev_kwh, home_kwh) VALUES ('%s', 'M1', 'CONSUMPTION', 0.3, 0.28, "
                    "'OFF_PEAK', 6.0, %s, %s)" % (bs, ev, 6.0 - ev))
    # 0: dispatch-capped — EV 1.45 where the bill says 3.409 (6 Sep 2026 10:30's shape)
    block(_bs(0), 6.0); segs(_bs(0), (1.45, 0.05493, "off_peak", "ev"), (4.55, 0.05493, "off_peak", "house")); bill(_bs(0), 3.409)
    # 1: v4's residual-absorber house rate, a few millionths off the EV leg's: one rate in substance
    block(_bs(1), 6.0); segs(_bs(1), (3.37, 0.05493, "off_peak", "ev"), (2.63, 0.0549297, "off_peak", "house")); bill(_bs(1), 3.409)
    # 2: a capped boundary — EV off-peak, house at the day rate: reported, not moved
    block(_bs(2), 6.0); segs(_bs(2), (1.0, 0.05493, "off_peak", "ev"), (5.0, 0.32309, "day", "house")); bill(_bs(2), 3.0)
    # 3: the 4.5.23 case — the bill says no EV where EMT's split has the car: reported
    block(_bs(3), 6.0); segs(_bs(3), (3.1, 0.05493, "off_peak", "ev"), (2.9, 0.05493, "off_peak", "house")); bill(_bs(3), 0.0)
    # 4: already the bill's — untouched
    block(_bs(4), 6.0); segs(_bs(4), (3.409, 0.05493, "off_peak", "ev"), (2.591, 0.05493, "off_peak", "house")); bill(_bs(4), 3.409)
    # 5: corrected — untouched
    block(_bs(5), 6.0, rate_source="corrected", corrected=1); segs(_bs(5), (1.0, 0.05493, "off_peak", "ev"), (5.0, 0.05493, "off_peak", "house")); bill(_bs(5), 3.409)
    # 6: before the era (31 Aug, its bill states no split): untouched
    pre = "2026-08-31T02:00:00"
    block(pre, 6.0); segs(pre, (1.0, 0.05493, "off_peak", "ev"), (5.0, 0.05493, "off_peak", "house"))
    rows.append("INSERT INTO measured_cost (slot_start, mpan, direction, cost_incl, cost_excl, label, "
                "kwh) VALUES ('%s', 'M1', 'CONSUMPTION', 0.3, 0.28, 'OFF_PEAK', 6.0)" % pre)
    return tuple(rows)


class TheBillsSplit(_Dir):
    """Transform 3b (bill and split paths design §7.1, 16 Oct 2026): settled IOG-SMB history takes
    its cached bill's split in the importer, not a runtime heal. Replayed on the soak's v4 file:
    169 of 991 moved, none reported, EV +12.91 kWh; imp_cost total identical, no rate changed.
    Discriminating (fail on the parent), except the guards named."""

    def _ev(self, conn, bs):
        return round(conn.execute("SELECT COALESCE(SUM(kwh), 0) FROM block_segments WHERE "
                                  "block_start = ? AND attribution = 'ev'", (bs,)).fetchone()[0], 6)

    def _cost(self, conn, bs):
        return conn.execute("SELECT SUM(kwh * inc_rate) FROM block_segments WHERE block_start = ?",
                            (bs,)).fetchone()[0]

    def test_a_dispatch_capped_split_takes_the_bills(self):
        conn, report = self.imported(extra_sql=_bill_split_sql())
        self.assertEqual(self._ev(conn, _bs(0)), 3.409)
        self.assertAlmostEqual(self._cost(conn, _bs(0)), 6.0 * 0.05493, places=6)

    def test_the_residual_house_rate_is_one_rate_and_its_cost_is_kept(self):
        conn, report = self.imported(extra_sql=_bill_split_sql())
        self.assertEqual(self._ev(conn, _bs(1)), 3.409)
        self.assertAlmostEqual(self._cost(conn, _bs(1)), 3.37 * 0.05493 + 2.63 * 0.0549297, places=6)

    def test_a_capped_boundary_and_the_4523_case_are_reported_not_moved(self):
        conn, report = self.imported(extra_sql=_bill_split_sql())
        t = report["transforms"]["bill_split"]
        self.assertEqual((t["reported_mixed"], t["reported_held"]), ([_bs(2)], [_bs(3)]))
        self.assertEqual((self._ev(conn, _bs(2)), self._ev(conn, _bs(3))), (1.0, 3.1))

    def test_already_the_bills_corrected_and_pre_era_are_untouched(self):
        """GUARD."""
        conn, report = self.imported(extra_sql=_bill_split_sql())
        self.assertEqual([self._ev(conn, b) for b in (_bs(4), _bs(5), "2026-08-31T02:00:00")],
                         [3.409, 1.0, 1.0])
        self.assertEqual(report["transforms"]["bill_split"]["moved"], 2)

    def test_the_gates_hold(self):
        _, report = self.imported(extra_sql=_bill_split_sql())
        self.assertTrue(all(g["ok"] for g in report["gates"].values()), report["gates"])
        self.assertEqual(report["gates"]["bill_split"], {"moved": 2, "reported": 2, "ok": True})


def _refit_then_split_sql():
    """A settled block transform 3 refits (its segments miss its kWh) and 3b then moves to the
    bill's split — the staging import of prod's 4.5.24 database, 19 Oct 2026: 11 Sep 00:00's shape."""
    bs, kwh = "2026-09-09T02:00:00", 2.858
    return ("INSERT INTO blocks (id, block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "imp_rate, imp_cost, imp_cost_exc, rate_source, rate_corrected, imp_kwh_ev, imp_cost_ev, "
            "imp_rate_ev, imp_ev_band) VALUES (120, '%s', '%s', 'electricity_main', 1, %s, 0.0549297, "
            "0.15699, 0.14951, 'measured', 0, 2.82, 0.154903, 0.05493, 'off_peak')" % (bs, bs, kwh),
            "INSERT INTO block_segments (block_start, meter_id, channel, seq, kwh, inc_rate, exc_rate, "
            "band, attribution) VALUES ('%s', 'electricity_main', 'import', 0, 2.82, 0.05493, 0.0523143, "
            "'off_peak', 'ev'), ('%s', 'electricity_main', 'import', 1, 0.067, 0.0549297, 0.0523140, "
            "'off_peak', 'house')" % (bs, bs),
            "INSERT INTO measured_cost (slot_start, mpan, direction, cost_incl, cost_excl, label, kwh, "
            "ev_kwh, home_kwh) VALUES ('%s', 'M1', 'CONSUMPTION', 0.15699, 0.14951, 'OFF_PEAK', %s, "
            "2.84125, 0.01675)" % (bs, kwh))


class ARefitThenMovedBlock(_Dir):
    """A block transform 3 refits and 3b then moves (19 Oct 2026). One record per block let 3b's
    overwrite 3's, so the `bill_split` gate judged it against the source segments the refit had
    correctly changed, and refused prod's import (4 such blocks); `held_fit` no longer saw it.
    Discriminating: on the parent the import is refused."""
    BS = "2026-09-09T02:00:00"

    def _imported(self):
        return self.imported(extra_sql=_bill_split_sql() + _refit_then_split_sql())

    def test_it_imports(self):
        _, report = self._imported()
        self.assertTrue(all(g["ok"] for g in report["gates"].values()), report["gates"])
        self.assertEqual(report["gates"]["bill_split"]["moved"], 3)
        self.assertEqual(report["gates"]["held_fit"]["refitted"], 1)

    def test_its_segments_are_the_kwh_the_bill_s_ev_and_its_cost(self):
        conn, _ = self._imported()
        k, c, ev = conn.execute(
            "SELECT SUM(kwh), SUM(kwh * inc_rate), SUM(CASE WHEN attribution = 'ev' THEN kwh END) "
            "FROM block_segments WHERE block_start = ? AND meter_id = 'electricity_main'",
            (self.BS,)).fetchone()
        self.assertAlmostEqual(k, 2.858, places=6)
        self.assertAlmostEqual(ev, 2.84125, places=6)
        self.assertAlmostEqual(c, 0.15699, places=5)

    def test_a_refit_that_re_priced_is_still_caught(self):
        """The move's starting point is the refit's result: a re-priced refit fails the import."""
        from v4import import transforms
        real = transforms.refit_held

        def repricing(conn, report):
            real(conn, report)
            conn.execute("UPDATE main.block_segments SET inc_rate = inc_rate * 1.01 "
                         "WHERE block_start = ?", (self.BS,))
        with mock.patch.object(transforms, "refit_held", repricing):
            with self.assertRaises(v4import.ImportFailed) as e:
                self._imported()
        self.assertIn("re-priced by the refit", str(e.exception))


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
                     ("api_import_done_import", "1")):
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

    def test_the_pre_live_snapshot_marker_crosses(self):
        """v4 took its one pre-live snapshot and the restore has just taken a safety backup:
        re-run, it wrote a third 80 MB copy on the Pi (10 Oct 2026). Fails on the parent."""
        conn, _ = self.imported(state={"pre_live_snapshot_done": "1"})
        self.assertEqual(self.meta(conn, "pre_live_snapshot_done", "kraken_state"), "1")

    MAIN_METER = "INSERT INTO meters (meter_id, config_period_id, is_sub_meter) VALUES ('%s', 1, 0)" % MAIN

    def _rerun(self, conn):
        return conn.execute("SELECT COUNT(*) FROM blocks WHERE needs_pass2_rerun = 1 AND meter_id IN "
                            "(SELECT meter_id FROM meters WHERE is_sub_meter = 0)").fetchone()[0]

    def test_the_settlement_source_is_not_carried(self):
        """Retired 21 Oct 2026 (data management design §1 (7)). Fails on the parent."""
        conn, report = self.imported(state={"data_source_mode": "cad+api", "billing_source": "dcc"},
                                     extra_sql=(self.MAIN_METER,))
        self.assertIsNone(self.meta(conn, "billing_source", "kraken_state"))
        self.assertIn("billing_source", report["keys"]["kraken_state"]["dropped"])
        self.assertEqual(self._rerun(conn), 0)                 # 'dcc' is v5's own: nothing moves

    def test_a_chosen_cad_re_runs_the_main_meter(self):
        """A v4 on CAD + API that billed from its own readings: back to the settled kWh, so every
        main-meter half-hour is re-priced. Fails on the parent."""
        conn, report = self.imported(state={"data_source_mode": "cad+api", "billing_source": "cad"},
                                     extra_sql=(self.MAIN_METER,))
        n = conn.execute("SELECT COUNT(*) FROM blocks WHERE meter_id IN "
                         "(SELECT meter_id FROM meters WHERE is_sub_meter = 0)").fetchone()[0]
        self.assertGreater(n, 0)
        self.assertEqual(self._rerun(conn), n)
        self.assertEqual(report["billing_source"], {"was": "cad", "mode": "cad+api", "rerun": n})

    def test_the_gate_refuses_a_re_run_left_undone(self):
        """Gate `billing_source`: the report says re-run, the blocks are not flagged."""
        def unflagged(conn, report):
            report["billing_source"] = {"was": "cad", "mode": "cad+api", "rerun": 1}
        with mock.patch.object(v4import.transforms, "_billing_source", unflagged):
            with self.assertRaises(v4import.ImportFailed) as e:
                self.imported(state={"data_source_mode": "cad+api", "billing_source": "cad"},
                              extra_sql=(self.MAIN_METER,))
        self.assertIn("settlement source re-run", str(e.exception))

    def test_cad_without_the_api_moves_nothing(self):
        """GUARD: on CAD-only the source never meant anything (no settled kWh to prefer)."""
        conn, report = self.imported(state={"data_source_mode": "cad", "billing_source": "cad"},
                                     extra_sql=(self.MAIN_METER,))
        self.assertEqual(self._rerun(conn), 0)

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

    def _seg_src(self, **kw):
        return v4_db(os.path.join(self.dir, "src.db"), extra_sql=SEG_SQL)

    def test_ev_segments_that_miss_the_columns(self):
        self.src = self._seg_src
        self._broken("UPDATE main.block_segments SET kwh = 2.5 WHERE block_start = '%s' "
                     "AND attribution = 'ev'" % EV_BS, "not reproducing their columns")

    def test_a_held_block_left_unfitted(self):
        self.src = self._seg_src
        self._broken("UPDATE main.block_segments SET kwh = 4.058 WHERE block_start = '%s' "
                     "AND attribution = 'house'" % HELD_BS, "still miss their kWh")

    def test_a_refit_that_re_priced_a_segment(self):
        self.src = self._seg_src
        self._broken("UPDATE main.block_segments SET inc_rate = 0.25 WHERE block_start = '%s' "
                     "AND attribution = 'ev'" % HELD_BS, "re-priced by the refit")

    def test_an_ev_segment_whose_cost_moved(self):
        """Gate 5 (spec §9.5): the EV segments reproduce the source's imp_cost_ev — on a block
        whose segments came from the source (the held block: refitted, its EV unchanged)."""
        self.src = self._seg_src
        self._broken("UPDATE main.block_segments SET inc_rate = inc_rate + 0.01 "
                     "WHERE block_start = '%s' AND attribution = 'ev'" % HELD_BS,
                     "do not reproduce the EV-split columns")

    def test_a_rebuilt_ev_segment_whose_cost_moved(self):
        """GUARD: a block transform 2 rebuilt is judged by the EV-segments gate's rule instead
        (on the parent gate 5 refused it too, with its own message)."""
        self.src = self._seg_src
        self._broken("UPDATE main.block_segments SET inc_rate = inc_rate + 0.01 "
                     "WHERE block_start = '%s' AND attribution = 'ev'" % EV_BS,
                     "not reproducing their columns")

    def test_a_phantom_ev_segment(self):
        """Gate 5: no block has EV segments where the source's columns had no split."""
        self.src = self._seg_src
        self._broken("UPDATE main.block_segments SET attribution = 'ev' WHERE block_start = '%s'"
                     % B1, "with EV segments where the columns had none")

    def test_a_rewritten_segment_outside_the_transforms(self):
        """GUARD: "carried exactly" caught this before; it still does for untouched blocks."""
        self.src = self._seg_src
        self._broken("UPDATE main.block_segments SET band = 'peak' WHERE block_start = '%s'"
                     % B1, "block_segments")

    def test_a_changed_key(self):
        def _src(**kw):
            return v4_db(os.path.join(self.dir, "src.db"),
                         state={"data_source_mode": "api"})
        self.src = _src
        self._broken("UPDATE main.kraken_state SET value = 'cad' WHERE key = 'data_source_mode'",
                     "keys not carried")



IMP = ["2024-07-01T17:%02d:00" % m for m in (0, 30)] + ["2024-07-01T18:00:00", "2024-07-01T18:30:00"]


def _imported_sql(mpan_bills=True):
    """v4's API import (engine step 4 design §6 (e)): imported main half-hours with Octopus's cost on
    the block and no bill row — one Octopus-costed (`exc_source = 'measurement'`), one v4 reconstructed
    from the tariff ('tariff'), one Octopus-costed that already has a bill, one with no kWh — and a
    live block with no bill. `mpan_bills=False` drops the source's bills (the MPAN then comes from the
    main import channel)."""
    sql = ["INSERT INTO blocks (id, block_start, block_end, meter_id, config_period_id, imp_kwh, "
           "imp_rate, imp_cost, imp_cost_exc, source, exc_source) VALUES "
           "(200, '%s', '%s', 'electricity_main', 1, 2.04, 0.0700035, 0.142807, 0.136007, "
           "'imported_api', 'measurement'), "
           "(201, '%s', '%s', 'electricity_main', 1, 1.0, 0.24, 0.24, 0.228571, 'imported_api', "
           "'tariff'), "
           "(202, '%s', '%s', 'electricity_main', 1, 3.0, 0.07, 0.21, 0.2, 'imported_api', "
           "'measurement'), "
           "(203, '%s', '%s', 'electricity_main', 1, 0.0, 0.07, 0.0, 0.0, 'imported_api', "
           "'measurement'), "
           "(204, '2026-09-16T12:00:00', '2026-09-16T12:30:00', 'electricity_main', 1, 1.0, 0.24, "
           "0.24, 0.228571, NULL, 'measurement')"
           % (IMP[0], IMP[1], IMP[1], IMP[2], IMP[2], IMP[3], IMP[3], "2024-07-01T19:00:00"),
           "INSERT INTO measured_cost (slot_start, mpan, direction, cost_incl, cost_excl, label, kwh) "
           "VALUES ('%s', 'M1', 'CONSUMPTION', 0.21, 0.2, 'OFF_PEAK', 3.0)" % IMP[2]]
    if not mpan_bills:
        sql += ["DELETE FROM measured_cost",
                "INSERT INTO meters (id, config_period_id, meter_id) VALUES (7, 1, 'electricity_main')",
                "INSERT INTO meter_channels (meter_id, channel, mpan) VALUES (7, 'import', 'M9')"]
    return tuple(sql)


class TheImportedBills(_Dir):
    """Transform 3c (engine step 4 design §6 (e), 25 Oct 2026): an imported half-hour's Octopus cost
    becomes its bill. v4's API import wrote none, so history past Octopus's two years kept the band it
    was billed at only in its stored cost (dev's settle parity report: 1,084 evening smart charges
    reading peak, +£476.10). Discriminating (fail on the parent), except the guards named."""

    def _bill(self, conn, bs):
        r = conn.execute("SELECT mpan, cost_incl, cost_excl, label, kwh FROM measured_cost "
                         "WHERE slot_start = ? AND direction = 'CONSUMPTION'", (bs,)).fetchone()
        return None if r is None else tuple(r)

    def test_an_octopus_cost_becomes_its_bill(self):
        conn, report = self.imported(extra_sql=_imported_sql())
        self.assertEqual(self._bill(conn, IMP[0]), ("M1", 0.142807, 0.136007, None, 2.04))
        self.assertEqual(report["transforms"]["imported_bills"], {"written": 1, "skipped": None})

    def test_a_reconstructed_cost_is_not_a_bill(self):
        """GUARD (no bill is written on the parent either; with the first test it discriminates):
        v4's kWh × tariff rate, where Octopus gave no cost, is v4's figure, not Octopus's."""
        conn, _ = self.imported(extra_sql=_imported_sql())
        self.assertIsNone(self._bill(conn, IMP[1]))

    def test_a_bill_already_cached_and_a_live_block_are_left(self):
        """GUARD: the cached bill is carried exactly, and nothing writes a bill for a live block or
        one with no kWh — true on the parent too."""
        conn, _ = self.imported(extra_sql=_imported_sql())
        self.assertEqual(self._bill(conn, IMP[2]), ("M1", 0.21, 0.2, "OFF_PEAK", 3.0))
        self.assertIsNone(self._bill(conn, "2026-09-16T12:00:00"))
        self.assertIsNone(self._bill(conn, IMP[3]))

    def test_with_no_bills_the_mpan_is_the_main_import_channels(self):
        conn, _ = self.imported(extra_sql=_imported_sql(mpan_bills=False))
        self.assertEqual(self._bill(conn, IMP[0])[0], "M9")

    def test_with_no_mpan_nothing_is_written(self):
        conn, report = self.imported(extra_sql=_imported_sql(mpan_bills=False)
                                     + ("DELETE FROM meter_channels",))
        self.assertIsNone(self._bill(conn, IMP[0]))
        self.assertIn("no MPAN", report["transforms"]["imported_bills"]["skipped"])

    def test_the_gates_hold(self):
        _, report = self.imported(extra_sql=_imported_sql())
        self.assertTrue(all(g["ok"] for g in report["gates"].values()), report["gates"])
        self.assertEqual(report["gates"]["imported_bills"]["written"], 1)

    def _broken(self, sql, says):
        real = v4import.transforms.run

        def _run(conn, report):
            real(conn, report)
            conn.execute(sql)
        with mock.patch.object(v4import.transforms, "run", _run):
            with self.assertRaises(v4import.ImportFailed) as cm:
                v4import.import_v4(v4_db(os.path.join(self.dir, "src.db"),
                                         extra_sql=_imported_sql()), self.target)
        self.assertIn(says, str(cm.exception))

    def test_the_gate_bites_on_a_wrong_cost(self):
        self._broken("UPDATE main.measured_cost SET cost_incl = 0.5 WHERE slot_start = '%s'" % IMP[0],
                     "not their block's Octopus cost")

    def test_the_gate_bites_on_a_missed_half_hour(self):
        self._broken("DELETE FROM main.measured_cost WHERE slot_start = '%s'" % IMP[0], "left without one")

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
        self.assertTrue(os.path.exists(os.path.join(self.share, "blocks.db")),
                        "the share's own copy was moved, not read")

    def _copies(self):
        calls = []
        real = self.srv._atomic_restore_write

        def _rec(dest, fill):
            calls.append(os.path.basename(dest))
            return real(dest, fill)
        return calls, mock.patch.object(self.srv, "_atomic_restore_write", _rec)

    def test_an_upload_is_moved_into_place_not_copied(self):
        """10 Oct 2026 soak: the swap copied the 80 MB import target into place — 40 s and a
        second write on the Pi's SD card. Both files are in DATA_DIR, so it is a rename.
        Fails on the parent."""
        staged = v4_db(os.path.join(self.data, "uploaded_restore-x.db"))
        calls, p = self._copies()
        with p:
            self.srv._restore_worker("", ["blocks.db"], False, staged=staged)
        self._done()
        self.assertNotIn("blocks.db", calls)
        self.assertEqual(read_emt_schema(self.live), EMT_SCHEMA)

    def test_a_flat_v4_copy_is_moved_from_its_import_not_copied(self):
        """Fails on the parent."""
        v4_db(os.path.join(self.share, "blocks.db"))
        calls, p = self._copies()
        with p:
            self.srv._restore_worker("", None, True)
        self._done()
        self.assertNotIn("blocks.db", calls)

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

    def test_a_staged_zip_imports_a_v4_backup(self):
        """Stage, then confirm (first-run design §2): the zip goes through the restore worker,
        as every upload now does."""
        src = v4_db(os.path.join(self.share, "v4.db"))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.write(src, "blocks.db")
        buf.seek(0)
        self.srv._restore_job["status"] = "idle"
        client = self.srv.app.test_client()
        r = client.post("/api/restore/stage", data={"file": (buf, "v4.zip")},
                        content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["facts"]["kind"], "v4")

        class _Now:
            def __init__(self, target, args=(), kwargs=None, **_):
                self.run = lambda: target(*args, **(kwargs or {}))
            def start(self):
                self.run()
        with mock.patch.object(self.srv.threading, "Thread", _Now):
            r = client.post("/api/restore/confirm", json={"token": r.get_json()["token"]})
        self.assertEqual(r.status_code, 200, r.get_json())
        self._done()
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



class TheDeviceSensors(_Dir):
    """Transform 6 (device sensors design §6, spec v23): each configured read becomes its channel's
    primary; a sub-meter's Device History sensors that are not its primary become its secondaries,
    the latest run first. Fails on the parent: the transform and its gate do not exist there."""

    METERS = ("INSERT INTO meters (id, config_period_id, meter_id, is_sub_meter) VALUES "
              "(1, 1, 'electricity_main', 0), (2, 1, 'ev_charger', 1), (3, 1, 'house_battery', 1)",
              "INSERT INTO meter_channels (meter_id, channel, read_sensor) VALUES "
              "(1, 'import', NULL), (1, 'export', NULL), (2, 'import', 'sensor.charger_now'), "
              "(3, 'import', 'sensor.battery_in')")
    RUNS = [
        {"run_id": "a", "meter_id": "ev_charger", "created_at": "2026-08-05T10:06:00",
         "sensor_ids": ["sensor.charger_now", "sensor.charger_old"]},
        {"run_id": "b", "meter_id": "house_battery", "created_at": "2026-08-05T10:07:00",
         "sensor_ids": ["sensor.battery_in"]},
        {"run_id": "c", "meter_id": "ev_charger", "created_at": "2026-08-26T20:48:00",
         "sensor_ids": ["sensor.charger_now", "sensor.charger_older", "sensor.charger_old"]}]

    def _entries(self, conn, meter):
        return [(r["entity_id"], r["role"], r["position"], r["origin"]) for r in conn.execute(
            "SELECT * FROM device_sensors WHERE meter_id = ? ORDER BY role, position", (meter,))]

    def test_the_configured_read_is_the_primary(self):
        conn, _ = self.imported(extra_sql=self.METERS)
        self.assertEqual(self._entries(conn, "ev_charger"),
                         [("sensor.charger_now", "primary", None, "configured")])
        self.assertEqual(self._entries(conn, "electricity_main"), [])     # the Mini: no read

    def test_device_history_sensors_become_secondaries_latest_run_first(self):
        conn, report = self.imported(extra_sql=self.METERS,
                                     meta={"recorder_attribution_runs": self.RUNS})
        self.assertEqual(self._entries(conn, "ev_charger")[1:],
                         [("sensor.charger_older", "secondary", 0, "v4_attribution"),
                          ("sensor.charger_old", "secondary", 1, "v4_attribution")])
        self.assertEqual(self._entries(conn, "house_battery"),
                         [("sensor.battery_in", "primary", None, "configured")])
        self.assertEqual(report["transforms"]["device_sensors"],
                         {"primaries": 2, "secondaries": 2, "devices": ["ev_charger"]})
        self.assertTrue(report["gates"]["device_sensors"]["ok"])

    def test_no_runs_no_secondaries(self):
        """GUARD (passes on the parent, which writes none at all)."""
        conn, _ = self.imported(extra_sql=self.METERS)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM device_sensors "
                                      "WHERE role = 'secondary'").fetchone()[0], 0)

    def test_the_gate_checks_against_the_source(self):
        """A transform that lost a Device History sensor fails the import."""
        from v4import import transforms
        real = transforms.device_sensors

        def lossy(conn, report):
            real(conn, report)
            conn.execute("DELETE FROM main.device_sensors WHERE entity_id = 'sensor.charger_old'")
        with mock.patch.object(transforms, "device_sensors", lossy):
            with self.assertRaises(v4import.ImportFailed) as e:
                self.imported(extra_sql=self.METERS, meta={"recorder_attribution_runs": self.RUNS})
        self.assertIn("Device History sensor(s) not carried", str(e.exception))

if __name__ == "__main__":
    unittest.main()
