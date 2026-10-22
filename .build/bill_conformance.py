#!/usr/bin/env python3
"""
Tier-1 conformance: check EMT's stored pricing against OCTOPUS'S OWN BILL.

WHY THIS EXISTS
    v5 may adopt `reprice.py` as its pricing core. Before that, we need to know
    whether v4's pricing agrees with the supplier — because "reprice.py matches
    v4" is worthless if v4 is wrong, and 4.5.14's BL-71 is proof that it can be.

    The obvious harness — replay blocks and diff against what v4 stored — is a
    TAUTOLOGY waiting to happen: v4 wrote those values, so reconstructing v4's
    inputs from them and checking you get them back proves nothing. `measured_cost`
    escapes that. It is Octopus's per-slot billed cost, label and four-bucket
    Home/EV breakdown, cached immutably. It was produced by NEITHER implementation,
    so it can referee both.

    This script is stage 1: assay the corpus and measure V4 against it. Stage 2
    (reprice.py through the same referee) is only worth writing if stage 1 shows
    the corpus is big enough to mean anything — which is exactly what this tells
    you. Run it before deciding.

WHAT THE REFEREE CAN AND CANNOT ADJUDICATE  (read this before trusting a verdict)

  * COST  — yes. `cost_incl` is what Octopus charged.
  * RATE + BAND — yes. The four-bucket breakdown says which bucket carried the
    energy at what rate; that is the supplier's own band attribution.
  * EV QUANTITY — **NO.** v4's own comment in `apply_measured_to_block` says why:

        "Octopus's settled four-bucket EV_DEVICE total OVER-attributes on slots
         where another grid load (e.g. a home battery grid-charging overnight)
         drew concurrently INSIDE the dispatch window: the breakdown has no
         battery bucket, so that concurrent draw lands in EV."

    So the bill sets the EV rate and band, never the quantity. This script does
    NOT check kwh_ev against the bill, and a version that did would report a
    stream of false divergences on any account with a battery.

  * CIRCULAR ROWS — the bill is always applied (v4's `_MEASURED_APPLY` was True), so where
    `rate_source='measured'` v4 COPIED the bill into the block. Checking those is
    checking the bill against itself. They are excluded and counted separately;
    a high circular share is itself a finding, because it means v4's stored
    pricing on those slots is not independent evidence of anything.

CORPUS SHAPE — the limit that decides whether tier 1 is viable
    `measured_cost` is only ever populated for DISPATCHED slots on an IOG tariff
    at or after `_measured_floor()` (the current IOG-SMB agreement start). It is
    not a sample of history; it is a window onto the slots where the EV/house
    split actually does something. Good for the interesting case, silent on
    plain house-only slots — which is fine, since those are one rate and no split.

USAGE
    python3 .build/bill_conformance.py <db_path> [--json] [--tol 0.005] [--limit N]

    Opens the database READ-ONLY (sqlite `mode=ro`), so it is safe to point at a
    live copy — but take a copy anyway, because a database being written while it
    is read is a moving target, not because this can damage it.
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from urllib.request import pathname2url

TOL_COST = 0.005        # £ — half a penny; below this the bill and a clean rate agree
TOL_RATE = 1e-4         # £/kWh


def _f(v):
    return None if v is None else float(v)


def _close(a, b, tol):
    if a is None or b is None:
        return None            # unknowable, not a pass and not a failure
    return abs(float(a) - float(b)) <= tol


def _band_of(rate, day_rate, off_rate):
    """Which band a stored rate sits in, by nearest observed tariff value."""
    if rate is None or day_rate is None or off_rate is None:
        return None
    return "off_peak" if abs(rate - off_rate) <= abs(rate - day_rate) else "peak"


def _table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def analyse(db_path, tol_cost=TOL_COST, limit=0):
    # STRICTLY read-only. This will be pointed at a live add-on database sooner or
    # later, so it opens through the `mode=ro` URI rather than a plain connect: no
    # WAL recovery, no -shm/-wal creation, no schema migration, no writes possible
    # even by accident. A missing file raises here instead of being created empty.
    if not os.path.exists(db_path):
        return {"ok": False, "reason": "no such database: %s" % db_path}
    conn = sqlite3.connect("file:%s?mode=ro" % pathname2url(db_path), uri=True)
    conn.row_factory = sqlite3.Row

    if not _table_exists(conn, "measured_cost"):
        return {"ok": False,
                "reason": "no measured_cost table — this database predates 4.5.7, "
                          "so there is no bill to check against"}

    mc_cols = _cols(conn, "measured_cost")
    blk_cols = _cols(conn, "blocks")
    has_buckets = {"home_kwh", "home_rate", "ev_kwh", "ev_rate"} <= mc_cols
    has_rate_source = "rate_source" in blk_cols

    total_mc = conn.execute("SELECT COUNT(*) FROM measured_cost").fetchone()[0]
    if total_mc == 0:
        return {"ok": False, "total_measured_rows": 0,
                "reason": "measured_cost is EMPTY — the settled-cost fetch has never "
                          "stored a row on this database. Tier 1 has no corpus here; "
                          "that is a finding, not an error."}

    sel = ["mc.slot_start AS slot", "mc.cost_incl", "mc.cost_excl", "mc.label",
           "mc.kwh AS bill_kwh",
           "b.imp_kwh", "b.imp_kwh_api", "b.imp_rate", "b.imp_cost",
           "b.imp_kwh_ev", "b.imp_cost_ev", "b.imp_rate_ev",
           "b.rate_corrected", "b.source"]
    if has_buckets:
        sel += ["mc.home_kwh", "mc.home_rate", "mc.ev_kwh", "mc.ev_rate"]
    if has_rate_source:
        sel += ["b.rate_source"]

    q = (f"SELECT {', '.join(sel)} FROM measured_cost mc "
         "JOIN blocks b ON b.block_start = mc.slot_start "
         "AND b.meter_id = 'electricity_main' "
         "WHERE mc.direction = 'CONSUMPTION' ORDER BY mc.slot_start")
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = conn.execute(q).fetchall()

    # ── observed tariff values, taken from the BILL only ────────────────────
    # Used to say which band a stored rate sits in. Derived from the referee,
    # never from block_segments (which v4 wrote — that way lies the tautology).
    day_rates, ev_rates = Counter(), Counter()
    if has_buckets:
        for r in rows:
            if r["home_rate"] is not None and (r["home_kwh"] or 0) > 1e-9:
                day_rates[round(float(r["home_rate"]), 6)] += 1
            if r["ev_rate"] is not None and (r["ev_kwh"] or 0) > 1e-9:
                ev_rates[round(float(r["ev_rate"]), 6)] += 1
    house_off = min(day_rates) if day_rates else None
    house_day = max(day_rates) if day_rates else None

    out = {
        "ok": True,
        "db": db_path,
        "total_measured_rows": total_mc,
        "joined_to_a_block": len(rows),
        "buckets_available": has_buckets,
        "tolerance_gbp": tol_cost,
        "corpus": {},
        "cost": {"agrees": 0, "differs": 0, "unknowable": 0, "trivial": 0,
                 "examples": []},
        "ev_rate": {"agrees": 0, "differs": 0, "unknowable": 0, "examples": []},
        "band": {"agrees": 0, "differs": 0, "unknowable": 0, "examples": []},
        "excluded_circular": 0,
        "notes": [],
    }
    if not has_buckets:
        out["notes"].append(
            "No four-bucket columns on this database: only the total cost can be "
            "refereed. Rate and band checks are reported as unknowable, not as passes.")

    labels = Counter()
    first = last = None
    ev_refereeable = ev_circular = 0      # where the split actually does something
    for r in rows:
        slot = r["slot"]
        first = slot if first is None else min(first, slot)
        last = slot if last is None else max(last, slot)
        labels[r["label"] or "(none)"] += 1

        # Circular: v4 copied the bill into this block, so it cannot referee itself.
        rs = (r["rate_source"] if has_rate_source else None) or ""
        if rs in ("measured", "settled", "corrected") or (r["rate_corrected"] or 0):
            out["excluded_circular"] += 1
            if has_buckets and (r["ev_kwh"] or 0) > 1e-9:
                ev_circular += 1
            continue
        if has_buckets and (r["ev_kwh"] or 0) > 1e-9:
            ev_refereeable += 1

        # ── COST ────────────────────────────────────────────────────────────
        # A ZERO row is the bill confirming nothing was drawn (cost 0, kwh 0). Scoring
        # 0.00 == 0.00 as agreement inflates the rate with slots that had no pricing to
        # get wrong, so those are counted apart.
        trivial = ((r["label"] == "ZERO")
                   or ((_f(r["imp_kwh"]) or 0.0) <= 1e-9
                       and abs(_f(r["cost_incl"]) or 0.0) <= 1e-9))
        v = _close(_f(r["imp_cost"]), _f(r["cost_incl"]), tol_cost)
        if trivial:
            out["cost"]["trivial"] += 1
        elif v is None:
            out["cost"]["unknowable"] += 1
        elif v:
            out["cost"]["agrees"] += 1
        else:
            out["cost"]["differs"] += 1
            if len(out["cost"]["examples"]) < 20:
                out["cost"]["examples"].append(
                    {"slot": slot, "emt": _f(r["imp_cost"]), "bill": _f(r["cost_incl"]),
                     "delta": round((_f(r["imp_cost"]) or 0) - (_f(r["cost_incl"]) or 0), 6),
                     "kwh": _f(r["imp_kwh"])})

        # ── EV RATE (never EV quantity — see the module docstring) ──────────
        if has_buckets and (r["ev_kwh"] or 0) > 1e-9:
            v = _close(_f(r["imp_rate_ev"]), _f(r["ev_rate"]), TOL_RATE)
            if v is None:
                out["ev_rate"]["unknowable"] += 1
            elif v:
                out["ev_rate"]["agrees"] += 1
            else:
                out["ev_rate"]["differs"] += 1
                if len(out["ev_rate"]["examples"]) < 20:
                    out["ev_rate"]["examples"].append(
                        {"slot": slot, "emt": _f(r["imp_rate_ev"]),
                         "bill": _f(r["ev_rate"])})
        elif has_buckets:
            out["ev_rate"]["unknowable"] += 1

        # ── BAND: EMT's stored main rate vs the bill's label ────────────────
        lbl = r["label"]
        emt_band = _band_of(_f(r["imp_rate"]), house_day, house_off)
        # "ZERO" is not a band — it is the bill saying no energy was drawn. Mapping it
        # onto peak/off-peak (which an `else: peak` does silently) invents a verdict.
        if lbl in (None, "mixed", "ZERO") or emt_band is None:
            out["band"]["unknowable"] += 1
        else:
            bill_band = "off_peak" if lbl == "OFF_PEAK" else "peak"
            if emt_band == bill_band:
                out["band"]["agrees"] += 1
            else:
                out["band"]["differs"] += 1
                if len(out["band"]["examples"]) < 20:
                    out["band"]["examples"].append(
                        {"slot": slot, "emt": emt_band, "bill": bill_band,
                         "emt_rate": _f(r["imp_rate"])})

    out["corpus"] = {
        "first_slot": first, "last_slot": last,
        "refereeable_with_ev_energy": ev_refereeable,
        "circular_with_ev_energy": ev_circular,
        "labels": dict(labels),
        "observed_house_rates": dict(day_rates),
        "observed_ev_rates": dict(ev_rates),
        "refereeable": len(rows) - out["excluded_circular"],
    }
    if out["excluded_circular"]:
        share = out["excluded_circular"] / max(1, len(rows))
        out["notes"].append(
            f"{out['excluded_circular']} of {len(rows)} rows ({share:.0%}) are circular "
            "(rate_source='measured'/'corrected'): v4 copied the bill into the block, so "
            "they are not independent evidence and are excluded from every check.")
    if has_buckets and ev_circular and not ev_refereeable:
        out["notes"].append(
            f"STRUCTURAL BIAS: all {ev_circular} slots carrying EV bucket energy are "
            "circular, and NONE of the refereeable slots carry any. The corpus therefore "
            "says nothing about the house/EV split, the cap, or band attribution on "
            "dispatched slots — which is the entire pricing question. Agreement here is "
            "agreement on single-rate house-only slots.")
    if out["corpus"]["refereeable"] < 500:
        out["notes"].append(
            f"Only {out['corpus']['refereeable']} refereeable slots. That is a thin corpus "
            "for a pricing-core decision — tier 1 alone probably cannot settle it.")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--tol", type=float, default=TOL_COST)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    rep = analyse(a.db, tol_cost=a.tol, limit=a.limit)
    if a.json:
        print(json.dumps(rep, indent=2))
        return 0 if rep.get("ok") else 2

    if not rep.get("ok"):
        print("NO CORPUS: " + rep["reason"])
        return 2

    c = rep["corpus"]
    print(f"bill corpus in {rep['db']}")
    print(f"  measured_cost rows      : {rep['total_measured_rows']}")
    print(f"  joined to a block       : {rep['joined_to_a_block']}")
    print(f"  excluded as circular    : {rep['excluded_circular']}")
    print(f"  REFEREEABLE             : {c['refereeable']}")
    print(f"  range                   : {c['first_slot']} .. {c['last_slot']}")
    print(f"  four-bucket split       : {'yes' if rep['buckets_available'] else 'NO'}")
    print(f"  bill labels             : {c['labels']}")
    print(f"  refereeable WITH EV kWh : {c['refereeable_with_ev_energy']}")
    print(f"  circular   WITH EV kWh  : {c['circular_with_ev_energy']}")
    print(f"  house rates seen        : {c['observed_house_rates']}")
    print(f"  ev rates seen           : {c['observed_ev_rates']}")
    print()
    for k, title in (("cost", "EMT cost vs billed cost"),
                     ("ev_rate", "EMT EV rate vs billed EV rate"),
                     ("band", "EMT band vs billed label")):
        s = rep[k]
        n = s["agrees"] + s["differs"]
        pct = f"{100.0 * s['agrees'] / n:.2f}%" if n else "n/a"
        extra = f", {s['trivial']} trivial (zero-energy)" if s.get("trivial") else ""
        print(f"{title}: {s['agrees']}/{n} agree ({pct}), "
              f"{s['differs']} differ, {s['unknowable']} unknowable{extra}")
        for e in s["examples"][:5]:
            print(f"    {e}")
    if rep["notes"]:
        print()
        for nt in rep["notes"]:
            print("NOTE: " + nt)
    print("\nEV QUANTITY IS NOT CHECKED — the bill over-attributes it on any account "
          "with a concurrent grid load. See the module docstring.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
