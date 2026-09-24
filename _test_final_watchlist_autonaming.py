import os
import re
import sys
import unittest
from datetime import datetime
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
