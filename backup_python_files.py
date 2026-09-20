"""Back up the root-level Python source files from the OPENCODE folder.

This is intentionally a source-only convenience backup.  It does not replace
``create_important_backup.py``, which also preserves databases, configuration,
historical data, runtime dependencies, and other files needed for restoration.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
from datetime import datetime
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
BACKUP_ROOT = PROJECT_DIR / "backups"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def python_sources() -> list[Path]:
    """Return only .py files located directly inside the project folder."""
    return sorted(
        (path for path in PROJECT_DIR.glob("*.py") if path.is_file()),
        key=lambda path: path.name.casefold(),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a timestamped backup containing only the root-level "
            "Python files from this OPENCODE folder."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the files that would be backed up without creating anything.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    sources = python_sources()
    if not sources:
        raise FileNotFoundError(f"No Python files were found in {PROJECT_DIR}")

    print(f"Python source folder: {PROJECT_DIR}")
    print(f"Python files found: {len(sources):,}")
    if args.dry_run:
        for source in sources:
            print(f"  {source.name}")
        print("Dry run complete; no backup folder was created.")
        return 0

    # Hash every source before creating the destination. This is both a
    # readability preflight and the expected checksum used after each copy.
    expected_hashes = {source: sha256_file(source) for source in sources}

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_dir = (BACKUP_ROOT / f"OPENCODE_Python_Source_{stamp}").resolve()
    backup_dir.relative_to(BACKUP_ROOT.resolve())
    if backup_dir.exists():
        raise FileExistsError(f"Refusing to overwrite {backup_dir}")
    backup_dir.mkdir(parents=True)

    manifest_lines: list[str] = []
    total_bytes = 0
    for source in sources:
        destination = backup_dir / source.name
        shutil.copy2(source, destination)
        copied_hash = sha256_file(destination)
        if copied_hash != expected_hashes[source]:
            raise RuntimeError(f"Checksum verification failed for {source.name}")
        manifest_lines.append(f"{copied_hash}  {source.name}")
        total_bytes += destination.stat().st_size
        print(f"Copied: {source.name}")

    manifest = backup_dir / "PYTHON_SOURCE_MANIFEST.sha256"
    manifest.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")

    print(f"Python source backup ready: {backup_dir}")
    print(f"Python files: {len(sources):,}")
    print(f"Bytes: {total_bytes:,}")
    print(
        "Note: this contains source code only. Use create_important_backup.py "
        "for a complete restorable project backup."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
