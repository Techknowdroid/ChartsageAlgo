"""Create a complete restorable ChartSage project backup inside OPENCODE.

Generated distributions, regenerable Bhavcopy market data, build work, Python
bytecode, old ZIP exports and spreadsheet reports are omitted. SQLite databases
that remain selected are copied with SQLite's online backup API and verified,
so the resulting backup is transactionally consistent.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
BACKUP_ROOT = PROJECT_DIR / "backups"
IMPORTANT_DIRECTORIES = (
    ".opencode",
    "FINALCHARTinkV1_data",
    "historical_membership_sources",
    "nse_calendar",
    "seed_data",
    "_packaging_runtime",
)

# These are the stores containing work that a user would otherwise have to
# recreate manually after a disk failure.  Root-level files are already
# selected automatically and FINALCHARTinkV1_data is selected above; keeping
# this explicit catalogue makes their recovery coverage testable and visible.
USER_CREATED_DATA_STORES = {
    Path("scanner_templates.json"): "scanner templates and structured rule trees",
    Path("watchlists.json"): "ChartSage user-created watchlists",
    Path("custom_filter_examples.json"): "user-created filter examples",
    Path("stock_alerts.db"): "stock-specific alerts, local rules, and alert history",
    Path("paper_trades.db"): "paper-trading portfolios and transaction history",
    Path("FINALCHARTinkV1_data/finalchartink_recipes.json"): (
        "saved FINAL ChartSage strategies and their Step 1/2A/2B/3 selections"
    ),
    Path("FINALCHARTinkV1_data/finalchartink_watchlists.json"): (
        "private, generated, BUY, EXIT, and timestamped FINAL ChartSage watchlists"
    ),
    Path("FINALCHARTinkV1_data/finalchartink_watchlist_metadata.json"): (
        "watchlist creation and regeneration metadata"
    ),
    Path("FINALCHARTinkV1_data/chartsage_watchlists_before_finalchartink_publish.json"): (
        "pre-publication ChartSage watchlist safety snapshot"
    ),
    Path("FINALCHARTinkV1_data/finalchartink_paper_trades.db"): (
        "FINAL ChartSage paper-backtest portfolios and transactions"
    ),
    Path("FINALCHARTinkV1_data/finalchartink_run_history.db"): (
        "FINAL ChartSage run history"
    ),
}

# Large derived stores that can be rebuilt from other files retained in the
# recovery backup.  Keep this list explicit so exclusions cannot silently grow.
REGENERABLE_DATA_STORES = {
    Path("bhavcopy_history.db"): (
        "rebuilt from the retained nse_bhavcopy_archive by ChartSageBhavcopy"
    ),
}
REGENERABLE_DATA_DIRECTORIES = {
    "nse_bhavcopy_archive": "downloaded NSE Bhavcopy source archives",
    "bhavcopy_snapshots": "generated Bhavcopy database snapshots",
}
EXCLUDED_ROOT_DIRECTORIES = {
    "backups",
    "build",
    "dist",
    "__pycache__",
    "_build_tools",
    "_membership_tools",
    "_packaging_deps",
}


def is_transient(path: Path, *, at_project_root: bool = False) -> bool:
    try:
        relative = path.relative_to(PROJECT_DIR)
    except ValueError:
        relative = None
    if relative in USER_CREATED_DATA_STORES:
        # A few active safety/state stores contain words such as "before" in
        # their names.  Their explicit recovery-critical declaration wins over
        # the generic old-copy filename filter below.
        return False
    if relative in REGENERABLE_DATA_STORES:
        return True
    name = path.name.lower()
    if name.endswith((".pyc", ".db-wal", ".db-shm")):
        return True
    if ".seeding." in name or "__pycache__" in path.parts:
        return True
    if at_project_root and path.suffix.lower() in {".zip", ".xlsx"}:
        return True
    if path.suffix.lower() == ".xlsx":
        return True
    return any(marker in name for marker in (
        "_before_", "_backup_", ".superseded_"
    ))


def iter_backup_source_files():
    """Yield every source file selected by the backup inventory rules."""
    for source in PROJECT_DIR.iterdir():
        if source.is_dir():
            if source.name in EXCLUDED_ROOT_DIRECTORIES:
                continue
            if source.name in REGENERABLE_DATA_DIRECTORIES:
                continue
            if source.name not in IMPORTANT_DIRECTORIES:
                continue
            for item in source.rglob("*"):
                if item.is_file() and not is_transient(item):
                    yield item
        elif not is_transient(source, at_project_root=True):
            yield source


def verify_backup_sources_readable() -> None:
    """Fail before creating a partial multi-gigabyte backup on ACL errors."""
    unreadable: list[str] = []
    for source in iter_backup_source_files():
        try:
            with source.open("rb") as handle:
                handle.read(1)
        except OSError as exc:
            unreadable.append(f"{source.relative_to(PROJECT_DIR)}: {exc}")
            if len(unreadable) >= 20:
                break
    if unreadable:
        details = "\n  ".join(unreadable)
        raise PermissionError(
            "Backup preflight found source files that cannot be read. "
            "No new backup folder was created.\n  " + details
        )


def verify_user_created_data_coverage() -> list[tuple[Path, str]]:
    """Verify that every existing user-created store is selected for backup."""
    selected = {
        source.relative_to(PROJECT_DIR)
        for source in iter_backup_source_files()
    }
    covered: list[tuple[Path, str]] = []
    missing: list[Path] = []
    for relative, description in USER_CREATED_DATA_STORES.items():
        source = PROJECT_DIR / relative
        if not source.exists():
            # Some stores are created only after the corresponding feature is
            # first used.  Once present, they become mandatory automatically.
            continue
        if relative not in selected:
            missing.append(relative)
        else:
            covered.append((relative, description))
    if missing:
        names = "\n  ".join(path.as_posix() for path in missing)
        raise RuntimeError(
            "Recovery-critical user data is not selected for backup:\n  " + names
        )
    return covered


def sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True) as source_db:
        with sqlite3.connect(destination) as destination_db:
            source_db.backup(destination_db, pages=4096)
            destination_db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            destination_db.commit()
            destination_db.execute("PRAGMA journal_mode=DELETE")
            destination_db.commit()
    with sqlite3.connect(f"file:{destination.as_posix()}?mode=ro", uri=True) as check_db:
        result = check_db.execute("PRAGMA quick_check").fetchone()
    if result != ("ok",):
        raise RuntimeError(f"SQLite verification failed for {destination}: {result!r}")


def copy_file(source: Path, destination: Path) -> None:
    if source.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
        print(f"Backing up database: {source.relative_to(PROJECT_DIR)}", flush=True)
        sqlite_backup(source, destination)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def copy_directory(source: Path, destination: Path) -> None:
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif not is_transient(item):
            copy_file(item, target)


def create_manifest(backup_dir: Path) -> tuple[int, int]:
    manifest = backup_dir / "BACKUP_MANIFEST.sha256"
    lines: list[str] = []
    total_size = 0
    for path in sorted(backup_dir.rglob("*"), key=lambda value: str(value).lower()):
        if not path.is_file() or path == manifest:
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                digest.update(block)
        relative = path.relative_to(backup_dir).as_posix()
        lines.append(f"{digest.hexdigest()}  {relative}")
        total_size += path.stat().st_size
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines), total_size


def main() -> int:
    print("Verifying user-created templates, rules, strategies, and watchlists...", flush=True)
    covered_user_stores = verify_user_created_data_coverage()
    print("Checking backup source permissions...", flush=True)
    verify_backup_sources_readable()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = (BACKUP_ROOT / f"OPENCODE_Important_{stamp}").resolve()
    backup_dir.relative_to(BACKUP_ROOT.resolve())
    if backup_dir.exists():
        raise FileExistsError(f"Refusing to overwrite {backup_dir}")
    backup_dir.mkdir(parents=True)

    for source in PROJECT_DIR.iterdir():
        if source.is_dir():
            if source.name in EXCLUDED_ROOT_DIRECTORIES:
                continue
            if source.name not in IMPORTANT_DIRECTORIES:
                continue
            print(f"Copying directory: {source.name}", flush=True)
            copy_directory(source, backup_dir / source.name)
        elif not is_transient(source, at_project_root=True):
            copy_file(source, backup_dir / source.name)

    restore_text = f"""ChartSage OPENCODE Important Backup
Created: {datetime.now():%d %B %Y %H:%M:%S}
Source: C:\\OPENCODE

RESTORE
1. Close all ChartSage applications.
2. Copy the complete contents of this backup into a writable OPENCODE folder.
3. Keep all files and directories in their existing relative locations.

INCLUDED
Source code, build specifications/scripts, JSON state, SQLite databases,
historical membership and benchmark data, scanner data, templates, watchlists,
recipes, and the consolidated packaging runtime.

INTENTIONALLY OMITTED
Generated build/dist folders, prior backup folders, __pycache__, legacy split
packaging tool folders, root ZIP distributions, spreadsheet exports, SQLite
WAL/SHM sidecars, superseded/before-change copies, bhavcopy_history.db,
nse_bhavcopy_archive, and bhavcopy_snapshots. Bhavcopy market data is
regenerable; after restoration, run ChartSageBhavcopy to download the required
history again before running scans that need it.

Windows Credential Manager secrets are stored outside C:\\OPENCODE and cannot
be included in this folder backup.
"""
    (backup_dir / "RESTORE_INSTRUCTIONS.txt").write_text(
        restore_text, encoding="utf-8"
    )
    recovery_catalogue = [
        "ChartSage user-created data included in this backup",
        f"Created: {datetime.now():%d %B %Y %H:%M:%S}",
        "",
    ]
    recovery_catalogue.extend(
        f"INCLUDED  {relative.as_posix()}  - {description}"
        for relative, description in covered_user_stores
    )
    recovery_catalogue.extend((
        "",
        "Stores for features never used may not exist yet and are therefore not listed.",
        "Windows Credential Manager secrets are not files and must be configured again",
        "on the restored PC.",
    ))
    (backup_dir / "USER_CREATED_DATA_INCLUDED.txt").write_text(
        "\n".join(recovery_catalogue) + "\n", encoding="utf-8"
    )
    print("Creating SHA-256 integrity manifest...", flush=True)
    file_count, total_size = create_manifest(backup_dir)
    print(f"Backup ready: {backup_dir}")
    print(f"Files: {file_count:,}")
    print(f"Bytes: {total_size:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
