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

1 and 5 exist; 2 to 4 follow, in this order (E design §10).
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


def provenance(conn, report):
    """1: v5's provenance words (spec §5.2, BL-61(5)). A bill-settled block says 'settled'
    where v4 said 'measured'. A block a dispatch reconciliation flagged (rate_reconciled = 1)
    while leaving 'schedule' or no word says 'reconciled'; a settled or corrected one keeps
    its word, which outranks a reconciliation. Every meter: v4 flagged sub-meter rows too.
    The flag column itself stays until the column drop."""
    renamed = conn.execute(
        "UPDATE main.blocks SET rate_source = 'settled' WHERE rate_source = 'measured'").rowcount
    folded = conn.execute(
        "UPDATE main.blocks SET rate_source = 'reconciled' WHERE rate_reconciled = 1 "
        "AND (rate_source IS NULL OR rate_source = 'schedule')").rowcount
    report["transforms"]["provenance"] = {"settled": renamed, "reconciled_folded": folded}


def run(conn, report):
    provenance(conn, report)
    carry_keys(conn, report)
