from __future__ import annotations

import math
import os
import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR / "_packaging_runtime"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pandas as pd

import ChartSageAlgoOPENcode as chartsage
import FINALChartSageV1 as final_chartsage


def rising_prices(periods: int) -> pd.DataFrame:
    index = pd.bdate_range("2025-01-01", periods=periods)
    close = pd.Series(np.arange(100.0, 100.0 + periods), index=index)
    return pd.DataFrame(
        {
            "Open": close - 0.5,
            "High": close + 1.0,
            "Low": close - 1.0,
            "Close": close,
            "Volume": 1_000_000.0,
        },
        index=index,
    )


class OccurrenceCountRuleTests(unittest.TestCase):
    EXAMPLES = (
        "Close above SMA 20 on at least 10 of the last 30 days",
        "Close above EMA 50 on at least 20 of the last 30 days",
        "Close above Supertrend(10,3) on at least 15 of the last 20 days",
        "RSI 14 above 60 on at least 8 of the last 15 days",
        "SMA 10 above SMA 20 on at least 12 of the last 20 days",
        "EMA 10 crosses above EMA 20 on at least 2 of the last 60 days",
    )

    def test_all_built_in_frequency_examples_parse_and_round_trip(self) -> None:
        for sentence in self.EXAMPLES:
            with self.subTest(sentence=sentence):
                node = chartsage.RuleParser.parse(sentence)
                chartsage.validate_ast(node)
                self.assertEqual(node["occurrence"]["timeframe"], "D")
                reparsed = chartsage.RuleParser.parse(chartsage.expression_text(node))
                self.assertEqual(reparsed, node)

    def test_chart_style_count_syntax_is_also_accepted(self) -> None:
        node = chartsage.RuleParser.parse(
            "Daily Count(30, Daily Close > Daily SMA 20) >= 10"
        )
        self.assertEqual(
            node["occurrence"],
            {"lookback": 30, "threshold": 10, "operator": ">=", "timeframe": "D"},
        )
        natural = chartsage.RuleParser.parse(
            "Close above SMA 20 for at least 10 out of the last 30 days"
        )
        self.assertEqual(natural, node)

    def test_current_scan_counts_true_candles_and_records_evidence(self) -> None:
        frame = rising_prices(80)
        node = chartsage.RuleParser.parse(self.EXAMPLES[0])
        market = chartsage.MarketData(frame, frame.index[-1], include_current=True)
        evidence: list[dict] = []

        self.assertTrue(chartsage.evaluate_ast(node, market, evidence))
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["left"], 30.0)
        self.assertEqual(evidence[0]["right"], 10.0)
        self.assertIn("Count(30 D)", evidence[0]["result_column"])

    def test_incomplete_valid_window_does_not_pass(self) -> None:
        frame = rising_prices(40)
        node = chartsage.RuleParser.parse(self.EXAMPLES[0])
        market = chartsage.MarketData(frame, frame.index[-1], include_current=True)
        evidence: list[dict] = []

        self.assertFalse(chartsage.evaluate_ast(node, market, evidence))
        self.assertTrue(math.isnan(evidence[0]["left"]))

    def test_historical_and_current_latest_results_agree(self) -> None:
        frame = rising_prices(80)
        node = chartsage.RuleParser.parse(self.EXAMPLES[0])
        current_market = chartsage.MarketData(
            frame, frame.index[-1], include_current=True
        )
        history_market = chartsage.HistoricalMarketData(frame, frame.index[-1])
        current_evidence: list[dict] = []
        history_evidence: list[dict] = []

        current_match = chartsage.evaluate_ast(
            node, current_market, current_evidence
        )
        historical_matches = chartsage.evaluate_historical_ast(
            node, history_market, history_evidence
        )
        self.assertEqual(bool(historical_matches.iloc[-1]), current_match)
        self.assertEqual(
            float(history_evidence[0]["left"].iloc[-1]),
            current_evidence[0]["left"],
        )

    def test_weekly_frequency_counts_weekly_candles(self) -> None:
        frame = rising_prices(100)
        node = chartsage.RuleParser.parse(
            "Weekly close above weekly SMA 2 on at least 2 of the last 3 weeks"
        )
        current_market = chartsage.MarketData(
            frame, frame.index[-1], include_current=True
        )
        history_market = chartsage.HistoricalMarketData(frame, frame.index[-1])
        current_evidence: list[dict] = []
        history_evidence: list[dict] = []

        self.assertTrue(
            chartsage.evaluate_ast(node, current_market, current_evidence)
        )
        history_result = chartsage.evaluate_historical_ast(
            node, history_market, history_evidence
        )
        self.assertTrue(bool(history_result.iloc[-1]))
        self.assertEqual(current_evidence[0]["left"], 3.0)
        self.assertEqual(float(history_evidence[0]["left"].iloc[-1]), 3.0)

    def test_validation_rejects_mixed_timeframes_and_pnl_frequency(self) -> None:
        mixed = chartsage.occurrence_rule(
            chartsage.rule_node(
                chartsage.field_operand("Close", "D"),
                ">",
                chartsage.field_operand("SMA", "W", period=20),
            ),
            30,
            10,
            ">=",
            "D",
        )
        with self.assertRaisesRegex(ValueError, "same timeframe"):
            chartsage.validate_ast(mixed)

        pnl = chartsage.occurrence_rule(
            chartsage.rule_node(
                chartsage.field_operand("Profit %", "D"),
                ">=",
                chartsage.value_operand(5),
            )
        )
        with self.assertRaisesRegex(ValueError, "Profit/Loss"):
            chartsage.validate_ast(pnl)

    def test_rule_dialog_round_trips_frequency_controls(self) -> None:
        app = chartsage.QApplication.instance() or chartsage.QApplication([])
        node = chartsage.RuleParser.parse(self.EXAMPLES[0])
        dialog = chartsage.RuleDialog(node)
        try:
            self.assertTrue(dialog.occurrence_enabled.isChecked())
            self.assertEqual(dialog.occurrence_timeframe.currentData(), "D")
            self.assertEqual(dialog.occurrence_lookback.value(), 30)
            self.assertEqual(dialog.occurrence_threshold.value(), 10)
            self.assertEqual(dialog.result_rule(), node)
        finally:
            dialog.close()
            app.processEvents()

    def test_final_chartsage_can_consume_the_same_saved_rule(self) -> None:
        sentence = self.EXAMPLES[0]
        algo_node = chartsage.RuleParser.parse(sentence)
        final_node = final_chartsage.RuleParser.parse(sentence)
        self.assertEqual(final_node, algo_node)

        frame = rising_prices(80)
        market = final_chartsage.MarketData(
            frame, frame.index[-1], include_current=True
        )
        evidence: list[dict] = []
        self.assertTrue(final_chartsage.evaluate_ast(final_node, market, evidence))
        self.assertEqual(evidence[0]["left"], 30.0)


if __name__ == "__main__":
    unittest.main()
