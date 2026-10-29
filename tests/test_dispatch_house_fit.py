"""
4.5.21 — on a DISPATCH-ONLY account the devices fit inside the house (grid − the car).

An account with Octopus's dispatch EV on the main (`kwh_ev`) and no EV meter: PASS 2 fitted the
devices against the WHOLE main, never reading `kwh_ev`, and nothing re-fitted them once the
completed dispatch arrived. A battery charging alongside the car kept grid the car used; the EV
then had nothing left to sit in (the bill capped it, Insights' house went negative).

Now the car's grid is reserved first and the devices fit in what is left; the remainder keeps its
shape (grid − devices, the EV inside it) and can no longer be smaller than the EV. Each later
writer of `kwh_ev` re-fits the devices (`_refit_devices_to_house`), leaving a measured block's
bill on the main untouched. With an EV meter the meter is the EV and nothing changes. (Step 5,
10 Oct 2026: the remainder is no longer stored; on a loaded row it is derived as the main's
imp_kwh − the devices' grid shares, which is how these tests read it.)

Ported from v4 4.5.21 without its heal (`dispatch_house_refit_done`); the 4.5.21 import floor means
a database arrives already re-fitted. Engine step 4e (27 Oct 2026): the re-fit on the EV's arrival
(`_refit_devices_to_house`, `dispatch_house_candidates`, `write_resplit`) went — the arrival is a
completed dispatch, the carve settles the half-hour, and settle() reserves the main's EV before the
devices as PASS 2 does (test_settle.py); its tests went with it.

No local database is a dispatch-only account with devices, so these fixtures are synthetic.
Discriminating: test_the_car_is_reserved_before_the_devices fails on 4.5.20; the re-fit tests
exercise new code. The EV-meter and no-dispatch cases are guards.
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import test_rebuild_meter_type as H              # noqa: E402  (stubs + harness)
engine = H.engine

MAIN, BATT, EV = 8.0, 7.0, 2.86                  # the car drew 2.86; the battery 7.0; grid 8.0


def _block(ev_kwh=EV, ev_meter_type=None):
    """A block dict: the main carries the dispatch EV; a battery; optionally an EV meter."""
    meters = {
        "electricity_main": {
            "meta": {"sub_meter": False},
            "channels": {"import": {"kwh": MAIN, "kwh_total": MAIN, "rate": 0.245,
                                    "cost": round(MAIN * 0.245, 6), "kwh_ev": ev_kwh}}},
        "house_battery": {
            "meta": {"sub_meter": True, "meter_type": "battery", "parent_meter": "electricity_main"},
            "channels": {"import": {"kwh": BATT, "rate": 0.245, "cost": round(BATT * 0.245, 6)}}},
    }
    if ev_meter_type:
        meters["ev_charger"] = {
            "meta": {"sub_meter": True, "meter_type": ev_meter_type, "parent_meter": "electricity_main"},
            "channels": {"import": {"kwh": EV, "rate": 0.245, "cost": round(EV * 0.245, 6)}}}
    return {"start": "2026-07-12T15:30:00", "end": "2026-07-12T16:00:00", "interpolated": False,
            "meters": meters}


def _grid(blk, mid):
    return blk["meters"][mid]["channels"]["import"]["kwh_grid"]


def _rem(blk):
    return blk["meters"]["electricity_main"]["channels"]["import"]["kwh_remainder"]


class TheSplit(unittest.TestCase):

    def test_the_car_is_reserved_before_the_devices(self):
        blk = _block()
        engine._apply_pass2(blk)
        self.assertAlmostEqual(_grid(blk, "house_battery"), MAIN - EV, places=6)
        self.assertAlmostEqual(_rem(blk), EV, places=6)        # grid − devices, the car inside it

    def test_an_ev_meter_is_the_ev(self):
        """GUARD: with an EV meter the meter claims first and the dispatch figure is not reserved
        again — unchanged from 4.5.20 (for an `ev`- or `ev_charger`-typed meter alike)."""
        for t in ("ev", "ev_charger"):
            with self.subTest(meter_type=t):
                blk = _block(ev_meter_type=t)
                engine._apply_pass2(blk)
                self.assertAlmostEqual(_grid(blk, "house_battery") + _grid(blk, "ev_charger"),
                                       MAIN, places=6)
                self.assertAlmostEqual(_rem(blk), 0.0, places=6)

    def test_no_dispatch_is_unchanged(self):
        """GUARD: no dispatch EV, nothing reserved."""
        blk = _block(ev_kwh=None)
        engine._apply_pass2(blk)
        self.assertAlmostEqual(_grid(blk, "house_battery"), BATT, places=6)
        self.assertAlmostEqual(_rem(blk), MAIN - BATT, places=6)

    def test_a_dispatch_larger_than_the_main_is_capped_at_it(self):
        blk = _block(ev_kwh=MAIN + 5)
        engine._apply_pass2(blk)
        self.assertAlmostEqual(_grid(blk, "house_battery"), 0.0, places=6)
        self.assertAlmostEqual(_rem(blk), MAIN, places=6)

    def test_the_battery_keeps_its_ha_reading(self):
        blk = _block()
        engine._apply_pass2(blk)
        imp = blk["meters"]["house_battery"]["channels"]["import"]
        self.assertEqual(imp["kwh"], BATT)
        self.assertLessEqual(imp["kwh_grid"], BATT)


def _battery_only():
    cfg = H._cfg()
    cfg["meters"].pop("ev_charger")
    return cfg


class TheRefit(H._Harness):
    """A block closes before its completed dispatch is known; the EV then arrives on the main."""

    def _make_cfg(self):
        return _battery_only()

    def _close_then_arrive(self, measured=False):
        blk = engine.create_block(H.datetime.fromisoformat(H.START), H.datetime.fromisoformat(H.END),
                                  30, seed_meters=True)
        for meter, lo, d in (("electricity_main", 100.0, MAIN), ("house_battery", 10.0, BATT)):
            blk["meters"][meter]["channels"]["import"]["reads"] = [
                {"ts": H.START, "value": lo}, {"ts": H.END, "value": round(lo + d, 6)}]
        engine.finalise_block(H._HA(), block_data=blk)
        if measured:
            with engine._store._conn:
                engine._store._conn.execute(
                    "UPDATE blocks SET rate_source = 'settled', imp_cost = 1.2345"
                    " WHERE block_start = ? AND meter_id = 'electricity_main'", (H.START,))
        # The EV arrives in the SEGMENTS, as every real writer records it (BL-79; step 5c, the
        # imp_*_ev columns are gone): the split is the segments', and the refit's candidates are
        # read from them.
        r = engine._store._conn.execute("SELECT imp_rate FROM blocks WHERE block_start = ? "
                                        "AND meter_id = 'electricity_main'", (H.START,)).fetchone()[0]
        engine._store.set_block_segments(H.START, "electricity_main", [
            (EV, r, None, "off_peak", "ev"), (round(MAIN - EV, 6), r, None, "off_peak", "house")])

    def test_at_close_the_battery_takes_the_grid(self):
        """GUARD: before the EV is known there is nothing to reserve."""
        self._close_then_arrive()
        self.assertAlmostEqual(self._row("house_battery")["imp_kwh_grid"], BATT, places=4)

    def test_a_normal_split_still_warns_of_a_clip(self):
        """GUARD: outside a bulk pass the clip is still logged."""
        blk = _block(ev_kwh=None)
        blk["meters"]["electricity_main"]["channels"]["import"]["kwh"] = 1.0
        with self.assertLogs("engine", level="WARNING") as cm:
            engine._apply_pass2(blk)
        self.assertTrue(any("clipped" in m or "EXCEEDS" in m for m in cm.output))


if __name__ == "__main__":
    unittest.main()
