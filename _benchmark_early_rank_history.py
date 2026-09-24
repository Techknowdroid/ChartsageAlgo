"""Read-only A/B benchmark for ranking before historical evidence rows."""

from __future__ import annotations

import copy
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "_packaging_runtime"))

import _benchmark_actual_history_pipeline as bench
import pandas as pd

app = bench.app


class EarlyRankHistoryScannerWorker(app.HistoryScannerWorker):
    """Benchmark prototype: defer full row construction until after Top-N."""

    def scan(self, progress=None):
        if not bool(self.setup.get("ranking_enabled", False)):
            return super().scan(progress)
        app.validate_ast(self.setup["expression"])
        app.validate_ranking_setup(self.setup)
        progress = progress or (lambda _percent, _text: None)
        months = int(self.setup.get("history_months") or 24)
        if months not in (6, 12, 24, 48):
            raise ValueError("Unsupported benchmark period")
        if not self.symbols:
            self.symbols, universe_errors, self.symbol_details = app.resolve_universes(
                self.setup.get("universes", []),
                self.setup.get("custom_symbols", ""),
                self._check_cancelled,
            )
        else:
            universe_errors = []
        as_of = pd.Timestamp(self.setup["as_of_date"]).normalize()
        history_start = (as_of - pd.DateOffset(months=months)).normalize()
        lookback = max(
            int(app.required_calendar_days(self.setup["expression"])),
            app.ranking_required_calendar_days(self.setup),
        )
        download_start = history_start - pd.DateOffset(days=lookback)
        adjusted = bool(self.setup["adjusted"])
        cache = app.PriceCache(self.cache_path)
        frames = {}
        failed = []
        for index, symbol in enumerate(self.symbols, 1):
            self._check_cancelled()
            frame, covered, _reused = self._load_history_frame(
                cache, symbol, download_start, as_of, adjusted
            )
            if covered and not frame.empty:
                frames[symbol] = frame
            else:
                # The actual benchmark is intentionally read-only. A missing
                # range would make this comparison unlike the completed-cache
                # production run, so fail rather than downloading.
                failed.append(symbol)
            if index % 25 == 0 or index == len(self.symbols):
                progress(1 + int(index / len(self.symbols) * 7), f"Early-rank cache {index}/{len(self.symbols)}")
        if failed:
            raise RuntimeError(f"Early-rank benchmark lacks cached history for: {failed[:10]}")

        periods = app.normalize_ranking_rsi_periods(
            self.setup.get("ranking_rsi_periods", [22, 44, 66])
        )
        states = {}
        candidates_by_date = defaultdict(list)
        all_dates = set()
        insufficient_symbols = 0
        symbols_with_data = [s for s in self.symbols if s in frames and not frames[s].empty]

        # Pass 1: evaluate conditions and retain only values needed to rank.
        for symbol_index, symbol in enumerate(symbols_with_data, 1):
            self._check_cancelled()
            market = app.HistoricalMarketData(frames[symbol], as_of, symbol=symbol)
            ranking_series = {
                period: market.aligned(app.field_operand("RSI", "D", 0, period))
                for period in periods
            }
            eligible = market.index[(market.index >= history_start) & (market.index <= as_of)]
            all_dates.update(stamp.date().isoformat() for stamp in eligible)
            evidence = []
            matched = app.evaluate_historical_ast(
                self.setup["expression"], market, evidence
            ).reindex(eligible, fill_value=False)
            if any(
                not item["valid"].reindex(eligible, fill_value=False).any()
                for item in evidence
            ):
                insufficient_symbols += 1
            matched_dates = matched.index[matched]
            for stamp in matched_dates:
                values = [float(ranking_series[p].get(stamp, math.nan)) for p in periods]
                score = (
                    sum(values) / len(periods)
                    if all(math.isfinite(value) for value in values)
                    else None
                )
                candidates_by_date[stamp.date().isoformat()].append({
                    "Symbol": symbol,
                    app.COMPOSITE_RSI_MOMENTUM_COLUMN: score,
                })
            states[symbol] = {
                "market": market,
                "ranking": ranking_series,
                "evidence": evidence,
                "percent_changes": app.daily_percent_changes(market.daily),
            }
            if symbol_index % 25 == 0 or symbol_index == len(symbols_with_data):
                progress(8 + int(symbol_index / len(symbols_with_data) * 55), f"Early-rank evaluate {symbol_index}/{len(symbols_with_data)}")

        selected_by_symbol = defaultdict(list)
        for day, candidates in candidates_by_date.items():
            for candidate in app.rank_scan_rows(candidates, self.setup):
                selected_by_symbol[str(candidate["Symbol"])].append(pd.Timestamp(day))

        # Pass 2: construct the identical detailed row only for selected winners.
        rows_by_date = {}
        for symbol_index, symbol in enumerate(symbols_with_data, 1):
            self._check_cancelled()
            state = states[symbol]
            market = state["market"]
            ranking_series = state["ranking"]
            evidence = state["evidence"]
            percent_changes = state["percent_changes"]
            for stamp in selected_by_symbol.get(symbol, []):
                last = market.daily.loc[stamp]
                if isinstance(last, pd.DataFrame):
                    last = last.iloc[-1]
                evidence_parts = []
                row = {
                    "Symbol": symbol,
                    "Universe": self.symbol_details.get(symbol, {}).get("Universe", ""),
                    "Sector": self.symbol_details.get(symbol, {}).get("Sector", "") or "n/a",
                    "Industry": self.symbol_details.get(symbol, {}).get("Industry", ""),
                    "Market Cap": self.symbol_details.get(symbol, {}).get("Market Cap", ""),
                    "As Of": stamp.date().isoformat(),
                    app.DATA_SOURCE_COLUMN: cache.candle_source(symbol, stamp, adjusted),
                    app.PRICE_BASIS_COLUMN: app.price_basis_label(adjusted),
                    "Open": app.finite_or_none(last.get("Open")),
                    "High": app.finite_or_none(last.get("High")),
                    "Low": app.finite_or_none(last.get("Low")),
                    "Close": app.finite_or_none(last.get("Close")),
                    app.RSI_D_COLUMN: app.finite_or_none(
                        market.aligned(app.field_operand("RSI", "D", 0, 14)).get(stamp, math.nan)
                    ),
                    app.RSI_1DAY_AGO_COLUMN: app.finite_or_none(
                        market.aligned(app.field_operand("RSI", "D", 1, 14)).get(stamp, math.nan)
                    ),
                    app.PERCENT_CHANGE_COLUMN: app.finite_or_none(percent_changes.get(stamp, math.nan)),
                    "Volume": app.finite_or_none(last.get("Volume")),
                    app.HOLDING_QTY_COLUMN: None,
                    app.BUY_PRICE_COLUMN: None,
                    app.PNL_PCT_COLUMN: None,
                    app.PNL_AMT_COLUMN: None,
                }
                app.add_composite_rsi_ranking_values(
                    row, self.setup,
                    lambda period, stamp=stamp: ranking_series[period].get(stamp, math.nan),
                )
                for item in evidence:
                    rule_matched = bool(item["matched"].at[stamp])
                    if not rule_matched:
                        continue
                    left = float(item["left"].at[stamp])
                    right = float(item["right"].at[stamp])
                    previous_left = float(item["previous_left"].at[stamp])
                    previous_right = float(item["previous_right"].at[stamp])
                    evidence_parts.append(
                        f"{item['rule']} [{app.format_number(left)} vs {app.format_number(right)}]"
                    )
                    for side, value in (("left", left), ("right", right)):
                        operand = item[f"{side}_operand"]
                        if operand.get("kind") != "field":
                            continue
                        row[app.operand_text(operand)] = app.finite_or_none(value)
                        if operand["name"] == "52 Week High Distance %":
                            offset = int(operand.get("offset", 0))
                            close_operand = app.field_operand("Close", "D", offset)
                            high_operand = app.matching_lookback_high_operand(operand)
                            close_label = "Close" if offset == 0 else app.operand_text(close_operand)
                            row[close_label] = app.finite_or_none(market.aligned(close_operand).at[stamp])
                            row[app.operand_text(high_operand)] = app.finite_or_none(
                                market.aligned(high_operand).at[stamp]
                            )
                    if item["operator"] in {"crosses above", "crosses below"}:
                        for side, value in (("left", previous_left), ("right", previous_right)):
                            operand = item[f"{side}_operand"]
                            if operand.get("kind") == "field":
                                row[f"Previous {app.operand_text(operand)}"] = app.finite_or_none(value)
                row["Evidence"] = " | ".join(evidence_parts)
                rows_by_date.setdefault(row["As Of"], []).append(row)
            if symbol_index % 25 == 0 or symbol_index == len(symbols_with_data):
                progress(63 + int(symbol_index / len(symbols_with_data) * 25), f"Early-rank evidence {symbol_index}/{len(symbols_with_data)}")

        for day, rows in list(rows_by_date.items()):
            rows_by_date[day] = app.rank_scan_rows(rows, self.setup)
        sorted_dates = sorted(all_dates)
        daily_summary = []
        for day in sorted_dates:
            sectors = {}
            for row in rows_by_date.get(day, []):
                sector = row.get("Sector") or "n/a"
                sectors[sector] = sectors.get(sector, 0) + 1
            daily_summary.append({"date": day, "total": len(rows_by_date.get(day, [])), "sectors": sectors})
        return app.HistoryReport(
            rows_by_date=rows_by_date,
            daily_summary=daily_summary,
            total_symbols=len(self.symbols),
            scanned_symbols=len(symbols_with_data),
            total_matches=sum(len(rows) for rows in rows_by_date.values()),
            dates_with_matches=len(rows_by_date),
            downloaded_symbols=0,
            cache_hits=len(frames),
            missing_symbols=max(0, len(self.symbols) - len(symbols_with_data)),
            download_failure_symbols=0,
            insufficient_symbols=insufficient_symbols,
            universe_errors=universe_errors,
            start_date=history_start.date().isoformat(),
            end_date=as_of.date().isoformat(),
            download_failed_symbols=[],
        )


def main():
    active, config, templates = bench.load_active()
    catalog = app.finalchartink_source_catalog()
    print(f"active_strategy={active}", flush=True)
    current_seconds, current_signature = bench.run_pass(
        "current-shared-frame",
        copy.deepcopy(config), templates, catalog, optimized=False,
    )
    original_worker = app.HistoryScannerWorker
    app.HistoryScannerWorker = EarlyRankHistoryScannerWorker
    try:
        proposed_seconds, proposed_signature = bench.run_pass(
            "early-rank-prototype",
            copy.deepcopy(config), templates, catalog, optimized=False,
        )
    finally:
        app.HistoryScannerWorker = original_worker
    identical = current_signature == proposed_signature
    print(json.dumps({
        "outputs_identical": identical,
        "current_seconds": current_seconds,
        "early_rank_seconds": proposed_seconds,
        "speedup": current_seconds / proposed_seconds,
        "time_reduction_percent": (1.0 - proposed_seconds / current_seconds) * 100.0,
        "signature": current_signature,
    }, indent=2), flush=True)
    if not identical:
        raise SystemExit("Early-ranking output differs")


if __name__ == "__main__":
    main()
