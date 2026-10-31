"""
A tariff's prices as dated price periods, for display (G2 design, 31 Oct 2026).

Octopus publishes a tariff's prices as records, and how many depends on the tariff:

- a standing charge or a flat unit rate: one record per price change;
- Go and the uncapped Intelligent: two windowed records a day (off-peak 23:30-05:30 local, peak the
  rest), about 730 a year for two prices;
- Agile: a record every half-hour.

`summarise` turns an agreement's records into the prices: one period per price, per band, from and
to. Consecutive records of a band with the same value become one period. A windowed band is named by
its local window; with exactly two windows the cheaper is `off_peak` and the other `peak`. A tariff
whose unit rate changes by the half-hour (the AGILE family, or more than 8 windows in a local day) is
`dynamic`: one row, no value. Tracker, one rate a day, is not dynamic: it is a period a day.

Display only: nothing that prices a half-hour reads these. Values are pence, ex-VAT, as published.
"""
from datetime import datetime, timedelta, timezone

import tariff_names

DYNAMIC_FAMILIES = ("AGILE",)        # the product part's family: Agile and Agile Outgoing
MAX_WINDOWS_A_DAY = 8                # more than this in a local day is half-hourly (Agile has 48)
_FLAT = timedelta(hours=23)          # a record this long or longer is a flat price, not a window
_EPS = 1e-9


def _dt(iso):
    return datetime.fromisoformat(str(iso)[:19])


def _local(iso, tz):
    return _dt(iso).replace(tzinfo=timezone.utc).astimezone(tz)


def is_dynamic_code(tariff_code: str) -> bool:
    """A tariff named as changing by the half-hour (the AGILE family)."""
    product = tariff_names.product_part(tariff_code).upper()
    return any(f in product for f in DYNAMIC_FAMILIES)


def _mins(hhmm):
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def _within(part, whole):
    """A local window (start, end) inside another, either may wrap past midnight."""
    def span(w):
        a, b = _mins(w[0]), _mins(w[1])
        return a, (b if b > a else b + 1440)
    pa, pb = span(part)
    wa, wb = span(whole)
    return any(wa <= pa + k and pb + k <= wb for k in (0, 1440, -1440))


def _merge(records):
    """[(from, to, value)] of one band, sorted → price periods: a new one where the value changes."""
    out = []
    for vf, vt, v in records:
        if out and abs(out[-1]["value_exc"] - v) <= _EPS:
            out[-1]["valid_to"] = vt
        else:
            out.append({"value_exc": v, "valid_from": vf, "valid_to": vt})
    return out


def summarise(periods, *, kind: str, tariff_code: str, tz_name: str = "Europe/London") -> list:
    """An agreement's published records [(valid_from, valid_to | None, value)], naive UTC, as price
    periods [{band, window, value_exc, valid_from, valid_to}], oldest first.

    `kind` 'standing': one band (''). `kind` 'unit': flat records band ''; windowed records by their
    local window ('23:30-05:30'), named off_peak / peak where there are exactly two; a dynamic tariff
    one row {band: 'dynamic', value_exc: None} over the records' span."""
    from zoneinfo import ZoneInfo
    recs = sorted((p for p in periods if p and p[0]), key=lambda p: p[0])
    if not recs:
        return []
    span = (recs[0][0], recs[-1][1])

    def dynamic():
        return [{"band": "dynamic", "window": None, "value_exc": None,
                 "valid_from": span[0], "valid_to": span[1]}]

    if kind == "standing":
        return [dict(r, band="", window=None) for r in _merge(recs)]
    if is_dynamic_code(tariff_code):
        return dynamic()

    tz = ZoneInfo(tz_name)
    flat, keyed, per_day = [], [], {}
    for vf, vt, v in recs:
        if vt is None or _dt(vt) - _dt(vf) >= _FLAT:
            flat.append((vf, vt, v))
            continue
        a, b = _local(vf, tz), _local(vt, tz)
        keyed.append(((a.strftime("%H:%M"), b.strftime("%H:%M")), (vf, vt, v)))
        day = a.date()
        per_day[day] = per_day.get(day, 0) + 1
    # A window seen once is a fragment — a record clipped at the agreement's edge (00:00-05:30 of a
    # 23:30-05:30 night) — and goes to the recurring window that holds it.
    seen = {}
    for w, _ in keyed:
        seen[w] = seen.get(w, 0) + 1
    full = [w for w, n in seen.items() if n > 1]
    windowed = {}
    for w, r in keyed:
        if seen[w] == 1:
            w = next((f for f in full if _within(w, f)), w)
        windowed.setdefault("%s-%s" % w, []).append(r)
    if per_day and max(per_day.values()) > MAX_WINDOWS_A_DAY:
        return dynamic()

    names = {w: "" for w in windowed}
    if len(windowed) == 2:
        w1, w2 = sorted(windowed, key=lambda w: sum(r[2] for r in windowed[w]) / len(windowed[w]))
        names = {w1: "off_peak", w2: "peak"}
    out = [dict(r, band="", window=None) for r in _merge(flat)]
    for w, rs in windowed.items():
        out.extend(dict(r, band=names[w], window=w) for r in _merge(rs))
    out.sort(key=lambda r: (r["valid_from"], r["band"]))
    return out


def counts(rows) -> dict:
    """{band: periods} of a summary, for the log line (G2 §2's open question, measured)."""
    out = {}
    for r in rows:
        key = r["band"] or (r["window"] or "flat")
        out[key] = out.get(key, 0) + 1
    return out
