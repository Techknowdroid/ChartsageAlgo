"""Reproducible, read-only benchmark for shared historical rule evaluation.

This does not write application state or alter production scan behaviour.  It
compares the current rule-major evaluation shape with a proposed symbol-major
shape that reuses HistoricalMarketData and its indicator cache across rules.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "_packaging_runtime"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pandas as pd

import FINALChartSageV1 as app


SYMBOL_COUNT = 537
AS_OF = pd.Timestamp("2026-09-23")
HISTORY_START = AS_OF - pd.DateOffset(months=6)
DATES = pd.bdate_range("2022-12-01", AS_OF)


def r(left, operator, right):
    return app.rule_node(left, operator, right)


def g(op, *children):
    return app.group_node(op, list(children))


RULES = {
    "RSI5 above 50": g(
        "AND", r(app.field_operand("RSI", "D", 0, 5), ">", app.value_operand(50))
    ),
    "RSI5 crosses 30": g(
        "AND",
        r(app.field_operand("RSI", "D", 0, 5), "crosses above", app.value_operand(30)),
    ),
    "RSI14 above 60": g(
        "AND", r(app.field_operand("RSI", "D", 0, 14), ">", app.value_operand(60))
    ),
    "MACD histogram positive": g(
        "AND",
        r(app.field_operand("MACD Histogram", "D"), ">", app.value_operand(0)),
    ),
    "MACD line crosses signal": g(
        "AND",
        r(
            app.field_operand("MACD Line", "D"),
            "crosses above",
            app.field_operand("MACD Signal", "D"),
        ),
    ),
    "Close above SMA200": g(
        "AND",
        r(app.field_operand("Close", "D"), ">", app.field_operand("SMA", "D", 0, 200)),
    ),
    "Close above SMA50": g(
        "AND",
        r(app.field_operand("Close", "D"), ">", app.field_operand("SMA", "D", 0, 50)),
    ),
    "Close above EMA50": g(
        "AND",
        r(app.field_operand("Close", "D"), ">", app.field_operand("EMA", "D", 0, 50)),
    ),
    "Supertrend positive": g(
        "AND",
        r(
            app.field_operand("Close", "D"),
            ">",
            app.field_operand("Supertrend", "D", 0, 10, 3.0),
        ),
    ),
    "RSI uptrend compound": g(
        "AND",
        r(app.field_operand("RSI", "D", 0, 5), "crosses above", app.value_operand(30)),
        r(app.field_operand("Close", "D"), ">", app.field_operand("SMA", "D", 0, 20)),
        r(app.field_operand("Close", "D"), ">", app.field_operand("SMA", "D", 0, 50)),
        r(app.field_operand("Close", "D"), ">", app.field_operand("SMA", "D", 0, 100)),
        r(app.field_operand("Close", "D"), ">", app.field_operand("SMA", "D", 0, 200)),
    ),
}


def make_frames() -> dict[str, pd.DataFrame]:
    frames = {}
    for number in range(SYMBOL_COUNT):
        rng = np.random.default_rng(10_000 + number)
        returns = rng.normal(0.00035, 0.018, len(DATES))
        close = (80.0 + number % 170) * np.exp(np.cumsum(returns))
        open_ = close * (1.0 + rng.normal(0.0, 0.004, len(DATES)))
        spread = rng.uniform(0.002, 0.025, len(DATES))
        frames[f"SIM{number:04d}"] = pd.DataFrame(
            {
                "Open": open_,
                "High": np.maximum(open_, close) * (1.0 + spread),
                "Low": np.minimum(open_, close) * (1.0 - spread),
                "Close": close,
                "Volume": rng.integers(50_000, 5_000_000, len(DATES)).astype(float),
            },
            index=DATES,
        )
    return frames


def record(matches, rule_name, symbol, series):
    selected = series.index[series]
    matches[rule_name].extend(f"{symbol}:{stamp.date().isoformat()}" for stamp in selected)


def signature(matches):
    digest = hashlib.sha256()
    counts = {}
    for rule_name in RULES:
        values = sorted(matches[rule_name])
        counts[rule_name] = len(values)
        digest.update(rule_name.encode())
        digest.update("\n".join(values).encode())
    return digest.hexdigest(), counts


def current_method(frames):
    matches = defaultdict(list)
    started = time.perf_counter()
    for rule_name, expression in RULES.items():
        for symbol, frame in frames.items():
            market = app.HistoricalMarketData(frame, AS_OF, symbol=symbol)
            eligible = market.index[
                (market.index >= HISTORY_START) & (market.index <= AS_OF)
            ]
            result = app.evaluate_historical_ast(expression, market, []).reindex(
                eligible, fill_value=False
            )
            record(matches, rule_name, symbol, result)
    return time.perf_counter() - started, signature(matches)


def shared_method(frames):
    matches = defaultdict(list)
    started = time.perf_counter()
    for symbol, frame in frames.items():
        market = app.HistoricalMarketData(frame, AS_OF, symbol=symbol)
        eligible = market.index[
            (market.index >= HISTORY_START) & (market.index <= AS_OF)
        ]
        for rule_name, expression in RULES.items():
            result = app.evaluate_historical_ast(expression, market, []).reindex(
                eligible, fill_value=False
            )
            record(matches, rule_name, symbol, result)
    return time.perf_counter() - started, signature(matches)


def main():
    generated = time.perf_counter()
    frames = make_frames()
    generation_seconds = time.perf_counter() - generated
    current_seconds, current_result = current_method(frames)
    shared_seconds, shared_result = shared_method(frames)
    identical = current_result == shared_result
    print(f"symbols={SYMBOL_COUNT} rules={len(RULES)} rows_per_symbol={len(DATES)}")
    print(f"frame_generation_seconds={generation_seconds:.3f}")
    print(f"current_seconds={current_seconds:.3f}")
    print(f"shared_seconds={shared_seconds:.3f}")
    print(f"speedup={current_seconds / shared_seconds:.3f}x")
    print(f"time_reduction={(1.0 - shared_seconds / current_seconds) * 100.0:.2f}%")
    print(f"outputs_identical={identical}")
    print(f"output_digest={current_result[0]}")
    print(f"match_counts={current_result[1]}")
    if not identical:
        raise SystemExit("Benchmark outputs differ")


if __name__ == "__main__":
    main()
