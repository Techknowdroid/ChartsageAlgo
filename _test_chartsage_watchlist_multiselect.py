import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "_packaging_runtime"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import ChartSageAlgoOPENcode as module


class MemoryWatchlistStore:
    def __init__(self, data):
        self.data = dict(data)
        self.saved = None

    def load(self):
        return dict(self.data)

    def save(self, updated):
        self.saved = dict(updated)
        self.data = dict(updated)


class ChartSageWatchlistMultiSelectTests(unittest.TestCase):
    def setUp(self):
        self.app = module.QApplication.instance() or module.QApplication([])

    def _dialog(self, store):
        patcher = patch.object(
            module.WatchlistManagerDialog,
            "_load_cached_nse_symbols_or_download",
            return_value=None,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        dialog = module.WatchlistManagerDialog(store)
        self.addCleanup(dialog.close)
        return dialog

    def test_multiple_custom_watchlists_are_deleted_in_one_save(self):
        store = MemoryWatchlistStore({
            "Published One": ["AAA"],
            "Published Two": ["BBB"],
            "Keep This": ["CCC"],
        })
        dialog = self._dialog(store)
        self.assertEqual(
            dialog.watchlist_list.selectionMode(),
            module.QAbstractItemView.SelectionMode.ExtendedSelection,
        )

        selected_names = {"Published One", "Published Two"}
        for row in range(dialog.watchlist_list.count()):
            item = dialog.watchlist_list.item(row)
            if item.data(module.Qt.ItemDataRole.UserRole) in selected_names:
                item.setSelected(True)
        self.app.processEvents()
        self.assertEqual(len(dialog.watchlist_list.selectedItems()), 2)
        self.assertIn("Delete 2 Selected", dialog.del_btn.text())

        with patch.object(
            module.QMessageBox,
            "question",
            return_value=module.QMessageBox.StandardButton.Yes,
        ) as question:
            dialog._delete_watchlist()

        self.assertEqual(store.saved, {"Keep This": ["CCC"]})
        self.assertEqual(dialog.watchlists, {"Keep This": ["CCC"]})
        self.assertEqual(dialog.watchlist_list.count(), 1)
        self.assertIn("2 selected", question.call_args.args[2])

    def test_bulk_delete_is_disabled_when_zerodha_account_is_selected(self):
        zerodha_name = f"{module.HOLDINGS_UNIVERSE_PREFIX} AB1234"
        store = MemoryWatchlistStore({
            "Ordinary List": ["AAA"],
            zerodha_name: ["BBB"],
        })
        dialog = self._dialog(store)
        for row in range(dialog.watchlist_list.count()):
            dialog.watchlist_list.item(row).setSelected(True)
        self.app.processEvents()

        self.assertFalse(dialog.del_btn.isEnabled())
        with patch.object(module.QMessageBox, "information") as information:
            dialog._delete_watchlist()
        information.assert_called_once()
        self.assertIsNone(store.saved)
        self.assertEqual(set(dialog.watchlists), {"Ordinary List", zerodha_name})


if __name__ == "__main__":
    unittest.main()
