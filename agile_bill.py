"""Which half-hours an Agile statement rounds, and what that moves (26 Nov 2026).

An Agile statement prices each half-hour at its rate × its kWh to 0.01, half to even (statement_kwh).
The bill card does the same on the account's Agile agreements, import and Agile Outgoing export; this
module finds those agreements and, for a label that sums each day's net (the Billing tab's picker),
what the rounding moves each day by. Its own module, not energy_charts, because the web server's
period list reads it, and the engine's tests stand energy_charts in with a stub.
"""
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import billing
import statement_kwh
import tariff_prices

logger = logging.getLogger("agile_bill")


def agile_windows(agreements, direction):
    """[(valid_from, valid_to | None)] of the agreements in `direction` ('import' | 'export') on an
    Agile tariff (the AGILE family by tariff code, Agile Outgoing included), naive UTC as stored."""
    return [(str(a.get("valid_from") or "")[:19], (str(a["valid_to"])[:19] if a.get("valid_to") else None))
            for a in (agreements or [])
            if a.get("direction") == direction and tariff_prices.is_dynamic_code(a.get("tariff_code") or "")]


def agile_predicates(store):
    """{'import' | 'export': a predicate on a half-hour's naive-UTC start, or None}: the account's
    agreements on Agile (statement_kwh.covers). Both None with no store, no account or no Agile."""
    none = {"import": None, "export": None}
    if store is None:
        return none
    try:
        agr = store.get_tariff_agreements(store.get_kraken_state("kraken_account_number"))
        return {d: statement_kwh.covers(agile_windows(agr, d)) for d in ("import", "export")}
    except Exception as e:
        logger.warning("agile_predicates: reading the Agile agreements failed: %s", e)
        return none


def statement_day_adjustments(store, tz_name, utc_start, utc_end):
    """{local day 'YYYY-MM-DD': £ inc VAT} — what the bill card's Agile rounding (statement_kwh) moves
    each day's net by: import Σ cost × (statement kWh ÷ kWh − 1), less the same on an Agile export.
    For a label that sums each day's net (the Billing tab's picker), so it reads within pennies of the
    card below it. Empty on an account never on Agile."""
    pred = agile_predicates(store)
    if pred["import"] is None and pred["export"] is None:
        return {}
    c = {name: i for i, name in enumerate(billing.BILL_ROW_COLUMNS)}
    tz = ZoneInfo(tz_name or "UTC")
    out = {}
    rows, _segs = store.get_bill_rows(utc_start, utc_end)
    for r in rows:
        if r[c["meter_id"]] != "electricity_main":
            continue
        bs = r[c["block_start"]]
        adj = 0.0
        for side, kk, ck, sign in (("import", "imp_kwh", "imp_cost", 1.0), ("export", "exp_kwh", "exp_cost", -1.0)):
            k, cost = r[c[kk]], r[c[ck]]
            if k and cost and pred[side] is not None and pred[side](bs):
                # the summary credits an export at −|£| (billing._summarise), so the same here
                adj += sign * (cost if side == "import" else abs(cost)) * (statement_kwh.statement_kwh(k) / k - 1.0)
        if adj:
            day = datetime.fromisoformat(str(bs)[:19]).replace(tzinfo=timezone.utc).astimezone(tz).date().isoformat()
            out[day] = out.get(day, 0.0) + adj
    return out
