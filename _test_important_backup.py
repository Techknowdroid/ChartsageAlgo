"""Focused recovery-inventory tests for create_important_backup.py."""

from __future__ import annotations

import unittest
from unittest import mock

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


class GitHubRecoveryGateTests(unittest.TestCase):
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
