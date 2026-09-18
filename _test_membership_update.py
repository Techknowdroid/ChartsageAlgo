from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from membership_update import (
    ARCHIVE_URL,
    CURRENT_ROOT,
    IST,
    MembershipUpdateError,
    membership_coverage_summary,
    update_historical_membership,
)
from build_historical_index_membership import parse_temporary_exclusion_notice
from historical_index_membership import SUPPORTED_INDICES


PROJECT_DIR = Path(__file__).resolve().parent


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class HistoricalMembershipUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        shutil.copy2(
            PROJECT_DIR / "historical_index_membership.db",
            self.root / "historical_index_membership.db",
        )
        source = PROJECT_DIR / "historical_membership_sources"
        target = self.root / "historical_membership_sources"
        target.mkdir()
        shutil.copy2(source / "manifest.json", target / "manifest.json")
        for text_file in source.glob("*.txt"):
            shutil.copy2(text_file, target / text_file.name)
        shutil.copytree(source / "current", target / "current")
        self.csv_payloads = {
            CURRENT_ROOT + path.name: path.read_bytes()
            for path in (source / "current").glob("*.csv")
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def downloader(self, url: str) -> bytes:
        if url == ARCHIVE_URL:
            return b"<html><body>No new notices</body></html>"
        return self.csv_payloads[url]

    def test_success_extends_coverage_without_touching_bhavcopy(self) -> None:
        bhavcopy = self.root / "bhavcopy_history.db"
        bhavcopy.write_bytes(b"price database sentinel")
        result = update_historical_membership(self.root, download=self.downloader)
        self.assertEqual(result["verified_indices"], len(SUPPORTED_INDICES))
        self.assertEqual(result["end_date"], datetime.now(IST).date().isoformat())
        self.assertEqual(bhavcopy.read_bytes(), b"price database sentinel")
        self.assertEqual(
            membership_coverage_summary(self.root)["end_date"], result["end_date"]
        )

    def test_invalid_current_list_preserves_live_database(self) -> None:
        original = digest(self.root / "historical_index_membership.db")

        def broken_download(url: str) -> bytes:
            if url.endswith("ind_nifty50list.csv"):
                return b"Symbol\nONLYONE\n"
            return self.downloader(url)

        with self.assertRaises(MembershipUpdateError):
            update_historical_membership(self.root, download=broken_download)
        self.assertEqual(original, digest(self.root / "historical_index_membership.db"))

    def test_future_demerger_interval_is_parsed_without_manual_symbol_rules(self) -> None:
        source = self.root / "historical_membership_sources"
        notice = source / "future_demerger.txt"
        notice.write_text(
            """PRESS RELEASE
Exclusion of Example Consumer Ltd. from Nifty indices
The dummy symbol DUMMYEXAMPLE was included in various indices effective from
January 10, 2026. After the demerger, Example Consumer Ltd. (EXAMPLE) was listed.
The committee has decided to exclude Example Consumer Ltd. (EXAMPLE) from various
indices as listed hereunder effective from March 20, 2026.
Sr. No. Index Name
1 Nifty 500
2 Nifty Smallcap 250
""",
            encoding="utf-8",
        )
        events = parse_temporary_exclusion_notice(
            {
                "title": "Exclusion of Example Consumer Ltd. from Nifty indices",
                "url": "https://www.niftyindices.com/Press_Release/example.pdf",
                "text_file": notice.name,
            },
            source_dir=source,
            current_date=datetime.now(IST).date(),
            start_date=datetime.now(IST).date().replace(year=2022),
        )
        transitions = {
            (event["effective_date"], event["index_name"], tuple(event["included"]), tuple(event["excluded"]))
            for event in events
        }
        self.assertIn(("2026-01-10", "NIFTY 500", ("EXAMPLE",), ()), transitions)
        self.assertIn(("2026-03-20", "NIFTY SMALLCAP 250", (), ("EXAMPLE",)), transitions)


if __name__ == "__main__":
    unittest.main()
