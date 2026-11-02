"""
The made-up household the screenshots are taken from (`.build/demo_db.py`, 2 Nov 2026: the repository going public).

It builds through EMT's own device split and settle sweep, names "Demo House" and the placeholder postcode SW1A, and
holds nothing that looks like a real meter point, meter serial or account: no 13-digit number, no `A-XXXXXXXX`,
anywhere in the database. Its devices sit inside the main meter, and the same seed gives the same figures.

Discriminating: every test fails on the parent tree (the builder is new).
"""
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TheDemo(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        res = subprocess.run([sys.executable, os.path.join(_ROOT, ".build", "demo_db.py"), cls.dir],
                             capture_output=True, text=True, timeout=600)
        assert res.returncode == 0, res.stderr[-2000:]
        cls.db = sqlite3.connect(os.path.join(cls.dir, "blocks.db"))

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        shutil.rmtree(cls.dir)

    def test_it_names_no_one(self):
        self.assertEqual(self.db.execute("SELECT site_name FROM config_periods").fetchone()[0], "Demo House")
        text = "\n".join(self.db.iterdump())
        self.assertIsNone(re.search(r"(?<![\d.])\d{13}(?![\d.])", text))
        self.assertIsNone(re.search(r"\bA-[0-9A-F]{8}\b", text))
        self.assertIn("SW1A", text)

    def test_two_and_a_half_years_with_its_devices(self):
        n, lo, hi = self.db.execute("SELECT COUNT(*), MIN(block_start), MAX(block_start) FROM blocks "
                                    "WHERE meter_id = 'electricity_main'").fetchone()
        self.assertEqual((lo[:10], hi[:10]), ("2024-05-31", "2026-10-31"))
        self.assertGreater(n, 42000)
        devices = {r[0] for r in self.db.execute("SELECT DISTINCT meter_id FROM blocks")}
        self.assertEqual(devices, {"electricity_main", "ev_charger", "heat_pump", "house_battery"})

    def test_the_devices_sit_inside_the_meter(self):
        over = self.db.execute(
            "SELECT COUNT(*) FROM (SELECT m.block_start, m.imp_kwh AS main, SUM(d.imp_kwh_grid) AS dev "
            "FROM blocks m JOIN blocks d ON d.block_start = m.block_start AND d.meter_id != 'electricity_main' "
            "WHERE m.meter_id = 'electricity_main' GROUP BY m.block_start) WHERE dev > main + 1e-6").fetchone()[0]
        self.assertEqual(over, 0)

    def test_the_same_seed_the_same_figures(self):
        kwh = self.db.execute("SELECT ROUND(SUM(imp_kwh), 3) FROM blocks WHERE meter_id = 'electricity_main' "
                              "AND block_start LIKE '2025%'").fetchone()[0]
        self.assertAlmostEqual(kwh, 10280, delta=1)


if __name__ == "__main__":
    unittest.main()
