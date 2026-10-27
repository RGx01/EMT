"""
settlement_state.py — what has settled, per main-meter half-hour (ROADMAP BL-85; settlement
surfaces design §1).

A half-hour settles in three stages, each on its own clock (ROADMAP BL-82's model): its kWh (DCC),
then its bill (Octopus's cost for it), then on IOG-SMB its House/EV split. The kWh stage is already
on the payload (`kwh_authoritative`); this decides the other two, once, so every surface says the
same thing. Pure: facts in (BlockStore.get_settlement_facts), states out.

    bill   applied | applied_emt_split | estimate_dcc | estimate_cad | overdue | differs |
           corrected | imported | abandoned | none                           (design §9.3)
           imported  — not Octopus's cost, and final: CSV history, an import's own cost as a bill
           (label IMPORTED), or no bill past Octopus's two years (27 Oct 2026, the user)
           abandoned — EMT's kWh x rate, and EMT no longer asks Octopus for the cost: a bill
           asked 30 days unanswered, or an API-imported half-hour Octopus gave no cost for
           (19 Oct 2026, the user: red, hatched)
    export applied | waiting | overdue | imported | no_agreement | none        (design §1.2b, §9.4)
    split  None (no completed dispatch, or not IOG-SMB) | emt | bill | held | kept | missing
    flags  bill_kwh_differs  — the bill priced a different kWh than the one that settled
           vat_disagrees     — the bill's own VAT is not the calendar's for its day (#505)
           split_missing     — a completed dispatch, energy in the block, and no split at all
                               (the Split view's red; the view went 17 Oct 2026, and the Import
                               bill carries it as attention)
"""
from datetime import datetime, timedelta

# A bill is overdue 7 days after its half-hour's kWh settled (design §7 (a), decided 13 Oct 2026,
# "to come down if it proves generous"). EMT does not record when a half-hour's DCC kWh arrived,
# so it is measured from the half-hour, plus the kWh's own ~2-day lag.
BILL_OVERDUE_DAYS = 7
KWH_LAG_DAYS = 2
# Octopus's two years (engine._API_RETENTION_DAYS): the asker asks nothing older.
API_RETENTION_DAYS = 730
# A bill holding an import's own cost (block_store.IMPORTED_BILL_LABEL, v4import.transforms).
IMPORTED_LABEL = "IMPORTED"

_EPS_GBP = 1e-5          # a thousandth of a penny: below it is float and rounding noise
_EPS_KWH = 0.0005
_VAT_TOL = 0.005          # |inc/exc - (1 + VAT)|; 0% and 5% are 0.05 apart
_MIN_EXC = 0.001          # GBP; a pair below 0.1p ex-VAT says nothing about VAT


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _beyond(now) -> str:
    """The half-hour start before which Octopus no longer serves a half-hour (naive UTC ISO)."""
    return (now - timedelta(days=API_RETENTION_DAYS)).isoformat()


def _clock(start, now):
    """'waiting' inside the window, 'overdue' past it (design §7 (a): 7 days after the kWh, from
    the half-hour + the kWh's 2-day lag)."""
    age = now - datetime.fromisoformat(start)
    return "overdue" if age >= timedelta(days=BILL_OVERDUE_DAYS + KWH_LAG_DAYS) else "waiting"


# A change to what this module returns for a half-hour must bump server._HEALTH_FILE_V: the health
# strip keeps its month counts across restarts, and its marks see the data, not these rules.
def classify(f: dict, *, smb: bool, now: datetime, held=frozenset(), kept=frozenset(),
             vat_at=None, kind_at=None, split_at=None, export_channel: bool = False,
             export_sensor: bool = True, export_agreement_at=None,
             given_up=frozenset(), export_deleted=()) -> dict:
    """One half-hour's {bill, export, split, flags}. `f` is a get_settlement_facts row. `smb` is
    the install's tariff (engine._import_is_smb_capped); `held` / `kept` are 4.5.23's wait map
    (engine.bill_split_held); `vat_at(start)` the VAT calendar; `kind_at(start)` the half-hour's
    tariff kind, 'intelligent' | 'published' (engine.tariff_kind_at; Intelligent when absent);
    `split_at(start)` whether its agreement has a split from the bill (engine.split_at; `smb`
    when absent). `export_channel`: the main meter has an export channel, so a half-hour whose
    export kWh has not settled is waiting for it, whatever EMT read live.
    `export_sensor`: the main meter's export channel has a live read sensor. Without one, the
    live export kWh is not a reading — finalise stores 0.0 from a channel with no sensor (the
    Mini feeds import only) — so it says nothing about what was exported.
    `export_agreement_at(start)`: True / False whether an export agreement covers the half-hour,
    None where it is not known (engine.export_agreement_at); False makes it `no_agreement`.
    `given_up` (engine.bill_no_cost): half-hours whose cost EMT no longer asks Octopus for (30
    days unanswered) — they read `abandoned` where they would otherwise wait for a bill.
    `export_deleted`: the [from, to) spans the imported-export delete cleared (soft delete design
    §4): an imported half-hour there with no export reads `none`, as decided; elsewhere it is
    `no_data` — Octopus (or the CSV) gave no export figure, a gap.

    The Import bill states say whether the half-hour's cost is final, and whose it is (design
    §9.3). On a published-rate tariff the settled kWh x the published rate is the bill; on an
    Intelligent one only Octopus's cost is final, as a dispatch can re-band the half-hour."""
    start = f["block_start"]
    kwh = _f(f.get("imp_kwh")) or 0.0
    cost = _f(f.get("imp_cost"))
    bill_cost = _f(f.get("bill_cost"))
    src = f.get("source") or ""
    kind = kind_at(start) if kind_at else "intelligent"
    flags = []

    # ── split: IOG-SMB, half-hours with a completed dispatch (first: a green cost reads it) ──
    split = None
    if (split_at(start) if split_at else smb) and f.get("completed"):
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

    # ── Import bill ──
    def estimate():
        # EMT's kWh x rate: on which kWh, inside the window; red past it
        if _clock(start, now) == "overdue":
            return "overdue"
        return "estimate_dcc" if f.get("imp_kwh_api") is not None else "estimate_cad"

    if kwh <= 1e-9:
        # nothing imported: no cost to settle, and no rate to have corrected — a correction made
        # over a span keeps its flag on the half-hours that drew nothing (staging, 20 Oct 2026:
        # 21 of 33 corrected half-hours had no kWh, blue, and a re-fetch had nothing to change)
        bill = "none"
    elif f.get("rate_corrected") or f.get("rate_source") == "corrected":
        bill = "corrected"
    elif bill_cost is not None and str(f.get("bill_label") or "").upper() == IMPORTED_LABEL:
        # the import's own cost as a bill (v4import transform 3c, the CSV import): settle() prices
        # it, so it reads as settled, but the cost is the import's — v4's tariff price on most API
        # history — not Octopus's (27 Oct 2026, the user: imported, not applied)
        bill = "imported"
    elif bill_cost is None and start < _beyond(now):
        # no bill, and past Octopus's two years: nothing will ask for it, so it would wait for good
        # (dev, 27 Oct 2026: 1,242 API-imported half-hours red) — final at EMT's kWh x rate
        bill = "imported"
    elif bill_cost is not None and f.get("rate_source") == "settled":
        # Since engine step 2b the settled cost is kWh x Octopus's band rate, so `differs` says
        # Octopus's own cost is not kWh x its rate — a kWh it priced differently, or an odd rate.
        # (BL-82's `recost_bl82`, which excused a cost equal to kWh x rate, would excuse every one.)
        bill = "applied" if cost is not None and abs(cost - bill_cost) <= _EPS_GBP else "differs"
    elif src == "imported_api" and bill_cost is not None:
        # Octopus's bill is cached for it: whatever the import's no-cost record says, the cost is
        # Octopus's (21 Oct 2026, staging: 15 half-hours of 21 Jul read "no cost, no longer asked"
        # with their bills cached and applied — the record was never cleared)
        bill = "applied" if cost is not None and abs(cost - bill_cost) <= _EPS_GBP else "differs"
    elif src == "imported_api" and kind == "intelligent":
        # no bill yet: the one asker asks Octopus for it as for a live half-hour (engine step 3,
        # decided (b), 24 Oct 2026), so it waits as one does — on its settled kWh (the import's) —
        # until the bill, or the give-up. (Until step 3e an import's no-cost record read
        # "abandoned" at once, and history imported before engine step 2c read "applied".)
        if start in given_up:
            bill = "abandoned"
        else:
            bill = "overdue" if _clock(start, now) == "overdue" else "estimate_dcc"
    elif src == "imported_api":
        bill = "applied"              # a published tariff: settled kWh x the published rate
    elif src.startswith("imported"):
        bill = "imported"             # CSV history: a cost not from Octopus's API
    elif kind == "published" and f.get("imp_kwh_api") is not None:
        bill = "applied"              # settled kWh x the published rate is the bill
    else:
        bill = estimate()             # a cached bill not applied yet reads the same: transient
        if start in given_up:
            bill = "abandoned"        # asked 30 days, unanswered: no longer asked
    if bill == "applied" and split in ("emt", "held", "kept"):
        bill = "applied_emt_split"    # Octopus's cost, EMT's split

    # ── export: no export bill is fetched — kWh x rate is final once the export kWh settles ──
    # Keyed on whether the export kWh has SETTLED, not on the live reading (17 Oct 2026): a meter
    # with no live export sensor has no export kWh until DCC settles it, so an unsettled
    # half-hour read 0 and showed "none", like a night with nothing exported. A live reading of
    # nothing is still "none"; a settled nothing is "none". But with no export SENSOR, the live
    # 0.0 is no reading at all (18 Oct 2026, the user: grey runs between amber on an API-only
    # account) — the half-hour waits for DCC, as a NULL does.
    exp_live = _f(f.get("exp_kwh"))
    exp_api = _f(f.get("exp_kwh_api"))
    exp = exp_api if exp_api is not None else (exp_live or 0.0)
    if (src.startswith("imported") and export_channel and exp_api is None and exp_live is None
            and not any(lo <= start < hi for lo, hi in export_deleted)):
        # an imported half-hour with no export figure at all, where the meter exports: a gap, not a
        # quiet half-hour (21 Oct 2026, staging: three days of March 2025 Octopus returned import
        # for and no export read grey, as nothing exported)
        export = "no_data"
    elif exp_api is not None or src == "imported_api":
        export = "applied" if exp > 1e-9 else "none"
    elif src.startswith("imported"):
        export = "imported" if exp > 1e-9 else "none"
    elif exp > 1e-9 or (export_channel and (exp_live is None or not export_sensor)):
        export = _clock(start, now)
    else:
        export = "none"
    # No export agreement covers it: no export rate, so nothing is paid and nothing will settle
    # into a bill (17 Oct 2026, the user) — not amber, then red for good. An API import's export
    # cost is Octopus's own and stands; CSV history stays CSV history (22 Oct 2026, the user: it
    # had turned grey `no_agreement` and, then an issue, filled the day bars); and a missing export
    # figure there is not a gap — with no agreement there was nothing to miss.
    if (export_agreement_at is not None and export_agreement_at(start) is False
            and (export == "no_data" or (export not in ("none", "imported") and src != "imported_api"))):
        export = "no_agreement"

    if bill_cost is not None:
        bk, api = _f(f.get("bill_kwh")), _f(f.get("imp_kwh_api"))
        if bk is not None and bk > 1e-9 and api is not None and abs(bk - api) > _EPS_KWH:
            flags.append("bill_kwh_differs")
        exc = _f(f.get("bill_cost_exc"))
        if vat_at is not None and exc is not None and exc >= _MIN_EXC:
            if abs(bill_cost / exc - (1.0 + float(vat_at(start)))) > _VAT_TOL:
                flags.append("vat_disagrees")

    if split == "missing":
        flags.append("split_missing")

    return {"bill": bill, "export": export, "split": split, "flags": flags}


def classify_range(facts, yield_every: int = 0, **ctx) -> dict:
    """{block_start: classify(...)} over get_settlement_facts rows.

    `yield_every`: every that many half-hours, a 1 ms sleep, so a long pass on a web thread hands
    the interpreter to the engine loop (staging, 22 Oct 2026: the loop's many short database reads
    each waited for it behind the health count, and the loop was held 14.5 s)."""
    import time as _time
    out = {}
    for i, f in enumerate(facts):
        if yield_every and i and i % yield_every == 0:
            _time.sleep(0.001)
        out[f["block_start"]] = classify(f, **ctx)
    return out
