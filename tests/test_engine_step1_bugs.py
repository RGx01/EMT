"""
Latent bugs found by the engine review (docs/design/EMT-v5_engine_review.md §3.5), fixed in step 1
(22 Oct 2026). Each was read in the code; none had been seen to fire.

1. run_deferred_verify_pricing: the `else:` reading the cursor back belonged to the span write's
   `try`, not to `if restart:`. A failed span write on a resumed pass left `cursor` unbound; a failed
   cursor write on a restart resumed from the OLD cursor instead of the newest edge.

Discriminating: every test here fails on the parent.
"""
import asyncio
import os
import sys
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine                                      # noqa: E402
from test_verify_defers_to_import import _Base, T0, DAYS   # noqa: E402


class TheVerifyCursor(_Base):

    def _failing(self, key):
        real = self.st.set_kraken_state

        def f(k, v):
            if k == key:
                raise RuntimeError("disk full")
            return real(k, v)
        self.st.set_kraken_state = f

    def test_a_resumed_pass_survives_a_failed_span_write(self):
        mid = (T0 + timedelta(days=40)).isoformat()
        self.st.set_kraken_state(engine._VERIFY_CURSOR_KEY, mid)
        self._failing(engine._VERIFY_SPAN_KEY)
        self._pass(restart=False)                   # raised UnboundLocalError on the parent
        self.assertTrue(self.chunks)
        self.assertLessEqual(self.chunks[0][1][:10], mid[:10])   # from the saved cursor down

    def test_a_restart_starts_at_the_top_even_when_the_cursor_write_fails(self):
        self.st.set_kraken_state(engine._VERIFY_CURSOR_KEY, (T0 + timedelta(days=10)).isoformat())
        self._failing(engine._VERIFY_CURSOR_KEY)
        self._pass(restart=True)
        top = (T0 + timedelta(days=DAYS)).isoformat()[:10]
        self.assertGreaterEqual(self.chunks[0][1][:10], top)     # the newest edge, not 10 days in


if __name__ == "__main__":
    unittest.main()
