"""Focused tests for the complete external project backup tool."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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

    def test_smart_backup_reuses_unchanged_and_updates_changed_files(self) -> None:
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
                first = complete_backup.create_complete_backup(
                    destination,
                    source_root=source,
                    require_different_drive=False,
                    progress=lambda _message: None,
                )
                first_summary = json.loads(
                    (first / "BACKUP_SUMMARY.json").read_text(encoding="utf-8")
                )
                # Destination timestamp precision differs across NTFS/exFAT.
                # Reuse is based on recorded source metadata plus checksums.
                (first / "source.py").touch()
                second = complete_backup.create_complete_backup(
                    destination,
                    source_root=source,
                    require_different_drive=False,
                    progress=lambda _message: None,
                )
                second_summary = json.loads(
                    (second / "BACKUP_SUMMARY.json").read_text(encoding="utf-8")
                )

                (source / "source.py").write_text(
                    "print('changed')\n", encoding="utf-8"
                )
                (source / "scanner_templates.json").unlink()
                (source / "new_file.txt").write_text("new\n", encoding="utf-8")
                database = sqlite3.connect(source / "watchlists.db")
                try:
                    database.execute("INSERT INTO lists VALUES('New Watchlist')")
                    database.commit()
                finally:
                    database.close()
                third = complete_backup.create_complete_backup(
                    destination,
                    source_root=source,
                    require_different_drive=False,
                    progress=lambda _message: None,
                )
                third_summary = json.loads(
                    (third / "BACKUP_SUMMARY.json").read_text(encoding="utf-8")
                )

            self.assertEqual(first, second)
            self.assertEqual(second, third)
            self.assertEqual(first.name, complete_backup.SMART_BACKUP_NAME)
            self.assertFalse(first_summary["incremental_update"])
            self.assertEqual(
                first_summary["effective_verification_mode"], "full"
            )
            self.assertTrue(second_summary["incremental_update"])
            self.assertEqual(
                second_summary["effective_verification_mode"], "fast"
            )
            self.assertGreater(second_summary["reused_verified_files"], 0)
            self.assertEqual(second_summary["copied_or_refreshed_files"], 0)
            self.assertEqual(
                (third / "source.py").read_text(encoding="utf-8"),
                "print('changed')\n",
            )
            self.assertEqual(
                (third / "new_file.txt").read_text(encoding="utf-8"), "new\n"
            )
            self.assertFalse((third / "scanner_templates.json").exists())
            self.assertEqual(third_summary["removed_retired_files"], 1)
            self.assertTrue((third / "BACKUP_COMPLETE.txt").is_file())
            database = sqlite3.connect(third / "watchlists.db")
            try:
                names = database.execute(
                    "SELECT name FROM lists ORDER BY name"
                ).fetchall()
            finally:
                database.close()
            self.assertEqual(names, [("My Watchlist",), ("New Watchlist",)])

    def test_fast_mode_skips_destination_rehash_and_full_mode_repairs_corruption(self) -> None:
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
                    progress=lambda _message: None,
                )

                # Fast mode must trust the last completed source metadata and
                # manifest rather than invoking the USB-reading reuse checker.
                with mock.patch.object(
                    complete_backup,
                    "_reuse_verified_file",
                    side_effect=AssertionError(
                        "fast mode reopened an unchanged destination file"
                    ),
                ):
                    complete_backup.create_complete_backup(
                        destination,
                        source_root=source,
                        require_different_drive=False,
                        verification_mode=complete_backup.FAST_VERIFICATION_MODE,
                        progress=lambda _message: None,
                    )

                backed_up_source = target / "source.py"
                backed_up_source.write_text("corrupt destination\n", encoding="utf-8")
                complete_backup.create_complete_backup(
                    destination,
                    source_root=source,
                    require_different_drive=False,
                    verification_mode=complete_backup.FULL_VERIFICATION_MODE,
                    progress=lambda _message: None,
                )

            self.assertEqual(
                backed_up_source.read_text(encoding="utf-8"),
                "print('ready')\n",
            )
            summary = json.loads(
                (target / "BACKUP_SUMMARY.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["effective_verification_mode"], "full")
            self.assertTrue(summary["full_destination_verification_performed"])
            self.assertEqual(summary["fast_incremental_runs_since_full"], 0)

    def test_incomplete_prior_backup_forces_full_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            base = Path(temporary_name)
            source = base / "project"
            destination = base / "usb"
            source.mkdir()
            destination.mkdir()
            self._make_project(source)
            messages = []
            with mock.patch.object(
                complete_backup, "_verify_git_repository", return_value="test passed"
            ):
                target = complete_backup.create_complete_backup(
                    destination,
                    source_root=source,
                    require_different_drive=False,
                    progress=lambda _message: None,
                )
                (target / "BACKUP_COMPLETE.txt").unlink()
                complete_backup.create_complete_backup(
                    destination,
                    source_root=source,
                    require_different_drive=False,
                    verification_mode=complete_backup.FAST_VERIFICATION_MODE,
                    progress=messages.append,
                )
            summary = json.loads(
                (target / "BACKUP_SUMMARY.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["requested_verification_mode"], "fast")
            self.assertEqual(summary["effective_verification_mode"], "full")
            self.assertTrue(any("automatically switching" in item for item in messages))

    def test_interactive_mode_choice_accepts_fast_and_full(self) -> None:
        with mock.patch("builtins.input", return_value=""):
            self.assertEqual(
                complete_backup.choose_verification_mode(),
                complete_backup.FAST_VERIFICATION_MODE,
            )
        with mock.patch("builtins.input", side_effect=["wrong", "2"]):
            self.assertEqual(
                complete_backup.choose_verification_mode(),
                complete_backup.FULL_VERIFICATION_MODE,
            )

    def test_plain_launch_routes_to_gui_with_fast_mode_selected(self) -> None:
        arguments = SimpleNamespace(
            gui=False,
            console=False,
            destination=None,
            yes=False,
            new_snapshot=False,
            fast=False,
            full_verify=False,
        )
        with (
            mock.patch.object(complete_backup, "parse_arguments", return_value=arguments),
            mock.patch.object(
                complete_backup, "run_backup_gui", return_value=0
            ) as gui,
            mock.patch.object(complete_backup.sys, "argv", ["backup.py"]),
        ):
            self.assertEqual(complete_backup.main(), 0)
        gui.assert_called_once_with(
            complete_backup.FAST_VERIFICATION_MODE,
            initial_destination=None,
            initial_snapshot=False,
        )

    def test_smart_backup_refuses_unmarked_existing_current_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            base = Path(temporary_name)
            source = base / "project"
            destination = base / "usb"
            source.mkdir()
            destination.mkdir()
            self._make_project(source)
            foreign = destination / complete_backup.SMART_BACKUP_NAME
            foreign.mkdir()
            (foreign / "do_not_touch.txt").write_text("personal", encoding="utf-8")
            with self.assertRaises(complete_backup.CompleteBackupError):
                complete_backup.create_complete_backup(
                    destination,
                    source_root=source,
                    require_different_drive=False,
                    progress=lambda _message: None,
                )
            self.assertEqual(
                (foreign / "do_not_touch.txt").read_text(encoding="utf-8"),
                "personal",
            )

    def test_read_only_git_object_and_interrupted_temporary_are_handled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            base = Path(temporary_name)
            source = base / "project"
            destination = base / "usb"
            source.mkdir()
            destination.mkdir()
            self._make_project(source)
            object_dir = source / ".git" / "objects" / "00"
            object_dir.mkdir(parents=True)
            source_object = object_dir / "readonly-object"
            source_object.write_bytes(b"packed git object")
            source_object.chmod(0o444)

            target = destination / complete_backup.SMART_BACKUP_NAME
            target.mkdir()
            complete_backup._write_backup_marker(target)
            interrupted_dir = target / ".git" / "objects" / "00"
            interrupted_dir.mkdir(parents=True)
            interrupted = interrupted_dir / ".chartsage-copy-interrupted"
            interrupted.write_bytes(b"partial")
            interrupted.chmod(0o444)
            prior_destination = interrupted_dir / source_object.name
            prior_destination.write_bytes(b"old incomplete copy")
            prior_destination.chmod(0o444)
            try:
                with mock.patch.object(
                    complete_backup,
                    "_verify_git_repository",
                    return_value="test passed",
                ):
                    result = complete_backup.create_complete_backup(
                        destination,
                        source_root=source,
                        require_different_drive=False,
                        progress=lambda _message: None,
                    )
                self.assertFalse(interrupted.exists())
                self.assertEqual(
                    (result / ".git" / "objects" / "00" / source_object.name).read_bytes(),
                    b"packed git object",
                )
                self.assertTrue((result / "BACKUP_COMPLETE.txt").is_file())
            finally:
                source_object.chmod(0o666)
                if prior_destination.exists():
                    prior_destination.chmod(0o666)


if __name__ == "__main__":
    unittest.main()
