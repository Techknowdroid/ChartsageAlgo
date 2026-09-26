# ChartSageAlgo

ChartSageAlgo is a Windows desktop suite for rule-based NSE stock scanning,
multi-rule BUY/EXIT guidance, historical backtesting and paper-trade replay.

## Applications

- `ChartSageAlgoOPENcode.py` — rule scanner and strategy editor.
- `ChartSageStockAlerts.py` — persistent stock-specific technical alert monitor.
- `FINALChartSageV1.py` — multi-rule watchlist flow, backtest history and paper replay.
- `ChartSageBhavcopy.py` — official NSE Bhavcopy archive and market-data maintenance.

The two scanner applications can use either the original latest-price path or
validated Tickertape batch prices with automatic per-symbol fallback. On
startup they safely check missing Bhavcopy sessions, corporate actions and
historical NIFTY membership.

The ChartSage Rule Scanner universe section includes a large resizable pop-out
selector with search, bulk select/clear controls, optional-symbol entry and
watchlist management. Its selections remain live-synchronised with the compact
main-window checklist in both directions.

FINAL ChartSage automatically names its four generated watchlists from the
selected Step 1, Step 2A, Step 2B and Step 3 flow. The deterministic names use
role suffixes for Step 1 matches, final input, BUY and EXIT results, plus a
short flow ID. Automatic naming is enabled by default and can be disabled per
saved strategy to restore editable legacy names. Optional manual-run snapshots
append a sortable date/time stamp.

Step 2A and Step 2B each display sources in two adjustable sections:
watchlists above and built-in universes below. Newly generated watchlists are
shown first, newest run first and in that run's generation order; other
watchlists and universes retain predictable alphabetical ordering. The search
field on either side filters both of its sections without changing saved
source names or scan behaviour.

Step 3 supports one or more checked BUY rules/FINAL SETs and one or more
checked EXIT rules/FINAL SETs. Checked items on each side use an outer ANY
(OR) combination, while every selected FINAL SET retains its own internal
AND, OR or K-of-N logic. Older strategies with one saved BUY/EXIT selection
load unchanged as a single checked item.

Holding-dependent EXIT templates can be created without a Zerodha account for
Paper Backtest use. `Profit % >= 7` represents a take-profit rule and the more
natural `Loss % >= 5` represents a loss of 5% or more from the simulated
weighted-average acquisition cost. These templates are restricted to one
standalone Step 3 EXIT selection; broad current-market scans remain blocked
without an identifiable real portfolio and cost basis.

Saved FINAL ChartSage strategies can also be duplicated into an independent,
unsaved copy, renamed, modified and saved under a new name. The original
strategy remains unchanged, and duplicated manual-mode watchlists receive
separate names to avoid overwriting the original strategy's outputs.

For a Latest backtest, FINAL ChartSage anchors the evaluation to the newest
verified official Bhavcopy session available locally. If the current session's
official file has not yet been published, the GUI and workbook show the
effective official price date instead of redownloading years of history for
every stock or silently using a provisional candle.

During a history run, the progress area reports the active phase and its item
count: source and membership preparation, cached-price checks or refreshes,
per-rule indicator evaluation, match ranking, Paper Backtest reference data,
final BUY/EXIT combination and summary construction.

Within one FINAL ChartSage history run, each stock's maximum required adjusted
history is loaded once and safely sliced back to every rule's original warm-up
boundary. Missing maximum-range coverage automatically falls back to the
existing exact-range load/download path.

Ranking-enabled Backtest History templates with a finite Top-N limit first
select each date's ranked winners and then construct the detailed evidence rows
only for those winners. Templates without ranking, and ranking templates set to
retain all matches, continue through the original evidence-building path.

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

Use `create_important_backup.py` for a complete restorable local backup. Its
verified recovery inventory includes user-created scanner templates and rule
trees, ChartSage and FINAL ChartSage watchlists, FINAL strategies, custom rule
examples, stock-alert rules/history, paper-trading state, application data,
historical data, and the required packaging runtime. Copy the completed backup
folder to an external drive or encrypted cloud location; a backup left only on
the same SSD does not protect against that SSD failing. Windows Credential
Manager secrets are intentionally not exported and must be configured again
on a restored PC. Regenerable Bhavcopy market data is intentionally omitted,
including `bhavcopy_history.db`, `nse_bhavcopy_archive`, and
`bhavcopy_snapshots`. After restoration, run ChartSageBhavcopy to download the
required market history again before using scans that depend on it. Use
`build_portable_bundle.py` when an explicitly requested, validated portable
Windows distribution is required.

For a full disaster-recovery and continued-development copy on a USB/external
drive, run:

```powershell
python backup_complete_project_to_usb.py
```

Select a folder on a different drive and type `BACKUP` when prompted. This
larger backup includes the complete working project, Git history, user data,
all SQLite databases, Bhavcopy databases/archives/snapshots, build and packaging
assets, distributions, and reports. It transactionally copies SQLite files,
verifies SHA-256 checksums, and writes `BACKUP_COMPLETE.txt` only after
verification succeeds. Redundant prior backup folders and transient caches,
locks, and SQLite sidecars are intentionally omitted. System installations and
Windows Credential Manager secrets cannot be copied; the restore instructions
inside each backup explain what must be installed or configured on a new PC.

## Updating GitHub after future changes

Run the automatic safety helper from PowerShell in this folder:

```powershell
python update_github.py
```

The helper detects and stages Git-eligible source changes, displays the selected
files and summary, checks for likely private data, generates an appropriate
conventional commit message, creates and verifies the complete recovery backup,
then commits and pushes automatically. A custom commit message can still be
supplied as its optional command-line argument. It stops without committing if
there are pre-existing staged files, suspected credentials, or any backup or
Git error. It respects `.gitignore`. GitHub still receives only safe
source-controlled files; private/live operational data remains in the separate
recovery backup, which must be copied off the SSD.

## Safety

The FINAL ChartSage application provides guidance and historical paper replay;
it does not place Zerodha orders. Each user must configure their own broker and
Telegram credentials locally. Secrets are never intended to be committed.
