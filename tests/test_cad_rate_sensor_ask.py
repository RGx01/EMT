"""
CAD-only asks for the sensors its prices come from (data management design §2 (a), 21 Oct 2026).

Without the supplier API every half-hour is priced from Home Assistant: the import rate, the standing
charge and, with export, the export rate. *Disconnect Octopus* lands on 'cad' and keeps the config as it
was, so nothing asked for them:
- the disconnect's modal said only "add a local meter sensor";
- Settings showed the fields but said nothing of an empty one;
- the wizard took an empty import rate without a word.

And a channel's "use the supplier API" choice outlived the API. `standing_charge_source = 'api'` forced
the standing charge to 0 over a mapped sensor, with no schedule left to supply it. That is the shape of
a staging copy's config: both sensors mapped, both sources 'api'.

Discriminating: the finalise test in CAD mode and the template tests fail on the parent. The API-mode
finalise test is a GUARD (the choice still wins there).
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))
import engine                                  # noqa: E402
from block_store import BlockStore             # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SENSOR_SC, SCHED_SC = 0.5512, 0.6001
RATE = 0.2450

META = {"sub_meter": False, "block_minutes": 30, "timezone": "UTC", "billing_day": 1,
        "currency_symbol": "£", "currency_code": "GBP"}
CFG = {"meters": {"electricity_main": {"meta": META, "channels": {
    "import": {"read": "sensor.imp", "rate": "sensor.rate", "standing_charge_sensor": "sensor.sc",
               "rate_source": "api", "standing_charge_source": "api"},
    "export": {"read": "", "rate": ""}}}}}
CB = {"start": "2026-10-20T13:00:00", "end": "2026-10-20T13:30:00", "meters": {"electricity_main": {
    "meta": META, "channels": {
        "import": {"reads": [{"ts": "2026-10-20T13:00:00", "value": 1000.0},
                             {"ts": "2026-10-20T13:30:00", "value": 1001.0}],
                   "rates": [{"ts": "2026-10-20T12:00:00", "value": RATE}]},
        "export": {"reads": [], "rates": []}}}}}


def _finalise(mode, sched_sc=None):
    s = BlockStore(":memory:")
    s.insert_config_period(CFG)
    out = []
    with patch.object(engine, "_store", s), \
         patch.object(engine, "append_block", lambda b: (out.append(b), s.append_block(b))), \
         patch.object(engine, "load_config", lambda: CFG), \
         patch.object(engine, "load_json", lambda path, default=None: CFG if "config" in str(path) else (default or {})), \
         patch.object(engine, "_backup_to_share", lambda: None), \
         patch.object(engine, "get_data_source_mode", lambda: mode), \
         patch.object(engine, "read_sensor", lambda ha, eid: SENSOR_SC if eid == "sensor.sc" else None), \
         patch.object(engine, "_kraken_standing_resolver", lambda ts: sched_sc), \
         patch.object(engine, "_kraken_rate_resolver", lambda ch, ts: None):
        engine.finalise_block(MagicMock(), block_data=CB)
    return out[-1]["meters"]["electricity_main"]


class AStaleApiChoice(unittest.TestCase):

    def test_cad_takes_the_mapped_standing_charge_sensor(self):
        """The disconnect left standing_charge_source='api' and cleared the schedule: the sensor was
        ignored and the day had no standing charge."""
        m = _finalise("cad")
        self.assertAlmostEqual(m["standing_charge"], SENSOR_SC, places=6)

    def test_an_api_mode_still_takes_the_schedule(self):
        """GUARD: with the API, the explicit choice wins over the sensor, as before."""
        m = _finalise("cad+api", sched_sc=SCHED_SC)
        self.assertAlmostEqual(m["standing_charge"], SCHED_SC, places=6)

    def test_cad_prices_from_the_rate_sensor(self):
        """GUARD: the stale rate_source fell through to the sensor before (no schedule); now it is
        not consulted at all."""
        m = _finalise("cad")
        self.assertAlmostEqual(m["channels"]["import"]["rate"], RATE, places=6)


def _page():
    with open(os.path.join(ROOT, "web", "templates", "meter_config.html"), encoding="utf-8") as f:
        return f.read()


class SetupAsks(unittest.TestCase):

    def test_settings_says_what_an_empty_sensor_does(self):
        mc = _page()
        self.assertIn("(usesApi ? '' : cadPricingNote(imp, exp))", mc)
        for text in ("costed at the last rate known, then at", "no standing charge is added"):
            self.assertIn(text, mc)

    def test_the_wizard_requires_the_import_rate_without_the_api(self):
        mc = _page()
        self.assertIn("curStep === 'sensors' && (wiz.data.mode || 'cad').indexOf('api') === -1", mc)
        self.assertIn("Please choose your Import Rate Sensor.", mc)

    def test_the_disconnect_names_the_rate_sensors(self):
        mc = _page()
        modal = mc[mc.index("Disconnect Octopus Modal"):mc.index('id="disconnect-confirm-btn"')]
        self.assertIn("import rate and standing charge", modal)


if __name__ == "__main__":
    unittest.main()
