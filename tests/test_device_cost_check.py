"""
The device cost check (device cost check design, 31 Oct 2026): a settled half-hour whose devices cost
more than the meter.

Found on one install's v4 history, 14 Sep 2026: three half-hours corrected by hand to off-peak (the
bill agreed), their devices left on the peak rate, and v4's 4.5.21 heal carrying only their ex-VAT
side. The bill total was right; the device breakdown read too high. Nothing repaired it:
every automatic pass skips a corrected half-hour.

What they pin:

- **The rule** (`settlement_state.devices_exceed`): over by more than a thousandth of a penny, on a
  settled half-hour, compared as magnitudes. 2e-6 is rounding (seen on real databases), and an
  unsettled live half-hour can be over until settlement re-clips it.
- **The flag** goes on the half-hour's states, and a corrected half-hour's cell turns red
  (`settleValue`), with the fix named in the flag's text.
- **A re-fetch lists it** even where Octopus agrees with the meter, and **its apply prices the
  devices again**: on their grid shares at the half-hour's rate, so they no longer exceed the meter.
  For a corrected one, Octopus's bill replaces the correction.
- **The importer reports** what it carries over, and writes nothing.

Discriminating: every test fails on the parent (no rule, flag, re-fetch reason or report), except
test_sound_devices_are_no_difference, a GUARD.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import settlement_state as SS                                      # noqa: E402
from tests.test_range_refetch import _Harness, _Octopus, _node, SLOT, KWH, OFF   # noqa: E402
from block_store import BlockStore                                 # noqa: E402
import engine                                                      # noqa: E402

PEAK = 0.323092


def _facts(**kw):
    f = {"block_start": SLOT, "imp_kwh": KWH, "imp_kwh_api": KWH, "imp_cost": 0.366493,
         "source": "kraken_api", "rate_source": "settled", "devices_cost": None}
    f.update(kw)
    return f


class TheRule(unittest.TestCase):

    def test_devices_over_the_meter_are_caught(self):
        self.assertTrue(SS.devices_exceed(_facts(devices_cost=0.969276)))

    def test_rounding_is_not(self):
        self.assertFalse(SS.devices_exceed(_facts(devices_cost=0.366495)))      # 2e-6 over
        self.assertTrue(SS.devices_exceed(_facts(devices_cost=0.366493 + 0.00002)))

    def test_only_a_settled_half_hour(self):
        self.assertFalse(SS.devices_exceed(_facts(imp_kwh_api=None, devices_cost=0.9)))
        self.assertTrue(SS.devices_exceed(_facts(imp_kwh_api=None, source="imported_api",
                                                 devices_cost=0.9)))

    def test_a_negative_price_reads_the_right_way_round(self):
        self.assertFalse(SS.devices_exceed(_facts(imp_cost=-0.30, devices_cost=-0.25)))
        self.assertTrue(SS.devices_exceed(_facts(imp_cost=-0.30, devices_cost=-0.35)))

    def test_no_devices_no_flag(self):
        self.assertFalse(SS.devices_exceed(_facts()))

    def test_it_is_a_flag_on_the_half_hours_states(self):
        st = SS.classify(_facts(rate_source="corrected", rate_corrected=1, devices_cost=0.969276),
                         smb=True, now=datetime(2026, 10, 31))
        self.assertEqual(st["bill"], "corrected")
        self.assertIn("devices_exceed", st["flags"])


class ThePage(unittest.TestCase):

    def test_the_cell_turns_red_whatever_the_state(self):
        page = open(os.path.join(_ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8").read()
        fn = page[page.index("function settleValue(m, st) {"):]
        fn = fn[:fn.index("\n  }\n")]
        self.assertIn("if (fl.indexOf('devices_exceed') >= 0 && v !== 'none' && v !== 'missing') "
                      "return 'flagged';", fn)
        self.assertIn("devices_exceed: 'the devices cost more than the meter", page)
        self.assertIn("Re-fetch it to price ' +", page)


class TheReFetch(_Harness):
    """The harness's store, with a heat pump on the half-hour: the meter as its bill says, the device
    on the PEAK rate — 14 Sep's shape."""

    def setUp(self):
        super().setUp()
        cfg = {"meters": {
            "electricity_main": {"meta": {"timezone": "Europe/London", "billing_day": 1,
                                          "block_minutes": 30, "currency_symbol": "£",
                                          "currency_code": "GBP"},
                                 "channels": {"import": {}}},
            "heat_pump": {"meta": {"sub_meter": True, "parent_meter": "electricity_main",
                                   "device": "Pump", "meter_type": "heat_pump"},
                          "channels": {"import": {}}}}}
        st = BlockStore(":memory:")
        st.insert_config_period(cfg)
        engine._store = self.st = st

    def _device(self, rate=PEAK, kwh=3.0):
        self.st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "imp_kwh_grid, imp_rate, imp_cost, rate_source) VALUES (?,?,?,1,?,?,?,?,'schedule')",
            (SLOT, "2026-09-01T02:30:00", "heat_pump", kwh, kwh, rate, round(kwh * rate, 6)))
        self.st._conn.commit()

    def _row(self, meter):
        return self.st._conn.execute("SELECT imp_rate, imp_cost, rate_source, imp_kwh_grid FROM blocks "
                                     "WHERE block_start=? AND meter_id=?", (SLOT, meter)).fetchone()

    def test_a_flagged_half_hour_is_listed_and_its_devices_priced_again(self):
        self._block()
        self._device()
        (fact,) = self.st.get_settlement_facts("2026-09-01T00:00:00", "2026-09-02T00:00:00")
        self.assertTrue(SS.devices_exceed(fact))
        st = self._preview(_Octopus(node=_node(KWH, 0.0)))
        (row,) = st["rows"]
        self.assertTrue(row["bill"]["devices"])
        self.assertEqual(st["summary"]["devices"], 1)
        self._apply(st["token"])
        rate, cost, src, grid = self._row("heat_pump")
        self.assertAlmostEqual(rate, OFF, places=5)
        self.assertAlmostEqual(cost, round(grid * OFF, 6), places=6)
        main_cost = self._row("electricity_main")[1]
        self.assertLessEqual(cost, main_cost + 1e-5)
        (fact,) = self.st.get_settlement_facts("2026-09-01T00:00:00", "2026-09-02T00:00:00")
        self.assertFalse(SS.devices_exceed(fact))

    def test_a_corrected_one_takes_the_bill_and_its_devices_follow(self):
        self._block(rate_source="corrected")
        self._device()
        st = self._preview(_Octopus(node=_node(KWH, 0.0)))
        (row,) = st["rows"]
        self.assertTrue(row["bill"]["devices"] and row["bill"]["corrected"])
        self._apply(st["token"])
        self.assertEqual(self._row("electricity_main")[2], "settled")
        self.assertAlmostEqual(self._row("heat_pump")[0], OFF, places=5)

    def test_sound_devices_are_no_difference(self):
        """GUARD: a device on the meter's rate leaves an unchanged half-hour unchanged."""
        self._block()
        self._device(rate=OFF)
        self.assertEqual(self._preview(_Octopus(node=_node(KWH, 0.0)))["summary"]["differ"], 0)


class TheImporter(unittest.TestCase):

    def test_it_reports_and_writes_nothing(self):
        from tests.test_v4import import v4_db
        import v4import
        d = tempfile.mkdtemp()
        src, dst = os.path.join(d, "v4.db"), os.path.join(d, "v5.db")
        v4_db(src)
        c = sqlite3.connect(src)
        c.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter) VALUES (1, 'pump', 1)")
        main = c.execute("SELECT block_start, imp_cost FROM blocks WHERE meter_id = 'electricity_main' "
                         "AND imp_kwh > 0 ORDER BY block_start LIMIT 1").fetchone()
        c.execute("UPDATE blocks SET imp_kwh_api = imp_kwh WHERE meter_id = 'electricity_main' "
                  "AND block_start = ?", (main[0],))                    # DCC-settled, as 14 Sep's were
        c.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
                  "imp_kwh_grid, imp_rate, imp_cost) VALUES (?, ?, 'pump', 1, 1.0, 1.0, 1.0, ?)",
                  (main[0], main[0], main[1] + 0.5))
        c.commit(); c.close()
        report = v4import.import_v4(src, dst)
        self.assertEqual(report["transforms"]["device_costs"], {"reported": [main[0]]})
        got = sqlite3.connect(dst).execute("SELECT imp_cost FROM blocks WHERE meter_id = 'pump'").fetchone()[0]
        self.assertAlmostEqual(got, main[1] + 0.5, places=6)         # carried as it was


if __name__ == "__main__":
    unittest.main()
