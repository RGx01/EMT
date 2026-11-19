"""
Eras and chart styles to a file, and from one (19 Nov 2026, the user: "this could allow users to share
themes to import").

What the file carries is what the database keeps of how the charts look and what the home has been:

- **the home's eras** (`home_eras`, the store's meta) — imported, they *replace* the eras the
  instance has (the user);
- **each chart style** — a bar colour and a rate line: the house (the main meter's import), its
  export, each device's import (`meter_channels.bar_color` / `rate_line`, the current config period),
  and the EV of an account with no EV device (`ev_style`, the store's meta).

A style is matched by **what** a meter is, not its id (the user: ids differ between instances): the
house, the export, and each device by its role (`meter_roles.role` — its `meter_type`, else a guess
from its id). Two devices of one role are paired in config order — the first battery's style to the
first battery, the second's to the second. The EV without a device is role `ev`, so a car's colour
crosses between an account with a charger and one without. A style in the file with nothing to land on
is reported as skipped; a meter the file has nothing for keeps its own.

The file holds the colours as drawn (a chosen one, else the palette's by position), so a theme looks
the same wherever it lands. It holds no MPAN, no account and no address: eras keep the site name a
house move was given, as they do on screen.

What stays per browser and is not in the file: Usage Stats' view — its period, metric, series pickers,
Running total's mode and start, Flow's span (localStorage, `emt_bar_state`). Spans are not stored: they
are the tariffs and the eras.
"""
import os
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone

import chart_style
import home_eras
import meter_roles

FORMAT = "emt-customisations"
VERSION = 1
MAX_BYTES = 256 * 1024            # a theme is a few kB; anything near this is not one


class ThemeError(ValueError):
    """A file that cannot be applied, with the reason the page shows."""


def _main_id(cfg):
    return next((m for m, d in ((cfg or {}).get("meters") or {}).items()
                 if not (d.get("meta") or {}).get("sub_meter")), None)


def targets(cfg, ev_shown: bool) -> list:
    """What can take a style here, in the charts' order: [{role, label, where}] — `where` is
    (meter_id, channel), or "ev_dispatch" for the EV without a device. The house, its export, then each
    device in config order, then the EV without a device where the account has had one."""
    meters = (cfg or {}).get("meters") or {}
    main = _main_id(cfg)
    out = []
    if main:
        out.append({"role": "house", "label": "Direct import (the house)", "where": (main, "import")})
        out.append({"role": "export", "label": "Export", "where": (main, "export")})
    for mid, md in meters.items():
        meta = md.get("meta") or {}
        if not meta.get("sub_meter"):
            continue
        out.append({"role": meter_roles.role(mid, meta) or "device", "label": meta.get("device") or mid,
                    "where": (mid, "import")})
    if ev_shown:
        out.append({"role": "ev", "label": "EV", "where": "ev_dispatch"})
    return out


def collect(store, cfg, ev_shown: bool, source: str = "this database") -> dict:
    """The file's content for a store and its current config: the eras and every style as drawn."""
    import energy_charts as _ec
    drawn = _ec.build_meter_styles(cfg)
    main = _main_id(cfg)
    styles = []
    for t in targets(cfg, ev_shown):
        if t["where"] == "ev_dispatch":
            st = _ec.ev_dispatch_style(store)
        else:
            mid, ch = t["where"]
            key = ("electricity_main" if ch == "import" else "electricity_main_export") if mid == main else mid
            st = drawn.get(key)
        if not st:
            continue
        styles.append({"role": t["role"], "label": t["label"], "bar_color": st["bar"],
                       "rate_line": "dashed" if st["dashed"] else "solid"})
    return {"format": FORMAT, "version": VERSION,
            "exported": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": source, "eras": home_eras.load(store), "styles": styles}


def collect_from_backup(zip_path: str, ev_shown_of) -> dict:
    """`collect` for a backup zip's blocks.db: extracted to a temp folder, opened read-only, removed.
    `ev_shown_of(store, cfg)` says whether that database draws the EV without a device."""
    from block_store import BlockStore
    tmp = tempfile.mkdtemp(prefix="emt-theme-")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            name = next((i.filename for i in zf.infolist() if os.path.basename(i.filename) == "blocks.db"), None)
            if not name:
                raise ThemeError("That backup holds no blocks.db")
            db = os.path.join(tmp, "blocks.db")
            with zf.open(name) as src, open(db, "wb") as dst:
                shutil.copyfileobj(src, dst)
        store = BlockStore(db, read_only=True)
        try:
            pid = store.get_current_config_period_id()
            cfg = store.config_from_db(pid) if pid is not None else {}
            return collect(store, cfg, ev_shown_of(store, cfg), source="backup " + os.path.basename(zip_path))
        finally:
            try:
                store._conn.close()
            except Exception:
                pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def parse(data) -> dict:
    """A file's content checked: {eras: cleaned, styles: [{role, label, bar_color, rate_line}]}. Raises
    ThemeError for something that is not one of these files, or eras that cannot be kept. A file
    without `eras` leaves the eras alone; one with an empty list clears them."""
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ThemeError("This is not an EMT eras and colours file")
    if not isinstance(data.get("version"), int) or data["version"] > VERSION:
        raise ThemeError("This file was made by a newer EMT: update EMT to import it")
    out = {"eras": None, "styles": []}
    if "eras" in data:
        try:
            out["eras"] = home_eras.clean(data["eras"])
        except home_eras.EraError as e:
            raise ThemeError("An era in the file cannot be kept: %s" % e)
    styles = data.get("styles") or []
    if not isinstance(styles, list):
        raise ThemeError("The file's styles are not a list")
    for s in styles:
        if not isinstance(s, dict):
            continue
        role = str(s.get("role") or "")[:32]
        bar, line = chart_style.clean_color(s.get("bar_color")), chart_style.clean_rate_line(s.get("rate_line"))
        if not role or not (bar or line):
            continue
        out["styles"].append({"role": role, "label": " ".join(str(s.get("label") or role).split())[:64],
                              "bar_color": bar, "rate_line": line})
    return out


def plan(parsed: dict, tgts: list) -> dict:
    """What applying does, written nowhere: {pairs: [(style, target)], skipped: [style], kept: [target]}.
    Pairs by role, each role's styles and targets in their order. Pure."""
    by_role = {}
    for t in tgts:
        by_role.setdefault(t["role"], []).append(t)
    pairs, skipped, used = [], [], set()
    taken = {}
    for s in parsed["styles"]:
        k = taken.get(s["role"], 0)
        cands = by_role.get(s["role"]) or []
        if k < len(cands):
            pairs.append((s, cands[k]))
            used.add(id(cands[k]))
        else:
            skipped.append(s)
        taken[s["role"]] = k + 1
    return {"pairs": pairs, "skipped": skipped, "kept": [t for t in tgts if id(t) not in used]}


def summary(parsed: dict, p: dict, eras_now: int) -> dict:
    """The page's account of a plan: what replaces what, in words it can show."""
    return {"eras": None if parsed["eras"] is None else len(parsed["eras"]), "eras_now": eras_now,
            "styles": [{"from": s["label"], "to": t["label"], "bar_color": s["bar_color"],
                        "rate_line": s["rate_line"]} for s, t in p["pairs"]],
            "skipped": [s["label"] for s in p["skipped"]],
            "kept": [t["label"] for t in p["kept"]]}


def apply(store, parsed: dict, p: dict, period_id) -> None:
    """Write a plan (the engine thread's store: the route hands this to `_engine_write`): the eras
    replaced, then each paired style written where its target keeps one. No engine restart, as the
    chart styles modal's save."""
    if parsed["eras"] is not None:
        home_eras.save(store, parsed["eras"])
    rows, ev = [], None
    for s, t in p["pairs"]:
        if t["where"] == "ev_dispatch":
            ev = {k: v for k, v in (("bar_color", s["bar_color"]), ("rate_line", s["rate_line"])) if v}
        else:
            rows.append((t["where"][0], t["where"][1], s["bar_color"], s["rate_line"]))
    if rows and period_id is not None:
        chart_style.apply_styles(store, period_id, rows)
    if ev is not None:
        store.set_meta("ev_style", ev or None)
