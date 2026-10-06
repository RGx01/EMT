"""
direct_import.py — the house's share of the main meter ("Direct import"), one rule for every surface
(6 Oct 2026).

The main meter is the bill. Devices (sub-meters) attribute that supply; they never add to it. What
the devices do not account for is Direct import — the house:

    per half-hour   Direct kWh = max(0, main kWh − Σ device grid kWh)
                    Direct £   = max(0, main £ − Σ device £), except that a negative main £ (an
                                 Agile plunge credit) passes through unfloored

    over any span   Direct + Σ devices − beyond = main, where `beyond` is what the devices record
                    beyond the metered supply. Every term is a plain sum of half-hours, so
                    `beyond` is too: `beyond(...)` gives it from the span's totals.

The floor is per HALF-HOUR because that is where it is physical: a battery charging off solar at
noon does not offset the house's evening import.

Why one module. Before it, three surfaces floored at three different places and, on one fixture
where a battery recorded 84 kWh the main meter never saw (`tests/tariff_fixtures.py`, "over"), gave
three answers for the same period: the Bill card floored each half-hour (its lines then overshot
its own total), the Overview card floored the whole window (its Direct read 9 kWh for £13.57), and
Usage Stats floored kWh per half-hour and £ per day (£17.43, a third figure). v4 had already been
caught once by the same family: its period total, built as `max(0, main − Σsubs) + Σsubs`, once
followed the devices to a bill 90% over. Every surface that shows a Direct import uses this module;
`tests/test_direct_import.py` asserts they agree on every fixture.

Real data, 6 Oct 2026: on the five local databases with devices (about 112,000 half-hours), devices
never exceed the main in a half-hour, in kWh or £ — `engine._apply_pass2` clips them — and the
stored `imp_kwh_remainder` equals `max(0, main − Σ device grid kWh)` on every block. So the rule
moves no real figure; the floor and `beyond` are a backstop for data the write path should not
produce, and the reason for one module is that the surfaces cannot drift apart again.
"""

# Below this a `beyond` is rounding, not energy: not shown.
BEYOND_KWH = 0.0005
BEYOND_COST = 0.005


def direct(main_kwh, main_cost, dev_kwh, dev_cost):
    """(kWh, £) of one half-hour's Direct import, from the main meter's figures and the sum of
    its devices' (grid kWh — `COALESCE(imp_kwh_grid, imp_kwh)` — and £)."""
    c = main_cost - dev_cost
    return max(0.0, main_kwh - dev_kwh), (c if main_cost < 0 else max(0.0, c))


def beyond(main_kwh, main_cost, dev_kwh, dev_cost, direct_kwh, direct_cost):
    """(kWh, £) the devices record beyond the metered supply over a span — so that
    Direct + devices − beyond = main. Zero (to rounding) unless a floor in `direct` bound."""
    return direct_kwh + dev_kwh - main_kwh, direct_cost + dev_cost - main_cost


def shows_beyond(kwh, cost):
    """Whether a span's `beyond` is worth a line."""
    return kwh > BEYOND_KWH or abs(cost) > BEYOND_COST


# The same rule for a query that sums Direct in SQL. Each placeholder is a SQL expression for one
# half-hour. `tests/test_direct_import.py` asserts these agree with `direct` on a grid of inputs.
def sql_kwh(main_kwh, dev_kwh):
    return "MAX(0.0, (%s) - (%s))" % (main_kwh, dev_kwh)


def sql_cost(main_cost, dev_cost):
    return ("(CASE WHEN (%(m)s) < 0 THEN (%(m)s) - (%(d)s) ELSE MAX(0.0, (%(m)s) - (%(d)s)) END)"
            % {"m": main_cost, "d": dev_cost})


# Beyond's line, worded once.
BEYOND_LABEL = "Devices beyond the metered supply"
