"""
The device rebuild and the heatmap's month loads, beside each other (22 Oct 2026, staging).

After #265 the engine loop was still held up to 16.9 s during a device rebuild after a re-import.
The health count had waited, as #265 meant it to. The stack dumps showed two things at once:
- **The rebuild read the config from the database for every device row.** `_apply_pass2`'s #307
  ceiling wants the block's length. The rebuild's new device entry carries no `block_minutes` in its
  meta, so each row fell through to `get_block_minutes()` → `load_config()`. A stored block carries
  its config period's length (`_block_minutes`), which is now asked first.
- **Four heatmap months loaded at once**, each classifying and compressing ~2 MB, filling every web
  thread. The heatmap now loads at most two at a time, in the order asked.

Discriminating: every test here fails on the parent but the GUARD.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_engine  # noqa: F401  — installs stubs, imports engine
import engine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _block(**top):
    b = {"interpolated": False,
         "meters": {
             "electricity_main": {"meta": {"block_minutes": 30},
                                  "channels": {"import": {"kwh": 3.0, "rate": 0.30}}},
             # as the rebuild writes it (_write_device_into_block): no block_minutes
             "ev_charger": {"meta": {"sub_meter": True, "parent_meter": "electricity_main"},
                            "channels": {"import": {"kwh": 1.2, "rate": 0.30}}}}}
    b.update(top)
    return b


class TheCeiling(unittest.TestCase):

    def test_takes_the_blocks_own_length(self):
        with mock.patch.object(engine, "get_block_minutes",
                               side_effect=AssertionError("read the config")) as g:
            engine._apply_pass2(_block(_block_minutes=30))
        self.assertEqual(g.call_count, 0)

    def test_without_it_reads_the_config_as_before(self):
        """GUARD: a block with no length of its own still asks."""
        with mock.patch.object(engine, "get_block_minutes", return_value=30) as g:
            engine._apply_pass2(_block())
        self.assertGreaterEqual(g.call_count, 1)


class TheMonthLoads(unittest.TestCase):

    def test_two_at_a_time_in_order(self):
        with open(os.path.join(ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8") as f:
            hm = f.read()
        self.assertIn("var FETCH_MAX = 2, fetchQ = [], fetching = 0;", hm)
        fn = hm[hm.index("function pumpFetches() {"):]
        fn = fn[:fn.index("\n  }\n")]
        self.assertIn("while (fetching < FETCH_MAX && fetchQ.length) {", fn)
        self.assertIn("var j = fetchQ.shift(); fetching++;", fn)
        self.assertIn("fetchMonthNow(j.key).then(j.ok, j.bad).then(function () { fetching--; pumpFetches(); });", fn)


if __name__ == "__main__":
    unittest.main()
