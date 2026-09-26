"""Create a verified, restorable copy of the complete ChartSage project.

The destination must be on a different drive from this source folder. The
backup includes source code, Git history, databases, downloaded market data,
user-created data, build/runtime assets, distributions, reports, and project
configuration. Only redundant prior backups and transient cache/lock files are
omitted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable


PROJECT_DIR = Path(__file__).resolve().parent
BACKUP_PREFIX = "ChartSage_COMPLETE_Project"
EXCLUDED_TOP_LEVEL_DIRECTORIES = {"backups"}
EXCLUDED_DIRECTORY_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache"}
EXCLUDED_RELATIVE_FILES = {
    Path(".git/index.lock"),
    Path("market_data_maintenance.lock"),
}
EXCLUDED_FILE_ENDINGS = (".pyc", ".pyo", ".db-wal", ".db-shm", ".db-journal")
SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
ProgressCallback = Callable[[str], None]


class CompleteBackupError(RuntimeError):
    """Raised when a complete external backup cannot be trusted."""


@dataclass(frozen=True)
class SourceInventory:
    files: tuple[Path, ...]
    directories: tuple[Path, ...]
    total_bytes: int


def _skip_directory(relative: Path) -> bool:
    if not relative.parts:
        return False
    if relative.parts[0] in EXCLUDED_TOP_LEVEL_DIRECTORIES:
        return True
    return any(part in EXCLUDED_DIRECTORY_NAMES for part in relative.parts)


def _skip_file(relative: Path) -> bool:
    if relative in EXCLUDED_RELATIVE_FILES:
        return True
    if _skip_directory(relative.parent):
        return True
    return relative.name.lower().endswith(EXCLUDED_FILE_ENDINGS)


def build_source_inventory(source_root: Path = PROJECT_DIR) -> SourceInventory:
    """Return the complete restorable project inventory without following links."""
    source_root = source_root.resolve()
    files: list[Path] = []
    directories: list[Path] = []
    total_bytes = 0

    for current, directory_names, file_names in os.walk(
        source_root, topdown=True, followlinks=False
    ):
        current_path = Path(current)
        kept_directories: list[str] = []
        for name in directory_names:
            path = current_path / name
            relative = path.relative_to(source_root)
            if _skip_directory(relative):
                continue
            if path.is_symlink():
                raise CompleteBackupError(
                    f"Symbolic-link directory is not supported safely: {relative}"
                )
            directories.append(relative)
            kept_directories.append(name)
        directory_names[:] = kept_directories

        for name in file_names:
            path = current_path / name
            relative = path.relative_to(source_root)
            if _skip_file(relative):
                continue
            if path.is_symlink():
                raise CompleteBackupError(
                    f"Symbolic-link file is not supported safely: {relative}"
                )
            if not path.is_file():
                raise CompleteBackupError(f"Unsupported project entry: {relative}")
            try:
                size = path.stat().st_size
            except OSError as exc:
                raise CompleteBackupError(f"Cannot inspect {relative}: {exc}") from exc
            files.append(relative)
            total_bytes += size

    return SourceInventory(
        files=tuple(sorted(files, key=lambda item: item.as_posix().casefold())),
        directories=tuple(
            sorted(directories, key=lambda item: item.as_posix().casefold())
        ),
        total_bytes=total_bytes,
    )


def validate_destination(
    destination_parent: Path,
    source_root: Path = PROJECT_DIR,
    *,
    require_different_drive: bool = True,
) -> Path:
    """Validate that the selected existing destination cannot recurse into source."""
    source_root = source_root.resolve()
    destination_parent = destination_parent.expanduser().resolve()
    if not destination_parent.is_dir():
        raise CompleteBackupError(
            f"The selected destination does not exist: {destination_parent}"
        )
    try:
        destination_parent.relative_to(source_root)
    except ValueError:
        pass
    else:
        raise CompleteBackupError("The backup destination cannot be inside the project.")

    source_drive = source_root.drive.casefold()
    destination_drive = destination_parent.drive.casefold()
    if require_different_drive and source_drive == destination_drive:
        raise CompleteBackupError(
            "Select the USB/external drive, not another folder on the project drive "
            f"({source_root.drive or source_root.anchor})."
        )
    return destination_parent


def _copy_regular_file_consistently(source: Path, destination: Path) -> None:
    """Copy a non-database file, retrying if it changes during the copy."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        before = source.stat()
        shutil.copy2(source, destination)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) == (
            after.st_size,
            after.st_mtime_ns,
        ):
            return
        if attempt < 2:
            time.sleep(0.25)
    raise CompleteBackupError(
        f"File kept changing during backup; close the application using it: {source}"
    )


def _copy_sqlite_database(source: Path, destination: Path) -> None:
    """Create and integrity-check a transactionally consistent SQLite copy."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"file:{source.as_posix()}?mode=ro"
    try:
        with closing(
            sqlite3.connect(source_uri, uri=True, timeout=60)
        ) as source_database:
            with closing(
                sqlite3.connect(destination, timeout=60)
            ) as destination_database:
                source_database.backup(destination_database, pages=4096)
                destination_database.commit()
                destination_database.execute("PRAGMA journal_mode=DELETE").fetchone()
                destination_database.commit()
        with closing(sqlite3.connect(
            f"file:{destination.as_posix()}?mode=ro", uri=True, timeout=60
        )) as check_database:
            result = check_database.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as exc:
        raise CompleteBackupError(f"SQLite backup failed for {source}: {exc}") from exc
    if result != ("ok",):
        raise CompleteBackupError(
            f"SQLite verification failed for {source}: {result!r}"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("bytes", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:,.2f} {unit}" if unit != "bytes" else f"{int(amount):,} bytes"
        amount /= 1024
    return f"{value:,} bytes"


def _write_restore_instructions(target: Path, source_root: Path) -> None:
    text = f"""ChartSage complete external project backup
Created: {datetime.now():%d %B %Y %H:%M:%S}
Original project: {source_root}

PURPOSE
This is a complete restorable working-project backup for disaster recovery and
continued development in Visual Studio Code. It contains the Git repository,
source code, user templates/rules/strategies/watchlists, SQLite databases,
Bhavcopy data and archives, build/runtime assets, distributions, reports, and
project configuration that existed when the backup was created.

RESTORE ON ANOTHER PC
1. You may open this backup folder directly from the writable external drive in
   Visual Studio Code. For better performance, copy the entire backup folder to
   a writable local folder on the new PC first.
2. Keep every file and directory in its existing relative location.
3. Open the external or locally copied folder in Visual Studio Code.
4. Install Python and any development tools required by the project if they are
   not already installed. The backup preserves project files, not system-wide
   installations of Python, VS Code, Git, or Windows components.
5. Configure that PC's Telegram/broker secrets again. Windows Credential
   Manager credentials live outside the project and cannot be included here.
6. Run the project's compile/tests before making new changes.

INTENTIONALLY OMITTED
The project's backups directory (redundant copies of earlier backups), Python
bytecode/cache directories, SQLite WAL/SHM/journal sidecars, and transient lock
files. Active SQLite contents were captured through SQLite's online backup API.

VALIDATION
BACKUP_MANIFEST.sha256 records every copied project/restore-metadata file and
its SHA-256 checksum.
BACKUP_COMPLETE.txt is written only after all copied files pass verification.
"""
    (target / "RESTORE_INSTRUCTIONS.txt").write_text(text, encoding="utf-8")


def _create_and_verify_manifest(target: Path, progress: ProgressCallback) -> int:
    manifest_path = target / "BACKUP_MANIFEST.sha256"
    complete_path = target / "BACKUP_COMPLETE.txt"
    paths = [
        path
        for path in target.rglob("*")
        if path.is_file() and path not in {manifest_path, complete_path}
    ]
    paths.sort(key=lambda item: item.relative_to(target).as_posix().casefold())
    records: list[tuple[str, str]] = []
    for index, path in enumerate(paths, 1):
        if index == 1 or index % 250 == 0 or index == len(paths):
            progress(f"Checksumming files: {index:,}/{len(paths):,}")
        records.append((_sha256(path), path.relative_to(target).as_posix()))
    manifest_path.write_text(
        "".join(f"{digest}  {relative}\n" for digest, relative in records),
        encoding="utf-8",
    )

    for index, (expected, relative) in enumerate(records, 1):
        if index == 1 or index % 250 == 0 or index == len(records):
            progress(f"Verifying checksums: {index:,}/{len(records):,}")
        actual = _sha256(target / Path(relative))
        if actual != expected:
            raise CompleteBackupError(f"Checksum mismatch: {relative}")
    return len(records)


def _verify_git_repository(target: Path) -> str:
    if not (target / ".git").is_dir():
        raise CompleteBackupError("The copied project is missing its .git history.")
    git = shutil.which("git")
    if not git:
        return "Git executable unavailable; .git directory presence verified only"
    result = subprocess.run(
        [git, "-C", str(target), "fsck", "--no-progress"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise CompleteBackupError(f"Copied Git repository verification failed: {detail}")
    return "git fsck passed"


def create_complete_backup(
    destination_parent: Path,
    *,
    source_root: Path = PROJECT_DIR,
    require_different_drive: bool = True,
    stamp: str | None = None,
    progress: ProgressCallback = print,
    inventory: SourceInventory | None = None,
) -> Path:
    """Create, verify, and return a complete external project backup folder."""
    source_root = source_root.resolve()
    destination_parent = validate_destination(
        destination_parent,
        source_root,
        require_different_drive=require_different_drive,
    )
    if inventory is None:
        progress("Building complete project inventory...")
        inventory = build_source_inventory(source_root)
    required_headroom = max(64 * 1024 * 1024, inventory.total_bytes // 50)
    free_bytes = shutil.disk_usage(destination_parent).free
    if free_bytes < inventory.total_bytes + required_headroom:
        raise CompleteBackupError(
            "The selected drive does not have enough free space. "
            f"Required approximately {inventory.total_bytes + required_headroom:,} "
            f"bytes; available {free_bytes:,} bytes."
        )

    stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    target = destination_parent / f"{BACKUP_PREFIX}_{stamp}"
    if target.exists():
        raise CompleteBackupError(f"Refusing to overwrite existing backup: {target}")
    target.mkdir(parents=True)

    started = datetime.now()
    try:
        for relative in inventory.directories:
            (target / relative).mkdir(parents=True, exist_ok=True)

        file_total = len(inventory.files)
        for index, relative in enumerate(inventory.files, 1):
            if index == 1 or index % 100 == 0 or index == file_total:
                progress(f"Copying files: {index:,}/{file_total:,} - {relative}")
            source = source_root / relative
            destination = target / relative
            if source.suffix.lower() in SQLITE_SUFFIXES:
                _copy_sqlite_database(source, destination)
            else:
                _copy_regular_file_consistently(source, destination)

        _write_restore_instructions(target, source_root)
        summary = {
            "backup_type": "complete_external_project",
            "created_at": datetime.now().astimezone().isoformat(),
            "source": str(source_root),
            "destination": str(target),
            "source_file_count": len(inventory.files),
            "source_directory_count": len(inventory.directories),
            "source_bytes_before_sqlite_compaction": inventory.total_bytes,
            "excluded_top_level_directories": sorted(
                EXCLUDED_TOP_LEVEL_DIRECTORIES
            ),
            "excluded_transient_directory_names": sorted(
                EXCLUDED_DIRECTORY_NAMES
            ),
        }
        (target / "BACKUP_SUMMARY.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )
        manifest_files = _create_and_verify_manifest(target, progress)
        git_result = _verify_git_repository(target)
        elapsed = datetime.now() - started
        complete_text = (
            "COMPLETE AND VERIFIED\n"
            f"Completed: {datetime.now():%d %B %Y %H:%M:%S}\n"
            f"Manifest files: {manifest_files:,}\n"
            f"Git verification: {git_result}\n"
            f"Elapsed: {elapsed}\n"
        )
        (target / "BACKUP_COMPLETE.txt").write_text(
            complete_text, encoding="utf-8"
        )
        progress(f"Complete verified backup ready: {target}")
        return target
    except Exception as exc:
        try:
            (target / "BACKUP_FAILED.txt").write_text(
                f"Backup failed at {datetime.now().isoformat()}: {exc}\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        if isinstance(exc, CompleteBackupError):
            raise
        raise CompleteBackupError(str(exc)) from exc


def choose_destination_folder() -> Path | None:
    """Ask the user to select the USB/external drive or a folder on it."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise CompleteBackupError(
            "The folder chooser is unavailable. Pass the USB path on the command line."
        ) from exc
    root = tk.Tk()
    root.withdraw()
    try:
        selected = filedialog.askdirectory(
            title="Select USB/external drive folder for complete ChartSage backup",
            mustexist=True,
        )
    finally:
        root.destroy()
    return Path(selected) if selected else None


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a complete verified ChartSage project backup on a USB or "
            "external drive."
        )
    )
    parser.add_argument(
        "destination",
        nargs="?",
        type=Path,
        help="USB/external destination folder; omit to use the folder chooser",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the final BACKUP confirmation prompt",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    destination = arguments.destination or choose_destination_folder()
    if destination is None:
        print("Backup cancelled; no destination was selected.")
        return 0
    destination = validate_destination(destination)
    print("Building complete project inventory...")
    inventory = build_source_inventory(PROJECT_DIR)
    print(f"Source:      {PROJECT_DIR}")
    print(f"Destination: {destination}")
    print(f"Files:       {len(inventory.files):,}")
    print(f"Data size:   {format_bytes(inventory.total_bytes)}")
    print(
        "Close ChartSage applications before continuing so ordinary JSON and "
        "configuration files remain unchanged during the copy."
    )
    print(
        "This backup contains private project and user data. Keep the external "
        "drive physically secure or encrypted."
    )
    if not arguments.yes:
        confirmation = input("Type BACKUP to create the complete external copy: ")
        if confirmation.strip().casefold() != "backup":
            print("Backup cancelled.")
            return 0
    target = create_complete_backup(destination, inventory=inventory)
    print(f"\nSUCCESS: {target}")
    print("Open RESTORE_INSTRUCTIONS.txt inside that folder for recovery steps.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CompleteBackupError, OSError) as exc:
        print(f"\nBACKUP FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
