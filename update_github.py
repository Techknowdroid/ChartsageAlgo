"""Safely review, commit, and push this project to its configured Git remote.

This helper is intentionally conservative:

* it operates from the folder containing this file;
* it honours the repository's ``.gitignore`` rules;
* it refuses to mix its work with files that are already staged;
* it displays the proposed file list and summary before committing;
* it checks staged filenames and text for common credential mistakes; and
* it proposes a concise conventional commit message from the staged files; and
* it requires explicit confirmation before both commit and push.

This is a source-control update, not a restorable operational-data backup.
Continue to run ``create_important_backup.py`` for databases, archives,
personal configuration, and other local data intentionally excluded from Git.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent

# These paths should remain local even if .gitignore is accidentally changed.
BLOCKED_NAME_PARTS = (
    "enctoken",
    "access_token",
    "api_token",
    "bot_token",
    "telegram_token",
    "credentials",
    "secrets",
    "paper_trades.db",
    "price_cache.db",
    "bhavcopy_history.db",
)

# Match likely literal secrets, but not ordinary source-code variable names.
SECRET_VALUE_PATTERNS = (
    re.compile(
        rb"(?i)(?:api[_ -]?key|api[_ -]?secret|access[_ -]?token|enctoken|"
        rb"bot[_ -]?token|client[_ -]?secret)\s*[:=]\s*[\"'][^\"'\r\n]{8,}[\"']"
    ),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"(?i)authorization\s*[:=]\s*[\"']?bearer\s+[A-Za-z0-9._~+/-]{12,}"),
)


class GitUpdateError(RuntimeError):
    """Raised for a safe, user-correctable Git update failure."""


def run_git(*arguments: str, capture: bool = True) -> str:
    """Run Git in this project and return its standard output."""
    result = subprocess.run(
        ["git", *arguments],
        cwd=PROJECT_DIR,
        check=False,
        capture_output=capture,
        text=capture,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip() if capture else ""
        raise GitUpdateError(detail or f"git {' '.join(arguments)} failed")
    return result.stdout if capture else ""


def confirm(prompt: str) -> bool:
    """Return True only for an explicit yes response."""
    return input(f"{prompt} [y/N]: ").strip().casefold() in {"y", "yes"}


def staged_paths() -> list[str]:
    output = run_git("diff", "--cached", "--name-only", "-z")
    return [path for path in output.split("\0") if path]


def staged_changes() -> list[tuple[str, str]]:
    """Return ``(status, current_path)`` pairs for the staged snapshot."""
    output = run_git("diff", "--cached", "--name-status", "--find-renames")
    changes: list[tuple[str, str]] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0].strip().upper()
        # Rename/copy records contain old and new paths; describe the current one.
        current_path = parts[-1].strip()
        if current_path:
            changes.append((status, current_path))
    return changes


def _join_subjects(subjects: list[str]) -> str:
    if len(subjects) == 1:
        return subjects[0]
    if len(subjects) == 2:
        return f"{subjects[0]} and {subjects[1]}"
    return "ChartSage application suite"


def suggest_commit_message(changes: list[tuple[str, str]]) -> str:
    """Create a conservative commit subject from the staged file inventory."""
    if not changes:
        return "chore: update project files"

    product_rules = (
        ("stock alert monitoring", {
            "chartsagestockalerts.py", "stock_alert_engine.py",
            "launch_stock_alerts.py", "runchartsagestockalerts.cmd",
        }),
        ("final strategy workflow", {"finalchartinkv1.py"}),
        ("scanner workflow", {"chartsagealgoopencode.py"}),
        ("market-data maintenance", {"chartsagebhavcopy.py"}),
        ("backtesting", {"runbacktest52.py"}),
    )
    product_areas: list[str] = []
    product_statuses: list[str] = []
    lower_changes = [(status, path.replace("\\", "/").casefold()) for status, path in changes]
    for area, filenames in product_rules:
        matching = [
            status for status, path in lower_changes
            if Path(path).name in filenames
        ]
        if matching:
            product_areas.append(area)
            product_statuses.extend(matching)

    if product_areas:
        verb = "add" if product_statuses and all(s.startswith("A") for s in product_statuses) else "improve"
        return f"feat: {verb} {_join_subjects(product_areas)}"

    paths = [path for _status, path in lower_changes]
    if all(path.endswith((".md", ".txt")) for path in paths):
        return "docs: update project documentation"
    if all(
        Path(path).name.startswith(("test_", "_test_"))
        or "/tests/" in f"/{path}"
        for path in paths
    ):
        return "test: update project test coverage"
    if all(
        path.endswith(".spec")
        or Path(path).name.startswith("build_")
        or "packag" in Path(path).name
        for path in paths
    ):
        return "build: update portable packaging"
    if any(Path(path).name.startswith("create_important_backup") for path in paths):
        return "chore: improve project backup tooling"
    return "chore: update ChartSage project files"


def check_staged_content(paths: list[str]) -> list[str]:
    """Return safe descriptions of possible secret leaks without values."""
    warnings: list[str] = []
    for relative_path in paths:
        normalized = relative_path.replace("\\", "/").casefold()
        if any(part in normalized for part in BLOCKED_NAME_PARTS):
            warnings.append(f"blocked or private-looking filename: {relative_path}")

        # Read the staged Git object, not a potentially different working copy.
        blob = subprocess.run(
            ["git", "show", f":{relative_path}"],
            cwd=PROJECT_DIR,
            check=False,
            capture_output=True,
        )
        if blob.returncode != 0 or b"\x00" in blob.stdout[:8192]:
            continue
        if any(pattern.search(blob.stdout) for pattern in SECRET_VALUE_PATTERNS):
            warnings.append(f"possible credential value in: {relative_path}")
    return warnings


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Review, commit, and push safe project source changes."
    )
    parser.add_argument(
        "message",
        nargs="?",
        help='commit message, for example: "fix: improve history report"',
    )
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    print(f"Project: {PROJECT_DIR}")

    if not (PROJECT_DIR / ".git").exists():
        raise GitUpdateError("This folder is not the expected Git repository.")
    if not (PROJECT_DIR / ".gitignore").is_file():
        raise GitUpdateError(".gitignore is missing; refusing to stage files.")

    branch = run_git("branch", "--show-current").strip()
    if not branch:
        raise GitUpdateError("Git is in detached-HEAD mode; no update was made.")

    remote = run_git("remote", "get-url", "origin").strip()
    print(f"Branch:  {branch}")
    print(f"Remote:  {remote}")

    already_staged = staged_paths()
    if already_staged:
        print("\nFiles are already staged:")
        for path in already_staged:
            print(f"  {path}")
        raise GitUpdateError(
            "Review/commit the existing staged work first; this helper will not mix it "
            "with a new update."
        )

    status = run_git("status", "--short")
    if not status.strip():
        print("\nNothing has changed; GitHub is already up to date.")
        return 0

    print("\nCurrent changes (ignored operational/private files are not shown):")
    print(status.rstrip())
    if not confirm("Stage these changes for review?"):
        print("No files were staged.")
        return 0

    run_git("add", "-A")
    paths = staged_paths()
    if not paths:
        print("Nothing eligible was staged.")
        return 0

    print("\nProposed commit files:")
    for path in paths:
        print(f"  {path}")
    print("\nProposed commit summary:")
    print(run_git("diff", "--cached", "--stat").rstrip())

    warnings = check_staged_content(paths)
    if warnings:
        print("\nSAFETY CHECK FAILED:")
        for warning in warnings:
            print(f"  - {warning}")
        print(
            "Nothing was committed or pushed. The files remain staged so you can "
            "inspect them with: git diff --cached"
        )
        return 2

    message = (args.message or "").strip()
    if not message:
        suggested = suggest_commit_message(staged_changes())
        print(f"\nSuggested commit message: {suggested}")
        replacement = input(
            "Press Enter to use it, or type a replacement: "
        ).strip()
        message = replacement or suggested

    if not confirm(f'Commit these files as "{message}"?'):
        print("Nothing was committed or pushed. The reviewed files remain staged.")
        return 0

    run_git("commit", "-m", message, capture=False)
    print("\nCommit created successfully.")

    if not confirm(f"Push branch {branch} to origin now?"):
        print("Commit retained locally. Run 'git push' later when ready.")
        return 0

    run_git("push", "origin", branch, capture=False)
    print("\nGitHub update completed successfully.")
    print("Reminder: GitHub is not the full data backup; also run create_important_backup.py.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GitUpdateError, OSError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
