"""
`drain_tick()` — what `run_exclusive` actually was.

BL-48 introduced `run_exclusive` so a device-delete/recompute could not overlap an
in-flight capture/finalise tick on the shared SQLite connection. Its one caller
passed `lambda: None`: it never ran anything under the lock, it only waited for the
tick to let go. So the surviving behaviour is a barrier, and it is named as one.

The primitive itself is deleted — B §6 q6. It ran its function on an executor
thread, which A §3.3 forbids; it excluded only the tick, not the fourteen other
writers on the loop; and what closed that gap was a docstring telling callers to
pause and drain first.
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine                                        # noqa: E402


class TestDrainTick(unittest.TestCase):

    def tearDown(self):
        engine._engine_loop_lock = None

    def test_it_waits_for_the_lock_and_releases_it(self):
        engine._engine_loop_lock = asyncio.Lock()
        asyncio.run(engine.drain_tick())
        self.assertFalse(engine._engine_loop_lock.locked(),
                         "the lock must not be left held — this drains, it does "
                         "not hold the next tick off")

    def test_it_blocks_until_an_in_flight_tick_finishes(self):
        """The whole point: `pause_engine()` stops the NEXT tick, not one already
        running. A caller that deletes data needs the running one finished."""
        async def _go():
            engine._engine_loop_lock = asyncio.Lock()
            order = []

            async def _tick():
                async with engine._engine_loop_lock:
                    order.append("tick-start")
                    await asyncio.sleep(0.05)
                    order.append("tick-end")

            t = asyncio.create_task(_tick())
            await asyncio.sleep(0)          # let the tick take the lock
            await engine.drain_tick()
            order.append("drained")
            await t
            return order

        order = asyncio.run(_go())
        self.assertEqual(order[:2], ["tick-start", "tick-end"])
        self.assertEqual(order[-1], "drained",
                         "drain_tick returned before the tick finished")

    def test_no_lock_is_a_no_op(self):
        """Tests and pre-init have no loop lock; there is no tick to drain."""
        engine._engine_loop_lock = None
        asyncio.run(engine.drain_tick())        # must not raise

    def test_run_exclusive_is_gone(self):
        """§4a deletion. If it comes back, B §6 q6 needs re-arguing rather than
        the primitive quietly returning."""
        self.assertFalse(hasattr(engine, "run_exclusive"))


if __name__ == "__main__":
    unittest.main()
