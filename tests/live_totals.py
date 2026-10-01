"""
A window's billing totals as Live Power's card computes them (C, 1 Oct 2026):
`BlockStore.get_day_live_rollups` summed over the window's local days, main meter only —
grid kWh (`COALESCE(imp_kwh_grid, imp_kwh)`), cost, export, and the standing charge once
per local day (the earliest non-zero). These are `bill_card.live_rows`' inputs.

Shared by the tests that pinned `BlockStore.get_billing_totals_for_utc_range` — /api/billing's
store method, deleted with it — for the behaviour that carries over to the card's path.
"""


def live_totals(store, utc_start, utc_end, tz_name, ev_meter_id=None):
    live = store.get_day_live_rollups(utc_start, utc_end, tz_name, ev_meter_id)
    out = {"imp_kwh": 0.0, "imp_cost": 0.0, "exp_kwh": 0.0, "exp_cost": 0.0, "standing": 0.0,
           "sub_kwh": 0.0, "sub_cost": 0.0}
    for day in live["meters"].values():
        sc = 0.0
        for m in day.values():
            if m["is_sub"]:
                out["sub_kwh"] += m["grid_kwh"]; out["sub_cost"] += m["imp_cost"]
                continue
            out["imp_kwh"] += m["grid_kwh"]; out["imp_cost"] += m["imp_cost"]
            out["exp_kwh"] += m["exp_kwh"]; out["exp_cost"] += m["exp_cost"]
            if m["standing"] > 0 and not sc:
                sc = m["standing"]
        out["standing"] += sc
    return {k: round(v, 4) for k, v in out.items()}
