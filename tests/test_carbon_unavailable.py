"""
The historical carbon backfill gives up on half-hours the source has no intensity for.

On the soak Pi (20 Nov 2026) the backfill warned every day about the same 113 imported
half-hours in four runs (one in Oct 2023, one in Jun 2024, two in 2025): the national Carbon
Intensity API has nothing for them, so every pass filled nothing and cooled down for 24
hours, for ever. Now a slot that comes back empty from fetches that SUCCEEDED, on three
passes at least 12 hours apart, is recorded in store_meta `carbon_unavailable`, never
asked for again, and named once at INFO. The block keeps its NULL carbon (charts show it
as missing). New NULL slots are still fetched; a recent slot, a failed fetch, or rapid
retries never count; a region change asks again.

Every test here fails on the parent (no record, no `exclude`) except
test_failed_fetch_never_counts, which is a guard. The defect itself is pinned by
test_gives_up_after_three_passes and test_marked_slot_is_not_asked_again.
"""
import sys
import os
import types
import unittest
import asyncio
from unittest.mock import MagicMock

# ── Minimal stubs so engine.py imports without HA/filesystem ─────────────────
eio = types.ModuleType("energy_engine_io")
eio.ensure_dir = lambda *a, **kw: None
eio.load_json = lambda *a, **kw: a[1] if len(a) > 1 else {}
eio.save_json_atomic = lambda *a, **kw: None
eio.save_file = lambda *a, **kw: None
sys.modules["energy_engine_io"] = eio

ec = types.ModuleType("energy_charts")
sys.modules["energy_charts"] = ec

hc = types.ModuleType("ha_client")
hc.HAClient = MagicMock
sys.modules["ha_client"] = hc

from block_store import BlockStore, open_block_store
_boot_store = BlockStore(":memory:")
_boot_store.insert_config_period({"meters": {"electricity_main": {"meta": {
    "timezone": "UTC", "billing_day": 1, "block_minutes": 30,
    "currency_symbol": "£", "currency_code": "GBP"}}}})

bs = types.ModuleType("block_store")
bs.BlockStore = BlockStore
bs.open_block_store = lambda path: _boot_store
sys.modules["block_store"] = bs

sys.path.insert(0, os.path.dirname(__file__))
import engine
engine._store = _boot_store


# ── Helpers ──────────────────────────────────────────────────────────────────
def _store_with_postcode(postcode="DE1"):
    st = BlockStore(":memory:")
    meta = {"timezone": "Europe/London", "billing_day": 1, "block_minutes": 30,
            "currency_symbol": "£", "sub_meter": False}
    if postcode:
        meta["postcode_prefix"] = postcode
    st.insert_config_period({"meters": {"electricity_main": {"meta": meta}}})
    return st


def _cp_id(st):
    return st._conn.execute("SELECT id FROM config_periods LIMIT 1").fetchone()["id"]


def _insert_null_block(st, start, imp_kwh, cp):
    st._conn.execute(
        "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
        "interpolated, imp_kwh, imp_rate, imp_cost, standing_charge, "
        "carbon_intensity_g) VALUES (?,?,?,?,0,?,?,?,?,NULL)",
        (start, start, "electricity_main", cp,
         imp_kwh, 0.30, round(imp_kwh * 0.30, 4), 0.50))
    st._conn.commit()


def _carbon_of(st, start):
    r = st._conn.execute(
        "SELECT carbon_intensity_g, carbon_g FROM blocks WHERE block_start=?",
        (start,)).fetchone()
    return (r["carbon_intensity_g"], r["carbon_g"]) if r else (None, None)


# ── This file's helpers ──────────────────────────────────────────────────────
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

KEY = "carbon_unavailable"
MARKER = "carbon_backfill_state"
OLD_RUN = ["2023-10-21T00:00:00", "2023-10-21T00:30:00", "2023-10-21T01:00:00"]


def _age_misses(st, hours=13):
    """Move every counted miss `hours` into the past — a day's cool-down, compressed."""
    ua = st.get_meta(KEY, {}) or {}
    for e in (ua.get("misses") or {}).values():
        e["last"] = (_dt.fromisoformat(e["last"]) - _td(hours=hours)).isoformat()
    st.set_meta(KEY, ua)


def _rearm(st):
    """What _maybe_backfill_historical_carbon does once retry_after has passed."""
    st.set_meta(MARKER, {})


class _Spy:
    def __init__(self, data=None, raise_=False):
        self.calls, self.data, self.raise_ = [], data or {}, raise_

    def __call__(self, pc, f, t):
        self.calls.append((pc, f, t))
        if self.raise_:
            raise RuntimeError("simulated network error")
        return dict(self.data)


class TestCarbonUnavailable(unittest.TestCase):
    def setUp(self):
        self.st = _store_with_postcode("DE1")
        self.cp = _cp_id(self.st)
        self._orig_store = engine._store
        self._orig_fetch = engine._fetch_carbon_intensity_range
        engine._store = self.st
        engine._carbon_backfill_running = False
        for s in OLD_RUN:
            _insert_null_block(self.st, s, 1.0, self.cp)

    def tearDown(self):
        engine._store = self._orig_store
        engine._fetch_carbon_intensity_range = self._orig_fetch

    def _pass(self, spy):
        engine._fetch_carbon_intensity_range = spy
        return asyncio.run(engine._run_historical_carbon_backfill())

    def _three_empty_passes(self):
        for i in range(3):
            if i:
                _age_misses(self.st)
                _rearm(self.st)
            self.assertEqual(self._pass(_Spy()), 0)

    def test_gives_up_after_three_passes(self):
        with self.assertLogs(engine.logger, level="INFO") as cm:
            self._three_empty_passes()
        ua = self.st.get_meta(KEY, {}) or {}
        self.assertEqual(sorted(ua.get("unavailable", {})), OLD_RUN)
        self.assertEqual(set(ua["unavailable"].values()), {"DE1"})
        self.assertEqual(ua.get("misses"), {})
        mk = self.st.get_meta(MARKER)
        self.assertTrue(mk.get("done"))
        self.assertNotIn("unfilled_from", mk, "the third pass completes, it does not cool down")
        named = [m for m in cm.output if "no longer asking" in m]
        self.assertEqual(len(named), 1, cm.output)
        self.assertIn("2023-10-21T00:00→2023-10-21T01:00 (3)", named[0])
        self.assertTrue(named[0].startswith("INFO:"))
        # The blocks keep NULL: charts treat them as missing, never zero.
        for s in OLD_RUN:
            self.assertEqual(_carbon_of(self.st, s), (None, None))

    def test_marked_slot_is_not_asked_again(self):
        self._three_empty_passes()
        _rearm(self.st)                       # even a re-arm (a restart, an import)
        spy = _Spy()
        self.assertEqual(self._pass(spy), 0)
        self.assertEqual(spy.calls, [], "a slot given up on must not be fetched again")
        self.assertTrue(self.st.get_meta(MARKER).get("done"))
        # and the scheduler's re-arm check sees nothing left to do
        self.st.set_meta(MARKER, {"done": True, "completed_at": "x"})
        self.assertIsNone(self.st.get_missing_carbon_date_range(
            exclude=self.st.get_meta(KEY)["unavailable"]))

    def test_new_null_slot_still_fetched(self):
        self._three_empty_passes()
        _insert_null_block(self.st, "2025-08-11T00:00:00", 2.0, self.cp)
        _rearm(self.st)
        spy = _Spy({"2025-08-11T00:00": 100.0})
        self.assertEqual(self._pass(spy), 1)
        self.assertEqual(_carbon_of(self.st, "2025-08-11T00:00:00"), (100.0, 200.0))
        self.assertTrue(all(f.startswith("2025-08-11") for _, f, _t in spy.calls), spy.calls)

    def test_rapid_retries_count_once(self):
        for _ in range(4):                    # no 12 hours between them
            _rearm(self.st)
            self._pass(_Spy())
        ua = self.st.get_meta(KEY, {}) or {}
        self.assertEqual(ua.get("unavailable", {}), {})
        self.assertEqual({e["n"] for e in ua["misses"].values()}, {1})

    def test_failed_fetch_never_counts(self):
        for i in range(4):
            if i:
                _rearm(self.st)
            self._pass(_Spy(raise_=True))
        self.assertEqual(self.st.get_meta(KEY, None), None)

    def test_recent_slot_never_given_up(self):
        recent = (_dt.now(_tz.utc).replace(tzinfo=None) - _td(days=2)).replace(
            hour=0, minute=0, second=0, microsecond=0).isoformat()
        _insert_null_block(self.st, recent, 1.0, self.cp)
        self._three_empty_passes()
        ua = self.st.get_meta(KEY)
        self.assertNotIn(recent, ua["unavailable"])
        self.assertNotIn(recent, ua["misses"])

    def test_filled_slot_drops_its_count(self):
        self._pass(_Spy())
        _age_misses(self.st)
        _rearm(self.st)
        self._pass(_Spy({s[:16]: 150.0 for s in OLD_RUN}))
        self.assertEqual(self.st.get_meta(KEY)["misses"], {})

    def test_region_change_asks_again(self):
        self._three_empty_passes()
        ua = self.st.get_meta(KEY)
        ua["unavailable"] = {s: "XX9" for s in ua["unavailable"]}   # asked for another region
        self.st.set_meta(KEY, ua)
        _rearm(self.st)
        spy = _Spy({s[:16]: 150.0 for s in OLD_RUN})
        self.assertEqual(self._pass(spy), 3)
        self.assertEqual(self.st.get_meta(KEY)["unavailable"], {})

    def test_runs_named(self):
        self.assertEqual(engine._carbon_runs(
            ["2024-06-12T00:00:00", "2023-10-20T23:00:00", "2023-10-20T23:30:00",
             "2024-06-12T00:30:00", "2024-06-12T01:30:00"]),
            "2023-10-20T23:00→2023-10-20T23:30 (2), 2024-06-12T00:00→2024-06-12T00:30 (2), "
            "2024-06-12T01:30→2024-06-12T01:30 (1)")


class TestStoreExclude(unittest.TestCase):
    def setUp(self):
        self.st = _store_with_postcode("DE1")
        self.cp = _cp_id(self.st)
        for s in ("2023-10-21T00:00:00", "2024-06-12T00:00:00",
                  "2025-01-13T00:00:00", "2025-08-11T00:00:00"):
            _insert_null_block(self.st, s, 1.0, self.cp)

    def test_store_excludes(self):
        ex = {"2023-10-21T00:00:00": "DE1", "2025-08-11T00:00:00": "DE1"}
        self.assertEqual(self.st.get_missing_carbon_date_range(exclude=ex),
                         ("2024-06-12T00:00:00", "2025-01-13T00:00:00"))
        self.assertEqual(self.st.get_block_starts_missing_carbon_in_range(
            "2023-01-01", "2026-01-01", exclude=ex),
            ["2024-06-12T00:00:00", "2025-01-13T00:00:00"])
        self.assertEqual(self.st.get_block_starts_missing_carbon_in_range(
            "2023-01-01", "2026-01-01", limit=1, exclude=ex), ["2024-06-12T00:00:00"])
        self.assertIsNone(self.st.get_missing_carbon_date_range(exclude={
            "2023-10-21T00:00:00", "2024-06-12T00:00:00",
            "2025-01-13T00:00:00", "2025-08-11T00:00:00"}))
        # no exclude → unchanged
        self.assertEqual(self.st.get_missing_carbon_date_range(),
                         ("2023-10-21T00:00:00", "2025-08-11T00:00:00"))


if __name__ == "__main__":
    unittest.main()
