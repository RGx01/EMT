"""
Engine step 5b (27 Oct 2026; EMT-v5_engine_step5_design.md §1): carbon's writers to one formula and
one lookup. The rule is v4's, unchanged (decided): a device's carbon is its own read × intensity, the
main's (`kwh_total` − export) × intensity.

- `_carbon_of` is the one formula: finalise, the startup gap fill and the recompute each had a copy;
- finalise attributes through `_attribute_block_carbon` and its 60-minute bound — its own copy took
  the nearest carbon-intensity row whatever its distance, so a stalled fetch gave a closing block a
  far-off slot's intensity;
- the startup gap fill's own unbounded copy goes: `build_gap_blocks` has already attributed them.

Discriminating: every test here fails on the parent, but those labelled GUARD — TheFormula's first
two only by name (`_carbon_of` is new; the rule they pin is v4's, unchanged).
"""
import ast
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import test_rebuild_meter_type as H              # noqa: E402  (stubs, real engine, flat tariff)
engine = H.engine
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read()


def _fn(name):
    return next(n for n in ast.walk(ast.parse(SRC))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _calls(name):
    return {getattr(c.func, "id", getattr(c.func, "attr", None))
            for c in ast.walk(_fn(name)) if isinstance(c, ast.Call)}


class TheFormula(unittest.TestCase):

    def test_the_main_nets_its_export(self):
        mb = {"meta": {"sub_meter": False},
              "channels": {"import": {"kwh": 1.0, "kwh_total": 3.0}, "export": {"kwh": 0.5}}}
        self.assertEqual(engine._carbon_of(mb, 200.0), 500.0)

    def test_a_device_is_its_own_read(self):
        """As v4: the gross figure, whatever supplied it (a grid share of 0.3 is not read)."""
        mb = {"meta": {"sub_meter": True},
              "channels": {"import": {"kwh": 2.0, "kwh_grid": 0.3}}}
        self.assertEqual(engine._carbon_of(mb, 200.0), 400.0)

    def test_it_is_written_once(self):
        self.assertEqual(len(re.findall(r"\* *_?intensity, 4\)", SRC)), 2,
                         "the formula's two lines, in _carbon_of only")


class FinaliseIsBounded(H._Harness):

    def _ci_at(self, ts, intensity=250.0):
        engine._store.upsert_carbon_intensity(ts, "DE1", intensity, "high", None)

    def _main_intensity(self):
        return engine._store._conn.execute(
            "SELECT carbon_intensity_g FROM blocks WHERE block_start = ? "
            "AND meter_id = 'electricity_main'", (H.START,)).fetchone()[0]

    def test_a_far_off_slot_is_refused(self):
        self._ci_at("2026-07-12T12:30:00")                 # three hours before the half-hour
        with mock.patch.object(engine, "_get_postcode", lambda: "DE1"):
            self._finalise()
        self.assertIsNone(self._main_intensity(), "left for the recovery sweep")

    def test_GUARD_the_half_hours_own_slot_is_taken(self):
        self._ci_at(H.START)
        with mock.patch.object(engine, "_get_postcode", lambda: "DE1"):
            self._finalise()
        self.assertEqual(self._main_intensity(), 250.0)


class TheCopiesAreGone(unittest.TestCase):

    def test_finalise_and_startup_go_through_the_lookup(self):
        self.assertIn("_attribute_block_carbon", _calls("finalise_block"))
        self.assertNotIn("get_nearest_carbon_intensity", _calls("finalise_block"))
        self.assertNotIn("get_nearest_carbon_intensity", _calls("_engine_startup_impl"))


if __name__ == "__main__":
    unittest.main()
