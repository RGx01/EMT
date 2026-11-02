# EMT v5 — the device cost check

> **History — a design record.** What was decided, when and why, as it stood then; parts describe code that has since changed. How EMT works today: [reference/data-management-and-devices.md](../../reference/data-management-and-devices.md). PR numbers (`#NNN`) are in the private archive, `RGx01/EMT-archive`.

*Status: decided and built, 31 Oct 2026 (ROADMAP BL-91). What it is: a settled half-hour whose
devices cost more than the meter, flagged red on the data heatmap, with Re-fetch as the fix, and
reported by the v4 importer.*

---

## 1. What was found

On one install's v4 history, three half-hours on the afternoon of 14 Sep 2026 had been corrected by
hand to the off-peak rate. Octopus's bill agreed with the correction. The devices were not carried
along:

- Their **inc-VAT** cost stayed at the **peak** rate × their **raw** reading, not their grid
  share: a battery that took part of its charge from solar was charged for all of it.
- Their **ex-VAT** cost was right: grid share × the corrected off-peak rate. v4's 4.5.21 heal
  (`_corrected_device_exc_core`) rewrote only that side, assuming the inc-VAT rate had followed the
  correction. Here it hadn't.

v4's 4.5.24 database held the rows exactly so, and v5's importer carried them as they were. The
meter's figures, and so the bill total, are right. The device breakdown read pounds too high over the
day, and the house's share fell to zero in those half-hours. Usage Stats' Import column (#346) showed the
mismatch.

Nothing repairs it on its own. Every automatic pass skips a corrected half-hour, and v5 refuses new
corrections on a supplier-API account.

On that install these are the only device rows off their meter's rate. The other install checked
has dozens of corrected half-hours and none of this.

**Two faults compound in the battery's rows.** The device whose raw reading is far above its grid
share is a battery whose sensor counts solar charging, and EMT's battery check flags it (battery
sensor and device split design §3). Pricing on the raw reading is v4's #473 fault, fixed in 4.5.15
with a one-off heal. It charges such a sensor for its solar too, so it overcharged this battery most.
The rows date from v4 4.5.13 or earlier. The heal would have repaired them as they now stand, so a
later write re-priced them after it ran. v4's correction route has carried the rate to devices since
3.1.1, so the correction did not leave them on peak. Without the install's v4 history from before the
import, that later write cannot be named.

- **What a re-fetch fixes:** the pricing, each device on its grid share at the half-hour's rate.
- **What it can't fix:** the share itself. A solar-counting sensor still claims grid it did not take
  in the device split. The battery sensor warning is the remedy for that, and it stays the user's
  choice whether to replace the sensor.

## 2. The rule

`settlement_state.devices_exceed(f)`: a **settled** half-hour where

    |Σ devices' imp_cost| > |the meter's imp_cost| + £0.00001

- **Why it is a fault.** Each device is priced on its grid share of the meter's kWh, at the
  meter's rates, and the shares sum to the meter's kWh. So the devices can only exceed the meter by
  rounding.
- **The tolerance** is the module's rounding bound, a thousandth of a penny (`_EPS_GBP`). Each cost
  is stored to 6 dp. A sound half-hour on the real databases checked is over by at most 2e-6: the
  6 dp of the meter's own cost and its devices'. An absolute £ tolerance such as half a penny would
  pass small real faults.
- **It isn't VAT.** On every half-hour this rule catches, inc ÷ ex-VAT is exactly the VAT rate.
  - 14 Sep's three are a rate fault.
  - Three on 29 Sep, a fraction of a penny between them, are kWh: the battery is priced on 1–2 Wh
    more than its stored grid share.
  - All six are real, and a re-fetch fixes all six.
- **Settled only:** the kWh is DCC's or an import's. An unsettled live half-hour can be over until
  settlement re-clips it.
- **Magnitudes,** so a negative (plunge) price reads the right way round.

`get_settlement_facts` carries `devices_cost`: one subquery, the sum over the half-hour's
sub-meters.

## 3. Where it shows, and the fix

- **The flag** joins `bill_kwh_differs`, `vat_disagrees` and `split_missing` on the half-hour's
  states.
- **The cell turns red** (*flagged*) whatever the cost's state, except `none` and `missing`:
  - *corrected* (blue) too, which is the case it was found in;
  - also *applied* and the estimates.
- **The detail panel** says what is wrong and how to fix it:
  > the devices cost more than the meter — the device breakdown reads too high and the house's too
  > low (the bill is not affected). Re-fetch it to price the devices again
- *Flagged* is rank 3, so the health strip, the day totals and Issues only count it as they count
  every issue.
- **Re-fetch** lists the half-hour even where Octopus agrees with the meter (`bill.devices`), and
  names it in the preview. Its apply settles the half-hour again. `settle()` prices the devices on
  their grid shares at the half-hour's rates, writing the meter and devices in one transaction. A
  corrected one takes Octopus's bill in place of the correction, as a re-fetch already does.
- **Nothing is changed** until the user applies a re-fetch, in line with "flag, don't act".

## 4. The importer

`v4import.transforms.device_costs` comes last, and **reports only**. It lists the half-hours the
import carries with devices over the meter, by the same rule, under
`report["transforms"]["device_costs"]["reported"]`, and writes nothing. The restore logs a warning
naming the count and the first one. The heatmap flag is what the user sees.

On the Agile user's v4 4.5.24 database the step reports the six half-hours; on the other install's
it reports none.

## 5. Tests

`tests/test_device_cost_check.py`:

- the rule: over, rounding, unsettled, imported, negative price, no devices;
- the flag on a corrected half-hour's states;
- the cell's rule and the flag's text;
- the re-fetch, end to end: listed, applied, the device on the off-peak rate on its grid share, the
  flag gone; and a corrected one taking the bill;
- the importer reporting and carrying the row unchanged.
