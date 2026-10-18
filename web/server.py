"""
server.py
=========
Flask web server serving:
  - Config UI  (meter editor with live HA entity picker)
  - Chart viewer (net heatmap + daily usage)
  - Data import
  - REST API for the UI

Runs on port 8099 inside the add-on container.
Started as a background thread from main.py alongside the engine.
"""

import gzip as _gzip
import json
import logging
import os
import re
import threading
from pathlib import Path

from flask import Flask, Response, jsonify, make_response, redirect, render_template, request, send_file, url_for

logger = logging.getLogger("server")

app = Flask(__name__, template_folder="templates")


# ── Response compression ──────────────────────────────────────────────────────
# HA's ingress proxy gzips responses, but the add-on's own web server (direct
# :8099 access, and Lovelace iframe/webpage cards) does not — so large text
# responses like the multi-MB daily chart HTML transferred uncompressed and took
# 15-20s to download on the direct port. Compress compressible text bodies when
# the client advertises gzip. Safe under ingress: a body already carrying
# Content-Encoding is passed through untouched, so nginx never double-compresses.
_GZIP_MIN_BYTES = 1024
_GZIP_TYPES = ("text/html", "text/css", "text/plain", "text/javascript",
               "application/javascript", "application/json", "application/xml",
               "image/svg+xml")


@app.after_request
def _compress_response(response):
    try:
        if "gzip" not in (request.headers.get("Accept-Encoding") or "").lower():
            return response
        if not (200 <= response.status_code < 300):
            return response
        if response.headers.get("Content-Encoding"):
            return response
        ctype = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in _GZIP_TYPES:
            return response
        # Materialise the body — send_file responses use direct_passthrough.
        response.direct_passthrough = False
        data = response.get_data()
        if len(data) < _GZIP_MIN_BYTES:
            return response
        compressed = _gzip.compress(data, compresslevel=6)
        if len(compressed) >= len(data):
            return response
        response.set_data(compressed)
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Content-Length"] = str(len(compressed))
        vary = response.headers.get("Vary")
        if not vary:
            response.headers["Vary"] = "Accept-Encoding"
        elif "accept-encoding" not in vary.lower():
            response.headers["Vary"] = vary + ", Accept-Encoding"
    except Exception as e:
        logger.warning("response compression failed: %s", e)
    return response


def _read_version() -> str:
    """Read version from config.yaml — works in both supervised and standalone modes."""
    # config.yaml sits one directory above server.py (web/../config.yaml)
    candidates = [
        os.path.join(os.path.dirname(__file__), "..", "config.yaml"),
        "/addons/energy_meter_tracker/config.yaml",
    ]
    for path in candidates:
        try:
            with open(os.path.normpath(path)) as f:
                for line in f:
                    if line.startswith("version:"):
                        return line.split(":", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    return ""

APP_VERSION = _read_version()


def _read_build_id() -> str:
    """What an open page needs to know it is stale (18 Oct 2026, the Pi): a short hash of the
    version and every page template, worked out once at start. A page keeps running the code it
    was served with across an add-on update — the heatmap's fix (#205) did nothing until the page
    was reloaded — and dev builds share a version string, so the version alone cannot tell."""
    import hashlib
    h = hashlib.sha1(APP_VERSION.encode())
    tdir = os.path.join(os.path.dirname(__file__), "templates")
    try:
        for name in sorted(os.listdir(tdir)):
            if name.endswith(".html"):
                with open(os.path.join(tdir, name), "rb") as f:
                    h.update(name.encode()); h.update(f.read())
    except Exception:
        pass
    return h.hexdigest()[:12]


BUILD_ID = _read_build_id()

# Per-instance display label for the footer. This is deliberately an INSTALL
# identity, not a data one: it must NOT come from the database (site name lives
# in the config-period chain and travels with a restore, so prod-dev restored
# from prod would show prod's name). Resolution order, highest first:
#   1. the `instance_name` option (exported as EMT_INSTANCE_NAME by run.sh, or
#      set directly in a standalone container) — the explicit per-instance
#      override, and the ONLY distinguisher when two installs share one manifest
#      name (the repo-URL workaround);
#   2. the add-on's manifest `name` via Supervisor self-info ("… (DEV)");
#   3. the container hostname (standalone last resort).
# Memoised: resolved once per process (never changes at runtime).
_INSTANCE_LABEL: "str | None" = None


def _instance_label() -> str:
    global _INSTANCE_LABEL
    if _INSTANCE_LABEL is not None:
        return _INSTANCE_LABEL
    # 1. Explicit per-instance override (the `instance_name` option).
    label = (os.environ.get("EMT_INSTANCE_NAME") or "").strip()
    # 2. Supervised: the add-on's manifest name (distinct per install).
    if not label:
        token = os.environ.get("SUPERVISOR_TOKEN")
        if token:
            try:
                import urllib.request
                req = urllib.request.Request(
                    "http://supervisor/addons/self/info",
                    headers={"Authorization": f"Bearer {token}"})
                with urllib.request.urlopen(req, timeout=3) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                label = ((data.get("data") or {}).get("name") or "").strip()
            except Exception as e:
                logger.info("_instance_label: supervisor self-info failed (%s)", e)
    # 3. Standalone last resort: the container hostname.
    if not label:
        import socket
        label = (socket.gethostname() or "").strip()
    _INSTANCE_LABEL = label
    return label


@app.context_processor
def inject_globals():
    has_power_sensor = False
    has_postcode = False
    try:
        from energy_engine_io import load_json as _lj
        cfg = load_config()
        for m_data in cfg.get("meters", {}).values():
            if not (m_data.get("meta") or {}).get("sub_meter"):
                _meta = (m_data.get("meta") or {})
                # A live-power source is a configured power_sensor entity (device
                # sensor or auto-adopted BCD) OR the Octopus Mini opted in as the
                # source (power_source=="mini" — not an HA entity, polled via API).
                has_power_sensor = (bool(_effective_power_sensor(_meta))
                                    or _meta.get("power_source") == "mini")
                has_postcode     = bool(_meta.get("postcode_prefix", "").strip())
                break
    except Exception:
        pass
    return {"app_version": APP_VERSION, "build_id": BUILD_ID,
            "has_power_sensor": has_power_sensor, "has_postcode": has_postcode,
            "server_port": int(os.environ.get("EMT_PORT") or "8099"),
            "instance_label": _instance_label()}


class IngressMiddleware:
    """
    WSGI middleware that rewrites the PATH_INFO to strip the ingress prefix
    and sets SCRIPT_NAME so Flask url_for() generates correct URLs.
    """
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        ingress_path = environ.get("HTTP_X_INGRESS_PATH", "")
        if ingress_path:
            environ["SCRIPT_NAME"] = ingress_path
            path = environ.get("PATH_INFO", "")
            if path.startswith(ingress_path):
                environ["PATH_INFO"] = path[len(ingress_path):] or "/"
        return self.app(environ, start_response)

app.wsgi_app = IngressMiddleware(app.wsgi_app)
app.secret_key = os.urandom(24)

# ── Paths (injected from main.py before server starts) ────────────────────────
DATA_DIR         = None
import os as _os


def _share_backup_dir() -> str:
    """BL-5: the backup dir is resolved at startup from the site slug (see
    instance.py) and lives on `engine`. Read it there rather than keeping a
    second, stale copy — this module used to duplicate the derivation, which
    would silently disagree after a site rename."""
    try:
        import engine as _eng
        return _eng.SHARE_BACKUP_DIR
    except Exception:
        return (_os.path.join("/data/energy_meter_tracker", "backup")
                if _os.environ.get("EMT_MODE") == "standalone"
                else "/share/energy_meter_tracker_backup")

_ha_client = None   # reference to the running HAClient instance
_event_loop = None  # asyncio event loop — captured at init time

# BlockStore — opened lazily on first use, shared across all server requests
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(__file__)))

import store_access                 # workstream A: store() / writer()
from job_coordinator import job     # workstream B: coordinator membership
from block_store import BlockStore, open_block_store
_store: BlockStore | None = None


# Set ONLY by the suite, via tests/test_server.py::_use_store. A background
# worker opens its own connection (below), which a test cannot reach into — so an
# injected test store would be ignored and the worker would operate on an empty
# database instead. Same deliberate hole as `store_access.bind_object_for_tests`
# (A §5a), and conspicuous for the same reason: production never assigns it, so
# it shows up in a diff if anything ever does.
_own_store_for_tests = None


def _own_store() -> BlockStore:
    """A BlockStore opened FOR THE CALLING THREAD, to be closed by it.

    Background workers (`_delete_worker`, `_purge_worker`, `_restore_worker`) run
    long bulk mutations on their own threads. They cannot hop onto the engine
    loop — a whole-history delete or a restore would stall live capture for
    minutes, the same reason `api_db_vacuum` is a permanent exception (B §4) —
    and they must not share a connection with a request thread.

    So each opens its own, which is the pattern `_create_backup_zip` already
    uses. That is what lets `check_same_thread=False` come off the shared
    connection: a SQLite connection then belongs to exactly one thread, and the
    default check enforces it rather than a comment asking nicely.
    """
    if _own_store_for_tests is not None:
        return _own_store_for_tests
    if DATA_DIR is None:
        raise RuntimeError("server.init() has not been called")
    return open_block_store(_os.path.join(DATA_DIR, "blocks.db"))


def _release_store(st) -> None:
    """Close a store obtained from `_own_store()`, if we opened it.

    Paired with `_own_store` rather than inlined as `st.close()`, because under
    the test override the returned store is the SUITE's — shared across every
    test in the file — and closing it made 27 unrelated tests fail on a closed
    database. A worker must close what it opened and only what it opened.
    """
    if st is None or st is _own_store_for_tests:
        return
    try:
        st.close()
    except Exception:
        pass


def _get_store() -> BlockStore:
    """Return the server's BlockStore, opening it lazily on first call."""
    global _store
    if _store is None:
        if DATA_DIR is None:
            raise RuntimeError("server.init() has not been called")
        db_path = _os.path.join(DATA_DIR, "blocks.db")
        _store = open_block_store(db_path)
    return _store


# BL-18c: per-THREAD read-only store. Waitress serves requests on multiple threads;
# sharing the single RW `_store` connection across them caused intermittent
# SQLITE_MISUSE ("bad parameter or other API misuse") when concurrent page-load
# reads (e.g. the corrections page's review-blocks + billing-source) hit the same
# connection at once. Each thread gets its OWN read snapshot connection (query_only +
# busy_timeout), safe alongside the engine's writer under WAL. Reads only — writes
# still go through _get_store().
_read_local = threading.local()


# `_get_read_store()` retired — workstream A §3.4 ("one mechanism, not three").
# It was the third invention of the read companion in v4 and reached 0% adoption.
# `store_access.store()` replaces it, without its fallback: that one ended
# `return _get_store()` when it could not open its own connection, so the safe
# call could hand back the unsafe object.
#
# The line above it still stands, and is the open half of this: "Reads only —
# writes still go through _get_store()". `_store` is a module global and
# waitress serves on four threads, so every POST/PUT/DELETE handler writes
# through ONE connection shared by all of them — the same condition that
# produced SQLITE_MISUSE for reads, acknowledged and never closed. A §3.3 says
# how it closes: a route causes a write by asking the engine to perform it.


def _config_timezone() -> str:
    """The main meter's configured timezone (for wall-clock display of UTC
    block_start values in the gap / deleted-ranges lists). Falls back to UTC."""
    try:
        _cfg = load_config()
        for _md in (_cfg.get("meters") or {}).values():
            if not (_md.get("meta") or {}).get("sub_meter"):
                return (_md.get("meta") or {}).get("timezone", "UTC")
    except Exception:
        pass
    return "UTC"



# Fold near-identical stored rates (float / rounding artifacts -- e.g. 0.054929 vs the
# canonical 0.054930, ~1e-6 apart) into the dominant value so the nearby-rate picker
# never splits off a confusing 1-block option. STRICT < epsilon: Agile unit rates step
# in 0.01p = 1e-4 increments, so two genuinely-distinct Agile rates exactly 1e-4 apart
# are NEVER merged.
_RATE_CLUSTER_EPS = 1e-4


def _cluster_nearby_rates(rows, eps: float = _RATE_CLUSTER_EPS):
    """rows: list of {"rate": float, "count": int}, assumed sorted by count DESC. Folds
    any rate within < eps of an already-accepted (higher-count) representative into it,
    summing counts. Returns the reduced list re-sorted by count DESC then rate; the most
    common exact value in each cluster is kept as the representative."""
    reps = []  # [rate, count]
    for r in rows:
        rate, cnt = r["rate"], r["count"]
        for rep in reps:
            if abs(rate - rep[0]) < eps:
                rep[1] += cnt
                break
        else:
            reps.append([rate, cnt])
    reps.sort(key=lambda x: (-x[1], x[0]))
    return [{"rate": rr, "count": cc} for rr, cc in reps]


def init(data_dir: str, ha_client):
    global DATA_DIR, _ha_client, _event_loop
    import asyncio as _asyncio
    _event_loop = _asyncio.get_event_loop()
    DATA_DIR   = data_dir
    _ha_client = ha_client
    # A staged upload is only ever confirmed by the page that staged it, in this process:
    # after a restart none can be, so none is kept (first-run design §2.2).
    _remove_stale_stages(max_age=0)
    # Workstream A §5b: store_access.bind() is NOT called here. It moved to
    # main.py, ahead of BOTH engine_startup() and this init(), because the read
    # path has to serve the pre-wizard state -- a fresh install, or one just
    # reset by api_meter_main_reset, runs with no configuration at all, and the
    # setup wizard it must serve is read-only. bind() takes a path and nothing
    # else, so it has no business depending on either subsystem's lifecycle.


def _run_on_engine_loop(coro, timeout: float = 30.0):
    """Run an async coroutine on the ENGINE's running event loop and block this
    (Flask) thread on the result.

    This is the ONLY correct way for a sync route to await something that uses
    the engine's aiohttp session (HA client, Kraken client): those objects bind
    their connector/timeout to the loop they were created on. Spinning up a
    fresh loop with run_until_complete() raises "Timeout context manager should
    be used inside a task". Raises RuntimeError if the loop isn't available.
    """
    import asyncio as _asyncio
    if not (_event_loop and _event_loop.is_running()):
        raise RuntimeError("engine event loop not running")
    fut = _asyncio.run_coroutine_threadsafe(coro, _event_loop)
    return fut.result(timeout=timeout)


def _engine_write(op, timeout: float = 30.0):
    """Perform a store MUTATION on the engine thread and block this one.

    A route must never write through its own thread. v4 had three that did, and
    engine.py names the consequence: *"a DB write from the executor thread races
    the loop-thread writers on that one connection and corrupts it: 'cannot
    commit - no transaction is active' / 'error return without exception
    set'"*. Under workstream A `writer()` raises off the engine thread, so this
    hop is the only way a route can cause a write.

    `op` receives the engine's store and runs on the loop.
    """
    import engine as _eng

    async def _do():
        return op(_eng.get_store())

    return _run_on_engine_loop(_do(), timeout=timeout)


def _engine_call(fn, *args, timeout: float = 30.0, **kwargs):
    """Run an ENGINE function that writes engine state on the engine thread, and
    block this one on its result. For the engine's own mutators
    (`set_data_source_mode`, `apply_billing_source_change`) that write through the
    engine's `_store` — which raises off the engine thread (A §5.5)."""
    if _event_loop is None or not _event_loop.is_running():
        return fn(*args, **kwargs)      # no engine loop (tests, pre-startup)

    async def _do():
        return fn(*args, **kwargs)

    return _run_on_engine_loop(_do(), timeout=timeout)


def _reset_engine_store(timeout: float = 30.0):
    """Reopen the engine's store on the ENGINE thread after the database file was
    replaced or its meters changed.

    `reset_store()` opens the engine's new connection on whichever thread calls
    it. Called from a request thread (retire, unretire, import-from-zip did), the
    engine would be left holding a connection it may not use — every engine write
    raising until restart. The restore worker already hopped onto the loop for this;
    now every caller does, through here.
    """
    import engine as _eng
    if _event_loop is not None and _event_loop.is_running():
        async def _co():
            _eng.reset_store()
        return _run_on_engine_loop(_co(), timeout=timeout)
    return _eng.reset_store()      # no engine loop (tests, pre-startup)


class _ConfigError(Exception):
    """A config-period mutation validation error → surfaced as HTTP 400."""


class _ConfigBusy(Exception):
    """The mutation was submitted to the engine loop but the loop was busy (typically a
    long post-restore engine_startup, made slower by an HA WebSocket drop), so we
    stopped waiting. run_coroutine_threadsafe is NOT cancelled by a result-timeout —
    the coroutine stays queued and WILL apply once the loop frees. The endpoint reports
    an honest 'queued' instead of a misleading raw TimeoutError."""


_CONFIG_BUSY_MSG = ("The engine is busy finishing a restore or reconnecting — your "
                    "change has been queued and should apply within a minute. Refresh "
                    "to confirm.")


def _config_mutation_on_loop(mutate, timeout: float = 30.0):
    """Run a config_periods read+write on the ENGINE's loop and connection, then
    schedule an OFF-loop chart regen (#361).

    Config edits used to write on the shared web BlockStore from the Flask request
    thread, racing the engine's writes on its own connection — producing SQLite
    "database is locked" / "another row available", and (when a write half-applied)
    a Total Bill that no longer summed until the next finalise regenerated the
    charts. Running the mutation on the engine loop serialises it with every other
    engine write (one connection, one thread). `mutate(store)` receives the engine's
    store and returns the JSON-able result. No engine loop (tests / pre-init) →
    there is no concurrent writer, so run inline on the web store."""
    if not (_event_loop and _event_loop.is_running()):
        return mutate(_get_store())

    # ONE mechanism (A §3.4). `_engine_write` is the primitive — run this
    # mutation on the engine thread. (It was that plus a chart regen until C
    # deleted the pre-rendered charts, 30 Sep 2026.)
    import concurrent.futures as _cf
    try:
        return _engine_write(mutate, timeout=timeout)
    except _cf.TimeoutError:
        # The loop was busy (e.g. a slow post-restore engine_startup). The mutation is
        # still queued and will apply — signal 'queued' so the endpoint doesn't report
        # a raw TimeoutError for a change that actually succeeds.
        logger.warning("_config_mutation_on_loop: engine loop busy after %.0fs — "
                       "mutation left queued (will apply once the loop frees)", timeout)
        raise _ConfigBusy()


def start():
    """Start Flask in a background daemon thread."""
    t = threading.Thread(target=_run, daemon=True, name="flask")
    t.start()
    _port = int(os.environ.get("EMT_PORT") or "8099")
    logger.info("server: Flask started on port %d", _port)


def _run():
    import logging as _logging
    _log = _logging.getLogger("server")
    try:
        from waitress import serve
        _port = int(os.environ.get("EMT_PORT") or "8099")
        _log.info("server: waitress binding on port %d", _port)
        serve(app, host="0.0.0.0", port=_port, threads=4)
    except Exception as _e:
        _log.critical("server: waitress failed: %s", _e, exc_info=True)
        raise


# ── Settings defaults ─────────────────────────────────────────────────────────

# Carbon assumption defaults with citations.
# Stored in store_meta['settings'] as JSON; missing keys fall back to these.
SETTINGS_DEFAULTS = {
    # ── Equivalence factors ────────────────────────────────────────────────
    "co2_car_petrol_g_per_mile":   180.0,  # BEIS/DESNZ 2023 GHG conversion factors
    "co2_car_diesel_g_per_mile":   168.0,  # BEIS/DESNZ 2023 GHG conversion factors
    "co2_tree_kg_per_year":          21.0, # Woodland Trust estimate
    "co2_flight_lhr_nyc_kg":        670.0, # BEIS 2023 (economy, radiative forcing)
    # ── Display ────────────────────────────────────────────────────────────
    "distance_unit":              "miles", # miles | km
    # ── Export methodology ─────────────────────────────────────────────────
    "co2_export_method":       "grid_average",  # grid_average | custom
    "co2_export_custom_intensity":  200.0, # gCO₂/kWh — only if method=custom
    # ── EV ─────────────────────────────────────────────────────────────────
    "ev_efficiency":                  3.2, # miles/kWh or km/kWh (distance_unit)
    "ev_charge_efficiency":          0.88, # AC→DC charge efficiency (IEA 2022, typical Type 2)
    # ── Battery ────────────────────────────────────────────────────────────
    "battery_round_trip_efficiency": 0.90, # round-trip AC→DC→AC (typical Li-ion home battery)
    # ── Heat pump ──────────────────────────────────────────────────────────
    "hp_cop":                         3.0, # seasonal COP (SCOP) — override with manufacturer spec
    "gas_co2_g_per_kwh":            203.0, # BEIS/DESNZ 2023 GHG conversion factors
    "gas_boiler_efficiency":         0.90, # typical modern condensing boiler
    # ── Bill rounding (BL-24) ──────────────────────────────────────────────
    # Opt-in: show the Billing-tab bill summary with Octopus's bill-rounding
    # method (Guy Lipman ladder) on the stored ex-VAT figures. Default off ⇒ the
    # summary is byte-identical to today.
    "bill_rounding_summary":        False,  # bool
}

SETTINGS_NUMERIC = {
    "co2_car_petrol_g_per_mile",
    "co2_car_diesel_g_per_mile",
    "co2_tree_kg_per_year",
    "co2_flight_lhr_nyc_kg",
    "co2_export_custom_intensity",
    "ev_efficiency",
    "ev_charge_efficiency",
    "battery_round_trip_efficiency",
    "hp_cop",
    "gas_co2_g_per_kwh",
    "gas_boiler_efficiency",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def config_path():
    return os.path.join(DATA_DIR, "meters_config.json")


def _rebuild_config_period_chain(store):
    """
    Re-sort all config periods by effective_from and rebuild the contiguous
    chain: each period's effective_to is set to the next period's effective_from,
    with the last (most recent) period getting effective_to = NULL.

    Also reassigns blocks so every block's config_period_id matches the period
    whose [effective_from, effective_to) range contains the block's block_start.

    Called after any insert, edit or delete of a config period.
    """
    cur = store._conn.execute(
        "SELECT id, effective_from FROM config_periods ORDER BY effective_from ASC"
    )
    periods = cur.fetchall()
    if not periods:
        return

    # Build effective_to for each period
    updates = []
    for i, row in enumerate(periods):
        if i + 1 < len(periods):
            updates.append((periods[i + 1]["effective_from"], row["id"]))
        else:
            updates.append((None, row["id"]))

    store._conn.execute("BEGIN")
    for effective_to, period_id in updates:
        store._conn.execute(
            "UPDATE config_periods SET effective_to = ? WHERE id = ?",
            (effective_to, period_id)
        )

    # Reassign blocks: each block goes to the period containing its block_start
    # Fetch updated periods
    cur2 = store._conn.execute(
        "SELECT id, effective_from, effective_to FROM config_periods ORDER BY effective_from ASC"
    )
    chain = cur2.fetchall()
    for i, period in enumerate(chain):
        pid          = period["id"]
        ef_from      = period["effective_from"]
        ef_to        = period["effective_to"]
        if ef_to is not None:
            store._conn.execute(
                """UPDATE blocks SET config_period_id = ?
                   WHERE block_start >= ? AND block_start < ?""",
                (pid, ef_from, ef_to)
            )
        else:
            # Last period: all blocks from effective_from onwards
            store._conn.execute(
                """UPDATE blocks SET config_period_id = ?
                   WHERE block_start >= ?""",
                (pid, ef_from)
            )

    store._conn.execute("COMMIT")


def load_config():
    """
    Load meter configuration from the normalised DB (blocks.db) config_periods table.
    The DB is the single source of truth — meters_config.json is only used as a
    last resort for a truly fresh install where blocks.db does not yet exist.
    """
    db_path = os.path.join(DATA_DIR, "blocks.db") if DATA_DIR else None
    db_exists = db_path and os.path.exists(db_path)

    _load_attempts = 3
    for _attempt in range(_load_attempts):
        try:
            store = store_access.store()
            cp = store._conn.execute(
                "SELECT id FROM config_periods "
                "WHERE effective_to IS NULL ORDER BY effective_from DESC LIMIT 1"
            ).fetchone()
            if cp:
                return store.config_from_db(cp["id"])
            # DB exists but no config period — may be a transient WAL lock.
            # Retry before concluding there is genuinely no config period.
            if db_exists and _attempt < _load_attempts - 1:
                import time as _time
                _time.sleep(0.1)
                continue
            if db_exists:
                logger.warning("load_config: blocks.db exists but no active config period found")
                return {"schema_version": "1.0", "meters": {}}
        except Exception as e:
            if _attempt < _load_attempts - 1:
                import time as _time
                _time.sleep(0.1)
                continue
            logger.error("load_config: failed to read config from DB: %s", e)
            # Only fall back to JSON if no DB exists at all
            if db_exists:
                logger.error("load_config: DB exists but config read failed — returning empty config")
                return {"schema_version": "1.0", "meters": {}}
        break

    # True fresh install: no DB yet, try meters_config.json
    p = config_path()
    if not os.path.exists(p):
        return {"schema_version": "1.0", "meters": {}}
    logger.info("load_config: no DB found, loading from meters_config.json")
    with open(p) as f:
        return json.load(f)


def save_config(data: dict):
    """
    Save meter configuration. The DB (normalised meters/meter_channels tables)
    is the authoritative store. meters_config.json is written as a
    human-readable export only and is not read back as live state.
    """
    # Preserve channel meta from existing config that the UI doesn't manage.
    # Read from DB (authoritative) rather than the JSON file.
    try:
        existing = load_config()
        for meter_id, meter in existing.get("meters", {}).items():
            for ch_id, ch in meter.get("channels", {}).items():
                if "meta" in ch:
                    try:
                        data["meters"][meter_id]["channels"][ch_id].setdefault("meta", ch["meta"])
                    except (KeyError, TypeError):
                        pass
    except Exception:
        pass

    # Write to the active config period in the DB (normalised tables)
    try:
        # #361 / A §3.3: this runs on the ENGINE thread now. It used to write
        # through the shared web BlockStore from a Flask request thread — the
        # same race `_config_mutation_on_loop` was written for, on the path
        # that saves the whole configuration.
        def _mutate(store):
            main_meta = {}
            for md in data.get("meters", {}).values():
                if not (md.get("meta") or {}).get("sub_meter"):
                    main_meta = md.get("meta") or {}
                    break
            active = store._conn.execute(
                "SELECT id FROM config_periods WHERE effective_to IS NULL "
                "ORDER BY effective_from DESC LIMIT 1"
            ).fetchone()
            if active:
                period_id = active["id"]
                with store._conn:
                    # Update billing scalar fields on config_periods
                    store._conn.execute(
                        """UPDATE config_periods
                           SET billing_day     = ?,
                               block_minutes   = ?,
                               timezone        = ?,
                               currency_symbol = ?,
                               currency_code   = ?,
                               site_name       = ?
                           WHERE id = ?""",
                        (
                            int(main_meta.get("billing_day") or 1),
                            int(main_meta.get("block_minutes") or 30),
                            main_meta.get("timezone", "UTC"),
                            main_meta.get("currency_symbol", "£"),
                            main_meta.get("currency_code", "GBP"),
                            main_meta.get("site"),
                            period_id,
                        )
                    )
                    # Rewrite normalised meter rows for this period
                    # Delete existing rows first (upsert handles adds/updates;
                    # deletion handles meters removed from config)
                    old_meter_ids = [r["id"] for r in store._conn.execute(
                        "SELECT id FROM meters WHERE config_period_id=?", (period_id,)
                    ).fetchall()]
                    for mid in old_meter_ids:
                        store._conn.execute(
                            "DELETE FROM meter_channels WHERE meter_id=?", (mid,)
                        )
                    store._conn.execute(
                        "DELETE FROM meters WHERE config_period_id=?", (period_id,)
                    )
                    store._write_meters(data, period_id)

        _engine_write(_mutate)
    except Exception as _e:
        logger.error("save_config: DB write failed: %s", _e)
        raise

    # Write meters_config.json as a convenience export (not read back as state)
    try:
        p = config_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, p)
    except Exception as _e:
        logger.warning("save_config: could not write meters_config.json export: %s", _e)


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    cfg = load_config()
    if cfg.get("meters"):
        last = request.cookies.get("emt_last_page", "charts")
        # Map old cookie values to new page names for backwards compatibility
        page_map = {
            "charts":   "charts",
            "summary":  "live_power",
            "import":   "data_management",
            "logs":     "logs",
            "help":     "help",
            "config":   "settings",
            # new names
            "live_power":     "live_power",
            "data_management": "data_management",
            "settings":       "settings",
            "insights":       "insights",
        }
        last = page_map.get(last, "charts")
        return redirect(url_for(last + "_page"))
    # No configuration: the first-run fork — new setup, or restore a backup (first-run design
    # §1). A restore running into an empty instance lands there too, and the page follows it.
    return redirect(url_for("welcome_page"))


@app.route("/welcome")
def welcome_page():
    # The floor is named from the gate, so the page cannot fall behind a raise (12 Oct 2026).
    return render_template("welcome.html", active="welcome",
                           floor=_import_gate().FLOOR_VERSION)


@app.route("/api/last-page", methods=["POST"])
def api_set_last_page():
    page = request.get_json(force=True).get("page", "charts")
    valid = {"charts", "live_power", "data_management", "logs", "help", "settings", "insights"}
    if page not in valid:
        page = "charts"
    resp = jsonify({"ok": True})
    resp.set_cookie("emt_last_page", page, max_age=60*60*24*365, samesite="Lax")
    return resp





@app.route("/static/logo.png")
def serve_logo():
    import os
    p = "/app/logo.png"
    if os.path.exists(p):
        return send_file(p, mimetype="image/png")
    return "", 404


@app.route("/static/icon.png")
def serve_icon():
    import os
    p = "/app/icon.png"
    if os.path.exists(p):
        return send_file(p, mimetype="image/png")
    return "", 404


@app.route("/favicon.ico")
def serve_favicon():
    # Belt-and-braces for the browser's default /favicon.ico request (helps
    # standalone; under ingress the <link> in base.html carries the correct path).
    import os
    p = "/app/icon.png"
    if os.path.exists(p):
        return send_file(p, mimetype="image/png")
    return "", 404


@app.route("/help")
def help_page():
    return render_template("help.html", active="help")


@app.route("/logs")
def logs_page():
    return render_template("logs.html", active="logs")


@app.route("/api/logs")
def api_logs():
    """Fetch add-on logs — via Supervisor API in supervised mode, log file in standalone."""
    import urllib.request
    lines = min(int(request.args.get("lines", 100)), 1000)
    emt_mode = os.environ.get("EMT_MODE", "supervised")

    if emt_mode == "standalone":
        # In standalone mode read from log file if available, otherwise return empty
        log_path = "/data/energy_meter_tracker/addon.log"
        try:
            if os.path.exists(log_path):
                with open(log_path, "r", errors="replace") as f:
                    all_lines = f.read().splitlines()
                return jsonify({"lines": all_lines[-lines:]})
            else:
                return jsonify({"lines": ["[Logs not available in standalone Docker mode]",
                                          "Run with -v /path/to/logs:/data/energy_meter_tracker",
                                          "or check docker logs <container_name>"]})
        except Exception as e:
            return jsonify({"error": str(e), "lines": []})
    else:
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        try:
            req = urllib.request.Request(
                "http://supervisor/addons/self/logs",
                headers={"Authorization": "Bearer " + token}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            all_lines = raw.splitlines()
            return jsonify({"lines": all_lines[-lines:]})
        except Exception as e:
            logger.error("api_logs: %s", e)
            return jsonify({"error": str(e), "lines": []})


@app.route("/charts")
def charts_page():
    try:
        block_count = store_access.store().count_blocks()
    except Exception:
        block_count = 0
    return render_template(
        "charts.html",
        block_count=block_count,
        active="charts",
    )


@app.route("/live-power")
def live_power_page():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from energy_engine_io import load_json as _load_json

    try:
        cfg      = load_config()
        store    = store_access.store()
        main_meta = {}
        for md in cfg.get("meters", {}).values():
            if not (md.get("meta") or {}).get("sub_meter"):
                main_meta = md.get("meta") or {}
                break
        currency    = main_meta.get("currency_symbol", "£")
        tz_name     = main_meta.get("timezone", "UTC")
        billing_day = int(main_meta.get("billing_day") or 1)
        _tz         = ZoneInfo(tz_name)
        now_local   = datetime.now(_tz)
        today_str   = now_local.date().isoformat()

        # ── Billing cards are populated by SSE on first tick — skip expensive
        # block queries on initial page load so the page renders instantly ──
        today_total = today_rows = today_date = None
        month_total = month_rows = month_period = None
        year_total  = year_rows  = year_period  = None
        today_date  = now_local.strftime("%d %b %Y")

        # ── Gauge scale — derived from 48hr power_history (real kW readings) ──
        # Falls back to sensible defaults when no power sensor is configured.
        block_minutes = int(main_meta.get("block_minutes") or 30)
        gauge_max_imp = 10
        gauge_max_exp = 5
        try:
            ph_rows = store.get_power_history(hours=48)
            if ph_rows:
                def _pct(vals, p):
                    s = sorted(vals)
                    return s[min(int(len(s) * p / 100), len(s) - 1)]
                def _nc(kw):
                    for s in [1, 2, 3, 5, 7, 10, 15, 20, 30, 50]:
                        if s >= kw: return s
                    return round(kw * 1.2)
                imp_vals = [r["net_kw"] for r in ph_rows if r["net_kw"] > 0]
                exp_vals = [-r["net_kw"] for r in ph_rows if r["net_kw"] < 0]
                if imp_vals:
                    gauge_max_imp = _nc(_pct(imp_vals, 95))
                if exp_vals:
                    gauge_max_exp = _nc(_pct(exp_vals, 95))
        except Exception as _ge:
            logger.warning("live_power_page: gauge scale from power_history failed: %s", _ge)

    except Exception as e:
        logger.error("summary_page: %s", e)
        currency = "£"
        month_total = today_total = year_total = None
        month_rows = today_rows = year_rows = []
        month_period = year_period = today_date = ""
        gauge_max_imp = 10; gauge_max_exp = 5

    # Check if a live-power source is configured: a power_sensor entity OR the
    # Octopus Mini opted in as the source (power_source=="mini").
    mini_chosen = main_meta.get("power_source") == "mini"
    has_power_sensor = bool(_effective_power_sensor(main_meta)) or mini_chosen

    # Sub-meter device cards (battery SoC / EV / heat-pump power) render
    # independently of the main gauge — each needs only its own sensor. Without
    # this flag the cards were gated on has_power_sensor and vanished on
    # API-only / DCC setups that have no main live-power source.
    has_sub_devices = False
    try:
        from datetime import datetime as _dt_sd
        _today_sd = _dt_sd.now().strftime("%Y-%m-%d")
        for _m in (cfg.get("meters") or {}).values():
            _mm = (_m or {}).get("meta") or {}
            if not _mm.get("sub_meter"):
                continue
            if _mm.get("retired_at") and _mm["retired_at"] <= _today_sd:
                continue
            if (_mm.get("soc_sensor") or _mm.get("inverter_power_sensor")
                    or _mm.get("device_power_sensor") or _mm.get("pv_power_sensor")):
                has_sub_devices = True
                break
    except Exception:
        has_sub_devices = False

    # Mini opt-in offer: a Mini is present AND there's no cheaper live source
    # (no power_sensor entity, no BCD demand sensor). Cheap — no GraphQL here.
    mini_device = bool(_mini_device_id())
    bcd_found = False
    try:
        import engine as _eng_mini
        bcd_found = bool(((_eng_mini._detected_integrations or {}).get("bcd") or {}).get("found"))
    except Exception:
        pass
    mini_available = mini_device and not main_meta.get("power_sensor") and not bcd_found

    # BL-10: gate the smart-charging card on there being IOG dispatch data. No gap
    # when absent — the card element isn't emitted at all.
    has_dispatch_data = False
    try:
        has_dispatch_data = bool(store_access.store()._conn.execute(
            "SELECT 1 FROM dispatch_history LIMIT 1").fetchone())
    except Exception:
        has_dispatch_data = False

    return render_template(
        "live_power.html",
        has_dispatch_data=has_dispatch_data,
        active="live_power",
        currency=currency,
        today_total=today_total,
        today_rows=today_rows,
        today_date=today_date,
        month_total=month_total,
        month_rows=month_rows,
        month_period=month_period,
        year_total=year_total,
        year_rows=year_rows,
        year_period=year_period,
        has_power_sensor=has_power_sensor,
        has_sub_devices=has_sub_devices,
        mini_available=mini_available,
        mini_chosen=mini_chosen,
        gauge_max_imp=gauge_max_imp,
        gauge_max_exp=gauge_max_exp,
        block_minutes=int(main_meta.get("block_minutes") or 30),
    )


def _is_meter_type(meta: dict, meter_id: str, expected_type: str) -> bool:
    """Check if a meter is of the given type using only the explicit meter_type field.
    No keyword fallback — meter_type must be explicitly set via the config UI (2.7.0+).
    """
    return (meta.get('meter_type') or '').lower() == expected_type


def _build_soc_response(soc_sensors, ha_client):
    """Safely build SoC sensor readings dict for the live power API response."""
    import engine as _eng_pc
    result = {}
    for m_id, info in soc_sensors.items():
        soc_val = None
        power_kw = None
        pv_kw = None
        try:
            if ha_client and info.get("soc_entity"):
                v = ha_client.get_state(info["soc_entity"])
                if v not in (None, "unknown", "unavailable"):
                    soc_val = round(float(v), 1)
        except (ValueError, TypeError):
            pass
        try:
            if ha_client and info.get("power_entity"):
                v = ha_client.get_state(info["power_entity"])
                if v not in (None, "unknown", "unavailable"):
                    unit = ""
                    try:
                        unit = (ha_client.get_attributes(info["power_entity"])
                                or {}).get("unit_of_measurement", "")
                    except Exception:
                        pass
                    # Shared converter: unit attr → kW (override when the declared
                    # unit is wrong/absent), then invert (positive = charging).
                    power_kw = _eng_pc._power_value_to_kw(
                        v, unit, info.get("power_unit"), info.get("power_invert"))
        except (ValueError, TypeError):
            pass
        # PV / solar generation — always positive, auto unit, no invert.
        try:
            if ha_client and info.get("pv_entity"):
                v = ha_client.get_state(info["pv_entity"])
                if v not in (None, "unknown", "unavailable"):
                    unit = ""
                    try:
                        unit = (ha_client.get_attributes(info["pv_entity"])
                                or {}).get("unit_of_measurement", "")
                    except Exception:
                        pass
                    pv_kw = _eng_pc._power_value_to_kw(v, unit, None, False)
        except (ValueError, TypeError):
            pass
        result[m_id] = {
            "label":        info.get("label", m_id),
            "type":         info.get("type", "battery"),
            "v2x":          info.get("v2x", False),
            "soc_entity":   info.get("soc_entity"),
            "power_entity": info.get("power_entity"),
            "pv_entity":    info.get("pv_entity"),
            "soc":          soc_val,
            "power_kw":     power_kw,
            "pv_kw":        pv_kw,
        }
    return result


def _mini_device_id():
    """The discovered Octopus Mini deviceId (from the engine's boundary reader),
    or None when no Mini is present / not yet probed."""
    try:
        import engine as _eng
        reader = getattr(_eng, "_kraken_mini_reader", None)
        return getattr(reader, "device_id", None) if reader else None
    except Exception:
        return None


def _mini_live_demand_kw():
    """Latest Mini demand in kW, read from the engine's cache (the engine polls
    the Mini server-side, page-independently). Returns None when there's no recent
    reading. No GraphQL call here — the engine is the single poller."""
    try:
        import engine as _eng
        import time as _time
        d = getattr(_eng, "_last_mini_demand", None) or {}
        kw = d.get("kw")
        wall = d.get("wall", 0.0)
        if kw is None:
            return None
        # Treat readings older than a few poll gaps as stale (Mini stopped / gone).
        if (_time.time() - wall) > 180.0:
            return None
        return kw
    except Exception:
        return None


def _bcd_demand_sensor():
    """BottlecapDave's current_demand entity (live-power sensor) when BCD is
    detected, else None. Used to auto-adopt BCD as the power sensor when the user
    hasn't configured one of their own — it's a free local feed, no API quota."""
    try:
        import engine as _eng
        bcd = (_eng._detected_integrations or {}).get("bcd") or {}
        return bcd.get("demand_sensor") if bcd.get("found") else None
    except Exception:
        return None


def _ohme_detection():
    """OHME charge-mode detection result, surfaced to the EV card so the hint can
    say whether EMT found an Ohme charge-mode entity (verified off-peak path) or
    fell back to the optimistic path. None-safe; always returns a plain dict."""
    try:
        import engine as _eng
        ohme = (_eng._detected_integrations or {}).get("ohme") or {}
        if ohme.get("found"):
            return {
                "found": True,
                "integration": ohme.get("integration"),
                "charge_mode_entity": ohme.get("charge_mode_entity"),
            }
    except Exception:
        pass
    return {"found": False, "integration": None, "charge_mode_entity": None}


def _effective_power_sensor(main_meta):
    """Resolve the live-power sensor entity: the user's configured power_sensor,
    or — when none is set — BottlecapDave's current_demand (auto-adopted)."""
    return (main_meta or {}).get("power_sensor") or _bcd_demand_sensor()


@app.route("/api/power")
def api_power():
    """Returns live power (kW) from configured power sensor or derived from reads."""
    try:
        from energy_engine_io import load_json as _lj
        from datetime import datetime
        cfg        = load_config()
        block      = store_access.store().load_current_block()
        meters_cfg = cfg.get("meters", {}) or {}
        meters_blk = block.get("meters", {}) or {}

        # Find power sensor and sub-meter sensors from config
        power_sensor = bat_sensor = ev_sensor = None
        power_source = None  # e.g. "mini" — Octopus Mini opted in as the live source
        postcode_prefix = None
        _main_power_invert = False
        _main_power_unit = None
        soc_sensors = {}  # {meter_id: {soc, inverter_power}}
        from datetime import datetime as _dt2
        _today = _dt2.now().strftime("%Y-%m-%d")
        for m_id, m_data in meters_cfg.items():
            meta = (m_data or {}).get("meta", {}) or {}
            # Skip retired sub-meters — don't show their cards on Live Power
            if meta.get("sub_meter") and meta.get("retired_at") and meta["retired_at"] <= _today:
                continue
            if not meta.get("sub_meter"):
                power_sensor = meta.get("power_sensor")
                power_source = meta.get("power_source")
                postcode_prefix = meta.get("postcode_prefix")
                # invert / unit-override apply to the user's OWN sensor only.
                if power_sensor:
                    _main_power_invert = bool(meta.get("power_invert", False))
                    _main_power_unit = meta.get("power_unit") or None
            elif _is_meter_type(meta, m_id, "battery"):
                bat_sensor = ((m_data.get("channels") or {}).get("import") or {}).get("read")
                if meta.get("soc_sensor") or meta.get("inverter_power_sensor") or meta.get("pv_power_sensor"):
                    soc_sensors[m_id] = {
                        "soc_entity":    meta.get("soc_sensor"),
                        "power_entity":  meta.get("inverter_power_sensor"),
                        "power_invert":  bool(meta.get("inverter_power_invert", False)),
                        "power_unit":    meta.get("inverter_power_unit") or None,
                        "pv_entity":     meta.get("pv_power_sensor"),
                        "label":         meta.get("device") or m_id,
                        "type":          "battery",
                    }
            elif _is_meter_type(meta, m_id, "ev"):
                ev_sensor = ((m_data.get("channels") or {}).get("import") or {}).get("read")
                if meta.get("device_power_sensor"):
                    soc_sensors[m_id] = {
                        "soc_entity":   None,
                        "power_entity": meta.get("device_power_sensor"),
                        "power_invert": bool(meta.get("device_power_invert", False)),
                        "power_unit":   meta.get("device_power_unit") or None,
                        "label":        meta.get("device") or m_id,
                        "type":         "ev",
                        "v2x":          bool(meta.get("v2x_capable")),
                    }
            elif _is_meter_type(meta, m_id, "heat_pump"):
                if meta.get("device_power_sensor"):
                    soc_sensors[m_id] = {
                        "soc_entity":   None,
                        "power_entity": meta.get("device_power_sensor"),
                        "power_invert": bool(meta.get("device_power_invert", False)),
                        "power_unit":   meta.get("device_power_unit") or None,
                        "label":        meta.get("device") or m_id,
                        "type":         "heat_pump",
                    }

        def sensor_kw(entity_id, invert=False, unit_override=None):
            if not entity_id or not _ha_client:
                return None
            val = _ha_client.get_state(entity_id)
            if val in (None, "unknown", "unavailable"):
                return None
            unit = ""
            try:
                unit = ((_ha_client.get_attributes(entity_id) or {})
                        .get("unit_of_measurement") or "")
            except Exception:
                pass
            # Shared converter: unit attr → kW, override when the declared unit is
            # wrong/absent (a CT sensor labelled kW but emitting W-scale numbers),
            # then invert for sensors wired import-negative. Falls back to the
            # magnitude heuristic when the unit is missing.
            import engine as _eng_pc
            return _eng_pc._power_value_to_kw(val, unit, unit_override, invert)

        def derive_kw(reads):
            if not reads or len(reads) < 2:
                return None
            try:
                r2 = reads[-1]
                r1 = None
                for r in reversed(reads[:-1]):
                    if r["ts"] != r2["ts"] and float(r["value"]) != float(r2["value"]):
                        r1 = r
                        break
                if r1 is None:
                    return None
                t1 = datetime.fromisoformat(r1["ts"])
                t2 = datetime.fromisoformat(r2["ts"])
                dt_hours = (t2 - t1).total_seconds() / 3600.0
                if dt_hours <= 0 or dt_hours > 0.5:
                    return None
                delta_kwh = float(r2["value"]) - float(r1["value"])
                return round(delta_kwh / dt_hours, 3)
            except Exception:
                return None

        # Always derive battery/EV from cumulative reads — they don't have power sensors
        bat_kw = ev_kw = None
        for m_id, m_data in meters_blk.items():
            if not m_data:
                continue
            # Use config meta for type detection — block meta may not have meter_type
            cfg_meta = (meters_cfg.get(m_id) or {}).get("meta", {}) or {}
            ch       = m_data.get("channels", {}) or {}
            if not cfg_meta.get("sub_meter", False):
                continue
            if _is_meter_type(cfg_meta, m_id, "battery"):
                bat_kw = derive_kw(ch.get("import", {}).get("reads", []))
            elif _is_meter_type(cfg_meta, m_id, "ev"):
                ev_kw = derive_kw(ch.get("import", {}).get("reads", []))

        effective_ps = power_sensor or _bcd_demand_sensor()
        mini_active = (not effective_ps) and power_source == "mini" and bool(_mini_device_id())
        if effective_ps:
            # Main meter — direct power sensor: the user's own, or BCD's
            # current_demand auto-adopted (kW after sensor_kw's unit handling).
            # invert / unit-override apply to the user's OWN sensor only.
            _own = (effective_ps == power_sensor)
            net_kw = sensor_kw(effective_ps,
                               invert=(_main_power_invert if _own else False),
                               unit_override=(_main_power_unit if _own else None))
            imp_kw = max(0.0, net_kw)  if net_kw is not None else None
            exp_kw = max(0.0, -net_kw) if net_kw is not None else None
        elif mini_active:
            # Main meter — Octopus Mini live demand (kW), polled by the engine.
            net_kw = _mini_live_demand_kw()
            imp_kw = max(0.0, net_kw)  if net_kw is not None else None
            exp_kw = max(0.0, -net_kw) if net_kw is not None else None
        else:
            # Main meter — derive from cumulative reads
            imp_kw = exp_kw = None
            for m_id, m_data in meters_blk.items():
                if not m_data:
                    continue
                meta   = m_data.get("meta", {}) or {}
                ch     = m_data.get("channels", {}) or {}
                if not meta.get("sub_meter", False):
                    imp_kw = derive_kw(ch.get("import", {}).get("reads", []))
                    exp_kw = derive_kw(ch.get("export", {}).get("reads", []))
                    break
            if imp_kw is not None: imp_kw = max(0.0, imp_kw)
            if exp_kw is not None: exp_kw = max(0.0, exp_kw)

        # Get current import rate from HA state cache
        rate_sensor = None
        try:
            from energy_engine_io import load_json as _lj2
            _cfg2 = load_config()
            for _m in _cfg2.get("meters", {}).values():
                if not (_m.get("meta") or {}).get("sub_meter"):
                    rate_sensor = ((_m.get("channels") or {}).get("import") or {}).get("rate")
                    break
        except Exception:
            pass
        current_rate = None
        if rate_sensor and _ha_client:
            try:
                rv = _ha_client.get_state(rate_sensor)
                if rv not in (None, "unknown", "unavailable"):
                    current_rate = round(float(rv), 6)
            except Exception:
                pass

        # Current generation mix — read the SAME source as the 48h chart (mix_history,
        # latest CI-tick slot) so the donut and the chart can never disagree (BL-38).
        # The old path read the latest block-stamped generation_mix, which lagged/diverged
        # from the live mix and could grab two blocks (fragile LIMIT 9).
        current_mix = []
        try:
            _mix_store = store_access.store()
            # #408: mix_history also holds fw48h FORECAST rows, so MAX(captured_at)
            # alone grabbed a slot up to ~48h ahead (a gas-heavy forecast night read
            # ~56% gas vs ~30% now, while the 48h chart showed the real slot). Bound
            # the latest-slot pick to <= now.
            _now_ci = datetime.utcnow().strftime("%Y-%m-%dT%H:%M")  # api_power imports datetime only (no timezone)
            _mix_rows = _mix_store._conn.execute(
                """SELECT fuel, perc FROM mix_history
                   WHERE captured_at = (SELECT MAX(captured_at) FROM mix_history
                                        WHERE captured_at <= ?)
                   ORDER BY perc DESC""",
                (_now_ci,)
            ).fetchall()
            current_mix = [{"fuel": r["fuel"], "perc": r["perc"]} for r in _mix_rows]
            if not current_mix:
                # Fallback (mix_history empty / pre-2.8.0 data): the latest single block
                # that actually has a stamped mix — all its fuel rows, not a LIMIT slice.
                _mix_rows = _mix_store._conn.execute(
                    """SELECT gm.fuel, gm.perc FROM generation_mix gm
                       WHERE gm.block_id = (
                           SELECT b.id FROM blocks b
                           WHERE b.meter_id = 'electricity_main'
                             AND EXISTS (SELECT 1 FROM generation_mix g2
                                         WHERE g2.block_id = b.id)
                           ORDER BY b.block_start DESC LIMIT 1)"""
                ).fetchall()
                current_mix = [{"fuel": r["fuel"], "perc": r["perc"]} for r in _mix_rows]
        except Exception:
            pass

        # Mini availability for the Overview opt-in toggle: a Mini is present AND
        # there's no cheaper live source (no power_sensor entity, no BCD demand).
        mini_present = bool(_mini_device_id())
        bcd_present = False
        try:
            import engine as _eng
            bcd_present = bool(((_eng._detected_integrations or {}).get("bcd") or {}).get("found"))
        except Exception:
            pass
        mini_available = mini_present and not power_sensor and not bcd_present

        return jsonify({
            "import_kw":        imp_kw,
            "export_kw":        exp_kw,
            "battery_kw":       bat_kw,
            "ev_kw":            ev_kw,
            "max_kw":           10,
            "has_power_sensor": bool(effective_ps) or bool(mini_active),
            "mini_active":      bool(mini_active),
            "mini_available":   bool(mini_available),
            "rate":             current_rate,
            "soc_sensors":      _build_soc_response(soc_sensors, _ha_client),
            "generation_mix":   current_mix,
        })
    except Exception as e:
        logger.error("api_power: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/power-source/mini", methods=["POST"])
def api_power_source_mini():
    """Opt the Octopus Mini in/out as the live-power source on the main meter
    (sets/clears meta.power_source == 'mini'). When on, the Overview gauge is
    driven by polling smartMeterTelemetry — which consumes the GraphQL query
    allowance, so this is strictly opt-in."""
    try:
        data = request.get_json(force=True) or {}
        enabled = bool(data.get("enabled"))
        cfg = load_config()
        main_id = None
        for m_id, m_data in (cfg.get("meters") or {}).items():
            if not ((m_data or {}).get("meta") or {}).get("sub_meter"):
                main_id = m_id
                break
        if not main_id:
            return jsonify({"error": "no main meter configured"}), 404
        meta = cfg["meters"][main_id].setdefault("meta", {})
        if enabled:
            if meta.get("power_sensor"):
                # never override a real device sensor
                return jsonify({"error": "a power sensor is already configured"}), 409
            meta["power_source"] = "mini"
        else:
            meta.pop("power_source", None)
        save_config(cfg)
        logger.info("api_power_source_mini: Mini live source %s",
                    "enabled" if enabled else "disabled")
        return jsonify({"ok": True, "enabled": enabled})
    except Exception as e:
        logger.error("api_power_source_mini: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/carbon")
def api_carbon():
    """Fetch 24-hour carbon intensity forecast from National Grid API."""
    import urllib.request
    import urllib.error
    try:
        from energy_engine_io import load_json as _lj
        cfg = load_config()
        postcode = None
        for m_data in cfg.get("meters", {}).values():
            meta = (m_data or {}).get("meta", {}) or {}
            if not meta.get("sub_meter"):
                postcode = meta.get("postcode_prefix", "").strip().upper().split()[0] if meta.get("postcode_prefix", "").strip() else ""
                break
        if not postcode:
            return jsonify({"error": "no_postcode"}), 404

        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        url = f"https://api.carbonintensity.org.uk/regional/intensity/{now_iso}/fw48h/postcode/{postcode}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())

        slots = []
        raw = data.get("data", [])
        # Handle dict shape: {data: {regionid, postcode, data: [{from, to, intensity}]}}
        if isinstance(raw, dict):
            for slot in raw.get("data", []):
                slots.append({
                    "from":      slot.get("from"),
                    "to":        slot.get("to"),
                    "intensity": slot.get("intensity", {}).get("forecast"),
                    "index":     slot.get("intensity", {}).get("index"),
                })
        # Handle list shape
        elif isinstance(raw, list) and raw:
            first = raw[0]
            if "data" in first:
                # [{regionid, postcode, data: [{from, to, intensity}]}]
                for slot in first.get("data", []):
                    slots.append({
                        "from":      slot.get("from"),
                        "to":        slot.get("to"),
                        "intensity": slot.get("intensity", {}).get("forecast"),
                        "index":     slot.get("intensity", {}).get("index"),
                    })
            elif "from" in first:
                # flat [{from, to, intensity}]
                for slot in raw:
                    slots.append({
                        "from":      slot.get("from"),
                        "to":        slot.get("to"),
                        "intensity": slot.get("intensity", {}).get("forecast"),
                        "index":     slot.get("intensity", {}).get("index"),
                    })
        slots = slots[:96]  # cap at 48 hours

        return jsonify({"postcode": postcode, "slots": slots})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        logger.warning("api_carbon: HTTP %s — %s", e.code, body)
        return jsonify({"error": f"http_{e.code}", "detail": body}), 503
    except urllib.error.URLError as e:
        logger.warning("api_carbon: network error: %s", e)
        return jsonify({"error": "network_error"}), 503
    except Exception as e:
        logger.error("api_carbon: type=%s repr=%r str=%s", type(e).__name__, e, e)
        return jsonify({"error": str(e), "type": type(e).__name__, "repr": repr(e)}), 500


# ── Power history + carbon intensity ─────────────────────────────────────────

@app.route("/api/power/history")
def api_power_history():
    """
    Return rolling 48-hour net power and carbon intensity history.
    Used by the Live Power rolling chart.
    Returns: { rows: [{captured_at, net_kw, intensity}] }
    """
    try:
        hours = min(int(request.args.get("hours", 48)), 48)
        store = store_access.store()
        rows  = store.get_power_history(hours=hours)
        return jsonify({"rows": rows})
    except Exception as e:
        logger.error("api_power_history: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/power/mix-history")
def api_power_mix_history():
    """
    Return generation mix for the last 48 hours at CI-tick resolution (~15 min).
    Uses mix_history table (written per CI tick, independent of block size).
    Returns: { slots: [{captured_at, fuels: {wind, solar, gas, ...}}] }
    """
    try:
        store = store_access.store()
        slots = store.get_mix_history(hours=48)
        return jsonify({"slots": slots})
    except Exception as e:
        logger.error("api_power_mix_history: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/engine/meter-reset-detected")
def api_meter_reset_detected():
    """Returns whether a meter read reset was detected (possible replacement/move).
    Clears the flag on read — call once per page load."""
    try:
        import engine as _eng
        detected = _eng.get_and_clear_meter_reset()
        if detected:
            logger.warning("api_meter_reset_detected: meter reset flag was set — serving advisory to UI")
        return jsonify({"detected": detected})
    except Exception as e:
        return jsonify({"detected": False, "error": str(e)})


@app.route("/api/engine/pause-state")
def api_engine_pause_state():
    """Is live capture stopped, by what, and for how long.

    The only thing that can stop the live tick is `pause_engine()` — a restore, a
    device-data delete, a zip import or a VACUUM. A paused tick is skipped rather
    than queued, so a long pause means at least one block boundary was
    interpolated. That used to be visible only as one log line with no reason
    attached; this makes it answerable (B §4.0).
    """
    try:
        import engine as _eng
        return jsonify({"ok": True, **_eng.engine_pause_state(),
                        "loop_lag": _eng.engine_loop_lag()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/meter/<meter_id>/retire", methods=["POST"])
def api_retire_meter(meter_id: str):
    """Retire a sub-meter from a given date. Preserves all historical data."""
    try:
        data = request.get_json(force=True) or {}
        retired_at = data.get("retired_at")
        retired_reason = data.get("retired_reason", "")
        if not retired_at:
            return jsonify({"error": "retired_at is required (YYYY-MM-DD)"}), 400
        # Verify it's a sub-meter
        row = store_access.store()._conn.execute(
            "SELECT is_sub_meter FROM meters WHERE meter_id = ?", (meter_id,)
        ).fetchone()
        if not row:
            return jsonify({"error": f"Meter '{meter_id}' not found"}), 404
        if not row["is_sub_meter"]:
            return jsonify({"error": "Cannot retire the main meter"}), 400
        _engine_write(lambda st: st.retire_meter(meter_id, retired_at, retired_reason))
        try:
            _reset_engine_store()
        except Exception as _re:
            logger.warning("api_retire_meter: engine reset_store failed: %s", _re)
        logger.info("api_retire_meter: %s retired at %s (%s)", meter_id, retired_at, retired_reason)
        return jsonify({"ok": True, "meter_id": meter_id, "retired_at": retired_at})
    except Exception as e:
        logger.error("api_retire_meter: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/meter/<meter_id>/unretire", methods=["POST"])
def api_unretire_meter(meter_id: str):
    """Clear retirement status from a sub-meter.
    Returns 409 Conflict if the sensor is already in use by another active meter."""
    try:
        _engine_write(lambda st: st.unretire_meter(meter_id))
        try:
            _reset_engine_store()
        except Exception as _re:
            logger.warning("api_unretire_meter: engine reset_store failed: %s", _re)
        logger.info("api_unretire_meter: %s unretired", meter_id)
        return jsonify({"ok": True, "meter_id": meter_id})
    except ValueError as e:
        # Sensor conflict — return 409 so the UI can show a meaningful message
        logger.warning("api_unretire_meter: conflict — %s", e)
        return jsonify({"error": str(e), "conflict": True}), 409
    except Exception as e:
        logger.error("api_unretire_meter: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/meter/main/reset", methods=["POST"])
def api_meter_main_reset():
    """
    Nuclear option — backup the DB then wipe it entirely.
    Used when user deletes the main meter (electricity_main).
    Triggers engine restart via engine_startup on next connection.
    """
    try:
        import shutil, time

        # Back up first, and refuse without one (#67). This called
        # `_backup_to_share(store)`, a name this module never defined: the NameError
        # was logged as "backup failed" and the database was deleted anyway. Only the
        # 30-minute share copy, now gone, ever stood behind this.
        _bk, _refuse = _safety_backup_or_refuse("pre-reset")
        if _refuse:
            return _refuse
        logger.info("api_meter_main_reset: backup %s written", _bk)

        # Drop the web module's shared handle. It belongs to whichever thread opened it, so
        # it is closed only if this is that thread (a close from another raises); either
        # way it is not used again. (15 Oct 2026: request threads used it for reads and
        # failed whenever waitress served them on another thread.)
        global _store
        db_path = _os.path.join(DATA_DIR, "blocks.db")
        if _store is not None:
            try:
                _store._conn.close()
            except Exception:
                pass
            _store = None

        # Delete the DB files
        for suffix in ["", "-wal", "-shm"]:
            p = db_path + suffix
            if os.path.exists(p):
                os.remove(p)
                logger.info("api_meter_main_reset: removed %s", p)

        # Also clear meters_config.json
        cfg_p = config_path()
        if os.path.exists(cfg_p):
            import json as _j
            with open(cfg_p, "w") as f:
                _j.dump({"meters": {}}, f, indent=2)


        # Clear stored API credentials — they live outside the DB (so they're
        # not in backups), which means a DB wipe alone leaves them orphaned and
        # the API wrongly re-activates on next start. A reset means start over.
        try:
            import engine as _eng
            _eng.save_kraken_credentials("", None)   # empty key clears the file
            logger.info("api_meter_main_reset: cleared stored API credentials")
        except Exception as _ke:
            logger.warning("api_meter_main_reset: could not clear credentials: %s", _ke)

        logger.info("api_meter_main_reset: database wiped — restarting addon")

        # Trigger addon restart via supervisor API — runs in background thread
        # so the HTTP response is returned before the process exits
        import threading, urllib.request as _ur, json as _j2
        def _restart():
            import time; time.sleep(1)
            try:
                token = os.environ.get("SUPERVISOR_TOKEN", "")
                req = _ur.Request(
                    "http://supervisor/addons/self/restart",
                    data=b"{}",
                    headers={"Authorization": "Bearer " + token,
                             "Content-Type": "application/json"},
                    method="POST"
                )
                _ur.urlopen(req, timeout=10)
            except Exception as re:
                logger.warning("api_meter_main_reset: supervisor restart failed: %s — using sys.exit", re)
                import sys; sys.exit(0)
        threading.Thread(target=_restart, daemon=True).start()

        return jsonify({"ok": True})
    except Exception as e:
        logger.error("api_meter_main_reset: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/meter/<meter_id>/delete-data", methods=["POST"])
@job("delete")
def api_meter_delete_data(meter_id: str):
    """
    Atomically remove a meter from config AND delete all its data, then re-derive
    the parent meter's house/EV split (BL-46). The DB work runs serialized against
    the engine tick (BL-48) so it can't overlap an in-flight block finalise on the
    shared SQLite connection.
    Body: { config: <full config object> }
    Returns: { deleted: {...}, ok: true }

    A coordinator member, "delete" — one member with the range delete and the purge,
    as `delete_in_progress()` already means (B §4). It paused the engine but never
    set the delete flag, so the re-price sweep, carbon backfill and measured drain,
    each `exclusive_with` "delete", could not see it. The claim is taken first,
    before the safety backup (B §5 acceptance 4: claim before any slow work), and
    another delete already running is refused: the flag the three share cannot hold
    two at once.
    """
    import engine as _eng_m
    if _eng_m.delete_in_progress():
        return jsonify({"error": "Another delete is running. Try again once it finishes.",
                        "retry": True}), 409
    _eng_m.set_delete_active(True)
    try:
        return _meter_delete_data(meter_id)
    finally:
        _eng_m.set_delete_active(False)


def _meter_delete_data(meter_id: str):
    """api_meter_delete_data's work, with the "delete" claim held by the caller."""
    try:
        payload  = request.get_json(force=True) or {}
        new_cfg  = payload.get("config")          # read on THIS (web) thread — request is thread-local

        # A device and all its data: back up first, before the engine is paused (#67).
        _bk, _refuse = _safety_backup_or_refuse("pre-delete-device")
        if _refuse:
            return _refuse

        import engine as _eng_d

        # Pause the engine tick. NB: pause only stops the NEXT tick — a tick already
        # running (e.g. a boundary finalise) keeps going, and the API import writes on
        # the engine loop too. Both share this one SQLite connection, so we must also
        # (a) drain a running import and (b) run the delete under the tick lock below,
        # or a delete fired on a block boundary races a finalise (logical corruption +
        # the two-threads-one-connection segfault the restore path already guards).
        try:
            _eng_d.pause_engine("meter data delete")
        except Exception:
            pass

        # (a) Drain any running API import before touching the DB (mirrors the restore
        # path). If it won't stop, abort rather than risk a concurrent writer.
        try:
            if getattr(_eng_d, "api_import_running", None) and _eng_d.api_import_running():
                try: _eng_d.api_import_control("cancel")
                except Exception: pass
                import time as _t_d
                _stopped = False
                for _ in range(120):                      # up to ~60s
                    if not _eng_d.api_import_running():
                        _stopped = True
                        break
                    _t_d.sleep(0.5)
                if not _stopped:
                    try: _eng_d.resume_engine()
                    except Exception: pass
                    return jsonify({"error": "An import was still finishing — delete "
                                    "aborted. Try again in a moment."}), 409
        except Exception as _ie:
            logger.warning("api_meter_delete_data: import-drain check failed: %s", _ie)

        # The whole DB mutation (capture window → delete → EV-aware parent recompute)
        # as one callable, run with exclusive DB access. Uses `new_cfg`/`meter_id`
        # from the closure — never `request` (not valid off the web thread).
        # Runs on the ENGINE thread. The tick is already paused and a running
        # import already cancelled by the time we get here, so this is the
        # exclusivity `run_exclusive` was reaching for — without its executor
        # thread, which A §3.3 forbids.
        #
        # It does NOT need to be a paced job: the deletes are four bulk SQL
        # statements (`DELETE FROM ... WHERE meter_id = ?`), not a Python loop
        # over blocks, and the one loop inside iterates METERS — a handful. The
        # loop stalls for as long as SQLite takes, which is not the same problem
        # a chunked sweep exists to solve.
        def _do_delete_and_recompute(st):
            deleted = {}

            # BL-46: capture the deleted device's parent + block window BEFORE deleting.
            _bl46_parent = _bl46_lo = _bl46_hi = None
            try:
                _pr = st._conn.execute(
                    "SELECT parent_meter_id FROM meters WHERE meter_id = ?", (meter_id,)).fetchone()
                _bl46_parent = ((_pr["parent_meter_id"] if _pr else None) or "electricity_main")
                _rng = st._conn.execute(
                    "SELECT MIN(block_start) lo, MAX(block_start) hi FROM blocks WHERE meter_id = ?",
                    (meter_id,)).fetchone()
                if _rng:
                    _bl46_lo, _bl46_hi = _rng["lo"], _rng["hi"]
            except Exception:
                pass

            with st._conn:
                # 1. Delete all data for this meter
                cur = st._conn.execute("DELETE FROM blocks WHERE meter_id = ?", (meter_id,))
                deleted["blocks"] = cur.rowcount

                # P3.1: block_segments keyed by block_start (no block_id FK) → not
                # cascade-cleaned; remove this meter's segments so none are orphaned.
                cur = st._conn.execute("DELETE FROM block_segments WHERE meter_id = ?", (meter_id,))
                deleted["block_segments"] = cur.rowcount

                cur = st._conn.execute("DELETE FROM sub_meter_history WHERE meter_id = ?", (meter_id,))
                deleted["sub_meter_history"] = cur.rowcount

                cur = st._conn.execute("DELETE FROM current_reads WHERE meter_id = ?", (meter_id,))
                deleted["current_reads"] = cur.rowcount

                # Remove from current_block JSON if present
                try:
                    cb_row = st._conn.execute(
                        "SELECT id, last_checkpoint FROM current_block WHERE id = 1").fetchone()
                    if cb_row and cb_row["last_checkpoint"]:
                        import json as _json
                        cb = _json.loads(cb_row["last_checkpoint"])
                        if meter_id in (cb.get("meters") or {}):
                            del cb["meters"][meter_id]
                            st._conn.execute(
                                "UPDATE current_block SET last_checkpoint = ? WHERE id = 1",
                                (_json.dumps(cb),))
                            deleted["current_block"] = 1
                except Exception:
                    pass

                # 2. Save updated config (meter removed) in the same transaction.
                if new_cfg:
                    period_id = st.get_current_config_period_id()
                    old_meter_ids = [r["id"] for r in st._conn.execute(
                        "SELECT id FROM meters WHERE config_period_id=?", (period_id,)).fetchall()]
                    for old_mid in old_meter_ids:
                        st._conn.execute("DELETE FROM meter_channels WHERE meter_id=?", (old_mid,))
                    st._conn.execute("DELETE FROM meters WHERE config_period_id=?", (period_id,))
                    st._write_meters(new_cfg, period_id)

            logger.info("api_meter_delete_data: deleted %s for meter %s", deleted, meter_id)

            # BL-46: re-derive the parent's house/EV remainder over the deleted window
            # (re-runs PASS 2 over the surviving devices; the house is derived where read).
            try:
                if _bl46_parent and _bl46_lo and _bl46_hi:
                    from datetime import datetime as _dt46, timedelta as _td46
                    try:
                        _hi_excl = (_dt46.fromisoformat(_bl46_hi)
                                    + _td46(minutes=int(_eng_d.get_block_minutes() or 30))).isoformat()
                    except Exception:
                        _hi_excl = _bl46_hi
                    _n46 = _eng_d.recompute_remainders_for_window(_bl46_parent, _bl46_lo, _hi_excl)
                    logger.info("api_meter_delete_data: recomputed %s parent block(s) after removing %s",
                                _n46, meter_id)
            except Exception as _re46:
                logger.warning("api_meter_delete_data: parent recompute failed: %s", _re46)

            return deleted

        # (b) Drain any in-flight tick, then run the work on THIS (web) thread.
        # A no-op run under the tick lock waits for the current tick to finish; the
        # engine is paused, so no NEW tick does DB work (it returns at the pause check
        # before touching the connection). With the in-flight tick drained and new ones
        # inert, the web thread has the SQLite connection to itself. We deliberately do
        # NOT run the recompute on the engine loop — a whole-history device delete can
        # touch tens of thousands of blocks and take minutes; on the loop that would
        # freeze HA comms and blow the request timeout (observed: WS reconnect storm +
        # queue backlog + a spurious empty-message 500 while the job actually finished).
        if _event_loop and _event_loop.is_running():
            try:
                import asyncio as _asyncio
                _asyncio.run_coroutine_threadsafe(
                    _eng_d.drain_tick(), _event_loop).result(timeout=30)
            except Exception as _de:
                logger.warning("api_meter_delete_data: tick-drain barrier failed: %s", _de)
        deleted = _engine_write(_do_delete_and_recompute, timeout=600.0)

        # Data is committed & consistent → clear the pause so the tick runs again.
        # (engine_startup does NOT resume on its own — every pause caller must.)
        try:
            _eng_d.resume_engine()
        except Exception:
            pass

        # Restart engine to pick up the config change.
        try:
            import asyncio as _asyncio
            from engine import engine_startup as _engine_startup_d
            if _event_loop and _event_loop.is_running() and _ha_client:
                _asyncio.run_coroutine_threadsafe(_engine_startup_d(_ha_client), _event_loop)
        except Exception:
            pass

        return jsonify({"ok": True, "deleted": deleted, "meter_id": meter_id})
    except Exception as e:
        logger.error("api_meter_delete_data: %s", e)
        # Best-effort: never leave the engine paused on an error path.
        try:
            import engine as _eng_e
            _eng_e.resume_engine()
        except Exception:
            pass
        return jsonify({"error": str(e)}), 500


@app.route("/api/sub-meter/history")
def api_sub_meter_history():
    """
    Return rolling 48-hour SoC and inverter power history for a battery sub-meter.
    Used by the Live Power page to auto-scale the inverter gauge.
    Returns: { rows: [{captured_at, soc_pct, inverter_kw}], max_kw: float }
    """
    try:
        meter_id = request.args.get("meter_id", "")
        hours    = min(int(request.args.get("hours", 48)), 48)
        if not meter_id:
            return jsonify({"error": "meter_id required"}), 400
        store = store_access.store()
        rows  = store.get_sub_meter_history(meter_id, hours=hours)
        # Compute max abs inverter kW for gauge scaling.
        # Use 90th percentile — spurious sensor spikes (e.g. Solax reporting
        # impossible values) can make up > 5% of readings, causing p95 to land
        # on a spike. p90 is robust against up to 10% bad readings.
        # Also clamp to 20 kW — no residential inverter exceeds this; values
        # above it are always sensor errors and should not inflate the scale.
        kw_vals = [abs(r["inverter_kw"]) for r in rows
                   if r.get("inverter_kw") is not None and abs(r["inverter_kw"]) <= 20.0]
        max_kw = 5.0  # minimum scale
        if kw_vals:
            kw_vals.sort()
            p90_idx = min(int(len(kw_vals) * 0.90), len(kw_vals) - 1)
            max_kw = max(max_kw, kw_vals[p90_idx])
        return jsonify({"rows": rows, "max_kw": round(max_kw, 1)})
    except Exception as e:
        logger.error("api_sub_meter_history: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/carbon/current")
def api_carbon_current():
    """
    Return the current carbon intensity from the stored carbon_intensity table.
    Falls back to the live National Grid API call if no stored data exists.
    Returns: { intensity, ci_index, postcode, source } or { error }
    """
    try:
        store    = store_access.store()
        cfg      = load_config()
        postcode = None
        for m_data in cfg.get("meters", {}).values():
            meta = (m_data or {}).get("meta", {}) or {}
            if not meta.get("sub_meter"):
                postcode = meta.get("postcode_prefix", "").strip().upper().split()[0] if meta.get("postcode_prefix", "").strip() else ""
                break

        if not postcode:
            return jsonify({"error": "no_postcode"}), 404

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        row = store.get_nearest_carbon_intensity(now, postcode)
        if row:
            return jsonify({
                "intensity": row["intensity"],
                "ci_index":  row["ci_index"],
                "postcode":  postcode,
                "source":    "stored",
            })
        return jsonify({"error": "no_data", "postcode": postcode}), 404
    except Exception as e:
        logger.error("api_carbon_current: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/data-management")
def data_management_page():
    return render_template("data_management.html", active="data_management")


@app.route("/historical-probe")
def historical_probe_page():
    """Beta, read-only: recorder long-term-statistics probe (historical-import
    spike). Lets the user pick a sensor per device and characterise what history
    is available. Creates nothing."""
    return render_template("historical_probe.html", active="data_management")


from usage_view import (  # noqa: E402 — moved there (C step 1); the routes and tests use these names
    _aggregate_block_rows, _bucket_to_row_values, _configured_ev_meter_id,
    _dispatch_derived_ev_kwh, _dispatch_ev_split_by_bucket, _hybrid_ev_by_block,
    _apply_ev_split_to_summary_rows, _apply_hybrid_ev_to_summary_rows)


@app.route("/billing-history")
def billing_history_page():
    return render_template("billing_history.html", active="settings")


@app.route("/bill-rounding")
def bill_rounding_page():
    # BL-24: opt-in bill-style rounding for the Billing summary.
    return render_template("bill_rounding.html", active="settings")


@app.route("/delete-blocks")
def delete_blocks_page():
    return render_template("delete_blocks.html", active="data_management")


@app.route("/corrections")
def corrections_page():
    return render_template("corrections.html", active="data_management")


@app.route("/fill-history")
def fill_history_page():
    """Landing hub for the two 'bring data in / fill gaps' tools — Historical
    Import (supplier API / CSV backfill + gap fill) and Device History (recorder
    attribution). Reached from the Data Management topbar."""
    return render_template("fill_history.html", active="data_management")


# ── Lovelace (Home Assistant dashboard) pages ─────────────────────────────────
# A bare page per chart, for an iframe/webpage card. The URLs predate C and are
# kept so existing cards keep working; both now draw in the browser from the API.

_LOVELACE_REFRESH = 130  # seconds between fingerprint-checked refreshes


@app.route("/lovelace/billing")
def lovelace_billing():
    """The Billing tab on its own, for a Home Assistant dashboard card.

    Same URL as ever; it served the pre-rendered daily_usage.html until C deleted
    it (30 Sep 2026) and now serves billing_module.html on a bare page, which
    refreshes itself every _LOVELACE_REFRESH seconds from /api/energy."""
    resp = make_response(render_template("lovelace_billing.html", refresh_s=_LOVELACE_REFRESH))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/lovelace/heatmap")
def lovelace_heatmap():
    """Net energy heatmap — Lovelace-friendly URL, for a Home Assistant dashboard card.

    Same URL as ever, so existing cards keep working; it now serves the in-browser
    heatmap (heatmap_module.html, drawing from /api/energy) instead of the
    pre-rendered net_heatmap.html, which no longer exists. The module refreshes the
    current month on a timer, so the page needs no meta refresh."""
    resp = make_response(render_template("lovelace_heatmap.html",
                                         refresh_s=_LOVELACE_REFRESH))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


def _config_token(store) -> str:
    """What a page shows beyond the blocks that a write can change: the billing periods
    (a billing-day change or an added/removed meter re-cuts them) and the bill-rounding
    option (bill method vs inc-VAT). Part of `_blocks_data_version`, and `/api/energy`'s
    `fingerprint.config` — the per-day fingerprint cannot see either."""
    try:
        _cp = store._conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS mx, "
            "COALESCE(GROUP_CONCAT(billing_day || '@' || effective_from, ','), '') AS bd "
            "FROM config_periods").fetchone()
        _mc = store._conn.execute("SELECT COUNT(*) AS n FROM meters").fetchone()
        import zlib as _zlib
        _cfgf = "%s/%s/%d/%d" % (_cp["n"], _cp["mx"], _zlib.crc32(_cp["bd"].encode()), _mc["n"])
    except Exception:
        _cfgf = "0"
    try:
        _br = "1" if (store.get_settings() or {}).get("bill_rounding_summary") else "0"
    except Exception:
        _br = "0"
    return _cfgf + "|" + _br


def _blocks_data_version(store) -> str:
    """`/api/energy?fields=fingerprint`'s `token` — the Charts page's change poll (it was
    /api/charts/data-version until C step 1, part 4, 1 Oct 2026). Global by design: the
    poll only asks "has anything changed?" and refreshes the open tab; a client caching
    per day compares `fingerprint.days` instead. The same columns as day_fingerprints,
    over all history in one aggregate — 52 ms against 163 ms for every day's fingerprint
    on a 138k-block database, every minute per open tab.

    A cheap token that changes whenever the block data changes — row COUNT +
    newest block_start + a value fingerprint (summed kWh / cost / carbon). Lets the
    Charts and Usage-Stats UIs skip a rebuild on tab-focus/poll when nothing has
    changed (no needless blank) and refresh promptly when it has (e.g. a new live
    half-hour finalised).

    The value fingerprint is what catches an **in-place** edit that leaves the row
    count and newest block untouched — a reprice/settlement, a carbon backfill, or a
    device attribution that rewrites historical blocks (e.g. healing a sub-meter
    zero-hole moves energy off the house remainder onto the device). Without it,
    those edits were invisible to the token and the client kept serving its stale
    cache until the TTL expired. It sums **both import and export** kWh + cost (plus
    carbon) — export settles later than import, so an export-only DCC settlement
    moves exp_kwh/exp_cost but not the import figures, and must still bust the cache.
    Every summed column lives in idx_blocks_insights, so this stays a single
    index-only scan (~20ms over ~130k rows)."""
    row = store._conn.execute(
        "SELECT COUNT(*) AS c, MAX(block_start) AS m, "
        "ROUND(COALESCE(SUM(imp_kwh),  0), 3) AS ik, "
        "ROUND(COALESCE(SUM(imp_cost), 0), 2) AS ic, "
        "ROUND(COALESCE(SUM(exp_kwh),  0), 3) AS ek, "
        "ROUND(COALESCE(SUM(exp_cost), 0), 2) AS ec, "
        "ROUND(COALESCE(SUM(carbon_g), 0), 0) AS cg, "
        # What the Billing tab shows that a cost-neutral write can move (C, 30 Sep
        # 2026): the IOG EV/Home split (carve, settled-split heal), the ex-VAT cost,
        # the device/house division and the standing charge. Until #23 these moved
        # the token through the chart regen every such write triggered. Outside the
        # covering index, so this is a table scan: measured 15 ms against 9 ms.
        # (The EV/Home split is the segments since step 5c; _sf below fingerprints them.)
        "ROUND(COALESCE(SUM(imp_cost_exc), 0), 4) AS xx, "
        "ROUND(COALESCE(SUM(imp_kwh_grid), 0), 4) AS xg, "
        "ROUND(COALESCE(SUM(standing_charge), 0), 4) AS xs "
        "FROM blocks").fetchone()
    # The settlement state and the cached bills (18 Oct 2026, the Pi): a bill that agrees with
    # EMT's estimate moves the cost by under a penny but turns the half-hour settled; nothing
    # here moved, so no chart or heatmap redrew. As day_fingerprints. Guarded, as the segments
    # are: an older schema contributes '0'.
    try:
        _b = store._conn.execute(          # one statement: the poll runs every minute per tab
            "SELECT (SELECT SUM(CASE WHEN rate_source IN ('settled', 'corrected') THEN 1 ELSE 0 END) "
            "        FROM blocks) AS st, "
            "       (SELECT SUM(COALESCE(rate_corrected, 0)) FROM blocks) AS rc, "
            "       (SELECT COUNT(*) FROM measured_cost) AS n, "
            "       (SELECT ROUND(COALESCE(SUM(cost_incl), 0), 4) FROM measured_cost) AS c, "
            "       (SELECT ROUND(COALESCE(SUM(ev_kwh), 0), 4) FROM measured_cost) AS e").fetchone()
        _bf = f"{_b['st']}:{_b['rc']}:{_b['n']}:{_b['c']}:{_b['e']}"
    except Exception:
        _bf = "0"
    # What the Billing tab shows beyond the blocks: the billing periods (a billing-day
    # change or an added/removed meter re-cuts them), the bulk epoch (an import, delete
    # or restore — C §2a), and the bill-rounding option (bill method vs inc-VAT).
    # This was the pre-rendered files' mtimes until C deleted them (30 Sep 2026): every
    # such change scheduled a regen that rewrote the file, and its mtime moved the
    # token. Those changes are now asked about directly.
    _cfgf, _br = _config_token(store).split("|")
    try:
        _epoch = store.get_data_epoch()
    except Exception:
        _epoch = 0
    # BL-32: a cost-neutral RE-PRICE (the first-upgrade migration, or a settlement rate
    # correction) changes segment/displayed RATES but leaves imp_kwh/imp_cost byte-identical
    # (the reconciliation invariant), so the cost fingerprint above does NOT move — and the
    # Charts/Bill UI kept its cached render until a manual refresh. Fingerprint the priced
    # segments' rates too, so any rate-only re-price busts the cache. Guarded: a pre-4.4.0 DB
    # (no block_segments table) contributes '0'. SUM ignores NULL exc (unsettled) rows.
    try:
        _seg = store._conn.execute(
            "SELECT COUNT(*) AS n, "
            "ROUND(COALESCE(SUM(inc_rate), 0), 4) AS ir, "
            "ROUND(COALESCE(SUM(exc_rate), 0), 4) AS er, "
            # attribution and band: a carve or a band relabel moves kWh between
            # house/EV or off-peak/peak segments at unchanged rates.
            "ROUND(COALESCE(SUM(CASE WHEN attribution = 'ev' THEN kwh END), 0), 4) AS ea, "
            # A weight distinct for every label in use (off_peak 359, standard 363,
            # peak 236, day 193, night 265, mixed 264): length alone ties off_peak and
            # standard, and unknown -> off_peak is a real relabel.
            "ROUND(COALESCE(SUM(kwh * (unicode(band) + 31 * length(band))), 0), 3) AS pk "
            "FROM block_segments").fetchone()
        _sf = f"{_seg['n']}:{_seg['ir']}:{_seg['er']}:{_seg['ea']}:{_seg['pk']}"
    except Exception:
        _sf = "0"
    return (f"{row['c']}:{row['m'] or ''}:"
            f"{row['ik']}:{row['ic']}:{row['ek']}:{row['ec']}:{row['cg']}:"
            f"{row['xx']}:{row['xg']}:{row['xs']}:"
            f"{_cfgf}:{_epoch}:{_br}:{_sf}:{_bf}")


def _all_history_utc(store, tz_name):
    """[utc_start, utc_end) covering every LOCAL day that holds a block: local midnight
    of the first block's day to local midnight after the last's. None when there are
    no blocks.

    Two conversions, not one per block. A local date never falls as the UTC instant
    rises, so the first and last blocks' local days bound every other's. It listed
    every local day in history to keep the two ends, about 78% of the Charts
    recent-day check (#46, 2 Oct 2026)."""
    from datetime import datetime as _dt, timezone as _tz
    from zoneinfo import ZoneInfo
    from block_store import local_date_to_utc_bounds as _ldtub
    first, last = store._conn.execute("SELECT MIN(block_start), MAX(block_start) FROM blocks").fetchone()
    if not first:
        return None
    zone = ZoneInfo(tz_name)
    day = lambda iso: _dt.fromisoformat(iso).replace(tzinfo=_tz.utc).astimezone(zone).date().isoformat()
    return _ldtub(day(first), tz_name)[0], _ldtub(day(last), tz_name)[1]


@app.route("/api/entities")
def api_entities():
    """Return all HA entity IDs with unit_of_measurement and device_class for UI filtering."""
    import urllib.request
    token    = os.environ.get("HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN", "")
    ha_url   = os.environ.get("HA_URL", "").rstrip("/")
    base_url = (ha_url + "/api") if ha_url else "http://supervisor/core/api"
    try:
        req = urllib.request.Request(
            base_url + "/states",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            states = json.loads(resp.read().decode())
        entities = sorted([
            {
                "id":           s["entity_id"],
                "unit":         s.get("attributes", {}).get("unit_of_measurement", ""),
                "device_class": s.get("attributes", {}).get("device_class", ""),
            }
            for s in states
        ], key=lambda x: x["id"])
        return jsonify(entities)
    except Exception as e:
        logger.error("api_entities: %s", e)
        # Fall back to state cache
        if _ha_client:
            return jsonify(sorted(_ha_client._state_cache.keys()))
        return jsonify([])


@app.route("/api/config", methods=["GET"])
def api_get_config():
    # Read on THIS thread. This used to hop onto the engine loop — v4's fix for the
    # 26 Aug 2026 incident, when concurrent requests shared one connection and a
    # garbled read surfaced a column as None (a None dict key -> jsonify crashed).
    # Workstream A made that impossible by construction: every request thread has its
    # own read connection, and this module's `load_config()` reads through it
    # (`store_access.store()`). The hop then only cost a wait behind whatever held the
    # loop — second Pi soak (29 Sep 2026): p50 0.02 s quiet, 15.0 s during the sweep.
    return jsonify(load_config())


@app.route("/api/config/history")
def api_config_history():
    """Return config period history with block counts per period."""
    try:
        store = store_access.store()
        cur = store._conn.execute(
            """
            SELECT cp.id, cp.effective_from, cp.effective_to, cp.billing_day,
                   cp.block_minutes, cp.timezone, cp.currency_symbol, cp.currency_code,
                   cp.site_name, cp.supplier, cp.change_reason,
                   (SELECT postcode_prefix FROM meters
                      WHERE config_period_id = cp.id AND is_sub_meter = 0
                      ORDER BY id ASC LIMIT 1) as postcode_prefix,
                   (SELECT postcode_source FROM meters
                      WHERE config_period_id = cp.id AND is_sub_meter = 0
                      ORDER BY id ASC LIMIT 1) as postcode_source,
                   COUNT(DISTINCT b.block_start) as block_count
            FROM config_periods cp
            LEFT JOIN blocks b ON b.config_period_id = cp.id
            GROUP BY cp.id
            ORDER BY cp.effective_from DESC
            """
        )
        rows = []
        for r in cur.fetchall():
            rows.append({
                "id":             r["id"],
                "effective_from": r["effective_from"],
                "effective_to":   r["effective_to"],
                "billing_day":    r["billing_day"],
                "block_minutes":  r["block_minutes"],
                "timezone":       r["timezone"],
                "currency_symbol":r["currency_symbol"],
                "currency_code":  r["currency_code"],
                "site_name":      r["site_name"],
                "supplier":       r["supplier"],
                "change_reason":  r["change_reason"],
                "postcode_prefix":r["postcode_prefix"],
                "postcode_source":r["postcode_source"],
                "block_count":    r["block_count"],
            })
        # Include the configured timezone for client-side date formatting
        cfg_tz = "UTC"
        try:
            from energy_engine_io import load_json as _lj_tz
            _cfg_tz = load_config()
            for _m in _cfg_tz.get("meters", {}).values():
                if not (_m.get("meta") or {}).get("sub_meter"):
                    cfg_tz = (_m.get("meta") or {}).get("timezone", "UTC")
                    break
        except Exception:
            pass
        return jsonify({"periods": rows, "timezone": cfg_tz})
    except Exception as e:
        logger.error("api_config_history: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/region/reconcile")
def api_region_reconcile_get():
    """Pending region-reconciliation plan (the post-import 'name your sites'
    step). Returns {pending: False} when there's nothing to confirm."""
    try:
        store = store_access.store()
        plan = store.get_meta("region_reconcile_pending", None)
        if not plan or not plan.get("needs_confirmation"):
            return jsonify({"pending": False})
        return jsonify({"pending": True, "plan": plan})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/region/reconcile/apply", methods=["POST"])
def api_region_reconcile_apply():
    """Apply a confirmed reconciliation: split at each move boundary and stamp
    every period's region (outward code) + user-supplied site name.
    Body: {"sites": [{outcode, from, to, site_name}]}."""
    try:
        store = store_access.store()
        data = request.get_json(force=True) or {}
        sites = data.get("sites") or []
        if not sites:
            return jsonify({"error": "no sites supplied"}), 400
        _bk, _refuse = _safety_backup_or_refuse("pre-region-reconcile")   # #67
        if _refuse:
            return _refuse
        # One hop for all three: they belong together (apply, clear the pending
        # marker, re-arm carbon) and splitting them would put a half-applied
        # reconciliation between two engine turns. Loops over SITES — a handful
        # of regions, not blocks — so a synchronous hop costs nothing.
        def _apply_regions(st):
            out = st.apply_region_reconciliation(sites)
            st.set_meta("region_reconcile_pending", None)   # clear pending
            st.rearm_carbon_backfill()                      # new regions → re-scan carbon
            return out

        res = _engine_write(_apply_regions)
        try:
            _engine_write(_rebuild_config_period_chain)
        except Exception as _e:
            logger.warning("api_region_reconcile_apply: chain rebuild failed: %s", _e)
        logger.info("api_region_reconcile_apply: %s", res)
        return jsonify({"ok": True, **res})
    except Exception as e:
        logger.error("api_region_reconcile_apply: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/region/reconcile/dismiss", methods=["POST"])
def api_region_reconcile_dismiss():
    """Dismiss the pending reconciliation without applying it."""
    try:
        _engine_write(lambda st: st.set_meta("region_reconcile_pending", None))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/config/history", methods=["POST"])
def api_config_history_create():
    """Create a new config period, inheriting meter definitions from the current active period."""
    try:
        data  = request.get_json(force=True)

        from datetime import datetime as _dt
        ef_from_raw = data.get("effective_from")
        if not ef_from_raw:
            return jsonify({"error": "effective_from is required"}), 400
        ef_from = str(ef_from_raw).replace(" ", "T").split(".")[0]
        try:
            _dt.fromisoformat(ef_from)
        except ValueError:
            return jsonify({"error": "Invalid effective_from date"}), 400

        # #361: read + write on the ENGINE loop/connection (serialised with the
        # engine's writes) instead of the shared web connection on the Flask thread.
        def _mutate(store):
            active_cp = store._conn.execute(
                "SELECT id, billing_day, block_minutes, timezone, "
                "currency_symbol, currency_code, site_name, supplier "
                "FROM config_periods WHERE effective_to IS NULL "
                "ORDER BY effective_from DESC LIMIT 1"
            ).fetchone()
            if not active_cp:
                raise _ConfigError("No active config period found to inherit from")

            billing_day     = data.get("billing_day",     active_cp["billing_day"])
            timezone        = data.get("timezone",        active_cp["timezone"])
            currency_symbol = data.get("currency_symbol", active_cp["currency_symbol"])
            currency_code   = data.get("currency_code",   active_cp["currency_code"])
            site_name       = data.get("site_name",       active_cp["site_name"])
            supplier        = data.get("supplier",        active_cp["supplier"])
            change_reason   = data.get("change_reason") or None

            # Reconstruct config from DB, apply overrides, then insert new period
            full_cfg = store.config_from_db(active_cp["id"])
            for md in full_cfg.get("meters", {}).values():
                meta = md.get("meta") or {}
                if "billing_day"     in data: meta["billing_day"]     = int(billing_day)
                if "timezone"        in data: meta["timezone"]        = timezone
                if "currency_symbol" in data: meta["currency_symbol"] = currency_symbol
                if "currency_code"   in data: meta["currency_code"]   = currency_code
                if "site_name"       in data: meta["site_name"]       = site_name
                if "supplier"        in data: meta["supplier"]        = supplier
                # Postcode is main-meter-only (outward code); a manual entry is 'user'.
                if "postcode" in data and not meta.get("sub_meter"):
                    from block_store import outward_code as _oc
                    meta["postcode_prefix"] = _oc(data.get("postcode"))
                    meta["postcode_source"] = "user"
                md["meta"] = meta

            with store._conn:
                cur = store._conn.execute(
                    """INSERT INTO config_periods
                       (effective_from, effective_to, billing_day, block_minutes, timezone,
                        currency_symbol, currency_code, site_name, supplier, change_reason)
                       VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        ef_from,
                        int(billing_day) if billing_day else 1,
                        int(active_cp["block_minutes"] or 30),
                        timezone or "UTC",
                        currency_symbol or "£",
                        currency_code or "GBP",
                        site_name,
                        supplier,
                        change_reason,
                    )
                )
                new_period_id = cur.lastrowid
                store._write_meters(full_cfg, new_period_id)
            _rebuild_config_period_chain(store)
            return {"ok": True}

        _bk, _refuse = _safety_backup_or_refuse("pre-config-period")      # #67
        if _refuse:
            return _refuse
        result = _config_mutation_on_loop(_mutate)
        logger.info("api_config_history_create: new period from %s", ef_from)
        return jsonify(result)
    except _ConfigError as ce:
        return jsonify({"error": str(ce)}), 400
    except _ConfigBusy:
        return jsonify({"queued": True, "error": _CONFIG_BUSY_MSG}), 503
    except Exception as e:
        logger.error("api_config_history_create: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/config/history/<int:period_id>", methods=["PUT"])
def api_config_history_update(period_id):
    """Update effective_from, effective_to, or change_reason for a config period."""
    try:
        store = store_access.store()
        data  = request.get_json(force=True)

        # Validate period exists
        cp = store.get_config_period(period_id)
        if not cp:
            return jsonify({"error": "Config period not found"}), 404

        allowed = {
            "effective_from", "effective_to", "change_reason",
            "billing_day", "timezone",
            "currency_symbol", "currency_code", "site_name", "supplier",
        }
        updates = {k: v for k, v in data.items() if k in allowed}

        # `postcode` is not a config_periods column — it lives on the period's MAIN
        # meter (outward code only). Handle it separately: a manual edit here is a
        # user value, so mark provenance 'user'. Empty string clears it.
        postcode_edit = "postcode" in data
        postcode_outcode = None
        if postcode_edit:
            from block_store import outward_code as _outcode
            postcode_outcode = _outcode(data.get("postcode"))

        if not updates and not postcode_edit:
            return jsonify({"error": "No valid fields to update"}), 400

        # Type coerce numeric fields
        from datetime import datetime as _dt
        for field in ("billing_day", "block_minutes"):
            if field in updates and updates[field] is not None:
                try:
                    updates[field] = int(updates[field])
                except (ValueError, TypeError):
                    return jsonify({"error": f"Invalid value for {field}"}), 400

        # Normalise and validate date fields — ensure T separator, no microseconds
        for field in ("effective_from", "effective_to"):
            if field in updates and updates[field]:
                val = str(updates[field]).replace(" ", "T").split(".")[0]
                try:
                    _dt.fromisoformat(val)
                    updates[field] = val  # store normalised form
                except ValueError:
                    return jsonify({"error": f"Invalid date format for {field}"}), 400

        # Validate effective_from < effective_to if both present
        from_val = updates.get("effective_from") or cp.get("effective_from")
        to_val   = updates.get("effective_to")   or cp.get("effective_to")
        if from_val and to_val:
            if _dt.fromisoformat(from_val) >= _dt.fromisoformat(to_val):
                return jsonify({"error": "Effective From must be before Effective To"}), 400

        # #361: run the write + chain rebuild on the ENGINE loop/connection (serialised
        # with the engine's writes — no shared-web-connection race), then regen charts
        # OFF the loop so a billing-period change reflects immediately. Same path as
        # the create endpoint via _config_mutation_on_loop.
        def _mutate(estore):
            with estore._conn:
                if updates:
                    set_clause = ", ".join(f"{k} = ?" for k in updates)
                    estore._conn.execute(
                        f"UPDATE config_periods SET {set_clause} WHERE id = ?",
                        list(updates.values()) + [period_id]
                    )
                if postcode_edit:
                    # Outward code only; a manual edit is provenance 'user'.
                    estore._conn.execute(
                        "UPDATE meters SET postcode_prefix = ?, postcode_source = 'user' "
                        "WHERE config_period_id = ? AND is_sub_meter = 0",
                        (postcode_outcode, period_id)
                    )
            if postcode_edit:
                estore.rearm_carbon_backfill()   # a region change may free carbon backfill
            # Rebuild the contiguous chain: sort all periods by effective_from, then
            # set each period's effective_to = next period's effective_from. Robust
            # regardless of how far the date was moved.
            try:
                _rebuild_config_period_chain(estore)
            except Exception as _snap_e:
                logger.warning("api_config_history_update: chain rebuild failed: %s", _snap_e)
            return {"ok": True}

        _bk, _refuse = _safety_backup_or_refuse("pre-config-period")      # #67
        if _refuse:
            return _refuse
        result = _config_mutation_on_loop(_mutate)
        logger.info("api_config_history_update: period %d updated %s%s",
                    period_id, list(updates.keys()),
                    " +postcode" if postcode_edit else "")
        return jsonify(result)
    except _ConfigBusy:
        return jsonify({"queued": True, "error": _CONFIG_BUSY_MSG}), 503
    except Exception as e:
        logger.error("api_config_history_update: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/config/history/<int:period_id>", methods=["DELETE"])
def api_config_history_delete(period_id):
    """Delete a config period, re-assigning its blocks to an adjacent period."""
    try:
        # #361: run the delete + chain rebuild on the ENGINE loop/connection so it's
        # serialised with the engine's own writes — a delete on the shared web store
        # from the Flask thread raced the engine and surfaced as SQLite "another row
        # available" / "database is locked". Charts regen OFF the loop afterwards, the
        # same path as create/update via _config_mutation_on_loop.
        def _mutate(estore):
            # Whether this is the active period must be read on the same connection,
            # before the delete.
            cp = estore.get_config_period(period_id)
            is_active = bool(cp and cp.get("effective_to") is None)
            result = estore.delete_config_period(period_id)
            # Rebuild the contiguous chain so effective_to values stay consistent.
            try:
                _rebuild_config_period_chain(estore)
            except Exception as _e:
                logger.warning("api_config_history_delete: chain rebuild failed: %s", _e)
            # If we deleted the active period, the predecessor is now active — write
            # its config back to meters_config.json (convenience export).
            if is_active:
                try:
                    new_active = estore._conn.execute(
                        "SELECT id FROM config_periods "
                        "WHERE effective_to IS NULL ORDER BY effective_from DESC LIMIT 1"
                    ).fetchone()
                    if new_active:
                        from energy_engine_io import save_json_atomic as _sja
                        restored_cfg = estore.config_from_db(new_active["id"])
                        cfg_path = os.path.join(DATA_DIR, "meters_config.json")
                        _sja(cfg_path, restored_cfg)
                        logger.info(
                            "api_config_history_delete: meters_config.json restored "
                            "from newly-active config period"
                        )
                except Exception as _e:
                    logger.warning(
                        "api_config_history_delete: could not restore meters_config.json: %s", _e
                    )
            return {"ok": True,
                    "blocks_reassigned": result["blocks_reassigned"],
                    "config_restored": is_active}

        _bk, _refuse = _safety_backup_or_refuse("pre-config-period")      # #67
        if _refuse:
            return _refuse
        result = _config_mutation_on_loop(_mutate)
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except _ConfigBusy:
        return jsonify({"queued": True, "error": _CONFIG_BUSY_MSG}), 503
    except Exception as e:
        # repr(): a loop timeout (TimeoutError) str()s to '' — repr keeps the type.
        logger.error("api_config_history_delete: %r", e)
        return jsonify({"error": str(e) or e.__class__.__name__}), 500


@app.route("/api/config", methods=["POST"])
def api_save_config():
    try:
        # Don't let a config save race an in-flight DB restore: it writes to the same
        # SQLite file the restore is swapping out (→ "database is locked"), takes a
        # pointless pre_config_save backup, and schedules an extra engine_startup that
        # piles onto the restore's own — the concurrency storm seen in the logs. Refuse
        # clearly; the user can save again once the restore's progress banner clears.
        if _restore_job.get("status") == "running":
            return jsonify({"error": "A database restore is in progress — wait for it "
                                     "to finish, then save your config again."}), 409

        payload = request.get_json(force=True)
        if not isinstance(payload, dict) or "meters" not in payload:
            return jsonify({"error": "Invalid config structure"}), 400

        # change_reason is an optional UI field, not part of the config schema
        change_reason = payload.pop("change_reason", None) or None
        data = payload

        # Validate device names — unique, max 40 chars, safe characters
        import re as _re
        _NAME_REGEX = _re.compile(r"^[a-zA-Z0-9 '\-&().]+$")
        _NAME_MAX   = 40
        seen_names  = {}
        name_errors = []
        for mid, mdata in (data.get("meters") or {}).items():
            meta     = (mdata or {}).get("meta") or {}
            is_sub   = meta.get("sub_meter")
            raw_name = (meta.get("device") if is_sub else meta.get("site") or "").strip()
            if not raw_name:
                if is_sub:
                    name_errors.append(f'Meter "{mid}": device name is required.')
                continue
            if len(raw_name) > _NAME_MAX:
                name_errors.append(f'"{raw_name}": name must be {_NAME_MAX} characters or fewer.')
            if not _NAME_REGEX.match(raw_name):
                name_errors.append(f'"{raw_name}": name contains invalid characters.')
            lower = raw_name.lower()
            if lower in seen_names:
                name_errors.append(f'"{raw_name}": name is already used by another meter.')
            else:
                seen_names[lower] = mid
        if name_errors:
            return jsonify({"error": " | ".join(name_errors)}), 400

        # Zip current data before committing config change
        _create_backup_zip(label="pre_config_save")

        # Determine if billing-significant meta changed before writing,
        # so we know whether to create a new period or update in place.
        try:
            from block_store import config_meta_significant
            import json as _json
            store = store_access.store()  # reads only; the write goes through _engine_write
            cur_id = store.get_current_config_period_id()
            should_create = True
            if cur_id is not None:
                old_cfg = store.config_from_db(cur_id)
                # If current period has no blocks yet, update in place regardless
                # of how significant the change is — no data to protect
                block_count = store._conn.execute(
                    "SELECT COUNT(*) FROM blocks WHERE config_period_id = ?", (cur_id,)
                ).fetchone()[0]
                if block_count == 0:
                    should_create = False
                elif old_cfg.get("meters"):
                    if not config_meta_significant(old_cfg, data):
                        should_create = False
            if should_create:
                # New billing period — insert_config_period writes to normalised
                # tables, so it runs on the engine thread like every other config
                # write (A §3.3). It loops over the configured METERS, of which
                # there are a handful, so a synchronous hop costs nothing.
                _engine_write(lambda st: st.insert_config_period(
                    data, change_reason=change_reason))
                logger.info("server: config period created (reason=%s)", change_reason or "none")
                # Write file export (not authoritative — convenience only)
                try:
                    p = config_path()
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    tmp = p + ".tmp"
                    with open(tmp, "w") as _f:
                        _json.dump(data, _f, indent=2)
                    os.replace(tmp, p)
                except Exception as _fe:
                    logger.warning("server: could not write meters_config.json export: %s", _fe)
            else:
                # No billing change — save_config updates the active period in-place
                save_config(data)
                logger.info("server: config saved — no billing meta change, period unchanged")
        except Exception as _e:
            logger.warning("server: config save failed: %s", _e)
            # Fallback: always attempt file export so UI isn't left stale
            try:
                p = config_path()
                os.makedirs(os.path.dirname(p), exist_ok=True)
                import json as _j2
                tmp = p + ".tmp"
                with open(tmp, "w") as _f:
                    _j2.dump(data, _f, indent=2)
                os.replace(tmp, p)
            except Exception:
                pass

        logger.info("server: config saved (%d meters)", len(data["meters"]))

        # Re-run engine_startup to pick up new sensor subscriptions
        import asyncio
        from engine import engine_startup
        if _event_loop and _event_loop.is_running() and _ha_client:
            asyncio.run_coroutine_threadsafe(engine_startup(_ha_client), _event_loop)
            logger.info("server: engine_startup scheduled after config save")

        return jsonify({"ok": True})
    except Exception as e:
        logger.error("api_save_config: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/backup/create", methods=["POST"])
def api_backup_create():
    """Create a manual backup zip with an optional label."""
    try:
        data  = request.get_json(force=True) or {}
        label = data.get("label", "manual")
        path  = _create_backup_zip(label=label)
        return jsonify({"ok": True, "path": os.path.basename(path)})
    except Exception as e:
        logger.error("api_backup_create: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/backup", methods=["POST"])
def api_backup():
    """Create a manual backup zip of all data files. Returns the filename and its
    size so the UI can confirm 'Backup ready: <name> (<size>)'."""
    try:
        path = _create_backup_zip(label="manual")
        try:
            size = os.path.getsize(path)
        except Exception:
            size = None
        return jsonify({"ok": True, "path": os.path.basename(path), "size": size})
    except Exception as e:
        logger.error("api_backup: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/backup/info", methods=["GET"])
def api_backup_info():
    """Return backup configuration info."""
    return jsonify({
        "backup_dir": _share_backup_dir(),
        "mode": os.environ.get("EMT_MODE", "supervised")
    })


@app.route("/api/backup/list", methods=["GET"])
def api_backup_list():
    """List available backup zips and last-finalise flat files."""
    import glob
    try:
        zips = sorted(glob.glob(f"{_share_backup_dir()}/backups/*.zip"), reverse=True)
        # Check for flat files from last finalise
        primary = ["blocks.db"]
        legacy  = ["blocks.json"]   # written by 1.x/2.0.x, no longer updated
        flat_files = []
        for fname in primary + legacy:
            fpath = f"{_share_backup_dir()}/{fname}"
            if os.path.exists(fpath):
                mtime = os.path.getmtime(fpath)
                from datetime import datetime as _dt
                entry = {
                    "name": fname,
                    "modified": _dt.utcfromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%S"),
                }
                if fname in legacy:
                    entry["legacy"] = True
                flat_files.append(entry)
        return jsonify({
            "zips": [os.path.basename(z) for z in zips],
            "flat": flat_files
        })
    except Exception as e:
        return jsonify({"zips": [], "flat": []})


_restore_job = {"status": "idle"}
# Held while a restore is checked for and launched, so two requests cannot both see "not
# running" and start one each (10 Oct 2026). The worker clears nothing under it: the job's
# status says whether one is running, and this only makes the check-and-claim one step.
_restore_launch_lock = threading.Lock()


def _fsync_dir(path):
    """fsync the directory holding `path`, so a rename into it is durable."""
    try:
        _dfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        try: os.fsync(_dfd)
        finally: os.close(_dfd)
    except Exception:
        pass


def _move_restore_into_place(src, dest):
    """Rename a database already in DATA_DIR (the import target, or a staged upload) over
    `dest`: same filesystem, so atomic, and no second 80 MB write on the Pi's SD card, which a
    copy was (40 s of the 10 Oct 2026 soak's restore). `src` is synced first, so the rename
    can never expose a file whose pages are still in the OS cache."""
    fd = os.open(src, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(src, dest)
    _fsync_dir(dest)


def _in_data_dir(path):
    return bool(path) and os.path.dirname(os.path.abspath(path)) == os.path.abspath(DATA_DIR)


def _atomic_restore_write(dest, fill):
    """Write `dest` atomically: fill a temp file, fsync it, then os.replace into
    place. An interrupted restore (e.g. the add-on being rebuilt/restarted
    mid-write) then leaves EITHER the old file OR the complete new one — never a
    truncated/zero-length blocks.db that would open as a fresh EMPTY database
    (the failure that silently wiped a user's data after a restore→rebuild).
    `fill(fh)` writes the file body to the open temp handle."""
    tmp = dest + ".restore-tmp"
    try:
        with open(tmp, "wb") as _t:
            fill(_t)
            _t.flush()
            os.fsync(_t.fileno())
        os.replace(tmp, dest)          # atomic on POSIX
        _fsync_dir(dest)
    finally:
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except Exception: pass


@app.route("/api/backup/restore", methods=["POST"])
def api_backup_restore():
    """Start a restore as a BACKGROUND job and return at once — so it completes
    even if the user navigates away. Poll /api/backup/restore/status for progress."""
    try:
        data      = request.get_json(force=True) or {}
        zipname   = data.get("zip", "")
        selected  = data.get("files", None)
        from_flat = data.get("from_flat", False)
        if not from_flat:
            if not zipname or "/" in zipname or "\\" in zipname:
                return jsonify({"error": "Invalid zip name"}), 400
            if not os.path.exists(f"{_share_backup_dir()}/backups/{zipname}"):
                return jsonify({"error": "Backup not found"}), 404
        with _restore_launch_lock:
            if _restore_job.get("status") == "running":
                return jsonify({"error": "a restore is already running",
                                "status": _restore_job}), 409
            _restore_job.clear()
            _restore_job.update({"status": "running", "step": "starting",
                                 "restored": None, "error": None})
        threading.Thread(target=_restore_worker,
                         args=(zipname, selected, from_flat),
                         daemon=True, name="restore").start()
        return jsonify({"ok": True, "status": "running"})
    except Exception as e:
        logger.error("api_backup_restore(launch): %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/backup/restore/status", methods=["GET"])
def api_backup_restore_status():
    """Poll the background restore job's progress/status."""
    return jsonify(_restore_job)


def _resolve_backup_zip(zipname: str):
    """Validate a user-supplied backup zip name and resolve it to an absolute path
    strictly inside the backups dir. Returns (path, None) or (None, (error, code))."""
    zipname = os.path.basename(zipname or "")          # strip any path components
    if not zipname or "/" in zipname or "\\" in zipname or not zipname.endswith(".zip"):
        return None, ("Invalid backup name", 400)
    path = os.path.join(_share_backup_dir(), "backups", zipname)
    if not os.path.exists(path):
        return None, ("Backup not found", 404)
    return path, None


@app.route("/api/backup/download/<zipname>", methods=["GET"])
def api_backup_download(zipname):
    """Download a named backup zip. Path-guarded to the backups dir."""
    path, err = _resolve_backup_zip(zipname)
    if err:
        return jsonify({"error": err[0]}), err[1]
    # octet-stream (not application/zip) so browsers don't treat it as a "safe"
    # archive and auto-extract it on download (e.g. Safari's "open safe files"),
    # which would leave a bare blocks.db instead of the .zip.
    return send_file(path, as_attachment=True,
                     download_name=os.path.basename(path),
                     mimetype="application/octet-stream")


@app.route("/api/backup/delete", methods=["POST"])
def api_backup_delete():
    """Delete a named backup zip (user-initiated from the backup list). Path-guarded
    to the backups dir; only removes the one requested .zip, nothing else."""
    try:
        data = request.get_json(force=True) or {}
        path, err = _resolve_backup_zip(data.get("zip", ""))
        if err:
            return jsonify({"error": err[0]}), err[1]
        os.remove(path)
        logger.info("api_backup_delete: removed backup %s", os.path.basename(path))
        return jsonify({"ok": True, "deleted": os.path.basename(path)})
    except Exception as e:
        logger.error("api_backup_delete: %s", e)
        return jsonify({"error": str(e)}), 500


class _RestoreRefused(Exception):
    """A backup the import gate refuses; its message is the user-facing reason."""


def _import_gate():
    from v4import import gate
    return gate


V5_OLDER_SCHEMA = (
    "This backup was made by an earlier development build of EMT, and its data is stored "
    "differently from this one. Restore a backup from the v4 add-on instead.")
V5_NEWER_SCHEMA = (
    "This backup was made by a newer version of EMT than this one. Update EMT, then "
    "restore it.")


def _restore_phase1(candidate):
    """Import spec §3 phase 1 on `candidate`: everything checkable from the file alone. Returns
    its emt_schema revision (a v5 backup) or None (a v4 one), and raises _RestoreRefused with
    the user-facing reason. A v5 backup takes gate 1 and must be this build's revision; any
    other database takes the v4 gate."""
    import block_store as _bs
    gate = _import_gate()
    rev = _bs.read_emt_schema(candidate)
    if rev is not None:
        why = gate.check_intact_file(candidate)
        if why:
            raise _RestoreRefused(why)
        if rev < _bs.EMT_SCHEMA:
            raise _RestoreRefused(V5_OLDER_SCHEMA)
        if rev > _bs.EMT_SCHEMA:
            raise _RestoreRefused(V5_NEWER_SCHEMA)
        return rev
    why = gate.check(candidate)
    if why:
        raise _RestoreRefused(why)
    return None


def _prepare_restore_db(candidate, step=None, checked=False):
    """The database file to swap in for `candidate`, and the import report (None for a v5
    backup). Raises _RestoreRefused with the user-facing reason; the live database is not
    touched either way (E design §2).

    A v5 backup (it carries emt_schema) is swapped in as it is. Any other database is
    v4-shaped: the importer builds a v5 database from it beside the live one, as
    DATA_DIR/import_target.db. Phase 1 runs first unless `checked` — the staged upload
    /api/restore/stage has already checked (first-run design §2.2)."""
    import block_store as _bs
    if checked:
        rev = _bs.read_emt_schema(candidate)
    else:
        rev = _restore_phase1(candidate)
    if rev is not None:
        logger.info("restore: a v5 backup (emt_schema %d)", rev)
        return candidate, None
    gate = _import_gate()
    logger.info("restore: v4 backup admitted; supplier %s", gate.describe_supplier(candidate))
    import v4import
    target = os.path.join(DATA_DIR, "import_target.db")
    try:
        report = v4import.import_v4(candidate, target, progress=step)
    except v4import.ImportFailed as e:
        raise _RestoreRefused(str(e))
    logger.info("restore: imported from v4 %s in %.1f s (%s) — %s; keys carried %s",
                report["source"].get("written_by"), report["seconds"].get("total", 0.0),
                ", ".join("%s %.1f" % (k, v) for k, v in report["seconds"].items()
                          if k != "total"),
                ", ".join("%s %d" % (t, c["out"]) for t, c in report["tables"].items()
                          if c["out"]),
                sum(len(k["carried"]) for k in report["keys"].values()))
    return target, report


def _zip_has_hot_wal(zf):
    """Import spec §3 gate 1, for a zip: a non-empty blocks.db-wal inside it is
    committed history its blocks.db does not contain — a copy of a running
    database, not a backup. (A backup zip holds blocks.db alone.)"""
    return any(os.path.basename(i.filename) == "blocks.db-wal" and i.file_size > 0
               for i in zf.infolist())


def _extract_restore_candidate(zipname, selected):
    """Extract a backup zip's blocks.db beside the live one, for the import gate to
    read before the swap; the swap then renames it into place. None when the zip has no
    blocks.db, or the restore does not select it."""
    import zipfile, shutil
    if selected is not None and "blocks.db" not in selected:
        return None
    zip_path = f"{_share_backup_dir()}/backups/{zipname}"
    if not os.path.exists(zip_path):
        return None
    with zipfile.ZipFile(zip_path, "r") as zf:
        if _zip_has_hot_wal(zf):
            raise _RestoreRefused(_import_gate().HOT_WAL)
        for name in zf.namelist():
            if os.path.basename(name) == "blocks.db":
                out = os.path.join(DATA_DIR, "restore_candidate.db")
                with zf.open(name) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                    dst.flush()
                    os.fsync(dst.fileno())
                return out
    return None


@job("restore", depends_on=("import",))
def _restore_worker(zipname, selected, from_flat, staged=None, checked=False):
    """Background restore worker (runs in its own thread; survives navigation).
    Updates _restore_job at each phase. Restores selected data files from a named
    backup zip, from last-finalise flat files, or — when `staged` is an absolute
    path to an already-validated uploaded blocks.db (#356) — from that single
    upload. The upload path gets the SAME safety backup, progress, atomic swap and
    gap-detection as the backup-zip restore."""
    global _store
    import zipfile, shutil, sys as _sys_r
    _j = _restore_job
    _atomic_write = _atomic_restore_write
    def _step(s):
        _j["step"] = s
        logger.info("restore: %s", s)
    _zip_candidate = None
    try:
        known = {"blocks.db", "blocks.json"}

        # ── Step 1: pause engine ─────────────────────────────────────────────
        # #56: and REGISTER the restore with the coordinator, so the long-running
        # writers that declare `exclusive_with=("restore",)` stand aside — the re-price
        # sweep kept committing through the whole restore before (it never read the
        # pause), and every commit restarted the safety backup's copy. force=True: the
        # restore pre-empts by hand (below), it does not wait on admission.
        import job_coordinator as _jc_r
        _jc_r.claim("restore", force=True)
        _jc_r.mark("restore", status="running")
        _eng_r = _sys_r.modules.get("engine")
        if _eng_r and hasattr(_eng_r, "pause_engine"):
            _eng_r.pause_engine("backup restore")

        # ── Step 1a: may this database be restored at all? (import spec §3) ──
        # Before anything is stopped or backed up: a refusal leaves the live database,
        # and any running import, exactly as they were. The zip's blocks.db is
        # extracted once, here, and swapped in from the extracted copy below.
        _step("checking the backup")
        if staged:
            _candidate = staged
        elif from_flat:
            _candidate = (f"{_share_backup_dir()}/blocks.db"
                          if "blocks.db" in (selected or known) else None)
        else:
            try:
                _candidate = _extract_restore_candidate(zipname, selected)
            except _RestoreRefused as _rr:
                logger.warning("restore refused: %s", _rr)
                _j["status"] = "error"
                _j["error"] = str(_rr)
                return
            _zip_candidate = _candidate
        _swap_from = None              # the file swapped in: the candidate, or the import
        if _candidate and os.path.exists(_candidate):
            try:
                _swap_from, _ = _prepare_restore_db(_candidate, _step,
                                                    checked=bool(staged) and checked)
            except _RestoreRefused as _rr:
                logger.warning("restore refused: %s", _rr)
                _j["status"] = "error"
                _j["error"] = str(_rr)
                return

        # ── Step 1b: STOP any running API import before touching the DB ───────
        # The background import writes via the SQLite store on the engine loop.
        # Closing that store under it (Step 3) uses a freed connection from
        # another thread → segfault. Cancel it and wait for it to actually stop;
        # if it won't, abort the restore rather than risk crashing the process.
        if getattr(_eng_r, "api_import_running", None) and _eng_r.api_import_running():
            _step("stopping the running import")
            import time as _t_r
            try:
                _eng_r.api_import_control("cancel")
            except Exception:
                pass
            stopped = False
            for _ in range(120):        # up to ~60s
                if not _eng_r.api_import_running():
                    stopped = True
                    break
                _t_r.sleep(0.5)
            if not stopped:
                if hasattr(_eng_r, "resume_engine"):
                    _eng_r.resume_engine()
                if staged and os.path.exists(staged):
                    try: os.remove(staged)
                    except Exception: pass
                _j["status"] = "error"
                _j["error"] = ("An import was still finishing — restore aborted. "
                               "Try again in a moment.")
                return

        # ── Step 2: pre-restore backup (stores still open — backup needs them) ─
        _step("taking a safety backup")
        _create_backup_zip(label="pre_restore")
        # C §2a: the epoch lives in the file about to be replaced; remember it so
        # the restored database's epoch can be set above anything a client saw.
        try:
            _epoch_before = store_access.store().get_data_epoch()
        except Exception:
            _epoch_before = 0

        # ── Step 3: checkpoint WAL then close ALL store connections ───────────
        # Must happen after backup but before writing restored file.
        # If any connection remains open, SQLite WAL will be inconsistent with
        # the newly written DB file, causing "malformed" on next open.
        try:
            # Checkpoint + close the ENGINE store ON THE ENGINE LOOP — never from this
            # restore-worker thread. The engine's connection is opened
            # check_same_thread=False, so a PRAGMA wal_checkpoint / close() issued here
            # while the loop is mid-query on the SAME connection is a concurrent use of
            # one SQLite connection from two threads → Segmentation fault. pause_engine()
            # only sets a cooperative flag; it does NOT stop work already in flight on the
            # loop (a chart regen, discovery, a scheduled task), so the collision is real
            # whenever the loop is busy. Running this as a loop task serialises it against
            # every other loop DB access (the event loop is single-threaded).
            def _teardown_engine_store():
                import engine as _eng_mod2
                if getattr(_eng_mod2, "_store", None) is None:
                    return
                try:
                    _eng_mod2._store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    logger.info("api_backup_restore: WAL checkpoint complete")
                except Exception as _ce:
                    logger.warning("api_backup_restore: WAL checkpoint failed: %s", _ce)
                try:
                    _eng_mod2._store.close()
                except Exception:
                    pass
                _eng_mod2._store = None
            try:
                if _event_loop and _event_loop.is_running():
                    async def _co_teardown():
                        _teardown_engine_store()
                    _run_on_engine_loop(_co_teardown(), timeout=30.0)
                else:
                    _teardown_engine_store()
            except Exception as _te:
                # repr(): a loop timeout (TimeoutError) has an empty str(), which
                # logged a blank reason — repr keeps the type visible.
                logger.warning("api_backup_restore: engine store teardown failed: %r", _te)
            # Close web server store connection (this thread's own store handle)
            if _store:
                try: _store.close()
                except Exception: pass
            _store = None
            # Remove WAL/SHM files so restored DB opens cleanly
            _db_path_pre = os.path.join(DATA_DIR, "blocks.db")
            for _ext in ("-wal", "-shm"):
                _wf = _db_path_pre + _ext
                if os.path.exists(_wf):
                    try: os.remove(_wf)
                    except Exception: pass
            logger.info("api_backup_restore: all store connections closed, WAL files removed")
        except Exception as _pre_e:
            logger.warning("api_backup_restore: pre-restore cleanup failed: %s", _pre_e)

        _step("restoring files")
        restored = []

        if staged:
            # #356: swap in the uploaded (already SQLite-validated) blocks.db — or the
            # database the importer built from it — then drop the staging file.
            # Everything downstream (reset, migrate, gap-detect) is shared. Both live in
            # DATA_DIR, so the swap is a rename, not a copy (10 Oct 2026).
            dest = os.path.join(DATA_DIR, "blocks.db")
            _move_restore_into_place(_swap_from or staged, dest)
            restored.append("blocks.db")
            try: os.remove(staged)
            except Exception: pass
            logger.info("api_backup_restore: restored blocks.db from upload")
        elif from_flat:
            for fname in (selected or list(known)):
                if fname not in known:
                    continue
                src_path = f"{_share_backup_dir()}/{fname}"
                if fname == "blocks.db" and _swap_from:
                    src_path = _swap_from
                dst_path = os.path.join(DATA_DIR, fname)
                if os.path.exists(src_path):
                    if _in_data_dir(src_path):     # the import target: rename it in
                        _move_restore_into_place(src_path, dst_path)
                    else:                          # the share's own copy: never moved
                        with open(src_path, "rb") as _src:
                            _atomic_write(dst_path,
                                          lambda d, _s=_src: shutil.copyfileobj(_s, d))
                    restored.append(fname)
            logger.info("api_backup_restore: restored flat files %s", restored)
        else:
            zip_path = f"{_share_backup_dir()}/backups/{zipname}"
            if not os.path.exists(zip_path):
                _j["status"] = "error"; _j["error"] = "Backup not found"
                return
            with zipfile.ZipFile(zip_path, "r") as zf:
                for name in zf.namelist():
                    basename = os.path.basename(name)
                    if basename not in known:
                        continue
                    if selected is not None and basename not in selected:
                        continue
                    dest = os.path.join(DATA_DIR, basename)
                    if basename == "blocks.db" and _swap_from:
                        # the copy Step 1a extracted and checked, or the database the
                        # importer built from it: rename it into place (same directory,
                        # so atomic) rather than write the database a second time
                        os.replace(_swap_from, dest)
                        _fsync_dir(dest)
                    else:
                        with zf.open(name) as zf_src:
                            _atomic_write(dest,
                                          lambda d, _s=zf_src: shutil.copyfileobj(_s, d))
                    restored.append(basename)
            logger.info("api_backup_restore: restored %s from %s", restored, zipname)

        # A legacy pre-SQLite blocks.json restored from an old backup is no
        # longer auto-migrated (JSON->SQLite migration was removed in 4.0.0).
        # Warn loudly and leave the file untouched rather than silently importing.
        legacy_json = os.path.join(DATA_DIR, "blocks.json")
        if "blocks.json" in restored and os.path.exists(legacy_json):
            logger.error(
                "api_backup_restore: restored a legacy blocks.json, but automatic "
                "JSON->SQLite migration was REMOVED in 4.0.0. It is left untouched "
                "and NOT imported. To recover it, migrate with an earlier release "
                "(3.x or older) before upgrading."
            )

        # ── Reset stores and fix WAL before any further DB access ────────────
        _step("reinitialising")
        if "blocks.db" in restored or "blocks.json" in restored:
            # Close web server store
            if _store:
                try: _store.close()
                except Exception: pass
            _store = None
            # Remove WAL/SHM files left by the running engine — these cause
            # "malformed" errors when BlockStore reopens with PRAGMA journal_mode=WAL
            _db_path = os.path.join(DATA_DIR, "blocks.db")
            for _ext in ("-wal", "-shm"):
                _wf = _db_path + _ext
                if os.path.exists(_wf):
                    try: os.remove(_wf)
                    except Exception: pass
            # Switch to DELETE journal mode via raw sqlite3 so BlockStore can
            # open cleanly without triggering the corruption recovery handler
            try:
                import sqlite3 as _sq3r
                _rc = _sq3r.connect(_db_path)
                _rc.execute("PRAGMA journal_mode=DELETE")
                _rc.close()
            except Exception as _je:
                logger.warning("api_backup_restore: journal_mode switch failed: %s", _je)
            # Reset engine store so it reopens from the new file — ON THE ENGINE LOOP,
            # so the fresh connection is created and owned by the loop thread and never
            # races a concurrent loop query (same check_same_thread=False hazard as the
            # checkpoint/close above).
            import sys as _sys_r
            _eng_r = _sys_r.modules.get("engine")
            if _eng_r and hasattr(_eng_r, "reset_store"):
                try:
                    if _event_loop and _event_loop.is_running():
                        async def _co_reset():
                            _eng_r.reset_store()
                        _run_on_engine_loop(_co_reset(), timeout=30.0)
                    else:
                        _eng_r.reset_store()
                    logger.info("api_backup_restore: engine store reset OK")
                except Exception as _re:
                    logger.warning("api_backup_restore: engine reset_store failed: %s", _re)

        # Run schema migration (2.0.x legacy full_config_json → normalised tables)
        if "blocks.db" in restored or "blocks.json" in restored:
            store_mig = None
            try:
                store_mig = _own_store()      # own thread, own connection
                n = store_mig.migrate_full_config_json()
                if n:
                    logger.info(
                        "api_backup_restore: ran migrate_full_config_json — "
                        "%d periods migrated from full_config_json", n
                    )
                # C §2a: a restore replaces all history — bump the epoch, above the
                # pre-restore value so a client holding it cannot mistake the
                # restored database for the one it cached.
                _ep = store_mig.bump_data_epoch(floor=_epoch_before)
                logger.info("api_backup_restore: data epoch -> %d", _ep)
            except Exception as _mig_e:
                logger.warning("api_backup_restore: schema migration failed: %s", _mig_e)
            finally:
                # Own connection on the restore worker's thread — close it, or a
                # restore leaks one and holds a handle on a file the restore may
                # be about to replace.
                _release_store(store_mig)

        # Re-run engine_startup so gap detection runs against the restored DB.
        # resume_engine() alone just unpauses the tick loop — it doesn't detect
        # the session gap between the backup timestamp and now. engine_startup()
        # reads the last finalised block, detects the gap, sets a gap marker and
        # fills missing blocks as soon as the first live sensor read arrives.
        import asyncio as _asyncio_r
        from engine import engine_startup as _engine_startup_r
        if _event_loop and _event_loop.is_running() and _ha_client:
            _asyncio_r.run_coroutine_threadsafe(_engine_startup_r(_ha_client), _event_loop)
            logger.info("api_backup_restore: engine_startup scheduled for gap detection")

        _j["restored"] = restored
        _j["status"] = "done"
        _step("done")
    except Exception as e:
        logger.error("api_backup_restore: %s", e)
        _j["status"] = "error"
        _j["error"] = str(e)
        if staged and os.path.exists(staged):
            try: os.remove(staged)
            except Exception: pass
    finally:
        if staged and _j.get("status") == "error" and os.path.exists(staged):
            try: os.remove(staged)          # a refused upload is not kept
            except Exception: pass
        for _left in (_zip_candidate, os.path.join(DATA_DIR, "import_target.db")):
            if _left and os.path.exists(_left):
                try: os.remove(_left)       # extracted, or imported and not swapped in
                except Exception: pass
        # Always resume engine — reset_store already called above on success,
        # but we must resume even if an exception occurred mid-restore
        import sys as _sys_fin
        _eng_fin = _sys_fin.modules.get("engine")
        if _eng_fin and hasattr(_eng_fin, "resume_engine"):
            try: _eng_fin.resume_engine()
            except Exception: pass
        try:
            import job_coordinator as _jc_fin
            _jc_fin.mark("restore", status=_j.get("status") or "done")
        except Exception:
            pass


@app.teardown_request
def _close_retired_stores(_exc=None):
    """#57: a request is over, so a connection a file swap retired on this thread can no
    longer be in use — close it (store_access.store() no longer closes it mid-request)."""
    try:
        store_access.close_retired()
    except Exception:
        pass


@app.route("/insights")
def insights_page():
    return render_template("insights.html", active="insights")


@app.route("/settings")
def settings_page():
    tab = request.args.get("tab", "meter-config")
    if tab == "carbon":
        return render_template("settings.html", active="settings")
    # Default: Meter Config tab
    store = store_access.store()
    cfg   = load_config()
    tz_select_html = '<select class="js-meta" data-key="timezone"><option value="UTC">UTC</option><option value="Europe/London">Europe/London (UK)</option><option value="Europe/Dublin">Europe/Dublin (Ireland)</option><option value="Europe/Lisbon">Europe/Lisbon (Portugal)</option><option value="Europe/Paris">Europe/Paris (France, Belgium, Netherlands)</option><option value="Europe/Berlin">Europe/Berlin (Germany, Austria)</option><option value="Europe/Amsterdam">Europe/Amsterdam</option><option value="Europe/Rome">Europe/Rome (Italy)</option><option value="Europe/Madrid">Europe/Madrid (Spain)</option><option value="Europe/Stockholm">Europe/Stockholm (Sweden, Norway, Denmark)</option><option value="Europe/Helsinki">Europe/Helsinki (Finland)</option><option value="Europe/Warsaw">Europe/Warsaw (Poland)</option><option value="Europe/Athens">Europe/Athens (Greece)</option><option value="Europe/Istanbul">Europe/Istanbul (Turkey)</option><option value="Europe/Moscow">Europe/Moscow (Russia)</option><option value="America/New_York">America/New_York (US Eastern)</option><option value="America/Chicago">America/Chicago (US Central)</option><option value="America/Denver">America/Denver (US Mountain)</option><option value="America/Los_Angeles">America/Los_Angeles (US Pacific)</option><option value="America/Toronto">America/Toronto (Canada Eastern)</option><option value="America/Vancouver">America/Vancouver (Canada Pacific)</option><option value="America/Sao_Paulo">America/Sao_Paulo (Brazil)</option><option value="Asia/Dubai">Asia/Dubai (UAE)</option><option value="Asia/Kolkata">Asia/Kolkata (India)</option><option value="Asia/Singapore">Asia/Singapore</option><option value="Asia/Tokyo">Asia/Tokyo (Japan)</option><option value="Asia/Shanghai">Asia/Shanghai (China)</option><option value="Australia/Sydney">Australia/Sydney</option><option value="Australia/Perth">Australia/Perth</option><option value="Pacific/Auckland">Pacific/Auckland (New Zealand)</option></select>'
    has_data = bool(store.count_blocks())
    try:
        import engine as _eng
        _ds_mode = _eng.get_data_source_mode()
    except Exception:
        _ds_mode = "cad"
    try:
        import engine as _eng2
        _has_creds = _eng2.has_kraken_credentials()
    except Exception:
        _has_creds = False
    return render_template(
        "meter_config.html",
        config=cfg,
        active="settings",
        tz_select_html=tz_select_html,
        has_data=has_data,
        data_source_mode=_ds_mode,
        has_credentials=_has_creds,
        ohme_detection=_ohme_detection()
    )


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    """Return current settings merged with defaults."""
    store = store_access.store()
    saved = store.get_settings()
    # Merge saved over defaults — any missing key falls back to default
    result = dict(SETTINGS_DEFAULTS)
    result.update(saved)
    return jsonify(result)


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    """Save settings. Only known keys accepted."""
    data = request.get_json(force=True) or {}
    saved = store_access.store().get_settings()
    for key in SETTINGS_DEFAULTS:
        if key in data:
            val = data[key]
            # Numeric fields
            if key in SETTINGS_NUMERIC:
                try:
                    val = float(val)
                    if val <= 0:
                        return jsonify({"error": f"{key} must be positive"}), 400
                except (TypeError, ValueError):
                    return jsonify({"error": f"{key} must be a number"}), 400
            saved[key] = val
    _engine_write(lambda st: st.save_settings(saved))
    return jsonify({"ok": True})


@app.route("/api/detect-bcd", methods=["GET"])
def api_detect_bcd():
    """Scan HA states for a BottlecapDave Octopus integration and report what
    EMT can pre-fill (account, MPANs, rate/standing-charge sensors, Mini
    presence). BCD is OPTIONAL — when absent this cleanly returns {found:false}
    and the wizard proceeds on the pure-API path. Never fails hard."""
    try:
        import engine as _eng
        from kraken_api_client import detect_bottlecapdave
        if _ha_client is None:
            return jsonify({"found": False, "detail": "no_ha_client"})
        states = _run_on_engine_loop(_ha_client.get_all_states(), timeout=20.0)
        result = detect_bottlecapdave(states or [])
        return jsonify(result)
    except Exception as e:
        # Detection failure must never block setup — fall back to pure-API path.
        return jsonify({"found": False, "detail": str(e)})


@app.route("/api/data-source-mode", methods=["GET"])
def api_data_source_mode_get():
    """Return the current data-source mode and derived capability flags."""
    try:
        import engine as _eng
        mode = _eng.get_data_source_mode()
        uses_api = _eng.mode_uses_api(mode)
        has_creds = _eng.has_kraken_credentials()
        return jsonify({"mode": mode,
                        "uses_api": uses_api,
                        "uses_mini": _eng.mode_uses_mini(mode),
                        "has_credentials": has_creds,
                        # API mode restored/selected but no stored key — e.g. after
                        # restoring a backup onto a fresh /data (creds live in a
                        # file that backups don't include).
                        "credentials_missing": bool(uses_api and not has_creds)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/data-source-mode", methods=["POST"])
def api_data_source_mode_post():
    """Set the data-source mode (derived by the setup survey).

    Enforces supplier gating: an API-backed mode (cad+api / api / api+mini) is
    rejected unless the supplier is API-capable. The supplier is taken from the
    request if present, else from the saved config's main meter. This is the
    authoritative gate — the wizard UI hides the API path for non-API suppliers,
    but we never trust the UI alone.
    """
    try:
        import engine as _eng
        data = request.get_json(force=True) or {}
        requested_mode = data.get("mode")
        if _eng.mode_uses_api(requested_mode):
            supplier = data.get("supplier")
            if supplier is None:
                # Fall back to the saved config's supplier.
                try:
                    _cfg = load_config()
                    for _m in (_cfg.get("meters") or {}).values():
                        if (_m.get("meta") or {}).get("sub_meter"):
                            continue
                        supplier = (_m.get("meta") or {}).get("supplier")
                        break
                except Exception:
                    supplier = None
            if not _eng.supplier_is_api_capable(supplier):
                return jsonify({
                    "error": "supplier_not_api_capable",
                    "detail": ("mode %r requires an API-capable supplier; "
                               "supplier %r is local-only"
                               % (requested_mode, supplier or ""))
                }), 400
            # Guard: an API mode needs credentials present (Change Setup cad→api
            # must not strand the user on an API mode with nothing to poll).
            if not _eng.has_kraken_credentials():
                return jsonify({
                    "error": "no_credentials",
                    "detail": ("mode %r requires Kraken API credentials, but none "
                               "are configured" % (requested_mode,))
                }), 400
        mode = _engine_call(_eng.set_data_source_mode, requested_mode)
        return jsonify({"ok": True, "mode": mode})
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/kraken-config", methods=["GET"])
def api_kraken_config_get():
    """Report whether API credentials are configured and the current connection
    status. NEVER returns the API key itself."""
    try:
        import engine as _eng
        env = _eng._kraken_env()
        configured = bool(env.get("api_key"))
        out = {
            "configured": configured,
            "account_number": env.get("account_number"),
            "connected": _eng.kraken_available(),
            "mini": _eng._kraken_mini_reader is not None,
            # #357: the account this DB was previously connected to (stamped on first
            # discovery). Lets the UI pre-fill the account on a lightweight reconnect
            # after a restore, so re-adding a key doesn't need the full setup wizard.
            "db_account": _eng.get_db_account(),
        }
        # #357: if this DB is stamped to a different account than the credentials,
        # the API was NOT auto-activated — surface it so the UI can prompt an
        # explicit reconnect (credentials are kept, not dropped).
        mm = _eng.kraken_account_mismatch()
        if mm:
            out["account_mismatch"] = True
            out["db_account"] = mm[0]
            out["credentials_account"] = mm[1]
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/kraken-config", methods=["POST"])
def api_kraken_config_post():
    """Save API credentials to the credentials file (outside the DB/backups),
    then run discovery immediately and return the live result. Sending an empty
    api_key disconnects (clears the file)."""
    try:
        import engine as _eng
        data = request.get_json(force=True) or {}
        api_key = data.get("api_key")
        account = data.get("account_number")
        base_url = data.get("base_url")
        _eng.save_kraken_credentials(api_key, account, base_url)
        if not (api_key or "").strip():
            # Full disconnect, not just a file delete: stop polling, tear down the
            # live client + Mini, wipe API-derived state, land on cad. (Deleting
            # the file alone left the in-memory client polling until restart.)
            result = _run_on_engine_loop(_eng.disconnect_kraken(), timeout=30.0)
            return jsonify({"ok": result.get("ok", True), "connected": False,
                            "disconnected": True, "mode": result.get("mode")})
        result = _run_on_engine_loop(_eng.connect_kraken_now(), timeout=45.0)
        if not result.get("ok"):
            # The key was already persisted (save happens above, before the live
            # check). A failed check is usually TRANSIENT — rate limit, a network
            # blip, or a DB mid-swap — not a bad key. Make the save visible so a
            # failed verify never looks like the credentials were dropped, and
            # never auto-delete them here: only Disconnect removes credentials.
            result["saved"] = True
            result["message"] = (
                "Credentials saved, but the connection could not be verified right "
                "now (%s). They have been kept — EMT will connect automatically when "
                "the API is reachable. If the key is wrong, use Disconnect to remove "
                "it." % (result.get("detail") or "unknown"))
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/disconnect-kraken", methods=["POST"])
def api_disconnect_kraken():
    """Destructive 'Disconnect Octopus' action (MODE-UI §5): clear API
    credentials + API-derived state, stop polling immediately, tear down the live
    client/Mini, and land on local-sensor mode ('cad'). Historical DCC-settled
    billing data and the billing source are KEPT. The UI confirms before calling
    this. See engine.disconnect_kraken."""
    try:
        import engine as _eng
        result = _run_on_engine_loop(_eng.disconnect_kraken(), timeout=30.0)
        return jsonify(result), (200 if result.get("ok") else 500)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/reactivate-kraken", methods=["POST"])
def api_reactivate_kraken():
    """Reactivate the supplier API using the ALREADY-STORED credentials (#381).

    The limbo this escapes: a prior Disconnect or a DB-swap can leave the
    data-source mode on 'cad'/'unset' while a valid API key still lives in its
    own file. Startup's mode-gate then skips auto-activation, so the app looks
    like the credentials are missing even though they are present. This runs a
    live connect from the stored key WITHOUT the user re-typing it; on success
    engine.connect_kraken_now() promotes the mode back to an API mode, so the
    fix persists across restarts. It never writes or changes the key, so it can
    only ever move the user from local-only back to their own configured API.
    A transient failure keeps everything as-is (the key is untouched)."""
    try:
        import engine as _eng
        if not _eng.has_kraken_credentials():
            return jsonify({"ok": False, "connected": False,
                            "detail": "no_credentials"}), 400
        result = _run_on_engine_loop(_eng.connect_kraken_now(), timeout=45.0)
        # Report the (possibly promoted) mode so the UI can re-render in place.
        try:
            result["mode"] = _eng.get_data_source_mode()
        except Exception:
            pass
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _parse_version(v: str):
    """'3.2.0' -> (3, 2, 0). Non-numeric parts sort as 0. Tolerates a 'v' prefix."""
    v = (v or "").strip().lstrip("vV")
    parts = []
    for chunk in v.split(".")[:3]:
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


_update_check_cache = {"checked_at": 0.0, "payload": None}
_UPDATE_CHECK_TTL = 86400.0   # once a day — the release cadence is far slower


@app.route("/api/update-check", methods=["GET"])
def api_update_check():
    """BL-6: is a newer release available?

    Supervised users get update badges from the Supervisor; unsupervised (Docker
    / HA Container) users get nothing and only learn of a release by checking the
    repo manually. Cached for a day — a release lookup is not worth a request per
    page load, and GitHub rate-limits unauthenticated calls.

    Never fatal: any failure returns update_available=false, so the UI is silent
    rather than showing an error the user can do nothing about.
    """
    import time
    now = time.time()
    if (_update_check_cache["payload"] is not None
            and now - _update_check_cache["checked_at"] < _UPDATE_CHECK_TTL):
        return jsonify(_update_check_cache["payload"])

    payload = {"current": APP_VERSION, "latest": None, "url": None,
               "update_available": False}
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.github.com/repos/RGx01/energy-meter-tracker-addon/releases/latest",
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "energy-meter-tracker"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        latest = (data.get("tag_name") or "").strip()
        if latest:
            payload["latest"] = latest.lstrip("vV")
            payload["url"] = data.get("html_url")
            # If we cannot determine our OWN version, never claim an update —
            # (0,0,0) < anything would prompt on every release, wrongly.
            payload["update_available"] = bool(APP_VERSION) and (
                _parse_version(latest) > _parse_version(APP_VERSION))
    except Exception as e:
        logger.info("api_update_check: lookup failed (non-fatal): %s", e)

    _update_check_cache.update({"checked_at": now, "payload": payload})
    return jsonify(payload)


@app.route("/api/instance/notice", methods=["GET"])
def api_instance_notice():
    """BL-5: is the open database restored from another instance?

    A native DB carries db_uuid == install_id; a restored one carries the source
    install's id. Computed once at startup (engine.FOREIGN_RESTORE_NOTICE). The
    UI shows a dismissible notice when foreign and not yet acknowledged. Never
    fatal — any error reports "not foreign" so the UI stays silent.
    """
    try:
        import engine as _eng
        notice = getattr(_eng, "FOREIGN_RESTORE_NOTICE", None) or {}
        return jsonify({"foreign": bool(notice.get("foreign")),
                        "db_uuid": notice.get("db_uuid"),
                        "acknowledged": bool(notice.get("acknowledged"))})
    except Exception as e:
        logger.info("api_instance_notice: %s", e)
        return jsonify({"foreign": False, "db_uuid": None, "acknowledged": False})


@app.route("/api/instance/notice/dismiss", methods=["POST"])
def api_instance_notice_dismiss():
    """BL-5: dismiss the foreign-restore notice for a data lineage. Persisted per
    db_uuid in /data (install-scoped), so it survives the next restore — a routine
    repeated restore of the same source is acknowledged once and stays quiet."""
    try:
        import engine as _eng
        import instance as _inst
        notice = getattr(_eng, "FOREIGN_RESTORE_NOTICE", None) or {}
        body = request.get_json(silent=True) or {}
        db_uuid = body.get("db_uuid") or notice.get("db_uuid")
        if not db_uuid:
            return jsonify({"ok": False, "error": "no db_uuid"}), 400
        _inst.acknowledge_db_uuid(db_uuid)
        if isinstance(getattr(_eng, "FOREIGN_RESTORE_NOTICE", None), dict) \
                and _eng.FOREIGN_RESTORE_NOTICE.get("db_uuid") == db_uuid:
            _eng.FOREIGN_RESTORE_NOTICE["acknowledged"] = True
        return jsonify({"ok": True})
    except Exception as e:
        logger.warning("api_instance_notice_dismiss: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


def _settlement_horizon_floor() -> str:
    """UTC-ISO cutoff = now − the DCC settlement horizon. Blocks older than this
    can't still be 'awaiting DCC settlement' (settlement lands within days), so
    the unsettled badge/list floors here — keeping historic/imported reconstructed
    blocks (which never settle) out of the count regardless of whether the
    settlement sweep has run to finalise them yet."""
    from datetime import datetime, timezone, timedelta
    try:
        import engine as _eng
        days = int(getattr(_eng, "_SETTLEMENT_SWEEP_HORIZON_DAYS", 14))
    except Exception:
        days = 14
    return (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).isoformat()


@app.route("/api/settlement/refetch/preview", methods=["POST"])
def api_refetch_preview():
    """BL-86: start a re-fetch of a range from Octopus — into memory, compared with what is
    stored (settlement surfaces design §5.2). Body {from, to, parts?}: naive-UTC ISO, at most 31
    days; parts ⊆ ["kwh", "bill"], both by default. Writes nothing. Poll /status for the
    differences and a token; /apply writes them."""
    try:
        import engine as _eng
        import asyncio as _asyncio
        body = request.get_json(force=True, silent=True) or {}
        utc_from, utc_to = (body.get("from") or "").strip(), (body.get("to") or "").strip()
        why = _eng._refetch_check_range(utc_from, utc_to)
        parts = tuple(body.get("parts") or _eng._REFETCH_PARTS)
        if not why and (not parts or set(parts) - set(_eng._REFETCH_PARTS)):
            why = "parts must be some of %s" % list(_eng._REFETCH_PARTS)
        if why:
            return jsonify({"ok": False, "error": why}), 400
        if not _eng.kraken_available():
            return jsonify({"ok": False, "error": "no supplier API connected"}), 400
        if _restore_job.get("status") == "running":
            return jsonify({"ok": False, "error": "a restore is running"}), 409
        if not (_event_loop and _event_loop.is_running()):
            return jsonify({"ok": False, "error": "engine loop not running"}), 500
        ok, reason = _eng.refetch_claim()
        if not ok:
            return jsonify({"ok": False, "error": reason}), 409
        try:
            _asyncio.run_coroutine_threadsafe(
                _eng.run_refetch_preview(utc_from, utc_to, parts), _event_loop)
        except Exception:
            import job_coordinator as _jc
            _jc.release("range_refetch")
            raise
        return jsonify({"ok": True, "status": "running"})
    except Exception as e:
        logger.error("api_refetch_preview: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/settlement/refetch/status", methods=["GET"])
def api_refetch_status():
    """The re-fetch job's state: its phase, and when a preview is done, its summary, the
    half-hours that differ (stored → Octopus) and the token to apply them with."""
    import engine as _eng
    return jsonify(_eng.refetch_status())


@app.route("/api/settlement/refetch/apply", methods=["POST"])
def api_refetch_apply():
    """BL-86: write a preview's differences. Body {token}. Refused if the preview is gone, over
    an hour old, or the range has been written since; a safety backup first, and none means no
    apply (as Delete Blocks)."""
    try:
        import engine as _eng
        import asyncio as _asyncio
        body = request.get_json(force=True, silent=True) or {}
        token = (body.get("token") or "").strip()
        why = _eng.refetch_apply_check(token, store_access.store())   # this thread's own reader
        if why:
            return jsonify({"ok": False, "error": why}), 409
        if _restore_job.get("status") == "running":
            return jsonify({"ok": False, "error": "a restore is running"}), 409
        if not (_event_loop and _event_loop.is_running()):
            return jsonify({"ok": False, "error": "engine loop not running"}), 500
        try:
            backup = _safety_backup("pre-refetch")
        except Exception as be:
            return jsonify({"ok": False, "error": "no safety backup, so nothing applied: %s" % be}), 500
        ok, reason = _eng.refetch_claim()
        if not ok:
            return jsonify({"ok": False, "error": reason}), 409
        try:
            _asyncio.run_coroutine_threadsafe(_eng.run_refetch_apply(token), _event_loop)
        except Exception:
            import job_coordinator as _jc
            _jc.release("range_refetch")
            raise
        return jsonify({"ok": True, "status": "running", "backup": backup})
    except Exception as e:
        logger.error("api_refetch_apply: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Gap fill as an action on the heatmap's selection (gap fill design §2–§4) ──────────────────
# The preview is counted from the store on the request thread (no Octopus call), held under a
# token for an hour; the apply re-counts, takes a required safety backup, and runs the fill as
# one engine job over the runs, finishing once.
_GAP_FILL_TTL_S = 3600
_gap_fill_preview: dict = {}


def _gap_fill_channels() -> tuple:
    return ("import", "export") if _main_has_export() else ("import",)


def _gap_fill_runs_from(body):
    runs = body.get("runs")
    if not isinstance(runs, list) or not runs or len(runs) > 2000:
        return None
    out = []
    for r in runs:
        if not (isinstance(r, (list, tuple)) and len(r) == 2 and all(isinstance(x, str) for x in r)):
            return None
        if r[0] >= r[1]:
            return None
        out.append([r[0], r[1]])
    return out


@app.route("/api/gaps/around", methods=["POST"])
def api_gaps_around():
    """The whole gaps the selection's missing half-hours sit in, counted (gap fill, 18 Oct 2026:
    any half-hour selected in a gap means the gap). Body {at: [half-hour start, …]} (naive UTC).
    Returns {gaps: [{from, to, missing, deleted, too_recent, too_old, fillable}]}. Read-only, and
    no supplier API needed: a gap fills from a CSV or the PDF bills too."""
    try:
        import engine as _eng
        at = (request.get_json(force=True, silent=True) or {}).get("at")
        if not isinstance(at, list) or not at or len(at) > 2000 \
                or not all(isinstance(x, str) for x in at):
            return jsonify({"ok": False, "error": "at must be a list of half-hour starts"}), 400
        st = store_access.store()
        gaps = _eng.gap_bounds(st, at)
        plan = _eng.gap_fill_plan(st, gaps, channels=_gap_fill_channels()) if gaps else {"runs": []}
        return jsonify({"ok": True, "gaps": plan["runs"]})
    except Exception as e:
        logger.error("api_gaps_around: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/gaps/fill/preview", methods=["POST"])
def api_gap_fill_preview():
    """What filling the selection's missing half-hours would do — counted from the store, nothing
    written. Body {runs: [[from, to), …]} (naive-UTC half-hour starts, the cells the heatmap
    marked missing). Returns per run and in total: missing, deleted, too recent (under 48 h:
    left out), too old (beyond Octopus's two years: left out), fillable; the size per channel;
    and a token for /apply (an hour)."""
    try:
        import engine as _eng
        import secrets as _secrets
        import time as _time
        if not _eng.kraken_available():
            return jsonify({"ok": False, "error": "no supplier API connected"}), 400
        runs = _gap_fill_runs_from(request.get_json(force=True, silent=True) or {})
        if runs is None:
            return jsonify({"ok": False, "error": "runs must be [[from, to), …], from before to"}), 400
        chans = _gap_fill_channels()
        plan = _eng.gap_fill_plan(store_access.store(), runs, channels=chans)
        token = _secrets.token_hex(8) if plan["fill_runs"] else None
        _gap_fill_preview.clear()
        if token:
            _gap_fill_preview.update({"token": token, "made": _time.time(), "runs": runs,
                                      "fill_runs": plan["fill_runs"], "channels": chans})
        return jsonify({"ok": True, "token": token, **plan})
    except Exception as e:
        logger.error("api_gap_fill_preview: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/gaps/fill/apply", methods=["POST"])
def api_gap_fill_apply():
    """Fill a preview's runs. Body {token}. Refused if the preview is gone or over an hour old,
    if the store no longer gives the same fillable runs, or while an import, its verify pass or a
    restore runs; a safety backup first, and none means no fill."""
    try:
        import engine as _eng
        import asyncio as _asyncio
        import time as _time
        token = ((request.get_json(force=True, silent=True) or {}).get("token") or "").strip()
        p = dict(_gap_fill_preview)
        if not token or p.get("token") != token:
            return jsonify({"ok": False, "error": "that preview is no longer held — preview again"}), 409
        if _time.time() - p["made"] > _GAP_FILL_TTL_S:
            return jsonify({"ok": False, "error": "that preview is over an hour old — preview again"}), 409
        again = _eng.gap_fill_plan(store_access.store(), p["runs"], channels=p["channels"])
        if again["fill_runs"] != p["fill_runs"]:
            return jsonify({"ok": False, "error": "the selection has changed since the preview — "
                                                  "preview again"}), 409
        if _eng.api_import_running():
            return jsonify({"ok": False, "error": "an import is already running"}), 409
        if _backfill_verify_active(_eng):
            return jsonify({"ok": False, "error": "the previous fill's pricing check is still "
                                                  "finishing"}), 409
        if _restore_job.get("status") == "running":
            return jsonify({"ok": False, "error": "a restore is running"}), 409
        if not (_event_loop and _event_loop.is_running()):
            return jsonify({"ok": False, "error": "engine loop not running"}), 500
        try:
            backup = _safety_backup("pre-gap-fill")
        except Exception as be:
            return jsonify({"ok": False, "error": "no safety backup, so nothing filled: %s" % be}), 500
        _gap_fill_preview.clear()
        _asyncio.run_coroutine_threadsafe(
            _eng.run_gap_fill_runs(p["fill_runs"], channels=p["channels"]), _event_loop)
        return jsonify({"ok": True, "status": "running", "backup": backup,
                        "runs": len(p["fill_runs"])})
    except Exception as e:
        logger.error("api_gap_fill_apply: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/gaps/fill/status", methods=["GET"])
def api_gap_fill_status():
    """The fill's progress: the shared import job state, with each run's under `runs`."""
    import engine as _eng
    return jsonify(_eng.api_import_status())


@app.route("/api/blocks/settlement", methods=["GET"])
def api_block_settlement():
    """One main-meter half-hour's three settlement facts with their sources — the detail panel
    (settlement surfaces design §4). `start` is the block's naive-UTC start. Read-only.

    {start, end, state: {bill, split, flags}, kwh: {stored, dcc, settled},
     export: {kwh, dcc} | null, cost: {stored, stored_exc, rate, rate_source, corrected},
     bill: {cost, cost_exc, kwh, label, fetched_at, home_kwh, home_rate, ev_kwh, ev_rate} | null,
     segments: [...], dispatch: {completed_kwh, kinds} | null}"""
    from datetime import datetime as _dt, timedelta as _td
    start = (request.args.get("start") or "").strip()
    try:
        t0 = _dt.fromisoformat(start)
    except ValueError:
        return jsonify({"error": "start must be an ISO date-time"}), 400
    start = t0.replace(tzinfo=None).isoformat()
    try:
        store = store_access.store()
        d = store.get_settlement_detail(start)
        if d is None:
            return jsonify({"error": "no block at %s" % start}), 404
        states = _settlement_states(store, start, (t0.replace(tzinfo=None) + _td(minutes=30)).isoformat())
        hist = store.get_dispatch_history_for_slot(start)
        comp = [h for h in hist if h.get("kind") == "completed" and h.get("energy_kwh") is not None]
        _r = lambda x, n=6: (round(float(x), n) if x is not None else None)
        return jsonify({
            "start": d["block_start"], "end": d["block_end"],
            "state": states.get(d["block_start"]),
            "kwh": {"stored": _r(d["imp_kwh"]), "dcc": _r(d["imp_kwh_api"]),
                    "settled": d["imp_kwh_api"] is not None
                               or (d["source"] or "").startswith("imported")},
            "export": ({"kwh": _r(d["exp_kwh"]), "dcc": _r(d["exp_kwh_api"])}
                       if (d["exp_kwh"] or d["exp_kwh_api"]) else None),
            "cost": {"stored": _r(d["imp_cost"]), "stored_exc": _r(d["imp_cost_exc"]),
                     "rate": _r(d["imp_rate"]), "rate_source": d["rate_source"],
                     "corrected": bool(d["rate_corrected"])},
            "bill": ({"cost": _r(d["bill_cost"]), "cost_exc": _r(d["bill_cost_exc"]),
                      "kwh": _r(d["bill_kwh"]), "label": d["bill_label"],
                      "fetched_at": d["bill_fetched_at"],
                      "home_kwh": _r(d["bill_home_kwh"]), "home_rate": _r(d["bill_home_rate"]),
                      "ev_kwh": _r(d["bill_ev_kwh"]), "ev_rate": _r(d["bill_ev_rate"])}
                     if d["bill_cost"] is not None else None),
            "segments": store.get_block_segments(start, "electricity_main"),
            "dispatch": ({"completed_kwh": (round(abs(float(comp[-1]["energy_kwh"])), 3)
                                            if comp else None),
                          "kinds": sorted({h.get("kind") for h in hist})} if hist else None),
        })
    except Exception as e:
        logger.warning("api_block_settlement: %s", e)
        return jsonify({"error": str(e)}), 500


def _settle_views(_eng) -> list:
    try:
        # Import bill wherever the install has a supplier API connection, any tariff — the
        # tariff decides what is final inside it, not whether it shows (design §9.2)
        views = ["bill"] if (_eng.mode_uses_api() and _eng.has_kraken_credentials()) else []
        # Export bill beside it where the main meter has an export channel
        if views and _main_has_export():
            views.append("export")
        # (The Split view went on 17 Oct 2026: the Import bill carries the split — teal for EMT's,
        # red for none at all — and the detail panel names each half-hour's split state.)
        return views
    except Exception:
        return []


def _main_has_export() -> bool:
    """The main meter has an export channel (the Export bill view, and what an unsettled export
    half-hour means)."""
    try:
        main = next((m for m in (load_config().get("meters") or {}).values()
                     if not (m.get("meta") or {}).get("sub_meter")), {})
        return "export" in (main.get("channels") or {})
    except Exception:
        return False


def _main_has_export_sensor() -> bool:
    """The main meter's export channel has a live read sensor, so a live export kWh is a reading.
    Without one (API-only, the Mini feeds import only) it is not: finalise stores 0.0."""
    try:
        main = next((m for m in (load_config().get("meters") or {}).values()
                     if not (m.get("meta") or {}).get("sub_meter")), {})
        return bool(((main.get("channels") or {}).get("export") or {}).get("read"))
    except Exception:
        return False


def _settlement_states(store, utc_start, utc_end) -> dict:
    """{block_start: {bill, export, split, flags}} for the main meter over [utc_start, utc_end)."""
    import engine as _eng
    import settlement_state as _ss
    import vat_calendar as _vc
    from datetime import datetime as _dt, timezone as _tz
    held, kept = _eng.bill_split_held(store)
    return _ss.classify_range(
        store.get_settlement_facts(utc_start, utc_end),
        smb=bool(_eng._import_is_smb_capped()), now=_dt.now(_tz.utc).replace(tzinfo=None),
        held=held, kept=kept, vat_at=_vc.rate_lookup(store.get_vat_calendar()),
        kind_at=_eng.tariff_kind_at, split_at=_eng.split_at,
        export_channel=_main_has_export(), export_sensor=_main_has_export_sensor(),
        export_agreement_at=_eng.export_agreement_at)


@app.route("/api/billing-source", methods=["GET"])
def api_billing_source_get():
    """Return the current global billing source and unsettled-block count."""
    try:
        import engine as _eng
        store = store_access.store()
        source = _eng._get_billing_source()
        unsettled = store.count_unsettled_blocks(since_iso=_settlement_horizon_floor())
        _uses_api = _eng.mode_uses_api()
        _has_creds = _eng.has_kraken_credentials()
        _since = _settlement_horizon_floor()
        try:
            awaiting = _eng.settlement_awaiting(store, _since)
        except Exception as _ae:
            logger.warning("api_billing_source_get: settlement stages: %s", _ae)
            awaiting = {"kwh": unsettled, "bill": 0, "split": 0}
        return jsonify({"source": source, "unsettled": unsettled,
                        # BL-85: the three stages a half-hour waits at — kWh, then its bill, then
                        # (IOG-SMB) its split. `unsettled` is the first, kept for its other readers.
                        "awaiting": awaiting,
                        # BL-85: the settlement views that mean something here — Import and
                        # Export bill on an API connection, Split on IOG-SMB (design §9.2).
                        "settle_views": _settle_views(_eng),
                        "api_available": _eng.kraken_available(),
                        # The supplier API is part of this install's data source — the
                        # rule is mode_uses_api(), so a page need not re-derive it
                        # from `mode` (the Heatmaps tab's API-coverage view).
                        "uses_api": bool(_uses_api),
                        # the main meter's channels the API supplies readings for —
                        # the API-coverage view shows one heatmap per channel.
                        "api_channels": (sorted((next((m for m in (load_config().get("meters") or {}).values()
                                                       if not (m.get("meta") or {}).get("sub_meter")), {})
                                                 .get("channels") or {}).keys()) if _uses_api else []),
                        # The DCC/CAD settlement choice only makes sense with BOTH a
                        # local sensor (CAD) AND a supplier API — i.e. mode 'cad+api'.
                        # Pure-API has no CAD to fall back to; pure-CAD has no DCC.
                        "mode": _eng.get_data_source_mode(),
                        # API/DCC mode is selected but the stored API key is gone
                        # (typically after restoring a backup onto a fresh install
                        # — creds aren't in the backup). Distinct from "no API".
                        "credentials_missing": bool(_uses_api and not _has_creds),
                        "unsupported_tariff": _eng._rate_schedule_unsupported})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/billing-source", methods=["POST"])
def api_billing_source_post():
    """Set the global billing source (cad/dcc). When it changes, all blocks are
    flagged for PASS 2 re-run (the drain re-materialises billing); the response
    carries the flagged count so the UI can warn that recalculation takes time.
    """
    try:
        import engine as _eng
        data = request.get_json(force=True) or {}
        new_source = (data.get("source") or "").lower()
        result = _engine_call(_eng.apply_billing_source_change, new_source)
        return jsonify({"ok": True, **result})
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/unsettled-blocks")
def api_unsettled_blocks():
    """View-only report of main-meter blocks with no DCC import settlement
    (running on CAD fallback). Relevant when billing_source='dcc'. Returns a
    capped list plus the full count."""
    try:
        store = store_access.store()
        limit = int(request.args.get("limit", 500))
        _floor = _settlement_horizon_floor()
        count = store.count_unsettled_blocks(since_iso=_floor)
        rows = store.get_unsettled_blocks(limit=limit, since_iso=_floor)
        out = [{"block_start": r["block_start"],
                "imp_kwh": r["imp_kwh"],
                "exp_kwh": r["exp_kwh"],
                "is_provisional": bool(r["is_provisional"])}
               for r in rows]
        return jsonify({"count": count, "limit": limit, "blocks": out})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Unified backfill (Heal Gaps + Historical Import) ────────────────────────
def _bf_step_minutes(store) -> int:
    """Block length in minutes from the earliest config period (fallback 30)."""
    try:
        row = store._conn.execute(
            "SELECT block_start FROM blocks ORDER BY block_start LIMIT 1").fetchone()
        if row:
            cp = store.get_config_period_for_date(row[0]) or {}
            return int(cp.get("block_minutes") or 30)
    except Exception:
        pass
    return 30


def _bf_flatten_gap_starts(gaps, step_min, cap=20000):
    """Expand [{start,end,slots}] runs into individual half-hour start ISO strings."""
    from datetime import datetime as _dt, timedelta as _td
    out, step = [], _td(minutes=step_min)
    for g in gaps:
        try:
            t = _dt.fromisoformat(g["start"]); end = _dt.fromisoformat(g["end"])
        except Exception:
            continue
        while t <= end and len(out) < cap:
            out.append(t.isoformat()); t += step
        if len(out) >= cap:
            break
    return out


def _bf_range_starts(from_iso, to_iso, step_min, cap=20000):
    """Every block start in [from, to) as ISO strings (capped)."""
    from datetime import datetime as _dt, timedelta as _td
    out = []
    try:
        t = _dt.fromisoformat(from_iso); end = _dt.fromisoformat(to_iso)
    except Exception:
        return out
    step = _td(minutes=step_min)
    while t < end and len(out) < cap:
        out.append(t.isoformat()); t += step
    return out


@app.route("/api/backfill/plan", methods=["POST"])
def api_backfill_plan():
    """Preview a unified backfill: gate + dispatch + window classification.

    Body: {scope: whole_history|gaps|range, source: api|csv, from?, to?}.
    Read-only — computes what WOULD happen, performs no writes. Delegates the
    decision logic to backfill.plan_backfill; this endpoint only gathers the live
    state (API availability, gaps, occupied windows) it needs.
    """
    try:
        import engine as _eng
        import backfill as _bf
        body   = request.get_json(silent=True) or {}
        scope  = body.get("scope")
        source = body.get("source")
        store  = store_access.store()     # this request thread's own read connection
        api_available = _eng.kraken_available()
        has_blocks    = bool(store.count_blocks())
        gaps          = store.find_block_gaps()

        target_starts = occupied_starts = None
        if scope == "gaps":
            target_starts = _bf_flatten_gap_starts(gaps, _bf_step_minutes(store))
            occupied_starts = []                     # gaps are empty by definition
        elif scope == "range":
            _from, _to = body.get("from"), body.get("to")
            if not _from or not _to:
                return jsonify({"ok": False, "reason": "bad_range",
                                "message": "A start and end date are required."}), 400
            occupied_starts = [r[0] for r in store._conn.execute(
                "SELECT block_start FROM blocks WHERE meter_id='electricity_main' "
                "AND block_start >= ? AND block_start < ? ORDER BY block_start",
                (_from, _to))]
            target_starts = _bf_range_starts(_from, _to, _bf_step_minutes(store))

        plan = _bf.plan_backfill(
            scope=scope, source=source, api_available=api_available,
            has_blocks=has_blocks, gaps=gaps,
            target_starts=target_starts, occupied_starts=occupied_starts)
        plan.update({"api_available": api_available, "has_blocks": has_blocks,
                     "gaps_present": bool(gaps),
                     "gap_slots_total": sum(g.get("slots", 0) for g in gaps)})
        return jsonify(plan), (200 if plan.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/backfill/run", methods=["POST"])
def api_backfill_run():
    """Execute a unified backfill after a preview.

    Re-checks the gate server-side (never trusts the client's earlier preview),
    requires `confirmed`, then dispatches on the plan's action:

      resolve_history_gaps -> run inline on the engine loop  (gaps + API)
      run_api_import_job   -> DEFER to /api/historical/api-import/start, the
                              dedicated background controller (owns backup +
                              pause/resume/cancel).
      apply_csv_import     -> apply here if a full `channel_csvs` dict is supplied;
                              otherwise DEFER to the CSV import wizard (which owns
                              rate/period confirmation).

    A 'deferred' response tells the caller which existing controller to drive.
    """
    try:
        import engine as _eng
        import backfill as _bf
        body   = request.get_json(silent=True) or {}
        scope  = body.get("scope")
        source = body.get("source")
        store  = store_access.store()     # this request thread's own read connection
        api_available = _eng.kraken_available()
        has_blocks    = bool(store.count_blocks())
        gaps          = store.find_block_gaps()

        gate = _bf.evaluate_gates(scope, source, api_available=api_available,
                                  has_blocks=has_blocks, gaps_present=bool(gaps))
        if not gate["allowed"]:
            return jsonify({"ok": False, "reason": gate["reason"],
                            "message": gate["message"]}), 400
        if not body.get("confirmed"):
            return jsonify({"ok": False, "reason": "unconfirmed",
                            "message": "Confirmation required before writing."}), 400

        action = _bf.dispatch_action(scope, source)["action"]

        if action == "resolve_history_gaps":
            result = _run_on_engine_loop(_eng.resolve_history_gaps(), timeout=300.0) or {}
            return jsonify({"ok": bool(result.get("ok", True)),
                            "action": action, **result}), 200

        if action == "apply_csv_import":
            channel_csvs = body.get("channel_csvs")
            if not channel_csvs and body.get("csv"):
                channel_csvs = {"import": body["csv"]}
            if not channel_csvs:
                return jsonify({"ok": True, "action": action, "deferred": True,
                                "use_endpoint": "/api/historical/import-csv",
                                "message": "Upload the CSV via the import wizard "
                                           "(rate/period confirmation happens there)."}), 200
            res = _engine_write(lambda st: st.apply_csv_import(channel_csvs),
                                timeout=1800.0)
            return jsonify({"ok": True, "action": action, "result": res}), 200

        if action == "run_api_import_job":
            return jsonify({"ok": True, "action": action, "deferred": True,
                            "use_endpoint": "/api/historical/api-import/start",
                            "message": "Start the background API import via its "
                                       "controller (backup + pause/resume/cancel)."}), 200

        return jsonify({"ok": False, "message": "no runner for action '%s'" % action}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/backfill/reach")
def api_backfill_reach():
    """How far back the supplier API can provide data, and the point it fills up to
    (your oldest existing block), for the Import-history 'how far back' display.
    Read-only; flattens plan_api_import. Returns ok:False (not an error) when no API."""
    try:
        import engine as _eng
        if not _eng.kraken_available():
            return jsonify({"ok": False, "reason": "no_api", "available": False}), 200
        plan = _run_on_engine_loop(_eng.plan_api_import(), timeout=60.0) or {}
        if not plan.get("ok"):
            return jsonify({"ok": False, "reason": plan.get("reason") or "unavailable",
                            "note": plan.get("note")}), 200
        chans = plan.get("channels") or {}
        froms = [c["window"]["from"] for c in chans.values()
                 if c.get("ok") and (c.get("window") or {}).get("from")]
        chunks = [c.get("chunk_count", 0) for c in chans.values() if c.get("ok")]
        return jsonify({"ok": True, "available": True,
                        "reach_from": (min(froms) if froms else None),
                        "up_to": plan.get("go_live"),
                        "chunk_estimate": (max(chunks) if chunks else 0)}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _gap_end_inclusive(store, frm, to):
    """A gap's `to` is the LAST missing slot's START (inclusive), but the fill window
    (run_gap_fill_job → import_api_history's until_ts) is HALF-OPEN, so passing `to`
    as-is fetches [from, to) and leaves the final missing half-hour unfilled — the
    "1 block still missing after a gap fill" off-by-one. Extend `to` by one block so
    the last slot is included, mirroring the CSV gap-template's own inclusive step.
    Returns the extended ISO string (or the original `to` unchanged on any failure)."""
    try:
        bm, _tz = _period_block_minutes_tz(store, frm)
        from datetime import datetime as _dt, timedelta as _td
        t = _dt.fromisoformat(str(to).replace("Z", "").split("+")[0])
        return (t + _td(minutes=bm)).isoformat()
    except Exception:
        return to


def _backfill_verify_active(_eng) -> bool:
    """True while the deferred pricing-verification pass is running/waiting/paused.
    A backfill RUN isn't finished until this post-import pass completes, so the
    start endpoints treat it as 'still busy' — no second run can begin until the
    current one (import THROUGH verify) is done. Server-enforced so a page refresh
    or a direct API call can't get round the UI lock."""
    try:
        return (_eng.api_verify_pricing_status() or {}).get("status") in (
            "running", "waiting", "paused")
    except Exception:
        return False


@app.route("/api/backfill/fill-gap", methods=["POST"])
def api_backfill_fill_gap():
    """Fill ONE gap window via the supplier API — as a BACKGROUND job, identical in
    behaviour to the whole/date import but bounded to [from → to] on the gap's
    channel: in-pass price recovery, the verify pass, carbon re-arm, chart rebuild,
    pause/resume — all tracked in the shared import status + pricing-health panel.
    Channel-scoped so a missing-export gap doesn't overwrite present live import
    data. Body: {from, to, channel?, confirmed}. Requires API + confirmation."""
    try:
        import engine as _eng
        import asyncio as _asyncio
        if not _eng.kraken_available():
            return jsonify({"ok": False, "reason": "no_api",
                            "message": "No supplier API configured."}), 400
        body = request.get_json(silent=True) or {}
        frm, to = body.get("from"), body.get("to")
        if not frm or not to:
            return jsonify({"ok": False, "reason": "bad_range",
                            "message": "from and to are required."}), 400
        if not body.get("confirmed"):
            return jsonify({"ok": False, "reason": "unconfirmed",
                            "message": "Confirmation required before writing."}), 400
        if _eng.api_import_running():
            return jsonify({"ok": False, "reason": "busy",
                            "message": "An import is already running.",
                            "status": _eng.api_import_status()}), 409
        if _backfill_verify_active(_eng):
            return jsonify({"ok": False, "reason": "verify_active",
                            "message": "The previous backfill's pricing-verification pass is still "
                                       "finishing. Wait for it to complete before starting another."}), 409
        if not (_event_loop and _event_loop.is_running()):
            return jsonify({"ok": False, "reason": "no_loop",
                            "message": "engine loop not running"}), 500
        ch = body.get("channel")
        channels = (ch,) if ch in ("import", "export") else ("import", "export")
        # Include the gap's final half-hour: `to` is the last missing slot's start
        # (inclusive) but the fill window is half-open — extend by one block.
        to = _gap_end_inclusive(store_access.store(), frm, to)
        _asyncio.run_coroutine_threadsafe(
            _eng.run_gap_fill_job(frm, to, channels=channels), _event_loop)
        return jsonify({"ok": True, "status": "running"}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/blocks/reimport", methods=["POST"])
def api_blocks_reimport():
    """BL-8 phase 2: TARGETED restore of a deliberately-deleted range — the explicit
    inverse of Delete. Lifts the tombstone over [from_date, to_date] (sub-span split)
    and IMMEDIATELY re-fills the window from the supplier API. The tombstone lift
    happens inside `run_gap_fill_job` (the shared targeted-fill worker), so this is
    the same 'clear + fill' primitive the per-gap button uses, scoped to a date range.
    Body: {from_date, to_date, meter_id?, channel?, confirmed}."""
    try:
        import engine as _eng
        import asyncio as _asyncio
        from block_store import local_date_range_to_utc_bounds
        if not _eng.kraken_available():
            return jsonify({"ok": False, "reason": "no_api",
                            "message": "No supplier API configured."}), 400
        body = request.get_json(silent=True) or {}
        # Accept EITHER an explicit UTC window (from the deleted-ranges list, exact —
        # no lossy local-date round-trip across a DST boundary) OR local dates.
        from_utc = (body.get("from_utc") or "").strip()
        to_utc   = (body.get("to_utc") or "").strip()
        if not body.get("confirmed"):
            return jsonify({"ok": False, "reason": "unconfirmed",
                            "message": "Confirmation required before writing."}), 400
        if _eng.api_import_running():
            return jsonify({"ok": False, "reason": "busy",
                            "message": "An import is already running.",
                            "status": _eng.api_import_status()}), 409
        if not (_event_loop and _event_loop.is_running()):
            return jsonify({"ok": False, "reason": "no_loop",
                            "message": "engine loop not running"}), 500
        if from_utc and to_utc:
            if from_utc >= to_utc:
                return jsonify({"ok": False, "reason": "bad_range",
                                "message": "from_utc must be before to_utc."}), 400
            utc_start, utc_end = from_utc, to_utc
        else:
            from_date = (body.get("from_date") or "").strip()
            to_date   = (body.get("to_date") or "").strip()
            if not from_date or not to_date:
                return jsonify({"ok": False, "reason": "bad_range",
                                "message": "from_date and to_date (or from_utc and "
                                           "to_utc) are required."}), 400
            if from_date > to_date:
                return jsonify({"ok": False, "reason": "bad_range",
                                "message": "from_date must not be after to_date."}), 400
            _cfg = load_config()
            _tz = "UTC"
            for _md in (_cfg.get("meters") or {}).values():
                if not (_md.get("meta") or {}).get("sub_meter"):
                    _tz = (_md.get("meta") or {}).get("timezone", "UTC")
                    break
            utc_start, utc_end = local_date_range_to_utc_bounds(from_date, to_date, _tz)
        meter_id = body.get("meter_id") or "electricity_main"
        ch = body.get("channel")
        channels = (ch,) if ch in ("import", "export") else ("import", "export")
        _asyncio.run_coroutine_threadsafe(
            _eng.run_gap_fill_job(utc_start, utc_end, channels=channels,
                                  meter_id=meter_id), _event_loop)
        return jsonify({"ok": True, "status": "running",
                        "window": {"from": utc_start, "to": utc_end}}), 200
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        logger.error("api_blocks_reimport: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/blocks/deleted-ranges")
def api_blocks_deleted_ranges():
    """Active deletion tombstones for the Data Management list. Each is annotated
    with `fillable` — whether the supplier API can re-fetch it (advisory only; the
    re-import button uses it to warn/disable, it NEVER auto-clears a tombstone)."""
    try:
        import engine as _eng
        from datetime import datetime, timezone, timedelta
        store = store_access.store()
        # 4.5.6: orphaned tombstones — rows whose range is already fully populated
        # again — are HIDDEN here and PRUNED by the engine (run_prune_stale_tombstones,
        # hourly). This GET used to prune them itself through _engine_write: a write
        # in a read, and a wait on the engine loop on every page view (first Pi soak:
        # p50 0.02 s quiet, 16.9 s while the re-price sweep held the loop).
        try:
            _stale = set(store.stale_deleted_range_ids())
        except Exception as _pe:
            logger.warning("deleted-ranges: stale-tombstone check failed: %s", _pe)
            _stale = set()
        api_ok = _eng.kraken_available()
        horizon_days = getattr(_eng, "_KRAKEN_BACKFILL_DAYS", 400) or 400
        floor = (datetime.now(timezone.utc).replace(tzinfo=None)
                 - timedelta(days=horizon_days)).isoformat()
        from block_store import utc_to_local_label as _lbl
        _tz = _config_timezone()
        ranges = [{**r, "fillable": bool(api_ok and r["end_utc"] > floor),
                   "start_local": _lbl(r["start_utc"], _tz),
                   "end_local": _lbl(r["end_utc"], _tz)}
                  for r in store.get_deleted_ranges() if r["id"] not in _stale]
        return jsonify({"ok": True, "ranges": ranges, "api_available": api_ok,
                        "timezone": _tz})
    except Exception as e:
        logger.error("api_blocks_deleted_ranges: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/backfill/gaps")
def api_backfill_gaps():
    """Unified "what's missing" for the heal UI. The user doesn't distinguish, so
    we merge two kinds into one labelled list:

      * missing BLOCK rows  — find_block_gaps() (import channel, interior holes).
      * missing per-channel DATA — the import's own self-clearing record for the
        import AND export channels (engine.api_import_gaps): windows where blocks
        exist but that channel's data is absent (e.g. a settlement/DCC outage).

    Each entry carries channel + kind so the fill can pick the right template.
    `fetchable` marks entries an API can (re)fetch. Read-only."""
    try:
        import engine as _eng
        store = store_access.store()
        api_available = _eng.kraken_available()
        # Does the main meter export? A whole missing BLOCK then needs BOTH channels
        # filled (import AND export), not just import — otherwise the CSV template and
        # the API fill silently omit export for that window.
        try:
            _cfg = load_config()
            _main = next((m for m in _cfg.get("meters", {}).values()
                          if not (m.get("meta") or {}).get("sub_meter")), {})
            has_export = "export" in (_main.get("channels") or {})
        except Exception:
            has_export = False
        block_channels = ["import", "export"] if has_export else ["import"]
        gaps = []
        # (1) Missing block rows — the whole block is absent, so it needs every
        # configured channel (import, and export where the meter exports).
        for g in store.find_block_gaps():
            slots = int(g.get("slots") or 0)
            # BL-8 phase 2: flag a hole that is a deliberate deletion (tombstoned) so
            # the UI can label it "deleted — fill to restore" rather than as a fault.
            # It still appears here so the user can TARGET it for a restore; only the
            # blanket "recover all" and the automatic poll step over it.
            _deleted = store.is_slot_tombstoned("electricity_main", g.get("start"))
            gaps.append({"start": g.get("start"), "end": g.get("end"), "slots": slots,
                         "hours": round(slots / 2.0, 1), "channel": "import",
                         "channels": list(block_channels),
                         "kind": "missing_blocks", "fetchable": True,
                         "deleted": _deleted})
        # (2) Missing per-channel data recorded by the import (import + export).
        imp = _eng.api_import_gaps() or {}
        for ch, cd in (imp.get("channels") or {}).items():
            for g in (cd.get("gaps") or []):
                cnt = int(g.get("count") or 0)
                if not g.get("from"):
                    continue
                gaps.append({"start": g.get("from"), "end": g.get("to") or g.get("from"),
                             "slots": cnt, "hours": round(cnt / 2.0, 1), "channel": ch,
                             "channels": [ch],
                             "kind": "missing_data", "fetchable": api_available})
        gaps.sort(key=lambda x: (x.get("start") or ""), reverse=True)
        # Gap start/end are naive-UTC block_start; annotate wall-clock (local) so the
        # UI shows the times the user thinks in, not UTC. start/end stay UTC (the
        # fill/re-import endpoints need them).
        from block_store import utc_to_local_label as _lbl
        _tz = _config_timezone()
        for g in gaps:
            g["start_local"] = _lbl(g.get("start"), _tz)
            g["end_local"] = _lbl(g.get("end"), _tz)
        return jsonify({
            "ok": True, "gaps": gaps, "timezone": _tz,
            "total_slots": sum(g["slots"] for g in gaps),
            "fetchable_slots": sum(g["slots"] for g in gaps if g["fetchable"]),
            "api_available": api_available,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/history-gaps")
def api_history_gaps():
    """Review: missing half-hour slots in the block history (outage holes).

    Read-only, and deliberately NOT gated on the supplier API: finding gaps is a
    question about our own blocks table, which a CAD-only user can ask and get a
    true answer to. `api_available` is returned so the UI can explain that
    *recovering* them does need a supplier (the readings can only come from there)
    rather than silently hiding the action.
    """
    try:
        import engine as _eng
        store = store_access.store()
        gaps = store.find_block_gaps()
        return jsonify({"ok": True, "gaps": gaps,
                        "total_slots": sum(g["slots"] for g in gaps),
                        "api_available": _eng.kraken_available()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/resolve-gaps", methods=["POST"])
def api_resolve_gaps():
    """Act: backfill history gaps from settled supplier data.

    User-triggered on purpose — an automatic sweep could not tell an outage gap
    from blocks the user deleted deliberately, and would resurrect them.
    """
    try:
        import engine as _eng
        if not _eng.kraken_available():
            return jsonify({"ok": False, "reason": "no_api",
                            "error": "No supplier API configured"}), 400
        result = _run_on_engine_loop(_eng.resolve_history_gaps(), timeout=300.0)
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/retry-settlement", methods=["POST"])
def api_retry_settlement():
    """User-triggered re-fetch of DCC over oldest-unsettled → now, to settle
    late/old gaps on demand. Requires a configured Kraken API. Runs the async
    retry on the engine's event loop (server handlers are sync)."""
    try:
        import engine as _eng
        if not _eng.kraken_available():
            return jsonify({"ok": False, "reason": "no_api",
                            "error": "No supplier API configured"}), 400
        result = _run_on_engine_loop(_eng.retry_settlement_for_unsettled(),
                                     timeout=120.0)
        # Settlement rewrote block figures (e.g. late export finally landing) — the
        # pre-built charts still show the pre-settlement values until regenerated.
        status = 200 if result.get("ok") else 400
        return jsonify(result), status
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/historical/probe", methods=["POST"])
def api_historical_probe():
    """READ-ONLY beta diagnostic (historical-import spike): probe HA long-term
    statistics for the chosen per-device sensors, to characterise retention,
    cadence, gaps, energy-vs-power and DST/timestamp behaviour before building
    the historical import. Creates no blocks and writes nothing.

    Body: {"entity_ids": [...] } or {"sensors": [{"device","entity_id"},...]},
    optional "days" (lookback, default 800, max 2000)."""
    try:
        import engine as _eng
        body = request.get_json(force=True, silent=True) or {}
        entity_ids = body.get("entity_ids") or [
            (s or {}).get("entity_id") for s in (body.get("sensors") or [])]
        entity_ids = [e for e in entity_ids if e]
        if not entity_ids:
            return jsonify({"ok": False, "error": "no sensors selected"}), 400
        # No look-back control any more — probe the full retained history. HA only
        # keeps so much, so a generous window == "everything there is". A caller can
        # still pass days to narrow it.
        try:
            days = max(1, min(int(body.get("days", 3660)), 3660))
        except (TypeError, ValueError):
            days = 3660
        result = _run_on_engine_loop(
            _eng.probe_recorder_statistics(entity_ids, days), timeout=300.0)
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/historical/probe-devices", methods=["GET"])
def api_probe_devices():
    """List configured meters/devices with the sensor entities currently assigned to
    each, so the recorder-history probe can pre-populate itself. Read-only."""
    try:
        from datetime import datetime as _dt
        cfg = load_config()
        today = _dt.now().strftime("%Y-%m-%d")
        # Sensor ledger: if a device still has a reconstructed layer, remember the
        # sensor(s) that built it (from the run ledger) and pre-populate them until
        # that layer is removed — so revisiting the page shows what was used.
        store = store_access.store()
        _latest_run_sensors = {}
        for _r in (store.get_attribution_runs() or []):
            _mid = _r.get("meter_id")
            if _mid and _r.get("sensor_ids"):
                _latest_run_sensors[_mid] = list(_r["sensor_ids"])   # appended in order → last wins
        _attributed_meters = {m["meter_id"] for m in
                              (store.count_recorder_attributed().get("meters") or [])}
        devices = []
        for m_id, m_data in (cfg.get("meters") or {}).items():
            meta = (m_data or {}).get("meta", {}) or {}
            if not meta.get("sub_meter"):
                continue                       # main meter = the house control total,
                                               # not an attributable device — omit it
            if meta.get("retired_at") and meta["retired_at"] <= today:
                continue                       # skip retired sub-meters
            is_sub = True
            label = meta.get("device") or m_id
            sensors = []
            for ch in (m_data.get("channels") or {}).values():
                rd = (ch or {}).get("read")    # per-channel energy read sensor
                if rd:
                    sensors.append(rd)
            for k in ("power_sensor", "device_power_sensor", "soc_sensor",
                      "inverter_power_sensor", "pv_power_sensor"):
                v = meta.get(k)
                if v:
                    sensors.append(v)
            seen = set()
            sensors = [s for s in sensors if not (s in seen or seen.add(s))]
            # Kept sensors from the last reconstruction, while its layer still exists.
            history_sensors = (_latest_run_sensors.get(m_id, [])
                               if m_id in _attributed_meters else [])
            devices.append({
                "meter_id": m_id, "label": label, "is_sub_meter": is_sub,
                "meter_type": meta.get("meter_type") or ("sub" if is_sub else "main"),
                "sensors": sensors,
                "history_sensors": history_sensors,
            })
        devices.sort(key=lambda d: (d["is_sub_meter"], d["label"].lower()))
        return jsonify({"ok": True, "devices": devices})
    except Exception as e:
        logger.error("api_probe_devices: %s", e)
        return jsonify({"ok": False, "error": str(e), "devices": []}), 500


@app.route("/api/historical/attribute/start", methods=["POST"])
def api_attribute_start():
    """Start a recorder device-attribution run as a BACKGROUND job. Body:
    {meter_id, sensor_ids:[...]}. Takes a backup, schedules the worker on the engine
    loop, returns at once. Poll /status, drive /control, undo via /backout."""
    try:
        import engine as _eng
        import asyncio as _asyncio
        body = request.get_json(force=True, silent=True) or {}
        meter_id = (body.get("meter_id") or "").strip()
        sensor_ids = [s for s in (body.get("sensor_ids") or []) if s]
        if not meter_id or not sensor_ids:
            return jsonify({"ok": False, "error": "meter_id and at least one sensor required"}), 400
        # Attribution is for sub-meter DEVICES only. The main meter is the house
        # control total — its usage is the remainder, never a reconstructed device.
        _meta = ((load_config().get("meters") or {}).get(meter_id) or {}).get("meta") or {}
        if not _meta.get("sub_meter"):
            return jsonify({"ok": False, "error": "attribution applies to sub-meter "
                            "devices only; the house total is the control total"}), 400
        if not (_event_loop and _event_loop.is_running()):
            return jsonify({"ok": False, "error": "engine loop not running"}), 500
        # CLAIM THE DEVICE FIRST, synchronously. Everything below this line is slow
        # — a preflight that can take 180s and a full database zip — and the run's
        # status used to be set only inside the coroutine scheduled at the end. A
        # second start arriving in that gap passed the old check. Reserving the
        # meter_id under a lock closes it; every early return below must release.
        if not _eng.attribution_claim(meter_id, sensor_ids):
            return jsonify({"ok": False,
                            "error": "an attribution run is already active for this device",
                            "status": _eng.api_attribution_status(meter_id)}), 409
        # Sanity-check the chosen sensor(s) against the house import before writing:
        # a device that totals MORE than the house drew (or is absurdly small) is
        # almost certainly the wrong sensor or a Wh/kWh mix-up. The user can still
        # proceed by re-submitting with confirm_override.
        if not body.get("confirm_override"):
            try:
                pf = _asyncio.run_coroutine_threadsafe(
                    _eng.attribution_preflight(meter_id, sensor_ids), _event_loop
                ).result(timeout=180)
            except Exception as _pe:
                logger.warning("attribution preflight failed (continuing): %s", _pe)
                pf = {"verdict": "error"}
            _v = pf.get("verdict")
            if _v in ("device_exceeds_house", "suspiciously_small"):
                _dk, _hk = pf.get("device_kwh"), pf.get("house_kwh")
                _sp = ((pf.get("from") or "")[:10] + " → " + (pf.get("to") or "")[:10])
                _msg = (("This sensor totals about %s kWh over %s, but the house only "
                         "imported %s kWh in that time. A device can't exceed the house it "
                         "draws from — this looks like the wrong sensor, or a Wh/kWh mismatch."
                         % (_dk, _sp, _hk)) if _v == "device_exceeds_house" else
                        ("This sensor totals only about %s kWh over %s vs the house's %s kWh — "
                         "unusually small, which can mean a unit mismatch or the wrong sensor."
                         % (_dk, _sp, _hk)))
                _eng.attribution_release(meter_id)
                return jsonify({"ok": False, "sanity": _v, "warn": _msg,
                                "device_kwh": _dk, "house_kwh": _hk}), 409
        backup = None
        try:
            backup = os.path.basename(_create_backup_zip(label="pre-attribution"))
        except Exception as be:
            logger.warning("attribution: backup failed (continuing): %s", be)
        try:
            _asyncio.run_coroutine_threadsafe(
                _eng.run_attribution_job(meter_id, sensor_ids, pace_s=0.05), _event_loop)
        except Exception:
            _eng.attribution_release(meter_id)
            raise
        return jsonify({"ok": True, "status": "running", "backup": backup,
                        "device": meter_id})
    except Exception as e:
        logger.error("api_attribute_start: %s", e)
        try:
            import engine as _eng_rel
            _eng_rel.attribution_release(meter_id)
        except Exception:
            pass
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/historical/attribute/status", methods=["GET"])
def api_attribute_status():
    """Poll the background attribution job(s).

    Flat keys describe the active run (or the most recent), so the existing page
    polls unchanged; `runs` carries every device. `?meter_id=` selects one.
    """
    import engine as _eng
    return jsonify(_eng.api_attribution_status(
        (request.args.get("meter_id") or "").strip() or None))


@app.route("/api/historical/attribute/control", methods=["POST"])
def api_attribute_control():
    """pause / resume / cancel the running attribution job."""
    import engine as _eng
    _body = request.get_json(force=True, silent=True) or {}
    action = (_body.get("action") or "").lower()
    if action not in ("pause", "resume", "cancel"):
        return jsonify({"ok": False, "error": "action must be pause/resume/cancel"}), 400
    # `meter_id` is optional — the current page does not send one, and with a
    # single run that addresses it. With several runs and no meter_id it applies
    # to all of them, which is the honest reading of an unqualified "cancel".
    return jsonify({"ok": True,
                    **_eng.attribution_control(action, (_body.get("meter_id") or "").strip() or None)})


@app.route("/api/historical/attribute/runs", methods=["GET"])
def api_attribute_runs():
    """The attribution run ledger + a summary of the reconstructed device layer,
    for the runs/undo list."""
    store = store_access.store()
    return jsonify({"ok": True, "runs": store.get_attribution_runs(),
                    "summary": store.count_recorder_attributed()})


@app.route("/api/historical/attribute/backout", methods=["POST"])
def api_attribute_backout():
    """Undo an attribution run (by run_id) or an explicit device+range: deletes the
    reconstructed device blocks and re-derives the parent remainder. Refused while a
    run is active, while another back-out is already in flight, or for a run that is
    no longer in the ledger (already undone) — so a double-click can't misfire."""
    try:
        import engine as _eng
        if _eng.attribution_running():
            return jsonify({"ok": False, "error": "stop the running attribution first"}), 409
        if _eng.backout_running():
            return jsonify({"ok": False, "error": "an undo is already in progress",
                            "status": _eng.api_backout_status()}), 409
        body = request.get_json(force=True, silent=True) or {}
        res = _eng.backout_recorder_attribution(
            run_id=body.get("run_id"), meter_id=body.get("meter_id"),
            from_date=body.get("from"), to_date=body.get("to"))
        if not res.get("ok"):
            err = res.get("error")
            if err == "backout_in_progress":
                return jsonify({"ok": False, "error": "an undo is already in progress",
                                "status": _eng.api_backout_status()}), 409
            if err == "run_not_found":
                return jsonify({"ok": False, "error": "that run was already undone "
                                "(or no longer exists)"}), 409
            return jsonify(res), 500
        return jsonify(res)
    except Exception as e:
        logger.error("api_attribute_backout: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/historical/attribute/backout/status", methods=["GET"])
def api_attribute_backout_status():
    """Poll whether an undo is in flight (UI disables the undo buttons)."""
    import engine as _eng
    return jsonify(_eng.api_backout_status())


@app.route("/api/historical/export-probe", methods=["POST"])
def api_export_probe():
    """READ-ONLY beta diagnostic (historical-import spike): measure how far back
    the Octopus API reaches for import vs export, via cheap single-row boundary
    fetches. No body — uses the discovered meters. Creates nothing, writes
    nothing. Gated on an available API (nothing to probe otherwise)."""
    try:
        import engine as _eng
        if not _eng.kraken_available():
            return jsonify({"ok": False, "error": "no API connected"}), 400
        result = _run_on_engine_loop(
            _eng.probe_consumption_retention(), timeout=60.0)
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/historical/consumption-diagnostic", methods=["POST"])
def api_consumption_diagnostic():
    """READ-ONLY deep diagnostic: enumerate every meter point + serial on the
    account with each serial's reachable consumption span, to tell apart an API
    retention limit, a meter exchange, or a query artifact. No body. Writes
    nothing. Gated on an available API."""
    try:
        import engine as _eng
        if not _eng.kraken_available():
            return jsonify({"ok": False, "error": "no API connected"}), 400
        result = _run_on_engine_loop(
            _eng.diagnose_consumption_retention(), timeout=180.0)
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/historical/import-range-diagnostic", methods=["POST"])
def api_import_range_diagnostic():
    """READ-ONLY: report where each channel's import starts (its earliest supplier
    agreement) and probe the Measurements API for data BEFORE that floor — so we
    can tell whether real history exists earlier than where the import began
    (e.g. import from 01/07/24, export from a later export-agreement date). Writes
    nothing. Gated on an available API."""
    try:
        import engine as _eng
        if not _eng.kraken_available():
            return jsonify({"ok": False, "error": "no API connected"}), 400
        result = _run_on_engine_loop(
            _eng.diagnose_import_range(), timeout=120.0)
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/historical/site-plan", methods=["GET"])
def api_historical_site_plan():
    """READ-ONLY. Discover the account's property history for the PRE-import
    'confirm your sites' step. Returns {ok, needs_confirmation, sites[]}. When
    needs_confirmation is False (never-moved account), the wizard skips straight
    to the import. Writes nothing."""
    try:
        import engine as _eng
        if not _eng.kraken_available():
            return jsonify({"ok": False, "error": "no API connected"}), 400
        result = _run_on_engine_loop(_eng.discover_pre_import_sites(), timeout=60.0)
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/historical/site-plan/apply", methods=["POST"])
def api_historical_site_plan_apply():
    """Create/extend config periods to match the user-confirmed tenancy sites
    BEFORE the import runs, so imported blocks land in the right period from the
    first write. Body: {"sites": [{outcode, from, to, site_name?}]}. Sets a marker
    so the post-import region probe doesn't re-stamp the layout. Backs up first."""
    try:
        store = store_access.store()
        data = request.get_json(force=True, silent=True) or {}
        sites = data.get("sites") or []
        if not sites:
            return jsonify({"ok": False, "error": "no sites supplied"}), 400
        try:
            backup = os.path.basename(_create_backup_zip(label="pre-import-sites"))
        except Exception as be:
            backup = None
            logger.warning("site-plan apply: backup failed (continuing): %s", be)
        from datetime import datetime as _dt_now
        def _apply_sites(st):
            out = st.apply_pre_import_sites(sites)
            st.set_meta("preimport_sites_applied", _dt_now.utcnow().isoformat())
            st.rearm_carbon_backfill()   # new regions → re-scan carbon after import
            return out

        res = _engine_write(_apply_sites)
        try:
            _engine_write(_rebuild_config_period_chain)
        except Exception as _e:
            logger.warning("site-plan apply: chain rebuild failed: %s", _e)
        logger.info("api_historical_site_plan_apply: %s", res)
        return jsonify({"ok": True, "backup": backup, **res})
    except Exception as e:
        logger.error("api_historical_site_plan_apply: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/historical/api-import/plan", methods=["POST"])
def api_historical_api_plan():
    """READ-ONLY preview for the API import route: the per-channel window and
    newest→oldest chunk plan (clamped to the ~2-year wall + go-live). No writes."""
    try:
        import engine as _eng
        if not _eng.kraken_available():
            return jsonify({"ok": False, "error": "no API connected"}), 400
        body = request.get_json(force=True, silent=True) or {}
        try:
            chunk_days = max(1, min(int(body.get("chunk_days", 60)), 120))
        except (TypeError, ValueError):
            chunk_days = 60
        result = _run_on_engine_loop(
            _eng.plan_api_import(body.get("from"), chunk_days=chunk_days), timeout=60.0)
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/historical/api-import/start", methods=["POST"])
def api_historical_api_start():
    """Start the API import as a BACKGROUND job (survives navigating away; can be
    paused/resumed/cancelled). Takes a backup, then schedules the worker on the
    engine loop and returns immediately. Poll /status; drive /control."""
    try:
        import engine as _eng
        import asyncio as _asyncio
        if not _eng.kraken_available():
            return jsonify({"ok": False, "error": "no API connected"}), 400
        body = request.get_json(force=True, silent=True) or {}
        if not body.get("confirmed"):
            return jsonify({"ok": False, "error": "confirmation required"}), 400
        if _backfill_verify_active(_eng):
            return jsonify({"ok": False, "reason": "verify_active",
                            "error": "The previous backfill's pricing-verification pass is still "
                                     "finishing. Wait for it to complete before starting another."}), 409
        if not (_event_loop and _event_loop.is_running()):
            return jsonify({"ok": False, "error": "engine loop not running"}), 500
        try:
            chunk_days = max(1, min(int(body.get("chunk_days", 60)), 120))
        except (TypeError, ValueError):
            chunk_days = 60
        try:
            max_chunks = max(1, min(int(body.get("max_chunks", 1)), 60))
        except (TypeError, ValueError):
            max_chunks = 1
        try:
            pace_s = min(max(float(body.get("pace_s", 1.5)), 0.0), 10.0)
        except (TypeError, ValueError):
            pace_s = 1.5
        # Claim BEFORE the backup (B §5, acceptance 4): the check-then-backup window
        # let two starts both pass, and a delete or restore see no import to stop.
        if not _eng.api_import_claim():
            return jsonify({"ok": False, "error": "an import is already running",
                            "status": _eng.api_import_status()}), 409
        try:
            backup = None
            try:
                backup = os.path.basename(_create_backup_zip(label="pre-api-import"))
            except Exception as be:
                logger.warning("api import: backup failed (continuing): %s", be)
            _asyncio.run_coroutine_threadsafe(
                _eng.run_api_import_job(body.get("from"), chunk_days=chunk_days,
                                        max_chunks=max_chunks, pace_s=pace_s),
                _event_loop)      # fire-and-forget; poll /status
        except Exception:
            _eng.api_import_release()
            raise
        return jsonify({"ok": True, "status": "running", "backup": backup})
    except Exception as e:
        logger.error("api_historical_api_start: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/historical/api-import/control", methods=["POST"])
def api_historical_api_control():
    """pause / resume / cancel the running background import."""
    try:
        import engine as _eng
        body = request.get_json(force=True, silent=True) or {}
        return jsonify(_eng.api_import_control(body.get("action", "")))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/historical/api-import/status", methods=["GET"])
def api_historical_api_status():
    """Poll the background import job's progress/status."""
    try:
        import engine as _eng
        return jsonify(_eng.api_import_status())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/historical/api-import/gaps", methods=["GET"])
def api_historical_api_gaps():
    """Persisted reconstruction gaps (missing half-hour runs) per channel from the
    last import — so the import page can show holes without re-querying. Read-only."""
    try:
        import engine as _eng
        return jsonify(_eng.api_import_gaps())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/historical/api-import/health", methods=["GET"])
def api_historical_api_health():
    """Post-import health summary (flags raised / auto-recovered / still remaining,
    plus imported range) for the import page's persistent panel. 'remaining' is read
    live from the reprice queue. Read-only."""
    try:
        import engine as _eng
        # Read-only demo payloads so the panel's unhappy states can be inspected on a
        # healthy account (see engine.api_import_health_demo). Never reached unless the
        # caller asks for it by name.
        # Operator-only. Silently ignored unless the add-on is running with
        # `log_level: debug`, so a normal user who lands on a shared URL carrying the
        # parameter just sees their own real figures rather than an error or a banner.
        demo = (request.args.get("demo") or "").strip()
        if demo and _eng.debug_mode():
            return jsonify(_eng.api_import_health_demo(demo))
        return jsonify(_eng.api_import_health())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/historical/api-import/verify-status", methods=["GET"])
def api_historical_verify_status():
    """Live status of the deferred, rate-limit-gated pricing-verification pass that
    runs after an import (chunk progress, slots corrected, waiting-for-allowance).
    Read-only."""
    try:
        import engine as _eng
        return jsonify(_eng.api_verify_pricing_status())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/historical/reprice-repair", methods=["POST"])
def api_historical_reprice_repair():
    """Calm, targeted re-price of imported half-hours the bulk import mispriced
    (Measurements returned kWh but empty cost metaData under load). Re-queries the
    slots one small window at a time and re-prices in place. Also the root-cause
    test: the response's recovered/still_missing split says whether a calm re-fetch
    recovers what the loaded import missed (⇒ load-induced) or not (⇒ real gap).

    Body: {from_date?, to_date?, preview?, include_export?}. YYYY-MM-DD; omit dates
    to use the reprice queue. `preview` counts the affected blocks WITHOUT running
    (for a confirm step). Range mode defaults to IMPORT-only; set `include_export`
    to also reprice export — now that _billed_rate prefers the flat/Agile schedule
    for a published-rate slot, this COLLAPSES a fragmented export rate back to its
    clean scheduled value (it no longer fragments it from cost÷kWh)."""
    try:
        import engine as _eng
        if not _eng.kraken_available():
            return jsonify({"ok": False, "error": "no API connected"}), 400
        # Guard against a concurrent API pass: the deferred verify-pricing pass
        # fetches from Octopus on the shared rate-limit allowance. Running a reprice
        # at the same time would compete for the same budget (and re-check the same
        # slots), so refuse while verify is live — the UI greys the Retry button to
        # match, and the verify pass covers these slots anyway.
        try:
            _vs = _eng.api_verify_pricing_status() or {}
        except Exception:
            _vs = {}
        if _vs.get("status") in ("running", "waiting", "paused"):
            return jsonify({"ok": False, "reason": "verify_active",
                            "error": "A pricing verification pass is running. It shares the "
                                     "Octopus rate-limit allowance and already re-checks these "
                                     "slots — please wait for it to finish, then retry if any "
                                     "still need it."}), 409
        body = request.get_json(force=True, silent=True) or {}
        from_date = (body.get("from_date") or "").strip() or None
        to_date = (body.get("to_date") or "").strip() or None
        preview = bool(body.get("preview"))
        # Guard: refuse a real reprice while the first-upgrade migration sweep is still working —
        # it would race the sweep over the same blocks (and spin up a second one via the post-fill
        # re-arm). Preview (count-only) is safe and allowed. The UI greys Retry to match.
        if not preview and _eng.reprice_history_in_progress():
            return jsonify({"ok": False, "reason": "migrating",
                            "error": "History is still migrating to the new pricing model. Please "
                                     "wait for it to finish, then retry any slots that still need "
                                     "it."}), 409
        # Date validation: end must not precede start.
        if from_date and to_date and to_date < from_date:
            return jsonify({"ok": False,
                            "error": "The 'to' date can't be before the 'from' date."}), 400
        # Range mode defaults to import-only (the tool's purpose); queue mode keeps
        # whatever was flagged. include_export opts back into export re-pricing.
        if from_date or to_date:
            channels = ("import", "export") if body.get("include_export") else ("import",)
        else:
            channels = ("import", "export")
        if not preview:
            _bk, _refuse = _safety_backup_or_refuse("pre-reprice-repair")  # #67
            if _refuse:
                return _refuse
        result = _run_on_engine_loop(
            _eng.repair_import_pricing(from_date, to_date, channels=channels,
                                       count_only=preview),
            timeout=(60.0 if preview else 1800.0))
        # G2: a real recovery invalidated those blocks' segments (block_store) — re-run the reprice
        # sweep so the segment surfaces reflect the fill immediately, not just after a restart.
        if not preview and (result.get("recovered") or 0) > 0:
            try:
                _run_on_engine_loop(_eng.run_reprice_history_sweep_to_done(), timeout=1800.0)
            except Exception as _e:
                logger.warning("reprice-repair: post-fill re-segment sweep failed: %s", _e)
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:
        logger.error("api_historical_reprice_repair: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/historical/retag-imports", methods=["POST"])
def api_historical_retag_imports():
    """Repair blocks the carbon round-trip bug wiped from 'imported_api' to NULL:
    re-tag reconstruction blocks (pre-go-live, no meter read) as imported_api.
    No re-import, no API cost — pure local repair. Returns {retagged, go_live}."""
    try:
        res = _engine_write(lambda st: st.retag_untagged_imports())
        try:
            _engine_write(_rebuild_config_period_chain)
        except Exception as _e:
            logger.warning("api_historical_retag_imports: chain rebuild failed: %s", _e)
        logger.info("api_historical_retag_imports: %s", res)
        return jsonify({"ok": True, **res})
    except Exception as e:
        logger.error("api_historical_retag_imports: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500



@app.route("/api/carbon/pause", methods=["GET", "POST"])
def api_carbon_pause():
    """Get or set the carbon-backfill kill switch. POST {"paused": true|false}.
    While paused, the historical backfill and live recovery both stand down."""
    try:
        store = store_access.store()
        if request.method == "POST":
            data = request.get_json(force=True, silent=True) or {}
            paused = bool(data.get("paused"))
            _engine_write(lambda st: st.set_meta(
                "carbon_paused", True if paused else None))   # A: writes go to the engine
            logger.info("api_carbon_pause: carbon backfill %s",
                        "PAUSED" if paused else "resumed")
            return jsonify({"ok": True, "paused": paused})
        return jsonify({"ok": True, "paused": bool(store.get_meta("carbon_paused", None))})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/historical/reprice-from-csv", methods=["POST"])
def api_historical_reprice_from_csv():
    """Overlay exact billed cost/rate onto imported blocks from an Octopus CSV
    export (the billing source of truth) — the only way to price dispatch slots
    Octopus's Measurements API returns with no cost. Body: {csv, channel?}."""
    try:
        store = store_access.store()      # reads only; the write goes through _engine_write
        data = request.get_json(force=True, silent=True) or {}
        csv_text = (data.get("csv") or data.get("import_csv") or "").strip()
        channel = data.get("channel") or "import"
        if not csv_text:
            return jsonify({"ok": False, "error": "no CSV supplied"}), 400
        try:
            import engine as _eng_g
            if _eng_g.reprice_history_in_progress():
                return jsonify({"ok": False, "reason": "migrating",
                                "error": "History is still migrating to the new pricing model. "
                                         "Please wait for it to finish, then fill from CSV."}), 409
        except Exception:
            pass
        _bk, _refuse = _safety_backup_or_refuse("pre-csv-reprice")        # #67
        if _refuse:
            return _refuse
        # On the engine thread. This endpoint already blocks the request for up to
        # 1800s on the sweep below, so the hop adds no new blocking — it just
        # stops the write going through the shared web connection, which four
        # waitress threads share.
        res = _engine_write(
            lambda st: st.reprice_imported_blocks_from_csv(csv_text, channel=channel),
            timeout=1800.0)
        # G2: re-segment the CSV-filled blocks (their stale segments were invalidated in the store).
        try:
            import engine as _eng_csv
            if _eng_csv.kraken_available():
                _run_on_engine_loop(_eng_csv.run_reprice_history_sweep_to_done(), timeout=1800.0)
        except Exception as _e:
            logger.warning("reprice-from-csv: post-fill re-segment sweep failed: %s", _e)
        return jsonify({"ok": True, **res})
    except Exception as e:
        logger.error("api_historical_reprice_from_csv: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/historical-import")
def historical_import_page():
    """Historical import wizard (3.5.0). Currently the CSV route: upload the
    Octopus per-channel export, review the cost-derived rates, confirm, import."""
    return render_template("historical_import.html", active="data_management")


def _csv_channel_texts(body):
    texts = {}
    for channel in ("import", "export"):
        t = body.get(channel + "_csv") or body.get(channel)
        if t:
            texts[channel] = t
    return texts


@app.route("/api/historical/bills/build", methods=["POST"])
def api_historical_bills_build():
    """Parse uploaded Octopus PDF bills → per-(MPAN, channel) CSV TEXT, ready to feed
    the normal CSV preview/apply. Multipart form: `files` (one or more PDFs, the
    user's local folder uploaded by the browser). Groups by import MPAN — a folder
    can span a house move — and each group yields import + export CSV plus a
    reconciliation report and any shape-approximation warnings.

    Strictly upstream of block-writing: nothing is imported here. The generated CSV
    is returned to the client for review, then flows through the existing CSV path."""
    try:
        import bill_parser as _bp
        if not _bp.pdf_support():
            return jsonify({"ok": False,
                            "error": "PDF support (pypdf) isn't available in this build."}), 400
        files = request.files.getlist("files") or []
        # Only consider PDFs (a folder upload may include other files).
        pdfs = [f for f in files if (f.filename or "").lower().endswith(".pdf")]
        if not pdfs:
            return jsonify({"ok": False, "error": "No PDF files were uploaded."}), 400

        parsed, failed = [], []
        for f in pdfs:
            try:
                b = _bp.parse_bill(f.read(), source_name=f.filename or "bill.pdf")
                parsed.append(b)
            except Exception as e:
                failed.append({"file": f.filename, "error": str(e)})

        groups = {}
        for b in parsed:
            groups.setdefault(b.mpan_import or "unknown", []).append(b)

        def _dedupe(rows):        # later bill wins on overlap / re-issue (§6)
            by = {}
            for r in rows:
                by[r["Start"]] = r
            return [by[k] for k in sorted(by)]

        out_groups = []
        for mpan, blist in sorted(groups.items()):
            imp_rows, exp_rows, days, periods, gwarn = [], [], [], [], []
            mpan_export = None
            for b in blist:
                mpan_export = mpan_export or b.mpan_export
                try:
                    rows = _bp.build_csv_rows(b)
                except Exception as _be:      # one odd bill mustn't 500 the batch
                    gwarn.append(f"{b.source}: could not build CSV rows ({_be}).")
                    logger.warning("bills_build: %s CSV build failed: %s", b.source, _be)
                    rows = {"import": [], "export": []}
                imp_rows += rows["import"]
                exp_rows += rows["export"]
                days += b.reconciliation.get("days", [])
                periods += b.reconciliation.get("import_periods", [])
                gwarn += b.warnings
            imp_rows, exp_rows = _dedupe(imp_rows), _dedupe(exp_rows)
            recon_ok = (all(d.get("ok") for d in days) and all(p.get("ok") for p in periods))
            out_groups.append({
                "mpan_import": mpan, "mpan_export": mpan_export,
                "sources": [b.source for b in blist],
                "import_csv": _bp.rows_to_csv(imp_rows) if imp_rows else "",
                "export_csv": _bp.rows_to_csv(exp_rows) if exp_rows else "",
                "import_rows": len(imp_rows), "export_rows": len(exp_rows),
                "reconciliation": {"ok": recon_ok, "days": days, "import_periods": periods},
                "warnings": gwarn,
            })

        warnings = []
        if len([g for g in groups if g != "unknown"]) > 1:
            warnings.append(
                "More than one electricity import MPAN was found across these bills "
                "(a house move). Each is a separate site — import them one MPAN at a time.")
        if failed:
            warnings.append(f"{len(failed)} file(s) could not be parsed: "
                            + ", ".join(x['file'] or '?' for x in failed))
        return jsonify({"ok": True, "files": len(pdfs), "groups": out_groups,
                        "failed": failed, "warnings": warnings})
    except Exception as e:
        logger.error("api_historical_bills_build: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/historical/csv/preview", methods=["POST"])
def api_historical_csv_preview():
    """Parse + derive rates from the Octopus CSV(s) WITHOUT writing — the wizard's
    review/confirm step. Pure (no backup, no blocks): shows per-period tiers, the
    cost-derived rates + confidence, off/peak split, and the reconciliation."""
    try:
        import csv_import as _ci
        body = request.get_json(force=True, silent=True) or {}
        texts = _csv_channel_texts(body)
        if not texts:
            return jsonify({"ok": False, "error": "no CSV provided"}), 400
        out = {"ok": True, "channels": {}}
        for channel, text in texts.items():
            parsed = _ci.parse_octopus_csv(text, channel)
            if not parsed["ok"]:
                out["channels"][channel] = {"ok": False, "errors": parsed["errors"]}
                continue
            deriv = _ci.derive_rates(parsed["blocks"])
            out["channels"][channel] = {
                "ok": True, "block_count": len(parsed["blocks"]),
                "parse_errors": parsed["errors"], "periods": deriv["periods"],
                "off_peak_kwh": deriv["off_peak_kwh"], "peak_kwh": deriv["peak_kwh"],
                "reconcile": _ci.reconcile(parsed["blocks"], deriv["flags"]),
            }
        return jsonify(out)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/historical/csv/apply", methods=["POST"])
def api_historical_csv_apply():
    """Back up, then write reconstructed imported_csv blocks from the CSV(s) with
    the user's confirmed rate overrides. Requires confirmed=true. Reversible via
    the reconstructed-history delete filter."""
    try:
        body = request.get_json(force=True, silent=True) or {}
        if not body.get("confirmed"):
            return jsonify({"ok": False, "error": "confirmation required"}), 400
        texts = _csv_channel_texts(body)
        if not texts:
            return jsonify({"ok": False, "error": "no CSV provided"}), 400
        meter_id = body.get("meter_id") or "electricity_main"
        overrides = body.get("overrides") or {}
        backup = None
        try:
            backup = os.path.basename(_create_backup_zip(label="pre-csv-import"))
        except Exception as be:
            logger.warning("csv apply: backup failed (continuing): %s", be)
        store = store_access.store()      # reads only; the writes go through _engine_write
        # Spans a previous API import proved the supplier does not cost. Inside those,
        # and ONLY there, a bill may replace what the API import left behind (usage x
        # published rate) instead of being skipped by first-man-wins. Live/settled
        # readings and user corrections are still protected inside them.
        try:
            import engine as _eng
            replace_ranges = _eng._uncostable_ranges("import")
        except Exception as _rr:
            logger.warning("csv apply: uncostable ranges unavailable (%s) — "
                           "first-man-wins everywhere", _rr)
            replace_ranges = []
        # On the engine thread. This endpoint already zips the whole database for
        # a pre-import backup before it gets here, so it is a long request either
        # way; the hop only stops the write going through the connection four
        # waitress threads share.
        result = _engine_write(
            lambda st: st.apply_csv_import(
                texts, meter_id=meter_id, overrides=overrides,
                replace_ranges=replace_ranges),
            timeout=1800.0)
        if result.get("blocks_replaced"):
            logger.info("csv apply: replaced %d block(s) inside %d uncostable tariff "
                        "period(s); %d protected (live or user-corrected)",
                        result.get("blocks_replaced"), len(replace_ranges),
                        result.get("blocks_protected") or 0)
        result["backup"] = backup
        # Offer a region/site confirmation for the imported span. CSV carries no
        # provenance, so the user names the site AND sets the region; skipping
        # leaves the data blended into whatever period already covers it (carbon
        # excluded). The panel lives on the Billing history page.
        try:
            span = result.get("span") or {}
            if span.get("from"):
                plan = store_access.store().plan_csv_reconciliation(
                    span["from"], span.get("to"))
                if plan.get("needs_confirmation"):
                    _engine_write(lambda st: st.set_meta("region_reconcile_pending", plan))
                    result["region_reconcile"] = True
        except Exception as _re:
            logger.warning("csv apply: region plan failed: %s", _re)
        # Rebuild the pre-rendered billing/usage charts so the imported span shows
        # up immediately — the API import path already does this; the CSV/bill path
        # previously left the charts stale until the next startup/live-block regen.
        return jsonify(result)
    except Exception as e:
        logger.error("api_historical_csv_apply: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


def _period_block_minutes_tz(store, from_iso):
    """(block_minutes, timezone) for the config period covering from_iso, with
    sane fallbacks — used to render template timestamps on the right grid/tz."""
    bm, tz = 30, "Europe/London"
    try:
        cp = store.get_config_period_for_date(from_iso) if from_iso else None
        if not cp:
            cp = store._conn.execute(
                "SELECT block_minutes, timezone FROM config_periods "
                "ORDER BY effective_from DESC LIMIT 1").fetchone()
        if cp:
            bm = int(cp["block_minutes"] or 30)
            tz = cp["timezone"] or "Europe/London"
    except Exception:
        pass
    return bm, tz


@app.route("/api/historical/gap-template")
def api_historical_gap_template():
    """Download a per-half-hour CSV pre-filled with Start/End for a gap, ready for
    the user to fill Consumption + Cost from their bill. Query: from, to (UTC ISO),
    channel (import|export, for the filename)."""
    try:
        from flask import Response
        import csv_import as _ci
        frm = request.args.get("from")
        to  = request.args.get("to")
        channel = (request.args.get("channel") or "import").strip().lower()
        inclusive = (request.args.get("inclusive") or "").lower() in ("1", "true", "yes")
        from_local = (request.args.get("from_local") or "").lower() in ("1", "true", "yes")
        to_local = (request.args.get("to_local") or "").lower() in ("1", "true", "yes")
        if not frm or not to:
            return jsonify({"error": "from and to are required"}), 400
        bm, tz = _period_block_minutes_tz(store_access.store(), frm)
        # A date-range picker sends LOCAL wall-clock (e.g. the picked day
        # 2026-08-01T00:00:00 meaning local midnight); the generator treats its
        # input as UTC, so in BST that renders the first row as 01:00+01:00 not
        # 00:00+01:00. Localise those boundaries to naive-UTC first so the grid
        # lands on local midnight. (Gap paths pass real UTC block_starts and set
        # neither flag, so they are unaffected.)
        if from_local or to_local:
            import block_store as _bs
            def _loc(s):
                s = str(s)
                return _bs.local_datetime_to_utc(s[:10], (s[11:16] or "00:00"), tz)
            try:
                if from_local:
                    frm = _loc(frm)
                if to_local:
                    to = _loc(to)
            except Exception:
                pass
        # A persisted gap's `to` is the LAST missing slot's start (inclusive); the
        # generator is half-open, so extend the end by one block to include it. A
        # localised end is already an exclusive next-midnight/end-of-day boundary,
        # so it needs no bump (bumping would spill one slot into the next day).
        if inclusive and not to_local:
            from datetime import datetime as _dt, timedelta as _td
            try:
                _t = _dt.fromisoformat(str(to).replace("Z", "").split("+")[0])
                to = (_t + _td(minutes=bm)).isoformat()
            except Exception:
                pass
        csv_text = _ci.gap_template_csv(frm, to, block_minutes=bm, tz_name=tz)
        fname = f"emt-gap-{channel}-{frm[:10]}_to_{to[:10]}.csv"
        return Response(csv_text, mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})
    except Exception as e:
        logger.error("api_historical_gap_template: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/historical/slots-template", methods=["POST"])
def api_historical_slots_template():
    """Pre-filled CSV for an ARBITRARY set of slots — the pricing-health escape
    hatch: hand the user exactly the half-hours the API couldn't price so they can
    fill them from their bill and run them through the CSV reprice path.
    Body: {starts: [naive-UTC iso...], channel}."""
    try:
        from flask import Response
        import csv_import as _ci
        body = request.get_json(silent=True) or {}
        starts = body.get("starts") or []
        channel = (body.get("channel") or "import").strip().lower()
        if not starts:
            return jsonify({"error": "no slots"}), 400
        bm, tz = _period_block_minutes_tz(store_access.store(), starts[0])
        csv_text = _ci.slots_template_csv(starts, block_minutes=bm, tz_name=tz)
        fname = f"emt-slots-{channel}-{len(starts)}.csv"
        return Response(csv_text, mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})
    except Exception as e:
        logger.error("api_historical_slots_template: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/historical/csv-template")
def api_historical_csv_template():
    """Download a small illustrative CSV template (headers + example rows in the
    exact time format) for users supplying their own consumption CSV."""
    try:
        from flask import Response
        import csv_import as _ci
        bm, _tz = _period_block_minutes_tz(store_access.store(), None)
        csv_text = _ci.blank_template_csv(block_minutes=bm)
        return Response(csv_text, mimetype="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="emt-consumption-template.csv"'})
    except Exception as e:
        logger.error("api_historical_csv_template: %s", e)
        return jsonify({"error": str(e)}), 500


_REPRICE_CAUSE_WINDOW_H = 12.0   # an import finishing this recently owns the sweep


def _reprice_sweep_cause(store) -> str:
    """Why is history being re-priced — 'import' or 'upgrade'?

    The sweep has two triggers and the banner used to name only one, so a FRESH INSTALL
    that imported its history was told EMT was "finishing your upgrade" — on a box that
    had never run a previous version, and had just been asked to do exactly this work.
    The verify pass runs the sweep forced after every import and gap-fill (P3.3d), so a
    live or recently-finished run is the cause; only in its absence is this an upgrade.
    Read-only and fail-safe: anything unexpected reads as 'upgrade', the old wording."""
    try:
        import engine as _eng
        if _eng.api_import_running():
            return "import"
        import json as _json
        from datetime import datetime as _dt, timedelta as _td
        raw = store.get_kraken_state(_eng._IMPORT_RUN_KEY)
        snap = _json.loads(raw) if raw else {}
        fin = snap.get("finished_at")
        if fin:
            age = _dt.utcnow() - _dt.fromisoformat(str(fin)[:19])
            if age < _td(hours=_REPRICE_CAUSE_WINDOW_H):
                return "import"
    except Exception:
        pass
    return "upgrade"


@app.route("/api/reprice-history-status")
def api_reprice_history_status():
    """Read-only: is the historical re-price sweep still working through a backlog? Drives the
    re-pricing banner. `in_progress` = not done AND blocks still need re-pricing. `cause` says
    whether an import or an upgrade armed it, so the banner can stop calling a fresh install's
    own import an upgrade. Fails safe to not-in-progress so a hiccup never nags."""
    try:
        import engine as _eng
        store = store_access.store()
        m = store.get_meta(_eng._REPRICE_HISTORY_MARKER, {}) or {}
        remaining = int(store.count_blocks_needing_reprice())
        done = bool(m.get("done"))
        _ip = (not done) and remaining > 0
        return jsonify({
            "done": done,
            "remaining": remaining,
            "swept": int(m.get("swept") or 0),
            "stalled": int(m.get("stalled") or 0),
            "in_progress": _ip,
            "cause": _reprice_sweep_cause(store) if _ip else None,
        })
    except Exception as e:
        logger.debug("api_reprice_history_status: %s", e)
        return jsonify({"in_progress": False, "remaining": 0, "done": True, "cause": None})


@app.route("/api/reprice-history-conformance")
def api_reprice_history_conformance():
    """P3.3c soak check (non-destructive): recompute split+exc for a UTC date range via the
    unified sweep core and diff against stored values. `verdict` is "clean" only with zero
    uncapped divergences. Params: from=YYYY-MM-DD, to=YYYY-MM-DD (UTC), meter_id (optional)."""
    try:
        import engine as _eng
        frm = request.args.get("from"); to = request.args.get("to")
        if not frm or not to:
            return jsonify({"ok": False, "reason": "from and to (YYYY-MM-DD) required"}), 400
        meter = request.args.get("meter_id", "electricity_main")
        # Build the full historical import rate segments ON THE ENGINE LOOP (the Kraken client is
        # bound to it) and hand them to the harness — a sync-thread build raises "attached to a
        # different loop". If it fails, the harness simply skips exc (exc_checked=False).
        rate_segs = None
        try:
            rate_segs = _run_on_engine_loop(_eng._build_channel_rate_segs("import"), timeout=60)
        except Exception as _rse:
            logger.warning("api_reprice_history_conformance: engine-loop rate-seg build failed: %s", _rse)
        rep = _eng.reprice_history_conformance(frm + "T00:00:00", to + "T00:00:00", meter,
                                               rate_segs=rate_segs)
        return jsonify(rep)
    except Exception as e:
        logger.exception("api_reprice_history_conformance failed")
        return jsonify({"ok": False, "error": str(e)}), 500


def _resolve_period(kind, store, cfg, tz, args):
    """Turn a period request into `(utc_start, utc_end, meta)`.

    ONE implementation of the three period kinds. The six
    `/api/{usage,insights}/{billing-period,calendar-month,calendar-year}` routes
    each carry their own copy — 83% identical between the usage and insights
    pair, 71% between the month and year pair — differing only in which period
    they resolve and which aggregator they then call. Those are two independent
    axes expressed as six endpoints.

    Returns `(None, None, error_dict)` when the period cannot be found, so the
    caller can answer 404 without this knowing about HTTP.
    """
    from datetime import datetime as _dt, timezone as _tzu, timedelta as _td
    import billing as _billing

    now_local = _dt.now(tz).replace(tzinfo=None)

    def _utc(d):
        if d.tzinfo is None:
            d = d.replace(tzinfo=tz)
        return d.astimezone(_tzu.utc).replace(tzinfo=None).isoformat()

    if kind == "billing":
        periods = _billing.get_billing_periods_from_config_periods(
            store.get_config_periods(), tz=tz)
        if not periods:
            return None, None, {"error": "No billing periods found"}
        requested = args.get("period_start")
        target = None
        for (ps, pe) in periods:
            if requested:
                if ps.date().isoformat() == requested:
                    target = (ps, pe)
                    break
            else:
                target = (ps, pe)        # no request → the LAST period, i.e. current
        if target is None:
            return None, None, {"error": "Billing period not found"}
        ps, pe = target
        return _utc(ps), _utc(pe), {
            "kind": "billing",
            "period_start": ps.date().isoformat(),
            "period_end": pe.date().isoformat(),
            "is_current": ps <= now_local < pe,
        }

    if kind in ("month", "quarter", "year"):
        year = int(args.get("year", now_local.year))
        if kind == "month":
            month = int(args.get("month", now_local.month))
        elif kind == "quarter":
            q = int(args.get("quarter", (now_local.month - 1) // 3 + 1))
            month = (q - 1) * 3 + 1
        else:
            month = 1
        first = _dt(year, month, 1)
        (ps, pe), = _billing.calendar_periods(kind, first, first)
        label = (ps.strftime("%B %Y") if kind == "month"
                 else "Q%d %d" % ((ps.month - 1) // 3 + 1, ps.year) if kind == "quarter"
                 else str(ps.year))
        return _utc(ps), _utc(pe), {
            "kind": kind,
            "period_start": ps.date().isoformat(),
            "period_end": (pe - _td(days=1)).date().isoformat(),
            "period_label": label,
            "is_current": ps <= now_local < pe,
        }

    if kind == "day":
        # One LOCAL day, `date=YYYY-MM-DD` (default today): local midnight to local
        # midnight, so a clock-change day spans 23 or 25 hours.
        from block_store import local_date_to_utc_bounds as _ldtub
        ds = (args.get("date") or now_local.date().isoformat()).strip()
        try:
            d = _dt.strptime(ds, "%Y-%m-%d").date()
        except ValueError:
            return None, None, {"error": "date must be YYYY-MM-DD"}
        us, ue = _ldtub(d.isoformat(), tz.key)
        return us, ue, {"kind": "day", "period_start": d.isoformat(),
                        "period_end": d.isoformat(), "period_label": d.isoformat(),
                        "is_current": d == now_local.date()}

    if kind == "range":
        # Local days `start`..`end` inclusive (YYYY-MM-DD) — a client re-fetching the days
        # whose fingerprint moved, not all of history (C, 2 Oct 2026).
        from block_store import local_date_range_to_utc_bounds as _lrb
        try:
            d0 = _dt.strptime((args.get("start") or "").strip(), "%Y-%m-%d").date()
            d1 = _dt.strptime((args.get("end") or "").strip(), "%Y-%m-%d").date()
        except ValueError:
            return None, None, {"error": "kind=range takes start and end, YYYY-MM-DD"}
        if d1 < d0:
            return None, None, {"error": "kind=range: end is before start"}
        us, ue = _lrb(d0.isoformat(), d1.isoformat(), tz.key)
        return us, ue, {"kind": "range", "period_start": d0.isoformat(),
                        "period_end": d1.isoformat(),
                        "period_label": "%s → %s" % (d0.isoformat(), d1.isoformat()),
                        "is_current": d0 <= now_local.date() <= d1}

    if kind == "all":
        # Every local day that holds a block — the Spiral's and Usage Stats' history.
        span = _all_history_utc(store, tz.key)
        if span is None:
            # 404, not 400: nothing is wrong with the request, there is just no data yet.
            # Usage Stats and Spiral draw an empty chart on a 404 and an error on a 400.
            return None, None, {"error": "No blocks found", "status": 404}
        us, ue = span
        _loc = lambda u: (_dt.fromisoformat(u).replace(tzinfo=_tzu.utc).astimezone(tz)).date()
        return us, ue, {"kind": "all", "period_start": _loc(us).isoformat(),
                        "period_end": (_loc(ue) - _td(days=1)).isoformat(),
                        "period_label": "All", "is_current": True}

    return None, None, {"error": "kind must be billing|month|quarter|year|day|all|range"}


# How much history one /api/energy request may cover (C §7.3, 5 Oct 2026). An unbounded
# range re-creates the wholesale problem at request time instead of write time. Set from what
# the pages ask for and what the Pi serves (soak 18, quiet phase):
#   block   31 local days — a month, the heatmap's viewport and the most any page asks; a month
#           of blocks is p50 0.32 s / p95 1.1 s. A year is ~15 MB raw (C §7.1).
#   period  366 days — a calendar year, the longest bill any page asks; p50 4.8 s.
#   day     no cap — one row per local day; all history is what Spiral / Usage Stats load
#           (p50 2.9 s), and its budget is asserted by the soak.
# Only kind=range (and kind=all, at period resolution) can exceed these; no page does. A client
# needing more pages through with kind=range.
_ENERGY_MAX_DAYS = {"block": 31, "period": 366}


def _energy_range_refusal(resolution, utc_start, utc_end, fields=""):
    """The refusal for a range longer than `resolution` serves, or None.

    Counted from the resolved UTC range, not the period's start/end labels: a billing
    period's `period_end` is the NEXT period's start (exclusive), a month's and a range's is
    their last day (inclusive). A clock-change day is 23 or 25 hours; rounding the span to
    whole days absorbs it."""
    from datetime import datetime as _dt
    cap = _ENERGY_MAX_DAYS.get(resolution)
    if cap is None or fields == "days":          # per-day fingerprints only — cheap
        return None
    try:
        span = _dt.fromisoformat(str(utc_end)) - _dt.fromisoformat(str(utc_start))
    except (TypeError, ValueError):
        return None
    days = round(span.total_seconds() / 86400)
    if days <= cap:
        return None
    return ("resolution=%s covers at most %d days and this request covers %d; ask for a "
            "shorter period, or page through with kind=range&start=&end=."
            % (resolution, cap, days))


@app.route("/api/energy")
def api_energy():
    """The consolidated read surface — C §3: `resolution=period|day|block`.

        GET /api/energy?resolution=period&kind=billing|month|quarter|year
                       &include=usage,carbon,billing
                       [&period_start=YYYY-MM-DD | &year=&month=&quarter=]

    Replaces six routes whose only differences are WHICH period and WHAT to
    aggregate — two independent axes previously expressed as six endpoints, at
    77-83% textual duplication between them.

    **`include=` rather than separate endpoints, because both aggregators scan
    the same blocks over the same range.** A page wanting cost and carbon makes
    one request and the store reads the range once; today it makes two and the
    range is read twice.

    **`resolution=day|block`** return per-day rollups (one row per LOCAL day per
    meter, `store.get_day_rollups`) and blocks-with-segments (`get_energy_blocks`,
    sub-meter rows first-class, provenance as three facts). *This docstring said
    they returned 501 until 30 Sep 2026, after they had been implemented; the
    first Pi soaks were set up from it and only ever asked for `period`.*
    `tariff_context` is not served yet (C §3: optional, omitted when absent).

    **`include=billing`** is the Billing tab's data (billing_view.py): at `period` the
    bill summary its card is formatted from, at `day` each local day's chart series
    (charged-rate lines) and side panel. Not at `block`.

    **`fields=`** asks for one part and nothing else: `fingerprint` (the change poll's
    global token), `days` (per-day fingerprints, with `since=` a `history` token for
    everything before), `stats` (the Usage Stats / Spiral rows, at `day` or `block`;
    #47, 2 Oct 2026).

    It replaced six routes: `/api/{usage,insights}/{billing-period,calendar-month,
    calendar-year}`, deleted with C step 2 (1 Oct 2026) once Insights read this.
    """
    try:
        from zoneinfo import ZoneInfo as _ZI

        resolution = (request.args.get("resolution") or "period").lower()
        if resolution not in ("period", "day", "block"):
            return jsonify({"error": "resolution must be period|day|block"}), 400

        kind = (request.args.get("kind") or "billing").lower()
        # `include` defaults to `usage` for a period request (that IS the answer)
        # and to NOTHING for day/block, where the rows are the answer and an
        # aggregate is an extra scan the caller did not ask for.
        _default_include = "usage" if resolution == "period" else ""
        include = {x.strip() for x in (request.args.get("include") or _default_include).split(",") if x.strip()}
        unknown = include - {"usage", "carbon", "billing", "stats", "settle", "gaps"}
        if unknown:
            return jsonify({"error": "include: unknown facet(s) %s" % sorted(unknown)}), 400
        if "billing" in include and resolution == "block":
            return jsonify({"error": "include=billing is served at resolution=period|day"}), 400
        if "settle" in include and resolution != "block":
            return jsonify({"error": "include=settle is served at resolution=block"}), 400
        if "gaps" in include and resolution != "block":
            return jsonify({"error": "include=gaps is served at resolution=block"}), 400
        fields = request.args.get("fields") or ""
        if ("stats" in include or fields == "stats") and resolution == "period":
            return jsonify({"error": "stats are served at resolution=day|block"}), 400
        if resolution == "block" and kind == "all":
            # A year of blocks is ~15 MB raw (C §7.1): blocks are fetched for a viewport.
            return jsonify({"error": "resolution=block takes a viewport: kind=day|month"}), 400

        store = store_access.store()
        if fields == "fingerprint":
            # The change poll's answer and nothing else — no period, no rows: one global
            # token over the blocks, segments, config and epoch (_blocks_data_version).
            return jsonify({"fingerprint": {"token": _blocks_data_version(store),
                                            "epoch": store.get_data_epoch(),
                                            "config": _config_token(store)},
                            # the running build, so an open page can tell it is stale
                            "build": BUILD_ID})
        cfg = load_config()
        # `_config_timezone()` already exists (server.py:292) and takes NO
        # arguments — it re-reads the config and, unlike a naive first-meter
        # loop, SKIPS SUB-METERS. A sub-meter-first config would otherwise hand
        # back a device's timezone as the site's.
        tz_name = _config_timezone()
        tz = _ZI(tz_name)

        utc_start, utc_end, meta = _resolve_period(kind, store, cfg, tz, request.args)
        if utc_start is None:
            status = meta.pop("status", None) or (
                404 if "not found" in str(meta.get("error", "")).lower() else 400)
            return jsonify(meta), status
        _too_long = _energy_range_refusal(resolution, utc_start, utc_end, fields)
        if _too_long:
            return jsonify({"error": _too_long}), 400

        if fields == "stats":
            # The Usage Stats / Spiral rows and nothing else (#47, 2 Oct 2026). With
            # `include=stats` the response also carried the day rollups and a fingerprint
            # per day over the whole range — for all history, 0.17 s of a 0.40 s load on
            # the dev database — and the Charts page read neither: it keeps its rows by
            # day and asks `fields=days` what moved.
            import usage_view
            return jsonify({"resolution": resolution, "timezone": tz_name, "period": meta,
                            "range": {"start_utc": utc_start, "end_utc": utc_end},
                            "stats": (usage_view.day_stats(store, cfg, tz_name, utc_start, utc_end)
                                      if resolution == "day"
                                      else usage_view.slot_stats(store, cfg, tz_name,
                                                                 utc_start, utc_end))})
        if fields == "days":
            # Which days moved, and nothing else — for a client that keeps rows by day and
            # re-fetches only those (C, 2 Oct 2026). With `since=YYYY-MM-DD` (a local day):
            # per-day fingerprints from that day's UTC start on, and ONE `history` token over
            # everything before it — all history per day was 0.42 s / 161 KB on 138k blocks.
            # Day keys are UTC dates (day_fingerprints).
            fs, hist = utc_start, None
            since = (request.args.get("since") or "").strip()
            if since:
                from block_store import local_date_to_utc_bounds as _ldtub2
                try:
                    _dt_since = __import__("datetime").date.fromisoformat(since)
                except ValueError:
                    return jsonify({"error": "since must be YYYY-MM-DD"}), 400
                fs = max(utc_start, _ldtub2(_dt_since.isoformat(), tz_name)[0])
                hist = store.history_token(fs)
            return jsonify({"period": meta, "range": {"start_utc": utc_start, "end_utc": utc_end},
                            "fingerprint": {"epoch": store.get_data_epoch(),
                                            "days": store.day_fingerprints(fs, utc_end),
                                            "history": hist,
                                            "config": _config_token(store)}})
        out = {"resolution": resolution, "timezone": tz_name, "period": meta,
               "range": {"start_utc": utc_start, "end_utc": utc_end},
               # C §2a. Per-day over the range plus the bulk epoch — NOT one
               # global token: `_blocks_data_version` is global, so a new
               # half-hour tells a client looking at 2023 to re-fetch four years.
               "fingerprint": {"epoch": store.get_data_epoch(),
                               "days": store.day_fingerprints(utc_start, utc_end),
                               # Billing periods, meters, bill rounding: what the
                               # per-day fingerprint cannot see (C step 1).
                               "config": _config_token(store)}}
        if resolution == "day":
            out["days"] = store.get_day_rollups(utc_start, utc_end, tz_name)
        elif resolution == "block":
            # Blocks and segments, not chart geometry (C §3). Sub-meter rows are
            # first-class: they carry device energy and have no segments, so a
            # client projecting devices from segments alone gets nothing.
            out["blocks"] = store.get_energy_blocks(utc_start, utc_end)
            out["segments"] = store.get_energy_segments(utc_start, utc_end)
            if "gaps" in include:
                # The main meter's tombstones over the range (heatmap issues design §2, §6 (c)): a
                # deleted half-hour is a gap like any other, and its tooltip says when.
                out["deleted"] = [
                    {"start_utc": r["start_utc"], "end_utc": r["end_utc"],
                     "created_at": r["created_at"], "reason": r["reason"]}
                    for r in store.get_deleted_ranges()
                    if r["meter_id"] in ("electricity_main", "*")
                    and r["start_utc"] < utc_end and r["end_utc"] > utc_start]
            if "settle" in include:
                # BL-85: each main-meter half-hour's bill and split state (settlement
                # surfaces design §1, §3), decided once here for every surface. Opt-in, so
                # the surfaces that do not draw it keep their query budget.
                out["settle"] = _settlement_states(store, utc_start, utc_end)

        if "usage" in include:
            out["usage"] = _aggregate_usage(store, cfg, utc_start, utc_end, tz_name)
            # The period's bill total is the SAME derivation a client makes from
            # resolution=day — per local day, penny-rounded — not
            # _aggregate_usage's once-over-the-period rounding, which was up to 3p
            # from v4's Billing card in 49 of 80 real periods (C acceptance 7).
            from billing import bill_net_from_day_rollups
            out["usage"]["net_cost"] = bill_net_from_day_rollups(
                store.get_day_rollups(utc_start, utc_end, tz_name))
        if "carbon" in include:
            # NB `_aggregate_insights` takes no tz — it aggregates over a UTC
            # range and emits nothing clock-local. `_aggregate_usage` does, for
            # its peak-demand window. Their signatures differ by one argument and
            # that difference is real, not an oversight to paper over.
            out["carbon"] = _aggregate_insights(store, cfg, utc_start, utc_end)
        if "stats" in include:
            # The Usage Stats / Spiral rows (usage_view): one per local day at `day`,
            # one per half-hour slot at `block`, with the meters to draw them with.
            import usage_view
            out["stats"] = (usage_view.day_stats(store, cfg, tz_name, utc_start, utc_end)
                            if resolution == "day"
                            else usage_view.slot_stats(store, cfg, tz_name, utc_start, utc_end))
        if "billing" in include:
            # The Billing tab's data (billing_view): at `period` the bill summary the
            # card is formatted from; at `day` each day's chart series and side panel.
            # The same functions the pre-rendered page calls, so the two cannot differ.
            import billing_view
            out["billing"] = (billing_view.period_billing(store, cfg, tz_name, utc_start, utc_end, meta)
                              if resolution == "period"
                              else billing_view.day_billing(store, cfg, tz_name, utc_start, utc_end))
        return jsonify(out)
    except Exception as e:
        logger.exception("api_energy failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/periods")
def api_periods():
    """Which periods can be asked about — C §3a.

    `/api/energy` answers "what happened between these two instants"; it cannot
    answer "which instants are worth asking about". That is a different question
    — config-period boundaries, calendar arithmetic, the earliest block — and
    folding it in would mean an endpoint that sometimes takes a range and
    sometimes returns the ranges available.

        GET /api/periods?kind=billing|month|quarter|year[&include=net_cost,carbon]

    Returns the boundaries and the data bounds, and NO aggregates unless asked
    (`include=`), and then never a query per period: one day-rollup pass over the
    whole span, folded per period by local day.
    `api_insights_periods`, which this replaced, ran one SQL query PER PERIOD to
    attach a carbon summary — thirty-six queries on a three-year account, to
    populate a picker. Totals are `/api/energy` with `resolution=period`, asked
    for the one period the user actually selects.

    Replaced `insights/periods` and `insights/data-bounds` (deleted with C step 2, 1 Oct
    2026). `include=carbon` is the Insights trend strip's per-period net carbon — what
    `insights/periods` gave, from the main meter's rollups, cut at LOCAL boundaries (it called
    `.astimezone()` on naive local datetimes, so on a host whose clock is UTC a BST
    period boundary landed an hour late).
    """
    try:
        from zoneinfo import ZoneInfo as _ZI
        from datetime import datetime as _dt, timezone as _tzu
        import billing as _billing

        kind = (request.args.get("kind") or "billing").lower()
        if kind not in ("billing", "month", "quarter", "year", "live"):
            return jsonify({"error": "kind must be billing|month|quarter|year|live"}), 400

        store = store_access.store()
        # Was an inline first-meter loop, which returns a SUB-METER's timezone on
        # a config that lists one first. `_config_timezone()` skips them.
        tz_name = _config_timezone()
        tz = _ZI(tz_name)
        bounds = store.get_block_bounds()

        keys = labels = None
        if kind == "live":
            # Live Power's three cards (C, 1 Oct 2026; were /api/billing): today, the
            # billing period holding today (#221's fallback when none does), the calendar
            # year — each to its own end; no block lies past now, so that is "to date".
            from datetime import timedelta as _td
            import usage_view as _uv
            _now = _dt.now(tz).replace(tzinfo=None)
            _today = _now.replace(hour=0, minute=0, second=0, microsecond=0)
            _bs, _be = _billing.current_billing_period(
                store.get_config_periods(), tz, _now, _uv._main_meta(load_config()).get("billing_day"))
            _ys = _today.replace(month=1, day=1)
            periods = [(_today, _today + _td(days=1)), (_bs, _be), (_ys, _ys.replace(year=_ys.year + 1))]
            keys = ["today", "bill", "year"]
            labels = [_today.strftime("%d %b %Y"),
                      "%s → %s" % (_bs.strftime("%d %b"), (_be - _td(days=1)).strftime("%d %b %Y")),
                      "1 Jan → %s" % _today.strftime("%d %b %Y")]
        elif kind == "billing":
            periods = _billing.get_billing_periods_from_config_periods(
                store.get_config_periods(), tz=tz)
        else:
            first = last = None
            if bounds.get("earliest") and bounds.get("latest"):
                first = _billing._parse_block_start(bounds["earliest"], tz).replace(tzinfo=None)
                last = _billing._parse_block_start(bounds["latest"], tz).replace(tzinfo=None)
            periods = _billing.calendar_periods(kind, first, last)

        now_local = _dt.now(tz).replace(tzinfo=None)

        def _utc(dt):
            """Period boundaries are LOCAL — a billing day is a wall-clock thing.
            The API speaks UTC, so both are returned and no consumer has to guess
            which it got."""
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            return dt.astimezone(_tzu.utc).replace(tzinfo=None).isoformat()

        out = [{"start": ps.date().isoformat(),
                "end": pe.date().isoformat(),
                "start_utc": _utc(ps),
                "end_utc": _utc(pe),
                "is_current": ps <= now_local < pe}
               for ps, pe in periods]
        if keys:
            for p, k, lb in zip(out, keys, labels):
                p["key"], p["label"] = k, lb

        # `include=net_cost`: each period's bill total and whether it holds any data,
        # for a picker that labels every period with its total (the Billing tab's).
        # ONE rollup query over the whole span, folded per period by local day with
        # the bill's own per-day rule — the rule the Billing card's Total Bill is
        # reconciled against (80 of 80 periods to the penny, .build/reconcile_v4.py).
        include = {x.strip() for x in (request.args.get("include") or "").split(",") if x.strip()}
        if include - {"net_cost", "carbon", "rows"}:
            return jsonify({"error": "include: unknown facet(s) %s"
                            % sorted(include - {"net_cost", "carbon", "rows"})}), 400
        if "rows" in include and kind != "live":
            return jsonify({"error": "include=rows is served at kind=live"}), 400
        # The span every period lies in — min/max, not first/last: kind=live's periods
        # (today, this bill, this year) are not in time order.
        span = ((min(p["start_utc"] for p in out), max(p["end_utc"] for p in out))
                if out else (None, None))
        rows = (store.get_day_rollups(span[0], span[1], tz_name)
                if (include - {"rows"}) and out else [])
        if "net_cost" in include and out:
            _mm = next(((m.get("meta") or {}) for m in (load_config().get("meters") or {}).values()
                        if not (m.get("meta") or {}).get("sub_meter")), {})
            currency = _mm.get("currency_symbol", "£")
            by_day = {}
            for r in rows:
                by_day.setdefault(r["day"], []).append(r)
            days = sorted(by_day)
            import bisect as _bisect
            for p in out:
                lo = _bisect.bisect_left(days, p["start"])
                hi = _bisect.bisect_left(days, p["end"])
                rs = [r for d in days[lo:hi] for r in by_day[d]]
                p["net_cost"] = _billing.bill_net_from_day_rollups(rs)
                p["net_cost_text"] = "%s%.2f" % (currency, p["net_cost"])   # as the picker prints it
                p["has_data"] = bool(rs)
        if "rows" in include and out:
            # Live Power's card rows per window, folded from ONE per-day pass
            # (get_day_live_rollups) — the rows /api/billing built with six queries per
            # window. The window's total is `net_cost` (include it): the bill's per-day
            # rule, which the rows, each rounded on its own, can miss by a penny.
            import bill_card as _bc
            import usage_view as _uv
            _cfg = load_config()
            _ev = _uv._configured_ev_meter_id(_cfg)
            live = store.get_day_live_rollups(span[0], span[1], tz_name, _ev)
            from datetime import date as _date, timedelta as _td2
            for p in out:
                d0, d1 = _date.fromisoformat(p["start"]), _date.fromisoformat(p["end"])
                ds = [(d0 + _td2(days=i)).isoformat() for i in range((d1 - d0).days)]
                p["rows"] = _bc.live_rows(live, ds, _cfg, _ev)
        if "carbon" in include and out:
            # Net carbon (g) per period, the main meter's: `has_carbon` from the count of
            # blocks carrying any — a period of real net zero is not "no data".
            import bisect as _bisect
            main = {}
            for r in rows:
                if r["meter_id"] == "electricity_main":
                    main[r["day"]] = r
            days = sorted(main)
            for p in out:
                rs = [main[d] for d in days[_bisect.bisect_left(days, p["start"]):
                                            _bisect.bisect_left(days, p["end"])]]
                p["has_carbon"] = any((r.get("carbon_blocks") or 0) > 0 for r in rs)
                p["carbon_g_net"] = round(sum(r["carbon_g"] or 0 for r in rs), 1) if rs else None

        return jsonify({
            "kind": kind,
            "timezone": tz_name,
            "periods": out,
            "bounds": {
                "earliest": (bounds.get("earliest") or "")[:10] or None,
                "latest": (bounds.get("latest") or "")[:10] or None,
                # the first and last main-meter blocks' starts, naive UTC: where the heatmap's
                # missing half-hours begin and end (heatmap issues design §2)
                "earliest_block": bounds.get("earliest") or None,
                "latest_block": bounds.get("latest") or None,
                # where Octopus's history starts, when it is older than EMT's oldest block: the
                # heatmap's "import your history" row — not a gap (gap fill design)
                "reach_from": _history_reach_from(bounds.get("earliest")),
                "carbon_earliest": (bounds.get("carbon_earliest") or "")[:10] or None,
                "carbon_latest": (bounds.get("carbon_latest") or "")[:10] or None,
            },
        })
    except Exception as e:
        logger.exception("api_periods failed")
        return jsonify({"error": str(e)}), 500



def _history_reach_from(earliest_block):
    """`engine.history_reach_from`, or None if it fails — the periods answer never does."""
    try:
        import engine as _eng
        return _eng.history_reach_from(earliest_block)
    except Exception as e:
        logger.debug("history_reach_from: %s", e)
        return None


def _collapse_rate_tiers(tiers_list):
    """Fold a serialised rate_tiers list to ONE cost-weighted-average entry when it
    exceeds the Billing view's _MAX_RATE_ROWS threshold — the same collapse
    bill_card.rate_rows / energy_charts._collapse_rate_kwh apply to Agile's ~48 rates a
    day, so Usage Insights and Billing fold identically (#371). The average rate is
    Σcost / Σkwh, so a net plunge-credit period reads NEGATIVE (sign preserved).
    `collapsed` + `n_rates` let the renderer label it 'avg of N rates'."""
    import energy_charts as _ec
    if len(tiers_list) <= _ec._MAX_RATE_ROWS:
        return tiers_list
    tot_kwh  = round(sum(t["kwh"]  for t in tiers_list), 3)
    tot_cost = round(sum(t["cost"] for t in tiers_list), 4)
    tot_blk  = sum(t.get("blocks", 0) for t in tiers_list)
    avg = round(tot_cost / tot_kwh, 6) if tot_kwh else 0.0
    return [{"rate": avg, "kwh": tot_kwh, "cost": tot_cost, "blocks": tot_blk,
             "collapsed": True, "n_rates": len(tiers_list)}]


def _aggregate_insights(store, cfg, utc_start: str, utc_end: str) -> dict:
    """
    Core aggregation for Insights — works for any UTC time range.
    Uses a direct lightweight SQL query rather than get_blocks_for_range
    to avoid fetching unnecessary columns and joining reads.
    """
    from datetime import datetime as _dt

    meters_cfg = cfg.get("meters") or {}
    sub_meter_ids = {
        mid for mid, md in meters_cfg.items()
        if (md.get("meta") or {}).get("sub_meter")
    }

    def _meter_type(mid, md):
        explicit = (md.get("meta") or {}).get("meter_type", "")
        if explicit:
            return explicit
        mid_l = mid.lower()
        if any(k in mid_l for k in ("ev", "charger")):      return "ev_charger"
        if any(k in mid_l for k in ("battery", "batt")):    return "battery"
        if any(k in mid_l for k in ("heat", "pump")):       return "heat_pump"
        if any(k in mid_l for k in ("solar", "pv", "inv")): return "inverter"
        return "unknown"

    # Lightweight direct query — only columns needed for carbon insights
    rows = store._conn.execute(
        """SELECT b.meter_id, b.imp_kwh, b.exp_kwh, b.carbon_g,
                  COALESCE(m.is_sub_meter, 0) as is_sub_meter
           FROM blocks b
           LEFT JOIN meters m ON m.meter_id = b.meter_id
                              AND m.config_period_id = b.config_period_id
           WHERE b.block_start >= ? AND b.block_start < ?
           ORDER BY b.block_start""",
        (utc_start, utc_end)
    ).fetchall()

    block_minutes = 30
    if meters_cfg:
        first_mid = next(iter(meters_cfg))
        block_minutes = int((meters_cfg[first_mid].get("meta") or {}).get("block_minutes", 30) or 30)

    main_imp_kwh = main_exp_kwh = 0.0
    main_ci_imp_kwh = main_ci_exp_kwh = 0.0
    cg_imp = cg_exp = cg_net = 0.0
    ci_kwh_weighted = ci_duration = 0.0
    eff_carbon = eff_kwh = 0.0
    has_carbon = False
    total_blocks = 0
    ci_blocks = 0

    sub_totals = {}

    for row in rows:
        mid        = row["meter_id"]
        b_imp_raw  = float(row["imp_kwh"] or 0)
        is_sub     = bool(row["is_sub_meter"]) or mid in sub_meter_ids
        b_imp      = b_imp_raw
        b_exp      = float(row["exp_kwh"] or 0)
        b_net      = b_imp - b_exp
        cg         = row["carbon_g"]

        if is_sub:
            if mid not in sub_totals:
                _m_meta = (meters_cfg.get(mid, {}).get("meta") or {})
                sub_totals[mid] = {
                    "imp_kwh": 0.0, "carbon_g": 0.0, "ci_blocks": 0,
                    "ci_imp_kwh": 0.0, "total_blocks": 0,
                    "meter_type": _meter_type(mid, meters_cfg.get(mid, {})),
                    # inverter_possible removed — deprecated in 2.9.0
                    "ci_kwh_weighted": 0.0, "ci_kwh_duration": 0.0,
                }
            sub_totals[mid]["imp_kwh"]      += b_imp
            sub_totals[mid]["total_blocks"] += 1
            if cg is not None:
                cg_f = float(cg)
                sub_totals[mid]["carbon_g"]   += cg_f
                sub_totals[mid]["ci_blocks"]  += 1
                sub_totals[mid]["ci_imp_kwh"] += b_imp
                if b_imp > 0.001:
                    b_intensity = abs(cg_f / b_imp)
                    sub_totals[mid]["ci_kwh_weighted"] += b_intensity * b_imp
                    sub_totals[mid]["ci_kwh_duration"] += b_imp
            continue

        # Main meter
        total_blocks += 1
        main_imp_kwh += b_imp
        main_exp_kwh += b_exp

        if cg is None:
            continue

        cg_f = float(cg)
        has_carbon = True
        ci_blocks += 1
        cg_net += cg_f
        main_ci_imp_kwh += b_imp
        main_ci_exp_kwh += b_exp

        if b_net != 0:
            b_intensity = abs(cg_f / b_net)
            cg_imp += b_imp * b_intensity
            cg_exp += b_exp * b_intensity
            ci_kwh_weighted += b_intensity * block_minutes
            ci_duration     += block_minutes
            eff_carbon += abs(cg_f)
            eff_kwh    += abs(b_net)
        elif b_imp > 0:
            cg_imp += abs(cg_f)
        else:
            cg_exp += abs(cg_f)

    effective_intensity  = round(eff_carbon / eff_kwh, 1) if eff_kwh > 0 else None
    grid_avg_intensity   = round(ci_kwh_weighted / ci_duration, 1) if ci_duration > 0 else None
    avg_export_intensity = round(cg_exp / main_ci_exp_kwh, 1) if main_ci_exp_kwh > 0 else None

    for mid, st in sub_totals.items():
        if st["ci_kwh_duration"] > 0:
            st["avg_charge_intensity"] = round(st["ci_kwh_weighted"] / st["ci_kwh_duration"], 1)
        else:
            st["avg_charge_intensity"] = None
        del st["ci_kwh_weighted"]
        del st["ci_kwh_duration"]

    # ── EV carbon (synthetic dispatch, single-source) ─────────────────────────
    # 4.4.0 Δ5-wire: the EV carbon axis comes from the SYNTHETIC dispatch device, not the
    # physical charger (mirrors the cost side's single-source rule). The credit is the draw
    # gap — the car's full completed-dispatch draw minus the grid-clipped billing kWh
    # (ev_split: the EV segments; imp_kwh_ev until step 5) — valued at the block's grid
    # intensity (carbon.py; price never enters).
    # ADDITIVE: a new `ev_carbon` key only; every existing field above is byte-identical.
    # Non-IOG (no completed dispatch in range) ⇒ zeros ⇒ the template shows nothing.
    ev_carbon_block = None
    try:
        import carbon as _carbon
        _unclipped = {}
        for _r in store._conn.execute(
                "SELECT slot_start, energy_kwh FROM dispatch_history "
                "WHERE kind='completed' AND slot_start >= ? AND slot_start < ?",
                (utc_start, utc_end)):
            _e = _r["energy_kwh"]
            if _e is None:
                continue
            _v = abs(float(_e))
            if _v > 1e-9:
                _unclipped[_r["slot_start"]] = _unclipped.get(_r["slot_start"], 0.0) + _v
        if _unclipped:
            import ev_split as _evs
            _ev_rows = store._conn.execute(
                "SELECT b.block_start, " + _evs.sql_kwh("b") + " AS ev_kwh, "
                "b.carbon_intensity_g FROM blocks b WHERE b.meter_id='electricity_main' "
                "AND b.block_start >= ? AND b.block_start < ?",
                (utc_start, utc_end)).fetchall()
            _agg = _carbon.period_ev_saving(
                ((r["block_start"], r["ev_kwh"], r["carbon_intensity_g"]) for r in _ev_rows
                 if r["ev_kwh"] is not None and r["ev_kwh"] > 0),
                _unclipped)
            if _agg["clipped_kwh"] > 0:
                ev_carbon_block = {
                    "source":        "synthetic_dispatch",
                    "grid_g":        _agg["grid_g"],
                    "saving_g":      _agg["saving_g"],
                    "gross_g":       _agg["gross_g"],
                    "clipped_kwh":   round(_agg["clipped_kwh"], 3),
                    "credit_blocks": _agg["credit_blocks"],
                }
    except Exception:
        logger.debug("insights: synthetic EV carbon unavailable", exc_info=True)

    # SINGLE-SOURCE EV on IOG (watch #8 — the carbon twin of P4.15): when the synthetic dispatch
    # EV carbon exists, IT (not the physical charger) is authority for the carbon EV axis. Drop the
    # physical EV device from the breakdown (superseded — its metered carbon can diverge wildly from
    # the dispatch truth: the fixture reads 3129 kg physical vs 141 kg dispatch), net the SYNTHETIC
    # EV GRID carbon/kWh from the house remainder (the behind-meter saving never crossed the grid, so
    # it is NOT subtracted), and keep non-EV sub-meters. Non-IOG (no dispatch) ⇒ byte-identical.
    _rec_ev_kwh = _rec_ev_carbon = _rec_ev_ci_kwh = 0.0
    if ev_carbon_block:
        # H4 HYBRID: before dropping the physical EV sub-meter, fold in its PRE-SEAM portion
        # (blocks with no dispatch split — ev_split's NULL) so a window straddling the seam
        # counts RECORDED EV pre-seam + SYNTHETIC EV post-seam, not synthetic only. Post-seam-only
        # windows and non-IOG accounts return 0 here → byte-identical.
        _ev_mid = _configured_ev_meter_id(cfg)
        if _ev_mid:
            try:
                import ev_split as _evs
                _pr = store._conn.execute(
                    "SELECT COALESCE(SUM(sub.imp_kwh),0.0) k, "
                    "       COALESCE(SUM(sub.carbon_g),0.0) g, "
                    "       COALESCE(SUM(CASE WHEN sub.carbon_g IS NOT NULL THEN sub.imp_kwh ELSE 0 END),0.0) ck "
                    "FROM blocks sub JOIN blocks main "
                    "  ON main.block_start=sub.block_start AND main.meter_id='electricity_main' "
                    "  AND main.config_period_id=sub.config_period_id "
                    "WHERE sub.meter_id=? AND " + _evs.sql_kwh("main") + " IS NULL "
                    "  AND sub.block_start>=? AND sub.block_start<?",
                    (_ev_mid, utc_start, utc_end)).fetchone()
                _rec_ev_kwh    = float(_pr["k"]  or 0.0)
                _rec_ev_carbon = float(_pr["g"]  or 0.0)
                _rec_ev_ci_kwh = float(_pr["ck"] or 0.0)
            except Exception:
                logger.debug("insights: recorded pre-seam EV carbon unavailable", exc_info=True)
        for _m in [mid for mid, st in list(sub_totals.items())
                   if (st.get("meter_type") or "") in ("ev", "ev_charger")
                   or any(k in mid.lower() for k in ("ev", "charger"))]:
            sub_totals.pop(_m, None)
    total_sub_imp_kwh  = sum(st["imp_kwh"]   for st in sub_totals.values())
    total_sub_carbon_g = sum(st["carbon_g"]  for st in sub_totals.values())
    _syn_ev_kwh    = ev_carbon_block["clipped_kwh"] if ev_carbon_block else 0.0
    _syn_ev_carbon = ev_carbon_block["grid_g"]      if ev_carbon_block else 0.0
    _ev_kwh_total    = _syn_ev_kwh + _rec_ev_kwh          # hybrid EV axis (synthetic + recorded)
    _ev_carbon_total = _syn_ev_carbon + _rec_ev_carbon
    if ev_carbon_block:                                   # surface the hybrid split (additive keys)
        ev_carbon_block["recorded_kwh"] = round(_rec_ev_kwh, 3)
        ev_carbon_block["recorded_g"]   = round(_rec_ev_carbon, 1)
        ev_carbon_block["ev_kwh_total"] = round(_ev_kwh_total, 3)
        ev_carbon_block["grid_g_total"] = round(_ev_carbon_total, 1)
    house_imp_kwh    = max(0.0, main_imp_kwh - total_sub_imp_kwh - _ev_kwh_total)
    house_ci_imp_kwh = max(0.0, main_ci_imp_kwh - sum(
        st.get("ci_imp_kwh", 0.0) for st in sub_totals.values()) - _syn_ev_kwh - _rec_ev_ci_kwh)
    house_carbon_g   = max(0.0, cg_imp - total_sub_carbon_g - _ev_carbon_total) if has_carbon else None
    house_avg_intensity = round(house_carbon_g / house_ci_imp_kwh, 1) \
        if house_carbon_g and house_ci_imp_kwh > 0 else None

    settings = store.get_settings()
    merged   = dict(SETTINGS_DEFAULTS)
    merged.update(settings)

    coverage_pct = round(ci_blocks / total_blocks * 100, 1) if total_blocks else 0

    # Actual first block date within range — main meter and per sub-meter
    first_block_row = store._conn.execute(
        "SELECT MIN(block_start) as first_block FROM blocks "
        "WHERE meter_id = 'electricity_main' AND block_start >= ? AND block_start < ?",
        (utc_start, utc_end)
    ).fetchone()
    data_start = first_block_row["first_block"][:10] if first_block_row and first_block_row["first_block"] else None

    for mid in sub_totals:
        row = store._conn.execute(
            "SELECT MIN(block_start) as first_block FROM blocks "
            "WHERE meter_id = ? AND block_start >= ? AND block_start < ?",
            (mid, utc_start, utc_end)
        ).fetchone()
        sub_totals[mid]["data_start"] = row["first_block"][:10] if row and row["first_block"] else data_start

    # Generation mix — imp_kwh-weighted average for this period
    generation_mix = []
    try:
        generation_mix = store.get_generation_mix_for_range(utc_start, utc_end)
    except Exception:
        pass


    return {
        "has_carbon":           has_carbon,
        "carbon_coverage_pct":  coverage_pct,
        "data_start":           data_start,
        "generation_mix":       generation_mix,
        "ev_carbon":            ev_carbon_block,
        "imp_kwh":              round(main_imp_kwh, 3),
        "exp_kwh":              round(main_exp_kwh, 3),
        "ci_imp_kwh":           round(main_ci_imp_kwh, 3),
        "ci_exp_kwh":           round(main_ci_exp_kwh, 3),
        "carbon_g_imp":         round(cg_imp, 1) if has_carbon else None,
        "carbon_g_exp":         round(cg_exp, 1) if has_carbon else None,
        "carbon_g_net":         round(cg_net, 1) if has_carbon else None,
        "effective_intensity":  effective_intensity,
        "avg_export_intensity": avg_export_intensity,
        "grid_avg_intensity":   grid_avg_intensity,
        "sub_meters":           sub_totals,
        "house_imp_kwh":        round(house_imp_kwh, 3),
        "house_ci_imp_kwh":     round(house_ci_imp_kwh, 3),
        "house_carbon_g":       round(house_carbon_g, 1) if house_carbon_g is not None else None,
        "house_avg_intensity":  house_avg_intensity,
        "assumptions":          merged,
    }



# ─────────────────────────────────────────────────────────────────────────────
# Usage Insights — aggregation + endpoints
# ─────────────────────────────────────────────────────────────────────────────

def _aggregate_usage(store, cfg, utc_start: str, utc_end: str,
                     tz_name: str) -> dict:
    """
    Core aggregation for Usage Insights — works for any UTC time range.
    Returns cost, earnings, rate-tier distribution, self-sufficiency,
    peak demand window, and per-device breakdowns.
    All monetary values in the configured currency.
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    from collections import defaultdict

    try:
        tz = _ZI(tz_name)
    except Exception:
        tz = _ZI("UTC")

    meters_cfg  = cfg.get("meters") or {}
    sub_meter_ids = {
        mid for mid, md in meters_cfg.items()
        if (md.get("meta") or {}).get("sub_meter")
    }

    # Fetch all columns needed for usage insights in one query
    rows = store._conn.execute(
        """SELECT b.meter_id, b.block_start,
                  b.imp_kwh, b.imp_kwh_grid,
                  b.imp_rate, b.imp_cost,
                  b.exp_kwh, b.exp_rate, b.exp_cost,
                  b.standing_charge,
                  COALESCE(m.is_sub_meter, 0) as is_sub_meter,
                  m.meter_type
           FROM blocks b
           LEFT JOIN meters m ON m.meter_id = b.meter_id
                              AND m.config_period_id = b.config_period_id
           WHERE b.block_start >= ? AND b.block_start < ?
           ORDER BY b.block_start""",
        (utc_start, utc_end)
    ).fetchall()

    # Each block's devices (grid kWh, £), for its Direct import by direct_import's rule (step 5,
    # 8 Oct 2026). The house was the stored remainder in kWh and a pro-rata share of the main's
    # cost in £ — a sixth way of working Direct out, and one that read a stale remainder as truth.
    import direct_import as _direct
    _dev_sum: dict = {}
    for _r in rows:
        if bool(_r["is_sub_meter"]) or _r["meter_id"] in sub_meter_ids:
            _g = _r["imp_kwh_grid"]
            _d = _dev_sum.setdefault(_r["block_start"], [0.0, 0.0])
            _d[0] += float(_g) if _g is not None else float(_r["imp_kwh"] or 0)
            _d[1] += float(_r["imp_cost"] or 0)

    # ── Accumulators ──────────────────────────────────────────────────────────
    main_imp_kwh   = 0.0   # total grid import
    main_exp_kwh   = 0.0   # total export
    main_imp_cost  = 0.0   # total import cost
    main_exp_cost  = 0.0   # total export earnings
    house_imp_kwh  = 0.0   # import remainder (house only, excl sub-meters)
    house_imp_cost = 0.0   # cost of house-only import

    # Standing charge — sum once per local day
    daily_sc: dict = {}    # date_str → max sc value

    # Rate tier distribution {rate → {kwh, cost, blocks}}
    rate_tiers: dict = defaultdict(lambda: {"kwh": 0.0, "cost": 0.0, "blocks": 0})

    # Peak demand window — 2hr buckets (0–23) by hour, accumulate house imp_kwh
    hour_kwh: dict = defaultdict(float)   # hour (0-23) → house import kWh

    total_blocks = 0

    # Sub-meter accumulators
    sub_totals: dict = {}

    # BL-27: per-block device grid draw, so a post-pass can re-price each physical
    # device from the main block's priced SEGMENTS (EV charger on the EV-attributed
    # segment cost, house devices at the house-band rate) instead of the parent
    # blended rate. block_start → [(meter_id, meter_type, grid_kwh)].
    _dev_by_block: dict = defaultdict(list)

    for row in rows:
        mid     = row["meter_id"]
        bs      = row["block_start"]
        is_sub  = bool(row["is_sub_meter"]) or mid in sub_meter_ids
        imp     = float(row["imp_kwh"] or 0)
        exp     = float(row["exp_kwh"] or 0)
        imp_cost = float(row["imp_cost"] or 0)
        exp_cost = float(row["exp_cost"] or 0)
        rate     = float(row["imp_rate"] or 0)
        sc       = float(row["standing_charge"] or 0)

        if is_sub:
            if mid not in sub_totals:
                _meta = (meters_cfg.get(mid, {}).get("meta") or {})
                sub_totals[mid] = {
                    "imp_kwh":   0.0,
                    "imp_cost":  0.0,
                    "exp_kwh":   0.0,
                    "label":     _meta.get("device") or mid,
                    "meter_type": (row["meter_type"] or "").lower(),
                    # rate tier breakdown per sub-meter
                    "rate_tiers": defaultdict(lambda: {"kwh": 0.0, "cost": 0.0, "blocks": 0}),
                }
            # Use imp_kwh_grid for sub-meters (grid portion only). Distinguish a
            # real 0 (device charged entirely from battery/solar this block — no
            # grid) from NULL (not computed): `or imp` would treat 0 as missing and
            # fall back to TOTAL consumption, over-stating the device (bites the
            # battery hardest). Mirrors the remainder handling below (`is not None`).
            _grid = row["imp_kwh_grid"]
            sub_grid_kwh = float(_grid) if _grid is not None else imp
            _dev_by_block[bs].append(
                (mid, (row["meter_type"] or "").lower(), sub_grid_kwh))
            sub_totals[mid]["imp_kwh"]  += sub_grid_kwh
            sub_totals[mid]["imp_cost"] += imp_cost
            sub_totals[mid]["exp_kwh"]  += exp
            if rate != 0:      # keep plunge-price (negative-rate) slots too (#371)
                sub_totals[mid]["rate_tiers"][round(rate, 6)]["kwh"]    += sub_grid_kwh
                sub_totals[mid]["rate_tiers"][round(rate, 6)]["cost"]   += imp_cost
                sub_totals[mid]["rate_tiers"][round(rate, 6)]["blocks"] += 1
            continue

        # ── Main meter ────────────────────────────────────────────────────────
        total_blocks += 1
        main_imp_kwh  += imp
        main_exp_kwh  += exp
        main_imp_cost += imp_cost
        main_exp_cost += exp_cost

        # House-only import: direct_import's rule on this half-hour (main less its devices).
        _dk, _dc = _dev_sum.get(bs, (0.0, 0.0))
        h_imp, h_cost = _direct.direct(imp, imp_cost, _dk, _dc)
        house_imp_kwh  += h_imp
        house_imp_cost += h_cost

        # Standing charge — start-of-day value. Take the earliest block's
        # charge, but don't let a leading zero (e.g. an early gap-filled block)
        # shadow the real value: only set once with the first NON-zero seen,
        # falling back to 0 only if the whole day is zero. Not MAX.
        try:
            local_d = (_dt.fromisoformat(bs)
                       .replace(tzinfo=_ZI("UTC"))
                       .astimezone(tz)
                       .strftime("%Y-%m-%d"))
        except Exception:
            local_d = bs[:10]
        if sc > 0:
            if not daily_sc.get(local_d):      # unset or still 0
                daily_sc[local_d] = sc
        elif local_d not in daily_sc:
            daily_sc[local_d] = 0.0

        # Rate tier accumulation (main meter total) — only when actually importing.
        # `rate != 0` (not `> 0`) so Agile plunge-price slots (negative rate — a
        # CREDIT) stay in the distribution instead of being silently dropped (#371);
        # a rate of exactly 0 still means unpriced/gap-filled and is excluded.
        if imp > 0 and rate != 0:
            rate_key = round(rate, 6)
            rate_tiers[rate_key]["kwh"]    += imp
            rate_tiers[rate_key]["cost"]   += imp_cost
            rate_tiers[rate_key]["blocks"] += 1

        # Peak demand window — local hour bucket
        try:
            local_hour = (_dt.fromisoformat(bs)
                          .replace(tzinfo=_ZI("UTC"))
                          .astimezone(tz)
                          .hour)
            hour_kwh[local_hour] += h_imp
        except Exception:
            pass

    # ── BL-27: re-price physical devices from the block SEGMENTS ─────────────────
    # When every main import block in the range carries priced segments (i.e. the
    # account is fully backfilled), the per-device headline kWh/cost and the house
    # remainder are recomputed with `price_devices_hybrid`: each device's METERED grid
    # kWh is valued at the block's band rate for its attribution. On a single-rate
    # (uncapped-equivalent) block every band rate equals the block rate, so a device
    # costs metered × block_rate — byte-identical to its stored cost column, with no
    # dispatch-maturity divergence and nothing leaking into house. On a multi-rate
    # (capped / boundary) block an EV device is valued at the EV band rate and a house
    # device at the house band rate — the 4-rate fix the parent blended rate got wrong.
    # Gated + fallback: any block missing a segment keeps the column figures.
    try:
        _missing = store._conn.execute(
            "SELECT COUNT(*) n FROM blocks b "
            "JOIN meters m ON m.meter_id=b.meter_id AND m.config_period_id=b.config_period_id "
            "WHERE b.block_start>=? AND b.block_start<? AND COALESCE(m.is_sub_meter,0)=0 "
            "AND b.imp_kwh>0 AND NOT EXISTS (SELECT 1 FROM block_segments s "
            "WHERE s.block_start=b.block_start AND s.meter_id=b.meter_id AND s.channel='import')",
            (utc_start, utc_end)).fetchone()["n"]
        _seg_rows = [] if _missing else store._conn.execute(
            "SELECT block_start, kwh, inc_rate, exc_rate, band, attribution "
            "FROM block_segments WHERE channel='import' AND block_start>=? AND block_start<? "
            "ORDER BY block_start, seq", (utc_start, utc_end)).fetchall()
    except Exception:
        _seg_rows = []
    if _seg_rows:
        import pricing_segments as _ps
        _segs_by_block: dict = defaultdict(list)
        for _r in _seg_rows:
            _segs_by_block[_r["block_start"]].append(_ps.Segment(
                _r["kwh"], _r["inc_rate"], _r["exc_rate"], _r["band"], _r["attribution"]))

        def _is_ev_dev(mid, mt):
            return mt in ("ev", "ev_charger") or any(
                k in (mid or "").lower() for k in ("ev", "charger"))

        # HYBRID EV AUTHORITY (H3, per-block, one 'EV' identity — id 'ev_dispatch'):
        #   * block HAS a dispatch 'ev' SEGMENT → SYNTHETIC is authority (Octopus's smart-charge
        #     energy + what they cost); the physical EV sub-meter is superseded for that block.
        #   * block has NO dispatch segment but a physical EV meter recorded it (pre-seam / HA
        #     recorder) → that RECORDED draw is the EV, priced at the block band (not folded to
        #     house). Both feed the SAME ev_dispatch row, so the seam is invisible and continuous.
        # A physical EV clamp's excess over dispatch (granny top-up in a dispatched block) still
        # lands in the house remainder. An account with NO physical EV meter keeps non-dispatch
        # charging in house by construction (nothing recorded it). Non-EV sub-meters (heat pump,
        # …) keep their metered kWh, priced at the house band.
        # Tariff-gated for free: a block has an 'ev' segment only while on a dispatch tariff, so
        # off-tariff blocks fall back to the sensor via the column path (BL-27 not entered).
        _ev_seg_k_by_block: dict = defaultdict(float)
        for _r in _seg_rows:
            if _r["attribution"] == "ev":
                _ev_seg_k_by_block[_r["block_start"]] += float(_r["kwh"] or 0.0)
        _EV_SYN_ID = "ev_dispatch"
        _phys_ev_ids: set = set()
        _seg_dev: dict = {}
        _seg_house_k = _seg_house_c = 0.0
        _syn_ev_k = _syn_ev_c = 0.0
        for _bs, _segs in _segs_by_block.items():
            _devs = []
            _phys_ev_grid = 0.0                    # H3: this block's recorded physical EV draw
            for (_m, _mt, _gk) in _dev_by_block.get(_bs, []):
                if _is_ev_dev(_m, _mt):
                    _phys_ev_ids.add(_m)          # folded into the one hybrid EV row below
                    _phys_ev_grid += float(_gk or 0.0)
                    continue
                _devs.append({"meter_id": _m, "attribution": "house", "grid_kwh": _gk})
            _evk = _ev_seg_k_by_block.get(_bs, 0.0)
            if _evk > 1e-9:                        # SYNTHETIC (dispatch) is authority for this block
                _devs.append({"meter_id": _EV_SYN_ID, "attribution": "ev", "grid_kwh": _evk})
            elif _phys_ev_grid > 1e-9:            # H3 hybrid: pre-seam / no dispatch → RECORDED physical EV
                _devs.append({"meter_id": _EV_SYN_ID, "attribution": "ev", "grid_kwh": _phys_ev_grid})
            _res = _ps.price_devices_hybrid(_segs, _devs)
            for _m, _v in _res["devices"].items():
                if _m == _EV_SYN_ID:
                    _syn_ev_k += _v["kwh"]; _syn_ev_c += _v["cost"]
                else:
                    _d = _seg_dev.setdefault(_m, {"kwh": 0.0, "cost": 0.0})
                    _d["kwh"] += _v["kwh"]; _d["cost"] += _v["cost"]
            _seg_house_k += _res["remainder"]["kwh"]
            _seg_house_c += _res["remainder"]["cost"]
        # Non-EV devices → overwrite their headline kWh/cost from segments.
        for _m, _v in _seg_dev.items():
            if _m in sub_totals:
                sub_totals[_m]["imp_kwh"]  = _v["kwh"]
                sub_totals[_m]["imp_cost"] = _v["cost"]
        # Physical EV clamp(s) → superseded by the synthetic EV. Drop from the breakdown
        # entirely so there's no 0-kWh ghost row in the UI; the sensor's block data stays
        # untouched in the DB, kept only for a possible future use (no consumer for a posterity
        # row today).
        for _m in _phys_ev_ids:
            sub_totals.pop(_m, None)
        # Synthetic EV headline row (the authority).
        if _syn_ev_k > 1e-9:
            sub_totals[_EV_SYN_ID] = {
                "imp_kwh": round(_syn_ev_k, 6), "imp_cost": round(_syn_ev_c, 6), "exp_kwh": 0.0,
                "label": "EV", "meter_type": "ev_charger", "derived": True,
                "rate_tiers": defaultdict(lambda: {"kwh": 0.0, "cost": 0.0, "blocks": 0}),
            }
        house_imp_kwh  = _seg_house_k
        house_imp_cost = _seg_house_c

        # P4.2: rebuild the main-import RATE TIERS from segments too. Otherwise a cap-boundary
        # block contributes at its single BLENDED imp_rate (a unique mid-value per boundary block),
        # which proliferates distinct rates past _MAX_RATE_ROWS and collapses the whole Usage-Stats
        # rate breakdown into one meaningless average row (observed on the 3h-cap fixture: 16
        # boundary blends → one 0.0752 row). From segments each boundary block splits into its two
        # REAL bands (off-peak + peak), matching the bill. Byte-identical uncapped (1 band/block).
        # Cluster adjacent segment rates within _SPLIT_BAND_EPS into display bands (as the bill's
        # bill_card.rate_rows does) so per-slot jitter (0.0549 vs 0.055) doesn't shatter one band into
        # look-alike rows — otherwise the distinct-rate count still trips _collapse_rate_tiers.
        _acc: dict = defaultdict(lambda: {"kwh": 0.0, "cost": 0.0, "blk": set()})
        for _r in _seg_rows:
            _ir = _r["inc_rate"]
            if not _ir:
                continue
            _v = _acc[round(_ir, 6)]
            _v["kwh"] += _r["kwh"]; _v["cost"] += _r["kwh"] * _ir; _v["blk"].add(_r["block_start"])
        _EPS = 0.0015
        _bands: list = []
        for _rk in sorted(_acc):
            _v = _acc[_rk]
            if _bands and (_rk - _bands[-1]["max"]) <= _EPS:
                _b = _bands[-1]
                _b["kwh"] += _v["kwh"]; _b["cost"] += _v["cost"]; _b["blk"] |= _v["blk"]; _b["max"] = _rk
            else:
                _bands.append({"kwh": _v["kwh"], "cost": _v["cost"], "blk": set(_v["blk"]), "max": _rk})
        rate_tiers.clear()
        for _b in _bands:
            _wr = round(_b["cost"] / _b["kwh"], 6) if _b["kwh"] else 0.0
            rate_tiers[_wr]["kwh"]  += _b["kwh"]
            rate_tiers[_wr]["cost"] += _b["cost"]
            rate_tiers[_wr]["blocks"] = len(_b["blk"])

    # ── Synthetic dispatch-derived EV device (display-only; BL-22) ───────────────
    # For an account with NO EV sub-meter AND no other sub-meters (a pure-API /
    # no-HA-sensor setup), reconstruct the EV/house split from the completed-dispatch
    # delta — Octopus's own per-slot EV energy — so a sensor-less user still sees
    # car-vs-house. Guaranteed no-op when an EV meter (or any sub-meter) exists.
    # Grid-clipped per slot (EV can't exceed that slot's grid import) with its cost
    # apportioned from the slot's import cost, so house + EV == grid import exactly.
    if not _seg_rows and _configured_ev_meter_id(cfg) is None and not sub_totals:
        # Fallback ONLY when BL-27 did not run (range not fully segmented). When it did, the
        # synthetic EV is already derived above from the 'ev' segments — running this too would
        # double-count.
        try:
            _drows = store._conn.execute(
                "SELECT slot_start, kind, energy_kwh FROM dispatch_history "
                "WHERE kind='completed' AND slot_start >= ? AND slot_start < ?",
                (utc_start, utc_end)).fetchall()
            _ev_by_slot = _dispatch_derived_ev_kwh(
                [{"slot_start": r["slot_start"], "kind": r["kind"],
                  "energy_kwh": r["energy_kwh"]} for r in _drows])
        except Exception:
            _ev_by_slot = {}
        if _ev_by_slot:
            import ev_split as _evs
            _main_by_slot = {}
            for row in rows:                     # no sub-meters here, so all main
                _main_by_slot[row["block_start"]] = (
                    float(row["imp_kwh"] or 0), float(row["imp_cost"] or 0),
                    float(row["imp_rate"] or 0))
            # The split is ev_split's: the EV segments, the ones the bill prices (step 5,
            # 8 Oct 2026). A block with segments but no EV segment has none, whatever its
            # imp_*_ev columns say; one with no segments at all keeps its columns (LEGACY).
            _split = {}
            try:
                for _sg in store._conn.execute(
                        "SELECT b.block_start, " + _evs.sql_kwh("b") + " AS k, "
                        + _evs.sql_cost("b") + " AS c FROM blocks b "
                        "WHERE b.meter_id = 'electricity_main' "
                        "AND b.block_start >= ? AND b.block_start < ?", (utc_start, utc_end)):
                    if _sg["k"] is not None:
                        _split[_sg["block_start"]] = (float(_sg["k"]), float(_sg["c"] or 0.0))
            except Exception:
                _split = {}
            _ev_kwh = _ev_cost = 0.0
            _ev_tiers = defaultdict(lambda: {"kwh": 0.0, "cost": 0.0, "blocks": 0})
            for _slot, _e in _ev_by_slot.items():
                _mk, _mc, _mr = _main_by_slot.get(_slot, (0.0, 0.0, 0.0))
                _gk, _gc = _split.get(_slot, (None, None))
                # The split where there is one; else the dispatch pro-rata carve. EV is tiered
                # at its own rate.
                if _gk is not None and _gk > 1e-9:
                    _ek = min(_gk, _mk) if _mk > 0 else 0.0
                    _ec = _gc * (_ek / _gk) if _gk > 0 else 0.0
                    _tier_rate = (_gc / _gk if _gk > 0 else 0.0) or _mr
                else:
                    _ek = min(_e, _mk) if _mk > 0 else 0.0     # grid-clip to the slot
                    _ec = _mc * (_ek / _mk) if _mk > 0 else 0.0   # apportion cost
                    _tier_rate = _mr
                if _ek <= 1e-9:
                    continue
                _ev_kwh += _ek
                _ev_cost += _ec
                if _tier_rate != 0:              # keep plunge-price slots too (#371)
                    _t = _ev_tiers[round(_tier_rate, 6)]
                    _t["kwh"] += _ek; _t["cost"] += _ec; _t["blocks"] += 1
            if _ev_kwh > 1e-9:
                house_imp_kwh  = max(0.0, house_imp_kwh  - _ev_kwh)
                house_imp_cost = max(0.0, house_imp_cost - _ev_cost)
                sub_totals["ev_dispatch"] = {
                    "imp_kwh": _ev_kwh, "imp_cost": _ev_cost, "exp_kwh": 0.0,
                    "label": "EV (from dispatch)", "meter_type": "ev_charger",
                    "derived": True, "rate_tiers": _ev_tiers,
                }

    # ── Derived totals ────────────────────────────────────────────────────────
    total_sc = round(sum(daily_sc.values()), 4)
    total_days = len(daily_sc)
    net_cost = round(main_imp_cost + total_sc - main_exp_cost, 4)

    # Weighted average import rate
    weighted_rate = round(main_imp_cost / main_imp_kwh, 6) if main_imp_kwh > 0 else None

    # Net grid position — import minus export (positive = net consumer, negative = net exporter)
    # Self-sufficiency cannot be computed without a generation meter (we don't know
    # how much solar was self-consumed vs exported). We report net position instead.
    net_grid_kwh = round(main_imp_kwh - main_exp_kwh, 3)

    # Net exporter days — days where exp_kwh > imp_kwh
    # Requires a per-day query (lightweight)
    net_exporter_days = 0
    try:
        day_rows = store._conn.execute(
            """SELECT SUM(imp_kwh) as di, SUM(exp_kwh) as de
               FROM blocks
               WHERE meter_id='electricity_main'
                 AND block_start >= ? AND block_start < ?
               GROUP BY date(block_start)""",
            (utc_start, utc_end)
        ).fetchall()
        net_exporter_days = sum(1 for r in day_rows
                                if (r["de"] or 0) > (r["di"] or 0))
    except Exception:
        pass

    # Peak demand window — find the 2-hour window with highest average house import
    peak_window_start = peak_window_kwh = None
    if hour_kwh:
        hours = sorted(hour_kwh.keys())
        best = -1.0
        best_h = hours[0]
        for h in hours:
            h2 = (h + 1) % 24
            combined = hour_kwh.get(h, 0) + hour_kwh.get(h2, 0)
            if combined > best:
                best = combined
                best_h = h
        peak_window_start = best_h
        peak_window_kwh   = round(best, 3)

    # Serialise rate_tiers (keys are floats, not JSON-safe as dict keys), then fold
    # to a single average row above _MAX_RATE_ROWS so Agile matches the Billing view.
    rate_tiers_list = _collapse_rate_tiers(sorted(
        [{"rate": k, "kwh": round(v["kwh"], 3),
          "cost": round(v["cost"], 4), "blocks": v["blocks"]}
         for k, v in rate_tiers.items()],
        key=lambda x: x["rate"]
    ))

    # Serialise sub_totals rate_tiers similarly (same per-device collapse)
    for mid, st in sub_totals.items():
        st["rate_tiers"] = _collapse_rate_tiers(sorted(
            [{"rate": k, "kwh": round(v["kwh"], 3),
              "cost": round(v["cost"], 4), "blocks": v["blocks"]}
             for k, v in st["rate_tiers"].items()],
            key=lambda x: x["rate"]
        ))

    return {
        # Cost & earnings
        "imp_kwh":          round(main_imp_kwh,  3),
        "exp_kwh":          round(main_exp_kwh,  3),
        "imp_cost":         round(main_imp_cost, 4),
        "exp_cost":         round(main_exp_cost, 4),
        "standing_charge":  total_sc,
        "net_cost":         net_cost,
        "weighted_rate":    weighted_rate,
        "total_days":       total_days,
        # Rate tier distribution (main meter)
        "rate_tiers":       rate_tiers_list,
        # Net grid position
        "net_grid_kwh":      net_grid_kwh,    # positive = net importer, negative = net exporter
        "net_exporter_days": net_exporter_days,
        # House consumption (excl sub-meters)
        "house_imp_kwh":    round(house_imp_kwh,  3),
        "house_imp_cost":   round(house_imp_cost, 4),
        # Peak demand
        "peak_window_start": peak_window_start,   # local hour 0-23
        "peak_window_kwh":   peak_window_kwh,
        # Sub-meters
        "sub_meters":        sub_totals,
    }



@app.route("/api/storage-info")
def api_storage_info():
    """Return DB size, disk usage, growth rate and runway prediction."""
    import shutil as _shutil
    store = store_access.store()
    db_path = os.path.join(DATA_DIR, "blocks.db")

    # DB and disk sizes
    db_bytes  = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    # The write-ahead log and shared-memory file sit next to blocks.db and are
    # real disk. Reporting blocks.db alone under-reports what EMT occupies, and
    # hides the one case where that matters: a reader holding an open
    # transaction during heavy writing inflates the WAL, and before
    # journal_size_limit was set it stayed inflated until a restart. ~4 MB is
    # normal (SQLite auto-checkpoints at 1000 pages); a persistently larger
    # figure is worth investigating rather than invisible.
    wal_bytes = os.path.getsize(db_path + "-wal") if os.path.exists(db_path + "-wal") else 0
    shm_bytes = os.path.getsize(db_path + "-shm") if os.path.exists(db_path + "-shm") else 0
    try:
        du = _shutil.disk_usage(DATA_DIR)
        disk_total = du.total
        disk_free  = du.free
        disk_used  = du.used
    except Exception:
        disk_total = disk_free = disk_used = 0

    # Growth rate — oldest block to now
    growth_mb_per_day  = None
    runway_days        = None
    days_of_data       = None
    try:
        row = store._conn.execute(
            "SELECT MIN(block_start) FROM blocks"
        ).fetchone()
        if row and row[0]:
            from datetime import datetime, timezone
            oldest = datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc)
            now    = datetime.now(timezone.utc)
            days_of_data = max((now - oldest).total_seconds() / 86400, 1)
            growth_mb_per_day = (db_bytes / 1024 / 1024) / days_of_data
            if growth_mb_per_day > 0:
                runway_days = int((disk_free / 1024 / 1024) / growth_mb_per_day)
    except Exception:
        pass

    return jsonify({
        "db_bytes":         db_bytes,
        "db_mb":            round(db_bytes / 1024 / 1024, 2),
        "wal_bytes":        wal_bytes,
        "wal_mb":           round(wal_bytes / 1024 / 1024, 2),
        "shm_bytes":        shm_bytes,
        "total_db_mb":      round((db_bytes + wal_bytes + shm_bytes) / 1024 / 1024, 2),
        "disk_total_mb":    round(disk_total / 1024 / 1024, 1),
        "disk_free_mb":     round(disk_free / 1024 / 1024, 1),
        "disk_used_mb":     round(disk_used / 1024 / 1024, 1),
        "disk_free_pct":    round(disk_free / disk_total * 100, 1) if disk_total else 0,
        "days_of_data":     round(days_of_data, 1) if days_of_data else None,
        "growth_mb_per_day": round(growth_mb_per_day, 4) if growth_mb_per_day else None,
        "runway_days":      runway_days,
    })


@app.route("/api/import/extract-zip", methods=["POST"])
def api_import_extract_zip():
    """Extract JSON files from an uploaded zip and return them as base64."""
    import zipfile, base64
    _upload_zip = os.path.join(DATA_DIR, "upload_extract.zip")
    try:
        zf_file = request.files.get("zipfile")
        if not zf_file:
            return jsonify({"error": "No zip file provided"}), 400
        known = {"blocks.db", "blocks.json"}
        files = {}
        # From a file, not the upload stream — see api_import_from_zip (3.10 has no
        # SpooledTemporaryFile.seekable(), which zipfile needs).
        zf_file.save(_upload_zip)
        with zipfile.ZipFile(_upload_zip, "r") as zf:
            for name in zf.namelist():
                basename = os.path.basename(name)
                if basename in known:
                    files[basename] = base64.b64encode(zf.read(name)).decode("utf-8")
        if not files:
            return jsonify({"error": "No recognised files found in zip"}), 400
        logger.info("api_import_extract_zip: extracted %s", list(files.keys()))
        return jsonify({"files": files})
    except Exception as e:
        logger.error("api_import_extract_zip: %s", e)
        return jsonify({"error": str(e)}), 500
    finally:
        if os.path.exists(_upload_zip):
            try: os.remove(_upload_zip)
            except Exception: pass


@app.route("/api/backup/flat-info", methods=["GET"])
def api_backup_flat_info():
    """Return metadata about the last-finalise flat backup files.

    Primary files (written by 2.1.0+):
      blocks.db       — the only file needed for a full restore
      meters_config.json — convenience export

    Legacy files (written by older versions, no longer updated):
      blocks.json     — flat JSON format from 1.x/2.0.x
    """
    from datetime import datetime as _dt
    # Primary files — written by current engine
    primary = ["blocks.db"]
    # Legacy files — may be present from older versions, no longer written
    legacy  = ["blocks.json"]

    files = {}
    for fname in primary + legacy:
        fpath = f"{_share_backup_dir()}/{fname}"
        if fname == "meters_config.json":
            continue  # no longer a restore target — config lives inside blocks.db
        if os.path.exists(fpath):
            mtime = os.path.getmtime(fpath)
            size  = os.path.getsize(fpath)
            entry = {
                "modified": _dt.utcfromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%S UTC"),
                "size_kb":  round(size / 1024, 1),
            }
            if fname in legacy:
                entry["legacy"] = True
                entry["note"]   = "Legacy format from v1.x/v2.0.x — no longer updated"
            files[fname] = entry
    return jsonify(files)


@app.route("/api/import/extract-zip-by-name", methods=["POST"])
def api_import_extract_zip_by_name():
    """Extract JSON files from a named backup zip (server-side) and return as base64."""
    import zipfile, base64
    try:
        data    = request.get_json(force=True)
        zipname = data.get("zip", "")
        if not zipname or "/" in zipname or "\\" in zipname:
            return jsonify({"error": "Invalid zip name"}), 400
        zip_path = f"{_share_backup_dir()}/backups/{zipname}"
        if not os.path.exists(zip_path):
            return jsonify({"error": "Backup not found"}), 404
        known = {"blocks.db", "blocks.json"}
        files = {}
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                basename = os.path.basename(name)
                if basename in known:
                    files[basename] = base64.b64encode(zf.read(name)).decode("utf-8")
        if not files:
            return jsonify({"error": "No recognised JSON files found in backup"}), 400
        return jsonify({"files": files})
    except Exception as e:
        logger.error("api_import_extract_zip_by_name: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/backup/zip-entries", methods=["POST"])
def api_backup_zip_entries():
    """List the recognised files inside a named backup zip — names + sizes ONLY, no
    decompression of the bodies. This is what the Restore dialog needs to build its
    file picker; the old path base64-encoded the whole blocks.db (tens of MB) just to
    read the filenames, which stalled the dialog. The actual restore re-extracts the
    zip server-side, so nothing else needs the contents here."""
    import zipfile
    try:
        data = request.get_json(force=True) or {}
        zipname = data.get("zip", "")
        if not zipname or "/" in zipname or "\\" in zipname:
            return jsonify({"error": "Invalid zip name"}), 400
        zip_path = f"{_share_backup_dir()}/backups/{zipname}"
        if not os.path.exists(zip_path):
            return jsonify({"error": "Backup not found"}), 404
        known = {"blocks.db", "blocks.json"}
        entries = {}
        with zipfile.ZipFile(zip_path, "r") as zf:
            for info in zf.infolist():
                base = os.path.basename(info.filename)
                if base in known:
                    entries[base] = {"size_kb": round(info.file_size / 1024.0, 1)}
        if not entries:
            return jsonify({"error": "No recognised files found in backup"}), 400
        return jsonify({"files": entries})
    except Exception as e:
        logger.error("api_backup_zip_entries: %s", e)
        return jsonify({"error": str(e)}), 500


def _create_backup_zip(label="backup"):
    """Zip all data files into /share/energy_meter_tracker_backup/backups/."""
    import zipfile
    import glob
    from datetime import datetime as _dt
    backup_dir = f"{_share_backup_dir()}/backups"
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = _dt.utcnow().strftime("%Y%m%dT%H%M%S")
    zip_path  = f"{backup_dir}/{timestamp}_{label}.zip"
    files = []  # blocks.db is the only file needed — config is inside it
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Backup blocks DB using SQLite online backup API into a temp file
        import tempfile
        db_src = os.path.join(DATA_DIR, "blocks.db")
        if os.path.exists(db_src):
            try:
                # OWN CONNECTION, opened in THIS thread.
                #
                # This used to run `PRAGMA wal_checkpoint(TRUNCATE)` and `.backup()`
                # on the ENGINE's connection. Every one of the eight callers is a
                # Flask request thread or the restore worker — never the engine loop
                # — so that was concurrent use of one SQLite connection from two
                # threads. The restore path three steps below this one spells out
                # the consequence: *"a PRAGMA wal_checkpoint / close() issued here
                # while the loop is mid-query on the SAME connection ... →
                # Segmentation fault"*. The correct pattern was already written
                # down next to the wrong one.
                #
                # The checkpoint is not needed, which is what made the old version
                # look harmless. SQLite's online backup reads the database through
                # a normal connection, so committed transactions still in the WAL
                # are included — measured: a 2-row table with an un-checkpointed
                # 16 KB WAL backs up as 2 rows, and a row from an OPEN writer
                # transaction is correctly absent. So a private read connection
                # gives a consistent snapshot without touching the writer at all.
                #
                # Unique temp name per call: two backups can run at once (e.g. a
                # config-save's pre_config_save and a restore's pre_restore), and a
                # shared 'blocks.db.bak' let one delete the other's file mid-write →
                # "No such file: blocks.db.bak". Scope it to this pid+thread.
                _bak = f"{db_src}.{os.getpid()}.{threading.get_ident()}.bak"
                try:
                    import sqlite3 as _sq3_bk
                    _src_conn = _sq3_bk.connect(db_src)
                    try:
                        _dst_conn = _sq3_bk.connect(_bak)
                        try:
                            # ONE step (#56). A stepped backup (pages=100) RESTARTS from
                            # the first page whenever another connection writes the
                            # source; under a re-price sweep on the Pi's SD card it
                            # restarted for 3 min 50 s, the WAL grew to 262 MB and one
                            # commit took 118 s. One step holds a read snapshot for the
                            # copy instead — WAL mode, so writers are not blocked.
                            _src_conn.backup(_dst_conn, pages=-1)
                        finally:
                            _dst_conn.close()
                    finally:
                        _src_conn.close()
                    zf.write(_bak, "blocks.db")
                finally:
                    if os.path.exists(_bak):
                        try: os.remove(_bak)
                        except OSError: pass
            except Exception as _e:
                logger.warning("_create_backup_zip: blocks.db backup failed: %s", _e)
        for fname in files:
            src_f = f"{DATA_DIR}/{fname}"
            if os.path.exists(src_f):
                zf.write(src_f, fname)
    # Keep only the 20 most recent zips
    all_zips = sorted(glob.glob(f"{backup_dir}/*.zip"))
    for old_zip in all_zips[:-20]:
        try: os.remove(old_zip)
        except: pass
    logger.info("_create_backup_zip: %s written", os.path.basename(zip_path))
    return zip_path


def _safety_backup(label):
    """A zip backup before an irreversible operation (#67, 4 Oct 2026), CHECKED:
    _create_backup_zip logs a failed database copy and still writes the zip, so a
    zip without blocks.db is a failure here. Returns the zip's name; raises if
    there is no usable backup. The caller then refuses the operation — for a
    delete or a reset, this backup is the only undo."""
    import zipfile
    path = _create_backup_zip(label=label)
    with zipfile.ZipFile(path) as zf:
        if "blocks.db" not in zf.namelist():
            raise RuntimeError("the backup %s holds no database" % os.path.basename(path))
    return os.path.basename(path)


def _stop_running_import(wait_s: float = 60.0) -> bool:
    """Cancel a running API import and wait for it to stop. True when none is
    running (now or to begin with); False if it would not stop within `wait_s`.

    How `delete` honours its `depends_on=("import",)` (B §5, acceptance 3): the
    import writes on the engine loop, and a delete under it leaves a half-deleted
    span the import then writes back into. PREEMPT, as the restore and the device
    delete already did inline: the delete cancels the import and waits, rather than
    deferring to it — an import can run for hours, and the user asked to delete."""
    import time as _t
    import engine as _eng
    running = getattr(_eng, "api_import_running", None)
    if running is None or not running():
        return True
    try:
        _eng.api_import_control("cancel")
    except Exception:
        pass
    deadline = _t.monotonic() + wait_s
    while _t.monotonic() < deadline:
        if not running():
            return True
        _t.sleep(0.5)
    return not running()


_IMPORT_STILL_FINISHING = "An import was still finishing — {} aborted. Try again in a moment."


def _safety_backup_or_refuse(label):
    """(backup name, None), or (None, a 500 response) when the backup failed."""
    try:
        return _safety_backup(label), None
    except Exception as e:
        logger.error("safety backup %s failed — operation refused: %s", label, e)
        return None, (jsonify({"ok": False, "error": "A safety backup could not be taken, "
                               "so nothing was changed: %s" % e}), 500)




# ── Restore: stage, then confirm (first-run design §2, 11 Oct 2026) ─────────────────────────
# One restore path for an uploaded backup. `stage` saves the upload and runs phase 1 on it, and
# says what the backup is; the page asks for that supplier's credentials (or does not); `confirm`
# checks a typed key against the backup's account stamp (phase 2), and starts _restore_worker.
# They replace /api/import (a blocks.db, straight into the worker) and /api/import/from-zip (a
# zip restored in the request thread, with no job claim and no engine_startup after it).

_STAGED_PREFIX = "uploaded_restore-"
_STAGED_RE = re.compile(r"^uploaded_restore-[A-Za-z0-9_]+\.db$")
_STAGED_MAX_AGE_S = 3600


def _staged_path(token):
    """The staged file a token names, or None: the token must be a staged file's own name, and
    the file must still be in DATA_DIR (first-run design §2.2)."""
    if not isinstance(token, str) or not _STAGED_RE.match(token):
        return None
    p = os.path.join(DATA_DIR, token)
    return p if _in_data_dir(p) and os.path.isfile(p) else None


def _remove_stale_stages(max_age=_STAGED_MAX_AGE_S, keep=None):
    """Remove staged uploads nobody confirmed. Never the running restore's own file."""
    import time as _t
    now = _t.time()
    try:
        names = os.listdir(DATA_DIR)
    except OSError:
        return
    running = _restore_job.get("status") == "running"
    for n in names:
        if not (_STAGED_RE.match(n) or _PART_RE.match(n)):    # an abandoned upload's pieces too
            continue
        p = os.path.join(DATA_DIR, n)
        if p == keep or running:
            continue
        try:
            if now - os.path.getmtime(p) > max_age:
                os.remove(p)
                logger.info("restore: removed a staged upload nobody confirmed (%s)", n)
        except OSError:
            pass


def _restore_facts(path, rev):
    """What a staged backup is, for the page (first-run design §2.1), and its account stamp.
    `rev` is phase 1's: a v5 revision, or None for v4. Read here for both kinds, so the v5 path
    needs nothing v4-shaped."""
    import json as _json
    import sqlite3 as _sq3
    import suppliers as _sup
    from urllib.parse import quote as _q
    conn = _sq3.connect("file:%s?immutable=1" % _q(os.path.abspath(path)), uri=True)
    try:
        def _meta(key):
            row = conn.execute("SELECT value FROM store_meta WHERE key = ?", (key,)).fetchone()
            if not row or row[0] is None:
                return None
            try:
                return _json.loads(row[0])
            except (TypeError, ValueError):
                return row[0]
        try:
            row = conn.execute("SELECT value FROM kraken_state "
                               "WHERE key = 'kraken_account_number'").fetchone()
            account = (row[0] or None) if row else None
        except _sq3.Error:
            account = None
        n, first, last = conn.execute(
            "SELECT COUNT(*), MIN(block_start), MAX(block_start) FROM blocks").fetchone()
        written_by = _meta("written_by")
        own_supplier = _meta("supplier") or None
    finally:
        conn.close()
    # No supplier stamp, but an account stamp: Octopus, the only API EMT has had (import spec
    # §3, 4 Oct 2026 — the v4 gate's source_supplier reads it the same way). Either kind can
    # lack the stamp: v5 stamps the supplier only when its config names one.
    supplier, inferred = own_supplier, False
    if not supplier and account:
        supplier, inferred = "octopus", True
    return {
        "kind": "v4" if rev is None else "v5",
        "written_by": written_by,
        "supplier": supplier,
        "supplier_inferred": bool(inferred),
        "prompt": supplier if supplier in _sup.API_CAPABLE else None,
        "account_stamped": bool(account),
        "blocks": n,
        "first_day": (first or "")[:10] or None,
        "last_day": (last or "")[:10] or None,
    }, account


def _test_supplier_key(supplier, creds):
    """Phase 2's key check (first-run design §2.3): a throwaway client, one account query, closed
    again. Saves nothing and touches no store, so the live database is not stamped and no poll
    starts. Returns test_connection's {ok, detail, account_number}."""
    if supplier != "octopus":
        return {"ok": False, "detail": "no key check for supplier %r" % supplier,
                "account_number": None}

    async def _probe():
        from kraken_api_client import KrakenAPIClient
        kw = {"account_number": (creds.get("account_number") or "").strip() or None}
        if (creds.get("base_url") or "").strip():
            kw["base_url"] = creds["base_url"].strip()
        client = KrakenAPIClient(creds["api_key"].strip(), **kw)
        try:
            return await client.test_connection()
        finally:
            try:
                await client.close()
            except Exception:
                pass
    if _event_loop and _event_loop.is_running():
        return _run_on_engine_loop(_probe(), timeout=45.0)
    import asyncio as _asyncio
    return _asyncio.run(_probe())


def _stage_upload(upload):
    """Save a multipart upload as DATA_DIR/uploaded_restore-*.db and stage it (_stage_file)."""
    import tempfile as _tf
    _fd, staged = _tf.mkstemp(prefix=_STAGED_PREFIX, suffix=".db", dir=DATA_DIR)
    os.close(_fd)
    try:
        upload.save(staged)
    except BaseException:
        try: os.remove(staged)
        except OSError: pass
        raise
    return _stage_file(staged)


def _stage_file(staged):
    """Stage the file at `staged` (DATA_DIR/uploaded_restore-*.db, already written): a .db as it
    is, a zip's blocks.db extracted in its place. Returns the staged path; raises _RestoreRefused
    with the user-facing reason, and leaves nothing behind when it does."""
    import tempfile as _tf
    import zipfile as _zf
    import shutil as _sh
    import sqlite3 as _sq3
    from urllib.parse import quote as _q
    try:
        if _zf.is_zipfile(staged):
            # From a file, not the upload stream: on Python 3.10 that stream is a
            # SpooledTemporaryFile with no seekable(), which zipfile needs (added in 3.11).
            _fd, zpath = _tf.mkstemp(prefix="upload_restore-", suffix=".zip", dir=DATA_DIR)
            os.close(_fd)
            os.replace(staged, zpath)
            try:
                with _zf.ZipFile(zpath, "r") as zf:
                    if _zip_has_hot_wal(zf):
                        raise _RestoreRefused(_import_gate().HOT_WAL)
                    name = next((n for n in zf.namelist()
                                 if os.path.basename(n) == "blocks.db"), None)
                    if not name:
                        raise _RestoreRefused("That zip has no blocks.db in it — it is not an "
                                              "Energy Meter Tracker backup.")
                    with zf.open(name) as src, open(staged, "wb") as dst:
                        _sh.copyfileobj(src, dst)
                        dst.flush()
                        os.fsync(dst.fileno())
            finally:
                try: os.remove(zpath)
                except OSError: pass
        # A cheap probe, not a COUNT(*): that the file is a database with a readable `blocks`
        # table. Immutable, so it leaves no -wal / -shm beside the staged file.
        try:
            _tc = _sq3.connect("file:%s?mode=ro&immutable=1" % _q(os.path.abspath(staged)),
                               uri=True)
            try:
                _tc.execute("SELECT 1 FROM blocks LIMIT 1").fetchone()
            finally:
                _tc.close()
        except _sq3.Error:
            raise _RestoreRefused("That file is not a valid EMT database (blocks.db).")
        return staged
    except BaseException:
        try: os.remove(staged)
        except OSError: pass
        raise


@app.route("/api/restore/stage", methods=["POST"])
def api_restore_stage():
    """Stage an uploaded backup (.zip or blocks.db) and run phase 1 on it. 200 {token, facts}:
    the token names the staged file for /api/restore/confirm, and facts say what the backup is
    and which credentials to ask for (first-run design §2). A refusal is 400 {error}, and the
    staged file is gone. Nothing live is touched."""
    import time as _t
    _t0 = _t.monotonic()
    try:
        upload = request.files.get("file")
        if not upload:
            return jsonify({"error": "No file received"}), 400
        # Refuse BEFORE saving anything while a restore runs (10 Oct 2026): a second upload
        # (an ingress retry, a second click) must never land beside the running one.
        if _restore_job.get("status") == "running":
            return jsonify({"error": "a restore is already running",
                            "status": _restore_job}), 409
        _t_parse = _t.monotonic() - _t0
        _t1 = _t.monotonic()
        try:
            staged = _stage_upload(upload)
        except _RestoreRefused as e:
            logger.warning("restore/stage: refused: %s", e)
            return jsonify({"error": str(e)}), 400
        return _stage_reply(staged, "parsed in %.1f s, saved in %.1f s"
                            % (_t_parse, _t.monotonic() - _t1))
    except Exception as e:
        logger.error("restore/stage: %s", e)
        return jsonify({"error": str(e)}), 500


def _stage_reply(staged, how):
    """Phase 1 and the facts on a staged file, as the stage routes answer: 200 {token, facts}, or
    400 {error} with the staged file removed."""
    import time as _t
    _remove_stale_stages(keep=staged)
    _t1 = _t.monotonic()
    try:
        rev = _restore_phase1(staged)
        facts, _account = _restore_facts(staged, rev)
    except Exception as e:
        try: os.remove(staged)
        except OSError: pass
        msg = str(e) if isinstance(e, _RestoreRefused) else \
            "That file is not a valid EMT database (blocks.db)."
        logger.warning("restore/stage: refused: %s", e)
        return jsonify({"error": msg}), 400
    logger.info("restore/stage: received %.1f MB — %s, checked in %.1f s; a %s backup, "
                "written by %s, supplier %s", os.path.getsize(staged) / 1048576.0, how,
                _t.monotonic() - _t1, facts["kind"], facts["written_by"],
                facts["supplier"] or "none")
    return jsonify({"ok": True, "token": os.path.basename(staged), "facts": facts})


# ── A restore upload in pieces (14 Oct 2026) ────────────────────────────────────────────────
# Through Home Assistant's ingress a whole backup in one request is refused before it reaches
# EMT — "HTTP 413", the proxy's body limit — so the page sends it in pieces small enough for any
# limit, and the server reassembles it. begin reserves a file; each chunk is written at the offset
# it states (a retried piece overwrites itself, nothing is appended twice); finish hands the whole
# file to the same stage checks as a one-request upload.
_PART_RE = re.compile(r"^uploaded_restore-[A-Za-z0-9_]+\.part$")
_PART_MAX_BYTES = 1024 * 1024 * 1024          # a backup over 1 GB is not one


def _part_path(upload_id):
    if not isinstance(upload_id, str) or not _PART_RE.match(upload_id):
        return None
    p = os.path.join(DATA_DIR, upload_id)
    return p if _in_data_dir(p) and os.path.isfile(p) else None


@app.route("/api/restore/upload/begin", methods=["POST"])
def api_restore_upload_begin():
    """Reserve a staging file for an upload in pieces. Body {size}. 200 {id, chunk}."""
    import tempfile as _tf
    try:
        if _restore_job.get("status") == "running":
            return jsonify({"error": "a restore is already running", "status": _restore_job}), 409
        body = request.get_json(silent=True) or {}
        try:
            size = int(body.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size <= 0 or size > _PART_MAX_BYTES:
            return jsonify({"error": "That file is not a backup EMT can take."}), 400
        _fd, part = _tf.mkstemp(prefix=_STAGED_PREFIX, suffix=".part", dir=DATA_DIR)
        os.close(_fd)
        return jsonify({"ok": True, "id": os.path.basename(part), "chunk": 512 * 1024})
    except Exception as e:
        logger.error("restore/upload/begin: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/restore/upload/chunk", methods=["POST"])
def api_restore_upload_chunk():
    """One piece: ?id=&offset=, the bytes as the body. Written at `offset`; 200 {received}."""
    try:
        part = _part_path(request.args.get("id"))
        if not part:
            return jsonify({"error": "That upload is no longer open — choose the file again."}), 400
        try:
            offset = int(request.args.get("offset", ""))
        except ValueError:
            return jsonify({"error": "offset must be a number"}), 400
        have = os.path.getsize(part)
        if offset < 0 or offset > have:
            return jsonify({"error": "a piece arrived out of order", "received": have}), 409
        data = request.get_data(cache=False)
        if offset + len(data) > _PART_MAX_BYTES:
            return jsonify({"error": "That file is not a backup EMT can take."}), 400
        with open(part, "r+b") as f:
            f.seek(offset)
            f.write(data)
            f.truncate(offset + len(data))      # a retried piece replaces itself, no more
        return jsonify({"ok": True, "received": offset + len(data)})
    except Exception as e:
        logger.error("restore/upload/chunk: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/restore/upload/finish", methods=["POST"])
def api_restore_upload_finish():
    """The last piece is in: body {id, size}. Stages the whole file exactly as /api/restore/stage
    does — 200 {token, facts}, or 400 {error} with nothing left behind."""
    import time as _t
    try:
        body = request.get_json(silent=True) or {}
        part = _part_path(body.get("id"))
        if not part:
            return jsonify({"error": "That upload is no longer open — choose the file again."}), 400
        if _restore_job.get("status") == "running":
            return jsonify({"error": "a restore is already running", "status": _restore_job}), 409
        try:
            want = int(body.get("size"))
        except (TypeError, ValueError):
            want = -1
        if os.path.getsize(part) != want:
            got = os.path.getsize(part)
            try: os.remove(part)
            except OSError: pass
            return jsonify({"error": "The upload was incomplete (%d of %d bytes) — choose the "
                                     "file again." % (got, want)}), 400
        staged = part[:-len(".part")] + ".db"
        os.replace(part, staged)
        _t1 = _t.monotonic()
        try:
            staged = _stage_file(staged)
        except _RestoreRefused as e:
            logger.warning("restore/stage: refused: %s", e)
            return jsonify({"error": str(e)}), 400
        return _stage_reply(staged, "in pieces, staged in %.1f s" % (_t.monotonic() - _t1))
    except Exception as e:
        logger.error("restore/upload/finish: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/restore/confirm", methods=["POST"])
def api_restore_confirm():
    """Restore a staged backup. Body {token, credentials?, accept_mismatch?}.

    With credentials, phase 2 first (first-run design §2.3–2.4): the key is tested without
    saving it; a failed test is 400; a key for another account than the backup's stamp is 409
    {mismatch} unless accept_mismatch, which imports without keeping the key; a match (or a
    backup with no stamp) saves the key, so the restore's own engine_startup connects. Then the
    staged file goes to _restore_worker. 200 {ok, status:'running'}; the page polls
    /api/backup/restore/status."""
    global _restore_job
    try:
        body = request.get_json(silent=True) or {}
        staged = _staged_path(body.get("token"))
        if not staged:
            return jsonify({"error": "That upload is no longer waiting — upload the backup "
                                     "again."}), 400
        if _restore_job.get("status") == "running":
            return jsonify({"error": "a restore is already running",
                            "status": _restore_job}), 409
        creds = body.get("credentials") or None
        save_creds = None
        if creds and (creds.get("api_key") or "").strip():
            import block_store as _bs
            facts, stamped = _restore_facts(staged, _bs.read_emt_schema(staged))
            if not facts["prompt"]:
                return jsonify({"error": "This backup has no supplier account to connect."}), 400
            # Octopus cannot name the account behind a key: a key is tested by opening an account.
            # Blank, that is the account the backup belongs to — the one being connected (Pi run,
            # 12 Oct 2026: blank was sent as no account and every key failed, "account_number is
            # required"). Typed, it is the user's own, and a different one is the mismatch below.
            from kraken_api_client import _mask
            typed = (creds.get("account_number") or "").strip()
            tried = typed or stamped
            if not tried:
                return jsonify({"error": "Enter the account number for this key — this backup "
                                         "does not record one."}), 400
            result = _test_supplier_key(facts["prompt"], dict(creds, account_number=tried))
            if not result.get("ok"):
                if not typed:
                    return jsonify({"error": "That key could not open account %s, which this backup "
                                             "belongs to (%s). Check the key — or, if it is for "
                                             "another account, enter that account's number."
                                             % (_mask(tried), result.get("detail") or "unknown")}), 400
                return jsonify({"error": "That key could not open account %s (%s)."
                                         % (_mask(tried), result.get("detail") or "unknown")}), 400
            key_acct = result.get("account_number") or tried
            import engine as _eng
            if stamped and key_acct and _eng._norm_account(stamped) != _eng._norm_account(key_acct):
                if not body.get("accept_mismatch"):
                    logger.info("restore/confirm: the key is for another account than the "
                                "backup's (%s, %s) — asking", _mask(stamped), _mask(key_acct))
                    return jsonify({"mismatch": {"backup": _mask(stamped),
                                                 "key": _mask(key_acct)}}), 409
                logger.info("restore/confirm: account mismatch accepted — importing without "
                            "connecting; the key is not kept")
            else:
                save_creds = (creds["api_key"].strip(), key_acct,
                              (creds.get("base_url") or "").strip() or None)
        with _restore_launch_lock:
            if _restore_job.get("status") == "running":
                return jsonify({"error": "a restore is already running",
                                "status": _restore_job}), 409
            if save_creds:
                import engine as _eng
                _eng.save_kraken_credentials(*save_creds)
                logger.info("restore/confirm: the key matches the backup's account — saved")
            _restore_job.clear()
            _restore_job.update({"status": "running", "step": "starting",
                                 "restored": None, "error": None})
        threading.Thread(target=_restore_worker,
                         args=("", ["blocks.db"], False),
                         kwargs={"staged": staged, "checked": True},
                         daemon=True, name="restore-upload").start()
        return jsonify({"ok": True, "status": "running"})
    except Exception as e:
        logger.error("restore/confirm: %s", e)
        return jsonify({"error": str(e)}), 500

# ── Database maintenance ───────────────────────────────────────────────────────────────────

@app.route("/api/db/vacuum", methods=["POST"])
def api_db_vacuum():
    """
    Run VACUUM on the blocks database.
    Pauses the engine during the operation to ensure exclusive DB access.
    Returns { ok, size_before_kb, size_after_kb, saved_kb }.
    """
    import os
    try:
        import engine as _eng
        store = store_access.store()      # the PRAGMA reads; VACUUM opens its own below

        # Measure before
        db_path = store._conn.execute("PRAGMA database_list").fetchone()["file"]
        size_before = os.path.getsize(db_path) if db_path and os.path.exists(db_path) else 0

        # Check free pages — gives user an honest picture before we run
        page_size  = store._conn.execute("PRAGMA page_size").fetchone()[0]
        free_pages = store._conn.execute("PRAGMA freelist_count").fetchone()[0]
        free_bytes = free_pages * page_size

        # VACUUM gets its OWN connection, and deliberately does NOT go through
        # `_engine_write`. It rewrites the entire database file — minutes on a
        # Pi — and the engine thread is where member 23 lives: hopping this onto
        # the loop would stall live capture for the duration, which is the one
        # thing B §4.0 says must not happen. Pausing the engine and working on a
        # private connection is the right shape; it was only ever using the
        # shared web store, which four waitress threads also write through.
        _eng.pause_engine("database vacuum")
        try:
            import sqlite3 as _sq3_vac
            _vac = _sq3_vac.connect(db_path)
            try:
                _vac.execute("VACUUM")
                _vac.commit()
            finally:
                _vac.close()
        finally:
            _eng.resume_engine()

        size_after = os.path.getsize(db_path) if db_path and os.path.exists(db_path) else 0
        saved = max(0, size_before - size_after)

        logger.info(
            "api_db_vacuum: complete — before=%d B after=%d B saved=%d B (free_pages=%d)",
            size_before, size_after, saved, free_pages
        )
        return jsonify({
            "ok":           True,
            "size_before":  size_before,
            "size_after":   size_after,
            "saved":        saved,
            "free_pages":   free_pages,
            "free_bytes":   free_bytes,
        })
    except Exception as e:
        try:
            import engine as _eng
            _eng.resume_engine()
        except Exception:
            pass
        logger.error("api_db_vacuum: %s", e)
        return jsonify({"error": str(e)}), 500


# ── Block deletion ────────────────────────────────────────────────────────────────────────────────

def _delete_window_to_utc_times(from_time, to_time, tz_name, from_date, to_date):
    """Resolve the delete's time-of-day window to the UTC HH:MM the store filters on.

    A WHOLE-DAY window (00:00–23:59) must NOT become a UTC time-of-day filter: the
    local-date→UTC bounds (local_date_range_to_utc_bounds) already cover complete
    local days. Converting the day boundaries to UTC under BST turns 23:59 local
    into 22:59 UTC → `TIME(block_start) <= '22:59'`, which excludes the 23:00–23:59
    UTC band that IS local midnight — so 2 blocks/day (00:00 & 00:30 local) survive
    every delete. Only an EXPLICIT partial window is converted for the filter.
    """
    if from_time == "00:00" and to_time == "23:59":
        return "00:00", "23:59"            # whole day → rely on the date bounds only
    return (_corrections_time_to_utc(from_time, tz_name, from_date),
            _corrections_time_to_utc(to_time, tz_name, to_date))


@app.route("/api/blocks/delete/preview", methods=["POST"])
def api_blocks_delete_preview():
    """
    Preview how many blocks would be deleted for a date range.
    Body: { from_date, to_date, meter_id? }
    Returns: { blocks, dates }
    """
    try:
        data      = request.get_json(force=True) or {}
        from_date = data.get("from_date", "").strip()
        from_time = data.get("from_time", "00:00").strip() or "00:00"
        to_date   = data.get("to_date", "").strip()
        to_time   = data.get("to_time", "23:59").strip() or "23:59"
        meter_id  = data.get("meter_id") or None
        if not from_date or not to_date:
            return jsonify({"error": "from_date and to_date required"}), 400
        if from_date > to_date:
            return jsonify({"error": "from_date must not be after to_date"}), 400
        # Convert local times to UTC using the configured timezone
        _cfg = load_config()
        _tz_name = "UTC"
        for _md in (_cfg.get("meters") or {}).values():
            if not (_md.get("meta") or {}).get("sub_meter"):
                _tz_name = (_md.get("meta") or {}).get("timezone", "UTC")
                break
        reconstructed_only = bool(data.get("reconstructed_only"))
        store = store_access.store()      # this request thread's own read connection
        # Pass LOCAL times: the store treats from/to as the ends of ONE contiguous
        # span (from_date+from_time .. to_date+to_time), not a per-day window.
        result = store.count_blocks_for_date_range(from_date, to_date, meter_id, from_time, to_time,
                                                   tz_name=_tz_name, reconstructed_only=reconstructed_only)
        return jsonify(result)
    except Exception as e:
        logger.error("api_blocks_delete_preview: %s", e)
        return jsonify({"error": str(e)}), 500


# Background delete/purge job — shared status so the UI can show a persistent
# "Deleting... don't rebuild/restart" banner that survives navigation, exactly
# like the restore job. Only one destructive op runs at a time (both use this).
_delete_job = {"status": "idle"}


def _delete_step(s):
    _delete_job["step"] = s
    logger.info("delete: %s", s)


@app.route("/api/blocks/delete/status", methods=["GET"])
def api_blocks_delete_status():
    """Poll the background delete/purge job's progress/status."""
    return jsonify(_delete_job)


@job("delete")
def _delete_worker(from_date, from_time_utc, to_date, to_time_utc,
                   meter_id, tz_name, reconstructed_only):
    """Background date-range delete worker (own thread; survives navigation).
    Same store access as the old synchronous route (off the engine loop thread),
    just with progress reported into _delete_job."""
    _j = _delete_job
    import engine as _eng
    _eng.set_delete_active(True)   # pause the carbon backfill from starting mid-delete
    store = None
    try:
        # B: the import first (depends_on=("import",)), then the backup.
        _delete_step("stopping the running import")
        if not _stop_running_import():
            _j.update({"status": "error", "error": _IMPORT_STILL_FINISHING.format("delete")})
            return
        # #67: back up first, and stop without one — the 30-minute share copy this
        # delete used to fall back on is gone.
        _delete_step("taking a safety backup")
        _j["backup"] = _safety_backup("pre-delete")
        _delete_step("deleting blocks")
        store = _own_store()          # own thread, own connection
        result = store.delete_blocks_for_date_range(
            from_date, to_date, meter_id, from_time_utc, to_time_utc,
            tz_name=tz_name, reconstructed_only=reconstructed_only)
        # C §2a: a bulk operation bumps the EPOCH rather than moving thousands of
        # day fingerprints. A range delete can span years, and a client holding
        # per-day state needs telling once, not per day.
        store.bump_data_epoch()
        # A device-only delete leaves the parent main meter's remainder stale for
        # those windows; recompute it (PASS 2) against the surviving sub-meters.
        if result.get("recompute_parent"):
            try:
                import engine as _eng
                _delete_step("recomputing meter remainders")
                result["remainders_recomputed"] = _eng.recompute_remainders_for_window(
                    result["recompute_parent"], result["recompute_from"],
                    result["recompute_to"])
            except Exception as _re:
                logger.error("_delete_worker: remainder recompute failed: %s", _re)
                result["remainders_recomputed"] = 0
        logger.info("_delete_worker: deleted %d blocks across %d dates "
                    "(meter=%s)", result.get("deleted"), result.get("dates"),
                    meter_id or "all")
        _j.update({"status": "done", "kind": "delete", "result": result, "error": None})
    except Exception as e:
        logger.error("_delete_worker: %s", e)
        _j.update({"status": "error", "error": str(e)})
    finally:
        _eng.set_delete_active(False)
        # Own connection, own thread — close it here or every delete leaks one.
        _release_store(store)


@job("delete")
def _purge_worker():
    """Background 'delete all imported data' worker (own thread). Backs up first,
    purges every imported block + derivations + checkpoints, rebuilds charts."""
    _j = _delete_job
    import engine as _eng
    _eng.set_delete_active(True)   # pause the carbon backfill from starting mid-purge
    store = None
    try:
        # B: the import first (depends_on=("import",)) — a purge under a running
        # import removes history the import is still writing.
        _delete_step("stopping the running import")
        if not _stop_running_import():
            _j.update({"status": "error", "error": _IMPORT_STILL_FINISHING.format("purge")})
            return
        store = _own_store()          # own thread, own connection
        backup = None
        try:
            _delete_step("taking a safety backup")
            backup = os.path.basename(_create_backup_zip(label="pre-purge-import"))
        except Exception as be:
            logger.warning("purge: backup failed (continuing): %s", be)
        _delete_step("deleting all imported data")
        result = store.purge_imported_history()
        store.bump_data_epoch()          # C §2a — removes all imported history
        result["backup"] = backup
        logger.info("_purge_worker: removed %d imported block(s) (backup=%s)",
                    result.get("blocks"), backup)
        _j.update({"status": "done", "kind": "purge",
                   "result": {"ok": True, **result}, "error": None})
    except Exception as e:
        logger.error("_purge_worker: %s", e)
        _j.update({"status": "error", "error": str(e)})
    finally:
        _eng.set_delete_active(False)
        # Own connection, own thread — close it here or every delete leaks one.
        _release_store(store)


@app.route("/api/blocks/delete", methods=["POST"])
def api_blocks_delete():
    """
    Delete blocks for a date range as a BACKGROUND job — returns at once so it
    finishes even if the user navigates away. Poll /api/blocks/delete/status.
    Body: { from_date, to_date, meter_id?, confirmed: true }
    """
    try:
        data      = request.get_json(force=True) or {}
        from_date = data.get("from_date", "").strip()
        from_time = data.get("from_time", "00:00").strip() or "00:00"
        to_date   = data.get("to_date", "").strip()
        to_time   = data.get("to_time", "23:59").strip() or "23:59"
        meter_id  = data.get("meter_id") or None
        confirmed = data.get("confirmed", False)
        if not from_date or not to_date:
            return jsonify({"error": "from_date and to_date required"}), 400
        if from_date > to_date:
            return jsonify({"error": "from_date must not be after to_date"}), 400
        if not confirmed:
            return jsonify({"error": "confirmed must be true"}), 400
        if _delete_job.get("status") == "running":
            return jsonify({"error": "a delete is already running",
                            "status": _delete_job}), 409
        import engine as _eng_q
        if _eng_q.delete_in_progress():            # a device delete holds the flag (B)
            return jsonify({"error": "Another delete is running. Try again once it finishes.",
                            "retry": True}), 409
        # Refuse while a kraken poll / BL-8 backfill is mutating blocks — the two
        # share the single SQLite connection and a delete racing an in-flight
        # backfill leaves a half-deleted day (the 'deleted the 12th mid-backfill,
        # it came back partial' incident). The flag is cleared when the poll cycle
        # finishes, so a retry succeeds once the log shows poll completion.
        import engine as _eng
        if _eng.poll_in_progress():
            return jsonify({"error": "Unable to delete — a backfill is in progress. "
                            "Please try again once it completes (watch the logs for "
                            "poll completion), then retry.",
                            "retry": True}), 409
        # Resolve the configured timezone; the store converts the LOCAL from/to
        # times into one CONTIGUOUS UTC span (not a per-day time-of-day window).
        _cfg = load_config()
        _tz_name = "UTC"
        for _md in (_cfg.get("meters") or {}).values():
            if not (_md.get("meta") or {}).get("sub_meter"):
                _tz_name = (_md.get("meta") or {}).get("timezone", "UTC")
                break
        reconstructed_only = bool(data.get("reconstructed_only"))
        _delete_job.clear()
        _delete_job.update({"status": "running", "kind": "delete",
                            "step": "starting", "result": None, "error": None})
        threading.Thread(
            target=_delete_worker,
            args=(from_date, from_time, to_date, to_time,
                  meter_id, _tz_name, reconstructed_only),
            daemon=True, name="delete").start()
        return jsonify({"ok": True, "status": "running"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error("api_blocks_delete: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/historical/purge-preview")
def api_historical_purge_preview():
    """How much historical-import data would a purge remove (block count + span)."""
    try:
        return jsonify(store_access.store().count_imported_history())
    except Exception as e:
        logger.error("api_historical_purge_preview: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/historical/purge", methods=["POST"])
def api_historical_purge():
    """The get-out-clause: back up, then delete ALL historical-import data and
    everything derived from it (imported blocks, rate derivations, import
    checkpoints/gaps). Requires confirmed=true. Live data is untouched."""
    try:
        body = request.get_json(force=True, silent=True) or {}
        if not body.get("confirmed"):
            return jsonify({"error": "confirmation required"}), 400
        if _delete_job.get("status") == "running":
            return jsonify({"error": "a delete is already running",
                            "status": _delete_job}), 409
        import engine as _eng_q
        if _eng_q.delete_in_progress():            # a device delete holds the flag (B)
            return jsonify({"error": "Another delete is running. Try again once it finishes.",
                            "retry": True}), 409
        _delete_job.clear()
        _delete_job.update({"status": "running", "kind": "purge",
                            "step": "starting", "result": None, "error": None})
        threading.Thread(target=_purge_worker, daemon=True, name="purge").start()
        return jsonify({"ok": True, "status": "running"})
    except Exception as e:
        logger.error("api_historical_purge: %s", e)
        return jsonify({"error": str(e)}), 500


# ── Historical corrections ────────────────────────────────────────────────────

def _corrections_time_to_utc(time_str: str, tz_name: str,
                              anchor_date: str = "") -> str:
    """
    Convert a local HH:MM time string to UTC HH:MM for use in block_start TIME() filter.
    anchor_date (YYYY-MM-DD) should be the correction date so the correct DST offset is
    applied — e.g. a March date uses GMT, an April date uses BST.
    Returns the original string if conversion fails.
    """
    if not time_str:
        return time_str
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime, timezone
        h, m = int(time_str[:2]), int(time_str[3:5])
        if anchor_date:
            y, mo, d = int(anchor_date[:4]), int(anchor_date[5:7]), int(anchor_date[8:10])
            naive = datetime(y, mo, d, h, m, 0)
        else:
            naive = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
        local_dt = naive.replace(tzinfo=ZoneInfo(tz_name))
        utc_dt   = local_dt.astimezone(timezone.utc)
        return utc_dt.strftime("%H:%M")
    except Exception:
        return time_str


def _corrections_build_where(corr_type, from_date, to_date, channel,
                              from_time_utc, to_time_utc, meter_id, tz_name,
                              api_settled_only=False):
    """
    Build WHERE clause and params for corrections queries.

    Date filtering uses UTC block_start bounds derived from local dates via
    local_date_range_to_utc_bounds() so BST blocks are handled correctly.

    Time filtering uses TIME(block_start) in UTC, with the times already converted
    from local to UTC by _corrections_time_to_utc. When the converted window crosses
    UTC midnight (e.g. Economy 7 00:30-07:30 BST = 23:30-06:30 UTC), the clause uses
    OR instead of AND so blocks on both sides of midnight are included.

    Standing charge corrections are always restricted to the main meter
    (meter_id in the meters table with is_sub_meter=0) — sub-meter rows
    carry sc=0.0 and must not be overwritten.
    """
    from block_store import local_date_range_to_utc_bounds as _corr_utc_bounds
    col = None
    if corr_type == "rate":
        col = "imp_rate" if channel == "import" else "exp_rate"

    # Use UTC bounds derived from local dates — handles BST correctly without local_date column
    _utc_s, _utc_e = _corr_utc_bounds(from_date, to_date, tz_name)
    clauses = ["block_start >= ?", "block_start < ?"]
    params  = [_utc_s, _utc_e]

    if from_time_utc and to_time_utc:
        if from_time_utc <= to_time_utc:
            # Normal window — entirely within one UTC day
            clauses.append("TIME(block_start) >= ?")
            params.append(from_time_utc)
            clauses.append("TIME(block_start) < ?")
            params.append(to_time_utc)
        else:
            # Window crosses UTC midnight (e.g. BST night rate)
            # Select blocks OUTSIDE the gap (i.e. after from OR before to)
            clauses.append(
                "(TIME(block_start) >= ? OR TIME(block_start) < ?)"
            )
            params.extend([from_time_utc, to_time_utc])
    elif from_time_utc:
        clauses.append("TIME(block_start) >= ?")
        params.append(from_time_utc)
    elif to_time_utc:
        clauses.append("TIME(block_start) < ?")
        params.append(to_time_utc)

    if corr_type == "standing":
        # Standing charge only lives on main meter rows — always restrict
        # regardless of the meter_id selector (which is hidden for standing)
        clauses.append(
            "meter_id IN (SELECT meter_id FROM meters WHERE is_sub_meter = 0)"
        )
    elif meter_id and meter_id != "all":
        clauses.append("meter_id = ?")
        params.append(meter_id)

    if col:
        clauses.append(f"{col} IS NOT NULL")

    if api_settled_only and corr_type == "rate":
        # API+ gate: only DCC-settled blocks (imp_kwh_api/exp_kwh_api populated)
        # may have their rate corrected. An unsettled block's kWh — and its
        # overlay-derived rate — are still going to be rewritten at settlement,
        # which would clobber the correction.
        settled_col = "imp_kwh_api" if channel == "import" else "exp_kwh_api"
        clauses.append(f"{settled_col} IS NOT NULL")

    return " AND ".join(clauses), params, col


def _corrections_api_gate_active() -> bool:
    """True when the data-source mode uses the DCC API, so rate corrections must
    be gated to settled blocks. Pure CAD (no API) has no DCC reconciliation to
    clobber a correction, so it returns False and corrections apply immediately.
    """
    try:
        import engine as _eng
        return bool(_eng.mode_uses_api(_eng.get_data_source_mode()))
    except Exception:
        return False


def _corrections_unreconciled_count(store, from_date, to_date, channel,
                                    from_time_utc, to_time_utc, meter_id,
                                    tz_name) -> int:
    """Count in-window rate blocks NOT yet DCC-settled — the ones a gated rate
    correction will skip — so the UI can tell the user to retry after settlement.
    """
    settled_col = "imp_kwh_api" if channel == "import" else "exp_kwh_api"
    where, params, _ = _corrections_build_where(
        "rate", from_date, to_date, channel,
        from_time_utc, to_time_utc, meter_id, tz_name, api_settled_only=False)
    # Only MAIN-meter blocks settle via DCC (imp_kwh_api). Device rows never get
    # imp_kwh_api — they follow the main — so counting them here produced a
    # spurious "N blocks await settlement" warning (issue #254c).
    row = store._conn.execute(
        f"SELECT COUNT(*) AS n FROM blocks WHERE {where} AND {settled_col} IS NULL "
        f"AND meter_id IN (SELECT meter_id FROM meters WHERE is_sub_meter = 0)",
        params).fetchone()
    return int((row["n"] if row else 0) or 0)


_CHARGE_SESSION_BRIDGE_MIN = 180  # minutes; merge bursts within one plug-in period
# Dispatch source vocabulary (mirrors engine._SMART_CHARGE_SOURCES). A smart-charge
# dispatch bills off-peak; a bump/boost bills at peak; unknown/null can't be confirmed
# and shows provisional. Used to colour the card by dispatch semantics rather than the
# settlement-laggy block rate.
_SMART_SOURCES = {"smart-charge", "smart"}
_BUMP_SOURCES = {"bump-charge", "boost"}
# The "upcoming" plan is a live forecast that IOG revises. dispatch_history keeps
# every planned slot it ever saw (so absent stays meaningful for *completed*), but
# a slot dropped from a revised plan must not keep inflating the upcoming total.
# So an upcoming planned slot only counts if it was re-seen in (or very near) the
# most-recent planned observation — dispatches are polled ≥5 min apart, so a
# superseded slot's last_seen falls a full poll behind the current plan's.
_UPCOMING_BATCH_SEC = 150   # slots within this of the freshest plan = the current plan
_UPCOMING_STALE_MIN = 20    # if the freshest plan itself is older than this, no live plan


def _slot_is_off_peak(rate, peak_r):
    """A slot billed below the day's peak rate is off-peak. Unknown rate → treat
    as off-peak (no billing data to contradict it, avoids false peak flags)."""
    if rate is None or peak_r is None:
        return True
    return rate < peak_r - 1e-6


def _build_charge_sessions(history, rates, day_peak, slot_kwh=None,
                           bridge_min=_CHARGE_SESSION_BRIDGE_MIN):
    """BL-10: fold *delivered* IOG dispatch slots into charging sessions.

    Pure — no I/O, so it is unit-testable. Dispatch data identifies *which* slots
    were smart-charged (a ``completed`` row); planned-but-never-charged forecasts
    are dropped so the card reflects real charging. Each slot's **energy** is the
    metered grid import for that half-hour (``slot_kwh``) so the card's kWh,
    off-peak/peak split and savings reconcile with the billing charts; when a
    block isn't present yet (unsettled), it falls back to the dispatch figure.
    Each slot is coloured by its **actual billed rate** (``rates`` / ``day_peak``),
    so a slot priced at the day's peak reads peak, not off-peak.

    `history` is a list of dispatch_history rows (slot_start, kind, energy_kwh,
    raw_start?, raw_end?, provider). `rates` maps slot_start → the main meter's
    billed imp_rate for that half-hour. `slot_kwh` maps slot_start → that half-
    hour's metered import (kWh). `day_peak` maps a date (YYYY-MM-DD) → that day's
    highest main rate. The saving is the off-peak benefit vs the day's peak:
    ``max(0, day_peak - slot_rate) * kWh`` per slot (peak-billed slots contribute
    nothing). Consecutive delivered slots no more than ``bridge_min`` minutes
    apart form one session, bridging IOG's inter-burst gaps within a single
    plug-in. Windows use BL-11's second-precision raw_start/raw_end where present,
    else the 30-min slot boundaries. Returns sessions newest-first.
    """
    slot_kwh = slot_kwh or {}
    from datetime import datetime as _dt, timedelta as _td
    slots = {}
    for r in history:
        s = slots.setdefault(r["slot_start"], {
            "started": False, "completed": None,
            "raw_start": None, "raw_end": None, "source": None,
            "pstart": None, "pend": None, "provider": r.get("provider")})
        k = r.get("kind")
        if k == "completed" and r.get("energy_kwh") is not None:
            s["completed"] = r.get("energy_kwh")
        elif k == "started":
            s["started"] = True
        # The smart-charge vs bump/boost signal lives on the PLANNED/started record;
        # completed dispatches report source=null, so capture the source here (used
        # to colour the slot by dispatch semantics, not the settlement-laggy rate).
        if k in ("planned", "started") and r.get("source"):
            s["source"] = r.get("source")
        if r.get("raw_start"):
            s["raw_start"] = r["raw_start"]
        if r.get("raw_end"):
            s["raw_end"] = r["raw_end"]
        # The PLANNED window carries Octopus's real, sub-slot-precise dispatch bounds
        # (e.g. a dispatch that finished early reads 04:30–04:48, and a short one
        # 15:27–15:30) — whereas completedDispatches are padded to the 30-min slot.
        # Octopus re-plans continuously, but once a slot completes its planned row
        # stops updating, so this is the FROZEN last plan for that slot. Tracked
        # separately and used (clipped per slot) for dispatch_minutes below.
        if k == "planned":
            if r.get("raw_start"):
                s["pstart"] = r["raw_start"]
            if r.get("raw_end"):
                s["pend"] = r["raw_end"]

    # Delivered slots only — energy that actually flowed.
    delivered = sorted(k for k, v in slots.items() if v["completed"] is not None)
    if not delivered:
        return []

    groups, cur, prev = [], [], None
    for k in delivered:
        dt = _dt.fromisoformat(k)
        if prev is not None and (dt - prev) > _td(minutes=bridge_min):
            groups.append(cur); cur = []
        cur.append(k); prev = dt
    if cur:
        groups.append(cur)

    out = []
    for grp in groups:
        first, last = grp[0], grp[-1]
        end = (_dt.fromisoformat(last) + _td(minutes=30)).isoformat()
        exact_start = min((slots[k]["raw_start"] or k) for k in grp)
        exact_end = max(
            (slots[k]["raw_end"] or (_dt.fromisoformat(k) + _td(minutes=30)).isoformat())
            for k in grp)
        # Time actually spent charging = the merged UNION of each delivered slot's
        # PLANNED dispatch window, CLIPPED to that slot. Planned windows carry
        # Octopus's real sub-slot bounds (a dispatch that finished early reads
        # 04:30–04:48, not a padded 04:30–05:00; a 3-minute top-up reads 15:27–15:30)
        # where completedDispatches are always slot-aligned. Clipping to the slot
        # keeps a multi-hour plan from bleeding across a gap into an un-delivered
        # half-hour, and only DELIVERED slots are counted, so idle gaps between
        # bursts are excluded. Rate-limit-proof: never divides by energy. Falls back
        # to the completed/slot bounds when no planned window was captured.
        _ivs = []
        for k in grp:
            _ks = _dt.fromisoformat(k)
            _ke = _ks + _td(minutes=30)
            _a = _dt.fromisoformat(slots[k]["pstart"] or slots[k]["raw_start"] or k)
            _b = _dt.fromisoformat(
                slots[k]["pend"] or slots[k]["raw_end"] or _ke.isoformat())
            _a = max(_a, _ks)   # clip to this half-hour so a long plan can't bleed
            _b = min(_b, _ke)
            if _b > _a:
                _ivs.append((_a, _b))
        _ivs.sort()
        _merged = []
        for _a, _b in _ivs:
            if _merged and _a <= _merged[-1][1]:
                _merged[-1] = (_merged[-1][0], max(_merged[-1][1], _b))
            else:
                _merged.append((_a, _b))
        dispatch_minutes = round(
            sum((_b - _a).total_seconds() for _a, _b in _merged) / 60.0)
        kwh = op = pk = saving = 0.0
        started_slots = 0
        fill = []
        for k in grp:
            # Metered grid import for the half-hour (reconciles with the bill);
            # fall back to the dispatch figure when the block isn't settled yet.
            mv = slot_kwh.get(k)
            v = abs(mv) if mv is not None else abs(slots[k]["completed"])
            kwh += v
            peak_r = day_peak.get(k[:10])
            rate = rates.get(k)
            # Colour/label from the DISPATCH SEMANTICS where they're known, so a
            # just-completed smart charge reads off-peak IMMEDIATELY instead of
            # flashing peak until the overlay reprices it (the "red flash"). On IOG a
            # smart-charge dispatch is off-peak; a bump/boost bills at peak. When the
            # source is unknown (e.g. OHME, or a completed-only slot), fall back to the
            # settled block rate — which is authoritative once it lands.
            _src = (slots[k].get("source") or "").lower()
            if _src in _BUMP_SOURCES:            # TODO: or over-6h cap on the cap tariff
                is_op = False
            elif _src in _SMART_SOURCES:
                is_op = True
            else:
                is_op = _slot_is_off_peak(rate, peak_r)
            if is_op:
                op += v
                # Off-peak benefit vs the day's peak rate (firms up once settled).
                if peak_r is not None and rate is not None and peak_r > rate:
                    saving += (peak_r - rate) * v
            else:
                pk += v
            if slots[k]["started"]:
                started_slots += 1
            fill.append({"slot": k, "kwh": round(v, 3), "off_peak": is_op})
        # ≈charging ESTIMATE: infer the car's active power from the FULLEST
        # delivered half-hour (a fully-saturated slot ≈ true charge power) and
        # divide total delivered energy by it — the peak-slot form of energy÷power
        # (algebraically 30 × kWh ÷ max_slot_kWh). Validated 2026-08-07: 5 slots,
        # 11.41 kWh, peak 6.36 kW → 1.79 h vs a measured 1.77 h. This is a tighter
        # "how long did it actually charge" than the dispatched window; the two are
        # shown side by side (est ≤ dispatched always). Guards: need a real anchor
        # slot to infer power (else leave None — card shows only the window); clamp
        # inferred power to a plausible ceiling so a glitch slot can't crash the
        # time; never exceed the dispatched window.
        max_e = max((f["kwh"] for f in fill), default=0.0)
        charge_min = round(30.0 * kwh / max_e) if max_e > 1e-9 else 0
        _ANCHOR_KWH = 0.4    # ~0.8 kW half-hour — below this we can't infer power
        _MAX_KW = 25.0       # plausibility ceiling for a domestic charger
        active_kw = min(round(2.0 * max_e, 2), _MAX_KW)
        if max_e >= _ANCHOR_KWH and active_kw > 1e-9:
            est_charge_minutes = min(round(60.0 * kwh / active_kw), dispatch_minutes)
        else:
            active_kw = est_charge_minutes = None
        out.append({
            "start": first, "end": end,
            "exact_start": exact_start, "exact_end": exact_end,
            "kwh": round(kwh, 3), "n_slots": len(grp),
            "started_slots": started_slots, "status": "completed",
            "off_peak_kwh": round(op, 3), "peak_kwh": round(pk, 3),
            "charge_minutes": charge_min,
            "dispatch_minutes": dispatch_minutes,
            "est_charge_minutes": est_charge_minutes,
            "active_kw": active_kw,
            "saving": round(saving, 4) if saving > 1e-9 else None,
            "provider": slots[first]["provider"],
            "fill": fill,
        })
    out.reverse()   # newest-first
    return out


def _build_upcoming_dispatches(history, rates, day_peak, now_iso,
                               bridge_min=_CHARGE_SESSION_BRIDGE_MIN):
    """BL-10: fold *future planned* IOG dispatch slots into upcoming sessions.

    Complements _build_charge_sessions. A slot qualifies if it carries a planned
    forecast, has not yet completed, and has not fully elapsed (its 30-min end is
    at/after now_iso). Each slot is coloured by the tariff rate for that half-hour
    (`rates` / `day_peak`), so a plan landing in a peak half-hour reads peak.
    Consecutive slots ≤ bridge_min apart form one session. Returns sessions
    soonest-first, each with status 'scheduled' and saving None (a forecast).
    """
    from datetime import datetime as _dt, timedelta as _td
    now = _dt.fromisoformat(now_iso)
    slots = {}
    for r in history:
        s = slots.setdefault(r["slot_start"], {
            "planned": None, "completed": None, "planned_seen": None,
            "raw_start": None, "raw_end": None, "provider": r.get("provider")})
        k = r.get("kind")
        if k == "planned" and r.get("energy_kwh") is not None:
            s["planned"] = r.get("energy_kwh")
            if r.get("last_seen"):
                s["planned_seen"] = r["last_seen"]
        elif k == "completed" and r.get("energy_kwh") is not None:
            s["completed"] = r.get("energy_kwh")
        if r.get("raw_start"):
            s["raw_start"] = r["raw_start"]
        if r.get("raw_end"):
            s["raw_end"] = r["raw_end"]

    # Only the *current* plan is upcoming: keep planned slots re-seen in (or within
    # _UPCOMING_BATCH_SEC of) the freshest planned observation, so slots a revised
    # plan dropped don't linger and inflate the total. If the freshest plan itself
    # is older than _UPCOMING_STALE_MIN, there's no live plan → nothing upcoming.
    # When last_seen is absent (older data / unit tests), skip this filter.
    _seens = [v["planned_seen"] for v in slots.values()
              if v["planned"] is not None and v["planned_seen"]]
    _batch_cutoff = None
    if _seens:
        _newest = max(_dt.fromisoformat(s) for s in _seens)
        if (now - _newest) > _td(minutes=_UPCOMING_STALE_MIN):
            return []
        _batch_cutoff = _newest - _td(seconds=_UPCOMING_BATCH_SEC)

    def _is_current(v):
        if _batch_cutoff is None or not v["planned_seen"]:
            return True
        return _dt.fromisoformat(v["planned_seen"]) >= _batch_cutoff

    upcoming = sorted(
        k for k, v in slots.items()
        if v["planned"] is not None and v["completed"] is None
        and _dt.fromisoformat(k) + _td(minutes=30) > now
        and _is_current(v))
    if not upcoming:
        return []

    groups, cur, prev = [], [], None
    for k in upcoming:
        dt = _dt.fromisoformat(k)
        if prev is not None and (dt - prev) > _td(minutes=bridge_min):
            groups.append(cur); cur = []
        cur.append(k); prev = dt
    if cur:
        groups.append(cur)

    out = []
    for grp in groups:
        first, last = grp[0], grp[-1]
        end = (_dt.fromisoformat(last) + _td(minutes=30)).isoformat()
        exact_start = min((slots[k]["raw_start"] or k) for k in grp)
        exact_end = max(
            (slots[k]["raw_end"] or (_dt.fromisoformat(k) + _td(minutes=30)).isoformat())
            for k in grp)
        kwh = op = pk = 0.0
        fill = []
        for k in grp:
            v = abs(slots[k]["planned"])
            kwh += v
            is_op = _slot_is_off_peak(rates.get(k), day_peak.get(k[:10]))
            if is_op:
                op += v
            else:
                pk += v
            fill.append({"slot": k, "kwh": round(v, 3), "off_peak": is_op})
        max_e = max((f["kwh"] for f in fill), default=0.0)
        charge_min = round(30.0 * kwh / max_e) if max_e > 1e-9 else 0
        out.append({
            "start": first, "end": end,
            "exact_start": exact_start, "exact_end": exact_end,
            "kwh": round(kwh, 3), "n_slots": len(grp),
            "started_slots": 0, "status": "scheduled",
            "off_peak_kwh": round(op, 3), "peak_kwh": round(pk, 3),
            "charge_minutes": charge_min,
            "saving": None, "provider": slots[first]["provider"],
            "fill": fill,
        })
    return out   # soonest-first


@app.route("/api/charge-sessions", methods=["GET"])
def api_charge_sessions():
    """BL-10: smart-charge sessions from dispatch_history for the Overview card.

    Gated on there being dispatch data (IOG). Query `days` (default 14) sets the
    lookback. Returns {sessions, upcoming, has_data, currency}. `sessions` are
    delivered charges (newest-first); `upcoming` are future planned dispatches
    (soonest-first). Each carries kWh, local windows, and a per-slot fill curve.
    """
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime as _d2, timedelta as _t2, timezone as _tzc
        store = store_access.store()
        try:
            days = max(1, min(int(request.args.get("days", 14)), 90))
        except (TypeError, ValueError):
            days = 14

        cfg = load_config()
        main_meta, main_id = {}, None
        for mid, md in cfg.get("meters", {}).items():
            if not (md.get("meta") or {}).get("sub_meter"):
                main_meta, main_id = md.get("meta") or {}, mid
                break
        main_id = main_id or "electricity_main"
        tz_name = main_meta.get("timezone", "UTC")
        currency = main_meta.get("currency_symbol", "£")

        now = _d2.now(_tzc.utc).replace(tzinfo=None)
        win_start = (now - _t2(days=days)).isoformat()
        win_end = (now + _t2(days=2)).isoformat()   # +2d captures tonight's plan

        history = store.get_dispatch_history(win_start, win_end)
        if not history:
            return jsonify({"sessions": [], "upcoming": [],
                            "has_data": False, "currency": currency})

        # Energy source = the EV's charge, from the supplier's dispatch figure —
        # what the car actually charged, without the house baseload the meter's
        # own import carries. (A future EV sub-meter could supply a bill-
        # reconciled per-slot figure via `slot_kwh`; left None here.) `rates` /
        # `day_peak` still come from the billing blocks so pricing/colouring
        # match the bill.
        rates = {r["block_start"]: r["imp_rate"] for r in store._conn.execute(
            "SELECT block_start, imp_rate FROM blocks WHERE meter_id = ? "
            "AND block_start >= ? AND block_start < ? AND imp_rate IS NOT NULL",
            (main_id, win_start, win_end))}
        day_peak = {r["d"]: r["p"] for r in store._conn.execute(
            "SELECT substr(block_start,1,10) d, MAX(imp_rate) p FROM blocks "
            "WHERE meter_id = ? AND block_start >= ? AND block_start < ? "
            "AND imp_rate IS NOT NULL GROUP BY d", (main_id, win_start, win_end))}

        sessions = _build_charge_sessions(history, rates, day_peak)

        # Attach local-time display strings (UTC→site tz), like the review list.
        try:
            _tz = ZoneInfo(tz_name)
        except Exception:
            _tz = ZoneInfo("UTC")

        def _loc(iso):
            try:
                d = _d2.fromisoformat(iso).replace(tzinfo=ZoneInfo("UTC")).astimezone(_tz)
                return {"date": d.strftime("%Y-%m-%d"), "time": d.strftime("%H:%M")}
            except Exception:
                return {"date": None, "time": None}

        upcoming = _build_upcoming_dispatches(history, rates, day_peak, now.isoformat())

        for s in sessions + upcoming:
            s["local"] = {
                "start": _loc(s["exact_start"]), "end": _loc(s["exact_end"])}
            # Localise each per-slot label too (UTC->site tz), so the fill-chart bar
            # times match the summary window instead of reading an hour off under BST.
            for f in s.get("fill", []):
                f["local_time"] = _loc(f["slot"])["time"]

        return jsonify({"sessions": sessions, "upcoming": upcoming,
                        "has_data": True, "currency": currency})
    except Exception as e:
        logger.error("api_charge_sessions: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/corrections/meters", methods=["GET"])
def api_corrections_meters():
    """Return distinct meter_ids present in the blocks table for the corrections UI."""
    try:
        store = store_access.store()
        rows = store._conn.execute(
            "SELECT DISTINCT meter_id FROM blocks ORDER BY meter_id"
        ).fetchall()
        return jsonify({"meters": [r["meter_id"] for r in rows]})
    except Exception as e:
        logger.error("api_corrections_meters: %s", e)
        return jsonify({"error": str(e)}), 500


# Days of blocks either side of the correction window to sample rates from.
_NEARBY_RATES_PAD_DAYS = 3


@app.route("/api/corrections/nearby-rates", methods=["GET"])
def api_corrections_nearby_rates():
    """#270: the distinct main-meter rates in force in blocks SURROUNDING a
    correction window, so the user can pick the exact stored value instead of
    re-typing it (avoiding fat-finger / decimal-place / rounding errors).

    Query: channel=import|export, from_date=YYYY-MM-DD, to_date=YYYY-MM-DD.
    Samples blocks in [from_date - N days, to_date + N days] (N = 3), grouped by
    the exact stored rate, ranked by how many blocks carry it (most common first)
    so the everyday off-peak/peak values surface at the top. Full precision — the
    UI fills the value verbatim.
    """
    try:
        channel   = request.args.get("channel", "import")
        from_date = request.args.get("from_date", "")
        to_date   = request.args.get("to_date", "")
        if channel not in ("import", "export"):
            return jsonify({"error": "channel must be 'import' or 'export'"}), 400
        if not from_date or not to_date:
            return jsonify({"error": "from_date and to_date required"}), 400

        from datetime import date as _date, timedelta as _td
        from block_store import local_date_range_to_utc_bounds as _utc_bounds

        store = store_access.store()
        cfg = load_config()
        main_meta, main_id = {}, None
        for mid, md in cfg.get("meters", {}).items():
            if not (md.get("meta") or {}).get("sub_meter"):
                main_meta, main_id = md.get("meta") or {}, mid
                break
        if main_id is None:
            row = store._conn.execute(
                "SELECT meter_id FROM meters WHERE is_sub_meter = 0 "
                "ORDER BY meter_id LIMIT 1").fetchone()
            main_id = row["meter_id"] if row else "electricity_main"
        tz_name = main_meta.get("timezone", "UTC")

        # Pad the window by a few days each side to sample surrounding rates.
        pad = _td(days=_NEARBY_RATES_PAD_DAYS)
        win_from = (_date.fromisoformat(from_date) - pad).isoformat()
        win_to   = (_date.fromisoformat(to_date)   + pad).isoformat()
        utc_start, utc_end = _utc_bounds(win_from, win_to, tz_name)

        rate_col = "imp_rate" if channel == "import" else "exp_rate"
        rows = store._conn.execute(
            f"""SELECT {rate_col} AS rate, COUNT(*) AS n
                FROM blocks
                WHERE meter_id = ? AND block_start >= ? AND block_start < ?
                  AND {rate_col} IS NOT NULL AND {rate_col} > 0
                GROUP BY {rate_col}
                ORDER BY n DESC, {rate_col}""",
            (main_id, utc_start, utc_end)).fetchall()
        rates = _cluster_nearby_rates(
            [{"rate": r["rate"], "count": r["n"]} for r in rows])
        return jsonify({"rates": rates, "channel": channel,
                        "window": [win_from, win_to]})
    except Exception as e:
        logger.error("api_corrections_nearby_rates: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/corrections/preview", methods=["POST"])
def api_corrections_preview():
    """
    Preview what a correction would affect — returns per-block detail for rate
    corrections so the user can verify exactly which blocks will change.

    Body: {
      type: "standing"|"rate",
      from_date: "YYYY-MM-DD", to_date: "YYYY-MM-DD",
      value: float,
      channel: "import"|"export"  (rate only),
      from_time: "HH:MM"          (optional local time — start of window),
      to_time:   "HH:MM"          (optional local time — end of window, exclusive),
      meter_id:  "all"|"<meter>"  (optional — default "all")
    }

    Returns for standing: { days, blocks, current_min, current_max }
    Returns for rate:     { blocks: [{block_start, meter_id, current_rate,
                                       new_rate, kwh, current_cost, new_cost}] }
    """
    try:
        data       = request.get_json(force=True) or {}
        corr_type  = data.get("type")
        from_date  = data.get("from_date", "")
        to_date    = data.get("to_date", "")
        channel    = data.get("channel", "import")
        from_time  = data.get("from_time", "")   # local HH:MM
        to_time    = data.get("to_time", "")     # local HH:MM
        meter_id   = data.get("meter_id", "all")
        value      = data.get("value")

        if corr_type not in ("standing", "rate"):
            return jsonify({"error": "type must be 'standing' or 'rate'"}), 400
        if not from_date or not to_date:
            return jsonify({"error": "from_date and to_date required"}), 400

        store = store_access.store()      # reads only; the write goes through _engine_write
        cfg   = load_config()
        main_meta = {}
        for md in cfg.get("meters", {}).values():
            if not (md.get("meta") or {}).get("sub_meter"):
                main_meta = md.get("meta") or {}
                break
        tz_name = main_meta.get("timezone", "UTC")

        # Use from_date as DST anchor so March/BST offsets are correct
        from_time_utc = _corrections_time_to_utc(from_time, tz_name, from_date) if from_time else ""
        to_time_utc   = _corrections_time_to_utc(to_time,   tz_name, from_date) if to_time   else ""

        where, params, col = _corrections_build_where(
            corr_type, from_date, to_date, channel,
            from_time_utc, to_time_utc, meter_id, tz_name
        )

        # API+ gate: rate corrections only touch DCC-settled blocks (see
        # _corrections_build_where). Pure CAD applies immediately.
        gate = corr_type == "rate" and _corrections_api_gate_active()
        skipped = 0
        if gate:
            where, params, col = _corrections_build_where(
                corr_type, from_date, to_date, channel,
                from_time_utc, to_time_utc, meter_id, tz_name,
                api_settled_only=True
            )
            skipped = _corrections_unreconciled_count(
                store, from_date, to_date, channel,
                from_time_utc, to_time_utc, meter_id, tz_name)

        if corr_type == "standing":
            cur = store._conn.execute(
                f"""SELECT COUNT(DISTINCT date(block_start)) as days,
                          COUNT(*) as blocks,
                          MIN(standing_charge) as cur_min,
                          MAX(standing_charge) as cur_max
                   FROM blocks WHERE {where}""",
                params
            )
            row = cur.fetchone()
            return jsonify({
                "days":        row["days"]    or 0,
                "blocks":      row["blocks"]  or 0,
                "current_min": round(float(row["cur_min"] or 0), 6),
                "current_max": round(float(row["cur_max"] or 0), 6),
            })

        else:  # rate — per-block detail; device rows follow the main (issue #254b)
            from zoneinfo import ZoneInfo
            from datetime import datetime, timezone as _tz
            tz = ZoneInfo(tz_name)
            kwh_col  = "imp_kwh"  if channel == "import" else "exp_kwh"
            cost_col = "imp_cost" if channel == "import" else "exp_cost"

            def _display(bs):
                try:
                    return (datetime.fromisoformat(bs).replace(tzinfo=_tz.utc)
                            .astimezone(tz).strftime("%d/%m %H:%M"))
                except Exception:
                    return bs

            def _row(bs, mid, cur_rate, kwh, cur_cost):
                nc = round(float(kwh or 0) * float(value), 6) if value is not None else None
                return {"block_start": bs, "display": _display(bs), "meter_id": mid,
                        "current_rate": round(float(cur_rate or 0), 6),
                        "new_rate": float(value) if value is not None else None,
                        "kwh": round(float(kwh or 0), 6),
                        "current_cost": round(float(cur_cost or 0), 6), "new_cost": nc}

            blocks_out = []
            # MAIN blocks (settled gate applied in `where`) — cost basis = imp_kwh
            main_rows = store._conn.execute(
                f"""SELECT block_start, meter_id, {col} as current_rate,
                           {kwh_col} as kwh, {cost_col} as current_cost
                    FROM blocks WHERE {where} ORDER BY block_start, meter_id""",
                params).fetchall()
            for r in main_rows:
                blocks_out.append(_row(r["block_start"], r["meter_id"],
                                       r["current_rate"], r["kwh"], r["current_cost"]))

            # DEVICE rows that will follow the correction (import only). Cost basis
            # is the grid-attributed kWh (imp_kwh_grid), exactly what PASS 2 uses,
            # so the preview matches what apply writes.
            if channel == "import" and main_rows:
                bs_list = [r["block_start"] for r in main_rows]
                ph = ",".join("?" * len(bs_list))
                dev_rows = store._conn.execute(
                    f"""SELECT block_start, meter_id, imp_rate as current_rate,
                               COALESCE(imp_kwh_grid, imp_kwh) as kwh, imp_cost as current_cost
                        FROM blocks
                        WHERE block_start IN ({ph}) AND imp_rate IS NOT NULL
                          AND meter_id IN (SELECT meter_id FROM meters WHERE is_sub_meter = 1)
                        ORDER BY block_start, meter_id""", bs_list).fetchall()
                for r in dev_rows:
                    blocks_out.append(_row(r["block_start"], r["meter_id"],
                                           r["current_rate"], r["kwh"], r["current_cost"]))

            blocks_out.sort(key=lambda b: (b["block_start"], b["meter_id"]))
            return jsonify({"blocks": blocks_out,
                            "skipped_unreconciled": skipped,
                            "api_gated": gate})

    except Exception as e:
        logger.error("api_corrections_preview: %s", e)
        return jsonify({"error": str(e)}), 500


def _corrections_multiband_count(store, where, params):
    """Count main-meter blocks in the correction range whose IMPORT segments span more than one
    rate band (a cap-transition / 4-rate block). A single-rate correction flattens that split
    (UPDATE block_segments SET inc_rate = value), so the tool guards against it on IOG. Segments
    only exist on 4.4.0+ segmented accounts; returns 0 otherwise."""
    try:
        if not store._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='block_segments'"
                ).fetchone():
            return 0
        starts = [r[0] for r in store._conn.execute(
            f"SELECT DISTINCT block_start FROM blocks WHERE {where}", params).fetchall()]
        if not starts:
            return 0
        ph = ",".join("?" * len(starts))
        # A GENUINE multi-rate (cap-transition) block spans a real band gap (off-peak vs peak,
        # ~0.27) -- not a same-rate EV/house label pair (peak vs day), nor float/rounding noise
        # (~1e-5). Gate on the rate SPREAD, well above noise and below the off/peak gap.
        row = store._conn.execute(
            "SELECT COUNT(*) FROM (SELECT block_start FROM block_segments "
            "  WHERE channel = 'import' AND inc_rate IS NOT NULL AND block_start IN (" + ph + ") "
            "  GROUP BY block_start HAVING (MAX(inc_rate) - MIN(inc_rate)) > 0.001)",
            starts).fetchone()
        return int(row[0]) if row and row[0] else 0
    except Exception as e:
        logger.warning("_corrections_multiband_count: %s", e)
        return 0


@app.route("/api/corrections/apply", methods=["POST"])
def api_corrections_apply():
    """
    Apply a standing-charge or rate correction to the live database.
    Body: {
      type: "standing"|"rate",
      from_date: "YYYY-MM-DD", to_date: "YYYY-MM-DD",
      value: float,
      channel: "import"|"export"  (rate only),
      recalc_cost: bool           (rate only — recalculate cost from rate × kwh),
      from_time: "HH:MM"          (optional local time — start of window),
      to_time:   "HH:MM"          (optional local time — end of window, exclusive),
      meter_id:  "all"|"<meter>"  (optional — default "all")
    }
    Returns: { updated_blocks: int }
    """
    try:
        data        = request.get_json(force=True) or {}
        corr_type   = data.get("type")
        from_date   = data.get("from_date", "")
        to_date     = data.get("to_date", "")
        value       = data.get("value")
        channel     = data.get("channel", "import")
        recalc_cost = bool(data.get("recalc_cost", True))
        from_time   = data.get("from_time", "")
        to_time     = data.get("to_time", "")
        meter_id    = data.get("meter_id", "all")

        if corr_type not in ("standing", "rate"):
            return jsonify({"error": "type must be 'standing' or 'rate'"}), 400
        if not from_date or not to_date:
            return jsonify({"error": "from_date and to_date required"}), 400
        if value is None:
            return jsonify({"error": "value required"}), 400

        value = float(value)
        if value < 0:
            return jsonify({"error": "value must be >= 0"}), 400

        store = store_access.store()      # reads only; the write goes through _engine_write
        cfg   = load_config()
        main_meta = {}
        for md in cfg.get("meters", {}).values():
            if not (md.get("meta") or {}).get("sub_meter"):
                main_meta = md.get("meta") or {}
                break
        tz_name = main_meta.get("timezone", "UTC")

        # Use from_date as DST anchor so March/BST offsets are correct
        from_time_utc = _corrections_time_to_utc(from_time, tz_name, from_date) if from_time else ""
        to_time_utc   = _corrections_time_to_utc(to_time,   tz_name, from_date) if to_time   else ""

        where, params, col = _corrections_build_where(
            corr_type, from_date, to_date, channel,
            from_time_utc, to_time_utc, meter_id, tz_name
        )

        # IOG multi-band guard (4.5.7): a single-rate correction flattens a cap-transition
        # block's 4-rate split (the segment UPDATE collapses EV/house peak/off-peak into one
        # rate). Refuse when the range includes such a block unless the caller explicitly
        # confirms. Single-band blocks (flat / Agile / E7) and standing corrections are unaffected.
        if corr_type == "rate" and not bool(data.get("confirm_multiband")):
            _mb = _corrections_multiband_count(store, where, params)
            if _mb:
                return jsonify({
                    "error": "multiband_block",
                    "multiband_blocks": _mb,
                    "message": (f"{_mb} block(s) in this range have a multi-rate "
                                "(cap-transition) split. A single rate would flatten the "
                                "EV / house peak / off-peak breakdown \u2014 let settlement "
                                "price these, or re-submit with confirmation to override."),
                }), 409

        # API+ gate: a rate correction only touches DCC-settled blocks; unsettled
        # ones would be clobbered at settlement, so skip + report them. Pure CAD
        # applies immediately.
        gate = corr_type == "rate" and _corrections_api_gate_active()
        skipped = 0
        if gate:
            where, params, col = _corrections_build_where(
                corr_type, from_date, to_date, channel,
                from_time_utc, to_time_utc, meter_id, tz_name,
                api_settled_only=True
            )
            skipped = _corrections_unreconciled_count(
                store, from_date, to_date, channel,
                from_time_utc, to_time_utc, meter_id, tz_name)

        # The whole correction runs on the ENGINE thread as ONE unit.
        #
        # It is not a single statement: three alternative UPDATEs, a device-row
        # propagation, a block_segments rate rewrite and an exc recompute, with
        # explicit commits between them. Hopping each write separately would let
        # another writer interleave between the main-meter update and the device
        # rows that are supposed to follow it — the 'partial heal' the comments
        # inside already warn about, which left the 2026-08-18 EV columns 100x out.
        #
        # `updated` is the only value the rest of the handler needs back.
        def _apply_correction(st):
            if corr_type == "standing":
                cur = st._conn.execute(
                    f"UPDATE blocks SET standing_charge = ? WHERE {where}",
                    [value] + params
                )
                st._conn.commit()
                updated = cur.rowcount
                logger.info(
                    "api_corrections_apply: standing_charge=%.4f %d blocks (%s %s→%s %s→%s)",
                    value, updated, meter_id, from_date, to_date,
                    from_time or "00:00", to_time or "24:00"
                )

            else:  # rate correction
                # BL-79 (8 Oct 2026): ONE transaction for the whole correction — main rows,
                # the EV columns, device rows, the segments' rates and the ex-VAT
                # re-derivation. Each used to commit on its own, so a failure part-way left
                # the columns corrected over segments at the old rate (or the reverse).
                with st.batch():
                    kwh_col = "imp_kwh" if channel == "import" else "exp_kwh"
                    # Stamp a durable marker on manual RATE corrections so the dispatch
                    # reconciliation pass never overwrites a user's override.
                    _mark = (", rate_corrected = 1, rate_source = 'corrected'"
                             if col in ("imp_rate", "exp_rate") else "")
                    if recalc_cost and channel == "import":
                        # P4.6 (watch #11) — the corrected rate applies to the WHOLE block: imp_cost
                        # already recomputes over imp_kwh, which INCLUDES the EV kWh. The EV split is
                        # the segments (step 5c), re-priced below in this same transaction (P4.10).
                        cur = st._conn.execute(
                            f"""UPDATE blocks
                                SET imp_rate = ?,
                                    imp_cost = ROUND(imp_kwh * ?, 6),
                                    -- BL-57: block-level exc must follow the corrected inc (the
                                    -- reconcile already does this; the corrections tool didn't, so a
                                    -- corrected block kept a stale ex-VAT figure). Rescale by
                                    -- new/old inc to preserve the block's VAT ratio; leave NULL exc
                                    -- (view falls back to inc / VAT). Only the £ is stored (5b).
                                    imp_cost_exc = CASE WHEN imp_cost_exc IS NOT NULL AND imp_rate != 0
                                                       THEN ROUND(imp_cost_exc * ? / imp_rate, 6)
                                                       ELSE imp_cost_exc END{_mark}
                                WHERE {where}""",
                            [value, value, value] + params
                        )
                    elif recalc_cost:
                        cost_col = "imp_cost" if channel == "import" else "exp_cost"
                        cur = st._conn.execute(
                            f"""UPDATE blocks
                                SET {col} = ?,
                                    {cost_col} = ROUND({kwh_col} * ?, 6){_mark}
                                WHERE {where}""",
                            [value, value] + params
                        )
                    else:
                        cur = st._conn.execute(
                            f"UPDATE blocks SET {col} = ?{_mark} WHERE {where}",
                            [value] + params
                        )
                    updated = cur.rowcount

                    # Devices follow the main meter's rate, so propagate the corrected
                    # IMPORT rate onto device rows for the same blocks (issue #254b). The
                    # settled gate above only matches the main meter — device rows never
                    # carry imp_kwh_api — so without this the correction silently skipped
                    # them. Device cost = grid-attributed kWh × rate, exactly what PASS 2
                    # computes at finalise.
                    if channel == "import" and updated:
                        bs_list = [r["block_start"] for r in st._conn.execute(
                            f"SELECT DISTINCT block_start FROM blocks WHERE {where}", params)]
                        if bs_list:
                            ph = ",".join("?" * len(bs_list))
                            dev = (f"block_start IN ({ph}) AND meter_id IN "
                                   "(SELECT meter_id FROM meters WHERE is_sub_meter = 1)")
                            # v4 4.5.21 forward fix: the ex-VAT rate follows too — the main's, as the
                            # UPDATE above has just corrected it (BL-57). It didn't: a corrected device
                            # kept its pre-correction ex-VAT, so every ex-VAT view
                            # still priced it at the old band. Taken from the MAIN rather than rescaled
                            # from the device's own, so re-applying a correction also repairs a device
                            # an earlier one left stale.
                            # The main's ex-VAT rate from its own pair, as the UPDATE above left it
                            # (its imp_rate_exc column went in step 5b, 10 Oct 2026).
                            main_exc = ("(SELECT m.imp_rate * m.imp_cost_exc / NULLIF(m.imp_cost, 0) "
                                        "FROM blocks m "
                                        " WHERE m.block_start = blocks.block_start AND m.meter_id IN "
                                        "   (SELECT meter_id FROM meters WHERE is_sub_meter = 0) LIMIT 1)")
                            if recalc_cost:
                                st._conn.execute(
                                    f"UPDATE blocks SET imp_rate = ?, "
                                    f"imp_cost = ROUND(COALESCE(imp_kwh_grid, imp_kwh, 0) * ?, 6), "
                                    f"imp_cost_exc = ROUND(COALESCE(imp_kwh_grid, imp_kwh, 0) * {main_exc}, 6) "
                                    f"WHERE {dev}", [value, value] + bs_list)
                            else:
                                st._conn.execute(
                                    f"UPDATE blocks SET imp_rate = ? "
                                    f"WHERE {dev}",
                                    [value] + bs_list)

                            # P4.10 (watch #11) — on a SEGMENTED account (4.4.0+) the block's pricing
                            # ALSO lives in block_segments, which the segment-based surfaces read
                            # (Usage-Stats rate-tiers P4.2, the EV/house split). A rate correction that
                            # only rewrote the columns would leave segments on the stale rate. The
                            # correction flattens the block to ONE import rate (imp_cost = imp_kwh ×
                            # value), so every import segment on the affected blocks takes that rate —
                            # Σ(kwh×inc_rate) == imp_cost still holds (Σ kwh == imp_kwh is unchanged).
                            # Segment exc_rate is rescaled PROPORTIONALLY (P4.18) — preserving each
                            # segment's exc/inc ratio — NOT left as-is: leaving it would break the ratio
                            # even on a normal correction (inc→0.28 but exc still 0.2857 > inc), and it
                            # is the exact hole that stopped the Cost-Corrections tool from repairing a
                            # capped block whose segments the upgrade sweep built in PENCE (segment inc
                            # AND exc both pence). `exc_rate * value / inc_rate` uses the OLD inc_rate
                            # (SQLite evaluates all RHS against the original row), so pence→£ and normal
                            # corrections both land on the right exc. Guarded on non-zero inc + non-NULL
                            # exc (NULL exc → view falls back to inc÷VAT, leave it). Gated on recalc and
                            # skipped on pre-4.4.0 DBs with no block_segments table.
                            if recalc_cost and st._conn.execute(
                                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                                    "AND name = 'block_segments'").fetchone():
                                st._conn.execute(
                                    f"UPDATE block_segments SET "
                                    f"  exc_rate = CASE WHEN exc_rate IS NOT NULL AND inc_rate != 0 "
                                    f"                  THEN ROUND(exc_rate * ? / inc_rate, 6) "
                                    f"                  ELSE exc_rate END, "
                                    f"  inc_rate = ? "
                                    f"WHERE channel = 'import' AND block_start IN ({ph})",
                                    [value, value] + bs_list)

                            # BL-57b: SUPERSEDES the proportional exc rescales above with an
                            # AUTHORITATIVE re-derivation of block + segment ex-VAT from the corrected
                            # inc via the VAT calendar (per-block vat_rate_at). Sets exc explicitly even
                            # when it was NULL (no more inc÷1.05 view-approximation) and stays correct
                            # across a VAT-rate change. Import rate corrections only.
                            if recalc_cost:
                                st.set_import_exc_from_inc(bs_list, meter_id)

                    logger.info(
                        "api_corrections_apply: %s %s_rate=%.6f recalc=%s %d main block(s) "
                        "(%s→%s %s→%s); devices follow",
                        meter_id, channel, value, recalc_cost, updated,
                        from_date, to_date, from_time or "00:00", to_time or "24:00"
                    )
            return updated

        _bk, _refuse = _safety_backup_or_refuse("pre-correction")         # #67
        if _refuse:
            return _refuse
        updated = _engine_write(_apply_correction, timeout=600.0)

        return jsonify({"ok": True, "updated_blocks": updated,
                        "skipped_unreconciled": skipped, "api_gated": gate})
    except Exception as e:
        logger.error("api_corrections_apply: %s", e)
        return jsonify({"error": str(e)}), 500