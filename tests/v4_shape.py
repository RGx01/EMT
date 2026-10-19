"""
What a test needs to turn a v5-created database into a v4-shaped one: v5's marker removed (the
callers do that) and what v5 ADDED that v4 never had — a real v4 database has none of it, and the
importer refuses anything its rules do not name. One list, so a new v5-only table or column is
added here once.
"""

# v5-only, by the change that added it
V5_ONLY_TABLES = ("device_sensors",)                                   # device sensors design §2
V5_ONLY_COLUMNS = (("blocks", "imp_sensor_id"), ("blocks", "exp_sensor_id"))   # ... §7


def strip_v5_only(conn) -> None:
    for t in V5_ONLY_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    for t, col in V5_ONLY_COLUMNS:
        if col in {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}:
            conn.execute(f"ALTER TABLE {t} DROP COLUMN {col}")
