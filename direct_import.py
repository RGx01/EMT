"""
direct_import.py — the house's share of the main meter ("Direct import"), one rule for every surface
(6 Oct 2026).

The main meter is the grid boundary, and it is the bill. Devices (sub-meters) attribute that
supply; they never add to it. What the devices do not account for is Direct import — the house:

    per half-hour   Direct kWh = max(0, main kWh − Σ device grid kWh)
                    Direct £   = max(0, main £ − Σ device £), except that a negative main £ (an
                                 Agile plunge credit) passes through unfloored

THE CLIP IS NOT HERE. A device's grid share (`imp_kwh_grid`, and the £ priced from it) is clipped
to what the main supplied by the house/device split, `engine._apply_pass2`, when the block is
written. So the devices' grid shares never exceed the main and the floor above never binds — except
on an interpolated (gap-fill) block whose main has not settled, which the split records as-is
(the main is itself an estimate there) until settlement re-runs it. On such a block, for a day or
two, a surface's breakdown reads above the meter; that is accepted (6 Oct 2026) rather than a
second rule here. The device's OWN reading from Home Assistant (`imp_kwh`, and its register reads)
is kept as recorded whatever the split clipped: only the grid share is cut.

Why one module. Before it, three surfaces worked Direct out three ways — the Bill card per
half-hour, the Overview cards per window, Usage Stats per half-hour in kWh but per day in £ — and on
a fixture whose devices out-read the main they gave three answers for one period. v4 had been
caught by the same family: its period total, built as `max(0, main − Σsubs) + Σsubs`, once followed
the devices to a bill 90% over. Every surface that shows a Direct import uses `direct`;
`tests/test_direct_import.py` asserts they agree on every fixture.

Real data, 6 Oct 2026: on the five local databases with devices (about 112,000 half-hours), the
devices never exceed the main in a half-hour, in kWh or £, and the stored `imp_kwh_remainder`
equals this rule on every block.
"""


def direct(main_kwh, main_cost, dev_kwh, dev_cost):
    """(kWh, £) of one half-hour's Direct import, from the main meter's figures and the sum of
    its devices' (grid kWh — `COALESCE(imp_kwh_grid, imp_kwh)` — and £)."""
    c = main_cost - dev_cost
    return max(0.0, main_kwh - dev_kwh), (c if main_cost < 0 else max(0.0, c))


# The same rule for a query that sums Direct in SQL. Each placeholder is a SQL expression for one
# half-hour. `tests/test_direct_import.py` asserts these agree with `direct` on a grid of inputs.
def sql_kwh(main_kwh, dev_kwh):
    return "MAX(0.0, (%s) - (%s))" % (main_kwh, dev_kwh)


def sql_cost(main_cost, dev_cost):
    return ("(CASE WHEN (%(m)s) < 0 THEN (%(m)s) - (%(d)s) ELSE MAX(0.0, (%(m)s) - (%(d)s)) END)"
            % {"m": main_cost, "d": dev_cost})
