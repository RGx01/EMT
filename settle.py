"""One settle() for a main-meter half-hour (engine step 4, EMT-v5_engine_step4_settle_design.md §3).

Pure: facts in, the half-hour out. No store, no schedules, no clock — the caller (engine's
`settle_facts`) reads the facts; this decides. Running it twice on the same facts gives the same
answer, whatever order the facts arrived in (§4 (d): #475's defect was arrival order).

The rules, each from the design's decisions of 25 Oct 2026:
- **band** — Octopus's if it has settled it, else EMT's prediction (billed wins). A band is never
  read back from a rate (§4 (a), v4's BL-62).
- **rate** — the tariff's rate for each leg's band; on a capped half-hour the cap's four rates.
- **imp_rate** — the blend, cost ÷ kWh, so `imp_cost = imp_kwh × imp_rate` always (§4 (a)).
- **ex-VAT** — each leg's rate ÷ (1 + the VAT calendar's rate), one way (§4 (c)).
- **devices** — clipped to the grid as PASS 2 clips them, each priced at the rate of the legs its
  kWh sits in (§4 (b)): the EV meter fills the EV legs first, every other device the house legs.

A correction is not settled here (4a): the user's rate is kept only on the block today, and settle()
reads no stored rate. It becomes a fact of its own when the correction route becomes a trigger.
"""
from dataclasses import dataclass, field
from typing import Optional

import iog_cap
import pricing_segments as _ps

EV_METER_TYPES = ("ev", "ev_charger")     # the config UI writes 'ev'; the derived EV, 'ev_charger'
_EPS = 1e-9


@dataclass
class Device:
    meter_id: str
    kwh: float                  # the device's own read (raw imp_kwh), not the clipped figure
    is_ev: bool = False


@dataclass
class Facts:
    start: str
    end: str
    kwh: float                  # DCC once settled, else the live reading; an import's own
    kind: str                   # 'intelligent' | 'published' | 'none'
    vat: float                  # the VAT calendar's rate at the half-hour's UK-local date
    clock_rate: Optional[float] = None      # published: the schedule's rate (£)
    lo: Optional[float] = None              # intelligent: the day's off-peak rate (£)
    hi: Optional[float] = None              # intelligent: the day's peak rate (£)
    settled: Optional[str] = None           # settled_band: off_peak | peak | mixed | None
    settled_legs: dict = field(default_factory=dict)   # {'ev'|'house': band} from the buckets
    predicted: Optional[str] = None         # predicted_band, on `kwh`
    ev_kwh: float = 0.0                     # the bill's EV on IOG-SMB, else the completed dispatch
    capped: bool = False                    # capped_at, with the EV-device rates loaded
    ev_off: Optional[float] = None          # capped: the EV-device off-peak / peak rates (£)
    ev_peak: Optional[float] = None
    in_window: bool = False                 # the tariff's guaranteed off-peak window
    is_boost: bool = False                  # a bump/boost dispatch covers it
    boundary: Optional[tuple] = None        # the cap-day's (boundary_slot, boundary_frac)
    devices: list = field(default_factory=list)
    clamp: bool = True                      # PASS 2's clamp_overflow: settled, or not interpolated


@dataclass
class Leg:
    kwh: float
    inc_rate: float
    exc_rate: float
    band: str
    attribution: str            # 'ev' | 'house'


@dataclass
class DeviceCost:
    meter_id: str
    kwh_grid: float
    kwh_other: float            # what the grid could not supply (solar, the battery)
    rate: float
    cost: float


@dataclass
class Settled:
    status: str                 # settled | predicted | clock | empty | no_rate | no_band
    band: Optional[str] = None
    legs: list = field(default_factory=list)
    imp_rate: Optional[float] = None
    imp_cost: Optional[float] = None
    imp_cost_exc: Optional[float] = None
    ev_kwh: float = 0.0
    devices: list = field(default_factory=list)

    @property
    def two_rates(self) -> bool:
        """Legs at more than one rate — what §4 (a) and (b) decide."""
        return len({round(g.inc_rate, 6) for g in self.legs if g.kwh > _EPS}) > 1


def _exc(rate: float, vat: float) -> float:
    return round(rate / (1.0 + (vat or 0.0)), 6)


def _house_band(band: str) -> str:
    return "off_peak" if band == "off_peak" else "day"


def _one_rate_legs(kwh: float, ev: float, rate: float, ev_band: str, home_band: str) -> list:
    """EV + house at one rate, as (kWh, rate, band, attribution): the one builder's
    (`pricing_segments.ev_house_at`, bill and split paths design §3.3)."""
    return [(g.kwh, g.inc_rate, g.band, g.attribution)
            for g in _ps.ev_house_at(kwh, ev, inc_rate=rate, exc_rate=None, ev_band=ev_band,
                                     home_band=home_band)]


def _single_band_legs(f: Facts, band: str, ev: float) -> list:
    """Every leg on one band: the EV at off-peak/peak, the house at off-peak/day — the bill apply's
    single-band legs (step 2b), at the day's two rates."""
    return _one_rate_legs(f.kwh, ev, f.lo if band == "off_peak" else f.hi, band, _house_band(band))


def _bucket_legs(f: Facts, ev: float) -> "list | None":
    """A mixed bill off the cap: each leg on its own bucket's band (the bill's two buckets at the two
    rates), each built by the one builder. None where the buckets do not name both legs' bands."""
    eb, hb = f.settled_legs.get("ev"), f.settled_legs.get("house")
    out = []
    if ev > _EPS:
        if eb not in ("off_peak", "peak"):
            return None
        out += _one_rate_legs(ev, ev, f.lo if eb == "off_peak" else f.hi, eb, _house_band(eb))
    if f.kwh - ev > _EPS:
        if hb not in ("off_peak", "peak"):
            return None
        out += _one_rate_legs(f.kwh - ev, 0.0, f.lo if hb == "off_peak" else f.hi, eb or hb,
                              _house_band(hb))
    return out


def _cap_legs(f: Facts, ev: float, *, boost: bool) -> "list | None":
    """The 6-hour cap's legs (iog_cap.price_slot): EV off-peak within the cap, peak beyond or on a
    boost; the house off-peak in the window or with a within-cap dispatch, else day."""
    if None in (f.lo, f.hi, f.ev_off, f.ev_peak):
        return None
    res = iog_cap.price_slot(f.start, f.end, f.kwh, ev,
                             in_off_peak_window=f.in_window, is_boost=boost, boundary=f.boundary,
                             house_offpeak_rate=f.lo, house_day_rate=f.hi,
                             ev_offpeak_rate=f.ev_off, ev_peak_rate=f.ev_peak)
    segs = res.get("segments")
    return [(k, r, b, a) for (k, r, b, a) in segs] if segs else None


def device_grid_shares(grid: float, devices, *, ev_kwh: float = 0.0, has_ev_meter: bool = False,
                       clamp: bool = True) -> list:
    """Each device's share of a half-hour's grid import — the one split rule, called by PASS 2
    (`engine._apply_pass2`) and by `_devices` (BL-76; battery sensor and device split design §2).

    `devices`: [(kWh read, is an EV meter)], in any order; the shares come back in that order.
    The EV first: an EV meter claims what it read; with no EV meter the main's dispatch EV (`ev_kwh`)
    is reserved, so the devices fit inside the house. Every other device then shares what is left in
    proportion to its reading — `left × kWh / Σ kWh` where they read more than is left, else each its
    reading. Bounded, order-independent, no cliff (it was largest first, the smallest taking the whole
    shortfall). A device's reading past its share is its non-grid supply. With `clamp` off — an
    unsettled gap block, whose main is itself an estimate — a device reading more than the whole
    grid is recorded as read."""
    grid = max(float(grid or 0.0), 0.0)
    kwh = [max(float(k or 0.0), 0.0) for k, _ev in devices]
    left = grid if has_ev_meter else max(grid - min(max(float(ev_kwh or 0.0), 0.0), grid), 0.0)
    shares = [0.0] * len(kwh)
    evs = sorted((i for i, (_k, ev) in enumerate(devices) if ev and kwh[i] > _EPS), key=lambda i: -kwh[i])
    for i in evs:
        shares[i] = kwh[i] if (not clamp and kwh[i] > grid) else min(kwh[i], left)
        left = max(left - shares[i], 0.0)
    rest = [i for i, (_k, ev) in enumerate(devices) if not ev and kwh[i] > _EPS]
    for i in [i for i in rest if not clamp and kwh[i] > grid]:
        shares[i] = kwh[i]                        # recorded as read: the main is an estimate
        left = max(left - kwh[i], 0.0)
    rest = [i for i in rest if clamp or kwh[i] <= grid]
    total = sum(kwh[i] for i in rest)
    if total <= left + _EPS:
        for i in rest:
            shares[i] = kwh[i]
        return shares
    # Shared in whole watt-hours, as the meters read: each its proportion rounded down, the watt-hours
    # left over to the largest remainders — so the shares add up to exactly what is left (never past
    # the main), every surface sums the same figures, and none passes its own reading.
    left_wh = int(left * 1000.0 + 1e-6)
    exact = {i: left_wh * kwh[i] / total for i in rest}
    wh = {i: int(exact[i]) for i in rest}
    for i in sorted(rest, key=lambda i: (-(exact[i] - wh[i]), -kwh[i], i))[:left_wh - sum(wh.values())]:
        wh[i] += 1
    for i in rest:
        shares[i] = min(wh[i] / 1000.0, kwh[i])
    return shares


def _devices(f: Facts, legs: list) -> list:
    """PASS 2's grid clip (`device_grid_shares`, the one rule), each device priced by leg (§4 (b)).

    The clip: an EV meter claims grid first, then every other device shares what is left in
    proportion to its reading (BL-76); with no EV meter the main's EV is reserved, so the devices fit
    inside the house. A device past its share keeps the rest as non-grid supply — unless the main is
    an unsettled gap block (`clamp` off), where the main is itself an estimate and the device is
    recorded as read.

    The price: the legs are two pools, the EV legs and the house legs. A device draws from its own
    pool first (the EV meter the EV legs, every other device the house legs) and then from the other,
    paying each pool's blend for what it takes. The devices are clipped to the main's kWh, so their
    cost can never pass the main's."""
    pools = {"ev": [0.0, 0.0], "house": [0.0, 0.0]}           # [kWh left, £ left]
    for k, r, _b, a in legs:
        p = pools["ev" if a == "ev" else "house"]
        p[0] += k
        p[1] += k * r
    blend = {a: (p[1] / p[0] if p[0] > _EPS else None) for a, p in pools.items()}
    fallback = next((b for b in (blend["house"], blend["ev"]) if b is not None), 0.0)
    shares = device_grid_shares(f.kwh, [(d.kwh, d.is_ev) for d in f.devices], ev_kwh=f.ev_kwh,
                                has_ev_meter=any(d.is_ev for d in f.devices), clamp=f.clamp)
    share = {d.meter_id: s for d, s in zip(f.devices, shares)}
    # priced in a fixed order — the EV first, then by reading — so a pool that runs dry is met the same way
    order = sorted((d for d in f.devices if d.kwh > _EPS), key=lambda d: (0 if d.is_ev else 1, -d.kwh))
    out = {}
    for d in order:
        claimed = share[d.meter_id]
        cost, need = 0.0, claimed
        for a in (("ev", "house") if d.is_ev else ("house", "ev")):
            p = pools[a]
            take = min(need, p[0])
            if take > _EPS:
                c = take * blend[a]
                cost += c
                p[0] -= take
                p[1] -= c
                need -= take
        if need > _EPS:                       # an unclamped gap block: beyond the main's legs
            cost += need * fallback
        own = blend["ev" if d.is_ev else "house"]
        rate = (cost / claimed) if claimed > _EPS else (own if own is not None else fallback)
        out[d.meter_id] = DeviceCost(d.meter_id, round(claimed, 6), round(d.kwh - claimed, 6),
                                     round(rate, 6), round(cost, 6))
    for d in f.devices:                       # a device that drew nothing follows its pool's rate
        if d.meter_id not in out:
            own = blend["ev" if d.is_ev else "house"]
            out[d.meter_id] = DeviceCost(d.meter_id, 0.0, 0.0,
                                         round(own if own is not None else fallback, 6), 0.0)
    return [out[d.meter_id] for d in f.devices]


def settle(f: Facts) -> Settled:
    """The half-hour from its facts (design §3). `status` says what decided the band: settled (a
    bill), predicted (EMT's prediction), clock (a published tariff); or why nothing could be priced."""
    kwh = max(f.kwh or 0.0, 0.0)
    if kwh <= _EPS:
        return Settled("empty", imp_cost=0.0, imp_cost_exc=0.0,
                       devices=_devices(f, []) if f.devices else [])
    if f.kind != "intelligent":
        if f.clock_rate is None:
            return Settled("no_rate")
        raw, band, status, ev = (_one_rate_legs(kwh, 0.0, f.clock_rate, "standard", "standard"),
                                 "standard", "clock", 0.0)
    else:
        if f.lo is None or f.hi is None:
            return Settled("no_rate")
        if f.settled in ("off_peak", "peak", "mixed"):
            band, status = f.settled, "settled"
        elif f.predicted in ("off_peak", "peak"):
            band, status = f.predicted, "predicted"
        else:
            return Settled("no_band")
        ev = min(max(f.ev_kwh or 0.0, 0.0), kwh)
        raw = None
        if f.capped and ev > _EPS and (band == "mixed" or status == "predicted"):
            # the cap's four rates: a mixed bill's legs, or the prediction's (a predicted peak is a
            # dispatch that does not count — a bump — so the EV is on peak, as on a boost)
            raw = _cap_legs(f, ev, boost=f.is_boost or band == "peak")
        if raw is None and band == "mixed":
            raw = _bucket_legs(f, ev)
            if raw is None:                   # the buckets name no band per leg: peak, as today
                raw = _single_band_legs(f, "peak", ev)
        if raw is None:
            raw = _single_band_legs(f, "peak" if band == "mixed" else band, ev)
    legs = [Leg(round(k, 6), r, _exc(r, f.vat), b, a) for (k, r, b, a) in raw if k > _EPS]
    raw_cost = sum(g.kwh * g.inc_rate for g in legs)
    cost = round(raw_cost, 6)
    cost_exc = round(sum(g.kwh * g.exc_rate for g in legs), 6)
    # the blend from the unrounded sum: a rate worked back from a rounded cost scatters (0.004 kWh at
    # 0.32309235 → 0.323), the residue v4's archive names; a single-rate half-hour gives its rate
    rates = {g.inc_rate for g in legs}
    imp_rate = rates.pop() if len(rates) == 1 else round(raw_cost / sum(g.kwh for g in legs), 8)
    return Settled(status, band=band, legs=legs, imp_rate=imp_rate, imp_cost=cost,
                   imp_cost_exc=cost_exc,
                   ev_kwh=round(sum(g.kwh for g in legs if g.attribution == "ev"), 6),
                   devices=_devices(f, raw))
