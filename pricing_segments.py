"""
pricing_segments.py — BL-27: a block's grid import as an ordered list of priced SEGMENTS.

A segment is one homogeneously-priced slice of the block's grid import:

    Segment(kwh, inc_rate, exc_rate, band, attribution)
        band, attribution : OPEN labels (strings). Storage and surfaces NEVER switch on
                            them — only the classifier assigns meaning. This is what makes
                            a new rate dimension (a battery band, a heat-pump attribution)
                            a one-place change. (Why segments: v4 stored the EV split, the ex-VAT
                            figures and the device prices beside the rate, so every path that
                            changed the rate had to remember all three; it kept not happening.)
        exc_rate          : per-segment ex-VAT rate — VAT is a property of the slice, so a
                            VAT holiday / per-band quirk is absorbed with no special-casing.

This module is PURE (no I/O, no engine imports). The *classifier* that decides the
off-peak fractions/bands lives in `iog_cap`; here we turn (a kWh split + per-band rates)
into segments and expose the PROJECTIONS that the legacy `imp_*` columns become views over
(blended rate, EV/house split, ex-VAT). One decomposition; everything else reads it.

Invariant (the acceptance gate everywhere downstream):
    Σ segment.kwh   == the block's grid kWh
    Σ segment.kwh × inc_rate == the block's grid cost (to rounding)
so house + devices + remainder always reconcile to the grid import, capped or uncapped.
"""

from typing import NamedTuple, Optional, List

_EPS = 1e-9


class Segment(NamedTuple):
    kwh: float
    inc_rate: float
    exc_rate: Optional[float]
    band: str          # open label: off_peak | day | peak | …
    attribution: str   # open label: house | ev | …


def _seg(kwh: float, inc_rate: float, band: str, attribution: str,
         vat: float) -> Segment:
    exc = round(inc_rate / (1.0 + vat), 6) if inc_rate is not None else None
    return Segment(round(kwh, 6), inc_rate, exc, band, attribution)


def segments_from_legacy(*, imp_kwh: float, imp_cost: float, imp_rate: float,
                         kwh_ev: Optional[float] = None,
                         cost_ev: Optional[float] = None,
                         rate_ev: Optional[float] = None,
                         ev_band: Optional[str] = None,
                         home_band: Optional[str] = None,
                         exc_ratio: Optional[float] = None) -> List[Segment]:
    """Reconstruct a block's segments from the LEGACY stored columns — the backfill's core.

    Produces the same figures the `imp_*` columns held, so migrated readers show identical
    values: an EV segment (`imp_kwh_ev` @ `imp_rate_ev`) plus the house remainder at its
    derived rate when a split is present, else a single house segment at `imp_rate`. The
    columns already collapsed any 4-rate boundary detail, so this yields 1–2 segments; the
    live seam writes the full 1–4 going forward. `exc_ratio` = the block's exc/inc ratio
    (`imp_rate_exc`/`imp_rate`, or 1/(1+VAT)); None leaves exc NULL (view falls back).
    """
    def _exc(inc):
        return round(inc * exc_ratio, 6) if (exc_ratio is not None and inc is not None) else None

    def _rate(kwh, cost, tariff_rate):  # used for the no-EV single house segment only
        # Prefer the block's stored TARIFF rate when it reproduces the stored cost at
        # storage precision (a CLEAN block: round(kWh×rate) == round(cost)). Only when it
        # does NOT — a genuinely settlement-adjusted / stale-split block where cost ≠
        # kWh×rate — fall back to deriving the rate from the cost, which keeps Σ cost ==
        # imp_cost on those blocks. Back-computing a CLEAN block instead (the old
        # unconditional path) turned the stored cost's 6-dp rounding into 1/kWh rate
        # SCATTER — e.g. a 0.006 kWh peak block priced a clean 0.323092 stored a segment
        # at 0.323167 → exc 0.307778 — which then showed a spurious ex-VAT rate tick on
        # small billing bands. The migrated segment must be as faithful as the block.
        if tariff_rate and kwh > _EPS \
                and abs(round(kwh * tariff_rate, 6) - round(cost, 6)) < 1e-9:
            return round(tariff_rate, 6)
        return round(cost / kwh, 6) if kwh > _EPS else (tariff_rate or 0.0)

    segs: List[Segment] = []
    if kwh_ev is not None and kwh_ev > _EPS:
        hk = imp_kwh - kwh_ev
        # The house remainder absorbs the difference so Σ cost == imp_cost exactly. When
        # there's NO house remainder (all-EV block), the EV segment carries the full cost —
        # so a stale/settlement-adjusted cost_ev can never leave an unreconciled residual.
        ev_c = imp_cost if hk <= _EPS else (
            cost_ev if cost_ev is not None else kwh_ev * (rate_ev or 0.0))
        # EV segment stays cost-derived (byte-identical): an EV charge is always a
        # substantial kWh, so cost/kWh never scatters the way a tiny house-only block does,
        # and keeping it cost-derived preserves Σ cost == imp_cost with no residual drift.
        er = round(ev_c / kwh_ev, 6)
        segs.append(Segment(round(kwh_ev, 6), er, _exc(er), ev_band or "standard", "ev"))
        if hk > _EPS:
            # House remainder = residual absorber; legacy columns carry no separate house
            # tariff rate, so this stays cost-derived (its own scatter is bounded by the
            # remainder kWh, and on a clean split reduces to the house day rate).
            hr = round((imp_cost - ev_c) / hk, 6)
            segs.append(Segment(round(hk, 6), hr, _exc(hr), home_band or "standard", "house"))
    elif imp_kwh > _EPS:
        r = _rate(imp_kwh, imp_cost, imp_rate)
        segs.append(Segment(round(imp_kwh, 6), r, _exc(r), home_band or "standard", "house"))
    return segs


def ev_house_at(kwh: float, ev_kwh: Optional[float], *, inc_rate: float,
                exc_rate: Optional[float], ev_band: str, home_band: str) -> List[Segment]:
    """A block's EV + house segments at ONE rate: the EV leg (`ev_kwh`) and the house remainder,
    or — with no EV — one house segment for the whole kWh. The one builder (bill and split paths
    design §3.3, 16 Oct 2026) for the four places that wrote it by hand: the carve, the settle's
    single-band fallback, the reconcile's stale-rated split heal (BL-58b), and its revert/restore
    verdict. A remainder that rounds to nothing gets no segment."""
    segs: List[Segment] = []
    kwh = float(kwh or 0.0)
    if ev_kwh is not None and float(ev_kwh) > _EPS:
        ev_kwh = float(ev_kwh)
        segs.append(Segment(round(ev_kwh, 6), inc_rate, exc_rate, ev_band, "ev"))
        hk = round(kwh - ev_kwh, 6)
        if hk > _EPS:
            segs.append(Segment(hk, inc_rate, exc_rate, home_band, "house"))
    else:
        segs.append(Segment(round(kwh, 6), inc_rate, exc_rate, home_band, "house"))
    return segs


# ── Projections — the legacy imp_* columns become views over the segments ────────────

def refit_to_kwh(segs, kwh: float, *, rate: float, exc_rate: Optional[float],
                 band: str) -> List[Segment]:
    """A block's segments fitted to a new metered kWh at the rates they already carry — for a
    settled kWh arriving on a block whose rate is held (a correction, a reconciliation, a bill).
    The settled figure says how much was drawn, never at what price, so no segment is re-priced.

    The house takes the change: its segments scale together, keeping their rate mix. A block
    with no house segment gains one at `rate` / `exc_rate` / `band` (the block's held rate).
    Only when the house is gone does the EV shrink, scaled the same way, so the segments never
    claim more than the meter. `segs` are Segments or (kwh, inc_rate, exc_rate, band,
    attribution) dicts/tuples; the result (EV segments, then house) sums to `kwh` to 6 dp."""
    segs = [s if isinstance(s, Segment) else
            Segment(*(s[k] for k in Segment._fields)) if isinstance(s, dict) else Segment(*s)
            for s in (segs or [])]
    kwh = max(0.0, float(kwh or 0.0))
    house = [s for s in segs if s.attribution != "ev"]
    ev = [s for s in segs if s.attribution == "ev"]
    ev_k = sum(s.kwh for s in ev)
    house_k = max(0.0, kwh - ev_k)

    def _scaled(group, to):
        have = sum(s.kwh for s in group)
        if have <= _EPS:
            return []
        return [s._replace(kwh=round(s.kwh * to / have, 6)) for s in group]

    if kwh < ev_k - _EPS:                      # less than the EV alone: the house is gone
        out = _scaled(ev, kwh)
    else:
        if house:
            new_house = _scaled(house, house_k) if sum(s.kwh for s in house) > _EPS else \
                [house[0]._replace(kwh=round(house_k, 6))]
        elif house_k > _EPS:
            new_house = [Segment(round(house_k, 6), rate, exc_rate, band, "house")]
        else:
            new_house = []
        out = ev + new_house                   # the seam's order: EV segments, then house
    out = [s for s in out if s.kwh > _EPS]
    drift = round(kwh - sum(s.kwh for s in out), 6)    # rounding: the last segment takes it
    if out and abs(drift) > 0:
        out[-1] = out[-1]._replace(kwh=round(out[-1].kwh + drift, 6))
    return out


def total_kwh(segs: List[Segment]) -> float:
    return round(sum(s.kwh for s in segs), 6)


def total_cost(segs: List[Segment]) -> float:
    return round(sum(s.kwh * s.inc_rate for s in segs), 6)


def blended_rate(segs: List[Segment]) -> float:
    k = total_kwh(segs)
    return round(total_cost(segs) / k, 6) if k > _EPS else 0.0


def attribution_kwh(segs: List[Segment], attribution: str) -> float:
    return round(sum(s.kwh for s in segs if s.attribution == attribution), 6)


def attribution_cost(segs: List[Segment], attribution: str) -> float:
    return round(sum(s.kwh * s.inc_rate for s in segs if s.attribution == attribution), 6)


def attribution_rate(segs: List[Segment], attribution: str) -> float:
    k = attribution_kwh(segs, attribution)
    return round(attribution_cost(segs, attribution) / k, 6) if k > _EPS else 0.0


# ── Device attribution — a device's metered kWh priced on its segments ───────────────

def _row(kwh: float, cost: float) -> dict:
    return {"kwh": round(kwh, 6), "cost": round(cost, 6),
            "rate": round(cost / kwh, 6) if kwh > _EPS else 0.0}


def price_devices_hybrid(segments: List[Segment], devices) -> dict:
    """Price PHYSICAL sub-devices by valuing each device's METERED grid kWh at the block's
    band rate for its attribution — the model for accounts that meter a device directly.

    Contrast with `attribute_devices`, which prices an EV device from the EV *dispatch*
    segment cost: on an account with a real EV meter the metered draw is the physical truth
    (a car also grid-charges outside smart-dispatch windows, at the house/day rate), so we
    keep the device's own kWh and value it at the correct rate:

      * a **single-rate** (uncapped-equivalent) block → every band rate equals the block's
        blended rate, so a device costs `metered × block_rate` — BYTE-IDENTICAL to its
        stored `imp_cost` column, so uncapped accounts are unchanged (no dispatch-maturity
        divergence, no leakage into house);
      * a **multi-rate** (capped / boundary) block → an EV device's metered kWh is valued at
        the EV band rate (off-peak within cap, peak beyond), a house device at the house band
        rate — the 4-rate fix the parent blended rate got wrong.

    `devices` = iterable of {"meter_id", "attribution" ('ev'|else house), "grid_kwh"}.
    The house **remainder** is the plug on both axes, so devices + remainder == the grid
    total exactly (the reconciliation invariant). Returns
    {"devices": {meter_id: {kwh,cost,rate}}, "remainder": {kwh,cost,rate}}.
    """
    grid_k = total_kwh(segments)
    grid_c = total_cost(segments)
    blended = blended_rate(segments)
    single = len({round(s.inc_rate, 6) for s in segments}) <= 1
    ev_rate = blended if single else (attribution_rate(segments, "ev") or blended)
    house_rate = blended if single else (attribution_rate(segments, "house") or blended)

    out: dict = {}
    claimed_k = claimed_c = 0.0
    for d in devices:
        k = d.get("grid_kwh", 0.0)
        r = ev_rate if d.get("attribution") == "ev" else house_rate
        c = round(k * r, 6)
        out[d["meter_id"]] = _row(k, c)
        claimed_k += k
        claimed_c += c

    remainder = _row(grid_k - claimed_k, grid_c - claimed_c)
    return {"devices": out, "remainder": remainder}
