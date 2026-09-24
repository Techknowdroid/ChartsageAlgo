import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "_packaging_runtime"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd
import FINALChartSageV1 as module
from PySide6.QtTest import QTest


class _Signal:
    def connect(self, callback):
        self.callback = callback


class _FakePipeline:
    calls = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.progress = _Signal()
        self.__class__.calls.append(kwargs)

    def _run_pipeline(self):
        name = (
            self.kwargs.get("finalset_name")
            or self.kwargs.get("exit_finalset_name")
            or next(iter(self.kwargs.get("member_names") or []), "")
            or next(iter(self.kwargs.get("exit_names") or []), "")
        )
        is_exit = bool(
            self.kwargs.get("exit_names")
            or self.kwargs.get("exit_finalset_name")
        )
        symbols = {
            "BUY A": ["AAA"],
            "BUY B": ["AAA", "BBB"],
            "BUY SET": ["CCC"],
            "EXIT X": ["BBB", "DDD"],
        }.get(name, [])
        signal = "EXIT / WEAKNESS" if is_exit else "BUY"
        return {
            "guidance_rows": [
                {
                    "Symbol": symbol,
                    "Signal": signal,
                    "Matched Members": name,
                    "Confirmation Count": 1,
                }
                for symbol in symbols
            ]
        }


class FinalStep3MultiSelectTests(unittest.TestCase):
    def setUp(self):
        app = module.QApplication.instance() or module.QApplication([])
        self.app = app
        rule = module.default_setup()
        self.templates = {
            "BUY A": dict(rule),
            "BUY B": dict(rule),
            "EXIT X": dict(rule),
            "BUY SET": {
                **module.default_setup(),
                "is_final_template": True,
                "finalset_side": "BUY",
                "finalset_op": "AND",
                "finalset_members": ["BUY A", "BUY B"],
            },
        }

    def test_loss_percent_parser_and_examples_use_positive_loss_wording(self):
        rule = module.RuleParser.parse("Loss % >= 5%")
        self.assertEqual(rule["left"]["name"], "Loss %")
        self.assertEqual(rule["operator"], ">=")
        self.assertEqual(rule["right"]["value"], 5.0)
        self.assertTrue(module.expression_has_pnl_rules(rule))
        examples = module.filter_examples_text()
        self.assertIn("Loss % >= 5%", examples)
        self.assertIn("Profit % <= -5%", examples)

    def test_position_rule_can_save_for_paper_without_zerodha_but_not_live_algo(self):
        loss_rule = module.RuleParser.parse("Loss % >= 5%")
        setup = module.default_setup()
        setup["universes"] = ["NIFTY 500"]
        setup["expression"] = module.group_node("AND", [loss_rule])
        dummy_window = SimpleNamespace(active_template_name=None)
        self.assertTrue(module.ScannerWindow._validate_template_setup(
            dummy_window, setup, silent=True
        ))
        self.assertEqual(setup["position_rule_scope"], "PAPER_BACKTEST_ONLY")
        self.assertEqual(setup["strategy_guide_role"], "EXIT")

        live_setup = dict(setup)
        live_setup["is_algo_template"] = True
        self.assertFalse(module.ScannerWindow._validate_template_setup(
            dummy_window, live_setup, silent=True
        ))

    def test_old_singular_strategy_migrates_to_one_checked_item(self):
        config = module.normalize_finalchartink_recipe({
            "recipe_name": "Old",
            "buy_finalset": "BUY A",
            "exit_finalset": "EXIT X",
        })
        self.assertEqual(config["buy_finalsets"], ["BUY A"])
        self.assertEqual(config["exit_finalsets"], ["EXIT X"])
        self.assertEqual(config["buy_finalset"], "BUY A")
        self.assertEqual(config["exit_finalset"], "EXIT X")

    def test_checkable_combos_round_trip_multiple_saved_items(self):
        window = module.FinalChartInkWindow()
        try:
            window.templates = self.templates
            window.buy_set_combo.clear()
            window.exit_set_combo.clear()
            for name in ("BUY A", "BUY B", "BUY SET"):
                window.buy_set_combo.addCheckItem(name, name)
            window.exit_set_combo.addCheckItem("EXIT X", "EXIT X")
            config = module.finalchartink_defaults()
            config.update({
                "buy_finalsets": ["BUY A", "BUY SET"],
                "exit_finalsets": ["EXIT X"],
            })
            window._apply_config(config)
            saved = window._config_from_ui()
            self.assertEqual(saved["buy_finalsets"], ["BUY A", "BUY SET"])
            self.assertEqual(saved["exit_finalsets"], ["EXIT X"])
            self.assertIn("match ANY", window.buy_set_combo.lineEdit().text())
        finally:
            window.close()
            self.app.processEvents()

    def test_mouse_clicks_can_check_more_than_one_combo_item(self):
        combo = module.CheckableComboBox(empty_text="None")
        combo.addCheckItem("Rule A", "A")
        combo.addCheckItem("Rule B", "B")
        combo.addCheckItem("Rule C", "C")
        combo.resize(360, 30)
        combo.show()
        try:
            combo.showPopup()
            self.app.processEvents()
            view = combo.view()
            for row in (0, 1):
                model_index = combo.model().index(row, 0)
                QTest.mouseClick(
                    view.viewport(),
                    module.Qt.MouseButton.LeftButton,
                    pos=view.visualRect(model_index).center(),
                )
                self.app.processEvents()
            self.assertEqual(combo.checkedData(), ["A", "B"])
            self.assertTrue(view.isVisible())

            first_index = combo.model().index(0, 0)
            QTest.mouseClick(
                view.viewport(),
                module.Qt.MouseButton.LeftButton,
                pos=view.visualRect(first_index).center(),
            )
            self.assertEqual(combo.checkedData(), ["B"])
        finally:
            combo.hidePopup()
            combo.close()
            self.app.processEvents()

    def test_quick_or_deduplicates_and_preserves_all_matching_items(self):
        worker = module.FinalChartInkCycleWorker(
            module.finalchartink_defaults(), self.templates, {}
        )
        _FakePipeline.calls = []
        with patch.object(module, "PaperPipelineWorker", _FakePipeline):
            payload = worker._run_step3_quick_or(
                ["AAA", "BBB", "CCC", "DDD"],
                ["BUY A", "BUY B", "BUY SET"],
                ["EXIT X"],
            )
        rows = {row["Symbol"]: row for row in payload["guidance_rows"]}
        self.assertEqual(set(rows), {"AAA", "BBB", "CCC", "DDD"})
        self.assertEqual(rows["AAA"]["Signal"], "BUY")
        self.assertEqual(rows["BBB"]["Signal"], "REVIEW (BUY + EXIT)")
        self.assertEqual(rows["DDD"]["Signal"], "EXIT / WEAKNESS")
        self.assertEqual(
            rows["AAA"]["Matched Step 3 Items"], "BUY A, BUY B"
        )
        self.assertEqual(rows["CCC"]["Matched Step 3 Items"], "BUY SET")
        finalset_call = next(
            call for call in _FakePipeline.calls
            if call.get("finalset_name") == "BUY SET"
        )
        self.assertEqual(finalset_call["member_names"], ["BUY A", "BUY B"])

    def test_validation_accepts_many_and_rejects_missing_item(self):
        config = module.finalchartink_defaults()
        config.update({
            "final_additional_sources": ["NIFTY 500"],
            "buy_finalsets": ["BUY A", "BUY SET"],
            "exit_finalsets": ["EXIT X"],
        })
        self.assertEqual(
            module.validate_finalchartink_cycle_config(config, self.templates), ""
        )
        config["buy_finalsets"].append("MISSING")
        self.assertIn(
            "MISSING",
            module.validate_finalchartink_cycle_config(config, self.templates),
        )

    def test_auto_names_change_when_any_checked_step3_item_changes(self):
        config = module.finalchartink_defaults()
        config.update({
            "recipe_name": "Multi",
            "final_additional_sources": ["NIFTY 500"],
            "buy_finalsets": ["BUY A", "BUY B"],
            "exit_finalsets": ["EXIT X"],
        })
        first = module.build_finalchartink_auto_watchlist_names(
            config, self.templates
        )
        changed = dict(config)
        changed["buy_finalsets"] = ["BUY A", "BUY SET"]
        second = module.build_finalchartink_auto_watchlist_names(
            changed, self.templates
        )
        self.assertNotEqual(first["watchlist_flow_id"], second["watchlist_flow_id"])
        self.assertIn("S3B-", first["final_input_watchlist"])

    def test_duplicate_strategy_is_unsaved_editable_and_independent(self):
        window = module.FinalChartInkWindow()
        try:
            source_name = "Momentum Strategy"
            source = module.finalchartink_defaults()
            source.update({
                "recipe_name": source_name,
                "auto_watchlist_naming": False,
                "multirule_watchlist": "Original Multi",
                "manual_multirule_watchlist": "Original Multi",
                "final_input_watchlist": "Original Final",
                "manual_final_input_watchlist": "Original Final",
                "interval_minutes": 27,
                "technical_adjusted": False,
            })
            existing_copy = f"{source_name} — Copy"
            window.recipe_state = {
                "version": 1,
                "active": source_name,
                "recipes": {
                    source_name: source,
                    existing_copy: {
                        **source,
                        "recipe_name": existing_copy,
                    },
                },
            }
            window.recipe_combo.blockSignals(True)
            window.recipe_combo.clear()
            window.recipe_combo.addItem("— New unsaved strategy —", "")
            window.recipe_combo.addItem(source_name, source_name)
            window.recipe_combo.addItem(existing_copy, existing_copy)
            window.recipe_combo.setCurrentIndex(1)
            window.recipe_combo.blockSignals(False)
            window._apply_config(source)
            window._update_delete_recipe_button()
            original_snapshot = dict(window.recipe_state["recipes"][source_name])

            self.assertTrue(window.duplicate_recipe_btn.isEnabled())
            window._duplicate_recipe()

            self.assertEqual(window.recipe_combo.currentData(), "")
            self.assertEqual(
                window.recipe_name_edit.text(),
                f"{source_name} — Copy 2",
            )
            self.assertEqual(window.interval_spin.value(), 27)
            self.assertFalse(window.adjusted_cb.isChecked())
            self.assertNotEqual(
                window.multirule_name_edit.text(), "Original Multi"
            )
            self.assertNotEqual(
                window.final_source_name_edit.text(), "Original Final"
            )
            self.assertFalse(window.delete_recipe_btn.isEnabled())
            self.assertFalse(window.duplicate_recipe_btn.isEnabled())
            self.assertEqual(
                window.recipe_state["recipes"][source_name], original_snapshot
            )

            window.recipe_name_edit.setText("My Modified Copy")
            self.assertEqual(window.recipe_name_edit.text(), "My Modified Copy")
            self.assertNotIn(
                "My Modified Copy", window.recipe_state["recipes"]
            )
        finally:
            window.close()
            self.app.processEvents()

    def test_latest_history_anchors_to_newest_official_bhavcopy(self):
        requested = module.NSE_CALENDAR.previous_session(module.date.today())
        official = module.NSE_CALENDAR.previous_session(
            requested - module.timedelta(days=1)
        )
        config = module.finalchartink_defaults()
        config["as_of_mode"] = "latest"
        with patch.object(
            module, "latest_official_bhavcopy_session", return_value=official
        ):
            worker = module.FinalChartInkHistoryWorker(
                config, self.templates, {}, 6
            )
        self.assertEqual(worker.config["history_requested_end"], requested.isoformat())
        self.assertEqual(worker.history_end, official.isoformat())
        self.assertEqual(worker.config["history_effective_end"], official.isoformat())
        self.assertTrue(worker.config["history_official_anchor_applied"])
        self.assertEqual(
            worker.config["history_price_date_policy"],
            "latest verified official Bhavcopy session",
        )

    def test_historical_as_of_keeps_its_selected_session_policy(self):
        config = module.finalchartink_defaults()
        config.update({
            "as_of_mode": "historical",
            "as_of_date": "2026-09-20",
        })
        expected = module.NSE_CALENDAR.previous_session(
            module.date.fromisoformat("2026-09-20")
        )
        with patch.object(
            module,
            "latest_official_bhavcopy_session",
            side_effect=AssertionError("Historical mode must not consult latest"),
        ):
            worker = module.FinalChartInkHistoryWorker(
                config, self.templates, {}, 6
            )
        self.assertEqual(worker.history_end, expected.isoformat())
        self.assertFalse(worker.config["history_official_anchor_applied"])
        self.assertEqual(
            worker.config["history_price_date_policy"],
            "selected historical session",
        )

    def test_component_history_scan_uses_effective_date_exactly(self):
        config = module.finalchartink_defaults()
        config["as_of_mode"] = "latest"
        official = module.date.fromisoformat("2026-09-23")
        with patch.object(
            module, "latest_official_bhavcopy_session", return_value=official
        ):
            worker = module.FinalChartInkHistoryWorker(
                config, self.templates, {}, 6
            )
        captured = {}

        class FakeHistoryScanner:
            def __init__(self, setup, **kwargs):
                captured["setup"] = setup

            def scan(self, progress):
                return "report"

        with patch.object(module, "HistoryScannerWorker", FakeHistoryScanner):
            report = worker._scan("BUY A", ["AAA"], {}, 0, 100)
        self.assertEqual(report, "report")
        self.assertEqual(captured["setup"]["as_of_mode"], "historical")
        self.assertEqual(captured["setup"]["as_of_date"], "2026-09-23")

    def test_history_frame_pool_loads_maximum_range_once_and_returns_exact_slices(self):
        dates = pd.date_range("2025-01-01", "2025-01-10", freq="D")
        full = pd.DataFrame({
            "Open": range(10), "High": range(1, 11), "Low": range(10),
            "Close": range(1, 11), "Volume": [100] * 10,
        }, index=dates)

        class FakeCache:
            def __init__(self):
                self.loads = []

            def load(self, symbol, start, end, adjusted):
                self.loads.append((symbol, pd.Timestamp(start), pd.Timestamp(end)))
                return full.loc[(full.index >= start) & (full.index <= end)].copy()

            def is_covered(self, symbol, start, end, adjusted):
                return True

        shared = {}
        cache = FakeCache()
        first = module.HistoryScannerWorker(
            module.default_setup(),
            cache_path=ROOT / "price_cache.db",
            shared_history_frames=shared,
            shared_history_start="2025-01-01",
        )
        first_frame, first_covered, first_reused = first._load_history_frame(
            cache, "ABC", pd.Timestamp("2025-01-05"),
            pd.Timestamp("2025-01-10"), True,
        )
        second = module.HistoryScannerWorker(
            module.default_setup(),
            cache_path=ROOT / "price_cache.db",
            shared_history_frames=shared,
            shared_history_start="2025-01-01",
        )
        second_frame, second_covered, second_reused = second._load_history_frame(
            cache, "ABC", pd.Timestamp("2025-01-08"),
            pd.Timestamp("2025-01-10"), True,
        )
        self.assertEqual(len(cache.loads), 1)
        self.assertTrue(first_covered)
        self.assertFalse(first_reused)
        self.assertEqual(first_frame.index.min(), pd.Timestamp("2025-01-05"))
        self.assertTrue(second_covered)
        self.assertTrue(second_reused)
        self.assertEqual(second_frame.index.min(), pd.Timestamp("2025-01-08"))

    def test_history_frame_pool_falls_back_to_original_exact_range(self):
        dates = pd.date_range("2025-01-01", "2025-01-10", freq="D")
        full = pd.DataFrame({
            "Open": range(10), "High": range(1, 11), "Low": range(10),
            "Close": range(1, 11), "Volume": [100] * 10,
        }, index=dates)

        class FakeCache:
            def __init__(self):
                self.loads = []

            def load(self, symbol, start, end, adjusted):
                self.loads.append(pd.Timestamp(start))
                return full.loc[(full.index >= start) & (full.index <= end)].copy()

            def is_covered(self, symbol, start, end, adjusted):
                return pd.Timestamp(start) >= pd.Timestamp("2025-01-05")

        shared = {}
        cache = FakeCache()
        worker = module.HistoryScannerWorker(
            module.default_setup(),
            cache_path=ROOT / "price_cache.db",
            shared_history_frames=shared,
            shared_history_start="2025-01-01",
        )
        frame, covered, reused = worker._load_history_frame(
            cache, "ABC", pd.Timestamp("2025-01-05"),
            pd.Timestamp("2025-01-10"), True,
        )
        self.assertEqual(cache.loads, [
            pd.Timestamp("2025-01-01"), pd.Timestamp("2025-01-05")
        ])
        self.assertTrue(covered)
        self.assertFalse(reused)
        self.assertFalse(shared)
        self.assertEqual(frame.index.min(), pd.Timestamp("2025-01-05"))


if __name__ == "__main__":
    unittest.main()
