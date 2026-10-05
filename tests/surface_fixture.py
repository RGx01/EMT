"""A synthetic EMT database for the surface query budgets (workstream D, 5 Oct 2026).

A main meter with an EV charger and a battery, `days` local days of half-hourly blocks ending on
`END`, priced at a two-rate day/night tariff. Deterministic: the same `days` gives the same
database. Shape, not realism — the budgets count statements, and a statement count must not
depend on how much history there is."""
from datetime import datetime, timedelta

from block_store import BlockStore

END = datetime(2026, 10, 1)
CONFIG = {"meters": {
    "electricity_main": {"meta": {"billing_day": 3, "block_minutes": 30,
                                  "timezone": "Europe/London", "currency_symbol": "£",
                                  "currency_code": "GBP"},
                         "channels": {"import": {}, "export": {}}},
    "ev_charger": {"meta": {"sub_meter": True, "meter_type": "ev_charger",
                            "parent_meter": "electricity_main"}, "channels": {"import": {}}},
    "house_battery": {"meta": {"sub_meter": True, "meter_type": "battery",
                               "parent_meter": "electricity_main"}, "channels": {"import": {}}},
}}


def build(path, days):
    st = BlockStore(path)
    st.insert_config_period(CONFIG)
    cp = st.get_current_config_period_id()
    t = END - timedelta(days=days)
    rows = []
    while t < END:
        bs = t.isoformat()
        be = (t + timedelta(minutes=30)).isoformat()
        night = t.hour < 5 or (t.hour == 5 and t.minute == 0) or t.hour >= 23
        rate = 0.07 if night else 0.30
        ev = 2.5 if (night and t.day % 3 == 0) else 0.0
        bat = 0.4 if night else 0.0
        main = 0.3 + ev + bat
        rows.append((bs, be, "electricity_main", main, main - ev - bat, rate, round(main * rate, 6),
                     0.0, 0.0, 0.5, round(main * 120, 2)))
        rows.append((bs, be, "ev_charger", ev, None, rate, round(ev * rate, 6), 0, 0, 0, None))
        rows.append((bs, be, "house_battery", bat, None, rate, round(bat * rate, 6), 0, 0, 0, None))
        t += timedelta(minutes=30)
    with st._conn:
        st._conn.executemany(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "imp_kwh_remainder, imp_rate, imp_cost, exp_kwh, exp_cost, standing_charge, carbon_g) "
            "VALUES (?, ?, ?, %d, ?, ?, ?, ?, ?, ?, ?, ?)" % cp, rows)
    st.close()
    return path
