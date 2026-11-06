"""
Agile for everyone (BL-103, agile for everyone design, 1 Nov 2026): Octopus's Agile prices in the
account's region, for an account not on Agile — the Overview's Agile card on every account, as
*Agile in your region*, and what the last 30 days would have cost at them.

Octopus publishes Agile's prices for anyone. The region is the letter that ends the account's own
tariff code; the product (its code changes as Octopus launches versions) is read from the product
list once a week. The prices are fetched once a day after Octopus publishes tomorrow's (about 4pm),
through 23:00 local, where Agile's day ends, kept in the store's meta, so a restart asks for nothing it has — within the API rule (capped,
paced, persisted). Display only: nothing that prices a half-hour reads them.

This module is the rules, pure; `engine._fetch_agile_public` asks Octopus and keeps the record.
"""
import re
from datetime import datetime, timedelta, timezone

META_KEY = "agile_public"
KEEP_DAYS = 32                  # the comparison's 30 days, and a little over
COMPARE_DAYS = 30
PRODUCTS_EVERY = timedelta(days=7)
PUBLISH_LOCAL = (16, 30)        # Octopus publishes tomorrow's about 4pm; asked at 16:30 local
DAY_END_HOUR = 23               # Agile's day runs 23:00 to 23:00, so what is published stops there
RETRY_AFTER = timedelta(minutes=90)
MAX_TRIES_A_DAY = 4
_REGION = re.compile(r"[A-P]")


def hidden_channels(settings: dict) -> set:
    """The directions whose Overview card the user has hidden (the settings `agile_hide_import`,
    `agile_hide_export`). A hidden direction is neither shown nor, if public, fetched."""
    return {ch for ch in ("import", "export") if (settings or {}).get("agile_hide_" + ch)}


def region_of(tariff_code: str) -> "str | None":
    """The region letter that ends a tariff code (E-1R-VAR-22-11-01-C → C), or None."""
    part = str(tariff_code or "").rsplit("-", 1)[-1].upper()
    return part if _REGION.fullmatch(part) else None


def pick_product(products: list, direction: str) -> "str | None":
    """The newest Agile product still on sale in `direction` ('import' | 'export'): AGILE codes, an
    export one OUTGOING, the import ones not; none with an `available_to`."""
    out = []
    for p in products or []:
        code = str(p.get("code") or "").upper()
        if not code.startswith("AGILE-") or p.get("available_to"):
            continue
        is_out = "OUTGOING" in code
        if (direction == "export") != is_out:
            continue
        if p.get("is_business") or p.get("is_prepay"):
            continue
        out.append((str(p.get("available_from") or ""), code))
    return max(out)[1] if out else None


def tariff_code(product: str, region: str) -> str:
    return "E-1R-%s-%s" % (product, region)


def _slot(iso: str) -> str:
    """A record's start as a naive-UTC half-hour, 'YYYY-MM-DDTHH:MM:SS'."""
    s = str(iso).replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(second=0, microsecond=0).isoformat()


def merge(kept: list, records: list, now_utc: datetime, price_key: str = "value_inc_vat") -> list:
    """The kept prices [[slot, pence]] with Octopus's `records` laid over them (a half-hour each),
    oldest first, none older than KEEP_DAYS."""
    by = {s: p for s, p in (kept or [])}
    for r in records or []:
        v = r.get(price_key)
        if v is None or not r.get("valid_from"):
            continue
        by[_slot(r["valid_from"])] = round(float(v), 4)
    floor = (now_utc - timedelta(days=KEEP_DAYS)).isoformat()
    return [[s, by[s]] for s in sorted(by) if s >= floor]


def target_through(now_local: datetime, tz) -> datetime:
    """How far the prices should reach, naive UTC: 23:00 tomorrow once Octopus has published (16:30
    local), 23:00 today before. Not midnight (6 Nov 2026): Agile's day ends at 23:00, so midnight was
    never reached, every check found the prices short, and the day's four tries went by 04:30, none
    left for tomorrow's at 4pm."""
    day = now_local.date() + timedelta(days=1 if (now_local.hour, now_local.minute) >= PUBLISH_LOCAL else 0)
    end = datetime(day.year, day.month, day.day, DAY_END_HOUR, tzinfo=tz)
    return end.astimezone(timezone.utc).replace(tzinfo=None)


def through_of(prices: list) -> "datetime | None":
    """The end of the last kept half-hour."""
    return datetime.fromisoformat(prices[-1][0]) + timedelta(minutes=30) if prices else None


def due(rec: dict, now_utc: datetime, now_local: datetime, tz, channels) -> bool:
    """Whether to ask Octopus now: a channel's prices short of `target_through`, and not asked in the
    last 90 minutes, nor more than MAX_TRIES_A_DAY times today (local). Pure."""
    rec = rec or {}
    want = target_through(now_local, tz)
    short = any((through_of((rec.get(ch) or {}).get("prices") or []) or datetime.min) < want - timedelta(minutes=30)
                for ch in channels)
    if not short:
        return False
    last = rec.get("asked")
    if last and now_utc - datetime.fromisoformat(last) < RETRY_AFTER:
        return False
    today = now_local.date().isoformat()
    return int((rec.get("tries") or {}).get(today, 0)) < MAX_TRIES_A_DAY


def products_due(rec: dict, now_utc: datetime) -> bool:
    last = (rec or {}).get("products_checked")
    return not last or now_utc - datetime.fromisoformat(last) >= PRODUCTS_EVERY


def window(prices: list, now_utc: datetime, back_hours: int = 24, ahead_hours: int = 24) -> list:
    """The kept prices from `back_hours` before now to `ahead_hours` after."""
    lo = (now_utc - timedelta(hours=back_hours)).isoformat()
    hi = (now_utc + timedelta(hours=ahead_hours)).isoformat()
    return [[s, p] for s, p in prices or [] if lo <= s < hi]


def compare(prices: list, slots: list) -> "dict | None":
    """What `slots` [(slot, kwh, cost £)] — the account's own half-hours — would have cost at these
    prices (pence), against what they did: {kwh, agile £, actual £, days}, over the half-hours with
    both a price and kWh. None with none. Unit rates only: no standing charge either way."""
    by = {s: p for s, p in prices or []}
    kwh = agile = actual = 0.0
    days = set()
    for s, k, c in slots or []:
        p = by.get(s)
        if p is None or not k or k <= 0:
            continue
        kwh += k
        agile += k * p / 100.0
        actual += float(c or 0.0)
        days.add(s[:10])
    if not days:
        return None
    return {"kwh": round(kwh, 3), "agile": round(agile, 2), "actual": round(actual, 2), "days": len(days)}
