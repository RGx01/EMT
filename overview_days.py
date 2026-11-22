"""Today against yesterday on the Overview's cards (overview rows design, part 2, 22 Nov 2026).

Pure: each card's cumulative energy since local midnight, today and yesterday, as points
[minute, value] — the minute is real time elapsed since that day's local midnight, so a clock-change
day is 23 or 25 hours long. No store, no clock: the route reads the rows and passes `now`.

- Live Power: the main meter's half-hours (`half_hour_lines`), the meter's own figures, plus the
  half-hour in progress from its register reads (`with_partial`).
- A device: its power sensor's samples, trapezoid-summed into 5-minute steps in SQLite
  (`BlockStore.get_sample_energy_steps`) and drawn here (`energy_lines`). A gap longer than
  `GAP_S` (EMT was down) adds nothing. Positive power is in (a battery charging, a device using),
  negative is out (a battery or a V2X car discharging). A device without a power sensor: its
  half-hours, import only.
- A battery's state of charge, a heat pump's outdoor temperature: the last reading in each step
  (`BlockStore.get_sample_level_steps`).
"""
from datetime import datetime, timedelta, timezone

STEP_MIN = 5          # a device's line is one point per 5 minutes: 288 a day
GAP_S = 300           # samples further apart than this are a gap, not a slope


def to_celsius(value, unit=""):
    """A temperature sensor's state in °C: °F and K converted, anything else taken as °C; None when the state
    is not a number (a heat pump's outdoor temperature, 22 Nov 2026)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:                                     # NaN
        return None
    u = (unit or "").strip().upper().replace("°", "").replace("DEG", "")
    if u == "F":
        v = (v - 32.0) * 5.0 / 9.0
    elif u == "K":
        v = v - 273.15
    return round(v, 1)


def day_bounds(now_local):
    """(yesterday's, today's and tomorrow's local midnight), aware, for an aware `now_local`."""
    tz = now_local.tzinfo
    d = now_local.date()

    def midnight(day):
        return datetime(day.year, day.month, day.day, tzinfo=tz)
    return midnight(d - timedelta(days=1)), midnight(d), midnight(d + timedelta(days=1))


def parse_utc(s):
    """A stored naive-UTC ISO timestamp as an aware datetime."""
    t = datetime.fromisoformat(s)
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


def minutes(t, day0):
    """Minutes from `day0` to `t`, to the hundredth — real time: two times in the same zone subtract by
    the wall clock in Python, which made the day the clocks go back 24 hours, not 25."""
    return round((t.astimezone(timezone.utc) - day0.astimezone(timezone.utc)).total_seconds() / 60.0, 2)


def _which_day(t, y0, t0, t1):
    if y0 <= t < t0:
        return "yesterday", y0
    if t0 <= t < t1:
        return "today", t0
    return None, None


def half_hour_lines(rows, y0, t0, t1, block_minutes=30, names=("imp", "exp"), cols=("imp_kwh", "exp_kwh")):
    """{"yesterday": {name: points}, "today": {...}} from half-hour rows ({block_start, imp_kwh, exp_kwh}):
    each day starts at [0, 0] and steps up at each half-hour's END."""
    out = {d: {n: [[0, 0.0]] for n in names} for d in ("yesterday", "today")}
    tot = {d: {n: 0.0 for n in names} for d in ("yesterday", "today")}
    for r in sorted(rows, key=lambda r: r["block_start"]):
        start = parse_utc(r["block_start"])
        day, day0 = _which_day(start, y0, t0, t1)
        if not day:
            continue
        end_min = minutes(start + timedelta(minutes=block_minutes), day0)
        for n, c in zip(names, cols):
            tot[day][n] += float(r.get(c) or 0.0)
            out[day][n].append([end_min, round(tot[day][n], 4)])
    return out


def with_partial(today, at_min, adds, starts_min=None):
    """Today's lines with the half-hour in progress added at `at_min`: `adds` {name: kWh so far}. Only
    when it starts where the finished half-hours end (`starts_min`, its start in minutes): a
    half-hour in progress left from before a restart is not today's."""
    for n, pts in today.items():
        k = adds.get(n)
        if k is None or not pts:
            continue
        last = pts[-1]
        if starts_min is not None and abs(starts_min - last[0]) > 0.5:
            continue
        if at_min > last[0]:
            pts.append([round(at_min, 2), round(last[1] + max(0.0, float(k)), 4)])
    return today


def partial_kwh(reads):
    """kWh so far in the half-hour in progress, from its register reads [{ts, value}]; None without two."""
    vals = []
    for r in reads or []:
        try:
            vals.append(float(r["value"]))
        except (KeyError, TypeError, ValueError):
            continue
    if len(vals) < 2:
        return None
    d = vals[-1] - vals[0]
    return d if d >= 0 else None          # a register reset is not negative energy


def energy_lines(day, step_min=STEP_MIN, out_too=True):
    """{"in": points[, "out": points]} for one day, from its steps (`BlockStore.get_sample_energy_steps`:
    each step's kWh in and out, the first such segment's start minute and the last one's end). A line
    starts at [0, 0], holds level through a stretch with nothing — drawn straight from the last point it
    climbed through idle hours — to where the next energy starts, takes one point a step, and runs level
    to the day's last sample (`last_m`). The same lines the per-sample sum drew (24 Nov 2026: moved into
    SQLite, 22 Nov 2026: it ran in Python once a minute over every sample)."""
    names = ("in", "out") if out_too else ("in",)
    out = {n: [[0, 0.0]] for n in names}
    tot = {n: 0.0 for n in names}
    for st in (day or {}).get("steps") or []:
        for n in names:
            kwh = st.get(n + "_kwh")
            if not kwh or kwh <= 0:
                continue
            pts = out[n]
            start_m, end_m = st[n + "_start"], st[n + "_end"]
            if start_m - pts[-1][0] >= step_min:
                pts.append([start_m, pts[-1][1]])
            tot[n] += kwh
            pts.append([end_m, round(tot[n], 4)])
    last_m = (day or {}).get("last_m")
    if last_m is not None:
        for n in names:
            pts = out[n]
            if last_m > pts[-1][0]:
                pts.append([last_m, pts[-1][1]])
    return out
