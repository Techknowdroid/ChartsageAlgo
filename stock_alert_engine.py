"""Persistent state and trigger policy for ChartSage stock-specific alerts."""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2
IST = timezone(timedelta(hours=5, minutes=30))
TRIGGER_ONCE = "ONCE"
TRIGGER_TRANSITION = "TRANSITION"
TRIGGER_EVERY_MATCH = "EVERY_MATCH"
TRIGGER_MODES = {TRIGGER_ONCE, TRIGGER_TRANSITION, TRIGGER_EVERY_MATCH}


def now_iso() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def should_trigger(mode: str, matched: bool, previous: bool | None) -> bool:
    """Return whether this evaluation should create a notification event."""
    if not matched:
        return False
    clean_mode = str(mode or TRIGGER_TRANSITION).upper()
    if clean_mode == TRIGGER_EVERY_MATCH:
        return True
    if clean_mode in {TRIGGER_ONCE, TRIGGER_TRANSITION}:
        return previous is not True
    raise ValueError(f"Unsupported trigger mode: {mode!r}")


def next_due_at(alert: dict[str, Any]) -> datetime:
    last = str(alert.get("last_evaluated_at", "") or "").strip()
    if not last:
        return datetime(1970, 1, 1, tzinfo=IST)
    try:
        parsed = datetime.fromisoformat(last)
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=IST)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed + timedelta(minutes=max(1, int(alert.get("interval_minutes", 5))))


class StockAlertStore:
    """Small transactional SQLite store for definitions, state and audit events."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _database(self):
        """Yield a transactional connection and always release its file lock."""
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._database() as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    template_name TEXT NOT NULL,
                    setup_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    include_current INTEGER NOT NULL DEFAULT 1,
                    adjusted INTEGER NOT NULL DEFAULT 1,
                    interval_minutes INTEGER NOT NULL DEFAULT 5,
                    trigger_mode TEXT NOT NULL DEFAULT 'TRANSITION',
                    telegram_enabled INTEGER NOT NULL DEFAULT 1,
                    expires_at TEXT NOT NULL DEFAULT '',
                    last_condition INTEGER,
                    last_evaluated_at TEXT NOT NULL DEFAULT '',
                    last_triggered_at TEXT NOT NULL DEFAULT '',
                    trigger_count INTEGER NOT NULL DEFAULT 0,
                    last_close REAL,
                    last_evidence TEXT NOT NULL DEFAULT '',
                    last_telegram_at TEXT NOT NULL DEFAULT '',
                    last_telegram_status TEXT NOT NULL DEFAULT '',
                    last_telegram_error TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS alerts_enabled_due
                    ON alerts(enabled, last_evaluated_at);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    matched INTEGER,
                    close_value REAL,
                    evidence TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(alert_id) REFERENCES alerts(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS events_alert_time
                    ON events(alert_id, occurred_at DESC);
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            existing_columns = {
                str(row[1])
                for row in database.execute("PRAGMA table_info(alerts)").fetchall()
            }
            for name, declaration in (
                ("last_telegram_at", "TEXT NOT NULL DEFAULT ''"),
                ("last_telegram_status", "TEXT NOT NULL DEFAULT ''"),
                ("last_telegram_error", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in existing_columns:
                    database.execute(
                        f"ALTER TABLE alerts ADD COLUMN {name} {declaration}"
                    )
            database.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for key in ("enabled", "include_current", "adjusted", "telegram_enabled"):
            result[key] = bool(result.get(key))
        value = result.get("last_condition")
        result["last_condition"] = None if value is None else bool(value)
        try:
            result["setup"] = json.loads(result.pop("setup_json"))
        except (TypeError, json.JSONDecodeError):
            result["setup"] = {}
        return result

    @staticmethod
    def _clean_definition(value: dict[str, Any]) -> dict[str, Any]:
        symbol = "".join(str(value.get("symbol", "")).strip().upper().split())
        name = str(value.get("name", "") or "").strip()
        template_name = str(value.get("template_name", "") or "").strip()
        setup = value.get("setup")
        if not symbol or not all(character.isalnum() or character in ".&-" for character in symbol):
            raise ValueError("Enter a valid NSE stock symbol.")
        if not name:
            name = f"{symbol} — Stock Alert"
        if not template_name or not isinstance(setup, dict):
            raise ValueError("Build or import a valid alert rule.")
        interval = int(value.get("interval_minutes", 5) or 5)
        if interval not in {1, 2, 5, 10, 15, 30, 60}:
            raise ValueError("Unsupported monitoring interval.")
        trigger_mode = str(value.get("trigger_mode", TRIGGER_TRANSITION)).upper()
        if trigger_mode not in TRIGGER_MODES:
            raise ValueError("Unsupported trigger frequency.")
        expires_at = str(value.get("expires_at", "") or "").strip()
        if expires_at:
            datetime.fromisoformat(expires_at)
        return {
            "name": name,
            "symbol": symbol,
            "template_name": template_name,
            "setup_json": json.dumps(copy.deepcopy(setup), sort_keys=True),
            "enabled": int(bool(value.get("enabled", True))),
            "include_current": int(bool(value.get("include_current", True))),
            "adjusted": int(bool(value.get("adjusted", True))),
            "interval_minutes": interval,
            "trigger_mode": trigger_mode,
            "telegram_enabled": int(bool(value.get("telegram_enabled", True))),
            "expires_at": expires_at,
        }

    def create_alert(self, value: dict[str, Any]) -> int:
        clean = self._clean_definition(value)
        timestamp = now_iso()
        columns = ", ".join(clean)
        placeholders = ", ".join("?" for _ in clean)
        with self._lock, self._database() as database:
            cursor = database.execute(
                f"INSERT INTO alerts ({columns}, created_at, updated_at) "
                f"VALUES ({placeholders}, ?, ?)",
                (*clean.values(), timestamp, timestamp),
            )
            return int(cursor.lastrowid)

    def update_alert(self, alert_id: int, value: dict[str, Any]) -> None:
        clean = self._clean_definition(value)
        assignments = ", ".join(f"{key}=?" for key in clean)
        with self._lock, self._database() as database:
            cursor = database.execute(
                f"UPDATE alerts SET {assignments}, last_condition=NULL, "
                "last_error=CASE WHEN last_telegram_error<>'' "
                "THEN last_telegram_error ELSE '' END, updated_at=? WHERE id=?",
                (*clean.values(), now_iso(), int(alert_id)),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Alert {alert_id} no longer exists.")

    def delete_alert(self, alert_id: int) -> None:
        with self._lock, self._database() as database:
            database.execute("DELETE FROM alerts WHERE id=?", (int(alert_id),))

    def set_enabled(self, alert_id: int, enabled: bool) -> None:
        with self._lock, self._database() as database:
            database.execute(
                "UPDATE alerts SET enabled=?, updated_at=? WHERE id=?",
                (int(bool(enabled)), now_iso(), int(alert_id)),
            )

    def list_alerts(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE enabled=1" if enabled_only else ""
        with self._lock, self._database() as database:
            rows = database.execute(
                f"SELECT * FROM alerts {where} ORDER BY enabled DESC, symbol, name, id"
            ).fetchall()
        return [self._row(row) for row in rows if row is not None]  # type: ignore[list-item]

    def get_alert(self, alert_id: int) -> dict[str, Any] | None:
        with self._lock, self._database() as database:
            row = database.execute(
                "SELECT * FROM alerts WHERE id=?", (int(alert_id),)
            ).fetchone()
        return self._row(row)

    def record_evaluation(
        self,
        alert_id: int,
        matched: bool,
        *,
        triggered: bool = False,
        close_value: float | None = None,
        evidence: str = "",
        disable: bool = False,
    ) -> None:
        timestamp = now_iso()
        with self._lock, self._database() as database:
            database.execute(
                "UPDATE alerts SET last_condition=?, last_evaluated_at=?, "
                "last_triggered_at=CASE WHEN ? THEN ? ELSE last_triggered_at END, "
                "trigger_count=trigger_count+?, last_close=?, last_evidence=?, "
                "last_error=CASE WHEN last_telegram_error<>'' "
                "THEN last_telegram_error ELSE '' END, "
                "enabled=CASE WHEN ? THEN 0 ELSE enabled END, "
                "updated_at=? WHERE id=?",
                (
                    int(bool(matched)), timestamp, int(triggered), timestamp,
                    int(triggered), close_value, str(evidence), int(disable),
                    timestamp, int(alert_id),
                ),
            )
            if triggered:
                database.execute(
                    "INSERT INTO events(alert_id,event_type,occurred_at,matched,"
                    "close_value,evidence,message) VALUES(?,?,?,?,?,?,?)",
                    (
                        int(alert_id), "TRIGGERED", timestamp, 1, close_value,
                        str(evidence), "Alert condition matched",
                    ),
                )

    def record_telegram_delivery(
        self,
        alert_id: int,
        succeeded: bool,
        details: str,
    ) -> None:
        """Persist the delivery outcome separately from the rule trigger."""
        timestamp = now_iso()
        clean = str(details or "Telegram delivery failed").strip()[-4000:]
        status = clean if succeeded else "Failed"
        error = "" if succeeded else clean
        event_type = "TELEGRAM_SENT" if succeeded else "TELEGRAM_FAILED"
        with self._lock, self._database() as database:
            exists = database.execute(
                "SELECT 1 FROM alerts WHERE id=?", (int(alert_id),)
            ).fetchone()
            if exists is None:
                return
            database.execute(
                "UPDATE alerts SET last_telegram_at=?, last_telegram_status=?, "
                "last_telegram_error=?, last_error=?, updated_at=? WHERE id=?",
                (timestamp, status, error, error, timestamp, int(alert_id)),
            )
            database.execute(
                "INSERT INTO events(alert_id,event_type,occurred_at,message) "
                "VALUES(?,?,?,?)",
                (int(alert_id), event_type, timestamp, clean),
            )

    def record_error(self, alert_id: int, message: str) -> None:
        timestamp = now_iso()
        clean = str(message or "Evaluation failed").strip()[-4000:]
        with self._lock, self._database() as database:
            database.execute(
                "UPDATE alerts SET last_evaluated_at=?, last_error=?, updated_at=? "
                "WHERE id=?",
                (timestamp, clean, timestamp, int(alert_id)),
            )
            database.execute(
                "INSERT INTO events(alert_id,event_type,occurred_at,message) "
                "VALUES(?,?,?,?)",
                (int(alert_id), "ERROR", timestamp, clean),
            )

    def expire_due_alerts(self, current: datetime | None = None) -> int:
        current = current or datetime.now(IST)
        changed = 0
        for alert in self.list_alerts(enabled_only=True):
            expires = str(alert.get("expires_at", "") or "").strip()
            if not expires:
                continue
            try:
                value = datetime.fromisoformat(expires)
                if value.tzinfo is None:
                    value = value.replace(tzinfo=IST)
            except ValueError:
                continue
            if value <= current:
                self.set_enabled(int(alert["id"]), False)
                with self._lock, self._database() as database:
                    database.execute(
                        "INSERT INTO events(alert_id,event_type,occurred_at,message) "
                        "VALUES(?,?,?,?)",
                        (int(alert["id"]), "EXPIRED", now_iso(), "Alert expired"),
                    )
                changed += 1
        return changed

    def list_events(self, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock, self._database() as database:
            rows = database.execute(
                "SELECT events.*, alerts.name AS alert_name, alerts.symbol AS symbol "
                "FROM events JOIN alerts ON alerts.id=events.alert_id "
                "ORDER BY events.id DESC LIMIT ?",
                (max(1, min(5000, int(limit))),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_setting(self, key: str, default: Any) -> Any:
        with self._lock, self._database() as database:
            row = database.execute(
                "SELECT value FROM settings WHERE key=?", (str(key),)
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(str(row["value"]))
        except json.JSONDecodeError:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        payload = json.dumps(value)
        with self._lock, self._database() as database:
            database.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(key), payload),
            )

    def quick_check(self) -> str:
        with self._lock, self._database() as database:
            row = database.execute("PRAGMA quick_check").fetchone()
        return str(row[0]) if row else "no result"
