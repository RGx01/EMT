# EMT v5 — the EV on an account with no EV sensor (BL-97)

*Status: decided and built, 1 Nov 2026 (the user: the tab "EV"; a new EV device inherits the colour). Code facts read at `dev` after #372.*

---

## 1. What the user sees today

On an account with no EV sensor in Home Assistant — API-only, or a sensor for the house but none for
the car — the car still appears, as **"EV (from dispatch)"**: a line in the bill's breakdown, a bar
in the day charts and a device in Usage Stats. Two complaints (the user, 1 Nov 2026):

- **The name is from before Octopus's bill carried the split.** It says the figures come from the
  dispatches. They mostly do not (§2).
- **Its colour cannot be changed.** A colour lives on a device's channel in the config
  (`channels.import.bar_color`, `rate_line`, chosen on the device's Settings tab). This EV is no
  device, so it has none: it is `#8b5cf6` in code, in `energy_charts.py` and `usage_view.py`.

## 2. Where its figures come from (already right)

`energy_charts._dispatch_ev_slot_map`, per half-hour:

- **Where Octopus's bill has a device breakdown, the bill decides.** Its EV segment is the EV; a
  breakdown with no EV means no EV, whatever a dispatch says.
- **Where no breakdown has been fetched,** a completed dispatch's energy is the EV, clipped to the
  half-hour's import and costed at its rate.

So the split is already Octopus's wherever Octopus has given one. **No change to the figures.**

## 3. Proposed

### 3.1 The name: "EV"

The same name an account with an EV sensor gets (its one hybrid `EV` line). Every surface that
says "EV (from dispatch)": `energy_charts` (the day charts' meter name, the bill's breakdown),
`usage_view`, `bill_card`, `/api/energy`'s `sub_meters`. The internal id `ev_dispatch` stays.

### 3.2 A tab on Settings for it, with only its chart style

**`🚗 EV`** among the device tabs (the user), shown only when both hold:

- **no EV device is configured** (`_ev_meter_id(cfg)` none). With one, its own tab and colour
  rule, as now;
- **the account has ever had an EV:** a stored EV segment (`block_segments.attribution = 'ev'`) or a
  completed dispatch (`dispatch_history`, kept for good — `prune_dispatch_history` has no caller;
  the user corrected a first draft that said 90 days). Either alone: an imported history has EV
  shares and no dispatches, and a dispatch can come before its bill's split. On the staging copy the
  check answers in 3 ms (4.5 ms with nothing to find).

The tab says what this EV is — *"Octopus's split of your bill, or a completed dispatch where the
bill has not said yet. No sensor."* — and holds the chart style card every device has (bar colour,
rate line). Nothing else: no sensors to choose, nothing to delete.

### 3.3 Where its style is kept

In the **store's meta**: `ev_style = {bar_color, rate_line}`, read by `energy_charts.ev_dispatch_style`
for the day charts, the bill's breakdown and Usage Stats. Unset, it stays `#8b5cf6` and a solid rate
line, so nothing moves for anyone who does not choose. Saved from its tab when chosen
(`POST /api/ev-style`), not with the config: a config save restarts the engine, and the tab has
nothing else to save.

*Why not the main meter's meta:* the config is stored as columns (`block_store._write_meters`), which
drop a key they do not name; a column of its own is a schema change. *Why not a meter of its own:*
every reader of `meters` takes a meter for something with HA sensors — PASS 2, the attribution job,
the wizard, gap fill, the importer's key allow-lists. A meter with no sensors would need a guard in
each. The importer carries only named keys from a v4 database, which never has this one.

### 3.4 When an EV sensor is added later

The account then has an EV device, so the tab goes (§3.2) and the device's line takes over, as the
hybrid `EV` does today. The new device's **colour starts from the EV's** (`ev_style`, or the purple unset), so the car
keeps its colour across the change; it is then the device's, chosen on its own tab.

## 4. Not changed

- The figures (§2), the gate on the split, and the hybrid EV for an account with an EV sensor.
- No new storage beyond the one config key, no writers, no Octopus calls.

## 5. Decided (the user, 1 Nov 2026)

1. **The tab's name: "EV".**
2. **A new EV device inherits the EV's colour** (§3.4), from the Add device form or the setup wizard.

## 6. Order

One PR: the name (§3.1), `ev_style` read by the charts (§3.3), the Settings tab and its flag from the
server (§3.2), the carried colour (§3.4). Tests: the tab only with an EV segment and no EV device;
the chosen colour on the day chart and in Usage Stats; the default unchanged; the new EV device
starting from it.
