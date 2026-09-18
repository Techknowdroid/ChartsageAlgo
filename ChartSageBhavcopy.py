"""ChartSage official NSE cash-market bhavcopy feeder.

This program is intentionally independent of ChartSageAlgoOPENcode.py.  It
downloads, validates and archives official NSE daily bhavcopy ZIP files and
builds a read-optimised SQLite database consumed by the main scanner.

Run without arguments for the desktop GUI, or use --headless for automation.
Only raw exchange prices are stored; adjusted histories must be derived by a
consumer without overwriting these immutable official values.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import queue
import re
import shutil
import sqlite3
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from membership_update import (
    membership_coverage_summary,
    update_historical_membership,
)


# In a PyInstaller onedir build ``__file__`` resolves inside ``_internal``.
# Runtime data must instead live beside the executable so the complete suite
# remains portable when its folder is copied or installed anywhere writable.
APP_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
DATABASE_PATH = APP_DIR / "bhavcopy_history.db"
ARCHIVE_DIR = APP_DIR / "nse_bhavcopy_archive"
SNAPSHOT_DIR = APP_DIR / "bhavcopy_snapshots"
STATE_PATH = APP_DIR / "bhavcopy_state.json"
SCHEMA_VERSION = "1"
# Keep four full calendar years ahead of the user's 2018 scan horizon.  This
# supplies the warm-up requested by long daily indicators such as SMA(200),
# while the visible scan results can still begin on 2018-01-01.
EARLIEST_SUPPORTED_DATE = date(2014, 1, 1)
DEFAULT_START_DATE = EARLIEST_SUPPORTED_DATE
UDIFF_START_DATE = date(2024, 7, 8)
SUPPORTED_SERIES = {"EQ", "BE", "BZ"}
IST = timezone(timedelta(hours=5, minutes=30))

LEGACY_URL = (
    "https://nsearchives.nseindia.com/content/historical/EQUITIES/"
    "{year}/{month}/cm{day}{month}{year}bhav.csv.zip"
)
UDIFF_URL = (
    "https://nsearchives.nseindia.com/content/cm/"
    "BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip"
)
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/124 Safari/537.36"
    ),
    "Accept": "application/zip,application/octet-stream,*/*",
    "Referer": "https://www.nseindia.com/all-reports",
}


class FeederError(RuntimeError):
    pass


class DownloadStopped(FeederError):
    pass


@dataclass(frozen=True)
class PriceRow:
    trade_date: str
    symbol: str
    series: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class RunSummary:
    considered: int = 0
    downloaded: int = 0
    already_present: int = 0
    unavailable: int = 0
    failed: int = 0
    rows: int = 0


def iso_now() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def default_end_date() -> date:
    now = datetime.now(IST)
    candidate = now.date() if now.time() >= clock_time(18, 30) else now.date() - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def iter_weekdays(start: date, end: date) -> Iterable[date]:
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            yield cursor
        cursor += timedelta(days=1)


def source_for_date(trade_date: date) -> tuple[str, str]:
    if trade_date >= UDIFF_START_DATE:
        filename = f"BhavCopy_NSE_CM_0_0_0_{trade_date:%Y%m%d}_F_0000.csv.zip"
        return "udiff", UDIFF_URL.format(yyyymmdd=trade_date.strftime("%Y%m%d"))
    month = trade_date.strftime("%b").upper()
    filename = f"cm{trade_date:%d}{month}{trade_date:%Y}bhav.csv.zip"
    return "legacy", LEGACY_URL.format(
        year=trade_date.strftime("%Y"),
        month=month,
        day=trade_date.strftime("%d"),
    )


def archive_path(trade_date: date, fmt: str) -> Path:
    if fmt == "udiff":
        name = f"BhavCopy_NSE_CM_0_0_0_{trade_date:%Y%m%d}_F_0000.csv.zip"
    else:
        month = trade_date.strftime("%b").upper()
        name = f"cm{trade_date:%d}{month}{trade_date:%Y}bhav.csv.zip"
    return ARCHIVE_DIR / f"{trade_date:%Y}" / f"{trade_date:%m}" / name


def parse_number(value: object, field: str) -> float:
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError) as exc:
        raise FeederError(f"Invalid {field}: {value!r}") from exc
    if number < 0:
        raise FeederError(f"Negative {field}: {value!r}")
    return number


def normalise_header(value: object) -> str:
    return str(value or "").replace("\ufeff", "").strip()


def parse_bhavcopy(content: bytes, trade_date: date, fmt: str) -> list[PriceRow]:
    try:
        with ZipFile(io.BytesIO(content)) as archive:
            names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if not names:
                raise FeederError("ZIP contains no CSV file")
            raw_csv = archive.read(names[0])
    except (BadZipFile, KeyError) as exc:
        raise FeederError(f"Invalid bhavcopy ZIP: {exc}") from exc

    text = raw_csv.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise FeederError("Bhavcopy CSV has no header")
    reader.fieldnames = [normalise_header(name) for name in reader.fieldnames]

    if fmt == "udiff":
        mapping = {
            "symbol": "TckrSymb",
            "series": "SctySrs",
            "open": "OpnPric",
            "high": "HghPric",
            "low": "LwPric",
            "close": "ClsPric",
            "volume": "TtlTradgVol",
            "date": "TradDt",
        }
    else:
        mapping = {
            "symbol": "SYMBOL",
            "series": "SERIES",
            "open": "OPEN",
            "high": "HIGH",
            "low": "LOW",
            "close": "CLOSE",
            "volume": "TOTTRDQTY",
            "date": "TIMESTAMP",
        }
    missing = [column for column in mapping.values() if column not in reader.fieldnames]
    if missing:
        raise FeederError(f"Unexpected {fmt} schema; missing {', '.join(missing)}")

    parsed: list[PriceRow] = []
    seen: set[tuple[str, str]] = set()
    for record in reader:
        series = str(record.get(mapping["series"], "")).strip().upper()
        if series not in SUPPORTED_SERIES:
            continue
        symbol = str(record.get(mapping["symbol"], "")).strip().upper()
        if not symbol:
            continue
        raw_date = str(record.get(mapping["date"], "")).strip()
        parsed_date: date | None = None
        for date_format in ("%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y"):
            try:
                parsed_date = datetime.strptime(raw_date.title(), date_format).date()
                break
            except ValueError:
                continue
        if parsed_date != trade_date:
            raise FeederError(
                f"Row date {raw_date!r} does not match requested {trade_date}"
            )
        key = (symbol, series)
        if key in seen:
            raise FeederError(f"Duplicate {symbol}/{series} row")
        seen.add(key)
        open_price = parse_number(record.get(mapping["open"]), "open")
        high = parse_number(record.get(mapping["high"]), "high")
        low = parse_number(record.get(mapping["low"]), "low")
        close_price = parse_number(record.get(mapping["close"]), "close")
        volume = parse_number(record.get(mapping["volume"]), "volume")
        if min(open_price, high, low, close_price) <= 0:
            raise FeederError(f"Non-positive OHLC for {symbol}/{series}")
        if high < max(open_price, low, close_price) or low > min(open_price, high, close_price):
            raise FeederError(f"Invalid OHLC relationship for {symbol}/{series}")
        parsed.append(
            PriceRow(
                trade_date=trade_date.isoformat(),
                symbol=symbol,
                series=series,
                open=open_price,
                high=high,
                low=low,
                close=close_price,
                volume=volume,
            )
        )
    if not parsed:
        raise FeederError("Bhavcopy contains no supported EQ/BE/BZ rows")
    return parsed


def parse_nse_date(value: object) -> date | None:
    text = str(value or "").strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text.title(), fmt).date()
        except ValueError:
            continue
    return None


def technical_action_factor(subject: str) -> tuple[float | None, str, str]:
    """Parse only deterministic bonus/split/consolidation factors."""
    text = " ".join(str(subject or "").split())
    factor = 1.0
    action_types: list[str] = []
    supported_keyword = False
    non_equity_bonus = "ncrps" in text.lower() or "debenture" in text.lower()
    bonus = (
        None
        if non_equity_bonus
        else re.search(
            r"\bbonus\s*[-–]?\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)",
            text,
            re.I,
        )
    )
    if "bonus" in text.lower():
        supported_keyword = True
        if bonus:
            numerator, denominator = map(float, bonus.groups())
            if numerator > 0 and denominator > 0:
                factor *= (numerator + denominator) / denominator
                action_types.append("bonus")
    lowered = text.lower()
    is_split = "split" in lowered or "sub-division" in lowered or "sub division" in lowered
    is_consolidation = "consolidation" in lowered
    if is_split or is_consolidation:
        supported_keyword = True
        values = re.search(
            r"\bfrom\s+(?:rs\.?|re\.?)?\s*(\d+(?:\.\d+)?)\s*/?-?\s*"
            r"(?:per\s+share\s+)?\bto\s+(?:rs\.?|re\.?)?\s*(\d+(?:\.\d+)?)",
            text,
            re.I,
        )
        if values:
            old_value, new_value = map(float, values.groups())
            if old_value > 0 and new_value > 0:
                factor *= old_value / new_value
                action_types.append("consolidation" if is_consolidation else "split")
    if action_types and math.isfinite(factor) and factor > 0:
        return factor, "+".join(action_types), "supported"
    return None, "", "ambiguous" if supported_keyword else "ignored"


class BhavcopyDatabase:
    def __init__(self, path: Path = DATABASE_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=60)
        conn.execute("PRAGMA journal_mode=WAL").fetchone()
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=60000")
        return conn

    def migrate(self) -> None:
        with closing(self.connect()) as conn, conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS metadata (
                       key TEXT PRIMARY KEY, value TEXT NOT NULL)"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS bhavcopy_files (
                       trade_date TEXT PRIMARY KEY,
                       format TEXT NOT NULL,
                       filename TEXT,
                       url TEXT,
                       sha256 TEXT,
                       byte_size INTEGER,
                       row_count INTEGER,
                       downloaded_at TEXT,
                       checked_at TEXT NOT NULL,
                       status TEXT NOT NULL,
                       error TEXT)"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS official_prices (
                       trade_date TEXT NOT NULL,
                       symbol TEXT NOT NULL,
                       series TEXT NOT NULL,
                       open REAL NOT NULL,
                       high REAL NOT NULL,
                       low REAL NOT NULL,
                       close REAL NOT NULL,
                       volume REAL NOT NULL,
                       source_file TEXT NOT NULL,
                       PRIMARY KEY (trade_date, symbol, series),
                       FOREIGN KEY (trade_date) REFERENCES bhavcopy_files(trade_date))"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_official_prices_symbol_date "
                "ON official_prices(symbol, trade_date)"
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS corporate_actions (
                       symbol TEXT NOT NULL, series TEXT NOT NULL,
                       ex_date TEXT NOT NULL, subject TEXT NOT NULL,
                       company TEXT, isin TEXT, face_value REAL,
                       record_date TEXT, action_types TEXT,
                       adjustment_factor REAL, parse_status TEXT NOT NULL,
                       fetched_at TEXT NOT NULL,
                       PRIMARY KEY(symbol,series,ex_date,subject))"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_corporate_actions_symbol_date "
                "ON corporate_actions(symbol,ex_date)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version',?)",
                (SCHEMA_VERSION,),
            )

    def successful(self, trade_date: date) -> bool:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT filename FROM bhavcopy_files WHERE trade_date=? AND status='ok'",
                (trade_date.isoformat(),),
            ).fetchone()
        return bool(row and row[0] and (APP_DIR / str(row[0])).is_file())

    def status(self, trade_date: date) -> str | None:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT status FROM bhavcopy_files WHERE trade_date=?",
                (trade_date.isoformat(),),
            ).fetchone()
        return str(row[0]) if row and row[0] else None

    def record_failure(self, trade_date: date, fmt: str, url: str, status: str, error: str) -> None:
        with closing(self.connect()) as conn, conn:
            conn.execute(
                """INSERT INTO bhavcopy_files
                   (trade_date,format,url,checked_at,status,error)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(trade_date) DO UPDATE SET
                     format=excluded.format,url=excluded.url,checked_at=excluded.checked_at,
                     status=excluded.status,error=excluded.error""",
                (trade_date.isoformat(), fmt, url, iso_now(), status, error[:1000]),
            )

    def store_success(
        self,
        trade_date: date,
        fmt: str,
        url: str,
        target: Path,
        content: bytes,
        rows: list[PriceRow],
    ) -> None:
        digest = hashlib.sha256(content).hexdigest()
        now = iso_now()
        with closing(self.connect()) as conn, conn:
            conn.execute("DELETE FROM official_prices WHERE trade_date=?", (trade_date.isoformat(),))
            conn.execute(
                """INSERT INTO bhavcopy_files
                   (trade_date,format,filename,url,sha256,byte_size,row_count,
                    downloaded_at,checked_at,status,error)
                   VALUES (?,?,?,?,?,?,?,?,?,'ok',NULL)
                   ON CONFLICT(trade_date) DO UPDATE SET
                     format=excluded.format,filename=excluded.filename,url=excluded.url,
                     sha256=excluded.sha256,byte_size=excluded.byte_size,
                     row_count=excluded.row_count,downloaded_at=excluded.downloaded_at,
                     checked_at=excluded.checked_at,status='ok',error=NULL""",
                (
                    trade_date.isoformat(), fmt, str(target.relative_to(APP_DIR)), url,
                    digest, len(content), len(rows), now, now,
                ),
            )
            conn.executemany(
                """INSERT INTO official_prices
                   (trade_date,symbol,series,open,high,low,close,volume,source_file)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        row.trade_date, row.symbol, row.series, row.open, row.high,
                        row.low, row.close, row.volume, str(target.relative_to(APP_DIR)),
                    )
                    for row in rows
                ],
            )

    def stats(self) -> dict[str, object]:
        with closing(self.connect()) as conn:
            ok, first_date, last_date = conn.execute(
                "SELECT COUNT(*),MIN(trade_date),MAX(trade_date) "
                "FROM bhavcopy_files WHERE status='ok'"
            ).fetchone()
            rows = conn.execute("SELECT COUNT(*) FROM official_prices").fetchone()[0]
            failures = conn.execute(
                "SELECT COUNT(*) FROM bhavcopy_files WHERE status!='ok'"
            ).fetchone()[0]
        return {
            "files": int(ok or 0), "rows": int(rows or 0),
            "first_date": first_date, "last_date": last_date,
            "unavailable_or_failed": int(failures or 0),
        }

    def create_shareable_snapshot(self, output_dir: Path | None = None) -> Path:
        """Create a consistent compressed database snapshot using SQLite backup.

        The live database can remain open in another process: SQLite's online
        backup API captures one transactionally consistent point in time. Raw
        exchange ZIP files are deliberately excluded from this portable copy.
        """
        output_dir = Path(output_dir or SNAPSHOT_DIR).resolve()
        app_root = APP_DIR.resolve()
        try:
            output_dir.relative_to(app_root)
        except ValueError as exc:
            raise FeederError("Snapshots must be created inside the ChartSage folder") from exc
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
        target = output_dir / f"ChartSageBhavcopySnapshot_{stamp}.zip"
        suffix = 1
        while target.exists():
            target = output_dir / f"ChartSageBhavcopySnapshot_{stamp}_{suffix}.zip"
            suffix += 1

        with tempfile.TemporaryDirectory(prefix="chartsage_snapshot_") as temporary_name:
            temporary_dir = Path(temporary_name)
            snapshot_db = temporary_dir / "bhavcopy_history.db"
            with closing(self.connect()) as source, closing(
                sqlite3.connect(snapshot_db, timeout=60)
            ) as destination:
                source.backup(destination, pages=2048)
                destination.commit()
                integrity = destination.execute("PRAGMA integrity_check").fetchone()
                if not integrity or str(integrity[0]).lower() != "ok":
                    raise FeederError(
                        f"Snapshot database integrity check failed: {integrity}"
                    )
                file_stats = destination.execute(
                    "SELECT COUNT(*),MIN(trade_date),MAX(trade_date) "
                    "FROM bhavcopy_files WHERE status='ok'"
                ).fetchone()
                snapshot_stats = {
                    "files": int(file_stats[0] or 0),
                    "first_date": file_stats[1],
                    "last_date": file_stats[2],
                    "rows": int(
                        destination.execute(
                            "SELECT COUNT(*) FROM official_prices"
                        ).fetchone()[0]
                        or 0
                    ),
                    "supported_actions": int(
                        destination.execute(
                            "SELECT COUNT(*) FROM corporate_actions "
                            "WHERE parse_status='supported'"
                        ).fetchone()[0]
                        or 0
                    ),
                    "ambiguous_actions": int(
                        destination.execute(
                            "SELECT COUNT(*) FROM corporate_actions "
                            "WHERE parse_status='ambiguous'"
                        ).fetchone()[0]
                        or 0
                    ),
                }
            database_bytes = snapshot_db.read_bytes()
            database_hash = hashlib.sha256(database_bytes).hexdigest()
            manifest = {
                "snapshot_format": 1,
                "created_at_ist": iso_now(),
                "database_filename": "bhavcopy_history.db",
                "database_sha256": database_hash,
                "database_bytes": len(database_bytes),
                "schema_version": SCHEMA_VERSION,
                "official_file_count": snapshot_stats["files"],
                "official_price_rows": snapshot_stats["rows"],
                "first_trade_date": snapshot_stats["first_date"],
                "last_trade_date": snapshot_stats["last_date"],
                "supported_corporate_actions": snapshot_stats["supported_actions"],
                "ambiguous_corporate_actions": snapshot_stats["ambiguous_actions"],
                "raw_nse_zip_archive_included": False,
            }
            readme = (
                "ChartSage Bhavcopy Database Snapshot\n"
                "====================================\n\n"
                "This package contains a transactionally consistent copy of "
                "bhavcopy_history.db. It does not contain the original NSE ZIP archive.\n\n"
                "Installation: close ChartSage and its feeder, extract "
                "bhavcopy_history.db beside ChartSageBhavcopy.py and "
                "ChartSageAlgoOPENcode.py, then start ChartSage normally.\n\n"
                "Verify the database SHA-256 against snapshot_manifest.json before use.\n"
                "The provider must ensure that sharing and use comply with applicable "
                "NSE data licensing and redistribution terms.\n"
            )
            temporary_zip = target.with_suffix(target.suffix + ".part")
            try:
                with ZipFile(temporary_zip, "w", ZIP_DEFLATED, compresslevel=6) as archive:
                    archive.write(snapshot_db, "bhavcopy_history.db")
                    archive.writestr(
                        "snapshot_manifest.json",
                        json.dumps(manifest, indent=2) + "\n",
                    )
                    archive.writestr("README.txt", readme)
                temporary_zip.replace(target)
            finally:
                temporary_zip.unlink(missing_ok=True)
        return target

    @staticmethod
    def _archive_identity(path: Path) -> tuple[date, str] | None:
        name = path.name
        udiff = re.fullmatch(
            r"BhavCopy_NSE_CM_0_0_0_(\d{8})_F_0000\.csv\.zip",
            name,
            flags=re.IGNORECASE,
        )
        if udiff:
            return datetime.strptime(udiff.group(1), "%Y%m%d").date(), "udiff"
        legacy = re.fullmatch(
            r"cm(\d{2}[A-Za-z]{3}\d{4})bhav\.csv\.zip",
            name,
            flags=re.IGNORECASE,
        )
        if legacy:
            return (
                datetime.strptime(legacy.group(1).title(), "%d%b%Y").date(),
                "legacy",
            )
        return None

    def rebuild_from_archive(
        self,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> tuple[int, int, Path]:
        """Build a fresh database from retained ZIPs and atomically replace live DB."""
        progress = progress or (lambda _done, _total, _message: None)
        archive_root = ARCHIVE_DIR.resolve()
        if not archive_root.is_dir():
            raise FeederError("The NSE bhavcopy archive folder does not exist")
        dated_files: dict[date, tuple[Path, str]] = {}
        for path in sorted(archive_root.rglob("*.zip")):
            identity = self._archive_identity(path)
            if identity is None:
                continue
            trade_date, fmt = identity
            if trade_date in dated_files:
                raise FeederError(f"Duplicate archived bhavcopy date: {trade_date}")
            dated_files[trade_date] = (path, fmt)
        if not dated_files:
            raise FeederError("No recognised bhavcopy ZIP files were found")

        safety_snapshot = self.create_shareable_snapshot()
        with closing(self.connect()) as conn:
            action_rows = conn.execute(
                """SELECT symbol,series,ex_date,subject,company,isin,face_value,
                          record_date,action_types,adjustment_factor,parse_status,fetched_at
                   FROM corporate_actions"""
            ).fetchall()
        with tempfile.TemporaryDirectory(prefix="chartsage_rebuild_") as temporary_name:
            rebuilt_path = Path(temporary_name) / "bhavcopy_history.db"
            rebuilt = BhavcopyDatabase(rebuilt_path)
            row_total = 0
            ordered = sorted(dated_files.items())
            for index, (trade_date, (path, fmt)) in enumerate(ordered, 1):
                content = path.read_bytes()
                rows = parse_bhavcopy(content, trade_date, fmt)
                _actual_fmt, url = source_for_date(trade_date)
                rebuilt.store_success(trade_date, fmt, url, path, content, rows)
                row_total += len(rows)
                progress(
                    index,
                    len(ordered),
                    f"Rebuilt {trade_date}: {len(rows):,} rows",
                )
            if action_rows:
                with closing(rebuilt.connect()) as conn, conn:
                    conn.executemany(
                        """INSERT OR REPLACE INTO corporate_actions
                           (symbol,series,ex_date,subject,company,isin,face_value,
                            record_date,action_types,adjustment_factor,parse_status,fetched_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        action_rows,
                    )
            with closing(sqlite3.connect(rebuilt_path, timeout=60)) as conn:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()
                if not integrity or str(integrity[0]).lower() != "ok":
                    raise FeederError(f"Rebuilt database failed integrity check: {integrity}")
            try:
                with closing(
                    sqlite3.connect(rebuilt_path, timeout=60)
                ) as rebuilt_connection, closing(self.connect()) as live_connection:
                    rebuilt_connection.backup(live_connection, pages=2048)
                    live_connection.commit()
            except sqlite3.Error as exc:
                raise FeederError(
                    "Could not update the database. Close ChartSage and try again."
                ) from exc
        self.migrate()
        return len(dated_files), row_total, safety_snapshot

    def reset_unsuccessful_dates(self) -> int:
        """Forget failed/404 checks so the next backfill retries those dates."""
        with closing(self.connect()) as conn, conn:
            dates = [
                str(row[0])
                for row in conn.execute(
                    "SELECT trade_date FROM bhavcopy_files WHERE status!='ok'"
                ).fetchall()
            ]
            if dates:
                conn.executemany(
                    "DELETE FROM official_prices WHERE trade_date=?",
                    [(item,) for item in dates],
                )
                conn.executemany(
                    "DELETE FROM bhavcopy_files WHERE trade_date=?",
                    [(item,) for item in dates],
                )
        return len(dates)

    def store_corporate_actions(self, actions: list[dict[str, object]]) -> int:
        with closing(self.connect()) as conn, conn:
            conn.executemany(
                """INSERT OR REPLACE INTO corporate_actions
                   (symbol,series,ex_date,subject,company,isin,face_value,
                    record_date,action_types,adjustment_factor,parse_status,fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        item["symbol"], item["series"], item["ex_date"],
                        item["subject"], item.get("company"), item.get("isin"),
                        item.get("face_value"), item.get("record_date"),
                        item.get("action_types"), item.get("adjustment_factor"),
                        item["parse_status"], iso_now(),
                    )
                    for item in actions
                ],
            )
        return len(actions)

    def corporate_action_stats(self) -> dict[str, int]:
        with closing(self.connect()) as conn:
            total = conn.execute("SELECT COUNT(*) FROM corporate_actions").fetchone()[0]
            supported = conn.execute(
                "SELECT COUNT(*) FROM corporate_actions WHERE parse_status='supported'"
            ).fetchone()[0]
            ambiguous = conn.execute(
                "SELECT COUNT(*) FROM corporate_actions WHERE parse_status='ambiguous'"
            ).fetchone()[0]
        return {"total": int(total), "supported": int(supported), "ambiguous": int(ambiguous)}

    def delete_database_and_archive(self) -> Path:
        """Snapshot the normalised DB, then remove all feeder data and reinitialise."""
        safety_snapshot = self.create_shareable_snapshot()
        app_root = APP_DIR.resolve()
        archive_root = ARCHIVE_DIR.resolve()
        if archive_root.parent != app_root or archive_root.name != "nse_bhavcopy_archive":
            raise FeederError("Refusing to delete an unexpected archive path")
        database_path = self.path.resolve()
        if database_path.parent != app_root or database_path.name != "bhavcopy_history.db":
            raise FeederError("Refusing to delete an unexpected database path")
        state_path = STATE_PATH.resolve()
        if state_path.parent != app_root:
            raise FeederError("Refusing to delete a file outside the ChartSage folder")
        if archive_root.exists():
            shutil.rmtree(archive_root)
        with closing(self.connect()) as conn, conn:
            conn.execute("DELETE FROM official_prices")
            conn.execute("DELETE FROM bhavcopy_files")
            conn.execute("DELETE FROM corporate_actions")
            conn.execute("DELETE FROM metadata")
            conn.execute(
                "INSERT INTO metadata(key,value) VALUES('schema_version',?)",
                (SCHEMA_VERSION,),
            )
        state_path.unlink(missing_ok=True)
        return safety_snapshot

    def verify(self, progress: Callable[[str], None] | None = None) -> tuple[int, int]:
        progress = progress or (lambda _message: None)
        good = 0
        bad = 0
        with closing(self.connect()) as conn:
            records = conn.execute(
                "SELECT trade_date,filename,sha256,row_count FROM bhavcopy_files "
                "WHERE status='ok' ORDER BY trade_date"
            ).fetchall()
        for trade_date_text, relative_name, expected_hash, expected_rows in records:
            path = APP_DIR / str(relative_name)
            try:
                content = path.read_bytes()
                actual_hash = hashlib.sha256(content).hexdigest()
                if actual_hash != expected_hash:
                    raise FeederError("checksum mismatch")
                with closing(self.connect()) as conn:
                    actual_rows = conn.execute(
                        "SELECT COUNT(*) FROM official_prices WHERE trade_date=?",
                        (trade_date_text,),
                    ).fetchone()[0]
                if int(actual_rows) != int(expected_rows):
                    raise FeederError("database row-count mismatch")
                good += 1
            except Exception as exc:
                bad += 1
                progress(f"{trade_date_text}: verification failed: {exc}")
        return good, bad


class BhavcopyFeeder:
    def __init__(self, database: BhavcopyDatabase | None = None, delay_seconds: float = 0.35):
        self.database = database or BhavcopyDatabase()
        self.delay_seconds = max(0.0, float(delay_seconds))
        self.stop_event = threading.Event()
        self.ssl_context = ssl.create_default_context()
        # Some managed Windows trust chains omit a non-essential RFC flag that
        # newer OpenSSL builds enforce in strict mode. Keep hostname and chain
        # verification enabled while accepting the same trusted chain Windows
        # and browsers use.
        strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
        if strict_flag:
            self.ssl_context.verify_flags &= ~strict_flag

    def stop(self) -> None:
        self.stop_event.set()

    def _download(self, url: str) -> bytes | None:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            if self.stop_event.is_set():
                raise DownloadStopped("Stopped by user")
            try:
                request = urllib.request.Request(url, headers=HTTP_HEADERS)
                with urllib.request.urlopen(
                    request, timeout=35, context=self.ssl_context
                ) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None
                last_error = exc
                if attempt < 3:
                    time.sleep(attempt * 1.5)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(attempt * 1.5)
        raise FeederError(str(last_error or "Download failed"))

    def update_corporate_actions(
        self,
        start: date,
        end: date,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, int]:
        """Download official NSE action records and parse deterministic factors."""
        progress = progress or (lambda _done, _total, _message: None)
        headers = dict(HTTP_HEADERS)
        headers["Accept"] = "application/json,text/plain,*/*"
        landing = "https://www.nseindia.com/companies-listing/corporate-filings-actions"
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()),
            urllib.request.HTTPSHandler(context=self.ssl_context),
        )
        try:
            opener.open(urllib.request.Request(landing, headers=headers), timeout=35).read(256)
        except Exception as exc:
            raise FeederError(f"Could not initialise NSE corporate-actions session: {exc}") from exc
        years = list(range(start.year, end.year + 1))
        records: dict[tuple[str, str, str, str], dict[str, object]] = {}
        for index, year in enumerate(years, 1):
            window_start = max(start, date(year, 1, 1))
            window_end = min(end, date(year, 12, 31))
            query = urllib.parse.urlencode(
                {
                    "index": "equities",
                    "from_date": window_start.strftime("%d-%m-%Y"),
                    "to_date": window_end.strftime("%d-%m-%Y"),
                }
            )
            url = "https://www.nseindia.com/api/corporates-corporateActions?" + query
            try:
                with opener.open(
                    urllib.request.Request(url, headers=headers), timeout=45
                ) as response:
                    payload = json.load(response)
            except Exception as exc:
                raise FeederError(f"Corporate actions failed for {year}: {exc}") from exc
            for raw in payload if isinstance(payload, list) else []:
                series = str(raw.get("series", "")).strip().upper()
                symbol = str(raw.get("symbol", "")).strip().upper()
                ex_date = parse_nse_date(raw.get("exDate"))
                subject = " ".join(str(raw.get("subject", "")).split())
                if series not in SUPPORTED_SERIES or not symbol or not ex_date or not subject:
                    continue
                factor, action_types, status = technical_action_factor(subject)
                try:
                    face_value = float(raw.get("faceVal"))
                except (TypeError, ValueError):
                    face_value = None
                record_date = parse_nse_date(raw.get("recDate"))
                item = {
                    "symbol": symbol,
                    "series": series,
                    "ex_date": ex_date.isoformat(),
                    "subject": subject,
                    "company": str(raw.get("comp", "")).strip(),
                    "isin": str(raw.get("isin", "")).strip(),
                    "face_value": face_value,
                    "record_date": record_date.isoformat() if record_date else None,
                    "action_types": action_types,
                    "adjustment_factor": factor,
                    "parse_status": status,
                }
                records[(symbol, series, ex_date.isoformat(), subject)] = item
            progress(index, len(years), f"Corporate actions: loaded {year}")
            if self.delay_seconds:
                time.sleep(self.delay_seconds)
        self.database.store_corporate_actions(list(records.values()))
        return self.database.corporate_action_stats()

    def run(
        self,
        start: date,
        end: date,
        *,
        retry_failed: bool = False,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> RunSummary:
        if start < EARLIEST_SUPPORTED_DATE:
            raise FeederError(
                f"Start date cannot be before {EARLIEST_SUPPORTED_DATE.isoformat()}"
            )
        if end < start:
            raise FeederError("End date is before start date")
        dates = list(iter_weekdays(start, end))
        summary = RunSummary(considered=len(dates))
        progress = progress or (lambda _done, _total, _message: None)
        self.stop_event.clear()
        for index, trade_date in enumerate(dates, 1):
            if self.stop_event.is_set():
                raise DownloadStopped("Stopped by user")
            fmt, url = source_for_date(trade_date)
            if self.database.successful(trade_date):
                summary.already_present += 1
                progress(index, len(dates), f"{trade_date}: already present")
                continue
            previous_status = self.database.status(trade_date)
            if (
                previous_status == "not_published"
                and not retry_failed
                and trade_date < datetime.now(IST).date() - timedelta(days=7)
            ):
                summary.unavailable += 1
                progress(index, len(dates), f"{trade_date}: known holiday/unavailable")
                continue
            target = archive_path(trade_date, fmt)
            try:
                if target.is_file():
                    content = target.read_bytes()
                else:
                    downloaded_content = self._download(url)
                    if downloaded_content is None:
                        summary.unavailable += 1
                        self.database.record_failure(
                            trade_date, fmt, url, "not_published", "HTTP 404"
                        )
                        progress(index, len(dates), f"{trade_date}: holiday or unavailable")
                        continue
                    content = downloaded_content
                rows = parse_bhavcopy(content, trade_date, fmt)
                if not target.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_suffix(target.suffix + ".part")
                    temporary.write_bytes(content)
                    temporary.replace(target)
                self.database.store_success(trade_date, fmt, url, target, content, rows)
                summary.downloaded += 1
                summary.rows += len(rows)
                progress(index, len(dates), f"{trade_date}: {len(rows):,} rows stored")
            except DownloadStopped:
                raise
            except Exception as exc:
                summary.failed += 1
                self.database.record_failure(trade_date, fmt, url, "failed", str(exc))
                progress(index, len(dates), f"{trade_date}: FAILED - {exc}")
            if self.delay_seconds:
                time.sleep(self.delay_seconds)
        self._save_state(start, end, summary)
        return summary

    @staticmethod
    def _save_state(start: date, end: date, summary: RunSummary) -> None:
        payload = {
            "last_run_at": iso_now(),
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
            "summary": summary.__dict__,
        }
        STATE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use YYYY-MM-DD") from exc


def run_headless(args: argparse.Namespace) -> int:
    feeder = BhavcopyFeeder(delay_seconds=args.delay)

    def report(done: int, total: int, message: str) -> None:
        print(f"[{done}/{total}] {message}", flush=True)

    if args.verify_only:
        good, bad = feeder.database.verify(print)
        print(f"Verified: {good}; failed: {bad}")
        return 1 if bad else 0
    if args.create_snapshot:
        try:
            snapshot = feeder.database.create_shareable_snapshot()
        except FeederError as exc:
            print(f"Snapshot failed: {exc}", file=sys.stderr)
            return 1
        print(f"Shareable snapshot created: {snapshot}")
        return 0
    try:
        summary = feeder.run(
            args.start, args.end, retry_failed=args.retry_failed, progress=report
        )
    except (FeederError, DownloadStopped) as exc:
        print(f"Bhavcopy feeder stopped: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary.__dict__, indent=2))
    print(json.dumps(feeder.database.stats(), indent=2))
    return 1 if summary.failed else 0


def run_gui() -> int:
    import tkinter as tk
    from tkinter import messagebox, simpledialog, ttk

    root = tk.Tk()
    root.title("ChartSage NSE Bhavcopy Feeder")
    root.geometry("900x720")
    root.minsize(760, 540)
    events: queue.Queue[tuple[str, object]] = queue.Queue()
    feeder_holder: dict[str, BhavcopyFeeder | None] = {"feeder": None}
    busy = {"value": False}
    automatic_membership_attempt = {"date": ""}

    main = ttk.Frame(root, padding=12)
    main.pack(fill="both", expand=True)
    ttk.Label(main, text="Official NSE Bhavcopy Archive", font=("Segoe UI", 14, "bold")).pack(anchor="w")
    ttk.Label(
        main,
        text=(
            "Standalone feeder for ChartSage. It preserves official ZIP files and "
            "updates bhavcopy_history.db; it does not launch or modify scans. "
            "The 2014-2017 history supplies indicator warm-up for scans from 2018."
        ),
        wraplength=800,
    ).pack(anchor="w", pady=(2, 10))

    form = ttk.Frame(main)
    form.pack(fill="x")
    start_var = tk.StringVar(value=DEFAULT_START_DATE.isoformat())
    end_var = tk.StringVar(value=default_end_date().isoformat())
    ttk.Label(form, text="Start date (YYYY-MM-DD)").grid(row=0, column=0, sticky="w")
    ttk.Entry(form, textvariable=start_var, width=16).grid(row=1, column=0, sticky="w", padx=(0, 18))
    ttk.Label(form, text="End date (YYYY-MM-DD)").grid(row=0, column=1, sticky="w")
    ttk.Entry(form, textvariable=end_var, width=16).grid(row=1, column=1, sticky="w")
    ttk.Label(form, text=f"Database: {DATABASE_PATH}").grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))
    ttk.Label(form, text=f"Archive: {ARCHIVE_DIR}").grid(row=3, column=0, columnspan=3, sticky="w")
    ttk.Label(form, text=f"Snapshots: {SNAPSHOT_DIR}").grid(row=4, column=0, columnspan=3, sticky="w")

    controls = ttk.Frame(main)
    controls.pack(fill="x", pady=10)
    button_styles = ttk.Style(root)
    button_styles.configure(
        "CompactMaintenance.TButton",
        font=("Segoe UI", 9),
        padding=(6, 3),
    )
    start_button = tk.Button(
        controls,
        text="1. Start / Resume Backfill",
        width=24,
        font=("Segoe UI", 10, "bold"),
        foreground="white",
        background="#198754",
        activeforeground="white",
        activebackground="#157347",
        disabledforeground="#e6e6e6",
        relief="raised",
        borderwidth=1,
        padx=12,
        pady=7,
        cursor="hand2",
    )
    stop_button = ttk.Button(controls, text="Stop", state="disabled")
    verify_button = ttk.Button(controls, text="Verify Archive")
    snapshot_button = ttk.Button(controls, text="Create Shareable Snapshot")
    actions_button = ttk.Button(controls, text="2. Update Corporate Actions")
    start_button.pack(side="left")
    stop_button.pack(side="left", padx=8)
    verify_button.pack(side="left")
    snapshot_button.pack(side="left", padx=(8, 0))
    actions_button.pack(side="left", padx=(8, 0))

    maintenance = ttk.LabelFrame(main, text="Database Maintenance", padding=8)
    maintenance.pack(fill="x", pady=(0, 10))
    rebuild_button = ttk.Button(
        maintenance,
        text="Rebuild Database from Archive",
        width=29,
        style="CompactMaintenance.TButton",
    )
    reset_button = ttk.Button(maintenance, text="Reset Failed/Unavailable Dates")
    delete_button = ttk.Button(maintenance, text="Delete Database + Archive")
    rebuild_button.grid(row=0, column=0, sticky="w", padx=(0, 6))
    reset_button.grid(row=0, column=1, sticky="ew", padx=6)
    delete_button.grid(row=0, column=2, sticky="ew", padx=(6, 0))
    maintenance.columnconfigure(0, weight=0)
    maintenance.columnconfigure(1, weight=1)
    maintenance.columnconfigure(2, weight=1)
    ttk.Label(
        maintenance,
        text=(
            "For normal additions or missing dates, use 1. Start / Resume Backfill. "
            "Rebuild and deletion create a database snapshot first. Rebuild keeps "
            "the original NSE ZIP archive; deletion permanently removes it."
        ),
        wraplength=840,
    ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

    membership_frame = ttk.LabelFrame(
        main, text="Historical NIFTY Membership", padding=8
    )
    membership_frame.pack(fill="x", pady=(0, 10))
    membership_button = ttk.Button(
        membership_frame, text="3. Update Historical NIFTY Membership"
    )
    membership_button.grid(row=0, column=0, sticky="w", padx=(0, 12))
    membership_coverage_var = tk.StringVar(value="Checking installed coverage...")
    ttk.Label(
        membership_frame, textvariable=membership_coverage_var
    ).grid(row=0, column=1, sticky="w")
    membership_frame.columnconfigure(1, weight=1)
    ttk.Label(
        membership_frame,
        text=(
            "This is independent of Bhavcopy prices. It checks official NSE Indices "
            "notices and current constituent lists, validates a temporary rebuilt "
            "database, and replaces the live membership database only after success. "
            "One automatic check is made after 18:30 IST when coverage is behind."
        ),
        wraplength=840,
    ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

    progress_var = tk.DoubleVar(value=0)
    bar = ttk.Progressbar(main, variable=progress_var, maximum=100)
    bar.pack(fill="x")
    status_var = tk.StringVar(value="Ready")
    ttk.Label(main, textvariable=status_var).pack(anchor="w", pady=(4, 8))
    log = tk.Text(main, height=18, wrap="word", state="disabled")
    log.pack(fill="both", expand=True)

    def append_log(message: str) -> None:
        log.configure(state="normal")
        log.insert("end", message + "\n")
        log.see("end")
        log.configure(state="disabled")

    def set_running(running: bool, stoppable: bool = False) -> None:
        busy["value"] = running
        start_button.configure(state="disabled" if running else "normal")
        verify_button.configure(state="disabled" if running else "normal")
        snapshot_button.configure(state="disabled" if running else "normal")
        actions_button.configure(state="disabled" if running else "normal")
        rebuild_button.configure(state="disabled" if running else "normal")
        reset_button.configure(state="disabled" if running else "normal")
        delete_button.configure(state="disabled" if running else "normal")
        membership_button.configure(state="disabled" if running else "normal")
        stop_button.configure(state="normal" if running and stoppable else "disabled")

    def worker(start: date, end: date) -> None:
        feeder = BhavcopyFeeder()
        feeder_holder["feeder"] = feeder
        try:
            summary = feeder.run(
                start,
                end,
                progress=lambda done, total, message: events.put(
                    ("progress", (done, total, message))
                ),
            )
            events.put(("finished", summary))
        except DownloadStopped as exc:
            events.put(("stopped", str(exc)))
        except Exception as exc:
            events.put(("error", str(exc)))

    def start_run() -> None:
        try:
            start = date.fromisoformat(start_var.get().strip())
            end = date.fromisoformat(end_var.get().strip())
            if start < EARLIEST_SUPPORTED_DATE or end < start:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Invalid dates",
                f"Enter a valid range on or after {EARLIEST_SUPPORTED_DATE.isoformat()}.",
            )
            return
        set_running(True, stoppable=True)
        progress_var.set(0)
        append_log(f"Starting {start} through {end}")
        threading.Thread(target=worker, args=(start, end), daemon=True).start()

    def stop_run() -> None:
        feeder = feeder_holder.get("feeder")
        if feeder:
            feeder.stop()
            status_var.set("Stopping after the current request...")

    def verify_worker() -> None:
        try:
            database = BhavcopyDatabase()
            good, bad = database.verify(lambda message: events.put(("log", message)))
            events.put(("verified", (good, bad)))
        except Exception as exc:
            events.put(("error", str(exc)))

    def verify_run() -> None:
        set_running(True)
        status_var.set("Verifying archive checksums...")
        threading.Thread(target=verify_worker, daemon=True).start()

    def snapshot_worker() -> None:
        try:
            path = BhavcopyDatabase().create_shareable_snapshot()
            events.put(("snapshot", path))
        except Exception as exc:
            events.put(("error", str(exc)))

    def snapshot_run() -> None:
        set_running(True)
        status_var.set("Creating a consistent compressed database snapshot...")
        threading.Thread(target=snapshot_worker, daemon=True).start()

    def actions_worker(start: date, end: date) -> None:
        try:
            feeder = BhavcopyFeeder()
            stats = feeder.update_corporate_actions(
                start,
                end,
                progress=lambda done, total, message: events.put(
                    ("progress", (done, total, message))
                ),
            )
            events.put(("actions", stats))
        except Exception as exc:
            events.put(("error", str(exc)))

    def actions_run() -> None:
        try:
            start = date.fromisoformat(start_var.get().strip())
            end = date.fromisoformat(end_var.get().strip())
        except ValueError:
            messagebox.showerror("Invalid dates", "Enter valid YYYY-MM-DD dates first.")
            return
        set_running(True)
        progress_var.set(0)
        status_var.set("Downloading and validating NSE corporate actions...")
        threading.Thread(target=actions_worker, args=(start, end), daemon=True).start()

    def refresh_membership_coverage_label() -> dict[str, object]:
        try:
            coverage = membership_coverage_summary(APP_DIR)
            supported = int(coverage.get("supported_indices", 0))
            if supported and int(coverage["verified_indices"]) == supported:
                membership_coverage_var.set(
                    "Verified coverage: "
                    f"{coverage['start_date']} to {coverage['end_date']} "
                    f"({supported} indices)"
                )
            else:
                membership_coverage_var.set("Verified membership database is incomplete")
            return coverage
        except Exception as exc:
            membership_coverage_var.set(f"Membership coverage unavailable: {exc}")
            return {"verified_indices": 0, "start_date": "", "end_date": ""}

    def membership_worker(automatic: bool) -> None:
        try:
            result = update_historical_membership(
                APP_DIR,
                progress=lambda done, total, message: events.put(
                    ("progress", (done, total, message))
                ),
            )
            events.put(("membership", (result, automatic)))
        except Exception as exc:
            events.put(("membership_error", (str(exc), automatic)))

    def membership_run(automatic: bool = False) -> None:
        if busy["value"]:
            return
        set_running(True)
        progress_var.set(0)
        status_var.set("Checking official historical NIFTY membership sources...")
        append_log(
            "Starting automatic historical membership check."
            if automatic
            else "Starting historical NIFTY membership update."
        )
        threading.Thread(
            target=membership_worker, args=(automatic,), daemon=True
        ).start()

    def maybe_auto_membership_update() -> None:
        now = datetime.now(IST)
        today = now.date().isoformat()
        coverage = refresh_membership_coverage_label()
        if (
            not busy["value"]
            and now.time() >= clock_time(18, 30)
            and str(coverage.get("end_date", "")) < today
            and automatic_membership_attempt["date"] != today
        ):
            automatic_membership_attempt["date"] = today
            membership_run(automatic=True)
        root.after(15 * 60 * 1000, maybe_auto_membership_update)

    def rebuild_worker() -> None:
        try:
            result = BhavcopyDatabase().rebuild_from_archive(
                progress=lambda done, total, message: events.put(
                    ("progress", (done, total, message))
                )
            )
            events.put(("rebuilt", result))
        except Exception as exc:
            events.put(("error", str(exc)))

    def rebuild_run() -> None:
        if not messagebox.askyesno(
            "Rebuild the entire Bhavcopy database?",
            "This operation can take considerable time because every retained "
            "official NSE ZIP file must be read and imported again. The duration "
            "depends on the size of your archive and the speed of this PC.\n\n"
            "A rebuild is NOT normally needed for daily updates, missing dates, "
            "or strategy changes. Use 1. Start / Resume Backfill for those tasks.\n\n"
            "Rebuild only when the database is missing, damaged or inconsistent, "
            "or when you deliberately need to reconstruct it from the archive. "
            "A safety snapshot will be created first and the original ZIP archive "
            "will be retained.\n\n"
            "Close the main ChartSage scanner before continuing.\n\n"
            "Do you want to start the full rebuild now?",
            icon="warning",
            default="no",
            parent=root,
        ):
            return
        set_running(True)
        progress_var.set(0)
        status_var.set("Creating safety snapshot and rebuilding from the archive...")
        threading.Thread(target=rebuild_worker, daemon=True).start()

    def reset_worker() -> None:
        try:
            count = BhavcopyDatabase().reset_unsuccessful_dates()
            events.put(("reset", count))
        except Exception as exc:
            events.put(("error", str(exc)))

    def reset_run() -> None:
        if not messagebox.askyesno(
            "Reset Failed Dates",
            "Forget all failed and unavailable-date checks?\n\n"
            "The next backfill will retry those dates. Successful price data and "
            "archived ZIP files will not be changed.",
        ):
            return
        set_running(True)
        status_var.set("Resetting failed and unavailable-date checks...")
        threading.Thread(target=reset_worker, daemon=True).start()

    def delete_worker() -> None:
        try:
            snapshot = BhavcopyDatabase().delete_database_and_archive()
            events.put(("deleted", snapshot))
        except Exception as exc:
            events.put(("error", str(exc)))

    def delete_run() -> None:
        phrase = "DELETE ALL BHAVCOPY DATA"
        entered = simpledialog.askstring(
            "Permanent Bhavcopy Deletion",
            "This permanently deletes bhavcopy_history.db, the original NSE ZIP "
            "archive, and feeder state. A database-only snapshot will be created, "
            "but the raw ZIP archive cannot be recovered from it.\n\n"
            f"Close the main ChartSage scanner, then type:\n{phrase}",
            parent=root,
        )
        if entered != phrase:
            if entered is not None:
                messagebox.showwarning(
                    "Deletion cancelled", "The confirmation phrase did not match."
                )
            return
        set_running(True)
        status_var.set("Creating safety snapshot, then deleting feeder data...")
        threading.Thread(target=delete_worker, daemon=True).start()

    def poll_events() -> None:
        try:
            while True:
                kind, payload = events.get_nowait()
                if kind == "progress":
                    done, total, message = payload  # type: ignore[misc]
                    progress_var.set((done / max(1, total)) * 100)
                    status_var.set(str(message))
                    append_log(str(message))
                elif kind == "log":
                    append_log(str(payload))
                elif kind == "finished":
                    summary = payload
                    set_running(False)
                    feeder_holder["feeder"] = None
                    stats = BhavcopyDatabase().stats()
                    status_var.set(
                        f"Complete: {stats['files']:,} files, {stats['rows']:,} official rows"
                    )
                    append_log(f"Run complete: {summary}")
                    messagebox.showinfo(
                        "Bhavcopy backfill completed",
                        "The 1. Start / Resume Backfill process has completed.\n\n"
                        f"Dates considered: {summary.considered:,}\n"
                        f"Downloaded: {summary.downloaded:,}\n"
                        f"Already present: {summary.already_present:,}\n"
                        f"Unavailable: {summary.unavailable:,}\n"
                        f"Failed: {summary.failed:,}\n"
                        f"Price rows added: {summary.rows:,}\n\n"
                        f"Database total: {stats['files']:,} files and "
                        f"{stats['rows']:,} official rows.",
                        parent=root,
                    )
                elif kind == "verified":
                    good, bad = payload  # type: ignore[misc]
                    set_running(False)
                    status_var.set(f"Verification complete: {good} good, {bad} failed")
                    append_log(
                        f"Archive verification completed: {good:,} valid, "
                        f"{bad:,} failed."
                    )
                    verification_message = (
                        "Archive verification has completed.\n\n"
                        f"Valid archive files: {good:,}\n"
                        f"Files with problems: {bad:,}"
                    )
                    if bad:
                        messagebox.showwarning(
                            "Archive verification completed with problems",
                            verification_message,
                            parent=root,
                        )
                    else:
                        messagebox.showinfo(
                            "Archive verification completed",
                            verification_message,
                            parent=root,
                        )
                elif kind == "snapshot":
                    set_running(False)
                    status_var.set(f"Shareable snapshot created: {payload}")
                    append_log(f"Shareable snapshot created: {payload}")
                    messagebox.showinfo(
                        "Snapshot created",
                        f"The consistent snapshot package was created at:\n\n{payload}",
                    )
                elif kind == "actions":
                    set_running(False)
                    stats = payload  # type: ignore[assignment]
                    status_var.set(
                        f"Corporate actions: {stats['supported']:,} supported; "
                        f"{stats['ambiguous']:,} require review"
                    )
                    append_log(
                        f"Corporate actions updated: {stats['total']:,} total, "
                        f"{stats['supported']:,} supported, "
                        f"{stats['ambiguous']:,} ambiguous."
                    )
                    messagebox.showinfo(
                        "Corporate actions update completed",
                        "The corporate-actions update has completed.\n\n"
                        f"Total records: {stats['total']:,}\n"
                        f"Supported: {stats['supported']:,}\n"
                        f"Require review: {stats['ambiguous']:,}",
                        parent=root,
                    )
                elif kind == "membership":
                    result, automatic = payload  # type: ignore[misc]
                    set_running(False)
                    progress_var.set(100)
                    refresh_membership_coverage_label()
                    status_var.set(
                        "Historical NIFTY membership updated through "
                        f"{result['end_date']}"
                    )
                    append_log(
                        "Historical membership update complete: "
                        f"{result['new_notices']} new notices; "
                        f"verified through {result['end_date']}."
                    )
                    if not automatic:
                        messagebox.showinfo(
                            "Historical membership updated",
                            "Verified point-in-time membership now covers\n"
                            f"{result['start_date']} through {result['end_date']}\n\n"
                            "Bhavcopy prices were not changed.",
                        )
                elif kind == "membership_error":
                    error, automatic = payload  # type: ignore[misc]
                    set_running(False)
                    refresh_membership_coverage_label()
                    status_var.set("Historical membership update failed; previous database preserved")
                    append_log(f"Historical membership update failed: {error}")
                    if not automatic:
                        messagebox.showerror(
                            "Historical membership update",
                            f"The update could not be completed. The previous working "
                            f"database was preserved.\n\n{error}",
                        )
                elif kind == "rebuilt":
                    files, rows, snapshot = payload  # type: ignore[misc]
                    set_running(False)
                    progress_var.set(100)
                    status_var.set(
                        f"Rebuild complete: {files:,} files, {rows:,} official rows"
                    )
                    append_log(
                        f"Rebuild complete. Safety snapshot: {snapshot}"
                    )
                    messagebox.showinfo(
                        "Database rebuilt",
                        f"Rebuilt {files:,} files and {rows:,} price rows.\n\n"
                        f"Safety snapshot:\n{snapshot}",
                    )
                elif kind == "reset":
                    set_running(False)
                    count = int(payload)
                    status_var.set(f"Reset complete: {count:,} dates will be retried")
                    append_log(f"Reset {count:,} failed/unavailable date checks.")
                    messagebox.showinfo(
                        "Reset completed",
                        f"The reset has completed.\n\n{count:,} failed or unavailable "
                        "date checks will be retried during the next backfill.",
                        parent=root,
                    )
                elif kind == "deleted":
                    set_running(False)
                    progress_var.set(0)
                    status_var.set("Feeder database and raw archive deleted; empty database created")
                    append_log(f"Data deleted. Database safety snapshot: {payload}")
                    messagebox.showinfo(
                        "Bhavcopy data deleted",
                        "The database and raw ZIP archive were removed. An empty "
                        f"database was created.\n\nDatabase snapshot:\n{payload}",
                    )
                elif kind == "stopped":
                    set_running(False)
                    feeder_holder["feeder"] = None
                    status_var.set("Stopped; progress was saved and Resume will skip completed dates.")
                    append_log(f"Backfill stopped: {payload}")
                    messagebox.showinfo(
                        "Bhavcopy backfill stopped",
                        "The backfill has stopped. Progress was saved, and Start / "
                        "Resume will skip dates that are already complete.",
                        parent=root,
                    )
                elif kind == "error":
                    set_running(False)
                    feeder_holder["feeder"] = None
                    status_var.set("Failed")
                    messagebox.showerror("Bhavcopy feeder", str(payload))
        except queue.Empty:
            pass
        root.after(150, poll_events)

    start_button.configure(command=start_run)
    stop_button.configure(command=stop_run)
    verify_button.configure(command=verify_run)
    snapshot_button.configure(command=snapshot_run)
    actions_button.configure(command=actions_run)
    membership_button.configure(command=membership_run)
    rebuild_button.configure(command=rebuild_run)
    reset_button.configure(command=reset_run)
    delete_button.configure(command=delete_run)
    root.after(150, poll_events)
    root.after(2000, maybe_auto_membership_update)
    root.mainloop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headless", action="store_true", help="Run without the desktop GUI")
    parser.add_argument("--start", type=parse_iso_date, default=DEFAULT_START_DATE)
    parser.add_argument("--end", type=parse_iso_date, default=default_end_date())
    parser.add_argument("--delay", type=float, default=0.35, help="Delay between NSE requests")
    parser.add_argument("--retry-failed", action="store_true", help="Retry previously unsuccessful dates")
    parser.add_argument("--verify-only", action="store_true", help="Verify archive checksums and exit")
    parser.add_argument(
        "--create-snapshot",
        action="store_true",
        help="Create a consistent shareable database ZIP and exit",
    )
    parser.add_argument(
        "--portable-smoke-test",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.portable_smoke_test:
        # The feeder uses Tk rather than Qt. A Tcl interpreter verifies that
        # its bundled Python/Tcl runtime loads without opening a window.
        import tkinter

        tkinter.Tcl()
        required = (DATABASE_PATH, ARCHIVE_DIR, APP_DIR / "historical_index_membership.db")
        return 0 if all(path.exists() for path in required) else 3
    if args.headless or args.verify_only or args.create_snapshot:
        return run_headless(args)
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
