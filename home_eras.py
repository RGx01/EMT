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
# PR 3 (design §2): eras EMT finds for itself — the found ones the user deleted, not found again; and
# the meter points it has seen, as fingerprints (a hash: never the MPAN or the serial)
DISMISSED_KEY = "home_eras_dismissed"
POINTS_KEY = "meter_points_seen"
MAX_ERAS = 100
KINDS = ("solar", "battery", "heat_pump", "a2a", "ev", "insulation", "house_move", "meter_change", "other")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ID = re.compile(r"[0-9a-f]{8,32}")
_FOUND_KEY = re.compile(r"(move|meter):[0-9a-f]{16}")
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
        fk = str(era.get("found_key") or "")
        if _FOUND_KEY.fullmatch(fk):
            out["found_key"] = fk
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


def save(store, eras: list) -> None:
    """Keep a cleaned list (the engine thread's store: the route hands this to `_engine_write`). A
    found era the user deleted is not found again (PR 3): its key goes to `home_eras_dismissed`."""
    old = {e.get("found_key") for e in load(store) if e.get("found_key")}
    gone = old - {e.get("found_key") for e in eras if e.get("found_key")}
    if gone:
        store.set_meta(DISMISSED_KEY, sorted(set(store.get_meta(DISMISSED_KEY) or []) | gone))
    store.set_meta(META_KEY, eras)


def load(store) -> list:
    """The kept eras, oldest start first; [] when none (or the record is not a list)."""
    try:
        v = store.get_meta(META_KEY) or []
    except Exception:
        return []
    return v if isinstance(v, list) else []


# ── Eras found (PR 3, design §2) ──────────────────────────────────────────────────────────────
def fingerprint(*parts) -> str:
    """16 hex characters of a SHA-256 over `parts`: the same meter point gives the same print, and the
    print gives back nothing of it. Never stored with what it was made from."""
    import hashlib
    return hashlib.sha256("|".join(str(p or "") for p in parts).encode("utf-8")).hexdigest()[:16]


def note_point(seen: list, mpan: str, serial: str, when: str) -> "tuple":
    """The import meter point discovery reports, against the ones seen: (seen', change). `change` is
    None, or {"kind": "house_move" | "meter_change", "key", "date"} where the point differs from the
    last seen — a new supply point a move, the same one with a new serial a meter change. The first
    point seen is the baseline: no change. `when` is the local date it was seen. Pure."""
    if not mpan or not serial:
        return seen, None
    rec = {"mpan": fingerprint("mpan", mpan), "meter": fingerprint("meter", mpan, serial), "seen": when}
    seen = [r for r in (seen or []) if isinstance(r, dict)]
    last = seen[-1] if seen else None
    if last and last.get("mpan") == rec["mpan"] and last.get("meter") == rec["meter"]:
        return seen, None
    seen = (seen + [rec])[-20:]
    if not last:
        return seen, None
    if last.get("mpan") != rec["mpan"]:
        return seen, {"kind": "house_move", "key": "move:" + rec["mpan"], "date": when}
    return seen, {"kind": "meter_change", "key": "meter:" + rec["meter"], "date": when}


def moves_from_agreements(rows: list, local_date) -> list:
    """A house move per supply point after the first, from the stored import agreements: each one's
    first agreement's start, as a local date (`local_date(utc_iso)`). [{"key", "date"}], oldest
    first. Pure."""
    first = {}
    for r in rows or []:
        if r.get("direction") != "import" or not r.get("mpan") or not r.get("valid_from"):
            continue
        if r["mpan"] not in first or r["valid_from"] < first[r["mpan"]]:
            first[r["mpan"]] = r["valid_from"]
    order = sorted(first.items(), key=lambda kv: kv[1])
    return [{"key": "move:" + fingerprint("mpan", m), "date": local_date(vf)} for m, vf in order[1:]]


def found_eras(changes: list, eras: list, dismissed, site: str = "") -> list:
    """The eras to add for `changes` ({"kind", "key", "date"}): one a key, not already an era's and
    not one the user deleted. A house move is named by the site name; no address. Pure."""
    have = {e.get("found_key") for e in eras or []} | set(dismissed or [])
    out = []
    for c in changes:
        if c["key"] in have:
            continue
        have.add(c["key"])
        if c["kind"] == "house_move":
            e = {"label": "Moved house", "kind": "house_move", "start": c["date"],
                 "notes": "Found by EMT: a new supply point in your Octopus agreements."}
            if site:
                e["site"] = site
        else:
            e = {"label": "Meter changed", "kind": "meter_change", "start": c["date"],
                 "notes": "Found by EMT: your meter's serial changed; dated when EMT noticed."}
        e.update({"found": True, "found_key": c["key"]})
        out.append(clean_one(e))
    return out
