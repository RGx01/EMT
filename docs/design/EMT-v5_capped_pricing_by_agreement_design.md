# EMT v5 — Capped pricing judged per half-hour by its agreement

*Draft, 16 Oct 2026. Left open by the consolidation note (bill and split paths design §5, PR 5 row:
"capped pricing judged per half-hour by its agreement … a pricing change, for its own note").
Every line below was read in the code or queried on `blocks-36` (the soak backup of 15 Oct);
nothing is taken from a docstring.*

**The question:** is a half-hour priced under the 6-hour cap (IOG-SMB's four buckets) because
**its own agreement** was capped, or because the account's agreement **today** is?

**Today's answer:** the second. The split and the bill views have been per-agreement since PR 5
(`split_at`, `tariff_kind_at`), so they and the pricing can disagree about the same half-hour.

**No stored data is wrong on the soak** (§3), so this is forward code only. No heal.

---

## 1. What decides "capped" today

| gate | reads | used by |
|---|---|---|
| `_capped_schedules()` (`engine.py` 628) | `ev_device_off_peak` and `ev_device_peak` are loaded, i.e. built for the **current** product | `_apply_iog_split` (727): the seam, which re-prices the block on capped. Reached from finalise and the settlement re-run (`_reprice_main_import_block`), the carve (`run_iog_split_carve`), and the history re-price sweep (`_reprice_history_block`) |
| | | `apply_measured_to_block` (12657): the mixed boundary block's four-band rebuild |
| | | `reconcile_dispatch_overlay` (13016): the BL-34 band relabel runs only when **not** capped |
| `_import_is_smb_capped()` (11470) | the **current** tariff or product code says IOG-SMB | the history drain and its backlog (`run_measured_history_drain`, `_measured_history_backlog`, `_maybe_drain_measured_history`) |
| | | `bill_stage`'s `every_half_hour` |
| | | `_chart_cap_from` |
| | | the heatmap's Split view and `classify_range(smb=…)` (`web/server.py` 3893, 3909) |
| | | `split_at`'s fallback when no agreement covers the half-hour |
| `_measured_floor()` (11451) | `_kraken_current_agreement_from`: the **current** agreement's `valid_from` | the live bill pass, the drain, the apply (`apply_measured_settled`) and `_chart_cap_from` |
| `cap_day_boundary` (`block_store.py`) | the cap-day's completed dispatches | the seam's boundary. No agreement bound: it counts a cap on any day |

The EV-device schedules come from `build_ev_device_schedules` for the current product and tariff
only, with no period bounds (`engine.py` 10291). The import schedule is stitched across every
agreement (BL-54), so a past half-hour gets its own agreement's house rates. The EV-device rates
are not stitched.

## 2. Where it goes wrong

### 2.1 Before the agreement, on an account now on IOG-SMB
A seam re-run over a half-hour from the earlier, uncapped Intelligent agreement prices it capped:
- `_capped_schedules()` is true;
- the current product's EV-device rates resolve at that time whenever the product's rate records start before the customer's agreement (the soak's product is `…IOG-SMB-FIX-12M-26-03-17`, with rates from 17 Mar 2026; its agreement began in late August);
- `cap_day_boundary` counts a 6-hour cap the old tariff never had.

On a cap-day over six hours, the EV beyond the cap goes to the **peak** EV rate, and the block's
rate and cost are re-priced (`if capped and not hold_rate`). Under the uncapped agreement, the
whole dispatch was off-peak.

Re-runs that reach pre-agreement history:
- **The history re-price sweep** (`_run_historical_reprice_sweep`): every block lacking segments, e.g. a gap fill or API import into July or August. It writes the seam's four-band segments (`_build_block_segments`).
- **A settlement re-run** of a late-settling block.
- **The carve:** EV at the block's own rate, so cost-neutral, but the EV beyond the "cap" is labelled `peak`.

`split_at` already says those half-hours have no Octopus split, so the surfaces treat them as EMT's split: final at the bill. The pricing treats them as capped.

### 2.2 The reverse: an account that has left IOG-SMB
`_capped_schedules()` goes false and `_import_is_smb_capped()` goes false:
- a seam re-run over capped history prices it uncapped;
- the drain never runs for it;
- the Split view disappears, though `split_at` still says those half-hours have Octopus's split.

### 2.3 A renewal onto a new IOG-SMB agreement
Every fixed product ends: the soak's is a twelve-month fix. `_measured_floor()` jumps to the
new agreement's start:
- the live pass, the drain and the apply stop reaching the earlier capped history;
- `_chart_cap_from` moves the rate lines' cap date;
- the EV-device rates are rebuilt for the new product only. An earlier capped half-hour would take the new product's EV rates, or fall through to uncapped pricing if they start after it.

The docstring of `_measured_floor()` names this case: "a future account with more than one SMB agreement would instead take the earliest SMB valid_from".

## 3. Measured on `blocks-36`

- **Stored capped pricing before the agreement: none.** Of the dispatched main-import half-hours before 15 Sep, no EV segment is banded `peak`. The pre-agreement ones were finalised live while the uncapped tariff was current, and nothing has re-run the seam over them since.
- **What a re-run would change:** six cap-days before September carry more than twelve completed dispatch slots, a slot-count approximation of over six hours: 7, 15, 22 and 30 Jul, and 2 and 24 Aug. All are before the agreement; the first cached bill with a split is 23 Aug 23:00 UTC, and 24 Aug's bill was off-peak (settlement surfaces design §9). A gap fill, delete-and-re-import, or late settlement over any of them would put the EV beyond the "cap" on the peak rate.
- **Aside, not this note's:** 18 EV segments across July (7–31 Jul) carry a peak rate under the `off_peak` band. That is a v4-era band label on a bill-snapped rate, so BL-34 territory.

## 4. Proposal: one per-half-hour test

```python
def capped_at(start) -> bool:
    """The agreement live at `start` is IOG-SMB (the 6-hour cap), from `_agreement_era`;
    where none covers it, the current tariff — as `split_at`."""
```

`split_at` and `capped_at` are the same question, whether Octopus's four buckets apply, so they
become one function. `split_at` stays as the surfaces' name for it.

| gate | becomes |
|---|---|
| `_apply_iog_split` | `capped = capped_at(block_start) and _capped_schedules()`. The second term stays: no EV-device rates means no capped pricing, as today. |
| `apply_measured_to_block` | `capped_at(bs)` |
| `reconcile_dispatch_overlay` relabel | `not capped_at(bs)`, so pre-agreement blocks on an SMB account get BL-34's relabel. A behaviour change, labelled in its test. |
| `cap_day_boundary` | unchanged. The seam asks for it only when capped. |
| `_measured_floor()` | the **earliest** IOG-SMB agreement's `valid_from` from discovery, else as today |
| `_chart_cap_from` | per day, `capped_at` of the day's first half-hour |
| drain, backlog, `every_half_hour`, the Split view, `classify_range(smb=…)` | "any IOG-SMB agreement in discovery", not "the current one" |
| EV-device schedules | stitched across the IOG-SMB agreements, as the import schedule is (BL-54), so each capped half-hour takes its own product's EV rates |

**Fallback:** where discovery has not run, every test falls back to the current tariff, as
`split_at` and `tariff_kind_at` do now. Nothing changes until discovery has run.

## 5. Questions for the user

**(a) An account that has left IOG-SMB.** Should the drain keep asking Octopus for the past
capped half-hours' breakdowns? The proposal says yes, because the history is still capped.

**(b) The relabel.** Should pre-agreement blocks on an SMB account take BL-34's band relabel?
The proposal says yes: it is the uncapped rule, and they were uncapped.

**(c) The stitched EV-device schedule.** Should it be in the first PR, or wait for a renewal? Without
it, §2.3's earlier half-hours price on the new product's EV rates. The proposal has it in the
second PR, well before any renewal.

## 6. Order

| PR | what | discriminating tests (fail on `dev`) |
|---|---|---|
| 1 | `capped_at` = `split_at`; the seam, the mixed rebuild and the relabel judged per half-hour | a pre-agreement cap-day over six hours, on an account now on IOG-SMB, re-priced by the seam: all EV off-peak, rate unchanged. A post-agreement one is still capped (guard). An account off IOG-SMB re-pricing its capped history keeps it capped. |
| 2 | the earliest-SMB floor; the account-level gates as "any IOG-SMB agreement"; the stitched EV-device schedules | two IOG-SMB agreements: the drain and the apply reach the first; the first's half-hours take the first product's EV rates |

Each PR is checked against the tariff goldens and `.build/real_db_parity.py` on `blocks-36`.
§3 predicts zero stored change on the soak: no re-run has reached the pre-agreement cap-days.
