"""Focused tests for the complete external project backup tool."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backup_complete_project_to_usb as complete_backup


class CompleteProjectBackupTests(unittest.TestCase):
    def _make_project(self, root: Path) -> None:
        (root / ".git").mkdir()
        (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (root / "source.py").write_text("print('ready')\n", encoding="utf-8")
        (root / "scanner_templates.json").write_text(
            json.dumps({"User Rule": {"expression": {"type": "rule"}}}),
            encoding="utf-8",
        )
        (root / "nse_bhavcopy_archive").mkdir()
        (root / "nse_bhavcopy_archive" / "market.zip").write_bytes(b"archive")
        (root / "backups" / "old").mkdir(parents=True)
        (root / "backups" / "old" / "redundant.txt").write_text("old")
        (root / "__pycache__").mkdir()
        (root / "__pycache__" / "source.pyc").write_bytes(b"cache")
        database = sqlite3.connect(root / "watchlists.db")
        try:
            database.execute("CREATE TABLE lists(name TEXT PRIMARY KEY)")
            database.execute("INSERT INTO lists VALUES('My Watchlist')")
            database.commit()
        finally:
            database.close()

    def test_inventory_includes_git_market_data_and_user_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            source = Path(temporary_name) / "project"
            source.mkdir()
            self._make_project(source)
            inventory = complete_backup.build_source_inventory(source)
            selected = set(inventory.files)
        self.assertIn(Path(".git/HEAD"), selected)
        self.assertIn(Path("scanner_templates.json"), selected)
        self.assertIn(Path("nse_bhavcopy_archive/market.zip"), selected)
        self.assertNotIn(Path("backups/old/redundant.txt"), selected)
        self.assertNotIn(Path("__pycache__/source.pyc"), selected)

    def test_complete_backup_is_directly_restorable_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            base = Path(temporary_name)
            source = base / "project"
            destination = base / "usb"
            source.mkdir()
            destination.mkdir()
            self._make_project(source)
            with mock.patch.object(
                complete_backup, "_verify_git_repository", return_value="test passed"
            ):
                target = complete_backup.create_complete_backup(
                    destination,
                    source_root=source,
                    require_different_drive=False,
                    stamp="TEST",
                    progress=lambda _message: None,
                )
            self.assertTrue((target / ".git" / "HEAD").is_file())
            self.assertTrue((target / "scanner_templates.json").is_file())
            self.assertTrue((target / "BACKUP_MANIFEST.sha256").is_file())
            self.assertTrue((target / "BACKUP_COMPLETE.txt").is_file())
            self.assertFalse((target / "BACKUP_FAILED.txt").exists())
            self.assertFalse((target / "backups").exists())
            database = sqlite3.connect(target / "watchlists.db")
            try:
                value = database.execute("SELECT name FROM lists").fetchone()
            finally:
                database.close()
            self.assertEqual(value, ("My Watchlist",))

    def test_same_drive_rejection_is_enforced_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            base = Path(temporary_name)
            source = base / "project"
            destination = base / "destination"
            source.mkdir()
            destination.mkdir()
            with self.assertRaises(complete_backup.CompleteBackupError):
                complete_backup.validate_destination(destination, source)


if __name__ == "__main__":
    unittest.main()
