"""Create a verified, incrementally updated copy of the ChartSage project.

The destination must be on a different drive from this source folder. The
default target is a directly usable ``ChartSage_COMPLETE_Project_CURRENT``
mirror. The first run copies and fully verifies the complete project. Later
runs let the user choose a fast incremental update (reuse prior verification
for unchanged files) or a full verification (reread every destination file).
A separate timestamped full snapshot remains available on request. Backups
include source code, Git history, databases, downloaded market data,
user-created data, build/runtime assets, distributions, reports, and project
configuration. Only redundant prior backups and transient cache/lock files
are omitted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable


PROJECT_DIR = Path(__file__).resolve().parent
BACKUP_PREFIX = "ChartSage_COMPLETE_Project"
SMART_BACKUP_NAME = f"{BACKUP_PREFIX}_CURRENT"
BACKUP_MARKER_FILE = ".chartsage_complete_backup.json"
BACKUP_STATE_FILE = "BACKUP_STATE.json"
BACKUP_FORMAT_VERSION = 2
FAST_VERIFICATION_MODE = "fast"
FULL_VERIFICATION_MODE = "full"
VERIFICATION_MODES = {FAST_VERIFICATION_MODE, FULL_VERIFICATION_MODE}
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
        # copy2() propagates Windows' read-only attribute from packed Git
        # object files to our temporary file. A read-only temporary cannot be
        # atomically renamed or removed on some external drives. Content is
        # what recovery requires; source timestamps are tracked separately in
        # BACKUP_STATE.json for incremental-change detection.
        shutil.copyfile(source, destination)
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


def _load_json_object(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_manifest(path: Path) -> dict[str, str]:
    records: dict[str, str] = {}
    if not path.is_file():
        return records
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return records
    for line in lines:
        digest, separator, relative = line.partition("  ")
        if separator and len(digest) == 64 and relative:
            records[relative] = digest.casefold()
    return records


def _source_fingerprint(path: Path) -> dict[str, int]:
    """Return stable source metadata, including a SQLite WAL when present."""
    stat = path.stat()
    fingerprint = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    if path.suffix.lower() in SQLITE_SUFFIXES:
        wal_path = Path(f"{path}-wal")
        if wal_path.is_file():
            wal_stat = wal_path.stat()
            fingerprint.update({
                "wal_size": wal_stat.st_size,
                "wal_mtime_ns": wal_stat.st_mtime_ns,
            })
    return fingerprint


def _write_json_atomically(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".chartsage-metadata-", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def _validate_managed_target(target: Path) -> None:
    """Refuse to incrementally alter an unmarked or foreign directory."""
    marker = _load_json_object(target / BACKUP_MARKER_FILE)
    if (
        marker.get("backup_type") != "chartsage_complete_project"
        or int(marker.get("format_version", 0) or 0) != BACKUP_FORMAT_VERSION
    ):
        raise CompleteBackupError(
            f"Refusing to update unrecognised backup directory: {target}. "
            f"Expected a valid {BACKUP_MARKER_FILE} marker."
        )


def _write_backup_marker(target: Path) -> None:
    marker = {
        "backup_type": "chartsage_complete_project",
        "format_version": BACKUP_FORMAT_VERSION,
        "managed_incremental_mirror": target.name == SMART_BACKUP_NAME,
    }
    _write_json_atomically(target / BACKUP_MARKER_FILE, marker)


def _safe_managed_path(target: Path, relative_text: str) -> Path:
    relative = Path(str(relative_text))
    if relative.is_absolute() or ".." in relative.parts:
        raise CompleteBackupError(
            f"Unsafe relative path in prior backup state: {relative_text!r}"
        )
    candidate = (target / relative).resolve()
    try:
        candidate.relative_to(target.resolve())
    except ValueError as exc:
        raise CompleteBackupError(
            f"Prior backup state points outside its target: {relative_text!r}"
        ) from exc
    return candidate


def _unlink_backup_temporary(path: Path) -> None:
    """Remove one utility-owned temporary, including a read-only remnant."""
    if path.is_symlink():
        path.unlink(missing_ok=True)
        return
    try:
        path.chmod(0o600)
    except OSError:
        pass
    path.unlink(missing_ok=True)


def _prepare_managed_destination_for_replace(path: Path) -> None:
    """Clear an old Windows read-only attribute before atomic replacement."""
    if path.is_symlink():
        path.unlink(missing_ok=True)
        return
    if not path.exists():
        return
    try:
        path.chmod(0o600)
    except OSError as exc:
        raise CompleteBackupError(
            f"Cannot make the prior managed backup file replaceable: {path}: {exc}"
        ) from exc


def _cleanup_interrupted_temporaries(target: Path) -> int:
    """Remove only temporary files whose reserved names belong to this tool."""
    removed = 0
    for path in target.rglob("*"):
        if not path.is_file() and not path.is_symlink():
            continue
        if not path.name.startswith((".chartsage-copy-", ".chartsage-metadata-")):
            continue
        _unlink_backup_temporary(path)
        removed += 1
    return removed


def _copy_file_atomically(source: Path, destination: Path) -> str:
    """Copy one source through a same-volume temporary file and return its hash."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".chartsage-copy-", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        if source.suffix.lower() in SQLITE_SUFFIXES:
            _copy_sqlite_database(source, temporary)
        else:
            _copy_regular_file_consistently(source, temporary)
            source_digest = _sha256(source)
            copied_digest = _sha256(temporary)
            if source_digest != copied_digest:
                raise CompleteBackupError(
                    f"Copied file checksum mismatch: {source}"
                )
        digest = _sha256(temporary)
        _prepare_managed_destination_for_replace(destination)
        try:
            os.replace(temporary, destination)
        except PermissionError:
            # Some Windows/external-drive combinations still reject replacing
            # a formerly read-only target even after chmod. The new temporary
            # has already been fully copied and checksummed, so remove only the
            # known managed destination and retry the same-volume rename.
            if destination.exists() or destination.is_symlink():
                _unlink_backup_temporary(destination)
            os.replace(temporary, destination)
        return digest
    finally:
        if temporary.exists():
            _unlink_backup_temporary(temporary)


def _reuse_verified_file(
    destination: Path,
    prior_digest: str,
    current_fingerprint: dict[str, int],
    prior_fingerprint: object,
) -> str | None:
    """Return the verified digest when source metadata is unchanged."""
    if (
        not prior_digest
        or not destination.is_file()
        or not isinstance(prior_fingerprint, dict)
        or current_fingerprint != prior_fingerprint
    ):
        return None
    actual = _sha256(destination)
    return actual if actual.casefold() == prior_digest.casefold() else None


def _reuse_prior_verification(
    prior_digest: str,
    current_fingerprint: dict[str, int],
    prior_fingerprint: object,
    was_in_prior_source: bool,
) -> str | None:
    """Reuse a completed backup's digest without reopening the USB file."""
    if (
        not was_in_prior_source
        or not prior_digest
        or not isinstance(prior_fingerprint, dict)
        or current_fingerprint != prior_fingerprint
    ):
        return None
    return prior_digest.casefold()


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

SMART UPDATES
The default CURRENT backup is updated incrementally. Fast mode compares the
source size and modification time with the prior completed state, reuses the
prior checksums for unchanged files, and copies/checksums only new or changed
files. Full verification mode additionally rereads and checksums every file on
the destination and validates the copied Git repository. Files previously
managed by this backup that were removed from the source project are also
removed from CURRENT.

Use fast mode for routine backups. Run full verification periodically, after
an unsafe drive removal, or whenever external-drive corruption is suspected.
An interrupted or incomplete prior backup automatically forces full
verification before the mirror is marked complete again.

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


def _create_and_verify_manifest(
    target: Path,
    progress: ProgressCallback,
    known_digests: dict[str, str] | None = None,
    *,
    full_verify: bool = True,
    expected_source_files: set[str] | None = None,
) -> int:
    manifest_path = target / "BACKUP_MANIFEST.sha256"
    complete_path = target / "BACKUP_COMPLETE.txt"
    verified_digests = dict(known_digests or {})
    if not full_verify:
        expected = set(expected_source_files or set())
        missing = expected.difference(verified_digests)
        if missing:
            first = sorted(missing, key=str.casefold)[0]
            raise CompleteBackupError(
                f"Fast manifest is missing a source checksum: {first}"
            )
        for name in (
            BACKUP_MARKER_FILE,
            "RESTORE_INSTRUCTIONS.txt",
            "BACKUP_SUMMARY.json",
            BACKUP_STATE_FILE,
        ):
            path = target / name
            if not path.is_file():
                raise CompleteBackupError(
                    f"Fast backup metadata is missing: {name}"
                )
            verified_digests[name] = _sha256(path)
        records = sorted(
            verified_digests.items(), key=lambda item: item[0].casefold()
        )
        progress(
            f"Writing incremental checksum manifest: {len(records):,} files"
        )
        manifest_path.write_text(
            "".join(f"{digest}  {relative}\n" for relative, digest in records),
            encoding="utf-8",
        )
        return len(records)

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
        relative = path.relative_to(target).as_posix()
        digest = verified_digests.get(relative) or _sha256(path)
        records.append((digest, relative))
    manifest_path.write_text(
        "".join(f"{digest}  {relative}\n" for digest, relative in records),
        encoding="utf-8",
    )

    for index, (expected, relative) in enumerate(records, 1):
        if index == 1 or index % 250 == 0 or index == len(records):
            progress(f"Verifying checksums: {index:,}/{len(records):,}")
        # Source files were already checksummed either after their atomic copy
        # or while proving that the prior target copy was still intact. Avoid
        # reading multi-gigabyte unchanged files from the external drive twice.
        if relative in verified_digests:
            continue
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
    new_snapshot: bool = False,
    verification_mode: str = FAST_VERIFICATION_MODE,
) -> Path:
    """Create or incrementally update a verified external project backup."""
    verification_mode = str(verification_mode or "").strip().casefold()
    if verification_mode not in VERIFICATION_MODES:
        raise CompleteBackupError(
            "Verification mode must be 'fast' or 'full'."
        )
    source_root = source_root.resolve()
    destination_parent = validate_destination(
        destination_parent,
        source_root,
        require_different_drive=require_different_drive,
    )
    if inventory is None:
        progress("Building complete project inventory...")
        inventory = build_source_inventory(source_root)
    if new_snapshot or stamp:
        stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
        target = destination_parent / f"{BACKUP_PREFIX}_{stamp}"
        if target.exists():
            raise CompleteBackupError(
                f"Refusing to overwrite existing snapshot: {target}"
            )
        target.mkdir(parents=True)
        incremental_update = False
    else:
        target = destination_parent / SMART_BACKUP_NAME
        incremental_update = target.exists()
        if incremental_update:
            _validate_managed_target(target)
        else:
            target.mkdir(parents=True)
    if not incremental_update:
        _write_backup_marker(target)

    requested_verification_mode = verification_mode
    prior_completed = (target / "BACKUP_COMPLETE.txt").is_file()
    full_verify = (
        verification_mode == FULL_VERIFICATION_MODE or not incremental_update
    )
    if not incremental_update and verification_mode == FAST_VERIFICATION_MODE:
        progress(
            "No completed CURRENT mirror exists yet; the first backup will be "
            "copied and fully verified. Later Fast runs can reuse this state."
        )
    if incremental_update and not full_verify and not prior_completed:
        progress(
            "The prior backup is incomplete or was interrupted; automatically "
            "switching this run to full verification."
        )
        full_verify = True
    effective_verification_mode = (
        FULL_VERIFICATION_MODE if full_verify else FAST_VERIFICATION_MODE
    )
    if full_verify:
        cleaned_temporaries = _cleanup_interrupted_temporaries(target)
        if cleaned_temporaries:
            progress(
                f"Cleaned {cleaned_temporaries:,} interrupted temporary file(s) "
                "from the prior attempt."
            )

    prior_state = _load_json_object(target / BACKUP_STATE_FILE)
    prior_manifest = _load_manifest(target / "BACKUP_MANIFEST.sha256")
    source_files = {relative.as_posix() for relative in inventory.files}
    prior_source_files_value = prior_state.get("source_files", [])
    prior_source_files = {
        str(value) for value in prior_source_files_value
    } if isinstance(prior_source_files_value, list) else set()
    prior_metadata = prior_state.get("source_file_metadata", {})
    if not isinstance(prior_metadata, dict):
        prior_metadata = {}
    if incremental_update and not full_verify and (
        not prior_state or not prior_manifest or not prior_source_files
    ):
        progress(
            "The prior checksum state is unavailable; automatically switching "
            "this run to full verification."
        )
        full_verify = True
        effective_verification_mode = FULL_VERIFICATION_MODE
    source_metadata = {
        relative.as_posix(): _source_fingerprint(source_root / relative)
        for relative in inventory.files
    }
    fast_reused_digests: dict[str, str] = {}
    files_to_copy: list[Path] = []
    if incremental_update and not full_verify:
        for relative in inventory.files:
            relative_text = relative.as_posix()
            digest = _reuse_prior_verification(
                prior_manifest.get(relative_text, ""),
                source_metadata[relative_text],
                prior_metadata.get(relative_text),
                relative_text in prior_source_files,
            )
            if digest:
                fast_reused_digests[relative_text] = digest
            else:
                files_to_copy.append(relative)
    else:
        # Full verification still avoids recopying matching destination files,
        # but it rereads each one to prove that its prior checksum remains true.
        files_to_copy = list(inventory.files)
    if incremental_update and full_verify:
        estimated_copy_bytes = 0
        for relative in inventory.files:
            relative_text = relative.as_posix()
            if not (
                (target / relative).is_file()
                and source_metadata[relative_text]
                == prior_metadata.get(relative_text)
            ):
                estimated_copy_bytes += source_metadata[relative_text]["size"]
    else:
        estimated_copy_bytes = sum(
            source_metadata[relative.as_posix()]["size"]
            for relative in files_to_copy
        )
    retired_file_count = len(prior_source_files.difference(source_files))
    if incremental_update and not full_verify:
        progress(
            "Fast comparison complete: "
            f"{len(fast_reused_digests):,} unchanged, "
            f"{len(files_to_copy):,} new/changed, "
            f"{retired_file_count:,} deleted; "
            f"approximately {format_bytes(estimated_copy_bytes)} to copy."
        )
    required_headroom = max(64 * 1024 * 1024, estimated_copy_bytes // 50)
    free_bytes = shutil.disk_usage(destination_parent).free
    if free_bytes < estimated_copy_bytes + required_headroom:
        raise CompleteBackupError(
            "The selected drive does not have enough free space for changed files. "
            f"Required approximately {estimated_copy_bytes + required_headroom:,} "
            f"bytes; available {free_bytes:,} bytes."
        )

    started = datetime.now()
    copied_files = 0
    reused_files = 0
    removed_files = 0
    known_digests: dict[str, str] = {}
    try:
        for status_name in ("BACKUP_COMPLETE.txt", "BACKUP_FAILED.txt"):
            status_path = target / status_name
            if status_path.exists():
                status_path.unlink()

        prior_directories_value = prior_state.get("source_directories", [])
        prior_directories = {
            str(value) for value in prior_directories_value
        } if isinstance(prior_directories_value, list) else set()
        directories_to_create = (
            [
                relative for relative in inventory.directories
                if relative.as_posix() not in prior_directories
            ]
            if incremental_update and not full_verify
            else list(inventory.directories)
        )
        for relative in directories_to_create:
            (target / relative).mkdir(parents=True, exist_ok=True)

        if incremental_update and not full_verify:
            known_digests.update(fast_reused_digests)
            reused_files = len(fast_reused_digests)
            file_total = len(files_to_copy)
            for index, relative in enumerate(files_to_copy, 1):
                if index == 1 or index % 100 == 0 or index == file_total:
                    progress(
                        f"Copying new/changed files: {index:,}/{file_total:,} - "
                        f"{relative}"
                    )
                relative_text = relative.as_posix()
                known_digests[relative_text] = _copy_file_atomically(
                    source_root / relative, target / relative
                )
                copied_files += 1
        else:
            file_total = len(inventory.files)
            for index, relative in enumerate(inventory.files, 1):
                if index == 1 or index % 100 == 0 or index == file_total:
                    progress(
                        f"Fully verifying files: {index:,}/{file_total:,} - "
                        f"{relative}"
                    )
                source = source_root / relative
                destination = target / relative
                relative_text = relative.as_posix()
                reusable_digest = _reuse_verified_file(
                    destination,
                    prior_manifest.get(relative_text, ""),
                    source_metadata[relative_text],
                    prior_metadata.get(relative_text),
                )
                if reusable_digest:
                    reused_files += 1
                    known_digests[relative_text] = reusable_digest
                else:
                    known_digests[relative_text] = _copy_file_atomically(
                        source, destination
                    )
                    copied_files += 1

        if incremental_update:
            for relative_text in prior_source_files:
                clean_relative = str(relative_text)
                if clean_relative in source_files:
                    continue
                retired = _safe_managed_path(target, clean_relative)
                if retired.is_file():
                    retired.unlink()
                    removed_files += 1

            for relative_text in sorted(
                prior_directories,
                key=lambda value: (value.count("/"), value.casefold()),
                reverse=True,
            ):
                retired_dir = _safe_managed_path(target, relative_text)
                if retired_dir.is_dir():
                    try:
                        retired_dir.rmdir()
                    except OSError:
                        pass

        _write_restore_instructions(target, source_root)
        completed_at = datetime.now().astimezone().isoformat()
        prior_full_verification = str(
            prior_state.get("last_full_verification_at", "") or ""
        )
        last_full_verification = (
            completed_at if full_verify else prior_full_verification
        )
        prior_fast_runs = int(
            prior_state.get("fast_incremental_runs_since_full", 0) or 0
        )
        fast_runs_since_full = 0 if full_verify else prior_fast_runs + 1
        summary = {
            "backup_type": (
                "complete_external_project_incremental_mirror"
                if not new_snapshot and stamp is None
                else "complete_external_project_snapshot"
            ),
            "format_version": BACKUP_FORMAT_VERSION,
            "created_at": datetime.now().astimezone().isoformat(),
            "source": str(source_root),
            "destination": str(target),
            "incremental_update": incremental_update,
            "requested_verification_mode": requested_verification_mode,
            "effective_verification_mode": effective_verification_mode,
            "full_destination_verification_performed": full_verify,
            "last_full_verification_at": last_full_verification,
            "fast_incremental_runs_since_full": fast_runs_since_full,
            "copied_or_refreshed_files": copied_files,
            "reused_verified_files": reused_files,
            "removed_retired_files": removed_files,
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
        _write_json_atomically(target / "BACKUP_SUMMARY.json", summary)
        state = {
            "format_version": BACKUP_FORMAT_VERSION,
            "source_files": sorted(source_files, key=str.casefold),
            "source_file_metadata": source_metadata,
            "source_directories": [
                relative.as_posix() for relative in inventory.directories
            ],
            "last_completed_at": completed_at,
            "last_full_verification_at": last_full_verification,
            "fast_incremental_runs_since_full": fast_runs_since_full,
            "last_verification_mode": effective_verification_mode,
        }
        _write_json_atomically(target / BACKUP_STATE_FILE, state)
        manifest_files = _create_and_verify_manifest(
            target,
            progress,
            known_digests,
            full_verify=full_verify,
            expected_source_files=source_files,
        )
        git_result = (
            _verify_git_repository(target)
            if full_verify
            else "Deferred in fast mode; run Full verification periodically"
        )
        elapsed = datetime.now() - started
        completion_heading = (
            "COMPLETE AND FULLY VERIFIED"
            if full_verify
            else "COMPLETE - FAST INCREMENTAL UPDATE"
        )
        complete_text = (
            f"{completion_heading}\n"
            f"Completed: {datetime.now():%d %B %Y %H:%M:%S}\n"
            f"Verification mode: {effective_verification_mode}\n"
            f"Last full verification: {last_full_verification or 'Not recorded'}\n"
            f"Manifest files: {manifest_files:,}\n"
            f"Copied/refreshed files: {copied_files:,}\n"
            f"Unchanged files reused: {reused_files:,}\n"
            f"Removed retired files: {removed_files:,}\n"
            f"Git verification: {git_result}\n"
            f"Elapsed: {elapsed}\n"
        )
        (target / "BACKUP_COMPLETE.txt").write_text(
            complete_text, encoding="utf-8"
        )
        progress(
            f"Smart backup result ({effective_verification_mode} mode): "
            f"{copied_files:,} copied/refreshed, "
            f"{reused_files:,} unchanged files reused, "
            f"{removed_files:,} retired files removed."
        )
        progress(f"Complete backup ready: {target}")
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


def choose_verification_mode() -> str:
    """Ask an interactive user how deeply to validate the existing mirror."""
    print("\nChoose backup mode:")
    print(
        "  1. Fast incremental update (recommended for routine backups)\n"
        "     Compare saved source metadata and copy/verify only new or changed files."
    )
    print(
        "  2. Full verification\n"
        "     Reread/checksum every USB file and run Git integrity verification."
    )
    while True:
        choice = input("Select 1 or 2 [1]: ").strip().casefold()
        if choice in {"", "1", "fast", "f"}:
            return FAST_VERIFICATION_MODE
        if choice in {"2", "full", "full verify", "full verification", "v"}:
            return FULL_VERIFICATION_MODE
        print("Please enter 1 for Fast incremental or 2 for Full verification.")


def run_backup_gui(
    initial_mode: str = FAST_VERIFICATION_MODE,
    initial_destination: Path | None = None,
    initial_snapshot: bool = False,
) -> int:
    """Run a small responsive GUI over the same verified backup engine."""
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError as exc:
        raise CompleteBackupError(
            "The backup window is unavailable because tkinter is not installed. "
            "Run this file with --console to use terminal mode."
        ) from exc

    root = tk.Tk()
    root.title("ChartSage Complete Project Backup")
    root.geometry("780x590")
    root.minsize(680, 520)

    destination_var = tk.StringVar(
        value=str(initial_destination) if initial_destination is not None else ""
    )
    mode_var = tk.StringVar(
        value=(
            initial_mode
            if initial_mode in VERIFICATION_MODES
            else FAST_VERIFICATION_MODE
        )
    )
    snapshot_var = tk.BooleanVar(value=bool(initial_snapshot))
    status_var = tk.StringVar(value="Choose the external-drive destination.")
    event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
    running = {"value": False}

    outer = ttk.Frame(root, padding=14)
    outer.pack(fill="both", expand=True)
    outer.columnconfigure(0, weight=1)
    outer.rowconfigure(5, weight=1)

    ttk.Label(
        outer,
        text="ChartSage Complete Project Backup",
        font=("Segoe UI", 15, "bold"),
    ).grid(row=0, column=0, sticky="w")
    ttk.Label(
        outer,
        text=(
            "Maintain a directly restorable external copy of the complete project. "
            "Close ChartSage applications before starting."
        ),
        wraplength=740,
    ).grid(row=1, column=0, sticky="ew", pady=(3, 12))

    destination_frame = ttk.LabelFrame(
        outer, text="1. External-drive destination", padding=10
    )
    destination_frame.grid(row=2, column=0, sticky="ew")
    destination_frame.columnconfigure(0, weight=1)
    destination_entry = ttk.Entry(
        destination_frame, textvariable=destination_var
    )
    destination_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))

    def browse_destination() -> None:
        selected = filedialog.askdirectory(
            parent=root,
            title="Select USB/external drive folder for complete ChartSage backup",
            mustexist=True,
        )
        if selected:
            destination_var.set(selected)

    browse_button = ttk.Button(
        destination_frame, text="Browse...", command=browse_destination
    )
    browse_button.grid(row=0, column=1)
    ttk.Label(
        destination_frame,
        text=f"Backup folder created/updated: {SMART_BACKUP_NAME}",
    ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

    mode_frame = ttk.LabelFrame(outer, text="2. Backup mode", padding=10)
    mode_frame.grid(row=3, column=0, sticky="ew", pady=(10, 0))
    mode_frame.columnconfigure(1, weight=1)
    fast_radio = ttk.Radiobutton(
        mode_frame,
        text="Fast incremental update (recommended)",
        variable=mode_var,
        value=FAST_VERIFICATION_MODE,
    )
    fast_radio.grid(row=0, column=0, columnspan=2, sticky="w")
    ttk.Label(
        mode_frame,
        text="Copies and verifies only new or changed files; reuses prior checksums.",
        foreground="#555555",
    ).grid(row=1, column=1, sticky="w", padx=(18, 0), pady=(0, 6))
    full_radio = ttk.Radiobutton(
        mode_frame,
        text="Full verification",
        variable=mode_var,
        value=FULL_VERIFICATION_MODE,
    )
    full_radio.grid(row=2, column=0, columnspan=2, sticky="w")
    ttk.Label(
        mode_frame,
        text="Rereads/checksums every USB file and runs Git integrity verification.",
        foreground="#555555",
    ).grid(row=3, column=1, sticky="w", padx=(18, 0), pady=(0, 6))
    snapshot_check = ttk.Checkbutton(
        mode_frame,
        text="Create a separate timestamped full snapshot instead of updating CURRENT",
        variable=snapshot_var,
    )
    snapshot_check.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

    def update_mode_controls(*_args: object) -> None:
        state = "disabled" if snapshot_var.get() else "normal"
        fast_radio.configure(state=state)
        full_radio.configure(state=state)
        if snapshot_var.get():
            status_var.set("A new snapshot is always copied and fully verified.")
        elif not running["value"]:
            status_var.set("Ready to start the selected backup mode.")

    snapshot_var.trace_add("write", update_mode_controls)

    activity_frame = ttk.LabelFrame(outer, text="3. Activity", padding=8)
    activity_frame.grid(row=5, column=0, sticky="nsew", pady=(10, 0))
    activity_frame.columnconfigure(0, weight=1)
    activity_frame.rowconfigure(2, weight=1)
    status_label = ttk.Label(
        activity_frame, textvariable=status_var, wraplength=720
    )
    status_label.grid(row=0, column=0, sticky="ew")
    progress_bar = ttk.Progressbar(activity_frame, mode="indeterminate")
    progress_bar.grid(row=1, column=0, sticky="ew", pady=(7, 7))
    log_frame = ttk.Frame(activity_frame)
    log_frame.grid(row=2, column=0, sticky="nsew")
    log_frame.columnconfigure(0, weight=1)
    log_frame.rowconfigure(0, weight=1)
    log_text = tk.Text(
        log_frame,
        height=12,
        wrap="word",
        state="disabled",
        font=("Consolas", 9),
    )
    log_text.grid(row=0, column=0, sticky="nsew")
    log_scroll = ttk.Scrollbar(
        log_frame, orient="vertical", command=log_text.yview
    )
    log_scroll.grid(row=0, column=1, sticky="ns")
    log_text.configure(yscrollcommand=log_scroll.set)

    button_frame = ttk.Frame(outer)
    button_frame.grid(row=6, column=0, sticky="ew", pady=(10, 0))
    button_frame.columnconfigure(0, weight=1)

    def append_log(message: object) -> None:
        text = str(message)
        log_text.configure(state="normal")
        log_text.insert("end", text + "\n")
        log_text.see("end")
        log_text.configure(state="disabled")
        status_var.set(text)

    def set_controls_enabled(enabled: bool) -> None:
        widget_state = "normal" if enabled else "disabled"
        destination_entry.configure(state=widget_state)
        browse_button.configure(state=widget_state)
        snapshot_check.configure(state=widget_state)
        start_button.configure(state=widget_state)
        close_button.configure(state=widget_state)
        if enabled:
            update_mode_controls()
        else:
            fast_radio.configure(state="disabled")
            full_radio.configure(state="disabled")

    def backup_worker(
        destination: Path, verification_mode: str, new_snapshot: bool
    ) -> None:
        try:
            event_queue.put(("progress", "Building complete project inventory..."))
            inventory = build_source_inventory(PROJECT_DIR)
            event_queue.put((
                "progress",
                f"Inventory: {len(inventory.files):,} files, "
                f"{format_bytes(inventory.total_bytes)}.",
            ))
            target = create_complete_backup(
                destination,
                inventory=inventory,
                new_snapshot=new_snapshot,
                verification_mode=verification_mode,
                progress=lambda message: event_queue.put(("progress", message)),
            )
            event_queue.put(("done", target))
        except Exception as exc:
            event_queue.put(("error", str(exc)))

    def start_backup() -> None:
        destination_text = destination_var.get().strip()
        if not destination_text:
            messagebox.showwarning(
                "Destination required",
                "Select a folder on the USB or external drive.",
                parent=root,
            )
            return
        destination = Path(destination_text)
        new_snapshot = snapshot_var.get()
        verification_mode = (
            FULL_VERIFICATION_MODE if new_snapshot else mode_var.get()
        )
        mode_label = (
            "new timestamped full snapshot"
            if new_snapshot
            else "Fast incremental update"
            if verification_mode == FAST_VERIFICATION_MODE
            else "Full verification"
        )
        if not messagebox.askyesno(
            "Start ChartSage backup?",
            f"Destination: {destination}\nMode: {mode_label}\n\n"
            "This backup contains private project and user data. Keep the "
            "external drive physically secure or encrypted.\n\nStart now?",
            parent=root,
        ):
            return
        running["value"] = True
        set_controls_enabled(False)
        append_log(f"Starting {mode_label}...")
        progress_bar.start(12)
        threading.Thread(
            target=backup_worker,
            args=(destination, verification_mode, new_snapshot),
            daemon=True,
        ).start()

    start_button = ttk.Button(
        button_frame, text="Start Backup", command=start_backup
    )
    start_button.grid(row=0, column=1, padx=(0, 8))

    def close_window() -> None:
        if running["value"]:
            messagebox.showwarning(
                "Backup running",
                "Please wait for the backup to finish before closing this window.",
                parent=root,
            )
            return
        root.destroy()

    close_button = ttk.Button(
        button_frame, text="Close", command=close_window
    )
    close_button.grid(row=0, column=2)

    def poll_events() -> None:
        try:
            while True:
                event_type, payload = event_queue.get_nowait()
                if event_type == "progress":
                    append_log(payload)
                elif event_type == "done":
                    running["value"] = False
                    progress_bar.stop()
                    set_controls_enabled(True)
                    append_log(f"SUCCESS: {payload}")
                    messagebox.showinfo(
                        "Backup complete",
                        f"The complete ChartSage backup is ready:\n\n{payload}",
                        parent=root,
                    )
                elif event_type == "error":
                    running["value"] = False
                    progress_bar.stop()
                    set_controls_enabled(True)
                    append_log(f"BACKUP FAILED: {payload}")
                    messagebox.showerror(
                        "Backup failed", str(payload), parent=root
                    )
        except queue.Empty:
            pass
        root.after(100, poll_events)

    root.protocol("WM_DELETE_WINDOW", close_window)
    root.after(100, poll_events)
    update_mode_controls()
    destination_entry.focus_set()
    root.mainloop()
    return 0


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
    interface_group = parser.add_mutually_exclusive_group()
    interface_group.add_argument(
        "--gui",
        action="store_true",
        help="open the graphical backup window",
    )
    interface_group.add_argument(
        "--console",
        action="store_true",
        help="use the terminal prompts even when no destination is supplied",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the final BACKUP confirmation prompt",
    )
    parser.add_argument(
        "--new-snapshot",
        action="store_true",
        help=(
            "create a separate timestamped full snapshot instead of updating "
            f"{SMART_BACKUP_NAME}"
        ),
    )
    verification_group = parser.add_mutually_exclusive_group()
    verification_group.add_argument(
        "--fast",
        action="store_true",
        help="quickly update only new, changed and deleted files",
    )
    verification_group.add_argument(
        "--full-verify",
        action="store_true",
        help="reread/checksum every destination file and run git fsck",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    if arguments.gui or (
        not arguments.console
        and arguments.destination is None
        and len(sys.argv) == 1
    ):
        initial_mode = (
            FULL_VERIFICATION_MODE
            if arguments.full_verify
            else FAST_VERIFICATION_MODE
        )
        return run_backup_gui(
            initial_mode,
            initial_destination=arguments.destination,
            initial_snapshot=arguments.new_snapshot,
        )
    destination = arguments.destination or choose_destination_folder()
    if destination is None:
        print("Backup cancelled; no destination was selected.")
        return 0
    destination = validate_destination(destination)
    if arguments.new_snapshot:
        verification_mode = FULL_VERIFICATION_MODE
    elif arguments.full_verify:
        verification_mode = FULL_VERIFICATION_MODE
    elif arguments.fast or arguments.yes:
        verification_mode = FAST_VERIFICATION_MODE
    else:
        verification_mode = choose_verification_mode()
    print("Building complete project inventory...")
    inventory = build_source_inventory(PROJECT_DIR)
    print(f"Source:      {PROJECT_DIR}")
    print(f"Destination: {destination}")
    print(f"Files:       {len(inventory.files):,}")
    print(f"Data size:   {format_bytes(inventory.total_bytes)}")
    if arguments.new_snapshot:
        print("Mode:        New independent timestamped full snapshot")
    elif verification_mode == FULL_VERIFICATION_MODE:
        print(
            f"Mode:        Full verification of {SMART_BACKUP_NAME}; unchanged "
            "USB files will be reread"
        )
    else:
        print(
            f"Mode:        Fast incremental mirror ({SMART_BACKUP_NAME}); "
            "prior checksums are reused for unchanged files"
        )
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
    target = create_complete_backup(
        destination,
        inventory=inventory,
        new_snapshot=arguments.new_snapshot,
        verification_mode=verification_mode,
    )
    print(f"\nSUCCESS: {target}")
    print("Open RESTORE_INSTRUCTIONS.txt inside that folder for recovery steps.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CompleteBackupError, OSError) as exc:
        print(f"\nBACKUP FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
