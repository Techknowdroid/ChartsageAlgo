import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "_packaging_runtime"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import ChartSageAlgoOPENcode as module


class _FakeScanner(module.QWidget):
    def __init__(self):
        super().__init__()
        self.universe_list = module.QListWidget()
        self.custom_symbols = module.QLineEdit()
        self.manager_calls = 0
        self.sync_calls = 0

    def add_source(self, name, display, checked=False):
        item = module.QListWidgetItem(display)
        item.setData(module.Qt.ItemDataRole.UserRole, name)
        item.setFlags(item.flags() | module.Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(
            module.Qt.CheckState.Checked
            if checked else module.Qt.CheckState.Unchecked
        )
        self.universe_list.addItem(item)

    def _open_watchlist_manager(self):
        self.manager_calls += 1

    def _sync_universe_list(self):
        self.sync_calls += 1


class ExpandedUniverseSelectorTests(unittest.TestCase):
    def setUp(self):
        self.app = module.QApplication.instance() or module.QApplication([])
        self.main = _FakeScanner()
        self.main.add_source("NSE ALL", "NSE ALL", checked=True)
        self.main.add_source("NIFTY 500", "NIFTY 500 (500)")
        self.main.custom_symbols.setText("TCS")
        self.dialog = module.ExpandedUniverseSelectorDialog(self.main)

    def tearDown(self):
        self.dialog.close()
        self.main.close()
        self.app.processEvents()

    def test_selector_is_large_searchable_and_live_two_way(self):
        self.assertGreaterEqual(self.dialog.minimumWidth(), 650)
        self.assertGreaterEqual(self.dialog.minimumHeight(), 450)
        self.assertEqual(self.dialog.universe_list.count(), 2)
        self.assertEqual(
            self.dialog.universe_list.item(0).checkState(),
            module.Qt.CheckState.Checked,
        )
        self.assertEqual(self.dialog.custom_symbols.text(), "TCS")

        self.dialog.universe_list.item(1).setCheckState(
            module.Qt.CheckState.Checked
        )
        self.assertEqual(
            self.main.universe_list.item(1).checkState(),
            module.Qt.CheckState.Checked,
        )

        self.main.universe_list.item(0).setCheckState(
            module.Qt.CheckState.Unchecked
        )
        self.assertEqual(
            self.dialog.universe_list.item(0).checkState(),
            module.Qt.CheckState.Unchecked,
        )

        self.dialog.custom_symbols.setText("INFY RELIANCE")
        self.assertEqual(self.main.custom_symbols.text(), "INFY RELIANCE")
        self.main.custom_symbols.setText("SBIN")
        self.assertEqual(self.dialog.custom_symbols.text(), "SBIN")

        self.dialog.search_edit.setText("NIFTY 500")
        self.assertTrue(self.dialog.universe_list.item(0).isHidden())
        self.assertFalse(self.dialog.universe_list.item(1).isHidden())
        self.dialog._set_visible(False)
        self.assertEqual(
            self.main.universe_list.item(1).checkState(),
            module.Qt.CheckState.Unchecked,
        )

    def test_full_resync_and_watchlist_manager_bridge(self):
        self.main.add_source("NIFTY AUTO", "NIFTY AUTO (15)")
        self.dialog.sync_from_main()
        self.assertEqual(self.dialog.universe_list.count(), 3)
        self.assertEqual(
            self.dialog.universe_list.item(2).data(
                module.Qt.ItemDataRole.UserRole
            ),
            "NIFTY AUTO",
        )
        self.dialog._manage_watchlists()
        self.assertEqual(self.main.manager_calls, 1)
        self.assertEqual(self.main.sync_calls, 1)


if __name__ == "__main__":
    unittest.main()
