"""
Engine step 3b (24 Oct 2026): the ask record is a table of its own, `octopus_ask` (step 3 design
§4 (a)). The three kraken_state records it replaces — `bill_ask`, `split_ask`, `bill_split_wait` —
move into it when the store opens (an existing v5 database's, or a v4 import's carried
`bill_split_wait`), and the engine's helpers read and save the same maps from it, so nothing asks
Octopus differently and every surface answers as before.

Discriminating: every test here fails on the parent (the table is new), but the GUARD.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_engine  # noqa: F401  — installs stubs, imports engine
import engine
from block_store import BlockStore

BILL = {"2026-09-01T12:00:00": {"first": "2026-09-02T00:00:00", "last": "2026-09-03T00:00:00"},
        "2026-08-01T12:00:00": {"first": "2026-08-02T00:00:00", "last": "2026-08-20T00:00:00",
                                "gave_up": "2026-09-02T01:00:00"}}
SPLIT = {"2026-09-05T02:00:00": {"first": "2026-09-06T00:00:00"}}
HOLD = {"2026-10-01T03:00:00": {"first": "2026-10-02T00:00:00"},
        "2026-09-01T03:00:00": {"first": "2026-09-02T00:00:00", "kept": "2026-09-30T00:00:00"}}


def _legacy_db():
    """A database holding the three records the old way, in kraken_state."""
    path = os.path.join(tempfile.mkdtemp(), "blocks.db")
    st = BlockStore(path)
    st._conn.execute("DELETE FROM octopus_ask")
    for k, v in (("bill_ask", BILL), ("split_ask", SPLIT), ("bill_split_wait", HOLD)):
        st.set_kraken_state(k, json.dumps(v))
    st._conn.commit()
    st.close()
    return path


class TheMove(unittest.TestCase):

    def test_on_open_the_records_move_into_the_table(self):
        st = BlockStore(_legacy_db())
        self.addCleanup(st.close)
        self.assertEqual((st.ask_record("bill"), st.ask_record("split"), st.ask_record("hold")),
                         (BILL, SPLIT, HOLD))
        for k in ("bill_ask", "split_ask", "bill_split_wait"):
            self.assertIsNone(st.get_kraken_state(k), k)

    def test_the_states_are_the_table_s_own(self):
        st = BlockStore(_legacy_db())
        self.addCleanup(st.close)
        rows = {(r[0], r[1]): r[2] for r in st._conn.execute(
            "SELECT slot_start, kind, state FROM octopus_ask")}
        self.assertEqual(rows[("2026-09-01T12:00:00", "bill")], "waiting")
        self.assertEqual(rows[("2026-08-01T12:00:00", "bill")], "given_up")
        self.assertEqual(rows[("2026-10-01T03:00:00", "hold")], "held")
        self.assertEqual(rows[("2026-09-01T03:00:00", "hold")], "kept")

    def test_a_row_already_in_the_table_is_kept(self):
        path = _legacy_db()
        st = BlockStore(path)
        st.ask_record_save("bill", {"2026-09-01T12:00:00": {"first": "2026-09-01T23:00:00"}})
        st.set_kraken_state("bill_ask", json.dumps(BILL))       # a stale legacy copy reappears
        st.close()
        st = BlockStore(path)
        self.addCleanup(st.close)
        self.assertEqual(st.ask_record("bill")["2026-09-01T12:00:00"], {"first": "2026-09-01T23:00:00"})

    def test_a_save_replaces_one_kind_only(self):
        st = BlockStore(_legacy_db())
        self.addCleanup(st.close)
        st.ask_record_save("split", {})
        self.assertEqual((st.ask_record("split"), st.ask_record("bill")), ({}, BILL))


class TheReaders(unittest.TestCase):
    """The surfaces read the same answers from the table."""

    def test_held_and_kept(self):
        st = BlockStore(_legacy_db())
        self.addCleanup(st.close)
        self.assertEqual(engine.bill_split_held(st),
                         ({"2026-10-01T03:00:00"}, {"2026-09-01T03:00:00"}))

    def test_given_up(self):
        st = BlockStore(_legacy_db())
        self.addCleanup(st.close)
        with mock.patch.object(engine, "_kraken_discovery", {"import": {"mpan": "M"}}):
            given_up = engine.bill_no_cost(st)
        self.assertIn("2026-08-01T12:00:00", given_up)


class TheV4Import(unittest.TestCase):

    def test_a_carried_hold_back_lands_in_the_table(self):
        """v4's `bill_split_wait` is carried (rules: carry); opened in v5 it is the table's."""
        from v4import import rules
        self.assertEqual(rules.key_rule("kraken_state", "bill_split_wait"), "carry")
        self.assertIn("octopus_ask", rules.DROP_TABLES)        # v5's own; a v4 one has none
        path = os.path.join(tempfile.mkdtemp(), "blocks.db")
        st = BlockStore(path)
        st._conn.execute("DROP TABLE octopus_ask")              # as a v4 database: no table
        st.set_kraken_state("bill_split_wait", json.dumps(HOLD))
        st._conn.commit()
        st.close()
        st = BlockStore(path)
        self.addCleanup(st.close)
        self.assertEqual(st.ask_record("hold"), HOLD)

    def test_the_old_keys_stay_dropped(self):
        """GUARD: `bill_ask` and `split_ask` are runtime state the importer drops, as before."""
        from v4import import rules
        self.assertNotEqual(rules.key_rule("kraken_state", "bill_ask"), "carry")


if __name__ == "__main__":
    unittest.main()
