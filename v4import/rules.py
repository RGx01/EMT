"""
v4import/rules.py — what happens to every table, column and state key of a v4 database
(import spec §5, §6; E design §3, §7).

Declarative on purpose. Every source table, every column of a carried table, and every
`store_meta` / `kraken_state` key must be named here. Anything else aborts the import and
is named in the message, so a table, column or key that v4 added after this file was
written reaches a person. It is never carried, and never dropped, by default.

Every carried column is carried as it is; the remaps are transforms. A column on step 5's
drop list (DROP_COLUMNS) is read by any transform that needs it and is not carried.
"""

# ── Tables ──────────────────────────────────────────────────────────────────────────
# Spec §8's order: config before what refers to it, blocks before their segments.
# Every column is listed: the check is that the source has no column missing from here.
CARRY = (
    ("config_periods", ("id", "effective_from", "effective_to", "billing_day",
                        "block_minutes", "timezone", "currency_symbol", "currency_code",
                        "site_name", "supplier", "change_reason")),
    ("meters", ("id", "config_period_id", "meter_id", "is_sub_meter", "parent_meter_id",
                "device_label", "meter_type", "protected", "inverter_possible",
                "power_sensor", "postcode_prefix", "postcode_source", "v2x_capable",
                "inverter_power_invert", "power_invert", "device_power_invert",
                "soc_sensor", "inverter_power_sensor", "device_power_sensor",
                "pv_power_sensor", "retired_at", "retired_reason", "power_source",
                "rate_source")),
    ("meter_channels", ("id", "meter_id", "channel", "read_sensor", "rate_sensor",
                        "standing_charge_sensor", "mpan", "tariff", "rate_source",
                        "standing_charge_source")),
    ("blocks", ("id", "block_start", "block_end", "meter_id", "config_period_id",
                "interpolated", "imp_kwh", "imp_kwh_grid", "imp_rate",
                "imp_cost", "imp_cost_exc",
                "imp_read_start", "imp_read_end", "exp_kwh", "exp_rate", "exp_cost",
                "exp_read_start", "exp_read_end", "standing_charge",
                "carbon_g", "imp_provisional", "source", "is_provisional",
                "needs_pass2_rerun", "imp_kwh_api", "exp_kwh_api", "needs_review",
                "carbon_intensity_g", "rate_corrected", "derivation_id",
                "finalised_from_cad", "review_reason", "review_dismissed", "rate_source")),
    ("block_segments", ("id", "block_start", "meter_id", "channel", "seq", "kwh",
                        "inc_rate", "exc_rate", "band", "attribution")),
    ("measured_cost", ("slot_start", "mpan", "direction", "cost_incl", "cost_excl",
                       "label", "kwh", "fetched_at", "home_kwh", "home_rate", "ev_kwh",
                       "ev_rate")),
    ("dispatch_slots", ("slot_start", "off_peak", "provider", "source", "captured_at",
                        "state", "energy_planned", "energy_completed", "raw_start",
                        "raw_end")),
    ("dispatch_history", ("slot_start", "kind", "provider", "source", "energy_kwh",
                          "first_seen", "last_seen", "raw_start", "raw_end")),
    ("historical_derivation", ("id", "scope", "subject", "period_from", "period_to",
                               "sensor_ids", "sensor_kind", "params", "derived_value",
                               "confirmed_value", "method_version", "derived_at", "source",
                               "superseded_by", "notes")),
    ("deleted_ranges", ("id", "meter_id", "start_utc", "end_utc", "created_at", "reason")),
    ("sub_meter_history", ("id", "captured_at", "meter_id", "soc_pct", "inverter_kw")),
    ("power_history", ("captured_at", "net_kw", "intensity", "carbon_gco2_min")),
    ("mix_history", ("captured_at", "fuel", "perc")),
    ("carbon_intensity", ("captured_at", "postcode", "intensity_forecast",
                          "intensity_actual", "ci_index")),
    ("generation_mix", ("block_id", "fuel", "perc")),
)

# Columns v5 no longer has (architecture step 5; spec §5.2's drop list). Named, so a source
# that holds them is not "unknown"; read by the transforms that need them first (the
# reconciled fold reads rate_reconciled), never carried.
DROP_COLUMNS = {
    "blocks": frozenset({
        "imp_kwh_remainder", "imp_cost_remainder",   # the house, derived where read
        "rate_reconciled",                           # folded into rate_source (transform 1)
        # the ex-VAT rate, the standing charge's ex-VAT and their provenance: derived where
        # read from imp_cost_exc (carried, spec v15) and the VAT calendar (5b)
        "imp_rate_exc", "standing_charge_exc", "exc_source",
        # the EV split and its bands: the block's segments, written from these by transform 2
        # where a block had none (5c)
        "imp_kwh_ev", "imp_cost_ev", "imp_rate_ev", "imp_ev_band", "imp_home_band",
    }),
}

# Spec §5.5: in-flight state the new app forms for itself, and `reads`, which nothing
# has ever written (spec Q7).
DROP_TABLES = frozenset({"current_block", "current_reads", "reads"})

# Carried key by key, under KEYS below.
KEYED_TABLES = ("store_meta", "kraken_state")

# ── Keys ────────────────────────────────────────────────────────────────────────────
# E design §7. Setup facts and user choices cross. Claims that work already ran do not,
# because v5 carries no v4 heals and its own passes have not run. A marker that stops
# v5 re-running a pass that would overwrite carried data crosses, as a setup fact.
CARRY_KEYS = {
    "store_meta": frozenset({
        "vat_calendar", "settings", "recorder_attribution_runs",
        "export_settlement_expected", "supplier", "carbon_paused",
        "region_probe_clobber_done",       # its pass would overwrite the carried regions
    }),
    "kraken_state": frozenset({
        "kraken_account_number", "data_source_mode",
        # how far back the carried history reaches, per direction
        "api_import_oldest_import", "api_import_oldest_export",
        "api_import_done_import", "api_import_done_export",
        # the one-time snapshot before the first live DCC write: v4 took it, and a restore
        # has just taken its own safety backup — re-run, it wrote a third 80 MB copy on the
        # Pi's SD card in the 10 Oct 2026 soak
        "pre_live_snapshot_done",
        # 4.5.23: bills held back because they had no EV where the split had the car, and
        # the ones whose 28 days ran out with EMT's own split kept. v5's forward code reads it.
        "bill_split_wait",
    }),
}

# Dropped and named. `written_by` and `db_uuid` are REGEN: v5's engine_startup stamps its
# own on the first start after the restore (spec §6).
DROP_KEYS = {
    "store_meta": frozenset({
        "written_by", "db_uuid", "schema_version", "data_epoch",
        "carbon_backfill_state", "carbon_backfill_done", "exc_backfill_state",
        "iog_split_backfill_state", "stale_exc_repair_state",
        "stale_iog_split_repair_state", "segment_backfill_state", "export_zero_fill_v1",
        "import_reprice_queue", "region_reconcile_pending", "reprice_history_state",
        "import_pricing_diag", "preimport_sites_applied",
        # what the API import found about each tariff era's costs — nothing reads it since the
        # pricing-health card went (engine step 2d, 23 Oct 2026)
        "import_cost_coverage",
    }),
    "kraken_state": frozenset({
        "last_poll_utc", "last_settlement_sweep_utc", "verify_pricing_cursor",
        "verify_pricing_summary", "import_health_summary", "import_run_status",
        "import_gaps_import", "import_gaps_export", "measured_vat_resync",
        # the settlement source, retired 21 Oct 2026 (data management design §1 (7)): v5
        # settles against the supplier wherever the API is on. Transform 5 re-runs the
        # main meter's half-hours of a v4 that chose its own readings ('cad').
        "billing_source",
    }),
}

# A key ending `_done` or `_done_v<n>` is a v4 completion marker: a heal or sweep that ran
# (`smb_rate_repair_done_v2`, `vat_backdate_heal_done_v4`, …). Explicit entries above win,
# which is how `region_probe_clobber_done` crosses. Checked against every key in v4's
# history (10 Oct 2026): none with this ending is a setup fact.
import re as _re
COMPLETION_MARKER = _re.compile(r"_done(_v\d+)?$")


def key_rule(table, key):
    """'carry', 'drop', or None for a key nothing here classifies."""
    if key in CARRY_KEYS[table]:
        return "carry"
    if key in DROP_KEYS[table] or COMPLETION_MARKER.search(key):
        return "drop"
    return None

