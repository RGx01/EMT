# Replacing a device's history after a wrong sensor (design)

> **History — a design record.** What was decided, when and why, as it stood then; parts describe code
> that has since changed. How it works today: [reference/data-management-and-devices.md](../../reference/data-management-and-devices.md)
> (Devices), the help (Settings → devices), and the code. PR numbers (`#NNN`) are in this repository,
> `RGx01/EMT`.

*Status: built, 4 Nov 2026, in the PR that carries this record. It adds to the device sensors design
([EMT-v5_device_sensors_design.md](EMT-v5_device_sensors_design.md)), whose §3 asked a sensor change
one question, and to the soft delete design's `ha_reads` ([EMT-v5_soft_delete_design.md](EMT-v5_soft_delete_design.md) §2).*

*Decided, 4 Nov 2026, with the user ("I changed the sensor but now unsure if the history has been
rebuilt and old sensor data is deleted. we possibly need to improve things around this aspect"):* a
sensor change asks why; a wrong sensor's history can be replaced, whole; the card says where a device's
history came from.

## 1. What happened

A scratch setup's battery was set up on `battery_in_aggregated_energy`, which counts its solar charging
too (a battery must report grid import). The import rebuilt its history from it. The user changed the
sensor and answered *No, drop it* to "Keep the old sensor?". The support file, after:

| the battery | sensor | rows | kWh |
|---|---|---|---|
| rebuilt half-hours, 4 Nov 2024 → 2 Nov 2026 | the old | 34,924 | 6,941 |
| recorder hours kept in `ha_reads` | the old | 20,603 | 7,710 |
| live half-hours | the old, then the new | 1 + 1 | 0.0 |

*No* kept the old sensor off the stitch, so no rebuild would use it again; nothing else changed. And
neither action on the card could undo it:

- *Rebuild history* fills only half-hours the device has no figure for: every one had.
- *Remove*, then *Rebuild history*: the rebuild takes the kept hours and the fetched ones, the fetch
  winning where both (`{**stored, **fetched}`). Every hour the new sensor has no statistics for is the
  old sensor's again, and its live half-hours are put back as they were.

And the question assumed a rename, with yes the default: answered on a wrong sensor, it would have made
the wrong sensor a history sensor.

## 2. Replace history

One action, on the card (**↻ Replace history**) and offered after a wrong-sensor save (§3):

1. **The claim and the sensor check**, as *Rebuild history* (`/api/historical/attribute/replace`,
   sharing `/start`'s path): a sensor reading more than the house drew, or far less, is refused with
   *Replace anyway*. Unlike a rebuild, a replace is also refused when there is nothing to rebuild from —
   Home Assistant not connected, or no statistics for the sensors — and the engine checks the
   connection again before it drops anything: a replace that cannot rebuild would only delete. (Found
   in the local preview, which has no Home Assistant: the drop ran, then the rebuild failed.)
2. **A backup**, `pre-replace-history`. Unlike *Rebuild history*, a replace that cannot back up does
   not start: it deletes.
3. **The drop** (`drop_device_history`, one transaction): all the device's rebuilt half-hours
   (`recorder_attributed` — the rebuild makes them again); every `ha_reads` row and live half-hour
   recorded by a sensor it **no longer uses** (`device_off_stitch_sensors`: a secondary off the stitch,
   kept only because a row names it); then those sensors' entries, nothing naming them now. Imported
   device rows and the sensors in use are not touched.
4. **The house's remainder** over the span, again (`recompute_remainders_for_window`), as *Remove* does;
   the device's runs leave the ledger — what they wrote is gone.
5. **The rebuild**, whole, from HA's recorder (`run_attribution_job`), from the sensors it uses now.

"No longer uses" is the line because the device's record already draws it: a rename kept for history is
on the stitch and stays; a new device's or a wrong sensor's old one is off it. A replace on a device
whose old sensor was a different device, not a wrong one, drops that history too — the confirm names
the sensors whose readings go.

Checked on a copy of the user's database, the store's drop, then the whole replace from the page in the
local preview (the wrong-sensor answer, the offer after the reload, *Rebuild now*): the drop took 34,925 half-hours (the 34,924 rebuilt, the old
sensor's live one) and 20,604 reads (its 20,603 hours, its live one), and the old sensor's entry; the
Zappi's 34,944 half-hours were untouched.

## 3. A sensor change asks why

The question is now **"Why did *device*'s sensor change?"**, with three answers:

- **Renamed — keep for history** (the default, as before): the old name a history sensor.
- **A new device**: what the old sensor recorded stays; it is kept off the stitch. Was *No, drop it*.
- **It was the wrong sensor**: as *A new device*, and the device is put on the rebuild offer that
  follows the save's reload, as a **replace** — "replace its history from its new sensor: what *old*
  recorded, and its rebuilt half-hours, are dropped" — ticked. The user still says *Rebuild now*.

## 4. Where the history came from

The card's history section lists, under the rebuilt span, each sensor the kept history came from
(`device_history_sources`): its hours, live half-hours and span, and **no longer used** against one off
the stitch, with a warning that *Replace history* rebuilds from the sensors in use. It answers the
user's question — "has it been rebuilt, is the old data gone" — at a glance.

## 5. Not built

- A replace for one span, or by sensor while keeping the rest: the whole device, as a rebuild is.
- Removing a sensor's history without a rebuild: *Remove* stays for the rebuilt layer.
- The wizard's own sensor change: Change Setup does not ask the question; the card's *Replace history*
  is there for it.

## 6. Checked

`tests/test_replace_device_history.py`: the old sensor off the stitch; the card's sources; the drop's
rows, and the sensors in use keeping theirs (GUARD); a replace leaving the new sensor's history alone,
the hour only the wrong sensor had and its live half-hour gone; nothing dropped without Home Assistant;
the ledger; a GUARD pinning what a
rebuild alone did; the route backing up then replacing, and not starting without a backup or with
nothing to rebuild from; the page's
three answers, the offer, the card.
