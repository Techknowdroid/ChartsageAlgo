# ChartSageAlgo

ChartSageAlgo is a Windows desktop suite for rule-based NSE stock scanning,
multi-rule BUY/EXIT guidance, historical backtesting and paper-trade replay.

## Applications

- `ChartSageAlgoOPENcode.py` — rule scanner and strategy editor.
- `ChartSageStockAlerts.py` — persistent stock-specific technical alert monitor.
- `FINALCHARTinkV1.py` — multi-rule watchlist flow, backtest history and paper replay.
- `ChartSageBhavcopy.py` — official NSE Bhavcopy archive and market-data maintenance.

The two scanner applications can use either the original latest-price path or
validated Tickertape batch prices with automatic per-symbol fallback. On
startup they safely check missing Bhavcopy sessions, corporate actions and
historical NIFTY membership.

Run the stock-alert monitor by double-clicking `RunChartSageStockAlerts.cmd`,
or from PowerShell with:

```powershell
.\RunChartSageStockAlerts.cmd
```

It evaluates one stock against its own independently built price/technical rule
expression, maintains trigger history in `stock_alerts.db`, and can send Telegram
notifications. Saved ChartSage rules are optional imports only; imported rules
are copied into the alert and can be edited without changing the original.
It provides guidance only and never submits broker orders.
The launcher uses the project-local `_packaging_runtime` before any globally
installed Python packages.

## Repository scope

This repository contains source code, tests, PyInstaller specifications,
sanitised seed configuration and the compact historical-membership source set.
It intentionally excludes credentials, broker sessions, personal holdings,
live SQLite databases, downloaded Bhavcopy archives, reports, backups and built
distributions.

Use `create_important_backup.py` for a complete restorable local backup. Use
`build_portable_bundle.py` when an explicitly requested, validated portable
Windows distribution is required.

## Updating GitHub after future changes

Run the interactive safety helper from PowerShell in this folder:

```powershell
python update_github.py
```

The helper reviews the staged source changes, checks them for likely private
data, and proposes an appropriate commit message. Press Enter to accept the
suggestion; you can still type a replacement when desired. Commit and push each
retain their own explicit confirmation.

It shows the changed and staged files, checks for common private-data mistakes,
and requires confirmation before committing and pushing. It respects
`.gitignore`. This is only a source-code update; run
`create_important_backup.py` separately for a restorable backup containing the
local databases and operational data.

## Safety

The FINAL ChartInk application provides guidance and historical paper replay;
it does not place Zerodha orders. Each user must configure their own broker and
Telegram credentials locally. Secrets are never intended to be committed.
