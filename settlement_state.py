"""
settlement_state.py — what has settled, per main-meter half-hour (ROADMAP BL-85; settlement
surfaces design §1).

A half-hour settles in three stages, each on its own clock (ROADMAP BL-82's model): its kWh (DCC),
then its bill (Octopus's cost for it), then on IOG-SMB its House/EV split. The kWh stage is already
on the payload (`kwh_authoritative`); this decides the other two, once, so every surface says the
same thing. Pure: facts in (BlockStore.get_settlement_facts), states out.

    bill   none | waiting | overdue | applied | differs | corrected | imported
    split  None (no completed dispatch, or not IOG-SMB) | emt | bill | held | kept | missing
    flags  recost_bl82       — `differs` because the settlement re-run re-costed the bill to
                                kWh x rate before BL-82's fix (13 Oct 2026): known, not new
           bill_kwh_differs  — the bill priced a different kWh than the one that settled
           vat_disagrees     — the bill's own VAT is not the calendar's for its day (#505)
"""
from datetime import datetime, timedelta

# A bill is overdue 7 days after its half-hour's kWh settled (design §7 (a), decided 13 Oct 2026,
# "to come down if it proves generous"). EMT does not record when a half-hour's DCC kWh arrived,
# so it is measured from the half-hour, plus the kWh's own ~2-day lag.
BILL_OVERDUE_DAYS = 7
KWH_LAG_DAYS = 2

_EPS_GBP = 1e-5          # a thousandth of a penny: below it is float and rounding noise
_EPS_KWH = 0.0005
_VAT_TOL = 0.005          # |inc/exc - (1 + VAT)|; 0% and 5% are 0.05 apart
_MIN_EXC = 0.001          # GBP; a pair below 0.1p ex-VAT says nothing about VAT


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def classify(f: dict, *, bills: bool, every_half_hour: bool, smb: bool, now: datetime,
             held=frozenset(), kept=frozenset(), vat_at=None, floor=None) -> dict:
    """One half-hour's {bill, split, flags}. `f` is a get_settlement_facts row. `bills`,
    `every_half_hour` and `smb` describe the install (engine.bill_stage, _import_is_smb_capped);
    `held` / `kept` are 4.5.23's wait map (engine.bill_split_held); `vat_at(start)` the VAT calendar;
    `floor` the bill fetch's floor (engine.bill_stage) — before it EMT never asks for a bill."""
    start = f["block_start"]
    kwh = _f(f.get("imp_kwh")) or 0.0
    cost = _f(f.get("imp_cost"))
    bill_cost = _f(f.get("bill_cost"))
    flags = []

    # ── bill ──
    if f.get("rate_corrected") or f.get("rate_source") == "corrected":
        bill = "corrected"
    elif bill_cost is not None and kwh > 1e-9:
        if f.get("rate_source") == "settled":
            if cost is not None and abs(cost - bill_cost) <= _EPS_GBP:
                bill = "applied"
            else:
                bill = "differs"
                rate = _f(f.get("imp_rate"))
                if (cost is not None and rate is not None
                        and abs(cost - round(kwh * rate, 6)) <= _EPS_GBP):
                    flags.append("recost_bl82")
        else:
            bill = "waiting"          # cached, not applied yet: the next pass applies it
    elif (f.get("source") or "").startswith("imported"):
        bill = "imported"
    elif (not bills or kwh <= 1e-9 or (floor is not None and start < floor)
          or (not every_half_hour and not f.get("dispatched"))):
        # no bill stage: kWh x rate is final with the kWh. Before the floor (the current
        # agreement's start) EMT never asks for a bill, so none is overdue — PR 5 on the Pi,
        # 14 Oct 2026: 1-25 Aug showed red "overdue" for bills no pass would fetch, while the
        # pill, floored, did not count them.
        bill = "none"
    else:
        age = now - datetime.fromisoformat(start)
        bill = ("overdue" if age >= timedelta(days=BILL_OVERDUE_DAYS + KWH_LAG_DAYS)
                else "waiting")

    if bill_cost is not None:
        bk, api = _f(f.get("bill_kwh")), _f(f.get("imp_kwh_api"))
        if bk is not None and bk > 1e-9 and api is not None and abs(bk - api) > _EPS_KWH:
            flags.append("bill_kwh_differs")
        exc = _f(f.get("bill_cost_exc"))
        if vat_at is not None and exc is not None and exc >= _MIN_EXC:
            if abs(bill_cost / exc - (1.0 + float(vat_at(start)))) > _VAT_TOL:
                flags.append("vat_disagrees")

    # ── split: IOG-SMB, half-hours with a completed dispatch ──
    split = None
    if smb and f.get("completed"):
        bill_ev = _f(f.get("bill_ev_kwh"))
        if start in kept:
            split = "kept"
        elif start in held:
            split = "held"
        elif bill_ev is not None:
            # the bill has stated the split; none at all against a completed dispatch is the
            # 10 Oct 2026 case — needs attention, not a split
            split = "bill" if (bill_ev > 1e-9 or kwh <= 1e-9) else "missing"
        elif (_f(f.get("ev_kwh")) or 0.0) > 1e-9:
            split = "emt"
        elif kwh > 1e-9:
            split = "missing"
    return {"bill": bill, "split": split, "flags": flags}


def classify_range(facts, **ctx) -> dict:
    """{block_start: classify(...)} over get_settlement_facts rows."""
    return {f["block_start"]: classify(f, **ctx) for f in facts}
