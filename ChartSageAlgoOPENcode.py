"""ChartSage rule-first stock scanner.

The scanner engine, cache and template store deliberately have no dependency on
the widgets below them.  This makes a scan reproducible from one JSON setup and
keeps the UI thread free while downloads and indicator calculations run.
"""

from __future__ import annotations

import copy
import html
import json
import math
import os
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time as _time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timezone, timedelta
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any, Callable, Iterable
from zipfile import ZipFile

import numpy as np
import pandas as pd
import requests
import keyring  # type: ignore
from nse_calendar import NSETradingCalendar, fetch_official_nse_payloads
from tickertape_intraday import (
    ORIGINAL_PRICE_MODE,
    TICKERTAPE_PRICE_MODE,
    TICKERTAPE_SOURCE,
    ensure_recent_bhavcopy,
    fetch_intraday_quotes,
    get_session_price_mode,
    session_price_mode_label,
    set_session_price_mode,
    should_fetch_intraday_quotes,
)
from startup_market_maintenance import (
    run_startup_maintenance_dialog,
    show_startup_maintenance_summary,
)
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# Configure Playwright bundled browsers path for frozen executable and standalone distribution
_app_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
_bundled_browsers = _app_dir / "playwright-browsers"
if _bundled_browsers.exists():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_bundled_browsers)
else:
    _user_pw = Path.home() / "AppData" / "Local" / "ms-playwright"
    if _user_pw.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_user_pw)
from PySide6.QtCore import QDate, QObject, QPoint, QThread, QTime, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QToolTip,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


def get_ist_now() -> datetime:
    """Return the current time in India (IST) as an offset-naive datetime."""
    utc_now = datetime.now(timezone.utc)
    ist_now = utc_now + timedelta(hours=5, minutes=30)
    return ist_now.replace(tzinfo=None)


APP_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
_TESTING_MARKER = bool(os.environ.get("CHARTSAGE_TESTING", "").strip())
_data_dir_override = os.environ.get("CHARTSAGE_DATA_DIR", "").strip()
if _TESTING_MARKER and _data_dir_override:
    # Private escape hatch for automated tests only; a normal run never honors it.
    DATA_DIR = Path(_data_dir_override).expanduser().resolve()
else:
    # Source and frozen builds are both deliberately portable: the scanner and
    # feeder share the database, archive and state files beside their code/EXEs.
    # This also ensures a copied ChartSageSuite uses its bundled Bhavcopy history
    # instead of silently creating a separate per-user cache elsewhere.
    DATA_DIR = APP_DIR

PRICE_CACHE_DB = DATA_DIR / "price_cache.db"
BHAVCOPY_HISTORY_DB = DATA_DIR / "bhavcopy_history.db"
TEMPLATE_FILE = DATA_DIR / "scanner_templates.json"
TELEGRAM_SETTINGS_FILE = DATA_DIR / "telegram_settings.json"
TELEGRAM_ALERT_STATE_FILE = DATA_DIR / "telegram_alert_state.json"
WATCHLISTS_FILE = DATA_DIR / "watchlists.json"
APP_SETTINGS_FILE = DATA_DIR / "app_settings.json"
NSE_SYMBOLS_CACHE_FILE = DATA_DIR / "nse_symbols_cache.json"
NSE_SECTORS_CACHE_FILE = DATA_DIR / "nse_sectors_cache.json"
NSE_CALENDAR_FILE = DATA_DIR / "nse_trading_calendar.json"
CUSTOM_FILTER_EXAMPLES_FILE = DATA_DIR / "custom_filter_examples.json"
BROKER_LOGINS_FILE = DATA_DIR / "broker_logins.json"
HOLDINGS_DETAILS_FILE = DATA_DIR / "holdings_details.json"
ZERODHA_SESSION_FILE = DATA_DIR / "zerodha_session.json"
EXECUTED_ORDERS_FILE = DATA_DIR / "today_executed_orders.json"
PAPER_TRADES_FILE = DATA_DIR / "paper_trades.json"
PAPER_TRADES_DB = DATA_DIR / "paper_trades.db"
_BACKGROUND_WORKERS: set[QThread] = set()
_HOLDINGS_STORE_LOCK = threading.RLock()
_ORDER_SUBMISSION_LOCK = threading.RLock()
_PAPER_STORE_LOCK = threading.RLock()
_NSE_BHAVCOPY_LOCK = threading.RLock()
_NSE_BHAVCOPY_MEMORY: dict[date, tuple[datetime, dict[str, dict[str, float]]]] = {}
_OFFICIAL_HISTORY_LOCK = threading.RLock()
_OFFICIAL_HISTORY_COVERAGE_CACHE: dict[tuple[int, str, str], bool] = {}

NSE_BHAVCOPY_RETRY_MINUTES = 5
NSE_BHAVCOPY_URL = (
    "https://nsearchives.nseindia.com/content/cm/"
    "BhavCopy_NSE_CM_0_0_0_{trade_date}_F_0000.csv.zip"
)

PAPER_STARTING_CASH = 1_000_000.0
PAPER_FEE_PCT = 0.10
PAPER_MIN_STARTING_CASH = 1_000.0

_LEGACY_STATE_FILENAMES = (
    "price_cache.db",
    "scanner_templates.json",
    "telegram_settings.json",
    "telegram_alert_state.json",
    "watchlists.json",
    "app_settings.json",
    "nse_symbols_cache.json",
    "nse_sectors_cache.json",
    "nse_trading_calendar.json",
    "custom_filter_examples.json",
    "broker_logins.json",
    "holdings_details.json",
    "zerodha_session.json",
    "today_executed_orders.json",
    "paper_trades.json",
    "paper_trades.db",
)
_HOME_MARKER_FILENAME = "_chartsage_home.json"

SEED_FILENAMES = (
    "scanner_templates.json",
    "watchlists.json",
    "nse_symbols_cache.json",
    "nse_sectors_cache.json",
    "nse_trading_calendar.json",
)


def _legacy_user_data_dir() -> Path:
    """The pre-folder-lock per-user data directory, used for one-time adoption."""
    if os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "ChartSageAlgo"
    return Path.home() / ".local" / "share" / "ChartSageAlgo"


def _default_state_for(filename: str) -> Any:
    """Schema-correct empty payload for a state file, or None to skip creation."""
    if filename == "scanner_templates.json":
        return TemplateStore._empty_state()
    if filename == "broker_logins.json":
        return {}
    if filename == "custom_filter_examples.json":
        return []
    if filename == "watchlists.json":
        return {}
    if filename == "app_settings.json":
        return AppSettingsStore.defaults()
    return None


def _seed_from_bundled(data_dir: Path, seed_dir: Path) -> list[str]:
    """Copy bundled seed files to DATA_DIR on first run when files don't exist."""
    if not seed_dir.is_dir():
        return []
    created: list[str] = []
    for filename in SEED_FILENAMES:
        target = data_dir / filename
        if target.exists():
            continue
        source = seed_dir / filename
        if not source.exists():
            continue
        shutil.copy2(source, target)
        created.append(filename)
    return created


def _ensure_default_state_files(data_dir: Path) -> list[str]:
    """Create missing state files so the app never needs to look outside data_dir."""
    created: list[str] = []
    for filename in (
        "scanner_templates.json",
        "broker_logins.json",
        "custom_filter_examples.json",
        "watchlists.json",
        "app_settings.json",
    ):
        target = data_dir / filename
        if target.exists():
            continue
        payload = _default_state_for(filename)
        if payload is None:
            continue
        try:
            atomic_write_json(target, payload)
            created.append(filename)
        except OSError:
            pass
    return created


def _is_newer(source: Path, target: Path) -> bool:
    try:
        return source.stat().st_mtime > target.stat().st_mtime
    except OSError:
        return False


def _adopt_file(source: Path, target: Path, report: list[str], filename: str) -> None:
    if target.exists():
        backup = target.with_name(
            f"{target.name}.superseded_{datetime.now():%Y%m%d_%H%M%S}"
        )
        try:
            shutil.copy2(target, backup)
            report.append(f"Backed up the older {filename} as {backup.name}.")
        except OSError:
            pass
    fd, temp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".migrating", dir=target.parent
    )
    os.close(fd)
    try:
        shutil.copy2(source, temp_name)
        os.replace(temp_name, target)
        report.append(f"Adopted a newer {filename} from the previous data folder.")
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _merge_broker_accounts(source: Path, target: Path) -> list[str]:
    """Union-merge accounts from a legacy broker_logins.json into the local one."""
    try:
        legacy = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(legacy, dict):
            return []
        if not target.exists():
            report: list[str] = []
            _adopt_file(source, target, report, "broker_logins.json")
            return report
        local = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(local, dict):
            return []
    except (OSError, json.JSONDecodeError):
        return []
    recovered: list[str] = []
    changed = False
    for broker, accounts in legacy.items():
        if not isinstance(accounts, list):
            continue
        local_accounts = local.setdefault(broker, [])
        known = {
            str(a.get("user_id", "")).strip().upper(): a
            for a in local_accounts
            if isinstance(a, dict) and str(a.get("user_id", "")).strip()
        }
        for account in accounts:
            if not isinstance(account, dict):
                continue
            user_id = str(account.get("user_id", "")).strip().upper()
            if not user_id:
                continue
            existing = known.get(user_id)
            if existing is None:
                local_accounts.append(dict(account))
                known[user_id] = local_accounts[-1]
                recovered.append(f"{broker}:{user_id}")
                changed = True
            elif str(account.get("token_updated_at", "")) > str(
                existing.get("token_updated_at", "")
            ):
                existing.update(dict(account))
                changed = True
    if changed:
        atomic_write_json(target, local)
    if not recovered:
        return []
    return [
        "Recovered broker account(s) from the previous data folder: "
        + ", ".join(sorted(recovered))
        + "."
    ]


def reconcile_state_files(
    data_dir: Path,
    legacy_dirs: Iterable[Path] = (),
) -> list[str]:
    """Ensure `data_dir` is a complete, self-contained home for the app.

    Verifies the folder exists and is writable, creates missing schema-correct
    state files, and on the first launch of a copy (no ``_chartsage_home.json``
    marker yet) adopts legacy state from `legacy_dirs` exactly once: broker
    accounts are union-merged by (broker, user_id); every other file is adopted
    when it is newer than the local copy, with the local copy preserved as a
    ``.superseded_<timestamp>`` backup. After the marker is written the app
    never reads `legacy_dirs` again, so every copy is fully self-contained.

    Returns a list of human-readable notes for the startup report.
    """
    data_dir = Path(data_dir)
    report: list[str] = []
    if not data_dir.is_dir():
        data_dir.mkdir(parents=True, exist_ok=True)
    probe = data_dir / f"._chartsage_write_probe_{os.getpid()}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(
            f"Data folder {data_dir} is not writable: {exc}. The app must "
            "live and work only with the files in its own folder."
        ) from exc

    home_marker = data_dir / _HOME_MARKER_FILENAME
    need_adoption = not home_marker.exists()
    if need_adoption:
        for legacy_dir in legacy_dirs:
            legacy_dir = Path(legacy_dir)
            if legacy_dir.resolve() == data_dir.resolve() or not legacy_dir.is_dir():
                continue
            for filename in _LEGACY_STATE_FILENAMES:
                source = legacy_dir / filename
                if not source.is_file():
                    continue
                target = data_dir / filename
                if filename == "broker_logins.json":
                    report.extend(_merge_broker_accounts(source, target))
                elif not target.exists() or _is_newer(source, target):
                    _adopt_file(source, target, report, filename)
        atomic_write_json(
            home_marker,
            {
                "home": str(data_dir),
                "adopted_at_ist": get_ist_now().isoformat(),
            },
        )

    created: list[str] = []
    if getattr(sys, "frozen", False):
        seed_dir = Path(sys._MEIPASS) / "seed_data"
        created.extend(_seed_from_bundled(data_dir, seed_dir))
    created.extend(_ensure_default_state_files(data_dir))
    if created:
        report.append("Created missing state file(s): " + ", ".join(created) + ".")
    return report
TICKER_ALIASES = {
    "SHRIRAMFIN": "SRTRANSFIN",
    "LTF": "L&TFH",
}
AUTOSAVE_NAME = "__autosave__"
TEMPLATE_SCHEMA_VERSION = 1
TELEGRAM_SETTINGS_VERSION = 2
TELEGRAM_ALERT_STATE_VERSION = 1
TELEGRAM_KEYRING_SERVICE = "ChartInkTelegramScanner"
BROKER_KEYRING_SERVICE = "ChartSageBrokerLogins"

class CustomSnippetsStore:
    """Manages custom saved user rule snippets in custom_filter_examples.json."""

    def __init__(self, path: Path = CUSTOM_FILTER_EXAMPLES_FILE):
        self.path = Path(path)

    def load_snippets(self) -> list[str]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                return [str(item).strip() for item in payload if str(item).strip()]
        except (OSError, json.JSONDecodeError):
            pass
        return []

    def save_snippet(self, text: str) -> bool:
        clean = str(text).strip()
        if not clean:
            return False
        snippets = self.load_snippets()
        if clean not in snippets:
            snippets.insert(0, clean)
            atomic_write_json(self.path, snippets)
            return True
        return False

    def delete_snippet(self, text: str) -> bool:
        clean = str(text).strip()
        snippets = self.load_snippets()
        if clean in snippets:
            snippets.remove(clean)
            atomic_write_json(self.path, snippets)
            return True
        return False
TELEGRAM_KEYRING_ACCOUNT = "bot_token"
TELEGRAM_TOKEN_ENV = "CHARTINK_TELEGRAM_BOT_TOKEN"
TELEGRAM_MESSAGE_LIMIT = 3900
TELEGRAM_ALERT_MODES = (
    ("new", "New matches only (recommended)"),
    ("new_removed", "New and removed matches"),
    ("full", "Complete list after every scan"),
)

MACD_FIELDS = ("MACD Line", "MACD Signal", "MACD Histogram")
HIGH_52_WEEK_FIELDS = ("52 Week High", "52 Week High Distance %")
FIELD_DISPLAY_NAMES = {
    "52 Week High": "Prior High",
    "52 Week High Distance %": "Prior High Distance %",
}
ALL_AVAILABLE_HISTORY_UNIT = "All available history since 2014"
AVAILABLE_HISTORY_START_DATE = date(2014, 1, 1)
LOOKBACK_HIGH_UNITS = ("Days", "Weeks", "Months", "Years", ALL_AVAILABLE_HISTORY_UNIT)
LOOKBACK_HIGH_BARS_PER_UNIT = {"Days": 1, "Weeks": 5, "Months": 21, "Years": 252}
MAX_LOOKBACK_HIGH_BARS = 252 * 30
PNL_FIELDS = ("Profit %", "Loss %", "Stop Loss %")
FIELDS = (
    "Open",
    "High",
    "Low",
    "Close",
    "Volume",
    "RSI",
    "SMA",
    "EMA",
    "ADX",
    "OBV",
    "Supertrend",
    *MACD_FIELDS,
    *HIGH_52_WEEK_FIELDS,
    *PNL_FIELDS,
)
LEGACY_HIGH_52_WEEK_RANGE_OPERATOR = "within 52-week high range"
HIGH_52_WEEK_RANGE_OPERATOR = "within prior-high range"
OPERATORS = (
    ">",
    ">=",
    "<",
    "<=",
    "=",
    "!=",
    "crosses above",
    "crosses below",
    HIGH_52_WEEK_RANGE_OPERATOR,
)
VALID_OPERATORS = (*OPERATORS, LEGACY_HIGH_52_WEEK_RANGE_OPERATOR)
TIMEFRAMES = ("D", "W", "M")
NSE_MARKET_OPEN = time(9, 15)
NSE_MARKET_CLOSE = time(15, 30)
NSE_CALENDAR = NSETradingCalendar(NSE_CALENDAR_FILE)
REFRESH_MINUTE_CHOICES = (1, 2, 5, 10, 15, 20, 30, 45, 60)
DEFAULT_REFRESH_MINUTES = 5
SERIAL_COLUMN = "S.No."
DATA_SOURCE_COLUMN = "Data Source"
PRICE_BASIS_COLUMN = "Price Basis"
DATA_TIME_COLUMN = "Yahoo Data Time (IST)"
DATA_AGE_COLUMN = "Data Age"
PERCENT_CHANGE_COLUMN = "% Change"
TIMING_RESULT_COLUMNS = (
    DATA_SOURCE_COLUMN,
    PRICE_BASIS_COLUMN,
    DATA_TIME_COLUMN,
    DATA_AGE_COLUMN,
)
RSI_D_COLUMN = "RSI(14)D"
RSI_1DAY_AGO_COLUMN = "RSI(14)1DayAgo"
COMPOSITE_RSI_MOMENTUM_COLUMN = "Composite RSI Momentum"
ALWAYS_AVAILABLE_INDICATOR_COLUMNS = (RSI_D_COLUMN, RSI_1DAY_AGO_COLUMN)

HOLDING_QTY_COLUMN = "Holding Qty"
BUY_PRICE_COLUMN = "Buy Price"
PNL_PCT_COLUMN = "P&L %"
PNL_AMT_COLUMN = "P&L Amount"
HOLDING_RESULT_COLUMNS = (
    HOLDING_QTY_COLUMN,
    BUY_PRICE_COLUMN,
    PNL_PCT_COLUMN,
    PNL_AMT_COLUMN,
)
ACTION_COLUMN = "Action"

ALL_RESULT_COLUMNS = (
    SERIAL_COLUMN,
    "Symbol",
    ACTION_COLUMN,
    PERCENT_CHANGE_COLUMN,
    "Universe",
    "Sector",
    "As Of",
    *TIMING_RESULT_COLUMNS,
    "Open",
    "High",
    "Low",
    "Close",
    *ALWAYS_AVAILABLE_INDICATOR_COLUMNS,
    "Volume",
    *HOLDING_RESULT_COLUMNS,
    "Evidence",
)

FILTER_EXAMPLE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Price and OHLC",
        (
            "Daily close above 100",
            "Daily close below daily open",
            "Daily high >= 250",
            "Daily low <= 90",
            "Daily close>=daily SMA 20",
            "RSI>=60",
            "Daily close equal to daily open",
            "Daily close not equal to daily open",
            "Yesterday close above 100",
            "Close 3 candles ago below daily SMA 20",
        ),
    ),
    (
        "Price and moving-average crossovers",
        (
            "Daily close crosses above daily SMA 20",
            "Daily close crossed below daily EMA 50",
            "SMA 20 crosses above EMA 50",
            "Weekly close crosses above weekly EMA 10",
        ),
    ),
    (
        "RSI",
        (
            "RSI 14 above 60",
            "RSI above 60",
            "RSI 14 below 40",
            "RSI 14 crosses above 60",
            "RSI 14 crossed below 40",
            "RSI 14 one day ago above 60",
            "Weekly RSI 14 above 60",
        ),
    ),
    (
        "SMA and EMA",
        (
            "Close above SMA 20",
            "Close below EMA 50",
            "Daily SMA 20 above daily SMA 50",
            "Daily EMA 20 crosses above daily EMA 50",
            "Weekly close above weekly SMA 20",
            "Monthly close above monthly EMA 10",
        ),
    ),
    (
        "ADX",
        (
            "ADX above 25",
            "ADX(14,14) crosses above 25",
            "ADX 14 10 below 20",
            "Weekly ADX(14,14) above 25",
            "ADX(14,14) one day ago above 20",
        ),
    ),
    (
        "OBV",
        (
            "OBV above OBV one day ago",
            "OBV below 0",
            "OBV crosses above 0",
            "Weekly OBV above weekly OBV previous week",
        ),
    ),
    (
        "MACD",
        (
            "MACD histogram(12,26,9) above 0",
            "MACD histogram(12,26,9) crossed above 0",
            "Daily MACD line crosses above daily MACD signal",
            "Daily MACD line below daily MACD signal",
            "Weekly MACD line crosses below weekly MACD signal",
        ),
    ),
    (
        "Supertrend",
        (
            "Close above Supertrend(10,3)",
            "Close crosses above Supertrend(10,3)",
            "Close crossed below Supertrend(10,3)",
        ),
    ),
    (
        "Volume",
        (
            "Volume above 1000000",
            "Today volume above volume one day ago",
            "Weekly volume above weekly volume previous week",
        ),
    ),
    (
        "Configurable prior high",
        (
            "Close within 2% above or below the 52 week high",
            "Close within 2% above or below the 2 year high",
            "Close within 2% above or below the available history high since 2014",
            "Daily Close is between 3% above and 1.5% below the available history high since 2014",
            "Close near the available history high since 2014 by 3.5%",
            "Close at the available history high since 2014",
            "Close crosses above the available history high since 2014",
            "Close within 3% above or below the 90 day high",
            "Daily Close is between 3% above and 1.5% below the prior 18 month high",
            "Daily Close is between 3% above and 1.5% below the prior 52-week high",
            "Close near the 52-week high by 3.5%",
            "Close within 4% above or below the 52 week high",
            "Close at 52 week high",
            "Close within 2% above or below the 52 week high one day ago",
            "Close crosses above 52 week high",
        ),
    ),
    (
        "Paper holdings and portfolio Take Profit / Stop Loss",
        (
            "Profit % >= 7%",
            "Loss % >= 5%",
            "Profit % <= -5%",
            "Stop Loss % <= -5%",
            "Stop Loss % <= 5%",
        ),
    ),
    (
        "Moving Averages & Multipliers (Spread & Percent Rules)",
        (
            "Daily EMA 10 > Daily EMA 20 * 1.001",
            "Daily Ema(Daily Close, 10) Greater than Daily Ema(Daily Close, 20) * Number 1.01",
            "Daily Close > Daily SMA 20 * 1.02",
            "Daily Close < Daily SMA 20 * 0.98",
        ),
    ),
)


def filter_examples_text(custom_snippets: list[str] | None = None) -> str:
    sections = [
        "FILTER EXAMPLES",
        "",
        "Select one complete sentence, then click “Use selected example” or copy and paste it into the English rule box.",
        "Add one rule at a time. After parsing, confirm or edit its structured values.",
        "",
    ]
    if custom_snippets:
        sections.extend(["MY SAVED SNIPPETS", *custom_snippets, ""])
    sections.extend(
        [
            "Grammar you can vary:",
            "• Comparisons: above, below, equal to, not equal to, >=, <=, =, !=, crosses above, crosses below",
            "• Timeframes: daily, weekly, monthly",
            "• Offsets: today, yesterday, one day ago, 3 candles ago, previous week, previous month",
            "• RSI defaults to period 14 when its period is omitted.",
            "• SMA and EMA use Close as their source.",
            "• ADX defaults to DI length 14 and ADX smoothing 14.",
            "• OBV is cumulative signed volume and takes no period.",
            "• MACD defaults to (12,26,9) when its parameters are omitted.",
            "• Prior-high duration can use days, weeks, months, or years (for example, 90 day high or 4 year high).",
            "• “All available history since 2014” uses every available daily candle from 2014 onward.",
            "• “Near 52 week high” defaults to 2% above and 2% below; use “between A% above and B% below” for asymmetric limits.",
        ]
    )
    for heading, examples in FILTER_EXAMPLE_GROUPS:
        sections.extend(("", heading.upper(), *examples))
    return "\n".join(sections)


UNIVERSES: dict[str, Any] = {
    "NSE ALL": "https://archives.nseindia.com/content/equities/EQUITY_L.csv",
    "PSU RAILWAY": ["IRFC", "RVNL", "IRCON", "RAILTEL", "IRCTC", "CONCOR", "BEML", "TITAGARH", "TEXRAIL", "JWL"],
    "NIFTY 50": "https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
    "NIFTY NEXT 50": "https://archives.nseindia.com/content/indices/ind_niftynext50list.csv",
    "NIFTY 100": "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
    "NIFTY 200": "https://archives.nseindia.com/content/indices/ind_nifty200list.csv",
    "NIFTY 500": "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
    "NIFTY MIDCAP 150": "https://archives.nseindia.com/content/indices/ind_niftymidcap150list.csv",
    "NIFTY SMALLCAP 250": "https://archives.nseindia.com/content/indices/ind_niftysmallcap250list.csv",
    "NIFTY AUTO": "https://archives.nseindia.com/content/indices/ind_niftyautolist.csv",
    "NIFTY BANK": "https://archives.nseindia.com/content/indices/ind_niftybanklist.csv",
    "NIFTY FINANCIAL SERVICES": "https://archives.nseindia.com/content/indices/ind_niftyfinancelist.csv",
    "NIFTY FMCG": "https://archives.nseindia.com/content/indices/ind_niftyfmcglist.csv",
    "NIFTY IT": "https://archives.nseindia.com/content/indices/ind_niftyitlist.csv",
    "NIFTY MEDIA": "https://archives.nseindia.com/content/indices/ind_niftymedialist.csv",
    "NIFTY METAL": "https://archives.nseindia.com/content/indices/ind_niftymetallist.csv",
    "NIFTY PHARMA": "https://archives.nseindia.com/content/indices/ind_niftypharmalist.csv",
    "NIFTY PSU BANK": "https://archives.nseindia.com/content/indices/ind_niftypsubanklist.csv",
    "NIFTY REALTY": "https://archives.nseindia.com/content/indices/ind_niftyrealtylist.csv",
    "NIFTY ENERGY": "https://archives.nseindia.com/content/indices/ind_niftyenergylist.csv",
    "NIFTY INFRASTRUCTURE": "https://archives.nseindia.com/content/indices/ind_niftyinfralist.csv",
    "NIFTY CONSUMPTION": "https://archives.nseindia.com/content/indices/ind_niftyconsumptionlist.csv",
    "NIFTY COMMODITIES": "https://archives.nseindia.com/content/indices/ind_niftycommoditieslist.csv",
    "NIFTY HEALTHCARE": "https://archives.nseindia.com/content/indices/ind_niftyhealthcarelist.csv",
}


def value_operand(value: float = 0.0) -> dict[str, Any]:
    return {"kind": "value", "value": float(value)}


def field_operand(
    name: str = "Close",
    timeframe: str = "D",
    offset: int = 0,
    period: int = 14,
    multiplier: float | None = None,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
    adx_smoothing: int = 14,
    lookback_count: int | None = None,
    lookback_unit: str | None = None,
) -> dict[str, Any]:
    default_mult = 3.0 if name == "Supertrend" else 1.0
    mult = float(default_mult if multiplier is None else multiplier)
    result = {
        "kind": "field",
        "name": name,
        "timeframe": timeframe,
        "offset": int(offset),
        "period": int(period),
        "multiplier": mult,
        "fast_period": int(fast_period),
        "slow_period": int(slow_period),
        "signal_period": int(signal_period),
        "adx_smoothing": int(adx_smoothing),
    }
    if name in HIGH_52_WEEK_FIELDS and lookback_count is not None:
        result["lookback_count"] = int(lookback_count)
        result["lookback_unit"] = str(lookback_unit or "Years")
    return result


def lookback_high_parameters(operand: dict[str, Any]) -> tuple[int, str, int]:
    """Return display count/unit and daily bars for a configurable prior high.

    Old templates intentionally contain neither metadata field. They retain the
    original definition of 252 completed daily candles and display as 52 Weeks.
    """
    if "lookback_count" not in operand:
        return 52, "Weeks", 252
    count = int(operand.get("lookback_count", 1))
    raw_unit = str(operand.get("lookback_unit", "Years")).strip()
    unit = (
        ALL_AVAILABLE_HISTORY_UNIT
        if raw_unit.casefold() == ALL_AVAILABLE_HISTORY_UNIT.casefold()
        else raw_unit.title()
    )
    if unit == ALL_AVAILABLE_HISTORY_UNIT:
        return 1, unit, 0
    factor = LOOKBACK_HIGH_BARS_PER_UNIT.get(unit)
    if factor is None:
        raise ValueError(f"Unknown high-lookback unit: {unit}")
    return count, unit, count * factor


def lookback_high_name(operand: dict[str, Any]) -> str:
    count, unit, _bars = lookback_high_parameters(operand)
    if unit == ALL_AVAILABLE_HISTORY_UNIT:
        suffix = " Distance %" if operand.get("name") == "52 Week High Distance %" else ""
        return f"Available-History High (Since 2014){suffix}"
    # The duration is adjectival here: "2 Year High", not "2 Years High".
    unit_label = unit[:-1]
    suffix = " Distance %" if operand.get("name") == "52 Week High Distance %" else ""
    return f"{count} {unit_label} High{suffix}"


def matching_lookback_high_operand(
    source: dict[str, Any], name: str = "52 Week High"
) -> dict[str, Any]:
    """Create the companion high field without losing its duration metadata."""
    return field_operand(
        name,
        "D",
        int(source.get("offset", 0)),
        lookback_count=source.get("lookback_count"),
        lookback_unit=source.get("lookback_unit"),
    )


def rule_node(left: dict[str, Any] | None = None, operator: str = ">", right: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "rule",
        "enabled": True,
        "left": left or field_operand(),
        "operator": operator,
        "right": right or value_operand(),
    }


def high_52_week_range_rule(
    above_percent: float = 2.0,
    below_percent: float = 2.0,
    offset: int = 0,
    lookback_count: int | None = None,
    lookback_unit: str | None = None,
) -> dict[str, Any]:
    node = rule_node(
        field_operand("Close", "D", offset),
        HIGH_52_WEEK_RANGE_OPERATOR,
        field_operand(
            "52 Week High", "D", offset, lookback_count=lookback_count,
            lookback_unit=lookback_unit,
        ),
    )
    node["above_percent"] = float(above_percent)
    node["below_percent"] = float(below_percent)
    return node


def is_legacy_52_week_range_rule(node: dict[str, Any]) -> bool:
    return (
        node.get("type") == "rule"
        and node.get("operator") == "<="
        and node.get("left", {}).get("kind") == "field"
        and node["left"].get("name") == "52 Week High Distance %"
        and node.get("right", {}).get("kind") == "value"
    )


def is_52_week_range_rule(node: dict[str, Any]) -> bool:
    return node.get("type") == "rule" and (
        node.get("operator") in {
            HIGH_52_WEEK_RANGE_OPERATOR,
            LEGACY_HIGH_52_WEEK_RANGE_OPERATOR,
        }
        or is_legacy_52_week_range_rule(node)
    )


def is_high_range_operator(operator: Any) -> bool:
    return operator in {
        HIGH_52_WEEK_RANGE_OPERATOR,
        LEGACY_HIGH_52_WEEK_RANGE_OPERATOR,
    }


def range_rule_limits(node: dict[str, Any]) -> tuple[float, float, int]:
    if is_legacy_52_week_range_rule(node):
        tolerance = float(node["right"]["value"])
        return tolerance, tolerance, int(node["left"].get("offset", 0))
    return (
        float(node.get("above_percent", 2.0)),
        float(node.get("below_percent", 2.0)),
        int(node["left"].get("offset", 0)),
    )


def group_node(op: str = "AND", children: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"type": "group", "enabled": True, "op": op, "children": children or []}


def node_enabled(node: dict[str, Any]) -> bool:
    """Older saved templates have no flag and remain enabled by default."""
    return bool(node.get("enabled", True))


def has_active_rule(node: dict[str, Any]) -> bool:
    if not node_enabled(node):
        return False
    if node.get("type") == "rule":
        return True
    return any(has_active_rule(child) for child in node.get("children", []))


def default_setup() -> dict[str, Any]:
    return {
        "version": 1,
        "universes": ["NIFTY 500"],
        "custom_symbols": "",
        "expression": group_node("AND"),
        "as_of_mode": "latest",
        "as_of_date": date.today().isoformat(),
        "adjusted": True,
        "include_current": True,
        "auto_run": True,
        "auto_run_start_time": "09:15",
        "auto_run_end_time": "15:30",
        "auto_run_trading_days_only": True,
        "telegram_alerts_enabled": True,
        "telegram_alert_mode": "full",
        "telegram_auto_reset_daily": True,
        "algo_auto_order_enabled": False,
        "algo_auto_order_cb": False,
        "algo_paper_account": "",
        "algo_trade_action": "BUY (Entry Order - Budget Based)",
        "algo_sell_mode": "100% (Sell Full Holding Quantity)",
        "algo_sell_pct": 50.0,
        "algo_capital_mode": "Dynamic Allocation (Live Cash ÷ Max Position Slots)",
        "algo_max_slots": 5,
        "algo_capital_per_stock": 5000.0,
        "algo_product": "CNC (Delivery)",
        "algo_order_type": "LIMIT",
        "algo_limit_buffer_pct": 0.50,
        "paper_auto_watchlist": True,
        "ranking_enabled": False,
        "ranking_rsi_periods": [22, 44, 66],
        "ranking_direction": "DESC",
        "ranking_top_n": 0,
        "ranking_watchlist_enabled": False,
        "ranking_watchlist_name": "",
        "is_final_template": False,
        "finalset_op": "AND",
        "finalset_k": 1,
        "finalset_members": [],
        "history_years": 2,
        "result_columns": list(ALL_RESULT_COLUMNS),
        "result_column_order": list(ALL_RESULT_COLUMNS),
    }


HOLDINGS_UNIVERSE_PREFIX = "💼 ZERODHA"


def is_algo_template_setup(name: str, setup: dict[str, Any]) -> bool:
    """Return whether a template has explicit ALGO execution configuration.

    Descriptive words in a template name do not make an ordinary scan an ALGO
    strategy. This keeps rules such as "RSI ... HEATED" in General Scans until
    the user explicitly promotes or configures them for ALGO execution.
    """
    try:
        is_algo_tpl = bool(setup.get("is_algo_template", False))
        target_acc_setting = str(setup.get("algo_target_account", "")).strip().upper()
        has_target_acc = bool(target_acc_setting) and target_acc_setting != "ACTIVE"
        is_auto_cb = bool(setup.get("algo_auto_order_cb", False)) or bool(
            setup.get("algo_auto_order_enabled", False)
        )
        has_tp_sl = bool(setup.get("algo_target_profit_enabled", False)) or bool(
            setup.get("algo_stop_loss_enabled", False)
        )
        return is_algo_tpl or has_target_acc or is_auto_cb or has_tp_sl
    except Exception:
        return False


def is_holdings_universe(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(HOLDINGS_UNIVERSE_PREFIX)


PAPER_WATCHLIST_PREFIX = "PAPER "
PAPER_TAB_INDEX = 2
GUIDE_FEEDER_WATCHLIST_PREFIX = "GUIDE FEEDER "


def strategy_automation_defaults() -> dict[str, Any]:
    return {
        "recipe_name": "MY STRATEGY GUIDE",
        "feeder_templates": [],
        "candidate_templates": [],
        "buy_mode": "AND",
        "buy_k": 1,
        "buy_finalset": "",
        "exit_templates": [],
        "exit_mode": "OR",
        "exit_k": 1,
        "exit_finalset": "",
        "track_model": False,
        "model_account": "",
        "auto_paper_watchlists": True,
        "combined_watchlist_name": "GUIDE AUTOMATION SOURCE",
        "create_individual_watchlists": True,
        "interval_minutes": 15,
        "start_time": "09:15",
        "end_time": "15:30",
        "trading_days_only": True,
        "telegram_enabled": True,
        "automation_enabled": False,
        "technical_adjusted": True,
        "include_current": True,
    }


def normalize_strategy_automation_config(value: Any) -> dict[str, Any]:
    config = strategy_automation_defaults()
    if isinstance(value, dict):
        config.update(copy.deepcopy(value))
    config["recipe_name"] = str(config.get("recipe_name", "") or "").strip()
    config["combined_watchlist_name"] = str(
        config.get("combined_watchlist_name", "") or ""
    ).strip()
    for list_key in ("feeder_templates", "candidate_templates", "exit_templates"):
        config[list_key] = list(
            dict.fromkeys(
                str(name).strip()
                for name in (config.get(list_key, []) or [])
                if str(name).strip()
            )
        )
    try:
        interval = int(config.get("interval_minutes", 15) or 15)
    except (TypeError, ValueError):
        interval = 15
    config["interval_minutes"] = interval if interval in (5, 10, 15, 30, 60) else 15
    for key, fallback in (("start_time", "09:15"), ("end_time", "15:30")):
        text = str(config.get(key, fallback) or fallback)
        config[key] = text if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text) else fallback
    for key in (
        "create_individual_watchlists",
        "trading_days_only",
        "telegram_enabled",
        "technical_adjusted",
        "include_current",
        "automation_enabled",
        "track_model",
        "auto_paper_watchlists",
    ):
        config[key] = bool(config.get(key, strategy_automation_defaults()[key]))
    return config


def guide_feeder_watchlist_name(template_name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9]+", " ", str(template_name)).strip().upper()
    return f"{GUIDE_FEEDER_WATCHLIST_PREFIX}{stem}" if stem else ""


def paper_watchlist_name(template_name: str) -> str:
    """Derive the auto-updated paper watchlist name for a rule template run.

    '52 WEEK HIGH' -> 'PAPER 52WEEKHIGH'. Returns '' when the template has no
    usable name. The marker prefix keeps paper lists distinct from hand-made
    watchlists and from reserved universe names.
    """
    stem = re.sub(r"[^A-Za-z0-9]+", "", str(template_name).strip()).upper()
    return f"{PAPER_WATCHLIST_PREFIX}{stem}" if stem else ""


def auto_update_paper_watchlist(
    template_name: str, symbols: list[str]
) -> str | None:
    """Creates or replaces the PAPER watchlist for a rule template run.

    The list is rebuilt from this run's matching symbols, so a re-run always
    reflects the latest results. Returns the watchlist name, or None when the
    template name cannot form a valid list.
    """
    wl_name = paper_watchlist_name(template_name)
    if not wl_name:
        return None
    clean = sorted(
        {
            str(symbol).strip().upper()
            for symbol in symbols
            if str(symbol).strip()
        }
    )
    store = WatchlistStore()
    watchlists = store.load()
    watchlists[wl_name] = clean
    store.save(watchlists)
    UNIVERSES[wl_name] = clean
    return wl_name


def is_final_template_setup(name: str, setup: dict[str, Any]) -> bool:
    """True for FINAL RULE SET templates that combine member templates by name."""
    try:
        return bool(setup.get("is_final_template", False)) or bool(
            setup.get("finalset_members")
        )
    except Exception:
        return False


def strategy_guide_template_role(name: str, setup: dict[str, Any]) -> str:
    """Return BUY or EXIT for Strategy Guide list filtering.

    An explicit user override wins. Older templates are classified from their
    saved trade action, P&L operands and conservative name heuristics.
    """
    explicit = str(setup.get("strategy_guide_role", "")).strip().upper()
    if explicit in {"BUY", "EXIT"}:
        return explicit
    if "SELL" in str(setup.get("algo_trade_action", "")).upper():
        return "EXIT"
    if expression_has_pnl_rules(setup.get("expression")):
        return "EXIT"
    upper = str(name).strip().upper()
    exit_markers = (
        "[SELL]", " SELL", "EXIT", "WEAK", "FALLING", "SUPER HEATED",
        "SUPERHEATED", "OVERBOUGHT", "STOP LOSS", "STOPLOSS", "PROFIT BOOK",
    )
    return "EXIT" if any(marker in upper for marker in exit_markers) else "BUY"


def strategy_guide_universe_label(setup: dict[str, Any]) -> str:
    universes = [
        str(value).strip() for value in setup.get("universes", [])
        if str(value).strip()
    ]
    if not universes and str(setup.get("custom_symbols", "")).strip():
        return "CUSTOM"
    if not universes:
        return "NO UNIVERSE"
    return universes[0] if len(universes) == 1 else f"{universes[0]} +{len(universes) - 1}"


def finalset_combine(matches: list[bool], op: str, k: int) -> bool:
    """Combine per-member match results for a FINAL RULE SET."""
    if not matches:
        return False
    op = str(op or "AND").strip().upper()
    if op == "OR":
        return any(matches)
    if op.startswith("ANY"):
        try:
            need = max(1, int(k) if k else 1)
        except (TypeError, ValueError):
            need = 1
        return sum(1 for m in matches if m) >= need
    return all(matches)


def validate_finalset_members(
    finalset_name: str,
    members: list[str],
    templates: dict[str, dict[str, Any]],
    _visiting: set[str] | None = None,
) -> None:
    """Validate a FINAL RULE SET's members by name and reject cyclic references."""
    if _visiting is None:
        _visiting = set()
    if finalset_name in _visiting:
        raise ValueError(
            f"FINAL RULE SET '{finalset_name}' references itself through "
            "another FINAL RULE SET."
        )
    visiting = set(_visiting)
    visiting.add(finalset_name)
    for member in members or []:
        member = str(member).strip()
        if not member:
            continue
        if member not in templates:
            raise ValueError(
                f"FINAL RULE SET '{finalset_name}' references template "
                f"'{member}', which no longer exists."
            )
        if is_final_template_setup(member, templates[member]):
            validate_finalset_members(
                member,
                templates[member].get("finalset_members", []),
                templates,
                visiting,
            )


def normalize_general_universes(
    name: str, setup: dict[str, Any]
) -> tuple[dict[str, Any], bool, list[str]]:
    """Strip system-managed Zerodha holdings universes from non-ALGO setups.

    Falls back to NIFTY 500 when stripping leaves the universe list empty.
    Returns ``(setup, changed, removed_universes)``. ALGO setups are untouched.
    """
    if is_algo_template_setup(name, setup) or expression_has_pnl_rules(
        setup.get("expression")
    ):
        return setup, False, []
    unis = list(setup.get("universes", []))
    removed = [u for u in unis if is_holdings_universe(u)]
    if not removed:
        return setup, False, []
    unis = [u for u in unis if not is_holdings_universe(u)]
    if not unis:
        unis = ["NIFTY 500"]
    setup["universes"] = unis
    return setup, True, removed


def validate_operand(operand: Any) -> None:
    if not isinstance(operand, dict) or operand.get("kind") not in {"field", "value"}:
        raise ValueError("Each operand must be a field or numeric value.")
    if operand["kind"] == "value":
        number = float(operand["value"])
        if not math.isfinite(number):
            raise ValueError("Numeric operands must be finite (not NaN or infinity).")
        return
    if operand.get("name") not in FIELDS:
        raise ValueError(f"Unknown field: {operand.get('name')}")
    if operand.get("timeframe") not in TIMEFRAMES:
        raise ValueError("Timeframe must be D, W, or M.")
    if operand["name"] in HIGH_52_WEEK_FIELDS and operand["timeframe"] != "D":
        raise ValueError("52-week-high fields use daily candles.")
    if operand["name"] in HIGH_52_WEEK_FIELDS:
        count, unit, bars = lookback_high_parameters(operand)
        if count < 1:
            raise ValueError("High-lookback duration must be at least 1.")
        if unit not in LOOKBACK_HIGH_UNITS:
            raise ValueError("High-lookback unit must be Days, Weeks, Months, or Years.")
        if unit != ALL_AVAILABLE_HISTORY_UNIT and bars > MAX_LOOKBACK_HIGH_BARS:
            raise ValueError("High-lookback duration cannot exceed 30 trading years.")
    if not 0 <= int(operand.get("offset", -1)) <= 250:
        raise ValueError("Offset must be between 0 and 250.")
    if operand["name"] in {"RSI", "SMA", "EMA", "ADX", "Supertrend"} and not 1 <= int(operand.get("period", 0)) <= 500:
        raise ValueError("Indicator period must be between 1 and 500.")
    if operand["name"] == "ADX" and not 1 <= int(operand.get("adx_smoothing", 14)) <= 500:
        raise ValueError("ADX smoothing period must be between 1 and 500.")
    if operand["name"] == "Supertrend" and float(operand.get("multiplier", 0)) <= 0:
        raise ValueError("Supertrend multiplier must be positive.")
    if operand["name"] in MACD_FIELDS:
        fast = int(operand.get("fast_period", 12))
        slow = int(operand.get("slow_period", 26))
        signal = int(operand.get("signal_period", 9))
        if not all(1 <= value <= 500 for value in (fast, slow, signal)):
            raise ValueError("MACD fast, slow, and signal periods must be between 1 and 500.")
        if fast >= slow:
            raise ValueError("MACD fast period must be smaller than its slow period.")


def validate_ast(node: Any, root: bool = True, allow_empty_groups: bool = False) -> None:
    if not isinstance(node, dict):
        raise ValueError("Expression nodes must be JSON objects.")
    if "enabled" in node and not isinstance(node["enabled"], bool):
        raise ValueError("A rule or group's enabled state must be true or false.")
    if root and not allow_empty_groups and not has_active_rule(node):
        raise ValueError("Enable at least one rule before running the scan.")
    if node.get("type") == "group":
        op = node.get("op")
        any_match = re.fullmatch(r"ANY ([1-9]\d*)", op) if isinstance(op, str) else None
        if op not in {"AND", "OR"} and any_match is None:
            raise ValueError("Groups must use AND, OR, or ANY K.")
        children = node.get("children")
        if not isinstance(children, list):
            raise ValueError("A group needs a children list.")
        if not children and node_enabled(node) and not allow_empty_groups:
            raise ValueError("Every group must contain at least one rule or subgroup.")
        for child in children:
            validate_ast(
                child,
                False,
                allow_empty_groups or not node_enabled(node),
            )
        if any_match and node_enabled(node) and not allow_empty_groups:
            active_children = sum(1 for child in children if has_active_rule(child))
            if int(any_match.group(1)) > active_children:
                raise ValueError("ANY K cannot exceed the number of active child rules or groups.")
        return
    if node.get("type") == "rule":
        if node.get("operator") not in VALID_OPERATORS:
            raise ValueError(f"Unknown comparison: {node.get('operator')}")
        validate_operand(node.get("left"))
        validate_operand(node.get("right"))
        if (
            node["left"].get("kind") == "field"
            and node["left"].get("name") == "Stop Loss %"
            and node["right"].get("kind") == "value"
            and node.get("operator") in {"<", "<=", "="}
            and float(node["right"].get("value", 0)) > 0
        ):
            raise ValueError(
                "Stop Loss % thresholds for <, <=, or = must be zero or negative."
            )
        if is_52_week_range_rule(node):
            above, below, _offset = range_rule_limits(node)
            if not all(math.isfinite(value) and 0 <= value <= 100 for value in (above, below)):
                raise ValueError("High-lookback limits must each be between 0% and 100%.")
        if is_high_range_operator(node.get("operator")):
            left = node["left"]
            right = node["right"]
            if not (
                left.get("kind") == "field"
                and left.get("name") == "Close"
                and left.get("timeframe") == "D"
                and right.get("kind") == "field"
                and right.get("name") == "52 Week High"
                and right.get("timeframe") == "D"
                and int(left.get("offset", 0)) == int(right.get("offset", 0))
            ):
                raise ValueError(
                    "A high-lookback range must compare Daily Close with a prior "
                    "high using the same offset."
                )
        return
    raise ValueError("A node must be a group or rule.")


def operand_text(operand: dict[str, Any]) -> str:
    if operand["kind"] == "value":
        return f"{float(operand['value']):g}"
    name = operand["name"]
    display_name = lookback_high_name(operand) if name in HIGH_52_WEEK_FIELDS else name
    detail = ""
    if name in {"RSI", "SMA", "EMA"}:
        detail = f"({int(operand['period'])})"
    elif name == "ADX":
        detail = (
            f"({int(operand.get('period', 14))},"
            f"{int(operand.get('adx_smoothing', 14))})"
        )
    elif name == "Supertrend":
        detail = f"({int(operand['period'])},{float(operand['multiplier']):g})"
    elif name in MACD_FIELDS:
        detail = (
            f"({int(operand.get('fast_period', 12))},"
            f"{int(operand.get('slow_period', 26))},"
            f"{int(operand.get('signal_period', 9))})"
        )
    suffix = f" {operand['timeframe']}" if name not in PNL_FIELDS else ""
    if int(operand["offset"]):
        suffix += f" offset {int(operand['offset'])}"
    if name != "Supertrend":
        mult = float(operand.get("multiplier", 1.0))
        if mult != 1.0:
            suffix += f" * {mult:g}"
    return display_name + detail + suffix


def normalize_ranking_rsi_periods(value: Any) -> list[int]:
    """Parse a stable, unique list of RSI periods for composite ranking."""
    raw_values: list[Any]
    if isinstance(value, str):
        raw_values = [part for part in re.split(r"[\s,;+]+", value.strip()) if part]
    elif isinstance(value, (list, tuple)):
        raw_values = list(value)
    else:
        raw_values = []
    periods: list[int] = []
    for raw in raw_values:
        try:
            period = int(raw)
        except (TypeError, ValueError):
            continue
        if 1 <= period <= 500 and period not in periods:
            periods.append(period)
        if len(periods) >= 12:
            break
    return periods or [22, 44, 66]


def parse_ranking_rsi_periods_strict(value: Any) -> list[int]:
    if isinstance(value, str):
        raw_values = [part for part in re.split(r"[\s,;+]+", value.strip()) if part]
    elif isinstance(value, (list, tuple)):
        raw_values = list(value)
    else:
        raw_values = []
    if not raw_values:
        raise ValueError("Enter at least two RSI periods, for example 22, 44, 66.")
    periods: list[int] = []
    for raw in raw_values:
        try:
            period = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid RSI period: {raw!r}.") from exc
        if not 1 <= period <= 500:
            raise ValueError("Every RSI ranking period must be between 1 and 500.")
        if period not in periods:
            periods.append(period)
    if len(periods) < 2:
        raise ValueError("Composite RSI ranking requires at least two different periods.")
    if len(periods) > 12:
        raise ValueError("Composite RSI ranking supports at most 12 periods.")
    return periods


def validate_ranking_setup(setup: dict[str, Any]) -> None:
    if not bool(setup.get("ranking_enabled", False)):
        return
    parse_ranking_rsi_periods_strict(
        setup.get("ranking_rsi_periods", [22, 44, 66])
    )
    if bool(setup.get("ranking_watchlist_enabled", False)):
        watchlist_name = str(
            setup.get("ranking_watchlist_name", "") or ""
        ).strip()
        if not watchlist_name:
            raise ValueError(
                "Enter a name for the ranked watchlist, or disable watchlist updating."
            )
        if not re.fullmatch(r"[A-Za-z0-9\s_-]+", watchlist_name):
            raise ValueError(
                "Ranked watchlist names may contain only letters, numbers, "
                "spaces, hyphens, and underscores."
            )


def ranking_result_columns(setup: dict[str, Any]) -> list[str]:
    if not bool(setup.get("ranking_enabled", False)):
        return []
    periods = normalize_ranking_rsi_periods(
        setup.get("ranking_rsi_periods", [22, 44, 66])
    )
    return [
        *(operand_text(field_operand("RSI", "D", 0, period)) for period in periods),
        COMPOSITE_RSI_MOMENTUM_COLUMN,
    ]


def ranking_required_calendar_days(setup: dict[str, Any]) -> int:
    if not bool(setup.get("ranking_enabled", False)):
        return 0
    max_period = max(
        normalize_ranking_rsi_periods(
            setup.get("ranking_rsi_periods", [22, 44, 66])
        )
    )
    # Match the scanner's long RSI/RMA warm-up policy used for rule operands.
    return min((max(max_period * 10, 250) + 3) * 2, 365 * 30)


def add_composite_rsi_ranking_values(
    row: dict[str, Any],
    setup: dict[str, Any],
    value_for_period: Callable[[int], Any],
) -> None:
    if not bool(setup.get("ranking_enabled", False)):
        return
    values: list[float] = []
    for period in normalize_ranking_rsi_periods(
        setup.get("ranking_rsi_periods", [22, 44, 66])
    ):
        label = operand_text(field_operand("RSI", "D", 0, period))
        try:
            value = float(value_for_period(period))
        except (TypeError, ValueError):
            value = math.nan
        row[label] = finite_or_none(value)
        if math.isfinite(value):
            values.append(value)
    expected = len(
        normalize_ranking_rsi_periods(
            setup.get("ranking_rsi_periods", [22, 44, 66])
        )
    )
    row[COMPOSITE_RSI_MOMENTUM_COLUMN] = (
        finite_or_none(sum(values) / expected) if len(values) == expected else None
    )


def rank_scan_rows(
    rows: list[dict[str, Any]], setup: dict[str, Any]
) -> list[dict[str, Any]]:
    if not bool(setup.get("ranking_enabled", False)):
        return rows
    descending = str(setup.get("ranking_direction", "DESC")).upper() != "ASC"

    def score(row: dict[str, Any]) -> float:
        try:
            value = float(row.get(COMPOSITE_RSI_MOMENTUM_COLUMN))
            return value if math.isfinite(value) else math.nan
        except (TypeError, ValueError):
            return math.nan

    valid = [row for row in rows if math.isfinite(score(row))]
    invalid = [row for row in rows if not math.isfinite(score(row))]
    valid.sort(
        key=lambda row: (
            -score(row) if descending else score(row),
            str(row.get("Symbol", "")),
        )
    )
    invalid.sort(key=lambda row: str(row.get("Symbol", "")))
    ranked = valid + invalid
    try:
        top_n = max(0, int(setup.get("ranking_top_n", 0) or 0))
    except (TypeError, ValueError):
        top_n = 0
    return ranked[:top_n] if top_n else ranked


def expression_text(node: dict[str, Any]) -> str:
    if node["type"] == "rule":
        if is_52_week_range_rule(node):
            return plain_rule_text(node)
        left_txt = operand_text(node['left'])
        right_txt = operand_text(node['right'])
        if node['left'].get('kind') == 'field' and node['left'].get('name') in PNL_FIELDS:
            if node['right'].get('kind') == 'value' and not right_txt.endswith('%'):
                right_txt += '%'
        return f"{left_txt} {node['operator']} {right_txt}"
    if not node_enabled(node):
        return "<no enabled rules>"
    children = [expression_text(c) for c in node["children"] if has_active_rule(c)]
    if not children:
        return "<no enabled rules>"
    return "(" + f" {node['op']} ".join(children) + ")"


def format_active_rules(node: dict[str, Any], indent: int = 0) -> str:
    """Format the active (enabled) rules recursively as a nested bullet list."""
    if not node_enabled(node) or not has_active_rule(node):
        return ""
    prefix = "  " * indent
    if node["type"] == "rule":
        return f"{prefix}• {expression_text(node)}"
    if node["type"] == "group":
        op = str(node.get("op", "AND"))
        if op == "AND":
            label = "AND (Pass ALL)"
        elif op == "OR":
            label = "OR (Pass ANY 1)"
        elif op.startswith("ANY "):
            try:
                k = op.split()[1]
            except IndexError:
                k = "1"
            label = f"{op} (Pass at least {k})"
        else:
            label = f"{op} Group"
        header = f"{prefix}🔹 {label}:"
        lines = [header]
        for child in node.get("children", []):
            child_text = format_active_rules(child, indent + 1)
            if child_text:
                lines.append(child_text)
        return "\n".join(lines)
    return ""


def plain_operand_text(operand: dict[str, Any]) -> str:
    if operand["kind"] == "value":
        return f"{float(operand['value']):g}"
    timeframe = {"D": "Daily", "W": "Weekly", "M": "Monthly"}[
        operand["timeframe"]
    ]
    offset = int(operand.get("offset", 0))
    compact = operand_text(operand)
    compact_suffix = f" {operand['timeframe']}"
    if offset:
        compact_suffix += f" offset {offset}"
    core = compact[: -len(compact_suffix)]
    result = f"{timeframe} {core}"
    if offset:
        candle_word = {
            "D": "daily",
            "W": "weekly",
            "M": "monthly",
        }[operand["timeframe"]]
        count = "one" if offset == 1 else str(offset)
        plural = "" if offset == 1 else "s"
        result += f" from {count} {candle_word} candle{plural} ago"
    return result


def plain_rule_text(node: dict[str, Any]) -> str:
    left_operand = node["left"]
    right_operand = node["right"]
    if is_52_week_range_rule(node):
        above, below, offset = range_rule_limits(node)
        when = ""
        if offset == 1:
            when = " one day ago"
        elif offset:
            when = f" {offset} days ago"
        if math.isclose(above, below, rel_tol=0.0, abs_tol=1e-12):
            return (
                f"Daily Close{when} is within {above:g}% above or below "
                f"the prior {lookback_high_name(right_operand).removesuffix(' Distance %').lower()}"
            )
        return (
            f"Daily Close{when} is between {above:g}% above and {below:g}% below "
            f"the prior {lookback_high_name(right_operand).removesuffix(' Distance %').lower()}"
        )

    operator_phrases = {
        ">": "is above",
        ">=": "is at or above",
        "<": "is below",
        "<=": "is at or below",
        "=": "is equal to",
        "!=": "is not equal to",
        "crosses above": "crosses above",
        "crosses below": "crosses below",
    }
    left = plain_operand_text(left_operand)
    right = plain_operand_text(right_operand)
    explanation = f"{left} {operator_phrases[node['operator']]} {right}"
    if node["operator"] == "crosses above":
        explanation += (
            " (it is above on the evaluated candle and was at or below "
            "on the preceding comparison candle)"
        )
    elif node["operator"] == "crosses below":
        explanation += (
            " (it is below on the evaluated candle and was at or above "
            "on the preceding comparison candle)"
        )
    return explanation


def filter_logic_explanation(node: dict[str, Any]) -> str:
    tree_lines: list[str] = []

    def tree(
        current: dict[str, Any],
        prefix: str = "",
        connector: str = "",
        ancestor_active: bool = True,
    ) -> None:
        effective = ancestor_active and node_enabled(current)
        if current["type"] == "group":
            meaning = (
                "everything must pass"
                if current["op"] == "AND"
                else "any one may pass"
            )
            label = f"{current['op']} group — {meaning}"
            if not current["children"]:
                label += " (empty)"
        else:
            label = plain_rule_text(current)
        if not effective:
            label += " [disabled — ignored by scans]"
        tree_lines.append(prefix + connector + label)
        if current["type"] != "group":
            return
        child_prefix = prefix + ("   " if not connector else "   ")
        for index, child in enumerate(current["children"]):
            last = index == len(current["children"]) - 1
            tree(
                child,
                child_prefix,
                "└─ " if last else "├─ ",
                effective,
            )

    explanation_lines: list[str] = []

    def explain_group(current: dict[str, Any], indent: int = 0) -> None:
        padding = "  " * indent
        if not node_enabled(current):
            explanation_lines.append(
                f"{padding}{current['op']} group: Disabled; this entire branch is ignored."
            )
            return
        if not current["children"]:
            explanation_lines.append(
                f"{padding}{current['op']} group: This group is empty. "
                "Add at least one rule or subgroup before scanning."
            )
            return
        requirement = (
            "Every item below must pass."
            if current["op"] == "AND"
            else "At least one item below must pass."
        )
        explanation_lines.append(
            f"{padding}{current['op']} group: {requirement}"
        )
        for index, child in enumerate(current["children"], 1):
            if not has_active_rule(child):
                if child["type"] == "rule":
                    description = plain_rule_text(child)
                    reason = "Disabled; ignored by scans."
                else:
                    description = f"Nested {child['op']} group"
                    reason = (
                        "Disabled; its entire branch is ignored."
                        if not node_enabled(child)
                        else "Contains no enabled rules; ignored by scans."
                    )
                explanation_lines.append(f"{padding}  {index}. {description}: {reason}")
                continue
            if child["type"] == "rule":
                explanation_lines.append(
                    f"{padding}  {index}. {plain_rule_text(child)}."
                )
            else:
                explanation_lines.append(
                    f"{padding}  {index}. Nested {child['op']} group:"
                )
                explain_group(child, indent + 2)

    tree(node)
    explain_group(node)
    if not has_active_rule(node):
        outcome = "No rules are enabled, so the scanner will not run."
    elif node["op"] == "AND":
        outcome = (
            "A stock matches only when every item in the top-level AND group passes."
        )
    else:
        outcome = (
            "A stock matches when at least one item in the top-level OR group passes."
        )
    return "\n".join(
        [
            "FILTER LOGIC EXPLAINED",
            "",
            "STRUCTURE",
            *tree_lines,
            "",
            "HOW THE FILTER IS EVALUATED",
            outcome,
            *explanation_lines,
            "",
            "Only confirmed, enabled rules visible in the structured tree are included. "
            "An unparsed English-rule draft is not part of this explanation.",
        ]
    )


def rule_value_columns(
    node: dict[str, Any],
    include_disabled: bool = False,
) -> list[str]:
    """Return stable, deduplicated result columns for every field used by the AST."""
    columns: list[str] = []

    def add_operand(operand: dict[str, Any], previous: bool = False) -> None:
        if operand.get("kind") != "field":
            return
        label = operand_text(operand)
        if previous:
            label = f"Previous {label}"
        if label not in columns:
            columns.append(label)
        if operand["name"] == "52 Week High Distance %" and not previous:
            offset = int(operand.get("offset", 0))
            close_label = "Close" if offset == 0 else operand_text(field_operand("Close", "D", offset))
            high_label = operand_text(matching_lookback_high_operand(operand))
            for related_label in (close_label, high_label):
                if related_label not in columns:
                    columns.append(related_label)

    def visit(current: dict[str, Any]) -> None:
        if not include_disabled and not node_enabled(current):
            return
        if current["type"] == "group":
            for child in current["children"]:
                visit(child)
            return
        add_operand(current["left"])
        add_operand(current["right"])
        if current["operator"] in {"crosses above", "crosses below"}:
            add_operand(current["left"], True)
            add_operand(current["right"], True)

    visit(node)
    return columns


def selected_result_headers(setup: dict[str, Any]) -> list[str]:
    selected = list(setup.get("result_columns", ALL_RESULT_COLUMNS))
    # Templates saved before provider timing was introduced should gain the new
    # useful columns automatically. Once saved again, the user's selections are
    # respected through timing_result_columns_saved.
    if not setup.get("timing_result_columns_saved", False):
        insert_at = selected.index("As Of") + 1 if "As Of" in selected else len(selected)
        for column in reversed(TIMING_RESULT_COLUMNS):
            if column not in selected:
                selected.insert(insert_at, column)
    if not setup.get("data_source_column_saved", False):
        insert_at = selected.index("As Of") + 1 if "As Of" in selected else len(selected)
        if DATA_SOURCE_COLUMN not in selected:
            selected.insert(insert_at, DATA_SOURCE_COLUMN)
    if not setup.get("percent_change_column_saved", False):
        insert_at = selected.index("Symbol") + 1 if "Symbol" in selected else 0
        if PERCENT_CHANGE_COLUMN not in selected:
            selected.insert(insert_at, PERCENT_CHANGE_COLUMN)
    rule_columns = rule_value_columns(setup["expression"])
    ranking_columns = ranking_result_columns(setup)
    dynamic_columns = [*rule_columns, *ranking_columns]
    dynamic_saved = bool(setup.get("dynamic_result_columns_saved", False))
    headers = [
        column
        for column in selected
        if column in ALL_RESULT_COLUMNS or column in dynamic_columns
    ]
    if not setup.get("serial_result_column_saved", False) and SERIAL_COLUMN not in headers:
        headers.insert(0, SERIAL_COLUMN)
    elif SERIAL_COLUMN in headers:
        # Serial numbers are generated from displayed row order, so keep them
        # first even if an older template stored columns in another order.
        headers.remove(SERIAL_COLUMN)
        headers.insert(0, SERIAL_COLUMN)
    if "Symbol" not in headers:
        headers.insert(1, "Symbol")
    if PERCENT_CHANGE_COLUMN in headers:
        headers.remove(PERCENT_CHANGE_COLUMN)
        headers.insert(headers.index("Symbol") + 1, PERCENT_CHANGE_COLUMN)
    if not dynamic_saved:
        headers.extend(column for column in dynamic_columns if column not in headers)
    elif ranking_columns:
        # Ranking columns are integral to understanding the requested order.
        headers.extend(column for column in ranking_columns if column not in headers)
    saved_order = normalized_result_column_order(setup)
    return [column for column in saved_order if column in headers] + [
        column for column in headers if column not in saved_order
    ]


def normalized_result_column_order(setup: dict[str, Any]) -> list[str]:
    """Stable name-based order including currently disabled rule columns."""
    available: list[str] = []
    for column in (
        *ALL_RESULT_COLUMNS,
        *rule_value_columns(setup["expression"], include_disabled=True),
        *ranking_result_columns(setup),
    ):
        if column not in available:
            available.append(column)
    stored = setup.get("result_column_order", [])
    if not isinstance(stored, list):
        stored = []
    ordered = [
        column
        for column in stored
        if isinstance(column, str) and column in available
    ]
    # Ignore duplicate/corrupt entries and append columns introduced by new rules.
    ordered = list(dict.fromkeys(ordered))
    ordered.extend(column for column in available if column not in ordered)
    return ordered


def merge_visible_result_order(
    setup: dict[str, Any], visible_order: Iterable[str]
) -> list[str]:
    """Reorder visible names while retaining disabled/unchecked column slots."""
    full_order = normalized_result_column_order(setup)
    visible = list(
        dict.fromkeys(column for column in visible_order if column in full_order)
    )
    visible_set = set(visible)
    replacements = iter(visible)
    merged = [
        next(replacements) if column in visible_set else column
        for column in full_order
    ]
    return merged


def expression_has_pnl_rules(node: dict[str, Any] | None) -> bool:
    """Checks whether an AST expression contains holding-dependent P&L rules."""
    if not isinstance(node, dict):
        return False
    node_type = node.get("type") or node.get("kind")
    pnl_names = {"Profit %", "Loss %", "Stop Loss %", "P&L %", "P&L Amount"}
    if node_type == "rule":
        left = node.get("left", {})
        right = node.get("right", {})
        left_name = left.get("name", "") if isinstance(left, dict) else ""
        right_name = right.get("name", "") if isinstance(right, dict) else ""
        if left_name in pnl_names or right_name in pnl_names:
            return True
    elif node_type == "group":
        for child in node.get("children", []):
            if expression_has_pnl_rules(child):
                return True
    return False


def setup_portfolio_account(setup: dict[str, Any]) -> str | None:
    """Return the one broker account unambiguously selected by a setup."""
    explicit = str(setup.get("algo_target_account", "")).strip().upper()
    if explicit and explicit not in {"ACTIVE", "ALL", "MAIN"}:
        return explicit
    account_ids: set[str] = set()
    for universe in setup.get("universes", []):
        match = re.search(r"\(([A-Z0-9]+)\)\s*$", str(universe).strip(), re.I)
        if match:
            account_ids.add(match.group(1).upper())
    return next(iter(account_ids)) if len(account_ids) == 1 else None


def setup_paper_account(setup: dict[str, Any]) -> str | None:
    """Return the virtual paper account uid selected by a setup, if any."""
    value = str(setup.get("algo_paper_account", "")).strip()
    return value.upper() if value else None


def setup_has_portfolio_exit_triggers(setup: dict[str, Any]) -> bool:
    return (
        "SELL" in str(setup.get("algo_trade_action", "")).upper()
        and (
            bool(setup.get("algo_target_profit_enabled", False))
            or bool(setup.get("algo_stop_loss_enabled", False))
        )
    )


def build_paper_plan(
    rows: list[dict[str, Any]],
    setup: dict[str, Any],
    account: str,
) -> list[dict[str, Any]]:
    """Turn matched scan rows into a paper-trade plan for one virtual account."""
    template_name = str(setup.get("template_name", "")).strip()
    scan_id = get_ist_now().strftime("SCAN-%Y%m%d-%H%M%S-%f")
    is_sell = "SELL" in str(setup.get("algo_trade_action", "")).upper()
    order_type = str(setup.get("algo_order_type", "LIMIT")).upper()
    product = "CNC" if "CNC" in str(setup.get("algo_product", "CNC (Delivery)")) else "MIS"
    capital_mode = str(setup.get("algo_capital_mode", ""))
    max_slots = max(1, int(setup.get("algo_max_slots", 5)) or 5)
    sell_pct: float | None = None
    per_stock = 0.0
    if is_sell:
        if "100%" not in str(
            setup.get("algo_sell_mode", "100% (Sell Full Holding Quantity)")
        ):
            try:
                sell_pct = max(1.0, float(setup.get("algo_sell_pct", 50.0)) or 50.0)
            except (TypeError, ValueError):
                sell_pct = 50.0
    else:
        fallback_cash = PAPER_STARTING_CASH
        acc = None
        try:
            data = PaperTradesStore().load()
            acc = data.get("accounts", {}).get(str(account).strip().upper())
        except PaperTradesStoreError:
            acc = None
        try:
            fallback_cash = PaperTradesStore().get_account_settings(account)[
                "starting_cash"
            ]
        except PaperTradesStoreError:
            pass
        cash = float(acc.get("cash", fallback_cash)) if acc else fallback_cash
        if "Dynamic Allocation" in capital_mode:
            per_stock = cash / max_slots
        else:
            try:
                per_stock = float(setup.get("algo_capital_per_stock", 5000.0)) or 5000.0
            except (TypeError, ValueError):
                per_stock = 5000.0
        if per_stock <= 0:
            return []
    plan: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get("Symbol", "")).strip().upper()
        if not symbol:
            continue
        try:
            price = float(str(row.get("Close", 0)).replace(",", ""))
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            continue
        item: dict[str, Any] = {
            "account": str(account).strip().upper(),
            "symbol": symbol,
            "action": "SELL" if is_sell else "BUY",
            "price": price,
            "qty": 0,
            "sell_percent": sell_pct,
            "template": template_name,
            "matched_rule": str(row.get("Evidence", "")),
            "scan_id": scan_id,
            "order_type": order_type,
            "product": product,
            "capital_mode": capital_mode,
            "slot": None,
            "slots": max_slots if not is_sell else None,
        }
        if not is_sell:
            qty = int(per_stock // price)
            if qty < 1:
                continue
            item["qty"] = qty
        plan.append(item)
    return plan


class RuleParser:
    """Deliberately narrow one-rule parser; ambiguous input is rejected."""

    OP_RE = re.compile(
        r"(?:\s+(crosses\s+above|crosses\s+below|crossed\s+above|crossed\s+below|"
        r"is\s+not\s+equal\s+to|does\s+not\s+equal|not\s+equal\s+to|not\s+equals?|"
        r"is\s+equal\s+to|equal\s+to|equals)\s+|\s*(>=|<=|!=|==|=|>|<)\s*)",
        re.I,
    )
    NUMBER_WORDS = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
    }
    NATURAL_OFFSET_RE = re.compile(
        r"(?:"
        r"(?P<count>\d+|one|two|three|four|five|six|seven|eight|nine|ten)"
        r"\s+(?:trading\s+)?(?P<unit>days?|weeks?|months?|candles?)\s+ago"
        r"|previous\s+(?P<previous>day|week|month|candle)"
        r"|(?P<yesterday>yesterday)"
        r")",
        re.I,
    )
    FIELD_RE = re.compile(
        r"^(?:(daily|weekly|monthly|d|w|m)\s+)?"
        r"(52\s*week\s*high(?:\s*distance\s*%)?|macd\s+(?:line|signal|histogram)|"
        r"profit\s*%|loss\s*%|stop\s*loss\s*%|p&l\s*%|pnl\s*%|p&l\s*amount|pnl\s*amount|profit|loss|stop\s*loss|p&l|pnl|"
        r"open|high|low|close|volume|rsi|sma|ema|adx|obv|supertrend)"
        r"(?:"
        r"\s*\(\s*(?:(?:daily|weekly|monthly\s+)?close\s*,\s*)?(\d+)(?:\s*,\s*(\d+(?:\.\d+)?))?(?:\s*,\s*(\d+))?\s*\)"
        r"|\s+(\d+)(?:\s+(\d+(?:\.\d+)?))?(?:\s+(\d+))?"
        r")?"
        r"(?:\s+(?:offset|ago)\s+(\d+))?"
        r"(?:\s+(daily|weekly|monthly|d|w|m))?$",
        re.I,
    )
    TF = {"daily": "D", "weekly": "W", "monthly": "M", "d": "D", "w": "W", "m": "M"}

    @classmethod
    def parse(cls, text: str) -> dict[str, Any]:
        clean = " ".join(text.strip().split())
        clean = re.sub(r"\b(ema|sma|rsi|adx|obv|supertrend)\s*\(\s*(?:(?:daily|weekly|monthly)\s+)?close\s*,\s*(\d+)\s*\)", r"\1 \2", clean, flags=re.I)
        clean = re.sub(r"\s+greater\s+than\s+or\s+equal\s+to\s+", " >= ", clean, flags=re.I)
        clean = re.sub(r"\s+less\s+than\s+or\s+equal\s+to\s+", " <= ", clean, flags=re.I)
        clean = re.sub(r"\s+greater\s+than\s+", " > ", clean, flags=re.I)
        clean = re.sub(r"\s+less\s+than\s+", " < ", clean, flags=re.I)
        clean = re.sub(r"\s+equal\s+to\s+", " = ", clean, flags=re.I)
        clean = re.sub(r"\s+today$", "", clean, flags=re.I)
        high_rule = cls._parse_52_week_high_rule(clean)
        if high_rule is not None:
            return high_rule
        clean = re.sub(r"(?<!crosses)(?<!crossed)\s+above\s+", " > ", clean, count=1, flags=re.I)
        clean = re.sub(r"(?<!crosses)(?<!crossed)\s+below\s+", " < ", clean, count=1, flags=re.I)
        matches = list(cls.OP_RE.finditer(clean))
        if len(matches) != 1:
            raise ValueError("Enter exactly one comparison, for example: daily close crosses above daily SMA(20).")
        match = matches[0]
        left = cls._operand(clean[: match.start()])
        right = cls._operand(clean[match.end() :])
        operator = (match.group(1) or match.group(2)).lower()
        if operator.startswith("crossed "):
            operator = "crosses " + operator.removeprefix("crossed ")
        elif operator in {"is equal to", "equal to", "equals", "=="}:
            operator = "="
        elif operator in {
            "is not equal to",
            "does not equal",
            "not equal to",
            "not equal",
            "not equals",
        }:
            operator = "!="
        
        node = rule_node(left, operator, right)
        if left.get("kind") == "field" and left.get("name") in {"Stop Loss %", "P&L %"} and right.get("kind") == "value":
            val = float(right.get("value", 0))
            if left.get("name") == "Stop Loss %" and val > 0 and operator in {"<=", "<", "="}:
                right["value"] = -val
        return node

    @classmethod
    def _parse_52_week_high_rule(cls, text: str) -> dict[str, Any] | None:
        clean, offset, natural_timeframe = cls._extract_natural_offset(text)
        available_match = re.search(
            r"\b(?:all\s+)?available(?:-|\s+)history(?:\s+since\s+2014)?"
            r"\s+high(?:\s+since\s+2014)?\b",
            clean,
            re.I,
        )
        high_match = available_match or re.search(
            r"\b(\d+)\s*(?:-|\s)?\s*(day|week|month|year)s?\s+high\b",
            clean,
            re.I,
        )
        if not high_match:
            return None
        if available_match:
            lookback_count = 1
            lookback_unit = ALL_AVAILABLE_HISTORY_UNIT
            legacy_52_week = False
        else:
            lookback_count = int(high_match.group(1))
            lookback_unit = high_match.group(2).title() + "s"
            if lookback_count < 1:
                raise ValueError("High-lookback duration must be at least 1.")
            legacy_52_week = lookback_count == 52 and lookback_unit == "Weeks"
        normalized = clean[: high_match.start()] + "52 week high" + clean[high_match.end() :]
        normalized = " ".join(normalized.split())
        if natural_timeframe not in (None, "D"):
            raise ValueError("High-lookback rules accept daily offsets such as 'one day ago'.")

        duration_args = (
            (None, None) if legacy_52_week else (lookback_count, lookback_unit)
        )

        asymmetric_match = re.fullmatch(
            r"(?:daily\s+)?close\s+(?:is\s+)?(?:between|within)\s+"
            r"(\d+(?:\.\d+)?)\s*%\s+above\s+(?:and|&)\s+"
            r"(\d+(?:\.\d+)?)\s*%\s+below\s+"
            r"(?:of\s+)?(?:the\s+)?(?:prior\s+)?52 week high",
            normalized,
            re.I,
        )
        if asymmetric_match:
            above = float(asymmetric_match.group(1))
            below = float(asymmetric_match.group(2))
            if not all(0 <= value <= 100 for value in (above, below)):
                raise ValueError(
                    "The above and below 52-week-high limits must each be between 0% and 100%."
                )
            return high_52_week_range_rule(
                above, below, int(offset or 0), *duration_args
            )

        at_match = re.fullmatch(
            r"(?:daily\s+)?close\s+(?:is\s+)?at\s+(?:the\s+)?(?:prior\s+)?52 week high",
            normalized,
            re.I,
        )
        tolerance: float | None = 0.0 if at_match else None
        if tolerance is None:
            near_match = re.fullmatch(
                r"(?:daily\s+)?close\s+(?:is\s+)?near\s+(?:the\s+)?(?:prior\s+)?52 week high"
                r"(?:\s+(?:by|within)\s+(\d+(?:\.\d+)?)\s*%?)?",
                normalized,
                re.I,
            )
            if near_match:
                tolerance = float(near_match.group(1) or 2.0)
        if tolerance is None:
            within_match = re.fullmatch(
                r"(?:daily\s+)?close\s+(?:is\s+)?within\s+(\d+(?:\.\d+)?)\s*%"
                r"\s+(?:(?:above\s+or\s+below|either\s+side\s+of)\s+)?"
                r"(?:of\s+)?(?:the\s+)?(?:prior\s+)?52 week high",
                normalized,
                re.I,
            )
            if within_match:
                tolerance = float(within_match.group(1))
        if tolerance is None:
            return None
        if not 0 <= tolerance <= 100:
            raise ValueError("The 52-week-high tolerance must be between 0% and 100%.")
        return high_52_week_range_rule(
            tolerance, tolerance, int(offset or 0), *duration_args
        )

    @classmethod
    def _operand(cls, text: str) -> dict[str, Any]:
        text, natural_offset, natural_timeframe = cls._extract_natural_offset(text)

        available_match = re.fullmatch(
            r"(?:(?:daily|d)\s+)?(?:the\s+)?(?:all\s+)?available(?:-|\s+)history"
            r"(?:\s+since\s+2014)?\s+high(?:\s+since\s+2014)?"
            r"(?:\s+distance\s*%)?(?:\s+(?:daily|d))?",
            text.strip(),
            re.I,
        )
        if available_match:
            is_distance = bool(re.search(r"distance\s*%", text, re.I))
            operand = field_operand(
                "52 Week High Distance %" if is_distance else "52 Week High",
                "D",
                int(natural_offset or 0),
                lookback_count=1,
                lookback_unit=ALL_AVAILABLE_HISTORY_UNIT,
            )
            validate_operand(operand)
            return operand

        lookback_match = re.fullmatch(
            r"(?:(?:daily|d)\s+)?(\d+)\s*(?:-|\s)?\s*"
            r"(day|week|month|year)s?\s+high(?:\s+distance\s*%)?"
            r"(?:\s+(?:daily|d))?",
            text.strip(),
            re.I,
        )
        if lookback_match:
            count = int(lookback_match.group(1))
            unit = lookback_match.group(2).title() + "s"
            is_distance = bool(re.search(r"distance\s*%", text, re.I))
            legacy = count == 52 and unit == "Weeks"
            operand = field_operand(
                "52 Week High Distance %" if is_distance else "52 Week High",
                "D",
                int(natural_offset or 0),
                lookback_count=None if legacy else count,
                lookback_unit=None if legacy else unit,
            )
            validate_operand(operand)
            return operand
        
        parsed_mult = None
        mult_match = re.search(r"\s*(?:\*|x|times|multiplied\s+by)\s*(?:Number\s+)?(\d+(?:\.\d+)?)\s*$", text, re.I)
        if mult_match:
            parsed_mult = float(mult_match.group(1))
            text = text[: mult_match.start()].strip()

        text = re.sub(r"\bNumber\s+", "", text, flags=re.I).strip()
        prefix_period = re.fullmatch(r"(\d+)\s+(RSI|SMA|EMA|ADX)", text.strip(), re.I)
        if prefix_period:
            text = f"{prefix_period.group(2)} {prefix_period.group(1)}"
        try:
            val_clean = text.strip().replace(",", "").rstrip("%")
            return value_operand(float(val_clean))
        except ValueError:
            pass
        match = cls.FIELD_RE.fullmatch(text.strip())
        if not match:
            raise ValueError(f"Could not understand operand: {text!r}")
        prefix_tf, name, p1, m1, s1, p2, m2, s2, offset, suffix_tf = match.groups()
        if prefix_tf and suffix_tf and cls.TF[prefix_tf.lower()] != cls.TF[suffix_tf.lower()]:
            raise ValueError("An operand cannot specify two different timeframes.")
        explicit_timeframe = cls.TF[(prefix_tf or suffix_tf).lower()] if (prefix_tf or suffix_tf) else None
        if natural_timeframe and explicit_timeframe and natural_timeframe != explicit_timeframe:
            raise ValueError("The natural offset unit conflicts with the operand timeframe.")
        timeframe = natural_timeframe or explicit_timeframe or "D"
        lower_name = name.lower()
        if lower_name in {"rsi", "sma", "ema", "adx", "obv"}:
            canonical = name.upper()
        elif lower_name.startswith("52"):
            canonical = (
                "52 Week High Distance %"
                if "distance" in lower_name
                else "52 Week High"
            )
        elif "profit" in lower_name:
            canonical = "Profit %"
        elif lower_name in {"loss", "loss %"}:
            canonical = "Loss %"
        elif "stop" in lower_name:
            canonical = "Stop Loss %"
        elif "p&l" in lower_name or "pnl" in lower_name:
            canonical = "P&L Amount" if "amount" in lower_name else "P&L %"
        elif lower_name.startswith("macd "):
            canonical = "MACD " + lower_name.removeprefix("macd ").title()
        else:
            canonical = name.title()
        period = int(p1 or p2 or 14)
        multiplier = float(m1 or m2 or 3.0)
        supplied_parameters = (p1, m1, s1, p2, m2, s2)
        if canonical in {"Open", "High", "Low", "Close", "Volume", "OBV", "P&L %", "P&L Amount", *HIGH_52_WEEK_FIELDS} and any(
            supplied_parameters
        ):
            raise ValueError(f"{canonical} does not take a period.")
        if canonical in {"RSI", "SMA", "EMA"} and any((m1, s1, m2, s2)):
            raise ValueError(f"{canonical} accepts one period.")
        if canonical == "Supertrend" and any((s1, s2)):
            raise ValueError("Supertrend accepts a period and multiplier.")
        adx_smoothing = 14
        if canonical == "ADX":
            if any((s1, s2)):
                raise ValueError("ADX accepts DI length and ADX smoothing.")
            raw_smoothing = m1 or m2
            if raw_smoothing is not None and not float(raw_smoothing).is_integer():
                raise ValueError("ADX smoothing must be a whole number.")
            adx_smoothing = (
                int(float(raw_smoothing)) if raw_smoothing is not None else 14
            )
        fast_period, slow_period, signal_period = 12, 26, 9
        if canonical in MACD_FIELDS:
            fast_period = int(p1 or p2 or 12)
            raw_slow = m1 or m2
            if raw_slow is not None and not float(raw_slow).is_integer():
                raise ValueError("MACD slow period must be a whole number.")
            slow_period = int(float(raw_slow)) if raw_slow is not None else 26
            signal_period = int(s1 or s2 or 9)
        if natural_offset is not None and offset is not None:
            raise ValueError("Specify an offset only once.")
        mult_arg = parsed_mult if parsed_mult is not None else (multiplier if canonical == "Supertrend" else 1.0)
        operand = field_operand(
            canonical,
            timeframe,
            natural_offset if natural_offset is not None else int(offset or 0),
            period,
            mult_arg,
            fast_period,
            slow_period,
            signal_period,
            adx_smoothing,
        )
        validate_operand(operand)
        return operand

    @classmethod
    def _extract_natural_offset(cls, text: str) -> tuple[str, int | None, str | None]:
        clean = " ".join(text.strip().split())
        if re.search(r"\s+today$", clean, re.I):
            return re.sub(r"\s+today$", "", clean, flags=re.I), 0, None
        if re.match(r"^today\s+", clean, re.I):
            return re.sub(r"^today\s+", "", clean, flags=re.I), 0, None

        match = re.search(rf"\s+({cls.NATURAL_OFFSET_RE.pattern})$", clean, re.I)
        prefix = False
        if not match:
            match = re.match(rf"^({cls.NATURAL_OFFSET_RE.pattern})\s+", clean, re.I)
            prefix = bool(match)
        if not match:
            return clean, None, None

        natural = cls.NATURAL_OFFSET_RE.fullmatch(match.group(1))
        if natural is None:
            return clean, None, None
        if prefix:
            remaining = clean[match.end() :]
        else:
            remaining = clean[: match.start()]

        if natural.group("yesterday"):
            return remaining, 1, "D"
        previous = natural.group("previous")
        if previous:
            timeframe = {"day": "D", "week": "W", "month": "M"}.get(previous.lower())
            return remaining, 1, timeframe
        raw_count = natural.group("count").lower()
        count = int(raw_count) if raw_count.isdigit() else cls.NUMBER_WORDS[raw_count]
        unit = natural.group("unit").lower()
        timeframe = None
        if unit.startswith("day"):
            timeframe = "D"
        elif unit.startswith("week"):
            timeframe = "W"
        elif unit.startswith("month"):
            timeframe = "M"
        return remaining, count, timeframe


class TemplateStoreError(ValueError):
    pass


class TemplateStore:
    """Versioned, atomic named setup storage with a separate recovery setup."""

    def __init__(self, path: Path = TEMPLATE_FILE):
        self.path = Path(path)

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {"schema_version": TEMPLATE_SCHEMA_VERSION, "active": None, "recovery": None, "templates": {}}

    def load_state(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty_state()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TemplateStoreError(f"Template file is malformed and was preserved: {exc}") from exc
        if not isinstance(payload, dict):
            raise TemplateStoreError("Template file root is invalid and was preserved.")
        if "schema_version" not in payload:
            active = payload.get("active") if isinstance(payload.get("active"), str) else None
            recovery = payload.get(AUTOSAVE_NAME) if isinstance(payload.get(AUTOSAVE_NAME), dict) else None
            meta_keys = {
                "active", "multi_scan", "strategy_automation",
                "strategy_automation_recipes",
                AUTOSAVE_NAME, "schema_version",
            }
            templates = {
                name: value for name, value in payload.items()
                if name not in meta_keys and isinstance(value, dict)
            }
            return {
                "schema_version": TEMPLATE_SCHEMA_VERSION,
                "active": active,
                "recovery": recovery,
                "templates": templates,
            }
        if payload.get("schema_version") != TEMPLATE_SCHEMA_VERSION:
            raise TemplateStoreError(f"Unsupported template schema version: {payload.get('schema_version')}")
        if not isinstance(payload.get("templates"), dict):
            raise TemplateStoreError("Template collection is invalid and was preserved.")
        if payload.get("active") is not None and payload["active"] not in payload["templates"]:
            payload["active"] = None
        payload.setdefault("recovery", None)
        return payload

    def load_all(self) -> dict[str, dict[str, Any]]:
        return self.load_state()["templates"]

    def save_edit(self, active_name: str | None, setup: dict[str, Any]) -> None:
        payload = self.load_state()
        payload["recovery"] = copy.deepcopy(setup)
        if active_name:
            if active_name not in payload["templates"]:
                raise TemplateStoreError("The active template no longer exists.")
            payload["templates"][active_name] = copy.deepcopy(setup)
            payload["active"] = active_name
        self._write(payload)

    def create(self, name: str, setup: dict[str, Any]) -> None:
        clean_name = name.strip()
        if not clean_name:
            raise TemplateStoreError("Template name cannot be blank.")
        payload = self.load_state()
        if clean_name in payload["templates"]:
            raise TemplateStoreError("A template with that name already exists.")
        payload["templates"][clean_name] = copy.deepcopy(setup)
        payload["active"] = clean_name
        payload["recovery"] = copy.deepcopy(setup)
        self._write(payload)

    def set_active(self, name: str) -> dict[str, Any]:
        payload = self.load_state()
        if name not in payload["templates"]:
            raise TemplateStoreError("Template no longer exists.")
        payload["active"] = name
        payload["recovery"] = copy.deepcopy(payload["templates"][name])
        self._write(payload)
        return copy.deepcopy(payload["templates"][name])

    def delete(self, name: str) -> str | None:
        payload = self.load_state()
        payload["templates"].pop(name, None)
        if payload.get("active") == name:
            payload["active"] = None
            # Do not resurrect the just-deleted active template as the unnamed
            # recovery setup the next time the application starts.
            payload["recovery"] = None
        self._write(payload)
        return payload["active"]

    def rename(self, old_name: str, new_name: str) -> None:
        clean_name = new_name.strip()
        payload = self.load_state()
        if old_name not in payload["templates"]:
            raise TemplateStoreError("Template no longer exists.")
        if not clean_name:
            raise TemplateStoreError("Template name cannot be blank.")
        if clean_name != old_name and clean_name in payload["templates"]:
            raise TemplateStoreError("A template with that name already exists.")
        value = payload["templates"].pop(old_name)
        payload["templates"][clean_name] = value
        if payload.get("active") == old_name:
            payload["active"] = clean_name
        self._write(payload)

    def update_template_fields(self, name: str, fields: dict[str, Any]) -> None:
        """Update template metadata without changing the active template."""
        payload = self.load_state()
        if name not in payload["templates"]:
            raise TemplateStoreError("Template no longer exists.")
        payload["templates"][name].update(copy.deepcopy(fields))
        if payload.get("active") == name and isinstance(payload.get("recovery"), dict):
            payload["recovery"].update(copy.deepcopy(fields))
        self._write(payload)

    def rename_universe_in_templates(self, old_name: str, new_name: str) -> None:
        payload = self.load_state()
        modified = False

        def update_setup(setup: Any) -> None:
            nonlocal modified
            if isinstance(setup, dict) and isinstance(setup.get("universes"), list):
                unis = setup["universes"]
                for idx, val in enumerate(unis):
                    if val == old_name:
                        unis[idx] = new_name
                        modified = True

        update_setup(payload.get("recovery"))
        for template_setup in payload.get("templates", {}).values():
            update_setup(template_setup)

        if modified:
            self._write(payload)

    def save_multi_scan_config(self, selected_templates: list[str], interval_mins: int) -> None:
        payload = self.load_state()
        payload["multi_scan"] = {
            "selected_templates": list(selected_templates),
            "interval_mins": int(interval_mins),
        }
        self._write(payload)

    def load_multi_scan_config(self) -> tuple[list[str], int]:
        payload = self.load_state()
        config = payload.get("multi_scan", {})
        selected = list(config.get("selected_templates", []))
        interval = int(config.get("interval_mins", 5))
        return selected, interval

    def save_strategy_automation_config(self, config: dict[str, Any]) -> None:
        payload = self.load_state()
        payload["strategy_automation"] = copy.deepcopy(config)
        self._write(payload)

    def load_strategy_automation_config(self) -> dict[str, Any]:
        payload = self.load_state()
        config = payload.get("strategy_automation", {})
        return copy.deepcopy(config) if isinstance(config, dict) else {}

    def load_strategy_automation_recipes(self) -> dict[str, Any]:
        """Load all automation recipes, migrating the former single recipe."""
        payload = self.load_state()
        state = payload.get("strategy_automation_recipes")
        if isinstance(state, dict) and isinstance(state.get("recipes"), dict):
            recipes = {
                str(name): normalize_strategy_automation_config(config)
                for name, config in state["recipes"].items()
                if str(name).strip() and isinstance(config, dict)
            }
            active = str(state.get("active", "") or "").strip()
            if active not in recipes:
                active = next(iter(recipes), "")
            return {"active": active, "recipes": recipes}

        legacy = payload.get("strategy_automation", {})
        if isinstance(legacy, dict) and legacy:
            config = normalize_strategy_automation_config(legacy)
            name = str(config.get("recipe_name", "") or "MY STRATEGY GUIDE").strip()
            config["recipe_name"] = name
            config.setdefault("automation_enabled", False)
            return {"active": name, "recipes": {name: config}}
        return {"active": "", "recipes": {}}

    def save_strategy_automation_recipes(self, state: dict[str, Any]) -> None:
        recipes = state.get("recipes", {}) if isinstance(state, dict) else {}
        clean_recipes = {
            str(name).strip(): normalize_strategy_automation_config(config)
            for name, config in recipes.items()
            if str(name).strip() and isinstance(config, dict)
        }
        for name, config in clean_recipes.items():
            config["recipe_name"] = name
        active = str(state.get("active", "") or "").strip()
        if active not in clean_recipes:
            active = next(iter(clean_recipes), "")
        payload = self.load_state()
        payload["strategy_automation_recipes"] = {
            "version": 1,
            "active": active,
            "recipes": clean_recipes,
        }
        # Keep the old field only as a compatibility mirror of the active
        # recipe; older builds can still open a shared data folder safely.
        payload["strategy_automation"] = copy.deepcopy(
            clean_recipes.get(active, {})
        )
        self._write(payload)

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise


SHAREABLE_TEMPLATE_FILE_FORMAT = "ChartSageTemplate"
SHAREABLE_TEMPLATE_VERSION = 1

SHAREABLE_WATCHLIST_FILE_FORMAT = "ChartSageWatchlists"
SHAREABLE_WATCHLIST_VERSION = 1


def export_watchlists_json(
    watchlists: dict[str, list[str]], path: Path
) -> None:
    """Exports custom user watchlists to a shareable JSON file."""
    payload = {
        "format": SHAREABLE_WATCHLIST_FILE_FORMAT,
        "version": SHAREABLE_WATCHLIST_VERSION,
        "exported_at": get_ist_now().isoformat(timespec="seconds"),
        "watchlists": watchlists,
    }
    atomic_write_json(path, payload)


def import_watchlists_json(path: Path) -> dict[str, list[str]]:
    """Imports and validates custom user watchlists from a JSON file."""
    try:
        content = path.read_text(encoding="utf-8")
        payload = json.loads(content)
    except (OSError, json.JSONDecodeError) as exc:
        raise TemplateStoreError(f"Could not read watchlist file: {exc}") from exc

    if not isinstance(payload, dict):
        raise TemplateStoreError("Invalid watchlist file format.")

    watchlists_data = None
    if payload.get("format") == SHAREABLE_WATCHLIST_FILE_FORMAT:
        watchlists_data = payload.get("watchlists")
    elif isinstance(payload, dict):
        watchlists_data = payload.get("watchlists", payload)

    if not isinstance(watchlists_data, dict):
        raise TemplateStoreError("Watchlist file does not contain valid watchlist data.")

    clean_map: dict[str, list[str]] = {}
    for name, symbols in watchlists_data.items():
        if isinstance(name, str) and isinstance(symbols, list):
            clean_symbols = [str(s).strip().upper() for s in symbols if str(s).strip()]
            if name.strip():
                clean_map[name.strip()] = clean_symbols

    if not clean_map:
        raise TemplateStoreError("No valid custom watchlists found in file.")

    return clean_map


def export_template_json(
    template_name: str,
    setup: dict[str, Any],
    path: Path,
    include_watchlists: bool = True,
    watchlist_store: WatchlistStore | None = None,
) -> None:
    """Exports a clean shareable JSON template file."""
    clean_setup = copy.deepcopy(setup)
    clean_setup.pop("template_name", None)

    custom_watchlists_data: dict[str, list[str]] = {}
    if include_watchlists:
        store = watchlist_store or WatchlistStore()
        all_watchlists = store.load_all()
        for uni in setup.get("universes", []):
            if uni in all_watchlists:
                custom_watchlists_data[uni] = list(all_watchlists[uni])

    payload = {
        "format": SHAREABLE_TEMPLATE_FILE_FORMAT,
        "version": SHAREABLE_TEMPLATE_VERSION,
        "template_name": str(template_name).strip(),
        "exported_at": get_ist_now().isoformat(timespec="seconds"),
        "setup": clean_setup,
        "custom_watchlists": custom_watchlists_data,
    }
    atomic_write_json(path, payload)


def import_template_json(path: Path) -> dict[str, dict[str, Any]]:
    """Imports and validates a shareable JSON template file or master scanner_templates.json file."""
    try:
        content = path.read_text(encoding="utf-8")
        payload = json.loads(content)
    except (OSError, json.JSONDecodeError) as exc:
        raise TemplateStoreError(f"Could not read template file: {exc}") from exc
    if not isinstance(payload, dict):
        raise TemplateStoreError("Invalid template file format.")

    templates_map: dict[str, dict[str, Any]] = {}

    # Case 1: Master scanner_templates.json file containing a "templates" dict
    if isinstance(payload.get("templates"), dict):
        raw_templates = payload["templates"]
        for name, setup in raw_templates.items():
            if (
                isinstance(name, str)
                and isinstance(setup, dict)
                and "expression" in setup
            ):
                try:
                    validate_ast(setup["expression"], allow_empty_groups=True)
                    templates_map[name.strip()] = setup
                except ValueError:
                    pass
        if not templates_map:
            raise TemplateStoreError(
                "Master template file contains no valid strategy setups."
            )
        return templates_map

    # Case 2: Single exported template file or setup dict
    setup = None
    name = ""
    if payload.get("format") == SHAREABLE_TEMPLATE_FILE_FORMAT:
        name = str(payload.get("template_name", "")).strip()
        setup = payload.get("setup")
    elif "expression" in payload or "universes" in payload:
        name = str(payload.get("template_name", path.stem)).strip()
        setup = payload

    if not isinstance(setup, dict) or "expression" not in setup:
        raise TemplateStoreError(
            "Template file does not contain a valid strategy setup."
        )

    try:
        validate_ast(setup["expression"], allow_empty_groups=True)
    except ValueError as exc:
        raise TemplateStoreError(
            f"Template contains invalid rule syntax: {exc}"
        ) from exc

    custom_watchlists = payload.get("custom_watchlists")
    if isinstance(custom_watchlists, dict) and custom_watchlists:
        setup["_bundled_custom_watchlists"] = custom_watchlists

    final_name = name or path.stem
    return {final_name: setup}


class TelegramSettingsError(ValueError):
    pass


class TelegramKeyringSaveError(TelegramSettingsError):
    pass


class TelegramSendError(RuntimeError):
    pass


class StateStoreError(ValueError):
    """A persisted state file is unreadable and must not be overwritten."""


class WatchlistStoreError(StateStoreError):
    pass


class HoldingsStoreError(StateStoreError):
    pass


class BrokerLoginStoreError(StateStoreError):
    pass


class OrderSubmissionUncertainError(RuntimeError):
    """The broker may have received an order but did not return a usable response."""


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


class TelegramSettingsStore:
    """Global chat configuration with the token kept outside project JSON."""

    def __init__(
        self,
        path: Path = TELEGRAM_SETTINGS_FILE,
        keyring_module: Any = keyring,
    ):
        self.path = Path(path)
        self.keyring = keyring_module

    def _activate_windows_backend(self) -> bool:
        """Select the bundled Windows vault backend when discovery chose none."""
        if os.name != "nt" or self.keyring is not keyring:
            return False
        try:
            from keyring.backends.Windows import WinVaultKeyring

            backend = WinVaultKeyring()
            if float(getattr(backend, "priority", 0)) <= 0:
                return False
            self.keyring.set_keyring(backend)
            return True
        except Exception:
            return False

    def _keyring_call(self, method_name: str, *arguments: str) -> Any:
        """Call keyring once normally, then retry with WinVault on Windows."""
        try:
            return getattr(self.keyring, method_name)(*arguments)
        except Exception as first_error:
            if not self._activate_windows_backend():
                raise first_error
            return getattr(self.keyring, method_name)(*arguments)

    @staticmethod
    def default_destinations() -> list[dict[str, Any]]:
        return [
            {"key": "group_1", "label": "Group 1", "chat_id": "", "enabled": False},
            {"key": "group_2", "label": "Group 2", "chat_id": "", "enabled": False},
        ]

    def load_destinations(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return self.default_destinations()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TelegramSettingsError(
                f"Telegram settings are malformed and were preserved: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise TelegramSettingsError("Unsupported Telegram settings format.")
        version = payload.get("version")
        if version == 1:
            legacy_chat_id = str(payload.get("chat_id", "")).strip()
            destinations = self.default_destinations()
            destinations[0]["chat_id"] = legacy_chat_id
            destinations[0]["enabled"] = bool(legacy_chat_id)
            return destinations
        if version != TELEGRAM_SETTINGS_VERSION:
            raise TelegramSettingsError("Unsupported Telegram settings format.")
        raw_destinations = payload.get("destinations")
        if not isinstance(raw_destinations, list):
            raise TelegramSettingsError("Telegram destinations are malformed.")
        by_key = {
            str(item.get("key")): item
            for item in raw_destinations
            if isinstance(item, dict)
        }
        destinations = self.default_destinations()
        for destination in destinations:
            saved = by_key.get(destination["key"], {})
            destination["chat_id"] = str(saved.get("chat_id", "")).strip()
            destination["enabled"] = bool(saved.get("enabled", False))
        return destinations

    def load_chat_id(self) -> str:
        destinations = self.load_destinations()
        enabled = [item for item in destinations if item["enabled"]]
        selected = enabled[0] if enabled else destinations[0]
        return str(selected["chat_id"])

    def load_token(self) -> str:
        environment_token = os.environ.get(TELEGRAM_TOKEN_ENV, "").strip()
        if environment_token:
            return environment_token
        try:
            return (
                self._keyring_call(
                    "get_password",
                    TELEGRAM_KEYRING_SERVICE, TELEGRAM_KEYRING_ACCOUNT
                )
                or ""
            ).strip()
        except Exception as exc:
            raise TelegramSettingsError(
                "Could not read the bot token from Windows Credential Manager: "
                f"{exc}"
            ) from exc

    def load(self) -> tuple[str, list[dict[str, Any]]]:
        return self.load_token(), self.load_destinations()

    def save(
        self,
        token: str,
        destinations: Iterable[dict[str, Any]],
        ignore_keyring_errors: bool = False,
    ) -> None:
        clean_token = validate_telegram_token(token)
        supplied = {
            str(item.get("key")): item
            for item in destinations
            if isinstance(item, dict)
        }
        clean_destinations = self.default_destinations()
        for destination in clean_destinations:
            raw = supplied.get(destination["key"], {})
            chat_id = str(raw.get("chat_id", "")).strip()
            enabled = bool(raw.get("enabled", False))
            if chat_id:
                chat_id = validate_telegram_chat_id(chat_id)
            elif enabled:
                raise TelegramSettingsError(
                    f"Enter a Chat ID for enabled {destination['label']}."
                )
            destination["chat_id"] = chat_id
            destination["enabled"] = enabled
        enabled_chat_ids = [
            item["chat_id"] for item in clean_destinations if item["enabled"]
        ]
        if len(enabled_chat_ids) != len(set(enabled_chat_ids)):
            raise TelegramSettingsError(
                "Group 1 and Group 2 cannot use the same enabled Chat ID."
            )
        try:
            self._keyring_call(
                "set_password",
                TELEGRAM_KEYRING_SERVICE,
                TELEGRAM_KEYRING_ACCOUNT,
                clean_token,
            )
        except Exception as exc:
            env_token = os.environ.get(TELEGRAM_TOKEN_ENV, "").strip()
            if not ignore_keyring_errors and not (env_token and clean_token == env_token):
                raise TelegramKeyringSaveError(
                    "Could not save the bot token in Windows Credential Manager: "
                    f"{exc}"
                )
        atomic_write_json(
            self.path,
            {
                "version": TELEGRAM_SETTINGS_VERSION,
                "destinations": clean_destinations,
            },
        )


class AppSettingsStore:
    """App-wide data freshness policy persisted to app_settings.json."""

    APP_SETTINGS_VERSION = 1

    def __init__(self, path: Path = APP_SETTINGS_FILE):
        self.path = Path(path)

    @staticmethod
    def defaults() -> dict[str, Any]:
        return {
            "version": AppSettingsStore.APP_SETTINGS_VERSION,
            "data_refresh_minutes": DEFAULT_REFRESH_MINUTES,
            "force_fresh": True,
            "main_splitter_sizes": [960, 960],
        }

    def load(self) -> dict[str, Any]:
        settings = self.defaults()
        if not self.path.exists():
            return settings
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateStoreError(
                f"App settings file is unreadable and was preserved at {self.path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise StateStoreError(
                f"App settings file has an unsupported structure and was preserved at {self.path}."
            )
        try:
            refresh_minutes = int(
                payload.get("data_refresh_minutes", DEFAULT_REFRESH_MINUTES)
            )
        except (TypeError, ValueError):
            refresh_minutes = DEFAULT_REFRESH_MINUTES
        if refresh_minutes not in REFRESH_MINUTE_CHOICES:
            refresh_minutes = DEFAULT_REFRESH_MINUTES
        force_fresh = bool(payload.get("force_fresh", True))
        raw_splitter_sizes = payload.get("main_splitter_sizes", [960, 960])
        splitter_sizes = [960, 960]
        if isinstance(raw_splitter_sizes, list) and len(raw_splitter_sizes) == 2:
            try:
                parsed_sizes = [max(0, int(value)) for value in raw_splitter_sizes]
                if sum(parsed_sizes) > 0:
                    splitter_sizes = parsed_sizes
            except (TypeError, ValueError):
                pass
        return {
            "version": self.APP_SETTINGS_VERSION,
            "data_refresh_minutes": refresh_minutes,
            "force_fresh": force_fresh,
            "main_splitter_sizes": splitter_sizes,
        }

    def save(
        self,
        refresh_minutes: int = DEFAULT_REFRESH_MINUTES,
        force_fresh: bool = True,
        main_splitter_sizes: list[int] | None = None,
    ) -> None:
        try:
            refresh_minutes = int(refresh_minutes)
        except (TypeError, ValueError):
            refresh_minutes = DEFAULT_REFRESH_MINUTES
        if refresh_minutes not in REFRESH_MINUTE_CHOICES:
            refresh_minutes = DEFAULT_REFRESH_MINUTES
        if main_splitter_sizes is None:
            try:
                main_splitter_sizes = self.load().get(
                    "main_splitter_sizes", [960, 960]
                )
            except StateStoreError:
                main_splitter_sizes = [960, 960]
        clean_splitter_sizes = [960, 960]
        if isinstance(main_splitter_sizes, list) and len(main_splitter_sizes) == 2:
            try:
                parsed_sizes = [max(0, int(value)) for value in main_splitter_sizes]
                if sum(parsed_sizes) > 0:
                    clean_splitter_sizes = parsed_sizes
            except (TypeError, ValueError):
                pass
        atomic_write_json(
            self.path,
            {
                "version": self.APP_SETTINGS_VERSION,
                "data_refresh_minutes": refresh_minutes,
                "force_fresh": bool(force_fresh),
                "main_splitter_sizes": clean_splitter_sizes,
            },
        )


class PaperTradesStoreError(StateStoreError):
    pass


def paper_lookup_holding(data: dict[str, Any], account: str, symbol: str) -> dict[str, Any] | None:
    """Return a HoldingsStore-compatible {'qty', 'buy_price'} map for an open paper position."""
    acc = data.get("accounts", {}).get(str(account).strip().upper(), {})
    pos = acc.get("positions", {}).get(str(symbol).strip().upper())
    if not pos or float(pos.get("qty", 0)) <= 0 or float(pos.get("avg_price", 0)) <= 0:
        return None
    return {"qty": float(pos["qty"]), "buy_price": float(pos["avg_price"])}


def paper_fee_estimate(value: float, fee_pct: float = PAPER_FEE_PCT) -> float:
    return round(max(0.0, float(value)) * float(fee_pct) / 100.0, 2)


class PaperTradesStore:
    """Persistent simulated (paper) trading ledger in the SQLite database paper_trades.db.

    The ledger table is append-only and is the authoritative source of truth. Cash,
    open positions, realized P&L and fees are derived values rebuilt from the ledger
    whenever the store is loaded or an account is reset, preserving last marks. A
    legacy paper_trades.json is imported once on first use and then backed up.
    """

    VERSION = 1

    LEDGER_COLUMNS = (
        "account", "action", "symbol", "qty", "fill_price", "value", "fees",
        "scan_id", "template", "matched_rule", "order_type", "product",
        "capital_mode", "slot", "slots", "realized_pnl", "holding_days", "ts_ist",
    )

    def __init__(self, path: Path = PAPER_TRADES_DB):
        self.path = Path(path)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _ensure_schema(self) -> None:
        self._migrate()

    @staticmethod
    def empty_account() -> dict[str, Any]:
        return {
            "starting_cash": PAPER_STARTING_CASH,
            "cash": PAPER_STARTING_CASH,
            "positions": {},
            "ledger": [],
            "realized_pnl": 0.0,
            "fees_paid": 0.0,
        }

    def _migrate(self) -> None:
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS accounts (
                        uid TEXT PRIMARY KEY,
                        starting_cash REAL NOT NULL DEFAULT 1000000,
                        fee_pct REAL NOT NULL DEFAULT 0.10,
                        display_name TEXT,
                        kind TEXT,
                        created_ts_ist TEXT)"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS ledger (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        account TEXT NOT NULL,
                        action TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        qty REAL NOT NULL,
                        fill_price REAL NOT NULL,
                        value REAL NOT NULL DEFAULT 0,
                        fees REAL NOT NULL DEFAULT 0,
                        scan_id TEXT,
                        template TEXT,
                        matched_rule TEXT,
                        order_type TEXT,
                        product TEXT,
                        capital_mode TEXT,
                        slot INTEGER,
                        slots INTEGER,
                        realized_pnl REAL,
                        holding_days INTEGER,
                        ts_ist TEXT NOT NULL)"""
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_account ON ledger(account)")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ledger_account_symbol ON ledger(account, symbol)"
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_scan ON ledger(scan_id)")
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS positions (
                        account TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        qty REAL NOT NULL,
                        avg_price REAL NOT NULL,
                        opened_ts_ist TEXT,
                        template TEXT,
                        last_mark REAL,
                        PRIMARY KEY (account, symbol))"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL)"""
                )
                conn.execute("PRAGMA user_version=1")
                cols = {
                    str(r["name"]) for r in conn.execute("PRAGMA table_info(accounts)")
                }
                if "fee_pct" not in cols:
                    conn.execute(
                        "ALTER TABLE accounts ADD COLUMN fee_pct REAL NOT NULL DEFAULT 0.10"
                    )
                if "display_name" not in cols:
                    conn.execute("ALTER TABLE accounts ADD COLUMN display_name TEXT")
                if "kind" not in cols:
                    conn.execute("ALTER TABLE accounts ADD COLUMN kind TEXT")
                conn.execute(
                    "UPDATE accounts SET kind='legacy' WHERE kind IS NULL"
                )
            if self._ledger_count(conn) == 0:
                self._import_json_backup(conn)

    def _ledger_count(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COUNT(*) FROM ledger").fetchone()
        return int(row[0]) if row else 0

    def get_settings(self) -> dict[str, float]:
        """User-settable paper trading defaults from the settings table."""
        try:
            self._ensure_schema()
            with closing(self._connect()) as conn:
                rows = {
                    str(row["key"]): str(row["value"])
                    for row in conn.execute("SELECT key, value FROM settings")
                }
        except (sqlite3.Error, OSError):
            rows = {}
        try:
            starting_cash = float(rows.get("paper_starting_cash", PAPER_STARTING_CASH))
        except (TypeError, ValueError):
            starting_cash = PAPER_STARTING_CASH
        try:
            fee_pct = float(rows.get("paper_fee_pct", PAPER_FEE_PCT))
        except (TypeError, ValueError):
            fee_pct = PAPER_FEE_PCT
        return {"starting_cash": starting_cash, "fee_pct": fee_pct}

    def save_settings(self, starting_cash: float, fee_pct: float) -> None:
        """Persist the paper trading defaults (validated)."""
        try:
            starting_cash = round(float(starting_cash), 2)
        except (TypeError, ValueError):
            raise PaperTradesStoreError("Paper starting cash must be a number.")
        try:
            fee_pct = round(float(fee_pct), 2)
        except (TypeError, ValueError):
            raise PaperTradesStoreError("Paper fee percentage must be a number.")
        if starting_cash < PAPER_MIN_STARTING_CASH:
            raise PaperTradesStoreError(
                f"Paper starting cash must be at least ₹ {format_number(PAPER_MIN_STARTING_CASH)}."
            )
        fee_pct = max(0.0, min(10.0, fee_pct))
        with _PAPER_STORE_LOCK:
            self._ensure_schema()
            with closing(self._connect()) as conn:
                with conn:
                    conn.execute(
                        "INSERT INTO settings(key, value) VALUES(?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        ("paper_starting_cash", f"{starting_cash:.2f}"),
                    )
                    conn.execute(
                        "INSERT INTO settings(key, value) VALUES(?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        ("paper_fee_pct", f"{fee_pct:.2f}"),
                    )

    def get_account_settings(self, uid: str) -> dict[str, float]:
        """Per-account starting cash and fee (falls back to the global defaults)."""
        key = str(uid).strip().upper()
        defaults = self.get_settings()
        try:
            self._ensure_schema()
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT starting_cash, fee_pct FROM accounts WHERE uid=?", (key,)
                ).fetchone()
        except (sqlite3.Error, OSError):
            row = None
        if not row:
            return defaults
        try:
            starting_cash = (
                float(row["starting_cash"])
                if row["starting_cash"] is not None
                else defaults["starting_cash"]
            )
        except (TypeError, ValueError):
            starting_cash = defaults["starting_cash"]
        try:
            fee_pct = (
                float(row["fee_pct"])
                if row["fee_pct"] is not None
                else defaults["fee_pct"]
            )
        except (TypeError, ValueError):
            fee_pct = defaults["fee_pct"]
        return {"starting_cash": starting_cash, "fee_pct": fee_pct}

    def create_paper_account(
        self, display_name: str, starting_cash: float, fee_pct: float
    ) -> str:
        """Create a named virtual paper account and return its uid."""
        display_name = str(display_name).strip()
        if not display_name:
            raise PaperTradesStoreError("Paper account name is required.")
        try:
            starting_cash = round(float(starting_cash), 2)
        except (TypeError, ValueError):
            raise PaperTradesStoreError("Paper starting cash must be a number.")
        if starting_cash < PAPER_MIN_STARTING_CASH:
            raise PaperTradesStoreError(
                f"Paper starting cash must be at least ₹ {format_number(PAPER_MIN_STARTING_CASH)}."
            )
        try:
            fee_pct = round(float(fee_pct), 2)
        except (TypeError, ValueError):
            raise PaperTradesStoreError("Paper fee percentage must be a number.")
        fee_pct = max(0.0, min(10.0, fee_pct))
        with _PAPER_STORE_LOCK:
            self._ensure_schema()
            base = f"PAPER-{int(_time.time() * 1000)}"
            uid = base
            counter = 0
            with closing(self._connect()) as conn:
                with conn:
                    while conn.execute(
                        "SELECT 1 FROM accounts WHERE uid=?", (uid,)
                    ).fetchone():
                        counter += 1
                        uid = f"{base}-{counter}"
                    conn.execute(
                        "INSERT INTO accounts(uid, starting_cash, fee_pct, display_name, kind, created_ts_ist)"
                        " VALUES(?,?,?,?,?,?)",
                        (uid, starting_cash, fee_pct, display_name, "paper", ""),
                    )
        return uid

    def update_paper_account(
        self,
        uid: str,
        display_name: str | None = None,
        starting_cash: float | None = None,
        fee_pct: float | None = None,
    ) -> None:
        key = str(uid).strip().upper()
        updates: list[str] = []
        params: list[Any] = []
        if display_name is not None:
            name = str(display_name).strip()
            if not name:
                raise PaperTradesStoreError("Paper account name is required.")
            updates.append("display_name=?")
            params.append(name)
        if starting_cash is not None:
            try:
                cash = round(float(starting_cash), 2)
            except (TypeError, ValueError):
                raise PaperTradesStoreError("Paper starting cash must be a number.")
            if cash < PAPER_MIN_STARTING_CASH:
                raise PaperTradesStoreError(
                    f"Paper starting cash must be at least ₹ {format_number(PAPER_MIN_STARTING_CASH)}."
                )
            updates.append("starting_cash=?")
            params.append(cash)
        if fee_pct is not None:
            try:
                fee = round(float(fee_pct), 2)
            except (TypeError, ValueError):
                raise PaperTradesStoreError("Paper fee percentage must be a number.")
            fee = max(0.0, min(10.0, fee))
            updates.append("fee_pct=?")
            params.append(fee)
        if not updates:
            return
        params.append(key)
        with _PAPER_STORE_LOCK:
            self._ensure_schema()
            with closing(self._connect()) as conn:
                with conn:
                    conn.execute(
                        f"UPDATE accounts SET {', '.join(updates)} WHERE uid=?", params
                    )

    def delete_paper_account(self, uid: str) -> None:
        key = str(uid).strip().upper()
        with _PAPER_STORE_LOCK:
            self._ensure_schema()
            with closing(self._connect()) as conn:
                with conn:
                    conn.execute("DELETE FROM ledger WHERE account=?", (key,))
                    conn.execute("DELETE FROM positions WHERE account=?", (key,))
                    conn.execute("DELETE FROM accounts WHERE uid=?", (key,))

    def list_paper_accounts(self) -> list[dict[str, Any]]:
        """Named virtual paper accounts (kind='paper'), excluding legacy UID accounts."""
        self._ensure_schema()
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT uid, display_name, starting_cash, fee_pct FROM accounts "
                "WHERE kind='paper' ORDER BY display_name COLLATE NOCASE, uid"
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            uid = str(row["uid"])
            result.append(
                {
                    "uid": uid,
                    "display_name": str(row["display_name"] or uid),
                    "starting_cash": float(
                        row["starting_cash"]
                        if row["starting_cash"] is not None
                        else PAPER_STARTING_CASH
                    ),
                    "fee_pct": float(
                        row["fee_pct"]
                        if row["fee_pct"] is not None
                        else PAPER_FEE_PCT
                    ),
                }
            )
        return result

    def _import_json_backup(self, conn: sqlite3.Connection) -> None:
        json_path = self.path.parent / "paper_trades.json"
        if json_path.resolve() == self.path.resolve() or not json_path.exists():
            return
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        with conn:
            for uid_raw, acc in (payload.get("accounts") or {}).items():
                if not isinstance(acc, dict):
                    continue
                uid = str(uid_raw).strip().upper()
                try:
                    starting_cash = float(acc.get("starting_cash", PAPER_STARTING_CASH))
                except (TypeError, ValueError):
                    starting_cash = PAPER_STARTING_CASH
                conn.execute(
                    "INSERT OR IGNORE INTO accounts(uid, starting_cash, created_ts_ist) VALUES(?,?,?)",
                    (uid, starting_cash, ""),
                )
                first_ts = ""
                for entry in acc.get("ledger", []) or []:
                    if isinstance(entry, dict):
                        self._insert_ledger_row(conn, uid, entry)
                        first_ts = first_ts or str(entry.get("ts_ist", ""))
                if first_ts:
                    conn.execute(
                        "UPDATE accounts SET created_ts_ist=? WHERE uid=?", (first_ts, uid)
                    )
                state, _marks = self._replay_account(conn, uid)
                for sym_raw, pos in (acc.get("positions") or {}).items():
                    if isinstance(pos, dict) and pos.get("last_mark"):
                        try:
                            mark = float(pos["last_mark"])
                        except (TypeError, ValueError):
                            continue
                        if mark > 0:
                            sym = str(sym_raw).strip().upper()
                            if sym in state["positions"]:
                                state["positions"][sym]["last_mark"] = round(mark, 2)
                self._persist_positions(conn, uid, state)
            backup = self.path.parent / f"paper_trades_backup_{datetime.now():%Y%m%d_%H%M%S}.json"
            try:
                json_path.replace(backup)
            except OSError:
                pass

    def _insert_ledger_row(
        self, conn: sqlite3.Connection, account: str, entry: dict[str, Any]
    ) -> None:
        try:
            qty = float(entry.get("qty", 0) or 0)
            fill_price = float(entry.get("fill_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            qty = 0.0
            fill_price = 0.0
        try:
            value = float(entry.get("value", qty * fill_price) or 0.0)
            fees = float(entry.get("fees", 0.0) or 0.0)
        except (TypeError, ValueError):
            value = 0.0
            fees = 0.0
        values: list[Any] = [
            account,
            str(entry.get("action", "")).strip().upper(),
            str(entry.get("symbol", "")).strip().upper(),
            qty,
            fill_price,
            value,
            fees,
            str(entry.get("scan_id", "")),
            str(entry.get("template", "")),
            str(entry.get("matched_rule", "")),
            str(entry.get("order_type", "LIMIT")),
            str(entry.get("product", "CNC")),
            str(entry.get("capital_mode", "")),
            entry.get("slot"),
            entry.get("slots"),
            entry.get("realized_pnl"),
            entry.get("holding_days"),
            str(entry.get("ts_ist", "")),
        ]
        cols = ", ".join(self.LEDGER_COLUMNS)
        ph = ", ".join("?" * len(self.LEDGER_COLUMNS))
        conn.execute(f"INSERT INTO ledger({cols}) VALUES({ph})", values)

    def _entry_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": f"PT-{int(row['id']):04d}",
            "account": row["account"],
            "action": row["action"],
            "symbol": row["symbol"],
            "qty": row["qty"],
            "fill_price": row["fill_price"],
            "value": row["value"],
            "fees": row["fees"],
            "scan_id": row["scan_id"],
            "template": row["template"],
            "matched_rule": row["matched_rule"],
            "order_type": row["order_type"],
            "product": row["product"],
            "capital_mode": row["capital_mode"],
            "slot": row["slot"],
            "slots": row["slots"],
            "realized_pnl": row["realized_pnl"],
            "holding_days": row["holding_days"],
            "ts_ist": row["ts_ist"],
        }

    def _replay_account(
        self, conn: sqlite3.Connection, uid: str
    ) -> tuple[dict[str, Any], dict[str, float]]:
        acc = self.empty_account()
        row = conn.execute("SELECT starting_cash FROM accounts WHERE uid=?", (uid,)).fetchone()
        if row:
            acc["starting_cash"] = float(row["starting_cash"])
        for db_row in conn.execute(
            "SELECT * FROM ledger WHERE account=? ORDER BY id", (uid,)
        ):
            acc["ledger"].append(self._entry_from_row(db_row))
        acc["cash"] = float(acc["starting_cash"])
        acc["positions"] = {}
        acc["realized_pnl"] = 0.0
        acc["fees_paid"] = 0.0
        for entry in acc["ledger"]:
            self._apply_entry_locked(acc, entry)
        marks: dict[str, float] = {}
        for db_row in conn.execute(
            "SELECT symbol, last_mark FROM positions WHERE account=?", (uid,)
        ):
            mark = db_row["last_mark"]
            if mark is not None:
                try:
                    mark = float(mark)
                except (TypeError, ValueError):
                    continue
                if mark > 0:
                    marks[str(db_row["symbol"]).strip().upper()] = mark
        for sym, mark in marks.items():
            if sym in acc["positions"]:
                acc["positions"][sym]["last_mark"] = mark
        return acc, marks

    def _persist_positions(
        self, conn: sqlite3.Connection, uid: str, acc: dict[str, Any]
    ) -> None:
        conn.execute("DELETE FROM positions WHERE account=?", (uid,))
        for sym, pos in acc.get("positions", {}).items():
            conn.execute(
                "INSERT INTO positions(account, symbol, qty, avg_price, opened_ts_ist, template, last_mark)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    uid,
                    str(sym).strip().upper(),
                    float(pos.get("qty", 0)),
                    float(pos.get("avg_price", 0.0)),
                    str(pos.get("opened", "")),
                    str(pos.get("template", "")),
                    pos.get("last_mark"),
                ),
            )
        conn.execute(
            "UPDATE accounts SET starting_cash=? WHERE uid=?", (float(acc["starting_cash"]), uid)
        )

    def load(self) -> dict[str, Any]:
        try:
            self._ensure_schema()
        except sqlite3.Error as exc:
            raise PaperTradesStoreError(
                f"Paper trading database is unreadable and was preserved at {self.path}: {exc}"
            ) from exc
        if not self.path.exists():
            return {"version": self.VERSION, "accounts": {}}
        try:
            with closing(self._connect()) as conn:
                accounts: dict[str, Any] = {}
                for row in conn.execute("SELECT uid FROM accounts ORDER BY uid"):
                    uid = str(row["uid"]).strip().upper()
                    acc, _marks = self._replay_account(conn, uid)
                    accounts[uid] = acc
                return {"version": self.VERSION, "accounts": accounts}
        except sqlite3.Error as exc:
            raise PaperTradesStoreError(
                f"Paper trading database is unreadable and was preserved at {self.path}: {exc}"
            ) from exc

    @staticmethod
    def _apply_entry_locked(acc: dict[str, Any], entry: dict[str, Any]) -> None:
        try:
            symbol = str(entry.get("symbol", "")).strip().upper()
            action = str(entry.get("action", "")).strip().upper()
            qty = int(entry.get("qty", 0))
            price = float(entry.get("fill_price", 0.0))
            fees = float(entry.get("fees", 0.0))
            value = float(entry.get("value", qty * price))
        except (TypeError, ValueError):
            return
        if action == "BUY" and qty > 0 and price > 0:
            acc["cash"] = float(acc["cash"]) - value - fees
            pos = acc["positions"].setdefault(
                symbol,
                {"qty": 0, "avg_price": 0.0, "opened": "", "template": "", "last_mark": None},
            )
            old_qty = int(pos.get("qty", 0))
            old_avg = float(pos.get("avg_price", 0.0))
            pos["qty"] = old_qty + qty
            pos["avg_price"] = (
                (old_avg * old_qty + price * qty) / (old_qty + qty) if (old_qty + qty) else 0.0
            )
            pos["opened"] = pos.get("opened") or str(entry.get("ts_ist", ""))
            pos["template"] = str(entry.get("template", "")) or pos.get("template", "")
            acc["fees_paid"] = float(acc["fees_paid"]) + fees
        elif action == "SELL" and qty > 0 and price > 0:
            pos = acc["positions"].get(symbol)
            if not pos:
                return
            sellable = min(qty, int(pos.get("qty", 0)))
            if sellable <= 0:
                return
            avg = float(pos.get("avg_price", 0.0))
            value = round(sellable * price, 2)
            fees = float(entry.get("fees", 0.0))
            acc["cash"] = float(acc["cash"]) + value - fees
            acc["realized_pnl"] = float(acc["realized_pnl"]) + (price - avg) * sellable - fees
            acc["fees_paid"] = float(acc["fees_paid"]) + fees
            pos["qty"] = int(pos.get("qty", 0)) - sellable
            if pos["qty"] <= 0:
                del acc["positions"][symbol]

    def accounts(self) -> dict[str, Any]:
        return self.load().get("accounts", {})

    def record_trade(
        self,
        uid: str,
        *,
        symbol: str,
        action: str,
        qty: int,
        price: float,
        template: str = "",
        matched_rule: str = "",
        scan_id: str = "",
        order_type: str = "LIMIT",
        product: str = "CNC",
        capital_mode: str = "",
        slot: int | None = None,
        slots: int | None = None,
        sell_percent: float | None = None,
    ) -> dict[str, Any]:
        with _PAPER_STORE_LOCK:
            key = str(uid).strip().upper()
            symbol = str(symbol).strip().upper()
            action = str(action).strip().upper()
            if action not in ("BUY", "SELL"):
                return {"placed": False, "reason": f"Unsupported paper action {action!r}."}
            try:
                price = float(price)
            except (TypeError, ValueError):
                return {"placed": False, "reason": "Invalid paper fill price."}
            if price <= 0:
                return {"placed": False, "reason": "Invalid paper fill price."}
            try:
                self._ensure_schema()
                defaults = self.get_settings()
                with closing(self._connect()) as conn:
                    with conn:
                        if conn.execute("SELECT 1 FROM accounts WHERE uid=?", (key,)).fetchone() is None:
                            conn.execute(
                                "INSERT OR IGNORE INTO accounts(uid, starting_cash, fee_pct, display_name, kind, created_ts_ist)"
                                " VALUES(?,?,?,?,?,?)",
                                (key, defaults["starting_cash"], defaults["fee_pct"], key, "paper", ""),
                            )
                        fee_row = conn.execute(
                            "SELECT fee_pct FROM accounts WHERE uid=?", (key,)
                        ).fetchone()
                        fee_pct = (
                            float(fee_row["fee_pct"])
                            if fee_row and fee_row["fee_pct"] is not None
                            else defaults["fee_pct"]
                        )
                        acc, _marks = self._replay_account(conn, key)
                        if scan_id:
                            if conn.execute(
                                "SELECT 1 FROM ledger WHERE account=? AND scan_id=? AND symbol=? AND action=? LIMIT 1",
                                (key, scan_id, symbol, action),
                            ).fetchone():
                                return {"placed": False, "reason": "Already processed for this scan."}
                        ts = get_ist_now().strftime("%Y-%m-%d %H:%M:%S")
                        if action == "BUY":
                            if acc["positions"].get(symbol) and int(acc["positions"][symbol].get("qty", 0)) > 0:
                                return {"placed": False, "reason": f"{symbol} is already open on paper."}
                            qty = int(qty)
                            if qty < 1:
                                return {"placed": False, "reason": "Quantity must be at least 1."}
                            value = round(qty * price, 2)
                            fees = paper_fee_estimate(value, fee_pct)
                            if acc["cash"] < value + fees:
                                return {
                                    "placed": False,
                                    "reason": (
                                        f"Insufficient paper cash (need ₹ {format_number(value + fees)}; "
                                        f"have ₹ {format_number(acc['cash'])})."
                                    ),
                                }
                            acc["cash"] = round(float(acc["cash"]) - value - fees, 2)
                            pos = acc["positions"].setdefault(
                                symbol,
                                {"qty": 0, "avg_price": 0.0, "opened": "", "template": "", "last_mark": None},
                            )
                            old_qty = int(pos.get("qty", 0))
                            old_avg = float(pos.get("avg_price", 0.0))
                            pos["qty"] = old_qty + qty
                            pos["avg_price"] = round(
                                (old_avg * old_qty + price * qty) / (old_qty + qty) if (old_qty + qty) else 0.0, 2
                            )
                            pos["opened"] = pos.get("opened") or ts
                            pos["template"] = str(template) or pos.get("template", "")
                            pos["last_mark"] = round(price, 2)
                            realized: float | None = None
                            holding_days: int | None = None
                        else:
                            pos = acc["positions"].get(symbol)
                            available = int(pos.get("qty", 0)) if pos else 0
                            if available <= 0:
                                return {"placed": False, "reason": f"No open paper position for {symbol}."}
                            if sell_percent is not None:
                                pct = min(100.0, max(1.0, float(sell_percent)))
                                sellable = min(available, math.ceil(available * pct / 100.0))
                            else:
                                sellable = min(available, int(qty) if qty else available)
                            if sellable <= 0:
                                return {"placed": False, "reason": f"No open paper position quantity for {symbol}."}
                            opened_ts = str(pos.get("opened", ""))
                            value = round(sellable * price, 2)
                            fees = paper_fee_estimate(value, fee_pct)
                            avg = float(pos.get("avg_price", 0.0))
                            realized = round((price - avg) * sellable - fees, 2)
                            acc["cash"] = round(float(acc["cash"]) + value - fees, 2)
                            acc["realized_pnl"] = round(float(acc["realized_pnl"]) + realized, 2)
                            pos["qty"] = available - sellable
                            if pos["qty"] <= 0:
                                del acc["positions"][symbol]
                            holding_days = None
                            try:
                                if opened_ts:
                                    opened_date = datetime.strptime(opened_ts, "%Y-%m-%d %H:%M:%S").date()
                                    holding_days = max(0, (get_ist_now().date() - opened_date).days)
                            except (ValueError, TypeError):
                                holding_days = None
                            qty = sellable
                        acc["fees_paid"] = round(float(acc["fees_paid"]) + fees, 2)
                        entry = {
                            "id": f"PT-{len(acc['ledger']) + 1:04d}",
                            "ts_ist": ts,
                            "template": str(template),
                            "account": key,
                            "symbol": symbol,
                            "action": action,
                            "qty": qty,
                            "fill_price": round(price, 2),
                            "value": round(value, 2),
                            "fees": round(fees, 2),
                            "order_type": str(order_type),
                            "product": str(product),
                            "capital_mode": str(capital_mode),
                            "slot": slot,
                            "slots": slots,
                            "matched_rule": str(matched_rule),
                            "scan_id": str(scan_id),
                            "realized_pnl": realized,
                            "holding_days": holding_days,
                        }
                        acc["ledger"].append(entry)
                        self._insert_ledger_row(conn, key, entry)
                        self._persist_positions(conn, key, acc)
                        return {
                            "placed": True,
                            "entry": entry,
                            "cash": acc["cash"],
                            "realized_pnl": acc["realized_pnl"],
                        }
            except sqlite3.Error as exc:
                raise PaperTradesStoreError(
                    f"Failed to record the paper trade at {self.path}: {exc}"
                ) from exc

    def mark_many(self, uid: str, marks: dict[str, float]) -> None:
        with _PAPER_STORE_LOCK:
            key = str(uid).strip().upper()
            try:
                self._ensure_schema()
                with closing(self._connect()) as conn:
                    with conn:
                        if conn.execute("SELECT 1 FROM accounts WHERE uid=?", (key,)).fetchone() is None:
                            return
                        for sym_raw, price in marks.items():
                            try:
                                price = float(price)
                            except (TypeError, ValueError):
                                continue
                            if price <= 0:
                                continue
                            conn.execute(
                                "UPDATE positions SET last_mark=? WHERE account=? AND symbol=?",
                                (round(price, 2), key, str(sym_raw).strip().upper()),
                            )
            except sqlite3.Error as exc:
                raise PaperTradesStoreError(
                    f"Failed to update paper marks at {self.path}: {exc}"
                ) from exc

    def reset_account(self, uid: str) -> None:
        with _PAPER_STORE_LOCK:
            key = str(uid).strip().upper()
            try:
                self._ensure_schema()
                defaults_cash = self.get_settings()["starting_cash"]
                with closing(self._connect()) as conn:
                    with conn:
                        row = conn.execute(
                            "SELECT starting_cash FROM accounts WHERE uid=?", (key,)
                        ).fetchone()
                        starting_cash = (
                            float(row["starting_cash"])
                            if row and row["starting_cash"] is not None
                            else defaults_cash
                        )
                        conn.execute(
                            "INSERT OR IGNORE INTO accounts(uid, starting_cash, created_ts_ist)"
                            " VALUES(?,?,?)",
                            (key, starting_cash, ""),
                        )
                        conn.execute(
                            "UPDATE accounts SET starting_cash=? WHERE uid=?",
                            (starting_cash, key),
                        )
                        conn.execute("DELETE FROM positions WHERE account=?", (key,))
                        conn.execute("DELETE FROM ledger WHERE account=?", (key,))
            except sqlite3.Error as exc:
                raise PaperTradesStoreError(
                    f"Failed to reset the paper account at {self.path}: {exc}"
                ) from exc

    def save(self, data: dict[str, Any] | None = None) -> None:
        """Compatibility no-op: every mutation commits within its own transaction."""


class WatchlistStore:
    """Read and write custom user watchlists to a JSON file."""

    def __init__(self, path: Path = WATCHLISTS_FILE):
        self.path = Path(path)

    def load(self) -> dict[str, list[str]]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WatchlistStoreError(
                f"Watchlist file is unreadable and was preserved at {self.path}: {exc}"
            ) from exc
        if not isinstance(payload, dict) or any(
            not isinstance(symbols, list) for symbols in payload.values()
        ):
            raise WatchlistStoreError(
                f"Watchlist file has an unsupported structure and was preserved at {self.path}."
            )
        return {
            str(name): [str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()]
            for name, symbols in payload.items()
        }

    def load_all(self) -> dict[str, list[str]]:
        return self.load()

    def save(self, watchlists: dict[str, list[str]]) -> None:
        if self.path.exists():
            self.load()  # Refuse to overwrite an unreadable existing file.
        atomic_write_json(self.path, watchlists)


class BrokerLoginStore:
    """Persist broker account metadata in JSON and secrets in the OS keyring."""

    def __init__(self, path: Path = BROKER_LOGINS_FILE):
        self.path = Path(path)
        self._load_error: str | None = None

    @staticmethod
    def _secret_key(broker: str, user_id: str, field: str) -> str:
        return f"{str(broker).strip().lower()}:{str(user_id).strip().upper()}:{field}"

    def _load_secret(self, broker: str, user_id: str, field: str) -> str:
        try:
            return str(
                keyring.get_password(
                    BROKER_KEYRING_SERVICE,
                    self._secret_key(broker, user_id, field),
                )
                or ""
            )
        except Exception as exc:
            raise BrokerLoginStoreError(
                "Could not read broker credentials from the OS keyring; account "
                "changes are blocked to prevent accidental credential deletion."
            ) from exc

    def _save_secret(self, broker: str, user_id: str, field: str, value: str) -> None:
        clean = str(value).strip()
        if clean:
            keyring.set_password(
                BROKER_KEYRING_SERVICE,
                self._secret_key(broker, user_id, field),
                clean,
            )

    def load_all(self) -> dict[str, list[dict[str, str]]]:
        if not self.path.exists():
            self._load_error = None
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                result: dict[str, list[dict[str, str]]] = {}
                migrated = False
                for broker, accounts in payload.items():
                    if not isinstance(accounts, list):
                        raise ValueError(f"broker {broker!r} account collection is not a list")
                    loaded_accounts: list[dict[str, str]] = []
                    for acc in accounts:
                        if not isinstance(acc, dict):
                            raise ValueError(f"broker {broker!r} contains a non-object account")
                        user_id = str(acc.get("user_id", "")).strip().upper()
                        loaded = {
                            "label": str(acc.get("label", "")),
                            "user_id": user_id,
                            "token_updated_at": str(acc.get("token_updated_at", "")),
                        }
                        for field in ("password", "totp_secret", "enctoken"):
                            legacy = str(acc.get(field, ""))
                            secure = self._load_secret(str(broker), user_id, field)
                            loaded[field] = secure or legacy
                            if legacy and user_id:
                                try:
                                    self._save_secret(str(broker), user_id, field, legacy)
                                except Exception:
                                    continue
                                acc[field] = ""
                                migrated = True
                        loaded_accounts.append(loaded)
                    result[str(broker)] = loaded_accounts
                if migrated:
                    try:
                        atomic_write_json(self.path, payload)
                    except OSError:
                        # Secrets are already available from the keyring/result;
                        # a read-only legacy file must not hide the accounts.
                        pass
                self._load_error = None
                return result
            raise ValueError("top-level value is not an object")
        except Exception as exc:
            self._load_error = (
                f"Broker login file is unreadable and was preserved at {self.path}: {exc}"
            )
        return {}

    def get_broker_accounts(self, broker: str = "Zerodha") -> list[dict[str, str]]:
        return self.load_all().get(broker, [])

    def save_broker_accounts(self, broker: str, accounts: list[dict[str, str]]) -> None:
        data = self.load_all()
        if self._load_error:
            raise BrokerLoginStoreError(self._load_error)
        previous_user_ids = {
            str(account.get("user_id", "")).strip().upper()
            for account in data.get(broker, [])
            if str(account.get("user_id", "")).strip()
        }
        next_user_ids = {
            str(account.get("user_id", "")).strip().upper()
            for account in accounts
            if str(account.get("user_id", "")).strip()
        }
        data[broker] = accounts
        serialized: dict[str, list[dict[str, str]]] = {}
        for broker_name, broker_accounts in data.items():
            clean_accounts: list[dict[str, str]] = []
            for account in broker_accounts:
                clean = dict(account)
                user_id = str(clean.get("user_id", "")).strip().upper()
                if not user_id:
                    continue
                for field in ("password", "totp_secret", "enctoken"):
                    secret_value = str(clean.get(field, ""))
                    if secret_value.strip():
                        self._save_secret(
                            broker_name, user_id, field, secret_value
                        )
                    else:
                        try:
                            keyring.delete_password(
                                BROKER_KEYRING_SERVICE,
                                self._secret_key(broker_name, user_id, field),
                            )
                        except Exception as exc:
                            if type(exc).__name__ != "PasswordDeleteError":
                                raise
                    clean[field] = ""
                clean_accounts.append(clean)
            serialized[broker_name] = clean_accounts
        atomic_write_json(self.path, serialized)
        for removed_user_id in previous_user_ids - next_user_ids:
            for field in ("password", "totp_secret", "enctoken"):
                try:
                    keyring.delete_password(
                        BROKER_KEYRING_SERVICE,
                        self._secret_key(broker, removed_user_id, field),
                    )
                except Exception:
                    pass

    def save_token_for_user(self, user_id: str, enctoken: str, broker: str = "Zerodha") -> None:
        u_id = user_id.strip().upper()
        if not u_id or not enctoken:
            return
        accounts = self.get_broker_accounts(broker)
        updated = False
        for acc in accounts:
            if str(acc.get("user_id", "")).strip().upper() == u_id:
                acc["enctoken"] = enctoken
                acc["token_updated_at"] = get_ist_now().isoformat()
                updated = True
                break
        if not updated:
            accounts.append({
                "label": f"Zerodha ({u_id})",
                "user_id": u_id,
                "password": "",
                "totp_secret": "",
                "enctoken": enctoken,
                "token_updated_at": get_ist_now().isoformat(),
            })
        self.save_broker_accounts(broker, accounts)

    def get_token_for_user(self, user_id: str, broker: str = "Zerodha") -> str:
        u_id = user_id.strip().upper()
        if not u_id:
            return ""
        for acc in self.get_broker_accounts(broker):
            if str(acc.get("user_id", "")).strip().upper() == u_id:
                return str(acc.get("enctoken", "")).strip()
        return ""


def migrate_legacy_broker_secrets() -> list[str]:
    """Move copied legacy secrets to keyring and redact recoverable old files."""
    migrated: list[str] = []
    cleanup_errors: list[str] = []
    store = BrokerLoginStore()
    store.load_all()  # Performs the keyring migration for the new metadata file.

    if APP_DIR.resolve() != DATA_DIR.resolve():
        legacy_logins = APP_DIR / "broker_logins.json"
        if legacy_logins.is_file() and BROKER_LOGINS_FILE.is_file():
            try:
                current = json.loads(BROKER_LOGINS_FILE.read_text(encoding="utf-8"))
                legacy = json.loads(legacy_logins.read_text(encoding="utf-8"))
                if not isinstance(current, dict) or not isinstance(legacy, dict):
                    raise ValueError("broker login metadata must be a JSON object")
                destination_accounts = {
                    (str(broker), str(account.get("user_id", "")).strip().upper())
                    for broker, accounts in current.items()
                    if isinstance(accounts, list)
                    for account in accounts
                    if isinstance(account, dict)
                    and str(account.get("user_id", "")).strip()
                }
                destination_redacted = all(
                    not str(account.get(field, ""))
                    for accounts in current.values()
                    if isinstance(accounts, list)
                    for account in accounts
                    if isinstance(account, dict)
                    for field in ("password", "totp_secret", "enctoken")
                )
                secrets_proven = True
                secret_count = 0
                for broker, accounts in legacy.items():
                    if not isinstance(accounts, list):
                        continue
                    for account in accounts:
                        if not isinstance(account, dict):
                            continue
                        user_id = str(account.get("user_id", "")).strip().upper()
                        for field in ("password", "totp_secret", "enctoken"):
                            secret = str(account.get(field, ""))
                            if not secret:
                                continue
                            secret_count += 1
                            if (
                                not user_id
                                or (str(broker), user_id) not in destination_accounts
                                or store._load_secret(str(broker), user_id, field) != secret
                            ):
                                secrets_proven = False
                if destination_redacted and secrets_proven and secret_count:
                    for accounts in legacy.values():
                        if not isinstance(accounts, list):
                            continue
                        for account in accounts:
                            if isinstance(account, dict):
                                for field in ("password", "totp_secret", "enctoken"):
                                    account[field] = ""
                    atomic_write_json(legacy_logins, legacy)
                    migrated.append("broker_logins.json secrets")
                elif secret_count and not (destination_redacted and secrets_proven):
                    cleanup_errors.append(
                        "keyring migration could not prove every legacy secret was "
                        "preserved in the destination account; legacy plaintext was "
                        f"left unchanged in {legacy_logins}"
                    )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                cleanup_errors.append(f"could not redact {legacy_logins}: {exc}")

    # Only APP_DIR is a proven legacy location.  The process working directory may
    # be the Desktop or another project and must never be treated as app-owned.
    legacy_token_candidates = (APP_DIR / "zerodha_token.json",)
    for legacy_token in legacy_token_candidates:
        if not legacy_token.is_file():
            continue
        try:
            payload = json.loads(legacy_token.read_text(encoding="utf-8"))
            user_id = str(payload.get("user_id", "")).strip().upper()
            token = str(payload.get("enctoken", "")).strip()
            if not user_id or not token:
                continue
            store.save_token_for_user(user_id, token)
            atomic_write_json(
                ZERODHA_SESSION_FILE,
                {"user_id": user_id, "migrated_at": get_ist_now().isoformat()},
            )
            atomic_write_json(
                legacy_token,
                {"user_id": user_id, "migrated_at": get_ist_now().isoformat()},
            )
            migrated.append(str(legacy_token))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            cleanup_errors.append(f"could not securely migrate {legacy_token}: {exc}")
    if cleanup_errors:
        raise RuntimeError("; ".join(cleanup_errors))
    return migrated


class HoldingsStore:
    """Read and write saved broker holdings details (qty, buy_price) to a JSON file."""

    def __init__(self, path: Path = HOLDINGS_DETAILS_FILE):
        self.path = Path(path)

    def load_all(self) -> dict[str, dict[str, Any]]:
        with _HOLDINGS_STORE_LOCK:
            if not self.path.exists():
                return {}
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise HoldingsStoreError(
                    f"Holdings file is unreadable and was preserved at {self.path}: {exc}"
                ) from exc
            if not isinstance(payload, dict):
                raise HoldingsStoreError(
                    f"Holdings file has an unsupported structure and was preserved at {self.path}."
                )
            result = {}
            try:
                for k, v in payload.items():
                    if not isinstance(v, dict):
                        raise ValueError(f"entry {k!r} is not an object")
                    acc = str(v.get("account", "Main")).strip().upper()
                    sym = str(v.get("symbol", str(k).split(":", 1)[-1])).strip().upper()
                    if not acc or not sym:
                        raise ValueError(f"entry {k!r} has no account or symbol")
                    numbers = {
                        field: float(v.get(field, 0))
                        for field in ("qty", "buy_price", "last_price", "close_price", "pnl")
                    }
                    if not all(math.isfinite(value) for value in numbers.values()):
                        raise ValueError(f"entry {k!r} contains a non-finite number")
                    if numbers["qty"] < 0 or any(
                        numbers[field] < 0
                        for field in ("buy_price", "last_price", "close_price")
                    ):
                        raise ValueError(f"entry {k!r} contains a negative quantity or price")
                    key = f"{acc}:{sym}" if ":" in str(k) or acc != "MAIN" else sym
                    result[key] = {"symbol": sym, "account": acc, **numbers}
            except (TypeError, ValueError) as exc:
                raise HoldingsStoreError(
                    f"Holdings file is malformed and was preserved at {self.path}: {exc}"
                ) from exc
            return result

    def get_holdings_for_account(self, account_name: str = "ALL") -> dict[str, dict[str, Any]]:
        all_h = self.load_all()
        target = str(account_name).strip().upper()
        if not target or target == "ALL":
            return all_h
        return {
            k: v for k, v in all_h.items()
            if str(v.get("account", "MAIN")).strip().upper() == target
        }

    def get_holding(self, symbol: str, account_name: str = "ALL") -> dict[str, Any] | None:
        target_holdings = self.get_holdings_for_account(account_name)
        sym_upper = str(symbol).strip().upper()
        for v in target_holdings.values():
            if str(v.get("symbol", "")).strip().upper() == sym_upper:
                return v
        return None

    def remove_account(self, account_name: str) -> int:
        """Remove locally cached holdings for one broker account."""
        target = str(account_name).strip().upper()
        if not target or target == "ALL":
            raise ValueError("A specific broker account is required.")
        with _HOLDINGS_STORE_LOCK:
            holdings = self.load_all()
            retained = {
                key: value
                for key, value in holdings.items()
                if str(value.get("account", "MAIN")).strip().upper() != target
            }
            removed_count = len(holdings) - len(retained)
            if removed_count:
                atomic_write_json(self.path, retained)
            return removed_count

    def update_holdings(self, holdings_list: list[dict[str, Any]], account_name: str | None = None) -> None:
        with _HOLDINGS_STORE_LOCK:
            self._update_holdings_locked(holdings_list, account_name)

    def _update_holdings_locked(
        self, holdings_list: list[dict[str, Any]], account_name: str | None
    ) -> None:
        all_h = self.load_all()
        
        target_account = ""
        if account_name and account_name.upper() != "ALL":
            target_account = account_name.strip().upper()
        elif holdings_list and isinstance(holdings_list[0], dict):
            target_account = str(holdings_list[0].get("account", "")).strip().upper()

        new_data = {}
        for k, v in all_h.items():
            v_acc = str(v.get("account", "MAIN")).strip().upper()
            if target_account and v_acc == target_account:
                continue
            new_data[k] = v

        for item in holdings_list:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol", "")).strip().upper()
            qty = float(item.get("qty", 0))
            item_acc = str(item.get("account", target_account or "MAIN")).strip().upper()
            if symbol and qty > 0:
                key = f"{item_acc}:{symbol}"
                new_data[key] = {
                    "symbol": symbol,
                    "qty": qty,
                    "buy_price": float(item.get("buy_price", 0)),
                    "last_price": float(item.get("last_price", 0)),
                    "close_price": float(item.get("close_price", 0)),
                    "pnl": float(item.get("pnl", 0)),
                    "account": item_acc,
                }
        atomic_write_json(self.path, new_data)
        try:
            sync_zerodha_system_watchlist()
        except Exception:
            pass


def sync_zerodha_system_watchlist() -> str:
    """Creates or updates system-managed Zerodha portfolio watchlist per registered Zerodha account."""
    holdings_dict = HoldingsStore().load_all()
    accounts = BrokerLoginStore().get_broker_accounts("Zerodha")
    w_store = WatchlistStore()
    watchlists = w_store.load()
    
    # Clean up old legacy un-keyed zerodha watchlists
    legacy_keys = [k for k in watchlists.keys() if k.startswith("💼 ZERODHA") and not re.search(r"\([A-Z0-9]+\)$", k)]
    for lk in legacy_keys:
        del watchlists[lk]
        if lk in UNIVERSES:
            del UNIVERSES[lk]

    first_wl = ""
    for acc in accounts:
        user_id = str(acc.get("user_id", "")).strip().upper()
        label = str(acc.get("label", "")).strip()
        if not user_id:
            continue

        acc_symbols = sorted([
            info.get("symbol", key.split(":", 1)[-1]) for key, info in holdings_dict.items()
            if (key.startswith(f"{user_id}:") or info.get("account") == user_id) and float(info.get("qty", 0)) > 0
        ])

        if not acc_symbols and len(accounts) == 1:
            acc_symbols = sorted([
                info.get("symbol", sym) for sym, info in holdings_dict.items()
                if float(info.get("qty", 0)) > 0
            ])

        wl_name = f"💼 ZERODHA {label} ({user_id})" if label else f"💼 ZERODHA ({user_id})"
        watchlists[wl_name] = acc_symbols
        UNIVERSES[wl_name] = acc_symbols
        if not first_wl:
            first_wl = wl_name

    w_store.save(watchlists)
    return first_wl or "💼 ZERODHA Portfolio Holdings"


def zerodha_account_id_from_watchlist(watchlist_name: str) -> str:
    """Return the account ID stored in a system Zerodha watchlist name."""
    if not is_holdings_universe(watchlist_name):
        return ""
    match = re.search(r"\(([A-Z0-9]+)\)\s*$", str(watchlist_name).strip(), re.IGNORECASE)
    return match.group(1).upper() if match else ""


def remove_saved_broker_account(broker: str, user_id: str) -> dict[str, Any]:
    """Remove a saved account plus its local holdings and generated watchlists."""
    clean_broker = str(broker).strip() or "Zerodha"
    clean_user_id = str(user_id).strip().upper()
    if not clean_user_id:
        raise ValueError("The broker account has no user ID.")

    login_store = BrokerLoginStore()
    accounts = login_store.get_broker_accounts(clean_broker)
    removed_account = next(
        (
            account
            for account in accounts
            if str(account.get("user_id", "")).strip().upper() == clean_user_id
        ),
        None,
    )
    if removed_account is None:
        raise ValueError(f"Saved {clean_broker} account '{clean_user_id}' was not found.")

    remaining_accounts = [
        account
        for account in accounts
        if str(account.get("user_id", "")).strip().upper() != clean_user_id
    ]
    # This also removes the deleted account's password, TOTP secret and token
    # from the Windows credential store.
    login_store.save_broker_accounts(clean_broker, remaining_accounts)

    holdings_removed = HoldingsStore().remove_account(clean_user_id)

    watchlist_store = WatchlistStore()
    watchlists = watchlist_store.load()
    removed_watchlists = [
        name
        for name in watchlists
        if is_holdings_universe(name)
        and zerodha_account_id_from_watchlist(name) == clean_user_id
    ]
    for name in removed_watchlists:
        watchlists.pop(name, None)
        UNIVERSES.pop(name, None)
    if removed_watchlists:
        watchlist_store.save(watchlists)

    # Do not leave the removed account selected as the legacy active session.
    if ZERODHA_SESSION_FILE.exists():
        try:
            session = json.loads(ZERODHA_SESSION_FILE.read_text(encoding="utf-8"))
            if str(session.get("user_id", "")).strip().upper() == clean_user_id:
                ZERODHA_SESSION_FILE.unlink()
        except (OSError, ValueError, json.JSONDecodeError):
            # Preserve an unreadable session file rather than deleting unknown data.
            pass

    return {
        "account": removed_account,
        "holdings_removed": holdings_removed,
        "watchlists_removed": removed_watchlists,
    }


class NSESymbolsDownloadWorker(QThread):
    """Background worker to download and cache the NSE equity symbols list."""

    succeeded = Signal(list)
    failed = Signal(str)

    def __init__(self, cache_path: Path = NSE_SYMBOLS_CACHE_FILE):
        super().__init__()
        self.cache_path = Path(cache_path)

    def run(self) -> None:
        try:
            url = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
            response = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 ChartInkScanner/1.0"},
                timeout=(5, 20),
            )
            response.raise_for_status()

            table = pd.read_csv(StringIO(response.text))
            symbol_col = next(
                (c for c in table.columns if str(c).strip().lower() == "symbol"),
                None,
            )
            name_col = next(
                (
                    c
                    for c in table.columns
                    if str(c).strip().lower()
                    in {"name of company", "company name"}
                ),
                None,
            )

            if symbol_col is None:
                raise ValueError("NSE CSV lacks a Symbol column")

            symbols_list = []
            for _, row in table.iterrows():
                symbol = str(row[symbol_col]).strip().upper()
                name = str(row[name_col]).strip() if name_col is not None else ""
                if symbol and symbol != "NAN" and symbol != "SYMBOL":
                    symbol = symbol.replace(".NS", "")
                    symbols_list.append({"symbol": symbol, "name": name})

            seen = set()
            deduped = []
            for item in symbols_list:
                if item["symbol"] not in seen:
                    seen.add(item["symbol"])
                    deduped.append(item)

            deduped.sort(key=lambda x: x["symbol"])

            payload = {
                "downloaded_at": get_ist_now().isoformat(timespec="seconds"),
                "symbols": deduped,
            }
            atomic_write_json(self.cache_path, payload)
            self.succeeded.emit(deduped)
        except Exception as exc:
            self.failed.emit(str(exc))


class ZerodhaHoldingsWorker(QThread):
    """Background worker to download the holdings list from Zerodha Kite."""

    succeeded = Signal(list)
    failed = Signal(str)

    def __init__(self, enctoken: str, account_name: str = "Main"):
        super().__init__()
        self.enctoken = str(enctoken).strip()
        self.account_name = str(account_name).strip() or "Main"

    def run(self) -> None:
        try:
            url = "https://kite.zerodha.com/oms/portfolio/holdings"
            headers = {
                "Authorization": f"enctoken {self.enctoken}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            }

            real_user_id = ZerodhaOrderExecutor(self.enctoken).fetch_profile_user_id()
            if real_user_id:
                if self.account_name and self.account_name.upper() not in ["MAIN", "ALL"]:
                    target_upper = self.account_name.upper()
                    if real_user_id != target_upper:
                        raise ValueError(f"Active session token belongs to account '{real_user_id}', but selected target is '{self.account_name}'. Please click '🔑 Login Zerodha' to log into '{self.account_name}'.")
                self.account_name = real_user_id
                try:
                    atomic_write_json(ZERODHA_SESSION_FILE, {
                        "user_id": real_user_id,
                        "downloaded_at": get_ist_now().isoformat(),
                    })
                    BrokerLoginStore().save_token_for_user(real_user_id, self.enctoken)
                except Exception:
                    pass

            response = requests.get(url, headers=headers, timeout=(5, 20))
            if response.status_code == 403:
                raise ValueError("Kite session expired or invalid enctoken.")
            response.raise_for_status()

            payload = response.json()
            if not isinstance(payload, dict) or payload.get("status") != "success":
                error_msg = payload.get("message", "Invalid holdings response.")
                raise ValueError(error_msg)

            data = payload.get("data")
            if not isinstance(data, list):
                raise ValueError("Holdings list is missing from response data.")

            symbols = []
            detailed_holdings = []
            for item in data:
                if not isinstance(item, dict):
                    raise ValueError("Zerodha holdings response contained a non-object holding.")
                symbol = str(item.get("tradingsymbol", "")).strip().upper()
                if not symbol:
                    raise ValueError("Zerodha holdings response contained a holding without a symbol.")
                try:
                    demat_qty = float(item.get("quantity", 0) or 0)
                    t1_qty = float(item.get("t1_quantity", 0) or 0)
                    qty = demat_qty + t1_qty
                    buy_price = float(item.get("average_price", 0) or 0)
                    last_price = float(item.get("last_price", 0) or 0)
                    close_price = float(item.get("close_price", 0) or 0)
                    pnl = float(item.get("pnl", 0) or 0)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"Zerodha returned invalid numeric holdings data for {symbol}.") from exc
                if not all(
                    math.isfinite(value)
                    for value in (demat_qty, t1_qty, buy_price, last_price, close_price, pnl)
                ) or qty < 0 or any(
                    value < 0 for value in (buy_price, last_price, close_price)
                ):
                    raise ValueError(f"Zerodha returned unsafe holdings data for {symbol}.")
                symbols.append(symbol)
                detailed_holdings.append({
                    "symbol": symbol,
                    "qty": qty,
                    "buy_price": buy_price,
                    "last_price": last_price,
                    "close_price": close_price,
                    "pnl": pnl,
                    "account": self.account_name,
                })

            # A corrupt local holdings file must fail the refresh instead of being
            # silently replaced with a partial/empty account snapshot.
            HoldingsStore().update_holdings(detailed_holdings, self.account_name)

            self.succeeded.emit(symbols)
        except Exception as exc:
            self.failed.emit(str(exc))


class ZerodhaMarginsFetcher(QThread):
    """Background worker to fetch account funds/margins from Zerodha Kite via OMS API."""

    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(self, enctoken: str, parent: QObject | None = None):
        super().__init__(parent)
        self.enctoken = str(enctoken).strip()

    def run(self) -> None:
        try:
            url = "https://kite.zerodha.com/oms/user/margins"
            headers = {
                "Authorization": f"enctoken {self.enctoken}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            }
            response = requests.get(url, headers=headers, timeout=(5, 15))
            if response.status_code == 403:
                raise ValueError("Zerodha session expired or invalid enctoken.")
            response.raise_for_status()

            payload = response.json()
            if not isinstance(payload, dict) or payload.get("status") != "success":
                error_msg = payload.get("message", "Invalid margins response.")
                raise ValueError(error_msg)

            data = payload.get("data", {})
            equity_data = data.get("equity", {})
            available_data = equity_data.get("available", {})

            cash = float(available_data.get("cash", 0.0) or equity_data.get("net", 0.0) or 0.0)
            net = float(equity_data.get("net", cash) or cash)

            self.succeeded.emit({
                "available_cash": cash,
                "net": net,
            })
        except Exception as exc:
            self.failed.emit(str(exc))


class ZerodhaOrderExecutor:
    """Helper class to place buy/sell orders on Zerodha Kite via OMS API."""

    def __init__(self, enctoken: str):
        self.enctoken = str(enctoken).strip()

    def place_order(
        self,
        symbol: str,
        quantity: int,
        transaction_type: str = "BUY",
        order_type: str = "MARKET",
        product: str = "CNC",
        price: float = 0.0,
        exchange: str = "NSE",
    ) -> dict[str, Any]:
        url = "https://kite.zerodha.com/oms/orders/regular"
        headers = {
            "Authorization": f"enctoken {self.enctoken}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        payload = {
            "tradingsymbol": str(symbol).strip().upper(),
            "exchange": exchange,
            "transaction_type": str(transaction_type).upper(),
            "order_type": str(order_type).upper(),
            "quantity": int(quantity),
            "product": str(product).upper(),
            "validity": "DAY",
        }
        if str(order_type).upper() == "LIMIT" and price > 0:
            payload["price"] = str(round(price, 2))

        try:
            response = requests.post(url, headers=headers, data=payload, timeout=(5, 15))
        except requests.RequestException as exc:
            raise OrderSubmissionUncertainError(
                "The order request lost contact with Zerodha. Its submission status is "
                "unknown; do not retry until you verify the Zerodha order book."
            ) from exc
        if response.status_code == 403:
            raise ValueError("Zerodha session expired or invalid enctoken.")
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            if response.status_code >= 500:
                raise OrderSubmissionUncertainError(
                    "Zerodha returned a server error after receiving the order request. "
                    "Its submission status is unknown; do not retry until you verify "
                    "the Zerodha order book."
                ) from exc
            raise
        try:
            res_json = response.json()
        except (ValueError, requests.RequestException) as exc:
            raise OrderSubmissionUncertainError(
                "Zerodha returned an unreadable response after the order request. Its "
                "submission status is unknown; do not retry until you verify the order book."
            ) from exc
        if not isinstance(res_json, dict):
            raise OrderSubmissionUncertainError(
                "Zerodha returned an invalid response after the order request. Its "
                "submission status is unknown; do not retry until you verify the order book."
            )
        response_status = res_json.get("status")
        if response_status == "error":
            raise ValueError(str(res_json.get("message", "Order placement failed.")))
        if response_status != "success":
            raise OrderSubmissionUncertainError(
                "Zerodha returned an unrecognized response after the order request. Its "
                "submission status is unknown; do not retry until you verify the order book."
            )
        data = res_json.get("data")
        if not isinstance(data, dict) or not str(data.get("order_id", "")).strip():
            raise OrderSubmissionUncertainError(
                "Zerodha reported success but returned no order ID. The submission status "
                "is unknown; do not retry until you verify the Zerodha order book."
            )
        return data

    def fetch_orders(self) -> list[dict[str, Any]]:
        url = "https://kite.zerodha.com/oms/orders"
        headers = {
            "Authorization": f"enctoken {self.enctoken}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        response = requests.get(url, headers=headers, timeout=(5, 15))
        if response.status_code == 403:
            raise ValueError("Zerodha session expired or invalid enctoken.")
        response.raise_for_status()
        res_json = response.json()
        if not isinstance(res_json, dict) or res_json.get("status") != "success":
            message = res_json.get("message", "Invalid Zerodha orders response.") if isinstance(res_json, dict) else "Invalid Zerodha orders response."
            raise ValueError(str(message))
        data = res_json.get("data")
        if not isinstance(data, list):
            raise ValueError("Zerodha orders response did not contain an order list.")
        for item in data:
            if not isinstance(item, dict):
                raise ValueError("Zerodha orders response contained a malformed order.")
            symbol = item.get("tradingsymbol")
            transaction_type = item.get("transaction_type")
            status = item.get("status")
            if (
                not isinstance(symbol, str)
                or not symbol.strip()
                or not isinstance(transaction_type, str)
                or transaction_type.strip().upper() not in {"BUY", "SELL"}
                or not isinstance(status, str)
                or not status.strip()
            ):
                raise ValueError("Zerodha orders response contained a malformed order.")
        return data

    def fetch_holdings(self) -> list[dict[str, Any]]:
        response = requests.get(
            "https://kite.zerodha.com/oms/portfolio/holdings",
            headers={
                "Authorization": f"enctoken {self.enctoken}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            },
            timeout=(5, 15),
        )
        if response.status_code == 403:
            raise ValueError("Zerodha session expired or invalid enctoken.")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("status") != "success":
            message = payload.get("message", "Invalid Zerodha holdings response.") if isinstance(payload, dict) else "Invalid Zerodha holdings response."
            raise ValueError(str(message))
        data = payload.get("data")
        if not isinstance(data, list):
            raise ValueError("Zerodha holdings response did not contain a holdings list.")
        for item in data:
            symbol = item.get("tradingsymbol") if isinstance(item, dict) else None
            if not isinstance(symbol, str) or not symbol.strip() or "quantity" not in item:
                raise ValueError("Zerodha holdings response contained a malformed holding.")
            try:
                if isinstance(item.get("quantity"), bool) or isinstance(
                    item.get("t1_quantity", 0), bool
                ):
                    raise TypeError("boolean quantity")
                quantities = (
                    float(item.get("quantity", 0) or 0),
                    float(item.get("t1_quantity", 0) or 0),
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("Zerodha holdings response contained an invalid quantity.") from exc
            if not all(math.isfinite(value) and value >= 0 for value in quantities):
                raise ValueError("Zerodha holdings response contained an invalid quantity.")
        return data

    def fetch_available_cash(self) -> float:
        response = requests.get(
            "https://kite.zerodha.com/oms/user/margins",
            headers={
                "Authorization": f"enctoken {self.enctoken}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            },
            timeout=(5, 15),
        )
        if response.status_code == 403:
            raise ValueError("Zerodha session expired or invalid enctoken.")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("status") != "success":
            message = payload.get("message", "Invalid Zerodha margins response.") if isinstance(payload, dict) else "Invalid Zerodha margins response."
            raise ValueError(str(message))
        try:
            equity = payload["data"]["equity"]
            available = equity.get("available", {})
            cash = float(available.get("cash", equity.get("net")))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Zerodha margins response did not contain available equity cash.") from exc
        if not math.isfinite(cash) or cash < 0:
            raise ValueError("Zerodha returned an invalid available cash amount.")
        return cash

    def fetch_profile_user_id(self) -> str:
        response = requests.get(
            "https://kite.zerodha.com/oms/user/profile",
            headers={
                "Authorization": f"enctoken {self.enctoken}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            },
            timeout=(5, 10),
        )
        if response.status_code == 403:
            raise ValueError("Zerodha session expired or invalid enctoken.")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("status") != "success":
            message = payload.get("message", "Invalid Zerodha profile response.") if isinstance(payload, dict) else "Invalid Zerodha profile response."
            raise ValueError(str(message))
        user_id = str(payload.get("data", {}).get("user_id", "")).strip().upper()
        if not user_id:
            raise ValueError("Could not verify the Zerodha account for this order.")
        return user_id


def load_today_execution_log() -> dict[str, Any]:
    today_str = get_ist_now().date().isoformat()
    if not EXECUTED_ORDERS_FILE.exists():
        return {"date": today_str, "accounts": {}}
    try:
        payload = json.loads(EXECUTED_ORDERS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateStoreError(
            f"Order log is unreadable and was preserved at {EXECUTED_ORDERS_FILE}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise StateStoreError("Order log has an unsupported structure and was preserved.")
    if payload.get("date") != today_str:
        return {"date": today_str, "accounts": {}}
    accounts = payload.get("accounts", {})
    if not isinstance(accounts, dict):
        raise StateStoreError("Order log account data is malformed and was preserved.")
    for account, entries in accounts.items():
        if not isinstance(entries, dict):
            raise StateStoreError(
                f"Order log data for account {account!r} is malformed and was preserved."
            )
        for field in ("executed_buys", "executed_sells"):
            entries.setdefault(field, [])
            if not isinstance(entries[field], list):
                raise StateStoreError(
                    f"Order log data for account {account!r} is malformed and was preserved."
                )
    for field in ("executed_buys", "executed_sells"):
        if field in payload and not isinstance(payload[field], list):
            raise StateStoreError(f"Legacy order log field {field!r} is malformed and was preserved.")
    return payload


def check_can_buy_stock(symbol: str, enctoken: str = "", target_account: str = "ALL") -> tuple[bool, str]:
    """Strictly checks if a stock can be bought by checking target account portfolio holdings, today's order log (buys & sells), and Zerodha OMS orders."""
    symbol = str(symbol).strip().upper()
    if not enctoken:
        return False, "A verified Zerodha session is required to reconcile live orders."

    # 1. Check existing Demat / Zerodha Holdings for target_account
    holdings = HoldingsStore().get_holdings_for_account(target_account)
    for k, h_info in holdings.items():
        h_sym = str(h_info.get("symbol", "")).strip().upper()
        h_qty = float(h_info.get("qty", 0))
        if h_sym == symbol and h_qty > 0:
            acc_label = str(h_info.get("account", target_account))
            return False, f"Stock {symbol} is already in {acc_label}'s portfolio holdings (Qty: {int(h_qty)})."

    # 2. Check today's automated trade execution log (Buys & Sells)
    today_str = get_ist_now().date().isoformat()
    try:
        log_data = load_today_execution_log()
        account_key = str(target_account).strip().upper() or "ALL"
        account_log = log_data.get("accounts", {}).get(account_key, {})
        legacy_buys = log_data.get("executed_buys", [])
        legacy_sells = log_data.get("executed_sells", [])
        if symbol in account_log.get("executed_buys", []) or symbol in legacy_buys:
            return False, f"Stock {symbol} was already bought today ({today_str})."
        if symbol in account_log.get("executed_sells", []) or symbol in legacy_sells:
            return False, f"Stock {symbol} was SOLD earlier today ({today_str}). Same-Day Churn Guard prevents re-buying sold stocks on the same day."
    except Exception as exc:
        return False, f"Could not verify existing Zerodha orders: {exc}"

    # 3. Check live open/pending or completed orders on Zerodha OMS today
    if enctoken:
        try:
            executor = ZerodhaOrderExecutor(enctoken)
            orders = executor.fetch_orders()
            for ord_item in orders:
                if str(ord_item.get("tradingsymbol", "")).upper() == symbol:
                    status = str(ord_item.get("status", "")).upper()
                    tx_type = str(ord_item.get("transaction_type", "")).upper()
                    if tx_type == "BUY" and status not in {"CANCELLED", "REJECTED"}:
                        return False, f"BUY order for {symbol} already exists on Zerodha today (Status: {status})."
                    if tx_type == "SELL" and status not in {"CANCELLED", "REJECTED"}:
                        return False, f"SELL order for {symbol} was executed on Zerodha today (Status: {status}). Same-Day Churn Guard prevents re-buying sold stocks on the same day."
        except Exception as exc:
            return False, f"Could not reconcile live Zerodha orders; BUY blocked: {exc}"

    return True, "OK"


def validate_live_buy_funds(
    executor: ZerodhaOrderExecutor,
    quantity: int,
    reference_price: float,
    market_order: bool,
) -> None:
    if quantity <= 0 or not math.isfinite(reference_price) or reference_price <= 0:
        raise ValueError("A positive quantity and current reference price are required.")
    available_cash = executor.fetch_available_cash()
    # A market order has no guaranteed execution price; reserve a modest buffer
    # while leaving the broker's own RMS as the final authority.
    estimated_value = quantity * reference_price * (1.05 if market_order else 1.0)
    if estimated_value > available_cash:
        raise ValueError(
            f"Estimated order requirement ₹{estimated_value:,.2f} exceeds live "
            f"available equity cash ₹{available_cash:,.2f}."
        )


def validate_live_buy_holding_absent(
    executor: ZerodhaOrderExecutor, symbol: str
) -> None:
    clean_symbol = str(symbol).strip().upper()
    for item in executor.fetch_holdings():
        if str(item.get("tradingsymbol", "")).strip().upper() != clean_symbol:
            continue
        try:
            quantity = float(item.get("quantity", 0) or 0)
            t1_quantity = float(item.get("t1_quantity", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Zerodha returned an invalid holding quantity for {clean_symbol}.") from exc
        if not math.isfinite(quantity) or not math.isfinite(t1_quantity):
            raise ValueError(f"Zerodha returned an invalid holding quantity for {clean_symbol}.")
        if quantity + t1_quantity > 0:
            raise ValueError(
                f"Stock {clean_symbol} is already present in the live Zerodha holdings."
            )


def validate_live_sell_quantity(
    executor: ZerodhaOrderExecutor,
    symbol: str,
    requested_quantity: int,
) -> None:
    clean_symbol = str(symbol).strip().upper()
    if requested_quantity <= 0:
        raise ValueError("Sell quantity must be positive.")
    orders = executor.fetch_orders()  # Mandatory reconciliation; errors fail closed.
    for item in orders:
        if (
            str(item.get("tradingsymbol", "")).strip().upper() == clean_symbol
            and str(item.get("transaction_type", "")).strip().upper() == "SELL"
            and str(item.get("status", "")).strip().upper() not in {"CANCELLED", "REJECTED"}
        ):
            status = str(item.get("status", "UNKNOWN")).strip().upper()
            raise ValueError(
                f"A SELL order for {clean_symbol} already exists today (Status: {status})."
            )
    available_quantity = 0
    for item in executor.fetch_holdings():
        if str(item.get("tradingsymbol", "")).strip().upper() != clean_symbol:
            continue
        try:
            quantity = float(item.get("quantity", 0) or 0)
            t1_quantity = float(item.get("t1_quantity", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Zerodha returned an invalid holding quantity for {clean_symbol}.") from exc
        if not math.isfinite(quantity) or not math.isfinite(t1_quantity):
            raise ValueError(f"Zerodha returned an invalid holding quantity for {clean_symbol}.")
        available_quantity += max(0, int(quantity + t1_quantity))
    if requested_quantity > available_quantity:
        raise ValueError(
            f"Sell quantity {requested_quantity} exceeds live delivery holdings "
            f"of {available_quantity} for {clean_symbol}."
        )


def trigger_silent_holdings_and_funds_refresh(account_name: str) -> None:
    """Triggers background silent holdings fetch and cash funds refresh after trade executions."""
    try:
        user_id = str(account_name).strip().upper()
        enctoken = BrokerLoginStore().get_token_for_user(user_id)
        if enctoken and user_id:
            worker = ZerodhaHoldingsWorker(enctoken, account_name=user_id)
            _BACKGROUND_WORKERS.add(worker)
            worker.finished.connect(
                lambda w=worker: _BACKGROUND_WORKERS.discard(w)
            )
            worker.start()
    except Exception:
        pass


def record_stock_bought_today(symbol: str, account_name: str) -> None:
    """Record a submitted BUY order in today's persistent execution log."""
    symbol = str(symbol).strip().upper()
    today_str = get_ist_now().date().isoformat()
    log_file = EXECUTED_ORDERS_FILE
    log_data = load_today_execution_log()
    account_key = str(account_name).strip().upper()
    account_log = log_data.setdefault("accounts", {}).setdefault(
        account_key, {"executed_buys": [], "executed_sells": []}
    )
    if symbol not in account_log["executed_buys"]:
        account_log["executed_buys"].append(symbol)
    atomic_write_json(log_file, log_data)
    trigger_silent_holdings_and_funds_refresh(account_name)


def record_stock_sold_today(symbol: str, account_name: str) -> None:
    """Record a submitted SELL order in today's persistent execution log."""
    symbol = str(symbol).strip().upper()
    today_str = get_ist_now().date().isoformat()
    log_file = EXECUTED_ORDERS_FILE
    log_data = load_today_execution_log()
    account_key = str(account_name).strip().upper()
    account_log = log_data.setdefault("accounts", {}).setdefault(
        account_key, {"executed_buys": [], "executed_sells": []}
    )
    if symbol not in account_log["executed_sells"]:
        account_log["executed_sells"].append(symbol)
    atomic_write_json(log_file, log_data)
    trigger_silent_holdings_and_funds_refresh(account_name)


class ZerodhaAutomatedLoginWorker(QThread):
    """Background worker to orchestrate browser login and capture Zerodha enctoken."""

    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        user_id: str = "",
        password: str = "",
        totp_secret: str = "",
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.user_id = str(user_id or "").strip()
        self.password = str(password or "").strip()
        self.totp_secret = str(totp_secret or "").strip()

    def run(self) -> None:
        import time
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.failed.emit(
                "Playwright library is not installed.\n\n"
                "To use automated login, please run the following in your terminal:\n"
                "  pip install playwright\n"
                "  playwright install"
            )
            return

        try:
            with sync_playwright() as p:
                browser = None
                # Bulletproof Browser Launcher with 4-level fallback
                try:
                    browser = p.chromium.launch(headless=False)
                except Exception:
                    try:
                        browser = p.chromium.launch(channel="chrome", headless=False)
                    except Exception:
                        try:
                            browser = p.chromium.launch(channel="msedge", headless=False)
                        except Exception:
                            possible_paths = [
                                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
                                os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
                            ]
                            for exe_path in possible_paths:
                                if os.path.exists(exe_path):
                                    try:
                                        browser = p.chromium.launch(executable_path=exe_path, headless=False)
                                        break
                                    except Exception:
                                        pass
                if browser is None:
                    raise RuntimeError("Could not launch any browser (Bundled Chromium, System Chrome, or Microsoft Edge).")

                context = browser.new_context()
                page = context.new_page()
                page.goto("https://kite.zerodha.com/")

                # Auto-fill User ID & Password if credentials were provided
                if self.user_id and self.password:
                    try:
                        page.wait_for_selector("#userid", timeout=5000)
                        page.fill("#userid", self.user_id)
                        page.fill("#password", self.password)
                        page.click("button[type=submit]")
                        time.sleep(1.5)
                    except Exception:
                        pass

                # Auto-fill TOTP 2FA code if TOTP secret key was provided
                if self.totp_secret:
                    try:
                        totp_code = None
                        try:
                            import pyotp  # type: ignore
                            totp_code = pyotp.TOTP(self.totp_secret).now()
                        except Exception:
                            pass
                        if totp_code:
                            totp_selector = "input[type=number], #totp, input[placeholder='6-digit TOTP']"
                            page.wait_for_selector(totp_selector, timeout=4000)
                            page.fill(totp_selector, totp_code)
                            time.sleep(1.0)
                    except Exception:
                        pass

                enctoken = None
                start_time = time.time()
                timeout = 300  # 5 minutes

                while time.time() - start_time < timeout:
                    if page.is_closed():
                        raise ValueError("Browser window was closed by the user.")

                    cookies = context.cookies()
                    for cookie in cookies:
                        if cookie.get("name") == "enctoken":
                            enctoken = cookie.get("value")
                            break

                    if enctoken:
                        break

                    time.sleep(1)

                browser.close()

                if enctoken:
                    self.succeeded.emit(enctoken)
                else:
                    self.failed.emit("Timeout waiting for enctoken.")
        except Exception as exc:
            self.failed.emit(str(exc))


class TelegramAlertStateStore:
    """Non-secret per-template baseline used to suppress duplicate alerts."""

    def __init__(self, path: Path = TELEGRAM_ALERT_STATE_FILE):
        self.path = Path(path)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": TELEGRAM_ALERT_STATE_VERSION, "templates": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TelegramSettingsError(
                f"Telegram alert history is malformed and was preserved: {exc}"
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("version") != TELEGRAM_ALERT_STATE_VERSION
            or not isinstance(payload.get("templates"), dict)
        ):
            raise TelegramSettingsError("Unsupported Telegram alert-history format.")
        return payload

    def symbols(
        self,
        template_name: str,
        destination_key: str | None = None,
        allow_legacy: bool = False,
        auto_reset_daily: bool = False,
        now: datetime | None = None,
    ) -> set[str] | None:
        entry = self._load()["templates"].get(template_name)
        if not isinstance(entry, dict):
            return None

        if auto_reset_daily:
            current = now or get_ist_now()
            updated_at_str = None
            if destination_key:
                destinations = entry.get("destinations", {})
                if isinstance(destinations, dict):
                    dest = destinations.get(destination_key)
                    if isinstance(dest, dict):
                        updated_at_str = dest.get("updated_at")
            if not updated_at_str:
                updated_at_str = entry.get("updated_at")
            if updated_at_str:
                try:
                    updated_date = datetime.fromisoformat(updated_at_str).date()
                    if updated_date < current.date():
                        # Saved baseline is from a previous calendar day (before 12:00 midnight IST). Reset!
                        return None
                except (ValueError, TypeError):
                    pass

        if destination_key:
            destinations = entry.get("destinations", {})
            destination = (
                destinations.get(destination_key)
                if isinstance(destinations, dict)
                else None
            )
            if isinstance(destination, dict) and isinstance(
                destination.get("symbols"), list
            ):
                return {
                    str(symbol)
                    for symbol in destination["symbols"]
                    if str(symbol)
                }
            if allow_legacy and isinstance(entry.get("symbols"), list):
                return {str(symbol) for symbol in entry["symbols"] if str(symbol)}
            if isinstance(destinations, dict) and destinations:
                return None
            if not allow_legacy:
                return None
        if not isinstance(entry.get("symbols"), list):
            return None
        return {str(symbol) for symbol in entry["symbols"] if str(symbol)}

    def save_symbols(
        self,
        template_name: str,
        symbols: Iterable[str],
        destination_key: str | None = None,
    ) -> None:
        try:
            payload = self._load()
        except TelegramSettingsError:
            payload = {"version": TELEGRAM_ALERT_STATE_VERSION, "templates": {}}
        saved = {
            "symbols": sorted(set(str(symbol) for symbol in symbols if str(symbol))),
            "updated_at": get_ist_now().isoformat(timespec="seconds"),
        }
        if destination_key:
            entry = payload["templates"].setdefault(template_name, {})
            destinations = entry.setdefault("destinations", {})
            destinations[destination_key] = saved
        else:
            payload["templates"][template_name] = saved
        atomic_write_json(self.path, payload)

    def clear(self, template_name: str) -> None:
        try:
            payload = self._load()
        except TelegramSettingsError:
            payload = {"version": TELEGRAM_ALERT_STATE_VERSION, "templates": {}}
        payload["templates"].pop(template_name, None)
        atomic_write_json(self.path, payload)

    def rename_template(self, old_name: str, new_name: str) -> None:
        try:
            payload = self._load()
        except TelegramSettingsError:
            payload = {"version": TELEGRAM_ALERT_STATE_VERSION, "templates": {}}
        if old_name in payload["templates"]:
            payload["templates"][new_name] = payload["templates"].pop(old_name)
            atomic_write_json(self.path, payload)


def validate_telegram_token(token: str) -> str:
    clean = str(token).strip()
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", clean):
        raise TelegramSettingsError(
            "Enter the complete bot token supplied by BotFather."
        )
    return clean


def validate_telegram_chat_id(chat_id: str) -> str:
    clean = str(chat_id).strip()
    if not (re.fullmatch(r"-?\d+", clean) or re.fullmatch(r"@[A-Za-z0-9_]{5,}", clean)):
        raise TelegramSettingsError(
            "Chat ID must be a numeric ID or a channel username such as @mychannel."
        )
    return clean


def telegram_safe_error(exc: Exception, token: str) -> str:
    return str(exc).replace(token, "<redacted-token>")


def send_telegram_message(
    token: str,
    chat_id: str,
    text: str,
    post: Callable[..., Any] = requests.post,
) -> None:
    clean_token = validate_telegram_token(token)
    clean_chat_id = validate_telegram_chat_id(chat_id)
    try:
        response = post(
            f"https://api.telegram.org/bot{clean_token}/sendMessage",
            json={"chat_id": clean_chat_id, "text": str(text)},
            timeout=(5, 20),
        )
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if not getattr(response, "ok", False) or not payload.get("ok", False):
            description = payload.get("description") or (
                f"Telegram HTTP error {getattr(response, 'status_code', 'unknown')}"
            )
            raise TelegramSendError(str(description))
    except TelegramSendError:
        raise
    except Exception as exc:
        raise TelegramSendError(telegram_safe_error(exc, clean_token)) from exc


def chunk_telegram_lines(
    lines: Iterable[str],
    continuation_header: str,
    limit: int = TELEGRAM_MESSAGE_LIMIT,
) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    piece_limit = max(1, limit - len(continuation_header) - 1)
    for raw_line in lines:
        line = str(raw_line)
        pieces = [
            line[index : index + piece_limit]
            for index in range(0, len(line), piece_limit)
        ] or [""]
        for piece in pieces:
            extra = len(piece) + (1 if current else 0)
            if current and current_length + extra > limit:
                chunks.append("\n".join(current))
                current = [continuation_header, piece]
                current_length = len(continuation_header) + 1 + len(piece)
            else:
                current.append(piece)
                current_length += extra
    if current:
        chunks.append("\n".join(current))
    return chunks


@dataclass
class TelegramAlertPlan:
    messages: list[str]
    current_symbols: set[str]
    status: str


@dataclass
class TelegramDelivery:
    key: str
    label: str
    chat_id: str
    messages: list[str]


def build_telegram_alert_plan(
    template_name: str,
    rows: list[dict[str, Any]],
    previous_symbols: set[str] | None,
    mode: str,
    generated_at: datetime | None = None,
    universe_names: Iterable[str] | None = None,
    custom_symbols_included: bool = False,
    expression_tree: dict[str, Any] | None = None,
) -> TelegramAlertPlan:
    valid_modes = {value for value, _label in TELEGRAM_ALERT_MODES}
    if mode not in valid_modes:
        mode = "new"
    row_by_symbol: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("Symbol", "")).strip()
        if symbol and symbol not in row_by_symbol:
            row_by_symbol[symbol] = row
    current_symbols = set(row_by_symbol)
    baseline_missing = previous_symbols is None
    previous = previous_symbols or set()
    entered = current_symbols - previous
    removed = previous - current_symbols

    if mode == "full":
        included = current_symbols
        heading = "Complete matching list"
    elif baseline_missing:
        included = current_symbols
        heading = "Initial matching list"
    else:
        included = entered
        heading = "New matching stocks"

    if mode == "new" and not baseline_missing and not entered:
        return TelegramAlertPlan([], current_symbols, "No new matching stocks.")
    if mode == "new_removed" and not baseline_missing and not entered and not removed:
        return TelegramAlertPlan([], current_symbols, "No Telegram list changes.")

    selected_universes = [
        str(name).strip() for name in (universe_names or []) if str(name).strip()
    ]
    if custom_symbols_included:
        selected_universes.append("Custom symbols")
    if not selected_universes:
        selected_universes = sorted(
            {
                str(row.get("Universe", "")).strip()
                for row in rows
                if str(row.get("Universe", "")).strip()
            }
        )
    universe_text = ", ".join(dict.fromkeys(selected_universes)) or "Not specified"
    created = generated_at or get_ist_now()
    lines = [
        "ChartSage Scanner Alert",
        f"Template: {template_name}",
        f"Universe: {universe_text}",
        f"Generated: {created:%Y-%m-%d %H:%M:%S} IST",
        f"Total matches: {len(current_symbols)}",
        "",
        f"{heading}: {len(included)}",
    ]
    ordered_rows = [
        row_by_symbol[symbol]
        for symbol in row_by_symbol
        if symbol in included
    ]
    if ordered_rows:
        for index, row in enumerate(ordered_rows, 1):
            symbol = str(row.get("Symbol", ""))
            change = finite_or_none(row.get(PERCENT_CHANGE_COLUMN))
            close_value = finite_or_none(row.get("Close"))
            qty = finite_or_none(row.get(HOLDING_QTY_COLUMN))
            buy_price = finite_or_none(row.get(BUY_PRICE_COLUMN))
            pnl_pct = finite_or_none(row.get(PNL_PCT_COLUMN))
            details = [f"{index}. {symbol}"]
            if change is not None:
                details.append(f"{change:+.2f}%")
            if close_value is not None:
                details.append(f"Close {format_number(close_value)}")
            if qty is not None and buy_price is not None:
                pnl_str = f" ({pnl_pct:+.2f}%)" if pnl_pct is not None else ""
                details.append(f"Holding {format_number(qty)} @ {format_number(buy_price)}{pnl_str}")
            lines.append(" | ".join(details))
            evidence_str = str(row.get("Evidence", "")).strip()
            if evidence_str:
                lines.append(f"   └ Evidence: {evidence_str}")
    else:
        lines.append("No matching stocks.")

    if mode == "new_removed" and removed:
        lines.extend(["", f"Removed from list: {len(removed)}"])
        lines.extend(f"- {symbol}" for symbol in sorted(removed))

    data_times = sorted(
        {
            str(row.get(DATA_TIME_COLUMN))
            for row in rows
            if row.get(DATA_TIME_COLUMN) not in (None, "", "Unavailable")
        }
    )
    if data_times:
        lines.extend(["", f"Latest Yahoo data time: {data_times[-1]} IST"])

    if expression_tree:
        rules_text = format_active_rules(expression_tree)
        if rules_text:
            lines.extend(["", "Rules:", rules_text])

    messages = chunk_telegram_lines(
        lines,
        continuation_header=f"ChartSage Scanner Alert — {template_name} (continued)",
    )
    return TelegramAlertPlan(
        messages,
        current_symbols,
        f"Prepared {len(messages)} Telegram message(s).",
    )


class TelegramSendWorker(QThread):
    succeeded = Signal(int)
    failed = Signal(str)
    deliveriesCompleted = Signal(object, int)

    def __init__(
        self,
        token: str,
        deliveries: Iterable[TelegramDelivery],
        sender: Callable[[str, str, str], None] = send_telegram_message,
    ):
        super().__init__()
        self.token = token
        self.deliveries = list(deliveries)
        self.sender = sender

    def run(self) -> None:
        completed_keys: list[str] = []
        message_count = 0
        errors: list[str] = []
        for delivery in self.deliveries:
            try:
                for message in delivery.messages:
                    self.sender(self.token, delivery.chat_id, message)
                    message_count += 1
                completed_keys.append(delivery.key)
            except Exception as exc:
                errors.append(
                    f"{delivery.label}: {telegram_safe_error(exc, self.token)}"
                )
        self.deliveriesCompleted.emit(completed_keys, message_count)
        if errors:
            self.failed.emit("; ".join(errors))
        else:
            self.succeeded.emit(message_count)


class NSECalendarRefreshWorker(QThread):
    """Refresh the official NSE calendar without blocking the Qt event loop."""

    succeeded = Signal(object)
    failed = Signal(str)

    def run(self) -> None:
        try:
            holiday_checked = NSE_CALENDAR.snapshot().get("holiday_fetched_at")
            include_holidays = True
            if holiday_checked:
                try:
                    include_holidays = (
                        datetime.fromisoformat(str(holiday_checked)).date()
                        != get_ist_now().date()
                    )
                except ValueError:
                    pass
            holiday_payload, status_payload = fetch_official_nse_payloads(
                include_holidays=include_holidays
            )
            result = NSE_CALENDAR.update_from_official_payloads(
                holiday_payload, status_payload, get_ist_now()
            )
            self.succeeded.emit(result)
        except Exception as exc:
            # The validated disk cache/bootstrap remains active when NSE is offline.
            self.failed.emit(str(exc))


class PriceCache:
    COLUMNS = ["Open", "High", "Low", "Close", "Volume"]

    def __init__(self, path: Path = PRICE_CACHE_DB):
        self.path = str(path)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _migrate(self) -> None:
        # sqlite3.Connection.__exit__ commits or rolls back but does not close the
        # handle. Explicitly close it so repeated scans cannot leak/lock databases.
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS daily_price_cache (
                        symbol TEXT NOT NULL, date TEXT NOT NULL, open REAL,
                        high REAL, low REAL, close REAL, volume REAL,
                        source TEXT,
                        adjusted INTEGER NOT NULL DEFAULT 1,
                        PRIMARY KEY (symbol, date, adjusted))"""
                )
                columns = {row[1].lower() for row in conn.execute("PRAGMA table_info(daily_price_cache)")}
                if "open" not in columns:
                    conn.execute("ALTER TABLE daily_price_cache ADD COLUMN open REAL")
                    # Legacy cached rows cannot supply Open. Keep them, but invalidate
                    # their range metadata so the next scan refreshes full OHLCV.
                    try:
                        conn.execute("DELETE FROM cache_metadata")
                    except sqlite3.OperationalError:
                        pass
                if "source" not in columns:
                    conn.execute("ALTER TABLE daily_price_cache ADD COLUMN source TEXT")
                    try:
                        conn.execute("DELETE FROM cache_metadata")
                    except sqlite3.OperationalError:
                        pass
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS cache_metadata (
                        symbol TEXT NOT NULL, adjusted INTEGER NOT NULL,
                        last_checked TEXT, status TEXT, request_start TEXT, request_end TEXT,
                        provider_data_time TEXT, official_eod_date TEXT,
                        PRIMARY KEY (symbol, adjusted))"""
                )
                metadata = {row[1].lower() for row in conn.execute("PRAGMA table_info(cache_metadata)")}
                for column in (
                    "request_start",
                    "request_end",
                    "provider_data_time",
                    "official_eod_date",
                ):
                    if column not in metadata:
                        conn.execute(f"ALTER TABLE cache_metadata ADD COLUMN {column} TEXT")

    def load(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp, adjusted: bool) -> pd.DataFrame:
        technical_adjusted = bool(adjusted)
        with closing(self._connect()) as conn:
            rows = pd.read_sql_query(
                """SELECT date, open, high, low, close, volume
                   FROM daily_price_cache
                   WHERE symbol=? AND adjusted=? AND date>=? AND date<=?
                   ORDER BY date""",
                conn,
                params=(symbol, 0, start.date().isoformat(), end.date().isoformat()),
                parse_dates=["date"],
            )
        if rows.empty:
            cached = pd.DataFrame(columns=self.COLUMNS)
        else:
            rows = rows.set_index("date")
            rows.columns = self.COLUMNS
            cached = rows.apply(pd.to_numeric, errors="coerce").dropna(
                subset=["Close", "High"]
            )
            cached = cached[cached["Volume"] > 0]
        # The feeder database is the authority for completed raw NSE sessions.
        # It is merged at read time so an existing Yahoo cache benefits
        # immediately without a destructive cache migration.
        if not str(symbol).strip().upper().endswith(".BO"):
            official = load_official_bhavcopy_history(symbol, start, end)
            covered_dates = set(official.attrs.get("covered_dates", set()))
            if covered_dates and not cached.empty:
                official_dates = {stamp.normalize() for stamp in official.index}
                remove_dates = [
                    stamp
                    for stamp in cached.index
                    if stamp.normalize() in covered_dates
                    and stamp.normalize() not in official_dates
                ]
                if remove_dates:
                    cached = cached.drop(index=remove_dates, errors="ignore")
            if not official.empty:
                cached = pd.concat([cached, official])
                cached = cached[~cached.index.duplicated(keep="last")].sort_index()
        if technical_adjusted:
            cached = apply_technical_adjustments(cached, symbol, end)
        return cached

    def last_close(self, symbol: str) -> float | None:
        """Latest cached unadjusted close for a symbol, or None when unavailable."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT close FROM daily_price_cache "
                "WHERE symbol=? AND close IS NOT NULL "
                "ORDER BY date DESC, adjusted ASC LIMIT 1",
                (str(symbol).strip().upper(),),
            ).fetchone()
        if not row:
            return None
        try:
            close = float(row[0])
        except (TypeError, ValueError):
            return None
        return close if close > 0 else None

    def candle_source(
        self, symbol: str, candle_date: Any, adjusted: bool
    ) -> str:
        """Provider recorded for one cached daily candle."""
        if candle_date is None:
            return "Unknown"
        try:
            date_key = pd.Timestamp(candle_date).date().isoformat()
        except (TypeError, ValueError):
            return "Unknown"
        if (
            not str(symbol).strip().upper().endswith(".BO")
            and official_bhavcopy_has_candle(symbol, date_key)
        ):
            return "NSE Bhavcopy"
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT source FROM daily_price_cache "
                "WHERE symbol=? AND date=? AND adjusted=?",
                (str(symbol).strip().upper(), date_key, 0),
            ).fetchone()
        return str(row[0]) if row and row[0] else "Unknown (legacy cache)"

    def coverage(
        self, symbol: str, adjusted: bool, conn: sqlite3.Connection | None = None
    ) -> tuple[str | None, str | None, str | None, str | None]:
        owns_connection = conn is None
        connection = conn or self._connect()
        try:
            row = connection.execute(
                "SELECT request_start, request_end, last_checked, status FROM cache_metadata WHERE symbol=? AND adjusted=?",
                (symbol, 0),
            ).fetchone()
        finally:
            if owns_connection:
                connection.close()
        return tuple(row) if row else (None, None, None, None)

    def is_covered(
        self,
        symbol: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
        adjusted: bool,
        latest_request: bool = False,
        latest_cache_ttl_seconds: int = DEFAULT_REFRESH_MINUTES * 60,
        now: datetime | None = None,
    ) -> bool:
        # A completed historical request can be served wholly by the feeder,
        # even when the older Yahoo cache metadata is absent or stale. Latest
        # requests deliberately retain their TTL checks for intraday candles.
        if (
            not latest_request
            and not str(symbol).strip().upper().endswith(".BO")
            and official_bhavcopy_history_covers(start, end)
        ):
            return True
        actual_start, actual_end, checked, status = self.coverage(symbol, adjusted)
        if status != "ok" or not actual_start or not actual_end:
            return False
        covered = (
            pd.Timestamp(actual_start) <= coverage_start_deadline(start)
            and pd.Timestamp(actual_end) >= coverage_end_floor(end)
        )
        if not covered:
            return False
        if not str(symbol).strip().upper().endswith(".BO"):
            required_eod = coverage_end_floor(end).date().isoformat()
            verified_eod = self.official_eod_date(symbol, adjusted)
            if not verified_eod or pd.Timestamp(verified_eod) < pd.Timestamp(required_eod):
                # Old/provisional cache entries are refreshed once so the most recent
                # completed candle is verified against the official NSE bhavcopy.
                return False
        # Historical scans have no age expiry. Latest scans refresh once their
        # cache is over one minute old while NSE is trading.
        ist_now = get_ist_now()
        if latest_request and end.date() >= ist_now.date():
            current = now or ist_now
            if not checked:
                return False
            checked_at = datetime.fromisoformat(checked)
            if nse_market_is_open(current):
                age_seconds = (current - checked_at).total_seconds()
                if age_seconds < 0 or age_seconds > max(1, int(latest_cache_ttl_seconds)):
                    return False
            elif NSE_CALENDAR.is_session_date(current.date()):
                session_close = NSE_CALENDAR.session_close_time(
                    current.date(), NSE_MARKET_CLOSE
                )
                # Exceptional sessions with unannounced hours are never treated
                # as safely complete from a cached intraday response.
                if session_close is None:
                    return False
                # A pre-close check cannot satisfy a latest EOD request once the
                # authoritative close for this session has passed.
                if (
                    current.time() >= session_close
                    and checked_at.date() == current.date()
                    and checked_at.time() < session_close
                ):
                    return False
            # Caches created before this feature have prices but no Yahoo market
            # timestamp. Refresh them once so the new timing columns are useful
            # immediately instead of remaining unavailable for the whole day.
            if not self.timing(symbol, adjusted)[0]:
                return False
        return True

    def timing(self, symbol: str, adjusted: bool) -> tuple[str | None, str | None]:
        """Return Yahoo's market-data timestamp and this cache entry's fetch time."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT provider_data_time, last_checked FROM cache_metadata
                   WHERE symbol=? AND adjusted=?""",
                (symbol, 0),
            ).fetchone()
        return tuple(row) if row else (None, None)

    def official_eod_date(self, symbol: str, adjusted: bool) -> str | None:
        """Most recent completed candle verified from an official NSE bhavcopy."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT official_eod_date FROM cache_metadata "
                "WHERE symbol=? AND adjusted=?",
                (symbol, 0),
            ).fetchone()
        return str(row[0]) if row and row[0] else None

    def store(
        self,
        symbol: str,
        frame: pd.DataFrame,
        adjusted: bool,
        request_start: pd.Timestamp,
        request_end: pd.Timestamp,
        status: str = "ok",
    ) -> None:
        provider_data_time = getattr(frame, "attrs", {}).get("provider_data_time")
        official_eod_date = getattr(frame, "attrs", {}).get("official_eod_date")
        source_by_date = getattr(frame, "attrs", {}).get("source_by_date", {})
        clean = normalize_download(frame)
        records = [
            (
                symbol,
                idx.date().isoformat(),
                finite_or_none(row["Open"]),
                finite_or_none(row["High"]),
                finite_or_none(row["Low"]),
                finite_or_none(row["Close"]),
                finite_or_none(row["Volume"]),
                str(source_by_date.get(idx.date().isoformat(), "Unknown")),
                0,
            )
            for idx, row in clean.iterrows()
        ]
        with closing(self._connect()) as conn:
            with conn:
                if records:
                    conn.executemany(
                        """INSERT OR REPLACE INTO daily_price_cache
                           (symbol,date,open,high,low,close,volume,source,adjusted)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        records,
                    )
                # Metadata describes the interval proven by this provider
                # response, not MIN/MAX across potentially disjoint cache islands.
                actual_start = (
                    clean.index.min().date().isoformat() if records else None
                )
                actual_end = (
                    clean.index.max().date().isoformat() if records else None
                )
                if official_eod_date and (
                    not actual_end
                    or pd.Timestamp(official_eod_date) > pd.Timestamp(actual_end)
                ):
                    # A verified no-trade terminal session has no candle record,
                    # but the official file still proves date coverage.
                    actual_end = str(official_eod_date)
                start_covered = bool(
                    actual_start
                    and pd.Timestamp(actual_start) <= coverage_start_deadline(request_start)
                )
                end_covered = bool(
                    actual_end
                    and pd.Timestamp(actual_end) >= coverage_end_floor(request_end)
                )
                if not records:
                    effective_status = "empty"
                elif start_covered and end_covered:
                    effective_status = "ok"
                else:
                    effective_status = "truncated"
                conn.execute(
                    """INSERT OR REPLACE INTO cache_metadata
                       (symbol,adjusted,last_checked,status,request_start,request_end,
                        provider_data_time,official_eod_date)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        symbol,
                        0,
                        get_ist_now().isoformat(timespec="seconds"),
                        effective_status if status == "ok" else status,
                        actual_start,
                        actual_end,
                        provider_data_time,
                        official_eod_date,
                    ),
                )


    def store_intraday_quotes(
        self,
        quotes: dict[str, Any],
        request_start: pd.Timestamp,
        request_end: pd.Timestamp,
    ) -> None:
        """Persist one validated unfinished Tickertape candle per symbol."""
        if not quotes:
            return
        request_start_text = pd.Timestamp(request_start).date().isoformat()
        request_end_text = pd.Timestamp(request_end).date().isoformat()
        completed_text = coverage_end_floor(
            pd.Timestamp(request_end) - pd.Timedelta(days=1)
        ).date().isoformat()
        checked_at = get_ist_now().isoformat(timespec="seconds")
        price_rows = []
        metadata_rows = []
        for symbol, quote in quotes.items():
            price_rows.append(
                (
                    symbol,
                    quote.trade_date.isoformat(),
                    quote.open,
                    quote.high,
                    quote.low,
                    quote.close,
                    quote.volume,
                    TICKERTAPE_SOURCE,
                    0,
                )
            )
            metadata_rows.append(
                (
                    symbol,
                    0,
                    checked_at,
                    "ok",
                    request_start_text,
                    request_end_text,
                    quote.provider_data_time,
                    completed_text,
                )
            )
        with closing(self._connect()) as conn:
            with conn:
                conn.executemany(
                    """INSERT OR REPLACE INTO daily_price_cache
                       (symbol,date,open,high,low,close,volume,source,adjusted)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    price_rows,
                )
                conn.executemany(
                    """INSERT OR REPLACE INTO cache_metadata
                       (symbol,adjusted,last_checked,status,request_start,request_end,
                        provider_data_time,official_eod_date)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    metadata_rows,
                )

def finite_or_none(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def price_basis_label(adjusted: bool) -> str:
    return "Technical Adjusted" if adjusted else "Official Raw"


def daily_percent_changes(frame: pd.DataFrame) -> pd.Series:
    """Daily close-to-close percentage change, with invalid bases left missing."""
    if frame is None or frame.empty or "Close" not in frame:
        return pd.Series(dtype=float)
    close = pd.to_numeric(frame["Close"], errors="coerce")
    previous = close.shift(1)
    change = close.div(previous).sub(1.0).mul(100.0)
    return change.where(previous.ne(0)).replace([np.inf, -np.inf], np.nan)


def first_business_day_on_or_after(value: pd.Timestamp) -> pd.Timestamp:
    return pd.Timestamp(NSE_CALENDAR.next_session(pd.Timestamp(value).date()))


def completed_market_date(value: pd.Timestamp) -> pd.Timestamp:
    stamp = pd.Timestamp(value).normalize()
    ist_now = get_ist_now()
    if stamp.date() == ist_now.date() and NSE_CALENDAR.is_session_date(stamp.date()):
        session_close = NSE_CALENDAR.session_close_time(stamp.date(), NSE_MARKET_CLOSE)
        # Unknown exceptional-session timings fail closed for the whole day.
        if session_close is None or ist_now.time() < session_close:
            stamp = stamp - timedelta(days=1)
    return pd.Timestamp(NSE_CALENDAR.previous_session(stamp.date()))


def nse_market_is_open(value: datetime | None = None) -> bool:
    """Return whether an IST-local time falls within the regular NSE session."""
    current = value or get_ist_now()
    return NSE_CALENDAR.is_open_at(
        current,
        regular_open=NSE_MARKET_OPEN,
        regular_close=NSE_MARKET_CLOSE,
    )


def coverage_start_deadline(value: pd.Timestamp) -> pd.Timestamp:
    """The first weekday on which the provider can satisfy the request."""
    return first_business_day_on_or_after(value)


def coverage_end_floor(value: pd.Timestamp) -> pd.Timestamp:
    """The most recent completed weekday required by the request."""
    return completed_market_date(value)


def normalize_download(frame: pd.DataFrame) -> pd.DataFrame:
    attrs = dict(getattr(frame, "attrs", {})) if frame is not None else {}
    if frame is None or frame.empty:
        result = pd.DataFrame(columns=PriceCache.COLUMNS)
        result.attrs.update(attrs)
        return result
    data = frame.copy()
    if isinstance(data.columns, pd.MultiIndex):
        # yfinance may return (field, ticker) or (ticker, field).
        field_level = 0 if "Close" in data.columns.get_level_values(0) else 1
        data.columns = data.columns.get_level_values(field_level)
    data.columns = [str(column).title() for column in data.columns]
    for column in PriceCache.COLUMNS:
        if column not in data:
            data[column] = np.nan
    data = data[PriceCache.COLUMNS]
    data.index = pd.to_datetime(data.index, errors="coerce").tz_localize(None).normalize()
    data = data[~data.index.isna()]
    result = data[~data.index.duplicated(keep="last")].sort_index().apply(pd.to_numeric, errors="coerce")
    result.attrs.update(attrs)
    return result


def expected_completed_sessions(
    start: pd.Timestamp, end: pd.Timestamp
) -> list[pd.Timestamp]:
    """Expected NSE sessions through the last completed candle in a request."""
    first = pd.Timestamp(start).tz_localize(None).normalize()
    last = coverage_end_floor(pd.Timestamp(end)).tz_localize(None).normalize()
    if first > last:
        return []
    sessions: list[pd.Timestamp] = []
    cursor = first
    while cursor <= last:
        if NSE_CALENDAR.is_session_date(cursor.date()):
            sessions.append(cursor)
        cursor += timedelta(days=1)
    return sessions


def missing_completed_sessions(
    frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp
) -> list[pd.Timestamp]:
    """Find internal/terminal NSE-session gaps without penalising pre-listing dates."""
    clean = normalize_download(frame)
    expected = expected_completed_sessions(start, end)
    if not expected:
        return []
    valid = clean.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    valid = valid[valid["Volume"] > 0]
    if valid.empty:
        return [expected[-1]]
    first_traded = valid.index.min().normalize()
    available = {stamp.normalize() for stamp in valid.index}
    return [stamp for stamp in expected if stamp >= first_traded and stamp not in available]


def _official_history_connection() -> sqlite3.Connection | None:
    """Open the feeder database read-only when its expected schema is present."""
    if not BHAVCOPY_HISTORY_DB.exists():
        return None
    try:
        connection = sqlite3.connect(
            f"file:{BHAVCOPY_HISTORY_DB.as_posix()}?mode=ro", uri=True, timeout=10
        )
        connection.execute("PRAGMA query_only=ON")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {"bhavcopy_files", "official_prices"}.issubset(tables):
            connection.close()
            return None
        return connection
    except sqlite3.Error:
        return None


def official_bhavcopy_file_dates(
    start: pd.Timestamp, end: pd.Timestamp
) -> set[pd.Timestamp]:
    """Dates conclusively covered by the feeder.

    ``ok`` means an official file was validated and imported. ``not_published``
    means the feeder checked the official archive and confirmed that no file
    exists for that date (normally an exchange holiday). Both outcomes resolve
    the date; failed or unchecked dates deliberately do not.
    """
    connection = _official_history_connection()
    if connection is None:
        return set()
    try:
        rows = connection.execute(
            "SELECT trade_date FROM bhavcopy_files "
            "WHERE status IN ('ok','not_published') "
            "AND trade_date>=? AND trade_date<=?",
            (
                pd.Timestamp(start).date().isoformat(),
                pd.Timestamp(end).date().isoformat(),
            ),
        ).fetchall()
        return {pd.Timestamp(row[0]).normalize() for row in rows}
    except (sqlite3.Error, TypeError, ValueError):
        return set()
    finally:
        connection.close()


def official_bhavcopy_history_covers(
    start: pd.Timestamp, end: pd.Timestamp
) -> bool:
    """True when each expected date has a file or confirmed non-publication."""
    try:
        modified = BHAVCOPY_HISTORY_DB.stat().st_mtime_ns
        wal_path = Path(str(BHAVCOPY_HISTORY_DB) + "-wal")
        if wal_path.exists():
            modified = max(modified, wal_path.stat().st_mtime_ns)
    except OSError:
        return False
    key = (
        int(modified),
        pd.Timestamp(start).date().isoformat(),
        coverage_end_floor(pd.Timestamp(end)).date().isoformat(),
    )
    with _OFFICIAL_HISTORY_LOCK:
        cached = _OFFICIAL_HISTORY_COVERAGE_CACHE.get(key)
    if cached is not None:
        return cached
    expected = expected_completed_sessions(start, end)
    if not expected:
        return False
    available = official_bhavcopy_file_dates(start, end)
    covered = all(stamp.normalize() in available for stamp in expected)
    with _OFFICIAL_HISTORY_LOCK:
        # Retain only entries for the current database generation.
        stale = [item for item in _OFFICIAL_HISTORY_COVERAGE_CACHE if item[0] != modified]
        for item in stale:
            _OFFICIAL_HISTORY_COVERAGE_CACHE.pop(item, None)
        _OFFICIAL_HISTORY_COVERAGE_CACHE[key] = covered
    return covered


def load_official_bhavcopy_history(
    symbol: str, start: pd.Timestamp, end: pd.Timestamp
) -> pd.DataFrame:
    """Load immutable raw EQ/BE/BZ candles created by ChartSageBhavcopy.py."""
    raw_symbol = (
        str(symbol).replace(".NS", "").replace(".BO", "").strip().upper()
    )
    if not raw_symbol or str(symbol).strip().upper().endswith(".BO"):
        return pd.DataFrame(columns=PriceCache.COLUMNS)
    connection = _official_history_connection()
    if connection is None:
        return pd.DataFrame(columns=PriceCache.COLUMNS)
    try:
        file_rows = connection.execute(
            "SELECT trade_date FROM bhavcopy_files "
            "WHERE status IN ('ok','not_published') "
            "AND trade_date>=? AND trade_date<=?",
            (
                pd.Timestamp(start).date().isoformat(),
                pd.Timestamp(end).date().isoformat(),
            ),
        ).fetchall()
        covered_dates = {pd.Timestamp(row[0]).normalize() for row in file_rows}
        rows = pd.read_sql_query(
            """SELECT trade_date,open,high,low,close,volume,series
               FROM official_prices
               WHERE symbol=? AND trade_date>=? AND trade_date<=?
                 AND series IN ('EQ','BE','BZ')
               ORDER BY trade_date,
                 CASE series WHEN 'EQ' THEN 0 WHEN 'BE' THEN 1 ELSE 2 END""",
            connection,
            params=(
                raw_symbol,
                pd.Timestamp(start).date().isoformat(),
                pd.Timestamp(end).date().isoformat(),
            ),
            parse_dates=["trade_date"],
        )
    except (sqlite3.Error, ValueError):
        return pd.DataFrame(columns=PriceCache.COLUMNS)
    finally:
        connection.close()
    if rows.empty:
        result = pd.DataFrame(columns=PriceCache.COLUMNS)
        result.attrs["covered_dates"] = covered_dates
        return result
    rows = rows.drop_duplicates(subset=["trade_date"], keep="first").set_index(
        "trade_date"
    )
    rows = rows.drop(columns=["series"])
    rows.columns = PriceCache.COLUMNS
    result = rows.apply(pd.to_numeric, errors="coerce")
    result = result.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    result = result[result["Volume"] > 0].sort_index()
    result.attrs["source_by_date"] = {
        stamp.date().isoformat(): "NSE Bhavcopy" for stamp in result.index
    }
    result.attrs["covered_dates"] = covered_dates
    return result


def official_bhavcopy_has_candle(symbol: str, date_key: str) -> bool:
    raw_symbol = (
        str(symbol).replace(".NS", "").replace(".BO", "").strip().upper()
    )
    connection = _official_history_connection()
    if connection is None or not raw_symbol:
        return False
    try:
        row = connection.execute(
            "SELECT 1 FROM official_prices WHERE symbol=? AND trade_date=? "
            "AND series IN ('EQ','BE','BZ') LIMIT 1",
            (raw_symbol, date_key),
        ).fetchone()
        return bool(row)
    except sqlite3.Error:
        return False
    finally:
        connection.close()


def apply_technical_adjustments(
    frame: pd.DataFrame, symbol: str, as_of: pd.Timestamp
) -> pd.DataFrame:
    """Return split/bonus/consolidation-adjusted OHLCV without changing raw data."""
    clean = normalize_download(frame)
    if clean.empty or str(symbol).strip().upper().endswith(".BO"):
        return clean
    raw_symbol = str(symbol).replace(".NS", "").strip().upper()
    connection = _official_history_connection()
    if connection is None:
        return clean
    try:
        actions = connection.execute(
            """SELECT ex_date,adjustment_factor FROM corporate_actions
               WHERE symbol=? AND parse_status='supported'
                 AND adjustment_factor>0 AND ex_date<=?
               ORDER BY ex_date""",
            (raw_symbol, pd.Timestamp(as_of).date().isoformat()),
        ).fetchall()
    except sqlite3.Error:
        return clean
    finally:
        connection.close()
    result = clean.copy()
    for ex_date_text, factor_value in actions:
        try:
            ex_date = pd.Timestamp(ex_date_text).normalize()
            factor = float(factor_value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(factor) or factor <= 0 or factor == 1:
            continue
        mask = result.index < ex_date
        result.loc[mask, ["Open", "High", "Low", "Close"]] = result.loc[
            mask, ["Open", "High", "Low", "Close"]
        ].div(factor)
        result.loc[mask, "Volume"] = result.loc[mask, "Volume"].mul(factor)
    result.attrs.update(getattr(clean, "attrs", {}))
    result.attrs["price_basis"] = "Technical Adjusted"
    return result


def fetch_official_nse_bhavcopy(
    trade_date: date,
) -> dict[str, dict[str, float]]:
    """Download one official NSE CM UDiFF bhavcopy and cache it in memory.

    Failed publication checks are cached briefly so a 500-stock scan makes one
    request, while later auto-scans can discover the file once NSE publishes it.
    The downloaded ZIP is parsed in memory; validated rows are persisted only via
    the normal SQLite price cache.
    """
    now = get_ist_now()
    with _NSE_BHAVCOPY_LOCK:
        cached = _NSE_BHAVCOPY_MEMORY.get(trade_date)
        if cached:
            checked_at, rows = cached
            if rows or (now - checked_at).total_seconds() < NSE_BHAVCOPY_RETRY_MINUTES * 60:
                return rows

        url = NSE_BHAVCOPY_URL.format(trade_date=trade_date.strftime("%Y%m%d"))
        rows: dict[str, dict[str, float]] = {}
        try:
            response = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                timeout=(5, 20),
            )
            response.raise_for_status()
            with ZipFile(BytesIO(response.content)) as archive:
                csv_names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
                if not csv_names:
                    raise ValueError("Official NSE bhavcopy ZIP contained no CSV file.")
                table = pd.read_csv(BytesIO(archive.read(csv_names[0])), low_memory=False)
            required = {
                "TradDt",
                "TckrSymb",
                "SctySrs",
                "OpnPric",
                "HghPric",
                "LwPric",
                "ClsPric",
                "TtlTradgVol",
            }
            if not required.issubset(table.columns):
                raise ValueError("Official NSE bhavcopy has an unexpected schema.")
            table = table[
                table["TradDt"].astype(str).eq(trade_date.isoformat())
                & table["SctySrs"]
                .astype(str)
                .str.strip()
                .str.upper()
                .isin({"EQ", "BE", "BZ"})
            ]
            for item in table.to_dict("records"):
                symbol = str(item.get("TckrSymb", "")).strip().upper()
                values = {
                    "Open": finite_or_none(item.get("OpnPric")),
                    "High": finite_or_none(item.get("HghPric")),
                    "Low": finite_or_none(item.get("LwPric")),
                    "Close": finite_or_none(item.get("ClsPric")),
                    "Volume": finite_or_none(item.get("TtlTradgVol")),
                }
                if symbol and all(value is not None for value in values.values()):
                    rows[symbol] = {key: float(value) for key, value in values.items()}
        except Exception:
            rows = {}
        _NSE_BHAVCOPY_MEMORY[trade_date] = (now, rows)
        return rows


def overlay_official_nse_eod(
    frame: pd.DataFrame,
    symbol: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    split_events: Iterable[dict[str, Any]] | None = None,
) -> pd.DataFrame:
    """Repair gaps and replace the terminal completed candle with official NSE EOD.

    A successfully loaded bhavcopy with no row for the symbol proves a genuine
    no-trade session. Historical repairs are split/bonus-adjusted to the same
    share basis as Yahoo/TradingView, while the terminal as-of candle remains the
    actual price that traded on that date (future actions are never requested).
    """
    clean = normalize_download(frame)
    if str(symbol).strip().upper().endswith(".BO"):
        return clean
    attrs = dict(getattr(clean, "attrs", {}))
    source_by_date = dict(attrs.get("source_by_date", {}))
    expected = expected_completed_sessions(start, end)
    if not expected:
        return clean
    terminal = expected[-1]
    archived = load_official_bhavcopy_history(symbol, start, end)
    archived_file_dates = set(archived.attrs.get("covered_dates", set()))
    if archived_file_dates:
        archived_dates = {stamp.normalize() for stamp in archived.index}
        # A validated daily file with no row for this symbol proves that no
        # official candle exists for it on that session. Remove provisional
        # provider artefacts before overlaying the immutable official rows.
        remove_dates = [
            stamp
            for stamp in clean.index
            if stamp.normalize() in archived_file_dates
            and stamp.normalize() not in archived_dates
        ]
        if remove_dates:
            clean = clean.drop(index=remove_dates, errors="ignore")
            for stamp in remove_dates:
                source_by_date.pop(stamp.date().isoformat(), None)
    if not archived.empty:
        clean = pd.concat([clean, archived])
        clean = clean[~clean.index.duplicated(keep="last")].sort_index()
        source_by_date.update(archived.attrs.get("source_by_date", {}))
    candidates = set(missing_completed_sessions(clean, start, end))
    candidates.add(terminal)
    # Dates already represented by a validated feeder file need no network
    # request. This includes a genuine no-trade date with no symbol row.
    candidates = {
        stamp for stamp in candidates if stamp.normalize() not in archived_file_dates
    }
    raw_symbol = symbol.replace(".NS", "").replace(".BO", "").strip().upper()
    audit_complete = True
    events = list(split_events or [])
    for stamp in sorted(candidates):
        bhavcopy = fetch_official_nse_bhavcopy(stamp.date())
        if not bhavcopy:
            audit_complete = False
            continue
        official = bhavcopy.get(raw_symbol)
        if not official:
            # The exchange file exists and contains no row for this security:
            # there was no candle, so remove any provisional provider artefact.
            clean = clean.drop(index=stamp, errors="ignore")
            source_by_date.pop(stamp.date().isoformat(), None)
            continue
        values = {column: float(official[column]) for column in PriceCache.COLUMNS}
        split_ratio = 1.0
        for event in events:
            try:
                event_date = (
                    pd.Timestamp(float(event.get("date")), unit="s", tz="UTC")
                    .tz_convert("Asia/Kolkata")
                    .date()
                )
                numerator = float(event.get("numerator"))
                denominator = float(event.get("denominator"))
                if event_date > stamp.date() and numerator > 0 and denominator > 0:
                    split_ratio *= numerator / denominator
            except (TypeError, ValueError, OverflowError):
                continue
        if split_ratio != 1.0:
            for column in ("Open", "High", "Low", "Close"):
                values[column] /= split_ratio
            values["Volume"] *= split_ratio
        clean.loc[stamp, PriceCache.COLUMNS] = [values[column] for column in PriceCache.COLUMNS]
        source_by_date[stamp.date().isoformat()] = "NSE Bhavcopy"
    clean = normalize_download(clean)
    clean.attrs.update(attrs)
    clean.attrs["source_by_date"] = source_by_date
    if audit_complete:
        clean.attrs["official_eod_date"] = terminal.date().isoformat()
    return clean


def yahoo_payload_to_frame(
    payload: dict[str, Any],
    symbol: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    adjusted: bool,
) -> pd.DataFrame:
    """Build a Yahoo frame, overlay official completed EOD data, then adjust if asked."""
    quote = (payload.get("indicators", {}).get("quote") or [{}])[0]
    timestamps = pd.to_datetime(payload.get("timestamp", []), unit="s", utc=True).tz_convert(
        "Asia/Kolkata"
    )
    index = timestamps.tz_localize(None).normalize()
    frame = pd.DataFrame(
        {
            "Open": quote.get("open"),
            "High": quote.get("high"),
            "Low": quote.get("low"),
            "Close": quote.get("close"),
            "Volume": quote.get("volume"),
        },
        index=index,
    )
    frame.attrs["source_by_date"] = {
        stamp.date().isoformat(): "Yahoo" for stamp in frame.index
    }
    # Always cache the provider's raw series. Technical adjustments are applied
    # dynamically by PriceCache.load from the feeder's official action table.
    frame = overlay_official_nse_eod(frame, symbol, start, end)
    return normalize_download(frame)


def fetch_zerodha_historical(symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Fallback fetcher using active Zerodha Kite OMS historical API session."""
    if not ZERODHA_SESSION_FILE.exists():
        return pd.DataFrame(columns=PriceCache.COLUMNS)
    try:
        payload = json.loads(ZERODHA_SESSION_FILE.read_text(encoding="utf-8"))
        user_id = str(payload.get("user_id", "")).strip().upper()
        enctoken = BrokerLoginStore().get_token_for_user(user_id)
        if not enctoken:
            return pd.DataFrame(columns=PriceCache.COLUMNS)
        
        raw_sym = symbol.replace(".NS", "").replace(".BO", "").strip().upper()
        search_resp = requests.get(
            f"https://kite.zerodha.com/oms/instruments?symbol={raw_sym}",
            headers={"Authorization": f"enctoken {enctoken}", "User-Agent": "Mozilla/5.0"},
            timeout=5,
        )
        if search_resp.status_code != 200:
            return pd.DataFrame(columns=PriceCache.COLUMNS)
            
        instruments = search_resp.json().get("data", [])
        instrument_token = None
        for inst in instruments:
            if inst.get("tradingsymbol") == raw_sym and inst.get("exchange") in ("NSE", "BSE"):
                instrument_token = inst.get("instrument_token")
                break
        if not instrument_token and instruments:
            instrument_token = instruments[0].get("instrument_token")
            
        if not instrument_token:
            return pd.DataFrame(columns=PriceCache.COLUMNS)
            
        from_date = pd.Timestamp(start).strftime("%Y-%m-%d")
        to_date = pd.Timestamp(end).strftime("%Y-%m-%d")
        hist_url = f"https://kite.zerodha.com/oms/instruments/historical/{instrument_token}/day"
        hist_resp = requests.get(
            hist_url,
            params={"from": from_date, "to": to_date},
            headers={"Authorization": f"enctoken {enctoken}", "User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if hist_resp.status_code != 200:
            return pd.DataFrame(columns=PriceCache.COLUMNS)
            
        candles = hist_resp.json().get("data", {}).get("candles", [])
        if not candles:
            return pd.DataFrame(columns=PriceCache.COLUMNS)
            
        records = []
        for c in candles:
            ts = pd.to_datetime(c[0]).tz_localize(None).normalize()
            records.append({
                "Date": ts,
                "Open": float(c[1]),
                "High": float(c[2]),
                "Low": float(c[3]),
                "Close": float(c[4]),
                "Volume": float(c[5]) if len(c) > 5 else 0.0,
            })
        df = pd.DataFrame(records).set_index("Date")
        df.attrs["source_by_date"] = {
            stamp.date().isoformat(): "Zerodha" for stamp in df.index
        }
        return normalize_download(df)
    except Exception:
        return pd.DataFrame(columns=PriceCache.COLUMNS)


def download_symbol_fallback(symbol: str, start: pd.Timestamp, end: pd.Timestamp, adjusted: bool) -> pd.DataFrame:
    """Secondary fallback fetcher using alternative Yahoo endpoint & Zerodha Kite API."""
    ticker = symbol if symbol.upper().endswith((".NS", ".BO")) else symbol.upper() + ".NS"
    period_start = int(pd.Timestamp(start).tz_localize("UTC").timestamp())
    period_end = yahoo_period_end_epoch(end)
    try:
        response = requests.get(
            f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={"period1": period_start, "period2": period_end, "interval": "1d", "events": "div,splits"},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=(5, 15),
        )
        if response.status_code == 200:
            chart = response.json().get("chart", {})
            results = chart.get("result") or []
            if results and results[0].get("timestamp"):
                payload = results[0]
                return yahoo_payload_to_frame(payload, symbol, start, end, adjusted)
    except Exception:
        pass

    # Option 3: Fallback to Zerodha Kite OMS historical API if logged in
    try:
        zk_df = fetch_zerodha_historical(symbol, start, end)
        if not zk_df.empty:
            return overlay_official_nse_eod(zk_df, symbol, start, end)
    except Exception:
        pass

    official_only = overlay_official_nse_eod(
        pd.DataFrame(columns=PriceCache.COLUMNS), symbol, start, end
    )
    return official_only


def yahoo_period_end_epoch(end: pd.Timestamp) -> int:
    """Return Yahoo's exclusive UTC end epoch without NumPy timedelta coercion."""
    end_date = pd.Timestamp(end)
    if end_date.tzinfo is not None:
        # The request is date-based. Preserve the caller's calendar date rather
        # than converting local midnight and shifting the boundary in UTC.
        end_date = end_date.tz_localize(None)
    exclusive_end = (end_date.normalize() + timedelta(days=1)).tz_localize("UTC")
    return int(exclusive_end.timestamp())


def download_symbol(symbol: str, start: pd.Timestamp, end: pd.Timestamp, adjusted: bool) -> pd.DataFrame:
    """Fetch one symbol through Yahoo's chart endpoint with smart request staggering, retries, and fallback."""
    ticker = symbol if symbol.upper().endswith((".NS", ".BO")) else symbol.upper() + ".NS"
    period_start = int(pd.Timestamp(start).tz_localize("UTC").timestamp())
    period_end = yahoo_period_end_epoch(end)
    
    last_exc = None
    for attempt in range(1, 4):
        try:
            # Micro-staggering to avoid burst concurrency throttling
            _time.sleep(0.01 * attempt + random.uniform(0.005, 0.02))
            response = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                params={
                    "period1": period_start,
                    "period2": period_end,
                    "interval": "1d",
                    "events": "div,splits",
                },
                headers={"User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) ChartSage/1.{attempt}"},
                timeout=(5, 15),
            )
            response.raise_for_status()
            chart = response.json().get("chart", {})
            if chart.get("error"):
                raise ValueError(chart["error"].get("description") or f"Yahoo returned an error for {ticker}")
            results = chart.get("result") or []
            if not results or not results[0].get("timestamp"):
                raise ValueError(f"Yahoo returned no daily candles for {ticker}")
            payload = results[0]
            market_epoch = payload.get("meta", {}).get("regularMarketTime")
            frame = yahoo_payload_to_frame(payload, symbol, start, end, adjusted)
            if market_epoch is not None and pd.Timestamp(end).date() >= date.today():
                try:
                    frame.attrs["provider_data_time"] = (
                        pd.Timestamp(market_epoch, unit="s", tz="UTC")
                        .tz_convert("Asia/Kolkata")
                        .isoformat()
                    )
                except (TypeError, ValueError, OverflowError):
                    pass
            return frame
        except Exception as exc:
            last_exc = exc
            if attempt < 3:
                _time.sleep(0.3 * (2 ** attempt))
                
    # Fallback to secondary endpoint if primary retries exhausted
    try:
        fallback_frame = download_symbol_fallback(symbol, start, end, adjusted)
        if not fallback_frame.empty:
            return fallback_frame
    except Exception:
        pass

    if last_exc:
        raise last_exc
    return pd.DataFrame(columns=PriceCache.COLUMNS)


def load_sectors_cache() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Load sectors, industries, and market caps map from cache, or download and rebuild it (resets at calendar midnight)."""
    cache_file = NSE_SECTORS_CACHE_FILE

    # Try to load from cache first
    try:
        if cache_file.exists():
            # Check if file was modified on the same calendar day (resets at midnight)
            mtime = datetime.fromtimestamp(cache_file.stat().st_mtime)
            if mtime.date() == datetime.now().date():
                payload = json.loads(cache_file.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and "sectors" in payload:
                    return (
                        payload.get("sectors", {}),
                        payload.get("industries", {}),
                        payload.get("market_caps", {}),
                    )
    except Exception:
        pass

    # Rebuild from Tickertape screener API & NSE Archives
    try:
        # 1. Fetch baseline market cap classification from NSE lists
        base_market_caps = {}
        urls = {
            "Large Cap": "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
            "Mid Cap": "https://archives.nseindia.com/content/indices/ind_niftymidcap150list.csv",
            "Small Cap": "https://archives.nseindia.com/content/indices/ind_niftysmallcap250list.csv",
        }
        for category, url in urls.items():
            try:
                resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                if resp.status_code == 200:
                    df = pd.read_csv(StringIO(resp.text))
                    for sym in df["Symbol"].dropna().unique():
                        base_market_caps[sym] = category
            except Exception:
                pass

        # 2. Fetch full lists from Tickertape Screener
        from tickertape_api import TickertapeClient
        downloaded_sectors = {}
        downloaded_industries = {}
        downloaded_market_caps = {}

        with TickertapeClient() as tt:
            result = tt.screener_query({
                "match": {},
                "sortBy": "mrktCapf",
                "sortOrder": -1,
                "offset": 0,
                "count": 10000,
                "project": ["sector", "subindustry", "mrktCapRank"],
            })
            results = result.get("results", []) if isinstance(result, dict) else []
            for r in results:
                stock = r.get("stock", {})
                info = stock.get("info", {})
                adv = stock.get("advancedRatios", {})
                ticker = info.get("ticker", "")
                if ticker:
                    sector = info.get("sector", "")
                    industry = adv.get("subindustry", "")
                    if sector:
                        downloaded_sectors[ticker] = sector
                    if industry:
                        downloaded_industries[ticker] = industry
                    
                    rank = adv.get("mrktCapRank")
                    if rank is not None:
                        try:
                            rank = int(rank)
                            downloaded_market_caps[ticker] = (
                                "Large Cap" if rank <= 100
                                else "Mid Cap" if rank <= 250
                                else "Small Cap"
                            )
                        except (ValueError, TypeError):
                            pass

        if len(downloaded_sectors) >= 5000:
            base_market_caps.update(downloaded_market_caps)
            payload = {
                "downloaded_at": datetime.now().isoformat(),
                "sectors": downloaded_sectors,
                "industries": downloaded_industries,
                "market_caps": base_market_caps,
            }
            cache_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            return downloaded_sectors, downloaded_industries, base_market_caps
        else:
            raise RuntimeError(
                f"Tickertape download incomplete: only got {len(downloaded_sectors)} sectors."
            )
    except Exception:
        pass

    # Final fallback if download failed and we have an old cache file
    try:
        if cache_file.exists():
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and "sectors" in payload:
                return (
                    payload.get("sectors", {}),
                    payload.get("industries", {}),
                    payload.get("market_caps", {}),
                )
    except Exception:
        pass

    return {}, {}, {}


def resolve_universes(
    names: Iterable[str],
    custom_symbols: str = "",
    cancel_check: Callable[[], None] | None = None,
) -> tuple[list[str], list[str], dict[str, dict[str, str]]]:
    symbols: list[str] = []
    errors: list[str] = []
    membership: dict[str, set[str]] = {}
    sectors: dict[str, str] = {}
    for name in names:
        if cancel_check:
            cancel_check()
        source = UNIVERSES.get(name)
        if isinstance(source, list):
            found = source
            symbols.extend(found)
            for symbol in found:
                membership.setdefault(symbol, set()).add(name)
                if name == "PSU RAILWAY":
                    sectors[symbol] = "Railway"
        elif isinstance(source, str):
            try:
                response = requests.get(
                    source,
                    headers={"User-Agent": "Mozilla/5.0 ChartInkScanner/1.0"},
                    timeout=(5, 20),
                )
                response.raise_for_status()
                if cancel_check:
                    cancel_check()
                table = pd.read_csv(StringIO(response.text))
                column = next((c for c in table.columns if str(c).strip().lower() == "symbol"), None)
                if column is None:
                    raise ValueError("CSV has no Symbol column")
                sector_column = next(
                    (c for c in table.columns if str(c).strip().lower() in {"industry", "sector", "industry name"}), None
                )
                found = table[column].dropna().astype(str).str.upper().str.replace(".NS", "", regex=False)
                symbols.extend(found)
                for row_index, symbol in found.items():
                    membership.setdefault(symbol, set()).add(name)
                    if sector_column and pd.notna(table.at[row_index, sector_column]):
                        sectors.setdefault(symbol, str(table.at[row_index, sector_column]).strip())
            except Exception as exc:
                errors.append(f"{name}: {exc}")
    custom = re.split(r"[\s,;]+", custom_symbols.strip())
    symbols.extend(custom)
    for symbol in custom:
        if symbol:
            membership.setdefault(symbol.upper().removesuffix(".NS"), set()).add("Custom")
    clean: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        symbol = str(raw).strip().upper()
        if symbol.endswith(".NS"):
            symbol = symbol[:-3]
        if symbol and symbol not in seen:
            clean.append(symbol)
            seen.add(symbol)
    sectors_map, industries_map, market_caps_map = load_sectors_cache()
    details = {}
    for symbol in clean:
        alias = TICKER_ALIASES.get(symbol, symbol)
        sec = sectors.get(symbol) or sectors_map.get(symbol, "") or sectors_map.get(alias, "")
        ind = industries_map.get(symbol, "") or industries_map.get(alias, "")
        mcap = market_caps_map.get(symbol, "") or market_caps_map.get(alias, "")
        details[symbol] = {
            "Universe": ", ".join(sorted(membership.get(symbol, {"Custom"}))),
            "Sector": sec,
            "Industry": ind,
            "Market Cap": mcap,
        }
    return clean, errors, details


def wilder_rma(series: pd.Series, period: int) -> pd.Series:
    """TradingView/Pine ta.rma: SMA seed, then alpha=1/period recursion."""
    values = pd.to_numeric(series, errors="coerce").astype(float)
    raw = values.to_numpy(dtype=float, copy=False)
    output = np.full(len(raw), np.nan, dtype=float)
    valid_positions = np.flatnonzero(np.isfinite(raw))
    if len(valid_positions) < period:
        return pd.Series(output, index=values.index, dtype=float)

    seed_position = int(valid_positions[period - 1])
    previous = float(raw[valid_positions[:period]].mean())
    output[seed_position] = previous
    alpha = 1.0 / period
    # NumPy array access avoids two expensive Pandas ``iloc`` operations for
    # every historical bar of every scanned stock while preserving the exact
    # TradingView-compatible recurrence and missing-value behavior.
    for position in range(seed_position + 1, len(raw)):
        current = raw[position]
        if math.isfinite(current):
            previous = alpha * current + (1.0 - alpha) * previous
        output[position] = previous
    return pd.Series(output, index=values.index, dtype=float)


def rsi(series: pd.Series, period: int) -> pd.Series:
    close = pd.to_numeric(series, errors="coerce").astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = wilder_rma(gain, period)
    avg_loss = wilder_rma(loss, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        relative_strength = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + relative_strength))


def adx(frame: pd.DataFrame, di_period: int, smoothing_period: int) -> pd.Series:
    """TradingView/Pine DMI ADX: Wilder RMA for TR, directional movement and DX."""
    high = pd.to_numeric(frame["High"], errors="coerce").astype(float)
    low = pd.to_numeric(frame["Low"], errors="coerce").astype(float)
    close = pd.to_numeric(frame["Close"], errors="coerce").astype(float)
    upward = high.diff()
    downward = -low.diff()
    plus_dm = upward.where((upward > downward) & (upward > 0), 0.0)
    minus_dm = downward.where((downward > upward) & (downward > 0), 0.0)
    # Pine's directional movement is undefined on the first candle. Keeping that
    # NaN prevents it from being counted as a zero in the initial RMA seed.
    direction_available = upward.notna() & downward.notna()
    plus_dm = plus_dm.where(direction_available)
    minus_dm = minus_dm.where(direction_available)
    true_range = pd.concat(
        [
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    smoothed_range = wilder_rma(true_range, di_period)
    plus = 100.0 * wilder_rma(plus_dm, di_period) / smoothed_range
    minus = 100.0 * wilder_rma(minus_dm, di_period) / smoothed_range
    plus = plus.where(smoothed_range != 0, 0.0).where(smoothed_range.notna())
    minus = minus.where(smoothed_range != 0, 0.0).where(smoothed_range.notna())
    directional_sum = plus + minus
    dx = (plus - minus).abs() / directional_sum
    dx = dx.where(directional_sum != 0, 0.0).where(directional_sum.notna())
    return 100.0 * wilder_rma(dx, smoothing_period)


def obv(frame: pd.DataFrame) -> pd.Series:
    """TradingView-style cumulative signed volume, starting at zero."""
    close = pd.to_numeric(frame["Close"], errors="coerce").astype(float)
    volume = pd.to_numeric(frame["Volume"], errors="coerce").astype(float)
    direction = np.sign(close.diff()).fillna(0.0)
    return (direction * volume.fillna(0.0)).cumsum()


def macd_components(
    series: pd.Series, fast_period: int, slow_period: int, signal_period: int
) -> dict[str, pd.Series]:
    """MACD components using TradingView-style recursively seeded EMAs."""
    close = pd.to_numeric(series, errors="coerce").astype(float)
    fast_ema = close.ewm(span=fast_period, adjust=False).mean()
    slow_ema = close.ewm(span=slow_period, adjust=False).mean()
    line = fast_ema - slow_ema
    signal = line.ewm(span=signal_period, adjust=False).mean()
    return {
        "MACD Line": line,
        "MACD Signal": signal,
        "MACD Histogram": line - signal,
    }


def supertrend(frame: pd.DataFrame, period: int, multiplier: float) -> pd.Series:
    high, low, close = frame["High"], frame["Low"], frame["Close"]
    tr = pd.concat([(high - low), (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = wilder_rma(tr, period)
    midpoint = (high + low) / 2
    basic_upper, basic_lower = midpoint + multiplier * atr, midpoint - multiplier * atr
    final_upper, final_lower = basic_upper.copy(), basic_lower.copy()
    trend = pd.Series(np.nan, index=frame.index, dtype=float)
    for i in range(1, len(frame)):
        if pd.isna(atr.iloc[i]):
            continue
        previous = i - 1
        previous_upper = final_upper.iloc[previous]
        previous_lower = final_lower.iloc[previous]
        if pd.isna(previous_upper):
            previous_upper = 0.0
        if pd.isna(previous_lower):
            previous_lower = 0.0
        final_upper.iloc[i] = (
            basic_upper.iloc[i]
            if basic_upper.iloc[i] < previous_upper or close.iloc[previous] > previous_upper
            else previous_upper
        )
        final_lower.iloc[i] = (
            basic_lower.iloc[i]
            if basic_lower.iloc[i] > previous_lower or close.iloc[previous] < previous_lower
            else previous_lower
        )
        previous_trend = trend.iloc[previous]
        if pd.isna(atr.iloc[previous]):
            # TradingView initializes Supertrend on the upper band.
            trend.iloc[i] = final_upper.iloc[i]
        elif previous_trend == previous_upper:
            trend.iloc[i] = final_lower.iloc[i] if close.iloc[i] > final_upper.iloc[i] else final_upper.iloc[i]
        else:
            trend.iloc[i] = final_upper.iloc[i] if close.iloc[i] < final_lower.iloc[i] else final_lower.iloc[i]
    return trend


class MarketData:
    def __init__(
        self,
        daily: pd.DataFrame,
        as_of: pd.Timestamp,
        include_current: bool,
        symbol: str = "",
        account_name: str = "ALL",
        holdings_path: Path = HOLDINGS_DETAILS_FILE,
        paper_store_data: dict[str, Any] | None = None,
        paper_account: str = "",
    ):
        raw = normalize_download(daily)
        self.symbol = str(symbol).strip().upper()
        self.account_name = str(account_name).strip().upper() or "ALL"
        self.holdings_path = Path(holdings_path)
        self.paper_store_data = paper_store_data
        self.paper_account = str(paper_account).strip().upper()
        self.as_of = pd.Timestamp(as_of).tz_localize(None).normalize()
        raw = raw.loc[raw.index <= self.as_of]
        ist_now = get_ist_now()
        if not include_current and self.as_of.date() == ist_now.date():
            raw = raw.loc[raw.index <= completed_market_date(self.as_of)]
        self.daily = raw
        self.include_current = include_current
        self._frames: dict[str, pd.DataFrame] = {"D": raw}
        self._series: dict[tuple[Any, ...], pd.Series] = {}

    def frame(self, timeframe: str) -> pd.DataFrame:
        if timeframe in self._frames:
            return self._frames[timeframe]
        aggregation = {
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        }
        if timeframe == "W":
            periods = self.daily.index.to_period("W-SUN")
            result = self.daily.groupby(periods).agg(aggregation)
            result.index = pd.DatetimeIndex(
                [period.end_time.normalize() for period in result.index]
            )
        else:
            try:
                result = self.daily.resample("ME").agg(aggregation)
            except ValueError:  # pandas < 2.2
                result = self.daily.resample("M").agg(aggregation)
        result = result.dropna(subset=["Close"])
        if not self.include_current and not self.daily.empty:
            cutoff = completed_market_date(self.as_of)
            availability = pd.DatetimeIndex(
                [
                    NSE_CALENDAR.period_final_session(stamp.date(), timeframe)
                    for stamp in result.index
                ]
            )
            completed = availability <= cutoff
            result = result.loc[completed]
        self._frames[timeframe] = result
        return result

    def series(self, operand: dict[str, Any]) -> pd.Series:
        key = (
            operand["name"],
            operand["timeframe"],
            int(operand.get("period", 14)),
            float(operand.get("multiplier", 3.0)),
            int(operand.get("fast_period", 12)),
            int(operand.get("slow_period", 26)),
            int(operand.get("signal_period", 9)),
            int(operand.get("adx_smoothing", 14)),
            int(operand.get("lookback_count", 0)),
            str(operand.get("lookback_unit", "")),
        )
        if key in self._series:
            return self._series[key]
        frame = self.frame(operand["timeframe"])
        name = operand["name"]
        period = int(operand.get("period", 14))
        if name in {"Open", "High", "Low", "Close", "Volume"}:
            result = frame[name]
        elif name == "RSI":
            result = rsi(frame["Close"], period)
        elif name == "SMA":
            result = frame["Close"].rolling(period, min_periods=period).mean()
        elif name == "EMA":
            result = frame["Close"].ewm(span=period, min_periods=period, adjust=False).mean()
        elif name == "ADX":
            result = adx(
                frame,
                period,
                int(operand.get("adx_smoothing", 14)),
            )
        elif name == "OBV":
            result = obv(frame)
        elif name == "Supertrend":
            result = supertrend(frame, period, float(operand.get("multiplier", 3.0)))
        elif name in MACD_FIELDS:
            components = macd_components(
                frame["Close"],
                int(operand.get("fast_period", 12)),
                int(operand.get("slow_period", 26)),
                int(operand.get("signal_period", 9)),
            )
            for component_name, component_series in components.items():
                component_key = (
                    component_name,
                    operand["timeframe"],
                    int(operand.get("period", 14)),
                    float(operand.get("multiplier", 3.0)),
                    int(operand.get("fast_period", 12)),
                    int(operand.get("slow_period", 26)),
                    int(operand.get("signal_period", 9)),
                    int(operand.get("adx_smoothing", 14)),
                    int(operand.get("lookback_count", 0)),
                    str(operand.get("lookback_unit", "")),
                )
                self._series[component_key] = component_series
            return self._series[key]
        elif name in HIGH_52_WEEK_FIELDS:
            _count, _unit, lookback_bars = lookback_high_parameters(operand)
            # Use completed daily candles preceding each evaluated candle.
            # Excluding the evaluated candle makes distance symmetric for closes
            # just above a prior high (breakouts) and just below it.
            if _unit == ALL_AVAILABLE_HISTORY_UNIT:
                eligible_high = frame["High"].where(
                    frame.index >= pd.Timestamp(AVAILABLE_HISTORY_START_DATE)
                )
                prior_high = eligible_high.shift(1).expanding(min_periods=1).max()
            else:
                prior_high = (
                    frame["High"].shift(1)
                    .rolling(lookback_bars, min_periods=lookback_bars)
                    .max()
                )
            if name == "52 Week High":
                result = prior_high
            else:
                result = (frame["Close"] - prior_high).abs().div(prior_high).mul(100.0)
        elif name in PNL_FIELDS:
            symbol = getattr(self, "symbol", "")
            if symbol and self.paper_store_data is not None and self.paper_account:
                holding = paper_lookup_holding(
                    self.paper_store_data, self.paper_account, symbol
                )
            else:
                holding = (
                    HoldingsStore(self.holdings_path).get_holding(symbol, self.account_name)
                    if symbol
                    else None
                )
            buy_price = float(holding.get("buy_price", 0)) if holding else 0.0
            if buy_price > 0:
                profit_percent = (
                    (frame["Close"] - buy_price).div(buy_price).mul(100.0)
                )
                result = (
                    profit_percent.mul(-1.0).clip(lower=0.0)
                    if name == "Loss %"
                    else profit_percent
                )
            else:
                result = pd.Series(np.nan, index=frame.index)
        else:
            raise ValueError(f"Unsupported field: {name}")
        self._series[key] = result
        return result

    def scalar(self, operand: dict[str, Any], crossing_step: int = 0) -> float:
        if operand["kind"] == "value":
            return float(operand["value"])
        series = self.series(operand)
        position = int(operand["offset"]) + crossing_step + 1
        if len(series) < position:
            return math.nan
        val = finite_or_none(series.iloc[-position])
        if val is None:
            return math.nan
        if operand.get("name") != "Supertrend":
            val *= float(operand.get("multiplier", 1.0))
        return val


def compare_values(left: float, operator: str, right: float, previous_left: float, previous_right: float) -> bool:
    values = (left, right)
    if any(not math.isfinite(value) for value in values):
        return False
    if operator == ">":
        return left > right
    if operator == ">=":
        return left >= right
    if operator == "<":
        return left < right
    if operator == "<=":
        return left <= right
    if operator == "=":
        return left == right
    if operator == "!=":
        return left != right
    if not all(math.isfinite(value) for value in (previous_left, previous_right)):
        return False
    if operator == "crosses above":
        return left > right and previous_left <= previous_right
    if operator == "crosses below":
        return left < right and previous_left >= previous_right
    raise ValueError(f"Unsupported operator: {operator}")


def within_52_week_high_range(
    close: float,
    prior_high: float,
    above_percent: float,
    below_percent: float,
) -> bool:
    if not all(
        math.isfinite(value)
        for value in (close, prior_high, above_percent, below_percent)
    ) or prior_high <= 0:
        return False
    lower_bound = prior_high * (1.0 - below_percent / 100.0)
    upper_bound = prior_high * (1.0 + above_percent / 100.0)
    return lower_bound <= close <= upper_bound


def evaluate_ast(node: dict[str, Any], market: MarketData, evidence: list[dict[str, Any]]) -> bool:
    if not node_enabled(node):
        return False
    if node["type"] == "group":
        outcomes = [
            evaluate_ast(child, market, evidence)
            for child in node["children"]
            if has_active_rule(child)
        ]
        if not outcomes:
            return False
        op = str(node.get("op", "AND"))
        if op == "AND":
            return all(outcomes)
        elif op == "OR":
            return any(outcomes)
        elif op.startswith("ANY "):
            try:
                k = int(op.split()[1])
            except (IndexError, ValueError):
                k = 1
            return sum(1 for outcome in outcomes if outcome) >= k
        return all(outcomes)
    if is_high_range_operator(node["operator"]):
        # This operator has dedicated above/below percentages, so operand scale
        # multipliers must never distort its Close-versus-prior-high comparison.
        left_operand = {**node["left"], "multiplier": 1.0}
        right_operand = {**node["right"], "multiplier": 1.0}
        left = market.scalar(left_operand)
        right = market.scalar(right_operand)
        previous_left = market.scalar(left_operand, 1)
        previous_right = market.scalar(right_operand, 1)
        above, below, _offset = range_rule_limits(node)
        matched = within_52_week_high_range(left, right, above, below)
    else:
        left = market.scalar(node["left"])
        right = market.scalar(node["right"])
        previous_left = market.scalar(node["left"], 1)
        previous_right = market.scalar(node["right"], 1)
        matched = compare_values(left, node["operator"], right, previous_left, previous_right)
    evidence.append(
        {
            "rule": expression_text(node),
            "left": left,
            "right": right,
            "previous_left": previous_left,
            "previous_right": previous_right,
            "left_operand": node["left"],
            "right_operand": node["right"],
            "operator": node["operator"],
            "matched": matched,
        }
    )
    return matched


class HistoricalMarketData:
    """Causal indicator series aligned to historical daily scan dates."""

    def __init__(
        self,
        daily: pd.DataFrame,
        as_of: pd.Timestamp,
        symbol: str = "",
        account_name: str = "ALL",
        holdings_path: Path = HOLDINGS_DETAILS_FILE,
    ):
        self.symbol = str(symbol).strip().upper()
        self.market = MarketData(
            daily,
            as_of,
            include_current=False,
            symbol=self.symbol,
            account_name=account_name,
            holdings_path=holdings_path,
        )
        self.daily = self.market.daily
        self.index = self.daily.index
        self._aligned: dict[tuple[Any, ...], pd.Series] = {}

    @staticmethod
    def _key(operand: dict[str, Any], crossing_step: int) -> tuple[Any, ...]:
        if operand["kind"] == "value":
            return ("value", float(operand["value"]), crossing_step)
        return (
            operand["name"],
            operand["timeframe"],
            int(operand.get("offset", 0)),
            int(operand.get("period", 14)),
            float(operand.get("multiplier", 3.0)),
            int(operand.get("fast_period", 12)),
            int(operand.get("slow_period", 26)),
            int(operand.get("signal_period", 9)),
            int(operand.get("adx_smoothing", 14)),
            crossing_step,
        )

    def aligned(self, operand: dict[str, Any], crossing_step: int = 0) -> pd.Series:
        key = self._key(operand, crossing_step)
        if key in self._aligned:
            return self._aligned[key]
        if operand["kind"] == "value":
            result = pd.Series(float(operand["value"]), index=self.index, dtype=float)
        else:
            source = self.market.series(operand).shift(
                int(operand.get("offset", 0)) + crossing_step
            )
            if operand.get("name") != "Supertrend":
                source = source.mul(float(operand.get("multiplier", 1.0)))
            if operand["timeframe"] == "D":
                result = source.reindex(self.index)
            else:
                # Use deterministic period boundaries rather than a symbol's last
                # observed candle. The latter can leak a monthly/weekly value early
                # when that symbol was suspended or has missing data. A calendar
                # month-end on a weekend is moved to its business month-end.
                available_index = pd.DatetimeIndex(
                    [
                        NSE_CALENDAR.period_final_session(
                            stamp.date(), operand["timeframe"]
                        )
                        for stamp in source.index
                    ]
                )
                source = source.copy()
                source.index = pd.DatetimeIndex(available_index)
                source = source[~source.index.duplicated(keep="last")]
                combined = source.index.union(self.index)
                result = source.reindex(combined).sort_index().ffill().reindex(self.index)
        self._aligned[key] = result.astype(float)
        return self._aligned[key]


def evaluate_historical_ast(
    node: dict[str, Any],
    market: HistoricalMarketData,
    evidence: list[dict[str, Any]],
) -> pd.Series:
    if not node_enabled(node):
        return pd.Series(False, index=market.index, dtype=bool)
    if node["type"] == "group":
        outcomes = [
            evaluate_historical_ast(child, market, evidence)
            for child in node["children"]
            if has_active_rule(child)
        ]
        if not outcomes:
            return pd.Series(False, index=market.index, dtype=bool)
        op = str(node.get("op", "AND"))
        if op == "AND":
            result = outcomes[0].copy()
            for outcome in outcomes[1:]:
                result = result & outcome
            return result
        elif op == "OR":
            result = outcomes[0].copy()
            for outcome in outcomes[1:]:
                result = result | outcome
            return result
        elif op.startswith("ANY "):
            try:
                k = int(op.split()[1])
            except (IndexError, ValueError):
                k = 1
            count = sum(outcome.astype(int) for outcome in outcomes)
            return count >= k
        result = outcomes[0].copy()
        for outcome in outcomes[1:]:
            result = result & outcome
        return result

    operator = node["operator"]
    if is_high_range_operator(operator):
        left_operand = {**node["left"], "multiplier": 1.0}
        right_operand = {**node["right"], "multiplier": 1.0}
        left = market.aligned(left_operand)
        right = market.aligned(right_operand)
        previous_left = market.aligned(left_operand, 1)
        previous_right = market.aligned(right_operand, 1)
        valid = np.isfinite(left) & np.isfinite(right)
        above, below, _offset = range_rule_limits(node)
        lower_bound = right * (1.0 - below / 100.0)
        upper_bound = right * (1.0 + above / 100.0)
        matched = valid & (right > 0) & (left >= lower_bound) & (left <= upper_bound)
    else:
        left = market.aligned(node["left"])
        right = market.aligned(node["right"])
        previous_left = market.aligned(node["left"], 1)
        previous_right = market.aligned(node["right"], 1)
        valid = np.isfinite(left) & np.isfinite(right)
        if operator == ">":
            matched = valid & (left > right)
        elif operator == ">=":
            matched = valid & (left >= right)
        elif operator == "<":
            matched = valid & (left < right)
        elif operator == "<=":
            matched = valid & (left <= right)
        elif operator == "=":
            matched = valid & (left == right)
        elif operator == "!=":
            matched = valid & (left != right)
        else:
            previous_valid = np.isfinite(previous_left) & np.isfinite(previous_right)
            if operator == "crosses above":
                matched = valid & previous_valid & (left > right) & (previous_left <= previous_right)
            elif operator == "crosses below":
                matched = valid & previous_valid & (left < right) & (previous_left >= previous_right)
            else:
                raise ValueError(f"Unsupported operator: {operator}")
    evidence.append(
        {
            "rule": expression_text(node),
            "left": left,
            "right": right,
            "previous_left": previous_left,
            "previous_right": previous_right,
            "left_operand": node["left"],
            "right_operand": node["right"],
            "operator": operator,
            "matched": matched,
            "valid": valid,
        }
    )
    return matched.fillna(False)


def required_calendar_days(node: dict[str, Any]) -> int:
    operands: list[dict[str, Any]] = []

    def visit(current: dict[str, Any]) -> None:
        if not node_enabled(current):
            return
        if current["type"] == "group":
            for child in current["children"]:
                visit(child)
        else:
            operands.extend([current["left"], current["right"]])

    visit(node)
    required = 250
    factors = {"D": 2, "W": 9, "M": 35}
    for operand in operands:
        if operand.get("kind") != "field":
            continue
        bars = int(operand.get("offset", 0)) + 3
        if operand["name"] == "RSI":
            # RMA is recursive. A long warm-up makes the current value
            # independent of the scanner's download boundary, as on TradingView.
            bars += max(int(operand.get("period", 14)) * 10, 250)
        elif operand["name"] == "ADX":
            di_period = int(operand.get("period", 14))
            smoothing = int(operand.get("adx_smoothing", 14))
            bars += max((di_period + smoothing) * 10, 250)
        elif operand["name"] == "OBV":
            # OBV is cumulative, so use a long common warm-up window.
            required = max(required, 365 * 5)
        elif operand["name"] in MACD_FIELDS:
            slow = int(operand.get("slow_period", 26))
            signal = int(operand.get("signal_period", 9))
            bars += max((slow + signal) * 10, 250)
        elif operand["name"] in {"SMA", "EMA", "Supertrend"}:
            bars += int(operand.get("period", 14)) * 3
        elif operand["name"] in HIGH_52_WEEK_FIELDS:
            _count, unit, lookback_bars = lookback_high_parameters(operand)
            if unit == ALL_AVAILABLE_HISTORY_UNIT:
                required = max(
                    required,
                    (date.today() - AVAILABLE_HISTORY_START_DATE).days + 366,
                )
            else:
                bars += lookback_bars
        required = max(required, bars * factors[operand["timeframe"]])
    return min(required, 365 * 30)


@dataclass
class ScanReport:
    rows: list[dict[str, Any]]
    total_symbols: int
    scanned_symbols: int
    matched_symbols: int
    downloaded_symbols: int
    cache_hits: int
    missing_symbols: int
    download_failure_symbols: int
    insufficient_symbols: int
    universe_errors: list[str]


@dataclass
class HistoryReport:
    rows_by_date: dict[str, list[dict[str, Any]]]
    daily_summary: list[dict[str, Any]]
    total_symbols: int
    scanned_symbols: int
    total_matches: int
    dates_with_matches: int
    downloaded_symbols: int
    cache_hits: int
    missing_symbols: int
    download_failure_symbols: int
    insufficient_symbols: int
    universe_errors: list[str]
    start_date: str
    end_date: str


class ScanCancelled(Exception):
    pass


class RuleScannerWorker(QThread):
    """Cancellable scan service: immutable setup in, report out, no widget access."""

    progress = Signal(int, str)
    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        setup: dict[str, Any],
        symbols: list[str] | None = None,
        symbol_details: dict[str, dict[str, str]] | None = None,
        cache_path: Path = PRICE_CACHE_DB,
        downloader: Callable[[str, pd.Timestamp, pd.Timestamp, bool], pd.DataFrame] = download_symbol,
    ):
        super().__init__()
        self.setup = copy.deepcopy(setup)
        self.symbols = list(dict.fromkeys(symbols or []))
        self.symbol_details = copy.deepcopy(symbol_details or {})
        self.cache_path = cache_path
        self.downloader = downloader
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def _check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise ScanCancelled

    def run(self) -> None:
        try:
            report = self.scan(lambda percent, text: self.progress.emit(percent, text))
            self.succeeded.emit(report)
        except ScanCancelled:
            self.cancelled.emit()
        except Exception:
            self.failed.emit(traceback.format_exc())

    def scan(self, progress: Callable[[int, str], None] | None = None) -> ScanReport:
        validate_ast(self.setup["expression"])
        validate_ranking_setup(self.setup)
        self._paper_mode = bool(
            self.setup.get("algo_paper_mode", False)
            or self.setup.get("algo_auto_order_cb", False)
            or self.setup.get("algo_auto_order_enabled", False)
        )
        account_name = setup_portfolio_account(self.setup) or "ALL"
        if not self._paper_mode and (
            expression_has_pnl_rules(self.setup.get("expression"))
            or setup_has_portfolio_exit_triggers(self.setup)
        ) and account_name == "ALL":
            raise ValueError(
                "Portfolio P&L rules and exit triggers require exactly one identifiable Zerodha account."
            )
        self._paper_account = (
            setup_paper_account(self.setup) if self._paper_mode else None
        )
        if self._paper_mode and not self._paper_account:
            raise ValueError(
                "Simulation mode requires a Paper Account. Create or select one on the ALGO tab."
            )
        self._paper_store_data: dict[str, Any] | None = None
        self._paper_marks: dict[str, float] = {}
        if self._paper_mode and self._paper_account:
            try:
                self._paper_store_data = PaperTradesStore(
                    self.cache_path.parent / "paper_trades.db"
                ).load()
            except Exception:
                self._paper_store_data = None
        progress = progress or (lambda _percent, _text: None)
        universe_errors: list[str] = []
        if not self.symbols:
            progress(1, "Resolving selected universes...")
            self.symbols, universe_errors, self.symbol_details = resolve_universes(
                self.setup.get("universes", []),
                self.setup.get("custom_symbols", ""),
                self._check_cancelled,
            )
            self._check_cancelled()
        if not self.symbols:
            details = "\n".join(universe_errors) or "The selected setup resolved to no symbols."
            raise ValueError(details)
        as_of = pd.Timestamp(date.today() if self.setup["as_of_mode"] == "latest" else self.setup["as_of_date"])
        NSE_CALENDAR.require_coverage(as_of.date(), as_of.date())
        lookback = max(
            int(required_calendar_days(self.setup["expression"])),
            ranking_required_calendar_days(self.setup),
        )
        start = as_of - pd.DateOffset(days=lookback)
        adjusted = bool(self.setup["adjusted"])
        latest_request = self.setup.get("as_of_mode", "latest") == "latest"
        force_fresh = latest_request and bool(self.setup.get("force_fresh", False))
        cache = PriceCache(self.cache_path)
        try:
            refresh_minutes = int(
                self.setup.get("refresh_minutes", DEFAULT_REFRESH_MINUTES)
            )
        except (TypeError, ValueError):
            refresh_minutes = DEFAULT_REFRESH_MINUTES
        if refresh_minutes not in REFRESH_MINUTE_CHOICES:
            refresh_minutes = DEFAULT_REFRESH_MINUTES
        frames, cache_hits, downloaded, download_failures = self._prepare_frames(
            self.symbols, as_of, start, adjusted, force_fresh, refresh_minutes, progress
        )

        rows: list[dict[str, Any]] = []
        insufficient_symbols = 0
        timing_reference = pd.Timestamp.now(tz="Asia/Kolkata")
        symbols_with_data = [symbol for symbol in self.symbols if symbol in frames and not frames[symbol].empty]
        for index, symbol in enumerate(symbols_with_data, 1):
            self._check_cancelled()
            if self._paper_store_data is not None and self._paper_account:
                pos = (
                    self._paper_store_data.get("accounts", {})
                    .get(self._paper_account, {})
                    .get("positions", {})
                    .get(symbol)
                )
                if pos and float(pos.get("qty", 0)) > 0:
                    try:
                        close_p = float(frames[symbol]["Close"].iloc[-1])
                        if close_p > 0:
                            self._paper_marks[symbol] = close_p
                    except Exception:
                        pass
            market = MarketData(
                frames[symbol],
                as_of,
                bool(self.setup["include_current"]),
                symbol=symbol,
                account_name=account_name,
                holdings_path=self.cache_path.parent / "holdings_details.json",
                paper_store_data=(
                    self._paper_store_data if self._paper_mode else None
                ),
                paper_account=(self._paper_account or ""),
            )
            evidence: list[dict[str, Any]] = []
            matched = evaluate_ast(self.setup["expression"], market, evidence)

            # Dedicated Target Profit & Stop Loss Exit Trigger Evaluation for SELL templates
            if not matched and "SELL" in str(self.setup.get("algo_trade_action", "")).upper():
                if self._paper_store_data is not None and self._paper_account:
                    holding = paper_lookup_holding(
                        self._paper_store_data, self._paper_account, symbol
                    )
                else:
                    holding = (
                        HoldingsStore(
                            self.cache_path.parent / "holdings_details.json"
                        ).get_holding(symbol, account_name)
                        if account_name != "ALL"
                        else None
                    )
                if holding and float(holding.get("qty", 0)) > 0 and float(holding.get("buy_price", 0)) > 0:
                    close_p = market.daily["Close"].iloc[-1] if not market.daily.empty else 0.0
                    buy_p = float(holding.get("buy_price", 0.0))
                    if buy_p > 0 and close_p > 0:
                        pnl_p = ((close_p - buy_p) / buy_p) * 100.0
                        tp_enabled = bool(self.setup.get("algo_target_profit_enabled", False))
                        tp_target = float(self.setup.get("algo_target_profit_pct", 10.0))
                        sl_enabled = bool(self.setup.get("algo_stop_loss_enabled", False))
                        sl_target = float(self.setup.get("algo_stop_loss_pct", 5.0))
                        if tp_enabled and pnl_p >= tp_target:
                            matched = True
                            evidence.append({
                                "rule": f"Target Profit Trigger (+{tp_target:.2f}% reached)",
                                "matched": True,
                                "left": pnl_p,
                                "right": tp_target,
                                "previous_left": math.nan,
                                "previous_right": math.nan,
                                "left_operand": field_operand("Profit %", "D"),
                                "right_operand": value_operand(tp_target),
                                "operator": ">=",
                            })
                        elif sl_enabled and pnl_p <= -abs(sl_target):
                            matched = True
                            evidence.append({
                                "rule": f"Stop Loss Trigger (-{sl_target:.2f}% hit)",
                                "matched": True,
                                "left": pnl_p,
                                "right": -abs(sl_target),
                                "previous_left": math.nan,
                                "previous_right": math.nan,
                                "left_operand": field_operand("Stop Loss %", "D"),
                                "right_operand": value_operand(-abs(sl_target)),
                                "operator": "<=",
                            })

            insufficient = any(
                not math.isfinite(item.get("left", 0))
                or not math.isfinite(item.get("right", 0))
                or (
                    str(item.get("rule", "")).find("crosses ") >= 0
                    and (
                        not math.isfinite(item.get("previous_left", 0))
                        or not math.isfinite(item.get("previous_right", 0))
                    )
                )
                for item in evidence
            )
            if insufficient:
                insufficient_symbols += 1
            if matched:
                last = market.daily.iloc[-1] if not market.daily.empty else pd.Series(dtype=float)
                changes = daily_percent_changes(market.daily)
                percent_change = changes.iloc[-1] if not changes.empty else np.nan
                provider_data_time = cache.timing(symbol, adjusted)[0] if latest_request else None
                result_date = market.daily.index[-1] if not market.daily.empty else None
                row = {
                    "Symbol": symbol,
                    "Universe": self.symbol_details.get(symbol, {}).get("Universe", ""),
                    "Sector": self.symbol_details.get(symbol, {}).get("Sector", ""),
                    "Industry": self.symbol_details.get(symbol, {}).get("Industry", ""),
                    "Market Cap": self.symbol_details.get(symbol, {}).get("Market Cap", ""),
                    "As Of": result_date.date().isoformat() if result_date is not None else "",
                    DATA_SOURCE_COLUMN: cache.candle_source(
                        symbol, result_date, adjusted
                    ),
                    PRICE_BASIS_COLUMN: price_basis_label(adjusted),
                    DATA_TIME_COLUMN: format_provider_data_time(provider_data_time),
                    DATA_AGE_COLUMN: format_data_age(provider_data_time, timing_reference),
                    "Open": finite_or_none(last.get("Open")),
                    "High": finite_or_none(last.get("High")),
                    "Low": finite_or_none(last.get("Low")),
                    "Close": finite_or_none(last.get("Close")),
                    RSI_D_COLUMN: finite_or_none(market.scalar(field_operand("RSI", "D", 0, 14))),
                    RSI_1DAY_AGO_COLUMN: finite_or_none(market.scalar(field_operand("RSI", "D", 1, 14))),
                    PERCENT_CHANGE_COLUMN: finite_or_none(percent_change),
                    "Volume": finite_or_none(last.get("Volume")),
                }
                if self._paper_store_data is not None and self._paper_account:
                    holding = paper_lookup_holding(
                        self._paper_store_data, self._paper_account, symbol
                    )
                else:
                    holding = (
                        HoldingsStore(
                            self.cache_path.parent / "holdings_details.json"
                        ).get_holding(symbol, account_name)
                        if account_name != "ALL"
                        else None
                    )
                if holding and holding.get("qty") and holding.get("buy_price"):
                    qty = holding["qty"]
                    buy_price = holding["buy_price"]
                    close = row.get("Close")
                    row[HOLDING_QTY_COLUMN] = finite_or_none(qty)
                    row[BUY_PRICE_COLUMN] = finite_or_none(buy_price)
                    if close is not None and buy_price > 0:
                        pnl_pct = ((close - buy_price) / buy_price) * 100.0
                        pnl_amt = (close - buy_price) * qty
                        row[PNL_PCT_COLUMN] = finite_or_none(pnl_pct)
                        row[PNL_AMT_COLUMN] = finite_or_none(pnl_amt)
                    else:
                        row[PNL_PCT_COLUMN] = None
                        row[PNL_AMT_COLUMN] = None
                else:
                    row[HOLDING_QTY_COLUMN] = None
                    row[BUY_PRICE_COLUMN] = None
                    row[PNL_PCT_COLUMN] = None
                    row[PNL_AMT_COLUMN] = None

                row["Evidence"] = " | ".join(
                    f"{item['rule']} [{format_number(item['left'])} vs {format_number(item['right'])}]"
                    for item in evidence
                    if item.get("matched", True)
                )
                for item in evidence:
                    for side in ("left", "right"):
                        operand = item[f"{side}_operand"]
                        if operand.get("kind") == "field":
                            row[operand_text(operand)] = finite_or_none(item[side])
                            if operand["name"] == "52 Week High Distance %":
                                offset = int(operand.get("offset", 0))
                                close_operand = field_operand("Close", "D", offset)
                                high_operand = matching_lookback_high_operand(operand)
                                close_label = "Close" if offset == 0 else operand_text(close_operand)
                                row[close_label] = finite_or_none(market.scalar(close_operand))
                                row[operand_text(high_operand)] = finite_or_none(
                                    market.scalar(high_operand)
                                )
                    if item["operator"] in {"crosses above", "crosses below"}:
                        for side in ("left", "right"):
                            operand = item[f"{side}_operand"]
                            if operand.get("kind") == "field":
                                row[f"Previous {operand_text(operand)}"] = finite_or_none(
                                    item[f"previous_{side}"]
                                )
                add_composite_rsi_ranking_values(
                    row,
                    self.setup,
                    lambda period: market.scalar(
                        field_operand("RSI", "D", 0, period)
                    ),
                )
                rows.append(row)
            progress(45 + int(index / max(1, len(symbols_with_data)) * 55), f"Evaluated {index}/{len(symbols_with_data)}")
        if self._paper_marks and self._paper_account:
            try:
                PaperTradesStore(
                    self.cache_path.parent / "paper_trades.db"
                ).mark_many(self._paper_account, self._paper_marks)
            except Exception:
                pass
        rows = rank_scan_rows(rows, self.setup)
        return ScanReport(
            rows=rows,
            total_symbols=len(self.symbols),
            scanned_symbols=len(symbols_with_data),
            matched_symbols=len(rows),
            downloaded_symbols=downloaded,
            cache_hits=cache_hits,
            missing_symbols=max(0, len(self.symbols) - len(symbols_with_data) - download_failures),
            download_failure_symbols=download_failures,
            insufficient_symbols=insufficient_symbols,
            universe_errors=universe_errors,
        )

    def _prepare_frames(
        self,
        symbols: list[str],
        as_of: pd.Timestamp,
        start: pd.Timestamp,
        adjusted: bool,
        force_fresh: bool,
        refresh_minutes: int,
        progress: Callable[[int, str], None],
    ) -> tuple[dict[str, pd.DataFrame], int, int, int]:
        """Load or download price frames for the given symbols (shared cache path).

        Returns ``(frames, cache_hits, downloaded, download_failures)``.
        """
        cache = PriceCache(self.cache_path)
        latest_request = self.setup.get("as_of_mode", "latest") == "latest"
        frames: dict[str, pd.DataFrame] = {}
        to_download: list[str] = []
        tickertape_candidates: dict[str, pd.DataFrame] = {}
        current_ist = get_ist_now()
        tickertape_requested = (
            get_session_price_mode() == TICKERTAPE_PRICE_MODE
            and latest_request
            and bool(self.setup.get("include_current", False))
            and as_of.date() == current_ist.date()
        )
        use_tickertape = bool(
            tickertape_requested
            and should_fetch_intraday_quotes(
                as_of.date(),
                current_ist,
                NSE_CALENDAR.is_session_date(current_ist.date()),
                NSE_CALENDAR.session_open_time(
                    current_ist.date(), NSE_MARKET_OPEN
                ),
            )
        )
        use_completed_official = bool(tickertape_requested and not use_tickertape)
        historical_end = as_of - pd.Timedelta(days=1)
        if tickertape_requested:
            required_completed_date = coverage_end_floor(historical_end).date()
            sync = ensure_recent_bhavcopy(
                required_completed_date,
                lambda message: progress(1, message),
            )
            if sync.downloaded:
                progress(
                    1,
                    "Official Bhavcopy catch-up complete: "
                    f"{sync.downloaded} session(s) added through "
                    f"{required_completed_date.isoformat()}",
                )
            if sync.error:
                progress(
                    1,
                    "Tickertape history preparation incomplete; safe Original "
                    f"fallback remains active ({sync.error})",
                )
        official_history_ready = bool(
            tickertape_requested
            and official_bhavcopy_history_covers(start, historical_end)
        )
        if use_completed_official and official_history_ready:
            progress(
                1,
                "No current NSE session is available yet; using completed "
                f"official prices through {coverage_end_floor(historical_end).date().isoformat()}",
            )

        def load_cached_symbol(
            symbol: str,
        ) -> tuple[str, pd.DataFrame, bool, bool]:
            cached = cache.load(symbol, start, as_of, adjusted)
            current_covered = bool(
                not cached.empty
                and (
                    (
                        not force_fresh
                        and cache.is_covered(
                            symbol,
                            start,
                            as_of,
                            adjusted,
                            latest_request=latest_request,
                            latest_cache_ttl_seconds=refresh_minutes * 60,
                        )
                    )
                    or (
                        use_completed_official
                        and official_history_ready
                        and not str(symbol).strip().upper().endswith(".BO")
                    )
                )
            )
            history_covered = bool(
                use_tickertape
                and not cached.empty
                and not str(symbol).strip().upper().endswith(".BO")
                and (
                    official_history_ready
                    or cache.is_covered(
                        symbol,
                        start,
                        historical_end,
                        adjusted,
                        latest_request=False,
                    )
                )
            )
            return symbol, cached, current_covered, history_covered

        cache_pool = ThreadPoolExecutor(max_workers=min(8, max(1, len(symbols))))
        cache_futures = {
            cache_pool.submit(load_cached_symbol, symbol): symbol for symbol in symbols
        }
        try:
            for future in as_completed(cache_futures):
                self._check_cancelled()
                symbol = cache_futures[future]
                try:
                    _, cached, current_covered, history_covered = future.result()
                except Exception:
                    cached = pd.DataFrame()
                    current_covered = False
                    history_covered = False
                if current_covered:
                    frames[symbol] = cached
                else:
                    to_download.append(symbol)
                    if history_covered:
                        tickertape_candidates[symbol] = cached
        finally:
            cancelled = self._cancel.is_set()
            if cancelled:
                for future in cache_futures:
                    future.cancel()
            cache_pool.shutdown(wait=True, cancel_futures=cancelled)
        cache_hits = len(frames)
        progress(
            2,
            f"Universe {len(symbols):,} | cache hits {cache_hits:,} | "
            f"download queue {len(to_download):,}",
        )

        downloaded = 0
        download_failures = 0
        if tickertape_candidates:
            self._check_cancelled()
            batch = fetch_intraday_quotes(
                tickertape_candidates.keys(),
                as_of.date(),
            )
            self._check_cancelled()
            if batch.quotes:
                cache.store_intraday_quotes(batch.quotes, start, as_of)
                for symbol, quote in batch.quotes.items():
                    frame = tickertape_candidates[symbol].copy()
                    attrs = dict(getattr(frame, "attrs", {}))
                    frame.loc[pd.Timestamp(quote.trade_date), PriceCache.COLUMNS] = [
                        quote.open,
                        quote.high,
                        quote.low,
                        quote.close,
                        quote.volume,
                    ]
                    source_by_date = dict(attrs.get("source_by_date", {}))
                    source_by_date[quote.trade_date.isoformat()] = TICKERTAPE_SOURCE
                    attrs["source_by_date"] = source_by_date
                    attrs["provider_data_time"] = quote.provider_data_time
                    frame = normalize_download(frame)
                    frame.attrs.update(attrs)
                    frames[symbol] = frame
                downloaded += len(batch.quotes)
                accepted = set(batch.quotes)
                to_download = [symbol for symbol in to_download if symbol not in accepted]
            progress(
                3,
                f"Tickertape batch {len(batch.quotes):,}/{len(tickertape_candidates):,} "
                f"accepted in {batch.elapsed_seconds:.2f}s | "
                f"original fallback {len(to_download):,}",
            )
        if to_download:
            pool = ThreadPoolExecutor(max_workers=min(8, len(to_download)))
            futures = {}
            try:
                futures = {pool.submit(self.downloader, symbol, start, as_of, adjusted): symbol for symbol in to_download}
                for index, future in enumerate(as_completed(futures), 1):
                    self._check_cancelled()
                    symbol = futures[future]
                    try:
                        frame = future.result()
                        if not frame.empty:
                            cache.store(symbol, frame, adjusted, start, as_of)
                            loaded = cache.load(symbol, start, as_of, adjusted)
                            if not loaded.empty:
                                frames[symbol] = loaded
                                downloaded += 1
                            else:
                                download_failures += 1
                        else:
                            cache.store(symbol, frame, adjusted, start, as_of, "empty")
                    except Exception:
                        # A single bad ticker/network response must not abort the universe.
                        download_failures += 1
                    progress(
                        max(3, int(index / len(to_download) * 45)),
                        f"Universe {len(symbols):,} | cache hits {cache_hits:,} | "
                        f"download progress {index:,}/{len(to_download):,}",
                    )
            finally:
                cancelled = self._cancel.is_set()
                if cancelled:
                    for future in futures:
                        future.cancel()
                # Running requests cannot be cancelled by Future.cancel(). Keep
                # this QThread alive until they finish so it never outlives work
                # that still owns its downloader/cache state.
                pool.shutdown(wait=True, cancel_futures=cancelled)
        return frames, cache_hits, downloaded, download_failures


class FinalsetScannerWorker(RuleScannerWorker):
    """Scans the paper universe with a FINAL RULE SET composed of member rule
    templates referenced by name. Every symbol is evaluated against each
    member's expression, and the per-member matches are combined with the
    finalset's AND / OR / ANY K operator."""

    def __init__(
        self,
        setup: dict[str, Any],
        templates: dict[str, dict[str, Any]] | None = None,
        symbols: list[str] | None = None,
        symbol_details: dict[str, dict[str, str]] | None = None,
        cache_path: Path = PRICE_CACHE_DB,
        downloader: Callable[[str, pd.Timestamp, pd.Timestamp, bool], pd.DataFrame] = download_symbol,
    ):
        super().__init__(
            setup,
            symbols=symbols,
            symbol_details=symbol_details,
            cache_path=cache_path,
            downloader=downloader,
        )
        self.templates = templates or {}
        self._members: list[tuple[str, dict[str, Any]]] = []

    def _resolve_members(self) -> list[tuple[str, dict[str, Any]]]:
        names = [
            str(m).strip()
            for m in self.setup.get("finalset_members", [])
            if str(m).strip()
        ]
        if not names:
            raise ValueError(
                "The FINAL RULE SET has no member rule templates selected."
            )
        validate_finalset_members(
            str(self.setup.get("template_name", "") or "FINAL RULE SET"),
            names,
            self.templates,
        )
        members: list[tuple[str, dict[str, Any]]] = []
        for name in names:
            if name not in self.templates:
                raise ValueError(
                    f"FINAL RULE SET references template '{name}', which no longer exists."
                )
            setup = copy.deepcopy(self.templates[name])
            validate_ast(setup.get("expression", self.setup["expression"]))
            members.append((name, setup))
        return members

    def scan(
        self, progress: Callable[[int, str], None] | None = None
    ) -> ScanReport:
        self._members = self._resolve_members()
        validate_ranking_setup(self.setup)
        self._paper_mode = bool(
            self.setup.get("algo_paper_mode", False)
            or self.setup.get("algo_auto_order_cb", False)
            or self.setup.get("algo_auto_order_enabled", False)
        )
        account_name = setup_portfolio_account(self.setup) or "ALL"
        self._paper_account = (
            setup_paper_account(self.setup) if self._paper_mode else None
        )
        if self._paper_mode and not self._paper_account:
            raise ValueError(
                "Simulation mode requires a Paper Account. Create or select one on the ALGO tab."
            )
        self._paper_store_data: dict[str, Any] | None = None
        self._paper_marks: dict[str, float] = {}
        if self._paper_mode and self._paper_account:
            try:
                self._paper_store_data = PaperTradesStore(
                    self.cache_path.parent / "paper_trades.db"
                ).load()
            except Exception:
                self._paper_store_data = None
        progress = progress or (lambda _percent, _text: None)
        universe_errors: list[str] = []
        if not self.symbols:
            progress(1, "Resolving paper universe...")
            self.symbols, universe_errors, self.symbol_details = resolve_universes(
                self.setup.get("universes", []),
                self.setup.get("custom_symbols", ""),
                self._check_cancelled,
            )
            self._check_cancelled()
        if not self.symbols:
            details = "\n".join(universe_errors) or (
                "The FINAL RULE SET universe resolved to no symbols."
            )
            raise ValueError(details)
        as_of = pd.Timestamp(
            date.today()
            if self.setup["as_of_mode"] == "latest"
            else self.setup["as_of_date"]
        )
        NSE_CALENDAR.require_coverage(as_of.date(), as_of.date())
        try:
            lookback = max(
                int(required_calendar_days(member.get("expression")))
                for _name, member in self._members
            )
        except (TypeError, ValueError):
            lookback = 250
        lookback = max(lookback, ranking_required_calendar_days(self.setup))
        start = as_of - pd.DateOffset(days=lookback)
        adjusted = bool(self.setup["adjusted"])
        force_fresh = (
            self.setup.get("as_of_mode", "latest") == "latest"
            and bool(self.setup.get("force_fresh", False))
        )
        try:
            refresh_minutes = int(
                self.setup.get("refresh_minutes", DEFAULT_REFRESH_MINUTES)
            )
        except (TypeError, ValueError):
            refresh_minutes = DEFAULT_REFRESH_MINUTES
        if refresh_minutes not in REFRESH_MINUTE_CHOICES:
            refresh_minutes = DEFAULT_REFRESH_MINUTES
        frames, cache_hits, downloaded, download_failures = self._prepare_frames(
            self.symbols, as_of, start, adjusted, force_fresh, refresh_minutes, progress
        )
        cache = PriceCache(self.cache_path)
        latest_request = self.setup.get("as_of_mode", "latest") == "latest"
        combine_op = str(self.setup.get("finalset_op", "AND") or "AND")
        try:
            combine_k = max(1, int(self.setup.get("finalset_k", 1) or 1))
        except (TypeError, ValueError):
            combine_k = 1

        rows: list[dict[str, Any]] = []
        insufficient_symbols = 0
        timing_reference = pd.Timestamp.now(tz="Asia/Kolkata")
        symbols_with_data = [
            symbol for symbol in self.symbols
            if symbol in frames and not frames[symbol].empty
        ]
        for index, symbol in enumerate(symbols_with_data, 1):
            self._check_cancelled()
            if self._paper_store_data is not None and self._paper_account:
                pos = (
                    self._paper_store_data.get("accounts", {})
                    .get(self._paper_account, {})
                    .get("positions", {})
                    .get(symbol)
                )
                if pos and float(pos.get("qty", 0)) > 0:
                    try:
                        close_p = float(frames[symbol]["Close"].iloc[-1])
                        if close_p > 0:
                            self._paper_marks[symbol] = close_p
                    except Exception:
                        pass
            market = MarketData(
                frames[symbol],
                as_of,
                bool(self.setup["include_current"]),
                symbol=symbol,
                account_name=account_name,
                holdings_path=self.cache_path.parent / "holdings_details.json",
                paper_store_data=(
                    self._paper_store_data if self._paper_mode else None
                ),
                paper_account=(self._paper_account or ""),
            )
            evidence: list[dict[str, Any]] = []
            matches: list[bool] = []
            for member_name, member_setup in self._members:
                ev: list[dict[str, Any]] = []
                try:
                    matched = evaluate_ast(
                        member_setup.get("expression"), market, ev
                    )
                except Exception:
                    matched = False
                    ev = []
                for item in ev:
                    item["member"] = member_name
                matches.append(matched)
                evidence.extend(ev)
            combined = finalset_combine(matches, combine_op, combine_k)
            insufficient = any(
                not math.isfinite(item.get("left", 0))
                or not math.isfinite(item.get("right", 0))
                or (
                    str(item.get("rule", "")).find("crosses ") >= 0
                    and (
                        not math.isfinite(item.get("previous_left", 0))
                        or not math.isfinite(item.get("previous_right", 0))
                    )
                )
                for item in evidence
            )
            if insufficient:
                insufficient_symbols += 1
            if combined:
                last = (
                    market.daily.iloc[-1]
                    if not market.daily.empty
                    else pd.Series(dtype=float)
                )
                changes = daily_percent_changes(market.daily)
                percent_change = (
                    changes.iloc[-1] if not changes.empty else np.nan
                )
                provider_data_time = (
                    cache.timing(symbol, adjusted)[0] if latest_request else None
                )
                result_date = market.daily.index[-1] if not market.daily.empty else None
                matched_members = [
                    member_name
                    for (member_name, _member_setup), ok in zip(
                        self._members, matches
                    )
                    if ok
                ]
                evidence_parts: list[str] = []
                for item in evidence:
                    if not item.get("matched", True):
                        continue
                    member_name = item.get("member", "")
                    tag = f"[{member_name}] " if member_name else ""
                    evidence_parts.append(
                        f"{tag}{item['rule']} "
                        f"[{format_number(item.get('left'))} vs "
                        f"{format_number(item.get('right'))}]"
                    )
                row = {
                    "Symbol": symbol,
                    "Universe": self.symbol_details.get(symbol, {}).get("Universe", ""),
                    "As Of": (
                        result_date.date().isoformat() if result_date is not None else ""
                    ),
                    DATA_SOURCE_COLUMN: cache.candle_source(
                        symbol, result_date, adjusted
                    ),
                    PRICE_BASIS_COLUMN: price_basis_label(adjusted),
                    DATA_TIME_COLUMN: format_provider_data_time(provider_data_time),
                    DATA_AGE_COLUMN: format_data_age(
                        provider_data_time, timing_reference
                    ),
                    "Close": finite_or_none(last.get("Close")),
                    "Percent Change": finite_or_none(percent_change),
                    "Matched Members": ", ".join(matched_members),
                    "Evidence": " | ".join(evidence_parts),
                }
                if self._paper_store_data is not None and self._paper_account:
                    holding = paper_lookup_holding(
                        self._paper_store_data, self._paper_account, symbol
                    )
                    if holding and holding.get("qty") and holding.get("buy_price"):
                        row[HOLDING_QTY_COLUMN] = finite_or_none(holding["qty"])
                        row[BUY_PRICE_COLUMN] = finite_or_none(holding["buy_price"])
                add_composite_rsi_ranking_values(
                    row,
                    self.setup,
                    lambda period: market.scalar(
                        field_operand("RSI", "D", 0, period)
                    ),
                )
                rows.append(row)
            progress(
                45 + int(index / max(1, len(symbols_with_data)) * 55),
                f"Finalset evaluated {index}/{len(symbols_with_data)}",
            )
        if self._paper_marks and self._paper_account:
            try:
                PaperTradesStore(
                    self.cache_path.parent / "paper_trades.db"
                ).mark_many(self._paper_account, self._paper_marks)
            except Exception:
                pass
        rows = rank_scan_rows(rows, self.setup)
        return ScanReport(
            rows=rows,
            total_symbols=len(self.symbols),
            scanned_symbols=len(symbols_with_data),
            matched_symbols=len(rows),
            downloaded_symbols=downloaded,
            cache_hits=cache_hits,
            missing_symbols=max(
                0, len(self.symbols) - len(symbols_with_data) - download_failures
            ),
            download_failure_symbols=download_failures,
            insufficient_symbols=insufficient_symbols,
            universe_errors=universe_errors,
        )


class HistoryScannerWorker(RuleScannerWorker):
    """Evaluate one immutable rule setup across two or four years of daily dates."""

    def scan(self, progress: Callable[[int, str], None] | None = None) -> HistoryReport:
        validate_ast(self.setup["expression"])
        validate_ranking_setup(self.setup)
        if expression_has_pnl_rules(
            self.setup.get("expression")
        ) or setup_has_portfolio_exit_triggers(self.setup):
            raise ValueError(
                "Portfolio P&L rules and exit triggers cannot be backtested without dated position history."
            )
        progress = progress or (lambda _percent, _text: None)
        years = int(self.setup.get("history_years", 2))
        if years not in (2, 4):
            raise ValueError("Backtest history must be 2 or 4 years.")

        universe_errors: list[str] = []
        if not self.symbols:
            progress(1, "Resolving selected universes...")
            self.symbols, universe_errors, self.symbol_details = resolve_universes(
                self.setup.get("universes", []),
                self.setup.get("custom_symbols", ""),
                self._check_cancelled,
            )
            self._check_cancelled()
        if not self.symbols:
            details = "\n".join(universe_errors) or "The selected setup resolved to no symbols."
            raise ValueError(details)

        as_of = pd.Timestamp(
            date.today()
            if self.setup["as_of_mode"] == "latest"
            else self.setup["as_of_date"]
        ).normalize()
        history_start = (as_of - pd.DateOffset(years=years)).normalize()
        NSE_CALENDAR.require_coverage(history_start.date(), as_of.date())
        lookback = max(
            int(required_calendar_days(self.setup["expression"])),
            ranking_required_calendar_days(self.setup),
        )
        download_start = history_start - pd.DateOffset(days=lookback)
        adjusted = bool(self.setup["adjusted"])
        cache = PriceCache(self.cache_path)
        frames: dict[str, pd.DataFrame] = {}
        to_download: list[str] = []
        for symbol in self.symbols:
            self._check_cancelled()
            cached = cache.load(symbol, download_start, as_of, adjusted)
            if (
                cache.is_covered(symbol, download_start, as_of, adjusted)
                and not cached.empty
            ):
                frames[symbol] = cached
            else:
                to_download.append(symbol)
        cache_hits = len(frames)
        progress(2, f"{cache_hits} cache hits; downloading {len(to_download)}")

        downloaded = 0
        download_failures = 0
        if to_download:
            pool = ThreadPoolExecutor(max_workers=min(8, len(to_download)))
            futures = {}
            try:
                futures = {
                    pool.submit(
                        self.downloader, symbol, download_start, as_of, adjusted
                    ): symbol
                    for symbol in to_download
                }
                for index, future in enumerate(as_completed(futures), 1):
                    self._check_cancelled()
                    symbol = futures[future]
                    try:
                        frame = future.result()
                        if not frame.empty:
                            cache.store(
                                symbol, frame, adjusted, download_start, as_of
                            )
                            loaded = cache.load(
                                symbol, download_start, as_of, adjusted
                            )
                            if not loaded.empty:
                                frames[symbol] = loaded
                                downloaded += 1
                            else:
                                download_failures += 1
                        else:
                            cache.store(
                                symbol,
                                frame,
                                adjusted,
                                download_start,
                                as_of,
                                "empty",
                            )
                    except Exception:
                        download_failures += 1
                    progress(
                        max(3, int(index / len(to_download) * 45)),
                        f"Downloaded {index}/{len(to_download)}",
                    )
            finally:
                cancelled = self._cancel.is_set()
                if cancelled:
                    for future in futures:
                        future.cancel()
                pool.shutdown(wait=True, cancel_futures=cancelled)

        rows_by_date: dict[str, list[dict[str, Any]]] = {}
        all_dates: set[str] = set()
        insufficient_symbols = 0
        symbols_with_data = [
            symbol
            for symbol in self.symbols
            if symbol in frames and not frames[symbol].empty
        ]
        for symbol_index, symbol in enumerate(symbols_with_data, 1):
            self._check_cancelled()
            market = HistoricalMarketData(frames[symbol], as_of, symbol=symbol)
            percent_changes = daily_percent_changes(market.daily)
            ranking_series = {
                period: market.aligned(field_operand("RSI", "D", 0, period))
                for period in normalize_ranking_rsi_periods(
                    self.setup.get("ranking_rsi_periods", [22, 44, 66])
                )
            } if bool(self.setup.get("ranking_enabled", False)) else {}
            eligible_index = market.index[
                (market.index >= history_start) & (market.index <= as_of)
            ]
            all_dates.update(stamp.date().isoformat() for stamp in eligible_index)
            leaf_evidence: list[dict[str, Any]] = []
            matched = evaluate_historical_ast(
                self.setup["expression"], market, leaf_evidence
            ).reindex(eligible_index, fill_value=False)
            if any(
                not item["valid"].reindex(eligible_index, fill_value=False).any()
                for item in leaf_evidence
            ):
                insufficient_symbols += 1

            matched_dates = matched.index[matched]
            for match_index, stamp in enumerate(matched_dates):
                if match_index % 100 == 0:
                    self._check_cancelled()
                last = market.daily.loc[stamp]
                if isinstance(last, pd.DataFrame):
                    last = last.iloc[-1]
                evidence_parts: list[str] = []
                row = {
                    "Symbol": symbol,
                    "Universe": self.symbol_details.get(symbol, {}).get("Universe", ""),
                    "Sector": self.symbol_details.get(symbol, {}).get("Sector", "") or "n/a",
                    "Industry": self.symbol_details.get(symbol, {}).get("Industry", ""),
                    "Market Cap": self.symbol_details.get(symbol, {}).get("Market Cap", ""),
                    "As Of": stamp.date().isoformat(),
                    DATA_SOURCE_COLUMN: cache.candle_source(symbol, stamp, adjusted),
                    PRICE_BASIS_COLUMN: price_basis_label(adjusted),
                    "Open": finite_or_none(last.get("Open")),
                    "High": finite_or_none(last.get("High")),
                    "Low": finite_or_none(last.get("Low")),
                    "Close": finite_or_none(last.get("Close")),
                    RSI_D_COLUMN: finite_or_none(
                        market.aligned(field_operand("RSI", "D", 0, 14)).get(
                            stamp, np.nan
                        )
                    ),
                    RSI_1DAY_AGO_COLUMN: finite_or_none(
                        market.aligned(field_operand("RSI", "D", 1, 14)).get(
                            stamp, np.nan
                        )
                    ),
                    PERCENT_CHANGE_COLUMN: finite_or_none(
                        percent_changes.get(stamp, np.nan)
                    ),
                    "Volume": finite_or_none(last.get("Volume")),
                }
                # Current holdings are not historical facts. Do not attach them to
                # past rows or imply that historical portfolio P&L was reconstructed.
                row[HOLDING_QTY_COLUMN] = None
                row[BUY_PRICE_COLUMN] = None
                row[PNL_PCT_COLUMN] = None
                row[PNL_AMT_COLUMN] = None
                add_composite_rsi_ranking_values(
                    row,
                    self.setup,
                    lambda period: ranking_series[period].get(stamp, np.nan),
                )
                for item in leaf_evidence:
                    rule_matched = (
                        bool(item["matched"].at[stamp])
                        if "matched" in item and hasattr(item["matched"], "at")
                        else True
                    )
                    if not rule_matched:
                        continue
                    left = float(item["left"].at[stamp])
                    right = float(item["right"].at[stamp])
                    previous_left = float(item["previous_left"].at[stamp])
                    previous_right = float(item["previous_right"].at[stamp])
                    evidence_parts.append(
                        f"{item['rule']} [{format_number(left)} vs {format_number(right)}]"
                    )
                    for side, value in (("left", left), ("right", right)):
                        operand = item[f"{side}_operand"]
                        if operand.get("kind") != "field":
                            continue
                        row[operand_text(operand)] = finite_or_none(value)
                        if operand["name"] == "52 Week High Distance %":
                            offset = int(operand.get("offset", 0))
                            close_operand = field_operand("Close", "D", offset)
                            high_operand = matching_lookback_high_operand(operand)
                            close_label = (
                                "Close"
                                if offset == 0
                                else operand_text(close_operand)
                            )
                            row[close_label] = finite_or_none(
                                market.aligned(close_operand).at[stamp]
                            )
                            row[operand_text(high_operand)] = finite_or_none(
                                market.aligned(high_operand).at[stamp]
                            )
                    if item["operator"] in {"crosses above", "crosses below"}:
                        for side, value in (
                            ("left", previous_left),
                            ("right", previous_right),
                        ):
                            operand = item[f"{side}_operand"]
                            if operand.get("kind") == "field":
                                row[f"Previous {operand_text(operand)}"] = (
                                    finite_or_none(value)
                                )
                row["Evidence"] = " | ".join(evidence_parts)
                rows_by_date.setdefault(row["As Of"], []).append(row)
            progress(
                45
                + int(
                    symbol_index / max(1, len(symbols_with_data)) * 55
                ),
                f"Backtested {symbol_index}/{len(symbols_with_data)} symbols",
            )

        for day, day_rows in list(rows_by_date.items()):
            rows_by_date[day] = rank_scan_rows(day_rows, self.setup)

        daily_summary: list[dict[str, Any]] = []
        for day in sorted(all_dates):
            sector_counts: dict[str, int] = {}
            for row in rows_by_date.get(day, []):
                sector = row.get("Sector") or "n/a"
                sector_counts[sector] = sector_counts.get(sector, 0) + 1
            daily_summary.append(
                {
                    "date": day,
                    "total": len(rows_by_date.get(day, [])),
                    "sectors": sector_counts,
                }
            )
        total_matches = sum(len(rows) for rows in rows_by_date.values())
        return HistoryReport(
            rows_by_date=rows_by_date,
            daily_summary=daily_summary,
            total_symbols=len(self.symbols),
            scanned_symbols=len(symbols_with_data),
            total_matches=total_matches,
            dates_with_matches=len(rows_by_date),
            downloaded_symbols=downloaded,
            cache_hits=cache_hits,
            missing_symbols=max(
                0,
                len(self.symbols)
                - len(symbols_with_data)
                - download_failures,
            ),
            download_failure_symbols=download_failures,
            insufficient_symbols=insufficient_symbols,
            universe_errors=universe_errors,
            start_date=history_start.date().isoformat(),
            end_date=as_of.date().isoformat(),
        )


def format_number(value: Any) -> str:
    try:
        number = float(value)
        if not math.isfinite(number):
            return "n/a"
        return f"{number:,.4f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "n/a"


def format_percent(value: Any) -> str:
    try:
        number = float(value)
        if not math.isfinite(number):
            return "n/a"
        return f"{number:,.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def _provider_timestamp(value: Any) -> pd.Timestamp | None:
    if not value:
        return None
    try:
        stamp = pd.Timestamp(value)
        if pd.isna(stamp):
            return None
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("Asia/Kolkata")
        else:
            stamp = stamp.tz_convert("Asia/Kolkata")
        return stamp
    except (TypeError, ValueError, OverflowError):
        return None


def format_provider_data_time(value: Any) -> str:
    stamp = _provider_timestamp(value)
    return stamp.strftime("%Y-%m-%d %H:%M:%S") if stamp is not None else "Unavailable"


def format_data_age(value: Any, reference: Any = None) -> str:
    stamp = _provider_timestamp(value)
    if stamp is None:
        return "Unavailable"
    now = pd.Timestamp(reference) if reference is not None else pd.Timestamp.now(tz="Asia/Kolkata")
    if now.tzinfo is None:
        now = now.tz_localize("Asia/Kolkata")
    else:
        now = now.tz_convert("Asia/Kolkata")
    seconds = max(0, int((now - stamp).total_seconds()))
    days, remainder = divmod(seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes = remainder // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return "< 1m"


def export_workbook(path: str, rows: list[dict[str, Any]], setup: dict[str, Any]) -> None:
    workbook = Workbook()
    results = workbook.active
    results.title = "Results"
    headers = selected_result_headers(setup)
    if "Sector" in headers:
        sec_idx = headers.index("Sector")
        headers.insert(sec_idx + 1, "Industry")
        headers.insert(sec_idx + 2, "Market Cap")
    results.append(headers)
    for serial, row in enumerate(rows, 1):
        results.append(
            [serial if header == SERIAL_COLUMN else row.get(header) for header in headers]
        )
    config = workbook.create_sheet("Configuration")
    config.append(["Setting", "Value"])
    for key in (
        "template_name",
        "universes",
        "custom_symbols",
        "as_of_mode",
        "as_of_date",
        "adjusted",
        "include_current",
        "auto_run",
        "telegram_alerts_enabled",
        "telegram_alert_mode",
        "result_columns",
        "result_column_order",
    ):
        value = setup.get(key)
        config.append([key, json.dumps(value) if isinstance(value, (dict, list)) else value])
    expression = workbook.create_sheet("Expression")
    expression.append(["Readable expression", expression_text(setup["expression"])])
    expression.append(["JSON AST", json.dumps(setup["expression"], indent=2)])
    for sheet in workbook.worksheets:
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E78")
            cell.alignment = Alignment(horizontal="center")
        for column in range(1, sheet.max_column + 1):
            max_length = max(len(str(sheet.cell(row, column).value or "")) for row in range(1, sheet.max_row + 1))
            sheet.column_dimensions[get_column_letter(column)].width = min(max(max_length + 2, 12), 80)
    results.auto_filter.ref = (
        f"A1:{get_column_letter(max(1, len(headers)))}{max(1, results.max_row)}"
    )
    results.freeze_panes = "A2"
    if "Evidence" in headers:
        evidence_column = get_column_letter(headers.index("Evidence") + 1)
        for cell in results[evidence_column]:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    if PERCENT_CHANGE_COLUMN in headers:
        change_column = get_column_letter(headers.index(PERCENT_CHANGE_COLUMN) + 1)
        for cell in results[change_column][1:]:
            cell.number_format = '0.00"%"'
    workbook.save(path)


def export_paper_trades_workbook(
    path: str, account: str, acc: dict[str, Any]
) -> None:
    """Export one paper-trading account to an .xlsx workbook (summary, positions, ledger)."""
    workbook = Workbook()
    summary = workbook.active
    summary.title = "Summary"
    positions = acc.get("positions", {}) or {}
    ledger = acc.get("ledger", []) or []
    unrealized = 0.0
    invested = 0.0
    for pos in positions.values():
        qty = float(pos.get("qty", 0))
        avg = float(pos.get("avg_price", 0.0))
        mark = float(pos.get("last_mark") or 0.0)
        invested += avg * qty
        if mark > 0:
            unrealized += (mark - avg) * qty
    realized = float(acc.get("realized_pnl", 0.0))
    summary.append(["Paper Trading Account", account])
    summary.append(["Starting Cash (₹)", float(acc.get("starting_cash", PAPER_STARTING_CASH))])
    summary.append(["Cash Available (₹)", float(acc.get("cash", 0.0))])
    summary.append(["Invested Value (₹)", invested])
    summary.append(["Realized P&L (₹)", realized])
    summary.append(["Unrealized P&L (₹)", unrealized])
    summary.append(["Total P&L (₹)", realized + unrealized])
    summary.append(["Fees Paid (₹)", float(acc.get("fees_paid", 0.0))])
    summary.append(["Open Positions", len(positions)])

    pos_sheet = workbook.create_sheet("Open Positions")
    pos_sheet.append(
        ["Symbol", "Qty", "Avg Buy Price (₹)", "Last Mark (₹)", "Current Value (₹)", "Unrealized P&L (₹)"]
    )
    for sym, pos in positions.items():
        qty = float(pos.get("qty", 0))
        avg = float(pos.get("avg_price", 0.0))
        mark = float(pos.get("last_mark") or 0.0)
        pos_sheet.append(
            [
                sym,
                qty,
                avg,
                mark if mark > 0 else None,
                (mark * qty) if mark > 0 else (avg * qty),
                (mark - avg) * qty if mark > 0 else None,
            ]
        )

    hist_sheet = workbook.create_sheet("Trade History")
    hist_sheet.append(
        [
            "ID",
            "Time (IST)",
            "Action",
            "Symbol",
            "Qty",
            "Fill Price (₹)",
            "Value (₹)",
            "Fees (₹)",
            "Realized P&L (₹)",
            "Holding Days",
            "Order Type",
            "Product",
            "Template",
            "Scan ID",
            "Matched Rule",
        ]
    )
    for entry in reversed(ledger):
        hist_sheet.append(
            [
                entry.get("id", ""),
                entry.get("ts_ist", ""),
                entry.get("action", ""),
                entry.get("symbol", ""),
                entry.get("qty", 0),
                entry.get("fill_price", 0.0),
                entry.get("value", 0.0),
                entry.get("fees", 0.0),
                entry.get("realized_pnl"),
                entry.get("holding_days"),
                entry.get("order_type", ""),
                entry.get("product", ""),
                entry.get("template", ""),
                entry.get("scan_id", ""),
                entry.get("matched_rule", ""),
            ]
        )

    for sheet in workbook.worksheets:
        sheet.freeze_panes = "A2"
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E78")
            cell.alignment = Alignment(horizontal="center")
        for column in range(1, sheet.max_column + 1):
            max_length = max(
                len(str(sheet.cell(row, column).value or ""))
                for row in range(1, sheet.max_row + 1)
            )
            sheet.column_dimensions[get_column_letter(column)].width = min(
                max(max_length + 2, 12), 80
            )
    workbook.save(path)


def export_history_workbook(
    path: str, report: HistoryReport, setup: dict[str, Any]
) -> None:
    workbook = Workbook()
    summary = workbook.active
    summary.title = "Daily Summary"
    sectors = sorted(
        {
            sector
            for item in report.daily_summary
            for sector in item.get("sectors", {})
        }
    )
    summary_headers = ["Date", "Matches", *sectors]
    summary.append(summary_headers)
    for item in report.daily_summary:
        summary.append(
            [
                item["date"],
                item["total"],
                *(item.get("sectors", {}).get(sector, 0) for sector in sectors),
            ]
        )

    matches = workbook.create_sheet("Matched Stocks")
    headers = selected_result_headers(setup)
    matches.append(headers)
    serial = 0
    for day in sorted(report.rows_by_date):
        for row in report.rows_by_date[day]:
            serial += 1
            matches.append(
                [serial if header == SERIAL_COLUMN else row.get(header) for header in headers]
            )

    config = workbook.create_sheet("Configuration")
    config.append(["Setting", "Value"])
    for key in (
        "template_name",
        "history_years",
        "universes",
        "custom_symbols",
        "as_of_mode",
        "as_of_date",
        "adjusted",
        "telegram_alerts_enabled",
        "telegram_alert_mode",
        "result_columns",
        "result_column_order",
    ):
        value = setup.get(key)
        config.append(
            [
                key,
                json.dumps(value) if isinstance(value, (dict, list)) else value,
            ]
        )
    config.append(
        [
            "Historical candle policy",
            "Completed daily snapshots with completed weekly/monthly candles",
        ]
    )
    expression = workbook.create_sheet("Expression")
    expression.append(
        ["Readable expression", expression_text(setup["expression"])]
    )
    expression.append(["JSON AST", json.dumps(setup["expression"], indent=2)])

    for sheet in workbook.worksheets:
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E78")
            cell.alignment = Alignment(horizontal="center")
        for column in range(1, sheet.max_column + 1):
            max_length = max(
                len(str(sheet.cell(row, column).value or ""))
                for row in range(1, sheet.max_row + 1)
            )
            sheet.column_dimensions[get_column_letter(column)].width = min(
                max(max_length + 2, 12), 80
            )
    if "Evidence" in headers:
        evidence_column = get_column_letter(headers.index("Evidence") + 1)
        for cell in matches[evidence_column]:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    if PERCENT_CHANGE_COLUMN in headers:
        change_column = get_column_letter(headers.index(PERCENT_CHANGE_COLUMN) + 1)
        for cell in matches[change_column][1:]:
            cell.number_format = '0.00"%"'
    workbook.save(path)


class OperandDialog(QDialog):
    def __init__(self, operand: dict[str, Any] | None = None, allow_value: bool = True, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Operand")
        source = copy.deepcopy(operand or field_operand())
        layout = QFormLayout(self)
        self.kind = QComboBox()
        self.kind.addItems(["field", "value"] if allow_value else ["field"])
        self.kind.setCurrentText(source.get("kind", "field"))
        self.field = QComboBox()
        for field_name in FIELDS:
            self.field.addItem(FIELD_DISPLAY_NAMES.get(field_name, field_name), field_name)
        source_field = str(source.get("name", "Close"))
        source_index = self.field.findData(source_field)
        self.field.setCurrentIndex(max(0, source_index))
        self.field.setToolTip(
            "High lookback uses the highest High from the selected number of prior "
            "completed daily candles. Its Distance % is the absolute percentage "
            "above or below that level."
        )
        self.timeframe = QComboBox()
        self.timeframe.addItems(TIMEFRAMES)
        self.timeframe.setCurrentText(source.get("timeframe", "D"))
        self.offset = QSpinBox()
        self.offset.setRange(0, 250)
        self.offset.setValue(int(source.get("offset", 0)))
        self.period = QSpinBox()
        self.period.setRange(1, 500)
        self.period.setValue(int(source.get("period", 14)))
        self.multiplier = QDoubleSpinBox()
        self.multiplier.setRange(0.1, 100.0)
        self.multiplier.setDecimals(2)
        self.multiplier.setSingleStep(0.1)
        self.multiplier.setValue(float(source.get("multiplier", 3) if source.get("name") == "Supertrend" else 3.0))
        self.scale_multiplier = QDoubleSpinBox()
        self.scale_multiplier.setRange(0.0001, 1000.0)
        self.scale_multiplier.setDecimals(4)
        self.scale_multiplier.setSingleStep(0.001)
        self.scale_multiplier.setValue(float(source.get("multiplier", 1.0) if source.get("name") != "Supertrend" else 1.0))
        self.fast_period = QSpinBox()
        self.fast_period.setRange(1, 500)
        self.fast_period.setValue(int(source.get("fast_period", 12)))
        self.slow_period = QSpinBox()
        self.slow_period.setRange(1, 500)
        self.slow_period.setValue(int(source.get("slow_period", 26)))
        self.signal_period = QSpinBox()
        self.signal_period.setRange(1, 500)
        self.signal_period.setValue(int(source.get("signal_period", 9)))
        self.adx_smoothing = QSpinBox()
        self.adx_smoothing.setRange(1, 500)
        self.adx_smoothing.setValue(int(source.get("adx_smoothing", 14)))
        default_count, default_unit, _default_bars = lookback_high_parameters(source)
        # Editing an old 52-week operand presents the equivalent 1 trading year
        # rather than converting it to 260 candles (52 * 5).
        if "lookback_count" not in source:
            default_count, default_unit = 1, "Years"
        self.lookback_count = QSpinBox()
        self.lookback_count.setRange(1, MAX_LOOKBACK_HIGH_BARS)
        self.lookback_count.setValue(default_count)
        self.lookback_unit = QComboBox()
        self.lookback_unit.addItems(LOOKBACK_HIGH_UNITS)
        self.lookback_unit.setCurrentText(default_unit)
        lookback_tip = (
            "Uses completed trading candles: Day = 1, Week = 5, Month = 21, "
            "Year = 252. 'All available history since 2014' uses a causal "
            "expanding high. The evaluated candle itself is excluded."
        )
        self.lookback_count.setToolTip(lookback_tip)
        self.lookback_unit.setToolTip(lookback_tip)
        self.value = QLineEdit(str(source.get("value", 0)))
        layout.addRow("Type", self.kind)
        layout.addRow("Field", self.field)
        layout.addRow("Timeframe", self.timeframe)
        layout.addRow("Offset", self.offset)
        layout.addRow("Period", self.period)
        layout.addRow("Supertrend multiplier", self.multiplier)
        layout.addRow("Scale multiplier (e.g. 1.001)", self.scale_multiplier)
        layout.addRow("MACD fast period", self.fast_period)
        layout.addRow("MACD slow period", self.slow_period)
        layout.addRow("MACD signal period", self.signal_period)
        layout.addRow("ADX smoothing period", self.adx_smoothing)
        layout.addRow("High lookback duration", self.lookback_count)
        layout.addRow("High lookback unit", self.lookback_unit)
        layout.addRow("Numeric value", self.value)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)
        self.kind.currentTextChanged.connect(self._refresh)
        self.field.currentTextChanged.connect(self._refresh)
        self.lookback_unit.currentTextChanged.connect(self._refresh)
        self._refresh()

    def _refresh(self) -> None:
        is_field = self.kind.currentText() == "field"
        field_name = self._field_name()
        is_52_week_high = field_name in HIGH_52_WEEK_FIELDS
        if is_52_week_high and self.timeframe.currentText() != "D":
            self.timeframe.setCurrentText("D")
        indicator = field_name in {"RSI", "SMA", "EMA", "ADX", "Supertrend"}
        is_macd = field_name in MACD_FIELDS
        is_adx = field_name == "ADX"
        self.field.setEnabled(is_field)
        self.timeframe.setEnabled(is_field and not is_52_week_high)
        self.offset.setEnabled(is_field)
        self.period.setEnabled(is_field and indicator)
        self.multiplier.setEnabled(is_field and field_name == "Supertrend")
        self.scale_multiplier.setEnabled(is_field and field_name != "Supertrend")
        for widget in (self.fast_period, self.slow_period, self.signal_period):
            widget.setEnabled(is_field and is_macd)
        self.adx_smoothing.setEnabled(is_field and is_adx)
        all_history = self.lookback_unit.currentText() == ALL_AVAILABLE_HISTORY_UNIT
        self.lookback_count.setEnabled(is_field and is_52_week_high and not all_history)
        self.lookback_unit.setEnabled(is_field and is_52_week_high)
        self.value.setEnabled(not is_field)

    def _accept(self) -> None:
        try:
            validate_operand(self.operand())
            self.accept()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid operand", str(exc))

    def _field_name(self) -> str:
        return str(self.field.currentData() or self.field.currentText())

    def operand(self) -> dict[str, Any]:
        if self.kind.currentText() == "value":
            return value_operand(float(self.value.text()))
        field_name = self._field_name()
        is_st = field_name == "Supertrend"
        m_val = self.multiplier.value() if is_st else self.scale_multiplier.value()
        return field_operand(
            field_name,
            self.timeframe.currentText(),
            self.offset.value(),
            self.period.value(),
            m_val,
            self.fast_period.value(),
            self.slow_period.value(),
            self.signal_period.value(),
            self.adx_smoothing.value(),
            self.lookback_count.value() if field_name in HIGH_52_WEEK_FIELDS else None,
            self.lookback_unit.currentText() if field_name in HIGH_52_WEEK_FIELDS else None,
        )


class RuleDialog(QDialog):
    def __init__(self, rule: dict[str, Any] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Rule")
        self.rule = copy.deepcopy(rule or rule_node())
        if self.rule.get("operator") == LEGACY_HIGH_52_WEEK_RANGE_OPERATOR:
            self.rule["operator"] = HIGH_52_WEEK_RANGE_OPERATOR
        if is_legacy_52_week_range_rule(self.rule):
            above, below, offset = range_rule_limits(self.rule)
            self.rule = high_52_week_range_rule(above, below, offset)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.left_label = QLabel(operand_text(self.rule["left"]))
        self.right_label = QLabel(operand_text(self.rule["right"]))
        self.left_button = QPushButton("Choose...")
        self.right_button = QPushButton("Choose...")
        self.left_button.clicked.connect(lambda: self._choose("left"))
        self.right_button.clicked.connect(lambda: self._choose("right"))
        left_row, right_row = QHBoxLayout(), QHBoxLayout()
        left_row.addWidget(self.left_label, 1)
        left_row.addWidget(self.left_button)
        right_row.addWidget(self.right_label, 1)
        right_row.addWidget(self.right_button)
        self.operator = QComboBox()
        self.operator.addItems(OPERATORS)
        self.operator.setCurrentText(self.rule["operator"])
        above, below, _offset = (
            range_rule_limits(self.rule)
            if is_52_week_range_rule(self.rule)
            else (2.0, 2.0, 0)
        )
        self.above_percent = QDoubleSpinBox()
        self.above_percent.setRange(0.0, 100.0)
        self.above_percent.setDecimals(2)
        self.above_percent.setSingleStep(0.5)
        self.above_percent.setSuffix(" %")
        self.above_percent.setValue(above)
        self.below_percent = QDoubleSpinBox()
        self.below_percent.setRange(0.0, 100.0)
        self.below_percent.setDecimals(2)
        self.below_percent.setSingleStep(0.5)
        self.below_percent.setSuffix(" %")
        self.below_percent.setValue(below)
        right_source = self.rule.get("right", {})
        lookback_count, lookback_unit, _lookback_bars = lookback_high_parameters(right_source)
        if "lookback_count" not in right_source:
            lookback_count, lookback_unit = 1, "Years"
        self.lookback_count = QSpinBox()
        self.lookback_count.setRange(1, MAX_LOOKBACK_HIGH_BARS)
        self.lookback_count.setValue(lookback_count)
        self.lookback_unit = QComboBox()
        self.lookback_unit.addItems(LOOKBACK_HIGH_UNITS)
        self.lookback_unit.setCurrentText(lookback_unit)
        lookback_tip = (
            "Uses completed trading candles: Day = 1, Week = 5, Month = 21, "
            "Year = 252. 'All available history since 2014' uses a causal "
            "expanding high. The evaluated candle itself is excluded."
        )
        self.lookback_count.setToolTip(lookback_tip)
        self.lookback_unit.setToolTip(lookback_tip)
        form.addRow("Left", left_row)
        form.addRow("Comparison", self.operator)
        form.addRow("Right", right_row)
        form.addRow("Maximum distance above prior high", self.above_percent)
        form.addRow("Maximum distance below prior high", self.below_percent)
        form.addRow("Prior-high lookback duration", self.lookback_count)
        form.addRow("Prior-high lookback unit", self.lookback_unit)
        self.available_history_note = QLabel(
            "Limitation: this uses locally available price history from 2014 "
            "onward. For older stocks, it may differ from the true all-time high."
        )
        self.available_history_note.setWordWrap(True)
        self.available_history_note.setStyleSheet(
            "color: #8a4f00; background: #fff4ce; padding: 6px;"
        )
        form.addRow(self.available_history_note)
        self.above_percent_label = form.labelForField(self.above_percent)
        self.below_percent_label = form.labelForField(self.below_percent)
        self.lookback_count_label = form.labelForField(self.lookback_count)
        self.lookback_unit_label = form.labelForField(self.lookback_unit)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.operator.currentTextChanged.connect(self._operator_changed)
        self.lookback_count.valueChanged.connect(self._lookback_changed)
        self.lookback_unit.currentTextChanged.connect(self._lookback_changed)
        self._operator_changed(self.operator.currentText())

    def _operator_changed(self, operator: str) -> None:
        is_range = operator == HIGH_52_WEEK_RANGE_OPERATOR
        if is_range:
            offset = (
                int(self.rule["left"].get("offset", 0))
                if self.rule.get("left", {}).get("kind") == "field"
                else 0
            )
            # The range itself supplies the percentage tolerances. Always
            # normalize Close to scale 1 when this comparison is selected.
            self.rule["left"] = field_operand("Close", "D", offset)
            self.left_label.setText(operand_text(self.rule["left"]))
        if is_range:
            self._lookback_changed()
        self.above_percent.setEnabled(is_range)
        self.below_percent.setEnabled(is_range)
        self.above_percent.setVisible(is_range)
        self.below_percent.setVisible(is_range)
        self.above_percent_label.setVisible(is_range)
        self.below_percent_label.setVisible(is_range)
        self.lookback_count.setVisible(is_range)
        self.lookback_unit.setVisible(is_range)
        self.lookback_count_label.setVisible(is_range)
        self.lookback_unit_label.setVisible(is_range)
        self.available_history_note.setVisible(
            is_range
            and self.lookback_unit.currentText() == ALL_AVAILABLE_HISTORY_UNIT
        )

    def _lookback_changed(self, *_args: Any) -> None:
        if self.operator.currentText() != HIGH_52_WEEK_RANGE_OPERATOR:
            return
        all_history = self.lookback_unit.currentText() == ALL_AVAILABLE_HISTORY_UNIT
        self.lookback_count.setEnabled(not all_history)
        self.lookback_count_label.setEnabled(not all_history)
        self.available_history_note.setVisible(all_history)
        offset = int(self.rule.get("left", {}).get("offset", 0))
        self.rule["right"] = field_operand(
            "52 Week High",
            "D",
            offset,
            lookback_count=self.lookback_count.value(),
            lookback_unit=self.lookback_unit.currentText(),
        )
        self.right_label.setText(operand_text(self.rule["right"]))

    def _accept(self) -> None:
        try:
            # A disabled rule is still editable; the full expression performs the
            # "at least one enabled rule" check immediately before a scan.
            validate_ast(self.result_rule(), allow_empty_groups=True)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid rule", str(exc))
            return
        self.accept()

    def _choose(self, side: str) -> None:
        dialog = OperandDialog(self.rule[side], allow_value=side == "right", parent=self)
        if dialog.exec():
            self.rule[side] = dialog.operand()
            getattr(self, side + "_label").setText(operand_text(self.rule[side]))
            if side == "right" and self.rule[side].get("name") in HIGH_52_WEEK_FIELDS:
                count, unit, _bars = lookback_high_parameters(self.rule[side])
                self.lookback_count.setValue(count)
                self.lookback_unit.setCurrentText(unit)

    def result_rule(self) -> dict[str, Any]:
        self.rule["operator"] = self.operator.currentText()
        if (
            self.rule["left"].get("kind") == "field"
            and self.rule["left"].get("name") == "Stop Loss %"
            and self.rule["right"].get("kind") == "value"
            and self.rule["operator"] in {"<", "<=", "="}
            and float(self.rule["right"].get("value", 0)) > 0
        ):
            self.rule["right"]["value"] = -float(self.rule["right"]["value"])
        if self.rule["operator"] == HIGH_52_WEEK_RANGE_OPERATOR:
            self.rule["above_percent"] = self.above_percent.value()
            self.rule["below_percent"] = self.below_percent.value()
            self.rule["left"] = field_operand(
                "Close", "D", int(self.rule["left"].get("offset", 0))
            )
            self.rule["right"] = field_operand(
                "52 Week High",
                "D",
                int(self.rule["left"].get("offset", 0)),
                lookback_count=self.lookback_count.value(),
                lookback_unit=self.lookback_unit.currentText(),
            )
        return copy.deepcopy(self.rule)


class NumericItem(QTableWidgetItem):
    def __lt__(self, other: QTableWidgetItem) -> bool:
        left = self.data(Qt.ItemDataRole.UserRole)
        right = other.data(Qt.ItemDataRole.UserRole)
        if left is not None and right is not None:
            return left < right
        return super().__lt__(other)


def eye_switch_icon(enabled: bool) -> QIcon:
    """Small dependency-free open/closed eye used as a tree switch."""
    pixmap = QPixmap(24, 16)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = QColor("#2e8b57" if enabled else "#8a8a8a")
    painter.setPen(QPen(color, 2))
    if enabled:
        painter.drawEllipse(2, 3, 20, 10)
        painter.setBrush(color)
        painter.drawEllipse(9, 5, 6, 6)
    else:
        painter.drawLine(3, 8, 21, 8)
        painter.drawLine(6, 8, 4, 12)
        painter.drawLine(12, 8, 12, 13)
        painter.drawLine(18, 8, 20, 12)
    painter.end()
    return QIcon(pixmap)


class FilterExamplesDialog(QDialog):
    def __init__(self, target: QLineEdit, parent: QWidget | None = None):
        super().__init__(parent)
        self.target = target
        self.snippets_store = CustomSnippetsStore()
        self.setWindowTitle("Filter Examples")
        self.resize(850, 650)
        layout = QVBoxLayout(self)
        self.examples = QPlainTextEdit()
        self.examples.setReadOnly(True)
        self.examples.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._refresh_examples()
        layout.addWidget(self.examples, 1)

        hint = QLabel(
            "Tip: click a sentence to use its whole line, or select exactly one sentence. "
            "The sentence is placed in the English rule box; click “Parse one rule” to add it."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        copy_button = QPushButton("Copy selected")
        use_button = QPushButton("Use selected example")
        delete_button = QPushButton("Delete snippet")
        delete_button.setToolTip("Delete selected custom snippet from My Saved Snippets")
        close_button = QPushButton("Close")
        copy_button.clicked.connect(self._copy_selected)
        use_button.clicked.connect(self._use_selected)
        delete_button.clicked.connect(self._delete_selected_snippet)
        close_button.clicked.connect(self.reject)
        buttons.addStretch(1)
        buttons.addWidget(copy_button)
        buttons.addWidget(use_button)
        buttons.addWidget(delete_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

    def _refresh_examples(self) -> None:
        custom_snippets = self.snippets_store.load_snippets()
        self.examples.setPlainText(filter_examples_text(custom_snippets))

    def _delete_selected_snippet(self) -> None:
        cursor = self.examples.textCursor()
        sentence = cursor.selectedText().replace("\u2029", "\n").strip()
        if not sentence:
            sentence = cursor.block().text().strip()
        snippets = self.snippets_store.load_snippets()
        if sentence in snippets:
            self.snippets_store.delete_snippet(sentence)
            self._refresh_examples()
            QMessageBox.information(
                self,
                "Snippet deleted",
                f'Removed "{sentence}" from My Saved Snippets.',
            )
        else:
            QMessageBox.information(
                self,
                "Select a saved snippet",
                "Select a custom snippet sentence under 'MY SAVED SNIPPETS' to delete.",
            )

    def _selected_sentence(self) -> str | None:
        cursor = self.examples.textCursor()
        sentence = cursor.selectedText().replace("\u2029", "\n").strip()
        if not sentence:
            sentence = cursor.block().text().strip()
        if "\n" in sentence:
            QMessageBox.information(
                self,
                "Select one example",
                "Select exactly one complete filter sentence.",
            )
            return None
        try:
            RuleParser.parse(sentence)
        except ValueError:
            QMessageBox.information(
                self,
                "Select a filter sentence",
                "Select or click one of the complete example sentences, not a heading or help note.",
            )
            return None
        return sentence

    def _copy_selected(self) -> None:
        sentence = self._selected_sentence()
        if sentence:
            QApplication.clipboard().setText(sentence)

    def _use_selected(self) -> None:
        sentence = self._selected_sentence()
        if sentence:
            self.target.setText(sentence)
            self.target.setFocus()
            self.accept()


class FilterLogicDialog(QDialog):
    def __init__(
        self, expression: dict[str, Any], parent: QWidget | None = None
    ):
        super().__init__(parent)
        self.setWindowTitle("Logic of Each Filter Explained")
        self.resize(900, 650)
        layout = QVBoxLayout(self)
        self.explanation = QPlainTextEdit()
        self.explanation.setReadOnly(True)
        self.explanation.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.explanation.setPlainText(filter_logic_explanation(expression))
        layout.addWidget(self.explanation, 1)

        hint = QLabel(
            "This explanation reflects the current structured rule tree. "
            "Select any text to copy it, or use Copy all."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QHBoxLayout()
        copy_button = QPushButton("Copy all")
        close_button = QPushButton("Close")
        copy_button.clicked.connect(
            lambda: QApplication.clipboard().setText(
                self.explanation.toPlainText()
            )
        )
        close_button.clicked.connect(self.accept)
        buttons.addStretch(1)
        buttons.addWidget(copy_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)


class TelegramSettingsDialog(QDialog):
    def __init__(
        self,
        store: TelegramSettingsStore,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.store = store
        self.test_worker: TelegramSendWorker | None = None
        self.setWindowTitle("Telegram Settings")
        self.resize(700, 340)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("BotFather token")
        form.addRow("Bot token", self.token)
        self.destination_enabled: dict[str, QCheckBox] = {}
        self.destination_chat_id: dict[str, QLineEdit] = {}
        for destination in TelegramSettingsStore.default_destinations():
            key = str(destination["key"])
            row = QHBoxLayout()
            enabled = QCheckBox("Enabled")
            chat_id = QLineEdit()
            chat_id.setPlaceholderText("Numeric group Chat ID, e.g. -1001234567890")
            row.addWidget(enabled)
            row.addWidget(chat_id, 1)
            self.destination_enabled[key] = enabled
            self.destination_chat_id[key] = chat_id
            form.addRow(str(destination["label"]), row)
        layout.addLayout(form)
        security_note = QLabel(
            "The bot token is stored in Windows Credential Manager, not in "
            "scanner_templates.json. Group Chat IDs and their enabled settings are "
            "stored in telegram_settings.json. Enable either group, both groups, or neither."
        )
        security_note.setWordWrap(True)
        security_note.setStyleSheet("color: #555; font-size: 11px")
        layout.addWidget(security_note)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.test_button = QPushButton("Send test message")
        self.test_button.clicked.connect(self._send_test)
        actions.addWidget(self.test_button)
        actions.addStretch(1)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        actions.addWidget(self.buttons)
        layout.addLayout(actions)
        load_errors: list[str] = []
        try:
            destinations = self.store.load_destinations()
            for destination in destinations:
                key = str(destination["key"])
                if key not in self.destination_chat_id:
                    continue
                self.destination_chat_id[key].setText(
                    str(destination.get("chat_id", ""))
                )
                self.destination_enabled[key].setChecked(
                    bool(destination.get("enabled", False))
                )
        except TelegramSettingsError as exc:
            load_errors.append(str(exc))
        try:
            token = self.store.load_token()
            self.token.setText(token)
            if not token:
                load_errors.append(
                    "No saved Telegram bot token was found in Windows Credential Manager."
                )
        except TelegramSettingsError as exc:
            load_errors.append(str(exc))
        if load_errors:
            self.status.setText("\n".join(load_errors))
            self.status.setStyleSheet("color: #9c0006")

    def _credentials(self) -> tuple[str, list[dict[str, Any]]]:
        token = validate_telegram_token(self.token.text())
        destinations: list[dict[str, Any]] = []
        for default in TelegramSettingsStore.default_destinations():
            key = str(default["key"])
            chat_id = self.destination_chat_id[key].text().strip()
            enabled = self.destination_enabled[key].isChecked()
            if chat_id:
                chat_id = validate_telegram_chat_id(chat_id)
            elif enabled:
                raise TelegramSettingsError(
                    f"Enter a Chat ID for enabled {default['label']}."
                )
            destinations.append(
                {
                    "key": key,
                    "label": default["label"],
                    "chat_id": chat_id,
                    "enabled": enabled,
                }
            )
        enabled_chat_ids = [
            item["chat_id"] for item in destinations if item["enabled"]
        ]
        if len(enabled_chat_ids) != len(set(enabled_chat_ids)):
            raise TelegramSettingsError(
                "Group 1 and Group 2 cannot use the same enabled Chat ID."
            )
        return token, destinations

    def _set_busy(self, busy: bool) -> None:
        self.test_button.setEnabled(not busy)
        for button in (
            self.buttons.button(QDialogButtonBox.StandardButton.Save),
            self.buttons.button(QDialogButtonBox.StandardButton.Cancel),
        ):
            if button is not None:
                button.setEnabled(not busy)

    def _send_test(self) -> None:
        try:
            token, destinations = self._credentials()
        except TelegramSettingsError as exc:
            QMessageBox.warning(self, "Invalid Telegram settings", str(exc))
            return
        enabled = [item for item in destinations if item["enabled"]]
        if not enabled:
            QMessageBox.information(
                self,
                "No Telegram groups enabled",
                "Enable Group 1, Group 2, or both before sending a test message.",
            )
            return
        self._set_busy(True)
        self.status.setText(f"Sending a test message to {len(enabled)} group(s)...")
        self.status.setStyleSheet("color: #445")
        test_message = (
            "ChartSage Scanner Alert\n"
            "Test message successful. Telegram alerts can be enabled per template."
        )
        self.test_worker = TelegramSendWorker(
            token,
            [
                TelegramDelivery(
                    str(item["key"]),
                    str(item["label"]),
                    str(item["chat_id"]),
                    [test_message],
                )
                for item in enabled
            ],
        )
        self.test_worker.succeeded.connect(self._test_succeeded)
        self.test_worker.failed.connect(self._test_failed)
        self.test_worker.finished.connect(self._test_finished)
        self.test_worker.start()

    def _test_succeeded(self, count: int) -> None:
        self.status.setText(f"Test message sent successfully ({count} delivery/deliveries).")
        self.status.setStyleSheet("color: #006100; font-weight: 600")

    def _test_failed(self, details: str) -> None:
        self.status.setText(f"Test failed: {details}")
        self.status.setStyleSheet("color: #9c0006")

    def _test_finished(self) -> None:
        self.test_worker = None
        self._set_busy(False)

    def _save(self) -> None:
        try:
            token, destinations = self._credentials()
            self.store.save(token, destinations)
        except TelegramKeyringSaveError as exc:
            msg = (
                f"{exc}\n\n"
                "Do you want to save the Chat ID destinations anyway?\n"
                "(Note: You will need to configure the bot token via the "
                "CHARTINK_TELEGRAM_BOT_TOKEN environment variable for alerts to work.)"
            )
            if QMessageBox.question(
                self,
                "Keyring Error",
                msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            ) == QMessageBox.StandardButton.Yes:
                try:
                    self.store.save(token, destinations, ignore_keyring_errors=True)
                except (TelegramSettingsError, OSError) as exc2:
                    QMessageBox.warning(self, "Telegram settings not saved", str(exc2))
                    return
            else:
                return
        except (TelegramSettingsError, OSError) as exc:
            QMessageBox.warning(self, "Telegram settings not saved", str(exc))
            return
        self.accept()

    def reject(self) -> None:
        if self.test_worker and self.test_worker.isRunning():
            QMessageBox.information(
                self,
                "Test still running",
                "Wait for the Telegram test request to finish before closing.",
            )
            return
        super().reject()


class ExportWatchlistsDialog(QDialog):
    """Dialog allowing user to pick specific custom watchlists (or select all) for export."""

    def __init__(
        self,
        watchlists: dict[str, list[str]],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.watchlists = watchlists
        self.setWindowTitle("Export Custom Watchlists")
        self.resize(500, 420)
        layout = QVBoxLayout(self)

        header = QLabel(
            f"Select which custom watchlists ({len(watchlists)} available) you would like to export:"
        )
        header.setWordWrap(True)
        header.setStyleSheet(
            "font-weight: bold; font-size: 13px; color: #17365d; margin-bottom: 5px;"
        )
        layout.addWidget(header)

        self.list_widget = QListWidget()
        for name, symbols in sorted(watchlists.items()):
            item = QListWidgetItem(f"{name} ({len(symbols)} stocks)")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.list_widget.addItem(item)

        layout.addWidget(self.list_widget, 1)

        sel_layout = QHBoxLayout()
        select_all_btn = QPushButton("Select All")
        deselect_all_btn = QPushButton("Deselect All")
        select_all_btn.clicked.connect(lambda: self._set_all(True))
        deselect_all_btn.clicked.connect(lambda: self._set_all(False))
        sel_layout.addWidget(select_all_btn)
        sel_layout.addWidget(deselect_all_btn)
        sel_layout.addStretch(1)
        layout.addLayout(sel_layout)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        export_btn = QPushButton("Export Selected")
        cancel_btn = QPushButton("Cancel")
        export_btn.setStyleSheet(
            "font-weight: bold; background: #27ae60; color: white; padding: 6px 16px;"
        )
        cancel_btn.clicked.connect(self.reject)
        export_btn.clicked.connect(self.accept)
        buttons.addWidget(cancel_btn)
        buttons.addWidget(export_btn)
        layout.addLayout(buttons)

    def _set_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.list_widget.count()):
            self.list_widget.item(i).setCheckState(state)

    def get_selected_watchlists(self) -> dict[str, list[str]]:
        result = {}
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                name = item.data(Qt.ItemDataRole.UserRole)
                result[name] = list(self.watchlists[name])
        return result


class ExpandedExpressionDialog(QDialog):
    """Standalone popped-out resizable expression editor window for viewing and editing large rule sets."""

    def __init__(self, main_window: Any):
        super().__init__(main_window)
        self.main_window = main_window
        self.setWindowTitle("ChartSage — Standalone Structured Expression Editor")
        self.resize(1150, 780)
        self.setMinimumSize(900, 600)

        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        title_lbl = QLabel("Expanded Structured Expression Editor")
        title_lbl.setStyleSheet("font-size: 18px; font-weight: 700; color: #17365d")
        sub_lbl = QLabel("Live two-way sync with Main Scanner Window")
        sub_lbl.setStyleSheet("color: #666; font-size: 12px; margin-left: 10px;")

        return_btn = QPushButton("Return to Main Window")
        return_btn.setStyleSheet("font-weight: 600; padding: 5px 12px;")
        return_btn.clicked.connect(self.accept)

        header.addWidget(title_lbl)
        header.addWidget(sub_lbl)
        header.addStretch(1)
        header.addWidget(return_btn)
        layout.addLayout(header)

        rules_box = QGroupBox("Structured expression tree")
        rules_layout = QVBoxLayout(rules_box)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Use", "Rule / group"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.main_window._show_tree_context_menu)
        self.tree.setStyleSheet("""
            QTreeView::branch:has-siblings:!adjoins-item {
                border-left: 1px dotted #a0a0a0;
            }
            QTreeView::branch:has-siblings:adjoins-item {
                border-left: 1px dotted #a0a0a0;
                border-bottom: 1px dotted #a0a0a0;
            }
            QTreeView::branch:!has-siblings:adjoins-item {
                border-left: 1px dotted #a0a0a0;
                border-bottom: 1px dotted #a0a0a0;
            }
            QTreeView::item {
                padding: 4px 3px;
                font-size: 13px;
            }
        """)
        self.tree.itemClicked.connect(self.main_window._tree_item_clicked)
        rules_layout.addWidget(self.tree, 1)

        buttons = QHBoxLayout()
        add_rule_btn = QPushButton("Add rule")
        add_rule_btn.clicked.connect(self.main_window._add_rule)
        buttons.addWidget(add_rule_btn)

        add_group_btn = QToolButton()
        add_group_btn.setText("Add group ▾")
        add_group_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        group_menu = QMenu(self)
        for op, label in [
            ("AND", "AND (Pass ALL)"),
            ("OR", "OR (Pass ANY 1)"),
            ("ANY 2", "ANY 2 (Pass at least 2)"),
            ("ANY 3", "ANY 3 (Pass at least 3)"),
            ("ANY 4", "ANY 4 (Pass at least 4)"),
            ("ANY 5", "ANY 5 (Pass at least 5)"),
        ]:
            act = group_menu.addAction(f"Add {label}")
            act.triggered.connect(lambda _checked, o=op: self.main_window._add_group(o))
        add_group_btn.setMenu(group_menu)
        buttons.addWidget(add_group_btn)

        for text, slot in (
            ("Edit", self.main_window._edit_node),
            ("Duplicate", self.main_window._duplicate_node),
            ("Delete", self.main_window._delete_node),
            ("Up", lambda: self.main_window._move_node(-1)),
            ("Down", lambda: self.main_window._move_node(1)),
            ("Into", self.main_window._move_into),
            ("Out", self.main_window._move_out),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        rules_layout.addLayout(buttons)

        parser_row = QHBoxLayout()
        self.english_rule = QLineEdit()
        self.english_rule.setPlaceholderText(
            "One rule: Close within 2% above or below the 52 week high"
        )
        copy_draft_button = QPushButton("Copy filter to draft")
        copy_draft_button.clicked.connect(self.main_window._copy_to_draft)
        save_example_button = QPushButton("Save to Examples")
        save_example_button.clicked.connect(self.main_window._save_rule_to_examples)
        examples_button = QPushButton("Filter Examples")
        examples_button.clicked.connect(self.main_window._show_filter_examples)
        explain_button = QPushButton("Explain Filter Logic")
        explain_button.clicked.connect(self.main_window._show_filter_logic)
        parse_button = QPushButton("Parse one rule")
        parse_button.clicked.connect(self.main_window._parse_rule)

        parser_row.addWidget(self.english_rule, 1)
        parser_row.addWidget(copy_draft_button)
        parser_row.addWidget(save_example_button)
        parser_row.addWidget(examples_button)
        parser_row.addWidget(explain_button)
        parser_row.addWidget(parse_button)
        rules_layout.addLayout(parser_row)

        self.expression_label = QLabel()
        self.expression_label.setWordWrap(True)
        self.expression_label.setStyleSheet("color: #444; font-family: monospace; font-size: 12px; margin-top: 4px;")
        rules_layout.addWidget(self.expression_label)

        layout.addWidget(rules_box, 1)

        self.sync_from_main()

    def sync_from_main(self, select_path: tuple[int, ...] | None = None) -> None:
        self.tree.clear()
        root = self.main_window._make_tree_item(self.main_window.setup["expression"], ())
        self.tree.addTopLevelItem(root)
        root.setExpanded(True)
        self.tree.expandAll()
        if select_path is not None:
            selected = self.main_window._tree_item_at(select_path, target_tree=self.tree)
            if selected is not None:
                self.tree.setCurrentItem(selected)
                self.tree.scrollToItem(selected)
        self.expression_label.setText(expression_text(self.main_window.setup["expression"]))


class WatchlistManagerDialog(QDialog):
    """Dialog to create, delete, and edit custom watchlists using NSE stock search."""

    def __init__(self, store: WatchlistStore, parent: QWidget | None = None):
        super().__init__(parent)
        self.store = store
        self.watchlists = self.store.load()
        self.nse_symbols: list[dict[str, str]] = []
        self.download_worker: NSESymbolsDownloadWorker | None = None

        self.setWindowTitle("Manage Custom Watchlists")
        self.resize(1000, 520)
        self.setMinimumSize(700, 400)
        self.setSizeGripEnabled(True)

        layout = QVBoxLayout(self)

        # A splitter lets users give either long watchlist names or the symbol
        # list as much horizontal room as needed.  Keep both sides available so
        # the divider cannot accidentally collapse a pane completely.
        self.watchlist_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.watchlist_splitter.setChildrenCollapsible(False)
        self.watchlist_splitter.setOpaqueResize(True)
        self.watchlist_splitter.setHandleWidth(9)
        self.watchlist_splitter.setToolTip(
            "Drag the vertical divider left or right to resize both panes."
        )
        self.watchlist_splitter.setStyleSheet(
            "QSplitter::handle:horizontal {"
            " background: #c4ced8; border-left: 1px solid #8796a5;"
            " border-right: 1px solid #8796a5; margin: 1px 2px; }"
            "QSplitter::handle:horizontal:hover, QSplitter::handle:horizontal:pressed {"
            " background: #2e75b6; border-color: #17365d; }"
        )

        left_box = QGroupBox("Custom Watchlists")
        left_box.setMinimumWidth(220)
        left_layout = QVBoxLayout(left_box)
        self.watchlist_list = QListWidget()
        self.watchlist_list.currentTextChanged.connect(self._watchlist_selected)
        left_layout.addWidget(self.watchlist_list)

        left_buttons = QHBoxLayout()
        self.new_btn = QPushButton("New")
        self.new_btn.clicked.connect(self._create_watchlist)
        self.rename_btn = QPushButton("Rename")
        self.rename_btn.clicked.connect(self._rename_watchlist)
        self.del_btn = QPushButton("Delete")
        self.del_btn.clicked.connect(self._delete_watchlist)
        left_buttons.addWidget(self.new_btn)
        left_buttons.addWidget(self.rename_btn)
        left_buttons.addWidget(self.del_btn)
        left_layout.addLayout(left_buttons)

        left_io_buttons = QHBoxLayout()
        self.export_wl_btn = QPushButton("Export...")
        self.export_wl_btn.setToolTip("Export selected watchlists to a shareable JSON file")
        self.export_wl_btn.clicked.connect(self._export_watchlists)
        self.import_wl_btn = QPushButton("Import...")
        self.import_wl_btn.setToolTip("Import custom watchlists from a shareable JSON file")
        self.import_wl_btn.clicked.connect(self._import_watchlists)
        left_io_buttons.addWidget(self.export_wl_btn)
        left_io_buttons.addWidget(self.import_wl_btn)
        left_layout.addLayout(left_io_buttons)

        self.watchlist_splitter.addWidget(left_box)

        self.right_box = QGroupBox("Symbols in Watchlist")
        self.right_box.setMinimumWidth(260)
        right_layout = QVBoxLayout(self.right_box)

        self.symbol_list = QListWidget()
        right_layout.addWidget(self.symbol_list)

        add_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search stock by Symbol or Name...")
        self.search_input.returnPressed.connect(self._add_selected_symbol)

        self.completer = QCompleter(self)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.search_input.setCompleter(self.completer)

        self.add_btn = QPushButton("Add stock")
        self.add_btn.clicked.connect(self._add_selected_symbol)
        add_layout.addWidget(self.search_input, 1)
        add_layout.addWidget(self.add_btn)
        right_layout.addLayout(add_layout)

        sym_actions_layout = QHBoxLayout()
        self.del_sym_btn = QPushButton("Delete Selected Stock")
        self.del_sym_btn.clicked.connect(self._delete_symbol)
        self.import_zerodha_btn = QPushButton("Import from Zerodha...")
        self.import_zerodha_btn.clicked.connect(self._import_from_zerodha)
        sym_actions_layout.addWidget(self.del_sym_btn)
        sym_actions_layout.addWidget(self.import_zerodha_btn)
        right_layout.addLayout(sym_actions_layout)

        self.watchlist_splitter.addWidget(self.right_box)
        self.watchlist_splitter.setStretchFactor(0, 1)
        self.watchlist_splitter.setStretchFactor(1, 1)
        # Start close to an even split so long names are visible, while still
        # leaving the final proportion entirely under the user's control.
        self.watchlist_splitter.setSizes([470, 520])
        layout.addWidget(self.watchlist_splitter, 1)

        bottom_layout = QHBoxLayout()
        self.status_lbl = QLabel("")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet("color: #555; font-size: 11px")

        self.refresh_nse_btn = QPushButton("Refresh NSE symbols")
        self.refresh_nse_btn.clicked.connect(self._refresh_nse_symbols)
        self.refresh_nse_btn.setStyleSheet("max-width: 150px")

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_btn.setStyleSheet("max-width: 100px")

        bottom_layout.addWidget(self.status_lbl, 1)
        bottom_layout.addWidget(self.refresh_nse_btn)
        bottom_layout.addWidget(close_btn)
        layout.addLayout(bottom_layout)

        self._refresh_watchlist_list()
        self._load_cached_nse_symbols_or_download()

    def _get_selected_watchlist_name(self) -> str:
        current_item = self.watchlist_list.currentItem()
        if not current_item:
            return ""
        name = current_item.data(Qt.ItemDataRole.UserRole)
        if name and name in self.watchlists:
            return str(name)
        text = current_item.text()
        if text in self.watchlists:
            return text
        if " (" in text:
            raw = text.rsplit(" (", 1)[0]
            if raw in self.watchlists:
                return raw
        return text

    def _watchlist_selected(self, *_args: Any) -> None:
        self.symbol_list.clear()
        self.search_input.clear()
        watchlist_name = self._get_selected_watchlist_name()
        if not watchlist_name:
            self.right_box.setTitle("Symbols in Watchlist")
            self.right_box.setEnabled(False)
            return

        is_system_zerodha = watchlist_name.startswith("💼 ZERODHA") or watchlist_name.startswith("💼 Zerodha")
        if is_system_zerodha:
            self.rename_btn.setEnabled(False)
            self.rename_btn.setToolTip("System-managed Zerodha portfolio watchlist cannot be renamed.")
            self.del_btn.setText("Remove Account...")
            self.del_btn.setEnabled(True)
            self.del_btn.setToolTip(
                "Permanently remove this saved Zerodha account and its local portfolio list."
            )
            self.search_input.setEnabled(False)
            self.add_btn.setEnabled(False)
            self.del_sym_btn.setEnabled(False)
        else:
            self.rename_btn.setEnabled(True)
            self.rename_btn.setToolTip("")
            self.del_btn.setText("Delete")
            self.del_btn.setEnabled(True)
            self.del_btn.setToolTip("")
            self.search_input.setEnabled(True)
            self.add_btn.setEnabled(True)
            self.del_sym_btn.setEnabled(True)

        symbols = self.watchlists.get(watchlist_name, [])
        self.right_box.setTitle(f'Symbols in "{watchlist_name}" ({len(symbols)} stocks)')
        self.right_box.setEnabled(not is_system_zerodha or bool(symbols))

        for symbol in symbols:
            self.symbol_list.addItem(symbol)

    def _create_watchlist(self) -> None:
        name, ok = QInputDialog.getText(self, "New Watchlist", "Watchlist Name:")
        if not ok or not name.strip():
            return
        clean_name = name.strip()

        if clean_name in UNIVERSES and clean_name not in self.watchlists:
            QMessageBox.warning(self, "Invalid Name", f"'{clean_name}' is a reserved built-in universe name.")
            return
        if clean_name in self.watchlists:
            QMessageBox.warning(self, "Duplicate Name", f"A watchlist named '{clean_name}' already exists.")
            return
        if not re.fullmatch(r"[A-Za-z0-9\s_-]+", clean_name):
            QMessageBox.warning(self, "Invalid Name", "Watchlist name can only contain letters, numbers, spaces, hyphens, and underscores.")
            return

        self.watchlists[clean_name] = []
        self.store.save(self.watchlists)
        self._refresh_watchlist_list()
        # Find row for new watchlist and select it
        for row in range(self.watchlist_list.count()):
            if self.watchlist_list.item(row).text() == clean_name:
                self.watchlist_list.setCurrentRow(row)
                break

        if self.parent() and hasattr(self.parent(), "_watchlist_created"):
            self.parent()._watchlist_created(clean_name)

    def _rename_watchlist(self) -> None:
        old_name = self._get_selected_watchlist_name()
        if not old_name:
            return

        name, ok = QInputDialog.getText(
            self, "Rename Watchlist", f"New Name for '{old_name}':", text=old_name
        )
        if not ok or not name.strip() or name.strip() == old_name:
            return
        new_name = name.strip()

        if new_name in UNIVERSES and new_name not in self.watchlists:
            QMessageBox.warning(self, "Invalid Name", f"'{new_name}' is a reserved built-in universe name.")
            return
        if new_name in self.watchlists:
            QMessageBox.warning(self, "Duplicate Name", f"A watchlist named '{new_name}' already exists.")
            return
        if not re.fullmatch(r"[A-Za-z0-9\s_-]+", new_name):
            QMessageBox.warning(self, "Invalid Name", "Watchlist name can only contain letters, numbers, spaces, hyphens, and underscores.")
            return

        self.watchlists[new_name] = self.watchlists.pop(old_name)
        self.store.save(self.watchlists)
        self._refresh_watchlist_list()

        for row in range(self.watchlist_list.count()):
            item = self.watchlist_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == new_name:
                self.watchlist_list.setCurrentRow(row)
                break

        if self.parent() and hasattr(self.parent(), "_watchlist_renamed"):
            self.parent()._watchlist_renamed(old_name, new_name)

    def _delete_watchlist(self) -> None:
        name = self._get_selected_watchlist_name()
        if not name:
            return
        if is_holdings_universe(name):
            self._remove_zerodha_account(name)
            return
        if QMessageBox.question(
            self,
            "Delete Watchlist",
            f"Are you sure you want to delete the watchlist '{name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        self.watchlists.pop(name, None)
        self.store.save(self.watchlists)
        self._refresh_watchlist_list()

        if self.parent() and hasattr(self.parent(), "_watchlist_deleted"):
            self.parent()._watchlist_deleted(name)

    def _remove_zerodha_account(self, watchlist_name: str) -> None:
        user_id = zerodha_account_id_from_watchlist(watchlist_name)
        if not user_id:
            QMessageBox.warning(
                self,
                "Account ID unavailable",
                "This older portfolio-list name does not identify its Zerodha "
                "account. Open the Zerodha login manager to remove the account.",
            )
            return

        accounts = BrokerLoginStore().get_broker_accounts("Zerodha")
        account = next(
            (
                item
                for item in accounts
                if str(item.get("user_id", "")).strip().upper() == user_id
            ),
            None,
        )
        if account is None:
            QMessageBox.warning(
                self,
                "Saved account not found",
                f"No saved Zerodha account was found for {user_id}. The portfolio "
                "list was preserved so no unknown data is deleted.",
            )
            return

        label = str(account.get("label", "")).strip() or user_id
        answer = QMessageBox.warning(
            self,
            "Remove Zerodha Account",
            f"Permanently remove the saved Zerodha account '{label}' ({user_id})?\n\n"
            "This removes its saved login credentials, locally cached holdings, "
            "and generated ZERODHA portfolio watchlist. It does not place orders "
            "or change anything in the actual Zerodha account.\n\n"
            "Any scanner or ALGO template that refers to this account or watchlist "
            "will need to be updated manually.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            result = remove_saved_broker_account("Zerodha", user_id)
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Account Removal Failed",
                f"Could not completely remove the saved Zerodha account:\n{exc}",
            )
            self.watchlists = self.store.load()
            self._refresh_watchlist_list()
            return

        removed_names = list(result.get("watchlists_removed", []))
        self.watchlists = self.store.load()
        self._refresh_watchlist_list()
        for removed_name in removed_names:
            if self.parent() and hasattr(self.parent(), "_watchlist_deleted"):
                self.parent()._watchlist_deleted(removed_name)

        QMessageBox.information(
            self,
            "Zerodha Account Removed",
            f"Removed '{label}' ({user_id}).\n\n"
            f"Cached holdings removed: {int(result.get('holdings_removed', 0))}\n"
            f"Portfolio watchlists removed: {len(removed_names)}",
        )

    def _export_watchlists(self) -> None:
        if not self.watchlists:
            QMessageBox.information(
                self,
                "No watchlists",
                "Create a custom watchlist first before exporting.",
            )
            return
        dialog = ExportWatchlistsDialog(self.watchlists, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected_wl = dialog.get_selected_watchlists()
        if not selected_wl:
            return
        if len(selected_wl) == 1:
            raw_name = next(iter(selected_wl.keys()))
            default_filename = f"{raw_name.replace(' ', '_')}_Watchlist.json"
        else:
            default_filename = "exported_watchlists.json"

        path_str, _filter = QFileDialog.getSaveFileName(
            self,
            "Export Custom Watchlists",
            default_filename,
            "JSON Watchlist Files (*.json);;All Files (*.*)",
        )
        if not path_str:
            return
        export_path = Path(path_str)
        try:
            export_watchlists_json(selected_wl, export_path)
            QMessageBox.information(
                self,
                "Watchlists Exported",
                f"Successfully exported {len(selected_wl)} custom watchlist(s) to:\n\n{export_path}",
            )
        except Exception as exc:
            QMessageBox.critical(
                self, "Export failed", f"Could not export watchlists: {exc}"
            )

    def _import_watchlists(self) -> None:
        path_str, _filter = QFileDialog.getOpenFileName(
            self,
            "Import Custom Watchlists",
            "",
            "JSON Watchlist Files (*.json);;All Files (*.*)",
        )
        if not path_str:
            return
        import_path = Path(path_str)
        try:
            imported_wl = import_watchlists_json(import_path)
        except TemplateStoreError as exc:
            QMessageBox.warning(self, "Import failed", str(exc))
            return

        imported_count = 0
        first_name = None
        for raw_name, symbols in imported_wl.items():
            target_name = raw_name
            if target_name in self.watchlists:
                target_name = f"{raw_name} (Imported)"
                counter = 2
                while target_name in self.watchlists:
                    target_name = f"{raw_name} (Imported {counter})"
                    counter += 1

            self.watchlists[target_name] = symbols
            imported_count += 1
            if first_name is None:
                first_name = target_name

        self.store.save(self.watchlists)
        self._refresh_watchlist_list()

        if self.parent() and hasattr(self.parent(), "_watchlist_updated"):
            for name, symbols in self.watchlists.items():
                self.parent()._watchlist_updated(name, symbols)

        if first_name:
            items = self.watchlist_list.findItems(
                first_name, Qt.MatchFlag.MatchExactly
            )
            if items:
                self.watchlist_list.setCurrentItem(items[0])

        QMessageBox.information(
            self,
            "Watchlists Imported",
            f"Successfully imported {imported_count} custom watchlist(s)!\n\n"
            f'Loaded "{first_name}".',
        )

    def _add_selected_symbol(self) -> None:
        watchlist_name = self._get_selected_watchlist_name()
        if not watchlist_name:
            return
        text = self.search_input.text().strip()
        if not text:
            return

        symbol = text
        if " - " in text:
            symbol = text.split(" - ")[0].strip()
        symbol = symbol.upper()

        if not symbol:
            return

        watchlist_symbols = self.watchlists.get(watchlist_name, [])
        if symbol in watchlist_symbols:
            QMessageBox.information(self, "Symbol Exists", f"'{symbol}' is already in this watchlist.")
            self.search_input.clear()
            return

        watchlist_symbols.append(symbol)
        self.watchlists[watchlist_name] = watchlist_symbols
        self.store.save(self.watchlists)

        self.symbol_list.addItem(symbol)
        self.search_input.clear()

        count = len(watchlist_symbols)
        self.right_box.setTitle(f'Symbols in "{watchlist_name}" ({count} stocks)')
        current_item = self.watchlist_list.currentItem()
        if current_item:
            current_item.setText(f"{watchlist_name} ({count})")

        if self.parent() and hasattr(self.parent(), "_watchlist_updated"):
            self.parent()._watchlist_updated(watchlist_name, watchlist_symbols)

    def _delete_symbol(self) -> None:
        watchlist_name = self._get_selected_watchlist_name()
        if not watchlist_name:
            return

        current_symbol_item = self.symbol_list.currentItem()
        if not current_symbol_item:
            return
        symbol = current_symbol_item.text()

        watchlist_symbols = self.watchlists.get(watchlist_name, [])
        if symbol in watchlist_symbols:
            watchlist_symbols.remove(symbol)
            self.watchlists[watchlist_name] = watchlist_symbols
            self.store.save(self.watchlists)

            self.symbol_list.takeItem(self.symbol_list.row(current_symbol_item))

            count = len(watchlist_symbols)
            self.right_box.setTitle(f'Symbols in "{watchlist_name}" ({count} stocks)')
            current_item = self.watchlist_list.currentItem()
            if current_item:
                current_item.setText(f"{watchlist_name} ({count})")

            if self.parent() and hasattr(self.parent(), "_watchlist_updated"):
                self.parent()._watchlist_updated(watchlist_name, watchlist_symbols)

    def _import_from_zerodha(self) -> None:
        watchlist_name = self._get_selected_watchlist_name()
        if not watchlist_name:
            QMessageBox.information(
                self,
                "Select a Watchlist",
                "Please select or create a watchlist first before importing.",
            )
            return

        dialog = ZerodhaImportDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            fetched_symbols = dialog.imported_symbols
            if not fetched_symbols:
                QMessageBox.information(
                    self,
                    "Import Complete",
                    "No symbols retrieved from Zerodha holdings.",
                )
                return

            if not self.nse_symbols:
                QMessageBox.warning(
                    self,
                    "NSE Cache Empty",
                    "NSE Stock list is not loaded yet. Cannot filter symbols.",
                )
                return

            valid_nse_tickers = {item["symbol"].upper() for item in self.nse_symbols}

            imported_count = 0
            skipped_count = 0
            watchlist_symbols = self.watchlists.get(watchlist_name, [])
            existing_symbols = set(watchlist_symbols)

            for symbol in fetched_symbols:
                clean_sym = symbol.upper()
                if clean_sym in valid_nse_tickers:
                    if clean_sym not in existing_symbols:
                        watchlist_symbols.append(clean_sym)
                        existing_symbols.add(clean_sym)
                        imported_count += 1
                else:
                    skipped_count += 1

            if imported_count > 0:
                self.watchlists[watchlist_name] = watchlist_symbols
                self.store.save(self.watchlists)
                self._watchlist_selected(watchlist_name)

                current_item = self.watchlist_list.currentItem()
                if current_item:
                    current_item.setText(f"{watchlist_name} ({len(watchlist_symbols)})")

                if self.parent() and hasattr(self.parent(), "_watchlist_updated"):
                    self.parent()._watchlist_updated(watchlist_name, watchlist_symbols)

            QMessageBox.information(
                self,
                "Import Summary",
                f"Successfully imported {imported_count} stock(s).\n"
                f"Skipped {skipped_count} non-equity holdings/mutual funds.",
            )

    def _refresh_watchlist_list(self) -> None:
        self.watchlist_list.clear()
        for name in sorted(self.watchlists.keys()):
            count = len(self.watchlists[name])
            item = QListWidgetItem(f"{name} ({count})")
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.watchlist_list.addItem(item)

    def _load_cached_nse_symbols_or_download(self) -> None:
        if not NSE_SYMBOLS_CACHE_FILE.exists():
            self._download_nse_symbols()
            return
        try:
            payload = json.loads(NSE_SYMBOLS_CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("symbols"), list):
                self.nse_symbols = payload["symbols"]
                downloaded_at = payload.get("downloaded_at", "unknown date")
                self.status_lbl.setText(f"NSE symbols cache: loaded ({len(self.nse_symbols)} stocks, fetched {downloaded_at} IST)")
                self._populate_completer()
                return
        except Exception:
            pass
        self._download_nse_symbols()

    def _download_nse_symbols(self) -> None:
        if self.download_worker and self.download_worker.isRunning():
            return
        self.search_input.setEnabled(False)
        self.add_btn.setEnabled(False)
        self.refresh_nse_btn.setEnabled(False)
        self.status_lbl.setText("Downloading latest NSE symbol list from Exchange...")

        self.download_worker = NSESymbolsDownloadWorker()
        self.download_worker.succeeded.connect(self._download_succeeded)
        self.download_worker.failed.connect(self._download_failed)
        self.download_worker.finished.connect(self._download_finished)
        self.download_worker.start()

    def _download_succeeded(self, symbols: list) -> None:
        self.nse_symbols = symbols
        self.status_lbl.setText(f"NSE symbols cache: updated ({len(self.nse_symbols)} stocks fetched successfully)")
        self._populate_completer()

    def _download_failed(self, error: str) -> None:
        self.status_lbl.setText(f"NSE symbols fetch failed: {error}")
        QMessageBox.warning(self, "Fetch Failed", f"Could not download symbols from NSE Exchange:\n{error}\n\nAutocomplete searching will be unavailable until downloaded.")

    def _download_finished(self) -> None:
        self.download_worker = None
        self.search_input.setEnabled(True)
        self.add_btn.setEnabled(True)
        self.refresh_nse_btn.setEnabled(True)

    def _refresh_nse_symbols(self) -> None:
        self._download_nse_symbols()

    def _populate_completer(self) -> None:
        suggestions = []
        for item in self.nse_symbols:
            symbol = item.get("symbol", "").strip()
            name = item.get("name", "").strip()
            if symbol:
                if name:
                    suggestions.append(f"{symbol} - {name}")
                else:
                    suggestions.append(symbol)

        from PySide6.QtCore import QStringListModel
        model = QStringListModel(suggestions, self)
        self.completer.setModel(model)

    def reject(self) -> None:
        if self.download_worker and self.download_worker.isRunning():
            QMessageBox.information(
                self,
                "Download still running",
                "Wait for the NSE symbols download to finish before closing.",
            )
            return
        super().reject()


class EditBrokerLoginDialog(QDialog):
    """Dialog to add or edit a saved broker login account profile."""

    def __init__(self, account: dict[str, str] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Edit Saved Account" if account else "Add Saved Account")
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.label_input = QLineEdit()
        self.label_input.setPlaceholderText("e.g. Zerodha Main (AB1234)")

        self.userid_input = QLineEdit()
        self.userid_input.setPlaceholderText("e.g. AB1234")

        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setPlaceholderText("Your Zerodha password")

        self.totp_input = QLineEdit()
        self.totp_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.totp_input.setPlaceholderText("Optional 2FA TOTP secret key")

        form.addRow("Account Label:", self.label_input)
        form.addRow("User ID / Login:", self.userid_input)
        form.addRow("Password:", self.password_input)
        form.addRow("TOTP Secret (Optional):", self.totp_input)

        layout.addLayout(form)

        if account:
            self.label_input.setText(account.get("label", ""))
            self.userid_input.setText(account.get("user_id", ""))
            self.password_input.setText(account.get("password", ""))
            self.totp_input.setText(account.get("totp_secret", ""))

        buttons = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._save)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        buttons.addStretch(1)
        buttons.addWidget(save_btn)
        buttons.addWidget(cancel_btn)
        layout.addLayout(buttons)

        self.result_account: dict[str, str] | None = None

    def _save(self) -> None:
        user_id = self.userid_input.text().strip()
        label = self.label_input.text().strip() or (f"Account ({user_id})" if user_id else "Zerodha Account")
        if not user_id:
            QMessageBox.warning(self, "User ID Required", "Please enter your User ID / Login.")
            return
        self.result_account = {
            "label": label,
            "user_id": user_id,
            "password": self.password_input.text(),
            "totp_secret": self.totp_input.text().strip(),
        }
        self.accept()


class ManageBrokerLoginsDialog(QDialog):
    """Dialog to manage saved broker logins and passwords."""

    def __init__(self, broker: str = "Zerodha", parent: QWidget | None = None):
        super().__init__(parent)
        self.broker = broker
        self.store = BrokerLoginStore()
        self.accounts = self.store.get_broker_accounts(self.broker)

        self.setWindowTitle(f"Manage Saved Logins — {self.broker}")
        self.resize(500, 320)

        layout = QVBoxLayout(self)
        info = QLabel(f"Manage saved login credentials for <b>{self.broker}</b> accounts:")
        layout.addWidget(info)

        self.list_widget = QListWidget()
        self._refresh_list()
        layout.addWidget(self.list_widget, 1)

        btns = QHBoxLayout()
        add_btn = QPushButton("Add Account...")
        add_btn.clicked.connect(self._add_account)
        edit_btn = QPushButton("Edit Selected...")
        edit_btn.clicked.connect(self._edit_account)
        del_btn = QPushButton("Delete Selected")
        del_btn.clicked.connect(self._delete_account)
        close_btn = QPushButton("Done")
        close_btn.clicked.connect(self.accept)

        btns.addWidget(add_btn)
        btns.addWidget(edit_btn)
        btns.addWidget(del_btn)
        btns.addStretch(1)
        btns.addWidget(close_btn)
        layout.addLayout(btns)

    def _refresh_list(self) -> None:
        self.list_widget.clear()
        for acc in self.accounts:
            label = acc.get("label") or f"Account ({acc.get('user_id')})"
            has_totp = " (TOTP 2FA enabled)" if acc.get("totp_secret") else ""
            self.list_widget.addItem(f"👤 {label}{has_totp}")

    def _persist_accounts(self) -> bool:
        try:
            self.store.save_broker_accounts(self.broker, self.accounts)
            return True
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Credential Save Failed",
                "The account was NOT saved. Could not store its secrets in the "
                f"Windows credential store (keyring):\n{exc}\n\nThe account has "
                "been left unchanged and will not appear until the credentials "
                "save successfully.",
            )
            self.accounts = self.store.get_broker_accounts(self.broker)
            self._refresh_list()
            return False

    def _add_account(self) -> None:
        dialog = EditBrokerLoginDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result_account:
            self.accounts.append(dialog.result_account)
            if self._persist_accounts():
                self._refresh_list()

    def _edit_account(self) -> None:
        row = self.list_widget.currentRow()
        if row < 0 or row >= len(self.accounts):
            QMessageBox.warning(self, "Selection Required", "Please select an account to edit.")
            return
        dialog = EditBrokerLoginDialog(self.accounts[row], parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result_account:
            updated_account = dict(dialog.result_account)
            updated_account["enctoken"] = self.accounts[row].get("enctoken", "")
            updated_account["token_updated_at"] = self.accounts[row].get(
                "token_updated_at", ""
            )
            self.accounts[row] = updated_account
            if self._persist_accounts():
                self._refresh_list()

    def _delete_account(self) -> None:
        row = self.list_widget.currentRow()
        if row < 0 or row >= len(self.accounts):
            QMessageBox.warning(self, "Selection Required", "Please select an account to delete.")
            return
        account = self.accounts[row]
        acc_label = str(account.get("label", "")).strip() or "Selected Account"
        user_id = str(account.get("user_id", "")).strip().upper()
        answer = QMessageBox.warning(
            self,
            "Remove Broker Account",
            f"Permanently remove '{acc_label}' ({user_id})?\n\n"
            "This removes its saved credentials, locally cached holdings, and "
            "generated portfolio watchlist. It does not change the actual broker account.\n\n"
            "Templates referring to this account or watchlist must be updated manually.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            result = remove_saved_broker_account(self.broker, user_id)
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Account Removal Failed",
                f"Could not completely remove the saved account:\n{exc}",
            )
            self.accounts = self.store.get_broker_accounts(self.broker)
            self._refresh_list()
            return
        self.accounts = self.store.get_broker_accounts(self.broker)
        self._refresh_list()
        QMessageBox.information(
            self,
            "Broker Account Removed",
            f"Removed '{acc_label}' ({user_id}).\n\n"
            f"Cached holdings removed: {int(result.get('holdings_removed', 0))}\n"
            f"Portfolio watchlists removed: {len(result.get('watchlists_removed', []))}",
        )


class ZerodhaImportDialog(QDialog):
    """Dialog to retrieve holdings from Zerodha Kite via enctoken authentication."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.imported_symbols: list[str] = []
        self.worker: ZerodhaHoldingsWorker | None = None
        self.auto_worker: ZerodhaAutomatedLoginWorker | None = None
        self.login_store = BrokerLoginStore()

        self.setWindowTitle("Import Holdings from Zerodha Kite")
        self.resize(550, 360)

        layout = QVBoxLayout(self)

        instructions = QLabel(
            "<b>How to get your Zerodha enctoken (Free/No Cost):</b><br/>"
            "1. Log in to <a href='https://kite.zerodha.com'>kite.zerodha.com</a> in your web browser.<br/>"
            "2. Press <b>F12</b> (or Right-Click -> Inspect) and select the <b>Network</b> tab.<br/>"
            "3. Search for the request named <b>holdings</b> (or refresh your Kite page).<br/>"
            "4. Select the request, look at <b>Headers</b> -> <b>Request Headers</b>.<br/>"
            "5. Locate <b>Authorization: enctoken ...</b>, copy the long token string next to it.<br/>"
            "6. Paste that copied token string into the box below."
        )
        instructions.setWordWrap(True)
        instructions.setOpenExternalLinks(True)
        layout.addWidget(instructions)

        form_layout = QFormLayout()

        account_row = QHBoxLayout()
        self.account_combo = QComboBox()
        self._populate_accounts()
        self.manage_logins_btn = QPushButton("🔑 Manage Logins...")
        self.manage_logins_btn.clicked.connect(self._manage_logins)
        account_row.addWidget(self.account_combo, 1)
        account_row.addWidget(self.manage_logins_btn)
        form_layout.addRow("Saved Account:", account_row)

        self.token_input = QLineEdit()
        self.token_input.setPlaceholderText("Paste enctoken here (or click Automate below)...")
        form_layout.addRow("Enctoken:", self.token_input)
        layout.addLayout(form_layout)

        self.status_lbl = QLabel("")
        self.status_lbl.setWordWrap(True)
        layout.addWidget(self.status_lbl)

        buttons = QHBoxLayout()
        self.auto_login_btn = QPushButton("Automate Login & Fetch")
        self.auto_login_btn.clicked.connect(self._automate_login)

        self.fetch_btn = QPushButton("Fetch & Import")
        self.fetch_btn.clicked.connect(self._fetch_holdings)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)

        buttons.addWidget(self.auto_login_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.fetch_btn)
        buttons.addWidget(self.cancel_btn)
        layout.addLayout(buttons)

    def _populate_accounts(self) -> None:
        self.account_combo.clear()
        self.account_combo.addItem("🌐 (Manual Login / Interactive Browser)", None)
        accounts = self.login_store.get_broker_accounts("Zerodha")
        for acc in accounts:
            label = acc.get("label") or f"Account ({acc.get('user_id')})"
            self.account_combo.addItem(f"👤 {label}", acc)

    def _manage_logins(self) -> None:
        dialog = ManageBrokerLoginsDialog("Zerodha", parent=self)
        dialog.exec()
        self._populate_accounts()

    def _automate_login(self) -> None:
        selected_data = self.account_combo.currentData()
        user_id = selected_data.get("user_id", "") if isinstance(selected_data, dict) else ""
        password = selected_data.get("password", "") if isinstance(selected_data, dict) else ""
        totp_secret = selected_data.get("totp_secret", "") if isinstance(selected_data, dict) else ""

        self.auto_login_btn.setEnabled(False)
        self.fetch_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.token_input.setEnabled(False)

        msg = "Opening browser window... Please complete login/2FA in the browser window."
        if user_id:
            msg = f"Opening browser window & auto-filling credentials for '{user_id}'..."
        self.status_lbl.setText(msg)
        self.status_lbl.setStyleSheet("color: #004085")

        self.auto_worker = ZerodhaAutomatedLoginWorker(
            user_id=user_id,
            password=password,
            totp_secret=totp_secret,
        )
        self.auto_worker.succeeded.connect(self._auto_login_succeeded)
        self.auto_worker.failed.connect(self._auto_login_failed)
        self.auto_worker.finished.connect(self._auto_login_finished)
        self.auto_worker.start()

    def _auto_login_succeeded(self, token: str) -> None:
        self.token_input.setText(token)
        self.status_lbl.setText("Token captured successfully! Fetching holdings...")
        self.status_lbl.setStyleSheet("color: #006100")
        self._fetch_holdings()

    def _auto_login_failed(self, error: str) -> None:
        self.status_lbl.setText(f"Automated login failed: {error}")
        self.status_lbl.setStyleSheet("color: #9c0006")
        QMessageBox.warning(self, "Automated Login Failed", error)

    def _auto_login_finished(self) -> None:
        self.auto_worker = None
        self.auto_login_btn.setEnabled(True)
        self.fetch_btn.setEnabled(True)
        self.cancel_btn.setEnabled(True)
        self.token_input.setEnabled(True)

    def _fetch_holdings(self) -> None:
        token = self.token_input.text().strip()
        if not token:
            QMessageBox.warning(self, "Token Required", "Please paste your Zerodha enctoken.")
            return

        self.fetch_btn.setEnabled(False)
        self.auto_login_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.token_input.setEnabled(False)
        self.status_lbl.setText("Connecting to Zerodha Kite and fetching holdings...")
        self.status_lbl.setStyleSheet("color: #445")

        self.worker = ZerodhaHoldingsWorker(token)
        self.worker.succeeded.connect(self._fetch_succeeded)
        self.worker.failed.connect(self._fetch_failed)
        self.worker.finished.connect(self._fetch_finished)
        self.worker.start()

    def _fetch_succeeded(self, symbols: list) -> None:
        self.imported_symbols = symbols
        self.accept()

    def _fetch_failed(self, error: str) -> None:
        self.status_lbl.setText(f"Fetch failed: {error}")
        self.status_lbl.setStyleSheet("color: #9c0006")
        QMessageBox.warning(self, "Fetch Failed", f"Could not fetch holdings from Zerodha:\n{error}")

    def _fetch_finished(self) -> None:
        self.worker = None
        self.fetch_btn.setEnabled(True)
        self.auto_login_btn.setEnabled(True)
        self.cancel_btn.setEnabled(True)
        self.token_input.setEnabled(True)

    def reject(self) -> None:
        if (self.worker and self.worker.isRunning()) or (
            self.auto_worker and self.auto_worker.isRunning()
        ):
            QMessageBox.information(
                self,
                "Zerodha operation still running",
                "Wait for the login or holdings request to finish before closing.",
            )
            return
        super().reject()


class ZerodhaPortfolioDashboardDialog(QDialog):
    """Multi-account Zerodha Portfolio Management Dashboard."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("💼 Zerodha Multi-Account Portfolio Manager")
        self.resize(1020, 620)
        self.holdings_store = HoldingsStore()
        self.login_store = BrokerLoginStore()

        layout = QVBoxLayout(self)

        hdr_layout = QHBoxLayout()
        hdr_lbl = QLabel("<b>💼 Multi-Account Zerodha Portfolio</b>")
        hdr_lbl.setStyleSheet("font-size: 16px; color: #17365d;")
        hdr_layout.addWidget(hdr_lbl)
        hdr_layout.addStretch(1)

        self.account_combo = QComboBox()
        self._populate_accounts()
        self.account_combo.currentIndexChanged.connect(self._refresh_portfolio)
        hdr_layout.addWidget(QLabel("Select Account:"))
        hdr_layout.addWidget(self.account_combo)

        self.login_zerodha_btn = QPushButton("🔑 Login Zerodha")
        self.login_zerodha_btn.setStyleSheet("font-weight: 700; background-color: #1976d2; color: white; padding: 5px 12px; border-radius: 3px;")
        self.login_zerodha_btn.setToolTip("Log in to Zerodha or capture a fresh enctoken.")
        self.login_zerodha_btn.clicked.connect(self._open_zerodha_login)
        hdr_layout.addWidget(self.login_zerodha_btn)

        self.sync_btn = QPushButton("🔄 Sync Holdings from Zerodha")
        self.sync_btn.setStyleSheet("font-weight: 700; background: #2980b9; color: white; padding: 5px 12px; border-radius: 3px;")
        self.sync_btn.clicked.connect(self._sync_holdings)
        hdr_layout.addWidget(self.sync_btn)

        layout.addLayout(hdr_layout)

        cards_row = QHBoxLayout()
        cards_row.setSpacing(12)

        self.card_invested_val, self.card_invested_box = self._create_card("Total Investment", "₹ 0.00", "#34495e")
        self.card_value_val, self.card_value_box = self._create_card("Current Value", "₹ 0.00", "#2980b9")
        self.card_pnl_val, self.card_pnl_box = self._create_card("Overall P&L", "₹ 0.00 (0.00%)", "#27ae60")
        self.card_count_val, self.card_count_box = self._create_card("Total Holdings", "0 Stocks", "#8e44ad")

        cards_row.addWidget(self.card_invested_box, 1)
        cards_row.addWidget(self.card_value_box, 1)
        cards_row.addWidget(self.card_pnl_box, 1)
        cards_row.addWidget(self.card_count_box, 1)
        layout.addLayout(cards_row)

        self.table = QTableWidget()
        self.table.setColumnCount(9)
        self.table.setHorizontalHeaderLabels([
            "Symbol", "Quantity", "Avg Buy Price (₹)", "LTP / Current Price (₹)", "Current Value (₹)", "P&L (%)", "P&L Amount (₹)", "Account", "Action"
        ])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        layout.addWidget(self.table, 1)

        footer = QHBoxLayout()
        self.status_lbl = QLabel("Ready.")
        self.status_lbl.setStyleSheet("color: #555;")
        footer.addWidget(self.status_lbl, 1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        footer.addWidget(close_btn)
        layout.addLayout(footer)

        self._refresh_portfolio()

    def _create_card(self, title: str, default_val: str, color: str) -> tuple[QLabel, QFrame]:
        box = QFrame()
        box.setStyleSheet(f"background: #f8f9fa; border: 1px solid #dcdde1; border-top: 3px solid {color}; border-radius: 4px; padding: 8px;")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(6, 6, 6, 6)
        t_lbl = QLabel(title)
        t_lbl.setStyleSheet("font-size: 11px; color: #7f8c8d; font-weight: 600;")
        v_lbl = QLabel(default_val)
        v_lbl.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {color};")
        layout.addWidget(t_lbl)
        layout.addWidget(v_lbl)
        return v_lbl, box

    def _populate_accounts(self) -> None:
        self.account_combo.clear()
        self.account_combo.addItem("📊 All Accounts (Aggregated)", "ALL")
        accounts = self.login_store.get_broker_accounts("Zerodha")
        for acc in accounts:
            label = acc.get("label") or f"Account ({acc.get('user_id')})"
            user_id = acc.get("user_id") or label
            self.account_combo.addItem(f"👤 {label}", user_id)

    def _open_zerodha_login(self) -> None:
        dialog = ZerodhaImportDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.status_lbl.setText("Holdings synced successfully from Zerodha!")
            self.status_lbl.setStyleSheet("color: #27ae60; font-weight: 600;")
            self._populate_accounts()
            self._refresh_portfolio()
            parent_win = self.parent()
            if parent_win:
                if hasattr(parent_win, "_reload_system_watchlists"):
                    parent_win._reload_system_watchlists()
                if hasattr(parent_win, "_populate_algo_target_accounts"):
                    parent_win._populate_algo_target_accounts()
                if hasattr(parent_win, "_on_algo_target_account_changed"):
                    parent_win._on_algo_target_account_changed()

    def _manage_logins(self) -> None:
        dialog = ManageBrokerLoginsDialog("Zerodha", parent=self)
        dialog.exec()
        self._populate_accounts()

    def _sync_holdings(self) -> None:
        acc_id = str(self.account_combo.currentData() or "ALL").strip().upper()
        acc_text = str(self.account_combo.currentText() or "Main").replace("👤 ", "").strip()

        if acc_id == "ALL":
            accounts = self.login_store.get_broker_accounts("Zerodha")
            tokens_to_sync = []
            for acc in accounts:
                tok = str(acc.get("enctoken", "")).strip()
                uid = str(acc.get("user_id", "")).strip().upper()
                if tok and uid:
                    tokens_to_sync.append((uid, tok))
            
            if tokens_to_sync:
                self.sync_btn.setEnabled(False)
                self.sync_btn.setText("🔄 Batch Syncing...")
                self.status_lbl.setText(f"Syncing live holdings for {len(tokens_to_sync)} remembered Zerodha accounts...")
                self.status_lbl.setStyleSheet("color: #2980b9; font-weight: 600;")
                self._batch_sync_tokens = tokens_to_sync
                self._sync_next_batch_account()
                return

        target_acc = acc_id if acc_id != "ALL" else acc_text
        enctoken = self.login_store.get_token_for_user(acc_id)

        if enctoken:
            self.sync_btn.setEnabled(False)
            self.sync_btn.setText("🔄 Syncing...")
            self.status_lbl.setText(f"Connecting to Zerodha OMS and syncing holdings for {target_acc}...")
            self.status_lbl.setStyleSheet("color: #2980b9; font-weight: 600;")
            
            if not hasattr(self, "_active_batch_workers"):
                self._active_batch_workers = []
            worker = ZerodhaHoldingsWorker(enctoken, account_name=target_acc)
            self._active_batch_workers.append(worker)
            worker.succeeded.connect(self._on_silent_sync_success)
            worker.failed.connect(self._on_silent_sync_failed)
            worker.start()
        else:
            dialog = ZerodhaImportDialog(parent=self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.status_lbl.setText("Holdings synced successfully from Zerodha!")
                self.status_lbl.setStyleSheet("color: #27ae60;")
                self._populate_accounts()
                self._refresh_portfolio()

    def _sync_next_batch_account(self) -> None:
        if not hasattr(self, "_active_batch_workers"):
            self._active_batch_workers = []

        if not getattr(self, "_batch_sync_tokens", None):
            self.sync_btn.setEnabled(True)
            self.sync_btn.setText("🔄 Sync Holdings from Zerodha")
            self.status_lbl.setText("All remembered account holdings synced successfully!")
            self.status_lbl.setStyleSheet("color: #27ae60; font-weight: 600;")
            self._refresh_portfolio()
            parent_win = self.parent()
            if parent_win and hasattr(parent_win, "_reload_system_watchlists"):
                parent_win._reload_system_watchlists()
            return
        
        uid, tok = self._batch_sync_tokens.pop(0)
        self.status_lbl.setText(f"Syncing holdings for account {uid}...")
        
        worker = ZerodhaHoldingsWorker(tok, account_name=uid)
        self._active_batch_workers.append(worker)
        worker.finished.connect(lambda: QTimer.singleShot(50, self._sync_next_batch_account))
        worker.start()

    def _on_silent_sync_success(self, symbols: list[str]) -> None:
        self.sync_btn.setEnabled(True)
        self.sync_btn.setText("🔄 Sync Holdings from Zerodha")
        self.status_lbl.setText(f"Holdings synced successfully! ({len(symbols)} stocks updated)")
        self.status_lbl.setStyleSheet("color: #27ae60; font-weight: 600;")
        self._refresh_portfolio()
        parent_win = self.parent()
        if parent_win:
            if hasattr(parent_win, "_reload_system_watchlists"):
                parent_win._reload_system_watchlists()
            if hasattr(parent_win, "_populate_algo_target_accounts"):
                parent_win._populate_algo_target_accounts()
            if hasattr(parent_win, "_on_algo_target_account_changed"):
                parent_win._on_algo_target_account_changed()
            elif hasattr(parent_win, "_fetch_live_zerodha_funds"):
                parent_win._fetch_live_zerodha_funds()

    def _on_silent_sync_failed(self, err: str) -> None:
        self.sync_btn.setEnabled(True)
        self.sync_btn.setText("🔄 Sync Holdings from Zerodha")
        err_lower = err.lower()
        if "expired" in err_lower or "invalid" in err_lower or "403" in err_lower:
            self.status_lbl.setText("Session expired. Opening login window to refresh token...")
            self.status_lbl.setStyleSheet("color: #d35400; font-weight: 600;")
            dialog = ZerodhaImportDialog(parent=self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.status_lbl.setText("Holdings synced successfully from Zerodha!")
                self.status_lbl.setStyleSheet("color: #27ae60;")
                self._refresh_portfolio()
                parent_win = self.parent()
                if parent_win and hasattr(parent_win, "_reload_system_watchlists"):
                    parent_win._reload_system_watchlists()
        else:
            self.status_lbl.setText(f"Sync failed: {err}")
            self.status_lbl.setStyleSheet("color: #c0392b; font-weight: 600;")

    def _refresh_portfolio(self) -> None:
        self.table.setSortingEnabled(False)
        all_holdings = self.holdings_store.load_all()
        self.table.setRowCount(0)
        if not all_holdings:
            self.card_invested_val.setText("₹ 0.00")
            self.card_value_val.setText("₹ 0.00")
            self.card_pnl_val.setText("₹ 0.00 (0.00%)")
            self.card_count_val.setText("0 Stocks")
            self.table.setSortingEnabled(True)
            return

        cached_prices: dict[str, float] = {}
        try:
            if PRICE_CACHE_DB.exists():
                with sqlite3.connect(PRICE_CACHE_DB) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT symbol, close FROM daily_price_cache WHERE date = (SELECT MAX(date) FROM daily_price_cache)"
                    )
                    cached_prices = {str(r[0]).upper(): float(r[1]) for r in cursor.fetchall() if r[0] and r[1]}
        except Exception:
            pass

        selected_acc_data = str(self.account_combo.currentData() or "ALL").strip().upper()

        total_invested = 0.0
        total_curr_val = 0.0
        row_idx = 0

        for key_name, h_info in sorted(all_holdings.items()):
            symbol = str(h_info.get("symbol", key_name.split(":", 1)[-1])).strip().upper()
            acc_name = str(h_info.get("account", "Main")).strip().upper()
            if selected_acc_data != "ALL" and acc_name != selected_acc_data:
                continue

            qty = float(h_info.get("qty", 0))
            buy_price = float(h_info.get("buy_price", 0))
            last_price = float(h_info.get("last_price", 0))
            cached_price = cached_prices.get(symbol, 0.0)

            if qty <= 0:
                continue

            invested = qty * buy_price
            curr_price = last_price if last_price > 0 else (cached_price if cached_price > 0 else buy_price)
            curr_val = qty * curr_price
            pnl_amt = curr_val - invested
            pnl_pct = (pnl_amt / invested * 100.0) if invested > 0 else 0.0

            total_invested += invested
            total_curr_val += curr_val

            qty_str = f"{int(qty)}" if qty.is_integer() else f"{qty:,.2f}"
            self.table.insertRow(row_idx)
            self.table.setItem(row_idx, 0, QTableWidgetItem(symbol))
            
            qty_item = NumericItem(qty_str)
            qty_item.setData(Qt.ItemDataRole.UserRole, float(qty))
            self.table.setItem(row_idx, 1, qty_item)

            buy_item = NumericItem(f"{buy_price:,.2f}")
            buy_item.setData(Qt.ItemDataRole.UserRole, float(buy_price))
            self.table.setItem(row_idx, 2, buy_item)

            curr_price_item = NumericItem(f"{curr_price:,.2f}")
            curr_price_item.setData(Qt.ItemDataRole.UserRole, float(curr_price))
            self.table.setItem(row_idx, 3, curr_price_item)

            curr_val_item = NumericItem(f"{curr_val:,.2f}")
            curr_val_item.setData(Qt.ItemDataRole.UserRole, float(curr_val))
            self.table.setItem(row_idx, 4, curr_val_item)

            pnl_pct_item = NumericItem(f"{pnl_pct:+.2f}%")
            pnl_pct_item.setData(Qt.ItemDataRole.UserRole, float(pnl_pct))
            pnl_pct_item.setForeground(Qt.GlobalColor.darkGreen if pnl_pct >= 0 else Qt.GlobalColor.red)
            self.table.setItem(row_idx, 5, pnl_pct_item)

            pnl_amt_item = NumericItem(f"{pnl_amt:+,.2f}")
            pnl_amt_item.setData(Qt.ItemDataRole.UserRole, float(pnl_amt))
            pnl_amt_item.setForeground(Qt.GlobalColor.darkGreen if pnl_amt >= 0 else Qt.GlobalColor.red)
            self.table.setItem(row_idx, 6, pnl_amt_item)

            self.table.setItem(row_idx, 7, QTableWidgetItem(acc_name))
            row_token = self.login_store.get_token_for_user(acc_name)

            act_widget = QWidget()
            act_layout = QHBoxLayout(act_widget)
            act_layout.setContentsMargins(2, 2, 2, 2)
            act_layout.setSpacing(4)

            buy_btn = QPushButton("🟢 Buy More")
            buy_btn.setStyleSheet("font-weight: 700; padding: 2px 6px; background: #27ae60; color: white; border-radius: 3px; font-size: 11px;")
            buy_btn.clicked.connect(
                lambda _chk, s=symbol, p=curr_price, a=acc_name, t=row_token: PlaceOrderDialog(
                    s,
                    close_price=p,
                    action="BUY",
                    enctoken=t,
                    account_name=a,
                    parent=self,
                ).exec()
            )
            act_layout.addWidget(buy_btn)

            sell_btn = QPushButton("🔴 Exit / Sell")
            sell_btn.setStyleSheet("font-weight: 700; padding: 2px 6px; background: #c0392b; color: white; border-radius: 3px; font-size: 11px;")
            sell_btn.clicked.connect(
                lambda _chk, s=symbol, p=curr_price, a=acc_name, t=row_token: PlaceOrderDialog(
                    s,
                    close_price=p,
                    action="SELL",
                    enctoken=t,
                    account_name=a,
                    parent=self,
                ).exec()
            )
            act_layout.addWidget(sell_btn)

            self.table.setCellWidget(row_idx, 8, act_widget)

            row_idx += 1

        overall_pnl_amt = total_curr_val - total_invested
        overall_pnl_pct = (overall_pnl_amt / total_invested * 100.0) if total_invested > 0 else 0.0

        self.card_invested_val.setText(f"₹ {total_invested:,.2f}")
        self.card_value_val.setText(f"₹ {total_curr_val:,.2f}")
        pnl_str = f"₹ {overall_pnl_amt:+,.2f} ({overall_pnl_pct:+.2f}%)"
        self.card_pnl_val.setText(pnl_str)
        pnl_color = "#27ae60" if overall_pnl_amt >= 0 else "#c0392b"
        self.card_pnl_val.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {pnl_color};")
        self.card_count_val.setText(f"{row_idx} Stocks")
        self.table.resizeColumnsToContents()
        self.table.setSortingEnabled(True)

    def _sync_is_running(self) -> bool:
        workers = getattr(self, "_active_batch_workers", [])
        return bool(getattr(self, "_batch_sync_tokens", [])) or any(
            worker.isRunning() for worker in workers
        )

    def _close_if_idle(self, accept: bool) -> None:
        if self._sync_is_running():
            QMessageBox.information(
                self,
                "Holdings sync still running",
                "Wait for all account holdings requests to finish before closing.",
            )
            return
        if accept:
            super().accept()
        else:
            super().reject()

    def accept(self) -> None:
        self._close_if_idle(True)

    def reject(self) -> None:
        self._close_if_idle(False)


class _PaperAccountEditorDialog(QDialog):
    """Prompt for a paper account's name, starting cash and fee."""

    def __init__(
        self,
        parent: QWidget | None = None,
        display_name: str = "",
        starting_cash: float = PAPER_STARTING_CASH,
        fee_pct: float = PAPER_FEE_PCT,
        title: str = "New Paper Account",
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.name_edit = QLineEdit(str(display_name))
        self.name_edit.setPlaceholderText("e.g. Paper Test A")
        form.addRow("Account Name:", self.name_edit)

        self.cash_spin = QDoubleSpinBox()
        self.cash_spin.setRange(PAPER_MIN_STARTING_CASH, 1_000_000_000.0)
        self.cash_spin.setDecimals(0)
        self.cash_spin.setSingleStep(100_000)
        self.cash_spin.setGroupSeparatorShown(True)
        self.cash_spin.setValue(max(PAPER_MIN_STARTING_CASH, float(starting_cash)))
        form.addRow("Starting Cash (₹):", self.cash_spin)

        self.fee_spin = QDoubleSpinBox()
        self.fee_spin.setRange(0.0, 10.0)
        self.fee_spin.setDecimals(2)
        self.fee_spin.setSingleStep(0.05)
        self.fee_spin.setSuffix(" %")
        self.fee_spin.setValue(min(10.0, max(0.0, float(fee_pct))))
        form.addRow("Broker Fee (%):", self.fee_spin)
        layout.addLayout(form)

        btns = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("OK")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        btns.addStretch(1)
        btns.addWidget(cancel_btn)
        btns.addWidget(ok_btn)
        layout.addLayout(btns)

    def values(self) -> tuple[str, float, float]:
        return self.name_edit.text().strip(), self.cash_spin.value(), self.fee_spin.value()


class BuildFinalRuleSetDialog(QDialog):
    """Create a FINAL RULE SET template that combines rule templates by name.

    The member rule templates are referenced by name and re-evaluated live at
    scan time, so edits to a member are picked up automatically. Cyclic
    references are rejected.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        templates: dict[str, dict[str, Any]] | None = None,
        existing_name: str = "",
        existing_setup: dict[str, Any] | None = None,
        side: str = "BUY",
    ):
        super().__init__(parent)
        self.templates = templates or {}
        self.existing_name = str(existing_name or "").strip()
        self.existing_setup = existing_setup
        self.side = str(side or "BUY").strip().upper()
        if self.side not in ("BUY", "EXIT"):
            self.side = "BUY"
        self.setWindowTitle(
            "Build FINAL RULE SET" if not self.existing_name else "Edit FINAL RULE SET"
        )
        self.setMinimumWidth(520)
        self.setMinimumHeight(480)
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.side_combo = QComboBox()
        self.side_combo.addItem("BUY shortlist", "BUY")
        self.side_combo.addItem("EXIT (sell triggers)", "EXIT")
        form.addRow("Set type:", self.side_combo)
        if self.existing_name:
            side = str(
                (self.existing_setup or {}).get("finalset_side", "BUY") or "BUY"
            ).strip().upper()
            idx = self.side_combo.findData(side if side in ("BUY", "EXIT") else "BUY")
            self.side_combo.setCurrentIndex(idx if idx >= 0 else 0)
            self.side_combo.setEnabled(False)
        else:
            idx = self.side_combo.findData(self.side)
            self.side_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.name_edit = QLineEdit(self.existing_name)
        self.name_edit.setPlaceholderText("e.g. FINALRULESET1")
        form.addRow("FINAL RULE SET Name:", self.name_edit)

        op_row = QHBoxLayout()
        self.op_combo = QComboBox()
        self.op_combo.addItems(["AND (all members must match)", "OR (any member matches)", "ANY K (at least K members)"])
        self.k_spin = QSpinBox()
        self.k_spin.setRange(1, 99)
        self.k_spin.setValue(1)
        self.k_spin.setEnabled(False)
        self.op_combo.currentIndexChanged.connect(
            lambda idx: self.k_spin.setEnabled(idx == 2)
        )
        op_row.addWidget(self.op_combo, 1)
        op_row.addWidget(QLabel("K:"))
        op_row.addWidget(self.k_spin)
        form.addRow("Combine:", op_row)
        layout.addLayout(form)
        if self.existing_name and self.existing_setup:
            op = str(self.existing_setup.get("finalset_op", "AND") or "AND")
            op_idx = 0 if op == "AND" else (1 if op == "OR" else 2)
            self.op_combo.setCurrentIndex(op_idx)
            self.k_spin.setValue(int(self.existing_setup.get("finalset_k", 1) or 1))

        layout.addWidget(QLabel("Member rule templates (each one (re)builds its PAPER watchlist):"))
        self.member_list = QListWidget()
        self._populate_members()
        layout.addWidget(self.member_list, 1)

        if self.existing_name:
            add_row = QHBoxLayout()
            add_row.setSpacing(8)
            self.add_member_combo = QComboBox()
            self.add_member_combo.setMinimumWidth(260)
            self.add_member_combo.setToolTip("Choose a rule template to add to this FINAL RULE SET.")
            add_btn = QPushButton("Add template")
            add_btn.setToolTip("Add the chosen template to the FINAL RULE SET.")
            add_btn.clicked.connect(self._add_member)
            self.remove_member_btn = QPushButton("Remove Selected")
            self.remove_member_btn.setToolTip("Remove the highlighted member from the FINAL RULE SET.")
            self.remove_member_btn.clicked.connect(self._remove_selected_member)
            add_row.addWidget(self.add_member_combo, 1)
            add_row.addWidget(add_btn)
            add_row.addWidget(self.remove_member_btn)
            layout.addLayout(add_row)
            self._populate_add_combo()

        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: #7f6000; font-size: 11px;")
        layout.addWidget(self.hint)

        btns = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        self.save_btn = QPushButton("Save FINAL RULE SET 💾")
        self.save_btn.setDefault(True)
        self.save_btn.setStyleSheet(
            "font-weight: 700; background-color: #17365d; color: white; padding: 5px 12px; border-radius: 3px;"
        )
        self.save_btn.clicked.connect(self._save)
        btns.addStretch(1)
        btns.addWidget(cancel_btn)
        btns.addWidget(self.save_btn)
        layout.addLayout(btns)
        self._refresh_save_state()

    def _valid_member_names(self) -> list[str]:
        names: list[str] = []
        for name, setup in sorted(self.templates.items(), key=lambda kv: str(kv[0]).upper()):
            if is_final_template_setup(name, setup):
                continue
            if strategy_guide_template_role(name, setup) != self.side:
                continue
            if self.side == "EXIT" and expression_has_pnl_rules(setup.get("expression")):
                continue
            try:
                validate_ast(setup.get("expression", default_setup()["expression"]))
            except ValueError:
                continue
            names.append(name)
        return names

    def _current_member_names(self) -> list[str]:
        return [
            str(self.member_list.item(i).data(Qt.ItemDataRole.UserRole))
            for i in range(self.member_list.count())
        ]

    def _populate_members(self) -> None:
        valid = self._valid_member_names()
        self._valid_member_count = len(valid)
        self.member_list.clear()
        if self.existing_name:
            self.member_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            for raw in (self.existing_setup or {}).get("finalset_members", []):
                member = str(raw)
                item = QListWidgetItem(f"🔍 {member}")
                item.setData(Qt.ItemDataRole.UserRole, member)
                self.member_list.addItem(item)
        else:
            self.member_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
            for name in valid:
                item = QListWidgetItem(f"🔍 {name}")
                item.setData(Qt.ItemDataRole.UserRole, name)
                self.member_list.addItem(item)
        self._populate_add_combo()

    def _populate_add_combo(self) -> None:
        if not hasattr(self, "add_member_combo"):
            return
        members = set(self._current_member_names())
        self.add_member_combo.blockSignals(True)
        self.add_member_combo.clear()
        for name in self._valid_member_names():
            if name not in members:
                self.add_member_combo.addItem(name, name)
        self.add_member_combo.blockSignals(False)

    def _add_member(self) -> None:
        name = str(self.add_member_combo.currentData() or "").strip()
        if not name or name in set(self._current_member_names()):
            return
        item = QListWidgetItem(f"🔍 {name}")
        item.setData(Qt.ItemDataRole.UserRole, name)
        self.member_list.addItem(item)
        self._populate_add_combo()
        self._refresh_save_state()

    def _remove_selected_member(self) -> None:
        row = self.member_list.currentRow()
        if row < 0:
            return
        self.member_list.takeItem(row)
        self._populate_add_combo()
        self._refresh_save_state()

    def _refresh_save_state(self) -> None:
        valid = getattr(self, "_valid_member_count", 0)
        if valid == 0:
            self.hint.setText("No rule templates available yet — build and save a rule template first.")
        elif (
            self.existing_name
            and hasattr(self, "add_member_combo")
            and self.add_member_combo.count() == 0
            and self.member_list.count() > 0
        ):
            self.hint.setText("All available rule templates are already members of this set.")
        else:
            self.hint.setText("")
        self.save_btn.setEnabled(valid > 0)

    def _save(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Name required", "Give the FINAL RULE SET a name.")
            return
        if self.existing_name:
            selected = self._current_member_names()
        else:
            selected = [
                self.member_list.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self.member_list.count())
                if self.member_list.item(i).isSelected()
            ]
        selected = [str(s) for s in selected if s]
        if not selected:
            QMessageBox.warning(self, "No members", "Select at least one member rule template.")
            return
        if self.existing_name and name != self.existing_name and name in self.templates:
            QMessageBox.warning(self, "Name taken", f"A template named '{name}' already exists.")
            return
        try:
            validate_finalset_members(name, selected, self.templates)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid FINAL RULE SET", str(exc))
            return
        setup = default_setup()
        setup.update({
            "is_final_template": True,
            "finalset_op": "AND" if self.op_combo.currentIndex() == 0 else ("OR" if self.op_combo.currentIndex() == 1 else "ANY K"),
            "finalset_k": int(self.k_spin.value()),
            "finalset_members": selected,
            "finalset_side": str(self.side_combo.currentData() or "BUY"),
            "template_name": name,
        })
        store = TemplateStore()
        try:
            if self.existing_name and self.existing_name in self.templates:
                if name != self.existing_name:
                    store.rename(self.existing_name, name)
                store.save_edit(name, setup)
            else:
                store.create(name, setup)
        except (TemplateStoreError, OSError) as exc:
            QMessageBox.warning(self, "Could not save", str(exc))
            return
        self.accept()


class PaperAccountsDialog(QDialog):
    """Manage named virtual paper accounts and the defaults applied to new ones."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.store = PaperTradesStore()
        self._accounts: dict[str, dict[str, Any]] = {}
        self.setWindowTitle("📒 Paper Accounts")
        self.setMinimumWidth(740)
        self.resize(780, 480)

        layout = QVBoxLayout(self)

        defaults_box = QGroupBox("Defaults for new paper accounts")
        dform = QFormLayout(defaults_box)
        settings = {}
        try:
            settings = self.store.get_settings()
        except PaperTradesStoreError:
            pass
        self.default_cash_spin = QDoubleSpinBox()
        self.default_cash_spin.setRange(PAPER_MIN_STARTING_CASH, 1_000_000_000.0)
        self.default_cash_spin.setDecimals(0)
        self.default_cash_spin.setSingleStep(100_000)
        self.default_cash_spin.setGroupSeparatorShown(True)
        self.default_cash_spin.setValue(
            max(PAPER_MIN_STARTING_CASH, float(settings.get("starting_cash", PAPER_STARTING_CASH)))
        )
        dform.addRow("Starting Cash (₹):", self.default_cash_spin)
        self.default_fee_spin = QDoubleSpinBox()
        self.default_fee_spin.setRange(0.0, 10.0)
        self.default_fee_spin.setDecimals(2)
        self.default_fee_spin.setSingleStep(0.05)
        self.default_fee_spin.setSuffix(" %")
        self.default_fee_spin.setValue(
            min(10.0, max(0.0, float(settings.get("fee_pct", PAPER_FEE_PCT))))
        )
        dform.addRow("Broker Fee (%):", self.default_fee_spin)
        save_defaults_btn = QPushButton("💾 Save Defaults")
        save_defaults_btn.setToolTip(
            "Save the default cash and fee applied when creating NEW paper accounts."
        )
        save_defaults_btn.clicked.connect(self._save_defaults)
        dform.addRow("", save_defaults_btn)
        layout.addWidget(defaults_box)

        layout.addWidget(QLabel("<b>Paper Accounts</b>"))
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            [
                "Name",
                "Starting Cash (₹)",
                "Fee %",
                "Paper Cash (₹)",
                "Open Positions",
                "Realized P&L (₹)",
            ]
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)

        btns = QHBoxLayout()
        new_btn = QPushButton("＋ New Paper Account…")
        new_btn.clicked.connect(self._new_account)
        btns.addWidget(new_btn)
        edit_btn = QPushButton("✏️ Edit…")
        edit_btn.clicked.connect(self._edit_account)
        btns.addWidget(edit_btn)
        delete_btn = QPushButton("🗑 Delete…")
        delete_btn.setStyleSheet("color: #c0392b; font-weight: 700;")
        delete_btn.clicked.connect(self._delete_account)
        btns.addWidget(delete_btn)
        btns.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        btns.addWidget(close_btn)
        layout.addLayout(btns)

        self.refresh()

    def refresh(self) -> None:
        try:
            accounts = self.store.list_paper_accounts()
            data = self.store.load()
        except PaperTradesStoreError:
            accounts = []
            data = {}
        self._accounts = {str(a["uid"]).strip().upper(): a for a in accounts}
        keys = list(self._accounts.keys())
        self.table.setRowCount(len(keys))
        for i, uid in enumerate(keys):
            acc = self._accounts[uid]
            state = data.get("accounts", {}).get(uid, {}) or {}
            self._set_cell(i, 0, str(acc.get("display_name") or uid))
            self._set_cell(i, 1, format_number(acc.get("starting_cash", 0.0)))
            self._set_cell(i, 2, f"{float(acc.get('fee_pct', 0.0)):.2f}%")
            self._set_cell(i, 3, format_number(state.get("cash", 0.0)))
            self._set_cell(i, 4, str(len(state.get("positions", {}) or {})))
            realized = float(state.get("realized_pnl", 0.0))
            self._set_cell(i, 5, format_number(realized), self._pnl_color(realized))
        self.table.resizeColumnsToContents()

    def _set_cell(self, row: int, column: int, text: str, color: str = "") -> None:
        item = QTableWidgetItem(str(text))
        if color:
            item.setForeground(QColor(color))
        self.table.setItem(row, column, item)

    @staticmethod
    def _pnl_color(value: float) -> str:
        return "#27ae60" if value >= 0 else "#c0392b"

    def _selected_uid(self) -> str:
        row = self.table.currentRow()
        keys = list(self._accounts.keys())
        if row < 0 or row >= len(keys):
            return ""
        return keys[row]

    def _select_uid(self, uid: str) -> None:
        keys = list(self._accounts.keys())
        try:
            row = keys.index(str(uid).strip().upper())
        except ValueError:
            return
        self.table.selectRow(row)

    def _save_defaults(self) -> None:
        try:
            self.store.save_settings(
                self.default_cash_spin.value(), self.default_fee_spin.value()
            )
        except PaperTradesStoreError as exc:
            QMessageBox.warning(self, "Paper Accounts", str(exc))
            return
        QMessageBox.information(
            self,
            "Paper Accounts",
            "Defaults saved. They apply to NEW paper accounts only.",
        )

    def _new_account(self) -> None:
        try:
            settings = self.store.get_settings()
        except PaperTradesStoreError:
            settings = {}
        dialog = _PaperAccountEditorDialog(
            self,
            starting_cash=float(settings.get("starting_cash", PAPER_STARTING_CASH)),
            fee_pct=float(settings.get("fee_pct", PAPER_FEE_PCT)),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, cash, fee = dialog.values()
        if not name:
            QMessageBox.warning(self, "Paper Accounts", "Account name is required.")
            return
        try:
            uid = self.store.create_paper_account(name, cash, fee)
        except PaperTradesStoreError as exc:
            QMessageBox.warning(self, "Paper Accounts", str(exc))
            return
        QMessageBox.information(
            self, "Paper Accounts", f'Paper account "{name}" created.'
        )
        self.refresh()
        self._select_uid(uid)

    def _edit_account(self) -> None:
        uid = self._selected_uid()
        if not uid:
            QMessageBox.information(
                self, "Paper Accounts", "Select a paper account to edit."
            )
            return
        acc = self._accounts[uid]
        dialog = _PaperAccountEditorDialog(
            self,
            display_name=str(acc.get("display_name") or uid),
            starting_cash=float(acc.get("starting_cash", PAPER_STARTING_CASH)),
            fee_pct=float(acc.get("fee_pct", PAPER_FEE_PCT)),
            title="Edit Paper Account",
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, cash, fee = dialog.values()
        try:
            self.store.update_paper_account(
                uid, display_name=name, starting_cash=cash, fee_pct=fee
            )
        except PaperTradesStoreError as exc:
            QMessageBox.warning(self, "Paper Accounts", str(exc))
            return
        self.refresh()
        self._select_uid(uid)

    def _delete_account(self) -> None:
        uid = self._selected_uid()
        if not uid:
            QMessageBox.information(
                self, "Paper Accounts", "Select a paper account to delete."
            )
            return
        name = str(self._accounts[uid].get("display_name") or uid)
        ret = QMessageBox.question(
            self,
            "Delete paper account",
            f"Delete paper account '{name}'?\n\n"
            "All of its paper positions, trade history, cash and P&L will be permanently removed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.delete_paper_account(uid)
        except PaperTradesStoreError as exc:
            QMessageBox.warning(self, "Paper Accounts", str(exc))
            return
        self.refresh()


class PaperTradesDialog(QDialog):
    """Per-account paper trading ledger: open positions, history, P&L, export."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("📒 Paper Trading (Simulation) Ledger")
        self.resize(1100, 660)
        self.store = PaperTradesStore()
        self.login_store = BrokerLoginStore()

        layout = QVBoxLayout(self)

        hdr_layout = QHBoxLayout()
        hdr_lbl = QLabel("<b>📒 Paper Trading Ledger</b>")
        hdr_lbl.setStyleSheet("font-size: 16px; color: #17365d;")
        hdr_layout.addWidget(hdr_lbl)
        hdr_layout.addStretch(1)
        hdr_layout.addWidget(QLabel("Account:"))
        self.account_combo = QComboBox()
        self._populate_accounts()
        self.account_combo.currentIndexChanged.connect(lambda _idx: self.refresh())
        hdr_layout.addWidget(self.account_combo)
        self.refresh_marks_btn = QPushButton("↻ Refresh Marks")
        self.refresh_marks_btn.setToolTip(
            "Update unrealized P&L using the latest cached closes."
        )
        self.refresh_marks_btn.clicked.connect(self._refresh_marks_from_cache)
        hdr_layout.addWidget(self.refresh_marks_btn)
        self.paper_settings_btn = QPushButton("⚙️ Settings")
        self.paper_settings_btn.setToolTip(
            "Edit the paper trading defaults: starting cash for new accounts and the broker fee."
        )
        self.paper_settings_btn.clicked.connect(self._open_paper_settings)
        hdr_layout.addWidget(self.paper_settings_btn)
        layout.addLayout(hdr_layout)

        cards_row = QHBoxLayout()
        cards_row.setSpacing(12)
        self.card_cash_val, self.card_cash_box = self._create_card(
            "Paper Cash", "₹ 0.00", "#34495e"
        )
        self.card_realized_val, self.card_realized_box = self._create_card(
            "Realized P&L", "₹ 0.00", "#27ae60"
        )
        self.card_unrealized_val, self.card_unrealized_box = self._create_card(
            "Unrealized P&L", "₹ 0.00", "#2980b9"
        )
        self.card_total_val, self.card_total_box = self._create_card(
            "Total P&L", "₹ 0.00", "#8e44ad"
        )
        self.card_fees_val, self.card_fees_box = self._create_card(
            "Fees Paid", "₹ 0.00", "#7f8c8d"
        )
        self.card_positions_val, self.card_positions_box = self._create_card(
            "Open Positions", "0", "#c0392b"
        )
        for box in (
            self.card_cash_box,
            self.card_realized_box,
            self.card_unrealized_box,
            self.card_total_box,
            self.card_fees_box,
            self.card_positions_box,
        ):
            cards_row.addWidget(box, 1)
        layout.addLayout(cards_row)

        layout.addWidget(QLabel("<b>Open Positions</b>"))
        self.positions_table = QTableWidget()
        self.positions_table.setColumnCount(6)
        self.positions_table.setHorizontalHeaderLabels(
            [
                "Symbol",
                "Qty",
                "Avg Buy Price (₹)",
                "Last Mark (₹)",
                "Current Value (₹)",
                "Unrealized P&L (₹)",
            ]
        )
        self.positions_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.positions_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.positions_table.verticalHeader().setVisible(False)
        layout.addWidget(self.positions_table)

        layout.addWidget(QLabel("<b>Trade History</b>"))
        self.ledger_table = QTableWidget()
        self.ledger_table.setColumnCount(11)
        self.ledger_table.setHorizontalHeaderLabels(
            [
                "#",
                "Time (IST)",
                "Action",
                "Symbol",
                "Qty",
                "Price (₹)",
                "Value (₹)",
                "Fees (₹)",
                "Realized P&L (₹)",
                "Holding Days",
                "Template",
            ]
        )
        self.ledger_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.ledger_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.ledger_table.verticalHeader().setVisible(False)
        layout.addWidget(self.ledger_table)

        buttons = QHBoxLayout()
        export_btn = QPushButton("📥 Export to Excel")
        export_btn.setToolTip(
            "Save this account's summary, positions and trade history to an .xlsx file."
        )
        export_btn.clicked.connect(self._export)
        buttons.addWidget(export_btn)
        buttons.addStretch(1)
        reset_btn = QPushButton("🔄 Reset Paper Account")
        reset_btn.setToolTip(
            "Clear this account's paper positions, history, cash and P&L."
        )
        reset_btn.setStyleSheet(
            "font-weight: 700; color: #c0392b; padding: 4px 10px; border-radius: 3px;"
        )
        reset_btn.clicked.connect(self._reset_account)
        buttons.addWidget(reset_btn)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        self.refresh()

    def _create_card(self, title: str, value: str, color: str) -> tuple[QLabel, QFrame]:
        box = QFrame()
        box.setFrameShape(QFrame.Shape.StyledPanel)
        box.setStyleSheet(
            "QFrame { border: 1px solid #d0d7de; border-radius: 6px; background: #fafbfc; }"
        )
        v = QVBoxLayout(box)
        v.setContentsMargins(10, 8, 10, 8)
        title_lbl = QLabel(title)
        title_lbl.setStyleSheet("color: #556677; font-size: 11px;")
        val_lbl = QLabel(value)
        val_lbl.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {color};")
        v.addWidget(title_lbl)
        v.addWidget(val_lbl)
        return val_lbl, box

    @staticmethod
    def _pnl_color(value: float) -> str:
        return "#27ae60" if value >= 0 else "#c0392b"

    def _populate_accounts(self) -> None:
        accounts: list[dict[str, Any]] = []
        try:
            accounts = self.store.list_paper_accounts()
        except PaperTradesStoreError:
            pass
        self.account_combo.clear()
        for acc in accounts:
            uid = str(acc.get("uid", "")).strip().upper()
            label = str(acc.get("display_name") or uid)
            if uid:
                self.account_combo.addItem(f"📒 {label}", uid)

    def current_account(self) -> str:
        return str(self.account_combo.currentData() or "").strip()

    @staticmethod
    def _set_cell(
        table: QTableWidget, row: int, column: int, text: str, color: str = ""
    ) -> None:
        item = QTableWidgetItem(str(text))
        if color:
            item.setForeground(QColor(color))
        table.setItem(row, column, item)

    def _colorize_pnl(self, label: QLabel, value: float) -> None:
        base = label.styleSheet()
        color = self._pnl_color(value)
        label.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {color};")

    def refresh(self) -> None:
        uid = self.current_account()
        if not uid:
            self.card_cash_val.setText("₹ 0.00")
            self.card_realized_val.setText("₹ 0.00")
            self.card_unrealized_val.setText("₹ 0.00")
            self.card_total_val.setText("₹ 0.00")
            self.card_fees_val.setText("₹ 0.00")
            self.card_positions_val.setText("0")
            self.positions_table.setRowCount(0)
            self.ledger_table.setRowCount(0)
            return
        try:
            data = self.store.load()
            acc = data.get("accounts", {}).get(uid)
        except PaperTradesStoreError as exc:
            QMessageBox.warning(self, "Paper trading file preserved", str(exc))
            acc = None
        if not acc:
            acc = self.store.empty_account()
        cash = float(acc.get("cash", 0.0))
        realized = float(acc.get("realized_pnl", 0.0))
        fees = float(acc.get("fees_paid", 0.0))
        positions = acc.get("positions", {}) or {}
        unrealized = 0.0
        pos_rows: list[tuple[str, float, float, float, float, float]] = []
        for sym, pos in positions.items():
            qty = float(pos.get("qty", 0))
            avg = float(pos.get("avg_price", 0.0))
            mark = float(pos.get("last_mark") or 0.0)
            if mark > 0:
                value = mark * qty
                unrealized += (mark - avg) * qty
            else:
                value = avg * qty
            pos_rows.append(
                (sym, qty, avg, mark, value, (mark - avg) * qty if mark > 0 else 0.0)
            )
        total = realized + unrealized
        self.card_cash_val.setText(f"₹ {format_number(cash)}")
        self.card_realized_val.setText(f"₹ {format_number(realized)}")
        self.card_unrealized_val.setText(f"₹ {format_number(unrealized)}")
        self.card_total_val.setText(f"₹ {format_number(total)}")
        self.card_fees_val.setText(f"₹ {format_number(fees)}")
        self.card_positions_val.setText(f"{len(pos_rows)}")
        self._colorize_pnl(self.card_realized_val, realized)
        self._colorize_pnl(self.card_unrealized_val, unrealized)
        self._colorize_pnl(self.card_total_val, total)

        self.positions_table.setRowCount(len(pos_rows))
        for i, (sym, qty, avg, mark, value, upnl) in enumerate(pos_rows):
            self._set_cell(self.positions_table, i, 0, sym)
            self._set_cell(self.positions_table, i, 1, f"{qty:g}")
            self._set_cell(self.positions_table, i, 2, format_number(avg))
            self._set_cell(self.positions_table, i, 3, format_number(mark) if mark else "—")
            self._set_cell(self.positions_table, i, 4, format_number(value))
            self._set_cell(
                self.positions_table,
                i,
                5,
                format_number(upnl),
                self._pnl_color(upnl),
            )
        self.positions_table.resizeColumnsToContents()

        ledger = list(reversed(acc.get("ledger", []) or []))
        self.ledger_table.setRowCount(len(ledger))
        for i, entry in enumerate(ledger):
            rp = entry.get("realized_pnl")
            self._set_cell(self.ledger_table, i, 0, str(entry.get("id", "")))
            self._set_cell(self.ledger_table, i, 1, str(entry.get("ts_ist", "")))
            self._set_cell(self.ledger_table, i, 2, str(entry.get("action", "")))
            self._set_cell(self.ledger_table, i, 3, str(entry.get("symbol", "")))
            self._set_cell(self.ledger_table, i, 4, f"{int(entry.get('qty', 0) or 0):g}")
            self._set_cell(
                self.ledger_table, i, 5, format_number(entry.get("fill_price", 0.0))
            )
            self._set_cell(self.ledger_table, i, 6, format_number(entry.get("value", 0.0)))
            self._set_cell(self.ledger_table, i, 7, format_number(entry.get("fees", 0.0)))
            self._set_cell(
                self.ledger_table,
                i,
                8,
                format_number(rp) if rp is not None else "—",
                self._pnl_color(float(rp)) if rp is not None else "",
            )
            self._set_cell(
                self.ledger_table,
                i,
                9,
                str(entry.get("holding_days", ""))
                if entry.get("holding_days") is not None
                else "",
            )
            self._set_cell(self.ledger_table, i, 10, str(entry.get("template", "")))
        self.ledger_table.resizeColumnsToContents()

    def _open_paper_settings(self) -> None:
        PaperAccountsDialog(parent=self).exec()
        self._populate_accounts()
        self.refresh()

    def _refresh_marks_from_cache(self) -> None:
        uid = self.current_account()
        if not uid:
            return
        try:
            data = self.store.load()
            acc = data.get("accounts", {}).get(uid)
        except PaperTradesStoreError as exc:
            QMessageBox.warning(self, "Paper trading file preserved", str(exc))
            return
        if not acc:
            return
        marks: dict[str, float] = {}
        cache = PriceCache()
        for sym, pos in (acc.get("positions", {}) or {}).items():
            if float(pos.get("qty", 0)) > 0:
                close = cache.last_close(sym)
                if close:
                    marks[sym] = close
        if marks:
            try:
                self.store.mark_many(uid, marks)
            except PaperTradesStoreError as exc:
                QMessageBox.warning(self, "Paper trading file preserved", str(exc))
                return
        self.refresh()

    def _reset_account(self) -> None:
        uid = self.current_account()
        if not uid:
            return
        ret = QMessageBox.question(
            self,
            "Reset paper account",
            f"Reset the paper trading account {uid}?\n\n"
            "All paper positions, trade history, cash and P&L will be cleared.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.reset_account(uid)
        except PaperTradesStoreError as exc:
            QMessageBox.warning(self, "Paper trading file preserved", str(exc))
            return
        self.refresh()

    def _export(self) -> None:
        uid = self.current_account()
        if not uid:
            return
        try:
            data = self.store.load()
            acc = data.get("accounts", {}).get(uid)
        except PaperTradesStoreError as exc:
            QMessageBox.warning(self, "Paper trading file preserved", str(exc))
            return
        if not acc:
            return
        default_name = (
            APP_DIR
            / f"paper_trades_{uid}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Export paper trades", str(default_name), "Excel Workbook (*.xlsx)"
        )
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        try:
            export_paper_trades_workbook(path, uid, acc)
            QMessageBox.information(
                self, "Export complete", f"Saved paper trading records to:\n{path}"
            )
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))


class PlaceOrderDialog(QDialog):
    """Dialog to confirm and place a Zerodha buy order for a matched stock."""

    def __init__(
        self,
        symbol: str,
        close_price: float | None = None,
        action: str = "BUY",
        order_type: str = "LIMIT",
        enctoken: str = "",
        account_name: str = "",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.symbol = str(symbol).strip().upper()
        self.close_price = close_price or 0.0
        self.enctoken = str(enctoken).strip()
        self.account_name = str(account_name).strip().upper()

        self.setWindowTitle(f"Place Zerodha Order — {self.symbol}")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.symbol_lbl = QLabel(f"<b>{self.symbol}</b> (Last Price: ₹ {format_number(self.close_price)})")
        form.addRow("Stock Symbol:", self.symbol_lbl)
        form.addRow("Trading Account:", QLabel(f"<b>{self.account_name or 'Not selected'}</b>"))

        self.type_combo = QComboBox()
        self.type_combo.addItems(["BUY", "SELL"])
        self.type_combo.setCurrentText(action if action in ["BUY", "SELL"] else "BUY")
        form.addRow("Action:", self.type_combo)

        self.product_combo = QComboBox()
        self.product_combo.addItems(["CNC (Delivery)", "MIS (Intraday)"])
        form.addRow("Product:", self.product_combo)

        self.order_type_combo = QComboBox()
        self.order_type_combo.addItems(["LIMIT", "MARKET"])
        self.order_type_combo.setCurrentText(order_type if order_type in ["LIMIT", "MARKET"] else "LIMIT")
        self.order_type_combo.currentTextChanged.connect(self._toggle_limit_price)
        form.addRow("Order Type:", self.order_type_combo)

        self.qty_spin = QSpinBox()
        self.qty_spin.setRange(1, 100000)
        self.qty_spin.setValue(1)
        self.qty_spin.valueChanged.connect(self._update_total_val)
        form.addRow("Quantity (Shares):", self.qty_spin)

        self.limit_price_spin = QDoubleSpinBox()
        self.limit_price_spin.setRange(0.05, 1000000.0)
        self.limit_price_spin.setValue(self.close_price)
        self.limit_price_spin.setEnabled(self.order_type_combo.currentText() == "LIMIT")
        self.limit_price_spin.valueChanged.connect(self._update_total_val)
        form.addRow("Limit Price (₹):", self.limit_price_spin)

        self.total_val_lbl = QLabel("")
        form.addRow("Est. Order Value:", self.total_val_lbl)
        self._update_total_val()

        layout.addLayout(form)

        buttons = QHBoxLayout()
        confirm_btn = QPushButton("🛒 Confirm & Place Order")
        confirm_btn.clicked.connect(self._place_order)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        buttons.addStretch(1)
        buttons.addWidget(confirm_btn)
        buttons.addWidget(cancel_btn)
        layout.addLayout(buttons)

        self.placed_order_id: str | None = None

    def _toggle_limit_price(self, order_type: str) -> None:
        self.limit_price_spin.setEnabled(order_type == "LIMIT")
        self._update_total_val()

    def _update_total_val(self) -> None:
        price = self.limit_price_spin.value() if self.order_type_combo.currentText() == "LIMIT" else self.close_price
        total = price * self.qty_spin.value()
        self.total_val_lbl.setText(f"<b>₹ {format_number(total)}</b>")

    def _place_order(self) -> None:
        if not self.enctoken or not self.account_name:
            QMessageBox.warning(
                self,
                "Verified Account Required",
                "Select and log in to a specific Zerodha account before placing an order.",
            )
            return

        product_code = "CNC" if "CNC" in self.product_combo.currentText() else "MIS"
        order_type = self.order_type_combo.currentText()
        tx_type = self.type_combo.currentText()
        price = self.limit_price_spin.value() if order_type == "LIMIT" else 0.0

        try:
            record_error: Exception | None = None
            with _ORDER_SUBMISSION_LOCK:
                # Validate local idempotency state before any external submission.
                # A corrupt log must never be replaced after an order is accepted.
                load_today_execution_log()
                executor = ZerodhaOrderExecutor(self.enctoken)
                verified_user_id = executor.fetch_profile_user_id()
                if verified_user_id != self.account_name:
                    raise ValueError(
                        f"The selected token belongs to account {verified_user_id}, not {self.account_name}."
                    )
                if tx_type == "BUY":
                    can_buy, reason = check_can_buy_stock(
                        self.symbol,
                        self.enctoken,
                        self.account_name,
                    )
                    if not can_buy:
                        raise ValueError(reason)
                    validate_live_buy_holding_absent(executor, self.symbol)
                    validate_live_buy_funds(
                        executor,
                        self.qty_spin.value(),
                        price if order_type == "LIMIT" else self.close_price,
                        market_order=order_type == "MARKET",
                    )
                else:
                    validate_live_sell_quantity(
                        executor, self.symbol, self.qty_spin.value()
                    )
                res = executor.place_order(
                    symbol=self.symbol,
                    quantity=self.qty_spin.value(),
                    transaction_type=tx_type,
                    order_type=order_type,
                    product=product_code,
                    price=price,
                )
                order_id = str(res["order_id"])
                self.placed_order_id = order_id
                try:
                    if tx_type == "BUY":
                        record_stock_bought_today(self.symbol, self.account_name)
                    elif tx_type == "SELL":
                        record_stock_sold_today(self.symbol, self.account_name)
                except Exception as exc:
                    record_error = exc
            if record_error is not None:
                QMessageBox.critical(
                    self,
                    "Order Submitted — Local Record Failed",
                    f"Zerodha returned order ID {order_id}, but the local safety log "
                    f"could not be updated:\n{record_error}\n\nDo not retry this order. "
                    "Verify its final status in Zerodha.",
                )
                self.accept()
                return
            QMessageBox.information(
                self,
                "Order Submitted",
                f"Order submitted for {self.symbol}. Verify its final status in Zerodha.\n\nOrder ID: {order_id}",
            )
            self.accept()
        except OrderSubmissionUncertainError as exc:
            QMessageBox.critical(self, "Order Status Unknown", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "Order Failed", f"Could not place order on Zerodha:\n{exc}")


class MultiTemplateDialog(QDialog):
    """Dialog to configure and start/stop automated cycling through multiple saved templates."""

    def __init__(
        self,
        template_store: TemplateStore,
        active_templates: list[str],
        active_interval_mins: int,
        is_running: bool,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.template_store = template_store
        self.selected_templates: list[str] = list(active_templates)
        self.interval_mins: int = active_interval_mins
        self.is_running: bool = is_running
        self.should_start: bool = False
        self.should_stop: bool = False

        self.setWindowTitle("Multi-Template Auto-Scanner")
        self.resize(600, 480)

        layout = QVBoxLayout(self)

        header = QLabel("Multi-Template Automated Cycling & Telegram Alerts")
        header.setStyleSheet("font-size: 16px; font-weight: 700; color: #17365d;")
        layout.addWidget(header)

        desc = QLabel(
            "Select the saved templates you want to auto-run in sequential batch cycles. "
            "Every interval, all selected templates will run one by one, updating the results "
            "and sending template-segregated Telegram alerts for any hits."
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # Template checklist section
        template_group = QGroupBox("Saved Templates to Auto-Run")
        group_layout = QVBoxLayout(template_group)

        btn_bar = QHBoxLayout()
        select_all_btn = QPushButton("Select All")
        select_none_btn = QPushButton("Deselect All")
        select_all_btn.clicked.connect(self._select_all)
        select_none_btn.clicked.connect(self._select_none)
        refresh_btn = QPushButton("↻ Refresh List")
        refresh_btn.setToolTip(
            "Reload the saved-template list from scanner_templates.json now."
        )
        refresh_btn.clicked.connect(self._refresh_template_list)
        btn_bar.addWidget(select_all_btn)
        btn_bar.addWidget(select_none_btn)
        btn_bar.addWidget(refresh_btn)
        btn_bar.addStretch()
        group_layout.addLayout(btn_bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        self.checkbox_layout = QVBoxLayout(scroll_content)
        self.template_checkboxes: dict[str, QCheckBox] = {}
        self._last_templates_mtime_ns = self._template_file_mtime_ns()

        self._rebuild_template_list(checked_names=self.selected_templates)

        scroll.setWidget(scroll_content)
        group_layout.addWidget(scroll)
        layout.addWidget(template_group, 1)

        # Configuration options (Interval & Status)
        config_form = QFormLayout()
        self.interval_combo = QComboBox()
        for mins in (1, 2, 5, 10, 15, 30, 60):
            self.interval_combo.addItem(f"{mins} minute{'s' if mins > 1 else ''}", mins)
        
        idx = self.interval_combo.findData(self.interval_mins)
        if idx >= 0:
            self.interval_combo.setCurrentIndex(idx)
            
        config_form.addRow("Cycle Interval (N mins):", self.interval_combo)
        layout.addLayout(config_form)

        # Live Status bar / action buttons
        action_bar = QHBoxLayout()
        self.status_lbl = QLabel(
            "● Multi-Auto-Run is currently ACTIVE" if self.is_running else "○ Multi-Auto-Run is currently INACTIVE"
        )
        self.status_lbl.setStyleSheet(
            "font-weight: bold; color: #27ae60;" if self.is_running else "font-weight: bold; color: #7f8c8d;"
        )
        action_bar.addWidget(self.status_lbl, 1)

        self.start_btn = QPushButton("Start Multi-Auto-Run")
        self.start_btn.setStyleSheet(
            "background-color: #2980b9; color: white; font-weight: bold; padding: 6px 12px;"
        )
        self.start_btn.clicked.connect(self._on_start_clicked)

        self.stop_btn = QPushButton("Stop Multi-Auto-Run")
        self.stop_btn.setStyleSheet(
            "background-color: #c0392b; color: white; font-weight: bold; padding: 6px 12px;"
        )
        self.stop_btn.clicked.connect(self._on_stop_clicked)
        self.start_btn.setEnabled(not self.is_running)
        self.stop_btn.setEnabled(self.is_running)

        action_bar.addWidget(self.start_btn)
        action_bar.addWidget(self.stop_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        action_bar.addWidget(close_btn)

        layout.addLayout(action_bar)

        self._auto_refresh_timer = QTimer(self)
        self._auto_refresh_timer.setInterval(2_000)
        self._auto_refresh_timer.timeout.connect(self._auto_refresh_check)
        self._auto_refresh_timer.start()

    def _template_file_mtime_ns(self) -> int:
        try:
            return self.template_store.path.stat().st_mtime_ns
        except (OSError, AttributeError):
            return 0

    def _auto_refresh_check(self) -> None:
        current = self._template_file_mtime_ns()
        if current != self._last_templates_mtime_ns:
            self._last_templates_mtime_ns = current
            checked = [
                name for name, cb in self.template_checkboxes.items() if cb.isChecked()
            ]
            self._rebuild_template_list(checked_names=checked)

    def _refresh_template_list(self) -> None:
        checked = [
            name for name, cb in self.template_checkboxes.items() if cb.isChecked()
        ]
        self._rebuild_template_list(checked_names=checked)

    def _rebuild_template_list(
        self, checked_names: list[str] | None = None
    ) -> None:
        checked = (
            set(checked_names)
            if checked_names is not None
            else {
                name
                for name, cb in self.template_checkboxes.items()
                if cb.isChecked()
            }
        )
        while self.checkbox_layout.count():
            item = self.checkbox_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.template_checkboxes.clear()
        all_templates_dict = self.template_store.load_all()
        if not all_templates_dict:
            no_tpl_lbl = QLabel(
                "No saved templates found. Save a template first to use multi-scan."
            )
            no_tpl_lbl.setStyleSheet("color: #7f8c8d; font-style: italic;")
            self.checkbox_layout.addWidget(no_tpl_lbl)
            return
        for name in sorted(all_templates_dict.keys()):
            setup = all_templates_dict.get(name, {})
            is_algo = setup.get("is_algo_template", False) or bool(
                re.search(r"-\s*[A-Z0-9]+$", name.strip(), re.IGNORECASE)
            )
            icon = "⚡" if is_algo else "🔍"
            cb = QCheckBox(f"{icon} {name}")
            if name in checked:
                cb.setChecked(True)
            self.template_checkboxes[name] = cb
            self.checkbox_layout.addWidget(cb)

    def accept(self) -> None:
        if getattr(self, "_auto_refresh_timer", None) is not None:
            self._auto_refresh_timer.stop()
        super().accept()

    def reject(self) -> None:
        if getattr(self, "_auto_refresh_timer", None) is not None:
            self._auto_refresh_timer.stop()
        super().reject()

    def _select_all(self) -> None:
        for cb in self.template_checkboxes.values():
            cb.setChecked(True)

    def _select_none(self) -> None:
        for cb in self.template_checkboxes.values():
            cb.setChecked(False)

    def get_selected_templates(self) -> list[str]:
        return [name for name, cb in self.template_checkboxes.items() if cb.isChecked()]

    def get_interval_mins(self) -> int:
        return int(self.interval_combo.currentData() or 5)

    def _on_start_clicked(self) -> None:
        selected = self.get_selected_templates()
        if not selected:
            QMessageBox.warning(self, "No templates selected", "Please check at least one saved template to start multi-auto-run.")
            return
        self.should_start = True
        self.accept()

    def _on_stop_clicked(self) -> None:
        self.should_stop = True
        self.accept()


class ImportBundleDialog(QDialog):
    """Dialog allowing user to pick specific templates (or select all) when importing a bundle."""

    def __init__(
        self,
        templates: dict[str, dict[str, Any]],
        existing_names: set[str],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.templates = templates
        self.existing_names = existing_names
        self.setWindowTitle("Import Strategy Templates")
        self.resize(540, 480)
        layout = QVBoxLayout(self)

        header = QLabel(
            f"Found {len(templates)} strategy templates in file.\n"
            "Select which templates you would like to import into your scanner:"
        )
        header.setWordWrap(True)
        header.setStyleSheet(
            "font-weight: bold; font-size: 13px; color: #17365d; margin-bottom: 5px;"
        )
        layout.addWidget(header)

        self.list_widget = QListWidget()
        for name in sorted(templates):
            tag = " (Already exists)" if name in existing_names else ""
            item = QListWidgetItem(f"{name}{tag}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.list_widget.addItem(item)

        layout.addWidget(self.list_widget, 1)

        sel_layout = QHBoxLayout()
        select_all_btn = QPushButton("Select All")
        deselect_all_btn = QPushButton("Deselect All")
        select_all_btn.clicked.connect(lambda: self._set_all(True))
        deselect_all_btn.clicked.connect(lambda: self._set_all(False))
        sel_layout.addWidget(select_all_btn)
        sel_layout.addWidget(deselect_all_btn)
        sel_layout.addStretch(1)
        layout.addLayout(sel_layout)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        import_btn = QPushButton("Import Selected")
        cancel_btn = QPushButton("Cancel")
        import_btn.setStyleSheet(
            "font-weight: bold; background: #2980b9; color: white; padding: 6px 16px;"
        )
        cancel_btn.clicked.connect(self.reject)
        import_btn.clicked.connect(self.accept)
        buttons.addWidget(cancel_btn)
        buttons.addWidget(import_btn)
        layout.addLayout(buttons)

    def _set_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.list_widget.count()):
            self.list_widget.item(i).setCheckState(state)

    def get_selected_templates(self) -> dict[str, dict[str, Any]]:
        result = {}
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                raw_name = item.data(Qt.ItemDataRole.UserRole)
                result[raw_name] = self.templates[raw_name]
        return result


class ExportTemplateDialog(QDialog):
    """Dialog allowing user to choose export settings, including watchlist privacy options."""

    def __init__(
        self,
        template_name: str,
        custom_watchlists_used: list[str],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.template_name = template_name
        self.custom_watchlists_used = custom_watchlists_used
        self.setWindowTitle("Export Strategy Template")
        self.resize(500, 320)
        layout = QVBoxLayout(self)

        header = QLabel(f'Exporting Strategy: "{template_name}"')
        header.setStyleSheet("font-size: 15px; font-weight: bold; color: #17365d;")
        layout.addWidget(header)

        if custom_watchlists_used:
            wl_str = ", ".join(f'"{name}"' for name in custom_watchlists_used)
            desc = QLabel(
                f"This template uses the following custom watchlist(s): {wl_str}.\n\n"
                "You can choose whether to include the ticker symbols for these watchlists "
                "in the exported file:"
            )
            desc.setWordWrap(True)
            layout.addWidget(desc)

            self.include_watchlist_cb = QCheckBox(
                "Include custom watchlist ticker data in export file"
            )
            self.include_watchlist_cb.setChecked(True)
            self.include_watchlist_cb.setToolTip(
                "When checked, friends importing this file will get your custom watchlist stocks. "
                "Uncheck to keep your holdings/portfolio secret!"
            )
            layout.addWidget(self.include_watchlist_cb)
        else:
            desc = QLabel(
                "Exporting technical rules and strategy settings to a shareable JSON file."
            )
            desc.setWordWrap(True)
            layout.addWidget(desc)
            self.include_watchlist_cb = None

        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        export_btn = QPushButton("Continue Export...")
        cancel_btn = QPushButton("Cancel")
        export_btn.setStyleSheet(
            "font-weight: bold; background: #27ae60; color: white; padding: 6px 16px;"
        )
        cancel_btn.clicked.connect(self.reject)
        export_btn.clicked.connect(self.accept)
        buttons.addWidget(cancel_btn)
        buttons.addWidget(export_btn)
        layout.addLayout(buttons)

    def should_include_watchlists(self) -> bool:
        if self.include_watchlist_cb is not None:
            return self.include_watchlist_cb.isChecked()
        return True


class HistoryChartWidget(QWidget):
    dateSelected = Signal(str)
    PALETTE = (
        "#5bc0de",
        "#f06292",
        "#81c784",
        "#f4b64f",
        "#7e83e6",
        "#ef5b73",
        "#e0c94f",
        "#35989b",
        "#ef6461",
        "#80d8d0",
        "#e60070",
        "#449ddd",
        "#b8df52",
        "#26b9a8",
        "#ff7f0e",
        "#62d629",
    )

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.summary: list[dict[str, Any]] = []
        self.selected_date = ""
        self.left_margin = 48
        self.bar_step = 6
        self.setFixedHeight(310)
        self.setMouseTracking(True)
        self.setFixedWidth(700)

    @classmethod
    def sector_color(cls, sector: str) -> QColor:
        index = sum((position + 1) * ord(char) for position, char in enumerate(sector))
        return QColor(cls.PALETTE[index % len(cls.PALETTE)])

    def set_summary(self, summary: list[dict[str, Any]]) -> None:
        self.summary = copy.deepcopy(summary)
        self.selected_date = ""
        width = max(700, self.left_margin + len(self.summary) * self.bar_step + 25)
        self.setFixedWidth(width)
        self.update()

    def select_date(self, selected_date: str) -> None:
        self.selected_date = selected_date
        self.update()

    def _index_at(self, x: float) -> int | None:
        index = int((x - self.left_margin) // self.bar_step)
        return index if 0 <= index < len(self.summary) else None

    def mousePressEvent(self, event: Any) -> None:
        index = self._index_at(event.position().x())
        if index is not None:
            selected = self.summary[index]["date"]
            self.select_date(selected)
            self.dateSelected.emit(selected)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        index = self._index_at(event.position().x())
        if index is not None:
            item = self.summary[index]
            QToolTip.showText(
                event.globalPosition().toPoint(),
                f"{item['date']}: {item['total']} matches",
                self,
            )
        super().mouseMoveEvent(event)

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#ffffff"))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        if not self.summary:
            painter.setPen(QColor("#666666"))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "Run a 2-year or 4-year history scan to see daily matches.",
            )
            return

        top, bottom = 12, self.height() - 42
        plot_height = max(1, bottom - top)
        max_total = max(1, max(int(item["total"]) for item in self.summary))
        painter.setPen(QPen(QColor("#c8d2dc"), 1))
        painter.drawLine(self.left_margin, bottom, self.width() - 10, bottom)
        painter.drawLine(self.left_margin, top, self.left_margin, bottom)
        painter.setPen(QColor("#445566"))
        painter.drawText(4, top + 5, str(max_total))
        painter.drawText(24, bottom + 5, "0")

        label_every = max(1, len(self.summary) // 10)
        for index, item in enumerate(self.summary):
            x = self.left_margin + index * self.bar_step + 1
            y = bottom
            for sector, count in sorted(item.get("sectors", {}).items()):
                height = max(
                    1,
                    round(int(count) / max_total * plot_height),
                )
                y -= height
                painter.fillRect(x, y, self.bar_step - 2, height, self.sector_color(sector))
            if item["date"] == self.selected_date:
                total_height = round(int(item["total"]) / max_total * plot_height)
                painter.setPen(QPen(QColor("#17365d"), 2))
                painter.drawRect(
                    x - 1,
                    bottom - total_height - 2,
                    self.bar_step,
                    total_height + 3,
                )
            if index % label_every == 0 or index == len(self.summary) - 1:
                painter.setPen(QColor("#556677"))
                painter.drawText(x - 14, bottom + 18, item["date"][5:])


class AlgoPaperOrderWorker(QThread):
    """Applies a planned set of simulated paper trades to the paper ledger off the UI thread."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, plan: list[dict[str, Any]], parent: QObject | None = None):
        super().__init__(parent)
        self.plan = plan

    def run(self) -> None:
        try:
            store = PaperTradesStore()
            results: list[dict[str, Any]] = []
            for item in self.plan:
                try:
                    result = store.record_trade(
                        item["account"],
                        symbol=item["symbol"],
                        action=item["action"],
                        qty=item.get("qty", 0),
                        price=item["price"],
                        template=item.get("template", ""),
                        matched_rule=item.get("matched_rule", ""),
                        scan_id=item.get("scan_id", ""),
                        order_type=item.get("order_type", "LIMIT"),
                        product=item.get("product", "CNC"),
                        capital_mode=item.get("capital_mode", ""),
                        slot=item.get("slot"),
                        slots=item.get("slots"),
                        sell_percent=item.get("sell_percent"),
                    )
                except Exception as exc:
                    result = {
                        "placed": False,
                        "reason": str(exc),
                        "symbol": item.get("symbol", ""),
                        "action": item.get("action", ""),
                    }
                results.append(result)
            self.succeeded.emit({"results": results, "plan": self.plan})
        except Exception as exc:
            self.failed.emit(str(exc))


class PaperPipelineWorker(QThread):
    """Runs the strategy-guidance pipeline off the UI thread:

    1. Each selected rule template scans in advisory mode and (re)builds its
       PAPER watchlist from the matched symbols.
    2. The BUY FINAL RULE SET is evaluated over the complete candidate universe.
    3. EXIT/Weakness rules are evaluated independently over that same universe.
    4. Zerodha and model holdings are added only as read-only result overlays.
    5. When requested, exits are applied before entries in an independent model
       portfolio; no Zerodha order-placement path is reachable from this worker.
    """

    succeeded = Signal(object)
    failed = Signal(str)
    progress = Signal(int, str)

    def __init__(
        self,
        *,
        account: str,
        member_names: list[str],
        source_universes: list[str] | None = None,
        source_symbols: list[str] | None = None,
        finalset_name: str = "",
        buy_op: str = "OR",
        buy_k: int = 1,
        exit_names: list[str] | None = None,
        exit_finalset_name: str = "",
        exit_op: str = "OR",
        exit_k: int = 1,
        auto_watchlist: bool = True,
        track_model: bool = True,
        technical_adjusted: bool = True,
        include_current: bool = True,
        buy_setup: dict[str, Any] | None = None,
        templates: dict[str, dict[str, Any]] | None = None,
        cache_path: Path = PRICE_CACHE_DB,
    ):
        super().__init__()
        self.account = str(account).strip().upper()
        self.member_names = [
            str(name).strip() for name in member_names if str(name).strip()
        ]
        self.source_universes = list(
            dict.fromkeys(
                str(name).strip()
                for name in (source_universes or [])
                if str(name).strip()
            )
        )
        self.source_symbols = list(
            dict.fromkeys(
                str(symbol).strip().upper()
                for symbol in (source_symbols or [])
                if str(symbol).strip()
            )
        )
        self.finalset_name = str(finalset_name or "").strip()
        self.buy_op = str(buy_op or "OR").strip().upper()
        self.buy_k = max(1, int(buy_k or 1))
        self.exit_names = [
            str(name).strip() for name in (exit_names or []) if str(name).strip()
        ]
        self.exit_finalset_name = str(exit_finalset_name or "").strip()
        self.exit_op = str(exit_op or "OR").strip().upper()
        self.exit_k = max(1, int(exit_k or 1))
        self.auto_watchlist = auto_watchlist
        self.track_model = bool(track_model)
        self.technical_adjusted = bool(technical_adjusted)
        self.include_current = bool(include_current)
        self.buy_setup = copy.deepcopy(buy_setup or default_setup())
        self.templates = templates or {}
        self.cache_path = Path(cache_path)
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def _check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise ScanCancelled

    def _mapped_progress(
        self, start: int, end: int, prefix: str
    ) -> Callable[[int, str], None]:
        span = max(0, int(end) - int(start))

        def report(percent: int, text: str) -> None:
            try:
                nested = max(0, min(100, int(percent)))
            except (TypeError, ValueError):
                nested = 0
            mapped = int(start) + int(span * nested / 100)
            detail = str(text or "").strip()
            self.progress.emit(mapped, f"{prefix}: {detail}" if detail else prefix)

        return report

    def _member_setup(self, name: str) -> dict[str, Any]:
        setup = copy.deepcopy(self.templates[name])
        if self.source_universes:
            # The Strategy Guide source selection is a run-time override only;
            # the saved candidate template is never modified.
            setup["universes"] = list(self.source_universes)
            setup["custom_symbols"] = ""
        setup["as_of_mode"] = "latest"
        setup["adjusted"] = self.technical_adjusted
        setup["include_current"] = self.include_current
        setup["algo_paper_mode"] = False
        setup["algo_auto_order_cb"] = False
        setup["algo_auto_order_enabled"] = False
        setup["algo_paper_account"] = ""
        setup["template_name"] = name
        return setup

    def run(self) -> None:
        try:
            payload = self._run_pipeline()
            self.succeeded.emit(payload)
        except ScanCancelled:
            self.failed.emit("The strategy guide was cancelled.")
        except Exception as exc:
            self.failed.emit(traceback.format_exc())

    def _run_pipeline(self) -> dict[str, Any]:
        self.progress.emit(1, "Preparing Strategy Guide recipe…")
        if self.track_model and not self.account:
            raise ValueError("Select a model account or turn off model tracking.")
        if not self.member_names:
            raise ValueError("Select at least one rule template for the pipeline.")
        for name in self.member_names:
            if name not in self.templates:
                raise ValueError(f"Rule template '{name}' no longer exists.")

        scan_member_names = list(self.member_names)
        if self.finalset_name and self.finalset_name in self.templates:
            final_members = self.templates[self.finalset_name].get(
                "finalset_members", []
            )
            for raw_name in final_members:
                name = str(raw_name).strip()
                if name and name not in scan_member_names:
                    scan_member_names.append(name)
        scannable_member_names = [
            name for name in scan_member_names
            if name in self.templates
            and not is_final_template_setup(name, self.templates[name])
        ]
        watchlists: dict[str, list[str]] = {}
        union_symbols: list[str] = []
        union_rows: list[dict[str, Any]] = []
        guidance_universe: list[str] = []
        source_symbols: list[str] = []
        source_details: dict[str, dict[str, str]] = {}
        if self.source_symbols:
            source_symbols = list(self.source_symbols)
            source_label = ", ".join(self.source_universes) or "Strategy Guide source"
            source_details = {
                symbol: {"Universe": source_label} for symbol in source_symbols
            }
        elif self.source_universes:
            self.progress.emit(
                2, "Step 1A — resolving selected source watchlists/universes…"
            )
            source_symbols, source_errors, source_details = resolve_universes(
                self.source_universes,
                "",
                self._check_cancelled,
            )
            self._check_cancelled()
            if source_errors:
                raise ValueError(
                    "One or more selected Strategy Guide sources could not be "
                    "resolved:\n" + "\n".join(source_errors)
                )
            if not source_symbols:
                details = "\n".join(source_errors) or (
                    "The selected Strategy Guide sources contain no symbols."
                )
                raise ValueError(details)
        candidate_total = max(1, len(scannable_member_names))
        for candidate_index, name in enumerate(scannable_member_names, 1):
            self._check_cancelled()
            if expression_has_pnl_rules(self.templates[name].get("expression")):
                raise ValueError(
                    f"Strategy Guide template '{name}' contains a portfolio P&L rule. "
                    "BUY/EXIT guidance must be independent of holdings; replace it "
                    "with a price or technical-indicator rule."
                )
            setup = self._member_setup(name)
            setup["refresh_minutes"] = DEFAULT_REFRESH_MINUTES
            setup["force_fresh"] = False
            worker = RuleScannerWorker(
                setup,
                symbols=(
                    source_symbols
                    if self.source_symbols or self.source_universes else None
                ),
                symbol_details=(
                    source_details
                    if self.source_symbols or self.source_universes else None
                ),
                cache_path=self.cache_path,
            )
            phase_start = 4 + int((candidate_index - 1) * 52 / candidate_total)
            phase_end = 4 + int(candidate_index * 52 / candidate_total)
            candidate_prefix = (
                f"Step 1B — candidate rule {candidate_index}/{candidate_total} — {name}"
            )
            report = worker.scan(
                self._mapped_progress(phase_start, phase_end, candidate_prefix)
            )
            guidance_universe.extend(worker.symbols)
            for row in report.rows:
                tagged = dict(row)
                tagged["_Guide Member"] = name
                union_rows.append(tagged)
            symbols = [
                str(row.get("Symbol", "")).strip().upper()
                for row in report.rows
                if str(row.get("Symbol", "")).strip()
            ]
            union_symbols.extend(symbols)
            wl_name = paper_watchlist_name(name)
            if wl_name:
                watchlists[wl_name] = sorted(set(symbols))
            self.progress.emit(
                phase_end,
                f"Step 1B — candidate rule {candidate_index}/{candidate_total} complete; "
                f"{len(symbols):,} match(es)",
            )
        union_symbols = list(dict.fromkeys(union_symbols))
        guidance_universe = list(dict.fromkeys(guidance_universe))

        shortlist: list[dict[str, Any]] = []
        self.progress.emit(58, "Step 2 — applying BUY confirmation…")
        if self.finalset_name:
            self._check_cancelled()
            if self.finalset_name not in self.templates:
                raise ValueError(
                    f"FINAL RULE SET '{self.finalset_name}' no longer exists."
                )
            member_universe = list(union_symbols)
            fs_setup = copy.deepcopy(self.templates[self.finalset_name])
            fs_setup.update({
                "as_of_mode": "latest",
                "algo_paper_mode": False,
                "algo_auto_order_cb": False,
                "algo_auto_order_enabled": False,
                "algo_paper_account": "",
                "template_name": self.finalset_name,
                "adjusted": self.technical_adjusted,
                "include_current": self.include_current,
                "force_fresh": False,
                "refresh_minutes": DEFAULT_REFRESH_MINUTES,
            })
            if member_universe:
                fs_worker = FinalsetScannerWorker(
                    fs_setup,
                    templates=self.templates,
                    symbols=member_universe,
                    cache_path=self.cache_path,
                )
                shortlist = fs_worker.scan(
                    self._mapped_progress(
                        58, 72, f"Step 2 — saved BUY set — {self.finalset_name}"
                    )
                ).rows
        else:
            combined_rows: dict[str, dict[str, Any]] = {}
            for row in union_rows:
                sym = str(row.get("Symbol", "")).strip().upper()
                if not sym:
                    continue
                member = str(row.get("_Guide Member", "")).strip()
                if sym not in combined_rows:
                    combined_rows[sym] = dict(row)
                    combined_rows[sym]["_Guide Members"] = []
                if member and member not in combined_rows[sym]["_Guide Members"]:
                    combined_rows[sym]["_Guide Members"].append(member)
            for row in combined_rows.values():
                members = list(row.pop("_Guide Members", []))
                row.pop("_Guide Member", None)
                row["Matched Members"] = ", ".join(members)
                row["Confirmation Count"] = len(members)
                shortlist.append(row)
            required = 1
            if self.buy_op == "AND":
                required = len(self.member_names)
            elif self.buy_op.startswith("ANY"):
                required = min(len(self.member_names), self.buy_k)
            shortlist = [
                row for row in shortlist
                if int(row.get("Confirmation Count", 0) or 0) >= required
            ]

        for row in shortlist:
            members = [
                item.strip()
                for item in str(row.get("Matched Members", "")).split(",")
                if item.strip()
            ]
            row["Confirmation Count"] = max(
                int(row.get("Confirmation Count", 0) or 0), len(members), 1
            )
            row["Signal"] = "BUY"
        shortlist.sort(
            key=lambda row: (
                -int(row.get("Confirmation Count", 0) or 0),
                str(row.get("Symbol", "")),
            )
        )

        self.progress.emit(
            73, f"Step 2 complete — {len(shortlist):,} BUY suggestion(s)"
        )
        self.progress.emit(74, "Step 3 — evaluating EXIT / weakness rules…")
        exit_rows = self._scan_exit_signals(
            guidance_universe,
            self._mapped_progress(74, 90, "Step 3 — EXIT / weakness"),
        )
        exit_symbols = {
            str(row.get("Symbol", "")).strip().upper() for row in exit_rows
        }
        buy_symbols = {
            str(row.get("Symbol", "")).strip().upper() for row in shortlist
        }
        guidance_by_symbol: dict[str, dict[str, Any]] = {}
        for row in shortlist:
            symbol = str(row.get("Symbol", "")).strip().upper()
            if symbol:
                guidance_by_symbol[symbol] = dict(row)
        for exit_row in exit_rows:
            symbol = str(exit_row.get("Symbol", "")).strip().upper()
            if not symbol:
                continue
            if symbol in guidance_by_symbol:
                existing = guidance_by_symbol[symbol]
                existing["Signal"] = "REVIEW (BUY + EXIT)"
                exit_evidence = str(exit_row.get("Evidence", "")).strip()
                if exit_evidence:
                    prior = str(existing.get("Evidence", "")).strip()
                    existing["Evidence"] = (
                        prior + " | [EXIT] " + exit_evidence
                        if prior else "[EXIT] " + exit_evidence
                    )
            else:
                item = dict(exit_row)
                item["Signal"] = "EXIT / WEAKNESS"
                guidance_by_symbol[symbol] = item

        self.progress.emit(
            91, f"Step 3 complete — {len(exit_symbols):,} EXIT/weakness signal(s)"
        )
        self.progress.emit(93, "Adding model and Zerodha read-only overlays…")
        model_symbols: set[str] = set()
        if self.account:
            try:
                model_data = PaperTradesStore(
                    self.cache_path.parent / "paper_trades.db"
                ).load()
                model_positions = (
                    model_data.get("accounts", {})
                    .get(self.account, {})
                    .get("positions", {})
                )
                model_symbols = {
                    str(symbol).strip().upper()
                    for symbol, pos in model_positions.items()
                    if float(pos.get("qty", 0)) > 0
                }
            except Exception:
                model_symbols = set()
        zerodha_accounts: dict[str, set[str]] = {}
        try:
            for holding in HoldingsStore(
                self.cache_path.parent / "holdings_details.json"
            ).load_all().values():
                if float(holding.get("qty", 0)) <= 0:
                    continue
                symbol = str(holding.get("symbol", "")).strip().upper()
                account = str(holding.get("account", "MAIN")).strip().upper()
                if symbol:
                    zerodha_accounts.setdefault(symbol, set()).add(account)
        except Exception:
            zerodha_accounts = {}

        guidance_rows = list(guidance_by_symbol.values())
        for row in guidance_rows:
            symbol = str(row.get("Symbol", "")).strip().upper()
            row["In Model Portfolio"] = "Yes" if symbol in model_symbols else "No"
            accounts = sorted(zerodha_accounts.get(symbol, set()))
            row["In Zerodha"] = "Yes" if accounts else "No"
            row["Zerodha Accounts"] = ", ".join(accounts)
        guidance_rows.sort(
            key=lambda row: (
                0 if str(row.get("Signal")) == "BUY" else 1,
                -int(row.get("Confirmation Count", 0) or 0),
                str(row.get("Symbol", "")),
            )
        )

        sell_results: list[dict[str, Any]] = []
        buy_results: list[dict[str, Any]] = []
        if self.track_model:
            self.progress.emit(96, "Updating the independent simulated model portfolio…")
            # Release cash/slots first and never buy a same-cycle EXIT signal.
            sell_results = self._execute_plan(
                self._build_model_sell_plan(exit_rows)
            )
            eligible_buys = [
                row for row in shortlist
                if str(row.get("Symbol", "")).strip().upper() not in exit_symbols
            ]
            buy_results = self._execute_plan(self._build_buy_plan(eligible_buys))
        if self.auto_watchlist and watchlists:
            store = WatchlistStore(self.cache_path.parent / WATCHLISTS_FILE.name)
            saved_watchlists = store.load()
            saved_watchlists.update(watchlists)
            store.save(saved_watchlists)
        self.progress.emit(100, "Strategy Guide complete")
        return {
            "account": self.account,
            "shortlist": shortlist,
            "exit_signals": exit_rows,
            "guidance_rows": guidance_rows,
            "watchlists": watchlists,
            "buy_results": buy_results,
            "sell_results": sell_results,
            "model_tracking": self.track_model,
            "buy_signal_count": len(buy_symbols),
            "exit_signal_count": len(exit_symbols),
        }

    def _scan_exit_signals(
        self,
        universe_symbols: list[str],
        progress: Callable[[int, str], None],
    ) -> list[dict[str, Any]]:
        """Evaluate EXIT/Weakness rules broadly, independent of any holdings."""
        if not universe_symbols:
            return []
        exit_op = self.exit_op
        exit_k = self.exit_k
        exit_members: list[str] = []
        exit_label = ""
        if self.exit_finalset_name and self.exit_finalset_name in self.templates:
            final_setup = self.templates[self.exit_finalset_name]
            if is_final_template_setup(self.exit_finalset_name, final_setup):
                exit_members = [
                    str(name).strip()
                    for name in final_setup.get("finalset_members", [])
                    if str(name).strip() in self.templates
                ]
                exit_op = str(final_setup.get("finalset_op", "AND") or "AND")
                try:
                    exit_k = max(1, int(final_setup.get("finalset_k", 1) or 1))
                except (TypeError, ValueError):
                    exit_k = 1
                exit_label = self.exit_finalset_name
        if not exit_members:
            exit_members = [
                name for name in self.exit_names if name in self.templates
            ]
        if not exit_members:
            return []
        pnl_members = [
            name for name in exit_members
            if expression_has_pnl_rules(self.templates[name].get("expression"))
        ]
        if pnl_members:
            raise ValueError(
                "Broad EXIT/weakness guidance cannot use portfolio P&L rules because "
                "it is intentionally independent of holdings. Replace the P&L rules "
                "with technical exit rules in: " + ", ".join(pnl_members)
            )
        synthetic = default_setup()
        synthetic.update({
            "template_name": exit_label or "EXIT / WEAKNESS GUIDE",
            "as_of_mode": "latest",
            "adjusted": self.technical_adjusted,
            "include_current": self.include_current,
            "force_fresh": False,
            "refresh_minutes": DEFAULT_REFRESH_MINUTES,
            "is_final_template": True,
            "finalset_members": exit_members,
            "finalset_op": exit_op,
            "finalset_k": exit_k,
            "finalset_side": "EXIT",
            "algo_paper_mode": False,
            "algo_auto_order_cb": False,
            "algo_auto_order_enabled": False,
            "algo_paper_account": "",
        })
        worker = FinalsetScannerWorker(
            synthetic,
            templates=self.templates,
            symbols=list(universe_symbols),
            cache_path=self.cache_path,
        )
        rows = worker.scan(progress).rows
        for row in rows:
            row["Signal"] = "EXIT / WEAKNESS"
            members = [
                item.strip()
                for item in str(row.get("Matched Members", "")).split(",")
                if item.strip()
            ]
            row["Confirmation Count"] = max(1, len(members))
        return rows

    def _build_buy_plan(self, shortlist: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not shortlist:
            return []
        try:
            data = PaperTradesStore(
                self.cache_path.parent / "paper_trades.db"
            ).load()
            positions = (
                data.get("accounts", {}).get(self.account, {}).get("positions", {})
            )
        except Exception:
            positions = {}
        open_symbols = {
            str(symbol).strip().upper()
            for symbol, pos in positions.items()
            if float(pos.get("qty", 0)) > 0
        }
        configured_slots = max(
            1, int(self.buy_setup.get("algo_max_slots", 5) or 5)
        )
        remaining_slots = max(0, configured_slots - len(open_symbols))
        if remaining_slots <= 0:
            return []
        ranked = [
            row for row in shortlist
            if str(row.get("Symbol", "")).strip().upper() not in open_symbols
        ][:remaining_slots]
        if not ranked:
            return []
        buy_setup = copy.deepcopy(self.buy_setup)
        buy_setup.update({
            "template_name": self.finalset_name or "+".join(self.member_names[:3]),
            "as_of_mode": "latest",
            "algo_trade_action": "BUY (Entry Order - Budget Based)",
            "algo_auto_order_cb": True,
            "algo_auto_order_enabled": True,
            "algo_paper_account": self.account,
        })
        # Allocate current cash over the genuinely available slots, while the
        # ledger continues to display the configured portfolio slot ceiling.
        buy_setup["algo_max_slots"] = remaining_slots
        plan = build_paper_plan(ranked, buy_setup, self.account)
        for item in plan:
            item["slots"] = configured_slots
        return plan

    def _build_model_sell_plan(
        self, exit_rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Turn broad EXIT signals into actions only for model-held symbols."""
        if not exit_rows:
            return []
        store = PaperTradesStore(self.cache_path.parent / "paper_trades.db")
        try:
            data = store.load()
            positions = data.get("accounts", {}).get(self.account, {}).get("positions", {})
        except PaperTradesStoreError:
            return []
        open_symbols = {
            str(symbol).strip().upper()
            for symbol, pos in (positions or {}).items()
            if float(pos.get("qty", 0)) > 0
        }
        sell_rows = [
            row for row in exit_rows
            if str(row.get("Symbol", "")).strip().upper() in open_symbols
        ]
        if not sell_rows:
            return []
        sell_setup = copy.deepcopy(self.buy_setup)
        sell_setup.update({
            "template_name": self.exit_finalset_name
            or "+".join(self.exit_names[:3])
            or "MODEL EXITS",
            "as_of_mode": "latest",
            "algo_trade_action": "SELL (Exit Order - Portfolio Holdings Guard)",
            "algo_sell_mode": "100% (Sell Full Holding Quantity)",
            "algo_auto_order_cb": True,
            "algo_auto_order_enabled": True,
            "algo_paper_account": self.account,
        })
        return build_paper_plan(sell_rows, sell_setup, self.account)

    def _execute_plan(self, plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
        store = PaperTradesStore(self.cache_path.parent / "paper_trades.db")
        results: list[dict[str, Any]] = []
        for item in plan:
            self._check_cancelled()
            try:
                result = store.record_trade(
                    item["account"],
                    symbol=item["symbol"],
                    action=item["action"],
                    qty=item.get("qty", 0),
                    price=item["price"],
                    template=item.get("template", ""),
                    matched_rule=item.get("matched_rule", ""),
                    scan_id=item.get("scan_id", ""),
                    order_type=item.get("order_type", "LIMIT"),
                    product=item.get("product", "CNC"),
                    capital_mode=item.get("capital_mode", ""),
                    slot=item.get("slot"),
                    slots=item.get("slots"),
                    sell_percent=item.get("sell_percent"),
                )
            except Exception as exc:
                result = {
                    "placed": False,
                    "reason": str(exc),
                    "symbol": item.get("symbol", ""),
                    "action": item.get("action", ""),
                }
            results.append(result)
        return results


def build_strategy_guide_telegram_messages(
    payload: dict[str, Any], config: dict[str, Any]
) -> list[str]:
    """Build one consolidated, chunk-safe report for an automation cycle."""
    recipe = str(config.get("recipe_name", "") or "Strategy Guide").strip()
    rows = list(payload.get("guidance_rows", []) or [])
    feeder_results = list(payload.get("feeder_results", []) or [])
    buy_rows = [row for row in rows if str(row.get("Signal", "")) == "BUY"]
    review_rows = [
        row for row in rows if str(row.get("Signal", "")).startswith("REVIEW")
    ]
    exit_rows = [
        row for row in rows if str(row.get("Signal", "")).startswith("EXIT")
    ]
    lines = [
        "ChartSage Automated Strategy Guide",
        f"Recipe: {recipe}",
        f"Generated: {get_ist_now():%Y-%m-%d %H:%M:%S} IST",
        f"Source watchlist: {payload.get('combined_watchlist_name', 'n/a')}",
        f"Source symbols: {int(payload.get('combined_source_count', 0) or 0):,}",
        "",
        f"BUY: {len(buy_rows)} | EXIT/Weakness: {len(exit_rows)} | "
        f"BUY+EXIT Review: {len(review_rows)}",
    ]
    if feeder_results:
        lines.extend(["", "Feeder scans:"])
        for item in feeder_results:
            lines.append(
                f"- {item.get('template', '')}: "
                f"{int(item.get('matches', 0) or 0):,} match(es)"
            )

    def add_signal_section(title: str, signal_rows: list[dict[str, Any]]) -> None:
        lines.extend(["", f"{title} ({len(signal_rows)}):"])
        if not signal_rows:
            lines.append("- None")
            return
        for index, row in enumerate(signal_rows, 1):
            symbol = str(row.get("Symbol", "")).strip()
            close_value = finite_or_none(row.get("Close"))
            confirmations = int(row.get("Confirmation Count", 0) or 0)
            members = str(row.get("Matched Members", "") or "").strip()
            details = [f"{index}. {symbol}"]
            if close_value is not None:
                details.append(f"Close {format_number(close_value)}")
            if confirmations:
                details.append(f"Confirmations {confirmations}")
            lines.append(" | ".join(details))
            if members:
                lines.append(f"   Rules: {members}")

    add_signal_section("BUY suggestions", buy_rows)
    add_signal_section("EXIT / weakness", exit_rows)
    add_signal_section("REVIEW conflicts", review_rows)
    if not rows:
        lines.extend(["", "No BUY or EXIT/weakness signals were generated."])

    buy_results = list(payload.get("buy_results", []) or [])
    sell_results = list(payload.get("sell_results", []) or [])
    if bool(payload.get("model_tracking", False)):
        lines.extend([
            "",
            "Model portfolio actions: "
            f"{sum(bool(item.get('placed')) for item in buy_results)} BUY, "
            f"{sum(bool(item.get('placed')) for item in sell_results)} EXIT.",
        ])
    return chunk_telegram_lines(
        lines,
        continuation_header=f"ChartSage Strategy Guide — {recipe} (continued)",
    )


class StrategyAutomationWorker(QThread):
    """Run feeder templates atomically, then execute one Strategy Guide cycle."""

    succeeded = Signal(object)
    failed = Signal(str)
    progress = Signal(int, str)

    def __init__(
        self,
        *,
        config: dict[str, Any],
        guide_kwargs: dict[str, Any],
        templates: dict[str, dict[str, Any]],
        cache_path: Path = PRICE_CACHE_DB,
    ):
        super().__init__()
        self.config = normalize_strategy_automation_config(config)
        self.guide_kwargs = copy.deepcopy(guide_kwargs)
        self.templates = copy.deepcopy(templates)
        self.cache_path = Path(cache_path)
        self._cancel = threading.Event()
        self._active_worker: RuleScannerWorker | None = None
        self._nested_pipeline: PaperPipelineWorker | None = None

    def cancel(self) -> None:
        self._cancel.set()
        if self._active_worker is not None:
            self._active_worker.cancel()
        if self._nested_pipeline is not None:
            self._nested_pipeline.cancel()

    def _check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise ScanCancelled

    def run(self) -> None:
        try:
            self.succeeded.emit(self._run_cycle())
        except ScanCancelled:
            self.failed.emit("The automated Strategy Guide cycle was cancelled.")
        except Exception:
            self.failed.emit(traceback.format_exc())
        finally:
            self._active_worker = None
            self._nested_pipeline = None

    def _run_cycle(self) -> dict[str, Any]:
        feeder_names = list(self.config.get("feeder_templates", []))
        if not feeder_names:
            raise ValueError("Select at least one General Scan feeder template.")
        combined_name = str(
            self.config.get("combined_watchlist_name", "") or ""
        ).strip()
        if not combined_name:
            raise ValueError("Enter a combined Strategy Guide source watchlist name.")
        if not re.fullmatch(r"[A-Za-z0-9\s_-]+", combined_name):
            raise ValueError(
                "The combined watchlist name may contain only letters, numbers, "
                "spaces, hyphens, and underscores."
            )

        feeder_rows: list[dict[str, Any]] = []
        combined_symbols: list[str] = []
        pending_watchlists: dict[str, list[str]] = {}
        total = len(feeder_names)
        for index, name in enumerate(feeder_names, 1):
            self._check_cancelled()
            if name not in self.templates:
                raise ValueError(f"Feeder template '{name}' no longer exists.")
            source_setup = self.templates[name]
            if is_final_template_setup(name, source_setup):
                raise ValueError(f"'{name}' is a FINAL RULE SET, not a feeder scan.")
            if expression_has_pnl_rules(source_setup.get("expression")):
                raise ValueError(
                    f"Feeder template '{name}' contains a portfolio-dependent P&L rule."
                )
            setup = copy.deepcopy(source_setup)
            setup.update({
                "as_of_mode": "latest",
                "adjusted": bool(self.config.get("technical_adjusted", True)),
                "include_current": bool(self.config.get("include_current", True)),
                "force_fresh": False,
                "refresh_minutes": DEFAULT_REFRESH_MINUTES,
                "algo_paper_mode": False,
                "algo_auto_order_cb": False,
                "algo_auto_order_enabled": False,
                "algo_paper_account": "",
                "template_name": name,
            })
            phase_start = 3 + int((index - 1) * 47 / max(1, total))
            phase_end = 3 + int(index * 47 / max(1, total))
            self.progress.emit(
                phase_start, f"Feeder {index}/{total} — {name}"
            )
            worker = RuleScannerWorker(setup, cache_path=self.cache_path)
            self._active_worker = worker

            def feeder_progress(percent: int, text: str, start: int = phase_start, end: int = phase_end, label: str = name) -> None:
                mapped = start + int((end - start) * max(0, min(100, percent)) / 100)
                self.progress.emit(mapped, f"Feeder — {label}: {text}")

            report = worker.scan(feeder_progress)
            self._active_worker = None
            symbols = list(
                dict.fromkeys(
                    str(row.get("Symbol", "")).strip().upper()
                    for row in report.rows
                    if str(row.get("Symbol", "")).strip()
                )
            )
            combined_symbols.extend(symbols)
            feeder_rows.append({
                "template": name,
                "matches": len(symbols),
                "symbols": symbols,
                "scanned": report.scanned_symbols,
            })
            if bool(self.config.get("create_individual_watchlists", True)):
                individual_name = guide_feeder_watchlist_name(name)
                if individual_name:
                    pending_watchlists[individual_name] = symbols
            self.progress.emit(
                phase_end,
                f"Feeder {index}/{total} complete — {len(symbols):,} match(es)",
            )

        self._check_cancelled()
        combined_symbols = list(dict.fromkeys(combined_symbols))
        pending_watchlists[combined_name] = combined_symbols
        self.progress.emit(
            52,
            f"Feeder scans complete; "
            f"combined source has {len(combined_symbols):,} symbol(s)",
        )

        if combined_symbols:
            nested_kwargs = copy.deepcopy(self.guide_kwargs)
            create_paper_watchlists = bool(
                nested_kwargs.get("auto_watchlist", True)
            )
            nested_kwargs.update({
                "source_universes": [combined_name],
                "source_symbols": combined_symbols,
                # Commit PAPER and feeder watchlists together only after the
                # entire guide succeeds.
                "auto_watchlist": False,
                "templates": self.templates,
                "cache_path": self.cache_path,
            })
            pipeline = PaperPipelineWorker(**nested_kwargs)
            self._nested_pipeline = pipeline
            pipeline.progress.connect(
                lambda percent, text: self.progress.emit(
                    54 + int(max(0, min(100, percent)) * 45 / 100), text
                )
            )
            payload = pipeline._run_pipeline()
            self._nested_pipeline = None
            if create_paper_watchlists:
                pending_watchlists.update(
                    {
                        str(name): list(symbols)
                        for name, symbols in (payload.get("watchlists", {}) or {}).items()
                    }
                )
        else:
            payload = {
                "account": str(self.guide_kwargs.get("account", "")),
                "shortlist": [],
                "exit_signals": [],
                "guidance_rows": [],
                "watchlists": {},
                "buy_results": [],
                "sell_results": [],
                "model_tracking": bool(self.guide_kwargs.get("track_model", False)),
                "buy_signal_count": 0,
                "exit_signal_count": 0,
            }
        self._check_cancelled()
        store = WatchlistStore(self.cache_path.parent / WATCHLISTS_FILE.name)
        watchlists = store.load()
        if combined_name in UNIVERSES and combined_name not in watchlists:
            raise ValueError(
                f"'{combined_name}' is a reserved built-in universe name."
            )
        watchlists.update(pending_watchlists)
        store.save(watchlists)
        payload.update({
            "automation_recipe": str(self.config.get("recipe_name", "")),
            "feeder_results": feeder_rows,
            "feeder_watchlists": pending_watchlists,
            "combined_watchlist_name": combined_name,
            "combined_source_count": len(combined_symbols),
        })
        self.progress.emit(100, "Automated Strategy Guide cycle complete")
        return payload


class StrategyGuideWindow(QMainWindow):
    """Top-level host for the Strategy Guide page while it is detached."""

    return_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle("ChartSage — Strategy Guide")
        self.resize(1500, 900)

    def closeEvent(self, event: Any) -> None:
        self.return_requested.emit()
        event.accept()


class ScannerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"ChartSage Telegram Rule Scanner — data folder: {DATA_DIR}")
        self.resize(1450, 900)
        self.store = TemplateStore()
        self._store_error: str | None = None
        try:
            state = self.store.load_state()
        except TemplateStoreError as exc:
            state = self.store._empty_state()
            self._store_error = str(exc)
        self.active_template_name: str | None = state.get("active")
        self.setup = copy.deepcopy(
            state.get("templates", {}).get(self.active_template_name)
            or state.get("recovery")
            or default_setup()
        )
        self._normalize_general_templates()
        self.app_settings = AppSettingsStore()
        self.worker: RuleScannerWorker | None = None
        self.history_worker: HistoryScannerWorker | None = None
        self.telegram_settings_store = TelegramSettingsStore()
        self.telegram_state_store = TelegramAlertStateStore()
        self.watchlist_store = WatchlistStore()
        self._watchlist_store_error: str | None = None
        self.login_store = BrokerLoginStore()
        try:
            sync_zerodha_system_watchlist()
        except Exception:
            pass
        try:
            self.watchlists = self.watchlist_store.load()
        except WatchlistStoreError as exc:
            self.watchlists = {}
            self._watchlist_store_error = str(exc)
        for name, symbols in self.watchlists.items():
            UNIVERSES[name] = symbols
        self.telegram_worker: TelegramSendWorker | None = None
        self.telegram_pending_state: dict[str, tuple[str, set[str]]] = {}
        self.telegram_state_errors: list[str] = []
        self.last_rows: list[dict[str, Any]] = []
        self.last_scan_setup: dict[str, Any] | None = None
        self.last_history_report: HistoryReport | None = None
        self.last_history_setup: dict[str, Any] | None = None
        self._strategy_guide_window: StrategyGuideWindow | None = None
        self._strategy_guide_tab_index = 2
        self._stale = False
        self._history_stale = False
        self._dirty = False
        self._loading = True
        self._refreshing_result_columns = False
        self._moving_result_columns = False
        saved_multi_tpls, saved_multi_mins = self.store.load_multi_scan_config()
        self._multi_scan_active: bool = False
        self._multi_scan_templates: list[str] = saved_multi_tpls
        self._multi_scan_interval_mins: int = saved_multi_mins
        self._multi_scan_queue: list[str] = []
        self._multi_scan_timer = QTimer(self)
        self._multi_scan_timer.setSingleShot(True)
        self._multi_scan_timer.timeout.connect(self._start_next_multi_scan_cycle)
        try:
            automation_state = self.store.load_strategy_automation_recipes()
        except TemplateStoreError:
            automation_state = {"active": "", "recipes": {}}
        self._guide_automation_recipes: dict[str, dict[str, Any]] = copy.deepcopy(
            automation_state.get("recipes", {})
        )
        self._guide_automation_active_recipe = str(
            automation_state.get("active", "") or ""
        )
        self._guide_automation_config = normalize_strategy_automation_config(
            self._guide_automation_recipes.get(
                self._guide_automation_active_recipe, {}
            )
        )
        self._guide_automation_active = False
        self._guide_automation_worker: StrategyAutomationWorker | None = None
        self._guide_automation_running_recipe = ""
        self._guide_automation_run_config: dict[str, Any] = {}
        self._guide_automation_run_manual = False
        self._guide_automation_next_due: dict[str, datetime] = {}
        self._guide_automation_timer = QTimer(self)
        self._guide_automation_timer.setSingleShot(True)
        self._guide_automation_timer.timeout.connect(
            self._run_strategy_automation_cycle
        )
        self.auto_scan_timer = QTimer(self)
        self.auto_scan_timer.setSingleShot(True)
        self.auto_scan_timer.timeout.connect(self._auto_run_due)
        self._holdings_sync_in_progress = False
        self._holdings_auto_sync_timer = QTimer(self)
        self._holdings_auto_sync_timer.setInterval(30_000)
        self._holdings_auto_sync_timer.timeout.connect(self._auto_sync_holdings_tick)
        self._holdings_auto_sync_timer.start()
        self._nse_calendar_worker: NSECalendarRefreshWorker | None = None
        self._nse_calendar_refresh_timer = QTimer(self)
        self._nse_calendar_refresh_timer.setInterval(15 * 60_000)
        self._nse_calendar_refresh_timer.timeout.connect(self._refresh_nse_calendar)
        self._nse_calendar_refresh_timer.start()
        QTimer.singleShot(1_000, self._refresh_nse_calendar)
        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(1_000)
        self._clock_timer.timeout.connect(self._update_clock)
        self._clock_timer.start()
        self._build_ui()
        self._update_clock()
        self._load_setup(self.setup)
        self._loading = False
        self._refresh_templates()
        self._mark_stale(False)
        self._mark_history_stale(False)
        self._set_dirty(False)
        if self._store_error:
            QTimer.singleShot(
                0,
                lambda: QMessageBox.warning(
                    self,
                    "Template file preserved",
                    self._store_error
                    + "\n\nFix or move scanner_templates.json before saving templates.",
                ),
            )
        if self._watchlist_store_error:
            QTimer.singleShot(
                0,
                lambda: QMessageBox.warning(
                    self,
                    "Watchlist file preserved",
                    self._watchlist_store_error
                    + "\n\nFix or move watchlists.json before editing watchlists or syncing holdings.",
                ),
            )

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        title = QLabel("ChartSage Rule Scanner")
        title.setStyleSheet("font-size: 22px; font-weight: 700; color: #17365d")
        header_row = QHBoxLayout()
        header_row.addWidget(title)
        header_row.addStretch(1)
        outer.addLayout(header_row)
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setHandleWidth(10)
        self.main_splitter.setOpaqueResize(True)
        self.main_splitter.setChildrenCollapsible(True)
        self.main_splitter.setToolTip(
            "Drag the vertical divider left or right to resize the setup and results panes."
        )
        self.main_splitter.setStyleSheet(
            "QSplitter::handle:horizontal {"
            " background: #c4ced8; border-left: 1px solid #8796a5;"
            " border-right: 1px solid #8796a5; margin: 1px 2px; }"
            "QSplitter::handle:horizontal:hover, QSplitter::handle:horizontal:pressed {"
            " background: #2e75b6; border-color: #17365d; }"
        )
        outer.addWidget(self.main_splitter, 1)

        left = QWidget()
        left.setMinimumWidth(0)
        left.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        left_layout = QVBoxLayout(left)
        universe_box = QGroupBox("1. Universe (union, duplicates removed)")
        universe_layout = QVBoxLayout(universe_box)
        self.universe_list = QListWidget()
        self.universe_list.setMaximumHeight(170)
        self._sync_universe_list()
        self.custom_symbols = QLineEdit()
        self.custom_symbols.setPlaceholderText("Optional symbols separated by commas/spaces, e.g. RELIANCE TCS")
        self.manage_watchlists_btn = QPushButton("Manage Watchlists...")
        self.manage_watchlists_btn.clicked.connect(self._open_watchlist_manager)
        universe_layout.addWidget(self.universe_list)
        universe_layout.addWidget(self.custom_symbols)
        universe_layout.addWidget(self.manage_watchlists_btn)
        left_layout.addWidget(universe_box)

        rules_box = QGroupBox("2. Structured expression")
        rules_layout = QVBoxLayout(rules_box)

        popout_header = QHBoxLayout()
        popout_note = QLabel("Build and edit scanning criteria rules")
        popout_note.setStyleSheet("color: #666; font-size: 11px;")
        self.popout_editor_btn = QPushButton("Pop out editor 🗖")
        self.popout_editor_btn.setToolTip("Open expression editor in a separate large window for easy viewing of complex rule sets")
        self.popout_editor_btn.setStyleSheet("font-weight: 600;")
        self.popout_editor_btn.clicked.connect(self._open_expanded_expression_editor)
        popout_header.addWidget(popout_note, 1)
        popout_header.addWidget(self.popout_editor_btn)
        rules_layout.addLayout(popout_header)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Use", "Rule / group"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_tree_context_menu)
        self.tree.setStyleSheet("""
            QTreeView::branch:has-siblings:!adjoins-item {
                border-left: 1px dotted #a0a0a0;
            }
            QTreeView::branch:has-siblings:adjoins-item {
                border-left: 1px dotted #a0a0a0;
                border-bottom: 1px dotted #a0a0a0;
            }
            QTreeView::branch:!has-siblings:adjoins-item {
                border-left: 1px dotted #a0a0a0;
                border-bottom: 1px dotted #a0a0a0;
            }
            QTreeView::item {
                padding: 3px 2px;
            }
        """)
        self.tree.setToolTip(
            "Click an eye in the Use column to temporarily enable or disable a rule or group. Right-click any item for context menu."
        )
        rules_layout.addWidget(self.tree, 1)
        buttons = QHBoxLayout()
        add_rule_btn = QPushButton("Add rule")
        add_rule_btn.clicked.connect(self._add_rule)
        buttons.addWidget(add_rule_btn)

        add_group_btn = QToolButton()
        add_group_btn.setText("Add group ▾")
        add_group_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        group_menu = QMenu(self)
        for op, label in [
            ("AND", "AND (Pass ALL)"),
            ("OR", "OR (Pass ANY 1)"),
            ("ANY 2", "ANY 2 (Pass at least 2)"),
            ("ANY 3", "ANY 3 (Pass at least 3)"),
            ("ANY 4", "ANY 4 (Pass at least 4)"),
            ("ANY 5", "ANY 5 (Pass at least 5)"),
        ]:
            act = group_menu.addAction(f"Add {label}")
            act.triggered.connect(lambda _checked, o=op: self._add_group(o))
        add_group_btn.setMenu(group_menu)
        buttons.addWidget(add_group_btn)

        for text, slot in (
            ("Edit", self._edit_node),
            ("Duplicate", self._duplicate_node),
            ("Delete", self._delete_node),
            ("Up", lambda: self._move_node(-1)),
            ("Down", lambda: self._move_node(1)),
            ("Into", self._move_into),
            ("Out", self._move_out),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        rules_layout.addLayout(buttons)
        parser_row = QHBoxLayout()
        self.english_rule = QLineEdit()
        self.english_rule.setPlaceholderText(
            "One rule: Close within 2% above or below the 52 week high"
        )
        copy_draft_button = QPushButton("Copy filter to draft")
        copy_draft_button.setToolTip(
            "Copy the selected rule or group from the tree into this text field for editing."
        )
        copy_draft_button.clicked.connect(self._copy_to_draft)
        save_example_button = QPushButton("Save to Examples")
        save_example_button.setToolTip(
            "Save selected rule/group from the tree (or text in the rule box) to My Saved Snippets."
        )
        save_example_button.clicked.connect(self._save_rule_to_examples)
        examples_button = QPushButton("Filter Examples")
        examples_button.clicked.connect(self._show_filter_examples)
        explain_button = QPushButton("Explain Filter Logic")
        explain_button.clicked.connect(self._show_filter_logic)
        parse_button = QPushButton("Parse one rule")
        parse_button.clicked.connect(self._parse_rule)
        parser_row.addWidget(self.english_rule, 1)
        parser_row.addWidget(copy_draft_button)
        parser_row.addWidget(save_example_button)
        parser_row.addWidget(examples_button)
        parser_row.addWidget(explain_button)
        parser_row.addWidget(parse_button)
        rules_layout.addLayout(parser_row)
        draft_hint = QLabel(
            "English text is a draft until you click “Parse one rule” and confirm it. "
            "Templates save the structured tree above."
        )
        draft_hint.setWordWrap(True)
        draft_hint.setStyleSheet("color: #7f6000; font-size: 11px")
        rules_layout.addWidget(draft_hint)
        self.expression_label = QLabel()
        self.expression_label.setWordWrap(True)
        self.expression_label.setStyleSheet("color: #445; padding: 4px")
        rules_layout.addWidget(self.expression_label)
        left_layout.addWidget(rules_box, 1)

        scan_box = QGroupBox("3. Scan settings")
        scan_outer_layout = QVBoxLayout(scan_box)

        scan_header = QHBoxLayout()
        scan_header_note = QLabel("Configure scan behavior & alert settings")
        scan_header_note.setStyleSheet("color: #666; font-size: 11px;")
        self.copy_settings_all_btn = QPushButton("Copy settings to ALL templates 📋")
        self.copy_settings_all_btn.setToolTip(
            "Apply current Section 3 scan settings (refresh rate, Telegram alert modes, adjusted prices, incomplete bars, result columns, and universes) to all saved strategy templates."
        )
        self.copy_settings_all_btn.setStyleSheet("font-weight: 600;")
        self.copy_settings_all_btn.clicked.connect(self._copy_scan_settings_to_all_templates)
        self.open_strategy_guide_btn = QPushButton("↗ Open Strategy Guide Window")
        self.open_strategy_guide_btn.setToolTip(
            "Detach the Strategy Guide into its own maximized, resizable window."
        )
        self.open_strategy_guide_btn.setStyleSheet("font-weight: 700; color: #17365d;")
        self.open_strategy_guide_btn.clicked.connect(self._open_strategy_guide_window)
        scan_header.addWidget(scan_header_note, 1)
        scan_header.addWidget(self.open_strategy_guide_btn)
        scan_header.addWidget(self.copy_settings_all_btn)
        scan_outer_layout.addLayout(scan_header)

        self.scan_settings_tabs = QTabWidget()

        # TAB 1: Scan & Telegram Settings
        tab_scan_tg = QWidget()
        self.scan_telegram_settings_tab = tab_scan_tg
        tab_scan_tg_layout = QHBoxLayout(tab_scan_tg)
        tab_scan_tg_layout.setSpacing(15)

        col1_left = QVBoxLayout()
        asof_row = QHBoxLayout()
        self.latest = QRadioButton("Latest")
        self.historical = QRadioButton("Historical as-of")
        self.latest.setChecked(True)
        self.as_of_date = QDateEdit(QDate.currentDate())
        self.as_of_date.setCalendarPopup(True)
        self.as_of_date.setMaximumDate(QDate.currentDate())
        asof_row.addWidget(self.latest)
        asof_row.addWidget(self.historical)
        asof_row.addWidget(self.as_of_date)

        date_form = QFormLayout()
        date_form.addRow("Date", asof_row)
        col1_left.addLayout(date_form)

        self.adjusted = QCheckBox("Technical Adjusted prices (splits/bonus/consolidation)")
        self.adjusted.setChecked(True)
        self.adjusted.setToolTip(
            "Off uses immutable Official Raw NSE prices. On dynamically adjusts "
            "older OHLCV for validated NSE splits, bonuses and consolidations; "
            "rights, dividends, mergers and demergers remain unadjusted."
        )
        self.include_current = QCheckBox("Include current incomplete D/W/M bars")
        col1_left.addWidget(self.adjusted)
        col1_left.addWidget(self.include_current)

        self.auto_run = QCheckBox("Auto-run scan at selected interval")
        self.auto_run.setToolTip(
            "Runs a Latest scan immediately when enabled, then schedules the next "
            "scan after each completed scan. Scans never overlap."
        )
        window_row = QHBoxLayout()
        window_row.addWidget(QLabel("Auto-run window:"))
        self.auto_run_start_edit = QTimeEdit()
        self.auto_run_start_edit.setDisplayFormat("HH:mm")
        self.auto_run_start_edit.setToolTip("Daily auto-run/telegram start time (IST)")
        self.auto_run_end_edit = QTimeEdit()
        self.auto_run_end_edit.setDisplayFormat("HH:mm")
        self.auto_run_end_edit.setToolTip("Daily auto-run/telegram stop time (IST)")
        window_row.addWidget(self.auto_run_start_edit)
        window_row.addWidget(QLabel("to"))
        window_row.addWidget(self.auto_run_end_edit)
        self.auto_run_trading_days_only = QCheckBox("Trading days only")
        self.auto_run_trading_days_only.setToolTip(
            "When checked, auto-run skips weekends and NSE holidays. "
            "Uncheck to scan/backtest on holidays too."
        )
        window_row.addWidget(self.auto_run_trading_days_only)
        window_row.addStretch(1)
        self.auto_run_window_hint = QLabel("")
        self.auto_run_window_hint.setStyleSheet("font-size: 10px; color: #9c5700")
        col1_left.addWidget(self.auto_run)
        col1_left.addLayout(window_row)
        col1_left.addWidget(self.auto_run_window_hint)

        tab_scan_tg_layout.addLayout(col1_left, 1)

        col1_right = QVBoxLayout()
        self.telegram_alerts = QCheckBox(
            "Send Telegram alerts after successful scans"
        )
        self.telegram_alerts.setToolTip(
            "Telegram alerts are configured and saved separately for each template."
        )
        self.telegram_auto_reset_daily = QCheckBox(
            "Auto-reset baseline every morning"
        )
        self.telegram_auto_reset_daily.setToolTip(
            "When enabled, baseline resets after midnight IST."
        )
        col1_right.addWidget(self.telegram_alerts)
        col1_right.addWidget(self.telegram_auto_reset_daily)

        telegram_row = QHBoxLayout()
        self.telegram_alert_mode = QComboBox()
        for mode, label in TELEGRAM_ALERT_MODES:
            self.telegram_alert_mode.addItem(label, mode)
        self.telegram_settings_button = QPushButton("Telegram Settings...")
        self.telegram_settings_button.clicked.connect(
            self._open_telegram_settings
        )
        self.telegram_reset_button = QPushButton("Reset alert baseline")
        self.telegram_reset_button.setToolTip(
            "Forget the last alerted result set."
        )
        self.telegram_reset_button.clicked.connect(
            self._reset_telegram_baseline
        )
        telegram_row.addWidget(self.telegram_alert_mode, 1)
        telegram_row.addWidget(self.telegram_settings_button)
        telegram_row.addWidget(self.telegram_reset_button)

        tg_form = QFormLayout()
        tg_form.addRow("Alert mode", telegram_row)
        col1_right.addLayout(tg_form)

        self.ranking_box = QGroupBox("Composite RSI Momentum Ranking")
        self.ranking_box.setMinimumHeight(145)
        self.ranking_box.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
        )
        ranking_grid = QGridLayout(self.ranking_box)
        ranking_grid.setContentsMargins(8, 8, 8, 8)
        ranking_grid.setHorizontalSpacing(8)
        ranking_grid.setVerticalSpacing(4)
        ranking_grid.setColumnStretch(1, 1)
        ranking_grid.setColumnStretch(3, 2)
        self.ranking_enabled_cb = QCheckBox(
            "Rank qualifying stocks by average RSI momentum"
        )
        ranking_grid.addWidget(self.ranking_enabled_cb, 0, 0, 1, 4)
        ranking_grid.addWidget(QLabel("RSI periods:"), 1, 0)
        self.ranking_rsi_periods_edit = QLineEdit("22, 44, 66")
        self.ranking_rsi_periods_edit.setPlaceholderText("e.g. 22, 44, 66")
        self.ranking_rsi_periods_edit.setToolTip(
            "Comma-separated Daily RSI periods from 1 to 500. The score is their equal-weight average."
        )
        ranking_grid.addWidget(self.ranking_rsi_periods_edit, 1, 1)
        ranking_grid.addWidget(QLabel("Order:"), 1, 2)
        self.ranking_direction_combo = QComboBox()
        self.ranking_direction_combo.addItem("Highest momentum first", "DESC")
        self.ranking_direction_combo.addItem("Lowest momentum first", "ASC")
        ranking_grid.addWidget(self.ranking_direction_combo, 1, 3)
        ranking_grid.addWidget(QLabel("Top N:"), 2, 0)
        self.ranking_top_n_spin = QSpinBox()
        self.ranking_top_n_spin.setRange(0, 10000)
        self.ranking_top_n_spin.setSpecialValueText("All")
        self.ranking_top_n_spin.setToolTip("0 keeps every qualifying stock; otherwise retain only the top N.")
        ranking_grid.addWidget(self.ranking_top_n_spin, 2, 1)
        self.ranking_watchlist_cb = QCheckBox("Create/update ranked watchlist:")
        ranking_grid.addWidget(self.ranking_watchlist_cb, 2, 2)
        self.ranking_watchlist_name_edit = QLineEdit()
        self.ranking_watchlist_name_edit.setPlaceholderText("e.g. RSI MOMENTUM LEADERS")
        ranking_grid.addWidget(self.ranking_watchlist_name_edit, 2, 3)
        self.ranking_formula_lbl = QLabel()
        self.ranking_formula_lbl.setWordWrap(True)
        self.ranking_formula_lbl.setStyleSheet("color: #5d4037; font-size: 10px;")
        ranking_grid.addWidget(self.ranking_formula_lbl, 3, 0, 1, 4)

        # Keep the ALGO strategy save action on the first settings tab, where
        # users edit the scan and rule-facing settings most often. It invokes
        # the same complete save routine as before, including the execution
        # controls that remain on the second tab.
        save_algo_btn_row = QHBoxLayout()
        self.save_algo_settings_btn = QPushButton("SAVE ALGO STRATEGY 💾")
        self.save_algo_settings_btn.setMinimumSize(220, 36)
        self.save_algo_settings_btn.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self.save_algo_settings_btn.setStyleSheet(
            "font-weight: 700; background-color: #17365d; color: white; "
            "padding: 4px 16px; border-radius: 4px;"
        )
        self.save_algo_settings_btn.setToolTip(
            "Save the complete active ALGO strategy, including scan, rule, "
            "Telegram and order-execution settings."
        )
        self.save_algo_settings_btn.clicked.connect(
            self._save_algo_execution_settings
        )
        save_algo_btn_row.addWidget(self.save_algo_settings_btn)
        save_algo_btn_row.addStretch(1)
        col1_left.addLayout(save_algo_btn_row)
        col1_left.addStretch(1)

        self.result_columns_list = QListWidget()
        self.result_columns_list.setMaximumHeight(85)
        self._refresh_result_columns(set(ALL_RESULT_COLUMNS), dynamic_saved=False)
        result_columns_box = QWidget()
        result_columns_layout = QVBoxLayout(result_columns_box)
        result_columns_layout.setContentsMargins(0, 0, 0, 0)
        result_columns_layout.addWidget(self.result_columns_list)
        result_columns_actions = QHBoxLayout()
        result_columns_actions.addStretch(1)
        self.reset_column_order_button = QPushButton("Reset column order")
        self.reset_column_order_button.setToolTip(
            "Restore the standard result-column layout for this template."
        )
        self.reset_column_order_button.clicked.connect(self._reset_column_order)
        result_columns_actions.addWidget(self.reset_column_order_button)
        result_columns_layout.addLayout(result_columns_actions)

        cols_form = QFormLayout()
        cols_form.addRow("Result columns", result_columns_box)
        col1_right.addLayout(cols_form)
        col1_right.addStretch(1)

        tab_scan_tg_layout.addLayout(col1_right, 1)

        self.scan_settings_tabs.addTab(tab_scan_tg, "⚙️ Scan & Telegram Settings")

        # TAB 2: ALGO Order Placement Settings (Scrollable & 2-Column Layout)
        tab_algo = QWidget()
        self.algo_order_settings_tab = tab_algo
        tab_algo_main_layout = QVBoxLayout(tab_algo)
        tab_algo_main_layout.setContentsMargins(0, 0, 0, 0)

        tab_algo_scroll = QScrollArea()
        tab_algo_scroll.setWidgetResizable(True)
        tab_algo_scroll.setFrameShape(QFrame.Shape.NoFrame)

        tab_algo_content = QWidget()
        tab_algo_layout = QVBoxLayout(tab_algo_content)
        tab_algo_layout.setSpacing(8)
        tab_algo_layout.setContentsMargins(8, 8, 8, 8)

        self.algo_auto_order_cb = QCheckBox(
            "Simulation mode — paper trades (no real orders)"
        )
        self.algo_auto_order_cb.setToolTip(
            "When checked, every scan that matches signals is simulated: matched BUY signals open paper "
            "positions and matched SELL signals close them using the scan's latest prices. No real orders "
            "are ever placed. This is saved per template and remembered across app launches."
        )
        self.algo_auto_order_cb.setChecked(False)
        self.algo_auto_order_cb.setStyleSheet("font-size: 13px; font-weight: 600; margin-bottom: 2px;")

        self._paper_confirm_shown = False
        self.algo_auto_order_cb.stateChanged.connect(self._on_algo_auto_order_toggled)
        tab_algo_layout.addWidget(self.algo_auto_order_cb)

        paper_account_row = QHBoxLayout()
        paper_account_row.setSpacing(8)
        paper_account_row.addWidget(QLabel("📒 Simulation Account:"))
        self.algo_paper_account_combo = QComboBox()
        self.algo_paper_account_combo.setMinimumHeight(24)
        self.algo_paper_account_combo.setMinimumWidth(220)
        self.algo_paper_account_combo.setToolTip(
            "The virtual paper account this template simulates into. "
            "Fully separate from your real Zerodha accounts and holdings."
        )
        self.algo_paper_account_combo.currentIndexChanged.connect(
            lambda _idx: self._on_algo_paper_account_changed()
        )
        paper_account_row.addWidget(self.algo_paper_account_combo)
        self.paper_accounts_btn = QPushButton("✏️ Manage…")
        self.paper_accounts_btn.setToolTip(
            "Create, edit or delete virtual paper accounts and set defaults for new ones."
        )
        self.paper_accounts_btn.clicked.connect(self._open_paper_settings)
        paper_account_row.addWidget(self.paper_accounts_btn)
        paper_account_row.addStretch(1)
        tab_algo_layout.addLayout(paper_account_row)

        paper_btn_row = QHBoxLayout()
        paper_btn_row.setSpacing(8)
        self.open_paper_trades_btn = QPushButton("📒 Paper Trades")
        self.open_paper_trades_btn.setStyleSheet(
            "font-weight: 700; background-color: #2e7d32; color: white; padding: 5px 12px; border-radius: 3px;"
        )
        self.open_paper_trades_btn.setToolTip(
            "Open the paper trading ledger: open positions, trade history and per-account P&L. "
            "Paper trades are fully simulated and never reach your Zerodha account."
        )
        self.open_paper_trades_btn.clicked.connect(self._open_paper_trades_dialog)
        paper_btn_row.addWidget(self.open_paper_trades_btn)
        self.paper_settings_btn = QPushButton("⚙️ Paper Settings…")
        self.paper_settings_btn.setStyleSheet(
            "font-weight: 600; background-color: #17365d; color: white; padding: 5px 12px; border-radius: 3px;"
        )
        self.paper_settings_btn.setToolTip(
            "Edit the paper trading defaults: starting cash for new accounts and the broker fee."
        )
        self.paper_settings_btn.clicked.connect(self._open_paper_settings)
        paper_btn_row.addWidget(self.paper_settings_btn)
        paper_btn_row.addWidget(
            QLabel("Simulated trades only — no orders reach Zerodha.")
        )
        paper_btn_row.addStretch(1)
        tab_algo_layout.addLayout(paper_btn_row)

        # 2-Column Side-by-Side Grid Layout
        algo_columns_layout = QHBoxLayout()
        algo_columns_layout.setSpacing(10)

        # LEFT COLUMN: Account Funds & Capital Allocation GroupBox
        funds_box = QGroupBox("💰 Account Funds && Capital Allocation")
        funds_box.setStyleSheet(
            "QGroupBox { font-weight: bold; font-size: 12px; border: 1px solid #cccccc; border-radius: 6px; margin-top: 6px; padding-top: 10px; background-color: #fafafa; } "
            "QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; left: 10px; padding: 0 4px; background-color: #fafafa; color: #1a237e; }"
        )
        funds_box_layout = QVBoxLayout(funds_box)
        funds_box_layout.setContentsMargins(10, 10, 10, 10)
        funds_box_layout.setSpacing(6)

        funds_hdr = QHBoxLayout()
        funds_hdr.setSpacing(6)
        self.funds_available_label = QLabel("FUNDS AVAILABLE: <b>₹ --</b>")
        self.funds_available_label.setStyleSheet("font-size: 12px; color: #1565c0; font-weight: 600; min-height: 22px;")
        funds_hdr.addWidget(self.funds_available_label, 1)

        self.refresh_funds_btn = QPushButton("↻ Refresh Funds")
        self.refresh_funds_btn.setStyleSheet("font-size: 11px; padding: 2px 8px; min-height: 24px;")
        self.refresh_funds_btn.setToolTip("Fetch live available cash margin from your Zerodha Kite account.")
        self.refresh_funds_btn.clicked.connect(self._fetch_live_zerodha_funds)
        funds_hdr.addWidget(self.refresh_funds_btn)
        funds_box_layout.addLayout(funds_hdr)

        alloc_form = QFormLayout()
        alloc_form.setVerticalSpacing(6)
        alloc_form.setHorizontalSpacing(10)

        self.algo_target_account_combo = QComboBox()
        self.algo_target_account_combo.setMinimumHeight(24)
        self._populate_algo_target_accounts()
        self.algo_target_account_combo.setToolTip("Select the specific Zerodha account that this ALGO template will trade on and update holdings/watchlists for.")
        self.algo_target_account_combo.currentIndexChanged.connect(self._on_algo_target_account_changed)
        alloc_form.addRow("Trading Account:", self.algo_target_account_combo)

        self.algo_capital_mode_combo = QComboBox()
        self.algo_capital_mode_combo.setMinimumHeight(24)
        self.algo_capital_mode_combo.addItems([
            "Dynamic Allocation (Live Cash ÷ Max Position Slots)",
            "Fixed Capital per Stock (Manual Ceiling)"
        ])
        self.algo_capital_mode_combo.setToolTip("Select whether to auto-divide live Zerodha account cash across slots or use a manual fixed capital ceiling per stock.")
        self.algo_capital_mode_combo.currentIndexChanged.connect(lambda _idx: self._update_effective_capital_per_stock())
        alloc_form.addRow("Capital Mode:", self.algo_capital_mode_combo)

        self.algo_max_slots_spin = QSpinBox()
        self.algo_max_slots_spin.setMinimumHeight(24)
        self.algo_max_slots_spin.setRange(1, 100)
        self.algo_max_slots_spin.setValue(5)
        self.algo_max_slots_spin.setToolTip("Maximum number of open stock position slots to divide total account cash across.")
        self.algo_max_slots_spin.valueChanged.connect(lambda _val: self._update_effective_capital_per_stock())
        alloc_form.addRow("Max Slots:", self.algo_max_slots_spin)

        self.algo_capital_spin = QDoubleSpinBox()
        self.algo_capital_spin.setMinimumHeight(24)
        self.algo_capital_spin.setRange(100.0, 10000000.0)
        self.algo_capital_spin.setSingleStep(1000.0)
        self.algo_capital_spin.setValue(5000.0)
        self.algo_capital_spin.setPrefix("₹ ")
        self.algo_capital_spin.valueChanged.connect(lambda _val: self._update_effective_capital_per_stock())
        alloc_form.addRow("Fixed Cap (₹):", self.algo_capital_spin)

        self.algo_calculated_capital_lbl = QLabel("Allocated/Stock: <b>₹ 0.00</b>")
        self.algo_calculated_capital_lbl.setStyleSheet("font-size: 11px; font-weight: bold; color: #2e7d32; min-height: 20px;")
        alloc_form.addRow("Capital/Stock:", self.algo_calculated_capital_lbl)

        funds_box_layout.addLayout(alloc_form)

        self.algo_account_scope_banner = QLabel()
        self.algo_account_scope_banner.setWordWrap(True)
        self.algo_account_scope_banner.setStyleSheet(
            "background-color: #e8f5e9; color: #1b5e20; border: 1px solid #c8e6c9; border-radius: 4px; padding: 6px; font-size: 11px;"
        )
        funds_box_layout.addWidget(self.algo_account_scope_banner)
        self._on_algo_target_account_changed()
        algo_columns_layout.addWidget(funds_box, 1)

        # RIGHT COLUMN: Order Action & Type Rules GroupBox
        order_box = QGroupBox("⚙️ Trade Rules && Order Types")
        order_box.setStyleSheet(
            "QGroupBox { font-weight: bold; font-size: 12px; border: 1px solid #cccccc; border-radius: 6px; margin-top: 6px; padding-top: 10px; background-color: #fafafa; } "
            "QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; left: 10px; padding: 0 4px; background-color: #fafafa; color: #1a237e; }"
        )
        order_box_layout = QVBoxLayout(order_box)
        order_box_layout.setContentsMargins(10, 10, 10, 10)
        order_box_layout.setSpacing(6)

        algo_form = QFormLayout()
        algo_form.setVerticalSpacing(6)
        algo_form.setHorizontalSpacing(10)

        self.algo_action_combo = QComboBox()
        self.algo_action_combo.setMinimumHeight(24)
        self.algo_action_combo.addItems([
            "BUY (Entry Order - Budget Based)",
            "SELL (Exit Order - Portfolio Holdings Guard)"
        ])
        self.algo_action_combo.setToolTip(
            "Select BUY for entry scans or SELL for exit scans. SELL automatically uses your exact portfolio holding quantity."
        )
        algo_form.addRow("Trade Action:", self.algo_action_combo)

        self.algo_sell_mode_combo = QComboBox()
        self.algo_sell_mode_combo.setMinimumHeight(24)
        self.algo_sell_mode_combo.addItems([
            "100% (Sell Full Holding Quantity)",
            "Custom % of Holding Quantity"
        ])
        self.algo_sell_mode_combo.setToolTip("Choose whether to exit 100% of holdings or a partial percentage (e.g. 50% for partial profit taking).")
        algo_form.addRow("Exit Mode:", self.algo_sell_mode_combo)

        self.algo_sell_pct_spin = QDoubleSpinBox()
        self.algo_sell_pct_spin.setMinimumHeight(24)
        self.algo_sell_pct_spin.setRange(1.0, 100.0)
        self.algo_sell_pct_spin.setSingleStep(5.0)
        self.algo_sell_pct_spin.setValue(50.0)
        self.algo_sell_pct_spin.setSuffix("%")
        self.algo_sell_pct_spin.setEnabled(False)
        self.algo_sell_pct_spin.setToolTip("Percentage of your portfolio holding quantity to sell during an exit scan.")
        self.algo_sell_mode_combo.currentIndexChanged.connect(
            lambda idx: self.algo_sell_pct_spin.setEnabled(self.algo_action_combo.currentIndex() == 1 and idx == 1)
        )
        algo_form.addRow("Exit Qty %:", self.algo_sell_pct_spin)

        def _update_algo_action_ui(idx: int) -> None:
            is_sell = (idx == 1)
            self.algo_capital_mode_combo.setEnabled(not is_sell)
            self.algo_max_slots_spin.setEnabled(not is_sell)
            self.algo_capital_spin.setEnabled(not is_sell)
            self.algo_sell_mode_combo.setEnabled(is_sell)
            self.algo_sell_pct_spin.setEnabled(is_sell and self.algo_sell_mode_combo.currentIndex() == 1)
            if is_sell:
                self.algo_calculated_capital_lbl.setText("Allocated/Stock: <b>N/A (SELL order exits holding qty)</b>")
            else:
                self._update_effective_capital_per_stock()

        self.algo_action_combo.currentIndexChanged.connect(_update_algo_action_ui)
        self._update_algo_action_ui_fn = _update_algo_action_ui

        self.algo_product_combo = QComboBox()
        self.algo_product_combo.setMinimumHeight(24)
        self.algo_product_combo.addItems(["CNC (Delivery)", "MIS (Intraday)"])
        algo_form.addRow("Product Type:", self.algo_product_combo)

        self.algo_order_type_combo = QComboBox()
        self.algo_order_type_combo.setMinimumHeight(24)
        self.algo_order_type_combo.addItems(["MARKET", "LIMIT"])
        algo_form.addRow("Order Type:", self.algo_order_type_combo)

        self.algo_limit_pct_spin = QDoubleSpinBox()
        self.algo_limit_pct_spin.setMinimumHeight(24)
        self.algo_limit_pct_spin.setRange(0.0, 20.0)
        self.algo_limit_pct_spin.setSingleStep(0.25)
        self.algo_limit_pct_spin.setValue(0.50)
        self.algo_limit_pct_spin.setSuffix("% below LTP")
        self.algo_limit_pct_spin.setEnabled(False)
        self.algo_limit_pct_spin.setToolTip("Percentage below stock LTP to set the LIMIT order buy price (e.g. 0.5% below LTP). Set 0.0% to buy at exact current price.")
        algo_form.addRow("Limit Price Discount:", self.algo_limit_pct_spin)

        def _update_order_type_ui(order_type: str) -> None:
            self.algo_limit_pct_spin.setEnabled(order_type == "LIMIT")

        self.algo_order_type_combo.currentTextChanged.connect(_update_order_type_ui)

        order_box_layout.addLayout(algo_form)
        algo_columns_layout.addWidget(order_box, 1)

        tab_algo_layout.addLayout(algo_columns_layout)

        tab_algo_layout.addStretch(1)

        tab_algo_scroll.setWidget(tab_algo_content)
        tab_algo_main_layout.addWidget(tab_algo_scroll)

        self.scan_settings_tabs.addTab(tab_algo, "🤖 ALGO Order Execution Settings")

        # TAB 3: Strategy Guide Workspace
        tab_paper = QWidget()
        self.paper_trading_tab = tab_paper
        tab_paper_main_layout = QVBoxLayout(tab_paper)
        tab_paper_main_layout.setContentsMargins(0, 0, 0, 0)

        tab_paper_scroll = QScrollArea()
        tab_paper_scroll.setWidgetResizable(True)
        tab_paper_scroll.setFrameShape(QFrame.Shape.NoFrame)

        tab_paper_content = QWidget()
        tab_paper_layout = QVBoxLayout(tab_paper_content)
        tab_paper_layout.setSpacing(8)
        tab_paper_layout.setContentsMargins(8, 8, 8, 8)

        paper_columns_layout = QHBoxLayout()
        paper_columns_layout.setSpacing(10)

        # LEFT: guide configuration
        paper_cfg_box = QGroupBox("🧭 Strategy Guide")
        paper_cfg_box.setStyleSheet(
            "QGroupBox { font-weight: bold; font-size: 12px; border: 1px solid #cccccc; "
            "border-radius: 6px; margin-top: 6px; padding-top: 10px; background-color: #fafafa; } "
            "QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; left: 10px; "
            "padding: 0 4px; background-color: #fafafa; color: #1a237e; }"
        )
        paper_cfg_layout = QVBoxLayout(paper_cfg_box)
        paper_cfg_layout.setContentsMargins(10, 10, 10, 10)
        paper_cfg_layout.setSpacing(6)

        account_row = QHBoxLayout()
        account_row.setSpacing(8)
        account_row.addWidget(QLabel("📒 Model Portfolio:"))
        self.paper_pipeline_account_combo = QComboBox()
        self.paper_pipeline_account_combo.setMinimumWidth(220)
        self.paper_pipeline_account_combo.setToolTip(
            "Optional independent model portfolio used to record simulated BUY/EXIT actions. "
            "It is fully separate from your real Zerodha accounts."
        )
        self._populate_paper_pipeline_account_combo()
        account_row.addWidget(self.paper_pipeline_account_combo, 1)
        self.paper_pipeline_manage_btn = QPushButton("✏️ Manage…")
        self.paper_pipeline_manage_btn.clicked.connect(self._open_paper_settings)
        account_row.addWidget(self.paper_pipeline_manage_btn)
        self.paper_pipeline_trades_btn = QPushButton("📒 Paper Trades")
        self.paper_pipeline_trades_btn.setToolTip("Open the paper ledger for the selected account.")
        self.paper_pipeline_trades_btn.clicked.connect(self._open_paper_trades_dialog)
        account_row.addWidget(self.paper_pipeline_trades_btn)
        paper_cfg_layout.addLayout(account_row)

        self.guide_track_model_cb = QCheckBox(
            "Track suggestions in the independent model portfolio (simulated trades only)"
        )
        self.guide_track_model_cb.setChecked(False)
        self.guide_track_model_cb.setToolTip(
            "When enabled, qualifying suggestions are also recorded in the selected model portfolio. "
            "This never places a Zerodha order."
        )
        self.guide_track_model_cb.toggled.connect(
            lambda enabled: (
                self.paper_pipeline_account_combo.setEnabled(enabled),
                self.paper_pipeline_manage_btn.setEnabled(enabled),
                self.paper_pipeline_trades_btn.setEnabled(enabled),
            )
        )
        self.paper_pipeline_account_combo.setEnabled(False)
        self.paper_pipeline_manage_btn.setEnabled(False)
        self.paper_pipeline_trades_btn.setEnabled(False)
        paper_cfg_layout.addWidget(self.guide_track_model_cb)

        guide_notice = QLabel(
            "Zerodha holdings are used only as a read-only overlay/watchlist. "
            "BUY and EXIT guidance is generated independently of what you currently hold."
        )
        guide_notice.setWordWrap(True)
        guide_notice.setStyleSheet(
            "background-color: #e3f2fd; color: #0d47a1; border: 1px solid #90caf9; "
            "border-radius: 4px; padding: 6px; font-size: 11px;"
        )
        paper_cfg_layout.addWidget(guide_notice)

        source_header = QHBoxLayout()
        source_header.addWidget(
            QLabel("Step 1A — Choose source watchlists/universes (optional override):"),
            1,
        )
        self.guide_source_count_lbl = QLabel("Using template sources")
        self.guide_source_count_lbl.setStyleSheet("color: #555; font-weight: 600;")
        source_header.addWidget(self.guide_source_count_lbl)
        self.guide_source_refresh_btn = QPushButton("↻ Refresh")
        self.guide_source_refresh_btn.setToolTip(
            "Reload built-in universes and every watchlist currently stored in Manage Watchlists."
        )
        self.guide_source_refresh_btn.clicked.connect(
            self._populate_guide_source_list
        )
        source_header.addWidget(self.guide_source_refresh_btn)
        paper_cfg_layout.addLayout(source_header)

        self.guide_source_list = QListWidget()
        self.guide_source_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.guide_source_list.setMaximumHeight(125)
        self.guide_source_list.setToolTip(
            "Check one or more watchlists/universes to scan their combined symbols. "
            "Leave all unchecked to use each candidate template's saved universe."
        )
        self._populate_guide_source_list()
        self.guide_source_list.itemChanged.connect(
            self._update_strategy_guide_summary
        )
        paper_cfg_layout.addWidget(self.guide_source_list)

        source_hint = QLabel(
            "No source checked = use each candidate template's saved universe. "
            "Checked sources temporarily override the templates for this Guide run; "
            "the saved templates are not changed."
        )
        source_hint.setWordWrap(True)
        source_hint.setStyleSheet("color: #555; font-size: 10px;")
        paper_cfg_layout.addWidget(source_hint)

        step1_header = QHBoxLayout()
        step1_header.addWidget(
            QLabel("Step 1B — Choose candidate BUY rule templates:"), 1
        )
        self.paper_member_count_lbl = QLabel("0 selected")
        self.paper_member_count_lbl.setStyleSheet("color: #555; font-weight: 600;")
        step1_header.addWidget(self.paper_member_count_lbl)
        self.paper_member_role_btn = QPushButton("Change highlighted role…")
        self.paper_member_role_btn.setToolTip(
            "Correct an automatically classified template by marking it as Candidate/BUY or EXIT/Weakness."
        )
        self.paper_member_role_btn.clicked.connect(
            lambda: self._change_guide_template_role(self.paper_member_list)
        )
        step1_header.addWidget(self.paper_member_role_btn)
        paper_cfg_layout.addLayout(step1_header)
        self.paper_member_list = QListWidget()
        self.paper_member_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.paper_member_list.setMaximumHeight(150)
        self._populate_paper_member_list()
        self.paper_member_list.itemChanged.connect(self._update_strategy_guide_summary)
        paper_cfg_layout.addWidget(self.paper_member_list)

        buy_mode_row = QHBoxLayout()
        buy_mode_row.addWidget(QLabel("Step 2 — Confirm BUY when:"))
        self.paper_buy_mode_combo = QComboBox()
        self.paper_buy_mode_combo.addItem("ALL checked candidate rules match (AND)", "AND")
        self.paper_buy_mode_combo.addItem("ANY checked candidate rule matches (OR)", "OR")
        self.paper_buy_mode_combo.addItem("At least K checked candidate rules match", "ANY K")
        self.paper_buy_mode_combo.addItem("Use a saved BUY Final Rule Set (advanced)", "SAVED")
        self.paper_buy_mode_combo.setCurrentIndex(0)
        buy_mode_row.addWidget(self.paper_buy_mode_combo, 1)
        buy_mode_row.addWidget(QLabel("K:"))
        self.paper_buy_k_spin = QSpinBox()
        self.paper_buy_k_spin.setRange(1, 1)
        self.paper_buy_k_spin.setEnabled(False)
        buy_mode_row.addWidget(self.paper_buy_k_spin)
        paper_cfg_layout.addLayout(buy_mode_row)

        fs_row = QHBoxLayout()
        fs_row.setSpacing(8)
        self.paper_saved_buy_lbl = QLabel("Saved BUY set:")
        fs_row.addWidget(self.paper_saved_buy_lbl)
        self.paper_finalset_combo = QComboBox()
        self.paper_finalset_combo.setMinimumWidth(220)
        self.paper_finalset_combo.setToolTip(
            "The FINAL RULE SET combines member rule templates by name (AND / OR / ANY K). "
            "It is evaluated over the union of the member PAPER watchlists."
        )
        self._populate_paper_finalset_combo()
        fs_row.addWidget(self.paper_finalset_combo, 1)
        self.build_finalset_btn = QPushButton("Build FINAL RULE SET…")
        self.build_finalset_btn.setToolTip("Create or edit a FINAL RULE SET from existing rule templates.")
        self.build_finalset_btn.clicked.connect(
            lambda: self._open_build_finalset_dialog("BUY")
        )
        fs_row.addWidget(self.build_finalset_btn)
        self.edit_finalset_btn = QPushButton("Edit…")
        self.edit_finalset_btn.setToolTip("Modify the selected FINAL RULE SET: rename it or change its members / AND / OR / ANY K.")
        self.edit_finalset_btn.clicked.connect(
            lambda: self._open_edit_finalset_dialog(self.paper_finalset_combo, "BUY")
        )
        fs_row.addWidget(self.edit_finalset_btn)
        self.delete_finalset_btn = QPushButton("Delete")
        self.delete_finalset_btn.setToolTip("Delete the selected FINAL RULE SET.")
        self.delete_finalset_btn.clicked.connect(
            lambda: self._delete_paper_finalset(self.paper_finalset_combo)
        )
        fs_row.addWidget(self.delete_finalset_btn)
        self.paper_finalset_combo.currentIndexChanged.connect(self._update_paper_finalset_buttons)
        self._update_paper_finalset_buttons()
        paper_cfg_layout.addLayout(fs_row)

        exit_mode_row = QHBoxLayout()
        exit_mode_row.addWidget(QLabel("Step 3 — Suggest EXIT / WEAKNESS when:"))
        self.paper_exit_mode_combo = QComboBox()
        self.paper_exit_mode_combo.addItem("ANY checked exit rule matches (OR)", "OR")
        self.paper_exit_mode_combo.addItem("ALL checked exit rules match (AND)", "AND")
        self.paper_exit_mode_combo.addItem("At least K checked exit rules match", "ANY K")
        self.paper_exit_mode_combo.addItem("Use a saved EXIT Final Rule Set (advanced)", "SAVED")
        exit_mode_row.addWidget(self.paper_exit_mode_combo, 1)
        exit_mode_row.addWidget(QLabel("K:"))
        self.paper_exit_k_spin = QSpinBox()
        self.paper_exit_k_spin.setRange(1, 1)
        self.paper_exit_k_spin.setEnabled(False)
        exit_mode_row.addWidget(self.paper_exit_k_spin)
        paper_cfg_layout.addLayout(exit_mode_row)

        exit_fs_row = QHBoxLayout()
        exit_fs_row.setSpacing(8)
        self.paper_saved_exit_lbl = QLabel("Saved EXIT set:")
        exit_fs_row.addWidget(self.paper_saved_exit_lbl)
        self.paper_exit_finalset_combo = QComboBox()
        self.paper_exit_finalset_combo.setMinimumWidth(220)
        self.paper_exit_finalset_combo.setToolTip(
            "The EXIT FINAL RULE SET combines exit rule templates by name (AND / OR / ANY K). "
            "It is evaluated against the complete Step 1 candidate universe, independently of holdings. "
            "Leave '— None —' to use the exit templates listed below (any match produces guidance)."
        )
        self._populate_paper_exit_finalset_combo()
        exit_fs_row.addWidget(self.paper_exit_finalset_combo, 1)
        self.build_exit_finalset_btn = QPushButton("Build EXIT SET…")
        self.build_exit_finalset_btn.setToolTip("Create an EXIT FINAL RULE SET from existing rule templates.")
        self.build_exit_finalset_btn.clicked.connect(
            lambda: self._open_build_finalset_dialog("EXIT")
        )
        exit_fs_row.addWidget(self.build_exit_finalset_btn)
        self.edit_exit_finalset_btn = QPushButton("Edit…")
        self.edit_exit_finalset_btn.setToolTip("Modify the selected EXIT FINAL RULE SET.")
        self.edit_exit_finalset_btn.clicked.connect(
            lambda: self._open_edit_finalset_dialog(self.paper_exit_finalset_combo, "EXIT")
        )
        exit_fs_row.addWidget(self.edit_exit_finalset_btn)
        self.delete_exit_finalset_btn = QPushButton("Delete")
        self.delete_exit_finalset_btn.setToolTip("Delete the selected EXIT FINAL RULE SET.")
        self.delete_exit_finalset_btn.clicked.connect(
            lambda: self._delete_paper_finalset(self.paper_exit_finalset_combo)
        )
        exit_fs_row.addWidget(self.delete_exit_finalset_btn)
        self.paper_exit_finalset_combo.currentIndexChanged.connect(self._update_paper_exit_finalset_buttons)
        self._update_paper_exit_finalset_buttons()
        paper_cfg_layout.addLayout(exit_fs_row)

        exit_list_header = QHBoxLayout()
        exit_list_header.addWidget(QLabel("EXIT/weakness templates:"), 1)
        self.paper_exit_count_lbl = QLabel("0 selected")
        self.paper_exit_count_lbl.setStyleSheet("color: #555; font-weight: 600;")
        exit_list_header.addWidget(self.paper_exit_count_lbl)
        self.paper_exit_role_btn = QPushButton("Change highlighted role…")
        self.paper_exit_role_btn.clicked.connect(
            lambda: self._change_guide_template_role(self.paper_exit_list)
        )
        exit_list_header.addWidget(self.paper_exit_role_btn)
        paper_cfg_layout.addLayout(exit_list_header)
        self.paper_exit_list = QListWidget()
        self.paper_exit_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.paper_exit_list.setMaximumHeight(120)
        self._populate_paper_exit_list()
        self.paper_exit_list.itemChanged.connect(self._update_strategy_guide_summary)
        paper_cfg_layout.addWidget(self.paper_exit_list)

        self.paper_buy_mode_combo.currentIndexChanged.connect(self._update_guide_controls)
        self.paper_exit_mode_combo.currentIndexChanged.connect(self._update_guide_controls)
        self.paper_buy_k_spin.valueChanged.connect(self._update_strategy_guide_summary)
        self.paper_exit_k_spin.valueChanged.connect(self._update_strategy_guide_summary)

        self.strategy_guide_summary = QLabel()
        self.strategy_guide_summary.setWordWrap(True)
        self.strategy_guide_summary.setStyleSheet(
            "background-color: #fff8e1; color: #5d4037; border: 1px solid #ffe082; "
            "border-radius: 4px; padding: 7px; font-size: 11px;"
        )
        paper_cfg_layout.addWidget(self.strategy_guide_summary)

        alloc_form = QFormLayout()
        alloc_form.setVerticalSpacing(5)
        alloc_form.setHorizontalSpacing(10)
        self.paper_capital_mode_combo = QComboBox()
        self.paper_capital_mode_combo.addItems([
            "Dynamic Allocation (Model Cash ÷ Available Position Slots)",
            "Fixed Capital per Stock (Manual Ceiling)",
        ])
        alloc_form.addRow("Capital Mode:", self.paper_capital_mode_combo)
        self.paper_max_slots_spin = QSpinBox()
        self.paper_max_slots_spin.setRange(1, 100)
        self.paper_max_slots_spin.setValue(5)
        alloc_form.addRow("Max Slots:", self.paper_max_slots_spin)
        self.paper_capital_spin = QDoubleSpinBox()
        self.paper_capital_spin.setRange(100.0, 10000000.0)
        self.paper_capital_spin.setSingleStep(1000.0)
        self.paper_capital_spin.setValue(5000.0)
        self.paper_capital_spin.setPrefix("₹ ")
        alloc_form.addRow("Fixed Cap (₹):", self.paper_capital_spin)
        self.paper_product_combo = QComboBox()
        self.paper_product_combo.addItems(["CNC (Delivery)", "MIS (Intraday)"])
        alloc_form.addRow("Product Type:", self.paper_product_combo)
        self.paper_order_type_combo = QComboBox()
        self.paper_order_type_combo.addItems(["MARKET", "LIMIT"])
        alloc_form.addRow("Order Type:", self.paper_order_type_combo)
        paper_cfg_layout.addLayout(alloc_form)

        price_row = QHBoxLayout()
        self.guide_adjusted_cb = QCheckBox(
            "Technical Adjusted prices (splits/bonus/consolidation)"
        )
        self.guide_adjusted_cb.setChecked(True)
        price_row.addWidget(self.guide_adjusted_cb)
        self.guide_include_current_cb = QCheckBox("Include current incomplete D/W/M bars")
        self.guide_include_current_cb.setChecked(True)
        price_row.addWidget(self.guide_include_current_cb)
        price_row.addStretch(1)
        paper_cfg_layout.addLayout(price_row)

        self.paper_auto_watchlist_cb = QCheckBox(
            "Auto-create / update 'PAPER <Template>' watchlists from each run"
        )
        self.paper_auto_watchlist_cb.setChecked(True)
        paper_cfg_layout.addWidget(self.paper_auto_watchlist_cb)

        run_row = QHBoxLayout()
        run_row.setSpacing(8)
        self.run_paper_pipeline_btn = QPushButton("▶ Generate BUY / EXIT Guidance")
        self.run_paper_pipeline_btn.setToolTip(
            "Run Steps 1, 2 and 3 in sequence and generate the combined BUY and EXIT/weakness guidance."
        )
        self.run_paper_pipeline_btn.setStyleSheet(
            "font-weight: 700; background-color: #2e7d32; color: white; padding: 6px 16px; border-radius: 4px;"
        )
        self.run_paper_pipeline_btn.clicked.connect(self._run_paper_pipeline)
        run_row.addWidget(self.run_paper_pipeline_btn)
        self.paper_pipeline_status = QLabel("")
        self.paper_pipeline_status.setStyleSheet("color: #666; font-size: 11px;")
        run_row.addWidget(self.paper_pipeline_status, 1)
        paper_cfg_layout.addLayout(run_row)
        self.paper_pipeline_progress = QProgressBar()
        self.paper_pipeline_progress.setRange(0, 100)
        self.paper_pipeline_progress.setValue(0)
        self.paper_pipeline_progress.setFormat("Ready")
        self.paper_pipeline_progress.setTextVisible(True)
        self.paper_pipeline_progress.setMinimumHeight(22)
        self.paper_pipeline_progress.setVisible(False)
        paper_cfg_layout.addWidget(self.paper_pipeline_progress)
        self.guide_track_model_cb.toggled.connect(self._update_strategy_guide_summary)
        self.paper_pipeline_account_combo.currentIndexChanged.connect(
            self._update_strategy_guide_summary
        )
        self.paper_finalset_combo.currentIndexChanged.connect(
            self._update_strategy_guide_summary
        )
        self.paper_exit_finalset_combo.currentIndexChanged.connect(
            self._update_strategy_guide_summary
        )
        self._update_guide_controls()
        paper_cfg_layout.addStretch(1)
        self.strategy_guide_left_tabs = QTabWidget()
        self.strategy_guide_left_tabs.addTab(paper_cfg_box, "Guidance Setup")
        self.strategy_guide_automation_tab = self._build_strategy_automation_tab()
        self.strategy_guide_left_tabs.addTab(
            self.strategy_guide_automation_tab, "Feeders & Automation"
        )
        paper_columns_layout.addWidget(self.strategy_guide_left_tabs, 3)

        # RIGHT: results + watchlists
        paper_results_box = QGroupBox("📋 BUY / EXIT Guidance")
        paper_results_box.setStyleSheet(
            "QGroupBox { font-weight: bold; font-size: 12px; border: 1px solid #cccccc; "
            "border-radius: 6px; margin-top: 6px; padding-top: 10px; background-color: #fafafa; } "
            "QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; left: 10px; "
            "padding: 0 4px; background-color: #fafafa; color: #1a237e; }"
        )
        paper_results_layout = QVBoxLayout(paper_results_box)
        paper_results_layout.setContentsMargins(8, 8, 8, 8)
        self.paper_shortlist_table = QTableWidget()
        self.paper_shortlist_table.setColumnCount(9)
        self.paper_shortlist_table.setHorizontalHeaderLabels(
            [
                "Signal", "Symbol", "Close", "Confirmations", "Matched Members",
                "In Model", "In Zerodha", "Zerodha Accounts", "Evidence",
            ]
        )
        self.paper_shortlist_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.paper_shortlist_table.horizontalHeader().setSectionResizeMode(
            8, QHeaderView.ResizeMode.Stretch
        )
        paper_results_layout.addWidget(self.paper_shortlist_table, 1)

        wl_row = QHBoxLayout()
        wl_row.addWidget(QLabel("📑 Guide watchlists:"))
        self.paper_watchlist_combo = QComboBox()
        self.paper_watchlist_combo.setMinimumWidth(200)
        wl_row.addWidget(self.paper_watchlist_combo, 1)
        self.refresh_paper_watchlists_btn = QPushButton("↻ Refresh")
        self.refresh_paper_watchlists_btn.clicked.connect(self._refresh_paper_watchlists_panel)
        wl_row.addWidget(self.refresh_paper_watchlists_btn)
        paper_results_layout.addLayout(wl_row)
        self.paper_watchlist_count_lbl = QLabel("")
        self.paper_watchlist_count_lbl.setStyleSheet("color: #666; font-size: 11px;")
        paper_results_layout.addWidget(self.paper_watchlist_count_lbl)
        self._refresh_paper_watchlists_panel()
        paper_columns_layout.addWidget(paper_results_box, 4)

        tab_paper_layout.addLayout(paper_columns_layout)
        tab_paper_layout.addStretch(1)

        tab_paper_scroll.setWidget(tab_paper_content)
        tab_paper_main_layout.addWidget(tab_paper_scroll)

        self.scan_settings_tabs.addTab(tab_paper, "🧭 Strategy Guide")

        # Ranking needs more vertical room than the compact Scan/Telegram page
        # can reliably provide. A dedicated tab keeps every control readable at
        # different screen sizes and Windows display-scaling settings.
        tab_ranking = QWidget()
        self.ranking_settings_tab = tab_ranking
        ranking_tab_layout = QVBoxLayout(tab_ranking)
        ranking_tab_layout.setContentsMargins(12, 10, 12, 10)
        ranking_tab_layout.addWidget(self.ranking_box)
        ranking_tab_layout.addStretch(1)
        self.scan_settings_tabs.addTab(tab_ranking, "📈 Ranking & Scoring")

        self._algo_tab_hint_shown = False
        self.scan_settings_tabs.currentChanged.connect(
            self._on_scan_settings_tab_changed
        )
        self._update_copy_settings_all_enabled(self.scan_settings_tabs.currentIndex())

        scan_outer_layout.addWidget(self.scan_settings_tabs)
        left_layout.addWidget(scan_box)

        templates = QVBoxLayout()
        templates.setSpacing(4)

        tpl_row1 = QHBoxLayout()
        tpl_row1.setSpacing(6)

        self.template_filter_combo = QComboBox()
        self.template_filter_combo.setToolTip("Filter template dropdown by category: General Scans Only or ALGO Execution Only.")
        self.template_filter_combo.addItem("🔍 General Scans Only", "GENERAL")
        self.template_filter_combo.addItem("🤖 ALGO Execution Only", "ALGO")
        self.template_filter_combo.currentIndexChanged.connect(lambda _idx: self._refresh_templates())
        tpl_row1.addWidget(self.template_filter_combo)

        self.template_combo = QComboBox()
        self.template_combo.setMinimumWidth(300)
        self.template_combo.setToolTip("Select a saved scanner template to load its rules and settings.")
        self.template_combo.currentIndexChanged.connect(lambda _idx: self._on_template_combo_changed())
        tpl_row1.addWidget(self.template_combo, 1)
        templates.addLayout(tpl_row1)

        tpl_row2 = QHBoxLayout()
        tpl_row2.setSpacing(6)
        self.master_refresh_interval = QComboBox()
        for minutes in REFRESH_MINUTE_CHOICES:
            if minutes == 60:
                label = "Every 1 Hour"
            elif minutes == 1:
                label = "Every 1 minute"
            else:
                label = f"Every {minutes} minutes"
            self.master_refresh_interval.addItem(label, minutes)
        self.master_refresh_interval.setToolTip(
            "App-wide data freshness for Latest scans (all templates). During NSE "
            "market hours, Latest scans reuse cached data until this interval has "
            "elapsed. Outside market hours the normal cache is used."
        )
        self.master_force_fresh = QCheckBox("Force fresh every scan")
        self.master_force_fresh.setToolTip(
            "Re-download every symbol in the scanned template's universe on every "
            "Latest scan, bypassing the cache. Applies to single, auto-run, and "
            "multi-scan runs. Historical/backtest scans keep using their cache."
        )
        master_settings = self.app_settings.load()
        master_idx = self.master_refresh_interval.findData(
            master_settings.get("data_refresh_minutes", DEFAULT_REFRESH_MINUTES)
        )
        self.master_refresh_interval.setCurrentIndex(max(0, master_idx))
        self.master_force_fresh.setChecked(
            bool(master_settings.get("force_fresh", True))
        )
        tpl_row2.addWidget(QLabel("Refresh latest data:"))
        tpl_row2.addWidget(self.master_refresh_interval)
        tpl_row2.addWidget(self.master_force_fresh)
        tpl_row2.addStretch(1)
        templates.addLayout(tpl_row2)

        self.master_refresh_interval.currentIndexChanged.connect(
            self._master_data_freshness_changed
        )
        self.master_force_fresh.toggled.connect(self._master_data_freshness_changed)

        # General Scans Action Bar
        self.general_actions_widget = QWidget()
        gen_layout = QHBoxLayout(self.general_actions_widget)
        gen_layout.setContentsMargins(0, 0, 0, 0)
        gen_layout.setSpacing(6)
        for text, slot in (
            ("Create New Scan", self._create_new_scan),
            ("Save", self._save_template),
            ("Save as new", self._new_template),
            ("Duplicate", self._duplicate_template),
            ("Promote to ALGO Strategy 🤖", self._promote_general_scan_to_algo_template),
            ("Rename", self._rename_template),
            ("Delete", self._delete_template),
        ):
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            gen_layout.addWidget(btn)

        gen_tools_btn = QToolButton()
        gen_tools_btn.setText("Template Tools ▾")
        gen_tools_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        gen_tools_menu = QMenu(gen_tools_btn)
        gen_export_action = gen_tools_menu.addAction("Export Active Template...")
        gen_export_action.triggered.connect(self._export_template_file)
        gen_import_action = gen_tools_menu.addAction("Import Template File...")
        gen_import_action.triggered.connect(self._import_template_file)
        gen_tools_btn.setMenu(gen_tools_menu)
        gen_layout.addWidget(gen_tools_btn)
        gen_layout.addStretch(1)

        # ALGO Execution Action Bar
        self.algo_actions_widget = QWidget()
        algo_layout = QHBoxLayout(self.algo_actions_widget)
        algo_layout.setContentsMargins(0, 0, 0, 0)
        algo_layout.setSpacing(6)
        for text, slot in (
            ("Create NEW ALGO Template 🤖", self._create_algo_template),
            ("Copy to Another Account...", self._copy_algo_template_to_another_account),
            ("Delete", self._delete_template),
        ):
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            algo_layout.addWidget(btn)

        algo_tools_btn = QToolButton()
        algo_tools_btn.setText("Template Tools ▾")
        algo_tools_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        algo_tools_menu = QMenu(algo_tools_btn)
        algo_copy_acc_action = algo_tools_menu.addAction("Copy to Another Account...")
        algo_copy_acc_action.triggered.connect(self._copy_algo_template_to_another_account)
        algo_tools_menu.addSeparator()
        algo_export_action = algo_tools_menu.addAction("Export Active Template...")
        algo_export_action.triggered.connect(self._export_template_file)
        algo_import_action = algo_tools_menu.addAction("Import Template File...")
        algo_import_action.triggered.connect(self._import_template_file)
        algo_tools_btn.setMenu(algo_tools_menu)
        algo_layout.addWidget(algo_tools_btn)
        algo_layout.addStretch(1)

        templates.addWidget(self.general_actions_widget)
        templates.addWidget(self.algo_actions_widget)
        left_layout.addLayout(templates)
        self.unsaved_label = QLabel("Unsaved template changes - click Save.")
        self.unsaved_label.setStyleSheet("color: #9c0006; background: #ffc7ce; padding: 5px")
        left_layout.addWidget(self.unsaved_label)
        self.main_splitter.addWidget(left)

        right = QWidget()
        right.setMinimumWidth(0)
        right.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        right_layout = QVBoxLayout(right)
        clock_row = QHBoxLayout()
        clock_row.setSpacing(12)
        self.clock_time_lbl = QLabel("--:--:-- IST")
        self.clock_time_lbl.setStyleSheet(
            "font-size: 15px; font-weight: 700; color: #17365d; font-family: Consolas, monospace"
        )
        self.clock_time_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft)
        clock_row.addWidget(self.clock_time_lbl)
        self.market_status_lbl = QLabel("")
        self.market_status_lbl.setStyleSheet("font-size: 14px; font-weight: 600")
        self.market_status_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft)
        clock_row.addWidget(self.market_status_lbl)
        clock_row.addStretch(1)
        right_layout.addLayout(clock_row)
        status_row = QHBoxLayout()
        self.scan_button = QPushButton("Run scan")
        self.scan_button.setStyleSheet("padding: 8px 20px; font-weight: 700; background: #2e75b6; color: white")
        self.scan_button.clicked.connect(self._start_scan)
        self.multi_scan_button = QPushButton("Multi-Scan...")
        self.multi_scan_button.setStyleSheet("padding: 8px 16px; font-weight: 700; background: #8e44ad; color: white")
        self.multi_scan_button.clicked.connect(self._open_multi_scan_dialog)
        self.portfolio_button = QPushButton("💼 Portfolio...")
        self.portfolio_button.setStyleSheet("padding: 8px 16px; font-weight: 700; background: #2c3e50; color: white")
        self.portfolio_button.setToolTip("View and manage your Zerodha portfolio holdings, investments, and P&L across multiple accounts.")
        self.portfolio_button.clicked.connect(self._open_portfolio_dashboard)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel_scan)
        self.export_button = QPushButton("Export Excel...")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._export)
        self.stale_label = QLabel("Settings changed - rerun scan to refresh results.")
        self.stale_label.setStyleSheet("color: #9c5700; background: #ffeb9c; padding: 7px")
        status_row.addWidget(self.scan_button)
        status_row.addWidget(self.multi_scan_button)
        status_row.addWidget(self.portfolio_button)
        status_row.addWidget(self.cancel_button)
        status_row.addWidget(self.export_button)
        status_row.addWidget(self.stale_label, 1)
        right_layout.addLayout(status_row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setFormat("Ready")
        right_layout.addWidget(self.progress)
        self.counts = QLabel("No scan run.")
        right_layout.addWidget(self.counts)
        self.telegram_status = QLabel("Telegram alerts are off.")
        self.telegram_status.setWordWrap(True)
        self.telegram_status.setStyleSheet("color: #555; font-size: 11px")
        right_layout.addWidget(self.telegram_status)
        self.results = QTableWidget(0, 10)
        self.results.setHorizontalHeaderLabels(
            ["Symbol", "Universe", "Sector", "As Of", "Open", "High", "Low", "Close", "Volume", "Evidence"]
        )
        self.results.setSortingEnabled(True)
        self.results.setAlternatingRowColors(True)
        self.results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.results.horizontalHeader().setSectionsMovable(True)
        self.results.verticalHeader().setVisible(False)
        self.results.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.results.customContextMenuRequested.connect(
            lambda pos: self._show_results_context_menu(self.results, pos)
        )
        self.results.itemDoubleClicked.connect(
            lambda item: self._on_result_item_double_clicked(self.results, item)
        )

        self.result_tabs = QTabWidget()
        current_page = QWidget()
        current_layout = QVBoxLayout(current_page)
        current_layout.setContentsMargins(0, 0, 0, 0)
        current_layout.addWidget(self.results, 1)
        self.result_tabs.addTab(current_page, "Current results")

        history_page = QWidget()
        history_layout = QVBoxLayout(history_page)
        history_controls = QHBoxLayout()
        history_controls.addWidget(QLabel("Backtest history"))
        self.history_period = QComboBox()
        self.history_period.addItem("2 years", 2)
        self.history_period.addItem("4 years", 4)
        self.history_period.currentIndexChanged.connect(
            self._history_period_changed
        )
        history_controls.addWidget(self.history_period)
        self.history_run_button = QPushButton("Run history")
        self.history_run_button.clicked.connect(self._start_history)
        self.history_cancel_button = QPushButton("Cancel")
        self.history_cancel_button.setEnabled(False)
        self.history_cancel_button.clicked.connect(self._cancel_history)
        self.history_export_button = QPushButton("Export history Excel...")
        self.history_export_button.setEnabled(False)
        self.history_export_button.clicked.connect(self._export_history)
        history_controls.addWidget(self.history_run_button)
        history_controls.addWidget(self.history_cancel_button)
        history_controls.addWidget(self.history_export_button)
        self.history_stale_label = QLabel("Settings changed - rerun history.")
        self.history_stale_label.setStyleSheet(
            "color: #9c5700; background: #ffeb9c; padding: 5px"
        )
        self.history_stale_label.setVisible(False)
        history_controls.addWidget(self.history_stale_label, 1)
        history_layout.addLayout(history_controls)

        history_note = QLabel(
            "Uses the currently selected universe and completed historical daily snapshots. "
            "Weekly/monthly rules use their latest completed candles. Universe membership is today's list."
        )
        history_note.setWordWrap(True)
        history_note.setStyleSheet("color: #666; font-size: 11px")
        history_layout.addWidget(history_note)
        self.history_progress = QProgressBar()
        self.history_progress.setRange(0, 100)
        self.history_progress.setFormat("History not run.")
        history_layout.addWidget(self.history_progress)
        self.history_counts = QLabel("No history scan run.")
        history_layout.addWidget(self.history_counts)

        self.history_chart = HistoryChartWidget()
        self.history_chart.dateSelected.connect(self._show_history_date)
        self.history_scroll = QScrollArea()
        self.history_scroll.setWidget(self.history_chart)
        self.history_scroll.setWidgetResizable(False)
        self.history_scroll.setMinimumHeight(335)
        history_layout.addWidget(self.history_scroll)
        self.history_legend = QLabel()
        self.history_legend.setWordWrap(True)
        history_layout.addWidget(self.history_legend)
        self.history_selected_label = QLabel(
            "Click a bar to view the stocks matched on that date."
        )
        self.history_selected_label.setStyleSheet("font-weight: 600; color: #17365d")
        history_layout.addWidget(self.history_selected_label)
        self.history_results = QTableWidget(0, 10)
        self.history_results.setSortingEnabled(True)
        self.history_results.setAlternatingRowColors(True)
        self.history_results.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.history_results.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.history_results.horizontalHeader().setSectionsMovable(True)
        self.history_results.verticalHeader().setVisible(False)
        self.history_results.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.history_results.customContextMenuRequested.connect(
            lambda pos: self._show_results_context_menu(self.history_results, pos)
        )
        self.history_results.itemDoubleClicked.connect(
            lambda item: self._on_result_item_double_clicked(self.history_results, item)
        )
        history_layout.addWidget(self.history_results, 1)
        self.result_tabs.addTab(history_page, "Backtest history")
        right_layout.addWidget(self.result_tabs, 1)
        self.main_splitter.addWidget(right)
        self.main_splitter.setStretchFactor(0, 1)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes(
            master_settings.get("main_splitter_sizes", [960, 960])
        )

        self.results.horizontalHeader().sectionMoved.connect(
            lambda logical, old, new: self._result_column_moved(
                self.results, logical, old, new
            )
        )
        self.history_results.horizontalHeader().sectionMoved.connect(
            lambda logical, old, new: self._result_column_moved(
                self.history_results, logical, old, new
            )
        )

        for signal in (
            self.universe_list.itemChanged,
            self.custom_symbols.textChanged,
            self.latest.toggled,
            self.historical.toggled,
            self.as_of_date.dateChanged,
            self.adjusted.toggled,
            self.include_current.toggled,
            self.auto_run.toggled,
            self.auto_run_start_edit.timeChanged,
            self.auto_run_end_edit.timeChanged,
            self.auto_run_trading_days_only.toggled,
            self.result_columns_list.itemChanged,
            self.algo_auto_order_cb.toggled,
            self.algo_action_combo.currentIndexChanged,
            self.algo_sell_mode_combo.currentIndexChanged,
            self.algo_sell_pct_spin.valueChanged,
            self.algo_capital_mode_combo.currentIndexChanged,
            self.algo_max_slots_spin.valueChanged,
            self.algo_capital_spin.valueChanged,
            self.algo_product_combo.currentIndexChanged,
            self.algo_order_type_combo.currentIndexChanged,
            self.algo_limit_pct_spin.valueChanged,
        ):
            signal.connect(self._settings_changed)
        for signal in (
            self.ranking_enabled_cb.toggled,
            self.ranking_rsi_periods_edit.textChanged,
            self.ranking_direction_combo.currentIndexChanged,
            self.ranking_top_n_spin.valueChanged,
            self.ranking_watchlist_cb.toggled,
            self.ranking_watchlist_name_edit.textChanged,
        ):
            signal.connect(self._ranking_settings_changed)
        self.tree.itemClicked.connect(self._tree_item_clicked)
        self.as_of_date.setEnabled(False)
        self.historical.toggled.connect(self.as_of_date.setEnabled)
        self.latest.toggled.connect(self.auto_run.setEnabled)
        self.auto_run.toggled.connect(self._auto_run_toggled)
        self.auto_run_start_edit.timeChanged.connect(self._auto_run_window_changed)
        self.auto_run_end_edit.timeChanged.connect(self._auto_run_window_changed)
        self.auto_run_trading_days_only.toggled.connect(
            self._auto_run_window_changed
        )
        self.latest.toggled.connect(self._auto_run_mode_changed)
        self.auto_run.setEnabled(self.latest.isChecked())
        self.telegram_alerts.toggled.connect(self._telegram_alerts_toggled)
        self.telegram_auto_reset_daily.toggled.connect(
            self._telegram_template_setting_changed
        )
        self.telegram_alert_mode.currentIndexChanged.connect(
            self._telegram_template_setting_changed
        )
        self.telegram_alert_mode.setEnabled(self.telegram_alerts.isChecked())
        self.telegram_auto_reset_daily.setEnabled(self.telegram_alerts.isChecked())
        self._fetch_live_zerodha_funds()
        self._refresh_templates()

    def _show_results_context_menu(self, table: QTableWidget, pos: QPoint) -> None:
        if table is self.history_results:
            return
        item = table.itemAt(pos)
        if not item:
            return
        row = item.row()
        headers = [table.horizontalHeaderItem(i).text() for i in range(table.columnCount())]
        symbol_idx = headers.index("Symbol") if "Symbol" in headers else 0
        close_idx = headers.index("Close") if "Close" in headers else -1
        symbol = table.item(row, symbol_idx).text().strip() if table.item(row, symbol_idx) else ""
        close_str = table.item(row, close_idx).text().strip() if close_idx >= 0 and table.item(row, close_idx) else "0"
        try:
            close_price = float(close_str.replace(",", ""))
        except ValueError:
            close_price = 0.0

        if not symbol:
            return

        account_name, enctoken = self._order_credentials(
            self.last_scan_setup or self._current_setup()
        )

        menu = QMenu(self)
        buy_act = menu.addAction(f"🛒 Place Buy Order for {symbol}...")
        buy_act.triggered.connect(
            lambda: PlaceOrderDialog(
                symbol,
                close_price=close_price,
                action="BUY",
                enctoken=enctoken,
                account_name=account_name,
                parent=self,
            ).exec()
        )
        sell_act = menu.addAction(f"🔴 Place Sell Order for {symbol}...")
        sell_act.triggered.connect(
            lambda: PlaceOrderDialog(
                symbol,
                close_price=close_price,
                action="SELL",
                enctoken=enctoken,
                account_name=account_name,
                parent=self,
            ).exec()
        )
        menu.exec(table.viewport().mapToGlobal(pos))

    def _on_result_item_double_clicked(self, table: QTableWidget, item: QTableWidgetItem) -> None:
        if table is self.history_results:
            return
        row = item.row()
        headers = [table.horizontalHeaderItem(i).text() for i in range(table.columnCount())]
        symbol_idx = headers.index("Symbol") if "Symbol" in headers else 0
        close_idx = headers.index("Close") if "Close" in headers else -1
        symbol = table.item(row, symbol_idx).text().strip() if table.item(row, symbol_idx) else ""
        close_str = table.item(row, close_idx).text().strip() if close_idx >= 0 and table.item(row, close_idx) else "0"
        try:
            close_price = float(close_str.replace(",", ""))
        except ValueError:
            close_price = 0.0

        if symbol:
            account_name, enctoken = self._order_credentials(
                self.last_scan_setup or self._current_setup()
            )
            dialog = PlaceOrderDialog(
                symbol,
                close_price=close_price,
                enctoken=enctoken,
                account_name=account_name,
                parent=self,
            )
            dialog.exec()

    def _normalize_general_templates(self) -> None:
        """Strip Zerodha holdings universes from General templates (idempotent)."""
        try:
            state = self.store.load_state()
            templates = state.get("templates", {})
        except TemplateStoreError:
            return
        changed: dict[str, dict[str, Any]] = {}
        for name, setup in templates.items():
            normalized, is_changed, _removed = normalize_general_universes(name, setup)
            if is_changed:
                changed[name] = normalized
        if not changed:
            return
        try:
            payload = self.store.load_state()
            payload["templates"].update(changed)
            self.store._write(payload)
        except Exception as exc:
            print(f"[ChartSage] Failed to normalize General template universes: {exc}")
            return
        print(
            "[ChartSage] Stripped Zerodha holdings universes from General templates: "
            + ", ".join(sorted(changed))
        )

    def _refresh_templates(self) -> None:
        if getattr(self, "_loading", False) or not hasattr(self, "template_combo"):
            return
        try:
            raw_templates = self.store.load_all()
        except TemplateStoreError as exc:
            self._store_error = str(exc)
            return

        filter_mode = "GENERAL"
        if hasattr(self, "template_filter_combo"):
            filter_mode = str(self.template_filter_combo.currentData() or "GENERAL")
        if hasattr(self, "copy_settings_all_btn"):
            self._update_copy_settings_all_enabled(
                self.scan_settings_tabs.currentIndex()
            )
        self._sync_universe_list()

        if hasattr(self, "general_actions_widget") and hasattr(self, "algo_actions_widget"):
            if filter_mode == "ALGO":
                self.general_actions_widget.hide()
                self.algo_actions_widget.show()
            else:
                self.general_actions_widget.show()
                self.algo_actions_widget.hide()

        is_algo_mode = (filter_mode == "ALGO")
        if hasattr(self, "scan_settings_tabs"):
            self.scan_settings_tabs.setTabEnabled(1, True)
            paper_tab_active = bool(
                hasattr(self, "paper_trading_tab")
                and self.scan_settings_tabs.currentWidget() is self.paper_trading_tab
            )
            if not is_algo_mode:
                self.scan_settings_tabs.setTabToolTip(
                    1,
                    "ALGO order & paper-trade simulation settings — strategies that use them "
                    "are listed under '🤖 ALGO Execution Only'.",
                )
                if not paper_tab_active:
                    self.scan_settings_tabs.setCurrentIndex(0)
            else:
                self.scan_settings_tabs.setTabToolTip(
                    1,
                    "Configure Zerodha account, trade action, capital allocation, and limit order buffers for this ALGO strategy.",
                )

        filtered_items = []
        for name, setup in raw_templates.items():
            try:
                if is_final_template_setup(name, setup):
                    continue
                name_upper = str(name).strip().upper()
                is_algo = is_algo_template_setup(name, setup)
            except Exception:
                is_algo = False

            if filter_mode == "ALGO" and not is_algo:
                continue
            if filter_mode == "GENERAL" and is_algo:
                continue

            target_acc = str(setup.get("algo_target_account", "ACTIVE"))
            if target_acc == "ACTIVE":
                active_uid, _ = self._get_active_account_label()
                if active_uid and active_uid != "ACTIVE":
                    target_acc = active_uid

            tact = str(setup.get("algo_trade_action", "")).upper()
            is_sell = "SELL" in tact or any(k in name_upper for k in ["EXIT", "SELL", "HEATED", "PROFIT", "STOP LOSS"])
            action_tag = "SELL" if is_sell else "BUY"

            if is_algo:
                disp = f"⚡ {name} [{action_tag} ➔ {target_acc}]"
            else:
                disp = f"🔍 {name}"

            filtered_items.append((name, disp))

        filtered_items.sort(key=lambda x: x[0])
        current = self.active_template_name or ""
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        
        target_idx = -1
        for idx, (real_name, disp_text) in enumerate(filtered_items):
            self.template_combo.addItem(disp_text, real_name)
            if current and real_name == current:
                target_idx = idx

        active_to_load = None
        if target_idx >= 0:
            self.template_combo.setCurrentIndex(target_idx)
            active_to_load = filtered_items[target_idx][0]
        elif filtered_items:
            active_to_load = filtered_items[0][0]
            self.template_combo.setCurrentIndex(0)
        else:
            self.active_template_name = None
            self.template_combo.setCurrentIndex(-1)
            setup = default_setup()
            self._load_setup(setup)

        self.template_combo.blockSignals(False)

        if active_to_load:
            self._load_template(active_to_load, force=True)

    def _restore_active_template_selection(self) -> None:
        if self.active_template_name:
            for idx in range(self.template_combo.count()):
                if self.template_combo.itemData(idx) == self.active_template_name:
                    self.template_combo.setCurrentIndex(idx)
                    return
            self.template_combo.setCurrentText(self.active_template_name)
        else:
            self.template_combo.setCurrentIndex(-1)

    def _refresh_result_columns(
        self,
        selected_columns: set[str] | None = None,
        dynamic_saved: bool = True,
    ) -> None:
        if not hasattr(self, "result_columns_list"):
            return
        if selected_columns is None:
            selected_columns = {
                self.result_columns_list.item(i).text()
                for i in range(self.result_columns_list.count())
                if self.result_columns_list.item(i).checkState()
                == Qt.CheckState.Checked
            }
            dynamic_saved = True
        # Keep disabled-rule columns visible in the selector so closing and
        # reopening an eye restores the user's prior column choices. Actual
        # tables/exports still filter them through selected_result_headers().
        rule_columns = rule_value_columns(
            self.setup["expression"],
            include_disabled=True,
        )
        ranking_columns = ranking_result_columns(self.setup)
        dynamic_columns = [*rule_columns, *ranking_columns]
        all_columns = normalized_result_column_order(self.setup)
        self._refreshing_result_columns = True
        self.result_columns_list.blockSignals(True)
        self.result_columns_list.clear()
        for column_name in all_columns:
            item = QListWidgetItem(column_name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            if column_name in dynamic_columns:
                if column_name in ranking_columns:
                    item.setToolTip(
                        "Composite-momentum ranking value. It is automatically shown "
                        "when ranking is enabled."
                    )
                else:
                    item.setToolTip(
                        "Rule value column. Its selection is remembered while the rule "
                        "is disabled, but it appears in results only when that rule is enabled."
                    )
                checked = (
                    True
                    if column_name in ranking_columns
                    else column_name in selected_columns or not dynamic_saved
                )
            else:
                checked = column_name in selected_columns
            item.setCheckState(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )
            self.result_columns_list.addItem(item)
        self.result_columns_list.blockSignals(False)
        self._refreshing_result_columns = False

    def _load_setup(self, setup: dict[str, Any]) -> None:
        self._loading = True
        self.setup = copy.deepcopy(setup)
        self.setup.setdefault("auto_run_start_time", "09:15")
        self.setup.setdefault("auto_run_end_time", "15:30")
        self.setup.setdefault("auto_run_trading_days_only", True)
        
        target_acc = str(setup.get("algo_target_account", "ACTIVE"))
        target_uid = target_acc
        if target_uid == "ACTIVE":
            active_uid, _ = self._get_active_account_label()
            if active_uid and active_uid != "ACTIVE":
                target_uid = active_uid

        filter_mode = "GENERAL"
        if hasattr(self, "template_filter_combo"):
            filter_mode = str(self.template_filter_combo.currentData() or "GENERAL")

        is_algo_tpl = bool(setup.get("is_algo_template", False)) or (filter_mode == "ALGO")

        selected = set(setup.get("universes", []))
        for index in range(self.universe_list.count()):
            item = self.universe_list.item(index)
            raw_name = item.data(Qt.ItemDataRole.UserRole) or item.text().rsplit(" (", 1)[0]
            item_text = item.text()

            if is_algo_tpl and (raw_name.startswith("💼 ZERODHA") or item_text.startswith("💼 ZERODHA")) and target_uid and target_uid != "ACTIVE":
                target_tag = f"({target_uid.upper()})"
                is_match = target_tag in raw_name.upper() or target_tag in item_text.upper()
                item.setCheckState(Qt.CheckState.Checked if is_match else Qt.CheckState.Unchecked)
            else:
                item.setCheckState(
                    Qt.CheckState.Checked
                    if (raw_name in selected or item_text in selected)
                    else Qt.CheckState.Unchecked
                )
        self.custom_symbols.setText(setup.get("custom_symbols", ""))
        latest = setup.get("as_of_mode", "latest") == "latest"
        self.latest.setChecked(latest)
        self.historical.setChecked(not latest)
        qdate = QDate.fromString(setup.get("as_of_date", date.today().isoformat()), Qt.DateFormat.ISODate)
        self.as_of_date.setDate(qdate if qdate.isValid() else QDate.currentDate())
        self.adjusted.setChecked(bool(setup.get("adjusted", True)))
        self.include_current.setChecked(bool(setup.get("include_current", False)))
        self.auto_run.setChecked(bool(setup.get("auto_run", False)))
        start_time = setup.get("auto_run_start_time", "09:15")
        end_time = setup.get("auto_run_end_time", "15:30")
        start_qtime = QTime.fromString(str(start_time), "HH:mm")
        if start_qtime.isValid():
            self.auto_run_start_edit.setTime(start_qtime)
        end_qtime = QTime.fromString(str(end_time), "HH:mm")
        if end_qtime.isValid():
            self.auto_run_end_edit.setTime(end_qtime)
        self.auto_run_trading_days_only.setChecked(
            bool(setup.get("auto_run_trading_days_only", True))
        )
        self.telegram_alerts.setChecked(
            bool(setup.get("telegram_alerts_enabled", False))
        )
        self.telegram_auto_reset_daily.setChecked(
            bool(setup.get("telegram_auto_reset_daily", False))
        )
        telegram_mode = str(setup.get("telegram_alert_mode", "new"))
        telegram_index = self.telegram_alert_mode.findData(telegram_mode)
        self.telegram_alert_mode.setCurrentIndex(max(0, telegram_index))
        self.telegram_alert_mode.setEnabled(self.telegram_alerts.isChecked())
        self.telegram_auto_reset_daily.setEnabled(self.telegram_alerts.isChecked())

        self.ranking_enabled_cb.setChecked(bool(setup.get("ranking_enabled", False)))
        ranking_periods = normalize_ranking_rsi_periods(
            setup.get("ranking_rsi_periods", [22, 44, 66])
        )
        self.ranking_rsi_periods_edit.setText(
            ", ".join(str(period) for period in ranking_periods)
        )
        ranking_direction = str(setup.get("ranking_direction", "DESC") or "DESC").upper()
        ranking_index = self.ranking_direction_combo.findData(ranking_direction)
        self.ranking_direction_combo.setCurrentIndex(max(0, ranking_index))
        try:
            ranking_top_n = max(0, int(setup.get("ranking_top_n", 0) or 0))
        except (TypeError, ValueError):
            ranking_top_n = 0
        self.ranking_top_n_spin.setValue(min(10000, ranking_top_n))
        self.ranking_watchlist_cb.setChecked(
            bool(setup.get("ranking_watchlist_enabled", False))
        )
        self.ranking_watchlist_name_edit.setText(
            str(setup.get("ranking_watchlist_name", "") or "")
        )
        self._update_ranking_controls(mark_dirty=False)

        self.algo_auto_order_cb.setChecked(
            bool(setup.get("algo_auto_order_cb", False))
            or bool(setup.get("algo_auto_order_enabled", False))
        )
        self._populate_paper_account_combo()
        paper_acc = str(setup.get("algo_paper_account", "")).strip().upper()
        paper_idx = self.algo_paper_account_combo.findData(paper_acc)
        self.algo_paper_account_combo.setCurrentIndex(
            paper_idx if paper_idx >= 0 else 0
        )
        self.algo_paper_account_combo.setEnabled(self.algo_auto_order_cb.isChecked())
        self._populate_algo_target_accounts()
        target_acc = str(setup.get("algo_target_account", "ACTIVE")).strip()
        tpl_name_upper = (self.active_template_name or "").upper()
        
        t_idx = -1
        for i in range(self.algo_target_account_combo.count()):
            data = str(self.algo_target_account_combo.itemData(i) or "").strip().upper()
            text = str(self.algo_target_account_combo.itemText(i) or "").strip().upper()
            
            if data and data != "ACTIVE":
                if data == target_acc.upper() or f"({data})" in target_acc.upper() or data in target_acc.upper():
                    t_idx = i
                    break
                if f"({data})" in tpl_name_upper or f"- {data}" in tpl_name_upper or f"[{data}]" in tpl_name_upper:
                    t_idx = i
                    break

        if t_idx >= 0:
            self.algo_target_account_combo.setCurrentIndex(t_idx)
        else:
            self.algo_target_account_combo.setCurrentIndex(0)
        self._on_algo_target_account_changed()
        if "algo_trade_action" in setup:
            tact = str(setup.get("algo_trade_action", "BUY"))
            is_sell = "SELL" in tact
        else:
            tpl_name = (self.active_template_name or "").upper()
            is_sell = any(k in tpl_name for k in ["EXIT", "SELL", "HEATED", "PROFIT", "STOP LOSS"])

        try:
            if hasattr(self, "algo_action_combo") and self.algo_action_combo:
                self.algo_action_combo.blockSignals(True)
                self.algo_action_combo.setCurrentText(
                    "SELL (Exit Order - Portfolio Holdings Guard)" if is_sell else "BUY (Entry Order - Budget Based)"
                )
                self.algo_action_combo.blockSignals(False)
        except RuntimeError:
            pass

        try:
            if hasattr(self, "algo_sell_mode_combo") and self.algo_sell_mode_combo:
                sell_mode = str(setup.get("algo_sell_mode", "100% (Sell Full Holding Quantity)"))
                self.algo_sell_mode_combo.setCurrentText(
                    sell_mode if "Custom" in sell_mode else "100% (Sell Full Holding Quantity)"
                )
        except RuntimeError:
            pass

        try:
            if hasattr(self, "_update_algo_action_ui_fn") and hasattr(self, "algo_action_combo"):
                self._update_algo_action_ui_fn(self.algo_action_combo.currentIndex())
        except RuntimeError:
            pass

        try:
            if hasattr(self, "algo_capital_mode_combo") and self.algo_capital_mode_combo:
                cap_mode = str(setup.get("algo_capital_mode", "Dynamic Allocation (Live Cash ÷ Max Position Slots)"))
                self.algo_capital_mode_combo.setCurrentText(cap_mode if cap_mode in [
                    "Dynamic Allocation (Live Cash ÷ Max Position Slots)",
                    "Fixed Capital per Stock (Manual Ceiling)"
                ] else "Dynamic Allocation (Live Cash ÷ Max Position Slots)")
        except RuntimeError:
            pass

        try:
            if hasattr(self, "algo_max_slots_spin") and self.algo_max_slots_spin:
                self.algo_max_slots_spin.setValue(int(setup.get("algo_max_slots", 5)))
        except RuntimeError:
            pass

        try:
            if hasattr(self, "algo_capital_spin") and self.algo_capital_spin:
                self.algo_capital_spin.setValue(float(setup.get("algo_capital_per_stock", 5000.0)))
        except RuntimeError:
            pass

        try:
            if hasattr(self, "_update_effective_capital_per_stock"):
                self._update_effective_capital_per_stock()
        except RuntimeError:
            pass

        try:
            if hasattr(self, "algo_product_combo") and self.algo_product_combo:
                prod = str(setup.get("algo_product", "CNC (Delivery)"))
                self.algo_product_combo.setCurrentText(prod if prod in ["CNC (Delivery)", "MIS (Intraday)"] else "CNC (Delivery)")
        except RuntimeError:
            pass

        try:
            if hasattr(self, "algo_order_type_combo") and self.algo_order_type_combo:
                default_otype = "MARKET" if is_sell else "LIMIT"
                otype = str(setup.get("algo_order_type", default_otype))
                self.algo_order_type_combo.setCurrentText(otype if otype in ["MARKET", "LIMIT"] else default_otype)
        except RuntimeError:
            pass

        try:
            if hasattr(self, "algo_limit_pct_spin") and self.algo_limit_pct_spin:
                self.algo_limit_pct_spin.setValue(float(setup.get("algo_limit_buffer_pct", 0.50)))
                self.algo_limit_pct_spin.setEnabled(self.algo_order_type_combo.currentText() == "LIMIT")
        except RuntimeError:
            pass

        try:
            if hasattr(self, "telegram_status") and self.telegram_status:
                self.telegram_status.setText(
                    "Telegram alerts are enabled for this template."
                    if self.telegram_alerts.isChecked()
                    else "Telegram alerts are off."
                )
        except RuntimeError:
            pass
        history_index = self.history_period.findData(
            int(setup.get("history_years", 2))
        )
        self.history_period.setCurrentIndex(max(0, history_index))
        try:
            # Templates are editable drafts and may legitimately contain an
            # unfinished group. Strict validation still runs before every scan.
            validate_ast(setup["expression"], allow_empty_groups=True)
            self.setup["expression"] = copy.deepcopy(setup["expression"])
        except (KeyError, ValueError):
            self.setup["expression"] = default_setup()["expression"]
        selected_result_columns = set(setup.get("result_columns", ALL_RESULT_COLUMNS))
        if not setup.get("serial_result_column_saved", False):
            selected_result_columns.add(SERIAL_COLUMN)
        if not setup.get("timing_result_columns_saved", False):
            selected_result_columns.update(TIMING_RESULT_COLUMNS)
        if not setup.get("data_source_column_saved", False):
            selected_result_columns.add(DATA_SOURCE_COLUMN)
        if not setup.get("percent_change_column_saved", False):
            selected_result_columns.add(PERCENT_CHANGE_COLUMN)
        self._refresh_result_columns(
            selected_result_columns,
            dynamic_saved=bool(setup.get("dynamic_result_columns_saved", False)),
        )
        self._rebuild_tree()
        self._loading = False
        self._on_algo_target_account_changed()
        if hasattr(self, "save_algo_settings_btn"):
            active_algo = bool(self.active_template_name and is_algo_tpl)
            self.save_algo_settings_btn.setVisible(active_algo)
            self.save_algo_settings_btn.setEnabled(active_algo)

    def _safe_widget_val(self, widget_name: str, getter_name: str, fallback: Any = "") -> Any:
        widget = getattr(self, widget_name, None)
        if widget is not None:
            try:
                getter = getattr(widget, getter_name, None)
                if callable(getter):
                    val = getter()
                    if val is not None:
                        return val
            except RuntimeError:
                pass
        if hasattr(self, "setup") and isinstance(self.setup, dict):
            return self.setup.get(widget_name, fallback)
        return fallback

    def _current_setup(self) -> dict[str, Any]:
        universes = []
        if hasattr(self, "universe_list"):
            try:
                universes = [
                    self.universe_list.item(i).data(Qt.ItemDataRole.UserRole)
                    or self.universe_list.item(i).text().rsplit(" (", 1)[0]
                    for i in range(self.universe_list.count())
                    if self.universe_list.item(i).checkState() == Qt.CheckState.Checked
                ]
            except RuntimeError:
                universes = self.setup.get("universes", [])
        else:
            universes = self.setup.get("universes", [])

        result_columns = []
        if hasattr(self, "result_columns_list"):
            try:
                result_columns = [
                    self.result_columns_list.item(i).text()
                    for i in range(self.result_columns_list.count())
                    if self.result_columns_list.item(i).checkState() == Qt.CheckState.Checked
                ]
            except RuntimeError:
                result_columns = self.setup.get("result_columns", ALL_RESULT_COLUMNS)
        else:
            result_columns = self.setup.get("result_columns", ALL_RESULT_COLUMNS)

        if "Symbol" not in result_columns:
            result_columns.insert(0, "Symbol")

        ranking_enabled = bool(
            self._safe_widget_val(
                "ranking_enabled_cb", "isChecked",
                self.setup.get("ranking_enabled", False),
            )
        )
        ranking_periods_input = self._safe_widget_val(
            "ranking_rsi_periods_edit", "text",
            self.setup.get("ranking_rsi_periods", [22, 44, 66]),
        )
        try:
            ranking_periods: Any = (
                parse_ranking_rsi_periods_strict(ranking_periods_input)
                if ranking_enabled
                else normalize_ranking_rsi_periods(ranking_periods_input)
            )
        except ValueError:
            # Preserve invalid editor text so validation reports it instead of
            # silently substituting the default periods.
            ranking_periods = ranking_periods_input
        ranking_direction = str(
            self._safe_widget_val(
                "ranking_direction_combo", "currentData",
                self.setup.get("ranking_direction", "DESC"),
            ) or "DESC"
        ).upper()

        result_column_order = normalized_result_column_order(
            {
                "expression": self.setup["expression"],
                "result_column_order": self.setup.get("result_column_order", []),
                "ranking_enabled": ranking_enabled,
                "ranking_rsi_periods": ranking_periods,
            }
        )

        custom_syms = ""
        if hasattr(self, "custom_symbols"):
            try:
                custom_syms = self.custom_symbols.text().strip()
            except RuntimeError:
                custom_syms = self.setup.get("custom_symbols", "")

        as_of_mode = "latest"
        if hasattr(self, "latest"):
            try:
                as_of_mode = "latest" if self.latest.isChecked() else "historical"
            except RuntimeError:
                as_of_mode = self.setup.get("as_of_mode", "latest")

        as_of_date_str = date.today().isoformat()
        if hasattr(self, "as_of_date"):
            try:
                as_of_date_str = self.as_of_date.date().toString(Qt.DateFormat.ISODate)
            except RuntimeError:
                as_of_date_str = self.setup.get("as_of_date", date.today().isoformat())

        return {
            "version": 1,
            "is_algo_template": bool(self.setup.get("is_algo_template", False)),
            "universes": universes,
            "custom_symbols": custom_syms,
            "expression": copy.deepcopy(self.setup["expression"]),
            "as_of_mode": as_of_mode,
            "as_of_date": as_of_date_str,
            "adjusted": bool(self._safe_widget_val("adjusted", "isChecked", self.setup.get("adjusted", True))),
            "include_current": bool(self._safe_widget_val("include_current", "isChecked", self.setup.get("include_current", False))),
            "auto_run": bool(self._safe_widget_val("auto_run", "isChecked", self.setup.get("auto_run", False))),
            "auto_run_start_time": self.auto_run_start_edit.time().toString("HH:mm"),
            "auto_run_end_time": self.auto_run_end_edit.time().toString("HH:mm"),
            "auto_run_trading_days_only": self.auto_run_trading_days_only.isChecked(),
            "telegram_alerts_enabled": bool(self._safe_widget_val("telegram_alerts", "isChecked", self.setup.get("telegram_alerts_enabled", False))),
            "telegram_alert_mode": str(self._safe_widget_val("telegram_alert_mode", "currentData", self.setup.get("telegram_alert_mode", "new")) or "new"),
            "telegram_auto_reset_daily": bool(self._safe_widget_val("telegram_auto_reset_daily", "isChecked", self.setup.get("telegram_auto_reset_daily", False))),
            "algo_auto_order_enabled": bool(self._safe_widget_val("algo_auto_order_cb", "isChecked", self.setup.get("algo_auto_order_cb", False))),
            "algo_auto_order_cb": bool(self._safe_widget_val("algo_auto_order_cb", "isChecked", self.setup.get("algo_auto_order_cb", False))),
            "algo_paper_account": str(self._safe_widget_val("algo_paper_account_combo", "currentData", self.setup.get("algo_paper_account", "")) or ""),
            "algo_trade_action": str(self._safe_widget_val("algo_action_combo", "currentText", self.setup.get("algo_trade_action", "BUY")) or "BUY"),
            "algo_target_account": str(self._safe_widget_val("algo_target_account_combo", "currentData", self.setup.get("algo_target_account", "ACTIVE")) or "ACTIVE"),
            "algo_sell_mode": str(self._safe_widget_val("algo_sell_mode_combo", "currentText", self.setup.get("algo_sell_mode", "100% (Sell Full Holding Quantity)")) or "100% (Sell Full Holding Quantity)"),
            "algo_sell_pct": float(self._safe_widget_val("algo_sell_pct_spin", "value", self.setup.get("algo_sell_pct", 50.0)) or 50.0),
            "algo_capital_mode": str(self._safe_widget_val("algo_capital_mode_combo", "currentText", self.setup.get("algo_capital_mode", "Dynamic Allocation (Live Cash ÷ Max Position Slots)")) or "Dynamic Allocation (Live Cash ÷ Max Position Slots)"),
            "algo_max_slots": int(self._safe_widget_val("algo_max_slots_spin", "value", self.setup.get("algo_max_slots", 5)) or 5),
            "algo_capital_per_stock": float(self._safe_widget_val("algo_capital_spin", "value", self.setup.get("algo_capital_per_stock", 5000.0)) or 5000.0),
            "algo_product": str(self._safe_widget_val("algo_product_combo", "currentText", self.setup.get("algo_product", "CNC (Delivery)")) or "CNC (Delivery)"),
            "algo_order_type": str(self._safe_widget_val("algo_order_type_combo", "currentText", self.setup.get("algo_order_type", "LIMIT")) or "LIMIT"),
            "algo_limit_buffer_pct": float(self._safe_widget_val("algo_limit_pct_spin", "value", self.setup.get("algo_limit_buffer_pct", 0.50)) or 0.50),
            "ranking_enabled": ranking_enabled,
            "ranking_rsi_periods": ranking_periods,
            "ranking_direction": ranking_direction,
            "ranking_top_n": int(self._safe_widget_val("ranking_top_n_spin", "value", self.setup.get("ranking_top_n", 0)) or 0),
            "ranking_watchlist_enabled": bool(self._safe_widget_val("ranking_watchlist_cb", "isChecked", self.setup.get("ranking_watchlist_enabled", False))),
            "ranking_watchlist_name": str(self._safe_widget_val("ranking_watchlist_name_edit", "text", self.setup.get("ranking_watchlist_name", "")) or "").strip(),
            "history_years": int(self._safe_widget_val("history_period", "currentData", self.setup.get("history_years", 2)) or 2),
            "result_columns": result_columns,
            "result_column_order": result_column_order,
            "dynamic_result_columns_saved": True,
            "timing_result_columns_saved": True,
            "data_source_column_saved": True,
            "serial_result_column_saved": True,
            "percent_change_column_saved": True,
        }

    def _get_active_account_label(self) -> tuple[str, str]:
        active_uid = ""
        if ZERODHA_SESSION_FILE.exists():
            try:
                payload = json.loads(ZERODHA_SESSION_FILE.read_text(encoding="utf-8"))
                active_uid = str(payload.get("user_id", "")).strip().upper()
            except Exception:
                pass

        login_store = BrokerLoginStore()
        if active_uid:
            try:
                accounts = login_store.get_broker_accounts("Zerodha")
                for acc in accounts:
                    u_id = str(acc.get("user_id", "")).strip().upper()
                    if u_id == active_uid:
                        lbl = str(acc.get("label", f"Zerodha ({u_id})")).strip()
                        return active_uid, f"⚡ Active Account: {lbl} ({u_id})"
            except Exception:
                pass
            return active_uid, f"⚡ Active Account: Zerodha ({active_uid})"

        return "ACTIVE", "⚡ Active Logged-in Account"

    def _populate_paper_account_combo(self) -> None:
        if not hasattr(self, "algo_paper_account_combo"):
            return
        current = str(self.algo_paper_account_combo.currentData() or "").strip().upper()
        self.algo_paper_account_combo.blockSignals(True)
        self.algo_paper_account_combo.clear()
        self.algo_paper_account_combo.addItem("— None selected —", "")
        try:
            accounts = PaperTradesStore().list_paper_accounts()
        except PaperTradesStoreError:
            accounts = []
        for acc in accounts:
            uid = str(acc.get("uid", "")).strip().upper()
            label = str(acc.get("display_name") or uid)
            self.algo_paper_account_combo.addItem(f"📒 {label}", uid)
        self.algo_paper_account_combo.blockSignals(False)
        idx = self.algo_paper_account_combo.findData(current)
        self.algo_paper_account_combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _on_algo_paper_account_changed(self, *_args: Any) -> None:
        if getattr(self, "_loading", False):
            return
        self.setup = self._current_setup()
        self._set_dirty(True)

    def _populate_algo_target_accounts(self) -> None:
        if not hasattr(self, "algo_target_account_combo"):
            return
        self.algo_target_account_combo.blockSignals(True)
        self.algo_target_account_combo.clear()
        
        active_uid, active_label = self._get_active_account_label()
        self.algo_target_account_combo.addItem(active_label, "ACTIVE")

        login_store = BrokerLoginStore()
        try:
            accounts = login_store.get_broker_accounts("Zerodha")
            for acc in accounts:
                user_id = str(acc.get("user_id") or "").strip()
                label = str(acc.get("label") or f"Zerodha ({user_id})").strip()
                if user_id:
                    self.algo_target_account_combo.addItem(f"👤 {label} ({user_id})", user_id)
        except Exception:
            pass
        self.algo_target_account_combo.blockSignals(False)

    def _on_algo_target_account_changed(self, *_args: Any) -> None:
        if getattr(self, "_loading", False):
            return
        acc_data = str(self.algo_target_account_combo.currentData() or "ACTIVE")
        acc_text = str(self.algo_target_account_combo.currentText() or "Active Account")

        target_uid = acc_data
        if target_uid == "ACTIVE":
            active_uid, _ = self._get_active_account_label()
            if active_uid and active_uid != "ACTIVE":
                target_uid = active_uid

        filter_mode = "GENERAL"
        if hasattr(self, "template_filter_combo"):
            filter_mode = str(self.template_filter_combo.currentData() or "GENERAL")

        if filter_mode == "ALGO" and target_uid and target_uid != "ACTIVE" and hasattr(self, "universe_list"):
            target_tag = f"({target_uid.upper()})"
            for row in range(self.universe_list.count()):
                item = self.universe_list.item(row)
                txt = item.text()
                raw = item.data(Qt.ItemDataRole.UserRole) or txt.rsplit(" (", 1)[0]
                if txt.startswith("💼 ZERODHA") or txt.startswith("💼 Zerodha") or raw.startswith("💼 ZERODHA"):
                    if target_tag in txt.upper() or target_tag in raw.upper():
                        item.setCheckState(Qt.CheckState.Checked)
                    else:
                        item.setCheckState(Qt.CheckState.Unchecked)

        if hasattr(self, "algo_account_scope_banner"):
            if acc_data == "ACTIVE":
                self.algo_account_scope_banner.setText(
                    "<b>ℹ️ ALGO Execution Scope:</b> Orders placed by this template will execute on the <b>currently active logged-in account</b>. "
                    "Available cash, holdings, and system watchlists are isolated to that active account."
                )
            else:
                self.algo_account_scope_banner.setText(
                    f"<b>ℹ️ ALGO Execution Scope:</b> Orders placed by this template will execute exclusively on <b>{acc_text}</b>. "
                    f"Available cash, holdings, and system watchlists (💼 {acc_text.replace('👤 ', '')}) are isolated to this account."
                )
        self._fetch_live_zerodha_funds()

    def _get_enctoken_for_account(self, user_id: str) -> str:
        user_id_upper = user_id.strip().upper()
        try:
            accounts = self.login_store.get_broker_accounts("Zerodha")
            for acc in accounts:
                if str(acc.get("user_id", "")).strip().upper() == user_id_upper:
                    tok = str(acc.get("enctoken", "")).strip()
                    if tok:
                        return tok
        except Exception:
            pass
        return ""

    def _get_active_enctoken(self) -> str:
        active_uid, _label = self._get_active_account_label()
        if active_uid and active_uid != "ACTIVE":
            token = self.login_store.get_token_for_user(active_uid)
            if token:
                self.enctoken = token
                return token
        return getattr(self, "enctoken", "")

    def _order_credentials(self, setup: dict[str, Any]) -> tuple[str, str]:
        """Resolve a specific account and its token; never return an unbound token."""
        account_name = setup_portfolio_account(setup)
        if account_name is None:
            active_uid, _label = self._get_active_account_label()
            account_name = active_uid if active_uid != "ACTIVE" else None
        if not account_name:
            return "", ""
        return account_name, self._get_enctoken_for_account(account_name)

    def _auto_sync_holdings_tick(self) -> None:
        ist_now = get_ist_now()
        if not nse_market_is_open(ist_now):
            return

        token = self._get_active_enctoken()
        if not token or getattr(self, "_holdings_sync_in_progress", False):
            return

        self._holdings_sync_in_progress = True
        self._auto_holdings_worker = ZerodhaHoldingsWorker(token)
        self._auto_holdings_worker.succeeded.connect(self._on_auto_sync_holdings_success)
        self._auto_holdings_worker.failed.connect(self._on_auto_sync_holdings_failed)
        self._auto_holdings_worker.start()

    def _refresh_nse_calendar(self) -> None:
        if self._nse_calendar_worker and self._nse_calendar_worker.isRunning():
            return
        self._nse_calendar_worker = NSECalendarRefreshWorker()
        self._nse_calendar_worker.finished.connect(self._nse_calendar_refresh_finished)
        self._nse_calendar_worker.start()

    def _nse_calendar_refresh_finished(self) -> None:
        self._nse_calendar_worker = None

    def _on_auto_sync_holdings_success(self, symbols: list[str]) -> None:
        self._holdings_sync_in_progress = False
        self._reload_system_watchlists()

    def _on_auto_sync_holdings_failed(self, err: str) -> None:
        self._holdings_sync_in_progress = False

    def _reload_system_watchlists(self) -> None:
        try:
            sync_zerodha_system_watchlist()
            self.watchlists = self.watchlist_store.load()
            for name, syms in self.watchlists.items():
                UNIVERSES[name] = syms
        except Exception:
            pass
        self._sync_universe_list()

    def _open_zerodha_login(self) -> None:
        dialog = ZerodhaImportDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._reload_system_watchlists()
        self._populate_algo_target_accounts()
        self.enctoken = ""
        self._fetch_live_zerodha_funds()

    def _fetch_live_zerodha_funds(self) -> None:
        acc_data = "ACTIVE"
        acc_text = "your ACCOUNT"
        if hasattr(self, "algo_target_account_combo"):
            acc_data = str(self.algo_target_account_combo.currentData() or "ACTIVE")
            acc_text = str(self.algo_target_account_combo.currentText() or "your ACCOUNT").replace("👤 ", "")

        token = ""
        if acc_data == "ACTIVE":
            token = self._get_active_enctoken()
        else:
            token = self._get_enctoken_for_account(acc_data)

        if not token:
            self.funds_available_label.setText(f"FUNDS AVAILABLE in {acc_text}: <b style='color: #d32f2f;'>Session Expired. Click '💼 Portfolio...' to log in & refresh token.</b>")
            self.live_available_cash = 0.0
            self._update_effective_capital_per_stock()
            return

        self.refresh_funds_btn.setEnabled(False)
        self.refresh_funds_btn.setText("Refreshing...")
        self._margins_fetcher = ZerodhaMarginsFetcher(token, self)
        self._margins_fetcher.succeeded.connect(self._on_margins_fetched)
        self._margins_fetcher.failed.connect(self._on_margins_failed)
        self._margins_fetcher.start()

    def _on_margins_fetched(self, data: dict[str, float]) -> None:
        self.refresh_funds_btn.setEnabled(True)
        self.refresh_funds_btn.setText("↻ Refresh Funds")
        cash = data.get("available_cash", 0.0)
        self.live_available_cash = cash
        
        acc_text = "your ACCOUNT"
        if hasattr(self, "algo_target_account_combo"):
            acc_text = str(self.algo_target_account_combo.currentText() or "your ACCOUNT").replace("👤 ", "")
        
        self.funds_available_label.setText(f"FUNDS AVAILABLE in {acc_text}: <b style='color: #1b5e20;'>₹ {format_number(cash)}</b>")
        self._update_effective_capital_per_stock()

    def _on_margins_failed(self, err: str) -> None:
        self.refresh_funds_btn.setEnabled(True)
        self.refresh_funds_btn.setText("↻ Refresh Funds")
        acc_text = "your ACCOUNT"
        if hasattr(self, "algo_target_account_combo"):
            acc_text = str(self.algo_target_account_combo.currentText() or "your ACCOUNT").replace("👤 ", "")
        self.funds_available_label.setText(f"FUNDS AVAILABLE in {acc_text}: <b style='color: #d32f2f;'>Session Expired. Click '💼 Portfolio...' to log in & refresh token.</b>")
        self.live_available_cash = 0.0
        self._update_effective_capital_per_stock()

    def _update_effective_capital_per_stock(self) -> None:
        is_dynamic = (self.algo_capital_mode_combo.currentIndex() == 0)
        self.algo_max_slots_spin.setEnabled(is_dynamic)
        self.algo_capital_spin.setEnabled(not is_dynamic)

        if is_dynamic:
            slots = max(1, self.algo_max_slots_spin.value())
            cash = getattr(self, "live_available_cash", 0.0)
            allocated = cash / slots if cash > 0 else 0.0
            if cash > 0:
                self.algo_calculated_capital_lbl.setText(
                    f"Allocated Capital Per Stock: <b>₹ {format_number(allocated)}</b> (₹ {format_number(cash)} ÷ {slots} slots)"
                )
            else:
                self.algo_calculated_capital_lbl.setText(
                    f"Allocated Capital Per Stock: <b>₹ --</b> (Click '↻ Refresh Funds' to fetch Zerodha Cash)"
                )
        else:
            allocated = self.algo_capital_spin.value()
            self.algo_calculated_capital_lbl.setText(f"Allocated Capital Per Stock: <b>₹ {format_number(allocated)}</b> (Manual Fixed)")

    def get_effective_capital_per_stock(self) -> float:
        if self.algo_capital_mode_combo.currentIndex() == 0:
            slots = max(1, self.algo_max_slots_spin.value())
            cash = getattr(self, "live_available_cash", 0.0)
            return cash / slots if cash > 0 else self.algo_capital_spin.value()
        return self.algo_capital_spin.value()

    def _telegram_alerts_toggled(self, enabled: bool) -> None:
        self.telegram_alert_mode.setEnabled(enabled)
        self.telegram_auto_reset_daily.setEnabled(enabled)
        self._telegram_template_setting_changed()

    def _telegram_template_setting_changed(self, *_args: Any) -> None:
        if self._loading:
            return
        self.setup = self._current_setup()
        self._set_dirty(True)
        self.telegram_status.setText(
            "Telegram alerts are enabled; save the template to retain this setting."
            if self.telegram_alerts.isChecked()
            else "Telegram alerts are off; save the template to retain this setting."
        )

    def _open_telegram_settings(self) -> None:
        dialog = TelegramSettingsDialog(self.telegram_settings_store, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.telegram_status.setText(
                "Telegram settings saved. Alerts will be sent after successful scans "
                "for templates that have alerts enabled."
            )
            self.telegram_status.setStyleSheet("color: #006100; font-size: 11px")

    def _open_multi_scan_dialog(self) -> None:
        dialog = MultiTemplateDialog(
            template_store=self.store,
            active_templates=self._multi_scan_templates,
            active_interval_mins=self._multi_scan_interval_mins,
            is_running=self._multi_scan_active,
            parent=self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._multi_scan_templates = dialog.get_selected_templates()
            self._multi_scan_interval_mins = dialog.get_interval_mins()
            self.store.save_multi_scan_config(
                self._multi_scan_templates, self._multi_scan_interval_mins
            )
            if dialog.should_stop:
                self._stop_multi_scan()
            elif dialog.should_start:
                self._start_multi_scan()

    def _start_multi_scan(self) -> None:
        if not self._multi_scan_templates:
            return
        self._multi_scan_active = True
        self.multi_scan_button.setText("Multi-Scan (RUNNING)")
        self.multi_scan_button.setStyleSheet(
            "padding: 8px 16px; font-weight: 700; background: #27ae60; color: white"
        )
        self._start_next_multi_scan_cycle()

    def _stop_multi_scan(self) -> None:
        self._multi_scan_active = False
        self._multi_scan_queue.clear()
        self._multi_scan_timer.stop()
        for worker in (self.worker, self.history_worker):
            if worker and worker.isRunning():
                worker.cancel()
        self.multi_scan_button.setText("Multi-Scan...")
        self.multi_scan_button.setStyleSheet(
            "padding: 8px 16px; font-weight: 700; background: #8e44ad; color: white"
        )
        self.cancel_button.setEnabled(False)
        self.history_cancel_button.setEnabled(False)
        self.progress.setFormat("Multi-Scan stopped.")

    def _start_next_multi_scan_cycle(self) -> None:
        if not self._multi_scan_active:
            return
        self._multi_scan_queue = list(self._multi_scan_templates)
        self._process_next_multi_scan_queue_item()

    def _process_next_multi_scan_queue_item(self) -> None:
        if not self._multi_scan_active:
            return
        now = get_ist_now()
        if not self._within_auto_run_window(now):
            next_due = self._next_auto_run_due(now)
            if next_due is not None:
                delay_ms = max(
                    1_000, int((next_due - now).total_seconds() * 1000)
                )
                self._multi_scan_timer.start(delay_ms)
                self.progress.setFormat(
                    f"Multi-Scan paused until {next_due.strftime('%H:%M')} "
                    "(outside auto-run window)."
                )
            else:
                self.progress.setFormat("Multi-Scan paused (invalid auto-run window).")
            return
        if not self._multi_scan_queue:
            # Cycle complete. Schedule next cycle timer.
            delay_ms = self._multi_scan_interval_mins * 60_000
            self._multi_scan_timer.start(delay_ms)
            self.progress.setFormat(
                f"Multi-Scan cycle complete ({len(self._multi_scan_templates)} templates). "
                f"Next cycle in {self._multi_scan_interval_mins}m."
            )
            return

        if (
            (self.worker and self.worker.isRunning())
            or (self.history_worker and self.history_worker.isRunning())
            or (self.telegram_worker and self.telegram_worker.isRunning())
            or (
                self._guide_automation_worker
                and self._guide_automation_worker.isRunning()
            )
        ):
            QTimer.singleShot(400, self._process_next_multi_scan_queue_item)
            return

        template_name = self._multi_scan_queue.pop(0)
        try:
            setup = self.store.load_all().get(template_name)
            if setup is None:
                raise ValueError(f"Template '{template_name}' no longer exists.")
            setup = copy.deepcopy(setup)
            self._load_setup(setup)
            self.active_template_name = template_name
            self.template_combo.setCurrentText(template_name)
            total_tpls = len(self._multi_scan_templates)
            remaining = len(self._multi_scan_queue)
            current_idx = total_tpls - remaining
            self.progress.setFormat(
                f"Multi-Scan [{current_idx}/{total_tpls}]: Running {template_name}..."
            )
            if not self._start_scan():
                QTimer.singleShot(200, self._process_next_multi_scan_queue_item)
        except Exception as exc:
            print(f"Multi-scan template load error ({template_name}): {exc}")
            QTimer.singleShot(200, self._process_next_multi_scan_queue_item)

    def _open_expanded_expression_editor(self) -> None:
        if not hasattr(self, "expanded_editor_dialog") or self.expanded_editor_dialog is None:
            self.expanded_editor_dialog = ExpandedExpressionDialog(self)
        self.expanded_editor_dialog.sync_from_main()
        self.expanded_editor_dialog.show()
        self.expanded_editor_dialog.raise_()
        self.expanded_editor_dialog.activateWindow()

    def _open_watchlist_manager(self) -> None:
        try:
            dialog = WatchlistManagerDialog(self.watchlist_store, self)
        except WatchlistStoreError as exc:
            QMessageBox.warning(self, "Watchlist file preserved", str(exc))
            return
        dialog.exec()

    def _watchlist_created(self, name: str) -> None:
        UNIVERSES[name] = []
        self._sync_universe_list()

    def _watchlist_deleted(self, name: str) -> None:
        UNIVERSES.pop(name, None)
        self._sync_universe_list()

    def _watchlist_renamed(self, old_name: str, new_name: str) -> None:
        if old_name in UNIVERSES:
            UNIVERSES[new_name] = UNIVERSES.pop(old_name)

        if "universes" in self.setup and isinstance(self.setup["universes"], list):
            self.setup["universes"] = [
                new_name if u == old_name else u for u in self.setup["universes"]
            ]

        self.store.rename_universe_in_templates(old_name, new_name)

        checked_names = set()
        for row in range(self.universe_list.count()):
            item = self.universe_list.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                name = item.text()
                if name == old_name:
                    checked_names.add(new_name)
                else:
                    checked_names.add(name)

        self.universe_list.clear()
        for name in UNIVERSES:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            if name in checked_names:
                item.setCheckState(Qt.CheckState.Checked)
            else:
                item.setCheckState(Qt.CheckState.Unchecked)
            self.universe_list.addItem(item)

    def _watchlist_updated(self, name: str, symbols: list[str]) -> None:
        UNIVERSES[name] = symbols

    def _sync_universe_list(self) -> None:
        filter_mode = "GENERAL"
        if hasattr(self, "template_filter_combo"):
            filter_mode = str(self.template_filter_combo.currentData() or "GENERAL")
        checked_names = set()
        for row in range(self.universe_list.count()):
            item = self.universe_list.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                raw_name = item.data(Qt.ItemDataRole.UserRole) or item.text().rsplit(" (", 1)[0]
                checked_names.add(raw_name)

        self.universe_list.clear()
        paper_tab_active = bool(
            hasattr(self, "scan_settings_tabs")
            and hasattr(self, "paper_trading_tab")
            and self.scan_settings_tabs.widget(
                self.scan_settings_tabs.currentIndex()
            ) is self.paper_trading_tab
        )
        for name in UNIVERSES:
            if is_holdings_universe(name) and (
                filter_mode == "GENERAL" or paper_tab_active
            ):
                continue
            symbols = UNIVERSES[name]
            count_str = f" ({len(symbols)})" if isinstance(symbols, list) and len(symbols) > 0 else ""
            display_text = f"{name}{count_str}"
            item = QListWidgetItem(display_text)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            if name in checked_names:
                item.setCheckState(Qt.CheckState.Checked)
            else:
                item.setCheckState(Qt.CheckState.Unchecked)
            self.universe_list.addItem(item)

    def _reset_telegram_baseline(self) -> None:
        if not self.active_template_name:
            QMessageBox.information(
                self,
                "Save the template first",
                "Save this scan as a named template before resetting its Telegram baseline.",
            )
            return
        if QMessageBox.warning(
            self,
            "Reset Telegram alert baseline",
            f'Warning: Resetting the alert baseline for "{self.active_template_name}" '
            "will clear its saved alert history.\n\n"
            "The next scan will treat all matching stocks as a fresh Initial Matching List.\n\n"
            "Are you sure you want to proceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            self.telegram_state_store.clear(self.active_template_name)
        except (TelegramSettingsError, OSError) as exc:
            QMessageBox.warning(self, "Baseline not reset", str(exc))
            return
        self.telegram_status.setText(
            "Telegram alert baseline reset. The next successful scan sends the full list."
        )
        self.telegram_status.setStyleSheet("color: #7f6000; font-size: 11px")

    def _copy_algo_template_to_another_account(self) -> None:
        if (self.worker and self.worker.isRunning()) or (
            self.history_worker and self.history_worker.isRunning()
        ):
            QMessageBox.information(
                self,
                "Scan running",
                "Cancel or finish the running scan before copying templates.",
            )
            return
        if self._has_unparsed_rule_draft("copying template to another account"):
            return
        if not self.active_template_name:
            QMessageBox.information(
                self,
                "Save template first",
                "Save this strategy setup as a template before copying it to another account.",
            )
            return

        accounts = BrokerLoginStore().get_broker_accounts("Zerodha")
        if not accounts:
            QMessageBox.warning(
                self,
                "No Accounts Registered",
                "No Zerodha accounts found. Please log into Zerodha from the Portfolio menu first.",
            )
            return

        acc_options = []
        acc_ids = []
        for acc in accounts:
            u_id = str(acc.get("user_id", "")).strip().upper()
            lbl = str(acc.get("label", f"Zerodha ({u_id})")).strip()
            if u_id:
                acc_options.append(f"👤 {lbl} ({u_id})")
                acc_ids.append(u_id)

        if not acc_ids:
            QMessageBox.warning(self, "No Accounts Found", "No valid Zerodha user accounts found.")
            return

        selected_acc_str, ok = QInputDialog.getItem(
            self,
            "Copy to Another Account",
            "Select target Zerodha Account to copy this strategy to:",
            acc_options,
            0,
            False,
        )
        if not ok or not selected_acc_str:
            return

        sel_idx = acc_options.index(selected_acc_str)
        target_uid = acc_ids[sel_idx]
        target_lbl = selected_acc_str.replace("👤 ", "")

        # Compute suggested name for new copy
        base_name = self.active_template_name or "ALGO Strategy"
        base_clean = re.sub(r"\s*-\s*[A-Z0-9]+$", "", base_name, flags=re.IGNORECASE)
        base_clean = re.sub(r"\s*\((?:Copy|\d+)\)*", "", base_clean, flags=re.IGNORECASE).strip()
        suggested_name = f"{base_clean} - {target_uid}"

        name, accepted = QInputDialog.getText(
            self,
            "Copy Strategy to Account",
            f"Enter new strategy name for account {target_uid}:",
            text=suggested_name,
        )
        if not accepted or not name.strip():
            return

        final_name = name.strip()

        setup = copy.deepcopy(self._current_setup())
        setup["is_algo_template"] = True
        setup["algo_target_account"] = target_uid

        # Portfolio Universe Re-mapping: Always re-map Zerodha portfolio watchlists to target account
        target_wl_name = f"💼 ZERODHA {target_lbl}" if "(" in target_lbl else f"💼 ZERODHA ({target_uid})"
        for k in UNIVERSES.keys():
            if f"({target_uid})" in k and k.startswith("💼 ZERODHA"):
                target_wl_name = k
                break

        current_unis = setup.get("universes", [])
        new_unis = []
        replaced_portfolio_wl = False
        for uni in current_unis:
            if str(uni).startswith("💼 ZERODHA") or str(uni).startswith("💼 Zerodha"):
                new_unis.append(target_wl_name)
                replaced_portfolio_wl = True
            else:
                new_unis.append(uni)

        tact = str(setup.get("algo_trade_action", "")).upper()
        name_upper = final_name.upper()
        is_sell = "SELL" in tact or any(k in name_upper for k in ["EXIT", "SELL", "HEATED", "PROFIT", "STOP LOSS"])
        has_pnl = expression_has_pnl_rules(setup.get("expression"))

        if (is_sell or has_pnl) and not replaced_portfolio_wl:
            new_unis = [target_wl_name]

        setup["universes"] = new_unis

        try:
            self.store.create(final_name, setup)
        except TemplateStoreError as exc:
            QMessageBox.warning(self, "Copy failed", str(exc))
            return

        self._store_error = None
        self.active_template_name = final_name
        self._refresh_templates()
        self.template_combo.setCurrentText(final_name)
        self._load_setup(setup)
        self._set_dirty(False)

        QMessageBox.information(
            self,
            "Strategy Copied to Account",
            f'Successfully copied strategy to account {target_uid} as "{final_name}"!\n\n'
            f'Target Account: {target_lbl}\n'
            f'Universe Selection: Portfolio watchlists re-mapped to {target_wl_name}.\n'
            f'Automated ALGO Order Placement is OFF by default for trader safety.',
        )

    def _promote_general_scan_to_algo_template(self) -> None:
        if (self.worker and self.worker.isRunning()) or (
            self.history_worker and self.history_worker.isRunning()
        ):
            QMessageBox.information(
                self,
                "Scan running",
                "Cancel or finish the running scan before promoting templates.",
            )
            return
        if self._has_unparsed_rule_draft("promoting general scan to ALGO strategy"):
            return

        accounts = BrokerLoginStore().get_broker_accounts("Zerodha")
        if not accounts:
            QMessageBox.warning(
                self,
                "No Accounts Registered",
                "No Zerodha accounts found. Please log into Zerodha from the Portfolio menu first.",
            )
            return

        acc_options = []
        acc_ids = []
        for acc in accounts:
            u_id = str(acc.get("user_id", "")).strip().upper()
            lbl = str(acc.get("label", f"Zerodha ({u_id})")).strip()
            if u_id:
                acc_options.append(f"👤 {lbl} ({u_id})")
                acc_ids.append(u_id)

        if not acc_ids:
            QMessageBox.warning(self, "No Accounts Found", "No valid Zerodha user accounts found.")
            return

        selected_acc_str, ok = QInputDialog.getItem(
            self,
            "Target Account Selection",
            "Select Zerodha Account to bind this promoted ALGO Strategy to:",
            acc_options,
            0,
            False,
        )
        if not ok or not selected_acc_str:
            return

        sel_idx = acc_options.index(selected_acc_str)
        target_uid = acc_ids[sel_idx]
        target_lbl = selected_acc_str.replace("👤 ", "")

        # Ask explicitly whether BUY or SELL strategy
        action_options = [
            "🛒 BUY (Entry Order - Budget Based)",
            "🔴 SELL (Exit Order - Portfolio Holdings Guard)",
        ]
        selected_action_str, ok_act = QInputDialog.getItem(
            self,
            "Trade Action Selection",
            f"Is this promoted strategy a BUY (Entry) or SELL (Exit) scan for account {target_uid}?",
            action_options,
            0,
            False,
        )
        if not ok_act or not selected_action_str:
            return

        is_sell = "SELL" in selected_action_str
        full_trade_action = (
            "SELL (Exit Order - Portfolio Holdings Guard)"
            if is_sell
            else "BUY (Entry Order - Budget Based)"
        )
        action_tag = "SELL" if is_sell else "BUY"

        # Compute suggested template name
        base_name = self.active_template_name or "General Scan"
        base_clean = re.sub(r"\s*-\s*[A-Z0-9]+$", "", base_name, flags=re.IGNORECASE)
        base_clean = re.sub(r"\s*\[(?:BUY|SELL)\]\s*", "", base_clean, flags=re.IGNORECASE)
        base_clean = re.sub(r"\s*\((?:Copy|\d+)\)*", "", base_clean, flags=re.IGNORECASE).strip()
        suggested_name = f"[{action_tag}] {base_clean} - {target_uid}"

        name, accepted = QInputDialog.getText(
            self,
            "Promote General Scan to ALGO Strategy",
            f"Enter new ALGO strategy name for account {target_uid}:",
            text=suggested_name,
        )
        if not accepted or not name.strip():
            return

        final_name = name.strip()

        setup = copy.deepcopy(self._current_setup())
        setup["is_algo_template"] = True
        setup["algo_target_account"] = target_uid
        setup["algo_trade_action"] = full_trade_action

        # Portfolio Universe Re-mapping
        target_wl_name = f"💼 ZERODHA {target_lbl}" if "(" in target_lbl else f"💼 ZERODHA ({target_uid})"
        for k in UNIVERSES.keys():
            if f"({target_uid})" in k and k.startswith("💼 ZERODHA"):
                target_wl_name = k
                break

        current_unis = setup.get("universes", [])
        new_unis = []
        replaced_portfolio_wl = False
        for uni in current_unis:
            if str(uni).startswith("💼 ZERODHA") or str(uni).startswith("💼 Zerodha"):
                new_unis.append(target_wl_name)
                replaced_portfolio_wl = True
            else:
                new_unis.append(uni)

        has_pnl = expression_has_pnl_rules(setup.get("expression"))
        if (is_sell or has_pnl) and not replaced_portfolio_wl:
            new_unis = [target_wl_name]

        setup["universes"] = new_unis

        try:
            self.store.create(final_name, setup)
        except TemplateStoreError as exc:
            QMessageBox.warning(self, "Promotion failed", str(exc))
            return

        self._store_error = None
        self.active_template_name = final_name

        # Switch category view to ALGO Execution Only
        if hasattr(self, "template_filter_combo"):
            idx = self.template_filter_combo.findData("ALGO")
            if idx >= 0:
                self.template_filter_combo.blockSignals(True)
                self.template_filter_combo.setCurrentIndex(idx)
                self.template_filter_combo.blockSignals(False)

        self._refresh_templates()
        self.template_combo.setCurrentText(final_name)
        self._load_setup(setup)
        self._set_dirty(False)

        QMessageBox.information(
            self,
            "General Scan Promoted to ALGO",
            f'General scan promoted successfully to ALGO Strategy "{final_name}"!\n\n'
            f'Target Account: {target_lbl}\n'
            f'Trade Action: {full_trade_action}\n'
            f'Universe Selection: {", ".join(new_unis) or "Default"}\n\n'
            f'Active account banner and live Zerodha funds have been updated for {target_uid}.\n'
            f'Automated ALGO Order Placement is OFF by default for trader safety.',
        )

    def _export_template_file(self) -> None:
        if not self.active_template_name:
            QMessageBox.information(
                self,
                "Save template first",
                "Save this scan as a named template before exporting it to a shareable file.",
            )
            return
        setup = self._current_setup()
        local_watchlists = self.watchlist_store.load_all()
        custom_watchlists_used = [
            uni for uni in setup.get("universes", []) if uni in local_watchlists
        ]

        dialog = ExportTemplateDialog(
            self.active_template_name, custom_watchlists_used, self
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        include_watchlists = dialog.should_include_watchlists()

        default_filename = f"{self.active_template_name.replace(' ', '_')}.json"
        path_str, _filter = QFileDialog.getSaveFileName(
            self,
            "Export Strategy Template",
            default_filename,
            "JSON Template Files (*.json);;All Files (*.*)",
        )
        if not path_str:
            return
        export_path = Path(path_str)
        try:
            export_template_json(
                self.active_template_name,
                setup,
                export_path,
                include_watchlists=include_watchlists,
                watchlist_store=self.watchlist_store,
            )
            QMessageBox.information(
                self,
                "Template Exported",
                f'Strategy template "{self.active_template_name}" was exported successfully to:\n\n{export_path}',
            )
        except Exception as exc:
            QMessageBox.critical(
                self, "Export failed", f"Could not export template file: {exc}"
            )

    def _process_imported_setup_universes(
        self, setup: dict[str, Any], name: str = ""
    ) -> tuple[dict[str, Any], str | None]:
        """Processes bundled custom watchlists or falls back to NIFTY 500 if custom watchlist is missing/excluded."""
        notice = None
        bundled_watchlists = setup.pop("_bundled_custom_watchlists", {})
        if isinstance(bundled_watchlists, dict) and bundled_watchlists:
            local_wl = self.watchlist_store.load_all()
            for wl_name, symbols in bundled_watchlists.items():
                if isinstance(symbols, list):
                    local_wl[wl_name] = symbols
                    UNIVERSES[wl_name] = symbols
            self.watchlist_store.save(local_wl)
            self._sync_universe_list()

        unis = setup.get("universes", [])
        valid_unis = []
        missing_custom_wl = False
        all_wl = self.watchlist_store.load_all()
        for uni in unis:
            if uni in UNIVERSES or uni in all_wl:
                valid_unis.append(uni)
            else:
                missing_custom_wl = True

        if missing_custom_wl or not valid_unis:
            setup["universes"] = ["NIFTY 500"]
            notice = (
                "Custom watchlist ticker data was excluded for privacy by the author. "
                "The active universe has been set to NIFTY 500."
            )
        else:
            setup["universes"] = valid_unis

        setup, _changed, removed = normalize_general_universes(name, setup)
        if removed:
            notice = (
                (notice + " ") if notice else ""
            ) + (
                f"Zerodha holdings watchlists are reserved for ALGO templates and were "
                f"removed from this General template: {', '.join(removed)}."
            )

        return setup, notice

    def _import_template_file(self) -> None:
        path_str, _filter = QFileDialog.getOpenFileName(
            self,
            "Import Strategy Template",
            "",
            "JSON Template Files (*.json);;All Files (*.*)",
        )
        if not path_str:
            return
        import_path = Path(path_str)
        try:
            templates_map = import_template_json(import_path)
        except TemplateStoreError as exc:
            QMessageBox.warning(self, "Import failed", str(exc))
            return

        all_templates = self.store.load_all()

        if len(templates_map) == 1:
            orig_name, setup = next(iter(templates_map.items()))
            setup, notice = self._process_imported_setup_universes(setup, orig_name)
            target_name = orig_name
            if target_name in all_templates:
                copy_name = f"{target_name} (Imported)"
                msg = QMessageBox(self)
                msg.setIcon(QMessageBox.Icon.Question)
                msg.setWindowTitle("Template Already Exists")
                msg.setText(
                    f'A template named "{target_name}" already exists in your scanner.\n\n'
                    "How would you like to proceed?"
                )
                overwrite_btn = msg.addButton(
                    "Overwrite Existing", QMessageBox.ButtonRole.AcceptRole
                )
                copy_btn = msg.addButton(
                    f'Save as Copy ("{copy_name}")', QMessageBox.ButtonRole.ActionRole
                )
                cancel_btn = msg.addButton(
                    "Cancel", QMessageBox.ButtonRole.RejectRole
                )
                msg.exec()

                clicked = msg.clickedButton()
                if clicked == cancel_btn:
                    return
                elif clicked == copy_btn:
                    target_name = copy_name

            try:
                self.store.create(target_name, setup)
                self._refresh_templates()
                self.template_combo.setCurrentText(target_name)
                self._load_template(target_name)
                msg_text = f'Successfully imported template "{target_name}"!\n\nIt is now active and ready to run.'
                if notice:
                    msg_text += f"\n\nℹ️ {notice}"
                QMessageBox.information(
                    self,
                    "Template Imported",
                    msg_text,
                )
            except TemplateStoreError as exc:
                QMessageBox.warning(self, "Import failed", str(exc))
        else:
            dialog = ImportBundleDialog(templates_map, set(all_templates), self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                selected_map = dialog.get_selected_templates()
                if not selected_map:
                    return
                imported_count = 0
                first_imported_name = None
                has_notice = False
                for raw_name, raw_setup in selected_map.items():
                    setup, notice = self._process_imported_setup_universes(raw_setup, raw_name)
                    if notice:
                        has_notice = True
                    tag_name = raw_name if raw_name.endswith("(Imported)") else f"{raw_name} (Imported)"
                    if tag_name in all_templates:
                        self.store.save_edit(tag_name, setup)
                    else:
                        self.store.create(tag_name, setup)
                    imported_count += 1
                    if first_imported_name is None:
                        first_imported_name = tag_name

                self._refresh_templates()
                if first_imported_name:
                    self.template_combo.setCurrentText(first_imported_name)
                    self._load_template(first_imported_name)
                msg_text = (
                    f'Successfully imported {imported_count} strategy templates tagged with "(Imported)"!\n\n'
                    f'Loaded and activated "{first_imported_name}".'
                )
                if has_notice:
                    msg_text += "\n\nℹ️ Note: Custom watchlist data was excluded for privacy by the author. Active universe(s) set to NIFTY 500."
                QMessageBox.information(
                    self,
                    "Templates Imported",
                    msg_text,
                )

    @staticmethod
    def _table_visual_headers(table: QTableWidget) -> list[str]:
        header = table.horizontalHeader()
        names: list[str] = []
        for visual in range(header.count()):
            logical = header.logicalIndex(visual)
            item = table.horizontalHeaderItem(logical)
            if item is not None:
                names.append(item.text())
        return names

    def _apply_visual_header_order(
        self, table: QTableWidget, ordered_names: Iterable[str]
    ) -> None:
        header = table.horizontalHeader()
        logical_by_name = {
            table.horizontalHeaderItem(logical).text(): logical
            for logical in range(table.columnCount())
            if table.horizontalHeaderItem(logical) is not None
        }
        desired = [name for name in ordered_names if name in logical_by_name]
        desired.extend(name for name in logical_by_name if name not in desired)
        previous_guard = self._moving_result_columns
        self._moving_result_columns = True
        try:
            for target_visual, name in enumerate(desired):
                current_visual = header.visualIndex(logical_by_name[name])
                if current_visual != target_visual:
                    header.moveSection(current_visual, target_visual)
        finally:
            self._moving_result_columns = previous_guard

    def _result_column_moved(
        self,
        table: QTableWidget,
        _logical: int,
        _old_visual: int,
        _new_visual: int,
    ) -> None:
        if self._moving_result_columns or self._loading:
            return
        visible_order = self._table_visual_headers(table)
        self.setup["result_column_order"] = merge_visible_result_order(
            self.setup, visible_order
        )
        for completed_setup in (self.last_scan_setup, self.last_history_setup):
            if completed_setup is not None:
                completed_setup["result_column_order"] = merge_visible_result_order(
                    completed_setup, visible_order
                )
        other = self.history_results if table is self.results else self.results
        self._apply_visual_header_order(other, visible_order)
        selected_columns = {
            self.result_columns_list.item(i).text()
            for i in range(self.result_columns_list.count())
            if self.result_columns_list.item(i).checkState()
            == Qt.CheckState.Checked
        }
        self._refresh_result_columns(selected_columns, dynamic_saved=True)
        self._set_dirty(True)

    def _reset_column_order(self, *_args: Any) -> None:
        current_default = normalized_result_column_order(
            {"expression": self.setup["expression"], "result_column_order": []}
        )
        self.setup["result_column_order"] = current_default
        for completed_setup in (self.last_scan_setup, self.last_history_setup):
            if completed_setup is not None:
                completed_setup["result_column_order"] = normalized_result_column_order(
                    {
                        "expression": completed_setup["expression"],
                        "result_column_order": [],
                    }
                )
        self._apply_visual_header_order(self.results, current_default)
        self._apply_visual_header_order(self.history_results, current_default)
        selected_columns = {
            self.result_columns_list.item(i).text()
            for i in range(self.result_columns_list.count())
            if self.result_columns_list.item(i).checkState()
            == Qt.CheckState.Checked
        }
        self._refresh_result_columns(selected_columns, dynamic_saved=True)
        self._set_dirty(True)

    def _history_period_changed(self, *_args: Any) -> None:
        if self._loading:
            return
        self.setup = self._current_setup()
        self._set_dirty(True)
        self._mark_history_stale(self.last_history_report is not None)

    def _update_copy_settings_all_enabled(self, tab_index: int) -> None:
        """Only allow bulk copying from the general Scan & Telegram view."""
        is_algo_tab = (
            self.scan_settings_tabs.widget(tab_index)
            is self.algo_order_settings_tab
        )
        is_paper_tab = (
            hasattr(self, "paper_trading_tab")
            and self.scan_settings_tabs.widget(tab_index) is self.paper_trading_tab
        )
        is_algo_filter = (
            hasattr(self, "template_filter_combo")
            and str(self.template_filter_combo.currentData() or "GENERAL") == "ALGO"
        )
        self.copy_settings_all_btn.setEnabled(
            not is_algo_tab and not is_paper_tab and not is_algo_filter
        )
        if is_algo_filter:
            self.copy_settings_all_btn.setToolTip(
                "Bulk copying is unavailable in ALGO Execution Only mode. "
                "Switch to General Scans Only to copy scan settings."
            )
        elif is_paper_tab:
            self.copy_settings_all_btn.setToolTip(
                "Bulk copying is unavailable on the Paper Trading workspace."
            )
        elif is_algo_tab:
            self.copy_settings_all_btn.setToolTip(
                "Bulk copying is unavailable on ALGO Order Execution Settings. "
                "Switch to Scan & Telegram Settings to copy scan settings."
            )
        else:
            self.copy_settings_all_btn.setToolTip(
                "Apply current Section 3 scan settings (refresh rate, Telegram "
                "alert modes, adjusted prices, incomplete bars, result columns, "
                "and universes) to all saved strategy templates."
            )

    def _on_scan_settings_tab_changed(self, tab_index: int) -> None:
        self._update_copy_settings_all_enabled(tab_index)
        is_paper_tab = (
            hasattr(self, "paper_trading_tab")
            and self.scan_settings_tabs.widget(tab_index) is self.paper_trading_tab
        )
        if is_paper_tab:
            if hasattr(self, "paper_pipeline_account_combo"):
                self._refresh_paper_pipeline_options()
            if hasattr(self, "scan_button"):
                self.scan_button.setEnabled(False)
            self._sync_universe_list()
            return
        if hasattr(self, "scan_button"):
            self.scan_button.setEnabled(True)
        is_algo_tab = (
            self.scan_settings_tabs.widget(tab_index) is self.algo_order_settings_tab
        )
        is_algo_filter = (
            hasattr(self, "template_filter_combo")
            and str(self.template_filter_combo.currentData() or "GENERAL") == "ALGO"
        )
        if not is_algo_tab or is_algo_filter:
            return
        if not getattr(self, "_algo_tab_hint_shown", False):
            QMessageBox.information(
                self,
                "ALGO settings",
                "ALGO order execution and paper-trade simulation settings apply to ALGO "
                "strategies.\n\n"
                "Switched the template filter to '🤖 ALGO Execution Only' and loaded the "
                "first ALGO strategy.",
            )
            self._algo_tab_hint_shown = True
        idx = self.template_filter_combo.findData("ALGO")
        if idx >= 0:
            self.template_filter_combo.setCurrentIndex(idx)

    def _open_strategy_guide_window(self) -> None:
        """Move the existing guide page into a full-size top-level window."""
        existing = self._strategy_guide_window
        if existing is not None:
            existing.showMaximized()
            existing.raise_()
            existing.activateWindow()
            return
        page = self.paper_trading_tab
        tab_index = self.scan_settings_tabs.indexOf(page)
        if tab_index < 0:
            return
        self._strategy_guide_tab_index = tab_index
        self.scan_settings_tabs.removeTab(tab_index)
        window = StrategyGuideWindow(self)
        window.setCentralWidget(page)
        # QTabWidget.removeTab() leaves the detached page hidden. Reparenting it
        # as a central widget does not reliably clear that hidden state.
        page.setVisible(True)
        window.return_requested.connect(self._restore_strategy_guide_tab)
        self._strategy_guide_window = window
        self.open_strategy_guide_btn.setEnabled(False)
        self.open_strategy_guide_btn.setText("Strategy Guide window is open")
        self._refresh_paper_pipeline_options()
        window.showMaximized()

    def _restore_strategy_guide_tab(self) -> None:
        """Return the same live guide page to Section 3 when its window closes."""
        window = self._strategy_guide_window
        if window is None:
            return
        page = window.takeCentralWidget() or self.paper_trading_tab
        if self.scan_settings_tabs.indexOf(page) < 0:
            index = min(
                max(0, int(self._strategy_guide_tab_index)),
                self.scan_settings_tabs.count(),
            )
            self.scan_settings_tabs.insertTab(index, page, "🧭 Strategy Guide")
        page.setVisible(True)
        self._strategy_guide_window = None
        self.open_strategy_guide_btn.setEnabled(True)
        self.open_strategy_guide_btn.setText("↗ Open Strategy Guide Window")
        self.scan_settings_tabs.setCurrentWidget(page)

    def _copy_scan_settings_to_all_templates(self) -> None:
        if self._has_unparsed_rule_draft("copying scan settings to all templates"):
            return
        all_templates = self.store.load_all()
        count = len(all_templates)
        if count == 0:
            QMessageBox.information(self, "No Templates", "No saved strategy templates found.")
            return

        active_name = self.active_template_name or "Current"
        reply = QMessageBox.question(
            self,
            "Copy Scan Settings to All Templates",
            f"Are you sure you want to copy the Section 3 scan settings from '{active_name}' to ALL {count} saved strategy templates?\n\n"
            f"This will update Telegram alert settings, adjusted prices, incomplete bars, result columns, and universe selection across all templates.\n\n"
            f"Strategy rules (expressions) for each template will remain untouched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        current_setup = self._current_setup()
        try:
            validate_ranking_setup(current_setup)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid ranking setup", str(exc))
            return
        keys_to_copy = [
            "as_of_mode",
            "as_of_date",
            "adjusted",
            "include_current",
            "auto_run",
            "auto_run_start_time",
            "auto_run_end_time",
            "auto_run_trading_days_only",
            "telegram_alerts_enabled",
            "telegram_auto_reset_daily",
            "telegram_alert_mode",
            "result_columns",
            "result_column_order",
            "universes",
            "custom_symbols",
            "ranking_enabled",
            "ranking_rsi_periods",
            "ranking_direction",
            "ranking_top_n",
            "ranking_watchlist_enabled",
            "ranking_watchlist_name",
        ]

        updated_count = 0
        for name, t_setup in all_templates.items():
            new_setup = copy.deepcopy(t_setup)
            for k in keys_to_copy:
                if k in current_setup:
                    new_setup[k] = copy.deepcopy(current_setup[k])
            self.store.save_edit(name, new_setup)
            updated_count += 1

        self.setup = copy.deepcopy(current_setup)
        self._set_dirty(False)

        QMessageBox.information(
            self,
            "Scan Settings Copied",
            f"Successfully applied scan settings from '{active_name}' to all {updated_count} strategy templates!",
        )
    def _update_ranking_controls(self, mark_dirty: bool = True) -> None:
        enabled = self.ranking_enabled_cb.isChecked()
        self.ranking_rsi_periods_edit.setEnabled(enabled)
        self.ranking_direction_combo.setEnabled(enabled)
        self.ranking_top_n_spin.setEnabled(enabled)
        self.ranking_watchlist_cb.setEnabled(enabled)
        self.ranking_watchlist_name_edit.setEnabled(
            enabled and self.ranking_watchlist_cb.isChecked()
        )
        try:
            periods = parse_ranking_rsi_periods_strict(
                self.ranking_rsi_periods_edit.text()
            )
            formula = " + ".join(f"RSI({period})" for period in periods)
            direction = (
                "descending" if str(self.ranking_direction_combo.currentData()) == "DESC"
                else "ascending"
            )
            top_n = self.ranking_top_n_spin.value()
            suffix = f"; keep Top {top_n}" if top_n else "; keep all matches"
            self.ranking_formula_lbl.setText(
                f"Score = ({formula}) ÷ {len(periods)}; order {direction}{suffix}."
            )
            self.ranking_formula_lbl.setStyleSheet("color: #5d4037; font-size: 10px;")
        except ValueError as exc:
            self.ranking_formula_lbl.setText(f"⚠ {exc}")
            self.ranking_formula_lbl.setStyleSheet("color: #b71c1c; font-size: 10px;")
        if mark_dirty and not self._loading:
            selected_columns = {
                self.result_columns_list.item(i).text()
                for i in range(self.result_columns_list.count())
                if self.result_columns_list.item(i).checkState() == Qt.CheckState.Checked
            }
            current = self._current_setup()
            self.setup.update(current)
            self._refresh_result_columns(selected_columns, dynamic_saved=True)
            self._set_dirty(True)
            self._mark_stale(True)
            self._mark_history_stale(True)

    def _ranking_settings_changed(self, *_args: Any) -> None:
        self._update_ranking_controls(mark_dirty=True)

    def _settings_changed(self, *_args: Any) -> None:
        if self._loading or self._refreshing_result_columns:
            return
        self.setup = self._current_setup()
        self._set_dirty(True)
        self._mark_stale(True)
        self._mark_history_stale(True)

    def _parse_auto_run_window(self) -> tuple[time | None, time | None, bool]:
        setup = getattr(self, "setup", None) or {}
        if hasattr(self, "auto_run_start_edit"):
            start_str = self.auto_run_start_edit.time().toString("HH:mm")
            end_str = self.auto_run_end_edit.time().toString("HH:mm")
            trading_only = self.auto_run_trading_days_only.isChecked()
        else:
            start_str = str(setup.get("auto_run_start_time", "09:15"))
            end_str = str(setup.get("auto_run_end_time", "15:30"))
            trading_only = bool(setup.get("auto_run_trading_days_only", True))
        try:
            start_time = datetime.strptime(start_str, "%H:%M").time()
        except (TypeError, ValueError):
            start_time = time(9, 15)
        try:
            end_time = datetime.strptime(end_str, "%H:%M").time()
        except (TypeError, ValueError):
            end_time = time(15, 30)
        return start_time, end_time, trading_only

    def _within_auto_run_window(self, now: datetime) -> bool:
        start_time, end_time, trading_only = self._parse_auto_run_window()
        if end_time <= start_time:
            return False
        if not (start_time <= now.time() <= end_time):
            return False
        if trading_only and not NSE_CALENDAR.is_session_date(now.date()):
            return False
        return True

    def _next_auto_run_due(self, now: datetime) -> datetime | None:
        start_time, _end_time, trading_only = self._parse_auto_run_window()
        if _end_time <= start_time:
            return None
        if self._within_auto_run_window(now):
            return now
        candidate = now.replace(
            hour=start_time.hour, minute=start_time.minute, second=0, microsecond=0
        )
        if now.time() >= start_time:
            candidate = candidate + timedelta(days=1)
        if trading_only:
            guard = 0
            while not NSE_CALENDAR.is_session_date(candidate.date()):
                candidate = candidate + timedelta(days=1)
                guard += 1
                if guard > 14:
                    return None
        return candidate

    def _auto_run_window_summary(self) -> str:
        start_time, end_time, trading_only = self._parse_auto_run_window()
        label = f"{start_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')}"
        if trading_only:
            label += " (trading days)"
        return label

    def _update_clock(self) -> None:
        if not hasattr(self, "clock_time_lbl"):
            return
        now = get_ist_now()
        self.clock_time_lbl.setText(
            f"{now.strftime('%H:%M:%S')} IST  ·  {now.strftime('%a %d %b %Y')}"
        )
        if not NSE_CALENDAR.is_session_date(now.date()):
            market_text = "● NSE HOLIDAY"
            market_color = "#d35400"
        elif nse_market_is_open(now):
            market_text = "● MARKET OPEN"
            market_color = "#1b5e20"
        else:
            market_text = "● MARKET CLOSED"
            market_color = "#c0392b"
        auto_text = ""
        if hasattr(self, "auto_run") and self.auto_run.isChecked():
            if not self._within_auto_run_window(now):
                next_due = self._next_auto_run_due(now)
                if next_due is not None:
                    auto_text = f"  Auto-run paused until {next_due.strftime('%H:%M')}"
                else:
                    auto_text = "  Auto-run paused (invalid window)"
            else:
                auto_text = "  Auto-run active"
        self.market_status_lbl.setText(market_text + auto_text)
        self.market_status_lbl.setStyleSheet(
            f"font-size: 14px; font-weight: 600; color: {market_color}"
        )

    def _auto_run_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        if checked:
            self._schedule_auto_scan(immediate=False)
        else:
            self.auto_scan_timer.stop()

    def _auto_run_mode_changed(self, latest: bool) -> None:
        if self._loading:
            return
        if not latest:
            self.auto_scan_timer.stop()
        elif self.auto_run.isChecked():
            self._schedule_auto_scan(immediate=False)

    def _auto_run_window_changed(self, *_args: Any) -> None:
        if self._loading:
            return
        start_time, end_time, _trading_only = self._parse_auto_run_window()
        if end_time <= start_time:
            self.auto_run_window_hint.setText(
                "Window invalid: end time must be after start time. Auto-run is paused."
            )
            self.auto_scan_timer.stop()
        else:
            self.auto_run_window_hint.setText("")
            if self.auto_run.isChecked():
                self._schedule_auto_scan(immediate=False)
        self._update_clock()

    def _schedule_auto_scan(self, immediate: bool = False) -> None:
        self.auto_scan_timer.stop()
        if (
            not hasattr(self, "auto_run")
            or not self.auto_run.isChecked()
            or not self.latest.isChecked()
        ):
            return
        if (self.worker and self.worker.isRunning()) or (
            self.history_worker and self.history_worker.isRunning()
        ):
            # The appropriate thread-finished handler schedules the next run.
            return
        now = get_ist_now()
        next_due = self._next_auto_run_due(now)
        if next_due is None:
            return
        if self._within_auto_run_window(now):
            delay_ms = 0 if immediate else self._global_refresh_minutes() * 60_000
        else:
            delay_ms = max(1_000, int((next_due - now).total_seconds() * 1000))
        self.auto_scan_timer.start(delay_ms)

    def _global_refresh_minutes(self) -> int:
        try:
            return int(self.app_settings.load().get("data_refresh_minutes", DEFAULT_REFRESH_MINUTES))
        except Exception:
            return DEFAULT_REFRESH_MINUTES

    def _master_data_freshness_changed(self, *_args: Any) -> None:
        minutes = int(
            self.master_refresh_interval.currentData() or DEFAULT_REFRESH_MINUTES
        )
        force_fresh = self.master_force_fresh.isChecked()
        try:
            self.app_settings.save(minutes, force_fresh)
        except Exception as exc:
            print(f"App settings save failed: {exc}")
            return
        if not self._loading and self.auto_run.isChecked():
            self._schedule_auto_scan(immediate=False)

    def _auto_run_due(self) -> None:
        if not self.auto_run.isChecked() or not self.latest.isChecked():
            return
        if (self.worker and self.worker.isRunning()) or (
            self.history_worker and self.history_worker.isRunning()
        ):
            return
        if not self._within_auto_run_window(get_ist_now()):
            self._schedule_auto_scan(immediate=False)
            return
        self._start_scan()
        if self.worker is None:
            # Invalid rules or a missing universe already produced a clear
            # message. Stop automatic retries from repeating that dialog.
            self.auto_run.setChecked(False)

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        if hasattr(self, "unsaved_label"):
            self.unsaved_label.setVisible(dirty)

    def _confirm_discard_unsaved(self, action: str) -> bool:
        if not self._dirty:
            return True
        template = self.active_template_name or "the current unsaved setup"
        return (
            QMessageBox.question(
                self,
                "Unsaved template changes",
                f'Changes to "{template}" have not been saved. Discard them and {action}?',
            )
            == QMessageBox.StandardButton.Yes
        )

    def _mark_stale(self, stale: bool) -> None:
        self._stale = stale
        self.stale_label.setVisible(stale)
        if stale and self.last_rows:
            self.counts.setText("Displayed results are stale. Rerun to apply the changed setup.")

    def _mark_history_stale(self, stale: bool) -> None:
        self._history_stale = stale
        if hasattr(self, "history_stale_label"):
            self.history_stale_label.setVisible(stale)
        if stale and self.last_history_report is not None:
            self.history_counts.setText(
                "Displayed history is stale. Rerun to apply the changed setup."
            )

    def _active_tree(self) -> QTreeWidget:
        if (
            hasattr(self, "expanded_editor_dialog")
            and self.expanded_editor_dialog
            and self.expanded_editor_dialog.isVisible()
        ):
            return self.expanded_editor_dialog.tree
        return self.tree

    def _rebuild_tree(self, select_path: tuple[int, ...] | None = None) -> None:
        self.tree.clear()
        root = self._make_tree_item(self.setup["expression"], ())
        self.tree.addTopLevelItem(root)
        root.setExpanded(True)
        self.tree.expandAll()
        if select_path is not None:
            selected = self._tree_item_at(select_path, target_tree=self.tree)
            if selected is not None:
                self.tree.setCurrentItem(selected)
                self.tree.scrollToItem(selected)
        self.expression_label.setText(expression_text(self.setup["expression"]))
        self._refresh_result_columns()

    def _make_tree_item(
        self,
        node: dict[str, Any],
        path: tuple[int, ...],
        ancestor_enabled: bool = True,
    ) -> QTreeWidgetItem:
        own_enabled = node_enabled(node)
        effective_enabled = ancestor_enabled and own_enabled
        branch_has_rule = node["type"] == "rule" or has_active_rule(node)
        display_active = effective_enabled and branch_has_rule
        if node["type"] == "group":
            op = str(node.get("op", "AND"))
            if op == "AND":
                text = "AND — Stock passes ALL of the below"
            elif op == "OR":
                text = "OR — Stock passes ANY 1 of the below"
            elif op.startswith("ANY "):
                try:
                    k = op.split()[1]
                except IndexError:
                    k = "1"
                text = f"{op} — Stock passes ANY {k} of the below"
            else:
                text = f"{op} group"
            if not node["children"]:
                text += " (empty - select this group, then add rules)"
            elif effective_enabled and not branch_has_rule:
                text += " (no enabled rules)"
        else:
            text = expression_text(node)
        item = QTreeWidgetItem(["", text])
        item.setIcon(0, eye_switch_icon(own_enabled))
        if own_enabled and effective_enabled and branch_has_rule:
            switch_help = "Open eye: this item is enabled. Click to disable it temporarily."
        elif own_enabled and effective_enabled:
            switch_help = "Open eye, but this group currently contains no enabled rules."
        elif own_enabled:
            switch_help = "Enabled, but inactive because a parent group is disabled."
        else:
            switch_help = "Closed eye: this item is disabled. Click to enable it."
        item.setToolTip(0, switch_help)
        item.setData(0, Qt.ItemDataRole.UserRole, path)
        item.setData(1, Qt.ItemDataRole.UserRole, path)

        font = item.font(1)
        if not display_active or not own_enabled:
            dim_color = QColor("#888888")
            item.setForeground(0, dim_color)
            item.setForeground(1, dim_color)
            font.setItalic(True)
            font.setBold(False)
            item.setFont(1, font)
        else:
            font.setItalic(False)
            if node["type"] == "group":
                font.setBold(True)
                group_color = QColor("#9c5700" if not node["children"] else "#17365d")
                item.setForeground(0, group_color)
                item.setForeground(1, group_color)
            else:
                font.setBold(False)
                rule_color = QColor("#222222")
                item.setForeground(0, rule_color)
                item.setForeground(1, rule_color)
            item.setFont(1, font)

        if node["type"] == "group":
            for index, child in enumerate(node["children"]):
                item.addChild(
                    self._make_tree_item(
                        child,
                        path + (index,),
                        effective_enabled,
                    )
                )
        return item

    def _tree_item_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0:
            return
        path = tuple(item.data(0, Qt.ItemDataRole.UserRole) or ())
        node = self._node_at(path)
        node["enabled"] = not node_enabled(node)
        self._tree_changed(path)

    def _tree_item_at(
        self, path: tuple[int, ...], target_tree: QTreeWidget | None = None
    ) -> QTreeWidgetItem | None:
        tree = target_tree if target_tree is not None else self._active_tree()
        item = tree.topLevelItem(0)
        if item is None:
            return None
        for index in path:
            item = item.child(index)
            if item is None:
                return None
        return item

    def _node_at(self, path: tuple[int, ...]) -> dict[str, Any]:
        node = self.setup["expression"]
        for index in path:
            node = node["children"][index]
        return node

    def _parent_and_index(self, path: tuple[int, ...]) -> tuple[dict[str, Any], int]:
        if not path:
            raise ValueError("The root group cannot be deleted or replaced.")
        return self._node_at(path[:-1]), path[-1]

    def _selected_path(self) -> tuple[int, ...]:
        active_tree = self._active_tree()
        item = active_tree.currentItem()
        if not item:
            return ()
        data = item.data(0, Qt.ItemDataRole.UserRole)
        return tuple(data) if data is not None else ()

    def _target_group(self) -> dict[str, Any]:
        return self._node_at(self._target_group_path())

    def _target_group_path(self) -> tuple[int, ...]:
        path = self._selected_path()
        node = self._node_at(path)
        if node["type"] == "group":
            return path
        return path[:-1]

    def _add_rule(self) -> None:
        dialog = RuleDialog(parent=self)
        if dialog.exec():
            target_path = self._target_group_path()
            target = self._node_at(target_path)
            new_path = target_path + (len(target["children"]),)
            target["children"].append(dialog.result_rule())
            self._tree_changed(new_path)

    def _add_group(self, op: str) -> None:
        target_path = self._target_group_path()
        target = self._node_at(target_path)
        new_path = target_path + (len(target["children"]),)
        target["children"].append(group_node(op))
        self._tree_changed(new_path)

    def _show_tree_context_menu(self, pos: QPoint) -> None:
        item = self.tree.itemAt(pos)
        if not item:
            return
        path = tuple(item.data(0, Qt.ItemDataRole.UserRole) or ())
        node = self._node_at(path)

        menu = QMenu(self)

        if node["type"] == "group":
            add_rule_action = menu.addAction("➕ Add Rule Inside Group")
            add_rule_action.triggered.connect(lambda: self._add_rule_to_path(path))

            sub_group_menu = menu.addMenu("📁 Add Subgroup")
            for op, label in [
                ("AND", "AND (Pass ALL)"),
                ("OR", "OR (Pass ANY 1)"),
                ("ANY 2", "ANY 2 (Pass at least 2)"),
                ("ANY 3", "ANY 3 (Pass at least 3)"),
                ("ANY 4", "ANY 4 (Pass at least 4)"),
                ("ANY 5", "ANY 5 (Pass at least 5)"),
            ]:
                act = sub_group_menu.addAction(f"Add {label}")
                act.triggered.connect(lambda _checked, o=op: self._add_group_to_path(path, o))
            menu.addSeparator()

        edit_action = menu.addAction("✏ Edit / Change Type")
        edit_action.triggered.connect(self._edit_node)

        enabled = node_enabled(node)
        toggle_label = "👁‍🗨 Disable Item" if enabled else "👁 Enable Item"
        toggle_action = menu.addAction(toggle_label)
        toggle_action.triggered.connect(lambda: self._toggle_node_path(path))

        copy_action = menu.addAction("📋 Copy Filter to Draft")
        copy_action.triggered.connect(self._copy_to_draft)

        dup_action = menu.addAction("📄 Duplicate")
        dup_action.triggered.connect(self._duplicate_node)

        del_action = menu.addAction("❌ Delete")
        del_action.triggered.connect(self._delete_node)

        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _add_rule_to_path(self, target_path: tuple[int, ...]) -> None:
        target = self._node_at(target_path)
        if target["type"] != "group":
            return
        dialog = RuleDialog(parent=self)
        if dialog.exec():
            new_path = target_path + (len(target["children"]),)
            target["children"].append(dialog.result_rule())
            self._tree_changed(new_path)

    def _add_group_to_path(self, target_path: tuple[int, ...], op: str) -> None:
        target = self._node_at(target_path)
        if target["type"] != "group":
            return
        new_path = target_path + (len(target["children"]),)
        target["children"].append(group_node(op))
        self._tree_changed(new_path)

    def _toggle_node_path(self, path: tuple[int, ...]) -> None:
        node = self._node_at(path)
        node["enabled"] = not node_enabled(node)
        self._tree_changed(path)

    def _edit_node(self) -> None:
        path = self._selected_path()
        node = self._node_at(path)
        if node["type"] == "group":
            choices = [
                "AND — Stock passes ALL of the below",
                "OR — Stock passes ANY 1 of the below",
                "ANY 2 — Stock passes ANY 2 of the below",
                "ANY 3 — Stock passes ANY 3 of the below",
                "ANY 4 — Stock passes ANY 4 of the below",
                "ANY 5 — Stock passes ANY 5 of the below",
            ]
            op_map = {
                "AND — Stock passes ALL of the below": "AND",
                "OR — Stock passes ANY 1 of the below": "OR",
                "ANY 2 — Stock passes ANY 2 of the below": "ANY 2",
                "ANY 3 — Stock passes ANY 3 of the below": "ANY 3",
                "ANY 4 — Stock passes ANY 4 of the below": "ANY 4",
                "ANY 5 — Stock passes ANY 5 of the below": "ANY 5",
            }
            current_op = str(node.get("op", "AND"))
            current_index = 0
            for idx, choice in enumerate(choices):
                if op_map[choice] == current_op:
                    current_index = idx
                    break
            selected, ok = QInputDialog.getItem(
                self, "Edit Group Condition", "Select Group Type:", choices, current_index, False
            )
            if ok and selected in op_map:
                node["op"] = op_map[selected]
                self._tree_changed()
        else:
            dialog = RuleDialog(node, self)
            if dialog.exec():
                parent, index = self._parent_and_index(path)
                parent["children"][index] = dialog.result_rule()
                self._tree_changed()

    def _duplicate_node(self) -> None:
        path = self._selected_path()
        if not path:
            node = copy.deepcopy(self.setup["expression"])
            self.setup["expression"]["children"].append(node)
            self._tree_changed()
            return
        parent, index = self._parent_and_index(path)
        node = copy.deepcopy(parent["children"][index])
        parent["children"].insert(index + 1, node)
        new_path = list(path)
        new_path[-1] += 1
        self._tree_changed(tuple(new_path))

    def _copy_to_draft(self) -> None:
        path = self._selected_path()
        node = self._node_at(path) if path is not None else self.setup["expression"]
        text = expression_text(node)
        self.english_rule.setText(text)
        self.english_rule.setFocus()
        self.english_rule.selectAll()

    def _save_rule_to_examples(self) -> None:
        text = self.english_rule.text().strip()
        if not text:
            path = self._selected_path()
            node = self._node_at(path) if path is not None else self.setup["expression"]
            text = expression_text(node).strip()
        if not text:
            QMessageBox.information(
                self,
                "Nothing to save",
                "Select a rule in the tree or type a rule sentence first.",
            )
            return
        store = CustomSnippetsStore()
        if store.save_snippet(text):
            QMessageBox.information(
                self,
                "Saved to Examples",
                f'Saved "{text}" to My Saved Snippets!',
            )
        else:
            QMessageBox.information(
                self,
                "Already in Examples",
                f'"{text}" is already in My Saved Snippets.',
            )

    def _delete_node(self) -> None:
        path = self._selected_path()
        if not path:
            QMessageBox.information(self, "Root group", "The root group cannot be deleted.")
            return
        parent, index = self._parent_and_index(path)
        del parent["children"][index]
        self._tree_changed()

    def _move_node(self, direction: int) -> None:
        path = self._selected_path()
        if not path:
            return
        parent, index = self._parent_and_index(path)
        target = index + direction
        if 0 <= target < len(parent["children"]):
            parent["children"][index], parent["children"][target] = parent["children"][target], parent["children"][index]
            self._tree_changed()

    def _move_into(self) -> None:
        path = self._selected_path()
        if not path:
            return
        parent, index = self._parent_and_index(path)
        candidates = [
            sibling_index
            for sibling_index, sibling in enumerate(parent["children"])
            if sibling_index != index and sibling["type"] == "group"
        ]
        if not candidates:
            QMessageBox.information(self, "Move into", "Add or select a sibling group first.")
            return
        labels = [f"{i + 1}: {parent['children'][i]['op']} group" for i in candidates]
        selected, accepted = QInputDialog.getItem(self, "Move into", "Destination group", labels, 0, False)
        if accepted:
            destination_index = candidates[labels.index(selected)]
            node = parent["children"].pop(index)
            if destination_index > index:
                destination_index -= 1
            parent["children"][destination_index]["children"].append(node)
            self._tree_changed()

    def _move_out(self) -> None:
        path = self._selected_path()
        if len(path) < 2:
            QMessageBox.information(self, "Move out", "This node is already in the root group.")
            return
        parent_path = path[:-1]
        parent_group, index = self._parent_and_index(path)
        grandparent, parent_index = self._parent_and_index(parent_path)
        node = parent_group["children"].pop(index)
        grandparent["children"].insert(parent_index + 1, node)
        self._tree_changed()

    def _tree_changed(self, select_path: tuple[int, ...] | None = None) -> None:
        self._rebuild_tree(select_path)
        if hasattr(self, "expanded_editor_dialog") and self.expanded_editor_dialog and self.expanded_editor_dialog.isVisible():
            self.expanded_editor_dialog.sync_from_main(select_path)
        self._settings_changed()

    def _parse_rule(self) -> None:
        try:
            parsed = RuleParser.parse(self.english_rule.text())
        except ValueError as exc:
            QMessageBox.warning(
                self,
                "Rule not understood",
                str(exc)
                + "\n\nExamples:\n"
                + "MACD histogram(12,26,9) crossed above 0\n"
                + "Daily MACD line crosses above daily MACD signal\n"
                + "RSI 14 crossed above 60\n"
                + "RSI 14 one day ago above 60\n"
                + "Yesterday close above SMA 20\n"
                + "Weekly EMA 10 previous week crosses above 50\n"
                + "Close 3 candles ago below daily SMA 20\n"
                + "Close within 2% above or below the 52 week high\n"
                + "Close within 4% above or below the 52 week high\n"
                + "Close at 52 week high",
            )
            return
        if expression_has_pnl_rules(parsed):
            QMessageBox.information(
                self,
                "Position-dependent EXIT rule",
                "This holding-dependent Profit/Loss rule can be saved without "
                "a Zerodha portfolio for use as a standalone Step 3 EXIT rule "
                "in FINAL ChartSage Paper Backtest.\n\n"
                "A normal current scan still requires one real portfolio and "
                "cost basis. Paper Backtest instead evaluates it against its "
                "simulated holdings and weighted-average acquisition cost.",
            )
        dialog = RuleDialog(parsed, self)
        dialog.setWindowTitle("Confirm or edit parsed rule")
        if dialog.exec():
            target_path = self._target_group_path()
            target = self._node_at(target_path)
            new_path = target_path + (len(target["children"]),)
            target["children"].append(dialog.result_rule())
            self.english_rule.clear()
            self._tree_changed(new_path)

    def _show_filter_examples(self) -> None:
        FilterExamplesDialog(self.english_rule, self).exec()

    def _show_filter_logic(self) -> None:
        FilterLogicDialog(self.setup["expression"], self).exec()

    def _has_unparsed_rule_draft(self, action: str) -> bool:
        if not self.english_rule.text().strip():
            return False
        QMessageBox.information(
            self,
            "Rule not added yet",
            f'The English rule is still a draft. Click "Parse one rule" and confirm it before {action}. '
            "Only rules visible in the structured expression tree are saved.",
        )
        self.english_rule.setFocus()
        return True

    def _create_new_scan(self) -> None:
        if (self.worker and self.worker.isRunning()) or (
            self.history_worker and self.history_worker.isRunning()
        ):
            QMessageBox.information(
                self,
                "Scan running",
                "Cancel or finish the running scan before creating a new scan.",
            )
            return
        if self._has_unparsed_rule_draft("creating a new scan"):
            return
        if not self._confirm_discard_unsaved("create a new scan"):
            return

        setup = default_setup()
        setup["custom_symbols"] = ""
        setup["expression"] = group_node("AND")
        self.active_template_name = None
        self._load_setup(setup)
        self._restore_active_template_selection()
        self._set_dirty(True)

        self.last_rows = []
        self.last_scan_setup = None
        self._populate_results([], setup)
        self.counts.setText(
            "New blank scan. Choose a universe and add at least one rule."
        )
        self.progress.setValue(0)
        self.progress.setFormat("Ready")
        self.export_button.setEnabled(False)
        self._mark_stale(False)

        self.last_history_report = None
        self.last_history_setup = None
        self.history_chart.set_summary([])
        self.history_legend.clear()
        self.history_selected_label.setText(
            "Click a bar to view the stocks matched on that date."
        )
        self._populate_table(self.history_results, [], setup)
        self.history_counts.setText("No history scan run.")
        self.history_progress.setValue(0)
        self.history_progress.setFormat("History not run.")
        self.history_export_button.setEnabled(False)
        self._mark_history_stale(False)
        self.result_tabs.setCurrentIndex(0)

    def _validate_template_setup(self, setup: dict[str, Any], action_desc: str = "saving the template", silent: bool = False) -> bool:
        try:
            validate_ast(setup["expression"])
            validate_ranking_setup(setup)
        except ValueError as exc:
            if not silent:
                QMessageBox.warning(self, "Invalid scan setup", str(exc))
            return False

        if expression_has_pnl_rules(setup.get("expression")):
            selected_unis = setup.get("universes", [])
            has_zerodha_uni = any(
                str(u).startswith("💼 ZERODHA") or str(u).startswith("💼 Zerodha")
                for u in selected_unis
            )
            non_zerodha_unis = [
                u for u in selected_unis
                if not (str(u).startswith("💼 ZERODHA") or str(u).startswith("💼 Zerodha"))
            ]
            live_portfolio_scope = has_zerodha_uni and not non_zerodha_unis
            if is_algo_template_setup(
                self.active_template_name or "", setup
            ) and not live_portfolio_scope:
                if not silent:
                    QMessageBox.warning(
                        self,
                        "Portfolio Watchlist Required",
                        f"A live/ALGO Profit or Loss rule requires portfolio "
                        f"holdings exclusively before {action_desc}.\n\n"
                        "Select only one account-specific Zerodha Portfolio "
                        "Watchlist. To use this rule without Zerodha, save it "
                        "as a normal template and select it as a standalone "
                        "Step 3 EXIT in FINAL ChartSage Paper Backtest.",
                    )
                return False
            setup["position_rule_scope"] = (
                "PORTFOLIO_AND_PAPER"
                if live_portfolio_scope
                else "PAPER_BACKTEST_ONLY"
            )
            setup["strategy_guide_role"] = "EXIT"
        else:
            setup.pop("position_rule_scope", None)

        # Account Mismatch Guard: Prevent saving an ALGO template belonging to one account to another account
        if self.active_template_name:
            all_templates = self.store.load_all()
            existing_setup = all_templates.get(self.active_template_name)
            if existing_setup:
                orig_target = str(existing_setup.get("algo_target_account", "")).strip().upper()
                new_target = str(setup.get("algo_target_account", "")).strip().upper()
                if orig_target == "ACTIVE":
                    active_uid, _ = self._get_active_account_label()
                    if active_uid and active_uid != "ACTIVE":
                        orig_target = active_uid
                if new_target == "ACTIVE":
                    active_uid, _ = self._get_active_account_label()
                    if active_uid and active_uid != "ACTIVE":
                        new_target = active_uid

                if orig_target and orig_target != "ACTIVE" and new_target and new_target != "ACTIVE" and orig_target != new_target:
                    if not silent:
                        QMessageBox.warning(
                            self,
                            "Account Mismatch Guard",
                            f"Template '{self.active_template_name}' is permanently bound to account '{orig_target}'.\n\n"
                            f"You are trying to save it under target account '{new_target}'. Saving an ALGO template to a different account is not allowed.\n\n"
                            f"To use this strategy on account '{new_target}', please click 'Create NEW ALGO Template 🤖' to create a template for {new_target}."
                        )
                    return False

        # Check embedded account tag in template name vs target account
        tpl_name = self.active_template_name or ""
        match_acc = re.search(r"-\s*([A-Z0-9]+)$", tpl_name.strip(), re.IGNORECASE)
        if match_acc:
            name_acc = match_acc.group(1).upper()
            accounts = BrokerLoginStore().get_broker_accounts("Zerodha")
            known_uids = {str(a.get("user_id", "")).strip().upper() for a in accounts if a.get("user_id")}
            if name_acc in known_uids:
                new_target = str(setup.get("algo_target_account", "")).strip().upper()
                if new_target == "ACTIVE":
                    active_uid, _ = self._get_active_account_label()
                    if active_uid and active_uid != "ACTIVE":
                        new_target = active_uid
                if new_target and new_target != "ACTIVE" and name_acc != new_target:
                    if not silent:
                        QMessageBox.warning(
                            self,
                            "Account Mismatch Guard",
                            f"Template '{tpl_name}' is bound to account '{name_acc}', but target account is set to '{new_target}'.\n\n"
                            f"Saving an ALGO template for one user account into another active account is not allowed."
                        )
                    return False

        return True

    def _get_suggested_template_name(self, base_name: str = "") -> str:
        base = base_name or self.active_template_name or "New Scanner"
        # Clean existing account and (Copy) suffixes if present
        base_clean = re.sub(r"\s*-\s*[A-Z0-9]+$", "", base, flags=re.IGNORECASE)
        base_clean = re.sub(r"\s*\((?:Copy|\d+)\)*", "", base_clean, flags=re.IGNORECASE).strip()
        
        acc_data = str(self.algo_target_account_combo.currentData() or "ACTIVE") if hasattr(self, "algo_target_account_combo") else "ACTIVE"
        acc_tag = acc_data
        if acc_tag == "ACTIVE":
            active_uid, _ = self._get_active_account_label()
            if active_uid and active_uid != "ACTIVE":
                acc_tag = active_uid

        if acc_tag and acc_tag != "ACTIVE":
            return f"{base_clean} - {acc_tag}"
        return f"{base_clean} (Copy)" if base_name else base_clean

    def _new_template(self) -> None:
        if self._has_unparsed_rule_draft("saving the template"):
            return
        setup = self._current_setup()
        if not self._validate_template_setup(setup, "creating the template"):
            return
        default_name = self._get_suggested_template_name()
        name, accepted = QInputDialog.getText(self, "Save current setup as a template", "Template name", text=default_name)
        if not accepted or not name.strip():
            return
        final_name = name.strip()
        setup, _changed, removed = normalize_general_universes(final_name, setup)
        if removed:
            QMessageBox.information(
                self,
                "Holdings universes removed",
                f"Zerodha holdings watchlists are reserved for ALGO templates.\n"
                f"Removed from '{final_name}': {', '.join(removed)}.",
            )
        try:
            self.store.create(final_name, setup)
        except TemplateStoreError as exc:
            QMessageBox.warning(self, "Create template failed", str(exc))
            return
        self._store_error = None
        self.active_template_name = final_name
        self._refresh_templates()
        self._set_dirty(False)

    def _save_template(self) -> None:
        if self._has_unparsed_rule_draft("saving the template"):
            return
        if not self.active_template_name:
            self._new_template()
            return
        setup = self._current_setup()
        if not self._validate_template_setup(setup, "saving the template"):
            return
        setup, _changed, removed = normalize_general_universes(
            self.active_template_name, setup
        )
        if removed:
            QMessageBox.information(
                self,
                "Holdings universes removed",
                f"Zerodha holdings watchlists are reserved for ALGO templates.\n"
                f"Removed from '{self.active_template_name}': {', '.join(removed)}.",
            )
        try:
            self.store.save_edit(self.active_template_name, setup)
        except TemplateStoreError as exc:
            self._store_error = str(exc)
            QMessageBox.warning(self, "Save template failed", self._store_error)
            return
        self._store_error = None
        self.setup = copy.deepcopy(setup)
        self._refresh_templates()
        self._restore_active_template_selection()
        self._set_dirty(False)
        QMessageBox.information(
            self,
            "Template saved",
            f'Template "{self.active_template_name}" was saved successfully.',
        )

    def _save_algo_execution_settings(self) -> None:
        if not self.active_template_name:
            QMessageBox.information(
                self,
                "No Active Template",
                "Please select or create an ALGO strategy before saving it.",
            )
            return

        current_setup = copy.deepcopy(self._current_setup())
        current_setup["is_algo_template"] = True

        # Gather modified execution fields from UI
        universes = [
            self.universe_list.item(i).data(Qt.ItemDataRole.UserRole)
            or self.universe_list.item(i).text().rsplit(" (", 1)[0]
            for i in range(self.universe_list.count())
            if self.universe_list.item(i).checkState() == Qt.CheckState.Checked
        ]

        current_setup["universes"] = universes
        current_setup["algo_capital_mode"] = self.algo_capital_mode_combo.currentText()
        current_setup["algo_max_slots"] = self.algo_max_slots_spin.value()
        current_setup["algo_capital_per_stock"] = self.algo_capital_spin.value()
        current_setup["algo_fixed_cap"] = self.algo_capital_spin.value()
        current_setup["algo_sell_mode"] = self.algo_sell_mode_combo.currentText()
        current_setup["algo_sell_pct"] = self.algo_sell_pct_spin.value()
        current_setup["algo_product"] = self.algo_product_combo.currentText()
        current_setup["algo_product_type"] = self.algo_product_combo.currentText()
        current_setup["algo_order_type"] = self.algo_order_type_combo.currentText()
        current_setup["algo_limit_buffer_pct"] = self.algo_limit_pct_spin.value()
        current_setup["algo_limit_pct"] = self.algo_limit_pct_spin.value()

        try:
            self.store.save_edit(self.active_template_name, current_setup)
        except TemplateStoreError as exc:
            QMessageBox.warning(self, "Save ALGO strategy failed", str(exc))
            return

        self.setup = copy.deepcopy(current_setup)
        self._refresh_templates()
        self._restore_active_template_selection()
        self._set_dirty(False)

        QMessageBox.information(
            self,
            "ALGO Strategy Saved",
            f'ALGO strategy "{self.active_template_name}" was saved successfully.',
        )

    def _duplicate_template(self) -> None:
        if self._has_unparsed_rule_draft("duplicating the template"):
            return
        setup = self._current_setup()
        if not self._validate_template_setup(setup, "duplicating the template"):
            return
        default_name = self._get_suggested_template_name()
        name, ok = QInputDialog.getText(self, "Duplicate full setup", "New template name", text=default_name)
        if ok and name.strip():
            setup = self._current_setup()
            try:
                self.store.create(name, setup)
            except TemplateStoreError as exc:
                QMessageBox.warning(self, "Duplicate failed", str(exc))
                return
            self._store_error = None
            self.active_template_name = name.strip()
            self._refresh_templates()
            self._set_dirty(False)

    def _create_algo_template(self) -> None:
        if (self.worker and self.worker.isRunning()) or (
            self.history_worker and self.history_worker.isRunning()
        ):
            QMessageBox.information(
                self,
                "Scan running",
                "Cancel or finish the running scan before creating a new ALGO template.",
            )
            return
        if self._has_unparsed_rule_draft("creating an ALGO template"):
            return
        if not self._confirm_discard_unsaved("create a new ALGO template"):
            return

        accounts = BrokerLoginStore().get_broker_accounts("Zerodha")
        if not accounts:
            QMessageBox.warning(
                self,
                "No Accounts Registered",
                "No Zerodha accounts found. Please log into Zerodha from the Portfolio menu first.",
            )
            return

        acc_options = []
        acc_ids = []
        for acc in accounts:
            u_id = str(acc.get("user_id", "")).strip().upper()
            lbl = str(acc.get("label", f"Zerodha ({u_id})")).strip()
            if u_id:
                acc_options.append(f"👤 {lbl} ({u_id})")
                acc_ids.append(u_id)

        if not acc_ids:
            QMessageBox.warning(self, "No Accounts Found", "No valid Zerodha user accounts found.")
            return

        # Pre-select currently active trading account if available
        default_acc_idx = 0
        active_uid, _ = self._get_active_account_label()
        if active_uid and active_uid in acc_ids:
            default_acc_idx = acc_ids.index(active_uid)

        selected_acc_str, ok = QInputDialog.getItem(
            self,
            "Target Account Selection",
            "Select Zerodha Account to bind this ALGO Strategy to:",
            acc_options,
            default_acc_idx,
            False,
        )
        if not ok or not selected_acc_str:
            return

        sel_idx = acc_options.index(selected_acc_str)
        target_uid = acc_ids[sel_idx]
        target_lbl = selected_acc_str.replace("👤 ", "")

        # Ask explicitly whether BUY or SELL strategy
        action_options = [
            "🛒 BUY (Entry Order - Budget Based)",
            "🔴 SELL (Exit Order - Portfolio Holdings Guard)",
        ]
        selected_action_str, ok_act = QInputDialog.getItem(
            self,
            "Trade Action Selection",
            f"Is this new ALGO strategy a BUY (Entry) or SELL (Exit) scan for account {target_uid}?",
            action_options,
            0,
            False,
        )
        if not ok_act or not selected_action_str:
            return

        is_sell = "SELL" in selected_action_str
        full_trade_action = (
            "SELL (Exit Order - Portfolio Holdings Guard)"
            if is_sell
            else "BUY (Entry Order - Budget Based)"
        )
        action_tag = "SELL" if is_sell else "BUY"

        suggested_name = f"[{action_tag}] ALGO Strategy - {target_uid}"

        name, accepted = QInputDialog.getText(
            self,
            "Create New ALGO Template",
            f"Enter New ALGO Template Name for account {target_uid}:",
            text=suggested_name,
        )
        if not accepted or not name.strip():
            return

        final_name = name.strip()

        # Target portfolio watchlist name for target account
        target_wl_name = f"💼 ZERODHA {target_lbl}" if "(" in target_lbl else f"💼 ZERODHA ({target_uid})"
        for k in UNIVERSES.keys():
            if f"({target_uid})" in k and k.startswith("💼 ZERODHA"):
                target_wl_name = k
                break

        # Initialize fresh new ALGO template
        algo_setup = default_setup()
        algo_setup["is_algo_template"] = True
        algo_setup["universes"] = [target_wl_name] if is_sell else ["NIFTY 500"]
        algo_setup["custom_symbols"] = ""
        algo_setup["expression"] = group_node("AND")
        algo_setup["algo_target_account"] = target_uid
        algo_setup["algo_trade_action"] = full_trade_action

        try:
            self.store.create(final_name, algo_setup)
        except TemplateStoreError as exc:
            QMessageBox.warning(self, "Create ALGO template failed", str(exc))
            return

        self._store_error = None
        self.active_template_name = final_name
        self._refresh_templates()
        self.template_combo.setCurrentText(final_name)
        self._load_setup(algo_setup)
        self._set_dirty(False)

        self.last_rows = []
        self.last_scan_setup = None
        self._populate_results([], algo_setup)
        self.counts.setText(
            f"New {action_tag} ALGO template '{final_name}' bound to account {target_uid} created successfully!"
        )
        self.progress.setValue(0)
        self.progress.setFormat("Ready")
        self.export_button.setEnabled(False)
        self._mark_stale(False)

        QMessageBox.information(
            self,
            "ALGO Template Created",
            f'New ALGO Strategy Template "{final_name}" bound to account {target_uid} has been created successfully!\n\n'
            f'Target Account: {target_lbl}\n\n'
            f'You can now add new rules and select your target universe for this strategy.',
        )

    def _get_current_template_real_name(self) -> str:
        real_data = str(self.template_combo.currentData() or "").strip()
        all_tpls = self.store.load_all()
        if real_data and real_data in all_tpls:
            return real_data
        if self.active_template_name and self.active_template_name in all_tpls:
            return self.active_template_name
        
        curr_text = str(self.template_combo.currentText() or "").strip()
        if curr_text in all_tpls:
            return curr_text
        
        clean_text = re.sub(r"^[⚡🔍]\s*", "", curr_text)
        clean_text = re.sub(r"\s*\[.*\]$", "", clean_text).strip()
        if clean_text in all_tpls:
            return clean_text
            
        return real_data or self.active_template_name or curr_text

    def _rename_template(self) -> None:
        old_name = self._get_current_template_real_name()
        if not old_name:
            return
        setup = self._current_setup()
        if not self._validate_template_setup(setup, "renaming the template"):
            return
        name, ok = QInputDialog.getText(self, "Rename template", "New name", text=old_name)
        if ok and name.strip():
            renamed_active = (old_name == self.active_template_name)
            new_name = name.strip()
            try:
                self.store.rename(old_name, new_name)
            except TemplateStoreError as exc:
                QMessageBox.warning(self, "Rename failed", str(exc))
                return
            try:
                self.telegram_state_store.rename_template(old_name, new_name)
            except Exception:
                pass
            if renamed_active:
                self.active_template_name = new_name
            self._refresh_templates()
            self.template_combo.setCurrentText(new_name)

    def _on_template_combo_changed(self) -> None:
        if getattr(self, "_loading", False):
            return
        idx = self.template_combo.currentIndex()
        if idx >= 0:
            selected = str(self.template_combo.itemData(idx) or "").strip()
            if selected:
                self._load_template(selected, force=True)

    def _load_template(self, name: str = "", force: bool = False) -> None:
        all_tpls = self.store.load_all()
        target_name = (name or str(self.template_combo.currentData() or "")).strip()

        if target_name not in all_tpls:
            # Fallback: Strip emoji prefixes and badges if passed formatted string
            clean_name = re.sub(r"^[⚡🔍]\s*", "", target_name)
            clean_name = re.sub(r"\s*\[.*\]$", "", clean_name).strip()
            if clean_name in all_tpls:
                target_name = clean_name

        if not target_name or target_name not in all_tpls:
            return

        if not force and target_name == self.active_template_name and hasattr(self, "setup") and self.setup and self.setup.get("expression"):
            return
        if not force and self._has_unparsed_rule_draft("loading another template"):
            self._restore_active_template_selection()
            return
        if not force and not self._confirm_discard_unsaved("load another template"):
            self._restore_active_template_selection()
            return

        try:
            payload = self.store.set_active(target_name)
        except TemplateStoreError as exc:
            QMessageBox.warning(self, "Load template failed", str(exc))
            self._restore_active_template_selection()
            return

        self._store_error = None
        self.active_template_name = target_name
        self._load_setup(payload)
        self._set_dirty(False)
        self._mark_stale(True)
        self._mark_history_stale(True)

    def _delete_template(self) -> None:
        if self._has_unparsed_rule_draft("deleting a template"):
            return
        name = self._get_current_template_real_name()
        if name and QMessageBox.question(self, "Delete template", f'Delete template "{name}"?') == QMessageBox.StandardButton.Yes:
            deleted_active = self.active_template_name == name
            try:
                self.store.delete(name)
            except TemplateStoreError as exc:
                QMessageBox.warning(self, "Delete failed", str(exc))
                return
            try:
                self.telegram_state_store.clear(name)
            except Exception:
                pass
            if deleted_active:
                self.active_template_name = None
            self._refresh_templates()
            if deleted_active:
                if self.active_template_name:
                    next_name = self.active_template_name
                    self.active_template_name = None
                    self._load_template(next_name)
                else:
                    setup = default_setup()
                    self._load_setup(setup)
                    self._set_dirty(False)
                    self._mark_stale(True)
                    self._mark_history_stale(True)

    def _open_portfolio_dashboard(self) -> None:
        dialog = ZerodhaPortfolioDashboardDialog(parent=self)
        dialog.exec()

    def _start_scan(self) -> bool:
        if (
            self._guide_automation_worker
            and self._guide_automation_worker.isRunning()
        ):
            QMessageBox.information(
                self,
                "Strategy Guide automation running",
                "Wait for the current automated Strategy Guide cycle to finish.",
            )
            return False
        if self.history_worker and self.history_worker.isRunning():
            QMessageBox.information(
                self,
                "History scan running",
                "Cancel or finish the history scan before running a current scan.",
            )
            return False
        setup = self._current_setup()
        global_settings = self.app_settings.load()
        setup["refresh_minutes"] = global_settings.get(
            "data_refresh_minutes", DEFAULT_REFRESH_MINUTES
        )
        setup["force_fresh"] = bool(global_settings.get("force_fresh", True))
        setup["algo_paper_mode"] = bool(
            setup.get("algo_auto_order_cb", False)
            or setup.get("algo_auto_order_enabled", False)
        )
        setup["template_name"] = self.active_template_name or "(unsaved recovery)"
        try:
            validate_ast(setup["expression"])
            validate_ranking_setup(setup)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid scan setup", str(exc))
            return False
        if (
            expression_has_pnl_rules(setup.get("expression"))
            and str(setup.get("position_rule_scope", "")).upper()
            == "PAPER_BACKTEST_ONLY"
        ):
            QMessageBox.information(
                self,
                "Paper Backtest EXIT rule",
                "This position-dependent rule is intentionally not a broad "
                "market scan. Select it as a standalone Step 3 EXIT rule in "
                "FINAL ChartSage and run Paper Backtest, where simulated "
                "holdings provide the required acquisition cost.",
            )
            return False
        if not setup["universes"] and not setup["custom_symbols"]:
            QMessageBox.warning(self, "No universe", "Choose at least one universe or enter custom symbols.")
            return False
        if setup.get("algo_paper_mode", False):
            if setup_paper_account(setup) is None:
                QMessageBox.warning(
                    self,
                    "Paper Account Required",
                    "Simulation mode requires a virtual paper account.\n\n"
                    "Open the ALGO tab and create or select one in the "
                    "'📒 Simulation Account' selector.",
                )
                return False
        elif expression_has_pnl_rules(
            setup.get("expression")
        ) or setup_has_portfolio_exit_triggers(setup):
            if setup_portfolio_account(setup) is None:
                QMessageBox.warning(
                    self,
                    "One Portfolio Account Required",
                    "Profit/stop rules and exit triggers require exactly one identifiable Zerodha account.\n\n"
                    "Select one account-specific Zerodha portfolio watchlist or assign one target account.",
                )
                return False
        self.auto_scan_timer.stop()
        self.scan_button.setEnabled(False)
        self.history_run_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setValue(0)
        self.progress.setFormat("Resolving universe...")
        self._reload_system_watchlists()
        self.worker = RuleScannerWorker(setup)
        self.worker.progress.connect(self._on_progress)
        self.worker.succeeded.connect(self._scan_finished)
        self.worker.failed.connect(self._scan_failed)
        self.worker.cancelled.connect(self._scan_cancelled)
        self.worker.finished.connect(self._worker_thread_finished)
        self.worker.start()
        return True

    def _cancel_scan(self) -> None:
        if self._multi_scan_active:
            self._stop_multi_scan()
        if self.worker:
            self.worker.cancel()
            self.cancel_button.setEnabled(False)
            self.progress.setFormat("Cancelling after current request...")

    def _on_progress(self, percent: int, text: str) -> None:
        self.progress.setValue(percent)
        self.progress.setFormat(text)

    def _scan_finished(self, report: ScanReport) -> None:
        completed_setup = copy.deepcopy(
            self.worker.setup if self.worker is not None else self._current_setup()
        )
        self.last_rows = report.rows
        self.last_scan_setup = completed_setup
        self._populate_results(report.rows, completed_setup)
        self.progress.setValue(100)
        self.progress.setFormat("Complete")
        self.counts.setText(
            f"Universe {report.total_symbols:,} | scanned {report.scanned_symbols:,} | matched {report.matched_symbols:,} | "
            f"cache hits {report.cache_hits:,} | downloaded {report.downloaded_symbols:,} | "
            f"missing data {report.missing_symbols:,} | download failures {report.download_failure_symbols:,} | "
            f"insufficient history {report.insufficient_symbols:,}"
            + (f" | universe errors {len(report.universe_errors)}" if report.universe_errors else "")
        )
        self._scan_cleanup()
        self.export_button.setEnabled(True)
        current_setup = self._current_setup()
        current_setup["template_name"] = self.active_template_name or "(unsaved recovery)"
        stale = current_setup != completed_setup
        self._mark_stale(stale)
        self._maybe_send_telegram_alert(report.rows, completed_setup)
        self._maybe_update_ranked_watchlist(report.rows, completed_setup)
        self._maybe_auto_update_paper_watchlist(report.rows, completed_setup)
        self._maybe_run_paper_trades(report.rows, completed_setup)
        if report.universe_errors:
            QMessageBox.warning(self, "Some universes failed", "\n".join(report.universe_errors))
        if self._multi_scan_active:
            QTimer.singleShot(300, self._process_next_multi_scan_queue_item)

    def _maybe_auto_update_paper_watchlist(
        self, rows: list[dict[str, Any]], setup: dict[str, Any]
    ) -> None:
        """Rebuild the PAPER watchlist for a rule template run in simulation mode."""
        if str(setup.get("as_of_mode", "latest")) != "latest":
            return
        if not (
            bool(setup.get("algo_auto_order_cb", False))
            or bool(setup.get("algo_auto_order_enabled", False))
        ):
            return
        if not bool(setup.get("paper_auto_watchlist", True)):
            return
        template_name = str(setup.get("template_name", "")).strip()
        if not template_name:
            return
        symbols = [
            str(row.get("Symbol", "")).strip().upper()
            for row in rows
            if str(row.get("Symbol", "")).strip()
        ]
        if not symbols:
            return
        wl_name = auto_update_paper_watchlist(template_name, symbols)
        if wl_name:
            self._sync_universe_list()

    def _maybe_update_ranked_watchlist(
        self, rows: list[dict[str, Any]], setup: dict[str, Any]
    ) -> None:
        """Replace the requested watchlist while preserving score order."""
        if not (
            bool(setup.get("ranking_enabled", False))
            and bool(setup.get("ranking_watchlist_enabled", False))
        ):
            return
        name = str(setup.get("ranking_watchlist_name", "") or "").strip()
        if not name:
            return
        if not re.fullmatch(r"[A-Za-z0-9\s_-]+", name):
            QMessageBox.warning(
                self,
                "Ranked watchlist not updated",
                "Watchlist names may contain only letters, numbers, spaces, "
                "hyphens, and underscores.",
            )
            return
        try:
            store = WatchlistStore()
            watchlists = store.load()
            if name in UNIVERSES and name not in watchlists:
                QMessageBox.warning(
                    self,
                    "Ranked watchlist not updated",
                    f"'{name}' is a reserved built-in universe name.",
                )
                return
            symbols = list(
                dict.fromkeys(
                    str(row.get("Symbol", "")).strip().upper()
                    for row in rows
                    if str(row.get("Symbol", "")).strip()
                )
            )
            # Save an empty result too: the ranked watchlist must reflect the
            # latest completed run rather than retain stale constituents.
            watchlists[name] = symbols
            store.save(watchlists)
            UNIVERSES[name] = symbols
            self._sync_universe_list()
        except (WatchlistStoreError, OSError) as exc:
            QMessageBox.warning(
                self, "Ranked watchlist not updated", str(exc)
            )

    def _maybe_run_paper_trades(
        self, rows: list[dict[str, Any]], setup: dict[str, Any]
    ) -> None:
        if not (
            bool(setup.get("algo_auto_order_cb", False))
            or bool(setup.get("algo_auto_order_enabled", False))
        ):
            return
        if str(setup.get("as_of_mode", "latest")) != "latest":
            return
        account = setup_paper_account(setup)
        if not account:
            return
        if getattr(self, "paper_worker", None) is not None and self.paper_worker.isRunning():
            return
        plan = self._build_paper_plan(rows, setup, account)
        if not plan:
            return
        self.paper_worker = AlgoPaperOrderWorker(plan, parent=self)
        self.paper_worker.succeeded.connect(self._on_paper_trades_finished)
        self.paper_worker.failed.connect(self._on_paper_trades_failed)
        self.paper_worker.finished.connect(self._paper_worker_finished)
        self.paper_worker.start()

    def _build_paper_plan(
        self,
        rows: list[dict[str, Any]],
        setup: dict[str, Any],
        account: str,
    ) -> list[dict[str, Any]]:
        return build_paper_plan(rows, setup, account)

    def _on_paper_trades_finished(self, payload: dict[str, Any]) -> None:
        results = payload.get("results", []) or []
        placed = [r for r in results if r.get("placed")]
        skipped = [r for r in results if not r.get("placed")]
        lines: list[str] = []
        for r in placed:
            entry = r.get("entry", {})
            lines.append(
                f"🟢 {entry.get('action')} {entry.get('symbol')} "
                f"× {entry.get('qty')} @ ₹ {format_number(entry.get('fill_price'))}"
            )
        for r in skipped:
            lines.append(
                f"⏭ {r.get('action')} {r.get('symbol')} — {r.get('reason')}"
            )
        if lines:
            QMessageBox.information(
                self,
                "Paper trades recorded",
                "\n".join(lines) if lines else "No paper trades were recorded.",
            )
        self._mark_paper_trades_dirty()

    def _on_paper_trades_failed(self, error: str) -> None:
        QMessageBox.warning(
            self,
            "Paper trades failed",
            "Simulated paper trades could not be recorded.\n\n"
            f"{error[:1200]}",
        )

    def _paper_worker_finished(self) -> None:
        self.paper_worker = None

    def _mark_paper_trades_dirty(self) -> None:
        dialog = getattr(self, "_paper_trades_dialog", None)
        if dialog is not None:
            dialog.refresh()

    def _on_algo_auto_order_toggled(self, state: int) -> None:
        if self._loading:
            return
        enabled = bool(state)
        paper_account = str(
            self.algo_paper_account_combo.currentData() or ""
        ).strip()
        if enabled:
            if not paper_account:
                QMessageBox.warning(
                    self,
                    "Paper account required",
                    "Simulation mode needs a virtual paper account.\n\n"
                    "Create one with the '✏️ Manage…' button next to the "
                    "'📒 Simulation Account' selector.",
                )
                self.algo_auto_order_cb.blockSignals(True)
                self.algo_auto_order_cb.setChecked(False)
                self.algo_auto_order_cb.blockSignals(False)
                return
            if not getattr(self, "_paper_confirm_shown", False):
                QMessageBox.information(
                    self,
                    "Simulation (paper) mode enabled",
                    "Matched signals for this template will now be logged as SIMULATED paper trades "
                    f"on the virtual paper account {paper_account}. No real orders are placed.",
                )
                self._paper_confirm_shown = True
        self.algo_paper_account_combo.setEnabled(enabled)
        self.setup = self._current_setup()
        self._set_dirty(True)

    def _open_paper_trades_dialog(self) -> None:
        dialog = PaperTradesDialog(parent=self)
        self._paper_trades_dialog = dialog
        dialog.exec()
        self._paper_trades_dialog = None

    def _open_paper_settings(self) -> None:
        PaperAccountsDialog(parent=self).exec()
        self._populate_paper_account_combo()
        if self.algo_paper_account_combo.isEnabled():
            self.setup = self._current_setup()
            self._set_dirty(True)
        if hasattr(self, "paper_pipeline_account_combo"):
            self._populate_paper_pipeline_account_combo()

    def _populate_paper_pipeline_account_combo(self) -> None:
        if not hasattr(self, "paper_pipeline_account_combo"):
            return
        current = str(
            self.paper_pipeline_account_combo.currentData() or ""
        ).strip().upper()
        self.paper_pipeline_account_combo.blockSignals(True)
        self.paper_pipeline_account_combo.clear()
        self.paper_pipeline_account_combo.addItem("— None selected —", "")
        try:
            accounts = PaperTradesStore().list_paper_accounts()
        except PaperTradesStoreError:
            accounts = []
        for acc in accounts:
            uid = str(acc.get("uid", "")).strip().upper()
            label = str(acc.get("display_name") or uid)
            self.paper_pipeline_account_combo.addItem(f"📒 {label}", uid)
        self.paper_pipeline_account_combo.blockSignals(False)
        idx = self.paper_pipeline_account_combo.findData(current)
        self.paper_pipeline_account_combo.setCurrentIndex(
            idx if idx >= 0 else 0
        )

    def _paper_template_names(self) -> dict[str, dict[str, Any]]:
        try:
            return self.store.load_all()
        except TemplateStoreError:
            return {}

    def _build_strategy_automation_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        recipe_row = QHBoxLayout()
        recipe_row.addWidget(QLabel("Saved recipe:"))
        self.guide_auto_recipe_combo = QComboBox()
        self.guide_auto_recipe_combo.setMinimumWidth(240)
        self.guide_auto_recipe_combo.setToolTip(
            "Select a saved recipe to edit. Save current changes before "
            "switching to another recipe. A filled circle means the recipe "
            "is included in automated runs."
        )
        self.guide_auto_recipe_combo.currentIndexChanged.connect(
            self._strategy_automation_recipe_selected
        )
        recipe_row.addWidget(self.guide_auto_recipe_combo, 1)
        self.guide_auto_new_btn = QPushButton("New")
        self.guide_auto_new_btn.clicked.connect(self._new_strategy_automation_recipe)
        self.guide_auto_save_as_btn = QPushButton("Save As…")
        self.guide_auto_save_as_btn.clicked.connect(self._save_as_strategy_automation_recipe)
        self.guide_auto_duplicate_btn = QPushButton("Duplicate…")
        self.guide_auto_duplicate_btn.clicked.connect(self._duplicate_strategy_automation_recipe)
        self.guide_auto_rename_btn = QPushButton("Rename…")
        self.guide_auto_rename_btn.clicked.connect(self._rename_strategy_automation_recipe)
        self.guide_auto_delete_btn = QPushButton("Delete")
        self.guide_auto_delete_btn.clicked.connect(self._delete_strategy_automation_recipe)
        recipe_row.addWidget(self.guide_auto_new_btn)
        recipe_row.addWidget(self.guide_auto_save_as_btn)
        layout.addLayout(recipe_row)

        recipe_manage_row = QHBoxLayout()
        recipe_manage_row.addStretch(1)
        recipe_manage_row.addWidget(self.guide_auto_duplicate_btn)
        recipe_manage_row.addWidget(self.guide_auto_rename_btn)
        recipe_manage_row.addWidget(self.guide_auto_delete_btn)
        layout.addLayout(recipe_manage_row)

        self.guide_auto_enabled_cb = QCheckBox(
            "Include this recipe when Start Automation is running"
        )
        self.guide_auto_enabled_cb.setToolTip(
            "Every enabled recipe keeps its own interval and operating window. "
            "Due recipes are queued and run one at a time."
        )
        layout.addWidget(self.guide_auto_enabled_cb)
        self.guide_auto_recipe_last_run_lbl = QLabel("Never run")
        self.guide_auto_recipe_last_run_lbl.setStyleSheet(
            "color:#555; font-size:10px;"
        )
        layout.addWidget(self.guide_auto_recipe_last_run_lbl)

        intro = QLabel(
            "Run selected General Scan templates as feeders, rebuild their "
            "watchlists, combine the matches into one Step 1A source, then run "
            "the complete BUY/EXIT Strategy Guide recipe."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(
            "background:#e8f5e9; color:#1b5e20; border:1px solid #a5d6a7; "
            "border-radius:4px; padding:7px;"
        )
        layout.addWidget(intro)

        form = QFormLayout()
        self.guide_auto_recipe_edit = QLineEdit()
        self.guide_auto_recipe_edit.setPlaceholderText("e.g. NIFTY 500 MOMENTUM GUIDE")
        form.addRow("Recipe name:", self.guide_auto_recipe_edit)
        self.guide_auto_combined_watchlist_edit = QLineEdit()
        self.guide_auto_combined_watchlist_edit.setPlaceholderText(
            "e.g. GUIDE MOMENTUM SOURCE"
        )
        form.addRow(
            "Combined Step 1A watchlist:",
            self.guide_auto_combined_watchlist_edit,
        )
        layout.addLayout(form)

        feeder_header = QHBoxLayout()
        feeder_header.addWidget(QLabel("General Scan feeder templates:"), 1)
        self.guide_auto_feeder_count_lbl = QLabel("0 selected")
        feeder_header.addWidget(self.guide_auto_feeder_count_lbl)
        self.guide_auto_refresh_feeders_btn = QPushButton("↻ Refresh")
        self.guide_auto_refresh_feeders_btn.clicked.connect(
            self._populate_strategy_feeder_list
        )
        feeder_header.addWidget(self.guide_auto_refresh_feeders_btn)
        layout.addLayout(feeder_header)
        self.guide_auto_feeder_list = QListWidget()
        self.guide_auto_feeder_list.setMinimumHeight(190)
        self.guide_auto_feeder_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.guide_auto_feeder_list.setToolTip(
            "Each checked General Scan template runs over its own saved universe. "
            "Its matches feed the combined Strategy Guide source."
        )
        self.guide_auto_feeder_list.itemChanged.connect(
            self._strategy_automation_selection_changed
        )
        layout.addWidget(self.guide_auto_feeder_list, 1)

        self.guide_auto_individual_watchlists_cb = QCheckBox(
            "Also create/update one 'GUIDE FEEDER <Template>' watchlist per feeder"
        )
        layout.addWidget(self.guide_auto_individual_watchlists_cb)

        schedule_box = QGroupBox("Automatic schedule (runs while ChartSage is open)")
        schedule_grid = QGridLayout(schedule_box)
        self.guide_auto_interval_combo = QComboBox()
        for minutes in (5, 10, 15, 30, 60):
            self.guide_auto_interval_combo.addItem(f"Every {minutes} minutes", minutes)
        self.guide_auto_start_time = QTimeEdit()
        self.guide_auto_start_time.setDisplayFormat("HH:mm")
        self.guide_auto_end_time = QTimeEdit()
        self.guide_auto_end_time.setDisplayFormat("HH:mm")
        self.guide_auto_trading_days_cb = QCheckBox("Trading days only")
        schedule_grid.addWidget(QLabel("Interval:"), 0, 0)
        schedule_grid.addWidget(self.guide_auto_interval_combo, 0, 1)
        schedule_grid.addWidget(QLabel("Window:"), 0, 2)
        schedule_grid.addWidget(self.guide_auto_start_time, 0, 3)
        schedule_grid.addWidget(QLabel("to"), 0, 4)
        schedule_grid.addWidget(self.guide_auto_end_time, 0, 5)
        schedule_grid.addWidget(self.guide_auto_trading_days_cb, 0, 6)
        layout.addWidget(schedule_box)

        options_row = QHBoxLayout()
        self.guide_auto_telegram_cb = QCheckBox(
            "Send one consolidated Telegram report after every cycle"
        )
        options_row.addWidget(self.guide_auto_telegram_cb, 1)
        self.guide_auto_telegram_settings_btn = QPushButton("Telegram Settings…")
        self.guide_auto_telegram_settings_btn.clicked.connect(
            self._open_telegram_settings
        )
        options_row.addWidget(self.guide_auto_telegram_settings_btn)
        layout.addLayout(options_row)
        price_row = QHBoxLayout()
        self.guide_auto_adjusted_cb = QCheckBox(
            "Technical Adjusted prices"
        )
        self.guide_auto_include_current_cb = QCheckBox(
            "Include current incomplete D/W/M bars"
        )
        price_row.addWidget(self.guide_auto_adjusted_cb)
        price_row.addWidget(self.guide_auto_include_current_cb)
        price_row.addStretch(1)
        layout.addLayout(price_row)

        buttons = QHBoxLayout()
        self.guide_auto_save_btn = QPushButton("Save Recipe")
        self.guide_auto_save_btn.clicked.connect(self._save_strategy_automation_recipe)
        self.guide_auto_run_btn = QPushButton("▶ Run Full Cycle Now")
        self.guide_auto_run_btn.setStyleSheet(
            "font-weight:700; background:#2e7d32; color:white; padding:6px 12px;"
        )
        self.guide_auto_run_btn.clicked.connect(
            lambda: self._start_strategy_automation(run_once=True)
        )
        self.guide_auto_start_btn = QPushButton("Start Automation")
        self.guide_auto_start_btn.setToolTip(
            "Start the repeating queue for every saved recipe whose "
            "'Include this recipe' box is enabled."
        )
        self.guide_auto_start_btn.clicked.connect(
            lambda: self._start_strategy_automation(run_once=False)
        )
        self.guide_auto_stop_btn = QPushButton("Stop Automation")
        self.guide_auto_stop_btn.clicked.connect(self._stop_strategy_automation)
        buttons.addWidget(self.guide_auto_save_btn)
        buttons.addWidget(self.guide_auto_run_btn)
        buttons.addWidget(self.guide_auto_start_btn)
        buttons.addWidget(self.guide_auto_stop_btn)
        layout.addLayout(buttons)

        self.guide_auto_progress = QProgressBar()
        self.guide_auto_progress.setRange(0, 100)
        self.guide_auto_progress.setValue(0)
        self.guide_auto_progress.setFormat("Ready")
        layout.addWidget(self.guide_auto_progress)
        self.guide_auto_status_lbl = QLabel("Automation is stopped.")
        self.guide_auto_status_lbl.setWordWrap(True)
        self.guide_auto_status_lbl.setStyleSheet("color:#555; font-size:11px;")
        layout.addWidget(self.guide_auto_status_lbl)

        self._populate_strategy_feeder_list()
        self._refresh_strategy_automation_recipe_combo()
        if not self._guide_automation_active_recipe:
            self._apply_strategy_automation_config(self._guide_automation_config)
        self._update_strategy_automation_buttons()
        return page

    def _populate_strategy_feeder_list(self) -> None:
        if not hasattr(self, "guide_auto_feeder_list"):
            return
        checked = {
            str(self.guide_auto_feeder_list.item(i).data(Qt.ItemDataRole.UserRole) or "")
            for i in range(self.guide_auto_feeder_list.count())
            if self.guide_auto_feeder_list.item(i).checkState()
            == Qt.CheckState.Checked
        }
        if not checked:
            checked = set(self._guide_automation_config.get("feeder_templates", []))
        self.guide_auto_feeder_list.blockSignals(True)
        self.guide_auto_feeder_list.clear()
        for name, setup in sorted(
            self._paper_template_names().items(), key=lambda item: item[0].upper()
        ):
            if is_final_template_setup(name, setup) or is_algo_template_setup(name, setup):
                continue
            try:
                validate_ast(setup.get("expression", default_setup()["expression"]))
            except ValueError:
                continue
            universe = strategy_guide_universe_label(setup)
            item = QListWidgetItem(f"[{universe}]  {name}")
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if name in checked else Qt.CheckState.Unchecked
            )
            item.setToolTip(
                f"General Scan feeder\nSaved universe: {universe}\n"
                f"Output: {guide_feeder_watchlist_name(name)}"
            )
            self.guide_auto_feeder_list.addItem(item)
        self.guide_auto_feeder_list.blockSignals(False)
        self._strategy_automation_selection_changed()

    def _strategy_automation_selection_changed(self, *_args: Any) -> None:
        if not hasattr(self, "guide_auto_feeder_count_lbl"):
            return
        count = len(self._checked_guide_template_names(self.guide_auto_feeder_list))
        self.guide_auto_feeder_count_lbl.setText(f"{count} selected")

    @staticmethod
    def _set_checked_list_values(widget: QListWidget, values: Iterable[str]) -> None:
        selected = {str(value).strip() for value in values if str(value).strip()}
        widget.blockSignals(True)
        for index in range(widget.count()):
            item = widget.item(index)
            name = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(
                    Qt.CheckState.Checked
                    if name in selected else Qt.CheckState.Unchecked
                )
        widget.blockSignals(False)

    def _persist_strategy_automation_recipes(self) -> bool:
        try:
            self.store.save_strategy_automation_recipes({
                "active": self._guide_automation_active_recipe,
                "recipes": self._guide_automation_recipes,
            })
            return True
        except (TemplateStoreError, OSError) as exc:
            QMessageBox.warning(self, "Recipes not saved", str(exc))
            return False

    def _refresh_strategy_automation_recipe_combo(self, select_name: str = "") -> None:
        if not hasattr(self, "guide_auto_recipe_combo"):
            return
        target = str(
            select_name or self._guide_automation_active_recipe or ""
        ).strip()
        self.guide_auto_recipe_combo.blockSignals(True)
        self.guide_auto_recipe_combo.clear()
        self.guide_auto_recipe_combo.addItem("— New unsaved recipe —", "")
        for name in sorted(self._guide_automation_recipes, key=str.upper):
            recipe_config = self._guide_automation_recipes[name]
            enabled = bool(recipe_config.get("automation_enabled", False))
            marker = "●" if enabled else "○"
            last_status = str(recipe_config.get("last_status", "") or "").strip()
            status_suffix = f" — {last_status}" if last_status else ""
            self.guide_auto_recipe_combo.addItem(
                f"{marker} {name}{status_suffix}", name
            )
        index = self.guide_auto_recipe_combo.findData(target)
        self.guide_auto_recipe_combo.setCurrentIndex(max(0, index))
        self.guide_auto_recipe_combo.blockSignals(False)
        self._strategy_automation_recipe_selected(
            self.guide_auto_recipe_combo.currentIndex()
        )

    def _strategy_automation_recipe_selected(self, _index: int = -1) -> None:
        if not hasattr(self, "guide_auto_recipe_combo"):
            return
        name = str(self.guide_auto_recipe_combo.currentData() or "").strip()
        self._guide_automation_active_recipe = name
        if name and name in self._guide_automation_recipes:
            self._guide_automation_config = normalize_strategy_automation_config(
                self._guide_automation_recipes[name]
            )
            self._guide_automation_config["recipe_name"] = name
        else:
            self._guide_automation_config = strategy_automation_defaults()
            self._guide_automation_config["recipe_name"] = ""
        self._apply_strategy_automation_config(self._guide_automation_config)
        last_run = str(self._guide_automation_config.get("last_run_at", "") or "")
        last_status = str(self._guide_automation_config.get("last_status", "") or "")
        self.guide_auto_recipe_last_run_lbl.setText(
            f"Last run: {last_run} | {last_status}"
            if last_run else "Never run"
        )
        self.guide_auto_recipe_edit.setReadOnly(bool(name))
        self.guide_auto_rename_btn.setEnabled(bool(name))
        self.guide_auto_delete_btn.setEnabled(bool(name))
        self.guide_auto_duplicate_btn.setEnabled(bool(name))

    def _new_strategy_automation_recipe(self) -> None:
        self._guide_automation_active_recipe = ""
        self.guide_auto_recipe_combo.setCurrentIndex(0)
        config = strategy_automation_defaults()
        config["recipe_name"] = ""
        config["automation_enabled"] = False
        self._guide_automation_config = config
        self._apply_strategy_automation_config(config)
        self.guide_auto_recipe_edit.setReadOnly(False)
        self.guide_auto_recipe_edit.setFocus()
        self.guide_auto_status_lbl.setText("Enter a name and configure the new recipe.")

    def _save_as_strategy_automation_recipe(self) -> None:
        name, accepted = QInputDialog.getText(
            self,
            "Save Strategy Guide Recipe As",
            "New recipe name:",
            text=(
                f"{self._guide_automation_active_recipe} Copy"
                if self._guide_automation_active_recipe else ""
            ),
        )
        name = str(name).strip()
        if not accepted or not name:
            return
        if name in self._guide_automation_recipes:
            QMessageBox.warning(self, "Duplicate recipe", f"A recipe named '{name}' already exists.")
            return
        old_active = self._guide_automation_active_recipe
        old_text = self.guide_auto_recipe_edit.text()
        old_watchlist = self.guide_auto_combined_watchlist_edit.text().strip()
        used_watchlists = {
            str(item.get("combined_watchlist_name", "") or "").strip().upper()
            for item in self._guide_automation_recipes.values()
        }
        candidate_watchlist = f"{old_watchlist or 'GUIDE AUTOMATION SOURCE'} COPY"
        suffix = 2
        while candidate_watchlist.upper() in used_watchlists:
            candidate_watchlist = (
                f"{old_watchlist or 'GUIDE AUTOMATION SOURCE'} COPY {suffix}"
            )
            suffix += 1
        self._guide_automation_active_recipe = ""
        self.guide_auto_recipe_edit.setReadOnly(False)
        self.guide_auto_recipe_edit.setText(name)
        self.guide_auto_combined_watchlist_edit.setText(candidate_watchlist)
        if not self._save_strategy_automation_recipe():
            self._guide_automation_active_recipe = old_active
            self.guide_auto_recipe_edit.setText(old_text)
            self.guide_auto_combined_watchlist_edit.setText(old_watchlist)

    def _duplicate_strategy_automation_recipe(self) -> None:
        current = self._guide_automation_active_recipe
        if not current or current not in self._guide_automation_recipes:
            return
        name, accepted = QInputDialog.getText(
            self,
            "Duplicate Strategy Guide Recipe",
            "Duplicate name:",
            text=f"{current} Copy",
        )
        name = str(name).strip()
        if not accepted or not name:
            return
        if name in self._guide_automation_recipes:
            QMessageBox.warning(self, "Duplicate recipe", f"A recipe named '{name}' already exists.")
            return
        config = copy.deepcopy(self._guide_automation_recipes[current])
        config["recipe_name"] = name
        config["automation_enabled"] = False
        base_watchlist = str(
            config.get("combined_watchlist_name", "GUIDE AUTOMATION SOURCE")
            or "GUIDE AUTOMATION SOURCE"
        ).strip()
        used_watchlists = {
            str(item.get("combined_watchlist_name", "") or "").strip().upper()
            for item in self._guide_automation_recipes.values()
        }
        candidate_watchlist = f"{base_watchlist} COPY"
        suffix = 2
        while candidate_watchlist.upper() in used_watchlists:
            candidate_watchlist = f"{base_watchlist} COPY {suffix}"
            suffix += 1
        config["combined_watchlist_name"] = candidate_watchlist
        self._guide_automation_recipes[name] = config
        self._guide_automation_active_recipe = name
        if self._persist_strategy_automation_recipes():
            self._refresh_strategy_automation_recipe_combo(name)

    def _rename_strategy_automation_recipe(self) -> None:
        current = self._guide_automation_active_recipe
        if not current or current not in self._guide_automation_recipes:
            return
        if current == self._guide_automation_running_recipe:
            QMessageBox.information(
                self, "Recipe is running", "Wait for this recipe's current cycle to finish before renaming it."
            )
            return
        name, accepted = QInputDialog.getText(
            self, "Rename Strategy Guide Recipe", "New name:", text=current
        )
        name = str(name).strip()
        if not accepted or not name or name == current:
            return
        if name in self._guide_automation_recipes:
            QMessageBox.warning(self, "Duplicate recipe", f"A recipe named '{name}' already exists.")
            return
        config = self._guide_automation_recipes.pop(current)
        config["recipe_name"] = name
        self._guide_automation_recipes[name] = config
        if current in self._guide_automation_next_due:
            self._guide_automation_next_due[name] = self._guide_automation_next_due.pop(current)
        self._guide_automation_active_recipe = name
        if self._persist_strategy_automation_recipes():
            self._refresh_strategy_automation_recipe_combo(name)

    def _delete_strategy_automation_recipe(self) -> None:
        name = self._guide_automation_active_recipe
        if not name or name not in self._guide_automation_recipes:
            return
        if name == self._guide_automation_running_recipe:
            QMessageBox.information(
                self, "Recipe is running", "Wait for this recipe's current cycle to finish before deleting it."
            )
            return
        if QMessageBox.question(
            self, "Delete Strategy Guide Recipe", f'Delete recipe "{name}"?\n\nGenerated watchlists will be preserved.'
        ) != QMessageBox.StandardButton.Yes:
            return
        self._guide_automation_recipes.pop(name, None)
        self._guide_automation_next_due.pop(name, None)
        self._guide_automation_active_recipe = next(iter(self._guide_automation_recipes), "")
        if self._persist_strategy_automation_recipes():
            self._refresh_strategy_automation_recipe_combo(
                self._guide_automation_active_recipe
            )
        if self._guide_automation_active and not any(
            bool(config.get("automation_enabled", False))
            for config in self._guide_automation_recipes.values()
        ):
            self._stop_strategy_automation()
            self.guide_auto_status_lbl.setText(
                "Automation stopped because no enabled recipes remain."
            )

    def _apply_strategy_automation_config(self, raw: dict[str, Any]) -> None:
        config = normalize_strategy_automation_config(raw)
        self.guide_auto_recipe_edit.setText(str(config["recipe_name"]))
        self.guide_auto_combined_watchlist_edit.setText(
            str(config["combined_watchlist_name"])
        )
        self._set_checked_list_values(
            self.guide_auto_feeder_list, config["feeder_templates"]
        )
        interval_index = self.guide_auto_interval_combo.findData(
            int(config["interval_minutes"])
        )
        self.guide_auto_interval_combo.setCurrentIndex(max(0, interval_index))
        self.guide_auto_start_time.setTime(QTime.fromString(config["start_time"], "HH:mm"))
        self.guide_auto_end_time.setTime(QTime.fromString(config["end_time"], "HH:mm"))
        self.guide_auto_trading_days_cb.setChecked(bool(config["trading_days_only"]))
        self.guide_auto_telegram_cb.setChecked(bool(config["telegram_enabled"]))
        self.guide_auto_enabled_cb.setChecked(bool(config["automation_enabled"]))
        self.guide_auto_adjusted_cb.setChecked(bool(config["technical_adjusted"]))
        self.guide_auto_include_current_cb.setChecked(bool(config["include_current"]))
        self.guide_auto_individual_watchlists_cb.setChecked(
            bool(config["create_individual_watchlists"])
        )
        self._set_checked_list_values(
            self.paper_member_list, config.get("candidate_templates", [])
        )
        self._set_checked_list_values(
            self.paper_exit_list, config.get("exit_templates", [])
        )
        for combo, key in (
            (self.paper_buy_mode_combo, "buy_mode"),
            (self.paper_exit_mode_combo, "exit_mode"),
            (self.paper_finalset_combo, "buy_finalset"),
            (self.paper_exit_finalset_combo, "exit_finalset"),
        ):
            index = combo.findData(str(config.get(key, "") or ""))
            if index >= 0:
                combo.setCurrentIndex(index)
        self.paper_buy_k_spin.setValue(max(1, int(config.get("buy_k", 1) or 1)))
        self.paper_exit_k_spin.setValue(max(1, int(config.get("exit_k", 1) or 1)))
        self.guide_track_model_cb.setChecked(bool(config.get("track_model", False)))
        account_index = self.paper_pipeline_account_combo.findData(
            str(config.get("model_account", "") or "").upper()
        )
        if account_index >= 0:
            self.paper_pipeline_account_combo.setCurrentIndex(account_index)
        self.paper_auto_watchlist_cb.setChecked(
            bool(config.get("auto_paper_watchlists", True))
        )
        capital_mode = str(config.get("capital_mode", "") or "")
        if capital_mode:
            index = self.paper_capital_mode_combo.findText(capital_mode)
            if index >= 0:
                self.paper_capital_mode_combo.setCurrentIndex(index)
        self.paper_max_slots_spin.setValue(
            max(1, int(config.get("max_slots", self.paper_max_slots_spin.value()) or 1))
        )
        self.paper_capital_spin.setValue(
            float(config.get("fixed_capital", self.paper_capital_spin.value()) or 0)
        )
        for combo, key in (
            (self.paper_product_combo, "product"),
            (self.paper_order_type_combo, "order_type"),
        ):
            text = str(config.get(key, "") or "")
            if text:
                index = combo.findText(text)
                if index >= 0:
                    combo.setCurrentIndex(index)
        self._strategy_automation_selection_changed()
        self._update_guide_controls()

    def _strategy_automation_config_from_ui(self) -> dict[str, Any]:
        config = {
            "recipe_name": self.guide_auto_recipe_edit.text().strip(),
            "feeder_templates": self._checked_guide_template_names(
                self.guide_auto_feeder_list
            ),
            "candidate_templates": self._checked_guide_template_names(
                self.paper_member_list
            ),
            "buy_mode": str(self.paper_buy_mode_combo.currentData() or "AND"),
            "buy_k": int(self.paper_buy_k_spin.value()),
            "buy_finalset": str(self.paper_finalset_combo.currentData() or ""),
            "exit_templates": self._checked_guide_template_names(self.paper_exit_list),
            "exit_mode": str(self.paper_exit_mode_combo.currentData() or "OR"),
            "exit_k": int(self.paper_exit_k_spin.value()),
            "exit_finalset": str(self.paper_exit_finalset_combo.currentData() or ""),
            "track_model": self.guide_track_model_cb.isChecked(),
            "model_account": str(self.paper_pipeline_account_combo.currentData() or ""),
            "auto_paper_watchlists": self.paper_auto_watchlist_cb.isChecked(),
            "capital_mode": str(self.paper_capital_mode_combo.currentText() or ""),
            "max_slots": int(self.paper_max_slots_spin.value()),
            "fixed_capital": float(self.paper_capital_spin.value()),
            "product": str(self.paper_product_combo.currentText() or ""),
            "order_type": str(self.paper_order_type_combo.currentText() or ""),
            "combined_watchlist_name": self.guide_auto_combined_watchlist_edit.text().strip(),
            "create_individual_watchlists": self.guide_auto_individual_watchlists_cb.isChecked(),
            "interval_minutes": int(self.guide_auto_interval_combo.currentData() or 15),
            "start_time": self.guide_auto_start_time.time().toString("HH:mm"),
            "end_time": self.guide_auto_end_time.time().toString("HH:mm"),
            "trading_days_only": self.guide_auto_trading_days_cb.isChecked(),
            "telegram_enabled": self.guide_auto_telegram_cb.isChecked(),
            "automation_enabled": self.guide_auto_enabled_cb.isChecked(),
            "technical_adjusted": self.guide_auto_adjusted_cb.isChecked(),
            "include_current": self.guide_auto_include_current_cb.isChecked(),
        }
        return normalize_strategy_automation_config(config)

    def _save_strategy_automation_recipe(self) -> bool:
        config = self._strategy_automation_config_from_ui()
        recipe_name = str(
            self._guide_automation_active_recipe
            or config.get("recipe_name", "") or ""
        ).strip()
        config["recipe_name"] = recipe_name
        if not recipe_name:
            QMessageBox.warning(self, "Recipe name required", "Enter a Strategy Guide recipe name.")
            return False
        if (
            not self._guide_automation_active_recipe
            and recipe_name in self._guide_automation_recipes
        ):
            QMessageBox.warning(
                self,
                "Duplicate recipe",
                f"A recipe named '{recipe_name}' already exists. Select it or use Save As.",
            )
            return False
        if not config["combined_watchlist_name"]:
            QMessageBox.warning(self, "Watchlist name required", "Enter the combined Step 1A watchlist name.")
            return False
        if not re.fullmatch(r"[A-Za-z0-9\s_-]+", config["combined_watchlist_name"]):
            QMessageBox.warning(self, "Invalid watchlist name", "Use only letters, numbers, spaces, hyphens and underscores.")
            return False
        try:
            existing_watchlists = WatchlistStore().load()
        except WatchlistStoreError as exc:
            QMessageBox.warning(self, "Watchlists unavailable", str(exc))
            return False
        if (
            config["combined_watchlist_name"] in UNIVERSES
            and config["combined_watchlist_name"] not in existing_watchlists
        ):
            QMessageBox.warning(
                self,
                "Reserved watchlist name",
                "Choose a name that is not a built-in market universe.",
            )
            return False
        if config["start_time"] > config["end_time"]:
            QMessageBox.warning(
                self,
                "Invalid schedule window",
                "The automation start time must be earlier than or equal to the end time.",
            )
            return False
        if not config["feeder_templates"]:
            QMessageBox.warning(self, "No feeders", "Select at least one General Scan feeder template.")
            return False
        if not config["candidate_templates"]:
            QMessageBox.warning(self, "No candidate rules", "Select at least one Candidate BUY rule on Guidance Setup.")
            return False
        if config["buy_mode"] == "SAVED" and not config["buy_finalset"]:
            QMessageBox.warning(self, "No BUY set", "Choose a saved BUY Final Rule Set.")
            return False
        if config["exit_mode"] == "SAVED" and not config["exit_finalset"]:
            QMessageBox.warning(self, "No EXIT set", "Choose a saved EXIT Final Rule Set.")
            return False
        if config["track_model"] and not config["model_account"]:
            QMessageBox.warning(self, "No model portfolio", "Select a model portfolio or turn off model tracking.")
            return False
        for other_name, other_config in self._guide_automation_recipes.items():
            if other_name == recipe_name:
                continue
            if str(other_config.get("combined_watchlist_name", "") or "").strip().upper() == config["combined_watchlist_name"].upper():
                QMessageBox.warning(
                    self,
                    "Watchlist already used",
                    f"Recipe '{other_name}' already writes to this combined watchlist. "
                    "Use a unique combined Step 1A watchlist for each recipe.",
                )
                return False
        previous = self._guide_automation_recipes.get(recipe_name, {})
        for history_key in (
            "last_run_at",
            "last_status",
            "last_buy_count",
            "last_exit_count",
            "last_source_count",
        ):
            if history_key in previous:
                config[history_key] = copy.deepcopy(previous[history_key])
        self._guide_automation_recipes[recipe_name] = copy.deepcopy(config)
        self._guide_automation_active_recipe = recipe_name
        if not self._persist_strategy_automation_recipes():
            return False
        self._guide_automation_config = config
        self._refresh_strategy_automation_recipe_combo(recipe_name)
        self.guide_auto_status_lbl.setText(
            f"Saved recipe '{recipe_name}'."
        )
        return True

    def _update_strategy_automation_buttons(self) -> None:
        if not hasattr(self, "guide_auto_start_btn"):
            return
        running = bool(
            self._guide_automation_worker
            and self._guide_automation_worker.isRunning()
        )
        self.guide_auto_start_btn.setEnabled(not self._guide_automation_active)
        self.guide_auto_stop_btn.setEnabled(self._guide_automation_active)
        self.guide_auto_run_btn.setEnabled(not running)
        self.guide_auto_save_btn.setEnabled(not running)
        self.guide_auto_start_btn.setText(
            "Automation Running" if self._guide_automation_active else "Start Automation"
        )

    def _start_strategy_automation(self, *, run_once: bool) -> None:
        if not self._save_strategy_automation_recipe():
            return
        if self._guide_automation_worker and self._guide_automation_worker.isRunning():
            QMessageBox.information(
                self, "Cycle already running", "The Strategy Guide cycle is already running."
            )
            return
        if not run_once:
            enabled_names = [
                name
                for name, config in self._guide_automation_recipes.items()
                if bool(config.get("automation_enabled", False))
            ]
            if not enabled_names:
                QMessageBox.information(
                    self,
                    "No enabled recipes",
                    "Enable 'Include this recipe when Start Automation is running' "
                    "for at least one saved recipe.",
                )
                return
            self._guide_automation_active = True
            now = get_ist_now()
            self._guide_automation_next_due = {
                name: now for name in enabled_names
            }
            self.guide_auto_status_lbl.setText(
                f"Automation started for {len(enabled_names)} enabled recipe(s); "
                "preparing the queue…"
            )
        self._update_strategy_automation_buttons()
        self._run_strategy_automation_cycle(
            manual=run_once,
            recipe_name=self._guide_automation_active_recipe if run_once else "",
        )

    def _stop_strategy_automation(self) -> None:
        self._guide_automation_active = False
        self._guide_automation_timer.stop()
        self._guide_automation_next_due.clear()
        if self._guide_automation_worker and self._guide_automation_worker.isRunning():
            self.guide_auto_status_lbl.setText(
                "Automation stopped; the current cycle will finish but will not repeat."
            )
        else:
            self.guide_auto_status_lbl.setText("Automation stopped.")
        self._update_strategy_automation_buttons()

    def _strategy_automation_window_open(
        self, now: datetime, config: dict[str, Any]
    ) -> bool:
        if bool(config.get("trading_days_only", True)):
            try:
                if not NSE_CALENDAR.is_session_date(now.date()):
                    return False
            except Exception:
                if now.weekday() >= 5:
                    return False
        try:
            start = datetime.strptime(str(config.get("start_time", "09:15")), "%H:%M").time()
            end = datetime.strptime(str(config.get("end_time", "15:30")), "%H:%M").time()
        except ValueError:
            return False
        return start <= now.time() <= end

    def _strategy_automation_guide_kwargs(
        self, config: dict[str, Any]
    ) -> dict[str, Any]:
        buy_mode = str(config.get("buy_mode", "AND") or "AND")
        exit_mode = str(config.get("exit_mode", "OR") or "OR")
        buy_setup = default_setup()
        buy_setup.update({
            "algo_capital_mode": str(config.get("capital_mode", "") or ""),
            "algo_max_slots": int(config.get("max_slots", 5) or 5),
            "algo_capital_per_stock": float(config.get("fixed_capital", 5000) or 5000),
            "algo_product": str(config.get("product", "CNC (Delivery)") or "CNC (Delivery)"),
            "algo_order_type": str(config.get("order_type", "MARKET") or "MARKET"),
        })
        return {
            "account": (
                str(config.get("model_account", "") or "").strip().upper()
                if bool(config.get("track_model", False)) else ""
            ),
            "member_names": list(config.get("candidate_templates", [])),
            "finalset_name": (
                str(config.get("buy_finalset", "") or "")
                if buy_mode == "SAVED" else ""
            ),
            "buy_op": buy_mode if buy_mode != "SAVED" else "AND",
            "buy_k": int(config.get("buy_k", 1) or 1),
            "exit_names": list(config.get("exit_templates", [])),
            "exit_finalset_name": (
                str(config.get("exit_finalset", "") or "")
                if exit_mode == "SAVED" else ""
            ),
            "exit_op": exit_mode if exit_mode != "SAVED" else "OR",
            "exit_k": int(config.get("exit_k", 1) or 1),
            "auto_watchlist": bool(config.get("auto_paper_watchlists", True)),
            "track_model": bool(config.get("track_model", False)),
            "technical_adjusted": bool(config.get("technical_adjusted", True)),
            "include_current": bool(config.get("include_current", True)),
            "buy_setup": buy_setup,
        }

    def _run_strategy_automation_cycle(
        self, manual: bool = False, recipe_name: str = ""
    ) -> None:
        if not manual and not self._guide_automation_active:
            return
        if self._guide_automation_worker and self._guide_automation_worker.isRunning():
            return
        now = get_ist_now()
        if manual:
            selected_name = str(
                recipe_name or self._guide_automation_active_recipe or ""
            ).strip()
            config = copy.deepcopy(
                self._guide_automation_recipes.get(
                    selected_name, self._guide_automation_config
                )
            )
        else:
            enabled = {
                name: config
                for name, config in self._guide_automation_recipes.items()
                if bool(config.get("automation_enabled", False))
            }
            for name in list(self._guide_automation_next_due):
                if name not in enabled:
                    self._guide_automation_next_due.pop(name, None)
            for name in enabled:
                self._guide_automation_next_due.setdefault(name, now)
            due_names = [
                name
                for name, due in self._guide_automation_next_due.items()
                if name in enabled
                and due <= now
                and self._strategy_automation_window_open(now, enabled[name])
            ]
            if not due_names:
                self.guide_auto_status_lbl.setText(
                    f"Automation queue active for {len(enabled)} recipe(s); "
                    "no recipe is currently due inside its trading window."
                )
                self._guide_automation_timer.start(60_000)
                return
            selected_name = min(
                due_names,
                key=lambda name: (self._guide_automation_next_due[name], name.upper()),
            )
            config = copy.deepcopy(enabled[selected_name])
        busy = bool(
            (self.worker and self.worker.isRunning())
            or (self.history_worker and self.history_worker.isRunning())
            or (getattr(self, "paper_pipeline_worker", None) is not None and self.paper_pipeline_worker.isRunning())
            or (self.telegram_worker and self.telegram_worker.isRunning())
        )
        if busy:
            self.guide_auto_status_lbl.setText(
                "Waiting for the current scan/Telegram operation to finish…"
            )
            if self._guide_automation_active:
                self._guide_automation_timer.start(15_000)
            else:
                QMessageBox.information(
                    self, "Scanner busy", "Finish the current scan or Telegram operation, then run the cycle again."
                )
            return
        config = normalize_strategy_automation_config(config)
        config["recipe_name"] = selected_name
        self._guide_automation_running_recipe = selected_name
        self._guide_automation_run_config = copy.deepcopy(config)
        self._guide_automation_run_manual = bool(manual)
        self.guide_auto_progress.setValue(1)
        self.guide_auto_progress.setFormat("%p% — Preparing feeder scans…")
        self.guide_auto_status_lbl.setText(
            f"Running Strategy Guide recipe '{selected_name}'…"
        )
        self.guide_auto_run_btn.setEnabled(False)
        self.run_paper_pipeline_btn.setEnabled(False)
        worker = StrategyAutomationWorker(
            config=config,
            guide_kwargs=self._strategy_automation_guide_kwargs(config),
            templates=self._paper_template_names(),
            cache_path=PRICE_CACHE_DB,
        )
        self._guide_automation_worker = worker
        worker.progress.connect(self._on_strategy_automation_progress)
        worker.succeeded.connect(self._on_strategy_automation_finished)
        worker.failed.connect(self._on_strategy_automation_failed)
        worker.finished.connect(self._strategy_automation_worker_finished)
        self._update_strategy_automation_buttons()
        worker.start()

    def _on_strategy_automation_progress(self, percent: int, text: str) -> None:
        value = max(0, min(100, int(percent)))
        message = str(text or "Working…")
        self.guide_auto_progress.setValue(value)
        self.guide_auto_progress.setFormat(f"%p% — {message}")
        self.guide_auto_status_lbl.setText(message)
        self.paper_pipeline_progress.setVisible(True)
        self.paper_pipeline_progress.setValue(value)
        self.paper_pipeline_progress.setFormat(f"%p% — {message}")

    def _select_strategy_guide_source(self, name: str) -> None:
        self._populate_guide_source_list()
        self._set_checked_list_values(self.guide_source_list, [name])
        self._update_strategy_guide_summary()

    def _record_strategy_automation_run_status(
        self, status: str, **fields: Any
    ) -> None:
        name = self._guide_automation_running_recipe
        if not name or name not in self._guide_automation_recipes:
            return
        config = self._guide_automation_recipes[name]
        config["last_run_at"] = f"{get_ist_now():%Y-%m-%d %H:%M:%S} IST"
        config["last_status"] = str(status)
        config.update(fields)
        try:
            self.store.save_strategy_automation_recipes({
                "active": self._guide_automation_active_recipe,
                "recipes": self._guide_automation_recipes,
            })
        except (TemplateStoreError, OSError):
            pass
        if hasattr(self, "guide_auto_recipe_combo"):
            index = self.guide_auto_recipe_combo.findData(name)
            if index >= 0:
                marker = "●" if bool(config.get("automation_enabled", False)) else "○"
                self.guide_auto_recipe_combo.setItemText(
                    index, f"{marker} {name} — {status}"
                )
        if name == self._guide_automation_active_recipe:
            self.guide_auto_recipe_last_run_lbl.setText(
                f"Last run: {config['last_run_at']} | {status}"
            )

    def _on_strategy_automation_finished(self, payload: dict[str, Any]) -> None:
        self.guide_auto_progress.setValue(100)
        self.guide_auto_progress.setFormat("100% — Full cycle complete")
        self._show_pipeline_shortlist(payload.get("guidance_rows", []) or [])
        self._refresh_paper_pipeline_options()
        self._sync_universe_list()
        combined_name = str(payload.get("combined_watchlist_name", "") or "")
        if combined_name:
            self._select_strategy_guide_source(combined_name)
        buy_count = int(payload.get("buy_signal_count", 0) or 0)
        exit_count = int(payload.get("exit_signal_count", 0) or 0)
        source_count = int(payload.get("combined_source_count", 0) or 0)
        self._record_strategy_automation_run_status(
            f"OK: BUY {buy_count}, EXIT {exit_count}",
            last_buy_count=buy_count,
            last_exit_count=exit_count,
            last_source_count=source_count,
        )
        self.guide_auto_status_lbl.setText(
            f"Cycle complete: source {source_count:,} | BUY {buy_count:,} | "
            f"EXIT/weakness {exit_count:,}."
        )
        self.paper_pipeline_status.setText(self.guide_auto_status_lbl.text())
        if bool(payload.get("model_tracking", False)):
            self._mark_paper_trades_dirty()
        if bool(self._guide_automation_run_config.get("telegram_enabled", True)):
            self._send_strategy_automation_telegram(payload)
        self._schedule_next_strategy_automation_cycle()

    def _on_strategy_automation_failed(self, details: str) -> None:
        clean = str(details or "Strategy Guide automation failed.")
        self._record_strategy_automation_run_status("FAILED")
        self.guide_auto_progress.setFormat("Automation cycle failed")
        self.guide_auto_status_lbl.setText("Cycle failed; generated watchlists were not partially replaced.")
        self.paper_pipeline_status.setText("Automated Strategy Guide cycle failed.")
        if bool(self._guide_automation_run_config.get("telegram_enabled", True)):
            self._send_strategy_automation_failure_telegram(clean)
        if not self._guide_automation_active:
            QMessageBox.warning(self, "Strategy Guide automation failed", clean[:1800])
        self._schedule_next_strategy_automation_cycle()

    def _strategy_automation_worker_finished(self) -> None:
        self._guide_automation_worker = None
        self._guide_automation_running_recipe = ""
        self._guide_automation_run_config = {}
        self._guide_automation_run_manual = False
        self.run_paper_pipeline_btn.setEnabled(True)
        self._update_strategy_automation_buttons()

    def _schedule_next_strategy_automation_cycle(self) -> None:
        if not self._guide_automation_active:
            return
        recipe_name = self._guide_automation_running_recipe
        if recipe_name and not self._guide_automation_run_manual:
            minutes = int(
                self._guide_automation_run_config.get("interval_minutes", 15) or 15
            )
            self._guide_automation_next_due[recipe_name] = (
                get_ist_now() + timedelta(minutes=max(1, minutes))
            )
        else:
            minutes = 0
        self._guide_automation_timer.start(1_000)
        self.guide_auto_status_lbl.setText(
            self.guide_auto_status_lbl.text()
            + (
                f" '{recipe_name}' is next due in {minutes} minute(s); checking the queue."
                if recipe_name and minutes else " Checking the enabled-recipe queue."
            )
        )

    def _start_strategy_telegram_worker(self, messages: list[str]) -> None:
        if not messages:
            return
        if self.telegram_worker and self.telegram_worker.isRunning():
            self.guide_auto_status_lbl.setText(
                self.guide_auto_status_lbl.text() + " Telegram skipped: another send is in progress."
            )
            return
        try:
            token, destinations = self.telegram_settings_store.load()
            enabled = [item for item in destinations if bool(item.get("enabled", False))]
            if not token or not enabled:
                raise TelegramSettingsError(
                    "Configure a bot token and at least one enabled Telegram destination."
                )
            deliveries = [
                TelegramDelivery(
                    f"strategy_automation:{item['key']}:{item['chat_id']}",
                    str(item["label"]),
                    str(item["chat_id"]),
                    list(messages),
                )
                for item in enabled
            ]
        except (TelegramSettingsError, OSError, ValueError) as exc:
            self.guide_auto_status_lbl.setText(
                self.guide_auto_status_lbl.text() + f" Telegram not sent: {exc}"
            )
            return
        self.telegram_worker = TelegramSendWorker(token, deliveries)
        self.telegram_worker.succeeded.connect(
            lambda count: self.guide_auto_status_lbl.setText(
                self.guide_auto_status_lbl.text()
                + f" Telegram report sent ({count} message(s))."
            )
        )
        self.telegram_worker.failed.connect(
            lambda error: self.guide_auto_status_lbl.setText(
                self.guide_auto_status_lbl.text() + f" Telegram failed: {error}"
            )
        )
        self.telegram_worker.finished.connect(self._telegram_worker_finished)
        self.telegram_worker.start()

    def _send_strategy_automation_telegram(self, payload: dict[str, Any]) -> None:
        self._start_strategy_telegram_worker(
            build_strategy_guide_telegram_messages(
                payload, self._guide_automation_run_config
            )
        )

    def _send_strategy_automation_failure_telegram(self, details: str) -> None:
        recipe = str(self._guide_automation_run_config.get("recipe_name", "") or "Strategy Guide")
        lines = [
            "ChartSage Automated Strategy Guide — FAILED",
            f"Recipe: {recipe}",
            f"Generated: {get_ist_now():%Y-%m-%d %H:%M:%S} IST",
            "The feeder/watchlist/Strategy Guide cycle did not complete.",
            "Existing generated watchlists were preserved.",
            "",
            str(details)[-1200:],
        ]
        self._start_strategy_telegram_worker(
            chunk_telegram_lines(lines, f"ChartSage Strategy Guide — {recipe} failure (continued)")
        )

    def _populate_guide_source_list(self) -> None:
        """Show every stored watchlist plus the built-in market universes."""
        if not hasattr(self, "guide_source_list"):
            return
        checked = {
            str(self.guide_source_list.item(i).data(Qt.ItemDataRole.UserRole) or "")
            for i in range(self.guide_source_list.count())
            if self.guide_source_list.item(i).checkState()
            == Qt.CheckState.Checked
        }
        try:
            watchlists = WatchlistStore().load()
        except WatchlistStoreError:
            watchlists = {}
        for name, symbols in watchlists.items():
            UNIVERSES[str(name)] = list(symbols)
        custom_names = set(watchlists)

        def source_sort_key(name: str) -> tuple[int, str]:
            if name in custom_names:
                return (0, name.upper())
            return (1, name.upper())

        self.guide_source_list.blockSignals(True)
        self.guide_source_list.clear()
        for name in sorted(UNIVERSES, key=source_sort_key):
            source = UNIVERSES.get(name)
            count = len(source) if isinstance(source, list) else None
            if is_holdings_universe(name):
                kind = "ZERODHA WATCHLIST"
            elif name in custom_names:
                kind = "WATCHLIST"
            else:
                kind = "UNIVERSE"
            count_text = f" ({count:,})" if count is not None else ""
            item = QListWidgetItem(f"[{kind}] {name}{count_text}")
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if name in checked else Qt.CheckState.Unchecked
            )
            item.setToolTip(
                f"{kind.title()}: {name}\n"
                "Check to include its symbols in the Strategy Guide source union."
            )
            self.guide_source_list.addItem(item)
        self.guide_source_list.blockSignals(False)
        if hasattr(self, "strategy_guide_summary"):
            self._update_strategy_guide_summary()

    def _checked_guide_source_names(self) -> list[str]:
        if not hasattr(self, "guide_source_list"):
            return []
        return [
            str(self.guide_source_list.item(i).data(Qt.ItemDataRole.UserRole) or "").strip()
            for i in range(self.guide_source_list.count())
            if self.guide_source_list.item(i).checkState()
            == Qt.CheckState.Checked
            and str(
                self.guide_source_list.item(i).data(Qt.ItemDataRole.UserRole) or ""
            ).strip()
        ]

    def _populate_paper_member_list(self) -> None:
        if not hasattr(self, "paper_member_list"):
            return
        checked = {
            self.paper_member_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.paper_member_list.count())
            if self.paper_member_list.item(i).checkState() == Qt.CheckState.Checked
        }
        templates = self._paper_template_names()
        self.paper_member_list.blockSignals(True)
        self.paper_member_list.clear()
        for name, setup in sorted(
            templates.items(), key=lambda kv: str(kv[0]).upper()
        ):
            if is_final_template_setup(name, setup):
                continue
            if strategy_guide_template_role(name, setup) != "BUY":
                continue
            try:
                validate_ast(setup.get("expression", default_setup()["expression"]))
            except ValueError:
                continue
            universe = strategy_guide_universe_label(setup)
            item = QListWidgetItem(f"[CANDIDATE RULE]  {name}")
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if name in checked else Qt.CheckState.Unchecked
            )
            item.setToolTip(
                f"Candidate/BUY rule template\nSaved template source: {universe}\n"
                "If Step 1A has checked sources, those sources override this saved "
                "source for the Guide run."
            )
            self.paper_member_list.addItem(item)
        self.paper_member_list.blockSignals(False)
        if hasattr(self, "strategy_guide_summary"):
            self._update_strategy_guide_summary()

    def _populate_paper_finalset_combo(self) -> None:
        if not hasattr(self, "paper_finalset_combo"):
            return
        current = str(self.paper_finalset_combo.currentData() or "").strip()
        templates = self._paper_template_names()
        self.paper_finalset_combo.blockSignals(True)
        self.paper_finalset_combo.clear()
        self.paper_finalset_combo.addItem(
            "— Select saved BUY set —", ""
        )
        for name, setup in sorted(
            templates.items(), key=lambda kv: str(kv[0]).upper()
        ):
            if not is_final_template_setup(name, setup):
                continue
            if str(setup.get("finalset_side", "BUY") or "BUY").upper() == "EXIT":
                continue
            op = str(setup.get("finalset_op", "AND") or "AND")
            k = int(setup.get("finalset_k", 1) or 1)
            op_tag = op if op in ("AND", "OR") else f"{op} {k}"
            self.paper_finalset_combo.addItem(f"⚡ {name} [{op_tag}]", name)
        self.paper_finalset_combo.blockSignals(False)
        idx = self.paper_finalset_combo.findData(current)
        self.paper_finalset_combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _populate_paper_exit_finalset_combo(self) -> None:
        if not hasattr(self, "paper_exit_finalset_combo"):
            return
        current = str(self.paper_exit_finalset_combo.currentData() or "").strip()
        templates = self._paper_template_names()
        self.paper_exit_finalset_combo.blockSignals(True)
        self.paper_exit_finalset_combo.clear()
        self.paper_exit_finalset_combo.addItem(
            "— Select saved EXIT set —", ""
        )
        for name, setup in sorted(
            templates.items(), key=lambda kv: str(kv[0]).upper()
        ):
            if not is_final_template_setup(name, setup):
                continue
            if str(setup.get("finalset_side", "BUY") or "BUY").upper() != "EXIT":
                continue
            op = str(setup.get("finalset_op", "AND") or "AND")
            k = int(setup.get("finalset_k", 1) or 1)
            op_tag = op if op in ("AND", "OR") else f"{op} {k}"
            self.paper_exit_finalset_combo.addItem(f"⚡ {name} [{op_tag}]", name)
        self.paper_exit_finalset_combo.blockSignals(False)
        idx = self.paper_exit_finalset_combo.findData(current)
        self.paper_exit_finalset_combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _populate_paper_exit_list(self) -> None:
        if not hasattr(self, "paper_exit_list"):
            return
        checked = {
            self.paper_exit_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.paper_exit_list.count())
            if self.paper_exit_list.item(i).checkState() == Qt.CheckState.Checked
        }
        templates = self._paper_template_names()
        self.paper_exit_list.blockSignals(True)
        self.paper_exit_list.clear()
        for name, setup in sorted(
            templates.items(), key=lambda kv: str(kv[0]).upper()
        ):
            if is_final_template_setup(name, setup):
                continue
            if strategy_guide_template_role(name, setup) != "EXIT":
                continue
            try:
                validate_ast(setup.get("expression", default_setup()["expression"]))
            except ValueError:
                continue
            universe = strategy_guide_universe_label(setup)
            position_dependent = expression_has_pnl_rules(setup.get("expression"))
            prefix = "[POSITION-DEPENDENT]" if position_dependent else "[EXIT]"
            item = QListWidgetItem(f"{prefix} RULE  {name}")
            item.setData(Qt.ItemDataRole.UserRole, name)
            if position_dependent:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                item.setToolTip(
                    "Unavailable in broad guidance: this template needs an existing "
                    "position's buy price or P&L. Use a technical EXIT rule instead."
                )
            else:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked if name in checked else Qt.CheckState.Unchecked
                )
                item.setToolTip(
                    f"EXIT/weakness rule template\nSaved template source: {universe}\n"
                    "Strategy Guide applies this rule to the complete Step 1 source."
                )
            self.paper_exit_list.addItem(item)
        self.paper_exit_list.blockSignals(False)
        if hasattr(self, "strategy_guide_summary"):
            self._update_strategy_guide_summary()

    @staticmethod
    def _checked_guide_template_names(widget: QListWidget) -> list[str]:
        return [
            str(widget.item(i).data(Qt.ItemDataRole.UserRole) or "").strip()
            for i in range(widget.count())
            if widget.item(i).checkState() == Qt.CheckState.Checked
            and str(widget.item(i).data(Qt.ItemDataRole.UserRole) or "").strip()
        ]

    def _change_guide_template_role(self, widget: QListWidget) -> None:
        item = widget.currentItem()
        if item is None:
            QMessageBox.information(
                self, "Highlight a template",
                "Highlight one template first, then choose its Strategy Guide role.",
            )
            return
        name = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
        if not name:
            return
        current_setup = self._paper_template_names().get(name, {})
        current_role = strategy_guide_template_role(name, current_setup)
        choices = ["Candidate / BUY", "EXIT / Weakness"]
        choice, ok = QInputDialog.getItem(
            self,
            "Strategy Guide template role",
            f'How should "{name}" be used?',
            choices,
            0 if current_role == "BUY" else 1,
            False,
        )
        if not ok:
            return
        role = "BUY" if choice == choices[0] else "EXIT"
        try:
            self.store.update_template_fields(name, {"strategy_guide_role": role})
        except TemplateStoreError as exc:
            QMessageBox.warning(self, "Role update failed", str(exc))
            return
        self._refresh_paper_pipeline_options()

    def _update_guide_controls(self, *_args: Any) -> None:
        buy_mode = str(self.paper_buy_mode_combo.currentData() or "AND")
        exit_mode = str(self.paper_exit_mode_combo.currentData() or "OR")
        buy_saved = buy_mode == "SAVED"
        exit_saved = exit_mode == "SAVED"
        for widget in (
            self.paper_saved_buy_lbl, self.paper_finalset_combo,
            self.build_finalset_btn, self.edit_finalset_btn, self.delete_finalset_btn,
        ):
            widget.setEnabled(buy_saved)
        for widget in (
            self.paper_saved_exit_lbl, self.paper_exit_finalset_combo,
            self.build_exit_finalset_btn, self.edit_exit_finalset_btn,
            self.delete_exit_finalset_btn,
        ):
            widget.setEnabled(exit_saved)
        self.paper_buy_k_spin.setEnabled(buy_mode == "ANY K")
        self.paper_exit_k_spin.setEnabled(exit_mode == "ANY K")
        buy_count = max(1, len(self._checked_guide_template_names(self.paper_member_list)))
        exit_count = max(1, len(self._checked_guide_template_names(self.paper_exit_list)))
        self.paper_buy_k_spin.setMaximum(buy_count)
        self.paper_exit_k_spin.setMaximum(exit_count)
        self._update_paper_finalset_buttons()
        self._update_paper_exit_finalset_buttons()
        self._update_strategy_guide_summary()

    def _update_strategy_guide_summary(self, *_args: Any) -> None:
        if not hasattr(self, "strategy_guide_summary"):
            return
        source_names = self._checked_guide_source_names()
        buy_names = self._checked_guide_template_names(self.paper_member_list)
        exit_names = self._checked_guide_template_names(self.paper_exit_list)
        self.guide_source_count_lbl.setText(
            f"{len(source_names)} selected"
            if source_names else "Using template sources"
        )
        self.paper_member_count_lbl.setText(f"{len(buy_names)} selected")
        self.paper_exit_count_lbl.setText(f"{len(exit_names)} selected")
        self.paper_buy_k_spin.setMaximum(max(1, len(buy_names)))
        self.paper_exit_k_spin.setMaximum(max(1, len(exit_names)))
        buy_mode = str(self.paper_buy_mode_combo.currentData() or "AND")
        exit_mode = str(self.paper_exit_mode_combo.currentData() or "OR")
        if buy_mode == "SAVED":
            buy_text = str(self.paper_finalset_combo.currentData() or "no saved set selected")
        elif buy_mode == "AND":
            buy_text = "all checked candidate rules match"
        elif buy_mode == "OR":
            buy_text = "any checked candidate rule matches"
        else:
            buy_text = f"at least {self.paper_buy_k_spin.value()} checked candidate rules match"
        if exit_mode == "SAVED":
            exit_text = str(self.paper_exit_finalset_combo.currentData() or "no saved set selected")
        elif not exit_names:
            exit_text = "no EXIT/weakness guidance"
        elif exit_mode == "AND":
            exit_text = "all checked exit rules match"
        elif exit_mode == "OR":
            exit_text = "any checked exit rule matches"
        else:
            exit_text = f"at least {self.paper_exit_k_spin.value()} checked exit rules match"
        templates = self._paper_template_names()
        universe_sets = {
            tuple(str(u).strip() for u in templates.get(name, {}).get("universes", []) if str(u).strip())
            for name in buy_names
        }
        blockers: list[str] = []
        cautions: list[str] = []
        if not buy_names:
            blockers.append("Select at least one Step 1B candidate BUY rule template.")
        if not source_names and len(universe_sets) > 1:
            cautions.append("Checked candidate templates use different saved universes.")
        if buy_mode == "SAVED" and not str(self.paper_finalset_combo.currentData() or "").strip():
            blockers.append("Choose a saved BUY Final Rule Set.")
        if exit_mode == "SAVED" and not str(self.paper_exit_finalset_combo.currentData() or "").strip():
            blockers.append("Choose a saved EXIT Final Rule Set.")
        model_text = (
            f"record simulated actions in {self.paper_pipeline_account_combo.currentText()}"
            if self.guide_track_model_cb.isChecked() else "guidance only; model tracking is off"
        )
        source_text = (
            f"the union of {len(source_names)} selected source list(s)"
            if source_names else "each template's saved universe"
        )
        summary = (
            f"Recipe: scan {source_text} with {len(buy_names)} candidate template(s); "
            f"suggest BUY when {buy_text}; "
            f"suggest EXIT/weakness when {exit_text}; {model_text}."
        )
        messages = blockers + cautions
        if messages:
            summary += "  ⚠ " + " ".join(messages)
        self.strategy_guide_summary.setText(summary)
        self.run_paper_pipeline_btn.setEnabled(not blockers)

    def _open_build_finalset_dialog(self, side: str = "BUY") -> None:
        dialog = BuildFinalRuleSetDialog(
            parent=self, templates=self._paper_template_names(), side=side
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._refresh_paper_pipeline_options()

    def _update_paper_finalset_buttons(self) -> None:
        if not hasattr(self, "edit_finalset_btn"):
            return
        saved_mode = (
            not hasattr(self, "paper_buy_mode_combo")
            or str(self.paper_buy_mode_combo.currentData() or "") == "SAVED"
        )
        selected = saved_mode and bool(
            str(self.paper_finalset_combo.currentData() or "").strip()
        )
        self.edit_finalset_btn.setEnabled(selected)
        self.delete_finalset_btn.setEnabled(selected)

    def _update_paper_exit_finalset_buttons(self) -> None:
        if not hasattr(self, "edit_exit_finalset_btn"):
            return
        saved_mode = (
            not hasattr(self, "paper_exit_mode_combo")
            or str(self.paper_exit_mode_combo.currentData() or "") == "SAVED"
        )
        selected = saved_mode and bool(
            str(self.paper_exit_finalset_combo.currentData() or "").strip()
        )
        self.edit_exit_finalset_btn.setEnabled(selected)
        self.delete_exit_finalset_btn.setEnabled(selected)

    def _open_edit_finalset_dialog(
        self, combo: QComboBox, side: str = "BUY"
    ) -> None:
        name = str(combo.currentData() or "").strip()
        if not name:
            return
        setup = TemplateStore().load_all().get(name)
        if not setup:
            return
        dialog = BuildFinalRuleSetDialog(
            parent=self,
            templates=self._paper_template_names(),
            existing_name=name,
            existing_setup=setup,
            side=side,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._refresh_paper_pipeline_options()

    def _delete_paper_finalset(self, combo: QComboBox) -> None:
        name = str(combo.currentData() or "").strip()
        if not name:
            return
        if QMessageBox.question(
            self, "Delete FINAL RULE SET", f'Delete FINAL RULE SET "{name}"?'
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            TemplateStore().delete(name)
        except TemplateStoreError as exc:
            QMessageBox.warning(self, "Delete failed", str(exc))
            return
        self._refresh_paper_pipeline_options()

    def _refresh_paper_pipeline_options(self) -> None:
        self._populate_guide_source_list()
        self._populate_strategy_feeder_list()
        self._populate_paper_member_list()
        self._populate_paper_finalset_combo()
        self._populate_paper_exit_finalset_combo()
        self._populate_paper_exit_list()
        self._refresh_paper_watchlists_panel()
        self._refresh_templates()
        if hasattr(self, "paper_buy_mode_combo"):
            self._update_guide_controls()

    def _refresh_paper_watchlists_panel(self) -> None:
        if not hasattr(self, "paper_watchlist_combo"):
            return
        try:
            watchlists = WatchlistStore().load()
        except WatchlistStoreError:
            watchlists = {}
        paper_lists = {
            str(name): list(symbols)
            for name, symbols in watchlists.items()
            if str(name).startswith(PAPER_WATCHLIST_PREFIX)
        }
        current = str(self.paper_watchlist_combo.currentData() or "")
        self.paper_watchlist_combo.blockSignals(True)
        self.paper_watchlist_combo.clear()
        for name in sorted(paper_lists.keys(), key=str.upper):
            self.paper_watchlist_combo.addItem(
                f"{name} ({len(paper_lists[name])})", name
            )
        self.paper_watchlist_combo.blockSignals(False)
        if current in paper_lists:
            idx = self.paper_watchlist_combo.findData(current)
            self.paper_watchlist_combo.setCurrentIndex(idx)
        self.paper_watchlist_count_lbl.setText(
            f"{len(paper_lists)} auto-updated PAPER watchlist(s)."
        )

    def _run_paper_pipeline(self) -> None:
        if (
            self._guide_automation_worker
            and self._guide_automation_worker.isRunning()
        ):
            QMessageBox.information(
                self,
                "Automation cycle running",
                "Wait for the automated Strategy Guide cycle to finish.",
            )
            return
        track_model = self.guide_track_model_cb.isChecked()
        account = str(
            self.paper_pipeline_account_combo.currentData() or ""
        ).strip().upper()
        if track_model and not account:
            QMessageBox.warning(
                self, "No model portfolio",
                "Select a model portfolio, or turn off model-portfolio tracking.",
            )
            return
        member_names = self._checked_guide_template_names(self.paper_member_list)
        if not member_names:
            QMessageBox.warning(
                self, "No rule templates",
                "Check at least one candidate BUY rule template in Step 1B.",
            )
            return
        source_universes = self._checked_guide_source_names()
        if (
            getattr(self, "paper_pipeline_worker", None) is not None
            and self.paper_pipeline_worker.isRunning()
        ):
            return
        buy_mode = str(self.paper_buy_mode_combo.currentData() or "AND")
        exit_mode = str(self.paper_exit_mode_combo.currentData() or "OR")
        finalset_name = (
            str(self.paper_finalset_combo.currentData() or "").strip()
            if buy_mode == "SAVED" else ""
        )
        exit_names = self._checked_guide_template_names(self.paper_exit_list)
        exit_finalset_name = (
            str(self.paper_exit_finalset_combo.currentData() or "").strip()
            if exit_mode == "SAVED" else ""
        )
        if buy_mode == "SAVED" and not finalset_name:
            QMessageBox.warning(self, "No BUY set", "Choose a saved BUY Final Rule Set.")
            return
        if exit_mode == "SAVED" and not exit_finalset_name:
            QMessageBox.warning(self, "No EXIT set", "Choose a saved EXIT Final Rule Set.")
            return
        templates = self._paper_template_names()
        universe_sets = {
            tuple(str(u).strip() for u in templates.get(name, {}).get("universes", []) if str(u).strip())
            for name in member_names
        }
        if not source_universes and len(universe_sets) > 1 and QMessageBox.question(
            self,
            "Different candidate universes",
            "No Step 1A source override is selected, and the checked Step 1B "
            "templates use different saved universes. "
            "Their candidate results will be combined. Continue?",
        ) != QMessageBox.StandardButton.Yes:
            return
        buy_setup = default_setup()
        buy_setup.update({
            "algo_capital_mode": str(
                self.paper_capital_mode_combo.currentText() or ""
            ),
            "algo_max_slots": int(self.paper_max_slots_spin.value()),
            "algo_capital_per_stock": float(self.paper_capital_spin.value()),
            "algo_product": str(
                self.paper_product_combo.currentText() or "CNC (Delivery)"
            ),
            "algo_order_type": str(
                self.paper_order_type_combo.currentText() or "LIMIT"
            ),
        })
        self.paper_pipeline_status.setText("Running strategy guide…")
        self.paper_pipeline_progress.setVisible(True)
        self.paper_pipeline_progress.setValue(1)
        self.paper_pipeline_progress.setFormat("%p% — Preparing Strategy Guide recipe…")
        self.run_paper_pipeline_btn.setEnabled(False)
        self.paper_pipeline_worker = PaperPipelineWorker(
            account=account,
            member_names=member_names,
            source_universes=source_universes,
            finalset_name=finalset_name,
            buy_op=(buy_mode if buy_mode != "SAVED" else "AND"),
            buy_k=int(self.paper_buy_k_spin.value()),
            exit_names=exit_names,
            exit_finalset_name=exit_finalset_name,
            exit_op=(exit_mode if exit_mode != "SAVED" else "OR"),
            exit_k=int(self.paper_exit_k_spin.value()),
            auto_watchlist=self.paper_auto_watchlist_cb.isChecked(),
            buy_setup=buy_setup,
            templates=self._paper_template_names(),
            cache_path=PRICE_CACHE_DB,
            track_model=track_model,
            technical_adjusted=self.guide_adjusted_cb.isChecked(),
            include_current=self.guide_include_current_cb.isChecked(),
        )
        self.paper_pipeline_worker.succeeded.connect(
            self._on_paper_pipeline_finished
        )
        self.paper_pipeline_worker.progress.connect(
            self._on_paper_pipeline_progress
        )
        self.paper_pipeline_worker.failed.connect(
            self._on_paper_pipeline_failed
        )
        self.paper_pipeline_worker.finished.connect(
            self._paper_pipeline_worker_finished
        )
        self.paper_pipeline_worker.start()

    def _on_paper_pipeline_progress(self, percent: int, message: str) -> None:
        value = max(0, min(100, int(percent)))
        clean_message = str(message or "Working…").strip()
        self.paper_pipeline_progress.setVisible(True)
        self.paper_pipeline_progress.setValue(value)
        self.paper_pipeline_progress.setFormat(f"%p% — {clean_message}")
        self.paper_pipeline_status.setText(clean_message)

    def _on_paper_pipeline_finished(self, payload: dict[str, Any]) -> None:
        self.paper_pipeline_progress.setVisible(True)
        self.paper_pipeline_progress.setValue(100)
        self.paper_pipeline_progress.setFormat("100% — Strategy Guide complete")
        self._show_pipeline_shortlist(payload.get("guidance_rows", []))
        self._refresh_paper_pipeline_options()
        self._sync_universe_list()
        buy_results = payload.get("buy_results", []) or []
        sell_results = payload.get("sell_results", []) or []
        buy_placed = [r for r in buy_results if r.get("placed")]
        sell_placed = [r for r in sell_results if r.get("placed")]
        lines: list[str] = []
        if buy_placed:
            lines.append(f"🟢 MODEL BUYS ({len(buy_placed)}):")
            for r in buy_placed:
                entry = r.get("entry", {})
                lines.append(
                    f"   {r.get('symbol')} × {entry.get('qty')} @ ₹ {format_number(entry.get('fill_price'))}"
                )
        if sell_placed:
            lines.append(f"🔴 MODEL EXITS ({len(sell_placed)}):")
            for r in sell_placed:
                entry = r.get("entry", {})
                lines.append(
                    f"   {r.get('symbol')} × {entry.get('qty')} @ ₹ {format_number(entry.get('fill_price'))}"
                )
        skipped = [r for r in buy_results + sell_results if not r.get("placed")]
        if skipped:
            lines.append(f"⏭ Skipped ({len(skipped)}):")
            for r in skipped[:8]:
                lines.append(
                    f"   {r.get('action')} {r.get('symbol')} — {r.get('reason')}"
                )
        buy_signals = int(payload.get("buy_signal_count", 0) or 0)
        exit_signals = int(payload.get("exit_signal_count", 0) or 0)
        model_tracking = bool(payload.get("model_tracking", False))
        model_note = (
            f" Model: {len(buy_placed)} buys, {len(sell_placed)} exits."
            if model_tracking else " Model tracking is off."
        )
        self.paper_pipeline_status.setText(
            f"Done: {buy_signals} BUY, {exit_signals} EXIT/weakness signals.{model_note}"
        )
        if model_tracking:
            self._mark_paper_trades_dirty()
        if lines:
            QMessageBox.information(
                self, "Strategy Guide finished",
                "These are simulated model-portfolio actions only; no Zerodha orders were placed.\n\n"
                + "\n".join(lines),
            )

    def _on_paper_pipeline_failed(self, error: str) -> None:
        self.paper_pipeline_status.setText("Strategy guide failed.")
        self.paper_pipeline_progress.setVisible(True)
        self.paper_pipeline_progress.setFormat("Strategy Guide failed")
        QMessageBox.warning(self, "Strategy Guide failed", f"{error[:1500]}")

    def _paper_pipeline_worker_finished(self) -> None:
        self.paper_pipeline_worker = None
        self._update_strategy_guide_summary()

    def _show_pipeline_shortlist(self, rows: list[dict[str, Any]]) -> None:
        table = self.paper_shortlist_table
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            values = [
                row.get("Signal", ""),
                row.get("Symbol", ""),
                row.get("Close", ""),
                row.get("Confirmation Count", ""),
                row.get("Matched Members", ""),
                row.get("In Model Portfolio", "No"),
                row.get("In Zerodha", "No"),
                row.get("Zerodha Accounts", ""),
                row.get("Evidence", ""),
            ]
            for column, value in enumerate(values):
                table.setItem(r, column, QTableWidgetItem(str(value)))
        table.resizeColumnsToContents()
        table.horizontalHeader().setSectionResizeMode(
            8, QHeaderView.ResizeMode.Stretch
        )

    def _maybe_send_telegram_alert(
        self,
        rows: list[dict[str, Any]],
        setup: dict[str, Any],
    ) -> None:
        if not bool(setup.get("telegram_alerts_enabled", False)):
            self.telegram_status.setText("Telegram alerts are off.")
            self.telegram_status.setStyleSheet("color: #555; font-size: 11px")
            return
        if not self._within_auto_run_window(get_ist_now()):
            self.telegram_status.setText(
                f"Telegram alert skipped: outside the auto-run window "
                f"({self._auto_run_window_summary()})."
            )
            self.telegram_status.setStyleSheet("color: #9c5700; font-size: 11px")
            return
        template_name = str(setup.get("template_name", "")).strip()
        if not template_name or template_name.startswith("("):
            self.telegram_status.setText(
                "Telegram alert skipped: save this scan as a named template first."
            )
            self.telegram_status.setStyleSheet("color: #9c5700; font-size: 11px")
            return
        if self.telegram_worker and self.telegram_worker.isRunning():
            self.telegram_status.setText(
                "Telegram alert skipped because the previous alert is still sending; "
                "the next scan will catch up."
            )
            self.telegram_status.setStyleSheet("color: #9c5700; font-size: 11px")
            return
        try:
            token, destinations = self.telegram_settings_store.load()
            enabled_destinations = [
                destination
                for destination in destinations
                if bool(destination.get("enabled", False))
            ]
            if not enabled_destinations:
                self.telegram_status.setText(
                    "Telegram alerts are enabled for this template, but no Telegram "
                    "group is currently enabled. No alert was sent."
                )
                self.telegram_status.setStyleSheet("color: #555; font-size: 11px")
                return
            if not token:
                raise TelegramSettingsError(
                    "Open Telegram Settings and enter the bot token."
                )
            deliveries: list[TelegramDelivery] = []
            pending_state: dict[str, tuple[str, set[str]]] = {}
            unchanged_groups = 0
            for destination in enabled_destinations:
                sorted_unis = sorted(
                    str(u).strip() for u in setup.get("universes", []) if str(u).strip()
                )
                universe_suffix = ",".join(sorted_unis) if sorted_unis else "default"
                destination_key = (
                    f"{destination['key']}:{destination['chat_id']}:{universe_suffix}"
                )
                previous = self.telegram_state_store.symbols(
                    template_name,
                    destination_key,
                    allow_legacy=destination.get("key") == "group_1",
                    auto_reset_daily=bool(setup.get("telegram_auto_reset_daily", False)),
                )
                plan = build_telegram_alert_plan(
                    template_name,
                    rows,
                    previous,
                    str(setup.get("telegram_alert_mode", "new")),
                    universe_names=setup.get("universes", []),
                    custom_symbols_included=bool(
                        str(setup.get("custom_symbols", "")).strip()
                    ),
                    expression_tree=setup.get("expression"),
                )
                if not plan.messages:
                    self.telegram_state_store.save_symbols(
                        template_name,
                        plan.current_symbols,
                        destination_key,
                    )
                    unchanged_groups += 1
                    continue
                deliveries.append(
                    TelegramDelivery(
                        destination_key,
                        str(destination["label"]),
                        str(destination["chat_id"]),
                        plan.messages,
                    )
                )
                pending_state[destination_key] = (
                    template_name,
                    plan.current_symbols,
                )
        except (TelegramSettingsError, OSError, ValueError) as exc:
            self.telegram_status.setText(f"Telegram alert skipped: {exc}")
            self.telegram_status.setStyleSheet("color: #9c0006; font-size: 11px")
            return

        if not deliveries:
            self.telegram_status.setText(
                f"No Telegram list changes for {unchanged_groups} enabled group(s)."
            )
            self.telegram_status.setStyleSheet("color: #555; font-size: 11px")
            return

        self.telegram_pending_state = pending_state
        self.telegram_state_errors = []
        self.telegram_worker = TelegramSendWorker(token, deliveries)
        self.telegram_worker.deliveriesCompleted.connect(
            self._telegram_deliveries_completed
        )
        self.telegram_worker.succeeded.connect(self._telegram_alert_sent)
        self.telegram_worker.failed.connect(self._telegram_alert_failed)
        self.telegram_worker.finished.connect(self._telegram_worker_finished)
        self.telegram_status.setText(
            f"Sending Telegram alerts to {len(deliveries)} enabled group(s)..."
        )
        self.telegram_status.setStyleSheet("color: #445; font-size: 11px")
        self.telegram_worker.start()

    def _telegram_deliveries_completed(
        self, destination_keys: list[str], _message_count: int
    ) -> None:
        for destination_key in destination_keys:
            pending = self.telegram_pending_state.get(destination_key)
            if pending is None:
                continue
            template_name, symbols = pending
            try:
                self.telegram_state_store.save_symbols(
                    template_name, symbols, destination_key
                )
            except (TelegramSettingsError, OSError) as exc:
                self.telegram_state_errors.append(str(exc))

    def _telegram_alert_sent(self, message_count: int) -> None:
        if self.telegram_state_errors:
            self.telegram_status.setText(
                f"Telegram sent {message_count} message(s), but its alert baseline "
                f"could not be saved: {'; '.join(self.telegram_state_errors)}"
            )
            self.telegram_status.setStyleSheet("color: #9c0006; font-size: 11px")
            return
        self.telegram_status.setText(
            f"Telegram alert sent successfully ({message_count} message(s))."
        )
        self.telegram_status.setStyleSheet(
            "color: #006100; font-size: 11px; font-weight: 600"
        )

    def _telegram_alert_failed(self, details: str) -> None:
        state_details = (
            f" Baseline error: {'; '.join(self.telegram_state_errors)}"
            if self.telegram_state_errors
            else ""
        )
        self.telegram_status.setText(
            f"Telegram alert failed for one or more groups: {details}{state_details}"
        )
        self.telegram_status.setStyleSheet("color: #9c0006; font-size: 11px")

    def _telegram_worker_finished(self) -> None:
        self.telegram_worker = None
        self.telegram_pending_state = {}
        self.telegram_state_errors = []

    def _scan_failed(self, details: str) -> None:
        self._scan_cleanup()
        self.progress.setFormat("Failed")
        self.auto_run.setChecked(False)
        QMessageBox.critical(self, "Scan failed", details)
        if self._multi_scan_active:
            QTimer.singleShot(300, self._process_next_multi_scan_queue_item)

    def _scan_cancelled(self) -> None:
        self._scan_cleanup()
        self.progress.setFormat("Cancelled")
        self.counts.setText("Scan cancelled; previous results were retained.")
        self.auto_run.setChecked(False)

    def _scan_cleanup(self) -> None:
        self.cancel_button.setEnabled(False)
        self.scan_button.setEnabled(False)

    def _worker_thread_finished(self) -> None:
        self.worker = None
        self.scan_button.setEnabled(True)
        if not (self.history_worker and self.history_worker.isRunning()):
            self.history_run_button.setEnabled(True)
        self._schedule_auto_scan(immediate=False)

    def _start_history(self) -> None:
        if (
            self._guide_automation_worker
            and self._guide_automation_worker.isRunning()
        ):
            QMessageBox.information(
                self,
                "Strategy Guide automation running",
                "Wait for the current automated Strategy Guide cycle to finish.",
            )
            return
        if self.worker and self.worker.isRunning():
            QMessageBox.information(
                self,
                "Current scan running",
                "Cancel or finish the current scan before running backtest history.",
            )
            return
        setup = self._current_setup()
        setup["template_name"] = self.active_template_name or "(unsaved recovery)"
        setup["history_years"] = int(self.history_period.currentData())
        try:
            validate_ast(setup["expression"])
            validate_ranking_setup(setup)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid scan setup", str(exc))
            return
        if not setup["universes"] and not setup["custom_symbols"]:
            QMessageBox.warning(
                self,
                "No universe",
                "Choose at least one universe or enter custom symbols.",
            )
            return
        if expression_has_pnl_rules(
            setup.get("expression")
        ) or setup_has_portfolio_exit_triggers(setup):
            QMessageBox.warning(
                self,
                "Historical Position Data Required",
                "Portfolio P&L rules and exit triggers cannot be backtested from current holdings.\n\n"
                "Dated position and transaction history is required to avoid look-ahead bias.",
            )
            return
        self.auto_scan_timer.stop()
        self.result_tabs.setCurrentIndex(1)
        self.history_run_button.setEnabled(False)
        self.scan_button.setEnabled(False)
        self.history_cancel_button.setEnabled(True)
        self.history_export_button.setEnabled(False)
        self.history_progress.setValue(0)
        self.history_progress.setFormat("Resolving universe...")
        self.history_worker = HistoryScannerWorker(setup)
        self.history_worker.progress.connect(self._on_history_progress)
        self.history_worker.succeeded.connect(self._history_finished)
        self.history_worker.failed.connect(self._history_failed)
        self.history_worker.cancelled.connect(self._history_cancelled)
        self.history_worker.finished.connect(self._history_thread_finished)
        self.history_worker.start()

    def _cancel_history(self) -> None:
        if self.history_worker:
            self.history_worker.cancel()
            self.history_cancel_button.setEnabled(False)
            self.history_progress.setFormat(
                "Cancelling after the current request..."
            )

    def _on_history_progress(self, percent: int, text: str) -> None:
        self.history_progress.setValue(percent)
        self.history_progress.setFormat(text)

    def _history_finished(self, report: HistoryReport) -> None:
        completed_setup = copy.deepcopy(
            self.history_worker.setup
            if self.history_worker is not None
            else self._current_setup()
        )
        self.last_history_report = report
        self.last_history_setup = completed_setup
        self.history_chart.set_summary(report.daily_summary)
        sectors = sorted(
            {
                sector
                for item in report.daily_summary
                for sector in item.get("sectors", {})
            }
        )
        legend_parts = [
            (
                f'<span style="color:{HistoryChartWidget.sector_color(sector).name()};">'
                f"&#9632;</span> {html.escape(sector)}"
            )
            for sector in sectors
        ]
        self.history_legend.setText(" &nbsp; ".join(legend_parts))
        self.history_progress.setValue(100)
        self.history_progress.setFormat("History complete")
        self.history_counts.setText(
            f"{report.start_date} to {report.end_date} | universe {report.total_symbols:,} | "
            f"scanned {report.scanned_symbols:,} | match occurrences {report.total_matches:,} | "
            f"dates with matches {report.dates_with_matches:,} | cache hits {report.cache_hits:,} | "
            f"downloaded {report.downloaded_symbols:,} | missing {report.missing_symbols:,} | "
            f"download failures {report.download_failure_symbols:,} | "
            f"insufficient history {report.insufficient_symbols:,}"
        )
        self.history_cancel_button.setEnabled(False)
        self.history_export_button.setEnabled(True)
        current_setup = self._current_setup()
        current_setup["template_name"] = (
            self.active_template_name or "(unsaved recovery)"
        )
        current_setup["history_years"] = int(self.history_period.currentData())
        self._mark_history_stale(current_setup != completed_setup)
        matching_dates = sorted(report.rows_by_date)
        if matching_dates:
            self._show_history_date(matching_dates[-1])
            QTimer.singleShot(
                0,
                lambda: self.history_scroll.horizontalScrollBar().setValue(
                    self.history_scroll.horizontalScrollBar().maximum()
                ),
            )
        else:
            self.history_selected_label.setText(
                "No stocks matched on any historical date."
            )
            self._populate_table(self.history_results, [], completed_setup)
        if report.universe_errors:
            QMessageBox.warning(
                self, "Some universes failed", "\n".join(report.universe_errors)
            )

    def _history_failed(self, details: str) -> None:
        self.history_cancel_button.setEnabled(False)
        self.history_progress.setFormat("History failed")
        QMessageBox.critical(self, "History scan failed", details)

    def _history_cancelled(self) -> None:
        self.history_cancel_button.setEnabled(False)
        self.history_progress.setFormat("History cancelled")
        self.history_counts.setText(
            "History scan cancelled; previous history results were retained."
        )

    def _history_thread_finished(self) -> None:
        self.history_worker = None
        self.history_run_button.setEnabled(True)
        if not (self.worker and self.worker.isRunning()):
            self.scan_button.setEnabled(True)
        self._schedule_auto_scan(immediate=False)

    def _show_history_date(self, selected_date: str) -> None:
        if self.last_history_report is None or self.last_history_setup is None:
            return
        rows = self.last_history_report.rows_by_date.get(selected_date, [])
        self.history_chart.select_date(selected_date)
        self.history_selected_label.setText(
            f"{selected_date}: {len(rows):,} matching stocks"
        )
        self._populate_table(self.history_results, rows, self.last_history_setup)

    def _populate_results(
        self, rows: list[dict[str, Any]], setup: dict[str, Any] | None = None
    ) -> None:
        self._populate_table(self.results, rows, setup)

    def _populate_table(
        self,
        table: QTableWidget,
        rows: list[dict[str, Any]],
        setup: dict[str, Any] | None = None,
    ) -> None:
        table.setSortingEnabled(False)
        effective_setup = setup or self._current_setup()
        is_history_table = table is self.history_results
        account_name, enctoken = (
            ("", "") if is_history_table else self._order_credentials(effective_setup)
        )
        headers = selected_result_headers(effective_setup)
        dynamic_columns = [
            *rule_value_columns(effective_setup["expression"]),
            *ranking_result_columns(effective_setup),
        ]
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setRowCount(len(rows))
        numeric = {
            SERIAL_COLUMN,
            "Open",
            "High",
            "Low",
            "Close",
            PERCENT_CHANGE_COLUMN,
            "Volume",
            *dynamic_columns,
        }
        for row_index, row in enumerate(rows):
            for column, header in enumerate(headers):
                if header == ACTION_COLUMN:
                    if is_history_table:
                        table.setItem(row_index, column, QTableWidgetItem("Historical result"))
                        continue
                    symbol = str(row.get("Symbol", "")).strip()
                    try:
                        close_price = float(str(row.get("Close", 0)).replace(",", ""))
                    except (ValueError, TypeError):
                        close_price = 0.0

                    action_widget = QWidget()
                    action_layout = QHBoxLayout(action_widget)
                    action_layout.setContentsMargins(2, 2, 2, 2)
                    action_layout.setSpacing(4)

                    buy_btn = QPushButton("🛒 Buy")
                    buy_btn.setStyleSheet(
                        "font-weight: 700; padding: 2px 8px; background: #27ae60; color: white; border-radius: 3px;"
                    )
                    buy_btn.setToolTip(f"Place Buy Order for {symbol}")
                    buy_btn.clicked.connect(
                        lambda _checked, s=symbol, p=close_price: PlaceOrderDialog(
                            s,
                            close_price=p,
                            action="BUY",
                            enctoken=enctoken,
                            account_name=account_name,
                            parent=self,
                        ).exec()
                    )

                    sell_btn = QPushButton("🔴 Sell")
                    sell_btn.setStyleSheet(
                        "font-weight: 700; padding: 2px 8px; background: #c0392b; color: white; border-radius: 3px;"
                    )
                    sell_btn.setToolTip(f"Place Sell Order for {symbol}")
                    sell_btn.clicked.connect(
                        lambda _checked, s=symbol, p=close_price: PlaceOrderDialog(
                            s,
                            close_price=p,
                            action="SELL",
                            enctoken=enctoken,
                            account_name=account_name,
                            parent=self,
                        ).exec()
                    )

                    action_layout.addWidget(buy_btn)
                    action_layout.addWidget(sell_btn)
                    table.setCellWidget(row_index, column, action_widget)
                    table.setItem(row_index, column, QTableWidgetItem(""))
                    continue

                value = row_index + 1 if header == SERIAL_COLUMN else row.get(header)
                if header in numeric:
                    text = (
                        format_percent(value)
                        if header == PERCENT_CHANGE_COLUMN
                        else format_number(value)
                    )
                    item = NumericItem(text)
                    item.setData(Qt.ItemDataRole.UserRole, float(value) if value is not None else -math.inf)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                else:
                    item = QTableWidgetItem(str(value or ""))
                table.setItem(row_index, column, item)
        table.resizeColumnsToContents()
        table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        table.horizontalHeader().setSectionsMovable(True)
        table.horizontalHeader().setToolTip(
            "Drag a column border to resize it on the fly, or drag a heading left/right to rearrange columns."
        )
        self._apply_visual_header_order(table, headers)
        if "Evidence" in headers:
            ev_idx = headers.index("Evidence")
            current_w = table.columnWidth(ev_idx)
            table.setColumnWidth(ev_idx, max(280, current_w))
        table.setSortingEnabled(True)
        if (
            bool(effective_setup.get("ranking_enabled", False))
            and COMPOSITE_RSI_MOMENTUM_COLUMN in headers
        ):
            order = (
                Qt.SortOrder.DescendingOrder
                if str(effective_setup.get("ranking_direction", "DESC")).upper()
                != "ASC"
                else Qt.SortOrder.AscendingOrder
            )
            table.sortItems(headers.index(COMPOSITE_RSI_MOMENTUM_COLUMN), order)

    def _export(self) -> None:
        if self.last_scan_setup is None:
            return
        default_name = APP_DIR / f"scanner_results_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        path, _ = QFileDialog.getSaveFileName(self, "Export scan", str(default_name), "Excel Workbook (*.xlsx)")
        if path:
            if not path.lower().endswith(".xlsx"):
                path += ".xlsx"
            try:
                export_workbook(path, self.last_rows, self.last_scan_setup)
                QMessageBox.information(self, "Export complete", f"Saved {len(self.last_rows)} rows to:\n{path}")
            except Exception as exc:
                QMessageBox.critical(self, "Export failed", str(exc))

    def _export_history(self) -> None:
        if self.last_history_report is None or self.last_history_setup is None:
            return
        default_name = APP_DIR / (
            f"scanner_history_{int(self.last_history_setup.get('history_years', 2))}y_"
            f"{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        )
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export backtest history",
            str(default_name),
            "Excel Workbook (*.xlsx)",
        )
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        try:
            export_history_workbook(
                path, self.last_history_report, self.last_history_setup
            )
            QMessageBox.information(
                self,
                "History export complete",
                f"Saved {self.last_history_report.total_matches:,} historical match "
                f"occurrences to:\n{path}",
            )
        except Exception as exc:
            QMessageBox.critical(self, "History export failed", str(exc))

    def closeEvent(self, event: Any) -> None:
        if self._dirty or self.english_rule.text().strip():
            template = self.active_template_name or "the current setup"
            if QMessageBox.question(
                self,
                "Unsaved template changes",
                f'Changes to "{template}" have not been saved. Exit without saving?',
            ) != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._guide_automation_active = False
        self._guide_automation_timer.stop()
        automation_worker = getattr(self, "_guide_automation_worker", None)
        if automation_worker is not None and automation_worker.isRunning():
            automation_worker.cancel()
            if not automation_worker.wait(5000):
                QMessageBox.warning(
                    self,
                    "Automation still stopping",
                    "The automated Strategy Guide is still finishing a data request. "
                    "Please wait and close again.",
                )
                event.ignore()
                return
        guide_worker = getattr(self, "paper_pipeline_worker", None)
        if guide_worker is not None and guide_worker.isRunning():
            guide_worker.cancel()
            if not guide_worker.wait(5000):
                QMessageBox.warning(
                    self,
                    "Strategy Guide still stopping",
                    "The Strategy Guide is still finishing a data request. "
                    "Please wait and close again.",
                )
                event.ignore()
                return
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            if not self.worker.wait(5000):
                QMessageBox.warning(
                    self,
                    "Scan still stopping",
                    "A network request is still finishing. Please wait and close again.",
                )
                event.ignore()
                return
        if self.history_worker and self.history_worker.isRunning():
            self.history_worker.cancel()
            if not self.history_worker.wait(5000):
                QMessageBox.warning(
                    self,
                    "History scan still stopping",
                    "A network request is still finishing. Please wait and close again.",
                )
                event.ignore()
                return
        if self.telegram_worker and self.telegram_worker.isRunning():
            import time
            start_time = time.time()
            while self.telegram_worker.isRunning() and time.time() - start_time < 5.0:
                QApplication.processEvents()
                time.sleep(0.01)
            if self.telegram_worker.isRunning():
                QMessageBox.warning(
                    self,
                    "Telegram alert still sending",
                    "The Telegram request is still finishing. Please wait and close again.",
                )
                event.ignore()
                return
        auxiliary_workers = [
            worker
            for worker in (
                getattr(self, "_auto_holdings_worker", None),
                getattr(self, "_margins_fetcher", None),
                getattr(self, "_nse_calendar_worker", None),
                *_BACKGROUND_WORKERS,
            )
            if worker is not None and worker.isRunning()
        ]
        if auxiliary_workers:
            QMessageBox.warning(
                self,
                "Background request still running",
                "A holdings, funds, or NSE calendar request is still finishing. "
                "Please wait and close again.",
            )
            event.ignore()
            return
        if self._strategy_guide_window is not None:
            self._strategy_guide_window.close()
        self.auto_scan_timer.stop()
        self._holdings_auto_sync_timer.stop()
        self._nse_calendar_refresh_timer.stop()
        self._multi_scan_timer.stop()
        self._guide_automation_timer.stop()
        try:
            current_settings = self.app_settings.load()
            self.app_settings.save(
                current_settings.get(
                    "data_refresh_minutes", DEFAULT_REFRESH_MINUTES
                ),
                current_settings.get("force_fresh", True),
                self.main_splitter.sizes(),
            )
        except (OSError, StateStoreError, AttributeError):
            # A layout preference must never prevent the application from closing.
            pass
        event.accept()


def choose_startup_price_mode() -> bool:
    """Ask which latest-price path this process should use."""
    box = QMessageBox()
    box.setWindowTitle("Choose Latest Price Data Approach")
    box.setIcon(QMessageBox.Icon.Question)
    box.setText(
        "<b>How should latest/intraday scans obtain today's prices?</b>"
    )
    box.setInformativeText(
        "TICKERTAPE batch prices first catch up any missing recent official "
        "Bhavcopy sessions, then obtain today's prices in batches. Missing, stale "
        "or invalid symbols automatically use the original downloader. The "
        "Original price approach preserves the existing behaviour. Historical-as-of "
        "scans and backtests are unchanged in either mode."
    )
    tickertape_button = box.addButton(
        "Use TICKERTAPE Batch Prices",
        QMessageBox.ButtonRole.AcceptRole,
    )
    original_button = box.addButton(
        "Use Original Price Approach",
        QMessageBox.ButtonRole.ActionRole,
    )
    exit_button = box.addButton(
        "Exit Program",
        QMessageBox.ButtonRole.RejectRole,
    )
    box.setDefaultButton(original_button)
    box.exec()
    clicked = box.clickedButton()
    if clicked is tickertape_button:
        set_session_price_mode(TICKERTAPE_PRICE_MODE)
        return True
    if clicked is original_button:
        set_session_price_mode(ORIGINAL_PRICE_MODE)
        return True
    if clicked is exit_button or clicked is None:
        return False
    return False


def main() -> int:
    if "--portable-smoke-test" in sys.argv:
        # Importing this module has already loaded PySide6 and its native DLLs.
        # Constructing QApplication additionally proves the bundled Windows
        # platform plugin can initialise without using a machine-wide Qt/Python.
        import shiboken6  # noqa: F401

        smoke_app = QApplication.instance() or QApplication([])
        required = (
            APP_DIR / "bhavcopy_history.db",
            APP_DIR / "scanner_templates.json",
            APP_DIR / "seed_data",
        )
        if any(not path.exists() for path in required):
            return 3
        smoke_app.quit()
        return 0
    migration_error = ""
    reconcile_report: list[str] = []
    try:
        legacy_dirs: list[Path] = []
        if not getattr(sys, "frozen", False):
            legacy_dirs = [_legacy_user_data_dir()]
        reconcile_report = reconcile_state_files(DATA_DIR, legacy_dirs)
        migrate_legacy_broker_secrets()
    except Exception as exc:
        migration_error = str(exc)
    for line in reconcile_report:
        print(f"[ChartSage] {line}")
    print(f"[ChartSage] Data folder: {DATA_DIR}")
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    if not choose_startup_price_mode():
        return 0
    maintenance_report = run_startup_maintenance_dialog()
    window = ScannerWindow()
    window.setWindowTitle(
        f"{window.windowTitle()} - {session_price_mode_label()}"
    )
    window.showMaximized()
    QTimer.singleShot(
        0,
        lambda: show_startup_maintenance_summary(window, maintenance_report),
    )
    if migration_error:
        QTimer.singleShot(
            0,
            lambda: QMessageBox.warning(
                window,
                "Data folder check failed",
                "The app could not finish reconciling its data folder:\n"
                + migration_error,
            ),
        )
    elif reconcile_report:
        QTimer.singleShot(
            0,
            lambda: QMessageBox.information(
                window,
                "Data folder reconciled",
                "The app keeps all of its files in its own folder. Startup changes:\n\n"
                + "\n".join(reconcile_report),
            ),
        )
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
