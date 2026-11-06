"""
engine.py
=========
Energy Meter Tracker — ported from PyScript to HA add-on.

PyScript → add-on mapping:
  log.info/error          →  logging module
  state.get(entity_id)    →  read_sensor(ha, entity_id)
  state.set(...)          →  await ha.set_state(...)
  task.executor(fn, ...)  →  fn(...)  (direct call — we own the thread model)
  @state_trigger(eid)     →  ha.subscribe_state(eid, callback)
  @time_trigger startup   →  engine_startup() called from main()
  @time_trigger period    →  asyncio.sleep(10) loop in engine_loop_task()
  @task_unique(...)       →  asyncio.Lock()
"""

import asyncio
import logging
from contextlib import contextmanager

import job_coordinator as _coord
from job_coordinator import job, ADMIT, DEFER, WAIT
import os
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone

from energy_engine_io import (
    ensure_dir,
    load_json,
    save_json_atomic,
)
from ha_client import HAClient
from block_store import BlockStore, open_block_store

logger = logging.getLogger("engine")

# ─────────────────────────────────────────────────────────────────────────────
# Paths & constants
# ─────────────────────────────────────────────────────────────────────────────

DATA_DIR           = "/data/energy_meter_tracker"
CONFIG_PATH        = f"{DATA_DIR}/meters_config.json"
# API credentials are stored in their OWN file (not the DB, not config.yaml) so
# they are never swept into the DB backups. Entered in-app, read at startup.
KRAKEN_CREDS_PATH  = f"{DATA_DIR}/kraken_credentials.json"
# current_block.json removed in 2.1.0 — state stored in DB current_block table
BLOCKS_PATH        = f"{DATA_DIR}/blocks.json"    # legacy pre-SQLite store; detected only to warn (no longer auto-migrated)
BLOCKS_DB_PATH     = f"{DATA_DIR}/blocks.db"
# cumulative_totals.json removed in 2.1.0 — totals derived from blocks table

import os as _os_engine
# BL-5: the supervised backup dir is namespaced by site slug once the DB is open
# (see instance.py). Until then this holds the legacy/standalone default so any
# pre-startup use is still valid. `resolve_backup_dir_for_site()` rebinds it.
SHARE_BACKUP_DIR   = (
    _os_engine.path.join(DATA_DIR, "backup")
    if _os_engine.environ.get("EMT_MODE") == "standalone"
    else "/share/energy_meter_tracker_backup"
)

# BL-5: set at startup from the data lineage check. Read by the server to surface
# a dismissible "restored from another instance" notice. Never gates anything.
FOREIGN_RESTORE_NOTICE = {"foreign": False, "db_uuid": None, "acknowledged": False}


def resolve_backup_dir_for_site(site_name: str | None) -> str:
    """BL-5: rebind SHARE_BACKUP_DIR for this site, migrating/adopting as needed.

    Called at startup (and after a site rename) once the config period — and
    therefore the site name — is known. Standalone is unaffected: its backup dir
    is volume-scoped and site-independent. Silent in all cases.
    """
    global SHARE_BACKUP_DIR
    try:
        import instance as _inst
        # Only pass `current_dir` when it is a directory THIS instance owns — it
        # is used to move our backups on a site rename. At startup the constant
        # still holds the legacy path, which we do not own until adopted; passing
        # it here made the rename path try to claim another instance's backups.
        current = None
        if _os_engine.path.isdir(SHARE_BACKUP_DIR):
            iid = _inst.get_instance_id()
            if _inst._owned_by_us(SHARE_BACKUP_DIR, iid):
                current = SHARE_BACKUP_DIR
        SHARE_BACKUP_DIR = _inst.resolve_backup_dir(site_name, current_dir=current)
    except Exception as e:
        logger.warning("resolve_backup_dir_for_site: failed (%s); keeping %s",
                       e, SHARE_BACKUP_DIR)
    return SHARE_BACKUP_DIR

# Module-level BlockStore instance — opened in engine_startup()
_store: BlockStore | None = None


def get_store() -> BlockStore:
    """Return the active BlockStore. Raises if engine_startup has not run."""
    if _store is None:
        raise RuntimeError("BlockStore not initialised — engine_startup() has not run")
    return _store


def get_and_clear_meter_reset() -> bool:
    """Return True if a meter read reset was detected since last call, then clear the flag.
    Used by the web server to surface an advisory notification to the user."""
    global _meter_reset_detected
    val = _meter_reset_detected
    _meter_reset_detected = False
    return val

BLOCK_MINUTES      = 30  # default — overridden at runtime from config
GAP_FILL_LIMIT_HOURS = 12  # gaps longer than this are not gap-filled

# ─────────────────────────────────────────────────────────────────────────────
# Module-level state
# ─────────────────────────────────────────────────────────────────────────────

_read_queue:               list         = []
_last_known_sensor_values: dict         = {}
_engine_loop_lock:         asyncio.Lock = None   # initialised in setup()
_engine_paused:            bool         = False
_pass2_quiet:              bool         = False   # suppress per-block PASS 2 logs during bulk recompute (INFO, and the routine clip WARNINGs — v4 4.5.21; a bulk pass logs one summary instead)
_last_ci_fetch:            datetime | None = None   # UTC — last carbon intensity fetch
_last_dispatch_capture:    datetime | None = None   # UTC — last dispatch-slot capture (5-min cadence)
_last_reconcile:           datetime | None = None   # UTC — last dispatch reconciliation (hourly cadence)
_current_slot_mix:         dict            = {}      # captured_at → generationmix list
_meter_reset_detected:     bool            = False   # set when post-gap read < pre-gap read (possible meter replacement)

# 3.0.0 Kraken integration runtime state. Populated at startup by
# _kraken_startup_discovery() when an API key is configured. The ingester
# task (4b-ii) reads _kraken_discovery for the verified meter identifiers.
# None/empty until then; the presence of _kraken_client gates all API work.
_kraken_client = None          # KrakenAPIClient | None
_kraken_discovery: dict | None = None
# The account's agreements as stored (G1 design §3), in discovery's shape — {channel: {mpan,
# tariff_code, agreements}} — loaded at start-up before discovery runs, so a restart with the API down
# still knows them. `_agreement_info` reads discovery's answer once it has one, these until then.
_stored_agreements: dict | None = None
_kraken_account_number: str | None = None
# (db_account, creds_account) when the loaded DB is stamped to a different Octopus
# account than the current credentials → API auto-activation is suppressed (#357).
_kraken_account_mismatch = None
# When discovery last ran (time.monotonic()), and how often the agreements are asked for again
# while the engine runs (17 Oct 2026, the user: once a day is adequate). Discovery ran only at
# startup and on an in-app connect, so a long-running install never learnt of a new agreement —
# a renewal, an export tariff starting or ending, a move off IOG-SMB — until it restarted.
_kraken_discovered_at: "float | None" = None
_REDISCOVERY_INTERVAL_S = 24 * 3600
# Start-up discovery asked again while it has not answered (G1 design §5): the settle sweep is
# scheduled only once discovery has answered, so a failure at start-up — the API down after a restore
# — left the history unsettled until the next engine start.
_DISCOVERY_RETRY_S = 3600
_discovery_retry_at: "float | None" = None
_discovery_retry_running = False
_REDISCOVERY_RETRY_S = 3600              # after a failed re-discovery
# Reference to the running HAClient + poll-task handle, captured when the engine
# loop starts. Lets connect_kraken_now() (the in-app credential-save path)
# launch the DCC poll task without an addon restart — the task is otherwise
# started once at boot and exits permanently if the API wasn't yet configured.
_engine_ha = None              # HAClient | None
_kraken_poll_task_handle = None  # asyncio.Task | None
# The rate schedules connect_kraken_now() builds after it has answered (3 Nov 2026): cancelled with the
# poll task, before engine_startup or a disconnect closes the client it reads.
_kraken_rates_task_handle = None  # asyncio.Task | None
# True once the first (cold) engine_startup has completed in this process. The
# rogue-total guard (clearing the in-progress block's carried reads) is valid
# ONLY on a cold start, where those reads are stale leftovers from a previous
# container. Every later engine_startup in the SAME process — HA reconnect or
# config-save — is "warm": the process never stopped, the in-progress reads are
# LIVE, and clearing them would discard real consumption (observed: a Mini block
# lost ~3.4 kWh during an HA-upgrade reconnect storm). A new process resets this
# to False, so a genuine container restart is correctly treated as cold.
_cold_start_complete = False
# Result of the last third-party HA integration detection (BCD live-power
# offload source, OHME charge-mode smart-vs-boost signal). Populated by
# _detect_and_log_integrations() each startup; consulted by the overlay (OHME
# verified path) and the live-power tile (BCD offload). None until first run.
_detected_integrations: dict | None = None
# 4b-ii: ingester + scheduling. dry-run is HARDCODED here (4b choice); the
# flip to live writes is a deliberate later step. Poll every 6h (DCC settles
# ~daily, so frequent polling adds nothing). backfill cap is generous; the real
# window is MIN(block_start)→now via get_oldest_block_start().
_kraken_ingester = None         # KrakenIngester | None
_KRAKEN_POLL_INTERVAL_S = 6 * 3600
# BL-95 (1 Nov 2026): the settlement pill's *Retry settlement* runs the poll's cycle now, then the
# 6 hours start again from it. A request within this long of a cycle's start is refused, so clicks
# cannot spend the account's Octopus allowance (four instances share it).
_KRAKEN_POLL_NOW_MIN_S = 15 * 60
_kraken_poll_wake = None        # asyncio.Event the poll waits on between cycles, made by the task
_kraken_poll_state: dict = {"running": False, "started": None, "finished": None}
# 4b-iii: LIVE. The poll writes DCC settlement into blocks (imp_kwh_api / exp_kwh_api +
# needs_pass2_rerun), which the engine drain re-runs into billing. (v4's one-time snapshot before
# the first live write, `pre_live_snapshot_done`, went in engine step 5a, 27 Oct 2026: every version
# starts from the pre-migration copy, and the share copy runs nightly.)
_KRAKEN_BACKFILL_CAP_DAYS = 400
# Auto settlement sweep: the 6h incremental poll only re-checks a sliding window
# (last-seen − 6h → now). When DCC settles intervals out of order or later than
# that overlap (export commonly lags import by days), an earlier interval can age
# out behind the cursor and never be re-requested by the incremental poll. A
# daily sweep re-fetches oldest-unsettled → now (advance_cursor=False) so those
# gaps self-heal. Bounded to a horizon: beyond it, DCC settlement is unlikely and
# the provisional value stands (also caps the sweep window cost: 30 days is 1,440
# half-hours, one 1,500-row REST page a channel).
# 30 days, not 14 (24 Oct 2026, the user): a smart meter off the HAN can wait up to 21 days
# before the supplier asks for a reconnect, and its DCC figures follow — 14 gave up on them.
_SETTLEMENT_SWEEP_HORIZON_DAYS = 30
_SETTLEMENT_SWEEP_MIN_INTERVAL_S = 24 * 3600   # run at most once per day
_STATE_LAST_SWEEP = "last_settlement_sweep_utc"
# Chunk 5: cached rate schedules for reconcile-time rate repair. Built once per
# poll cycle by the (async) poll task; read by the (sync) drain via the resolver
# below. Rates stored as £/kWh (converted from the API's pence at build time).
_kraken_rate_schedules: dict = {}   # {"import": RateSchedule, "export": RateSchedule}
# BL-54: True once the import schedule is agreement-stitched (or single-agreement) so it
# prices each block on the tariff that applied on its date. False if a multi-agreement
# stitch failed — then the schedule is current-tariff-only and a pre-migration block must
# NOT be repriced from it (it would get the current tariff's rate). Drives _agreement_priced_ok.
_import_schedule_covers_history = True
_kraken_current_agreement_from = None   # current import agreement valid_from (naive-UTC)
# Standing-charge schedule (single, import-side — the daily standing charge).
# Stored as £/day after conversion at resolve time, like rates.
_kraken_standing_schedule = None    # RateSchedule | None
_rate_schedule_unsupported: dict | None = None  # set when an active tariff returns
#   NO standard unit rates — the new IOG time-of-use / 6-hour-cap tariff signature.
#   Surfaced so a migrated user sees a clear "not yet supported" state, not £0 bills.
_kraken_mini_reader = None          # MiniBoundaryReader | None — Chunk 7
# Latest Mini live demand, polled server-side by the engine tick (page-independent)
# so the 48h history fills continuously and the gauge reads it without its own
# GraphQL call. {"kw": float|None, "ts": monotonic seconds, "wall": epoch seconds}
_last_mini_demand = {"kw": None, "ts": 0.0, "wall": 0.0}
_MINI_POLL_GAP = 55.0               # seconds between Mini demand polls (~1/min)
# Start collecting Mini telemetry this many seconds BEFORE the block boundary
# (per spec: 20s lead), continuing until the post-boundary read is bracketed.
_MINI_COLLECT_LEAD_S = 20


_BILLING_SOURCE_KEY = "billing_source"
_VALID_BILLING_SOURCES = ("dcc", "cad")

_MODE_KEY = "data_source_mode"
_VALID_MODES = ("cad", "cad+api", "api", "api+mini")

# DB↔account stamp (#357): the Octopus account this DB belongs to, written on the
# first successful discovery. Lets us swap DBs safely — if the credentials now
# present are for a DIFFERENT account than the loaded DB was stamped with, the API
# is NOT auto-activated (which would poll the wrong account's data into this DB).
# Credentials are KEPT either way; an explicit in-app reconnect re-associates.
_ACCOUNT_KEY = "kraken_account_number"

# BL-73: which supplier this database's history belongs to, as a normalised
# registry key in store_meta. Lives in store_meta rather than kraken_state
# because "which supplier" is not a Kraken fact — a local-only install has no
# Kraken anything and still has a supplier answer ("not-listed").
_SUPPLIER_KEY = "supplier"

# BL-72: the software version that last opened this database. The ONLY thing a
# database could not previously say about itself — `schema_version` reads 1 on a
# 4.3.1 and a 4.5.14 alike, and the upgrade-backup version file lives in DATA_DIR,
# which backups do not contain. So a backup, or a blocks.db handed to anything,
# carried no record of what produced it.
_WRITTEN_BY_KEY = "written_by"


def _norm_account(x) -> str:
    return (x or "").strip().upper()


def get_db_account() -> str | None:
    """The Octopus account this DB is stamped to, or None if never stamped
    (legacy/fresh DB)."""
    try:
        v = _reader().get_kraken_state(_ACCOUNT_KEY)
    except Exception:
        v = None
    return v or None


def kraken_account_mismatch():
    """When non-None, the loaded DB is stamped to a different Octopus account than
    the current credentials, so the API was NOT auto-activated. Returns
    (db_account, creds_account). Credentials are kept; an explicit in-app reconnect
    re-associates this DB."""
    return _kraken_account_mismatch


def get_data_source_mode() -> str:
    """Current data-source mode, stored in kraken_state. One of:
    'unset'    — no mode chosen yet (fresh/flattened DB, pre-survey). NOTHING
                 should auto-activate (esp. the API) in this state.
    'cad'      — local meter reads only, no supplier API
    'cad+api'  — local reads + DCC settlement/rates
    'api'      — no local reads; DCC is the import source
    'api+mini' — no local reads; Mini provisional + DCC settlement
    Returns 'unset' when nothing is stored — deliberately NOT defaulting to
    'cad', so a flat DB (which has chosen nothing) is distinguishable from a
    user who actually chose cad. The survey sets the mode explicitly."""
    try:
        val = _reader().get_kraken_state(_MODE_KEY)
    except Exception:
        val = None
    return val if val in _VALID_MODES else "unset"

def set_data_source_mode(mode: str) -> str:
    """Persist the data-source mode. Returns the stored value. Raises ValueError
    on an unknown mode. The mode is derived by the setup survey and drives
    sensor collection, API requirement, and Mini activation."""
    mode = (mode or "").lower()
    if mode not in _VALID_MODES:
        raise ValueError(f"mode must be one of {_VALID_MODES}, got {mode!r}")
    _store.set_kraken_state(_MODE_KEY, mode)
    logger.info("set_data_source_mode: %s", mode)
    return mode


# Kept in v5 (22 Oct 2026, engine review step 1): no 2.x database reaches v5 (the import floor
# refuses it) and a v4 import carries its mode, but the first-run wizard saves the mode before the
# config and treats a failed mode save as non-fatal ("mode defaults to cad") — this is what makes
# that default true. (Marked "DEPRECATED — remove in 5.0.0" until then, with another function's
# wording: a "pre-4.4.0 legacy migration".)
def _detect_upgrade_mode(config: dict) -> str | None:
    """Upgrade bridge: decide the mode for an install that has no stored mode.

    2.x installs predate the data-source-mode concept, so on first 3.0.0 boot
    get_data_source_mode() returns 'unset'. We must NOT leave an existing,
    working user in 'unset' (which disables everything pending the survey), and
    we must NOT infer or auto-activate the API. The conservative rule:

      no stored mode AND a non-sub (main) meter has a configured import read
      sensor (channels.import.read set)  →  this is an existing CAD user;
      silently set mode 'cad' and carry on exactly as before.

    A genuinely fresh/flattened install (no import sensor configured) is left
    'unset' so the setup survey runs. Returns the mode it set, or None if it
    made no change (already had a mode, or fresh install).

    Because 'cad' makes mode_uses_api() False, this also neutralises any stale
    credentials file left in /data on upgrade — nothing reads it under cad.
    """
    if get_data_source_mode() != "unset":
        return None  # mode already chosen (survey, or a prior 3.0.0 boot)
    has_import_sensor = any(
        ((meter.get("channels") or {}).get("import") or {}).get("read")
        for meter in (config.get("meters") or {}).values()
        if not (meter.get("meta") or {}).get("sub_meter")
    )
    if not has_import_sensor:
        return None  # fresh install — leave 'unset' for the survey
    set_data_source_mode("cad")
    logger.info(
        "engine_startup: upgrade detected — existing import sensor present and "
        "no stored mode; preserving as 'cad' (survey not required, API path off)"
    )
    return "cad"


def mode_uses_api(mode: str = None) -> bool:
    """True only if an explicitly-set mode involves the supplier API
    (cad+api / api / api+mini). 'unset' → False (don't auto-activate API)."""
    m = mode or get_data_source_mode()
    return m in _VALID_MODES and "api" in m


def mode_uses_mini(mode: str = None) -> bool:
    """True only for api+mini."""
    return (mode or get_data_source_mode()) == "api+mini"


# ── Supplier capability (server-side gating seam) ────────────────────────────
# The supplier gate decides whether an API-backed mode is permitted at all. This
# is the authoritative server-side check: the UI gating is convenience, this is
# enforcement. The registry itself is suppliers.py, shared with the import gate.
from suppliers import API_CAPABLE as _API_CAPABLE_SUPPLIERS


def normalize_supplier(supplier: str) -> str:
    """Normalise a stored/submitted supplier (possibly legacy free-text like
    'Octopus Energy', or empty for a v2 user) to a registry key. Empty → ''.
    Any non-Octopus free-text collapses to 'not-listed' (the local-only
    migration default), so legacy configs map cleanly onto the gating model."""
    v = (supplier or "").strip().lower()
    if not v:
        return ""
    if "octopus" in v:
        return "octopus"
    if v in _API_CAPABLE_SUPPLIERS or v == "not-listed":
        return v
    return "not-listed"


def supplier_is_api_capable(supplier: str) -> bool:
    """True if the (normalised) supplier supports an API-backed mode."""
    return normalize_supplier(supplier) in _API_CAPABLE_SUPPLIERS


def stamp_version(store) -> str | None:
    """BL-72: record the running app version in store_meta['written_by'].

    What this is for: a future reader — EMT (v5) importing a v4 backup — needs to
    know which software produced the history it has been handed, because that is
    what says whether the one-off heals have run against it. All heals are
    marker-gated and ungated markers run at startup, so upgrading ANY older
    database to a given release runs every heal that release carries. "Opened by
    version X" therefore means "healed to X's standard", and a reader gates on a
    floor version rather than on the heals individually.

    Deliberately NOT a computed heal level. An earlier design proposed one, on the
    grounds that a level could rise without a reader learning v4's version
    numbering. But heals are historical: if a later release adds one, the reader
    simply raises the floor it accepts. A version string carries that with no
    mechanism, and the same stamp continues into v5 (which records its own release
    and uses the same marker mechanism for its own heals).

    Stamped on every startup, so it names the LAST version to open the database —
    which is the version whose heals have run. Absent only on a database no
    stamping release has ever opened, and that absence is itself the answer.

    Does NOT write when the version cannot be read: an unreadable config leaves the
    previous stamp in place, which is the last thing actually known to be true,
    rather than overwriting it with 'unknown'.

    Returns the version stamped, or None if there was nothing to stamp.
    """
    if store is None:
        return None
    try:
        ver = _read_config_version()
        if not ver or ver == "unknown":
            return None
        if store.get_meta(_WRITTEN_BY_KEY) != ver:
            store.set_meta(_WRITTEN_BY_KEY, ver)
            logger.info("stamp_version: %s", ver)
        return ver
    except Exception as e:
        # Never block startup for a marker.
        logger.warning("stamp_version: failed: %s", e)
        return None


def stamp_supplier(store) -> str | None:
    """BL-73: record this database's supplier as a normalised registry key in
    store_meta['supplier'].

    Why stamp it when config_periods.supplier already exists: that column is the
    DISPLAY + historical record and holds two different kinds of thing. An
    install set up through the wizard writes a registry key ('octopus'); one that
    predates the dropdown holds whatever the user typed ('Octopus Energy'), and
    every real database we have carries the latter. normalize_supplier() is the
    authoritative mapping and it lives here, so v4 records its OWN reading of its
    own field, once — rather than leaving every future reader to reimplement
    those rules against a field with mixed contents.

    The answer is NOT always 'octopus'. 'not-listed' is a real, supported state:
    a local-only install with no supplier API at all. Any reader that assumes
    an Energy Meter Tracker database implies an Octopus account is wrong, and
    this is the marker that says so.

    CURRENT supplier, deliberately. The stamp answers "whose credentials does
    this install use", which a reader needs in order to know what to ask for —
    not "who supplied the oldest block". A supplier change opens a new config
    period and engine_startup re-runs on config save, so the stamp follows.

    ABSENT means never established — a v2-era config predating the field. That
    is a real distinction from 'not-listed' (an answer given), so nothing is
    written rather than a placeholder that would read as an answer.

    Returns the key stamped, or None when there was nothing to stamp.
    """
    if store is None:
        return None
    try:
        pid = store.get_current_config_period_id()
        if pid is None:
            return None
        key = normalize_supplier((store.get_config_period(pid) or {}).get("supplier"))
        if not key:
            return None
        if store.get_meta(_SUPPLIER_KEY) != key:
            store.set_meta(_SUPPLIER_KEY, key)
            logger.info("stamp_supplier: %s", key)
        return key
    except Exception as e:
        # Never block startup for a marker. An absent stamp is a defined state.
        logger.warning("stamp_supplier: failed: %s", e)
        return None


def _get_billing_source() -> str:
    """Current global billing source ('dcc' default, or 'cad').

    Stored in kraken_state so it persists and is readable by the sync drain.
    Defaults to 'dcc' (the validated default — settle to DCC except where
    missing).
    """
    try:
        val = _reader().get_kraken_state(_BILLING_SOURCE_KEY)
    except Exception:
        val = None
    return val if val in _VALID_BILLING_SOURCES else "dcc"



# Meter-validation noise floor (kWh). A smart-charge slot is credited off-peak
# only if the block drew at least this much import — a PRESENCE check (did real
# charging happen?), NOT a comparison to the dispatch delta. Block draw is
# car + household baseload; dispatch delta is car-only, so comparing them is
# wrong. 0.1 kWh rejects sensor noise / tiny baseload trickle while any genuine
# EV charge clears it with large margin (live data: charging slots 2–6 kWh,
# idle slots ~0.0). Confirmed against real slot data.
_DISPATCH_OVERLAY_MIN_KWH = 0.1


def _dispatch_overlay_rate(channel_name: str, block_start: str,
                           base_rate: float, imp_kwh: float | None, quiet: bool = False):
    """Dispatch overlay (step 2 piece 2). Given a block's base-resolved import
    rate, decide whether an Intelligent smart-charge dispatch should override it
    to the off-peak rate. Returns the rate to use (£/kWh).

    Rules (design §1c):
      - Import channel only (export is never dispatch-adjusted).
      - The block's 30-min slot must be a persisted smart-charge dispatch_slot.
      - METER VALIDATION: the block must have drawn at least _DISPATCH_OVERLAY_MIN_KWH
        of import (0.1 kWh noise floor). A PRESENCE check — did real charging
        happen? — NOT a comparison to the dispatch delta. A slot with no real
        draw is NOT overridden (the over-reporting guard — Zappi/IOG dispatch
        slots stay 'on' after charging stops).
      - Only overrides when base_rate is ABOVE the off-peak rate (i.e. the slot
        is out-of-window / peak). In-window slots are already off-peak — no-op.

    Idempotent: the decision is a pure function of (base schedule, dispatch_slots,
    meter draw). (Its compute-and-log mode, never switched on since it went live,
    went 22 Oct 2026.)
    """
    if channel_name != "import" or _store is None or not block_start:
        return base_rate
    try:
        # _reader(): the detail panel asks for a half-hour's predicted band from a request thread
        slot = _reader().get_dispatch_slot(_snap_to_slot(block_start))
    except Exception:
        return base_rate
    if not slot or not slot.get("off_peak"):
        return base_rate
    if not _agreement_priced_ok(block_start):
        return base_rate      # BL-54: don't flip a pre-current-agreement block on a
                              # current-tariff-only schedule (would use the wrong tariff)
    # Per-decision config fingerprint (design open-item 2): makes every overlay
    # decision self-describing in the log, so a user's bug report carries the
    # config context (mode, mini, provider, source, apply-state) without a
    # round-trip. NOTE mini=active reports the ACTUAL runtime reader state
    # (_kraken_mini_reader), not the nominal mode: in plain `api` mode a Mini
    # device elevates to provisional collection at runtime, so mode=api can
    # legitimately run with mini=active — and the Mini is what supplies the
    # provisional kWh this guard validates against.
    _fp = ("mode=%s mini=%s provider=%s source=%s"
           % (get_data_source_mode(),
              "active" if _kraken_mini_reader is not None else "off",
              slot.get("provider"), slot.get("source")))
    # Meter validation: only credit slots that actually drew power. PRESENCE
    # check against the block's CURRENT kWh (provisional at finalise,
    # authoritative at settlement) — a 0.1 kWh noise floor, NOT a dispatch-delta
    # comparison. Below the floor → treat as no real charging (over-report guard).
    if not imp_kwh or imp_kwh < _DISPATCH_OVERLAY_MIN_KWH:
        if not quiet: logger.info("dispatch-overlay: slot %s active but draw below floor "
                    "(%.4f kWh < %.2f) — NOT overriding (over-report guard) [%s]",
                    block_start, imp_kwh or 0.0, _DISPATCH_OVERLAY_MIN_KWH, _fp)
        return base_rate
    sched = _kraken_rate_schedules.get("import")
    if sched is None or sched.is_empty():
        return base_rate
    off_peak_pence = sched.off_peak_rate_near(block_start)
    if off_peak_pence is None:
        return base_rate
    off_peak_rate = round(off_peak_pence / 100.0, 6)
    # Only override if base is meaningfully above off-peak (out-of-window slot).
    if base_rate <= off_peak_rate + 1e-9:
        return base_rate  # already off-peak (in-window) — no-op
    if not quiet: logger.info("dispatch-overlay: APPLIED %s peak→off-peak %.5f→%.5f "
                "(%.4f kWh) [%s]", block_start, base_rate,
                off_peak_rate, imp_kwh, _fp)
    return off_peak_rate


def _error_text(where, e) -> str:
    """What a job's status or a reply tells a person about an exception: Octopus's in plain words
    (`kraken_api_client.person_message` — never its URL, which names the account, MPAN or serial),
    anything else as where and when. The caller logs the exception itself (2 Nov 2026)."""
    from kraken_api_client import person_message
    msg = person_message(e)
    if msg:
        return msg
    return ("Something went wrong (%s, %s) — the log has the detail."
            % (where, datetime.now().strftime("%H:%M:%S")))


def _snap_to_slot(block_start: str) -> str:
    """Snap a block_start to its 30-min slot boundary ISO (matches capture)."""
    try:
        dt = datetime.fromisoformat(str(block_start).replace("Z", "").split("+")[0])
        dt = dt.replace(minute=(0 if dt.minute < 30 else 30),
                        second=0, microsecond=0)
        return dt.isoformat()
    except Exception:
        return block_start


# ── IOG 6-hour-cap house/EV billing split (BL-9) ─────────────────────────────
# A bump/boost dispatch bills EV at peak, never off-peak (the cap's exception).
_IOG_BUMP_SOURCES = {"bump-charge", "boost"}


def dispatch_ev_kwh(block_start: str, kwh: float, store=None) -> float:
    """The completed dispatch's EV kWh for a half-hour, clipped to the half-hour's grid import —
    the one computation of the dispatch's EV (bill and split paths design §3.3). It was two: this
    (the snapped slot, through `_reader()`, for the pricing seam) and `completed_dispatch_kwh` + min
    in the settle's rule (the exact slot). `store` defaults to this thread's read store."""
    if not kwh or kwh <= 0:
        return 0.0
    st = store if store is not None else (_reader() if _store is not None else None)
    if st is None:
        return 0.0
    try:
        ev = st.completed_dispatch_kwh(_snap_to_slot(block_start))
    except Exception:
        return 0.0
    return min(float(ev or 0.0), float(kwh))


def _capped_schedules() -> bool:
    """The capped IOG-SMB rate schedules are loaded (EV device off-peak and peak) — the 6-hour-cap
    pricing can run. One test where there were four copies (bill and split paths design §3.3)."""
    return ("ev_device_off_peak" in _kraken_rate_schedules
            and "ev_device_peak" in _kraken_rate_schedules)


def _iog_slot_is_boost(block_start: str, store=None) -> bool:
    """True when a bump/boost dispatch covers the slot (source on the planned/
    started record; completed rows carry no source). `store` defaults to the
    module store; the one-off rate repair passes an explicit store (testable)."""
    st = store if store is not None else _store
    if st is None:
        return False
    try:
        rows = st.get_dispatch_history_for_slot(_snap_to_slot(block_start))
    except Exception:
        return False
    return any((r["source"] or "") in _IOG_BUMP_SOURCES for r in rows)


def cap_excluded(slot_start: str, store=None) -> bool:
    """Whether a completed charge in this half-hour is left out of the 6-hour cap: it was not a
    smart charge billed off-peak, so it cannot use the allowance (1 Nov 2026, the user: "bumped
    charges and peak rate costed charges shouldn't count toward 6hr limit").
    - a bump or boost by its source (`_iog_slot_is_boost`) — which Octopus no longer sends: on the
      user's account every planned dispatch reads `smart-charge` and every completed one `unknown`;
    - the bill priced the EV's bucket at peak (`_bucket_bands`, Octopus's own rate);
    - with no EV bucket billed, a bump by EMT's own rule: outside the guaranteed window, the
      dispatch lifecycle's verdict is peak (`_lifecycle_band` — completed with no plan or start while
      EMT was recording), as `predicted_band` prices it.
    A charge past the 6 hours not billed yet still counts, so the boundary stays where it is."""
    st = store if store is not None else _store
    if st is None:
        return False
    try:
        if _iog_slot_is_boost(slot_start, st):
            return True
        bd = st.get_measured_breakdown(slot_start)
        if bd and bd.get("ev_kwh") is not None and float(bd["ev_kwh"]) > 1e-9:
            return _bucket_bands(slot_start, bd).get("ev") == "peak"
        sched = _kraken_rate_schedules.get("import")
        if sched is None or sched.is_off_peak(slot_start) is True:
            return False
        return _lifecycle_band(slot_start, "off_peak",
                               was_online=not st.has_interpolated_block(slot_start)) == "peak"
    except Exception:
        return False


def _iog_cap_day_boundary(block_start: str, tz_name: str, store=None):
    """The 6-hour cap boundary for this slot's noon→noon cap-day, or None. Thin wrapper
    over BlockStore.cap_day_boundary — the SINGLE source that unions the cap-day's
    completed dispatch windows and EXCLUDES what cannot use the allowance (`cap_excluded`:
    a bump, a peak-billed charge). `store` defaults to the module store."""
    st = store if store is not None else _store
    if st is None:
        return None
    try:
        return st.cap_day_boundary(block_start, tz_name, exclude=lambda s: cap_excluded(s, st))
    except Exception:
        return None


def _iog_site_tz(store=None) -> str:
    """The site's IANA timezone (for the noon→noon cap-day), from the newest
    config period; 'UTC' fallback. `store` defaults to the module store."""
    st = store if store is not None else _store
    if st is None:
        return "UTC"
    try:
        return st.site_timezone()
    except Exception:
        return "UTC"


# The channel key pricing leaves a main import's segments on for the block write to commit in
# its own transaction (step 5c). Must equal block_store.STAGED_SEGMENTS, which reads it (a
# literal here because tests stub block_store before engine imports; test_segments_one_write
# asserts the two match).
STAGED_SEGMENTS = "_staged_segments"
_SEGMENT_WRITE_FAILURES = 0   # HARDENING (watch #10): count silent segment-write misses so a hole is a signal, not a mystery


def _refit_held_segments(imp_ch: dict, block_start: str, meter_id: str, kwh: float,
                         rate: float, *, stage: bool = False) -> bool:
    """Fit a held-rate block's stored segments to its settled kWh (BL-79), and keep the EV
    columns' kWh and £ equal to the EV segments if the fit shrank the EV. Returns whether the
    segments were rewritten. Best-effort, like _persist_block_segments: never blocks pricing."""
    if _store is None or not block_start:
        return False
    try:
        import pricing_segments as _ps
        segs = _store.get_block_segments(block_start, meter_id)
        if not segs or abs(sum(float(x["kwh"] or 0.0) for x in segs) - float(kwh or 0.0)) <= 1e-6:
            return False
        # The band a NEW house segment takes (the fit gives an all-EV block one): the house's own
        # band where the block recorded one, else the EV's on the house axis (off_peak stays
        # off_peak, peak is the house's day), else standard. Until step 5c the house band came
        # from imp_home_band; a loaded block's home_band is now its house segments', and a block
        # that needs a new house segment has none.
        _hb = imp_ch.get("home_band")
        if _hb not in ("off_peak", "day"):
            _hb = {"off_peak": "off_peak", "peak": "day"}.get(imp_ch.get("ev_band"), "standard")
        _r = _exc_ratio(imp_ch, block_start)
        new = _ps.refit_to_kwh(segs, kwh, rate=rate,
                               exc_rate=(round(rate * _r, 6) if _r is not None and rate else None),
                               band=_hb)
        if stage:
            imp_ch[STAGED_SEGMENTS] = new
        else:
            _store.set_block_segments(block_start, meter_id, new)
        if imp_ch.get("kwh_ev") is not None:
            evk = _ps.attribution_kwh(new, "ev")
            if abs(evk - float(imp_ch["kwh_ev"])) > 1e-9:
                imp_ch["kwh_ev"] = round(evk, 6)
                imp_ch["cost_ev"] = round(_ps.attribution_cost(new, "ev"), 6)
        return True
    except Exception as e:
        logger.warning("_refit_held_segments: %s: %s", block_start, e)
        return False


def _persist_block_segments(block_start: str, meter_id: str, imp_ch: dict, *,
                            stage: bool = False) -> None:
    """BL-27: write the priced segments for a just-priced MAIN import channel, so new /
    settled blocks carry `block_segments` alongside the imp_* columns (history is filled by
    the segment backfill). Segments mirror the columns during the migration; the readers
    then read segments. Derived from the channel so it stays in sync with whatever pricing
    just ran (overlay / IOG split / settlement). Best-effort — never blocks pricing.

    `stage` (step 5c, 10 Oct 2026): build the segments but do not write them — leave them on
    the channel as STAGED_SEGMENTS for the block write (BlockStore.append_block /
    append_block_replace) to commit in ITS transaction. Finalise and the settlement re-run
    stage: once the EV-split columns went, a segment write that failed while the block row
    landed would have left the split recorded nowhere."""
    if _store is None or not block_start or not imp_ch:
        return
    try:
        _segs = _build_block_segments(block_start, imp_ch)
        if stage:
            imp_ch[STAGED_SEGMENTS] = _segs
        else:
            _store.set_block_segments(block_start, meter_id, _segs)
    except Exception as e:
        # Best-effort by design (never blocks pricing) — but a swallowed miss leaves the block
        # unsegmented, which the presentation gate can amplify. Count + tag it so it is greppable
        # (SEGMENT-WRITE-FAIL) and observable rather than silent.
        global _SEGMENT_WRITE_FAILURES
        _SEGMENT_WRITE_FAILURES += 1
        logger.warning("SEGMENT-WRITE-FAIL: %s meter=%s (total this run: %d): %s",
                       block_start, meter_id, _SEGMENT_WRITE_FAILURES, e)


def _exc_ratio(imp_ch: dict, block_start: str, *, vat_fallback: bool = True):
    """A main import channel's ex-VAT / inc-VAT ratio: the ex-VAT rate this pricing run derived
    (`rate_exc`, where a pricing run set one), else the block's OWN stored pair (`cost_exc` /
    `cost`), else 1 / (1 + VAT on its day). None when none can be had.

    Step 5b (10 Oct 2026): `imp_rate_exc` is no longer stored or loaded, so on a channel read
    from the database `rate_exc` is absent and the pair speaks. The pair is the per-block
    ex-VAT authority (import spec §5.2, v15) — on a settled block it is the bill's own."""
    rate = float(imp_ch.get("rate") or 0.0)
    rexc = imp_ch.get("rate_exc")
    if rexc is not None and rate:
        return float(rexc) / rate
    cost, cexc = imp_ch.get("cost"), imp_ch.get("cost_exc")
    if cexc is not None and cost:
        return float(cexc) / float(cost)
    if not vat_fallback:
        return None
    try:
        return 1.0 / (1.0 + _store.vat_rate_at(block_start))
    except Exception:
        return None


def _carry_exc_to_cost(imp_ch: dict, ratio) -> None:
    """Keep a re-costed channel's ex-VAT with its new inc figure, at `ratio` (the block's own,
    taken BEFORE the re-cost). The settlement re-run moves kWh and £ to the settled figure; with
    no ex-VAT tariff to re-derive from, the loaded `cost_exc` was written back unchanged — stale
    against the new cost, hidden while `imp_rate_exc` was stored. On an account with a tariff
    schedule settle() then writes the VAT calendar's figure (engine step 4d, 4e)."""
    if ratio is None or imp_ch.get("cost") is None:
        return
    imp_ch["cost_exc"] = round(float(imp_ch["cost"]) * ratio, 6)
    if imp_ch.get("rate") is not None:
        imp_ch["rate_exc"] = round(float(imp_ch["rate"]) * ratio, 6)


def _build_block_segments(block_start: str, imp_ch: dict) -> list:
    """The priced segments for a just-priced MAIN import channel, unwritten — the seam's own
    where it emitted them, else reconstructed from the channel's columns. For a writer that
    commits them with the columns it writes (BL-79); _persist_block_segments writes them alone.
    """
    import pricing_segments as _ps
    _rate = float(imp_ch.get("rate") or 0.0)
    _ratio = _exc_ratio(imp_ch, block_start)
    # BL-27: prefer the FULL-fidelity segments the pricing seam emitted (all four bands
    # on a boundary block); apply the block's exc ratio here. Fall back to reconstructing
    # from the columns for non-dispatched / non-seam blocks (single house segment).
    _seam = imp_ch.get("segments")
    if _seam:
        def _exc(inc):
            return round(inc * _ratio, 6) if (_ratio is not None and inc is not None) else None
        segs = [_ps.Segment(round(float(k), 6), float(r), _exc(float(r)), band, attr)
                for (k, r, band, attr) in _seam]
    else:
        segs = _ps.segments_from_legacy(
            imp_kwh=float(imp_ch.get("kwh") or 0.0), imp_cost=float(imp_ch.get("cost") or 0.0),
            imp_rate=_rate, kwh_ev=imp_ch.get("kwh_ev"), cost_ev=imp_ch.get("cost_ev"),
            rate_ev=imp_ch.get("rate_ev"), ev_band=imp_ch.get("ev_band"),
            home_band=imp_ch.get("home_band"), exc_ratio=_ratio)
    return segs


_UNIT_SANITY_MAX_RATE_GBP = 3.0   # £/kWh ceiling — above this an INC rate is pence-not-pounds


def _sanitise_inc_units(imp_ch: dict, start: str) -> bool:
    """SAFETY GUARD (design 4.4.0 §4a): a main-import block must never carry INC figures in
    pence while exc stays in £. The Kraken live path resolves £ (`_kraken_rate_resolver` does
    pence/100), but a fallback that carries an un-normalised value (last-known-rate, a restore
    glitch) can slip a pence rate onto the block — leaving imp_rate/imp_cost 100x while
    imp_cost_exc stays correct £. That is exactly the 2026-08-18 capped-DB break: 4 blocks with
    imp_rate=6.9 (pence) and imp_cost_exc=0.26 (£). exc is the trusted reference and is already
    £; no domestic import unit rate approaches £3/kWh, so an inc rate above the ceiling is
    unambiguously a pence/pounds unit mix. Normalise every INC field (rate/cost + EV split) by
    /100 in ONE place, before persist, and WARN. Non-destructive: a plausible rate (<= ceiling)
    is left byte-identical, so healthy blocks are untouched. Idempotent (post-fix rate <= ceiling).
    """
    r = imp_ch.get("rate")
    if r is None or abs(r) <= _UNIT_SANITY_MAX_RATE_GBP:
        return False
    for k in ("rate", "cost", "cost_ev", "rate_ev", "cost_remainder"):
        v = imp_ch.get(k)
        if v is not None:
            imp_ch[k] = round(v / 100.0, 6)
    logger.warning(
        "UNIT-GUARD: %s import inc rate %.5f £/kWh implausible (> £%.1f/kWh) — pence-not-pounds; "
        "normalised all inc fields /100 (rate -> %.5f). exc left untouched (already £).",
        start, r, _UNIT_SANITY_MAX_RATE_GBP, imp_ch.get("rate"))
    return True


def _reprice_main_import_block(result: dict, start: str, meter_name: str,
                               meter: "dict | None" = None, *, persist: bool = True,
                               stage: bool = False) -> None:
    """What a MAIN import channel carries when its block is written: the rate it was priced at
    (the rate sensor, or the live schedule), its units checked, and its segments built from it.

    Engine step 4e (27 Oct 2026): the dispatch overlay, the IOG split and the tariff's ex-VAT went
    from here. Every block write is settled after it (4d), and settle() writes the band, the legs,
    the ex-VAT and the devices from the facts; what is left is the fallback for a half-hour
    settle() cannot price — an account with no tariff schedule (a rate sensor), whose figures stand
    as written: kWh × that rate, one house segment."""
    # SAFETY GUARD (§4a) — INC must be £, never pence. PASS 2 devices then inherit the £ rate.
    _sanitise_inc_units(result, start)
    if persist:
        _persist_block_segments(start, meter_name, result, stage=stage)


def _log_config_state(config: dict | None = None) -> None:
    """Emit a one-shot config-state dump at startup (design open-item 2).

    On a self-hosted add-on the LOG is the only window into a user's setup, so
    "rely on user reporting" only works if a report carries the config context.
    This logs the OVERLAY-relevant config that is known synchronously at startup.
    Tariff/MPAN/account are logged separately by the Kraken discovery step when
    it completes (async), so they are intentionally not duplicated here.

    Reports only GROUNDED values. Detection features not yet built (BCD-detected
    live-tile offload, explicit rate-sensor override, OHME charge-mode sensor
    path) are marked as a seam rather than printed as fabricated values — the log
    must never claim a state the code can't actually determine.
    """
    cfg = config if config is not None else {}
    mode = get_data_source_mode()
    # Is a main import/export read sensor configured? (cad-side presence)
    has_import = has_export = False
    for mcfg in (cfg.get("meters") or {}).values():
        if (mcfg.get("meta") or {}).get("sub_meter"):
            continue
        ch = mcfg.get("channels") or {}
        if (ch.get("import") or {}).get("read"):
            has_import = True
        if (ch.get("export") or {}).get("read"):
            has_export = True
    try:
        billing_source = _get_billing_source()
    except Exception:
        billing_source = "?"
    # Mini at startup-dump time: the reader is wired LATER in startup (after
    # device discovery), so we cannot yet report a definitive active/off here.
    # Report ELIGIBILITY honestly instead — a definitive `mini_provisional=...`
    # line is emitted once _maybe_setup_mini() resolves. Reporting the nominal
    # mode (== api+mini) would be WRONG: a plain `api` mode elevates to Mini at
    # runtime when a device is present, so mode alone can't tell the truth.
    if not mode_uses_api(mode):
        mini_note = "off (no API mode)"
    elif has_import:
        mini_note = "off (local import sensor authoritative)"
    else:
        mini_note = "pending (API mode, no local sensor — will probe for device)"
    logger.info(
        "config-state: mode=%s uses_api=%s mini=%s billing_source=%s "
        "import_sensor=%s export_sensor=%s",
        mode, mode_uses_api(mode), mini_note, billing_source,
        has_import, has_export)
    logger.info(
        "config-state: dispatch_overlay min_kwh=%.2f (overlay %s)",
        _DISPATCH_OVERLAY_MIN_KWH,
        "ACTIVE — dispatch capture + repricing runs" if mode_uses_api(mode)
        else "inactive — no API mode, dispatch path off")
    # BCD + OHME detection are reported by _detect_and_log_integrations() once
    # HA states have been fetched (a `config-state: detection ...` line, emitted
    # later in startup like mini_provisional). The remaining unbuilt feature is
    # the explicit rate-sensor override; mark only that as a seam here.
    logger.info(
        "config-state: rate-sensor-override pending — not yet implemented "
        "(bcd + ohme detection logged after HA state fetch)")


async def _detect_and_log_integrations(ha) -> dict:
    """Detect third-party HA integrations EMT can use and log the result.

    BCD (BottlecapDave Octopus): its `current_demand` sensor lets the live-power
    tile read off BCD instead of EMT polling the Mini (~60/hr saved). OHME: a
    charge-mode signal (official `ohme` select or dan-r binary) upgrades the
    optimistic off-peak default to VERIFIED smart-vs-boost. One REST round-trip,
    non-fatal; stored in _detected_integrations for the overlay + tile to read.
    Re-run each startup so a mid-life install is picked up.
    """
    global _detected_integrations
    from kraken_api_client import detect_bottlecapdave, detect_ohme_charge_mode
    bcd = {"found": False}
    ohme = {"found": False, "integration": None}
    try:
        states = await ha.get_all_states()
        bcd = detect_bottlecapdave(states or [])
        ohme = detect_ohme_charge_mode(states or [])
    except Exception as e:
        logger.warning("config-state: integration detection failed: %s", e)
    _detected_integrations = {"bcd": bcd, "ohme": ohme}

    # Preload the OHME charge-mode entity so the capture tick's get_state() has a
    # value before the entity's first state_changed event. After that, the WS
    # listener keeps it fresh automatically (it caches every state_changed).
    if ohme.get("found") and ohme.get("charge_mode_entity"):
        try:
            await ha.preload_states([ohme["charge_mode_entity"]])
        except Exception as e:
            logger.warning("config-state: ohme entity preload failed: %s", e)
    logger.info(
        "config-state: detection bcd=%s bcd_live_power=%s ohme_charge_mode=%s "
        "(integration=%s) ohme_path=%s",
        bool(bcd.get("found")),
        "yes" if bcd.get("demand_sensor") else "no",
        bool(ohme.get("found")),
        ohme.get("integration") or "none",
        "verified" if ohme.get("found") else "optimistic")
    return _detected_integrations


def _is_gap_zero_register(block: dict, channel: dict) -> bool:
    """v4 #493: a gap-filled block's channel stored with registers 0.0 → 0.0 — the shape
    build_gap_blocks writes for a channel it had no readings for. Those zeros keep the
    block's shape; they are NOT a meter reading, and a reader that took one as the
    opening register booked the whole lifetime register (from 0 up to the real
    reading) as one interval. A LIVE block's 0.0 can be real (a daily-reset sensor at
    midnight), so only a gap block's counts."""
    if not block.get("interpolated"):
        return False
    try:
        return (float(channel.get("read_start") or 0.0) == 0.0
                and channel.get("read_end") is not None
                and float(channel["read_end"]) == 0.0)
    except (TypeError, ValueError):
        return False


def _reseed_opener_after_short_restart(last_block: dict, current_block: dict) -> list:
    """Restore the in-progress block's opener after a within-block restart.

    On restart the rogue-total guard clears the in-progress block's reads. The
    block's channel structure is reconstructed FROM those reads, so once they're
    gone load_current_block() returns the block with meters={} — no channels at
    all. We therefore rebuild the opener by iterating LAST_block's channels (each
    carries the durable read_end = this block's true opening register) and writing
    a single seed read into current_block, CREATING the meter/channel as needed.

    Without this the block re-seeds from a late post-restart read and UNDER-COUNTS,
    measuring only the tail (observed live: Mini blocks finalising at ~0.23 kWh
    when ~3.5 kWh really flowed, because each restart dropped the opener).

    Safe because:
      - acts only when last_block.end == current_block.start (contiguous — the
        no-gap case; gap-fill owns seeding when there's a gap, so we return []);
      - seeds a channel only when current_block has no live reads for it (never
        clobbers reads that survived a warm restart);
      - read_end is the durable finalised register, not a volatile in-block read,
        so it cannot reintroduce a rogue total.

    Mutates current_block in place; returns human-readable seeded descriptors
    (empty list if nothing seeded). Caller persists + logs.
    """
    if not last_block or not current_block:
        return []
    # Contiguity gate: only when the current block immediately follows last_block.
    if (last_block.get("end") or None) != (current_block.get("start") or None):
        return []
    seed_ts = current_block.get("start")
    if not seed_ts:
        return []
    reseeded = []
    cur_meters = current_block.setdefault("meters", {})
    for mn, lb_m in (last_block.get("meters") or {}).items():
        for cn, lb_ch in (lb_m.get("channels") or {}).items():
            read_end = lb_ch.get("read_end")
            if read_end is None or _is_gap_zero_register(last_block, lb_ch):
                continue        # v4 #493: a gap block's 0.0 is not an opening register
            cur_ch = (cur_meters.setdefault(mn, {})
                                 .setdefault("channels", {})
                                 .setdefault(cn, {}))
            if cur_ch.get("reads"):
                continue  # live reads survived — never clobber
            cur_ch["reads"] = [{"ts": seed_ts, "value": float(read_end)}]
            reseeded.append("%s/%s=%.3f" % (mn, cn, float(read_end)))
    return reseeded


def _kraken_rate_resolver(channel_name: str, block_start: str):
    """Sync resolver the drain passes to the re-run. Returns £/kWh or None.

    Reads the cached schedule for the channel and resolves the rate at
    block_start, converting the API's pence/kWh to £/kWh. Pure/in-memory — no
    network. Returns None when no schedule or no covering period exists, so the
    re-run leaves the block flagged for the tooling rather than guessing.
    """
    sched = _kraken_rate_schedules.get(channel_name)
    if sched is None or sched.is_empty():
        return None
    pence = sched.resolve(block_start)
    if pence is None:
        return None
    return round(pence / 100.0, 6)   # pence/kWh → £/kWh


def _kraken_standing_resolver(block_start: str):
    """Resolve the standing charge (£/day) at block_start from the cached
    schedule, or None. pence/day → £/day. Pure/in-memory."""
    sched = _kraken_standing_schedule
    if sched is None or sched.is_empty():
        return None
    pence = sched.resolve(block_start)
    if pence is None:
        return None
    return round(pence / 100.0, 6)

def setup():
    """Call once from main() before starting any tasks."""
    global _engine_loop_lock
    _engine_loop_lock = asyncio.Lock()
    ensure_dir(DATA_DIR)
    _remove_stale_chart_files()


# The pre-rendered chart files v4 and the first v5 builds wrote next to the database.
# Nothing writes or serves them now (C, 30 Sep 2026: the Billing tab and the heatmap
# are drawn in the browser from /api/energy); on an install that had them they would
# only take space — the Billing page for three years of history was 36 MB.
_STALE_CHART_FILES = ("daily_usage.html", "net_heatmap.html")


def _remove_stale_chart_files():
    for name in _STALE_CHART_FILES:
        path = os.path.join(DATA_DIR, name)
        try:
            if os.path.exists(path):
                os.remove(path)
                logger.info("setup: removed the stale pre-rendered chart %s", name)
        except OSError as e:
            logger.warning("setup: could not remove %s: %s", name, e)


_engine_pause_reason: "str | None" = None
_engine_paused_since: "float | None" = None


def pause_engine(reason: str = None):
    """Stop the live tick.

    This is the only thing in the system that can stop live capture, and it is
    worth being precise about what it costs. `_engine_tick` opens with
    `if _engine_paused: return` — a paused tick is SKIPPED, not queued. Energy is
    not lost by that, because blocks store register readings
    (`imp_read_start`/`imp_read_end`) rather than samples, so the delta across the
    gap is still correct. What a pause can cost is a block BOUNDARY: if one falls
    inside the pause, its closing register is estimated by `interpolate_value()`
    and the block is stamped `interpolated = 1`.

    That price was previously paid silently — the log said "engine: paused" and
    nothing else. `reason` and the duration are recorded so a pause is
    attributable, and `resume_engine()` says when a boundary was crossed.

    B §4.0: the live tick is the one coordinator member that is never deferred, so
    this function belongs to the coordinator rather than being a module flag four
    callers set by hand.
    """
    global _engine_paused, _engine_pause_reason, _engine_paused_since
    import time as _t
    _engine_paused = True
    _engine_pause_reason = reason or "unspecified"
    _engine_paused_since = _t.monotonic()
    logger.info("engine: paused (%s)", _engine_pause_reason)


def resume_engine():
    """Restart the live tick, and say what the pause cost."""
    global _engine_paused, _engine_pause_reason, _engine_paused_since
    import time as _t
    held = (_t.monotonic() - _engine_paused_since) if _engine_paused_since else 0.0
    why = _engine_pause_reason or "unspecified"
    _engine_paused = False
    _engine_pause_reason = None
    _engine_paused_since = None
    # A pause shorter than a block cannot straddle a boundary; one longer than a
    # block certainly does. In between it depends where the boundary fell, which
    # this cannot know without the block clock — so it reports "may have".
    try:
        block_s = get_block_minutes() * 60
    except Exception:
        block_s = 1800
    if held >= block_s:
        logger.warning("engine: resumed after %.0fs (%s) — longer than one %ds "
                       "block, so at least one boundary was interpolated",
                       held, why, block_s)
    elif held > 0:
        logger.info("engine: resumed after %.1fs (%s) — a boundary inside that "
                    "span would have been interpolated", held, why)
    else:
        logger.info("engine: resumed (%s)", why)


def engine_pause_state() -> dict:
    """Who paused the tick and for how long — for the status surface and for a
    coordinator that needs to know whether member 23 is running."""
    import time as _t
    return {"paused": _engine_paused, "reason": _engine_pause_reason,
            "held_s": round(_t.monotonic() - _engine_paused_since, 1)
                      if _engine_paused_since else 0.0}


# ── Engine-loop lag ─────────────────────────────────────────────────────────
#
# The one way left for a read to wait behind a write. Since workstream A a read
# cannot block in SQLite (WAL readers never wait on the writer), so a request can
# only be held up by work that must run ON the engine loop — and anything on the
# loop waits for whatever holds it. Second Pi soak (29 Sep 2026): the re-price
# sweep held it ~14 s per chunk, and every loop hop waited that long. The live
# tick and the HA WebSocket heartbeat wait the same way.
#
# Latency at the web surface cannot separate that from a heavy read slowed by a
# saturated Pi (the soak tried). This measures it at the source: a coroutine that
# asks to wake every _LOOP_LAG_INTERVAL_S and records how late it actually did.
_LOOP_LAG_INTERVAL_S = 0.5
_LOOP_LAG_WARN_S = 1.0
_loop_lag = {"max_s": 0.0, "max_at": None, "last_s": 0.0, "warnings": 0,
             "since": None}
# Watchdog: a THREAD that notices the monitor has stopped ticking and logs every
# thread's stack, so a stall names the code that caused it. Third Pi soak (30 Sep
# 2026): the loop froze 445 s after a restore while charts rendered — in a worker
# thread, with its own connection, not on the loop — so the log alone could not
# say what held it. The dumps settle that.
_WATCHDOG_STALL_S = 5.0          # dump once the loop has not ticked for this long
_WATCHDOG_REDUMP_S = 30.0        # and again while it stays stalled
_loop_heartbeat = {"t": None, "thread": None}


async def engine_loop_lag_monitor() -> None:
    """Log `engine loop lag: X s` at WARNING whenever the loop was held more than
    _LOOP_LAG_WARN_S past a wake-up; keep the worst in `engine_loop_lag()`.
    Started once from main.py, beside the Kraken poll task — never from
    engine_startup, which re-runs on every restore and config save."""
    import asyncio as _a
    import threading as _th
    _loop_lag["since"] = datetime.now(timezone.utc).isoformat()
    _loop_heartbeat.update(t=time.monotonic(), thread=_th.get_ident())
    _th.Thread(target=_loop_watchdog, name="loop-watchdog", daemon=True).start()
    logger.info("engine loop lag monitor started (interval %.1f s, warn above %.1f s, "
                "watchdog stack dump after %.0f s)",
                _LOOP_LAG_INTERVAL_S, _LOOP_LAG_WARN_S, _WATCHDOG_STALL_S)
    while True:
        t0 = time.monotonic()
        await _a.sleep(_LOOP_LAG_INTERVAL_S)
        _loop_heartbeat["t"] = time.monotonic()
        lag = max(0.0, time.monotonic() - t0 - _LOOP_LAG_INTERVAL_S)
        _record_loop_lag(lag)


def _loop_watchdog() -> None:
    """Runs forever in its own daemon thread; see `_watchdog_check`.

    CAVEAT, recorded because it is the hypothesis being tested: if a C call holds
    the GIL for the whole stall, this thread cannot run either, and the dump lands
    when the GIL is released — at the END of the stall, not during it. It still names
    what every thread was doing then."""
    last_dump = [None]
    while True:
        time.sleep(1.0)
        try:
            _watchdog_check(last_dump)
        except Exception as e:                       # never let the watchdog die
            logger.debug("loop watchdog: %s", e)


def _watchdog_check(last_dump: list, now: float = None) -> bool:
    """Dump every thread's stack if the engine loop has not ticked for
    _WATCHDOG_STALL_S; again every _WATCHDOG_REDUMP_S while it stays stalled.
    Returns True when it dumped."""
    import sys as _sys
    import threading as _th
    import traceback as _tb
    hb = _loop_heartbeat["t"]
    if hb is None:
        return False
    now = time.monotonic() if now is None else now
    stalled = now - hb
    if stalled < _WATCHDOG_STALL_S:
        last_dump[0] = None
        return False
    if last_dump[0] is not None and now - last_dump[0] < _WATCHDOG_REDUMP_S:
        return False
    last_dump[0] = now
    names = {t.ident: t.name for t in _th.enumerate()}
    frames = _sys._current_frames()
    engine_tid = _loop_heartbeat["thread"]
    out = ["engine loop stalled: %.1f s — thread stacks follow (engine loop = %s)"
           % (stalled, names.get(engine_tid, engine_tid))]
    order = sorted(frames, key=lambda tid: (tid != engine_tid, names.get(tid, "")))
    for tid in order:
        if names.get(tid) == "loop-watchdog":
            continue
        tag = "ENGINE LOOP" if tid == engine_tid else "thread"
        out.append("  --- %s %s (%s)" % (tag, names.get(tid, "?"), tid))
        for fs in _tb.extract_stack(frames[tid])[-12:]:
            out.append("    %s:%d in %s: %s" % (os.path.basename(fs.filename), fs.lineno,
                                                fs.name, (fs.line or "").strip()[:100]))
    logger.warning("\n".join(out))
    return True


def _record_loop_lag(lag: float) -> None:
    _loop_lag["last_s"] = round(lag, 3)
    if lag > _loop_lag["max_s"]:
        _loop_lag["max_s"] = round(lag, 3)
        _loop_lag["max_at"] = datetime.now(timezone.utc).isoformat()
    if lag > _LOOP_LAG_WARN_S:
        _loop_lag["warnings"] += 1
        logger.warning("engine loop lag: %.2f s (loop held past a %.1f s wake-up)",
                       lag, _LOOP_LAG_INTERVAL_S)


def engine_loop_lag() -> dict:
    """The monitor's readings: worst lag and when, the latest, and how many times
    it crossed the warning line since start."""
    return dict(_loop_lag)


@contextmanager
def engine_paused(reason: str):
    """Pause for the duration of a block, resuming on every path.

    The four callers today each hand-roll this: three with `try/finally`, one with
    a resume at each of three exits. All correct, all separately arrived at —
    which is the pattern that produced seventeen coordination primitives across
    four mechanisms. One shape instead.
    """
    pause_engine(reason)
    try:
        yield
    finally:
        resume_engine()


async def drain_tick() -> None:
    """Wait for any in-flight capture/finalise tick to finish.

    Named for what it does. This replaced `run_exclusive(lambda: None)`, whose
    single caller used a mutual-exclusion primitive as a barrier and never ran
    anything under it — so the primitive read as load-bearing while doing nothing
    but this.

    Acquiring `_engine_loop_lock` blocks until the running tick releases it; the
    lock is dropped again on return, so this DRAINS the in-flight tick rather than
    holding the next one off. That is all the caller needs: `pause_engine()`
    already stops the next tick, but not one already running.

    (`run_exclusive` itself is gone — B §6 q6. It ran its function on an executor
    thread, which A §3.3 forbids; it excluded only the tick, not the fourteen
    other loop writers; and what closed that gap was a docstring telling callers
    to pause and drain first.)
    """
    if _engine_loop_lock is None:
        return
    async with _engine_loop_lock:
        return


def reset_store():
    """
    Close the current BlockStore and reopen it from BLOCKS_DB_PATH.
    Called after a DB restore/import so the engine picks up the new file.
    Must be called while the engine is paused.
    """
    global _store
    import store_access as _sa
    if not _sa.on_engine_thread():
        # The new connection would belong to the caller's thread, and the engine
        # could never use it. Hop onto the loop (web/server._reset_engine_store).
        raise _sa.WrongThread("reset_store() must run on the engine thread")
    try:
        if _store is not None:
            try:
                _store.close()
            except Exception:
                pass
            _store = None
        _store = open_block_store(BLOCKS_DB_PATH)
        # Every request thread's ReadStore is still open on the file that was just
        # replaced; have each one reopen on its next read (store_access.invalidate).
        import store_access as _sa
        _sa.invalidate()
        logger.info("engine: store reset — reopened %s", BLOCKS_DB_PATH)
    except Exception as e:
        logger.error("engine: reset_store failed: %s", e)
        raise


def _reader():
    """The store to READ engine state from, on whichever thread is asking.

    On the engine thread, the engine's own `_store`. Anywhere else — a request
    thread calling `get_data_source_mode()`, `load_config()` and the like — the
    per-thread `store_access.store()`. The engine's connection may only be used on
    the thread that opened it (A §5.5); touched from a request thread it raises,
    and these accessors each swallowed that and returned their default. Measured on
    the first Pi soak (29 Sep 2026): the database said `data_source_mode='api'`, the
    settings page said "unset" and offered to reactivate a supplier that was
    connected; /api/kraken-config reported no account on a stamped database.

    Falls back to `_store` before `bind()` (tests, pre-startup), where there is no
    other thread's connection to protect.
    """
    import store_access as _sa
    if _sa._db_path is None or _sa.on_engine_thread():
        return _store
    return _sa.store()


# ─────────────────────────────────────────────────────────────────────────────
# IO helpers
# ─────────────────────────────────────────────────────────────────────────────

def append_block(block: dict):
    """finalise_block's write: every meter's channels as HA reported them go to `ha_reads` with
    the block (soft delete design §2) — now, before settlement can put Octopus's kWh on it. Then
    the half-hour is settle()'s (engine step 4d)."""
    get_store().append_block(block, ha_meters=list(block.get("meters") or {}))
    _settle_written(block)


def append_block_replace(block: dict, **kw):
    get_store().append_block_replace(block, **kw)
    _settle_written(block)


def _settle_written(block: dict) -> None:
    """A block was written — finalise, a gap block, a provisional amendment, a device rebuild, the
    PASS 2 re-run: its main half-hour is settle()'s from the facts it now holds, written only where
    it differs (engine step 4d). The writer's own pricing stands where settle() cannot price it (no
    tariff schedule: a rate-sensor account) or the half-hour is corrected. Never raises."""
    if "electricity_main" not in (block.get("meters") or {}) or not block.get("start"):
        return
    try:
        r = settle_half_hour(get_store(), block["start"])
    except Exception as e:
        logger.warning("settle failed for %s after its write (the writer's figures stand): %s",
                       block.get("start"), e)
        return
    if r.get("written"):
        logger.debug("settle: %s %s after its write — %s", block["start"], r["status"],
                     ", ".join(r["parts"]))


# ─────────────────────────────────────────────────────────────────────────────
# Backup to /share
# ─────────────────────────────────────────────────────────────────────────────

_backup_in_progress = False


def _backup_to_share():
    """Backup data files to SHARE_BACKUP_DIR — OFF the event loop. Once a day, from
    _maybe_daily_share_backup (#67, 4 Oct 2026). It ran after EVERY finalise: on the
    Pi it took ~80 s each time, and in those windows (4% of a night) fell 18 of the
    night's 25 loop-lag warnings, single-row commits taking 5–27 s on the SD card.

    Root cause of the ~every-30-min "restart" (diagnosed 2026-07): this ran
    SYNCHRONOUSLY on the finalise path and `store.backup()` copies the whole DB to
    /share (frequently a slow network mount). At tens of MB it took ~40s, stalling
    the asyncio loop — so the HA WebSocket heartbeat timed out, HA dropped the
    connection, and the reconnect re-ran the full engine_startup. It only LOOKED
    like a restart.

    Now it runs in a worker thread with its OWN read connection: the engine's
    `self._conn` is bound to the loop thread and SQLite forbids cross-thread use,
    while WAL lets a separate reader snapshot the DB consistently while the engine
    keeps writing. An overlap guard skips a block if the previous (slow) backup is
    still going, so a slow /share can't pile up backups."""
    global _backup_in_progress
    if _backup_in_progress:
        logger.info("_backup_to_share: previous backup still running — skipping")
        return
    try:
        db_path = get_store()._path
    except Exception:
        return

    def _work():
        global _backup_in_progress
        import sqlite3 as _sqlite
        try:
            ensure_dir(SHARE_BACKUP_DIR)
            src = _sqlite.connect(db_path)          # fresh connection in THIS thread
            try:
                dst = _sqlite.connect(f"{SHARE_BACKUP_DIR}/blocks.db")
                try:
                    # ONE step (#56, #67): a stepped copy restarts whenever the
                    # engine writes, which it does every tick.
                    src.backup(dst, pages=-1)
                finally:
                    dst.close()
            finally:
                src.close()
            for filename in ("meters_config.json",):  # current_block state is in DB
                src_path = f"{DATA_DIR}/{filename}"
                if os.path.exists(src_path):
                    shutil.copy2(src_path, f"{SHARE_BACKUP_DIR}/{filename}")
            logger.info("_backup_to_share: backup written to %s", SHARE_BACKUP_DIR)
        except Exception as e:
            logger.warning("_backup_to_share: failed: %s", e)
        finally:
            _backup_in_progress = False

    _backup_in_progress = True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.run_in_executor(None, _work)           # off the loop — never stalls the tick
    else:
        _work()                                     # no loop (sync/test) — inline


# The daily share copy (#67): local early hours, starting only in the quarter-hour
# after :15 or :45 — clear of the :00/:30 finalise either side of a ~1–3 min copy.
_SHARE_BACKUP_HOUR = 3
_SHARE_BACKUP_STARTS = (15, 45)       # minute of the hour a start window opens
_SHARE_BACKUP_SPAN = 10               # minutes each start window stays open
_share_backup_tried_day = None        # one attempt per local day per process


def _maybe_daily_share_backup(now_utc=None) -> bool:
    """Start the daily share copy if it is due. Called from every engine tick; a
    clock comparison until it is due. Due means: inside a start window at the local
    _SHARE_BACKUP_HOUR, no copy yet today (the file's own time says so, which
    survives a restart), not tried already today, and nothing heavy running — the
    re-price sweep, an import, a delete or a restore, each of which writes enough to
    make the copy and the job slow each other. Skipped windows try again in the next
    one; a day whose windows are all busy goes without. Returns True if started."""
    global _share_backup_tried_day
    if _backup_in_progress:
        return False
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(_iog_site_tz())
    except Exception:
        tz = timezone.utc
    now = (now_utc or datetime.now(timezone.utc).replace(tzinfo=None)) \
        .replace(tzinfo=timezone.utc).astimezone(tz)
    if now.hour != _SHARE_BACKUP_HOUR or not any(
            st <= now.minute < st + _SHARE_BACKUP_SPAN for st in _SHARE_BACKUP_STARTS):
        return False
    today = now.date()
    if _share_backup_tried_day == today:
        return False
    try:
        _p = f"{SHARE_BACKUP_DIR}/blocks.db"
        if os.path.exists(_p) and datetime.fromtimestamp(os.path.getmtime(_p), tz).date() == today:
            _share_backup_tried_day = today
            return False
    except Exception:
        pass
    if settle_history_in_progress() or delete_in_progress() or any(
            _coord.running(j) for j in ("import", "delete", "restore")):
        return False
    _share_backup_tried_day = today
    logger.info("_maybe_daily_share_backup: starting the daily share copy")
    _backup_to_share()
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Time helpers
# ─────────────────────────────────────────────────────────────────────────────

def floor_to_block(dt: datetime, block_minutes: int = BLOCK_MINUTES) -> datetime:
    minute = (dt.minute // block_minutes) * block_minutes
    return dt.replace(minute=minute, second=0, microsecond=0)

def iso(dt: datetime) -> str:
    return dt.isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Currency detection
# ─────────────────────────────────────────────────────────────────────────────

# Map ISO 4217 currency codes to symbols
_CURRENCY_SYMBOLS = {
    "GBP": "£", "USD": "$", "EUR": "€", "AUD": "A$", "CAD": "C$",
    "NZD": "NZ$", "SGD": "S$", "HKD": "HK$", "JPY": "¥", "CNY": "¥",
    "SEK": "kr", "NOK": "kr", "DKK": "kr", "CHF": "Fr", "INR": "₹",
    "ZAR": "R", "BRL": "R$", "MXN": "$", "TRY": "₺", "KRW": "₩",
}


def detect_currency_symbol(unit_of_measurement: str) -> str:
    """
    Extract a currency symbol from a HA sensor unit string.
    Examples: "GBP/kWh" → "£", "USD/kWh" → "$", "EUR/day" → "€"
    Falls back to the raw currency code if not in the lookup table,
    or "¤" (generic currency sign) if nothing can be parsed.
    """
    if not unit_of_measurement:
        return "¤"
    # Strip trailing unit suffix e.g. "/kWh", "/MWh", "/day"
    code = unit_of_measurement.split("/")[0].strip().upper()
    return _CURRENCY_SYMBOLS.get(code, code) if code else "¤"


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    """
    Load meter configuration from the normalised DB (blocks.db) config_periods table.
    The DB is the single source of truth. meters_config.json is only used before
    the DB is open at startup on a truly fresh install with no DB file.
    """
    global _store
    db_exists = os.path.exists(os.path.join(DATA_DIR, "blocks.db"))

    if _store is not None:
        try:
            # BlockStore.get_current_config_period_id() is this query, character
            # for character. It was re-implemented here against the raw
            # connection rather than called.
            cp_id = _reader().get_current_config_period_id()
            if cp_id is not None:
                return _reader().config_from_db(cp_id)
            # DB open but no config period — fresh DB
            if db_exists:
                logger.warning("load_config: DB open but no active config period")
                return {"meters": {}}
        except Exception as _e:
            logger.error("load_config: DB read failed: %s", _e)
            if db_exists:
                logger.error("load_config: DB exists — not falling back to JSON")
                return {"meters": {}}

    # Pre-startup: DB not open yet
    if db_exists:
        # DB exists but store not open yet — don't use stale JSON
        logger.warning("load_config: store not open, DB exists — returning empty config")
        return {"meters": {}}

    # True fresh install: no DB, use meters_config.json
    logger.info("load_config: no DB found, loading from meters_config.json")
    return load_json(CONFIG_PATH, {"meters": {}})


def get_block_minutes() -> int:
    """Read block_minutes from the main meter meta — defaults to 30."""
    cfg = load_config()
    for meter_id, meter in (cfg.get("meters") or {}).items():
        bm = (meter.get("meta") or {}).get("block_minutes")
        if bm:
            return int(bm)
    return BLOCK_MINUTES


# ─────────────────────────────────────────────────────────────────────────────
# Block lifecycle helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_block_window(now: datetime, block_minutes: int = BLOCK_MINUTES):
    start = floor_to_block(now, block_minutes)
    return start, start + timedelta(minutes=block_minutes)


def create_block(start: datetime, end: datetime, block_minutes: int = BLOCK_MINUTES,
                 seed_meters: bool = False) -> dict:
    block = {
        "start":         iso(start),
        "end":           iso(end),
        "block_minutes": block_minutes,
        "meters":        {},
        "interpolated":  False,
    }
    # In API/Mini mode there are no local read sensors, so capture_samples adds
    # no reads — but the block still needs a meter SHELL (meter present, meta
    # set, empty channels) so finalise_block doesn't bail with "nothing to
    # finalise" and the Mini boundary read / DCC settlement have a block to
    # attach to. Seed it from config.
    if seed_meters:
        try:
            cfg = load_config()
            for meter_id, meter_cfg in (cfg.get("meters") or {}).items():
                meta = meter_cfg.get("meta", {}) or {}
                # Skip retired sub-meters.
                if meta.get("sub_meter") and meta.get("retired_at"):
                    continue
                channels = {}
                for channel_id in (meter_cfg.get("channels") or {}):
                    channels[channel_id] = {"reads": [], "rates": []}
                block["meters"][meter_id] = {
                    "meta": meta, "channels": channels, "interpolated": False,
                }
        except Exception as e:
            logger.warning("create_block: seed_meters failed: %s", e)
    return block


def interpolate_value(pre_read: dict, post_read: dict, target_dt: datetime) -> dict:
    pre_ts         = datetime.fromisoformat(pre_read["ts"])
    post_ts        = datetime.fromisoformat(post_read["ts"])
    window_seconds = (post_ts - pre_ts).total_seconds()

    if window_seconds <= 0:
        logger.warning("interpolate_value: zero/negative window, returning pre_read value")
        return {"value": pre_read["value"], "ts": target_dt.isoformat(), "interpolated": True}

    fraction           = max(0.0, min(1.0, (target_dt - pre_ts).total_seconds() / window_seconds))
    interpolated_value = pre_read["value"] + fraction * (post_read["value"] - pre_read["value"])
    result             = round(interpolated_value, 3)

    logger.info(
        "interpolate_value: %.3f → %.3f at %s fraction=%.4f result=%.3f",
        pre_read["value"], post_read["value"], target_dt.isoformat(), fraction, result,
    )
    return {"value": result, "ts": target_dt.isoformat(), "interpolated": True}


def detect_gap(last_read_ts: str | None, now: datetime, block_minutes: int = BLOCK_MINUTES) -> list:
    if not last_read_ts:
        return []

    last_dt        = datetime.fromisoformat(last_read_ts)
    last_block_end = floor_to_block(last_dt, block_minutes) + timedelta(minutes=block_minutes)
    current_start  = floor_to_block(now, block_minutes)

    missing      = []
    window_start = last_block_end
    while window_start < current_start:
        window_end = window_start + timedelta(minutes=block_minutes)
        missing.append((window_start, window_end))
        window_start = window_end

    if missing:
        logger.warning(
            "detect_gap: %d missing blocks from %s to %s",
            len(missing), iso(last_block_end), iso(current_start),
        )
    return missing


def extract_last_reads(block: dict):
    """
    Extract the last known read and rate from each channel of a block dict.
    Returns (reads, rates) where both are {meter: {channel: {"ts": ..., "value": ...}}}.
    Both must be dicts-of-dicts so save_current_block can call r.get("ts")/r.get("value"),
    and so detect_gap gets a valid timestamp for pre_ts (gap fill anchor).

    Handles two block shapes:
    - Live current_block: channels have "reads" list of {"ts", "value"} dicts
    - Finalised DB block (_row_to_block): channels have "read_end" float + block "end" timestamp
    """
    from datetime import datetime, timezone as _tz
    _now_iso  = datetime.now(_tz.utc).isoformat()
    # Use block end time as timestamp anchor for finalised DB blocks
    block_end_iso = block.get("end") or _now_iso
    reads = {}
    rates = {}
    for meter_name, meter_data in block.get("meters", {}).items():
        reads[meter_name] = {}
        rates[meter_name] = {}
        for channel_name, channel in meter_data.get("channels", {}).items():
            channel_reads = channel.get("reads", [])
            channel_rates = channel.get("rates", [])

            if channel_reads:
                last_read = channel_reads[-1]
                if isinstance(last_read, dict):
                    reads[meter_name][channel_name] = last_read
                else:
                    reads[meter_name][channel_name] = {"ts": block_end_iso, "value": float(last_read)}
            elif channel.get("read_end") is not None and not _is_gap_zero_register(block, channel):
                # Finalised DB block — read_end is the last sensor value, block end is the timestamp.
                # v4 #493: not a gap block's 0.0 → 0.0 register — the next gap treats that
                # channel as missing (settlement fills the main meter; a device books
                # nothing) instead of opening from zero.
                reads[meter_name][channel_name] = {
                    "ts":    block_end_iso,
                    "value": float(channel["read_end"]),
                }

            if channel_rates:
                last_rate = channel_rates[-1]
                if isinstance(last_rate, dict):
                    rates[meter_name][channel_name] = {
                        "ts":    last_rate.get("ts", block_end_iso),
                        "value": float(last_rate.get("value", 0)),
                    }
                else:
                    rates[meter_name][channel_name] = {"ts": block_end_iso, "value": float(last_rate)}
            else:
                # Fallback: rate stored directly on channel (finalised DB block)
                rate = channel.get("rate") or channel.get("rate_used")
                if rate:
                    rates[meter_name][channel_name] = {"ts": block_end_iso, "value": float(rate)}
    return reads, rates


# ─────────────────────────────────────────────────────────────────────────────
# Gap marker helpers
# ─────────────────────────────────────────────────────────────────────────────

def _rate_value(rate_entry) -> float:
    """Unwrap a rate entry that may be a float or a {"ts":..., "value":...} dict."""
    if isinstance(rate_entry, dict):
        return float(rate_entry.get("value", 0.0))
    return float(rate_entry or 0.0)


def set_gap_marker(block: dict, pre_reads: dict, last_known_rates: dict,
                   last_block_start: str | None = None):
    block["_gap_marker"] = {
        "detected_at":      datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "pre_reads":        pre_reads,
        "last_known_rates": last_known_rates,
        "last_block_start": last_block_start,
    }
    logger.info("set_gap_marker: stored at %s last_block_start=%s",
                block["_gap_marker"]["detected_at"], last_block_start)


def clear_gap_marker(block: dict):
    if "_gap_marker" in block:
        block.pop("_gap_marker")
        logger.info("clear_gap_marker: cleared")


def has_gap_marker(block: dict) -> bool:
    return "_gap_marker" in block


# ─────────────────────────────────────────────────────────────────────────────
# Sensor reading  (replaces PyScript state.get())
# ─────────────────────────────────────────────────────────────────────────────

def read_sensor(ha: HAClient, entity_id: str, use_cache: bool = True) -> float | None:
    try:
        val = ha.get_state(entity_id)
        if val in ("unknown", "unavailable", None):
            # An unavailable/unknown sensor is a GAP, not a value. Never substitute a
            # cached number for a cumulative register: the cache can be stale (a
            # dropped/restarting integration briefly surfaces an old state), and a
            # stale-low register read is booked as a phantom delta downstream — the
            # cause of the 2026-07-21 house_battery 48.93 kWh half-hour, where a
            # 4-day-old reading (6259.77) resurfaced during a sensor dropout. Return
            # None so the gap machinery brackets and interpolates it honestly.
            logger.warning("read_sensor: %s='%s' — no reading (gap)", entity_id, val)
            return None
        val = float(val)
        _last_known_sensor_values[entity_id] = val
        return val
    except (ValueError, TypeError) as e:
        logger.warning("read_sensor: cannot cast %s to float — %s", entity_id, e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Block rollover
# ─────────────────────────────────────────────────────────────────────────────

def ensure_correct_block(ha: HAClient, current_block: dict, now: datetime,
                         last_known_rates: dict | None = None) -> dict:
    start, end = get_block_window(now, block_minutes=int(get_block_minutes()))

    # Reload from store — a concurrent tick (e.g. gap fill) may have saved
    # a more recent current_block since the caller loaded theirs.
    fresh = _store.load_current_block()
    if fresh and fresh.get("start"):
        fresh_start = datetime.fromisoformat(fresh["start"])
        if fresh_start != datetime.fromisoformat(current_block.get("start", "")):
            logger.info("ensure_correct_block: current_block updated by concurrent tick (%s → %s), using fresh",
                        current_block.get("start"), fresh["start"])
            current_block = fresh

    if not current_block or not current_block.get("start"):
        # Don't create the first block until sensors are configured.
        # If no import sensor is set we're in the pre-wizard state —
        # the block_minutes value is the default (30) which may not match
        # what the wizard will set. Creating a block now would cause a
        # block-size mismatch and racing after wizard saves.
        cfg = load_config()
        has_sensors = any(
            (meter.get("channels") or {}).get("import", {}).get("read")
            for meter in (cfg.get("meters") or {}).values()
            if not (meter.get("meta") or {}).get("sub_meter")
        )
        # API/Mini mode has no local read sensor by design — blocks are
        # populated by the Mini boundary reader (provisional) and DCC
        # settlement. Block formation must still proceed once an API mode is
        # configured, with a seeded meter shell so the block can finalise and
        # fire the boundary. (CAD without sensors stays pre-wizard: no block.)
        api_mode = False
        try:
            api_mode = mode_uses_api()
        except Exception:
            api_mode = False
        # The API arm must ALSO require configuration, not just a mode. The
        # setup wizard writes the mode BEFORE it writes the meters
        # (POST /api/data-source-mode, then POST /api/billing-source, then
        # POST /api/config), and the 10s engine loop is already running --
        # main.py starts it unconditionally, it is merely inert until this
        # gate opens. A tick landing in that window saw api_mode True against
        # an empty config and created a block anyway, with the DEFAULT
        # block_minutes (no meter meta to read yet) and an empty seeded shell
        # (nothing to seed FROM) -- exactly the block-size mismatch and
        # wizard-save race the CAD arm above exists to prevent. Measured in
        # tests/test_first_run_gate.py.
        has_main_meter = any(
            not (meter.get("meta") or {}).get("sub_meter")
            for meter in (cfg.get("meters") or {}).values()
        )
        if not has_sensors and not (api_mode and has_main_meter):
            return current_block  # pre-wizard / CAD-without-sensors: no block yet
        logger.info("Creating first block %s%s", iso(start),
                    " (API mode — seeded meter shell)" if (api_mode and not has_sensors) else "")
        return create_block(start, end, block_minutes=int(get_block_minutes()),
                            seed_meters=(api_mode and not has_sensors))

    existing_start = datetime.fromisoformat(current_block["start"])
    if existing_start == start:
        return current_block

    logger.info("Block rollover: %s → %s", current_block["start"], iso(start))

    # Wait for at least one post-boundary read before finalising. This brackets
    # the boundary so the block's end-register can be interpolated at the exact
    # boundary instant. It applies to BOTH local-sensor (CAD) reads AND Mini
    # telemetry reads — the Mini reader feeds its (timestamped, possibly delayed)
    # readings into the same per-channel reads buffer, so we wait for a read
    # timestamped at/after the boundary before finalising.
    boundary_iso           = iso(start)
    has_post_boundary_read = False
    for meter_data in current_block.get("meters", {}).values():
        for channel in meter_data.get("channels", {}).values():
            for read in channel.get("reads", []):
                if read["ts"] >= boundary_iso:
                    has_post_boundary_read = True
                    break

    if not has_post_boundary_read:
        seconds_since_boundary = (now - start).total_seconds()
        if seconds_since_boundary < 120:
            logger.info("ensure_correct_block: waiting for post-boundary read")
            return current_block
        logger.warning(
            "ensure_correct_block: no post-boundary read after %.0fs, finalising anyway",
            seconds_since_boundary
        )

    # Gap detection before finalise
    last_read_ts = None
    for meter_data in current_block.get("meters", {}).values():
        for channel in meter_data.get("channels", {}).values():
            reads = channel.get("reads", [])
            if reads:
                ts = reads[-1]["ts"]
                if not last_read_ts or ts > last_read_ts:
                    last_read_ts = ts

    _ecb_bm = int(get_block_minutes())
    missing_windows = detect_gap(last_read_ts, now, block_minutes=_ecb_bm)
    if missing_windows:
        logger.warning(
            "ensure_correct_block: %d missing blocks, setting gap marker", len(missing_windows)
        )
        pre_reads, last_rates = extract_last_reads(current_block)
        set_gap_marker(current_block, pre_reads, last_rates)

    # Skip chart regeneration during catch-up rollovers (more than one block
    # behind now). Charts are expensive; we only need them current, not for
    # every historical block we roll through after a gap or restore.
    catching_up = (start - existing_start).total_seconds() > int(get_block_minutes()) * 60
    finalise_block(ha, block_data=current_block, interpolated=catching_up,
                   last_known_rates=last_known_rates)

    new_block = _store.load_current_block()
    _seed = False
    try:
        _seed = mode_uses_api() and not _has_local_import_sensor()
    except Exception:
        _seed = False
    if not new_block or not new_block.get("start"):
        logger.warning("ensure_correct_block: pruned buffer missing, creating fresh")
        return create_block(start, end, block_minutes=int(get_block_minutes()),
                            seed_meters=_seed)
    # 4.5.3-B: an EMPTY block at a passed boundary finalises as "nothing to
    # finalise" WITHOUT advancing the opener, so on an API-only account with no
    # live source (no Mini device AND no local sensor) the opener stays behind
    # `now` and every tick re-rolls the SAME boundary — the observed loop
    # ("no post-boundary read after {N}s ... nothing to finalise", every ~10s,
    # never advancing). If the opener did NOT move (unchanged from the block we
    # just finalised), roll it forward to the current window so we advance one
    # block per boundary and let DCC settlement backfill, rather than thrashing.
    # NOTE keyed on "unchanged", not "< start", so genuine gap catch-up (which
    # advances one window per call) is never skipped.
    try:
        if new_block["start"] == current_block.get("start"):
            logger.info("ensure_correct_block: opener did not advance past %s "
                        "(empty block / no live source) — rolling forward to break "
                        "the re-roll loop", iso(start))
            fresh = create_block(start, end, block_minutes=int(get_block_minutes()),
                                 seed_meters=_seed)
            _store.save_current_block(fresh)
            return fresh
    except Exception as _ae:
        logger.warning("ensure_correct_block: forward-roll check failed: %s", _ae)
    return new_block


# ─────────────────────────────────────────────────────────────────────────────
# Sample capture
# ─────────────────────────────────────────────────────────────────────────────

def capture_samples(ha: HAClient, block: dict, now: datetime):
    now_iso = iso(now)
    config  = load_config()

    if not config:
        logger.error("capture_samples: meters_config missing")
        return

    # Ensure block has meters key — may be missing if block was reset to {}
    if "meters" not in block:
        block["meters"] = {}

    today_iso = now.strftime("%Y-%m-%d")
    for meter_id, meter_cfg in config.get("meters", {}).items():
        # Skip retired sub-meters — don't record any reads after retirement date
        meta = meter_cfg.get("meta", {})
        if meta.get("sub_meter") and meta.get("retired_at"):
            if meta["retired_at"] <= today_iso:
                continue

        meter_block = block["meters"].setdefault(
            meter_id, {"meta": {}, "channels": {}, "interpolated": False}
        )
        meter_block["meta"] = meta

        for channel_id, channel_cfg in meter_cfg.get("channels", {}).items():
            channel_block = meter_block["channels"].setdefault(
                channel_id, {"reads": [], "rates": []}
            )

            read_id = channel_cfg.get("read")
            if read_id:
                read_val = read_sensor(ha, read_id)
                if read_val is not None:
                    channel_block["reads"].append({"value": read_val, "ts": now_iso})

            rate_id = channel_cfg.get("rate")
            if rate_id:
                rate_val = read_sensor(ha, rate_id)
                if rate_val is not None:
                    channel_block["rates"].append({"value": rate_val, "ts": now_iso})


# ─────────────────────────────────────────────────────────────────────────────
# Compute kWh / cost
# ─────────────────────────────────────────────────────────────────────────────

_zero_rate_warned: dict = {}   # (meter_id, channel_id) -> last warn monotonic ts


def _warn_zero_rate(meter_id: str, channel_id: str) -> None:
    """Warn (throttled to once/hour per meter+channel) that a block with real
    consumption is being costed at a zero rate — i.e. no rate is configured for
    this channel. This is the 'rate field left blank' misconfiguration; usage
    is being billed as free. Throttled so it doesn't spam every 30-min block.
    """
    import time as _t
    key = (meter_id, channel_id)
    now = _t.monotonic()
    last = _zero_rate_warned.get(key)
    if last is not None and (now - last) < 3600:
        return
    # In API modes the Kraken rate schedule fills missing rates at settlement,
    # so a transiently-zero rate is expected, not a misconfiguration. Only warn
    # in CAD-only mode where a blank rate genuinely means zero-cost blocks.
    try:
        if mode_uses_api():
            return
    except Exception:
        pass
    _zero_rate_warned[key] = now
    logger.warning(
        "compute_channel: %s/%s has consumption but NO rate configured — "
        "blocks are being costed at £0. Set a rate sensor for this channel "
        "(Configuration → meter), or configure the supplier API.",
        meter_id, channel_id)


# A single block's import/export delta can never plausibly reach this. Domestic
# supply tops out well under 100 kW, so a per-block delta in the hundreds of kWh
# is always a lost-opener artifact — the whole cumulative register booked as one
# interval — not real usage. Used as the rogue-total backstop in compute_channel;
# 15×+ margin over the most extreme domestic block so it never trips on real use.
_ROGUE_BLOCK_KWH_CEILING = 500.0

# Same idea for a SINGLE sub-meter (device) channel — but the main-meter clamp
# above is is_sub_meter-gated, which left device channels unprotected (#260: a
# newly-added cumulative battery/EV sensor booked its whole lifetime register as
# one block). Physical ceiling for ONE device: even a 48 kW 3-phase charger tops
# out ~24 kWh in a 30-min block / ~48 kWh in an hour, so 60 kWh is impossible and
# means a lost opener. Kept well above any real charge so session-energy sensors
# (which count a genuine 10–50 kWh charge) are never clamped.
_ROGUE_SUB_BLOCK_KWH_CEILING = 60.0
# #307: generalise the flat 60 kWh ceiling to any block length — no domestic device
# sustains 120 kW, so block_minutes/60 × 120 kW is the physical impossibility line
# (60 kWh for a 30-min block, matching the flat value above). Used by the write-point
# guard in _apply_pass2 so gap-fill / reconstruction / recovery can't bypass it.
_MAX_PLAUSIBLE_DEVICE_KW = 120.0


def _max_plausible_block_kwh(block_minutes=None) -> float:
    try:
        bm = int(block_minutes or get_block_minutes() or 30)
    except Exception:
        bm = 30
    return bm / 60.0 * _MAX_PLAUSIBLE_DEVICE_KW


def compute_channel(channel: dict, parent_rates=None, is_sub_meter: bool = False,
                    meter_id: str = "?", channel_id: str = "?") -> dict:
    reads     = channel.get("reads", [])
    rates     = channel.get("rates", [])
    if not rates and parent_rates:
        rates = parent_rates
    last_rate = rates[-1]["value"] if rates else 0.0

    if len(reads) < 2:
        return {
            "kwh":        0.0,
            "rate":       last_rate,
            "cost":       0.0,
            "read_start": reads[0]["value"] if reads else 0.0,
            "read_end":   reads[-1]["value"] if reads else 0.0,
        }

    # ── Main meter ────────────────────────────────────────────────────────
    if not is_sub_meter:
        raw_delta = reads[-1]["value"] - reads[0]["value"]
        total_kwh = max(raw_delta, 0.0)
        # Rogue-total backstop (source-agnostic — sensor, Mini, or DCC reads).
        # A single block cannot import a physically impossible amount. This
        # occurs when the opening register is lost or zeroed — e.g. a read
        # dropout during a restart leaves the opener at 0 while the closer still
        # holds the true cumulative register, so the block books the ENTIRE
        # lifetime register as one interval's delta. Clamp to zero and collapse
        # read_start onto read_end so (a) the total is not inflated and (b) the
        # next block still opens at the correct register. Logged (not silent).
        # (Incident 2026-07: 30961 kWh booked in one 30-min
        # block from a zeroed opener during device config churn — a £10k bill.)
        if total_kwh > _ROGUE_BLOCK_KWH_CEILING:
            logger.warning(
                "compute_channel: %s/%s rogue block delta %.1f kWh "
                "(opener=%.3f closer=%.3f) exceeds %.0f kWh ceiling — clamping to "
                "0 (lost/zeroed opener); register continuity preserved via read_end.",
                meter_id, channel_id, total_kwh, reads[0]["value"],
                reads[-1]["value"], _ROGUE_BLOCK_KWH_CEILING)
            return {
                "kwh":          0.0,
                "rate":         last_rate,
                "cost":         0.0,
                "read_start":   reads[-1]["value"],
                "read_end":     reads[-1]["value"],
            }
        # Runtime backstop: a block that consumed/exported energy but has no
        # rate is being costed at ZERO silently — i.e. real usage billed as
        # free. This is the "rate field left blank" hole. Surface it (throttled)
        # so it's visible; billing isn't changed here, only flagged. (Zero-kwh
        # blocks with zero rate are harmless and don't warn.)
        if total_kwh > 0.0001 and (last_rate is None or abs(last_rate) < 1e-9):
            _warn_zero_rate(meter_id, channel_id)
        return {
            "kwh":        total_kwh,
            "rate":       last_rate,
            "cost":       total_kwh * last_rate,
            "read_start": reads[0]["value"],
            "read_end":   reads[-1]["value"],
        }

    # ── Sub meter — backward tariff reconstruction ────────────────────────
    corrected_rates = []
    if rates:
        current_rate = rates[-1]["value"]
        corrected_rates.append({"ts": rates[-1]["ts"], "value": current_rate})
        for r in reversed(rates[:-1]):
            if r["value"] < current_rate:
                current_rate = r["value"]
            corrected_rates.append({"ts": r["ts"], "value": current_rate})
        corrected_rates.reverse()
    else:
        corrected_rates = [{"ts": reads[0]["ts"], "value": last_rate}]

    total_kwh  = 0.0
    total_cost = 0.0
    rate_index = 0
    current_rate = corrected_rates[0]["value"]

    for i in range(1, len(reads)):
        prev_read = reads[i - 1]
        curr_read = reads[i]
        delta     = curr_read["value"] - prev_read["value"]
        if delta < 0:
            continue
        while (
            rate_index + 1 < len(corrected_rates)
            and corrected_rates[rate_index + 1]["ts"] <= curr_read["ts"]
        ):
            rate_index  += 1
            current_rate = corrected_rates[rate_index]["value"]
        total_kwh  += delta
        total_cost += delta * current_rate

    # Use the last rate in the block — consistent with main meter behaviour.
    # This captures the rate as close to the end of the block as possible,
    # accounting for any API lag from remote rate providers.
    display_rate = corrected_rates[-1]["value"]

    # Rogue-total backstop for sub-meters (#260). The main-meter clamp above is
    # is_sub_meter-gated, so device channels had none — a cumulative battery/EV
    # sensor with a lost/absent opener booked its whole lifetime register as one
    # block (the reporter's ~10 MWh). Guard on PHYSICAL PLAUSIBILITY only: no
    # single domestic device (even a 48 kW 3-phase charger) moves 60 kWh in a
    # block, so a delta this large is a lost opener / lifetime dump. Crucially we
    # do NOT key on a near-zero opener — a session-energy sensor legitimately
    # starts each charge at 0 and counts up, and must be booked normally. Clamp
    # to 0 and baseline read_start onto read_end (register continuity preserved).
    if total_kwh > _ROGUE_SUB_BLOCK_KWH_CEILING:
        logger.warning(
            "compute_channel: %s/%s rogue sub-meter block %.1f kWh "
            "(opener=%.3f closer=%.3f) exceeds %.0f kWh ceiling — clamping to 0 "
            "(lost opener / lifetime dump); baseline preserved via read_end.",
            meter_id, channel_id, total_kwh, reads[0]["value"], reads[-1]["value"],
            _ROGUE_SUB_BLOCK_KWH_CEILING)
        return {
            "kwh":          0.0,
            "rate":         display_rate,
            "cost":         0.0,
            "read_start":   reads[-1]["value"],
            "read_end":     reads[-1]["value"],
        }

    return {
        "kwh":        total_kwh,
        "rate":       display_rate,
        "cost":       total_cost,
        "read_start": reads[0]["value"],
        "read_end":   reads[-1]["value"],
    }


def select_opening_read(reads: list, boundary_dt: datetime) -> dict | None:
    boundary_iso = boundary_dt.isoformat()
    pre = [r for r in reads if r["ts"] <= boundary_iso]
    if pre:
        return pre[-1]
    post = [r for r in reads if r["ts"] > boundary_iso]
    return post[0] if post else None


def select_closing_read(reads: list, boundary_dt: datetime) -> dict | None:
    boundary_iso = boundary_dt.isoformat()
    post = [r for r in reads if r["ts"] >= boundary_iso]
    if post:
        return post[0]
    pre = [r for r in reads if r["ts"] < boundary_iso]
    return pre[-1] if pre else None


# ─────────────────────────────────────────────────────────────────────────────
# Gap block builder
# ─────────────────────────────────────────────────────────────────────────────

def build_gap_blocks(
    missing_windows:       list,
    pre_reads_by_channel:  dict,
    post_reads_by_channel: dict,
    last_known_rates:      dict,
    config:                dict,
    last_standing_charge:  float = 0.0,
) -> list:
    gap_blocks = []

    cfg_bm = int((next(iter(config.get("meters", {}).values()), {}).get("meta") or {}).get("block_minutes") or BLOCK_MINUTES)
    for window_start, window_end in missing_windows:
        block = {
            "start":         iso(window_start),
            "end":           iso(window_end),
            "block_minutes": cfg_bm,
            "meters": {},
            "totals": {
                "import_kwh": 0.0, "import_cost": 0.0,
                "export_kwh": 0.0, "export_cost": 0.0,
            },
            "interpolated": True,
        }

        for meter_name, meter_cfg in config.get("meters", {}).items():
            meter_meta  = meter_cfg.get("meta", {})
            is_sub      = meter_meta.get("sub_meter", False)

            # Skip retired sub-meters in gap-fill blocks
            if is_sub and meter_meta.get("retired_at"):
                window_date = window_start.strftime("%Y-%m-%d")
                if meter_meta["retired_at"] <= window_date:
                    continue

            meter_block = {
                "channels": {}, "meta": meter_meta,
                "interpolated": True, "standing_charge": last_standing_charge,
            }

            for channel_name in meter_cfg.get("channels", {}).keys():

                if is_sub:
                    pre_read  = pre_reads_by_channel.get(meter_name, {}).get(channel_name)
                    post_read = post_reads_by_channel.get(meter_name, {}).get(channel_name)
                    sub_kwh = sub_rate = sub_cost = sub_start = sub_end = 0.0
                    skip_reason = None

                    if not pre_read or not post_read:
                        skip_reason = "missing reads"
                    else:
                        pre_ts    = datetime.fromisoformat(pre_read["ts"])
                        post_ts   = datetime.fromisoformat(post_read["ts"])
                        gap_hours = (post_ts - pre_ts).total_seconds() / 3600
                        if gap_hours > 12:
                            skip_reason = f"gap too large ({gap_hours:.1f}hrs)"
                        elif post_read["value"] == pre_read["value"]:
                            # Identical reads — the sub-meter sensor did not change
                            # during the gap (e.g. a brief HA outage with no actual
                            # consumption). The delta is ZERO; do NOT mistake this
                            # for a reset and bill the full cumulative register.
                            # (Regression: 2.10.9 — rogue full-register kWh, e.g.
                            # 5798 kWh on a battery, from a momentary restart.)
                            sub_kwh = 0.0
                            logger.info(
                                "build_gap_blocks: %s/%s unchanged (%.4f → %.4f) "
                                "— zero delta for gap block",
                                meter_name, channel_name,
                                pre_read["value"], post_read["value"]
                            )
                        elif post_read["value"] < pre_read["value"] * 0.5:
                            # Genuine reset — the register COLLAPSED to a small
                            # fraction of its prior level (daily-reset sensor, device
                            # swap). Use post_read value directly as kWh accumulated
                            # since the reset.
                            sub_kwh = max(round(post_read["value"], 6), 0.0)
                            sub_end = sub_kwh
                            logger.info(
                                "build_gap_blocks: %s/%s reset detected (%.4f → %.4f) "
                                "using post-reset value %.4f kWh",
                                meter_name, channel_name,
                                pre_read["value"], post_read["value"], sub_kwh
                            )
                        elif post_read["value"] < pre_read["value"]:
                            # Register stepped BACKWARD but did not collapse — a
                            # cumulative counter can't do that for real, so this is a
                            # glitch / stale read (a dropped sensor briefly surfacing an
                            # old value), NOT a reset. Booking the post value (or, worse,
                            # the later climb back up) manufactures phantom energy — the
                            # 2026-07-21 house_battery 6259.77 case. Zero the delta and
                            # carry the pre-gap register forward so continuity holds.
                            sub_kwh   = 0.0
                            sub_start = sub_end = pre_read["value"]
                            logger.warning(
                                "build_gap_blocks: %s/%s register dipped backward "
                                "(%.4f → %.4f) without collapsing — glitch/stale read; "
                                "zero delta, register carried forward",
                                meter_name, channel_name,
                                pre_read["value"], post_read["value"]
                            )
                        else:
                            opener   = interpolate_value(pre_read, post_read, window_start)
                            closer   = interpolate_value(pre_read, post_read, window_end)
                            sub_kwh  = max(round(closer["value"] - opener["value"], 6), 0.0)
                            _sr = last_known_rates.get(meter_name, {}).get(channel_name)
                            if _sr is None:
                                parent_name = meter_meta.get("parent_meter")
                                _sr = last_known_rates.get(parent_name, {}).get(channel_name)
                            sub_rate = _rate_value(_sr)
                            if not sub_rate:
                                logger.info("build_gap_blocks: %s/%s using parent rate %.4f", meter_name, channel_name, sub_rate)
                            sub_cost  = round(sub_kwh * sub_rate, 6)
                            sub_start = opener["value"]
                            sub_end   = closer["value"]

                    if skip_reason:
                        logger.warning("build_gap_blocks: %s/%s zero — %s", meter_name, channel_name, skip_reason)
                        # Still look up the last known rate so the chart rate line
                        # doesn't spike to zero on restarts
                        _sr = last_known_rates.get(meter_name, {}).get(channel_name)
                        if _sr is None:
                            parent_name = meter_meta.get("parent_meter")
                            _sr = last_known_rates.get(parent_name, {}).get(channel_name)
                        sub_rate = _rate_value(_sr)
                    else:
                        # Spike detection — sub-meter kwh cannot exceed parent grid import
                        # for this window. Get parent import kwh for sanity check.
                        parent_name   = meter_meta.get("parent_meter", "electricity_main")
                        parent_pre    = pre_reads_by_channel.get(parent_name, {}).get(channel_name)
                        parent_post   = post_reads_by_channel.get(parent_name, {}).get(channel_name)
                        if parent_pre and parent_post and parent_post["value"] > parent_pre["value"]:
                            p_opener    = interpolate_value(parent_pre, parent_post, window_start)
                            p_closer    = interpolate_value(parent_pre, parent_post, window_end)
                            parent_kwh  = max(round(p_closer["value"] - p_opener["value"], 6), 0.0)
                            if sub_kwh > parent_kwh * 1.05:  # 5% tolerance for rounding
                                logger.warning(
                                    "build_gap_blocks: %s/%s %.4f kWh EXCEEDS parent grid %.4f kWh "
                                    "— may be gap attribution issue or misconfigured sensor. "
                                    "Recording as-is.",
                                    meter_name, channel_name, sub_kwh, parent_kwh,
                                )

                    meter_block["channels"][channel_name] = {
                        "kwh": sub_kwh, "rate": sub_rate, "cost": sub_cost,
                        "read_start": sub_start, "read_end": sub_end, "interpolated": True,
                    }
                    continue

                # ── Main meter ────────────────────────────────────────────
                pre_read  = pre_reads_by_channel.get(meter_name, {}).get(channel_name)
                post_read = post_reads_by_channel.get(meter_name, {}).get(channel_name)

                if not pre_read or not post_read:
                    if channel_name == "export":
                        # Export with no reads means a DCC-only / no-export-sensor setup
                        # (FIT, Mini, no export tariff). DON'T plant a false 0.0 — it renders
                        # as "zero export" (e.g. right through the solar peak) until DCC
                        # settles. Leave the channel UNMATERIALISED (NULL): DCC fills a NULL
                        # identically to a 0.0, and until then the view reads "awaiting
                        # settlement" rather than a wrong zero.
                        logger.info("build_gap_blocks: %s export has no reads (DCC-only) "
                                    "— leaving unmaterialised for settlement (no false-0)",
                                    meter_name)
                        continue
                    logger.warning("build_gap_blocks: missing reads for %s/%s", meter_name, channel_name)
                    _fallback_rate = _rate_value(last_known_rates.get(meter_name, {}).get(channel_name, 0.0))
                    meter_block["channels"][channel_name] = {
                        "kwh": 0.0, "rate": _fallback_rate, "cost": 0.0,
                        "read_start": 0.0, "read_end": 0.0, "interpolated": True,
                    }
                    continue

                opener = interpolate_value(pre_read, post_read, window_start)
                # For the last gap window, anchor read_end to the actual post_read
                # value rather than an interpolated value. An interpolated closer can
                # exceed the next real block's read_start, causing the same register
                # space to be counted in both the gap block and the real block.
                _is_last_window = ((window_start, window_end) == missing_windows[-1])
                if _is_last_window:
                    closer = {"value": post_read["value"], "ts": post_read["ts"]}
                else:
                    closer = interpolate_value(pre_read, post_read, window_end)
                kwh    = max(round(closer["value"] - opener["value"], 6), 0.0)
                rate   = _rate_value(last_known_rates.get(meter_name, {}).get(channel_name, 0.0))
                cost   = round(kwh * rate, 6)

                meter_block["channels"][channel_name] = {
                    "kwh": kwh, "rate": rate, "cost": cost,
                    "read_start": opener["value"], "read_end": closer["value"],
                    "interpolated": True,
                }

                if channel_name == "import":
                    block["totals"]["import_kwh"]  += kwh
                    block["totals"]["import_cost"] += cost
                elif channel_name == "export":
                    block["totals"]["export_kwh"]  += kwh
                    block["totals"]["export_cost"] += cost

            block["meters"][meter_name] = meter_block

        # Apply grid-authoritative clipping to gap blocks (same as finalise_block PASS 2)
        _apply_pass2(block)

        # PASS 3b for gap blocks: attribute carbon if a CI slot is available for
        # this window. Gap blocks previously skipped this, leaving
        # carbon_intensity_g NULL (the outage-block carbon gap). If CI isn't
        # available now, the recovery sweep will backfill it later.
        if not _attribute_block_carbon(block, iso(window_start)):
            logger.info("build_gap_blocks: %s carbon not attributed "
                        "(no CI slot yet; recovery sweep will retry)",
                        iso(window_start))

        gap_blocks.append(block)
        logger.info(
            "build_gap_blocks: %s → %s  import=%.4f kWh  export=%.4f kWh",
            iso(window_start), iso(window_end),
            block["totals"]["import_kwh"], block["totals"]["export_kwh"],
        )

    return gap_blocks


# ─────────────────────────────────────────────────────────────────────────────
# HA sensor update helper  (replaces repeated state.set() blocks)
# ─────────────────────────────────────────────────────────────────────────────

# BL-5 (3.2.0): the four synthetic block sensors were REMOVED —
#   sensor.energy_meter_import_kwh / _export_kwh / _import_cost / _export_credit
# They were unused and actively misleading: they published live per-block figures
# that DCC settlement retrospectively corrects, so their values disagreed with the
# billing EMT itself reports. The last remaining entity,
# sensor.energy_meter_tracker_api_deprecations, was removed in 3.4.0 (BL-17) — its
# deprecation signal is now handled by the CI GraphQL-deprecation check — so EMT
# now publishes NO HA entities at all.


# ─────────────────────────────────────────────────────────────────────────────
# Block finalise
# ─────────────────────────────────────────────────────────────────────────────

def _import_rate_or_fallback(block_start: str, meter_id: str, imp_ch: dict) -> float:
    """Resolve an import block's unit rate when the legacy `imp_rate` column is NULL —
    which happens transiently while a gap-fill's verify pass hasn't yet repriced the
    range. Prefer the priced `block_segments` (the source of truth; kWh-weighted inc
    rate), else derive cost/kWh, else 0.0 (genuinely unpriced). Never returns None, so
    `kwh * rate` in PASS 2 can't raise `float * NoneType` during device re-attribution."""
    try:
        segs = get_store().get_block_segments(block_start, meter_id, "import") or []
        tot = sum((sg.get("kwh") or 0.0) for sg in segs)
        if tot > 0:
            return sum((sg.get("kwh") or 0.0) * (sg.get("inc_rate") or 0.0) for sg in segs) / tot
    except Exception:
        pass
    k = imp_ch.get("kwh") or 0.0
    c = imp_ch.get("cost")
    if c is not None and k > 0:
        return c / k
    return 0.0


def _apply_pass2(block: dict) -> None:
    """
    PASS 2 — grid-authoritative sub-meter distribution.
    Clips each sub-meter's import to what the parent grid import can supply, and sets kwh_grid
    on each sub-meter channel — the share `settle.device_grid_shares` gives it (BL-76). Its
    reading past that share is its non-grid supply, `kwh − kwh_grid`.
    Called from both finalise_block and build_gap_blocks.
    """
    # v4 #493: the main meter's rogue-total backstop at the shared write point. The
    # ceiling lived only in compute_channel, which a gap block never passes through:
    # build_gap_blocks interpolates opener → closer itself, so a gap whose opening
    # register was 0.0 booked the meter's whole lifetime register as an hour's
    # export after a power cut (v4 #493) — an export credit in the thousands. Same rule
    # and the same ceiling as compute_channel: clamp to zero, collapse read_start onto
    # read_end so the next block opens on the real register. Logged.
    # Settlement still fills the true figure where DCC data exists.
    for meter_name, meter_block in block["meters"].items():
        if (meter_block.get("meta") or {}).get("sub_meter"):
            continue
        for channel_name, ch in (meter_block.get("channels") or {}).items():
            _k = (ch or {}).get("kwh") or 0.0
            if _k > _ROGUE_BLOCK_KWH_CEILING:
                logger.warning(
                    "PASS 2: %s/%s implausible %.1f kWh in one block (> %.1f kWh "
                    "ceiling) — lost opener (read_start %s → read_end %s); clamping "
                    "to 0, baselining register (v4 #493).",
                    meter_name, channel_name, _k, _ROGUE_BLOCK_KWH_CEILING,
                    ch.get("read_start"), ch.get("read_end"))
                _c = ch.get("cost") or 0.0
                if ch.get("read_end") is not None:
                    ch["read_start"] = ch["read_end"]
                ch["kwh"] = 0.0
                ch["cost"] = 0.0
                _t = block.get("totals")
                if channel_name in ("import", "export") and isinstance(_t, dict):
                    _t[channel_name + "_kwh"] = (_t.get(channel_name + "_kwh") or 0.0) - _k
                    _t[channel_name + "_cost"] = (_t.get(channel_name + "_cost") or 0.0) - _c

    # Build parent → sub_kwh_total map
    parent_sub_kwh: dict = {}
    for meter_name, meter_block in block["meters"].items():
        meta = meter_block.get("meta", {})
        if not meta.get("sub_meter"):
            continue
        parent_name = meta.get("parent_meter")
        if not parent_name:
            continue
        sub_import = meter_block["channels"].get("import")
        if sub_import:
            parent_sub_kwh[parent_name] = parent_sub_kwh.get(parent_name, 0.0) + sub_import.get("kwh", 0.0)

    for parent_meter_name in parent_sub_kwh:
        parent_block  = block["meters"].get(parent_meter_name)
        if not parent_block:
            continue
        parent_import = parent_block["channels"].get("import")
        if not parent_import:
            continue

        grid_kwh       = parent_import.get("kwh") or 0.0
        # Legacy `imp_rate` can be NULL (block imported but its verify reprice hasn't
        # landed yet — device re-attribution over a fresh gap fill hits this). Resolve
        # from segments / cost-per-kWh so `claimed * parent_rate` never sees None, and
        # the device is costed at the real rate rather than crashing or zeroing.
        parent_rate    = parent_import.get("rate")
        if parent_rate is None:
            parent_rate = _import_rate_or_fallback(block.get("start", ""),
                                                   parent_meter_name, parent_import)
        # BL-19: is the main's grid import authoritative? A DCC-settled main
        # (imp_kwh_api present) is the true grid boundary for the block, so the
        # sub-meter invariant (Σ sub kwh_grid ≤ main grid import) MUST be
        # enforced — even when the block is flagged interpolated (a gap-fill
        # origin whose main has since settled per-half-hour). See clamp_overflow.
        parent_settled = parent_block.get("imp_kwh_api") is not None

        protected   = []

        for meter_name, meter_block in block["meters"].items():
            meta = meter_block.get("meta", {})
            if not meta.get("sub_meter") or meta.get("parent_meter") != parent_meter_name:
                continue
            sub_import = meter_block["channels"].get("import")
            if not sub_import:
                continue
            delta = sub_import.get("kwh", 0.0)
            # #307: physical-plausibility backstop at the shared write point. A lost
            # or zeroed OPENING register books the whole lifetime register as one
            # interval (prod: house_battery 0 → 6137 = 6137 kWh in a half-hour). That
            # is impossible for any device — clamp IMMEDIATELY, never defer to
            # settlement (which must only refine a sane figure, never pass an
            # impossibility). Sensor-agnostic: a session-energy sensor's real charge
            # never reaches this ceiling, so this can't mis-fire on a Zappi/session
            # reset. Baseline read_start onto read_end so the register stays
            # continuous, and zero the block. Logged.
            # The block's own length first — a stored block carries its config period's, and the
            # device rebuild's new device entry has no meta to speak of: falling through to
            # get_block_minutes() read the config from the database for every device row
            # (staging, 22 Oct 2026: a rebuild beside the heatmap's month loads, the loop held 16.9 s)
            _ceiling = _max_plausible_block_kwh((meta or {}).get("block_minutes")
                                                or block.get("_block_minutes"))
            if delta > _ceiling:
                logger.warning(
                    "PASS 2: %s implausible %.1f kWh in one block (> %.1f kWh "
                    "ceiling) — lost-opener/glitch; clamping to 0 and baselining "
                    "register (#307).",
                    meter_name, delta, _ceiling)
                _re = sub_import.get("read_end")
                if _re is not None:
                    sub_import["read_start"] = _re
                sub_import["kwh"] = 0.0
                sub_import["kwh_grid"] = 0.0
                sub_import["cost"] = 0.0
                continue
            if delta < 0:
                # Reset detected mid-block — read_end is the kWh accumulated since
                # the reset. Use it directly rather than skipping the block.
                delta = sub_import.get("read_end", 0.0)
                logger.info(
                    "PASS 2: %s reset detected, using post-reset read_end %.4f kWh",
                    meter_name, delta
                )
                if delta <= 0.0:
                    continue
                sub_import["kwh"] = delta
            if delta == 0.0:
                # A device that drew nothing this block still FOLLOWS the main
                # meter's effective rate for its displayed rate (cost is 0). Skip
                # was leaving the sub-meter on whatever compute_channel produced,
                # and a sub-meter's running-minimum reconstruction collapses to the
                # adjacent off-peak rate at an off-peak→peak boundary block — so a
                # zero-draw device diverged from a peak main. Pin it to the parent.
                sub_import["rate"]        = parent_rate
                sub_import["cost"]        = 0.0
                sub_import["kwh_grid"]    = 0.0
                continue
            entry = {
                "meter_name": meter_name, "meter_block": meter_block,
                "sub_import": sub_import, "kwh": delta,
            }
            protected.append(entry)  # all sub-meters are protected (inverter_possible removed)

        # Grid attribution (BL-76, the battery sensor and device split design §2): the EV claims grid
        # import FIRST, then every other device shares what remains in proportion to its reading.
        # EV charging is grid-charging by design — on IOG/smart-charge the supplier is deliberately
        # pulling cheap grid for the car — so the car's import must land on the grid, not get
        # squeezed out: biggest-draw-first handed the whole grid pool to a simultaneously-charging
        # battery and the car vanished from the grid view until the battery filled (issue #212).
        # The rest shared largest first until BL-76 — the smaller device absorbed the whole shortfall.
        # v4 4.5.21 forward fix: on a DISPATCH-ONLY account (no device is an EV meter) the main's
        # dispatch EV (`kwh_ev`) is reserved before the devices are fitted, so they fit inside the
        # house = grid − EV; with an EV meter the meter IS the EV. One rule, shared with settle().
        import settle as _st
        _is_ev = lambda e: ((e["meter_block"].get("meta", {}) or {}).get("meter_type")
                            in _st.EV_METER_TYPES)
        _has_ev_meter = any(_is_ev(e) for e in protected)
        _dispatch_ev = 0.0
        if not _has_ev_meter:
            _dispatch_ev = min(max(float(parent_import.get("kwh_ev") or 0.0), 0.0), max(grid_kwh, 0.0))

        is_interpolated = block.get("interpolated", False)
        # BL-19: enforce the sub-meter grid invariant unless this is an UNSETTLED
        # gap block. When the main is live-metered OR DCC-settled its grid import
        # is authoritative, so a sub-meter can never claim more grid than the
        # house drew — clip the overflow to grid and spill the remainder to
        # self-consumption. The only case we still record as-is is an interpolated
        # block whose main has NOT yet settled: there the main is itself a
        # gap-fill estimate that may under-shoot, and clamping could push a real
        # (e.g. overnight) grid draw into free self-consumption and under-bill.
        # Settlement re-runs PASS 2 (imp_kwh_api set), so those self-correct then.
        clamp_overflow = parent_settled or not is_interpolated
        _shares = _st.device_grid_shares(grid_kwh, [(e["kwh"], _is_ev(e)) for e in protected],
                                         ev_kwh=_dispatch_ev, has_ev_meter=_has_ev_meter,
                                         clamp=clamp_overflow)
        for entry, claimed in zip(protected, _shares):
            if entry["kwh"] > grid_kwh and not _pass2_quiet:
                logger.warning(
                    "PASS 2: %s sub-meter %.4f kWh EXCEEDS parent grid import %.4f kWh — %s.",
                    entry["meter_name"], entry["kwh"], grid_kwh,
                    "clipping to its grid share (%s)" % ("settled" if parent_settled else "live")
                    if clamp_overflow else
                    "unsettled gap block, recording as-is (settlement will re-clamp)")
            elif claimed < entry["kwh"] - 1e-9 and not _pass2_quiet:
                logger.warning(
                    "PASS 2: %s protected load %.4f kWh clipped to %.4f kWh",
                    entry["meter_name"], entry["kwh"], claimed,
                )
            entry["sub_import"]["kwh_grid"]    = claimed
            # Devices always follow the main meter's EFFECTIVE import rate (base
            # tariff + dispatch overlay): grid import is a portion of the main's
            # metered supply, billed at the main's rate, so per-device costs sum
            # to the metered bill. Set BOTH rate and cost here — this is the single
            # costing point (runs at finalise and settlement), so rate always
            # equals cost/kWh with no dependency on meter order or a per-device
            # rate source.
            entry["sub_import"]["rate"]        = parent_rate
            entry["sub_import"]["cost"]        = round(claimed * parent_rate, 6)
            if not _pass2_quiet: logger.info(
                "PASS 2: %s protected  grid=%.4f  other=%.4f",
                entry["meter_name"], claimed, entry["kwh"] - claimed,
            )
        grid_remaining = max(grid_kwh - _dispatch_ev - sum(_shares), 0.0)

        remainder_kwh  = max(grid_remaining, 0.0) + _dispatch_ev

        # cost_remainder is computed as remainder_kwh × rate, NOT as main_cost − sub_costs.
        #
        # Why: both sub_import["cost"] (line above) and cost_remainder are derived from
        # kwh × rate. This is intentionally consistent — both sides of the identity use
        # the same arithmetic path.
        #
        # Why not main_cost − sub_costs: main_cost comes from actual meter register reads
        # (a cumulative kWh delta interpolated to block boundaries), while sub-meter costs
        # come from power sensor integration (kW polled at ~10s intervals). These are two
        # independent measurement systems with different sensor resolutions and timing.
        # Subtracting one from the other would silently absorb all sensor disagreement
        # into cost_remainder, potentially making it negative on blocks where the sub-meter
        # sensor over-reported relative to the main meter.
        #
        # The resulting ~0.01p/block discrepancy between cost_remainder + sub_costs and
        # main_cost is an inherent measurement artefact of combining two sensor systems.
        # Presentation layers (usage_view's Usage Stats rows, billing charts) use rate-based
        # subtraction (main_cost[rate] − sub_cost[rate]) for display, which avoids
        # accumulating this artefact across periods. See block_store._aggregate_usage
        # and usage_view._aggregate_block_rows for the display-layer approach.
        remainder_cost = round(remainder_kwh * parent_rate, 6)

        parent_import["kwh_total"]      = grid_kwh
        parent_import["kwh_remainder"]  = remainder_kwh
        parent_import["cost_remainder"] = remainder_cost
        parent_import["rate_used"]      = parent_rate
        if not _pass2_quiet: logger.info(
            "PASS 2: %s  grid=%.4f kWh  remainder=%.4f kWh",
            parent_meter_name, grid_kwh, remainder_kwh,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2.10.0 — Sub-meter boundary interpolation amendment
# ─────────────────────────────────────────────────────────────────────────────

# Maximum median inter-read interval (seconds) for a sub-meter to be eligible
# for boundary interpolation.  Devices publishing slower than this are too
# coarse — the linear interpolation assumption breaks down and the provisional
# figure (last pre-boundary read) is the best available estimate.
# 90 s gives 60 s devices a comfortable jitter margin.
_PROVISIONAL_MAX_INTERVAL_S: float = 90.0

# A post-boundary read is only used for interpolation if it arrives within
# this multiple of the observed device interval after the boundary.  Catches
# the stop-then-restart case: if the device genuinely stopped before the
# boundary and restarted 15 min later, the gap is >> 2× interval and we
# correctly leave the provisional figure unchanged.
_PROVISIONAL_GAP_MULTIPLIER: float = 2.0

# Minimum number of inter-read gaps required to compute a reliable median
# device interval.  3 gaps (4 reads) gives a median robust against one
# outlier on a stable periodic signal — sufficient regardless of block size.
# Larger blocks naturally provide more gaps but the minimum stays flat.
_PROVISIONAL_MIN_GAPS: int = 3


def _observed_device_interval_s(reads: list) -> float | None:
    """Return the median inter-read gap in seconds from a list of read dicts.

    Returns None if there are fewer than (_PROVISIONAL_MIN_GAPS + 1) reads
    (i.e. fewer than 4 reads / 3 gaps).  3 gaps is the minimum needed for a
    median robust against one outlier on a stable periodic signal, and holds
    across all block sizes — a 5-minute block with a 60s device still yields
    ~4 reads, which is sufficient.
    Each read dict must have a 'ts' key containing an ISO timestamp string.
    """
    if len(reads) < _PROVISIONAL_MIN_GAPS + 1:
        return None
    sorted_reads = sorted(reads, key=lambda r: r["ts"])
    gaps = []
    for i in range(1, len(sorted_reads)):
        try:
            t0 = datetime.fromisoformat(sorted_reads[i - 1]["ts"])
            t1 = datetime.fromisoformat(sorted_reads[i]["ts"])
            gap_s = (t1 - t0).total_seconds()
            if gap_s > 0:
                gaps.append(gap_s)
        except (ValueError, KeyError):
            continue
    if len(gaps) < _PROVISIONAL_MIN_GAPS:
        return None
    gaps.sort()
    mid = len(gaps) // 2
    return gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2.0


def _commit_provisional_as_final(ha: HAClient, full_block: dict,
                                  meter_name: str, block_start_iso: str,
                                  block_end_iso: str) -> None:
    """Clear the provisional flag on *meter_name* in *full_block*, re-run PASS 2,
    and write back to DB.

    The kWh figure is left exactly as originally computed — this path is taken
    when interpolation is not appropriate (device too slow, gap too large,
    gap-fill in progress, insufficient reads).  PASS 2 is re-run for
    correctness but produces the same result as the original write.
    """
    full_block["meters"][meter_name].pop("provisional", None)
    _apply_pass2(full_block)
    try:
        append_block_replace(full_block)
        logger.info(
            "_amend_provisional: block %s → %s committed as-final for %s "
            "(no interpolation applied)",
            block_start_iso, block_end_iso, meter_name,
        )
    except Exception as _we:
        logger.error(
            "_amend_provisional: DB write failed committing as-final for %s: %s",
            meter_name, _we,
        )
        return


def _carbon_of(meter_block: dict, intensity: float) -> float:
    """A meter's carbon for one half-hour (gCO2) at a grid intensity (gCO2/kWh) — the one formula
    (engine step 5b, 27 Oct 2026: finalise, the startup gap fill and the recompute each had a copy).

    Measured inputs only (step 5 design §1.3, decided as v4): a device's carbon is its OWN read
    × intensity — the gross figure, whatever supplied it; EMT does not measure a device's source,
    and PASS 2's grid clip is its allocation. The main meter's is (`kwh_total` — the whole grid
    draw before the devices — − export) × intensity, net of what it exported."""
    meta = meter_block.get("meta", {}) or {}
    imp_ch = (meter_block.get("channels") or {}).get("import") or {}
    exp_ch = (meter_block.get("channels") or {}).get("export") or {}
    if meta.get("sub_meter", False):
        return round(float(imp_ch.get("kwh", 0.0) or 0.0) * intensity, 4)
    imp_kwh = float(imp_ch.get("kwh_total", imp_ch.get("kwh", 0.0)) or 0.0)
    exp_kwh = float(exp_ch.get("kwh", 0.0) or 0.0)
    return round((imp_kwh - exp_kwh) * intensity, 4)


def _apply_intensity_to_block(block: dict, intensity: float) -> None:
    """Attribute carbon_g + carbon_intensity_g to every meter in *block* using a
    known grid intensity (gCO2/kWh): the CI-table attribution (_attribute_block_carbon —
    finalise, the gap blocks, the recovery) and the historical backfill."""
    for meter_name, meter_block in block.get("meters", {}).items():
        meter_block["carbon_g"] = _carbon_of(meter_block, intensity)
        meter_block["carbon_intensity_g"] = intensity


def _persist_block_carbon(block: dict, start: str) -> int:
    """Write the per-meter carbon set by _apply_intensity_to_block IN PLACE
    (targeted carbon-column UPDATE per meter). Replaces append_block_replace on
    the carbon paths, which round-tripped the whole row and silently wiped the
    `source` tag (untagging every imported block carbon touched). Returns rows
    updated."""
    if _store is None:
        return 0
    n = 0
    for _bs, meter_name, ig, cg in _block_carbon_rows(block, start):
        if _store.set_block_carbon(start, meter_name, ig, cg):
            n += 1
    return n


def _block_carbon_rows(block: dict, start: str) -> list:
    """(block_start, meter_id, carbon_intensity_g, carbon_g) for each meter of
    *block* — what _persist_block_carbon writes."""
    return [(start, meter_name, mb.get("carbon_intensity_g"), mb.get("carbon_g"))
            for meter_name, mb in (block.get("meters") or {}).items()]


def _attribute_block_carbon(block: dict, start: str, postcode: str | None = None) -> bool:
    """Look up the nearest carbon-intensity slot for *start* and attribute
    carbon_g (+ persist carbon_intensity_g) to every meter in *block*.

    This is the PASS 3b logic, extracted so it can run from finalise_block,
    the gap-fill path (build_gap_blocks — prevention), and the NULL-carbon
    recovery sweep. Returns True if a CI slot was found and carbon attributed,
    False otherwise (no postcode, or no CI slot available for that time —
    e.g. the slot has aged out of the 4-day CI table). A caller attributing many
    blocks passes *postcode*: _get_postcode() rebuilds the whole configuration.
    """
    try:
        if postcode is None:
            postcode = _get_postcode()
        if not postcode:
            return False
        ci_row = _store.get_nearest_carbon_intensity(start, postcode)
        if not ci_row:
            return False
        # Bound the slot distance: get_nearest_carbon_intensity returns the
        # nearest row with NO distance limit, so a block whose true slot has aged
        # out of the 4-day table would otherwise be attributed a wrong, distant
        # CI value. Only attribute when the slot is genuinely near (≤60 min; CI
        # slots are 30 min, so a correct match is ≤30 min away).
        try:
            from datetime import datetime as _dt
            _bs = _dt.fromisoformat(str(start).replace("Z", "").split("+")[0])
            _cs = _dt.fromisoformat(str(ci_row["captured_at"]).replace("Z", "").split("+")[0])
            if abs((_bs - _cs).total_seconds()) > 3600:
                return False
        except Exception:
            pass
        _apply_intensity_to_block(block, ci_row["intensity"])
        return True
    except Exception as e:
        logger.warning("_attribute_block_carbon: failed for %s: %s", start, e)
        return False


def _recover_missing_carbon(limit: int = 200) -> int:
    """Backfill carbon for blocks whose carbon_intensity_g is NULL.

    These are typically outage gap-fill blocks that were built before a CI slot
    was available (the energy was recovered by interpolation, but carbon wasn't
    attributed). Now that the CI tick has populated the table, re-attribute from
    the nearest CI slot and persist. Blocks whose slot has aged out of the 4-day
    CI table can't be recovered and are left for a future manual tool.

    Returns the number of blocks recovered. Safe to call on startup and after a
    CI tick; a no-op when nothing is missing.
    """
    if _store is None:
        return 0
    if _store.get_meta("carbon_paused", None):
        return 0
    try:
        starts = _store.get_block_starts_missing_carbon(limit=limit)
    except Exception as e:
        logger.warning("_recover_missing_carbon: query failed: %s", e)
        return 0
    if not starts:
        return 0
    # The postcode once per pass, and every write in ONE transaction (#65, 3 Oct
    # 2026). This runs on the engine loop after a CI fetch; it committed once per
    # block and meter, ~37 ms each on the Pi's SD card, and held the loop 7.6 s for
    # 95 blocks after a restore. The writes are set_block_carbon's own (in place,
    # carbon columns only — keeps the source tag).
    postcode = _get_postcode()
    if not postcode:
        return 0
    rows, done = [], []
    for start in starts:
        block = _store.get_block_dict_by_start(start)
        if not block:
            continue
        if _attribute_block_carbon(block, start, postcode=postcode):
            rows.extend(_block_carbon_rows(block, start))
            done.append(start)
        # If not attributed, the CI slot isn't available (aged out) — skip.
    if not rows:
        return 0
    try:
        _store.set_block_carbon_many(rows)
    except Exception as e:
        logger.warning("_recover_missing_carbon: persist failed for %d block(s) "
                       "(%s…%s): %s", len(done), done[0], done[-1], e)
        return 0
    recovered = len(done)
    for start in done:
        logger.info("_recover_missing_carbon: backfilled carbon for %s", start)
    if recovered:
        logger.info("_recover_missing_carbon: recovered %d block(s)", recovered)
    return recovered


def _recompute_block_carbon(block: dict) -> None:
    """Recompute carbon_g for every meter in *block* using the intensity
    persisted on the block (carbon_intensity_g), NOT a fresh CI-table lookup.

    The same formula as finalise_block PASS 3b (_carbon_of), but sources intensity from the
    stored value so a DCC re-run weeks later (after the 4-day CI prune) still
    produces the correct figure. If a meter has no stored intensity, carbon_g
    is left unchanged (we cannot recompute without it, and reverse-deriving
    from the old carbon_g would be circular).
    """
    for meter_name, meter_block in block.get("meters", {}).items():
        intensity = meter_block.get("carbon_intensity_g")
        if intensity is None:
            continue
        meter_block["carbon_g"] = _carbon_of(meter_block, intensity)


def _resolve_block_rate(channel: dict, block_start: str, channel_name: str,
                        rate_resolver=None) -> tuple[float, bool]:
    """Return (rate, repaired) for a channel at reconcile time.

    Uses the channel's stored rate when it is a sane non-zero value. When the
    stored rate is zero/missing AND a rate_resolver is supplied, asks the
    resolver for the historical rate at block_start (the Kraken fallback,
    Chunk 5). The resolver returns rate in £/kWh already (caller converts from
    the API's pence). A resolved rate outside a plausible band is rejected
    (logged) rather than applied, guarding against a units surprise.

    repaired=True when the resolver supplied the rate, so the caller can flag
    the block. Never returns a silent zero when a resolver could help: if the
    resolver yields nothing, the original (possibly zero) rate is returned and
    the block stays flagged for the tooling.
    """
    stored = channel.get("rate", 0.0) or 0.0
    if stored and abs(stored) >= 1e-6:
        return stored, False
    if rate_resolver is None:
        return stored, False
    try:
        resolved = rate_resolver(channel_name, block_start)
    except Exception as e:
        logger.warning("_resolve_block_rate: resolver error %s/%s: %s",
                       channel_name, block_start, e)
        return stored, False
    if resolved is None:
        return stored, False
    # Sanity band for a UK £/kWh rate (import or export). Reject wild values
    # (e.g. pence not converted → 24.5) rather than corrupt a block.
    if not (-2.0 <= resolved <= 2.0):
        logger.warning("_resolve_block_rate: rejected implausible rate %.4f "
                       "for %s/%s (units?)", resolved, channel_name, block_start)
        return stored, False
    logger.info("_resolve_block_rate: repaired %s rate at %s → %.4f £/kWh",
                channel_name, block_start, resolved)
    return resolved, True


def _recompute_pass3_totals(block: dict) -> None:
    """PASS 3 — re-derive a block's import/export totals from its (already
    PASS-2'd) per-meter channels: sub-meters contribute kwh_grid, the main
    contributes its kwh_remainder. Mirrors finalise_block PASS 3 and is shared
    by the settlement rerun and the post-delete remainder recompute so the two
    can never drift."""
    block.setdefault("totals", {})
    block["totals"].update({"import_kwh": 0.0, "import_cost": 0.0,
                            "export_kwh": 0.0, "export_cost": 0.0})
    for meter_name, meter_block in block["meters"].items():
        meta = meter_block.get("meta", {}) or {}
        for channel_name, channel in (meter_block.get("channels") or {}).items():
            if channel_name == "import":
                if meta.get("sub_meter"):
                    ik = channel.get("kwh_grid")
                    if ik is None:
                        ik = channel.get("kwh") or 0.0
                    block["totals"]["import_kwh"]  += ik or 0.0
                    block["totals"]["import_cost"] += channel.get("cost") or 0.0
                else:
                    ik = channel.get("kwh_remainder")
                    if ik is None:
                        ik = channel.get("kwh") or 0.0
                    ic = channel.get("cost_remainder")
                    if ic is None:
                        ic = channel.get("cost") or 0.0
                    block["totals"]["import_kwh"]  += ik or 0.0
                    block["totals"]["import_cost"] += ic or 0.0
            elif channel_name == "export":
                block["totals"]["export_kwh"]  += channel.get("kwh") or 0.0
                block["totals"]["export_cost"] += channel.get("cost") or 0.0


async def recompute_remainders_for_window_async(parent_meter_id: str, utc_start: str,
                                                utc_end: str, *, every: int = 50) -> int:
    """`recompute_remainders_for_window`, yielding to the loop every `every` blocks: over a
    device's whole history it is tens of thousands of blocks, and run straight through it held
    the loop for 104 s on an SSD — long enough for HA's WebSocket to drop (4 Nov 2026, b7)."""
    if not parent_meter_id:
        return 0
    starts = get_store().get_block_starts_in_range(parent_meter_id, utc_start, utc_end)
    done = 0
    for i in range(0, len(starts), every):
        chunk = starts[i:i + every]
        done += recompute_remainders_for_window(parent_meter_id, chunk[0],
                                                (chunk[-1] + "\x7f"), quiet_total=True)
        await asyncio.sleep(0)
    logger.info("recompute_remainders_for_window: recomputed %d block(s) for parent %s",
                done, parent_meter_id)
    return done


def recompute_remainders_for_window(parent_meter_id: str, utc_start: str,
                                    utc_end: str, *, quiet_total: bool = False) -> int:
    """Re-run PASS 2 over a main meter's blocks after one of its sub-meters' blocks were
    deleted, so the surviving devices' grid shares, the totals and carbon are re-derived.
    (Step 5, 10 Oct 2026: the main no longer stores a remainder to reset; the house is
    derived where read, max(0, main − Σ grid shares).)

    For each affected parent block in [utc_start, utc_end) this reconstructs the
    block (the deleted device is already absent), re-runs PASS 2 against the
    surviving sub-meters — reattributing the deleted device's grid-attributed
    energy back into the remainder — then re-derives PASS 3 totals and carbon
    and writes it back. Returns the number of parent blocks recomputed.

    Uses _apply_pass2 directly, NOT _rerun_pass2_for_settled_block: the
    remainder is a PASS-2 concern only. The settlement rerun would also
    materialise DCC/CAD kWh, capture kwh_cad, apply the dispatch overlay and
    clear is_provisional — all of which would be wrong to trigger from a delete.
    Recomputing a window that was not actually affected is a no-op (the same
    surviving subs yield the same remainder), so the bounds can be generous.
    """
    if not parent_meter_id:
        return 0
    global _pass2_quiet
    _prev_quiet = _pass2_quiet
    _pass2_quiet = True          # a whole-history delete can hit tens of thousands of blocks
    store = get_store()
    starts = store.get_block_starts_in_range(parent_meter_id, utc_start, utc_end)
    done = 0
    for bs in starts:
        try:
            block = store.get_block_dict_by_start(bs)
            if not block:
                continue
            _apply_pass2(block)
            _recompute_pass3_totals(block)
            _recompute_block_carbon(block)
            append_block_replace(block)
            done += 1
        except Exception as e:
            logger.error(
                "recompute_remainders_for_window: block %s failed: %s", bs, e)
    _pass2_quiet = _prev_quiet
    if not quiet_total:
        logger.info(
            "recompute_remainders_for_window: recomputed %d block(s) for parent %s",
            done, parent_meter_id)
    return done


_backout_job: dict = {"status": "idle"}
_backout_lock = threading.Lock()   # atomic single-flight guard (endpoint runs on a Flask worker thread)


def api_backout_status() -> dict:
    """JSON-safe snapshot of the back-out job (for the UI to disable/poll)."""
    return dict(_backout_job)


def backout_forget_last() -> None:
    """A new undo is starting: the page polls for ITS result, not the one before."""
    _backout_job.pop("last", None)


def backout_running() -> bool:
    return _backout_job.get("status") == "running"


def backout_recorder_attribution(**kw) -> dict:
    """`backout_recorder_attribution_async`, run to completion here — for a caller with no
    running loop (tests, a script). The route runs the async one on the engine loop."""
    return asyncio.run(backout_recorder_attribution_async(**kw))


async def backout_recorder_attribution_async(**kw) -> dict:
    """The undo, its result kept on the status (`last`) for a page that started it and polls."""
    res = await _backout_impl(**kw)
    if res.get("error") != "backout_in_progress":
        _backout_job["last"] = dict(res, meter_id=kw.get("meter_id"), run_id=kw.get("run_id"))
    return res


async def _backout_impl(*, run_id=None, meter_id=None, from_date=None, to_date=None,
                        whole: bool = False) -> dict:
    """Undo a recorder-attribution run (or an explicit device + range): delete the
    reconstructed 'recorder_attributed' device blocks, re-derive each affected parent
    meter's remainder over the span, and drop the run from the ledger. The reversible
    safety net — fast, surgical, and it never touches live or imported house totals
    (only the additive device layer is removed; the remainder is re-derived from the
    surviving sub-meters). Returns {ok, deleted, parents, recomputed, from, to}.

    Guarded two ways so a stray double-click can't misfire:
      • only ONE back-out runs at a time — a second call while one is in flight
        returns {ok:False, error:'backout_in_progress'} rather than racing it;
      • a run_id that isn't (or is no longer) in the ledger returns
        {ok:False, error:'run_not_found'} instead of falling through to an
        all-None delete that would wipe the whole attributed layer.

    `whole` (Clear history, 6 Nov 2026): a device with no sensor left loses everything it holds,
    live and imported half-hours and its kept readings too, so a later refill cannot bring them back.
    The device stays, and so does its Overview card. Refused while it has any sensor."""
    from datetime import datetime as _dt, timedelta as _td
    # Atomic single-flight: if a back-out is already running, refuse rather than
    # race it on the store's single DB connection (the exact double-undo crash).
    if not _backout_lock.acquire(blocking=False):
        return {"ok": False, "error": "backout_in_progress",
                "in_progress": dict(_backout_job)}
    try:
        store = get_store()
        from_iso = from_date
        to_iso = to_date
        if run_id:
            matched = next((r for r in store.get_attribution_runs()
                            if r.get("run_id") == run_id), None)
            if matched is None:
                # Already undone, or never existed — do NOT proceed with empty filters.
                return {"ok": False, "error": "run_not_found", "run_id": run_id}
            meter_id = meter_id or matched.get("meter_id")
            from_iso = from_iso or matched.get("from")
            to_iso = to_iso or matched.get("to")
        _backout_job.clear()
        _backout_job.update({"status": "running", "run_id": run_id,
                             "meter_id": meter_id, "started_at": _dt_now_iso_safe()})
        if whole:
            if not meter_id or store.device_stitch_sensors(meter_id):
                return {"ok": False, "error": "has_sensor"}
            off = store.device_off_stitch_sensors(meter_id)
            d = store.drop_device_history(meter_id, [o["id"] for o in off], everything=True)
            res = {"deleted": d["deleted_blocks"], "parents": d["parents"], "from": d["from"], "to": d["to"]}
            for r in store.get_attribution_runs():
                if r.get("meter_id") == meter_id and r.get("run_id"):
                    store.remove_attribution_run(r["run_id"])
        else:
            res = store.delete_recorder_attributed(
                meter_id=meter_id, from_iso=from_iso, to_iso=to_iso)
        lo, hi = res.get("from"), res.get("to")
        recomputed = 0
        if lo and hi:
            try:
                hi_excl = (_dt.fromisoformat(hi)
                           + _td(minutes=int(get_block_minutes() or 30))).isoformat()
            except Exception:
                hi_excl = hi
            for parent in (res.get("parents") or []):
                # yielding: over a whole history, straight through, it stalled the loop
                # long enough to drop HA's WebSocket (4 Nov 2026)
                recomputed += await recompute_remainders_for_window_async(parent, lo, hi_excl)
                store = get_store()          # never held across an await: a reconnect reopens it
        if run_id:
            store.remove_attribution_run(run_id)
        logger.info("backout_recorder_attribution: deleted %d device block(s), "
                    "recomputed %d parent block(s) (run=%s meter=%s)",
                    res.get("deleted"), recomputed, run_id, meter_id)
        return {"ok": True, "deleted": res.get("deleted", 0),
                "parents": res.get("parents") or [], "recomputed": recomputed,
                "from": lo, "to": hi}
    except Exception as e:
        logger.error("backout_recorder_attribution failed (run=%s): %s", run_id, e, exc_info=e)
        # A code, not the exception's text: the route answers the page with it (CodeQL, 2 Nov 2026).
        return {"ok": False, "error": "backout_failed"}
    finally:
        _last = _backout_job.get("last")
        _backout_job.clear()
        _backout_job.update({"status": "idle"})
        if _last:
            _backout_job["last"] = _last
        _backout_lock.release()


def _replace_state(meter_id: str, **fields) -> None:
    """The replace's own state on its run entry, for the card: `phase` while it is still the
    claim (fetching, dropping, the house), or the error that ended it."""
    with _attribution_lock:
        j = _attribution_jobs.setdefault(meter_id, {"device": meter_id})
        j.update(fields)


def _replace_refused(meter_id: str, error: str, code: str) -> dict:
    """End a replace before its run, nothing dropped: the error on the card, the claim freed."""
    _replace_state(meter_id, status="error", error=error, phase=None)
    _coord.release("attribution", meter_id)            # the claim never became a run
    return {"ok": False, "error": code}


async def replace_device_history(meter_id: str, sensor_ids: list) -> dict:
    """Replace a device's history from the sensors it uses now (replace device history design
    §2) — the route has claimed the device and checked those sensors.

    1. **Fetch first.** HA's statistics for the sensors in use, kept as recorder hours. A fetch
       that fails, or finds nothing, ends it there: nothing dropped (4 Nov 2026, b7 — the drop
       ran first, the fetch then went out on a WebSocket the drop's stall had dropped, and the
       device was left with no history).
    2. **The drop** (`drop_device_history`): the rebuilt half-hours, and what the sensors it no
       longer uses recorded.
    3. **The house** over the span, again — yielding, so the loop and HA's WebSocket keep going
       (`recompute_remainders_for_window_async`); the device's runs leave the ledger.
    4. **The rebuild** from the kept hours (`run_attribution_job`, no second fetch).

    Never raises; a failure is the run's error, on its card."""
    from datetime import datetime as _dt, timedelta as _td
    if _engine_ha is None:
        return _replace_refused(meter_id, "Home Assistant is not connected — nothing was replaced",
                                "no_ha")
    try:
        _replace_state(meter_id, phase="Asking Home Assistant for the history of " +
                       ", ".join(sensor_ids) + "…")
        now = datetime.now(timezone.utc)
        series, failed = await _fetch_device_series(
            sensor_ids, (now - timedelta(days=3660)).isoformat(), now.isoformat())
        if failed:
            return _replace_refused(meter_id, "Home Assistant didn't answer for " + ", ".join(failed) +
                                    " — nothing was replaced; try again", "fetch_failed")
        src_of: dict = {}
        fetched = _stitch_hourly_energy(series, list(sensor_ids), sources=src_of)
        if not fetched:
            return _replace_refused(meter_id, "Home Assistant has no history for " +
                                    ", ".join(sensor_ids) + " — nothing was replaced", "no_data")
        get_store().record_recorder_hours(meter_id, "import", fetched, src_of)
        _replace_state(meter_id, phase="Dropping the old history…")
        off = get_store().device_off_stitch_sensors(meter_id)
        res = get_store().drop_device_history(meter_id, [o["id"] for o in off])
        lo, hi = res.get("from"), res.get("to")
        if lo and hi:
            _replace_state(meter_id, phase="Giving the house back its share…")
            hi_excl = (_dt.fromisoformat(hi) + _td(minutes=int(get_block_minutes() or 30))).isoformat()
            for parent in res.get("parents") or []:
                await recompute_remainders_for_window_async(parent, lo, hi_excl)
        for r in get_store().get_attribution_runs():
            if r.get("meter_id") == meter_id and r.get("run_id"):
                get_store().remove_attribution_run(r["run_id"])
        logger.info("replace_device_history: %s — %d hour(s) kept from %s; dropped %d half-hour(s) "
                    "and %d read(s) (%s); rebuilding", meter_id, len(fetched), ", ".join(sensor_ids),
                    res.get("deleted_blocks", 0), res.get("deleted_reads", 0),
                    ", ".join(o["entity_id"] for o in off) or "no sensor it no longer uses")
    except Exception as e:
        logger.error("replace_device_history failed (%s): %s", meter_id, e, exc_info=e)
        return _replace_refused(meter_id, "the history could not be replaced", "replace_failed")
    _replace_state(meter_id, phase=None)
    return await run_attribution_job(meter_id, sensor_ids, pace_s=0.05, fetch=False)


def _stitch_hourly_energy(series_by_sensor: dict, priority: list,
                          sources: dict | None = None) -> dict:
    """Combine one or more cumulative-kWh statistic series into a single hourly
    ENERGY map {naive-UTC hour ISO: kWh}. Pure.

    Per sensor, the hour energy is the HA `change` if present, else the delta of
    consecutive `sum` values; a DECREASE in `sum` is a counter reset (device swap /
    integration reset) → that step is dropped and the series continues. Sensors are
    tried in `priority` order and the FIRST to supply an hour wins, so listing
    [new_sensor, old_sensor] stitches a rename: the new sensor owns recent hours,
    the old fills the earlier ones. Each hour's energy is keyed to the hour it
    belongs to; alignment to the two half-hour blocks happens in _split_hour_to_blocks.
    `sources`, when given, is filled with the sensor that supplied each hour."""
    import statistics_probe as _sp
    out: dict = {}
    for sensor in (priority or []):
        parsed = []
        for r in (series_by_sensor.get(sensor) or []):
            t = _sp._to_utc(r.get("start"))
            if t is None:
                continue
            hour = t.replace(minute=0, second=0, microsecond=0, tzinfo=None)
            parsed.append((hour, r))
        parsed.sort(key=lambda x: x[0])
        prev_sum = None
        for hour, r in parsed:
            s = r.get("sum")
            ch = r.get("change")
            e = None
            if ch is not None:
                try:
                    e = float(ch)
                except (TypeError, ValueError):
                    e = None
            elif s is not None and prev_sum is not None:
                d = s - prev_sum
                if d >= 0:
                    e = d
            if s is not None:
                prev_sum = s
            key = hour.isoformat()
            if e is not None and e >= 0 and key not in out:
                out[key] = round(float(e), 6)
                if sources is not None:
                    sources[key] = sensor
    return out


def _split_hour_to_blocks(hour_kwh, house_imports: list) -> list:
    """Distribute an hour's measured device energy across its constituent blocks in
    proportion to the house IMPORT shape for those blocks — so a charge burst lands
    in the half-hour it actually happened, not smeared. `house_imports` = per-block
    house import kWh for the hour, in order; equal split when the house imported
    nothing that hour. Returns per-block device kWh, same length/order. Pure."""
    n = len(house_imports)
    if n == 0:
        return []
    e = float(hour_kwh or 0.0)
    tot = sum((x or 0.0) for x in house_imports)
    if tot <= 0:
        return [round(e / n, 6)] * n
    return [round(e * ((x or 0.0) / tot), 6) for x in house_imports]

def _write_device_into_block(block: dict, device_meter_id: str,
                             parent_meter_id: str, dev_kwh, *, kept: dict | None = None) -> bool:
    """Add a reconstructed device sub-meter to an existing house block and re-run
    PASS 2/3 so the device's grid share is clipped to the parent import and the
    parent remainder is re-derived. Tags ONLY the device row 'recorder_attributed'.
    Returns False (no write) when the parent isn't in the block, when a *real*
    (non-zero) device row is already present (never overwrite live/attributed
    consumption), or when the existing row is an empty hole but there's no recorder
    energy to put in it (nothing to heal — leave the genuine zero alone).

    A device row that is present but reads ~0 import is treated as a hole, not
    coverage: if the recorder says the device really drew energy in this slot
    (dev_kwh > 0) we OVERWRITE the zero with the reconstructed value. This heals
    sub-meter dropouts — a stretch where the device's own sensor flat-lined at 0
    while the load actually sat in the house remainder — without ever clobbering a
    real reading.

    `kept`: an `ha_reads` row being put back as it was (soft delete design §2) — a live
    half-hour returns as live (its registers, no source tag, the sensor that recorded it), a
    recorder half-hour as recorder_attributed. Its kWh is `dev_kwh`."""
    meters = block.get("meters") or {}
    if parent_meter_id not in meters:
        return False
    existing = meters.get(device_meter_id)
    if existing is not None:
        ex_kwh = ((existing.get("channels") or {}).get("import") or {}).get("kwh") or 0.0
        if abs(float(ex_kwh)) > 1e-9:
            return False                # real device reading present → never overwrite
        if not (dev_kwh and float(dev_kwh) > 1e-9):
            return False                # empty hole, but nothing to heal into it
        # else: zero-hole + real recorder energy → fall through and overwrite it
    live = bool(kept) and kept.get("origin") == "live"
    dev_block = {
        "meta": {"sub_meter": True, "parent_meter": parent_meter_id},
        "source": None if live else get_store().RECORDER_ATTRIBUTED_SOURCE,
        "channels": {"import": {"kwh": round(float(dev_kwh or 0.0), 6)}},
    }
    if live:
        dev_block["channels"]["import"].update(read_start=kept.get("read_start"),
                                               read_end=kept.get("read_end"))
    # Carbon: _recompute_block_carbon needs a per-meter carbon_intensity_g to
    # compute carbon_g at all (it skips meters with no stored intensity). A freshly
    # reconstructed device row has none, so inherit the intensity persisted on the
    # parent (house) meter for this same block — the grid CI is per-block, not
    # per-meter, so the parent's stored value is the correct one to apply.
    parent_ci = (meters.get(parent_meter_id) or {}).get("carbon_intensity_g")
    if parent_ci is not None:
        dev_block["carbon_intensity_g"] = parent_ci
    meters[device_meter_id] = dev_block
    block["meters"] = meters
    _apply_pass2(block)
    _recompute_pass3_totals(block)
    _recompute_block_carbon(block)
    append_block_replace(block)
    if live:
        # the sensor that recorded it — none where the read's sensor is only inferred (the v4
        # import named the primary for a block that named none): the block is put back as it was
        get_store().point_block_at_sensor(
            block.get("start"), device_meter_id,
            None if kept.get("sensor_inferred") else kept.get("sensor_id"))
    return True


def _attribute_hour(hour_iso: str, device_meter_id: str, parent_meter_id: str,
                    hour_kwh, *, block_minutes: int = 30, kept: dict | None = None) -> tuple:
    """Attribute one hour's measured device energy across its half-hour blocks,
    weighted by the house import shape, writing a recorder_attributed device block
    wherever one doesn't already exist. Returns (written, skipped, no_house).

    `kept` {half-hour start: `ha_reads` row} (soft delete design §2): a half-hour HA recorded
    live is put back as it was, not re-split; the rest of the hour — the recorder's hour less
    the kept half-hours — is split by the house shape over the others. `hour_kwh` None (no
    recorder hour): the kept half-hours alone."""
    from datetime import datetime as _dt, timedelta as _td
    store = get_store()
    try:
        h0 = _dt.fromisoformat(str(hour_iso))
    except ValueError:
        return (0, 0, 0)
    step = int(block_minutes or 30)
    nblocks = max(1, 60 // step)
    starts = [(h0 + _td(minutes=step * i)).isoformat() for i in range(nblocks)]
    blocks = [store.get_block_dict_by_start(bs) for bs in starts]
    house = []
    for b in blocks:
        pi = 0.0
        if b:
            pm = (b.get("meters") or {}).get(parent_meter_id)
            if pm:
                pi = ((pm.get("channels") or {}).get("import") or {}).get("kwh") or 0.0
        house.append(pi)
    kept = {bs: r for bs, r in (kept or {}).items() if bs in starts}
    rest = [i for i, bs in enumerate(starts) if bs not in kept]
    left = None
    if hour_kwh is not None:
        left = max(float(hour_kwh) - sum(float(r["kwh"] or 0.0) for r in kept.values()), 0.0)
    share = dict(zip(rest, _split_hour_to_blocks(left, [house[i] for i in rest])
                     if left is not None else []))
    written = skipped = no_house = 0
    for i, b in enumerate(blocks):
        row = kept.get(starts[i])
        if row is None and i not in share:
            continue                    # no figure for this half-hour
        if not b:                       # no house block → no control total
            no_house += 1
            continue
        if row is not None:
            ok = _write_device_into_block(b, device_meter_id, parent_meter_id, row["kwh"],
                                          kept=row)
        else:
            ok = _write_device_into_block(b, device_meter_id, parent_meter_id, share[i])
        if ok:
            written += 1
        else:
            skipped += 1                # already attributed / live
    return (written, skipped, no_house)


# One entry per device. Was a single dict, which made two problems at once:
# a second start CLOBBERED the first run's counters, run_id and device, and the
# one `control` key meant pausing either paused both.
#
# Attributing two devices concurrently is fine in itself — different meter_ids,
# disjoint rows, and the job already skips any block where the device has a row.
# It was only the bookkeeping that was single-tenant.
_attribution_jobs: dict = {}
_attribution_lock = threading.Lock()

_ACTIVE = ("claimed", "running", "paused")


def attribution_claim(meter_id: str, sensor_ids: list) -> bool:
    """Reserve `meter_id` for a run, atomically. False if it is already taken.

    THIS is the fix for the start-route window. The route used to check
    `attribution_running()` and then, still on the Flask thread, run a preflight
    (`timeout=180`) and zip the whole database before scheduling the coroutine —
    and the status only flipped to "running" INSIDE that coroutine. So the gap
    between "nobody is running" and "someone is running" was however long a
    preflight plus a database backup takes, and two starts arriving inside it
    both passed.

    Claiming synchronously, under a lock, closes it: the second caller is refused
    before it does any of that slow work. Callers MUST `attribution_release()` on
    every path that then fails to schedule the job.
    """
    with _attribution_lock:
        cur = _attribution_jobs.get(meter_id) or {}
        if cur.get("status") in _ACTIVE:
            return False
        # FIRST MEMBER MIGRATED (B §3.3). The coordinator now owns the fact that
        # this device is taken; `_attribution_jobs` keeps the run's counters and
        # control, because those are the UI's shape and workstream C owns that
        # surface. Two records of one thing would be the §3.4 failure again, so
        # the split is deliberate: coordinator = identity and admission,
        # `_attribution_jobs` = progress.
        ok, _why = _coord.claim("attribution", meter_id)
        if not ok:
            return False
        _attribution_jobs[meter_id] = {
            "status": "claimed", "device": meter_id,
            "sensors": list(sensor_ids or []), "control": "run",
            "written": 0, "skipped": 0, "gaps": 0,
            "done_hours": 0, "total_hours": 0,
            "started_at": _dt_now_iso_safe(),
        }
        return True


def attribution_release(meter_id: str) -> None:
    """Drop a claim that never became a run (preflight refused, backup failed)."""
    with _attribution_lock:
        if (_attribution_jobs.get(meter_id) or {}).get("status") == "claimed":
            _attribution_jobs.pop(meter_id, None)
            _coord.release("attribution", meter_id)


def api_attribution_status(meter_id: str = None) -> dict:
    """JSON-safe snapshot.

    Shape is backward compatible on purpose: the existing page polls a FLAT
    object (`status`, `device`, `done_hours`, `written`, ...) and has one
    pause/cancel pair. So the active run — or the most recent one — is still
    returned flat, with every run alongside it under `runs`. Workstream C
    rewrites this surface; until then the page keeps working untouched.
    """
    with _attribution_lock:
        runs = [{k: v for k, v in j.items() if k != "task"}
                for j in _attribution_jobs.values()]
    if meter_id is not None:
        one = next((r for r in runs if r.get("device") == meter_id), {"status": "idle"})
        return dict(one, runs=runs)
    active = [r for r in runs if r.get("status") in _ACTIVE]
    if active:
        flat = active[0]
    elif runs:
        flat = sorted(runs, key=lambda r: r.get("started_at") or "")[-1]
    else:
        flat = {"status": "idle"}
    return dict(flat, runs=runs)


def attribution_running(meter_id: str = None) -> bool:
    """Any run active, or that device's run. The no-argument form keeps its old
    meaning, because the back-out route uses it to mean "anything at all"."""
    with _attribution_lock:
        jobs = list(_attribution_jobs.values())
    if meter_id is not None:
        jobs = [j for j in jobs if j.get("device") == meter_id]
    return any(j.get("status") in _ACTIVE for j in jobs)


def attribution_control(action: str, meter_id: str = None) -> dict:
    """pause / resume / cancel (mirrors the import controls).

    `meter_id` is optional: with one run active this addresses it, which is what
    the current page sends. With several and no meter_id it addresses ALL of
    them — deliberate, because the alternative is silently picking one.
    """
    ctl = {"pause": "pause", "resume": "run", "cancel": "cancel"}.get(action)
    if ctl is None:
        return api_attribution_status()
    with _attribution_lock:
        for mid, j in _attribution_jobs.items():
            if meter_id is not None and mid != meter_id:
                continue
            if j.get("status") in _ACTIVE:
                j["control"] = ctl
    return api_attribution_status(meter_id)


async def attribution_preflight(device_meter_id: str, sensor_ids: list) -> dict:
    """Sanity-check a device's sensors BEFORE attributing: stitch their history to
    a total kWh and compare it to the house import over the same window. A device
    can never draw more than the house it's part of, so device > house means the
    wrong sensor (or a Wh/kWh unit slip that inflates it ~1000×). Returns
    {ok, verdict, device_kwh, house_kwh, from, to} — verdict is 'ok',
    'device_exceeds_house', 'suspiciously_small', or 'no_data'. Never raises."""
    try:
        # Fetch the store fresh at each use — never hold a handle across the recorder
        # fetch below: a config save / HA reconnect reopens _store mid-await and would
        # leave a captured handle closed ("Cannot operate on a closed database").
        if _engine_ha is None:
            return {"ok": True, "verdict": "no_ha", "device_kwh": None, "house_kwh": None}
        parent = get_store().get_parent_meter_id(device_meter_id) or "electricity_main"
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=3660)).isoformat()
        series = {}
        for sid in (sensor_ids or []):
            try:
                res = await _engine_ha.get_statistics([sid], start, now.isoformat(),
                                                      period="hour", timeout=120.0)
                series[sid] = res.get(sid) or []
            except Exception:
                series[sid] = []
        hourly = _stitch_hourly_energy(series, list(sensor_ids or []))
        if not hourly:
            return {"ok": True, "verdict": "no_data", "device_kwh": 0.0, "house_kwh": None}
        dev_kwh = round(sum(hourly.values()), 2)
        lo, hi = min(hourly.keys()), max(hourly.keys())
        house_kwh = round(get_store().sum_meter_import_kwh(parent, lo, hi), 2)
        verdict = "ok"
        if house_kwh > 0 and dev_kwh > house_kwh * 1.05:
            verdict = "device_exceeds_house"        # wrong sensor / Wh-as-kWh
        elif house_kwh > 0 and 0 < dev_kwh < house_kwh * 0.0005:
            verdict = "suspiciously_small"          # possible kWh-as-Wh / wrong sensor
        return {"ok": True, "verdict": verdict, "device_kwh": dev_kwh,
                "house_kwh": house_kwh, "from": lo, "to": hi}
    except Exception as e:
        logger.warning("attribution_preflight failed: %s", e)
        return {"ok": True, "verdict": "error", "device_kwh": None, "house_kwh": None}


async def _fetch_device_series(sensor_ids: list, start: str, end: str) -> tuple:
    """HA's hourly statistics for each sensor: ({sensor: rows}, [sensors whose fetch failed]).
    A sensor with no statistics is not a failure — its rows are []."""
    series, failed = {}, []
    for sid in (sensor_ids or []):
        try:
            res = await _engine_ha.get_statistics([sid], start, end, period="hour", timeout=180.0)
            series[sid] = res.get(sid) or []
        except Exception as e:
            logger.warning("run_attribution_job: stats fetch %s failed: %r", sid, e)
            series[sid] = []
            failed.append(sid)
    return series, failed


@job("attribution", depends_on=("import",),
     exclusive_with=("delete",), on_conflict=WAIT)
async def run_attribution_job(device_meter_id: str, sensor_ids: list, *,
                              pace_s: float = 0.0, window: tuple | None = None,
                              fetch: bool = True) -> dict:
    """Background: attribute a device's HA recorder history onto the house blocks it
    isn't on yet. Fetch stats (on the loop's WS listener), stitch to hourly energy,
    walk oldest-first writing recorder_attributed device blocks — skipping any block
    where the device already has a row (never overwrites live). Cooperative: yields
    between hours and PAUSES while a delete/purge runs, so it coexists with the live
    tick and the pass-2 settlement drain. Records a ledger run. Never raises.

    It rebuilds from `ha_reads` too (soft delete design §2): the hours it keeps, and the
    half-hours HA recorded live, which go back as they were. `fetch=False` asks HA's recorder
    nothing — a refill's rebuild (`rebuild_devices_after_refill`); `window` (lo, hi) bounds
    it to the refilled span."""
    import asyncio as _aio
    # This device's own entry — created by `attribution_claim()` when the route
    # reserved it, or here if the job was started directly (tests, a future
    # scheduler). Never `clear()`: that is what let a second start wipe the
    # first run's counters.
    with _attribution_lock:
        j = _attribution_jobs.setdefault(device_meter_id, {})
    # NB: always fetch the store fresh via get_store() at each use — never capture it
    # across an `await`. engine_startup (a config save OR an HA WebSocket reconnect)
    # closes and reopens _store, so a handle captured before the long recorder fetch
    # would be closed by the time we used it → "Cannot operate on a closed database".
    _coord.mark("attribution", device_meter_id, status="running")
    j.update({"status": "running", "phase": "fetching",
              "control": j.get("control") or "run",
              "device": device_meter_id, "sensors": list(sensor_ids or []),
              "written": 0, "skipped": 0, "gaps": 0, "done_hours": 0,
              "total_hours": 0,
              "started_at": j.get("started_at") or _dt_now_iso_safe()})
    run_id = _dt_now_iso_safe() + "|" + device_meter_id
    try:
        if fetch and _engine_ha is None:
            j.update({"status": "error", "error": "no HA connection"})
            _coord.mark("attribution", device_meter_id, status="error")
            return {"ok": False, "error": "no HA connection"}
        parent = get_store().get_parent_meter_id(device_meter_id) or "electricity_main"
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=3660)).isoformat()
        series, failed = (await _fetch_device_series(sensor_ids, start, now.isoformat())
                          if fetch else ({}, []))
        if fetch and sensor_ids and len(failed) == len(sensor_ids):
            # every sensor's fetch failed: not a rebuild that found nothing (4 Nov 2026, a
            # replace whose fetch went out on a WebSocket that had just dropped, and said "done")
            j.update({"status": "error", "error": "Home Assistant didn't answer for " +
                      ", ".join(failed) + " — try again"})
            _coord.mark("attribution", device_meter_id, status="error")
            return {"ok": False, "error": "fetch_failed"}
        src_of: dict = {}
        fetched = _stitch_hourly_energy(series, list(sensor_ids or []), sources=src_of) \
            if fetch else {}
        # The hours as HA's recorder gave them, kept as hours (soft delete design §2): the
        # split below is by the house import the blocks hold now, and is redone from these.
        if fetched:
            try:
                n_kept = get_store().record_recorder_hours(device_meter_id, "import", fetched,
                                                           src_of)
                logger.info("run_attribution_job: device=%s kept %d recorder hours",
                            device_meter_id, n_kept)
            except Exception as e:
                logger.warning("run_attribution_job: keeping recorder hours failed: %s", e)
        # What EMT already holds: the hours kept before (a fetch's own win where both), and
        # the half-hours to put back as they were.
        lo, hi = window if window else (None, None)
        stored, fixed = get_store().ha_reads_for(device_meter_id, "import",
                                                 lo[:13] + ":00:00" if lo else None, hi)
        hourly = {**stored, **fetched}
        if window:
            hourly = {h: k for h, k in hourly.items()
                      if h >= lo[:13] + ":00:00" and h < hi}
            fixed = {bs: r for bs, r in fixed.items() if lo <= bs < hi}
        kept_by_hour: dict = {}
        for bs, r in fixed.items():
            kept_by_hour.setdefault(bs[:13] + ":00:00", {})[bs] = r
        hours = sorted(set(hourly) | set(kept_by_hour))          # oldest first
        # Seam: where this device's REAL (live/imported) history begins in EMT.
        # Attribution fills up to — but not into — that boundary so the
        # reconstructed layer butts cleanly against the live one (no overlap, no
        # gap), and we stop walking at the seam rather than running to 'now'.
        # When the device has no live history yet (never configured), seam is None
        # and we fill the full available range.
        # BL-49: the seam (device's first live block) is kept for diagnostics only —
        # it must NOT be a blanket cutoff. Filtering to `< seam` made attribution skip
        # the entire live region, so a gap deleted after the device started recording
        # (a delete-data + re-attribute heal, or BL-47 gap-fill) could never be
        # refilled — it wrote 0. The per-block guard in _write_device_into_block already
        # protects live data: it fills only an ABSENT row or a zero-hole and never
        # overwrites a real non-zero device reading. So we walk the full recorded range
        # and let that guard decide, healing mid/late holes too without clobbering live.
        seam = get_store().get_device_live_coverage_start(device_meter_id)
        j["seam"] = seam
        bm = int(get_block_minutes() or 30)
        j.update({"phase": "attributing", "total_hours": len(hours)})
        written = skipped = gaps = 0
        global _pass2_quiet
        _prev_p2q = _pass2_quiet
        _pass2_quiet = True          # bulk attribution writes thousands of blocks — suppress the per-block PASS 2 INFO churn
        for i, hour in enumerate(hours):
            if j.get("control") == "cancel":
                j["status"] = "cancelled"
                _coord.mark("attribution", device_meter_id, status="cancelled")
                break
            # THE FIRST MEMBER TO CONSUME THE COORDINATOR, rather than only feed
            # it. This was three hand-rolled predicate calls —
            # `delete_in_progress() or api_import_running() or _verify_running()`
            # — and the reason for each lived in the comment above them:
            #
            #   "both write the pricing the attribution reads, and attributing
            #    over a not-yet-verified (imp_rate NULL) block used to crash
            #    PASS 2 and could cost the device at 0."
            #
            # That reason is now the DECLARATION on this job — depends_on
            # ("import",), exclusive_with ("delete") — so the
            # loop asks one question instead of naming three jobs. Adding a
            # fourth blocker becomes a declaration rather than an edit here, and
            # `validate()` catches a blocker that names nothing declared.
            #
            # The member's OWN pause control stays separate: a user pausing this
            # run is not a dependency, and conflating the two would make
            # `admission()` answerable by the UI.
            while True:
                if j.get("control") == "pause":
                    j["status"] = "paused"
                    j["phase"] = "paused by request"
                else:
                    _decision, _blocker = _coord.admission("attribution")
                    if _decision == ADMIT:
                        break
                    # Better than the old loop could manage: the page can now say
                    # WHAT it is waiting for, because the coordinator knows.
                    j["status"] = "paused"
                    j["phase"] = "waiting for %s" % _blocker
                await _aio.sleep(1.0)
                if j.get("control") == "cancel":
                    break
            if j.get("control") == "cancel":
                j["status"] = "cancelled"
                _coord.mark("attribution", device_meter_id, status="cancelled")
                break
            j["status"] = "running"
            w, s, nh = _attribute_hour(hour, device_meter_id, parent,
                                       hourly.get(hour), block_minutes=bm,
                                       kept=kept_by_hour.get(hour))
            written += w; skipped += s; gaps += nh
            if (i % 200) == 0:
                j.update({"written": written, "skipped": skipped, "gaps": gaps,
                          "done_hours": i})
                await _aio.sleep(pace_s)        # yield to the live tick / drain
        _pass2_quiet = _prev_p2q
        j.update({"written": written, "skipped": skipped, "gaps": gaps,
                  "done_hours": len(hours)})
        if written:
            get_store().record_attribution_run({
                "run_id": run_id, "meter_id": device_meter_id,
                "sensor_ids": list(sensor_ids or []),
                "from": hours[0] if hours else None,
                "to": hours[-1] if hours else None,
                "blocks_written": written, "created_at": _dt_now_iso_safe()})
        if j.get("status") != "cancelled":
            j["status"] = "done"
            _coord.mark("attribution", device_meter_id, status="done")
        j["run_id"] = run_id
        logger.info("run_attribution_job: device=%s wrote %d, skipped %d, gaps %d "
                    "(status=%s)", device_meter_id, written, skipped, gaps, j["status"])
        return {"ok": True, "written": written, "skipped": skipped, "gaps": gaps,
                "run_id": run_id, "status": j["status"]}
    except Exception as e:
        logger.warning("run_attribution_job failed: %s", e, exc_info=e)
        j.update({"status": "error", "error": _error_text("attribution", e)})
        _coord.mark("attribution", device_meter_id, status="error")
        return {"ok": False, "error": j["error"]}
    finally:
        _pass2_quiet = False          # never leave bulk-suppression on after the job


_REFILL_REBUILD_POLL_S = 5.0         # how often a queued refill rebuild asks to be admitted
_REFILL_REBUILD_WAIT_S = 3600.0      # and for how long, before it gives up (logged)


_SANITY_HELD = ("device_exceeds_house", "suspiciously_small")


async def _rebuild_when_admitted(mid: str, sensors: list, lo: str, hi: str, *,
                                 fetch: bool = False) -> None:
    """Claim `mid`'s attribution run once the coordinator admits it, then run it from `ha_reads`.
    The claim is refused while a member it depends on runs — the refill's own import, then the
    pricing verify — so it is retried, not given up on (staging, 20 Oct 2026: the gap fill's
    finish runs while its fill still holds "import", and every device's rebuild was refused).
    A run of the device's own that starts meanwhile is left to finish the job.

    `fetch`: a device whose history EMT never had, after an import (device history on first
    import design §3) — its whole history from HA's recorder, not the span from `ha_reads`. The
    sensor is checked against the house first, as *Rebuild history* checks it; a sensor that
    fails is not rebuilt but held, on the device's card, for the user to look at."""
    waited = 0.0
    while True:
        cur = (_attribution_jobs.get(mid) or {}).get("status")
        if cur in _ACTIVE:
            logger.info("rebuild_devices_after_refill: %s already rebuilding — left to its run", mid)
            return
        if attribution_claim(mid, sensors):
            break
        if waited >= _REFILL_REBUILD_WAIT_S:
            logger.warning("rebuild_devices_after_refill: %s not admitted after %.0f s (%s) — "
                           "use Rebuild history on its card", mid, waited,
                           _coord.admission("attribution")[1])
            return
        await asyncio.sleep(_REFILL_REBUILD_POLL_S)
        waited += _REFILL_REBUILD_POLL_S
    if fetch:
        pf = await attribution_preflight(mid, sensors)
        if pf.get("verdict") in _SANITY_HELD:
            attribution_release(mid)
            with _attribution_lock:
                _attribution_jobs[mid] = {"status": "held", "device": mid, "sensors": list(sensors),
                                          "verdict": pf.get("verdict"),
                                          "device_kwh": pf.get("device_kwh"),
                                          "house_kwh": pf.get("house_kwh"),
                                          "started_at": _dt_now_iso_safe()}
            logger.warning("rebuild_devices_after_refill: %s not rebuilt — its sensor reads %s kWh "
                           "against the house's %s kWh (%s); held for Rebuild history on its card",
                           mid, pf.get("device_kwh"), pf.get("house_kwh"), pf.get("verdict"))
            return
        logger.info("rebuild_devices_after_refill: %s — its history EMT never had, from HA's "
                    "recorder", mid)
        await run_attribution_job(mid, sensors, pace_s=0.05)
        return
    logger.info("rebuild_devices_after_refill: %s %s..%s — rebuilding from HA's reads", mid, lo, hi)
    await run_attribution_job(mid, sensors, pace_s=0.05, window=(lo, hi), fetch=False)


async def rebuild_devices_after_refill(lo: str, hi: str, *, fetch_new: bool = False) -> list:
    """After a refill of [lo, hi) — a gap fill or a CSV import, the main meter only — rebuild
    each device `ha_reads` holds figures for there, from those alone (soft delete design §3): no
    call to HA's recorder. Each device is queued (`_rebuild_when_admitted`): its run is the
    attribution job, claimed once the coordinator admits it — after the import and the pricing
    verify — and it fills only absent device rows. Returns the devices queued. Never raises.

    `fetch_new` (an import — Octopus's or a CSV — not a gap fill): a device whose history EMT
    never had (`devices_without_history`) is rebuilt from HA's recorder over its whole history
    instead, as *Rebuild history* would (device history on first import design §2)."""
    queued = []
    fresh = []
    if fetch_new:
        try:
            fresh = [m for m in get_store().devices_without_history()
                     if get_store().device_stitch_sensors(m)]
        except Exception as e:
            logger.warning("rebuild_devices_after_refill: devices without history: %s", e)
            fresh = []
    for mid in fresh:
        asyncio.create_task(_rebuild_when_admitted(mid, get_store().device_stitch_sensors(mid),
                                                   lo, hi, fetch=True))
        queued.append(mid)
    if fresh:
        logger.info("rebuild_devices_after_refill: %s — no history in EMT; queued from HA's "
                    "recorder, once the import and the pricing verify are done", ", ".join(fresh))
    try:
        devices = [m for m in get_store().ha_read_devices(lo, hi) if m not in fresh]
    except Exception as e:
        logger.warning("rebuild_devices_after_refill: %s", e)
        return queued
    if not devices and fresh:
        return queued
    if not devices:
        # said, so a refill whose devices stay empty can be told from one that never ran this
        logger.info("rebuild_devices_after_refill: %s..%s — no device reads kept there; "
                    "nothing to rebuild", lo, hi)
        return queued
    for mid in devices:
        try:
            sensors = get_store().device_stitch_sensors(mid)
        except Exception:
            sensors = []
        asyncio.create_task(_rebuild_when_admitted(mid, sensors, lo, hi))
        queued.append(mid)
    logger.info("rebuild_devices_after_refill: %s..%s — %s queued, from HA's reads, once the "
                "import and the pricing verify are done", lo, hi, ", ".join(devices))
    return queued


# ── The setup gap, from HA's recorder (docs/design/EMT-v5_setup_gap_from_ha_design.md) ─────────
_RECORDER_INTERPOLATED = 3      # BlockStore.RECORDER_INTERPOLATED
_SETUP_GAP_MODES = ("cad+api",)
_SETUP_GAP_STATS_TIMEOUT_S = 120.0


def _recorder_half_hours(rows: list, lo: datetime, hi: datetime) -> tuple:
    """HA's 5-minute statistics rows for one sensor → ({half-hour start: kWh}, {reason: count}).
    A half-hour is taken only with all six of its rows and the row before it (HA's first row after
    a hole carries the whole hole's energy), each change not negative, the total under the live
    path's ceiling. `change`, else the difference of `sum` from the row before. Naive UTC keys."""
    import statistics_probe as _sp
    by5: dict = {}
    for r in rows or []:
        t = _sp._to_utc(r.get("start"))
        if t is None:
            continue
        by5[t.replace(tzinfo=None, second=0, microsecond=0)] = r
    step5 = timedelta(minutes=5)
    energy: dict = {}
    for t in sorted(by5):
        r = by5[t]
        e = r.get("change")
        if e is None:
            prev = by5.get(t - step5)
            if prev is not None and r.get("sum") is not None and prev.get("sum") is not None:
                e = float(r["sum"]) - float(prev["sum"])
        if e is not None:
            energy[t] = float(e)
    out, refused = {}, {}

    def _no(why):
        refused[why] = refused.get(why, 0) + 1
    hh = lo
    while hh < hi:
        slots = [hh + step5 * i for i in range(6)]
        if any(x not in energy for x in slots):
            _no("incomplete")
        elif (hh - step5) not in by5:
            _no("after_hole")
        elif any(energy[x] < 0 for x in slots):
            _no("negative")
        else:
            kwh = sum(energy[x] for x in slots)
            if kwh > _ROGUE_BLOCK_KWH_CEILING:
                _no("ceiling")
            else:
                out[hh.isoformat()] = round(kwh, 6)
        hh += timedelta(minutes=30)
    return out, refused


_E7_CHECK_MIN_KWH = 0.05      # a half-hour's price is read from its cost only above this
_E7_CHECK_TOL = 0.15          # a price this far (relative) from the rule's is the other band


def check_e7_window(store, sched) -> "dict | None":
    """BL-102: does Economy 7's rule (night 00:30-07:30 UTC, kraken_rates) match what Octopus charged?
    Over the account's two-rate agreements, each half-hour with a cost from Octopus — its cached bill,
    or an API import's own cost — above `_E7_CHECK_MIN_KWH` is priced by the stitched schedule and the
    two compared (`sched`: the stitched import schedule, `_kraken_rate_schedules["import"]`). Says so in the log and changes nothing (flag, do not act): a meter that switches at
    other times shows as disagreements bunched at the window's edges. Returns {checked, disagree,
    at: {HH:MM UTC: n}} or None where there is nothing to check. Never raises."""
    try:
        from kraken_rates import _is_two_rate
        wins = [(vf, vt) for code, vf, vt in _agreement_windows("import") if _is_two_rate(code)]
        if not wins or sched is None or sched.is_empty():
            return None
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        checked, disagree, at = 0, 0, {}
        for vf, vt in wins:
            for f in store.get_settlement_facts(vf, vt or now):
                bk, bc = f.get("bill_kwh"), f.get("bill_cost")
                if bk is not None and bc is not None and bk > _E7_CHECK_MIN_KWH:
                    kwh, cost = float(bk), float(bc)
                elif (str(f.get("source") or "") == "imported_api" and f.get("imp_cost") is not None
                      and (f.get("imp_kwh") or 0) > _E7_CHECK_MIN_KWH):
                    kwh, cost = float(f["imp_kwh"]), float(f["imp_cost"])
                else:
                    continue
                try:
                    want = sched.resolve(f["block_start"])
                except Exception:
                    want = None
                if not want:
                    continue
                checked += 1
                got = cost / kwh * 100.0                 # p/kWh, as the schedule
                if abs(got - want) / want > _E7_CHECK_TOL:
                    disagree += 1
                    hm = f["block_start"][11:16]
                    at[hm] = at.get(hm, 0) + 1
        if not checked:
            return None
        res = {"checked": checked, "disagree": disagree,
               "at": dict(sorted(at.items(), key=lambda x: -x[1])[:8])}
        if disagree > max(2, 0.02 * checked):
            logger.warning("Economy 7: %d of %d half-hours Octopus charged at the other rate from "
                           "the night 00:30-07:30 UTC rule — the meter may switch at other times "
                           "(most at, UTC: %s). Nothing changed.", disagree, checked, res["at"])
        else:
            logger.info("Economy 7: the night 00:30-07:30 UTC rule matches Octopus's costs "
                        "(%d of %d half-hours disagree)", disagree, checked)
        return res
    except Exception as e:
        logger.warning("Economy 7 check not run: %s", e)
        return None


def _setup_gap_hi(fill: dict, hi: str) -> str:
    """A device rebuild's end, widened to take in the half-hours the setup gap fill wrote."""
    sp = (fill or {}).get("span")
    if not sp:
        return hi
    end = (datetime.fromisoformat(sp[1]) + timedelta(minutes=30)).isoformat()
    return max(hi, end)


async def fill_setup_gap_from_ha() -> dict:
    """Fill the main meter's half-hours between the imported history's end and the first live one
    — a new install's setup gap — from HA's 5-minute statistics for its CAD sensors, so they are
    predicted like every other half-hour until Octopus settles them (setup gap design §2). On
    `cad+api` only; only half-hours with no block; what HA cannot vouch for is left to the poll's
    BL-8 backfill, as before. Returns {filled, span [first, last] or None, refused, reason}.
    Never raises."""
    res = {"filled": 0, "span": None, "refused": {}, "reason": None}
    try:
        if get_data_source_mode() not in _SETUP_GAP_MODES:
            res["reason"] = "mode"
            return res
        if _engine_ha is None:
            res["reason"] = "no_ha"
            return res
        mid = _main_meter_id()
        meter = ((load_config().get("meters") or {}).get(mid) or {}) if mid else {}
        chans = meter.get("channels") or {}
        imp_sensor = (chans.get("import") or {}).get("read")
        exp_sensor = (chans.get("export") or {}).get("read")
        if not imp_sensor:
            res["reason"] = "no_sensor"
            return res
        store = get_store()
        ih = store.count_imported_history()
        if not ih.get("to"):
            res["reason"] = "no_import"
            return res
        lo = datetime.fromisoformat(ih["to"]) + timedelta(minutes=30)
        first_live = store.first_live_read_start(mid)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        cur = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
        hi = min(datetime.fromisoformat(first_live), cur) if first_live else cur
        if hi <= lo:
            res["reason"] = "no_gap"
            return res
        have = set(store.get_block_starts_in_range(mid, lo.isoformat(), hi.isoformat()))
        empty = []
        hh = lo
        while hh < hi:
            if hh.isoformat() not in have:
                empty.append(hh)
            hh += timedelta(minutes=30)
        if not empty:
            res["reason"] = "no_gap"
            return res
        f_lo, f_hi = empty[0], empty[-1] + timedelta(minutes=30)
        start = (f_lo - timedelta(minutes=5)).isoformat() + "+00:00"   # the row before the first
        end = f_hi.isoformat() + "+00:00"
        series = {}
        for ch, sid in (("import", imp_sensor), ("export", exp_sensor)):
            if not sid:
                continue
            got = await _engine_ha.get_statistics([sid], start, end, period="5minute",
                                                  types=["change", "sum"],
                                                  timeout=_SETUP_GAP_STATS_TIMEOUT_S)
            series[ch] = (got or {}).get(sid) or []
        imp, refused = _recorder_half_hours(series.get("import"), f_lo, f_hi)
        exp = None
        if exp_sensor:
            exp, _ = _recorder_half_hours(series.get("export"), f_lo, f_hi)
        want = {x.isoformat() for x in empty}
        half_hours = {bs: {"import": k, "export": (exp or {}).get(bs)}
                      for bs, k in imp.items() if bs in want}
        written = store.create_recorder_blocks(mid, half_hours)
        res.update(filled=len(written), refused=refused,
                   span=[written[0], written[-1]] if written else None)
        logger.info("setup gap: %s..%s — %d of %d empty half-hour(s) filled from HA's recorder "
                    "(%s); the rest wait for Octopus", f_lo.isoformat(), f_hi.isoformat(),
                    len(written), len(empty), refused or "none refused")
        return res
    except Exception as e:
        logger.warning("setup gap: not filled from HA's recorder: %s", e)
        res["reason"] = "error"
        return res


async def rebuild_devices_after_backfill(summary: dict) -> list:
    """After a poll that created half-hours (BL-8: Octopus settled them, EMT had none), rebuild
    the devices over them from `ha_reads`, as a gap fill does (`rebuild_devices_after_refill`).
    The backfill writes the main meter only; with nothing after it, a device's use there stayed
    the house's — the half-hours between an import's end and the first live tick on a new
    install, which the import's own rebuild cannot reach: they did not exist yet (5 Nov 2026).
    The rebuild fills only absent device rows, from HA's recorder hours EMT already keeps; no
    call to HA. Returns the devices queued. Never raises."""
    try:
        sp = (summary or {}).get("backfilled_span")
        if not (summary or {}).get("backfilled") or not sp:
            return []
        # an hour row is a device's from its start (`ha_read_devices`): from the hour
        lo = datetime.fromisoformat(sp[0]).replace(minute=0, second=0, microsecond=0)
        hi = datetime.fromisoformat(sp[1]) + timedelta(minutes=30)
        return await rebuild_devices_after_refill(lo.isoformat(), hi.isoformat())
    except Exception as e:
        logger.warning("rebuild_devices_after_backfill: %s", e)
        return []


def _rerun_settle(block: dict, main_meter_id: str = "electricity_main"):
    """settle() for the PASS 2 re-run: (Facts, Settled), or None where the half-hour is the user's
    (corrected) or settle() cannot price it (no rate, no band, nothing drawn). The kWh and the
    devices are the BLOCK's — the one the re-run is writing — not read back from the store."""
    bs = block.get("start", "")
    if _store is None or main_meter_id != "electricity_main":
        return None
    meters = block.get("meters") or {}
    main = meters.get(main_meter_id) or {}
    subs = []
    for mid, mb in meters.items():
        meta = mb.get("meta") or {}
        if not meta.get("sub_meter") or meta.get("parent_meter") != main_meter_id:
            continue
        imp = (mb.get("channels") or {}).get("import") or {}
        subs.append({"meter_id": mid, "imp_kwh": imp.get("kwh"), "meter_type": meta.get("meter_type")})
    try:
        a = _store.get_block_rate_authority(bs, main_meter_id)
        if a and (a["rate_corrected"] or a["rate_source"] == "corrected"):
            return None
        f, st = _settle_one(_store, bs, main={
            "imp_kwh_api": main.get("imp_kwh_api"),
            "interpolated": 1 if block.get("interpolated") else 0}, subs=subs)
    except Exception as e:
        logger.warning("_rerun_pass2: settle() failed for %s, the ladder prices it: %s", bs, e)
        return None
    if st is None or st.status not in ("settled", "predicted", "clock"):
        return None
    return f, st


def _rerun_pass2_for_settled_block(block: dict, main_meter_id: str = "electricity_main",
                                   rate_resolver=None, billing_source: str = "dcc",
                                   standing_resolver=None) -> dict:
    """Re-run PASS 2 + PASS 3b for a single block, materialising billing
    figures from the chosen source.

    billing_source (global toggle):
      - 'dcc' (default): billing kWh is the DCC settlement (imp_kwh_api /
        exp_kwh_api) where present, falling back to the CAD figure where DCC is
        absent — "settle to DCC except where missing".
      - 'cad': billing kWh is always the CAD figure, ignoring DCC. DCC values
        stay stored for drift/diagnostics, just unused for billing.

    Switching the toggle flags every block for re-run, so this re-materialises
    each block to the new source. The mechanism is identical both ways; only
    the kWh selection differs. The original CAD figure is preserved in kwh_cad
    on first settlement so a switch back to CAD can restore it exactly.

    rate_resolver (Chunk 5): consulted only when the chosen kWh has no rate.
    Returns the mutated block dict.
    """
    main = block.get("meters", {}).get(main_meter_id)
    if not main:
        return block
    _rate_repaired = False
    use_dcc = (billing_source != "cad")
    # settle()'s answer for the import, read again after PASS 2 below: set here, not only where the
    # main has an import channel (5 Nov 2026: a main block holding only its export raised
    # UnboundLocalError at every drain, the block left flagged and retried)
    _settled = None

    imp_ch = (main.get("channels") or {}).get("import")
    if imp_ch is not None:
        cad_kwh = imp_ch.get("kwh_cad")
        if cad_kwh is None:
            # First settlement: current kwh IS the CAD figure; preserve it so a
            # later switch back to CAD restores the original exactly.
            cad_kwh = imp_ch.get("kwh")
            imp_ch["kwh_cad"] = cad_kwh
        dcc_kwh = main.get("imp_kwh_api")
        chosen = dcc_kwh if (use_dcc and dcc_kwh is not None) else cad_kwh
        if chosen is not None and use_dcc and dcc_kwh is not None:
            # Engine step 4b (EMT-v5_engine_step4_settle_design.md §3, §4 (d)): the settled kWh is a
            # fact; the half-hour is settle()'s from its facts — a bill's band if Octopus has given
            # one, else EMT's prediction judged on THIS kWh, which may move it once across the
            # prediction's own thresholds (0.1 kWh, 0.4 kWh) and never on arrival order (#475's
            # defect, which hold_rate's freeze answered while the writers re-decided on what they
            # happened to know). A correction stays the user's. Where settle() cannot price it (no
            # tariff schedule — a rate-sensor account), the ladder below keeps the stored rate.
            _settled = _rerun_settle(block, main_meter_id)
        if _settled is not None:
            _sf, _ss = _settled
            imp_ch["rate"] = _ss.imp_rate
            imp_ch["kwh"] = chosen
            imp_ch["kwh_total"] = chosen
            imp_ch["cost"] = _ss.imp_cost
            imp_ch["cost_exc"] = _ss.imp_cost_exc
            imp_ch[STAGED_SEGMENTS] = _settled_segments(_ss)
            logger.info("_rerun_pass2: %s settled by settle() — %s, %s at %.5f",
                        block.get("start", ""), _ss.status, _ss.band, _ss.imp_rate)
        elif chosen is not None:
            # settle() did not price it: a correction (the user's), a CAD-billing install, an
            # import kWh not settled yet (an export-only settlement), or what settle() cannot
            # price (no tariff schedule — a rate-sensor account). The stored rate stands and is
            # re-costed to the chosen kWh; a settled block keeps its bill (BL-82). Engine step 4e
            # (27 Oct 2026): the overlay and IOG-split re-resolve that ran here went — on an
            # account with a schedule, settle() follows this write (4d).
            _held_bill = False
            try:
                _mrow = (_store.get_block_rate_authority(block.get("start", ""), main_meter_id)
                         if _store is not None else None)
                _held_bill = bool(_mrow and _mrow["rate_source"] == "settled"
                                  and not _mrow["rate_corrected"])
            except Exception:
                _held_bill = False
            _own_ratio = _exc_ratio(imp_ch, block.get("start", ""), vat_fallback=False)
            # a missing or zero stored rate is repaired from the resolver where one helps
            rate, rep = _resolve_block_rate(imp_ch, block.get("start", ""), "import",
                                            rate_resolver)
            _rate_repaired = _rate_repaired or rep
            _billed_kwh = imp_ch.get("kwh")
            imp_ch["rate"] = rate
            imp_ch["kwh"] = chosen
            imp_ch["kwh_total"] = chosen
            if _held_bill:
                # BL-82 (13 Oct 2026): a settled block's cost and ex-VAT ARE Octopus's bill —
                # kept, not re-costed to kWh x rate. A settled kWh that differs from the kWh the
                # bill priced is a question for Octopus (ROADMAP BL-86): logged, the bill kept.
                if _billed_kwh is not None and abs(float(_billed_kwh) - float(chosen)) > 0.0005:
                    logger.warning(
                        "_rerun_pass2: %s settled kWh %.3f differs from the %.3f the bill "
                        "priced — bill kept (£%.4f); re-fetch to reconcile",
                        block.get("start", ""), float(chosen), float(_billed_kwh),
                        float(imp_ch.get("cost") or 0.0))
            else:
                imp_ch["cost"] = round(chosen * rate, 6)
                _carry_exc_to_cost(imp_ch, _own_ratio)
            # BL-79 (8 Oct 2026): the stored segments follow the settled kWh at the rates they
            # carry — the house takes the change, the EV only if the house is gone. A block with
            # none keeps none, as before.
            _refit_held_segments(imp_ch, block.get("start", ""), main_meter_id, chosen, rate,
                                 stage=True)

    exp_ch = (main.get("channels") or {}).get("export")
    if exp_ch is None and main.get("exp_kwh_api") is not None:
        # DCC-only export (no export sensor, no Mini export layer): exp_kwh was
        # never materialised at finalise, so the reconstructed block has no
        # export channel — but the settled figure is present in exp_kwh_api.
        # Create the channel so the settlement below can materialise it.
        # Without this, settled export stays stuck in exp_kwh_api forever and
        # the export bill reads zero (the DCC-only-export persistence bug).
        exp_ch = {}
        main.setdefault("channels", {})["export"] = exp_ch
    if exp_ch is not None:
        cad_exp = exp_ch.get("kwh_cad")
        if cad_exp is None:
            cad_exp = exp_ch.get("kwh")
            exp_ch["kwh_cad"] = cad_exp
        dcc_exp = main.get("exp_kwh_api")
        chosen_exp = dcc_exp if (use_dcc and dcc_exp is not None) else cad_exp
        if chosen_exp is not None:
            exp_rate, rep = _resolve_block_rate(exp_ch, block.get("start", ""),
                                                "export", rate_resolver)
            _rate_repaired = _rate_repaired or rep
            if rep:
                exp_ch["rate"] = exp_rate
            exp_ch["kwh"] = chosen_exp
            exp_ch["cost"] = round(chosen_exp * exp_rate, 6)

    if _rate_repaired:
        main["rate_repaired"] = True

    # Standing charge: repair zeros AND re-verify non-zero against Kraken
    # (Kraken wins for now). Resolved value is £/day; reject implausible
    # (units guard) and only correct when materially different to avoid churn.
    if standing_resolver is not None:
        try:
            sc_new = standing_resolver(block.get("start", ""))
        except Exception as e:
            logger.warning("_rerun: standing resolver error at %s: %s",
                           block.get("start", ""), e)
            sc_new = None
        if sc_new is not None and (0.0 <= sc_new <= 2.0):
            sc_old = main.get("standing_charge", 0.0) or 0.0
            if abs(sc_new - sc_old) > 1e-6:
                main["standing_charge"] = sc_new
                main["standing_charge_repaired"] = True
                logger.info("_rerun: standing_charge %s %.4f → %.4f £/day",
                            block.get("start", ""), sc_old, sc_new)
        elif sc_new is not None:
            logger.warning("_rerun: rejected implausible standing charge %.4f "
                           "at %s (units?)", sc_new, block.get("start", ""))

    _apply_pass2(block)
    if _settled is not None:
        # settle()'s devices: PASS 2's clip, each priced at the rate of the legs its kWh sits in
        # (design §4 (b)) — not the main's single rate PASS 2 just gave them
        _sf, _ss = _settled
        for d in _ss.devices:
            sub = (((block["meters"].get(d.meter_id) or {}).get("channels") or {}).get("import"))
            if sub is None:
                continue
            sub["kwh_grid"] = d.kwh_grid
            sub["rate"] = d.rate
            sub["cost"] = d.cost
            sub["cost_exc"] = round(d.cost / (1.0 + (_sf.vat or 0.0)), 6)

    # PASS 3 totals re-derivation (mirror finalise_block PASS 3)
    _recompute_pass3_totals(block)

    _recompute_block_carbon(block)

    # Clear provisional markers — block is DCC-final now.
    for meter_block in block["meters"].values():
        meter_block.pop("is_provisional", None)
        meter_block.pop("provisional", None)
    return block


def _drain_pass2_queue(ha: HAClient, limit: int = 50, rate_resolver=None) -> int:
    """Process blocks flagged needs_pass2_rerun=1 (DCC settlement arrived).

    For each: reload the full block, re-run PASS 2+3b against imp_kwh_api,
    write back (clearing is_provisional), clear the re-run flag. Returns the
    number of blocks successfully re-run.

    Bounded per call (limit) so a large settlement batch is spread across
    ticks rather than blocking one. Each block is independent — a failure on
    one is logged and skipped, leaving its flag set for a later retry.

    rate_resolver (Chunk 5, optional): callable(channel_name, block_start) ->
    £/kWh or None, used to repair zero/missing rates from Kraken history at
    reconcile time. Built once per poll cycle and cached; the drain only reads
    it, never fetches.
    """
    queued = _store.get_blocks_needing_pass2_rerun(limit=limit)
    if not queued:
        return 0

    resolver = rate_resolver if rate_resolver is not None else _kraken_rate_resolver
    source = _get_billing_source()

    done = 0
    for row in queued:
        block_start = row["block_start"]
        block_id    = row["id"]
        try:
            block = _store.get_block_dict_by_start(block_start)
            if not block:
                logger.warning(
                    "_drain_pass2_queue: block %s vanished; clearing flag",
                    block_start)
                _store.clear_pass2_rerun_flag(block_id)
                continue

            _rerun_pass2_for_settled_block(block, rate_resolver=resolver,
                                           billing_source=source,
                                           standing_resolver=_kraken_standing_resolver)
            append_block_replace(block)
            _store.clear_pass2_rerun_flag(block_id)
            done += 1
            logger.info(
                "_drain_pass2_queue: re-ran PASS 2+3b for %s against DCC figure",
                block_start)
        except Exception as e:
            logger.error(
                "_drain_pass2_queue: re-run failed for %s (flag left set "
                "for retry): %s", block_start, e)

    return done


def _amend_provisional_sub_meter_blocks(ha: HAClient, current_block: dict) -> None:
    """Retrospectively correct provisional sub-meter blocks.

    Called each engine tick after reads are captured, but only when no gap
    marker is active (gap-seed reads must not be used as interpolation anchors).

    For each sub-meter whose previous block was written as provisional (no
    post-boundary read was available at close time), we:

      1. Check the device's observed publishing interval from the provisional
         block's own reads (≥ 6 reads required; median ≤ 90 s required).
      2. Check whether the first post-boundary read in the current rolling
         buffer arrived within 2 × that interval of the boundary.
      3. If both checks pass → interpolate to the boundary and amend the block.
      4. If either check fails → commit the provisional figure as-final without
         changing the kWh (the last pre-boundary read is the best estimate).

    Amendment is strictly non-cascading — only the immediately-previous
    block is touched.  Subsequent blocks already have their own reads and
    are unaffected.

    Gap-fill safety: the caller (_engine_tick) must not call this function
    when a gap marker is active.  Gap-seed reads look identical to real reads
    in the rolling buffer dict and must never be used as interpolation anchors.
    """
    provisional_blocks = _store.get_provisional_sub_meter_blocks()
    if not provisional_blocks:
        return

    for prov_block in provisional_blocks:
        block_start_iso = prov_block.get("start", "")
        block_end_iso   = prov_block.get("end", "")
        if not block_start_iso or not block_end_iso:
            continue

        block_end_dt = datetime.fromisoformat(block_end_iso)
        boundary_iso = block_end_iso  # boundary == block end

        for meter_name, meter_block in prov_block.get("meters", {}).items():
            if not meter_block.get("provisional"):
                continue
            meta = meter_block.get("meta", {})
            if not meta.get("sub_meter"):
                continue

            old_imp_ch = meter_block.get("channels", {}).get("import", {})
            old_rate   = old_imp_ch.get("rate", 0.0) or 0.0
            opener_val = old_imp_ch.get("read_start", 0.0)

            # ── Load full block from DB for PASS 2 ────────────────────────
            full_block = _store.get_last_block_before(block_end_iso)
            if not full_block or full_block.get("start") != block_start_iso:
                try:
                    blocks_here = _store.get_blocks_for_range(
                        datetime.fromisoformat(block_start_iso),
                        datetime.fromisoformat(block_end_iso),
                    )
                    full_block = blocks_here[-1] if blocks_here else None
                except Exception as _e:
                    logger.warning(
                        "_amend_provisional: block reload failed for %s: %s",
                        meter_name, _e,
                    )
                    full_block = None

            if not full_block or full_block.get("start") != block_start_iso:
                logger.warning(
                    "_amend_provisional: could not reload block %s from DB; "
                    "skipping %s",
                    block_start_iso, meter_name,
                )
                continue

            # ── Gate 1: device interval from provisional block's own reads ─
            # The provisional block's reads are in the prov_block meter_block,
            # but those reads are not stored to the DB — they live in the
            # current_reads table while the block is live and are discarded
            # after finalise.  We infer the interval from the rolling buffer's
            # pre-boundary reads, which include the full set of reads carried
            # into the current block as seeds plus any new reads.
            cb_meter = (current_block.get("meters") or {}).get(meter_name, {})
            cb_reads  = (cb_meter.get("channels") or {}).get("import", {}).get("reads", [])
            pre_reads  = [r for r in cb_reads if r.get("ts", "") <  boundary_iso]
            post_reads = [r for r in cb_reads if r.get("ts", "") >= boundary_iso]

            if not post_reads:
                # No post-boundary read yet — nothing to do this tick.
                logger.debug(
                    "_amend_provisional: %s — no post-boundary read yet",
                    meter_name,
                )
                continue

            if not pre_reads:
                # Post-boundary read exists but no pre-boundary seed in buffer.
                # Commit as-final — cannot interpolate without an opener.
                logger.warning(
                    "_amend_provisional: %s — post-boundary read present but no "
                    "pre-boundary seed; committing provisional as-final",
                    meter_name,
                )
                _commit_provisional_as_final(
                    ha, full_block, meter_name, block_start_iso, block_end_iso,
                )
                continue

            last_pre   = sorted(pre_reads,  key=lambda r: r["ts"])[-1]
            first_post = sorted(post_reads, key=lambda r: r["ts"])[0]

            # Observed interval: use all pre-boundary reads available in the
            # buffer, which covers the provisional block window.
            median_interval = _observed_device_interval_s(pre_reads)

            if median_interval is None:
                logger.info(
                    "_amend_provisional: %s — insufficient reads (%d) to "
                    "characterise device interval; committing provisional as-final",
                    meter_name, len(pre_reads),
                )
                _commit_provisional_as_final(
                    ha, full_block, meter_name, block_start_iso, block_end_iso,
                )
                continue

            if median_interval > _PROVISIONAL_MAX_INTERVAL_S:
                logger.warning(
                    "_amend_provisional: %s — device interval %.1fs exceeds "
                    "%.0fs limit; device too coarse for boundary interpolation. "
                    "Committing provisional as-final",
                    meter_name, median_interval, _PROVISIONAL_MAX_INTERVAL_S,
                )
                _commit_provisional_as_final(
                    ha, full_block, meter_name, block_start_iso, block_end_iso,
                )
                continue

            # ── Gate 2: post-boundary read must be within 2 × interval ────
            try:
                gap_s = (
                    datetime.fromisoformat(first_post["ts"]) -
                    datetime.fromisoformat(last_pre["ts"])
                ).total_seconds()
            except (ValueError, KeyError):
                gap_s = float("inf")

            threshold_s = _PROVISIONAL_GAP_MULTIPLIER * median_interval
            if gap_s > threshold_s:
                logger.info(
                    "_amend_provisional: %s — post-boundary gap %.1fs exceeds "
                    "%.1fs (%.0f × %.1fs interval); device likely stopped before "
                    "boundary. Committing provisional as-final",
                    meter_name, gap_s, threshold_s,
                    _PROVISIONAL_GAP_MULTIPLIER, median_interval,
                )
                _commit_provisional_as_final(
                    ha, full_block, meter_name, block_start_iso, block_end_iso,
                )
                continue

            # ── Both gates passed — interpolate to boundary ────────────────
            if last_pre["ts"] == first_post["ts"]:
                boundary_val = last_pre["value"]
            else:
                interp = interpolate_value(last_pre, first_post, block_end_dt)
                boundary_val = interp["value"] if interp else last_pre["value"]

            corrected_kwh = max(boundary_val - opener_val, 0.0)
            amended_imp = {
                "kwh":        corrected_kwh,
                "rate":       old_rate,
                "cost":       round(corrected_kwh * old_rate, 6),
                "read_start": opener_val,
                "read_end":   boundary_val,
            }
            if "meta" in old_imp_ch:
                amended_imp["meta"] = old_imp_ch["meta"]

            logger.info(
                "_amend_provisional: %s  interpolated — "
                "old_kwh=%.4f  new_kwh=%.4f  boundary=%.4f  "
                "gap=%.1fs  interval=%.1fs",
                meter_name, old_imp_ch.get("kwh", 0.0), corrected_kwh,
                boundary_val, gap_s, median_interval,
            )

            full_block["meters"][meter_name]["channels"]["import"] = amended_imp
            full_block["meters"][meter_name].pop("provisional", None)
            _apply_pass2(full_block)

            try:
                # Only the amended device is HA's new figure; the others are as stored.
                append_block_replace(full_block, ha_meters=[meter_name])
                logger.info(
                    "_amend_provisional: block %s → %s amended for %s",
                    block_start_iso, block_end_iso, meter_name,
                )
            except Exception as _we:
                logger.error(
                    "_amend_provisional: DB write failed for %s: %s",
                    meter_name, _we,
                )
                continue


def finalise_block(ha: HAClient, block_data: dict | None = None, interpolated: bool = False,
                   last_known_rates: dict | None = None):
    cb = block_data if block_data is not None else _store.load_current_block()

    if not cb or not cb.get("meters"):
        logger.warning("finalise_block: nothing to finalise")
        return

    start           = cb.get("start")
    end             = cb.get("end")
    block_start_dt  = datetime.fromisoformat(start)
    block_end_dt    = datetime.fromisoformat(end)
    boundary_iso    = block_end_dt.isoformat()

    block = {
        "start":  start,
        "end":    end,
        "meters": {},
        "totals": {
            "import_kwh": 0.0, "import_cost": 0.0,
            "export_kwh": 0.0, "export_cost": 0.0,
        },
        "interpolated": interpolated,
    }

    config           = load_json(CONFIG_PATH, {"meters": {}})

    # A channel's "use the supplier API" choice (rate_source / standing_charge_source = 'api') is
    # honoured only while the mode uses the API. A disconnect lands on 'cad' and keeps the config
    # as it was, so the choice outlived the API: the standing charge was forced to 0 over a mapped
    # sensor, with no schedule left to supply it (traced 21 Oct 2026, a staging copy's config).
    try:
        _api_mode = mode_uses_api()
    except Exception:
        _api_mode = False

    # ── PASS 1 — compute all meters with boundary interpolation ───────────
    for meter_name, meter_data in cb.get("meters", {}).items():
        meter_meta  = meter_data.get("meta", {})
        meter_block = {"channels": {}, "meta": meter_meta, "interpolated": interpolated}

        meter_cfg = config.get("meters", {}).get(meter_name)
        if not meter_cfg:
            logger.warning("finalise_block: no config for '%s', skipping", meter_name)
            continue

        import_channel_cfg     = meter_cfg.get("channels", {}).get("import", {})
        standing_charge_sensor = import_channel_cfg.get("standing_charge_sensor")
        # Honour an explicit "use supplier API standing charge" choice on the
        # main meter: take the schedule value even when a SC sensor is mapped.
        _sc_force_api          = (import_channel_cfg.get("standing_charge_source") == "api") \
                                 and not meter_meta.get("sub_meter") and _api_mode
        raw_sc                 = 0.0 if _sc_force_api else \
                                 (read_sensor(ha, standing_charge_sensor) if standing_charge_sensor else 0.0)
        if (raw_sc is None or raw_sc == 0.0) and standing_charge_sensor and not _sc_force_api:
            # Sensor unavailable — fall back to last known value from DB
            try:
                _sc_last = _store.last_standing_charge(meter_name)
                if _sc_last is not None:
                    raw_sc = _sc_last
                    logger.debug("finalise_block: standing charge sensor unavailable, using last known value %.4f", raw_sc)
            except Exception:
                pass
        # API mode (no SC sensor) OR an explicit "use API standing charge" choice:
        # the standing charge comes from the Kraken schedule. Apply it at finalise
        # (main import only; sub-meters inherit) so every block carries its
        # standing charge immediately, rather than 0-until-DCC-settlement. The
        # settlement rerun later refines it via the same resolver.
        if (raw_sc is None or raw_sc == 0.0) and (not standing_charge_sensor or _sc_force_api) \
                and not meter_meta.get("sub_meter"):
            try:
                _sc_sched = _kraken_standing_resolver(start)
                if _sc_sched:
                    raw_sc = float(_sc_sched)
            except Exception:
                pass
        meter_block["standing_charge"] = raw_sc if raw_sc is not None else 0.0

        parent_name  = meter_meta.get("parent_meter")
        parent_meter = cb.get("meters", {}).get(parent_name, {})
        parent_rates = parent_meter.get("channels", {}).get("import", {}).get("rates", [])

        for channel_name, channel in meter_data.get("channels", {}).items():
            is_sub      = meter_meta.get("sub_meter", False)
            rates       = channel.get("rates", [])
            valid_rates = [r for r in rates if r["ts"] < boundary_iso]
            if not valid_rates and rates:
                valid_rates = [rates[0]]
            # API/Mini mode: no rate SENSOR exists — the unit rate comes from the
            # Kraken rate schedule, which knows the correct PER-BLOCK time-of-use
            # rate (e.g. IOG off-peak overnight vs peak by day). Resolve it at the
            # block start (main meter import/export only; sub-meters inherit the
            # parent rate via compute_channel).
            #
            # This MUST be tried BEFORE the last_known_rates fallback below.
            # last_known_rates carries the PREVIOUS block's rate forward as a
            # single value; if it ran first it would mask the schedule resolver
            # and every block would inherit the first block's rate uniformly
            # (the IOG "every block billed at peak, incl. overnight off-peak"
            # bug). The schedule is time-of-use aware and per-block correct, so
            # it wins; last_known_rates is only the gap/restore fallback when the
            # resolver yields nothing (no schedule, or timestamp uncovered).
            # Honour an explicit "use supplier API rate" choice on the MAIN
            # meter: resolve the schedule for this block even when a rate sensor
            # is mapped (the config-screen / wizard toggle wins over the sensor).
            # If the schedule can't resolve, the sensor value already in
            # valid_rates is left as a graceful fallback.
            _ch_cfg = (meter_cfg or {}).get("channels", {}).get(channel_name, {})
            if (_ch_cfg.get("rate_source") == "api") and _api_mode and not is_sub \
                    and channel_name in ("import", "export"):
                try:
                    _kr = _kraken_rate_resolver(channel_name, start)
                    if _kr is not None:
                        valid_rates = [{"ts": start, "value": _kr}]
                except Exception:
                    pass
            if not valid_rates and not is_sub and channel_name in ("import", "export"):
                try:
                    _kr = _kraken_rate_resolver(channel_name, start)
                    if _kr is not None:
                        valid_rates = [{"ts": start, "value": _kr}]
                except Exception:
                    pass
            # Fallback to last_known_rates when no live rates AND the schedule
            # resolver could not resolve (e.g. CAD sensor gap after a restore
            # where stale reads were cleared, or an uncovered schedule window).
            if not valid_rates and last_known_rates:
                _fallback = last_known_rates.get(meter_name, {}).get(channel_name)
                if _fallback:
                    _rate_val = _rate_value(_fallback)
                    valid_rates = [{"ts": start, "value": _rate_val}]

            reads = channel.get("reads", [])

            if reads and not is_sub:
                pre_open  = select_opening_read(reads, block_start_dt)
                post_open = select_closing_read(reads, block_start_dt)
                if pre_open and post_open and pre_open["ts"] != post_open["ts"]:
                    interpolated_opener = interpolate_value(pre_open, post_open, block_start_dt)
                else:
                    interpolated_opener = pre_open or post_open

                pre_close  = select_opening_read(reads, block_end_dt)
                post_close = select_closing_read(reads, block_end_dt)
                if pre_close and post_close and pre_close["ts"] != post_close["ts"]:
                    interpolated_closer = interpolate_value(pre_close, post_close, block_end_dt)
                else:
                    interpolated_closer = post_close or pre_close

                if interpolated_opener and interpolated_closer:
                    channel_for_compute              = dict(channel)
                    channel_for_compute["reads"]     = [interpolated_opener, interpolated_closer]
                    channel_for_compute["rates"]     = valid_rates
                    logger.info(
                        "finalise_block: %s/%s boundary delta=%.4f",
                        meter_name, channel_name,
                        interpolated_closer["value"] - interpolated_opener["value"],
                    )
                else:
                    logger.warning(
                        "finalise_block: %s/%s could not select boundary reads", meter_name, channel_name
                    )
                    channel_for_compute          = dict(channel)
                    channel_for_compute["rates"] = valid_rates
            else:
                channel_for_compute          = dict(channel)
                channel_for_compute["rates"] = valid_rates

            result = compute_channel(channel_for_compute, parent_rates, is_sub_meter=is_sub,
                                     meter_id=meter_name, channel_id=channel_name)

            channel_cfg_meta = meter_cfg.get("channels", {}).get(channel_name, {}).get("meta")
            if channel_cfg_meta:
                result["meta"] = channel_cfg_meta

            meter_block["channels"][channel_name] = result

            # The main import's rate and segments as finalise read them (the rate sensor, or the
            # live schedule). settle() follows the block write (engine step 4d) with the band, the
            # legs and the ex-VAT from the facts; the dispatch overlay that ran here went in 4e.
            if not is_sub and channel_name == "import":
                _reprice_main_import_block(result, start, meter_name, meter_block, stage=True)

            # NOTE: sub-meter (device) import is NOT priced here. Devices always
            # follow the main meter's effective import rate, applied uniformly in
            # PASS 2 (_apply_pass2), which runs after every meter is finalised and
            # sets both the device's rate and cost from the parent's rate. This is
            # the single device-costing point; keeping it out of PASS 1 removes any
            # dependency on meter order and the old per-device over-report floor.

            # ── 2.10.0: provisional detection for sub-meter import ──────────
            # A sub-meter block is provisional when it was closed without a
            # post-boundary read.  The first post-boundary read in the NEXT
            # block's window will trigger a retrospective amendment.
            if is_sub and channel_name == "import" and not interpolated:
                has_post = any(r["ts"] >= boundary_iso for r in reads)
                if not has_post:
                    meter_block["provisional"] = True
                    logger.info(
                        "finalise_block: %s import marked provisional "
                        "(no post-boundary read — will amend when read arrives)",
                        meter_name,
                    )

        block["meters"][meter_name] = meter_block

    # ── PASS 2 — grid-authoritative sub-meter distribution ────────────────
    _apply_pass2(block)
    # ── PASS 3 — compute block totals ─────────────────────────────────────
    for meter_name, meter_block in block["meters"].items():
        meta = meter_block["meta"]
        for channel_name, channel in meter_block["channels"].items():
            if channel_name == "import":
                if meta.get("sub_meter"):
                    block["totals"]["import_kwh"]  += channel.get("kwh_grid", channel["kwh"])
                    block["totals"]["import_cost"] += channel["cost"]
                else:
                    block["totals"]["import_kwh"]  += channel.get("kwh_remainder", channel["kwh"])
                    block["totals"]["import_cost"] += channel.get("cost_remainder", channel["cost"])
            elif channel_name == "export":
                block["totals"]["export_kwh"]  += channel["kwh"]
                block["totals"]["export_cost"] += channel["cost"]

    # ── PASS 3b — carbon footprint ───────────────────────────────────────
    # The nearest carbon-intensity slot, within 60 minutes, onto every meter (and the intensity
    # kept on each, so a later re-run recomputes without the 4-day table). Until engine step 5b
    # (27 Oct 2026) this was its own copy, with no distance bound: a stalled CI fetch would give a
    # closing block a far-off slot's intensity.
    postcode = None
    try:
        postcode = _get_postcode()
        if postcode:
            _attribute_block_carbon(block, start, postcode=postcode)
    except Exception as e:
        logger.warning("finalise_block: carbon_g computation failed: %s", e)

    append_block(block)

    # ── PASS 3c — store generation mix for each meter block ─────────────
    try:
        if postcode and _current_slot_mix:
            start_iso = block.get("start", "")
            # Find nearest slot mix by timestamp
            nearest_key = min(_current_slot_mix.keys(),
                               key=lambda k: abs(
                                   datetime.fromisoformat(k).timestamp() -
                                   datetime.fromisoformat(start_iso).timestamp()
                               )) if start_iso else None
            if nearest_key:
                mix = _current_slot_mix[nearest_key]
                # Store mix against main meter block only — mix is a grid property,
                # not per-meter. Storing against all meters wastes ~3x space.
                _main_id = _store.get_block_id(start_iso)
                if _main_id is not None:
                    _store.upsert_generation_mix(_main_id, mix)
    except Exception as _gme:
        logger.debug("finalise_block: generation mix storage failed: %s", _gme)

    logger.info("finalise_block: %s → %s complete", start, end)

    # ── Prune rolling buffer ───────────────────────────────────────────────
    pruned_block = {
        "start":  iso(block_end_dt),
        "end":    iso(block_end_dt + timedelta(minutes=int(get_block_minutes()))),
        "meters": {},
        "interpolated": False,
    }

    for meter_name, meter_data in cb.get("meters", {}).items():
        pruned_block["meters"][meter_name] = {
            "meta":     meter_data.get("meta", {}),
            "channels": {},
            "interpolated": False,
        }
        for channel_name, channel in meter_data.get("channels", {}).items():
            reads = channel.get("reads", [])
            rates = channel.get("rates", [])

            pruned_reads = [r for r in reads if r["ts"] >= iso(block_end_dt)]
            pruned_rates = [r for r in rates if r["ts"] >= iso(block_end_dt)]

            if not pruned_reads and reads:
                pruned_reads = [reads[-1]]
            if not pruned_rates and rates:
                pruned_rates = [rates[-1]]

            # Carry last pre-boundary read as opener seed for next block
            pre_boundary = [r for r in reads if r["ts"] < iso(block_end_dt)]
            if pre_boundary:
                last_pre = pre_boundary[-1]
                if not any(r["ts"] == last_pre["ts"] for r in pruned_reads):
                    pruned_reads.insert(0, last_pre)
                    logger.info(
                        "finalise_block: carrying seed %s into next block for %s/%s",
                        last_pre["ts"], meter_name, channel_name,
                    )

            pruned_block["meters"][meter_name]["channels"][channel_name] = {
                "reads": pruned_reads,
                "rates": pruned_rates,
            }

    if "_gap_marker" in cb:
        pruned_block["_gap_marker"] = cb["_gap_marker"]
        logger.info("finalise_block: gap marker carried forward")

    _store.save_current_block(pruned_block)
    logger.info("finalise_block: rolling buffer pruned, new block starts %s", iso(block_end_dt))
    # No share copy here any more (#67): it is daily — see _maybe_daily_share_backup.


# ─────────────────────────────────────────────────────────────────────────────
# State trigger callbacks  (replaces @state_trigger decorators)
# ─────────────────────────────────────────────────────────────────────────────

async def on_import_meter_update(entity_id: str, new_val: str, full_state: dict):
    """Fired by ha_client when the main import sensor changes state."""
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        _read_queue.append(now)
        logger.info("on_import_meter_update: read queued at %s", now.isoformat())
    except Exception as e:
        logger.error("on_import_meter_update: %s", e)


async def on_export_meter_update(entity_id: str, new_val: str, full_state: dict):
    """Fired by ha_client when the main export sensor changes state."""
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        _read_queue.append(now)
        logger.info("on_export_meter_update: read queued at %s", now.isoformat())
    except Exception as e:
        logger.error("on_export_meter_update: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Engine loop  (replaces @time_trigger("period(now, 10s)"))
# ─────────────────────────────────────────────────────────────────────────────

async def _poll_mini_demand_kw(now) -> float | None:
    """Fetch the Mini's latest smartMeterTelemetry demand (kW), throttled to one
    call per _MINI_POLL_GAP. Updates _last_mini_demand (read by the gauge) and
    returns the kW for this tick's power_history row, or None when throttled /
    unavailable. Runs on the engine loop, so the GraphQL call is awaited directly."""
    global _last_mini_demand
    import time as _time
    t = _time.monotonic()
    if (t - _last_mini_demand["ts"]) < _MINI_POLL_GAP:
        return None  # throttled — no new row until the next poll window
    reader = _kraken_mini_reader
    device_id = getattr(reader, "device_id", None) if reader else None
    if not device_id or not _kraken_client:
        return None
    try:
        from datetime import timedelta
        start = now - timedelta(minutes=3)
        pts = await _kraken_client.get_telemetry(
            device_id, start.isoformat(), now.isoformat()) or []
        demand_w = None
        for p in reversed(pts):
            if p.get("demand") is not None:
                demand_w = float(p["demand"])
                break
        if demand_w is None:
            return None
        kw = round(demand_w / 1000.0, 3)  # telemetry demand is in watts
        _last_mini_demand = {"kw": kw, "ts": t, "wall": _time.time()}
        return kw
    except Exception as e:
        logger.warning("_poll_mini_demand_kw: %s", e)
        return None


def _power_value_to_kw(value, unit, unit_override=None, invert=False) -> float | None:
    """Convert a power sensor reading to kW.

    `unit_override` ('W'/'kW', case-insensitive) forces the unit when HA's
    unit_of_measurement is wrong or absent — e.g. a CT integration that declares
    'kW' but emits W-scale numbers (1400 for 1.4 kW), which no magnitude heuristic
    can catch because the (wrong) unit IS present. With no override, the sensor's
    declared unit drives it; an absent/unknown unit falls back to the magnitude
    heuristic (>100 ⇒ watts). `invert` negates the result for sensors whose sign
    convention is opposite EMT's (import positive / export negative; or, for a
    battery inverter, positive = charging). Returns None for non-numeric values.
    Mirrors the server's sensor_kw so BCD's current_demand is recorded correctly."""
    try:
        fv = float(value)
    except (ValueError, TypeError):
        return None
    ov = (unit_override or "").strip().upper()
    u = ov if ov in ("W", "KW") else (unit or "").upper()
    if u == "W":
        fv = fv / 1000.0
    elif u != "KW":
        fv = fv / 1000.0 if abs(fv) > 100 else fv
    if invert:
        fv = -fv
    return round(fv, 3)


def _get_postcode() -> str | None:
    """Return the postcode_prefix from the main meter config, or None."""
    try:
        cfg = load_config()
        for m_data in cfg.get("meters", {}).values():
            meta = (m_data or {}).get("meta", {}) or {}
            if not meta.get("sub_meter"):
                pc = _normalise_postcode(meta.get("postcode_prefix", ""))
                return pc if pc else None
    except Exception:
        pass
    return None


def _normalise_postcode(raw: str) -> str:
    """Strip full postcode to outward code only. 'DE1 3AT' → 'DE1', 'SW1A 2AA' → 'SW1A'."""
    return raw.strip().upper().split()[0] if raw and raw.strip() else ""


def _fetch_carbon_intensity(postcode: str) -> list:
    """
    Fetch current + 48hr forecast from National Grid API for the given postcode.
    Returns list of {captured_at, intensity, ci_index} dicts.
    Raises on HTTP/network error so caller can log appropriately.
    """
    import urllib.request
    import urllib.error
    import json as _json

    # Defensive: the API path takes the OUTWARD code only. A full postcode that
    # slipped past config normalisation (e.g. 'CO3 4SE') would put a space in the
    # URL -> "URL can't contain control characters". Normalise at the boundary so
    # every caller is safe regardless of what's stored.
    postcode = _normalise_postcode(postcode)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    url = (f"https://api.carbonintensity.org.uk/regional/intensity"
           f"/{now_iso}/fw48h/postcode/{postcode}")
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = _json.loads(resp.read())

    slots = []
    raw = data.get("data", [])
    # Handle dict shape: {data: {regionid, postcode, data: [...]}}
    if isinstance(raw, dict):
        entries = raw.get("data", [])
    elif isinstance(raw, list) and raw:
        first = raw[0]
        entries = first.get("data", []) if "data" in first else raw
    else:
        entries = []

    for slot in entries:
        intensity_obj = slot.get("intensity", {})
        forecast      = intensity_obj.get("forecast")
        actual        = intensity_obj.get("actual")    # None until ~24hr after slot
        ci_index      = intensity_obj.get("index")
        slot_from     = slot.get("from")
        if slot_from and (forecast is not None or actual is not None):
            captured_at = slot_from.replace("Z", "").replace("+00:00", "")
            slots.append({
                "captured_at":       captured_at,
                "intensity":         float(actual if actual is not None else forecast),
                "intensity_forecast": float(forecast) if forecast is not None else None,
                "intensity_actual":   float(actual)   if actual   is not None else None,
                "ci_index":          ci_index,
                "generationmix":     slot.get("generationmix", []),
            })

    return slots


# ── Historical carbon backfill (v2 -> v3 migration) ────────────────────────────
# The heatmap and Usage Stats now treat the stored carbon_intensity_g as the
# source of truth. Blocks created before CI first became available (the whole
# pre-postcode history, including everything carried over from v2) have a NULL
# intensity. The live recovery sweep (_recover_missing_carbon) can only reach
# blocks still inside the 4-day CI table, so this one-shot backfill pages the
# Carbon Intensity API's historical endpoint over the NULL span and writes the
# intensity straight onto the blocks. Gated on "postcode exists"; runs once
# (persistent marker); resumable (cursor); throttled to sub-14-day windows.
_CARBON_BACKFILL_MARKER = "carbon_backfill_state"   # store_meta key
_CARBON_RETRY_HOURS = 24                             # cool-down before re-attempting gaps
_carbon_backfill_running = False                    # in-process re-entry guard
_delete_active = False                              # a delete/purge job is mutating blocks


def set_delete_active(active: bool) -> None:
    """Flagged by the delete/purge workers around a block-removal job so the carbon
    backfill won't KICK OFF new work mid-delete (both share the single SQLite
    connection). An already-running backfill stays safe on its own — it re-reads each
    block before writing and skips any the delete removed."""
    global _delete_active
    _delete_active = bool(active)
    # One chokepoint, both workers. `_delete_worker` and `_purge_worker` share
    # this flag, and `delete_in_progress()` means "either" — so they are one
    # coordinator member with two entry points rather than two members.
    #
    # Inherits the flag's existing limitation rather than pretending otherwise:
    # if both ever ran at once, the first to finish would clear it for both. That
    # is true of `_delete_active` today; the coordinator does not make it worse,
    # and does not fix it either. Keyed claims would — `claim("delete", key)` —
    # but that is a behaviour change and belongs with its own evidence.
    if active:
        _coord.claim("delete", force=True)
    else:
        _coord.mark("delete", status="done")


def delete_in_progress() -> bool:
    return _delete_active


_poll_active = False   # a kraken DCC poll / BL-8 backfill cycle is mutating blocks


def set_poll_active(active: bool) -> None:
    """Flagged around a kraken poll cycle (DCC ingest + BL-8 outage backfill).
    A manual date-range delete refuses while this is set: the poll and the delete
    share the single SQLite connection, and BL-8 backfill re-creates settled blocks
    with no local block — so a delete racing a backfill leaves a half-deleted day
    (the dev-DB 'deleted the 12th mid-backfill, it came back partial' incident).
    Time-bounded (cleared in a finally), so a delete can never be blocked
    indefinitely."""
    global _poll_active
    _poll_active = bool(active)


def poll_in_progress() -> bool:
    return _poll_active


def _fetch_carbon_intensity_range(postcode: str, from_iso: str, to_iso: str) -> dict:
    """Fetch settled historical regional intensity for [from_iso, to_iso] and
    return {slot_from -> intensity_gco2}. The Carbon Intensity API serves at most
    a 14-day window per request, so callers must page. 'actual' is preferred over
    'forecast' (actuals settle ~24h after each slot; every backfill target is far
    older than that, so a real actual is always returned). Raises on HTTP/network
    error so the caller can persist a resume cursor and stop."""
    import urllib.request
    import json as _json

    # Defensive: outward code only (see _fetch_carbon_intensity). A stored full
    # postcode here injects a space and fails every backfill window, looping the
    # historical backfill forever ("running for hours"). Normalise at the boundary.
    postcode = _normalise_postcode(postcode)
    url = (f"https://api.carbonintensity.org.uk/regional/intensity"
           f"/{from_iso}/{to_iso}/postcode/{postcode}")
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = _json.loads(resp.read())

    raw = data.get("data", [])
    if isinstance(raw, dict):
        entries = raw.get("data", [])
    elif isinstance(raw, list) and raw:
        entries = raw[0].get("data", []) if "data" in raw[0] else raw
    else:
        entries = []

    out: dict = {}
    for slot in entries:
        io = slot.get("intensity", {}) or {}
        val = io.get("actual")
        if val is None:
            val = io.get("forecast")
        sf = slot.get("from")
        if sf and val is not None:
            out[sf.replace("Z", "").replace("+00:00", "")] = float(val)
    return out


def _nearest_intensity_from_map(ci_map: dict, block_start: str):
    """Nearest slot intensity (<=60 min) for *block_start* from a
    {slot_iso -> intensity} map. Tries the containing 30-min slot first (a direct
    hit for 30-min-aligned blocks), then falls back to the nearest within 60 min."""
    from datetime import datetime as _dt
    if not ci_map:
        return None
    try:
        bs = _dt.fromisoformat(str(block_start).replace("Z", "").split("+")[0])
    except Exception:
        return None
    floored = bs.replace(minute=(0 if bs.minute < 30 else 30),
                         second=0, microsecond=0)
    hit = ci_map.get(floored.strftime("%Y-%m-%dT%H:%M"))
    if hit is not None:
        return hit
    best = best_d = None
    for k, v in ci_map.items():
        try:
            kd = _dt.fromisoformat(k)
        except Exception:
            continue
        d = abs((bs - kd).total_seconds())
        if d <= 3600 and (best_d is None or d < best_d):
            best_d, best = d, v
    return best


@job("carbon_backfill", depends_on=("import",), exclusive_with=("delete",))
async def _run_historical_carbon_backfill(window_days: int = 13,
                                          max_windows: int = 60) -> int:
    """One-shot historical carbon backfill. Pages the Carbon Intensity API over
    the span of NULL-intensity blocks in sub-14-day windows, writes carbon_g +
    carbon_intensity_g straight onto those blocks (NOT via the 4-day-pruned CI
    table), and records a resume cursor after each window so a restart continues.
    Idempotent: only touches NULL blocks; sets a done marker when no NULL blocks
    remain. Returns blocks backfilled this invocation.

    CONCURRENCY (this is why it is a coroutine, not a threaded worker): every
    BlockStore read/write here runs on the event-loop thread — the same thread
    that owns the engine's single SQLite connection (opened check_same_thread=
    False for a *single* thread). Only the blocking CI fetch is offloaded, via
    run_in_executor. The first cut ran the whole worker on an executor thread; its
    DB writes then raced the main-loop _drain_pass2_queue on that one shared
    connection and corrupted both (SQLITE_MISUSE: "bad parameter or other API
    misuse", "cannot commit - no transaction is active"). Running the DB work on
    the loop thread serialises it cooperatively with the drain — no shared-
    connection race, and no second connection / last-write-wins race either.

    window_days defaults to 13, NOT 14: the API's /regional/intensity/{from}/{to}
    range cap is effectively exclusive — a [00:00..00:00] 14-day request lands one
    half-hour period past the limit and returns HTTP 400. 13-day windows sit
    safely under it (one extra request over the whole history; negligible).

    `max_windows` bounds a single invocation; a larger history finishes across
    successive triggers via the cursor."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    import asyncio as _aio
    _now = lambda: _dt.now(_tz.utc).replace(tzinfo=None).isoformat()
    # Cool-down before re-attempting gaps that a pass couldn't fill — so we self-heal
    # transient CI outages without hammering the API for genuinely-absent recent data.
    _retry_after = lambda: (_dt.now(_tz.utc).replace(tzinfo=None)
                            + _td(hours=_CARBON_RETRY_HOURS)).isoformat()
    if _store is None:
        return 0
    postcode = _get_postcode()
    if not postcode:
        return 0

    state = _store.get_meta(_CARBON_BACKFILL_MARKER, {}) or {}
    if state.get("done"):
        return 0

    rng = _store.get_missing_carbon_date_range()
    if not rng:
        _store.set_meta(_CARBON_BACKFILL_MARKER,
                        {"done": True, "completed_at": _now()})
        return 0

    lo_iso, hi_iso = rng
    cursor = state.get("cursor") or lo_iso
    try:
        cur_dt = _dt.fromisoformat(str(cursor).replace("Z", "").split("+")[0])
    except Exception:
        cur_dt = _dt.fromisoformat(str(lo_iso).replace("Z", "").split("+")[0])
    try:
        hi_dt = (_dt.fromisoformat(str(hi_iso).replace("Z", "").split("+")[0])
                 + _td(minutes=30))   # inclusive of the last slot
    except Exception:
        return 0

    try:
        loop = _aio.get_running_loop()
    except RuntimeError:
        loop = None

    def _apply_from_map(start, cmap):
        """Match a block to the nearest CI slot in `cmap` and persist carbon in
        place. Shared by the wide-window and the narrow per-day straggler pass.
        Returns True if written, None if no CI slot matched (a FETCH gap the
        straggler pass should refetch), False on a block/persist error (leave it for
        the next pass — a narrow refetch wouldn't help)."""
        intensity = _nearest_intensity_from_map(cmap, start)
        if intensity is None:
            return None
        block = _store.get_block_dict_by_start(start)
        if not block:
            return False
        _apply_intensity_to_block(block, intensity)
        try:
            _persist_block_carbon(block, start)
            return True
        except Exception as e:
            logger.warning("_run_historical_carbon_backfill: persist failed %s: %s",
                           start, e)
            return False

    backfilled = 0
    windows = 0
    while cur_dt < hi_dt and windows < max_windows:
        win_end = min(cur_dt + _td(days=window_days), hi_dt)
        f_iso = cur_dt.strftime("%Y-%m-%dT%H:%MZ")
        t_iso = win_end.strftime("%Y-%m-%dT%H:%MZ")
        # Resolve each block's region from its config period and fetch CI PER
        # region — so a mid-history property move attributes the correct region to
        # old blocks, not the current postcode. Blocks with no resolvable region
        # were already excluded by get_block_starts_missing_carbon_in_range.
        starts = _store.get_block_starts_missing_carbon_in_range(
            cur_dt.isoformat(), win_end.isoformat())
        by_oc: dict = {}
        for start in starts:
            oc = _store.get_postcode_prefix_at(start)[0] or postcode
            if oc:
                by_oc.setdefault(oc, []).append(start)
        for oc, oc_starts in by_oc.items():
            try:
                # Offload ONLY the blocking network fetch; DB stays on this thread.
                if loop is not None:
                    ci_map = await loop.run_in_executor(
                        None, _fetch_carbon_intensity_range, oc, f_iso, t_iso)
                else:
                    ci_map = _fetch_carbon_intensity_range(oc, f_iso, t_iso)
            except Exception as e:
                logger.warning("_run_historical_carbon_backfill: fetch failed "
                               "%s..%s (%s): %s — pausing, will resume",
                               f_iso, t_iso, oc, e)
                _store.set_meta(_CARBON_BACKFILL_MARKER,
                                {"cursor": cur_dt.isoformat(), "done": False})
                return backfilled
            missing = []
            for start in oc_starts:
                r = _apply_from_map(start, ci_map)
                if r is True:
                    backfilled += 1
                elif r is None:
                    missing.append(start)     # fetch gap → narrow refetch below
                # False (block/persist error) → leave for the next pass
            # Straggler pass: a thin wide-window CI response (a transient regional
            # outage, or a slow/partial return) leaves gaps the forward-only cursor
            # would otherwise strand. Retry the still-NULL slots with a NARROW
            # per-day fetch — the calmest request — which reliably returns what the
            # 13-day window missed. Mirrors the pricing single-slot recovery.
            for _day in sorted({s[:10] for s in missing}):
                try:
                    _d0 = _dt.fromisoformat(_day)
                    df = _d0.strftime("%Y-%m-%dT00:00Z")
                    dto = (_d0 + _td(days=1)).strftime("%Y-%m-%dT00:00Z")
                    if loop is not None:
                        day_map = await loop.run_in_executor(
                            None, _fetch_carbon_intensity_range, oc, df, dto)
                    else:
                        day_map = _fetch_carbon_intensity_range(oc, df, dto)
                except Exception as e:
                    logger.info("_run_historical_carbon_backfill: straggler refetch "
                                "%s (%s) failed: %s", _day, oc, e)
                    continue
                for start in [m for m in missing if m[:10] == _day]:
                    if _apply_from_map(start, day_map) is True:
                        backfilled += 1

        cur_dt = win_end
        windows += 1
        _store.set_meta(_CARBON_BACKFILL_MARKER,
                        {"cursor": cur_dt.isoformat(), "done": False})
        if loop is not None:
            await _aio.sleep(0)   # yield so ticks/reads interleave between windows

    if cur_dt < hi_dt:
        # Hit the per-invocation window cap; resume next trigger from the cursor.
        logger.info("_run_historical_carbon_backfill: paused after %d window(s), "
                    "%d block(s); will resume from cursor", windows, backfilled)
        return backfilled

    # Full span swept. Decide the marker by what NULL blocks ACTUALLY remain — not
    # by the cursor. A per-block persist failure leaves a gap the forward cursor
    # has already moved past; marking done on cursor alone (the original bug) would
    # strand it. Re-query the truth instead.
    remaining = _store.get_missing_carbon_date_range()
    if remaining is None:
        _store.set_meta(_CARBON_BACKFILL_MARKER,
                        {"done": True, "completed_at": _now()})
        logger.info("_run_historical_carbon_backfill: complete — %d block(s) "
                    "backfilled", backfilled)
    elif backfilled > 0:
        # Progress made but gaps remain (transient persist failures). Retry next
        # trigger from the earliest still-NULL block — bounded so a permanently
        # stuck slot can't loop forever re-hitting the API.
        attempts = int(state.get("retry_attempts", 0)) + 1
        if attempts >= 6:
            _store.set_meta(_CARBON_BACKFILL_MARKER,
                            {"done": True, "completed_at": _now(),
                             "unfilled_from": remaining[0],
                             "retry_after": _retry_after()})
            logger.warning("_run_historical_carbon_backfill: gaps remain from %s "
                           "after %d rapid retries — cooling down, will re-attempt "
                           "later", remaining[0], attempts)
        else:
            _store.set_meta(_CARBON_BACKFILL_MARKER,
                            {"cursor": remaining[0], "done": False,
                             "retry_attempts": attempts})
            logger.info("_run_historical_carbon_backfill: %d block(s) backfilled; "
                        "gaps remain from %s — will retry (%d)",
                        backfilled, remaining[0], attempts)
    else:
        # A whole pass filled nothing yet NULL blocks remain. Likely a transient CI
        # outage (recent slots may not have settled; a regional window came back
        # thin). Cool down rather than freeze forever: record retry_after so a later
        # trigger re-attempts. Old slots almost always settle, so this self-heals.
        _store.set_meta(_CARBON_BACKFILL_MARKER,
                        {"done": True, "completed_at": _now(),
                         "unfilled_from": remaining[0],
                         "retry_after": _retry_after()})
        logger.warning("_run_historical_carbon_backfill: span swept but NULL "
                       "blocks remain from %s and none filled this pass — cooling "
                       "down, will re-attempt after %s", remaining[0], _retry_after())
    return backfilled


def _maybe_backfill_historical_carbon() -> None:
    """Schedule the one-shot historical backfill if a postcode exists and it
    hasn't completed. Safe to call repeatedly (engine startup + every CI tick) —
    guarded by an in-process flag and the persistent done marker. The CI-tick
    trigger is what catches a postcode added *after* first boot.

    Dispatches the async worker as a loop TASK (create_task), not an executor
    thread: all BlockStore access must run on the event-loop thread that owns the
    engine's single SQLite connection. Self-heals a stale done marker — an earlier
    (concurrency-broken) run could mark done while transient failures left NULL
    blocks behind; if gaps remain and we didn't already conclude they're
    unattributable, re-arm and let the worker fill them."""
    global _carbon_backfill_running
    try:
        if _store is None or not _get_postcode() or _carbon_backfill_running:
            return
        if _store.get_meta("carbon_paused", None):     # manual kill switch
            return
        # The reason these two were named here — "Don't compete with an active
        # historical import — both do heavy work on the engine's single DB
        # connection" — is now the DECLARATION on this member, so the scheduler
        # asks one question instead of naming two jobs. Adding a third blocker
        # becomes a declaration rather than an edit here.
        _decision, _blocker = _coord.admission("carbon_backfill")
        if _decision != ADMIT:
            logger.info("_maybe_backfill_historical_carbon: %s active — deferring",
                        _blocker)
            return
        state = _store.get_meta(_CARBON_BACKFILL_MARKER, {}) or {}
        if state.get("done"):
            remaining = _store.get_missing_carbon_date_range()
            if remaining is None:
                return   # genuinely complete — no eligible NULL carbon blocks left
            # Gaps remain. A prior pass may have tombstoned them (unfilled_from) after
            # a transient CI outage — but old slots settle and are almost always
            # fillable later, so we must NOT freeze forever. Re-attempt on a slow
            # cadence: honour retry_after so we don't hammer the API for genuinely
            # recent/absent data, but always re-arm once the cool-down has passed.
            ra = state.get("retry_after")
            if state.get("unfilled_from") and ra:
                from datetime import datetime as _dt3, timezone as _tz3
                try:
                    if _dt3.now(_tz3.utc).replace(tzinfo=None).isoformat() < ra:
                        return   # still cooling down; re-check after retry_after
                except Exception:
                    pass
            logger.info("_maybe_backfill_historical_carbon: NULL carbon blocks remain "
                        "from %s — re-arming (retry)", remaining[0])
            _store.set_meta(_CARBON_BACKFILL_MARKER,
                            {"cursor": remaining[0], "done": False})
        import asyncio as _aio
        try:
            loop = _aio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            return   # no running loop to host the task (non-async caller)

        async def _task():
            global _carbon_backfill_running
            try:
                await _run_historical_carbon_backfill()
            except Exception as e:
                logger.warning("_maybe_backfill_historical_carbon: worker "
                               "failed: %s", e)
            finally:
                _carbon_backfill_running = False

        _carbon_backfill_running = True
        loop.create_task(_task())
    except Exception as e:
        logger.warning("_maybe_backfill_historical_carbon: schedule failed: %s", e)
        _carbon_backfill_running = False


def _ci_slot_plausible(mix, intensity, ci_index) -> bool:
    """BL-38: reject a physically-impossible regional generation-mix / intensity slot.

    National Grid's *regional* generation mix is a MODELLED estimate (embedded solar is
    inferred, not metered) and for a small region it occasionally emits a degenerate slot —
    one fuel ~100% with the rest ~0, and a collapsed 0 gCO2/kWh — e.g. 'solar 97% at 21:30'.
    Stored verbatim it corrupts the donut, the 48h chart AND (via intensity) the block carbon.
    Reject the glitch signature so the previous good slot stands; genuine mixes never trip it
    (real single-fuel maxima sit ~70-80%; GB intensity is never 0). Never rejects on a parse
    error (returns True). Pure — unit-tested.
    """
    try:
        if mix:
            top = max((float(f.get("perc") or 0.0) for f in mix), default=0.0)
            if top >= 95.0:
                return False
        if intensity is not None and float(intensity) <= 0.0:
            return False
    except Exception:
        return True
    return True


async def _tick_carbon_intensity() -> float | None:
    """
    Fetch and store carbon intensity if 15 minutes have elapsed since last fetch.
    Returns the current intensity (gCO2/kWh) or None.
    """
    global _last_ci_fetch
    import urllib.error

    postcode = _get_postcode()
    if not postcode:
        return None  # No postcode configured — fail silently

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    elapsed = (now - _last_ci_fetch).total_seconds() if _last_ci_fetch else None
    if elapsed is not None and elapsed < 900:  # 15 minutes
        # Not time yet — return current intensity from DB without fetching
        row = _store.get_nearest_carbon_intensity(now.isoformat(), postcode)
        return row["intensity"] if row else None

    try:
        # The fetch runs OFF the loop (#65, 3 Oct 2026): a blocking urlopen (8 s
        # timeout) on the loop held it every 15 minutes. It touches no database, so
        # engine_startup's rule (DB work on the loop thread) is kept: everything
        # below runs on the loop.
        slots = await asyncio.get_running_loop().run_in_executor(
            None, _fetch_carbon_intensity, postcode)
        good = []
        for slot in slots:
            # BL-38: drop an implausible regional-model glitch slot (one fuel ~100% /
            # 0 gCO2/kWh) so it never reaches the donut, the 48h chart or block carbon —
            # the previous good slot stands (get_nearest_carbon_intensity falls back).
            if not _ci_slot_plausible(slot.get("generationmix"),
                                      slot.get("intensity_forecast"),
                                      slot.get("ci_index")):
                _tf = max((float(f.get("perc") or 0.0)
                           for f in (slot.get("generationmix") or [])), default=None)
                logger.warning("_tick_carbon_intensity: implausible CI slot %s skipped "
                               "(intensity=%s, top_fuel=%s%%) — regional-model glitch (BL-38)",
                               slot.get("captured_at"), slot.get("intensity_forecast"), _tf)
                continue
            good.append(slot)
        # Every slot and its generation mix (mix_history, at CI-tick resolution,
        # independent of block size) in one transaction: ~190 commits before (#65).
        _store.upsert_carbon_slots(postcode, good)
        for slot in good:
            if slot.get("generationmix"):
                _current_slot_mix[slot["captured_at"]] = slot["generationmix"]
        _store.prune_carbon_intensity(days=4)
        _store.prune_mix_history(hours=48)
        _last_ci_fetch = now
        logger.info("_tick_carbon_intensity: stored %d slots for %s", len(slots), postcode)
        # CI is freshly populated — backfill any blocks left NULL by an outage
        # gap-fill (recovers without waiting for a restart).
        try:
            _recover_missing_carbon()
        except Exception as e:
            logger.warning("_tick_carbon_intensity: carbon recovery failed: %s", e)
        # Historical backfill (v2->v3 migration). Run-once; this trigger is what
        # picks it up when a postcode is added after first boot. No-op once done.
        try:
            _maybe_backfill_historical_carbon()
        except Exception as e:
            logger.warning("_tick_carbon_intensity: historical backfill "
                           "schedule failed: %s", e)
    except urllib.error.HTTPError as e:
        logger.warning("_tick_carbon_intensity: HTTP %s for postcode %s", e.code, postcode)
    except urllib.error.URLError as e:
        logger.warning("_tick_carbon_intensity: network error for postcode %s: %s", postcode, e)
    except Exception as e:
        logger.warning("_tick_carbon_intensity: unexpected error: %s", e)

    # Return current intensity from DB regardless of fetch success/failure
    row = _store.get_nearest_carbon_intensity(now.isoformat(), postcode)
    return row["intensity"] if row else None


@job("live_tick", on_conflict=ADMIT)                      # member 23: never deferred
async def engine_loop_task(ha: HAClient):
    """
    Runs forever, ticking every 10 seconds.
    Replaces @time_trigger("period(now, 10s)") + @task_unique("energy_engine_loop").
    The asyncio.Lock prevents overlapping executions.
    """
    logger.info("engine_loop_task: started")
    global _engine_ha
    _engine_ha = ha

    while True:
        try:
            async with _engine_loop_lock:
                await _engine_tick(ha)
        except Exception as e:
            logger.error("engine_loop_task: unhandled error: %s", e)

        await asyncio.sleep(10)


async def _engine_tick(ha: HAClient):
    if _engine_paused:
        return
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        _maybe_daily_share_backup(now)          # #67: once a day, in the early hours
    except Exception as _sb_e:
        logger.warning("_engine_tick: daily share backup check failed: %s", _sb_e)

    current_block = _store.load_current_block()

    # Load block size from config (may have changed since startup)
    block_minutes = get_block_minutes()

    # Periodic checkpoint
    last_checkpoint = current_block.get("_last_checkpoint")
    if last_checkpoint:
        since_checkpoint   = (now - datetime.fromisoformat(last_checkpoint)).total_seconds()
        periodic_checkpoint = since_checkpoint >= 60
    else:
        periodic_checkpoint = True

    seconds_into_block = (now.minute % block_minutes) * 60 + now.second
    near_boundary      = (block_minutes * 60 - seconds_into_block) <= 15

    # Drain read queue
    if _read_queue:
        drained = 0
        while _read_queue:
            queued_ts = _read_queue.pop(0)
            capture_samples(ha, current_block, queued_ts)
            drained += 1
        logger.info("_engine_tick: drained %d queued reads", drained)

    if not _read_queue and (periodic_checkpoint or near_boundary):
        capture_samples(ha, current_block, now)

    # ── 3.0.0: Mini telemetry collection (CAD-like) ───────────────────────
    # When the Mini provisional layer is active (api+mini, no local sensor),
    # collect smart-meter telemetry into the CURRENT block's import reads buffer
    # around each boundary — starting ~20s before and continuing until a read
    # timestamped past the boundary lands. The existing post-boundary-read wait
    # + finalise interpolation then bracket the boundary and compute the closing
    # register uniformly with CAD. Mini reads carry their OWN readAt timestamps.
    if _kraken_mini_reader is not None and current_block.get("start"):
        try:
            await _collect_mini_into_block(current_block, now, block_minutes)
        except Exception as _mc_e:
            logger.warning("_engine_tick: mini collection failed: %s", _mc_e)

    # ── 2.10.0: amend provisional sub-meter blocks ────────────────────────
    # Skip entirely when a gap marker is active — gap-seed reads in the
    # rolling buffer look identical to real reads and must never be used
    # as interpolation anchors for boundary correction.
    if not has_gap_marker(current_block):
        try:
            _amend_provisional_sub_meter_blocks(ha, current_block)
        except Exception as _amp_e:
            logger.warning("_engine_tick: provisional amendment failed: %s", _amp_e)

    # ── 3.0.0: drain the DCC PASS 2 re-run queue ──────────────────────────
    # Blocks whose Kraken DCC settlement has arrived (needs_pass2_rerun=1) are
    # re-run against imp_kwh_api here. No-op until the ingester sets the flag.
    #
    # BL-1: NOT gap-guarded. This was previously inside the `not
    # has_gap_marker(...)` block above, which globally froze settlement
    # application for the WHOLE history while any gap marker was live — an
    # outage on one day stopped every other day's settled figures from being
    # applied to billing until the marker cleared. Unlike the amendment above,
    # the drain takes its work queue from the DB and re-costs historical blocks
    # by block_start; it never reads the current block or the rolling buffer, so
    # gap-seed reads cannot contaminate it. Settling a gap-filled block is in
    # fact desirable — the authoritative DCC figure replaces the interpolated
    # guess (the ingester already counts these as `drifted_interpolated`).
    try:
        _drain_pass2_queue(ha)
    except Exception as _dpq_e:
        logger.warning("_engine_tick: PASS 2 re-run drain failed: %s", _dpq_e)

    # Deferred gap filling
    # Pre-populate with rates from the last finalised block so finalise_block
    # always has a rate fallback even when no gap fill has run this tick.
    _post_gap_rates = None
    try:
        _last_blk = _store.get_last_block()
        if _last_blk:
            _, _post_gap_rates = extract_last_reads(_last_blk)
    except Exception:
        pass
    if has_gap_marker(current_block):
        try:
            await _mini_post_outage_read(current_block, now)
        except Exception as _mpo_e:
            logger.warning("gap fill: Mini latest read failed (non-fatal): %s", _mpo_e)
        has_real_read = False
        post_reads    = {}

        for meter_name, meter_data in current_block.get("meters", {}).items():
            post_reads[meter_name] = {}
            for channel_name, channel in meter_data.get("channels", {}).items():
                reads = channel.get("reads", [])
                if reads:
                    # Use the LAST (most recent) read as post-gap anchor, not the first.
                    # If the sensor updated multiple times before gap fill triggered,
                    # reads[-1] gives the best interpolation endpoint.
                    post_reads[meter_name][channel_name] = reads[-1]
                    has_real_read = True

        if not has_real_read:
            logger.info("gap fill: waiting for first post-outage read")
        else:
            logger.info("gap fill: post-outage read available, filling gap now")
            marker     = current_block["_gap_marker"]
            pre_reads  = marker["pre_reads"]
            last_rates = marker["last_known_rates"]
            logger.info("gap fill: pre_reads=%s", pre_reads)
            logger.info("gap fill: last_rates=%s", last_rates)
            logger.info("gap fill: post_reads=%s", post_reads)

            pre_ts = None
            for meter_reads in pre_reads.values():
                for read in meter_reads.values():
                    if isinstance(read, dict) and read.get("ts"):
                        if not pre_ts or read["ts"] > pre_ts:
                            pre_ts = read["ts"]

            # Use last_block_start stored in the gap marker as the anchor.
            # This is the start of the last finalised block, so detect_gap
            # computes last_block_end = last_block_start + block_minutes,
            # which equals last_block.end — correctly including the unfinalised
            # current_block window in missing_windows.
            gap_anchor_ts = marker.get("last_block_start") or pre_ts

            logger.info("gap fill: pre_ts=%s gap_anchor_ts=%s now=%s", pre_ts, gap_anchor_ts, now)
            missing_windows = detect_gap(gap_anchor_ts, now, block_minutes=block_minutes)
            logger.info("gap fill: missing_windows count=%s first=%s last=%s",
                        len(missing_windows) if missing_windows else 0,
                        missing_windows[0] if missing_windows else None,
                        missing_windows[-1] if missing_windows else None)

            # GAP_FILL_LIMIT_HOURS defined at module level
            gap_hours = len(missing_windows) * block_minutes / 60.0 if missing_windows else 0.0

            if missing_windows and gap_hours > GAP_FILL_LIMIT_HOURS:
                # Gap exceeds 12-hour limit — skip gap-fill entirely.
                # The first resumed block will calculate its delta from post-gap
                # reads correctly. Handles: extended outages, meter replacement,
                # moving property — all produce gaps > 12 hours naturally.
                logger.warning(
                    "gap fill: gap of %.1f hours exceeds %d-hour limit — "
                    "gap-fill skipped. Data absent for this window.",
                    gap_hours, GAP_FILL_LIMIT_HOURS
                )
                # ── Detect possible meter replacement ────────────────────────
                # If post-gap read is significantly lower than pre-gap read,
                # flag it so the UI can suggest creating a new billing period.
                global _meter_reset_detected
                try:
                    main_pre  = pre_reads.get("electricity_main", {}).get("import", {})
                    main_post = post_reads.get("electricity_main", {}).get("import", {})
                    pre_val  = float(main_pre.get("value",  0)) if isinstance(main_pre,  dict) else None
                    post_val = float(main_post.get("value", 0)) if isinstance(main_post, dict) else None
                    RESET_THRESHOLD_KWH = 50.0
                    if pre_val is not None and post_val is not None:
                        if post_val < pre_val - RESET_THRESHOLD_KWH:
                            logger.warning(
                                "gap fill: meter read reset detected — "
                                "pre-gap=%.3f kWh post-gap=%.3f kWh (drop=%.1f kWh). "
                                "Possible meter replacement or property move.",
                                pre_val, post_val, pre_val - post_val
                            )
                            _meter_reset_detected = True
                except Exception as _re:
                    logger.debug("gap fill: meter reset check failed: %s", _re)

            elif missing_windows:
                config     = load_config()
                # Get last known standing charge from most recent finalised main-meter block
                last_sc = 0.0
                try:
                    last_sc = _store.last_standing_charge("electricity_main") or 0.0
                except Exception:
                    pass
                gap_blocks = build_gap_blocks(
                    missing_windows, pre_reads, post_reads, last_rates, config,
                    last_standing_charge=last_sc
                )
                for gb in gap_blocks:
                    append_block_replace(gb)

                logger.info("gap fill: %d interpolated blocks inserted", len(gap_blocks))
            else:
                logger.warning("gap fill: no missing windows found, clearing marker")

            clear_gap_marker(current_block)
            # Preserve last_known_rates for catch-up rollover rate fallback
            _post_gap_rates = last_rates
            # Reset current_block to the current window so the next restart
            # doesn't try to catch up through blocks already written by gap fill.
            block_minutes_now = int(get_block_minutes())
            gap_start, gap_end = get_block_window(now, block_minutes=block_minutes_now)
            current_block = create_block(gap_start, gap_end, block_minutes=block_minutes_now)
            _store.save_current_block(current_block)

    # ── Carbon intensity — fetch BEFORE block lifecycle so catch-up blocks ──
    # ── have fresh CI data when finalise_block computes carbon_g           ──
    try:
        current_intensity = await _tick_carbon_intensity()
    except Exception as _cie:
        logger.warning("_engine_tick: CI fetch raised: %s", _cie)
        current_intensity = None

    # Dispatch-slot capture (5-min throttled) — catch smart-charge slots while
    # still 'planned', before they convert to completed/unknown.
    try:
        await _tick_dispatch_capture()
    except Exception as _dce:
        logger.warning("_engine_tick: dispatch capture raised: %s", _dce)

    # Discovery asked again while it has not answered (G1 design §5), off the tick
    _start_discovery_retry()
    # A battery sensor that counts solar: its evidence, hourly (battery sensor design §3)
    _maybe_check_battery_sensors()
    # BL-103: Agile's prices in the account's region, once a day after Octopus publishes, off the tick
    _start_agile_public_fetch()

    # Block lifecycle
    updated_block = ensure_correct_block(ha, current_block, now, last_known_rates=_post_gap_rates)
    block_changed = updated_block.get("start") != current_block.get("start")

    if block_changed or periodic_checkpoint or near_boundary:
        updated_block["_last_checkpoint"] = now.isoformat()
        _store.save_current_block(updated_block)

    # Write power history row every tick if a live power source is available
    try:
        cfg = load_config()
        power_sensor = None
        power_source = None
        _power_invert = False
        _power_unit_ov = None
        for m_data in cfg.get("meters", {}).values():
            meta = (m_data or {}).get("meta", {}) or {}
            if not meta.get("sub_meter"):
                power_sensor = meta.get("power_sensor")
                power_source = meta.get("power_source")
                # invert / unit-override apply to the user's OWN sensor only,
                # never to the BCD fallback (whose convention we don't control).
                if power_sensor:
                    _power_invert = bool(meta.get("power_invert", False))
                    _power_unit_ov = meta.get("power_unit") or None
                break
        # Auto-adopt BottlecapDave's current_demand when no sensor is configured —
        # it's a free local HA entity, so we can sample it every tick.
        if not power_sensor:
            bcd = (_detected_integrations or {}).get("bcd") or {}
            if bcd.get("found"):
                power_sensor = bcd.get("demand_sensor")

        net_kw = None
        if power_sensor and ha:
            raw_kw = ha.get_state(power_sensor)
            if raw_kw not in (None, "unknown", "unavailable"):
                # Respect the sensor's unit (BCD current_demand is in watts).
                _unit = None
                try:
                    _unit = (ha.get_attributes(power_sensor) or {}).get(
                        "unit_of_measurement")
                except Exception:
                    pass
                net_kw = _power_value_to_kw(raw_kw, _unit,
                                            _power_unit_ov, _power_invert)
        elif (not power_sensor and power_source == "mini"
              and _kraken_mini_reader
              and getattr(_kraken_mini_reader, "device_id", None)):
            # Octopus Mini source — poll it here (throttled) so the 48h history
            # fills continuously, independent of whether the page is open.
            net_kw = await _poll_mini_demand_kw(now)

        if net_kw is not None:
            # Carbon rate: net_kw (kW) × intensity (gCO2/kWh) ÷ 60 = gCO2/min.
            # Derived directly from the power sensor — the kWh sensor only updates
            # every ~60s while the engine ticks every ~10s, so the read-delta approach
            # produced zeros (sensor unchanged) and spikes (full 60s of kWh over a
            # ~10s dt_min). The power sensor is already the correct instantaneous value.
            carbon_gco2_min = None
            if current_intensity is not None:
                try:
                    carbon_gco2_min = round(net_kw * current_intensity / 60.0, 4)
                except Exception as _ce:
                    logger.debug("_engine_tick: carbon_gco2_min skipped: %s", _ce)

            _store.append_power_history(now.isoformat(), net_kw,
                                        current_intensity, carbon_gco2_min)
            _store.prune_power_history(hours=48)
    except Exception as e:
        logger.warning("_engine_tick: power_history write skipped: %s", e)

    # Write sub_meter_history rows every tick for battery/EV sub-meters
    try:
        cfg = load_config()
        _pruned_sub = False
        for mid, m_data in cfg.get("meters", {}).items():
            meta = (m_data or {}).get("meta", {}) or {}
            if not meta.get("sub_meter"):
                continue
            soc_s = meta.get("soc_sensor")
            inv_s = meta.get("inverter_power_sensor")
            dev_s = meta.get("device_power_sensor")
            if not soc_s and not inv_s and not dev_s:
                continue  # no sensors configured for this sub-meter
            soc_val = inv_val = None
            if soc_s and ha:
                v = ha.get_state(soc_s)
                if v not in (None, "unknown", "unavailable"):
                    try:
                        soc_val = round(float(v), 1)
                    except (ValueError, TypeError):
                        pass
            # Use inverter_power_sensor for batteries, device_power_sensor for EV/heat pump.
            # Pick the matching invert/unit-override flags for whichever drives it.
            if inv_s:
                power_s = inv_s
                _sub_invert = bool(meta.get("inverter_power_invert", False))
                _sub_unit_ov = meta.get("inverter_power_unit") or None
            else:
                power_s = dev_s
                _sub_invert = bool(meta.get("device_power_invert", False))
                _sub_unit_ov = meta.get("device_power_unit") or None
            if power_s and ha:
                v = ha.get_state(power_s)
                if v not in (None, "unknown", "unavailable"):
                    try:
                        unit = (ha.get_attributes(power_s) or {}).get(
                            "unit_of_measurement", "")
                    except Exception:
                        unit = ""
                    # Shared converter: unit attr → kW, override when declared
                    # unit is wrong/absent, then invert. Falls back to the
                    # magnitude heuristic when the unit is missing (previously this
                    # path assumed kW on a missing unit — a W sensor read 1000× high).
                    inv_val = _power_value_to_kw(v, unit, _sub_unit_ov, _sub_invert)
            if soc_val is not None or inv_val is not None:
                _store.append_sub_meter_history(now.isoformat(), mid, soc_val, inv_val)
                if not _pruned_sub:
                    _store.prune_sub_meter_history(hours=48)
                    _pruned_sub = True
    except Exception as e:
        logger.warning("_engine_tick: sub_meter_history write skipped: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Startup  (replaces @time_trigger("startup"))
# ─────────────────────────────────────────────────────────────────────────────


def _read_kraken_creds_file() -> dict:
    """Read credentials from KRAKEN_CREDS_PATH, or {} if absent/unreadable."""
    try:
        with open(KRAKEN_CREDS_PATH, "r") as f:
            import json as _json
            data = _json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def save_kraken_credentials(api_key, account_number, base_url=None) -> None:
    """Persist API credentials to their own file (0600), outside the DB so they
    are never included in backups. Passing None/'' for api_key clears the file
    (disconnect). Account/base_url are stored alongside."""
    import json as _json
    ensure_dir(DATA_DIR)
    key = (api_key or "").strip()
    if not key:
        # Disconnect: remove the file entirely.
        try:
            os.remove(KRAKEN_CREDS_PATH)
        except FileNotFoundError:
            pass
        return
    payload = {"api_key": key,
               "account_number": (account_number or "").strip() or None,
               "base_url": (base_url or "").strip() or None}
    tmp = KRAKEN_CREDS_PATH + ".tmp"
    with open(tmp, "w") as f:
        _json.dump(payload, f)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, KRAKEN_CREDS_PATH)


def _kraken_env() -> dict:
    """Resolve Kraken credentials. Prefers the in-app credentials file
    (KRAKEN_CREDS_PATH); falls back to environment variables for backward
    compatibility (the 2.x add-on-config path). Normalises empties to None.
    Returns {api_key, account_number, base_url}."""
    def _clean(v):
        v = (v or "").strip()
        return None if v in ("", "null", "None") else v
    creds = _read_kraken_creds_file()
    if creds.get("api_key"):
        return {
            "api_key": _clean(creds.get("api_key")),
            "account_number": _clean(creds.get("account_number")),
            "base_url": _clean(creds.get("base_url")),
        }
    return {
        "api_key": _clean(os.environ.get("KRAKEN_API_KEY")),
        "account_number": _clean(os.environ.get("KRAKEN_ACCOUNT_NUMBER")),
        "base_url": _clean(os.environ.get("KRAKEN_BASE_URL")),
    }


def has_kraken_credentials() -> bool:
    """True if a Kraken API key is configured (creds file or env). Used to guard
    against activating an API mode with no credentials (Change Setup cad→api)."""
    try:
        return bool(_kraken_env().get("api_key"))
    except Exception:
        return False


async def connect_kraken_now() -> dict:
    """(Re)build the Kraken client from current credentials and run discovery
    immediately — used after the user enters credentials in-app, so they get a
    live connection result without restarting. Returns a status dict:
      {ok, connected, account_number?, import_mpan?, export_mpan?, mini?, detail?}
    Never raises."""
    env = _kraken_env()
    if not env["api_key"]:
        return {"ok": False, "connected": False, "detail": "no_api_key"}
    try:
        await _kraken_startup_discovery(force=True)
    except Exception as e:
        logger.warning("connect_kraken_now: %s", e, exc_info=e)
        return {"ok": False, "connected": False, "detail": _error_text("connecting to Octopus", e)}
    if _kraken_client is None or not _kraken_discovery:
        return {"ok": False, "connected": False, "detail": "discovery_failed"}
    imp = _kraken_discovery.get("import") or {}
    exp = _kraken_discovery.get("export") or {}
    # Also (re)build rate schedules so reconcile has them without waiting a poll — after answering.
    # The page waits 45 s for this reply, and an Agile tariff's schedule is its whole price history:
    # a beta tester's Agile Outgoing export was 137,588 half-hours from 2018, the wizard timed out on
    # its credentials step, and it never reached the meter (3 Nov 2026).
    _start_rate_refresh()
    # Mode promotion (#381): a live in-app connect just SUCCEEDED. If the
    # persisted data-source mode does not use the API — e.g. a prior disconnect
    # or a DB-swap left it parked on 'cad'/'unset' while the credentials
    # survived — promote it now. Otherwise the startup mode-gate
    # (mode_uses_api()) skips auto-activation on the NEXT restart and the user
    # falls straight back into the "creds present but API off" limbo, so
    # Reconnect appears to "not stick" and silently reverts to cad. Promote to
    # 'cad+api' when a local import sensor exists (keep the CAD feed), else
    # 'api'. A successful connect means the supplier IS API-capable, so this
    # never strands an api mode without a reachable API.
    try:
        _prev_mode = get_data_source_mode()
        if not mode_uses_api(_prev_mode):
            _promoted = "cad+api" if _has_local_import_sensor() else "api"
            set_data_source_mode(_promoted)
            logger.info(
                "connect_kraken_now: live connect succeeded while mode did not "
                "use the API — promoted data-source mode %r -> %r so activation "
                "persists across restart.", _prev_mode, _promoted)
    except Exception as _pm:
        logger.warning("connect_kraken_now: mode promotion failed: %s", _pm)
    # NB: the DCC poll task is launched at the END of engine_startup (after the
    # store + client are rebuilt), NOT here — launching it mid-connect raced the
    # config-save's engine_startup teardown of those shared resources.
    return {
        "ok": True, "connected": True,
        "account_number": _kraken_discovery.get("account_number"),
        "import_mpan": imp.get("mpan"),
        "export_mpan": exp.get("mpan") or None,
        "mini": _kraken_mini_reader is not None,
    }


async def disconnect_kraken() -> dict:
    """Destructive 'disconnect Octopus' action (MODE-UI §5): clear stored API
    credentials and API-derived runtime state, stop polling immediately, tear
    down the live client + Mini reader, and land the install on local-sensor
    mode ('cad').

    Kept on purpose: historical DCC-settled billing data (per-block imp_kwh_api /
    exp_kwh_api + materialised cost) — that's billing history, not a credential —
    and the billing source, so existing settled blocks keep their materialisation
    and future local blocks bill off local figures exactly as a cad-only install
    does. NOT re-priced.

    Lands on 'cad' because api / api+mini with no credentials is non-functional;
    'cad' makes mode_uses_api() False and surfaces the local meter-sensor fields
    in the config UI, so the user can add a sensor (or reconnect) to resume. Runs
    on the engine loop (touches the asyncio poll-task handle). Returns
    {ok, mode, had_credentials}. Never raises.
    """
    global _kraken_client, _kraken_mini_reader, _kraken_discovery
    global _kraken_rate_schedules, _kraken_standing_schedule
    global _kraken_account_mismatch
    _kraken_account_mismatch = None
    had = has_kraken_credentials()
    try:
        # 1. Stop the live poll loop and drop all in-memory API state, so polling
        #    stops NOW (not just on the next restart) — the existing 'send empty
        #    api_key' path only deleted the file and left the in-memory client
        #    polling.
        _cancel_kraken_poll_task()
        _cancel_rate_refresh()
        _kraken_client = None
        _kraken_mini_reader = None
        _kraken_discovery = None
        _kraken_rate_schedules = {}
        _kraken_standing_schedule = None
        # 2. Remove the credentials file (the actual disconnect).
        save_kraken_credentials(None, None)
        # 3. Wipe API-derived progress markers so a future reconnect starts clean
        #    (fresh backfill). Mode and
        #    billing source also live in kraken_state but are NOT progress markers
        #    — they are set / preserved explicitly, never blanket-deleted.
        try:
            from kraken_ingester import _STATE_LAST_POLL as _LAST_POLL_KEY
        except Exception:
            _LAST_POLL_KEY = "last_poll_utc"
        for k in (_LAST_POLL_KEY, _STATE_LAST_SWEEP):
            try:
                _store.delete_kraken_state(k)
            except Exception:
                pass
        # 4. Land on local-sensor mode (api-without-creds is invalid).
        set_data_source_mode("cad")
        logger.info("disconnect_kraken: credentials cleared (had=%s), API client/"
                    "Mini/discovery torn down, progress state wiped, mode→cad", had)
        return {"ok": True, "mode": "cad", "had_credentials": had}
    except Exception as e:
        logger.error("disconnect_kraken: failed: %s", e, exc_info=e)
        return {"ok": False, "detail": _error_text("disconnecting", e), "had_credentials": had}


def _cancel_kraken_poll_task() -> None:
    """Cancel the running DCC poll task, if any. Called before engine_startup
    tears down the store/client so an in-flight backfill can't operate on closed
    resources. Best-effort; the task is relaunched at the end of startup."""
    global _kraken_poll_task_handle
    h = _kraken_poll_task_handle
    if h is not None and not h.done():
        h.cancel()
        logger.info("_cancel_kraken_poll_task: cancelled in-flight poll")
    _kraken_poll_task_handle = None


def _start_rate_refresh() -> None:
    """connect_kraken_now()'s rate schedules, built on the engine loop after it has answered; a
    refresh already running is replaced."""
    global _kraken_rates_task_handle
    _cancel_rate_refresh()

    async def _run():
        try:
            await _refresh_kraken_rate_schedules()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("connect_kraken_now: rate schedules not built: %s", e)

    _kraken_rates_task_handle = asyncio.ensure_future(_run())


def _cancel_rate_refresh() -> None:
    """Cancel the rate refresh connect_kraken_now() started, if it is still running — before the
    client it reads is closed."""
    global _kraken_rates_task_handle
    h = _kraken_rates_task_handle
    if h is not None and not h.done():
        h.cancel()
        logger.info("_cancel_rate_refresh: cancelled the rate refresh in flight")
    _kraken_rates_task_handle = None


def _ensure_kraken_poll_task_running() -> bool:
    """Launch kraken_poll_task on the engine loop if it isn't already running.
    Idempotent and safe to call repeatedly (e.g. on every credential save).
    Returns True if a task is running afterwards.

    If the engine loop isn't up yet (_engine_ha is None), we're still in boot —
    main() launches the poll task via its gather() a moment later, so this is
    expected and NOT a problem. We log it at INFO (not WARNING) to avoid a
    misleading 'engine loop not up' alarm on a normal pre-configured startup."""
    global _kraken_poll_task_handle
    if _engine_ha is None:
        logger.info("_ensure_kraken_poll_task_running: engine loop not up yet "
                    "— poll task will be launched by startup")
        return False
    h = _kraken_poll_task_handle
    if h is not None and not h.done():
        # Already running — nothing to do.
        return True
    try:
        loop = asyncio.get_event_loop()
        _kraken_poll_task_handle = loop.create_task(kraken_poll_task(_engine_ha))
        logger.info("_ensure_kraken_poll_task_running: poll task launched")
        return True
    except Exception as e:
        logger.warning("_ensure_kraken_poll_task_running: could not launch: %s", e)
        return False


async def _kraken_startup_discovery(force: bool = False) -> None:
    """If a Kraken API key is configured, construct the client and run
    auto_discover ONCE at startup, logging what was found.

    force=True bypasses the fresh-setup guard — used by connect_kraken_now()
    when the user explicitly enters credentials in the wizard (the DB may still
    be empty mid-setup, but the connect is intentional).

    It performs the read-only account fetch discovery needs, and writes only the
    account stamp and `export_settlement_expected` (it polls no consumption). Any
    failure is logged and swallowed — API misconfiguration must never break engine
    startup or the CAD pipeline.
    """
    global _kraken_client, _kraken_discovery, _kraken_account_number, _kraken_discovered_at
    env = _kraken_env()
    if not env["api_key"]:
        logger.info("kraken_discovery: no API key configured — API integration off")
        return
    # API-activation gate: only auto-activate the supplier API when a mode has
    # been EXPLICITLY chosen (by the survey) AND that mode uses the API. A flat/
    # pre-survey DB reports mode 'unset' → no auto-activation, even though an
    # orphaned credentials file may still exist (it lives outside the DB, so a
    # flatten doesn't clear it). The wizard re-establishes the mode + creds.
    # force=True (the wizard's own connect) bypasses this.
    if not force and not mode_uses_api():
        logger.info("kraken_discovery: data-source mode is '%s' — API not "
                    "auto-activated (awaiting survey / not an API mode)",
                    get_data_source_mode())
        return
    try:
        from kraken_api_client import KrakenAPIClient
    except Exception as e:
        logger.warning("kraken_discovery: client import failed: %s", e)
        return
    try:
        kwargs = {"account_number": env["account_number"]}
        if env["base_url"]:
            kwargs["base_url"] = env["base_url"]
        # Close any previous client first — connect_kraken_now() re-runs this on
        # every in-app credential save, and leaking the old aiohttp session
        # ("Unclosed client session") accumulates connections.
        if _kraken_client is not None:
            try:
                await _kraken_client.close()
            except Exception:
                pass
            _kraken_client = None
        _kraken_client = KrakenAPIClient(env["api_key"], **kwargs)
        # test_connection first — clean credential check, never raises.
        conn = await _kraken_client.test_connection()
        if not conn.get("ok"):
            logger.warning("kraken_discovery: connection check failed: %s",
                           conn.get("detail"))
            return
        acct = env["account_number"] or conn.get("account_number")
        # DB↔account stamp guard (#357). A DB is stamped with its Octopus account on
        # first successful discovery. If the credentials now present are for a
        # DIFFERENT account (operator swapped to another user's DB), do NOT
        # auto-activate — that would poll the wrong account's data into this DB.
        # Credentials are KEPT (never silently dropped). An explicit in-app reconnect
        # (force=True) re-associates the DB with the new account.
        global _kraken_account_mismatch
        from kraken_api_client import _mask as _m
        stamped = get_db_account()
        if stamped and acct and _norm_account(stamped) != _norm_account(acct):
            if not force:
                _kraken_account_mismatch = (stamped, acct)
                logger.warning(
                    "kraken_discovery: this DB is stamped to account %s but the "
                    "configured credentials are for %s — API NOT auto-activated. "
                    "Credentials kept; reconnect in-app to re-associate this DB.",
                    _m(stamped), _m(acct))
                try:
                    await _kraken_client.close()
                except Exception:
                    pass
                _kraken_client = None
                _kraken_discovery = {}
                return
            logger.info("kraken_discovery: explicit reconnect re-associates this DB "
                        "from account %s to %s", _m(stamped), _m(acct))
            _store.set_kraken_state(_ACCOUNT_KEY, acct)
        elif acct and not stamped:
            _store.set_kraken_state(_ACCOUNT_KEY, acct)   # first association
        _kraken_account_mismatch = None
        _kraken_account_number = acct
        disc = await _kraken_client.auto_discover(acct)
        _kraken_discovery = disc
        _kraken_discovered_at = time.monotonic()
        _keep_agreements(disc)
        _find_home_eras(disc)                         # BL-101 PR 3: a move or a meter change

        imp = disc.get("import") or {}
        exp = disc.get("export") or {}
        _store_export_settlement_expected(disc)
        logger.info("=" * 60)
        logger.info("kraken_discovery: account verified — REVIEW BEFORE ENABLING POLLING")
        logger.info("  account_number : %s", _m(disc.get("account_number")))
        logger.info("  properties     : %s", disc.get("properties"))
        logger.info("  IMPORT mpan=%s serial=%s tariff=%s product=%s",
                    _m(imp.get("mpan")), _m(imp.get("serial")),
                    imp.get("tariff_code"), imp.get("product_code"))
        if exp:
            logger.info("  EXPORT mpan=%s serial=%s tariff=%s product=%s",
                        _m(exp.get("mpan")), _m(exp.get("serial")),
                        exp.get("tariff_code"), exp.get("product_code"))
        else:
            logger.info("  EXPORT : none detected")
        for w in disc.get("warnings", []):
            logger.warning("  ⚠ %s", w)
        logger.info("=" * 60)
        # Chunk 7: discover an Octopus Mini and, if present, wire the
        # near-real-time provisional import layer (api+mini). Best-effort:
        # any failure leaves the Mini layer off and DCC still reconciles.
        await _maybe_setup_mini()
        # Point-of-truth config-state line: now the reader is resolved, report
        # the ACTUAL Mini provisional state (the startup dump could only report
        # eligibility, as this runs later in startup).
        logger.info("config-state: mini_provisional=%s",
                    "active" if _kraken_mini_reader is not None else "off")
    except Exception as e:
        logger.warning("kraken_discovery: discovery failed (non-fatal): %s", e)


def _export_agreement_now(disc) -> bool:
    """An export agreement in `disc` covers now. The pill's account-level test for whether export
    is expected to settle (17 Oct 2026, the user: judged once a day, at discovery). It was "the
    account has an export MPAN or any export agreement at all", so an export MPAN with no
    agreement for the current period counted every export half-hour as awaiting settlement."""
    import api_import as _ai
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    for a in ((disc or {}).get("export") or {}).get("agreements") or []:
        try:
            vf = _ai._iso(_ai._naive_utc(a.get("valid_from"))) if a.get("valid_from") else None
            vt = _ai._iso(_ai._naive_utc(a.get("valid_to"))) if a.get("valid_to") else None
        except Exception:
            continue
        if vf and vf <= now and (vt is None or now < vt):
            return True
    return False


def _store_export_settlement_expected(disc) -> None:
    """Persist whether export is expected to DCC-settle, so the unsettled-blocks badge only
    treats export as 'awaiting settlement' when an export agreement covers the period (a
    FIT/deemed-export account, or an export MPAN between agreements, has none — counting it is a
    permanent false alarm). Default (flag absent) stays TRUE so a real gap is never silenced."""
    try:
        _store.set_meta("export_settlement_expected", _export_agreement_now(disc))
    except Exception:
        pass


def _agreements_signature(disc) -> tuple:
    """What re-discovery compares: each channel's agreements (tariff, from, to)."""
    out = []
    for ch in ("import", "export"):
        ags = ((disc or {}).get(ch) or {}).get("agreements") or []
        out.append((ch, tuple(sorted((a.get("tariff_code"), a.get("valid_from"), a.get("valid_to"))
                                     for a in ags))))
    return tuple(out)


def _find_home_eras(disc=None) -> int:
    """BL-101 PR 3 (home eras design §2): the eras EMT can see for itself, added to the user's — a
    house move per supply point after the first in the stored import agreements (dated at its first
    agreement), and a change of the import meter point discovery reports (a new supply point a move,
    a new serial a meter change; dated the day EMT noticed). Points are kept as fingerprints only, and
    a found era the user deleted is not found again. Display only. Returns how many were added; never
    raises."""
    if _store is None:
        return 0
    try:
        import home_eras as _he
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(_store.site_timezone() or "UTC")

        def _local(iso):
            return (datetime.fromisoformat(str(iso)[:19]).replace(tzinfo=timezone.utc)
                    .astimezone(tz).date().isoformat())
        today = datetime.now(timezone.utc).astimezone(tz).date().isoformat()
        acct = _kraken_account_number or get_db_account()
        changes = [dict(m, kind="house_move") for m in
                   _he.moves_from_agreements(_store.get_tariff_agreements(acct) if acct else [], _local)]
        imp = (disc or {}).get("import") or {}
        seen = _store.get_meta(_he.POINTS_KEY) or []
        seen2, ch = _he.note_point(seen, imp.get("mpan"), imp.get("serial"), today)
        if seen2 != seen:
            _store.set_meta(_he.POINTS_KEY, seen2)
        if ch:
            changes.append(ch)
        if not changes:
            return 0
        periods = _store.get_config_periods() or []
        site = (periods[-1].get("site_name") or "") if periods else ""
        eras = _he.load(_store)
        new = _he.found_eras(changes, eras, _store.get_meta(_he.DISMISSED_KEY) or [], site)
        if new:
            _store.set_meta(_he.META_KEY, _he.clean(eras + new))
            logger.info("home eras: found %s", ", ".join("%s from %s" % (e["label"], e["start"]) for e in new))
        return len(new)
    except Exception as e:
        logger.warning("home eras: finding them failed (%s)", e)
        return 0


def _meter_points(disc) -> tuple:
    return tuple((((disc or {}).get(ch) or {}).get("mpan"), ((disc or {}).get(ch) or {}).get("serial"))
                 for ch in ("import", "export"))


async def _maybe_rediscover(now: "float | None" = None) -> bool:
    """Ask Octopus for the account's agreements again, at most once a day (`_REDISCOVERY_INTERVAL_S`),
    and take them where the meter points are the same. Returns whether the agreements changed; the
    caller's rate-schedule refresh then builds on them in the same cycle. Everything judged by
    agreement reads them: `split_at`, `capped_at`, `_smb_windows`, `tariff_kind_at`,
    `export_agreement_at` and the pill's export flag.

    The light half of `_kraken_startup_discovery`: the client, the account check and the Mini are
    left as they are. A changed MPAN or meter serial is not taken — the ingester was built for the
    old ones — and is logged for a reconnect. A failure retries in an hour. Never raises."""
    global _kraken_discovery, _kraken_discovered_at
    t = time.monotonic() if now is None else now
    if (_kraken_client is None or not _kraken_discovery or _kraken_account_mismatch
            or not _kraken_account_number):
        return False
    if _kraken_discovered_at is not None and t - _kraken_discovered_at < _REDISCOVERY_INTERVAL_S:
        return False
    try:
        disc = await _kraken_client.auto_discover(_kraken_account_number)
    except Exception as e:
        logger.warning("kraken_rediscovery: failed (%s) — retrying in an hour", e)
        _kraken_discovered_at = t - _REDISCOVERY_INTERVAL_S + _REDISCOVERY_RETRY_S
        return False
    _kraken_discovered_at = t
    if not disc or not disc.get("import"):
        logger.warning("kraken_rediscovery: no import meter point in the answer — kept the last")
        return False
    if _meter_points(disc) != _meter_points(_kraken_discovery):
        logger.warning("kraken_rediscovery: the account's meter points have changed (a new MPAN or "
                       "meter) — not taken; reconnect in Settings to use them")
        _find_home_eras(disc)                         # BL-101 PR 3: dated the day it was seen
        return False
    changed = _agreements_signature(disc) != _agreements_signature(_kraken_discovery)
    if changed:
        logger.info("kraken_rediscovery: agreements changed — import tariff %s -> %s, export "
                    "tariff %s -> %s",
                    (_kraken_discovery.get("import") or {}).get("tariff_code"),
                    (disc.get("import") or {}).get("tariff_code"),
                    (_kraken_discovery.get("export") or {}).get("tariff_code"),
                    (disc.get("export") or {}).get("tariff_code"))
    _kraken_discovery = disc
    _keep_agreements(disc)
    _find_home_eras(disc)
    _store_export_settlement_expected(disc)
    return changed


async def _maybe_retry_discovery(now: "float | None" = None) -> bool:
    """Start-up discovery again, at most hourly, while an API mode has credentials and discovery has
    not answered (G1 design §5: the settle sweep waits for it). The first tick after start-up only
    sets the clock. A refused account (a mismatch) is not retried: it needs a reconnect. On an answer
    the agreements are stored (G1a), the poll task started and the settle sweep scheduled. Returns
    whether discovery answered. Never raises."""
    global _discovery_retry_at
    t = time.monotonic() if now is None else now
    try:
        if (_kraken_discovery or _kraken_account_mismatch or not mode_uses_api()
                or not _kraken_env().get("api_key")):
            return False
    except Exception:
        return False
    if _discovery_retry_at is None or t < _discovery_retry_at:
        if _discovery_retry_at is None:
            _discovery_retry_at = t + _DISCOVERY_RETRY_S
        return False
    _discovery_retry_at = t + _DISCOVERY_RETRY_S
    try:
        await _kraken_startup_discovery()
    except Exception as e:
        logger.warning("kraken_discovery: retry failed (%s) — again in an hour", e)
        return False
    if not _kraken_discovery:
        logger.info("kraken_discovery: retry had no answer — again in an hour")
        return False
    logger.info("kraken_discovery: answered on retry — scheduling the settle sweep")
    try:
        if _engine_ha is not None and _kraken_client is not None and mode_uses_api():
            _ensure_kraken_poll_task_running()
        _maybe_settle_history()
    except Exception as e:
        logger.warning("kraken_discovery: after the retry: %s", e)
    return True


def _start_discovery_retry() -> None:
    """`_maybe_retry_discovery` as a loop task, one at a time, so a retry against an API that is down
    never holds the engine tick."""
    global _discovery_retry_running
    if (_discovery_retry_running or _kraken_discovery
            or (_discovery_retry_at is not None and time.monotonic() < _discovery_retry_at)):
        return
    import asyncio as _aio
    try:
        loop = _aio.get_running_loop()
    except RuntimeError:
        return

    async def _go():
        global _discovery_retry_running
        try:
            await _maybe_retry_discovery()
        finally:
            _discovery_retry_running = False
    _discovery_retry_running = True
    loop.create_task(_go())


# ── Agile for everyone (BL-103, agile for everyone design) ─────────────────────────────────────────
_agile_public_running = False


def _agile_public_channels() -> list:
    """The directions to fetch Agile's public prices for: import unless the account is on Agile
    itself (its own schedule has them), export where it exports and is not on Agile Outgoing."""
    import tariff_prices as _tp
    disc = _kraken_discovery or {}
    out = []
    for ch in ("import", "export"):
        d = disc.get(ch) or {}
        if ch == "export" and not d:
            continue
        if not _tp.is_dynamic_code(d.get("tariff_code") or ""):
            out.append(ch)
    return out


def _agile_public_due() -> bool:
    """A cheap test, every tick: an API account with a region, a direction to fetch, and the record
    short of what it should hold (agile_public.due)."""
    if _store is None or _kraken_client is None or not _kraken_discovery:
        return False
    import agile_public as _ap
    from zoneinfo import ZoneInfo
    if not _ap.region_of(((_kraken_discovery.get("import") or {}).get("tariff_code")) or ""):
        return False
    chans = _agile_public_channels()
    if not chans:
        return False
    try:
        tz = ZoneInfo(_store.site_timezone() or "Europe/London")
        now = datetime.now(timezone.utc)
        return _ap.due(_reader().get_meta(_ap.META_KEY) or {}, now.replace(tzinfo=None), now.astimezone(tz), tz, chans)
    except Exception:
        return False


def _start_agile_public_fetch() -> None:
    """`_fetch_agile_public` as a loop task when it is due, one at a time, so a slow Octopus never
    holds the engine tick."""
    global _agile_public_running
    if _agile_public_running or not _agile_public_due():
        return
    import asyncio as _aio
    try:
        loop = _aio.get_running_loop()
    except RuntimeError:
        return

    async def _go():
        global _agile_public_running
        try:
            await _fetch_agile_public()
        finally:
            _agile_public_running = False
    _agile_public_running = True
    loop.create_task(_go())


async def _fetch_agile_public() -> dict:
    """BL-103: ask Octopus for Agile's prices in the account's region — the product list once a week,
    the prices from where the record ends (30 days back the first time) to the end of tomorrow — and
    keep them in the store's meta. At most a call a direction, MAX_TRIES_A_DAY times a day, 90
    minutes apart (agile_public.due). Returns the record; never raises."""
    import agile_public as _ap
    from zoneinfo import ZoneInfo
    try:
        rec = dict(_reader().get_meta(_ap.META_KEY) or {})
        tz = ZoneInfo(_store.site_timezone() or "Europe/London")
        now = datetime.now(timezone.utc)
        now_utc, now_local = now.replace(tzinfo=None), now.astimezone(tz)
        region = _ap.region_of(((_kraken_discovery or {}).get("import") or {}).get("tariff_code") or "")
        today = now_local.date().isoformat()
        rec["asked"] = now_utc.isoformat()
        rec["tries"] = {today: int((rec.get("tries") or {}).get(today, 0)) + 1}
        rec["region"] = region
        if _ap.products_due(rec, now_utc) or not all((rec.get(ch) or {}).get("product") for ch in _agile_public_channels()):
            products = await _kraken_client.get_products()
            for ch in ("import", "export"):
                p = _ap.pick_product(products, ch)
                if p:
                    rec.setdefault(ch, {})["product"] = p
            rec["products_checked"] = now_utc.isoformat()
        want = _ap.target_through(now_local, tz)
        for ch in _agile_public_channels():
            c = rec.setdefault(ch, {})
            if not c.get("product") or not region:
                continue
            tc = _ap.tariff_code(c["product"], region)
            if c.get("tariff") != tc:
                c["prices"] = []                    # a new product or region: start again
            c["tariff"] = tc
            have = _ap.through_of(c.get("prices") or [])
            frm = have or (now_utc - timedelta(days=_ap.COMPARE_DAYS + 1)).replace(minute=0, second=0, microsecond=0)
            if frm >= want:
                continue
            recs = await _kraken_client.get_unit_rates(
                c["product"], tc, period_from=frm.isoformat() + "Z", period_to=want.isoformat() + "Z")
            c["prices"] = _ap.merge(c.get("prices") or [], recs, now_utc)
            c["fetched"] = now_utc.isoformat()
            logger.info("agile prices: %s %s to %s (%d half-hours kept)", ch, tc,
                        (_ap.through_of(c["prices"]) or now_utc).isoformat(), len(c["prices"]))
        _store.set_meta(_ap.META_KEY, rec)
        return rec
    except Exception as e:
        logger.warning("agile prices: the fetch failed (%s) — asked again later", e)
        try:
            r = dict(_reader().get_meta(_ap.META_KEY) or {})
            r["asked"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            _store.set_meta(_ap.META_KEY, r)
        except Exception:
            pass
        return {}


def _teardown_mini_if_no_api() -> bool:
    """Tear down a stale Mini reader when the mode no longer uses the API.

    On a Change Setup api→cad transition the engine restarts (engine_startup),
    but _kraken_startup_discovery returns early in a non-API mode and never calls
    _maybe_setup_mini — so a Mini reader wired in a prior api+mini session would
    PERSIST (same process) and keep collecting into cad blocks. This clears it.
    Idempotent; returns True if it tore a reader down. Safe to call any time.
    """
    global _kraken_mini_reader
    if not mode_uses_api() and _kraken_mini_reader is not None:
        _kraken_mini_reader = None
        logger.info("teardown_mini: mode=%s — tore down stale Mini reader "
                    "(API path off)", get_data_source_mode())
        return True
    return False


async def _maybe_setup_mini() -> None:
    """If a Mini smart device exists AND there is no local meter read source,
    build the boundary reader and register it on the block-boundary hook.

    Gating rule (per design): Mini is a FALLBACK for when EMT has no local
    import feed. If a CAD / main-meter import read sensor is configured, that is
    the authoritative source and Mini must NEVER be used — even if a Mini device
    exists and even if mode were set to api+mini. Local sensors always win, so
    Mini and CAD can never contend for the same block's import. Best-effort;
    never raises."""
    global _kraken_mini_reader
    if _kraken_client is None:
        return
    if _has_local_import_sensor():
        logger.info("mini_setup: local meter import sensor present — Mini not "
                    "used (CAD/local is authoritative)")
        return
    # Mini is AUTOMATIC, not a survey choice: the survey derives cad/cad+api/api
    # only. In a no-local API mode, if a Mini device is actually present it
    # elevates behaviour to api+mini at runtime. So the gate is "mode uses API
    # and there's no local feed" — then we try to discover a device. (A cad/
    # cad+api setup has a local feed and is already excluded above.)
    if not mode_uses_api():
        logger.info("mini_setup: mode is %s (no API) — Mini layer off",
                    get_data_source_mode())
        return
    try:
        device_id = await _kraken_client.get_device_id(_kraken_account_number)
    except Exception as e:
        logger.warning("mini_setup: device discovery failed: %s", e)
        return
    if not device_id:
        logger.info("mini_setup: no Octopus Mini on account — staying plain API "
                    "(DCC settlement only)")
        return
    try:
        from kraken_mini import MiniBoundaryReader
        _kraken_mini_reader = MiniBoundaryReader(_kraken_client, device_id)
        # 3.0.0: Mini reads are collected per-tick into the current block's import buffer
        # (CAD-like) and bracketed/interpolated by finalise — see _collect_mini_into_block.
        # (The post-finalise boundary-read path it replaced, dormant since, went 22 Oct 2026.)
        logger.info("mini_setup: Mini provisional import layer ACTIVE "
                    "(no local sensor; per-tick boundary collection → "
                    "provisional, DCC reconciles)")
    except Exception as e:
        logger.warning("mini_setup: failed to wire Mini reader: %s", e)
        _kraken_mini_reader = None


def _has_local_import_sensor() -> bool:
    """True if any non-sub main meter has a local import 'read' sensor
    configured — i.e. a CAD/local feed exists and Mini must stand down."""
    try:
        cfg = load_config()
        return any(
            (meter.get("channels") or {}).get("import", {}).get("read")
            for meter in (cfg.get("meters") or {}).values()
            if not (meter.get("meta") or {}).get("sub_meter")
        )
    except Exception:
        # If config can't be read, assume a local sensor MIGHT exist and keep
        # Mini off — the safe default (never contend with a possible CAD feed).
        return True


def _main_meter_id() -> "str | None":
    """Return the id of the main (non-sub) meter from config, or None."""
    try:
        cfg = load_config()
        for mid, meter in (cfg.get("meters") or {}).items():
            if not (meter.get("meta") or {}).get("sub_meter"):
                return mid
    except Exception:
        pass
    return None


def _mini_collection_window_open(current_block: dict, now: datetime,
                                 block_minutes: int) -> bool:
    """True when we should be collecting Mini telemetry for the CURRENT block's
    upcoming boundary: from ~20s BEFORE the block end until a read timestamped
    at/after the boundary has landed (bracketing complete). The reader itself
    enforces the per-boundary call cap and drift pacing."""
    try:
        start = datetime.fromisoformat(current_block["start"])
    except (ValueError, KeyError, TypeError):
        return False
    boundary = start + timedelta(minutes=block_minutes)
    seconds_to_boundary = (boundary - now).total_seconds()
    # Open from 20s before the boundary onward (and remain open past it — the
    # reader stops itself once the post-boundary point is collected).
    return seconds_to_boundary <= _MINI_COLLECT_LEAD_S


# A gap fill waiting for its post-outage read on a Mini-only install asks the Mini for its latest
# register, sparingly — the telemetry API is rate-limited: at most this many tries per gap, this far
# apart. Then the boundary collection (20 s before the next boundary) is the fallback, as before.
_GAP_MINI_TRIES = 3
_GAP_MINI_EVERY_S = 300


async def _mini_post_outage_read(current_block: dict, now: datetime) -> bool:
    """For a gap waiting on its post-outage read with no local import sensor: ask the Mini for its
    latest import register (ONE call, paced by the marker — survives a restart) and add it to the
    current block's main import reads, when it is newer than the gap's last known read. A restart
    mid-half-hour otherwise waited for the next boundary's Mini collection, up to 30 minutes (Pi soak,
    19 Oct 2026). True when a read was added."""
    reader = _kraken_mini_reader
    marker = current_block.get("_gap_marker")
    if reader is None or not marker or _has_local_import_sensor():
        return False
    main_id = _main_meter_id()
    if not main_id:
        return False
    if any((ch or {}).get("reads") for m in (current_block.get("meters") or {}).values()
           for ch in ((m or {}).get("channels") or {}).values()):
        return False                                    # a read is there: nothing to ask
    tries = int(marker.get("mini_tries") or 0)
    if tries >= _GAP_MINI_TRIES or (marker.get("mini_next") and iso(now) < marker["mini_next"]):
        return False
    marker["mini_tries"] = tries + 1
    marker["mini_next"] = iso(now + timedelta(seconds=_GAP_MINI_EVERY_S))
    point = await reader.read_latest(now)
    pre_ts = (((marker.get("pre_reads") or {}).get(main_id) or {}).get("import") or {}).get("ts")
    added = bool(point and (not pre_ts or point["ts"] > pre_ts))
    if added:
        meter = current_block.setdefault("meters", {}).setdefault(
            main_id, {"meta": {}, "channels": {}, "interpolated": False})
        meter["channels"].setdefault("import", {"reads": [], "rates": []})["reads"].append(
            {"value": point["value"], "ts": point["ts"]})
    logger.info("gap fill: asked the Mini for its latest read (try %d of %d) — %s",
                tries + 1, _GAP_MINI_TRIES,
                "using %s" % point["ts"] if added else
                ("none newer than the gap" if point else "none returned"))
    try:
        _store.save_current_block(current_block)        # the tries, and any read
    except Exception:
        pass
    return added


async def _collect_mini_into_block(current_block: dict, now: datetime,
                                   block_minutes: int) -> None:
    """Drive Mini telemetry collection into the current block's main-import
    reads buffer around its boundary. No-op outside the collection window or if
    a local import sensor exists (CAD authoritative)."""
    reader = _kraken_mini_reader
    if reader is None:
        return
    if _has_local_import_sensor():
        return
    if not _mini_collection_window_open(current_block, now, block_minutes):
        return

    try:
        start = datetime.fromisoformat(current_block["start"])
    except (ValueError, KeyError, TypeError):
        return
    boundary_iso = iso(start + timedelta(minutes=block_minutes))

    main_id = _main_meter_id()
    if not main_id:
        return
    meters = current_block.setdefault("meters", {})
    meter_block = meters.setdefault(
        main_id, {"meta": {}, "channels": {}, "interpolated": False})
    channel = meter_block["channels"].setdefault(
        "import", {"reads": [], "rates": []})

    await reader.collect_into(channel["reads"], boundary_iso, now)
    # Persist the in-progress reads so a restart mid-collection isn't lost.
    try:
        _store.save_current_block(current_block)
    except Exception:
        pass

def _kraken_backfill_days() -> int:
    """Compute the first-run backfill window: from the oldest block STILL AWAITING
    settlement to now, capped. Beyond the oldest block, DCC rows have no block to
    attach to.

    Fresh DB (no blocks): return 0 — do NOT backfill. There are no blocks to
    reconcile, so pulling ~400 days (~19k half-hourly rows) is wasted work and
    needless API load. The first poll then starts the cursor at 'now' and only
    tracks forward. A deliberate historical-import action can be added later.

    Measured from the oldest UNSETTLED block, not the oldest block outright. An
    IMPORTED block already carries the supplier's own half-hourly figure — the
    historical import fetched it from this same API — so re-fetching it settles
    nothing. Anchoring on the oldest block meant a fresh install that imports two
    years immediately swept the full 400-day cap: 19,129 rows per poll, every poll,
    of figures already present (measured: 19,129 of 19,129 identical, total delta
    0.000000 kWh). `_UNSETTLED_WHERE` already excludes `imported%`, so the same
    predicate the poll uses to chase lagging settlement now sizes the window — and
    the two stay consistent, since the poll floors that anchor at this horizon."""
    oldest = None
    try:
        oldest = _store.get_oldest_unsettled_block_start()
    except Exception as e:
        # Do NOT fail silently. Returning 0 is the safe direction (never hammer the API
        # on an unknown state), but it is indistinguishable from "nothing to settle" —
        # and it LATCHES: the ingester keeps backfill_days for its lifetime, and the
        # poll floors its own unsettled anchor at `now - backfill_days`, so a horizon of
        # 0 clamps that anchor to now. Settlement then quietly stops reaching back and
        # blocks sit on estimates with no signal. Loud, so the cause is visible.
        logger.warning("_kraken_backfill_days: could not read the oldest unsettled "
                       "block (%s) — backfilling 0 days this run; settlement will not "
                       "reach back until this clears", e)
    if not oldest:
        return 0
    try:
        oldest_dt = datetime.fromisoformat(oldest)
        if oldest_dt.tzinfo is None:
            oldest_dt = oldest_dt.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - oldest_dt).days + 1
        return max(1, min(days, _KRAKEN_BACKFILL_CAP_DAYS))
    except Exception:
        return _KRAKEN_BACKFILL_CAP_DAYS


def _build_kraken_ingester():
    """Construct the KrakenIngester from verified discovery, or return None.

    Requires _kraken_client and a discovery result with an import meter. Export
    is wired only if discovery found an export MPAN. Returns None (logging why)
    if prerequisites are absent, so the poll task can no-op cleanly.
    """
    global _kraken_ingester
    if _kraken_client is None or not _kraken_discovery:
        return None
    imp = _kraken_discovery.get("import")
    if not imp or not imp.get("mpan") or not imp.get("serial"):
        logger.warning("kraken_ingester: no import meter in discovery — not built")
        return None
    try:
        from kraken_ingester import KrakenIngester
    except Exception as e:
        logger.warning("kraken_ingester: import failed: %s", e)
        return None
    exp = _kraken_discovery.get("export") or {}
    _kraken_ingester = KrakenIngester(
        _kraken_client, _store,
        import_mpan=imp["mpan"], import_serial=imp["serial"],
        export_mpan=exp.get("mpan"), export_serial=exp.get("serial"),
        main_meter_id="electricity_main",
        billing_source="api",
        backfill_days=_kraken_backfill_days(),
    )
    return _kraken_ingester


def kraken_available() -> bool:
    """True when a Kraken API client and verified discovery exist — i.e. there
    IS an API to settle/retry against. The UI uses this to gate the unsettled
    'retry settlement' action (no API ⇒ nothing to retry)."""
    return _kraken_client is not None and bool(_kraken_discovery)


def _clamp_sweep_start(oldest_iso: str, now: datetime,
                       max_lookback_days=None) -> datetime:
    """Earliest sweep start = oldest-unsettled block, floored at the horizon.

    Pure. Bounds how far back settlement is chased: an unsettled block older than
    `max_lookback_days` keeps its provisional value (DCC won't settle it now), and
    the floor caps the sweep window cost. None ⇒ unbounded (user-triggered retry).
    """
    odt = datetime.fromisoformat(oldest_iso)
    if odt.tzinfo is None:
        odt = odt.replace(tzinfo=timezone.utc)
    if max_lookback_days is not None:
        floor_dt = now - timedelta(days=max_lookback_days)
        if odt < floor_dt:
            odt = floor_dt
    return odt


def _sweep_is_due(last_iso, now: datetime, min_interval_s: int) -> bool:
    """Pure cadence gate: True if at least `min_interval_s` has elapsed since the
    last sweep (or there was none). Unparseable/missing state ⇒ due."""
    if not last_iso:
        return True
    try:
        last_dt = datetime.fromisoformat(last_iso)
    except (ValueError, TypeError):
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    return (now - last_dt).total_seconds() >= min_interval_s


async def retry_settlement_for_unsettled(now=None, max_lookback_days=None) -> dict:
    """Re-fetch DCC over the span oldest-unsettled → now and settle whatever is now
    available, WITHOUT advancing the incremental cursor. Run by the daily settlement
    sweep (_maybe_run_settlement_sweep); its Data Management "retry" button went with
    the sub-pages (21 Oct 2026).
    Returns {ok, reason?, settled_import, settled_export, unsettled_before,
    unsettled_after, window}.
    """
    if not kraken_available():
        return {"ok": False, "reason": "no_api"}
    ingester = _build_kraken_ingester()
    if ingester is None:
        return {"ok": False, "reason": "no_ingester"}

    before = _store.count_unsettled_blocks()
    if before == 0:
        return {"ok": True, "settled_import": 0, "settled_export": 0,
                "unsettled_before": 0, "unsettled_after": 0, "window": None}

    oldest = _store.get_oldest_unsettled_block_start(include_finalised=True)
    if not oldest:
        return {"ok": True, "settled_import": 0, "settled_export": 0,
                "unsettled_before": before, "unsettled_after": before,
                "window": None}

    _now = now or datetime.now(timezone.utc)
    # Build a Z-suffixed UTC window from oldest unsettled block → now, floored at
    # the look-back horizon when one is given (the daily auto-sweep passes one;
    # the user-triggered retry leaves it None = reach as far back as needed).
    try:
        odt = _clamp_sweep_start(oldest, _now, max_lookback_days)
    except ValueError:
        return {"ok": False, "reason": "bad_oldest"}
    period_from = odt.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"
    period_to = _now.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"

    summary = await ingester.poll(window=(period_from, period_to),
                                  advance_cursor=False)
    await rebuild_devices_after_backfill(summary)
    after = _store.count_unsettled_blocks()
    logger.info("retry_settlement: window=%s..%s settled_import=%d "
                "settled_export=%d unsettled %d→%d",
                period_from, period_to, summary.get("stored", 0),
                summary.get("export_stored", 0), before, after)
    return {"ok": True,
            "settled_import": summary.get("stored", 0),
            "settled_export": summary.get("export_stored", 0),
            "unsettled_before": before, "unsettled_after": after,
            "window": [period_from, period_to],
            "errors": summary.get("errors", [])}


async def probe_recorder_statistics(entity_ids: list, days: int = 800) -> dict:
    """READ-ONLY historical-import spike: fetch HA long-term statistics for the
    chosen sensors and return per-sensor probe reports characterising retention,
    cadence, gaps, state_class/energy-vs-power and DST/timestamp behaviour.

    No writes, no block creation — purely diagnostic. Runs on the engine loop
    (the WS listener that resolves the command futures lives there)."""
    import statistics_probe as _sp
    if _engine_ha is None:
        return {"ok": False, "reason": "no_ha", "reports": []}
    ids = [e for e in (entity_ids or []) if e]
    if not ids:
        return {"ok": True, "reports": [], "window": None}
    days = max(1, min(int(days), 2000))
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days)).isoformat()
    end = now.isoformat()
    try:
        meta_list = await _engine_ha.get_statistic_ids()
    except Exception as e:
        logger.warning("probe_recorder_statistics: list_statistic_ids failed: %s", e)
        meta_list = []
    meta_by_id = {m.get("statistic_id"): m for m in (meta_list or [])}
    # Fetch PER SENSOR (not all at once): over a multi-year window HA generates an
    # hourly bucket per sensor across the whole span, so one big multi-sensor call
    # blows past the WS timeout (blank TimeoutError). Per-sensor keeps each query
    # small, gets its own budget, and isolates a slow/absent sensor from the rest.
    stats = {}
    for eid in ids:
        try:
            one = await _engine_ha.get_statistics([eid], start, end, period="hour",
                                                  timeout=120.0)
            stats[eid] = one.get(eid) or []
        except Exception as e:
            logger.warning("probe_recorder_statistics: %s failed (%s): %s",
                           eid, type(e).__name__, e or "timeout")
            stats[eid] = []
    reports = [_sp.build_sensor_probe(eid, stats.get(eid) or [], meta_by_id.get(eid))
               for eid in ids]
    return {"ok": True, "window": [start, end], "reports": reports}

async def plan_api_import(requested_from=None, *, chunk_days: int = 60) -> dict:
    """READ-ONLY preview for the API import route: compute, per channel, the window
    it would import — from the earliest agreement (~join) up to go-live (EMT's
    oldest block) — and the newest→oldest chunk count. Matches the apply, which
    fetches GraphQL Measurements (reaching the account's full history, not the
    REST ~2-year wall). Writes nothing.

    Returns {ok, go_live, channels:{import:{available,window,chunk_count}, ...}}."""
    import api_import as _ai
    if not kraken_available():
        return {"ok": False, "reason": "no_api", "channels": {}}
    # Ceiling = oldest EXISTING block of ANY source (contiguity rule). A new backward
    # import must stop at the oldest block we already have — including blocks a
    # previous import wrote — so it only fills the remaining gap below them rather
    # than re-fetching a range that's already present. Interior holes are handled
    # separately by the gap-fill flow, so an earlier import can't leave a gap this
    # ceiling would wall off. An EMPTY store has neither concern: the ceiling is now.
    go_live, ceiling_source = _import_ceiling()
    if not go_live:
        return {"ok": False, "reason": "no_store", "channels": {},
                "message": "The data store is not ready yet — try again in a moment."}
    disc = _kraken_discovery or {}
    # The Measurements API is a rolling ~2-year window; older data has aged out and
    # is only reachable via CSV. Cap the advertised floor there so the preview never
    # promises phantom pre-retention chunks (e.g. from an agreement that predates it).
    retention_floor = _retention_floor_iso()
    out: dict = {"ok": True, "go_live": go_live, "channels": {},
                 "ceiling_source": ceiling_source,
                 "retention_floor": retention_floor,
                 "retention_days": _API_RETENTION_DAYS, "retention_capped": False}
    for name in ("import", "export"):
        info = disc.get(name) or {}
        if not info.get("mpan"):
            out["channels"][name] = {"ok": False, "available": False}
            continue
        # Fast, PURE preview — no network. Reuse the data-floor if a prior import
        # or "Check start dates" already resolved it; otherwise fall back to the
        # agreement floor. The import itself runs the (network) boundary probe to
        # extend the floor to the true data start, so the preview must not block
        # or time out on API calls (it has the shortest route timeout).
        floor = (requested_from or _hist_floor_cache.get(name)
                 or _earliest_agreement_from(info))
        if not floor:
            gl_dt = _ai._naive_utc(go_live)
            floor = _ai._iso(gl_dt - timedelta(days=1100)) if gl_dt else None
        if floor and floor < retention_floor:      # older than the API keeps
            floor = retention_floor
            out["retention_capped"] = True
        window = _ai.clamp_window(floor, go_live)
        if not window.get("ok"):
            out["channels"][name] = {"ok": False, "available": True,
                                     "note": window.get("note")}
            continue
        chunks = _ai.plan_chunks(window["from"], window["to"], chunk_days=chunk_days)
        out["channels"][name] = {
            "ok": True, "available": True, "chunk_count": len(chunks),
            "window": {"from": window["from"], "to": window["to"]}}
    return out


_API_IMPORT_CHECKPOINT = "api_import_oldest"   # kraken_state key (per channel suffix)
# The supplier's half-hourly Measurements API is a ROLLING window — it only returns
# roughly the last N days, then drops older data. Older history (e.g. an earlier
# tariff trial) genuinely isn't reachable via the API and must come from the CSV
# route (the bill). Cap the import/plan floor here so we don't advertise or fetch
# phantom chunks below the wall. Approximate; the real wall is where the API starts
# returning empty, which the import also stops at.
_API_RETENTION_DAYS = 730                       # ~2 years


def _retention_floor_iso():
    """Earliest half-hour the rolling ~2-year Measurements window can still return
    (naive-UTC iso). Anything older has aged out of the API — reachable only via the
    CSV route (the bill)."""
    from datetime import datetime, timedelta
    return (datetime.utcnow() - timedelta(days=_API_RETENTION_DAYS)
            ).replace(microsecond=0).isoformat()


def _import_ceiling():
    """Ceiling for a backward API import, as (iso, source).

    Normally EMT's oldest EXISTING block: a backward import tiles UP TO live capture
    and stops, so it never re-fetches a range already present (the contiguity rule).

    On an EMPTY store there is nothing to tile up to and nothing to protect — no live
    capture to stop short of, no existing range to avoid — which is the normal first
    run of an API-only install with no sensors. Refusing there ("no_go_live") made
    Whole history impossible on a fresh install while Date range and Gap fill, which
    tolerate a missing ceiling, both worked. So an empty store imports up to the
    CURRENT half-hour boundary instead; the partial in-progress slot is left alone
    because capture owns it.

    Returns (iso, "blocks") or (iso, "now"); (None, "none") only if the store is gone.
    """
    try:
        go_live = _store.get_oldest_block_start()
    except Exception:
        go_live = None
    if go_live:
        return go_live, "blocks"
    try:
        return _snap_to_slot(_dt_now_iso_safe()), "now"
    except Exception:
        return None, "none"


def _earliest_agreement_from(info: dict):
    """Earliest agreement valid_from (naive-UTC iso) for a channel, or None — the
    import floor (roughly account join)."""
    import api_import as _ai
    froms = [a.get("valid_from") for a in (info.get("agreements") or [])
             if a.get("valid_from")]
    naive = sorted(f for f in (_ai._iso(_ai._naive_utc(x)) for x in froms) if f)
    return naive[0] if naive else None


def history_reach_from(earliest_block):
    """Where Octopus's history starts for the heatmap's "import your history" row (gap fill
    design, decided 18 Oct 2026): the earliest channel floor `plan_api_import` would use (the
    resolved data floor, else the earliest agreement), no older than the API's two years. None
    without the API, without a known floor, or when EMT's oldest block (`earliest_block`, naive
    UTC) already reaches it. Pure — no network; a heatmap load calls it."""
    import api_import as _ai
    if not kraken_available():
        return None
    disc = _kraken_discovery or {}
    floors = [f for f in ((_hist_floor_cache.get(n) or _earliest_agreement_from(disc.get(n) or {}))
                          for n in ("import", "export") if (disc.get(n) or {}).get("mpan")) if f]
    if not floors:
        return None
    reach = max(min(floors), _retention_floor_iso())
    # whole half-hours, as the import's chunks start
    dt = _ai._naive_utc(reach)
    if dt is not None:
        dt = dt.replace(minute=0 if dt.minute < 30 else 30, second=0, microsecond=0)
        if dt.isoformat() < reach:
            dt += timedelta(minutes=30)
        reach = dt.isoformat()
    if earliest_block and reach >= str(earliest_block)[:19]:
        return None
    return reach

_hist_floor_cache: dict = {}


async def _data_floor_for(channel: str, info: dict):
    """The import floor for a channel: the earliest half-hour the Measurements API
    actually returns — which can be EARLIER than the earliest supplier *agreement*
    (e.g. solar export that ran before a formal export agreement was signed).

    The agreement floor (`_earliest_agreement_from`) was clamping the import there,
    silently dropping real earlier history. Because Measurements carries the billed
    cost per interval, pre-agreement half-hours still price correctly (cost from the
    API; rate falls back to cost÷kWh where no tariff schedule covers them).

    Cheap presence-probe: 30-day windows stepping ~6 months back from the agreement
    floor, one small page each (we only need to know IF data exists). A single empty
    window is tolerated (a mid-history outage); two in a row ends the scan. Returns
    the earlier of the agreement floor and the oldest window with data. Never floors
    HIGHER than the agreement (worst case = prior behaviour); agreement floor on any
    error."""
    import api_import as _ai
    agree = _earliest_agreement_from(info)
    mpan = info.get("mpan")
    if not mpan or _kraken_client is None:
        return agree
    anchor = _ai._naive_utc(agree) if agree else None
    if anchor is None:
        return agree
    direction = "GENERATION" if channel == "export" else "CONSUMPTION"
    step, win = 183, 30
    oldest_data = None
    empties = 0
    for i in range(1, 15):        # up to ~7 years of 6-month steps
        ps = anchor - timedelta(days=step * i)
        pe = ps + timedelta(days=win)
        try:
            rows = await _kraken_client.get_measurements(
                mpan, _ai._iso(ps) + "Z", _ai._iso(pe) + "Z",
                account_number=_kraken_account_number, direction=direction,
                page_size=100, max_pages=1)
        except Exception:
            break
        if rows:
            hits = [r["start"] for r in rows if r.get("start")]
            if hits:
                oldest_data = min(hits) if oldest_data is None else min(oldest_data, min(hits))
            empties = 0
        else:
            empties += 1
            if empties >= 2:
                break
    if oldest_data and (agree is None or oldest_data < agree):
        logger.info("_data_floor_for: %s floor extended %s → %s "
                    "(data predates agreement)", channel, agree, oldest_data)
        return oldest_data
    return agree


async def _cached_data_floor(channel: str, info: dict, *, restart: bool = False):
    """`_data_floor_for` memoised per run (cleared on restart, like rate segs)."""
    if restart:
        _hist_floor_cache.pop(channel, None)
    if channel not in _hist_floor_cache:
        _hist_floor_cache[channel] = await _data_floor_for(channel, info)
    return _hist_floor_cache[channel]


def _keep_tariff_prices(tariff_code, valid_from_raw, kind, sched, valid_to_raw=None) -> None:
    """G2 (tariff prices design §4): an agreement's schedule as Octopus published it, summarised to
    price periods (tariff_prices.summarise) and stored for the tariff overlay — only when it differs
    from what is stored. Ex-VAT, from the schedule's published ex-VAT series; a schedule without one
    is not kept. Display only: nothing that prices reads it. A log line gives each band's periods."""
    import tariff_prices as _tp
    from kraken_ingester import normalise_to_naive_utc
    exc = getattr(sched, "exc", None)
    src = getattr(sched, "raw", None) or sched          # with_vat() keeps the published one in .raw
    exc = getattr(src, "exc", None) or exc
    if exc is None or exc.is_empty():
        logger.info("tariff prices: %s %s — no ex-VAT series, not kept", tariff_code, kind)
        return
    try:
        ag_from = normalise_to_naive_utc(valid_from_raw) if valid_from_raw else "0000-01-01T00:00:00"
        ag_to = normalise_to_naive_utc(valid_to_raw) if valid_to_raw else None
    except ValueError:
        return
    from kraken_rates import _clip_periods
    pk = "standing" if kind == "standing" else "unit"
    # the agreement's own span: the refresh's current-tariff fetch is the tariff's whole history
    rows = _tp.summarise(_clip_periods(exc._periods, ag_from, ag_to), kind=pk,
                         tariff_code=tariff_code, tz_name=_iog_site_tz())
    if _store is not None and _store.replace_tariff_prices(
            tariff_code, ag_from, pk, rows, datetime.now(timezone.utc).replace(tzinfo=None).isoformat()):
        logger.info("tariff prices: %s from %s, %s — %s", tariff_code, ag_from[:10], pk,
                    ", ".join("%s %d" % kv for kv in sorted(_tp.counts(rows).items())))


def _keep_current_tariff_prices(channel, tariff_code, kind, sched) -> None:
    """The refresh's current-tariff schedule kept as the running agreement's prices (G2 §4): with a
    single agreement the stitch is skipped, and export's and the standing charge's agreements are
    fetched only here. The agreement is the stored one live now with this tariff."""
    try:
        from kraken_api_client import KrakenAPIClient
        ags = [a for a in (_agreement_info(channel).get("agreements") or [])
               if a.get("tariff_code") == tariff_code]
        cur = KrakenAPIClient._current_agreement(ags) if ags else None
        if cur:
            _keep_tariff_prices(tariff_code, cur.get("valid_from"), kind, sched, cur.get("valid_to"))
    except Exception as e:
        logger.warning("tariff prices: %s %s current not kept (%s)", channel, kind, e)


# G2b (tariff prices design §4): the closed agreements' prices the refresh does not fetch — import's
# standing charges and export's rates (and import's rates where the stitch did not keep them).
_CLOSED_PRICES_PER_CYCLE = 4          # agreement-kinds fetched per poll cycle (each one request, or a few pages)
_CLOSED_PRICES_PACE_S = 2.0           # seconds between them (test-overridable)
_CLOSED_PRICES_RETRY_S = 24 * 3600    # a failed or empty one is tried again after a day…
_CLOSED_PRICES_TRIES = 3              # …and not after the third
_CLOSED_PRICES_KEY = "tariff_prices_tried"


def _closed_price_wants() -> list:
    """[(channel, kind, agreement)] of the closed agreements whose prices are not stored: import's
    'rate' and 'standing', export's 'rate'. A degenerate window prices nothing and is left out."""
    from kraken_rates import _agreement_ended
    from kraken_ingester import normalise_to_naive_utc
    out = []
    for channel, kinds in (("import", ("standing", "rate")), ("export", ("rate",))):
        for ag in _agreement_info(channel).get("agreements") or []:
            tc, vf, vt = ag.get("tariff_code"), ag.get("valid_from"), ag.get("valid_to")
            if not tc or not vf or not vt:
                continue
            try:
                vf_n, vt_n = normalise_to_naive_utc(vf), normalise_to_naive_utc(vt)
            except ValueError:
                continue
            if vf_n >= vt_n or not _agreement_ended(vt_n):
                continue
            for kind in kinds:
                if not _store.has_tariff_prices(tc, vf_n, "standing" if kind == "standing" else "unit"):
                    out.append((channel, kind, ag))
    return out


async def _fetch_closed_agreement_prices() -> int:
    """G2b: fetch, a few a cycle, the closed agreements' prices not stored yet (§4). A closed
    agreement's prices cannot change, so each is fetched once ever: once stored, never again. A fetch
    that fails or comes back empty is tried again after a day, three times at most — the count kept in
    kraken_state, so a restart does not reset it. A half-hourly (Agile) agreement's unit rates are not
    fetched: its `dynamic` row is written from the agreement's window. Each request is logged.
    Returns the agreement-kinds fetched."""
    import json
    import kraken_rates as _kr
    import tariff_prices as _tp
    from kraken_api_client import KrakenAPIClient
    from kraken_ingester import normalise_to_naive_utc
    if _kraken_client is None or _store is None:
        return 0
    try:
        tried = json.loads(_store.get_kraken_state(_CLOSED_PRICES_KEY) or "{}")
    except Exception:
        tried = {}
    now = time.time()
    done = 0
    for channel, kind, ag in _closed_price_wants():
        tc, vf, vt = ag["tariff_code"], ag["valid_from"], ag.get("valid_to")
        key = "%s|%s|%s" % (tc, normalise_to_naive_utc(vf), kind)
        t = tried.get(key) or {}
        if t.get("n", 0) >= _CLOSED_PRICES_TRIES or now - t.get("at", 0) < _CLOSED_PRICES_RETRY_S:
            continue
        if kind == "rate" and _tp.is_dynamic_code(tc):
            vf_n = normalise_to_naive_utc(vf)
            _store.replace_tariff_prices(tc, vf_n, "unit", [
                {"band": "dynamic", "window": None, "value_exc": None, "valid_from": vf_n,
                 "valid_to": normalise_to_naive_utc(vt)}], datetime.now(timezone.utc).replace(tzinfo=None).isoformat())
            continue                                      # no call: Agile's rates are not kept
        if done >= _CLOSED_PRICES_PER_CYCLE:
            break
        if done:
            await asyncio.sleep(_CLOSED_PRICES_PACE_S)
        logger.info("tariff prices: fetching %s %s of %s, %s – %s (G2b)", channel, kind, tc,
                    str(vf)[:10], str(vt)[:10])
        # kept from what comes back, not on_built: a schedule the process has cached (the API import
        # fetched it) is returned without being built again
        segs = await _kr.agreement_schedules(_kraken_client, [ag], kind=kind,
                                             product_of=KrakenAPIClient._tariff_to_product_code)
        for _vf, _vt, sched in segs:
            _keep_tariff_prices(tc, vf, kind, sched, vt)
        done += 1
        if not segs or not _store.has_tariff_prices(tc, normalise_to_naive_utc(vf),
                                                    "standing" if kind == "standing" else "unit"):
            tried[key] = {"n": t.get("n", 0) + 1, "at": now}
            logger.warning("tariff prices: %s %s of %s — nothing kept (try %d of %d)", channel, kind,
                           tc, tried[key]["n"], _CLOSED_PRICES_TRIES)
        else:
            tried.pop(key, None)
    if tried:
        _store.set_kraken_state(_CLOSED_PRICES_KEY, json.dumps(tried, sort_keys=True))
    elif _store.get_kraken_state(_CLOSED_PRICES_KEY):
        _store.set_kraken_state(_CLOSED_PRICES_KEY, "")
    return done


async def _agreement_segs(channel: str, kind: str) -> list:
    """[(valid_from, valid_to, RateSchedule)] for each of `channel`'s agreements — through
    kraken_rates.agreement_schedules, the one fetch the refresh's import stitch shares (engine step
    5c, 27 Oct 2026: until then the import fetched every agreement again for itself, and again after
    a restart). VAT as the channel's (`_vat_context`); an open start reads 0000-01-01."""
    from kraken_api_client import KrakenAPIClient
    from kraken_rates import agreement_schedules
    info = _agreement_info(channel)          # the stored agreements: a refused answer is not priced
    segs = await agreement_schedules(_kraken_client, info.get("agreements") or [], kind=kind,
                                     product_of=KrakenAPIClient._tariff_to_product_code,
                                     on_built=_keep_tariff_prices)
    vat = _vat_context(channel)
    return [(vf or "0000-01-01T00:00:00", vt, sched.with_vat(vat)) for vf, vt, sched in segs]


async def _build_channel_rate_segs(channel: str):
    """Per-agreement rate schedules for a channel, from the discovered agreement
    history. Used to give imported blocks a CLEAN tariff rate (and to price the
    half-hours where Measurements returned kWh but no cost)."""
    return await _agreement_segs(channel, "rate")


async def _build_channel_standing_segs(channel: str):
    """Per-agreement STANDING-CHARGE schedules — the authoritative, complete source for the daily
    standing charge. Measurements' STANDING_CHARGE_COST is patchy (whole stretches come back with
    none), so imported days are priced from this schedule and only fall back to the Measurements
    figure where the schedule doesn't cover the date. Import only: a standing charge is the import
    tariff's (engine step 5c — the import asked for export's too)."""
    if channel != "import":
        return []
    return await _agreement_segs(channel, "standing")


def _local_date_of_utc(utc_iso: str, tz_name: str) -> str:
    """The local date of a naive-UTC time (block_store.utc_to_local_date; UTC on any error)."""
    try:
        from zoneinfo import ZoneInfo
        return (datetime.fromisoformat(str(utc_iso)[:19]).replace(tzinfo=timezone.utc)
                .astimezone(ZoneInfo(tz_name)).date().isoformat())
    except Exception:
        return str(utc_iso)[:10]


def _standing_for(standing_segs, day_iso):
    """Daily standing charge £/day for a local date (YYYY-MM-DD) from the schedule, or
    None if uncovered. Resolve at midday to avoid midnight/DST boundary edges (noon UTC is
    inside the UK local day either side of the clocks changing).
    Native pence/day → £/day."""
    ts = day_iso + "T12:00:00"
    for vf, vt, sched in standing_segs:
        if ts >= vf and (vt is None or ts < vt):
            v = sched.resolve(ts)
            if v is None:
                # A standing charge persists until it next changes. The API can
                # return records with an explicit valid_to and no open successor,
                # so resolve() stops carrying the rate forward past that date and
                # returns None — dropping us to the patchier Measurements figure
                # (the 0.5040 vs 0.504559 boundary mismatch). Carry the LAST known
                # rate at/before this date forward instead.
                periods = getattr(sched, "_periods", None) or []
                earlier = [r for (pf, _pt, r) in periods if pf <= ts]
                v = earlier[-1] if earlier else None
            if v is not None:
                return round(v / 100.0, 6)
            # Segment matched but its schedule has nothing at/before this date —
            # keep looking (overlapping agreements) before giving up.
    return None


def _tariff_rate_for(rate_segs, start, off_peak):
    """Clean £/kWh for a block from the tariff schedule, keyed by its bucket:
    OFF_PEAK→day's low rate, STANDARD→day's high rate, unknown→time-of-day
    resolve. None if uncovered. Native pence → £."""
    for vf, vt, sched in rate_segs:
        if start >= vf and (vt is None or start < vt):
            if off_peak is True:
                lo, _hi = sched.day_rate_bounds(start)
                v = lo
            elif off_peak is False:
                _lo, hi = sched.day_rate_bounds(start)
                v = hi
            else:
                v = sched.resolve(start)
            # Uncovered sliver inside a matched agreement window — the tariff-
            # transition seam where a new tariff's published unit rates start
            # AFTER its valid_from. For a FLAT tariff the rate is the same
            # everywhere, so back-fill it rather than returning None (which drops
            # pricing to a fragmenting cost÷kWh). flat_rate() is None for a banded
            # tariff, so IOG import keeps its exact per-slot rate untouched.
            if v is None:
                v = sched.flat_rate()
            return round(v / 100.0, 6) if v is not None else None
    return None


def _exc_segs(segs):
    """BL-23 (4.2): the parallel EX-VAT segments — swap each segment's RateSchedule for
    its `.exc` sibling. Reused with _tariff_rate_for / _standing_for so exc pricing uses
    the exact same band/coverage logic as inc. Empty when no segment carries exc."""
    return [(vf, vt, s.exc) for (vf, vt, s) in (segs or [])
            if getattr(s, "exc", None) is not None]



@job("iog_split_carve")
async def run_iog_split_carve(max_blocks: int = 2000, starts=None) -> dict:
    """Settle every half-hour whose completed dispatch has arrived or changed since its split was
    written (`get_blocks_off_dispatch_split`: no EV split yet, or one more than 0.0005 kWh off the
    dispatch clipped to its kWh) — engine step 4c, EMT-v5_engine_step4_settle_design.md §3. The
    dispatch is the fact; settle_half_hour decides the band, the legs, the cost and the devices from
    the facts and writes them together, only where they differ.

    Until 26 Oct 2026 this carved the EV from the dispatch and re-split the block at its OWN single
    rate, additively (imp_cost untouched), and only for a block with no split — so a dispatch Octopus
    restated after the carve stayed on its first figure (staging, 24 Oct 2026 04:00: 2.67 kWh carved,
    2.55 restated). A settled block on an IOG-SMB agreement whose bill states its split is the bill's,
    and left out.

    `starts`: only those block starts. The dispatch capture passes the slots it has just seen
    completed, so a split follows Octopus's completed record within the 5-minute capture rather
    than waiting for the hourly pass (13 Oct 2026: 05:30 BST's charge was recorded at 06:42 and
    still drawn as house at 06:47)."""
    if _store is None:
        return {"ok": False, "reason": "no store"}
    isched = _kraken_rate_schedules.get("import")
    if isched is None or isched.is_empty():
        return {"ok": True, "deferred": "schedule not ready"}
    rows = _store.get_blocks_off_dispatch_split(max_blocks, starts=starts)
    carved = 0
    for r in rows:
        bs = r["block_start"]
        if float(r["imp_kwh"] or 0.0) <= 1e-9:
            continue
        # the final stage is the settle's: a settled block on an IOG-SMB agreement whose bill
        # stated its split carries that split, not the dispatch's (design §6, the tower)
        if r["rate_source"] == "settled" and r["bill_ev"] is not None and split_at(bs):
            continue
        # Engine step 4c (EMT-v5_engine_step4_settle_design.md §3): the completed dispatch is a fact
        # that arrived or changed — settle the half-hour. settle() takes the split from it (clipped
        # to the kWh), prices each leg at its band's rate, and writes the main, segments and devices
        # together; until then the carve re-split the block at its own single rate and the devices
        # were re-fitted after (v4 4.5.21), in writes of their own.
        try:
            if settle_half_hour(_store, bs)["written"]:
                carved += 1
        except Exception as e:
            logger.warning("run_iog_split_carve: settle failed (%s): %s", bs, e)
    if carved:
        logger.info("run_iog_split_carve: settled %d half-hour(s) on a completed dispatch "
                    "that arrived or changed", carved)
    return {"ok": True, "carved": carved}


_SETTLE_HISTORY_MARKER = "settle_history_state"    # store_meta key (engine step 4d)
# raise to settle every half-hour again (settle()'s rules changed) — 2: the device grid split
# proportional after the EV (BL-76, 29 Oct 2026)
_SETTLE_HISTORY_VERSION = 2
_SETTLE_HISTORY_DAY = timedelta(days=1)
_SETTLE_HISTORY_PACE_S = 0.02
_SETTLE_HISTORY_PASS_PACE = 1.0
_settle_history_running = False
_settle_history_scheduled = False   # startup's task is holding or walking
_settle_history_rearm = False     # an import or a gap fill asked for the whole history again


def settle_range(store, utc_start: str, utc_end: str) -> dict:
    """settle() over every main-meter half-hour in [utc_start, utc_end), each written only where it
    differs from what it holds (`_settle_write`), from one get_settle_inputs read. Returns
    {read, written, statuses: {status: n}, parts: {part: n}}."""
    inp = store.get_settle_inputs(utc_start, utc_end)
    try:
        cal = store.get_vat_calendar()
    except Exception:
        cal = None
    out = {"read": 0, "written": 0, "statuses": {}, "parts": {}}
    for m in inp["mains"]:
        bs = m["block_start"]
        segs = inp["segments"].get(bs, [])
        r = _settle_write(store, m, inp["subs"].get(bs, []), segs, cal)
        if r["status"] in ("no_rate", "no_band") and _settle_fallback(store, m, segs):
            r = dict(r, written=True, parts=["fallback"])
        out["read"] += 1
        out["statuses"][r["status"]] = out["statuses"].get(r["status"], 0) + 1
        if r["written"]:
            out["written"] += 1
            for p in r["parts"]:
                out["parts"][p] = out["parts"].get(p, 0) + 1
    return out


def _settle_fallback(store, m: dict, segs: list) -> bool:
    """What settle() declines (no rate, no band) keeps the price it holds — the one small fallback
    (engine step 4e, decided 27 Oct 2026). Where such a half-hour has no segments, they are written
    from it — one house segment at its own rate — and an imported or kWh-settled one with no ex-VAT
    gets it from the VAT calendar: what the history reprice sweep did for every half-hour until 4e.
    A gap (no cost) is left unsegmented, as before. Returns whether anything was written."""
    import pricing_segments as _ps
    bs = m["block_start"]
    kwh, cost = float(m.get("imp_kwh") or 0.0), m.get("imp_cost")
    if kwh <= 1e-9 or cost is None:
        return False
    try:
        vat = float(store.vat_rate_at(bs) or 0.0)
    except Exception:
        vat = 0.0
    wrote = False
    exc = m.get("imp_cost_exc")
    if exc is None and (str(m.get("source") or "").startswith("imported")
                        or m.get("imp_kwh_api") is not None):
        exc = round(float(cost) / (1.0 + vat), 6)
        wrote = bool(store.set_blocks_exc([(bs, "electricity_main", exc)])) or wrote
    if not segs:
        ratio = (float(exc) / float(cost)) if (exc is not None and cost) else 1.0 / (1.0 + vat)
        store.set_block_segments(bs, "electricity_main", _ps.segments_from_legacy(
            imp_kwh=kwh, imp_cost=float(cost), imp_rate=float(m.get("imp_rate") or 0.0),
            exc_ratio=ratio))
        wrote = True
    return wrote


def _settle_history_done() -> bool:
    """The settle sweep has walked the history at this _SETTLE_HISTORY_VERSION."""
    m = (_store.get_meta(_SETTLE_HISTORY_MARKER, {}) or {}) if _store is not None else {}
    return bool(m.get("done")) and int(m.get("version") or 0) >= _SETTLE_HISTORY_VERSION


async def _settle_history_to_done(*, force: bool = False, wait_for_schedule: bool = False,
                                  max_passes: int = 500) -> None:
    """The settle sweep's passes until it is done — `force` restarts it from the history's start
    (after an import or a gap fill). A pass that stands aside resumes at its cursor. With no import
    schedule yet it stops, unless `wait_for_schedule` (startup, where the schedule is built after
    the sweep is scheduled)."""
    global _settle_history_running, _settle_history_rearm
    if force:
        _settle_history_rearm = True
    if _settle_history_running:
        return                              # the running sweep restarts at its next day
    _settle_history_running = True
    try:
        for _ in range(max_passes):
            if await _run_settle_history_sweep() < 0 and not wait_for_schedule:
                return                      # no schedule yet: the next startup or import retries
            if _store is None or (_settle_history_done() and not _settle_history_rearm):
                return
            await asyncio.sleep(_SETTLE_HISTORY_PASS_PACE)
    finally:
        _settle_history_running = False


def settle_history_in_progress() -> bool:
    """The settle sweep is walking the history, or has history still to walk with a schedule to
    price it from (without one — a rate-sensor account — it never runs, and is not in progress).
    The daily share copy waits for it, and the banner says so."""
    if _store is None:
        return False
    if _settle_history_running:
        return True
    _isched = _kraken_rate_schedules.get("import")
    if _isched is None or _isched.is_empty():
        return False
    m = _reader().get_meta(_SETTLE_HISTORY_MARKER, {}) or {}        # a request thread asks too
    if m.get("done") and int(m.get("version") or 0) >= _SETTLE_HISTORY_VERSION:
        return False
    # No history, nothing to walk: a new install connected to Octopus before its meter was set up
    # read "Finishing your upgrade … (null days left)" — the sweep that would mark it done runs only
    # with a meter (3 Nov 2026).
    first, _last = _reader().block_span("electricity_main")
    return bool(first)


def _settle_history_restart(state: dict) -> dict:
    """A walk from the history's start: the rearm taken, the state cleared — and the re-settle spans a
    change of agreements queued before it, which the walk covers (staging, 28 Oct 2026: a restore
    queued the whole history as the first answer stored, the sweep then walked it, and the queue
    would have walked it again). Spans queued during the walk stay: its cursor may have passed them."""
    global _settle_history_rearm
    _settle_history_rearm = False
    try:
        if _store is not None and _store.get_meta(_SETTLE_SPANS_KEY, []):
            _store.set_meta(_SETTLE_SPANS_KEY, [])
            logger.info("settle spans: cleared — the walk from the history's start covers them")
    except Exception as e:
        logger.debug("settle spans: not cleared (%s)", e)
    return {}


@job("settle_sweep", depends_on=("import",), exclusive_with=("delete",))
async def _run_settle_history_sweep(*, force: bool = False, max_days: int = 100000) -> int:
    """settle() over the whole history, a day at a time (engine step 4d): what was priced before
    settle() decided it — finalise's live label, history imported before 4a-2, the importer's
    bill-split skip — becomes settle()'s, written only where it differs. Once per
    _SETTLE_HISTORY_VERSION; `force` (after an import or a gap fill) walks it again from the start.
    One transaction a day, the loop yielded between; stands aside for an import, a delete or a
    restore, and resumes from its cursor. Returns the half-hours written this pass; -1 where there
    is no import schedule to price from yet."""
    import asyncio as _aio
    if _store is None or delete_in_progress():
        return 0
    state = _store.get_meta(_SETTLE_HISTORY_MARKER, {}) or {}
    if force or _settle_history_rearm or int(state.get("version") or 0) < _SETTLE_HISTORY_VERSION:
        state = _settle_history_restart(state)
    if state.get("done"):
        # the history walked: what a change of agreements queued (G1 design §4)
        return await _settle_spans_pass(max_days) if _settle_spans_pending() else 0
    _isched = _kraken_rate_schedules.get("import")
    if _isched is None or _isched.is_empty():
        # settle() prices from the schedule; without one (populated after startup, or a
        # rate-sensor account) it would price nothing and mark the history done
        logger.info("settle sweep: import schedule not ready — deferring")
        return -1
    first, last = _store.block_span("electricity_main")
    if not first:
        _store.set_meta(_SETTLE_HISTORY_MARKER, {"version": _SETTLE_HISTORY_VERSION, "done": True})
        return 0
    try:
        day = datetime.fromisoformat(state.get("cursor") or first[:10] + "T00:00:00")
        stop = datetime.fromisoformat(last) + timedelta(minutes=30)
    except ValueError:
        return 0
    tally = {k: state.get(k) or (0 if k in ("read", "written") else {})
             for k in ("read", "written", "parts")}
    written = 0
    for _ in range(max_days):
        if _settle_history_rearm:
            # an import or a gap fill asked for the whole history again while this pass walked it
            _settle_history_restart(state)
            first, last = _store.block_span("electricity_main")
            if not first:
                break
            day = datetime.fromisoformat(first[:10] + "T00:00:00")
            stop = datetime.fromisoformat(last) + timedelta(minutes=30)
            tally = {"read": 0, "written": 0, "parts": {}}
        if day >= stop:
            break
        if _store is None or _coord.admission("settle_sweep")[0] != ADMIT:
            logger.info("settle sweep: standing aside at %s (resumes next pass)", day.isoformat())
            return written
        nxt = day + _SETTLE_HISTORY_DAY
        with _store.batch():
            try:
                r = settle_range(_store, day.isoformat(), nxt.isoformat())
            except Exception as e:
                logger.warning("settle sweep: %s failed (skipped): %s", day.date(), e)
                r = {"read": 0, "written": 0, "parts": {}}
            written += r["written"]
            tally["read"] += r["read"]
            tally["written"] += r["written"]
            for p, n in r["parts"].items():
                tally["parts"][p] = tally["parts"].get(p, 0) + n
            _store.set_meta(_SETTLE_HISTORY_MARKER, dict(tally, version=_SETTLE_HISTORY_VERSION,
                                                         cursor=nxt.isoformat()))
        day = nxt
        await _aio.sleep(_SETTLE_HISTORY_PACE_S)
    if _store is None or day < stop:
        return written
    _store.set_meta(_SETTLE_HISTORY_MARKER, dict(
        tally, version=_SETTLE_HISTORY_VERSION, done=True,
        completed_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()))
    logger.info("settle sweep: complete — %d half-hour(s) read, %d written (%s)",
                tally["read"], tally["written"],
                ", ".join("%s %d" % kv for kv in sorted(tally["parts"].items())) or "nothing differed")
    return written


# The sweep waits for startup's own writes before its first pass (ROADMAP §A.1, 5 Oct 2026).
# Startup schedules it from inside itself, so it began while startup was still writing, the
# first Kraken poll (with its BL-8 outage backfill) and the carbon backfill alongside it —
# soak 17's worst lag, 11.2 s, was that overlap: a 7.7 s backfill commit and a 3.2 s
# settlement commit beside the sweep's own. Held until startup has finished, no poll is
# running, and _SWEEP_STARTUP_HOLD_S has passed since — never longer than the cap.
_SWEEP_STARTUP_HOLD_S = 60.0
_SWEEP_STARTUP_HOLD_MAX_S = 300.0
_SWEEP_STARTUP_HOLD_POLL_S = 1.0


async def _hold_sweep_after_startup() -> None:
    import asyncio as _aio
    t0 = time.monotonic()
    held = False
    while time.monotonic() - t0 < _SWEEP_STARTUP_HOLD_MAX_S:
        if (not _startup_lock.locked() and not poll_in_progress()
                and seconds_since_startup() >= _SWEEP_STARTUP_HOLD_S):
            if held:
                logger.info("settle sweep: startup's writes have settled — starting "
                            "after %.0f s", time.monotonic() - t0)
            return
        if not held:
            logger.info("settle sweep: holding until startup's own writes settle")
            held = True
        await _aio.sleep(_SWEEP_STARTUP_HOLD_POLL_S)
    logger.info("settle sweep: held %.0f s — starting anyway",
                _SWEEP_STARTUP_HOLD_MAX_S)


def _maybe_settle_history() -> None:
    """Schedule startup's settle sweep as a loop task, after startup's own writes (engine step 4e:
    it replaced the history reprice sweep, which ran first). No-op once done, without Kraken, or
    while an import, a delete or a restore holds the history."""
    global _settle_history_scheduled
    try:
        if _store is None or _settle_history_scheduled or not kraken_available():
            return
        _decision, _blocker = _coord.admission("settle_sweep")
        if _decision != ADMIT:
            logger.info("_maybe_settle_history: %s active — deferring", _blocker)
            return
        if _settle_history_done() and not _settle_spans_pending():
            return
        import asyncio as _aio
        try:
            loop = _aio.get_running_loop()
        except RuntimeError:
            return

        async def _task():
            global _settle_history_scheduled
            try:
                await _hold_sweep_after_startup()
                # #57: a restore closes the engine store and its own startup reschedules
                if _store is not None:
                    await _settle_history_to_done(wait_for_schedule=True)
            except Exception as e:
                logger.warning("_maybe_settle_history: worker failed: %s", e)
            finally:
                _settle_history_scheduled = False

        _settle_history_scheduled = True
        logger.info("settle sweep: scheduling the pass over the history")
        loop.create_task(_task())
    except Exception as e:
        logger.warning("_maybe_settle_history: schedule failed: %s", e)
        _settle_history_scheduled = False


def _import_label(off_peak) -> str:
    """A measurement's band as the bill cache labels it (parse_bill's: a cost node with no band
    is "mixed", which settles nothing)."""
    return "OFF_PEAK" if off_peak is True else ("STANDARD_RATE" if off_peak is False else "mixed")

# {channel: (agreements list, [(tariff_code, from, to)], len)} — each agreement's window parsed once
# per discovery. The list is held, so its id cannot be reused by another while it is cached.
_AGREEMENT_WINDOWS: dict = {}


def _agreement_info(channel: str) -> dict:
    """`channel`'s meter point as the agreement readers see it (G1 design §3): the stored agreements
    — every answer discovery takes is stored first, so they are its answer — else discovery's, where
    none is stored yet; {} with neither. An answer the guard did not take (`_unexpected_drops`)
    leaves the last good copy read. (An account mismatch leaves discovery `{}`: the stored are this
    database's own.)"""
    src = _stored_agreements or _kraken_discovery
    return (src or {}).get(channel) or {}


def _agreement_rows(disc) -> list:
    """Discovery's agreements as `tariff_agreements` rows, naive-UTC ISO (unparseable dates skipped)."""
    import api_import as _ai
    out = []
    for ch in ("import", "export"):
        info = (disc or {}).get(ch) or {}
        for a in info.get("agreements") or []:
            try:
                vf = _ai._naive_utc(a.get("valid_from"))
                vt = _ai._naive_utc(a.get("valid_to")) if a.get("valid_to") else None
            except Exception:
                continue
            if not (info.get("mpan") and a.get("tariff_code") and vf):
                continue
            out.append({"mpan": info["mpan"], "direction": ch, "tariff_code": a["tariff_code"],
                        "product_code": a.get("product_code"), "valid_from": _ai._iso(vf),
                        "valid_to": _ai._iso(vt) if vt else None})
    return out


def _keep_agreements(disc) -> "dict | None":
    """Write an answer discovery took into `tariff_agreements` (G1 design §2) and reload the stored
    copy. The caller has taken it — the same meter points, not a failure. An answer with no import
    agreement writes nothing. The diff ({added, superseded, kept}), or None. Never raises."""
    acct = _kraken_account_number or get_db_account()
    rows = _agreement_rows(disc)
    if _store is None or not acct or not any(r["direction"] == "import" for r in rows):
        return None
    try:
        odd = _unexpected_drops(_reader().get_tariff_agreements(acct), rows, datetime.utcnow())
    except Exception:
        odd = []
    if odd:
        # not a change a customer can make: an API change, more likely (G1 design §2, the user,
        # 28 Oct 2026: "if there's any api/graphql deprecations we don't get in a mess")
        logger.warning("tariff agreements: Octopus's answer drops or moves %d agreement(s) that "
                       "started and ended over %d days ago (%s) — not taken, the last good copy is "
                       "kept; an API change is the likely cause", len(odd), _AGREEMENT_CHANGE_DAYS,
                       "; ".join("%s %s %s..%s" % (d, c, vf, vt or "open") for d, c, vf, vt in odd[:5]))
        _remember_refused(acct, rows, odd)
        return None
    return _store_agreements(acct, rows)


def _store_agreements(acct: str, rows: list) -> "dict | None":
    """Write an answer into `tariff_agreements` (G1 design §2) — discovery's, taken, or one the
    user accepted — reload the stored copy, check the labels, queue the re-settle of what it moved,
    and forget a refused answer. The diff, or None. Never raises."""
    try:
        diff = _store.replace_tariff_agreements(acct, rows, datetime.utcnow().replace(microsecond=0).isoformat())
    except Exception as e:
        logger.warning("tariff agreements: not stored (%s)", e)
        return None
    if diff["added"] or diff["superseded"]:
        logger.info("tariff agreements: stored — %d added, %d superseded, %d kept",
                    len(diff["added"]), len(diff["superseded"]), len(diff["kept"]))
    _load_stored_agreements()
    _log_label_mismatch()
    _forget_refused()
    if diff["added"] or diff["superseded"]:
        try:
            first, last = _store.block_span("electricity_main")
            end = ((datetime.fromisoformat(last) + timedelta(minutes=30)).isoformat() if last else None)
            _queue_settle_spans(_agreement_change_spans(diff, first, end))
        except Exception as e:
            logger.warning("tariff agreements: the re-settle was not queued (%s)", e)
    return diff


# An answer the guard did not take (G1 design §2), kept for the user to accept from the page: a
# real correction of an old agreement and a lasting API change look the same to code.
_REFUSED_AGREEMENTS_KEY = "agreements_refused"


def _remember_refused(acct: str, rows: list, odd: list) -> None:
    try:
        _store.set_meta(_REFUSED_AGREEMENTS_KEY, {
            "at": datetime.utcnow().replace(microsecond=0).isoformat(), "account": acct,
            "rows": rows, "odd": [list(x) for x in odd]})
    except Exception as e:
        logger.debug("tariff agreements: the refused answer was not kept (%s)", e)


def _forget_refused() -> None:
    try:
        if _store.get_meta(_REFUSED_AGREEMENTS_KEY):
            _store.set_meta(_REFUSED_AGREEMENTS_KEY, None)
    except Exception:
        pass


def refused_agreements() -> "dict | None":
    """The answer the guard did not take, for the page (G1 design §2): {at, odd, old, new} —
    each [(direction, tariff_code, valid_from, valid_to)], no MPAN — or None."""
    try:
        m = _reader().get_meta(_REFUSED_AGREEMENTS_KEY)
        if not m or m.get("account") != get_db_account():
            return None
        row = lambda r: [r["direction"], r["tariff_code"], r["valid_from"], r.get("valid_to")]
        return {"at": m["at"], "odd": m["odd"],
                "old": [row(r) for r in _reader().get_tariff_agreements(m["account"])],
                "new": [row(r) for r in m["rows"]]}
    except Exception:
        return None


def accept_refused_agreements() -> dict:
    """The user accepts Octopus's answer the guard did not take: stored as any answer is, its moved
    history re-settled. Engine thread. {ok, added, superseded} or {ok: False, error}."""
    m = _store.get_meta(_REFUSED_AGREEMENTS_KEY) if _store is not None else None
    if not m or m.get("account") != get_db_account():
        return {"ok": False, "error": "no changed agreements are waiting"}
    diff = _store_agreements(m["account"], m["rows"])
    if diff is None:
        return {"ok": False, "error": "the agreements could not be stored"}
    logger.info("tariff agreements: Octopus's answer of %s accepted by the user — %d added, "
                "%d superseded", m["at"], len(diff["added"]), len(diff["superseded"]))
    return {"ok": True, "added": len(diff["added"]), "superseded": len(diff["superseded"])}




# A customer can change only recent agreements — 14 days' cooling-off, a switch reversed — so an answer
# that drops or moves one that started and ended longer ago than this is not taken (G1 design §2).
_AGREEMENT_CHANGE_DAYS = 30


def _unexpected_drops(current, rows, now) -> list:
    """The stored agreements an answer would drop or move that no customer change explains: started
    and ended over `_AGREEMENT_CHANGE_DAYS` ago, on a meter point the answer still has. A new
    agreement is always taken; so is a reversal of a recent one (its predecessor's end reopened),
    and a new meter point's (another MPAN is not an edit of this one's history).
    [(direction, tariff_code, valid_from, valid_to)]."""
    cutoff = (now - timedelta(days=_AGREEMENT_CHANGE_DAYS)).isoformat()
    key = lambda r: (r["mpan"], r["direction"], r["tariff_code"], r["valid_from"], r.get("valid_to"))
    new = {key(r) for r in rows}
    mpans = {(r["mpan"], r["direction"]) for r in rows}
    out = []
    for r in current or []:
        if key(r) in new or (r["mpan"], r["direction"]) not in mpans:
            continue
        if r["valid_from"] >= cutoff or (r.get("valid_to") and r["valid_to"] >= cutoff):
            continue
        out.append((r["direction"], r["tariff_code"], r["valid_from"], r.get("valid_to")))
    return out


# The spans a change of agreements re-settles (G1 design §4), worked off by the settle sweep's job
# once the history walk is done: [[from, to], …] naive UTC, in store_meta so a restart resumes them.
_SETTLE_SPANS_KEY = "settle_agreement_spans"


def _merge_spans(spans) -> list:
    out = []
    for lo, hi in sorted([list(x) for x in spans if x[0] < x[1]]):
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out


def _agreement_change_spans(diff, first, end) -> list:
    """The history a change of agreements moves (G1 design §4): each added or superseded agreement's
    window — a moved window is both, its old and new — clipped to [first, end) and merged. No
    agreements before to some now (the first answer stored) is the whole history."""
    if not first or not end or not (diff.get("added") or diff.get("superseded")):
        return []
    if not diff.get("kept") and not diff.get("superseded"):
        return [[first, end]]
    out = []
    for _direction, _code, vf, vt in list(diff.get("added") or []) + list(diff.get("superseded") or []):
        lo, hi = max(vf, first), min(vt or end, end)
        if lo < hi:
            out.append([lo, hi])
    return _merge_spans(out)


def _queue_settle_spans(spans) -> None:
    """Add `spans` to the re-settle queue, merged, and have the sweep's job take them."""
    if not spans or _store is None:
        return
    cur = _store.get_meta(_SETTLE_SPANS_KEY, []) or []
    q = _merge_spans(list(cur) + list(spans))
    _store.set_meta(_SETTLE_SPANS_KEY, q)
    logger.info("tariff agreements: changed — %d span(s) queued to re-settle (%s to %s)",
                len(q), q[0][0], q[-1][1])


def _settle_spans_pending() -> bool:
    try:
        return bool((_store.get_meta(_SETTLE_SPANS_KEY, []) or []) if _store is not None else [])
    except Exception:
        return False


async def _settle_spans_pass(max_days: int = 100000) -> int:
    """settle() over the queued spans, a UTC day at a time, each a transaction, the loop
    yielded between; stands aside as the sweep does. Returns the half-hours written."""
    import asyncio as _aio
    written = 0
    for _ in range(max_days):
        spans = (_store.get_meta(_SETTLE_SPANS_KEY, []) or []) if _store is not None else []
        if not spans:
            break
        if _store is None or _coord.admission("settle_sweep")[0] != ADMIT:
            logger.info("settle spans: standing aside (resumes next pass)")
            return written
        lo, hi = spans[0]
        nxt = min((datetime.fromisoformat(lo[:10] + "T00:00:00") + _SETTLE_HISTORY_DAY).isoformat(), hi)
        with _store.batch():
            try:
                written += settle_range(_store, lo, nxt)["written"]
            except Exception as e:
                logger.warning("settle spans: %s to %s failed (skipped): %s", lo, nxt, e)
            _store.set_meta(_SETTLE_SPANS_KEY, ([[nxt, hi]] if nxt < hi else []) + list(spans[1:]))
        if nxt >= hi and len(spans) == 1:
            logger.info("settle spans: the changed agreements' history re-settled")
        await _aio.sleep(_SETTLE_HISTORY_PACE_S)
    return written


def _log_label_mismatch() -> None:
    """A config period labelled Octopus with half-hours no Octopus agreement covers (G1 design §6):
    the agreement wins — they read `no_agreement` — and it is logged, at most daily (each discovery
    answer stored). Never raises."""
    try:
        wins = [(vf, vt) for _c, vf, vt in _agreement_windows("import") if vf]
        if not wins:
            return
        r = _reader().octopus_labelled_outside(wins)
        if r["n"]:
            logger.warning("tariff agreements: %d half-hour(s) of a period labelled Octopus are on no "
                           "Octopus agreement (%s to %s) — read as not on an agreement; check the "
                           "period's supplier", r["n"], r["first"], r["last"])
    except Exception as e:
        logger.debug("tariff agreements: label check skipped (%s)", e)


def _load_stored_agreements() -> None:
    """The stored agreements of the account this database is stamped to, into `_stored_agreements`
    (G1 design §3). Run at start-up before discovery, and after each write."""
    global _stored_agreements
    try:
        rows = _reader().get_tariff_agreements(get_db_account())
    except Exception as e:
        logger.debug("tariff agreements: none loaded (%s)", e)
        rows = []
    out = {}
    now = datetime.utcnow().isoformat()
    for r in rows:
        info = out.setdefault(r["direction"], {"mpan": r["mpan"], "tariff_code": None, "agreements": []})
        info["agreements"].append({"tariff_code": r["tariff_code"], "valid_from": r["valid_from"],
                                   "valid_to": r["valid_to"]})
        if r["valid_from"] <= now and (r["valid_to"] is None or now < r["valid_to"]):
            info["tariff_code"] = r["tariff_code"]          # the one live now, as discovery's
    _stored_agreements = out or None


def _agreement_windows(channel: str) -> list:
    """[(tariff_code, from, to)] of `channel`'s agreements, naive-UTC ISO, in discovery order.

    Parsed once per discovery (28 Oct 2026): `_agreement_era` is asked for every half-hour the ask
    selection reads — `tariff_kind_at`, `split_at`, `bill_kind_at` — and parsing every agreement's
    dates on each call held dev's engine loop for up to 26 s every hour, long enough to drop HA's
    WebSocket, whose reconnect re-ran startup under the running ask."""
    import api_import as _ai
    ags = _agreement_info(channel).get("agreements") or []
    hit = _AGREEMENT_WINDOWS.get(channel)
    if hit is not None and hit[0] is ags and hit[2] == len(ags):
        return hit[1]
    out = []
    for a in ags:
        code = a.get("tariff_code")
        if not code:
            continue
        try:
            vf = _ai._iso(_ai._naive_utc(a.get("valid_from"))) if a.get("valid_from") else None
            vt = _ai._iso(_ai._naive_utc(a.get("valid_to"))) if a.get("valid_to") else None
        except Exception:
            continue
        out.append((code, vf, vt))
    _AGREEMENT_WINDOWS[channel] = (ags, out, len(ags))
    return out


def _agreement_era(channel: str, start: str):
    """(tariff_code, from, to) of the agreement live at `start`, or None.

    Coverage is judged PER AGREEMENT because that is the unit Octopus configures product
    rates against — a raw whole-import count mixes a blank tariff era in with healthy ones
    and hides both.
    """
    for code, vf, vt in _agreement_windows(channel):
        if vf and start >= vf and (vt is None or start < vt):
            return (code, vf, vt)
    return None


def _is_intelligent_code(code) -> bool:
    _tu = (code or "").upper()
    return "IOG" in _tu or "INTELLI" in _tu


def export_agreement_at(start: str):
    """Whether an export agreement covers the half-hour at `start`: True / False where discovery
    holds the export MPAN (its agreements, `_agreement_era`; an MPAN with none is False — the
    FIT-like case), None where it holds no export MPAN or has not run. With no agreement there is
    no export rate: nothing is paid, and the Export bill has nothing to settle (17 Oct 2026)."""
    info = _agreement_info("export")
    if not info.get("mpan"):
        return None
    return _agreement_era("export", start) is not None


def tariff_kind_at(start: str) -> str:
    """'intelligent' | 'published' for the import tariff live at `start` (settlement surfaces
    design §9.1). On a published-rate tariff the settled kWh x the half-hour's published rate is
    the bill; on an Intelligent one a dispatch can re-band a half-hour, so only Octopus's cost is
    final. From the agreements discovery holds (`_agreement_era`). Where agreements are known and
    none covers `start`, 'none': not on an Octopus agreement — another supplier's, or a gap (G1 design
    §3, §6). The current tariff's kind only where no agreement is known at all."""
    era = _agreement_era("import", start)
    if era:
        code = era[0]
    elif _agreement_windows("import"):
        return "none"
    else:
        code = _agreement_info("import").get("tariff_code")
    return "intelligent" if _is_intelligent_code(code) else "published"


def published_rate_at(start: str):
    """The published rate (£/kWh inc VAT) of the half-hour's own import agreement, where that
    agreement is known and published and the stitched schedule prices it; None otherwise —
    Intelligent, no agreement covering it (no fallback to the current tariff, unlike
    `tariff_kind_at`), or no rate there. On a published tariff the settled kWh x this rate is the
    bill, at any age (published tariff final design §2)."""
    era = _agreement_era("import", start)
    if not era or _is_intelligent_code(era[0]) or not _agreement_priced_ok(start):
        return None
    sched = _kraken_rate_schedules.get("import")
    try:
        r = sched.resolve(start) if sched is not None and not sched.is_empty() else None
    except Exception:
        r = None
    return None if r is None else round(r / 100.0, 6)


def split_at(start: str) -> bool:
    """Whether the import agreement live at `start` is one Octopus serves a House/EV split for
    (IOG-SMB's device buckets). Before it EMT's split is the only one there will be, so it is
    not "still EMT's" (PR 5 on the Pi, 14 Oct 2026: every pre-agreement dispatched half-hour read
    teal for good). From `_agreement_era`; where none covers `start`, the current tariff."""
    era = _agreement_era("import", start)
    if era:
        return "IOG-SMB" in (era[0] or "").upper()
    return _import_is_smb_capped()

def _record_import_gaps(channel: str, gaps_key: str, checkpoint, imported) -> None:
    """Detect runs of MISSING half-hours this slice reveals — within the fetched
    set and at the checkpoint boundary — and APPEND them to the persisted
    per-channel gap list (kraken_state JSON). `imported` is newest→oldest.
    Non-fatal."""
    import json as _json
    import api_import as _ai
    try:
        seq = ([checkpoint] if checkpoint else []) + list(imported)   # descending
        new_gaps = []
        for i in range(len(seq) - 1):
            try:
                a = datetime.fromisoformat(seq[i])          # newer
                b = datetime.fromisoformat(seq[i + 1])      # older
            except Exception:
                continue
            missing = int((a - b).total_seconds() // 1800) - 1        # 30-min slots
            if missing > 0:
                new_gaps.append({
                    "from": _ai._iso(b + timedelta(minutes=30)),
                    "to": _ai._iso(a - timedelta(minutes=30)),
                    "count": missing,
                })
        if not new_gaps:
            return
        try:
            existing = _json.loads(_store.get_kraken_state(gaps_key) or "[]")
        except Exception:
            existing = []
        existing.extend(new_gaps)
        _store.set_kraken_state(gaps_key, _json.dumps(existing))
    except Exception as e:
        logger.info("import: gap recording skipped (%s): %s", channel, e)


def api_import_gaps() -> dict:
    """Persisted reconstruction gaps per channel (missing half-hour runs found
    during the historical import), for the import page to display without any
    re-query. Returns {channels:{import:{gaps:[...], missing:N}, export:{...}}}.

    Self-clearing: each persisted gap is reconciled against the live blocks table —
    once its slots carry data (a later import or a backfill filled them), the gap is
    pruned from the stored list, so the display reflects what's ACTUALLY still
    missing rather than a stale snapshot from import time."""
    import json as _json
    out = {"channels": {}}
    if _store is None:
        return out
    for channel in ("import", "export"):
        try:
            gaps = _json.loads(_reader().get_kraken_state("import_gaps_" + channel) or "[]")
        except Exception:
            gaps = []
        # Reconcile against reality — drop gaps whose slots are now all filled.
        kept = []
        for g in gaps:
            gf = g.get("from")
            gt = g.get("to") or gf
            need = int(g.get("count") or 0)
            try:
                have = _reader().channel_slots_present(channel, gf, gt) if gf else 0
            except Exception:
                have = 0
            if need <= 0 or have < need:
                kept.append(g)
        if len(kept) != len(gaps):
            # Persist the prune only on the engine thread. From a request thread the
            # engine's connection raises (A §5.5); the display still shows the pruned
            # list, and the next engine-side call stores it.
            import store_access as _sa
            if _sa.on_engine_thread():
                try:
                    _store.set_kraken_state("import_gaps_" + channel, _json.dumps(kept))
                except Exception:
                    pass
            gaps = kept
        missing = sum(int(g.get("count") or 0) for g in gaps)
        gaps_sorted = sorted(gaps, key=lambda g: g.get("from") or "", reverse=True)
        out["channels"][channel] = {"gaps": gaps_sorted, "missing": missing,
                                    "gap_count": len(gaps)}
    return out


async def import_api_history(requested_from=None, *, chunk_days: int = 60,
                             max_chunks: int = 6, pace_s: float = 0.0,
                             headroom_frac: float = 0.25, dry_run: bool = False,
                             restart: bool = False,
                             meter_id: str = "electricity_main",
                             until_ts=None,
                             channels=("import", "export"),
                             segs: dict | None = None) -> dict:
    """API import route apply — INCREMENTAL and rate-limit-aware.

    `segs`: the caller's {(channel, kind): segments}, kept across its calls — the import job's, so
    its rate and standing segments are built once a job, not on every call (5 Nov 2026: a slow
    account's import retried a chunk about every three minutes for seven hours, each retry
    building them again, the running agreement fetched from Octopus each time).

    Fetches half-hourly GraphQL Measurements (kWh + billed cost + Octopus's own
    OFF_PEAK/STANDARD_RATE label) newest→oldest and writes `imported_api` blocks.
    Pricing is exact and dispatch-aware (no tariff reconstruction / clustering).

    Bounded per call: processes at most `max_chunks` chunks, resuming from the
    per-channel checkpoint, so a full-history import spans several short calls (the
    UI loops until `done`). Between chunks it paces (`pace_s`, which also yields the
    engine loop) and checks `rateLimitInfo` — if remaining points fall below
    `headroom_frac × limit` it PAUSES (leaving budget for BCD / EMT's live poll)
    and returns `reason: 'rate_limit'` so the caller resumes later. `restart` clears
    the checkpoint to begin again from go-live. Reversible via the reconstructed-
    history delete filter."""
    import asyncio as _asyncio
    import api_import as _ai
    from block_store import IMPORTED_SOURCE_API
    if not kraken_available():
        return {"ok": False, "reason": "no_api", "channels": {}}
    if _store is None:      # e.g. a restore closed the store — never write to it
        return {"ok": False, "reason": "no_store", "channels": {}}
    # BOUNDED mode: fetch just [requested_from → until_ts] and write those blocks,
    # WITHOUT touching the full-import resume state (checkpoint / done flag / gap
    # record). This is the "fill this gap / this contiguous period" primitive the
    # two task flows use. A missing block has nothing to settle; a bounded run just
    # (re)fetches the supplier's data for the window and upserts it.
    bounded = until_ts is not None
    if bounded and not requested_from:
        return {"ok": False, "reason": "no_from", "channels": {}}
    # Ceiling = oldest EXISTING block of ANY source (contiguity rule) so a new
    # backward import stops at the oldest block we already have and only fills the
    # remaining gap below it — see plan_api_import. An EMPTY store (fresh API-only
    # install) has nothing to tile up to, so the ceiling is the current half-hour.
    go_live, _ceiling_source = _import_ceiling()
    if not go_live and not bounded:
        return {"ok": False, "reason": "no_store", "channels": {}}
    disc = _kraken_discovery or {}
    out: dict = {"ok": True, "go_live": go_live, "dry_run": dry_run, "channels": {}}
    all_done = True
    for channel in channels:
        info = disc.get(channel) or {}
        mpan = info.get("mpan")
        if not mpan:
            out["channels"][channel] = {"ok": False, "available": False, "done": True}
            continue
        ck_key = _API_IMPORT_CHECKPOINT + "_" + channel
        done_key = "api_import_done_" + channel
        gaps_key = "import_gaps_" + channel
        top_key = "api_import_top_" + channel
        if restart and not dry_run and not bounded:
            _store.set_kraken_state(ck_key, "")
            _store.set_kraken_state(done_key, "")
            _store.set_kraken_state(gaps_key, "")
            _store.set_kraken_state(top_key, "")
        if not dry_run and not bounded and _store.get_kraken_state(done_key) == "1":
            out["channels"][channel] = {"ok": True, "done": True,
                                        "reason": "already_done", "written": 0}
            continue
        direction = "GENERATION" if channel == "export" else "CONSUMPTION"

        # Tariff rate schedules → clean display rate + a price for half-hours Measurements
        # returns without cost; the standing charge (authoritative, complete — see
        # _standing_for). Through the one per-agreement fetch: closed agreements are cached for
        # the process, the running one fetched each run (engine step 5c).
        if segs is not None and (channel, "rate") in segs:
            rate_segs, standing_segs = segs[(channel, "rate")], segs[(channel, "standing")]
        else:
            rate_segs = await _build_channel_rate_segs(channel)
            standing_segs = await _build_channel_standing_segs(channel)
            if segs is not None:
                segs[(channel, "rate")], segs[(channel, "standing")] = rate_segs, standing_segs

        floor = requested_from or await _cached_data_floor(channel, info,
                                                            restart=restart)
        if not floor:
            gl_dt = _ai._naive_utc(go_live)
            floor = _ai._iso(gl_dt - timedelta(days=1100)) if gl_dt else None
        # Don't fetch below the rolling ~2-year retention wall — the API returns
        # empty there, so it's wasted rate-limit budget. Older history is CSV-only.
        if floor and not bounded:
            _rf = _retention_floor_iso()
            if floor < _rf:
                floor = _rf
        checkpoint = (_store.get_kraken_state(ck_key) or None) if (not dry_run and not bounded) else None
        # The channel's own top, kept from its first call (5 Nov 2026, a fresh install's support file):
        # until a chunk lands the channel has no checkpoint, and the ceiling is the oldest block of
        # ANY source — so when the import's first chunk failed and the export's landed, the export's
        # rows became the import's ceiling and its 60 days were never asked again (September and
        # October imported with no import kWh). The checkpoint keeps its meaning (the gaps use it).
        top = None
        if not dry_run and not bounded and not checkpoint:
            top = _store.get_kraken_state(top_key) or None
            if not top and go_live:
                top = go_live
                _store.set_kraken_state(top_key, go_live)
        hi = until_ts if bounded else (checkpoint or top or go_live)
        window = _ai.clamp_window(floor, hi)
        if not window.get("ok"):
            if not dry_run:
                _store.set_kraken_state(done_key, "1")
            out["channels"][channel] = {"ok": True, "done": True,
                                        "reason": "completed", "written": 0}
            continue
        chunks = _ai.plan_chunks(window["from"], window["to"], chunk_days=chunk_days)
        slice_ = chunks[:max_chunks]

        # Fetch the slice, paced + rate-limit-aware.
        by_start: dict = {}
        fetch_error = None
        paused = False
        for c in slice_:
            if headroom_frac > 0:
                try:
                    rl = await _kraken_client.get_rate_limit()
                except Exception:
                    rl = None
                if rl:
                    low = (rl.get("remaining") is not None and rl.get("pointsLimit")
                           and rl["remaining"] < headroom_frac * rl["pointsLimit"])
                    if rl.get("isBlocked") or low:
                        paused = True
                        break
            try:
                rows = await _kraken_client.get_measurements(
                    mpan, c["from"] + "Z", c["to"] + "Z", direction=direction)
            except Exception as e:
                fetch_error = str(e)
                logger.warning("import [%s]: Octopus did not answer %s..%s (%s) — asked again on the next pass",
                               channel, c["from"], c["to"], fetch_error or type(e).__name__)
                break
            for r in (rows or []):
                if r.get("start"):
                    by_start[r["start"]] = r
            # ── EXPORT REST fallback ─────────────────────────────────────────
            # The half-hourly *Measurements* export feed can begin LATER than the
            # raw meter readings — Octopus bills export monthly from meter install
            # but its settled half-hourly export series may only start months
            # later. Export has no off-peak/dispatch, so raw REST kWh priced from
            # the tariff schedule (a flat OUTGOING rate OR Agile Outgoing's
            # half-hourly rate, both via resolve()) reproduces the bill exactly.
            # Fill any half-hour REST returns that Measurements didn't.
            if channel == "export" and info.get("serial"):
                # Match Measurements' EXCLUSIVE upper bound: the REST endpoint
                # includes the period_to boundary interval, which is the NEXT
                # (newer) chunk's oldest half-hour. Re-adding it here makes this
                # slice's newest timestamp one step higher than the checkpoint
                # expects, so the newest→oldest contiguity check rejects the whole
                # slice and halts the import at the slice boundary.
                _chunk_top = _ai._iso(_ai._naive_utc(c["to"] + "Z"))
                try:
                    rest_rows = await _kraken_client.get_consumption(
                        mpan, info["serial"],
                        period_from=c["from"] + "Z", period_to=c["to"] + "Z")
                except Exception as _rest_e:
                    rest_rows = []
                    logger.info("import: export REST fallback fetch failed "
                                "(%s..%s): %s", c["from"], c["to"], _rest_e)
                for rr in (rest_rows or []):
                    _isv = rr.get("interval_start")
                    st = _ai._iso(_ai._naive_utc(_isv)) if _isv else None
                    if st and (_chunk_top is None or st < _chunk_top) \
                            and st not in by_start:
                        by_start[st] = {
                            "start": st,
                            "kwh": float(rr.get("consumption") or 0.0),
                            "cost_incl": None,      # priced from the tariff below
                            "off_peak": None,       # export: no off-peak bucket
                            "standing_incl": 0.0,
                        }
            if pace_s:
                await _asyncio.sleep(pace_s)

        # Import EVERY fetched half-hour — do NOT halt at internal discontinuities.
        # Real supplier data has gaps: settlement thinning near where the settled
        # half-hourly series begins, the DST spring-forward's missing hour, meter
        # comms dropouts. Halting at the first gap permanently stopped the backfill
        # short (it kept tripping on chunk boundaries, then the March DST change).
        # Reconstructed history is allowed to have holes; termination is by reaching
        # the floor, or by a fully-empty chunk (no_more_data) once we descend below
        # where the data actually begins.
        starts = sorted(by_start.keys(), reverse=True)
        imported = list(starts)

        # Record any gaps (runs of missing half-hours) this slice reveals, so the
        # reconstruction's holes are surfaced to the user instead of silently
        # under-reporting a bill period. Zero-export nights come back as zero-value
        # rows (not omitted), so a gap here is a genuine hole — a DCC/settlement
        # outage. Persisted per channel; survives reload without re-querying.
        if not dry_run and not bounded:
            _record_import_gaps(channel, gaps_key, checkpoint, imported)

        # Standing charge per day. PREFER the tariff standing-charge schedule
        # (authoritative + complete); only fall back to the summed Measurements
        # figure where the schedule doesn't cover the date. Measurements'
        # STANDING_CHARGE_COST is patchy (whole stretches return none → £0/day),
        # which was silently under-billing the reconstructed range.
        # By LOCAL day (31 Oct 2026, ROADMAP BL-92): by the UTC date, a summer local day's first
        # half-hour (23:00 UTC the day before) carried the day before's charge — the one the
        # readers take — so a summer price change showed a day late.
        _sc_tz = _iog_site_tz()

        def _sc_day(st):
            return _local_date_of_utc(st, _sc_tz)
        meas_standing: dict = {}
        meas_count: dict = {}
        for st in imported:
            d = _sc_day(st)
            meas_standing[d] = (meas_standing.get(d, 0.0)
                                + (by_start[st].get("standing_incl") or 0.0))
            meas_count[d] = meas_count.get(d, 0) + 1
        try:
            _block_min = max(1, int(get_block_minutes()))
        except Exception:
            _block_min = 30

        def _slots_in(d):
            # a local day's half-hours: 46 or 50 on a clock-change day
            try:
                import block_store as _bs
                a, b = _bs.local_date_to_utc_bounds(d, _sc_tz)
                return int(round((datetime.fromisoformat(b) - datetime.fromisoformat(a))
                                 .total_seconds() / 60 / _block_min))
            except Exception:
                return int(round(24 * 60 / _block_min))
        day_standing: dict = {}
        for d in meas_standing:
            sc = _standing_for(standing_segs, d)
            if sc is not None:
                day_standing[d] = sc
            else:
                # Measurements spreads the daily standing charge across the day's
                # intervals, so a PARTIAL day (a gap or the go-live boundary) sums
                # to only a FRACTION of the charge (e.g. ¾ × 0.4512 = 0.3384). You
                # pay the full standing charge regardless of how many half-hours
                # have data — extrapolate the present intervals back to a whole day.
                n = meas_count.get(d, 0)
                day_standing[d] = (round(meas_standing[d] / n * _slots_in(d), 6)
                                   if n else meas_standing[d])

        priced = off_peak = 0
        unscheduled = []                   # half-hours the schedule has no rate for
        rows_to_write = []
        for st in imported:
            r = by_start[st]
            kwh = r.get("kwh") or 0.0
            meas_cost = r.get("cost_incl")
            ofp = r.get("off_peak")
            # The rule (engine step 2d, 23 Oct 2026): kWh × the schedule's rate for Octopus's
            # band — the label's (OFF_PEAK the day's low rate, STANDARD its high), else the
            # half-hour's own (a published tariff, Agile, export). Octopus's cost is kept in the
            # bill cache as its answer (step 2c), not copied here; the apply settles the block
            # from it. (`_billed_rate` took cost ÷ kWh unless the schedule agreed within 0.1p.)
            rate = _tariff_rate_for(rate_segs, st, ofp)
            if rate is not None:
                cost = round(kwh * rate, 6) if kwh > 0 else 0.0
                rate_exc = _tariff_rate_for(_exc_segs(rate_segs), st, ofp)
                cost_exc = round(kwh * rate_exc, 6) if (rate_exc is not None and kwh > 0) else (
                    0.0 if rate_exc is not None else None)
            elif meas_cost is not None:
                # No schedule rate for the day: Octopus's own figure rather than an unpriced
                # block (PASS 2 cannot cost a device at a NULL rate). Counted and logged.
                cost, cost_exc = meas_cost, r.get("cost_excl")
                rate = round(meas_cost / kwh, 6) if kwh > 0 else None
                if kwh > 0:
                    unscheduled.append(st)
            else:
                cost = cost_exc = None
            if cost is not None:
                priced += 1
            if ofp:
                off_peak += 1
            # Standing charge is an IMPORT-meter charge on the shared block row —
            # the export channel must not write it (it would clobber the import's
            # value with 0, since OUTGOING tariffs have no standing charge).
            _standing = day_standing.get(_sc_day(st)) if channel == "import" else None
            rows_to_write.append({"start": st, "kwh": kwh, "rate": rate,
                                  "cost": cost, "standing": _standing,
                                  "cost_exc": cost_exc,
                                  "_octopus": meas_cost is not None,
                                  # Octopus's own figures, for the bill cache (not the block)
                                  "_oc": meas_cost, "_oce": r.get("cost_excl"),
                                  "_label": _import_label(ofp)})
        # The half-hours left without Octopus's cost (EMT's kWh × rate) and those that have it:
        # the Import bill hatches the first at once — nothing asks for them again (19 Oct 2026).
        if unscheduled:
            logger.warning("import [%s]: %d half-hour(s) the tariff schedule has no rate for, "
                           "priced at Octopus's own figure (first %s)",
                           channel, len(unscheduled), unscheduled[0])
        _no_band = [r["start"] for r in rows_to_write
                    if not r.get("_octopus") and (r.get("kwh") or 0) > 0] if channel == "import" else []
        # Octopus's answers kept as band evidence (engine step 2c): each costed Intelligent
        # half-hour's label and cost, in the bill cache, so it settles through the one rule
        # (kWh × the band's rate) as a live half-hour does. In the chunk's own transaction.
        _bills = ([{"start": r["start"], "cost_incl": r["_oc"], "cost_excl": r.get("_oce"),
                    "label": r.get("_label"), "kwh": r.get("kwh")}
                   for r in rows_to_write
                   if r.get("_octopus") and (r.get("kwh") or 0) > 0
                   and tariff_kind_at(r["start"]) == "intelligent"]
                  if not dry_run and channel == "import" else [])
        if _bills:
            try:
                _store.upsert_imported_bills(_bills, mpan=mpan)
            except Exception as _be:
                logger.warning("import: keeping Octopus's bands failed (non-fatal): %s", _be)
        for _r in rows_to_write:
            _r.pop("_octopus", None)
            _r.pop("_label", None)
            _r.pop("_oc", None)
            _r.pop("_oce", None)
        written = 0
        if not dry_run and rows_to_write:
            # One transaction for the whole chunk (not a commit per block) so the
            # write doesn't stall the async loop and starve the HA WebSocket.
            written = _store.upsert_imported_blocks(
                rows_to_write, meter_id, channel, source=IMPORTED_SOURCE_API)
            await _asyncio.sleep(0)      # yield the loop after the batch write
        if not dry_run and not bounded and imported:
            _store.set_kraken_state(ck_key, imported[-1])

        # Done when: gap-halt, or this slice reached the floor (all chunks fit),
        # or no data left below (past the account's start) — but never when paused
        # or a fetch failed (those must resume).
        reached_floor = len(chunks) <= max_chunks
        no_more_data = (not by_start) and (not paused) and (not fetch_error)
        done = (reached_floor or no_more_data) and not paused and not fetch_error
        reason = ("rate_limit" if paused
                  else "fetch_error" if fetch_error
                  else "completed" if done else "more")
        if done and not dry_run and not bounded:
            _store.set_kraken_state(done_key, "1")
        if not done:
            all_done = False

        out["channels"][channel] = {
            "ok": True, "done": done, "reason": reason,
            "fetched": len(by_start), "imported": len(imported),
            "written": written, "priced": priced, "off_peak_intervals": off_peak,
            # no cost, so no band, from Octopus: EMT asks for them (step 2 design §5 (c))
            "no_band": len(_no_band),
            "fetch_error": fetch_error, "paused": paused,
            "oldest": (imported[-1] if imported else checkpoint),
            "window": {"from": window["from"], "to": window["to"]},
        }
    out["done"] = all_done
    return out

# ── API import as a background job (pause / resume / cancel) ──────────────────
# The import runs as a task on the engine loop, not driven by the browser, so it
# survives navigating away and can be paused/cancelled. State is in-memory (single
# add-on process); the per-channel checkpoint in kraken_state means a restart
# mid-import loses only the job UI, not the progress (re-Start resumes idempotently).
_api_import_job: dict = {"status": "idle"}


def api_import_status() -> dict:
    """JSON-safe snapshot of the background import job. When nothing is live in
    memory (idle — e.g. an add-on restart, or a page reloaded after a gap fill
    finished) fall back to the persisted summary of the LAST completed run, so the
    import panel shows accurate 'N blocks imported' + status on ANY load instead of
    a blank/zero panel. (Poll-on-load, like the gaps + pricing-health panels.)"""
    if _api_import_job.get("status") in (None, "idle") and _store is not None:
        try:
            import json as _json
            raw = _reader().get_kraken_state(_IMPORT_RUN_KEY)
            if raw:
                d = _json.loads(raw)
                d["persisted"] = True
                return d
        except Exception:
            pass
    return {k: v for k, v in _api_import_job.items() if k != "task"}


def api_import_running() -> bool:
    # "claimed": reserved by the start route before its safety backup (B §5,
    # acceptance 4) — running as far as anyone who must not overlap an import knows.
    return _api_import_job.get("status") in ("claimed", "running", "paused", "rate_limited")


_api_import_lock = threading.Lock()


def api_import_claim() -> bool:
    """Reserve the API import, atomically, BEFORE the start route's slow work. False
    if one is already running or claimed.

    B §5 acceptance 4, the attribution window again (attribution_claim): the route
    checked `api_import_running()`, then zipped the whole database — a minute or more
    on the Pi — and only then scheduled the job, which claimed "import" when it ran.
    Inside that minute a second start passed the same check, and a delete or restore
    saw no import to stop. Claiming first closes it. Callers MUST call
    `api_import_release()` on every path that then fails to schedule the job."""
    with _api_import_lock:
        if api_import_running():
            return False
        _coord.claim("import", force=True)
        _api_import_job.clear()
        _api_import_job.update({"status": "claimed", "phase": "taking a safety backup",
                                "control": "run", "started_at": _dt_now_iso_safe()})
        return True


def api_import_release() -> None:
    """Undo api_import_claim when the job will not be scheduled."""
    with _api_import_lock:
        if _api_import_job.get("status") == "claimed":
            _api_import_job.update({"status": "idle", "phase": None})
            _coord.mark("import", status="done")


def api_import_control(action: str) -> dict:
    """pause / resume / cancel the running import (sets a flag the worker polls)."""
    j = _api_import_job
    if not api_import_running():
        return {"ok": False, "status": j.get("status"), "note": "no active import"}
    if action == "pause":
        j["control"] = "pause"
    elif action == "resume":
        j["control"] = "run"
    elif action == "cancel":
        j["control"] = "cancel"
    else:
        return {"ok": False, "error": f"unknown action: {action}"}
    return {"ok": True, "status": j.get("status"), "control": j["control"]}


def _import_extend_floor(earliest_import, plan) -> "str | None":
    """The earliest date it is SAFE to pull the current (oldest existing) config
    period's start back to after an import.

    Normally the earliest imported block. BUT when a **move** is detected (the
    region probe stashed a multi-site reconcile plan awaiting confirmation), the
    oldest existing period is the CURRENT site — so we stop at that site's move-in
    (the latest tenancy's `from`) and never cross into an earlier site's span.
    That prevents pre-move data (and its carbon region) being claimed by the
    current site before the user confirms the split. Pure/testable."""
    if plan and plan.get("needs_confirmation"):
        froms = [s.get("from") for s in (plan.get("sites") or []) if s.get("from")]
        if froms:
            return max(froms)
    return earliest_import


def _finalise_after_import() -> None:
    """Post-import housekeeping:

    1. **Extend the earliest config period** back to cover the imported history,
       so Billing history reflects the real data start (not the go-live date) and
       every imported block falls inside a period that temporally covers it —
       clamped by `_import_extend_floor` so a pending move never swallows an
       earlier site.
    2. **Re-arm the carbon backfill** so a (re-)import's NULL-carbon blocks fill
       automatically (a stale `unfilled_from` marker would otherwise block the
       self-heal re-arm). DEFERRED while a move awaits confirmation — pre-move
       blocks would resolve to the current region until the split is applied; the
       reconcile-apply re-arms once the sites (and regions) are set.
    """
    if _store is None:
        return
    # C §2a: an import writes history across years. Bump the EPOCH rather than
    # move a fingerprint for every day it touched — this is exactly the
    # "throw everything away" case the epoch exists for.
    try:
        _store.bump_data_epoch()
    except Exception as _ep_e:
        logger.warning("_finalise_after_import: epoch bump failed: %s", _ep_e)
    move_pending = False
    try:
        plan = _store.get_meta("region_reconcile_pending", None)
        move_pending = bool(plan and plan.get("needs_confirmation"))
        info = _store.count_imported_history()
        floor = _import_extend_floor(info.get("from"), plan)
        if floor:
            _store.extend_earliest_period_to(floor)
    except Exception as e:
        logger.warning("_finalise_after_import: period extend failed: %s", e)
    if move_pending:
        logger.info("_finalise_after_import: move awaiting confirmation — "
                    "deferring carbon re-arm to the reconcile apply")
        return
    try:
        _store.rearm_carbon_backfill()
    except Exception as e:
        logger.warning("_finalise_after_import: carbon re-arm failed: %s", e)


def _run_wrote_blocks(job: dict) -> int:
    """How many blocks a finished import/gap-fill job actually wrote."""
    try:
        return sum(int(v or 0) for v in (job.get("written") or {}).values())
    except Exception:
        return 0

_IMPORT_RUN_KEY = "import_run_status"   # kraken_state: last completed import/gap-fill run


def _persist_run_status(j: dict) -> None:
    """Persist a compact snapshot of the LAST completed import/gap-fill run so the
    import panel renders accurate 'N blocks imported' + status/window on any page
    load, not only from the ephemeral in-memory job (api_import_status reads this
    when nothing is live). Overwritten by the next run."""
    if _store is None:
        return
    import json as _json
    try:
        # The durable snapshot represents the LAST COMPLETED run, so its status
        # MUST be terminal. The API-import path calls this while j is still
        # 'finalising' (status flips to 'done' immediately after) — persist 'done'
        # so a later add-on RESTART, where api_import_status falls back to this
        # snapshot because nothing is live in memory, doesn't read back a
        # 'finalising'/'running' status and resurrect a phantom import that locks
        # the import page indefinitely.
        _st = j.get("status") or "done"
        if _st in ("finalising", "running", "rate_limited", "paused"):
            _st = "done"
        snap = {
            "status": _st,
            "phase": "done",
            "written": j.get("written") or {},
            "oldest": j.get("oldest") or {},
            "gap": j.get("gap"),
            "go_live": j.get("go_live"),
            "no_band": int(j.get("no_band") or 0),
            "finished_at": _dt_now_iso_safe(),
        }
        _store.set_kraken_state(_IMPORT_RUN_KEY, _json.dumps(snap))
    except Exception as e:
        logger.warning("_persist_run_status: %s", e)

_REGION_CLOBBER_MARKER   = "region_probe_clobber_done"   # store_meta: one-time upgrade overwrite
_REGION_RECONCILE_MARKER = "region_reconcile_pending"    # store_meta: plan awaiting user confirmation
_PREIMPORT_SITES_MARKER  = "preimport_sites_applied"     # store_meta: sites confirmed+created BEFORE this import


async def discover_pre_import_sites() -> dict:
    """READ-ONLY. Fetch the account's property history and classify it for the
    PRE-import 'confirm your sites' wizard step (BlockStore.plan_pre_import_sites).

    Returns {"ok": True, "needs_confirmation": bool, "sites": [...]} — the latest
    tenancy is the current site (read-only instance name), earlier ones are past
    addresses the user names so a covering period can be created for each BEFORE
    the import runs, letting every imported block land in the right (correctly
    regioned, carbon-eligible) period from the first write. Writes nothing. A
    never-moved account returns needs_confirmation False so the wizard skips
    straight to the import."""
    global _kraken_client, _kraken_account_number, _store
    try:
        if _kraken_client is None or _store is None:
            return {"ok": False, "error": "no API / store"}
        from block_store import derive_region_periods
        account = await _kraken_client.get_account(_kraken_account_number)
        spans = derive_region_periods(account or {})
        plan = _store.plan_pre_import_sites(spans)
        return {"ok": True, **plan}
    except Exception as e:
        logger.warning("discover_pre_import_sites: %s", e, exc_info=e)
        return {"ok": False, "error": _error_text("site discovery", e)}


async def _probe_and_apply_region_periods() -> dict | None:
    """If the API is configured, read the account's property address history,
    reduce it to OUTWARD CODES only, and stamp the DNO region onto the config
    period timeline (BlockStore.apply_region_periods). Best-effort; never raises.

    This is the region foundation for historical carbon: it records which region
    applied when, so carbon can later be attributed per-period rather than from a
    single global postcode. Full postcodes are never stored — only the outward
    code (see block_store.outward_code / derive_region_periods).

    ONE-TIME upgrade clobber: the first time this runs on a given DB, it
    overwrites even a user-entered postcode with the authoritative Octopus supply
    address (so a legacy wrong-region entry — e.g. DE1 where the supply is SW1A —
    self-corrects). After that it respects manual edits (overwrite_user=False),
    so CSV-only users and deliberate overrides stand."""
    global _kraken_client, _kraken_account_number, _store
    try:
        if _kraken_client is None or _store is None:
            return None
        from block_store import derive_region_periods
        account = await _kraken_client.get_account(_kraken_account_number)
        spans = derive_region_periods(account or {})
        if not spans:
            return None
        # A move (or an unnamed site) needs the user to name sites and confirm the
        # config-period split — stash the plan for the confirmation UI rather than
        # silently rewriting billing history.
        plan = _store.plan_region_reconciliation(spans)
        if plan.get("needs_confirmation"):
            _store.set_meta(_REGION_RECONCILE_MARKER, plan)
            logger.info("_probe_and_apply_region_periods: %d site(s) need "
                        "confirmation, %d split(s) proposed — stashed for UI",
                        len(plan.get("sites") or []), len(plan.get("split_dates") or []))
            return plan
        # Single site, nothing to confirm → silent apply (with one-time clobber).
        first_time = not (_store.get_meta(_REGION_CLOBBER_MARKER, None))
        res = _store.apply_region_periods(spans, overwrite_user=first_time)
        if first_time:
            _store.set_meta(_REGION_CLOBBER_MARKER, _dt_now_iso_safe())
        if res.get("applied"):
            try:
                _store.rearm_carbon_backfill()   # region now known → re-scan carbon
            except Exception:
                pass
        logger.info("_probe_and_apply_region_periods: regions=%s applied=%d "
                    "split_required=%s one_time_clobber=%s",
                    res.get("outcodes"), res.get("applied"),
                    res.get("split_required"), first_time)
        return res
    except Exception as e:
        logger.warning("_probe_and_apply_region_periods: %s", e)
        return None


@job("gap_fill")
async def run_gap_fill_job(from_ts, to_ts, *, channels=("import", "export"),
                           chunk_days: int = 60, max_chunks: int = 6,
                           pace_s: float = 1.5,
                           meter_id: str = "electricity_main") -> None:
    """Background worker: fill ONE gap window [from_ts → to_ts] via the API, then
    the SAME finish as the full import — calm price-recovery verify pass, carbon
    re-arm, chart rebuild. Reuses `_api_import_job` so the shared status +
    pricing-health panel tracks it, and honours pause/cancel between chunks.

    Drives its own newest→oldest frontier (each slice's 'oldest') across iterations
    rather than the full-import checkpoint, so a bounded gap fill never disturbs a
    full-import resume point. One contiguous span; never raises."""
    j = _api_import_job
    j.clear()
    j.update({"status": "running", "phase": "importing", "control": "run",
              "started_at": _dt_now_iso_safe(),
              "written": {"import": 0, "export": 0}, "oldest": {},
              "no_band": 0,
              "reason": None, "error": None, "done": False, "go_live": None,
              "gap": {"from": from_ts, "to": to_ts}})
    # BL-8 phase 2: a gap fill over a SPECIFIC window is a TARGETED, explicit restore
    # (per-gap button / range / re-import) — lift any tombstone over it first (sub-
    # span split), so the range is genuinely un-deleted and the create-gate no longer
    # skips it. The blanket "recover all" path (resolve_history_gaps) does NOT do this
    # and so leaves deliberately-deleted ranges deleted.
    try:
        if _store is not None:
            _lifted = _store.clear_deleted_range(meter_id, from_ts, to_ts)
            if _lifted:
                logger.info("run_gap_fill_job: lifted %d tombstone(s) over %s..%s "
                            "(targeted re-import)", _lifted, from_ts, to_ts)
    except Exception as _te:
        logger.warning("run_gap_fill_job: tombstone clear failed (non-fatal): %s", _te)
    # Claim "import" for the whole run, as run_api_import_job does. A gap fill writes
    # the same imported blocks through the same import_api_history, and shares the
    # import's job state; every member that depends on "import" (the sweep, carbon
    # backfill, measured drain, verify, attribution) must stand aside for it. In v4
    # they did, through api_import_running(), which this job's status sets; B moved
    # them to admission(), which sees only the coordinator, and only the full import
    # claimed it (4 Oct 2026).
    _coord.claim("import", force=True)
    try:
        if await _gap_fill_window(j, from_ts, to_ts, channels=channels, chunk_days=chunk_days,
                                  max_chunks=max_chunks, pace_s=pace_s, meter_id=meter_id):
            await _gap_fill_finish(j, channels)
    except Exception as e:
        logger.warning("run_gap_fill_job: %s", e, exc_info=e)
        j["status"] = "error"; j["error"] = _error_text("gap fill", e)
    finally:
        _coord.mark("import", status="done")      # every way out: done, cancel, error




async def _gap_fill_window(j, from_ts, to_ts, *, channels, chunk_days, max_chunks, pace_s,
                           meter_id) -> bool:
    """One window [from_ts, to_ts) of a gap fill, newest -> oldest through import_api_history
    (bounded mode), honouring pause / cancel and the rate-limit cool-down, adding to `j`'s
    counts. True when the window is done; False when cancelled or failed (`j` says which).
    The fetch loop of `run_gap_fill_job`, apart so a fill over several runs (heatmap gap fill)
    runs it per run and finishes once."""
    hi = to_ts
    while True:
        ctl = j.get("control")
        if ctl == "cancel":
            j["status"] = "cancelled"
            return False
        if ctl == "pause":
            j["status"] = "paused"
            await asyncio.sleep(1.0)
            continue
        j["status"] = "running"; j["phase"] = "importing"
        res = await import_api_history(
            from_ts, until_ts=hi, channels=tuple(channels),
            chunk_days=chunk_days, max_chunks=max_chunks, pace_s=pace_s,
            restart=False, meter_id=meter_id)
        if not res.get("ok"):
            j["status"] = "error"; j["error"] = res.get("reason") or "failed"
            return False
        frontier = None; rate_limited = False
        for ch, c in (res.get("channels") or {}).items():
            if not isinstance(c, dict):
                continue
            j["written"][ch] = j["written"].get(ch, 0) + (c.get("written") or 0)
            j["no_band"] = j.get("no_band", 0) + (c.get("no_band") or 0)
            o = c.get("oldest")
            if o:
                j["oldest"][ch] = o
                if frontier is None or o < frontier:
                    frontier = o
            if c.get("reason") == "rate_limit":
                rate_limited = True
        if rate_limited:
            j["status"] = "rate_limited"
            await asyncio.sleep(30)          # cool-down, then retry the same frontier
            continue
        # Done when the fetch says so, the frontier reached the floor, or it
        # stopped advancing (no more supplier data in the window).
        if res.get("done") or (not frontier) or frontier <= from_ts:
            break
        hi = frontier                          # continue newest→oldest below

    return True


async def _after_import_settle() -> None:
    """What follows a finished import or gap fill that wrote blocks, once the import claim is
    released (the import job's own finish runs while it holds it): the settle sweep, from the
    start, so the new history gets settle()'s band, legs, ex-VAT and devices (engine step 4d; the
    history reprice sweep that ran before it went in 4e); the cached bills applied, a page at a
    time (`_apply_bills_paced`); then the measured drain, so what has no bill yet is asked for now
    rather than at the next hourly tick. Detached.

    Both ran at the end of the deferred verify pass until engine step 2d removed it (23 Oct 2026).
    The drain kick's reason, from there: a fresh install wrote ~35k blocks and the drain is reached
    only from the hourly branch of `_tick_dispatch_capture`, which had already run once against
    the empty store — up to an hour with the EV/House split absent."""
    try:
        for _ in range(600):                       # the claim, released as the job returns
            if not _coord.running("import"):
                break
            await asyncio.sleep(1.0)
    except Exception as e:
        logger.warning("after import: waiting for the import claim failed (non-fatal): %s", e)
    try:
        await _settle_history_to_done(force=True)       # engine step 4d
    except Exception as e:
        logger.warning("after import: the settle sweep failed (non-fatal): %s", e)
    try:
        await _apply_bills_paced()
    except Exception as e:
        logger.warning("after import: applying the cached bills failed (non-fatal): %s", e)
    try:
        _maybe_drain_measured_history()
    except Exception as e:
        logger.warning("after import: measured history drain schedule failed: %s", e)


_APPLY_PAGE = 200            # bills applied per page by _apply_bills_paced
_APPLY_PAGE_PAUSE_S = 0.05


async def _apply_bills_paced() -> int:
    """`apply_measured_settled` a page at a time, yielding to the loop between pages, so an import's
    cached bills reach its blocks now rather than at the next hourly pass (staging, 23 Oct 2026: a
    re-import's 11,785 waited 52 minutes, read red, and then held the loop 5.6 s in one pass). A
    cursor, not "until nothing is left": a refused bill stays a candidate. Returns those applied."""
    after, n = "", 0
    while True:
        if _store is not None:
            with _store.batch():                 # the page's applies in one commit
                r = apply_measured_settled(after=after, limit=_APPLY_PAGE) or {}
        else:
            r = apply_measured_settled(after=after, limit=_APPLY_PAGE) or {}
        n += r.get("applied") or 0
        if not r.get("last") or (r.get("candidates") or 0) < _APPLY_PAGE:
            return n
        after = r["last"]
        await asyncio.sleep(_APPLY_PAGE_PAUSE_S)


async def _gap_fill_finish(j, channels) -> None:
    """The finish a gap fill shares with the full import (bar the region/period extend, which an
    interior gap does not need): carbon re-arm, the export zero-fill, the device rebuild, and
    `_after_import_settle`. Once per fill, however many runs."""
    # ── Same finish as the full import (bar the region/period extend, which an
    #     interior gap doesn't need): verify pricing, re-arm carbon, rebuild. ──
    j["done"] = True; j["status"] = "finalising"; j["phase"] = "finalising"
    try:
        if _store is not None:
            _store.rearm_carbon_backfill()
    except Exception as _ce:
        logger.warning("run_gap_fill_job: carbon re-arm failed: %s", _ce)
    # Historical EXPORT fill: a slot with no export (a solar meter overnight) is seen to come back
    # from neither feed — Measurements omits it, and the import's REST fallback
    # (import_api_history) writes only the half-hours REST returns — leaving it NULL, which then
    # reads as a permanent, un-fillable export gap. (import_api_history's gap record says the
    # opposite — zero-export nights "come back as zero-value rows". Which holds, and for which
    # feed, is not proved either way: engine review §2.) Fill blank export slots on days that DID
    # get export with 0 (day-scoped, imported-only, billing-neutral).
    if _store is not None and "export" in tuple(channels):
        try:
            _zn = _store.zero_fill_imported_export_blanks(_store.site_timezone())
            if _zn:
                logger.info("run_gap_fill_job: zero-filled %d blank export slot(s) "
                            "on imported export days", _zn)
        except Exception as _ze:
            logger.warning("run_gap_fill_job: export zero-fill failed: %s", _ze)
    # (The repair and the verify pass that ran here went in engine step 2d, 23 Oct 2026: the
    # fill's half-hours keep Octopus's band in the bill cache and settle through the one rule;
    # the sweep and the drain kick the verify ran are `_after_import_settle`'s.)
    _wrote = _run_wrote_blocks(j)
    if _wrote:
        asyncio.create_task(_after_import_settle())
        _g = j.get("gap") or {}
        if _g.get("from") and _g.get("to"):
            # the main meter is back; its devices from what HA recorded (soft delete §3)
            j["devices_rebuilding"] = await rebuild_devices_after_refill(_g["from"], _g["to"])
    j["status"] = "done"; j["phase"] = "done"
    _persist_run_status(j)     # durable summary for reload (poll-on-load)


_GAP_FILL_RECENT_H = 48          # DCC has not settled a younger half-hour: Octopus has nothing yet
_GAP_FILL_CHUNK_DAYS = 60


def gap_bounds(store, ats, *, now=None, meter_id: str = "electricity_main") -> list:
    """The whole gaps the half-hours `ats` sit in (decided 18 Oct 2026, the user: any half-hour
    selected in a gap means the gap). A gap runs from the half-hour after the last block before
    it to the first block after it — or to the last finished half-hour, where no block follows
    yet. A half-hour that has a block is in no gap; one before EMT's oldest block is not a gap
    either (Octopus's history row). Read-only. [[from, to), …] ascending, each gap once."""
    HH = timedelta(minutes=30)
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    done = now.replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0)
    out = []
    for at in ats or []:
        try:
            t = datetime.fromisoformat(str(at)[:19])
        except (TypeError, ValueError):
            continue
        iso = t.isoformat()
        if store.get_block_starts_in_range(meter_id, iso, (t + HH).isoformat(), with_import=True):
            continue
        prev, nxt = store.get_neighbour_block_starts(meter_id, iso, with_import=True)
        if prev is None:
            continue
        a = (datetime.fromisoformat(prev) + HH).isoformat()
        b = nxt or done.isoformat()
        if a < b and [a, b] not in out:
            out.append([a, b])
    return sorted(out)


def gap_fill_plan(store, runs, *, channels=("import", "export"), now=None,
                  meter_id: str = "electricity_main") -> dict:
    """What a heatmap gap fill would do over the selection's runs, counted from the STORE, not
    taken from the client (gap fill design §2). Read-only: `store` may be the request thread's
    reader. Each run [from, to) (naive-UTC half-hour starts) is walked half-hour by half-hour:
    a half-hour with no main-meter block, or one with no import kWh (5 Nov 2026), is missing — deleted where a tombstone covers it,
    too recent inside the last 48 h (DCC has not settled it, so Octopus has nothing yet; decided
    (b): left out), too old beyond Octopus's two years (decided (a): the only bound), else
    fillable. Returns per-run and total counts, the fillable runs, and the size per channel."""
    HH = timedelta(minutes=30)
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    recent = (now - timedelta(hours=_GAP_FILL_RECENT_H)).isoformat()
    old = (now - timedelta(days=_API_RETENTION_DAYS)).isoformat()
    total = {"missing": 0, "deleted": 0, "too_recent": 0, "too_old": 0, "fillable": 0}
    per_run, fill_runs = [], []
    for a, b in runs or []:
        try:
            t, end = datetime.fromisoformat(str(a)), datetime.fromisoformat(str(b))
        except (TypeError, ValueError):
            continue
        have = set(store.get_block_starts_in_range(meter_id, t.isoformat(), end.isoformat(),
                                                   with_import=True))
        tombs = store.tombstoned_slots_in(meter_id, t.isoformat(), end.isoformat())
        c = {"from": t.isoformat(), "to": end.isoformat(), "missing": 0, "deleted": 0,
             "too_recent": 0, "too_old": 0, "fillable": 0}
        cur = None
        while t < end:
            iso = t.isoformat()
            if iso not in have:
                c["missing"] += 1
                if any(s0 <= iso < e0 for s0, e0 in tombs):
                    c["deleted"] += 1
                if iso >= recent:
                    c["too_recent"] += 1
                elif iso < old:
                    c["too_old"] += 1
                else:
                    c["fillable"] += 1
                    nxt = (t + HH).isoformat()
                    if cur and cur[1] == iso:
                        cur[1] = nxt
                    else:
                        cur = [iso, nxt]
                        fill_runs.append(cur)
                    t += HH
                    continue
            cur = None
            t += HH
        per_run.append(c)
        for k in total:
            total[k] += c[k]
    days = sum((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 86400
               for a, b in fill_runs)
    chunks = sum(max(1, -(-int((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds())
                          // (_GAP_FILL_CHUNK_DAYS * 86400))) for a, b in fill_runs)
    return {"runs": per_run, "total": total, "fill_runs": fill_runs,
            "size": {ch: {"half_hours": total["fillable"], "chunks": chunks} for ch in channels},
            "days": round(days, 2)}


async def run_gap_fill_runs(runs, *, channels=("import", "export"), chunk_days: int = 60,
                            max_chunks: int = 6, pace_s: float = 1.5,
                            meter_id: str = "electricity_main") -> None:
    """The heatmap's gap fill (gap fill design §3): several runs [[from, to), …] of missing
    half-hours, newest first, each lifting its own tombstones (a targeted fill) and written
    first-man-wins — then the import's finish ONCE. Tracked in the shared import job state, with
    each run's progress under `j["runs"]`; pause and cancel as the import's. Never raises."""
    j = _api_import_job
    j.clear()
    runs = sorted(([str(a), str(b)] for a, b in runs), key=lambda r: r[0], reverse=True)
    j.update({"status": "running", "phase": "importing", "control": "run",
              "started_at": _dt_now_iso_safe(),
              "written": {"import": 0, "export": 0}, "oldest": {},
              "no_band": 0,
              "reason": None, "error": None, "done": False, "go_live": None,
              "gap": {"from": runs[-1][0] if runs else None, "to": runs[0][1] if runs else None},
              "runs": [{"from": a, "to": b, "status": "waiting", "lifted": 0} for a, b in runs]})
    _coord.claim("import", force=True)
    try:
        for r in j["runs"]:
            r["status"] = "filling"
            try:
                if _store is not None:
                    r["lifted"] = _store.clear_deleted_range(meter_id, r["from"], r["to"]) or 0
            except Exception as _te:
                logger.warning("run_gap_fill_runs: tombstone clear failed (non-fatal): %s", _te)
            ok = await _gap_fill_window(j, r["from"], r["to"], channels=channels,
                                        chunk_days=chunk_days, max_chunks=max_chunks,
                                        pace_s=pace_s, meter_id=meter_id)
            if not ok:
                r["status"] = j.get("status") or "stopped"
                return
            r["status"] = "done"
        await _gap_fill_finish(j, channels)
    except Exception as e:
        logger.warning("run_gap_fill_runs: %s", e, exc_info=e)
        j["status"] = "error"; j["error"] = _error_text("gap fill", e)
    finally:
        _coord.mark("import", status="done")


# The fewest days an import asks at a time: 7 days of half-hours (336) is one page of Octopus's
# measurements (500 a page), so a page that fails loses that page alone.
_IMPORT_CHUNK_MIN_DAYS = 7


def _import_chunk_after(chunk_days: int, res: dict) -> int:
    """The days the import asks at a time after `res`: halved (60 → 30 → 15 → 7) when a channel's
    chunk failed to fetch, else unchanged; kept for the rest of the job.

    5 Nov 2026, a tester's support file (b8 and b9): on one account nearly every measurements page
    timed out at 30 s, and a chunk is fetched as up to six pages, all thrown away when one fails after
    its retries. Each 60-day chunk took 7–23 minutes, then none got through for two hours; the same
    query took 9 minutes, whole, on another account. A smaller chunk loses less to a failure, and each
    one that lands is kept (the checkpoint moves), so the import goes on rather than starting the six
    pages again."""
    failed = any(isinstance(c, dict) and c.get("reason") == "fetch_error"
                 for c in (res.get("channels") or {}).values())
    if not failed or chunk_days <= _IMPORT_CHUNK_MIN_DAYS:
        return chunk_days
    smaller = max(_IMPORT_CHUNK_MIN_DAYS, chunk_days // 2)
    logger.info("import: Octopus did not answer a %d-day chunk — asking %d days at a time from here",
                chunk_days, smaller)
    return smaller


@job("import", exclusive_with=("delete",))
async def run_api_import_job(requested_from=None, *, chunk_days: int = 60,
                             max_chunks: int = 1, pace_s: float = 1.5,
                             headroom_frac: float = 0.25) -> None:
    """Background worker: loop `import_api_history` slices until done, honouring
    the pause/cancel control flag between slices and during rate-limit cooldown.
    Updates `_api_import_job` for the status endpoint. Never raises."""
    j = _api_import_job
    # A cancel that arrived while the start route held the claim (a delete or restore
    # pre-empting it during the backup) must survive the reset below (B §5 acc. 4).
    _cancelled_while_claimed = (j.get("status") == "claimed" and j.get("control") == "cancel")
    j.clear()
    # The import is the blocker four other members declare, so it is the
    # member that makes admission() mean anything. NOTE the coordinator takes
    # a STRICTER view than `api_import_running()`, deliberately: that predicate
    # excludes "finalising", but finalising runs _probe_and_apply_region_periods
    # and a chart regen — writes. A job admitted during finalising is admitted while the import is
    # still rewriting the pricing it reads, which is exactly the dependency
    # attribution exists to respect. v4's predicate is left alone; the
    # coordinator simply has the correct view from the start.
    _coord.claim("import", force=True)
    j.update({"status": "running", "phase": "importing", "control": "run",
              "started_at": _dt_now_iso_safe(),
              "written": {"import": 0, "export": 0}, "oldest": {},
              "no_band": 0,
              "reason": None, "error": None, "done": False, "go_live": None})
    if _cancelled_while_claimed:
        j["control"] = "cancel"
    first = True
    _segs: dict = {}            # the job's rate and standing segments, built once (import_api_history)
    _chunk = chunk_days         # the days asked at a time, halved after a chunk Octopus did not answer
    try:
        while True:
            ctl = j.get("control")
            if ctl == "cancel":
                j["status"] = "cancelled"
                _coord.mark("import", status="done")
                return
            if ctl == "pause":
                j["status"] = "paused"
                await asyncio.sleep(1.0)
                continue
            j["status"] = "running"
            j["phase"] = "importing"
            res = await import_api_history(
                requested_from, chunk_days=_chunk, max_chunks=max_chunks,
                pace_s=pace_s, headroom_frac=headroom_frac, restart=first, segs=_segs)
            first = False
            if not res.get("ok"):
                j["status"] = "error"
                _coord.mark("import", status="done")
                j["error"] = res.get("reason") or "failed"
                return
            j["go_live"] = res.get("go_live")
            rate_limited = False
            for ch, c in (res.get("channels") or {}).items():
                if not isinstance(c, dict):
                    continue
                j["written"][ch] = j["written"].get(ch, 0) + (c.get("written") or 0)
                j["no_band"] = j.get("no_band", 0) + (c.get("no_band") or 0)
                if c.get("oldest"):
                    j["oldest"][ch] = c["oldest"]
                if c.get("reason") == "rate_limit":
                    rate_limited = True
            _chunk = _import_chunk_after(_chunk, res)
            if res.get("done"):
                # Block-writing is finished, but a calm tail (region probe →
                # price recovery → chart rebuild) still runs. Surface it as phases
                # so the UI shows progress instead of a frozen block count.
                j["done"] = True
                j["status"] = "finalising"
                j["phase"] = "finalising"
                # Foundation for historical carbon: record which DNO region applied
                # when, from the account's property history (outward code only).
                # If the user already confirmed + created the site periods BEFORE
                # this import (the pre-import wizard step), the timeline is already
                # correct per period — running the post-import probe again would
                # re-stamp the latest region across the past periods, so skip it
                # (consume the marker) and let the pre-import layout stand.
                try:
                    if _store and _store.get_meta(_PREIMPORT_SITES_MARKER, None):
                        _store.set_meta(_PREIMPORT_SITES_MARKER, None)
                        logger.info("run_api_import_job: pre-import sites already "
                                    "applied — skipping post-import region probe")
                    else:
                        await _probe_and_apply_region_periods()
                except Exception as _e:
                    logger.warning("run_api_import_job: region probe failed: %s", _e)
                try:
                    _finalise_after_import()
                except Exception as _e:
                    logger.warning("run_api_import_job: post-import finalise failed: %s", _e)
                # BL-102: the rule against what Octopus charged; says only
                check_e7_window(_store, _kraken_rate_schedules.get("import"))
                try:
                    _persist_run_status(j)     # durable status for reload (poll-on-load)
                except Exception as _e:
                    logger.warning("run_api_import_job: status persist failed: %s", _e)
                _wrote = _run_wrote_blocks(j)
                if _wrote:
                    asyncio.create_task(_after_import_settle())
                    # the main meter is back; its devices from what HA recorded (soft delete §3:
                    # the gap fill and the CSV import did this, the API import never did). Over
                    # the imported history: the rebuild fills only absent device rows.
                    try:
                        _ih = _store.count_imported_history()
                        if _ih.get("from") and _ih.get("to"):
                            _hi = (datetime.fromisoformat(_ih["to"]) + timedelta(minutes=30)).isoformat()
                            # the half-hours up to the first live one, from HA's recorder, first:
                            # the devices are split over them in the same rebuild (setup gap design)
                            _hi = _setup_gap_hi(await fill_setup_gap_from_ha(), _hi)
                            j["devices_rebuilding"] = await rebuild_devices_after_refill(
                                _ih["from"], _hi, fetch_new=True)
                    except Exception as _e:
                        logger.warning("run_api_import_job: device rebuild not started: %s", _e)
                j["status"] = "done"
                _coord.mark("import", status="done")
                j["phase"] = "done"
                return
            if rate_limited:
                # NOT an end: the loop resumes after the cooldown, so "import" stays
                # claimed (v4's api_import_running() counts rate_limited as running).
                # Releasing here admitted every dependant beside the rest of the run.
                j["status"] = "rate_limited"
                for _ in range(60):     # ~60s cooldown, but stay responsive
                    if j.get("control") in ("cancel", "pause"):
                        break
                    await asyncio.sleep(1.0)
    except Exception as e:
        logger.warning("run_api_import_job: failed: %s", e, exc_info=e)
        j["status"] = "error"
        _coord.mark("import", status="done")
        j["error"] = _error_text("import", e)


async def _maybe_run_settlement_sweep() -> None:
    """Daily-cadenced sweep of unsettled gaps that aged out of the incremental
    poll's sliding window (late/out-of-order DCC settlement, e.g. export lagging
    import by days). Re-fetches oldest-unsettled → now bounded to the horizon, so
    such gaps self-heal without the user clicking 'Retry settlement'. Gated to run
    at most once per _SETTLEMENT_SWEEP_MIN_INTERVAL_S and non-fatal — a failure
    must never break the poll loop."""
    try:
        now = datetime.now(timezone.utc)
        last = _store.get_kraken_state(_STATE_LAST_SWEEP)
        if not _sweep_is_due(last, now, _SETTLEMENT_SWEEP_MIN_INTERVAL_S):
            return
        if _store.count_unsettled_blocks() == 0:
            _store.set_kraken_state(_STATE_LAST_SWEEP, now.isoformat())
            return
        res = await retry_settlement_for_unsettled(
            now=now, max_lookback_days=_SETTLEMENT_SWEEP_HORIZON_DAYS)
        _store.set_kraken_state(_STATE_LAST_SWEEP, now.isoformat())
        if res.get("ok"):
            logger.info("settlement_sweep: settled_import=%d settled_export=%d "
                        "unsettled %s→%s (horizon=%dd)",
                        res.get("settled_import", 0), res.get("settled_export", 0),
                        res.get("unsettled_before"), res.get("unsettled_after"),
                        _SETTLEMENT_SWEEP_HORIZON_DAYS)
        else:
            logger.info("settlement_sweep: skipped (%s)", res.get("reason"))
        # Finalise blocks that aged past the horizon still unsettled — DCC isn't
        # coming, so mark them finalised-from-CAD (distinct from a real
        # settlement; imp_kwh_api stays NULL) so the unsettled count can reach
        # zero and the poll stops reaching back for them. EMT stops asking; it
        # does not call them settled — the heatmap still shows them red, and its
        # range re-fetch recovers one (BL-75 (d)). Reversible: a later settlement
        # clears the flag in the store.
        cutoff = (now.replace(tzinfo=None)
                  - timedelta(days=_SETTLEMENT_SWEEP_HORIZON_DAYS)).isoformat()
        finalised = _store.finalise_past_horizon_blocks(cutoff)
        if finalised:
            logger.info("settlement_sweep: finalised %d past-horizon block(s) "
                        "from CAD (>%dd unsettled, DCC not expected)",
                        finalised, _SETTLEMENT_SWEEP_HORIZON_DAYS)
    except Exception as e:
        logger.warning("settlement_sweep: failed (non-fatal): %s", e)


def _dt_now_iso_safe() -> str:
    try:
        return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    except Exception:
        return ""


def _agreement_priced_ok(block_start: str) -> bool:
    """BL-54 guard: is it safe to (re)price/flip this block from the cached import
    schedule? Safe when the schedule covers history (agreement-stitched, or a single
    agreement) OR the block falls within the current agreement. A pre-current-agreement
    block on a current-tariff-only schedule must NOT be touched — it would be priced at
    the current tariff's rate. Unknown boundary → allow (no regression vs today)."""
    if _import_schedule_covers_history:
        return True
    caf = _kraken_current_agreement_from
    if not caf or not block_start:
        return True
    return str(block_start) >= caf


def _vat_context(channel: str):
    """What a rate-schedule builder takes as `vat` (C rule 2) for `channel`: the
    store's learned VAT calendar for import (the statutory seed always applies, so
    no store means seed only), NO_VAT for export — domestic export carries no VAT."""
    from kraken_rates import NO_VAT
    if channel == "export":
        return NO_VAT
    return _store.get_vat_calendar() if _store is not None else []


def agile_prices(now=None, back_hours: int = 24, ahead_hours: int = 24) -> dict:
    """BL-100 (1 Nov 2026): the half-hourly prices of a dynamic (Agile) tariff, import and export,
    for the Overview's card: {channel: {"tariff", "prices": [[slot, pence]]}} from `back_hours`
    before now to the last published half-hour within `ahead_hours`. From the schedules the poll's
    rate refresh already holds — no Octopus call of its own (import inc VAT, export as published).
    A channel not on a dynamic tariff, or with no schedule, is left out."""
    import tariff_prices as _tp
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    now = now or _dt.now(_tz.utc).replace(tzinfo=None)
    t0 = (now - _td(hours=back_hours)).replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0)
    out = {}
    for ch in ("import", "export"):
        tc = (((_kraken_discovery or {}).get(ch) or {}).get("tariff_code") or "")
        sch = (_kraken_rate_schedules or {}).get(ch)
        if not tc or sch is None or not _tp.is_dynamic_code(tc):
            continue
        prices, t = [], t0
        while t < now + _td(hours=ahead_hours):
            r = sch.resolve(t.isoformat())
            if r is not None:
                prices.append([t.isoformat(), round(float(r), 4)])
            t += _td(minutes=30)
        if prices:
            out[ch] = {"tariff": tc, "prices": prices}
    return out


def _rate_history_from(channel: str) -> "str | None":
    """Where `channel`'s single-tariff rate fetch starts, as Octopus's `period_from` (3 Nov 2026): a
    day before the earlier of the channel's first agreement and the first half-hour stored, so every
    half-hour EMT holds is covered as it was. It fetched the product's whole price history — a beta
    tester's Agile Outgoing from 2018, 137,588 half-hours, ~92 requests at every refresh. None (the
    whole history, as before) where neither is known."""
    starts = [vf for _tc, vf, _vt in _agreement_windows(channel) if vf]
    try:
        first, _last = _reader().block_span("electricity_main")
    except Exception:
        first = None
    if first:
        starts.append(first)
    if not starts:
        return None
    try:
        since = datetime.fromisoformat(min(starts)[:19]) - timedelta(days=1)
    except ValueError:
        return None
    return since.strftime("%Y-%m-%dT%H:%M:%SZ")


async def _refresh_kraken_rate_schedules() -> None:
    """Build/refresh the cached rate schedules for import and export from the
    discovered tariffs. Cheap (fixed tariffs ≈ 1 record). Failures leave the
    previous cache intact; an empty cache simply means the resolver returns
    None and zero-rate blocks stay flagged for the tooling.
    """
    global _kraken_rate_schedules, _kraken_standing_schedule, _rate_schedule_unsupported
    if _kraken_client is None or not _kraken_discovery:
        return
    try:
        from kraken_rates import (build_rate_schedule,
                                  build_standing_charge_schedule, RateFetchError)
    except Exception as e:
        logger.warning("_refresh_kraken_rate_schedules: import failed: %s", e)
        return
    new_cache: dict = {}
    unsupported: dict | None = None
    # Did we get a DEFINITIVE (non-error) result for the import channel this
    # cycle? A transient fetch failure must not raise — or clear — the banner.
    import_determined = False
    for ch in ("import", "export"):
        info = _kraken_discovery.get(ch) or {}
        product = info.get("product_code")
        tariff = info.get("tariff_code")
        if not product or not tariff:
            continue
        try:
            sched = await build_rate_schedule(
                _kraken_client, product, tariff, vat=_vat_context(ch),
                period_from=_rate_history_from(ch), raise_on_error=True)
        except RateFetchError as e:
            # The fetch FAILED (transport / HTTP / edge 403) — this is NOT an
            # unsupported tariff. Keep the last-known schedule and the previous
            # unsupported state untouched, so an API blip can't flip the
            # "tariff unsupported" banner (or a stale banner can't clear wrongly).
            logger.warning("_refresh_kraken_rate_schedules: %s rate fetch failed "
                           "transiently (%s); keeping last-known schedule", ch, e)
            continue
        except Exception as e:
            logger.warning("_refresh_kraken_rate_schedules: %s build failed: %s",
                           ch, e)
            continue
        if ch == "import":
            import_determined = True
        if not sched.is_empty():
            new_cache[ch] = sched
            _keep_current_tariff_prices(ch, tariff, "rate", sched)
        elif ch == "import":
            # Fetch SUCCEEDED but the tariff genuinely returns no standard AND no
            # day/night rates — the signature of the new time-of-use Intelligent
            # Octopus tariff (IOG-SMB-TOU — the 6-hour charge cap), which drops
            # `standard-unit-rates` for `day/night/ev_device_*` links EMT does not
            # yet read. FAIL LOUD — do not silently price at £0.
            unsupported = {"tariff": tariff, "product": product}
            logger.error(
                "_refresh_kraken_rate_schedules: import tariff %s returned NO "
                "standard unit rates. This is the new IOG time-of-use / 6-hour-"
                "cap tariff (day/night/ev_device rates), which EMT does NOT yet "
                "support. Rates are unavailable and billing for this meter will "
                "be incorrect until support lands. See issue #1708.", tariff)
    # Only update the banner when we actually determined the import status this
    # cycle. A transient failure leaves the previous flag exactly as it was.
    if import_determined:
        _rate_schedule_unsupported = unsupported

    # BL-54: stitch the import schedule across ALL agreements so a pre-migration block
    # prices on the tariff that applied on its date, not the current one. Records the
    # current agreement's valid_from and whether history is covered (for the guard).
    global _import_schedule_covers_history, _kraken_current_agreement_from
    _imp_info = _agreement_info("import")     # the stored agreements (G1 design §3)
    _ags = _imp_info.get("agreements") or []
    _kraken_current_agreement_from = None
    try:
        from kraken_api_client import KrakenAPIClient as _KC
        from kraken_ingester import normalise_to_naive_utc as _nn
        _cur = _KC._current_agreement(_ags)
        if _cur and _cur.get("valid_from"):
            _kraken_current_agreement_from = _nn(_cur["valid_from"])
    except Exception:
        pass
    _import_schedule_covers_history = (len(_ags) <= 1)
    if len(_ags) > 1:
        try:
            from kraken_rates import build_agreement_stitched_schedule
            _stitched = await build_agreement_stitched_schedule(
                _kraken_client, _ags, vat=_vat_context("import"), on_built=_keep_tariff_prices)
            if not _stitched.is_empty():
                new_cache["import"] = _stitched
                _import_schedule_covers_history = True
                logger.info("_refresh_kraken_rate_schedules: import stitched across %d "
                            "agreement(s) → %d periods", len(_ags), len(_stitched))
            else:
                logger.warning("_refresh_kraken_rate_schedules: agreement stitch empty "
                               "— keeping single-tariff import; pre-migration blocks "
                               "guarded from reprice")
        except Exception as e:
            logger.warning("_refresh_kraken_rate_schedules: agreement stitch failed (%s) "
                           "— keeping single-tariff import; pre-migration blocks "
                           "guarded from reprice", e)

    # IOG-SMB-TOU EV-device buckets (6-hour cap). Additive: only the capped tariff
    # exposes these, so gate on the IOG signature and keep only non-empty results;
    # any failure is non-fatal (the cap classifier then falls back to the general
    # overlay). The cap prices the EV portion of a dispatched slot from these.
    _imp = _kraken_discovery.get("import") or {}
    _imp_tariff = _imp.get("tariff_code") or ""
    # Stitched across the IOG-SMB agreements discovery holds, whatever the current tariff, so a
    # capped half-hour takes its own agreement's EV rates after a renewal or after leaving
    # IOG-SMB (capped pricing by agreement design §5 (c)). Where it holds none, the current
    # product's, as before.
    _smb_ags = [a for a in _ags if "IOG-SMB" in (a.get("tariff_code") or "").upper()]
    if _smb_ags:
        try:
            from kraken_rates import build_ev_device_stitched
            _ev_off, _ev_peak = await build_ev_device_stitched(
                _kraken_client, _smb_ags, vat=_vat_context("import"))
            if not _ev_off.is_empty():
                new_cache["ev_device_off_peak"] = _ev_off
            if not _ev_peak.is_empty():
                new_cache["ev_device_peak"] = _ev_peak
        except Exception as e:
            logger.warning("_refresh_kraken_rate_schedules: ev_device stitch failed: %s", e)
    elif _imp.get("product_code") and "IOG" in _imp_tariff.upper():
        try:
            from kraken_rates import build_ev_device_schedules
            _ev_off, _ev_peak = await build_ev_device_schedules(
                _kraken_client, _imp["product_code"], _imp_tariff,
                vat=_vat_context("import"), period_from=_rate_history_from("import"))
            if not _ev_off.is_empty():
                new_cache["ev_device_off_peak"] = _ev_off
            if not _ev_peak.is_empty():
                new_cache["ev_device_peak"] = _ev_peak
        except Exception as e:
            logger.warning("_refresh_kraken_rate_schedules: ev_device build failed: %s", e)

    if new_cache:
        _kraken_rate_schedules = new_cache
        logger.info("_refresh_kraken_rate_schedules: import=%d export=%d "
                    "ev_off=%d ev_peak=%d periods",
                    len(_kraken_rate_schedules.get("import", []) or []),
                    len(_kraken_rate_schedules.get("export", []) or []),
                    len(_kraken_rate_schedules.get("ev_device_off_peak", []) or []),
                    len(_kraken_rate_schedules.get("ev_device_peak", []) or []))
        if _learn_vat_from_import_schedule():
            # A change seen this cycle starts today: restate the VAT-bearing
            # schedules on the calendar that now has it.
            _cal = _vat_context("import")
            for _k in ("import", "ev_device_off_peak", "ev_device_peak"):
                if _k in _kraken_rate_schedules:
                    _kraken_rate_schedules[_k] = _kraken_rate_schedules[_k].with_vat(_cal)

    # Standing charge comes from the import tariff.
    imp = _kraken_discovery.get("import") or {}
    if imp.get("product_code") and imp.get("tariff_code"):
        try:
            sc = await build_standing_charge_schedule(
                _kraken_client, imp["product_code"], imp["tariff_code"],
                vat=_vat_context("import"), period_from=_rate_history_from("import"))
            if not sc.is_empty():
                _kraken_standing_schedule = sc
                _keep_current_tariff_prices("import", imp["tariff_code"], "standing", sc)
                logger.info("_refresh_kraken_rate_schedules: standing_charge=%d periods",
                            len(sc))
        except Exception as e:
            logger.warning("_refresh_kraken_rate_schedules: standing build failed: %s", e)


def _learn_vat_from_import_schedule(now_utc: str | None = None) -> bool:
    """Update the persisted VAT calendar from the import tariff's PUBLISHED inc/exc
    rates; True when it changed.

    `vat_periods()` reads each period's published VAT; vat_calendar.learn_from_records
    dates them — a closed period at its start, a running one no earlier than today,
    because Octopus edits a running record in place (1 Oct 2026: a 5 Jul record turned
    0%, which dated by its start backdated the 0% to July). Merged into the stored
    calendar. Feeds the schedules' VAT (RateSchedule.with_vat) and the fallback rate.
    No-op without a store or an exc sibling; idempotent."""
    if _store is None:
        return False
    sched = _kraken_rate_schedules.get("import")
    if sched is None or not hasattr(sched, "vat_periods"):
        return False
    try:
        periods = sched.vat_periods()
        if not periods:
            return False
        import vat_calendar as _vc
        now = now_utc or datetime.utcnow().replace(microsecond=0).isoformat()
        existing = _store.get_vat_calendar()
        merged = _vc.merge_learned(existing, _vc.learn_from_records(periods, now, existing))
        if merged != existing:
            _store.set_vat_calendar(merged)
            logger.info("vat calendar: learned from tariff → %s", merged)
            return True
    except Exception as e:
        logger.debug("_learn_vat_from_import_schedule: skipped (%s)", e)
    return False


# Smart-charge source vocabulary across API versions: legacy meta.source =
# 'smart-charge', flex type = 'smart'. (Bump/boost are excluded — see filter.)
_SMART_CHARGE_SOURCES = {"smart-charge", "smart"}


def _smart_charge_slots(planned: list) -> set:
    """30-min slots (naive-UTC ISO) covered by planned dispatches whose
    source is a smart-charge dispatch. This is the started/smart gate: bump-charge
    (boost) and unknown/null sources are EXCLUDED — only genuine smart-charge
    dispatches are off-peak candidates. 'Any active minute → whole slot.'

    Vocabulary: the legacy `plannedDispatches.meta.source` says 'smart-charge';
    the newer `flexPlannedDispatches.type` says 'smart'. Match BOTH so detection
    is correct whichever the API returns (mirrors BottlecapDave's
    INTELLIGENT_SOURCE_SMART_CHARGE_OPTIONS).
    """
    smart = [d for d in (planned or [])
             if str(d.get("source") or "").lower() in _SMART_CHARGE_SOURCES]
    return _planned_dispatch_slots_preview(smart)


# ── OHME charge-mode interpretation + capture (verified-gating path) ──────────
# OHME is structurally unlike Zappi: Octopus does not control the charge, so its
# planned-dispatch `source` labels are unreliable (the planned set is a superset
# wiped ~17:00, present even when unplugged; completed dispatches report
# source=null). The Zappi gate `source=='smart-charge'` would therefore capture
# almost nothing for OHME and badly under-bill. OHME gets its OWN capture-feeding
# rule — the overlay, meter-draw validation and storage stay the shared path.
# Three ways:
#   • no charge-mode sensor → OPTIMISTIC: treat every planned-superset slot as an
#     off-peak candidate (source ohme_assumed_unverified); the overlay's meter
#     validation narrows it to slots that actually drew. Bounded to
#     under-application (a boost in a drawn slot is the accepted residual).
#   • charge-mode sensor    → VERIFIED: the sensor is real-time charge STATE,
#     independent of Octopus's planned data, so it both vetoes boosts (Max charge
#     stays peak) AND catches smart charges the superset missed. Sensor-driven,
#     per-tick: snap NOW to its 30-min slot and capture it iff the sensor reports
#     a smart slot active. 5-min ticks sample each 30-min slot ~6× so a running
#     smart charge is caught.
_OHME_PROVIDER_TOKEN = "OHME"


def _is_ohme_provider(provider) -> bool:
    return _OHME_PROVIDER_TOKEN in str(provider or "").upper()


def _ohme_interpret_mode(integration, state) -> str:
    """Map a live OHME charge-mode entity state to 'smart' | 'boost' | 'idle'.

    official select: state is the underscore slug — 'smart_charge'→smart,
                     'max_charge'→boost, else idle. Space/underscore are
                     normalised so the display form 'Smart charge' also resolves
                     (#286: the raw state is 'smart_charge', so matching only
                     'smart charge' left every tick idle → nothing off-peak).
    dan-r binary:    'on'→smart, else idle. dan-r cannot report boost directly,
                     so boost is left to inference-by-absence (an out-of-window
                     drawn slot with the binary off is never captured → stays
                     peak), per the agreed dan-r asymmetry.
    """
    sl = str(state or "").strip().lower().replace(" ", "_")
    if integration == "official":
        if sl == "max_charge":
            return "boost"
        if sl == "smart_charge":
            return "smart"
        return "idle"
    if integration == "danr":
        return "smart" if sl in ("on", "true", "active") else "idle"
    return "idle"


def _ohme_status_charging(status) -> bool:
    """True if the official OHME Status sensor reports the charger actively
    charging (ChargerStatus 'charging'). Any other state — paused, plugged_in,
    finished, unplugged — is not an active draw."""
    return str(status or "").strip().lower() == "charging"


def _ohme_slot_for_now(now) -> str:
    """Snap a naive-UTC datetime to its 30-min slot start (naive-UTC ISO),
    matching _planned_dispatch_slots_preview's slot keys."""
    slot = now.replace(minute=(0 if now.minute < 30 else 30),
                       second=0, microsecond=0)
    return slot.isoformat()


def _parse_ohme_slots(raw, now_utc, tz_name: str) -> list:
    """Parse Ohme's OWN charge-slot plan into naive-UTC 30-min slot_start ISO strings.

    Tested end-to-end by Ohme owners, who report it working (22 Oct 2026, the user; the author is
    on Zappi), and unit-tested here. Accepts either Ohme HA integration shape, defensively:

      * OFFICIAL `sensor.<ohme>_slot_list` STATE — a string like ``"01:00-02:30, 03:30-04:00"``
        in **local** time (merged half-hour slots; ohmepy ``ChargeSlot.__str__`` is
        ``f"{start:%H:%M}-{end:%H:%M}"``). HH:MM carries no date, so each range is anchored to
        the local date that places its start nearest ``now`` (within ±12h); a range whose end
        ``<=`` start crosses midnight (+1 day). Shape confirmed against home-assistant/core
        ``ohme/sensor.py`` + ohmepy ``utils.ChargeSlot``.
      * dan-r attribute shape — a list of dicts carrying ISO ``start``/``end`` (ohmepy
        ``ChargeSlot.to_dict``). Unambiguous, no anchoring; authoritative for pricing.

    Returns sorted unique naive-UTC 30-min slot_start ISO strings the ranges cover.
    Best-effort: returns [] on anything unparseable (never raises).
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz, time as _time
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = _tz.utc

    def _expand(start_naive, end_naive):
        out = []
        cur = start_naive.replace(second=0, microsecond=0)
        cur = cur.replace(minute=(0 if cur.minute < 30 else 30))
        while cur < end_naive:
            out.append(cur.isoformat())
            cur = cur + _td(minutes=30)
        return out

    slots = []
    # dan-r attribute shape: list of {'start','end'} ISO dicts (authoritative)
    if isinstance(raw, (list, tuple)):
        for d in raw:
            try:
                sd = _dt.fromisoformat(str(d.get("start")))
                ed = _dt.fromisoformat(str(d.get("end")))
                if sd.tzinfo is not None:
                    sd = sd.astimezone(_tz.utc).replace(tzinfo=None)
                if ed.tzinfo is not None:
                    ed = ed.astimezone(_tz.utc).replace(tzinfo=None)
                if ed > sd:
                    slots += _expand(sd, ed)
            except Exception:
                continue
        return sorted(set(slots))

    # official HH:MM string shape (local time; anchor + convert)
    txt = str(raw or "").strip()
    if not txt or txt.lower() in ("unknown", "unavailable", "none", "-"):
        return []
    try:
        now_local = now_utc.replace(tzinfo=_tz.utc).astimezone(tz)
    except Exception:
        return []
    for part in txt.split(","):
        part = part.strip()
        if "-" not in part:
            continue
        a, _, b = part.partition("-")
        try:
            ah, am = (int(x) for x in a.strip().split(":")[:2])
            bh, bm = (int(x) for x in b.strip().split(":")[:2])
        except Exception:
            continue
        try:
            start_local = _dt.combine(now_local.date(), _time(ah, am), tzinfo=tz)
            end_local = _dt.combine(now_local.date(), _time(bh, bm), tzinfo=tz)
        except Exception:
            continue
        if end_local <= start_local:
            end_local += _td(days=1)
        # shift by whole days so the slot start sits nearest now (±12h window)
        guard = 0
        while start_local < now_local - _td(hours=12) and guard < 3:
            start_local += _td(days=1); end_local += _td(days=1); guard += 1
        guard = 0
        while start_local >= now_local + _td(hours=12) and guard < 3:
            start_local -= _td(days=1); end_local -= _td(days=1); guard += 1
        su = start_local.astimezone(_tz.utc).replace(tzinfo=None)
        eu = end_local.astimezone(_tz.utc).replace(tzinfo=None)
        slots += _expand(su, eu)
    return sorted(set(slots))


def _ohme_capture_slots(provider, planned, sensor_present, mode, status, now):
    """Decide which dispatch slots OHME should persist this tick. PURE — all I/O
    (sensor read, persistence, logging) stays in the caller.

    `status` is the raw OHME Status-sensor state (or None when no Status sensor is
    available). Returns a list of (slot_start_iso, source) pairs, or None when the
    provider is NOT OHME (signalling the caller to use the default path).
    """
    if not _is_ohme_provider(provider):
        return None
    if not sensor_present:
        # Optimistic: every planned-superset slot is an off-peak candidate.
        return [(s, "ohme_assumed_unverified")
                for s in sorted(_planned_dispatch_slots_preview(planned))]
    # Verified. Boost (Max charge) is never off-peak → veto; idle → nothing.
    if mode != "smart":
        return []
    # When a real Status sensor is present, require the charger to be ACTIVELY
    # charging this tick. This is precise (no capturing plugged-in-but-idle slots)
    # AND catches replanned/"additional" slots by construction — a drawn slot is
    # captured whenever status=charging, regardless of Octopus's planned data
    # (#286). With no Status sensor, fall back to mode-only; the overlay's
    # meter-draw validation then narrows to slots that actually drew.
    if status is not None and not _ohme_status_charging(status):
        return []
    return [(_ohme_slot_for_now(now), "ohme_verified")]


def _capture_ohme_slots(provider, planned) -> int:
    """OHME branch of _capture_dispatch_slots (the I/O side). Reads the detected
    charge-mode sensor (if any), decides slots via the pure _ohme_capture_slots
    helper, persists them, and logs richly — including the planned-source
    distribution — so an OHME user's log fully explains every decision. That log
    is the diagnostic backstop for a path we cannot validate on a Zappi account.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ohme = (_detected_integrations or {}).get("ohme") or {}
    entity = ohme.get("charge_mode_entity") if ohme.get("found") else None
    sensor_present = bool(entity)
    mode = None
    raw = None
    status_raw = None
    if sensor_present and _engine_ha is not None:
        try:
            raw = _engine_ha.get_state(entity)
        except Exception as e:
            logger.warning("_capture_ohme_slots: sensor read failed (%s): %s",
                           entity, e)
            raw = None
        mode = _ohme_interpret_mode(ohme.get("integration"), raw)
        # Official Status sensor: the real charging-state gate (#286). Absent on
        # dan-r / older setups → status_raw stays None → mode-only fallback.
        status_entity = ohme.get("status_entity")
        if status_entity:
            try:
                status_raw = _engine_ha.get_state(status_entity)
            except Exception as e:
                logger.warning("_capture_ohme_slots: status read failed (%s): %s",
                               status_entity, e)
                status_raw = None

    pairs = _ohme_capture_slots(provider, planned, sensor_present, mode,
                                status_raw, now)
    if pairs is None:        # not OHME — caller already gated; defensive
        return 0

    # Diagnostic backstop: the ACTUAL planned-source distribution OHME reports,
    # logged every capture so the first OHME log reveals what their dispatches
    # really carry (smart-charge / bump-charge / unknown / null).
    try:
        from collections import Counter
        dist = dict(Counter(str(d.get("source")).lower() for d in (planned or [])))
    except Exception:
        dist = {}

    if not sensor_present:
        decision = "optimistic (no charge-mode sensor; all planned slots)"
    elif mode == "boost":
        decision = "verified-boost (veto — stays peak)"
    elif mode != "smart":
        decision = "verified-idle (mode not smart)"
    elif status_raw is not None and not _ohme_status_charging(status_raw):
        decision = "verified-smart but status not charging (skip)"
    else:
        decision = "verified-smart charging (capture current slot)"

    with _store.batch():          # one commit for the poll (see _capture_dispatch_slots)
        captured = 0
        for slot_start, source in pairs:
            try:
                _store.upsert_dispatch_slot(
                    slot_start, off_peak=True, provider=provider, source=source,
                    state="planned")
                captured += 1
                # 4.5.0 (BL-31; reported working by Ohme owners): a sensor-VERIFIED smart slot is a real
                # smart-charge-in-progress → record 'started' so the reconcile confirms
                # off-peak instead of flagging it 'review' (the OHME review-churn). The
                # optimistic (no-sensor) source is NOT started — it stays review-eligible.
                if source == "ohme_verified":
                    try:
                        _store.record_dispatch_history(
                            slot_start, "started", provider=provider,
                            source="ohme", energy_kwh=None)
                    except Exception:
                        pass
            except Exception as e:
                logger.warning("_capture_ohme_slots: persist %s failed: %s",
                               slot_start, e)
    logger.info(
        "_capture_ohme_slots: provider=%s integration=%s mode=%s raw_mode=%r "
        "status=%r %s — captured %d slot(s) planned=%d source_dist=%s",
        provider, ohme.get("integration") or "none", mode or "n/a", raw,
        status_raw, decision, captured, len(planned or []), dist)
    # Retention invariant (4.5.0, BL-35): NEVER trim dispatch_slots / dispatch_history.
    # The planned/started/completed lifecycle is the canonical reprice ingredient —
    # EMT keeps it for the life of the DB (no Octopus-style 90-day amnesia).
    return captured


def _record_ohme_dispatch_history(provider, planned, completed) -> int:
    """BL-10: accumulate OHME planned + completed dispatches into dispatch_history.

    The main _capture_dispatch_slots path records this for Kraken-controlled
    providers, but OHME returns early (its dispatch_slots billing capture is
    sensor-gated). The smart-charging card reads dispatch_history and colours each
    slot by its BILLED RATE — not by the smart-vs-boost signal — so OHME's
    unreliable source labels don't matter here; we retain every planned slot
    (`include_all=True`) and every completed dispatch. OBSERVE-ONLY: this never
    touches dispatch_slots or billing. Returns rows written.
    """
    if _store is None:
        return 0
    n = 0
    # 4.5.0 (BL-31; reported working by Ohme owners, 22 Oct 2026 — the author is on Zappi): prefer
    # Ohme's OWN plan (authoritative — not Octopus's unreliable superset) for 'planned'.
    # This is what lets the smart-charging card show the real forward plan for a
    # charger-connected Ohme user, and gives the reconcile a real plan to key on.
    # Guarded: only when the slot_list sensor is present AND parses to non-empty;
    # otherwise the pre-existing superset behaviour is unchanged.
    own_slots = []
    try:
        _ohme = (_detected_integrations or {}).get("ohme") or {}
        _sl_ent = _ohme.get("slot_list_entity")
        if _sl_ent and _engine_ha is not None:
            _raw = _engine_ha.get_state(_sl_ent)
            if _raw in (None, "", "unknown", "unavailable"):
                _attrs = _engine_ha.get_attributes(_sl_ent) or {}
                _raw = (_attrs.get("slots") or _attrs.get("charge_slots")
                        or _attrs.get("planned_dispatches") or _raw)
            _now2 = datetime.now(timezone.utc).replace(tzinfo=None)
            own_slots = _parse_ohme_slots(_raw, _now2, _iog_site_tz())
    except Exception as _oe:
        logger.debug("_record_ohme_dispatch_history: own-plan read failed: %s", _oe)
    # One commit for the poll; completed rows written only when they change
    # (see _capture_dispatch_slots, 5 Oct 2026).
    with _store.batch():
        if own_slots:
            for slot_start in own_slots:
                _store.record_dispatch_history(
                    slot_start, "planned", provider=provider, source="ohme_plan",
                    energy_kwh=None)
                n += 1
            logger.info("_record_ohme_dispatch_history: using Ohme's OWN plan "
                        "(%d slot(s)) for card + lifecycle", len(own_slots))
        else:
            slot_energy = _planned_dispatch_slot_energy(planned, include_all=True)
            slot_bounds = _planned_dispatch_slot_bounds(planned, include_all=True)
            for slot_start in sorted(slot_energy):
                rb = slot_bounds.get(slot_start) or (None, None)
                _store.record_dispatch_history(
                    slot_start, "planned", provider=provider, source="ohme",
                    energy_kwh=slot_energy.get(slot_start), raw_start=rb[0], raw_end=rb[1])
                n += 1
        comp_bounds = _completed_dispatch_slot_bounds(completed)  # actual window
        for slot_start, e_comp in _completed_dispatch_slot_energy(completed).items():
            rb = comp_bounds.get(slot_start) or (None, None)
            _store.record_dispatch_history(
                slot_start, "completed", provider=provider, source="unknown",
                energy_kwh=e_comp, raw_start=rb[0], raw_end=rb[1], refresh_seen=False)
            n += 1
    return n


# The slots the latest capture saw completed, for the carve that follows it (_tick_dispatch_capture).
_capture_completed_slots: set = set()


async def _capture_dispatch_slots() -> int:
    """STEP 2 (piece 1): persist smart-charge dispatch slots to dispatch_slots.

    Captures forward each poll. NON-BILLING: this only records which slots had a
    smart-charge dispatch; it does NOT change any rate. The overlay resolver +
    meter-draw validation (next piece) decides whether a recorded slot actually
    gets the off-peak rate. Returns the number of slots captured this poll.

    Provider handling: the DEFAULT path is capability-based (design §1c) — we
    persist smart-charge slots for ANY provider (incl. MYENERGI_V2) gated by the
    source filter, not a provider allowlist. OHME is the one exception: its
    source labels are unreliable, so it branches to _capture_ohme_slots (which
    captures optimistically, or sensor-verified when a charge-mode signal is
    present). See that function for the rationale.
    """
    if _kraken_client is None or not _kraken_discovery or _store is None:
        return 0
    try:
        disp = await _kraken_client.get_dispatches(_kraken_account_number)
    except Exception as e:
        logger.warning("_capture_dispatch_slots: fetch failed: %s", e)
        return 0
    if not disp:
        return 0
    provider = disp.get("provider")
    planned = disp.get("planned") or []

    # OHME has its own capture-feeding rule (see _capture_ohme_slots): the Zappi
    # source=='smart-charge' gate would under-capture OHME badly.
    if _is_ohme_provider(provider):
        captured = _capture_ohme_slots(provider, planned)
        # BL-10: also accumulate observe-only dispatch_history so the smart-
        # charging card covers OHME (the billing dispatch_slots path is untouched).
        try:
            _record_ohme_dispatch_history(provider, planned, disp.get("completed") or [])
        except Exception as e:
            logger.warning("_capture_dispatch_slots: ohme history accumulation "
                           "failed: %s", e)
        return captured

    slots = _smart_charge_slots(planned)
    # NB: do NOT early-return when planned is empty. Completed dispatches land
    # AFTER the charge, by which point flexPlannedDispatches is already empty
    # (planned=0) — an early return here skipped the completed capture entirely.
    slot_energy = _planned_dispatch_slot_energy(planned) if slots else {}
    slot_bounds = _planned_dispatch_slot_bounds(planned) if slots else {}  # BL-11
    # One transaction for this poll's writes, and none at all for a slot re-seen
    # unchanged (5 Oct 2026). Octopus re-serves recent COMPLETED dispatches on every
    # 5-minute poll; each re-write was its own commit — about ten fsyncs a poll on the
    # SD card, stalling the engine loop for seconds at :04/:34. No await inside: the
    # batch holds a write transaction. The `started` derivation below awaits Octopus,
    # so it writes on its own.
    with _store.batch():
        captured = 0
        for slot_start in sorted(slots):
            _rb = slot_bounds.get(slot_start) or (None, None)
            try:
                _store.upsert_dispatch_slot(
                    slot_start, off_peak=True, provider=provider,
                    source="smart-charge", state="planned",
                    energy_planned=slot_energy.get(slot_start),
                    raw_start=_rb[0], raw_end=_rb[1])
                captured += 1
            except Exception as e:
                logger.warning("_capture_dispatch_slots: persist %s failed: %s",
                               slot_start, e)
        if captured:
            logger.info("_capture_dispatch_slots: persisted %d smart-charge slot(s) "
                        "(provider=%s)", captured, provider)

        # Record COMPLETED dispatch energy per slot (observe-only). Completed
        # dispatches land hours after the charge, so this is the settlement-time
        # signal the #253 validation will veto against — NOT used for billing yet.
        # OHME already returned above (its completed dispatches carry no reliable
        # smart-vs-boost signal), so this covers only Octopus-controlled providers.
        # We only ANNOTATE slots that already exist (were captured as planned) — we
        # never create a new off_peak slot from a completed dispatch, so this can't
        # change what the overlay prices. off_peak / provider / source are preserved.
        completed = disp.get("completed") or []
        if completed:
            comp = _completed_dispatch_slot_energy(completed)
            _comp_bounds_bill = _completed_dispatch_slot_bounds(completed)
            n_comp = 0
            n_new = 0
            for slot_start, e_comp in comp.items():
                if e_comp is None:
                    continue
                _capture_completed_slots.add(slot_start)
                existing = _store.get_dispatch_slot(slot_start)
                try:
                    if existing:
                        # Annotate an already-captured (planned) slot with completed
                        # energy — off_peak/provider/source preserved.
                        _store.upsert_dispatch_slot(
                            slot_start,
                            off_peak=bool(existing.get("off_peak")),
                            provider=existing.get("provider") or provider,
                            source=existing.get("source") or "smart-charge",
                            state="completed", energy_completed=e_comp)
                        n_comp += 1
                    else:
                        # No 'planned' slot was ever captured (EMT was offline for it, or
                        # this is a historical re-import). A COMPLETED Octopus dispatch is
                        # the supplier's authoritative smart-charge record, so create an
                        # off-peak slot from it — marked source='smart-charge-completed'
                        # so the settlement reconcile reprices it off-peak with an audit
                        # trail (reconciled-from-completed, not live-verified). The overlay
                        # still gates on real meter draw, so a phantom slot never credits.
                        _cb = _comp_bounds_bill.get(slot_start) or (None, None)
                        _store.upsert_dispatch_slot(
                            slot_start, off_peak=True, provider=provider,
                            source="smart-charge-completed", state="completed",
                            energy_completed=e_comp, raw_start=_cb[0], raw_end=_cb[1])
                        n_new += 1
                except Exception as e:
                    logger.warning("_capture_dispatch_slots: completed persist %s "
                                   "failed: %s", slot_start, e)
            if n_comp or n_new:
                logger.info("_capture_dispatch_slots: completed energy on %d slot(s); "
                            "created %d off-peak slot(s) from completed-only dispatch(es)",
                            n_comp, n_new)

        # Accumulate ALL planned + completed dispatches into dispatch_history
        # (observe-only, design §11) — a persistent record across polls, separate
        # from the billing dispatch_slots. This retains completed dispatches for
        # slots we never planned (the tail slots the per-poll snapshot loses) and is
        # what makes a future "never saw this slot" judgement trustworthy. Never read
        # for billing.
        try:
            for slot_start in sorted(slots):
                _rb = slot_bounds.get(slot_start) or (None, None)
                _store.record_dispatch_history(
                    slot_start, "planned", provider=provider,
                    source="smart-charge", energy_kwh=slot_energy.get(slot_start),
                    raw_start=_rb[0], raw_end=_rb[1])
            _comp_bounds = _completed_dispatch_slot_bounds(completed)  # actual window
            for slot_start, e_comp in _completed_dispatch_slot_energy(completed).items():
                _cb = _comp_bounds.get(slot_start) or (None, None)
                _store.record_dispatch_history(
                    slot_start, "completed", provider=provider,
                    source="unknown", energy_kwh=e_comp,
                    raw_start=_cb[0], raw_end=_cb[1], refresh_seen=False)
        except Exception as e:
            logger.warning("_capture_dispatch_slots: history accumulation failed: %s", e)

    # Derive `started` for the current slot (design §11.2) — the smart-vs-bump
    # discriminator. Only fires when the intelligent state is
    # SMART_CONTROL_IN_PROGRESS AND a planned dispatch is active now; a bump never
    # enters that state, so it never starts. Best-effort + observe-only: the
    # state fetch is isolated (None on any error → no started slot), and started
    # is recorded only into dispatch_history, never the billing dispatch_slots.
    try:
        state = await _kraken_client.get_intelligent_state(_kraken_account_number)
        if state:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            for slot_start in _derive_started_slots(now, planned, state):
                _store.record_dispatch_history(
                    slot_start, "started", provider=provider,
                    source="smart-charge", energy_kwh=None)
                logger.info("_capture_dispatch_slots: slot %s STARTED "
                            "(intelligent_state=%s)", slot_start, state)
    except Exception as e:
        logger.warning("_capture_dispatch_slots: started derivation failed: %s", e)

    # Retention invariant (4.5.0, BL-35): NEVER trim dispatch_slots / dispatch_history —
    # the planned/started/completed lifecycle is the canonical reprice ingredient,
    # kept for the life of the DB (no Octopus-style 90-day amnesia).
    return captured


_dispatch_skip_said = False     # the not-Intelligent skip, logged once


async def _tick_dispatch_capture() -> None:
    """5-minute-throttled dispatch-slot capture. Called from _engine_tick (every
    ~10s) but only fetches every 5 minutes.

    Rationale (confirmed against live data + BCD): a smart-charge dispatch only
    carries source='smart-charge' while it is PLANNED/upcoming; once actioned it
    moves to completed with source='unknown', losing the smart signal. The
    6-hour DCC poll is far too coarse to catch a short daytime dispatch while
    still planned. BCD refreshes ~every 60s; we use 5 min — frequent enough to
    catch the planned state with margin (Octopus schedules ahead), light enough
    for an add-on also doing DCC polls + CI ticks. Captured slots persist in
    dispatch_slots for the overlay to price later (gated by meter draw).
    """
    global _last_dispatch_capture
    if _kraken_client is None or not _kraken_discovery:
        return
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    elapsed = ((now - _last_dispatch_capture).total_seconds()
               if _last_dispatch_capture else None)
    if elapsed is not None and elapsed < 300:  # 5 minutes
        return
    _last_dispatch_capture = now
    _capture_completed_slots.clear()
    global _dispatch_skip_said
    try:
        _iog = tariff_kind_at(now.replace(microsecond=0).isoformat()) == "intelligent"
    except Exception:
        _iog = True                 # not known: asked, as before
    if not _iog:
        # Dispatches are Intelligent Octopus's: on another tariff the account has no device and
        # Octopus answers "Unable to find device for given account" — asked every five minutes,
        # a tester's Go account logged it 217 times (5 Nov 2026). Said once.
        if not _dispatch_skip_said:
            logger.info("_tick_dispatch_capture: the import tariff is not Intelligent — "
                        "dispatches not asked")
            _dispatch_skip_said = True
    else:
        _dispatch_skip_said = False
        try:
            await _capture_dispatch_slots()
        except Exception as e:
            logger.warning("_tick_dispatch_capture: failed: %s", e)
    # A completed record is the carve's input: split those slots now, not at the next hourly pass.
    # Local and additive; a slot already carved, or settled, does not match. Only the slots this
    # capture saw, so it never scans history.
    if _capture_completed_slots:
        try:
            await run_iog_split_carve(starts=sorted(_capture_completed_slots))
        except Exception as e:
            logger.warning("_tick_dispatch_capture: carve after capture failed: %s", e)

    # Settlement-time reconciliation (design §12) — hourly, not every 5 min. By
    # now `started` (real-time) and `completed` (~hours) have settled for older
    # slots, so we can reprice off-peak/peak from the lifecycle instead of the
    # meter. Runs on this loop thread (single store connection).
    global _last_reconcile
    # BL-59: only run — and only consume the hourly slot — once the import rate schedule
    # is actually built. On boot the first dispatch-capture tick can fire BEFORE
    # _refresh_kraken_rate_schedules finishes, so reconcile_dispatch_overlay would
    # early-return on the empty schedule while _last_reconcile was already advanced —
    # deferring the REAL reconcile a full hour. On a frequently-restarted box that hour
    # never elapses, so the reconcile (and the BL-52/58/58b heals riding it) never runs
    # and there is no heartbeat. Gate on readiness so an unready tick is a no-op that
    # does NOT burn the slot; the next tick (schedule ready) runs it.
    _isched_ready = _kraken_rate_schedules.get("import")
    _isched_ready = _isched_ready is not None and not _isched_ready.is_empty()
    r_elapsed = ((now - _last_reconcile).total_seconds()
                 if _last_reconcile else None)
    if _isched_ready and (r_elapsed is None or r_elapsed >= 3600):
        _last_reconcile = now
        try:
            await reconcile_dispatch_overlay()
        except Exception as e:
            logger.warning("_tick_dispatch_capture: reconcile failed: %s", e)
        # NOTE four one-off v4 heals ran here (smb_rate_repair 4.5.7, smb_device_recost
        # 4.5.7, smb_ev_resplit 4.5.9, band_rate_snap 4.5.13). Removed for v5: each was
        # marker-gated and self-marking, and the import floor guarantees they have
        # already run against any database v5 will ever open — "last opened by version X
        # means healed to X's standard" (EMT-v5_import_spec §1.1). v5 starts from an
        # empty heal set of its own.
        # 4.5.7: settled-label reconciliation. Fetch Octopus's settled per-slot bill (asked by
        # the local day) for settled IOG dispatched blocks and settle the block from it — both
        # bands, at any age: there is no asymmetric or age gate (apply_measured_settled; the
        # 4.5.7 gate this once described went with the rate-settlement horizon). Authority is
        # the settled bill, never the overlay.
        # 4.5.9: carve the PREDICTED EV/Home split onto unsettled dispatched blocks whose
        # completed dispatch landed after finalise (else the car's charge sits in House until
        # settlement). Additive, dispatch-capped; recurring (no done-marker).
        try:
            await run_iog_split_carve()
        except Exception as e:
            logger.warning("_tick_dispatch_capture: iog split carve failed: %s", e)
        # The one asker (engine step 3c, 24 Oct 2026): every half-hour waiting for Octopus's bill
        # or split, newest first, as a task off the loop. Backlog-gated; a no-op when nothing waits.
        # (It replaces the live bill pass, `measure_settled_dispatched_blocks`, that ran here first.)
        try:
            _maybe_drain_measured_history()
        except Exception as e:
            logger.warning("_tick_dispatch_capture: measured history drain schedule failed: %s", e)
        try:
            apply_measured_settled()
        except Exception as e:
            logger.warning("_tick_dispatch_capture: measured apply failed: %s", e)
        try:
            run_prune_stale_tombstones()
        except Exception as e:
            logger.warning("_tick_dispatch_capture: stale tombstone prune failed: %s", e)

def _planned_dispatch_slots_preview(planned: list) -> set:
    """Map planned dispatches to the 30-min slots they cover (naive-UTC ISO).

    Pure helper, no side effects. A dispatch covering any part of a 30-min slot
    maps the whole slot (matches BCD's 'any active minute counts' rule). Used by
    the observe-only step now; the real overlay (step 2+) will gate these on the
    intelligent state being active and validate against meter draw.
    """
    slots: set = set()
    for d in planned or []:
        start = d.get("start")
        end = d.get("end")
        if not start or not end:
            continue
        try:
            s = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            e = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        except Exception:
            continue
        # Normalise to naive UTC
        if s.tzinfo is not None:
            s = s.astimezone(timezone.utc).replace(tzinfo=None)
        if e.tzinfo is not None:
            e = e.astimezone(timezone.utc).replace(tzinfo=None)
        # Snap start down to the 30-min slot, then step in 30-min increments
        slot = s.replace(minute=(0 if s.minute < 30 else 30),
                         second=0, microsecond=0)
        while slot < e:
            slots.add(slot.isoformat())
            slot = slot + timedelta(minutes=30)
    return slots


def _planned_dispatch_slot_energy(planned: list, include_all: bool = False) -> dict:
    """Map smart-charge planned dispatches to per-slot FORECAST energy (kWh).

    Companion to _planned_dispatch_slots_preview: distributes each dispatch's
    energyAddedKwh (normalised to `delta`) evenly across the 30-min slots it
    covers, so we can persist energy_planned per slot. OBSERVE-ONLY — recorded
    for diagnostics / future validation, NOT used for billing. Slots whose
    dispatch has no delta map to None.

    `include_all` skips the smart-charge source filter — used by the OHME history
    path, whose source labels are unreliable so every planned slot is retained.
    """
    out: dict = {}
    for d in planned or []:
        if (not include_all
                and str(d.get("source") or "").lower() not in _SMART_CHARGE_SOURCES):
            continue
        start = d.get("start"); end = d.get("end")
        if not start or not end:
            continue
        try:
            s = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            e = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        except Exception:
            continue
        if s.tzinfo is not None:
            s = s.astimezone(timezone.utc).replace(tzinfo=None)
        if e.tzinfo is not None:
            e = e.astimezone(timezone.utc).replace(tzinfo=None)
        covered = []
        slot = s.replace(minute=(0 if s.minute < 30 else 30),
                         second=0, microsecond=0)
        while slot < e:
            covered.append(slot.isoformat())
            slot = slot + timedelta(minutes=30)
        if not covered:
            continue
        try:
            per = (float(d.get("delta")) / len(covered)) \
                if d.get("delta") is not None else None
        except (TypeError, ValueError):
            per = None
        for k in covered:
            if per is None:
                out.setdefault(k, None)
            else:
                out[k] = round((out.get(k) or 0.0) + per, 4)
    return out


def _planned_dispatch_slot_bounds(planned: list, include_all: bool = False) -> dict:
    """Map smart-charge planned dispatches to the EXACT (second-precision) raw
    window each slot belongs to (BL-11).

    The API's start/end are precise to the second, but capture snaps energy to
    30-min slots and discards the exact bounds. Retain them so a charge-session
    view (BL-10) can show the true scheduled window: every 30-min slot a dispatch
    covers carries that dispatch's (start, end) as naive-UTC ISO, so a session's
    exact window is min(raw_start) / max(raw_end) over its slots. Mirrors the
    slotting of _planned_dispatch_slot_energy exactly. OBSERVE-ONLY.
    Returns {slot_start_iso: (raw_start_iso, raw_end_iso)}.

    `include_all` skips the smart-charge source filter (OHME history path).
    """
    out: dict = {}
    for d in planned or []:
        if (not include_all
                and str(d.get("source") or "").lower() not in _SMART_CHARGE_SOURCES):
            continue
        start = d.get("start"); end = d.get("end")
        if not start or not end:
            continue
        try:
            s = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            e = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        except Exception:
            continue
        if s.tzinfo is not None:
            s = s.astimezone(timezone.utc).replace(tzinfo=None)
        if e.tzinfo is not None:
            e = e.astimezone(timezone.utc).replace(tzinfo=None)
        raw = (s.isoformat(), e.isoformat())
        slot = s.replace(minute=(0 if s.minute < 30 else 30),
                         second=0, microsecond=0)
        while slot < e:
            out[slot.isoformat()] = raw   # last dispatch wins on the rare overlap
            slot = slot + timedelta(minutes=30)
    return out


def _completed_dispatch_slot_energy(completed: list) -> dict:
    """Map COMPLETED dispatches to per-slot delivered energy (kWh).

    Like _planned_dispatch_slot_energy, but for completedDispatches — what
    Octopus ACTUALLY dispatched, which land hours after the charge. No source
    filter: completed dispatches come back source='unknown' (the smart-vs-bump
    label is lost on completion), so we key on the dispatch itself, not a label.
    Signed like the planned figure (a charge is negative kWh). OBSERVE-ONLY —
    recorded to energy_completed for a future settlement-time validation;
    NOT used for billing yet.
    """
    out: dict = {}
    for d in completed or []:
        start = d.get("start"); end = d.get("end")
        if not start or not end:
            continue
        try:
            s = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            e = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        except Exception:
            continue
        if s.tzinfo is not None:
            s = s.astimezone(timezone.utc).replace(tzinfo=None)
        if e.tzinfo is not None:
            e = e.astimezone(timezone.utc).replace(tzinfo=None)
        covered = []
        slot = s.replace(minute=(0 if s.minute < 30 else 30),
                         second=0, microsecond=0)
        while slot < e:
            covered.append(slot.isoformat())
            slot = slot + timedelta(minutes=30)
        if not covered:
            continue
        try:
            per = (float(d.get("delta")) / len(covered)) \
                if d.get("delta") is not None else None
        except (TypeError, ValueError):
            per = None
        for k in covered:
            if per is None:
                out.setdefault(k, None)
            else:
                out[k] = round((out.get(k) or 0.0) + per, 4)
    return out


def _completed_dispatch_slot_bounds(completed: list) -> dict:
    """Map COMPLETED dispatches to the EXACT (second-precision) raw window each
    slot belongs to — the completed twin of _planned_dispatch_slot_bounds.

    `completed` is what Octopus ACTUALLY dispatched (start/end to the second); the
    per-slot capture otherwise keeps only energy and the *planned* window, so a
    charge that ran a fraction of a slot (e.g. a 3-min top-up, or the tail of a
    dispatch that ended mid-slot) is indistinguishable from a full planned block.
    Retaining the real completed bounds gives the actual dispatch/charge window.
    No source filter (completed dispatches come back source='unknown', matching
    _completed_dispatch_slot_energy). Every 30-min slot the dispatch covers carries
    that dispatch's (start, end) as naive-UTC ISO, so a session's exact window is
    min(raw_start) / max(raw_end) over its slots. OBSERVE-ONLY.
    Returns {slot_start_iso: (raw_start_iso, raw_end_iso)}.
    """
    out: dict = {}
    for d in completed or []:
        start = d.get("start"); end = d.get("end")
        if not start or not end:
            continue
        try:
            s = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            e = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        except Exception:
            continue
        if s.tzinfo is not None:
            s = s.astimezone(timezone.utc).replace(tzinfo=None)
        if e.tzinfo is not None:
            e = e.astimezone(timezone.utc).replace(tzinfo=None)
        raw = (s.isoformat(), e.isoformat())
        slot = s.replace(minute=(0 if s.minute < 30 else 30),
                         second=0, microsecond=0)
        while slot < e:
            out[slot.isoformat()] = raw   # last dispatch wins on the rare overlap
            slot = slot + timedelta(minutes=30)
    return out


def _derive_started_slots(now_utc: datetime, planned: list,
                          intelligent_state: str | None) -> list:
    """Derive `started` 30-minute slots (design §11.2 — BCD's mechanism).

    The CURRENT slot becomes 'started' ONLY when the intelligent state is
    `SMART_CONTROL_IN_PROGRESS` AND a planned dispatch is active right now (and
    therefore still present in the list — planned can be pulled last-minute).
    Done one 30-min slot at a time; accumulation across polls builds the full
    set. A bump never enters `SMART_CONTROL_IN_PROGRESS`, so it never becomes
    started — which is exactly why `started` discriminates smart from bump where
    `completed` can't. Returns 0 or 1 slot_start ISO strings (naive UTC).
    """
    if intelligent_state != "SMART_CONTROL_IN_PROGRESS":
        return []
    now = now_utc
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    active = False
    for d in planned or []:
        s, e = d.get("start"), d.get("end")
        if not s or not e:
            continue
        try:
            sd = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
            ed = datetime.fromisoformat(str(e).replace("Z", "+00:00"))
        except Exception:
            continue
        if sd.tzinfo is not None:
            sd = sd.astimezone(timezone.utc).replace(tzinfo=None)
        if ed.tzinfo is not None:
            ed = ed.astimezone(timezone.utc).replace(tzinfo=None)
        if sd <= now < ed:
            active = True
            break
    if not active:
        return []
    slot = now.replace(minute=(0 if now.minute < 30 else 30),
                       second=0, microsecond=0)
    return [slot.isoformat()]


# A completed-without-started dispatch below this magnitude didn't materially run:
# too small to be a bump (which draws hard) OR a real smart charge, so it's peak.
# Keyed on Octopus's completed ENERGY, not the meter, so it stays solar-safe.
_RECONCILE_SMALL_COMPLETED_KWH = 0.4


def _reconcile_decision(has_started: bool, has_completed: bool,
                        completed_energy, currently_off_peak: bool,
                        has_planned: bool = True, was_online: bool = False,
                        contemporaneous: bool = True,
                        past_settle: bool = False,
                        small_kwh: float = _RECONCILE_SMALL_COMPLETED_KWH) -> tuple:
    """Settlement-time reconciliation of the dispatch overlay against the
    accumulated lifecycle (design §12, §14). Keys on whether the slot STARTED
    (SMART_CONTROL_IN_PROGRESS + planned active) — the validated smart-vs-bump,
    solar-immune signal — and, for the completed-without-started case, on whether we
    were online to see it (has_planned) and the completed ENERGY magnitude.

    Returns (target, reason) where target is one of:
      - 'off_peak' : (a) started AND (completed, or still inside the settle window,
                     or we were offline) → genuine smart charge (restore, incl. solar
                     slots the meter floor wrongly rejected); or (b) COMPLETED-ONLY
                     with substantial energy, NO planned/started ever captured, AND the
                     block is OFFLINE/imported (was_online=False) — EMT was down / it's a
                     re-import, so 'started' was never capturable. We accept Octopus's
                     completed dispatch as authoritative (design §14).
      - 'peak'     : (a0) STARTED but never COMPLETED once the settle window has
                     passed and we were online to have seen it (BL-71) — `started` is
                     sampled, not recorded, so it has a false-positive rate and must
                     not be permanently conclusive; (a) neither started nor completed
                     → planned but never charged;
                     (b) completed-without-started with NEGLIGIBLE energy
                     (|completed| < small_kwh) — the dispatch didn't materially run;
                     or (c) COMPLETED-ONLY on a LIVE block (was_online=True) — EMT was
                     online and polling, so an unplanned completed dispatch is an
                     out-of-app BUMP, not a missed smart charge (4.5.0, design §14a).
      - 'review'   : we WERE online (a 'planned' was captured) but the slot never
                     STARTED, yet completed with substantial energy — genuinely
                     ambiguous (a bump vs a paused smart charge), §11.1. Price left
                     unchanged and the block flagged for the user to check. We do NOT
                     auto-off-peak this case, because unlike the offline case we had
                     the chance to see 'started' and didn't.
      - 'ok'       : the correct target already matches the current rate.
    Only 'off_peak' and 'peak' are actionable; 'review' flags, 'ok' writes nothing.
    """
    if has_started and (has_completed or not past_settle or not was_online):
        # BL-71: `started` alone is not conclusive — it is a SAMPLED signal, not a
        # record. Octopus never returns a started dispatch; EMT derives it from
        # SMART_CONTROL_IN_PROGRESS observed while a planned slot is active, at the
        # poll cadence. So it is kept as the finalise-time gate (measured 97.4%
        # precise against 61.3% for `planned` alone) but `completed` now adjudicates
        # it in BOTH directions, which is what the design always said `completed` was
        # for. Off-peak still stands here when:
        #   - completed confirms it, or
        #   - the settle window has not passed (the fast RESTORE — `started` is
        #     real-time, so a solar-supplied charge the meter floor rejected is put
        #     right within the 40-minute gate rather than hours later), or
        #   - we were offline, where absence of `completed` is not evidence of
        #     anything (same principle as the completed-only branches below).
        target = "off_peak"
        reason = "started → off-peak (restore)"
    elif has_started:
        # started, past the settle window, we were online to see a completed — and
        # none came. Measured on three years of one account: 11 such slots, every one
        # with ZERO charger draw. Falls through to the same revert the never-started
        # case takes, so an in-window slot is unaffected (the caller skips those
        # outright) and the price is re-derived from the tariff schedule rather than
        # forced to peak. Self-correcting: if `completed` lands late, the next hourly
        # reconcile sees started+completed and restores off-peak.
        target = "peak"
        reason = "started but never completed after settle window → peak (revert)"
    elif not has_completed:
        target = "peak"
        reason = "never started or completed → peak (revert)"
    else:
        mag = abs(completed_energy) if completed_energy is not None else 0.0
        if mag < small_kwh:
            target = "peak"
            reason = (f"completed but not started, negligible energy "
                      f"({mag:.2f} kWh < {small_kwh} kWh) → peak (did not run)")
        elif has_planned:
            # We were ONLINE (captured a 'planned') but it never went
            # SMART_CONTROL_IN_PROGRESS. Per §11.1 that's ambiguous (bump vs paused
            # smart) — leave at peak, flag for review. Unchanged from the original.
            return ("review",
                    f"completed ({mag:.2f} kWh) but not started, planned was seen — "
                    "ambiguous (bump vs paused smart)")
        elif was_online and contemporaneous:
            # 4.5.0: COMPLETED-ONLY on a LIVE block (interpolated=0, not imported), and
            # the completed was seen CONTEMPORANEOUSLY → EMT was online AND accumulating
            # dispatch history for this slot, yet no 'planned'/'started' was ever seen. A
            # genuine smart charge is always planned (and we accumulate polls, so we'd
            # have caught it); the only completed-only dispatch that appears while we were
            # watching is an out-of-app BUMP/boost — peak, never sipped the cap. Confident,
            # not 'review': a bump is unplanned by construction, so revert to peak. (The
            # rare meter-up/dispatch-poller-down miss stays a fast-follow, BL-37.)
            target = "peak"
            reason = (f"completed ({mag:.2f} kWh) not started, no plan, EMT online "
                      "→ out-of-app bump → peak")
        else:
            # COMPLETED-ONLY but NOT a confident bump: either an OFFLINE block
            # (interpolated/gap), or the completed was re-fetched long after the slot
            # (rebuild / re-import → the locally-accumulated 'started' is LOST, not
            # absent). In both cases absence-of-started is not evidence of a bump, so
            # accept Octopus's completed dispatch as off-peak (design §14; §11.3). The
            # optimistic default matches pre-4.5.0; settlement stays authoritative.
            target = "off_peak"
            reason = (f"completed ({mag:.2f} kWh) not started, "
                      f"{'offline/import' if not was_online else 'history re-fetched (not contemporaneous)'}"
                      " — off-peak from Octopus completed dispatch (not live-verified)")
    if (target == "off_peak" and currently_off_peak) or \
       (target == "peak" and not currently_off_peak):
        return ("ok", "already correct")
    return (target, reason)


_RECONCILE_SETTLE_HOURS = 6.0     # revert/review: wait for completed (~hrs) to settle
_RECONCILE_STARTED_GATE_MIN = 40  # restore: slot ended + a poll margin (started is real-time)

# ── BL-53 step 2: measured-cost fetch (Octopus BILLED cost for settled dispatched
# slots — the authoritative figure the 4-rate cap total is hardest to re-derive) ──
# 4.5.6: Octopus removed the per-slot OFF_PEAK label from the Measurements API — a
# dispatched off-peak slot now returns the GROSS STANDARD (peak) rate with no way to
# tell it from a genuine peak slot (verified: STANDARD_RATE for 2026-07-21 16:00 BST,
# which the bill charged off-peak). With the label gone, the settlement-time fetch can
# only ever return peak, and applying it STAMPS SETTLED OFF-PEAK BLOCKS TO PEAK —
# the 4.5.5 corruption. Both switches are held OFF until Octopus re-exposes a per-slot
# band signal; the dispatch overlay (live capture) remains the per-slot off-peak truth.
# 4.5.7: RE-ENABLED, safely. The label reads correctly now (asked by the local day since 22 Oct
# 2026 — the narrow forward ladder had the middle of a long charge STANDARD),
# and the apply is ASYMMETRIC + AGE-GATED (see apply_measured_settled): it heals toward
# OFF_PEAK on the crisp/monotonic off-peak label at ANY age, but only moves a block
# toward PEAK (STANDARD/mixed) once it has aged past the rate-settlement horizon — so a
# PROVISIONAL STANDARD (the 4.5.6 corruption) can never downgrade an off-peak block.
# Authority is Octopus's settled bill, not EMT's dispatch overlay.
def capped_at(start: str) -> bool:
    """Whether the half-hour at `start` is priced under the 6-hour cap: its own import agreement
    is IOG-SMB. The same question as `split_at` — whether Octopus's four buckets apply — asked by
    the pricing (capped pricing by agreement design §4); where no agreement covers `start`, the
    current tariff, as `split_at`. Capped PRICING also needs the EV-device rates loaded
    (`_capped_schedules`); the band relabel needs only this."""
    return split_at(start)


_MEASURED_MAX_PER_PASS = 40        # bound the rate-limited fetch per hourly pass
# Tariff-era floor: the IOG-SMB rollout began ~1 Aug 2026, so measured pricing only
# needs to reach back to there (matches the BL-55 heal floor). Older dispatched slots
# were priced correctly by the pre-migration schedule and need no bill fetch. For the
# general release this should derive from the per-user tariff-migration boundary.
_MEASURED_FLOOR = "2026-08-01T00:00:00"


def _smb_windows() -> list:
    """[(from, to | None)] naive-UTC of the import agreements that are IOG-SMB, oldest first, from
    the agreements discovery holds (as `_agreement_era`). Where it holds none and the current
    tariff is IOG-SMB: one open window from the current agreement's start (or `_MEASURED_FLOOR`).
    Empty on an account that has never been on IOG-SMB. Capped pricing by agreement design §4:
    the account-level gates ask "any IOG-SMB agreement", not "the current one"."""
    import api_import as _ai
    info = _agreement_info("import")
    out = []
    for a in (info.get("agreements") or []):
        if "IOG-SMB" not in (a.get("tariff_code") or "").upper() or not a.get("valid_from"):
            continue
        try:
            vf = _ai._iso(_ai._naive_utc(a.get("valid_from")))
            vt = _ai._iso(_ai._naive_utc(a.get("valid_to"))) if a.get("valid_to") else None
        except Exception:
            continue
        out.append((vf, vt))
    if not out and _import_is_smb_capped():
        out.append((str(_kraken_current_agreement_from or _MEASURED_FLOOR), None))
    return sorted(out)


def _intelligent_windows() -> list:
    """[(from, to | None, is_smb)] naive-UTC of the import agreements on an Intelligent tariff
    (IOG, INTELLI — capped or not), oldest first, from the agreements discovery holds. Where it
    holds none and the current tariff is Intelligent: one open window from the current agreement's
    start (or `_MEASURED_FLOOR`). The bill passes walk these (18 Oct 2026, the user: on EVERY
    Intelligent tariff a dispatch can re-band a half-hour, so only Octopus's cost is final, and
    EMT must ask for it — settlement surfaces design §9.3). `is_smb`: the window is IOG-SMB,
    where imported history is asked too (its bill carries the House/EV split)."""
    import api_import as _ai
    info = _agreement_info("import")
    out = []
    for a in (info.get("agreements") or []):
        code = a.get("tariff_code") or ""
        if not _is_intelligent_code(code) or not a.get("valid_from"):
            continue
        try:
            vf = _ai._iso(_ai._naive_utc(a.get("valid_from")))
            vt = _ai._iso(_ai._naive_utc(a.get("valid_to"))) if a.get("valid_to") else None
        except Exception:
            continue
        out.append((vf, vt, "IOG-SMB" in code.upper()))
    if not out and _is_intelligent_code(info.get("tariff_code")):
        out.append((str(_kraken_current_agreement_from or _MEASURED_FLOOR), None,
                    _import_is_smb_capped()))
    return sorted(out)

def _any_intelligent() -> bool:
    """The account has been on an Intelligent tariff: its history is asked for its bills."""
    return bool(_intelligent_windows())


def _measured_floor() -> str:
    """Effective floor for the measured-cost pass — the start of the EARLIEST Intelligent
    agreement (18 Oct 2026, the user: every Intelligent half-hour needs Octopus's cost, capped or
    not — settlement surfaces design §9.3; it was the earliest IOG-SMB agreement's, 17 Oct, and the
    current agreement's before). On an account never on an Intelligent tariff, the start of the
    current agreement.

    v4 4.5.5's reason for the IOG-SMB floor, reversed here: "never reaches into pre-migration
    dates (which the prior tariff's schedule and the settlement reconcile already price
    correctly)". A dispatch EMT did not see can still re-band an uncapped Intelligent half-hour, so
    only the bill is final; and the risk 4.5.5 carried — snapping a pre-migration bill to the
    CURRENT tariff's bands — is the agreement-stitched schedule's (BL-54) to answer now, with
    `apply_measured_settled` skipping a half-hour the stitch does not cover (`_agreement_priced_ok`).

    The measured fetch exists to reconcile Octopus's 6-hour-cap accounting, which is
    an SMB-tariff concept. Pre-migration dispatched slots were priced correctly by the
    prior (uncapped, e.g. INTELLI) tariff's schedule and are owned by reconcile — the
    measured pass must not reach into them (that is what pulled 2026-08-14 and other
    pre-cap days into scope). `_kraken_current_agreement_from` is the live import
    agreement's valid_from (naive-UTC iso), maintained on every schedule refresh; for
    a single SMB agreement that is the migration boundary. Falls back to the blunt
    `_MEASURED_FLOOR` only when the boundary is unknown."""
    _w = _intelligent_windows()
    if _w:
        return _w[0][0]
    caf = _kraken_current_agreement_from
    if caf:
        return str(caf)
    return _MEASURED_FLOOR


def _import_is_smb_capped() -> bool:
    """True when the current import tariff is the capped IOG-SMB (6-hour-cap) tariff.

    Robust to a transient-empty `ev_device_off_peak` schedule build: keys off the
    tariff/product CODE (which never disappears mid-session), falling back to the
    derived schedule's presence. The old `"ev_device_off_peak" in _kraken_rate_schedules`
    check alone could silently go false on a refresh race and drop the rate-line cap
    context (EV line collapses to zero) while the import schedule stayed put."""
    imp = (_kraken_discovery or {}).get("import") or {}
    code = f"{imp.get('tariff_code') or ''} {imp.get('product_code') or ''}".upper()
    return "IOG-SMB" in code or "ev_device_off_peak" in _kraken_rate_schedules


def _chart_cap_from():
    """Per-day cap gating for the rate lines: the capped days, as [[from_day, to_day | None]]
    site-local dates — one per IOG-SMB agreement (`_smb_windows`), to_day exclusive — or None on
    an account never on IOG-SMB (its days render uncapped TOU). Read by
    billing_view.rate_context for the Billing tab's rate lines; `energy_charts.day_chart_data`
    gates on it. One date from the current agreement until 17 Oct 2026 (capped pricing by
    agreement design §4): after a renewal or after leaving IOG-SMB it gated the wrong days.

    The day is the SITE-LOCAL date the agreement starts: a window is naive UTC, so an agreement
    from 00:00 BST on 26 Aug 2026 is 2026-08-25T23:00:00, and its first ten characters made
    25 Aug a capped day."""
    windows = _smb_windows()
    if not windows:
        return None

    def _day(iso):
        if iso is None:
            return None
        try:
            from zoneinfo import ZoneInfo
            dt = datetime.fromisoformat(str(iso)).replace(tzinfo=timezone.utc)
            return dt.astimezone(ZoneInfo(_iog_site_tz())).date().isoformat()
        except Exception:
            return str(iso)[:10]
    return [[_day(vf), _day(vt)] for vf, vt in windows]


# ── A bill that drops the car's half-hour is not settled yet (v4 4.5.23, #510) ─────────────
# On 2026-10-10 Octopus's device breakdown, fetched ~29 h after the night, put every dispatched
# half-hour wholly on the house (EV 0 kWh) and billed 05:30-06:00 BST at the standard rate; days
# later it served the true split, all at the low rate. The breakdown is cached once and applied as
# the authority, so the provisional answer replaced EMT's own EV split for good. Now: where EMT's
# split has the car drawing and the bill says the EV drew nothing, the bill is HELD BACK — not
# cached, not applied, EMT's split kept — and asked for again on later passes. If Octopus still
# says so 28 days after the half-hour, EMT stops asking and keeps its own split. Forward code: v4's
# one-off re-fetch of bills cached that way before 4.5.23 is not ported (the 4.5.23 floor).
# Kept in the ask record, kind 'hold' ({slot: {"first": iso, "kept": iso?}}; kraken_state
# "bill_split_wait" until engine step 3b, 24 Oct 2026).
_BILL_SPLIT_WAIT_DAYS = 28
_BILL_SPLIT_EMT_MIN_KWH = 0.05              # EMT's EV split below this is no evidence of a charge


def _bill_drops_ev(emt_ev_kwh, bill_ev_kwh) -> bool:
    """The bill's breakdown has no EV where EMT's own split has the car drawing."""
    try:
        emt = float(emt_ev_kwh or 0.0)
        bill = float(bill_ev_kwh or 0.0)
    except (TypeError, ValueError):
        return False
    return emt > _BILL_SPLIT_EMT_MIN_KWH and bill <= 1e-9


def emt_ev_reference(segment_ev, cached_bill_ev) -> float:
    """What a new answer is judged against by the 4.5.23 hold-back (`_bill_drops_ev`): the car is
    known to have charged in a half-hour if EMT's split (its EV segments) OR a bill already cached
    has it — the larger of the two. Bill and split paths design §2 (16 Oct 2026): the four paths
    read it three ways (segments; the cached bill; the larger). Where nothing is cached (the live
    pass, the drain) this is the segments; where a bill is (the re-fetch), a later
    answer that drops the car is the 10 Oct 2026 provisional answer the other way round."""
    def _f(x):
        try:
            return float(x or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return max(_f(segment_ev), _f(cached_bill_ev))


def emt_ev_at(store, starts) -> dict:
    """{start: emt_ev_reference} for `starts` — one read of the segments and the cached bills."""
    seg = store.get_ev_split_kwh(starts)
    bill = store.get_bill_ev_kwh(starts)
    return {s: emt_ev_reference(seg.get(s), bill.get(s)) for s in starts}


def _bill_split_wait(store) -> dict:
    """The hold-back's half-hours ({slot: {first, kept?}}), from the ask record (step 3b)."""
    return store.ask_record("hold")


def _snapshot(m: dict) -> dict:
    """A copy of a kraken_state map deep enough to diff: its entries are dicts mutated in place."""
    return {k: (dict(v) if isinstance(v, dict) else v) for k, v in m.items()}


def _save_changes(store, before: dict, after: dict, read, save) -> None:
    """Save what this pass changed in a kraken_state map, not its whole copy (17 Oct 2026).

    The live bill pass and the history drain each read the hold-back's wait map and the bill-ask
    record, wait on Octopus, then save. They run as separate tasks on the loop, so one's
    whole-copy save overwrote whatever the other saved in between — a held half-hour's first
    date, a release, an unanswered half-hour. Re-read just before saving, with no await between
    the two, and apply only this pass's additions, changes and removals."""
    if after == before:
        return
    cur = read()
    for k in before:
        if k not in after:
            cur.pop(k, None)
    for k, v in after.items():
        if k not in before or before[k] != v:
            cur[k] = v
    save(store, cur)


def _bill_split_wait_save(store, w: dict) -> None:
    store.ask_record_save("hold", w)


def _bill_split_kept(store, w: dict) -> set:
    """Slots held back for 28 days and more: EMT keeps its split and stops asking. Marks (and logs)
    each one once, the first time it is seen past the limit."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    limit = (_dt.now(_tz.utc).replace(tzinfo=None) - _td(days=_BILL_SPLIT_WAIT_DAYS)).isoformat()
    kept, changed = set(), False
    for slot, ent in w.items():
        if slot >= limit:
            continue
        kept.add(slot)
        if isinstance(ent, dict) and not ent.get("kept"):
            ent["kept"] = _dt_now_iso_safe()
            changed = True
            logger.warning("bill split: after %d days Octopus's breakdown for %s still has no EV "
                           "where the car drew — keeping EMT's own split for it",
                           _BILL_SPLIT_WAIT_DAYS, slot)
    if changed:
        _bill_split_wait_save(store, w)
    return kept


def bill_stage() -> dict:
    """Whether this install has a bill stage, and for which half-hours (ROADMAP BL-82's model).
    `fetched`: an Intelligent tariff with the measured fetch on — EMT asks Octopus for bills.
    `every_half_hour`: the current tariff is IOG-SMB, where every half-hour's bill is fetched
    (the live pass for the dispatched ones, the history drain for the rest); otherwise only
    dispatched ones are.
    `windows`: every Intelligent agreement's window (`_intelligent_windows`) — every half-hour in
    them is fetched by the drain, whatever the current tariff (capped pricing by agreement design
    §4; all Intelligent agreements since 20 Oct 2026, not only IOG-SMB).
    `floor`: the measured pass's floor. One rule for the pill and the settlement views."""
    imp = (_kraken_discovery or {}).get("import") or {}
    _tu = (imp.get("tariff_code") or "").upper()
    fetched = "IOG" in _tu or "INTELLI" in _tu
    return {"fetched": fetched, "every_half_hour": bool(fetched and _import_is_smb_capped()),
            # every Intelligent agreement's window: the drain asks for every half-hour in them
            "windows": [(vf, vt) for vf, vt, _s in _intelligent_windows()],
            "floor": _measured_floor()}


def bill_split_held(store) -> tuple:
    """(held, kept): the half-hours whose bill is held back for its split and still being asked
    for, and those whose 28 days ran out with EMT's split kept (v4 4.5.23). Read-only."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    wait = _bill_split_wait(store)
    limit = (_dt.now(_tz.utc).replace(tzinfo=None) - _td(days=_BILL_SPLIT_WAIT_DAYS)).isoformat()
    held = {s for s, e in wait.items() if s >= limit and not (isinstance(e, dict) and e.get("kept"))}
    return held, set(wait) - held

def bill_no_cost(store) -> set:
    """The half-hours whose bill was asked for 30 days unanswered, and is no longer asked: they keep
    EMT's kWh × rate, and the Import bill hatches them (19 Oct 2026, the user). Live or imported
    alike since engine step 3e (24 Oct 2026) — the one asker asks imported history too (decided
    (b)), so the import's no-cost record, and its "never asked" imported half-hours, went.
    Read-only — the give-up is marked by the asker."""
    mpan = ((_kraken_discovery or {}).get("import") or {}).get("mpan")
    if not mpan:
        return set()
    try:
        return _bill_ask_given_up(_bill_ask(store, mpan), mark=False)
    except Exception:
        return set()


def no_cost_since(store, slot: str):
    """When EMT stopped asking Octopus for this half-hour's cost (naive UTC), or None: the 30th
    day of a bill asked unanswered. For the detail panel."""
    from datetime import datetime as _dt, timedelta as _td
    mpan = ((_kraken_discovery or {}).get("import") or {}).get("mpan")
    ent = (_bill_ask(store, mpan) if mpan else {}).get(slot) if mpan else None
    if isinstance(ent, dict):
        if ent.get("gave_up"):
            return ent["gave_up"]
        if ent.get("first"):
            try:
                return (_dt.fromisoformat(ent["first"][:19]) + _td(days=_BILL_ASK_DAYS)).isoformat()
            except ValueError:
                return None
    return None


def settlement_awaiting(store, since_iso: str) -> dict:
    """How many half-hours since `since_iso` wait at each stage of settlement (ROADMAP BL-82's
    model): their kWh (DCC), then their bill, then — on IOG-SMB, where the bill's split is held
    back (v4 4.5.23) — their split. For the Charts pill (BL-85, 13 Oct 2026). Read-only: `store`
    may be a ReadStore.
    - kwh: count_unsettled_blocks — import kWh, or export kWh where export settles.
    - bill: only where EMT fetches bills. Every half-hour inside an IOG-SMB agreement's window
      (the drain asks them, whatever the current tariff); outside them, on a current Intelligent
      tariff with the measured fetch on, dispatched ones from the floor. Otherwise 0.
    - split: held-back bills still being asked for (not past the 28 days).
    A half-hour Octopus has not billed in 30 days of asking is no longer waiting (§5 (a))."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    wait = _bill_split_wait(store)
    limit = (_dt.now(_tz.utc).replace(tzinfo=None) - _td(days=_BILL_SPLIT_WAIT_DAYS)).isoformat()
    split = sum(1 for s in wait if s >= limit and s >= since_iso)
    stage = bill_stage()
    bill = 0
    windows = stage.get("windows")
    if windows is None:                  # an older stage dict: one rule over the whole range
        windows = [(stage["floor"], None)] if stage.get("every_half_hour") else []
    if stage["fetched"] or windows:
        _mpan = ((_kraken_discovery or {}).get("import") or {}).get("mpan") or ""
        _gone = _bill_ask_given_up(_bill_ask(store, _mpan), mark=False)
        _ex = list(set(wait) | _gone)
        for vf, vt in windows:           # every half-hour inside an IOG-SMB agreement
            if vt and vt <= since_iso:
                continue
            bill += store.count_awaiting_bill(max(since_iso, vf), every_half_hour=True,
                                              until_iso=vt, exclude=_ex)
        if stage["fetched"]:             # the dispatched ones outside them, from the floor
            lo = max(since_iso, stage["floor"])
            for vf, vt in sorted(windows):
                if vf > lo:
                    bill += store.count_awaiting_bill(lo, every_half_hour=False,
                                                      until_iso=vf, exclude=_ex)
                if vt is None:
                    lo = None
                    break
                lo = max(lo, vt)
            if lo is not None:
                bill += store.count_awaiting_bill(lo, every_half_hour=False, exclude=_ex)
    return {"kwh": int(store.count_unsettled_blocks(since_iso=since_iso)), "bill": int(bill),
            "split": int(split)}


def _bill_split_hold(w: dict, slot: str) -> None:
    """Hold an answer back (4.5.23), and note when it was asked: a held half-hour follows the same
    backoff as an unanswered one (engine step 3c — it was asked every pass, hourly, for 28 days)."""
    if slot in w:
        if isinstance(w[slot], dict):
            w[slot]["last"] = _dt_now_iso_safe()
    else:
        w[slot] = {"first": _dt_now_iso_safe(), "last": _dt_now_iso_safe()}
        logger.info("bill split: Octopus's breakdown for %s has no EV where EMT's split has the car "
                    "drawing — holding it back and asking again later (up to %d days)",
                    slot, _BILL_SPLIT_WAIT_DAYS)


def _bill_split_release(w: dict, slot: str) -> None:
    if w.pop(slot, None) is not None:
        logger.info("bill split: Octopus's breakdown for %s now carries the EV — settled", slot)


# ── A half-hour Octopus never bills is not asked for ever (17 Oct 2026, the user) ────────────
# The live pass and the history drain asked again, every pass, for any half-hour with no cached
# bill. One Octopus never answers — no node, or nothing drawn where EMT holds kWh (`parse_bill`) —
# stayed in the drain's backlog, so the hourly tick rescheduled the drain for it for good: on the
# soak of 26-30 Sep 2026, 79 of 98 hourly runs were for the same three half-hours. Now each
# half-hour is asked for up to 30 days from the first time Octopus did not answer it, then
# dropped from both passes and the backlog. It keeps its estimate (and its cell on the heatmap);
# the range re-fetch still asks for it. A bill held back for its split (4.5.23) has its own wait.
# Capped pricing by agreement design §5 (a).
# Kept in the ask record, kind 'bill' ({slot: {"first", "last"?, "gave_up"?}}; kraken_state
# "bill_ask" until engine step 3b, 24 Oct 2026).
_BILL_ASK_DAYS = 30
# The backoff (20 Oct 2026, the user: go easier on the API): an unanswered bill is asked at every
# pass — hourly — for its first 2 days, then once a day, until the 30 days run out.
_BILL_ASK_HOURLY_DAYS = 2
_BILL_ASK_DAILY_HOURS = 24


def _bill_ask(store, mpan: str) -> dict:
    """The unanswered half-hours, less any whose bill has since been cached (by a re-fetch)."""
    a = store.ask_record("bill")
    if a:
        try:
            still = set(store.measured_slots_missing(sorted(a), mpan=mpan))
            a = {s: e for s, e in a.items() if s in still}
        except Exception:
            pass
    return a


# The same record for a half-hour whose cached bill is cost-only though the car drew in it
# (teal): asked for its BREAKDOWN up to 30 days, then left as it is. Its own key, because the
# bill-ask record forgets any half-hour with a cached bill (`_bill_ask`), and these all have one.
# Kept in the ask record, kind 'split' (the same shape; kraken_state "split_ask" until step 3b).


def _split_ask(store) -> dict:
    """The teal half-hours left unanswered, less any whose breakdown has since been cached."""
    a = store.ask_record("split")
    if a:
        try:
            have = store.get_bill_ev_kwh(sorted(a))
            a = {s: e for s, e in a.items() if s not in have}
        except Exception:
            pass
    return a


def _split_ask_save(store, a: dict) -> None:
    store.ask_record_save("split", a)


def _bill_ask_save(store, a: dict) -> None:
    store.ask_record_save("bill", a)


def _bill_ask_given_up(a: dict, *, mark: bool = True) -> set:
    """Half-hours first left unanswered 30 days ago or more: no longer asked. With `mark`, marks
    (and logs) each one once, the first time it is seen past the limit; the passes that ask mark,
    the counts that only read (the backlog, the pill) do not."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    limit = (_dt.now(_tz.utc).replace(tzinfo=None) - _td(days=_BILL_ASK_DAYS)).isoformat()
    out = set()
    for slot, ent in a.items():
        first = ent.get("first") if isinstance(ent, dict) else None
        if not first or first >= limit:
            continue
        out.add(slot)
        if mark and not ent.get("gave_up"):
            ent["gave_up"] = _dt_now_iso_safe()
            logger.warning("bill: Octopus has not billed %s in %d days of asking — no longer "
                           "asked; it keeps EMT's estimate (a range re-fetch still asks)",
                           slot, _BILL_ASK_DAYS)
    return out


def _bill_ask_resting(a: dict, now=None) -> set:
    """Half-hours not to ask this pass: past their first `_BILL_ASK_HOURLY_DAYS` of asking, and
    asked within the last `_BILL_ASK_DAILY_HOURS` (the backoff). One with no `last` — recorded
    before the backoff — is asked, and from then on daily."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    now = now or _dt.now(_tz.utc).replace(tzinfo=None)
    hourly_until = (now - _td(days=_BILL_ASK_HOURLY_DAYS)).isoformat()
    rested_since = (now - _td(hours=_BILL_ASK_DAILY_HOURS)).isoformat()
    out = set()
    for slot, ent in a.items():
        if not isinstance(ent, dict):
            continue
        first, last = ent.get("first"), ent.get("last")
        if first and first < hourly_until and last and last >= rested_since:
            out.add(slot)
    return out


def _bill_ask_unanswered(a: dict, slot: str) -> None:
    now = _dt_now_iso_safe()
    a.setdefault(slot, {"first": now})["last"] = now


def _bill_ask_answered(a: dict, slot: str) -> None:
    a.pop(slot, None)

# ── One bill reader (bill and split paths design §1.3, §6 (a)) ───────────────────────────────
# Every path that asks Octopus for a half-hour's bill — the live settled-cost pass, the history
# drain, the range re-fetch — chooses its source with `bill_kind_at`, fetches
# with `_fetch_bill_nodes` and reads the answer with `parse_bill`. Until 16 Oct 2026 the breakdown
# was parsed in three copies that disagreed on a zero answer, and the source was chosen three ways.

def bill_kind_at(start: str) -> str:
    """'breakdown' where the half-hour's agreement is one Octopus serves a device breakdown for
    (IOG-SMB, `split_at`), else 'cost' — the single billed cost. Per half-hour, by its own
    agreement: before the agreement Octopus answers the breakdown too, but not as the bill
    (24 Aug 2026 05:30 BST: bill £0.0879 off-peak, breakdown £0.5169 at the day rate)."""
    return "breakdown" if split_at(start) else "cost"


_ZERO_DISAGREE_LOGGED: set = set()


def parse_bill(node, kind: str, emt_kwh) -> "dict | None":
    """One of Octopus's answers for a half-hour as the `measured_cost` row to store, or None when it
    is not an answer. `kind` is `bill_kind_at`'s; `emt_kwh` the kWh EMT holds for the half-hour.

    * a breakdown node → cost (sum of the buckets), ex-VAT from the buckets' ex-VAT rates, the
      label from the bands that carried energy, kWh, and `breakdown` (home kWh, rate, EV kWh, rate);
    * a cost node → cost, ex-VAT, the OFF_PEAK / STANDARD_RATE label, kWh — no breakdown;
    * **the zero rule** (§6 (a)): an answer of nothing drawn is a settled fact only where EMT holds
      zero kWh too → `{"zero": True}`, cached as ZERO (the breakdown stays NULL, so the split heal
      leaves it). Where EMT holds kWh it is not an answer — a bill that has not run yet also reads
      zero — so None, asked again, and logged once as a kWh disagreement."""
    def _n(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0
    if not node:
        return None
    if kind == "breakdown":
        _hk, _ek = _n(node.get("home_kwh")), _n(node.get("ev_kwh"))
        cost_incl = round(_n(node.get("home_cost")) + _n(node.get("ev_cost")), 6)
        if cost_incl <= 0 and (_hk + _ek) <= 1e-9:
            return _zero_answer(node, emt_kwh)
        _hre, _ere = node.get("home_rate_exc"), node.get("ev_rate_exc")
        cost_excl = (round(_hk * _n(_hre) + _ek * _n(_ere), 6)
                     if (_hre is not None or _ere is not None) else None)
        _bands = {b for b in (node.get("home_band"), node.get("ev_band")) if b}
        label = ("OFF_PEAK" if _bands == {"off_peak"}
                 else ("STANDARD_RATE" if _bands == {"peak"} else "mixed"))
        return {"cost_incl": cost_incl, "cost_excl": cost_excl, "label": label,
                "kwh": round(_hk + _ek, 6), "breakdown": (_hk, node.get("home_rate"),
                                                          _ek, node.get("ev_rate"))}
    if node.get("cost_incl") is None:
        return None
    if _n(node.get("cost_incl")) <= 0 and _n(node.get("kwh")) <= 1e-9:
        return _zero_answer(node, emt_kwh)
    _op = node.get("off_peak")
    label = "OFF_PEAK" if _op is True else ("STANDARD_RATE" if _op is False else "mixed")
    return {"cost_incl": node.get("cost_incl"), "cost_excl": node.get("cost_excl"),
            "label": label, "kwh": node.get("kwh")}


def _zero_answer(node, emt_kwh):
    if emt_kwh is not None and float(emt_kwh or 0.0) <= 1e-9:
        return {"zero": True}
    key = str(node.get("start") or node.get("startAt") or id(node))
    if key not in _ZERO_DISAGREE_LOGGED:
        _ZERO_DISAGREE_LOGGED.add(key)
        logger.warning("bill: Octopus answered nothing drawn where EMT holds %s kWh (%s) — not "
                       "taken as the bill; asked again", emt_kwh, key)
    return None


def _store_bill(store, slot: str, mpan: str, row: dict) -> None:
    """Cache a parsed bill: the cost row, and the breakdown where it has one."""
    store.upsert_measured_cost(slot, mpan=mpan, cost_incl=row["cost_incl"],
                               cost_excl=row["cost_excl"], label=row["label"], kwh=row["kwh"])
    if row.get("breakdown"):
        hk, hr, ek, er = row["breakdown"]
        store.upsert_measured_breakdown(slot, mpan=mpan, home_kwh=hk, home_rate=hr,
                                        ev_kwh=ek, ev_rate=er)


# Windows the last `_fetch_bill_nodes` asked that raised an API error, after the client's retries
# (engine step 3c): their half-hours were not answered "no bill" — Octopus was not reachable.
_bill_fetch_failures = 0


async def _fetch_bill_nodes(mpan: str, slots) -> dict:
    """{slot: (kind, node)} — the breakdown asked for the half-hours on a breakdown agreement, the
    single billed cost for the rest. A breakdown half-hour the breakdown leaves unanswered is NOT
    asked the billed cost: a cost-only bill there would be cached with no split, and a cached slot
    is not asked again, so the split would freeze at EMT's (16 Oct 2026; #167 had that fallback for
    every half-hour). The window is kraken_api_client's: the local day, then the month."""
    global _bill_fetch_failures
    kinds = {s: bill_kind_at(s) for s in slots}
    on = [s for s in slots if kinds[s] == "breakdown"]
    rest = [s for s in slots if kinds[s] == "cost"]
    _bill_fetch_failures = 0
    got = (await _kraken_client.recover_device_breakdown(mpan, on) or {}) if on else {}
    if on:
        _bill_fetch_failures += int(getattr(_kraken_client, "last_fetch_failures", 0) or 0)
    cost = (await _kraken_client.recover_measurement_costs(mpan, rest) or {}) if rest else {}
    if rest:
        _bill_fetch_failures += int(getattr(_kraken_client, "last_fetch_failures", 0) or 0)
    out = {}
    for s in slots:
        node = got.get(s) if kinds[s] == "breakdown" else cost.get(s)
        if node:
            out[s] = (kinds[s], node)
    return out


# ── The band, predicted and settled (engine step 2) ──────────────────────────────────────────
# EMT-v5_engine_step2_import_bills_design.md (23 Oct 2026, the user's model): every half-hour's cost
# is kWh × its band's rate; the band predicted from the clock and the dispatches EMT recorded, then
# settled by Octopus's — the bill's label (the four buckets' band on IOG-SMB), the clock on a
# published tariff. Billed wins where it contradicts the prediction (decided). Step 2a's report of
# this rule went in engine step 4e; the settle parity report (step 4a) compares every part.
_BAND_PARITY_GBP = 0.0005          # a cost within 0.05p of today's is the same cost


def _band_rates(bs: str):
    """(off-peak £, peak £) the import schedule gives the half-hour's day, or (None, None)."""
    sched = _kraken_rate_schedules.get("import")
    if sched is None or sched.is_empty():
        return None, None
    try:
        lo, hi = sched.day_rate_bounds(bs)
    except Exception:
        return None, None
    if lo is None or hi is None:
        return None, None
    return round(lo / 100.0, 6), round(hi / 100.0, 6)


def settled_band(bs: str, bill_label, breakdown: "dict | None" = None, *, cost=None, bill_kwh=None,
                 own_kwh=None) -> "str | None":
    """Octopus's band for a half-hour on an Intelligent tariff: 'off_peak' / 'peak' (or 'mixed', the
    buckets carrying the kWh at both), or None — not settled yet, or a ZERO answer, which states no
    band.

    On IOG-SMB the band is the carrying BUCKET's — its unit rate, Octopus's stated price, against the
    day's two rates — not the summary label: on the nights of 27–28 Sep 2026 the label read
    STANDARD_RATE on 16 half-hours inside the night window whose only bucket was at the off-peak 5.493p,
    and Octopus's cost was the off-peak cost (2a's first report, 23 Oct 2026; the 4.5.5 design's "the
    label reserves OFF_PEAK for smart-charge credits"). So on IOG-SMB a label without the buckets settles
    nothing (2a's report on dev, 23 Oct 2026: 22 night half-hours of 26 Aug – 2 Sep labelled STANDARD_RATE,
    no breakdown cached, Octopus's cost the off-peak cost). Elsewhere, the label.

    A bill labelled IMPORTED (`cost`, `bill_kwh`, `own_kwh`; engine step 4a-2, design §6 (e)): the v4
    importer's, from what v4's import stored — Octopus's cost, or kWh × the tariff rate v4 chose by
    Octopus's off-peak label, or a CSV row's cost — since v4 wrote no bill and kept the band only in
    that rate. Its band is the cost ÷ the bill's kWh against the day's two rates, only where that kWh
    is the block's own (v4 4.5.7 refused cost ÷ kWh because it divided by the CAD kWh). The cost must
    match one rate and not the other: a blend of two buckets matches neither, and a tiny kWh both."""
    from kraken_api_client import label_band
    if str(bill_label or "").upper() == IMPORTED_LABEL:
        return _imported_cost_band(bs, cost, bill_kwh, own_kwh)
    if breakdown:
        bands = set(_bucket_bands(bs, breakdown).values())
        if len(bands) == 1:
            return bands.pop()
        if len(bands) == 2:
            return "mixed"
    elif split_at(bs):
        return None
    if not bill_label or str(bill_label).upper() == "ZERO":
        return None
    try:
        b = label_band(bill_label)
    except Exception:
        b = None
    return None if b is None else ("off_peak" if b else "peak")


IMPORTED_LABEL = "IMPORTED"       # v4import.transforms' bill label: banded by its cost ÷ kWh
_IMPORTED_COST_GBP = 0.0005       # an imported cost within 0.05p of kWh × a rate is at that rate


def _sensor_band(bs: str, rate) -> "str | None":
    """The band of a rate a sensor showed (£/kWh, `sensor_rates`): the nearer of the day's two rates,
    within 0.05p of it; None where it is near neither, equally near both, or the day has no two."""
    try:
        r = float(rate)
    except (TypeError, ValueError):
        return None
    lo, hi = _band_rates(bs)
    if lo is None or hi is None:
        return None
    d_lo, d_hi = abs(r - lo), abs(r - hi)
    if min(d_lo, d_hi) > _IMPORTED_COST_GBP or abs(d_lo - d_hi) <= 1e-9:
        return None
    return "off_peak" if d_lo < d_hi else "peak"


def _imported_cost_band(bs: str, cost, bill_kwh, own_kwh) -> "str | None":
    """settled_band's IMPORTED bill: the nearer of the day's two rates its cost is kWh × of, within
    _IMPORTED_COST_GBP; None where it is near neither (a blend) or equally near both."""
    try:
        c, k, own = float(cost), float(bill_kwh), float(own_kwh)
    except (TypeError, ValueError):
        return None
    if k <= 1e-9 or abs(k - own) > 1e-6:
        return None
    lo, hi = _band_rates(bs)
    if lo is None:
        return None
    d_lo, d_hi = abs(c - k * lo), abs(c - k * hi)
    if min(d_lo, d_hi) > _IMPORTED_COST_GBP or abs(d_lo - d_hi) <= 1e-9:
        return None                       # a blend of two buckets, or a tie
    # a tiny kWh is within the tolerance of both: the nearer, whose £ differs from the other's by
    # under 0.05p (engine step 4b: refused, it stayed a candidate of every bill pass for ever —
    # 3,670 such half-hours on dev's 4.5.24 backup)
    return "off_peak" if d_lo < d_hi else "peak"


def _bucket_bands(bs: str, breakdown: dict) -> dict:
    """{'house'|'ev': 'off_peak'|'peak'} — each IOG-SMB bucket that carries kWh, banded by its unit
    rate (Octopus's stated price) against the day's two rates. Octopus's rate, not EMT's stored one:
    the one place a band is read from a rate, and the rate is the bill's."""
    lo, hi = _band_rates(bs)
    out = {}
    if lo is None:
        return out
    for attr, k_kwh, k_rate in (("house", "home_kwh", "home_rate"), ("ev", "ev_kwh", "ev_rate")):
        kw, rt = breakdown.get(k_kwh), breakdown.get(k_rate)
        if kw is None or float(kw) <= 1e-9 or rt is None:
            continue
        out[attr] = "off_peak" if abs(float(rt) - lo) <= abs(float(rt) - hi) else "peak"
    return out


def band_note(bs: str, kwh, bill_label, breakdown=None, live: bool = True, *, cost=None,
              bill_kwh=None) -> "dict | None":
    """The detail panel's band evidence for an Intelligent half-hour with Octopus's answer:
    {settled, predicted, disagrees}. Billed wins, so a disagreement is a note, not an issue (the
    user, 23 Oct 2026: the cell stays green). None on a published tariff, or with no kWh.

    Before EMT's dispatch record began there is nothing to predict a dispatch from, so nothing
    disagrees (`before_record`): imported history's evening charges read "the rules give peak"
    against Octopus's off-peak once the re-import settled them (staging, 23 Oct 2026: 2,758)."""
    try:
        k = float(kwh or 0.0)
    except (TypeError, ValueError):
        return None
    if k <= 1e-9 or tariff_kind_at(bs) != "intelligent":
        return None
    try:
        first = _reader().first_dispatch_record()
    except Exception:
        first = None
    before = first is None or bs < first
    s = settled_band(bs, bill_label, breakdown, cost=cost, bill_kwh=bill_kwh, own_kwh=k)
    p = predicted_band(bs, k, live)
    return {"settled": s, "predicted": p, "before_record": before,
            "disagrees": bool(not before and s in ("off_peak", "peak") and p and s != p)}


def predicted_band(bs: str, kwh: float, live: bool = True, was_online: bool = True) -> "str | None":
    """EMT's own band for a half-hour on an Intelligent tariff — its two stages, as live pricing
    applies them: the clock's rate moved to off-peak by a recorded dispatch slot
    (`_dispatch_overlay_rate`, quietly, at finalise), then on a LIVE half-hour the dispatch's
    lifecycle (`_reconcile_decision`, as `reconcile_dispatch_overlay` decides it at settlement).
    None without a schedule. (The 6-hour cap lives in the four buckets once they land; 2a compares
    the main band.)

    The second stage was missing until 23 Oct 2026 (the user): 16 Aug 2026 13:00 BST has a
    completed dispatch of 0.23 kWh and no `started` — a bump by EMT's own rules, and under the
    0.4 kWh "did not materially run" floor — so peak, as Octopus billed it; the first stage alone
    read off-peak. Imported history is the reconcile's to skip, so the first stage is all it has.
    `was_online` False (a gap-filled half-hour) tells the lifecycle that a missing `started` may be
    lost history, as the reconcile told it (engine step 4c)."""
    sched = _kraken_rate_schedules.get("import")
    lo, hi = _band_rates(bs)
    if sched is None or lo is None:
        return None
    try:
        base = sched.resolve(bs)
    except Exception:
        base = None
    if base is None:
        return None
    rate = _dispatch_overlay_rate("import", bs, round(base / 100.0, 6), kwh, quiet=True)
    band = "off_peak" if abs(rate - lo) <= abs(rate - hi) else "peak"
    if live and abs(round(base / 100.0, 6) - lo) > 1e-6:   # inside the window: nothing to decide
        band = _lifecycle_band(bs, band, was_online) or band
    return band


def _lifecycle_band(bs: str, band: str, was_online: bool = True) -> "str | None":
    """The reconcile's verdict on a live half-hour's dispatch lifecycle, as a band: 'off_peak' /
    'peak', or None where it leaves the price as it is ('review', 'ok', no lifecycle, or a
    not-started slot still inside the settle window, which the reconcile defers). Its inputs as
    `reconcile_dispatch_overlay` builds them; the settled-block guard is not applied — that guard
    keeps a stored price for the bill to decide, and here the bill is what this is compared with."""
    try:
        drows = _reader().get_dispatch_history_for_slot(bs)
    except Exception:
        return None
    if not drows:
        return None
    kinds = {x["kind"] for x in drows}
    has_started = "started" in kinds
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    settle_cutoff = (now - timedelta(hours=_RECONCILE_SETTLE_HOURS)).isoformat()
    if not has_started and bs >= settle_cutoff:
        return None
    completed_energy = next((x["energy_kwh"] for x in drows
                             if x["kind"] == "completed" and x["energy_kwh"] is not None), None)
    _fs = min((x["first_seen"] for x in drows if x["kind"] == "completed" and x["first_seen"]),
              default=None)
    contemporaneous = True
    if _fs:
        try:
            contemporaneous = (datetime.fromisoformat(str(_fs).split(".")[0].replace("Z", ""))
                               - datetime.fromisoformat(bs)) <= timedelta(hours=36)
        except Exception:
            contemporaneous = True
    target, _why = _reconcile_decision(
        has_started, "completed" in kinds, completed_energy, band == "off_peak",
        has_planned=("planned" in kinds), was_online=was_online, contemporaneous=contemporaneous,
        past_settle=(bs < settle_cutoff))
    return target if target in ("off_peak", "peak") else None


# ── Engine step 3a: the ask dry run (EMT-v5_engine_step3_ask_queue_design.md §5) ─────────────────
# What the one selector (§3) would ask Octopus for, beside what the live pass and the drain ask
# today, with why each half-hour differs. Read-only: no Octopus call, nothing written but the report.
_ask_dry_run_state: dict = {"status": "idle"}


def ask_dry_run_status() -> dict:
    return dict(_ask_dry_run_state)

def _ss_other_supplier(supplier) -> bool:
    """settlement_state.other_supplier: a config period labelled with another supplier (G1 design §6)."""
    import settlement_state as _ss
    return _ss.other_supplier(supplier)


def _ask_queue(facts: list, store, mpan: str, now, considered: "dict | None" = None) -> dict:
    """{slot: "bill" | "split"} the one selector (design §3) would ask: settled kWh (DCC, or an
    imported block's own), not corrected, on an Intelligent agreement of its own, under the 6-hour
    gate and above Octopus's two years; a bill where none is cached for this MPAN, a split where an
    IOG-SMB bill is cost-only and a completed dispatch drew, or the half-hour is an API import's (it
    has no dispatch to say); less what is kept, given up or resting.
    Imported history is asked like any other (decided (b)). `considered`, if given, is filled with
    why each half-hour read is left out (the first reason that holds), so a zero says why."""
    from datetime import timedelta as _td
    cut = (now - _td(hours=_RECONCILE_SETTLE_HOURS)).isoformat()
    why = considered if considered is not None else {}

    def _out(reason):
        why[reason] = why.get(reason, 0) + 1
    wait = _bill_split_wait(store)
    kept = {s for s, e in wait.items() if isinstance(e, dict) and e.get("kept")}
    # a held answer follows the backoff too (engine step 3c; it was asked every pass for 28 days)
    held_resting = _bill_ask_resting({s: e for s, e in wait.items() if s not in kept}, now)
    ask, sask = _bill_ask(store, mpan), _split_ask(store)
    gone = {"bill": _bill_ask_given_up(ask, mark=False) | _bill_ask_resting(ask, now),
            "split": _bill_ask_given_up(sask, mark=False) | _bill_ask_resting(sask, now)}
    gone["imported"] = gone["bill"]           # asked as a bill, on the bill's record
    out = {}
    for f in facts:
        sl = f["block_start"]
        settled = f["imp_kwh_api"] is not None or str(f["source"] or "").startswith("imported")
        if sl >= cut:
            _out("under the 6-hour gate"); continue
        if not settled:
            _out("kWh not settled"); continue
        if f["corrected"] or f["rate_source"] == "corrected":
            _out("corrected"); continue
        if _ss_other_supplier(f.get("supplier")):
            _out("another supplier's"); continue
        if tariff_kind_at(sl) != "intelligent":
            _out("not on an Intelligent agreement"); continue
        if sl in kept:
            _out("kept (the hold-back's 28 days ran out)"); continue
        if sl in held_resting:
            _out("held, resting"); continue
        # an API import's half-hour whose only bill is the import's own (IMPORTED: v4's stored cost —
        # its price at Octopus's label, on most of it): asked for Octopus's, after everything else
        # (28 Oct 2026, the user). Octopus's replaces it; where Octopus has no cost it stays, grey.
        # A CSV's is its own bill, and older history is under the floor.
        # A split where an IOG-SMB bill is cost-only and the car drew: a completed dispatch says so on
        # a live half-hour; an API import's has none (Octopus keeps a short dispatch window), and since
        # engine step 2c it arrives with its cost-only bill, so it is asked for its split whatever it
        # drew — as v4's drain asked every imported capped half-hour (4 Nov 2026, a beta tester: a
        # new IOG-SMB install's 2,730 imported half-hours were never asked, and 35 daytime smart
        # charges Octopus billed off-peak stayed at the day rate).
        kind = ("bill" if not f["bill_mpan"]
                else "imported" if (f.get("bill_imported") and f["source"] == "imported_api")
                else "split" if (split_at(sl) and f["cost_only"]
                                 and (f["completed"] or f["source"] == "imported_api")) else None)
        if kind is None:
            _out("bill cached"); continue
        if sl in gone[kind]:
            _out("given up or resting (" + kind + ")"); continue
        out[sl] = kind
        _out("asked (" + kind + ")")
    return out

def _ask_query_estimate(slots: dict) -> dict:
    """About how many Octopus queries the queue's first pass would make: a cost half-hour's local
    day is one query (`recover_measurement_costs`); IOG-SMB breakdowns go in runs under 12
    half-hours (`recover_device_breakdown`)."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from zoneinfo import ZoneInfo
    lon = ZoneInfo("Europe/London")
    days, bd = set(), []
    for sl in slots:
        if bill_kind_at(sl) == "breakdown":
            bd.append(sl)
        else:
            days.add(_dt.fromisoformat(sl).replace(tzinfo=_tz.utc).astimezone(lon).date())
    runs, first = 0, None
    for sl in sorted(bd):
        t = _dt.fromisoformat(sl)
        if first is None or t - first >= _td(minutes=30 * 12):
            runs, first = runs + 1, t
    return {"cost_days": len(days), "breakdown_runs": runs, "queries": len(days) + runs}


async def run_ask_dry_run(out_dir: str) -> dict:
    """What the one asker would ask Octopus for now: a JSON summary (how many bills and splits, about
    how many queries, what it read and why each half-hour was left out) and a CSV of the half-hours,
    to `out_dir`. Read-only on the store; no Octopus call. (Step 3a's report, comparing the selector
    with the live pass and the drain; the comparison went with them in step 3e, 24 Oct 2026.)"""
    import csv as _csv
    import json as _json
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    st = _ask_dry_run_state
    st.clear()
    st.update({"status": "running"})
    try:
        store = _reader()
        mpan = ((_kraken_discovery or {}).get("import") or {}).get("mpan") or ""
        now = _dt.now(_tz.utc).replace(tzinfo=None)
        floor = (now - _td(days=_API_RETENTION_DAYS)).isoformat()
        cut = (now - _td(hours=_RECONCILE_SETTLE_HOURS)).isoformat()
        facts = store.ask_candidate_facts(floor, now.isoformat(), mpan=mpan)
        await asyncio.sleep(0)
        considered: dict = {}
        queue = _ask_queue(facts, store, mpan, now, considered)
        rows = [{"start": sl, "kind": queue[sl]} for sl in sorted(queue, reverse=True)]
        os.makedirs(out_dir, exist_ok=True)
        ts = _dt.now(_tz.utc).strftime("%Y%m%dT%H%M%S")
        csv_path = os.path.join(out_dir, "ask_dry_run_%s.csv" % ts)
        json_path = os.path.join(out_dir, "ask_dry_run_%s.json" % ts)
        with open(csv_path, "w", newline="") as fh:
            w = _csv.DictWriter(fh, fieldnames=["start", "kind"])
            w.writeheader()
            w.writerows(rows)
        summary = {"at": now.isoformat(), "floor": floor, "cutoff": cut,
                   "queue": {"bill": sum(1 for v in queue.values() if v == "bill"),
                             "split": sum(1 for v in queue.values() if v == "split"),
                             "imported": sum(1 for v in queue.values() if v == "imported"),
                             "first_pass": _ask_query_estimate(queue)},
                   # every half-hour with kWh in the two years read, and why each was left out
                   "considered": {"half_hours": len(facts),
                                  "intelligent_agreements": len(_intelligent_windows()),
                                  "by_reason": dict(sorted(considered.items()))},
                   "csv": os.path.basename(csv_path)}
        with open(json_path, "w") as fh:
            _json.dump(summary, fh, indent=1)
        st.update({"status": "done", "summary": summary, "json": json_path, "csv": csv_path})
        logger.info("ask dry run: %s — %s", summary["queue"], summary["considered"]["by_reason"])
        return summary
    except Exception as e:
        logger.warning("ask dry run failed: %s", e, exc_info=e)
        st.update({"status": "error", "error": _error_text("ask dry run", e)})
        return {"ok": False, "error": st["error"]}


# ── Engine step 4a: settle() and its parity report (EMT-v5_engine_step4_settle_design.md §5) ──────
# settle.py decides; settle_facts reads what it decides from. The report sets settle()'s answer
# beside what each half-hour holds today, every difference with its part, and counts what the
# design's §4 moves. Read-only: nothing is written but the report, and no Octopus call.
_settle_parity_state: dict = {"status": "idle"}
_SETTLE_KWH_EPS = 1e-5


def settle_facts(m: dict, subs: list, store, vat_cal=None, *, kwh=None):
    """settle.Facts for one main-meter half-hour (a get_settle_inputs `mains` row, its `subs`), from
    the facts alone: the kWh (DCC once settled, else the reading; an import's own), the tariff, the
    bill's band and buckets, EMT's prediction on that kWh, the EV (the bill's on IOG-SMB, else the
    completed dispatch), the cap, the devices. No stored rate, cost or split is read. `kwh` overrides
    the kWh (the report's CAD comparison)."""
    import settle as _st
    import vat_calendar as _vc
    bs = m["block_start"]
    imported = str(m.get("source") or "").startswith("imported")
    if kwh is None:
        kwh = float(m["imp_kwh_api"] if m.get("imp_kwh_api") is not None else (m.get("imp_kwh") or 0.0))
    end = m.get("block_end") or (datetime.fromisoformat(bs) + timedelta(minutes=30)).isoformat()
    kind = tariff_kind_at(bs)
    f = _st.Facts(start=bs, end=end, kwh=kwh,
                  kind=kind if kind in ("intelligent", "published") else "none",
                  vat=_vc.resolve_vat(bs, vat_cal),
                  devices=[_st.Device(s["meter_id"], max(float(s["imp_kwh"] or 0.0), 0.0),
                                      (s.get("meter_type") or "") in _st.EV_METER_TYPES)
                           for s in (subs or [])],
                  # unclamped only where the main is itself an estimate: a half-hour filled
                  # from HA's recorder is a reading, clamped as a live one (setup gap design)
                  clamp=bool(m.get("imp_kwh_api") is not None
                             or int(m.get("interpolated") or 0) in (0, _RECORDER_INTERPOLATED)))
    sched = _kraken_rate_schedules.get("import")
    if not _agreement_priced_ok(bs):
        # BL-54: a half-hour before the current agreement on a schedule that is not stitched would
        # be priced at the current tariff — no rate, so a caller keeps what it holds (engine step 4c:
        # the PASS 2 re-run, through settle() since 4b, was not gated)
        return f
    if f.kind != "intelligent":
        try:
            r = sched.resolve(bs) if sched is not None and not sched.is_empty() else None
        except Exception:
            r = None
        f.clock_rate = None if r is None else round(r / 100.0, 6)
        return f
    f.lo, f.hi = _band_rates(bs)
    if f.lo is None:
        return f
    bd = None
    if m.get("bill_home_kwh") is not None or m.get("bill_ev_kwh") is not None:
        try:
            bd = store.get_measured_breakdown(bs)
        except Exception:
            bd = None
    f.settled = settled_band(bs, m.get("bill_label"), bd, cost=m.get("bill_cost"),
                             bill_kwh=m.get("bill_kwh"), own_kwh=m.get("imp_kwh"))
    f.settled_legs = _bucket_bands(bs, bd) if bd else {}
    f.predicted = predicted_band(bs, kwh, live=not imported, was_online=not m.get("interpolated"))
    # v4's sensor era (sensor rate fact design §3): the rate Octopus's own sensor showed as the
    # half-hour closed, recorded by v4 before it had Octopus's API or any dispatch record — Octopus's
    # live view, so it outranks EMT's rules where there is no bill (a bill still settles the band)
    _sb = _sensor_band(bs, m.get("sensor_rate"))
    if _sb is not None:
        f.predicted = _sb
    if bd and bd.get("ev_kwh") is not None and split_at(bs):
        f.ev_kwh = min(max(float(bd["ev_kwh"]), 0.0), kwh)
    else:
        f.ev_kwh = dispatch_ev_kwh(bs, kwh, store)
    f.capped = bool(capped_at(bs) and _capped_schedules())
    try:
        f.in_window = sched.is_off_peak(bs) is True
    except Exception:
        f.in_window = False
    if f.capped and f.ev_kwh > 1e-9:
        try:
            eo = _kraken_rate_schedules["ev_device_off_peak"].resolve(bs)
            ep = _kraken_rate_schedules["ev_device_peak"].resolve(bs)
        except Exception:
            eo = ep = None
        f.ev_off = None if eo is None else round(eo / 100.0, 6)
        f.ev_peak = None if ep is None else round(ep / 100.0, 6)
        f.is_boost = _iog_slot_is_boost(bs, store)
        f.boundary = _iog_cap_day_boundary(bs, _iog_site_tz(store), store)
    return f


def _settle_parts(m: dict, subs: list, segs: list, f, s):
    """What of a half-hour settle() would change: (parts, device notes, the stored EV kWh). `parts`
    names each that differs — kwh, split, band, cost, rate, ex-VAT, segments, devices — empty where
    the stored half-hour is settle()'s. The settle parity report's comparison, and the writers'
    test of whether to write (engine step 4c)."""
    parts = []
    segs = segs or []
    stored_kwh = float(m.get("imp_kwh") or 0.0)
    if abs(stored_kwh - f.kwh) > _SETTLE_KWH_EPS:
        parts.append("kwh")
    st_ev = sum(float(g["kwh"] or 0.0) for g in segs if g["attribution"] == "ev")
    if abs(st_ev - s.ev_kwh) > _SETTLE_KWH_EPS:
        parts.append("split")
    want = sorted((g.attribution, g.band) for g in s.legs)
    have = sorted((g["attribution"], g["band"]) for g in segs)
    if want != have:
        parts.append("band")
    if m.get("imp_cost") is None or abs(float(m["imp_cost"]) - s.imp_cost) > _BAND_PARITY_GBP:
        parts.append("cost")
    if m.get("imp_rate") is None or abs(float(m["imp_rate"]) - s.imp_rate) > 1e-6:
        parts.append("rate")
    if m.get("imp_cost_exc") is None or abs(float(m["imp_cost_exc"]) - s.imp_cost_exc) > _BAND_PARITY_GBP:
        parts.append("ex-VAT")
    seg_cost = sum(float(g["kwh"] or 0.0) * float(g["inc_rate"] or 0.0) for g in segs)
    if not segs or abs(seg_cost - s.imp_cost) > _BAND_PARITY_GBP:
        parts.append("segments")
    stored_dev = {x["meter_id"]: x for x in (subs or [])}
    dev_txt = []
    for d in s.devices:
        x = stored_dev.get(d.meter_id) or {}
        g = float(x.get("imp_kwh_grid") if x.get("imp_kwh_grid") is not None else (x.get("imp_kwh") or 0.0))
        c = float(x.get("imp_cost") or 0.0)
        if abs(g - d.kwh_grid) > _SETTLE_KWH_EPS or abs(c - d.cost) > _BAND_PARITY_GBP:
            # the device's own read, then its grid share and cost stored → settle()'s (4a-2: staging's
            # report said a device differed, not from what)
            dev_txt.append("%s read %.4f: %.4f kWh £%.4f→%.4f kWh £%.4f"
                           % (d.meter_id, float(x.get("imp_kwh") or 0.0), g, c, d.kwh_grid, d.cost))
    if dev_txt:
        parts.append("devices")
    return parts, dev_txt, st_ev


def settle_half_hour(store, bs: str) -> dict:
    """A fact arrived for `bs`: settle it, and write it where it differs from what it holds (engine
    step 4c — the carve's and the reconcile's trigger). A bill's answer is written as the bill apply
    writes it (rate_source 'settled'); a prediction or the clock without changing who set it. A
    correction is the user's, and a half-hour settle() cannot price is left as it is.
    {status, parts, written}."""
    try:
        end = (datetime.fromisoformat(bs) + timedelta(minutes=30)).isoformat()
    except ValueError:
        return {"status": "no_block", "parts": [], "written": False}
    inp = store.get_settle_inputs(bs, end)
    m = next((x for x in inp["mains"] if x["block_start"] == bs), None)
    if m is None:
        return {"status": "no_block", "parts": [], "written": False}
    try:
        cal = store.get_vat_calendar()
    except Exception:
        cal = None
    return _settle_write(store, m, inp["subs"].get(bs, []), inp["segments"].get(bs, []), cal)


def _settle_write(store, m: dict, subs: list, segs: list, cal) -> dict:
    """settle_half_hour's settle-and-write for one get_settle_inputs `mains` row with its devices
    and segments — the sweep's too, over a day's rows read at once (engine step 4d)."""
    bs = m["block_start"]
    if m.get("rate_corrected") or m.get("rate_source") == "corrected":
        return {"status": "corrected", "parts": [], "written": False}
    import settle as _st
    f = settle_facts(m, subs, store, cal)
    s = _st.settle(f)
    if s.status not in ("settled", "predicted", "clock"):
        return {"status": s.status, "parts": [], "written": False}
    parts, _dev, _ev = _settle_parts(m, subs, segs, f, s)
    if not parts:
        return {"status": s.status, "parts": [], "written": False}
    w = store.apply_measured_to_main if s.status == "settled" else store.write_predicted
    w(bs, rate=s.imp_rate, cost=s.imp_cost, cost_exc=s.imp_cost_exc,
      segments=_settled_segments(s), devices=_settled_devices(f, s))
    return {"status": s.status, "parts": parts, "written": True}


def settle_parity_row(m: dict, subs: list, segs: list, store, vat_cal=None) -> dict:
    """One main-meter half-hour: settle()'s answer beside what it holds. `status`: same | differs |
    corrected | empty | no_rate | no_band; `parts`, for a differs, the parts that differ (kwh, band,
    split, rate, cost, ex-VAT, segments, devices). `two_rates` and `device_two_rates` count what §4 (a)
    and (b) decide; `dcc_moves` a predicted band the DCC kWh moves from the CAD reading's (§4 (d))."""
    import settle as _st
    bs = m["block_start"]
    out = {"start": bs, "status": None, "parts": "", "kwh": m.get("imp_kwh"),
           "kwh_dcc": m.get("imp_kwh_api"), "stored_rate": m.get("imp_rate"),
           "stored_cost": m.get("imp_cost"), "stored_bands": "", "settle_by": None, "band": None,
           "rate": None, "cost": None, "stored_ev": None, "ev": None, "two_rates": False,
           "device_two_rates": False, "dcc_moves": "", "devices": ""}
    if m.get("rate_corrected") or m.get("rate_source") == "corrected":
        out["status"] = "corrected"        # the user's own figure: a fact settle() does not hold yet
        return out
    f = settle_facts(m, subs, store, vat_cal)
    s = _st.settle(f)
    out.update({"settle_by": s.status, "band": s.band, "rate": s.imp_rate, "cost": s.imp_cost,
                "two_rates": s.two_rates})
    segs = segs or []
    out["stored_bands"] = "+".join("%s/%s" % (g["attribution"], g["band"]) for g in segs)
    # §4 (d): would the CAD reading have predicted another band? Only where no bill settles it.
    cad = m.get("cad_kwh")
    if (f.kind == "intelligent" and s.status == "predicted" and m.get("imp_kwh_api") is not None
            and cad is not None and abs(float(cad) - f.kwh) > _SETTLE_KWH_EPS):
        p_cad = predicted_band(bs, float(cad), live=not str(m.get("source") or "").startswith("imported"),
                               was_online=not m.get("interpolated"))
        if p_cad and p_cad != f.predicted:
            out["dcc_moves"] = "%s→%s" % (p_cad, f.predicted)
    if s.status in ("empty", "no_rate", "no_band"):
        out["status"] = s.status
        return out
    drawing = {d.meter_id for d in s.devices if d.kwh_grid > _SETTLE_KWH_EPS}
    out["device_two_rates"] = bool(s.two_rates and drawing)
    parts, dev_txt, st_ev = _settle_parts(m, subs, segs, f, s)
    out["stored_ev"], out["ev"] = round(st_ev, 6), s.ev_kwh
    out["devices"] = "; ".join(dev_txt)
    out["parts"] = "+".join(parts)
    out["status"] = "differs" if parts else "same"
    return out


def settle_parity_status() -> dict:
    return dict(_settle_parity_state)


async def run_settle_parity(utc_from: str, utc_to: str, out_dir: str) -> dict:
    """Step 4a's report over [utc_from, utc_to): every main-meter half-hour under settle(), a month at
    a time, yielding between months. Writes `settle_parity_<ts>.json` (the summary: by status, by
    part, by month, and §4's counts) and `.csv` (every half-hour not `same`, and every one §4 moves)
    to `out_dir`. Read-only on the store; no Octopus call."""
    import csv as _csv
    import json as _json
    st = _settle_parity_state
    st.clear()
    st.update({"status": "running", "from": utc_from, "to": utc_to, "done_to": utc_from})
    try:
        if _store is None:
            raise RuntimeError("no store")
        ensure_dir(out_dir)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        csv_path = os.path.join(out_dir, "settle_parity_%s.csv" % ts)
        json_path = os.path.join(out_dir, "settle_parity_%s.json" % ts)
        counts: dict = {}
        by_part: dict = {}
        months: dict = {}
        moves = {"two_rates": 0, "device_two_rates": 0, "dcc_moves": 0}
        tot = {"stored": 0.0, "settle": 0.0}
        fields = ["start", "status", "parts", "settle_by", "band", "stored_bands", "kwh", "kwh_dcc",
                  "stored_rate", "rate", "stored_cost", "cost", "stored_ev", "ev", "two_rates",
                  "device_two_rates", "dcc_moves", "devices"]
        store = _reader()
        try:
            vat_cal = store.get_vat_calendar()
        except Exception:
            vat_cal = None
        a = datetime.fromisoformat(utc_from)
        end = datetime.fromisoformat(utc_to)
        with open(csv_path, "w", newline="") as fh:
            w = _csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            while a < end:
                b = min(end, (a.replace(day=1) + timedelta(days=32)).replace(day=1))
                inp = store.get_settle_inputs(a.isoformat(), b.isoformat())
                for mrow in inp["mains"]:
                    bs = mrow["block_start"]
                    r = settle_parity_row(mrow, inp["subs"].get(bs, []), inp["segments"].get(bs, []),
                                          store, vat_cal)
                    counts[r["status"]] = counts.get(r["status"], 0) + 1
                    for p in filter(None, r["parts"].split("+")):
                        by_part[p] = by_part.get(p, 0) + 1
                    mo = months.setdefault(bs[:7], {"same": 0, "differs": 0, "stored": 0.0,
                                                    "settle": 0.0})
                    if r["status"] in mo:
                        mo[r["status"]] += 1
                    if r["cost"] is not None and r["stored_cost"] is not None:
                        mo["stored"] += float(r["stored_cost"]); mo["settle"] += r["cost"]
                        tot["stored"] += float(r["stored_cost"]); tot["settle"] += r["cost"]
                    moves["two_rates"] += int(r["two_rates"])
                    moves["device_two_rates"] += int(r["device_two_rates"])
                    moves["dcc_moves"] += int(bool(r["dcc_moves"]))
                    if r["status"] != "same" or r["two_rates"] or r["dcc_moves"]:
                        w.writerow(r)
                st["done_to"] = b.isoformat()
                a = b
                await asyncio.sleep(0)               # one month at a time: the loop breathes
        summary = {"from": utc_from, "to": utc_to, "counts": counts,
                   "by_part": dict(sorted(by_part.items(), key=lambda kv: -kv[1])),
                   # what the design's §4 moves: legs at two rates (a), with a device drawing (b),
                   # predicted bands the DCC kWh moves from the CAD reading's (d)
                   "section4": moves,
                   "totals_gbp": {k: round(v, 2) for k, v in tot.items()},
                   "months": {k: {kk: (round(vv, 2) if isinstance(vv, float) else vv)
                                  for kk, vv in v.items()} for k, v in sorted(months.items())},
                   "csv": os.path.basename(csv_path)}
        with open(json_path, "w") as fh:
            _json.dump(summary, fh, indent=1)
        st.update({"status": "done", "summary": summary, "json": json_path, "csv": csv_path})
        logger.info("settle parity: %s..%s — %s; parts %s; §4 %s; stored £%.2f, settle £%.2f",
                    utc_from, utc_to, counts, by_part, moves, tot["stored"], tot["settle"])
        return summary
    except Exception as e:
        logger.warning("settle parity: failed: %s", e, exc_info=e)
        st.update({"status": "error", "error": _error_text("settle parity", e)})
        return {"ok": False, "error": st["error"]}


# ── Re-fetch a range from Octopus: preview, then apply (ROADMAP BL-86) ─────────────────────
# Octopus restates history — #505's October VAT, 10 Oct 2026's split two days later — and EMT asks
# once and keeps the answer. The user's only lever was to delete the range and import it again,
# which loses the recorded device history. This fetches the range's kWh (DCC), bill and split INTO
# MEMORY, compares them with what is stored, and reports only what differs (settlement surfaces
# design §5.2). Nothing is written until apply, which writes only those half-hours, through the
# paths that already exist, after a safety backup taken by the route. 4.5.22's VAT resync and
# 4.5.23's re-fetch are special cases of it.
_REFETCH_MAX_DAYS = 31              # design §7 (e): a longer range runs month by month
_REFETCH_TTL_S = 3600               # a preview older than this is stale
_REFETCH_KWH_EPS = 0.0005
_REFETCH_GBP_EPS = 0.00001          # a thousandth of a penny (settlement_state's tolerance)
_REFETCH_PARTS = ("kwh", "bill")
_refetch_state: dict = {"status": "idle"}
_refetch_preview: dict = {}         # the one preview held: {token, ...}


def refetch_status() -> dict:
    """The job's state, for GET /api/settlement/refetch/status."""
    return dict(_refetch_state)


def refetch_claim() -> tuple:
    """Claim the job before any slow work (the route's synchronous step). (ok, reason)."""
    import job_coordinator as _jc
    if _refetch_state.get("status") == "running":
        return False, "a re-fetch is already running"
    if delete_in_progress() or api_import_running():
        return False, "an import or a delete is running"
    ok, why = _jc.claim("range_refetch")
    if ok:
        _refetch_state.clear()
        _refetch_state.update({"status": "running", "phase": "starting"})
    return ok, why


def _refetch_check_range(utc_from: str, utc_to: str) -> "str | None":
    try:
        a, b = datetime.fromisoformat(utc_from), datetime.fromisoformat(utc_to)
    except ValueError:
        return "from and to must be ISO date-times"
    if b <= a:
        return "the range is empty"
    if (b - a).total_seconds() > _REFETCH_MAX_DAYS * 86400 + 1:
        return "at most %d days at a time" % _REFETCH_MAX_DAYS
    return None


def _refetch_fp(store, utc_from: str, utc_to: str) -> str:
    """What says the range has not been written since the preview: the data epoch and the
    range's day fingerprints."""
    import json as _json
    return _json.dumps([store.get_data_epoch(), store.day_fingerprints(utc_from, utc_to)],
                       sort_keys=True, default=str)


async def _refetch_octopus(store, utc_from: str, utc_to: str, parts, facts: dict) -> dict:
    """{slot: {"kwh": float?, "exp_kwh": float?, "bill": parsed row?}} — Octopus's answer for the
    range, in memory only."""
    from kraken_ingester import normalise_to_naive_utc as _nn
    out: dict = {}
    if "kwh" in parts:
        ing = _build_kraken_ingester()
        if ing is not None:
            _refetch_state["phase"] = "fetching kWh"
            for chan, mpan, serial in (("kwh", ing.import_mpan, ing.import_serial),
                                       ("exp_kwh", ing.export_mpan, ing.export_serial)):
                if not (mpan and serial):
                    continue
                rows = await _kraken_client.get_consumption(
                    mpan, serial, period_from=utc_from + "Z", period_to=utc_to + "Z")
                for r in rows or []:
                    if r.get("interval_start") is None or r.get("consumption") is None:
                        continue
                    out.setdefault(_nn(r["interval_start"]), {})[chan] = float(r["consumption"])
    if "bill" in parts:
        imp = (_kraken_discovery or {}).get("import") or {}
        mpan = imp.get("mpan")
        # every half-hour in the range that imported, on an Intelligent tariff — wider than the
        # settled-cost pass's own scope (its floor, the current agreement's start; dispatched only
        # off SMB) and corrected half-hours too: the user asked, and a red cell outside that scope
        # has no other remedy (design §5.2). A published-rate half-hour needs no bill — its settled
        # kWh x rate is the bill — and applying one would snap its rate to the day's lowest or
        # highest band, a two-rate model (design §9.2). The automatic passes keep their scope.
        slots = [s for s, f in facts.items()
                 if (f.get("imp_kwh") or 0) > 1e-9 and tariff_kind_at(s) == "intelligent"]
        if mpan and slots:
            _refetch_state["phase"] = "fetching bills (%d half-hours)" % len(slots)
            # The device breakdown for half-hours on the IOG-SMB agreement, the billed cost for the
            # rest (`bill_kind_at`; see `_fetch_bill_nodes` for why an unanswered breakdown
            # half-hour is not asked the billed cost).
            nodes = await _fetch_bill_nodes(mpan, slots)   # one bill reader (design §1.3)
            for s in slots:
                kind, node = nodes.get(s, (None, None))
                row = parse_bill(node, kind, facts[s].get("imp_kwh"))
                if row and not row.get("zero"):
                    out.setdefault(s, {})["bill"] = row
            # what Octopus did not answer, so the dialog never says "matches" over a gap
            out.setdefault("_meta", {})["no_bill"] = sorted(
                s for s in slots if "bill" not in out.get(s, {}))
    return out


def _refetch_diff(facts: dict, octo: dict) -> list:
    """One row per half-hour where Octopus now differs from what is stored — compared like with
    like (PR 5 on the Pi, 14 Oct 2026: a first version listed 401 of 1,488 half-hours over two
    months, nearly all no-ops):
    - kWh: a DCC-settled block against its DCC figure; IMPORTED history against its own kWh (it
      came from the same API and has no DCC field by design) — reported, never written as DCC;
      export with none stored reads as 0.
    - the bill: against the cached bill where there is one, else against the stored cost — and a
      block whose stored cost is not its cached bill differs too (`unapplied`). A bill
      where none is held is a difference even at the same £ (`new_bill`): it turns EMT's kWh x
      rate into Octopus's cost (design §1.2, revised 14 Oct 2026). The EV share against the
      bill's, else EMT's own split.
    - a row where nothing would change is dropped: a bill 4.5.23 would hold whose cost agrees
      changes nothing either way. The bill's split arriving on a cost-only cached bill is a change, even
      at the same figures, on a completed dispatch or where the bill has the car."""
    import settlement_state as _ss
    def _f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    rows = []
    for s in sorted(octo):
        f, o = facts.get(s), octo[s]
        if f is None:
            continue                                  # no block here: nothing to correct
        imported = (f.get("source") or "").startswith("imported")
        corrected = bool(f.get("rate_corrected")) or f.get("rate_source") == "corrected"
        r: dict = {"start": s}
        if "kwh" in o:
            stored = _f(f.get("imp_kwh")) if imported else _f(f.get("imp_kwh_api"))
            if stored is None or abs(stored - o["kwh"]) > _REFETCH_KWH_EPS:
                r["kwh"] = {"stored": stored, "octopus": round(o["kwh"], 6), "imported": imported}
        if "exp_kwh" in o:
            stored = _f(f.get("exp_kwh") if imported else f.get("exp_kwh_api"))
            # no export figure stored, and Octopus says none was exported: nothing to change
            nothing = stored is None and o["exp_kwh"] <= _REFETCH_KWH_EPS \
                and (_f(f.get("exp_kwh")) or 0.0) <= _REFETCH_KWH_EPS
            if not nothing and (stored is None or abs(stored - o["exp_kwh"]) > _REFETCH_KWH_EPS):
                r["export"] = {"stored": stored, "octopus": round(o["exp_kwh"], 6), "imported": imported}
        b = o.get("bill")
        if b is not None:
            bd = b.get("breakdown")
            ev_new = _f(bd[2]) if bd else None
            have = _f(f.get("bill_cost"))
            # a corrected block holds the user's figure, not the cached bill: compare with that
            cost_now = have if have is not None and not corrected else _f(f.get("imp_cost"))
            cost_diff = cost_now is None or abs(cost_now - float(b["cost_incl"])) > _REFETCH_GBP_EPS
            ev_stored = _f(f.get("bill_ev_kwh"))
            ev_is_bill = ev_stored is not None
            if not ev_is_bill:
                ev_stored = _f(f.get("ev_kwh")) or 0.0       # EMT's own split, or none
            ev_diff = ev_new is not None and abs(ev_stored - ev_new) > _REFETCH_KWH_EPS
            # the bill's split where only EMT's was held: a change even at the same figures — the
            # half-hour's split moves from EMT's own to the bill's (PR 5 on the Pi, 14 Oct 2026:
            # two half-hours wholly on the car stayed "EMT's split" after 8 Sep was applied)
            # (a cached bill only: one not in yet at the same £ is still no difference)
            arrives = (ev_new is not None and not ev_is_bill and have is not None
                       and (bool(f.get("completed")) or ev_new > _REFETCH_KWH_EPS))
            held = bool(bd and _bill_drops_ev(
                emt_ev_reference(f.get("ev_kwh"), f.get("bill_ev_kwh")), ev_new))
            if held and not cost_diff:
                ev_diff = False                          # held, and the £ agrees: nothing changes
            if held and not cost_diff:
                arrives = False                          # held: the bill's split is not taken
            # no bill held at all, and Octopus has one: a change even at the same £ — the half-hour
            # goes from EMT's kWh x rate to Octopus's cost, red or amber to green (design §1.2,
            # revised 14 Oct 2026; 19 Aug 06:30 on the Pi, £0.0003 either way)
            new_bill = have is None and not corrected and not held
            # a correction Octopus's bill agrees with: still a change — the bill replaces it and the
            # half-hour becomes an ordinary billed one (PR 5 on the Pi, 15 Oct 2026: 24 Aug 22:00
            # BST, corrected to the day rate, £0.052341 = the bill, stayed blue and un-replaceable)
            confirms = corrected and not held and not cost_diff
            # the block does not carry the bill it has cached: a difference even where Octopus
            # still agrees with the cache — the apply puts the bill on the block (22 Oct 2026,
            # staging: 5 Mar 2025 20:00 held £0.2986 against a cached £1.0836, and the preview,
            # comparing Octopus with the cache alone, said nothing differed)
            block_cost = _f(f.get("imp_cost"))
            unapplied = (have is not None and not corrected and not held and block_cost is not None
                         and abs(block_cost - float(b["cost_incl"])) > _REFETCH_GBP_EPS)
            if unapplied and not cost_diff:
                cost_now = block_cost                    # what the block holds is what changes
            # the devices cost more than the meter (device cost check design §3): a change even
            # where Octopus agrees with the meter — the apply settles the half-hour again, and
            # settle() prices the devices on their grid shares at its rates
            devices = not held and _ss.devices_exceed(f)
            if cost_diff or ev_diff or arrives or new_bill or confirms or unapplied or devices:
                r["bill"] = {"stored": cost_now, "unapplied": unapplied,
                             "stored_is_bill": have is not None and not corrected and not unapplied,
                             "corrected": corrected, "new_bill": new_bill,
                             "octopus": b["cost_incl"], "stored_ev": ev_stored,
                             "stored_ev_is_bill": ev_is_bill,
                             "octopus_ev": ev_new, "held": held, "devices": devices}
        if len(r) > 1:
            rows.append(r)
    return rows


@job("range_refetch")
async def run_refetch_preview(utc_from: str, utc_to: str, parts=_REFETCH_PARTS) -> dict:
    """Fetch the range from Octopus into memory and work out what differs. Writes nothing.
    The route claims the job first (refetch_claim)."""
    import job_coordinator as _jc
    import secrets as _secrets
    global _refetch_preview
    st = _refetch_state
    try:
        _jc.mark("range_refetch", status="running")
        st.update({"status": "running", "phase": "reading the range", "from": utc_from,
                   "to": utc_to, "parts": list(parts)})
        if _store is None or _kraken_client is None:
            raise RuntimeError("no supplier API connected")
        facts = {f["block_start"]: f for f in _store.get_settlement_facts(utc_from, utc_to)}
        fp = _refetch_fp(_store, utc_from, utc_to)
        octo = await _refetch_octopus(_store, utc_from, utc_to, parts, facts)
        no_bill = (octo.pop("_meta", None) or {}).get("no_bill") or []
        rows = _refetch_diff(facts, octo)
        token = _secrets.token_hex(8)
        _refetch_preview = {"token": token, "from": utc_from, "to": utc_to, "parts": list(parts),
                            "made": datetime.now(timezone.utc).timestamp(), "fp": fp,
                            "rows": rows, "octo": {r["start"]: octo[r["start"]] for r in rows}}
        summary = {"checked": len(facts), "differ": len(rows),
                   "kwh": sum("kwh" in r for r in rows), "export": sum("export" in r for r in rows),
                   "bill": sum("bill" in r for r in rows),
                   "held": sum(bool(r.get("bill", {}).get("held")) for r in rows),
                   "corrected": sum(bool(r.get("bill", {}).get("corrected"))
                                    and not r["bill"].get("held") for r in rows),
                   "devices": sum(bool(r.get("bill", {}).get("devices")) for r in rows),
                   "no_bill": len(no_bill)}
        st.update({"status": "done", "phase": "preview", "token": token, "summary": summary,
                   "rows": rows[:500]})
        logger.info("range re-fetch: preview %s..%s — %s", utc_from, utc_to, summary)
        return summary
    except Exception as e:
        logger.warning("range re-fetch: preview failed: %s", e, exc_info=e)
        st.update({"status": "error", "error": _error_text("re-fetch preview", e)})
        return {"ok": False, "error": st["error"]}
    finally:
        _jc.mark("range_refetch", status=st.get("status") or "done")


def refetch_apply_check(token: str, store=None) -> "str | None":
    """Why `token` cannot be applied, or None. The route calls it before the safety backup with
    its OWN read store (store_access.store()): `_store` is the engine loop's connection, and a
    web thread must never use it (it is opened check_same_thread=False, so nothing would stop
    it). The apply coroutine, on the loop, passes nothing and uses `_store`."""
    p = _refetch_preview
    st = store if store is not None else _store
    if not token or p.get("token") != token:
        return "that preview is no longer held — preview again"
    if datetime.now(timezone.utc).timestamp() - p["made"] > _REFETCH_TTL_S:
        return "that preview is over an hour old — preview again"
    if st is None or _refetch_fp(st, p["from"], p["to"]) != p["fp"]:
        return "the range has changed since the preview — preview again"
    return None


@job("range_refetch")
async def run_refetch_apply(token: str) -> dict:
    """Write the previewed differences: the settled kWh (upsert_kraken_block, which queues the
    block's re-run), the bill through the asker's own write (`_store_bill`), then
    apply_measured_to_block, the split with it — except a bill 4.5.23 would hold. A corrected
    half-hour is fetched too, and its bill replaces the correction (design §5.2). The route has
    checked the token (refetch_apply_check) and taken the safety backup.

    The ask record follows (engine step 3d, 24 Oct 2026): a half-hour whose bill is written leaves
    the bill and split asks, a bill with the car releases its hold, and one 4.5.23 holds is held
    there — as the one asker does. One batch for the whole apply: a month is ~1,500 writes."""
    import job_coordinator as _jc
    global _refetch_preview
    st, p = _refetch_state, _refetch_preview
    done = {"kwh": 0, "export": 0, "bill": 0, "held": 0, "not_settled": 0, "corrections": 0}
    try:
        _jc.mark("range_refetch", status="running")
        st.update({"status": "running", "phase": "applying"})
        why = refetch_apply_check(token)
        if why:
            raise RuntimeError(why)
        imp = (_kraken_discovery or {}).get("import") or {}
        mpan = imp.get("mpan") or ""
        with _store.batch():
            _refetch_write(p, mpan, done)
        _refetch_preview = {}
        st.update({"status": "done", "phase": "applied", "applied": done, "token": None})
        logger.info("range re-fetch: applied %s..%s — %s", p.get("from"), p.get("to"), done)
        return done
    except Exception as e:
        logger.warning("range re-fetch: apply failed: %s", e, exc_info=e)
        st.update({"status": "error", "error": _error_text("re-fetch apply", e)})
        return {"ok": False, "error": st["error"]}
    finally:
        _jc.mark("range_refetch", status=st.get("status") or "done")


def _refetch_write(p: dict, mpan: str, done: dict) -> None:
    """The re-fetch's writes and the ask record's changes — synchronous, for one batch."""
    store = _store
    wait = _bill_split_wait(store)
    bills, splits = _bill_ask(store, mpan), _split_ask(store)
    before = (_snapshot(wait), _snapshot(bills), _snapshot(splits))
    for r in p["rows"]:
        s, o = r["start"], p["octo"][r["start"]]
        # imported history keeps its own kWh: a difference there is reported, not written
        # as DCC (it would mark imported history settled and queue it for re-runs)
        if "kwh" in r and not r["kwh"].get("imported"):
            _store.upsert_kraken_block(s, "electricity_main", o["kwh"], source="kraken_api",
                                       billing_source="api")
            done["kwh"] += 1
        if "export" in r and not r["export"].get("imported"):
            _store.upsert_kraken_block(s, "electricity_main", o["exp_kwh"], channel="export",
                                       source="kraken_api", billing_source="api")
            done["export"] += 1
        if "bill" in r:
            if r["bill"]["held"]:
                _bill_split_hold(wait, s)                  # held, as the asker holds it
                done["held"] += 1
                continue
            b = o["bill"]
            _store_bill(store, s, mpan, b)
            _bill_ask_answered(bills, s)
            if b.get("breakdown"):
                _bill_ask_answered(splits, s)
                _bill_split_release(wait, s)
            # the bill is stored either way; whether the block took it is the applier's call
            # (it refuses, e.g., with no tariff rate to snap to) — counted apart, not as applied
            # a corrected half-hour: the bill replaces the user's correction (design §5.2) —
            # the flag clears in the settle's own UPDATE, so a refusal leaves it as it was
            corr = bool(r["bill"].get("corrected"))
            try:
                took = apply_measured_to_block(s, cost_incl=float(b["cost_incl"]),
                                               cost_excl=b["cost_excl"], label=b["label"],
                                               replace_correction=corr)
            except Exception as e:
                logger.warning("range re-fetch: re-apply failed for %s: %s", s, e)
                took = False
            if took:
                done["bill"] += 1
                if corr:
                    done["corrections"] += 1
                    logger.info("range re-fetch: %s — the bill replaced the user's correction", s)
            else:
                done["not_settled"] += 1
                logger.warning("range re-fetch: %s — the bill is stored, the block did not "
                               "re-settle from it", s)
    _save_changes(store, before[0], wait, lambda: _bill_split_wait(store), _bill_split_wait_save)
    _save_changes(store, before[1], bills, lambda: _bill_ask(store, mpan), _bill_ask_save)
    _save_changes(store, before[2], splits, lambda: _split_ask(store), _split_ask_save)


_MEASURED_HISTORY_DRAIN_PACE = 0.4        # seconds between drain batches (test-overridable)
# ONE settleability predicate, shared by every settlement pass (drain candidates, drain
# backlog, apply_measured_settled). A block may take Octopus's billed cost as authoritative
# only if it is DCC-settled (imp_kwh_api present) OR an imported block — NEVER an unsettled
# live block, which would be priced off a provisional CAD kWh (4.5.12). Keep the three call
# sites on this single constant so they cannot drift apart.
# _SETTLEABLE_SQL moved to BlockStore (workstream A acceptance 5). It was spliced
# into three queries here by string concatenation and an f-string; all three are
# now store methods that share the class constant.
_measured_history_drain_running = False   # re-entry guard for the scheduled drain task


@job("measured_drain", depends_on=("import",), exclusive_with=("delete",))
async def run_measured_history_drain(max_batches: int = 5000) -> dict:
    """The one asker (engine step 3, EMT-v5_engine_step3_ask_queue_design.md §3): asks Octopus for
    every half-hour the one selector (`_ask_selection`) picks — a bill where none is cached, a split
    where an IOG-SMB bill is cost-only and the car drew — newest first, a page at a time (up to
    `_ASK_PAGE_DAYS` local days of cost half-hours, `_ASK_PAGE_BREAKDOWN` breakdown ones), and
    handles every answer one way:

    - a bill → cached (`_store_bill`); a zero one (EMT's kWh zero too) → cached as ZERO;
    - one that drops the car where EMT has it (4.5.23) → held, on the same backoff;
    - none, or a split asked and no breakdown given → unanswered (hourly for 2 days, then daily,
      given up at 30);
    - an API error (`_bill_fetch_failures`) → nothing marked; the pass stops, the next one asks again.

    Before each page the allowance: under `_ASK_HEADROOM` of it left, the pass stops (decided (c)).
    After each page the records are saved and the cached bills applied (`_apply_bills_paced`).
    Replaces the live pass (`measure_settled_dispatched_blocks`) and the drain's two phases;
    progress in `_ask_state`, for the banner."""
    store = _store
    st = _ask_state
    if store is None or _kraken_client is None or not _any_intelligent():
        return {"ok": True, "skipped": "not applicable"}
    mpan = ((_kraken_discovery or {}).get("import") or {}).get("mpan")
    if not mpan:
        return {"ok": True, "skipped": "no mpan"}
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc).replace(tzinfo=None)
    # mark (and log, once each) what has gone 30 days unanswered: the surfaces read when
    # ("abandoned", `no_cost_since`), as they did from the live pass
    for read, save in ((lambda: _bill_ask(store, mpan), _bill_ask_save),
                       (lambda: _split_ask(store), _split_ask_save)):
        rec = read()
        before = _snapshot(rec)
        _bill_ask_given_up(rec)
        _save_changes(store, before, rec, read, save)
    sel = _ask_selection(store, now)
    pages = _ask_pages(sel)
    st.clear()
    st.update({"status": "running", "total": len(sel), "done": 0, "stored": 0, "held": 0,
               "started": _dt_now_iso_safe(), "reason": None})
    try:
        for page in pages[:max_batches]:          # pages, now (the drain's batches until step 3c)
            if not await _ask_allowance_ok():
                st["reason"] = "allowance"
                logger.info("octopus asks: under %d%% of the allowance left — stopping, %d of %d "
                            "asked; the next pass carries on", int(_ASK_HEADROOM * 100),
                            st["done"], st["total"])
                break
            wait = _bill_split_wait(store)
            wait_before = _snapshot(wait)
            kept = _bill_split_kept(store, wait)
            bills, splits = _bill_ask(store, mpan), _split_ask(store)
            bills_before, splits_before = _snapshot(bills), _snapshot(splits)
            page = [s for s in page if s not in kept]
            emt_ev = emt_ev_at(store, page)
            try:
                nodes = await _fetch_bill_nodes(mpan, page)
            except Exception as e:
                logger.warning("octopus asks: fetch failed: %s", e)
                st["reason"] = "error"
                break
            failed = _bill_fetch_failures
            kwh = store.get_imp_kwh(page)
            # the page's writes in one commit: on dev's storage each commit could hold the loop for
            # seconds, and two a half-hour dropped HA's connection twice in its first pass (24 Oct
            # 2026, the support file). No await inside the batch.
            with store.batch():
                _ask_write_page(store, mpan, page, sel, nodes, kwh, emt_ev, failed, st,
                                (wait, wait_before), (bills, bills_before), (splits, splits_before))
            st["done"] += len(page)
            await _apply_bills_paced()
            if failed:
                st["reason"] = "error"
                logger.warning("octopus asks: %d window(s) failed — stopping; the next pass asks "
                               "again (none counted as unanswered)", failed)
                break
            await asyncio.sleep(_MEASURED_HISTORY_DRAIN_PACE)
        logger.info("octopus asks: %d of %d asked, %d bill(s) cached, %d held%s",
                    st["done"], st["total"], st["stored"], st["held"],
                    "" if not st.get("reason") else " — stopped (%s)" % st["reason"])
        return {"ok": True, "asked": st["done"], "stored": st["stored"], "held": st["held"],
                "settled": st["stored"], "reason": st.get("reason")}
    finally:
        st["status"] = "idle"
        st["finished"] = _dt_now_iso_safe()


def _ask_write_page(store, mpan: str, page: list, sel: dict, nodes: dict, kwh: dict, emt_ev: dict,
                    failed: int, st: dict, wait_pair, bills_pair, splits_pair) -> None:
    """One page's answers written, and the records' changes saved — synchronous, for one batch.
    Each `*_pair` is (the record as this page changed it, as it was read)."""
    wait, wait_before = wait_pair
    bills, bills_before = bills_pair
    splits, splits_before = splits_pair
    for slot in page:
        kind = sel[slot]
        rec = splits if kind == "split" else bills
        k, node = nodes.get(slot, (None, None))
        row = parse_bill(node, k, kwh.get(slot))
        if row is None or (kind == "split" and not row.get("breakdown")):
            if not failed:
                _bill_ask_unanswered(rec, slot)
            continue
        _bill_ask_answered(rec, slot)
        if row.get("zero"):
            store.upsert_measured_cost(slot, mpan=mpan, cost_incl=0.0, cost_excl=0.0,
                                       label="ZERO", kwh=0.0)
            continue
        bd = row.get("breakdown")
        if bd and _bill_drops_ev(emt_ev.get(slot), bd[2]):
            _bill_split_hold(wait, slot)
            st["held"] += 1
            continue
        if bd:
            _bill_split_release(wait, slot)
        _store_bill(store, slot, mpan, row)
        st["stored"] += 1
        if kind == "imported":
            # it was settled by the import's bill, so the paced apply (unsettled blocks only) would
            # not take Octopus's: settle it here
            try:
                settle_half_hour(store, slot)
            except Exception as e:
                logger.warning("octopus asks: settle failed for %s: %s", slot, e)
        if kind == "split":
            # its cost-only bill was applied already, so the paced apply (unsettled blocks only)
            # would not take the split: re-settle it here, as the drain's phase 2 did
            try:
                apply_measured_to_block(slot, cost_incl=row["cost_incl"],
                                        cost_excl=row["cost_excl"], label=row["label"])
            except Exception as e:
                logger.warning("octopus asks: re-settle failed for %s: %s", slot, e)
    # what this page changed, merged onto what anything else saved meanwhile
    _save_changes(store, wait_before, wait, lambda: _bill_split_wait(store), _bill_split_wait_save)
    _save_changes(store, bills_before, bills, lambda: _bill_ask(store, mpan), _bill_ask_save)
    _save_changes(store, splits_before, splits, lambda: _split_ask(store), _split_ask_save)


_ASK_PAGE_DAYS = 3            # local days of cost half-hours per page (one query a day)
_ASK_PAGE_BREAKDOWN = 48      # IOG-SMB breakdown half-hours per page (runs under 12 a query)
_ASK_HEADROOM = 0.25          # stop under this share of the allowance left (decided (c))
_ask_state: dict = {"status": "idle"}


def ask_status() -> dict:
    """The one asker's progress, for the banner: {status, total, done, stored, held, reason}."""
    return dict(_ask_state)


def _ask_selection(store, now=None) -> dict:
    """{slot: "bill" | "split"} the one selector picks now (`_ask_queue` over `ask_candidate_facts`)."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    now = now or _dt.now(_tz.utc).replace(tzinfo=None)
    mpan = ((_kraken_discovery or {}).get("import") or {}).get("mpan") or ""
    floor = (now - _td(days=_API_RETENTION_DAYS)).isoformat()
    facts = store.ask_candidate_facts(floor, now.isoformat(), mpan=mpan)
    return _ask_queue(facts, store, mpan, now)


def _ask_pages(sel: dict) -> list:
    """The selection newest first — an imported half-hour's after every other — in pages: up to
    `_ASK_PAGE_DAYS` local days of cost half-hours, or `_ASK_PAGE_BREAKDOWN` breakdown ones, a page."""
    from datetime import datetime as _dt, timezone as _tz
    from zoneinfo import ZoneInfo
    lon = ZoneInfo("Europe/London")
    pages, cur, days, n_bd = [], [], set(), 0
    # an imported half-hour's (it has the import's bill) after every other, each newest first
    for sl in sorted(sel, key=lambda s: (sel[s] != "imported", s), reverse=True):
        bd = bill_kind_at(sl) == "breakdown"
        day = None if bd else _dt.fromisoformat(sl).replace(tzinfo=_tz.utc).astimezone(lon).date()
        if cur and ((day is not None and day not in days and len(days) >= _ASK_PAGE_DAYS)
                    or (bd and n_bd >= _ASK_PAGE_BREAKDOWN)):
            pages.append(cur)
            cur, days, n_bd = [], set(), 0
        cur.append(sl)
        if bd:
            n_bd += 1
        else:
            days.add(day)
    if cur:
        pages.append(cur)
    return pages


async def _ask_allowance_ok() -> bool:
    """Whether Octopus's allowance has `_ASK_HEADROOM` or more left. True where the client cannot say."""
    get = getattr(_kraken_client, "get_rate_limit", None)
    if get is None:
        return True
    try:
        rl = await get()
    except Exception:
        return True
    if not rl:
        return True
    if rl.get("isBlocked"):
        return False
    rem, lim = rl.get("remaining"), rl.get("pointsLimit")
    return not (rem is not None and lim and rem < _ASK_HEADROOM * lim)


def _measured_history_backlog() -> int:
    """How many half-hours the one asker would ask Octopus about now (`_ask_selection`): its work
    signal, so it is scheduled only when there is something to ask (engine step 3c)."""
    store = _store
    if store is None or not _any_intelligent():
        return 0
    try:
        return len(_ask_selection(store))
    except Exception:
        return 0


def _maybe_drain_measured_history() -> None:
    """Schedule the SMB history drain as a loop TASK when a first-time / bulk import has left a
    backlog of capped slots without Octopus's billed breakdown. Guarded by an in-process flag; a
    no-op with no backlog, no API, or a running import/delete. Re-arms whenever a new backlog appears."""
    global _measured_history_drain_running
    try:
        if _store is None or _measured_history_drain_running or not _any_intelligent():
            return
        # `kraken_available()` is a PRECONDITION, not a dependency — the supplier
        # API is not a job and cannot be waited for. B §1 keeps the two apart;
        # folding a readiness check into the coordinator would make "is the API up"
        # answerable by the job registry.
        if not kraken_available():
            return
        _decision, _blocker = _coord.admission("measured_drain")
        if _decision != ADMIT:
            logger.info("_maybe_drain_measured_history: %s active — deferring", _blocker)
            return
        if _measured_history_backlog() <= 0:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            return

        async def _task():
            global _measured_history_drain_running
            try:
                await run_measured_history_drain()
            except Exception as e:
                logger.warning("_maybe_drain_measured_history: worker failed: %s", e)
            finally:
                _measured_history_drain_running = False

        _measured_history_drain_running = True
        logger.info("octopus asks: scheduling — %d half-hour(s) to ask Octopus about",
                    _measured_history_backlog())
        loop.create_task(_task())
    except Exception as e:
        logger.warning("_maybe_drain_measured_history: schedule failed: %s", e)
        _measured_history_drain_running = False


def apply_measured_settled(after: str = "", limit: "int | None" = None) -> dict:
    """BL-53 step 3: write Octopus's billed cost as authoritative for EVERY settled IOG
    dispatched block that has a cached measured_cost but isn't yet 'settled'/'corrected'.
    Operates over the whole cached-unapplied set (soak backlog + freshly-cached), so it is
    not limited to this pass's fetch. Rate keyed on cost via apply_measured_to_block.

    Settled = authority (design §4/§7): the bill is applied for EVERY cached settled slot,
    both bands, with NO age-gate and NO review-flag — a divergence from EMT's prediction is
    Octopus's own bill, not an anomaly to surface. The RATE is snapped to the tariff agreement
    in apply_measured_to_block (never cost/kWh). Idempotent: once a block is rate_source=
    'settled' it drops out of the query. Local writes only.

    (BL-70: `m.cost_incl > 0` skips the cached zero rows — a half-hour the bill says drew
    nothing has no cost to apply and no rate to snap, and apply_measured_to_block refuses it
    on `kwh <= 1e-9` anyway. Excluding them here just saves walking them every pass.)"""
    store = _store
    if store is None:
        return {}
    imp = (_kraken_discovery or {}).get("import") or {}
    mpan = imp.get("mpan")
    if not mpan:
        return {}
    try:
        rows = store.get_measured_costs_to_apply(mpan, _measured_floor(), after=after, limit=limit)
    except Exception as e:
        logger.warning("apply_measured: query failed: %s", e)
        return {}
    n_apply = n_published = 0
    refused: dict = {}                 # reason -> [count, first half-hour]
    for r in rows:
        # Only an Intelligent half-hour takes a bill (settlement surfaces design §9.2, bill and
        # split paths design §6 (c), 16 Oct 2026): applying snaps the rate to the day's lowest or
        # highest band, a two-rate model — on Agile / Go / Cosy / flat it would mis-set the rate
        # and band, and their settled kWh x the published rate is the bill anyway. Nothing fetches
        # a bill for such a half-hour today; this is the net under any that arrives in the cache.
        if tariff_kind_at(r["block_start"]) != "intelligent":
            n_published += 1
            continue
        # A half-hour before the current agreement takes its bill only where the schedule is
        # stitched across the agreements (BL-54): applying snaps the rate to the day's bands, and
        # without the stitch those would be the CURRENT tariff's (v4 4.5.5's reason for its floor).
        if not _agreement_priced_ok(r["block_start"]):
            continue
        # Settled = authority: apply the bill for every cached settled slot, both bands, no
        # age-gate and no review-flag. apply_measured_to_block skips a slot only when the
        # agreement can't provide a clean rate to snap to.
        _res = apply_measured_to_block(r["block_start"], cost_incl=r["cost_incl"],
                                       cost_excl=r["cost_excl"], label=r["label"])
        if _res:
            n_apply += 1
        else:
            _why = _APPLY_REFUSAL or "refused"
            _first = refused.setdefault(_why, [0, r["block_start"]])
            _first[0] += 1
    # Every cached bill that did not apply, by why — one line a pass, so a stuck schedule or a
    # refusing apply is in the log, not only on the heatmap (18 Oct 2026).
    for _why, (_n, _first) in sorted(refused.items()):
        logger.warning("apply_measured: %d cached bill(s) not applied — %s (first %s)",
                       _n, _why, _first)
    if n_apply:
        logger.info("apply_measured: applied %d block(s) from the bill (settled = authority)",
                    n_apply)
    return {"applied": n_apply, "candidates": len(rows), "published_rate": n_published,
            "last": rows[-1]["block_start"] if rows else None,
            "refused": {k: v[0] for k, v in refused.items()}}



# Why the last apply_measured_to_block refused, for the callers' summary (18 Oct 2026): v4's import
# schedule stopped covering new days after 14 Oct, and its apply refused every bill from then on
# with nothing in the log — the days read amber with no sign why.
_APPLY_REFUSAL: "str | None" = None
_REFUSE_NO_RATE = "no off-peak/peak rate for that day in the import schedule"
_REFUSE_NO_BAND = "Octopus's band for it is not settled yet"
_REFUSE_CAD_KWH = "its kWh is still the CAD figure, not the settled one"


def _refuse(reason: str) -> bool:
    global _APPLY_REFUSAL
    _APPLY_REFUSAL = reason
    return False


def apply_measured_to_block(bs: str, *, cost_incl: float, cost_excl=None,
                            label: str = None, replace_correction: bool = False) -> bool:
    """Settle an Intelligent half-hour from Octopus's answer: **Octopus's band, and the cost
    kWh × that band's rate** (engine step 2b, EMT-v5_engine_step2_import_bills_design.md §1) —
    through settle() since engine step 4b (EMT-v5_engine_step4_settle_design.md §3).

    The band is `settled_band`'s — on IOG-SMB the bucket that carries the kWh, elsewhere the label,
    the importer's IMPORTED bill by its cost ÷ kWh; each leg's rate is the CLEAN tariff band rate on
    the half-hour's own agreement, the cap's four on a capped half-hour; `imp_rate` the blend;
    ex-VAT through the VAT calendar; the devices clipped as PASS 2 clips them and priced at the
    rate of the legs their kWh sits in, written with the main in one transaction. Octopus's cost is
    never copied (on staging, 23 Oct 2026, all 5,119 bill-settled half-hours had it within a
    thousandth of a penny of kWh × the clean rate). No settled band refuses: the half-hour stays on
    EMT's prediction until Octopus's band arrives. rate_source='settled'. `cost_incl` / `label` are
    the bill being applied — they stand in for the cached one's; neither is written to the block.
    """
    global _APPLY_REFUSAL
    _APPLY_REFUSAL = None
    store = _store
    if store is None or not bs or cost_incl is None:
        return _refuse("no store, block or cost")
    row = store.get_block_measured_inputs(bs)
    if row is None:
        return _refuse("no block")
    # Defence in depth (4.5.12): NEVER settle an unsettled LIVE block, whoever calls us.
    # The callers' queries gate on BlockStore._SETTLEABLE_SQL too, but this is the single stamper of
    # rate_source='settled', so it must refuse on its own — a future/direct caller must
    # not be able to price a block off a provisional CAD kWh before its meter DCC-settles.
    if not str(row["source"] or "").startswith("imported") and row["imp_kwh_api"] is None:
        return _refuse("its kWh is not settled")
    kwh = row["imp_kwh"] or 0.0
    if kwh <= 1e-9:
        return _refuse("no kWh")
    # The cost is kWh × rate (step 2b), so the kWh must be the settled one: a block still on its CAD
    # figure after the DCC kWh landed waits for PASS 2 to carry it (the 07:00 shape — 0.004 CAD,
    # 0.001 settled). Copying Octopus's cost, the apply did not need to.
    if row["imp_kwh_api"] is not None and abs(kwh - float(row["imp_kwh_api"])) > 0.0005:
        return _refuse(_REFUSE_CAD_KWH)
    # ── settle() (engine step 4b, EMT-v5_engine_step4_settle_design.md §3) ─────────────────────
    # The half-hour from its facts: Octopus's band (the bill just cached is the latest), each leg at
    # its band's rate, imp_rate the blend, ex-VAT through the VAT calendar, the devices clipped and
    # priced by the legs their kWh sits in — main and devices in ONE transaction, so nothing is left
    # for a best-effort re-cost (recost_sub_meters) or the house re-fit to catch up on.
    # The bill is the one the caller passes (the re-fetch preview applies one it has not cached);
    # its buckets and kWh are the cache's. An Intelligent half-hour's only: apply_measured_settled
    # passes no other (16 Oct 2026).
    f, st = _settle_one(store, bs, bill={"bill_cost": cost_incl, "bill_label": label})
    if f is None:
        return _refuse("no block")
    if st.status == "no_rate" or (f.kind == "intelligent" and f.lo is None):
        return _refuse(_REFUSE_NO_RATE)
    if st.status != "settled":
        return _refuse(_REFUSE_NO_BAND)
    prior = _stored_band(store, bs)
    try:
        store.apply_measured_to_main(
            bs, rate=st.imp_rate, cost=st.imp_cost, cost_exc=st.imp_cost_exc,
            segments=_settled_segments(st), devices=_settled_devices(f, st),
            replace_correction=replace_correction)
    except Exception as e:
        logger.warning("apply_measured: write failed for %s, left for the next pass: %s", bs, e)
        return _refuse("the write failed")
    return {"prior_band": prior, "new_band": st.band, "new_rate": st.imp_rate}


def _settle_one(store, bs: str, bill: "dict | None" = None, *, main: "dict | None" = None,
                subs: "list | None" = None):
    """(settle.Facts, settle.Settled) for one main-meter half-hour, from the store's facts; (None,
    None) if there is no block. One query set (get_settle_inputs) for the half-hour. `bill`
    ({bill_cost, bill_label}) stands in for the latest cached bill's cost and label; `main`
    ({imp_kwh, imp_kwh_api, interpolated}) for the block's own figures and `subs` for its devices,
    where the caller holds the block it is writing (the PASS 2 re-run)."""
    import settle as _st
    try:
        end = (datetime.fromisoformat(bs) + timedelta(minutes=30)).isoformat()
    except ValueError:
        return None, None
    inp = store.get_settle_inputs(bs, end)
    m = next((x for x in inp["mains"] if x["block_start"] == bs), None)
    if m is None:
        return None, None
    if bill:
        m = dict(m, **bill)
    if main:
        m = dict(m, **main)
    try:
        cal = store.get_vat_calendar()
    except Exception:
        cal = None
    f = settle_facts(m, inp["subs"].get(bs, []) if subs is None else subs, store, cal)
    return f, _st.settle(f)


def _settled_segments(st) -> list:
    """settle()'s legs as the store's segments (pricing_segments.Segment)."""
    import pricing_segments as _ps
    return [_ps.Segment(g.kwh, g.inc_rate, g.exc_rate, g.band, g.attribution) for g in st.legs]


def _settled_devices(f, st) -> list:
    """settle()'s devices as (meter_id, grid kWh, rate, £, ex-VAT £): ex-VAT through the VAT calendar,
    as the legs' (design §4 (c))."""
    return [(d.meter_id, d.kwh_grid, d.rate, d.cost, round(d.cost / (1.0 + (f.vat or 0.0)), 6))
            for d in st.devices]


def _stored_band(store, bs: str) -> "str | None":
    """The band a half-hour's stored segments carry (the house leg's, else the EV's) — for the apply's
    note only. From the segments' labels, not read back from a rate (design §4 (a))."""
    try:
        segs = store.get_block_segments(bs, "electricity_main")
    except Exception:
        return None
    for want in ("house", "ev"):
        b = next((x["band"] for x in segs or [] if x["attribution"] == want), None)
        if b:
            return {"day": "peak"}.get(b, b)
    return None


@job("prune_tombstones")
def run_prune_stale_tombstones() -> dict:
    """Delete deletion tombstones whose range is already fully populated again.

    Moved here from `GET /api/blocks/deleted-ranges`, which pruned on every page
    view through `_engine_write` — a write inside a read, and a wait on the engine
    loop. First Pi soak (29 Sep 2026): that GET's p50 went from 0.02 s to 16.9 s
    while the re-price sweep held the loop. The GET now hides stale rows with the
    read-only `stale_deleted_range_ids()`; persisting the prune is the engine's.

    Defers while a delete runs (manifest: `exclusive_with=("delete",)`): a delete
    writes its tombstone before removing the blocks, so a prune in between would
    remove the tombstone it just wrote. Self-limiting — zero rows in the steady
    state. Runs on the hourly reconcile slot.
    """
    if _store is None:
        return {"ok": False, "reason": "no store"}
    decision, blocker = _coord.admission("prune_tombstones")
    if decision != ADMIT:
        return {"ok": False, "reason": "deferred: %s" % blocker}
    try:
        n = _store.prune_stale_deleted_ranges()
    except Exception as e:
        logger.warning("stale tombstone prune: failed: %s", e, exc_info=e)
        return {"ok": False, "reason": _error_text("tombstone prune", e)}
    if n:
        logger.info("stale tombstone prune: removed %d tombstone(s) whose range is "
                    "already fully populated", n)
    return {"ok": True, "pruned": n}


async def reconcile_dispatch_overlay() -> dict:
    """Settle each half-hour whose dispatch lifecycle has had time to land (design §12): a main
    block, not corrected, not bill-settled, not imported, whose slot was a smart-charge candidate
    (`get_reconcile_candidates`). Engine step 4c: the lifecycle's verdict — restore a slot the meter
    floor wrongly peaked, revert one planned but never charged, or completed without starting and too
    small to have materially run — is the prediction's second stage, so `settle_half_hour` decides
    and writes it, with the devices, only where the stored half-hour differs. Completed-only history
    rows are first materialised into dispatch slots (design §14). Runs on the engine loop."""
    store = _store
    if store is None:
        return {}
    sched = _kraken_rate_schedules.get("import")
    if sched is None or sched.is_empty():
        return {}
    # Settlement-time backfill of the billing overlay: a completed-ONLY dispatch
    # (EMT offline / re-imported) whose completed record has aged out of the
    # provider's rolling dispatch window is stranded in dispatch_history with no
    # billing slot, so the candidate query below can't see it and a genuine smart
    # charge stays mispriced at peak. Promote such substantial completed-only
    # history rows into off-peak dispatch_slots first so they become candidates
    # (design §14). Idempotent; no-op once slots exist.
    try:
        # Pass the tariff window so a completed-only dispatch OUTSIDE the off-peak
        # window, where EMT was up and saw no plan, is materialised as a PEAK bump
        # (is_boost hook reserved for chargers that flag a boost directly, e.g. Ohme).
        n_mat = store.materialise_completed_only_slots(
            _RECONCILE_SMALL_COMPLETED_KWH,
            is_off_peak=getattr(sched, "is_off_peak", None),
            is_boost=_iog_slot_is_boost)
        if n_mat:
            logger.info("reconcile: materialised %d off-peak slot(s) from completed-"
                        "only dispatch history (no prior billing slot)", n_mat)
    except Exception as e:
        logger.warning("reconcile: completed-only slot materialisation failed: %s", e)
    # The back-attribution that ran here (BL-9/BL-27: a block priced before its completed
    # dispatch landed) is the carve's since 16 Oct 2026 — one population, run at every capture
    # and hourly (bill and split paths design §3.3 2, 6b).
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    started_cutoff = (now - timedelta(minutes=_RECONCILE_STARTED_GATE_MIN)).isoformat()
    try:
        # The exclusions (BL-53 authority, imported history, the started-gate, the off-peak slot
        # and planned/completed evidence) live with the query in BlockStore.get_reconcile_candidates.
        rows = store.get_reconcile_candidates(started_cutoff)
    except Exception as e:
        logger.warning("reconcile_dispatch_overlay: query failed: %s", e)
        return {}
    # Engine step 4c (EMT-v5_engine_step4_settle_design.md §3): the lifecycle settling is a fact
    # arriving — settle each candidate. Its verdict (`_reconcile_decision`) is the prediction's own
    # second stage (predicted_band), so what decided off-peak or peak here decides it inside
    # settle(), with the half-hour's DCC kWh and its gap-fill state; settle_half_hour writes only
    # what differs. Until then this pass re-priced the block itself — restores, reverts, the BL-58b
    # stale-rated split heal and the BL-34 band relabel, each its own write, the band read back
    # from the stored rate — and held a DCC-settled block with a completed dispatch at its price
    # for the bill to decide; the bill decides it anyway (billed wins, 23 Oct 2026).
    n_settled = n_boundary = 0
    changed = False
    for r in rows:
        bs = r["block_start"]
        if not _agreement_priced_ok(bs):
            n_boundary += 1                  # BL-54: settle_facts gives it no rate either
            continue
        try:
            res = settle_half_hour(store, bs)
        except Exception as e:
            logger.warning("reconcile: settle failed for %s, left for the next pass: %s", bs, e)
            continue
        if res["written"]:
            n_settled += 1
            changed = True
            logger.info("reconcile: %s settled by settle() — %s (%s)",
                        bs, res["status"], "+".join(res["parts"]))
    # Heartbeat every run (even with no changes) so the pass is observable in the log.
    logger.info("reconcile: scanned %d candidate(s) — %d settled by settle(), %d agreement-guarded",
                len(rows), n_settled, n_boundary)
    return {"candidates": len(rows), "settled": n_settled, "agreement_guarded": n_boundary,
            "changed": changed}


async def resolve_history_gaps(now=None, meter_id: str = "electricity_main",
                               max_runs: int = 60) -> dict:
    """User-triggered sweep: find gaps in the block history and backfill them.

    The DCC poll's window is a forward-moving cursor, so an outage the cursor has
    already passed is never revisited — the hole is permanent. This asks the DB
    where the holes are, then re-polls each gap window explicitly
    (`advance_cursor=False`, so the normal cursor is untouched). BL-8's
    `create_backfill_block` materialises the missing blocks from the settled
    figures; PASS 2 then prices them.

    Deliberately **explicit, not automatic**: we cannot distinguish an outage gap
    from blocks the user deleted on purpose (Data Management → Delete Blocks), and
    an automatic sweep would silently resurrect them.
    """
    if not kraken_available():
        return {"ok": False, "reason": "no_api"}
    ingester = _build_kraken_ingester()
    if ingester is None:
        return {"ok": False, "reason": "no_ingester"}
    if _store is None:
        return {"ok": False, "reason": "no_store"}

    gaps = _store.find_block_gaps(meter_id, now=now)
    if not gaps:
        return {"ok": True, "gaps": 0, "backfilled": 0, "runs": []}

    total_backfilled = 0
    runs_done = []
    step = timedelta(minutes=30)
    for g in gaps[:max_runs]:
        # BL-8 phase 2: this is the BLANKET "recover all gaps" path — it must NOT
        # resurrect deliberately-deleted ranges. Skip a tombstoned hole (a targeted
        # per-gap fill / re-import is the only thing that restores one).
        if _store.is_slot_tombstoned(meter_id, g["start"]):
            continue
        # widen by one slot each side so boundary intervals are included
        pf = (datetime.fromisoformat(g["start"]) - step)
        pt = (datetime.fromisoformat(g["end"]) + step * 2)
        window = (pf.isoformat(timespec="seconds") + "Z",
                  pt.isoformat(timespec="seconds") + "Z")
        try:
            summary = await ingester.poll(window=window, advance_cursor=False)
        except Exception as e:
            logger.warning("resolve_history_gaps: window %s failed: %s", window, e)
            continue
        made = summary.get("backfilled", 0)
        await rebuild_devices_after_backfill(summary)
        total_backfilled += made
        runs_done.append({"start": g["start"], "end": g["end"],
                          "slots": g["slots"], "backfilled": made})
        logger.info("resolve_history_gaps: %s..%s (%d slots) -> backfilled %d",
                    g["start"], g["end"], g["slots"], made)

    logger.info("resolve_history_gaps: %d gap run(s), %d block(s) backfilled",
                len(gaps), total_backfilled)
    return {"ok": True, "gaps": len(gaps), "backfilled": total_backfilled,
            "runs": runs_done}


def kraken_poll_status() -> dict:
    """The poll's last cycle, for the pill: {running, started, finished} (naive-UTC ISO or None)."""
    return dict(_kraken_poll_state)


def request_kraken_poll(loop) -> tuple:
    """BL-95: wake the poll for a cycle now, from another thread (the web server's). (ok, reason):
    refused with no API, no poll task, a cycle running, or one started under 15 minutes ago."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    if _kraken_client is None or not _kraken_discovery:
        return False, "no supplier API connected"
    ev = _kraken_poll_wake
    if ev is None or _kraken_poll_task_handle is None or _kraken_poll_task_handle.done():
        return False, "the poll is not running"
    if _kraken_poll_state.get("running"):
        return False, "a poll is running"
    started = _kraken_poll_state.get("started")
    now = _dt.now(_tz.utc).replace(tzinfo=None)
    if started and now - _dt.fromisoformat(started) < _td(seconds=_KRAKEN_POLL_NOW_MIN_S):
        return False, "Octopus was asked under %d minutes ago" % (_KRAKEN_POLL_NOW_MIN_S // 60)
    loop.call_soon_threadsafe(ev.set)
    return True, ""


async def _kraken_poll_sleep(seconds: float) -> None:
    """Wait until the next cycle: `seconds`, or less where a cycle is asked for (BL-95)."""
    ev = _kraken_poll_wake
    if ev is None:
        await asyncio.sleep(seconds)
        return
    try:
        await asyncio.wait_for(ev.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
    ev.clear()


async def kraken_poll_task(ha: HAClient):
    """Periodic DCC ingest loop. Runs poll() every 6h.

    4b-iii: LIVE — poll() now writes settlement. Before the FIRST live write
    (once, ever, gated on a kraken_state marker), a full blocks.db snapshot is
    taken as a rollback point. dry_run is False; the flip was a deliberate step.

    No-op (returns immediately) when no API key / discovery / ingester — so it
    is always safe to schedule.
    """
    if _kraken_client is None or not _kraken_discovery:
        logger.info("kraken_poll_task: API not configured — task idle")
        return
    ingester = _build_kraken_ingester()
    if ingester is None:
        logger.info("kraken_poll_task: ingester unavailable — task idle")
        return

    logger.info("kraken_poll_task: started (interval=%ds, backfill_days=%d)",
                _KRAKEN_POLL_INTERVAL_S, ingester.backfill_days)
    global _kraken_poll_wake
    _kraken_poll_wake = asyncio.Event()
    while True:
        try:
            # Don't poll/backfill while a manual delete is mutating blocks: they
            # share one SQLite connection and BL-8 backfill would re-create rows the
            # delete is removing (and vice-versa). Skip this cycle; the next one
            # catches up (the window anchors back to the oldest unsettled block).
            if delete_in_progress():
                logger.info("kraken_poll_task: delete in progress — skipping this "
                            "poll cycle")
                await _kraken_poll_sleep(_KRAKEN_POLL_INTERVAL_S)
                continue
            # Mark the cycle active so a manual delete refuses rather than racing
            # this backfill. Cleared in finally, so it can never wedge deletes.
            set_poll_active(True)
            _kraken_poll_state.update(running=True, started=_dt_now_iso_safe())
            # The account's agreements, once a day (17 Oct 2026): before the rate refresh,
            # so a new agreement's rates are built in this same cycle.
            await _maybe_rediscover()
            # Chunk 5: refresh rate schedules once per cycle (cheap — fixed
            # tariffs return ~1 record). The sync drain reads these via
            # _kraken_rate_resolver to repair zero/missing rates at reconcile.
            await _refresh_kraken_rate_schedules()
            # a change of agreements re-settles its history on the rates just built (G1 design §4)
            _maybe_settle_history()
            # G2b: a closed agreement's prices not kept yet, a few a cycle, once ever
            await _fetch_closed_agreement_prices()
            summary = await ingester.poll()
            await rebuild_devices_after_backfill(summary)
            logger.info(
                "kraken_poll_task: %s window=%s..%s import_rows=%d "
                "store_import=%d export_rows=%d store_export=%d "
                "skip=%d drifted=%d (interpolated=%d) errors=%d",
                "LIVE",
                summary["window"][0], summary["window"][1],
                summary["import_rows"], summary["stored"],
                summary["export_rows"], summary["export_stored"],
                summary["skipped_no_block"], summary["drifted"],
                summary.get("drifted_interpolated", 0),
                len(summary["errors"]))
            for err in summary["errors"][:5]:
                logger.warning("kraken_poll_task: %s", err)
            # Label the provisional figure by its actual source: the Mini
            # supplies it whenever the runtime Mini reader is active (mode=api /
            # api+mini with no local sensor); otherwise it's the local CAD read.
            _prov_label = "Mini" if _kraken_mini_reader is not None else "CAD"
            for smp in summary.get("drift_samples", []):
                logger.info(
                    "kraken_poll_task:   drift-sample [%s]%s %s %s=%s DCC=%s drift=%s%%",
                    smp["channel"], " INTERP" if smp.get("interpolated") else "",
                    smp["block_start"], _prov_label, smp["cad_kwh"], smp["dcc_kwh"],
                    ("%.1f" % smp["drift_pct"]) if smp["drift_pct"] is not None
                    else "n/a")
            # Daily sweep for unsettled gaps that aged out of the sliding window
            # (late/out-of-order DCC settlement). Runs on the first poll after
            # startup too, so downtime gaps catch up. Self-gated + non-fatal.
            await _maybe_run_settlement_sweep()
        except Exception as e:
            logger.error("kraken_poll_task: poll failed: %s", e)
        finally:
            set_poll_active(False)
            if _kraken_poll_state.get("running"):
                _kraken_poll_state.update(running=False, finished=_dt_now_iso_safe())
        await _kraken_poll_sleep(_KRAKEN_POLL_INTERVAL_S)


def _clear_stale_inprogress_reads(cb_for_gap, cold_start: bool) -> bool:
    """Rogue-total guard: on a COLD start, clear the in-progress block's carried
    reads/rates so a stale prior-process register isn't taken as the block opener
    (reads[0] becomes the opener and the block's kWh = reads[-1] - reads[0] off a
    previous-session value — a "rogue total").

    Cold-start ONLY. On a warm re-run (HA reconnect / config-save within the same
    process) the in-progress reads are LIVE, not stale, so clearing them discards
    real consumption — e.g. a Mini block losing its ~3.4 kWh opener when an HA
    upgrade triggered repeated engine_startup re-runs. Returns True if it cleared
    anything (caller persists + logs). Mutates cb_for_gap in place.
    """
    if not cold_start:
        return False
    if not (cb_for_gap and cb_for_gap.get("start")):
        return False
    cleared = False
    for _md in (cb_for_gap.get("meters") or {}).values():
        for _ch in (_md.get("channels") or {}).values():
            if _ch.get("reads") or _ch.get("rates"):
                _ch["reads"] = []
                _ch["rates"] = []
                cleared = True
    return cleared


_startup_lock = asyncio.Lock()   # serialises engine_startup (see wrapper below)
_startup_pending = False         # a startup was requested while one was running
_last_startup_complete_ts = 0.0  # monotonic time the last startup finished (reconnect debounce)


# ── A battery sensor that counts solar (battery sensor and device split design §3) ──────────────
# A battery must report its grid charge; one that reports its total charge, solar included, reads
# more than the house imported. Evidence only where it is conclusive: more than 1.5 × the half-hour's
# whole grid import and more than 0.25 kWh above it (staging's trickles and its 14 Oct 2026 firmware
# over-reads stay below), on five days within 30. EMT changes nothing in the data on it (the user,
# 29 Oct 2026: a wrong verdict must not cost history): it says so loudly, on every page, until the
# sensor is changed or the user chooses to keep it anyway.
_BATTERY_RATIO = 1.5
_BATTERY_FLOOR_KWH = 0.25
_BATTERY_DAYS = 5
_BATTERY_WINDOW_DAYS = 30
_BATTERY_EVIDENCE_KEY = "battery_sensor_evidence"     # store_meta: {meter_id: the evidence}
_BATTERY_FLAGGED_KEY = "battery_sensor_flagged"       # store_meta: {meter_id: [sensor ids found counting solar]}
_BATTERY_CONFIRMED_KEY = "battery_sensor_confirmed"   # store_meta: {meter_id: {sensor_id, at}} — the user's word
_BATTERY_CHECK_S = 3600
_battery_check_at: "float | None" = None


def battery_sensor_verdict(days) -> "str | None":
    """The day the evidence becomes conclusive — the fifth of five distinct days within 30 — or None.
    `days`: ISO dates (YYYY-MM-DD), any order."""
    from datetime import date as _date
    ds = sorted({_date.fromisoformat(str(d)[:10]) for d in days or []})
    for i in range(len(ds) - _BATTERY_DAYS + 1):
        if (ds[i + _BATTERY_DAYS - 1] - ds[i]).days < _BATTERY_WINDOW_DAYS:
            return ds[i + _BATTERY_DAYS - 1].isoformat()
    return None


def check_battery_sensors() -> dict:
    """Each battery's evidence against its sensor, over its whole history, into store_meta
    (`_BATTERY_EVIDENCE_KEY`): {meter_id: {sensor_id, half_hours, days, first, last, conclusive_on,
    recent}}. A sensor whose evidence is conclusive is flagged (`_BATTERY_FLAGGED_KEY`) and warned of
    once — unless the user has confirmed it (`_BATTERY_CONFIRMED_KEY`). Nothing in the blocks changes.
    A battery whose flagged sensor was replaced counts only the rows naming its new sensor: the
    unnamed rows (imported, rebuilt) are the old sensor's. Never raises."""
    if _store is None:
        return {}
    try:
        old = _store.get_meta(_BATTERY_EVIDENCE_KEY, {}) or {}
        flagged = _store.get_meta(_BATTERY_FLAGGED_KEY, {}) or {}
        confirmed = _store.get_meta(_BATTERY_CONFIRMED_KEY, {}) or {}
        out = {}
        for mid, sid in _store.battery_devices():
            if sid is None:      # no sensor to judge: what an old one recorded is not this device's fault
                out[mid] = {"sensor_id": None, "half_hours": 0, "days": 0, "first": None, "last": None,
                            "conclusive_on": None, "recent": []}
                continue
            replaced = any(f != sid for f in flagged.get(mid) or [])
            rows = _store.battery_evidence_rows(mid, sid, ratio=_BATTERY_RATIO, floor_kwh=_BATTERY_FLOOR_KWH,
                                                unnamed=not replaced)
            days = sorted({r[0][:10] for r in rows})
            verdict = battery_sensor_verdict(days)
            out[mid] = {"sensor_id": sid, "half_hours": len(rows), "days": len(days),
                        "first": rows[0][0] if rows else None, "last": rows[-1][0] if rows else None,
                        "conclusive_on": verdict,
                        "recent": [[bs, round(k, 3), round(g, 3)] for bs, k, g in rows[-10:]]}
            if not verdict or sid in (flagged.get(mid) or []):
                continue
            flagged.setdefault(mid, []).append(sid)
            if (confirmed.get(mid) or {}).get("sensor_id") == sid:
                continue
            logger.warning(
                "battery sensor: %s's sensor reads more than the house imported — %d half-hour(s) "
                "on %d day(s), over %.1f× the grid import and %.2f kWh above it, conclusive on %s: "
                "it counts solar charging, not only grid, so the device split is wrong (the bill is "
                "not); choose the battery's grid-charge sensor", mid, len(rows), len(days),
                _BATTERY_RATIO, _BATTERY_FLOOR_KWH, verdict)
        _store.set_meta(_BATTERY_EVIDENCE_KEY, out)
        _store.set_meta(_BATTERY_FLAGGED_KEY, flagged)
        return out
    except Exception as e:
        logger.warning("battery sensor: the check failed (%s)", e)
        return {}


def battery_sensor_flags() -> dict:
    """The batteries whose sensor is found counting solar, for the page: {meter_id: {conclusive_on,
    half_hours, days, example [half-hour, battery kWh, grid kWh], confirmed_at}} — only while the
    sensor judged is still the battery's primary (a new one is judged afresh at the next check).
    `confirmed_at` is set where the user has chosen to keep the sensor anyway."""
    rd = _reader()
    ev = rd.get_meta(_BATTERY_EVIDENCE_KEY, {}) or {}
    confirmed = rd.get_meta(_BATTERY_CONFIRMED_KEY, {}) or {}
    out = {}
    for mid, sid in rd.battery_devices():
        e = ev.get(mid) or {}
        if sid is None or not e.get("conclusive_on") or e.get("sensor_id") != sid:
            continue
        c = confirmed.get(mid) or {}
        out[mid] = {"conclusive_on": e["conclusive_on"], "half_hours": e.get("half_hours"),
                    "days": e.get("days"),
                    "example": max(reversed(e.get("recent") or [[None, 0, 0]]), key=lambda r: r[1] - r[2]),
                    "confirmed_at": c.get("at") if c.get("sensor_id") == sid else None}
    return out


def confirm_battery_sensor(meter_id: str, confirmed: bool = True) -> bool:
    """The user's choice on a flagged battery sensor: keep it anyway (`confirmed`), so it is no
    longer shown as wrong — or, False, take that back. Held for the sensor confirmed; a new
    sensor is judged afresh. False when the battery has no primary sensor."""
    sid = dict(_store.battery_devices()).get(meter_id)
    if sid is None:
        return False
    rec = _store.get_meta(_BATTERY_CONFIRMED_KEY, {}) or {}
    if confirmed:
        rec[meter_id] = {"sensor_id": sid, "at": datetime.utcnow().replace(microsecond=0).isoformat()}
        logger.info("battery sensor: %s's flagged sensor kept by the user", meter_id)
    else:
        rec.pop(meter_id, None)
    _store.set_meta(_BATTERY_CONFIRMED_KEY, rec)
    return True


# ── A device sensor that reads the whole house (BL-108) ─────────────────────────────────────────
# 5 Nov 2026, a tester's support file: a battery set up with the inverter's reading of the house's grid
# import matched the main meter at every hour, the car's charging included, so the battery took
# everything but the car and the house read near zero. Any device, any type: its sensor is judged to
# read the whole house where, over the meter's last 30 days, it is within 5% of the meter's import in at
# least 75% of the half-hours the house imported over 0.1 kWh, on 200 of them or more. That battery:
# 86%; every other device in the databases to hand: 9% at most. As the battery check, EMT changes
# nothing on it: it says so, until the sensor is changed or the user keeps it anyway.
_WHOLE_HOUSE_DAYS = 30
_WHOLE_HOUSE_TOL = 0.05
_WHOLE_HOUSE_FLOOR_KWH = 0.1
_WHOLE_HOUSE_SHARE = 0.75
_WHOLE_HOUSE_MIN = 200
_WHOLE_HOUSE_EVIDENCE_KEY = "whole_house_sensor_evidence"     # store_meta: {meter_id: the evidence}
_WHOLE_HOUSE_FLAGGED_KEY = "whole_house_sensor_flagged"       # store_meta: {meter_id: [sensor ids found]}
_WHOLE_HOUSE_CONFIRMED_KEY = "whole_house_sensor_confirmed"   # store_meta: {meter_id: {sensor_id, at}}


def whole_house_verdict(n: int, matched: int) -> bool:
    """Whether a device's evidence says its sensor reads the whole house (BL-108)."""
    return n >= _WHOLE_HOUSE_MIN and matched >= _WHOLE_HOUSE_SHARE * n


def check_whole_house_sensors() -> dict:
    """Each device's evidence that its sensor reads the whole house's grid import, into store_meta
    (`_WHOLE_HOUSE_EVIDENCE_KEY`): {meter_id: {sensor_id, n, matched, share, example, whole_house}}. A
    sensor found so is flagged (`_WHOLE_HOUSE_FLAGGED_KEY`) and warned of once, unless the user has kept
    it (`_WHOLE_HOUSE_CONFIRMED_KEY`). A device whose flagged sensor was replaced counts only the rows
    naming its new one. Nothing in the blocks changes. Never raises."""
    if _store is None:
        return {}
    try:
        flagged = _store.get_meta(_WHOLE_HOUSE_FLAGGED_KEY, {}) or {}
        confirmed = _store.get_meta(_WHOLE_HOUSE_CONFIRMED_KEY, {}) or {}
        out = {}
        for mid, sid in _store.device_primary_sensors():
            if sid is None:      # no sensor to judge
                out[mid] = {"sensor_id": None, "n": 0, "matched": 0, "share": None, "example": None,
                            "whole_house": False}
                continue
            replaced = any(f != sid for f in flagged.get(mid) or [])
            ev = _store.whole_house_evidence(mid, sid, days=_WHOLE_HOUSE_DAYS, tol=_WHOLE_HOUSE_TOL,
                                             floor_kwh=_WHOLE_HOUSE_FLOOR_KWH, unnamed=not replaced)
            n, matched = ev["n"], ev["matched"]
            found = whole_house_verdict(n, matched)
            out[mid] = {"sensor_id": sid, "n": n, "matched": matched,
                        "share": round(matched / n, 3) if n else None,
                        "example": [ev["example"][0], round(ev["example"][1], 3), round(ev["example"][2], 3)]
                                   if ev["example"] else None,
                        "whole_house": found}
            if not found or sid in (flagged.get(mid) or []):
                continue
            flagged.setdefault(mid, []).append(sid)
            if (confirmed.get(mid) or {}).get("sensor_id") == sid:
                continue
            logger.warning(
                "device sensor: %s's sensor reads what the whole house imports — within %d%% of the meter "
                "in %d of %d half-hours over the last %d days: it is the house's grid import, not the "
                "device's, so the device takes the house's usage (the bill is not affected); choose the "
                "device's own sensor", mid, int(_WHOLE_HOUSE_TOL * 100), matched, n, _WHOLE_HOUSE_DAYS)
        _store.set_meta(_WHOLE_HOUSE_EVIDENCE_KEY, out)
        _store.set_meta(_WHOLE_HOUSE_FLAGGED_KEY, flagged)
        return out
    except Exception as e:
        logger.warning("device sensor: the whole-house check failed (%s)", e)
        return {}


def whole_house_sensor_flags() -> dict:
    """The devices whose sensor is found reading the whole house, for the page: {meter_id: {n,
    matched, share, example [half-hour, device kWh, meter kWh], confirmed_at}} — only while the sensor
    judged is still the device's primary. `confirmed_at` where the user has kept it anyway."""
    rd = _reader()
    ev = rd.get_meta(_WHOLE_HOUSE_EVIDENCE_KEY, {}) or {}
    confirmed = rd.get_meta(_WHOLE_HOUSE_CONFIRMED_KEY, {}) or {}
    out = {}
    for mid, sid in rd.device_primary_sensors():
        e = ev.get(mid) or {}
        if sid is None or not e.get("whole_house") or e.get("sensor_id") != sid:
            continue
        c = confirmed.get(mid) or {}
        out[mid] = {"n": e.get("n"), "matched": e.get("matched"), "share": e.get("share"),
                    "example": e.get("example"),
                    "confirmed_at": c.get("at") if c.get("sensor_id") == sid else None}
    return out


def confirm_whole_house_sensor(meter_id: str, confirmed: bool = True) -> bool:
    """The user's choice on a sensor found reading the whole house: keep it anyway, or (False) take
    that back. Held for the sensor confirmed. False when the device has no primary sensor."""
    sid = dict(_store.device_primary_sensors()).get(meter_id)
    if sid is None:
        return False
    rec = _store.get_meta(_WHOLE_HOUSE_CONFIRMED_KEY, {}) or {}
    if confirmed:
        rec[meter_id] = {"sensor_id": sid, "at": datetime.utcnow().replace(microsecond=0).isoformat()}
        logger.info("device sensor: %s's whole-house sensor kept by the user", meter_id)
    else:
        rec.pop(meter_id, None)
    _store.set_meta(_WHOLE_HOUSE_CONFIRMED_KEY, rec)
    return True


def _maybe_check_battery_sensors(now: "float | None" = None) -> None:
    """The device sensor checks at most hourly, from the engine tick (and at start-up): a battery
    counting solar, and any device reading the whole house (BL-108)."""
    global _battery_check_at
    t = time.monotonic() if now is None else now
    if _battery_check_at is not None and t - _battery_check_at < _BATTERY_CHECK_S:
        return
    _battery_check_at = t
    check_battery_sensors()
    check_whole_house_sensors()


def seconds_since_startup() -> float:
    """Monotonic seconds since the last engine_startup completed. Used to debounce
    the HA-reconnect handler: a brief WS flap must not re-run the whole (heavy)
    startup, which saturates the loop, stalls the WS heartbeat and triggers ANOTHER
    disconnect — a reconnect storm. A large value means it's been a while (a genuine
    HA outage), so a full re-run is warranted."""
    if not _last_startup_complete_ts:
        return float("inf")
    return time.monotonic() - _last_startup_complete_ts


def _read_config_version() -> str:
    """App version from config.yaml (for the once-per-version backup gates)."""
    try:
        _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
        if os.path.exists(_p):
            with open(_p) as _vf:
                for _vl in _vf:
                    if _vl.strip().startswith("version:"):
                        return _vl.split(":", 1)[1].strip().strip('"')
    except Exception:
        pass
    return "unknown"


_BACKUP_KEEP = 20          # zips kept in the share's backups folder


def _pre_upgrade_backup_if_needed() -> None:
    """BL-53: take a **pre-migration** safety snapshot on a version upgrade — a raw
    copy of blocks.db (+ WAL/SHM sidecars) taken BEFORE the store opens and runs
    its schema migration, so it's a true pre-upgrade rollback point. Once per
    version (keyed on a marker file); the one start-up backup since engine step 5a, so it
    trims the folder to _BACKUP_KEEP. Best-effort; never blocks startup."""
    import zipfile as _zf
    from datetime import datetime as _dt2
    _cur_ver = _read_config_version()
    _marker = os.path.join(DATA_DIR, ".pre_upgrade_backup_version")
    _last = ""
    if os.path.exists(_marker):
        try:
            with open(_marker) as _mf:
                _last = _mf.read().strip()
        except Exception:
            _last = ""
    if _cur_ver == _last:
        return
    if not os.path.exists(BLOCKS_DB_PATH):
        # fresh install — nothing to snapshot; record the version so we don't retry
        try:
            ensure_dir(DATA_DIR)
            with open(_marker, "w") as _mf:
                _mf.write(_cur_ver)
        except Exception:
            pass
        return
    _bk_dir = f"{SHARE_BACKUP_DIR}/backups"
    ensure_dir(_bk_dir)
    _ts = _dt2.utcnow().strftime("%Y%m%dT%H%M%S")
    _bk = f"{_bk_dir}/{_ts}_pre_upgrade_{_cur_ver}.zip"
    with _zf.ZipFile(_bk, "w", _zf.ZIP_DEFLATED) as _z:
        # copy the DB and its WAL/SHM sidecars together, so any not-yet-checkpointed
        # writes are preserved (restoring all three replays the WAL). No open store
        # here, so a SQLite backup() isn't available — the raw triple is the snapshot.
        _z.write(BLOCKS_DB_PATH, "blocks.db")
        for _side in ("-wal", "-shm"):
            _sp = BLOCKS_DB_PATH + _side
            if os.path.exists(_sp):
                _z.write(_sp, "blocks.db" + _side)
        _cfg = os.path.join(DATA_DIR, "meters_config.json")
        if os.path.exists(_cfg):
            _z.write(_cfg, "meters_config.json")
    # Keep only the 20 most recent zips — the trim the post-open upgrade backup did until engine
    # step 5a (Data Management's own backups trim the same way)
    import glob as _gl
    for _old_zip in sorted(_gl.glob(f"{_bk_dir}/*.zip"))[:-_BACKUP_KEEP]:
        try:
            os.remove(_old_zip)
        except Exception:
            pass
    try:
        ensure_dir(DATA_DIR)
        with open(_marker, "w") as _mf:
            _mf.write(_cur_ver)
    except Exception:
        pass
    logger.info("engine_startup: PRE-upgrade snapshot for v%s (before migration): %s",
                _cur_ver, os.path.basename(_bk))


async def engine_startup(ha: HAClient):
    """Re-entrancy guard around the real startup.

    engine_startup re-runs on several triggers — a config save, a DB restore, and an
    HA reconnect — and each one tears down and rebuilds the BlockStore and the Kraken
    API client. When two fire close together (e.g. a restore that also reconnects HA)
    they used to run CONCURRENTLY on the loop, so one run closed the store / aiohttp
    connector out from under the other: 'Connector is closed', 'Unclosed client
    session', 'store not open — returning empty config' (a chart then rendered in the
    wrong timezone), plus an API rate-limit storm from overlapping discovery. It also
    saturated the loop, so loop-serialised operations elsewhere (the restore's store
    teardown, a config-history delete) timed out.

    This serialises runs (only one at a time) and COALESCES: a request that lands while
    one is running sets a flag instead of stacking, and the in-flight run re-runs ONCE
    at the end to pick up the latest state. Single-threaded event loop, so the flag
    handoff is race-free."""
    global _startup_pending, _last_startup_complete_ts, _last_reconcile, _last_dispatch_capture
    if _startup_lock.locked():
        _startup_pending = True
        logger.info("engine_startup: already running — request coalesced")
        return
    async with _startup_lock:
        while True:
            _startup_pending = False
            await _engine_startup_impl(ha)
            _last_startup_complete_ts = time.monotonic()
            # The dispatch tick's clocks are the process's, and a restore or a config save
            # rebuilds the store without restarting it: the bill apply, the drain and the
            # reconcile waited up to an hour after a restore (18 Oct 2026, the Pi — a restored
            # v4 database's cached bills sat unapplied). Start them on the next tick.
            _last_reconcile = None
            _last_dispatch_capture = None
            if not _startup_pending:
                return
            logger.info("engine_startup: a startup was requested mid-run — re-running once")


async def _engine_startup_impl(ha: HAClient):
    """
    Run once when the add-on starts.
    Registers state triggers, detects session gaps, generates startup charts.
    Replaces @time_trigger("startup").
    """
    # ── Open BlockStore FIRST so load_config() reads from the live DB ────
    # This must happen before load_config() — if _store is None and blocks.db
    # exists, load_config() deliberately returns {} to avoid stale JSON reads.
    # Opening the store here ensures config is always read from the DB on startup,
    # including after a restore where the DB file has been replaced.
    global _store, _cold_start_complete
    # Cancel any in-flight DCC poll BEFORE we tear down the store and Kraken
    # client below. A long backfill reading the DB / HTTP session while we close
    # them throws "Cannot operate on a closed database" / "Connector is closed".
    # The poll is relaunched at the end of startup once resources are rebuilt. The connect's rate
    # refresh goes too: startup's poll cycle builds the schedules again.
    _cancel_kraken_poll_task()
    _cancel_rate_refresh()
    # CRITICAL: engine_startup runs again on every config save. Reopening the
    # store without closing the previous connection leaves an orphaned WAL
    # connection that holds locks indefinitely — the engine loop's writes then
    # fail every tick with "database is locked" (busy_timeout can't help: the
    # orphaned connection never releases). Close it first.
    if _store is not None:
        try:
            _store.close()
        except Exception as _se:
            logger.warning("engine_startup: closing prior store failed: %s", _se)
        _store = None
    # BL-53: pre-migration safety snapshot BEFORE the store opens/migrates.
    try:
        _pre_upgrade_backup_if_needed()
    except Exception as _pube:
        logger.warning("engine_startup: pre-upgrade backup failed: %s", _pube)
    _store = open_block_store(BLOCKS_DB_PATH)
    # Device sensors design §2: a database from before `device_sensors` (an earlier v5, or a
    # v4 import) gets each channel's configured read sensor as its primary entry.
    try:
        _ds = _store.seed_device_sensors()
        if _ds:
            logger.info("engine_startup: device sensors — %d primary entr%s from the config",
                        _ds, "y" if _ds == 1 else "ies")
    except Exception as _dse:
        logger.warning("engine_startup: device sensors seed failed (non-fatal): %s", _dse)

    # ── Checkpoint WAL immediately after opening ──────────────────────────
    # Ensures all committed blocks are flushed from WAL into the main DB file.
    # Cheap and always safe — runs on every startup.
    global _meter_reset_detected
    _meter_reset_detected = False  # Reset on every startup so reconnects don't carry stale state

    try:
        # Report what actually happened. `PRAGMA wal_checkpoint` does not raise
        # when it cannot run -- it returns busy=1 -- so the previous version
        # logged "complete" whether or not a single page had moved.
        _busy, _log_pages, _done = _store.checkpoint("TRUNCATE")
        if _busy:
            logger.info(
                "engine_startup: WAL checkpoint INCOMPLETE — %d of %d page(s) "
                "copied, log NOT truncated (a reader holds an open "
                "transaction). Harmless: SQLite auto-checkpoints at 1000 pages "
                "and journal_size_limit reclaims the file once one completes.",
                _done, _log_pages)
        else:
            logger.info("engine_startup: WAL checkpoint complete (%d page(s))",
                        _done)
    except Exception as _wce:
        logger.warning("engine_startup: WAL checkpoint failed: %s", _wce)

    # BL-5: resolve this instance's backup directory from the site name. In
    # supervised mode /share is shared across add-ons, so the path is namespaced
    # by site slug; standalone is volume-scoped and unaffected. engine_startup
    # re-runs on every config save, so a site rename is picked up here and the
    # directory is migrated/adopted silently (see instance.py).
    try:
        _site = None
        if _store is not None:
            _pid = _store.get_current_config_period_id()
            if _pid is not None:
                _site = (_store.get_config_period(_pid) or {}).get("site_name")
        _bd = resolve_backup_dir_for_site(_site)
        logger.info("engine_startup: backup dir = %s (site=%r)", _bd, _site)
    except Exception as _bde:
        logger.warning("engine_startup: backup dir resolution failed: %s", _bde)

    # BL-5: data lineage (db_uuid) and foreign-restore detection. db_uuid lives
    # in the DB (store_meta) and travels with the data; a native DB carries
    # db_uuid == install_id. A mismatch means this DB was restored from another
    # instance — surface a dismissible notice (dismissed per lineage). This never
    # affects backup-directory ownership, which keys on the install id alone.
    try:
        global FOREIGN_RESTORE_NOTICE
        import instance as _inst
        _dbid = _store.get_meta("db_uuid") if _store is not None else None
        _dbid2 = _inst.ensure_db_uuid(_dbid)
        if _store is not None and _dbid2 != _dbid:
            _store.set_meta("db_uuid", _dbid2)
        FOREIGN_RESTORE_NOTICE = _inst.foreign_restore_notice(_dbid2)
        if FOREIGN_RESTORE_NOTICE.get("foreign") \
                and not FOREIGN_RESTORE_NOTICE.get("acknowledged"):
            logger.warning("engine_startup: DB appears restored from another "
                           "instance (db_uuid=%s… != this install)",
                           str(_dbid2)[:8])
    except Exception as _fre:
        logger.warning("engine_startup: lineage check failed: %s", _fre)
        FOREIGN_RESTORE_NOTICE = {"foreign": False, "db_uuid": None,
                                  "acknowledged": False}

    # BL-72/73: stamp what this database is, alongside the lineage stamp above.
    # engine_startup re-runs on every config save, so a supplier change through
    # the wizard is picked up here without a separate hook; the version stamp
    # lands on every start, so it always names the last release to open the DB.
    stamp_version(_store)
    stamp_supplier(_store)

    config = load_config()  # now reads from the open store ✓

    # ── Repair empty config period from meters_config.json (post-JSON migration) ─
    # If config_periods exists but has no meters (e.g. migration ran with empty config),
    # try to load meters_config.json and update the config period with it.
    if not config.get("meters") and os.path.exists(CONFIG_PATH):
        _json_config = load_json(CONFIG_PATH, {})
        if _json_config.get("meters"):
            logger.info(
                "engine_startup: config_periods has no meters — repairing from meters_config.json"
            )
            try:
                cp_id = _store.get_current_config_period_id()
                if cp_id:
                    _store._write_meters(_json_config, cp_id)
                    logger.info(
                        "engine_startup: config repaired — %d meter(s) written to period %d",
                        len(_json_config.get("meters", {})), cp_id
                    )
                else:
                    _store.insert_config_period(_json_config)
                    logger.info(
                        "engine_startup: config repaired — new period created from meters_config.json"
                    )
                config = load_config()
            except Exception as _cre:
                logger.warning("engine_startup: config repair failed: %s", _cre)

    # ── Upgrade bridge: preserve existing CAD users on first 3.0.0 boot ───
    # If no mode is stored but a main import sensor is configured, this is an
    # existing 2.x user — set 'cad' silently so nothing is disrupted and the
    # API path stays off (also neutralises any stale creds file in /data).
    # A fresh install (no import sensor) is left 'unset' for the setup survey.
    try:
        _detect_upgrade_mode(config)
    except Exception as _ude:
        logger.warning("engine_startup: upgrade mode detection failed: %s", _ude)

    # ── Config-state diagnostic dump (open-item 2) ───────────────────────
    # One-shot, after mode is settled. The log is the only window into a
    # self-hosted user's config, so a bug report can be diagnosed from it.
    try:
        _log_config_state(config)
    except Exception as _lce:
        logger.warning("engine_startup: config-state dump failed: %s", _lce)

    # ── Register state triggers from config ─────────────────────────────
    main_import_sensor = None
    main_export_sensor = None

    for mid, mcfg in config.get("meters", {}).items():
        if not mcfg.get("meta", {}).get("sub_meter", False):
            main_import_sensor = mcfg.get("channels", {}).get("import", {}).get("read")
            main_export_sensor = mcfg.get("channels", {}).get("export", {}).get("read")
            break

    if main_import_sensor:
        ha.subscribe_state(main_import_sensor, on_import_meter_update)
        logger.info("engine_startup: import trigger active on %s", main_import_sensor)
    else:
        logger.warning("engine_startup: no main import sensor found in config")

    if main_export_sensor:
        ha.subscribe_state(main_export_sensor, on_export_meter_update)
        logger.info("engine_startup: export trigger active on %s", main_export_sensor)
    else:
        logger.warning("engine_startup: no main export sensor found in config")

    # Subscribe power sensor to state changes so the cache stays current
    # between restarts. No callback needed — ha_client updates the cache automatically.
    for mid, mcfg in config.get("meters", {}).items():
        if not mcfg.get("meta", {}).get("sub_meter", False):
            ps = mcfg.get("meta", {}).get("power_sensor")
            if ps:
                ha.subscribe_state(ps, lambda entity_id, new_val, full_state: None)
                logger.info("engine_startup: power sensor subscribed for cache: %s", ps)
            break

    # Subscribe to SoC sensors for all battery sub-meters (skip retired meters)
    for mid, mcfg in config.get("meters", {}).items():
        if mcfg.get("meta", {}).get("sub_meter", False):
            # Skip retired meters — don't subscribe to their sensors
            if _store.is_meter_retired(mid):
                logger.info("engine_startup: skipping retired meter %s", mid)
                continue
            soc_s = mcfg.get("meta", {}).get("soc_sensor")
            if soc_s:
                ha.subscribe_state(soc_s, lambda entity_id, new_val, full_state: None)
                logger.info("engine_startup: SoC sensor subscribed for cache: %s (%s)", soc_s, mid)
            inv_s = mcfg.get("meta", {}).get("inverter_power_sensor")
            if inv_s:
                ha.subscribe_state(inv_s, lambda entity_id, new_val, full_state: None)
                logger.info("engine_startup: inverter power sensor subscribed for cache: %s (%s)", inv_s, mid)
            dev_s = mcfg.get("meta", {}).get("device_power_sensor")
            if dev_s:
                ha.subscribe_state(dev_s, lambda entity_id, new_val, full_state: None)
                logger.info("engine_startup: device power sensor subscribed for cache: %s (%s)", dev_s, mid)

    # Pre-load sensor states into ha_client cache
    sensors_to_preload = []
    for mcfg in config.get("meters", {}).values():
        # Include power_sensor, soc_sensor and inverter_power_sensor from meta (not in channels)
        ps = mcfg.get("meta", {}).get("power_sensor")
        if ps:
            sensors_to_preload.append(ps)
        soc_s = mcfg.get("meta", {}).get("soc_sensor")
        if soc_s:
            sensors_to_preload.append(soc_s)
        inv_s = mcfg.get("meta", {}).get("inverter_power_sensor")
        if inv_s:
            sensors_to_preload.append(inv_s)
        dev_s = mcfg.get("meta", {}).get("device_power_sensor")
        if dev_s:
            sensors_to_preload.append(dev_s)
        for ccfg in mcfg.get("channels", {}).values():
            for key in ("read", "rate", "standing_charge_sensor"):
                eid = ccfg.get(key)
                if eid:
                    sensors_to_preload.append(eid)
    # ── Wait for sensors + CI (concurrent, with timeouts) ──────────────
    # Preload immediately — this gets the current REST-cached state for all sensors.
    if sensors_to_preload:
        await ha.preload_states(sensors_to_preload)

    # Identify read sensors that are still unavailable after preload.
    # These are the sensors we need to wait for — rate/SC sensors are less
    # critical since we fall back to last_known_rates from DB.
    _read_sensors = set()
    for mcfg in config.get("meters", {}).values():
        for ccfg in mcfg.get("channels", {}).values():
            eid = ccfg.get("read")
            if eid:
                _read_sensors.add(eid)

    _SENSOR_TIMEOUT = 60   # seconds to wait for read sensors
    _CI_TIMEOUT     = 30   # seconds to wait for CI API

    # Run sensor wait and CI fetch concurrently
    async def _wait_for_sensors():
        """Wait until all read sensors have a valid state or timeout.
        Only waits for sensors that exist in HA but are currently unavailable.
        Sensors that don't exist in HA at all (None) are skipped immediately —
        waiting won't help and would cause unnecessary 60s delays on dev systems
        using a prod DB with sensors not present in the dev HA instance.
        """
        import asyncio as _aio
        deadline = _aio.get_running_loop().time() + _SENSOR_TIMEOUT
        pending = {e for e in _read_sensors
                   if ha.get_state(e) in ("unknown", "unavailable")}
        missing = {e for e in _read_sensors if ha.get_state(e) is None}
        if missing:
            logger.warning("engine_startup: %d sensor(s) not found in HA — skipping wait: %s",
                           len(missing), missing)
        if not pending:
            logger.info("engine_startup: all read sensors available after preload")
            return
        logger.info("engine_startup: waiting for %d sensor(s): %s", len(pending), pending)
        while pending:
            remaining = deadline - _aio.get_running_loop().time()
            if remaining <= 0:
                logger.warning("engine_startup: sensor wait timeout — %d sensor(s) still unavailable: %s",
                               len(pending), pending)
                return
            await _aio.sleep(min(1.0, remaining))
            pending = {e for e in pending
                       if ha.get_state(e) in ("unknown", "unavailable")}
        logger.info("engine_startup: all read sensors now available")

    async def _startup_ci_fetch():
        """Fetch CI data at startup with timeout — runs in executor to avoid blocking event loop."""
        import asyncio as _aio
        import urllib.error
        # Extract postcode on the main thread BEFORE entering the executor.
        # _get_postcode() calls load_config() which accesses the SQLite connection.
        # Calling it from inside run_in_executor causes a cross-thread SQLite access
        # that can deadlock on a fresh install where the DB was just created.
        postcode = _get_postcode()
        if not postcode:
                return
        loop = _aio.get_running_loop()
        # Offload ONLY the blocking NETWORK fetch to the executor. All DB writes
        # stay on the loop thread — the engine owns a single SQLite connection
        # (check_same_thread=False), so a DB write from the executor thread races
        # the loop-thread writers (poll/finalise/historical-carbon) on that one
        # connection and corrupts it: "cannot commit - no transaction is active" /
        # "error return without exception set". (Same rule the historical carbon
        # backfill already follows.)
        try:
            slots = await _aio.wait_for(
                loop.run_in_executor(None, _fetch_carbon_intensity, postcode),
                timeout=_CI_TIMEOUT)
        except _aio.TimeoutError:
            logger.warning("engine_startup: CI fetch timed out after %ds — will retry on first tick", _CI_TIMEOUT)
            return
        except urllib.error.URLError as e:
            logger.warning("engine_startup: CI fetch failed: %s — will retry on first tick", e)
            return
        except Exception as e:
            logger.warning("engine_startup: CI fetch error: %s", e)
            return
        # DB writes on the loop thread.
        n = 0
        try:
            for slot in (slots or []):
                _store.upsert_carbon_intensity(
                    slot["captured_at"], postcode,
                    slot["intensity_forecast"], slot["ci_index"],
                    slot["intensity_actual"])
                n += 1
            _store.prune_carbon_intensity(days=4)
        except Exception as e:
            logger.warning("engine_startup: CI persist error: %s", e)
        if n:
            global _last_ci_fetch
            _last_ci_fetch = datetime.now(timezone.utc).replace(tzinfo=None)
            logger.info("engine_startup: CI fetch complete — %d slots for %s", n, postcode)
            # Carbon-gap recovery — DB work, so run it ON THE LOOP THREAD (not an
            # executor) for the same single-connection reason.
            try:
                rec = _recover_missing_carbon()
                if rec:
                    logger.info("engine_startup: carbon recovery backfilled "
                                "%d block(s)", rec)
            except Exception as e:
                logger.warning("engine_startup: carbon recovery failed: %s", e)
            # Historical backfill (run-once, resumable, paged) — dispatched as a
            # loop task; keeps its own DB work on the loop thread.
            try:
                _maybe_backfill_historical_carbon()
            except Exception as e:
                logger.warning("engine_startup: historical backfill schedule "
                               "failed: %s", e)

    import asyncio as _asyncio
    await _asyncio.gather(
        _wait_for_sensors(),
        _startup_ci_fetch(),
        return_exceptions=True
    )

    # ── Detect third-party HA integrations (BCD live-power, OHME charge-mode) ──
    # Non-fatal; logs the config-state detection line and stores the result for
    # the overlay (OHME verified path) and live-power tile (BCD offload).
    try:
        await _detect_and_log_integrations(ha)
    except Exception as _de:
        logger.warning("engine_startup: integration detection step failed: %s", _de)

    # ── Detect and store currency symbol ────────────────────────────────
    for mid, mcfg in config.get("meters", {}).items():
        if not mcfg.get("meta", {}).get("sub_meter", False):
            rate_sensor = mcfg.get("channels", {}).get("import", {}).get("rate")
            if rate_sensor:
                try:
                    attrs = await ha.get_entity_attributes(rate_sensor)
                    unit  = attrs.get("unit_of_measurement", "")
                    symbol = detect_currency_symbol(unit)
                    # Derive ISO code — strip "/kWh" etc
                    code = unit.split("/")[0].strip().upper() if unit else "GBP"
                    mcfg.setdefault("meta", {})["currency_symbol"] = symbol
                    mcfg["meta"]["currency_code"]   = code
                    logger.info(
                        "engine_startup: currency detected from %s unit='%s' symbol='%s' code='%s'",
                        rate_sensor, unit, symbol, code,
                    )
                except Exception as e:
                    logger.warning("engine_startup: currency detection failed: %s", e)
            break
    # Persist currency to meters_config so charts can read it
    # Write meters_config.json as a convenience export (human-readable).
    # The DB config_period is the authoritative source — this file is not read back.
    try:
        save_json_atomic(CONFIG_PATH, config)
    except Exception as _e:
        logger.warning("engine_startup: could not write meters_config.json export: %s", _e)

    # The upgrade backup taken here, after the store opened and migrated, went in engine step 5a
    # (27 Oct 2026, decided 22 Oct): the pre-migration copy (`_pre_upgrade_backup_if_needed`) is the
    # one per version, and it trims the folder now.

    # Migrate full_config_json → normalised tables (2.0→2.1 one-time upgrade)
    try:
        migrated = _store.migrate_full_config_json()
        if migrated:
            logger.info(
                "engine_startup: migrated %d config periods to normalised schema", migrated
            )
    except Exception as _me:
        logger.warning("engine_startup: full_config_json migration failed: %s", _me)

    # BL-19, #307 and the register dip-and-recover self-heals ran here once per
    # database (marker-gated in kraken_state) to clean pre-guard history. Deleted
    # 4 Oct 2026 (BL-74): every database v5 admits is stamped >= the import floor,
    # so all three markers are set, and a fresh v5 database has no history for them
    # to find. The write-point guards that stop NEW cases stay (_apply_pass2's BL-19
    # rule and #307 ceiling; the stale-read and dip-without-collapse guards), and
    # the markers stay in the data. The mechanism — a kraken_state marker read at
    # startup, run once, then set — is what a future heal would use.

    if _store.get_current_config_period_id() is None:
        # Fresh DB (no config period yet). Automatic migration of the pre-SQLite
        # JSON stores (blocks.json / current_block.json) was removed in 4.0.0.
        # If a legacy store is present we do NOT silently import it — we warn
        # loudly and start as a new install, leaving the JSON file untouched so
        # it can still be migrated with an earlier release (<= 3.x) if needed.
        _legacy_json = None
        if os.path.exists(BLOCKS_PATH):
            _legacy_json = BLOCKS_PATH
        elif os.path.exists(BLOCKS_PATH + ".migrated"):
            _legacy_json = BLOCKS_PATH + ".migrated"
        if _legacy_json:
            logger.error(
                "engine_startup: found a legacy pre-SQLite block store at %s, but "
                "automatic JSON->SQLite migration was REMOVED in 4.0.0. Starting as "
                "a NEW install — your data is NOT being imported, and %s is left "
                "untouched on disk. To recover it, run an earlier release (3.x or "
                "older) once to migrate, then upgrade again.",
                _legacy_json, _legacy_json,
            )
        # New install (or unmigrated legacy present) — create initial config
        # period starting NOW. Always use now regardless of any date in the
        # config JSON — a historical date would trigger gap fill for all missing
        # blocks.
        _store.insert_config_period(config, effective_from=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())
        # Clear any stale current_block from a previous session — if there's no
        # config period, the current_block is invalid.
        _store.save_current_block({})
        logger.info("engine_startup: new install — initial config period created")

    logger.info("engine_startup: %d existing blocks in store", _store.count_blocks())

    # ── One-off (versioned): zero-fill blank export slots on imported days that
    # already carry export. A PDF/CSV/API historical import of a solar export meter
    # only writes the daytime export and leaves overnight NULL; the gap detector
    # can't tell that from real missing data, so it shows a permanent, un-fillable
    # export "gap". Day-scoped (only days with >=1 imported export), imported blocks
    # only, billing-neutral (0 export = £0). Runs once; the export gap then
    # self-clears on the next api_import_gaps read.
    try:
        if not _store.get_meta("export_zero_fill_v1", False):
            _tz = "UTC"
            for _m in (config.get("meters") or {}).values():
                if not (_m.get("meta") or {}).get("sub_meter"):
                    _tz = (_m.get("meta") or {}).get("timezone", "UTC")
                    break
            _n = _store.zero_fill_imported_export_blanks(_tz)
            _store.set_meta("export_zero_fill_v1", True)
            if _n:
                logger.info("engine_startup: export zero-fill — set %d blank export "
                            "slot(s) to 0 on imported export days (one-off)", _n)
    except Exception as _ze:
        logger.warning("engine_startup: export zero-fill migration failed "
                       "(non-fatal): %s", _ze)

    # ── Session gap detection ────────────────────────────────────────────
    # Use the last block BEFORE current_block.start to avoid zero-rate catch-up
    # blocks (written by ensure_correct_block before this startup completes)
    # from contaminating the gap detection and last_known_rates.
    _cb_for_gap = _store.load_current_block()
    _cb_start   = _cb_for_gap.get("start") if _cb_for_gap else None
    last_block  = _store.get_last_block_before(_cb_start) if _cb_start else _store.get_last_block()
    logger.info("engine_startup: gap detection — current_block.start=%s", _cb_start)

    # ── Clear stale pre-restart reads from the in-progress block ──────────
    # The process has only just started and has captured nothing yet, so any
    # reads already in current_block are from BEFORE the restart. If left in,
    # the first one becomes reads[0] (the block opener) and the block's kWh is
    # computed as reads[-1] - reads[0] using a stale prior-session register —
    # a "rogue total" taken as a block value (e.g. read_start carried from the
    # previous session). The gap-detected path below also clears these, but a
    # SHORT restart (< one block) yields no missing windows and would otherwise
    # skip the clear entirely. Clearing unconditionally here covers both cases;
    # the correct opener is then supplied by boundary interpolation against
    # fresh post-restart reads (and the carry-seed written at finalise).
    # ── Clear stale pre-restart reads from the in-progress block ──────────
    # COLD START ONLY (see _clear_stale_inprogress_reads). On a warm re-run (HA
    # reconnect / config-save) the in-progress reads are live and must be kept;
    # an HA upgrade on the dev box was triggering reconnect-storm re-runs that
    # otherwise wiped the live Mini block opener.
    if _clear_stale_inprogress_reads(_cb_for_gap, not _cold_start_complete):
        _store.save_current_block(_cb_for_gap)
        logger.info("engine_startup: cleared stale pre-restart reads from "
                    "in-progress block %s (rogue-total guard, cold start)",
                    _cb_start)

    if last_block:
        logger.info("engine_startup: gap detection — last_block start=%s end=%s",
                    last_block.get("start"), last_block.get("end"))
        for _m, _md in last_block.get("meters", {}).items():
            for _ch, _chd in _md.get("channels", {}).items():
                logger.info("engine_startup: gap detection — last_block %s/%s rate=%s read_end=%s",
                            _m, _ch, _chd.get("rate"), _chd.get("read_end"))
    else:
        logger.warning("engine_startup: gap detection — no last_block found")
    if last_block:
        # Use the START of the last finalised block as the gap anchor so that
        # the unfinalised current_block window is included in gap fill.
        # detect_gap computes last_block_end = floor(anchor) + block_minutes,
        # so passing block start gives last_block_end = last_block.end,
        # covering the current_block window and everything after it.
        last_block_start = last_block.get("start")
        if last_block_start:
            # Derive block_minutes from the last block meta, falling back to config
            # then global default. Critical for non-30min setups.
            _lb_bm = int(
                (last_block.get("meters") or {})
                .get("electricity_main", {})
                .get("meta", {})
                .get("block_minutes") or get_block_minutes()
            )
            missing_windows = detect_gap(last_block_start, datetime.now(timezone.utc).replace(tzinfo=None), block_minutes=_lb_bm)
            if missing_windows:
                logger.warning(
                    "engine_startup: session gap detected — %d missing blocks", len(missing_windows)
                )
                logger.info("engine_startup: gap windows first=%s last=%s",
                            missing_windows[0], missing_windows[-1])
                current_block        = _store.load_current_block()
                pre_reads, last_rates = extract_last_reads(last_block)
                logger.info("engine_startup: pre_reads=%s", pre_reads)
                logger.info("engine_startup: last_rates=%s", last_rates)

                # ── Detect just-unretired sub-meters ─────────────────────
                # A sub-meter unretired after a long absence has a last block
                # from days/weeks ago. Including it in pre_reads causes a
                # massive delta spike on the first resumed block — the engine
                # tries to reconcile the entire retirement gap in one block.
                # Fix: remove any sub-meter whose last block is > 12 hours
                # old from pre_reads so it starts fresh from live reads.
                for _mid in list(pre_reads.keys()):
                    if _mid == "electricity_main":
                        continue
                    _last_end = _store.last_block_end(_mid)
                    if not _last_end:
                        continue
                    _last_dt  = datetime.fromisoformat(_last_end)
                    _now_utc  = datetime.now(timezone.utc).replace(tzinfo=None)
                    _gap_hrs  = (_now_utc - _last_dt).total_seconds() / 3600
                    if _gap_hrs > GAP_FILL_LIMIT_HOURS:
                        logger.warning(
                            "engine_startup: sub-meter %s last block %.1f hours ago — "
                            "removing from pre_reads to prevent delta spike on resume "
                            "(possible unretire or extended absence).",
                            _mid, _gap_hrs
                        )
                        del pre_reads[_mid]
                        last_rates.pop(_mid, None)
                # Clear stale reads from before the restart — if we leave them in,
                # the sub-meter's channel reads will span the restart gap, producing
                # a false large delta on the first post-restart block (e.g. 10 kWh
                # from a battery sensor whose cumulative read advanced while offline).
                # The gap marker's pre_reads captures the correct pre-gap values;
                # live reads will accumulate fresh from the first post-restart capture.
                for meter_data in (current_block.get("meters") or {}).values():
                    for channel in (meter_data.get("channels") or {}).values():
                        channel["reads"] = []
                        channel["rates"] = []
                # ── Attempt immediate gap fill using preloaded sensor states ──
                # Build post_reads from the preloaded HA state cache.
                # This avoids the race condition where gap fill was previously
                # deferred until the first live sensor fire, which could happen
                # before sub-meter sensors had reported — producing rate=0 gap blocks.
                _preload_post_reads = {}
                _has_preload_read = False
                for _m_name, _m_cfg in config.get("meters", {}).items():
                    _preload_post_reads[_m_name] = {}
                    for _ch_name, _ch_cfg in _m_cfg.get("channels", {}).items():
                        _eid = _ch_cfg.get("read")
                        if not _eid:
                            continue
                        _val = ha.get_state(_eid)
                        if _val not in (None, "unknown", "unavailable"):
                            try:
                                _preload_post_reads[_m_name][_ch_name] = {
                                    "ts":    datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                                    "value": float(_val),
                                }
                                _has_preload_read = True
                            except (ValueError, TypeError):
                                pass

                # ── 12-hour gap-fill limit ────────────────────────────────
                _startup_gap_hours = len(missing_windows) * _lb_bm / 60.0
                if _startup_gap_hours > 12:
                    logger.warning(
                        "engine_startup: gap of %.1f hours exceeds 12-hour limit — "
                        "gap-fill skipped. Data will be absent for this window.",
                        _startup_gap_hours
                    )
                    # ── Detect possible meter replacement ─────────────────────
                    try:
                        _su_pre  = pre_reads.get("electricity_main", {}).get("import", {})
                        _su_post = _preload_post_reads.get("electricity_main", {}).get("import", {})
                        _su_pre_val  = float(_su_pre.get("value",  0)) if isinstance(_su_pre,  dict) else None
                        _su_post_val = float(_su_post.get("value", 0)) if isinstance(_su_post, dict) else None
                        if _su_pre_val is not None and _su_post_val is not None:
                            if _su_post_val < _su_pre_val - 50.0:
                                logger.warning(
                                    "engine_startup: meter read reset detected — "
                                    "pre-gap=%.3f kWh post-gap=%.3f kWh (drop=%.1f kWh). "
                                    "Possible meter replacement or property move.",
                                    _su_pre_val, _su_post_val, _su_pre_val - _su_post_val
                                )
                                _meter_reset_detected = True
                    except Exception as _su_re:
                        logger.debug("engine_startup: meter reset check failed: %s", _su_re)
                    # Reset current_block to current window — don't leave stale start
                    _cb_bm = int(get_block_minutes())
                    _cb_start2, _cb_end2 = get_block_window(
                        datetime.now(timezone.utc).replace(tzinfo=None), block_minutes=_cb_bm
                    )
                    _store.save_current_block(create_block(_cb_start2, _cb_end2, block_minutes=_cb_bm))

                elif _has_preload_read:
                    # We have sensor data and gap is within limit — fill immediately
                    logger.info("engine_startup: filling gap immediately using preloaded sensor states")
                    _startup_sc = 0.0
                    try:
                        _startup_sc = _store.last_standing_charge("electricity_main") or 0.0
                    except Exception:
                        pass
                    _startup_gap_blocks = build_gap_blocks(
                        missing_windows, pre_reads, _preload_post_reads,
                        last_rates, config,
                        last_standing_charge=_startup_sc
                    )
                    # (build_gap_blocks has attributed their carbon — within 60 minutes — or left
                    # it for the recovery sweep; the unbounded copy here went in engine step 5b)
                    for _gb in _startup_gap_blocks:
                        # Skip if an interpolated block already exists for this
                        # window — prevents a double gap-fill when engine_startup
                        # runs twice in rapid succession (consecutive HA
                        # disconnects), where the second fill would overwrite the
                        # first with slightly different interpolated values.
                        # (Regression: 2.10.9.)
                        _gb_start = _gb.get("start")
                        if _gb_start:
                            if _store.has_interpolated_block(_gb_start):
                                logger.info(
                                    "engine_startup: gap block %s already exists "
                                    "(interpolated) — skipping", _gb_start
                                )
                                continue
                        append_block_replace(_gb)
                    logger.info("engine_startup: %d gap blocks filled at startup", len(_startup_gap_blocks))
                    # No gap marker needed — gap already filled
                else:
                    # Sensors still unavailable after wait — fall back to deferred fill
                    logger.warning("engine_startup: no preloaded reads available — deferring gap fill")
                    set_gap_marker(current_block, pre_reads, last_rates,
                                   last_block_start=last_block_start)
                    _store.save_current_block(current_block)
                    logger.info("engine_startup: gap marker set, will fill on first sensor capture")
            else:
                logger.info("engine_startup: no session gap detected")
                # ── Re-seed opener after a within-block (no-gap) restart ──────
                # The rogue-total guard above cleared the in-progress block's
                # reads, including the carry-seed opener. With no gap, gap-fill
                # does not run to restore it, so the block would under-count
                # (measure only the tail). Restore the opener from last_block's
                # read_end. Self-heals at DCC settlement anyway, but this fixes
                # the provisional kWh (and is the only fix for mini-only setups).
                try:
                    _rs_cb = _store.load_current_block()
                    if _rs_cb and last_block:
                        _seeded = _reseed_opener_after_short_restart(last_block, _rs_cb)
                        if _seeded:
                            _store.save_current_block(_rs_cb)
                            logger.info(
                                "engine_startup: re-seeded in-progress block opener "
                                "from last_block read_end after within-block restart "
                                "(%s)", ", ".join(_seeded))
                except Exception as _rse:
                    logger.warning("engine_startup: opener re-seed failed: %s", _rse)

    # ── Seed _current_slot_mix from recent DB data ───────────────────────
    # _current_slot_mix is in-memory and lost on restart. Re-seed from
    # generation_mix rows for the last 2 hours so that blocks finalising
    # immediately after startup get mix data without waiting for the next
    # CI tick (which fires every 30 min and might race with block finalise).
    global _current_slot_mix
    try:
        # NB this window used to be wider than it read. See
        # BlockStore.get_generation_mix_since: the old `datetime('now',
        # '-4 hours')` comparison matched every block from the same CALENDAR DAY,
        # because SQLite renders that with a space where block_start carries a
        # 'T'. The seed is now the 4 hours it always claimed to be.
        _seed_slots = _store.get_generation_mix_since(hours=4)
        for bs, mix in _seed_slots.items():
            _current_slot_mix[bs] = mix
        if _seed_slots:
            logger.info(
                "engine_startup: seeded _current_slot_mix with %d slots from DB",
                len(_seed_slots)
            )
            # Also backfill mix_history from generation_mix if mix_history is sparse
            try:
                mh_count = _store.count_mix_history()
                if mh_count == 0:
                    logger.info("engine_startup: backfilling mix_history from generation_mix")
                    for bs, mix in _seed_slots.items():
                        _store.upsert_mix_history(bs, mix)
            except Exception as _mh_err:
                logger.warning("engine_startup: mix_history backfill failed: %s", _mh_err)
    except Exception as _seed_err:
        logger.warning("engine_startup: could not seed slot mix from DB: %s", _seed_err)

    # ── Fire CI tick immediately so mix data is available for next finalise ─
    # If seed was empty (fresh install or long downtime), fetch mix now rather
    # than waiting up to 30 minutes for the scheduled CI tick.
    if not _current_slot_mix:
        try:
            await _tick_carbon_intensity()
            logger.info("engine_startup: immediate CI tick fired (no seed data)")
        except Exception as _ci_err:
            logger.warning("engine_startup: immediate CI tick failed: %s", _ci_err)

    # ── 3.0.0 Kraken discovery (read-only; no polling yet) ────────────────
    # Runs BEFORE the (potentially slow) startup chart render so the supplier API
    # is activated first — chart generation on a large history can take tens of
    # seconds and must not gate API discovery / polling behind it.
    # The agreements as stored first (G1 design §3): a restart with the API down still knows them.
    _load_stored_agreements()
    try:
        await _kraken_startup_discovery()
    except Exception as _kd_e:
        logger.warning("engine_startup: kraken discovery skipped: %s", _kd_e)

    # The settle sweep (engine step 4d) settles the history once per _SETTLE_HISTORY_VERSION, and
    # gives what settle() declines its segments from its own price (4e). It replaced the history
    # reprice sweep, which replaced the three legacy backfills (BL-33).
    try:
        _maybe_settle_history()
    except Exception as _rh_e:
        logger.warning("engine_startup: settle sweep schedule failed: %s", _rh_e)


    # ── One-time region-timeline reconciliation (historical-carbon foundation) ──
    # With the API just discovered, stamp the DNO region (OUTWARD CODE ONLY) onto
    # the config-period timeline from the account's supply address. The first run
    # on a DB overwrites a legacy hand-entered postcode (e.g. a wrong region) with
    # the authoritative one; later runs respect manual edits. Best-effort.
    try:
        await _probe_and_apply_region_periods()
    except Exception as _rp_e:
        logger.warning("engine_startup: region probe skipped: %s", _rp_e)

    # Teardown guard for a Change Setup api→cad transition. When the mode no
    # longer uses the API, _kraken_startup_discovery returns early and never
    # calls _maybe_setup_mini, so a Mini reader wired in a prior api+mini session
    # would otherwise PERSIST (same process) and keep collecting into cad blocks.
    # Clear it explicitly. (The poll task is already cancelled at startup top and
    # not relaunched below — gated on mode_uses_api — so no poll leak.)
    _teardown_mini_if_no_api()

    # If an API mode is configured and discovery succeeded, ensure the DCC poll
    # task is running. Only relevant when the engine loop is ALREADY up — i.e. a
    # mid-session engine_startup re-run (config save). At first boot _engine_ha
    # is still None and main()'s gather() launches the poll task itself, so we
    # skip here to avoid a redundant (and previously misleading) call. Launching
    # HERE rather than from connect_kraken_now ensures the store + client are
    # rebuilt first (avoids the "Connector is closed" teardown race).
    if (_engine_ha is not None and _kraken_client is not None
            and _kraken_discovery and mode_uses_api()):
        _ensure_kraken_poll_task_running()

    # Mark cold start done: any later engine_startup in this process (HA
    # reconnect / config-save) is warm and must not run the rogue-total guard.
    _cold_start_complete = True
    logger.info("engine_startup: complete")