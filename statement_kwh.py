"""The kWh an Agile statement prices: each half-hour to 0.01 kWh, half to even (26 Nov 2026).

Octopus's Agile statement prices every half-hour at its rate × the kWh it prints, and it prints the
meter's reading rounded to 0.01 kWh, half to even (0.005 → 0.00, 0.015 → 0.02, 1.945 → 1.94). The
meter reads to 0.001 kWh, and so does the API EMT settles from. On a house that runs from a battery,
hundreds of half-hours a month read a few watt-hours, and the statement prices most of them at
nothing: a beta tester's Agile statement sat below the readings, and this rule reproduced every
one of its month's half-hours.

An Intelligent statement prints its kWh rounded too (half up), but charges Octopus's own cost for
the half-hour, which EMT stores as it is: nothing here applies to it.

Display only — the bill card's statement method. EMT stores and charts what the meter measured.
A pure rule, importing nothing of the project's: `billing` may use it.
"""
from decimal import Decimal, ROUND_HALF_EVEN

_HUNDREDTH = Decimal("0.01")


def statement_kwh(kwh) -> float:
    """A half-hour's kWh as an Agile statement prints and prices it."""
    # round(…, 6) first: a stored 0.005 can arrive as 0.0050000000001 from a sum
    return float(Decimal(repr(round(float(kwh or 0.0), 6))).quantize(_HUNDREDTH, rounding=ROUND_HALF_EVEN))


def covers(windows):
    """A predicate on a half-hour's naive-UTC start: inside one of `windows` [(valid_from,
    valid_to | None)], naive UTC as stored. None for no windows, so a caller passes nothing on an
    account never on Agile."""
    if not windows:
        return None

    def inside(start) -> bool:
        s = str(start or "")[:19]
        return any(vf <= s and (vt is None or s < vt) for vf, vt in windows)
    return inside
