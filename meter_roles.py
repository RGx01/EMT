"""What a meter IS, and which meters are one (14 Nov 2026, the user: "a combination of type and id").

The type says what a meter is; the id says which one. A meter's role is its `meter_type`
when it has one — the setup wizard writes one for every device — and is guessed from the
words of its id only when it has none (a hand-written or older config). Until now the
display and setup code tested the id as a substring wherever it needed the EV, so a
wizard-made `sub_meter_<n>` was never the EV by id, and a battery whose id merely
contained "ev" (`battery_level`) was taken for it even though its type said battery.
The pricing core (settle, the engine's device fit) reads `meter_type` alone; this is for
everything else.

Roles: "ev", "battery", "heat_pump", "inverter". An explicit type outside these is its
own role ("other" stays "other"), never re-guessed from the id.
"""
import re

# The derived EV is typed 'ev_charger' (settle.EV_METER_TYPES); the wizard writes 'ev'.
_TYPE_ROLE = {"ev_charger": "ev"}

# Whole words of an id or name, in the order a guess tries them (the old substring tests'
# order: an id naming both a charger and a battery is the EV).
_WORDS = (
    ("ev", ("ev", "charger", "evcharger")),
    ("battery", ("battery", "batt")),
    ("heat_pump", ("heat", "pump", "heatpump")),
    ("inverter", ("solar", "pv", "inv", "inverter")),
)


def role_from_name(name):
    """The role an id or display name's words name, or None. `ev_charger`, `EV Charger` and
    `ev_dispatch` are the EV; `battery_level` and `devices` are not."""
    words = set(re.split(r"[^a-z0-9]+", str(name or "").lower()))
    for role, keys in _WORDS:
        if words.intersection(keys):
            return role
    return None


def role(meter_id, meta=None):
    """The meter's role: its `meter_type` if it has one (normalised), else a guess from its id."""
    t = ((meta or {}).get("meter_type") or "").strip()
    if t:
        return _TYPE_ROLE.get(t, t)
    return role_from_name(meter_id)


def meters_of(cfg, wanted, *, sub_meters_only=False):
    """Every meter id in `cfg` whose role is `wanted`, in config order."""
    out = []
    for mid, md in ((cfg or {}).get("meters") or {}).items():
        meta = (md or {}).get("meta") or {}
        if sub_meters_only and not meta.get("sub_meter"):
            continue
        if role(mid, meta) == wanted:
            out.append(mid)
    return out
