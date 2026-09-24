from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from historical_index_membership import (
    HistoricalIndexMembershipStore,
    HistoricalMembershipCoverageError,
    SUPPORTED_INDICES,
)


BUNDLED_DATABASE = Path(__file__).resolve().parent / "historical_index_membership.db"


class HistoricalIndexMembershipStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = HistoricalIndexMembershipStore(
            Path(self.temporary.name) / "membership.db"
        )
        self.store.replace_snapshot(
            "NIFTY 50", "2024-01-01", ["AAA", "BBB"], "baseline"
        )
        self.store.replace_snapshot(
            "NIFTY 50", "2024-07-01", ["BBB", "CCC"], "change"
        )
        self.store.set_coverage(
            "NIFTY 50", "2024-01-01", "2024-12-31", verified=True
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_membership_changes_on_effective_date(self) -> None:
        self.assertEqual(self.store.members_on("NIFTY 50", "2024-06-30"), {"AAA", "BBB"})
        self.assertEqual(self.store.members_on("NIFTY 50", "2024-07-01"), {"BBB", "CCC"})

    def test_period_union_contains_past_and_new_members(self) -> None:
        self.assertEqual(
            self.store.members_during("NIFTY 50", "2024-03-01", "2024-09-01"),
            {"AAA", "BBB", "CCC"},
        )

    def test_unverified_or_missing_period_is_rejected(self) -> None:
        with self.assertRaises(HistoricalMembershipCoverageError):
            self.store.members_on("NIFTY 50", "2023-12-31")
        self.store.set_coverage(
            "NIFTY 50", "2024-01-01", "2024-12-31", verified=False
        )
        with self.assertRaises(HistoricalMembershipCoverageError):
            self.store.members_on("NIFTY 50", "2024-06-30")

    def test_snapshot_replacement_is_atomic_at_same_date(self) -> None:
        self.store.replace_snapshot("NIFTY 50", "2024-07-01", ["DDD"])
        self.assertEqual(self.store.members_on("NIFTY 50", "2024-07-01"), {"DDD"})

    def test_placeholder_symbols_are_not_stored(self) -> None:
        self.store.replace_snapshot(
            "NIFTY 50", "2024-07-01", ["DDD", "DUMMYHEG", "dummy-example"]
        )
        self.assertEqual(self.store.members_on("NIFTY 50", "2024-07-01"), {"DDD"})


@unittest.skipUnless(
    BUNDLED_DATABASE.is_file(),
    "Operational historical_index_membership.db is intentionally not stored in Git",
)
class BundledHistoricalMembershipDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.store = HistoricalIndexMembershipStore(
            BUNDLED_DATABASE
        )

    def test_four_year_coverage_is_verified_for_every_supported_index(self) -> None:
        coverage = [self.store.coverage(name) for name in SUPPORTED_INDICES]
        self.assertTrue(all(item is not None for item in coverage))
        self.assertTrue(all(bool(item["verified"]) for item in coverage if item))
        common_start = max(
            date.fromisoformat(str(item["start_date"]))
            for item in coverage
            if item
        )
        common_end = min(
            date.fromisoformat(str(item["end_date"]))
            for item in coverage
            if item
        )
        self.assertGreaterEqual(
            (common_end - common_start).days,
            (4 * 365) - 7,
            "The verified common window must retain approximately four years.",
        )
        self.store.require_coverage(
            SUPPORTED_INDICES,
            common_start,
            common_end,
        )

    def test_jio_financial_temporary_membership_boundaries(self) -> None:
        self.assertNotIn("JIOFIN", self.store.members_on("NIFTY 50", "2023-07-19"))
        self.assertIn("JIOFIN", self.store.members_on("NIFTY 50", "2023-07-20"))
        self.assertIn("JIOFIN", self.store.members_on("NIFTY 50", "2023-09-06"))
        self.assertNotIn("JIOFIN", self.store.members_on("NIFTY 50", "2023-09-07"))

    def test_itc_hotels_temporary_membership_boundaries(self) -> None:
        self.assertNotIn("ITCHOTELS", self.store.members_on("NIFTY 500", "2025-01-05"))
        self.assertIn("ITCHOTELS", self.store.members_on("NIFTY 500", "2025-01-06"))
        self.assertNotIn("ITCHOTELS", self.store.members_on("NIFTY 500", "2025-02-10"))

    def test_bundled_database_contains_no_dummy_placeholders(self) -> None:
        for index_name in SUPPORTED_INDICES:
            coverage = self.store.coverage(index_name)
            self.assertIsNotNone(coverage, index_name)
            self.assertFalse(
                any(
                    symbol.startswith("DUMMY")
                    for symbol in self.store.members_during(
                        index_name,
                        str(coverage["start_date"]),
                        str(coverage["end_date"]),
                    )
                ),
                index_name,
            )

    def test_nifty_auto_dvr_membership_boundaries(self) -> None:
        self.assertNotIn("TATAMTRDVR", self.store.members_on("NIFTY AUTO", "2024-03-27"))
        self.assertIn("TATAMTRDVR", self.store.members_on("NIFTY AUTO", "2024-03-28"))
        self.assertIn("TATAMTRDVR", self.store.members_on("NIFTY AUTO", "2024-08-29"))
        self.assertNotIn("TATAMTRDVR", self.store.members_on("NIFTY AUTO", "2024-08-30"))

    def test_nifty_media_tips_symbol_rename_boundaries(self) -> None:
        before = self.store.members_on("NIFTY MEDIA", "2024-09-27")
        after = self.store.members_on("NIFTY MEDIA", "2024-09-30")
        self.assertIn("TIPSINDLTD", before)
        self.assertNotIn("TIPSMUSIC", before)
        self.assertNotIn("TIPSINDLTD", after)
        self.assertIn("TIPSMUSIC", after)

    def test_nifty_energy_expansion_boundaries(self) -> None:
        before = self.store.members_on("NIFTY ENERGY", "2024-12-30")
        after = self.store.members_on("NIFTY ENERGY", "2024-12-31")
        self.assertEqual(len(before), 10)
        self.assertEqual(len(after), 40)
        self.assertNotIn("GAIL", before)
        self.assertIn("GAIL", after)


if __name__ == "__main__":
    unittest.main()
