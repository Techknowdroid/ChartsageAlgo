import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import ChartSageStockAlerts as alerts
from PySide6.QtWidgets import QApplication


core = alerts.core


class FailingThenWorkingKeyring:
    def __init__(self) -> None:
        self.ready = False

    def get_password(self, _service: str, _account: str) -> str:
        if not self.ready:
            raise RuntimeError("No recommended backend was available")
        return "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ"


class RetryStore(core.TelegramSettingsStore):
    def _activate_windows_backend(self) -> bool:
        self.keyring.ready = True
        return True


class DestinationsOnlyStore:
    def load_destinations(self):
        return [
            {
                "key": "group_1",
                "label": "Group 1",
                "chat_id": "-1001234567890",
                "enabled": True,
            },
            {
                "key": "group_2",
                "label": "Group 2",
                "chat_id": "-1009876543210",
                "enabled": False,
            },
        ]

    def load_token(self):
        raise core.TelegramSettingsError("Credential Manager unavailable")


class TelegramKeyringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_keyring_operation_retries_after_backend_activation(self) -> None:
        fake = FailingThenWorkingKeyring()
        store = RetryStore(keyring_module=fake)
        self.assertEqual(
            store.load_token(), "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        )

    def test_dialog_loads_destinations_even_when_token_load_fails(self) -> None:
        dialog = core.TelegramSettingsDialog(DestinationsOnlyStore())
        self.assertEqual(
            dialog.destination_chat_id["group_1"].text(), "-1001234567890"
        )
        self.assertTrue(dialog.destination_enabled["group_1"].isChecked())
        self.assertIn("Credential Manager unavailable", dialog.status.text())
        dialog.close()

    def test_bundled_windows_vault_backend_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = core.TelegramSettingsStore(
                path=Path(directory) / "telegram_settings.json"
            )
            self.assertTrue(store._activate_windows_backend())
            backend = core.keyring.get_keyring()
            self.assertEqual(type(backend).__name__, "WinVaultKeyring")


if __name__ == "__main__":
    unittest.main()
