"""
block_store.py
==============
SQLite-backed storage for energy meter blocks and configuration history.

Replaces the flat blocks.json file with a relational schema that:
- Separates measurement data (blocks) from configuration (config_periods)
- Records configuration history so historical bills use the billing_day
  that was active when each block was recorded, not today's value
- Keeps what HA reported at the half-hour apart from the blocks (`ha_reads`)

Usage::

    store = BlockStore("/data/energy_meter_tracker/blocks.db")
    store.append_block(block_dict)
    blocks = store.get_blocks_for_range(start_dt, end_dt)
    store.close()

The :func:`open_block_store` factory is the preferred entry point — it applies
all required PRAGMAs and ensures the schema exists before returning.
"""

import contextlib
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import direct_import as _direct
import ev_split as _evsplit

# BL-79 (3): every block dict the engine loads carries the EV split ev_split reads — the EV
# segments, the columns only for a block with no segments — under its own names, which
# _row_to_block prefers to the imp_*_ev columns. Engine logic on a loaded block (PASS 2's EV
# reservation, the remainder recompute, the re-fit) then follows the record every surface reads,
# and a round trip writes it back to the columns.
_SPLIT_COLS = ("(" + _evsplit.sql_kwh("b") + ") AS _split_kwh, ("
               + _evsplit.sql_cost("b") + ") AS _split_cost, "
               + _evsplit.sql_band("b", "ev") + " AS _split_ev_band, "
               + _evsplit.sql_band("b", "house") + " AS _split_home_band, ")

logger = logging.getLogger("block_store")

# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

SCHEMA_VERSION = 1

# What a v5 database is (E design §2). Written into store_meta['emt_schema'] when v5 CREATES
# a database, and by the importer on the database it builds. A database without it is
# v4-shaped, whoever wrote it: v4, or a v5 build before the marker, whose `written_by`
# (4.5.22-a23) cannot tell it from v4. `schema_version` cannot either: it reads 1 on every
# v4 database (import spec §3). A revision, raised by each change to what v5 stores
# (the provenance words, the band, the column drop); a restore refuses a v5 backup of any
# other revision until the first release fixes the baseline.
EMT_SCHEMA = 5
#   1  10 Oct 2026  the marker itself (v4's schema and words)
#   2  10 Oct 2026  rate_source 'measured' -> 'settled'
#   3  10 Oct 2026  imp_kwh_remainder, imp_cost_remainder, rate_reconciled dropped
#   4  10 Oct 2026  imp_rate_exc, standing_charge_exc, exc_source dropped
#   5  10 Oct 2026  imp_kwh_ev, imp_cost_ev, imp_rate_ev, imp_ev_band, imp_home_band dropped

# A main import channel's priced segments, built by pricing and committed by the block write
# (append_block / append_block_replace) in the SAME transaction as the block row (step 5c).
STAGED_SEGMENTS = "_staged_segments"


def read_emt_schema(path):
    """The `emt_schema` revision of the database file at `path`, or None when it has none
    (a v4-shaped database). Opened read-only and immutable, like the import gate."""
    from urllib.parse import quote as _quote
    conn = sqlite3.connect("file:%s?immutable=1" % _quote(os.path.abspath(path)), uri=True)
    try:
        row = conn.execute("SELECT value FROM store_meta WHERE key = 'emt_schema'").fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None or row[0] is None:
        return None
    try:
        return int(json.loads(row[0]))
    except (TypeError, ValueError):
        return None

# ── 3.5.0 Historical-import provenance source values ─────────────────────────
# Reconstructed blocks carry one of these in blocks.source. The reconstructed-
# history delete filter matches the shared 'imported' prefix, so a bad import can
# be wiped without touching native (live/kraken) data.
def _in_span(spans, start: str) -> bool:
    """True when `start` falls inside any {from,to} span (half-open; to=None = open)."""
    for sp in spans or []:
        frm, to = sp.get("from"), sp.get("to")
        if frm and start >= frm and (to is None or start < to):
            return True
    return False


IMPORTED_SOURCE_API     = "imported_api"       # from the Octopus consumption API
IMPORTED_SOURCE_CSV     = "imported_csv"        # from a supplied CSV (rate-from-cost)
IMPORTED_SOURCE_BLENDED = "imported_blended"    # go-live period straddling live capture
IMPORTED_SOURCES        = (IMPORTED_SOURCE_API, IMPORTED_SOURCE_CSV, IMPORTED_SOURCE_BLENDED)
IMPORTED_SOURCE_PREFIX  = "imported"            # blocks.source LIKE 'imported%'

_DDL = """
-- Schema version tracking
CREATE TABLE IF NOT EXISTS store_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Configuration history: one row per config period.
-- Every time meters_config.json is saved a new row is inserted and
-- effective_to on the previous row is set to the same timestamp.
CREATE TABLE IF NOT EXISTS config_periods (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    effective_from   TEXT    NOT NULL,
    effective_to     TEXT,
    billing_day      INTEGER NOT NULL DEFAULT 1,
    block_minutes    INTEGER NOT NULL DEFAULT 30,
    timezone         TEXT    NOT NULL DEFAULT 'UTC',
    currency_symbol  TEXT    NOT NULL DEFAULT '£',
    currency_code    TEXT    NOT NULL DEFAULT 'GBP',
    site_name        TEXT,
    supplier         TEXT,               -- energy supplier name (display + historical record)
    change_reason    TEXT
);  -- full_config_json removed in 2.1.0; meters/channels in normalised tables

-- Meter definitions: one row per meter per config period.
-- meter_id is a stable string key (e.g. "electricity_main", "ev_charger").
-- blocks.meter_id references this by value (no FK) so adding/removing/
-- re-adding a meter never causes constraint issues on historical blocks.
CREATE TABLE IF NOT EXISTS meters (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    config_period_id   INTEGER NOT NULL,
    meter_id           TEXT    NOT NULL,
    is_sub_meter       INTEGER NOT NULL DEFAULT 0,
    parent_meter_id    TEXT,               -- meter_id of parent (sub-meters only)
    device_label       TEXT,               -- display name e.g. "EV Charger"
    meter_type         TEXT,               -- battery, ev, heat_pump, or NULL
    protected          INTEGER DEFAULT 0,  -- protected load (EV, heat pump)
    inverter_possible  INTEGER DEFAULT 0,  -- battery / inverter capable
    power_sensor       TEXT,               -- HA entity_id (main meter only)
    postcode_prefix    TEXT,               -- UK carbon intensity, OUTWARD CODE ONLY e.g. "SW1A" (main meter only)
    postcode_source    TEXT,               -- 'user' | 'octopus' | 'unknown' — provenance of postcode_prefix (main meter only)
    v2x_capable             INTEGER DEFAULT 0,  -- V2G / bidirectional charging capable
    inverter_power_invert   INTEGER DEFAULT 0,  -- negate inverter power sensor value
    power_invert            INTEGER DEFAULT 0,  -- negate MAIN power sensor value (Live Power)
    device_power_invert     INTEGER DEFAULT 0,  -- negate device power sensor value (Live Power)
    soc_sensor         TEXT,               -- HA entity_id for battery SoC % (informational)
    inverter_power_sensor TEXT,            -- HA entity_id for inverter power W/kW (informational)
    device_power_sensor TEXT,              -- HA entity_id for EV/heat pump power W/kW (informational)
    pv_power_sensor     TEXT,              -- HA entity_id for battery PV/solar power W/kW (informational)
    retired_at          TEXT,              -- ISO date from which this meter is retired (NULL = active)
    retired_reason      TEXT,              -- optional note e.g. "Device replaced", "Moved property"
    FOREIGN KEY (config_period_id) REFERENCES config_periods(id),
    UNIQUE (config_period_id, meter_id)
);

CREATE INDEX IF NOT EXISTS idx_meters_period    ON meters (config_period_id);
CREATE INDEX IF NOT EXISTS idx_meters_meter_id  ON meters (meter_id);

-- Per-channel sensor configuration for each meter.
CREATE TABLE IF NOT EXISTS meter_channels (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_id                INTEGER NOT NULL,  -- FK → meters.id
    channel                 TEXT    NOT NULL,  -- 'import' or 'export'
    read_sensor             TEXT,              -- HA entity_id for kWh sensor
    rate_sensor             TEXT,              -- HA entity_id for rate sensor
    standing_charge_sensor  TEXT,              -- HA entity_id (optional)
    mpan                    TEXT,              -- meter point reference number
    tariff                  TEXT,              -- tariff name / code
    FOREIGN KEY (meter_id) REFERENCES meters(id),
    UNIQUE (meter_id, channel)
);

CREATE INDEX IF NOT EXISTS idx_meter_channels_meter ON meter_channels (meter_id);

-- Blocks: pure measurement data, no repeated config fields.
-- config_period_id links each block to the config that was active when
-- it was recorded.
CREATE TABLE IF NOT EXISTS blocks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    block_start      TEXT    NOT NULL,
    block_end        TEXT    NOT NULL,

    meter_id         TEXT    NOT NULL,
    config_period_id INTEGER NOT NULL,
    interpolated     INTEGER NOT NULL DEFAULT 0,
    imp_kwh          REAL,
    imp_kwh_grid     REAL,
    imp_rate         REAL,
    imp_cost         REAL,
    imp_cost_exc     REAL,               -- 4.1.x BL-23: exc-VAT import cost (NULL until captured; inc figures unchanged)
    imp_read_start   REAL,
    imp_read_end     REAL,
    exp_kwh          REAL,
    exp_rate         REAL,
    exp_cost         REAL,
    exp_read_start   REAL,
    exp_read_end     REAL,
    standing_charge  REAL    NOT NULL DEFAULT 0,
    carbon_g         REAL,               -- net gCO2 for this block (NULL if no CI data)
    imp_provisional  INTEGER NOT NULL DEFAULT 0,  -- 1 = sub-meter kWh written without post-boundary read; 0 = final
    -- ── 3.0.0 Kraken API integration columns ──────────────────────────────
    source              TEXT,                       -- 'ha_sensor' | 'kraken_api' | 'kraken_mini' (NULL = legacy/ha_sensor)
    is_provisional      INTEGER NOT NULL DEFAULT 0, -- 1 = main meter not yet DCC-settled (api / api+mini modes)
    needs_pass2_rerun   INTEGER NOT NULL DEFAULT 0, -- 1 = DCC arrived, PASS 2+3b re-run pending
    imp_kwh_api         REAL,                       -- DCC-settled import kWh from Kraken REST (NULL until settlement)
    exp_kwh_api         REAL,                       -- DCC-settled export kWh from Kraken REST (NULL until settlement)
    needs_review        INTEGER NOT NULL DEFAULT 0, -- 1 = CAD/Mini vs DCC drift exceeded threshold
    carbon_intensity_g  REAL,                       -- gCO2/kWh at block_start, stored at write time (survives CI table pruning)
    rate_corrected      INTEGER NOT NULL DEFAULT 0, -- 1 = user manually corrected the rate; dispatch reconciliation must not touch it
    -- ── 3.5.0 Historical import ───────────────────────────────────────────────
    derivation_id       INTEGER,                    -- FK → historical_derivation for a reconstructed block (NULL = live)
    -- ── device sensors design §7: the sensor entry that recorded a live block's channel
    imp_sensor_id       INTEGER,                    -- FK → device_sensors.id (NULL: imported, rebuilt, no sensor)
    exp_sensor_id       INTEGER,
    FOREIGN KEY (config_period_id) REFERENCES config_periods(id),
    UNIQUE (block_start, meter_id)
);

-- Device sensors design §2 (19 Oct 2026): a meter's sensors, keyed by its own id (e.g.
-- 'ev_charger'), not per config period. One PRIMARY per channel — the live recorder, which
-- meter_channels.read_sensor mirrors — and ordered SECONDARIES Device History stitches for older
-- history (`position` NULL: kept only because a block points at it, never stitched). A rename
-- changes an entry's role and keeps its id, so the blocks it recorded still name it.
CREATE TABLE IF NOT EXISTS device_sensors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_id    TEXT    NOT NULL,
    channel     TEXT    NOT NULL,
    entity_id   TEXT    NOT NULL,
    role        TEXT    NOT NULL CHECK (role IN ('primary', 'secondary')),
    position    INTEGER,
    origin      TEXT    NOT NULL DEFAULT 'configured',   -- configured | rename | v4_attribution
    added_at    TEXT    NOT NULL,
    UNIQUE (meter_id, channel, entity_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_device_sensors_primary
    ON device_sensors (meter_id, channel) WHERE role = 'primary';

-- Soft delete design §2 (20 Oct 2026): what HA reported, kept apart from the blocks built from
-- it. A half-hour recorded live (its kWh as finalised, HA's registers at its edges), or an hour
-- from HA's recorder kept AS the hour, so Device History's split by the house's import shape
-- runs against the blocks as they are when it is applied. A delete of blocks leaves it; only a
-- device delete removes a device's rows. `sensor_id` names the device_sensors entry the figure
-- came from (NULL: none known, e.g. the Mini); `sensor_inferred` = 1 where the v4 import named
-- the device's primary, its block having had no pointer.
CREATE TABLE IF NOT EXISTS ha_reads (
    meter_id         TEXT    NOT NULL,
    channel          TEXT    NOT NULL,
    origin           TEXT    NOT NULL CHECK (origin IN ('live', 'recorder')),
    period_start     TEXT    NOT NULL,
    minutes          INTEGER NOT NULL,
    sensor_id        INTEGER,
    sensor_inferred  INTEGER NOT NULL DEFAULT 0,
    kwh              REAL    NOT NULL,
    read_start       REAL,
    read_end         REAL,
    interpolated     INTEGER NOT NULL DEFAULT 0,
    recorded_at      TEXT    NOT NULL,
    PRIMARY KEY (meter_id, channel, origin, period_start)
);

CREATE INDEX IF NOT EXISTS idx_blocks_start     ON blocks (block_start);

CREATE INDEX IF NOT EXISTS idx_blocks_meter     ON blocks (meter_id);
CREATE INDEX IF NOT EXISTS idx_blocks_meter_dt  ON blocks (meter_id, block_start);
CREATE INDEX IF NOT EXISTS idx_blocks_period    ON blocks (config_period_id);


-- current_block: single-row table holding the in-progress block state.
-- Replaces current_block.json as the engine's live state store.
-- gap_detected_at IS NOT NULL means a gap marker is active.
CREATE TABLE IF NOT EXISTS current_block (
    id              INTEGER PRIMARY KEY CHECK (id = 1),  -- enforce single row
    block_start     TEXT,       -- UTC ISO — current block window start
    block_end       TEXT,       -- UTC ISO — current block window end
    last_checkpoint TEXT,       -- UTC ISO — last capture timestamp
    gap_detected_at TEXT,       -- UTC ISO — when gap was detected, NULL if no gap
    gap_last_block_start TEXT,  -- UTC ISO — start of last finalised block before gap
    interpolated    INTEGER NOT NULL DEFAULT 0
);

-- Rolling reads/rates buffer for the in-progress block.
-- is_gap_seed: 0=live read, 1=gap seed kWh read, 2=gap seed rate reading.
-- Gap seed rows are the pre-gap meter readings used to interpolate missing blocks.
CREATE TABLE IF NOT EXISTS current_reads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at     TEXT    NOT NULL,
    meter_id        TEXT    NOT NULL,
    channel         TEXT    NOT NULL,   -- 'import' or 'export'
    channel_type    TEXT    NOT NULL DEFAULT 'read',  -- 'read' or 'rate'
    value           REAL    NOT NULL,   -- kWh for reads, £/kWh for rates
    standing_charge REAL,
    is_gap_seed     INTEGER NOT NULL DEFAULT 0  -- 0=live, 1=gap seed read, 2=gap seed rate
);

CREATE INDEX IF NOT EXISTS idx_current_reads_meter ON current_reads (meter_id, channel, channel_type);
CREATE INDEX IF NOT EXISTS idx_current_reads_time  ON current_reads (captured_at);

-- Carbon intensity samples from National Grid API (15-min cadence, ~4-day retention).
-- postcode stored per row so config changes don't invalidate historical data.
CREATE TABLE IF NOT EXISTS carbon_intensity (
    captured_at          TEXT NOT NULL,      -- UTC ISO, 30-min API slot boundary
    postcode             TEXT NOT NULL,
    intensity_forecast   REAL,               -- gCO₂/kWh forecast (always populated)
    intensity_actual     REAL,               -- gCO₂/kWh actual (populated ~24hr later)
    ci_index             TEXT,               -- very-low/low/moderate/high/very-high
    PRIMARY KEY (captured_at, postcode)
);
-- Computed alias: intensity = COALESCE(actual, forecast) for backward compat
-- All consumers should use get_nearest_carbon_intensity which applies this.

CREATE INDEX IF NOT EXISTS idx_carbon_intensity_time ON carbon_intensity (captured_at);

-- Generation mix per block — fuel type percentages at time of each block.
-- Populated at block finalise time and backfilled alongside carbon_g.
-- One row per fuel per block. Lives as long as the block.
CREATE TABLE IF NOT EXISTS generation_mix (
    block_id    INTEGER NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    fuel        TEXT    NOT NULL,   -- 'wind','solar','gas','nuclear','biomass','hydro','imports','other'
    perc        REAL    NOT NULL,   -- percentage of generation mix (0.0–100.0)
    PRIMARY KEY (block_id, fuel)
);

-- High-resolution generation mix (CI-tick cadence ~15min, 48hr retention).
-- Independent of block size — updated every CI tick so chart stays current.
CREATE TABLE IF NOT EXISTS mix_history (
    captured_at TEXT NOT NULL,  -- CI slot time (UTC, e.g. '2026-05-01T06:30')
    fuel        TEXT NOT NULL,
    perc        REAL NOT NULL,
    PRIMARY KEY (captured_at, fuel)
);
CREATE INDEX IF NOT EXISTS idx_mix_history_time ON mix_history (captured_at);

-- High-resolution net power history (engine-tick cadence ~10s, 48hr retention).
-- Only written when a power sensor is configured. intensity is denormalised from
-- the nearest carbon_intensity row at capture time.
CREATE TABLE IF NOT EXISTS power_history (
    captured_at      TEXT PRIMARY KEY,  -- UTC ISO
    net_kw           REAL NOT NULL,     -- positive = importing, negative = exporting
    intensity        REAL,              -- gCO2/kWh at capture time, NULL if no postcode
    carbon_gco2_min  REAL               -- net carbon rate gCO2/min from meter reads, NULL if unavailable
);

CREATE INDEX IF NOT EXISTS idx_power_history_time ON power_history (captured_at);

CREATE TABLE IF NOT EXISTS sub_meter_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT    NOT NULL,  -- UTC ISO
    meter_id    TEXT    NOT NULL,  -- battery sub-meter ID
    soc_pct     REAL,              -- state of charge 0-100, NULL if no SoC sensor
    inverter_kw REAL               -- inverter power kW, positive=charging, negative=discharging, NULL if no sensor
);

CREATE INDEX IF NOT EXISTS idx_sub_meter_history_time ON sub_meter_history (captured_at);
CREATE INDEX IF NOT EXISTS idx_sub_meter_history_meter ON sub_meter_history (meter_id, captured_at);

-- ── 3.0.0 Kraken API integration ─────────────────────────────────────────────
-- Persists ingester progress so a restart resumes from the last successful poll
-- rather than re-running a full backfill (quota protection). Single-row-per-key.
-- The blocks unique index idx_blocks_start_meter and the partial sweep indexes
-- are created in _ensure_schema AFTER the column ALTERs and duplicate detection,
-- because on an upgrade DB those columns do not exist when this DDL first runs.
CREATE TABLE IF NOT EXISTS kraken_state (
    key        TEXT PRIMARY KEY,   -- e.g. 'last_poll_utc', 'last_backfill_utc'
    value      TEXT,               -- ISO UTC timestamp or opaque string
    updated_at TEXT                -- ISO UTC of last write
);

-- ── 3.1.0 Intelligent dispatch overlay ───────────────────────────────────────
-- Durable record of which 30-min slots had an Intelligent smart-charge dispatch.
-- Dispatches are ephemeral in the Kraken API (planned dispatches churn and
-- disappear), so we capture them forward each poll and persist here. The overlay
-- resolver reads THIS table (not a live re-fetch) to decide whether a block's
-- slot should get the off-peak dispatch rate — gated by actual meter draw at
-- pricing time. One row per slot; re-capture upserts (last-write-wins).
CREATE TABLE IF NOT EXISTS dispatch_slots (
    slot_start  TEXT NOT NULL,      -- naive-UTC ISO, 30-min slot boundary
    off_peak    INTEGER NOT NULL DEFAULT 1,  -- 1 = smart-charge (off-peak candidate)
    provider    TEXT,               -- e.g. 'MYENERGI_V2', 'OHME', 'TESLA'
    source      TEXT,               -- meta.source: smart-charge/bump-charge/unknown
    captured_at TEXT NOT NULL,      -- UTC ISO when we recorded this slot
    -- Dispatch lifecycle capture (see dispatch_validation_design.md). OBSERVE-ONLY:
    -- recorded but NOT yet used for billing. Lets us validate a planned slot
    -- against what actually charged, instead of the meter-draw floor alone.
    state            TEXT,          -- 'planned' | 'started' | 'completed'
    energy_planned   REAL,          -- forecast energyAddedKwh for this slot (kWh)
    energy_completed REAL,          -- delivered energy for this slot at settlement (kWh)
    raw_start   TEXT,               -- BL-11: exact dispatch start (naive-UTC ISO, second precision)
    raw_end     TEXT,               -- BL-11: exact dispatch end (naive-UTC ISO, second precision)
    PRIMARY KEY (slot_start)
);
CREATE INDEX IF NOT EXISTS idx_dispatch_slots_start ON dispatch_slots (slot_start);

-- Local dispatch ACCUMULATION (observe-only; see dispatch_validation_design.md §11).
-- Separate from dispatch_slots (the billing surface) on purpose: this records
-- EVERY planned/completed dispatch we ever see, accumulated across polls, so
-- "absent" becomes meaningful and transient/tail dispatches aren't lost to the
-- per-poll snapshot. NOTHING reads this for billing. Mirrors BCD's local
-- intelligent_dispatches_history.
CREATE TABLE IF NOT EXISTS dispatch_history (
    slot_start  TEXT NOT NULL,      -- naive-UTC ISO, 30-min slot boundary
    kind        TEXT NOT NULL,      -- 'planned' | 'completed' | 'started'
    provider    TEXT,
    source      TEXT,               -- meta.source (smart-charge/bump/unknown)
    energy_kwh  REAL,               -- signed kWh for this slot (negative = charge)
    first_seen  TEXT NOT NULL,      -- UTC ISO — first poll that saw this slot/kind
    last_seen   TEXT NOT NULL,      -- UTC ISO — most recent poll that saw it
    raw_start   TEXT,               -- BL-11: exact dispatch start (naive-UTC ISO, second precision)
    raw_end     TEXT,               -- BL-11: exact dispatch end (naive-UTC ISO, second precision)
    PRIMARY KEY (slot_start, kind)
);
CREATE INDEX IF NOT EXISTS idx_dispatch_history_start ON dispatch_history (slot_start);

-- ── 3.5.0 Historical import: derivation provenance ────────────────────────────
-- One row per reconstruction (a device-attribution over a span, or a rate for a
-- tariff-period×tier). Reconstructed blocks point here via blocks.derivation_id,
-- so any imported figure can answer "which sensor / rate produced this, when",
-- and be rebuilt with new/extended data. Supersede (don't delete) on rebuild to
-- keep the audit trail; superseded_by IS NULL = the current derivation.
CREATE TABLE IF NOT EXISTS historical_derivation (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scope           TEXT NOT NULL,              -- 'device_attribution' | 'rate'
    subject         TEXT,                       -- device meter_id (attribution) | channel 'import'/'export' (rate)
    period_from     TEXT NOT NULL,              -- UTC span this derivation covers
    period_to       TEXT NOT NULL,
    sensor_ids      TEXT,                       -- JSON list of sensor entity_ids (device scope)
    sensor_kind     TEXT,                       -- energy_total | power | session
    params          TEXT,                       -- JSON: split weights/ε, or rate tier + confidence
    derived_value   REAL,                       -- derived rate (rate scope), etc.
    confirmed_value REAL,                       -- user-confirmed rate, if overridden
    method_version  INTEGER NOT NULL DEFAULT 1, -- bump when the algorithm changes → stale-derivation detection
    derived_at      TEXT NOT NULL,              -- UTC ISO
    source          TEXT,                       -- imported_api | imported_csv | recorder_probe
    superseded_by   INTEGER,                    -- id that replaced this (NULL = current)
    notes           TEXT,
    FOREIGN KEY (superseded_by) REFERENCES historical_derivation(id)
);
CREATE INDEX IF NOT EXISTS idx_hderiv_subject ON historical_derivation (scope, subject);
CREATE INDEX IF NOT EXISTS idx_hderiv_current ON historical_derivation (superseded_by);

-- ── BL-8 phase 2: deliberate-deletion persistence (deleted_ranges tombstone) ──
-- A manual delete records its span here so BL-8 backfill, the settlement sweep, and
-- the outage gap-scan SKIP it (a deleted range stays deleted instead of being
-- re-created as if it were an outage hole). Consulted ONLY at block-CREATION and
-- window-scan points — never by billing/chart/settlement reads, so the numbers are
-- byte-identical. meter_id '*' = all meters; [start_utc, end_utc) is half-open, in
-- block_start grain. An explicit, TARGETED user fill (re-import / per-gap / CSV /
-- range) clears or splits the covering row; a blanket "recover all" respects it.
CREATE TABLE IF NOT EXISTS deleted_ranges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_id    TEXT NOT NULL,       -- 'electricity_main', a sub-meter id, or '*' = all meters
    start_utc   TEXT NOT NULL,       -- inclusive, naive-UTC ISO 'YYYY-MM-DDTHH:MM:SS'
    end_utc     TEXT NOT NULL,       -- EXCLUSIVE upper bound
    created_at  TEXT NOT NULL,       -- UTC ISO
    reason      TEXT                 -- 'user_delete' | 'user_purge' | free text
);
CREATE INDEX IF NOT EXISTS idx_deleted_ranges ON deleted_ranges (meter_id, start_utc, end_utc);

-- BL-27: the priced-segment decomposition of a block's grid import. Each row is ONE
-- homogeneously-priced slice; a block carries 1–4 (the 4-rate matrix). `band` and
-- `attribution` are OPEN labels — storage and surfaces never switch on them, only the
-- classifier assigns meaning (a new rate dimension is a new label, not a schema change).
-- The legacy imp_rate/imp_cost/imp_*_ev/imp_*_exc columns are PROJECTIONS over these rows
-- (see pricing_segments). Invariant: Σ block_segments.kwh == the block's grid kWh.
CREATE TABLE IF NOT EXISTS block_segments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    block_start  TEXT NOT NULL,
    meter_id     TEXT NOT NULL,
    channel      TEXT NOT NULL DEFAULT 'import',
    seq          INTEGER NOT NULL,        -- order within the block (EV-off, EV-peak, house-off, house-day)
    kwh          REAL NOT NULL,
    inc_rate     REAL NOT NULL,
    exc_rate     REAL,                    -- per-segment ex-VAT rate (NULL → view falls back to inc ÷ VAT)
    band         TEXT NOT NULL,           -- open label: off_peak | day | peak | …
    attribution  TEXT NOT NULL            -- open label: house | ev | …
);
CREATE INDEX IF NOT EXISTS idx_block_segments ON block_segments (block_start, meter_id, channel);

-- BL-53: permanent cache of Octopus's BILLED per-slot cost/label (measurements API).
-- A settled slot's billed cost is immutable, so this is never re-fetched once stored.
CREATE TABLE IF NOT EXISTS measured_cost (
    slot_start  TEXT NOT NULL,
    mpan        TEXT NOT NULL DEFAULT '',
    direction   TEXT NOT NULL DEFAULT 'CONSUMPTION',
    cost_incl   REAL,
    cost_excl   REAL,
    label       TEXT,                    -- OFF_PEAK | STANDARD_RATE | mixed
    kwh         REAL,
    fetched_at  TEXT,
    -- C2 (4.5.7): the four-bucket SETTLED Home/EV split + per-bucket rate
    -- (getDeviceConsumptionBreakdown). NULL for non-SMB / not-yet-fetched. One row
    -- per settled slot holds cost + label + split — the whole settled truth (design §4).
    home_kwh    REAL,
    home_rate   REAL,
    ev_kwh      REAL,
    ev_rate     REAL,
    PRIMARY KEY (slot_start, mpan, direction)
);
"""


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def outward_code(postcode) -> Optional[str]:
    """Outward (first) part of a UK postcode, uppercased and space-stripped.

    'sw1a 1aa' → 'SW1A', 'M1 1AE' → 'M1', 'EH8' → 'EH8'. This is the ONLY part
    EMT persists (privacy: it resolves to a coarse DNO region, nothing more).
    A full postcode may be *read* transiently to detect a change, but callers
    must pass it through here before storage.
    """
    if not postcode:
        return None
    p = str(postcode).strip().upper()
    if not p:
        return None
    if " " in p:                      # normal case: outward code is before the space
        return p.split()[0]
    # No space present. A UK inward code is always 3 chars (digit + 2 letters);
    # strip it only when the tail actually looks like one, else assume already-outward.
    if len(p) > 3:
        tail = p[-3:]
        if tail[0].isdigit() and tail[1:].isalpha():
            return p[:-3]
    return p


def _property_key(p: dict) -> Optional[str]:
    """Stable, NON-identifying handle for a property — a short hash of the Octopus
    property id (or address+postcode as a fallback). Used only to group a
    property's records and to give the reconcile/confirmation UI a stable key;
    never stored, and not reversible to an address."""
    import hashlib
    pid = p.get("id")
    if pid is not None and str(pid).strip():
        base = "id:" + str(pid)
    else:
        addr = (p.get("address_line_1") or p.get("address_line1") or "").strip()
        pc   = (p.get("postcode") or p.get("post_code") or "").strip()
        if not (addr or pc):
            return None
        base = "addr:" + addr + "|" + pc
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]


def derive_region_periods(account: dict) -> list:
    """From a REST account payload, per-property tenancy spans for reconciliation.

    Returns [{"outcode","from","to","key","hint"}] sorted by move-in date, with
    consecutive records of the SAME property merged. Fields:
      - outcode : OUTWARD CODE ONLY (the only part persisted) — full postcodes
                  are read here and immediately reduced.
      - from/to : move-in / move-out (tolerant of moved_in_at/moved_in etc.).
      - key     : non-reversible property hash (grouping / UI handle; NOT stored).
      - hint    : the town, for the "name this site" UI — DISPLAY-ONLY, must NOT
                  be persisted.

    Different properties that happen to share an outward code are kept SEPARATE
    (they're different sites the user may name differently). Only when no stable
    key is derivable do we fall back to region-level merging so behaviour never
    regresses below the outward code.
    """
    spans = []
    for p in (account.get("properties") or []):
        oc = outward_code(p.get("postcode") or p.get("post_code"))
        if not oc:
            continue
        spans.append({
            "outcode": oc,
            "from": p.get("moved_in_at") or p.get("moved_in") or None,
            "to":   p.get("moved_out_at") or p.get("moved_out") or None,
            "key":  _property_key(p),
            "hint": (p.get("town") or None),
        })
    spans.sort(key=lambda s: s.get("from") or "")
    merged: list = []
    for s in spans:
        same = False
        if merged:
            prev = merged[-1]
            if s["key"] and prev["key"]:
                same = (s["key"] == prev["key"])          # same physical property
            else:
                same = (s["outcode"] == prev["outcode"])  # fallback: same region
        if same:
            merged[-1]["to"] = s["to"]                     # extend the open end
        else:
            merged.append(dict(s))
    return merged


def local_date_to_utc_bounds(local_date: str, tz_name: str) -> tuple[str, str]:
    """
    Convert a local date string (YYYY-MM-DD) to a pair of UTC ISO strings
    (utc_start_inclusive, utc_end_exclusive) representing the full local day
    in the given timezone.

    Handles DST correctly:
      - BST start day (23 hours): returns 23hr window
      - BST end day (25 hours): returns 25hr window
      - GMT days: returns exact 24hr window aligned to midnight UTC

    Returns naive ISO strings (no Z suffix) suitable for direct comparison
    with block_start values stored in the database.

    Example (Europe/London, BST):
      local_date_to_utc_bounds('2026-04-15', 'Europe/London')
      → ('2026-04-14T23:00:00', '2026-04-15T23:00:00')

    Example (Europe/London, GMT):
      local_date_to_utc_bounds('2026-01-15', 'Europe/London')
      → ('2026-01-15T00:00:00', '2026-01-16T00:00:00')

    Example (BST start day — 23 hours):
      local_date_to_utc_bounds('2026-03-29', 'Europe/London')
      → ('2026-03-28T23:00:00', '2026-03-29T23:00:00')

    Example (BST end day — 25 hours):
      local_date_to_utc_bounds('2026-10-25', 'Europe/London')
      → ('2026-10-24T23:00:00', '2026-10-25T23:00:00')
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    year, month, day = int(local_date[:4]), int(local_date[5:7]), int(local_date[8:10])

    # Midnight at start of local day
    local_start = datetime(year, month, day, 0, 0, 0, tzinfo=tz)
    # Midnight at start of next local day — computed by advancing date then re-localising
    next_day    = (datetime(year, month, day) + timedelta(days=1))
    local_end   = datetime(next_day.year, next_day.month, next_day.day, 0, 0, 0, tzinfo=tz)

    # Convert to UTC and strip tzinfo for DB comparison
    utc_start = local_start.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    utc_end   = local_end.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    return utc_start.isoformat(), utc_end.isoformat()


def local_datetime_to_utc(local_date: str, local_time: str, tz_name: str) -> str:
    """Convert a local wall-clock date + HH:MM to a naive-UTC ISO string (no Z),
    DST-correct, for direct comparison with block_start. Used by the contiguous
    date-range delete so `FROM 14/04 00:00 → TO 15/04 12:00` means one continuous
    span, not a per-day time-of-day window."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    y, m, d = int(local_date[:4]), int(local_date[5:7]), int(local_date[8:10])
    hh, mm = int(local_time[:2]), int(local_time[3:5])
    dt = datetime(y, m, d, hh, mm, 0, tzinfo=tz)
    return dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None).isoformat()


def utc_to_local_label(utc_iso: str, tz_name: str, with_date: bool = True) -> str:
    """Format a naive-UTC ISO string as local wall-clock time for display (the gap
    list / deleted-ranges list). Returns 'YYYY-MM-DD HH:MM' (or 'HH:MM' when
    with_date=False). Falls back to the raw string on any error."""
    try:
        tz = ZoneInfo(tz_name)
        dt = datetime.fromisoformat(utc_iso).replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
        return dt.strftime("%Y-%m-%d %H:%M" if with_date else "%H:%M")
    except Exception:
        return str(utc_iso).replace("T", " ")


def local_date_range_to_utc_bounds(first_local_date: str, last_local_date: str,
                                    tz_name: str) -> tuple[str, str]:
    """
    Convert an inclusive local date range to UTC bounds for DB filtering.

    Returns (utc_start_inclusive, utc_end_exclusive) covering all blocks
    whose local date falls within [first_local_date, last_local_date].

    Handles DST correctly across the full range — the UTC start uses the
    offset at first_local_date, the UTC end uses the offset at last_local_date.
    Both are computed independently so a range straddling a DST transition
    (e.g. March 3 GMT → April 2 BST) is handled correctly.
    """
    utc_start, _ = local_date_to_utc_bounds(first_local_date, tz_name)
    _, utc_end   = local_date_to_utc_bounds(last_local_date,  tz_name)
    return utc_start, utc_end


def _channel(meter_block: dict, channel_name: str) -> dict:
    return (meter_block.get("channels") or {}).get(channel_name) or {}


def _staged(block: dict) -> list:
    """(block_start, meter_id, segments) for each import channel that pricing left its
    segments on (STAGED_SEGMENTS), taking them off the dict so a re-used dict cannot write
    them twice. The block write commits them in its own transaction (step 5c, 10 Oct 2026)."""
    out = []
    for mid, mb in (block.get("meters") or {}).items():
        ch = ((mb or {}).get("channels") or {}).get("import")
        if isinstance(ch, dict) and STAGED_SEGMENTS in ch:
            out.append((block.get("start"), mid, ch.pop(STAGED_SEGMENTS) or []))
    return out


def _live_reads(block: dict, meter_ids) -> list[dict]:
    """`ha_reads` rows for what HA reported for `meter_ids` in a block being written live (soft
    delete design §2): each channel holding both registers (on the main meter, not the no-reads
    0.0 / 0.0), its kWh as EMT finalised it (an EV
    charger's sensor resets each session, so the kWh, not the registers' difference, is the
    figure). Taken at the write, before settlement can put Octopus's kWh on the block."""
    try:
        minutes = int((datetime.fromisoformat(block["end"])
                       - datetime.fromisoformat(block["start"])).total_seconds() // 60)
    except (KeyError, TypeError, ValueError):
        return []
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    interp = 1 if block.get("interpolated") else 0
    out = []
    meters = block.get("meters") or {}
    for mid in meter_ids or ():
        meta = ((meters.get(mid) or {}).get("meta")) or {}
        for ch_name, ch in (((meters.get(mid) or {}).get("channels")) or {}).items():
            if ch_name not in ("import", "export") or not isinstance(ch, dict):
                continue
            rs, re_ = ch.get("read_start"), ch.get("read_end")
            if rs is None or re_ is None or ch.get("kwh") is None:
                continue
            # A grid register is never 0: 0.0 / 0.0 on the main meter is compute_channel's
            # no-reads output, not HA's. A device's session sensor sits at 0 between charges.
            if rs == 0.0 and re_ == 0.0 and not meta.get("sub_meter"):
                continue
            out.append({"meter_id": mid, "channel": ch_name, "origin": "live",
                        "period_start": block["start"], "minutes": minutes,
                        "kwh": float(ch["kwh"]), "read_start": ch["read_start"],
                        "read_end": ch["read_end"],
                        "interpolated": 1 if (interp or ch.get("interpolated")) else 0,
                        "recorded_at": now})
    return out


_HA_READS_UPSERT = """
    INSERT INTO ha_reads (meter_id, channel, origin, period_start, minutes, sensor_id, kwh,
                          read_start, read_end, interpolated, recorded_at)
    VALUES (:meter_id, :channel, :origin, :period_start, :minutes,
            (SELECT s.id FROM device_sensors s WHERE s.meter_id = :meter_id
                AND s.channel = :channel AND s.role = 'primary'),
            :kwh, :read_start, :read_end, :interpolated, :recorded_at)
    ON CONFLICT (meter_id, channel, origin, period_start) DO UPDATE SET
        minutes = excluded.minutes, kwh = excluded.kwh, read_start = excluded.read_start,
        read_end = excluded.read_end, interpolated = excluded.interpolated,
        recorded_at = excluded.recorded_at,
        sensor_id = COALESCE(ha_reads.sensor_id, excluded.sensor_id)
"""


def _block_rows(block: dict, config_period_id: int, tz_name: str) -> list[dict]:
    """
    Decompose a finalised block dict (as written to blocks.json) into a list
    of row dicts, one per meter, ready for INSERT into the blocks table.
    """
    block_start = block.get("start", "")
    block_end   = block.get("end", "")
    interpolated = 1 if block.get("interpolated") else 0


    rows = []
    for meter_id, meter_block in (block.get("meters") or {}).items():
        meta    = meter_block.get("meta") or {}
        imp     = _channel(meter_block, "import")
        exp     = _channel(meter_block, "export")

        rows.append({
            "block_start":       block_start,
            "block_end":         block_end,

            "meter_id":          meter_id,
            "config_period_id":  config_period_id,
            "interpolated":      interpolated,
            # import channel
            "imp_kwh":           imp.get("kwh"),
            "imp_kwh_grid":      imp.get("kwh_grid"),
            "imp_rate":          imp.get("rate"),
            "imp_cost":          imp.get("cost"),
            # BL-23: exc-VAT import cost + rate, when the import channel carries them
            # (NULL otherwise — inc-VAT figures are unaffected).
            "imp_cost_exc":      imp.get("cost_exc"),
            # BL-9's EV split is not a column: it is the block's EV segments (step 5c).
            "imp_read_start":    imp.get("read_start"),
            "imp_read_end":      imp.get("read_end"),
            # export channel
            "exp_kwh":           exp.get("kwh"),
            "exp_rate":          exp.get("rate"),
            "exp_cost":          exp.get("cost"),
            "exp_read_start":    exp.get("read_start"),
            "exp_read_end":      exp.get("read_end"),
            # standing charge on main meter import
            "standing_charge":   float(meter_block.get("standing_charge") or 0),
            # BL-23 (4.2): exc-VAT standing + exc provenance (NULL until captured)
            # carbon footprint (NULL if no CI data available)
            "carbon_g":          meter_block.get("carbon_g"),
            # gCO2/kWh at block_start, stored at write time (3.0.0) — survives
            # carbon_intensity table pruning so PASS 3b re-run never re-queries it
            "carbon_intensity_g": meter_block.get("carbon_intensity_g"),
            # 3.0.0 Kraken state — preserved across append_block_replace so a
            # PASS 2 re-run (INSERT OR REPLACE) does not reset them. Defaults
            # keep pre-3.0.0 / CAD-mode blocks at 0/NULL.
            "imp_kwh_api":       meter_block.get("imp_kwh_api"),
            "exp_kwh_api":       meter_block.get("exp_kwh_api"),
            "is_provisional":    1 if meter_block.get("is_provisional") else 0,
            "needs_pass2_rerun": 1 if meter_block.get("needs_pass2_rerun") else 0,
            "needs_review":      1 if meter_block.get("needs_review") else 0,
            "source":            meter_block.get("source"),
            # 4.5.11: block pricing authority — preserved across round-trips (see _row_to_block).
            # A FRESH finalised block leaves these unset, so they default to 0 / NULL (uncorrected).
            "rate_corrected":    1 if meter_block.get("rate_corrected") else 0,
            "rate_source":       meter_block.get("price_rate_source"),
            # provisional: 1 if sub-meter was written without a post-boundary read
            "imp_provisional":   1 if meter_block.get("provisional") else 0,
        })
    return rows


def _row_to_block(rows: list[sqlite3.Row]) -> dict:
    """
    Reconstruct a block dict (matching the old blocks.json shape) from one or
    more DB rows that all share the same block_start.  The config fields
    (timezone, billing_day etc) are joined in from config_periods so callers
    don't need a separate lookup.
    """
    if not rows:
        return {}

    first = rows[0]
    block = {
        "start":       first["block_start"],
        "end":         first["block_end"],
        "interpolated": bool(first["interpolated"]),
        "meters":      {},
        "totals": {
            "import_kwh":  0.0,
            "import_cost": 0.0,
            "export_kwh":  0.0,
            "export_cost": 0.0,
        },
        # config fields joined from config_periods
        "_config_period_id": first["config_period_id"],
        "_effective_from":   first["effective_from"],
        "_billing_day":      first["billing_day"],
        "_block_minutes":    first["block_minutes"],
        "_timezone":         first["timezone"],
        "_currency_symbol":  first["currency_symbol"],
        "_currency_code":    first["currency_code"],
    }

    for row in rows:
        mid = row["meter_id"]
        imp_kwh  = row["imp_kwh"]  or 0.0
        imp_cost = row["imp_cost"] or 0.0
        exp_kwh  = row["exp_kwh"]  or 0.0
        exp_cost = row["exp_cost"] or 0.0

        # Build meta — include sub-meter flags from meters table if joined
        meta = {
            "block_minutes":  row["block_minutes"],
            "timezone":       row["timezone"],
            "billing_day":    row["billing_day"],
            "currency_symbol":row["currency_symbol"],
            "currency_code":  row["currency_code"],
        }
        try:
            if row["is_sub_meter"]:
                meta["sub_meter"] = True
            if row["parent_meter_id"]:
                meta["parent_meter"] = row["parent_meter_id"]
            if row["device_label"]:
                meta["device"] = row["device_label"]
            if row["inverter_possible"]:
                meta["inverter_possible"] = True
            if row["power_sensor"]:
                meta["power_sensor"] = row["power_sensor"]
            if row["postcode_prefix"]:
                meta["postcode_prefix"] = row["postcode_prefix"]
            if row["v2x_capable"]:
                meta["v2x_capable"] = True
            # v4 4.5.21 forward fix: meter_type is read on its own, ahead of the try below. It
            # sat inside it, after row["power_source"] — a column _select_blocks never selects —
            # so the IndexError skipped it and EVERY rebuilt block (settlement, device history
            # written in later, a device delete) lost it. _apply_pass2's EV-first grid priority
            # keys on it, so each re-split handed the EV's grid share to a bigger concurrent
            # device (a battery). Only meter_type moves: nothing else about a rebuilt block changes.
            if "meter_type" in row.keys() and row["meter_type"]:
                meta["meter_type"] = row["meter_type"]
            try:
                if row["power_source"]:
                    meta["power_source"] = row["power_source"]
                if row["rate_source"]:
                    meta["rate_source"] = row["rate_source"]
                if row["soc_sensor"]:
                    meta["soc_sensor"] = row["soc_sensor"]
                if row["inverter_power_sensor"]:
                    meta["inverter_power_sensor"] = row["inverter_power_sensor"]
                if row["inverter_power_invert"]:
                    meta["inverter_power_invert"] = True
                if "power_invert" in row.keys() and row["power_invert"]:
                    meta["power_invert"] = True
                if "device_power_invert" in row.keys() and row["device_power_invert"]:
                    meta["device_power_invert"] = True
                if row["device_power_sensor"]:
                    meta["device_power_sensor"] = row["device_power_sensor"]
                if row["retired_at"]:
                    meta["retired_at"] = row["retired_at"]
                if row["retired_reason"]:
                    meta["retired_reason"] = row["retired_reason"]
            except IndexError:
                pass  # older DB without these columns
        except IndexError:
            pass  # meters columns not present (e.g. get_last_block pre-join)

        meter_block = {
            "meta": meta,
            "interpolated":   bool(row["interpolated"]),
            "standing_charge": row["standing_charge"] or 0.0,
            "carbon_g":        row["carbon_g"],  # None when no CI data (pre-2.3.0)
            "channels":       {},
        }
        # Provenance tag (imported_api / imported_csv / recorder_attributed / …).
        # MUST be carried onto the dict: any read → modify → append_block_replace
        # round-trip (remainder recompute, carbon recompute, gap fill, device
        # attribution) writes meter_block.get("source") back — so if we drop it
        # here, the write silently re-inserts source = NULL and the block loses its
        # tag. This was the "carbon round-trip wiped imported_api to NULL" bug.
        try:
            if row["source"] is not None:
                meter_block["source"] = row["source"]
        except (IndexError, KeyError):
            pass
        # 3.0.0 columns — surfaced so the DCC re-run can read them. Guarded
        # with try/except for older rows / pre-join fetches lacking the columns.
        try:
            meter_block["carbon_intensity_g"] = row["carbon_intensity_g"]
        except (IndexError, KeyError):
            pass
        try:
            if row["imp_kwh_api"] is not None:
                meter_block["imp_kwh_api"] = row["imp_kwh_api"]
        except (IndexError, KeyError):
            pass
        try:
            if row["exp_kwh_api"] is not None:
                meter_block["exp_kwh_api"] = row["exp_kwh_api"]
        except (IndexError, KeyError):
            pass
        try:
            if row["is_provisional"]:
                meter_block["is_provisional"] = True
        except (IndexError, KeyError):
            pass
        # 4.5.11: preserve the block's pricing AUTHORITY across a read -> modify ->
        # append_block_replace round-trip. rate_corrected (a user Cost-Correction), rate_source
        # (schedule/settled/corrected) were NEVER carried, so any round-trip
        # (gap-fill, carbon/remainder recompute, device attribution, PASS 2 re-run) re-inserted
        # them at the column DEFAULTS -- silently un-protecting a manual correction, which the
        # dispatch reconcile then reverted (the negative-Home strand). Same class as the
        # source / imp_kwh_api drop above.
        try:
            if row["rate_corrected"]:
                meter_block["rate_corrected"] = int(row["rate_corrected"])
        except (IndexError, KeyError):
            pass
        try:
            if row["rate_source"] is not None:
                meter_block["price_rate_source"] = row["rate_source"]
        except (IndexError, KeyError):
            pass

        if row["imp_kwh"] is not None:
            imp_ch = {
                "kwh":        row["imp_kwh"],
                "rate":       row["imp_rate"],
                "cost":       row["imp_cost"],
                "read_start": row["imp_read_start"],
                "read_end":   row["imp_read_end"],
            }
            if row["imp_kwh_grid"] is not None:
                imp_ch["kwh_grid"] = row["imp_kwh_grid"]
            # BL-23 exc-VAT cost — surfaced (and thus preserved on round-trip rewrite).
            # The ex-VAT RATE is not loaded (step 5b, 10 Oct 2026): `rate_exc` on a channel
            # means "this pricing run derived it"; a loaded block's own ratio is
            # cost_exc / cost (exc_ratio_of). Guarded for pre-join fetches lacking the column.
            try:
                if row["imp_cost_exc"] is not None:
                    imp_ch["cost_exc"] = row["imp_cost_exc"]
            except (IndexError, KeyError):
                pass
            # BL-9 / step 5c (10 Oct 2026): the IOG house/EV split, from the block's segments
            # (_SPLIT_COLS) — the EV portion, and the bands each attribution was priced in
            # ('mixed' where its segments span two). The imp_*_ev columns are gone; a loader
            # that did not select _SPLIT_COLS gets no split keys.
            try:
                _sk, _sc = row["_split_kwh"], row["_split_cost"]
                _eb, _hb = row["_split_ev_band"], row["_split_home_band"]
            except (IndexError, KeyError):
                pass
            else:
                if _sk is not None and float(_sk) > 1e-9:
                    _sk, _sc = float(_sk), float(_sc or 0.0)
                    imp_ch["kwh_ev"] = round(_sk, 6)
                    imp_ch["cost_ev"] = round(_sc, 6)
                    imp_ch["rate_ev"] = round(_sc / _sk, 6)
                if _eb is not None:
                    imp_ch["ev_band"] = _eb
                if _hb is not None:
                    imp_ch["home_band"] = _hb
            # Expose provisional flag so the amendment path can identify blocks
            # written without a post-boundary sub-meter read.
            try:
                if row["imp_provisional"]:
                    meter_block["provisional"] = True
            except (IndexError, KeyError):
                pass
            meter_block["channels"]["import"] = imp_ch

        if row["exp_kwh"] is not None:
            meter_block["channels"]["export"] = {
                "kwh":        row["exp_kwh"],
                "rate":       row["exp_rate"],
                "cost":       row["exp_cost"],
                "read_start": row["exp_read_start"],
                "read_end":   row["exp_read_end"],
            }

        block["meters"][mid] = meter_block

        # Accumulate totals: the main meter's own import, as get_blocks_lightweight does.
        # Until step 5 (10 Oct 2026) the main contributed its stored remainder and each
        # device its grid share, which sums to the same figure; the remainder columns went.
        if not meter_block.get("meta", {}).get("sub_meter"):
            block["totals"]["import_kwh"]  += imp_kwh
            block["totals"]["import_cost"] += imp_cost

        block["totals"]["export_kwh"]  += exp_kwh
        block["totals"]["export_cost"] += exp_cost

    return block


def _rows_to_blocks(rows: list[sqlite3.Row]) -> list[dict]:
    """Group DB rows by block_start and reconstruct block dicts."""
    grouped: dict[str, list] = {}
    for row in rows:
        key = row["block_start"]
        grouped.setdefault(key, []).append(row)
    return [_row_to_block(group) for group in grouped.values()]


# ─────────────────────────────────────────────────────────────────────────────
# BlockStore
# ─────────────────────────────────────────────────────────────────────────────


def config_meta_significant(old_config: dict, new_config: dict) -> bool:
    """
    Return True if the billing-significant meta fields have changed between
    two config snapshots. Sensor entity IDs, power sensor, postcode etc are
    NOT significant — only fields that affect billing calculations are.
    """
    SIGNIFICANT = ("billing_day", "block_minutes", "timezone",
                   "currency_symbol", "currency_code")

    def _main_meta(cfg):
        for m in cfg.get("meters", {}).values():
            if not (m.get("meta") or {}).get("sub_meter"):
                return m.get("meta") or {}
        return {}

    old_meta = _main_meta(old_config)
    new_meta = _main_meta(new_config)

    for key in SIGNIFICANT:
        if old_meta.get(key) != new_meta.get(key):
            return True
    return False


SLOW_COMMIT_S = 1.0


class _TimedConnection(sqlite3.Connection):
    """A connection that reports any COMMIT slower than SLOW_COMMIT_S.

    Fourth Pi soak (30 Sep 2026): outside the chart render, the engine loop's 5-95 s
    stalls were commits — `with self._conn:` in set_block_segments,
    save_current_block, record_dispatch_history, _insert_block_rows — plus an
    os.fsync. A one-row commit taking seconds is not the work; it is the storage.
    The prime suspect is SQLite's automatic checkpoint, which runs INSIDE whichever
    commit pushes the WAL past 1,000 pages, on the committing thread — the engine's.

    This makes the question answerable: each slow commit logs the WAL size before
    and after. A WAL that SHRANK across it had a checkpoint run inside it.
    Costs two os.stat calls per commit; logs only when slow. `with self._conn:`
    commits through __exit__, not commit(), so both are covered."""
    _emt_wal = None

    def _wal_bytes(self):
        try:
            return os.path.getsize(self._emt_wal) if self._emt_wal else None
        except OSError:
            return 0

    def _report(self, t0, w0, caller):
        dt = time.monotonic() - t0
        if dt < SLOW_COMMIT_S:
            return
        w1 = self._wal_bytes()
        mb = (lambda b: "?" if b is None else "%.1f MB" % (b / 1048576.0))
        ckpt = w0 is not None and w1 is not None and w1 < w0
        logger.warning("slow commit: %.2f s in %s (WAL %s -> %s%s)", dt, caller, mb(w0),
                       mb(w1), " — a checkpoint ran in this commit" if ckpt else "")

    def commit(self):
        t0, w0 = time.monotonic(), self._wal_bytes()
        try:
            return super().commit()
        finally:
            self._report(t0, w0, sys._getframe(1).f_code.co_name)

    def __exit__(self, et, ev, tb):
        t0, w0 = time.monotonic(), self._wal_bytes()
        try:
            return super().__exit__(et, ev, tb)
        finally:
            if et is None:
                self._report(t0, w0, sys._getframe(1).f_code.co_name)

def _block_floor(iso: str, step: int) -> str:
    """The block boundary at or before naive-UTC `iso`, for blocks of `step` minutes (dividing an
    hour): '2026-07-14T23:29:00' -> '2026-07-14T23:00:00' for 30-minute blocks."""
    dt = datetime.fromisoformat(iso).replace(second=0, microsecond=0)
    m = dt.hour * 60 + dt.minute
    m -= m % step
    return dt.replace(hour=m // 60, minute=m % 60).isoformat()


def _block_ceil(iso: str, step: int) -> str:
    """The block boundary at or after naive-UTC `iso` (itself when it is one)."""
    dt = datetime.fromisoformat(iso)
    fl = datetime.fromisoformat(_block_floor(iso, step))
    return fl.isoformat() if fl == dt else (fl + timedelta(minutes=step)).isoformat()


class BlockStore:
    """
    SQLite-backed block store.

    Thread safety: each instance holds one connection.  The engine runs on a
    single thread; the web server should open its own instance (SQLite WAL
    mode allows concurrent readers alongside one writer).
    """

    def __init__(self, db_path: str, *, read_only: bool = False):
        self._path = db_path
        self._read_only = read_only
        # `uri=True` for a `file:...` path so a NAMED shared-cache in-memory
        # database can be opened by more than one connection — the fixture story
        # for the ReadStore/WriteStore split (A §7 q2). `":memory:"` is anonymous:
        # a second connection to it is a different, empty database, which is why
        # v4's read companion had to fall back to the shared handle.
        # check_same_thread is left at its DEFAULT (True). It was False from the
        # beginning of v4 and stayed until workstream A acceptance 5 (24 Sep
        # 2026), because connections travelled: engine.py held 57 raw `_conn`
        # sites, and three background workers in web/server.py wrote through the
        # shared web store on their own threads.
        #
        # Now nothing shares a connection across threads — the engine's store is
        # engine-thread-only, `store()` is per-thread, and each worker opens its
        # own via `server._own_store()`. So the check can do its job: a
        # cross-thread use is an immediate, loud error at the call that makes it,
        # rather than the class of corruption engine.py describes —
        # *"a DB write from the executor thread races the loop-thread writers on
        # that one connection and corrupts it"*.
        #
        # Guarded by tests/test_thread_affinity.py. Do not pass it again.
        self._conn = sqlite3.connect(db_path,
                                     uri=str(db_path).startswith("file:"),
                                     factory=_TimedConnection)
        _p = str(db_path)
        self._conn._emt_wal = (_p + "-wal") if (_p != ":memory:"
                                                 and not _p.startswith("file:")) else None
        self._conn.row_factory = sqlite3.Row
        if read_only:
            # Lightweight read-only companion (e.g. off-loop chart rendering):
            # NO schema/migration DDL, guarded by query_only so it can never
            # contend with the primary writer. Safe to use from a worker thread
            # while the engine writes on its own WAL connection (the db is
            # already WAL, so this connection reads the shared WAL snapshot).
            try:
                self._conn.execute("PRAGMA busy_timeout=5000")
                self._conn.execute("PRAGMA query_only=ON")
            except Exception:
                pass
            return
        self._apply_pragmas()
        self._ensure_schema()
        # Covering index for insights aggregation — created after migrations
        # so carbon_g and other late-added columns are guaranteed to exist
        # Covering index for all insights/summary queries — avoids row fetches.
        # Includes all columns used by _aggregate_insights, _aggregate_usage,
        # and the Usage Stats rows (usage_view). Rebuilt if schema changes (DROP + CREATE).
        _insights_idx_sql = (
            "CREATE INDEX IF NOT EXISTS idx_blocks_insights "
            "ON blocks (block_start, meter_id, config_period_id, "
            "imp_kwh, imp_kwh_grid, "
            "imp_rate, imp_cost, "
            "exp_kwh, exp_cost, "
            "standing_charge, carbon_g)"
        )
        try:
            existing = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_blocks_insights'"
            ).fetchone()
            if existing and existing[0] and ("standing_charge" not in existing[0]
                                             or "imp_kwh_remainder" in existing[0]):
                # Old narrow index, or one covering the remainder columns that step 5
                # dropped (10 Oct 2026) — drop and recreate with the current covering set
                with self._conn:
                    self._conn.execute("DROP INDEX IF EXISTS idx_blocks_insights")
            with self._conn:
                self._conn.execute(_insights_idx_sql)
        except Exception:
            pass
        # 2.8.0 — drop timezone-baked local date columns (now computed at query time)
        # Must drop indexes first — SQLite won't drop a column used in an index
        for _drop_idx in ("idx_blocks_date", "idx_blocks_ym"):
            try:
                with self._conn:
                    self._conn.execute(f"DROP INDEX IF EXISTS {_drop_idx}")
            except Exception:
                pass
        for _drop_col in ("local_date", "local_year", "local_month", "local_day"):
            try:
                cols = [r["name"] for r in self._conn.execute(
                    "PRAGMA table_info(blocks)"
                ).fetchall()]
                if _drop_col in cols:
                    with self._conn:
                        self._conn.execute(f"ALTER TABLE blocks DROP COLUMN {_drop_col}")
            except Exception as _e:
                pass  # SQLite < 3.35 or column already dropped
        # Migrate carbon_intensity: rename intensity → intensity_forecast, add intensity_actual
        try:
            cols = [r["name"] for r in self._conn.execute(
                "PRAGMA table_info(carbon_intensity)"
            ).fetchall()]
            if "intensity" in cols and "intensity_forecast" not in cols:
                with self._conn:
                    self._conn.execute(
                        "ALTER TABLE carbon_intensity RENAME COLUMN intensity TO intensity_forecast"
                    )
            if "intensity_actual" not in cols and "intensity_forecast" in cols + ["intensity_forecast"]:
                with self._conn:
                    self._conn.execute(
                        "ALTER TABLE carbon_intensity ADD COLUMN intensity_actual REAL"
                    )
        except Exception:
            pass

        # 2.8.0 — remove redundant generation_mix rows stored against sub-meter blocks.
        # Mix is a grid property; only the main meter block needs it.
        try:
            with self._conn:
                self._conn.execute("""
                    DELETE FROM generation_mix
                    WHERE block_id IN (
                        SELECT id FROM blocks WHERE meter_id != 'electricity_main'
                    )
                """)
        except Exception:
            pass

        try:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS generation_mix (
                    block_id INTEGER NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
                    fuel     TEXT    NOT NULL,
                    perc     REAL    NOT NULL,
                    PRIMARY KEY (block_id, fuel)
                )
            """)
            # 2.8.0 — create mix_history for CI-tick resolution mix (independent of block size)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS mix_history (
                    captured_at TEXT NOT NULL,
                    fuel        TEXT NOT NULL,
                    perc        REAL NOT NULL,
                    PRIMARY KEY (captured_at, fuel)
                )
            """)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mix_history_time ON mix_history (captured_at)"
            )
            # Backfill mix_history from generation_mix for existing users upgrading
            filled = self._conn.execute("SELECT COUNT(*) FROM mix_history").fetchone()[0]
            if filled == 0:
                self._conn.execute("""
                    INSERT OR IGNORE INTO mix_history (captured_at, fuel, perc)
                    SELECT b.block_start, gm.fuel, gm.perc
                    FROM generation_mix gm
                    JOIN blocks b ON b.id = gm.block_id
                    WHERE b.meter_id = 'electricity_main'
                      AND b.block_start >= datetime('now', '-48 hours')
                """)
        except Exception:
            pass
        # CRITICAL (fresh-DB lock): the blocks above (schema executescript, the
        # mix_history CREATE/INSERT) can leave an open transaction on the
        # connection. A lingering transaction makes the engine's startup
        # wal_checkpoint(TRUNCATE) fail with "database table is locked" (same
        # root cause as the 2.x 'fresh install hang' — executescript leaving an
        # open read txn on a fresh WAL DB). Commit now so the connection is in a
        # clean autocommit state before any checkpoint/backup runs.
        try:
            self._conn.commit()
        except Exception:
            pass
        logger.debug("BlockStore opened: %s", db_path)

    # ── Connection management ─────────────────────────────────────────────

    def _apply_pragmas(self) -> None:
        # busy_timeout is critical: EMT runs TWO connections (engine loop + web
        # server) against one WAL database. Without it, any momentary write
        # contention (a config save, a checkpoint, a block finalise) makes the
        # other connection fail INSTANTLY with "database is locked" instead of
        # waiting. 5s lets brief contention resolve transparently. This was the
        # cause of the every-tick "database is locked" loop errors on fresh DBs.
        #
        # journal_size_limit bounds the WAL's RECOVERY, not its growth. Measured:
        # with no concurrent reader, SQLite's own wal_autocheckpoint (1000 pages
        # = ~4 MB at a 4096-byte page) holds the WAL at 4.1 MB over 3,000
        # committed writes and it stays there indefinitely. A reader holding an
        # open transaction is the ONLY thing that breaks that -- a checkpoint
        # cannot reclaim pages a reader still needs -- and the same 3,000 writes
        # then take the WAL to 24.8 MB.
        #
        # Today that inflation is PERMANENT: once the reader goes away the WAL is
        # reused in place, never shrunk, so a single unlucky coincidence leaves
        # 25 MB on disk until the next restart -- and this add-on can run for
        # months. With the limit set, the next completed checkpoint truncates it
        # back (measured: 24.8 MB -> 4.2 MB, unattended).
        #
        # It cannot lose data. The truncation happens AFTER a checkpoint has
        # copied the WAL's contents into the main database; at that instant the
        # WAL is empty scratch space. If a checkpoint cannot complete, nothing is
        # truncated -- exactly today's behaviour.
        #
        # 4 MB matches the autocheckpoint threshold, so the file returns to where
        # normal running already keeps it rather than churning.
        self._conn.executescript("""
            PRAGMA busy_timeout        = 5000;
            PRAGMA journal_mode        = WAL;
            PRAGMA synchronous         = NORMAL;
            PRAGMA cache_size          = -8000;
            PRAGMA temp_store          = MEMORY;
            PRAGMA foreign_keys        = ON;
            PRAGMA journal_size_limit  = 4194304;
        """)

    def _ensure_schema(self) -> None:
        # Brand new: no tables yet. Only then is this database v5's own (EMT_SCHEMA); a
        # v4-shaped database opened here keeps no marker, so a restore still imports it.
        _new = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'blocks'").fetchone() is None
        self._conn.executescript(_DDL)
        # `reads` (v4 2.0.0's raw-sample table, never written; retired 20 Oct 2026): dropped from
        # v5's own database. A v4-shaped one keeps it — the importer drops it (rules.DROP_TABLES).
        if not _new and self._conn.execute(
                "SELECT 1 FROM store_meta WHERE key = 'emt_schema'").fetchone():
            self._conn.execute("DROP TABLE IF EXISTS reads")
        if _new:
            with self._conn:
                self._conn.execute("INSERT OR IGNORE INTO store_meta (key, value) VALUES "
                                   "('emt_schema', ?)", (json.dumps(EMT_SCHEMA),))

        # ── Incremental column additions ──────────────────────────────────────
        # These run on every open so new columns are available immediately,
        # even before migrate_full_config_json() runs. ALTER TABLE IF NOT EXISTS
        # is not supported in SQLite < 3.37 so we check PRAGMA first.
        _m_cols  = {r[1] for r in self._conn.execute("PRAGMA table_info(meters)").fetchall()}
        _cp_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(config_periods)").fetchall()}
        _mc_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(meter_channels)").fetchall()}

        _b_cols  = {r[1] for r in self._conn.execute("PRAGMA table_info(blocks)").fetchall()}
        _ds_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(dispatch_slots)").fetchall()} if self._conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='dispatch_slots'").fetchone() else set()
        _dh_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(dispatch_history)").fetchall()} if self._conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='dispatch_history'").fetchone() else set()
        _ph_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(power_history)").fetchall()} if self._conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='power_history'").fetchone() else set()
        _mc_cost_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(measured_cost)").fetchall()} if self._conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='measured_cost'").fetchone() else set()

        for _col, _tbl, _defn, _col_set in [
            ("protected",            "meters",         "INTEGER DEFAULT 0",  _m_cols),
            ("inverter_possible",    "meters",         "INTEGER DEFAULT 0",  _m_cols),
            ("power_sensor",         "meters",         "TEXT",               _m_cols),
            ("power_source",         "meters",         "TEXT",               _m_cols),
            ("rate_source",          "meters",         "TEXT",               _m_cols),
            ("postcode_prefix",      "meters",         "TEXT",               _m_cols),
            ("postcode_source",      "meters",         "TEXT",               _m_cols),
            ("v2x_capable",          "meters",         "INTEGER DEFAULT 0",  _m_cols),
            ("inverter_power_invert","meters",         "INTEGER DEFAULT 0",  _m_cols),
            ("power_invert",          "meters",         "INTEGER DEFAULT 0",  _m_cols),
            ("device_power_invert",    "meters",         "INTEGER DEFAULT 0",  _m_cols),
            ("meter_type",           "meters",         "TEXT",               _m_cols),
            ("soc_sensor",           "meters",         "TEXT",               _m_cols),
            ("inverter_power_sensor","meters",         "TEXT",               _m_cols),
            ("device_power_sensor",  "meters",         "TEXT",               _m_cols),
            ("pv_power_sensor",      "meters",         "TEXT",               _m_cols),
            ("retired_at",            "meters",         "TEXT",               _m_cols),
            ("retired_reason",        "meters",         "TEXT",               _m_cols),
            ("supplier",              "config_periods",  "TEXT",              _cp_cols),
            ("mpan",             "meter_channels",  "TEXT",              _mc_cols),
            ("tariff",           "meter_channels",  "TEXT",              _mc_cols),
            # ── 3.0.0 per-channel rate/standing-charge source (API vs sensor) ─
            ("rate_source",            "meter_channels",  "TEXT",        _mc_cols),
            ("standing_charge_source", "meter_channels",  "TEXT",        _mc_cols),
            ("carbon_g",         "blocks",          "REAL",              _b_cols),
            ("imp_provisional",  "blocks",          "INTEGER NOT NULL DEFAULT 0", _b_cols),
            # ── 3.0.0 Kraken API integration columns (upgrade path) ──────────
            ("source",             "blocks",        "TEXT",                       _b_cols),
            ("is_provisional",     "blocks",        "INTEGER NOT NULL DEFAULT 0", _b_cols),
            ("needs_pass2_rerun",  "blocks",        "INTEGER NOT NULL DEFAULT 0", _b_cols),
            ("imp_kwh_api",        "blocks",        "REAL",                       _b_cols),
            ("exp_kwh_api",        "blocks",        "REAL",                       _b_cols),
            ("finalised_from_cad", "blocks",        "INTEGER NOT NULL DEFAULT 0", _b_cols),
            ("needs_review",       "blocks",        "INTEGER NOT NULL DEFAULT 0", _b_cols),
            # ── 3.3.0 BL-18: why a block was flagged for review (drift vs dispatch)
            ("review_reason",      "blocks",        "TEXT",                       _b_cols),
            # ── #322: sticky "user dismissed this review" marker. Cleared review flags
            # were re-set every reconcile run (no memory of the dismissal), so the same
            # ambiguous dispatch blocks kept reappearing. Once dismissed, don't re-flag.
            ("review_dismissed",   "blocks",        "INTEGER NOT NULL DEFAULT 0", _b_cols),
            ("carbon_intensity_g", "blocks",        "REAL",                       _b_cols),
            ("rate_corrected",     "blocks",        "INTEGER NOT NULL DEFAULT 0", _b_cols),
            # ── BL-53: rate provenance / authority (schedule<reconciled<measured<corrected).
            # A lower authority never overwrites a higher one; the sweep skips measured/corrected.
            ("rate_source",        "blocks",        "TEXT",                       _b_cols),
            # ── 3.5.0 historical-import: link a reconstructed block to its derivation
            ("derivation_id",      "blocks",        "INTEGER",                    _b_cols),
            # ── 4.1.x BL-23: exc-VAT import cost (additive; NULL until captured)
            ("imp_cost_exc",       "blocks",        "REAL",                       _b_cols),
            # ── device sensors design §7: the entry that recorded a live block (19 Oct 2026)
            ("imp_sensor_id",      "blocks",        "INTEGER",                    _b_cols),
            ("exp_sensor_id",      "blocks",        "INTEGER",                    _b_cols),
            # (4.2's ex-VAT rate/standing/provenance and 4.3's BL-9 EV-split columns were
            # dropped in step 5, 10 Oct 2026: derived where read / the EV segments.)
            ("carbon_gco2_min",  "power_history",   "REAL",              _ph_cols),
            # ── 3.1.x dispatch lifecycle capture (observe-only, no billing effect)
            ("state",            "dispatch_slots", "TEXT",  _ds_cols),
            ("energy_planned",   "dispatch_slots", "REAL",  _ds_cols),
            ("energy_completed", "dispatch_slots", "REAL",  _ds_cols),
            # ── 3.4.0 BL-11: retain the exact (second-precision) dispatch window
            ("raw_start",        "dispatch_slots",   "TEXT",  _ds_cols),
            ("raw_end",          "dispatch_slots",   "TEXT",  _ds_cols),
            ("raw_start",        "dispatch_history", "TEXT",  _dh_cols),
            ("raw_end",          "dispatch_history", "TEXT",  _dh_cols),
            # C2 (4.5.7): four-bucket settled Home/EV split folded into measured_cost.
            ("home_kwh",         "measured_cost",  "REAL",  _mc_cost_cols),
            ("home_rate",        "measured_cost",  "REAL",  _mc_cost_cols),
            ("ev_kwh",           "measured_cost",  "REAL",  _mc_cost_cols),
            ("ev_rate",          "measured_cost",  "REAL",  _mc_cost_cols),
        ]:
            if _col not in _col_set:
                try:
                    self._conn.execute(f"ALTER TABLE {_tbl} ADD COLUMN {_col} {_defn}")
                    self._conn.commit()
                except Exception:
                    pass  # already exists or table missing — migrate will handle it

        # ── BL-53: backfill rate_source from the existing flags (pure derivation,
        # no data invented). corrected > reconciled > schedule. One-shot: only
        # rows still NULL, so a re-run is a no-op and a measured row (set later) is
        # never downgraded.
        try:
            if "rate_source" in _b_cols:
                self._conn.execute(
                    "UPDATE blocks SET rate_source = CASE "
                    "  WHEN rate_corrected = 1 THEN 'corrected' "
                    "  WHEN imp_rate IS NOT NULL THEN 'schedule' END "
                    "WHERE rate_source IS NULL")
                self._conn.commit()
        except Exception:
            pass

        # ── 3.5.x: tag existing (hand-entered) postcode prefixes as user-sourced ──
        # A pre-existing postcode_prefix was typed in Settings, so record its
        # provenance as 'user'. A one-time upgrade probe (engine) may then
        # overwrite it from the authoritative Octopus supply address — outward
        # code only — so a legacy wrong-region entry self-corrects.
        try:
            self._conn.execute(
                "UPDATE meters SET postcode_source = 'user' "
                "WHERE postcode_prefix IS NOT NULL AND postcode_source IS NULL")
            self._conn.commit()
        except Exception:
            pass  # column not present yet on a very old shape — migrate handles it

        # ── 3.0.0: backfill per-channel rate / standing-charge source ─────────
        # Pre-3.0.0 (v2) configs predate the explicit "API vs sensor" toggles.
        # Default every existing channel to the source it was ALREADY using, so an
        # upgrade does not change a single price until the user opts in:
        #   • sub-meter (device) channel → derived from the meter's legacy
        #     rate_source AND its own sensor: an explicit own-sensor choice
        #     ('own'/'base'/'sensor') OR a device that has its own rate sensor
        #     mapped becomes 'sensor'; anything else (incl. 'overlay'/'inherit'/
        #     NULL with no own sensor) becomes 'main' — i.e. inherit the main
        #     meter's effective rate, as v2 sub-meters did via parent_rates.
        #   • main-meter channel → 'sensor' when a rate sensor is actually mapped,
        #     otherwise 'api' (the rate must have been coming from the supplier
        #     API). We never silently flip a sensor-fed meter onto the API.
        #   • standing charge → 'sensor' when a standing-charge sensor is mapped,
        #     otherwise 'api'.
        # Only NULL rows are touched, so explicit UI/wizard choices are preserved.
        try:
            _mc_now = [r[1] for r in self._conn.execute("PRAGMA table_info(meter_channels)").fetchall()]
            if "rate_source" in _mc_now:
                n1 = self._conn.execute(
                    """UPDATE meter_channels SET rate_source = (
                           SELECT CASE
                               WHEN m.is_sub_meter = 1 THEN
                                   CASE WHEN m.rate_source IN ('own','base','sensor')
                                        THEN 'sensor'
                                        WHEN meter_channels.rate_sensor IS NOT NULL
                                             AND TRIM(meter_channels.rate_sensor) <> ''
                                        THEN 'sensor'
                                        ELSE 'main' END
                               ELSE
                                   CASE WHEN meter_channels.rate_sensor IS NOT NULL
                                             AND TRIM(meter_channels.rate_sensor) <> ''
                                        THEN 'sensor' ELSE 'api' END
                           END
                           FROM meters m WHERE m.id = meter_channels.meter_id)
                       WHERE rate_source IS NULL"""
                ).rowcount
                n2 = self._conn.execute(
                    """UPDATE meter_channels SET standing_charge_source =
                           CASE WHEN standing_charge_sensor IS NOT NULL
                                     AND TRIM(standing_charge_sensor) <> ''
                                THEN 'sensor' ELSE 'api' END
                       WHERE standing_charge_source IS NULL"""
                ).rowcount
                if n1 or n2:
                    self._conn.commit()
                    logger.info(
                        "_ensure_schema: backfilled channel sources "
                        "(rate_source=%d, standing_charge_source=%d) for v2 upgrade",
                        n1, n2,
                    )
        except Exception:
            logger.debug("_ensure_schema: rate-source backfill skipped", exc_info=True)

        # Clear inverter_possible for all existing meters — feature removed in 2.9.0
        # All sub-meters now use the protected queue in PASS 2 regardless of this flag
        try:
            if "inverter_possible" in [r[1] for r in self._conn.execute(
                "PRAGMA table_info(meters)"
            ).fetchall()]:
                n = self._conn.execute(
                    "UPDATE meters SET inverter_possible=0 WHERE inverter_possible=1"
                ).rowcount
                if n:
                    self._conn.commit()
                    logger.info("_ensure_schema: cleared %d inverter_possible flag(s) (deprecated)", n)
        except Exception:
            pass

        # Create is_gap_seed index only if column exists (deferred for upgrade compat)
        cr_cols = [r[1] for r in self._conn.execute(
            "PRAGMA table_info(current_reads)"
        ).fetchall()]
        if "is_gap_seed" in cr_cols:
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_current_reads_gap "
                "ON current_reads (is_gap_seed)"
            )

        # ── 3.0.0 Kraken integration: duplicate detection + partial indexes ───
        # The blocks table has carried a table-level UNIQUE(block_start, meter_id)
        # constraint since its inception, so genuine duplicates should not exist.
        # We still check defensively before relying on the named unique index,
        # and deduplicate (keeping the highest id) if any are somehow present —
        # e.g. a DB hand-restored from a pre-constraint export.
        try:
            dupes = self._conn.execute(
                """SELECT block_start, meter_id, COUNT(*) AS n
                   FROM blocks
                   GROUP BY block_start, meter_id
                   HAVING n > 1"""
            ).fetchall()
            if dupes:
                for d in dupes:
                    self._conn.execute(
                        """DELETE FROM blocks
                           WHERE block_start = ? AND meter_id = ?
                             AND id < (SELECT MAX(id) FROM blocks
                                       WHERE block_start = ? AND meter_id = ?)""",
                        (d["block_start"], d["meter_id"],
                         d["block_start"], d["meter_id"]),
                    )
                    logger.warning(
                        "_ensure_schema: deduplicated block_start=%s meter_id=%s "
                        "(%d duplicate row(s) removed, kept highest id)",
                        d["block_start"], d["meter_id"], d["n"] - 1,
                    )
                self._conn.commit()
        except Exception:
            logger.exception("_ensure_schema: duplicate detection failed (non-fatal)")

        # Partial indexes for the Kraken sweeps. Created here (not in _DDL) because
        # they reference columns that only exist after the ALTER loop above on an
        # upgrade DB. IF NOT EXISTS makes this idempotent on fresh installs too.
        _b_cols_now = {r[1] for r in self._conn.execute("PRAGMA table_info(blocks)").fetchall()}
        if {"is_provisional", "needs_pass2_rerun", "needs_review"} <= _b_cols_now:
            self._conn.executescript(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_blocks_start_meter "
                "    ON blocks (block_start, meter_id);"
                "CREATE INDEX IF NOT EXISTS idx_blocks_provisional "
                "    ON blocks (is_provisional)    WHERE is_provisional = 1;"
                "CREATE INDEX IF NOT EXISTS idx_blocks_pass2_rerun "
                "    ON blocks (needs_pass2_rerun) WHERE needs_pass2_rerun = 1;"
                "CREATE INDEX IF NOT EXISTS idx_blocks_needs_review "
                "    ON blocks (needs_review)      WHERE needs_review = 1;"
            )
            self._conn.commit()

        cur = self._conn.execute(
            "SELECT value FROM store_meta WHERE key = 'schema_version'"
        )
        row = cur.fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO store_meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),)
            )
            self._conn.commit()

    # ── Carbon intensity ─────────────────────────────────────────────────────

    def upsert_carbon_intensity(self, captured_at: str, postcode: str,
                                 intensity_forecast: float | None,
                                 ci_index: str | None,
                                 intensity_actual: float | None = None) -> None:
        """
        Store a carbon intensity sample. captured_at is the 30-min slot boundary.
        intensity_forecast is always provided. intensity_actual is populated ~24hr
        later when NESO publishes actuals — only updates if provided (not None).
        """
        with self._conn:
            self._upsert_carbon_intensity_row(captured_at, postcode, intensity_forecast,
                                              ci_index, intensity_actual)

    def upsert_carbon_slots(self, postcode: str, slots) -> int:
        """A carbon-intensity fetch's slots, and their generation mixes, in ONE
        transaction (#65, 3 Oct 2026). The same statements as upsert_carbon_intensity
        and upsert_mix_history: the tick wrote ~96 slots and ~96 mixes with a commit
        each, ~190 fsyncs on the Pi's SD card, on the engine loop. Each slot is a dict
        with captured_at, intensity_forecast, ci_index, intensity_actual and an
        optional generationmix. Returns the slots written."""
        n = 0
        with self._conn:
            for slot in slots:
                self._upsert_carbon_intensity_row(
                    slot["captured_at"], postcode, slot["intensity_forecast"],
                    slot["ci_index"], slot["intensity_actual"])
                if slot.get("generationmix"):
                    self._upsert_mix_rows(slot["captured_at"], slot["generationmix"])
                n += 1
        return n

    def _upsert_carbon_intensity_row(self, captured_at, postcode, intensity_forecast,
                                     ci_index, intensity_actual) -> None:
        """upsert_carbon_intensity's statement, inside the caller's transaction."""
        if intensity_actual is not None:
            self._conn.execute(
                """INSERT INTO carbon_intensity
                       (captured_at, postcode, intensity_forecast, intensity_actual, ci_index)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(captured_at, postcode) DO UPDATE SET
                       intensity_forecast = COALESCE(excluded.intensity_forecast, intensity_forecast),
                       intensity_actual   = excluded.intensity_actual,
                       ci_index           = excluded.ci_index""",
                (captured_at, postcode, intensity_forecast, intensity_actual, ci_index)
            )
        else:
            self._conn.execute(
                """INSERT INTO carbon_intensity
                       (captured_at, postcode, intensity_forecast, ci_index)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(captured_at, postcode) DO UPDATE SET
                       intensity_forecast = excluded.intensity_forecast,
                       ci_index           = excluded.ci_index""",
                (captured_at, postcode, intensity_forecast, ci_index)
            )

    def get_nearest_carbon_intensity(self, block_start: str, postcode: str) -> dict | None:
        """
        Return the nearest carbon_intensity row to block_start for the given postcode.
        intensity = COALESCE(actual, forecast) — actual preferred when available.
        """
        row = self._conn.execute(
            """SELECT captured_at, ci_index,
                      COALESCE(intensity_actual, intensity_forecast) as intensity,
                      intensity_forecast,
                      intensity_actual
               FROM carbon_intensity
               WHERE postcode = ?
               ORDER BY ABS(strftime('%s', captured_at) - strftime('%s', ?))
               LIMIT 1""",
            (postcode, block_start)
        ).fetchone()
        return dict(row) if row else None

    def get_block_starts_missing_carbon(self, limit: Optional[int] = None) -> list:
        """Distinct block_start values for main-meter blocks whose carbon was
        never attributed (carbon_intensity_g IS NULL but the block has energy).

        Used by the carbon recovery sweep: blocks left NULL by an outage gap-fill
        that ran before a CI slot was available. Ordered oldest-first so the most
        recently-recoverable (still within the 4-day CI window) are handled.
        """
        sql = (
            "SELECT DISTINCT block_start FROM blocks "
            "WHERE carbon_intensity_g IS NULL "
            "  AND (imp_kwh IS NOT NULL OR exp_kwh IS NOT NULL) "
            # 3.5.0: skip reconstructed history — carbon for years-old imported
            # blocks is low value and its bulk backfill stalls the engine loop.
            "  AND (source IS NULL OR source NOT LIKE 'imported%') "
            "ORDER BY block_start"
        )
        if limit is not None:
            sql += " LIMIT ?"
            return [r["block_start"]
                    for r in self._conn.execute(sql, (limit,)).fetchall()]
        return [r["block_start"]
                for r in self._conn.execute(sql).fetchall()]

    # Region-eligibility for carbon backfill: a live/reconstructed block is always
    # eligible; an IMPORTED block is eligible only once its config period has a
    # known region (postcode_prefix). Imported blocks without a region stay NULL
    # (excluded) rather than being attributed to a guessed region.
    _CARBON_ELIGIBLE = (
        "((blocks.source IS NULL OR blocks.source NOT LIKE 'imported%') "
        " OR EXISTS (SELECT 1 FROM meters m "
        "            WHERE m.config_period_id = blocks.config_period_id "
        "              AND m.is_sub_meter = 0 "
        "              AND m.postcode_prefix IS NOT NULL))"
    )

    # Reconstructed device attribution from the HA recorder. A distinct source so the
    # reconstructed device layer is identifiable and removable WITHOUT touching live
    # or imported house totals — the reversibility contract (see attribution design).
    RECORDER_ATTRIBUTED_SOURCE = "recorder_attributed"
    _ATTRIBUTION_RUNS_KEY = "recorder_attribution_runs"   # store_meta ledger

    def get_missing_carbon_date_range(self) -> tuple | None:
        """(min_block_start, max_block_start) over energy-bearing blocks whose
        carbon_intensity_g IS NULL AND that are region-eligible (see
        _CARBON_ELIGIBLE). None when there are no such blocks.

        Bounds the historical carbon backfill: every block before CI first became
        available carries a NULL intensity; imported history joins the span once
        its period's region is known."""
        row = self._conn.execute(
            "SELECT MIN(block_start) AS lo, MAX(block_start) AS hi FROM blocks "
            "WHERE carbon_intensity_g IS NULL "
            "  AND (imp_kwh IS NOT NULL OR exp_kwh IS NOT NULL) "
            "  AND " + self._CARBON_ELIGIBLE
        ).fetchone()
        if row and row["lo"] and row["hi"]:
            return (row["lo"], row["hi"])
        return None

    def get_block_starts_missing_carbon_in_range(
        self, from_iso: str, to_iso: str, limit: Optional[int] = None
    ) -> list:
        """Distinct NULL-carbon, region-eligible block_start values within
        [from_iso, to_iso), oldest-first. Used per fetched window by the
        historical backfill."""
        sql = (
            "SELECT DISTINCT block_start FROM blocks "
            "WHERE carbon_intensity_g IS NULL "
            "  AND (imp_kwh IS NOT NULL OR exp_kwh IS NOT NULL) "
            "  AND " + self._CARBON_ELIGIBLE + " "
            "  AND block_start >= ? AND block_start < ? "
            "ORDER BY block_start"
        )
        params: list = [from_iso, to_iso]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [r["block_start"]
                for r in self._conn.execute(sql, params).fetchall()]

    def extend_earliest_period_to(self, block_start_iso: str) -> bool:
        """Move the earliest config period's `effective_from` back to cover
        imported history that predates it (snapped to local midnight). ONLY ever
        extends backward — never shrinks. Keeps imported blocks inside a period
        that temporally covers them and makes Billing history show the real data
        start rather than the go-live date. Returns True if it changed anything."""
        if not block_start_iso:
            return False
        row = self._conn.execute(
            "SELECT id, effective_from, timezone FROM config_periods "
            "ORDER BY effective_from ASC LIMIT 1"
        ).fetchone()
        if not row:
            return False
        snapped = self._snap_to_midnight_utc(block_start_iso, row["timezone"] or "UTC")
        if snapped >= row["effective_from"]:
            return False   # already covers it (or would shrink) — no-op
        with self._conn:
            self._conn.execute(
                "UPDATE config_periods SET effective_from = ? WHERE id = ?",
                (snapped, row["id"]))
        logger.info("extend_earliest_period_to: period %d effective_from %s -> %s",
                    row["id"], row["effective_from"], snapped)
        return True

    def rearm_carbon_backfill(self) -> None:
        """Clear the historical-carbon backfill marker so the next scheduler tick
        re-scans from scratch. Called when a region is newly assigned to a period
        (reconcile / manual edit / one-time probe), which makes previously
        region-unknown imported blocks eligible for carbon."""
        self.set_meta("carbon_backfill_state", {})

    def prune_carbon_intensity(self, days: int = 4) -> int:
        """Delete carbon_intensity rows older than `days` days. Returns rows deleted."""
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
                  - timedelta(days=days)).isoformat()
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM carbon_intensity WHERE captured_at < ?", (cutoff,)
            )
        return cur.rowcount

    # ── 3.1.0 Intelligent dispatch slots ────────────────────────────────────────

    # ── BL-53: measured-cost cache (Octopus billed per-slot cost/label) ─────
    def get_measured_cost(self, slot_start: str, mpan: str = "",
                          direction: str = "CONSUMPTION") -> "dict | None":
        row = self._conn.execute(
            "SELECT slot_start, mpan, direction, cost_incl, cost_excl, label, kwh, "
            "fetched_at FROM measured_cost WHERE slot_start=? AND mpan=? AND direction=?",
            (slot_start, mpan or "", direction)).fetchone()
        return dict(row) if row else None

    def upsert_measured_cost(self, slot_start: str, *, mpan: str = "",
                             direction: str = "CONSUMPTION", cost_incl=None,
                             cost_excl=None, label=None, kwh=None,
                             fetched_at: str = "") -> None:
        self._conn.execute(
            "INSERT INTO measured_cost (slot_start, mpan, direction, cost_incl, "
            "cost_excl, label, kwh, fetched_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(slot_start, mpan, direction) DO UPDATE SET "
            "cost_incl=excluded.cost_incl, cost_excl=excluded.cost_excl, "
            "label=excluded.label, kwh=excluded.kwh, fetched_at=excluded.fetched_at",
            (slot_start, mpan or "", direction, cost_incl, cost_excl, label, kwh,
             fetched_at or _utc_now_iso()))
        self._conn.commit()

    def measured_costs_since(self, mpan: str, since: str,
                             direction: str = "CONSUMPTION") -> list:
        """(slot_start, cost_incl, cost_excl) of every cached measured cost at or after `since`,
        oldest first — the period the VAT resync (v4 #505) checks and re-fetches."""
        return [tuple(r) for r in self._conn.execute(
            "SELECT slot_start, cost_incl, cost_excl FROM measured_cost WHERE mpan = ? AND "
            "direction = ? AND slot_start >= ? ORDER BY slot_start",
            (mpan or "", direction, since)).fetchall()]

    def measured_main_costs_since(self, since: str) -> list:
        """(block_start, imp_cost, imp_cost_exc) of every settled main block — rate_source
        'settled', not hand-corrected — at or after `since`, oldest first."""
        return [tuple(r) for r in self._conn.execute(
            "SELECT block_start, imp_cost, imp_cost_exc FROM blocks "
            "WHERE meter_id = 'electricity_main' AND rate_source = 'settled' "
            "AND COALESCE(rate_corrected, 0) = 0 AND block_start >= ? ORDER BY block_start",
            (since,)).fetchall()]

    def upsert_measured_breakdown(self, slot_start: str, *, mpan: str = "",
                                 direction: str = "CONSUMPTION", home_kwh=None,
                                 home_rate=None, ev_kwh=None, ev_rate=None) -> None:
        """C2: fold the four-bucket settled Home/EV split onto the slot's measured_cost row.
        INSERTs a row (cost NULL) if none exists yet; otherwise updates ONLY the split
        columns, leaving the billed cost/label untouched. One row per slot = the whole
        settled truth (design §4)."""
        self._conn.execute(
            "INSERT INTO measured_cost (slot_start, mpan, direction, home_kwh, home_rate, "
            "ev_kwh, ev_rate, fetched_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(slot_start, mpan, direction) DO UPDATE SET "
            "home_kwh=excluded.home_kwh, home_rate=excluded.home_rate, "
            "ev_kwh=excluded.ev_kwh, ev_rate=excluded.ev_rate",
            (slot_start, mpan or "", direction, home_kwh, home_rate, ev_kwh, ev_rate,
             _utc_now_iso()))
        self._conn.commit()

    def get_measured_breakdown(self, slot_start: str, direction: str = "CONSUMPTION"):
        """C2: the cached four-bucket split for a slot (any mpan), or None if unset."""
        r = self._conn.execute(
            "SELECT home_kwh, home_rate, ev_kwh, ev_rate FROM measured_cost "
            "WHERE slot_start=? AND direction=? ORDER BY fetched_at DESC LIMIT 1",
            (slot_start, direction)).fetchone()
        if r is None or (r[0] is None and r[2] is None):
            return None
        return {"home_kwh": r[0], "home_rate": r[1], "ev_kwh": r[2], "ev_rate": r[3]}

    def slots_with_bill_split(self, start_iso: str, end_iso: str,
                              direction: str = "CONSUMPTION") -> set:
        """Slot starts in [start_iso, end_iso] for which Octopus's OWN device breakdown is
        held — i.e. the bill has actually STATED that half-hour's Home/EV split.

        A settled slot with no stored split means two very different things, and only this
        tells them apart. If the bill supplied a breakdown and its EV share is zero, the
        bill is saying there was no EV, and a stray dispatch row must not invent one. If no
        breakdown was ever retrieved — a legacy tariff has none, and an SMB slot settled
        before the four-bucket fetch reached it keeps `ev_kwh` NULL forever — then the bill
        has said NOTHING about the split, and a completed dispatch is still the best
        evidence available. `rate_source` cannot make that distinction: it records that the
        COST is settled, which is true in both cases.

        The range is inclusive at both ends; being over-inclusive by a slot is harmless,
        since callers only ever test membership for slots they are already rendering."""
        try:
            return {r[0] for r in self._conn.execute(
                "SELECT slot_start FROM measured_cost WHERE direction=? "
                "AND ev_kwh IS NOT NULL AND slot_start >= ? AND slot_start <= ?",
                (direction, start_iso, end_iso))}
        except Exception:
            return set()

    def measured_slots_missing(self, starts, mpan: str = "",
                               direction: str = "CONSUMPTION") -> list:
        """Of `starts`, the slot_starts with NO measured_cost row yet (so the
        caller only fetches what it hasn't already got)."""
        starts = [s for s in dict.fromkeys(starts or []) if s]
        if not starts:
            return []
        have = set()
        for i in range(0, len(starts), 500):
            chunk = starts[i:i + 500]
            ph = ",".join("?" * len(chunk))
            for r in self._conn.execute(
                    f"SELECT slot_start FROM measured_cost WHERE mpan=? AND "
                    f"direction=? AND slot_start IN ({ph})",
                    [mpan or "", direction] + chunk):
                have.add(r[0])
        return [s for s in starts if s not in have]

    def upsert_dispatch_slot(self, slot_start: str, *, off_peak: bool = True,
                             provider: Optional[str] = None,
                             source: Optional[str] = None,
                             state: Optional[str] = "planned",
                             energy_planned: Optional[float] = None,
                             energy_completed: Optional[float] = None,
                             raw_start: Optional[str] = None,
                             raw_end: Optional[str] = None,
                             captured_at: Optional[str] = None) -> None:
        """Record (or refresh) a 30-min slot that had an Intelligent smart-charge
        dispatch. Last-write-wins on slot_start. Captured forward each poll; the
        overlay reads these to decide off-peak candidacy (gated by meter draw at
        pricing time, NOT here).

        state / energy_planned / energy_completed are OBSERVE-ONLY lifecycle
        capture (see dispatch_validation_design.md) — recorded but not yet used
        for billing. energy_completed is preserved across refreshes if a later
        upsert doesn't supply it (a planned re-capture shouldn't wipe a settled
        figure), and likewise energy_planned once known.
        """
        if captured_at is None:
            captured_at = (datetime.now(timezone.utc)
                           .replace(tzinfo=None).isoformat())
        # The WHERE skips an update that would change nothing but captured_at, so a
        # slot re-seen unchanged on every 5-minute poll costs no write and no commit
        # (5 Oct 2026: each one was its own fsync on the SD card, and they stalled the
        # engine loop for seconds). captured_at is therefore when the slot's data last
        # changed. Nothing reads it for billing.
        with self._tx():
            self._conn.execute(
                """INSERT INTO dispatch_slots
                       (slot_start, off_peak, provider, source, captured_at,
                        state, energy_planned, energy_completed, raw_start, raw_end)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(slot_start) DO UPDATE SET
                       off_peak    = excluded.off_peak,
                       provider    = excluded.provider,
                       source      = excluded.source,
                       captured_at = excluded.captured_at,
                       state       = COALESCE(excluded.state, dispatch_slots.state),
                       energy_planned =
                           COALESCE(excluded.energy_planned, dispatch_slots.energy_planned),
                       energy_completed =
                           COALESCE(excluded.energy_completed, dispatch_slots.energy_completed),
                       raw_start   = COALESCE(excluded.raw_start, dispatch_slots.raw_start),
                       raw_end     = COALESCE(excluded.raw_end,   dispatch_slots.raw_end)
                   WHERE dispatch_slots.off_peak IS NOT excluded.off_peak
                      OR dispatch_slots.provider IS NOT excluded.provider
                      OR dispatch_slots.source   IS NOT excluded.source
                      OR dispatch_slots.state IS NOT
                             COALESCE(excluded.state, dispatch_slots.state)
                      OR dispatch_slots.energy_planned IS NOT
                             COALESCE(excluded.energy_planned, dispatch_slots.energy_planned)
                      OR dispatch_slots.energy_completed IS NOT
                             COALESCE(excluded.energy_completed, dispatch_slots.energy_completed)
                      OR dispatch_slots.raw_start IS NOT
                             COALESCE(excluded.raw_start, dispatch_slots.raw_start)
                      OR dispatch_slots.raw_end IS NOT
                             COALESCE(excluded.raw_end, dispatch_slots.raw_end)""",
                (slot_start, 1 if off_peak else 0, provider, source, captured_at,
                 state, energy_planned, energy_completed, raw_start, raw_end),
            )

    def get_dispatch_slot(self, slot_start: str) -> dict | None:
        """Return the dispatch_slots row for a slot, or None. Used by the overlay
        resolver to check whether a block's slot is an off-peak candidate.
        """
        row = self._conn.execute(
            "SELECT slot_start, off_peak, provider, source, captured_at, "
            "state, energy_planned, energy_completed, raw_start, raw_end "
            "FROM dispatch_slots WHERE slot_start = ?", (slot_start,)
        ).fetchone()
        return dict(row) if row else None

    def get_dispatch_slots_in_range(self, start_iso: str, end_iso: str) -> list:
        """All dispatch_slots with slot_start in [start_iso, end_iso). Ordered."""
        return [dict(r) for r in self._conn.execute(
            "SELECT slot_start, off_peak, provider, source, captured_at, "
            "state, energy_planned, energy_completed, raw_start, raw_end "
            "FROM dispatch_slots WHERE slot_start >= ? AND slot_start < ? "
            "ORDER BY slot_start", (start_iso, end_iso)
        ).fetchall()]

    # ── IOG 6-hour cap boundary (shared by settlement pricing + the day chart) ──
    _BUMP_SOURCES = ("bump-charge", "boost")

    def cap_day_boundary(self, block_start: str, tz_name: str):
        """The 6-hour-cap boundary for the noon->noon cap-day this slot belongs to, as
        `(boundary_slot, boundary_frac)` -- or None when the cap is never reached.

        The cap measures ACTUAL smart-charge time (design 4-rate rules): a BUMP/BOOST slot
        bills at PEAK and therefore must NOT advance the cap -- a peak-billed slot can't
        also consume the off-peak allowance. Completed dispatch slots that a bump/boost
        covers are excluded from the tally before the boundary is located. Pure projection
        of iog_cap over this store's completed dispatch feed -- the SINGLE source both the
        settlement split (_iog_cap_day_boundary) and the day chart's held-peak read."""
        import iog_cap
        key = iog_cap.cap_day_key(block_start, tz_name)
        try:
            from datetime import date as _date, timedelta as _td
            y, m, d = int(key[:4]), int(key[5:7]), int(key[8:10])
            nxt = (_date(y, m, d) + _td(days=1)).isoformat()
            lo, hi = key + "T00:00:00", nxt + "T23:59:59"
            rows = self._conn.execute(
                "SELECT slot_start, raw_start, raw_end, energy_kwh FROM dispatch_history "
                "WHERE kind='completed' AND raw_start IS NOT NULL AND raw_end IS NOT NULL "
                "AND raw_start >= ? AND raw_start < ?", (lo, hi)).fetchall()
            _qs = ",".join("?" * len(self._BUMP_SOURCES))
            boost = {r["slot_start"] for r in self._conn.execute(
                "SELECT DISTINCT slot_start FROM dispatch_history "
                f"WHERE source IN ({_qs}) AND slot_start >= ? AND slot_start < ?",
                (*self._BUMP_SOURCES, lo, hi)).fetchall()}
        except Exception:
            return None
        feed = [(r["raw_start"], r["raw_end"], r["energy_kwh"])
                for r in rows if r["slot_start"] not in boost]
        return iog_cap.cap_day_boundaries(feed, tz_name, iog_cap.CAP_HOURS).get(key)

    def prune_dispatch_slots(self, days: int = 90) -> int:
        """Delete dispatch_slots older than `days` days. Generous retention (90d)
        — these are small and the overlay may reprice historical blocks during a
        billing-period review. Returns rows deleted.
        """
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
                  - timedelta(days=days)).isoformat()
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM dispatch_slots WHERE slot_start < ?", (cutoff,)
            )
        return cur.rowcount

    def emt_recording_continuously(self, slot_start: str, hours: float = 4.0,
                                   block_minutes: int = 30) -> bool:
        """True when EMT recorded an UNBROKEN run of LIVE (non-interpolated) main-meter blocks
        across [slot_start - hours, slot_start) — i.e. it was demonstrably up and polling in
        the window a smart-charge PLAN for this slot would have been visible. EMT polls the
        dispatch API on the same loop, so continuous live blocks mean any published plan would
        have been captured. Used to turn a completed-only dispatch with NO plan into a
        CONFIDENT bump: EMT was up and still saw no plan -> Octopus never scheduled it. A
        missing OR interpolated (gap-filled) block in the window => EMT may have been offline
        when the plan appeared => NOT confident (leave the completed-only rescue as off-peak).
        """
        from datetime import datetime as _dt, timedelta as _td
        try:
            end = _dt.fromisoformat(str(slot_start).replace("Z", "").split("+")[0])
        except Exception:
            return False
        step = max(1, int(block_minutes or 30))
        start = end - _td(hours=float(hours))
        rows = self._conn.execute(
            "SELECT block_start, interpolated FROM blocks "
            "WHERE meter_id = 'electricity_main' AND block_start >= ? AND block_start < ?",
            (start.isoformat(), end.isoformat())).fetchall()
        live = {r["block_start"] for r in rows if not r["interpolated"]}
        t, n = start, 0
        while t < end:
            if t.isoformat() not in live:
                return False          # missing or interpolated -> not continuously live
            n += 1
            t += _td(minutes=step)
        return n > 0

    def materialise_completed_only_slots(self, small_kwh: float = 0.4,
                                         is_off_peak=None, is_boost=None,
                                         emt_up_hours: float = 4.0) -> int:
        """Promote SUBSTANTIAL completed-ONLY dispatches (no planned/started, no existing
        slot) from dispatch_history into dispatch_slots so reconcile/settlement can see them
        (design §14). Each is classified for its PROVISIONAL band:

          * INSIDE the off-peak window -> OFF-peak (guaranteed; even a boost is off-peak there).
          * OUTSIDE the window + CONFIDENT bump -> PEAK. Confident = the charger flags a boost
            (is_boost), OR EMT was recording continuously through the window a plan would have
            appeared (emt_recording_continuously) yet saw NO plan -> Octopus never scheduled it.
            Settlement heals it DOWN immediately if we're wrong (cheaper = applied at any age).
          * OUTSIDE the window + AMBIGUOUS (EMT may have been offline when a plan would appear)
            -> OFF-peak, preserving the original completed-only rescue (a plan missed offline).

        is_off_peak / is_boost: optional callables(slot_start)->bool|None. With is_off_peak
        None (no schedule), legacy behaviour: every completed-only slot is off-peak. Peak slots
        carry source='smart-charge-completed' like off-peak ones, so the settlement fetch (which
        now includes them) can still verify and heal them. Idempotent. Returns rows created."""
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        cands = self._conn.execute(
            """SELECT h.slot_start, MAX(h.provider) AS provider,
                      SUM(h.energy_kwh) AS energy_kwh,
                      MAX(h.raw_start) AS raw_start, MAX(h.raw_end) AS raw_end
               FROM dispatch_history h
               WHERE h.kind = 'completed' AND h.energy_kwh IS NOT NULL
                 AND ABS(h.energy_kwh) >= ?
                 AND NOT EXISTS (SELECT 1 FROM dispatch_slots s
                                 WHERE s.slot_start = h.slot_start)
                 AND NOT EXISTS (SELECT 1 FROM dispatch_history h2
                                 WHERE h2.slot_start = h.slot_start
                                   AND h2.kind IN ('planned', 'started'))
               GROUP BY h.slot_start""",
            (small_kwh,)).fetchall()
        n = 0
        with self._conn:
            for r in cands:
                ss = r["slot_start"]
                off_peak = 1
                _inwin = None
                if is_off_peak is not None:
                    try:
                        _inwin = is_off_peak(ss)
                    except Exception:
                        _inwin = None
                if _inwin is False:                 # OUTSIDE the guaranteed off-peak window
                    _boost = False
                    if is_boost is not None:
                        try:
                            _boost = bool(is_boost(ss))
                        except Exception:
                            _boost = False
                    if _boost or self.emt_recording_continuously(ss, emt_up_hours):
                        off_peak = 0                # confident bump -> provisional PEAK
                self._conn.execute(
                    """INSERT INTO dispatch_slots
                           (slot_start, off_peak, provider, source, captured_at,
                            state, energy_planned, energy_completed, raw_start, raw_end)
                       VALUES (?, ?, ?, 'smart-charge-completed', ?,
                               'completed', NULL, ?, ?, ?)""",
                    (ss, off_peak, r["provider"], now, r["energy_kwh"],
                     r["raw_start"], r["raw_end"]))
                n += 1
        return n

    # ── Dispatch history (observe-only accumulation, design §11) ──────────────

    def record_dispatch_history(self, slot_start: str, kind: str, *,
                                provider: str | None = None,
                                source: str | None = None,
                                energy_kwh: float | None = None,
                                raw_start: str | None = None,
                                raw_end: str | None = None,
                                seen_at: str | None = None,
                                refresh_seen: bool = True) -> None:
        """Accumulate one (slot_start, kind) dispatch observation. OBSERVE-ONLY —
        never read for billing. first_seen is set once and preserved; the latest
        energy/provider/source refresh whenever they change. Keeping a persistent
        record (vs the per-poll snapshot) is what makes a later 'never seen this
        slot' judgement trustworthy.

        `refresh_seen` (default True): last_seen advances on every poll that sees
        the row. The PLANNED rows need that — the smart-charging card tells the
        current plan from a superseded one by it (server `_UPCOMING_BATCH_SEC`).
        With False, an unchanged row is not written at all, so last_seen is when
        its data last changed: for COMPLETED rows, which Octopus re-serves on every
        5-minute poll and whose last_seen nothing reads (5 Oct 2026 — each re-write
        was its own SD-card commit, stalling the engine loop)."""
        seen = seen_at or datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        only_if_changed = "" if refresh_seen else """
                   WHERE dispatch_history.provider IS NOT
                             COALESCE(excluded.provider, dispatch_history.provider)
                      OR dispatch_history.source IS NOT
                             COALESCE(excluded.source, dispatch_history.source)
                      OR dispatch_history.energy_kwh IS NOT
                             COALESCE(excluded.energy_kwh, dispatch_history.energy_kwh)
                      OR dispatch_history.raw_start IS NOT
                             COALESCE(excluded.raw_start, dispatch_history.raw_start)
                      OR dispatch_history.raw_end IS NOT
                             COALESCE(excluded.raw_end, dispatch_history.raw_end)"""
        with self._tx():
            self._conn.execute(
                """INSERT INTO dispatch_history
                       (slot_start, kind, provider, source, energy_kwh,
                        first_seen, last_seen, raw_start, raw_end)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(slot_start, kind) DO UPDATE SET
                       provider   = COALESCE(excluded.provider, dispatch_history.provider),
                       source     = COALESCE(excluded.source,   dispatch_history.source),
                       energy_kwh = COALESCE(excluded.energy_kwh, dispatch_history.energy_kwh),
                       last_seen  = excluded.last_seen,
                       raw_start  = COALESCE(excluded.raw_start, dispatch_history.raw_start),
                       raw_end    = COALESCE(excluded.raw_end,   dispatch_history.raw_end)"""
                + only_if_changed,
                (slot_start, kind, provider, source, energy_kwh, seen, seen,
                 raw_start, raw_end))

    @contextlib.contextmanager
    def batch(self):
        """One transaction for a run of writes that would otherwise each commit: the
        writers that use `_tx()` (and set_meta) commit once, at the end, instead of
        each committing. On the Pi's SD card every commit is an fsync, and a run of
        them stalled the engine loop for seconds — the dispatch poll (about ten a
        poll, 5 Oct 2026) and the re-price sweep (one per block, ROADMAP §A.1).

        The caller must not `await` inside it: the batch holds a write transaction,
        and the loop must not run other work against it. A nested batch joins the
        outer one. An exception that escapes rolls the whole batch back."""
        if getattr(self, "_in_batch", False):
            yield
            return
        self._in_batch = True
        try:
            with self._conn:
                yield
        finally:
            self._in_batch = False

    # The dispatch poll's name for it (#91).
    dispatch_batch = batch

    def _tx(self):
        """A batched writer's transaction: the enclosing batch's, or its own."""
        return contextlib.nullcontext() if getattr(self, "_in_batch", False) \
            else self._conn

    def get_dispatch_history(self, start_iso: str, end_iso: str,
                             kind: str | None = None) -> list:
        """Accumulated dispatch observations with slot_start in [start, end).
        Optionally filter by kind. Ordered by slot_start."""
        if kind is not None:
            rows = self._conn.execute(
                "SELECT slot_start, kind, provider, source, energy_kwh, "
                "first_seen, last_seen, raw_start, raw_end FROM dispatch_history "
                "WHERE slot_start >= ? AND slot_start < ? AND kind = ? "
                "ORDER BY slot_start", (start_iso, end_iso, kind)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT slot_start, kind, provider, source, energy_kwh, "
                "first_seen, last_seen, raw_start, raw_end FROM dispatch_history "
                "WHERE slot_start >= ? AND slot_start < ? "
                "ORDER BY slot_start, kind", (start_iso, end_iso)).fetchall()
        return [dict(r) for r in rows]

    def get_dispatch_history_for_slot(self, slot_start: str,
                                      kind: str | None = None) -> list:
        """Dispatch observations for ONE slot, by exact `slot_start`.

        The sibling `get_dispatch_history` takes a half-open range, and a range
        of [slot, next_slot) would select exactly these rows -- but only if the
        caller does slot arithmetic to find `next_slot`, which is a dependency
        the question does not need. `dispatch_slots` already has its exact-match
        accessor in `get_dispatch_slot(slot_start)`; this is the same shape for
        `dispatch_history`.

        Same columns and ordering as `get_dispatch_history`, deliberately: two
        methods over one table that returned different shapes would be a trap
        for whoever reads them side by side.
        """
        if kind is not None:
            rows = self._conn.execute(
                "SELECT slot_start, kind, provider, source, energy_kwh, "
                "first_seen, last_seen, raw_start, raw_end FROM dispatch_history "
                "WHERE slot_start = ? AND kind = ? ORDER BY slot_start",
                (slot_start, kind)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT slot_start, kind, provider, source, energy_kwh, "
                "first_seen, last_seen, raw_start, raw_end FROM dispatch_history "
                "WHERE slot_start = ? ORDER BY kind", (slot_start,)).fetchall()
        return [dict(r) for r in rows]

    def prune_dispatch_history(self, days: int = 90) -> int:
        """Delete dispatch_history older than `days`. Returns rows deleted."""
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
                  - timedelta(days=days)).isoformat()
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM dispatch_history WHERE slot_start < ?", (cutoff,))
        return cur.rowcount

    # ── Generation mix ────────────────────────────────────────────────────────

    def upsert_mix_history(self, captured_at: str, mix: list[dict]) -> None:
        """Write CI-tick generation mix to mix_history (48hr rolling store)."""
        with self._conn:
            self._upsert_mix_rows(captured_at, mix)

    def _upsert_mix_rows(self, captured_at: str, mix: list[dict]) -> None:
        """upsert_mix_history's statements, inside the caller's transaction."""
        for entry in mix:
            self._conn.execute(
                """INSERT INTO mix_history (captured_at, fuel, perc)
                   VALUES (?, ?, ?)
                   ON CONFLICT(captured_at, fuel) DO UPDATE SET perc=excluded.perc""",
                (captured_at, entry.get("fuel", ""), entry.get("perc", 0.0))
            )

    def prune_mix_history(self, hours: int = 48) -> None:
        """Delete mix_history rows older than `hours` hours."""
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")
        with self._conn:
            self._conn.execute(
                "DELETE FROM mix_history WHERE captured_at < ?", (cutoff,)
            )

    def get_mix_history(self, hours: int = 48) -> list:
        """Return mix_history slots for the last `hours` hours."""
        from datetime import datetime, timezone, timedelta
        _now = datetime.now(timezone.utc)
        cutoff  = (_now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")
        # #408: mix_history also holds fw48h FORECAST slots (future captured_at).
        # Bound the upper edge to now so a forecast slot can never bleed into the
        # 48h chart (the donut hit the same bug via MAX(captured_at)).
        now_str = _now.strftime("%Y-%m-%dT%H:%M")
        rows = self._conn.execute(
            """SELECT captured_at, fuel, perc
               FROM mix_history
               WHERE captured_at >= ? AND captured_at <= ?
               ORDER BY captured_at ASC, fuel ASC""",
            (cutoff, now_str)
        ).fetchall()
        slots: dict = {}
        order: list = []
        for r in rows:
            ca = r["captured_at"]
            if ca not in slots:
                slots[ca] = {}
                order.append(ca)
            slots[ca][r["fuel"]] = r["perc"]
        return [{"captured_at": ca, "fuels": slots[ca]} for ca in order]

    def retire_meter(self, meter_id: str, retired_at: str, retired_reason: str = "") -> None:
        """Mark a sub-meter as retired from the given date. Historical data is preserved.
        Also clears any stale current_reads entries so the engine does not record
        cost/rate data for the retired meter after this point."""
        with self._conn:
            self._conn.execute(
                """UPDATE meters SET retired_at = ?, retired_reason = ?
                   WHERE meter_id = ? AND is_sub_meter = 1""",
                (retired_at, retired_reason or None, meter_id)
            )
            # Clear live read/rate accumulation for this meter
            self._conn.execute(
                "DELETE FROM current_reads WHERE meter_id = ?",
                (meter_id,)
            )

    def unretire_meter(self, meter_id: str) -> None:
        """Clear retirement status from a sub-meter.
        Raises ValueError if any active meter already uses the same read sensor."""
        # Check for sensor conflicts with active (non-retired) meters
        conflicts = self._conn.execute(
            """SELECT m.meter_id, mc.read_sensor
               FROM meters m
               JOIN meter_channels mc ON mc.meter_id = m.id
               WHERE m.retired_at IS NULL
                 AND m.meter_id != ?
                 AND mc.read_sensor IN (
                     SELECT mc2.read_sensor FROM meter_channels mc2
                     JOIN meters m2 ON m2.id = mc2.meter_id
                     WHERE m2.meter_id = ?
                       AND mc2.read_sensor IS NOT NULL
                 )""",
            (meter_id, meter_id)
        ).fetchall()
        if conflicts:
            sensors = ", ".join(set(r["read_sensor"] for r in conflicts))
            meters  = ", ".join(set(r["meter_id"]   for r in conflicts))
            raise ValueError(
                f"Cannot unretire — sensor(s) {sensors} already in use by active meter(s): {meters}. "
                f"Retire or reconfigure the conflicting meter first."
            )
        with self._conn:
            self._conn.execute(
                "UPDATE meters SET retired_at = NULL, retired_reason = NULL WHERE meter_id = ?",
                (meter_id,)
            )

    def get_retired_meters(self) -> list:
        """Return all meters with a retirement date set."""
        rows = self._conn.execute(
            """SELECT meter_id, device_label, meter_type, retired_at, retired_reason
               FROM meters WHERE retired_at IS NOT NULL ORDER BY retired_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def is_meter_retired(self, meter_id: str, as_of: str | None = None) -> bool:
        """Return True if the meter is retired as of the given date (default: today)."""
        from datetime import datetime, timezone
        check_date = as_of or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = self._conn.execute(
            "SELECT retired_at FROM meters WHERE meter_id = ?", (meter_id,)
        ).fetchone()
        if not row or row["retired_at"] is None:
            return False
        return row["retired_at"] <= check_date

    def upsert_generation_mix(self, block_id: int, mix: list[dict]) -> None:
        """
        Store generation mix for a block. mix is a list of {fuel, perc} dicts
        from the National Grid ESO API generationmix array.
        Replaces any existing mix for this block.
        """
        if not mix or block_id is None:
            return
        with self._conn:
            self._conn.execute(
                "DELETE FROM generation_mix WHERE block_id = ?", (block_id,)
            )
            self._conn.executemany(
                "INSERT INTO generation_mix (block_id, fuel, perc) VALUES (?, ?, ?)",
                [(block_id, str(row.get("fuel", "")).lower(), float(row.get("perc", 0)))
                 for row in mix if row.get("fuel") is not None]
            )

    def get_generation_mix(self, block_id: int) -> list[dict]:
        """Return generation mix for a block as [{fuel, perc}, ...] sorted by perc desc."""
        rows = self._conn.execute(
            "SELECT fuel, perc FROM generation_mix WHERE block_id = ? ORDER BY perc DESC",
            (block_id,)
        ).fetchall()
        return [{"fuel": r["fuel"], "perc": r["perc"]} for r in rows]

    def get_generation_mix_for_range(self, utc_start: str, utc_end: str,
                                      meter_id: str = "electricity_main") -> list[dict]:
        """
        Return imp_kwh-weighted average generation mix across all blocks in the
        given UTC range for the specified meter. Returns [{fuel, perc}, ...].
        Useful for period-level Insights aggregations.
        """
        rows = self._conn.execute(
            """SELECT gm.fuel,
                      SUM(gm.perc * b.imp_kwh) / NULLIF(SUM(b.imp_kwh), 0) as weighted_perc
               FROM generation_mix gm
               JOIN blocks b ON b.id = gm.block_id
               WHERE b.block_start >= ? AND b.block_start < ?
                 AND b.meter_id = ?
                 AND b.imp_kwh > 0
               GROUP BY gm.fuel
               ORDER BY weighted_perc DESC""",
            (utc_start, utc_end, meter_id)
        ).fetchall()
        return [{"fuel": r["fuel"], "perc": round(r["weighted_perc"], 2)} for r in rows]

    # ── Power history ─────────────────────────────────────────────────────────

    def append_power_history(self, captured_at: str, net_kw: float,
                              intensity: float | None,
                              carbon_gco2_min: float | None = None) -> None:
        """Append a power history row. Only call when power sensor is available."""
        with self._conn:
            self._conn.execute(
                """INSERT INTO power_history (captured_at, net_kw, intensity, carbon_gco2_min)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(captured_at) DO UPDATE SET
                       net_kw          = excluded.net_kw,
                       intensity       = excluded.intensity,
                       carbon_gco2_min = excluded.carbon_gco2_min""",
                (captured_at, net_kw, intensity, carbon_gco2_min)
            )

    # ── Settings ──────────────────────────────────────────────────────────────

    def get_settings(self) -> dict:
        """Return all settings as a dict. Missing keys return defaults."""
        import json as _json
        row = self._conn.execute(
            "SELECT value FROM store_meta WHERE key = 'settings'"
        ).fetchone()
        if row and row['value']:
            try:
                return _json.loads(row['value'])
            except Exception:
                pass
        return {}

    def save_settings(self, settings: dict) -> None:
        """Persist settings dict to store_meta."""
        import json as _json
        self._conn.execute(
            "INSERT INTO store_meta (key, value) VALUES ('settings', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_json.dumps(settings),)
        )
        self._conn.commit()

    def get_meta(self, key: str, default=None):
        """Generic KV read from store_meta. Values are JSON-encoded; the raw
        string is returned if it isn't valid JSON (e.g. legacy schema_version)."""
        import json as _json
        row = self._conn.execute(
            "SELECT value FROM store_meta WHERE key = ?", (key,)
        ).fetchone()
        if row and row["value"] is not None:
            try:
                return _json.loads(row["value"])
            except Exception:
                return row["value"]
        return default

    def set_meta(self, key: str, value) -> None:
        """Generic KV write to store_meta (value JSON-encoded). Used for the
        run-once historical-carbon-backfill marker / resume cursor."""
        import json as _json
        self._conn.execute(
            "INSERT INTO store_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, _json.dumps(value))
        )
        if not getattr(self, "_in_batch", False):     # inside batch(): its commit
            self._conn.commit()

    def prune_power_history(self, hours: int = 48) -> int:
        """Delete power_history rows older than `hours` hours. Returns rows deleted."""
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
                  - timedelta(hours=hours)).isoformat()
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM power_history WHERE captured_at < ?", (cutoff,)
            )
        return cur.rowcount

    def get_power_history(self, hours: int = 48) -> list:
        """Return power_history rows for the last `hours` hours, oldest first."""
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
                  - timedelta(hours=hours)).isoformat()
        rows = self._conn.execute(
            """SELECT captured_at, net_kw, intensity, carbon_gco2_min
               FROM power_history
               WHERE captured_at >= ?
               ORDER BY captured_at ASC""",
            (cutoff,)
        ).fetchall()
        return [dict(r) for r in rows]

    def append_sub_meter_history(self, captured_at: str, meter_id: str,
                                soc_pct: float | None,
                                inverter_kw: float | None) -> None:
        """Append a battery history row for one battery meter."""
        with self._conn:
            self._conn.execute(
                """INSERT INTO sub_meter_history (captured_at, meter_id, soc_pct, inverter_kw)
                   VALUES (?, ?, ?, ?)""",
                (captured_at, meter_id, soc_pct, inverter_kw)
            )

    def prune_sub_meter_history(self, hours: int = 48) -> int:
        """Delete sub_meter_history rows older than `hours` hours. Returns rows deleted."""
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
                  - timedelta(hours=hours)).isoformat()
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM sub_meter_history WHERE captured_at < ?", (cutoff,)
            )
        return cur.rowcount

    def get_sub_meter_history(self, meter_id: str, hours: int = 48) -> list:
        """Return sub_meter_history rows for one meter for the last `hours` hours, oldest first."""
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
                  - timedelta(hours=hours)).isoformat()
        rows = self._conn.execute(
            """SELECT captured_at, soc_pct, inverter_kw
               FROM sub_meter_history
               WHERE meter_id = ? AND captured_at >= ?
               ORDER BY captured_at ASC""",
            (meter_id, cutoff)
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def delete_config_period(self, period_id: int) -> dict:
        """
        Delete a config period.

        Blocks are always reassigned to the PREVIOUS period (older effective_from).
        If no previous period exists, they fall to the next period.

        Returns {"deleted": True, "blocks_reassigned": N}.
        Raises ValueError if the period does not exist or is the only period.
        """
        cp = self.get_config_period(period_id)
        if not cp:
            raise ValueError(f"Config period {period_id} not found")

        # Must not delete the only period
        cur = self._conn.execute("SELECT COUNT(*) FROM config_periods")
        if cur.fetchone()[0] <= 1:
            raise ValueError("Cannot remove the only config period")

        # Count blocks in this period
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM blocks WHERE config_period_id = ?", (period_id,)
        )
        block_rows = cur.fetchone()[0]

        # Find the PREVIOUS period (strictly older effective_from)
        absorb_id = None
        cur = self._conn.execute(
            """SELECT id FROM config_periods
               WHERE effective_from < ? AND id != ?
               ORDER BY effective_from DESC LIMIT 1""",
            (cp["effective_from"], period_id)
        )
        row = cur.fetchone()
        if row:
            absorb_id = row["id"]

        # Fall back to next period if no previous exists
        if absorb_id is None:
            cur = self._conn.execute(
                """SELECT id FROM config_periods
                   WHERE id != ?
                   ORDER BY effective_from ASC LIMIT 1""",
                (period_id,)
            )
            row = cur.fetchone()
            if row:
                absorb_id = row["id"]

        with self._conn:
            # Reassign blocks to absorbing period
            if block_rows > 0 and absorb_id:
                self._conn.execute(
                    "UPDATE blocks SET config_period_id = ? WHERE config_period_id = ?",
                    (absorb_id, period_id)
                )

            # Fix the chain:
            # - Middle period: predecessor's effective_to becomes this period's effective_to
            # - Active (last) period: predecessor becomes active (effective_to = NULL)
            if cp["effective_to"] is not None:
                # Middle period — bridge predecessor to successor
                self._conn.execute(
                    "UPDATE config_periods SET effective_to = ? WHERE effective_to = ?",
                    (cp["effective_to"], cp["effective_from"])
                )
            else:
                # Active period — make predecessor active
                self._conn.execute(
                    "UPDATE config_periods SET effective_to = NULL WHERE effective_to = ?",
                    (cp["effective_from"],)
                )

            # Delete normalised meter rows before removing the period
            # (FK constraint: meters.config_period_id → config_periods.id)
            meter_ids = [r["id"] for r in self._conn.execute(
                "SELECT id FROM meters WHERE config_period_id = ?", (period_id,)
            ).fetchall()]
            for mid in meter_ids:
                self._conn.execute(
                    "DELETE FROM meter_channels WHERE meter_id = ?", (mid,)
                )
            self._conn.execute(
                "DELETE FROM meters WHERE config_period_id = ?", (period_id,)
            )
            self._conn.execute(
                "DELETE FROM config_periods WHERE id = ?", (period_id,)
            )

        logger.info(
            "delete_config_period: id=%d deleted, %d blocks reassigned to id=%s",
            period_id, block_rows, absorb_id
        )
        return {"deleted": True, "blocks_reassigned": block_rows}


    def _resolve_delete_meters(self, meter_id):
        """Normalize the meter sentinel for a block delete/preview.

        Returns None when *all* meters should be affected (meter_id is None,
        empty, or the UI sentinel "all" — previously this string was passed
        through as a literal filter, matching no row and silently deleting
        nothing). Otherwise returns a de-duplicated list of meter_ids: the
        requested meter plus, when it is a parent (main) meter, all of its
        sub-meters — so deleting a day for the main meter takes its device
        blocks with it and cannot leave an orphaned, internally-inconsistent
        bill line behind.
        """
        if meter_id in (None, "", "all"):
            return None
        ids = [meter_id]
        cur = self._conn.execute(
            "SELECT DISTINCT meter_id FROM meters WHERE parent_meter_id = ?",
            (meter_id,),
        )
        ids.extend(r["meter_id"] for r in cur.fetchall())
        return list(dict.fromkeys(ids))

    @staticmethod
    def _utc_span_bounds(utc_span) -> tuple:
        """A [from, to) naive-UTC span as the delete's bounds (end exclusive)."""
        a, b = (str(x or "")[:19] for x in utc_span)
        try:
            datetime.fromisoformat(a); datetime.fromisoformat(b)
        except ValueError:
            raise ValueError("from_utc and to_utc must be ISO times")
        if not a < b:
            raise ValueError("from_utc must be before to_utc")
        return a, b, False

    def delete_span_preview(self, from_utc: str, to_utc: str) -> dict:
        """What deleting the half-hours [from_utc, to_utc) takes, for the heatmap's warning (soft
        delete design §3): the half-hours with any block, the devices among them, and HA's reads
        there — which the delete leaves, and a refill rebuilds the devices from."""
        a, b, _ = self._utc_span_bounds((from_utc, to_utc))
        r = self._conn.execute(
            "SELECT COUNT(DISTINCT block_start) n, COUNT(*) rows FROM blocks "
            "WHERE block_start >= ? AND block_start < ?", (a, b)).fetchone()
        devices = [x[0] for x in self._conn.execute(
            "SELECT DISTINCT b.meter_id FROM blocks b JOIN meters m ON m.meter_id = b.meter_id "
            "AND m.config_period_id = b.config_period_id WHERE m.is_sub_meter = 1 AND "
            "b.block_start >= ? AND b.block_start < ? ORDER BY b.meter_id", (a, b))]
        kept = self._conn.execute(
            "SELECT COUNT(*) FROM ha_reads WHERE period_start >= ? AND period_start < ?",
            (a, b)).fetchone()[0]
        # the imported ones (CSV, PDF bills, the API import): what *only the imported* would take
        # (data management design §1 (6)) — the delete's `reconstructed_only`, as the Delete Blocks
        # page offered it
        imp = self._conn.execute(
            "SELECT COUNT(DISTINCT block_start), COUNT(*) FROM blocks WHERE block_start >= ? AND "
            "block_start < ? AND source LIKE 'imported%'", (a, b)).fetchone()
        return {"from_utc": a, "to_utc": b, "half_hours": r["n"] or 0, "rows": r["rows"] or 0,
                "devices": devices, "ha_reads": kept,
                "imported_half_hours": imp[0] or 0, "imported_rows": imp[1] or 0}

    def _delete_range_bounds(self, from_date, to_date, from_time, to_time, tz_name):
        """Resolve the delete/preview window to a CONTIGUOUS UTC datetime span
        [utc_start, utc_end], NOT a per-day time-of-day window.

        `FROM 14/04 00:00 → TO 15/04 12:00` means one continuous range (14th 00:00
        through 15th 12:00), which is how the UI reads. The old behaviour applied
        the two times as a TIME(block_start) filter to every day in the range, so it
        deleted 00:00–12:00 on BOTH days — the delete-scoping bug. `from_time`/
        `to_time` are LOCAL wall-clock HH:MM.

        Returns (utc_start, utc_end, end_inclusive). A whole-day end (to_time
        '23:59') resolves to the next local-midnight boundary and is EXCLUSIVE; an
        explicit end time is INCLUSIVE of that slot.
        """
        from_time = from_time or "00:00"
        to_time   = to_time   or "23:59"
        utc_start = local_datetime_to_utc(from_date, from_time, tz_name)
        if to_time == "23:59":
            _, utc_end = local_date_to_utc_bounds(to_date, tz_name)  # exclusive next-midnight
            return utc_start, utc_end, False
        return utc_start, local_datetime_to_utc(to_date, to_time, tz_name), True

    def _block_range_where(self, utc_start, utc_end, end_inclusive, meter_ids,
                           reconstructed_only=False):
        """Build the shared WHERE clause + params for a CONTIGUOUS block date-range
        delete/preview. `meter_ids` is None (all meters) or a list of ids.

        `reconstructed_only` restricts to historical-import blocks
        (`source LIKE 'imported%'`), so a bad import can be rolled back without
        touching native live/kraken data."""
        clauses = ["block_start >= ?",
                   "block_start <= ?" if end_inclusive else "block_start < ?"]
        params  = [utc_start, utc_end]

        if reconstructed_only:
            clauses.append("source LIKE 'imported%'")

        if meter_ids is not None:
            placeholders = ",".join("?" * len(meter_ids))
            clauses.append(f"meter_id IN ({placeholders})")
            params.extend(meter_ids)

        return " AND ".join(clauses), params

    def _count_blocks_and_local_dates(self, where, params, tz_name):
        """Return (block_count, distinct_local_date_count) for a delete/preview
        WHERE clause. The day count is in the *local* timezone, not UTC: a BST
        local day starts at 23:00 UTC the previous day, so counting distinct
        date(block_start) on the stored UTC timestamps would report two calendar
        days for a single local day. Convert each block_start to local first."""
        n = self._conn.execute(
            f"SELECT COUNT(*) FROM blocks WHERE {where}", params).fetchone()[0]
        if not n:
            return 0, 0
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")
        starts = self._conn.execute(
            f"SELECT DISTINCT block_start FROM blocks WHERE {where}", params).fetchall()
        dates = {
            datetime.fromisoformat(r[0]).replace(tzinfo=timezone.utc).astimezone(tz).date()
            for r in starts
        }
        return n, len(dates)

    def delete_blocks_for_date_range(
        self, from_date: str, to_date: str, meter_id: str | None = None,
        from_time: str = "00:00", to_time: str = "23:59", *, tz_name: str,
        reconstructed_only: bool = False, utc_span: tuple | None = None
    ) -> dict:
        """
        Delete all blocks within [from_date, to_date] local date range (inclusive),
        optionally restricted by time-of-day (UTC HH:MM) and/or to a single meter
        (which also pulls in that meter's sub-meters — see _resolve_delete_meters).

        `reconstructed_only=True` limits the delete to historical-import blocks
        (`source LIKE 'imported%'`) — the reconstructed-history rollback filter.

        Cascades to the rows that reference the deleted blocks: `reads` (FK
        block_id, deleted first so foreign_keys=ON can't error) and
        `generation_mix` (block_id). When the delete removes the most recent
        finalised block for the targeted meters (a "tail" delete), the engine's
        live read-state (current_block + current_reads) is cleared in the same
        transaction so the next block re-anchors from the next live read instead
        of spanning the deleted window.

        `utc_span` (from, to): naive-UTC half-hour starts, [from, to) — the heatmap's selection
        (soft delete design §3), in place of the local dates and times.
        """
        if utc_span:
            utc_start, utc_end, end_incl = self._utc_span_bounds(utc_span)
        else:
            if not from_date or not to_date:
                raise ValueError("from_date and to_date are required")
            if from_date > to_date:
                raise ValueError("from_date must not be after to_date")

            from_time = from_time or "00:00"
            to_time   = to_time   or "23:59"

            # CONTIGUOUS span (from_date+from_time .. to_date+to_time), NOT a per-day
            # time-of-day window (the delete-scoping fix). from_time/to_time are LOCAL.
            utc_start, utc_end, end_incl = self._delete_range_bounds(
                from_date, to_date, from_time, to_time, tz_name)

        meter_ids = self._resolve_delete_meters(meter_id)
        where, params = self._block_range_where(
            utc_start, utc_end, end_incl, meter_ids, reconstructed_only
        )

        n_blocks, n_dates = self._count_blocks_and_local_dates(where, params, tz_name)

        # Tail detection: is the newest finalised block within the targeted
        # meters inside the deleted range? If so the engine anchor must reset.
        if meter_ids is not None:
            ph = ",".join("?" * len(meter_ids))
            latest = self._conn.execute(
                f"SELECT MAX(block_start) AS m FROM blocks WHERE meter_id IN ({ph})",
                meter_ids,
            ).fetchone()["m"]
        else:
            latest = self._conn.execute(
                "SELECT MAX(block_start) AS m FROM blocks"
            ).fetchone()["m"]
        is_tail = bool(latest is not None and utc_start <= latest
                       and (latest <= utc_end if end_incl else latest < utc_end))

        # Device-only delete? If the resolved target is a single sub-meter (its
        # parent's blocks are untouched and survive), the parent's stored
        # remainder for these windows is now stale — hand the parent + window
        # back so the caller can trigger a PASS-2 remainder recompute. A main
        # or all-meters delete takes the devices with it, so nothing to recompute.
        recompute_parent = None
        if meter_ids is not None and len(meter_ids) == 1:
            pr = self._conn.execute(
                "SELECT parent_meter_id FROM meters "
                "WHERE meter_id = ? AND is_sub_meter = 1 AND parent_meter_id IS NOT NULL "
                "LIMIT 1",
                (meter_ids[0],),
            ).fetchone()
            if pr and pr["parent_meter_id"]:
                recompute_parent = pr["parent_meter_id"]

        mix_deleted   = 0
        segments_deleted = 0
        with self._conn:
            if n_blocks:
                # Children first (generation_mix FK -> blocks(id) with foreign_keys=ON).
                # Match by block id via subquery: no huge IN-list, no param cap.
                cur_m = self._conn.execute(
                    f"DELETE FROM generation_mix WHERE block_id IN "
                    f"(SELECT id FROM blocks WHERE {where})",
                    params,
                )
                mix_deleted = cur_m.rowcount
                # P3.1: block_segments has no block_id FK (keyed by block_start) so it
                # is NOT cascade-cleaned — delete it explicitly or the range leaves
                # orphaned segments (breaks Σ segments == grid for the touched span).
                cur_s = self._conn.execute(
                    f"DELETE FROM block_segments WHERE (block_start, meter_id) IN "
                    f"(SELECT block_start, meter_id FROM blocks WHERE {where})",
                    params,
                )
                segments_deleted = cur_s.rowcount
                self._conn.execute(f"DELETE FROM blocks WHERE {where}", params)
            if is_tail and n_blocks:
                # Reseed live engine state so the next block starts cleanly.
                self._conn.execute("DELETE FROM current_block")
                self._conn.execute("DELETE FROM current_reads")
            if n_blocks:
                # BL-8 phase 2: tombstone the deleted span so auto-backfill, the
                # settlement sweep, and the gap-scan skip it — a deliberate delete
                # stays deleted instead of being re-created as an outage hole. One
                # row per resolved meter (or '*' for an all-meters delete). Written
                # in the SAME transaction as the delete so the two can't diverge.
                # The delete is now CONTIGUOUS, so [utc_start, tomb_end) matches
                # exactly what was removed. deleted_ranges is half-open; when the
                # end is INCLUSIVE (explicit to_time) extend by one block so the last
                # deleted slot is covered.
                # Both ends on block boundaries (17 Oct 2026, the Pi soak): an explicit end time
                # is rarely one. "00:00 to 00:29" on 15 Jul (BST) is 23:00 to 23:29 UTC; the
                # delete removed only the 23:00 block, but 23:29 + 30 min tombstoned 23:30 too,
                # a block still there — and a tombstoned slot is never re-created by the
                # automatic fills. The end is the last block the delete can have removed (the
                # latest start at or before utc_end) plus one block; the start, the first block
                # at or after utc_start.
                try:
                    _cp = self.get_config_period_for_date(utc_start)
                    _step = int((_cp or {}).get("block_minutes") or 30)
                except Exception:
                    _step = 30
                _tomb_start = _block_ceil(utc_start, _step)
                if end_incl:
                    _tomb_end = (datetime.fromisoformat(_block_floor(utc_end, _step))
                                 + timedelta(minutes=_step)).isoformat()
                else:
                    _tomb_end = _block_ceil(utc_end, _step)
                _reason = "imported_rollback" if reconstructed_only else "user_delete"
                _now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                for _mid in (meter_ids if meter_ids is not None else ["*"]):
                    self._conn.execute(
                        "INSERT INTO deleted_ranges (meter_id, start_utc, end_utc, "
                        "created_at, reason) VALUES (?,?,?,?,?)",
                        (_mid, _tomb_start, _tomb_end, _now, _reason))

        return {
            "deleted":                n_blocks,
            "dates":                  n_dates,
            "generation_mix_deleted": mix_deleted,
            "segments_deleted":       segments_deleted,
            "reseeded":               bool(is_tail and n_blocks),
            # set only for a device-only delete with surviving parent blocks
            "recompute_parent":       recompute_parent if n_blocks else None,
            "recompute_from":         utc_start if recompute_parent and n_blocks else None,
            "recompute_to":           utc_end if recompute_parent and n_blocks else None,
        }

    def count_blocks_for_date_range(
        self, from_date: str, to_date: str, meter_id: str | None = None,
        from_time: str = "00:00", to_time: str = "23:59", *, tz_name: str,
        reconstructed_only: bool = False, utc_span: tuple | None = None
    ) -> dict:
        """
        Preview how many blocks delete_blocks_for_date_range would remove, using
        the identical meter resolution (sentinel + sub-meter inclusion) and WHERE
        clause so the preview can never disagree with the delete. Honours
        `reconstructed_only` identically to the delete.
        Returns {"blocks": N, "dates": N_distinct_dates}.
        """
        from_time = from_time or "00:00"
        to_time   = to_time   or "23:59"

        # CONTIGUOUS span — identical bounds to the delete so the preview can never
        # disagree with what actually gets removed.
        utc_start, utc_end, end_incl = (self._utc_span_bounds(utc_span) if utc_span else
                                        self._delete_range_bounds(
                                            from_date, to_date, from_time, to_time, tz_name))
        meter_ids = self._resolve_delete_meters(meter_id)
        where, params = self._block_range_where(
            utc_start, utc_end, end_incl, meter_ids, reconstructed_only
        )

        n_blocks, n_dates = self._count_blocks_and_local_dates(where, params, tz_name)
        return {"blocks": n_blocks, "dates": n_dates}

    def count_imported_history(self) -> dict:
        """How much historical-import data exists: block count + date span."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n, MIN(block_start) AS lo, MAX(block_start) AS hi "
            "FROM blocks WHERE source LIKE 'imported%'"
        ).fetchone()
        return {"blocks": (row["n"] or 0) if row else 0,
                "from": row["lo"] if row else None,
                "to":   row["hi"] if row else None}

    def purge_imported_history(self) -> dict:
        """The get-out-clause: remove ALL historical-import data and everything
        derived from it, returning the account to its live-only state so a fresh
        import can start clean.

        Removes: imported blocks (``source LIKE 'imported%'`` — WHOLE blocks, all
        channels, so no per-meter apportionment is left stale) and their
        `generation_mix`; the CSV/API **rate derivations**; and the import
        **checkpoints + gap records** in `kraken_state`. Also clears any pending
        region-reconcile prompt and re-arms the carbon backfill. Live/CAD/settled
        blocks are untouched — imports never overlap them (go-live ceiling).
        """
        info = self.count_imported_history()
        with self._conn:
            mix = self._conn.execute(
                "DELETE FROM generation_mix WHERE block_id IN "
                "(SELECT id FROM blocks WHERE source LIKE 'imported%')").rowcount
            # P3.1: block_segments is keyed by block_start (no FK) — clean it too.
            segs = self._conn.execute(
                "DELETE FROM block_segments WHERE (block_start, meter_id) IN "
                "(SELECT block_start, meter_id FROM blocks WHERE source LIKE 'imported%')").rowcount
            self._conn.execute("DELETE FROM blocks WHERE source LIKE 'imported%'")
            derivs = self._conn.execute(
                "DELETE FROM historical_derivation WHERE source LIKE 'imported%'").rowcount
            try:
                self._conn.execute(
                    "DELETE FROM kraken_state WHERE key IN "
                    "('api_import_oldest_import','api_import_done_import',"
                    " 'api_import_oldest_export','api_import_done_export',"
                    " 'import_gaps_import','import_gaps_export')")
            except Exception:
                pass   # table/keys absent → nothing to reset
        # Clear the post-import region prompt and let carbon re-scan the survivors.
        self.set_meta("region_reconcile_pending", None)
        self.rearm_carbon_backfill()
        logger.info("purge_imported_history: removed %d block(s), %d derivation(s)",
                    info["blocks"], derivs)
        return {"blocks": info["blocks"], "generation_mix": mix,
                "block_segments": segs,
                "derivations": derivs, "from": info["from"], "to": info["to"]}

    # ── Recorder device attribution — reversible layer ────────────────────────

    def delete_recorder_attributed(self, meter_id=None, from_iso=None,
                                   to_iso=None) -> dict:
        """Delete recorder-attributed device blocks, optionally scoped to a meter
        and/or a [from, to) window. The undo primitive: removes ONLY the
        reconstructed device rows (source='recorder_attributed'), never live or
        imported house totals. Returns {deleted, meters, parents, from, to} — the
        caller recomputes each affected parent's remainder over [from, to)."""
        where = ["source = ?"]
        params = [self.RECORDER_ATTRIBUTED_SOURCE]
        if meter_id:
            where.append("meter_id = ?"); params.append(meter_id)
        if from_iso:
            where.append("block_start >= ?"); params.append(from_iso)
        if to_iso:
            where.append("block_start < ?"); params.append(to_iso)
        clause = " AND ".join(where)
        meters = [r["meter_id"] for r in self._conn.execute(
            f"SELECT DISTINCT meter_id FROM blocks WHERE {clause}", params).fetchall()]
        parents = []
        for mid in meters:
            pr = self._conn.execute(
                "SELECT parent_meter_id FROM meters WHERE meter_id = ? "
                "AND parent_meter_id IS NOT NULL LIMIT 1", (mid,)).fetchone()
            p = pr["parent_meter_id"] if pr else None
            if p and p not in parents:
                parents.append(p)
        span = self._conn.execute(
            f"SELECT MIN(block_start) lo, MAX(block_start) hi FROM blocks WHERE {clause}",
            params).fetchone()
        with self._conn:
            # P3.1: block_segments keyed by block_start (no FK) — clean before blocks.
            self._conn.execute(
                f"DELETE FROM block_segments WHERE (block_start, meter_id) IN "
                f"(SELECT block_start, meter_id FROM blocks WHERE {clause})", params)
            cur = self._conn.execute(f"DELETE FROM blocks WHERE {clause}", params)
        return {"deleted": cur.rowcount, "meters": meters, "parents": parents,
                "from": span["lo"] if span else None,
                "to": span["hi"] if span else None}

    def get_parent_meter_id(self, meter_id: str):
        """The parent meter_id of a sub-meter (or None). Falls back to the main
        (non-sub) meter when the sub-meter row has no explicit parent."""
        r = self._conn.execute(
            "SELECT parent_meter_id FROM meters WHERE meter_id = ? "
            "AND parent_meter_id IS NOT NULL LIMIT 1", (meter_id,)).fetchone()
        if r and r["parent_meter_id"]:
            return r["parent_meter_id"]
        main = self._conn.execute(
            "SELECT meter_id FROM meters WHERE is_sub_meter = 0 LIMIT 1").fetchone()
        return main["meter_id"] if main else None

    def get_device_live_coverage_start(self, meter_id: str):
        """The earliest block_start where *meter_id* has REAL (non-reconstructed)
        import data — i.e. any block whose source is not 'recorder_attributed' AND
        whose import is actually non-zero. This is the seam where a device's
        live/imported history begins; recorder attribution fills up to (but not
        into) it so the reconstructed layer butts cleanly against the real one.

        Crucially we require imp_kwh > 0: a *live* block that recorded zero import
        is a hole, not coverage (e.g. a sub-meter sensor that flat-lined at 0 for a
        stretch while the device was really drawing — the load then sits unsplit in
        the house remainder). Treating those leading zeros as 'not yet covered'
        advances the seam past them so attribution can heal them from the recorder
        instead of stopping short. Returns None when the device has no non-zero live
        history at all, in which case attribution may fill the full available range."""
        r = self._conn.execute(
            "SELECT MIN(block_start) AS lo FROM blocks "
            "WHERE meter_id = ? AND COALESCE(source, '') <> ? "
            "AND COALESCE(imp_kwh, 0) > 0",
            (meter_id, self.RECORDER_ATTRIBUTED_SOURCE)).fetchone()
        return r["lo"] if r and r["lo"] else None

    def sum_meter_import_kwh(self, meter_id: str, from_iso: str, to_iso: str) -> float:
        """Sum of a meter's raw import kWh over [from_iso, to_iso] (inclusive end).
        Used to sanity-check an attribution: a device can't consume more than the
        house drew from the grid over the same window."""
        r = self._conn.execute(
            "SELECT COALESCE(SUM(imp_kwh), 0.0) FROM blocks "
            "WHERE meter_id = ? AND block_start >= ? AND block_start <= ?",
            (meter_id, from_iso, to_iso)).fetchone()
        return float(r[0] or 0.0)

    def count_recorder_attributed(self) -> dict:
        """Summary of the reconstructed device layer: total rows + per-meter span,
        for the attribution page / back-out UI."""
        rows = self._conn.execute(
            "SELECT meter_id, COUNT(*) n, MIN(block_start) lo, MAX(block_start) hi "
            "FROM blocks WHERE source = ? GROUP BY meter_id ORDER BY meter_id",
            (self.RECORDER_ATTRIBUTED_SOURCE,)).fetchall()
        return {"total": sum(r["n"] for r in rows),
                "meters": [{"meter_id": r["meter_id"], "blocks": r["n"],
                            "from": r["lo"], "to": r["hi"]} for r in rows]}

    def record_attribution_run(self, run: dict) -> None:
        """Append a run to the attribution ledger (run_id, meter_id, span, sensors,
        created_at, blocks_written) so a user can see and undo each run."""
        runs = self.get_meta(self._ATTRIBUTION_RUNS_KEY, None) or []
        runs.append(dict(run))
        self.set_meta(self._ATTRIBUTION_RUNS_KEY, runs[-200:])

    def get_attribution_runs(self) -> list:
        return self.get_meta(self._ATTRIBUTION_RUNS_KEY, None) or []

    def remove_attribution_run(self, run_id: str) -> None:
        runs = self.get_meta(self._ATTRIBUTION_RUNS_KEY, None) or []
        self.set_meta(self._ATTRIBUTION_RUNS_KEY,
                      [r for r in runs if r.get("run_id") != run_id])

    # ── 3.5.0 Historical-import derivation provenance ─────────────────────────

    @staticmethod
    def _row_to_derivation(row) -> dict:
        """sqlite Row → dict, decoding the JSON columns (sensor_ids, params)."""
        d = dict(row)
        for k in ("sensor_ids", "params"):
            v = d.get(k)
            if v:
                try:
                    d[k] = json.loads(v)
                except (ValueError, TypeError):
                    pass  # leave raw string if it isn't valid JSON
        return d

    def insert_historical_derivation(
        self, scope: str, period_from: str, period_to: str, *,
        subject: str | None = None, sensor_ids=None, sensor_kind: str | None = None,
        params=None, derived_value: float | None = None,
        confirmed_value: float | None = None, method_version: int = 1,
        derived_at: str | None = None, source: str | None = None,
        notes: str | None = None,
    ) -> int:
        """Record one reconstruction (a device-attribution span, or a rate for a
        tariff-period×tier) and return its id. `sensor_ids`/`params` may be lists
        or dicts (stored as JSON). Pure insert — linking blocks is a separate
        step (tag_blocks_with_derivation)."""
        if scope not in ("device_attribution", "rate"):
            raise ValueError(f"invalid derivation scope: {scope!r}")
        cur = self._conn.execute(
            """INSERT INTO historical_derivation
                 (scope, subject, period_from, period_to, sensor_ids, sensor_kind,
                  params, derived_value, confirmed_value, method_version,
                  derived_at, source, superseded_by, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)""",
            (scope, subject, period_from, period_to,
             json.dumps(sensor_ids) if sensor_ids is not None else None,
             sensor_kind,
             json.dumps(params) if params is not None else None,
             derived_value, confirmed_value, int(method_version),
             derived_at or _utc_now_iso(), source, notes),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_historical_derivation(self, derivation_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM historical_derivation WHERE id = ?",
            (derivation_id,)).fetchone()
        return self._row_to_derivation(row) if row else None

    def list_historical_derivations(
        self, *, scope: str | None = None, subject: str | None = None,
        current_only: bool = True,
    ) -> list[dict]:
        """List derivations, newest first. `current_only` hides superseded rows
        (the audit trail is kept, not deleted)."""
        clauses, params = [], []
        if scope:
            clauses.append("scope = ?"); params.append(scope)
        if subject is not None:
            clauses.append("subject = ?"); params.append(subject)
        if current_only:
            clauses.append("superseded_by IS NULL")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM historical_derivation{where} "
            "ORDER BY derived_at DESC, id DESC", params).fetchall()
        return [self._row_to_derivation(r) for r in rows]

    def supersede_historical_derivation(self, old_id: int, new_id: int) -> None:
        """Mark `old_id` replaced by `new_id` (rebuild). Keeps the old row for
        audit; list_historical_derivations(current_only=True) then hides it."""
        self._conn.execute(
            "UPDATE historical_derivation SET superseded_by = ? WHERE id = ?",
            (new_id, old_id))
        self._conn.commit()

    def tag_blocks_with_derivation(
        self, derivation_id: int, meter_id: str, period_from: str, period_to: str,
    ) -> int:
        """Point a meter's blocks in [period_from, period_to) at a derivation.
        Returns the number of blocks tagged. Used after writing reconstructed
        blocks so each can answer 'which sensor/rate produced this'."""
        cur = self._conn.execute(
            """UPDATE blocks SET derivation_id = ?
               WHERE meter_id = ? AND block_start >= ? AND block_start < ?""",
            (derivation_id, meter_id, period_from, period_to))
        self._conn.commit()
        return cur.rowcount

    def _imported_config_period_id(self, block_start: str):
        """Config period id for a reconstructed block: the covering period, or —
        for pre-EMT dates no live period covers — the OLDEST existing period (for
        its timezone/billing-day/block_minutes). Returns (id, block_minutes) or
        (None, 30)."""
        cp = self.get_config_period_for_date(block_start)
        if not cp:
            row = self._conn.execute(
                "SELECT id, block_minutes FROM config_periods "
                "ORDER BY effective_from ASC LIMIT 1").fetchone()
            if not row:
                return None, 30
            return row["id"], int(row["block_minutes"] or 30)
        return cp["id"], int(cp.get("block_minutes") or 30)

    def imported_block_replaceable(self, block_start: str, meter_id: str,
                                   channel: str) -> tuple:
        """May a bill/CSV import REPLACE this block? Returns (ok, reason).

        First-man-wins is the default for good reason — a CSV can silently clobber live
        readings a user can never get back. This is the one narrow exception: a period
        the supplier never costed, where what we hold is usage x published rate and the
        bill carries the real charges. Even then, three things are never overwritten:

          * a live / CAD / settled reading (source is not an import) — the meter is a
            better source of kWh than a bill, always;
          * a block the user manually corrected — their judgement outranks any import;
          * a slot with no existing data — that is an ordinary insert, not a replace.
        """
        kcol = "imp_kwh" if channel == "import" else "exp_kwh"
        row = self._conn.execute(
            f"SELECT {kcol} AS v, source, rate_corrected, rate_source "
            "FROM blocks WHERE block_start = ? AND meter_id = ?",
            (block_start, meter_id)).fetchone()
        if row is None or row["v"] is None:
            return (False, "empty")                 # nothing there — plain insert
        src = str(row["source"] or "")
        if not src.startswith("imported"):
            return (False, "live")                  # never clobber a real reading
        if row["rate_corrected"] or str(row["rate_source"] or "") == "corrected":
            return (False, "corrected")             # the user's own fix wins
        return (True, "replaceable")


    def upsert_imported_block(
        self, block_start: str, meter_id: str, channel: str, *,
        kwh: float, rate: float | None, cost: float | None,
        standing: float | None = None, source: str = IMPORTED_SOURCE_CSV,
        derivation_id: int | None = None, overwrite: bool = False,
        cost_exc=None,
    ) -> tuple[Optional[int], bool]:
        """Insert one reconstructed block for a single channel — FIRST-MAN-WINS.

        A CSV/bill import must never clobber a block that already exists: the user
        can't tell exactly where their CSV butts up against data EMT already holds,
        so the rule is that whoever wrote a channel first keeps it. If this
        (block_start, meter_id) already carries data for `channel`, it is LEFT
        UNTOUCHED (this protects live/settled readings AND a previous import). Only a
        channel that's currently empty is filled — so import + export of a genuinely
        new block both land, and a later export CSV can still fill the export column
        of an import-only row. To change an existing block, delete it first (Delete
        Blocks) and re-fill from any source. Pass `overwrite=True` to force replace.

        Import and export share ONE row; `cost` is inc-VAT £ (billing sums it), `rate`
        is £/kWh, `standing` is the day's charge. Returns (block_id, wrote) — wrote is
        False when the slot already held data and was left as-is."""
        kcol, rcol, ccol = (("imp_kwh", "imp_rate", "imp_cost") if channel == "import"
                            else ("exp_kwh", "exp_rate", "exp_cost"))
        cp_id, bm = self._imported_config_period_id(block_start)
        if cp_id is None:
            return None, False
        try:
            block_end = (datetime.fromisoformat(block_start)
                         + timedelta(minutes=bm)).isoformat()
        except ValueError:
            return None, False
        with self._conn:
            if not overwrite:
                ex = self._conn.execute(
                    f"SELECT id, {kcol} AS v FROM blocks "
                    "WHERE block_start = ? AND meter_id = ?",
                    (block_start, meter_id)).fetchone()
                if ex is not None and ex["v"] is not None:
                    return ex["id"], False        # first-man wins: leave it as-is
            else:
                # `overwrite` means "replace an IMPORTED, uncorrected block" — never a
                # live/settled reading or a user correction, whatever the caller asks
                # for. Callers already check (imported_block_replaceable); this makes
                # the invariant a property of the writer so it cannot be bypassed.
                ok, why = self.imported_block_replaceable(block_start, meter_id, channel)
                if not ok and why in ("live", "corrected"):
                    ex = self._conn.execute(
                        "SELECT id FROM blocks WHERE block_start = ? AND meter_id = ?",
                        (block_start, meter_id)).fetchone()
                    return (ex["id"] if ex else None), False
            is_import = channel == "import"
            # BL-23 (4.2 Slice D): persist the ex-VAT cost for the import channel (NULL
            # otherwise). Its rate, the standing charge's and their provenance are derived
            # where read (step 5b, 10 Oct 2026).
            exc_cols = ", imp_cost_exc" if is_import else ""
            exc_ph = ", ?" if is_import else ""
            exc_upd = (""",
                       imp_cost_exc = excluded.imp_cost_exc""" if is_import else "")
            _p = [block_start, block_end, meter_id, cp_id,
                  kwh, rate, cost, (standing or 0.0), source, derivation_id]
            if is_import:
                _p += [cost_exc]
            cur = self._conn.execute(
                f"""INSERT INTO blocks
                      (block_start, block_end, meter_id, config_period_id,
                       interpolated, {kcol}, {rcol}, {ccol}, standing_charge,
                       source, derivation_id{exc_cols})
                    VALUES (?,?,?,?,0,?,?,?,?,?,?{exc_ph})
                    ON CONFLICT(block_start, meter_id) DO UPDATE SET
                       {kcol} = excluded.{kcol},
                       {rcol} = excluded.{rcol},
                       {ccol} = excluded.{ccol},
                       standing_charge = CASE
                           WHEN excluded.standing_charge IS NULL
                                OR excluded.standing_charge = 0
                                THEN blocks.standing_charge   -- export (0) never clobbers
                           WHEN blocks.standing_charge IS NULL
                                OR blocks.standing_charge = 0
                           THEN excluded.standing_charge ELSE blocks.standing_charge END,
                       derivation_id = COALESCE(blocks.derivation_id, excluded.derivation_id){exc_upd}""",
                _p)
            row = self._conn.execute(
                "SELECT id FROM blocks WHERE block_start = ? AND meter_id = ?",
                (block_start, meter_id)).fetchone()
        return (row["id"] if row else cur.lastrowid), True

    def upsert_imported_blocks(
        self, rows: list, meter_id: str, channel: str, *,
        source: str = IMPORTED_SOURCE_CSV, derivation_id: int | None = None,
    ) -> int:
        """Bulk insert-or-merge reconstructed blocks for one channel in a SINGLE
        transaction (one commit for the whole batch), returning the count written.

        This is the hot path for the API import: committing per block (thousands
        of fsyncs) blocks the async event loop long enough to starve the HA
        WebSocket heartbeat → reconnect storm. One transaction per chunk fixes it.
        `rows`: list of {start, kwh, rate, cost, standing}. This is the API hot path
        and OVERWRITES on conflict (re-fetch corrects prices) — unlike the singular
        upsert_imported_block, which is first-man-wins for CSV/bill imports.
        Config-period id is cached per day."""
        is_import = channel == "import"
        kcol, rcol, ccol = (("imp_kwh", "imp_rate", "imp_cost") if is_import
                            else ("exp_kwh", "exp_rate", "exp_cost"))
        # BL-23 (4.2): persist the ex-VAT cost for the IMPORT channel when the row carries
        # it (NULL otherwise — inc figures unaffected). Its rate, the standing charge's
        # and their provenance are derived where read (step 5b, 10 Oct 2026).
        exc_cols = ", imp_cost_exc" if is_import else ""
        exc_ph = ", ?" if is_import else ""
        exc_upd = (""",
                      imp_cost_exc = excluded.imp_cost_exc""" if is_import else "")
        sql = (f"""INSERT INTO blocks
                     (block_start, block_end, meter_id, config_period_id,
                      interpolated, {kcol}, {rcol}, {ccol}, standing_charge,
                      source, derivation_id{exc_cols})
                   VALUES (?,?,?,?,0,?,?,?,?,?,?{exc_ph})
                   ON CONFLICT(block_start, meter_id) DO UPDATE SET
                      {kcol} = excluded.{kcol},
                      {rcol} = excluded.{rcol},
                      {ccol} = excluded.{ccol},
                      standing_charge = CASE
                          WHEN excluded.standing_charge IS NULL
                               OR excluded.standing_charge = 0
                               THEN blocks.standing_charge   -- export (0) never clobbers
                          WHEN blocks.source LIKE 'imported%'
                               OR blocks.standing_charge IS NULL
                               OR blocks.standing_charge = 0
                          THEN excluded.standing_charge ELSE blocks.standing_charge END,
                      source = excluded.source,
                      derivation_id = COALESCE(blocks.derivation_id, excluded.derivation_id){exc_upd}""")
        cp_cache: dict = {}
        written = 0
        with self._conn:      # ONE transaction / one commit for the whole batch
            for r in rows:
                start = r.get("start")
                if not start:
                    continue
                day = start[:10]
                if day not in cp_cache:
                    cp_cache[day] = self._imported_config_period_id(start)
                cp_id, bm = cp_cache[day]
                if cp_id is None:
                    continue
                try:
                    block_end = (datetime.fromisoformat(start)
                                 + timedelta(minutes=bm)).isoformat()
                except ValueError:
                    continue
                params = [
                    start, block_end, meter_id, cp_id,
                    r.get("kwh") or 0.0, r.get("rate"), r.get("cost"),
                    (r.get("standing") or 0.0), source, derivation_id]
                if is_import:      # BL-23 exc cost (NULL when the row omits it)
                    params += [r.get("cost_exc")]
                self._conn.execute(sql, params)
                written += 1
        return written

    # ── Import re-price repair queue ─────────────────────────────────────────
    # During the bulk import, some Measurements responses come back with the kWh
    # but no billed cost / TOU bucket (a degraded/partial response under load),
    # so those half-hours are priced from the tariff *schedule* (peak if they fall
    # outside the fixed off-peak window) instead of Octopus's real dispatch-aware
    # OFF_PEAK cost. We record each such slot here; a calm, targeted re-fetch
    # (engine.repair_import_pricing) later re-queries just these and re-prices in
    # place. The Measurements API DOES have the cost — proven by the cost probe —
    # so a small quiet query reliably returns it.
    def add_reprice_queue(self, channel: str, starts) -> None:
        """Queue imported block_starts (for `channel`) that were schedule-priced
        because Measurements returned no billed cost. Deduped, persisted in meta."""
        starts = [s for s in (starts or []) if s]
        if not starts:
            return
        q = self.get_meta("import_reprice_queue", None) or {}
        merged = set(q.get(channel) or []) | set(starts)
        q[channel] = sorted(merged)
        self.set_meta("import_reprice_queue", q)

    # An API-imported half-hour Octopus gave no cost for (19 Oct 2026, the user): EMT's kWh × rate,
    # and nothing asks for it again, so the Import bill hatches it at once. Recorded here because the
    # block cannot tell — an imported block's cost and rate read the same either way, and the reprice
    # queue empties at the next import. {slot: first noted, naive UTC}; a slot leaves when an import or
    # a repair gets Octopus's cost for it.
    _IMPORT_NO_COST_KEY = "import_no_cost"

    def import_no_cost_slots(self) -> dict:
        """{slot: first noted} of API-imported import half-hours with no Octopus cost."""
        try:
            d = json.loads(self.get_kraken_state(self._IMPORT_NO_COST_KEY) or "{}")
        except (TypeError, ValueError):
            d = {}
        return d if isinstance(d, dict) else {}

    def note_import_cost(self, no_cost=(), costed=()) -> None:
        """Record half-hours an import left without Octopus's cost, and drop those it, or a repair,
        got one for. One read and write of the record."""
        no_cost, costed = set(no_cost or ()), set(costed or ())
        if not (no_cost or costed):
            return
        d = self.import_no_cost_slots()
        before = dict(d)
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        for st in no_cost - costed:
            d.setdefault(st, now)
        for st in costed:
            d.pop(st, None)
        if d != before:
            self.set_kraken_state(self._IMPORT_NO_COST_KEY, json.dumps(d, sort_keys=True))

    # ── Deleting an imported export (soft delete design §4, PR 1, 19 Oct 2026) ───────────────────
    # The export of IMPORTED main-meter history (CSV, PDF, the API import) over whole LOCAL days —
    # a third-party or FIT export CSV that was wrong, over import that is fine. Whole days because
    # zero_fill_imported_export_blanks sets a NULL export to 0.0 on a day that still holds some
    # imported export. No shelf: the required backup is the way back (the user, 19 Oct 2026).
    def _local_days_utc(self, from_date: str, to_date: str, tz_name: str) -> tuple:
        """[local midnight of from_date, local midnight after to_date) in naive UTC."""
        nxt = (datetime.fromisoformat(to_date[:10]) + timedelta(days=1)).date().isoformat()
        return (local_datetime_to_utc(from_date[:10], "00:00", tz_name),
                local_datetime_to_utc(nxt, "00:00", tz_name))

    _EXPORT_PRESENT = "(exp_kwh IS NOT NULL OR exp_kwh_api IS NOT NULL OR exp_cost IS NOT NULL)"

    def count_imported_export(self, from_date: str, to_date: str, tz_name: str,
                              meter_id: str = "electricity_main") -> dict:
        """What deleting the imported export over those local days would take: the imported
        half-hours with an export, and the live ones left alone. Read-only."""
        a, b = self._local_days_utc(from_date, to_date, tz_name)
        row = self._conn.execute(
            "SELECT SUM(CASE WHEN source LIKE 'imported%' THEN 1 ELSE 0 END) AS imported, "
            "SUM(CASE WHEN source LIKE 'imported%' THEN 0 ELSE 1 END) AS live, "
            "SUM(CASE WHEN source LIKE 'imported%' THEN COALESCE(exp_kwh, exp_kwh_api, 0) "
            "    ELSE 0 END) AS kwh "
            "FROM blocks WHERE meter_id = ? AND block_start >= ? AND block_start < ? AND "
            + self._EXPORT_PRESENT, (meter_id, a, b)).fetchone()
        return {"from_utc": a, "to_utc": b, "half_hours": int(row["imported"] or 0),
                "live_kept": int(row["live"] or 0), "kwh": round(float(row["kwh"] or 0.0), 3)}

    def delete_imported_export(self, from_date: str, to_date: str, tz_name: str,
                               meter_id: str = "electricity_main") -> dict:
        """Blank the export of the imported half-hours over those local days, and recompute their
        carbon from import alone (imp_kwh × carbon_intensity_g, where the intensity is held — the
        carbon backfill selects on the intensity, so it would not). Live blocks are not touched.
        One transaction. Returns {half_hours, from_utc, to_utc}."""
        a, b = self._local_days_utc(from_date, to_date, tz_name)
        with self._tx():
            cur = self._conn.execute(
                "UPDATE blocks SET exp_kwh = NULL, exp_rate = NULL, exp_cost = NULL, "
                "exp_read_start = NULL, exp_read_end = NULL, exp_kwh_api = NULL, "
                "carbon_g = CASE WHEN carbon_intensity_g IS NOT NULL "
                "  THEN ROUND(COALESCE(imp_kwh, 0) * carbon_intensity_g, 4) ELSE carbon_g END "
                "WHERE meter_id = ? AND source LIKE 'imported%' AND block_start >= ? "
                "AND block_start < ? AND " + self._EXPORT_PRESENT, (meter_id, a, b))
            n = cur.rowcount
        return {"half_hours": n, "from_utc": a, "to_utc": b}

    def get_reprice_queue(self) -> dict:
        """{'import': [start,...], 'export': [...]} of slots awaiting re-price."""
        return self.get_meta("import_reprice_queue", None) or {}

    def reprice_queue_count(self) -> int:
        q = self.get_reprice_queue()
        return sum(len(v or []) for v in q.values())

    def clear_reprice_queue(self) -> None:
        """Empty the whole reprice queue — used when a NEW import starts, so the
        previous run's flagged slots don't carry over into the fresh run's health."""
        self.set_meta("import_reprice_queue", {})

    def clear_reprice_queue_slots(self, channel: str, starts) -> None:
        """Remove `starts` from the channel's queue (they've been re-priced)."""
        if not starts:
            return
        q = self.get_meta("import_reprice_queue", None) or {}
        if channel not in q:
            return
        remaining = sorted(set(q[channel]) - set(starts))
        if remaining:
            q[channel] = remaining
        else:
            q.pop(channel, None)
        self.set_meta("import_reprice_queue", q)

    def reprice_imported_block(self, start: str, meter_id: str, channel: str,
                               rate, cost, cost_exc=None) -> bool:
        """Update an IMPORTED block's rate + cost in place (source LIKE
        'imported%' only — never touches live/settled rows), ONLY when the value
        actually differs (so the returned rowcount = a genuine correction, which
        the repair pass uses as its 'recovered' signal). Returns True if changed.

        BL-23 (4.2 C3): when a re-fetch supplies the ex-VAT cost too (import channel),
        correct imp_cost_exc in the same UPDATE, so the repair/settlement
        reprice keeps the exc figure in step with the billed inc cost.

        "Settled" above meant DCC-settled live rows; `source` alone never covered an IMPORTED
        block settled from a bill or corrected by the user, which keeps `source='imported_*'`.
        16 Oct 2026 (bill and split paths design §4): the import cost repair, from the reprice
        queue and from the deferred verify pass, overwrote those — the bill (or the correction)
        replaced by the measurements' cost and `_billed_rate`, and the segments deleted, so the
        bill's split was rebuilt from the dispatch or lost. Now: on import, a block whose
        `rate_source` is 'settled' or 'corrected', or that carries `rate_corrected`, is left
        alone; on export, `rate_corrected` (corrections stamp it for an export rate too)."""
        rcol, ccol = (("imp_rate", "imp_cost") if channel == "import"
                      else ("exp_rate", "exp_cost"))
        set_exc, extra = "", []
        if channel == "import" and cost_exc is not None:
            set_exc = ", imp_cost_exc = ?"
            extra = [cost_exc]
        with self._conn:
            cur = self._conn.execute(
                f"UPDATE blocks SET {rcol} = ?, {ccol} = ?{set_exc} "
                f"WHERE block_start = ? AND meter_id = ? AND source LIKE 'imported%' "
                f"AND COALESCE(rate_corrected, 0) = 0 "
                + ("AND COALESCE(rate_source, '') NOT IN ('settled', 'corrected') "
                   if channel == "import" else "")
                + f"AND (COALESCE({rcol}, -1) != ? OR COALESCE({ccol}, -1) != ?)",
                (rate, cost, *extra, start, meter_id, rate, cost))
            # G2: a real price change (a gap filled or a price corrected) invalidates any stale
            # segment so the reprice sweep rebuilds it from the corrected column — keeps the block
            # rewritable at any later date. Import-only (segments are import-only); no-op if none.
            if cur.rowcount > 0 and channel == "import":
                self._conn.execute(
                    "DELETE FROM block_segments WHERE block_start = ? AND meter_id = ? "
                    "AND channel = 'import'", (start, meter_id))
        return cur.rowcount > 0

    def set_block_carbon(self, block_start: str, meter_id: str,
                         intensity_g, carbon_g) -> bool:
        """Write carbon IN PLACE for one block+meter — a targeted UPDATE of only
        the carbon columns. Critically it does NOT round-trip the whole row: the
        read→mutate→append_block_replace path drops `source` (and imp_kwh_api /
        is_provisional / derivation_id, and re-homes config_period_id), which
        silently untagged every imported block the carbon backfill touched. This
        preserves the tag and everything else. Returns True if a row changed."""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE blocks SET carbon_intensity_g = ?, carbon_g = ? "
                "WHERE block_start = ? AND meter_id = ?",
                (intensity_g, carbon_g, block_start, meter_id))
        return cur.rowcount > 0

    def set_block_carbon_many(self, rows) -> int:
        """set_block_carbon for many (block_start, meter_id, intensity_g, carbon_g)
        rows in ONE transaction (#65, 3 Oct 2026): carbon recovery committed once per
        block and meter, ~37 ms each on the Pi's SD card, on the engine loop. Returns
        the rows changed."""
        n = 0
        with self._conn:
            for bs, mid, ig, cg in rows:
                n += self._conn.execute(
                    "UPDATE blocks SET carbon_intensity_g = ?, carbon_g = ? "
                    "WHERE block_start = ? AND meter_id = ?", (ig, cg, bs, mid)).rowcount > 0
        return n

    def get_import_blocks_missing_exc(self, limit: int = 2000,
                                      after_start: str | None = None) -> list:
        """4.2 exc historical backfill: page IMPORT blocks that carry energy but no
        ex-VAT cost yet (imp_cost_exc IS NULL, imp_kwh > 0). Covers both historically
        IMPORTED blocks (source LIKE 'imported%') AND SETTLED live blocks
        (imp_kwh_api IS NOT NULL) — the latter were captured live before ex-VAT
        existed and have already settled via DCC, so settlement capture will never
        re-stamp them; without this they stay on the inc÷VAT approximation forever.
        Truly-unsettled live blocks are excluded — they get real exc at settlement.
        Ordered by block_start; pass the last start back as `after_start` to continue.
        Returns [{start, meter_id, kwh, rate}]."""
        sql = ("SELECT block_start, meter_id, imp_kwh, imp_rate FROM blocks "
               "WHERE imp_cost_exc IS NULL AND imp_kwh IS NOT NULL AND imp_kwh > 0 "
               "AND (source LIKE 'imported%' OR imp_kwh_api IS NOT NULL)")
        params: list = []
        if after_start is not None:
            sql += " AND block_start > ?"
            params.append(after_start)
        sql += " ORDER BY block_start LIMIT ?"
        params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [{"start": r["block_start"], "meter_id": r["meter_id"],
                 "kwh": r["imp_kwh"], "rate": r["imp_rate"]} for r in rows]

    def count_import_blocks_missing_exc(self) -> int:
        """Count of import blocks still lacking ex-VAT cost — the backfill's
        remaining-work signal (0 ⇒ mark the pass done). Same coverage as
        get_import_blocks_missing_exc: imported OR settled live blocks."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM blocks WHERE imp_cost_exc IS NULL "
            "AND imp_kwh IS NOT NULL AND imp_kwh > 0 "
            "AND (source LIKE 'imported%' OR imp_kwh_api IS NOT NULL)"
        ).fetchone()
        return int(row["n"]) if row else 0

    def set_block_exc(self, block_start: str, meter_id: str, cost_exc) -> bool:
        """Write ex-VAT import figures IN PLACE for one block+meter — a targeted
        UPDATE of only the exc columns (mirrors set_block_carbon; no row round-trip,
        so source / imp_kwh_api / derivation_id are preserved). Only fills a NULL
        imp_cost_exc (idempotent — never overwrites captured exc) and NEVER touches
        the inc columns, so billing figures are unchanged. Returns True if a row
        changed."""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE blocks SET imp_cost_exc = ? "
                "WHERE block_start = ? AND meter_id = ? AND imp_cost_exc IS NULL "
                "AND (source LIKE 'imported%' OR imp_kwh_api IS NOT NULL)",
                (cost_exc, block_start, meter_id))
        return cur.rowcount > 0

    def set_blocks_exc(self, updates) -> int:
        """Batch ex-VAT writer: apply many `set_block_exc` UPDATEs inside ONE
        transaction (a single commit/fsync for the whole chunk, not one per block —
        the per-block commit made the historical backfill a minutes-long fsync storm
        that blocked the event loop). `updates` = iterable of
        (block_start, meter_id, cost_exc). Same NULL-only
        guard and coverage (imported OR settled live) as set_block_exc. Returns the
        number of rows changed."""
        changed = 0
        with self._tx():
            for bs, mid, ce in updates:
                cur = self._conn.execute(
                    "UPDATE blocks SET imp_cost_exc = ? "
                    "WHERE block_start = ? AND meter_id = ? AND imp_cost_exc IS NULL "
                    "AND (source LIKE 'imported%' OR imp_kwh_api IS NOT NULL)",
                    (ce, bs, mid))
                changed += cur.rowcount
        return changed

    # ── BL-9 IOG house/EV split historical backfill ─────────────────────────
    # The seam (_apply_iog_split) only runs when a block is finalised or repriced
    # at settlement, so blocks priced BEFORE the split shipped — the whole existing
    # dispatch history, plus any block already DCC-settled — never got the EV/Home
    # columns. These three mirror the ex-VAT backfill trio so a one-shot startup pass
    # can carve the split onto that history without a re-import.

    def set_blocks_iog_split(self, updates) -> int:
        """The history sweep's EV/Home split writer: many blocks in ONE transaction (one fsync
        per chunk, not per block). `updates` = iterable of
        (block_start, meter_id, segments, needs_segments).
        ADDITIVE ONLY — fills a split only where the block has none (idempotent; never
        re-carves) and NEVER touches imp_rate/imp_cost, so the settled inc bill stays
        byte-identical. Returns the number of splits filled.

        Step 5c (10 Oct 2026): the split IS the segments, so filling it is writing them. A
        block that already has an EV split keeps its segments unless `needs_segments` says it
        had none at all (then it gets them, as before)."""
        changed = 0
        with self._tx():
            for bs, mid, segs, needs in updates:
                row = self._conn.execute(
                    "SELECT " + _evsplit.sql_kwh("blocks") + " IS NULL AS open FROM blocks "
                    "WHERE block_start = ? AND meter_id = ?", (bs, mid)).fetchone()
                if row is None or segs is None:
                    continue
                if row["open"]:
                    changed += 1
                if row["open"] or needs:
                    self._replace_segments(bs, mid, segs)
        return changed

    # ── BL-27 priced segments — the block's grid import as priced slices ─────────────

    def set_block_segments(self, block_start: str, meter_id: str, segments,
                           channel: str = "import") -> int:
        """Replace the priced segments for one block/meter/channel. `segments` is an
        iterable of (kwh, inc_rate, exc_rate, band, attribution) — e.g. a list of
        pricing_segments.Segment. Written in ONE transaction (delete-then-insert, ordered
        by position), so the set is always consistent. Returns the number of rows written."""
        with self._tx():
            return self._replace_segments(block_start, meter_id, segments, channel)

    def _replace_segments(self, block_start, meter_id, segments, channel="import") -> int:
        """set_block_segments' body, inside the CALLER's transaction — for a writer that must
        commit its segments together with the columns they describe (BL-79)."""
        self._conn.execute(
            "DELETE FROM block_segments WHERE block_start = ? AND meter_id = ? "
            "AND channel = ?", (block_start, meter_id, channel))
        n = 0
        for seq, seg in enumerate(segments or []):
            kwh, inc_rate, exc_rate, band, attribution = seg
            self._conn.execute(
                "INSERT INTO block_segments (block_start, meter_id, channel, seq, "
                "kwh, inc_rate, exc_rate, band, attribution) VALUES (?,?,?,?,?,?,?,?,?)",
                (block_start, meter_id, channel, seq, kwh, inc_rate, exc_rate,
                 band, attribution))
            n += 1
        return n

    def get_block_segments(self, block_start: str, meter_id: str,
                           channel: str = "import") -> list:
        """Priced segments for one block/meter/channel, in position order — a list of
        {kwh, inc_rate, exc_rate, band, attribution}. Empty when none stored (the caller
        then falls back to the legacy imp_* columns during the migration)."""
        rows = self._conn.execute(
            "SELECT kwh, inc_rate, exc_rate, band, attribution FROM block_segments "
            "WHERE block_start = ? AND meter_id = ? AND channel = ? ORDER BY seq",
            (block_start, meter_id, channel)).fetchall()
        return [{"kwh": r["kwh"], "inc_rate": r["inc_rate"], "exc_rate": r["exc_rate"],
                 "band": r["band"], "attribution": r["attribution"]} for r in rows]

    def count_block_segments(self) -> int:
        """Total stored segment rows — the segment backfill's remaining-work signal."""
        return int(self._conn.execute(
            "SELECT COUNT(*) AS n FROM block_segments").fetchone()["n"])

    def get_blocks_missing_segments(self, limit: int = 2000,
                                    after_start: str | None = None) -> list:
        """Page MAIN-meter import blocks that carry energy but have no priced segments yet
        (BL-27 backfill). Sub-meters are excluded — segments live on the main import; a
        device's share is attributed from them at read time. Ordered by block_start; pass
        the last start back as `after_start` to continue."""
        sql = ("SELECT b.block_start, b.meter_id, b.imp_kwh, b.imp_cost, b.imp_rate, "
               "b.source, b.imp_kwh_api FROM blocks b "
               "WHERE b.imp_kwh IS NOT NULL AND b.imp_kwh > 0 "
               "AND b.meter_id NOT IN (SELECT meter_id FROM meters WHERE is_sub_meter = 1) "
               "AND NOT EXISTS (SELECT 1 FROM block_segments s "
               "                WHERE s.block_start = b.block_start AND s.meter_id = b.meter_id "
               "                AND s.channel = 'import')")
        params: list = []
        if after_start is not None:
            sql += " AND b.block_start > ?"
            params.append(after_start)
        sql += " ORDER BY b.block_start LIMIT ?"
        params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def count_blocks_missing_segments(self) -> int:
        """Count of MAIN-meter blocks still lacking priced segments — remaining-work signal."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM blocks b "
            "WHERE b.imp_kwh IS NOT NULL AND b.imp_kwh > 0 "
            "AND b.meter_id NOT IN (SELECT meter_id FROM meters WHERE is_sub_meter = 1) "
            "AND NOT EXISTS (SELECT 1 FROM block_segments s "
            "                WHERE s.block_start = b.block_start AND s.meter_id = b.meter_id "
            "                AND s.channel = 'import')").fetchone()
        return int(row["n"]) if row else 0

    # ── P3.3c: unified "needs reprice" coverage — a true SUPERSET of the three legacy backfills ──
    _NEEDS_REPRICE_WHERE = (
        "b.imp_kwh IS NOT NULL AND b.imp_kwh > 0 "
        # G1: only PRICED blocks are segmentable. A gap (imp_cost NULL = unknown price) is left
        # honestly unsegmented — never faked at £0, never stalls the sweep. A genuine free slot
        # (imp_cost 0, not NULL) is still included. Filling a gap flips imp_cost non-NULL, which
        # re-admits the block here so the sweep segments it from the real price (the fill-later door).
        "AND b.imp_cost IS NOT NULL "
        "AND b.meter_id NOT IN (SELECT meter_id FROM meters WHERE is_sub_meter = 1) "
        "AND ("
        "  NOT EXISTS (SELECT 1 FROM block_segments s WHERE s.block_start = b.block_start "
        "              AND s.meter_id = b.meter_id AND s.channel = 'import') "
        "  OR (b.imp_cost_exc IS NULL AND (b.source LIKE 'imported%' OR b.imp_kwh_api IS NOT NULL))"
        ")")

    def get_blocks_needing_reprice(self, limit: int = 2000,
                                   after_start: str | None = None) -> list:
        """Page MAIN-meter import blocks that need ANY derived field the sweep owns: missing
        priced segments, OR (imported/DCC-settled) missing ex-VAT. This is the SUPERSET the
        unified sweep walks so it fully covers the retired segment/iog_split/exc backfills.
        Each row carries `needs_segments` (1 = no segments yet) so the driver only (re)writes
        segments when they are ABSENT — split/exc use fill-null-only writers and are always safe."""
        sql = ("SELECT b.block_start, b.meter_id, b.imp_kwh, b.imp_cost, b.imp_rate, "
               "b.source, b.imp_kwh_api, "
               "(NOT EXISTS (SELECT 1 FROM block_segments s WHERE s.block_start = b.block_start "
               "             AND s.meter_id = b.meter_id AND s.channel = 'import')) AS needs_segments "
               "FROM blocks b WHERE " + self._NEEDS_REPRICE_WHERE)
        params: list = []
        if after_start is not None:
            sql += " AND b.block_start > ?"
            params.append(after_start)
        sql += " ORDER BY b.block_start LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def count_blocks_needing_reprice(self) -> int:
        """Remaining-work signal for the unified sweep (superset: missing segments OR
        imported/settled missing exc)."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM blocks b WHERE " + self._NEEDS_REPRICE_WHERE).fetchone()
        return int(row["n"]) if row else 0

    def get_vat_calendar(self) -> list:
        """Learned VAT-rate boundaries [(effective_from, rate), …] from store_meta.
        Merged with the statutory seed by vat_calendar.resolve_vat; empty until the
        engine observes a VAT change in the tariff."""
        raw = self.get_meta("vat_calendar", []) or []
        out = []
        for e in raw:
            try:
                d, r = e
                out.append((str(d)[:10], float(r)))
            except Exception:
                continue
        return out

    def set_vat_calendar(self, entries) -> None:
        """Persist the learned VAT boundaries (list of (date, rate))."""
        self.set_meta("vat_calendar",
                      [[str(d)[:10], float(r)] for d, r in (entries or [])])

    def vat_rate_lookup(self):
        """`vat_rate_at` as a function of the date, with the calendar read once — for a
        caller asking about every half-hour of a period (#48, 2 Oct 2026: a year's bill
        called `vat_rate_at` 5,440 times, each a read of the calendar)."""
        import vat_calendar as _vc
        return _vc.rate_lookup(self.get_vat_calendar())

    def vat_rate_at(self, date_iso) -> float:
        """The VAT rate effective at `date_iso` from the seed + learned calendar —
        the fallback rate used where no per-slot inc/exc pair is available."""
        import vat_calendar as _vc
        return _vc.resolve_vat(date_iso, self.get_vat_calendar())

    def set_import_exc_from_inc(self, block_starts, meter_id: str = "electricity_main") -> int:
        """BL-57b: authoritatively (re)derive block + segment ex-VAT from the stored INC
        via the VAT calendar (per-block `vat_rate_at`). The Cost-Corrections tool takes an
        inc-VAT rate; this yields a correct, EXPLICITLY-stored exc rather than a proportional
        rescale of the old ratio — and, crucially, sets exc even when it was NULL (so the
        view no longer falls back to the inc÷1.05 approximation). Exact for a flat charge and
        correct across a VAT-rate change (each block uses its own date's rate). Import main
        meter + its import segments. Returns the number of block rows updated."""
        if not block_starts:
            return 0
        seg = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='block_segments'").fetchone()
        n = 0
        with self._tx():
            for bs in block_starts:
                f = 1.0 / (1.0 + self.vat_rate_at(bs))
                cur = self._conn.execute(
                    "UPDATE blocks SET imp_cost_exc = ROUND(imp_cost * ?, 6) "
                    "WHERE block_start = ? AND meter_id = ? AND imp_rate IS NOT NULL",
                    (f, bs, meter_id))
                n += cur.rowcount
                if seg:
                    self._conn.execute(
                        "UPDATE block_segments SET exc_rate = ROUND(inc_rate * ?, 6) "
                        "WHERE channel = 'import' AND block_start = ?", (f, bs))
        return n

    def retag_untagged_imports(self) -> dict:
        """Repair blocks the carbon round-trip bug wiped from 'imported_api' to
        NULL. Anchor go-live to the earliest LIVE-signature block (a real meter
        read, or a kraken_api source); anything BEFORE it that is NULL-source with
        no meter read is reconstruction, so re-tag it 'imported_api'. Never touches
        live/settled rows. Idempotent. Returns {retagged, go_live}."""
        row = self._conn.execute(
            "SELECT MIN(block_start) AS gl FROM blocks "
            "WHERE imp_read_start IS NOT NULL OR source = 'kraken_api'").fetchone()
        go_live = row["gl"] if row else None
        if not go_live:
            return {"retagged": 0, "go_live": None}
        with self._conn:
            cur = self._conn.execute(
                "UPDATE blocks SET source = 'imported_api' "
                "WHERE source IS NULL AND block_start < ? AND imp_read_start IS NULL",
                (go_live,))
        logger.info("retag_untagged_imports: re-tagged %d block(s) before go-live %s",
                    cur.rowcount, go_live)
        return {"retagged": cur.rowcount, "go_live": go_live}


    def reprice_imported_blocks_from_csv(self, csv_text: str,
                                         meter_id: str = "electricity_main",
                                         channel: str = "import") -> dict:
        """Overlay EXACT billed cost + rate onto imported blocks from a filled CSV —
        the billing source of truth. For dispatch slots Octopus's Measurements API
        returns with NO cost/label (so they can't be priced via the API, only via
        the bill), this is the only way to match the bill.

        Shares the SAME name-matched parser as the main CSV import
        (csv_import.parse_octopus_csv), so this path accepts the exact same
        template — including the rate-first cost derivation (a Unit Rate column
        wins over an explicit Cost column). Columns are matched by header name, not
        position, so column order doesn't matter. The block's rate is then stored as
        cost ÷ kWh. Only imported blocks are touched, only where the value differs.
        Returns {checked, changed, from, to}.
        """
        import csv_import as _ci
        rcol, ccol = (("imp_rate", "imp_cost") if channel == "import"
                      else ("exp_rate", "exp_cost"))
        parsed = _ci.parse_octopus_csv(csv_text, channel)
        checked = changed = 0
        lo = hi = None
        changed_starts = []
        with self._conn:
            for b in (parsed.get("blocks") or []):
                start = b.get("block_start")
                kwh = b.get("kwh")
                cost = b.get("cost")
                if not start or cost is None:     # a row with neither rate nor cost
                    continue
                cost = round(float(cost), 6)
                rate = round(cost / kwh, 6) if (kwh and kwh > 0) else None
                checked += 1
                lo = start if (lo is None or start < lo) else lo
                hi = start if (hi is None or start > hi) else hi
                cur = self._conn.execute(
                    f"UPDATE blocks SET {rcol} = ?, {ccol} = ? "
                    f"WHERE block_start = ? AND meter_id = ? AND source LIKE 'imported%' "
                    f"AND (COALESCE({rcol}, -1) != ? OR COALESCE({ccol}, -1) != ?)",
                    (rate, cost, start, meter_id, rate, cost))
                if cur.rowcount > 0:
                    changed += 1
                    changed_starts.append(start)
        if changed_starts:
            self.clear_reprice_queue_slots(channel, changed_starts)
            # G2: invalidate segments for CSV-filled/corrected import blocks so the sweep rebuilds.
            if channel == "import":
                _ph = ",".join("?" * len(changed_starts))
                with self._conn:
                    self._conn.execute(
                        f"DELETE FROM block_segments WHERE channel = 'import' AND meter_id = ? "
                        f"AND block_start IN ({_ph})", [meter_id] + changed_starts)
        logger.info("reprice_imported_blocks_from_csv[%s]: %d changed of %d (%s..%s)",
                    channel, changed, checked, lo, hi)
        return {"checked": checked, "changed": changed, "from": lo, "to": hi}

    def get_imported_block_starts(self, from_iso: str, to_iso: str,
                                  meter_id: str = "electricity_main") -> list:
        """block_starts of IMPORTED blocks in [from, to) for a meter — the target
        set for a range-based re-price reconcile that runs WITHOUT a re-import."""
        rows = self._conn.execute(
            "SELECT block_start FROM blocks WHERE source LIKE 'imported%' "
            "AND meter_id = ? AND block_start >= ? AND block_start < ? "
            "ORDER BY block_start", (meter_id, from_iso, to_iso)).fetchall()
        return [r["block_start"] for r in rows]

    def get_imported_block_pricing(self, from_iso: str, to_iso: str,
                                   meter_id: str = "electricity_main") -> list:
        """Per-block CURRENT pricing for IMPORTED blocks in [from, to) — the cheap
        local read a suspect-only reprice uses to skip already-correct slots without
        touching the API. Returns dicts: block_start + imp/exp rate, kwh, cost."""
        rows = self._conn.execute(
            "SELECT block_start, imp_rate, imp_kwh, imp_cost, exp_rate, exp_kwh, exp_cost "
            "FROM blocks WHERE source LIKE 'imported%' AND meter_id = ? "
            "AND block_start >= ? AND block_start < ? ORDER BY block_start",
            (meter_id, from_iso, to_iso)).fetchall()
        return [{"start": r["block_start"],
                 "imp_rate": r["imp_rate"], "imp_kwh": r["imp_kwh"], "imp_cost": r["imp_cost"],
                 "exp_rate": r["exp_rate"], "exp_kwh": r["exp_kwh"], "exp_cost": r["exp_cost"]}
                for r in rows]

    def apply_csv_import(
        self, channel_csvs: dict, meter_id: str = "electricity_main", *,
        periods_by_channel: dict | None = None, overrides: dict | None = None,
        replace_ranges: list | None = None,
    ) -> dict:
        """Parse Octopus per-channel CSV(s), derive rates (rate-from-cost), and
        write reconstructed `imported_csv` blocks + one `rate` derivation per
        (channel, tariff period). `channel_csvs` = {'import': csv_text, 'export':
        csv_text}. `periods_by_channel` (optional) = {channel: [(from,to),...]}
        agreement bounds; None ⇒ one period per channel.

        `overrides` (optional) carries the wizard's user-confirmed rates:
        {channel: {period_index(str): {tier_label: rate}}}. A tier's override
        replaces the derived aggregate for its blocks and is stored as the
        derivation's confirmed_value.

        `replace_ranges` (optional) = [{from,to}] spans where an existing IMPORTED block
        may be REPLACED rather than skipped — the agreements a previous API import proved
        the supplier does not cost, where what we hold is usage x published rate and the
        bill carries the real charges. Outside those spans first-man-wins is unchanged,
        and inside them live/settled readings and user corrections are still protected
        (see imported_block_replaceable).

        Import is written before export so both settle onto the shared row.
        Returns a per-channel summary with blocks written, period rate derivations,
        and the re-price-vs-CSV-cost reconciliation."""
        import csv_import as _ci
        periods_by_channel = periods_by_channel or {}
        overrides = overrides or {}
        out: dict = {"ok": True, "meter_id": meter_id, "channels": {},
                     "blocks_written": 0, "blocks_skipped": 0,
                     "blocks_replaced": 0, "blocks_protected": 0,
                     "span": {"from": None, "to": None}}
        # Import first (house figure) so export merges onto existing rows.
        for channel in ("import", "export"):
            text = channel_csvs.get(channel)
            if not text:
                continue
            parsed = _ci.parse_octopus_csv(text, channel)
            if not parsed["ok"]:
                out["channels"][channel] = {"ok": False, "errors": parsed["errors"]}
                continue
            blocks = parsed["blocks"]
            if blocks:
                cf = blocks[0]["block_start"]
                ct = blocks[-1].get("block_end") or blocks[-1]["block_start"]
                if out["span"]["from"] is None or cf < out["span"]["from"]:
                    out["span"]["from"] = cf
                if out["span"]["to"] is None or ct > out["span"]["to"]:
                    out["span"]["to"] = ct
            deriv = _ci.derive_rates(blocks, periods=periods_by_channel.get(channel))
            ch_ov = overrides.get(channel, {}) or {}

            # One rate derivation per tariff period; blocks link to theirs.
            period_meta = []            # (from, to, derivation_id, tier_override_map)
            for i, p in enumerate(deriv["periods"]):
                ov_i = {k: float(v) for k, v in (ch_ov.get(str(i)) or {}).items()}
                tiers = [dict(t) for t in (p.get("tiers") or [])]
                for t in tiers:                       # fold confirmations into params
                    if t.get("label") in ov_i:
                        t["confirmed_rate"] = ov_i[t["label"]]
                derived_primary = min((t["rate"] for t in tiers if t.get("rate") is not None),
                                      default=None)
                confirmed_primary = min(ov_i.values()) if ov_i else None
                did = self.insert_historical_derivation(
                    "rate", p["from"], p["to"], subject=channel,
                    params={"kind": p["kind"], "tiers": tiers,
                            "standing_daily": p.get("standing_daily")},
                    derived_value=derived_primary, confirmed_value=confirmed_primary,
                    source=IMPORTED_SOURCE_CSV, notes="rate-from-cost (CSV import)")
                period_meta.append((p["from"], p["to"], did, ov_i))

            def _period_for(start):
                for f, t, d, ov in period_meta:
                    if f <= start < t:
                        return d, ov
                return (period_meta[-1][2], period_meta[-1][3]) if period_meta else (None, {})

            # Standing charge is per-day (sum the per-interval apportioned column).
            day_standing: dict = {}
            for b in blocks:
                day_standing[b["block_start"][:10]] = (
                    day_standing.get(b["block_start"][:10], 0.0) + (b.get("standing") or 0.0))

            written = 0
            skipped = 0                 # first-man-wins: slot already held data
            replaced = 0                # inside an uncostable span: usable bill data won
            protected = 0               # inside one, but live/settled or user-corrected
            eff_flags: dict = {}        # for reconciliation with confirmed rates
            for b in blocks:
                flag = deriv["flags"].get(b["block_start"]) or {}
                did, ov = _period_for(b["block_start"])
                tier = flag.get("tier")
                # Rate precedence — RATE-FIRST. A user override for this tier wins;
                # otherwise, if the CSV row carried an explicit unit rate (an Octopus
                # HH bill table transcribed exactly, or any rate-first CSV) store THAT
                # exact per-slot rate — do NOT flatten it to the period's tier average.
                # Only a cost-only CSV (no rate column) falls back to the derived
                # aggregate. Cost is already per-slot (rate×kWh), so this makes the
                # stored rate consistent with cost and preserves the off-peak/peak
                # split instead of blending a whole tariff period into one rate.
                if ov and tier in ov:
                    rate = ov[tier]
                elif b.get("rate") is not None:
                    rate = b["rate"]
                else:
                    rate = flag.get("rate")
                eff_flags[b["block_start"]] = {"tier": tier, "rate": rate}
                # Replace, rather than skip, only inside a span the supplier never
                # costed — and only over data an import wrote.
                _may_replace = False
                _blocked = False
                if _in_span(replace_ranges, b["block_start"]):
                    _ok, _why = self.imported_block_replaceable(
                        b["block_start"], meter_id, channel)
                    _may_replace = _ok
                    _blocked = _why in ("live", "corrected")
                bid, wrote = self.upsert_imported_block(
                    b["block_start"], meter_id, channel,
                    kwh=b["kwh"], rate=rate, cost=b.get("cost"),
                    standing=day_standing.get(b["block_start"][:10]),
                    derivation_id=did, overwrite=_may_replace,
                    cost_exc=b.get("cost_exc"))
                if wrote:
                    if _may_replace:
                        replaced += 1
                    else:
                        written += 1
                elif bid is not None:
                    if _blocked:
                        protected += 1  # live/settled or user-corrected — never touched
                    else:
                        skipped += 1    # a block already existed here — left as-is

            out["channels"][channel] = {
                "ok": True, "blocks_written": written, "blocks_skipped": skipped,
                "blocks_replaced": replaced, "blocks_protected": protected,
                "period_derivations": [d for _f, _t, d, _o in period_meta],
                "off_peak_kwh": deriv["off_peak_kwh"], "peak_kwh": deriv["peak_kwh"],
                "reconcile": _ci.reconcile(blocks, eff_flags),
                "parse_errors": parsed["errors"],
            }
            out["blocks_written"] += written
            out["blocks_skipped"] += skipped
            out["blocks_replaced"] += replaced
            out["blocks_protected"] += protected
        if out["blocks_written"]:
            # Freshly-imported history carries NULL carbon_intensity. The historical
            # carbon backfill sets a "done" marker once it has swept the then-known
            # span and won't revisit on its own — so a later import would sit at 0%
            # carbon forever. Clear the marker so the next scheduler tick re-scans and
            # fills the added span (region-eligible blocks inherit their period's
            # postcode; same-MPAN history needs no region change).
            self.rearm_carbon_backfill()
            # BL-8 phase 2: a CSV fill over a span is a TARGETED, explicit restore —
            # lift any tombstone over the written range (sub-span split) so the range
            # is genuinely un-deleted, not left as present data under a stale
            # tombstone. (The blanket auto-backfill never does this.)
            _sf, _st = out["span"].get("from"), out["span"].get("to")
            if _sf and _st:
                try:
                    self.clear_deleted_range(meter_id, _sf, _st)
                except Exception:
                    pass
            # Historical EXPORT import: a solar meter exports nothing overnight, and
            # the source (PDF/CSV) only lists the daytime export — so fill the blank
            # export slots on imported export days with 0, else they show as a
            # permanent, un-fillable export gap. Day-scoped + imported-only.
            if "export" in (channel_csvs or {}):
                try:
                    self.zero_fill_imported_export_blanks(self.site_timezone())
                except Exception:
                    pass
        return out

    CHECKPOINT_MODES = ("PASSIVE", "FULL", "RESTART", "TRUNCATE")

    def checkpoint(self, mode: str = "PASSIVE") -> tuple:
        """Copy the write-ahead log into the main database. Returns SQLite's own
        `(busy, log_pages, checkpointed)` row.

        **`busy = 1` means the checkpoint could not COMPLETE** -- and it is not
        an error, so it does not raise. A reader holding an open transaction
        blocks it; the caller decides whether that matters.

        Note "could not complete" is not "did nothing". Measured: a blocked
        TRUNCATE still returned `(busy=1, log_pages=N, checkpointed=207)` -- it
        copied 207 pages across and then failed to reset the log, because
        resetting needs a lock the reader holds. So `checkpointed` is real work
        done and `busy` is about whether the WAL was left in the state the mode
        asked for. Only `busy = 0` means the mode was honoured.

        Every call site before this method existed discarded the row, and
        `engine_startup` logged "WAL checkpoint complete" unconditionally --
        including when the log had not been truncated at all.

        On the WRITE surface deliberately. A checkpoint rewrites the main
        database file, and the restore path in `web/server.py` records what
        issuing one from the wrong thread costs -- *"a concurrent use of one
        SQLite connection from two threads -> Segmentation fault"*. Reaching it
        through `writer()` turns that from a comment into a `WrongThread`.

        An unrecognised mode is rejected rather than passed through: SQLite does
        NOT error on `PRAGMA wal_checkpoint(NONSENSE)`, it silently performs a
        PASSIVE checkpoint, so a typo would degrade a TRUNCATE to a no-op without
        a word anywhere.
        """
        m = (mode or "").upper()
        if m not in self.CHECKPOINT_MODES:
            raise ValueError("checkpoint mode must be one of %s, got %r"
                             % (", ".join(self.CHECKPOINT_MODES), mode))
        row = self._conn.execute("PRAGMA wal_checkpoint(%s)" % m).fetchone()
        return tuple(row) if row else (0, 0, 0)

    def backup(self, dst_path: str) -> None:
        """Hot backup to dst_path using SQLite's online backup API."""
        dst = sqlite3.connect(dst_path)
        try:
            self._conn.backup(dst, pages=100, sleep=0.05)
        finally:
            dst.close()

    # ── Config periods ────────────────────────────────────────────────────

    def get_current_config_period_id(self) -> Optional[int]:
        """Return the id of the currently active config period, or None."""
        cur = self._conn.execute(
            "SELECT id FROM config_periods WHERE effective_to IS NULL "
            "ORDER BY effective_from DESC LIMIT 1"
        )
        row = cur.fetchone()
        return row["id"] if row else None

    def get_config_period(self, period_id: int) -> Optional[dict]:
        """Return a config period row as a dict."""
        cur = self._conn.execute(
            "SELECT * FROM config_periods WHERE id = ?", (period_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_config_periods(self) -> list:
        """Return all config periods ordered by effective_from, with first/last block dates."""
        cur = self._conn.execute(
            """SELECT id, effective_from, effective_to, billing_day, block_minutes,
                      timezone, currency_symbol, currency_code, site_name
               FROM config_periods ORDER BY effective_from ASC"""
        )
        periods = [dict(row) for row in cur.fetchall()]

        # Attach first and last block_start for each period (used by billing period calc)
        for p in periods:
            c = self._conn.execute(
                """SELECT MIN(block_start) as first_bs, MAX(block_start) as last_bs
                   FROM blocks WHERE config_period_id = ?""",
                (p["id"],)
            )
            row = c.fetchone()
            p["first_block_start"] = row["first_bs"] if row else None
            p["last_block_start"]  = row["last_bs"]  if row else None
        return periods



    @staticmethod
    def _finalised_clause(finalised_only: bool, alias: str = "b") -> str:
        """SQL fragment to exclude provisional (not-yet-DCC-settled) blocks.

        Returns ' AND <alias>.is_provisional = 0' when finalised_only is True,
        else ''. Tier 1 (accuracy-critical) surfaces in 'api'/'api+mini' modes
        pass finalised_only=True. The default False preserves all pre-3.0.0
        behaviour: 'cad'/'cad+api' blocks are finalised at write time
        (is_provisional always 0), so the clause is a no-op for them anyway.
        """
        return f" AND {alias}.is_provisional = 0" if finalised_only else ""

    def compute_period_net(self, utc_s: str, utc_e: str, tz_name: str,
                           finalised_only: bool = False) -> float:
        """Compute net cost for a UTC period — single source of truth used by
        both Live Power (_fmt_total in server.py) and the billing chart
        (calculate_billing_summary_for_period in energy_charts.py).

        net_cost per day = round(
            round(main_imp, 4) + round(sc, 4) - round(exp, 4), 2)
        Summed across all days in the period. The import term is the MAIN METER's
        cost; sub-meters attribute that supply and never add to it (see below).
        """
        from collections import defaultdict
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        tz_obj = ZoneInfo(tz_name)
        rows = self._conn.execute(
            """SELECT b.block_start, b.imp_cost, b.exp_cost, b.standing_charge,
                      m.is_sub_meter, m.meter_id
               FROM blocks b JOIN meters m ON m.meter_id = b.meter_id
                                          AND m.config_period_id = b.config_period_id
               WHERE b.block_start >= ? AND b.block_start < ?"""
            + self._finalised_clause(finalised_only),
            (utc_s, utc_e)
        ).fetchall()

        by_d     = defaultdict(lambda: {"main_imp": 0.0, "exp": 0.0, "sc": 0.0})
        # Track the earliest block_start seen per day so the standing charge is
        # the start-of-day value (supplier convention), not MAX (which
        # over-bills on a decrease).
        _sc_earliest: dict = {}
        _day_of: dict = {}                  # block_start → local day, once per start

        for bs, imp_cost, exp_cost, standing, is_sub, _mid in rows:
            # Sub-meter rows add nothing (see below). They were summed per day into a
            # map no line read (#48, 3 Oct 2026).
            if is_sub:
                continue
            d = _day_of.get(bs)
            if d is None:
                # .date().isoformat() is strftime("%Y-%m-%d") for any 4-digit year, at a
                # fraction of the cost.
                d = _day_of[bs] = datetime.fromisoformat(bs).replace(
                    tzinfo=timezone.utc).astimezone(tz_obj).date().isoformat()
            _d = by_d[d]
            _d["main_imp"] += float(imp_cost or 0)
            _d["exp"] += float(exp_cost or 0)
            _sc = float(standing or 0)
            # Start-of-day standing charge: take the earliest block that has
            # a non-zero charge, so a leading zero (early gap-fill) doesn't
            # shadow the real value. Not MAX (over-bills on a decrease).
            if _sc > 0 and (d not in _sc_earliest or bs < _sc_earliest[d]):
                _sc_earliest[d] = bs
                _d["sc"] = _sc

        daily_nets = []
        for d in sorted(by_d):
            # THE PERIOD IMPORT IS THE MAIN METER'S COST. Nothing derived from
            # sub-meter readings may move it: the main meter is the metered supply
            # the supplier bills, and the sub-meters are an attribution OF that
            # supply, not an addition to it.
            #
            # This used to be built as `max(0, main - subs) + subs`, which is
            # algebraically `main` EXCEPT when the subs exceed the main -- and then
            # the clamp floored the remainder at 0 and the day's import silently
            # became SUM(subs). Sub-meters exceed the main routinely on any site
            # with storage: a battery charging off solar registers kWh on its own
            # sub-meter while the main meter shows little or no grid import.
            #
            # MEASURED on a 4.5.14 report (three sub-meters: heat pump, EV charger,
            # battery): 11 of 29 days tripped the clamp and the billing header read
            # £47.28 against a metered £24.86 -- overstated by £22.43, 90%.
            #
            # The clamp itself is not deleted, it is DEMOTED. A "Direct/House" line
            # showing a negative is nonsense to render, so the clamp belongs on the
            # displayed remainder -- see `_aggregate_block_rows` in web/server.py,
            # which keeps it for that purpose and now carries the metered total
            # alongside it for the row totals.
            imp_4    = round(by_d[d]["main_imp"], 4)
            sc_4     = round(by_d[d]["sc"], 4)
            exp_4    = round(by_d[d]["exp"], 4)
            daily_nets.append(round(imp_4 + sc_4 - exp_4, 2))

        return round(sum(daily_nets), 2)

    # ── 3.0.0 Kraken API integration — storage layer ─────────────────────────
    # All methods below operate only on the 3.0.0 columns/tables. They never
    # modify imp_kwh, imp_kwh_grid, imp_kwh_remainder or any pre-3.0.0 column,
    # so existing billing behaviour is unaffected.

    def set_kraken_state(self, key: str, value: Optional[str]) -> None:
        """Persist an ingester progress marker (e.g. 'last_poll_utc')."""
        self._conn.execute(
            """INSERT INTO kraken_state (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value,
                   updated_at = excluded.updated_at""",
            (key, value, _utc_now_iso()),
        )
        self._conn.commit()

    def get_kraken_state(self, key: str) -> Optional[str]:
        """Read an ingester progress marker. None if never set."""
        row = self._conn.execute(
            "SELECT value FROM kraken_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def delete_kraken_state(self, key: str) -> None:
        """Remove a kraken_state marker entirely (used by the disconnect action to
        wipe API-derived progress state — poll watermark, sweep cadence, pre-live
        snapshot flag — so a later reconnect starts clean). No-op if absent."""
        self._conn.execute("DELETE FROM kraken_state WHERE key = ?", (key,))
        self._conn.commit()

    def get_block_by_start(self, block_start: str, meter_id: str):
        """Return the block row for (block_start, meter_id), or None."""
        return self._conn.execute(
            "SELECT * FROM blocks WHERE block_start = ? AND meter_id = ?",
            (block_start, meter_id),
        ).fetchone()

    def get_block_by_id(self, block_id: int):
        """Return the block row for a given id, or None."""
        return self._conn.execute(
            "SELECT * FROM blocks WHERE id = ?", (block_id,)
        ).fetchone()

    def get_oldest_block_start(self, meter_id: Optional[str] = None,
                               *, exclude_imported: bool = False) -> Optional[str]:
        """Return the earliest block_start in the store, or None if empty.

        Drives the data-driven backfill window: the ingester fetches DCC from
        this point to now (capped). Backfilling earlier than the oldest block
        is pointless — upsert_kraken_block would only return missing_block.

        `exclude_imported=True` ignores reconstructed/imported blocks
        (source LIKE 'imported%'), so the historical-import "go-live" ceiling is
        where LIVE recording actually began — not wherever a previous partial
        import happened to write. Without this a partial import lowers the
        ceiling below its own gap, and the gap can never be topped up.
        """
        where = []
        params: list = []
        if meter_id:
            where.append("meter_id = ?")
            params.append(meter_id)
        if exclude_imported:
            where.append("(source IS NULL OR source NOT LIKE 'imported%')")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        row = self._conn.execute(
            "SELECT MIN(block_start) AS m FROM blocks" + clause, params).fetchone()
        return row["m"] if row and row["m"] else None

    def classify_kraken_block(
        self,
        block_start: str,
        meter_id: str,
        settled_kwh: Optional[float],
        *,
        channel: str = "import",
        billing_source: str = "api",
        drift_block_percent: float = 2.0,
        drift_min_kwh: float = 0.05,
    ) -> dict:
        """Read-only: determine what upsert_kraken_block WOULD do, without
        writing. Returns the same verdict dict (status / drift_pct /
        drifted / needs_pass2_rerun / cad_kwh / settled_kwh / channel /
        block_id) so a dry-run preview is identical to a live run minus the
        UPDATE. Performs one SELECT (the block lookup) and no writes.

        `drifted` is information for the poll's log, and nothing else: it flags
        nothing (BL-74, 4 Oct 2026). It used to set needs_review — OR-ed onto the
        stored flag, with no rate_source guard, so a cost-settled block inside the
        rolling backfill window was re-flagged on every poll — and no list could
        display a drift flag. On an API-sourced block the DCC figure is
        authoritative; the user sees what Octopus holds.

        Drift counts only when BOTH thresholds are exceeded: the absolute
        delta > drift_min_kwh (default 50 Wh) AND |drift| > drift_block_percent.
        The absolute floor stops percentage-on-tiny-numbers noise (e.g. 131% of
        16 Wh) dominating the count on a solar profile, while keeping real
        divergence on substantial blocks.
        """
        if channel not in ("import", "export"):
            raise ValueError(f"channel must be 'import' or 'export', got {channel!r}")
        cad_col = "imp_kwh" if channel == "import" else "exp_kwh"

        existing = self.get_block_by_start(block_start, meter_id)
        if existing is None:
            return {"status": "missing_block", "block_start": block_start,
                    "meter_id": meter_id, "channel": channel}
        if settled_kwh is None:
            return {"status": "no_value", "block_id": existing["id"],
                    "channel": channel}

        cad_kwh = existing[cad_col]
        drift_pct: Optional[float] = None
        review = 0
        # Materiality epsilon: kWh below this is treated as zero, so float dust
        # and sub-watt noise don't count as a real figure. 0.005 kWh = 5 Wh.
        _EPS = 0.005
        cad_material = cad_kwh is not None and abs(cad_kwh) >= _EPS
        dcc_material = settled_kwh is not None and abs(settled_kwh) >= _EPS

        if not cad_material and not dcc_material:
            # zero-vs-zero → agreement (e.g. night export, solar-covered import).
            review = 0
        elif cad_material and dcc_material:
            # both substantive → flag only if BOTH thresholds exceeded.
            abs_delta = abs(settled_kwh - cad_kwh)
            drift_pct = (settled_kwh - cad_kwh) / cad_kwh * 100.0
            if abs_delta > drift_min_kwh and abs(drift_pct) > drift_block_percent:
                review = 1
        else:
            # one side material, the other ~zero → genuine discrepancy, but only
            # worth flagging if the material side exceeds the absolute floor
            # (a lone 20 Wh blip is not actionable).
            material_val = settled_kwh if dcc_material else cad_kwh
            if abs(material_val) > drift_min_kwh:
                review = 1

        # Only queue a PASS 2 re-run when the DCC figure is NEW or CHANGED.
        # The poll re-upserts a rolling backfill window every cycle, so without
        # this guard an already-settled block would be re-flagged and re-run on
        # EVERY poll forever (rerun=1 unconditionally when billing_source=='api').
        # Compare the incoming settled_kwh against what's already materialised in
        # the api column: if unchanged, there's nothing new to re-run.
        api_col = "imp_kwh_api" if channel == "import" else "exp_kwh_api"
        try:
            prev_api = existing[api_col]
        except (IndexError, KeyError):
            prev_api = None
        _figure_changed = (prev_api is None) or (
            abs((settled_kwh or 0.0) - (prev_api or 0.0)) > 1e-6)
        # An IMPORTED block already holds the supplier's OWN half-hourly figure: the
        # historical import fetched it from this same API. Its `*_kwh_api` column is
        # NULL only because it never went through DCC settlement locally — not because
        # the number is an estimate. Treating that NULL as "changed" meant a first
        # settlement re-stating an identical figure queued the block for a full PASS 2
        # re-run. On a fresh install that imports two years, the backfill poll re-fetched
        # 19,129 slots and flagged every one: measured on the reporting DB, 19,129 of
        # 19,129 matched `imp_kwh` exactly (total delta 0.000000 kWh), so 13,779 blocks
        # were re-materialised against a kWh that had not moved.
        # The figure is still STORED — provenance stays honest and the corrections tool's
        # settled-only gate opens as it should. Only the re-run flag is withheld, and only
        # when the supplier has just confirmed what the import already wrote.
        if (_figure_changed and prev_api is None and settled_kwh is not None
                and cad_kwh is not None
                and str((existing["source"] if "source" in existing.keys() else "") or "")
                    .startswith("imported")
                and abs(float(settled_kwh) - float(cad_kwh)) <= 1e-6):
            _figure_changed = False
        rerun = 1 if (billing_source == "api" and _figure_changed) else 0
        new_rerun = 1 if (rerun or (existing["needs_pass2_rerun"] or 0)) else 0
        try:
            interpolated = bool(existing["interpolated"])
        except (IndexError, KeyError):
            interpolated = False
        return {
            "status": "stored",   # i.e. WOULD store
            "block_id": existing["id"],
            "channel": channel,
            "settled_kwh": settled_kwh,
            "cad_kwh": cad_kwh,
            "drift_pct": drift_pct,
            "interpolated": interpolated,
            "drifted": bool(review),
            "needs_pass2_rerun": new_rerun,
        }

    def store_mini_import(self, block_start: str, meter_id: str,
                          imp_kwh: float) -> dict:
        """Store a provisional Mini-derived import kWh on an existing block.

        api+mini near-real-time layer: writes imp_kwh, marks the block
        provisional + sourced 'kraken_mini', and queues it for PASS 2 re-run so
        DCC settlement (when it arrives) overwrites it. Returns
        {"status": "stored"|"missing_block"}.

        Mini is treated like CAD: imp_kwh is the working import figure until DCC
        reconciles. Export is never Mini-sourced (no Mini export register).
        """
        row = self.get_block_by_start(block_start, meter_id)
        if row is None:
            return {"status": "missing_block", "block_start": block_start}
        self._conn.execute(
            """UPDATE blocks
               SET imp_kwh = ?, is_provisional = 1, source = 'kraken_mini',
                   needs_pass2_rerun = 1
               WHERE block_start = ? AND meter_id = ?""",
            (imp_kwh, block_start, meter_id))
        self._conn.commit()
        return {"status": "stored", "imp_kwh": imp_kwh}

    def upsert_kraken_block(
        self,
        block_start: str,
        meter_id: str,
        settled_kwh: Optional[float],
        *,
        channel: str = "import",
        source: str = "kraken_api",
        billing_source: str = "api",
        drift_block_percent: float = 2.0,
        drift_min_kwh: float = 0.05,
    ) -> dict:
        """Store a DCC-settled figure on an existing block, for one channel.

        channel='import' writes imp_kwh_api and compares against imp_kwh;
        channel='export' writes exp_kwh_api and compares against exp_kwh. The
        two channels are symmetric and independent: a block may receive both
        (separate ingester calls), and the second call must not clear the
        first's flags, so needs_pass2_rerun is OR-ed with the existing row value
        rather than overwritten. needs_review is not written (BL-74).

        Classification is delegated to classify_kraken_block (shared with the
        dry-run preview); this method adds the UPDATE for a 'stored' verdict.

        Returns a dict describing what happened; status == "missing_block"
        when no matching block exists, "no_value" when settled_kwh is None.
        """
        verdict = self.classify_kraken_block(
            block_start, meter_id, settled_kwh, channel=channel,
            billing_source=billing_source, drift_block_percent=drift_block_percent,
            drift_min_kwh=drift_min_kwh)
        if verdict["status"] != "stored":
            return verdict

        api_col = "imp_kwh_api" if channel == "import" else "exp_kwh_api"
        self._conn.execute(
            # `source` is PROVENANCE — how the block came into existence — not a record
            # of who last touched it. Settlement writes the supplier's confirmed figure
            # into *_kwh_api; it must not restate where the block came from. COALESCE(?,
            # source) overwrote an importer's tag on first settlement, so 19,129
            # reconstructed blocks re-labelled themselves 'kraken_api' and silently left
            # three import-scoped behaviours: the Delete Blocks rollback filter
            # (source LIKE 'imported%'), the CSV/bill reprice path, and BL-62's scoped
            # bill replacement for an uncostable era — the PDF remedy expired simply by
            # the kWh settling. It also moved retag_untagged_imports' go-live anchor
            # (earliest read-or-kraken_api block) from today to 2025-08-12.
            # Narrow rule: an IMPORT tag is preserved because it records a reconstruction
            # those features scope on. Everything else is unchanged — an ha_sensor block
            # that DCC-settles still becomes 'kraken_api' (its figure genuinely is the
            # API's now, and no import-scoped behaviour keys on it).
            f"""UPDATE blocks
                SET {api_col} = ?,
                    source = CASE WHEN source LIKE 'imported%' THEN source
                                  ELSE COALESCE(?, source) END,
                    needs_pass2_rerun = ?,
                    finalised_from_cad = 0
                WHERE id = ?""",
            (settled_kwh, source,
             verdict["needs_pass2_rerun"], verdict["block_id"]),
        )
        self._conn.commit()
        return verdict

    # ── BL-8: outage backfill ────────────────────────────────────────────────

    BACKFILL_INTERPOLATED = 2   # blocks materialised from settled supplier data

    def create_backfill_block(self, block_start: str, meter_id: str,
                              settled_kwh: float, *,
                              channel: str = "import",
                              source: str = "kraken_api",
                              override_tombstone: bool = False) -> Optional[int]:
        """BL-8: materialise a block that does not exist, from DCC-settled data.

        An outage longer than the gap-fill limit leaves no block for the period,
        so `upsert_kraken_block` returns `missing_block` and the settled figure is
        dropped — a permanent hole. This creates the row with the authoritative
        kWh and marks it `needs_pass2_rerun=1`, letting the EXISTING PASS-2 drain
        resolve the rate and standing charge for that timestamp (it fetches
        historical rates, so any tariff — Agile included — prices correctly). We
        deliberately do not duplicate pricing logic here.

        `interpolated = BACKFILL_INTERPOLATED (2)` marks it externally sourced —
        distinct from 1 (locally interpolated gap-fill) — so the UI can show it as
        recovered/approximate. It carries NO sub-meter split (we were down) and,
        having no dispatch_history, will never be picked up by the dispatch
        reconciliation — its off-peak status is decided once, at creation.

        `block_end` is derived from the covering config period's `block_minutes`.
        Returns the new block id, or None if the block already exists or no
        config period covers the timestamp.

        BL-8 phase 2: a tombstoned (deliberately-deleted) slot is NOT re-created —
        this returns None — UNLESS `override_tombstone=True`, which only a TARGETED
        user re-import / per-gap / CSV / range fill passes (and which also clears the
        covering tombstone). Automatic callers (poll `missing_block` branch,
        settlement sweep) and blanket "recover all" fills leave it False, so a
        deliberate delete stays deleted.
        """
        if not override_tombstone and self.is_slot_tombstoned(meter_id, block_start):
            return None
        kwh_col = "imp_kwh" if channel == "import" else "exp_kwh"
        api_col = "imp_kwh_api" if channel == "import" else "exp_kwh_api"
        existing = self._conn.execute(
            f"SELECT id, {api_col}, interpolated FROM blocks "
            "WHERE block_start = ? AND meter_id = ?",
            (block_start, meter_id)).fetchone()
        if existing:
            # A backfill for the OTHER channel already created this block (import
            # is ingested before export). Don't lose this channel's kWh — fill it.
            # Guarded hard: only a block WE backfilled (interpolated=2) and whose
            # settled figure for THIS channel is unset. A live metered block is
            # never touched here (and never reaches this method anyway — we are
            # only called on a `missing_block` verdict).
            if existing[1] is None and existing[2] == self.BACKFILL_INTERPOLATED:
                with self._conn:
                    self._conn.execute(
                        f"UPDATE blocks SET {kwh_col} = ?, {api_col} = ?, "
                        "needs_pass2_rerun = 1 WHERE id = ?",
                        (settled_kwh, settled_kwh, existing[0]))
                return existing[0]
            return None
        cp = self.get_config_period_for_date(block_start)
        if not cp:
            return None
        try:
            bm = int(cp.get("block_minutes") or 30)
            block_end = (datetime.fromisoformat(block_start)
                         + timedelta(minutes=bm)).isoformat()
        except Exception:
            return None
        with self._conn:
            cur = self._conn.execute(
                f"""INSERT INTO blocks
                    (block_start, block_end, meter_id, config_period_id,
                     interpolated, {kwh_col}, {api_col}, source,
                     needs_pass2_rerun, is_provisional, finalised_from_cad)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 0)""",
                (block_start, block_end, meter_id, cp["id"],
                 self.BACKFILL_INTERPOLATED, settled_kwh, settled_kwh, source))
        return cur.lastrowid

    def count_backfilled_blocks(self) -> int:
        """How many blocks were materialised from settled supplier data."""
        return self._conn.execute(
            "SELECT COUNT(*) FROM blocks WHERE interpolated = ?",
            (self.BACKFILL_INTERPOLATED,)).fetchone()[0]

    def channel_slots_present(self, channel: str, from_iso: str, to_iso: str,
                              meter_id: str = "electricity_main") -> int:
        """Count blocks in [from_iso, to_iso] inclusive that HAVE this channel's
        kWh. Used to reconcile a persisted import-gap against reality: a gap whose
        slots now carry data has been filled and should drop from the display."""
        col = "imp_kwh" if channel == "import" else "exp_kwh"
        row = self._conn.execute(
            f"SELECT COUNT(*) FROM blocks WHERE meter_id = ? "
            f"AND block_start >= ? AND block_start <= ? AND {col} IS NOT NULL",
            (meter_id, from_iso, to_iso)).fetchone()
        return int(row[0]) if row else 0

    def zero_fill_imported_export_blanks(self, tz_name: Optional[str]) -> int:
        """Historical-export hygiene: on any LOCAL day that carries some imported
        export data, set that day's BLANK (NULL) export slots to 0.

        A solar export meter exports nothing overnight, but a PDF/CSV/API historical
        import that only wrote the daytime export left the rest of the day's export
        NULL. The gap detector can't tell a legitimately-zero overnight slot from
        real missing data, so those days show a permanent, un-fillable export "gap"
        (the API returns nothing for a slot with no export, so it can never clear).

        Day-scoped: only days that already have >=1 imported export value are filled,
        so a genuinely un-imported export range (no export on the day at all) is left
        untouched. IMPORTED blocks only (`source LIKE 'imported%'`) — never a live/DCC
        block that is correctly awaiting export settlement. Sets exp_kwh=0.0 and
        exp_cost=0.0 (billing-neutral: zero export is £0 either way), matching how the
        surrounding fully-imported days already store their overnight zeros. Returns
        the number of slots zero-filled.
        """
        from zoneinfo import ZoneInfo
        if not tz_name:
            r = self._conn.execute("SELECT timezone FROM config_periods "
                                   "ORDER BY effective_from DESC LIMIT 1").fetchone()
            tz_name = (r["timezone"] if r else "UTC") or "UTC"
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")
        utc = ZoneInfo("UTC")

        def _local_day(bs: str) -> str:
            try:
                return (datetime.fromisoformat(bs).replace(tzinfo=utc)
                        .astimezone(tz).date().isoformat())
            except Exception:
                return bs[:10]

        rows = self._conn.execute(
            "SELECT id, meter_id, block_start, exp_kwh FROM blocks "
            "WHERE source LIKE 'imported%'").fetchall()
        # (meter_id, local_day) that has any imported export value
        export_days = set()
        for r in rows:
            if r["exp_kwh"] is not None:
                export_days.add((r["meter_id"], _local_day(r["block_start"])))
        to_fill = [r["id"] for r in rows
                   if r["exp_kwh"] is None
                   and (r["meter_id"], _local_day(r["block_start"])) in export_days]
        if not to_fill:
            return 0
        with self._conn:
            for i in range(0, len(to_fill), 900):
                chunk = to_fill[i:i + 900]
                ph = ",".join("?" * len(chunk))
                self._conn.execute(
                    f"UPDATE blocks SET exp_kwh = 0.0, exp_cost = 0.0 "
                    f"WHERE id IN ({ph})", chunk)
        return len(to_fill)

    def block_span(self, meter_id: str = "electricity_main") -> tuple:
        """(first, last) block_start of a meter, or (None, None)."""
        r = self._conn.execute("SELECT MIN(block_start), MAX(block_start) FROM blocks "
                               "WHERE meter_id = ?", (meter_id,)).fetchone()
        return (r[0], r[1]) if r else (None, None)

    def find_block_gaps(self, meter_id: str = "electricity_main",
                        *, min_age_hours: float = 48.0,
                        now: Optional[datetime] = None) -> list:
        """Find missing half-hour slots in the block history (BL-8 gap sweep).

        The DCC poll's window is a forward-moving cursor (`last_poll_utc` → now),
        so once it has advanced past an outage that hole is never revisited. This
        asks the *database* where the holes are instead — exact, cheap, and it
        finds gaps however old and however they arose.

        Returns contiguous runs: [{"start", "end", "slots"}], where `end` is the
        start of the last missing slot. Skips anything newer than `min_age_hours`
        (DCC has not settled it yet, so a fetch would return nothing).
        """
        rows = [r[0] for r in self._conn.execute(
            "SELECT block_start FROM blocks WHERE meter_id = ? ORDER BY block_start",
            (meter_id,))]
        if len(rows) < 2:
            return []
        cp = self.get_config_period_for_date(rows[0]) or {}
        step = timedelta(minutes=int(cp.get("block_minutes") or 30))
        cutoff = ((now or datetime.now(timezone.utc)).replace(tzinfo=None)
                  - timedelta(hours=min_age_hours))
        have = set(rows)
        runs: list = []
        t = datetime.fromisoformat(rows[0])
        last = datetime.fromisoformat(rows[-1])
        while t <= last:
            if t.isoformat() not in have and t < cutoff:
                if runs and runs[-1]["_end_dt"] + step == t:
                    runs[-1]["_end_dt"] = t
                    runs[-1]["slots"] += 1
                else:
                    runs.append({"start": t.isoformat(), "_end_dt": t, "slots": 1})
            t += step
        for r in runs:
            r["end"] = r.pop("_end_dt").isoformat()
        return runs

    def get_blocks_needing_pass2_rerun(self, limit: Optional[int] = None) -> list:
        """Blocks where DCC has arrived and PASS 2+3b re-run is pending.

        The engine drains this queue: re-runs attribution + carbon using the
        stored carbon_intensity_g, then calls clear_pass2_rerun_flag().
        """
        sql = ("SELECT * FROM blocks WHERE needs_pass2_rerun = 1 "
               "ORDER BY block_start")
        if limit is not None:
            sql += " LIMIT ?"
            return self._conn.execute(sql, (limit,)).fetchall()
        return self._conn.execute(sql).fetchall()

    def clear_pass2_rerun_flag(self, block_id: int) -> None:
        """Mark a block's PASS 2+3b re-run as complete."""
        self._conn.execute(
            "UPDATE blocks SET needs_pass2_rerun = 0 WHERE id = ?", (block_id,)
        )
        self._conn.commit()

    def flag_all_for_pass2_rerun(self, main_meter_id: Optional[str] = None) -> int:
        """Flag every (main-meter) block for PASS 2 re-run.

        Used when the global billing_source toggle changes (cad<->dcc): the
        drain then reprocesses each block, rewriting billing figures to the
        chosen source. Returns the number of blocks flagged. Bounded only by
        the table size; the drain spreads the actual work across ticks.

        Only main-meter blocks carry the settlement columns, so sub-meter
        blocks don't need flagging — but flagging all is harmless (their re-run
        is a no-op) and simpler, so we scope to main meter when given.
        """
        if main_meter_id:
            cur = self._conn.execute(
                "UPDATE blocks SET needs_pass2_rerun = 1 WHERE meter_id = ?",
                (main_meter_id,))
        else:
            cur = self._conn.execute("UPDATE blocks SET needs_pass2_rerun = 1")
        self._conn.commit()
        return cur.rowcount


    # A main-meter block still awaiting DCC settlement on a channel it should
    # have: import not yet settled, OR export not yet settled on a block that
    # actually exported. Issue #303: export commonly settles days after import,
    # so keying "unsettled" on import alone let export-lagging blocks age out of
    # the re-fetch window and never settle — freezing the export figure on its
    # pre-settlement estimate. (Import-only accounts export 0, so the export
    # clause never fires for them.)
    # 3.5.0: reconstructed history (`imported_*`) is authoritative and pre-dates
    # EMT — DCC will NEVER settle it, so it must be excluded or every imported
    # block (imp_kwh_api NULL) is counted "awaiting DCC settlement" and chased by
    # the settlement sweep forever.
    # A block is "awaiting DCC settlement" if its import is unsettled, OR its export
    # is unsettled AND the meter demonstrably EXPORTS. The export test used to be
    # `exp_kwh IS NOT NULL AND exp_kwh > 0` — i.e. only chase export when a POSITIVE
    # live/CAD figure is already present. That silently strands DCC-export-only
    # meters, whose daytime export lands only via settlement: those slots carry
    # exp_kwh = 0 or NULL (no live figure), fail the guard, and are never re-fetched
    # (blocks-4.db: 245 stuck daytime slots). Instead: chase whenever exp_kwh_api is
    # NULL and the meter has EVER shown export (a live or settled positive anywhere)
    # — value-agnostic on the row, so 0/NULL placeholders are still corrected, while
    # a true import-only meter (never any export) is left alone. Bounded by the
    # settlement horizon + finalise-from-CAD so it can't chase forever.
    _UNSETTLED_WHERE = (
        "(imp_kwh_api IS NULL "
        " OR (exp_kwh_api IS NULL AND EXISTS ("
        "        SELECT 1 FROM blocks b2 WHERE b2.meter_id = blocks.meter_id "
        "        AND (b2.exp_kwh > 0 OR b2.exp_kwh_api > 0)))) "
        "AND (source IS NULL OR source NOT LIKE 'imported%')")

    # Import-only variant: used when export is NOT expected to DCC-settle (a FIT/
    # deemed-export account has no outgoing agreement, so its export never settles —
    # chasing/counting it is a permanent false alarm). See _unsettled_where.
    _UNSETTLED_WHERE_IMPORT_ONLY = (
        "imp_kwh_api IS NULL "
        "AND (source IS NULL OR source NOT LIKE 'imported%')")

    def _export_settlement_expected(self) -> bool:
        """Whether export is expected to DCC-settle for this account — True iff
        Octopus discovery found an OUTGOING/export agreement (persisted by
        engine._kraken_startup_discovery). FIT/deemed-export accounts have no
        outgoing agreement, so their export never settles and must not be counted
        as 'awaiting settlement'. Defaults TRUE when the flag is absent (older DB,
        or discovery hasn't run yet) so a genuine gap is never silenced — discovery
        sets it False for FIT accounts on the next account refresh."""
        return bool(self.get_meta("export_settlement_expected", True))

    def _unsettled_where(self, export_expected: bool) -> str:
        """The 'awaiting DCC settlement' predicate. Import is always chased; export
        is chased only when the account is on an outgoing/export agreement."""
        return (self._UNSETTLED_WHERE if export_expected
                else self._UNSETTLED_WHERE_IMPORT_ONLY)

    def get_unsettled_blocks(self, main_meter_id: str = "electricity_main",
                             limit: Optional[int] = None,
                             since_iso: Optional[str] = None) -> list:
        """Main-meter blocks still awaiting DCC settlement (import or export).

        For the view-only 'review unsettled blocks' report (relevant only when
        billing_source='dcc'): blocks running on the CAD/estimate fallback
        because DCC hasn't settled a channel yet. Ordered newest first so the
        most recent gaps surface at the top.

        `since_iso` floors the result at a settlement-horizon boundary — a block
        older than the horizon is not "awaiting DCC" (settlement lands within
        days; anything older either settled or never will), so the caller passes
        `now − horizon` to keep the count honest even before the settlement sweep
        has finalised past-horizon blocks.
        """
        w = self._unsettled_where(self._export_settlement_expected())
        sql = ("SELECT block_start, block_end, imp_kwh, imp_kwh_api, "
               "       exp_kwh, exp_kwh_api, is_provisional "
               f"FROM blocks WHERE meter_id = ? AND {w} "
               "AND finalised_from_cad = 0")
        params: list = [main_meter_id]
        if since_iso:
            sql += " AND block_start >= ?"
            params.append(since_iso)
        sql += " ORDER BY block_start DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return self._conn.execute(sql, params).fetchall()

    def count_unsettled_blocks(self, main_meter_id: str = "electricity_main",
                               since_iso: Optional[str] = None) -> int:
        """Count of main-meter blocks still awaiting DCC settlement on a channel
        (import or export), excluding those finalised-from-CAD. `since_iso` floors
        the count at the settlement horizon (see get_unsettled_blocks) so historic
        / never-settling blocks don't inflate the 'awaiting DCC settlement' badge."""
        w = self._unsettled_where(self._export_settlement_expected())
        sql = ("SELECT COUNT(*) AS n FROM blocks "
               f"WHERE meter_id = ? AND {w} "
               "AND finalised_from_cad = 0")
        params: list = [main_meter_id]
        if since_iso:
            sql += " AND block_start >= ?"
            params.append(since_iso)
        row = self._conn.execute(sql, params).fetchone()
        return int(row["n"]) if row else 0

    def count_awaiting_bill(self, since_iso: str, *, every_half_hour: bool, exclude=(),
                            until_iso: "str | None" = None,
                            meter_id: str = "electricity_main") -> int:
        """Main import blocks whose kWh has settled and whose bill has not arrived — the middle
        stage of settlement (ROADMAP BL-82's model: kWh, then the bill, then on IOG-SMB the split).
        `every_half_hour`: on IOG-SMB every half-hour is billed and fetched; elsewhere only a
        dispatched one is. Corrected and imported blocks are not waiting for a bill; `exclude`
        takes the half-hours whose bill is held back for its split (they wait at the next stage).
        `until_iso`: an exclusive upper bound (an IOG-SMB agreement's end), else none.
        """
        exclude = sorted(set(exclude or ()))
        sql = ("SELECT COUNT(*) FROM blocks b WHERE b.meter_id = ? AND b.imp_kwh > 0 "
               "  AND b.imp_kwh_api IS NOT NULL AND COALESCE(b.rate_corrected, 0) = 0 "
               "  AND (b.rate_source IS NULL OR b.rate_source NOT IN ('settled','corrected')) "
               "  AND (b.source IS NULL OR b.source NOT LIKE 'imported%') "
               "  AND b.block_start >= ? "
               "  AND NOT EXISTS (SELECT 1 FROM measured_cost mc WHERE mc.slot_start = b.block_start) ")
        args = [meter_id, since_iso]
        if until_iso:
            sql += "  AND b.block_start < ? "
            args.append(until_iso)
        if not every_half_hour:
            sql += ("  AND EXISTS (SELECT 1 FROM dispatch_slots s WHERE s.slot_start = b.block_start "
                    "              AND (s.off_peak = 1 OR s.source = 'smart-charge-completed')) ")
        if exclude:
            sql += "  AND b.block_start NOT IN (%s) " % ",".join("?" * len(exclude))
        return int(self._conn.execute(sql, (*args, *exclude)).fetchone()[0])

    # A half-hour's bill is one measured_cost row per MPAN; a meter change (a new MPAN) can leave
    # two. The settlement surfaces read the latest fetched, as get_measured_breakdown does — a
    # plain join returned whichever row SQLite met first (13 Oct 2026).
    _LATEST_BILL_SQL = ("SELECT m2.rowid FROM measured_cost m2 WHERE m2.slot_start = b.block_start "
                        "AND m2.direction = 'CONSUMPTION' ORDER BY m2.fetched_at DESC LIMIT 1")

    def get_settlement_facts(self, utc_start: str, utc_end: str,
                             meter_id: str = "electricity_main") -> list:
        """What `settlement_state.classify` needs for each main-meter half-hour in
        [utc_start, utc_end): the block's own figures and provenance, its cached bill (cost, ex-VAT,
        the kWh it priced, its EV share), its EV split (the EV segments), and whether it had a
        completed dispatch or an off-peak dispatch slot. One query, for the settlement views."""
        rows = self._conn.execute(
            "SELECT b.block_start, b.imp_kwh, b.imp_kwh_api, b.imp_cost, b.imp_rate, "
            "       b.exp_kwh, b.exp_kwh_api, "
            "       b.rate_source, COALESCE(b.rate_corrected, 0) AS rate_corrected, b.source, "
            "       mc.cost_incl AS bill_cost, mc.cost_excl AS bill_cost_exc, mc.kwh AS bill_kwh, "
            "       mc.ev_kwh AS bill_ev_kwh, mc.home_kwh AS bill_home_kwh, mc.label AS bill_label, "
            "       " + _evsplit.sql_kwh("b") + " AS ev_kwh, "
            "       EXISTS (SELECT 1 FROM dispatch_history d WHERE d.slot_start = b.block_start "
            "               AND d.kind = 'completed' AND d.energy_kwh IS NOT NULL) AS completed, "
            "       EXISTS (SELECT 1 FROM dispatch_slots s WHERE s.slot_start = b.block_start "
            "               AND (s.off_peak = 1 OR s.source = 'smart-charge-completed')) AS dispatched "
            "FROM blocks b LEFT JOIN measured_cost mc ON mc.rowid = (" + self._LATEST_BILL_SQL + ") "
            "WHERE b.meter_id = ? AND b.block_start >= ? AND b.block_start < ? "
            "ORDER BY b.block_start", (meter_id, utc_start, utc_end)).fetchall()
        return [dict(r) for r in rows]

    def get_settlement_detail(self, block_start: str,
                              meter_id: str = "electricity_main") -> "dict | None":
        """One main-meter half-hour's own figures and its cached bill, for the settlement detail
        panel (settlement surfaces design §4): the block's kWh (stored, DCC), cost, ex-VAT,
        rate and provenance, its export, and the bill row with when it was fetched. None if
        there is no such block."""
        r = self._conn.execute(
            "SELECT b.block_start, b.block_end, b.imp_kwh, b.imp_kwh_api, b.imp_cost, "
            "       b.imp_cost_exc, b.imp_rate, b.rate_source, COALESCE(b.rate_corrected, 0) "
            "       AS rate_corrected, b.source, b.exp_kwh, b.exp_kwh_api, b.exp_cost, b.exp_rate, "
            "       mc.cost_incl AS bill_cost, mc.cost_excl AS bill_cost_exc, mc.kwh AS bill_kwh, "
            "       mc.label AS bill_label, mc.fetched_at AS bill_fetched_at, "
            "       mc.home_kwh AS bill_home_kwh, mc.home_rate AS bill_home_rate, "
            "       mc.ev_kwh AS bill_ev_kwh, mc.ev_rate AS bill_ev_rate "
            "FROM blocks b LEFT JOIN measured_cost mc ON mc.rowid = (" + self._LATEST_BILL_SQL + ") "
            "WHERE b.block_start = ? AND b.meter_id = ?", (block_start, meter_id)).fetchone()
        return dict(r) if r else None

    def finalise_past_horizon_blocks(self, cutoff_block_start: str,
                                     main_meter_id: str = "electricity_main") -> int:
        """Mark main-meter blocks older than cutoff_block_start that are still
        awaiting DCC settlement (import or export, #303) as finalised-from-CAD,
        so the unsettled count can reach zero once DCC is no longer expected.
        This is DISTINCT from a real settlement — the *_api columns stay NULL —
        and reversible: a later DCC settlement clears the flag (see
        upsert_kraken_block). Returns the number of blocks newly flagged."""
        w = self._unsettled_where(self._export_settlement_expected())
        cur = self._conn.execute(
            "UPDATE blocks SET finalised_from_cad = 1 "
            f"WHERE meter_id = ? AND {w} "
            "AND finalised_from_cad = 0 AND block_start < ?",
            (main_meter_id, cutoff_block_start))
        self._conn.commit()
        return cur.rowcount

    def get_oldest_unsettled_block_start(
            self, main_meter_id: str = "electricity_main") -> Optional[str]:
        """Earliest block_start still awaiting DCC settlement on a channel
        (import or export, #303), or None.

        Bounds the sweep / user-triggered retry re-fetch window
        (oldest-unsettled → now). Including export here is what lets an
        export-lagging block stay in the window until its export settles."""
        w = self._unsettled_where(self._export_settlement_expected())
        row = self._conn.execute(
            "SELECT MIN(block_start) AS m FROM blocks "
            f"WHERE meter_id = ? AND {w}",
            (main_meter_id,)).fetchone()
        return row["m"] if row and row["m"] else None

    def get_oldest_gap_start(self, main_meter_id: str = "electricity_main",
                             since_iso: Optional[str] = None,
                             block_minutes: Optional[int] = None) -> Optional[str]:
        """Earliest MISSING main-meter block slot (an outage HOLE) within
        [since_iso, newest-block), or None.

        A hole has real blocks on BOTH sides — it's an interior/outage gap, not the
        pre-recording past. The DCC-poll window anchors at the oldest *unsettled*
        block (get_oldest_unsettled_block_start), which only sees blocks that EXIST —
        so a hole between two disjoint fetch windows (register measurements ending at
        T, DCC poll starting at T+n) is invisible to it and never backfilled. This
        surfaces the oldest such hole so the poll window can be pulled back to cover
        it (bounded by `since_iso` so we never chase ancient, un-resettleable gaps).
        block_minutes defaults to the newest config period's resolution.
        """
        from datetime import datetime, timedelta
        since = since_iso or ""
        rng = self._conn.execute(
            "SELECT MIN(block_start) AS mn, MAX(block_start) AS mx FROM blocks "
            "WHERE meter_id = ? AND block_start >= ?",
            (main_meter_id, since)).fetchone()
        if not rng or not rng["mn"]:
            return None
        if not block_minutes:
            r = self._conn.execute(
                "SELECT block_minutes FROM config_periods "
                "ORDER BY effective_from DESC LIMIT 1").fetchone()
            block_minutes = (r["block_minutes"] if r else 30) or 30
        have = {row[0] for row in self._conn.execute(
            "SELECT DISTINCT block_start FROM blocks "
            "WHERE meter_id = ? AND block_start >= ?",
            (main_meter_id, since)).fetchall()}
        try:
            step = timedelta(minutes=int(block_minutes))
            t = datetime.fromisoformat(rng["mn"])
            end = datetime.fromisoformat(rng["mx"])
        except ValueError:
            return None
        t += step
        while t < end:
            iso = t.isoformat()
            # BL-8 phase 2: a deliberately-deleted (tombstoned) hole is NOT an outage
            # gap — skip it so the poll window is never pulled back to re-create it.
            if iso not in have and not self.is_slot_tombstoned(main_meter_id, iso):
                return iso
            t += step
        return None

    # ── BL-8 phase 2: deleted_ranges tombstone helpers ───────────────────────────

    def is_slot_tombstoned(self, meter_id: str, slot_utc: str) -> bool:
        """True if `slot_utc` falls in an active deleted_range for this meter (or a
        '*' all-meters range). The one hot predicate the create-gates consult."""
        row = self._conn.execute(
            """SELECT 1 FROM deleted_ranges
               WHERE (meter_id = ? OR meter_id = '*')
                 AND ? >= start_utc AND ? < end_utc
               LIMIT 1""",
            (meter_id, slot_utc, slot_utc)).fetchone()
        return row is not None

    def tombstoned_slots_in(self, meter_id: str, start_utc: str,
                            end_utc: str) -> set:
        """The set of block_start slots in [start_utc, end_utc) that are tombstoned
        for this meter — one query to filter a whole poll window at once. Returns the
        covering ranges' union clipped to the window, materialised at block grain by
        the caller when needed; here we return the raw covering ranges as (s,e)."""
        rows = self._conn.execute(
            """SELECT start_utc, end_utc FROM deleted_ranges
               WHERE (meter_id = ? OR meter_id = '*')
                 AND end_utc > ? AND start_utc < ?""",
            (meter_id, start_utc, end_utc)).fetchall()
        return {(r["start_utc"], r["end_utc"]) for r in rows}

    def add_deleted_range(self, meter_id: str, start_utc: str, end_utc: str,
                          reason: str = "user_delete") -> int:
        """Record a deliberate deletion so auto-backfill/gap-scan skip the span.
        [start_utc, end_utc) half-open. Returns the new row id."""
        cur = self._conn.execute(
            "INSERT INTO deleted_ranges (meter_id, start_utc, end_utc, created_at, "
            "reason) VALUES (?,?,?,?,?)",
            (meter_id, start_utc, end_utc,
             datetime.now(timezone.utc).replace(tzinfo=None).isoformat(), reason))
        self._conn.commit()
        return cur.lastrowid

    def get_deleted_ranges(self, meter_id: Optional[str] = None) -> list:
        """Active tombstones (for the Data Management list), newest first."""
        if meter_id:
            rows = self._conn.execute(
                "SELECT * FROM deleted_ranges WHERE meter_id = ? "
                "ORDER BY start_utc DESC", (meter_id,)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM deleted_ranges ORDER BY start_utc DESC").fetchall()
        return [dict(r) for r in rows]

    def stale_deleted_range_ids(self) -> list:
        """Ids of tombstones whose range is ALREADY fully populated with blocks —
        orphans: deleted in the past, then re-created by another path (live poll /
        an older refill) that didn't clear the tombstone. A stale tombstone makes a
        present, correctly-priced block look 'deleted' (`is_deleted_range`) and
        clutters the Data Management list. Only a row where EVERY slot in
        [start,end) has a block for its meter counts — a genuinely still-empty or
        partially-filled deletion is not stale.

        READ-ONLY, so the Data Management GET can hide stale rows without writing:
        it used to prune them itself, which put a write — and a wait on the engine
        loop — inside every page view (first Pi soak, 29 Sep 2026: p50 0.02 s quiet,
        16.9 s while the re-price sweep held the loop). The engine prunes."""
        rows = self._conn.execute(
            "SELECT id, meter_id, start_utc, end_utc FROM deleted_ranges").fetchall()
        stale = []
        for r in rows:
            mid = r["meter_id"]
            check_mid = "electricity_main" if mid == "*" else mid
            try:
                s = datetime.fromisoformat(r["start_utc"])
                e = datetime.fromisoformat(r["end_utc"])
            except Exception:
                continue
            try:
                _cp = self.get_config_period_for_date(r["start_utc"])
                step = int((_cp or {}).get("block_minutes") or 30)
            except Exception:
                step = 30
            span_min = (e - s).total_seconds() / 60.0
            if span_min <= 0 or step <= 0:
                continue
            expected = int(round(span_min / step))
            if expected <= 0:
                continue
            have = self._conn.execute(
                "SELECT COUNT(DISTINCT block_start) FROM blocks "
                "WHERE meter_id = ? AND block_start >= ? AND block_start < ?",
                (check_mid, r["start_utc"], r["end_utc"])).fetchone()[0]
            if have >= expected:
                stale.append(r["id"])
        return stale

    def prune_stale_deleted_ranges(self) -> int:
        """4.5.6: delete the tombstones `stale_deleted_range_ids()` finds. Returns
        rows pruned. Run by the engine (`run_prune_stale_tombstones`), not by a
        request."""
        ids = self.stale_deleted_range_ids()
        if not ids:
            return 0
        with self._conn:
            self._conn.executemany("DELETE FROM deleted_ranges WHERE id = ?",
                                   [(i,) for i in ids])
        return len(ids)

    def clear_deleted_range(self, meter_id: str, start_utc: str,
                            end_utc: str) -> int:
        """Lift the tombstone over [start_utc, end_utc) for a TARGETED re-import.

        Interval subtraction (Q4 sub-span): every active row that overlaps the
        cleared window is removed; any portion of that row OUTSIDE the window is
        re-inserted as ≤2 remainder rows, so re-importing a sub-span leaves the rest
        deleted. A '*' row is split against the requested meter's clear too (the
        cleared window simply carves the interval; meter identity is preserved on the
        remainders). Returns the number of covering rows that were lifted.
        """
        overlapping = self._conn.execute(
            """SELECT id, meter_id, start_utc, end_utc, created_at, reason
               FROM deleted_ranges
               WHERE (meter_id = ? OR meter_id = '*' OR ? = '*')
                 AND end_utc > ? AND start_utc < ?""",
            (meter_id, meter_id, start_utc, end_utc)).fetchall()
        lifted = 0
        with self._conn:
            for r in overlapping:
                self._conn.execute("DELETE FROM deleted_ranges WHERE id = ?",
                                   (r["id"],))
                lifted += 1
                # left remainder: [row.start, clear.start)
                if r["start_utc"] < start_utc:
                    self._conn.execute(
                        "INSERT INTO deleted_ranges (meter_id, start_utc, end_utc, "
                        "created_at, reason) VALUES (?,?,?,?,?)",
                        (r["meter_id"], r["start_utc"], start_utc,
                         r["created_at"], r["reason"]))
                # right remainder: [clear.end, row.end)
                if r["end_utc"] > end_utc:
                    self._conn.execute(
                        "INSERT INTO deleted_ranges (meter_id, start_utc, end_utc, "
                        "created_at, reason) VALUES (?,?,?,?,?)",
                        (r["meter_id"], end_utc, r["end_utc"],
                         r["created_at"], r["reason"]))
        return lifted

    def get_timed_out_provisionals(
        self, cutoff_utc: str,
        sources: tuple = ("kraken_api", "kraken_mini"),
    ) -> list:
        """Provisional blocks older than cutoff whose DCC never arrived."""
        placeholders = ",".join("?" for _ in sources)
        return self._conn.execute(
            f"""SELECT * FROM blocks
                WHERE is_provisional = 1
                  AND block_start < ?
                  AND source IN ({placeholders})
                ORDER BY block_start""",
            (cutoff_utc, *sources),
        ).fetchall()

    def finalise_timed_out_provisionals(
        self, cutoff_utc: str,
        sources: tuple = ("kraken_api", "kraken_mini"),
    ) -> int:
        """Clear is_provisional on timed-out blocks. Returns rows affected.

        In 'api+mini' the Mini imp_kwh becomes the permanent billing value via
        COALESCE. In 'api' a block whose imp_kwh is still NULL finalises with
        NULL and is excluded from billing aggregations permanently — safer than
        writing a zero. The caller logs a WARNING per block (it has the list
        from get_timed_out_provisionals); this method only flips the flag.
        """
        placeholders = ",".join("?" for _ in sources)
        cur = self._conn.execute(
            f"""UPDATE blocks SET is_provisional = 0
                WHERE is_provisional = 1
                  AND block_start < ?
                  AND source IN ({placeholders})""",
            (cutoff_utc, *sources),
        )
        self._conn.commit()
        return cur.rowcount


    def get_settled_to(self, main_meter_id: str) -> Optional[str]:
        """Latest block_start that is fully DCC-settled for the main meter.

        Powers the Overview "As of HH:MM" timestamp in 'api'/'api+mini' modes.
        Returns None when nothing is settled yet.
        """
        row = self._conn.execute(
            """SELECT MAX(block_start) AS settled_to FROM blocks
               WHERE is_provisional = 0
                 AND imp_cost IS NOT NULL
                 AND meter_id = ?""",
            (main_meter_id,),
        ).fetchone()
        return row["settled_to"] if row and row["settled_to"] else None


    # ── Current block (replaces current_block.json) ──────────────────────────

    def save_current_block(self, block: dict) -> None:
        """
        Persist the in-progress block state to the current_block table.
        Replaces current_block.json.

        Gap marker state (previously a JSON blob) is now stored relationally:
          - current_block.gap_detected_at: timestamp when gap was detected
          - current_reads rows with is_gap_seed=1 (kWh) or 2 (rate): the
            pre-gap readings used to interpolate missing blocks

        block: the engine's current_block dict containing:
          start, end, interpolated, _last_checkpoint,
          _gap_marker (optional: {detected_at, pre_reads, last_known_rates}),
          meters[meter_id].channels[channel].reads[], .rates[]
        """
        block_start     = block.get("start")
        block_end       = block.get("end")
        last_checkpoint      = block.get("_last_checkpoint")
        gap_marker           = block.get("_gap_marker")
        gap_detected_at      = (gap_marker or {}).get("detected_at") if gap_marker else None
        gap_last_block_start = (gap_marker or {}).get("last_block_start") if gap_marker else None
        interpolated         = 1 if block.get("interpolated") else 0

        with self._conn:
            self._conn.execute(
                """INSERT INTO current_block
                       (id, block_start, block_end, last_checkpoint, gap_detected_at,
                        gap_last_block_start, interpolated)
                   VALUES (1, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       block_start          = excluded.block_start,
                       block_end            = excluded.block_end,
                       last_checkpoint      = excluded.last_checkpoint,
                       gap_detected_at      = excluded.gap_detected_at,
                       gap_last_block_start = excluded.gap_last_block_start,
                       interpolated         = excluded.interpolated""",
                (block_start, block_end, last_checkpoint, gap_detected_at,
                 gap_last_block_start, interpolated)
            )

            # Replace all current_reads with the rolling buffer + gap seed rows
            self._conn.execute("DELETE FROM current_reads")
            rows = []

            # Live reads and rates from the rolling buffer (is_gap_seed=0)
            for meter_id, meter_data in (block.get("meters") or {}).items():
                sc = float((meter_data or {}).get("standing_charge") or 0.0)
                for channel, ch_data in ((meter_data or {}).get("channels") or {}).items():
                    for r in (ch_data.get("reads") or []):
                        rows.append((r.get("ts"), meter_id, channel, "read",
                                     float(r.get("value", 0)), sc, 0))
                    for r in (ch_data.get("rates") or []):
                        rows.append((r.get("ts"), meter_id, channel, "rate",
                                     float(r.get("value", 0)), None, 0))

            # Gap seed rows from _gap_marker (is_gap_seed=1 for reads, 2 for rates)
            if gap_marker:
                for meter_id, channels in (gap_marker.get("pre_reads") or {}).items():
                    for channel, r in (channels or {}).items():
                        if r:
                            rows.append((r.get("ts"), meter_id, channel, "read",
                                         float(r.get("value", 0)), None, 1))
                for meter_id, channels in (gap_marker.get("last_known_rates") or {}).items():
                    for channel, r in (channels or {}).items():
                        if r:
                            rows.append((r.get("ts"), meter_id, channel, "rate",
                                         float(r.get("value", 0)), None, 2))

            if rows:
                self._conn.executemany(
                    """INSERT INTO current_reads
                           (captured_at, meter_id, channel, channel_type,
                            value, standing_charge, is_gap_seed)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    rows
                )

    def load_current_block(self) -> dict:
        """
        Load the in-progress block state from the DB.
        Returns a block dict in the same shape the engine expects,
        or {} if no current block exists.

        Gap marker is reconstructed from gap_detected_at and is_gap_seed rows.
        """
        from collections import defaultdict

        row = self._conn.execute(
            "SELECT * FROM current_block WHERE id = 1"
        ).fetchone()

        if not row or not row["block_start"]:
            return {}

        block = {
            "start":            row["block_start"],
            "end":              row["block_end"],
            "interpolated":     bool(row["interpolated"]),
            "_last_checkpoint": row["last_checkpoint"],
            "meters":           {},
        }

        # Load all current_reads rows — live and gap seed
        reads_cur = self._conn.execute(
            """SELECT meter_id, channel, channel_type, value,
                      standing_charge, captured_at, is_gap_seed
               FROM current_reads ORDER BY captured_at ASC"""
        )
        all_rows = reads_cur.fetchall()

        # Live reads (is_gap_seed=0) → reconstruct meters/channels
        meter_reads = defaultdict(lambda: defaultdict(list))
        meter_rates = defaultdict(lambda: defaultdict(list))
        meter_sc    = {}
        for r in all_rows:
            if r["is_gap_seed"] != 0:
                continue
            mid = r["meter_id"]; ch = r["channel"]
            entry = {"ts": r["captured_at"], "value": r["value"]}
            if r["channel_type"] == "read":
                meter_reads[mid][ch].append(entry)
                if r["standing_charge"] is not None:
                    meter_sc[mid] = r["standing_charge"]
            else:
                meter_rates[mid][ch].append(entry)

        # Get retirement dates for all meters so we can filter stale reads
        _retired = {}
        try:
            for _r in self._conn.execute(
                "SELECT meter_id, retired_at FROM meters WHERE retired_at IS NOT NULL"
            ).fetchall():
                _retired[_r["meter_id"]] = _r["retired_at"]
        except Exception:
            pass

        _block_date = (row["block_start"] or "")[:10]  # YYYY-MM-DD

        # BL-42: repopulate each meter's meta from config. load_current_block
        # reconstructs meters purely from current_reads with empty meta; the
        # api/Mini boundary-finalise path then hands finalise_block/_apply_pass2 a
        # block with no sub_meter/parent_meter, so the grid-authoritative device
        # split silently no-ops and imp_kwh_remainder is left NULL (the split only
        # re-lands at DCC settlement, via the get_block_dict join). Carry the
        # config meta here so the live provisional split works for sub-meter setups.
        _meta_by_id = {}
        try:
            _cp = self._conn.execute(
                "SELECT id FROM config_periods WHERE effective_to IS NULL "
                "ORDER BY effective_from DESC LIMIT 1").fetchone()
            if _cp:
                _cfg = self.config_from_db(_cp["id"])
                _meta_by_id = {_mid: (_m.get("meta") or {})
                               for _mid, _m in (_cfg.get("meters") or {}).items()}
        except Exception as _me:
            logger.warning("load_current_block: meta repopulate failed: %s", _me)

        all_meter_ids = set(list(meter_reads.keys()) + list(meter_rates.keys()))
        for mid in all_meter_ids:
            # Skip retired sub-meters — don't load their stale reads back into the block
            if mid in _retired and _retired[mid] <= _block_date:
                continue
            channels = {}
            for ch in set(list(meter_reads[mid].keys()) + list(meter_rates[mid].keys())):
                channels[ch] = {
                    "reads": meter_reads[mid].get(ch, []),
                    "rates": meter_rates[mid].get(ch, []),
                }
            block["meters"][mid] = {
                "channels": channels,
                "standing_charge": meter_sc.get(mid, 0.0),
                "meta": _meta_by_id.get(mid, {}),
            }

        # Reconstruct _gap_marker from gap_detected_at + gap seed rows
        if row["gap_detected_at"]:
            pre_reads       = defaultdict(dict)
            last_known_rates = defaultdict(dict)
            for r in all_rows:
                if r["is_gap_seed"] == 1:  # gap seed kWh read
                    pre_reads[r["meter_id"]][r["channel"]] = {
                        "ts": r["captured_at"], "value": r["value"]
                    }
                elif r["is_gap_seed"] == 2:  # gap seed rate
                    last_known_rates[r["meter_id"]][r["channel"]] = {
                        "ts": r["captured_at"], "value": r["value"]
                    }
            block["_gap_marker"] = {
                "detected_at":      row["gap_detected_at"],
                "pre_reads":        dict(pre_reads),
                "last_known_rates": dict(last_known_rates),
                "last_block_start": row["gap_last_block_start"],
            }

        return block

    def clear_current_block(self) -> None:
        """Clear the in-progress block state (e.g. after a reset)."""
        with self._conn:
            self._conn.execute("DELETE FROM current_block")
            self._conn.execute("DELETE FROM current_reads")

    def get_cumulative_totals(self) -> dict:
        """
        Return lifetime cumulative totals for HA sensor publishing.

        import_kwh is the MAIN METER's import: the grid boundary (step 5, 8 Oct 2026). It was
        the stored remainder plus every device's grid share — the main, wherever the remainder
        was right, and short of it wherever a device's rows had gone and its remainder stayed
        (95.8 kWh on one real database, 21.1 on another; identical on six). Sub-meters are an
        attribution of the main's import, never an addition to it.

        export_kwh/imp_cost/exp_cost: main meter only (sub-meters don't
        have independent costs or export).
        """
        active_period_sq = (
            "SELECT id FROM config_periods "
            "WHERE effective_to IS NULL ORDER BY effective_from DESC LIMIT 1"
        )
        cur = self._conn.execute(
            f"""SELECT
                 COALESCE(SUM(CASE WHEN m.is_sub_meter = 0 THEN b.imp_kwh ELSE 0 END), 0.0) as import_kwh,
                 COALESCE(SUM(CASE WHEN m.is_sub_meter = 0 THEN b.exp_kwh  ELSE 0 END), 0.0) as export_kwh,
                 COALESCE(SUM(CASE WHEN m.is_sub_meter = 0 THEN b.imp_cost ELSE 0 END), 0.0) as import_cost,
                 COALESCE(SUM(CASE WHEN m.is_sub_meter = 0 THEN b.exp_cost ELSE 0 END), 0.0) as export_cost
               FROM blocks b
               JOIN meters m
                 ON m.meter_id = b.meter_id
                AND m.config_period_id = ({active_period_sq})"""
        )
        row = cur.fetchone()
        return {
            "import_kwh":  round(float(row["import_kwh"]),  6),
            "export_kwh":  round(float(row["export_kwh"]),  6),
            "import_cost": round(float(row["import_cost"]), 6),
            "export_cost": round(float(row["export_cost"]), 6),
        }


    def get_config_period_for_date(self, date_iso: str) -> Optional[dict]:
        """
        Return the config period that was active on a given date (YYYY-MM-DD).
        Used by billing calculations to get the historically correct billing_day.
        """
        cur = self._conn.execute(
            """
            SELECT * FROM config_periods
            WHERE effective_from <= ?
              AND (effective_to IS NULL OR effective_to > ?)
            ORDER BY effective_from DESC
            LIMIT 1
            """,
            (date_iso, date_iso)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    # ── Region timeline (per-period postcode → DNO region for carbon) ──────────
    def get_postcode_prefix_at(self, date_iso: str) -> tuple:
        """(outward_code, source) for the config period effective at date_iso.

        Resolves via get_config_period_for_date → that period's MAIN meter.
        Pre-EMT/imported dates (no covering period) fall back to the OLDEST
        period, matching where imported blocks are attached. Returns
        (None, None) when there is no period or no postcode is configured.

        This is the per-date region lookup carbon backfill must use instead of a
        single global postcode, so a house move (a new period with a different
        outward code) attributes the correct region either side of the move.
        """
        cp = self.get_config_period_for_date(date_iso)
        if cp is None:
            row = self._conn.execute(
                "SELECT id FROM config_periods ORDER BY effective_from ASC LIMIT 1"
            ).fetchone()
            if not row:
                return (None, None)
            cp_id = row["id"]
        else:
            cp_id = cp["id"]
        r = self._conn.execute(
            "SELECT postcode_prefix, postcode_source FROM meters "
            "WHERE config_period_id = ? AND is_sub_meter = 0 "
            "ORDER BY id ASC LIMIT 1",
            (cp_id,)
        ).fetchone()
        if not r:
            return (None, None)
        try:
            src = r["postcode_source"]
        except (KeyError, IndexError):
            src = None
        return (r["postcode_prefix"], src)

    def set_period_postcode(self, period_id: int, outcode, source: str) -> None:
        """Set the outward code + provenance on a config period's MAIN meter."""
        with self._conn:
            self._conn.execute(
                "UPDATE meters SET postcode_prefix = ?, postcode_source = ? "
                "WHERE config_period_id = ? AND is_sub_meter = 0",
                (outward_code(outcode), source, period_id)
            )

    def apply_region_periods(self, spans: list, *, overwrite_user: bool = False) -> dict:
        """Auto-apply probed region spans (see derive_region_periods) onto the
        config-period timeline. Idempotent and privacy-preserving (outward code
        only). NEVER overwrites a user-set postcode unless overwrite_user=True.

        Single region (the common case, incl. never-moved accounts): stamp that
        outward code onto every period's main meter that lacks a user value —
        this fully "auto-applies" and lets carbon resolve historically.

        Multiple regions (a move): this needs a *historical* config-period split,
        for which there is no validated primitive yet (insert_config_period only
        appends a new current period). To avoid unvalidated surgery on
        billing-affecting history, we stamp the latest region where safe and
        return split_required=True with the spans, so the split can be done
        deliberately (and reviewed) rather than silently. See ROADMAP 3.5.1 /
        docs/region_timeline_design.md.
        """
        clean = []
        for s in (spans or []):
            oc = outward_code(s.get("outcode") or s.get("postcode"))
            if oc:
                clean.append({**s, "outcode": oc})
        if not clean:
            return {"applied": 0, "outcodes": [], "split_required": False,
                    "reason": "no region data"}

        outs = sorted({s["outcode"] for s in clean})
        periods = self._conn.execute(
            "SELECT id FROM config_periods ORDER BY effective_from ASC"
        ).fetchall()

        def _stamp(period_id: int, oc: str) -> bool:
            m = self._conn.execute(
                "SELECT postcode_prefix, postcode_source FROM meters "
                "WHERE config_period_id = ? AND is_sub_meter = 0 "
                "ORDER BY id ASC LIMIT 1", (period_id,)
            ).fetchone()
            if not m:
                return False
            try:
                src = m["postcode_source"]
            except (KeyError, IndexError):
                src = None
            if m["postcode_prefix"] and src == "user" and not overwrite_user:
                return False   # never clobber a user-set value
            if m["postcode_prefix"] == oc and src == "octopus":
                return False   # already applied — idempotent no-op
            self.set_period_postcode(period_id, oc, "octopus")
            return True

        applied = 0
        if len(outs) == 1:
            for p in periods:
                if _stamp(p["id"], outs[0]):
                    applied += 1
            return {"applied": applied, "outcodes": outs, "split_required": False}

        # Multiple regions → a move. Stamp the most-recent region where safe and flag.
        latest = sorted(clean, key=lambda s: (s.get("from") or ""))[-1]["outcode"]
        for p in periods:
            if _stamp(p["id"], latest):
                applied += 1
        logger.warning(
            "apply_region_periods: %d distinct regions %s imply a historical "
            "config-period split (a property move); applied latest region %r to "
            "existing periods only. Historical split NOT performed automatically.",
            len(outs), outs, latest)
        return {"applied": applied, "outcodes": outs, "split_required": True,
                "spans": clean}

    # ── Region reconciliation (post-import confirmation flow) ──────────────────
    @staticmethod
    def _site_for_date(sites: list, date_iso: str) -> Optional[dict]:
        """The tenancy whose [from, to) span contains date_iso (to=None → open).
        On overlap, the one with the latest `from` wins (most specific)."""
        best = None
        for s in sites:
            frm = s.get("from") or ""
            to  = s.get("to")
            if date_iso >= frm and (to is None or date_iso < to):
                if best is None or frm > (best.get("from") or ""):
                    best = s
        return best

    def _needs_split_at(self, from_iso: str):
        """(period_dict, snapped_iso) if a boundary at from_iso would genuinely
        divide an existing period; else (None, None)."""
        if not from_iso:
            return (None, None)
        cp = self.get_config_period_for_date(from_iso)
        if not cp:
            return (None, None)
        snapped = self._snap_to_midnight_utc(from_iso, cp.get("timezone") or "UTC")
        if snapped <= cp["effective_from"]:
            return (None, None)                       # boundary already present
        if cp.get("effective_to") is not None and snapped >= cp["effective_to"]:
            return (None, None)
        return (cp, snapped)

    def plan_region_reconciliation(self, derived: list) -> dict:
        """Read-only. Given derived tenancy spans (derive_region_periods), compute
        the plan to align the config-period timeline to the property history:

            {"needs_confirmation": bool,
             "sites": [{outcode, from, to, key, hint, block_count,
                        site_name(prefill|None), needs_name}],
             "split_dates": [iso, ...]}   # boundaries Apply will create

        `needs_confirmation` is True when any site lacks a name or any split is
        needed. Nothing is mutated."""
        sites = []
        for s in (derived or []):
            oc = outward_code(s.get("outcode"))
            if not oc:
                continue
            sites.append({"outcode": oc, "from": s.get("from"), "to": s.get("to"),
                          "key": s.get("key"), "hint": s.get("hint")})
        periods = self._conn.execute(
            "SELECT effective_from, effective_to, site_name FROM config_periods "
            "ORDER BY effective_from ASC").fetchall()
        if not sites or not periods:
            return {"needs_confirmation": False, "sites": [], "split_dates": []}

        split_dates = []
        for s in sites:
            for d in (s.get("from"), s.get("to")):   # a bounded span may need both ends cut
                cp, _snap = self._needs_split_at(d)
                if cp is not None:
                    split_dates.append(d)

        out_sites = []
        for s in sites:
            frm = s.get("from") or ""
            to  = s.get("to")
            if to is None:
                row = self._conn.execute(
                    "SELECT COUNT(*) c FROM blocks WHERE block_start >= ?", (frm,)).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) c FROM blocks WHERE block_start >= ? AND block_start < ?",
                    (frm, to)).fetchone()
            block_count = row["c"] if row else 0
            prefill = None
            for p in periods:
                pf = p["effective_from"]
                if pf >= frm and (to is None or pf < to) and p["site_name"]:
                    prefill = p["site_name"]
                    break
            out_sites.append({**s, "block_count": block_count,
                              "site_name": prefill, "needs_name": prefill is None})

        needs = any(x["needs_name"] for x in out_sites) or bool(split_dates)
        return {"needs_confirmation": needs, "sites": out_sites,
                "split_dates": sorted(set(split_dates))}

    def apply_region_reconciliation(self, sites: list) -> dict:
        """Apply a confirmed reconciliation: split config periods at each move
        boundary, then stamp every period with its tenancy's outward code
        (source 'octopus') and, where supplied, its user-confirmed site name.
        `sites` = [{outcode, from, to, site_name?}]. Returns counts."""
        splits = 0
        boundaries = []
        for s in sites:
            if s.get("from"):
                boundaries.append(s["from"])
            if s.get("to"):
                boundaries.append(s["to"])      # cut the far end of a bounded span too
        for d in sorted(set(boundaries)):
            cp, _snap = self._needs_split_at(d)
            if cp is not None:
                self.split_config_period_at(cp["id"], d)
                splits += 1
        stamped = 0
        periods = self._conn.execute(
            "SELECT id, effective_from, effective_to FROM config_periods ORDER BY effective_from ASC"
        ).fetchall()
        for p in periods:
            site = self._site_for_date(sites, p["effective_from"])
            if not site:
                continue
            oc = outward_code(site.get("outcode"))
            is_active = p["effective_to"] is None
            with self._conn:
                self._conn.execute(
                    "UPDATE meters SET postcode_prefix = ?, postcode_source = 'octopus' "
                    "WHERE config_period_id = ? AND is_sub_meter = 0", (oc, p["id"]))
                # NEVER rename the ACTIVE period — its site_name is the instance /
                # backup-folder identity (see instance.py). A historic import only
                # labels EARLIER (past-address) periods; the current site name is
                # managed via normal config, and a real future move creates a new
                # current period. Region (postcode) is always safe to stamp.
                if site.get("site_name") and not is_active:
                    self._conn.execute(
                        "UPDATE config_periods SET site_name = ? WHERE id = ?",
                        (site["site_name"], p["id"]))
            stamped += 1
        logger.info("apply_region_reconciliation: %d split(s), %d period(s) stamped",
                    splits, stamped)
        return {"splits": splits, "stamped": stamped}

    def plan_csv_reconciliation(self, from_iso: str, to_iso) -> dict:
        """Reconcile plan for a CSV import span. Unlike the API path, CSV carries
        NO provenance, so the single site is **region-editable**: the user names
        it AND supplies the outward code. If they skip, the blocks stay on their
        existing period(s) with region unknown (carbon excluded) — i.e. the data
        blends into whatever period already covers it. Returns the same shape as
        plan_region_reconciliation with one site + region_editable=True."""
        if not from_iso:
            return {"needs_confirmation": False, "sites": [], "split_dates": [], "source": "csv"}
        if to_iso is None:
            row = self._conn.execute(
                "SELECT COUNT(*) c FROM blocks WHERE block_start >= ?", (from_iso,)).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) c FROM blocks WHERE block_start >= ? AND block_start < ?",
                (from_iso, to_iso)).fetchone()
        block_count = row["c"] if row else 0
        prefill = None
        for p in self._conn.execute(
                "SELECT effective_from, site_name FROM config_periods "
                "ORDER BY effective_from ASC").fetchall():
            pf = p["effective_from"]
            if pf >= from_iso and (to_iso is None or pf < to_iso) and p["site_name"]:
                prefill = p["site_name"]
                break
        split_dates = []
        for d in (from_iso, to_iso):
            cp, _snap = self._needs_split_at(d)
            if cp is not None:
                split_dates.append(d)
        site = {"outcode": None, "from": from_iso, "to": to_iso, "key": None,
                "hint": None, "region_editable": True, "block_count": block_count,
                "site_name": prefill, "needs_name": prefill is None}
        return {"needs_confirmation": True, "sites": [site],
                "split_dates": sorted(set(split_dates)), "source": "csv"}

    # ── Pre-import site confirmation (create covering periods BEFORE import) ────
    # The reconcile flow above SPLITS periods AFTER an import — which cannot
    # create a period earlier than the oldest existing one, so a past address is
    # only labelled from the earliest existing period forward. The canonical flow
    # is instead to CONFIRM the property history FIRST and create the covering
    # periods up front, so every imported block lands in its correct period from
    # the first write (correctly regioned + carbon-eligible), with no post-hoc
    # split or block reassignment. See docs/region_timeline_design.md.

    def create_covering_period(self, from_iso: str, to_iso,
                               *, outcode=None, site_name: Optional[str] = None,
                               based_on_period_id: Optional[int] = None) -> int:
        """Create a config period covering ``[from, to)`` by CLONING an existing
        period's config (meters + channels) and stamping its region (outward code)
        and site name. Intended for the PRE-import flow: the range is empty (no
        blocks exist yet), so nothing is reassigned and the chain of *other*
        periods is untouched — the caller (apply_pre_import_sites) guarantees
        contiguity. ``to`` may be None only for an open-ended (current) period,
        but in practice covering periods are bounded. Returns the new period id."""
        ref_id = based_on_period_id or self.get_current_config_period_id()
        if ref_id is None:
            row = self._conn.execute(
                "SELECT id FROM config_periods ORDER BY effective_from ASC LIMIT 1"
            ).fetchone()
            ref_id = row["id"] if row else None
        if ref_id is None:
            raise ValueError("create_covering_period: no reference period to clone")
        ref = self.get_config_period(ref_id)
        tz = ref.get("timezone") or "UTC"
        frm = self._snap_to_midnight_utc(from_iso, tz)
        to = self._snap_to_midnight_utc(to_iso, tz) if to_iso else None
        if to is not None and to <= frm:
            raise ValueError("create_covering_period: to must be after from")
        with self._conn:
            cur = self._conn.execute(
                """INSERT INTO config_periods
                       (effective_from, effective_to, billing_day, block_minutes,
                        timezone, currency_symbol, currency_code, site_name,
                        supplier, change_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (frm, to, ref["billing_day"], ref["block_minutes"], ref["timezone"],
                 ref["currency_symbol"], ref["currency_code"], site_name,
                 ref.get("supplier"), "Historical site (pre-import)"))
            new_id = cur.lastrowid
            self._copy_meters_to_period(ref_id, new_id)
        if outcode:
            self.set_period_postcode(new_id, outcode, "octopus")
        logger.info("create_covering_period: [%s,%s) region=%s site=%r → period %d",
                    frm, to, outward_code(outcode), site_name, new_id)
        return new_id

    def _period_starting_at(self, from_iso: str) -> Optional[int]:
        """Id of a config period whose effective_from equals from_iso (snapped in
        the timeline's timezone), or None. Gives apply_pre_import_sites its
        idempotency: a re-run updates the existing period instead of duplicating."""
        if not from_iso:
            return None
        row = self._conn.execute(
            "SELECT timezone FROM config_periods ORDER BY effective_from ASC LIMIT 1"
        ).fetchone()
        tz = (row["timezone"] if row else None) or "UTC"
        snapped = self._snap_to_midnight_utc(from_iso, tz)
        hit = self._conn.execute(
            "SELECT id FROM config_periods WHERE effective_from = ? LIMIT 1",
            (snapped,)).fetchone()
        return hit["id"] if hit else None

    def _stamp_region_if_free(self, period_id: int, oc: str) -> bool:
        """Stamp outward code (source 'octopus') onto a period's main meter unless
        it already carries a USER value. Returns True if it changed anything."""
        m = self._conn.execute(
            "SELECT postcode_prefix, postcode_source FROM meters "
            "WHERE config_period_id = ? AND is_sub_meter = 0 ORDER BY id ASC LIMIT 1",
            (period_id,)).fetchone()
        if not m:
            return False
        try:
            src = m["postcode_source"]
        except (KeyError, IndexError):
            src = None
        if m["postcode_prefix"] and src == "user":
            return False
        if m["postcode_prefix"] == oc and src == "octopus":
            return False
        self.set_period_postcode(period_id, oc, "octopus")
        return True

    def plan_pre_import_sites(self, derived: list) -> dict:
        """Read-only. Classify derived tenancy spans (derive_region_periods) for
        the PRE-import confirmation UI:

            {"needs_confirmation": bool,
             "sites": [{outcode, from, to, key, hint, is_current,
                        site_name(prefill|None), needs_name}]}

        The latest span (to=None) is the CURRENT site — shown read-only and
        prefilled from the active period's site_name, because that name is the
        instance / backup-folder identity (see instance.py) and must not change on
        a historic import. Earlier spans are PAST sites the user names; each will
        become a new covering period. needs_confirmation is True only when there
        is at least one past site to name (a single-site account confirms nothing
        and goes straight to import). Nothing is mutated."""
        spans = []
        for s in (derived or []):
            oc = outward_code(s.get("outcode"))
            if not oc:
                continue
            spans.append({"outcode": oc, "from": s.get("from"), "to": s.get("to"),
                          "key": s.get("key"), "hint": s.get("hint")})
        spans.sort(key=lambda s: s.get("from") or "")
        if not spans:
            return {"needs_confirmation": False, "sites": []}
        arow = self._conn.execute(
            "SELECT site_name FROM config_periods WHERE effective_to IS NULL "
            "ORDER BY effective_from DESC LIMIT 1").fetchone()
        active_name = arow["site_name"] if arow else None
        out = []
        for s in spans:
            is_current = s.get("to") is None
            out.append({**s, "is_current": is_current,
                        "site_name": active_name if is_current else None,
                        "needs_name": (not is_current)})
        return {"needs_confirmation": any(x["needs_name"] for x in out),
                "sites": out}

    def apply_pre_import_sites(self, sites: list) -> dict:
        """Create/extend config periods to match confirmed tenancy sites BEFORE an
        import runs, so imported blocks land in the right period from the first
        write. ``sites`` = [{outcode, from, to, site_name?}] (as confirmed in the
        UI). Order of operations matters for contiguity:

          1. CURRENT tenancy (to=None): extend the ACTIVE period's effective_from
             back to its move-in (backward only — never shrinks, never renames:
             instance identity) and stamp its region. Done FIRST, while the active
             period is still the earliest, so the extend targets it.
          2. PAST tenancies: create a covering period for each (or, on a re-run,
             update the existing one). Each abuts the next via the tenancy spans.

        Idempotent. Returns {"created", "extended", "stamped"}."""
        clean = sorted([s for s in (sites or []) if s.get("from")],
                       key=lambda s: s["from"])
        if not clean:
            return {"created": 0, "extended": False, "stamped": 0}
        current = next((s for s in clean if s.get("to") is None), None)
        past = [s for s in clean if s.get("to") is not None]
        extended = False
        stamped = 0
        # 1. Current tenancy → extend + region-stamp the active period (no rename).
        if current:
            extended = self.extend_earliest_period_to(current["from"])
            oc = outward_code(current.get("outcode"))
            aid = self.get_current_config_period_id()
            if oc and aid and self._stamp_region_if_free(aid, oc):
                stamped += 1
        # 2. Past tenancies → create (or update) covering periods.
        created = 0
        for s in past:
            oc = outward_code(s.get("outcode"))
            existing = self._period_starting_at(s["from"])
            if existing is not None:
                if oc:
                    self.set_period_postcode(existing, oc, "octopus")
                if s.get("site_name"):
                    with self._conn:
                        self._conn.execute(
                            "UPDATE config_periods SET site_name = ? "
                            "WHERE id = ? AND effective_to IS NOT NULL",
                            (s["site_name"], existing))
            else:
                self.create_covering_period(
                    s["from"], s["to"], outcode=oc, site_name=s.get("site_name"))
                created += 1
        logger.info("apply_pre_import_sites: created=%d extended=%s stamped=%d",
                    created, extended, stamped)
        return {"created": created, "extended": extended, "stamped": stamped}

    def _snap_to_midnight_utc(self, raw_from: str, tz_name: str) -> str:
        """Snap a UTC ISO timestamp to local midnight, returned as UTC ISO."""
        try:
            from zoneinfo import ZoneInfo as _ZI
            from datetime import datetime as _dt2, timezone as _tz2
            raw_dt = _dt2.fromisoformat(raw_from.replace(" ", "T").split(".")[0])
            raw_dt_utc = raw_dt.replace(tzinfo=_tz2.utc)
            local_dt = raw_dt_utc.astimezone(_ZI(tz_name))
            midnight_local = local_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            return midnight_local.astimezone(_tz2.utc).replace(tzinfo=None).strftime(
                "%Y-%m-%dT%H:%M:%S"
            )
        except Exception:
            return raw_from.replace(" ", "T").split(".")[0]

    def _write_meters(self, config_json: dict, period_id: int) -> None:
        """
        Upsert meters and meter_channels rows
        for a config period from a config dict.
        """
        for meter_id, meter_cfg in (config_json.get("meters") or {}).items():
            if not meter_id:
                continue          # never write a NULL/empty meter_id row
            meta = meter_cfg.get("meta") or {}
            is_sub      = 1 if meta.get("sub_meter") else 0
            parent      = meta.get("parent_meter")
            device      = meta.get("device")
            protected   = 1 if meta.get("protected") else 0
            inv_poss    = 1 if (meta.get("meter_type") == "battery" or meta.get("inverter_possible")) else 0
            power_s     = meta.get("power_sensor")
            power_src   = meta.get("power_source")
            rate_src    = meta.get("rate_source")
            postcode    = meta.get("postcode_prefix")
            v2x         = 1 if meta.get("v2x_capable") else 0
            meter_type  = meta.get("meter_type")
            soc_s       = meta.get("soc_sensor")
            inv_pwr_s    = meta.get("inverter_power_sensor")
            inv_invert   = 1 if meta.get("inverter_power_invert") else 0
            pwr_invert   = 1 if meta.get("power_invert") else 0
            dev_invert   = 1 if meta.get("device_power_invert") else 0
            dev_pwr_s   = meta.get("device_power_sensor")
            pv_pwr_s    = meta.get("pv_power_sensor")

            postcode_src = meta.get("postcode_source")
            cur = self._conn.execute(
                """INSERT INTO meters
                       (config_period_id, meter_id, is_sub_meter, parent_meter_id,
                        device_label, protected, inverter_possible,
                        power_sensor, power_source, postcode_prefix, postcode_source, v2x_capable,
                        meter_type, soc_sensor, inverter_power_sensor, inverter_power_invert,
                        device_power_sensor, pv_power_sensor, rate_source, power_invert,
                        device_power_invert)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(config_period_id, meter_id) DO UPDATE SET
                       is_sub_meter            = excluded.is_sub_meter,
                       parent_meter_id         = excluded.parent_meter_id,
                       device_label            = excluded.device_label,
                       protected               = excluded.protected,
                       inverter_possible       = excluded.inverter_possible,
                       power_sensor            = excluded.power_sensor,
                       power_source            = excluded.power_source,
                       rate_source             = excluded.rate_source,
                       postcode_prefix         = excluded.postcode_prefix,
                       postcode_source         = excluded.postcode_source,
                       v2x_capable             = excluded.v2x_capable,
                       meter_type              = excluded.meter_type,
                       soc_sensor              = excluded.soc_sensor,
                       inverter_power_sensor   = excluded.inverter_power_sensor,
                       inverter_power_invert   = excluded.inverter_power_invert,
                       power_invert            = excluded.power_invert,
                       device_power_invert     = excluded.device_power_invert,
                       device_power_sensor     = excluded.device_power_sensor,
                       pv_power_sensor         = excluded.pv_power_sensor,
                       retired_at              = COALESCE(meters.retired_at, excluded.retired_at),
                       retired_reason          = COALESCE(meters.retired_reason, excluded.retired_reason)""",
                (period_id, meter_id, is_sub, parent, device,
                 protected, inv_poss, power_s, power_src, postcode, postcode_src, v2x,
                 meter_type, soc_s, inv_pwr_s, inv_invert, dev_pwr_s, pv_pwr_s, rate_src, pwr_invert,
                 dev_invert)
            )
            meter_row_id = cur.lastrowid or self._conn.execute(
                "SELECT id FROM meters WHERE config_period_id=? AND meter_id=?",
                (period_id, meter_id)
            ).fetchone()["id"]

            for channel, ch_cfg in (meter_cfg.get("channels") or {}).items():
                if not channel:
                    continue          # never write a NULL/empty channel name
                ch_meta = ch_cfg.get("meta") or {}
                self._conn.execute(
                    """INSERT INTO meter_channels
                           (meter_id, channel, read_sensor, rate_sensor,
                            standing_charge_sensor, mpan, tariff,
                            rate_source, standing_charge_source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(meter_id, channel) DO UPDATE SET
                           read_sensor            = excluded.read_sensor,
                           rate_sensor            = excluded.rate_sensor,
                           standing_charge_sensor = excluded.standing_charge_sensor,
                           mpan                   = excluded.mpan,
                           tariff                 = excluded.tariff,
                           rate_source            = excluded.rate_source,
                           standing_charge_source = excluded.standing_charge_source""",
                    (
                        meter_row_id, channel,
                        ch_cfg.get("read"),
                        ch_cfg.get("rate"),
                        ch_cfg.get("standing_charge_sensor"),
                        ch_meta.get("mpan"),
                        ch_meta.get("tariff"),
                        ch_cfg.get("rate_source"),
                        ch_cfg.get("standing_charge_source"),
                    )
                )
                # A device's old live sensor is kept for history on a rename unless the save says
                # no (the rename prompt, device sensors design §3); the main meter has no history
                # sensors (§2), so its old one is kept only while a block points at it.
                self._sync_device_sensors(meter_id, channel, ch_cfg.get("read"),
                                          ch_cfg.get("history_reads") if is_sub else None,
                                          keep_old=bool(is_sub) and ch_cfg.get("keep_old_read", True)
                                          is not False)

    # ── Device sensors (device sensors design §2–§3, 19 Oct 2026) ───────────────────────
    def _sync_device_sensors(self, meter_id: str, channel: str, read, history_reads=None,
                             *, keep_old: bool = True) -> None:
        """Bring `device_sensors` to a channel's config on a save (inside the caller's
        transaction). `read` becomes the PRIMARY. A different primary before it is kept
        (`keep_old`, the rename prompt's default yes) as the FIRST secondary, `origin='rename'`,
        with its id — or, without `keep_old`, kept off the stitch only while a block points at
        it. With `history_reads` given (a list) the secondaries become exactly that list, in
        order; omitted (None), they are left as they are, so a save that does not know them
        keeps them. A secondary dropped from the list is deleted unless a block points at it."""
        from datetime import datetime as _dt, timezone as _tz
        now = _dt.now(_tz.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        c = self._conn
        read = (read or "").strip() or None
        cur = c.execute("SELECT id, entity_id FROM device_sensors WHERE meter_id=? AND channel=? "
                        "AND role='primary'", (meter_id, channel)).fetchone()
        kept = None           # a primary this save replaced and kept for history
        if not (cur and cur["entity_id"] == read):
            if cur:
                if keep_old:      # first among the secondaries
                    c.execute("UPDATE device_sensors SET position = position + 1 WHERE meter_id=? "
                              "AND channel=? AND role='secondary' AND position IS NOT NULL",
                              (meter_id, channel))
                c.execute("UPDATE device_sensors SET role='secondary', position=?, origin='rename' "
                          "WHERE id=?", (0 if keep_old else None, cur["id"]))
                if keep_old:
                    kept = cur["entity_id"]
                else:
                    self._drop_unpointed_sensor(cur["id"])
            if read:
                had = c.execute("SELECT id FROM device_sensors WHERE meter_id=? AND channel=? AND "
                                "entity_id=?", (meter_id, channel, read)).fetchone()
                if had:           # a secondary promoted back to primary
                    c.execute("UPDATE device_sensors SET role='primary', position=NULL WHERE id=?",
                              (had["id"],))
                else:
                    c.execute("INSERT INTO device_sensors (meter_id, channel, entity_id, role, "
                              "origin, added_at) VALUES (?,?,?,'primary','configured',?)",
                              (meter_id, channel, read, now))
            self._renumber_secondaries(meter_id, channel)
        if history_reads is None:
            return
        # The page saves back the list it loaded, from before this save's rename: the sensor
        # just kept stays first unless the list itself names it.
        want = [kept] if kept and kept not in (history_reads or []) else []
        for e in history_reads:
            e = (e or "").strip()
            if e and e != read and e not in want:
                want.append(e)
        have = {r["entity_id"]: r["id"] for r in c.execute(
            "SELECT id, entity_id FROM device_sensors WHERE meter_id=? AND channel=? "
            "AND role='secondary'", (meter_id, channel))}
        for e, sid in have.items():
            if e not in want:
                c.execute("UPDATE device_sensors SET position=NULL WHERE id=?", (sid,))
                self._drop_unpointed_sensor(sid)
        for i, e in enumerate(want):
            if e in have:
                c.execute("UPDATE device_sensors SET position=? WHERE id=?", (i, have[e]))
            else:
                c.execute("INSERT INTO device_sensors (meter_id, channel, entity_id, role, position, "
                          "origin, added_at) VALUES (?,?,?,'secondary',?,'configured',?)",
                          (meter_id, channel, e, i, now))

    def _renumber_secondaries(self, meter_id: str, channel: str) -> None:
        rows = self._conn.execute(
            "SELECT id FROM device_sensors WHERE meter_id=? AND channel=? AND role='secondary' "
            "AND position IS NOT NULL ORDER BY position, id", (meter_id, channel)).fetchall()
        for i, r in enumerate(rows):
            self._conn.execute("UPDATE device_sensors SET position=? WHERE id=?", (i, r["id"]))

    def _drop_unpointed_sensor(self, sensor_id: int) -> None:
        """Delete a secondary no block or HA read points at; one pointed at stays (off the
        stitch), so the block or read still names the sensor that recorded it."""
        self._conn.execute(
            "DELETE FROM device_sensors WHERE id=? AND role='secondary' AND NOT EXISTS "
            "(SELECT 1 FROM blocks WHERE imp_sensor_id=? OR exp_sensor_id=?) AND NOT EXISTS "
            "(SELECT 1 FROM ha_reads WHERE sensor_id=?)",
            (sensor_id, sensor_id, sensor_id, sensor_id))

    def ha_reads_for(self, meter_id: str, channel: str = "import", lo: Optional[str] = None,
                     hi: Optional[str] = None) -> tuple:
        """A device's `ha_reads` as Device History rebuilds from them (soft delete design §2):
        (`hours` {hour start: kWh} — the recorder's hours, to be split by the house's shape —
        and `fixed` {half-hour start: row} — what is put back as it was: a live half-hour, or a
        recorder half-hour v4 left alone; live wins). `lo`/`hi` bound the period start, [lo, hi)."""
        sql = ("SELECT * FROM ha_reads WHERE meter_id = ? AND channel = ?"
               + (" AND period_start >= ?" if lo else "") + (" AND period_start < ?" if hi else ""))
        args = [meter_id, channel] + [x for x in (lo, hi) if x]
        hours, fixed = {}, {}
        for r in self._conn.execute(sql + " ORDER BY origin DESC", args):   # recorder, then live
            r = dict(r)
            if r["origin"] == "recorder" and r["minutes"] == 60:
                hours[r["period_start"]] = r["kwh"]
            else:
                fixed[r["period_start"]] = r
        return hours, fixed

    def device_stitch_sensors(self, meter_id: str, channel: str = "import") -> list:
        """A device's sensors in Device History's stitch order: its primary, then its ordered
        secondaries (one kept only because a block points at it, `position` NULL, is not)."""
        return [r[0] for r in self._conn.execute(
            "SELECT entity_id FROM device_sensors WHERE meter_id = ? AND channel = ? AND "
            "(role = 'primary' OR position IS NOT NULL) ORDER BY role = 'secondary', position",
            (meter_id, channel))]

    def ha_read_devices(self, lo: str, hi: str) -> list:
        """The sub-meters with `ha_reads` in [lo, hi) — a refill's devices to rebuild. An hour
        row is counted from its start: one starting before `lo` is not."""
        return [r[0] for r in self._conn.execute(
            "SELECT DISTINCT h.meter_id FROM ha_reads h JOIN meters m ON m.meter_id = h.meter_id "
            "AND m.config_period_id = (SELECT id FROM config_periods WHERE effective_to IS NULL "
            "ORDER BY effective_from DESC LIMIT 1) WHERE m.is_sub_meter = 1 AND "
            "h.period_start >= ? AND h.period_start < ? ORDER BY h.meter_id", (lo, hi))]

    def point_block_at_sensor(self, block_start: str, meter_id: str, sensor_id) -> None:
        """A device half-hour put back from `ha_reads` names the sensor that recorded it, not
        today's primary (the new row's default) — or none, `sensor_id` None, where that is not
        known."""
        with self._conn:
            self._conn.execute("UPDATE blocks SET imp_sensor_id = ? WHERE block_start = ? AND "
                               "meter_id = ?", (sensor_id, block_start, meter_id))

    def record_recorder_hours(self, meter_id: str, channel: str, hours: dict,
                              sources: Optional[dict] = None) -> int:
        """Keep the hours Device History fetched from HA's recorder (soft delete design §2):
        one `ha_reads` row per hour, AS the hour (`minutes` 60), `sensor_id` the entry whose
        statistics gave it (`sources`: hour -> entity id). A later fetch replaces an hour; a
        live half-hour is a different row and is never touched. One transaction."""
        if not hours:
            return 0
        ids = {r["entity_id"]: r["id"] for r in self._conn.execute(
            "SELECT id, entity_id FROM device_sensors WHERE meter_id=? AND channel=?",
            (meter_id, channel))}
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        rows = [(meter_id, channel, h, ids.get((sources or {}).get(h)), float(k), now)
                for h, k in hours.items() if k is not None]
        with self._conn:
            self._conn.executemany(
                "INSERT INTO ha_reads (meter_id, channel, origin, period_start, minutes, "
                "sensor_id, kwh, recorded_at) VALUES (?, ?, 'recorder', ?, 60, ?, ?, ?) "
                "ON CONFLICT (meter_id, channel, origin, period_start) DO UPDATE SET "
                "minutes = 60, sensor_id = excluded.sensor_id, kwh = excluded.kwh, "
                "recorded_at = excluded.recorded_at", rows)
        return len(rows)

    def set_device_history_sensors(self, meter_id: str, channel: str, entities: list) -> list:
        """A channel's secondaries become exactly `entities`, in order, its primary untouched
        (Device History's editor until the device screen has one — device sensors design §2,
        PR 3). Returns the secondaries as stored."""
        with self._conn:
            cur = self._conn.execute(
                "SELECT entity_id FROM device_sensors WHERE meter_id=? AND channel=? "
                "AND role='primary'", (meter_id, channel)).fetchone()
            self._sync_device_sensors(meter_id, channel, cur["entity_id"] if cur else None,
                                      list(entities or []))
        return [r["entity_id"] for r in self._conn.execute(
            "SELECT entity_id FROM device_sensors WHERE meter_id=? AND channel=? "
            "AND role='secondary' AND position IS NOT NULL ORDER BY position", (meter_id, channel))]

    def seed_device_sensors(self) -> int:
        """A database from before `device_sensors` (an earlier v5, or a v4 import): give each
        channel of the current config period that has a read sensor its primary entry.
        Idempotent — a channel that has a primary is left alone. Returns entries added."""
        pid = self.get_current_config_period_id()
        if pid is None:
            return 0
        rows = self._conn.execute(
            "SELECT m.meter_id, mc.channel, mc.read_sensor FROM meters m "
            "JOIN meter_channels mc ON mc.meter_id = m.id "
            "WHERE m.config_period_id = ? AND mc.read_sensor IS NOT NULL AND mc.read_sensor != '' "
            "AND NOT EXISTS (SELECT 1 FROM device_sensors s WHERE s.meter_id = m.meter_id "
            "                AND s.channel = mc.channel AND s.role = 'primary')", (pid,)).fetchall()
        if not rows:
            return 0
        with self._conn:
            for r in rows:
                self._sync_device_sensors(r["meter_id"], r["channel"], r["read_sensor"])
        return len(rows)

    def config_from_db(self, period_id: int) -> dict:
        """
        Reconstruct a config dict (matching the old meters_config.json shape)
        from the normalised meters/meter_channels tables.
        """
        cp = self.get_config_period(period_id)
        if not cp:
            return {"schema_version": "1.0", "meters": {}}

        meters_out = {}
        m_rows = self._conn.execute(
            "SELECT * FROM meters WHERE config_period_id=? ORDER BY id",
            (period_id,)
        ).fetchall()

        for m in m_rows:
            mid = m["meter_id"]
            if not mid:
                continue          # skip a NULL/empty meter_id row (legacy/pre-constraint) — a None dict key crashes jsonify

            meta = {
                "billing_day":    cp["billing_day"],
                "block_minutes":  cp["block_minutes"],
                "timezone":       cp["timezone"],
                "currency_symbol": cp["currency_symbol"],
                "currency_code":  cp["currency_code"],
                "site":           cp["site_name"],
            }
            if cp["supplier"]:
                meta["supplier"] = cp["supplier"]
            if m["is_sub_meter"]:
                meta["sub_meter"] = True
            if m["parent_meter_id"]:
                meta["parent_meter"] = m["parent_meter_id"]
            if m["device_label"]:
                meta["device"] = m["device_label"]
            if m["protected"]:
                meta["protected"] = True
            if m["inverter_possible"]:
                meta["inverter_possible"] = True
            if m["power_sensor"]:
                meta["power_sensor"] = m["power_sensor"]
            try:
                if m["power_source"]:
                    meta["power_source"] = m["power_source"]
                if m["rate_source"]:
                    meta["rate_source"] = m["rate_source"]
            except (KeyError, IndexError):
                pass
            if m["postcode_prefix"]:
                meta["postcode_prefix"] = m["postcode_prefix"]
            try:
                if m["postcode_source"]:
                    meta["postcode_source"] = m["postcode_source"]
            except (KeyError, IndexError):
                pass
            if m["v2x_capable"]:
                meta["v2x_capable"] = True
            try:
                if m["meter_type"]:
                    meta["meter_type"] = m["meter_type"]
                if m["soc_sensor"]:
                    meta["soc_sensor"] = m["soc_sensor"]
                if m["inverter_power_sensor"]:
                    meta["inverter_power_sensor"] = m["inverter_power_sensor"]
                if m["inverter_power_invert"]:
                    meta["inverter_power_invert"] = True
                if "power_invert" in m.keys() and m["power_invert"]:
                    meta["power_invert"] = True
                if "device_power_invert" in m.keys() and m["device_power_invert"]:
                    meta["device_power_invert"] = True
                if m["device_power_sensor"]:
                    meta["device_power_sensor"] = m["device_power_sensor"]
                if m["pv_power_sensor"]:
                    meta["pv_power_sensor"] = m["pv_power_sensor"]
                if m["retired_at"]:
                    meta["retired_at"] = m["retired_at"]
                if m["retired_reason"]:
                    meta["retired_reason"] = m["retired_reason"]
            except IndexError:
                pass

            channels = {}
            # device sensors design §2: the channel's secondaries come with it, in stitch order,
            # in the same statement (a config load is on every surface's query budget)
            ch_rows = self._conn.execute(
                "SELECT mc.*, (SELECT json_group_array(entity_id) FROM ("
                "    SELECT s.entity_id FROM device_sensors s WHERE s.meter_id = ? "
                "    AND s.channel = mc.channel AND s.role = 'secondary' "
                "    AND s.position IS NOT NULL ORDER BY s.position)) AS history_json "
                "FROM meter_channels mc WHERE mc.meter_id=?", (mid, m["id"])
            ).fetchall()
            for ch in ch_rows:
                ch_dict = {}
                if ch["read_sensor"]:
                    ch_dict["read"] = ch["read_sensor"]
                if ch["rate_sensor"]:
                    ch_dict["rate"] = ch["rate_sensor"]
                if ch["standing_charge_sensor"]:
                    ch_dict["standing_charge_sensor"] = ch["standing_charge_sensor"]
                # 3.0.0 per-channel source toggles (API vs sensor; 'main' = device
                # inherits the main meter's effective rate). Guarded for older DBs.
                try:
                    if ch["rate_source"]:
                        ch_dict["rate_source"] = ch["rate_source"]
                    if ch["standing_charge_source"]:
                        ch_dict["standing_charge_source"] = ch["standing_charge_source"]
                except (KeyError, IndexError):
                    pass
                # mpan / tariff as channel meta dict (preserves engine-expected shape)
                ch_meta = {}
                if ch["mpan"]:
                    ch_meta["mpan"] = ch["mpan"]
                if ch["tariff"]:
                    ch_meta["tariff"] = ch["tariff"]
                if ch_meta:
                    ch_dict["meta"] = ch_meta
                if not ch["channel"]:
                    continue          # skip a NULL/empty channel (legacy pre-constraint row) — a None dict key crashes jsonify
                # device sensors design §2: the secondaries Device History stitches, in order
                ch_dict["history_reads"] = json.loads(ch["history_json"] or "[]")
                channels[ch["channel"]] = ch_dict

            # Engine compatibility: the overlay path reads meta["rate_source"].
            # For a device, derive it from the (authoritative) import-channel
            # source so the existing engine honours the explicit choice:
            #   'main'   → 'overlay' (price on the main meter's effective rate)
            #   'sensor' → 'own'     (the device's own rate sensor wins)
            if meta.get("sub_meter"):
                _imp_src = (channels.get("import") or {}).get("rate_source")
                if _imp_src == "main":
                    meta["rate_source"] = "overlay"
                elif _imp_src == "sensor":
                    meta["rate_source"] = "own"

            meters_out[mid] = {"meta": meta, "channels": channels}

        return {"schema_version": "1.0", "meters": meters_out}

    def insert_config_period(self,
                             config_json: dict,
                             effective_from: Optional[str] = None,
                             change_reason: Optional[str] = None) -> int:
        """
        Snapshot the current config as a new config period.
        Closes the previous period's effective_to.
        Writes meter definitions to the normalised meters/meter_channels tables.
        Returns the new period's id.
        """
        main_meta = {}
        for m in config_json.get("meters", {}).values():
            if not (m.get("meta") or {}).get("sub_meter"):
                main_meta = m.get("meta") or {}
                break

        tz_name = main_meta.get("timezone", "UTC")
        now = self._snap_to_midnight_utc(effective_from or _utc_now_iso(), tz_name)

        with self._conn:
            self._conn.execute(
                "UPDATE config_periods SET effective_to = ? WHERE effective_to IS NULL",
                (now,)
            )
            cur = self._conn.execute(
                """INSERT INTO config_periods
                       (effective_from, effective_to, billing_day, block_minutes,
                        timezone, currency_symbol, currency_code, site_name,
                        supplier, change_reason)
                   VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    now,
                    int(main_meta.get("billing_day") or 1),
                    int(main_meta.get("block_minutes") or 30),
                    main_meta.get("timezone", "UTC"),
                    main_meta.get("currency_symbol", "£"),
                    main_meta.get("currency_code", "GBP"),
                    main_meta.get("site", main_meta.get("site_name")),
                    main_meta.get("supplier"),
                    change_reason,
                )
            )
            period_id = cur.lastrowid
            self._write_meters(config_json, period_id)

        logger.info("insert_config_period: new period id=%d effective_from=%s", period_id, now)
        return period_id

    # ── Region timeline: historical config-period split ───────────────────────
    def _copy_meters_to_period(self, src_period_id: int, dst_period_id: int) -> None:
        """Deep-copy every meter (and its channels) from one config period to
        another, preserving all columns except the surrogate keys. Used by
        split_config_period_at so the new half is a faithful config clone."""
        m_cols = [r[1] for r in self._conn.execute("PRAGMA table_info(meters)").fetchall()]
        copy_cols = [c for c in m_cols if c not in ("id", "config_period_id")]
        col_list = ", ".join(copy_cols)
        placeholders = ", ".join("?" for _ in copy_cols)
        src_rows = self._conn.execute(
            f"SELECT id, {col_list} FROM meters WHERE config_period_id = ?",
            (src_period_id,)
        ).fetchall()
        mc_cols = [r[1] for r in self._conn.execute("PRAGMA table_info(meter_channels)").fetchall()]
        ch_cols = [c for c in mc_cols if c not in ("id", "meter_id")]
        ch_list = ", ".join(ch_cols)
        ch_ph = ", ".join("?" for _ in ch_cols)
        for row in src_rows:
            old_meter_id = row["id"]
            vals = [row[c] for c in copy_cols]
            cur = self._conn.execute(
                f"INSERT INTO meters (config_period_id, {col_list}) VALUES (?, {placeholders})",
                [dst_period_id] + vals
            )
            new_meter_id = cur.lastrowid
            if ch_cols:
                for ch in self._conn.execute(
                    f"SELECT {ch_list} FROM meter_channels WHERE meter_id = ?",
                    (old_meter_id,)
                ).fetchall():
                    self._conn.execute(
                        f"INSERT INTO meter_channels (meter_id, {ch_list}) VALUES (?, {ch_ph})",
                        [new_meter_id] + [ch[c] for c in ch_cols]
                    )

    def split_config_period_at(self, period_id: int, split_date_iso: str,
                               *, change_reason: Optional[str] = None) -> int:
        """Divide an existing config period at a past date into two contiguous
        periods, preserving billing history.

        The ORIGINAL period keeps the earlier span ``[effective_from, split)``.
        A NEW period covers the later span ``[split, effective_to)`` with a full
        COPY of the original's meters + channels. Every block whose ``block_start``
        is >= split is reassigned to the new period; earlier blocks stay.

        This is the primitive the region timeline needs to record a mid-history
        property move (``insert_config_period`` only ever appends a *new current*
        period). The caller sets the region/site on whichever half applies.

        ``split_date_iso`` is snapped to local midnight (the period's timezone),
        matching ``insert_config_period``. Returns the NEW period's id.

        Raises ``ValueError`` if the split is not strictly inside the period.
        """
        cp = self.get_config_period(period_id)
        if not cp:
            raise ValueError(f"config period {period_id} not found")
        tz_name = cp.get("timezone") or "UTC"
        split = self._snap_to_midnight_utc(split_date_iso, tz_name)
        ef_from = cp["effective_from"]
        ef_to   = cp.get("effective_to")   # None for the active period
        if split <= ef_from:
            raise ValueError("split date must be after the period's effective_from")
        if ef_to is not None and split >= ef_to:
            raise ValueError("split date must be before the period's effective_to")

        with self._conn:
            cur = self._conn.execute(
                """INSERT INTO config_periods
                       (effective_from, effective_to, billing_day, block_minutes,
                        timezone, currency_symbol, currency_code, site_name,
                        supplier, change_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    split, ef_to,
                    cp["billing_day"], cp["block_minutes"], cp["timezone"],
                    cp["currency_symbol"], cp["currency_code"],
                    cp.get("site_name"), cp.get("supplier"),
                    change_reason or "Region timeline split",
                )
            )
            new_id = cur.lastrowid
            self._copy_meters_to_period(period_id, new_id)
            # Shrink the original to the earlier half.
            self._conn.execute(
                "UPDATE config_periods SET effective_to = ? WHERE id = ?",
                (split, period_id)
            )
            # Reassign the later blocks to the new period.
            self._conn.execute(
                "UPDATE blocks SET config_period_id = ? "
                "WHERE config_period_id = ? AND block_start >= ?",
                (new_id, period_id, split)
            )
        logger.info("split_config_period_at: period %d split at %s → new period %d",
                    period_id, split, new_id)
        return new_id

    def migrate_full_config_json(self) -> int:
        """
        One-time migration for 2.0→2.1 upgrade. Safe to call on every startup.

        Steps (each guarded independently so partial upgrades resume correctly):
          1. full_config_json → normalised meters/meter_channels + column drop
          2. gap_marker blob  → gap_detected_at column + is_gap_seed rows
          3. is_gap_seed column added to current_reads if missing
          4. mpan/tariff columns added to meter_channels if missing

        Returns the number of config periods whose meters were migrated (step 1).
        """
        migrated = 0

        # ── Step 0: upgrade meters table schema if it has the old 2.0.x shape ──
        # The 2.0.x meters table lacked protected, inverter_possible,
        # power_sensor, postcode_prefix. Recreate with the full 2.1.0 schema.
        m_cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(meters)"
        ).fetchall()}
        if m_cols and "protected" not in m_cols:
            try:
                with self._conn:
                    self._conn.execute("""
                        CREATE TABLE IF NOT EXISTS meters_new (
                            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                            config_period_id   INTEGER NOT NULL,
                            meter_id           TEXT    NOT NULL,
                            is_sub_meter       INTEGER NOT NULL DEFAULT 0,
                            parent_meter_id    TEXT,
                            device_label       TEXT,
                            protected          INTEGER DEFAULT 0,
                            inverter_possible  INTEGER DEFAULT 0,
                            power_sensor       TEXT,
                            postcode_prefix    TEXT,
                            supplier           TEXT,
                            v2x_capable        INTEGER DEFAULT 0,
                            meter_type         TEXT,
                            soc_sensor         TEXT,
                            inverter_power_sensor TEXT,
                            device_power_sensor TEXT,
                            FOREIGN KEY (config_period_id) REFERENCES config_periods(id),
                            UNIQUE (config_period_id, meter_id)
                        )
                    """)
                    # Copy compatible columns from old meters table
                    old_m_cols = {r[1] for r in self._conn.execute(
                        "PRAGMA table_info(meters)"
                    ).fetchall()}
                    common = {"id", "config_period_id", "meter_id", "is_sub_meter",
                              "parent_meter_id", "device_label"} & old_m_cols
                    col_list = ", ".join(sorted(common))
                    self._conn.execute(f"""
                        INSERT INTO meters_new ({col_list})
                        SELECT {col_list} FROM meters
                    """)
                    self._conn.execute("DROP TABLE meters")
                    self._conn.execute("ALTER TABLE meters_new RENAME TO meters")
                logger.info("migrate_full_config_json: upgraded meters table schema")
            except Exception as _e:
                logger.warning(
                    "migrate_full_config_json: meters schema upgrade failed: %s", _e
                )

        # ── Step 0b: add v2x_capable to meters if missing ───────────────────
        # Must run before Step 1 (_write_meters) which INSERTs this column.
        m_cols_now = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(meters)"
        ).fetchall()}
        for _col, _defn in [("v2x_capable", "INTEGER DEFAULT 0")]:
            if _col not in m_cols_now:
                try:
                    with self._conn:
                        self._conn.execute(
                            f"ALTER TABLE meters ADD COLUMN {_col} {_defn}"
                        )
                    logger.info(
                        "migrate_full_config_json: added %s to meters", _col
                    )
                except Exception as _e:
                    logger.warning(
                        "migrate_full_config_json: %s column add failed: %s", _col, _e
                    )

        # ── Step 1: full_config_json → normalised meter tables ───────────────
        cp_cols = [r[1] for r in self._conn.execute(
            "PRAGMA table_info(config_periods)"
        ).fetchall()]
        if "full_config_json" in cp_cols:
            rows = self._conn.execute(
                "SELECT id, full_config_json FROM config_periods "
                "WHERE full_config_json IS NOT NULL AND full_config_json != ''"
            ).fetchall()
            for row in rows:
                period_id = row["id"]
                existing = self._conn.execute(
                    "SELECT COUNT(*) FROM meters WHERE config_period_id=?", (period_id,)
                ).fetchone()[0]
                if existing:
                    continue
                try:
                    cfg = json.loads(row["full_config_json"])
                    with self._conn:
                        self._write_meters(cfg, period_id)
                    migrated += 1
                except Exception as e:
                    logger.warning(
                        "migrate_full_config_json: period %d failed: %s", period_id, e
                    )
            try:
                # Temporarily disable FK enforcement for table recreation
                self._conn.execute("PRAGMA foreign_keys = OFF")
                with self._conn:
                    self._conn.execute("""
                        CREATE TABLE IF NOT EXISTS config_periods_new (
                            id               INTEGER PRIMARY KEY AUTOINCREMENT,
                            effective_from   TEXT    NOT NULL,
                            effective_to     TEXT,
                            billing_day      INTEGER NOT NULL DEFAULT 1,
                            block_minutes    INTEGER NOT NULL DEFAULT 30,
                            timezone         TEXT    NOT NULL DEFAULT 'UTC',
                            currency_symbol  TEXT    NOT NULL DEFAULT '£',
                            currency_code    TEXT    NOT NULL DEFAULT 'GBP',
                            site_name        TEXT,
                            change_reason    TEXT
                        )
                    """)
                    self._conn.execute("""
                        INSERT INTO config_periods_new
                            (id, effective_from, effective_to, billing_day, block_minutes,
                             timezone, currency_symbol, currency_code, site_name, change_reason)
                        SELECT id, effective_from, effective_to, billing_day, block_minutes,
                               timezone, currency_symbol, currency_code, site_name, change_reason
                        FROM config_periods
                    """)
                    self._conn.execute("DROP TABLE config_periods")
                    self._conn.execute(
                        "ALTER TABLE config_periods_new RENAME TO config_periods"
                    )
                logger.info(
                    "migrate_full_config_json: dropped full_config_json, %d periods migrated",
                    migrated
                )
            except Exception as e:
                logger.warning("migrate_full_config_json: column drop failed: %s", e)
            finally:
                self._conn.execute("PRAGMA foreign_keys = ON")

        # ── Step 2a: is_gap_seed column on current_reads (must precede gap_marker migration) ───────────────────────
        cr_cols = [r[1] for r in self._conn.execute(
            "PRAGMA table_info(current_reads)"
        ).fetchall()]
        if "is_gap_seed" not in cr_cols:
            try:
                with self._conn:
                    self._conn.execute(
                        "ALTER TABLE current_reads "
                        "ADD COLUMN is_gap_seed INTEGER NOT NULL DEFAULT 0"
                    )
                    self._conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_current_reads_gap "
                        "ON current_reads (is_gap_seed)"
                    )
                logger.info(
                    "migrate_full_config_json: added is_gap_seed to current_reads"
                )
            except Exception as _e:
                logger.warning(
                    "migrate_full_config_json: is_gap_seed add failed: %s", _e
                )

        # ── Step 2b: gap_last_block_start column on current_block ────────────
        cb_cols_check = [r[1] for r in self._conn.execute(
            "PRAGMA table_info(current_block)"
        ).fetchall()]
        if "gap_last_block_start" not in cb_cols_check:
            try:
                with self._conn:
                    self._conn.execute(
                        "ALTER TABLE current_block "
                        "ADD COLUMN gap_last_block_start TEXT"
                    )
                logger.info("migrate: added gap_last_block_start to current_block")
            except Exception as _e:
                logger.warning("migrate: gap_last_block_start add failed: %s", _e)

        # ── Step 2c: gap_marker blob → gap_detected_at + is_gap_seed rows ────
        cb_cols = [r[1] for r in self._conn.execute(
            "PRAGMA table_info(current_block)"
        ).fetchall()]
        if "gap_marker" in cb_cols:
            try:
                with self._conn:
                    cb_row = self._conn.execute(
                        "SELECT gap_marker FROM current_block WHERE id=1"
                    ).fetchone()
                    gap_detected_at = None
                    if cb_row and cb_row["gap_marker"]:
                        try:
                            gm = json.loads(cb_row["gap_marker"])
                            gap_detected_at = gm.get("detected_at")
                            for meter_id, channels in (gm.get("pre_reads") or {}).items():
                                for ch, r in (channels or {}).items():
                                    if r:
                                        self._conn.execute(
                                            """INSERT OR IGNORE INTO current_reads
                                               (captured_at, meter_id, channel,
                                                channel_type, value, is_gap_seed)
                                               VALUES (?, ?, ?, 'read', ?, 1)""",
                                            (r.get("ts"), meter_id, ch, r.get("value", 0))
                                        )
                            for meter_id, channels in (gm.get("last_known_rates") or {}).items():
                                for ch, r in (channels or {}).items():
                                    if r:
                                        self._conn.execute(
                                            """INSERT OR IGNORE INTO current_reads
                                               (captured_at, meter_id, channel,
                                                channel_type, value, is_gap_seed)
                                               VALUES (?, ?, ?, 'rate', ?, 2)""",
                                            (r.get("ts"), meter_id, ch, r.get("value", 0))
                                        )
                        except Exception as _ge:
                            logger.warning(
                                "migrate_full_config_json: gap_marker parse failed: %s", _ge
                            )
                    self._conn.execute("""
                        CREATE TABLE IF NOT EXISTS current_block_new (
                            id              INTEGER PRIMARY KEY CHECK (id = 1),
                            block_start     TEXT,
                            block_end       TEXT,
                            last_checkpoint TEXT,
                            gap_detected_at TEXT,
                            interpolated    INTEGER NOT NULL DEFAULT 0
                        )
                    """)
                    self._conn.execute("""
                        INSERT INTO current_block_new
                            (id, block_start, block_end, last_checkpoint,
                             gap_detected_at, interpolated)
                        SELECT id, block_start, block_end, last_checkpoint, ?, interpolated
                        FROM current_block
                    """, (gap_detected_at,))
                    self._conn.execute("DROP TABLE current_block")
                    self._conn.execute(
                        "ALTER TABLE current_block_new RENAME TO current_block"
                    )
                logger.info(
                    "migrate_full_config_json: dropped gap_marker, gap_detected_at set"
                )
            except Exception as _e:
                logger.warning(
                    "migrate_full_config_json: gap_marker migration failed: %s", _e
                )

        # ── Step 4: mpan/tariff columns on meter_channels ─────────────────────
        mc_cols = [r[1] for r in self._conn.execute(
            "PRAGMA table_info(meter_channels)"
        ).fetchall()]
        for _col, _defn in [("mpan", "TEXT"), ("tariff", "TEXT")]:
            if _col not in mc_cols:
                try:
                    with self._conn:
                        self._conn.execute(
                            f"ALTER TABLE meter_channels ADD COLUMN {_col} {_defn}"
                        )
                    logger.info(
                        "migrate_full_config_json: added %s to meter_channels", _col
                    )
                except Exception as _e:
                    logger.warning(
                        "migrate_full_config_json: %s column add failed: %s", _col, _e
                    )

        # ── Step 5: supplier column on config_periods ────────────────────────
        # Supplier belongs on config_periods (not meters) so it has a historical
        # record — each config period records which supplier was active.
        # Attempt to migrate supplier from the full_config_json if still present
        # in any period (2.0.x databases that haven't been fully migrated yet).
        cp_cols = [r[1] for r in self._conn.execute(
            "PRAGMA table_info(config_periods)"
        ).fetchall()]
        if "supplier" not in cp_cols:
            try:
                with self._conn:
                    self._conn.execute(
                        "ALTER TABLE config_periods ADD COLUMN supplier TEXT"
                    )
                logger.info("migrate_full_config_json: added supplier to config_periods")
            except Exception as _e:
                logger.warning(
                    "migrate_full_config_json: supplier column add failed: %s", _e
                )

        return migrated

    # ── Write ─────────────────────────────────────────────────────────────

    def append_block(self, block: dict,
                     config_period_id: Optional[int] = None, *, ha_meters=()) -> None:
        """
        Insert a finalised block (all meters) as one transaction.
        If config_period_id is None, uses the current active period.
        `ha_meters`: the meters whose channels HA reported live — their `ha_reads` rows are
        written in the same transaction (soft delete design §2).
        """
        period_id = config_period_id or self.get_current_config_period_id()
        if period_id is None:
            raise RuntimeError(
                "BlockStore.append_block: no config period exists. "
                "Call insert_config_period() before appending blocks."
            )
        cp = self.get_config_period(period_id)
        tz_name = cp["timezone"] if cp else "UTC"
        rows = _block_rows(block, period_id, tz_name)
        self._insert_block_rows(rows, _staged(block), _live_reads(block, ha_meters))

    def append_blocks(self, blocks: list[dict],
                      config_period_id: Optional[int] = None) -> int:
        """
        Bulk insert a list of blocks. Used by migration and gap fill.
        Returns the number of meter-rows inserted.
        """
        period_id = config_period_id or self.get_current_config_period_id()
        if period_id is None:
            raise RuntimeError(
                "BlockStore.append_blocks: no config period exists."
            )
        cp = self.get_config_period(period_id)
        tz_name = cp["timezone"] if cp else "UTC"
        all_rows = []
        for block in blocks:
            all_rows.extend(_block_rows(block, period_id, tz_name))
        self._insert_block_rows(all_rows)
        return len(all_rows)

    def _insert_block_rows(self, rows: list[dict], staged=(), ha_reads=()) -> None:
        if not rows:
            return
        sql = """
            INSERT OR IGNORE INTO blocks (
                block_start, block_end,
                meter_id, config_period_id, interpolated,
                imp_kwh, imp_kwh_grid,
                imp_rate, imp_cost, imp_cost_exc,
                imp_read_start, imp_read_end,
                exp_kwh, exp_rate, exp_cost,
                exp_read_start, exp_read_end,
                standing_charge, carbon_g, carbon_intensity_g, imp_provisional,
                source, is_provisional, needs_pass2_rerun, imp_kwh_api, needs_review,
                exp_kwh_api,
                rate_corrected, rate_source,
                imp_sensor_id, exp_sensor_id
            ) VALUES (
                :block_start, :block_end,
                :meter_id, :config_period_id, :interpolated,
                :imp_kwh, :imp_kwh_grid,
                :imp_rate, :imp_cost, :imp_cost_exc,
                :imp_read_start, :imp_read_end,
                :exp_kwh, :exp_rate, :exp_cost,
                :exp_read_start, :exp_read_end,
                :standing_charge, :carbon_g, :carbon_intensity_g, :imp_provisional,
                :source, :is_provisional, :needs_pass2_rerun, :imp_kwh_api, :needs_review,
                :exp_kwh_api,
                :rate_corrected, :rate_source,
                CASE WHEN :imp_read_start IS NOT NULL THEN
                    (SELECT s.id FROM device_sensors s WHERE s.meter_id = :meter_id
                        AND s.channel = 'import' AND s.role = 'primary') END,
                CASE WHEN :exp_read_start IS NOT NULL THEN
                    (SELECT s.id FROM device_sensors s WHERE s.meter_id = :meter_id
                        AND s.channel = 'export' AND s.role = 'primary') END
            )
        """
        with self._conn:
            self._conn.executemany(sql, rows)
            for bs, mid, segs in staged:
                self._replace_segments(bs, mid, segs)
            if ha_reads:
                self._conn.executemany(_HA_READS_UPSERT, ha_reads)

    def _insert_block_rows_replace(self, rows: list[dict], staged=(), ha_reads=()) -> None:
        """Like _insert_block_rows but uses INSERT OR REPLACE to overwrite
        existing blocks. Used by gap fill to correct zero/bad blocks in the DB.

        The review columns are CARRIED from the stored row, not taken from the dict
        (4 Oct 2026). A REPLACE deletes the row and inserts a new one, so a column it
        does not list resets to its default — `review_reason` and `review_dismissed`
        were not listed, and `needs_review` came from the block dict, which the
        loader never fills — so every round-trip rewrite (`get_block_dict_by_start`
        → mutate → `append_block_replace`) wiped a block's review history. Since
        BL-74 nothing in v5 sets them, so they are history only, and are kept as
        they are. A brand-new row takes the dict's value (0 / NULL / 0).
        `imp_sensor_id` / `exp_sensor_id` are carried too: a rewrite after a rename must not
        point an old sensor's block at today's primary; only a new row takes it.

        Two more columns the REPLACE dropped are carried the same way, because a
        rewrite does not change what they record: `finalised_from_cad` (a block past
        the settlement horizon that will never DCC-settle — reset, it counted as
        "awaiting settlement" again until the daily sweep re-marked it; settlement
        clears it in upsert_kraken_block, not here) and `derivation_id` (which CSV
        reconstruction a block came from — the CSV upserts already keep it with
        COALESCE(blocks.derivation_id, …); the REPLACE lost it)."""
        if not rows:
            return
        sql = """
            INSERT OR REPLACE INTO blocks (
                block_start, block_end,
                meter_id, config_period_id, interpolated,
                imp_kwh, imp_kwh_grid,
                imp_rate, imp_cost, imp_cost_exc,
                imp_read_start, imp_read_end,
                exp_kwh, exp_rate, exp_cost,
                exp_read_start, exp_read_end,
                standing_charge, carbon_g, carbon_intensity_g, imp_provisional,
                source, is_provisional, needs_pass2_rerun, imp_kwh_api, needs_review,
                review_reason, review_dismissed, finalised_from_cad, derivation_id,
                exp_kwh_api,
                rate_corrected, rate_source,
                imp_sensor_id, exp_sensor_id
            ) VALUES (
                :block_start, :block_end,
                :meter_id, :config_period_id, :interpolated,
                :imp_kwh, :imp_kwh_grid,
                :imp_rate, :imp_cost, :imp_cost_exc,
                :imp_read_start, :imp_read_end,
                :exp_kwh, :exp_rate, :exp_cost,
                :exp_read_start, :exp_read_end,
                :standing_charge, :carbon_g, :carbon_intensity_g, :imp_provisional,
                :source, :is_provisional, :needs_pass2_rerun, :imp_kwh_api,
                COALESCE((SELECT o.needs_review FROM blocks o
                          WHERE o.block_start = :block_start AND o.meter_id = :meter_id),
                         :needs_review),
                (SELECT o.review_reason FROM blocks o
                  WHERE o.block_start = :block_start AND o.meter_id = :meter_id),
                COALESCE((SELECT o.review_dismissed FROM blocks o
                          WHERE o.block_start = :block_start AND o.meter_id = :meter_id), 0),
                COALESCE((SELECT o.finalised_from_cad FROM blocks o
                          WHERE o.block_start = :block_start AND o.meter_id = :meter_id), 0),
                (SELECT o.derivation_id FROM blocks o
                  WHERE o.block_start = :block_start AND o.meter_id = :meter_id),
                :exp_kwh_api,
                :rate_corrected, :rate_source,
                CASE WHEN EXISTS (SELECT 1 FROM blocks o
                                 WHERE o.block_start = :block_start AND o.meter_id = :meter_id)
                     THEN (SELECT o.imp_sensor_id FROM blocks o
                            WHERE o.block_start = :block_start AND o.meter_id = :meter_id)
                     ELSE CASE WHEN :imp_read_start IS NOT NULL THEN
                    (SELECT s.id FROM device_sensors s WHERE s.meter_id = :meter_id
                        AND s.channel = 'import' AND s.role = 'primary') END END,
                CASE WHEN EXISTS (SELECT 1 FROM blocks o
                                 WHERE o.block_start = :block_start AND o.meter_id = :meter_id)
                     THEN (SELECT o.exp_sensor_id FROM blocks o
                            WHERE o.block_start = :block_start AND o.meter_id = :meter_id)
                     ELSE CASE WHEN :exp_read_start IS NOT NULL THEN
                    (SELECT s.id FROM device_sensors s WHERE s.meter_id = :meter_id
                        AND s.channel = 'export' AND s.role = 'primary') END END
            )
        """
        with self._conn:
            self._conn.executemany(sql, rows)
            for bs, mid, segs in staged:
                self._replace_segments(bs, mid, segs)
            if ha_reads:
                self._conn.executemany(_HA_READS_UPSERT, ha_reads)

    def append_block_replace(self, block: dict,
                             config_period_id: Optional[int] = None, *, ha_meters=()) -> None:
        """Insert a finalised block, replacing any existing block at the same
        (block_start, meter_id, config_period_id). Used by gap fill.
        `ha_meters` as append_block's: only a write of what HA reported names any — a
        round-trip rewrite must not, since a settled block's kWh is Octopus's."""
        period_id = config_period_id or self.get_current_config_period_id()
        if period_id is None:
            raise RuntimeError(
                "BlockStore.append_block_replace: no config period exists."
            )
        cp = self.get_config_period(period_id)
        tz_name = cp["timezone"] if cp else "UTC"
        rows = _block_rows(block, period_id, tz_name)
        self._insert_block_rows_replace(rows, _staged(block), _live_reads(block, ha_meters))

    def get_provisional_sub_meter_blocks(self) -> list[dict]:
        """Return the most-recent block for each sub-meter that has imp_provisional=1.

        Used by the 2.10.0 boundary interpolation amendment path: after each
        engine tick we check whether a post-boundary read has arrived for any
        sub-meter whose previous block was written as provisional (no post-boundary
        read at block-close time).  Returns one single-meter block dict per
        provisional sub-meter row.
        """
        try:
            cur = self._conn.execute(
                """
                SELECT b.*, cp.billing_day, cp.block_minutes, cp.timezone, """ + _SPLIT_COLS + """
                       cp.currency_symbol, cp.currency_code, cp.effective_from,
                       m.is_sub_meter, m.parent_meter_id, m.device_label,
                       m.inverter_possible, m.power_sensor, m.postcode_prefix,
                       m.v2x_capable, m.meter_type, m.soc_sensor,
                       m.inverter_power_sensor, m.inverter_power_invert,
                       m.device_power_sensor, m.retired_at, m.retired_reason
                FROM blocks b
                JOIN config_periods cp ON b.config_period_id = cp.id
                LEFT JOIN meters m ON m.meter_id = b.meter_id
                                   AND m.config_period_id = b.config_period_id
                WHERE b.imp_provisional = 1
                  AND m.is_sub_meter = 1
                  AND b.block_start = (
                      SELECT MAX(b2.block_start) FROM blocks b2
                      WHERE b2.meter_id = b.meter_id AND b2.imp_provisional = 1
                  )
                ORDER BY b.block_start, b.meter_id
                """
            )
            rows = cur.fetchall()
        except Exception:
            return []
        result = []
        for row in rows:
            blk = _row_to_block([row])
            if blk:
                result.append(blk)
        return result

    def _select_blocks(self, where: str, params: tuple) -> list[dict]:
        sql = f"""
            SELECT b.*, cp.billing_day, cp.block_minutes, cp.timezone, {_SPLIT_COLS}
                   cp.currency_symbol, cp.currency_code, cp.effective_from,
                   m.is_sub_meter, m.parent_meter_id, m.device_label,
                   m.inverter_possible, m.power_sensor, m.postcode_prefix,
                   m.v2x_capable, m.meter_type, m.soc_sensor, m.inverter_power_sensor, m.inverter_power_invert, m.device_power_sensor
            FROM blocks b
            JOIN config_periods cp ON b.config_period_id = cp.id
            LEFT JOIN meters m ON m.meter_id = b.meter_id
                               AND m.config_period_id = b.config_period_id
            {where}
            ORDER BY b.block_start, b.meter_id
        """
        cur = self._conn.execute(sql, params)
        return _rows_to_blocks(cur.fetchall())

    def get_all_blocks(self) -> list[dict]:
        """Full export — used by chart generation during transition phase."""
        return self._select_blocks("", ())

    # The column order of get_bill_rows' rows; billing.summarise_bill_rows and
    # billing_view unpack by position.
    BILL_ROW_COLUMNS = (
        "block_start", "meter_id", "is_sub_meter", "device_label", "timezone",
        "imp_kwh", "imp_kwh_grid", "imp_rate", "imp_cost",
        "imp_read_start", "imp_read_end", "imp_cost_exc",
        "exp_kwh", "exp_rate", "exp_cost", "exp_read_start", "exp_read_end",
        "standing_charge")

    def get_bill_rows(self, utc_start: str, utc_end: str):
        """A period bill's blocks as flat rows (#48, 3 Oct 2026): `(rows, segments)`.

        `rows` are tuples in BILL_ROW_COLUMNS order, one per block and meter, ordered by
        block_start then meter_id: get_blocks_lightweight's join and order, with only
        the columns the bill reads. `segments` maps (block_start, meter_id) to that
        meter's import segments, (kwh, inc_rate, exc_rate, band, attribution) in seq
        order. It is the same data as get_blocks_lightweight without the nested dicts.
        Decoding 38 columns into them took half a year's bill on the Pi."""
        rows = self._conn.execute(
            """SELECT b.block_start, b.meter_id, m.is_sub_meter, m.device_label, cp.timezone,
                      b.imp_kwh, b.imp_kwh_grid, b.imp_rate, b.imp_cost,
                      b.imp_read_start, b.imp_read_end, b.imp_cost_exc,
                      b.exp_kwh, b.exp_rate, b.exp_cost, b.exp_read_start, b.exp_read_end,
                      b.standing_charge
               FROM blocks b
               JOIN config_periods cp ON b.config_period_id = cp.id
               LEFT JOIN meters m ON m.meter_id = b.meter_id
                 AND m.config_period_id = b.config_period_id
               WHERE b.block_start >= ? AND b.block_start < ?
               ORDER BY b.block_start, b.meter_id""", (utc_start, utc_end)).fetchall()
        segments: dict = {}
        if rows:
            for bs, mid, k, inc, exc, band, attr in self._conn.execute(
                    "SELECT block_start, meter_id, kwh, inc_rate, exc_rate, band, attribution "
                    "FROM block_segments WHERE channel = 'import' "
                    "AND block_start >= ? AND block_start < ? "
                    "ORDER BY block_start, meter_id, seq", (utc_start, utc_end)):
                segments.setdefault((bs, mid), []).append((k, inc, exc, band, attr))
        return rows, segments

    def get_blocks_lightweight(self, utc_start: str | None = None,
                                utc_end: str | None = None,
                                finalised_only: bool = False) -> list[dict]:
        """
        Lightweight block fetch for chart generation — returns minimal dicts
        without full _rows_to_blocks reconstruction. Significantly faster than
        get_all_blocks() for large datasets.

        Returns dicts compatible with
        generate_daily_import_export_charts:
          - start, totals.import_kwh, totals.export_kwh
          - meters[meter_id].carbon_g, .imp_kwh, .exp_kwh, .imp_cost, .exp_cost
          - meters[meter_id].meta, .standing_charge, .channels

        Filters to the given UTC range if provided.
        """
        where = ""
        params: list = []
        if utc_start and utc_end:
            where = "WHERE b.block_start >= ? AND b.block_start < ?"
            params = [utc_start, utc_end]
        elif utc_start:
            where = "WHERE b.block_start >= ?"
            params = [utc_start]

        if finalised_only:
            where = (where + " AND b.is_provisional = 0") if where \
                    else "WHERE b.is_provisional = 0"

        rows = self._conn.execute(
            f"""SELECT b.block_start, b.meter_id,
                       b.imp_kwh, b.imp_kwh_grid,
                       b.imp_rate, b.imp_cost, b.imp_read_start, b.imp_read_end,
                       b.imp_cost_exc,
                       b.exp_kwh, b.exp_rate, b.exp_cost,
                       b.exp_read_start, b.exp_read_end,
                       b.standing_charge, b.carbon_g, b.carbon_intensity_g, b.interpolated,
                       b.rate_corrected,
                       m.is_sub_meter, m.parent_meter_id, m.device_label,
                       m.inverter_possible, m.meter_type,
                       cp.billing_day, cp.block_minutes, cp.timezone,
                       cp.currency_symbol, cp.currency_code, cp.effective_from
               FROM blocks b
               JOIN config_periods cp ON b.config_period_id = cp.id
               LEFT JOIN meters m ON m.meter_id = b.meter_id
                 AND m.config_period_id = b.config_period_id
               {where}
               ORDER BY b.block_start, b.meter_id""",
            params
        ).fetchall()

        # Group by block_start and reconstruct minimal block dicts
        from collections import defaultdict as _dd
        block_map: dict = {}
        block_order: list = []

        # Each row is unpacked by position (#48, 3 Oct 2026). An sqlite3.Row looks a
        # column up by NAME, and the plain dict that replaced it (C, 30 Sep) was still
        # built per row: 38 keys, ~52k rows on a year's bill. The column order is the
        # SELECT above. Same values, same output.
        for row in rows:
            (bs, mid, r_imp_kwh, r_imp_kwh_grid,
             r_imp_rate, r_imp_cost, r_imp_read_start, r_imp_read_end,
             r_imp_cost_exc,
             r_exp_kwh, r_exp_rate, r_exp_cost,
             r_exp_read_start, r_exp_read_end,
             r_standing_charge, r_carbon_g, r_carbon_intensity_g, r_interpolated,
             r_rate_corrected,
             r_is_sub_meter, r_parent_meter_id, r_device_label,
             r_inverter_possible, r_meter_type,
             r_billing_day, r_block_minutes, r_timezone,
             r_currency_symbol, r_currency_code, r_effective_from) = row
            if bs not in block_map:
                block_map[bs] = {
                    "start":   bs,
                    "end":     bs,  # approximate — not stored separately here
                    "meters":  {},
                    "totals":  {"import_kwh": 0.0, "export_kwh": 0.0,
                                "import_cost": 0.0, "export_cost": 0.0},
                    "_billing_day":    r_billing_day,
                    "_block_minutes":  r_block_minutes,
                    "_timezone":       r_timezone,
                    "_currency_symbol": r_currency_symbol,
                    "_currency_code":  r_currency_code,
                    "_effective_from": r_effective_from,
                }
                block_order.append(bs)

            b = block_map[bs]
            is_sub = bool(r_is_sub_meter)
            imp = float(r_imp_kwh or 0)
            exp = float(r_exp_kwh or 0)
            imp_cost = float(r_imp_cost or 0)
            exp_cost = float(r_exp_cost or 0)

            imp_channel = {
                "kwh":           imp,
                "cost":          imp_cost,
                "read_start":    r_imp_read_start,
                "read_end":      r_imp_read_end,
            }
            # Only include kwh_grid / kwh_remainder / rate when actually set in
            # the DB. _row_to_block omits these when NULL; get_blocks_lightweight
            # must do the same, otherwise a present-but-None key makes downstream
            # membership tests (`if "kwh_remainder" in channel`) read None as 0,
            # zeroing a real import on no-sub-meter setups (the "cost but no kWh"
            # billing bug). kwh_grid/kwh_remainder are only set by PASS 2 when
            # sub-meters exist; on a sub-less main meter they are legitimately NULL.
            if r_imp_kwh_grid is not None:
                imp_channel["kwh_grid"] = float(r_imp_kwh_grid)
            if r_imp_rate is not None:
                imp_channel["rate"] = float(r_imp_rate)
            # v4 4.5.21 forward fix: a manually corrected rate is the user's decision, so the day
            # chart's rate line must draw it (chart_emit.day_rate_series). Only when set, like the rest.
            if r_rate_corrected:
                imp_channel["rate_corrected"] = True
            # BL-23/BL-24: surface captured ex-VAT so the billing summary's bill method
            # and the data table's ex-VAT columns use the REAL figure (not inc÷1.05).
            # _row_to_block does this too; the lightweight chart fetch must match or the
            # whole ex-VAT view silently falls back to the approximation.
            # Step 5 (8 Oct 2026): the ex-VAT COST is the stored authority; the rate, the
            # standing charge's ex-VAT and exc_source are derived where read, not carried.
            if r_imp_cost_exc is not None:
                imp_channel["cost_exc"] = float(r_imp_cost_exc)
            # BL-9: the IOG house/EV split is read from the segments attached below (5c).

            exp_channel = {
                "kwh":        exp,
                "cost":       exp_cost,
                "read_start": r_exp_read_start,
                "read_end":   r_exp_read_end,
            }
            if r_exp_rate is not None:
                exp_channel["rate"] = float(r_exp_rate)

            m = b["meters"][mid] = {
                "meta": {
                    "sub_meter":        is_sub,
                    "parent_meter":     r_parent_meter_id,
                    "device":           r_device_label,
                    "inverter_possible": bool(r_inverter_possible),
                    "meter_type":       r_meter_type,
                    "block_minutes":    r_block_minutes,
                    "timezone":         r_timezone,
                    "currency_symbol":  r_currency_symbol,
                },
                "carbon_g":      r_carbon_g,
                "carbon_intensity_g": r_carbon_intensity_g,
                "imp_kwh":       imp,
                "exp_kwh":       exp,
                "imp_cost":      imp_cost,
                "exp_cost":      exp_cost,
                "standing_charge": float(r_standing_charge or 0),
                "interpolated":  bool(r_interpolated),
                "channels": {
                    "import": imp_channel,
                    "export": exp_channel,
                },
            }

            if not is_sub:
                t = b["totals"]
                t["import_kwh"]  += imp
                t["export_kwh"]  += exp
                t["import_cost"] += imp_cost
                t["export_cost"] += exp_cost

        # BL-27: attach the priced segments (ONE query for the range) to each block's import
        # channel, so readers can price from segments with a legacy-column fallback.
        if block_map:
            _sw, _sp = "WHERE channel = 'import'", []
            if utc_start and utc_end:
                _sw += " AND block_start >= ? AND block_start < ?"; _sp = [utc_start, utc_end]
            elif utc_start:
                _sw += " AND block_start >= ?"; _sp = [utc_start]
            for _sbs, _smid, _sk, _si, _sx, _sband, _sattr in self._conn.execute(
                "SELECT block_start, meter_id, kwh, inc_rate, exc_rate, band, attribution "
                "FROM block_segments " + _sw + " ORDER BY block_start, meter_id, seq", _sp):
                _b = block_map.get(_sbs)
                _m = _b["meters"].get(_smid) if _b else None
                _ch = (_m.get("channels") or {}).get("import") if _m else None
                if _ch is not None:
                    _ch.setdefault("segments", []).append(
                        {"kwh": _sk, "inc_rate": _si, "exc_rate": _sx, "band": _sband,
                         "attribution": _sattr})

        return [block_map[bs] for bs in block_order]

    def get_last_block_before(self, block_start: str) -> Optional[dict]:
        """Return the most recently finalised block before the given block_start.
        Used by engine_startup to avoid catching up zero-rate blocks written
        by ensure_correct_block before startup completes."""
        cur = self._conn.execute(
            """
            SELECT b.*, cp.billing_day, cp.block_minutes, cp.timezone, """ + _SPLIT_COLS + """
                   cp.currency_symbol, cp.currency_code, cp.effective_from,
                   m.is_sub_meter, m.parent_meter_id, m.device_label,
                   m.inverter_possible, m.power_sensor, m.postcode_prefix,
                   m.v2x_capable, m.meter_type, m.soc_sensor, m.inverter_power_sensor, m.inverter_power_invert, m.device_power_sensor
            FROM blocks b
            JOIN config_periods cp ON b.config_period_id = cp.id
            LEFT JOIN meters m ON m.meter_id = b.meter_id
                               AND m.config_period_id = b.config_period_id
            WHERE b.block_start = (
                SELECT MAX(block_start) FROM blocks WHERE block_start < ?
            )
            ORDER BY b.meter_id
            """,
            (block_start,)
        )
        rows = cur.fetchall()
        if not rows:
            return None
        return _row_to_block(rows)

    def get_last_block(self) -> Optional[dict]:
        """Return the most recently finalised block (by block_start)."""
        cur = self._conn.execute(
            """
            SELECT b.*, cp.billing_day, cp.block_minutes, cp.timezone, """ + _SPLIT_COLS + """
                   cp.currency_symbol, cp.currency_code, cp.effective_from,
                   m.is_sub_meter, m.parent_meter_id, m.device_label,
                   m.inverter_possible, m.power_sensor, m.postcode_prefix,
                   m.v2x_capable, m.meter_type, m.soc_sensor, m.inverter_power_sensor, m.inverter_power_invert, m.device_power_sensor
            FROM blocks b
            JOIN config_periods cp ON b.config_period_id = cp.id
            LEFT JOIN meters m ON m.meter_id = b.meter_id
                               AND m.config_period_id = b.config_period_id
            WHERE b.block_start = (SELECT MAX(block_start) FROM blocks)
            ORDER BY b.meter_id
            """
        )
        rows = cur.fetchall()
        if not rows:
            return None
        return _row_to_block(rows)

    def last_standing_charge(self, meter_id: str) -> Optional[float]:
        """The most recent non-zero standing charge recorded for `meter_id`.

        Three call sites in `engine.py` each wrote this SELECT against the raw
        connection, independently, because `BlockStore` had no standing-charge
        accessor at all: the finalise fallback for an unavailable SC sensor, and
        the two gap-fill paths (tick and startup) that seed
        `build_gap_blocks(last_standing_charge=...)`. Identical query, three
        copies, one of them parameterised by meter and two hardcoded to
        'electricity_main'.

        `standing_charge > 0` is part of the question, not an optimisation: a
        stored 0.0 means "not known for this block", so a zero row is not a
        newer answer and must not shadow an older real one. Returns None when
        nothing qualifies — the callers each have their own default, and
        conflating "no charge recorded" with 0.0 here would take that choice
        away from them.
        """
        row = self._conn.execute(
            "SELECT standing_charge FROM blocks "
            "WHERE meter_id = ? AND standing_charge > 0 "
            "ORDER BY block_start DESC LIMIT 1",
            (meter_id,)
        ).fetchone()
        return float(row["standing_charge"]) if row else None

    def get_blocks_for_range(self,
                             start: datetime,
                             end: datetime,
                             meter_id: Optional[str] = None,
                             finalised_only: bool = False) -> list[dict]:
        """Return blocks within [start, end], optionally filtered by meter."""
        start_iso = start.isoformat()
        end_iso   = end.isoformat()
        fin = self._finalised_clause(finalised_only)
        if meter_id:
            return self._select_blocks(
                "WHERE b.block_start >= ? AND b.block_start <= ? AND b.meter_id = ?" + fin,
                (start_iso, end_iso, meter_id)
            )
        return self._select_blocks(
            "WHERE b.block_start >= ? AND b.block_start <= ?" + fin,
            (start_iso, end_iso)
        )

    # ── UTC-based query methods ───────────────────────────────────────────────

    def dispatch_house_candidates(self, starts=None) -> list:
        """Block starts on a DISPATCH-ONLY account whose devices must fit inside the house: the
        main carries a dispatch EV (imp_kwh_ev > 0) and there is at least one device. Empty for an
        account with an EV meter (the meter is the EV) and for one with no dispatches, so for
        nearly every account this is one query that finds nothing. `starts` narrows it.
        v4 4.5.21 forward fix (engine._refit_devices_to_house)."""
        c = self._conn
        if c.execute("SELECT 1 FROM meters WHERE is_sub_meter = 1 "
                     "AND meter_type IN ('ev', 'ev_charger') LIMIT 1").fetchone():
            return []
        sql = ("SELECT m.block_start FROM blocks m WHERE m.meter_id = 'electricity_main' "
               "AND " + _evsplit.sql_kwh("m") + " > 0 "      # BL-79 (3): ev_split's split
               "AND EXISTS (SELECT 1 FROM blocks o "
               "    WHERE o.block_start = m.block_start AND o.meter_id IN "
               "          (SELECT meter_id FROM meters WHERE is_sub_meter = 1))")
        if starts is None:
            return [r[0] for r in c.execute(sql + " ORDER BY m.block_start")]
        want = sorted(set(starts))
        out = []
        for i in range(0, len(want), 500):
            chunk = want[i:i + 500]
            out += [r[0] for r in c.execute(
                sql + " AND m.block_start IN (%s)" % ",".join("?" * len(chunk)), chunk)]
        return out

    def write_resplit(self, device_updates) -> None:
        """Write a re-split back with targeted UPDATEs (never the round-trip): per device its grid
        share and the £ priced from it at the row's OWN stored rate. v4 4.5.21 forward fix.
        device_updates: (grid_kwh, grid_kwh, grid_kwh, block_start, meter_id).
        (The main's stored remainder went with its columns, step 5, 10 Oct 2026: the house is
        derived where read, max(0, main − Σ grid shares).)"""
        if not device_updates:
            return
        with self._tx():
            self._conn.executemany(
                "UPDATE blocks SET imp_kwh_grid = ?, "
                "  imp_cost = CASE WHEN imp_rate IS NOT NULL "
                "       THEN ROUND(? * imp_rate, 6) ELSE imp_cost END, "
                # The ex-VAT £ keeps the row's own exc/inc ratio (imp_cost_exc / imp_cost: the
                # rate column went in step 5b). SET reads the OLD row, so both are the stored
                # pair. A row with no £ to take the ratio from keeps its ex-VAT as it is: no
                # v5 surface reads a device's ex-VAT (import spec §5.2, v15).
                "  imp_cost_exc = CASE WHEN imp_cost_exc IS NOT NULL AND imp_rate IS NOT NULL "
                "       AND imp_cost IS NOT NULL AND imp_cost != 0 "
                "       THEN ROUND(? * imp_rate * imp_cost_exc / imp_cost, 6) "
                "       ELSE imp_cost_exc END "
                "WHERE block_start = ? AND meter_id = ?", device_updates)

    def get_block_dict_by_start(self, block_start: str) -> Optional[dict]:
        """Return the fully-reconstructed (joined) block dict for a single
        block_start — all meters, with meta/channels and the 3.0.0 columns
        (carbon_intensity_g, imp_kwh_api, is_provisional) surfaced.

        Used by the DCC PASS 2+3b re-run, which needs the same dict shape that
        _apply_pass2 / PASS 3b operate on in finalise_block. Returns None if no
        block exists at that start.
        """
        blocks = self._select_blocks(
            "WHERE b.block_start = ?", (block_start,)
        )
        return blocks[0] if blocks else None

    def get_blocks_for_utc_range(self, utc_start: str, utc_end: str,
                                  meter_id: str | None = None,
                                  finalised_only: bool = False) -> list[dict]:
        """
        Return all blocks where block_start >= utc_start AND block_start < utc_end.
        utc_start / utc_end are naive ISO strings (no tzinfo) matching DB format.
        Replaces get_blocks_for_local_date_range — use local_date_range_to_utc_bounds()
        to compute the UTC bounds from local date strings.
        """
        fin = self._finalised_clause(finalised_only)
        if meter_id:
            return self._select_blocks(
                "WHERE b.block_start >= ? AND b.block_start < ? AND b.meter_id = ?" + fin,
                (utc_start, utc_end, meter_id)
            )
        return self._select_blocks(
            "WHERE b.block_start >= ? AND b.block_start < ?" + fin,
            (utc_start, utc_end)
        )

    def last_block_end(self, meter_id: str) -> Optional[str]:
        """The latest `block_end` recorded for `meter_id`, or None.

        Startup uses it to spot a sub-meter whose last block is old enough that
        resuming from its stored pre-gap read would reconcile an entire
        retirement absence into one block.
        """
        row = self._conn.execute(
            "SELECT MAX(block_end) AS last_end FROM blocks WHERE meter_id = ?",
            (meter_id,)).fetchone()
        return row["last_end"] if row and row["last_end"] else None

    def has_interpolated_block(self, block_start: str,
                               meter_id: str = "electricity_main") -> bool:
        """True when an INTERPOLATED block already exists at `block_start`.

        The gap filler asks before writing, so two startups in quick succession
        (consecutive HA disconnects) cannot have the second overwrite the first
        with slightly different interpolated values. Regression 2.10.9.

        Deliberately a bool: the caller had a row it only ever tested for
        truthiness, and returning the row invites someone to start reading
        columns off a question that was only ever "does this exist".
        """
        row = self._conn.execute(
            "SELECT 1 FROM blocks WHERE meter_id = ? AND block_start = ? "
            "AND interpolated = 1 LIMIT 1", (meter_id, block_start)).fetchone()
        return row is not None

    def get_generation_mix_since(self, hours: int = 4,
                                 meter_id: str = "electricity_main") -> dict:
        """Per-block generation mix for the last `hours` hours, as
        `{block_start: [{fuel, perc}, ...]}`.

        Distinct from `get_generation_mix_for_range`, which returns ONE
        kWh-weighted average across a range. This one keeps the blocks apart,
        because its caller re-seeds an in-memory per-slot cache.

        THE CUTOFF IS COMPUTED IN PYTHON, and that is a fix rather than a
        preference. The query this replaced compared against
        `datetime('now', '-4 hours')`, which SQLite renders as
        `'2026-09-23 17:44:05'` -- a SPACE between date and time, where
        `block_start` is stored ISO-style with a 'T'. String comparison then puts
        every same-date row above the cutoff, because 'T' (84) > ' ' (32):

            '2026-09-23T00:30:00' >= '2026-09-23 17:44:05'   ->  True

        So the "-4 hours" window was really "everything from today" -- nearly
        empty just after midnight and a whole day by late evening. Matching the
        stored format makes it the window it always claimed to be.
        """
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
                  - timedelta(hours=hours)).isoformat()
        rows = self._conn.execute(
            """SELECT b.block_start, gm.fuel, gm.perc
               FROM blocks b
               JOIN generation_mix gm ON gm.block_id = b.id
               WHERE b.meter_id = ? AND b.block_start >= ?
               ORDER BY b.block_start ASC, gm.fuel ASC""",
            (meter_id, cutoff)).fetchall()
        slots: dict = {}
        for r in rows:
            slots.setdefault(r["block_start"], []).append(
                {"fuel": r["fuel"], "perc": r["perc"]})
        return slots

    def count_mix_history(self) -> int:
        """Rows in mix_history. Startup backfills from generation_mix when 0."""
        return self._conn.execute(
            "SELECT COUNT(*) FROM mix_history").fetchone()[0]

    # A block is SETTLEABLE when it carries an authoritative kWh figure: imported
    # history brings its own, and a DCC-settled block has imp_kwh_api. A live CAD
    # block has neither and must never be stamped 'settled'. Written once here
    # and used by three queries, which previously spliced the same string into
    # their SQL by concatenation (twice) and an f-string (once).
    _SETTLEABLE_SQL = "(b.source LIKE 'imported%' OR b.imp_kwh_api IS NOT NULL)"

    def get_block_measured_inputs(self, block_start: str,
                                  meter_id: str = "electricity_main"):
        """The inputs `apply_measured_to_block` needs about the block itself, or
        None if there is no such block. `source` and `imp_kwh_api` are here for
        the settleable check the applier repeats as defence in depth."""
        # BL-79 (3): `imp_kwh_ev` here is ev_split's split (the EV segments; the column only for
        # a block with no segments) — the stored EV the applier prefers, and the heal's "healed".
        row = self._conn.execute(
            "SELECT b.imp_kwh, " + _evsplit.sql_kwh("b") + " AS imp_kwh_ev, b.imp_rate, "
            "b.source, b.imp_kwh_api FROM blocks b "
            "WHERE b.block_start = ? AND b.meter_id = ?",
            (block_start, meter_id)).fetchone()
        return dict(row) if row else None

    def completed_dispatch_kwh(self, slot_start: str) -> float:
        """Octopus's own completed-dispatch energy for a slot, as a positive
        total. 0.0 when there is none.

        ABS because dispatch energy is recorded negative (energy drawn). This is
        the EV CEILING: the bill sets the EV rate and band, but never a quantity
        beyond the car's actual session.
        """
        row = self._conn.execute(
            "SELECT SUM(ABS(energy_kwh)) FROM dispatch_history "
            "WHERE kind = 'completed' AND slot_start = ? AND energy_kwh IS NOT NULL",
            (slot_start,)).fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    def latest_measured_kwh(self, slot_start: str):
        """The freshest billed kWh for a slot, or None.

        `ORDER BY fetched_at DESC LIMIT 1` is load-bearing: measured_cost can hold
        more than one row per slot, and the band is decided by cost/kWh, so an
        older denominator moves the answer.
        """
        row = self._conn.execute(
            "SELECT kwh FROM measured_cost WHERE slot_start = ? "
            "AND direction = 'CONSUMPTION' ORDER BY fetched_at DESC LIMIT 1",
            (slot_start,)).fetchone()
        if row and row["kwh"] is not None and float(row["kwh"]) > 1e-9:
            return float(row["kwh"])
        return None

    # Off IOG-SMB the drain asks for live history's bill, and for an API-imported half-hour only
    # where the import got no cost from Octopus (`import_no_cost`): the rest already hold Octopus's
    # billed cost (20 Oct 2026, the user: on every Intelligent tariff only Octopus's cost is final).
    _ASKED_OFF_SMB_SQL = (
        "(b.source IS NULL OR b.source NOT LIKE 'imported%' OR b.block_start IN "
        "(SELECT j.key FROM kraken_state k, json_each(k.value) j WHERE k.key = 'import_no_cost'))")

    def get_settleable_blocks(self, floor: str, after: str, before: str,
                              limit: int,
                              meter_id: str = "electricity_main", *,
                              include_imported: bool = True) -> list:
        """One page of settleable blocks with no cached bill yet, oldest first.

        THREE bounds, not two, and they are not redundant: `floor` is fixed for
        the run, `after` is a cursor that advances past slots Octopus has no
        answer for WITHIN a run, and `before` is the not-yet-billed cutoff.
        Collapsing `floor` and `after` loses the paging and the drain re-reads the
        same page forever.

        `include_imported=False`: off IOG-SMB — live history, and the API-imported half-hours
        Octopus gave the import no cost for (`_ASKED_OFF_SMB_SQL`); the other imported ones
        already hold Octopus's billed cost (18 Oct 2026; the no-cost ones, 20 Oct 2026).
        """
        rows = self._conn.execute(
            "SELECT b.block_start FROM blocks b WHERE b.meter_id = ? "
            "  AND b.imp_kwh > 0 "
            "  AND " + self._SETTLEABLE_SQL + " "
            + ("" if include_imported else "  AND " + self._ASKED_OFF_SMB_SQL + " ") +
            "  AND b.block_start >= ? AND b.block_start > ? AND b.block_start < ? "
            "  AND (b.rate_source IS NULL OR b.rate_source NOT IN ('settled','corrected')) "
            "  AND NOT EXISTS (SELECT 1 FROM measured_cost mc WHERE mc.slot_start = b.block_start) "
            "ORDER BY b.block_start ASC LIMIT ?",
            (meter_id, floor, after, before, limit)).fetchall()
        return [r["block_start"] for r in rows]

    # A cost-only cached bill on a half-hour the car drew in — teal on the Import bill, the split
    # still EMT's (18 Oct 2026, the user: the recovery asks their breakdown, reversing bill and
    # split paths design §7.2). Main-meter, uncorrected, with a completed dispatch.
    _COST_ONLY_SPLIT_SQL = (
        "FROM measured_cost m JOIN blocks b ON b.block_start = m.slot_start "
        "  AND b.meter_id = 'electricity_main' "
        "WHERE m.direction = 'CONSUMPTION' AND m.ev_kwh IS NULL AND m.home_kwh IS NULL "
        "  AND m.cost_incl IS NOT NULL AND m.cost_incl > 0 AND b.imp_kwh > 0 "
        "  AND COALESCE(b.rate_corrected, 0) = 0 "
        "  AND (b.rate_source IS NULL OR b.rate_source != 'corrected') "
        "  AND m.fetched_at = (SELECT MAX(m2.fetched_at) FROM measured_cost m2 "
        "                      WHERE m2.slot_start = m.slot_start AND m2.direction = m.direction) "
        "  AND EXISTS (SELECT 1 FROM dispatch_history d WHERE d.slot_start = m.slot_start "
        "              AND d.kind = 'completed' AND d.energy_kwh IS NOT NULL) "
        "  AND m.slot_start >= ? AND m.slot_start < ? ")

    def get_cost_only_split_slots(self, floor: str, after: str, before: str, limit: int) -> list:
        """One page, oldest first, of the half-hours whose latest cached bill is cost-only though
        the car drew in them (`_COST_ONLY_SPLIT_SQL`) — the history drain asks their breakdown."""
        rows = self._conn.execute(
            "SELECT DISTINCT m.slot_start " + self._COST_ONLY_SPLIT_SQL +
            "  AND m.slot_start > ? ORDER BY m.slot_start ASC LIMIT ?",
            (floor, before, after, limit)).fetchall()
        return [r[0] for r in rows]

    def count_cost_only_split_slots(self, floor: str, before: str, exclude=()) -> int:
        """How many `get_cost_only_split_slots` still has (the drain's backlog), less `exclude`."""
        exclude = sorted(set(exclude or ()))
        return int(self._conn.execute(
            "SELECT COUNT(DISTINCT m.slot_start) " + self._COST_ONLY_SPLIT_SQL
            + (" AND m.slot_start NOT IN (%s)" % ",".join("?" * len(exclude)) if exclude else ""),
            (floor, before, *exclude)).fetchone()[0])

    def count_settleable_blocks(self, floor: str, before: str,
                                meter_id: str = "electricity_main", exclude=(), *,
                                include_imported: bool = True) -> int:
        """How many blocks `get_settleable_blocks` still has to work through.

        Same predicate, no cursor and no limit -- the backlog gate that decides
        whether the drain is scheduled at all. `exclude`: block starts that are
        not backlog however the predicate reads them (a bill held back, v4 4.5.23).
        """
        exclude = sorted(set(exclude or ()))
        return self._conn.execute(
            "SELECT COUNT(*) FROM blocks b WHERE b.meter_id = ? "
            "  AND b.imp_kwh > 0 "
            "  AND " + self._SETTLEABLE_SQL + " "
            + ("" if include_imported else "  AND " + self._ASKED_OFF_SMB_SQL + " ") +
            "  AND b.block_start >= ? AND b.block_start < ? "
            "  AND (b.rate_source IS NULL OR b.rate_source NOT IN ('settled','corrected')) "
            "  AND NOT EXISTS (SELECT 1 FROM measured_cost mc WHERE mc.slot_start = b.block_start)"
            + (" AND b.block_start NOT IN (%s)" % ",".join("?" * len(exclude)) if exclude else ""),
            (meter_id, floor, before, *exclude)).fetchone()[0]

    def get_imp_kwh(self, starts, meter_id: str = "electricity_main") -> dict:
        """{block_start: imp_kwh} for the blocks in `starts` (absent where there is no block) —
        what EMT holds against a bill's answer (bill and split paths design §6 (a))."""
        starts = [s for s in dict.fromkeys(starts or []) if s]
        out = {}
        for i in range(0, len(starts), 500):
            chunk = starts[i:i + 500]
            for r in self._conn.execute(
                    "SELECT block_start, imp_kwh FROM blocks WHERE meter_id = ? "
                    "AND block_start IN (%s)" % ",".join("?" * len(chunk)), [meter_id] + chunk):
                out[r[0]] = r[1]
        return out

    def get_bill_ev_kwh(self, starts, direction: str = "CONSUMPTION") -> dict:
        """{slot_start: EV kWh} of the latest cached bill for each slot in `starts` that states
        one (any mpan) — absent where no bill, or no breakdown, is cached."""
        starts = [s for s in dict.fromkeys(starts or []) if s]
        out = {}
        for i in range(0, len(starts), 500):
            chunk = starts[i:i + 500]
            for r in self._conn.execute(
                    "SELECT slot_start, ev_kwh FROM measured_cost m WHERE direction = ? "
                    "AND slot_start IN (%s) AND ev_kwh IS NOT NULL AND fetched_at = "
                    "(SELECT MAX(fetched_at) FROM measured_cost m2 WHERE m2.slot_start = "
                    "m.slot_start AND m2.direction = m.direction)" % ",".join("?" * len(chunk)),
                    [direction] + chunk):
                out[r[0]] = r[1]
        return out

    def get_ev_split_kwh(self, starts, meter_id: str = "electricity_main") -> dict:
        """{block_start: EV kWh} of the blocks in `starts` that have an EV split — their
        attribution='ev' import segments, which ARE the split (ev_split.py). A block with no
        EV segment is absent."""
        starts = [s for s in dict.fromkeys(starts or []) if s]
        out = {}
        for i in range(0, len(starts), 500):
            chunk = starts[i:i + 500]
            for r in self._conn.execute(
                    "SELECT block_start, SUM(kwh) FROM block_segments WHERE meter_id = ? "
                    "AND channel = 'import' AND attribution = 'ev' AND block_start IN (%s) "
                    "GROUP BY block_start" % ",".join("?" * len(chunk)), [meter_id] + chunk):
                out[r[0]] = r[1]
        return out

    def get_measured_costs_to_apply(self, mpan: str, floor: str,
                                    meter_id: str = "electricity_main") -> list:
        """Settleable blocks with a cached bill that has not been applied yet.

        Scoped to the MPAN so a move to a new meter cannot apply the old meter's
        bill, and to `direction = 'CONSUMPTION'` because the same table holds
        export rows.
        """
        rows = self._conn.execute(
            "SELECT b.block_start, b.imp_cost, m.cost_incl, m.cost_excl, m.label "
            "FROM blocks b "
            "JOIN measured_cost m ON m.slot_start = b.block_start "
            "     AND m.mpan = ? AND m.direction = 'CONSUMPTION' "
            "WHERE b.meter_id = ? AND b.rate_corrected = 0 "
            "  AND (b.rate_source IS NULL OR b.rate_source NOT IN ('settled','corrected')) "
            "  AND " + self._SETTLEABLE_SQL + " "
            "  AND b.block_start >= ? AND m.cost_incl IS NOT NULL AND m.cost_incl > 0 "
            "ORDER BY b.block_start",
            (mpan, meter_id, floor)).fetchall()
        return [dict(r) for r in rows]

    def get_dispatched_blocks_awaiting_measure(self, floor: str,
                                               meter_id: str = "electricity_main") -> list:
        """DCC-settled dispatched blocks with no bill fetched yet, NEWEST first.

        Newest-first on purpose: this is the live pass, and the most recent
        settlement is the one a user is looking at. The bulk backfill of old
        history is `get_settleable_blocks`, which goes oldest-first.

        `imp_kwh_api IS NOT NULL` rather than the shared settleable predicate --
        imported history is deliberately excluded here and drained separately.
        The dispatch test accepts `off_peak` OR a `smart-charge-completed`
        source, because a slot can be dispatched without having been planned
        off-peak.
        """
        rows = self._conn.execute(
            "SELECT b.block_start, b.imp_kwh, b.imp_rate, b.imp_cost "
            "FROM blocks b "
            "WHERE b.meter_id = ? AND b.imp_kwh_api IS NOT NULL "
            "  AND b.rate_corrected = 0 "
            "  AND (b.rate_source IS NULL OR b.rate_source NOT IN ('settled','corrected')) "
            "  AND (b.source IS NULL OR b.source NOT LIKE 'imported%') "
            "  AND b.block_start >= ? "
            "  AND EXISTS (SELECT 1 FROM dispatch_slots s "
            "              WHERE s.slot_start = b.block_start "
            "                AND (s.off_peak = 1 OR s.source = 'smart-charge-completed')) "
            "ORDER BY b.block_start DESC",
            (meter_id, floor)).fetchall()
        return [dict(r) for r in rows]

    def site_timezone(self) -> str:
        """The site's IANA timezone from the newest config period; 'UTC' if there
        is none. Used for the IOG noon-to-noon cap day, where the boundary is a
        LOCAL clock time and so moves with British Summer Time."""
        row = self._conn.execute(
            "SELECT timezone FROM config_periods "
            "ORDER BY effective_from DESC LIMIT 1").fetchone()
        return (row["timezone"] or "UTC") if row else "UTC"

    _EPOCH_KEY = "data_epoch"

    def get_data_epoch(self) -> int:
        """The bulk-change epoch (C §2a). Bumped by operations that legitimately
        touch all history — an import, a delete, a one-time heal — so a client
        can be told "throw everything away" once instead of through thousands of
        changed day fingerprints."""
        try:
            v = self.get_meta(self._EPOCH_KEY)
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    def bump_data_epoch(self, floor: int = 0) -> int:
        """Invalidate every client's cached state. Returns the new epoch.

        `floor` is for a restore or upload, where the epoch lives IN the file being
        swapped in: a restored database brings back whatever epoch it had, which a
        client may already hold — equal, or lower than what it saw since. Passing
        the epoch read BEFORE the swap makes the result max(restored, previous) + 1,
        so it can never repeat or go backwards."""
        n = max(self.get_data_epoch(), int(floor or 0)) + 1
        self.set_meta(self._EPOCH_KEY, str(n))
        return n

    def get_energy_blocks(self, utc_start: str, utc_end: str) -> list:
        """Priced blocks over [utc_start, utc_end) — every meter, with provenance.

        SUB-METER ROWS ARE FIRST-CLASS. They carry device energy and have **no
        segments at all** (measured: 6,845 ev_charger and 7,792 house_battery
        blocks, zero segments between them), so a payload that returns only the
        main meter plus its split has no device data in it, silently.

        PROVENANCE IS THREE FACTS, not one `settled` flag. They are orthogonal
        and they disagree — on the reference database 48,050 imported blocks are
        settleable with no `imp_kwh_api`, and 29 hold a cached bill that has not
        been applied:

          `kwh_authoritative`  imported history, or a DCC figure has arrived
          `bill_cached`        Octopus's billed cost for the slot is held
          `bill_applied`       that bill has been written onto the block

        AND A DEVICE INHERITS ITS PARENT'S. A sub-meter's own `rate_source` says
        how its ROW was written (PASS 2, at the parent's rate), not where its
        NUMBER came from: 1,206 device blocks under a settled parent carry
        `rate_source='schedule'`, because `recost_sub_meters` copies the parent's
        rate without touching the child's provenance. `bill_applied` therefore
        comes from the parent row. (An earlier version cited the rate matching
        the parent "1,206 of 1,206, £42.14" as evidence; C §3 withdrew that —
        devices track the parent's rate whatever its state, so the match
        distinguishes nothing. The lifecycle argument is what holds.)
        """
        rows = self._conn.execute(
            "SELECT b.block_start, b.block_end, b.meter_id, "
            "       COALESCE(m.is_sub_meter, 0) AS is_sub_meter, "
            "       b.imp_kwh, b.imp_kwh_grid, b.imp_cost, b.imp_rate, "
            "       b.exp_kwh, b.exp_cost, b.exp_rate, "
            "       b.carbon_g, b.carbon_intensity_g, b.standing_charge, "
            "       b.interpolated, b.source, b.rate_source, "
            "       (b.source LIKE 'imported%' OR b.imp_kwh_api IS NOT NULL) AS kwh_authoritative, "
            # the export channel's own: export settles separately (and later) — the
            # Heatmaps tab's API-coverage view splits by channel.
            "       (b.source LIKE 'imported%' OR b.exp_kwh_api IS NOT NULL) AS exp_kwh_authoritative, "
            "       EXISTS(SELECT 1 FROM measured_cost mc "
            "              WHERE mc.slot_start = b.block_start "
            "                AND mc.direction = 'CONSUMPTION') AS bill_cached, "
            "       (COALESCE(p.rate_source, b.rate_source) = 'settled') AS bill_applied "
            "FROM blocks b "
            "LEFT JOIN meters m ON m.meter_id = b.meter_id "
            "                  AND m.config_period_id = b.config_period_id "
            # the MAIN row for the same slot — a device's bill provenance is its
            # parent's, and `p` is NULL for the main row itself, hence COALESCE
            "LEFT JOIN blocks p ON p.block_start = b.block_start "
            "                  AND p.meter_id = 'electricity_main' "
            "                  AND b.meter_id != 'electricity_main' "
            "WHERE b.block_start >= ? AND b.block_start < ? "
            "ORDER BY b.block_start, b.meter_id", (utc_start, utc_end)).fetchall()
        # `carbon_intensity_g` in the payload is the GRID's intensity for the slot, by
        # carbon.slot_intensity — the one rule (stored value, else carbon_g/net for a
        # pre-3.0.0 block, else unknown; never negative: an export slot stores a
        # negative intensity). v4 applied it inside the heatmap renderer; applied here,
        # every surface gets the same number (tests/test_heatmap_intensity.py).
        import carbon as _carbon
        out = []
        for r in rows:
            d = dict(r)
            d["carbon_intensity_g"] = _carbon.slot_intensity(
                d.get("carbon_g"), d.get("carbon_intensity_g"),
                (d.get("imp_kwh") or 0.0) - (d.get("exp_kwh") or 0.0))
            out.append(d)
        return out

    def get_energy_segments(self, utc_start: str, utc_end: str) -> list:
        """Priced segments over [utc_start, utc_end), ordered within each block.

        Main-meter import only in practice — the split machinery does not run on
        sub-meters. `band` and `attribution` together are the 2x2 cell, which is
        why a client never has to infer a band from a rate (C §3).
        """
        rows = self._conn.execute(
            "SELECT block_start, meter_id, channel, seq, kwh, inc_rate, exc_rate, "
            "       band, attribution FROM block_segments "
            "WHERE block_start >= ? AND block_start < ? "
            "ORDER BY block_start, meter_id, channel, seq",
            (utc_start, utc_end)).fetchall()
        return [dict(r) for r in rows]

    def get_day_rollups(self, utc_start: str, utc_end: str, tz_name: str) -> list:
        """One row per LOCAL day per meter over [utc_start, utc_end): the cheap
        resolution (93 rows and 1.7 KB gzipped for a month, against 55 KB at block
        resolution).

        LOCAL, not UTC — corrected 30 Sep 2026. This grouped by UTC date and left
        "the local-day question" to the caller, but a UTC day's total cannot be
        split back into local days: from late March to late October a UK local
        day runs 23:00–23:00 UTC, so no client could recover correct daily totals
        for the half of the year that is BST. `tz_name` is required, with no
        default (C §2 rule 2): a day is a tariff-context question.

        Each row carries its day's UTC bounds, and meters stay separate so a client
        can apply the bill's per-day `max(0, main − Σsubs) + Σsubs`. Still one
        query over `blocks` — never a block fetch reduced in Python (C §3a)."""
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
        lo = datetime.fromisoformat(utc_start).replace(tzinfo=timezone.utc)
        hi = datetime.fromisoformat(utc_end).replace(tzinfo=timezone.utc)
        if hi <= lo:
            return []
        fmt = "%Y-%m-%dT%H:%M:%S"
        days, d = [], lo.astimezone(tz).date()
        while True:
            start = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc)
            if start >= hi:
                break
            nd = d + timedelta(days=1)
            end = datetime(nd.year, nd.month, nd.day, tzinfo=tz).astimezone(timezone.utc)
            s0, e0 = max(start, lo), min(end, hi)
            if e0 > s0:
                days.append((d.isoformat(), s0.strftime(fmt), e0.strftime(fmt)))
            d = nd
        if not days:
            return []
        values = ",".join("(?,?,?)" for _ in days)
        params = [x for day in days for x in day]
        rows = self._conn.execute(
            "WITH d(day, s, e) AS (VALUES " + values + ") "
            "SELECT d.day AS day, d.s AS start_utc, d.e AS end_utc, b.meter_id, "
            "       COUNT(*) AS blocks, "
            "       ROUND(COALESCE(SUM(b.imp_kwh), 0), 4) AS imp_kwh, "
            "       ROUND(COALESCE(SUM(b.imp_cost), 0), 4) AS imp_cost, "
            "       ROUND(COALESCE(SUM(b.exp_kwh), 0), 4) AS exp_kwh, "
            "       ROUND(COALESCE(SUM(b.exp_cost), 0), 4) AS exp_cost, "
            "       ROUND(COALESCE(SUM(b.carbon_g), 0), 1) AS carbon_g, "
            # How many of the day's blocks carry carbon at all: SUM(carbon_g) is 0
            # both for "net zero" and for "no intensity data" (C step 2, 1 Oct 2026).
            "       COUNT(b.carbon_g) AS carbon_blocks, "
            # The day's standing charge is ONE value, not a sum: every block carries
            # the full daily charge (measured: 48 of 48 blocks on every day of a
            # real 4.5.16 database), so SUM reported it 48x. The rule is v4's
            # compute_period_net: the EARLIEST block with a non-zero charge — the
            # start-of-day value (not MAX, which over-bills on a decrease).
            "       ROUND(COALESCE((SELECT b2.standing_charge FROM blocks b2 "
            "                       WHERE b2.meter_id = b.meter_id "
            "                         AND b2.block_start >= d.s AND b2.block_start < d.e "
            "                         AND b2.standing_charge > 0 "
            "                       ORDER BY b2.block_start LIMIT 1), 0), 4) AS standing_charge, "
            "       SUM(b.interpolated) AS interpolated_blocks "
            "FROM d JOIN blocks b ON b.block_start >= d.s AND b.block_start < d.e "
            "GROUP BY d.day, b.meter_id ORDER BY d.day, b.meter_id",
            params).fetchall()
        return [dict(r) for r in rows]

    def history_token(self, before_utc: str) -> str:
        """One token over every block (and segment) starting before `before_utc` — the same
        columns as `day_fingerprints`, as a single aggregate, so a client keeping rows by day
        can ask "has anything older than my recent window changed?" in one cheap answer
        instead of fetching a fingerprint per day for all of history (C, 2 Oct 2026)."""
        r = self._conn.execute(
            "SELECT COUNT(*) AS n, MAX(block_start) AS newest, "
            "       ROUND(COALESCE(SUM(imp_kwh), 0), 3) AS ik, ROUND(COALESCE(SUM(imp_cost), 0), 2) AS ic, "
            "       ROUND(COALESCE(SUM(exp_kwh), 0), 3) AS ek, ROUND(COALESCE(SUM(exp_cost), 0), 2) AS ec, "
            "       ROUND(COALESCE(SUM(carbon_g), 0), 0) AS cg, "
            "       ROUND(COALESCE(SUM(imp_cost_exc), 0), 4) AS xx, ROUND(COALESCE(SUM(imp_kwh_grid), 0), 4) AS xg, "
            "       ROUND(COALESCE(SUM(standing_charge), 0), 4) AS xs "
            # NOT INDEXED: the window is nearly every row, and walking the block_start index
            # with a row lookup each is far slower than one scan (0.28 s vs ~0.05 s, 138k).
            "FROM blocks NOT INDEXED WHERE block_start < ?", (before_utc,)).fetchone()
        g = self._conn.execute(
            "SELECT COUNT(*) AS n, ROUND(COALESCE(SUM(inc_rate), 0), 4) AS ir, "
            "       ROUND(COALESCE(SUM(exc_rate), 0), 4) AS er, "
            "       ROUND(COALESCE(SUM(CASE WHEN attribution = 'ev' THEN kwh END), 0), 4) AS ea, "
            "       ROUND(COALESCE(SUM(kwh * (unicode(band) + 31 * length(band))), 0), 3) AS pk "
            "FROM block_segments NOT INDEXED WHERE block_start < ?", (before_utc,)).fetchone()
        return ":".join(str(r[k] or "") for k in r.keys()) + "|" + ":".join(str(g[k]) for k in g.keys())

    def get_day_live_rollups(self, utc_start: str, utc_end: str, tz_name: str,
                             ev_meter_id) -> dict:
        """Live Power's card rows, per LOCAL day over [utc_start, utc_end) — summed over
        the days of a window, they are `/api/billing`'s window figures (C, 1 Oct 2026).

        `meters`: {day: {meter_id: {is_sub, grid_kwh, imp_cost, exp_kwh, exp_cost,
        standing}}} — the device flag from the ACTIVE config period, kWh grid-attributed
        (`COALESCE(imp_kwh_grid, imp_kwh)`), the standing charge the day's earliest
        non-zero one; meters with no row in the active period are left out, as the card
        always left them. `ev`: {day: {kwh, cost}} — the hybrid EV (#468): the main
        meter's EV split (ev_split: its EV segments) wherever it has one, else the physical EV
        meter's grid kWh (`ev_meter_id`, None for an account without one). `direct`:
        {day: {kwh, cost}} — the Direct import, direct_import's rule on each half-hour over the
        card's devices (every other sub-meter, and the hybrid EV in the EV meter's place).
        Unrounded, so a window's sum is the card's to the last digit. Two queries over the
        span — the card ran six per window, three windows, with per-block Python."""
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
        lo = datetime.fromisoformat(utc_start).replace(tzinfo=timezone.utc)
        hi = datetime.fromisoformat(utc_end).replace(tzinfo=timezone.utc)
        if hi <= lo:
            return {"meters": {}, "ev": {}, "direct": {}}
        fmt = "%Y-%m-%dT%H:%M:%S"
        days, d = [], lo.astimezone(tz).date()
        while True:
            start = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc)
            if start >= hi:
                break
            nd = d + timedelta(days=1)
            end = datetime(nd.year, nd.month, nd.day, tzinfo=tz).astimezone(timezone.utc)
            s0, e0 = max(start, lo), min(end, hi)
            if e0 > s0:
                days.append((d.isoformat(), s0.strftime(fmt), e0.strftime(fmt)))
            d = nd
        if not days:
            return {"meters": {}, "ev": {}, "direct": {}}
        cte = "WITH d(day, s, e) AS (VALUES " + ",".join("(?,?,?)" for _ in days) + ") "
        params = [x for day in days for x in day]
        active = ("SELECT id FROM config_periods "
                  "WHERE effective_to IS NULL ORDER BY effective_from DESC LIMIT 1")
        meters = {}
        for r in self._conn.execute(
                cte +
                "SELECT d.day AS day, b.meter_id, m.is_sub_meter AS is_sub, "
                "       COALESCE(SUM(COALESCE(b.imp_kwh_grid, b.imp_kwh)), 0) AS grid_kwh, "
                "       COALESCE(SUM(b.imp_cost), 0) AS imp_cost, "
                "       COALESCE(SUM(b.exp_kwh), 0) AS exp_kwh, "
                "       COALESCE(SUM(b.exp_cost), 0) AS exp_cost, "
                "       COALESCE((SELECT b2.standing_charge FROM blocks b2 "
                "                 WHERE b2.meter_id = b.meter_id "
                "                   AND b2.block_start >= d.s AND b2.block_start < d.e "
                "                   AND b2.standing_charge > 0 "
                "                 ORDER BY b2.block_start LIMIT 1), 0) AS standing "
                "FROM d JOIN blocks b ON b.block_start >= d.s AND b.block_start < d.e "
                "JOIN meters m ON m.meter_id = b.meter_id AND m.config_period_id = (" + active + ") "
                "GROUP BY d.day, b.meter_id", params).fetchall():
            meters.setdefault(r["day"], {})[r["meter_id"]] = {
                "is_sub": bool(r["is_sub"]), "grid_kwh": float(r["grid_kwh"]),
                "imp_cost": float(r["imp_cost"]), "exp_kwh": float(r["exp_kwh"]),
                "exp_cost": float(r["exp_cost"]), "standing": float(r["standing"])}
        # The hybrid EV, and the Direct import by direct_import's per-half-hour rule over the
        # card's devices: every other sub-meter, plus the hybrid EV in the EV meter's place.
        ev, direct = {}, {}
        # The split is ev_split's: the main's EV segments (step 5, 8 Oct 2026), joined once.
        _sk, _sc = _evsplit.joined_kwh("m"), _evsplit.joined_cost("m")
        ev_k = ("CASE WHEN " + _sk + " IS NOT NULL THEN " + _sk + " "
                "     ELSE COALESCE(s.imp_kwh_grid, s.imp_kwh, 0) END")
        ev_c = ("CASE WHEN " + _sk + " IS NOT NULL THEN COALESCE(" + _sc + ", 0) "
                "     ELSE COALESCE(s.imp_cost, 0) END")
        dev_k, dev_c = "COALESCE(o.k, 0) + " + ev_k, "COALESCE(o.c, 0) + " + ev_c
        for r in self._conn.execute(
                cte +
                "SELECT d.day AS day, "
                "       COALESCE(SUM(" + ev_k + "), 0) AS kwh, "
                "       COALESCE(SUM(" + ev_c + "), 0) AS cost, "
                "       COALESCE(SUM(" + _direct.sql_kwh(
                    "COALESCE(m.imp_kwh_grid, m.imp_kwh, 0)", dev_k) + "), 0) AS direct_kwh, "
                "       COALESCE(SUM(" + _direct.sql_cost(
                    "COALESCE(m.imp_cost, 0)", dev_c) + "), 0) AS direct_cost "
                "FROM d JOIN blocks m ON m.block_start >= d.s AND m.block_start < d.e "
                "                    AND m.meter_id = 'electricity_main' "
                "LEFT JOIN blocks s ON s.block_start = m.block_start AND s.meter_id = ? "
                + _evsplit.sql_join() + " ON _g.block_start = m.block_start AND _g.meter_id = m.meter_id "
                "LEFT JOIN (SELECT b.block_start, "
                "                  SUM(COALESCE(b.imp_kwh_grid, b.imp_kwh, 0)) AS k, "
                "                  SUM(COALESCE(b.imp_cost, 0)) AS c "
                "           FROM blocks b JOIN meters sm ON sm.meter_id = b.meter_id "
                "                AND sm.config_period_id = (" + active + ") AND sm.is_sub_meter = 1 "
                "           WHERE b.block_start >= ? AND b.block_start < ? "
                "             AND (? IS NULL OR b.meter_id != ?) "
                "           GROUP BY b.block_start) o ON o.block_start = m.block_start "
                "GROUP BY d.day",
                params + [ev_meter_id, days[0][1], days[-1][2],
                          days[0][1], days[-1][2], ev_meter_id, ev_meter_id]).fetchall():
            ev[r["day"]] = {"kwh": float(r["kwh"]), "cost": float(r["cost"])}
            direct[r["day"]] = {"kwh": float(r["direct_kwh"]), "cost": float(r["direct_cost"])}
        return {"meters": meters, "ev": ev, "direct": direct}

    def day_fingerprints(self, utc_start: str, utc_end: str) -> dict:
        """`{local-ish day: token}` over [utc_start, utc_end), one row per day.

        PER-DAY, not one global token. `_blocks_data_version` is global, so any
        change invalidates everything — a new half-hour tells a user looking at
        2023 to re-fetch four years, which is the wholesale problem migrated to
        the client. Measured on prod-dev over 13.5 hours of ordinary running:
        27 new blocks (exactly the clock) and 20 changed, none older than 1.8
        days. Today moves twice an hour; 2023 never moves.

        DERIVED FROM THE ROWS, not declared by a writer. A job that forgets to
        announce its scope cannot serve stale data — the fingerprint can only be
        slow, never wrong.

        COVERS RATES, NOT JUST MONEY. A re-price can be cost-neutral: 4.5.13's
        band snap changed 24,128 rates for a total `cost_drift` of £0.0028.
        Totals identical, bands and rate lines different. A fingerprint over
        money alone would have shown a stale rate line indefinitely — which is
        why `_blocks_data_version` already folds in segment rates (BL-32), and
        that property is carried forward rather than rediscovered.

        Both channels, because export settles later than import: an export-only
        settlement moves `exp_*` and nothing else, and must still invalidate.
        """
        rows = self._conn.execute(
            "SELECT substr(b.block_start, 1, 10) AS day, "
            "       COUNT(*) AS n, "
            "       MAX(b.block_start) AS newest, "
            "       ROUND(COALESCE(SUM(b.imp_kwh), 0), 3) AS ik, "
            "       ROUND(COALESCE(SUM(b.imp_cost), 0), 2) AS ic, "
            "       ROUND(COALESCE(SUM(b.exp_kwh), 0), 3) AS ek, "
            "       ROUND(COALESCE(SUM(b.exp_cost), 0), 2) AS ec, "
            "       ROUND(COALESCE(SUM(b.carbon_g), 0), 0) AS cg, "
            # The Billing tab's cost-neutral inputs (C, 30 Sep 2026): ex-VAT, device/house
            # division, standing charge — the EV/Home split is the segments' (`seg`, step
            # 5c). Same set as server._blocks_data_version.
            "       ROUND(COALESCE(SUM(b.imp_cost_exc), 0), 4) AS xx, "
            "       ROUND(COALESCE(SUM(b.imp_kwh_grid), 0), 4) AS xg, "
            "       ROUND(COALESCE(SUM(b.standing_charge), 0), 4) AS xs, "
            # The settlement state (18 Oct 2026, the Pi): a bill applied where it agrees with
            # EMT's estimate moves the cost by under a penny, but turns the half-hour settled —
            # amber to green on the heatmap — and that moved nothing here, so nothing redrew.
            "       SUM(CASE WHEN b.rate_source IN ('settled', 'corrected') THEN 1 ELSE 0 END) AS st, "
            "       SUM(COALESCE(b.rate_corrected, 0)) AS rc "
            "FROM blocks b WHERE b.block_start >= ? AND b.block_start < ? "
            "GROUP BY day", (utc_start, utc_end)).fetchall()
        segs = self._conn.execute(
            "SELECT substr(block_start, 1, 10) AS day, "
            "       COUNT(*) AS n, "
            "       ROUND(COALESCE(SUM(inc_rate), 0), 4) AS ir, "
            "       ROUND(COALESCE(SUM(exc_rate), 0), 4) AS er, "
            "       ROUND(COALESCE(SUM(CASE WHEN attribution = 'ev' THEN kwh END), 0), 4) AS ea, "
            "       ROUND(COALESCE(SUM(kwh * (unicode(band) + 31 * length(band))), 0), 3) AS pk "
            "FROM block_segments WHERE block_start >= ? AND block_start < ? "
            "GROUP BY day", (utc_start, utc_end)).fetchall()
        seg = {r["day"]: "%s:%s:%s:%s:%s" % (r["n"], r["ir"], r["er"], r["ea"], r["pk"]) for r in segs}
        # The cached bills: a bill fetched (and not yet applied, or held back) changes the
        # half-hour's state too — its split, its "Octopus's cost not in yet" — without a block write.
        bills = self._conn.execute(
            "SELECT substr(slot_start, 1, 10) AS day, COUNT(*) AS n, "
            "       ROUND(COALESCE(SUM(cost_incl), 0), 4) AS c, "
            "       ROUND(COALESCE(SUM(ev_kwh), 0), 4) AS e "
            "FROM measured_cost WHERE slot_start >= ? AND slot_start < ? "
            "GROUP BY day", (utc_start, utc_end)).fetchall()
        bill = {r["day"]: "%s:%s:%s" % (r["n"], r["c"], r["e"]) for r in bills}
        # Not the stored remainder (step 5, 8 Oct 2026): no surface reads it, and the main's import
        # and the grid shares it was derived from (ik, xg) are already here.
        return {r["day"]: "%s:%s:%s:%s:%s:%s:%s:%s:%s:%s:%s:%s:%s:%s" % (
                    r["n"], r["newest"] or "", r["ik"], r["ic"],
                    r["ek"], r["ec"], r["cg"], r["xx"], r["xg"],
                    r["xs"], seg.get(r["day"], "0"), r["st"], r["rc"], bill.get(r["day"], "0"))
                for r in rows}

    def get_block_bounds(self, meter_id: str = "electricity_main") -> dict:
        """First and last block for a meter, and the same for blocks carrying
        carbon — in ONE query.

        Two ranges, because they are genuinely different: `carbon_g` only exists
        from v2.3.0 onward, so an account with imported history has energy going
        back years and carbon going back months. A surface that gates a
        "vs last year" comparison on the carbon range wrongly disables it over
        backfilled months — which is what `api_insights_data_bounds` returned (until C step 2) as two
        pairs to avoid.

        Replaces that route's raw SELECT, and gives the calendar-period functions
        a cheap source for their bounds: they need two timestamps and were being
        handed every block to find them.
        """
        row = self._conn.execute(
            "SELECT MIN(block_start) AS earliest, MAX(block_start) AS latest, "
            "       MIN(CASE WHEN carbon_g IS NOT NULL THEN block_start END) AS carbon_earliest, "
            "       MAX(CASE WHEN carbon_g IS NOT NULL THEN block_start END) AS carbon_latest "
            "FROM blocks WHERE meter_id = ?", (meter_id,)).fetchone()
        return dict(row) if row else {"earliest": None, "latest": None,
                                      "carbon_earliest": None, "carbon_latest": None}

    def get_neighbour_block_starts(self, meter_id: str, at: str) -> tuple:
        """The meter's last block start before `at` and first after it, (None where there is
        none) — the bounds of the gap a missing half-hour sits in (gap fill, 18 Oct 2026)."""
        row = self._conn.execute(
            "SELECT (SELECT MAX(block_start) FROM blocks WHERE meter_id = ? AND block_start < ?) "
            "       AS prev, "
            "       (SELECT MIN(block_start) FROM blocks WHERE meter_id = ? AND block_start > ?) "
            "       AS next", (meter_id, at, meter_id, at)).fetchone()
        return (row["prev"], row["next"]) if row else (None, None)

    def get_block_starts_in_range(self, meter_id: str, utc_start: str,
                                  utc_end: str) -> list:
        """Distinct block starts for a meter in [utc_start, utc_end), ascending.

        DISTINCT because a whole-history delete walks the parent's slots to
        recompute remainders, and only needs each slot once.
        """
        rows = self._conn.execute(
            "SELECT DISTINCT block_start FROM blocks "
            "WHERE meter_id = ? AND block_start >= ? AND block_start < ? "
            "ORDER BY block_start", (meter_id, utc_start, utc_end)).fetchall()
        return [r["block_start"] for r in rows]

    def get_block_rate_authority(self, block_start: str,
                                 meter_id: str = "electricity_main"):
        """What outranks the dispatch-overlay heuristic for this block, or None
        if there is no such block.

        Returns `rate_corrected`, `imp_rate` and `rate_source`
        together because the caller's question is one question: *may I re-resolve
        this block's rate, or has something better already decided it?* A user
        correction, a completed reconciliation, or a `measured` (Octopus-billed)
        rate each says no -- BL-53 added the third so a settlement re-cost cannot
        re-resolve the overlay over the bill.

        The caller previously read this row POSITIONALLY (`_mrow[0]` … `_mrow[3]`)
        off a four-column SELECT. Returning a dict makes the SELECT list safe to
        edit.
        """
        row = self._conn.execute(
            "SELECT rate_corrected, imp_rate, rate_source "
            "FROM blocks WHERE block_start = ? AND meter_id = ?",
            (block_start, meter_id)).fetchone()
        return dict(row) if row else None

    def get_block_id(self, block_start: str,
                     meter_id: str = "electricity_main"):
        """The surrogate `blocks.id` for a slot, or None.

        Needed because `generation_mix` keys on `block_id` rather than on
        (block_start, meter_id). The mix is stored against the MAIN meter's row
        only -- it is a property of the grid, not of a meter, and storing it per
        meter would triple the rows for no information.
        """
        row = self._conn.execute(
            "SELECT id FROM blocks WHERE block_start = ? AND meter_id = ? LIMIT 1",
            (block_start, meter_id)).fetchone()
        return row["id"] if row else None

    def apply_measured_to_main(self, block_start: str, *, rate, cost, cost_exc,
                               meter_id: str = "electricity_main", segments=None,
                               replace_correction: bool = False) -> int:
        """Stamp Octopus's billed figure onto a main block. Returns rows changed.

        **Keyword-only after `block_start`, deliberately.** Three values, of
        which two are costs, on the one write in the system
        that records what the supplier actually charged. Positionally, `cost` and
        `cost_exc` transposed is a silently wrong bill; by keyword it does not
        compile into a mistake.

        `rate_source = 'settled'` is stamped here and nowhere else, which is why
        the caller re-checks settleability as defence in depth even though its own
        query already gated on it: once this runs, the drain's backlog can never
        revisit the block.

        The bill's EV/house split is its `segments` (step 5c, 10 Oct 2026): a bill with no EV
        has no EV segment, which is what the jobs that back-attribute a late-arriving dispatch
        select on.

        `replace_correction` (a user's range re-fetch only, settlement surfaces design §5.2):
        the bill replaces the user's Cost Correction, so `rate_corrected` clears in the same
        UPDATE. No automatic pass passes it — they never reach a corrected block.
        """
        with self._conn:
            cur = self._conn.execute(
                "UPDATE blocks SET imp_rate = ?, imp_cost = ?, rate_source = 'settled', "
                "imp_cost_exc = ?" + (", rate_corrected = 0" if replace_correction else "") + " "
                "WHERE block_start = ? AND meter_id = ?",
                (rate, cost, cost_exc, block_start, meter_id))
            # BL-79 (8 Oct 2026): the bill's segments in the same transaction. Written after,
            # a failed segment write left the block stamped 'settled' — never revisited by
            # the drain — under its pre-bill segments, which every surface reads.
            if cur.rowcount and segments is not None:
                self._replace_segments(block_start, meter_id, segments)
        return cur.rowcount

    def recost_sub_meters(self, block_start: str, rate, rate_exc,
                          main_meter_id: str = "electricity_main") -> int:
        """Re-cost every physical sub-meter on a slot to a new parent rate.

        PASS 2 costs each sub-meter at its parent's rate, so when settlement moves
        the main rate the children must follow or the block disagrees with itself
        -- device == main == bill. The kWh columns are untouched; only the priced
        rate layer moves.

        Cost derives from `imp_kwh_grid` -- the portion of the draw the GRID
        supplied, which is what PASS 2 prices -- falling back to raw `imp_kwh` only
        where PASS 2 never clipped. Pricing raw `imp_kwh` over-costs any device with
        non-grid supply (a battery on solar), and the bill's
        `max(0, main - SUM(devices))` clamp then carries that into the bill
        (v4 #473, 4.5.15; tests/test_device_cost_clip.py).

        `imp_kwh IS NOT NULL` is what excludes an export meter, which has no import
        to re-cost. No-op on a synthetic-EV account, which has no sub-meter blocks.

        **Best-effort by contract.** Its caller runs this in its own transaction
        after the main row has already committed, and swallows failures: the
        bill's figure for the main row is authoritative and must not be rolled
        back because a derived device cost could not be written. What makes that
        safe is `repair_device_rate_drift()`, which detects exactly this
        divergence on every pass -- see `run_device_rate_drift_repair`.
        """
        with self._conn:
            cur = self._conn.execute(
                "UPDATE blocks SET imp_rate = ?, "
                "imp_cost = ROUND(COALESCE(imp_kwh_grid, imp_kwh, 0) * ?, 6), "
                "imp_cost_exc = ROUND(COALESCE(imp_kwh_grid, imp_kwh, 0) * ?, 6) "
                "WHERE block_start = ? AND meter_id != ? AND imp_kwh IS NOT NULL",
                (rate, rate, rate_exc, block_start, main_meter_id))
        return cur.rowcount

    def repair_device_rate_drift(self) -> int:
        """Re-cost any sub-meter block whose priced rate has DRIFTED from its
        parent main block. Returns rows changed. Idempotent: matches only rows
        that differ, so a second call changes nothing.

        THE INVARIANT: PASS 2 costs every sub-meter at the parent's rate, so a
        physical device block (ev_charger / battery) must carry the same
        `imp_rate` as the main block for its slot. The kWh columns are never
        touched; only the priced rate layer moves. Cost derives from
        `COALESCE(imp_kwh_grid, imp_kwh)`, as in `recost_sub_meters` -- v4's body
        priced raw `imp_kwh`, and running that on every pass would re-apply the
        over-cost indefinitely (v4 #473, 4.5.15).

        This exists because settlement moves the parent and the children can be
        left behind. `apply_measured_to_block` flips the main row to the settled
        band and then re-costs the devices in a SECOND, best-effort transaction:
        the main write propagates on failure, the device write logs and
        continues. A block can therefore end up settled on the main meter while
        its devices hold the pre-settlement (often peak) rate, which shows up as
        the EV or battery priced at peak on Billing and Usage Stats while the
        bill says off-peak.

        RECOVERED FROM A DELETION, which is why the reasoning is written out
        here. This predicate was the body of v4's `_smb_device_recost_core`, a
        marker-gated one-off whose wrapper said "retires in v5.0.0" -- and the
        wrapper was correctly retired, because the FLOOR argument disposes of
        history (EMT-v5_import_spec §1.1). The body is not historical: there is
        no era filter, no date range and no tariff gate. It enforces a live
        invariant, and reading the docstring instead of the WHERE clause is how
        it nearly went with the wrapper.

        Ungated and recurring is STRICTER than v4, which ran it once ever and
        then left the invariant unenforced for the life of the install.
        """
        with self._conn:
            cur = self._conn.execute(
                "UPDATE blocks SET "
                "  imp_rate = (SELECT m.imp_rate FROM blocks m "
                "              WHERE m.meter_id='electricity_main' AND m.block_start=blocks.block_start), "
                "  imp_cost = ROUND(COALESCE(imp_kwh_grid, imp_kwh, 0) * (SELECT m.imp_rate FROM blocks m "
                "              WHERE m.meter_id='electricity_main' AND m.block_start=blocks.block_start), 6), "
                # the main's ex-VAT rate, derived from its own pair (step 5b: rate × exc/inc);
                # NULL where the main has no ex-VAT £, as before
                "  imp_cost_exc = ROUND(COALESCE(imp_kwh_grid, imp_kwh, 0) * (SELECT m.imp_rate * "
                "              m.imp_cost_exc / NULLIF(m.imp_cost, 0) FROM blocks m "
                "              WHERE m.meter_id='electricity_main' AND m.block_start=blocks.block_start), 6) "
                # sub-meters only; an export meter has no imp_kwh
                "WHERE meter_id != 'electricity_main' AND imp_kwh IS NOT NULL "
                "  AND EXISTS (SELECT 1 FROM blocks m WHERE m.meter_id='electricity_main' "
                "              AND m.block_start = blocks.block_start AND m.imp_rate IS NOT NULL "
                "              AND ABS(COALESCE(m.imp_rate,0) - COALESCE(blocks.imp_rate,0)) > 0.0001)")
        return cur.rowcount

    def relabel_segment_bands(self, block_start: str, ev_band: str,
                              home_band: str,
                              meter_id: str = "electricity_main") -> int:
        """Point each segment's band at the band its attribution should carry.

        BL-34: a block already at the correct RATE can still hold a stale band
        from provisional pricing, because the code that stamps the band only runs
        when the rate changes. A pure LABEL update -- kwh, rate, exc and
        attribution are untouched -- so the reconciliation invariant holds and it
        is self-limiting: rewrites once, then matches.
        """
        with self._conn:
            cur = self._conn.execute(
                "UPDATE block_segments SET band = CASE attribution "
                "WHEN 'ev' THEN ? WHEN 'house' THEN ? ELSE band END "
                "WHERE block_start = ? AND meter_id = ? "
                "AND ((attribution = 'ev' AND band != ?) "
                "     OR (attribution = 'house' AND band != ?))",
                (ev_band, home_band, block_start, meter_id, ev_band, home_band))
        return cur.rowcount

    _RECONCILED_SOURCE = ("rate_source = CASE WHEN rate_source IN ('settled', 'corrected') "
                          "THEN rate_source ELSE 'reconciled' END")

    def apply_reconcile_verdict(self, block_start: str, rate: float, rate_exc,
                                meter_id: str = "electricity_main", segments=None) -> dict:
        """Apply a dispatch-overlay verdict to a block: main row, its sub-meters
        and its segments, in ONE transaction. Returns rows changed per part.

        The atomicity is load-bearing and is why this is one method rather than
        three. A partial verdict leaves sub-meters priced at the OLD rate while
        the main row moves -- the sub-meter costs are derived from the main rate,
        so the block would disagree with itself until something else happened to
        rewrite it.

        Sub-meters bill on `COALESCE(imp_kwh_grid, imp_kwh)` -- the GRID portion,
        not the meter's own total, which for a battery or V2X device differ.
        `rate_corrected = 0` keeps a user's manual correction out of it.
        """
        with self._conn:
            # Step 5 (8 Oct 2026): the verdict is recorded in rate_source, the provenance
            # model, not the second flag rate_reconciled (dropped, step 5). A settled or
            # corrected row keeps its word: either outranks a reconciliation.
            main = self._conn.execute(
                "UPDATE blocks SET imp_rate = ?, " + self._RECONCILED_SOURCE + ", "
                "imp_cost = ROUND(COALESCE(imp_kwh, 0) * ?, 6), "
                "imp_cost_exc = ROUND(COALESCE(imp_kwh, 0) * ?, 6) "
                "WHERE block_start = ? AND meter_id = ?",
                (rate, rate, rate_exc, block_start, meter_id)).rowcount
            subs = self._conn.execute(
                "UPDATE blocks SET imp_rate = ?, " + self._RECONCILED_SOURCE + ", "
                "imp_cost = ROUND(COALESCE(imp_kwh_grid, imp_kwh, 0) * ?, 6), "
                "imp_cost_exc = ROUND(COALESCE(imp_kwh_grid, imp_kwh, 0) * ?, 6) "
                "WHERE block_start = ? AND rate_corrected = 0 AND meter_id IN "
                "(SELECT meter_id FROM meters WHERE is_sub_meter = 1)",
                (rate, rate, rate_exc, block_start)).rowcount
            # BL-79 (8 Oct 2026): the re-priced segments in the same transaction; they were
            # written after it, on their own. They ARE the EV split (step 5c): the EV columns
            # this used to restamp alongside are gone.
            if main and segments is not None:
                self._replace_segments(block_start, meter_id, segments)
        return {"main": main, "sub_meters": subs}

    def get_reconcile_candidates(self, before_iso: str,
                                 meter_id: str = "electricity_main") -> list:
        """Main blocks eligible for dispatch-overlay reconciliation, oldest first.

        Every exclusion here was added after something went wrong, so each is
        named rather than left to read as a filter:

          * `rate_corrected = 0` and `rate_source NOT IN ('settled','corrected')`
            -- BL-53. Never reconcile a block priced by a HIGHER authority than
            this heuristic: Octopus's billed cost, or a user's own correction.
          * `source NOT LIKE 'imported%'` -- imported history carries Octopus's
            ACTUAL billed, dispatch-aware rate. The local lifecycle heuristic
            ("we never saw it start, so it didn't charge") is invalid for periods
            EMT wasn't running, and reverting on it silently un-fixes an import.
            This is the "October regressed on its own" bug.
          * `block_start < ?` -- the started-gate. `started` is captured live, so
            a RESTORE can act as soon as the slot has ended.
          * an off-peak `dispatch_slots` row -- the slot was a smart-charge
            candidate at all.
          * a 'planned' OR 'completed' history row -- a completed-only slot (EMT
            offline, or re-imported) is still repriceable from the authoritative
            completed record.

        Returns dicts.
        """
        rows = self._conn.execute(
            """SELECT b.block_start, b.imp_kwh, b.imp_rate,
                      b.imp_kwh_api, b.interpolated
               FROM blocks b
               WHERE b.meter_id = ? AND b.rate_corrected = 0
                 AND (b.rate_source IS NULL OR b.rate_source NOT IN ('settled','corrected'))
                 AND (b.source IS NULL OR b.source NOT LIKE 'imported%')
                 AND b.block_start < ?
                 AND EXISTS (SELECT 1 FROM dispatch_slots s
                             WHERE s.slot_start = b.block_start AND s.off_peak = 1)
                 AND EXISTS (SELECT 1 FROM dispatch_history h
                             WHERE h.slot_start = b.block_start
                               AND h.kind IN ('planned', 'completed'))
               ORDER BY b.block_start""",
            (meter_id, before_iso)).fetchall()
        return [dict(r) for r in rows]

    def get_block_split_columns(self, block_start: str,
                                meter_id: str = "electricity_main") -> Optional[dict]:
        """One block's EV/house split columns, or None if there is no such block.

        Three sites in `reconcile_dispatch_overlay` each selected an overlapping
        subset of these -- the split re-price, the EV-column re-stamp and the
        post-verdict segment rebuild. One projection serves all three; the cost
        of the extra columns is nil and the cost of three near-identical SELECTs
        drifting apart is not.

        NOTE for the caller that used to filter `AND imp_kwh_ev IS NOT NULL` in
        SQL: that is no longer part of the query, so a block with no split comes
        back as a row with `imp_kwh_ev` None rather than as None. Test the
        COLUMN, not the row.

        BL-79 (3): `ev_kwh` is ev_split's split (the EV segments) — what a caller rebuilding the
        segments reads (the EV columns went in step 5c). `rate_exc`
        is DERIVED, the block's ex-VAT rate from its own pair (imp_rate × imp_cost_exc /
        imp_cost; NULL with no ex-VAT £): the column went in step 5b, 10 Oct 2026.
        """
        row = self._conn.execute(
            "SELECT b.imp_kwh, " + _evsplit.sql_kwh("b") + " AS ev_kwh, b.imp_rate, "
            "b.imp_rate * b.imp_cost_exc / NULLIF(b.imp_cost, 0) AS rate_exc "
            "FROM blocks b WHERE b.block_start = ? AND b.meter_id = ?",
            (block_start, meter_id)).fetchone()
        return dict(row) if row else None

    def distinct_segment_rates(self, block_start: str,
                               meter_id: str = "electricity_main") -> list:
        """The distinct inc-VAT rates across a block's segments, rounded to 6dp.

        The count is the question: exactly one means a SINGLE-RATE block, which
        is the only shape the stale-rated-split heal touches. A genuine
        multi-band capped block has two or more and is left for the measured
        apply.
        """
        rows = self._conn.execute(
            "SELECT DISTINCT ROUND(inc_rate, 6) AS rate FROM block_segments "
            "WHERE block_start = ? AND meter_id = ?",
            (block_start, meter_id)).fetchall()
        return [r["rate"] for r in rows if r["rate"] is not None]

    def count_stale_band_segments(self, block_start: str, ev_band: str,
                                  home_band: str,
                                  meter_id: str = "electricity_main") -> int:
        """How many of a block's segments carry a band that disagrees with the
        band its attribution should have.

        BL-34: a block at the correct RATE can still carry a stale band from
        provisional pricing, because the code that stamps the band only runs when
        the rate changes. Asking first keeps the relabel self-limiting -- it
        rewrites once and then matches.
        """
        return self._conn.execute(
            "SELECT COUNT(*) FROM block_segments "
            "WHERE block_start = ? AND meter_id = ? "
            "AND ((attribution = 'ev' AND band != ?) "
            "     OR (attribution = 'house' AND band != ?))",
            (block_start, meter_id, ev_band, home_band)).fetchone()[0]

    def replace_ev_split(self, block_start: str, meter_id: str = "electricity_main",
                         segments=None) -> int:
        """Write an EV/house split OVER whatever is there. Returns rows changed (0 or 1).

        The counterpart to `carve_ev_split`, and the difference is the whole
        point of having two methods rather than one with a flag:

          carve_ev_split    fills a BLANK. Guarded by "no EV split yet", and it keeps
                            the house band already recorded, so it cannot overwrite an
                            answer something better-informed already gave.
          replace_ev_split  is the RE-ATTRIBUTION path. Its caller has already
                            established that what is stored is absent or wrong
                            (priced before the completed dispatch landed, or a
                            gap fill that guessed), so overwriting is the job.

        The caller's selection is what makes this safe -- imported history and
        `rate_corrected` blocks are excluded there. A single method with an
        `overwrite=` argument would put that distinction one boolean away from
        being got wrong at a call site, which is how a manual correction gets
        stomped.

        Step 5c (10 Oct 2026): the split IS the segments, so this writes them, for a block that
        exists. The EV columns it used to write alongside them are gone.
        """
        with self._conn:
            if segments is None or self._conn.execute(
                    "SELECT 1 FROM blocks WHERE block_start = ? AND meter_id = ?",
                    (block_start, meter_id)).fetchone() is None:
                return 0
            self._replace_segments(block_start, meter_id, segments)
        return 1

    def carve_ev_split(self, block_start: str, meter_id: str = "electricity_main",
                       segments=None) -> int:
        """Write a PREDICTED EV/house split onto an un-carved block. Returns the
        number of rows changed (0 or 1).

        Two guards, both part of the operation rather than caller hygiene:

          * "no EV split yet" -- the sweep selects un-carved blocks and then writes
            them one at a time, so the block can have been carved in between.
            Re-writing it would double-count. Checking in the write's own
            transaction means the check and the write cannot come apart.
          * the house band already recorded -- by the block's existing (house-only)
            segments, when they share one -- was determined by something with better
            information (settlement, a user correction). A predicted carve fills a
            blank; it does not overwrite an answer. (Until step 5c, 10 Oct 2026, both
            lived in the imp_*_ev columns: `imp_kwh_ev IS NULL` and
            `COALESCE(imp_home_band, ?)`. Under the guard the block has no EV segment, so
            only the house band has a recorded answer to keep.)

        `segments`: the carve's (kwh, inc_rate, exc_rate, band, attribution) rows.
        """
        with self._conn:
            row = self._conn.execute(
                "SELECT " + _evsplit.sql_kwh("b") + " IS NULL AS open, "
                + _evsplit.sql_band("b", "house") + " AS house_band "
                "FROM blocks b WHERE b.meter_id = ? AND b.block_start = ?",
                (meter_id, block_start)).fetchone()
            if row is None or not row["open"] or segments is None:
                return 0
            hb = row["house_band"] if row["house_band"] not in (None, "mixed") else None
            self._replace_segments(block_start, meter_id, [
                (k, i, x, (hb if a == "house" else None) or b, a) for k, i, x, b, a in segments])
        return 1

    def get_blocks_pricing_for_range(self, meter_id: str, from_iso: str,
                                     to_iso: str, limit: int) -> list:
        """The pricing columns for blocks in [from_iso, to_iso), oldest first.

        A projection rather than `get_blocks_for_range`'s full block: the
        conformance report re-prices each row and compares the result against
        what is stored, so it needs the stored pricing values and nothing else.

        Rows come back as dicts. The consumer passes each one straight to
        `_reprice_history_block`, which subscripts by column name only -- the
        same access a `sqlite3.Row` supports, so the suite's hand-built Row
        fixtures and these dicts are interchangeable there.
        """
        rows = self._conn.execute(
            "SELECT block_start, meter_id, imp_kwh, imp_rate, imp_cost, imp_cost_exc, "
            + _evsplit.sql_kwh("blocks") + " AS imp_kwh_ev, "
            "imp_rate * imp_cost_exc / NULLIF(imp_cost, 0) AS rate_exc, "
            "source, imp_kwh_api FROM blocks "
            "WHERE meter_id = ? AND block_start >= ? AND block_start < ? "
            "ORDER BY block_start LIMIT ?",
            (meter_id, from_iso, to_iso, limit)).fetchall()
        return [dict(r) for r in rows]

    def get_blocks_off_dispatch_split(self, limit: int, meter_id: str = "electricity_main",
                                      starts=None, tolerance: float = 0.1) -> list:
        """Main blocks whose split should be the completed dispatch's and is not — the one
        population for the stages below the final (bill and split paths design §3.3 2, 6b,
        16 Oct 2026): not corrected, with kWh and a completed dispatch with energy, and either
        no EV split yet, or a GAP-FILLED block (`interpolated = 1`) whose EV differs from the
        dispatch clipped to its kWh by more than `tolerance` (BL-39: a gap fill can stamp a
        wrong split from a stale or planned fragment). The caller leaves out a block at the
        final stage (settled, on an IOG-SMB agreement, with a bill split cached — the settle's).

        It replaced two queries that selected this from opposite ends: the carve's (no split,
        not settled, imported included) and the back-attribution's (no split or BL-39, settled
        included, imported excluded, uncapped accounts only).

        `starts`, when given, limits it to those block starts (the slots a dispatch capture has
        just seen completed). Returns dicts, oldest first."""
        starts = sorted(set(starts)) if starts is not None else None
        if starts is not None and not starts:
            return []
        ev = _evsplit.sql_kwh("b")
        disp = ("(SELECT SUM(ABS(d.energy_kwh)) FROM dispatch_history d WHERE d.kind = 'completed' "
                "AND d.slot_start = b.block_start AND d.energy_kwh IS NOT NULL)")
        rows = self._conn.execute(
            "SELECT b.block_start, b.imp_kwh, b.imp_rate, b.rate_source, "
            "       b.imp_rate * b.imp_cost_exc / NULLIF(b.imp_cost, 0) AS rate_exc, "
            "       " + ev + " AS seg_ev, "
            "       (SELECT m.ev_kwh FROM measured_cost m WHERE m.slot_start = b.block_start "
            "        AND m.direction = 'CONSUMPTION' ORDER BY m.fetched_at DESC LIMIT 1) AS bill_ev "
            "FROM blocks b WHERE b.meter_id = ? AND COALESCE(b.imp_kwh, 0) > 0 "
            "  AND COALESCE(b.rate_corrected, 0) = 0 AND COALESCE(b.rate_source, '') <> 'corrected' "
            "  AND COALESCE(" + disp + ", 0) > 1e-9 "
            "  AND (" + ev + " IS NULL OR (b.interpolated = 1 AND "
            "       ABS(MIN(" + disp + ", b.imp_kwh) - COALESCE(" + ev + ", 0)) > ?)) "
            + ("  AND b.block_start IN (%s) " % ",".join("?" * len(starts)) if starts else "")
            + "ORDER BY b.block_start LIMIT ?",
            (meter_id, tolerance, *(starts or ()), int(limit))).fetchall()
        return [dict(r) for r in rows]

    def count_blocks(self) -> int:
        """Total distinct block count (by block_start)."""
        cur = self._conn.execute(
            "SELECT COUNT(DISTINCT block_start) FROM blocks"
        )
        return cur.fetchone()[0]

    def count_meter_rows(self) -> int:
        """Total meter-row count — useful for diagnostics."""
        cur = self._conn.execute("SELECT COUNT(*) FROM blocks")
        return cur.fetchone()[0]


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

def open_block_store(db_path: str) -> "BlockStore":
    """
    Preferred entry point. Opens (or creates) a BlockStore at db_path,
    applies all PRAGMAs, and ensures the schema exists.

    If the DB file is corrupt, renames it to .corrupt and starts fresh
    so the engine can still start.
    """
    import os
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    try:
        return BlockStore(db_path)
    except Exception as e:
        logger.error("open_block_store: failed to open %s: %s — attempting recovery", db_path, e)
        if os.path.exists(db_path):
            corrupt_path = db_path + ".corrupt"
            try:
                os.rename(db_path, corrupt_path)
                logger.warning("open_block_store: renamed corrupt DB to %s", corrupt_path)
            except Exception:
                pass
        # Start with a fresh DB.
        return BlockStore(db_path)