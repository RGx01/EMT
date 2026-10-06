"""Synthetic EMT databases, one per tariff shape — the golden outputs step 5 must not move
(5 Oct 2026).

Every real database on hand is Intelligent Octopus Go, so a parity check on them alone cannot see
a regression for anyone else. These are the other shapes:

  flat    one rate all day
  e7      Economy 7: a clock-determined night (00:30–07:30 local) and day
  go      Octopus Go: a short cheap window (00:30–04:30 local)
  agile   a different rate every half-hour, including zero and negative (plunge) slots

each with or without devices (an EV charger and a battery), and Economy 7 with many (five heat
pumps and three batteries). 20 Sep – 10 Oct 2026, so the 1 Oct
VAT change (5% → 0%) falls inside. Segments and ex-VAT are built the way production builds them:
the stored inc rate, then v5's own historical re-price sweep with a rate schedule matching the
tariff. Deterministic — the same arguments give the same database.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

from block_store import BlockStore

LONDON = ZoneInfo("Europe/London")
FROM = datetime(2026, 9, 20, tzinfo=LONDON)
TO = datetime(2026, 10, 11, tzinfo=LONDON)
VAT_CHANGE = datetime(2026, 10, 1).date()
TARIFFS = ("flat", "e7", "go", "agile")


def vat_on(local_day):
    return 0.05 if local_day < VAT_CHANGE else 0.0


def exc_rate(tariff, utc):
    """The tariff's ex-VAT rate (£/kWh) for the half-hour starting at `utc` (naive UTC)."""
    loc = utc.replace(tzinfo=timezone.utc).astimezone(LONDON)
    hm = loc.hour * 60 + loc.minute
    if tariff == "flat":
        return 0.24
    if tariff == "e7":
        return 0.115 if 30 <= hm < 450 else 0.29
    if tariff == "go":
        return 0.08 if 30 <= hm < 270 else 0.27
    if tariff == "agile":                             # a day's curve, a peak, plunges
        k = int(utc.timestamp() // 1800)
        # A day's curve: a triangle wave on the half-hour, lowest at 04:00, highest at 16:00. Not
        # math.sin — libm differs between platforms in the last bit, and rounding to 6 dp then
        # moved every Agile golden on CI's Linux against macOS (6 Oct 2026). +, −, ×, ÷ are
        # exact under IEEE on every platform.
        slot = (hm // 30 + 40) % 48                   # 0 at 04:00
        tri = 1 - abs(slot - 24) / 24                 # 0 at 04:00 … 1 at 16:00
        r = 0.08 + 0.16 * tri + 0.01 * ((k * 7919) % 5 - 2)
        if 960 <= hm < 1140:
            r += 0.14                                 # 16:00–19:00 peak
        if loc.day % 4 == 0 and 720 <= hm < 870:
            r = -0.05                                 # a midday plunge every fourth day
        if loc.day % 9 == 0 and 780 <= hm < 810:
            r = 0.0                                   # a free slot
        return round(r, 6)
    raise ValueError(tariff)


def inc_rate(tariff, utc):
    loc_day = utc.replace(tzinfo=timezone.utc).astimezone(LONDON).date()
    return round(exc_rate(tariff, utc) * (1 + vat_on(loc_day)), 6)


class _Exc:
    def __init__(self, tariff): self.t = tariff
    def resolve(self, ts): return exc_rate(self.t, _naive(ts))
    def flat_rate(self): return exc_rate(self.t, _naive(FROM))


class Schedule:
    """The rate-schedule interface the sweep reads: inc via resolve(), exc via .exc."""
    def __init__(self, tariff): self.t = tariff; self.exc = _Exc(tariff)
    def resolve(self, ts): return inc_rate(self.t, _naive(ts))
    def flat_rate(self): return inc_rate(self.t, _naive(FROM))
    def is_empty(self): return False


def _naive(ts):
    if isinstance(ts, datetime):
        d = ts
    else:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if d.tzinfo is not None:
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return d


def _devices(devices):
    """[(meter_id, meter_type, draw(loc, hm, cheap) -> kWh)] for a fixture variant.

    True:   an EV charger (cheap slots, every other day) and a battery (cheap slots).
    "many": five heat pumps and three batteries — the many-device case (6 Oct 2026): every
            surface must keep eight devices apart, and house + devices must still equal the
            main meter."""
    if devices == "many":
        hps = [("heat_pump_%d" % i, "heat_pump",
                (lambda k: lambda loc, hm, cheap: k)(round(0.1 + 0.05 * i, 2)))
               for i in range(1, 6)]
        bats = [("battery_%d" % i, "battery",
                 (lambda k: lambda loc, hm, cheap: k if cheap else 0.0)(round(0.4 + 0.1 * i, 2)))
                for i in range(1, 4)]
        return hps + bats
    if devices:
        return [("ev_charger", "ev_charger",
                 lambda loc, hm, cheap: 3.2 if (cheap and loc.day % 2 == 0) else 0.0),
                ("house_battery", "battery", lambda loc, hm, cheap: 1.1 if cheap else 0.0)]
    return []


def _config(devices):
    meters = {"electricity_main": {
        "meta": {"billing_day": 3, "block_minutes": 30, "timezone": "Europe/London",
                 "currency_symbol": "£", "currency_code": "GBP"},
        "channels": {"import": {}, "export": {}}}}
    for mid, mtype, _ in _devices(devices):
        meters[mid] = {"meta": {"sub_meter": True, "meter_type": mtype,
                                "parent_meter": "electricity_main"},
                       "channels": {"import": {}}}
    return {"meters": meters}


def build(path, tariff, devices=False):
    """Write the fixture to `path`, run the sweep over it, and return `path`."""
    st = BlockStore(path)
    st.insert_config_period(_config(devices))
    cp = st.get_current_config_period_id()
    t = FROM.astimezone(timezone.utc).replace(tzinfo=None)
    end = TO.astimezone(timezone.utc).replace(tzinfo=None)
    rows = []
    while t < end:
        loc = t.replace(tzinfo=timezone.utc).astimezone(LONDON)
        hm = loc.hour * 60 + loc.minute
        rate = inc_rate(tariff, t)
        cheap = rate < 0.13
        house = 0.18 + 0.25 * (1 if 1020 <= hm < 1320 else 0) + 0.02 * (t.minute // 30)
        draws = [(mid, draw(loc, hm, cheap)) for mid, _, draw in _devices(devices)]
        dev_total = sum(k for _, k in draws)
        main = round(house + dev_total, 4)
        bs, be = t.isoformat(), (t + timedelta(minutes=30)).isoformat()
        # What _apply_pass2 writes on a finalised block: each device's grid share
        # (imp_kwh_grid), and the main's remainder after them — house = main − devices.
        # A NULL remainder makes Usage Stats fall back to the whole main reading, which
        # counts every device twice (found 6 Oct 2026; the first goldens had it).
        rem = round(max(main - dev_total, 0.0), 4)
        rows.append((bs, be, "electricity_main", main, None, rem, round(rem * rate, 6),
                     round(main * rate, 6), rate, 0.5 * (1 + vat_on(loc.date())),
                     round(main * 110, 2)))
        for mid, k in draws:
            rows.append((bs, be, mid, k, k, None, None, round(k * rate, 6), rate, 0.0, None))
        t += timedelta(minutes=30)
    with st._conn:
        st._conn.executemany(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "imp_kwh_grid, imp_kwh_remainder, imp_cost_remainder, imp_cost, imp_rate, "
            "standing_charge, carbon_g, exp_kwh, exp_cost, source) "
            "VALUES (?, ?, ?, %d, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 'imported_api')" % cp, rows)
    _sweep(st, tariff)
    st.close()
    return path


def _sweep(st, tariff):
    import engine
    import job_coordinator
    sched = Schedule(tariff)

    async def _segs(ch):
        return [("2000-01-01T00:00:00", None, sched)]
    job_coordinator.reset_for_tests()
    with mock.patch.object(engine, "_store", st), \
         mock.patch.object(engine, "_kraken_rate_schedules", {"import": sched}), \
         mock.patch.object(engine, "_build_channel_rate_segs", _segs), \
         mock.patch.object(engine, "_hist_rate_segs_cache", {}), \
         mock.patch.object(engine, "_write_reprice_report", lambda **k: None), \
         mock.patch.object(engine, "_iog_site_tz", lambda store=None: "Europe/London"):
        for _ in range(50):
            asyncio.run(engine._run_historical_reprice_sweep(batch=500, pace_s=0))
            if (st.get_meta(engine._REPRICE_HISTORY_MARKER, {}) or {}).get("done"):
                break
    job_coordinator.reset_for_tests()
