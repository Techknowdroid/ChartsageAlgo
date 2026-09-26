"""Focused recovery-inventory tests for create_important_backup.py."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ChartSageBhavcopy import BhavcopyDatabase
import create_important_backup as backup
import update_github


class ImportantBackupInventoryTests(unittest.TestCase):
    def test_every_existing_user_store_is_selected(self) -> None:
        covered = dict(backup.verify_user_created_data_coverage())
        for relative in backup.USER_CREATED_DATA_STORES:
            if (backup.PROJECT_DIR / relative).exists():
                self.assertIn(relative, covered)

    def test_key_user_stores_are_declared(self) -> None:
        declared = {path.as_posix() for path in backup.USER_CREATED_DATA_STORES}
        self.assertIn("scanner_templates.json", declared)
        self.assertIn("watchlists.json", declared)
        self.assertIn("stock_alerts.db", declared)
        self.assertIn(
            "FINALCHARTinkV1_data/finalchartink_recipes.json", declared
        )
        self.assertIn(
            "FINALCHARTinkV1_data/finalchartink_watchlists.json", declared
        )

    def test_regenerable_bhavcopy_database_is_excluded(self) -> None:
        selected = {
            path.relative_to(backup.PROJECT_DIR)
            for path in backup.iter_backup_source_files()
        }
        bhavcopy_database = Path("bhavcopy_history.db")
        self.assertIn(bhavcopy_database, backup.REGENERABLE_DATA_STORES)
        self.assertNotIn(bhavcopy_database, selected)

    def test_regenerable_bhavcopy_directories_are_excluded(self) -> None:
        selected = {
            path.relative_to(backup.PROJECT_DIR)
            for path in backup.iter_backup_source_files()
        }
        self.assertEqual(
            {"nse_bhavcopy_archive", "bhavcopy_snapshots"},
            set(backup.REGENERABLE_DATA_DIRECTORIES),
        )
        for relative in selected:
            if relative.parts:
                self.assertNotIn(
                    relative.parts[0], backup.REGENERABLE_DATA_DIRECTORIES
                )

    def test_missing_bhavcopy_database_recreates_its_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            database_path = Path(temporary_name) / "bhavcopy_history.db"
            BhavcopyDatabase(database_path)
            connection = sqlite3.connect(database_path)
            try:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            finally:
                connection.close()
        self.assertTrue({
            "metadata", "bhavcopy_files", "official_prices", "corporate_actions"
        }.issubset(tables))


class GitHubRecoveryGateTests(unittest.TestCase):
    def test_backup_changes_receive_an_appropriate_automatic_message(self) -> None:
        message = update_github.suggest_commit_message([
            ("M", "create_important_backup.py"),
            ("M", "README.md"),
            ("M", "_test_important_backup.py"),
        ])
        self.assertEqual(message, "chore: improve project backup tooling")

    @mock.patch("update_github.subprocess.run")
    def test_github_gate_runs_complete_backup(self, run: mock.Mock) -> None:
        run.return_value.returncode = 0
        update_github.create_required_recovery_backup()
        command = run.call_args.args[0]
        self.assertEqual(command[0], update_github.sys.executable)
        self.assertEqual(command[1], str(backup.PROJECT_DIR / "create_important_backup.py"))

    @mock.patch("update_github.subprocess.run")
    def test_github_gate_stops_after_backup_failure(self, run: mock.Mock) -> None:
        run.return_value.returncode = 1
        with self.assertRaises(update_github.GitUpdateError):
            update_github.create_required_recovery_backup()


if __name__ == "__main__":
    unittest.main()
