"""The agreement live at a half-hour (`_agreement_era`) and what a finished import wrote
(`_run_wrote_blocks`).

What is left of the per-agreement cost coverage tests: the coverage, its verdict, the pricing-health
card and its demo went in engine step 2d (23 Oct 2026) — Octopus's band, kept by the import, settles
each half-hour through the one rule, and a half-hour with none is asked for like live history.
"""
import unittest
from unittest import mock

import engine


class TestAgreementEra(unittest.TestCase):
    AGREEMENTS = [
        {"tariff_code": "E-1R-OE-FIX-12M-24-05-11-H",
         "valid_from": "2024-05-15T00:00:00+01:00", "valid_to": "2024-09-12T00:00:00+01:00"},
        {"tariff_code": "E-1R-INTELLI-VAR-22-10-14-H",
         "valid_from": "2024-09-12T00:00:00+01:00", "valid_to": "2026-08-24T00:00:00+01:00"},
        {"tariff_code": "E-1R-IOG-SMB-FIX-6M-26-03-05-H",
         "valid_from": "2026-08-24T00:00:00+01:00", "valid_to": None},
    ]

    def setUp(self):
        patcher = mock.patch.object(
            engine, "_kraken_discovery", {"import": {"agreements": self.AGREEMENTS}})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_resolves_the_live_agreement(self):
        era = engine._agreement_era("import", "2025-06-25T12:00:00")
        self.assertEqual(era[0], "E-1R-INTELLI-VAR-22-10-14-H")

    def test_open_ended_agreement_has_no_end(self):
        era = engine._agreement_era("import", "2026-09-10T12:00:00")
        self.assertEqual(era[0], "E-1R-IOG-SMB-FIX-6M-26-03-05-H")
        self.assertIsNone(era[2])

    def test_boundary_is_half_open_and_bst_shifted(self):
        # The agreement reads 2026-08-24T00:00+01:00 — i.e. 23:00Z on the 23rd. Slots are
        # naive UTC, so the boundary must land there and not at midnight UTC, or a whole
        # BST evening gets attributed to the wrong tariff.
        self.assertEqual(engine._agreement_era("import", "2026-08-23T22:30:00")[0],
                         "E-1R-INTELLI-VAR-22-10-14-H")
        # Half-open [from, to): the switch instant itself belongs to the NEW agreement,
        # so no slot is ever counted against two eras (which would double the denominator).
        self.assertEqual(engine._agreement_era("import", "2026-08-23T23:00:00")[0],
                         "E-1R-IOG-SMB-FIX-6M-26-03-05-H")

    def test_uncovered_date_returns_none(self):
        self.assertIsNone(engine._agreement_era("import", "2019-01-01T00:00:00"))

    def test_no_discovery_returns_none(self):
        with mock.patch.object(engine, "_kraken_discovery", {}):
            self.assertIsNone(engine._agreement_era("import", "2025-06-25T12:00:00"))


class TheBlocksARunWrote(unittest.TestCase):
    """A finished import or gap fill counts what it wrote: only then are its devices rebuilt."""

    def test_counts_what_a_run_wrote(self):
        self.assertEqual(engine._run_wrote_blocks({"written": {"import": 1440, "export": 96}}), 1536)

    def test_an_empty_window_wrote_nothing(self):
        self.assertEqual(engine._run_wrote_blocks({"written": {"import": 0, "export": 0}}), 0)

    def test_missing_or_malformed_written_is_zero_not_an_error(self):
        for job in ({}, {"written": None}, {"written": {}},
                    {"written": {"import": None}}, {"written": {"import": "x"}}):
            self.assertEqual(engine._run_wrote_blocks(job), 0, job)

    def test_one_channel_written_still_counts(self):
        # A gap fill can land import-only; that must still be verified.
        self.assertEqual(engine._run_wrote_blocks({"written": {"import": 12, "export": 0}}), 12)
