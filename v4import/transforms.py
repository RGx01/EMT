"""
v4import/transforms.py — the ordered rewrites applied to the target after the carry
(E design §4).

Each transform reads and writes only the target (`main`), may read the attached source
(`src`), and records how many rows it touched in report["transforms"]. None is a heal:
no marker, no version gate, no second run. Each is a translation applied once, to the
importer's copy.

    1  provenance          'measured' -> 'settled', the reconciled fold
    2  EV segments         from the legacy columns, where a block has none
    3  held-rate refit     segments fitted to a settled kWh
    4  band relabel        house off_peak -> night, SMB era
    5  meta                the allow-listed store_meta / kraken_state keys

Only 5 exists yet; 1 to 4 follow, in this order (E design §10).
"""
from . import rules


def carry_keys(conn, report):
    """5: carry the allow-listed keys as they are, values untouched (store_meta values are
    JSON text, and v5 decodes them as v4 did). `written_by`, `db_uuid` and the markers are
    dropped; v5 stamps its own written_by and db_uuid at the first start (spec §6)."""
    for table in rules.KEYED_TABLES:
        carried, dropped = [], []
        cols = [r[1] for r in conn.execute('PRAGMA src.table_info("%s")' % table).fetchall()]
        if not cols:
            report["keys"][table] = {"carried": [], "dropped": []}
            continue
        sel = ", ".join('"%s"' % c for c in cols)
        for row in conn.execute('SELECT %s FROM src."%s" ORDER BY key' % (sel, table)).fetchall():
            if rules.key_rule(table, row["key"]) == "carry":
                conn.execute('INSERT OR REPLACE INTO main."%s" (%s) VALUES (%s)'
                             % (table, sel, ", ".join("?" * len(cols))), tuple(row))
                carried.append(row["key"])
            else:
                dropped.append(row["key"])
        report["keys"][table] = {"carried": carried, "dropped": dropped}


def run(conn, report):
    carry_keys(conn, report)
