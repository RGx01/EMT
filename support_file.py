"""
support_file.py — one zip to send to whoever is helping (data management design §1 (5), PR 5;
21 Oct 2026).

The zip holds:
  emt.log       the add-on's log (as much as the caller passes);
  support.json  a summary the caller builds (version, mode, config, counts, the import audit,
                the coordinator, the bill-ask record, the last errors, and the machine — `host`);
  blocks.db     the database cut to a span: a `VACUUM INTO` copy keeping that span's rows in the
                time-indexed tables (blocks, segments, `ha_reads`, bills, dispatches, histories)
                and every config and state table whole.

**Redaction** (on by default, the user's decision): the MPANs, meter serials, the account number
and the postcode become stable placeholders — `MPAN-1`, `SERIAL-1`, `ACCOUNT-1`, `POSTCODE-1` —
the same everywhere they appear, in every text column of the copy and in the log and summary, so
they still match each other. Read from where the database keeps them (traced 21 Oct 2026 on a
staging copy): `measured_cost.mpan`; `meter_channels.mpan`, which holds `serial-MPAN`;
`kraken_state.kraken_account_number`; `meters.postcode_prefix` and `carbon_intensity.postcode`
(the outward code only). The Octopus API key is not in the database (`kraken_credentials.json`).

Nothing here touches the live database: the copy is made with `VACUUM INTO` and cut and redacted
on its own connection.
"""
import json
import os
import re
import sqlite3
import zipfile

# The columns that date a row; a table with one is cut to the span, the rest are kept whole.
TIME_COLUMNS = ("block_start", "period_start", "slot_start", "captured_at")
# A backup zip of a prod-sized database came to ~0.15 of the file (87 MB → 12.5 MB).
ZIP_RATIO = 0.15
LOG_RATIO = 0.12


def identifiers(conn) -> dict:
    """{identifier: placeholder} for what the database holds (see the module docstring), longest
    first, so a serial-MPAN pair is replaced in its parts."""
    found = {"MPAN": [], "SERIAL": [], "ACCOUNT": [], "POSTCODE": []}

    def add(kind, v):
        v = (v or "").strip().strip('"')
        if len(v) >= 3 and v not in found[kind]:
            found[kind].append(v)

    def rows(sql):
        try:
            return conn.execute(sql).fetchall()
        except sqlite3.Error:
            return []
    for (v,) in rows("SELECT DISTINCT mpan FROM measured_cost WHERE mpan IS NOT NULL"):
        add("MPAN", v)
    for (v,) in rows("SELECT DISTINCT mpan FROM meter_channels WHERE mpan IS NOT NULL AND mpan != ''"):
        if "-" in v:
            serial, mpan = v.split("-", 1)
            add("SERIAL", serial)
            add("MPAN", mpan)
        else:
            add("MPAN", v)
    # the stored agreements (G1): an export MPAN may be held nowhere else
    for (v,) in rows("SELECT DISTINCT mpan FROM tariff_agreements WHERE mpan IS NOT NULL"):
        add("MPAN", v)
    for (v,) in rows("SELECT DISTINCT account FROM tariff_agreements WHERE account IS NOT NULL"):
        add("ACCOUNT", v)
    for (v,) in rows("SELECT value FROM kraken_state WHERE key = 'kraken_account_number'"):
        try:
            v = json.loads(v)
        except (TypeError, ValueError):
            pass
        add("ACCOUNT", str(v) if v is not None else "")
    for (v,) in rows("SELECT DISTINCT postcode_prefix FROM meters WHERE postcode_prefix IS NOT NULL"):
        add("POSTCODE", v)
    for (v,) in rows("SELECT DISTINCT postcode FROM carbon_intensity WHERE postcode IS NOT NULL"):
        add("POSTCODE", v)
    out = {}
    for kind, vals in found.items():
        for i, v in enumerate(vals, 1):
            out[v] = "%s-%d" % (kind, i)
    return dict(sorted(out.items(), key=lambda kv: -len(kv[0])))


def redact_text(text: str, mapping: dict) -> str:
    for v, ph in (mapping or {}).items():
        text = text.replace(v, ph)
    return text


def _tables(conn) -> list:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]


def _cols(conn, t) -> list:
    return [(r[1], (r[2] or "").upper()) for r in conn.execute('PRAGMA table_info("%s")' % t)]


def estimate(conn, lo, hi, db_bytes: int, log_bytes: int = 0) -> dict:
    """The zip's size before it is built: the database at a backup's compression, scaled by the
    span's share of every table's rows — a time-indexed table by its rows inside the span, the
    rest whole (the blocks alone misled: on a staging copy the rolling power and SoC histories,
    two days dense, were most of a 30-day file) — and the log at its own. {half_hours, bytes}."""
    total = inside = 0
    for t in _tables(conn):
        cols = [c for c, _ in _cols(conn, t)]
        n = conn.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0] or 0
        tc = next((c for c in TIME_COLUMNS if c in cols), None)
        if tc:
            where, args = _span_where(tc, lo, hi)
            k = conn.execute('SELECT COUNT(*) FROM "%s"' % t + where, args).fetchone()[0] or 0
        else:
            k = n
        total += n; inside += k
    where, args = _span_where("block_start", lo, hi)
    hh = conn.execute("SELECT COUNT(DISTINCT block_start) FROM blocks" + where, args).fetchone()[0] or 0
    share = (inside / total) if total else 0.0
    return {"half_hours": hh, "bytes": int(db_bytes * share * ZIP_RATIO + log_bytes * LOG_RATIO)}


def _span_where(col, lo, hi):
    parts, args = [], []
    if lo:
        parts.append('"%s" >= ?' % col); args.append(lo)
    if hi:
        parts.append('"%s" < ?' % col); args.append(hi)
    return (" WHERE " + " AND ".join(parts)) if parts else "", args


def cut(src_path: str, dst_path: str, lo=None, hi=None, mapping=None) -> dict:
    """A copy of the database at `dst_path`, cut to [lo, hi) — `lo`/`hi` naive-UTC ISO, either
    None for no bound — and redacted with `mapping`. {table: rows kept} for the cut tables."""
    if os.path.exists(dst_path):
        os.remove(dst_path)
    src = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(src_path), uri=True)
    try:
        src.execute("VACUUM INTO ?", (dst_path,))
    finally:
        src.close()
    db = sqlite3.connect(dst_path)
    kept = {}
    try:
        with db:
            for t in _tables(db):
                cols = [c for c, _ in _cols(db, t)]
                tc = next((c for c in TIME_COLUMNS if c in cols), None)
                if tc and (lo or hi):
                    parts, args = [], []
                    if lo:
                        parts.append('"%s" < ?' % tc); args.append(lo)
                    if hi:
                        parts.append('"%s" >= ?' % tc); args.append(hi)
                    db.execute('DELETE FROM "%s" WHERE %s' % (t, " OR ".join(parts)), args)
                if tc:
                    kept[t] = db.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
            if "generation_mix" in _tables(db):            # keyed to blocks by id, not by time
                db.execute("DELETE FROM generation_mix WHERE block_id NOT IN (SELECT id FROM blocks)")
            for v, ph in (mapping or {}).items():
                for t in _tables(db):
                    for c, typ in _cols(db, t):
                        if typ in ("TEXT", "") or "CHAR" in typ:
                            db.execute('UPDATE "%s" SET "%s" = REPLACE("%s", ?, ?) WHERE "%s" LIKE ?'
                                       % (t, c, c, c), (v, ph, "%" + v + "%"))
        db.execute("VACUUM")
    finally:
        db.close()
    return kept


def build(src_db: str, out_zip: str, *, lo=None, hi=None, redact: bool = True,
          log_text: str = "", summary: dict = None, work_dir: str = None) -> dict:
    """The support zip at `out_zip`. {bytes, kept, redacted} — `redacted` counts the
    identifiers replaced, never names them."""
    src = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(src_db), uri=True)
    try:
        mapping = identifiers(src) if redact else {}
    finally:
        src.close()
    work = work_dir or os.path.dirname(os.path.abspath(out_zip))
    tmp_db = os.path.join(work, "support_blocks.db")
    try:
        kept = cut(src_db, tmp_db, lo, hi, mapping)
        body = dict(summary or {})
        body["support_file"] = {"span": {"from_utc": lo, "to_utc": hi}, "redacted": bool(redact),
                                "rows_kept": kept}
        text = json.dumps(body, indent=1, sort_keys=True, default=str)
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            zf.writestr("support.json", redact_text(text, mapping))
            zf.writestr("emt.log", redact_text(log_text or "", mapping))
            zf.write(tmp_db, "blocks.db")
    finally:
        if os.path.exists(tmp_db):
            os.remove(tmp_db)
    return {"bytes": os.path.getsize(out_zip), "kept": kept, "redacted": len(mapping)}


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return default


def _block_device(path: str, mounts_text: str) -> str:
    """The block device under `path` ('mmcblk0', 'sda', 'nvme0n1'), from /proc/mounts' longest
    mount point above it; '' when it can't be told (an overlay, a container without /dev)."""
    best, dev = "", ""
    for line in (mounts_text or "").splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[0].startswith("/dev/"):
            continue
        mp = parts[1]
        if (path == mp or path.startswith(mp.rstrip("/") + "/")) and len(mp) >= len(best):
            best, dev = mp, parts[0][5:]
    m = re.match(r"^(mmcblk\d+|nvme\d+n\d+|[a-z]+?)(p?\d+)?$", dev)
    return m.group(1) if m else dev


def host_info(data_dir: str, supervisor: dict = None) -> dict:
    """What EMT is running on, for a helper reading the timings in the log (21 Oct 2026, the user:
    a count that is 0.9 s on a Mac was 22 s on a thin client). Read from the platform, /proc and
    /sys where they exist; nothing that names the machine or its owner (no hostname, no address).
    `supervisor` is the Supervisor's /info, when the caller has it: the board, the OS, versions."""
    import platform
    out = {"platform": {"system": platform.system(), "release": platform.release(),
                        "machine": platform.machine(), "python": platform.python_version(),
                        "sqlite": sqlite3.sqlite_version},
           "cpu": {"count": os.cpu_count()}}
    info = _read("/proc/cpuinfo") or ""
    for key in ("model name", "Model", "Hardware"):      # x86; a Pi names its board as Model
        m = re.search(r"^%s\s*:\s*(.+)$" % key, info, re.M)
        if m:
            out["cpu"][key.lower().replace(" ", "_")] = m.group(1).strip()
    try:
        out["cpu"]["load_avg"] = [round(x, 2) for x in os.getloadavg()]
    except Exception:
        pass
    mem = _read("/proc/meminfo") or ""
    for key in ("MemTotal", "MemAvailable", "SwapTotal"):
        m = re.search(r"^%s:\s*(\d+) kB" % key, mem, re.M)
        if m:
            out.setdefault("memory_mb", {})[key] = int(m.group(1)) // 1024
    up = (_read("/proc/uptime") or "").split()
    if up:
        try:
            out["uptime_h"] = round(float(up[0]) / 3600, 1)
        except ValueError:
            pass
    disk = {}
    try:
        st = os.statvfs(data_dir)
        disk.update(total_gb=round(st.f_blocks * st.f_frsize / 1e9, 1),
                    free_gb=round(st.f_bavail * st.f_frsize / 1e9, 1))
    except Exception:
        pass
    dev = _block_device(os.path.abspath(data_dir), _read("/proc/mounts"))
    if dev:
        disk["device"] = dev
        rot = (_read("/sys/block/%s/queue/rotational" % dev) or "").strip()
        if rot in ("0", "1"):
            disk["rotational"] = rot == "1"
        model = (_read("/sys/block/%s/device/model" % dev) or _read("/sys/block/%s/device/name" % dev) or "").strip()
        if model:
            disk["model"] = model
        disk["kind"] = ("SD card or eMMC" if dev.startswith("mmcblk") else "NVMe" if dev.startswith("nvme")
                        else "disk" if disk.get("rotational") else "SSD or USB")
    if disk:
        out["disk"] = disk
    if supervisor:
        out["supervisor"] = {k: supervisor.get(k) for k in
                             ("arch", "machine", "operating_system", "homeassistant", "supervisor",
                              "docker", "channel") if supervisor.get(k) is not None}
    return out


def last_errors(log_text: str, n: int = 40) -> list:
    """The log's last `n` ERROR or WARNING lines, for the summary."""
    return [l for l in (log_text or "").splitlines() if re.search(r"\b(ERROR|WARNING|CRITICAL)\b", l)][-n:]
