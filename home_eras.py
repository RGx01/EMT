"""
Home eras (BL-101, home eras design, 1 Nov 2026): a dated note about the home — *Added solar*,
*Increased storage*, *Added ASHP*, *Moved house* — for display only. Never read for pricing,
attribution, settlement or a bill.

Kept in the store's meta as one JSON list (`home_eras`): a short list, read whole; no schema change,
nothing for the v4 importer. `clean` is the one rule for what is kept: anything else in a posted era
is dropped, and an era without a label, a kind or a start is refused.

No address is ever kept (the user): a house move is named by the site name.
"""
import re
import uuid

META_KEY = "home_eras"
MAX_ERAS = 100
KINDS = ("solar", "battery", "heat_pump", "a2a", "ev", "insulation", "house_move", "meter_change", "other")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ID = re.compile(r"[0-9a-f]{8,32}")
_TEXT = {"label": 60, "manufacturer": 60, "model": 60, "site": 60, "notes": 500, "device": 64}
_NUMBER = {"kw": 1000.0, "kwh": 1000.0}


class EraError(ValueError):
    """An era that cannot be kept, with the reason the page shows."""


def _text(v, n):
    s = " ".join(str(v or "").split())          # one line, no runs of spaces
    return s[:n]


def _date(v):
    s = str(v or "").strip()
    if not _DATE.fullmatch(s):
        return None
    y, m, d = int(s[:4]), int(s[5:7]), int(s[8:10])
    if not (1990 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31):
        return None
    return s


def clean_one(era: dict) -> dict:
    """One posted era as kept: its id (made when new), label, kind, start, and the optional fields
    that hold something. Raises EraError for a missing label, an unknown kind, a bad start, or an end
    before its start."""
    if not isinstance(era, dict):
        raise EraError("an era must be an object")
    out = {}
    eid = str(era.get("id") or "").lower()
    out["id"] = eid if _ID.fullmatch(eid) else uuid.uuid4().hex[:12]
    out["label"] = _text(era.get("label"), _TEXT["label"])
    if not out["label"]:
        raise EraError("an era needs a name")
    kind = str(era.get("kind") or "")
    if kind not in KINDS:
        raise EraError("unknown kind: %s" % kind[:20])
    out["kind"] = kind
    start = _date(era.get("start"))
    if not start:
        raise EraError("%s: a start date is needed (YYYY-MM-DD)" % out["label"])
    out["start"] = start
    if era.get("end"):
        end = _date(era.get("end"))
        if not end:
            raise EraError("%s: the end date is not a date" % out["label"])
        if end < start:
            raise EraError("%s: it ends before it starts" % out["label"])
        out["end"] = end
    for k, n in _TEXT.items():
        if k == "label":
            continue
        v = _text(era.get(k), n)
        if v:
            out[k] = v
    for k, hi in _NUMBER.items():
        v = era.get(k)
        if v in (None, ""):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise EraError("%s: %s must be a number" % (out["label"], k))
        if not (0 <= f <= hi):
            raise EraError("%s: %s must be between 0 and %g" % (out["label"], k, hi))
        out[k] = round(f, 3)
    if era.get("found"):                         # an era EMT found for itself (design §2, PR 3)
        out["found"] = True
    return out


def clean(eras) -> list:
    """A posted list of eras as kept, oldest start first; ids made unique."""
    if not isinstance(eras, list):
        raise EraError("eras must be a list")
    if len(eras) > MAX_ERAS:
        raise EraError("at most %d eras" % MAX_ERAS)
    out, seen = [], set()
    for e in eras:
        c = clean_one(e)
        while c["id"] in seen:
            c["id"] = uuid.uuid4().hex[:12]
        seen.add(c["id"])
        out.append(c)
    out.sort(key=lambda e: (e["start"], e["label"]))
    return out


def load(store) -> list:
    """The kept eras, oldest start first; [] when none (or the record is not a list)."""
    try:
        v = store.get_meta(META_KEY) or []
    except Exception:
        return []
    return v if isinstance(v, list) else []
