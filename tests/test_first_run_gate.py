"""
The first-run gate: when is the FIRST block allowed to exist?

`ensure_correct_block` refuses to create a block until the install is
configured. The reason is stated in the code itself: before the wizard runs,
`get_block_minutes()` has nothing to read and returns the default 30, so a block
created now would "cause a block-size mismatch and racing after wizard saves".

The gate has two arms:

    if not has_sensors and not api_mode:
        return current_block          # pre-wizard: no block yet

`has_sensors` covers CAD. `api_mode` was added for API/Mini installs, which have
no local read sensor by design and would otherwise never form a block at all.

These tests pin both arms, and then pin the window BETWEEN them. The wizard's
finish sequence (`meter_config.html`, `wizApplyAndClose`) posts the data-source
mode BEFORE it posts the meters:

    await fetch('/api/data-source-mode', ...)   # (1) mode set
    await fetch('/api/billing-source', ...)     # (2)
    await fetch('/api/config', ...)             # (3) meters saved

The engine loop is already running and ticking every 10 seconds by then - it is
started unconditionally in `main.py`, not deferred until configuration exists.
So a tick landing between (1) and (3) sees `api_mode` True and an empty config,
and the gate opens on an install that is not yet configured.

test_the_setup_window_is_open is that state. It is written to FAIL once the gate
requires configuration as well as a mode; until then it records what the code
actually does.
"""
import os
import sys
import unittest
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import engine                                     # noqa: E402


class _NoCurrentBlockStore:
    """A store on a fresh install: no current block yet."""

    def load_current_block(self):
        return {}


_MAIN_WITH_SENSOR = {
    "meters": {
        "electricity_main": {
            "meta": {"block_minutes": 5},
            "channels": {"import": {"read": "sensor.house_import"}},
        }
    }
}

_MAIN_NO_SENSOR = {
    "meters": {
        "electricity_main": {
            "meta": {"block_minutes": 5},
            "channels": {"import": {"read": ""}},
        }
    }
}

_EMPTY = {"meters": {}}


class FirstRunGate(unittest.TestCase):

    def setUp(self):
        self._store = engine._store
        self._load_config = engine.load_config
        self._mode = engine.get_data_source_mode
        engine._store = _NoCurrentBlockStore()

    def tearDown(self):
        engine._store = self._store
        engine.load_config = self._load_config
        engine.get_data_source_mode = self._mode

    def _arrange(self, config, mode):
        engine.load_config = lambda *a, **k: config
        engine.get_data_source_mode = lambda *a, **k: mode

    def _tick(self):
        now = datetime(2026, 9, 23, 12, 3, 17)
        return engine.ensure_correct_block(None, {}, now)

    # -- the two arms, closed --------------------------------------------

    def test_cad_without_a_sensor_forms_no_block(self):
        """The original arm: nothing configured, nothing created."""
        self._arrange(_EMPTY, "unset")
        self.assertEqual(self._tick(), {})

    def test_a_configured_meter_with_no_sensor_still_forms_no_block(self):
        """`has_sensors` tests the sensor, not the meter's presence."""
        self._arrange(_MAIN_NO_SENSOR, "cad")
        self.assertEqual(self._tick(), {})

    # -- the two arms, open ----------------------------------------------

    def test_a_cad_sensor_opens_the_gate(self):
        self._arrange(_MAIN_WITH_SENSOR, "cad")
        block = self._tick()
        self.assertTrue(block.get("start"))
        self.assertEqual(block["block_minutes"], 5)

    def test_an_api_mode_opens_the_gate_with_a_seeded_shell(self):
        """API/Mini has no local sensor, so the block is seeded from config -
        otherwise finalise_block bails with 'nothing to finalise'."""
        self._arrange(_MAIN_NO_SENSOR, "api+mini")
        block = self._tick()
        self.assertTrue(block.get("start"))
        self.assertIn("electricity_main", block["meters"])
        self.assertEqual(block["block_minutes"], 5)

    # -- the window between them -----------------------------------------

    def test_the_setup_window_is_open(self):
        """Mode written (step 1), meters not yet (step 3). A tick here should
        form no block - the install is not configured. It currently forms one.

        The two symptoms are exactly the ones the CAD arm's comment names:

          * block_minutes is the default 30, not the 5 the wizard is about to
            save - the "block-size mismatch" - so the next tick computes a
            different window, rolls over, and finalises this stub into history;
          * seed_meters=True loops over an empty config, so the shell the seed
            exists to create is empty.
        """
        self._arrange(_EMPTY, "api+mini")
        block = self._tick()

        self.assertEqual(
            block, {},
            "gate opened on an unconfigured install: block_minutes=%s meters=%s"
            % (block.get("block_minutes"), block.get("meters")))


if __name__ == "__main__":
    unittest.main()
