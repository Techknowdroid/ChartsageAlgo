# ChartSageAlgo

ChartSageAlgo is a Windows desktop suite for rule-based NSE stock scanning,
multi-rule BUY/EXIT guidance, historical backtesting and paper-trade replay.

## Applications

- `ChartSageAlgoOPENcode.py` — rule scanner and strategy editor.
- `FINALCHARTinkV1.py` — multi-rule watchlist flow, backtest history and paper replay.
- `ChartSageBhavcopy.py` — official NSE Bhavcopy archive and market-data maintenance.

The two scanner applications can use either the original latest-price path or
validated Tickertape batch prices with automatic per-symbol fallback. On
startup they safely check missing Bhavcopy sessions, corporate actions and
historical NIFTY membership.

## Repository scope

This repository contains source code, tests, PyInstaller specifications,
sanitised seed configuration and the compact historical-membership source set.
It intentionally excludes credentials, broker sessions, personal holdings,
live SQLite databases, downloaded Bhavcopy archives, reports, backups and built
distributions.

Use `create_important_backup.py` for a complete restorable local backup. Use
`build_portable_bundle.py` when an explicitly requested, validated portable
Windows distribution is required.

## Safety

The FINAL ChartInk application provides guidance and historical paper replay;
it does not place Zerodha orders. Each user must configure their own broker and
Telegram credentials locally. Secrets are never intended to be committed.

