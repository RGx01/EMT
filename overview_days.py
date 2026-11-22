"""Today against yesterday on the Overview's cards (overview rows design, part 2, 22 Nov 2026).

Pure: each card's cumulative energy since local midnight, today and yesterday, as points
[minute, value] — the minute is real time elapsed since that day's local midnight, so a clock-change
day is 23 or 25 hours long. No store, no clock: the route reads the rows and passes `now`.

- Live Power: the main meter's half-hours (`half_hour_lines`), the meter's own figures, plus the
  half-hour in progress from its register reads (`with_partial`).
- A device: its power sensor's samples (`sample_lines`), trapezoid-summed. A gap longer than
  `gap_s` (EMT was down) adds nothing. Positive power is in (a battery charging, a device using),
  negative is out (a battery or a V2X car discharging). A device without a power sensor: its
  half-hours, import only.
- A battery's state of charge (`soc_lines`): the last reading in each step.
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


def _day_end(day, t0, t1):
    return t0 if day == "yesterday" else t1


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


def sample_lines(samples, y0, t0, t1, now, gap_s=GAP_S, step_min=STEP_MIN, out_too=True):
    """{"yesterday": {"in": points[, "out": points]}, "today": {...}} from power samples
    ({captured_at, inverter_kw}). Each segment between two samples no more than `gap_s` apart adds
    its trapezoid — positive power to "in", negative to "out" — to the day its start falls in. One
    point per `step_min` that saw energy change; a line holds level through a stretch with nothing,
    and runs level to its day's last sample."""
    names = ("in", "out") if out_too else ("in",)
    out = {d: {n: [[0, 0.0]] for n in names} for d in ("yesterday", "today")}
    tot = {d: {n: 0.0 for n in names} for d in ("yesterday", "today")}
    last_seen = {}                  # each day's last sample, where its lines run level to
    prev = None
    for s in samples:
        kw = s.get("inverter_kw")
        if kw is None:
            continue
        t = parse_utc(s["captured_at"])
        if t > now:
            break
        seen_day, seen0 = _which_day(t, y0, t0, t1)
        if seen_day:
            last_seen[seen_day] = minutes(t, seen0)
        if prev is not None:
            pt, pk = prev
            dt = (t - pt).total_seconds()
            day, day0 = _which_day(pt, y0, t0, t1)
            if day and 0 < dt <= gap_s:
                h = dt / 3600.0
                adds = {"in": (max(pk, 0.0) + max(kw, 0.0)) / 2.0 * h,
                        "out": (max(-pk, 0.0) + max(-kw, 0.0)) / 2.0 * h}
                # a segment across midnight is the day it starts in, and ends at its midnight
                m = minutes(min(t, _day_end(day, t0, t1)), day0)
                start_m = minutes(pt, day0)
                for n in names:
                    if adds[n] <= 0:
                        continue
                    pts = out[day][n]
                    # after a stretch with nothing, the line holds level to where this energy starts —
                    # drawn straight from the last point, it would climb through the idle hours
                    held = start_m - pts[-1][0] >= step_min
                    if held:
                        pts.append([start_m, pts[-1][1]])
                    tot[day][n] += adds[n]
                    v = round(tot[day][n], 4)
                    # one point per step: a later sample in the same step moves the step's point on (never
                    # the level point just held)
                    if not held and len(pts) > 1 and int(pts[-1][0] // step_min) == int(m // step_min):
                        pts[-1] = [m, v]
                    else:
                        pts.append([m, v])
        prev = (t, float(kw))
    for day, m in last_seen.items():
        for n in names:
            pts = out[day][n]
            if m > pts[-1][0]:
                pts.append([m, pts[-1][1]])
    return out


def soc_lines(samples, y0, t0, t1, now, step_min=STEP_MIN, key="soc_pct"):
    """{"yesterday": points, "today": points}: a level — the state of charge, or (`key="temp_c"`) a heat
    pump's outdoor temperature — the last reading in each step."""
    out = {"yesterday": [], "today": []}
    for s in samples:
        pct = s.get(key)
        if pct is None:
            continue
        t = parse_utc(s["captured_at"])
        if t > now:
            break
        day, day0 = _which_day(t, y0, t0, t1)
        if not day:
            continue
        m = minutes(t, day0)
        pts = out[day]
        p = [m, round(float(pct), 1)]
        if pts and int(pts[-1][0] // step_min) == int(m // step_min):
            pts[-1] = p
        else:
            pts.append(p)
    return out
