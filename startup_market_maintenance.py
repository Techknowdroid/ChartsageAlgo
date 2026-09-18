"""Safe automatic market-data maintenance shared by both scanner GUIs.

The three numbered maintenance operations exposed by ``ChartSageBhavcopy``
are coordinated here so ordinary users do not have to launch the feeder first.
Only incremental/download operations are used: this module never rebuilds or
deletes a database.  A small lock file prevents the two GUIs from running the
same maintenance concurrently.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable


APP_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
STATE_PATH = APP_DIR / "market_data_maintenance_state.json"
LOCK_PATH = APP_DIR / "market_data_maintenance.lock"
IST = timezone(timedelta(hours=5, minutes=30))
LOCK_STALE_SECONDS = 10 * 60
_process_lock = threading.Lock()


@dataclass
class MaintenanceStep:
    number: int
    name: str
    status: str = "pending"
    detail: str = ""


@dataclass
class MaintenanceReport:
    started_at: str
    completed_at: str = ""
    already_running: bool = False
    steps: list[MaintenanceStep] = field(default_factory=list)

    @property
    def errors(self) -> list[MaintenanceStep]:
        return [step for step in self.steps if step.status == "failed"]

    @property
    def changed(self) -> list[MaintenanceStep]:
        return [step for step in self.steps if step.status == "updated"]


def _now() -> datetime:
    return datetime.now(IST)


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


class _MaintenanceLock:
    def __init__(self, path: Path):
        self.path = path
        self.acquired = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _attempt in range(2):
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
                payload = json.dumps(
                    {"pid": os.getpid(), "started_at": _now().isoformat(timespec="seconds")}
                ).encode("utf-8")
                try:
                    os.write(descriptor, payload)
                finally:
                    os.close(descriptor)
                self.acquired = True
                return True
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    continue
                if age <= LOCK_STALE_SECONDS:
                    return False
                # Progress refreshes the mtime.  A lock with no heartbeat for
                # ten minutes is from an interrupted/crashed maintenance run.
                try:
                    self.path.unlink()
                except OSError:
                    return False
        return False

    def refresh(self) -> None:
        if self.acquired:
            try:
                self.path.touch()
            except OSError:
                pass

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink(missing_ok=True)
        finally:
            self.acquired = False


def _step_progress(
    callback: Callable[[int, str], None],
    lock: _MaintenanceLock,
    low: int,
    high: int,
    prefix: str,
) -> Callable[[int, int, str], None]:
    def report(done: int, total: int, message: str) -> None:
        lock.refresh()
        fraction = float(done) / max(1, int(total))
        callback(low + round((high - low) * fraction), f"{prefix}: {message}")

    return report


def run_startup_market_maintenance(
    progress: Callable[[int, str], None] | None = None,
) -> MaintenanceReport:
    """Run the three safe feeder updates, skipping work already current.

    Failure of one independent step is recorded and does not erase verified
    data or prevent the remaining steps from being attempted.
    """

    report_progress = progress or (lambda _percent, _message: None)
    report = MaintenanceReport(
        started_at=_now().isoformat(timespec="seconds"),
        steps=[
            MaintenanceStep(1, "Bhavcopy prices"),
            MaintenanceStep(2, "Corporate actions"),
            MaintenanceStep(3, "Historical NIFTY membership"),
        ],
    )
    lock = _MaintenanceLock(LOCK_PATH)
    with _process_lock:
        if not lock.acquire():
            report.already_running = True
            for step in report.steps:
                step.status = "skipped"
                step.detail = "Maintenance is already running in another ChartSage program."
            report.completed_at = _now().isoformat(timespec="seconds")
            report_progress(100, "Market-data maintenance is already running elsewhere.")
            return report

        try:
            from ChartSageBhavcopy import (
                EARLIEST_SUPPORTED_DATE,
                BhavcopyDatabase,
                BhavcopyFeeder,
                default_end_date,
            )
            from membership_update import (
                membership_coverage_summary,
                update_historical_membership,
            )

            state = _read_json(STATE_PATH)
            target = default_end_date()
            database = BhavcopyDatabase()

            # 1. Incremental Start / Resume Backfill.  An empty database gets
            # the complete supported history; an existing one gets only its
            # missing tail. A verified holiday/non-publication is also current.
            step = report.steps[0]
            report_progress(1, "Step 1 of 3: checking official Bhavcopy prices…")
            try:
                stats = database.stats()
                last_text = str(stats.get("last_date") or "")
                target_status = database.status(target)
                if target_status in {"ok", "not_published"}:
                    step.status = "current"
                    step.detail = f"Prices already checked through {target.isoformat()}."
                else:
                    start = (
                        date.fromisoformat(last_text) + timedelta(days=1)
                        if last_text
                        else EARLIEST_SUPPORTED_DATE
                    )
                    feeder = BhavcopyFeeder(
                        database=database,
                        delay_seconds=0.10 if not last_text else 0.0,
                    )
                    summary = feeder.run(
                        start,
                        target,
                        retry_failed=True,
                        progress=_step_progress(
                            report_progress, lock, 2, 55, "Step 1 of 3"
                        ),
                    )
                    final_status = database.status(target)
                    if summary.failed or final_status not in {"ok", "not_published"}:
                        raise RuntimeError(
                            f"{summary.failed} date(s) failed; target status is "
                            f"{final_status or 'missing'}"
                        )
                    step.status = "updated"
                    step.detail = (
                        f"Checked {start.isoformat()} to {target.isoformat()}; "
                        f"downloaded {summary.downloaded:,} file(s)."
                    )
            except Exception as exc:
                step.status = "failed"
                step.detail = str(exc)

            # 2. Corporate actions are independent of the price download.  A
            # 120-day overlap catches revisions without re-fetching every year
            # on every launch; the first coordinated run establishes the full
            # 2014-present baseline.
            step = report.steps[1]
            report_progress(56, "Step 2 of 3: checking corporate actions…")
            actions_state = state.get("corporate_actions")
            actions_state = actions_state if isinstance(actions_state, dict) else {}
            through_text = str(actions_state.get("through") or "")
            try:
                if through_text and date.fromisoformat(through_text) >= target:
                    step.status = "current"
                    step.detail = f"Corporate actions already checked through {through_text}."
                else:
                    actions_start = (
                        max(EARLIEST_SUPPORTED_DATE, target - timedelta(days=120))
                        if through_text
                        else EARLIEST_SUPPORTED_DATE
                    )
                    feeder = BhavcopyFeeder(database=database, delay_seconds=0.0)
                    action_stats = feeder.update_corporate_actions(
                        actions_start,
                        target,
                        progress=_step_progress(
                            report_progress, lock, 57, 76, "Step 2 of 3"
                        ),
                    )
                    step.status = "updated"
                    step.detail = (
                        f"Checked through {target.isoformat()}; "
                        f"{action_stats['supported']:,} supported action(s), "
                        f"{action_stats['ambiguous']:,} requiring review."
                    )
                    state["corporate_actions"] = {
                        "through": target.isoformat(),
                        "updated_at": _now().isoformat(timespec="seconds"),
                    }
                    _atomic_json(STATE_PATH, state)
            except Exception as exc:
                step.status = "failed"
                step.detail = str(exc)

            # 3. The updater itself stages and validates a replacement before
            # installing it.  Do not run it when all supported indices already
            # have verified point-in-time coverage through today.
            step = report.steps[2]
            today = _now().date()
            report_progress(77, "Step 3 of 3: checking historical NIFTY membership…")
            try:
                coverage = membership_coverage_summary(APP_DIR)
                supported = int(coverage.get("supported_indices", 0))
                verified = int(coverage.get("verified_indices", 0))
                covered_through = str(coverage.get("end_date") or "")
                if supported and verified == supported and covered_through >= today.isoformat():
                    step.status = "current"
                    step.detail = (
                        f"All {supported} indices are verified through {covered_through}."
                    )
                else:
                    result = update_historical_membership(
                        APP_DIR,
                        progress=_step_progress(
                            report_progress, lock, 78, 99, "Step 3 of 3"
                        ),
                    )
                    step.status = "updated"
                    step.detail = (
                        f"{result['verified_indices']} indices verified through "
                        f"{result['end_date']}."
                    )
            except Exception as exc:
                step.status = "failed"
                step.detail = str(exc)

            report.completed_at = _now().isoformat(timespec="seconds")
            state = _read_json(STATE_PATH)
            state["last_run"] = {
                "started_at": report.started_at,
                "completed_at": report.completed_at,
                "steps": [asdict(step) for step in report.steps],
            }
            _atomic_json(STATE_PATH, state)
            report_progress(100, "Automatic market-data maintenance completed.")
            return report
        finally:
            lock.release()


def report_text(report: MaintenanceReport) -> str:
    lines = []
    if report.already_running:
        lines.append(
            "Another ChartSage program is already performing market-data maintenance."
        )
    for step in report.steps:
        lines.append(
            f"{step.number}. {step.name}: {step.status.upper()}\n   {step.detail}"
        )
    return "\n\n".join(lines)


def run_startup_maintenance_dialog() -> MaintenanceReport:
    """Run maintenance in a responsive modal PySide6 startup dialog."""

    from PySide6.QtCore import QThread, Qt, Signal
    from PySide6.QtWidgets import (
        QDialog,
        QLabel,
        QProgressBar,
        QVBoxLayout,
    )

    class Worker(QThread):
        progress = Signal(int, str)
        completed = Signal(object)

        def run(self) -> None:
            try:
                result = run_startup_market_maintenance(self.progress.emit)
            except Exception as exc:  # final containment: the GUI must still open
                result = MaintenanceReport(
                    started_at=_now().isoformat(timespec="seconds"),
                    completed_at=_now().isoformat(timespec="seconds"),
                    steps=[MaintenanceStep(0, "Startup maintenance", "failed", str(exc))],
                )
            self.completed.emit(result)

    dialog = QDialog()
    dialog.setWindowTitle("Updating ChartSage Market Data")
    dialog.setModal(True)
    dialog.setMinimumWidth(620)
    dialog.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
    layout = QVBoxLayout(dialog)
    heading = QLabel("Checking the three required market-data updates…")
    heading.setStyleSheet("font-size:14px; font-weight:700;")
    layout.addWidget(heading)
    detail = QLabel("Starting automatic maintenance…")
    detail.setWordWrap(True)
    layout.addWidget(detail)
    bar = QProgressBar()
    bar.setRange(0, 100)
    layout.addWidget(bar)
    note = QLabel(
        "Existing verified data is preserved if the internet is unavailable. "
        "No database rebuild or deletion is performed."
    )
    note.setWordWrap(True)
    note.setStyleSheet("color:#555;")
    layout.addWidget(note)

    result_holder: dict[str, MaintenanceReport] = {}
    worker = Worker(dialog)

    def update(percent: int, message: str) -> None:
        bar.setValue(max(0, min(100, int(percent))))
        detail.setText(message)

    def finish(result: MaintenanceReport) -> None:
        result_holder["result"] = result
        bar.setValue(100)
        dialog.accept()

    worker.progress.connect(update)
    worker.completed.connect(finish)
    worker.start()
    dialog.exec()
    worker.wait()
    return result_holder.get(
        "result",
        MaintenanceReport(
            started_at=_now().isoformat(timespec="seconds"),
            completed_at=_now().isoformat(timespec="seconds"),
            steps=[MaintenanceStep(0, "Startup maintenance", "failed", "No result returned")],
        ),
    )


def show_startup_maintenance_summary(parent: object, report: MaintenanceReport) -> None:
    """Show a completion/error summary only when there is something to report."""

    from PySide6.QtWidgets import QMessageBox

    message = report_text(report)
    if report.errors:
        QMessageBox.warning(
            parent,
            "Market-data maintenance completed with problems",
            "The program will still open and all previously verified data was "
            "preserved. The affected update can be retried at the next startup.\n\n"
            + message,
        )
    elif report.already_running:
        QMessageBox.information(
            parent,
            "Market-data maintenance already running",
            message,
        )
    elif report.changed:
        QMessageBox.information(
            parent,
            "Market-data maintenance completed",
            message,
        )
