"""
The tier-1 bill-conformance harness (.build/bill_conformance.py) is the thing that
would tell us whether v4's pricing agrees with Octopus. So it has to be trustworthy
BEFORE it is pointed at a real database and its verdict believed.

Hermetic: builds tiny databases with planted answers — agreements, known divergences,
circular rows, missing buckets — and asserts the harness finds exactly what was planted.
A harness that cannot detect an injected error is a harness that says "clean" forever.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".build"))
import bill_conformance as bc          # noqa: E402

OFF, DAY = 0.05493, 0.323092


def _db(rows, *, buckets=True, rate_source=True):
    """rows: dicts with slot, imp_rate, imp_cost, imp_kwh, cost_incl, label, ..."""
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    c = sqlite3.connect(path)
    bcols = ("block_start TEXT, meter_id TEXT, imp_kwh REAL, imp_kwh_api REAL, "
             "imp_rate REAL, imp_cost REAL, imp_kwh_ev REAL, imp_cost_ev REAL, "
             "imp_rate_ev REAL, rate_corrected INTEGER DEFAULT 0, source TEXT"
             + (", rate_source TEXT" if rate_source else ""))
    c.execute(f"CREATE TABLE blocks ({bcols})")
    mcols = ("slot_start TEXT, mpan TEXT DEFAULT '', direction TEXT DEFAULT 'CONSUMPTION', "
             "cost_incl REAL, cost_excl REAL, label TEXT, kwh REAL, fetched_at TEXT"
             + (", home_kwh REAL, home_rate REAL, ev_kwh REAL, ev_rate REAL" if buckets else ""))
    c.execute(f"CREATE TABLE measured_cost ({mcols})")
    for r in rows:
        bvals = [r["slot"], "electricity_main", r.get("imp_kwh", 1.0), r.get("imp_kwh", 1.0),
                 r.get("imp_rate"), r.get("imp_cost"), r.get("imp_kwh_ev"),
                 r.get("imp_cost_ev"), r.get("imp_rate_ev"), r.get("rate_corrected", 0), None]
        if rate_source:
            bvals.append(r.get("rate_source"))
        c.execute(f"INSERT INTO blocks VALUES ({','.join('?' * len(bvals))})", bvals)
        mvals = [r["slot"], "", "CONSUMPTION", r.get("cost_incl"), None,
                 r.get("label"), r.get("imp_kwh", 1.0), None]
        if buckets:
            mvals += [r.get("home_kwh"), r.get("home_rate"),
                      r.get("ev_kwh"), r.get("ev_rate")]
        c.execute(f"INSERT INTO measured_cost VALUES ({','.join('?' * len(mvals))})", mvals)
    c.commit(); c.close()
    return path


def _row(i, **kw):
    base = {"slot": "2026-08-%02dT00:00:00" % (i + 1), "imp_kwh": 2.0,
            "imp_rate": OFF, "imp_cost": round(2.0 * OFF, 6),
            "cost_incl": round(2.0 * OFF, 6), "label": "OFF_PEAK",
            "home_kwh": 0.5, "home_rate": OFF, "ev_kwh": 1.5, "ev_rate": OFF,
            "imp_rate_ev": OFF}
    base.update(kw)
    return base


class TestEmptyAndMissing(unittest.TestCase):
    def test_no_measured_cost_table_is_a_finding_not_a_crash(self):
        path = os.path.join(tempfile.mkdtemp(), "t.db")
        c = sqlite3.connect(path); c.execute("CREATE TABLE blocks (block_start TEXT)"); c.commit()
        r = bc.analyse(path)
        self.assertFalse(r["ok"])
        self.assertIn("predates", r["reason"])

    def test_empty_measured_cost_reports_no_corpus(self):
        r = bc.analyse(_db([]))
        self.assertFalse(r["ok"])
        self.assertEqual(r["total_measured_rows"], 0)
        self.assertIn("EMPTY", r["reason"])


class TestReadOnly(unittest.TestCase):
    def test_a_missing_database_is_not_created(self):
        """A plain sqlite3.connect() CREATES the file. Pointed at a typo'd path
        that would leave an empty database lying next to the real one."""
        p = os.path.join(tempfile.mkdtemp(), "typo.db")
        r = bc.analyse(p)
        self.assertFalse(r["ok"])
        self.assertFalse(os.path.exists(p), "the harness created a database")

    def test_the_database_is_opened_read_only(self):
        path = _db([_row(i) for i in range(3)])
        before = os.path.getmtime(path)
        bc.analyse(path)
        self.assertEqual(os.path.getmtime(path), before)
        for side in (path + "-wal", path + "-shm"):
            self.assertFalse(os.path.exists(side), "left %s behind" % side)


class TestAgreement(unittest.TestCase):
    def test_a_clean_database_agrees_everywhere(self):
        r = bc.analyse(_db([_row(i) for i in range(10)]))
        self.assertTrue(r["ok"])
        self.assertEqual(r["cost"]["differs"], 0)
        self.assertEqual(r["cost"]["agrees"], 10)
        self.assertEqual(r["ev_rate"]["differs"], 0)

    def test_a_planted_cost_divergence_is_found(self):
        rows = [_row(i) for i in range(10)]
        rows[3]["imp_cost"] = rows[3]["cost_incl"] + 0.25      # 25p out
        r = bc.analyse(_db(rows))
        self.assertEqual(r["cost"]["differs"], 1)
        self.assertEqual(r["cost"]["examples"][0]["slot"], rows[3]["slot"])
        self.assertAlmostEqual(r["cost"]["examples"][0]["delta"], 0.25, places=6)

    def test_a_planted_ev_rate_divergence_is_found(self):
        rows = [_row(i) for i in range(10)]
        rows[5]["imp_rate_ev"] = DAY                            # billed off-peak, stored peak
        r = bc.analyse(_db(rows))
        self.assertEqual(r["ev_rate"]["differs"], 1)

    def test_a_mis_banded_slot_is_found(self):
        """The 2026-08-14 shape: the bill says peak, EMT stored off-peak."""
        rows = [_row(i) for i in range(9)]
        rows.append(_row(9, label="STANDARD_RATE", home_rate=DAY, ev_rate=DAY,
                         imp_rate=OFF,                       # EMT still off-peak
                         imp_rate_ev=DAY,
                         imp_cost=round(2.0 * OFF, 6), cost_incl=round(2.0 * OFF, 6)))
        r = bc.analyse(_db(rows))
        self.assertEqual(r["band"]["differs"], 1)
        self.assertEqual(r["band"]["examples"][0]["emt"], "off_peak")
        self.assertEqual(r["band"]["examples"][0]["bill"], "peak")


class TestZeroAndBias(unittest.TestCase):
    def test_zero_label_is_not_a_band(self):
        """ZERO means the bill confirmed no energy was drawn. An `else: peak`
        turns that into a peak verdict silently — found on a real database where
        20 of 910 rows were ZERO."""
        rows = [_row(i) for i in range(5)]
        rows[2].update(label="ZERO", cost_incl=0.0, imp_cost=0.0, imp_kwh=0.0,
                       home_kwh=0.0, ev_kwh=0.0)
        r = bc.analyse(_db(rows))
        self.assertEqual(r["band"]["differs"], 0)
        self.assertEqual(r["cost"]["trivial"], 1)
        self.assertEqual(r["cost"]["agrees"], 4, "a zero row must not pad agreement")

    def test_the_structural_bias_is_named(self):
        """The real corpus: every slot carrying EV energy was circular, so 100%
        agreement was agreement on house-only slots. The report has to say so
        rather than leaving the reader to notice."""
        rows = []
        for i in range(6):                       # EV slots, all bill-applied
            rows.append(_row(i, rate_source="measured", ev_kwh=1.5))
        for i in range(6, 12):                   # refereeable, but no EV energy
            rows.append(_row(i, ev_kwh=0.0, home_kwh=2.0))
        r = bc.analyse(_db(rows))
        self.assertEqual(r["corpus"]["refereeable_with_ev_energy"], 0)
        self.assertEqual(r["corpus"]["circular_with_ev_energy"], 6)
        self.assertTrue(any("STRUCTURAL BIAS" in n for n in r["notes"]),
                        "a corpus that cannot answer the question must say so")


class TestHonestyGuards(unittest.TestCase):
    def test_circular_rows_are_excluded_not_counted_as_agreement(self):
        """rate_source='measured' means v4 COPIED the bill in. Counting those as
        agreement is how a harness reports 100% and means nothing."""
        rows = [_row(i) for i in range(10)]
        for i in range(6):
            rows[i]["rate_source"] = "measured"
        r = bc.analyse(_db(rows))
        self.assertEqual(r["excluded_circular"], 6)
        self.assertEqual(r["cost"]["agrees"], 4)
        self.assertEqual(r["corpus"]["refereeable"], 4)
        self.assertTrue(any("circular" in n for n in r["notes"]))

    def test_user_corrected_blocks_are_circular_too(self):
        rows = [_row(i) for i in range(4)]
        rows[0]["rate_corrected"] = 1
        r = bc.analyse(_db(rows))
        self.assertEqual(r["excluded_circular"], 1)

    def test_missing_buckets_are_unknowable_not_passes(self):
        r = bc.analyse(_db([_row(i) for i in range(5)], buckets=False))
        self.assertFalse(r["buckets_available"])
        self.assertEqual(r["ev_rate"]["agrees"], 0)
        self.assertEqual(r["ev_rate"]["differs"], 0)
        self.assertTrue(any("unknowable" in n for n in r["notes"]))

    def test_mixed_label_is_unknowable_not_a_divergence(self):
        """A mixed slot spans bands, so a single stored rate cannot be judged
        against it either way."""
        rows = [_row(i) for i in range(5)]
        rows[2]["label"] = "mixed"
        r = bc.analyse(_db(rows))
        self.assertEqual(r["band"]["differs"], 0)
        self.assertGreaterEqual(r["band"]["unknowable"], 1)

    def test_ev_quantity_is_never_checked(self):
        """The bill over-attributes EV kWh on accounts with a concurrent grid
        load (v4's own comment). A harness that checked it would cry wolf on
        every battery account, so the wildest possible mismatch must be silent."""
        rows = [_row(i) for i in range(5)]
        rows[1]["ev_kwh"] = 99.0            # bill wildly over-attributes
        rows[1]["imp_kwh_ev"] = 1.5         # EMT clipped to the dispatched session
        r = bc.analyse(_db(rows))
        self.assertEqual(r["cost"]["differs"], 0)
        self.assertEqual(r["ev_rate"]["differs"], 0)
        blob = repr(r)
        self.assertNotIn("kwh_ev", blob, "EV quantity must not reach the report")

    def test_a_thin_corpus_says_so(self):
        r = bc.analyse(_db([_row(i) for i in range(3)]))
        self.assertTrue(any("thin corpus" in n for n in r["notes"]))


if __name__ == "__main__":
    unittest.main()
