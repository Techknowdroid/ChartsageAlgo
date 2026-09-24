"""Build a complete, relocatable ChartSage Windows distribution.

The output is a new dated folder under ``dist``.  It deliberately contains
shareable market data and user-created scanner content, while personal broker,
Telegram, holdings, session, and executed-order state is replaced with safe
empty defaults.  SQLite databases are copied through SQLite's online backup
API so a running source application cannot produce a torn database copy.
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
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile


PROJECT_DIR = Path(__file__).resolve().parent
DIST_DIR = PROJECT_DIR / "dist"
BUILD_DIR = PROJECT_DIR / "build"
SPEC_FILE = PROJECT_DIR / "ChartSageSuiteComplete.spec"
# Keep the build toolchain self-contained inside the project.  The consolidated
# runtime holds both application wheels and PyInstaller, avoiding stale or
# inaccessible split caches and preventing fallback to user installations.
PACKAGING_RUNTIME = PROJECT_DIR / "_packaging_runtime"
PACKAGING_DEPS = PACKAGING_RUNTIME
BUILD_TOOLS = PACKAGING_RUNTIME
MEMBERSHIP_TOOLS = PACKAGING_RUNTIME

ROOT_SHAREABLE_FILES = (
    "app_settings.json",
    "bhavcopy_state.json",
    "custom_filter_examples.json",
    "nse_sectors_cache.json",
    "nse_symbols_cache.json",
    "nse_trading_calendar.json",
    "scanner_templates.json",
    "watchlists.json",
    "historical_membership_update_state.json",
    "market_data_maintenance_state.json",
)
ROOT_SQLITE_FILES = (
    "bhavcopy_history.db",
    "price_cache.db",
    "historical_index_membership.db",
    "benchmark_history.db",
)
SHAREABLE_DIRECTORIES = (
    "nse_bhavcopy_archive",
    "bhavcopy_snapshots",
    "FINALCHARTinkV1_data",
    # Keep a visible portable seed copy as well as PyInstaller's internal copy.
    # This makes the recovery defaults auditable and lets the runtime smoke
    # test prove they remain present when the suite is moved to another PC.
    "seed_data",
)
PRIVATE_DATA_NAMES = {
    "finalchartink_executed_orders.json",
    "finalchartink_paper_trades.db",
    "finalchartink_paper_trades.json",
    "finalchartink_telegram_state.json",
}
PRIVATE_FILENAMES = (
    "broker_logins.json",
    "holdings_details.json",
    "paper_trades.db",
    "telegram_alert_state.json",
    "telegram_settings.json",
    "today_executed_orders.json",
    "zerodha_session.json",
)
RUNTIME_SOURCE_FILES = (
    "ChartSageAlgoOPENcode.py",
    "ChartSageStockAlerts.py",
    "FINALChartSageV1.py",
    "ChartSageBhavcopy.py",
    "benchmark_history.py",
    "historical_index_membership.py",
    "membership_update.py",
    "nse_calendar/__init__.py",
    "paper_signal_replay.py",
    "startup_market_maintenance.py",
    "stock_alert_engine.py",
    "tickertape_intraday.py",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--name",
        help="Output folder name (default: ChartSageSuiteComplete_YYYYMMDD_HHMMSS)",
    )
    parser.add_argument(
        "--no-zip",
        action="store_true",
        help="Build and verify the portable folder without also making a ZIP.",
    )
    return parser.parse_args()


def ensure_inside_project(path: Path) -> Path:
    resolved = path.resolve()
    resolved.relative_to(PROJECT_DIR)
    return resolved


def sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_db = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    destination_db = sqlite3.connect(destination)
    try:
        source_db.backup(destination_db, pages=4096)
        destination_db.commit()
    finally:
        destination_db.close()
        source_db.close()
    normalize_sqlite_database(destination)
    check_db = sqlite3.connect(f"file:{destination.as_posix()}?mode=ro", uri=True)
    try:
        result = check_db.execute("PRAGMA quick_check").fetchone()
    finally:
        check_db.close()
    if result != ("ok",):
        raise RuntimeError(f"SQLite verification failed for {destination}: {result!r}")


def normalize_sqlite_database(path: Path) -> None:
    """Make a packaged database one stable file without WAL sidecars."""
    # A copied WAL-mode database otherwise leaves -wal/-shm sidecars in the
    # archive and changes on its first open. A portable snapshot is safer as
    # one file; the apps may enable WAL again normally when they need it.
    database = sqlite3.connect(path, timeout=30)
    try:
        database.execute("PRAGMA busy_timeout=30000")
        database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        database.commit()
        mode = database.execute("PRAGMA journal_mode=DELETE").fetchone()
        database.commit()
    finally:
        database.close()
    if not mode or str(mode[0]).lower() != "delete":
        raise RuntimeError(f"Could not normalize SQLite journal mode for {path}")


def copy_shareable_data(output_dir: Path) -> None:
    for filename in ROOT_SHAREABLE_FILES:
        source = PROJECT_DIR / filename
        if source.is_file():
            shutil.copy2(source, output_dir / filename)

    for filename in ROOT_SQLITE_FILES:
        source = PROJECT_DIR / filename
        if not source.is_file():
            raise FileNotFoundError(f"Required database is missing: {source}")
        print(f"Creating consistent database copy: {filename}", flush=True)
        sqlite_backup(source, output_dir / filename)

    for dirname in SHAREABLE_DIRECTORIES:
        source_dir = PROJECT_DIR / dirname
        if not source_dir.is_dir():
            raise FileNotFoundError(f"Required data folder is missing: {source_dir}")
        target_dir = output_dir / dirname
        target_dir.mkdir(parents=True, exist_ok=True)
        for source in source_dir.rglob("*"):
            relative = source.relative_to(source_dir)
            target = target_dir / relative
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif source.name in PRIVATE_DATA_NAMES:
                continue
            elif source.name.endswith(("-wal", "-shm")) or ".seeding." in source.name:
                continue
            elif source.suffix.lower() == ".xlsx":
                # Generated reports are not runtime inputs and may contain the
                # builder's private strategy results.
                continue
            elif any(
                marker in source.name.lower()
                for marker in ("_before_", "_backup_", ".superseded_")
            ):
                continue
            elif source.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                print(f"Creating consistent database copy: {dirname}/{relative}", flush=True)
                sqlite_backup(source, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

    # The forward membership updater needs the compact extracted notice text
    # and current CSV baseline. The source PDFs are audit/build artefacts and
    # are intentionally omitted from the portable bundle to keep it small.
    membership_source = PROJECT_DIR / "historical_membership_sources"
    membership_target = output_dir / "historical_membership_sources"
    manifest = membership_source / "manifest.json"
    current = membership_source / "current"
    if not manifest.is_file() or not current.is_dir():
        raise FileNotFoundError("Historical membership update sources are incomplete")
    membership_target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(manifest, membership_target / manifest.name)
    for source in membership_source.glob("*.txt"):
        shutil.copy2(source, membership_target / source.name)
    shutil.copytree(current, membership_target / "current", dirs_exist_ok=True)

    # Valid, empty per-friend state. Credentials stored in Windows Credential
    # Manager are intentionally impossible and unsafe to copy to another PC.
    (output_dir / "broker_logins.json").write_text("{}\n", encoding="utf-8")
    telegram_defaults = {
        "version": 2,
        "destinations": [
            {"key": "group_1", "label": "Group 1", "chat_id": "", "enabled": False},
            {"key": "group_2", "label": "Group 2", "chat_id": "", "enabled": False},
        ],
    }
    (output_dir / "telegram_settings.json").write_text(
        json.dumps(telegram_defaults, indent=2) + "\n", encoding="utf-8"
    )
    # Alert definitions are personal strategy state. Distributions receive a
    # valid empty database rather than the builder's saved alerts/history.
    from stock_alert_engine import StockAlertStore

    stock_alerts_path = output_dir / "stock_alerts.db"
    StockAlertStore(stock_alerts_path)
    normalize_sqlite_database(stock_alerts_path)


def write_build_info(output_dir: Path) -> None:
    """Record exactly which current source revision was compiled."""
    source_hashes: dict[str, str] = {}
    for relative in RUNTIME_SOURCE_FILES:
        source = PROJECT_DIR / relative
        if not source.is_file():
            raise FileNotFoundError(f"Required runtime source is missing: {source}")
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        source_hashes[Path(relative).as_posix()] = digest.hexdigest()
    payload = {
        "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "bundle_format": 1,
        "compiled_source_sha256": source_hashes,
    }
    (output_dir / "BUILD_INFO.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def repair_qt_runtime_layout(output_dir: Path) -> None:
    """Place Qt/Shiboken DLLs beside their frozen Python extension modules.

    PyInstaller can preserve the name of a workspace-local dependency folder
    when analysing wheel DLLs.  That produces ``_internal/_packaging_runtime``
    paths which may work on the build PC (because its installed Qt is visible)
    but fail on a clean PC.  PySide6 and shiboken6 add their own package
    directories to the Windows DLL search path, so copy the wheel DLLs there.
    """
    internal_dir = output_dir / "_internal"
    for package in ("PySide6", "shiboken6"):
        source_dir = PACKAGING_DEPS / package
        target_dir = internal_dir / package
        if not source_dir.is_dir() or not target_dir.is_dir():
            raise FileNotFoundError(
                f"Required packaged Qt directory is missing: {source_dir} or {target_dir}"
            )
        copied = 0
        for source in source_dir.glob("*.dll"):
            shutil.copy2(source, target_dir / source.name)
            copied += 1
        if not copied:
            raise RuntimeError(f"No runtime DLLs found for {package}")


def write_readme(output_dir: Path) -> None:
    text = f"""ChartSage Complete Portable Suite
Built: {datetime.now():%d %B %Y %H:%M}

APPLICATIONS
  ChartSageAlgo.exe       Main scanner
  ChartSageStockAlerts.exe  Stock-specific technical alert monitor
  FINALChartSageV1.exe    Final ChartSage strategy-guidance scanner
  ChartSageBhavcopy.exe   Official NSE Bhavcopy feeder and maintenance GUI

HOW TO INSTALL
  1. Extract the whole ZIP to one writable folder, for example
     C:\\ChartSageSuite or D:\\Apps\\ChartSageSuite.
  2. Do not run an EXE from inside the ZIP.
  3. Keep all four EXEs, the _internal folder, databases, JSON files, and data
     folders together. You may create shortcuts, but do not move an EXE alone.
  4. Avoid C:\\Program Files because the suite writes updates beside its EXEs.

PORTABILITY
  All runtime paths are based on the folder containing each EXE. No Python
  installation is required. The included databases, Bhavcopy raw archive,
  snapshots, scanner templates, watchlists, caches, recipes, and final-scanner
  data move with this folder.

PRIVATE SETTINGS
  Broker credentials, Zerodha sessions, holdings, order history, Telegram chat
  IDs/tokens, and paper-trading state from the builder's PC are not distributed.
  Each friend must enter their own values. Secret tokens are stored by Windows
  Credential Manager on that PC and cannot be safely bundled.

UPDATES AND BACKUPS
  Starting ChartSageAlgo.exe or FINALChartSageV1.exe automatically checks the
  three routine maintenance steps: missing Bhavcopy sessions, corporate
  actions, and historical NIFTY membership. Current steps are skipped. The
  standalone ChartSageBhavcopy.exe remains available for verification,
  snapshots, manual maintenance, and exceptional database repair.
  Before replacing this folder with a newer release, preserve any JSON/DB files
  and the FINALCHARTinkV1_data folder that the user wants to keep.

See MANIFEST.sha256 for a file-integrity inventory.
"""
    (output_dir / "README.txt").write_text(text, encoding="utf-8")


def create_manifest(output_dir: Path) -> None:
    manifest_path = output_dir / "MANIFEST.sha256"
    lines: list[str] = []
    for path in sorted(output_dir.rglob("*"), key=lambda item: str(item).lower()):
        if not path.is_file() or path == manifest_path:
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                digest.update(block)
        relative = path.relative_to(output_dir).as_posix()
        lines.append(f"{digest.hexdigest()}  {relative}")
    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def verify_manifest(output_dir: Path) -> None:
    manifest_path = output_dir / "MANIFEST.sha256"
    expected: dict[str, str] = {}
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        expected[relative] = digest
    actual = {
        path.relative_to(output_dir).as_posix()
        for path in output_dir.rglob("*")
        if path.is_file() and path != manifest_path
    }
    missing = set(expected) - actual
    extra = actual - set(expected)
    bad: list[str] = []
    for relative, wanted in expected.items():
        path = output_dir / relative
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != wanted:
            bad.append(relative)
    if missing or extra or bad:
        raise RuntimeError(
            "Manifest verification failed: "
            f"{len(missing)} missing, {len(extra)} extra, {len(bad)} changed"
        )


def validate_output(output_dir: Path) -> None:
    required = (
        "ChartSageAlgo.exe",
        "ChartSageStockAlerts.exe",
        "FINALChartSageV1.exe",
        "ChartSageBhavcopy.exe",
        "_internal",
        "bhavcopy_history.db",
        "price_cache.db",
        "historical_index_membership.db",
        "benchmark_history.db",
        "historical_membership_sources",
        "historical_membership_sources/current",
        "BUILD_INFO.json",
        "app_settings.json",
        "custom_filter_examples.json",
        "nse_symbols_cache.json",
        "nse_trading_calendar.json",
        "scanner_templates.json",
        "watchlists.json",
        "stock_alerts.db",
        "nse_bhavcopy_archive",
        "bhavcopy_snapshots",
        "FINALCHARTinkV1_data",
        "FINALCHARTinkV1_data/finalchartink_nse_trading_calendar.json",
        "FINALCHARTinkV1_data/finalchartink_price_cache.db",
        "FINALCHARTinkV1_data/finalchartink_recipes.json",
        "FINALCHARTinkV1_data/finalchartink_run_history.db",
        "FINALCHARTinkV1_data/finalchartink_watchlists.json",
        "seed_data",
    )
    missing = [name for name in required if not (output_dir / name).exists()]
    if missing:
        raise RuntimeError("Portable bundle is incomplete: " + ", ".join(missing))
    leaked = [
        name
        for name in PRIVATE_FILENAMES
        if (output_dir / name).exists()
        and name not in {"broker_logins.json", "telegram_settings.json"}
    ]
    if leaked:
        raise RuntimeError("Private files unexpectedly entered bundle: " + ", ".join(leaked))
    nested_private = sorted(
        path.relative_to(output_dir).as_posix()
        for path in output_dir.rglob("*")
        if path.is_file() and path.name in PRIVATE_DATA_NAMES
    )
    if nested_private:
        raise RuntimeError(
            "Private runtime state unexpectedly entered bundle: "
            + ", ".join(nested_private)
        )
    generated_reports = sorted(
        path.relative_to(output_dir).as_posix()
        for path in output_dir.rglob("*.xlsx")
    )
    if generated_reports:
        raise RuntimeError(
            "Generated reports unexpectedly entered bundle: "
            + ", ".join(generated_reports[:20])
        )
    required_qt_runtime = (
        "_internal/shiboken6/Shiboken.pyd",
        "_internal/shiboken6/shiboken6.abi3.dll",
        "_internal/shiboken6/MSVCP140.dll",
        "_internal/PySide6/pyside6.abi3.dll",
        "_internal/PySide6/Qt6Core.dll",
        "_internal/PySide6/Qt6Gui.dll",
        "_internal/PySide6/Qt6Widgets.dll",
        "_internal/PySide6/MSVCP140.dll",
    )
    missing_qt = [
        relative for relative in required_qt_runtime
        if not (output_dir / Path(relative)).is_file()
    ]
    if missing_qt:
        raise RuntimeError(
            "Portable Qt/Shiboken runtime is incomplete: " + ", ".join(missing_qt)
        )


def verify_packaged_databases(output_dir: Path) -> None:
    databases = sorted(output_dir.rglob("*.db"))
    if not databases:
        raise RuntimeError("Portable bundle contains no SQLite databases")
    for path in databases:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as database:
            result = database.execute("PRAGMA quick_check").fetchone()
        if result != ("ok",):
            raise RuntimeError(
                f"Packaged SQLite verification failed for "
                f"{path.relative_to(output_dir)}: {result!r}"
            )
    print(f"Verified {len(databases):,} packaged SQLite database(s).", flush=True)


def reject_development_paths(output_dir: Path) -> None:
    """Reject portable text/config files that still name the build folder."""
    needles = {
        str(PROJECT_DIR).casefold(),
        str(PROJECT_DIR).replace("\\", "/").casefold(),
    }
    text_suffixes = {
        ".cfg", ".csv", ".ini", ".json", ".md", ".txt", ".yaml", ".yml"
    }
    leaked: list[str] = []
    for path in output_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in text_suffixes:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore").casefold()
        except OSError:
            continue
        if any(needle and needle in content for needle in needles):
            leaked.append(path.relative_to(output_dir).as_posix())
    if leaked:
        raise RuntimeError(
            "Development-only absolute path entered portable files: "
            + ", ".join(leaked[:20])
        )


def smoke_test_executables(output_dir: Path) -> None:
    """Load every staged EXE using only the bundle and Windows system DLLs."""
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    system_root = Path(environment.get("SystemRoot", r"C:\Windows"))
    environment["PATH"] = os.pathsep.join(
        (
            str(output_dir),
            str(output_dir / "_internal"),
            str(system_root / "System32"),
            str(system_root),
        )
    )
    for executable_name in (
        "ChartSageAlgo.exe",
        "ChartSageStockAlerts.exe",
        "FINALChartSageV1.exe",
        "ChartSageBhavcopy.exe",
    ):
        executable = output_dir / executable_name
        print(f"Clean-bundle smoke test: {executable_name}", flush=True)
        try:
            completed = subprocess.run(
                [str(executable), "--portable-smoke-test"],
                cwd=output_dir,
                env=environment,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Smoke test timed out: {executable_name}") from exc
        if completed.returncode:
            details = (completed.stdout + "\n" + completed.stderr).strip()
            raise RuntimeError(
                f"Smoke test failed for {executable_name} with exit code "
                f"{completed.returncode}: {details[-2000:]}"
            )


def create_zip(output_dir: Path) -> Path:
    zip_path = output_dir.with_suffix(".zip")
    if zip_path.exists():
        raise FileExistsError(f"Refusing to overwrite existing archive: {zip_path}")
    print(f"Creating ZIP: {zip_path.name}", flush=True)
    with ZipFile(zip_path, "w", allowZip64=True) as archive:
        for path in sorted(output_dir.rglob("*"), key=lambda item: str(item).lower()):
            if not path.is_file():
                continue
            relative = Path(output_dir.name) / path.relative_to(output_dir)
            # Databases, XLSX files and NSE ZIPs are already compressed or very
            # large, so storing them avoids hours of ineffective recompression.
            method = ZIP_STORED if path.suffix.lower() in {".db", ".xlsx", ".zip"} else ZIP_DEFLATED
            archive.write(path, relative.as_posix(), compress_type=method)
    return zip_path


def run_pyinstaller(bundle_name: str) -> Path:
    output_dir = ensure_inside_project(DIST_DIR / bundle_name)
    if output_dir.exists():
        raise FileExistsError(f"Refusing to overwrite existing distribution: {output_dir}")
    work_dir = ensure_inside_project(BUILD_DIR / bundle_name)
    env = os.environ.copy()
    env["CHARTSAGE_BUNDLE_NAME"] = bundle_name
    command = [
        sys.executable,
        "-c",
        (
            "import os,sys,site; "
            "sys.path[:] = [p for p in sys.path if "
            "'AppData\\\\Roaming\\\\Python' not in p]; "
            "site.getusersitepackages = lambda: ''; "
            f"sys.path.insert(0, {str(PACKAGING_DEPS)!r}); "
            f"sys.path.insert(0, {str(BUILD_TOOLS)!r}); "
            f"sys.path.insert(0, {str(MEMBERSHIP_TOOLS)!r}); "
            "from PyInstaller.__main__ import run; "
            "run(sys.argv[1:])"
        ),
        "--noconfirm",
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(work_dir),
        str(SPEC_FILE),
    ]
    subprocess.run(command, cwd=PROJECT_DIR, env=env, check=True)
    return output_dir


def main() -> int:
    args = parse_args()
    bundle_name = args.name or f"ChartSageSuiteComplete_{datetime.now():%Y%m%d_%H%M%S}"
    if not bundle_name or any(char in bundle_name for char in '<>:"/\\|?*'):
        raise ValueError("Bundle name contains characters invalid in a Windows folder name.")
    output_dir = run_pyinstaller(bundle_name)
    repair_qt_runtime_layout(output_dir)
    copy_shareable_data(output_dir)
    write_build_info(output_dir)
    write_readme(output_dir)
    validate_output(output_dir)
    verify_packaged_databases(output_dir)
    reject_development_paths(output_dir)
    smoke_test_executables(output_dir)
    print("Creating integrity manifest...", flush=True)
    create_manifest(output_dir)
    verify_manifest(output_dir)
    zip_path = None if args.no_zip else create_zip(output_dir)
    print(f"Portable folder ready: {output_dir}")
    if zip_path:
        print(f"Shareable ZIP ready: {zip_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
