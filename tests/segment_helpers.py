"""
tests/segment_helpers.py — segment builders the TESTS use, moved out of pricing_segments.py.

`import_segments`, `attribute_devices` and `total_cost_exc` had no caller in the application
(the reachability trace reported all three unreachable; BL-63, 4 Oct 2026), but the suite used
them to BUILD segments for tests of the live projections in pricing_segments — `total_cost`,
`attribution_*`, `segments_from_legacy`, `price_devices_hybrid` — and in the capped synthetic
fixture. Deleting them would have cut those tests of live code, so they live here instead,
unchanged, over the live helpers they always called.
"""
from typing import List, Optional

from pricing_segments import (Segment, _EPS, _seg, _row, attribution_cost,  # noqa: F401
                              attribution_kwh, attribution_rate, total_cost, total_kwh)


def import_segments(*, ev_kwh: float, house_kwh: float,
                    house_offpeak_rate: float, house_day_rate: float,
                    ev_offpeak_rate: Optional[float] = None,
                    ev_peak_rate: Optional[float] = None,
                    ev_offpeak_frac: float = 1.0,
                    house_offpeak_frac: float = 1.0,
                    vat: float) -> List[Segment]:
    """Decompose a block's grid import into priced segments.

    `*_offpeak_frac` (0..1) is the off-peak share of that portion — 1.0 = wholly off-peak,
    0.0 = wholly peak/day, and a value strictly between is the boundary slot (the cap
    crossing mid-block), which yields TWO sub-segments for that portion. `ev_kwh` is the
    dispatch-derived EV energy (grid-clipped upstream); `house_kwh` is the remainder.

    UNCAPPED usage: pass every rate equal to the block's rate (EV and house share it), and
    the fractions per the tariff window — the result is the same block rate on every
    segment, so it's byte-identical to today's single-rate pricing. CAPPED usage: pass the
    four distinct 4-rate values and the cap fractions from `iog_cap.classify_slot`.

    Zero-kWh slices are dropped. Segments are ordered EV-off, EV-peak, house-off, house-day.
    """
    out: List[Segment] = []
    if ev_kwh > _EPS and ev_offpeak_rate is not None and ev_peak_rate is not None:
        k_off = ev_kwh * ev_offpeak_frac
        k_pk = ev_kwh - k_off
        if k_off > _EPS:
            out.append(_seg(k_off, ev_offpeak_rate, "off_peak", "ev", vat))
        if k_pk > _EPS:
            out.append(_seg(k_pk, ev_peak_rate, "peak", "ev", vat))
    if house_kwh > _EPS:
        k_off = house_kwh * house_offpeak_frac
        k_day = house_kwh - k_off
        if k_off > _EPS:
            out.append(_seg(k_off, house_offpeak_rate, "off_peak", "house", vat))
        if k_day > _EPS:
            out.append(_seg(k_day, house_day_rate, "day", "house", vat))
    return out


def total_cost_exc(segs: List[Segment]) -> Optional[float]:
    if any(s.exc_rate is None for s in segs):
        return None
    return round(sum(s.kwh * s.exc_rate for s in segs), 6)


def attribute_devices(segments: List[Segment], devices) -> dict:
    """Attribute physical sub-devices onto the segments they belong to.

    `devices` = iterable of {"meter_id", "attribution", "grid_kwh"} (grid-clipped upstream;
    `attribution` maps the device's type to a segment attribution — "ev" for a charger,
    anything else is house load). Each device is SHOWN at its metered grid kWh; an **ev**
    device is priced from the EV-attributed segment cost (dispatch is the EV billing basis —
    kWh from the device, cost from the segments, split pro-rata across multiple EV devices);
    a **house** device is priced at the house-band rate. The house **remainder** is the plug
    on BOTH axes, so device costs + remainder == the grid total exactly (the reconciliation
    invariant), capped or uncapped.

    Returns {"devices": {meter_id: {kwh,cost,rate}}, "ev_dispatch": {kwh,cost,rate}|None,
             "remainder": {kwh,cost,rate}}. `ev_dispatch` is the synthetic EV row — present
    only when EV segments exist but no physical EV device claims them (the sensor-less case).
    """
    grid_k = total_kwh(segments)
    grid_c = total_cost(segments)
    ev_k = attribution_kwh(segments, "ev")
    ev_c = attribution_cost(segments, "ev")
    house_rate = attribution_rate(segments, "house")

    devs = list(devices)
    ev_devs = [d for d in devs if d.get("attribution") == "ev"]
    house_devs = [d for d in devs if d.get("attribution") != "ev"]

    out: dict = {}
    claimed_k = claimed_c = 0.0
    ev_met = sum(d.get("grid_kwh", 0.0) for d in ev_devs)
    for d in ev_devs:
        k = d.get("grid_kwh", 0.0)
        c = round(ev_c * (k / ev_met), 6) if ev_met > _EPS else 0.0
        out[d["meter_id"]] = _row(k, c)
        claimed_k += k
        claimed_c += c
    for d in house_devs:
        k = d.get("grid_kwh", 0.0)
        c = round(k * house_rate, 6)
        out[d["meter_id"]] = _row(k, c)
        claimed_k += k
        claimed_c += c

    ev_dispatch = None
    if not ev_devs and ev_k > _EPS:                 # sensor-less EV → synthetic row
        ev_dispatch = _row(ev_k, ev_c)
        claimed_k += ev_k
        claimed_c += ev_c

    remainder = _row(grid_k - claimed_k, grid_c - claimed_c)
    return {"devices": out, "ev_dispatch": ev_dispatch, "remainder": remainder}
