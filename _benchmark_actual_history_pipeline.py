"""Read-only end-to-end benchmark of the active FINAL ChartSage history flow.

The current pass uses the production rule-by-rule history path.  The proposed
pass reads one maximum-lookback adjusted frame per symbol, serves exact slices
to each component rule, and reuses HistoricalMarketData only when its complete
frame boundary is identical.  Live application state is never written.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "_packaging_runtime"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd

import FINALChartSageV1 as app


MONTHS = 6
RECIPE_PATH = ROOT / "FINALCHARTinkV1_data" / "finalchartink_recipes.json"
TEMPLATE_PATH = ROOT / "scanner_templates.json"
PRICE_PATH = ROOT / "price_cache.db"


class Metrics:
    def __init__(self):
        self.load_calls = 0
        self.load_seconds = 0.0
        self.load_rows = 0
        self.evaluate_calls = 0
        self.evaluate_seconds = 0.0
        self.rank_calls = 0
        self.rank_seconds = 0.0
        self.membership_calls = 0
        self.membership_seconds = 0.0
        self.phases = defaultdict(float)
        self._last_time = None
        self._last_phase = "setup/source resolution"
        self.events = []

    @staticmethod
    def phase_for(text: str) -> str:
        lower = str(text).lower()
        if "cached price" in lower or "price history" in lower or "refreshing price" in lower:
            return "price-cache loading"
        if "evaluating" in lower and ("indicator" in lower or "rule" in lower):
            return "indicator/rule evaluation"
        if "ranking" in lower or "daily summary" in lower:
            return "ranking/component summaries"
        if "membership" in lower:
            return "membership filtering"
        if "reference" in lower:
            return "paper reference preparation"
        if "combining final" in lower or "final summary" in lower:
            return "final combination"
        if "complete" in lower or "displaying" in lower:
            return "completion"
        return "setup/source resolution"

    def progress(self, percent: int, text: str):
        now = time.perf_counter()
        if self._last_time is not None:
            self.phases[self._last_phase] += now - self._last_time
        self._last_time = now
        self._last_phase = self.phase_for(text)
        self.events.append((int(percent), str(text), now))

    def finish(self):
        if self._last_time is not None:
            self.phases[self._last_phase] += time.perf_counter() - self._last_time


ORIGINAL_PRICE_CACHE = app.PriceCache
ORIGINAL_HISTORICAL_MARKET = app.HistoricalMarketData
ORIGINAL_EVALUATE = app.evaluate_historical_ast
ORIGINAL_RANK = app.rank_scan_rows
ORIGINAL_MEMBERS_ON = app.HistoricalIndexMembershipStore.members_on
ORIGINAL_MEMBERS_DURING = app.HistoricalIndexMembershipStore.members_during
ORIGINAL_REQUIRE_COVERAGE = app.HistoricalIndexMembershipStore.require_coverage


class ReadOnlyPriceCache(ORIGINAL_PRICE_CACHE):
    metrics: Metrics
    superset_frames: dict[str, pd.DataFrame] = {}
    optimized = False

    def __init__(self, path: Path = PRICE_PATH):
        resolved = Path(path).resolve().as_posix()
        self.path = f"file:{resolved}?mode=ro"

    def _connect(self):
        conn = sqlite3.connect(self.path, uri=True, timeout=30)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def load(self, symbol, start, end, adjusted):
        began = time.perf_counter()
        self.__class__.metrics.load_calls += 1
        clean_symbol = str(symbol).strip().upper()
        if self.__class__.optimized and clean_symbol in self.__class__.superset_frames:
            frame = self.__class__.superset_frames[clean_symbol]
            result = frame.loc[
                (frame.index >= pd.Timestamp(start)) & (frame.index <= pd.Timestamp(end))
            ].copy()
        else:
            result = super().load(symbol, start, end, adjusted)
        self.__class__.metrics.load_seconds += time.perf_counter() - began
        self.__class__.metrics.load_rows += len(result)
        return result

    def store(self, *args, **kwargs):
        raise RuntimeError("Read-only benchmark attempted to write the price cache")


def install_instrumentation(metrics: Metrics, share_markets: bool):
    ReadOnlyPriceCache.metrics = metrics
    app.PriceCache = ReadOnlyPriceCache

    market_cache = {}

    def historical_market(daily, as_of, symbol="", account_name="ALL", holdings_path=app.HOLDINGS_DETAILS_FILE):
        if not share_markets:
            return ORIGINAL_HISTORICAL_MARKET(
                daily, as_of, symbol=symbol, account_name=account_name,
                holdings_path=holdings_path,
            )
        first = str(daily.index[0]) if not daily.empty else ""
        last = str(daily.index[-1]) if not daily.empty else ""
        key = (str(symbol), str(pd.Timestamp(as_of)), first, last, len(daily), str(account_name))
        if key not in market_cache:
            market_cache[key] = ORIGINAL_HISTORICAL_MARKET(
                daily, as_of, symbol=symbol, account_name=account_name,
                holdings_path=holdings_path,
            )
        return market_cache[key]

    app.HistoricalMarketData = historical_market

    def evaluate(*args, **kwargs):
        began = time.perf_counter()
        try:
            return ORIGINAL_EVALUATE(*args, **kwargs)
        finally:
            metrics.evaluate_calls += 1
            metrics.evaluate_seconds += time.perf_counter() - began

    def rank(*args, **kwargs):
        began = time.perf_counter()
        try:
            return ORIGINAL_RANK(*args, **kwargs)
        finally:
            metrics.rank_calls += 1
            metrics.rank_seconds += time.perf_counter() - began

    def timed_members(original):
        def wrapper(self, *args, **kwargs):
            began = time.perf_counter()
            try:
                return original(self, *args, **kwargs)
            finally:
                metrics.membership_calls += 1
                metrics.membership_seconds += time.perf_counter() - began
        return wrapper

    app.evaluate_historical_ast = evaluate
    app.rank_scan_rows = rank
    app.HistoricalIndexMembershipStore.members_on = timed_members(ORIGINAL_MEMBERS_ON)
    app.HistoricalIndexMembershipStore.members_during = timed_members(ORIGINAL_MEMBERS_DURING)
    app.HistoricalIndexMembershipStore.require_coverage = timed_members(ORIGINAL_REQUIRE_COVERAGE)


def load_active():
    recipe_state = json.loads(RECIPE_PATH.read_text(encoding="utf-8"))
    template_state = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    active = recipe_state["active"]
    config = app.normalize_finalchartink_recipe(recipe_state["recipes"][active])
    templates = template_state["templates"]
    return active, config, templates


def technical_template_names(config, templates):
    names = list(config["feeder_templates"])
    for key in ("buy_finalsets", "exit_finalsets"):
        for selected in config[key]:
            members, _required = app.FinalChartInkHistoryWorker._rule_members(
                selected, templates
            )
            for name in members:
                if not app.expression_has_pnl_rules(templates[name].get("expression")):
                    names.append(name)
    return list(dict.fromkeys(names))


def preload_supersets(config, templates, catalog, metrics):
    probe = app.FinalChartInkHistoryWorker(config, templates, catalog, MONTHS)
    names = technical_template_names(probe.config, templates)
    earliest = pd.Timestamp(probe.history_start)
    for name in names:
        setup = templates[name]
        lookback = max(
            int(app.required_calendar_days(setup["expression"])),
            app.ranking_required_calendar_days(setup),
        )
        earliest = min(earliest, pd.Timestamp(probe.history_start) - pd.DateOffset(days=lookback))

    source_names = set(probe.config.get("final_additional_sources", []))
    for feeder_name in probe.config["feeder_templates"]:
        setup = templates[feeder_name]
        if probe.config["feeder_scope"] in {"SAVED", "COMBINE"}:
            source_names.update(setup.get("universes", []))
    if probe.config["feeder_scope"] in {"OVERRIDE", "COMBINE"}:
        source_names.update(probe.config.get("feeder_sources", []))
    symbols, _details = probe._resolve_period_sources(sorted(source_names))
    cache = ReadOnlyPriceCache(PRICE_PATH)
    frames = {}
    began = time.perf_counter()
    for index, symbol in enumerate(symbols, 1):
        frames[symbol] = cache.load(
            symbol, earliest, pd.Timestamp(probe.history_end),
            bool(probe.config["technical_adjusted"]),
        )
        if index % 100 == 0:
            print(f"preload {index}/{len(symbols)}", flush=True)
    preload_seconds = time.perf_counter() - began
    ReadOnlyPriceCache.superset_frames = frames
    return len(symbols), earliest.date().isoformat(), probe.history_end, preload_seconds


def report_signature(report):
    rows = []
    for day in sorted(report.rows_by_date):
        for row in sorted(report.rows_by_date[day], key=lambda item: (str(item.get("Symbol")), str(item.get("Signal")))):
            rows.append({key: str(value) for key, value in sorted(row.items())})
    payload = {
        "rows": rows,
        "daily_summary": report.daily_summary,
        "paper_exits": report.paper_exit_rows_by_date,
        "paper_position_rule": report.paper_position_exit_rule,
        "start": report.start_date,
        "end": report.end_date,
        "total": report.total_matches,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
    return hashlib.sha256(encoded).hexdigest(), len(rows), report.total_matches


def run_pass(label, config, templates, catalog, optimized):
    metrics = Metrics()
    ReadOnlyPriceCache.optimized = optimized
    ReadOnlyPriceCache.superset_frames = {}
    install_instrumentation(metrics, share_markets=optimized)
    preload_info = None
    total_started = time.perf_counter()
    if optimized:
        preload_info = preload_supersets(config, templates, catalog, metrics)
    worker = app.FinalChartInkHistoryWorker(config, templates, catalog, MONTHS)
    worker.progress.connect(metrics.progress)
    report = worker._run_history()
    metrics.finish()
    elapsed = time.perf_counter() - total_started
    print(json.dumps({
        "label": label,
        "elapsed_seconds": elapsed,
        "signature": report_signature(report),
        "price_load_calls": metrics.load_calls,
        "price_load_seconds": metrics.load_seconds,
        "price_rows_returned": metrics.load_rows,
        "evaluate_calls": metrics.evaluate_calls,
        "evaluate_seconds": metrics.evaluate_seconds,
        "rank_calls": metrics.rank_calls,
        "rank_seconds": metrics.rank_seconds,
        "membership_calls": metrics.membership_calls,
        "membership_seconds": metrics.membership_seconds,
        "progress_phases": dict(metrics.phases),
        "preload": preload_info,
    }, indent=2), flush=True)
    return elapsed, report_signature(report)


def main():
    active, config, templates = load_active()
    catalog = app.finalchartink_source_catalog()
    print(f"active_strategy={active}", flush=True)
    print(f"technical_templates={technical_template_names(config, templates)}", flush=True)
    print(f"price_db_size={PRICE_PATH.stat().st_size}", flush=True)
    current_elapsed, current_signature = run_pass(
        "current", copy.deepcopy(config), templates, catalog, optimized=False
    )
    if "--production-only" in sys.argv:
        expected = "602a44fad641e88188e81a32371f7d0b66ac54784eb404b6428c7eb2c986e714"
        matches_baseline = current_signature[0] == expected
        print(json.dumps({
            "matches_prechange_baseline": matches_baseline,
            "expected_digest": expected,
            "actual_digest": current_signature[0],
        }, indent=2), flush=True)
        if not matches_baseline:
            raise SystemExit("Production output differs from pre-change baseline")
        return
    proposed_elapsed, proposed_signature = run_pass(
        "proposed", copy.deepcopy(config), templates, catalog, optimized=True
    )
    identical = current_signature == proposed_signature
    print(json.dumps({
        "outputs_identical": identical,
        "current_seconds": current_elapsed,
        "proposed_seconds": proposed_elapsed,
        "speedup": current_elapsed / proposed_elapsed,
        "time_reduction_percent": (1.0 - proposed_elapsed / current_elapsed) * 100.0,
    }, indent=2), flush=True)
    if not identical:
        raise SystemExit("Actual-strategy outputs differ")


if __name__ == "__main__":
    main()
