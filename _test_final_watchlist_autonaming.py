import os
import re
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "_packaging_runtime"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import FINALChartSageV1 as module


class FinalWatchlistAutonamingTests(unittest.TestCase):
    def setUp(self):
        self.templates = {
            "NIFTY500 RSI Crosses above 30 in Uptrending Stocks": {
                "universes": ["NIFTY 500"],
                "custom_symbols": "",
            },
            "MACD Line Crossed above Signal While Both were above 0": {
                "universes": ["NIFTY 500"],
                "custom_symbols": "",
            },
        }
        self.config = module.finalchartink_defaults()
        self.config.update({
            "recipe_name": "NIFTY500 Momentum Test",
            "feeder_templates": list(self.templates),
            "feeder_scope": "COMBINE",
            "feeder_sources": ["NIFTY AUTO"],
            "final_additional_sources": ["NIFTY PHARMA"],
            "include_multirule": True,
            "buy_finalset": "NIFTY500 RSI Crosses above 30 in Uptrending Stocks",
            "exit_finalset": "MACD Line Crossed above Signal While Both were above 0",
        })

    def test_names_are_deterministic_readable_valid_and_bounded(self):
        first = module.build_finalchartink_auto_watchlist_names(
            self.config, self.templates
        )
        reordered = dict(self.config)
        reordered["feeder_templates"] = list(reversed(self.config["feeder_templates"]))
        second = module.build_finalchartink_auto_watchlist_names(
            reordered, self.templates
        )
        self.assertEqual(first, second)
        self.assertRegex(first["watchlist_flow_id"], r"^[A-F0-9]{6}$")
        for key, suffix in (
            ("multirule_watchlist", "_01-S1-MATCHES"),
            ("final_input_watchlist", "_02-FINAL-INPUT"),
            ("buy_results_watchlist", "_03-BUY"),
            ("exit_results_watchlist", "_04-EXIT"),
        ):
            name = first[key]
            self.assertTrue(name.endswith(suffix), name)
            self.assertLessEqual(len(name), module.AUTO_WATCHLIST_NAME_MAX)
            self.assertRegex(name, r"^[A-Za-z0-9 _-]+$")
            self.assertIn("S1-", name)
            self.assertIn("S2A-COMBINE-", name)
            self.assertIn("S2B-S1-PLUS-DIRECT-", name)
            self.assertIn("S3B-", name)
            self.assertIn("S3E-", name)

    def test_changed_flow_gets_changed_identity(self):
        first = module.build_finalchartink_auto_watchlist_names(
            self.config, self.templates
        )
        changed = dict(self.config)
        changed["buy_finalset"] = "Different final BUY rule"
        second = module.build_finalchartink_auto_watchlist_names(
            changed, self.templates
        )
        self.assertNotEqual(first["watchlist_flow_id"], second["watchlist_flow_id"])
        self.assertNotEqual(
            first["buy_results_watchlist"], second["buy_results_watchlist"]
        )

    def test_very_long_choices_keep_every_step_marker_within_limit(self):
        config = dict(self.config)
        config.update({
            "recipe_name": "A very long strategy name " * 8,
            "feeder_templates": [f"Long feeder rule number {n} " * 5 for n in range(8)],
            "feeder_sources": [f"Long Step 2A source {n} " * 5 for n in range(8)],
            "final_additional_sources": [
                f"Long Step 2B source {n} " * 5 for n in range(8)
            ],
            "buy_finalset": "A very long final BUY rule " * 8,
            "exit_finalset": "A very long final EXIT rule " * 8,
        })
        names = module.build_finalchartink_auto_watchlist_names(config, {})
        for key in (
            "multirule_watchlist", "final_input_watchlist",
            "buy_results_watchlist", "exit_results_watchlist",
        ):
            name = names[key]
            self.assertLessEqual(len(name), module.AUTO_WATCHLIST_NAME_MAX)
            for marker in ("S1-", "S2A-", "S2B-", "S3B-", "S3E-"):
                self.assertIn(marker, name)

    def test_direct_only_flow_is_explicit(self):
        config = module.finalchartink_defaults()
        config.update({
            "recipe_name": "Direct Pharma",
            "feeder_templates": [],
            "include_multirule": False,
            "final_additional_sources": ["NIFTY PHARMA"],
            "buy_finalset": "RSI above 50",
        })
        names = module.build_finalchartink_auto_watchlist_names(config, {})
        name = names["final_input_watchlist"]
        self.assertIn("S1-NONE", name)
        self.assertIn("S2A-INACTIVE-NONE", name)
        self.assertIn("S2B-DIRECT-ONLY-NIFTYPHARMA", name)

    def test_legacy_manual_names_and_auto_snapshot_names(self):
        legacy = module.finalchartink_defaults()
        legacy.update({
            "auto_watchlist_naming": False,
            "recipe_name": "Legacy Strategy",
            "multirule_watchlist": "Legacy Multi",
            "final_input_watchlist": "Legacy Final",
        })
        self.assertEqual(
            module.finalchartink_recipe_watchlist_names(legacy),
            [
                "Legacy Multi",
                "Legacy Final",
                "Legacy Strategy BUY RESULTS",
                "Legacy Strategy EXIT RESULTS",
            ],
        )

        auto = dict(self.config)
        auto.update(module.build_finalchartink_auto_watchlist_names(auto, self.templates))
        snapshots, records = module.build_finalchartink_timestamped_watchlists(
            auto,
            ["AAA"],
            ["AAA", "BBB"],
            ["AAA"],
            [],
            generated_at=datetime(2026, 9, 24, 10, 11, 12),
        )
        self.assertEqual(len(snapshots), 4)
        self.assertEqual(len(records), 4)
        self.assertTrue(all(name.endswith("20260924_101112") for name in snapshots))

    def test_old_strategy_keeps_its_manual_names_as_off_mode_fallback(self):
        migrated = module.normalize_finalchartink_recipe({
            "recipe_name": "Old Saved Strategy",
            "multirule_watchlist": "My Old Multi",
            "final_input_watchlist": "My Old Final",
        })
        self.assertFalse(migrated["auto_watchlist_naming"])
        self.assertEqual(
            migrated["manual_multirule_watchlist"], "My Old Multi"
        )
        self.assertEqual(
            migrated["manual_final_input_watchlist"], "My Old Final"
        )

    def test_new_auto_names_publish_with_chartsage_prefix(self):
        names = module.build_finalchartink_auto_watchlist_names(
            self.config, self.templates
        )
        published = module.finalchartink_chartsage_watchlist_name(
            names["buy_results_watchlist"]
        )
        self.assertTrue(published.startswith("FINALCHARTSAGE CS_"), published)

    def test_gui_mode_is_selectable_and_saved_in_config(self):
        app = module.QApplication.instance() or module.QApplication([])
        window = module.FinalChartInkWindow()
        try:
            window._apply_config(module.finalchartink_defaults())
            self.assertTrue(window.auto_watchlist_naming_cb.isChecked())
            self.assertTrue(window.multirule_name_edit.isReadOnly())
            self.assertTrue(window.final_source_name_edit.isReadOnly())
            self.assertTrue(
                window.buy_results_name_edit.text().endswith("_03-BUY")
            )
            window.auto_watchlist_naming_cb.setChecked(False)
            self.assertFalse(window.multirule_name_edit.isReadOnly())
            self.assertFalse(window.final_source_name_edit.isReadOnly())
            self.assertEqual(
                window.buy_results_name_edit.text(),
                "MY MULTIRULE GUIDE BUY RESULTS",
            )
            manual_config = window._config_from_ui()
            self.assertFalse(manual_config["auto_watchlist_naming"])
            window.auto_watchlist_naming_cb.setChecked(True)
            auto_config = window._config_from_ui()
            self.assertTrue(auto_config["auto_watchlist_naming"])
            self.assertTrue(
                auto_config["final_input_watchlist"].endswith("_02-FINAL-INPUT")
            )
        finally:
            window.close()
            app.processEvents()

    def test_flow_popup_refreshes_step3_rules_in_manual_naming_mode(self):
        app = module.QApplication.instance() or module.QApplication([])
        window = module.FinalChartInkWindow()
        try:
            window._apply_config(module.finalchartink_defaults())
            window.auto_watchlist_naming_cb.setChecked(False)
            window.buy_set_combo.clear()
            window.exit_set_combo.clear()
            window.buy_set_combo.addCheckItem("[RULE] BUY One", "BUY One")
            window.buy_set_combo.addCheckItem("[RULE] BUY Two", "BUY Two")
            window.exit_set_combo.addCheckItem("[RULE] EXIT One", "EXIT One")
            window.exit_set_combo.addCheckItem("[RULE] EXIT Two", "EXIT Two")
            window.buy_set_combo.setCheckedData(["BUY One", "BUY Two"])
            window.exit_set_combo.setCheckedData(["EXIT One", "EXIT Two"])
            selected_sources = (
                (window.source_universe_list, "NIFTY 500"),
                (window.source_watchlist_list, "Momentum Watchlist"),
                (window.final_universe_list, "NIFTY PHARMA"),
                (window.final_watchlist_list, "Published BUY Watchlist"),
            )
            for widget, name in selected_sources:
                item = module.QListWidgetItem(name)
                item.setData(module.Qt.ItemDataRole.UserRole, name)
                item.setCheckState(module.Qt.CheckState.Checked)
                widget.addItem(item)
            window._update_auto_watchlist_names()

            popup = window.multirule_name_edit.toolTip()
            self.assertTrue(popup.startswith("<qt>"), popup[:80])
            self.assertIn("width='560'", popup)
            self.assertIn(
                "Step 2A mode: Inactive because no Step 1 rule template is selected",
                popup,
            )
            self.assertNotIn("Step 2A mode: SAVED", popup)
            self.assertIn(
                "Step 2A universes/watchlists below: Ignored in this mode; checkmarked choices are not used",
                popup,
            )
            self.assertNotIn("Step 2A selected universes: NIFTY 500", popup)
            self.assertNotIn(
                "Step 2A selected watchlists: Momentum Watchlist", popup
            )
            self.assertIn(
                "Step 2A selection usage: Ignored; no Step 1 rule template is selected",
                popup,
            )
            self.assertIn("Step 2B direct universes: NIFTY PHARMA", popup)
            self.assertIn(
                "Step 2B direct watchlists: Published BUY Watchlist", popup
            )
            self.assertIn("Step 3 BUY (ANY): BUY One, BUY Two", popup)
            self.assertIn("Step 3 EXIT (ANY): EXIT One, EXIT Two", popup)
            self.assertIn("PRIVATE WATCHLISTS CREATED/UPDATED", popup)
            self.assertIn(
                "1. Step 1 matches: MultiruleWatchlist", popup
            )
            self.assertIn(
                "2. Final input: FINALCHARTSAGE INPUT", popup
            )
            self.assertIn(
                "3. BUY results: MY MULTIRULE GUIDE BUY RESULTS", popup
            )
            self.assertIn(
                "4. EXIT results: MY MULTIRULE GUIDE EXIT RESULTS", popup
            )
            self.assertIn(
                "TIMESTAMPED SNAPSHOTS — manual latest-data runs", popup
            )
            self.assertIn("&lt;YYYYMMDD_", popup)
            self.assertIn("HHMMSS&gt;", popup)
            self.assertEqual(
                popup,
                window.final_source_name_edit.toolTip(),
            )
            self.assertEqual(
                popup,
                window.buy_results_name_edit.toolTip(),
            )
            self.assertEqual(
                popup,
                window.exit_results_name_edit.toolTip(),
            )

            window.publish_to_chartsage_cb.setChecked(True)
            published_popup = window.multirule_name_edit.toolTip()
            self.assertIn(
                "PUBLISHED CHARTSAGE COPIES CREATED/UPDATED", published_popup
            )
            self.assertIn(
                module.finalchartink_chartsage_watchlist_name(
                    window.multirule_name_edit.text()
                ),
                published_popup,
            )
            window.historical_radio.setChecked(True)
            historical_popup = window.multirule_name_edit.toolTip()
            self.assertIn(
                "CONFIGURED OUTPUT WATCHLISTS — historical mode does not write them",
                historical_popup,
            )
            self.assertNotIn(
                "PUBLISHED CHARTSAGE COPIES CREATED/UPDATED", historical_popup
            )
            self.assertNotIn("TIMESTAMPED SNAPSHOTS", historical_popup)
            window.latest_radio.setChecked(True)

            combine_index = window.feeder_scope_combo.findData("COMBINE")
            window.feeder_scope_combo.setCurrentIndex(combine_index)
            window._update_auto_watchlist_names()
            active_popup = window.multirule_name_edit.toolTip()
            self.assertIn("Step 2A selected universes: NIFTY 500", active_popup)
            self.assertIn(
                "Step 2A selected watchlists: Momentum Watchlist", active_popup
            )
            self.assertNotIn(
                "checkmarked choices are not used", active_popup
            )
            self.assertNotIn("Step 2B selection status: NOTHING SELECTED", active_popup)

            window.final_universe_list.item(
                window.final_universe_list.count() - 1
            ).setCheckState(module.Qt.CheckState.Unchecked)
            window.final_watchlist_list.item(
                window.final_watchlist_list.count() - 1
            ).setCheckState(module.Qt.CheckState.Unchecked)
            window._update_auto_watchlist_names()
            empty_step2b_popup = window.multirule_name_edit.toolTip()
            self.assertIn(
                "Step 2B selection status: NOTHING SELECTED; no direct universe or watchlist is added",
                empty_step2b_popup,
            )
        finally:
            window.close()
            app.processEvents()

    def test_step2_sources_are_visually_split_and_generated_lists_are_newest_first(self):
        catalog = {
            "NIFTY PHARMA": ("UNIVERSE", object()),
            "NIFTY 500": ("UNIVERSE", object()),
            "Plain Personal List": ("CHARTSAGE WATCHLIST", ["AAA"]),
            "Older Generated": ("FINALCHARTINK WATCHLIST", ["BBB"]),
            "Latest Step 3 BUY": ("FINALCHARTINK WATCHLIST", ["CCC"]),
            "Latest Step 1 Matches": ("FINALCHARTINK WATCHLIST", ["DDD"]),
        }

        def key(kind, name):
            return module._watchlist_metadata_key(kind, name)

        metadata = {
            key("FINALCHARTINK WATCHLIST", "Older Generated"): {
                "last_written_at": "2026-09-25T12:00:00+05:30",
                "last_write_order": "0",
            },
            key("FINALCHARTINK WATCHLIST", "Latest Step 1 Matches"): {
                "last_written_at": "2026-09-26T12:00:00+05:30",
                "last_write_order": "0",
            },
            key("FINALCHARTINK WATCHLIST", "Latest Step 3 BUY"): {
                "last_written_at": "2026-09-26T12:00:00+05:30",
                "last_write_order": "2",
            },
        }
        app = module.QApplication.instance() or module.QApplication([])
        with (
            patch.object(module, "finalchartink_source_catalog", return_value=catalog),
            patch.object(
                module,
                "sync_finalchartink_watchlist_metadata",
                return_value=metadata,
            ),
        ):
            window = module.FinalChartInkWindow()
        try:
            self.assertEqual(
                window.feeder_category_splitter.orientation(),
                module.Qt.Orientation.Vertical,
            )
            self.assertEqual(
                window.final_category_splitter.orientation(),
                module.Qt.Orientation.Vertical,
            )
            expected_watchlists = [
                "Latest Step 1 Matches",
                "Latest Step 3 BUY",
                "Older Generated",
                "Plain Personal List",
            ]
            expected_universes = ["NIFTY 500", "NIFTY PHARMA"]
            for widget in (
                window.source_watchlist_list,
                window.final_watchlist_list,
            ):
                self.assertEqual(
                    [
                        widget.item(index).data(module.Qt.ItemDataRole.UserRole)
                        for index in range(widget.count())
                    ],
                    expected_watchlists,
                )
            for widget in (
                window.source_universe_list,
                window.final_universe_list,
            ):
                self.assertEqual(
                    [
                        widget.item(index).data(module.Qt.ItemDataRole.UserRole)
                        for index in range(widget.count())
                    ],
                    expected_universes,
                )

            module.FinalChartInkWindow._set_checked_lists(
                window.source_lists,
                ["Latest Step 1 Matches", "NIFTY PHARMA"],
            )
            module.FinalChartInkWindow._set_checked_lists(
                window.final_source_lists,
                ["Latest Step 3 BUY", "NIFTY 500"],
            )
            self.assertEqual(
                window._checked_lists(window.source_lists),
                ["Latest Step 1 Matches", "NIFTY PHARMA"],
            )
            self.assertEqual(
                window._checked_lists(window.final_source_lists),
                ["Latest Step 3 BUY", "NIFTY 500"],
            )

            window.source_search.setText("NIFTY 500")
            self.assertTrue(all(
                window.source_watchlist_list.item(index).isHidden()
                for index in range(window.source_watchlist_list.count())
            ))
            self.assertFalse(window.source_universe_list.item(0).isHidden())
            self.assertTrue(window.source_universe_list.item(1).isHidden())
        finally:
            window.close()
            app.processEvents()

    def test_latest_run_private_and_published_watchlists_use_distinct_highlights(self):
        catalog = {
            "Generated Private": ("FINALCHARTINK WATCHLIST", ["AAA"]),
            "FINALCHARTINK Generated Private": (
                "CHARTSAGE WATCHLIST", ["AAA"]
            ),
            "Older List": ("FINALCHARTINK WATCHLIST", ["BBB"]),
            "NIFTY 500": ("UNIVERSE", object()),
        }
        app = module.QApplication.instance() or module.QApplication([])
        with (
            patch.object(module, "finalchartink_source_catalog", return_value=catalog),
            patch.object(
                module,
                "sync_finalchartink_watchlist_metadata",
                return_value={},
            ),
        ):
            window = module.FinalChartInkWindow()
            try:
                window._latest_run_private_watchlists = {"Generated Private"}
                window._latest_run_published_watchlists = {
                    "FINALCHARTINK Generated Private"
                }
                window._refresh_source_catalog()

                def item_named(widget, name):
                    for index in range(widget.count()):
                        item = widget.item(index)
                        if item.data(module.Qt.ItemDataRole.UserRole) == name:
                            return item
                    self.fail(f"Missing source item {name}")

                for widget in (
                    window.source_watchlist_list,
                    window.final_watchlist_list,
                ):
                    private_item = item_named(widget, "Generated Private")
                    published_item = item_named(
                        widget, "FINALCHARTINK Generated Private"
                    )
                    older_item = item_named(widget, "Older List")
                    self.assertEqual(
                        private_item.background().color().name().upper(),
                        module.LATEST_RUN_PRIVATE_WATCHLIST_COLOR,
                    )
                    self.assertEqual(
                        published_item.background().color().name().upper(),
                        module.LATEST_RUN_PUBLISHED_WATCHLIST_COLOR,
                    )
                    self.assertIn("Generated or refreshed privately", private_item.toolTip())
                    self.assertIn("Published to ChartSage", published_item.toolTip())
                    self.assertEqual(
                        older_item.background().style(),
                        module.Qt.BrushStyle.NoBrush,
                    )

                priority = module.finalchartink_watchlist_highlight(
                    "Same Name",
                    "FINALCHARTINK WATCHLIST",
                    {"Same Name"},
                    {"Same Name"},
                )
                self.assertEqual(
                    priority[0], module.LATEST_RUN_PUBLISHED_WATCHLIST_COLOR
                )

                window._clear_latest_watchlist_highlights()
                self.assertFalse(window._latest_run_private_watchlists)
                self.assertFalse(window._latest_run_published_watchlists)
                for widget in (
                    window.source_watchlist_list,
                    window.final_watchlist_list,
                ):
                    self.assertEqual(
                        item_named(widget, "Generated Private").background().style(),
                        module.Qt.BrushStyle.NoBrush,
                    )
                    self.assertEqual(
                        item_named(
                            widget, "FINALCHARTINK Generated Private"
                        ).background().style(),
                        module.Qt.BrushStyle.NoBrush,
                    )
            finally:
                window.close()
                app.processEvents()

    def test_cycle_highlight_names_include_base_snapshots_and_published_copies(self):
        config = module.normalize_finalchartink_recipe({
            "multirule_watchlist": "S1 Result",
            "final_input_watchlist": "Final Input",
            "buy_results_watchlist": "Buy Result",
            "exit_results_watchlist": "Exit Result",
        })
        private_names, published_names = (
            module.finalchartink_cycle_watchlist_highlight_names({
                "config": config,
                "historical_mode": False,
                "timestamped_watchlists": [{"name": "Buy Result 20260928_120000"}],
                "chartsage_published_watchlists": [
                    {"published_name": "FINALCHARTSAGE Buy Result"}
                ],
            })
        )
        self.assertEqual(
            private_names,
            {
                "S1 Result",
                "Final Input",
                "Buy Result",
                "Exit Result",
                "Buy Result 20260928_120000",
            },
        )
        self.assertEqual(published_names, {"FINALCHARTSAGE Buy Result"})
        self.assertEqual(
            module.finalchartink_cycle_watchlist_highlight_names({
                "config": config,
                "historical_mode": True,
            }),
            (set(), set()),
        )

    def test_private_watchlist_manager_has_two_draggable_resizable_panes(self):
        class MemoryStore:
            def load(self):
                return {
                    "A long generated watchlist name": ["AAA", "BBB"],
                    "Another watchlist": ["CCC"],
                }

        app = module.QApplication.instance() or module.QApplication([])
        dialog = module.FinalChartInkPrivateWatchlistDialog(MemoryStore())
        try:
            dialog.resize(1000, 600)
            dialog.show()
            app.processEvents()
            splitter = dialog.watchlist_splitter
            self.assertEqual(splitter.count(), 2)
            self.assertFalse(splitter.childrenCollapsible())
            self.assertGreaterEqual(splitter.handleWidth(), 8)

            splitter.setSizes([700, 250])
            app.processEvents()
            left_wide = splitter.sizes()
            splitter.setSizes([250, 700])
            app.processEvents()
            right_wide = splitter.sizes()
            self.assertGreater(left_wide[0], right_wide[0])
            self.assertLess(left_wide[1], right_wide[1])
            self.assertGreater(splitter.handle(1).toolTip().strip(), "")
        finally:
            dialog.close()
            app.processEvents()

    def test_private_watchlist_manager_deletes_multiple_selected_lists(self):
        class MemoryStore:
            def __init__(self):
                self.data = {
                    "First List": ["AAA"],
                    "Second List": ["BBB"],
                    "Third List": ["CCC"],
                }
                self.saved = None

            def load(self):
                return dict(self.data)

            def save(self, updated):
                self.saved = dict(updated)
                self.data = dict(updated)

        app = module.QApplication.instance() or module.QApplication([])
        store = MemoryStore()
        dialog = module.FinalChartInkPrivateWatchlistDialog(store)
        try:
            self.assertEqual(
                dialog.watchlist_list.selectionMode(),
                module.QAbstractItemView.SelectionMode.ExtendedSelection,
            )
            dialog.watchlist_list.clearSelection()
            dialog.watchlist_list.item(0).setSelected(True)
            dialog.watchlist_list.item(1).setSelected(True)
            app.processEvents()
            self.assertEqual(len(dialog.watchlist_list.selectedItems()), 2)
            self.assertIn("Delete 2 Selected", dialog.delete_list_btn.text())

            with patch.object(
                module.QMessageBox,
                "question",
                return_value=module.QMessageBox.StandardButton.Yes,
            ) as question:
                dialog._delete_watchlist()

            self.assertEqual(store.saved, {"Third List": ["CCC"]})
            self.assertEqual(dialog.watchlists, {"Third List": ["CCC"]})
            self.assertEqual(dialog.watchlist_list.count(), 1)
            self.assertIn("2 selected", question.call_args.args[2])
        finally:
            dialog.close()
            app.processEvents()


if __name__ == "__main__":
    unittest.main()
