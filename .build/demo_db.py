#!/usr/bin/env python3
"""A made-up EMT install for screenshots and demos (2 Nov 2026: the repository going public).

Not anyone's data: a household invented here, seeded, so the same command gives the same database.
"Demo House", postcode prefix SW1A (the placeholder every fixture uses), no MPAN, no account. About
two and a half years of half-hours, Jun 2024 – Oct 2026, on an Octopus Go-shaped tariff (the cheap
window 00:30–04:30) — a published-rate tariff, so the settled kWh × rate is final and nothing waits on
a supplier that is not there:

  - the house: a morning and an evening peak, more in winter, a little noise;
  - an EV charger from Sep 2024: two or three nights a week, in the cheap window;
  - a heat pump from Oct 2024: by the day's temperature, plus hot water at night;
  - solar from Mar 2025: by the season and a day's cloud; what the house does not use is exported;
  - a battery from Mar 2025: charges from the grid in the cheap window, covers the evening.

The device split is EMT's own (`engine._apply_pass2`) and the history is settled by EMT's own sweep
(`tests/tariff_fixtures._sweep`), as an import leaves it. Home eras mark when each arrived.

    python .build/demo_db.py <data dir>      # writes <data dir>/blocks.db (refuses to overwrite)
"""
import math
import os
import random
import sys
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "tests")]

import tariff_fixtures as tf                                    # noqa: E402
from block_store import BlockStore                              # noqa: E402

FROM = datetime(2024, 6, 1, tzinfo=tf.LONDON)
TO = datetime(2026, 11, 1, tzinfo=tf.LONDON)
EV_FROM, HP_FROM, PV_FROM = datetime(2024, 9, 14).date(), datetime(2024, 10, 20).date(), datetime(2025, 3, 8).date()
EXPORT_RATE = 0.15
DEVICES = [("ev_charger", "ev_charger", "EV Charger"), ("heat_pump", "heat_pump", "Heat Pump"),
           ("house_battery", "battery", "House Battery")]
ERAS = [
    {"id": "d0e1a001", "kind": "ev", "label": "First EV", "start": "2024-09-14", "kw": 7.4, "kwh": 64},
    {"id": "d0e1a002", "kind": "heat_pump", "label": "Heat pump fitted", "start": "2024-10-20", "kw": 7.0},
    {"id": "d0e1a003", "kind": "solar", "label": "Solar panels", "start": "2025-03-08", "kw": 4.2},
    {"id": "d0e1a004", "kind": "battery", "label": "Home battery", "start": "2025-03-08", "kwh": 9.5},
    {"id": "d0e1a005", "kind": "insulation", "label": "Loft insulation", "start": "2025-10-04"},
]


def _config():
    meters = {"electricity_main": {
        "meta": {"site": "Demo House", "billing_day": 3, "block_minutes": 30, "timezone": "Europe/London",
                 "currency_symbol": "£", "currency_code": "GBP", "postcode_prefix": "SW1A",
                 "supplier": "octopus"},
        "channels": {"import": {}, "export": {}}}}
    for mid, mtype, name in DEVICES:
        meters[mid] = {"meta": {"sub_meter": True, "meter_type": mtype, "device": name,
                                "parent_meter": "electricity_main"},
                       "channels": {"import": {}}}
    return {"meters": meters}


class _Day:
    """A local day's weather and plan, drawn once."""
    def __init__(self, rng, d):
        doy = d.timetuple().tm_yday
        self.temp = 10.5 - 7.5 * math.cos(2 * math.pi * (doy - 20) / 365) + rng.gauss(0, 2.2)
        self.sun = max(0.05, min(1.0, rng.betavariate(2.2, 1.6)))         # a day's clear-sky share
        self.season = 0.55 + 0.45 * math.cos(2 * math.pi * (doy - 172) / 365)   # 1 in June
        self.ev = d >= EV_FROM and rng.random() < 0.38
        self.ev_kwh = rng.uniform(14, 30) if self.ev else 0.0
        self.cook = rng.choice((34, 35, 36, 37))                           # the evening meal's half-hour
        self.out = rng.random() < 0.12                                     # a day out: less at home
        self.insulated = d >= datetime(2025, 10, 4).date()


def _half_hour(rng, day, loc, cheap, ev_left, batt):
    """(house kWh, {device: grid kWh}, solar kWh, battery discharge kWh, ev_left, battery charge)."""
    hh = loc.hour * 2 + loc.minute // 30
    winter = 1.0 + 0.35 * max(0.0, (12 - day.temp) / 12)
    base = 0.11 + 0.03 * rng.random()
    if 13 <= hh < 18:
        base += 0.22 * rng.random() + 0.08                                 # the morning
    if 33 <= hh < 44:
        base += 0.18 + 0.12 * rng.random()                                 # the evening
    if hh == day.cook:
        base += 0.9 + 0.3 * rng.random()
    if day.out and 18 <= hh < 36:
        base *= 0.55
    house = round(base * winter, 3)
    draws = {}
    # the heat pump: by how cold it is, more in the morning and evening; hot water at night
    if loc.date() >= HP_FROM:
        need = max(0.0, 15.5 - day.temp) * (0.85 if day.insulated else 1.0)
        shape = 1.35 if (12 <= hh < 18 or 32 <= hh < 42) else (0.6 if hh < 10 else 1.0)
        hp = need * 0.035 * shape + (0.55 if hh in (2, 3) else 0.0)
        draws["heat_pump"] = round(hp * (0.9 + 0.2 * rng.random()), 3)
    # the EV: in the cheap window, at 3.6 kWh a half-hour until it is full
    ev = 0.0
    if cheap and ev_left > 0:
        ev = min(3.6, ev_left)
        ev_left -= ev
    if loc.date() >= EV_FROM:
        draws["ev_charger"] = round(ev, 3)
    # solar: a bell over the day, by the season and the day's cloud (4.2 kWp)
    solar = 0.0
    batt_dis = batt_ch = 0.0
    if loc.date() >= PV_FROM:
        frac = (hh + 0.5 - 24) / (9 + 3 * day.season)
        if abs(frac) < 1:
            solar = round(4.2 * 0.5 * math.cos(frac * math.pi / 2) ** 2 * day.season * day.sun
                          * (0.8 + 0.2 * rng.random()), 3)
        # the battery: fills from the grid in the cheap window, covers the evening
        if cheap and batt < 9.0:
            batt_ch = min(1.2, 9.0 - batt)
        elif 32 <= hh < 46 and batt > 0.5:
            batt_dis = min(0.6, batt - 0.5, house + draws.get("heat_pump", 0.0))
        draws["house_battery"] = round(batt_ch, 3)
    return house, draws, solar, batt_dis, ev_left, batt_ch


def build(data_dir):
    path = os.path.join(data_dir, "blocks.db")
    if os.path.exists(path):
        sys.exit("refusing to overwrite " + path)
    os.makedirs(data_dir, exist_ok=True)
    import engine
    st = BlockStore(path)
    st.insert_config_period(_config())
    cp = st.get_current_config_period_id()
    with st._conn:
        st._conn.execute("UPDATE config_periods SET effective_from = ? WHERE id = ?", ("2024-01-01T00:00:00", cp))
    rng = random.Random(20261102)
    t = FROM.astimezone(timezone.utc).replace(tzinfo=None)
    end = TO.astimezone(timezone.utc).replace(tzinfo=None)
    rows, days, ev_left, batt = [], {}, 0.0, 0.0
    while t < end:
        loc = t.replace(tzinfo=timezone.utc).astimezone(tf.LONDON)
        d = loc.date()
        if d not in days:
            days[d] = _Day(rng, d)
            if days[d].ev:
                ev_left += days[d].ev_kwh
        day = days[d]
        rate = tf.inc_rate("go", t)
        cheap = rate < 0.13
        house, draws, solar, batt_dis, ev_left, batt_ch = _half_hour(rng, day, loc, cheap, ev_left, batt)
        batt += batt_ch - batt_dis
        load = house + sum(draws.values()) - batt_dis
        main = round(max(0.0, load - solar), 4)
        export = round(max(0.0, solar - load), 4)
        intensity = 175 + 55 * math.sin(2 * math.pi * (d.toordinal() % 9) / 9) \
            + (35 if 32 <= loc.hour * 2 + loc.minute // 30 < 40 else 0) - 60 * (solar / 2.1)
        split = _pass2(engine, main, rate, draws) if draws else {}
        bs, be = t.isoformat(), (t + timedelta(minutes=30)).isoformat()
        rows.append((bs, be, "electricity_main", main, None, round(main * rate, 6), rate,
                     0.5 * (1 + tf.vat_on(d)), round(main * max(40.0, intensity), 2), export,
                     EXPORT_RATE, round(export * EXPORT_RATE, 6)))
        for mid, k in draws.items():
            g, c = split[mid]
            rows.append((bs, be, mid, k, g, c, rate, 0.0, None, 0.0, None, 0.0))
        t += timedelta(minutes=30)
    with st._conn:
        st._conn.executemany(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, interpolated, imp_kwh, "
            "imp_kwh_grid, imp_cost, imp_rate, standing_charge, carbon_g, exp_kwh, exp_rate, exp_cost, source) "
            "VALUES (?, ?, ?, %d, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'imported_api')" % cp, rows)
    st.set_meta("home_eras", ERAS)
    tf._sweep(st, "go")
    st.close()
    return path


def _pass2(engine, main, rate, draws):
    """What `engine._apply_pass2` writes for the half-hour: {device: (grid kWh, £)}."""
    meta = {mid: mtype for mid, mtype, _ in DEVICES}
    block = {"start": "2026-01-01T00:00:00", "end": "2026-01-01T00:30:00", "interpolated": False,
             "meters": {"electricity_main": {"meta": {"sub_meter": False}, "channels": {"import": {
                 "kwh": main, "kwh_total": main, "rate": rate, "cost": round(main * rate, 6)}}}}}
    for mid, k in draws.items():
        block["meters"][mid] = {"meta": {"sub_meter": True, "meter_type": meta[mid], "parent_meter": "electricity_main"},
                                "channels": {"import": {"kwh": k, "rate": rate, "cost": round(k * rate, 6)}}}
    engine._apply_pass2(block)
    return {mid: (block["meters"][mid]["channels"]["import"]["kwh_grid"],
                  block["meters"][mid]["channels"]["import"]["cost"]) for mid in draws}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(build(sys.argv[1]))
