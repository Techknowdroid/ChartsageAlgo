import sqlite3
import tempfile
import unittest
from pathlib import Path

from stock_alert_engine import StockAlertStore


OLD_ALERTS_SQL = """
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE alerts (
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
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE events (
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
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO metadata(key, value) VALUES('schema_version', '1');
"""


def alert_value() -> dict:
    return {
        "name": "INFY test",
        "symbol": "INFY",
        "template_name": "Custom alert rule",
        "setup": {"expression": {}},
        "enabled": True,
        "include_current": True,
        "adjusted": True,
        "interval_minutes": 5,
        "trigger_mode": "TRANSITION",
        "telegram_enabled": True,
        "expires_at": "",
    }


class StockAlertTelegramAuditTests(unittest.TestCase):
    def test_version_one_database_migrates_without_losing_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alerts.db"
            database = sqlite3.connect(path)
            try:
                database.executescript(OLD_ALERTS_SQL)
                database.execute(
                    "INSERT INTO alerts(name,symbol,template_name,setup_json,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    ("Existing", "INFY", "Custom", "{}", "before", "before"),
                )
                database.commit()
            finally:
                database.close()
            store = StockAlertStore(path)
            saved = store.list_alerts()
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]["symbol"], "INFY")
            self.assertEqual(saved[0]["last_telegram_at"], "")
            self.assertEqual(saved[0]["last_telegram_status"], "")
            self.assertEqual(saved[0]["last_telegram_error"], "")

    def test_delivery_failure_persists_until_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StockAlertStore(Path(directory) / "alerts.db")
            alert_id = store.create_alert(alert_value())
            store.record_evaluation(alert_id, True, triggered=True)
            store.record_telegram_delivery(alert_id, False, "Forbidden: bot blocked")
            failed = store.get_alert(alert_id)
            self.assertEqual(failed["last_telegram_status"], "Failed")
            self.assertEqual(failed["last_telegram_error"], "Forbidden: bot blocked")
            self.assertEqual(failed["last_error"], "Forbidden: bot blocked")

            store.record_evaluation(alert_id, False)
            still_failed = store.get_alert(alert_id)
            self.assertEqual(still_failed["last_error"], "Forbidden: bot blocked")

            store.record_telegram_delivery(
                alert_id, True, "Telegram sent to 2 destination(s)."
            )
            sent = store.get_alert(alert_id)
            self.assertEqual(
                sent["last_telegram_status"],
                "Telegram sent to 2 destination(s).",
            )
            self.assertEqual(sent["last_telegram_error"], "")
            self.assertEqual(sent["last_error"], "")
            self.assertTrue(sent["last_telegram_at"])
            self.assertEqual(
                [event["event_type"] for event in store.list_events()],
                ["TELEGRAM_SENT", "TELEGRAM_FAILED", "TRIGGERED"],
            )


if __name__ == "__main__":
    unittest.main()
