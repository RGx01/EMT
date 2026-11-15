# -*- coding: utf-8 -*-
"""BL-40 (4.5.1): the physical-EV-meter identity must recognise a config-UI device typed
'ev' (not just 'ev_charger'), so the hybrid resolver folds it and it doesn't double against
the synthetic 'EV (from dispatch)'. Regression for the Indra case (meter_type='ev', hashed id).

14 Nov 2026 (meter_roles): the type decides and the id is read only for an untyped meter, so a
meter TYPED heat_pump is no EV whatever its id (test_id_fallback_only_without_a_type, which
asserted the old type-or-id rule). The function is imported, not cut from the source by regex."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from energy_charts import _ev_meter_id as ev_meter_id

def _cfg(mid, mtype):
    return {"meters": {mid: {"meta": {"meter_type": mtype}}}}

class TestEvMeterId(unittest.TestCase):
    def test_indra_ev_type_hashed_id(self):   # the reported bug
        self.assertEqual(ev_meter_id(_cfg("sub_meter_1782651008694733", "ev")),
                         "sub_meter_1782651008694733")
    def test_ev_charger_type(self):
        self.assertEqual(ev_meter_id(_cfg("sub_meter_x", "ev_charger")), "sub_meter_x")
    def test_zappi_canonical_id(self):         # matched via substring even pre-fix
        self.assertEqual(ev_meter_id(_cfg("ev_charger", "ev")), "ev_charger")
    def test_battery_not_matched(self):
        self.assertIsNone(ev_meter_id(_cfg("sub_meter_1783243773736116", "battery")))
    def test_id_fallback_only_without_a_type(self):
        self.assertEqual(ev_meter_id(_cfg("zappi_charger", None)), "zappi_charger")
        self.assertIsNone(ev_meter_id(_cfg("zappi_charger", "heat_pump")))
    def test_empty(self):
        self.assertIsNone(ev_meter_id({}))
        self.assertIsNone(ev_meter_id(None))

if __name__ == "__main__":
    unittest.main()
